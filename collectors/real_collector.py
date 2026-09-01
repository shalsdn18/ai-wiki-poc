"""Collect recent entries from official OpenAI, Google, and Anthropic sources."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from engine.schemas import SourceRecord

LOGGER = logging.getLogger(__name__)
Fetch = Callable[[str], bytes]

OPENAI_FEED = "https://openai.com/news/rss.xml"
GEMINI_FEED = "https://blog.google/products-and-platforms/products/gemini/rss/"
CLAUDE_NEWS = "https://www.anthropic.com/news"


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "ai-wiki-poc/1.0"})
    with urlopen(request, timeout=20) as response:
        return response.read()


def _clean_html(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _stable_source_id(provider: str, url: str) -> str:
    parts = urlsplit(url.strip())
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))
    return f"{provider}-{sha256(normalized.encode('utf-8')).hexdigest()[:20]}"


def _published(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            for date_format in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    parsed = datetime.strptime(value, date_format)
                    break
                except ValueError:
                    continue
            else:
                raise
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _child_text(item: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in item:
        if child.tag.rsplit("}", 1)[-1] in names and child.text:
            return child.text.strip()
    return ""


def _parse_rss(data: bytes, provider: str, tier: str, limit: int, keyword: str | None = None) -> list[SourceRecord]:
    root = ElementTree.fromstring(data)
    records: list[SourceRecord] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        title = _child_text(item, ("title",))
        url = _child_text(item, ("link",))
        if not url:
            for child in item:
                if child.tag.rsplit("}", 1)[-1] == "link" and child.attrib.get("href"):
                    url = child.attrib["href"]
                    break
        raw_content = _child_text(item, ("encoded", "content", "description", "summary"))
        published = _child_text(item, ("pubDate", "published", "updated", "date"))
        searchable = f"{title} {_clean_html(raw_content)}".lower()
        if keyword and keyword.lower() not in searchable:
            continue
        if not all((title, url, raw_content, published)):
            continue
        records.append(
            SourceRecord(
                source_id=_stable_source_id(provider, url),
                title=title,
                content=_clean_html(raw_content),
                url=url,
                published_at=_published(published),
                source_tier=tier,
            )
        )
    records.sort(key=lambda record: record.published_at, reverse=True)
    return records[:limit]


class _AnthropicNewsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict[str, object]] = []
        self._entry: dict[str, object] | None = None
        self._heading_depth = 0
        self._paragraph_depth = 0
        self._time_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        href = attributes.get("href") or ""
        if tag == "a" and href.startswith("/news/"):
            self._entry = {"url": urljoin(CLAUDE_NEWS, href), "title": [], "content": [], "published": ""}
        if self._entry is None:
            return
        if tag in {"h2", "h3", "h4"}:
            self._heading_depth += 1
        elif tag == "p":
            self._paragraph_depth += 1
        elif tag == "time" and attributes.get("datetime"):
            self._entry["published"] = attributes["datetime"] or ""
            self._time_depth += 1
        elif tag == "time":
            self._time_depth += 1

    def handle_data(self, data: str) -> None:
        if self._entry is None or not data.strip():
            return
        if self._time_depth and not self._entry["published"]:
            self._entry["published"] = data.strip()
        elif self._heading_depth:
            self._entry["title"].append(data.strip())  # type: ignore[union-attr]
        elif self._paragraph_depth:
            self._entry["content"].append(data.strip())  # type: ignore[union-attr]

    def handle_endtag(self, tag: str) -> None:
        if self._entry is None:
            return
        if tag in {"h2", "h3", "h4"} and self._heading_depth:
            self._heading_depth -= 1
        elif tag == "p" and self._paragraph_depth:
            self._paragraph_depth -= 1
        elif tag == "time" and self._time_depth:
            self._time_depth -= 1
        elif tag == "a":
            title = " ".join(self._entry["title"])  # type: ignore[arg-type]
            content = " ".join(self._entry["content"])  # type: ignore[arg-type]
            if title and content and self._entry["published"]:
                self._entry.update(title=title, content=content)
                self.entries.append(self._entry)
            self._entry = None


def _parse_anthropic(data: bytes, limit: int) -> list[SourceRecord]:
    parser = _AnthropicNewsParser()
    parser.feed(data.decode("utf-8"))
    unique: dict[str, SourceRecord] = {}
    for entry in parser.entries:
        url = str(entry["url"])
        unique[url] = SourceRecord(
            source_id=_stable_source_id("claude", url),
            title=str(entry["title"]),
            content=str(entry["content"]),
            url=url,
            published_at=_published(str(entry["published"])),
            source_tier="official-anthropic-newsroom",
        )
    return sorted(unique.values(), key=lambda record: record.published_at, reverse=True)[:limit]


def _safely(name: str, operation: Callable[[], list[SourceRecord]]) -> list[SourceRecord]:
    try:
        return operation()
    except Exception as exc:  # A single external source must not stop other collectors.
        LOGGER.warning("%s collector failed: %s", name, exc)
        return []


def collect_openai(fetch: Fetch = _fetch, limit: int = 5) -> list[SourceRecord]:
    return _safely("openai", lambda: _parse_rss(fetch(OPENAI_FEED), "openai", "official-openai-news", limit))


def collect_gemini(fetch: Fetch = _fetch, limit: int = 5) -> list[SourceRecord]:
    return _safely(
        "gemini",
        lambda: _parse_rss(fetch(GEMINI_FEED), "gemini", "official-google-gemini-blog", limit),
    )


def collect_claude(fetch: Fetch = _fetch, limit: int = 5) -> list[SourceRecord]:
    return _safely("claude", lambda: _parse_anthropic(fetch(CLAUDE_NEWS), limit))


def collect_all(fetch: Fetch = _fetch, limit: int = 5) -> dict[str, list[SourceRecord]]:
    """Collect providers independently so one failure cannot suppress the others."""
    return {
        "openai": collect_openai(fetch, limit),
        "gemini": collect_gemini(fetch, limit),
        "claude": collect_claude(fetch, limit),
    }
