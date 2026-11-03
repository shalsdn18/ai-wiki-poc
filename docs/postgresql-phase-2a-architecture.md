# PostgreSQL Phase 2A: Operational State Architecture

**Status:** design; no runtime code, database, deployment, or user data changed.

## 1. Scope and repository evidence

This design gives PostgreSQL authority over Knowledge operational state while Cloudflare R2 remains the immutable content store. Phase 2A establishes schema, transaction and recovery contracts. It does not enable PostgreSQL in production or implement a write API, worker, chunks, FTS, embeddings, or pgvector.

Repository inspection was performed at start commit `e0d58052fb5b820cdcc22a74cde4dc791127ef1e` on `main`, initially clean and equal to `origin/main`. Relevant evidence:

- `engine/r2_knowledge_store.py` validates manifest schema 1, stable document IDs of 32–64 lowercase hex characters, 64-hex revision IDs, and 64-hex SHA-256 content hashes. It reads `knowledge/{document_id}/revisions/{revision_id}/source.md` and `manifest.json`, checks source hash and Markdown/manifest metadata agreement, and is read-only. It chooses current by `(updated_at, created_at, revision_id)` descending. The last tie-break is lexical, not an authoritative pointer.
- `engine/knowledge_store.py` defines the shared read protocol and filesystem/GitHub/R2 adapters. Markdown frontmatter has title, category, tags, created, updated, visibility, optional source_url and optional stable document_id. When no stable ID exists, filesystem/GitHub derive it from the relative path.
- `docs/r2-knowledge-phase-1.md` explicitly calls the process-local metadata/body snapshots temporary caches and expects PostgreSQL to own pointers, metadata, relations and processing state.
- `scripts/verify_r2_knowledge_live.py` is an opt-in live verifier, not a write API. Its isolated objects use `knowledge/`; it checks source bytes, manifest, hash, revision preservation, reader/API behavior and cleanup.
- `tests/test_r2_knowledge_store.py` exercises fake S3, malformed manifest isolation, revision choice, read API/auth and existing adapter selection.
- `engine/r2_document_store.py` is a distinct `/documents` store at `documents/{uuid32}.md`; it mutates one object on update and physically deletes on delete. It has no Knowledge revision semantics and remains out of scope.
- `app.py` exposes authenticated read-only `/knowledge/stats`, `/knowledge/recent`, `/knowledge/search`, `/knowledge/note/{id}` and separate authenticated `/documents` CRUD. Authentication is one app-wide PBKDF2 password and HMAC-signed seven-day session cookie; mutations require same-origin/CSRF checks. There is no per-user identity table, so idempotency ownership is currently the single application principal.
- SQLite use in `engine/cache.py` and `engine/vector_store.py` is for news/cache and rebuildable semantic vectors. `engine/vector_store.py` embeds through local Ollama; it is not a Knowledge operational catalog. Existing watcher/import and local RAG paths are separate legacy app functionality.
- `requirements.txt` has no PostgreSQL driver, ORM or migration framework. `render.yaml` has only the web service and no PostgreSQL resource. `.env.example` has filesystem/GitHub/R2 settings, no PostgreSQL settings.
- `docs/knowledge-write-phase-3.md` describes a GitHub-commit write design. That plan is superseded for future R2-backed Knowledge writes by this design; it does not change current adapters or `/documents`.

## 2. Phase 1 evidence and limits

The supplied completed live run reports `LIVE R2 E2E: PASS` against bucket `ai-wiki-documents`, prefix `knowledge/`. It verified actual source/manifest write and read, SHA-256 integrity, two revisions with preservation, `R2KnowledgeStore` list/get/recent/search/stats/health, authenticated Knowledge API, malformed-manifest isolation, isolated cleanup, and no interaction with `documents/`. Regression was reported as 150 passed. This is recorded as prior run evidence, not re-executed in this documentation-only task.

