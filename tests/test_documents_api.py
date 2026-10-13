import base64
import hashlib
from hashlib import sha256

from fastapi.testclient import TestClient

import app
from engine import document_store


def _client(tmp_path, monkeypatch):
    document_store._configured_store.cache_clear()
    monkeypatch.setenv("DOCUMENTS_ROOT", str(tmp_path / "documents"))
    password = "local-test-password"
    salt = "pytest-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000)
    monkeypatch.setattr(
        app,
        "_password_hash",
        f"pbkdf2_sha256$600000${salt}${base64.urlsafe_b64encode(digest).decode('ascii')}",
    )
    monkeypatch.setattr(app, "_session_secret", b"test-only-session-secret-with-extra-length")
    monkeypatch.setattr(app, "_production", False)
    client = TestClient(app.app)
    client.headers.update({"Origin": "http://testserver", "X-CSRF-Protection": "1"})
    assert client.post("/login", json={"password": password}).status_code == 200
    return client


def test_create_and_read_document_preserves_markdown_exactly(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    content = "---\ntags: [obsidian]\n---\n\n# 한글 문서\n\n본문 그대로 보존합니다.\n"

    response = client.post(
        "/documents",
        json={"title": "한글 문서", "content": content, "tags": ["개인", "AI"]},
    )

    assert response.status_code == 201
    created = response.json()
    assert len(created["id"]) == 32
    assert created["title"] == "한글 문서"
    assert created["content"] == content
    assert created["tags"] == ["개인", "AI"]
    assert created["path"] == f"documents/{created['id']}.md"
    assert created["content_hash"] == sha256(content.encode("utf-8")).hexdigest()
    assert created["created_at"] == created["updated_at"]
    stored_file = tmp_path / "documents" / f"{created['id']}.md"
    assert stored_file.is_file()
    assert content in stored_file.read_text(encoding="utf-8")

    fetched = client.get(f"/documents/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_list_documents_returns_created_documents(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    created = client.post("/documents", json={"title": "Note", "content": "# Note"}).json()

    response = client.get("/documents")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [created["id"]]


def test_update_document_replaces_content_and_preserves_created_at(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    original = client.post(
        "/documents", json={"title": "Original", "content": "# Before", "tags": ["old"]}
    ).json()

    response = client.put(
        f"/documents/{original['id']}",
        json={"title": "Updated", "content": "# 이후\n새 내용", "tags": ["new"]},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "Updated"
    assert updated["content"] == "# 이후\n새 내용"
    assert updated["tags"] == ["new"]
    assert updated["content_hash"] == sha256(updated["content"].encode("utf-8")).hexdigest()
    assert updated["created_at"] == original["created_at"]


def test_delete_document_removes_markdown_and_returns_not_found_afterward(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    created = client.post("/documents", json={"title": "Delete me", "content": "# Gone"}).json()
    stored_file = tmp_path / "documents" / f"{created['id']}.md"
    assert stored_file.exists()

    response = client.delete(f"/documents/{created['id']}")

    assert response.status_code == 204
    assert not stored_file.exists()
    assert client.get(f"/documents/{created['id']}").status_code == 404


def test_document_id_validation_blocks_path_traversal(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/documents/..%2f..%2fREADME.md")

    assert response.status_code == 404
    assert not (tmp_path / "README.md").exists()
