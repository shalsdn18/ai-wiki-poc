from __future__ import annotations

from io import BytesIO
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

import app
from engine import document_store
from engine.r2_document_store import ClientError, R2Store


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_operation = None

    def _maybe_fail(self, operation):
        if self.fail_operation == operation:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "offline"}},
                operation,
            )

    def put_object(self, **kwargs):
        self._maybe_fail("put_object")
        self.calls.append(("put_object", kwargs))
        self.objects[kwargs["Key"]] = {
            "Body": kwargs["Body"],
            "ContentType": kwargs["ContentType"],
        }

    def get_object(self, **kwargs):
        self._maybe_fail("get_object")
        self.calls.append(("get_object", kwargs))
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.objects[kwargs["Key"]]["Body"])}

    def delete_object(self, **kwargs):
        self._maybe_fail("delete_object")
        self.calls.append(("delete_object", kwargs))
        self.objects.pop(kwargs["Key"], None)

    def list_objects_v2(self, **kwargs):
        self._maybe_fail("list_objects_v2")
        self.calls.append(("list_objects_v2", kwargs))
        keys = sorted(key for key in self.objects if key.startswith(kwargs["Prefix"]))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


def test_r2_create_get_list_update_delete_uses_markdown_object_keys():
    client = FakeS3Client()
    store = R2Store(client, "private-notes")
    content = "---\ntags: [obsidian]\n---\n\n# 개인 문서\n내용 UTF-8 유지\n"

    created = store.create_document("개인 문서", content, ["개인", "노트"])

    key = f"documents/{created['id']}.md"
    assert key in client.objects
    assert client.objects[key]["ContentType"] == "text/markdown; charset=utf-8"
    assert client.objects[key]["Body"].decode("utf-8").endswith(content)
    assert created["path"] == key
    assert created["content"] == content
    assert len(created["id"]) == 32
    assert created["content_hash"] == sha256(content.encode("utf-8")).hexdigest()
    assert created["created_at"] == created["updated_at"]
    assert store.get_document(created["id"]) == created
    assert store.list_documents() == [created]

    updated = store.update_document(created["id"], "수정", "# 새 내용", ["변경"])
    assert updated["title"] == "수정"
    assert updated["content"] == "# 새 내용"
    assert updated["content_hash"] == sha256("# 새 내용".encode("utf-8")).hexdigest()
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]

    store.delete_document(created["id"])
    assert key not in client.objects
    with pytest.raises(document_store.DocumentNotFoundError):
        store.get_document(created["id"])
    with pytest.raises(document_store.DocumentNotFoundError):
        store.delete_document(created["id"])


def test_r2_ignores_objects_outside_document_id_key_shape():
    client = FakeS3Client()
    client.objects["documents/readme.md"] = {"Body": b"ignored", "ContentType": "text/markdown"}
    store = R2Store(client, "private-notes")

    assert store.list_documents() == []


def test_r2_rejects_invalid_markdown_frontmatter():
    client = FakeS3Client()
    document_id = "0123456789abcdef0123456789abcdef"
    client.objects[f"documents/{document_id}.md"] = {
        "Body": b"this is not a managed Markdown document",
        "ContentType": "text/markdown",
    }

    with pytest.raises(document_store.DocumentStorageError, match="frontmatter"):
        R2Store(client, "private-notes").get_document(document_id)


def test_r2_configuration_requires_endpoint_bucket_and_credentials(monkeypatch):
    document_store._configured_store.cache_clear()
    for name in (
        "R2_ENDPOINT_URL",
        "R2_ACCOUNT_ID",
        "R2_BUCKET_NAME",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCUMENT_STORAGE_BACKEND", "r2")

    with pytest.raises(document_store.DocumentStorageConfigurationError):
        document_store.get_store()


def test_r2_client_errors_become_storage_unavailable_errors():
    client = FakeS3Client()
    client.fail_operation = "list_objects_v2"
    store = R2Store(client, "private-notes")

    with pytest.raises(document_store.DocumentStorageUnavailableError):
        store.list_documents()


def test_r2_storage_errors_map_to_503_and_missing_objects_to_404(monkeypatch):
    import base64
    import hashlib

    password = "test-only-password"
    salt = "test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000)
    monkeypatch.setattr(
        app,
        "_password_hash",
        f"pbkdf2_sha256$600000${salt}${base64.urlsafe_b64encode(digest).decode('ascii')}",
    )
    monkeypatch.setattr(app, "_session_secret", b"test-only-session-secret-with-extra-length")
    monkeypatch.setattr(app, "_production", False)
    client = FakeS3Client()
    store = R2Store(client, "private-notes")
    monkeypatch.setattr(document_store, "get_store", lambda root=None: store)
    api_client = TestClient(app.app)
    csrf_headers = {"Origin": "http://testserver", "X-CSRF-Protection": "1"}
    api_client.post("/login", json={"password": password}, headers=csrf_headers)

    missing = api_client.get("/documents/00000000000000000000000000000000")
    assert missing.status_code == 404

    client.fail_operation = "list_objects_v2"
    unavailable = api_client.get("/documents")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "Document storage is temporarily unavailable"
