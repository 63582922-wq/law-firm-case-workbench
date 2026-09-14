-- Recoverable, lawyer-authorized Web Agent material-review runs.
-- Model output is always a page-bound candidate and never a formal fact,
-- transaction, evidence decision or legal conclusion.

BEGIN;

CREATE TABLE web_agent_material_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    requested_by uuid NOT NULL REFERENCES users(user_id),
    matter_version integer NOT NULL CHECK (matter_version > 0),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    plan_request_hash char(64) NOT NULL CHECK (plan_request_hash ~ '^[0-9a-f]{64}$'),
    agent_intent text NOT NULL CHECK (agent_intent IN (
        'MATERIAL_NEUTRAL_REVIEW',
        'MATERIAL_TO_PLAINTIFF_LEDGER',
        'MATERIAL_TO_DEFENSE_LEDGER'
    )),
    representation_profile_version bigint CHECK (representation_profile_version > 0),
    representation_profile_hash char(64) CHECK (
        representation_profile_hash IS NULL OR representation_profile_hash ~ '^[0-9a-f]{64}$'
    ),
    external_request_id uuid,
    run_version integer NOT NULL DEFAULT 1 CHECK (run_version > 0),
    status text NOT NULL CHECK (status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'NEEDS_REVIEW', 'FAILED')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0 AND attempt_count <= 3),
    lease_id uuid,
    lease_expires_at timestamptz,
    provider_request_hash char(64) CHECK (
        provider_request_hash IS NULL OR provider_request_hash ~ '^[0-9a-f]{64}$'
    ),
    output_hash char(64) CHECK (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$'),
    failure_code text CHECK (failure_code IS NULL OR failure_code IN (
        'MODEL_RESPONSE_INVALID', 'PROVIDER_REJECTED', 'PROVIDER_RESULT_UNKNOWN', 'INTERNAL_FAILURE'
    )),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (run_id, firm_id, matter_id),
    UNIQUE (external_request_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (requested_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (external_request_id, firm_id, matter_id)
        REFERENCES external_request_authorizations(request_id, firm_id, matter_id),
    CHECK ((representation_profile_version IS NULL) = (representation_profile_hash IS NULL)),
    -- Party-position plans remain reserved until a formal representation
    -- profile ledger and FK are introduced.  0028 permits only neutral runs.
    CHECK (agent_intent = 'MATERIAL_NEUTRAL_REVIEW'),
    CHECK (
        (status = 'QUEUED' AND attempt_count = 0 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND provider_request_hash IS NULL
            AND output_hash IS NULL AND failure_code IS NULL AND completed_at IS NULL)
        OR
        (status = 'CLAIMED' AND attempt_count > 0 AND external_request_id IS NOT NULL
            AND representation_profile_version IS NOT NULL AND lease_id IS NOT NULL
            AND lease_expires_at IS NOT NULL AND provider_request_hash IS NULL
            AND output_hash IS NULL AND failure_code IS NULL AND completed_at IS NULL)
        OR
        (status = 'RUNNING' AND attempt_count > 0 AND external_request_id IS NOT NULL
            AND representation_profile_version IS NOT NULL AND lease_id IS NULL
            AND lease_expires_at IS NULL AND provider_request_hash IS NOT NULL
            AND output_hash IS NULL AND failure_code IS NULL AND completed_at IS NULL)
        OR
        (status = 'NEEDS_REVIEW' AND attempt_count > 0 AND external_request_id IS NOT NULL
            AND representation_profile_version IS NOT NULL AND lease_id IS NULL
            AND lease_expires_at IS NULL AND provider_request_hash IS NOT NULL
            AND output_hash IS NOT NULL AND failure_code IS NULL AND completed_at IS NOT NULL)
        OR
        (status = 'FAILED' AND attempt_count > 0 AND external_request_id IS NOT NULL
            AND representation_profile_version IS NOT NULL AND lease_id IS NULL
            AND lease_expires_at IS NULL AND output_hash IS NULL
            AND failure_code IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE TABLE web_agent_material_tasks (
    task_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 4),
    task_kind text NOT NULL CHECK (task_kind IN (
        'MATERIAL_READING', 'PAGE_CLASSIFICATION',
        'RELEVANT_PAGE_EXTRACTION', 'EXCEPTION_ROUTING'
    )),
    status text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'NEEDS_REVIEW', 'FAILED')),
    UNIQUE (run_id, sequence),
    UNIQUE (task_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES web_agent_material_runs(run_id, firm_id, matter_id)
);

CREATE FUNCTION restrict_web_agent_material_run_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR (to_jsonb(NEW) - ARRAY[
            'matter_version','external_request_id','run_version','status','attempt_count',
            'lease_id','lease_expires_at','provider_request_hash','output_hash',
            'failure_code','updated_at','completed_at'
          ]) IS DISTINCT FROM
          (to_jsonb(OLD) - ARRAY[
            'matter_version','external_request_id','run_version','status','attempt_count',
            'lease_id','lease_expires_at','provider_request_hash','output_hash',
            'failure_code','updated_at','completed_at'
          ])
       OR NEW.run_version <> OLD.run_version + 1
       OR NOT (
            (OLD.status = 'QUEUED' AND NEW.status IN ('QUEUED','CLAIMED'))
            OR (OLD.status = 'CLAIMED' AND NEW.status IN ('CLAIMED','RUNNING','FAILED'))
            OR (OLD.status = 'RUNNING' AND NEW.status IN ('NEEDS_REVIEW','FAILED'))
          ) THEN
        RAISE EXCEPTION 'Web Agent material run transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER web_agent_material_runs_guard BEFORE UPDATE OR DELETE
    ON web_agent_material_runs FOR EACH ROW EXECUTE FUNCTION restrict_web_agent_material_run_mutation();

CREATE FUNCTION restrict_web_agent_material_task_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.task_id <> OLD.task_id OR NEW.run_id <> OLD.run_id
       OR NEW.firm_id <> OLD.firm_id OR NEW.matter_id <> OLD.matter_id
       OR NEW.sequence <> OLD.sequence OR NEW.task_kind <> OLD.task_kind
       OR NOT ((OLD.status = 'QUEUED' AND NEW.status IN ('RUNNING', 'FAILED'))
            OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'NEEDS_REVIEW', 'FAILED'))) THEN
        RAISE EXCEPTION 'Web Agent material task transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER web_agent_material_tasks_guard BEFORE UPDATE OR DELETE
    ON web_agent_material_tasks FOR EACH ROW EXECUTE FUNCTION restrict_web_agent_material_task_mutation();

CREATE TABLE web_agent_material_page_bindings (
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 64),
    evidence_page_id uuid NOT NULL,
    source_file_sha256 char(64) NOT NULL CHECK (source_file_sha256 ~ '^[0-9a-f]{64}$'),
    page_number integer NOT NULL CHECK (page_number > 0),
    extracted_text_sha256 char(64) NOT NULL CHECK (extracted_text_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (run_id, evidence_page_id),
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES web_agent_material_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id)
);

