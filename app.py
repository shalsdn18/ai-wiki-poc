"""FastAPI read/API layer for the existing Obsidian importer outputs."""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import engine.config
from engine import document_store
from engine.inbox_wiki import CATEGORIES
from engine.obsidian_import import discover_markdown
from engine.pipeline import PipelineStats, run_pipeline
from engine.vector_store import DEFAULT_VECTOR_DB, SemanticResult, get_documents, semantic_search

app = FastAPI(title="AI Wiki API", version="1.0.0")

_production = os.getenv("APP_ENV", "development").strip().casefold() == "production"
_session_cookie_name = "ai_wiki_session"
_session_ttl_seconds = 60 * 60 * 24 * 7

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
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().casefold() in {"1", "true", "yes", "on"}


def _auth_configured() -> bool:
    return bool(_password_hash() and len(_session_secret()) >= 32)


def _password_hash() -> str:
    """Read auth configuration when used so environment loaded by the runner is current."""
    return os.getenv("APP_PASSWORD_HASH", "").strip()


def _session_secret() -> bytes:
    return os.getenv("SESSION_SECRET", "").encode("utf-8")


def _verify_password(password: str) -> bool:
    """Verify a PBKDF2-SHA256 value encoded as pbkdf2_sha256$rounds$salt$digest."""
    try:
        algorithm, rounds_text, salt, expected = _password_hash().split("$", 3)
        rounds = int(rounds_text)
        if algorithm != "pbkdf2_sha256" or rounds < 100_000 or not salt or not expected:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(actual).decode("ascii"), expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _session_signature(payload: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(_session_secret(), payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii")


def _new_session() -> str:
    expires_at = int(time.time()) + _session_ttl_seconds
    payload = f"{expires_at}.{secrets.token_urlsafe(24)}"
    return f"{payload}.{_session_signature(payload)}"


def _valid_session(value: str | None) -> bool:
    if not value or not _session_secret():
        return False
    try:
        expires_text, nonce, signature = value.split(".", 2)
        expires_at = int(expires_text)
        payload = f"{expires_text}.{nonce}"
        return (
            expires_at >= int(time.time())
            and hmac.compare_digest(_session_signature(payload), signature)
        )
    except (ValueError, TypeError):
        return False


def _request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if referer:
        from urllib.parse import urlsplit

        parsed = urlsplit(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return None


def _require_same_origin(request: Request) -> None:
    if request.headers.get("x-csrf-protection") != "1":
        raise HTTPException(status_code=403, detail="CSRF protection header is required")
    origin = _request_origin(request)
    allowed = set(_default_cors_origins | _configured_cors_origins)
    # The request's own origin supports a same-origin frontend deployment.
    expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")
    if origin and (origin in allowed or origin == expected_origin):
        return
    raise HTTPException(status_code=403, detail="Request origin is not allowed")


def _require_document_session(request: Request) -> None:
    if not _auth_configured():
        raise HTTPException(status_code=503, detail="Document authentication is not configured")
    if not _valid_session(request.cookies.get(_session_cookie_name)):
        raise HTTPException(status_code=401, detail="Authentication required")


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


@app.post("/login")
def login(request: Request, credentials: LoginRequest, response: Response) -> dict[str, bool]:
    if not _auth_configured():
        raise HTTPException(status_code=503, detail="Document authentication is not configured")
    _require_same_origin(request)
    if not _verify_password(credentials.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    response.set_cookie(
        key=_session_cookie_name,
        value=_new_session(),
        max_age=_session_ttl_seconds,
        httponly=True,
        secure=_production,
        samesite="none" if _production else "lax",
        path="/",
    )
    return {"authenticated": True}


@app.post("/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    _require_same_origin(request)
    response.delete_cookie(
        key=_session_cookie_name,
        path="/",
        httponly=True,
        secure=_production,
        samesite="none" if _production else "lax",
    )
    return {"authenticated": False}


@app.get("/auth/status")
def auth_status(request: Request) -> dict[str, bool]:
    if not _auth_configured():
        return {"authenticated": False, "configured": False}
    return {
        "authenticated": _valid_session(request.cookies.get(_session_cookie_name)),
        "configured": True,
    }


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


class DocumentWriteRequest(BaseModel):
    title: str = Field(min_length=1)
    content: str
    tags: list[str] = Field(default_factory=list)


class DocumentResponse(BaseModel):
    id: str
    title: str
    content: str
    tags: list[str]
    path: str
    content_hash: str
    created_at: str
    updated_at: str


def _document_or_404(operation, *args):
    try:
        return operation(*args)
    except (document_store.DocumentNotFoundError, document_store.InvalidDocumentIdError) as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except document_store.DocumentStorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Document storage is temporarily unavailable") from exc


def _document_storage_call(operation, *args):
    try:
        return operation(*args)
    except document_store.DocumentStorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Document storage is temporarily unavailable") from exc


@app.get("/documents", response_model=list[DocumentResponse])
def documents(request: Request) -> list[DocumentResponse]:
    _require_document_session(request)
    items = _document_storage_call(document_store.list_documents)
    return [DocumentResponse(**item) for item in items]


@app.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(document_id: str, request: Request) -> DocumentResponse:
    _require_document_session(request)
    item = _document_or_404(document_store.get_document, document_id)
    return DocumentResponse(**item)


@app.post(
    "/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_document(payload: DocumentWriteRequest, request: Request) -> DocumentResponse:
    _require_same_origin(request)
    _require_document_session(request)
    item = _document_storage_call(
        document_store.create_document, payload.title, payload.content, payload.tags
    )
    return DocumentResponse(**item)


@app.put("/documents/{document_id}", response_model=DocumentResponse)
def update_document(
    document_id: str, payload: DocumentWriteRequest, request: Request
) -> DocumentResponse:
    _require_same_origin(request)
    _require_document_session(request)
    item = _document_or_404(
        document_store.update_document,
        document_id,
        payload.title,
        payload.content,
        payload.tags,
    )
    return DocumentResponse(**item)


@app.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str, request: Request) -> Response:
    _require_same_origin(request)
    _require_document_session(request)
    _document_or_404(document_store.delete_document, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
