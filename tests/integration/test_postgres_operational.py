from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import uuid
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

psycopg = pytest.importorskip("psycopg")
TEST_DSN = os.getenv("POSTGRES_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="POSTGRES_TEST_DATABASE_URL is not configured")


@pytest.fixture
def pg_store():
    from engine.postgres_operational import MIGRATION_DIR, PostgresOperationalStore, migrate

    database_name = conninfo_to_dict(TEST_DSN).get("dbname", "").casefold()
    if "test" not in database_name:
        pytest.fail("POSTGRES_TEST_DATABASE_URL must name a dedicated database containing 'test'")
    schema = "codex_test_" + uuid.uuid4().hex
    admin = psycopg.connect(TEST_DSN, autocommit=True)
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(TEST_DSN)
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        return conn

    try:
        expected_versions = [
            int(path.name.split("_", 1)[0])
            for path in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9]_*.sql"))
        ]
        assert migrate(connect) == expected_versions
        yield PostgresOperationalStore(connect), connect
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def _source(document_id: str, title: str, body: str) -> bytes:
    return (
        "---\n"
        f"document_id: {document_id}\n"
        f"title: {title}\n"
        "category: ai\n"
        "tags: [storage]\n"
        'created: "2026-01-01"\n'
        'updated: "2026-01-01"\n'
        "visibility: private\n"
        "---\n\n"
        f"{body}\n"
    ).encode()


def _request(document_id: str, key: str, expected: str | None, title: str, body: str):
    from engine.postgres_operational import WriteRequest

    return WriteRequest(
        key,
        document_id,
        expected,
        _source(document_id, title, body),
        {"title": title, "category": "ai", "tags": ["storage"], "visibility": "private"},
    )


