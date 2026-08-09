-- Explicit license/authorized-use review for every newly registered official source.
-- Apply after 0008_official_source_capture_runs.sql.

BEGIN;

ALTER TABLE official_legal_source_snapshots
    ADD COLUMN license_basis text,
    ADD COLUMN license_review_hash char(64)
        CHECK (license_review_hash IS NULL OR license_review_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT official_legal_source_license_review_pair
        CHECK (
            (license_basis IS NULL AND license_review_hash IS NULL)
            OR (length(trim(license_basis)) > 0 AND license_review_hash IS NOT NULL)
        );

COMMENT ON COLUMN official_legal_source_snapshots.license_basis IS
    'Lawyer-reviewed public access, storage and internal-use basis; legacy rows without this pair cannot support new rule approvals.';

COMMIT;
