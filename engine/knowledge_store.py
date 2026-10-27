"""Filesystem-backed reader for the private GitHub-first knowledge repository."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

CATEGORIES = ("ai", "investment", "philosophy", "misc")
VISIBILITIES = ("private", "public")
REQUIRED_FIELDS = ("title", "category", "tags", "created", "updated", "visibility")
DEFAULT_KNOWLEDGE_ROOT = Path("knowledge")
_FRONTMATTER = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)(.*)\Z", re.DOTALL)


class KnowledgeError(ValueError):
    """Base class for invalid knowledge documents."""


class KnowledgeDocumentError(KnowledgeError):
    """A knowledge document is malformed or has invalid metadata."""


class KnowledgeNotFoundError(LookupError):
    """No document exists for the requested stable ID."""


@dataclass(frozen=True)
class KnowledgeDocument:
    id: str
    title: str
    category: str
    tags: list[str]
    created: str
    updated: str
    visibility: str
    source_url: str | None
    content: str
    relative_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "tags": self.tags,
            "created": self.created,
            "updated": self.updated,
            "visibility": self.visibility,
            "source_url": self.source_url,
            "content": self.content,
            "relative_path": self.relative_path,
        }


class KnowledgeStore(Protocol):
    def list_documents(self, visibility: str | None = None) -> list[KnowledgeDocument]: ...

    def get_document(self, document_id: str) -> KnowledgeDocument: ...

    def search(self, query: str, limit: int = 50) -> list[KnowledgeDocument]: ...

    def stats(self, visibility: str | None = None) -> dict[str, Any]: ...


def _date_value(value: Any, field: str, relative_path: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError as exc:
            raise KnowledgeDocumentError(f"Invalid {field} date in {relative_path}") from exc
    raise KnowledgeDocumentError(f"Missing or invalid {field} in {relative_path}")


def _parse_document(path: Path, root: Path) -> KnowledgeDocument:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise KnowledgeDocumentError(f"Could not read knowledge document: {path.name}") from exc
    relative_path = path.relative_to(root).as_posix()
    match = _FRONTMATTER.match(raw)
    if not match:
        raise KnowledgeDocumentError(f"Invalid YAML frontmatter in {relative_path}")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise KnowledgeDocumentError(f"Invalid YAML frontmatter in {relative_path}") from exc
    if not isinstance(metadata, dict):
        raise KnowledgeDocumentError(f"Frontmatter must be a mapping in {relative_path}")
    missing = [field for field in REQUIRED_FIELDS if field not in metadata]
    if missing:
        raise KnowledgeDocumentError(
            f"Missing required metadata in {relative_path}: {', '.join(missing)}"
        )
    title = metadata["title"]
    category = metadata["category"]
    visibility = metadata["visibility"]
    tags = metadata["tags"]
    if not isinstance(title, str) or not title.strip():
        raise KnowledgeDocumentError(f"Invalid title in {relative_path}")
    if category not in CATEGORIES:
        raise KnowledgeDocumentError(f"Invalid category in {relative_path}")
    if path.relative_to(root).parts[0] != category:
        raise KnowledgeDocumentError(f"Path category does not match metadata in {relative_path}")
    if visibility not in VISIBILITIES:
        raise KnowledgeDocumentError(f"Invalid visibility in {relative_path}")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise KnowledgeDocumentError(f"Invalid tags in {relative_path}")
    source_url = metadata.get("source_url")
    if source_url is not None and not isinstance(source_url, str):
        raise KnowledgeDocumentError(f"Invalid source_url in {relative_path}")
    document_id = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
    return KnowledgeDocument(
        id=document_id,
        title=title.strip(),
        category=category,
        tags=tags,
        created=_date_value(metadata["created"], "created", relative_path),
        updated=_date_value(metadata["updated"], "updated", relative_path),
        visibility=visibility,
        source_url=source_url,
        content=match.group(2).strip(),
        relative_path=relative_path,
    )


class FilesystemKnowledgeStore:
    """Read Markdown documents from a local clone's knowledge/ directory."""

    def __init__(self, root: Path | str | None = None):
        configured = (
            root if root is not None else os.getenv("KNOWLEDGE_ROOT", str(DEFAULT_KNOWLEDGE_ROOT))
        )
        self.root = Path(configured).expanduser().resolve()

    def list_documents(self, visibility: str | None = None) -> list[KnowledgeDocument]:
        if visibility is not None and visibility not in VISIBILITIES:
            raise KnowledgeError("Invalid visibility filter")
        documents = []
        if self.root.exists():
            for path in sorted(self.root.rglob("*.md")):
                if path.is_symlink() or not path.is_file():
                    continue
                document = _parse_document(path, self.root)
                if visibility is None or document.visibility == visibility:
                    documents.append(document)
        return sorted(documents, key=lambda item: (item.updated, item.id), reverse=True)

    def get_document(self, document_id: str) -> KnowledgeDocument:
        for document in self.list_documents():
            if document.id == document_id:
                return document
        raise KnowledgeNotFoundError(document_id)

    def search(self, query: str, limit: int = 50) -> list[KnowledgeDocument]:
        needle = query.strip().casefold()
        if not needle:
            raise KnowledgeError("Search query must not be empty")
        matches = []
        for document in self.list_documents():
            searchable = " ".join(
                (document.title, document.category, " ".join(document.tags), document.content)
            )
            if needle in searchable.casefold():
                matches.append(document)
        return matches[:limit]

    def stats(self, visibility: str | None = None) -> dict[str, Any]:
        counts = {category: 0 for category in CATEGORIES}
        documents = self.list_documents(visibility)
        for document in documents:
            counts[document.category] += 1
        return {"documents": len(documents), "categories": counts}


def get_store(root: Path | str | None = None) -> KnowledgeStore:
    """Construct the configured adapter; callers depend only on KnowledgeStore."""
    return FilesystemKnowledgeStore(root)
