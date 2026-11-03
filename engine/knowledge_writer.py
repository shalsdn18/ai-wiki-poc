"""Orchestrate canonical Knowledge revisions across PostgreSQL and R2."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import yaml

from engine.knowledge_store import (
    CATEGORIES,
    VISIBILITIES,
    KnowledgeDocument,
    KnowledgeDocumentError,
    KnowledgeNotFoundError,
    parse_markdown_document,
    search_documents,
)
from engine.postgres_operational import (
    DOCUMENT_ID_RE,
    InvalidOperationalInput,
    PostgresOperationalStore,
    RevisionConflict,
    VerifiedRevision,
    WriteIntent,
    WriteRequest,
    normalize_tag,
    object_keys,
)
from engine.r2_revision_objects import (
    R2RevisionObjects,
    RevisionObjectConflict,
    RevisionS3Client,
)

_REVISION_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_MAX_SOURCE_BYTES = 5 * 1024 * 1024
_MIME_TYPE = "text/markdown; charset=utf-8"
_PIPELINE_FINGERPRINT = hashlib.sha256(b"ai-wiki-knowledge-writer-v1").hexdigest()


@dataclass(frozen=True)
class KnowledgeWriteInput:
    title: str
    category: str
    tags: list[str]
    visibility: str
    content: str
    source_url: str | None = None
    operation_date: str | None = None


@dataclass(frozen=True)
class KnowledgeWriteResult:
    document_id: str
    revision_id: str | None
    current_revision_id: str
    operation_result: str
    processing_status: str | None = None
    processing_job_id: str | None = None


def _validate_source_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise InvalidOperationalInput("Invalid source URL")
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise InvalidOperationalInput("Invalid source URL")
    return normalized


def _validate_input(value: KnowledgeWriteInput) -> dict[str, Any]:
    if not isinstance(value.title, str) or not value.title.strip() or len(value.title) > 500:
        raise InvalidOperationalInput("Invalid title")
    if value.category not in CATEGORIES:
        raise InvalidOperationalInput("Invalid category")
    if value.visibility not in VISIBILITIES:
        raise InvalidOperationalInput("Invalid visibility")
    if not isinstance(value.tags, list) or len(value.tags) > 50:
        raise InvalidOperationalInput("Invalid tags")
    tags = [tag.strip() for tag in value.tags if isinstance(tag, str)]
    if len(tags) != len(value.tags) or any(not tag or len(tag) > 100 for tag in tags):
        raise InvalidOperationalInput("Invalid tags")
    normalized_tags = [normalize_tag(tag) for tag in tags]
    if len(set(normalized_tags)) != len(normalized_tags):
        raise InvalidOperationalInput("Tags must be unique after normalization")
    if not isinstance(value.content, str) or "\x00" in value.content:
        raise InvalidOperationalInput("Invalid Markdown content")
    try:
        value.content.encode("utf-8")
    except UnicodeError:
        raise InvalidOperationalInput("Invalid Markdown content") from None
    if len(value.content.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise InvalidOperationalInput("Markdown content exceeds the size limit")
    operation_date = value.operation_date or datetime.now(UTC).date().isoformat()
    try:
        operation_date = date.fromisoformat(operation_date).isoformat()
    except (TypeError, ValueError):
        raise InvalidOperationalInput("Invalid operation date") from None
    return {
        "title": value.title.strip(),
        "category": value.category,
        "tags": tags,
        "visibility": value.visibility,
        "source_url": _validate_source_url(value.source_url),
        "operation_date": operation_date,
        "content": value.content.replace("\r\n", "\n").replace("\r", "\n"),
    }


def _metadata(normalized: dict[str, Any], *, created: str, updated: str) -> dict[str, Any]:
    return {
        "title": normalized["title"],
        "category": normalized["category"],
        "tags": normalized["tags"],
        "visibility": normalized["visibility"],
        "source_url": normalized["source_url"],
        "created": created,
        "updated": updated,
    }


def _source_bytes(document_id: str, metadata: dict[str, Any], content: str) -> bytes:
    # JSONB does not preserve Python mapping insertion order. Build frontmatter in a
    # fixed field order so create/retry bytes and their content hashes stay identical.
    frontmatter = {
        "document_id": document_id,
        "title": metadata["title"],
        "category": metadata["category"],
        "tags": metadata["tags"],
        "visibility": metadata["visibility"],
        "source_url": metadata.get("source_url"),
        "created": metadata["created"],
        "updated": metadata["updated"],
    }
    encoded = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    ).rstrip("\n")
    body = content
    if body and not body.endswith("\n"):
        body += "\n"
    try:
        raw = f"---\n{encoded}\n---\n\n{body}".encode()
    except UnicodeError:
        raise InvalidOperationalInput("Invalid Markdown content") from None
    if len(raw) > _MAX_SOURCE_BYTES:
        raise InvalidOperationalInput("Markdown content exceeds the size limit")
    return raw


def _manifest_bytes(
    document_id: str,
    revision_id: str,
    source_key: str,
    source_bytes: bytes,
    metadata: dict[str, Any],
) -> bytes:
    manifest = {
        "schema_version": 1,
        "document_id": document_id,
        "revision_id": revision_id,
        "title": metadata["title"],
        "category": metadata["category"],
        "tags": metadata["tags"],
        "visibility": metadata["visibility"],
        "source_url": metadata.get("source_url"),
        "mime_type": _MIME_TYPE,
        "content_hash": hashlib.sha256(source_bytes).hexdigest(),
        "object_key": source_key,
        "created_at": f"{metadata['created']}T00:00:00Z",
        "updated_at": f"{metadata['updated']}T00:00:00Z",
    }
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class KnowledgeWriter:
    """Own the durable-intent, immutable-R2, verified-finalize write lifecycle."""

    def __init__(
        self,
        operational_store: PostgresOperationalStore,
        r2_client: RevisionS3Client,
        bucket: str,
        prefix: str = "knowledge",
    ):
        self.operational_store = operational_store
        self.objects = R2RevisionObjects(r2_client, bucket, prefix)

    def _reserve_metadata(
        self,
        idempotency_key: str,
        document_id: str,
        expected_current_revision_id: str | None,
        value: KnowledgeWriteInput,
        *,
        created: str,
        updated: str,
    ) -> tuple[WriteIntent, dict[str, Any], bytes]:
        normalized = _validate_input(value)
        metadata = _metadata(normalized, created=created, updated=updated)
        source_bytes = _source_bytes(document_id, metadata, normalized["content"])
        intent = self.operational_store.reserve_intent(
            WriteRequest(
                idempotency_key,
                document_id,
                expected_current_revision_id,
                source_bytes,
                metadata,
            )
        )
        stored = self.operational_store.get_intent_by_idempotency_key(idempotency_key)
        if stored is None:
            raise RevisionConflict("Write intent disappeared after reservation")
        intent, stored_metadata = stored
        # Rebuild from the immutable intent snapshot to guarantee replay uses its exact dates.
        source_bytes = _source_bytes(
            document_id,
            stored_metadata,
            normalized["content"],
        )
        if hashlib.sha256(source_bytes).hexdigest() != intent.content_hash:
            raise RevisionConflict("Idempotency key was used with another request")
        return intent, stored_metadata, source_bytes

    def _write_verified_pair(
        self,
        intent: WriteIntent,
        source_bytes: bytes,
        metadata: dict[str, Any],
    ) -> tuple[VerifiedRevision, str]:
        source_key, manifest_key = object_keys(intent.document_id, intent.revision_id)
        if (source_key, manifest_key) != (intent.source_key, intent.manifest_key):
            raise InvalidOperationalInput("Write intent object keys are invalid")
        manifest_bytes = _manifest_bytes(
            intent.document_id, intent.revision_id, source_key, source_bytes, metadata
        )
        try:
            source_readback = self.objects.put_if_absent(source_key, source_bytes, _MIME_TYPE)
            if source_readback != source_bytes:
                raise RevisionObjectConflict("R2 source read-back mismatch")
            if hashlib.sha256(source_readback).digest() != hashlib.sha256(source_bytes).digest():
                raise RevisionObjectConflict("R2 source hash read-back mismatch")
            if intent.state == "PREPARED":
                self.operational_store.update_intent_progress(intent.id, "SOURCE_WRITTEN")
            manifest_readback = self.objects.put_if_absent(
                manifest_key, manifest_bytes, "application/json; charset=utf-8"
            )
            if manifest_readback != manifest_bytes:
                raise RevisionObjectConflict("R2 manifest read-back mismatch")
            verified = self._verify_pair(
                intent.document_id,
                intent.revision_id,
                source_key,
                manifest_key,
                source_readback,
                manifest_readback,
                metadata,
            )
            if intent.state in {"PREPARED", "SOURCE_WRITTEN"}:
                self.operational_store.update_intent_progress(intent.id, "COMPLETE")
            return verified, hashlib.sha256(manifest_readback).hexdigest()
        except RevisionObjectConflict:
            try:
                self.operational_store.update_intent_progress(
                    intent.id, "QUARANTINED", "R2_IMMUTABLE_KEY_CONFLICT"
                )
            except Exception:  # noqa: BLE001, S110 - preserve the sanitized object conflict
                pass
            raise

    @staticmethod
    def _verify_pair(
        document_id: str,
        revision_id: str,
        source_key: str,
        manifest_key: str,
        source_bytes: bytes,
        manifest_bytes: bytes,
        metadata: dict[str, Any],
    ) -> VerifiedRevision:
        expected_source, expected_manifest = object_keys(document_id, revision_id)
        if (source_key, manifest_key) != (expected_source, expected_manifest):
            raise InvalidOperationalInput("R2 revision keys do not match the intent")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            parsed = parse_markdown_document(
                f"{metadata['category']}/{document_id}.md", source_bytes.decode("utf-8")
            )
        except (UnicodeError, json.JSONDecodeError, KnowledgeDocumentError, KeyError, TypeError):
            raise InvalidOperationalInput("R2 revision pair is malformed") from None
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("document_id") != document_id
            or manifest.get("revision_id") != revision_id
            or manifest.get("object_key") != source_key
            or manifest_key != f"knowledge/{document_id}/revisions/{revision_id}/manifest.json"
            or manifest.get("content_hash") != source_hash
            or manifest.get("mime_type") != _MIME_TYPE
            or manifest.get("title") != metadata.get("title")
            or manifest.get("category") != metadata.get("category")
            or manifest.get("tags") != metadata.get("tags")
            or manifest.get("visibility") != metadata.get("visibility")
            or manifest.get("source_url") != metadata.get("source_url")
            or manifest.get("created_at") != f"{metadata.get('created')}T00:00:00Z"
            or manifest.get("updated_at") != f"{metadata.get('updated')}T00:00:00Z"
            or parsed.id != document_id
            or parsed.title != metadata.get("title")
            or parsed.category != metadata.get("category")
            or parsed.tags != metadata.get("tags")
            or parsed.visibility != metadata.get("visibility")
            or parsed.source_url != metadata.get("source_url")
            or parsed.created != metadata.get("created")
            or parsed.updated != metadata.get("updated")
        ):
            raise InvalidOperationalInput("R2 source and manifest do not match")
        return VerifiedRevision(source_bytes, manifest_bytes, _MIME_TYPE, 1)

    def _commit(
        self,
        intent: WriteIntent,
        source_bytes: bytes,
        metadata: dict[str, Any],
        operation_result: str,
    ) -> KnowledgeWriteResult:
        verified, manifest_hash = self._write_verified_pair(intent, source_bytes, metadata)
        job_id = self.operational_store.finalize_verified_revision(
            intent,
            verified,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
            mime_type=_MIME_TYPE,
        )
        current = self.operational_store.get_current_revision(intent.document_id)
        current_revision = current["current_revision_id"] if current else intent.revision_id
        del manifest_hash  # The finalized repository independently hashes verified bytes.
        return KnowledgeWriteResult(
            intent.document_id,
            intent.revision_id,
            current_revision,
            operation_result,
            "QUEUED",
            str(job_id),
        )

    @staticmethod
    def _date_for_replay(
        previous: tuple[WriteIntent, dict[str, Any]] | None, field: str, fallback: str
    ) -> str:
        if previous is None:
            return fallback
        value = previous[1].get(field)
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError):
            raise InvalidOperationalInput("Stored write metadata is malformed") from None

    def create_document(
        self,
        document_id: str,
        idempotency_key: str,
        value: KnowledgeWriteInput,
    ) -> KnowledgeWriteResult:
        if not DOCUMENT_ID_RE.fullmatch(document_id):
            raise InvalidOperationalInput("Invalid document ID")
        normalized = _validate_input(value)
        previous = self.operational_store.get_intent_by_idempotency_key(idempotency_key)
        operation_date = normalized["operation_date"]
        metadata = _metadata(
            normalized,
            created=self._date_for_replay(previous, "created", operation_date),
            updated=self._date_for_replay(previous, "updated", operation_date),
        )
        source_bytes = _source_bytes(document_id, metadata, normalized["content"])
        intent = self.operational_store.reserve_intent(
            WriteRequest(idempotency_key, document_id, None, source_bytes, metadata)
        )
        if intent.state == "NOOP":
            raise InvalidOperationalInput("Create intent cannot be a no-op")
        if intent.state == "CONFLICT":
            raise RevisionConflict("Create intent conflicts with an existing document")
        stored = self.operational_store.get_intent_by_idempotency_key(idempotency_key)
        if stored is None:
            raise RevisionConflict("Write intent disappeared after reservation")
        intent, stored_metadata = stored
        source_bytes = _source_bytes(document_id, stored_metadata, normalized["content"])
        return self._commit(intent, source_bytes, stored_metadata, "CREATED")

    def update_document(
        self,
        document_id: str,
        expected_current_revision_id: str,
        idempotency_key: str,
        value: KnowledgeWriteInput,
    ) -> KnowledgeWriteResult:
        if not DOCUMENT_ID_RE.fullmatch(document_id) or not _REVISION_ID_RE.fullmatch(
            expected_current_revision_id
        ):
            raise InvalidOperationalInput("Invalid update precondition")
        normalized = _validate_input(value)
        previous = self.operational_store.get_intent_by_idempotency_key(idempotency_key)
        current = self.operational_store.get_current_revision(document_id)
        replay_updated = self._date_for_replay(previous, "updated", normalized["operation_date"])

        if previous and previous[0].state == "NOOP":
            intent, stored_metadata, source_bytes = self._reserve_metadata(
                idempotency_key,
                document_id,
                expected_current_revision_id,
                value,
                created=self._date_for_replay(previous, "created", normalized["operation_date"]),
                updated=replay_updated,
            )
            result_revision = intent.result_revision_id
            current_now = self.operational_store.get_current_revision(document_id)
            if result_revision is None:
                raise InvalidOperationalInput("Stored no-op result is unavailable")
            return KnowledgeWriteResult(
                document_id,
                result_revision,
                current_now["current_revision_id"] if current_now else result_revision,
                "NOOP",
            )

        if current is None:
            raise KnowledgeNotFoundError("Knowledge document not found")
        if current["current_revision_id"] != expected_current_revision_id and (
            not previous or previous[0].state != "COMMITTED"
        ):
            # A committed idempotent replay may refer to an older parent. Let its intent
            # replay resolve below; new operations must reject this stale pointer.
            raise RevisionConflict("Expected current revision does not match")

        if previous and previous[0].state == "COMMITTED":
            intent, stored_metadata, source_bytes = self._reserve_metadata(
                idempotency_key,
                document_id,
                expected_current_revision_id,
                value,
                created=self._date_for_replay(previous, "created", normalized["operation_date"]),
                updated=replay_updated,
            )
            return self._commit(intent, source_bytes, stored_metadata, "UPDATED")

        current_source = self._read_current_source(current)
        existing_metadata = current["metadata_snapshot"]
        if self._same_as_current(normalized, current_source, existing_metadata):
            metadata = {
                key: existing_metadata[key]
                for key in (
                    "title",
                    "category",
                    "tags",
                    "visibility",
                    "source_url",
                    "created",
                    "updated",
                )
            }
            intent = self.operational_store.reserve_intent(
                WriteRequest(
                    idempotency_key,
                    document_id,
                    expected_current_revision_id,
                    current_source,
                    metadata,
                )
            )
            if intent.state == "NOOP":
                result_revision = intent.result_revision_id
            elif intent.state == "COMMITTED":
                stored = self.operational_store.get_intent_by_idempotency_key(idempotency_key)
                if stored is None:
                    raise RevisionConflict("Write intent disappeared after reservation")
                intent, stored_metadata = stored
                return self._commit(intent, current_source, stored_metadata, "UPDATED")
            else:
                result_revision = self.operational_store.complete_noop(intent)
            current_now = self.operational_store.get_current_revision(document_id)
            if result_revision is None:
                raise InvalidOperationalInput("No-op result is unavailable")
            return KnowledgeWriteResult(
                document_id,
                result_revision,
                current_now["current_revision_id"] if current_now else result_revision,
                "NOOP",
            )

        created = current["metadata_snapshot"].get("created")
        if not isinstance(created, str):
            raise InvalidOperationalInput("Current revision metadata is malformed")
        updated = replay_updated
        metadata = _metadata(normalized, created=created, updated=updated)
        source_bytes = _source_bytes(document_id, metadata, normalized["content"])
        intent = self.operational_store.reserve_intent(
            WriteRequest(
                idempotency_key,
                document_id,
                expected_current_revision_id,
                source_bytes,
                metadata,
            )
        )
        if intent.state == "NOOP":
            result_revision = intent.result_revision_id
            current_now = self.operational_store.get_current_revision(document_id)
            if result_revision is None:
                raise InvalidOperationalInput("Stored no-op result is unavailable")
            return KnowledgeWriteResult(
                document_id,
                result_revision,
                current_now["current_revision_id"] if current_now else result_revision,
                "NOOP",
            )
        stored = self.operational_store.get_intent_by_idempotency_key(idempotency_key)
        if stored is None:
            raise RevisionConflict("Write intent disappeared after reservation")
        intent, stored_metadata = stored
        source_bytes = _source_bytes(document_id, stored_metadata, normalized["content"])
        return self._commit(intent, source_bytes, stored_metadata, "UPDATED")

    @staticmethod
    def _same_as_current(
        normalized: dict[str, Any], source_bytes: bytes, metadata: dict[str, Any]
    ) -> bool:
        body_marker = b"\n---\n\n"
        marker_at = source_bytes.find(body_marker)
        if marker_at < 0:
            raise KnowledgeDocumentError("Current Markdown is malformed")
        body = normalized["content"]
        if body and not body.endswith("\n"):
            body += "\n"
        return source_bytes[marker_at + len(body_marker) :] == body.encode("utf-8") and all(
            normalized[field] == metadata.get(field)
            for field in ("title", "category", "tags", "visibility", "source_url")
        )

    def _read_current_source(self, record: dict[str, Any]) -> bytes:
        source = self.objects.get(record["source_object_key"])
        manifest = self.objects.get(record["manifest_object_key"])
        if hashlib.sha256(source).hexdigest() != record["content_hash"]:
            raise RevisionObjectConflict("Current R2 source hash does not match PostgreSQL")
        if hashlib.sha256(manifest).hexdigest() != record["manifest_hash"]:
            raise RevisionObjectConflict("Current R2 manifest hash does not match PostgreSQL")
        metadata = record["metadata_snapshot"]
        self._verify_pair(
            record["id"],
            record["current_revision_id"],
            record["source_object_key"],
            record["manifest_object_key"],
            source,
            manifest,
            metadata,
        )
        if (
            record["title"] != metadata.get("title")
            or record["category"] != metadata.get("category")
            or record["visibility"] != metadata.get("visibility")
            or record["source_url"] != metadata.get("source_url")
            or sorted(map(normalize_tag, record.get("tags", [])))
            != sorted(map(normalize_tag, metadata.get("tags", [])))
        ):
            raise RevisionObjectConflict(
                "Current PostgreSQL projection does not match its revision"
            )
        return source

    def delete_document(
        self, document_id: str, expected_current_revision_id: str
    ) -> KnowledgeWriteResult:
        self.operational_store.tombstone_document(document_id, expected_current_revision_id)
        return KnowledgeWriteResult(
            document_id,
            expected_current_revision_id,
            expected_current_revision_id,
            "DELETED",
        )

    def _document(self, record: dict[str, Any]) -> KnowledgeDocument:
        source = self._read_current_source(record)
        try:
            document = parse_markdown_document(
                f"{record['category']}/{record['id']}.md", source.decode("utf-8")
            )
            return replace(document, current_revision_id=record["current_revision_id"])
        except (UnicodeError, KnowledgeDocumentError):
            raise RevisionObjectConflict("Current R2 source Markdown is invalid") from None

    def list_documents(self, visibility: str | None = None) -> list[KnowledgeDocument]:
        if visibility is not None and visibility not in VISIBILITIES:
            raise InvalidOperationalInput("Invalid visibility filter")
        records = self.operational_store.list_active_documents()
        return [
            self._document(record)
            for record in records
            if visibility is None or record["visibility"] == visibility
        ]

    def get_document(self, document_id: str) -> KnowledgeDocument:
        record = self.operational_store.get_current_revision(document_id)
        if record is None:
            raise KnowledgeNotFoundError("Knowledge document not found")
        return self._document(record)

    def search(self, query: str, limit: int = 50) -> list[KnowledgeDocument]:
        return search_documents(self.list_documents(), query, limit)

    def stats(self, visibility: str | None = None) -> dict[str, Any]:
        if visibility is not None and visibility not in VISIBILITIES:
            raise InvalidOperationalInput("Invalid visibility filter")
        documents = [
            item
            for item in self.operational_store.list_active_documents()
            if visibility is None or item["visibility"] == visibility
        ]
        counts = {category: 0 for category in CATEGORIES}
        for item in documents:
            counts[item["category"]] += 1
        return {"documents": len(documents), "categories": counts}

    def health_status(self) -> dict[str, Any]:
        self.operational_store.list_active_documents()
        return {
            "backend": "postgres+r2",
            "status": "CONFIGURED",
            "ref": None,
            "commit": None,
            "last_refresh": None,
            "malformed_documents": 0,
        }


def get_knowledge_writer() -> KnowledgeWriter:
    from engine.postgres_operational import get_operational_store
    from engine.r2_knowledge_store import get_r2_knowledge_store

    r2_store = get_r2_knowledge_store()
    return KnowledgeWriter(
        get_operational_store(),
        r2_store.client,
        r2_store.bucket,
        r2_store.prefix,
    )
