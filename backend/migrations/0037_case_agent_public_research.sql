-- Durable, server-owned public-Web research bindings and exact egress archives.
--
-- Search is an A3 action.  A browser/model may choose neither provider, URL,
-- credential nor raw query.  The execution Worker derives a minimal public
-- query from current governed case references and an exact lawyer-approved
-- task.  The existing 0031 external-submission marker remains the first
-- durable network boundary; these tables bind that marker to one Brave GET,
-- archive the exact response/receipt, and make UNKNOWN lookup-only.

BEGIN;

CREATE TABLE case_agent_public_research_bindings (
    binding_id uuid PRIMARY KEY,
    external_request_id uuid NOT NULL UNIQUE,
    egress_grant_id uuid NOT NULL UNIQUE,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    question_id uuid NOT NULL,
    input_refs jsonb NOT NULL CHECK (
        jsonb_typeof(input_refs) = 'array'
        AND jsonb_array_length(input_refs) BETWEEN 1 AND 100
    ),
    -- Private server fields: FORCE RLS, no browser projection and no logging.
    confidential_question text NOT NULL CHECK (
        octet_length(confidential_question) BETWEEN 1 AND 20000
    ),
    confidential_question_hash char(64) NOT NULL CHECK (
        confidential_question_hash ~ '^[0-9a-f]{64}$'
    ),
    private_terms jsonb NOT NULL CHECK (jsonb_typeof(private_terms) = 'array'),
    private_terms_hash char(64) NOT NULL CHECK (
        private_terms_hash ~ '^[0-9a-f]{64}$'
    ),
    public_terms jsonb NOT NULL CHECK (
        jsonb_typeof(public_terms) = 'array'
        AND jsonb_array_length(public_terms) BETWEEN 2 AND 24
    ),
    public_query_text text NOT NULL CHECK (
        octet_length(public_query_text) BETWEEN 3 AND 720
    ),
    query_hash char(64) NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),
    purpose text NOT NULL CHECK (purpose = 'LEGAL_AUTHORITY_DISCOVERY'),
    language text NOT NULL CHECK (language IN ('zh-CN', 'en')),
    max_results integer NOT NULL CHECK (max_results BETWEEN 1 AND 20),
    task_input_hash char(64) NOT NULL CHECK (task_input_hash ~ '^[0-9a-f]{64}$'),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    source_binding_hash char(64) NOT NULL CHECK (
        source_binding_hash ~ '^[0-9a-f]{64}$'
    ),
    authorization_hash char(64) NOT NULL CHECK (
        authorization_hash ~ '^[0-9a-f]{64}$'
    ),
    binding_hash char(64) NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    provider_id text NOT NULL CHECK (provider_id = 'brave_web_search'),
    service_id text NOT NULL CHECK (service_id = 'web_search_v1'),
    allowed_host text NOT NULL CHECK (allowed_host = 'api.search.brave.com'),
    approved_by uuid NOT NULL,
    created_by_worker uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, binding_hash),
    UNIQUE (external_request_id, firm_id, matter_id),
    UNIQUE (binding_id, external_request_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (attempt_id, run_id, task_id, firm_id, matter_id)
        REFERENCES case_agent_task_attempts(attempt_id, run_id, task_id, firm_id, matter_id),
    FOREIGN KEY (approval_id, run_id, task_id, firm_id, matter_id)
        REFERENCES case_agent_approvals(approval_id, run_id, task_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (created_by_worker, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '30 minutes')
);

CREATE TABLE case_agent_public_research_egress_grants (
    egress_grant_id uuid PRIMARY KEY,
    binding_id uuid NOT NULL,
    external_request_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    method text NOT NULL CHECK (method = 'GET'),
    exact_host text NOT NULL CHECK (exact_host = 'api.search.brave.com'),
    exact_scheme text NOT NULL CHECK (exact_scheme = 'https'),
    exact_port integer NOT NULL CHECK (exact_port = 443),
    redirects_allowed boolean NOT NULL CHECK (redirects_allowed = false),
    max_requests integer NOT NULL CHECK (max_requests = 1),
    max_response_bytes integer NOT NULL CHECK (max_response_bytes = 2097152),
    query_data_minimized boolean NOT NULL CHECK (query_data_minimized = true),
    grant_hash char(64) NOT NULL CHECK (grant_hash ~ '^[0-9a-f]{64}$'),
    issued_by_worker uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (binding_id),
    UNIQUE (external_request_id),
    UNIQUE (egress_grant_id, external_request_id, firm_id, matter_id),
    FOREIGN KEY (binding_id, external_request_id, firm_id, matter_id)
        REFERENCES case_agent_public_research_bindings(
            binding_id, external_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (issued_by_worker, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (expires_at > issued_at AND expires_at <= issued_at + interval '30 minutes')
);

-- This immutable row is inserted only after 0031's exact submission marker is
-- visible and before the broker opens a socket.  There is exactly one send.
CREATE TABLE case_agent_public_research_exchanges (
    exchange_id uuid PRIMARY KEY,
    binding_id uuid NOT NULL,
    external_request_id uuid NOT NULL,
    egress_grant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    request_url text NOT NULL CHECK (
        request_url ~ '^https://api[.]search[.]brave[.]com/res/v1/web/search[?]'
        AND octet_length(request_url) <= 4096
    ),
    method text NOT NULL CHECK (method = 'GET'),
    request_body_bytes integer NOT NULL CHECK (request_body_bytes = 0),
    submission_record_id uuid NOT NULL,
    started_by_worker uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (binding_id),
    UNIQUE (external_request_id),
    UNIQUE (attempt_id),
    UNIQUE (exchange_id, external_request_id, firm_id, matter_id),
    FOREIGN KEY (binding_id, external_request_id, firm_id, matter_id)
        REFERENCES case_agent_public_research_bindings(
            binding_id, external_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (egress_grant_id, external_request_id, firm_id, matter_id)
        REFERENCES case_agent_public_research_egress_grants(
            egress_grant_id, external_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (submission_record_id, firm_id, matter_id)
        REFERENCES case_agent_external_submissions(submission_id, firm_id, matter_id),
    FOREIGN KEY (attempt_id, run_id, task_id, firm_id, matter_id)
        REFERENCES case_agent_task_attempts(attempt_id, run_id, task_id, firm_id, matter_id),
    FOREIGN KEY (started_by_worker, firm_id) REFERENCES users(user_id, firm_id)
);

-- Outcomes are append-only.  Sequence 1 is the first observation; sequence 2
-- exists only to recover a previously UNKNOWN exchange from a hash-bound S3
-- archive.  No outcome authorizes a second provider call.
CREATE TABLE case_agent_public_research_outcomes (
    outcome_id uuid PRIMARY KEY,
    exchange_id uuid NOT NULL,
    external_request_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    outcome_sequence integer NOT NULL CHECK (outcome_sequence IN (1, 2)),
    status text NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION')),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_sha256 char(64) CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    response_bytes integer CHECK (response_bytes BETWEEN 2 AND 2097152),
    archive_sha256 char(64) CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    archive_bytes integer CHECK (archive_bytes BETWEEN 2 AND 4194304),
    archive_object_key text,
    archive_object_version_id text,
    egress_receipt jsonb,
    egress_receipt_hash char(64) CHECK (egress_receipt_hash ~ '^[0-9a-f]{64}$'),
    error_code text CHECK (
        error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{2,79}$'
    ),
    recovered_from_unknown boolean NOT NULL DEFAULT false,
    recorded_by_worker uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exchange_id, outcome_sequence),
    UNIQUE (external_request_id, outcome_sequence),
    UNIQUE (outcome_id, firm_id, matter_id),
    FOREIGN KEY (exchange_id, external_request_id, firm_id, matter_id)
        REFERENCES case_agent_public_research_exchanges(
            exchange_id, external_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (recorded_by_worker, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (status = 'SUCCEEDED'
          AND response_sha256 IS NOT NULL AND response_bytes IS NOT NULL
          AND archive_sha256 IS NOT NULL AND archive_bytes IS NOT NULL
          AND archive_object_key IS NOT NULL
          AND archive_object_key ~ '^case-agent-research/v1/'
          AND egress_receipt IS NOT NULL
          AND jsonb_typeof(egress_receipt) = 'object'
          AND egress_receipt_hash IS NOT NULL AND error_code IS NULL)
        OR (status IN ('FAILED', 'UNKNOWN_SUBMISSION')
          AND response_sha256 IS NULL AND response_bytes IS NULL
          AND archive_sha256 IS NULL AND archive_bytes IS NULL
          AND archive_object_key IS NULL AND archive_object_version_id IS NULL
          AND egress_receipt IS NULL AND egress_receipt_hash IS NULL
          AND error_code IS NOT NULL)
    ),
    CHECK (
        (outcome_sequence = 1 AND recovered_from_unknown = false)
        OR (outcome_sequence = 2 AND status = 'SUCCEEDED' AND recovered_from_unknown = true)
    )
);

CREATE INDEX case_agent_public_research_binding_task_idx
    ON case_agent_public_research_bindings(firm_id, matter_id, run_id, task_id);
CREATE INDEX case_agent_public_research_outcome_recovery_idx
    ON case_agent_public_research_outcomes(
        firm_id, external_request_id, outcome_sequence DESC
    );

-- The execution role never needs to scan another matter's private query rows.
-- These indexes support the exact same-firm/current authority predicates used
-- by the trigger and binding port without weakening RLS.
CREATE INDEX case_agent_public_research_binding_attempt_idx
    ON case_agent_public_research_bindings(firm_id, attempt_id);

CREATE FUNCTION validate_case_agent_public_research_binding() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    task_row case_agent_tasks%ROWTYPE;
    attempt_row case_agent_task_attempts%ROWTYPE;
    approval_row case_agent_approvals%ROWTYPE;
    run_row case_agent_runs%ROWTYPE;
    worker_id uuid;
    ref_value text;
    ref_uuid uuid;
BEGIN
    worker_id := NULLIF(current_setting('app.actor_id', true), '')::uuid;
    IF worker_id IS NULL OR worker_id <> NEW.created_by_worker THEN
        RAISE EXCEPTION 'public research binding requires exact Worker transaction identity';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM users principal
        JOIN matter_actor_roles role
          ON role.user_id = principal.user_id AND role.firm_id = principal.firm_id
        WHERE principal.user_id = worker_id AND principal.firm_id = NEW.firm_id
          AND principal.status = 'ACTIVE' AND role.matter_id = NEW.matter_id
          AND role.role = 'SYSTEM_WORKER' AND role.revoked_at IS NULL
    ) OR EXISTS (
        SELECT 1 FROM matter_actor_roles role
        WHERE role.user_id = worker_id AND role.firm_id = NEW.firm_id
          AND role.matter_id = NEW.matter_id AND role.role <> 'SYSTEM_WORKER'
          AND role.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'public research binding requires a dedicated active SYSTEM_WORKER';
    END IF;
    SELECT * INTO run_row FROM case_agent_runs
      WHERE run_id = NEW.run_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    SELECT * INTO task_row FROM case_agent_tasks
      WHERE graph_id = NEW.graph_id AND task_id = NEW.task_id
        AND run_id = NEW.run_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    SELECT * INTO attempt_row FROM case_agent_task_attempts
      WHERE attempt_id = NEW.attempt_id AND run_id = NEW.run_id
        AND task_id = NEW.task_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    SELECT * INTO approval_row FROM case_agent_approvals
      WHERE approval_id = NEW.approval_id AND run_id = NEW.run_id
        AND task_id = NEW.task_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF run_row.run_id IS NULL OR task_row.task_id IS NULL
       OR attempt_row.attempt_id IS NULL OR approval_row.approval_id IS NULL
       OR run_row.current_graph_id IS DISTINCT FROM NEW.graph_id
       OR run_row.current_graph_hash IS DISTINCT FROM NEW.graph_hash
       OR task_row.input_hash <> NEW.task_input_hash
       OR task_row.input_refs <> NEW.input_refs
       OR task_row.skill_id <> 'controlled_web_search'
       OR task_row.tool_id <> 'search_public_web'
       OR task_row.execution_mode <> 'NETWORK_CONNECTOR'
       OR task_row.network_policy <> 'EXACT_ALLOWLIST'
       OR task_row.allowed_domains <> '["api.search.brave.com"]'::jsonb
       OR task_row.autonomy_level <> 'A3_LAWYER_APPROVAL'
       OR task_row.approval_gate <> 'LAWYER_REVIEW'
       OR task_row.retry_mode <> 'NEVER_AUTOMATIC'
       OR task_row.external_request_approval_required <> true
       OR COALESCE((task_row.resource_budget->>'max_external_calls')::integer, -1) <> 1
       OR COALESCE((task_row.resource_budget->>'max_attempts')::integer, -1) <> 1
       OR attempt_row.graph_id <> NEW.graph_id
       OR attempt_row.input_hash <> NEW.task_input_hash
       OR attempt_row.external_approval_id IS DISTINCT FROM NEW.approval_id
       OR attempt_row.status NOT IN ('RUNNING', 'RECONCILING')
       OR approval_row.approval_kind <> 'TASK'
       OR approval_row.graph_id <> NEW.graph_id
       OR approval_row.graph_hash <> NEW.graph_hash
       OR approval_row.task_input_hash <> NEW.task_input_hash
       OR approval_row.gate <> 'LAWYER_REVIEW'
       OR approval_row.approved_by <> NEW.approved_by
       OR NEW.created_at > now() + interval '5 seconds'
       OR NEW.expires_at <= now() THEN
        RAISE EXCEPTION 'public research binding differs from current approved Agent task';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM users lawyer
        JOIN matter_actor_roles role
          ON role.user_id = lawyer.user_id AND role.firm_id = lawyer.firm_id
        WHERE lawyer.user_id = NEW.approved_by AND lawyer.firm_id = NEW.firm_id
          AND lawyer.status = 'ACTIVE' AND role.matter_id = NEW.matter_id
          AND role.role IN ('LEAD_LAWYER', 'REVIEWER') AND role.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'public research task lacks current lawyer authority';
    END IF;
    IF NEW.confidential_question_hash <>
       encode(digest(convert_to(NEW.confidential_question, 'UTF8'), 'sha256'), 'hex')
       OR NEW.public_query_text <> (
           SELECT string_agg(value, ' ' ORDER BY ordinal)
           FROM jsonb_array_elements_text(NEW.public_terms) WITH ORDINALITY AS terms(value, ordinal)
       ) THEN
        RAISE EXCEPTION 'public research private/public query hashes or terms differ';
    END IF;
    FOR ref_value IN SELECT jsonb_array_elements_text(NEW.input_refs) LOOP
        IF ref_value ~ '^issue:[0-9a-f-]{36}$' THEN
            ref_uuid := substring(ref_value from 7)::uuid;
            IF NOT EXISTS (
                SELECT 1 FROM case_dispute_issues issue
                WHERE issue.issue_id = ref_uuid AND issue.firm_id = NEW.firm_id
                  AND issue.matter_id = NEW.matter_id AND issue.status = 'CONFIRMED'
            ) THEN RAISE EXCEPTION 'public research issue reference is not current and confirmed'; END IF;
        ELSIF ref_value ~ '^work-plan-item:[0-9a-f-]{36}$' THEN
            ref_uuid := substring(ref_value from 16)::uuid;
            IF NOT EXISTS (
                SELECT 1 FROM case_work_plan_items item
                JOIN case_work_plans plan ON plan.plan_id = item.plan_id
                  AND plan.firm_id = item.firm_id AND plan.matter_id = item.matter_id
                JOIN matters matter ON matter.matter_id = plan.matter_id
                  AND matter.firm_id = plan.firm_id
                WHERE item.item_id = ref_uuid AND item.firm_id = NEW.firm_id
                  AND item.matter_id = NEW.matter_id AND plan.status = 'ACTIVE'
                  AND plan.activated_matter_version = matter.version
                  AND (item.item_kind = 'RESEARCH_TASK' OR item.readiness = 'NEEDS_RESEARCH')
            ) THEN RAISE EXCEPTION 'public research work-plan reference is not current'; END IF;
        ELSIF ref_value ~ '^legal-source:[0-9a-f-]{36}$' THEN
            ref_uuid := substring(ref_value from 14)::uuid;
            IF NOT EXISTS (
                SELECT 1 FROM official_legal_source_snapshots source
                WHERE source.snapshot_id = ref_uuid AND source.firm_id = NEW.firm_id
                  AND source.verification_status = 'VERIFIED'
                  AND source.license_status = 'ACTIVE'
                  AND source.license_review_hash IS NOT NULL
            ) OR NOT EXISTS (
                SELECT 1 FROM case_agent_lawyer_decision_signals signal
                WHERE signal.run_id = NEW.run_id AND signal.firm_id = NEW.firm_id
                  AND signal.matter_id = NEW.matter_id AND signal.category = 'LEGAL_GAP'
                  AND signal.is_current AND signal.superseded_at IS NULL
                  AND signal.graph_hash = NEW.graph_hash
                  AND signal.source_ref_ids ? ref_value
            ) THEN RAISE EXCEPTION 'public research legal-source reference lacks a current legal gap'; END IF;
        ELSE
            RAISE EXCEPTION 'public research input reference kind is not governed';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_public_research_binding_guard
    BEFORE INSERT ON case_agent_public_research_bindings
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_public_research_binding();

CREATE FUNCTION validate_case_agent_public_research_egress_grant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bound case_agent_public_research_bindings%ROWTYPE;
BEGIN
    SELECT * INTO bound FROM case_agent_public_research_bindings
      WHERE binding_id = NEW.binding_id
        AND external_request_id = NEW.external_request_id
        AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF bound.binding_id IS NULL
       OR NEW.egress_grant_id <> bound.egress_grant_id
       OR NEW.issued_by_worker <> bound.created_by_worker
       OR NEW.expires_at <> bound.expires_at
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
          IS DISTINCT FROM NEW.issued_by_worker THEN
        RAISE EXCEPTION 'public research egress grant differs from its server binding';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_public_research_egress_grant_guard
    BEFORE INSERT ON case_agent_public_research_egress_grants
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_public_research_egress_grant();

CREATE FUNCTION validate_case_agent_public_research_exchange() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bound case_agent_public_research_bindings%ROWTYPE;
    submission case_agent_external_submissions%ROWTYPE;
BEGIN
    SELECT * INTO bound FROM case_agent_public_research_bindings
      WHERE binding_id = NEW.binding_id AND external_request_id = NEW.external_request_id
        AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    SELECT * INTO submission FROM case_agent_external_submissions
      WHERE submission_id = NEW.submission_record_id AND firm_id = NEW.firm_id
        AND matter_id = NEW.matter_id;
    IF bound.binding_id IS NULL OR submission.submission_id IS NULL
       OR bound.run_id <> NEW.run_id OR bound.task_id <> NEW.task_id
       OR bound.attempt_id <> NEW.attempt_id OR bound.egress_grant_id <> NEW.egress_grant_id
       OR submission.run_id <> NEW.run_id OR submission.task_id <> NEW.task_id
       OR submission.attempt_id <> NEW.attempt_id
       OR submission.external_request_id <> NEW.external_request_id::text
       OR submission.recorded_by <> NEW.started_by_worker
       OR submission.destination <> 'api.search.brave.com'
       OR submission.request_hash <> NEW.request_hash
       OR submission.submission_state <> 'STARTED'
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
          IS DISTINCT FROM NEW.started_by_worker
       OR NEW.started_by_worker <> bound.created_by_worker
       OR NEW.method <> 'GET' OR NEW.request_body_bytes <> 0
       OR NEW.request_url !~ '^https://api[.]search[.]brave[.]com/res/v1/web/search[?]'
       OR NEW.started_at >= bound.expires_at THEN
        RAISE EXCEPTION 'public research exchange differs from its exact submission boundary';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_public_research_exchange_guard
    BEFORE INSERT ON case_agent_public_research_exchanges
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_public_research_exchange();

CREATE FUNCTION validate_case_agent_public_research_outcome() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    exchange_row case_agent_public_research_exchanges%ROWTYPE;
    prior case_agent_public_research_outcomes%ROWTYPE;
BEGIN
    SELECT * INTO exchange_row FROM case_agent_public_research_exchanges
      WHERE exchange_id = NEW.exchange_id AND external_request_id = NEW.external_request_id
        AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF exchange_row.exchange_id IS NULL
       OR exchange_row.request_hash <> NEW.request_hash
       OR exchange_row.started_by_worker <> NEW.recorded_by_worker
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
          IS DISTINCT FROM NEW.recorded_by_worker THEN
        RAISE EXCEPTION 'public research outcome differs from its exchange';
    END IF;
    IF NEW.outcome_sequence = 2 THEN
        SELECT * INTO prior FROM case_agent_public_research_outcomes
          WHERE exchange_id = NEW.exchange_id AND outcome_sequence = 1;
        IF prior.outcome_id IS NULL OR prior.status <> 'UNKNOWN_SUBMISSION' THEN
            RAISE EXCEPTION 'public research recovery requires a prior unknown outcome';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_public_research_outcome_guard
    BEFORE INSERT ON case_agent_public_research_outcomes
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_public_research_outcome();

CREATE FUNCTION prohibit_case_agent_public_research_history_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent public research history is append-only';
END;
$$;

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_public_research_bindings',
        'case_agent_public_research_egress_grants',
        'case_agent_public_research_exchanges',
        'case_agent_public_research_outcomes'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (firm_id::text = current_setting(''app.firm_id'', true)) WITH CHECK (firm_id::text = current_setting(''app.firm_id'', true))',
            table_name || '_firm_isolation', table_name
        );
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_public_research_history_mutation()',
            table_name || '_append_only', table_name
        );
        EXECUTE format('REVOKE ALL ON TABLE %I FROM PUBLIC', table_name);
    END LOOP;
END;
$$;

COMMIT;
