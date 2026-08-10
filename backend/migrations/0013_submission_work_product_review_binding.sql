-- Bind lawyer approval to the exact immutable candidate output and provenance.
-- Legacy candidate rows cannot be approved until regenerated under this rule.

BEGIN;

ALTER TABLE submission_work_products
    ADD COLUMN review_input_hash char(64);

ALTER TABLE submission_work_products
    ADD CONSTRAINT submission_work_products_review_input_hash_valid
    CHECK (review_input_hash IS NULL OR review_input_hash ~ '^[0-9a-f]{64}$');

CREATE INDEX submission_work_products_review_input_idx
    ON submission_work_products (matter_id, review_input_hash)
    WHERE review_input_hash IS NOT NULL;

COMMIT;