def _verified(intent, title: str, body: str):
    from engine.postgres_operational import VerifiedRevision

    source = _source(intent.document_id, title, body)
    manifest = json.dumps(
        {
            "schema_version": 1,
            "document_id": intent.document_id,
            "revision_id": intent.revision_id,
            "title": title,
            "category": "ai",
            "tags": ["storage"],
            "visibility": "private",
            "source_url": None,
            "mime_type": "text/markdown; charset=utf-8",
            "content_hash": hashlib.sha256(source).hexdigest(),
            "object_key": intent.source_key,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        separators=(",", ":"),
    ).encode()
    return VerifiedRevision(source, manifest, "text/markdown; charset=utf-8")


def test_migrations_finalize_constraints_idempotency_and_cas_race(pg_store):
    from engine.postgres_operational import (
        IdempotencyConflict,
        OperationalStoreError,
        RevisionConflict,
        migrate,
    )

    store, connect = pg_store
    assert migrate(connect) == []
    document_id = uuid.uuid4().hex + uuid.uuid4().hex
    first_request = _request(document_id, "create-1", None, "First", "one")
    first_intent = store.reserve_intent(first_request)
    replay = store.reserve_intent(first_request)
    assert replay.replay and replay.id == first_intent.id
    with pytest.raises(IdempotencyConflict):
        store.reserve_intent(_request(document_id, "create-1", None, "First", "changed"))
    fingerprint = hashlib.sha256(b"pipeline-v1").hexdigest()
    first_job = store.finalize_verified_revision(
        first_intent, _verified(first_intent, "First", "one"), pipeline_fingerprint=fingerprint
    )
    assert (
        store.finalize_verified_revision(
            first_intent, _verified(first_intent, "First", "one"), pipeline_fingerprint=fingerprint
        )
        == first_job
    )
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_revision_id FROM documents WHERE id=%s", (document_id,))
            assert cur.fetchone()[0] == first_intent.revision_id
            cur.execute("SELECT state FROM revision_write_intents WHERE id=%s", (first_intent.id,))
            assert cur.fetchone()[0] == "COMMITTED"
            cur.execute("SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM processing_jobs WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()

    # Force failure after revision/pointer writes have begun, at durable job
    # insertion, and prove PostgreSQL rolls the entire finalize transaction back.
    rollback_intent = store.reserve_intent(
        _request(document_id, "rollback-at-job", first_intent.revision_id, "Rollback", "rollback")
    )
    conn = connect()
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "CREATE FUNCTION fail_job_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'test failure'; END; $$"
            )
            cur.execute(
                "CREATE TRIGGER fail_job_insert BEFORE INSERT ON processing_jobs "
                "FOR EACH ROW EXECUTE FUNCTION fail_job_insert()"
            )
    finally:
        conn.close()

    with pytest.raises(OperationalStoreError):
        store.finalize_verified_revision(
            rollback_intent,
            _verified(rollback_intent, "Rollback", "rollback"),
            pipeline_fingerprint=fingerprint,
        )
    conn = connect()
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT current_revision_id FROM documents WHERE id=%s", (document_id,))
            assert cur.fetchone()[0] == first_intent.revision_id
            cur.execute("SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM processing_jobs WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT state FROM revision_write_intents WHERE id=%s", (rollback_intent.id,)
            )
            assert cur.fetchone()[0] == "PREPARED"
            cur.execute("DROP TRIGGER fail_job_insert ON processing_jobs")
            cur.execute("DROP FUNCTION fail_job_insert()")
    finally:
        conn.close()
    store.finalize_verified_revision(
        rollback_intent,
        _verified(rollback_intent, "Rollback", "rollback"),
        pipeline_fingerprint=fingerprint,
    )

    # A current pointer to another document's revision must fail the composite FK.
    other_id = uuid.uuid4().hex + uuid.uuid4().hex
    conn = connect()
    try:
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation
        ), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents(id,current_revision_id,title,category,visibility,state) "
                "VALUES (%s,%s,'Other','ai','private','ACTIVE')",
                (other_id, first_intent.revision_id),
            )
    finally:
        conn.close()

    base = rollback_intent.revision_id
    left = store.reserve_intent(_request(document_id, "update-left", base, "Left", "left"))
    right = store.reserve_intent(_request(document_id, "update-right", base, "Right", "right"))
    barrier = threading.Barrier(2)
    outcomes = []
    backend_pids = []
    pid_lock = threading.Lock()
    connection_barrier = threading.Barrier(2)
    original_factory = store.connection_factory

    def tracking_factory():
        conn = original_factory()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        conn.commit()
        with pid_lock:
            backend_pids.append(pid)
            race_connection = len(backend_pids) <= 2
        if race_connection:
            connection_barrier.wait(timeout=10)
        return conn

    store.connection_factory = tracking_factory

    def finalize(intent, title, body):
        barrier.wait()
        try:
            job = store.finalize_verified_revision(
                intent, _verified(intent, title, body), pipeline_fingerprint=fingerprint
            )
            outcomes.append(("ok", job))
        except RevisionConflict:
            outcomes.append(("conflict", None))

    threads = [
        threading.Thread(target=finalize, args=(left, "Left", "left")),
        threading.Thread(target=finalize, args=(right, "Right", "right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(item[0] for item in outcomes) == ["conflict", "ok"]
    assert len(set(backend_pids)) >= 2
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_revision_id FROM documents WHERE id=%s", (document_id,))
            current = cur.fetchone()[0]
            assert current in {left.revision_id, right.revision_id}
            cur.execute("SELECT count(*) FROM processing_jobs WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 4
            losing = right if current == left.revision_id else left
            cur.execute("SELECT state FROM revision_write_intents WHERE id=%s", (losing.id,))
            assert cur.fetchone()[0] == "CONFLICT"
            cur.execute("SELECT count(*) FROM processing_jobs WHERE id=%s", (first_job,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_checksum_schema_constraints_and_immutability(pg_store):
    from engine.postgres_operational import migrate

    store, connect = pg_store
    assert migrate(connect) == []
    migration_paths = sorted(
        (Path(__file__).resolve().parents[2] / "migrations").glob("[0-9][0-9][0-9]_*.sql")
    )
    expected_migrations = [
        (
            int(path.name.split("_", 1)[0]),
            hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
        )
        for path in migration_paths
    ]
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version,checksum FROM schema_migrations ORDER BY version")
            assert [(row[0], row[1].strip()) for row in cur.fetchall()] == expected_migrations
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
            )
            tables = {row[0] for row in cur.fetchall()}
            assert {
                "schema_migrations",
                "documents",
                "revisions",
                "revision_write_intents",
                "tags",
                "document_tags",
                "relations",
                "processing_jobs",
                "processing_attempts",
            } <= tables
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='revision_write_intents'"
            )
            assert "result_revision_id" in {row[0] for row in cur.fetchall()}
            cur.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='revision_write_intents_state_check'"
            )
            assert "NOOP" in cur.fetchone()[0]
            cur.execute(
                "SELECT conname FROM pg_constraint WHERE connamespace=current_schema()::regnamespace"
            )
            constraints = {row[0] for row in cur.fetchall()}
            assert "documents_current_revision_fk" in constraints
            assert "documents_current_indexed_revision_fk" in constraints
            assert "revision_write_intents_result_revision_fk" in constraints
            assert "revision_write_intents_result_state_check" in constraints
            assert "revision_write_intents_state_check" in constraints
            cur.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid='revisions'::regclass AND NOT tgisinternal"
            )
            assert {row[0] for row in cur.fetchall()} == {"revisions_immutable"}
    finally:
        conn.close()

    doc_id = uuid.uuid4().hex + uuid.uuid4().hex
    intent = store.reserve_intent(_request(doc_id, "immutability", None, "Immutable", "bytes"))
    fingerprint = hashlib.sha256(b"pipeline-immutable").hexdigest()
    store.finalize_verified_revision(
        intent, _verified(intent, "Immutable", "bytes"), pipeline_fingerprint=fingerprint
    )
    conn = connect()
    try:
        with pytest.raises(psycopg.errors.RaiseException), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE revisions SET content_hash=%s WHERE document_id=%s AND revision_id=%s",
                ("0" * 64, doc_id, intent.revision_id),
            )
        with pytest.raises(psycopg.errors.RaiseException), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM revisions WHERE document_id=%s AND revision_id=%s",
                (doc_id, intent.revision_id),
            )

        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
            cur.execute("UPDATE documents SET category='invalid' WHERE id=%s", (doc_id,))
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE processing_jobs SET state='NOT_A_STATE' WHERE document_id=%s", (doc_id,)
            )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE processing_jobs SET attempt_count=-1 WHERE document_id=%s", (doc_id,)
            )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processing_attempts(job_id,attempt_number,started_at,embedding_dimensions) "
                "SELECT id,99,now(),0 FROM processing_jobs WHERE document_id=%s LIMIT 1",
                (doc_id,),
            )

        with pytest.raises(
            psycopg.errors.ForeignKeyViolation
        ), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processing_jobs(id,document_id,revision_id,pipeline_fingerprint,state) "
                "VALUES (%s,%s,%s,%s,'QUEUED')",
                (uuid.uuid4(), doc_id, "f" * 64, fingerprint),
            )
        missing_doc_id = "c" * 64
        missing_revision_id = "d" * 64
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation
        ), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO revisions(document_id,revision_id,content_hash,byte_length,source_object_key,"
                "manifest_object_key,manifest_hash,mime_type,metadata_snapshot,manifest_schema_version,registered_at) "
                "VALUES (%s,%s,%s,1,%s,%s,%s,'text/markdown',%s::jsonb,1,now())",
                (
                    missing_doc_id,
                    missing_revision_id,
                    "1" * 64,
                    f"knowledge/{missing_doc_id}/revisions/{missing_revision_id}/source.md",
                    f"knowledge/{missing_doc_id}/revisions/{missing_revision_id}/manifest.json",
                    "2" * 64,
                    json.dumps(
                        {"title": "Missing", "category": "ai", "visibility": "private", "tags": []}
                    ),
                ),
            )
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation
        ), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processing_attempts(job_id,attempt_number,started_at) VALUES (%s,1,now())",
                (uuid.uuid4(),),
            )
        with pytest.raises(
            psycopg.errors.UniqueViolation
        ), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processing_jobs(id,document_id,revision_id,pipeline_fingerprint,state) "
                "SELECT %s,document_id,revision_id,pipeline_fingerprint,'QUEUED' FROM processing_jobs "
                "WHERE document_id=%s",
                (uuid.uuid4(), doc_id),
            )
        with pytest.raises(
            psycopg.errors.UniqueViolation
        ), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO revision_write_intents(id,idempotency_key,request_digest,document_id,revision_id,"
                "source_object_key,manifest_object_key,content_hash,metadata_snapshot,state) "
                "SELECT %s,idempotency_key,request_digest,document_id,%s,%s,%s,content_hash,metadata_snapshot,'PREPARED' "
                "FROM revision_write_intents WHERE id=%s",
                (
                    uuid.uuid4(),
                    "e" * 64,
                    f"knowledge/{doc_id}/revisions/{'e' * 64}/source.md",
                    f"knowledge/{doc_id}/revisions/{'e' * 64}/manifest.json",
                    intent.id,
                ),
            )

        # Valid transition and a valid attempt with retry/lease fields.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE processing_jobs SET state='RUNNING',lease_owner='integration-test',"
                "lease_expires_at=now()+interval '1 minute',heartbeat_at=now() WHERE document_id=%s",
                (doc_id,),
            )
            cur.execute(
                "INSERT INTO processing_attempts(job_id,attempt_number,started_at,source_content_hash) "
                "SELECT id,1,now(),%s FROM processing_jobs WHERE document_id=%s",
                (hashlib.sha256(_source(doc_id, "Immutable", "bytes")).hexdigest(), doc_id),
            )
            cur.execute(
                "UPDATE processing_jobs SET state='RETRY_WAIT',attempt_count=1,max_attempts=3,"
                "next_attempt_at=now()+interval '5 seconds',lease_owner=NULL,lease_expires_at=NULL "
                "WHERE document_id=%s",
                (doc_id,),
            )
            cur.execute(
                "UPDATE processing_jobs SET state='SUCCEEDED',lease_owner=NULL,lease_expires_at=NULL "
                "WHERE document_id=%s",
                (doc_id,),
            )
            cur.execute(
                "SELECT state,attempt_count FROM processing_jobs WHERE document_id=%s", (doc_id,)
            )
            assert cur.fetchone() == ("SUCCEEDED", 1)
    finally:
        conn.close()


