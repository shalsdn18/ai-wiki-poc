"""SQLite cache for validated LLM payloads."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from engine.schemas import InboxKnowledgePayload

DEFAULT_CACHE_PATH = Path("cache.db")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("""CREATE TABLE IF NOT EXISTS llm_cache (
            content_hash TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    return connection


def get_cached_payload(
    content_hash: str,
    model: str,
    db_path: Path = DEFAULT_CACHE_PATH,
) -> InboxKnowledgePayload | None:
    """Return a valid cached payload, ignoring corrupt entries."""
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM llm_cache WHERE content_hash = ? AND model = ?",
            (content_hash, model),
        ).fetchone()
    if row is None:
        return None
    try:
        return InboxKnowledgePayload.model_validate_json(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def save_cached_payload(
    content_hash: str,
    model: str,
    payload: InboxKnowledgePayload,
    db_path: Path = DEFAULT_CACHE_PATH,
) -> None:
    """Store a payload, replacing an existing entry for the hash."""
    with _connect(db_path) as connection:
        connection.execute(
            """INSERT INTO llm_cache(content_hash, model, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                model = excluded.model,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at""",
            (
                content_hash,
                model,
                payload.model_dump_json(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
