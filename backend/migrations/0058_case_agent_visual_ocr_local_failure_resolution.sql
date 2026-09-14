-- Preserve the original UNKNOWN_SUBMISSION while allowing an administrator
-- to append a narrowly evidenced local-receipt failure resolution.  This is
-- lookup-only: it cannot create a provider result or resend the request.

BEGIN;

CREATE TABLE case_agent_visual_ocr_local_failure_resolutions (
    resolution_id uuid PRIMARY KEY,
    exchange_id uuid NOT NULL,
    external_request_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    error_code text NOT NULL CHECK (
        error_code = 'QWEN_VISUAL_OCR_LOCAL_RECEIPT_PERSISTENCE_FAILED'
    ),
    evidence_kind text NOT NULL CHECK (
        evidence_kind = 'POSTGRES_ERROR_LOG'
    ),
    evidence_sha256 char(64) NOT NULL CHECK (
        evidence_sha256 ~ '^[0-9a-f]{64}$'
    ),
    recorded_by_worker uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (exchange_id),
    UNIQUE (external_request_id),
    UNIQUE (resolution_id, firm_id, matter_id),
    FOREIGN KEY (exchange_id, external_request_id, firm_id, matter_id)
        REFERENCES case_agent_visual_ocr_exchanges(
            exchange_id, external_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (recorded_by_worker, firm_id)
        REFERENCES users(user_id, firm_id)
);

CREATE FUNCTION validate_case_agent_visual_ocr_local_failure_resolution()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    outcome_row record;
BEGIN
    SELECT outcome.status, outcome.error_code
      INTO outcome_row
      FROM case_agent_visual_ocr_outcomes outcome
     WHERE outcome.exchange_id = NEW.exchange_id
       AND outcome.external_request_id = NEW.external_request_id
       AND outcome.firm_id = NEW.firm_id
       AND outcome.matter_id = NEW.matter_id;

    IF outcome_row.status IS DISTINCT FROM 'UNKNOWN_SUBMISSION'
       OR outcome_row.error_code IS DISTINCT FROM
          'QWEN_VISUAL_OCR_OUTCOME_UNKNOWN'
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
          IS DISTINCT FROM NEW.recorded_by_worker
       OR NOT EXISTS (
            SELECT 1
              FROM users worker
              JOIN matter_actor_roles worker_role
                ON worker_role.user_id = worker.user_id
               AND worker_role.firm_id = worker.firm_id
             WHERE worker.user_id = NEW.recorded_by_worker
               AND worker.firm_id = NEW.firm_id
               AND worker.status = 'ACTIVE'
               AND worker_role.matter_id = NEW.matter_id
               AND worker_role.role = 'SYSTEM_WORKER'
               AND worker_role.revoked_at IS NULL
       ) THEN
        RAISE EXCEPTION 'visual OCR local failure resolution is not authorized';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_visual_ocr_local_failure_resolution_guard
    BEFORE INSERT ON case_agent_visual_ocr_local_failure_resolutions
    FOR EACH ROW EXECUTE FUNCTION
        validate_case_agent_visual_ocr_local_failure_resolution();

CREATE TRIGGER case_agent_visual_ocr_local_failure_resolutions_append_only
    BEFORE UPDATE OR DELETE
    ON case_agent_visual_ocr_local_failure_resolutions
    FOR EACH ROW EXECUTE FUNCTION
        prohibit_case_agent_visual_ocr_ledger_change();

ALTER TABLE case_agent_visual_ocr_local_failure_resolutions
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_visual_ocr_local_failure_resolutions
    FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_visual_ocr_local_failure_resolutions_firm_isolation
    ON case_agent_visual_ocr_local_failure_resolutions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE case_agent_visual_ocr_local_failure_resolutions
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker,
         lawcase_agent_verifier;
GRANT SELECT ON TABLE case_agent_visual_ocr_local_failure_resolutions
    TO lawcase_agent_worker;

COMMENT ON TABLE case_agent_visual_ocr_local_failure_resolutions IS
    'Append-only administrator evidence that a generic unknown Qwen call failed after provider return while the local success receipt was being persisted; never a provider result and never permission to resend.';

COMMIT;
