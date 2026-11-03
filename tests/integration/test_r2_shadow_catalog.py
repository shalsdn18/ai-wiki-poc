from __future__ import annotations

import hashlib
import io
import json
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("POSTGRES_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DATABASE_URL is not configured")


class FakeR2:
    def __init__(self, objects):
        self.objects = objects

    def list_objects_v2(self, **kwargs):
        return {
            "Contents": [
                {"Key": k} for k in sorted(self.objects) if k.startswith(kwargs["Prefix"])
            ],
            "IsTruncated": False,
        }

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def _dsn_factory(dsn, schema):
    def connect():
        conn = psycopg.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        return conn

    return connect


def test_shadow_import_is_idempotent_in_postgres():
    from engine.postgres_operational import MIGRATION_DIR, migrate
    from engine.r2_shadow_catalog import import_shadow, scan_r2, shadow_snapshot

    database = psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", "").casefold()
    if "test" not in database:
        pytest.fail("shadow integration requires a dedicated database containing 'test'")
    schema = "shadow_test_" + uuid.uuid4().hex
    admin = psycopg.connect(DSN, autocommit=True)
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    connect = _dsn_factory(DSN, schema)
    try:
        expected = [
            int(p.name.split("_", 1)[0])
            for p in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9]_*.sql"))
        ]
        assert migrate(connect) == expected
        doc, rev = "a" * 32, "b" * 64
        source_key = f"knowledge/{doc}/revisions/{rev}/source.md"
        manifest_key = f"knowledge/{doc}/revisions/{rev}/manifest.json"
        source = (
            f"---\ndocument_id: {doc}\ntitle: Shadow test\ncategory: ai\ntags: ["
            "Test"
            ", test]\n"
            "created: '2026-01-01'\nupdated: '2026-01-02'\nvisibility: private\n---\n\nsynthetic\n"
        ).encode()
        manifest = {
            "schema_version": 1,
            "document_id": doc,
            "revision_id": rev,
            "title": "Shadow test",
            "category": "ai",
            "tags": ["Test", "test"],
            "visibility": "private",
            "source_url": None,
            "mime_type": "text/markdown",
            "content_hash": hashlib.sha256(source).hexdigest(),
            "object_key": source_key,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
        objects = {source_key: source, manifest_key: json.dumps(manifest).encode()}
        inventory = scan_r2(FakeR2(objects), "synthetic-test-bucket")
        assert inventory.counts["VALID_COMPLETE"] == 1
        first = import_shadow(inventory, connect)
        snapshot_one = shadow_snapshot(connect)
        second = import_shadow(inventory, connect)
        snapshot_two = shadow_snapshot(connect)
        assert first["inserted_documents"] == first["inserted_revisions"] == 1
        assert second["inserted_documents"] == second["inserted_revisions"] == 0
        assert snapshot_one == snapshot_two
        assert first["processing_jobs"] == first["revision_write_intents"] == 0
        assert first["tags"] == first["document_tags"] == 1
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