Phase 1 proves the object layout and read integrity contract, not a cross-system transaction. A source can exist without its manifest; a complete manifest/source pair can exist before any PostgreSQL row. The manifest is revision-time metadata, not an operational current pointer. The current reader’s date ordering must be replaced as the authority only after a database snapshot has been validated and switched in a later rollout.

The manifest `revision_id` is generated independently and is not required to equal `content_hash`. The live verifier derives revision IDs from a run descriptor while hashing source bytes separately. Preserve this distinction: revision ID identifies an immutable revision event/object path; content hash identifies exact source bytes.

## 3. Source-of-Truth model

| Data | Classification | Authority | Duplicate / divergence rule |
|---|---|---|---|
| Original Markdown bytes | Canonical Content | Exact R2 `source.md` object at an immutable revision key | PostgreSQL stores hash, size and locator only. Never reconstruct canonical bytes from chunks. |
| Revision object identity and manifest | Canonical Content | R2 source/manifest pair; a revision is complete only after both validate against each other | PostgreSQL `revisions` registers verified facts. Mismatch or missing object means unavailable/quarantined; never silently substitute another revision. |
| Document identity | Canonical Operational State | PostgreSQL `documents.id` after bootstrap | Preserve existing 32–64 lowercase hex IDs, including path-derived IDs. Do not assume UUID-only. |
| Current content revision | Canonical Operational State | `documents.current_revision_id` in PostgreSQL | R2 date order is bootstrap/legacy fallback only. Update pointer transactionally with revision registration. |
| User-entered title/category/visibility/source_url | Canonical Operational State after write cutover; source frontmatter is canonical bytes | PostgreSQL current projection, tied to its revision; revision snapshot is immutable | During bootstrap verify frontmatter = manifest = DB projection. A future edit changes source bytes and gets a new revision. Divergence is an integrity error, not last-write-wins. |
| Tags | Canonical Operational State | PostgreSQL `tags` + `document_tags`, with immutable snapshot in each revision | Normalize for lookup while preserving display value. R2 manifest/frontmatter remains the revision-time copy. |
| Relations | Canonical Operational State | PostgreSQL `relations` | R2 does not encode relations; they cannot be rebuilt from R2 unless separately exported/backed up. |
| Visibility | Canonical Operational State | PostgreSQL current projection, with revision snapshot | API filters use PostgreSQL after cutover; do not infer from a stale manifest. |
| Processing status/errors | Canonical Operational State | PostgreSQL jobs/attempts | Not reconstructible from R2; use sanitized error codes/details. |
| Parser/chunker/embedding provenance | Canonical Operational State describing a derived generation | PostgreSQL processing attempt/index generation | Content hash and implementation/config versions must identify the exact inputs and process. |
| Chunks, FTS, embeddings | Derived / Rebuildable State | Future PostgreSQL index tables (Phase 2B) | Rebuild from R2 bytes and recorded provenance; never authority for source content. |
| Search index / indexed revision | Derived / Rebuildable State plus operational progress pointer | Derived index rows plus `current_indexed_revision_id` in PostgreSQL | Pointer means which revision has a complete active index generation, not what content is current. |
| Existing SQLite cache/vectors | Derived / Rebuildable State | No canonical authority | Preserve existing behavior during this phase; do not migrate them as source state. |

## 4. Recommended schema

Use PostgreSQL `text` identifiers with `CHECK` format constraints, `timestamptz` in UTC, JSONB only for immutable metadata/provenance snapshots, and explicit status text `CHECK`s rather than native enums for easier versioned migrations. All tables have `created_at`/`updated_at` where mutable; immutable revision and attempt facts should not be updated except narrowly documented administrative repair.

### `documents`

