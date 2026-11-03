# PostgreSQL Phase 2A local operations

Phase 2A adds an opt-in persistence foundation. Production Knowledge reads still use the configured filesystem/GitHub/R2 `KnowledgeStore`; `/knowledge/*`, `/documents`, Render, and Provider Snapshot are not wired to PostgreSQL.

## Configuration

Install the pinned project requirements. Set `POSTGRES_DATABASE_URL` only in a local ignored `.env` or a secret manager when explicitly running migration or repository code. If it is absent, `get_operational_store()` fails with a sanitized configuration error; current application routes never call it and continue in legacy mode.

For integration tests, create a disposable PostgreSQL database whose name includes `test` and set `POSTGRES_TEST_DATABASE_URL` in the test process. The fixture creates and removes a uniquely named schema inside it and fails closed if the database name does not identify a test database. Do not point it at production or a database with user data. PostgreSQL itself is not installed or started by the project.

## Migration

```powershell
python -m engine.postgres_migrations
```

The runner applies checked-in `migrations/NNN_*.sql` files, records SHA-256 checksums, takes a PostgreSQL advisory lock and is safe to rerun. It rejects checksum drift and unknown recorded versions. Migration SQL is forward-only; there is no automatic destructive downgrade. Before a stateful production cutover, use a tested database backup/restore plan. Application rollback retains the schema and operational state.

## Tests

Database independent contract tests:

```powershell
pytest tests/test_postgres_operational_unit.py
```

Real PostgreSQL constraint, migration, transaction and race tests:

```powershell
pytest tests/integration/test_postgres_operational.py
```

Without `POSTGRES_TEST_DATABASE_URL`, the integration test is reported as skipped. SQLite is not used to simulate PostgreSQL constraints or locking.

## Implemented boundary

`engine.postgres_operational` provides reservation and finalization primitives but performs no R2 network calls. The caller must verify the exact source/manifest pair and pass the resulting `VerifiedRevision`; finalization independently checks keys, manifest fields, hash and Markdown metadata before committing. A CAS loser is retained as a non-current revision candidate and the intent is durably marked `CONFLICT`; it does not create a processing job or change the current pointer. No Knowledge write API, worker, R2 uploader, GC deletion, production DSN, or Render PostgreSQL service is enabled in this phase.

Canonical revision rows are protected from application-level update/delete with a PostgreSQL trigger. `tombstone_document` updates operational visibility only and retains the current pointer and R2 bytes; no delete endpoint is added. Reconciliation returns classifications only and never mutates or deletes objects.
