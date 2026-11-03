# R2 to PostgreSQL shadow qualification

This opt-in qualification scans only the `knowledge/` prefix in the configured Knowledge R2 bucket. The SDK client rejects every S3 operation except `ListObjectsV2` and `GetObject`, and the adapter exposes only those two methods. It does not print keys, document metadata, source content, endpoints, or credentials.

The scanner verifies the version 1 manifest contract, revision identity and object keys, Markdown metadata, timestamp validity, and source SHA-256. It retains validated metadata and discards source bytes after parsing. Invalid/orphan revisions are counted by classification. Current selection mirrors the existing R2 reader order `(updated_at, created_at, revision_id)`; timestamp ties are marked ambiguous and imported without an active pointer.

The importer is qualification-only. It inserts validated historical rows into a migrated disposable PostgreSQL database, records no write intents or processing jobs, and does not change application runtime configuration. The qualification script requires `POSTGRES_TEST_DATABASE_URL` to name a localhost database containing `test`; an optional `SHADOW_TEST_DATABASE_URL_B` enables independent rebuild comparison. Use disposable databases only.

Run with the repository virtual environment and ignored local `.env` configured for Knowledge R2:

```powershell
$env:POSTGRES_TEST_DATABASE_URL = "postgresql://...@127.0.0.1:5432/ai_wiki_shadow_test"
$env:SHADOW_TEST_DATABASE_URL_B = "postgresql://...@127.0.0.1:5432/ai_wiki_shadow_b_test"
python scripts/qualify_r2_postgres_shadow.py
```

The script reports aggregate counts only. A qualification against an empty `knowledge/` prefix verifies read-only listing, migrations, empty import idempotency, and deterministic empty catalogs, but does not qualify migration of live revisions. No operation is performed on `documents/`, and the script never writes or deletes R2 objects.
