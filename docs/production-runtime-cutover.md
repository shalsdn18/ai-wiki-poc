# Production Runtime Preflight and Knowledge Reader Cutover

## PostgreSQL runtime diagnostic

`POST /admin/diagnostics/postgres` is a read-only connection check. It is disabled unless `POSTGRES_DIAGNOSTICS_ENABLED` is set to one of the accepted true values (`1`, `true`, `yes`, or `on`, case-insensitive) and a `POSTGRES_DIAGNOSTICS_ADMIN_TOKEN` of 32–128 ASCII letters, digits, hyphens, or underscores is configured. Keep the diagnostic token separate from the normal application login password. A random 32-byte token encoded as base64url is recommended.

The request also requires a valid existing `ai_wiki_session`, the existing CSRF/origin checks, and the exact token in `X-Postgres-Diagnostic-Admin-Token`. The current shared-password session has no user identity or role claim; the separate token is the explicit, narrow diagnostic capability. Do not treat an ordinary authenticated session as an administrator.

The diagnostic passes `connect_timeout=3` to psycopg and uses a read-only transaction with a three-second PostgreSQL statement timeout. The connection timeout applies per resolved host/address, so it is not a strict three-second total-request deadline when multiple addresses are tried. The statement timeout applies to SQL execution; the route has no separate total wall-clock deadline. It only reads PostgreSQL's numeric `server_version_num`. The response contains connection status and that version number; it never returns the database name, role, DSN, credential, or SQL error. Driver failures are converted to generic 503/504 responses and are not logged by the application.

There is no diagnostic-specific application rate limiter. Keep the flag off except for a short, operator-controlled verification window, use a high-entropy one-time capability token, perform only the needed check, then disable the flag and revoke the token. Application code does not log request headers; upstream log/trace header capture was not independently verified, so confirm redaction before enabling the capability.

| Condition | Response |
| --- | --- |
| Feature flag or valid diagnostic token absent | 404; no connection attempt |
| Application session missing/invalid | 401 |
| CSRF/origin or diagnostic capability invalid | 403 |
| PostgreSQL unavailable | 503 |
| Bounded connect/query timeout | 504 |
| Connection succeeds | 200 with `status=connected` and `server_version_num` |

No production variables are required for the default-off feature. If separately approved for runtime use, configure the enable flag and diagnostic token only on the backend service. Revoke/remove the token and disable the flag after the check. Render Free Web Services do not provide Shell, SSH, or One-Off Jobs; this endpoint is the application-level alternative and requires an app deployment to become available.

## Knowledge R2 Reader configuration

The Knowledge adapter uses a separate namespace from legacy `/documents` storage. It does not alias legacy `R2_*` settings.

Required backend variables:

- `KNOWLEDGE_STORE_BACKEND=r2`
- `KNOWLEDGE_WRITER_ENABLED=false`
- `KNOWLEDGE_R2_ENDPOINT_URL` **or** `KNOWLEDGE_R2_ACCOUNT_ID`
- `KNOWLEDGE_R2_BUCKET_NAME=ai-wiki-documents`
- `KNOWLEDGE_R2_ACCESS_KEY_ID`
- `KNOWLEDGE_R2_SECRET_ACCESS_KEY`

The `ai-wiki-documents` bucket is the checked-in live verifier's expected bucket. Confirm the real bucket before deployment. Optional defaults are `KNOWLEDGE_R2_REGION=auto`, `KNOWLEDGE_R2_PREFIX=knowledge`, `KNOWLEDGE_R2_REFRESH_SECONDS=30`, and `KNOWLEDGE_R2_STALE_MAX_SECONDS=900`. Production endpoints must use HTTPS.

Use a Cloudflare R2 `Object Read Only` credential limited to the intended bucket. That permission allows listing and reading objects in the bucket; the adapter filters its requests to `knowledge/`, but this is not a credential-level prefix boundary. If other sensitive objects share the bucket, account for the token's bucket-wide read scope. The current adapter has no session-token setting for Cloudflare prefix-scoped temporary credentials. Legacy token permissions and bucket scope must be verified before considering reuse; the legacy Documents adapter also supports object writes and deletes.

## Apply and verify after separate approval

1. Confirm the actual R2 bucket and token scope without revealing key material. Prefer a new bucket-scoped Object Read Only token; do not replace the existing legacy `R2_*` values.
2. Add the Knowledge variables to the Render backend only. Leave `POSTGRES_DATABASE_URL` and the frontend service unchanged. Keep `KNOWLEDGE_WRITER_ENABLED=false`.
3. Save and deploy the existing build so the process receives the new settings. Do not enable the Knowledge writer.
4. Confirm `/health` reports backend `r2`. Then use an authenticated, read-only `GET /knowledge/documents` to exercise manifest listing and source hydration. Also check authenticated `GET /knowledge/stats`, `/knowledge/recent`, and `/knowledge/search`; compare counts and document IDs without printing private content. The first successful cutover read must be `HEALTHY`; `STALE` is not an initial cutover pass.
5. In the test suite, keep writer-disabled routes blocked and assert that no database or R2 write calls occur. Run Knowledge R2, legacy Documents, PostgreSQL writer, API/auth, and Provider Snapshot regression tests.

Rollback if the fresh R2 read is not `HEALTHY`, an authenticated reader returns an error, expected counts/IDs are missing, content integrity fails, or private Knowledge appears in a public output. Restore `KNOWLEDGE_STORE_BACKEND=filesystem` (or remove it), keep the writer false, and redeploy. Do not change legacy `R2_*`, `DOCUMENT_STORAGE_BACKEND`, `POSTGRES_DATABASE_URL`, Provider Snapshot settings, or public publishing controls as part of this rollback.

All Render configuration changes, deployments, credential creation, and production queries require their respective separate approvals. This runbook does not itself authorize those actions.
