-- Atomic recovery intent and run-inbox fencing for the 0049 control cursor.
--
-- A replacement Agent run is created by the ordinary event-sourced control
-- plane, so its RUN_CREATED transaction cannot also move the 0049 control
-- head.  This migration makes that gap safe instead of pretending it is
-- atomic: an immutable, session-authorized recovery intent is committed
-- first; every run wake/claim treats an untransferred intent as non-runnable;
-- and the eventual transfer parks the old inbox and activates the replacement
-- in the same transaction as the control-head change.

BEGIN;

-- This upgrade must close the old create-run/claim window before it scans for
-- legacy orphans.  These write-conflicting table locks make the scan and the
-- replacement wake trigger one cutover: concurrent old Web/Worker writes
-- finish before the scan or wait until the new database fences are active.
LOCK TABLE
    public.case_agent_runs,
    public.case_agent_run_inbox,
    public.case_agent_ledger_exception_control_assignments,
    public.case_agent_ledger_exception_control_heads
    IN SHARE ROW EXCLUSIVE MODE;

-- 0049 replaces this 0046 helper after its early upgrade hardening block.
-- Re-assert the trusted lookup path for already-upgraded and fresh databases.
ALTER FUNCTION public.guard_case_agent_snapshot_refresh_request()
    SET search_path = pg_catalog, public, pg_temp;

-- Deferred 0042 constraint triggers execute when the Web transaction forces
-- constraints, after the 0048 definer command has returned.  Bind them to the
-- isolated lifecycle owner so they do not require exposing internal evidence
-- comparison helpers directly to the Web role.
ALTER FUNCTION public.enforce_case_agent_ledger_extraction_confirmation_integrity()
    SECURITY DEFINER;
ALTER FUNCTION public.enforce_case_agent_ledger_extraction_confirmation_integrity()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()
    SECURITY DEFINER;
ALTER FUNCTION public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()
    OWNER TO lawcase_ledger_confirmation_owner;