- PK `id text`, format `[a-f0-9]{32,64}`.
- `current_revision_id text NOT NULL` once activated; nullable only while a new document is pending its first verified revision.
- Current metadata projection: `title text NOT NULL`, `category text NOT NULL CHECK` in `ai/investment/philosophy/misc`, `visibility text NOT NULL CHECK` in `private/public`, `source_url text NULL`, `created_at`, `updated_at`, `row_version bigint NOT NULL DEFAULT 1`, `deleted_at timestamptz NULL`, `state text NOT NULL CHECK` in `PENDING/ACTIVE/DELETED/NEEDS_REVIEW`.
- Optional `current_indexed_revision_id text NULL` reserved for Phase 2B. It can point at an older revision during indexing lag. Do not claim indexed freshness from content-current state.
- Add `UNIQUE(id, current_revision_id)` only if useful for downstream references; the essential same-document constraint is the composite FK below. Index `(state, updated_at DESC)` and `(category, visibility, updated_at DESC)` for catalog filters.
- Soft delete by tombstone; no cascading deletion to R2 or revisions.

For a brand-new document, insert its `documents` row as `PENDING` with a null current pointer and an intent in the reservation transaction. During finalization, insert its first verified `revisions` row, then set the pointer and `ACTIVE` state in the same transaction. A `CHECK` should require `current_revision_id IS NOT NULL` for `ACTIVE` and `DELETED` rows; `PENDING` may have null. Existing documents are already active and always retain a current revision.

### `revisions`

- PK `(document_id text, revision_id text)`; FK `document_id → documents.id`. Create `documents` first with nullable pointer, then `revisions`, then add pointer FKs as deferred constraints.
- `revision_id` must satisfy 64 lowercase hex; `content_hash` same syntax but a separate column/meaning. Do not impose uniqueness on content hash: a revert is a new revision event, and identical bytes across documents remain separate identities.
- Immutable attributes: `parent_revision_id NULL`, `content_hash`, `byte_length`, `source_object_key`, `manifest_object_key`, `manifest_hash`, `mime_type`, `metadata_snapshot jsonb`, `manifest_schema_version`, `registered_at`.
- Unique `(document_id, source_object_key)` and `(document_id, manifest_object_key)`; globally unique object keys may also be enforced with `UNIQUE(source_object_key)` and `UNIQUE(manifest_object_key)` because R2 keys are bucket-wide. `CHECK` exact key templates against the document and revision IDs in application and preferably database constraints where manageable.
- Composite self-FK `(document_id,parent_revision_id) → revisions(document_id,revision_id)`; nullable for first revision. Index `(document_id, registered_at DESC)`.
- Store a revision only after both R2 objects have been read/verified. A failed upload belongs in intent state, not a falsely complete revision row.

### Same-document current pointer invariant

Add composite FK:

```sql
FOREIGN KEY (id, current_revision_id)
  REFERENCES revisions (document_id, revision_id)
  DEFERRABLE INITIALLY DEFERRED
```

This ensures the pointer, when non-null, targets a revision belonging to that document. A single-column FK on `revision_id` would be insufficient. For `current_indexed_revision_id`, use `(id,current_indexed_revision_id)` as the same composite FK. When either pointer is null, normal MATCH SIMPLE FK behavior skips checking it; the state CHECK above constrains the content pointer. The FK cannot prove that R2 objects still exist or that their hashes match; reconciliation does that. Make rows for referenced revisions immutable and use `RESTRICT` semantics; no `ON DELETE CASCADE` across canonical history.

### `revision_write_intents`

- PK `id uuid`; `idempotency_key text NOT NULL UNIQUE` for this single app principal; `request_digest char(64) NOT NULL`; `document_id text NOT NULL`; `revision_id char(64) NOT NULL`; `expected_current_revision_id text NULL`; exact source/manifest keys; `content_hash char(64)`; immutable metadata snapshot; `state` in `PREPARED/SOURCE_WRITTEN/COMPLETE/COMMITTED/CONFLICT/QUARANTINED`; `created_at`, `updated_at`, `last_attempt_at`, `attempt_count`, `safe_error_code NULL`.
- Unique `(document_id, revision_id)`. An intent is durable write coordination, not a completed revision. Retain terminal outcomes for idempotency replay/audit; define a long retention (for example, 1 year) before any privacy-reviewed pruning.
- No raw source body, credentials, or private content in intent rows/logs. Digest request fields deterministically.

