-- Database-owned OIDC/MFA authority for the 0042 low-risk confirmation path.
--
-- The Web application may read a review projection, but it may not choose the
-- confirming actor or directly insert a promotion/confirmation receipt.  A
-- first SECURITY DEFINER function resolves one opaque server-held Web session
-- to its ACTIVE user and current LEAD_LAWYER matter role, then issues a short-
-- lived, append-only approval bound to the exact batch/version/candidate set.
-- After the server re-reads the authorized source pages, a second definer
-- function consumes that opaque approval and repeats every authoritative
-- check before atomically writing the complete eligible lane.
--
-- Deployment MUST pre-provision ``lawcase_ledger_confirmation_owner`` as
-- NOLOGIN, NOINHERIT, NOSUPERUSER and NOBYPASSRLS.  The migration is expected
-- to run as a separate schema/migration owner that may ALTER OWNER and GRANT.
-- ``lawcase_web_application`` must not own the protected tables or inherit the
-- definer owner.  Failure of any of those prerequisites aborts the migration.
--
-- 0024 does not persist an ``amr``/``acr`` assurance marker.  These commands
-- can therefore prove a live HTTPS OIDC session row and the Web service can
-- require ``AuthenticationMethod.OIDC_MFA``, but PostgreSQL cannot by itself
-- re-prove the upstream MFA ceremony from the stored row.  Adding an immutable
-- session-assurance claim remains a separate identity-schema hardening item.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
         WHERE rolname = 'lawcase_ledger_confirmation_owner'
           AND NOT rolcanlogin AND NOT rolinherit
           AND NOT rolsuper AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION
            'lawcase_ledger_confirmation_owner must be a NOLOGIN NOINHERIT NOBYPASSRLS role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
         WHERE rolname = 'lawcase_web_application'
    ) THEN
        RAISE EXCEPTION 'lawcase_web_application role is missing';
    END IF;
    IF pg_catalog.pg_has_role(
        'lawcase_web_application',
        'lawcase_ledger_confirmation_owner',
        'MEMBER'
    ) THEN
        RAISE EXCEPTION
            'lawcase_web_application must not inherit the ledger confirmation owner';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_roles owner_role
            ON owner_role.oid = relation.relowner
         WHERE relation.oid = ANY(ARRAY[
             'public.case_facts'::regclass,
             'public.case_transactions'::regclass,
             'public.case_agent_ledger_extraction_batches'::regclass,
             'public.case_agent_ledger_extraction_staging_events'::regclass,
             'public.case_agent_ledger_extraction_candidates'::regclass,
             'public.case_agent_ledger_extraction_candidate_pages'::regclass,
             'public.case_agent_ledger_extraction_promotions'::regclass,
             'public.case_agent_ledger_extraction_batch_confirmations'::regclass,
             'public.case_agent_ledger_exception_groups'::regclass,
             'public.case_agent_ledger_exception_group_members'::regclass,
             'public.case_agent_ledger_exception_group_decisions'::regclass,
             'public.case_agent_ledger_exception_decision_events'::regclass
         ])
           AND owner_role.rolname = 'lawcase_web_application'
    ) THEN
        RAISE EXCEPTION
            'lawcase_web_application cannot own protected ledger confirmation tables';
    END IF;
END;
$$;

-- 0024 deliberately permits exact token-digest SELECT but uses
-- ``app.web_session_id`` only for revocation UPDATE.  The NOLOGIN definer
-- therefore gets one additional SELECT-only policy for the exact opaque UUID;
-- the Web application role is not a member of this policy role and cannot use
-- it by setting the GUC itself.
CREATE POLICY web_sessions_ledger_confirmation_exact_session
    ON public.web_sessions
    FOR SELECT
    TO lawcase_ledger_confirmation_owner
    USING (
        session_id::text = pg_catalog.current_setting(
            'app.web_session_id', true
        )
    );

