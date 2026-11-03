"""Read-only R2 inventory analysis and disposable PostgreSQL shadow import."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from engine.knowledge_store import (
    CATEGORIES,
    VISIBILITIES,
    KnowledgeDocumentError,
    parse_markdown_document,
)
from engine.postgres_operational import normalize_tag

_DOC = re.compile(r"^[a-f0-9]{32,64}$")
_REV = re.compile(r"^[a-f0-9]{64}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_KEY = re.compile(r"^knowledge/([^/]+)/revisions/([^/]+)/(source\.md|manifest\.json)$")


class ReadOnlyR2(Protocol):
    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


class GuardedR2Client:
    """Expose only LIST/GET and fail closed outside the knowledge prefix."""

    def __init__(self, client: Any, bucket: str, prefix: str = "knowledge"):
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/") + "/"
        self.operations: Counter[str] = Counter()

    def _validate(self, kwargs: dict[str, Any], listing: bool) -> None:
        if kwargs.get("Bucket") != self._bucket:
            raise PermissionError("R2 shadow access denied")
        target = kwargs.get("Prefix") if listing else kwargs.get("Key")
        if not isinstance(target, str) or not target.startswith(self._prefix):
            raise PermissionError("R2 shadow access denied")
        if any(part in {".", ".."} for part in target.split("/")):
            raise PermissionError("R2 shadow access denied")

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._validate(kwargs, True)
        self.operations["LIST"] += 1
        return self._client.list_objects_v2(**kwargs)

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._validate(kwargs, False)
        self.operations["GET"] += 1
        return self._client.get_object(**kwargs)


@dataclass(frozen=True)
class RevisionRecord:
    document_id: str
    revision_id: str
    source_key: str
    manifest_key: str
    classification: str
    title: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = ()
    visibility: str | None = None
    source_url: str | None = None
    mime_type: str | None = None
    content_hash: str | None = None
    manifest_hash: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    byte_length: int = 0
    content: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "category": self.category,
            "tags": list(self.tags),
            "visibility": self.visibility,
            "source_url": self.source_url,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True)
class DocumentAnalysis:
    document_id: str
    revision_count: int
    current_classification: str
    candidate_revision_id: str | None
    timestamp_tie: bool
    lexical_fallback: bool


@dataclass(frozen=True)
class Inventory:
    revisions: tuple[RevisionRecord, ...]
    documents: tuple[DocumentAnalysis, ...]
    counts: dict[str, int]


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        raise ValueError("invalid timestamp")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_body(response: dict[str, Any]) -> bytes:
    body = response.get("Body")
    try:
        data = body.read()
        if not isinstance(data, bytes):
            raise TypeError
        return data
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def scan_r2(client: ReadOnlyR2, bucket: str, prefix: str = "knowledge") -> Inventory:
    """List and GET only keys beneath prefix; source bytes are discarded after validation."""
    prefix = prefix.strip("/") + "/"
    listed: set[str] = set()
    token = None
    while True:
        args: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        for item in page.get("Contents", []):
            key = item.get("Key") if isinstance(item, dict) else None
            if isinstance(key, str) and key.startswith(prefix):
                listed.add(key)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token:
            raise RuntimeError("R2 listing is incomplete")

    groups: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    other: list[str] = []
    for key in sorted(listed):
        match = _KEY.fullmatch(key)
        if match and _DOC.fullmatch(match.group(1)) and _REV.fullmatch(match.group(2)):
            groups[(match.group(1), match.group(2))][match.group(3)] = key
        else:
            other.append(key)

    records: list[RevisionRecord] = []
    for (doc_id, rev_id), files in sorted(groups.items()):
        source_key = files.get("source.md", f"knowledge/{doc_id}/revisions/{rev_id}/source.md")
        manifest_key = files.get(
            "manifest.json", f"knowledge/{doc_id}/revisions/{rev_id}/manifest.json"
        )
        if "manifest.json" not in files:
            records.append(RevisionRecord(doc_id, rev_id, source_key, manifest_key, "SOURCE_ONLY"))
            continue
        manifest_bytes = _read_body(client.get_object(Bucket=bucket, Key=manifest_key))
        try:
            data = json.loads(manifest_bytes.decode("utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ValueError("malformed")
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            records.append(
                RevisionRecord(doc_id, rev_id, source_key, manifest_key, "MALFORMED_MANIFEST")
            )
            continue
        if data.get("document_id") != doc_id or data.get("revision_id") != rev_id:
            records.append(RevisionRecord(doc_id, rev_id, source_key, manifest_key, "ID_MISMATCH"))
            continue
        if data.get("object_key") != source_key:
            records.append(RevisionRecord(doc_id, rev_id, source_key, manifest_key, "KEY_MISMATCH"))
            continue
        try:
            created, updated = _timestamp(data.get("created_at")), _timestamp(
                data.get("updated_at")
            )
        except (TypeError, ValueError, OverflowError):
            records.append(
                RevisionRecord(doc_id, rev_id, source_key, manifest_key, "INVALID_TIMESTAMP")
            )
            continue
        title, category, tags = data.get("title"), data.get("category"), data.get("tags")
        visibility, mime, content_hash = (
            data.get("visibility"),
            data.get("mime_type"),
            data.get("content_hash"),
        )
        source_url = data.get("source_url")
        if (
            not isinstance(title, str)
            or not title.strip()
            or category not in CATEGORIES
            or not isinstance(tags, list)
            or any(not isinstance(x, str) for x in tags)
            or visibility not in VISIBILITIES
            or not isinstance(mime, str)
            or not mime.casefold().startswith("text/markdown")
            or not isinstance(content_hash, str)
            or not _HASH.fullmatch(content_hash)
            or (source_url is not None and not isinstance(source_url, str))
        ):
            records.append(
                RevisionRecord(doc_id, rev_id, source_key, manifest_key, "OTHER_INVALID")
            )
            continue
        if "source.md" not in files:
            records.append(
                RevisionRecord(doc_id, rev_id, source_key, manifest_key, "MANIFEST_ONLY")
            )
            continue
        source = _read_body(client.get_object(Bucket=bucket, Key=source_key))
        actual_hash = hashlib.sha256(source).hexdigest()
        if actual_hash != content_hash:
            records.append(
                RevisionRecord(doc_id, rev_id, source_key, manifest_key, "HASH_MISMATCH")
            )
            continue
        try:
            parsed = parse_markdown_document(f"{category}/{doc_id}.md", source.decode("utf-8"))
        except (KnowledgeDocumentError, UnicodeError):
            records.append(
                RevisionRecord(doc_id, rev_id, source_key, manifest_key, "OTHER_INVALID")
            )
            continue
        if (
            parsed.title != title.strip()
            or parsed.category != category
            or tuple(parsed.tags) != tuple(tags)
            or parsed.visibility != visibility
            or parsed.source_url != source_url
        ):
            records.append(
                RevisionRecord(doc_id, rev_id, source_key, manifest_key, "OTHER_INVALID")
            )
            continue
        records.append(
            RevisionRecord(
                doc_id,
                rev_id,
                source_key,
                manifest_key,
                "VALID_COMPLETE",
                title.strip(),
                category,
                tuple(tags),
                visibility,
                source_url,
                mime,
                content_hash,
                hashlib.sha256(manifest_bytes).hexdigest(),
                created,
                updated,
                len(source),
                parsed.content,
            )
        )

    for key in other:
        records.append(RevisionRecord("", "", key, key, "OTHER_INVALID"))
    by_doc: dict[str, list[RevisionRecord]] = defaultdict(list)
    for r in records:
        if r.document_id:
            by_doc[r.document_id].append(r)
    analyses = []
    for doc_id, revisions in sorted(by_doc.items()):
        complete = [r for r in revisions if r.classification == "VALID_COMPLETE"]
        # The runtime reader ignores malformed manifests, but selects a syntactically
        # valid manifest before hydrating source bytes. A selected broken source can
        # therefore make that document unavailable without falling back to older data.
        reader_blockers = [
            r
            for r in revisions
            if r.classification in {"MANIFEST_ONLY", "HASH_MISMATCH", "OTHER_INVALID"}
        ]
        if not complete or reader_blockers:
            analysis = DocumentAnalysis(
                doc_id, len(revisions), "CURRENT_INVALID", None, False, False
            )
        else:
            # The runtime reader selects from parseable manifests, then validates content on hydrate.
            ordered = sorted(
                complete, key=lambda r: (r.updated_at or "", r.created_at or "", r.revision_id)
            )
            selected = ordered[-1]
            ties = [
                r
                for r in complete
                if (r.updated_at, r.created_at) == (selected.updated_at, selected.created_at)
            ]
            # Equal timestamps are not semantic recency even though R2 reader resolves them lexically.
            ambiguous = len(ties) > 1
            analysis = DocumentAnalysis(
                doc_id,
                len(revisions),
                "CURRENT_AMBIGUOUS" if ambiguous else "CURRENT_UNAMBIGUOUS",
                selected.revision_id,
                ambiguous,
                ambiguous,
            )
        analyses.append(analysis)
    counts = Counter(r.classification for r in records)
    for label in (
        "VALID_COMPLETE",
        "SOURCE_ONLY",
        "MANIFEST_ONLY",
        "HASH_MISMATCH",
        "ID_MISMATCH",
        "KEY_MISMATCH",
        "MALFORMED_MANIFEST",
        "INVALID_TIMESTAMP",
        "OTHER_INVALID",
    ):
        counts.setdefault(label, 0)
    return Inventory(tuple(records), tuple(analyses), dict(counts))


def import_shadow(inventory: Inventory, connect: Any) -> dict[str, int]:
    """Insert validated historical revisions only; never creates runtime intents/jobs."""
    from psycopg.types.json import Jsonb

    analysis = {d.document_id: d for d in inventory.documents}
    grouped: dict[str, list[RevisionRecord]] = defaultdict(list)
    for record in inventory.revisions:
        if record.classification == "VALID_COMPLETE":
            grouped[record.document_id].append(record)
    inserted_docs = inserted_revs = 0

    def db_time(value: str) -> datetime:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)

    with connect() as conn, conn.cursor() as cur:
        for doc_id, revisions in sorted(grouped.items()):
            projection = max(
                revisions, key=lambda r: (r.updated_at or "", r.created_at or "", r.revision_id)
            )
            info = analysis[doc_id]
            state = (
                "ACTIVE" if info.current_classification == "CURRENT_UNAMBIGUOUS" else "NEEDS_REVIEW"
            )
            current = info.candidate_revision_id if state == "ACTIVE" else None
            created = db_time(min(r.created_at for r in revisions if r.created_at))
            updated = db_time(projection.updated_at)
            cur.execute(
                """INSERT INTO documents(id,current_revision_id,title,category,visibility,source_url,state,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (
                    doc_id,
                    current,
                    projection.title,
                    projection.category,
                    projection.visibility,
                    projection.source_url,
                    state,
                    created,
                    updated,
                ),
            )
            inserted_docs += cur.rowcount
            cur.execute(
                "SELECT current_revision_id,title,category,visibility,source_url,state,created_at,updated_at FROM documents WHERE id=%s",
                (doc_id,),
            )
            got = cur.fetchone()
            expected = (
                current,
                projection.title,
                projection.category,
                projection.visibility,
                projection.source_url,
                state,
                created,
                updated,
            )
            if got != expected:
                raise ValueError("Existing shadow document conflicts with R2 inventory")
            for r in sorted(revisions, key=lambda x: x.revision_id):
                metadata = r.metadata()
                cur.execute(
                    """INSERT INTO revisions(document_id,revision_id,parent_revision_id,content_hash,byte_length,
                    source_object_key,manifest_object_key,manifest_hash,mime_type,metadata_snapshot,manifest_schema_version,registered_at)
                    VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,1,%s) ON CONFLICT (document_id,revision_id) DO NOTHING""",
                    (
                        doc_id,
                        r.revision_id,
                        r.content_hash,
                        r.byte_length,
                        r.source_key,
                        r.manifest_key,
                        r.manifest_hash,
                        r.mime_type,
                        Jsonb(metadata),
                        db_time(r.created_at),
                    ),
                )
                inserted_revs += cur.rowcount
                cur.execute(
                    "SELECT content_hash,byte_length,source_object_key,manifest_object_key,manifest_hash,mime_type,metadata_snapshot,manifest_schema_version,registered_at FROM revisions WHERE document_id=%s AND revision_id=%s",
                    (doc_id, r.revision_id),
                )
                rev_got = cur.fetchone()
                rev_expected = (
                    r.content_hash,
                    r.byte_length,
                    r.source_key,
                    r.manifest_key,
                    r.manifest_hash,
                    r.mime_type,
                    metadata,
                    1,
                    db_time(r.created_at),
                )
                if rev_got != rev_expected:
                    raise ValueError("Existing shadow revision conflicts with R2 inventory")
            tags: dict[str, str] = {}
            for tag in projection.tags:
                normalized = normalize_tag(tag)
                if normalized:
                    tags.setdefault(normalized, tag.strip())
            for normalized, display in sorted(tags.items()):
                cur.execute(
                    "INSERT INTO tags(normalized_name,display_name) VALUES (%s,%s) ON CONFLICT(normalized_name) DO NOTHING",
                    (normalized, display),
                )
                cur.execute(
                    "SELECT id,display_name FROM tags WHERE normalized_name=%s", (normalized,)
                )
                tag_id, _ = cur.fetchone()
                cur.execute(
                    "INSERT INTO document_tags(document_id,tag_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (doc_id, tag_id),
                )
        cur.execute("SELECT count(*) FROM documents")
        docs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM revisions")
        revs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tags")
        tag_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM document_tags")
        links = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM processing_jobs")
        jobs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM revision_write_intents")
        intents = cur.fetchone()[0]
    if jobs or intents:
        raise ValueError("Shadow import unexpectedly created operational write state")
    return {
        "inserted_documents": inserted_docs,
        "inserted_revisions": inserted_revs,
        "documents": docs,
        "revisions": revs,
        "tags": tag_count,
        "document_tags": links,
        "processing_jobs": jobs,
        "revision_write_intents": intents,
    }


def shadow_snapshot(connect: Any) -> tuple[Any, ...]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id,current_revision_id,title,category,visibility,source_url,state,created_at,updated_at FROM documents ORDER BY id"
        )
        docs = tuple(cur.fetchall())
        cur.execute(
            "SELECT document_id,revision_id,content_hash,byte_length,source_object_key,manifest_object_key,manifest_hash,mime_type,metadata_snapshot,manifest_schema_version,registered_at FROM revisions ORDER BY document_id,revision_id"
        )
        revs = tuple(cur.fetchall())
        cur.execute("SELECT normalized_name,display_name FROM tags ORDER BY normalized_name")
        tags = tuple(cur.fetchall())
        cur.execute(
            "SELECT dt.document_id,t.normalized_name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id ORDER BY dt.document_id,t.normalized_name"
        )
        links = tuple(cur.fetchall())
    return docs, revs, tags, links
