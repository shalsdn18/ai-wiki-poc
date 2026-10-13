"""Backend-independent Markdown document store and local filesystem adapter."""

from __future__ import annotations

import json
import os
import re
import tempfile
from functools import lru_cache
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

DEFAULT_DOCUMENTS_ROOT = Path("documents")
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_FRONTMATTER_END = "\n---\n"


class DocumentNotFoundError(LookupError):
    """Raised when a document ID is not present in the document store."""


class InvalidDocumentIdError(ValueError):
    """Raised when an ID cannot safely identify a document file."""


class DocumentStorageError(RuntimeError):
    """Raised when a stored Markdown document is malformed or unsafe."""


class DocumentStorageUnavailableError(DocumentStorageError):
    """Raised when the configured remote document backend cannot be reached."""


class DocumentStorageConfigurationError(DocumentStorageUnavailableError):
    """Raised when the selected document backend is missing required settings."""


class DocumentStore(Protocol):
    def list_documents(self) -> list[dict[str, Any]]: ...

    def get_document(self, document_id: str) -> dict[str, Any]: ...

    def create_document(
        self, title: str, content: str, tags: list[str] | None = None
    ) -> dict[str, Any]: ...

    def update_document(
        self, document_id: str, title: str, content: str, tags: list[str] | None = None
    ) -> dict[str, Any]: ...

    def delete_document(self, document_id: str) -> None: ...


def _documents_root(root: Path | None = None) -> Path:
    configured = root or Path(os.getenv("DOCUMENTS_ROOT", str(DEFAULT_DOCUMENTS_ROOT)))
    configured.mkdir(parents=True, exist_ok=True)
    return configured.resolve()


def _document_path(document_id: str, root: Path | None = None) -> Path:
    if not _ID_PATTERN.fullmatch(document_id):
        raise InvalidDocumentIdError("Document ID must be a 32-character lowercase UUID hex value")
    base = _documents_root(root)
    path = base / f"{document_id}.md"
    if path.parent.resolve() != base:
        raise InvalidDocumentIdError("Document path is outside the document store")
    if path.is_symlink():
        raise DocumentStorageError("Document path cannot be a symbolic link")
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _serialize(metadata: dict[str, Any], content: str) -> str:
    frontmatter = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
    return f"---\n{frontmatter}\n---\n{content}"


def _parse_markdown(raw: str, document_id: str, relative_path: str) -> dict[str, Any]:
    if not raw.startswith("---\n"):
        raise DocumentStorageError(
            f"Markdown document has no service frontmatter: {relative_path}"
        )
    end = raw.find(_FRONTMATTER_END, 4)
    if end < 0:
        raise DocumentStorageError(f"Markdown document has invalid frontmatter: {relative_path}")
    try:
        metadata = json.loads(raw[4:end])
    except json.JSONDecodeError as exc:
        raise DocumentStorageError(f"Markdown document has invalid metadata: {relative_path}") from exc
    content = raw[end + len(_FRONTMATTER_END) :]
    if metadata.get("id") != document_id:
        raise DocumentStorageError(
            f"Markdown document ID does not match its storage key: {relative_path}"
        )
    return {
        "id": document_id,
        "title": metadata["title"],
        "content": content,
        "tags": metadata.get("tags", []),
        "path": relative_path,
        "content_hash": _content_hash(content),
        "created_at": metadata["created_at"],
        "updated_at": metadata["updated_at"],
    }


def _parse(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DocumentStorageError(f"Could not read Markdown document: {path.name}") from exc
    return _parse_markdown(raw, path.stem, f"documents/{path.name}")


def _write(path: Path, metadata: dict[str, Any], content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(_serialize(metadata, content))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise DocumentStorageError(f"Could not safely write Markdown document: {path.name}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _local_list_documents(root: Path | None = None) -> list[dict[str, Any]]:
    base = _documents_root(root)
    documents = [_parse(path) for path in base.glob("*.md") if not path.is_symlink()]
    return sorted(documents, key=lambda document: (document["updated_at"], document["id"]), reverse=True)


def _local_get_document(document_id: str, root: Path | None = None) -> dict[str, Any]:
    path = _document_path(document_id, root)
    if not path.is_file():
        raise DocumentNotFoundError(document_id)
    return _parse(path)


def _local_create_document(
    title: str,
    content: str,
    tags: list[str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    base = _documents_root(root)
    document_id = uuid4().hex
    path = _document_path(document_id, base)
    now = _utc_now()
    metadata = {
        "id": document_id,
        "title": title,
        "tags": tags or [],
        "content_hash": _content_hash(content),
        "created_at": now,
        "updated_at": now,
    }
    _write(path, metadata, content)
    return _parse(path)


def _local_update_document(
    document_id: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    current = _local_get_document(document_id, root)
    path = _document_path(document_id, root)
    metadata = {
        "id": document_id,
        "title": title,
        "tags": tags or [],
        "content_hash": _content_hash(content),
        "created_at": current["created_at"],
        "updated_at": _utc_now(),
    }
    _write(path, metadata, content)
    return _parse(path)


def _local_delete_document(document_id: str, root: Path | None = None) -> None:
    path = _document_path(document_id, root)
    if not path.is_file():
        raise DocumentNotFoundError(document_id)
    path.unlink()


class LocalFileStore:
    """Filesystem-backed Markdown store with atomic writes and path checks."""

    def __init__(self, root: Path | None = None):
        self.root = root

    def list_documents(self) -> list[dict[str, Any]]:
        return _local_list_documents(self.root)

    def get_document(self, document_id: str) -> dict[str, Any]:
        return _local_get_document(document_id, self.root)

    def create_document(
        self, title: str, content: str, tags: list[str] | None = None
    ) -> dict[str, Any]:
        return _local_create_document(title, content, tags, self.root)

    def update_document(
        self, document_id: str, title: str, content: str, tags: list[str] | None = None
    ) -> dict[str, Any]:
        return _local_update_document(document_id, title, content, tags, self.root)

    def delete_document(self, document_id: str) -> None:
        _local_delete_document(document_id, self.root)


@lru_cache(maxsize=1)
def _configured_store() -> DocumentStore:
    backend = os.getenv("DOCUMENT_STORAGE_BACKEND", "local").strip().casefold()
    if backend == "local":
        return LocalFileStore()
    if backend == "r2":
        from engine.r2_document_store import R2Store

        return R2Store.from_environment()
    raise DocumentStorageConfigurationError(
        "DOCUMENT_STORAGE_BACKEND must be either 'local' or 'r2'"
    )


def get_store(root: Path | None = None) -> DocumentStore:
    """Return the configured backend; an explicit root always selects local storage."""
    if root is not None:
        return LocalFileStore(root)
    return _configured_store()


def list_documents(root: Path | None = None) -> list[dict[str, Any]]:
    return get_store(root).list_documents()


def get_document(document_id: str, root: Path | None = None) -> dict[str, Any]:
    return get_store(root).get_document(document_id)


def create_document(
    title: str,
    content: str,
    tags: list[str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    return get_store(root).create_document(title, content, tags)


def update_document(
    document_id: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    return get_store(root).update_document(document_id, title, content, tags)


def delete_document(document_id: str, root: Path | None = None) -> None:
    get_store(root).delete_document(document_id)