CREATE TABLE public.case_agent_ledger_extraction_session_approvals (
    session_approval_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    web_session_id uuid NOT NULL,
    extraction_batch_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    expected_matter_version integer NOT NULL CHECK (expected_matter_version > 0),
    command_name text NOT NULL CHECK (
        command_name = 'CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH'
    ),
    idempotency_key text NOT NULL CHECK (
        length(idempotency_key) BETWEEN 16 AND 128
        AND idempotency_key ~ '^[A-Za-z0-9._~-]+$'
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    candidate_set_hash char(64) NOT NULL CHECK (
        candidate_set_hash ~ '^[0-9a-f]{64}$'
    ),
    decision_hash char(64) NOT NULL CHECK (decision_hash ~ '^[0-9a-f]{64}$'),
    source_binding_hash char(64) NOT NULL CHECK (
        source_binding_hash ~ '^[0-9a-f]{64}$'
    ),
    approval_attempt integer NOT NULL CHECK (approval_attempt > 0),
    authorized_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    expires_at timestamptz NOT NULL,
    UNIQUE (
        web_session_id, firm_id, matter_id, actor_id, command_name,
        idempotency_key, approval_attempt
    ),
    UNIQUE (session_approval_id, firm_id, matter_id),
    FOREIGN KEY (web_session_id)
        REFERENCES public.web_sessions(session_id),
    FOREIGN KEY (actor_id, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id),
    FOREIGN KEY (extraction_batch_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_extraction_batches(
            extraction_batch_id, firm_id, matter_id
        ),
    CHECK (
        expires_at > authorized_at
        AND expires_at <= authorized_at + interval '5 minutes'
    )
);

ALTER TABLE public.case_agent_ledger_extraction_batch_confirmations
    ADD COLUMN session_approval_id uuid UNIQUE,
    ADD CONSTRAINT case_agent_ledger_confirmation_session_approval_fk
        FOREIGN KEY (session_approval_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_extraction_session_approvals(
            session_approval_id, firm_id, matter_id
        );

CREATE INDEX case_agent_ledger_extraction_session_approvals_expiry_idx
    ON public.case_agent_ledger_extraction_session_approvals (expires_at);
CREATE INDEX case_agent_ledger_extraction_session_approvals_batch_idx
    ON public.case_agent_ledger_extraction_session_approvals (
        firm_id, matter_id, extraction_batch_id, authorized_at
    );

ALTER TABLE public.case_agent_ledger_extraction_session_approvals
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_extraction_session_approvals
    FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_ledger_extraction_session_approvals_exact_or_firm
    ON public.case_agent_ledger_extraction_session_approvals
    USING (
        firm_id::text = pg_catalog.current_setting('app.firm_id', true)
        OR session_approval_id::text = pg_catalog.current_setting(
            'app.ledger_confirmation_approval_id', true
        )
    )
    WITH CHECK (
        firm_id::text = pg_catalog.current_setting('app.firm_id', true)
    );

CREATE FUNCTION public.prohibit_case_agent_ledger_session_approval_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'case Agent ledger session approvals are append-only';
END;
$$;

CREATE TRIGGER case_agent_ledger_extraction_session_approvals_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_extraction_session_approvals
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_session_approval_mutation();

CREATE FUNCTION public.authorize_case_agent_ledger_extraction_low_risk_confirmation(
    input_session_id uuid,
    input_matter_id uuid,
    input_batch_id uuid,
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
    batch_row record;
    prior_row record;
    prior_approval public.case_agent_ledger_extraction_session_approvals%ROWTYPE;
    current_review_version integer;
    low_risk_status text;
    eligible_count integer;
    unpromoted_count integer;
    candidate_hashes text;
    source_page_bindings text;
    candidate_set_hash char(64);
    source_binding_hash char(64);
    decision_hash char(64);
    new_approval_id uuid;
    next_approval_attempt integer := 1;
    approval_expiry timestamptz;
    expected_request_hash char(64);
BEGIN
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'ledger confirmation authorization input is invalid';
    END IF;
    -- ``_payload_hash`` serializes this three-field ASCII object with sorted
    -- keys and compact separators.  Reproduce those exact bytes so the
    -- application cannot choose an unrelated idempotency request hash.
    expected_request_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            '{"expected_version":' || input_expected_version::text ||
            ',"extraction_batch_id":"' || input_batch_id::text ||
            '","matter_id":"' || input_matter_id::text || '"}',
            'UTF8'
        ), 'sha256'
    ), 'hex');
    IF input_request_hash IS DISTINCT FROM expected_request_hash THEN
        RAISE EXCEPTION 'ledger confirmation request hash differs';
    END IF;

    -- Ignore every caller-controlled tenant/actor setting.  The only
    -- pre-identity selector is the exact opaque UUID held by the server.
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
        RAISE EXCEPTION 'ledger confirmation requires a live OIDC/MFA Web session';
    END IF;
    PERFORM pg_catalog.set_config(
        'app.firm_id', session_row.firm_id::text, true
    );
    -- Stable authority lock order for every path (including receipt replay):
    -- exact session -> matter -> active user -> active role binding.
    PERFORM 1 FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger confirmation matter is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.users actor
         WHERE actor.user_id = session_row.user_id
           AND actor.firm_id = session_row.firm_id
           AND actor.status = 'ACTIVE'
         FOR SHARE
    ) THEN
        RAISE EXCEPTION 'ledger confirmation session actor is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
      JOIN public.users lead
        ON lead.user_id = role_binding.user_id
       AND lead.firm_id = role_binding.firm_id
     WHERE role_binding.matter_id = input_matter_id
       AND role_binding.firm_id = session_row.firm_id
       AND role_binding.user_id = session_row.user_id
       AND role_binding.role = 'LEAD_LAWYER'
       AND role_binding.revoked_at IS NULL
       AND lead.status = 'ACTIVE'
     FOR SHARE OF role_binding, lead;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger confirmation requires the active lead lawyer';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            session_row.user_id::text || '|' || input_matter_id::text ||
            '|CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH|' ||
            input_idempotency_key,
            0
        )
    );

    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = session_row.firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = session_row.user_id
       AND idempotency.command_name =
            'CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH'
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                input_idempotency_key
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXTRACTION_BATCH'
           OR prior_row.response_json->>'object_id' IS DISTINCT FROM
                input_batch_id::text
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                (input_expected_version + 1)::text
           OR NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_extraction_batch_confirmations confirmation
                  JOIN public.case_agent_ledger_extraction_session_approvals approval
                    ON approval.session_approval_id =
                            confirmation.session_approval_id
                   AND approval.firm_id = confirmation.firm_id
                   AND approval.matter_id = confirmation.matter_id
                  JOIN public.audit_events audit
                    ON audit.event_id =
                        (prior_row.response_json->>'audit_event_id')::uuid
                   AND audit.firm_id = confirmation.firm_id
                   AND audit.matter_id = confirmation.matter_id
                 WHERE confirmation.extraction_batch_id = input_batch_id
                   AND confirmation.firm_id = session_row.firm_id
                   AND confirmation.matter_id = input_matter_id
                   AND confirmation.confirmed_by = session_row.user_id
                   AND confirmation.confirmed_matter_version =
                        input_expected_version + 1
                   AND approval.actor_id = session_row.user_id
                   AND approval.request_hash = input_request_hash
                   AND audit.actor_id = session_row.user_id
                   AND audit.event_type =
                        'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED'
                   AND audit.input_version = input_expected_version
                   AND audit.output_version = input_expected_version + 1
                   AND audit.payload->>'extraction_batch_id' =
                        input_batch_id::text
                   AND audit.payload->>'lawyer_batch_decision_hash' =
                        confirmation.lawyer_batch_decision_hash::text
           ) THEN
            RAISE EXCEPTION 'ledger confirmation replay differs from its batch';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'status', 'COMPLETED', 'receipt', prior_row.response_json
        );
    END IF;

    SELECT matter.version,
           EXISTS (
               SELECT 1
                 FROM public.matter_actor_roles role_binding
                 JOIN public.users lead
                   ON lead.user_id = role_binding.user_id
                  AND lead.firm_id = role_binding.firm_id
                WHERE role_binding.matter_id = matter.matter_id
                  AND role_binding.firm_id = matter.firm_id
                  AND role_binding.user_id = session_row.user_id
                  AND role_binding.role = 'LEAD_LAWYER'
                  AND role_binding.revoked_at IS NULL
                  AND lead.status = 'ACTIVE'
           ) AS actor_is_lead
      INTO prior_row
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger confirmation matter is missing';
    END IF;
    IF prior_row.actor_is_lead IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'ledger confirmation requires the active lead lawyer';
    END IF;
    IF prior_row.version IS DISTINCT FROM input_expected_version THEN
        RAISE EXCEPTION 'ledger confirmation matter version is stale';
    END IF;

    current_review_version :=
        public.case_agent_ledger_extraction_current_review_version(
            input_batch_id, session_row.firm_id, input_matter_id
        );
    IF current_review_version IS DISTINCT FROM input_expected_version THEN
        RAISE EXCEPTION 'ledger confirmation batch is not current';
    END IF;
    SELECT status.low_risk_lane_status
      INTO low_risk_status
      FROM public.case_agent_ledger_extraction_batch_review_status(
          input_batch_id, session_row.firm_id, input_matter_id
      ) status;
    IF low_risk_status IS DISTINCT FROM 'OPEN' THEN
        RAISE EXCEPTION 'ledger confirmation low-risk lane is not open';
    END IF;

    SELECT batch.extraction_batch_id, batch.run_id, batch.graph_id,
           batch.task_id, batch.eligible_candidate_count,
           task.input_hash AS task_input_hash
      INTO batch_row
      FROM public.case_agent_ledger_extraction_batches batch
      JOIN public.case_agent_runs run
        ON run.run_id = batch.run_id AND run.firm_id = batch.firm_id
       AND run.matter_id = batch.matter_id
      JOIN public.case_agent_tasks task
        ON task.graph_id = batch.graph_id AND task.task_id = batch.task_id
       AND task.run_id = batch.run_id AND task.firm_id = batch.firm_id
       AND task.matter_id = batch.matter_id
     WHERE batch.extraction_batch_id = input_batch_id
       AND batch.firm_id = session_row.firm_id
       AND batch.matter_id = input_matter_id
       AND run.current_graph_id = batch.graph_id
       AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
       AND NOT run.is_stale AND NOT run.is_cancelled;
    IF NOT FOUND OR batch_row.eligible_candidate_count < 1 THEN
        RAISE EXCEPTION 'ledger confirmation batch has no eligible lane';
    END IF;
    IF NOT public.case_agent_ledger_extraction_run_staging_complete(
        batch_row.run_id, session_row.firm_id, input_matter_id
    ) THEN
        RAISE EXCEPTION
            'ledger confirmation is blocked until the verified run is fully staged';
    END IF;

    SELECT count(*)::integer,
           count(*) FILTER (
               WHERE promotion.extraction_candidate_id IS NULL
           )::integer,
           pg_catalog.string_agg(
               candidate.candidate_hash::text, ','
               ORDER BY candidate.candidate_hash
           )
      INTO eligible_count, unpromoted_count, candidate_hashes
      FROM public.case_agent_ledger_extraction_candidates candidate
      LEFT JOIN public.case_agent_ledger_extraction_promotions promotion
        ON promotion.extraction_candidate_id =
                candidate.extraction_candidate_id
       AND promotion.extraction_batch_id = candidate.extraction_batch_id
       AND promotion.firm_id = candidate.firm_id
       AND promotion.matter_id = candidate.matter_id
     WHERE candidate.extraction_batch_id = input_batch_id
       AND candidate.firm_id = session_row.firm_id
       AND candidate.matter_id = input_matter_id
       AND candidate.review_lane = 'BULK_PROMOTION_ELIGIBLE'
       AND candidate.eligible_for_bulk_promotion = true;
    IF eligible_count IS DISTINCT FROM batch_row.eligible_candidate_count
       OR unpromoted_count IS DISTINCT FROM eligible_count
       OR eligible_count < 1 OR candidate_hashes IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation eligible lane is incomplete';
    END IF;

    SELECT pg_catalog.string_agg(
               candidate.candidate_hash::text || ':' ||
               candidate_page.evidence_page_id::text || ':' ||
               candidate_page.source_text_sha256::text,
               ',' ORDER BY candidate.candidate_hash,
                            candidate_page.evidence_page_id
           )
      INTO source_page_bindings
      FROM public.case_agent_ledger_extraction_candidates candidate
      JOIN public.case_agent_ledger_extraction_candidate_pages candidate_page
        ON candidate_page.extraction_candidate_id =
                candidate.extraction_candidate_id
       AND candidate_page.firm_id = candidate.firm_id
       AND candidate_page.matter_id = candidate.matter_id
     WHERE candidate.extraction_batch_id = input_batch_id
       AND candidate.firm_id = session_row.firm_id
       AND candidate.matter_id = input_matter_id
       AND candidate.review_lane = 'BULK_PROMOTION_ELIGIBLE'
       AND candidate.eligible_for_bulk_promotion = true;
    IF source_page_bindings IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation source binding is empty';
    END IF;

    candidate_set_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            'case-agent-ledger-candidate-set-v1|' || input_batch_id::text ||
            '|' || candidate_hashes,
            'UTF8'
        ), 'sha256'
    ), 'hex');
    source_binding_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            'case-agent-ledger-source-verification-v1|' ||
            input_batch_id::text || '|' || batch_row.run_id::text || '|' ||
            batch_row.task_id::text || '|' ||
            batch_row.task_input_hash::text || '|' || candidate_hashes ||
            '|' || source_page_bindings,
            'UTF8'
        ), 'sha256'
    ), 'hex');
    decision_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            'case-ledger-extraction-batch-decision-v2|' ||
            'CONFIRM_REVIEWED_LOW_RISK_GROUP|' || input_matter_id::text ||
            '|' || input_batch_id::text || '|' || batch_row.run_id::text ||
            '|' || session_row.user_id::text || '|' || candidate_hashes,
            'UTF8'
        ), 'sha256'
    ), 'hex');

    SELECT approval.* INTO prior_approval
      FROM public.case_agent_ledger_extraction_session_approvals approval
     WHERE approval.web_session_id = input_session_id
       AND approval.firm_id = session_row.firm_id
       AND approval.matter_id = input_matter_id
       AND approval.actor_id = session_row.user_id
       AND approval.command_name =
            'CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH'
       AND approval.idempotency_key = input_idempotency_key
     ORDER BY approval.approval_attempt DESC
     LIMIT 1;
    IF FOUND THEN
        IF prior_approval.extraction_batch_id IS DISTINCT FROM input_batch_id
           OR prior_approval.expected_matter_version IS DISTINCT FROM
                input_expected_version
           OR prior_approval.request_hash IS DISTINCT FROM input_request_hash
           OR prior_approval.candidate_set_hash IS DISTINCT FROM
                candidate_set_hash
           OR prior_approval.source_binding_hash IS DISTINCT FROM
                source_binding_hash
           OR prior_approval.decision_hash IS DISTINCT FROM decision_hash THEN
            RAISE EXCEPTION 'ledger confirmation session approval is stale';
        END IF;
        IF prior_approval.expires_at > pg_catalog.clock_timestamp() THEN
            RETURN pg_catalog.jsonb_build_object(
                'status', 'AUTHORIZED',
                'approval_id', prior_approval.session_approval_id
            );
        END IF;
        -- An expired approval never becomes live again.  The same immutable
        -- intent may receive a new append-only server attempt after every
        -- authoritative binding above has been recomputed unchanged.
        next_approval_attempt := prior_approval.approval_attempt + 1;
    END IF;

    approval_expiry := LEAST(
        session_row.expires_at,
        pg_catalog.clock_timestamp() + interval '5 minutes'
    );
    IF approval_expiry <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'ledger confirmation session expires too soon';
    END IF;
    INSERT INTO public.case_agent_ledger_extraction_session_approvals (
        web_session_id, extraction_batch_id, firm_id, matter_id, actor_id,
        expected_matter_version, command_name, idempotency_key, request_hash,
        candidate_set_hash, decision_hash, source_binding_hash,
        approval_attempt, expires_at
    ) VALUES (
        input_session_id, input_batch_id, session_row.firm_id,
        input_matter_id, session_row.user_id, input_expected_version,
        'CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH',
        input_idempotency_key, input_request_hash, candidate_set_hash,
        decision_hash, source_binding_hash, next_approval_attempt,
        approval_expiry
    ) RETURNING session_approval_id INTO new_approval_id;
    RETURN pg_catalog.jsonb_build_object(
        'status', 'AUTHORIZED', 'approval_id', new_approval_id
    );
