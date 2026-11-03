"""Opt-in live Cloudflare R2 Knowledge E2E; never part of pytest collection."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

for logger_name in ("boto3", "botocore", "urllib3"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

EXPECTED_BUCKET = "ai-wiki-documents"
EXPECTED_PREFIX = "knowledge"
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


class LiveE2EFailure(RuntimeError):
    """A sanitized live verification failure."""


def _fail(message: str) -> None:
    raise LiveE2EFailure(message)


def _error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        detail = response.get("Error", {})
        code = detail.get("Code") if isinstance(detail, dict) else None
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
            return code
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            return f"HTTP_{status}"
    return type(error).__name__


def _safe_key(key: str) -> str:
    if not key.startswith(f"{EXPECTED_PREFIX}/") or ".." in key.split("/"):
        _fail("Refusing an object key outside the isolated Knowledge prefix")
    return key


def _read_body(response: dict) -> bytes:
    body = response.get("Body")
    try:
        value = body.read()
        if not isinstance(value, bytes):
            _fail("R2 returned an invalid object body")
        return value
    except LiveE2EFailure:
        raise
    except Exception as error:  # noqa: BLE001 - never print SDK/request details
        _fail(f"Object read failed ({_error_code(error)})")
    finally:
        close = getattr(body, "close", None)
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001, S110 - cleanup cannot mask the read result
                pass


def _head_exists(client, key: str) -> bool:
    try:
        client.head_object(Bucket=EXPECTED_BUCKET, Key=_safe_key(key))
        return True
    except Exception as error:  # noqa: BLE001 - classify without exposing response text
        code = _error_code(error)
        if code in {"404", "NoSuchKey", "NotFound", "HTTP_404"}:
            return False
        _fail(f"R2 preflight object check failed ({code})")


def _manifest_bytes(
    *,
    document_id: str,
    revision_id: str,
    title: str,
    source: bytes,
    source_key: str,
    created_at: datetime,
    updated_at: datetime,
) -> bytes:
    manifest = {
        "schema_version": 1,
        "document_id": document_id,
        "revision_id": revision_id,
        "title": title,
        "category": "ai",
        "tags": ["live-e2e", "r2-knowledge"],
        "visibility": "private",
        "source_url": None,
        "mime_type": "text/markdown; charset=utf-8",
        "content_hash": hashlib.sha256(source).hexdigest(),
        "object_key": source_key,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": updated_at.isoformat().replace("+00:00", "Z"),
    }
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _source_bytes(
    title: str, body: str, document_id: str, created_at: datetime, updated_at: datetime
) -> bytes:
    metadata = (
        "---\n"
        f"document_id: {document_id}\n"
        f"title: {title}\n"
        "category: ai\n"
        "tags: [live-e2e, r2-knowledge]\n"
        f'created: "{created_at.date().isoformat()}"\n'
        f'updated: "{updated_at.date().isoformat()}"\n'
        "visibility: private\n"
        "---\n\n"
    )
    return f"{metadata}{body}\n".encode()


def _verify_revision(
    client, *, doc_id: str, rev_id: str, expected_source: bytes
) -> tuple[str, str]:
    revision_prefix = f"{EXPECTED_PREFIX}/{doc_id}/revisions/{rev_id}/"
    source_key = _safe_key(revision_prefix + "source.md")
    manifest_key = _safe_key(revision_prefix + "manifest.json")
    source_head = client.head_object(Bucket=EXPECTED_BUCKET, Key=source_key)
    manifest_head = client.head_object(Bucket=EXPECTED_BUCKET, Key=manifest_key)
    if not str(source_head.get("ContentType", "")).casefold().startswith("text/markdown"):
        _fail("Stored source MIME type does not match the manifest contract")
    source_bytes = _read_body(client.get_object(Bucket=EXPECTED_BUCKET, Key=source_key))
    manifest_bytes = _read_body(client.get_object(Bucket=EXPECTED_BUCKET, Key=manifest_key))
    if source_bytes != expected_source:
        _fail("Stored Markdown bytes differ from the uploaded revision")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        created_at = datetime.fromisoformat(manifest["created_at"])
        updated_at = datetime.fromisoformat(manifest["updated_at"])
    except (UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        _fail("Stored manifest is not valid JSON with parseable timestamps")
    content_hash = hashlib.sha256(source_bytes).hexdigest()
    if (
        manifest.get("schema_version") != 1
        or manifest.get("document_id") != doc_id
        or manifest.get("revision_id") != rev_id
        or not isinstance(manifest.get("title"), str)
        or manifest.get("category") != "ai"
        or not isinstance(manifest.get("tags"), list)
        or manifest.get("visibility") != "private"
        or manifest.get("source_url") is not None
        or manifest.get("object_key") != source_key
        or manifest.get("content_hash") != content_hash
        or manifest.get("mime_type") != "text/markdown; charset=utf-8"
        or created_at.tzinfo is None
        or updated_at.tzinfo is None
        or not manifest_head
    ):
        _fail("Stored manifest does not match its object identity or source hash")
    if not source_key.startswith("knowledge/") or not manifest_key.startswith("knowledge/"):
        _fail("An E2E object escaped the Knowledge prefix")
    return source_key, manifest_key


def _verify_local_api(reader, marker: str, doc_id: str) -> None:
    from fastapi.testclient import TestClient

    import app

    password = secrets.token_urlsafe(24)
    salt = secrets.token_urlsafe(18)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000)
    encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    prior_hash = os.environ.get("APP_PASSWORD_HASH")
    prior_session = os.environ.get("SESSION_SECRET")
    prior_store = app.get_knowledge_store
    prior_production = app._production
    os.environ["APP_PASSWORD_HASH"] = f"pbkdf2_sha256$600000${salt}${encoded}"
    os.environ["SESSION_SECRET"] = secrets.token_urlsafe(48)
    app.get_knowledge_store = lambda: reader
    app._production = False
    try:
        with TestClient(app.app) as client:
            response = client.post(
                "/login",
                json={"password": password},
                headers={"Origin": "http://testserver", "X-CSRF-Protection": "1"},
            )
            if response.status_code != 200:
                _fail("Local E2E login setup failed")
            stats = client.get("/knowledge/stats")
            recent = client.get("/knowledge/recent", params={"limit": 100})
            search = client.get("/knowledge/search", params={"q": marker, "limit": 100})
            note = client.get(f"/knowledge/note/{doc_id}")
            if (
                stats.status_code != 200
                or recent.status_code != 200
                or search.status_code != 200
                or note.status_code != 200
                or not any(item.get("id") == doc_id for item in recent.json())
                or not any(item.get("id") == doc_id for item in search.json())
                or note.json().get("id") != doc_id
            ):
                _fail("Authenticated local /knowledge API contract failed")
    finally:
        app.get_knowledge_store = prior_store
        app._production = prior_production
        if prior_hash is None:
            os.environ.pop("APP_PASSWORD_HASH", None)
        else:
            os.environ["APP_PASSWORD_HASH"] = prior_hash
        if prior_session is None:
            os.environ.pop("SESSION_SECRET", None)
        else:
            os.environ["SESSION_SECRET"] = prior_session


def _configuration():
    if not (ROOT / ".env").is_file():
        _fail("Local .env file is missing")
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if tracked.returncode == 0 or ignored.returncode != 0:
        _fail("Local .env must be untracked and Git-ignored")
    names = (
        "KNOWLEDGE_STORE_BACKEND",
        "KNOWLEDGE_R2_BUCKET_NAME",
        "KNOWLEDGE_R2_PREFIX",
        "KNOWLEDGE_R2_ACCESS_KEY_ID",
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY",
    )
    missing = [name for name in names if not os.getenv(name, "").strip()]
    endpoint = os.getenv("KNOWLEDGE_R2_ENDPOINT_URL", "").strip()
    account_id = os.getenv("KNOWLEDGE_R2_ACCOUNT_ID", "").strip()
    if not endpoint and not account_id:
        missing.append("KNOWLEDGE_R2_ENDPOINT_URL or KNOWLEDGE_R2_ACCOUNT_ID")
    if missing:
        _fail("Missing required settings: " + ", ".join(missing))
    if os.getenv("KNOWLEDGE_STORE_BACKEND", "").strip().casefold() != "r2":
        _fail("KNOWLEDGE_STORE_BACKEND is not r2")
    if os.getenv("KNOWLEDGE_R2_BUCKET_NAME", "").strip() != EXPECTED_BUCKET:
        _fail("Knowledge R2 bucket does not match the approved target")
    if os.getenv("KNOWLEDGE_R2_PREFIX", "").strip("/") != EXPECTED_PREFIX:
        _fail("Knowledge R2 prefix does not match the isolated target")
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
            _fail("Knowledge R2 endpoint format is invalid")
    elif not all(character.isalnum() or character == "-" for character in account_id):
        _fail("Knowledge R2 account ID format is invalid")
    region = os.getenv("KNOWLEDGE_R2_REGION", "auto").strip() or "auto"
    if region != "auto":
        _fail("Knowledge R2 region is not the configured R2 region")
    try:
        refresh = float(os.getenv("KNOWLEDGE_R2_REFRESH_SECONDS", "30"))
        stale = float(os.getenv("KNOWLEDGE_R2_STALE_MAX_SECONDS", "900"))
    except ValueError:
        _fail("Knowledge R2 cache settings are invalid")
    if refresh < 0 or stale < 0:
        _fail("Knowledge R2 cache settings are invalid")


def run() -> tuple[bool, int, int, int]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-live-r2",
        action="store_true",
        help="authorize isolated writes and deletes under knowledge/ in the configured bucket",
    )
    args = parser.parse_args()
    if not args.confirm_live_r2:
        _fail("Pass --confirm-live-r2 to authorize the isolated live bucket test")

    _configuration()
    from engine.r2_knowledge_store import R2KnowledgeStore, get_r2_knowledge_store

    configured_store = get_r2_knowledge_store()
    if configured_store.bucket != EXPECTED_BUCKET or configured_store.prefix != EXPECTED_PREFIX:
        _fail("Application R2 store did not resolve to the approved bucket and prefix")
    client = configured_store.client

    try:
        configured_store.stats()
    except Exception as error:  # noqa: BLE001 - sanitize endpoint/auth details
        _fail(f"Read-only R2 connection preflight failed ({_error_code(error)})")
    baseline_health = configured_store.health_status()
    baseline_stats = configured_store.stats()
    baseline_malformed = baseline_health.get("malformed_documents", 0)
    if not isinstance(baseline_malformed, int):
        _fail("R2 health returned an invalid malformed-document count")
    print("CONFIGURATION: PASS (values withheld)")
    print("R2 READ-ONLY PREFLIGHT: PASS")

    run_id = uuid.uuid4().hex
    document_id = hashlib.sha256(f"ai-wiki-live-e2e:{run_id}".encode()).hexdigest()
    malformed_id = hashlib.sha256(f"ai-wiki-live-e2e-malformed:{run_id}".encode()).hexdigest()
    revision_1_id = hashlib.sha256(f"revision-1:{run_id}".encode()).hexdigest()
    revision_2_id = hashlib.sha256(f"revision-2:{run_id}".encode()).hexdigest()
    malformed_revision_id = hashlib.sha256(f"malformed:{run_id}".encode()).hexdigest()
    title = f"AI Wiki isolated R2 live E2E {run_id[:8]}"
    marker = f"AIWIKI_R2_LIVE_E2E_{run_id.upper()}"
    owned_keys: list[str] = []
    confirmed_put_keys: set[str] = set()
    failures: list[str] = []
    r1_source_key = f"knowledge/{document_id}/revisions/{revision_1_id}/source.md"
    r1_manifest_key = f"knowledge/{document_id}/revisions/{revision_1_id}/manifest.json"
    r2_source_key = f"knowledge/{document_id}/revisions/{revision_2_id}/source.md"
    r2_manifest_key = f"knowledge/{document_id}/revisions/{revision_2_id}/manifest.json"
    malformed_key = f"knowledge/{malformed_id}/revisions/{malformed_revision_id}/manifest.json"

    try:
        candidates = (r1_source_key, r1_manifest_key, r2_source_key, r2_manifest_key, malformed_key)
        for key in candidates:
            _safe_key(key)
            if _head_exists(client, key):
                _fail("Random E2E namespace collision detected; no object was overwritten")

        created_at = datetime.now(UTC)
        updated_1 = created_at + timedelta(days=365 * 20)
        updated_2 = updated_1 + timedelta(seconds=2)
        source_1 = _source_bytes(
            title, f"{marker} revision one searchable payload", document_id, created_at, updated_1
        )
        source_2 = _source_bytes(
            title,
            f"{marker} revision two changed searchable payload",
            document_id,
            created_at,
            updated_2,
        )
        if (
            revision_1_id == revision_2_id
            or hashlib.sha256(source_1).hexdigest() == hashlib.sha256(source_2).hexdigest()
        ):
            _fail("Revision fixture identity/content is not distinct")

        owned_keys.append(r1_source_key)
        client.put_object(
            Bucket=EXPECTED_BUCKET,
            Key=_safe_key(r1_source_key),
            Body=source_1,
            ContentType="text/markdown; charset=utf-8",
        )
        confirmed_put_keys.add(r1_source_key)
        owned_keys.append(r1_manifest_key)
        client.put_object(
            Bucket=EXPECTED_BUCKET,
            Key=_safe_key(r1_manifest_key),
            Body=_manifest_bytes(
                document_id=document_id,
                revision_id=revision_1_id,
                title=title,
                source=source_1,
                source_key=r1_source_key,
                created_at=created_at,
                updated_at=updated_1,
            ),
            ContentType="application/json",
        )
        confirmed_put_keys.add(r1_manifest_key)
        _verify_revision(client, doc_id=document_id, rev_id=revision_1_id, expected_source=source_1)
        print("REVISION 1: PASS")

        reader = R2KnowledgeStore(
            client,
            EXPECTED_BUCKET,
            EXPECTED_PREFIX,
            refresh_seconds=0,
            stale_max_seconds=900,
        )
        doc = reader.get_document(document_id)
        if doc.content != f"{marker} revision one searchable payload":
            _fail("Revision 1 was not returned through R2KnowledgeStore")
        if not any(item.id == document_id for item in reader.list_documents()):
            _fail("R2KnowledgeStore list omitted the E2E document")
        if not any(item.id == document_id for item in reader.search(marker)):
            _fail("R2KnowledgeStore search omitted the unique E2E marker")
        before_stats = reader.stats()
        if (
            before_stats["documents"] < baseline_stats["documents"] + 1
            or before_stats["categories"]["ai"] < baseline_stats["categories"]["ai"] + 1
        ):
            _fail("R2KnowledgeStore stats omitted the E2E document category")
        if reader.health_status().get("status") != "HEALTHY":
            _fail("R2KnowledgeStore health was not HEALTHY after revision 1")
        _verify_local_api(reader, marker, document_id)
        print("R2KnowledgeStore/API CONTRACT: PASS")

        owned_keys.append(r2_source_key)
        client.put_object(
            Bucket=EXPECTED_BUCKET,
            Key=_safe_key(r2_source_key),
            Body=source_2,
            ContentType="text/markdown; charset=utf-8",
        )
        confirmed_put_keys.add(r2_source_key)
        owned_keys.append(r2_manifest_key)
        client.put_object(
            Bucket=EXPECTED_BUCKET,
            Key=_safe_key(r2_manifest_key),
            Body=_manifest_bytes(
                document_id=document_id,
                revision_id=revision_2_id,
                title=title,
                source=source_2,
                source_key=r2_source_key,
                created_at=created_at,
                updated_at=updated_2,
            ),
            ContentType="application/json",
        )
        confirmed_put_keys.add(r2_manifest_key)
        _verify_revision(client, doc_id=document_id, rev_id=revision_2_id, expected_source=source_2)
        if revision_1_id == revision_2_id:
            _fail("Revision IDs unexpectedly matched")
        if hashlib.sha256(source_1).hexdigest() == hashlib.sha256(source_2).hexdigest():
            _fail("Revision content hashes unexpectedly matched")
        retained_r1 = _read_body(
            client.get_object(Bucket=EXPECTED_BUCKET, Key=_safe_key(r1_source_key))
        )
        if retained_r1 != source_1:
            _fail("Revision 1 source changed after revision 2 was written")
        latest = reader.get_document(document_id)
        if latest.content != f"{marker} revision two changed searchable payload":
            _fail("R2KnowledgeStore did not select revision 2 by current timestamp ordering")
        print("REVISION 2 / REVISION 1 RETENTION: PASS")

        owned_keys.append(malformed_key)
        client.put_object(
            Bucket=EXPECTED_BUCKET,
            Key=_safe_key(malformed_key),
            Body=b"{ definitely not valid json",
            ContentType="application/json",
        )
        confirmed_put_keys.add(malformed_key)
        documents = reader.list_documents()
        if not any(item.id == document_id for item in documents):
            _fail("Malformed manifest prevented the normal E2E document from loading")
        health = reader.health_status()
        if health.get("status") != "HEALTHY":
            _fail("Malformed manifest changed R2KnowledgeStore health from HEALTHY")
        if health.get("malformed_documents", 0) < baseline_malformed + 1:
            _fail("Malformed manifest was not isolated and counted by health")
        if not any(item.id == document_id for item in reader.search(marker)):
            _fail("Malformed manifest affected search for the normal E2E document")
        if reader.stats()["documents"] < baseline_stats["documents"] + 1:
            _fail("Malformed manifest affected stats for the normal E2E document")
        health_text = json.dumps(health, sort_keys=True)
        for secret_name in ("KNOWLEDGE_R2_ACCESS_KEY_ID", "KNOWLEDGE_R2_SECRET_ACCESS_KEY"):
            secret = os.getenv(secret_name, "")
            if secret and secret in health_text:
                _fail("R2 health exposed a configured credential")
        endpoint = os.getenv("KNOWLEDGE_R2_ENDPOINT_URL", "")
        if endpoint and endpoint in health_text:
            _fail("R2 health exposed the configured endpoint")
        print("MALFORMED ISOLATION: PASS")
    except Exception as error:  # noqa: BLE001 - no raw exception text may contain credentials
        failures.append(f"E2E operation failed ({_error_code(error)})")
    finally:
        cleanup_errors = []
        found_before_delete = set(confirmed_put_keys)
        for key in owned_keys:
            try:
                if _head_exists(client, key):
                    found_before_delete.add(key)
            except Exception as error:  # noqa: BLE001 - sanitized verification only
                cleanup_errors.append(_error_code(error))
                found_before_delete.add(key)
        for key in reversed(owned_keys):
            try:
                client.delete_object(Bucket=EXPECTED_BUCKET, Key=_safe_key(key))
            except Exception as error:  # noqa: BLE001 - only exact tracked E2E keys are deleted
                cleanup_errors.append(_error_code(error))
        remaining = []
        for key in owned_keys:
            try:
                if _head_exists(client, key):
                    remaining.append(key)
            except Exception as error:  # noqa: BLE001 - sanitized verification only
                cleanup_errors.append(_error_code(error))
                remaining.append(key)
        deleted_count = len(found_before_delete) - len(remaining)
        print(f"CLEANUP: created={len(found_before_delete)} deleted={deleted_count}")
        print(f"E2E_OBJECTS_REMAINING={len(remaining)}")
        if cleanup_errors:
            failures.append(f"Cleanup failed ({','.join(cleanup_errors[:5])})")
        if remaining:
            failures.append("Cleanup verification found E2E objects still present")

    if failures:
        for failure in failures:
            print("FAIL: " + failure)
        return False, len(found_before_delete), deleted_count, len(remaining)
    print("LIVE R2 E2E: PASS")
    return True, len(found_before_delete), deleted_count, 0


if __name__ == "__main__":
    try:
        passed, _, _, _ = run()
    except LiveE2EFailure as error:
        # Messages contain setting names or sanitized operation codes only.
        print("LIVE R2 E2E: FAIL")
        print(str(error))
        raise SystemExit(1)
    except Exception as error:  # noqa: BLE001 - never print raw client exceptions
        print("LIVE R2 E2E: FAIL")
        print(f"Unexpected failure ({_error_code(error)})")
        raise SystemExit(1)
    raise SystemExit(0 if passed else 1)
