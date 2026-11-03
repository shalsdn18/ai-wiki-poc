"""Opt-in, read-only R2 to disposable PostgreSQL shadow qualification."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.postgres_operational import migrate
from engine.r2_shadow_catalog import GuardedR2Client, import_shadow, scan_r2, shadow_snapshot


def _postgres_factory(dsn: str):
    def connect():
        import psycopg

        return psycopg.connect(dsn, connect_timeout=5)

    return connect


def main() -> int:
    load_dotenv(override=False)
    try:
        account = os.getenv("KNOWLEDGE_R2_ACCOUNT_ID", "").strip()
        endpoint = os.getenv("KNOWLEDGE_R2_ENDPOINT_URL", "").strip()
        if not endpoint and account:
            endpoint = f"https://{account}.r2.cloudflarestorage.com"
        bucket = os.getenv("KNOWLEDGE_R2_BUCKET_NAME", "").strip()
        access = os.getenv("KNOWLEDGE_R2_ACCESS_KEY_ID", "").strip()
        secret = os.getenv("KNOWLEDGE_R2_SECRET_ACCESS_KEY", "").strip()
        prefix = os.getenv("KNOWLEDGE_R2_PREFIX", "knowledge").strip("/")
        dsns = [
            os.getenv("POSTGRES_TEST_DATABASE_URL", "").strip(),
            os.getenv("SHADOW_TEST_DATABASE_URL_B", "").strip(),
        ]
        if not all((endpoint, bucket, access, secret)) or prefix != "knowledge":
            raise ValueError("required R2 configuration is incomplete")
        if not dsns[0]:
            raise ValueError("disposable PostgreSQL test URL is missing")
        for dsn in dsns:
            if not dsn:
                continue
            parsed = urlsplit(dsn)
            if (
                parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or "test" not in (parsed.path or "").casefold()
            ):
                raise ValueError("PostgreSQL target is not a localhost test database")

        import boto3

        raw = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name="auto",
            aws_access_key_id=access,
            aws_secret_access_key=secret,
        )

        def deny_non_read(event_name, params, model, **kwargs):
            if getattr(model, "name", "") not in {"ListObjectsV2", "GetObject"}:
                raise PermissionError("R2 shadow client permits LIST/GET only")
            if params.get("Bucket") != bucket:
                raise PermissionError("R2 shadow bucket mismatch")
            if model.name == "ListObjectsV2" and params.get("Prefix") != "knowledge/":
                raise PermissionError("R2 shadow prefix mismatch")
            if model.name == "GetObject" and not str(params.get("Key", "")).startswith(
                "knowledge/"
            ):
                raise PermissionError("R2 shadow key prefix mismatch")

        raw.meta.events.register("before-parameter-build.s3", deny_non_read)
        client = GuardedR2Client(raw, bucket, prefix)
        inventory = scan_r2(client, bucket, prefix)
        outputs = []
        snapshots = []
        for dsn in dsns:
            if not dsn:
                continue
            connect = _postgres_factory(dsn)
            migrate(connect)
            first = import_shadow(inventory, connect)
            snap1 = shadow_snapshot(connect)
            second = import_shadow(inventory, connect)
            snap2 = shadow_snapshot(connect)
            if snap1 != snap2 or second["inserted_documents"] or second["inserted_revisions"]:
                raise ValueError("shadow import idempotency failed")
            outputs.append({"first": first, "second": second})
            snapshots.append(snap2)

        store_crosscheck = {"status": "NOT_RUN"}
        if inventory.revisions:
            from engine.r2_knowledge_store import R2KnowledgeStore

            reader = R2KnowledgeStore(client, bucket, prefix=prefix, refresh_seconds=0)
            docs = reader.list_documents()
            candidates = {}
            for a in inventory.documents:
                if a.candidate_revision_id:
                    candidates[a.document_id] = next(
                        (
                            r
                            for r in inventory.revisions
                            if r.document_id == a.document_id
                            and r.revision_id == a.candidate_revision_id
                        ),
                        None,
                    )
            mismatch = 0
            observed = {d.id: d for d in docs}
            for doc_id, record in candidates.items():
                d = observed.get(doc_id)
                if record and (
                    d is None
                    or d.title != record.title
                    or d.category != record.category
                    or tuple(d.tags) != record.tags
                    or d.visibility != record.visibility
                    or d.source_url != record.source_url
                    or d.content != record.content
                ):
                    mismatch += 1
            if len(observed) != len(candidates):
                mismatch += abs(len(observed) - len(candidates))
            if mismatch:
                raise ValueError("R2KnowledgeStore cross-check mismatch")
            store_crosscheck = {"status": "PASS", "documents": len(docs)}

        if len(snapshots) == 2 and snapshots[0] != snapshots[1]:
            raise ValueError("independent database determinism failed")
        result = {
            "status": "PASS",
            "inventory": {
                "documents": len(inventory.documents),
                "revisions": len(inventory.revisions),
                **inventory.counts,
            },
            "current": {
                name: sum(d.current_classification == name for d in inventory.documents)
                for name in ("CURRENT_UNAMBIGUOUS", "CURRENT_AMBIGUOUS", "CURRENT_INVALID")
            },
            "timestamp_ties": sum(d.timestamp_tie for d in inventory.documents),
            "lexical_fallbacks": sum(d.lexical_fallback for d in inventory.documents),
            "postgres": outputs,
            "deterministic_databases": len(snapshots) < 2 or snapshots[0] == snapshots[1],
            "r2_reader": store_crosscheck,
            "r2_operations": dict(client.operations),
            "r2_write_count": 0,
            "r2_delete_count": 0,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - exception text may contain credentials or DSNs
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