def test_tombstone_retains_current_revision_without_external_storage(pg_store):
    from engine.postgres_operational import RevisionConflict

    store, connect = pg_store
    doc_id = uuid.uuid4().hex + uuid.uuid4().hex
    intent = store.reserve_intent(_request(doc_id, "tombstone", None, "Keep", "content"))
    fingerprint = hashlib.sha256(b"pipeline-tombstone").hexdigest()
    store.finalize_verified_revision(
        intent, _verified(intent, "Keep", "content"), pipeline_fingerprint=fingerprint
    )
    pending_update = store.reserve_intent(
        _request(doc_id, "update-racing-tombstone", intent.revision_id, "Pending", "new content")
    )
    store.tombstone_document(doc_id, intent.revision_id)
    with pytest.raises(RevisionConflict):
        store.finalize_verified_revision(
            pending_update,
            _verified(pending_update, "Pending", "new content"),
            pipeline_fingerprint=fingerprint,
        )
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state,current_revision_id,deleted_at FROM documents WHERE id=%s", (doc_id,)
            )
            state, current_revision_id, deleted_at = cur.fetchone()
            assert state == "DELETED"
            assert current_revision_id == intent.revision_id
            assert deleted_at is not None
            cur.execute("SELECT count(*) FROM revisions WHERE document_id=%s", (doc_id,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_tombstone_and_finalize_never_reactivates_a_committed_tombstone(pg_store):
    from concurrent.futures import ThreadPoolExecutor

    from engine.postgres_operational import PostgresOperationalStore, RevisionConflict

    store, connect = pg_store
    document_id = uuid.uuid4().hex + uuid.uuid4().hex
    first = store.reserve_intent(_request(document_id, "race-create", None, "First", "one"))
    fingerprint = hashlib.sha256(b"tombstone-finalize-race").hexdigest()
    store.finalize_verified_revision(
        first, _verified(first, "First", "one"), pipeline_fingerprint=fingerprint
    )
    pending = store.reserve_intent(
        _request(document_id, "race-update", first.revision_id, "Second", "two")
    )
    tracked_pids = set()
    pid_lock = threading.Lock()

    def tracked_connect():
        connection = connect()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
        connection.commit()
        with pid_lock:
            tracked_pids.add(pid)
        return connection

    concurrent_store = PostgresOperationalStore(tracked_connect)
    barrier = threading.Barrier(2)

    def tombstone():
        barrier.wait()
        try:
            concurrent_store.tombstone_document(document_id, first.revision_id)
            return "TOMBSTONE_COMMITTED"
        except RevisionConflict:
            return "TOMBSTONE_CONFLICT"

    def finalize():
        barrier.wait()
        try:
            concurrent_store.finalize_verified_revision(
                pending,
                _verified(pending, "Second", "two"),
                pipeline_fingerprint=fingerprint,
            )
            return "FINALIZE_COMMITTED"
        except RevisionConflict:
            return "FINALIZE_CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = {pool.submit(tombstone), pool.submit(finalize)}
        results = {future.result() for future in outcomes}
    assert len(tracked_pids) >= 2
    assert results in (
        {"TOMBSTONE_COMMITTED", "FINALIZE_CONFLICT"},
        {"FINALIZE_COMMITTED", "TOMBSTONE_CONFLICT"},
    )

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state,current_revision_id FROM documents WHERE id=%s", (document_id,)
            )
            state, current_revision_id = cur.fetchone()
            cur.execute("SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,))
            revision_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM processing_jobs WHERE document_id=%s", (document_id,))
            job_count = cur.fetchone()[0]
            if "TOMBSTONE_COMMITTED" in results:
                assert state == "DELETED"
                assert current_revision_id == first.revision_id
                assert revision_count == 1
                assert job_count == 1
            else:
                assert state == "ACTIVE"
                assert current_revision_id == pending.revision_id
                assert revision_count == 2
                assert job_count == 2
    finally:
        conn.close()


class _ConditionalFailure(Exception):
    def __init__(self):
        self.response = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }
        super().__init__("conditional write rejected")


class _FakeR2:
    supports_if_none_match = True

    def __init__(self):
        self.objects = {}
        self.fail_put_suffix = None
        self.corrupt_get_suffix = None

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):
        assert Bucket == "test-bucket"
        assert IfNoneMatch == "*"
        if self.fail_put_suffix and Key.endswith(self.fail_put_suffix):
            raise RuntimeError("injected R2 write failure")
        if Key in self.objects:
            raise _ConditionalFailure()
        self.objects[Key] = bytes(Body)
        return {}

    def get_object(self, *, Bucket, Key):
        assert Bucket == "test-bucket"
        body = self.objects[Key]
        if self.corrupt_get_suffix and Key.endswith(self.corrupt_get_suffix):
            body += b"tampered"
        return {"Body": io.BytesIO(body)}


