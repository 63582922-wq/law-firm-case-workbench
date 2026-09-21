-- A complete provider HTTP/response rejection is terminal, not indeterminate.
-- Preserve only a controlled error code; provider bodies and exception text
-- remain outside both the OCR ledger and the Agent event stream.

BEGIN;

ALTER TABLE case_agent_visual_ocr_outcomes
    DROP CONSTRAINT case_agent_visual_ocr_outcomes_status_check,
    DROP CONSTRAINT case_agent_visual_ocr_outcomes_check;

ALTER TABLE case_agent_visual_ocr_outcomes
    ADD CONSTRAINT case_agent_visual_ocr_outcomes_status_check CHECK (
        status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION')
    ),
    ADD CONSTRAINT case_agent_visual_ocr_outcomes_check CHECK (
        (status = 'SUCCEEDED'
         AND provider_request_id ~ '^[A-Za-z0-9._:-]{1,500}$'
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
         AND error_code = 'QWEN_VISUAL_OCR_OUTCOME_UNKNOWN')
    );

COMMENT ON CONSTRAINT case_agent_visual_ocr_outcomes_check
    ON case_agent_visual_ocr_outcomes IS
    'Complete terminal provider failures retain only a controlled code; uncertain transport remains UNKNOWN_SUBMISSION.';

COMMIT;