CREATE TABLE public.case_agent_ledger_exception_recovery_intents (
    recovery_intent_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    source_control_assignment_id uuid NOT NULL,
    replacement_run_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    prepared_web_session_id uuid NOT NULL,
    expected_matter_version integer NOT NULL CHECK (expected_matter_version > 0),
    idempotency_key text NOT NULL CHECK (
        idempotency_key ~ '^[A-Za-z0-9._~-]{16,128}$'
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    prepared_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (recovery_intent_id, firm_id, matter_id),
    UNIQUE (firm_id, matter_id, actor_id, idempotency_key),
    UNIQUE (firm_id, matter_id, replacement_run_id),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id),
    FOREIGN KEY (source_control_assignment_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_control_assignments(
            control_assignment_id, firm_id, matter_id
        ),
    FOREIGN KEY (actor_id, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (prepared_web_session_id)
        REFERENCES public.web_sessions(session_id)
);

CREATE TABLE public.case_agent_ledger_exception_recovery_intent_heads (
    recovery_intent_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    current_outcome text NOT NULL CHECK (
        current_outcome IN ('PENDING', 'TRANSFERRED', 'ABANDONED')
    ),
    transfer_control_assignment_id uuid,
    outcome_reason_code text CHECK (
        outcome_reason_code IS NULL
        OR outcome_reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'
    ),
    outcome_actor_id uuid,
    outcome_web_session_id uuid,
    outcome_at timestamptz,
    outcome_version integer NOT NULL DEFAULT 1 CHECK (outcome_version > 0),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (recovery_intent_id, firm_id, matter_id),
    FOREIGN KEY (recovery_intent_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_recovery_intents(
            recovery_intent_id, firm_id, matter_id
        ),
    FOREIGN KEY (transfer_control_assignment_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_control_assignments(
            control_assignment_id, firm_id, matter_id
        ),
    FOREIGN KEY (outcome_actor_id, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (outcome_web_session_id)
        REFERENCES public.web_sessions(session_id),
    CHECK (
        (current_outcome = 'PENDING'
            AND transfer_control_assignment_id IS NULL
            AND outcome_reason_code IS NULL
            AND outcome_actor_id IS NULL
            AND outcome_web_session_id IS NULL
            AND outcome_at IS NULL
            AND outcome_version = 1)
        OR (current_outcome = 'TRANSFERRED'
            AND transfer_control_assignment_id IS NOT NULL
            AND outcome_reason_code = 'RECOVER_FAILED_CONTROL_RUN'
            AND outcome_actor_id IS NOT NULL
            AND outcome_web_session_id IS NOT NULL
            AND outcome_at IS NOT NULL
            AND outcome_version = 2)
        OR (current_outcome = 'ABANDONED'
            AND transfer_control_assignment_id IS NULL
            AND outcome_reason_code IN (
                'SUPERSEDED_BY_NEW_RECOVERY_INTENT',
                'MATTER_VERSION_CHANGED',
                'CONTROL_AUTHORITY_ENDED',
                'CONTROL_AUTHORITY_CHANGED',
                'RECOVERY_ACTOR_CHANGED',
                'REPLACEMENT_RUN_INVALID'
            )
            AND outcome_actor_id IS NOT NULL
            AND outcome_web_session_id IS NOT NULL
            AND outcome_at IS NOT NULL
            AND outcome_version = 2)
    )
);

CREATE UNIQUE INDEX case_agent_ledger_exception_one_pending_recovery
    ON public.case_agent_ledger_exception_recovery_intent_heads(firm_id, matter_id)
    WHERE current_outcome = 'PENDING';

CREATE INDEX case_agent_ledger_exception_recovery_run_lookup
    ON public.case_agent_ledger_exception_recovery_intents(
        firm_id, matter_id, replacement_run_id
    );

CREATE INDEX case_agent_ledger_exception_recovery_source_history
    ON public.case_agent_ledger_exception_recovery_intents(
        source_control_assignment_id, prepared_at
    );

-- Quarantine the narrow pre-0050 crash window.  Older Web code could commit
-- the exact server-owned recovery RUN_CREATED before it durably transferred
-- control.  These immutable rows keep such an upgrade-time orphan parked
-- even after every ACTIVE follow-up later reaches a terminal state.
CREATE TABLE public.case_agent_ledger_exception_recovery_quarantines (
    run_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    source_control_assignment_id uuid NOT NULL,
    reason_code text NOT NULL CHECK (
        reason_code = 'LEGACY_PRE_INTENT_RECOVERY_RUN'
    ),
    quarantined_at timestamptz NOT NULL DEFAULT
        pg_catalog.clock_timestamp(),
    UNIQUE (run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES public.case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (source_control_assignment_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_control_assignments(
            control_assignment_id, firm_id, matter_id
        )
);

CREATE FUNCTION public.prohibit_case_agent_ledger_exception_recovery_intent_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'case Agent ledger exception recovery intents are append-only';
END;
$$;

CREATE FUNCTION public.guard_case_agent_ledger_exception_recovery_intent_head()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v2
    IF TG_OP = 'DELETE'
       OR NEW.recovery_intent_id <> OLD.recovery_intent_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR OLD.current_outcome <> 'PENDING'
       OR NEW.current_outcome NOT IN ('TRANSFERRED', 'ABANDONED')
       OR NEW.outcome_version <> OLD.outcome_version + 1
       OR NEW.updated_at <= OLD.updated_at
       OR NEW.outcome_at IS NULL
       OR NEW.outcome_actor_id IS NULL
       OR NEW.outcome_web_session_id IS NULL
       OR NOT EXISTS (
            SELECT 1
              FROM public.web_sessions session
              JOIN public.users actor
                ON actor.user_id = session.user_id
               AND actor.firm_id = session.firm_id
               AND actor.status = 'ACTIVE'
              JOIN public.matter_actor_roles role_binding
                ON role_binding.matter_id = NEW.matter_id
               AND role_binding.firm_id = NEW.firm_id
               AND role_binding.user_id = session.user_id
               AND role_binding.role = 'LEAD_LAWYER'
               AND role_binding.revoked_at IS NULL
             WHERE session.session_id = NEW.outcome_web_session_id
               AND session.firm_id = NEW.firm_id
               AND session.user_id = NEW.outcome_actor_id
               AND session.revoked_at IS NULL
               AND session.expires_at > NEW.outcome_at
               AND session.authenticated_at <= NEW.outcome_at
               AND session.issuer LIKE 'https://%'
       )
       OR (NEW.current_outcome = 'TRANSFERRED' AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_recovery_intents intent
              JOIN public.case_agent_ledger_exception_control_assignments assignment
                ON assignment.control_assignment_id =
                        NEW.transfer_control_assignment_id
               AND assignment.firm_id = intent.firm_id
               AND assignment.matter_id = intent.matter_id
               AND assignment.control_run_id = intent.replacement_run_id
               AND assignment.supersedes_control_assignment_id =
                        intent.source_control_assignment_id
               AND assignment.transition_type = 'TRANSFERRED'
               AND assignment.state_after = 'HEALTHY'
               AND assignment.actor_id = NEW.outcome_actor_id
               AND assignment.web_session_id = NEW.outcome_web_session_id
               AND assignment.reason_code = NEW.outcome_reason_code
             WHERE intent.recovery_intent_id = NEW.recovery_intent_id
               AND intent.firm_id = NEW.firm_id
               AND intent.matter_id = NEW.matter_id
       ))
       OR (NEW.current_outcome = 'ABANDONED' AND (
            NEW.transfer_control_assignment_id IS NOT NULL
            OR NEW.outcome_reason_code NOT IN (
                'SUPERSEDED_BY_NEW_RECOVERY_INTENT',
                'MATTER_VERSION_CHANGED',
                'CONTROL_AUTHORITY_ENDED',
                'CONTROL_AUTHORITY_CHANGED',
                'RECOVERY_ACTOR_CHANGED',
                'REPLACEMENT_RUN_INVALID'
            )
       )) THEN
        RAISE EXCEPTION
            'case Agent ledger exception recovery outcome transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_ledger_exception_recovery_intents_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_recovery_intents
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_recovery_intent_mutation();

CREATE TRIGGER case_agent_ledger_exception_recovery_quarantines_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_recovery_quarantines
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_recovery_intent_mutation();

CREATE TRIGGER case_agent_ledger_exception_recovery_intent_heads_guard
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_recovery_intent_heads
    FOR EACH ROW EXECUTE FUNCTION
        public.guard_case_agent_ledger_exception_recovery_intent_head();

INSERT INTO public.case_agent_ledger_exception_recovery_quarantines (
    run_id, firm_id, matter_id, source_control_assignment_id, reason_code
)
SELECT run.run_id, run.firm_id, run.matter_id,
       control_head.current_control_assignment_id,
       'LEGACY_PRE_INTENT_RECOVERY_RUN'
  FROM public.case_agent_runs run
  JOIN public.case_agent_goals goal
    ON goal.goal_id = run.goal_id
   AND goal.firm_id = run.firm_id
   AND goal.matter_id = run.matter_id
  JOIN public.case_agent_ledger_exception_control_heads control_head
    ON control_head.firm_id = run.firm_id
   AND control_head.matter_id = run.matter_id
   AND control_head.current_state = 'RECOVERY_REQUIRED'
  JOIN public.case_agent_ledger_exception_control_assignments control_assignment
    ON control_assignment.control_assignment_id =
            control_head.current_control_assignment_id
   AND control_assignment.firm_id = control_head.firm_id
   AND control_assignment.matter_id = control_head.matter_id
   AND control_assignment.state_after = control_head.current_state
   AND control_assignment.assignment_sequence = control_head.head_sequence
 WHERE run.created_at > control_assignment.assigned_at
   AND run.status = 'CREATED'
   AND run.current_event_version = 1
   AND run.current_graph_id IS NULL
   AND run.current_graph_version IS NULL
   AND run.current_graph_hash IS NULL
   AND run.is_stale IS DISTINCT FROM true
   AND run.is_cancelled IS DISTINCT FROM true
   AND goal.objective =
        '恢复本案异常材料后续工作并基于当前权威台账继续研判'
   AND goal.success_criteria = pg_catalog.jsonb_build_array(
        '接管全部待完成异常分流工作',
        '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
        '全部后续工作完成后基于当前案件版本重新规划'
   )
   AND goal.constraints = pg_catalog.jsonb_build_array(
        '不得自动确认正式事实、法律口径或对外提交',
        '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
   )
   AND EXISTS (
        SELECT 1
          FROM public.case_agent_events created_event
         WHERE created_event.run_id = run.run_id
           AND created_event.firm_id = run.firm_id
           AND created_event.matter_id = run.matter_id
           AND created_event.event_sequence = 1
           AND created_event.event_type = 'RUN_CREATED'
           AND created_event.actor_id = run.created_by
   )
   AND NOT EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_control_assignments assigned
         WHERE assigned.firm_id = run.firm_id
           AND assigned.matter_id = run.matter_id
           AND assigned.control_run_id = run.run_id
   );

-- Pre-0050 claimers do not know the new advisory key.  Lock every matched
-- inbox row in UUID order before inspecting leases; this either observes a
-- claim that already committed or makes an older claimer recheck/skip after
-- the quarantine has been parked.
SELECT inbox.run_id
  FROM public.case_agent_run_inbox inbox
  JOIN public.case_agent_ledger_exception_recovery_quarantines quarantine
    ON quarantine.run_id = inbox.run_id
   AND quarantine.firm_id = inbox.firm_id
   AND quarantine.matter_id = inbox.matter_id
 ORDER BY inbox.run_id
 FOR UPDATE OF inbox;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_recovery_quarantines quarantine
          JOIN public.case_agent_run_inbox inbox
            ON inbox.run_id = quarantine.run_id
           AND inbox.firm_id = quarantine.firm_id
           AND inbox.matter_id = quarantine.matter_id
         WHERE inbox.inbox_status = 'LEASED'
           AND inbox.lease_expires_at > pg_catalog.clock_timestamp()
    ) THEN
        RAISE EXCEPTION
            '0050 upgrade requires legacy recovery-run leases to be drained';
    END IF;
END;
$$;

UPDATE public.case_agent_run_inbox inbox
   SET inbox_status = 'QUIET',
       available_at = pg_catalog.clock_timestamp(),
       lease_owner = NULL,
       lease_token = NULL,
       lease_expires_at = NULL,
       inbox_version = inbox.inbox_version + 1,
       updated_at = pg_catalog.clock_timestamp()
  FROM public.case_agent_ledger_exception_recovery_quarantines quarantine
 WHERE quarantine.run_id = inbox.run_id
   AND quarantine.firm_id = inbox.firm_id
   AND quarantine.matter_id = inbox.matter_id
   AND NOT (
        inbox.inbox_status = 'LEASED'
        AND inbox.lease_expires_at > pg_catalog.clock_timestamp()
   );

CREATE FUNCTION public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
    input_session_id uuid,
    input_matter_id uuid,
    input_replacement_run_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    session_row record;
    matter_version integer;
    current_row record;
    prior_row record;
    pending_row record;
    prior_exists boolean := false;
    pending_exists boolean := false;
    authority_valid boolean := false;
    pending_run_exists boolean := false;
    pending_run_resumable boolean := false;
    abandon_reason text;
    new_intent_id uuid;
    terminal_at timestamptz;
    response_json jsonb;
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v2
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$'
       OR input_request_hash IS DISTINCT FROM
            public.case_agent_ledger_exception_control_transfer_request_hash(
                input_matter_id, input_expected_version,
                input_replacement_run_id
            ) THEN
        RAISE EXCEPTION 'ledger exception recovery intent input is invalid';
    END IF;

    PERFORM pg_catalog.set_config('app.firm_id', '', true);
    PERFORM pg_catalog.set_config(
        'app.web_session_id', input_session_id::text, true
    );
    SELECT session.session_id, session.firm_id, session.user_id,
           session.issuer, session.authenticated_at, session.expires_at
      INTO session_row
      FROM public.web_sessions session
     WHERE session.session_id = input_session_id
       AND session.revoked_at IS NULL
       AND session.expires_at > pg_catalog.clock_timestamp()
       AND session.authenticated_at <= pg_catalog.clock_timestamp()
       AND session.issuer LIKE 'https://%'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception recovery intent requires a live OIDC/MFA Web session';
    END IF;
    PERFORM pg_catalog.set_config(
        'app.firm_id', session_row.firm_id::text, true
    );
    PERFORM 1
      FROM public.users actor
     WHERE actor.user_id = session_row.user_id
       AND actor.firm_id = session_row.firm_id
       AND actor.status = 'ACTIVE'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception recovery intent actor is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.matter_id = input_matter_id
       AND role_binding.firm_id = session_row.firm_id
       AND role_binding.user_id = session_row.user_id
       AND role_binding.role = 'LEAD_LAWYER'
       AND role_binding.revoked_at IS NULL
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception recovery intent requires the current lead lawyer';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        session_row.user_id::text || '|' || input_matter_id::text ||
        '|PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY|' ||
        input_idempotency_key,
        0
    ));

    SELECT intent.recovery_intent_id, intent.request_hash,
           intent.replacement_run_id, intent.expected_matter_version,
           intent.idempotency_key, head.current_outcome
      INTO prior_row
      FROM public.case_agent_ledger_exception_recovery_intents intent
      JOIN public.case_agent_ledger_exception_recovery_intent_heads head
        ON head.recovery_intent_id = intent.recovery_intent_id
       AND head.firm_id = intent.firm_id
       AND head.matter_id = intent.matter_id
     WHERE intent.firm_id = session_row.firm_id
       AND intent.matter_id = input_matter_id
       AND intent.actor_id = session_row.user_id
       AND intent.idempotency_key = input_idempotency_key;
    prior_exists := FOUND;
    IF prior_exists THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.replacement_run_id IS DISTINCT FROM
                input_replacement_run_id
           OR prior_row.expected_matter_version IS DISTINCT FROM
                input_expected_version THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger exception recovery intent replay differs',
                ERRCODE = 'P4092';
        END IF;
        IF prior_row.current_outcome IN ('TRANSFERRED', 'ABANDONED') THEN
            SELECT EXISTS (
                SELECT 1
                  FROM public.case_agent_runs run
                 WHERE run.run_id = prior_row.replacement_run_id
                   AND run.firm_id = session_row.firm_id
                   AND run.matter_id = input_matter_id
            ) INTO pending_run_exists;
            RETURN pg_catalog.jsonb_build_object(
                'command_name',
                    'PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY',
                'idempotency_key', input_idempotency_key,
                'matter_id', input_matter_id::text,
                'matter_version', input_expected_version,
                'object_type', 'CASE_LEDGER_EXCEPTION_RECOVERY_INTENT',
                'object_id', prior_row.recovery_intent_id::text,
                'recovery_state', prior_row.current_outcome,
                'transfer_idempotency_key', prior_row.idempotency_key,
                'replacement_run_id', prior_row.replacement_run_id::text,
                'run_exists', pending_run_exists
            );
        END IF;
    END IF;

    -- Serialize every prepare/transfer/claim decision for this matter.  A
    -- reload may lose the browser's old idempotency key.  Under this lock a
    -- new authorized request can resume a committed run, or replace a
    -- run-less/stale PENDING intent, without racing transfer or Worker claim.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        session_row.firm_id::text || '|' || input_matter_id::text ||
        '|CASE_LEDGER_EXCEPTION_CONTROL_CLAIM',
        0
    ));

    SELECT matter.version
      INTO matter_version
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception recovery intent matter is missing';
    END IF;

    SELECT head.current_control_assignment_id,
           head.current_state, head.head_sequence,
           assignment.control_run_id, assignment.assigned_at
      INTO current_row
      FROM public.case_agent_ledger_exception_control_heads head
      JOIN public.case_agent_ledger_exception_control_assignments assignment
        ON assignment.control_assignment_id =
                head.current_control_assignment_id
       AND assignment.firm_id = head.firm_id
       AND assignment.matter_id = head.matter_id
       AND assignment.state_after = head.current_state
       AND assignment.assignment_sequence = head.head_sequence
     WHERE head.firm_id = session_row.firm_id
       AND head.matter_id = input_matter_id
     FOR UPDATE OF head;
    authority_valid := FOUND;
    IF authority_valid THEN
        authority_valid := current_row.current_state = 'RECOVERY_REQUIRED';
    END IF;
    IF authority_valid THEN
        SELECT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads followup_head
             WHERE followup_head.firm_id = session_row.firm_id
               AND followup_head.matter_id = input_matter_id
               AND followup_head.current_state = 'ACTIVE'
        ) INTO authority_valid;
    END IF;

    -- A transfer could have completed while this call waited for the shared
    -- matter lock.  Re-read a same-key PENDING row under the lock before
    -- deciding whether it is resumable or terminal.
    IF prior_exists THEN
        SELECT intent.recovery_intent_id, intent.request_hash,
               intent.replacement_run_id, intent.expected_matter_version,
               intent.idempotency_key, head.current_outcome
          INTO prior_row
          FROM public.case_agent_ledger_exception_recovery_intents intent
          JOIN public.case_agent_ledger_exception_recovery_intent_heads head
            ON head.recovery_intent_id = intent.recovery_intent_id
           AND head.firm_id = intent.firm_id
           AND head.matter_id = intent.matter_id
         WHERE intent.recovery_intent_id = prior_row.recovery_intent_id
           AND intent.firm_id = session_row.firm_id
           AND intent.matter_id = input_matter_id
         FOR UPDATE OF head;
        IF prior_row.current_outcome IN ('TRANSFERRED', 'ABANDONED') THEN
            SELECT EXISTS (
                SELECT 1
                  FROM public.case_agent_runs run
                 WHERE run.run_id = prior_row.replacement_run_id
                   AND run.firm_id = session_row.firm_id
                   AND run.matter_id = input_matter_id
            ) INTO pending_run_exists;
            RETURN pg_catalog.jsonb_build_object(
                'command_name',
                    'PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY',
                'idempotency_key', input_idempotency_key,
                'matter_id', input_matter_id::text,
                'matter_version', input_expected_version,
                'object_type', 'CASE_LEDGER_EXCEPTION_RECOVERY_INTENT',
                'object_id', prior_row.recovery_intent_id::text,
                'recovery_state', prior_row.current_outcome,
                'transfer_idempotency_key', prior_row.idempotency_key,
                'replacement_run_id', prior_row.replacement_run_id::text,
                'run_exists', pending_run_exists
            );
        END IF;
    END IF;

    SELECT intent.recovery_intent_id, intent.replacement_run_id,
           intent.source_control_assignment_id, intent.actor_id,
           intent.expected_matter_version, intent.idempotency_key,
           intent.request_hash
      INTO pending_row
      FROM public.case_agent_ledger_exception_recovery_intents intent
      JOIN public.case_agent_ledger_exception_recovery_intent_heads intent_head
        ON intent_head.recovery_intent_id = intent.recovery_intent_id
       AND intent_head.firm_id = intent.firm_id
       AND intent_head.matter_id = intent.matter_id
     WHERE intent.firm_id = session_row.firm_id
       AND intent.matter_id = input_matter_id
       AND intent_head.current_outcome = 'PENDING'
     FOR UPDATE OF intent_head;
    pending_exists := FOUND;
    IF prior_exists AND (
        NOT pending_exists
        OR pending_row.recovery_intent_id IS DISTINCT FROM
            prior_row.recovery_intent_id
    ) THEN
        RAISE EXCEPTION
            'same-key pending recovery intent lost its current head';
    END IF;

    IF pending_exists THEN
        IF EXISTS (
            SELECT 1
              FROM public.case_agent_run_inbox inbox
             WHERE inbox.run_id = pending_row.replacement_run_id
               AND inbox.firm_id = session_row.firm_id
               AND inbox.matter_id = input_matter_id
               AND inbox.inbox_status = 'LEASED'
               AND inbox.lease_expires_at > pg_catalog.clock_timestamp()
        ) THEN
            RAISE EXCEPTION
                'pending recovery run acquired an impossible active lease';
        END IF;

        SELECT EXISTS (
            SELECT 1
              FROM public.case_agent_runs run
             WHERE run.run_id = pending_row.replacement_run_id
               AND run.firm_id = session_row.firm_id
               AND run.matter_id = input_matter_id
        ) INTO pending_run_exists;
        IF authority_valid AND pending_run_exists THEN
            SELECT EXISTS (
                SELECT 1
                  FROM public.case_agent_runs run
                  JOIN public.case_agent_run_inbox inbox
                    ON inbox.run_id = run.run_id
                   AND inbox.firm_id = run.firm_id
                   AND inbox.matter_id = run.matter_id
                 WHERE run.run_id = pending_row.replacement_run_id
                   AND run.firm_id = session_row.firm_id
                   AND run.matter_id = input_matter_id
                   AND run.created_by = pending_row.actor_id
                   AND run.created_at > current_row.assigned_at
                   AND run.snapshot_matter_version =
                        pending_row.expected_matter_version
                   AND run.status = 'CREATED'
                   AND run.current_event_version = 1
                   AND run.current_graph_id IS NULL
                   AND run.current_graph_version IS NULL
                   AND run.current_graph_hash IS NULL
                   AND run.is_stale IS DISTINCT FROM true
                   AND run.is_cancelled IS DISTINCT FROM true
                   AND inbox.inbox_status = 'QUIET'
                   AND EXISTS (
                        SELECT 1
                          FROM public.case_agent_events created_event
                         WHERE created_event.run_id = run.run_id
                           AND created_event.firm_id = run.firm_id
                           AND created_event.matter_id = run.matter_id
                           AND created_event.event_sequence = 1
                           AND created_event.event_type = 'RUN_CREATED'
                           AND created_event.actor_id = pending_row.actor_id
                   )
            ) INTO pending_run_resumable;
        END IF;

        abandon_reason := NULL;
        IF NOT authority_valid THEN
            abandon_reason := 'CONTROL_AUTHORITY_ENDED';
        ELSIF pending_row.source_control_assignment_id IS DISTINCT FROM
                current_row.current_control_assignment_id THEN
            abandon_reason := 'CONTROL_AUTHORITY_CHANGED';
        ELSIF pending_row.expected_matter_version IS DISTINCT FROM
                matter_version THEN
            abandon_reason := 'MATTER_VERSION_CHANGED';
        ELSIF pending_row.actor_id IS DISTINCT FROM session_row.user_id THEN
            abandon_reason := 'RECOVERY_ACTOR_CHANGED';
        ELSIF pending_run_exists AND NOT pending_run_resumable THEN
            abandon_reason := 'REPLACEMENT_RUN_INVALID';
        ELSIF NOT prior_exists AND NOT pending_run_exists THEN
            abandon_reason := 'SUPERSEDED_BY_NEW_RECOVERY_INTENT';
        END IF;

        IF abandon_reason IS NULL THEN
            IF matter_version <> input_expected_version THEN
                RAISE EXCEPTION USING
                    MESSAGE =
                        'ledger exception recovery intent matter version is stale',
                    ERRCODE = 'P4091';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'command_name',
                    'PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY',
                'idempotency_key', input_idempotency_key,
                'matter_id', input_matter_id::text,
                'matter_version', input_expected_version,
                'object_type', 'CASE_LEDGER_EXCEPTION_RECOVERY_INTENT',
                'object_id', pending_row.recovery_intent_id::text,
                'recovery_state', 'PENDING',
                'transfer_idempotency_key', pending_row.idempotency_key,
                'replacement_run_id', pending_row.replacement_run_id::text,
                'run_exists', pending_run_exists
            );
        END IF;

        terminal_at := pg_catalog.clock_timestamp();
        UPDATE public.case_agent_ledger_exception_recovery_intent_heads intent_head
           SET current_outcome = 'ABANDONED',
               transfer_control_assignment_id = NULL,
               outcome_reason_code = abandon_reason,
               outcome_actor_id = session_row.user_id,
               outcome_web_session_id = input_session_id,
               outcome_at = terminal_at,
               outcome_version = 2,
               updated_at = terminal_at
         WHERE intent_head.recovery_intent_id = pending_row.recovery_intent_id
           AND intent_head.firm_id = session_row.firm_id
           AND intent_head.matter_id = input_matter_id
           AND intent_head.current_outcome = 'PENDING'
           AND intent_head.outcome_version = 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                MESSAGE = 'pending recovery intent changed before replacement',
                ERRCODE = 'P4092';
        END IF;
        UPDATE public.case_agent_run_inbox inbox
           SET inbox_status = 'QUIET',
               available_at = terminal_at,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at = NULL,
               inbox_version = inbox.inbox_version + 1,
               updated_at = terminal_at
         WHERE inbox.run_id = pending_row.replacement_run_id
           AND inbox.firm_id = session_row.firm_id
           AND inbox.matter_id = input_matter_id;

        IF prior_exists OR NOT authority_valid
           OR matter_version <> input_expected_version THEN
            RETURN pg_catalog.jsonb_build_object(
                'command_name',
                    'PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY',
                'idempotency_key', input_idempotency_key,
                'matter_id', input_matter_id::text,
                'matter_version', input_expected_version,
                'object_type', 'CASE_LEDGER_EXCEPTION_RECOVERY_INTENT',
                'object_id', pending_row.recovery_intent_id::text,
                'recovery_state', 'ABANDONED',
                'transfer_idempotency_key', pending_row.idempotency_key,
                'replacement_run_id', pending_row.replacement_run_id::text,
                'run_exists', pending_run_exists
            );
        END IF;
    END IF;

    IF matter_version <> input_expected_version THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception recovery intent matter version is stale',
            ERRCODE = 'P4091';
    ELSIF NOT authority_valid THEN
        RAISE EXCEPTION USING
            MESSAGE =
                'ledger exception recovery intent has no failed active authority',
            ERRCODE = 'P4092';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM public.case_agent_runs run
         WHERE run.run_id = input_replacement_run_id
    ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception recovery intent conflicts with existing work',
            ERRCODE = 'P4092';
    END IF;

    new_intent_id := public.gen_random_uuid();
    INSERT INTO public.case_agent_ledger_exception_recovery_intents (
        recovery_intent_id, firm_id, matter_id,
        source_control_assignment_id, replacement_run_id,
        actor_id, prepared_web_session_id, expected_matter_version,
        idempotency_key, request_hash
    ) VALUES (
        new_intent_id, session_row.firm_id, input_matter_id,
        current_row.current_control_assignment_id, input_replacement_run_id,
        session_row.user_id, input_session_id, input_expected_version,
        input_idempotency_key, input_request_hash
    );
    INSERT INTO public.case_agent_ledger_exception_recovery_intent_heads (
        recovery_intent_id, firm_id, matter_id, current_outcome,
        transfer_control_assignment_id, outcome_reason_code,
        outcome_actor_id, outcome_web_session_id, outcome_at,
        outcome_version
    ) VALUES (
        new_intent_id, session_row.firm_id, input_matter_id,
        'PENDING', NULL, NULL, NULL, NULL, NULL, 1
    );
    response_json := pg_catalog.jsonb_build_object(
        'command_name', 'PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY',
        'idempotency_key', input_idempotency_key,
        'matter_id', input_matter_id::text,
        'matter_version', input_expected_version,
        'object_type', 'CASE_LEDGER_EXCEPTION_RECOVERY_INTENT',
        'object_id', new_intent_id::text,
        'recovery_state', 'PENDING',
        'transfer_idempotency_key', input_idempotency_key,
        'replacement_run_id', input_replacement_run_id::text,
        'run_exists', false
    );
    RETURN response_json;
