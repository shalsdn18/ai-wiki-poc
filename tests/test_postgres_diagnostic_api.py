import base64
import hashlib

import psycopg
import pytest
from fastapi.testclient import TestClient

import app
from engine import postgres_operational


PASSWORD = "test-only-password"
ADMIN_TOKEN = "diagnostic-admin-capability-test-token-43chars"
ORIGIN = "http://testserver"
CSRF_HEADERS = {"Origin": ORIGIN, "X-CSRF-Protection": "1"}
DIAGNOSTIC_PATH = "/admin/diagnostics/postgres"


def _configure_auth(monkeypatch):
    salt = "diagnostic-test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt.encode(), 600_000)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        f"pbkdf2_sha256$600000${salt}${base64.urlsafe_b64encode(digest).decode('ascii')}",
    )
    monkeypatch.setenv("SESSION_SECRET", "diagnostic-test-session-signing-secret")
    monkeypatch.setattr(app, "_production", False)


def _enable_diagnostic(monkeypatch, token=ADMIN_TOKEN):
    monkeypatch.setenv("POSTGRES_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_DIAGNOSTICS_ADMIN_TOKEN", token)


def _authenticated_client(monkeypatch):
    _configure_auth(monkeypatch)
    client = TestClient(app.app)
    response = client.post("/login", json={"password": PASSWORD}, headers=CSRF_HEADERS)
    assert response.status_code == 200
    return client


def _diagnostic_headers(token=ADMIN_TOKEN):
    return {**CSRF_HEADERS, "X-Postgres-Diagnostic-Admin-Token": token}


def test_postgres_diagnostic_is_disabled_by_default_and_does_not_connect(monkeypatch):
    monkeypatch.delenv("POSTGRES_DIAGNOSTICS_ENABLED", raising=False)
    monkeypatch.delenv("POSTGRES_DIAGNOSTICS_ADMIN_TOKEN", raising=False)
    called = False

    def unexpected_connect():
        nonlocal called
        called = True

    monkeypatch.setattr(
        postgres_operational, "diagnose_postgres_connection_read_only", unexpected_connect
    )

    response = TestClient(app.app).post(DIAGNOSTIC_PATH)

    assert response.status_code == 404
    assert called is False


def test_postgres_diagnostic_requires_existing_authenticated_session(monkeypatch):
    _enable_diagnostic(monkeypatch)
    _configure_auth(monkeypatch)
    called = False

    def unexpected_connect():
        nonlocal called
        called = True

    monkeypatch.setattr(
        postgres_operational, "diagnose_postgres_connection_read_only", unexpected_connect
    )

    response = TestClient(app.app).post(
        DIAGNOSTIC_PATH, headers=_diagnostic_headers()
    )

    assert response.status_code == 401
    assert called is False


def test_postgres_diagnostic_requires_separate_admin_capability(monkeypatch):
    _enable_diagnostic(monkeypatch)
    client = _authenticated_client(monkeypatch)
    called = False

    def unexpected_connect():
        nonlocal called
        called = True

    monkeypatch.setattr(
        postgres_operational, "diagnose_postgres_connection_read_only", unexpected_connect
    )

    missing = client.post(DIAGNOSTIC_PATH, headers=CSRF_HEADERS)
    wrong = client.post(DIAGNOSTIC_PATH, headers=_diagnostic_headers("wrong-admin-token"))

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert called is False


def test_postgres_diagnostic_requires_csrf_and_same_origin(monkeypatch):
    _enable_diagnostic(monkeypatch)
    client = _authenticated_client(monkeypatch)
    called = False

    def unexpected_connect():
        nonlocal called
        called = True

    monkeypatch.setattr(
        postgres_operational, "diagnose_postgres_connection_read_only", unexpected_connect
    )

    response = client.post(
        DIAGNOSTIC_PATH,
        headers={"X-Postgres-Diagnostic-Admin-Token": ADMIN_TOKEN},
    )

    assert response.status_code == 403
    assert called is False


def test_postgres_diagnostic_success_returns_only_connection_status_and_version(monkeypatch):
    _enable_diagnostic(monkeypatch)
    client = _authenticated_client(monkeypatch)
    monkeypatch.setattr(
        postgres_operational, "diagnose_postgres_connection_read_only", lambda: 180006
    )

    response = client.post(DIAGNOSTIC_PATH, headers=_diagnostic_headers())

    assert response.status_code == 200
    assert response.json() == {"status": "connected", "server_version_num": 180006}
    assert ADMIN_TOKEN not in response.text
    assert "current_user" not in response.text
    assert "database" not in response.json()


def test_postgres_diagnostic_connection_failure_is_sanitized(monkeypatch, caplog):
    _enable_diagnostic(monkeypatch)
    client = _authenticated_client(monkeypatch)
    secret_detail = "dsn=postgresql://db-user:db-password@db.invalid/private raw SQL detail"

    def fail_connection():
        raise postgres_operational.OperationalStoreError(secret_detail)

    monkeypatch.setattr(
        postgres_operational, "diagnose_postgres_connection_read_only", fail_connection
    )

    response = client.post(DIAGNOSTIC_PATH, headers=_diagnostic_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "PostgreSQL diagnostic unavailable"}
    assert secret_detail not in response.text
    assert secret_detail not in caplog.text
    assert ADMIN_TOKEN not in caplog.text


