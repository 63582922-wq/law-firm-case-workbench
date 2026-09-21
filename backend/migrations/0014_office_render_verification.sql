-- Immutable raster verification receipt for Office-to-PDF evidence rendering.
-- Applies after 0013_submission_work_product_review_binding.sql.  Existing
-- image/text representations legitimately have no Office renderer receipt.

BEGIN;

ALTER TABLE evidence_normalized_representations
    ADD COLUMN render_verification_hash char(64)
    CHECK (render_verification_hash IS NULL OR render_verification_hash ~ '^[0-9a-f]{64}$');

COMMIT;