### `tags`, `document_tags`, `relations`

- `tags`: PK `id bigint generated`, `normalized_name text NOT NULL UNIQUE`, `display_name text NOT NULL`, timestamps. Normalization (Unicode normalization + casefold + trim) must be specified and tested before writes are enabled.
- `document_tags`: PK `(document_id, tag_id)`, FKs with `ON DELETE RESTRICT`; index `(tag_id, document_id)`. Keep current tags authoritative here and snapshot them in revision metadata.
- `relations`: PK `id uuid`; `source_document_id`, `target_document_id`, `relation_type`, optional safe label, timestamps; FKs to documents, unique `(source_document_id,target_document_id,relation_type)`, check relation type against the implemented allowlist, indexes in both directions. Reject self-relations unless a later contract needs them.

### `processing_jobs`, `processing_attempts`

- `processing_jobs`: PK `id uuid`; composite FK `(document_id,revision_id)` to revisions; `pipeline_fingerprint char(64)`; `state` in `QUEUED/RUNNING/RETRY_WAIT/SUCCEEDED/FAILED/CANCELLED/SUPERSEDED`; `attempt_count`, `max_attempts`, `next_attempt_at`, `lease_owner NULL`, `lease_expires_at NULL`, `heartbeat_at NULL`, `safe_error_code NULL`, timestamps. Unique `(document_id,revision_id,pipeline_fingerprint)` prevents duplicate work for the same pipeline generation. Index `(state,next_attempt_at,created_at)` for polling and `(lease_expires_at)` for recovery.
- `processing_attempts`: PK `(job_id,attempt_number)`; FK job; `started_at`, `finished_at`, `outcome`, `safe_error_code`, bounded sanitized diagnostic; provenance fields: `parser_name/version`, `chunker_name/version`, `chunking_config_hash`, `embedding_provider/model/dimensions/version`, `source_content_hash`, and output/index generation ID if applicable. Do not collapse provenance to model name alone.
- Keep state split: R2 storage completeness is established before a `revisions` row; processing lifecycle belongs to jobs; index freshness belongs to the indexed-revision pointer. Avoid a single `STORED/PARSING/INDEXED` document enum that conflates three facts.

### Migration foundation

No migration tooling exists in the inspected dependency/config files. Phase 2A implementation should use a small versioned plain-SQL migration set and `schema_migrations(version, checksum, applied_at)`, guarded by a PostgreSQL advisory lock. Apply each migration transactionally; fail closed on checksum mismatch or unknown newer version. Use `psycopg` v3 unless a later codebase decision justifies an ORM. Keep DSN in deployment secrets/environment, never source. This is a future implementation choice, not a dependency or environment change in this design task.

## 5. Recommended write/commit protocol

Choose **Option B: durable database intent before immutable R2 upload**. It leaves an explicit retry/reconciliation handle for each expected object and lets the DB reject stale writes before spending R2 work. It does not make the DB and R2 atomic; the protocol makes uncertainty observable and recoverable.