END;
$$;

CREATE FUNCTION public.finalize_case_agent_ledger_extraction_low_risk_confirmation(
    input_session_approval_id uuid,
    input_source_verification_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    approval public.case_agent_ledger_extraction_session_approvals%ROWTYPE;
    session_row record;
    batch_row record;
    prior_row record;
    candidate_row record;
    current_review_version integer;
    low_risk_status text;
    eligible_count integer;
    unpromoted_count integer;
    candidate_hashes text;
    source_page_bindings text;
    candidate_set_hash char(64);
    source_binding_hash char(64);
    decision_hash char(64);
    evidence_links jsonb;
    target_id uuid;
    target_type text;
    next_version integer;
    audit_event_id uuid;
    response_json jsonb;
BEGIN
    IF input_source_verification_hash IS NULL
       OR input_source_verification_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'ledger confirmation source verification is invalid';
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', '', true);
    PERFORM pg_catalog.set_config(
        'app.ledger_confirmation_approval_id',
        input_session_approval_id::text,
        true
    );
    SELECT stored.* INTO approval
      FROM public.case_agent_ledger_extraction_session_approvals stored
     WHERE stored.session_approval_id = input_session_approval_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger confirmation session approval is missing';
    END IF;
    PERFORM pg_catalog.set_config(
        'app.web_session_id', approval.web_session_id::text, true
    );
    PERFORM pg_catalog.set_config('app.firm_id', approval.firm_id::text, true);

    SELECT session.session_id, session.firm_id, session.user_id,
           session.expires_at
      INTO session_row
      FROM public.web_sessions session
     WHERE session.session_id = approval.web_session_id
       AND session.firm_id = approval.firm_id
       AND session.user_id = approval.actor_id
       AND session.revoked_at IS NULL
       AND session.expires_at > pg_catalog.clock_timestamp()
       AND session.authenticated_at <= pg_catalog.clock_timestamp()
       AND session.issuer LIKE 'https://%'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger confirmation OIDC/MFA Web session is no longer active';
    END IF;
    PERFORM 1 FROM public.matters matter
     WHERE matter.matter_id = approval.matter_id
       AND matter.firm_id = approval.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger confirmation matter is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.users actor
         WHERE actor.user_id = approval.actor_id
           AND actor.firm_id = approval.firm_id
           AND actor.status = 'ACTIVE'
         FOR SHARE
    ) THEN
        RAISE EXCEPTION 'ledger confirmation session actor is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
      JOIN public.users lead
        ON lead.user_id = role_binding.user_id
       AND lead.firm_id = role_binding.firm_id
     WHERE role_binding.matter_id = approval.matter_id
       AND role_binding.firm_id = approval.firm_id
       AND role_binding.user_id = approval.actor_id
       AND role_binding.role = 'LEAD_LAWYER'
       AND role_binding.revoked_at IS NULL
       AND lead.status = 'ACTIVE'
     FOR SHARE OF role_binding, lead;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger confirmation requires the current lead lawyer';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            approval.actor_id::text || '|' || approval.matter_id::text ||
            '|' || approval.command_name || '|' || approval.idempotency_key,
            0
        )
    );
    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = approval.firm_id
       AND idempotency.matter_id = approval.matter_id
       AND idempotency.actor_id = approval.actor_id
       AND idempotency.command_name = approval.command_name
       AND idempotency.idempotency_key = approval.idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM approval.request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                approval.command_name
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                approval.idempotency_key
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                approval.matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                (approval.expected_matter_version + 1)::text
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXTRACTION_BATCH'
           OR prior_row.response_json->>'object_id' IS DISTINCT FROM
                approval.extraction_batch_id::text
           OR NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_extraction_batch_confirmations confirmation
                  JOIN public.audit_events audit
                    ON audit.event_id =
                        (prior_row.response_json->>'audit_event_id')::uuid
                   AND audit.firm_id = confirmation.firm_id
                   AND audit.matter_id = confirmation.matter_id
                 WHERE confirmation.extraction_batch_id =
                        approval.extraction_batch_id
                   AND confirmation.firm_id = approval.firm_id
                   AND confirmation.matter_id = approval.matter_id
                   AND confirmation.session_approval_id =
                        approval.session_approval_id
                   AND confirmation.confirmed_by = approval.actor_id
                   AND confirmation.confirmed_matter_version =
                        approval.expected_matter_version + 1
                   AND audit.actor_id = approval.actor_id
                   AND audit.event_type =
                        'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED'
                   AND audit.input_version = approval.expected_matter_version
                   AND audit.output_version =
                        approval.expected_matter_version + 1
                   AND audit.payload->>'extraction_batch_id' =
                        approval.extraction_batch_id::text
                   AND audit.payload->>'lawyer_batch_decision_hash' =
                        confirmation.lawyer_batch_decision_hash::text
           ) THEN
            RAISE EXCEPTION 'ledger confirmation replay differs from approval';
        END IF;
        RETURN prior_row.response_json;
    END IF;
    IF approval.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'ledger confirmation session approval expired';
    END IF;

    SELECT matter.version,
           EXISTS (
               SELECT 1
                 FROM public.matter_actor_roles role_binding
                 JOIN public.users lead
                   ON lead.user_id = role_binding.user_id
                  AND lead.firm_id = role_binding.firm_id
                WHERE role_binding.matter_id = matter.matter_id
                  AND role_binding.firm_id = matter.firm_id
                  AND role_binding.user_id = approval.actor_id
                  AND role_binding.role = 'LEAD_LAWYER'
                  AND role_binding.revoked_at IS NULL
                  AND lead.status = 'ACTIVE'
           ) AS actor_is_lead
      INTO prior_row
      FROM public.matters matter
     WHERE matter.matter_id = approval.matter_id
       AND matter.firm_id = approval.firm_id
     FOR UPDATE;
    IF NOT FOUND OR prior_row.actor_is_lead IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'ledger confirmation requires the current lead lawyer';
    END IF;
    IF prior_row.version IS DISTINCT FROM approval.expected_matter_version THEN
        RAISE EXCEPTION 'ledger confirmation matter version changed';
    END IF;

    current_review_version :=
        public.case_agent_ledger_extraction_current_review_version(
            approval.extraction_batch_id, approval.firm_id,
            approval.matter_id
        );
    IF current_review_version IS DISTINCT FROM approval.expected_matter_version THEN
        RAISE EXCEPTION 'ledger confirmation batch is no longer current';
    END IF;
    SELECT status.low_risk_lane_status
      INTO low_risk_status
      FROM public.case_agent_ledger_extraction_batch_review_status(
          approval.extraction_batch_id, approval.firm_id,
          approval.matter_id
      ) status;
    IF low_risk_status IS DISTINCT FROM 'OPEN' THEN
        RAISE EXCEPTION 'ledger confirmation low-risk lane is no longer open';
    END IF;

    SELECT batch.run_id, batch.graph_id, batch.task_id,
           batch.eligible_candidate_count,
           task.input_hash AS task_input_hash
      INTO batch_row
      FROM public.case_agent_ledger_extraction_batches batch
      JOIN public.case_agent_runs run
        ON run.run_id = batch.run_id AND run.firm_id = batch.firm_id
       AND run.matter_id = batch.matter_id
      JOIN public.case_agent_tasks task
        ON task.graph_id = batch.graph_id AND task.task_id = batch.task_id
       AND task.run_id = batch.run_id AND task.firm_id = batch.firm_id
       AND task.matter_id = batch.matter_id
     WHERE batch.extraction_batch_id = approval.extraction_batch_id
       AND batch.firm_id = approval.firm_id
       AND batch.matter_id = approval.matter_id
       AND run.current_graph_id = batch.graph_id
       AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
       AND NOT run.is_stale AND NOT run.is_cancelled
     FOR SHARE OF run;
    IF NOT FOUND OR batch_row.eligible_candidate_count < 1 THEN
        RAISE EXCEPTION 'ledger confirmation batch is unavailable';
    END IF;
    IF NOT public.case_agent_ledger_extraction_run_staging_complete(
        batch_row.run_id, approval.firm_id, approval.matter_id
    ) THEN
        RAISE EXCEPTION
            'ledger confirmation verified run is no longer fully staged';
    END IF;

    SELECT count(*)::integer,
           count(*) FILTER (
               WHERE promotion.extraction_candidate_id IS NULL
           )::integer,
           pg_catalog.string_agg(
               candidate.candidate_hash::text, ','
               ORDER BY candidate.candidate_hash
           )
      INTO eligible_count, unpromoted_count, candidate_hashes
      FROM public.case_agent_ledger_extraction_candidates candidate
      LEFT JOIN public.case_agent_ledger_extraction_promotions promotion
        ON promotion.extraction_candidate_id =
                candidate.extraction_candidate_id
       AND promotion.extraction_batch_id = candidate.extraction_batch_id
       AND promotion.firm_id = candidate.firm_id
       AND promotion.matter_id = candidate.matter_id
     WHERE candidate.extraction_batch_id = approval.extraction_batch_id
       AND candidate.firm_id = approval.firm_id
       AND candidate.matter_id = approval.matter_id
       AND candidate.review_lane = 'BULK_PROMOTION_ELIGIBLE'
       AND candidate.eligible_for_bulk_promotion = true;
    IF eligible_count IS DISTINCT FROM batch_row.eligible_candidate_count
       OR unpromoted_count IS DISTINCT FROM eligible_count
       OR eligible_count < 1 OR candidate_hashes IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation eligible lane changed';
    END IF;

    SELECT pg_catalog.string_agg(
               candidate.candidate_hash::text || ':' ||
               candidate_page.evidence_page_id::text || ':' ||
               candidate_page.source_text_sha256::text,
               ',' ORDER BY candidate.candidate_hash,
                            candidate_page.evidence_page_id
           )
      INTO source_page_bindings
      FROM public.case_agent_ledger_extraction_candidates candidate
      JOIN public.case_agent_ledger_extraction_candidate_pages candidate_page
        ON candidate_page.extraction_candidate_id =
                candidate.extraction_candidate_id
       AND candidate_page.firm_id = candidate.firm_id
       AND candidate_page.matter_id = candidate.matter_id
     WHERE candidate.extraction_batch_id = approval.extraction_batch_id
       AND candidate.firm_id = approval.firm_id
       AND candidate.matter_id = approval.matter_id
       AND candidate.review_lane = 'BULK_PROMOTION_ELIGIBLE'
       AND candidate.eligible_for_bulk_promotion = true;
    IF source_page_bindings IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation source binding changed';
    END IF;

    candidate_set_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            'case-agent-ledger-candidate-set-v1|' ||
            approval.extraction_batch_id::text || '|' || candidate_hashes,
            'UTF8'
        ), 'sha256'
    ), 'hex');
    source_binding_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            'case-agent-ledger-source-verification-v1|' ||
            approval.extraction_batch_id::text || '|' ||
            batch_row.run_id::text || '|' || batch_row.task_id::text || '|' ||
            batch_row.task_input_hash::text || '|' || candidate_hashes ||
            '|' || source_page_bindings,
            'UTF8'
        ), 'sha256'
    ), 'hex');
    decision_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            'case-ledger-extraction-batch-decision-v2|' ||
            'CONFIRM_REVIEWED_LOW_RISK_GROUP|' || approval.matter_id::text ||
            '|' || approval.extraction_batch_id::text || '|' ||
            batch_row.run_id::text || '|' || approval.actor_id::text || '|' ||
            candidate_hashes,
            'UTF8'
        ), 'sha256'
    ), 'hex');
    IF candidate_set_hash IS DISTINCT FROM approval.candidate_set_hash
       OR source_binding_hash IS DISTINCT FROM approval.source_binding_hash
       OR source_binding_hash IS DISTINCT FROM input_source_verification_hash
       OR decision_hash IS DISTINCT FROM approval.decision_hash THEN
        RAISE EXCEPTION 'ledger confirmation approval binding changed';
    END IF;

    -- Conservative duplicate/conflict recheck against the current formal
    -- ledger.  Exceptions are not selected by this function and cannot be
    -- inserted into a promotion mapping.
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_extraction_candidates candidate
         WHERE candidate.extraction_batch_id = approval.extraction_batch_id
           AND candidate.firm_id = approval.firm_id
           AND candidate.matter_id = approval.matter_id
           AND candidate.review_lane = 'BULK_PROMOTION_ELIGIBLE'
           AND candidate.eligible_for_bulk_promotion = true
           AND (
               (candidate.candidate_kind = 'FACT' AND EXISTS (
                    SELECT 1 FROM public.case_facts fact
                     WHERE fact.firm_id = approval.firm_id
                       AND fact.matter_id = approval.matter_id
                       AND fact.status <> 'INVALIDATED'
                       AND (
                           fact.original_text =
                                candidate.candidate_payload->>'fact_text'
                           OR EXISTS (
                               SELECT 1
                                 FROM pg_catalog.jsonb_array_elements_text(
                                    candidate.candidate_payload->
                                        'evidence_page_ids'
                                 ) candidate_page(page_id)
                                 JOIN LATERAL pg_catalog.jsonb_array_elements(
                                    fact.evidence_links
                                 ) fact_link(value) ON true
                                WHERE fact_link.value->>'evidence_id' =
                                    candidate_page.page_id
                           )
                       )
               ))
               OR (candidate.candidate_kind = 'TRANSACTION' AND EXISTS (
                    SELECT 1 FROM public.case_transactions transaction_row
                     WHERE transaction_row.firm_id = approval.firm_id
                       AND transaction_row.matter_id = approval.matter_id
                       AND transaction_row.status <> 'INVALIDATED'
                       AND (
                           (
                               candidate.candidate_payload->>
                                    'transaction_reference' IS NOT NULL
                               AND transaction_row.transaction_reference
                                    IS NOT NULL
                               AND pg_catalog.lower(
                                    transaction_row.transaction_reference
                               ) = pg_catalog.lower(
                                    candidate.candidate_payload->>
                                        'transaction_reference'
                               )
                           )
                           OR (
                               transaction_row.local_date IS NOT DISTINCT FROM
                                    (NULLIF(
                                        candidate.candidate_payload->>
                                            'local_date', ''
                                    ))::date
                               AND transaction_row.date_precision =
                                    candidate.candidate_payload->>'date_precision'
                               AND transaction_row.amount =
                                    (candidate.candidate_payload->>'amount')::numeric
                               AND transaction_row.currency =
                                    candidate.candidate_payload->>'currency'
                               AND transaction_row.direction =
                                    candidate.candidate_payload->>'direction'
                               AND transaction_row.channel =
                                    candidate.candidate_payload->>'channel'
                           )
                           OR EXISTS (
                               SELECT 1
                                 FROM pg_catalog.jsonb_array_elements_text(
                                    candidate.candidate_payload->
                                        'evidence_page_ids'
                                 ) candidate_page(page_id)
                                 JOIN LATERAL pg_catalog.jsonb_array_elements(
                                    transaction_row.evidence_links
                                 ) transaction_link(value) ON true
                                WHERE transaction_link.value->>'evidence_id' =
                                    candidate_page.page_id
                           )
                       )
               ))
           )
    ) THEN
        RAISE EXCEPTION 'ledger confirmation now conflicts with the formal ledger';
    END IF;

    FOR candidate_row IN
        SELECT candidate.extraction_candidate_id,
               candidate.candidate_hash, candidate.candidate_kind,
               candidate.candidate_payload
          FROM public.case_agent_ledger_extraction_candidates candidate
         WHERE candidate.extraction_batch_id = approval.extraction_batch_id
           AND candidate.firm_id = approval.firm_id
           AND candidate.matter_id = approval.matter_id
           AND candidate.review_lane = 'BULK_PROMOTION_ELIGIBLE'
           AND candidate.eligible_for_bulk_promotion = true
         ORDER BY candidate.candidate_hash
    LOOP
        SELECT pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'evidence_id', page.evidence_page_id::text,
                       'original_file_sha256', source.original_file_sha256,
                       'page_number', page.page_number,
                       'region_id', NULL,
                       'original_label', source.original_label
                   ) ORDER BY page.page_number, page.evidence_page_id
               )
          INTO evidence_links
          FROM public.case_agent_ledger_extraction_candidate_pages candidate_page
          JOIN public.evidence_pages page
            ON page.evidence_page_id = candidate_page.evidence_page_id
           AND page.firm_id = candidate_page.firm_id
           AND page.matter_id = candidate_page.matter_id
          JOIN public.evidence_original_files source
            ON source.evidence_file_id = page.evidence_file_id
           AND source.firm_id = page.firm_id
           AND source.matter_id = page.matter_id
         WHERE candidate_page.extraction_candidate_id =
                candidate_row.extraction_candidate_id
           AND candidate_page.firm_id = approval.firm_id
           AND candidate_page.matter_id = approval.matter_id;
        IF evidence_links IS NULL
           OR pg_catalog.jsonb_array_length(evidence_links) < 1 THEN
            RAISE EXCEPTION 'ledger confirmation candidate has no source pages';
        END IF;

        target_id := public.gen_random_uuid();
        IF candidate_row.candidate_kind = 'FACT' THEN
            IF candidate_row.candidate_payload->>'kind' <> 'FACT'
               OR candidate_row.candidate_payload->>'fact_text' IS NULL
               OR candidate_row.candidate_payload->>'fact_text' <>
                    pg_catalog.btrim(
                        candidate_row.candidate_payload->>'fact_text'
                    )
               OR length(candidate_row.candidate_payload->>'fact_text')
                    NOT BETWEEN 1 AND 2000 THEN
                RAISE EXCEPTION 'ledger confirmation fact payload is invalid';
            END IF;
            INSERT INTO public.case_facts (
                fact_id, firm_id, matter_id, original_text, origin, status,
                evidence_links, decision_hash, decided_by
            ) VALUES (
                target_id, approval.firm_id, approval.matter_id,
                candidate_row.candidate_payload->>'fact_text',
                'AGENT_CANDIDATE', 'CONFIRMED', evidence_links,
                decision_hash, approval.actor_id
            );
            target_type := 'FACT';
        ELSIF candidate_row.candidate_kind = 'TRANSACTION' THEN
            IF candidate_row.candidate_payload->>'kind' <> 'TRANSACTION'
               OR candidate_row.candidate_payload->>'date_precision'
                    NOT IN ('EXACT_DATE', 'MONTH_ONLY', 'YEAR_ONLY', 'UNKNOWN')
               OR (
                    candidate_row.candidate_payload->>'date_precision' =
                        'EXACT_DATE'
                    AND candidate_row.candidate_payload->>'local_date' IS NULL
               )
               OR (
                    candidate_row.candidate_payload->>'date_precision' <>
                        'EXACT_DATE'
                    AND candidate_row.candidate_payload->>'local_date' IS NOT NULL
               )
               OR (candidate_row.candidate_payload->>'amount')::numeric <= 0
               OR candidate_row.candidate_payload->>'currency'
                    !~ '^[A-Z]{3}$'
               OR candidate_row.candidate_payload->>'direction'
                    NOT IN ('OUTGOING', 'INCOMING', 'UNKNOWN')
               OR candidate_row.candidate_payload->>'channel'
                    NOT IN (
                        'WECHAT', 'BANK', 'CASH', 'CHAT_RECORD',
                        'LOAN_INSTRUMENT', 'OTHER'
                    ) THEN
                RAISE EXCEPTION
                    'ledger confirmation transaction payload is invalid';
            END IF;
            INSERT INTO public.case_transactions (
                transaction_id, firm_id, matter_id, local_date,
                date_precision, amount, currency, direction, payer_label,
                payee_label, channel, transaction_reference, evidence_links,
                status, confirmation_hash, confirmed_by
            ) VALUES (
                target_id, approval.firm_id, approval.matter_id,
                (NULLIF(
                    candidate_row.candidate_payload->>'local_date', ''
                ))::date,
                candidate_row.candidate_payload->>'date_precision',
                (candidate_row.candidate_payload->>'amount')::numeric,
                candidate_row.candidate_payload->>'currency',
                candidate_row.candidate_payload->>'direction',
                candidate_row.candidate_payload->>'payer_label',
                candidate_row.candidate_payload->>'payee_label',
                candidate_row.candidate_payload->>'channel',
                candidate_row.candidate_payload->>'transaction_reference',
                evidence_links, 'CONFIRMED', decision_hash,
                approval.actor_id
            );
            target_type := 'TRANSACTION';
        ELSE
            RAISE EXCEPTION 'ledger confirmation candidate kind is invalid';
        END IF;
        INSERT INTO public.case_agent_ledger_extraction_promotions (
            extraction_promotion_id, extraction_batch_id,
            extraction_candidate_id, firm_id, matter_id,
            target_object_type, target_object_id, promoted_matter_version,
            lawyer_batch_decision_hash, promoted_by
        ) VALUES (
            public.gen_random_uuid(), approval.extraction_batch_id,
            candidate_row.extraction_candidate_id, approval.firm_id,
            approval.matter_id, target_type, target_id,
            approval.expected_matter_version + 1, decision_hash,
            approval.actor_id
        );
    END LOOP;

    INSERT INTO public.case_agent_ledger_extraction_batch_confirmations (
        extraction_batch_id, firm_id, matter_id,
        lawyer_batch_decision_hash, confirmed_candidate_count,
        confirmed_matter_version, confirmed_by, session_approval_id
    ) VALUES (
        approval.extraction_batch_id, approval.firm_id, approval.matter_id,
        decision_hash, eligible_count, approval.expected_matter_version + 1,
        approval.actor_id, approval.session_approval_id
    );

    UPDATE public.matters matter
       SET version = matter.version + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE matter.matter_id = approval.matter_id
       AND matter.firm_id = approval.firm_id
       AND matter.version = approval.expected_matter_version
     RETURNING matter.version INTO next_version;
    IF next_version IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation matter changed before commit';
    END IF;
    audit_event_id := public.gen_random_uuid();
    INSERT INTO public.audit_events (
        event_id, firm_id, matter_id, actor_id, event_type, input_version,
        output_version, request_id, payload
    ) VALUES (
        audit_event_id, approval.firm_id, approval.matter_id,
        approval.actor_id,
        'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED',
        approval.expected_matter_version, next_version,
        public.gen_random_uuid(),
        pg_catalog.jsonb_build_object(
            'extraction_batch_id', approval.extraction_batch_id,
            'lawyer_batch_decision_hash', decision_hash,
            'confirmed_candidate_count', eligible_count,
            'exception_candidates_included', false,
            'source_text_reverified', true,
            'session_approval_id', approval.session_approval_id
        )
    );
    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        approval.firm_id, approval.matter_id, next_version,
        'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED',
        pg_catalog.jsonb_build_object(
            'audit_event_id', audit_event_id,
            'object_id', approval.extraction_batch_id
        )
    );
    response_json := pg_catalog.jsonb_build_object(
        'command_name', approval.command_name,
        'idempotency_key', approval.idempotency_key,
        'matter_id', approval.matter_id::text,
        'matter_version', next_version,
        'audit_event_id', audit_event_id::text,
        'object_type', 'CASE_LEDGER_EXTRACTION_BATCH',
        'object_id', approval.extraction_batch_id::text
    );
    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        approval.firm_id, approval.matter_id, approval.actor_id,
        approval.command_name, approval.idempotency_key,
        approval.request_hash, response_json
    );
    RETURN response_json;
