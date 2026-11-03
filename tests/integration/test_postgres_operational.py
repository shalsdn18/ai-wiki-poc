from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid

import pytest
from psycopg.conninfo import conninfo_to_dict

psycopg = pytest.importorskip("psycopg")
TEST_DSN = os.getenv("POSTGRES_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="POSTGRES_TEST_DATABASE_URL is not configured")


@pytest.fixture
def pg_store():
    from engine.postgres_operational import PostgresOperationalStore, migrate

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
        migrate(connect)
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
