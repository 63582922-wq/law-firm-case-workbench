-- Durable bridge from a lawyer-confirmed Agent ledger batch to a fresh plan.
--
-- 0042 changes the authoritative matter ledger, while 0031 owns the Agent
-- event stream.  Those two version domains must not be advanced by a browser
-- request pretending to be a Worker.  This migration therefore derives one
-- server-owned refresh request from the same committed 0001 outbox event as
-- the ledger confirmation.  The unified SYSTEM_WORKER later consumes it by
-- appending CASE_SNAPSHOT_CHANGED and the existing reducer emits REPLAN.
--
-- Exception candidates deliberately keep the request blocked.  There is no
-- exception-decision ledger in this release, so no SQL or UI path may call an
-- open exception "resolved" merely to obtain a new work plan.

BEGIN;

-- Bind a refresh to the run through its immutable staged batch, instead of a
-- second direct FK to ``case_agent_runs``.  The 0042 confirmation already
-- owns the matter row when its outbox trigger inserts this request.  A direct
-- run FK would take a KEY SHARE lock in matter->run order while the Agent
-- event consumer uses run->matter order, creating an avoidable deadlock.
ALTER TABLE case_agent_ledger_extraction_batches
    ADD CONSTRAINT case_agent_ledger_extraction_batches_run_binding_unique
    UNIQUE (extraction_batch_id, run_id, firm_id, matter_id);