END;
$$;

-- Exception routing has no external source-read phase, so one atomic definer
-- command is sufficient.  It derives firm/actor from the same opaque live
-- session, revalidates the complete immutable group and preserves 0047's
-- rule that only an exception-only terminal run advances the matter version.
CREATE FUNCTION public.decide_case_agent_ledger_exception_group_from_web_session(
    input_session_id uuid,
    input_matter_id uuid,
    input_exception_group_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_decision text,
    input_reason_code text,
    input_reason_note text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    session_row record;
    matter_row record;
    group_row record;
    inserted_row record;
    prior_row record;
    event_id uuid;
    normalized_note text;
    run_is_resolved boolean;
    refresh_count integer;
    pending_refresh_count integer;
    newest_refresh_version integer;
    pending_refresh_version integer;
    batch_count integer;
    low_risk_batch_count integer;
    next_version integer;
    authority_audit_event_id uuid;
    prior_group_run_id uuid;
    response_json jsonb;
    expected_request_hash char(64);
BEGIN
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_decision NOT IN (
            'REJECT_AS_DUPLICATE', 'REQUEST_REEXTRACTION',
            'REQUEST_MORE_EVIDENCE', 'DEFER_WITH_REASON'
       )
       OR input_reason_code NOT IN (
            'DUPLICATE_CONFIRMED', 'SOURCE_QUALITY_INSUFFICIENT',
            'EXTRACTION_CONFLICT', 'EVIDENCE_GAP',
            'PARTY_DATE_AMOUNT_UNCLEAR', 'AWAITING_CLIENT_INPUT',
            'AWAITING_EXTERNAL_RECORD', 'NEEDS_LEAD_REVIEW'
       )
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'ledger exception decision input is invalid';
    END IF;
    normalized_note := CASE
        WHEN input_reason_note IS NULL THEN NULL
        ELSE NULLIF(pg_catalog.btrim(input_reason_note), '')
    END;
    IF normalized_note IS NOT NULL AND (
        length(normalized_note) > 500
        OR pg_catalog.octet_length(normalized_note) > 2000
        OR normalized_note ~ '[\x00-\x08\x0B\x0C\x0E-\x1F]'
    ) THEN
        RAISE EXCEPTION 'ledger exception decision note is invalid';
    END IF;
    expected_request_hash := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            'case-ledger-exception-group-request-v1' || E'\n' ||
            input_matter_id::text || E'\n' ||
            input_expected_version::text || E'\n' ||
            input_exception_group_id::text || E'\n' ||
            input_decision || E'\n' || input_reason_code || E'\n' ||
            pg_catalog.octet_length(
                COALESCE(normalized_note, '')
            )::text || ':' || COALESCE(normalized_note, ''),
            'UTF8'
        ), 'sha256'
    ), 'hex');
    IF input_request_hash IS DISTINCT FROM expected_request_hash THEN
        RAISE EXCEPTION 'ledger exception decision request hash differs';
    END IF;

    PERFORM pg_catalog.set_config('app.firm_id', '', true);
    PERFORM pg_catalog.set_config(
        'app.web_session_id', input_session_id::text, true
    );
    SELECT session.session_id, session.firm_id, session.user_id,
           session.expires_at
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
            'ledger exception decision requires a live OIDC/MFA Web session';
    END IF;
    PERFORM pg_catalog.set_config(
        'app.firm_id', session_row.firm_id::text, true
    );
    PERFORM 1 FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception matter is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.users actor
         WHERE actor.user_id = session_row.user_id
           AND actor.firm_id = session_row.firm_id
           AND actor.status = 'ACTIVE'
         FOR SHARE
    ) THEN
        RAISE EXCEPTION 'ledger exception session actor is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
      JOIN public.users lead
        ON lead.user_id = role_binding.user_id
       AND lead.firm_id = role_binding.firm_id
     WHERE role_binding.matter_id = input_matter_id
       AND role_binding.firm_id = session_row.firm_id
       AND role_binding.user_id = session_row.user_id
       AND role_binding.role = 'LEAD_LAWYER'
       AND role_binding.revoked_at IS NULL
       AND lead.status = 'ACTIVE'
     FOR SHARE OF role_binding, lead;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception decision requires the active lead lawyer';
    END IF;
    SELECT exception_group.run_id
      INTO prior_group_run_id
      FROM public.case_agent_ledger_exception_groups exception_group
     WHERE exception_group.exception_group_id = input_exception_group_id
       AND exception_group.firm_id = session_row.firm_id
       AND exception_group.matter_id = input_matter_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception group is missing';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            session_row.user_id::text || '|' || input_matter_id::text ||
            '|DECIDE_CASE_LEDGER_EXCEPTION_GROUP|' || input_idempotency_key,
            0
        )
    );
    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = session_row.firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = session_row.user_id
       AND idempotency.command_name =
            'DECIDE_CASE_LEDGER_EXCEPTION_GROUP'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'DECIDE_CASE_LEDGER_EXCEPTION_GROUP'
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                input_idempotency_key
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR NOT (
                (
                    prior_row.response_json->>'object_type' =
                        'CASE_LEDGER_EXCEPTION_GROUP'
                    AND prior_row.response_json->>'object_id' =
                        input_exception_group_id::text
                    AND prior_row.response_json->>'matter_version' =
                        input_expected_version::text
                    AND EXISTS (
                        SELECT 1
                          FROM public.case_agent_ledger_exception_group_decisions decision
                          JOIN public.case_agent_ledger_exception_decision_events event
                            ON event.exception_decision_id =
                                    decision.exception_decision_id
                           AND event.firm_id = decision.firm_id
                           AND event.matter_id = decision.matter_id
                         WHERE decision.exception_group_id =
                                input_exception_group_id
                           AND decision.firm_id = session_row.firm_id
                           AND decision.matter_id = input_matter_id
                           AND decision.decided_by = session_row.user_id
                           AND decision.idempotency_key =
                                input_idempotency_key
                           AND decision.request_hash = input_request_hash
                           AND event.exception_decision_event_id =
                                (prior_row.response_json->>
                                    'audit_event_id')::uuid
                           AND event.actor_id = session_row.user_id
                           AND event.matter_version = input_expected_version
                           AND event.request_hash = input_request_hash
                           AND event.payload->>'decision_hash' =
                                decision.decision_hash::text
                    )
                )
                OR (
                    prior_row.response_json->>'object_type' =
                        'CASE_LEDGER_EXTRACTION_RUN_REVIEW'
                    AND prior_row.response_json->>'object_id' =
                        prior_group_run_id::text
                    AND prior_row.response_json->>'matter_version' =
                        (input_expected_version + 1)::text
                    AND EXISTS (
                        SELECT 1
                          FROM public.case_agent_ledger_exception_group_decisions decision
                          JOIN public.audit_events audit
                            ON audit.event_id =
                                (prior_row.response_json->>
                                    'audit_event_id')::uuid
                           AND audit.firm_id = decision.firm_id
                           AND audit.matter_id = decision.matter_id
                         WHERE decision.exception_group_id =
                                input_exception_group_id
                           AND decision.run_id = prior_group_run_id
                           AND decision.firm_id = session_row.firm_id
                           AND decision.matter_id = input_matter_id
                           AND decision.decided_by = session_row.user_id
                           AND decision.idempotency_key =
                                input_idempotency_key
                           AND decision.request_hash = input_request_hash
                           AND audit.actor_id = session_row.user_id
                           AND audit.event_type =
                                'CASE_LEDGER_EXTRACTION_EXCEPTION_ONLY_RUN_RESOLVED'
                           AND audit.input_version = input_expected_version
                           AND audit.output_version =
                                input_expected_version + 1
                           AND audit.payload->>'run_id' =
                                prior_group_run_id::text
                           AND audit.payload->>'terminal_exception_group_id' =
                                input_exception_group_id::text
                           AND audit.payload->>'decision_hash' =
                                decision.decision_hash::text
                    )
                )
           ) THEN
            RAISE EXCEPTION 'ledger exception idempotency replay differs';
        END IF;
        RETURN prior_row.response_json;
    END IF;

    SELECT matter.version,
           EXISTS (
               SELECT 1
                 FROM public.matter_actor_roles role_binding
                 JOIN public.users lead
                   ON lead.user_id = role_binding.user_id
                  AND lead.firm_id = role_binding.firm_id
                WHERE role_binding.matter_id = matter.matter_id
                  AND role_binding.firm_id = matter.firm_id
                  AND role_binding.user_id = session_row.user_id
                  AND role_binding.role = 'LEAD_LAWYER'
                  AND role_binding.revoked_at IS NULL
                  AND lead.status = 'ACTIVE'
           ) AS actor_is_lead
      INTO matter_row
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND OR matter_row.actor_is_lead IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'ledger exception decision requires the active lead lawyer';
    END IF;
    IF matter_row.version IS DISTINCT FROM input_expected_version THEN
        -- Stable, deliberately narrow conflict code.  The application maps
        -- only this code to HTTP 409; every other P0001/transport failure
        -- remains an unknown server outcome.
        RAISE EXCEPTION USING
            ERRCODE = 'P4091',
            MESSAGE = 'ledger exception decision matter version is stale';
    END IF;

    SELECT exception_group.exception_group_id,
           exception_group.extraction_batch_id, exception_group.run_id,
           exception_group.group_key_hash,
           exception_group.candidate_set_hash,
           exception_group.candidate_count,
           exception_group.source_policy, exception_group.risk_policy,
           decision.exception_decision_id
      INTO group_row
      FROM public.case_agent_ledger_exception_groups exception_group
      LEFT JOIN public.case_agent_ledger_exception_group_decisions decision
        ON decision.exception_group_id = exception_group.exception_group_id
       AND decision.extraction_batch_id = exception_group.extraction_batch_id
       AND decision.firm_id = exception_group.firm_id
       AND decision.matter_id = exception_group.matter_id
     WHERE exception_group.exception_group_id = input_exception_group_id
       AND exception_group.firm_id = session_row.firm_id
       AND exception_group.matter_id = input_matter_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception group is missing';
    END IF;
    IF group_row.exception_decision_id IS NOT NULL THEN
        -- Exact-key replay returned above.  Reaching this branch therefore
        -- proves that another actor/key/intent already won this immutable
        -- group, rather than an unknown database failure.
        RAISE EXCEPTION USING
            ERRCODE = 'P4092',
            MESSAGE = 'ledger exception group was terminally routed by another intent';
    END IF;
    IF NOT public.validate_case_agent_ledger_exception_group_integrity(
        group_row.extraction_batch_id, session_row.firm_id, input_matter_id
    ) THEN
        RAISE EXCEPTION 'ledger exception group integrity failed';
    END IF;
    IF NOT public.case_agent_ledger_extraction_run_staging_complete(
        group_row.run_id, session_row.firm_id, input_matter_id
    ) THEN
        RAISE EXCEPTION
            'ledger exception decision is blocked until the run is fully staged';
    END IF;
    IF public.case_agent_ledger_extraction_current_review_version(
        group_row.extraction_batch_id, session_row.firm_id, input_matter_id
    ) IS DISTINCT FROM input_expected_version THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P4091',
            MESSAGE = 'ledger exception group review version is stale';
    END IF;

    INSERT INTO public.case_agent_ledger_exception_group_decisions (
        exception_decision_id, exception_group_id, extraction_batch_id,
        run_id, firm_id, matter_id, bound_group_key_hash,
        bound_candidate_set_hash, bound_candidate_count, decision,
        reason_code, reason_note, decision_hash, expected_matter_version,
        decided_by, idempotency_key, request_hash
    ) VALUES (
        public.gen_random_uuid(), input_exception_group_id,
        group_row.extraction_batch_id, group_row.run_id,
        session_row.firm_id, input_matter_id, group_row.group_key_hash,
        group_row.candidate_set_hash, group_row.candidate_count,
        input_decision, input_reason_code, normalized_note,
        repeat('0', 64), input_expected_version, session_row.user_id,
        input_idempotency_key, input_request_hash
    ) RETURNING exception_decision_id, decision_hash
      INTO inserted_row;
    SELECT event.exception_decision_event_id
      INTO event_id
      FROM public.case_agent_ledger_exception_decision_events event
     WHERE event.exception_decision_id = inserted_row.exception_decision_id
       AND event.firm_id = session_row.firm_id
       AND event.matter_id = input_matter_id;
    IF event_id IS NULL THEN
        RAISE EXCEPTION 'ledger exception decision event is missing';
    END IF;

    run_is_resolved := public.case_agent_ledger_extraction_run_review_resolved(
        group_row.run_id, session_row.firm_id, input_matter_id
    );
    SELECT count(*)::integer,
           count(*) FILTER (WHERE request_status = 'PENDING')::integer,
           max(target_matter_version),
           max(target_matter_version) FILTER (
               WHERE request_status = 'PENDING'
           )
      INTO refresh_count, pending_refresh_count, newest_refresh_version,
           pending_refresh_version
      FROM public.case_agent_snapshot_refresh_requests request
     WHERE request.run_id = group_row.run_id
       AND request.firm_id = session_row.firm_id
       AND request.matter_id = input_matter_id;

    IF run_is_resolved AND refresh_count = 0 THEN
        SELECT count(*)::integer,
               count(*) FILTER (
                   WHERE eligible_candidate_count > 0
               )::integer
          INTO batch_count, low_risk_batch_count
          FROM public.case_agent_ledger_extraction_batches batch
         WHERE batch.run_id = group_row.run_id
           AND batch.firm_id = session_row.firm_id
           AND batch.matter_id = input_matter_id;
        IF batch_count < 1 OR low_risk_batch_count <> 0 THEN
            RAISE EXCEPTION
                'resolved mixed exception run is missing a low-risk refresh';
        END IF;
        UPDATE public.matters matter
           SET version = matter.version + 1,
               updated_at = pg_catalog.clock_timestamp()
         WHERE matter.matter_id = input_matter_id
           AND matter.firm_id = session_row.firm_id
           AND matter.version = input_expected_version
         RETURNING matter.version INTO next_version;
        IF next_version IS NULL THEN
            RAISE EXCEPTION 'ledger exception matter changed before commit';
        END IF;
        authority_audit_event_id := public.gen_random_uuid();
        INSERT INTO public.audit_events (
            event_id, firm_id, matter_id, actor_id, event_type,
            input_version, output_version, request_id, payload
        ) VALUES (
            authority_audit_event_id, session_row.firm_id, input_matter_id,
            session_row.user_id,
            'CASE_LEDGER_EXTRACTION_EXCEPTION_ONLY_RUN_RESOLVED',
            input_expected_version, next_version, public.gen_random_uuid(),
            pg_catalog.jsonb_build_object(
                'run_id', group_row.run_id,
                'extraction_batch_id', group_row.extraction_batch_id,
                'terminal_exception_group_id', input_exception_group_id,
                'decision_hash', inserted_row.decision_hash,
                'exception_review_complete', true,
                'formal_ledger_write', false,
                'legal_conclusion', false
            )
        );
        INSERT INTO public.outbox_events (
            firm_id, matter_id, aggregate_version, event_type, payload
        ) VALUES (
            session_row.firm_id, input_matter_id, next_version,
            'CASE_LEDGER_EXTRACTION_EXCEPTION_ONLY_RUN_RESOLVED',
            pg_catalog.jsonb_build_object(
                'audit_event_id', authority_audit_event_id,
                'object_id', group_row.run_id
            )
        );
        response_json := pg_catalog.jsonb_build_object(
            'command_name', 'DECIDE_CASE_LEDGER_EXCEPTION_GROUP',
            'idempotency_key', input_idempotency_key,
            'matter_id', input_matter_id::text,
            'matter_version', next_version,
            'audit_event_id', authority_audit_event_id::text,
            'object_type', 'CASE_LEDGER_EXTRACTION_RUN_REVIEW',
            'object_id', group_row.run_id::text
        );
    ELSE
        IF run_is_resolved AND (
            pending_refresh_count <> 1
            OR pending_refresh_version IS DISTINCT FROM newest_refresh_version
        ) THEN
            RAISE EXCEPTION
                'resolved exception run did not release one latest refresh';
        ELSIF NOT run_is_resolved AND pending_refresh_count <> 0 THEN
            RAISE EXCEPTION
                'unresolved exception run exposed a pending refresh';
        END IF;
        response_json := pg_catalog.jsonb_build_object(
            'command_name', 'DECIDE_CASE_LEDGER_EXCEPTION_GROUP',
            'idempotency_key', input_idempotency_key,
            'matter_id', input_matter_id::text,
            'matter_version', input_expected_version,
            'audit_event_id', event_id::text,
            'object_type', 'CASE_LEDGER_EXCEPTION_GROUP',
            'object_id', input_exception_group_id::text
        );
    END IF;

    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        session_row.firm_id, input_matter_id, session_row.user_id,
        'DECIDE_CASE_LEDGER_EXCEPTION_GROUP', input_idempotency_key,
        input_request_hash, response_json
    );
    RETURN response_json;
