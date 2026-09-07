"""Import an Obsidian Vault as a read-only source provider for personal Wikis."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from engine.inbox_wiki import (
    DEFAULT_WIKI_ROOT,
    GeminiInboxClassifier,
    _category_path,
    _load_category_entries,
    _render_category,
    _validated_payload,
)
from engine.schemas import InboxKnowledgePayload, KnowledgeCategory, SourceRecord

LOGGER = logging.getLogger(__name__)
DEFAULT_STATE_PATH = Path(".obsidian_import_state.json")
EXCLUDED_DIRECTORIES = {
    ".agents",
    ".claude",
    ".copilot",
    ".obsidian",
    ".opencode",
    ".trash",
    "archive",
    "attachments",
    "copilot",
    "ai_wiki",
}


@dataclass(frozen=True)
class ImportStats:
    read_markdown: int = 0
    gemini_calls: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0


def _is_reparse_directory(path: Path) -> bool:
    """Return True for symlinks and Windows junction/reparse-point directories."""
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return os.path.islink(path) or bool(attributes & reparse_flag)


def discover_markdown(vault_path: Path) -> Iterator[Path]:
    """Yield Markdown recursively without entering hidden or excluded directories."""
    for root, directories, filenames in os.walk(vault_path):
        root_path = Path(root)
        directories[:] = sorted(
            directory
            for directory in directories
            if not directory.startswith(".")
            and directory.lower() not in EXCLUDED_DIRECTORIES
            and not _is_reparse_directory(root_path / directory)
        )
        for filename in sorted(filenames):
            if filename.lower().endswith(".md"):
                yield root_path / filename


def category_hint(relative_path: Path) -> KnowledgeCategory | None:
    parts = {part.casefold() for part in relative_path.parts[:-1]}
    mappings: tuple[tuple[KnowledgeCategory, set[str]], ...] = (
        ("ai", {"ai", "인공지능", "machine learning", "ml"}),
        ("investment", {"investment", "investing", "투자", "주식", "stocks"}),
        ("philosophy", {"philosophy", "철학"}),
        ("misc", {"misc", "기타"}),
    )
    for category, aliases in mappings:
        if parts & aliases:
            return category
    return None


def _source_from_file(path: Path, vault_path: Path) -> tuple[SourceRecord, str, str]:
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("Obsidian document is empty")
    relative_path = path.relative_to(vault_path).as_posix()
    content_hash = sha256(content.encode("utf-8")).hexdigest()
    source_id = f"obsidian-{sha256(relative_path.casefold().encode('utf-8')).hexdigest()[:20]}"
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", content)
    return (
        SourceRecord(
            source_id=source_id,
            title=heading.group(1).strip() if heading else path.stem,
            content=content,
            url=f"obsidian:///{quote(relative_path)}",
            published_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            source_tier="user-obsidian-vault",
            source_type="obsidian",
        ),
        relative_path,
        content_hash,
    )


def _empty_state() -> dict:
    return {"version": 1, "documents": {}}


def load_import_state(path: Path = DEFAULT_STATE_PATH) -> dict:
    if not path.exists():
        return _empty_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state.get("documents"), dict):
        raise ValueError(f"Invalid Obsidian import state: {path}")
    return state


def import_vault(
    classifier: GeminiInboxClassifier | None = None,
    vault_path: Path | None = None,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    state_path: Path = DEFAULT_STATE_PATH,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ImportStats:
    configured_value = os.environ.get("OBSIDIAN_VAULT_PATH")
    if vault_path is None and not configured_value:
        raise RuntimeError("OBSIDIAN_VAULT_PATH is required")
    configured_path = (vault_path or Path(configured_value or "")).expanduser().resolve()
    if not configured_path.is_dir():
        raise RuntimeError(f"Obsidian Vault directory does not exist: {configured_path}")

    state = load_import_state(state_path)
    shared_classifier = classifier
    read_count = calls = processed = skipped = failed = 0
    for path in discover_markdown(configured_path):
        read_count += 1
        try:
            source, relative_path, content_hash = _source_from_file(path, configured_path)
            previous = state["documents"].get(relative_path)
            if previous and previous.get("content_hash") == content_hash:
                skipped += 1
                continue
            if shared_classifier is None:
                shared_classifier = GeminiInboxClassifier()
            calls += 1
            hint = category_hint(Path(relative_path))
            payload: InboxKnowledgePayload = _validated_payload(
                shared_classifier.classify(source, category_hint=hint),
                None,
            )
            processed_at = now().astimezone(timezone.utc).isoformat()
            entry = {
                "source_id": source.source_id,
                "source_url": None,
                "input_filename": path.name,
                "source_file": str(path),
                "relative_path": relative_path,
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
                _render_category(payload.primary_category, entries),
                encoding="utf-8",
                newline="\n",
            )
            state["documents"][relative_path] = {
                "source_id": source.source_id,
                "source_type": "obsidian",
                "source_file": str(path),
                "relative_path": relative_path,
                "content_hash": content_hash,
                "processed_at": processed_at,
                "primary_category": payload.primary_category,
            }
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            processed += 1
        except Exception as exc:
            failed += 1
            LOGGER.error("Obsidian document %s failed and was not marked processed: %s", path, exc)
    return ImportStats(read_count, calls, processed, skipped, failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args()
    stats = import_vault(wiki_root=args.wiki_root, state_path=args.state)
    print(
        f"read={stats.read_markdown} gemini_calls={stats.gemini_calls} "
        f"processed={stats.processed} skipped={stats.skipped} failed={stats.failed}"
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