1. Authenticate with the existing Knowledge session. A future write endpoint must also require same-origin/CSRF protection, an `Idempotency-Key`, and an `expected_current_revision_id` (null only for create). No write endpoint is part of Phase 2A design implementation.
2. Validate Markdown/frontmatter, category, visibility, tags, size and request limits. Compute `content_hash = SHA256(exact UTF-8 bytes)`. Canonically serialize request identity and compute `request_digest` over document ID, expected pointer, source hash, and normalized metadata. Revision ID is independently allocated once as a random 64-hex token; content hash is not the revision ID.
3. In a short DB transaction, look up idempotency key. Same key + same digest returns stored state/result; same key + different digest conflicts. Lock the document row (`FOR UPDATE`) or reserve a new PENDING row, compare current pointer to expected pointer, then create a `PREPARED` intent and commit. Do not hold a DB transaction open during network I/O.
4. Put immutable `source.md` at the intent's exact revision key. If the key already exists after retry, GET its bytes and verify exact expected hash/length; matching bytes continue, mismatch quarantines and never overwrites.
5. Put `manifest.json` last. Read/verify manifest identity, exact source key, metadata and source hash. Update intent progress in its own short DB transaction as useful; its state is diagnostic, not a claim of DB/R2 atomicity.
6. In one DB transaction, lock document and intent; verify the expected pointer still matches. Insert the verified immutable revision, update current metadata and `current_revision_id`, increment `row_version`, mark intent `COMMITTED`, and insert the `processing_jobs` row in the same transaction. Thus content pointer and durable job queue either both commit or neither commits.
7. If a competing write advanced the pointer during upload, register the verified revision as a non-current history candidate, mark intent `CONFLICT`, leave current content unchanged, and return 409. Do not silently promote a late upload.
8. For a current document whose exact source hash and normalized metadata equal the current revision, record a durable idempotent no-op result linked to the existing revision. If content matches an older revision but current differs, create a new revision to represent an intentional revert; duplicate content hashes are allowed.
9. Future workers can poll `processing_jobs` using short transactions and row locking. If a broker is added later, add a transactional outbox in the same DB transaction as current-pointer/job creation; do not pretend a DB commit and broker publish are atomic.

