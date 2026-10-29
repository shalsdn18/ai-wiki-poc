import hashlib
import io
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import app
from engine.knowledge_store import (
    FilesystemKnowledgeStore,
    KnowledgeNotFoundError,
    KnowledgeUnavailableError,
    get_store,
    parse_markdown_document,
)
from engine.r2_knowledge_store import R2KnowledgeStore, get_r2_knowledge_store

DOCUMENT_ID = "a" * 64
REVISION_A = "1" * 64
REVISION_B = "2" * 64


def source(title="R2 Example", body="R2 searchable source", document_id=DOCUMENT_ID):
    return (
        "---\n"
        f"document_id: {document_id}\n"
        f"title: {title}\n"
        "category: ai\n"
        "tags: [storage, sample]\n"
        'created: "2026-01-01"\n'
        'updated: "2026-01-01"\n'
        "visibility: private\n"
        "---\n\n"
        f"{body}\n"
    ).encode()


def add_revision(
    client,
    revision_id=REVISION_A,
    *,
    title="R2 Example",
    body="R2 searchable source",
    updated="2026-01-01T00:00:00Z",
    document_id=DOCUMENT_ID,
):
    raw = source(title, body, document_id)
    base = f"knowledge/{document_id}/revisions/{revision_id}/"
    source_key = base + "source.md"
    manifest_key = base + "manifest.json"
    client.objects[source_key] = raw
    client.objects[manifest_key] = json.dumps(
        {
            "schema_version": 1,
            "document_id": document_id,
            "revision_id": revision_id,
            "title": title,
            "category": "ai",
            "tags": ["storage", "sample"],
            "visibility": "private",
            "source_url": None,
            "mime_type": "text/markdown; charset=utf-8",
            "content_hash": hashlib.sha256(raw).hexdigest(),
            "object_key": source_key,
            "created_at": updated,
            "updated_at": updated,
        }
    ).encode()
    return manifest_key


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_list = False
        self.fail_keys = set()

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list", kwargs.copy()))
        if self.fail_list:
            raise RuntimeError("secret=should-not-leak")
        prefix = kwargs["Prefix"]
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        return {
            "Contents": [
                {
                    "Key": key,
                    "ETag": hashlib.sha256(self.objects[key]).hexdigest(),
                    "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
                    "Size": len(self.objects[key]),
                }
                for key in keys
            ],
            "IsTruncated": False,
        }

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs.copy()))
        key = kwargs["Key"]
        if key in self.fail_keys or key not in self.objects:
            raise RuntimeError("secret=should-not-leak")
        return {"Body": io.BytesIO(self.objects[key])}


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def make_store(client=None, clock=None, **kwargs):
    return R2KnowledgeStore(
        client or FakeS3(),
        "private-knowledge",
        now=clock or Clock(),
        **kwargs,
    )


def test_manifest_snapshot_uses_valid_manifest_and_stable_document_id():
    client = FakeS3()
    add_revision(client)
    store = make_store(client)

    documents = store.list_documents()

    assert len(documents) == 1
    assert documents[0].id == DOCUMENT_ID
    assert documents[0].title == "R2 Example"
    assert documents[0].content == "R2 searchable source"
    assert store.health_status()["status"] == "HEALTHY"


def test_frontmatter_document_id_is_stable_across_repository_paths():
    raw = source().decode()

    first = parse_markdown_document("ai/one.md", raw)
    second = parse_markdown_document("ai/moved.md", raw)

    assert first.id == DOCUMENT_ID
    assert second.id == DOCUMENT_ID


def test_malformed_manifest_isolated_and_counted():
    client = FakeS3()
    add_revision(client)
    client.objects["knowledge/bad/revisions/bad/manifest.json"] = b"not-json"
    store = make_store(client)

    assert [item.title for item in store.list_documents()] == ["R2 Example"]
    assert store.health_status()["malformed_documents"] == 1


