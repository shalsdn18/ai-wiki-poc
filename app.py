"""FastAPI read/API layer for the existing Obsidian importer outputs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import engine.config
from engine.inbox_wiki import CATEGORIES
from engine.obsidian_import import discover_markdown
from engine.pipeline import PipelineStats, run_pipeline
from engine.vector_store import DEFAULT_VECTOR_DB, SemanticResult, get_documents, semantic_search

app = FastAPI(title="AI Wiki API", version="1.0.0")

_default_cors_origins = {
    "https://ai-wiki-dashboard-minwoo.cheatmin.chatgpt.site",
    "http://localhost:5173",
}
_configured_cors_origins = {
    origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if origin.strip()
}
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(_default_cors_origins | _configured_cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().casefold() in {"1", "true", "yes", "on"}


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "capabilities": {
            "wiki_read": True,
            "local_import": _enabled("ENABLE_LOCAL_IMPORT"),
            "semantic_search": _enabled("ENABLE_LOCAL_RAG"),
            "rag_chat": _enabled("ENABLE_LOCAL_RAG"),
        },
    }


class StatsResponse(BaseModel):
    documents: int
    categories: dict[str, int]


class NoteResponse(BaseModel):
    source_id: str
    title: str
    summary: str
    relative_path: str
    primary_category: str
    processed_at: str
    key_facts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    topic: str = ""


class ImportResponse(BaseModel):
    read_markdown: int
    gemini_calls: int
    processed: int
    skipped: int
    failed: int


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)


class ChatSource(BaseModel):
    id: str
    title: str
    score: float


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]


class SemanticSearchResponse(BaseModel):
    title: str
    score: float
    snippet: str
    id: str


def _state_path() -> Path:
    return Path(os.getenv("OBSIDIAN_IMPORT_STATE_PATH", ".obsidian_import_state.json"))


def _wiki_root() -> Path:
    return Path(os.getenv("WIKI_ROOT", "wiki"))


def _vault_path() -> Path:
    return Path(os.getenv("OBSIDIAN_VAULT_PATH", ""))


def _vector_db_path() -> Path:
    return Path(os.getenv("VECTOR_DB_PATH", "vectors.db"))


def _category_entries() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    root = _wiki_root()
    for category in CATEGORIES:
        path = root / category / "index.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---\n", 4)
        if end < 0:
            continue
        try:
            entries = json.loads(text[4:end]).get("entries", [])
            result[category] = entries if isinstance(entries, list) else []
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return result


def _entries() -> list[dict[str, Any]]:
    return [entry for entries in _category_entries().values() for entry in entries]


def _note(entry: dict[str, Any]) -> NoteResponse:
    return NoteResponse(
        source_id=entry["source_id"],
        title=entry["title"],
        summary=entry["summary"],
        relative_path=entry["relative_path"],
        primary_category=entry["primary_category"],
        processed_at=entry["processed_at"],
        key_facts=entry.get("key_facts", []),
        tags=entry.get("tags", []),
        topic=entry.get("topic", ""),
    )


@app.get("/stats", response_model=StatsResponse)
def stats() -> StatsResponse:
    category_entries = _category_entries()
    counts = {category: 0 for category in CATEGORIES}
    for category, entries in category_entries.items():
        counts[category] = len(entries)
    return StatsResponse(documents=sum(counts.values()), categories=counts)


@app.get("/recent", response_model=list[NoteResponse])
def recent(limit: int = Query(default=10, ge=1, le=100)) -> list[NoteResponse]:
    entries = sorted(_entries(), key=lambda item: item.get("processed_at", ""), reverse=True)
    return [_note(entry) for entry in entries[:limit]]


@app.get("/search", response_model=list[NoteResponse])
def search(
    q: str = Query(min_length=1), limit: int = Query(default=50, ge=1, le=100)
) -> list[NoteResponse]:
    query = q.casefold()
    matches = []
    for entry in _entries():
        searchable = " ".join(
            str(entry.get(field, ""))
            for field in ("title", "summary", "relative_path", "topic", "tags", "key_facts")
        )
        if query in searchable.casefold():
            matches.append(_note(entry))
    return matches[:limit]


@app.get("/note/{note_id}", response_model=NoteResponse)
def note(note_id: str) -> NoteResponse:
    for entry in _entries():
        if entry.get("source_id") == note_id:
            return _note(entry)
    raise HTTPException(status_code=404, detail="Note not found")


@app.get("/semantic-search", response_model=list[SemanticSearchResponse])
def semantic(
    q: str = Query(min_length=1), limit: int = Query(default=20, ge=1, le=100)
) -> list[SemanticSearchResponse]:
    if not _enabled("ENABLE_LOCAL_RAG"):
        raise HTTPException(
            status_code=503,
            detail="Semantic search is disabled. Set ENABLE_LOCAL_RAG=true for local RAG.",
        )
    try:
        return [
            SemanticSearchResponse(**result.__dict__)
            for result in semantic_search(q, _vector_db_path(), limit=limit)
        ]
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc


@app.post("/import", response_model=ImportResponse)
def run_import() -> ImportResponse:
    if not _enabled("ENABLE_LOCAL_IMPORT"):
        raise HTTPException(
            status_code=503,
            detail="Local import is disabled. Set ENABLE_LOCAL_IMPORT=true to enable it.",
        )
    vault = _vault_path()
    totals = {
        field: 0 for field in ("read_markdown", "gemini_calls", "processed", "skipped", "failed")
    }
    for path in discover_markdown(vault):
        try:
            result: PipelineStats = run_pipeline(path)
            for field in totals:
                totals[field] += getattr(result, field)
        except Exception:
            totals["failed"] += 1
    return ImportResponse(**totals)


def _generate_chat_answer(question: str, context: str) -> str:
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for chat")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        contents=(
            "Answer the question using only the supplied context. If the context "
            "does not contain the answer, say that clearly. Be concise and cite "
            "the relevant document titles in the answer.\n\n"
            f"QUESTION:\n{question}\n\nCONTEXT:\n{context}"
        ),
        config=types.GenerateContentConfig(temperature=0.2),
    )
    return response.text.strip()


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    if not _enabled("ENABLE_LOCAL_RAG"):
        raise HTTPException(
            status_code=503,
            detail="RAG chat is disabled. Set ENABLE_LOCAL_RAG=true for local RAG.",
        )
    vector_db = _vector_db_path()
    matches = semantic_search(request.question, vector_db, limit=5)
    documents = get_documents([match.id for match in matches], vector_db)
    context = "\n\n".join(
        f"[{match.id}] {match.title}\n{documents.get(match.id, match.snippet)}" for match in matches
    )
    try:
        answer = _generate_chat_answer(request.question, context)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ChatResponse(
        answer=answer,
        sources=[
            ChatSource(id=match.id, title=match.title, score=match.score) for match in matches
        ],
    )
