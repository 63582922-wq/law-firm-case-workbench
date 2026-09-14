BEGIN;

-- 0049 made ACTIVE exception follow-ups a hard work-plan gate, but the older
-- 0047 refresh helpers could still promote a blocked snapshot request as soon
-- as every extraction group had a routing decision.  A routing decision that
-- creates REEXTRACTION, MORE_EVIDENCE, or DEFERRED_REVIEW work is not the same
-- as completing that work.  Keep refreshes blocked until the current heads are
-- terminal, while preserving the original duplicate-only fast path.
SET LOCAL ROLE lawcase_schema_owner;

CREATE OR REPLACE FUNCTION public.guard_case_agent_snapshot_refresh_request()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE transition_valid boolean := false;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'case Agent snapshot refresh transition is invalid';
    END IF;
    transition_valid := (
        OLD.request_status = 'PENDING'
        AND NEW.request_status = 'APPLIED'
        AND NEW.applied_event_id IS NOT NULL
        AND NEW.applied_event_sequence IS NOT NULL
        AND NEW.applied_by IS NOT NULL
        AND NEW.applied_at IS NOT NULL
    ) OR (
        OLD.request_status IN (
            'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
            'BLOCKED_BY_OPEN_EXCEPTIONS'
        )
        AND NEW.request_status = 'SUPERSEDED'
        AND EXISTS (
            SELECT 1
              FROM public.case_agent_snapshot_refresh_requests replacement
             WHERE replacement.run_id = OLD.run_id
               AND replacement.firm_id = OLD.firm_id
               AND replacement.matter_id = OLD.matter_id
               AND replacement.refresh_request_id <> OLD.refresh_request_id
               AND replacement.target_matter_version >=
                    OLD.target_matter_version
        )
    ) OR (
        OLD.request_status IN (
            'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
        )
        AND NEW.request_status = 'PENDING'
        AND public.case_agent_ledger_extraction_run_review_resolved(
            OLD.run_id, OLD.firm_id, OLD.matter_id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads head
             WHERE head.firm_id = OLD.firm_id
               AND head.matter_id = OLD.matter_id
               AND head.current_state = 'ACTIVE'
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_snapshot_refresh_requests newer
             WHERE newer.run_id = OLD.run_id
               AND newer.firm_id = OLD.firm_id
               AND newer.matter_id = OLD.matter_id
               AND newer.target_matter_version > OLD.target_matter_version
        )
    );
    IF NEW.refresh_request_id <> OLD.refresh_request_id
       OR NEW.source_outbox_id <> OLD.source_outbox_id
       OR NEW.source_audit_event_id <> OLD.source_audit_event_id
       OR NEW.extraction_batch_id IS DISTINCT FROM OLD.extraction_batch_id
       OR NEW.control_assignment_id IS DISTINCT FROM
            OLD.control_assignment_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.source_matter_version <> OLD.source_matter_version
       OR NEW.target_matter_version <> OLD.target_matter_version
       OR NOT transition_valid
       OR NEW.updated_at <= OLD.updated_at THEN
        RAISE EXCEPTION 'case Agent snapshot refresh transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION
    public.unblock_case_agent_snapshot_refresh_after_exception_decision()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    -- Non-duplicate routes create an ACTIVE follow-up in the later 0049
    -- lifecycle trigger.  Trigger ordering must not expose a PENDING refresh
    -- in the gap before that lifecycle row is inserted.
    IF NEW.decision <> 'REJECT_AS_DUPLICATE'
       OR EXISTS (
            SELECT 1
              FROM case_agent_ledger_exception_followup_heads head
             WHERE head.firm_id = NEW.firm_id
               AND head.matter_id = NEW.matter_id
               AND head.current_state = 'ACTIVE'
       ) THEN
        RETURN NEW;
    END IF;
    IF NOT case_agent_ledger_extraction_run_review_resolved(
        NEW.run_id, NEW.firm_id, NEW.matter_id
    ) THEN
        RETURN NEW;
    END IF;
    UPDATE case_agent_snapshot_refresh_requests request
       SET request_status = 'SUPERSEDED', updated_at = now()
     WHERE request.run_id = NEW.run_id
       AND request.firm_id = NEW.firm_id
       AND request.matter_id = NEW.matter_id
       AND request.request_status IN (
           'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
           'BLOCKED_BY_OPEN_EXCEPTIONS'
       )
       AND request.target_matter_version < (
           SELECT max(latest.target_matter_version)
             FROM case_agent_snapshot_refresh_requests latest
            WHERE latest.run_id = NEW.run_id
              AND latest.firm_id = NEW.firm_id
              AND latest.matter_id = NEW.matter_id
       );
    UPDATE case_agent_snapshot_refresh_requests request
       SET request_status = 'PENDING', updated_at = now()
     WHERE request.run_id = NEW.run_id
       AND request.firm_id = NEW.firm_id
       AND request.matter_id = NEW.matter_id
       AND request.request_status IN (
           'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
       )
       AND request.target_matter_version = (
           SELECT max(latest.target_matter_version)
             FROM case_agent_snapshot_refresh_requests latest
            WHERE latest.run_id = NEW.run_id
              AND latest.firm_id = NEW.firm_id
              AND latest.matter_id = NEW.matter_id
       );
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION
    public.normalize_case_agent_snapshot_refresh_review_gate()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NEW.request_status IN (
        'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
    ) AND case_agent_ledger_extraction_run_review_resolved(
        NEW.run_id, NEW.firm_id, NEW.matter_id
    ) AND NOT EXISTS (
        SELECT 1
          FROM case_agent_ledger_exception_followup_heads head
         WHERE head.firm_id = NEW.firm_id
           AND head.matter_id = NEW.matter_id
           AND head.current_state = 'ACTIVE'
    ) AND NOT EXISTS (
        SELECT 1 FROM case_agent_snapshot_refresh_requests newer
         WHERE newer.run_id = NEW.run_id
           AND newer.firm_id = NEW.firm_id
           AND newer.matter_id = NEW.matter_id
           AND newer.target_matter_version > NEW.target_matter_version
    ) THEN
        UPDATE case_agent_snapshot_refresh_requests
           SET request_status = 'PENDING', updated_at = now()
         WHERE refresh_request_id = NEW.refresh_request_id
           AND request_status IN (
               'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
           );
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION
    public.gate_case_agent_snapshot_refresh_insert_for_active_followup()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.request_status = 'PENDING'
       AND EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads head
             WHERE head.firm_id = NEW.firm_id
               AND head.matter_id = NEW.matter_id
               AND head.current_state = 'ACTIVE'
       ) THEN
        NEW.request_status := 'BLOCKED_BY_OPEN_EXCEPTIONS';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION
    public.gate_case_agent_snapshot_refresh_insert_for_active_followup()
    FROM PUBLIC;

CREATE TRIGGER aa_case_agent_snapshot_refresh_active_followup_gate
    BEFORE INSERT ON public.case_agent_snapshot_refresh_requests
    FOR EACH ROW EXECUTE FUNCTION
        public.gate_case_agent_snapshot_refresh_insert_for_active_followup();

COMMIT;
