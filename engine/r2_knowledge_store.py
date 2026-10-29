"""Read-only, revision-aware KnowledgeStore backed by an S3-compatible R2 bucket.

R2 is the immutable source-object store. This adapter keeps a process-local metadata
snapshot and hydrated document cache; PostgreSQL is expected to replace that
temporary query index in a later phase.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from engine.knowledge_store import (
    CATEGORIES,
    VISIBILITIES,
    KnowledgeDocument,
    KnowledgeDocumentError,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeStore,
    KnowledgeUnavailableError,
    parse_markdown_document,
    search_documents,
)

_DOCUMENT_ID = re.compile(r"^[a-f0-9]{32,64}$")
_REVISION_ID = re.compile(r"^[a-f0-9]{64}$")
_CONTENT_HASH = re.compile(r"^[a-f0-9]{64}$")
_DEFAULT_REFRESH_SECONDS = 30.0
_DEFAULT_STALE_MAX_SECONDS = 900.0
_SCHEMA_VERSION = 1


class S3Client(Protocol):
    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Manifest:
    document_id: str
    revision_id: str
    title: str
    category: str
    tags: tuple[str, ...]
    visibility: str
    source_url: str | None
    mime_type: str
    content_hash: str
    source_key: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class _MetadataSnapshot:
    fingerprint: tuple[tuple[str, str, str, int], ...]
    manifests: tuple[_Manifest, ...]
    fetched_at: float
    malformed_count: int


class R2KnowledgeStore(KnowledgeStore):
    """Expose the newest immutable revision for each document through KnowledgeStore."""

    def __init__(
        self,
        client: S3Client,
        bucket: str,
        prefix: str = "knowledge",
        refresh_seconds: float = _DEFAULT_REFRESH_SECONDS,
        stale_max_seconds: float = _DEFAULT_STALE_MAX_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        self.client = client
        self.bucket = bucket.strip()
        self.prefix = prefix.strip("/")
        if not self.bucket or not self.prefix or ".." in self.prefix.split("/"):
            raise KnowledgeUnavailableError("R2 knowledge storage configuration is invalid")
        self.refresh_seconds = max(0.0, refresh_seconds)
        self.stale_max_seconds = max(0.0, stale_max_seconds)
        self.now = now
        self._metadata: _MetadataSnapshot | None = None
        self._documents: tuple[KnowledgeDocument, ...] | None = None
        self._documents_snapshot: _MetadataSnapshot | None = None
        self._documents_updated_at: float | None = None
        self._last_check = 0.0
        self._last_error_at: float | None = None
        self._status = "UNAVAILABLE"
        self._malformed_source_count = 0
        self._content_error_at: float | None = None
        self._lock = threading.RLock()

    def _key_prefix(self) -> str:
        return f"{self.prefix}/"

    def _request(self, operation: str, **kwargs: Any) -> Any:
        try:
            if operation == "list":
                return self.client.list_objects_v2(**kwargs)
            if operation == "get":
                return self.client.get_object(**kwargs)
        except Exception:  # noqa: BLE001 - normalize SDK and injected transport errors
            raise KnowledgeUnavailableError("R2 knowledge source is unavailable") from None
        raise KnowledgeUnavailableError("R2 knowledge operation is invalid")

    @staticmethod
    def _timestamp(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Invalid {field}")
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid {field}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"Invalid {field}")
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _parse_manifest(self, key: str, raw: bytes) -> _Manifest:
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError("Unsupported schema")
            document_id = data.get("document_id")
            revision_id = data.get("revision_id")
            category = data.get("category")
            title = data.get("title")
            tags = data.get("tags")
            visibility = data.get("visibility")
            source_key = data.get("object_key")
            content_hash = data.get("content_hash")
            mime_type = data.get("mime_type")
            expected_prefix = f"{self.prefix}/{document_id}/revisions/{revision_id}/"
            if not isinstance(document_id, str) or not _DOCUMENT_ID.fullmatch(document_id):
                raise ValueError("Invalid document ID")
            if not isinstance(revision_id, str) or not _REVISION_ID.fullmatch(revision_id):
                raise ValueError("Invalid revision ID")
            if key != expected_prefix + "manifest.json":
                raise ValueError("Manifest key does not match its identity")
            if category not in CATEGORIES or not isinstance(title, str) or not title.strip():
                raise ValueError("Invalid document metadata")
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise ValueError("Invalid tags")
            if visibility not in VISIBILITIES:
                raise ValueError("Invalid visibility")
            if not isinstance(source_key, str) or source_key != expected_prefix + "source.md":
                raise ValueError("Invalid source object key")
            if not isinstance(content_hash, str) or not _CONTENT_HASH.fullmatch(content_hash):
                raise ValueError("Invalid content hash")
            if not isinstance(mime_type, str) or not mime_type.casefold().startswith(
                "text/markdown"
            ):
                raise ValueError("Invalid source MIME type")
            source_url = data.get("source_url")
            if source_url is not None and not isinstance(source_url, str):
                raise ValueError("Invalid source URL")
            return _Manifest(
                document_id=document_id,
                revision_id=revision_id,
                title=title.strip(),
                category=category,
                tags=tuple(tags),
                visibility=visibility,
                source_url=source_url,
                mime_type=mime_type,
                content_hash=content_hash,
                source_key=source_key,
                created_at=self._timestamp(data.get("created_at"), "created_at"),
                updated_at=self._timestamp(data.get("updated_at"), "updated_at"),
            )
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            raise KnowledgeDocumentError("R2 knowledge manifest is malformed") from None

    def _list_manifest_keys(self) -> tuple[tuple[str, str, str, int], ...]:
        keys: list[tuple[str, str, str, int]] = []
        continuation = None
        while True:
            params: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": self._key_prefix(),
            }
            if continuation:
                params["ContinuationToken"] = continuation
            page = self._request("list", **params)
            contents = page.get("Contents", [])
            if not isinstance(contents, list):
                raise KnowledgeUnavailableError("R2 knowledge listing is invalid")
            for entry in contents:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("Key")
                if not isinstance(key, str) or not key.endswith("/manifest.json"):
                    continue
                etag = entry.get("ETag", "")
                modified = entry.get("LastModified")
                modified_value = (
                    modified.isoformat() if hasattr(modified, "isoformat") else str(modified or "")
                )
                size = entry.get("Size", 0)
                if not isinstance(size, int) or size < 0:
                    size = 0
                keys.append((key, str(etag), modified_value, size))
            if not page.get("IsTruncated"):
                break
            continuation = page.get("NextContinuationToken")
            if not isinstance(continuation, str) or not continuation:
                raise KnowledgeUnavailableError("R2 knowledge listing is incomplete")
        return tuple(sorted(keys))

    @staticmethod
    def _read_body(response: dict[str, Any]) -> bytes:
        body = response.get("Body")
        try:
            value = body.read()
            if not isinstance(value, bytes):
                raise TypeError("Object body is not bytes")
            return value
        except Exception:  # noqa: BLE001 - do not leak SDK/response details
            raise KnowledgeUnavailableError("R2 knowledge object could not be read") from None
        finally:
            close = getattr(body, "close", None)
            if close:
                try:
                    close()
                except Exception:  # noqa: BLE001, S110 - cleanup must not mask the read result
                    pass

    def _read_object(self, key: str) -> bytes:
        return self._read_body(self._request("get", Bucket=self.bucket, Key=key))

    def _build_metadata(
        self, fingerprint: tuple[tuple[str, str, str, int], ...]
    ) -> _MetadataSnapshot:
        manifests: dict[str, _Manifest] = {}
        malformed = 0
        for key, _, _, _ in fingerprint:
            try:
                manifest = self._parse_manifest(key, self._read_object(key))
            except KnowledgeDocumentError:
                malformed += 1
                continue
            current = manifests.get(manifest.document_id)
            revision_order = (manifest.updated_at, manifest.created_at, manifest.revision_id)
            if current is None or revision_order > (
                current.updated_at,
                current.created_at,
                current.revision_id,
            ):
                manifests[manifest.document_id] = manifest
        ordered = tuple(
            sorted(
                manifests.values(),
                key=lambda item: (item.updated_at, item.document_id),
                reverse=True,
            )
        )
        return _MetadataSnapshot(fingerprint, ordered, self.now(), malformed)

    def _metadata_snapshot(self) -> _MetadataSnapshot:
        with self._lock:
            now = self.now()
            if self._metadata is not None and now - self._last_check < self.refresh_seconds:
                if self._status == "UNAVAILABLE":
                    raise KnowledgeUnavailableError("R2 knowledge source is unavailable")
                if (
                    self._status == "STALE"
                    and self._last_error_at is not None
                    and now - self._last_error_at > self.stale_max_seconds
                ):
                    self._status = "UNAVAILABLE"
                    raise KnowledgeUnavailableError("R2 knowledge source is unavailable")
                return self._metadata

            self._last_check = now
            try:
                fingerprint = self._list_manifest_keys()
                if self._metadata is not None and fingerprint == self._metadata.fingerprint:
                    self._last_error_at = None
                    if self._content_error_at is None:
                        self._status = "HEALTHY"
                    elif (
                        self._documents_updated_at is not None
                        and self.now() - self._documents_updated_at <= self.stale_max_seconds
                    ):
                        self._status = "STALE"
                    else:
                        self._status = "UNAVAILABLE"
                    return self._metadata
                snapshot = self._build_metadata(fingerprint)
            except KnowledgeUnavailableError:
                self._last_error_at = self.now()
                if (
                    self._metadata is not None
                    and self._last_error_at - self._metadata.fetched_at <= self.stale_max_seconds
                ):
                    self._status = "STALE"
                    return self._metadata
                self._status = "UNAVAILABLE"
                raise KnowledgeUnavailableError("R2 knowledge source is unavailable") from None

            self._metadata = snapshot
            self._status = "HEALTHY"
            self._last_error_at = None
            return snapshot

    def _hydrate(self, snapshot: _MetadataSnapshot) -> tuple[KnowledgeDocument, ...]:
        with self._lock:
            if self._documents is not None and self._documents_updated_at == snapshot.fetched_at:
                return self._documents
            try:
                documents = []
                malformed_sources = 0
                for manifest in snapshot.manifests:
                    try:
                        raw_bytes = self._read_object(manifest.source_key)
                        if hashlib.sha256(raw_bytes).hexdigest() != manifest.content_hash:
                            raise KnowledgeUnavailableError(
                                "R2 knowledge source integrity check failed"
                            )
                        raw = raw_bytes.decode("utf-8")
                        relative_path = f"{manifest.category}/{manifest.document_id}.md"
                        document = parse_markdown_document(relative_path, raw)
                        if (
                            document.title != manifest.title
                            or document.category != manifest.category
                            or tuple(document.tags) != manifest.tags
                            or document.visibility != manifest.visibility
                            or document.source_url != manifest.source_url
                        ):
                            raise KnowledgeDocumentError("R2 source and manifest metadata differ")
                        created = date.fromisoformat(manifest.created_at[:10]).isoformat()
                        updated = date.fromisoformat(manifest.updated_at[:10]).isoformat()
                        documents.append(
                            KnowledgeDocument(
                                id=manifest.document_id,
                                title=document.title,
                                category=document.category,
                                tags=list(document.tags),
                                created=created,
                                updated=updated,
                                visibility=document.visibility,
                                source_url=document.source_url,
                                content=document.content,
                                relative_path=relative_path,
                            )
                        )
                    except KnowledgeDocumentError:
                        malformed_sources += 1
                self._malformed_source_count = malformed_sources
            except KnowledgeDocumentError:
                raise
            except (KnowledgeUnavailableError, UnicodeError, ValueError):
                self._last_error_at = self.now()
                self._content_error_at = self._last_error_at
                if (
                    self._documents is not None
                    and self._documents_updated_at is not None
                    and self._last_error_at - self._documents_updated_at <= self.stale_max_seconds
                ):
                    self._status = "STALE"
                    return self._documents
                self._status = "UNAVAILABLE"
                raise KnowledgeUnavailableError("R2 knowledge source is unavailable") from None
            self._documents = tuple(documents)
            self._documents_snapshot = snapshot
            self._documents_updated_at = snapshot.fetched_at
            self._status = "HEALTHY"
            self._last_error_at = None
            self._content_error_at = None
            return self._documents

    def list_documents(self, visibility: str | None = None) -> list[KnowledgeDocument]:
        if visibility is not None and visibility not in VISIBILITIES:
            raise KnowledgeError("Invalid visibility filter")
        documents = self._hydrate(self._metadata_snapshot())
        return [
            KnowledgeDocument(**{**item.as_dict(), "tags": list(item.tags)})
            for item in documents
            if visibility is None or item.visibility == visibility
        ]

    def get_document(self, document_id: str) -> KnowledgeDocument:
        for document in self.list_documents():
            if document.id == document_id:
                return document
        raise KnowledgeNotFoundError(document_id)

    def search(self, query: str, limit: int = 50) -> list[KnowledgeDocument]:
        return search_documents(self.list_documents(), query, limit)

    def stats(self, visibility: str | None = None) -> dict[str, Any]:
        if visibility is not None and visibility not in VISIBILITIES:
            raise KnowledgeError("Invalid visibility filter")
        snapshot = self._metadata_snapshot()
        with self._lock:
            if self._status == "STALE" and self._documents_snapshot is not None:
                snapshot = self._documents_snapshot
        manifests = [
            item
            for item in snapshot.manifests
            if visibility is None or item.visibility == visibility
        ]
        counts = {category: 0 for category in CATEGORIES}
        for manifest in manifests:
            counts[manifest.category] += 1
        return {"documents": len(manifests), "categories": counts}

    def health_status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._metadata
            status = self._status
            if self._content_error_at is not None:
                if (
                    self._documents_updated_at is not None
                    and self.now() - self._documents_updated_at <= self.stale_max_seconds
                ):
                    status = "STALE"
                else:
                    status = "UNAVAILABLE"
            if status in {"STALE", "UNAVAILABLE"} and self._documents_snapshot is not None:
                snapshot = self._documents_snapshot
            return {
                "backend": "r2",
                "status": status,
                "ref": None,
                "commit": None,
                "last_refresh": (
                    datetime.fromtimestamp(snapshot.fetched_at, UTC).isoformat()
                    if snapshot
                    else None
                ),
                "malformed_documents": (
                    snapshot.malformed_count + self._malformed_source_count if snapshot else 0
                ),
            }


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        raise KnowledgeUnavailableError("R2 knowledge cache settings are invalid") from None
    if value < 0:
        raise KnowledgeUnavailableError("R2 knowledge cache settings are invalid")
    return value


def _create_client(endpoint_url: str, region: str, access_key: str, secret_key: str) -> S3Client:
    try:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                connect_timeout=5,
                read_timeout=20,
                retries={"max_attempts": 2, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )
    except Exception:  # noqa: BLE001 - keep SDK configuration details out of health/errors
        raise KnowledgeUnavailableError("R2 knowledge client could not be initialized") from None


_STORE_LOCK = threading.Lock()
_STORE_CONFIG_DIGEST: str | None = None
_STORE_INSTANCE: R2KnowledgeStore | None = None


def get_r2_knowledge_store() -> R2KnowledgeStore:
    endpoint = os.getenv("KNOWLEDGE_R2_ENDPOINT_URL", "").strip()
    account_id = os.getenv("KNOWLEDGE_R2_ACCOUNT_ID", "").strip()
    if not endpoint and account_id:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    bucket = os.getenv("KNOWLEDGE_R2_BUCKET_NAME", "").strip()
    access_key = os.getenv("KNOWLEDGE_R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("KNOWLEDGE_R2_SECRET_ACCESS_KEY", "")
    region = os.getenv("KNOWLEDGE_R2_REGION", "auto").strip() or "auto"
    prefix = os.getenv("KNOWLEDGE_R2_PREFIX", "knowledge").strip("/")
    if not all((endpoint, bucket, access_key, secret_key, region, prefix)):
        raise KnowledgeUnavailableError("R2 knowledge storage is not configured")
    parsed = urlsplit(endpoint)
    production = os.getenv("APP_ENV", "development").strip().casefold() == "production"
    if (
        parsed.scheme not in {"https", "http"}
        or (production and parsed.scheme != "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise KnowledgeUnavailableError("R2 knowledge endpoint configuration is invalid")
    refresh_seconds = _positive_float("KNOWLEDGE_R2_REFRESH_SECONDS", _DEFAULT_REFRESH_SECONDS)
    stale_max_seconds = _positive_float(
        "KNOWLEDGE_R2_STALE_MAX_SECONDS", _DEFAULT_STALE_MAX_SECONDS
    )
    digest = hashlib.sha256(
        "\0".join(
            (
                endpoint,
                bucket,
                access_key,
                secret_key,
                region,
                prefix,
                str(refresh_seconds),
                str(stale_max_seconds),
            )
        ).encode("utf-8")
    ).hexdigest()
    global _STORE_CONFIG_DIGEST, _STORE_INSTANCE
    with _STORE_LOCK:
        if _STORE_INSTANCE is None or _STORE_CONFIG_DIGEST != digest:
            client = _create_client(endpoint, region, access_key, secret_key)
            _STORE_INSTANCE = R2KnowledgeStore(
                client, bucket, prefix, refresh_seconds, stale_max_seconds
            )
            _STORE_CONFIG_DIGEST = digest
        return _STORE_INSTANCE
