"""SQLite-backed semantic index using an Ollama embedding endpoint."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from engine.obsidian_import import discover_markdown, load_import_state

DEFAULT_VECTOR_DB = Path("vectors.db")
DEFAULT_EMBEDDING_MODEL = "bge-m3"


@dataclass(frozen=True)
class SemanticResult:
    title: str
    score: float
    snippet: str
    id: str


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("""CREATE TABLE IF NOT EXISTS document_vectors (
            id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            title TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            content TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            model TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    return connection


def embed(text: str, model: str | None = None) -> list[float]:
    """Create an embedding through the local Ollama HTTP API."""
    resolved_model = model or os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    request = Request(
        f"{base_url}/api/embeddings",
        data=json.dumps({"model": resolved_model, "prompt": text}, ensure_ascii=False).encode(
            "utf-8"
        ),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [float(value) for value in payload["embedding"]]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0


def sync_vault_vectors(
    vault_path: Path,
    state_path: Path,
    db_path: Path = DEFAULT_VECTOR_DB,
    model: str | None = None,
) -> int:
    """Index changed state documents and remove vectors no longer in the vault."""
    resolved_model = model or os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    state = load_import_state(state_path)
    state_by_path = state["documents"]
    paths = {
        path.relative_to(vault_path).as_posix(): path for path in discover_markdown(vault_path)
    }
    indexed = 0
    with _connect(db_path) as connection:
        existing = {
            row[0]: (row[1], row[2])
            for row in connection.execute("SELECT id, content_hash, model FROM document_vectors")
        }
        for relative_path, path in paths.items():
            document = state_by_path.get(relative_path)
            if not document:
                continue
            content_hash = document["content_hash"]
            source_id = document["source_id"]
            if existing.get(source_id) == (content_hash, resolved_model):
                continue
            content = path.read_text(encoding="utf-8-sig")
            vector = embed(content, resolved_model)
            connection.execute(
                """INSERT INTO document_vectors
                (id, content_hash, title, relative_path, content, vector_json, model, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET content_hash=excluded.content_hash,
                    title=excluded.title, relative_path=excluded.relative_path, content=excluded.content,
                    vector_json=excluded.vector_json, model=excluded.model, updated_at=excluded.updated_at""",
                (
                    source_id,
                    content_hash,
                    document.get("title", path.stem),
                    relative_path,
                    content,
                    json.dumps(vector),
                    resolved_model,
                ),
            )
            indexed += 1
        active_ids = {
            document["source_id"]
            for document in state_by_path.values()
            if document.get("relative_path") in paths
        }
        connection.executemany(
            "DELETE FROM document_vectors WHERE id = ?",
            ((source_id,) for source_id in set(existing) - active_ids),
        )
    return indexed


def update_vector(path: Path, db_path: Path = DEFAULT_VECTOR_DB, model: str | None = None) -> int:
    """Synchronize the vault after one import, including deleted documents."""
    configured = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not configured:
        raise RuntimeError("OBSIDIAN_VAULT_PATH is required for vector synchronization")
    vault_path = Path(configured).expanduser().resolve()
    state_path = Path(os.getenv("OBSIDIAN_IMPORT_STATE_PATH", ".obsidian_import_state.json"))
    return sync_vault_vectors(vault_path, state_path, db_path, model)


def semantic_search(
    query: str, db_path: Path = DEFAULT_VECTOR_DB, model: str | None = None, limit: int = 20
) -> list[SemanticResult]:
    query_vector = embed(query, model)
    resolved_model = model or os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT id, title, content, vector_json FROM document_vectors WHERE model = ?",
            (resolved_model,),
        ).fetchall()
    ranked = sorted(
        (
            SemanticResult(row[1], _cosine(query_vector, json.loads(row[3])), row[2][:240], row[0])
            for row in rows
        ),
        key=lambda result: result.score,
        reverse=True,
    )
    return ranked[:limit]


def get_documents(ids: list[str], db_path: Path = DEFAULT_VECTOR_DB) -> dict[str, str]:
    """Read indexed document content for a set of semantic result IDs."""
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with _connect(db_path) as connection:
        rows = connection.execute(
            f"SELECT id, content FROM document_vectors WHERE id IN ({placeholders})", ids
        ).fetchall()
    return {row[0]: row[1] for row in rows}