CREATE TABLE case_agent_snapshot_refresh_requests (
    refresh_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_outbox_id uuid NOT NULL UNIQUE REFERENCES outbox_events(outbox_id),
    source_audit_event_id uuid NOT NULL REFERENCES audit_events(event_id),
    extraction_batch_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    source_matter_version integer NOT NULL CHECK (source_matter_version > 0),
    target_matter_version integer NOT NULL CHECK (target_matter_version > 0),
    request_status text NOT NULL
        CONSTRAINT case_agent_snapshot_refresh_requests_status_check
        CHECK (request_status IN (
            'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
            'BLOCKED_BY_OPEN_EXCEPTIONS', 'APPLIED', 'SUPERSEDED'
        )),
    applied_event_id uuid,
    applied_event_sequence bigint,
    applied_by uuid,
    applied_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (refresh_request_id, firm_id, matter_id),
    UNIQUE (run_id, target_matter_version),
    FOREIGN KEY (extraction_batch_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_extraction_batches(
            extraction_batch_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (applied_event_id, firm_id, matter_id)
        REFERENCES case_agent_events(event_id, firm_id, matter_id),
    FOREIGN KEY (run_id, applied_event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id),
    FOREIGN KEY (applied_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (target_matter_version = source_matter_version + 1),
    CHECK (
        (request_status IN (
                'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
                'BLOCKED_BY_OPEN_EXCEPTIONS', 'SUPERSEDED'
            )
            AND applied_event_id IS NULL
            AND applied_event_sequence IS NULL
            AND applied_by IS NULL
            AND applied_at IS NULL)
        OR
        (request_status = 'APPLIED'
            AND applied_event_id IS NOT NULL
            AND applied_event_sequence IS NOT NULL
            AND applied_by IS NOT NULL
            AND applied_at IS NOT NULL)
    )
);

CREATE INDEX case_agent_snapshot_refresh_pending_idx
    ON case_agent_snapshot_refresh_requests (
        firm_id, request_status, created_at, refresh_request_id
    )
    WHERE request_status = 'PENDING';
CREATE INDEX case_agent_snapshot_refresh_run_idx
    ON case_agent_snapshot_refresh_requests (
        firm_id, matter_id, run_id, target_matter_version
    );

CREATE FUNCTION enqueue_case_agent_snapshot_refresh_from_ledger_confirmation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    source_run_id uuid;
    source_batch_id uuid;
    source_version integer;
    open_review_count integer;
    open_exception_count integer;
    new_refresh_request_id uuid;
BEGIN
    IF NEW.event_type <> 'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED' THEN
        RETURN NEW;
    END IF;
    IF NEW.matter_id IS NULL
       OR NEW.aggregate_version IS NULL
       OR jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
       OR NEW.payload->>'audit_event_id' IS NULL
       OR NEW.payload->>'object_id' IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation outbox binding is incomplete';
    END IF;

    SELECT batch.run_id, batch.extraction_batch_id, audit.input_version
      INTO source_run_id, source_batch_id, source_version
      FROM audit_events audit
      JOIN case_agent_ledger_extraction_batches batch
        ON batch.extraction_batch_id::text = NEW.payload->>'object_id'
       AND batch.firm_id = audit.firm_id
       AND batch.matter_id = audit.matter_id
      JOIN case_agent_ledger_extraction_batch_confirmations confirmation
        ON confirmation.extraction_batch_id = batch.extraction_batch_id
       AND confirmation.firm_id = batch.firm_id
       AND confirmation.matter_id = batch.matter_id
     WHERE audit.event_id = (NEW.payload->>'audit_event_id')::uuid
       AND audit.firm_id = NEW.firm_id
       AND audit.matter_id = NEW.matter_id
       AND audit.event_type = NEW.event_type
       AND audit.output_version = NEW.aggregate_version
       AND audit.output_version = audit.input_version + 1
       AND audit.payload->>'extraction_batch_id' = batch.extraction_batch_id::text
       AND confirmation.confirmed_matter_version = NEW.aggregate_version
       AND confirmation.confirmed_candidate_count = batch.eligible_candidate_count;

    IF source_run_id IS NULL
       OR source_batch_id IS NULL
       OR source_version IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation outbox differs from its batch and audit';
    END IF;

    SELECT
        count(*) FILTER (
            WHERE promotion.extraction_candidate_id IS NULL
        )::integer,
        count(*) FILTER (
            WHERE promotion.extraction_candidate_id IS NULL
              AND candidate.review_lane = 'EXCEPTION_REVIEW'
        )::integer
      INTO open_review_count, open_exception_count
      FROM case_agent_ledger_extraction_batches batch
      JOIN case_agent_ledger_extraction_candidates candidate
        ON candidate.extraction_batch_id = batch.extraction_batch_id
       AND candidate.firm_id = batch.firm_id
       AND candidate.matter_id = batch.matter_id
      LEFT JOIN case_agent_ledger_extraction_promotions promotion
        ON promotion.extraction_candidate_id = candidate.extraction_candidate_id
       AND promotion.extraction_batch_id = candidate.extraction_batch_id
       AND promotion.firm_id = candidate.firm_id
       AND promotion.matter_id = candidate.matter_id
     WHERE batch.run_id = source_run_id
       AND batch.firm_id = NEW.firm_id
       AND batch.matter_id = NEW.matter_id;

    INSERT INTO case_agent_snapshot_refresh_requests (
        source_outbox_id, source_audit_event_id, extraction_batch_id, run_id,
        firm_id, matter_id, source_matter_version, target_matter_version,
        request_status
    ) VALUES (
        NEW.outbox_id,
        (NEW.payload->>'audit_event_id')::uuid,
        source_batch_id,
        source_run_id,
        NEW.firm_id,
        NEW.matter_id,
        source_version,
        NEW.aggregate_version,
        CASE
            WHEN open_exception_count > 0
                THEN 'BLOCKED_BY_OPEN_EXCEPTIONS'
            WHEN open_review_count > 0
                THEN 'BLOCKED_BY_OPEN_REVIEW'
            ELSE 'PENDING'
        END
    ) RETURNING refresh_request_id INTO new_refresh_request_id;

    -- One run can own several extraction artifacts/batches.  Keep only the
    -- newest refresh cursor live; a later terminal decision represents a
    -- superset of the earlier matter versions.  The immutable source
    -- outbox/audit rows remain available on every SUPERSEDED request.
    UPDATE case_agent_snapshot_refresh_requests prior
       SET request_status = 'SUPERSEDED', updated_at = now()
     WHERE prior.run_id = source_run_id
       AND prior.firm_id = NEW.firm_id
       AND prior.matter_id = NEW.matter_id
       AND prior.refresh_request_id <> new_refresh_request_id
       AND prior.target_matter_version <= NEW.aggregate_version
       AND prior.request_status IN (
            'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
            'BLOCKED_BY_OPEN_EXCEPTIONS'
       );
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_ledger_confirmation_enqueues_snapshot_refresh
    AFTER INSERT ON outbox_events
    FOR EACH ROW
    WHEN (NEW.event_type = 'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED')
    EXECUTE FUNCTION enqueue_case_agent_snapshot_refresh_from_ledger_confirmation();

CREATE FUNCTION wake_case_agent_run_for_snapshot_refresh()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NEW.request_status <> 'PENDING' THEN
        RETURN NEW;
    END IF;
    UPDATE case_agent_run_inbox inbox
       SET inbox_status = 'READY',
           observed_event_version = run.current_event_version,
           available_at = now(),
           lease_owner = NULL,
           lease_token = NULL,
           lease_expires_at = NULL,
           inbox_version = inbox.inbox_version + 1,
           updated_at = now()
      FROM case_agent_runs run
     WHERE run.run_id = NEW.run_id
       AND run.firm_id = NEW.firm_id
       AND run.matter_id = NEW.matter_id
       AND inbox.run_id = run.run_id
       AND inbox.firm_id = run.firm_id
       AND inbox.matter_id = run.matter_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'snapshot refresh cannot wake its Agent run';
    END IF;
    PERFORM pg_notify('case_agent_run_ready', NEW.firm_id::text);
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_snapshot_refresh_wakes_worker
    AFTER INSERT OR UPDATE OF request_status
    ON case_agent_snapshot_refresh_requests
    FOR EACH ROW
    WHEN (NEW.request_status = 'PENDING')
    EXECUTE FUNCTION wake_case_agent_run_for_snapshot_refresh();

CREATE FUNCTION guard_case_agent_snapshot_refresh_request()
RETURNS trigger LANGUAGE plpgsql
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
            FROM case_agent_snapshot_refresh_requests replacement
            WHERE replacement.run_id = OLD.run_id
              AND replacement.firm_id = OLD.firm_id
              AND replacement.matter_id = OLD.matter_id
              AND replacement.refresh_request_id <> OLD.refresh_request_id
              AND replacement.target_matter_version >= OLD.target_matter_version
        )
    );
    IF NEW.refresh_request_id <> OLD.refresh_request_id
       OR NEW.source_outbox_id <> OLD.source_outbox_id
       OR NEW.source_audit_event_id <> OLD.source_audit_event_id
       OR NEW.extraction_batch_id <> OLD.extraction_batch_id
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

CREATE TRIGGER case_agent_snapshot_refresh_requests_guard
    BEFORE UPDATE OR DELETE ON case_agent_snapshot_refresh_requests
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_snapshot_refresh_request();

-- A verified graph cannot become a plan candidate while its own extraction
-- review still contains an unpromoted low-risk or exception record.  This is
-- the database backstop for the Worker's stage-before-plan orchestration.
CREATE FUNCTION block_unresolved_ledger_review_work_plan_promotion()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM case_agent_ledger_extraction_batches batch
          JOIN case_agent_ledger_extraction_candidates candidate
            ON candidate.extraction_batch_id = batch.extraction_batch_id
           AND candidate.firm_id = batch.firm_id
           AND candidate.matter_id = batch.matter_id
          LEFT JOIN case_agent_ledger_extraction_promotions promotion
            ON promotion.extraction_candidate_id = candidate.extraction_candidate_id
           AND promotion.extraction_batch_id = candidate.extraction_batch_id
           AND promotion.firm_id = candidate.firm_id
           AND promotion.matter_id = candidate.matter_id
         WHERE batch.run_id = NEW.run_id
           AND batch.firm_id = NEW.firm_id
           AND batch.matter_id = NEW.matter_id
           AND promotion.extraction_candidate_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Agent work plan promotion is blocked by open ledger review';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_work_plan_open_ledger_review_block
    BEFORE INSERT ON case_agent_work_plan_promotions
    FOR EACH ROW EXECUTE FUNCTION block_unresolved_ledger_review_work_plan_promotion();

-- Even if an older application bypasses the promotion command, activation is
-- denied for a plan whose source run still owns any unresolved 0042 record.
CREATE FUNCTION block_unresolved_ledger_review_work_plan_activation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'CANDIDATE' AND NEW.status = 'ACTIVE' AND EXISTS (
        SELECT 1
          FROM case_agent_work_plan_promotions plan_promotion
          JOIN case_agent_ledger_extraction_batches batch
            ON batch.run_id = plan_promotion.run_id
           AND batch.firm_id = plan_promotion.firm_id
           AND batch.matter_id = plan_promotion.matter_id
          JOIN case_agent_ledger_extraction_candidates candidate
            ON candidate.extraction_batch_id = batch.extraction_batch_id
           AND candidate.firm_id = batch.firm_id
           AND candidate.matter_id = batch.matter_id
          LEFT JOIN case_agent_ledger_extraction_promotions extraction_promotion
            ON extraction_promotion.extraction_candidate_id =
               candidate.extraction_candidate_id
           AND extraction_promotion.extraction_batch_id =
               candidate.extraction_batch_id
           AND extraction_promotion.firm_id = candidate.firm_id
           AND extraction_promotion.matter_id = candidate.matter_id
         WHERE plan_promotion.plan_id = NEW.plan_id
           AND plan_promotion.firm_id = NEW.firm_id
           AND plan_promotion.matter_id = NEW.matter_id
           AND extraction_promotion.extraction_candidate_id IS NULL
    ) THEN
        RAISE EXCEPTION 'work plan activation is blocked by open ledger review';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_work_plan_open_ledger_review_activation_block
    BEFORE UPDATE ON case_work_plans
    FOR EACH ROW EXECUTE FUNCTION block_unresolved_ledger_review_work_plan_activation();

ALTER TABLE case_agent_snapshot_refresh_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_snapshot_refresh_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_snapshot_refresh_requests_firm_isolation
    ON case_agent_snapshot_refresh_requests
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE case_agent_snapshot_refresh_requests FROM PUBLIC;

COMMIT;