def test_knowledge_writer_create_update_noop_retry_and_tombstone(pg_store):
    from engine.knowledge_writer import KnowledgeWriteInput, KnowledgeWriter
    from engine.postgres_operational import RevisionConflict

    store, connect = pg_store
    writer = KnowledgeWriter(store, _FakeR2(), "test-bucket")
    document_id = uuid.uuid4().hex + uuid.uuid4().hex
    first_input = KnowledgeWriteInput(
        "Canonical note",
        "ai",
        ["storage"],
        "private",
        "Revision one",
        operation_date="2026-01-01",
    )
    first = writer.create_document(document_id, "writer-create-1", first_input)
    assert first.operation_result == "CREATED"
    assert first.current_revision_id == first.revision_id
    assert first.processing_status == "QUEUED"
    first_retry = writer.create_document(document_id, "writer-create-1", first_input)
    assert first_retry.revision_id == first.revision_id
    assert first_retry.processing_job_id == first.processing_job_id
    from engine.postgres_operational import IdempotencyConflict

    with pytest.raises(IdempotencyConflict):
        writer.create_document(
            document_id,
            "writer-create-1",
            KnowledgeWriteInput(
                "Canonical note",
                "ai",
                ["storage"],
                "private",
                "different bytes",
                operation_date="2026-01-01",
            ),
        )

    second_input = KnowledgeWriteInput(
        "Canonical note",
        "ai",
        ["storage"],
        "private",
        "Revision two",
        operation_date="2026-01-02",
    )
    second = writer.update_document(document_id, first.revision_id, "writer-update-1", second_input)
    assert second.operation_result == "UPDATED"
    assert second.revision_id != first.revision_id
    retry = writer.update_document(document_id, first.revision_id, "writer-update-1", second_input)
    assert retry.revision_id == second.revision_id
    assert retry.processing_job_id == second.processing_job_id

    metadata_update = writer.update_document(
        document_id,
        second.revision_id,
        "writer-metadata-1",
        KnowledgeWriteInput(
            "Canonical note",
            "ai",
            ["storage", "metadata"],
            "private",
            "Revision two",
            operation_date="2026-01-03",
        ),
    )
    assert metadata_update.operation_result == "UPDATED"
    assert metadata_update.revision_id != second.revision_id

    no_op = writer.update_document(
        document_id,
        metadata_update.revision_id,
        "writer-noop-1",
        KnowledgeWriteInput(
            "Canonical note",
            "ai",
            ["storage", "metadata"],
            "private",
            "Revision two",
            operation_date="2026-01-04",
        ),
    )
    assert no_op.operation_result == "NOOP"
    assert no_op.current_revision_id == metadata_update.revision_id
    assert no_op.processing_job_id is None

    with pytest.raises(RevisionConflict):
        writer.update_document(document_id, first.revision_id, "writer-stale-1", second_input)
    assert (
        writer.delete_document(document_id, metadata_update.revision_id).operation_result
        == "DELETED"
    )
    assert writer.list_documents() == []
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT count(*) FROM processing_jobs WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT state FROM documents WHERE id=%s", (document_id,))
            assert cur.fetchone()[0] == "DELETED"
    finally:
        conn.close()


