import base64
import hashlib

from fastapi.testclient import TestClient

import app

PASSWORD = "test-only-password"
ORIGIN = "http://testserver"
CSRF_HEADERS = {"Origin": ORIGIN, "X-CSRF-Protection": "1"}


def _configure_auth(monkeypatch, production=False):
    salt = "fixed-test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt.encode(), 600_000)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        f"pbkdf2_sha256$600000${salt}${base64.urlsafe_b64encode(digest).decode('ascii')}",
    )
    monkeypatch.setenv("SESSION_SECRET", "test-only-signing-secret-with-extra-length")
    monkeypatch.setattr(app, "_production", production)


def test_auth_status_and_login_cookie(monkeypatch):
    _configure_auth(monkeypatch)
    client = TestClient(app.app)

    assert client.get("/auth/status").json() == {"authenticated": False, "configured": True}
    denied = client.post("/login", json={"password": "wrong"}, headers=CSRF_HEADERS)
    assert denied.status_code == 401

    response = client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie
    assert client.get("/auth/status").json()["authenticated"] is True


def test_production_session_cookie_is_secure_and_cross_site_compatible(monkeypatch):
    _configure_auth(monkeypatch, production=True)
    response = TestClient(app.app).post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=none" in cookie


def test_every_document_method_requires_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENTS_ROOT", str(tmp_path / "documents"))
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    payload = {"title": "Private", "content": "secret", "tags": []}

    assert client.get("/documents").status_code == 401
    assert client.get("/documents/00000000000000000000000000000000").status_code == 401
    assert client.post("/documents", json=payload, headers=CSRF_HEADERS).status_code == 401
    assert (
        client.put(
            "/documents/00000000000000000000000000000000", json=payload, headers=CSRF_HEADERS
        ).status_code
        == 401
    )
    assert (
        client.delete(
            "/documents/00000000000000000000000000000000", headers=CSRF_HEADERS
        ).status_code
        == 401
    )


def test_knowledge_api_requires_session_and_configured_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    _configure_auth(monkeypatch)
    client = TestClient(app.app)

    for path in (
        "/knowledge/stats",
        "/knowledge/recent",
        "/knowledge/search?q=secret",
        "/knowledge/note/unknown",
    ):
        assert client.get(path).status_code == 401

    monkeypatch.delenv("APP_PASSWORD_HASH")
    monkeypatch.delenv("SESSION_SECRET")
    assert client.get("/knowledge/stats").status_code == 503


def test_authenticated_knowledge_api_can_read_private_document(tmp_path, monkeypatch):
    knowledge = tmp_path / "knowledge" / "ai"
    knowledge.mkdir(parents=True)
    (knowledge / "private.md").write_text(
        "---\n"
        "title: Private note\n"
        "category: ai\n"
        "tags: [secret]\n"
        'created: "2026-01-01"\n'
        'updated: "2026-02-01"\n'
        "visibility: private\n"
        "---\n\nPrivate source text.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    assert (
        client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 200
    )

    response = client.get("/knowledge/recent")

    assert response.status_code == 200
    assert response.json()[0]["visibility"] == "private"
    assert response.json()[0]["content"] == "Private source text."
    assert client.get("/knowledge/stats").json()["documents"] == 1
    search = client.get("/knowledge/search", params={"q": "source text"})
    assert search.json()[0]["title"] == "Private note"
    document_id = response.json()[0]["id"]
    assert client.get(f"/knowledge/note/{document_id}").json()["content"] == "Private source text."


def test_knowledge_api_does_not_fallback_to_public_wiki_when_private_source_is_unavailable(
    monkeypatch,
):
    from engine.github_knowledge_store import GitHubKnowledgeStore

    class FailedTransport:
        def request_json(self, method, url, headers, body=None):
            raise RuntimeError("private source token=must-not-appear")

    class Credentials:
        def get_token(self):
            return "installation-token"

    store = GitHubKnowledgeStore(
        "owner",
        "private",
        "main",
        credential_provider=Credentials(),
        transport=FailedTransport(),
    )
    monkeypatch.setattr(app, "get_knowledge_store", lambda: store)
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    assert (
        client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 200
    )

    response = client.get("/knowledge/stats")

    assert response.status_code == 503
    assert "must-not-appear" not in response.text
    assert "temporarily unavailable" in response.json()["detail"]


def test_missing_github_credentials_produce_unavailable_not_server_error(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_STORE_BACKEND", "github")
    monkeypatch.setenv("KNOWLEDGE_GITHUB_OWNER", "owner")
    monkeypatch.setenv("KNOWLEDGE_GITHUB_REPO", "private")
    for name in (
        "KNOWLEDGE_GITHUB_APP_ID",
        "KNOWLEDGE_GITHUB_INSTALLATION_ID",
        "KNOWLEDGE_GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    assert (
        client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 200
    )

    response = client.get("/knowledge/stats")

    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]


def test_document_mutations_require_csrf_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENTS_ROOT", str(tmp_path / "documents"))
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    assert (
        client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 200
    )
    payload = {"title": "Private", "content": "secret", "tags": []}

    missing_token = client.post("/documents", json=payload, headers={"Origin": ORIGIN})
    wrong_origin = client.post(
        "/documents",
        json=payload,
        headers={"Origin": "https://attacker.example", "X-CSRF-Protection": "1"},
    )

    assert missing_token.status_code == 403
    assert wrong_origin.status_code == 403
    assert list((tmp_path / "documents").glob("*.md")) == []


def test_logout_invalidates_session_and_requires_csrf(monkeypatch):
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    assert (
        client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 200
    )

    rejected = client.post("/logout")
    assert rejected.status_code == 403
    assert client.get("/auth/status").json()["authenticated"] is True

    response = client.post("/logout", headers=CSRF_HEADERS)
    assert response.status_code == 200
    assert client.get("/auth/status").json()["authenticated"] is False


def test_auth_fails_closed_when_secrets_are_not_configured(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    client = TestClient(app.app)

    assert client.get("/auth/status").json() == {"authenticated": False, "configured": False}
    assert client.get("/documents").status_code == 503
    assert (
        client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 503
    )


def test_auth_status_reads_environment_loaded_after_app_import(monkeypatch):
    """Runner-provided env-file values remain visible even after app was imported."""
    salt = "runner-env-salt"
    digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt.encode(), 600_000)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        f"pbkdf2_sha256$600000${salt}${base64.urlsafe_b64encode(digest).decode('ascii')}",
    )
    monkeypatch.setenv("SESSION_SECRET", "runner-env-session-secret-with-sufficient-length")

    response = TestClient(app.app).get("/auth/status")

    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "configured": True}