END;
$$;

-- Once a 0042 target has an immutable promotion mapping, a broad legacy Web
-- application grant cannot rewrite its confirming actor/hash or delete it.
-- Unmapped legacy fact/transaction commands remain outside this migration and
-- are called out explicitly in the handoff as a residual hardening scope.
CREATE FUNCTION public.protect_session_bound_ledger_extraction_target()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_identifier uuid;
    target_kind text;
BEGIN
    IF TG_TABLE_NAME = 'case_facts' THEN
        target_identifier := OLD.fact_id;
        target_kind := 'FACT';
    ELSIF TG_TABLE_NAME = 'case_transactions' THEN
        target_identifier := OLD.transaction_id;
        target_kind := 'TRANSACTION';
    ELSE
        RAISE EXCEPTION 'unsupported protected ledger target';
    END IF;
    -- Resolve the guard's tenant from the target row, never from a caller-
    -- controlled GUC.  Otherwise clearing ``app.firm_id`` could hide the
    -- FORCE-RLS promotion mapping from this SECURITY DEFINER trigger.
    PERFORM pg_catalog.set_config('app.firm_id', OLD.firm_id::text, true);
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_extraction_promotions promotion
          JOIN public.case_agent_ledger_extraction_batch_confirmations confirmation
            ON confirmation.extraction_batch_id = promotion.extraction_batch_id
           AND confirmation.firm_id = promotion.firm_id
           AND confirmation.matter_id = promotion.matter_id
           AND confirmation.session_approval_id IS NOT NULL
         WHERE promotion.target_object_type = target_kind
           AND promotion.target_object_id = target_identifier
           AND promotion.firm_id = OLD.firm_id
           AND promotion.matter_id = OLD.matter_id
    ) THEN
        RAISE EXCEPTION
            'session-bound ledger extraction targets are immutable; supersede through a new reviewed batch';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER case_facts_session_bound_extraction_target_immutable
    BEFORE UPDATE OR DELETE ON public.case_facts
    FOR EACH ROW EXECUTE FUNCTION
        public.protect_session_bound_ledger_extraction_target();