def test_latest_revision_selected_by_updated_time():
    client = FakeS3()
    add_revision(client, REVISION_A, title="Old", body="old", updated="2026-01-01T00:00:00Z")
    add_revision(client, REVISION_B, title="New", body="new", updated="2026-02-01T00:00:00Z")
    store = make_store(client)

    assert store.get_document(DOCUMENT_ID).title == "New"
    assert store.stats()["documents"] == 1


def test_list_get_recent_search_and_stats():
    client = FakeS3()
    add_revision(client)
    store = make_store(client)

    assert store.list_documents()[0].id == DOCUMENT_ID
    assert store.get_document(DOCUMENT_ID).title == "R2 Example"
    assert store.search("SEARCHABLE")[0].title == "R2 Example"
    assert store.stats() == {
        "documents": 1,
        "categories": {"ai": 1, "investment": 0, "philosophy": 0, "misc": 0},
    }
    with pytest.raises(KnowledgeNotFoundError):
        store.get_document("missing")


def test_empty_bucket_has_empty_healthy_results():
    store = make_store()

    assert store.list_documents() == []
    assert store.stats()["documents"] == 0
    assert store.health_status()["status"] == "HEALTHY"


def test_metadata_refresh_failure_serves_stale_snapshot_then_expires():
    client = FakeS3()
    add_revision(client)
    clock = Clock()
    store = make_store(client, clock, refresh_seconds=10, stale_max_seconds=20)
    assert store.list_documents()[0].title == "R2 Example"

    client.fail_list = True
    clock.advance(11)
    assert store.list_documents()[0].title == "R2 Example"
    assert store.health_status()["status"] == "STALE"

    clock.advance(21)
    with pytest.raises(KnowledgeUnavailableError):
        store.list_documents()
    assert store.health_status()["status"] == "UNAVAILABLE"


def test_partial_manifest_fetch_does_not_replace_prior_snapshot():
    client = FakeS3()
    add_revision(client, REVISION_A, title="Good")
    clock = Clock()
    store = make_store(client, clock, refresh_seconds=10, stale_max_seconds=30)
    assert store.list_documents()[0].title == "Good"
    add_revision(client, REVISION_B, title="New", updated="2026-02-01T00:00:00Z")
    client.fail_keys.add(f"knowledge/{DOCUMENT_ID}/revisions/{REVISION_B}/manifest.json")
    clock.advance(11)

    assert store.list_documents()[0].title == "Good"
    assert store.health_status()["status"] == "STALE"
    clock.advance(11)
    assert store.stats()["documents"] == 1
    assert store.health_status()["status"] == "STALE"


def test_source_hydration_failure_keeps_previous_complete_content():
    client = FakeS3()
    add_revision(client, REVISION_A, title="Good")
    clock = Clock()
    store = make_store(client, clock, refresh_seconds=10, stale_max_seconds=30)
    assert store.list_documents()[0].title == "Good"
    new_document_id = "b" * 64
    add_revision(
        client,
        REVISION_B,
        title="New",
        updated="2026-02-01T00:00:00Z",
        document_id=new_document_id,
    )
    client.fail_keys.add(f"knowledge/{new_document_id}/revisions/{REVISION_B}/source.md")
    clock.advance(11)

    assert store.list_documents()[0].title == "Good"
    assert store.stats()["documents"] == 1
    assert store.health_status()["status"] == "STALE"


def test_metadata_snapshot_does_not_fetch_markdown_until_documents_are_needed():
    client = FakeS3()
    add_revision(client)
    store = make_store(client)

    assert store.stats()["documents"] == 1
    assert not any(kwargs.get("Key", "").endswith("source.md") for _, kwargs in client.calls)


def test_r2_health_and_errors_never_expose_secret_or_endpoint():
    client = FakeS3()
    client.fail_list = True
    store = make_store(client)
    with pytest.raises(KnowledgeUnavailableError) as error:
        store.stats()

    assert "should-not-leak" not in str(error.value)
    assert "private-knowledge" not in str(store.health_status())
    assert "access_key" not in str(store.health_status())


