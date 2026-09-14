-- PostgreSQL's ARE engine rejects repetition bounds above 255.  The prior
-- provider id guard used {1,500}, so a valid Qwen response reached the worker
-- but the append-only success receipt could not be inserted.  Keep the same
-- 500-character contract by separating character-class and length checks.

BEGIN;

ALTER TABLE case_agent_visual_ocr_outcomes
    DROP CONSTRAINT case_agent_visual_ocr_outcomes_check;

ALTER TABLE case_agent_visual_ocr_outcomes
    ADD CONSTRAINT case_agent_visual_ocr_outcomes_check CHECK (
        (status = 'SUCCEEDED'
         AND provider_request_id ~ '^[A-Za-z0-9._:-]+$'
         AND char_length(provider_request_id) BETWEEN 1 AND 500
         AND response_sha256 IS NOT NULL
         AND response_bytes IS NOT NULL
         AND octet_length(response_body) = response_bytes
         AND encode(digest(response_body, 'sha256'), 'hex') = response_sha256
         AND error_code IS NULL)
        OR
        (status = 'FAILED'
         AND provider_request_id IS NULL
         AND response_sha256 IS NULL
         AND response_bytes IS NULL
         AND response_body IS NULL
         AND error_code ~ '^QWEN_VISUAL_OCR_[A-Z0-9_]{3,57}$')
        OR
        (status = 'UNKNOWN_SUBMISSION'
         AND provider_request_id IS NULL
         AND response_sha256 IS NULL
         AND response_bytes IS NULL
         AND response_body IS NULL
         AND error_code IN (
             'QWEN_VISUAL_OCR_OUTCOME_UNKNOWN',
             'QWEN_VISUAL_OCR_UNKNOWN_DNS',
             'QWEN_VISUAL_OCR_UNKNOWN_CONNECT',
             'QWEN_VISUAL_OCR_UNKNOWN_SEND',
             'QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD',
             'QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_BODY'
         ))
    );

COMMENT ON CONSTRAINT case_agent_visual_ocr_outcomes_check
    ON case_agent_visual_ocr_outcomes IS
    'Qwen success receipts use a PostgreSQL-safe character guard plus an explicit 1..500 provider id length bound; unknown requests remain lookup-only.';

COMMIT;
