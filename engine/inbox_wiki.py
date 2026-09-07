"""Classify user-supplied Markdown and render deterministic category Wikis."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import engine.config
from engine.schemas import InboxKnowledgePayload, KnowledgeCategory, SourceRecord
from engine.wiki_poc import DEFAULT_MODEL

LOGGER = logging.getLogger(__name__)
DEFAULT_INBOX = Path("inbox")
DEFAULT_WIKI_ROOT = Path("wiki")
DEFAULT_STATE = DEFAULT_INBOX / ".processed.json"
CATEGORIES: tuple[KnowledgeCategory, ...] = ("ai", "investment", "philosophy", "misc")
_SUSPICIOUS_MOJIBAKE = re.compile(r"[\ufffdÃÂÐÑ]|(?:ì|ë|ê|í|ï»¿)[\x80-\xff]")
_JSON_FIELD_FRAGMENT = re.compile(r'[`{}]|"(?:title|summary|key_facts|topic|primary_category)"\s*:')
_REPEATED_TOKEN = re.compile(r"(.)\1{4,}")


class InboxClassifier(Protocol):
    def classify(self, source: SourceRecord) -> InboxKnowledgePayload: ...


class GeminiInboxClassifier:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        from google import genai

        resolved_key = api_key or os.getenv("GEMINI_API_KEY")
        if not resolved_key:
            raise RuntimeError("GEMINI_API_KEY is required for inbox classification")
        self._client = genai.Client(api_key=resolved_key)
        self._model = model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

    def classify(
        self,
        source: SourceRecord,
        category_hint: KnowledgeCategory | None = None,
    ) -> InboxKnowledgePayload:
        from google.genai import types

        korean_instruction = ""
        if re.search(r"[가-힣]", source.content):
            korean_instruction = (
                "The source is Korean. Write title, summary, key_facts, and topic in natural Korean. "
                "Do not distort the meaning of the source, and do not generate translation-like or "
                "garbled strings. Product names, technical terms, and proper nouns may retain their "
                "original spelling. "
            )
        prompt = (
            "Classify and extract the supplied personal knowledge source. "
            "primary_category must be exactly one of ai, investment, philosophy, misc. "
            "categories may contain multiple values but must include primary_category. "
            "importance is an integer from 1 to 5. Extract one concise topic and a "
            "related_topics list. Return structured data only, not Markdown. "
            f"The source path suggests category_hint={category_hint!r}; treat it only as a hint "
            "and independently validate the final categories.\n\n"
            + korean_instruction
            + json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2)
        )
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=InboxKnowledgePayload,
                temperature=0,
            ),
        )
        if response.parsed is not None:
            return InboxKnowledgePayload.model_validate(response.parsed)
        return InboxKnowledgePayload.model_validate_json(response.text)


def normalize_url(value: str) -> str:
    value = value.strip()
    markdown_link = re.fullmatch(r"\[[^]]*]\((https?://[^)]+)\)", value)
    if markdown_link:
        value = markdown_link.group(1)
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"Invalid source_url: {value}")
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise ValueError("Unclosed inbox frontmatter") from exc
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Invalid frontmatter line: {line}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, "\n".join(lines[end + 1 :]).strip()


def read_inbox_source(path: Path) -> tuple[SourceRecord, str | None, str]:
    metadata, content = _parse_frontmatter(path.read_text(encoding="utf-8"))
    if not content:
        raise ValueError("Inbox document content is empty")
    source_url = normalize_url(metadata["source_url"]) if metadata.get("source_url") else None
    content_hash = sha256(content.encode("utf-8")).hexdigest()
    identity = source_url or f"content:{content_hash}"
    source_id = f"inbox-{sha256(identity.encode('utf-8')).hexdigest()[:20]}"
    local_url = f"inbox:///{quote(path.name)}"
    return (
        SourceRecord(
            source_id=source_id,
            title=metadata.get("title") or path.stem,
            content=content,
            url=source_url or local_url,
            published_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            source_tier=f"user-inbox:{metadata.get('type', 'document')}",
        ),
        source_url,
        content_hash,
    )


def _empty_processed_state() -> dict:
    return {"version": 1, "processed_sources": {}}


def load_processed_state(path: Path = DEFAULT_STATE) -> dict:
    if not path.exists():
        return _empty_processed_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state.get("processed_sources"), dict):
        raise TypeError(f"Invalid processed state: {path}")
    return state


def _is_duplicate(state: dict, source_id: str, content_hash: str) -> bool:
    sources = state["processed_sources"]
    return source_id in sources or any(
        item.get("content_hash") == content_hash for item in sources.values()
    )


def _category_path(root: Path, category: KnowledgeCategory) -> Path:
    return root / category / "index.md"


def _load_category_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"Invalid category Wiki frontmatter: {path}")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"Unclosed category Wiki frontmatter: {path}")
    state = json.loads(text[4:end])
    return state.get("entries", [])


def _render_category(category: KnowledgeCategory, entries: Sequence[dict]) -> str:
    ordered = sorted(entries, key=lambda item: (item["processed_at"], item["title"]), reverse=True)
    frontmatter = json.dumps(
        {"category": category, "entries": ordered}, ensure_ascii=False, indent=2, sort_keys=True
    )
    sections = []
    for entry in ordered:
        facts = "\n".join(f"- {fact}" for fact in entry["key_facts"]) or "- _None._"
        tags = ", ".join(f"`{tag}`" for tag in entry["tags"]) or "_None_"
        related_topics = ", ".join(entry.get("related_topics", [])) or "_None_"
        source = (
            f"[{entry['source_url']}]({entry['source_url']})"
            if entry["source_url"]
            else "Local inbox document"
        )
        sections.append(
            f"## {entry['title']}\n\n"
            f"{entry['summary']}\n\n"
            f"- Importance: {entry['importance']}/5\n"
            f"- Topic: {entry.get('topic', entry['title'])}\n"
            f"- Related topics: {related_topics}\n"
            f"- Categories: {', '.join(entry['categories'])}\n"
            f"- Tags: {tags}\n"
            f"- Source: {source}\n"
            f"- Input file: `{entry['input_filename']}`\n"
            f"- Processed at: {entry['processed_at']}\n\n"
            f"### Key Facts\n\n{facts}"
        )
    body = "\n\n".join(sections) if sections else "_No knowledge saved yet._"
    return f"---\n{frontmatter}\n---\n\n# {category.title()} Knowledge Wiki\n\n{body}\n"


def _validated_payload(payload: object, source_url: str | None) -> InboxKnowledgePayload:
    validated = InboxKnowledgePayload.model_validate(payload)
    categories = list(dict.fromkeys(validated.categories))
    tags = list(dict.fromkeys(tag.strip() for tag in validated.tags if tag.strip()))
    facts = list(dict.fromkeys(fact.strip() for fact in validated.key_facts if fact.strip()))
    related_topics = list(
        dict.fromkeys(topic.strip() for topic in validated.related_topics if topic.strip())
    )
    normalized = validated.model_copy(
        update={
            "categories": categories,
            "tags": tags,
            "key_facts": facts,
            "related_topics": related_topics,
            "source_url": source_url,
        }
    )
    for field in ("title", "summary", "topic"):
        value = getattr(normalized, field).strip()
        if not value:
            raise ValueError(f"{field} must not be empty")
        if "\x00" in value or _SUSPICIOUS_MOJIBAKE.search(value):
            raise ValueError(f"{field} contains suspicious encoding text")
        if _JSON_FIELD_FRAGMENT.search(value) or _REPEATED_TOKEN.search(value):
            raise ValueError(f"{field} contains malformed generated text")
    for field, values in (
        ("key_facts", normalized.key_facts),
        ("tags", normalized.tags),
        ("related_topics", normalized.related_topics),
    ):
        for value in values:
            if (
                not value.strip()
                or _SUSPICIOUS_MOJIBAKE.search(value)
                or _JSON_FIELD_FRAGMENT.search(value)
                or _REPEATED_TOKEN.search(value)
            ):
                raise ValueError(f"{field} contains malformed generated text")
    if normalized.primary_category not in normalized.categories:
        raise ValueError("primary_category must also appear in categories")
    return normalized


def process_inbox(
    classifier: InboxClassifier | None,
    inbox_dir: Path = DEFAULT_INBOX,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    state_path: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, str]:
    state_path = state_path or inbox_dir / ".processed.json"
    state = load_processed_state(state_path)
    results: dict[str, str] = {}
    shared_classifier = classifier
    for path in sorted(inbox_dir.glob("*.md"), key=lambda item: item.name.lower()):
        try:
            source, source_url, content_hash = read_inbox_source(path)
            if _is_duplicate(state, source.source_id, content_hash):
                results[path.name] = "duplicate"
                continue
            if shared_classifier is None:
                shared_classifier = GeminiInboxClassifier()
            payload = _validated_payload(shared_classifier.classify(source), source_url)
            processed_at = now().astimezone(timezone.utc).isoformat()
            entry = {
                "source_id": source.source_id,
                "source_url": source_url,
                "input_filename": path.name,
                "processed_at": processed_at,
                **payload.model_dump(mode="json"),
            }
            category_path = _category_path(wiki_root, payload.primary_category)
            entries = [
                item
                for item in _load_category_entries(category_path)
                if item["source_id"] != source.source_id
            ]
            entries.append(entry)
            category_path.parent.mkdir(parents=True, exist_ok=True)
            category_path.write_text(
                _render_category(payload.primary_category, entries), encoding="utf-8", newline="\n"
            )

            state["processed_sources"][source.source_id] = {
                "content_hash": content_hash,
                "source_url": source_url,
                "input_filename": path.name,
                "processed_at": processed_at,
                "primary_category": payload.primary_category,
            }
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            results[path.name] = "processed"
        except Exception as exc:
            LOGGER.error("Inbox item %s failed and was not marked processed: %s", path.name, exc)
            results[path.name] = "failed"
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    args = parser.parse_args()
    results = process_inbox(None, args.inbox, args.wiki_root)
    for filename, status in results.items():
        print(f"{filename}: {status}")
    return 1 if any(status == "failed" for status in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
