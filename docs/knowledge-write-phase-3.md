# Phase 3: Knowledge write architecture decisions

The Phase 2 GitHub knowledge adapters are read-only. `/documents` remains a
separate authenticated CRUD store until the decisions below are implemented.

## Required write contract

- **Optimistic concurrency:** each edit carries the base commit SHA and the
  document blob SHA (or equivalent content hash). Reject writes if either no
  longer matches the current branch version.
- **Conflict handling:** return a conflict response with no automatic merge.
  The client reloads the latest Markdown and asks the user to reconcile it.
- **Commit message:** use a stable, non-sensitive template such as
  `knowledge: update <repository-relative-path>`; never place document body,
  title, or private source text in logs.
- **Atomic write:** create a GitHub blob, tree, and commit, then update the
  branch ref with `force=false`. Never update the public app repository.
- **Push failure:** retain no partially published state; report a generic
  unavailable/conflict result and leave the current branch unchanged.
- **Rollback:** revert by creating a new commit based on an explicitly selected
  prior version. Do not reset or force-push branch history.
- **Authorization:** keep writes behind the authenticated Knowledge API and
  require GitHub App Contents write permission only when the write feature is
  approved and ready. Phase 2 credentials remain read-only.

## UI boundary

The future Documents UI should call an authenticated knowledge write API. It
must not receive GitHub credentials or call GitHub directly. Preserve `/documents`
until the UI is migrated and the new write semantics have been verified.