CREATE TABLE web_agent_material_candidates (
    candidate_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    evidence_page_id uuid NOT NULL,
    source_file_sha256 char(64) NOT NULL CHECK (source_file_sha256 ~ '^[0-9a-f]{64}$'),
    page_number integer NOT NULL CHECK (page_number > 0),
    candidate_kind text NOT NULL CHECK (candidate_kind IN (
        'RELEVANT_PAGE', 'UNRELATED_PAGE', 'OCR_REQUIRED', 'DUPLICATE_CANDIDATE', 'UNCERTAIN'
    )),
    confidence numeric(6,5) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    review_priority text NOT NULL CHECK (review_priority IN ('LOW', 'MEDIUM', 'HIGH')),
    reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes) = 'array'),
    supporting_excerpt text NOT NULL CHECK (length(supporting_excerpt) <= 500),
    duplicate_of_page_id uuid,
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status = 'NEEDS_REVIEW'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, evidence_page_id),
    UNIQUE (candidate_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES web_agent_material_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    FOREIGN KEY (duplicate_of_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    CHECK ((candidate_kind = 'DUPLICATE_CANDIDATE') = (duplicate_of_page_id IS NOT NULL))
);

CREATE TABLE web_agent_material_commands (
    command_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    actor_id uuid NOT NULL REFERENCES users(user_id),
    command_name text NOT NULL CHECK (length(trim(command_name)) > 0),
    idempotency_key text NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    run_id uuid NOT NULL,
    response_run_version integer NOT NULL CHECK (response_run_version > 0),
    completed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, matter_id, actor_id, command_name, idempotency_key),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES web_agent_material_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE web_agent_material_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    run_id uuid NOT NULL,
    actor_id uuid REFERENCES users(user_id),
    event_type text NOT NULL CHECK (length(trim(event_type)) > 0),
    from_run_version integer NOT NULL CHECK (from_run_version >= 0),
    to_run_version integer NOT NULL CHECK (to_run_version > from_run_version),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES web_agent_material_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE INDEX web_agent_material_runs_matter_status_idx
    ON web_agent_material_runs (matter_id, status, created_at DESC);
CREATE INDEX web_agent_material_candidates_run_priority_idx
    ON web_agent_material_candidates (run_id, review_priority, page_number);
CREATE INDEX web_agent_material_events_run_idx
    ON web_agent_material_events (run_id, to_run_version);

ALTER TABLE web_agent_material_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_page_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_page_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_candidates FORCE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_commands FORCE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_agent_material_events FORCE ROW LEVEL SECURITY;

CREATE POLICY web_agent_material_runs_firm_isolation ON web_agent_material_runs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY web_agent_material_tasks_firm_isolation ON web_agent_material_tasks
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY web_agent_material_page_bindings_firm_isolation ON web_agent_material_page_bindings
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY web_agent_material_candidates_firm_isolation ON web_agent_material_candidates
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY web_agent_material_commands_firm_isolation ON web_agent_material_commands
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY web_agent_material_events_firm_isolation ON web_agent_material_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION prohibit_web_agent_material_append_only_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Web Agent material bindings, candidates, commands and events are append-only';
END;
$$;
CREATE TRIGGER web_agent_material_page_bindings_append_only BEFORE UPDATE OR DELETE
    ON web_agent_material_page_bindings FOR EACH ROW EXECUTE FUNCTION prohibit_web_agent_material_append_only_mutation();
CREATE TRIGGER web_agent_material_candidates_append_only BEFORE UPDATE OR DELETE
    ON web_agent_material_candidates FOR EACH ROW EXECUTE FUNCTION prohibit_web_agent_material_append_only_mutation();
CREATE TRIGGER web_agent_material_commands_append_only BEFORE UPDATE OR DELETE
    ON web_agent_material_commands FOR EACH ROW EXECUTE FUNCTION prohibit_web_agent_material_append_only_mutation();
CREATE TRIGGER web_agent_material_events_append_only BEFORE UPDATE OR DELETE
    ON web_agent_material_events FOR EACH ROW EXECUTE FUNCTION prohibit_web_agent_material_append_only_mutation();

COMMIT;
