-- A sealed-response recovery candidate is a narrowly governed review surface
-- for one authenticated historical provider response.  It is not an Agent
-- artifact, task result, verification receipt, document package, or court
-- submission.  In particular, it must never repair a failed task by mutating
-- the Agent event stream.

BEGIN;

CREATE TABLE public.case_agent_sealed_response_recovery_candidates (
    recovery_id uuid PRIMARY KEY,
    artifact_id uuid NOT NULL,
    idempotency_key char(64) NOT NULL
        CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    task_input_hash char(64) NOT NULL
        CHECK (task_input_hash ~ '^[0-9a-f]{64}$'),
    source_hash char(64) NOT NULL
        CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    candidate_content_sha256 char(64) NOT NULL
        CHECK (candidate_content_sha256 ~ '^[0-9a-f]{64}$'),
    source_run_event_version bigint NOT NULL
        CHECK (source_run_event_version > 0),
    source_snapshot_hash char(64) NOT NULL
        CHECK (source_snapshot_hash ~ '^[0-9a-f]{64}$'),
    recovery_kind text NOT NULL CHECK (
        recovery_kind = 'SEALED_RESPONSE_REPARSE'
    ),
    recovery_policy_hash char(64) NOT NULL
        CHECK (recovery_policy_hash ~ '^[0-9a-f]{64}$'),
    external_request_id text NOT NULL CHECK (
        length(trim(external_request_id)) BETWEEN 1 AND 500
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_sha256 char(64) NOT NULL CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    archive_sha256 char(64) NOT NULL CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    failure_code text NOT NULL CHECK (
        failure_code = 'LAWYER_ANALYSIS_OUTPUT_REJECTED'
    ),
    recovered_by uuid NOT NULL,
    recovered_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, idempotency_key),
    UNIQUE (artifact_id, run_id, firm_id, matter_id),
    FOREIGN KEY (artifact_id, run_id, firm_id, matter_id)
        REFERENCES public.case_agent_review_candidates(
            artifact_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES public.case_agent_tasks(
            graph_id, task_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES public.case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (recovered_by, firm_id)
        REFERENCES public.users(user_id, firm_id)
);

CREATE INDEX case_agent_sealed_response_recovery_candidates_run_idx
    ON public.case_agent_sealed_response_recovery_candidates (
        run_id, source_run_event_version, artifact_id
    );

CREATE FUNCTION public.validate_case_agent_sealed_response_recovery_candidate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    candidate_row public.case_agent_review_candidates%ROWTYPE;
    run_row public.case_agent_runs%ROWTYPE;
    task_row public.case_agent_tasks%ROWTYPE;
    head_status text;
    submission_count bigint;
    receipt_count bigint;
BEGIN
    SELECT * INTO candidate_row
      FROM public.case_agent_review_candidates candidate
     WHERE candidate.artifact_id = NEW.artifact_id
       AND candidate.run_id = NEW.run_id
       AND candidate.firm_id = NEW.firm_id
       AND candidate.matter_id = NEW.matter_id;
    IF candidate_row.artifact_id IS NULL
       OR candidate_row.idempotency_key <> NEW.idempotency_key
       OR candidate_row.graph_id <> NEW.graph_id
       OR candidate_row.task_id <> NEW.task_id
       OR candidate_row.task_input_hash <> NEW.task_input_hash
       OR candidate_row.source_hash <> NEW.source_hash
       OR candidate_row.content_sha256 <> NEW.candidate_content_sha256
       OR candidate_row.artifact_kind <> 'LAWYER_DECISION_PACKAGE_CANDIDATE'
       OR candidate_row.media_type <> 'application/json'
       OR candidate_row.review_status <> 'NEEDS_LAWYER_REVIEW' THEN
        RAISE EXCEPTION 'sealed-response recovery candidate differs from its review-only artifact';
    END IF;

    SELECT * INTO run_row
      FROM public.case_agent_runs run
     WHERE run.run_id = NEW.run_id
       AND run.firm_id = NEW.firm_id
       AND run.matter_id = NEW.matter_id;
    IF run_row.run_id IS NULL
       OR run_row.status <> 'WAITING_INPUT'
       OR run_row.is_stale OR run_row.is_cancelled
       OR run_row.current_graph_id <> NEW.graph_id
       OR run_row.current_event_version <> NEW.source_run_event_version
       OR run_row.snapshot_hash <> NEW.source_snapshot_hash THEN
        RAISE EXCEPTION 'sealed-response recovery requires the unchanged blocked run';
    END IF;

    SELECT * INTO task_row
      FROM public.case_agent_tasks task
     WHERE task.graph_id = NEW.graph_id
       AND task.task_id = NEW.task_id
       AND task.run_id = NEW.run_id
       AND task.firm_id = NEW.firm_id
       AND task.matter_id = NEW.matter_id;
    SELECT head.status INTO head_status
      FROM public.case_agent_task_heads head
     WHERE head.graph_id = NEW.graph_id
       AND head.task_id = NEW.task_id
       AND head.run_id = NEW.run_id
       AND head.firm_id = NEW.firm_id
       AND head.matter_id = NEW.matter_id
       AND head.is_current;
    IF task_row.task_id IS NULL
       OR task_row.input_hash <> NEW.task_input_hash
       OR task_row.tool_id <> 'analyze_lawyer_decision_package'
       OR head_status <> 'FAILED' THEN
        RAISE EXCEPTION 'sealed-response recovery task is not the failed lawyer analysis task';
    END IF;

    SELECT COUNT(*) INTO submission_count
      FROM public.case_agent_external_submissions submission
     WHERE submission.run_id = NEW.run_id
       AND submission.firm_id = NEW.firm_id
       AND submission.matter_id = NEW.matter_id;
    IF submission_count <> 1 THEN
        RAISE EXCEPTION 'sealed-response recovery requires exactly one historical external submission';
    END IF;

    SELECT COUNT(*) INTO receipt_count
      FROM public.case_agent_external_submissions submission
      JOIN public.case_agent_task_receipts receipt
        ON receipt.attempt_id = submission.attempt_id
       AND receipt.run_id = submission.run_id
       AND receipt.task_id = submission.task_id
       AND receipt.firm_id = submission.firm_id
       AND receipt.matter_id = submission.matter_id
     WHERE submission.run_id = NEW.run_id
       AND submission.task_id = NEW.task_id
       AND submission.firm_id = NEW.firm_id
       AND submission.matter_id = NEW.matter_id
       AND submission.external_request_id = NEW.external_request_id
       AND submission.request_hash = NEW.request_hash
       AND submission.recorded_by = NEW.recovered_by
       AND receipt.input_hash = NEW.task_input_hash
       AND receipt.result_status = 'FAILED'
       AND receipt.external_submission_state = 'SUBMITTED'
       AND receipt.error_code = NEW.failure_code
       AND receipt.external_request_id = NEW.external_request_id
       AND receipt.external_calls = 1;
    IF receipt_count <> 1 THEN
        RAISE EXCEPTION 'sealed-response recovery does not match the recorded rejected provider result';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.matter_actor_roles role
          JOIN public.users principal
            ON principal.user_id = role.user_id
           AND principal.firm_id = role.firm_id
         WHERE role.matter_id = NEW.matter_id
           AND role.firm_id = NEW.firm_id
           AND role.user_id = NEW.recovered_by
           AND role.role = 'SYSTEM_WORKER'
           AND role.revoked_at IS NULL
           AND principal.status = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION 'sealed-response recovery requires the active recorded SYSTEM_WORKER';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_sealed_response_recovery_candidates_validate
    BEFORE INSERT
    ON public.case_agent_sealed_response_recovery_candidates
    FOR EACH ROW EXECUTE FUNCTION public.validate_case_agent_sealed_response_recovery_candidate();

ALTER TABLE public.case_agent_sealed_response_recovery_candidates
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_sealed_response_recovery_candidates
    FORCE ROW LEVEL SECURITY;

CREATE POLICY case_agent_sealed_response_recovery_candidates_firm_isolation
    ON public.case_agent_sealed_response_recovery_candidates
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION public.prohibit_case_agent_sealed_response_recovery_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'sealed-response recovery candidates are append-only';
END;
$$;

CREATE TRIGGER case_agent_sealed_response_recovery_candidates_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_sealed_response_recovery_candidates
    FOR EACH ROW EXECUTE FUNCTION public.prohibit_case_agent_sealed_response_recovery_mutation();

REVOKE ALL ON TABLE public.case_agent_sealed_response_recovery_candidates
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
GRANT SELECT, INSERT ON TABLE public.case_agent_sealed_response_recovery_candidates
    TO lawcase_agent_worker;
GRANT SELECT ON TABLE public.case_agent_sealed_response_recovery_candidates
    TO lawcase_web_application;

COMMIT;