PostgreSQL row locks are held to transaction end; `FOR UPDATE` serializes competing updates of a document row. `SKIP LOCKED` is appropriate only for queue consumers, not ordinary catalog reads. See [PostgreSQL explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html) and [SELECT locking clauses](https://www.postgresql.org/docs/current/sql-select.html).

### Options

| Criterion | Option A: R2 first, then DB | Option B: DB intent, then R2, then DB finalize (recommended) |
|---|---|---|
| Crash recovery | Complete R2 orphan has no operation identity | Intent records expected keys, digest, CAS and retry state |
| Orphan risk | Source/complete object can be orphaned with no DB trace | Orphan can still occur but is attributable to a retained intent |
| Dangling DB row risk | Low for pending rows | No completed revision row until objects validate; explicit pending intent is expected |
| Idempotency | Must rediscover by scanning R2 | Unique key and request digest resolve retries directly |
| Complexity | Fewer tables/steps initially | Adds intent state and reconciliation work |
| Reconciliation | Guess intent/current from manifests | Join exact intent keys to R2 and resume or quarantine |
| Observability | Weak for ambiguous uploads | Durable operation state and safe error codes |
| Async ingestion | Hard to resume safely | Natural durable reservation/job handoff |

## 6. Failure matrix

| Failure point | Observable / canonical state | Retry | Reconciliation / loss / manual |
|---|---|---|---|
| 1. Before DB intent | No durable operation or object; current unchanged | Client may retry same key | Nothing to repair; no data loss; no manual action |
| 2. Intent committed, before source | PREPARED intent; no R2 object; current unchanged | Reuse same intent/IDs and upload | Retry while active; after bounded age mark abandoned/quarantine then GC candidate; no content loss; manual only after policy expiry |
| 3. Source succeeds, manifest fails | Intent + source-only object; previous current unchanged | Verify existing source hash, write manifest | Matching intent auto-resumes; no intent means quarantine then GC candidate after retention; no canonical revision lost |
| 4. Manifest succeeds, DB finalize not started/failed | Complete R2 pair + pending intent; current unchanged | Verify both exact objects and resume finalize | Auto-finalize only if intent/digest/CAS still valid; otherwise non-current candidate + conflict; manual for mismatch |
| 5. Revision insert succeeds but pointer step fails | Under protocol both are one DB transaction, so rollback means neither is visible | Query intent and retry final transaction | If database corruption/manual writes violate it, quarantine; no split should be produced by supported writer |
| 6. Pointer update succeeds but job enqueue fails | Pointer and job insert are one transaction; both commit or rollback | Query idempotency result | No missing job under supported path; if later external broker, transactional outbox required |
| 7. Job enqueued, process crashes | Durable QUEUED or RUNNING with lease | Worker reclaims after lease expiry; bounded attempts | Auto retry; terminal FAILED/manual retry after cap; canonical content intact |
| 8. Same request retried | Existing idempotency key/digest/outcome | Same digest returns same operation/result; changed digest conflicts | Unique constraint is decisive; no duplicate revision/job |
| 9. R2 timeout, object may have stored | Intent state uncertain; exact object may exist | HEAD/GET exact key; verify bytes/hash before continue | Auto resume if exact; mismatch quarantines; content loss only if object never stored, retryable |
| 10. DB commit timeout, transaction may have committed | Client does not know outcome | Read by idempotency key; reuse same IDs, never create another request identity | Reconciler sees committed intent/current/job atomically or retries finalize; no duplicate result |

## 7. Idempotency and revision identity

- Idempotency scope is global for the current single application principal. If per-user identities are introduced, change uniqueness to `(principal_id,idempotency_key)` before enabling multi-user writes.
- `request_digest` covers exact UTF-8 source SHA-256, document ID, expected current revision, and deterministic normalized metadata. Same key/same digest replays stored operation state. Same key/different digest returns conflict.
- Allocate document ID once and revision ID once when the intent is reserved; retries reuse them. Validate IDs against Phase 1 formats. `revision_id` is opaque and not derived from `content_hash`.
- Revision uniqueness: `(document_id,revision_id)`, and unique exact R2 keys. Do not make `(document_id,content_hash)` unique. Exact current content+metadata is no-op; prior-content revert is a new revision event; same bytes under another document remain separate.
- Job uniqueness: `(document_id,revision_id,pipeline_fingerprint)`. An explicit manual reprocess with a changed pipeline gets a new fingerprint; a retry of the same job increments attempts rather than duplicating it.

## 8. Current revision, processing and provenance

The current content revision changes as soon as its immutable R2 pair is validated and the DB finalization transaction succeeds; it does not wait on parsing or embeddings. `current_indexed_revision_id` remains null until Phase 2B has a complete searchable generation. During lag it may point to an earlier revision; reads/search must report that lag or apply a documented policy, never mislabel old results as current.

Job transitions:

```text
QUEUED → RUNNING → SUCCEEDED
                  ↘ RETRY_WAIT → RUNNING
                  ↘ FAILED
QUEUED/RETRY_WAIT → CANCELLED | SUPERSEDED
RUNNING → SUPERSEDED (worker may finish, but cannot promote stale output)
```

Workers claim with lease owner/expiry and heartbeat. Expired leases are reclaimable; attempts are append-only. Retry transient errors with bounded exponential backoff and a maximum count, then `FAILED`. Manual retry records an explicit new job generation or resets through an audited transition; it must not erase attempt history. When a newer revision becomes current, cancel queued older jobs and mark running older work superseded; a stale worker can store an inactive generation but cannot update the indexed pointer unless its revision is still eligible under the transaction check.

Provenance placement:

- Revision: source hash, exact object keys, byte length, manifest schema/hash, metadata snapshot, parent revision.
- Job/attempt: parser name/version, chunker name/version, deterministic chunking config hash, embedding provider/model/dimensions/version, source hash actually processed, start/end, outcome and safe error code.
- Future index generation/chunks: generation ID, revision ID, chunk ordinal/content hash, and a FK to the producing attempt/generation. A model name alone is insufficient to reproduce an index.

## 9. Reconciliation, garbage collection and deletion

Run reconciliation periodically and on demand, in read-only report mode first. Match exact object keys, document/revision IDs, manifest hash, source length/hash, and intents. Classifications:

| Finding | Action |
|---|---|
| Source-only with matching active intent | AUTO-REPAIR by verifying bytes and writing missing manifest on retry |
| Source-only without intent | QUARANTINE; later GC-CANDIDATE |
| Manifest-only / manifest points to missing source | QUARANTINE and mark availability/integrity issue; MANUAL restore; never serve as complete |
| Complete valid pair with matching intent and no revision row | AUTO-REPAIR finalize only if expected-current CAS remains valid; otherwise register non-current candidate and mark CONFLICT |
| Complete pair with no intent and no DB row | QUARANTINE; do not infer it should be current; MANUAL review |
| DB revision with missing source or manifest | Mark affected read unavailable/integrity incident; MANUAL restore from backup; do not repoint silently |
| Invalid pointer | Composite FK prevents supported invalid pointer; corruption/manual bypass is MANUAL and quarantine |
| Job references missing revision | FK prevents supported state; corruption/manual bypass is MANUAL |
| Duplicate/conflicting IDs or keys | Unique constraints prevent normal writes; unexpected external conflict is QUARANTINE/MANUAL |
| Expired processing lease | AUTO-REPAIR to retry if attempt budget remains; otherwise FAILED/MANUAL |

For orphan collection, use at least a 30-day grace window and two complete scans separated by at least 24 hours. Require exact-key classification, no active intent, no revision/current/index references, valid object ownership under `knowledge/`, dry-run report, and an explicit authorized cleanup run. Never broad-delete by prefix and never delete canonical revision history merely because it is absent from PostgreSQL. A document delete is a PostgreSQL tombstone and visibility change; revisions and R2 bytes are retained. Revision physical deletion is a separate exceptional retention/administrative operation. Derived index removal follows tombstone/current policy and is rebuildable. The `/documents` physical delete behavior is unrelated and unchanged.

## 10. Concurrency

Use optimistic concurrency with mandatory `expected_current_revision_id` plus a short `SELECT ... FOR UPDATE` on the `documents` row when reserving/finalizing. The row lock serializes writers; the explicit equality check is compare-and-swap protection against a stale client. If clients A and B both read X, the first successful finalization can set X→Y; the second request expecting X cannot set X→Z. Its uploaded pair may be retained as a non-current conflict candidate, but it never wins by completion time. PostgreSQL row locks are transaction-scoped, so release them before R2 calls.

This extends the old GitHub write plan's optimistic base version/no merge behavior to R2 revision IDs and content hashes. The prior Git commit/blob SHA contract is provider-specific and not the R2 write contract.

## 11. Backup and recovery

- **R2 canonical content:** enable versioning/retention or a separate protected copy/backup policy appropriate to the bucket. Database rows are not a content backup. Never assume derived chunks can restore Markdown.
- **PostgreSQL operational state:** automated encrypted backups with point-in-time recovery where available, plus periodic restore rehearsal. Back up current pointers, tags, relations, intents, jobs and attempts; retention must match personal knowledge recovery goals.
- **DB loss, R2 survives:** restore PostgreSQL backup first. With no DB backup, valid manifests/source hashes can reconstruct candidate documents and revision rows plus manifest metadata. They cannot prove the historical current pointer when multiple revisions or tied timestamps exist, nor recover relations, tombstones, idempotency outcomes, jobs, attempts or errors. Mark ambiguous documents `NEEDS_REVIEW`; never select solely by lexical revision ID as authoritative.
- **R2 loss, DB survives:** PostgreSQL can report IDs, hashes and object keys but cannot reconstruct original bytes. Restore from R2 versioning/replica/backup. Mark missing content unavailable; do not replace it with generated chunks.
- **FTS/vector/chunks loss (Phase 2B):** recreate from current R2 source using recorded pipeline provenance. Keep old indexed generation until new generation commits, then atomically advance `current_indexed_revision_id`.
- A periodic encrypted operational export may improve disaster recovery, but it is not a substitute for PG backup. R2 manifests need not duplicate mutable jobs/relations; if exported, label them as backup snapshots rather than authority.

## 12. Migration and rollout plan

1. **Schema foundation (Phase 2A):** add versioned migrations, connection/config validation, repository/catalog protocol and PostgreSQL schema behind an off-by-default selector. Preserve filesystem, GitHub and R2 adapters; preserve all existing endpoints, provider snapshot, `/documents`, watcher/import, and local RAG behavior.
2. **Read-only bootstrap dry run:** enumerate all valid R2 manifests and verify exact source bytes, SHA-256, frontmatter and metadata. Produce a report only. Group by document ID. If a single latest revision is unambiguous under dates, propose it; timestamp ties or conflicting metadata become `NEEDS_REVIEW`, not silent lexical selection. No production writes in the dry run.
3. **Shadow catalog validation:** import operational rows in a non-serving environment, compare document/revision counts, IDs, current metadata, hashes and object availability against R2. Re-run and prove idempotence; test restoring a backup. Do not change production reads.
4. **Read cutover (separate approval/deployment):** serve catalog and current pointers from PostgreSQL while hydrating canonical bytes from R2. Keep old adapters selectable for rollback/reference. Watch parity and availability.
5. **Write enablement (later separately approved phase):** use the intent-first protocol and R2 immutable keys. Once a PostgreSQL pointer has been promoted by the new writer, timestamp-only R2 reader is not a safe source of current state. Rollback means reverting application version while retaining PostgreSQL state, not switching back to legacy date selection.
6. **Phase 2B:** add chunks, FTS, pgvector and indexing pipeline only after content-current and indexed-current semantics are operationally proven.

Do not migrate `/documents` into this catalog as a side effect. Do not remove GitHub/filesystem adapters until their datasets and rollback path are explicitly retired in a later task. The existing GitHub-oriented `docs/knowledge-write-phase-3.md` must be marked superseded or updated before a future write implementation to avoid contradictory write contracts.

## 13. Phase boundary and final recommendation

**Phase 2A includes:** PostgreSQL operational schema, migration foundation, catalog adapter contract, current pointer, revision registration, idempotent intent protocol, processing job/attempt/provenance contracts, concurrency, reconciliation and backup/restore plan.

**Phase 2B includes:** chunk tables, FTS, pgvector, embedding generation, hybrid retrieval and reranking experiments. Do not introduce these in Phase 2A.

### Option comparison

| Factor | Minimal (`documents`, `revisions`, `processing_jobs`) | Durable (recommended) |
|---|---|---|
| Correctness | Pointer FK possible; weak operation trace | Same constraints plus explicit partial-write and retry state |
| Crash recovery | Requires scans/guessing for ambiguous R2 writes | Intent keys/digests allow exact resume or quarantine |
| Schema complexity | Lower | Moderate: intent, attempts, tags/relations |
| Code complexity | Lower initially, more bespoke repair later | More upfront validation, simpler repeatable recovery |
| Observability | Sparse | Safe operation and processing histories |
| Future async scale | Ad hoc | Durable polling queue; outbox only if broker arrives |
| Personal system fit | Fine for prototype with accepted manual recovery | Best balance for long-lived private knowledge and recoverability |

**Recommendation:** adopt the durable intent-first design, but keep the runtime narrow: no broker, no worker implementation, no vector/FTS, no cross-user abstraction, no physical canonical revision deletion. Use PostgreSQL for operational truth and R2 for immutable bytes. Keep content pointer transition and durable job creation in one DB transaction, and treat every cross-store uncertainty as an explicit intent for retry/reconciliation.

## References

- Repository Phase 1 contract: [`r2-knowledge-phase-1.md`](r2-knowledge-phase-1.md)
- Existing write plan requiring reconciliation: [`knowledge-write-phase-3.md`](knowledge-write-phase-3.md)
- PostgreSQL official docs: [constraints](https://www.postgresql.org/docs/current/ddl-constraints.html), [explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html), [SELECT locking clauses](https://www.postgresql.org/docs/current/sql-select.html).
