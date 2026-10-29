# R2 Knowledge storage: Phase 1 boundary

## Responsibilities

- Cloudflare R2 is the immutable source and revision object store.
- `R2KnowledgeStore` is a read-only `KnowledgeStore` adapter. It supports list,
  get, recent (through the existing list route), search, stats, and health.
- PostgreSQL is not introduced in this phase. The R2 adapter's in-memory
  metadata snapshot and hydrated content snapshot are temporary per-process
  query caches, not authoritative operational state.
- The separate `R2Store` used by authenticated `/documents` CRUD is unchanged.
  Knowledge R2 has its own `KNOWLEDGE_R2_*` configuration and does not expose
  Knowledge write APIs.

## Immutable revision layout

Each complete Markdown revision uses:

```text
knowledge/{document_id}/revisions/{revision_id}/source.md
knowledge/{document_id}/revisions/{revision_id}/manifest.json
```

`document_id` is a stable lowercase hex identifier (32 to 64 characters).
`revision_id` is an opaque immutable lowercase 64-hex identifier. The manifest's
`content_hash` is the SHA-256 of the exact UTF-8 source bytes. The manifest is
JSON schema version 1 and carries `document_id`, `revision_id`, `title`,
`category`, `tags`, `visibility`, optional `source_url`, `mime_type`,
`content_hash`, `object_key`, `created_at`, and `updated_at`. `object_key` must
point to the matching revision's `source.md`; arbitrary bucket keys are rejected.
The Markdown frontmatter remains the existing contract (`title`, `category`,
`tags`, `created`, `updated`, `visibility`, optional `source_url`). An optional
`document_id` frontmatter field supports stable IDs for future authoring;
existing documents without it keep their repository-path-derived IDs in the
filesystem and GitHub adapters.

The reader lists only manifest objects under the configured knowledge prefix,
selects one current revision per document by `updated_at`, then
`created_at`, then `revision_id`, and verifies source bytes against
`content_hash` before returning content. This selection rule is temporary until
PostgreSQL owns the current-revision pointer. No R2 write API or ingestion job is
added in this phase.

## Refresh, cache, and failure behavior

The process-local metadata snapshot is refreshed on the configured interval.
R2 manifest-key listing metadata (`ETag`, `LastModified`, and size) is compared
to the previous snapshot; unchanged manifests are not fetched again. Markdown
objects are fetched only when an operation needs full domain documents. Stats
uses manifests alone. A partial manifest refresh never replaces the last
complete snapshot. A complete cached snapshot may be served as `STALE` for at
most `KNOWLEDGE_R2_STALE_MAX_SECONDS`; after that the store returns unavailable.
Malformed manifests and malformed source documents are isolated and counted.
The cache is process-local and not shared across Render instances; it is not a
durable index and does not provide cross-instance consistency.

At larger corpus sizes, PostgreSQL should replace both cache/index duties and
per-process current-revision selection. Until then, periodic manifest listing
still grows with the number of revisions; it avoids scanning source binaries on
metadata refresh but is not the final scale strategy.

## PostgreSQL handoff and consistency

PostgreSQL is expected to own documents, revisions, current-revision pointers,
tags, relations, processing state, chunks, full-text search, and pgvector
embeddings. R2 remains the source object store.

The future write sequence should upload an immutable `source.md`, verify its
hash, upload its manifest last, and then commit the document/revision/current
pointer transaction to PostgreSQL. A failure before the manifest leaves an
unreferenced object; a failure after the manifest but before the database
transaction leaves an unreferenced complete revision. Neither case changes the
previous current revision. A reconciler can find those objects by comparing R2
manifests with PostgreSQL and apply a retention policy before garbage
collection. PostgreSQL metadata/indexes can be rebuilt by enumerating valid R2
manifests, validating each source hash and frontmatter, then reprocessing
derived chunks and embeddings; the source Markdown is never reconstructed from
derived data.

## Production configuration

Set `KNOWLEDGE_STORE_BACKEND=r2`, then configure the server environment with
`KNOWLEDGE_R2_ENDPOINT_URL` (or `KNOWLEDGE_R2_ACCOUNT_ID`),
`KNOWLEDGE_R2_BUCKET_NAME`, `KNOWLEDGE_R2_ACCESS_KEY_ID`, and
`KNOWLEDGE_R2_SECRET_ACCESS_KEY`. Optional settings are
`KNOWLEDGE_R2_REGION` (default `auto`), `KNOWLEDGE_R2_PREFIX` (default
`knowledge`), `KNOWLEDGE_R2_REFRESH_SECONDS` (default 30), and
`KNOWLEDGE_R2_STALE_MAX_SECONDS` (default 900). Credentials belong only in an
ignored local `.env` or the deployment secret manager. Health responses and
normalized errors do not include endpoints, keys, or SDK error details.

Render deployment settings and real R2 credentials are operational work and
are not changed by this code phase.