def test_postgres_diagnostic_timeout_is_sanitized(monkeypatch, caplog):
    _enable_diagnostic(monkeypatch)
    client = _authenticated_client(monkeypatch)
    sensitive_detail = "timeout for db-user password=sensitive raw SQL detail"

    def time_out():
        raise postgres_operational.OperationalStoreTimeout(sensitive_detail)

    monkeypatch.setattr(
        postgres_operational, "diagnose_postgres_connection_read_only", time_out
    )

    response = client.post(DIAGNOSTIC_PATH, headers=_diagnostic_headers())

    assert response.status_code == 504
    assert response.json() == {"detail": "PostgreSQL diagnostic timed out"}
    assert sensitive_detail not in response.text
    assert sensitive_detail not in caplog.text
    assert ADMIN_TOKEN not in caplog.text


def test_postgres_diagnostic_rejects_missing_or_short_admin_secret(monkeypatch):
    monkeypatch.setenv("POSTGRES_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.delenv("POSTGRES_DIAGNOSTICS_ADMIN_TOKEN", raising=False)
    assert app._postgres_diagnostics_configured() is False
    monkeypatch.setenv("POSTGRES_DIAGNOSTICS_ADMIN_TOKEN", "short")
    assert app._postgres_diagnostics_configured() is False
    monkeypatch.setenv("POSTGRES_DIAGNOSTICS_ADMIN_TOKEN", "a" * 129)
    assert app._postgres_diagnostics_configured() is False
    monkeypatch.setenv("POSTGRES_DIAGNOSTICS_ADMIN_TOKEN", "é" * 32)
    assert app._postgres_diagnostics_configured() is False


def test_postgres_diagnostic_helper_uses_read_only_transaction_and_bounded_timeouts(monkeypatch):
    statements = []
    closed = False
    connect_timeouts = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement):
            statements.append(statement)

        def fetchone(self):
            return ("180006",)

    class Connection:
        def transaction(self):
            class Transaction:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

            return Transaction()

        def cursor(self):
            return Cursor()

        def close(self):
            nonlocal closed
            closed = True

    def connect_from_environment(connect_timeout):
        connect_timeouts.append(connect_timeout)
        return Connection()

    monkeypatch.setattr(postgres_operational, "connect_from_environment", connect_from_environment)

    assert postgres_operational.diagnose_postgres_connection_read_only() == 180006
    assert connect_timeouts == [3]
    assert statements == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = '3000ms'",
        "SELECT current_setting('server_version_num')",
    ]
    assert closed is True


def test_postgres_connection_helper_maps_driver_timeouts_without_raw_details(monkeypatch):
    monkeypatch.setenv(
        "POSTGRES_DATABASE_URL", "postgresql://db-user:db-password@db.invalid/private"
    )
    secret_detail = "connection timeout dsn=postgresql://db-user:db-password@db.invalid/private"

    def timeout(*args, **kwargs):
        raise psycopg.errors.ConnectionTimeout(secret_detail)

    monkeypatch.setattr(psycopg, "connect", timeout)

    with pytest.raises(postgres_operational.OperationalStoreTimeout) as error:
        postgres_operational.connect_from_environment(connect_timeout=3)

    assert secret_detail not in str(error.value)


def test_postgres_connection_helper_maps_connection_errors_without_raw_details(monkeypatch):
    monkeypatch.setenv(
        "POSTGRES_DATABASE_URL", "postgresql://db-user:db-password@db.invalid/private"
    )
    secret_detail = "db-password=secret raw SQL detail"

    def fail(*args, **kwargs):
        raise psycopg.OperationalError(secret_detail)

    monkeypatch.setattr(psycopg, "connect", fail)

    with pytest.raises(postgres_operational.OperationalStoreError) as error:
        postgres_operational.connect_from_environment(connect_timeout=3)

    assert not isinstance(error.value, postgres_operational.OperationalStoreTimeout)
    assert secret_detail not in str(error.value)


def test_postgres_diagnostic_helper_maps_query_timeout_and_closes_connection(monkeypatch):
    closed = False

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement):
            if statement.startswith("SELECT"):
                raise psycopg.errors.QueryCanceled("sensitive query error and DSN")

        def fetchone(self):
            return None

    class Connection:
        def transaction(self):
            class Transaction:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

            return Transaction()

        def cursor(self):
            return Cursor()

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        postgres_operational, "connect_from_environment", lambda connect_timeout: Connection()
    )

    with pytest.raises(postgres_operational.OperationalStoreTimeout) as error:
        postgres_operational.diagnose_postgres_connection_read_only()

    assert "sensitive query error" not in str(error.value)
    assert closed is True