END;
$$;

-- Replace 0049's transfer with the intent-bound form.  The order is:
-- shared matter-control advisory lock; old/replacement run rows sorted by
-- UUID; matter; control head; old/replacement inbox rows sorted by UUID.
-- Generic Agent appends use run -> matter -> inbox, so this order cannot form
-- the old matter -> run inversion.  A claim uses the same advisory -> inbox
-- prefix and must finish before this function may inspect the lease.
CREATE OR REPLACE FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session(
    input_session_id uuid,
    input_matter_id uuid,
    input_replacement_run_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    session_row record;
    matter_version integer;
    prior_row record;
    intent_row record;
    current_row record;
    replacement_row record;
    old_inbox_row record;
    replacement_inbox_row record;
    old_control_run_id uuid;
    locked_run_count integer;
    locked_inbox_count integer;
    next_assignment_id uuid;
    audit_id uuid;
    next_sequence integer;
    response_json jsonb;
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v2
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$'
       OR input_request_hash IS DISTINCT FROM
            public.case_agent_ledger_exception_control_transfer_request_hash(
                input_matter_id, input_expected_version,
                input_replacement_run_id
            ) THEN
        RAISE EXCEPTION 'ledger exception control transfer input is invalid';
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', '', true);
    PERFORM pg_catalog.set_config(
        'app.web_session_id', input_session_id::text, true
    );
    SELECT session.session_id, session.firm_id, session.user_id,
           session.issuer, session.authenticated_at, session.expires_at
      INTO session_row
      FROM public.web_sessions session
     WHERE session.session_id = input_session_id
       AND session.revoked_at IS NULL
       AND session.expires_at > pg_catalog.clock_timestamp()
       AND session.authenticated_at <= pg_catalog.clock_timestamp()
       AND session.issuer LIKE 'https://%'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception control transfer requires a live OIDC/MFA Web session';
    END IF;
    PERFORM pg_catalog.set_config(
        'app.firm_id', session_row.firm_id::text, true
    );
    PERFORM 1
      FROM public.users actor
     WHERE actor.user_id = session_row.user_id
       AND actor.firm_id = session_row.firm_id
       AND actor.status = 'ACTIVE'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception control transfer actor is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.matter_id = input_matter_id
       AND role_binding.firm_id = session_row.firm_id
       AND role_binding.user_id = session_row.user_id
       AND role_binding.role = 'LEAD_LAWYER'
       AND role_binding.revoked_at IS NULL
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception control transfer requires the current lead lawyer';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        session_row.user_id::text || '|' || input_matter_id::text ||
        '|TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL|' || input_idempotency_key,
        0
    ));

    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = session_row.firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = session_row.user_id
       AND idempotency.command_name =
            'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL'
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                input_idempotency_key
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                input_expected_version::text
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXCEPTION_CONTROL_ASSIGNMENT'
           OR prior_row.response_json->>'control_health' IS DISTINCT FROM
                'HEALTHY'
           OR NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_recovery_intents intent
                  JOIN public.case_agent_ledger_exception_recovery_intent_heads intent_head
                    ON intent_head.recovery_intent_id = intent.recovery_intent_id
                   AND intent_head.firm_id = intent.firm_id
                   AND intent_head.matter_id = intent.matter_id
                  JOIN public.case_agent_ledger_exception_control_assignments assignment
                    ON assignment.control_assignment_id =
                            intent_head.transfer_control_assignment_id
                   AND assignment.firm_id = intent.firm_id
                   AND assignment.matter_id = intent.matter_id
                  JOIN public.audit_events audit
                    ON audit.event_id = assignment.audit_event_id
                   AND audit.firm_id = assignment.firm_id
                   AND audit.matter_id = assignment.matter_id
                 WHERE intent.firm_id = session_row.firm_id
                   AND intent.matter_id = input_matter_id
                   AND intent.actor_id = session_row.user_id
                   AND intent.idempotency_key = input_idempotency_key
                   AND intent.request_hash = input_request_hash
                   AND intent.replacement_run_id = input_replacement_run_id
                   AND intent.expected_matter_version = input_expected_version
                   AND intent_head.current_outcome = 'TRANSFERRED'
                   AND assignment.control_assignment_id =
                        (prior_row.response_json->>'object_id')::uuid
                   AND assignment.control_run_id = input_replacement_run_id
                   AND assignment.transition_type = 'TRANSFERRED'
                   AND assignment.state_after = 'HEALTHY'
                   AND assignment.actor_id = session_row.user_id
                   AND assignment.expected_matter_version =
                        input_expected_version
                   AND assignment.request_hash = input_request_hash
                   AND audit.event_id =
                        (prior_row.response_json->>'audit_event_id')::uuid
                   AND audit.event_type =
                        'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED'
                   AND audit.input_version = input_expected_version
                   AND audit.output_version = input_expected_version
           ) THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger exception control transfer replay differs',
                ERRCODE = 'P4092';
        END IF;
        RETURN prior_row.response_json;
    END IF;

    SELECT intent.recovery_intent_id,
           intent.source_control_assignment_id,
           intent.expected_matter_version,
           intent.request_hash,
           intent_head.current_outcome,
           intent_head.outcome_version
      INTO intent_row
      FROM public.case_agent_ledger_exception_recovery_intents intent
      JOIN public.case_agent_ledger_exception_recovery_intent_heads intent_head
        ON intent_head.recovery_intent_id = intent.recovery_intent_id
       AND intent_head.firm_id = intent.firm_id
       AND intent_head.matter_id = intent.matter_id
     WHERE intent.firm_id = session_row.firm_id
       AND intent.matter_id = input_matter_id
       AND intent.actor_id = session_row.user_id
       AND intent.idempotency_key = input_idempotency_key
       AND intent.replacement_run_id = input_replacement_run_id;
    IF NOT FOUND
       OR intent_row.request_hash IS DISTINCT FROM input_request_hash
       OR intent_row.expected_matter_version IS DISTINCT FROM
            input_expected_version
       OR intent_row.current_outcome <> 'PENDING'
       OR intent_row.outcome_version <> 1 THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control transfer lacks its pending recovery intent',
            ERRCODE = 'P4092';
    END IF;

    SELECT assignment.control_run_id
      INTO old_control_run_id
      FROM public.case_agent_ledger_exception_control_assignments assignment
     WHERE assignment.control_assignment_id =
            intent_row.source_control_assignment_id
       AND assignment.firm_id = session_row.firm_id
       AND assignment.matter_id = input_matter_id
       AND assignment.state_after = 'RECOVERY_REQUIRED';
    IF NOT FOUND OR old_control_run_id = input_replacement_run_id THEN
        RAISE EXCEPTION
            'ledger exception recovery intent source authority differs';
    END IF;

    -- This lock is also taken by the run-inbox claimer before it locks an
    -- inbox row.  It turns claim-vs-transfer into a single total order.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        session_row.firm_id::text || '|' || input_matter_id::text ||
        '|CASE_LEDGER_EXCEPTION_CONTROL_CLAIM',
        0
    ));

    PERFORM run.run_id
      FROM public.case_agent_runs run
     WHERE run.firm_id = session_row.firm_id
       AND run.matter_id = input_matter_id
       AND run.run_id IN (old_control_run_id, input_replacement_run_id)
     ORDER BY run.run_id
     FOR UPDATE;
    GET DIAGNOSTICS locked_run_count = ROW_COUNT;
    IF locked_run_count <> 2 THEN
        RAISE EXCEPTION
            'ledger exception control transfer run lineage is incomplete';
    END IF;

    SELECT matter.version
      INTO matter_version
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception control transfer matter is missing';
    ELSIF matter_version <> input_expected_version THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control transfer matter version is stale',
            ERRCODE = 'P4091';
    END IF;

    SELECT head.current_control_assignment_id,
           head.current_state, head.head_sequence,
           assignment.control_run_id, assignment.assigned_at
      INTO current_row
      FROM public.case_agent_ledger_exception_control_heads head
      JOIN public.case_agent_ledger_exception_control_assignments assignment
        ON assignment.control_assignment_id =
                head.current_control_assignment_id
       AND assignment.firm_id = head.firm_id
       AND assignment.matter_id = head.matter_id
       AND assignment.state_after = head.current_state
       AND assignment.assignment_sequence = head.head_sequence
     WHERE head.firm_id = session_row.firm_id
       AND head.matter_id = input_matter_id
     FOR UPDATE OF head;
    IF NOT FOUND
       OR current_row.current_control_assignment_id IS DISTINCT FROM
            intent_row.source_control_assignment_id
       OR current_row.current_state <> 'RECOVERY_REQUIRED'
       OR current_row.control_run_id IS DISTINCT FROM old_control_run_id
       OR NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads followup_head
             WHERE followup_head.firm_id = session_row.firm_id
               AND followup_head.matter_id = input_matter_id
               AND followup_head.current_state = 'ACTIVE'
       ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control transfer authority changed',
            ERRCODE = 'P4092';
    END IF;

    SELECT run.run_id, run.status, run.current_event_version,
           run.snapshot_matter_version, run.created_at
      INTO replacement_row
      FROM public.case_agent_runs run
     WHERE run.run_id = input_replacement_run_id
       AND run.firm_id = session_row.firm_id
       AND run.matter_id = input_matter_id
       AND run.created_by = session_row.user_id
       AND run.created_at > current_row.assigned_at
       AND run.snapshot_matter_version = input_expected_version
       AND run.status = 'CREATED'
       AND run.current_event_version = 1
       AND run.current_graph_id IS NULL
       AND run.current_graph_version IS NULL
       AND run.current_graph_hash IS NULL
       AND run.is_stale IS DISTINCT FROM true
       AND run.is_cancelled IS DISTINCT FROM true
       AND EXISTS (
            SELECT 1
              FROM public.case_agent_events created_event
             WHERE created_event.run_id = run.run_id
               AND created_event.firm_id = run.firm_id
               AND created_event.matter_id = run.matter_id
               AND created_event.event_sequence = 1
               AND created_event.event_type = 'RUN_CREATED'
               AND created_event.actor_id = session_row.user_id
       );
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'replacement control must be the pristine run bound to its recovery intent';
    END IF;

    PERFORM inbox.run_id
      FROM public.case_agent_run_inbox inbox
     WHERE inbox.firm_id = session_row.firm_id
       AND inbox.matter_id = input_matter_id
       AND inbox.run_id IN (old_control_run_id, input_replacement_run_id)
     ORDER BY inbox.run_id
     FOR UPDATE;
    GET DIAGNOSTICS locked_inbox_count = ROW_COUNT;
    IF locked_inbox_count <> 2 THEN
        RAISE EXCEPTION
            'ledger exception control transfer inbox lineage is incomplete';
    END IF;
    SELECT inbox.inbox_status, inbox.lease_expires_at
      INTO old_inbox_row
      FROM public.case_agent_run_inbox inbox
     WHERE inbox.run_id = old_control_run_id
       AND inbox.firm_id = session_row.firm_id
       AND inbox.matter_id = input_matter_id;
    SELECT inbox.inbox_status, inbox.lease_expires_at
      INTO replacement_inbox_row
      FROM public.case_agent_run_inbox inbox
     WHERE inbox.run_id = input_replacement_run_id
       AND inbox.firm_id = session_row.firm_id
       AND inbox.matter_id = input_matter_id;
    IF old_inbox_row.inbox_status = 'LEASED'
       AND old_inbox_row.lease_expires_at > pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control run is still executing',
            ERRCODE = 'P4092';
    END IF;
    IF replacement_inbox_row.inbox_status = 'LEASED' THEN
        RAISE EXCEPTION
            'untransferred recovery run acquired an impossible lease';
    END IF;

    next_assignment_id := public.gen_random_uuid();
    audit_id := public.gen_random_uuid();
    next_sequence := current_row.head_sequence + 1;
    INSERT INTO public.audit_events (
        event_id, firm_id, matter_id, actor_id, event_type,
        input_version, output_version, request_id, payload
    ) VALUES (
        audit_id, session_row.firm_id, input_matter_id,
        session_row.user_id, 'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED',
        input_expected_version, input_expected_version,
        public.gen_random_uuid(), pg_catalog.jsonb_build_object(
            'recovery_intent_id', intent_row.recovery_intent_id,
            'control_assignment_id', next_assignment_id,
            'replacement_run_id', input_replacement_run_id,
            'supersedes_control_assignment_id',
                current_row.current_control_assignment_id,
            'reason_code', 'RECOVER_FAILED_CONTROL_RUN',
            'request_hash', input_request_hash,
            'formal_ledger_write', false,
            'legal_conclusion', false
        )
    );
    INSERT INTO public.case_agent_ledger_exception_control_assignments (
        control_assignment_id, firm_id, matter_id, assignment_sequence,
        control_run_id, state_after, transition_type,
        supersedes_control_assignment_id, actor_id, web_session_id,
        expected_matter_version, reason_code, idempotency_key,
        request_hash, audit_event_id
    ) VALUES (
        next_assignment_id, session_row.firm_id, input_matter_id,
        next_sequence, input_replacement_run_id, 'HEALTHY', 'TRANSFERRED',
        current_row.current_control_assignment_id, session_row.user_id,
        input_session_id, input_expected_version,
        'RECOVER_FAILED_CONTROL_RUN', input_idempotency_key,
        input_request_hash, audit_id
    );
    UPDATE public.case_agent_ledger_exception_control_heads head
       SET current_control_assignment_id = next_assignment_id,
           current_state = 'HEALTHY',
           head_sequence = next_sequence,
           updated_at = pg_catalog.clock_timestamp()
     WHERE head.firm_id = session_row.firm_id
       AND head.matter_id = input_matter_id
       AND head.current_control_assignment_id =
            current_row.current_control_assignment_id
       AND head.head_sequence = current_row.head_sequence;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control head changed before transfer',
            ERRCODE = 'P4092';
    END IF;
    UPDATE public.case_agent_ledger_exception_recovery_intent_heads intent_head
       SET current_outcome = 'TRANSFERRED',
           transfer_control_assignment_id = next_assignment_id,
           outcome_reason_code = 'RECOVER_FAILED_CONTROL_RUN',
           outcome_actor_id = session_row.user_id,
           outcome_web_session_id = input_session_id,
           outcome_at = pg_catalog.clock_timestamp(),
           outcome_version = 2,
           updated_at = pg_catalog.clock_timestamp()
     WHERE intent_head.recovery_intent_id = intent_row.recovery_intent_id
       AND intent_head.firm_id = session_row.firm_id
       AND intent_head.matter_id = input_matter_id
       AND intent_head.current_outcome = 'PENDING'
       AND intent_head.outcome_version = 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception recovery intent changed before transfer',
            ERRCODE = 'P4092';
    END IF;

    UPDATE public.case_agent_run_inbox inbox
       SET inbox_status = 'QUIET',
           available_at = pg_catalog.clock_timestamp(),
           lease_owner = NULL, lease_token = NULL,
           lease_expires_at = NULL,
           inbox_version = inbox.inbox_version + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE inbox.run_id = old_control_run_id
       AND inbox.firm_id = session_row.firm_id
       AND inbox.matter_id = input_matter_id;
    UPDATE public.case_agent_run_inbox inbox
       SET inbox_status = 'READY',
           available_at = pg_catalog.clock_timestamp(),
           lease_owner = NULL, lease_token = NULL,
           lease_expires_at = NULL,
           inbox_version = inbox.inbox_version + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE inbox.run_id = input_replacement_run_id
       AND inbox.firm_id = session_row.firm_id
       AND inbox.matter_id = input_matter_id;

    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        session_row.firm_id, input_matter_id, input_expected_version,
        'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED',
        pg_catalog.jsonb_build_object(
            'audit_event_id', audit_id,
            'recovery_intent_id', intent_row.recovery_intent_id,
            'control_assignment_id', next_assignment_id,
            'replacement_run_id', input_replacement_run_id,
            'control_health', 'HEALTHY',
            'wakeup_source', 'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED'
        )
    );
    response_json := pg_catalog.jsonb_build_object(
        'command_name', 'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL',
        'idempotency_key', input_idempotency_key,
        'matter_id', input_matter_id::text,
        'matter_version', input_expected_version,
        'audit_event_id', audit_id::text,
        'object_type', 'CASE_LEDGER_EXCEPTION_CONTROL_ASSIGNMENT',
        'object_id', next_assignment_id::text,
        'control_health', 'HEALTHY'
    );
    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        session_row.firm_id, input_matter_id, session_row.user_id,
        'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL', input_idempotency_key,
        input_request_hash, response_json
    );
    PERFORM pg_catalog.pg_notify(
        'case_agent_run_ready', session_row.firm_id::text
    );
    RETURN response_json;
