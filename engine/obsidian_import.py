"""Import an Obsidian Vault as a read-only source provider for personal Wikis."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

import engine.config
from engine.cache import DEFAULT_CACHE_PATH, get_cached_payload, save_cached_payload
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
DEFAULT_DELAY_SECONDS = 5.0
DEFAULT_MAX_RETRIES = 3
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
EXCLUDED_MARKDOWN_NAMES = ("readme.md",)


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
            lowered = filename.casefold()
            if (
                lowered.endswith(".md")
                and lowered not in EXCLUDED_MARKDOWN_NAMES
                and not lowered.startswith("moc_")
                and not lowered.startswith("template")
            ):
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
    # utf-8-sig accepts regular UTF-8 and strips an optional UTF-8 BOM.
    content = path.read_text(encoding="utf-8-sig")
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
        raise TypeError(f"Invalid Obsidian import state: {path}")
    return state


def _setting_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid delay value: {value}") from exc
    if parsed < 0:
        raise ValueError(f"Delay must be non-negative: {value}")
    return parsed


def _setting_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid retry count: {value}") from exc
    if parsed < 0:
        raise ValueError(f"Retry count must be non-negative: {value}")
    return parsed


def _error_status(exc: Exception) -> int | None:
    for attribute in ("code", "status_code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    match = re.search(r"\b(429|503)\b", str(exc))
    return int(match.group(1)) if match else None


def _retry_delay_from_error(exc: Exception) -> float:
    message = str(exc)
    patterns = (
        r"retryDelay[^0-9]*(\d+(?:\.\d+)?)\s*s?",
        r"retryDelay[^0-9]*seconds[^0-9]*(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return 60.0


def _classify_with_retry(
    classifier: GeminiInboxClassifier | None,
    source: SourceRecord,
    category_hint: KnowledgeCategory | None,
    max_retries: int,
) -> InboxKnowledgePayload:
    for attempt in range(max_retries + 1):
        try:
            return _validated_payload(
                classifier.classify(source, category_hint=category_hint), None
            )
        except Exception as exc:
            status = _error_status(exc)
            if status not in {429, 503} or attempt >= max_retries:
                raise
            delay = _retry_delay_from_error(exc) if status == 429 else 30.0 * (2**attempt)
            LOGGER.warning(
                "Gemini request returned %s; retrying in %.0f second(s) (attempt %s/%s)",
                status,
                delay,
                attempt + 1,
                max_retries,
            )
            time.sleep(delay)
    raise AssertionError("retry loop exited unexpectedly")


def process_markdown_file(
    path: Path,
    vault_path: Path,
    state: dict,
    classifier: GeminiInboxClassifier,
    wiki_root: Path,
    state_path: Path,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    delay_seconds: float = 0.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    delay_before: bool = False,
    cache_path: Path = DEFAULT_CACHE_PATH,
    force_reprocess: bool = False,
) -> ImportStats:
    """Process one Markdown file and persist the updated import state."""
    source, relative_path, content_hash = _source_from_file(path, vault_path)
    previous = state["documents"].get(relative_path)
    if previous and previous.get("content_hash") == content_hash and not force_reprocess:
        return ImportStats(read_markdown=1, skipped=1)
    model = getattr(classifier, "_model", None) or os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
    payload = get_cached_payload(content_hash, model, cache_path)
    if payload is not None:
        try:
            payload = _validated_payload(payload, None)
        except Exception:
            LOGGER.warning("Ignoring invalid cached payload for %s", relative_path)
            payload = None
    gemini_calls = 0
    if payload is None:
        classifier = classifier or GeminiInboxClassifier()
        if delay_before:
            time.sleep(delay_seconds)
        hint = category_hint(Path(relative_path))
        payload = _classify_with_retry(classifier, source, hint, max_retries)
        save_cached_payload(content_hash, model, payload, cache_path)
        gemini_calls = 1
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
    return ImportStats(read_markdown=1, gemini_calls=gemini_calls, processed=1)


def _resolve_vault_path(vault_path: Path | None) -> Path:
    configured_value = os.environ.get("OBSIDIAN_VAULT_PATH")
    if vault_path is None and not configured_value:
        raise RuntimeError("OBSIDIAN_VAULT_PATH is required")
    configured_path = (vault_path or Path(configured_value or "")).expanduser().resolve()
    if not configured_path.is_dir():
        raise RuntimeError(f"Obsidian Vault directory does not exist: {configured_path}")
    return configured_path


def import_file(
    path: Path,
    classifier: GeminiInboxClassifier | None = None,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    state_path: Path = DEFAULT_STATE_PATH,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    delay_seconds: float | None = None,
    max_retries: int | None = None,
    cache_path: Path | None = None,
) -> ImportStats:
    """Import exactly one Markdown file using the incremental state."""
    configured_path = _resolve_vault_path(None)
    target = path.expanduser().resolve()
    if target.parent == configured_path or configured_path in target.parents:
        pass
    else:
        raise ValueError(f"File is outside the Obsidian Vault: {path}")
    if target.suffix.casefold() != ".md":
        raise ValueError(f"Not a Markdown file: {path}")
    state = load_import_state(state_path)
    resolved_delay = _setting_float(
        (
            os.environ.get("OBSIDIAN_IMPORT_DELAY_SECONDS")
            if delay_seconds is None
            else str(delay_seconds)
        ),
        DEFAULT_DELAY_SECONDS,
    )
    resolved_retries = _setting_int(
        os.environ.get("OBSIDIAN_IMPORT_MAX_RETRIES") if max_retries is None else str(max_retries),
        DEFAULT_MAX_RETRIES,
    )
    resolved_cache_path = cache_path or state_path.parent / DEFAULT_CACHE_PATH.name
    return process_markdown_file(
        target,
        configured_path,
        state,
        classifier,
        wiki_root,
        state_path,
        now,
        resolved_delay,
        resolved_retries,
        cache_path=resolved_cache_path,
    )


def import_vault(
    classifier: GeminiInboxClassifier | None = None,
    vault_path: Path | None = None,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    state_path: Path = DEFAULT_STATE_PATH,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    delay_seconds: float | None = None,
    max_retries: int | None = None,
    cache_path: Path | None = None,
    rebuild_invalid: bool = False,
) -> ImportStats:
    configured_path = _resolve_vault_path(vault_path)

    state = load_import_state(state_path)
    delay_seconds = _setting_float(
        (
            os.environ.get("OBSIDIAN_IMPORT_DELAY_SECONDS")
            if delay_seconds is None
            else str(delay_seconds)
        ),
        DEFAULT_DELAY_SECONDS,
    )
    max_retries = _setting_int(
        os.environ.get("OBSIDIAN_IMPORT_MAX_RETRIES") if max_retries is None else str(max_retries),
        DEFAULT_MAX_RETRIES,
    )
    resolved_cache_path = cache_path or state_path.parent / DEFAULT_CACHE_PATH.name
    invalid_source_ids = _invalid_source_ids(wiki_root) if rebuild_invalid else set()
    shared_classifier = classifier
    read_count = calls = processed = skipped = failed = 0
    for path in discover_markdown(configured_path):
        try:
            if shared_classifier is None:
                shared_classifier = GeminiInboxClassifier()
            result = process_markdown_file(
                path,
                configured_path,
                state,
                shared_classifier,
                wiki_root,
                state_path,
                now,
                delay_seconds,
                max_retries,
                delay_before=calls > 0,
                cache_path=resolved_cache_path,
                force_reprocess=source_id_for_path(path, configured_path) in invalid_source_ids,
            )
            read_count += result.read_markdown
            calls += result.gemini_calls
            processed += result.processed
            skipped += result.skipped
        except Exception as exc:
            failed += 1
            LOGGER.error("Obsidian document %s failed and was not marked processed: %s", path, exc)
    return ImportStats(read_count, calls, processed, skipped, failed)


def source_id_for_path(path: Path, vault_path: Path) -> str:
    relative_path = path.relative_to(vault_path).as_posix()
    return f"obsidian-{sha256(relative_path.casefold().encode('utf-8')).hexdigest()[:20]}"


def _invalid_source_ids(wiki_root: Path) -> set[str]:
    invalid: set[str] = set()
    for category in ("ai", "investment", "philosophy", "misc"):
        path = _category_path(wiki_root, category)  # type: ignore[arg-type]
        try:
            entries = _load_category_entries(path)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not inspect Wiki index %s: %s", path, exc)
            continue
        for entry in entries:
            try:
                if entry.get("primary_category") != category:
                    raise ValueError("Wiki index category does not match primary_category")
                _validated_payload(entry, entry.get("source_url"))
            except Exception as exc:
                source_id = entry.get("source_id")
                if source_id:
                    invalid.add(source_id)
                    LOGGER.warning("Invalid Wiki entry %s will be rebuilt: %s", source_id, exc)
    return invalid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--delay-seconds", type=float, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument(
        "--rebuild-invalid",
        action="store_true",
        help="Reclassify only source documents whose current Wiki payload is invalid",
    )
    args = parser.parse_args()
    stats = import_vault(
        wiki_root=args.wiki_root,
        state_path=args.state,
        delay_seconds=args.delay_seconds,
        max_retries=args.max_retries,
        rebuild_invalid=args.rebuild_invalid,
    )
    print(
        f"read={stats.read_markdown} gemini_calls={stats.gemini_calls} "
        f"processed={stats.processed} skipped={stats.skipped} failed={stats.failed}"
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
