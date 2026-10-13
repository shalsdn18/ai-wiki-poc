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
    monkeypatch.setattr(
        app,
        "_password_hash",
        f"pbkdf2_sha256$600000${salt}${base64.urlsafe_b64encode(digest).decode('ascii')}",
    )
    monkeypatch.setattr(app, "_session_secret", b"test-only-signing-secret-with-extra-length")
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
    response = TestClient(app.app).post(
        "/login", json={"password": PASSWORD}, headers=CSRF_HEADERS
    )

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
    assert client.put(
        "/documents/00000000000000000000000000000000", json=payload, headers=CSRF_HEADERS
    ).status_code == 401
    assert client.delete(
        "/documents/00000000000000000000000000000000", headers=CSRF_HEADERS
    ).status_code == 401


def test_document_mutations_require_csrf_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENTS_ROOT", str(tmp_path / "documents"))
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    assert client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 200
    payload = {"title": "Private", "content": "secret", "tags": []}

    missing_token = client.post("/documents", json=payload, headers={"Origin": ORIGIN})
    wrong_origin = client.post(
        "/documents", json=payload, headers={"Origin": "https://attacker.example", "X-CSRF-Protection": "1"}
    )

    assert missing_token.status_code == 403
    assert wrong_origin.status_code == 403
    assert list((tmp_path / "documents").glob("*.md")) == []


def test_logout_invalidates_session_and_requires_csrf(monkeypatch):
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    assert client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 200

    rejected = client.post("/logout")
    assert rejected.status_code == 403
    assert client.get("/auth/status").json()["authenticated"] is True

    response = client.post("/logout", headers=CSRF_HEADERS)
    assert response.status_code == 200
    assert client.get("/auth/status").json()["authenticated"] is False


def test_auth_fails_closed_when_secrets_are_not_configured(monkeypatch):
    monkeypatch.setattr(app, "_password_hash", "")
    monkeypatch.setattr(app, "_session_secret", b"")
    client = TestClient(app.app)

    assert client.get("/auth/status").json() == {"authenticated": False, "configured": False}
    assert client.get("/documents").status_code == 503
    assert client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS).status_code == 503