END;
$$;

-- Keep the wake projection aligned with the authoritative control cursor.
-- Any pending recovery run, failed current control run, or superseded control
-- run remains QUIET even if a late Agent event updates its event version.
CREATE OR REPLACE FUNCTION public.wake_case_agent_run()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    run_is_claimable boolean;
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v2
    PERFORM pg_catalog.set_config('app.firm_id', NEW.firm_id::text, true);
    IF TG_OP = 'INSERT'
       AND EXISTS (
            SELECT 1
              FROM public.case_agent_goals goal
             WHERE goal.goal_id = NEW.goal_id
               AND goal.firm_id = NEW.firm_id
               AND goal.matter_id = NEW.matter_id
               AND goal.objective =
                    '恢复本案异常材料后续工作并基于当前权威台账继续研判'
               AND goal.success_criteria = pg_catalog.jsonb_build_array(
                    '接管全部待完成异常分流工作',
                    '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                    '全部后续工作完成后基于当前案件版本重新规划'
               )
               AND goal.constraints = pg_catalog.jsonb_build_array(
                    '不得自动确认正式事实、法律口径或对外提交',
                    '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
               )
       )
       AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_recovery_intents intent
             WHERE intent.replacement_run_id = NEW.run_id
               AND intent.firm_id = NEW.firm_id
               AND intent.matter_id = NEW.matter_id
       ) THEN
        RAISE EXCEPTION
            'case Agent ledger exception recovery run requires a durable intent';
    END IF;
    SELECT
        -- Permanent structural fence for an old Web transaction that began
        -- before the migration scan but committed its exact recovery-shaped
        -- run after cutover.  Safety never depends on deployment timing.
        NOT EXISTS (
            SELECT 1
              FROM public.case_agent_goals legacy_goal
             WHERE legacy_goal.goal_id = NEW.goal_id
               AND legacy_goal.firm_id = NEW.firm_id
               AND legacy_goal.matter_id = NEW.matter_id
               AND legacy_goal.objective =
                    '恢复本案异常材料后续工作并基于当前权威台账继续研判'
               AND legacy_goal.success_criteria =
                    pg_catalog.jsonb_build_array(
                        '接管全部待完成异常分流工作',
                        '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                        '全部后续工作完成后基于当前案件版本重新规划'
                    )
               AND legacy_goal.constraints =
                    pg_catalog.jsonb_build_array(
                        '不得自动确认正式事实、法律口径或对外提交',
                        '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
                    )
               AND NOT EXISTS (
                    SELECT 1
                      FROM public.case_agent_ledger_exception_recovery_intents intent
                     WHERE intent.replacement_run_id = NEW.run_id
                       AND intent.firm_id = NEW.firm_id
                       AND intent.matter_id = NEW.matter_id
               )
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_recovery_quarantines quarantine
             WHERE quarantine.run_id = NEW.run_id
               AND quarantine.firm_id = NEW.firm_id
               AND quarantine.matter_id = NEW.matter_id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_recovery_intents intent
              JOIN public.case_agent_ledger_exception_recovery_intent_heads intent_head
                ON intent_head.recovery_intent_id = intent.recovery_intent_id
               AND intent_head.firm_id = intent.firm_id
               AND intent_head.matter_id = intent.matter_id
             WHERE intent.firm_id = NEW.firm_id
               AND intent.matter_id = NEW.matter_id
               AND intent.replacement_run_id = NEW.run_id
               AND (
                    intent_head.current_outcome <> 'TRANSFERRED'
                    OR NOT EXISTS (
                        SELECT 1
                          FROM public.case_agent_ledger_exception_control_heads control_head
                          JOIN public.case_agent_ledger_exception_control_assignments assignment
                            ON assignment.control_assignment_id =
                                    control_head.current_control_assignment_id
                           AND assignment.firm_id = control_head.firm_id
                           AND assignment.matter_id = control_head.matter_id
                           AND assignment.state_after = control_head.current_state
                           AND assignment.assignment_sequence =
                                    control_head.head_sequence
                         WHERE control_head.firm_id = intent.firm_id
                           AND control_head.matter_id = intent.matter_id
                           AND control_head.current_state = 'HEALTHY'
                           AND control_head.current_control_assignment_id =
                                intent_head.transfer_control_assignment_id
                           AND assignment.control_run_id = NEW.run_id
                    )
               )
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_control_assignments historical
             WHERE historical.firm_id = NEW.firm_id
               AND historical.matter_id = NEW.matter_id
               AND historical.control_run_id = NEW.run_id
               AND NOT EXISTS (
                    SELECT 1
                      FROM public.case_agent_ledger_exception_control_heads control_head
                      JOIN public.case_agent_ledger_exception_control_assignments current_assignment
                        ON current_assignment.control_assignment_id =
                                control_head.current_control_assignment_id
                       AND current_assignment.firm_id = control_head.firm_id
                       AND current_assignment.matter_id = control_head.matter_id
                       AND current_assignment.state_after = control_head.current_state
                       AND current_assignment.assignment_sequence =
                                control_head.head_sequence
                     WHERE control_head.firm_id = historical.firm_id
                       AND control_head.matter_id = historical.matter_id
                       AND control_head.current_state = 'HEALTHY'
                       AND current_assignment.control_run_id = NEW.run_id
               )
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads followup_head
              JOIN public.case_agent_ledger_exception_control_heads control_head
                ON control_head.firm_id = followup_head.firm_id
               AND control_head.matter_id = followup_head.matter_id
              JOIN public.case_agent_ledger_exception_control_assignments assignment
                ON assignment.control_assignment_id =
                        control_head.current_control_assignment_id
               AND assignment.firm_id = control_head.firm_id
               AND assignment.matter_id = control_head.matter_id
               AND assignment.state_after = control_head.current_state
               AND assignment.assignment_sequence = control_head.head_sequence
             WHERE followup_head.firm_id = NEW.firm_id
               AND followup_head.matter_id = NEW.matter_id
               AND followup_head.current_state = 'ACTIVE'
               AND (
                    control_head.current_state <> 'HEALTHY'
                    OR assignment.control_run_id <> NEW.run_id
               )
        )
      INTO run_is_claimable;

    INSERT INTO public.case_agent_run_inbox (
        run_id, firm_id, matter_id, inbox_status,
        observed_event_version, available_at,
        lease_owner, lease_token, lease_expires_at,
        inbox_version, updated_at
    ) VALUES (
        NEW.run_id, NEW.firm_id, NEW.matter_id,
        CASE WHEN run_is_claimable THEN 'READY' ELSE 'QUIET' END,
        NEW.current_event_version, pg_catalog.clock_timestamp(),
        NULL, NULL, NULL, 1, pg_catalog.clock_timestamp()
    )
    ON CONFLICT (run_id) DO UPDATE SET
        observed_event_version = EXCLUDED.observed_event_version,
        inbox_status = EXCLUDED.inbox_status,
        available_at = pg_catalog.clock_timestamp(),
        lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL,
        inbox_version = public.case_agent_run_inbox.inbox_version + 1,
        updated_at = pg_catalog.clock_timestamp();
    IF run_is_claimable THEN
        PERFORM pg_catalog.pg_notify('case_agent_run_ready', NEW.firm_id::text);
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE public.case_agent_ledger_exception_recovery_intents
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_recovery_intents
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_recovery_intent_heads
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_recovery_intent_heads
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_recovery_quarantines
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_recovery_quarantines
    FORCE ROW LEVEL SECURITY;