CREATE TRIGGER case_transactions_session_bound_extraction_target_immutable
    BEFORE UPDATE OR DELETE ON public.case_transactions
    FOR EACH ROW EXECUTE FUNCTION
        public.protect_session_bound_ledger_extraction_target();

ALTER TABLE public.case_agent_ledger_extraction_session_approvals
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.authorize_case_agent_ledger_extraction_low_risk_confirmation(
    uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.finalize_case_agent_ledger_extraction_low_risk_confirmation(
    uuid, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.decide_case_agent_ledger_exception_group_from_web_session(
    uuid, uuid, uuid, integer, text, text, text, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.protect_session_bound_ledger_extraction_target()
    OWNER TO lawcase_ledger_confirmation_owner;

REVOKE ALL ON TABLE
    public.case_agent_ledger_extraction_session_approvals
    FROM PUBLIC, lawcase_web_application;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
    public.case_agent_ledger_extraction_batches,
    public.case_agent_ledger_extraction_staging_events,
    public.case_agent_ledger_extraction_candidates,
    public.case_agent_ledger_extraction_candidate_pages,
    public.case_agent_ledger_extraction_promotions,
    public.case_agent_ledger_extraction_batch_confirmations,
    public.case_agent_ledger_exception_groups,
    public.case_agent_ledger_exception_group_members,
    public.case_agent_ledger_exception_group_decisions,
    public.case_agent_ledger_exception_decision_events
    FROM lawcase_web_application;
REVOKE ALL ON FUNCTION
    public.authorize_case_agent_ledger_extraction_low_risk_confirmation(
        uuid, uuid, uuid, integer, text, text
    ) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.finalize_case_agent_ledger_extraction_low_risk_confirmation(
        uuid, text
    ) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.decide_case_agent_ledger_exception_group_from_web_session(
        uuid, uuid, uuid, integer, text, text, text, text, text
    ) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.protect_session_bound_ledger_extraction_target()
    FROM PUBLIC, lawcase_web_application;

-- ``public`` is a trusted lookup schema for the 0046/0047 trigger chain.
-- No runtime or definer role may create shadow relations/functions in it.
REVOKE CREATE ON SCHEMA public
    FROM PUBLIC, lawcase_web_application, lawcase_ledger_confirmation_owner;
GRANT USAGE ON SCHEMA public
    TO lawcase_ledger_confirmation_owner, lawcase_web_application;
GRANT EXECUTE ON FUNCTION
    public.authorize_case_agent_ledger_extraction_low_risk_confirmation(
        uuid, uuid, uuid, integer, text, text
    ) TO lawcase_web_application;
GRANT EXECUTE ON FUNCTION
    public.finalize_case_agent_ledger_extraction_low_risk_confirmation(
        uuid, text
    ) TO lawcase_web_application;
GRANT EXECUTE ON FUNCTION
    public.decide_case_agent_ledger_exception_group_from_web_session(
        uuid, uuid, uuid, integer, text, text, text, text, text
    ) TO lawcase_web_application;

-- The definer owner receives only the reads/writes required by this one
-- command and its already-installed 0046/0047 outbox triggers.
GRANT SELECT ON TABLE
    public.web_sessions,
    public.users,
    public.matters,
    public.matter_actor_roles,
    public.command_idempotency,
    public.case_agent_ledger_extraction_batches,
    public.case_agent_ledger_extraction_candidates,
    public.case_agent_ledger_extraction_candidate_pages,
    public.case_agent_ledger_extraction_promotions,
    public.case_agent_ledger_extraction_batch_confirmations,
    public.case_agent_runs,
    public.case_agent_task_graphs,
    public.case_agent_tasks,
    public.case_agent_verification_attempts,
    public.case_agent_verification_receipts,
    public.case_agent_work_plan_promotions,
    public.audit_events,
    public.evidence_pages,
    public.evidence_original_files,
    public.case_facts,
    public.case_transactions,
    public.case_agent_ledger_exception_groups,
    public.case_agent_ledger_exception_group_members,
    public.case_agent_ledger_exception_group_decisions,
    public.case_agent_ledger_exception_decision_events,
    public.case_agent_snapshot_refresh_requests,
    public.case_agent_run_inbox
    TO lawcase_ledger_confirmation_owner;
GRANT INSERT ON TABLE
    public.case_facts,
    public.case_transactions,
    public.case_agent_ledger_extraction_promotions,
    public.case_agent_ledger_extraction_batch_confirmations,
    public.audit_events,
    public.outbox_events,
    public.command_idempotency,
    public.case_agent_snapshot_refresh_requests,
    public.case_agent_ledger_exception_group_decisions,
    public.case_agent_ledger_exception_decision_events
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (version, updated_at) ON TABLE public.matters
    TO lawcase_ledger_confirmation_owner;
-- PostgreSQL locking clauses require UPDATE privilege on at least one
-- column.  These narrow grants are lock entitlements for the isolated
-- NOLOGIN definer owner; the functions never mutate these identity keys.
GRANT UPDATE (session_id) ON TABLE public.web_sessions
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (user_id) ON TABLE public.users
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (user_id) ON TABLE public.matter_actor_roles
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (run_id) ON TABLE public.case_agent_runs
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE ON TABLE
    public.case_agent_snapshot_refresh_requests,
    public.case_agent_run_inbox
    TO lawcase_ledger_confirmation_owner;
GRANT EXECUTE ON FUNCTION
    public.case_agent_ledger_extraction_current_review_version(
        uuid, uuid, uuid
    ),
    public.case_agent_ledger_extraction_batch_review_status(
        uuid, uuid, uuid
    ),
    public.case_agent_ledger_extraction_target_matches_candidate(
        uuid, uuid, uuid, uuid, text, uuid
    ),
    public.validate_case_agent_ledger_exception_group_integrity(
        uuid, uuid, uuid
    ),
    public.case_agent_ledger_extraction_run_review_resolved(
        uuid, uuid, uuid
    ),
    public.case_agent_ledger_extraction_run_staging_complete(
        uuid, uuid, uuid
    )
    TO lawcase_ledger_confirmation_owner;

REVOKE ALL ON FUNCTION
    public.prohibit_case_agent_ledger_session_approval_mutation()
    FROM PUBLIC;

COMMIT;
