"""Opt-in PostgreSQL persistence for Knowledge operational state.

R2 remains the canonical source store. This module never performs R2 I/O and
is not selected by the existing KnowledgeStore. The API uses it only for the
explicitly enabled, read-only runtime diagnostic or the opt-in Knowledge writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

DOCUMENT_ID_RE = re.compile(r"^[a-f0-9]{32,64}$")
REVISION_ID_RE = re.compile(r"^[a-f0-9]{64}$")
HASH_RE = REVISION_ID_RE
CATEGORIES = {"ai", "investment", "philosophy", "misc"}
VISIBILITIES = {"private", "public"}
MIGRATION_DIR = Path(__file__).resolve().parents[1] / "migrations"
ADVISORY_LOCK_ID = 721904231


class OperationalStoreError(RuntimeError):
    """Base error with safe, non-secret messages."""


class OperationalStoreTimeout(OperationalStoreError):
    """A bounded PostgreSQL diagnostic exceeded its configured time limit."""


class IdempotencyConflict(OperationalStoreError):
    pass


class RevisionConflict(OperationalStoreError):
    pass


class InvalidOperationalInput(OperationalStoreError):
    pass


class KnowledgeRecordNotFound(OperationalStoreError):
    pass


class MigrationError(OperationalStoreError):
    pass


class ConnectionFactory(Protocol):
    def __call__(self) -> Any: ...


@dataclass(frozen=True)
class WriteRequest:
    idempotency_key: str
    document_id: str
    expected_current_revision_id: str | None
    source_bytes: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class WriteIntent:
    id: uuid.UUID
    document_id: str
    revision_id: str
    request_digest: str
    source_key: str
    manifest_key: str
    state: str
    replay: bool = False
    result_revision_id: str | None = None
    content_hash: str = ""


@dataclass(frozen=True)
class VerifiedRevision:
    """Evidence returned by an R2 verifier; this object itself does no I/O."""

    source_bytes: bytes
    manifest_bytes: bytes
    mime_type: str
    manifest_schema_version: int = 1


def validate_request(request: WriteRequest) -> tuple[str, dict[str, Any]]:
    if not request.idempotency_key or len(request.idempotency_key) > 200:
        raise InvalidOperationalInput("Invalid idempotency key")
    if not DOCUMENT_ID_RE.fullmatch(request.document_id):
        raise InvalidOperationalInput("Invalid document ID")
    expected = request.expected_current_revision_id
    if expected is not None and not REVISION_ID_RE.fullmatch(expected):
        raise InvalidOperationalInput("Invalid expected revision ID")
    metadata = request.metadata
    if set(metadata) - {
        "title",
        "category",
        "tags",
        "visibility",
        "source_url",
        "created",
        "updated",
    }:
        raise InvalidOperationalInput("Unsupported metadata field")
    title = metadata.get("title")
    category = metadata.get("category")
    visibility = metadata.get("visibility")
    tags = metadata.get("tags")
    if not isinstance(title, str) or not title.strip():
        raise InvalidOperationalInput("Invalid title")
    if category not in CATEGORIES or visibility not in VISIBILITIES:
        raise InvalidOperationalInput("Invalid category or visibility")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise InvalidOperationalInput("Invalid tags")
    display_tags = [tag.strip() for tag in tags]
    normalized_tags = [normalize_tag(tag) for tag in display_tags]
    if any(not tag for tag in normalized_tags) or len(set(normalized_tags)) != len(normalized_tags):
        raise InvalidOperationalInput("Tags must be non-empty and unique after normalization")
    normalized = {
        **metadata,
        "title": title.strip(),
        "tags": display_tags,
    }
    digest_input = {
        "document_id": request.document_id,
        "expected_current_revision_id": expected,
        "content_hash": hashlib.sha256(request.source_bytes).hexdigest(),
        "metadata": normalized,
    }
    encoded = json.dumps(
        digest_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest(), normalized


def object_keys(document_id: str, revision_id: str) -> tuple[str, str]:
    if not DOCUMENT_ID_RE.fullmatch(document_id) or not REVISION_ID_RE.fullmatch(revision_id):
        raise InvalidOperationalInput("Invalid revision identity")
    prefix = f"knowledge/{document_id}/revisions/{revision_id}/"
    return prefix + "source.md", prefix + "manifest.json"


def normalize_tag(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def transition_job(state: str, target: str) -> str:
    legal = {
        "QUEUED": {"RUNNING", "CANCELLED", "SUPERSEDED"},
        "RUNNING": {"RETRY_WAIT", "SUCCEEDED", "FAILED", "SUPERSEDED"},
        "RETRY_WAIT": {"RUNNING", "CANCELLED", "SUPERSEDED", "FAILED"},
        "SUCCEEDED": set(),
        "FAILED": set(),
        "CANCELLED": set(),
        "SUPERSEDED": set(),
    }
    if target not in legal.get(state, set()):
        raise InvalidOperationalInput("Illegal processing job state transition")
    return target


def transition_intent(state: str, target: str) -> str:
    legal = {
        "PREPARED": {"SOURCE_WRITTEN", "COMPLETE", "CONFLICT", "QUARANTINED", "NOOP"},
        "SOURCE_WRITTEN": {"COMPLETE", "CONFLICT", "QUARANTINED"},
        "COMPLETE": {"COMMITTED", "CONFLICT", "QUARANTINED"},
        "COMMITTED": set(),
        "CONFLICT": set(),
        "QUARANTINED": set(),
        "NOOP": set(),
    }
    if target not in legal.get(state, set()):
        raise InvalidOperationalInput("Illegal write intent state transition")
    return target


def connect_from_environment(connect_timeout: int = 5) -> Any:
    from dotenv import load_dotenv

    load_dotenv(override=False)
    dsn = os.getenv("POSTGRES_DATABASE_URL", "").strip()
    if not dsn:
        raise OperationalStoreError("PostgreSQL operational storage is not configured")
    try:
        import psycopg
    except Exception:  # noqa: BLE001 - keep optional driver import failures sanitized
        raise OperationalStoreError("PostgreSQL operational storage is unavailable") from None
    try:
        return psycopg.connect(dsn, connect_timeout=connect_timeout)
    except psycopg.errors.ConnectionTimeout:
        raise OperationalStoreTimeout("PostgreSQL connection timed out") from None
    except Exception:  # noqa: BLE001 - normalize driver errors that may contain the DSN
        # Do not forward driver exceptions: they can contain the DSN.
        raise OperationalStoreError("PostgreSQL operational storage is unavailable") from None


def diagnose_postgres_connection_read_only() -> int:
    """Return only the numeric server version after a bounded read-only query."""
    try:
        import psycopg
    except Exception:  # noqa: BLE001 - keep driver import failures sanitized
        raise OperationalStoreError("PostgreSQL diagnostic is unavailable") from None

    connection = connect_from_environment(connect_timeout=3)
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '3000ms'")
                cursor.execute("SELECT current_setting('server_version_num')")
                row = cursor.fetchone()
        if not row or not isinstance(row[0], str) or not row[0].isdecimal():
            raise OperationalStoreError("PostgreSQL diagnostic returned an invalid result")
        return int(row[0])
    except OperationalStoreTimeout:
        raise
    except Exception as exc:  # noqa: BLE001 - never forward SQL or driver error details
        if isinstance(exc, psycopg.errors.QueryCanceled):
            raise OperationalStoreTimeout("PostgreSQL diagnostic timed out") from None
        raise OperationalStoreError("PostgreSQL diagnostic is unavailable") from None
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - cleanup errors may contain connection details
            pass


def _migration_files() -> list[tuple[int, Path, str]]:
    result = []
    for path in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        match = re.match(r"^(\d{3})_", path.name)
        if match:
            sql = path.read_text(encoding="utf-8")
            result.append((int(match.group(1)), path, sql))
    return result


def migrate(connection_factory: ConnectionFactory = connect_from_environment) -> list[int]:
    """Apply checked-in SQL migrations, one transaction per version."""
    files = _migration_files()
    versions = [version for version, _, _ in files]
    if len(versions) != len(set(versions)) or versions != sorted(versions):
        raise MigrationError("Migration versions are duplicated or unordered")
    applied_now: list[int] = []
    connection = connection_factory()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version integer PRIMARY KEY, checksum char(64) NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cursor.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
            applied = dict(cursor.fetchall())
        known = set(versions)
        if set(applied) - known:
            raise MigrationError("Database contains an unknown migration version")
        for version, _, sql in files:
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version].strip() != checksum:
                    raise MigrationError("Applied migration checksum does not match source")
                continue
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(sql)
                cursor.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
            applied_now.append(version)
        connection.commit()
        return applied_now
    except MigrationError:
        connection.rollback()
        raise
    except Exception:  # noqa: BLE001 - do not leak database or migration details
        connection.rollback()
        raise MigrationError("PostgreSQL migration failed; details withheld") from None
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
            connection.commit()
        except Exception:  # noqa: BLE001 - cleanup errors must not expose connection details
            connection.rollback()
        connection.close()


class PostgresOperationalStore:
    """Persistence foundation, opt-in and separate from public Knowledge APIs."""

    def __init__(self, connection_factory: ConnectionFactory = connect_from_environment):
        self.connection_factory = connection_factory

    def reserve_intent(self, request: WriteRequest) -> WriteIntent:
        digest, metadata = validate_request(request)
        source_hash = hashlib.sha256(request.source_bytes).hexdigest()
        conn = self.connection_factory()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (request.idempotency_key,),
                )
                cur.execute(
                    "SELECT id, request_digest, document_id, revision_id, source_object_key, "
                    "manifest_object_key, state, result_revision_id, content_hash "
                    "FROM revision_write_intents WHERE idempotency_key=%s",
                    (request.idempotency_key,),
                )
                existing = cur.fetchone()
                if existing:
                    if existing[1].strip() != digest:
                        raise IdempotencyConflict("Idempotency key was used with another request")
                    return WriteIntent(
                        existing[0],
                        existing[2],
                        existing[3],
                        existing[1].strip(),
                        existing[4],
                        existing[5],
                        existing[6],
                        True,
                        existing[7],
                        existing[8],
                    )
                document_id = request.document_id
                intent_id = uuid.uuid4()
                revision_id = uuid.uuid4().hex + uuid.uuid4().hex
                source_key, manifest_key = object_keys(document_id, revision_id)
                cur.execute(
                    "INSERT INTO documents(id,title,category,visibility,source_url,state) "
                    "VALUES (%s,%s,%s,%s,%s,'PENDING') ON CONFLICT (id) DO NOTHING",
                    (
                        document_id,
                        metadata["title"],
                        metadata["category"],
                        metadata["visibility"],
                        metadata.get("source_url"),
                    ),
                )
                cur.execute(
                    "SELECT current_revision_id,state FROM documents WHERE id=%s FOR UPDATE",
                    (document_id,),
                )
                current = cur.fetchone()
                if current is None:
                    raise OperationalStoreError("Document reservation failed")
                if current[1] not in {"PENDING", "ACTIVE"}:
                    raise RevisionConflict("Document is not writable in its current state")
                if current[0] != request.expected_current_revision_id:
                    raise RevisionConflict("Expected current revision does not match")
                cur.execute(
                    "INSERT INTO revision_write_intents(id,idempotency_key,request_digest,document_id,revision_id,"
                    "expected_current_revision_id,source_object_key,manifest_object_key,content_hash,metadata_snapshot,state) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'PREPARED')",
                    (
                        intent_id,
                        request.idempotency_key,
                        digest,
                        document_id,
                        revision_id,
                        request.expected_current_revision_id,
                        source_key,
                        manifest_key,
                        source_hash,
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
            return WriteIntent(
                intent_id,
                document_id,
                revision_id,
                digest,
                source_key,
                manifest_key,
                "PREPARED",
                content_hash=source_hash,
            )
        except (
            IdempotencyConflict,
            RevisionConflict,
            InvalidOperationalInput,
            OperationalStoreError,
        ):
            raise
        except Exception:  # noqa: BLE001 - database errors may include query values
            raise OperationalStoreError("Could not reserve Knowledge write intent") from None
        finally:
            conn.close()

    def get_intent_by_idempotency_key(
        self, idempotency_key: str
    ) -> tuple[WriteIntent, dict[str, Any]] | None:
        if not idempotency_key or len(idempotency_key) > 200:
            raise InvalidOperationalInput("Invalid idempotency key")
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id,request_digest,document_id,revision_id,source_object_key,"
                    "manifest_object_key,state,result_revision_id,metadata_snapshot,content_hash "
                    "FROM revision_write_intents WHERE idempotency_key=%s",
                    (idempotency_key,),
                )
                row = cur.fetchone()
            if row is None:
                return None
            metadata = row[8] if isinstance(row[8], dict) else json.loads(row[8])
            return (
                WriteIntent(
                    row[0],
                    row[2],
                    row[3],
                    row[1].strip(),
                    row[4],
                    row[5],
                    row[6],
                    True,
                    row[7],
                    row[9],
                ),
                metadata,
            )
        except (InvalidOperationalInput, OperationalStoreError):
            raise
        except Exception:  # noqa: BLE001 - database errors may contain private values
            raise OperationalStoreError("Could not read Knowledge write intent") from None
        finally:
            conn.close()

    def complete_noop(self, intent: WriteIntent) -> str:
        """Record an idempotent update that exactly matches its active revision."""
        if intent.document_id is None or not DOCUMENT_ID_RE.fullmatch(intent.document_id):
            raise InvalidOperationalInput("Invalid no-op document identity")
        conn = self.connection_factory()
        result_revision_id: str | None = None
        conflict = False
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT document_id,revision_id,expected_current_revision_id,state,result_revision_id "
                    "FROM revision_write_intents WHERE id=%s FOR UPDATE",
                    (intent.id,),
                )
                row = cur.fetchone()
                if row is None or (row[0], row[1]) != (intent.document_id, intent.revision_id):
                    raise OperationalStoreError("Write intent identity mismatch")
                if row[3] == "NOOP":
                    result_revision_id = row[4]
                elif row[3] == "CONFLICT":
                    conflict = True
                elif row[3] != "PREPARED" or not row[2]:
                    raise InvalidOperationalInput("Write intent cannot complete as a no-op")
                else:
                    cur.execute(
                        "SELECT current_revision_id,state FROM documents WHERE id=%s FOR UPDATE",
                        (intent.document_id,),
                    )
                    document = cur.fetchone()
                    if document is None or document[1] != "ACTIVE" or document[0] != row[2]:
                        cur.execute(
                            "UPDATE revision_write_intents SET state='CONFLICT',updated_at=now(),"
                            "last_attempt_at=now(),attempt_count=attempt_count+1,"
                            "safe_error_code='CAS_CONFLICT' WHERE id=%s",
                            (intent.id,),
                        )
                        conflict = True
                    else:
                        transition_intent(row[3], "NOOP")
                        cur.execute(
                            "UPDATE revision_write_intents SET state='NOOP',result_revision_id=%s,"
                            "updated_at=now(),last_attempt_at=now(),attempt_count=attempt_count+1,"
                            "safe_error_code=NULL WHERE id=%s",
                            (row[2], intent.id),
                        )
                        result_revision_id = row[2]
            if conflict:
                raise RevisionConflict("Current revision changed before no-op completion")
            if not result_revision_id:
                raise OperationalStoreError("No-op result is unavailable")
            return result_revision_id
        except (InvalidOperationalInput, RevisionConflict, OperationalStoreError):
            raise
        except Exception:  # noqa: BLE001 - database errors may contain private values
            raise OperationalStoreError("Could not complete no-op Knowledge write") from None
        finally:
            conn.close()

    def get_current_revision(self, document_id: str) -> dict[str, Any] | None:
        if not DOCUMENT_ID_RE.fullmatch(document_id):
            raise InvalidOperationalInput("Invalid document ID")
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.id,d.current_revision_id,d.title,d.category,d.visibility,d.source_url,"
                    "d.created_at,d.updated_at,r.content_hash,r.source_object_key,r.manifest_object_key,"
                    "r.manifest_hash,r.mime_type,r.metadata_snapshot "
                    "FROM documents d JOIN revisions r "
                    "ON (r.document_id,r.revision_id)=(d.id,d.current_revision_id) "
                    "WHERE d.id=%s AND d.state='ACTIVE'",
                    (document_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute(
                    "SELECT t.display_name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id "
                    "WHERE dt.document_id=%s ORDER BY t.normalized_name",
                    (document_id,),
                )
                tags = [tag[0] for tag in cur.fetchall()]
            metadata = row[13] if isinstance(row[13], dict) else json.loads(row[13])
            return {
                "id": row[0],
                "current_revision_id": row[1],
                "title": row[2],
                "category": row[3],
                "visibility": row[4],
                "source_url": row[5],
                "created_at": row[6],
                "updated_at": row[7],
                "content_hash": row[8],
                "source_object_key": row[9],
                "manifest_object_key": row[10],
                "manifest_hash": row[11],
                "mime_type": row[12],
                "metadata_snapshot": metadata,
                "tags": tags,
            }
        except (InvalidOperationalInput, OperationalStoreError):
            raise
        except Exception:  # noqa: BLE001 - database errors may contain private values
            raise OperationalStoreError("Could not read current Knowledge revision") from None
        finally:
            conn.close()

    def list_active_documents(self) -> list[dict[str, Any]]:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.id,d.current_revision_id,d.title,d.category,d.visibility,d.source_url,"
                    "d.created_at,d.updated_at,r.content_hash,r.source_object_key,r.manifest_object_key,"
                    "r.manifest_hash,r.mime_type,r.metadata_snapshot,COALESCE((SELECT array_agg(t.display_name "
                    "ORDER BY t.normalized_name) FROM document_tags dt JOIN tags t ON t.id=dt.tag_id "
                    "WHERE dt.document_id=d.id),'{}') FROM documents d JOIN revisions r "
                    "ON (r.document_id,r.revision_id)=(d.id,d.current_revision_id) "
                    "WHERE d.state='ACTIVE' ORDER BY d.updated_at DESC,d.id"
                )
                rows = cur.fetchall()
            return [
                {
                    "id": row[0],
                    "current_revision_id": row[1],
                    "title": row[2],
                    "category": row[3],
                    "visibility": row[4],
                    "source_url": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "content_hash": row[8],
                    "source_object_key": row[9],
                    "manifest_object_key": row[10],
                    "manifest_hash": row[11],
                    "mime_type": row[12],
                    "metadata_snapshot": (
                        row[13] if isinstance(row[13], dict) else json.loads(row[13])
                    ),
                    "tags": list(row[14]),
                }
                for row in rows
            ]
        except OperationalStoreError:
            raise
        except Exception:  # noqa: BLE001 - database errors may contain private values
            raise OperationalStoreError("Could not list Knowledge documents") from None
        finally:
            conn.close()

    def finalize_verified_revision(
        self,
        intent: WriteIntent,
        verified: VerifiedRevision,
        *,
        pipeline_fingerprint: str,
        mime_type: str | None = None,
    ) -> uuid.UUID:
        """Atomically register revision, CAS current, complete intent and enqueue job."""
        if not HASH_RE.fullmatch(pipeline_fingerprint):
            raise InvalidOperationalInput("Invalid pipeline fingerprint")
        conn = self.connection_factory()
        job_id = uuid.uuid4()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT idempotency_key,request_digest,document_id,revision_id,expected_current_revision_id,"
                    "source_object_key,manifest_object_key,content_hash,metadata_snapshot,state "
                    "FROM revision_write_intents WHERE id=%s FOR UPDATE",
                    (intent.id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise OperationalStoreError("Write intent not found")
                (
                    _idem,
                    _digest,
                    doc_id,
                    rev_id,
                    expected,
                    source_key,
                    manifest_key,
                    content_hash,
                    metadata_json,
                    state,
                ) = row
                if (doc_id, rev_id, source_key, manifest_key) != (
                    intent.document_id,
                    intent.revision_id,
                    intent.source_key,
                    intent.manifest_key,
                ):
                    raise InvalidOperationalInput("Write intent identity mismatch")
                if state == "COMMITTED":
                    cur.execute(
                        "SELECT id FROM processing_jobs WHERE document_id=%s AND revision_id=%s AND pipeline_fingerprint=%s",
                        (doc_id, rev_id, pipeline_fingerprint),
                    )
                    found = cur.fetchone()
                    if found:
                        return found[0]
                    raise InvalidOperationalInput(
                        "Write intent is already committed under another pipeline"
                    )
                if state == "CONFLICT":
                    raise RevisionConflict("Write intent already conflicts with current revision")
                if state in {"NOOP", "QUARANTINED"}:
                    raise InvalidOperationalInput("Write intent is not finalizable")
                if content_hash != intent.content_hash:
                    raise InvalidOperationalInput("Durable write intent hash changed")
                actual_hash = hashlib.sha256(verified.source_bytes).hexdigest()
                manifest_hash = hashlib.sha256(verified.manifest_bytes).hexdigest()
                metadata = (
                    metadata_json if isinstance(metadata_json, dict) else json.loads(metadata_json)
                )
                if actual_hash != content_hash:
                    raise InvalidOperationalInput(
                        "Verified R2 source hash does not match write intent"
                    )
                if verified.manifest_schema_version != 1:
                    raise InvalidOperationalInput("Verified R2 evidence does not match intent")
                expected_prefix = f"knowledge/{doc_id}/revisions/{rev_id}/"
                try:
                    manifest = json.loads(verified.manifest_bytes.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    raise InvalidOperationalInput("Verified R2 manifest is malformed") from None
                if (
                    manifest.get("schema_version") != 1
                    or manifest.get("document_id") != doc_id
                    or manifest.get("revision_id") != rev_id
                    or manifest.get("object_key") != source_key
                    or source_key != expected_prefix + "source.md"
                    or manifest_key != expected_prefix + "manifest.json"
                    or manifest.get("content_hash") != actual_hash
                    or manifest.get("mime_type") != verified.mime_type
                    or not verified.mime_type.casefold().startswith("text/markdown")
                    or manifest.get("title") != metadata["title"]
                    or manifest.get("category") != metadata["category"]
                    or manifest.get("visibility") != metadata["visibility"]
                    or manifest.get("tags") != metadata["tags"]
                    or manifest.get("source_url") != metadata.get("source_url")
                ):
                    raise InvalidOperationalInput("R2 manifest does not match write intent")
                try:
                    from engine.knowledge_store import parse_markdown_document

                    parsed = parse_markdown_document(
                        f"{metadata['category']}/{doc_id}.md",
                        verified.source_bytes.decode("utf-8"),
                    )
                except (UnicodeError, ValueError, KeyError, TypeError):
                    raise InvalidOperationalInput("R2 source Markdown is invalid") from None
                if (
                    parsed.id != doc_id
                    or parsed.title != metadata["title"]
                    or parsed.category != metadata["category"]
                    or parsed.visibility != metadata["visibility"]
                    or parsed.tags != metadata["tags"]
                    or parsed.source_url != metadata.get("source_url")
                    or parsed.created != manifest.get("created_at", "")[:10]
                    or parsed.updated != manifest.get("updated_at", "")[:10]
                ):
                    raise InvalidOperationalInput("R2 source metadata does not match intent")
                cur.execute(
                    "SELECT current_revision_id,state FROM documents WHERE id=%s FOR UPDATE",
                    (doc_id,),
                )
                doc = cur.fetchone()
                if doc is None or doc[0] != expected or doc[1] not in {"PENDING", "ACTIVE"}:
                    raise RevisionConflict("Current revision changed before finalize")
                if state not in {"PREPARED", "SOURCE_WRITTEN", "COMPLETE"}:
                    raise InvalidOperationalInput("Write intent is not finalizable")
                now = datetime.now(UTC)
                cur.execute(
                    "INSERT INTO revisions(document_id,revision_id,parent_revision_id,content_hash,byte_length,"
                    "source_object_key,manifest_object_key,manifest_hash,mime_type,metadata_snapshot,"
                    "manifest_schema_version,registered_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                    (
                        doc_id,
                        rev_id,
                        expected,
                        content_hash,
                        len(verified.source_bytes),
                        source_key,
                        manifest_key,
                        manifest_hash,
                        mime_type or verified.mime_type,
                        json.dumps(metadata, ensure_ascii=False),
                        verified.manifest_schema_version,
                        now,
                    ),
                )
                cur.execute(
                    "UPDATE documents SET title=%s,category=%s,visibility=%s,source_url=%s,"
                    "current_revision_id=%s,state='ACTIVE',updated_at=%s,row_version=row_version+1 WHERE id=%s",
                    (
                        metadata["title"],
                        metadata["category"],
                        metadata["visibility"],
                        metadata.get("source_url"),
                        rev_id,
                        now,
                        doc_id,
                    ),
                )
                cur.execute(
                    "UPDATE processing_jobs SET state='SUPERSEDED',updated_at=now(),"
                    "safe_error_code='NEWER_REVISION_CURRENT' WHERE document_id=%s AND revision_id<>%s "
                    "AND state IN ('QUEUED','RETRY_WAIT','RUNNING')",
                    (doc_id, rev_id),
                )
                cur.execute(
                    "UPDATE revision_write_intents SET state='COMMITTED',updated_at=now(),last_attempt_at=now(),"
                    "attempt_count=attempt_count+1,safe_error_code=NULL WHERE id=%s",
                    (intent.id,),
                )
                cur.execute(
                    "INSERT INTO processing_jobs(id,document_id,revision_id,pipeline_fingerprint,state) "
                    "VALUES (%s,%s,%s,%s,'QUEUED')",
                    (job_id, doc_id, rev_id, pipeline_fingerprint),
                )
                cur.execute("DELETE FROM document_tags WHERE document_id=%s", (doc_id,))
                for tag in metadata["tags"]:
                    normalized = normalize_tag(tag)
                    cur.execute(
                        "INSERT INTO tags(normalized_name,display_name) VALUES (%s,%s) "
                        "ON CONFLICT(normalized_name) DO UPDATE SET normalized_name=EXCLUDED.normalized_name RETURNING id",
                        (normalized, tag.strip()),
                    )
                    tag_id = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO document_tags(document_id,tag_id) VALUES (%s,%s)",
                        (doc_id, tag_id),
                    )
            return job_id
        except RevisionConflict:
            # The failed finalize transaction is rolled back. Persist its
            # terminal CAS result separately so idempotency replay is stable.
            status_conn = self.connection_factory()
            try:
                with status_conn.transaction(), status_conn.cursor() as cur:
                    cur.execute("SELECT state FROM documents WHERE id=%s FOR UPDATE", (doc_id,))
                    current_state = cur.fetchone()
                    if current_state and current_state[0] in {"PENDING", "ACTIVE"}:
                        cur.execute(
                            "INSERT INTO revisions(document_id,revision_id,parent_revision_id,content_hash,byte_length,"
                            "source_object_key,manifest_object_key,manifest_hash,mime_type,metadata_snapshot,"
                            "manifest_schema_version,registered_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,now()) "
                            "ON CONFLICT (document_id,revision_id) DO NOTHING",
                            (
                                doc_id,
                                rev_id,
                                expected,
                                content_hash,
                                len(verified.source_bytes),
                                source_key,
                                manifest_key,
                                manifest_hash,
                                mime_type or verified.mime_type,
                                json.dumps(metadata, ensure_ascii=False),
                                verified.manifest_schema_version,
                            ),
                        )
                    cur.execute(
                        "UPDATE revision_write_intents SET state='CONFLICT',updated_at=now(),"
                        "last_attempt_at=now(),attempt_count=attempt_count+1,safe_error_code='CAS_CONFLICT' "
                        "WHERE id=%s AND state IN ('PREPARED','SOURCE_WRITTEN','COMPLETE')",
                        (intent.id,),
                    )
            except Exception:  # noqa: BLE001
                status_conn.rollback()
            finally:
                status_conn.close()
            raise
        except (InvalidOperationalInput, OperationalStoreError):
            raise
        except Exception:  # noqa: BLE001 - database errors may include private SQL values
            raise OperationalStoreError("Could not finalize Knowledge revision") from None
        finally:
            conn.close()

    def update_intent_progress(
        self, intent_id: uuid.UUID, target_state: str, safe_error_code: str | None = None
    ) -> None:
        """Persist source/manifest progress without implying cross-store atomicity."""
        conn = self.connection_factory()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM revision_write_intents WHERE id=%s FOR UPDATE", (intent_id,)
                )
                row = cur.fetchone()
                if row is None:
                    raise OperationalStoreError("Write intent not found")
                if row[0] in {"CONFLICT", "QUARANTINED", "NOOP"}:
                    raise InvalidOperationalInput("Write intent is terminal")
                progress = {"PREPARED": 0, "SOURCE_WRITTEN": 1, "COMPLETE": 2, "COMMITTED": 3}
                if (
                    target_state in progress
                    and row[0] in progress
                    and progress[row[0]] >= progress[target_state]
                ):
                    return
                transition_intent(row[0], target_state)
                cur.execute(
                    "UPDATE revision_write_intents SET state=%s,updated_at=now(),last_attempt_at=now(),"
                    "attempt_count=attempt_count+1,safe_error_code=%s WHERE id=%s",
                    (target_state, safe_error_code, intent_id),
                )
        except (InvalidOperationalInput, OperationalStoreError):
            raise
        except Exception:  # noqa: BLE001 - database errors may contain private SQL values
            raise OperationalStoreError("Could not update Knowledge write intent") from None
        finally:
            conn.close()

    def tombstone_document(self, document_id: str, expected_current_revision_id: str) -> None:
        """Soft-delete operational visibility; canonical R2 objects are retained."""
        if not DOCUMENT_ID_RE.fullmatch(document_id) or not REVISION_ID_RE.fullmatch(
            expected_current_revision_id
        ):
            raise InvalidOperationalInput("Invalid document deletion precondition")
        conn = self.connection_factory()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET state='DELETED',deleted_at=now(),updated_at=now(),"
                    "row_version=row_version+1 WHERE id=%s AND current_revision_id=%s AND state='ACTIVE'",
                    (document_id, expected_current_revision_id),
                )
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT state,current_revision_id FROM documents WHERE id=%s",
                        (document_id,),
                    )
                    current = cur.fetchone()
                    if current != ("DELETED", expected_current_revision_id):
                        raise RevisionConflict("Document changed or is unavailable")
        except (InvalidOperationalInput, RevisionConflict):
            raise
        except Exception:  # noqa: BLE001 - database errors may contain private SQL values
            raise OperationalStoreError("Could not tombstone Knowledge document") from None
        finally:
            conn.close()


def get_operational_store() -> PostgresOperationalStore:
    """Construct explicitly; never selected by existing API routes."""
    return PostgresOperationalStore()