CREATE POLICY case_agent_ledger_exception_recovery_intents_firm_isolation
    ON public.case_agent_ledger_exception_recovery_intents
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_recovery_intent_heads_firm_isolation
    ON public.case_agent_ledger_exception_recovery_intent_heads
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_recovery_quarantines_firm_isolation
    ON public.case_agent_ledger_exception_recovery_quarantines
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));

ALTER TABLE public.case_agent_ledger_exception_recovery_intents
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_recovery_intent_heads
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_recovery_quarantines
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.prohibit_case_agent_ledger_exception_recovery_intent_mutation()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.guard_case_agent_ledger_exception_recovery_intent_head()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
    uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session(
    uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.wake_case_agent_run()
    OWNER TO lawcase_ledger_confirmation_owner;

REVOKE ALL ON TABLE
    public.case_agent_ledger_exception_recovery_intents,
    public.case_agent_ledger_exception_recovery_intent_heads,
    public.case_agent_ledger_exception_recovery_quarantines
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
    public.case_agent_ledger_exception_recovery_intents,
    public.case_agent_ledger_exception_recovery_intent_heads,
    public.case_agent_ledger_exception_recovery_quarantines
    FROM lawcase_web_application, lawcase_agent_worker;
-- Recovery-session UUIDs remain visible only to the isolated lifecycle owner.
-- The Worker claim fence needs only these server-derived join/filter columns;
-- the Web role reaches recovery state exclusively through the definer commands.
GRANT SELECT (
    recovery_intent_id, firm_id, matter_id, replacement_run_id
) ON TABLE public.case_agent_ledger_exception_recovery_intents
    TO lawcase_agent_worker;
GRANT SELECT (
    recovery_intent_id, firm_id, matter_id, current_outcome,
    transfer_control_assignment_id
) ON TABLE public.case_agent_ledger_exception_recovery_intent_heads
    TO lawcase_agent_worker;
GRANT SELECT (run_id, firm_id, matter_id)
    ON TABLE public.case_agent_ledger_exception_recovery_quarantines
    TO lawcase_agent_worker;

REVOKE ALL ON FUNCTION
    public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ) FROM PUBLIC, lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.transfer_case_agent_ledger_exception_control_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ) FROM PUBLIC, lawcase_agent_worker;
GRANT EXECUTE ON FUNCTION
    public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ) TO lawcase_web_application;
GRANT EXECUTE ON FUNCTION
    public.transfer_case_agent_ledger_exception_control_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ) TO lawcase_web_application;
REVOKE ALL ON FUNCTION
    public.prohibit_case_agent_ledger_exception_recovery_intent_mutation(),
    public.guard_case_agent_ledger_exception_recovery_intent_head(),
    public.wake_case_agent_run()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.enforce_case_agent_ledger_extraction_confirmation_integrity(),
    public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

-- The wake trigger now runs as the isolated definer owner.  It needs only
-- this projection write plus the existing 0049 control reads.
GRANT SELECT, INSERT, UPDATE ON TABLE public.case_agent_run_inbox
    TO lawcase_ledger_confirmation_owner;
GRANT SELECT ON TABLE public.case_agent_goals
    TO lawcase_ledger_confirmation_owner;

COMMIT;
