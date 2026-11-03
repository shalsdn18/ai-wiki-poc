# Phase 4A: Canonical Knowledge Writer

## Authority and lifecycle

The writer is an opt-in authenticated path for new Knowledge documents. Markdown source revisions are immutable objects in the R2 `knowledge/` namespace. PostgreSQL owns the active document pointer, write intent, tombstone state, tag projection, and durable processing job. The legacy `/documents` API and R2 `documents/` object are unchanged and are not imported by this writer.

For create and update, the service validates and normalizes the request, reserves a PostgreSQL intent, conditionally creates `source.md`, reads it back and verifies exact bytes and SHA-256, conditionally creates `manifest.json`, reads and validates the pair, then calls `finalize_verified_revision`. PostgreSQL atomically registers the immutable revision, performs current-pointer compare-and-swap, commits the intent, updates tags, and queues one processing job. No worker is implemented in this phase; `QUEUED` is expected.

## Create, update, retry, and no-op

New document IDs are 32–64 lowercase hex characters generated once by the client using a cryptographically secure UUID. Retries retain both that ID and the `Idempotency-Key`. Revision IDs are 64 lowercase hex characters reserved once in PostgreSQL. The existing request digest binds the document ID, expected current revision, exact Markdown hash, and canonical metadata. Reusing an idempotency key for a different request returns a conflict.

Updates require `expected_current_revision_id`; stale clients receive HTTP 409. There is no last-write-wins or automatic merge. An update with identical normalized Markdown body and canonical metadata records a `NOOP` result pointing at the existing revision and queues no job. Changes to title, category, tags, visibility, or source URL create a new revision even when body text is unchanged. Dates are preserved on idempotent retry.

R2 supports conditional `If-None-Match: *` writes. A preexisting key with identical bytes is accepted as an exact retry; different bytes are a hard conflict. If a write response is uncertain, exact read-back resolves only an exact match; otherwise the operation remains retryable with its same intent and revision. A source-only object can remain after manifest failure. A complete R2 pair can remain after PostgreSQL finalize failure. The service does not delete either object as rollback. An immutable-key mismatch is quarantined.

## Delete and restore

Delete is a PostgreSQL tombstone with current-revision compare-and-swap. It never issues an R2 delete. Revisions and objects remain available for audit, while the active Knowledge read path hides the document. Repeating the same tombstone request is idempotent. Restore is not implemented; a deleted ID cannot be recreated or updated through the writer.

## API and publication boundary

The new API is `GET/POST /knowledge/documents`, `PUT/DELETE /knowledge/documents/{id}`, alongside the existing authenticated `/knowledge/*` readers. Writes require the existing session cookie, same-origin/CSRF header, and (for create/update) `Idempotency-Key`. `KNOWLEDGE_WRITER_ENABLED` defaults to false. Enable it only after configuring PostgreSQL and Knowledge R2 with conditional-write support. Reads use PostgreSQL current pointers when enabled and fail closed on backend errors; they do not fall back to the old snapshot.

`visibility=public` is metadata only. Saving a Knowledge document never edits `wiki/`, starts legacy publication, or changes Provider Snapshot behavior. Public Wiki publishing remains a separate explicit workflow. The existing `/documents` CRUD keeps its previous local/R2 behavior for compatibility.

## Qualification and limitations

Unit tests use a conditional-write fake. PostgreSQL integration tests require a disposable local database whose name contains `test`, supplied only through `POSTGRES_TEST_DATABASE_URL`; never point that variable at production or user data. The controlled live script is `scripts/verify_knowledge_writer_live.py` and refuses to run without `--confirm-live-r2`. It creates a unique Knowledge document, verifies create/update/no-op/tombstone and retained revisions, and deletes only R2 keys whose conditional PUT succeeded during that run. It never enumerates or touches `documents/`.

This phase does not implement indexing/embedding workers, restore, hard deletion, public publishing, historical import, or changes to Provider Snapshot. Do not enable the writer in a deployment until disposable PostgreSQL integration and controlled R2 qualification pass.