def test_unauthenticated_knowledge_routes_stay_protected(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_STORE_BACKEND", "r2")
    monkeypatch.setenv("APP_PASSWORD_HASH", "configured-test-hash")
    monkeypatch.setenv("SESSION_SECRET", "test-only-signing-secret-with-extra-length")
    client = TestClient(app.app)

    assert client.get("/knowledge/stats").status_code == 401
    assert client.get("/knowledge/recent").status_code == 401


def test_authenticated_knowledge_api_reads_r2_store_contract(monkeypatch):
    import base64

    password = "test-only-password"
    salt = "fixed-test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        f"pbkdf2_sha256$600000${salt}${base64.urlsafe_b64encode(digest).decode('ascii')}",
    )
    monkeypatch.setenv("SESSION_SECRET", "test-only-signing-secret-with-extra-length")
    client = FakeS3()
    add_revision(client)
    store = make_store(client)
    monkeypatch.setattr(app, "get_knowledge_store", lambda: store)
    test_client = TestClient(app.app)

    login = test_client.post(
        "/login",
        json={"password": password},
        headers={"Origin": "http://testserver", "X-CSRF-Protection": "1"},
    )
    assert login.status_code == 200
    assert test_client.get("/knowledge/stats").json()["documents"] == 1
    recent = test_client.get("/knowledge/recent").json()
    assert recent[0]["id"] == DOCUMENT_ID
    assert test_client.get("/knowledge/search?q=searchable").json()[0]["title"] == "R2 Example"
    assert test_client.get(f"/knowledge/note/{DOCUMENT_ID}").json()["content"] == (
        "R2 searchable source"
    )


def test_r2_selector_and_existing_filesystem_adapter_are_preserved(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    folder = root / "ai"
    folder.mkdir(parents=True)
    (folder / "note.md").write_text(
        "---\ntitle: Local\ncategory: ai\ntags: []\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\nvisibility: private\n---\n\nLocal body\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KNOWLEDGE_STORE_BACKEND", "filesystem")
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(root))

    assert isinstance(get_store(), FilesystemKnowledgeStore)
    assert get_store().list_documents()[0].title == "Local"
    monkeypatch.setenv("KNOWLEDGE_STORE_BACKEND", "r2")

    import engine.r2_knowledge_store as r2_module

    fake_store = make_store()
    monkeypatch.setattr(r2_module, "get_r2_knowledge_store", lambda: fake_store)
    assert get_store() is fake_store


def test_r2_environment_validation_does_not_echo_credentials(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_R2_ENDPOINT_URL", "https://r2.example.invalid")
    monkeypatch.setenv("KNOWLEDGE_R2_BUCKET_NAME", "knowledge")
    monkeypatch.setenv("KNOWLEDGE_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("KNOWLEDGE_R2_SECRET_ACCESS_KEY", "do-not-echo-secret")
    monkeypatch.setattr(
        "engine.r2_knowledge_store._create_client",
        lambda *args: (_ for _ in ()).throw(AssertionError("must be validated before use")),
    )
    monkeypatch.setenv("KNOWLEDGE_R2_ENDPOINT_URL", "https://user:password@r2.example.invalid")

    with pytest.raises(KnowledgeUnavailableError) as error:
        get_r2_knowledge_store()

    assert "password" not in str(error.value)
    assert "do-not-echo-secret" not in str(error.value)


def test_r2_store_constructs_with_valid_s3_configuration_without_network(monkeypatch):
    client = FakeS3()
    monkeypatch.setenv("KNOWLEDGE_R2_ENDPOINT_URL", "https://r2.example.invalid")
    monkeypatch.setenv("KNOWLEDGE_R2_BUCKET_NAME", "knowledge")
    monkeypatch.setenv("KNOWLEDGE_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("KNOWLEDGE_R2_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr("engine.r2_knowledge_store._create_client", lambda *args: client)

    store = get_r2_knowledge_store()

    assert isinstance(store, R2KnowledgeStore)
    assert store.client is client
    assert store.bucket == "knowledge"