@pytest.mark.parametrize("failure_suffix", ["/source.md", "/manifest.json"])
def test_knowledge_writer_retries_partial_r2_write_with_same_revision(pg_store, failure_suffix):
    from engine.knowledge_writer import KnowledgeWriteInput, KnowledgeWriter
    from engine.r2_revision_objects import RevisionObjectUnavailable

    store, _ = pg_store
    r2 = _FakeR2()
    writer = KnowledgeWriter(store, r2, "test-bucket")
    document_id = uuid.uuid4().hex + uuid.uuid4().hex
    request = KnowledgeWriteInput(
        "Retryable write", "misc", [], "private", "retry me", operation_date="2026-01-01"
    )
    r2.fail_put_suffix = failure_suffix
    with pytest.raises(RevisionObjectUnavailable):
        writer.create_document(document_id, "retryable-r2-write", request)
    intent_before, _ = store.get_intent_by_idempotency_key("retryable-r2-write")
    r2.fail_put_suffix = None
    completed = writer.create_document(document_id, "retryable-r2-write", request)
    assert completed.revision_id == intent_before.revision_id
    assert completed.processing_status == "QUEUED"


@pytest.mark.parametrize("corrupt_suffix", ["/source.md", "/manifest.json"])
def test_knowledge_writer_quarantines_readback_mismatch_without_finalize(pg_store, corrupt_suffix):
    from engine.knowledge_writer import KnowledgeWriteInput, KnowledgeWriter
    from engine.r2_revision_objects import RevisionObjectConflict

    store, connect = pg_store
    r2 = _FakeR2()
    writer = KnowledgeWriter(store, r2, "test-bucket")
    document_id = uuid.uuid4().hex + uuid.uuid4().hex
    r2.corrupt_get_suffix = corrupt_suffix
    with pytest.raises(RevisionObjectConflict):
        writer.create_document(
            document_id,
            "readback-mismatch",
            KnowledgeWriteInput("Mismatch", "misc", [], "private", "expected"),
        )
    intent, _ = store.get_intent_by_idempotency_key("readback-mismatch")
    assert intent.state == "QUARANTINED"
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT state FROM documents WHERE id=%s", (document_id,))
            assert cur.fetchone()[0] == "PENDING"
    finally:
        conn.close()


def test_knowledge_writer_reuses_completed_r2_pair_after_finalize_failure(pg_store, monkeypatch):
    from engine.knowledge_writer import KnowledgeWriteInput, KnowledgeWriter
    from engine.postgres_operational import OperationalStoreError

    store, _ = pg_store
    r2 = _FakeR2()
    writer = KnowledgeWriter(store, r2, "test-bucket")
    document_id = uuid.uuid4().hex + uuid.uuid4().hex
    request = KnowledgeWriteInput(
        "Finalize retry", "misc", [], "private", "same revision", operation_date="2026-01-01"
    )
    original = store.finalize_verified_revision
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalStoreError("injected finalize failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "finalize_verified_revision", fail_once)
    with pytest.raises(OperationalStoreError):
        writer.create_document(document_id, "finalize-retry", request)
    intent, _ = store.get_intent_by_idempotency_key("finalize-retry")
    keys_after_failure = set(r2.objects)
    result = writer.create_document(document_id, "finalize-retry", request)
    assert result.revision_id == intent.revision_id
    assert set(r2.objects) == keys_after_failure
