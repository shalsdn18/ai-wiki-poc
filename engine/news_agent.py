"""Periodically collect official AI news into an Obsidian Inbox."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import engine.config

LOGGER = logging.getLogger(__name__)
INTERVAL_SECONDS = 30 * 60
DEFAULT_DB_PATH = Path("news_agent.db")


@dataclass(frozen=True)
class NewsItem:
    provider: str
    title: str
    url: str
    content: str
    published_at: datetime


FEEDS = {
    "openai": "https://openai.com/news/rss.xml",
    "google-ai": "https://blog.google/technology/ai/rss/",
    "anthropic": "https://www.anthropic.com/news",
    "ollama": "https://ollama.com/blog/rss.xml",
}


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "ai-wiki-news-agent/1.0"})
    with urlopen(request, timeout=20) as response:
        return response.read()


def _published(value: str) -> datetime:
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=result.tzinfo or timezone.utc)


def _text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] in names and child.text:
            return child.text.strip()
    return ""


def parse_feed(data: bytes, provider: str, base_url: str, limit: int = 20) -> list[NewsItem]:
    root = ElementTree.fromstring(data)
    items: list[NewsItem] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        title = _text(element, ("title",))
        url = _text(element, ("link",))
        if not url:
            for child in element:
                if child.tag.rsplit("}", 1)[-1] == "link" and child.attrib.get("href"):
                    url = child.attrib["href"]
                    break
        url = urljoin(base_url, url)
        content = _text(element, ("encoded", "content", "description", "summary"))
        published = _text(element, ("pubDate", "published", "updated", "date"))
        if not all((title, url, content, published)):
            continue
        items.append(
            NewsItem(provider, title, url, re.sub(r"<[^>]+>", " ", content), _published(published))
        )
    return sorted(items, key=lambda item: item.published_at, reverse=True)[:limit]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("""CREATE TABLE IF NOT EXISTS seen_urls (
        url TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        seen_at TEXT NOT NULL
    )""")
    return connection


def _source_id(url: str) -> str:
    return f"news-{sha256(url.encode('utf-8')).hexdigest()[:20]}"


def _already_seen(connection: sqlite3.Connection, url: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,)).fetchone() is not None
    )


def _markdown(item: NewsItem) -> str:
    return (
        f"---\nsource_url: {item.url}\nsource_type: news\nprovider: {item.provider}\n---\n\n"
        f"# {item.title}\n\n{item.content}\n\n"
        f"Source: {item.url}\nPublished: {item.published_at.isoformat()}\n"
    )


def collect_once(
    vault_path: Path | None = None,
    db_path: Path | None = None,
    fetch=_fetch,
    retry_count: int = 3,
    sleep=time.sleep,
) -> int:
    """Fetch feeds once and write only URLs not previously recorded."""
    vault = (vault_path or Path(os.environ.get("OBSIDIAN_VAULT_PATH", ""))).expanduser().resolve()
    if not vault.is_dir():
        raise RuntimeError(f"Obsidian Vault directory does not exist: {vault}")
    inbox = vault / os.environ.get("NEWS_INBOX_SUBDIR", "Inbox")
    resolved_db = db_path or Path(os.environ.get("NEWS_AGENT_DB_PATH", str(DEFAULT_DB_PATH)))
    created = 0
    with _connect(resolved_db) as connection:
        for provider, url in FEEDS.items():
            data = None
            for attempt in range(retry_count + 1):
                try:
                    data = fetch(url)
                    break
                except Exception as exc:
                    if attempt >= retry_count:
                        LOGGER.warning("News feed %s failed: %s", provider, exc)
                    else:
                        sleep(2**attempt)
            if data is None:
                continue
            try:
                if provider == "anthropic":
                    from collectors.real_collector import _parse_anthropic

                    items = [
                        NewsItem(
                            item.source_tier, item.title, item.url, item.content, item.published_at
                        )
                        for item in _parse_anthropic(data, 20)
                    ]
                else:
                    items = parse_feed(data, provider, url)
            except Exception as exc:
                LOGGER.warning("News feed %s parse failed: %s", provider, exc)
                continue
            for item in items:
                if _already_seen(connection, item.url):
                    continue
                source_id = _source_id(item.url)
                filename = f"{item.published_at:%Y%m%d}-{source_id}.md"
                inbox.mkdir(parents=True, exist_ok=True)
                (inbox / filename).write_text(_markdown(item), encoding="utf-8", newline="\n")
                connection.execute(
                    "INSERT INTO seen_urls(url, source_id, seen_at) VALUES (?, ?, ?)",
                    (item.url, source_id, datetime.now(timezone.utc).isoformat()),
                )
                created += 1
    return created


def run_forever(interval_seconds: int = INTERVAL_SECONDS, **kwargs) -> None:
    while True:
        collect_once(**kwargs)
        time.sleep(interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=INTERVAL_SECONDS)
    args = parser.parse_args()
    if args.once:
        collect_once()
    else:
        try:
            run_forever(args.interval_seconds)
        except KeyboardInterrupt:
            LOGGER.info("News agent stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
