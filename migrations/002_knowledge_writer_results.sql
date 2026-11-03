ALTER TABLE revision_write_intents
    ADD COLUMN result_revision_id text NULL;

ALTER TABLE revision_write_intents
    ADD CONSTRAINT revision_write_intents_result_revision_fk
    FOREIGN KEY (document_id, result_revision_id)
    REFERENCES revisions(document_id, revision_id)
    ON DELETE RESTRICT;

ALTER TABLE revision_write_intents
    DROP CONSTRAINT revision_write_intents_state_check;

ALTER TABLE revision_write_intents
    ADD CONSTRAINT revision_write_intents_state_check
    CHECK (state IN ('PREPARED','SOURCE_WRITTEN','COMPLETE','COMMITTED','CONFLICT','QUARANTINED','NOOP'));

ALTER TABLE revision_write_intents
    ADD CONSTRAINT revision_write_intents_result_state_check
    CHECK (
        (state = 'NOOP' AND result_revision_id IS NOT NULL)
        OR (state <> 'NOOP' AND result_revision_id IS NULL)
    );
