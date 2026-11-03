CREATE TABLE documents (
    id text PRIMARY KEY CHECK (id ~ '^[a-f0-9]{32,64}$'),
    current_revision_id text NULL,
    current_indexed_revision_id text NULL,
    title text NOT NULL,
    category text NOT NULL CHECK (category IN ('ai','investment','philosophy','misc')),
    visibility text NOT NULL CHECK (visibility IN ('private','public')),
    source_url text NULL,
    state text NOT NULL CHECK (state IN ('PENDING','ACTIVE','DELETED','NEEDS_REVIEW')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    deleted_at timestamptz NULL,
    CHECK ((state IN ('PENDING','NEEDS_REVIEW') AND current_revision_id IS NULL)
        OR (state IN ('ACTIVE','DELETED') AND current_revision_id IS NOT NULL)),
    CHECK ((state = 'DELETED' AND deleted_at IS NOT NULL) OR state <> 'DELETED')
);

CREATE TABLE revisions (
    document_id text NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    revision_id text NOT NULL CHECK (revision_id ~ '^[a-f0-9]{64}$'),
    parent_revision_id text NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length >= 0),
    source_object_key text NOT NULL UNIQUE,
    manifest_object_key text NOT NULL UNIQUE,
    manifest_hash text NOT NULL CHECK (manifest_hash ~ '^[a-f0-9]{64}$'),
    mime_type text NOT NULL CHECK (mime_type LIKE 'text/markdown%'),
    metadata_snapshot jsonb NOT NULL CHECK (jsonb_typeof(metadata_snapshot) = 'object'),
    manifest_schema_version integer NOT NULL CHECK (manifest_schema_version > 0),
    registered_at timestamptz NOT NULL,
    PRIMARY KEY (document_id, revision_id),
    FOREIGN KEY (document_id, parent_revision_id)
        REFERENCES revisions(document_id, revision_id) ON DELETE RESTRICT,
    CHECK (source_object_key = 'knowledge/' || document_id || '/revisions/' || revision_id || '/source.md'),
    CHECK (manifest_object_key = 'knowledge/' || document_id || '/revisions/' || revision_id || '/manifest.json')
);

ALTER TABLE documents ADD CONSTRAINT documents_current_revision_fk
    FOREIGN KEY (id, current_revision_id)
    REFERENCES revisions(document_id, revision_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE documents ADD CONSTRAINT documents_current_indexed_revision_fk
    FOREIGN KEY (id, current_indexed_revision_id)
    REFERENCES revisions(document_id, revision_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX documents_state_updated_idx ON documents(state, updated_at DESC);
CREATE INDEX documents_category_visibility_updated_idx ON documents(category, visibility, updated_at DESC);
CREATE INDEX revisions_document_registered_idx ON revisions(document_id, registered_at DESC);

CREATE TABLE revision_write_intents (
    id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_digest text NOT NULL CHECK (request_digest ~ '^[a-f0-9]{64}$'),
    document_id text NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    revision_id text NOT NULL CHECK (revision_id ~ '^[a-f0-9]{64}$'),
    expected_current_revision_id text NULL,
    source_object_key text NOT NULL UNIQUE,
    manifest_object_key text NOT NULL UNIQUE,
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    metadata_snapshot jsonb NOT NULL CHECK (jsonb_typeof(metadata_snapshot) = 'object'),
    state text NOT NULL CHECK (state IN ('PREPARED','SOURCE_WRITTEN','COMPLETE','COMMITTED','CONFLICT','QUARANTINED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_attempt_at timestamptz NULL,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    safe_error_code text NULL,
    UNIQUE(document_id, revision_id),
    FOREIGN KEY (document_id, expected_current_revision_id)
        REFERENCES revisions(document_id, revision_id) ON DELETE RESTRICT,
    CHECK (source_object_key = 'knowledge/' || document_id || '/revisions/' || revision_id || '/source.md'),
    CHECK (manifest_object_key = 'knowledge/' || document_id || '/revisions/' || revision_id || '/manifest.json')
);
CREATE INDEX revision_write_intents_state_updated_idx ON revision_write_intents(state, updated_at);

CREATE TABLE tags (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalized_name text NOT NULL UNIQUE CHECK (length(normalized_name) > 0),
    display_name text NOT NULL CHECK (length(display_name) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE document_tags (
    document_id text NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    tag_id bigint NOT NULL REFERENCES tags(id) ON DELETE RESTRICT,
    PRIMARY KEY(document_id, tag_id)
);
CREATE INDEX document_tags_tag_document_idx ON document_tags(tag_id, document_id);

CREATE TABLE relations (
    id uuid PRIMARY KEY,
    source_document_id text NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    target_document_id text NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    relation_type text NOT NULL CHECK (relation_type IN ('related','supports','contradicts','references')),
    label text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (source_document_id <> target_document_id),
    UNIQUE(source_document_id, target_document_id, relation_type)
);
CREATE INDEX relations_target_source_idx ON relations(target_document_id, source_document_id);

CREATE TABLE processing_jobs (
    id uuid PRIMARY KEY,
    document_id text NOT NULL,
    revision_id text NOT NULL,
    pipeline_fingerprint text NOT NULL CHECK (pipeline_fingerprint ~ '^[a-f0-9]{64}$'),
    state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED','CANCELLED','SUPERSEDED')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    next_attempt_at timestamptz NULL,
    lease_owner text NULL,
    lease_expires_at timestamptz NULL,
    heartbeat_at timestamptz NULL,
    safe_error_code text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(document_id, revision_id) REFERENCES revisions(document_id, revision_id) ON DELETE RESTRICT,
    UNIQUE(document_id, revision_id, pipeline_fingerprint)
);
CREATE INDEX processing_jobs_poll_idx ON processing_jobs(state, next_attempt_at, created_at);
CREATE INDEX processing_jobs_lease_idx ON processing_jobs(lease_expires_at);

CREATE TABLE processing_attempts (
    job_id uuid NOT NULL REFERENCES processing_jobs(id) ON DELETE RESTRICT,
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    started_at timestamptz NOT NULL,
    finished_at timestamptz NULL,
    outcome text NULL CHECK (outcome IS NULL OR outcome IN ('SUCCEEDED','RETRY_WAIT','FAILED','CANCELLED','SUPERSEDED')),
    safe_error_code text NULL,
    safe_diagnostic text NULL CHECK (safe_diagnostic IS NULL OR length(safe_diagnostic) <= 2000),
    parser_name text NULL,
    parser_version text NULL,
    chunker_name text NULL,
    chunker_version text NULL,
    chunking_config_hash text NULL CHECK (chunking_config_hash IS NULL OR chunking_config_hash ~ '^[a-f0-9]{64}$'),
    embedding_provider text NULL,
    embedding_model text NULL,
    embedding_dimensions integer NULL CHECK (embedding_dimensions IS NULL OR embedding_dimensions > 0),
    embedding_version text NULL,
    source_content_hash text NULL CHECK (source_content_hash IS NULL OR source_content_hash ~ '^[a-f0-9]{64}$'),
    index_generation_id uuid NULL,
    PRIMARY KEY(job_id, attempt_number)
);

CREATE FUNCTION reject_revision_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'canonical revision records are immutable';
END;
$$;
CREATE TRIGGER revisions_immutable BEFORE UPDATE OR DELETE ON revisions
    FOR EACH ROW EXECUTE FUNCTION reject_revision_mutation();
