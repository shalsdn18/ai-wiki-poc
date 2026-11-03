"""Controlled live R2 + disposable PostgreSQL qualification for the Knowledge writer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXPECTED_BUCKET = "ai-wiki-documents"


class TrackedClient:
    """Track only conditional R2 PUTs that returned success in this process."""

    def __init__(self, client):
        self._client = client
        self.created_keys: set[str] = set()
        self.successful_put_count = 0
        self.put_conditions: list[str | None] = []
        self.delete_keys: list[str] = []

    def __getattr__(self, name):
        return getattr(self._client, name)

    def put_object(self, **kwargs):
        self.put_conditions.append(kwargs.get("IfNoneMatch"))
        response = self._client.put_object(**kwargs)
        if kwargs.get("IfNoneMatch") == "*" and kwargs.get("Key", "").startswith("knowledge/"):
            self.successful_put_count += 1
            self.created_keys.add(kwargs["Key"])
        return response

    def delete_object(self, **kwargs):
        self.delete_keys.append(kwargs["Key"])
        return self._client.delete_object(**kwargs)


def _head_is_absent(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return False
    except Exception as exc:  # noqa: BLE001 - inspect only status/code, never log details
        response = getattr(exc, "response", {})
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
        return (
            error.get("Code") in {"404", "NoSuchKey", "NotFound"}
            or metadata.get("HTTPStatusCode") == 404
        )


def fail(message: str) -> None:
    print(f"LIVE WRITER E2E: FAIL — {message}")
    raise SystemExit(1)


def local_test_dsn() -> str:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    dsn = os.getenv("POSTGRES_TEST_DATABASE_URL", "").strip()
    if not dsn:
        fail("POSTGRES_TEST_DATABASE_URL is not configured")
    try:
        fields = conninfo_to_dict(dsn)
    except (TypeError, ValueError):
        fail("test PostgreSQL connection settings are invalid")
    database = fields.get("dbname", "").casefold()
    host = fields.get("host", "localhost").strip("[]").casefold()
    if "test" not in database or host not in {"localhost", "127.0.0.1", "::1"}:
        fail("test PostgreSQL must be a localhost database with 'test' in its name")
    return dsn


def validate_r2_environment() -> None:
    endpoint = os.getenv("KNOWLEDGE_R2_ENDPOINT_URL", "").strip()
    account_id = os.getenv("KNOWLEDGE_R2_ACCOUNT_ID", "").strip()
    if not endpoint and not account_id:
        fail("Knowledge R2 endpoint settings are missing")
    if endpoint:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            fail("Knowledge R2 endpoint is not a safe HTTPS endpoint")
    elif any(not (character.isalnum() or character == "-") for character in account_id):
        fail("Knowledge R2 account setting is invalid")
    if os.getenv("KNOWLEDGE_R2_BUCKET_NAME", "").strip() != EXPECTED_BUCKET:
        fail("Knowledge R2 bucket is outside the approved target")
    if os.getenv("KNOWLEDGE_R2_PREFIX", "").strip("/") != "knowledge":
        fail("Knowledge R2 prefix is outside the isolated namespace")
    if (os.getenv("KNOWLEDGE_R2_REGION", "auto").strip() or "auto") != "auto":
        fail("Knowledge R2 region is not the configured R2 region")
    if not all(
        os.getenv(name, "").strip()
        for name in ("KNOWLEDGE_R2_ACCESS_KEY_ID", "KNOWLEDGE_R2_SECRET_ACCESS_KEY")
    ):
        fail("Knowledge R2 credentials are not configured")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-live-r2",
        action="store_true",
        help="authorize a unique isolated Knowledge write and cleanup in the configured R2 bucket",
    )
    args = parser.parse_args()
    if not args.confirm_live_r2:
        fail("Pass --confirm-live-r2 to authorize the isolated live test")

    import psycopg

    import engine.config  # noqa: F401 - loads local ignored environment settings
    from engine.knowledge_writer import KnowledgeWriteInput, KnowledgeWriter
    from engine.postgres_operational import PostgresOperationalStore, migrate
    from engine.r2_knowledge_store import get_r2_knowledge_store

    dsn = local_test_dsn()
    validate_r2_environment()
    r2_store = get_r2_knowledge_store()
    if r2_store.bucket != EXPECTED_BUCKET or r2_store.prefix != "knowledge":
        fail("Knowledge R2 bucket or prefix is outside the approved isolated target")
    try:
        r2_store.stats()
    except Exception:  # noqa: BLE001 - keep endpoint and authentication details private
        fail("Knowledge R2 read-only authentication preflight failed (details withheld)")
    print("SETTINGS: present; values withheld")
    print("R2 READ PREFLIGHT: PASS")

    schema = "codex_live_writer_test_" + uuid.uuid4().hex
    admin = psycopg.connect(dsn, autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        connection = psycopg.connect(dsn)
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    tracked = TrackedClient(r2_store.client)
    run_id = uuid.uuid4().hex
    document_id = uuid.uuid4().hex + uuid.uuid4().hex
    marker = f"AIWIKI_WRITER_LIVE_E2E_{run_id.upper()}"
    passed = False
    cleanup_ok = True
    try:
        writer = KnowledgeWriter(
            PostgresOperationalStore(connect), tracked, r2_store.bucket, r2_store.prefix
        )
        versions = migrate(connect)
        if versions != [1, 2] or migrate(connect) != []:
            fail("clean migrations or migration re-run did not match expected versions")

        first_input = KnowledgeWriteInput(
            f"Isolated writer E2E {run_id[:8]}",
            "misc",
            ["e2e", "isolated"],
            "private",
            f"{marker}\nRevision one.",
            operation_date="2026-01-01",
        )
        first = writer.create_document(document_id, f"{run_id}:create", first_input)
        if first.operation_result != "CREATED" or first.processing_status != "QUEUED":
            fail("initial revision did not finalize and queue processing")
        if not first.processing_job_id:
            fail("initial revision has no processing job reference")
        first_retry = writer.create_document(document_id, f"{run_id}:create", first_input)
        if first_retry.revision_id != first.revision_id:
            fail("create idempotency did not return the reserved revision")
        if len(tracked.created_keys) != 2 or tracked.successful_put_count != 2:
            fail("create retry wrote an unexpected number of R2 objects")
        if any(condition != "*" for condition in tracked.put_conditions):
            fail("an R2 writer request was not a conditional create-only PUT")
        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT source_object_key,manifest_object_key FROM revisions "
                    "WHERE document_id=%s AND revision_id=%s",
                    (document_id, first.revision_id),
                )
                r1_source, r1_manifest = cursor.fetchone()
        finally:
            connection.close()
        first_source = writer.objects.get(r1_source)
        first_manifest = writer.objects.get(r1_manifest)
        first_manifest_data = json.loads(first_manifest.decode("utf-8"))
        if first_manifest_data.get("content_hash") != hashlib.sha256(first_source).hexdigest():
            fail("revision 1 source and manifest integrity check failed")

        second_input = KnowledgeWriteInput(
            first_input.title,
            "misc",
            ["e2e", "isolated"],
            "private",
            f"{marker}\nRevision two.",
            operation_date="2026-01-02",
        )
        second = writer.update_document(
            document_id, first.revision_id, f"{run_id}:update", second_input
        )
        if second.operation_result != "UPDATED" or second.revision_id == first.revision_id:
            fail("update did not create a distinct immutable revision")
        if second.processing_status != "QUEUED" or not second.processing_job_id:
            fail("updated revision has no queued processing job")
        if (
            writer.objects.get(r1_source) != first_source
            or writer.objects.get(r1_manifest) != first_manifest
        ):
            fail("revision 1 was modified by revision 2")
        retry = writer.update_document(
            document_id, first.revision_id, f"{run_id}:update", second_input
        )
        if retry.revision_id != second.revision_id:
            fail("update idempotency did not return the reserved revision")
        if len(tracked.created_keys) != 4 or tracked.successful_put_count != 4:
            fail("update retry created duplicate R2 objects")
        try:
            writer.update_document(
                document_id,
                first.revision_id,
                f"{run_id}:stale-cas",
                KnowledgeWriteInput(
                    first_input.title,
                    "misc",
                    ["e2e", "isolated"],
                    "private",
                    f"{marker}\nStale write must not land.",
                    operation_date="2026-01-04",
                ),
            )
            fail("stale CAS unexpectedly succeeded")
        except Exception as exc:
            from engine.postgres_operational import RevisionConflict

            if not isinstance(exc, RevisionConflict):
                raise
        current = writer.operational_store.get_current_revision(document_id)
        if current["current_revision_id"] != second.revision_id:
            fail("stale CAS changed the current pointer")
        if len(tracked.created_keys) != 4 or tracked.successful_put_count != 4:
            fail("stale CAS created R2 objects")
        no_op = writer.update_document(
            document_id,
            second.revision_id,
            f"{run_id}:noop",
            KnowledgeWriteInput(
                first_input.title,
                "misc",
                ["e2e", "isolated"],
                "private",
                f"{marker}\nRevision two.",
                operation_date="2026-01-03",
            ),
        )
        if no_op.operation_result != "NOOP" or no_op.current_revision_id != second.revision_id:
            fail("exact update did not resolve as a no-op")
        if len(tracked.created_keys) != 4 or tracked.successful_put_count != 4:
            fail("no-op created R2 objects")

        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state,current_revision_id FROM documents WHERE id=%s", (document_id,)
                )
                if cursor.fetchone() != ("ACTIVE", second.revision_id):
                    fail("current pointer changed during retry, no-op, or stale CAS")
                cursor.execute(
                    "SELECT idempotency_key,state FROM revision_write_intents WHERE document_id=%s",
                    (document_id,),
                )
                intent_states = dict(cursor.fetchall())
                if intent_states != {
                    f"{run_id}:create": "COMMITTED",
                    f"{run_id}:update": "COMMITTED",
                    f"{run_id}:noop": "NOOP",
                }:
                    fail("write intent states did not match create/update/no-op results")
                cursor.execute(
                    "SELECT revision_id,state FROM processing_jobs WHERE document_id=%s",
                    (document_id,),
                )
                job_states = dict(cursor.fetchall())
                if job_states != {
                    first.revision_id: "SUPERSEDED",
                    second.revision_id: "QUEUED",
                }:
                    fail("processing jobs were duplicated or have unexpected states")
        finally:
            connection.close()

        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,)
                )
                if cursor.fetchone()[0] != 2:
                    fail("unexpected revision count before tombstone")
                cursor.execute(
                    "SELECT count(*) FROM processing_jobs WHERE document_id=%s", (document_id,)
                )
                if cursor.fetchone()[0] != 2:
                    fail("processing jobs were not idempotent")
        finally:
            connection.close()

        writer.delete_document(document_id, second.revision_id)
        writer.delete_document(document_id, second.revision_id)
        if len(tracked.delete_keys) != 0:
            fail("writer tombstone issued an R2 delete")
        if len(tracked.created_keys) != 4:
            fail("unexpected test-owned R2 object count before cleanup")
        for key in sorted(tracked.created_keys):
            if not key.startswith(f"knowledge/{document_id}/revisions/"):
                fail("test object escaped its unique document namespace")
            writer.objects.get(key)
        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT state FROM documents WHERE id=%s", (document_id,))
                if cursor.fetchone()[0] != "DELETED":
                    fail("tombstone did not persist")
                cursor.execute(
                    "SELECT count(*) FROM revisions WHERE document_id=%s", (document_id,)
                )
                if cursor.fetchone()[0] != 2:
                    fail("tombstone removed canonical revisions")
                cursor.execute(
                    "SELECT count(*) FROM revision_write_intents "
                    "WHERE document_id=%s AND state IN ('COMMITTED','NOOP')",
                    (document_id,),
                )
                if cursor.fetchone()[0] != 3:
                    fail("write intent results were not durably recorded")
        finally:
            connection.close()
        passed = True
        print("WRITER CREATE / RETRY / UPDATE / NOOP / TOMBSTONE: PASS")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - keep test operation details private
        print("LIVE WRITER E2E: FAIL — operation failed (details withheld)")
        return 1
    finally:
        # A successful conditional PUT is the only event that grants cleanup ownership.
        # Do not enumerate the bucket or touch any key outside this process-local set.
        for key in sorted(tracked.created_keys):
            try:
                if not key.startswith(f"knowledge/{document_id}/revisions/"):
                    cleanup_ok = False
                    continue
                tracked.delete_object(Bucket=r2_store.bucket, Key=key)
                if not _head_is_absent(r2_store.client, r2_store.bucket, key):
                    cleanup_ok = False
            except Exception:  # noqa: BLE001 - cleanup only reports success/failure
                cleanup_ok = False
        try:
            with admin.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        except Exception:  # noqa: BLE001 - schema cleanup must not expose connection data
            cleanup_ok = False
        admin.close()
        if not cleanup_ok:
            print("CLEANUP: FAIL — one or more test-owned resources could not be removed")

    if passed and cleanup_ok:
        print("R2 OBJECTS: created=4, overwritten=0, unexpected_deletes=0, residue=0")
        print("CLEANUP: PASS (test-owned keys and disposable schema only)")
        print("LIVE WRITER E2E: PASS")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
