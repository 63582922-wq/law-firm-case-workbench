-- ADR-0097: separate append-only metadata, never repair the Agent event stream.
BEGIN;

CREATE TABLE public.case_agent_visual_recovery_candidates (
    recovery_id uuid PRIMARY KEY,
    source_artifact_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    source_event_version bigint NOT NULL CHECK (source_event_version > 0),
    source_content_sha256 char(64) NOT NULL CHECK (source_content_sha256 ~ '^[0-9a-f]{64}$'),
    interpretation_policy text NOT NULL CHECK (interpretation_policy = 'visual-candidate-cost-separation-v1'),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 33554432),
    object_key text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 2000),
    object_version_id text,
    review_status text NOT NULL DEFAULT 'NEEDS_LAWYER_REVIEW' CHECK (review_status='NEEDS_LAWYER_REVIEW'),
    recovered_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_artifact_id, interpretation_policy),
    FOREIGN KEY (source_artifact_id, run_id, firm_id, matter_id)
        REFERENCES public.case_agent_review_candidates(artifact_id, run_id, firm_id, matter_id),
    FOREIGN KEY (recovered_by, firm_id) REFERENCES public.users(user_id, firm_id)
);

CREATE FUNCTION public.guard_case_agent_visual_recovery()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    source_row public.case_agent_review_candidates%ROWTYPE;
    run_row public.case_agent_runs%ROWTYPE;
    matter_version integer;
BEGIN
    IF current_user <> 'lawcase_agent_worker'
       OR NEW.recovered_by::text IS DISTINCT FROM current_setting('app.actor_id', true) THEN
        RAISE EXCEPTION 'visual recovery requires the bound worker identity';
    END IF;
    SELECT * INTO run_row FROM public.case_agent_runs
     WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id FOR UPDATE;
    SELECT version INTO matter_version FROM public.matters
     WHERE matter_id=NEW.matter_id AND firm_id=NEW.firm_id FOR SHARE;
    IF run_row.run_id IS NULL OR run_row.status <> 'FAILED'
       OR run_row.failure_code IS DISTINCT FROM 'ARTIFACT_VISUAL_CONTRACT_INVALID'
       OR run_row.is_stale OR run_row.is_cancelled
       OR run_row.current_event_version <> NEW.source_event_version
       OR run_row.snapshot_matter_version IS DISTINCT FROM matter_version THEN
        RAISE EXCEPTION 'visual recovery original run changed';
    END IF;
    PERFORM role.user_id FROM public.matter_actor_roles role
      JOIN public.users principal ON principal.user_id=role.user_id AND principal.firm_id=role.firm_id
     WHERE role.matter_id=NEW.matter_id AND role.firm_id=NEW.firm_id
       AND role.user_id=NEW.recovered_by AND role.role='SYSTEM_WORKER'
       AND role.revoked_at IS NULL AND principal.status='ACTIVE' FOR SHARE OF role;
    IF NOT FOUND THEN RAISE EXCEPTION 'visual recovery worker is inactive or revoked'; END IF;
    SELECT * INTO source_row FROM public.case_agent_review_candidates
     WHERE artifact_id=NEW.source_artifact_id AND run_id=NEW.run_id
       AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id;
    IF source_row.artifact_id IS NULL
       OR source_row.graph_id IS DISTINCT FROM run_row.current_graph_id
       OR source_row.content_sha256 <> NEW.source_content_sha256
       OR source_row.artifact_kind <> 'VISUAL_PAGE_REVIEW_CANDIDATE'
       OR source_row.review_status <> 'NEEDS_LAWYER_REVIEW' THEN
        RAISE EXCEPTION 'visual recovery source differs';
    END IF;
    IF (SELECT count(*) FROM public.case_agent_artifacts artifact
        JOIN public.case_agent_task_receipts receipt USING (receipt_id,run_id,firm_id,matter_id)
        JOIN public.case_agent_tasks task ON task.run_id=receipt.run_id
         AND task.task_id=receipt.task_id AND task.graph_id=source_row.graph_id
        JOIN public.case_agent_task_heads head ON head.run_id=task.run_id
         AND head.task_id=task.task_id AND head.graph_id=task.graph_id AND head.is_current
        WHERE artifact.artifact_id=NEW.source_artifact_id AND artifact.run_id=NEW.run_id
          AND artifact.firm_id=NEW.firm_id AND artifact.matter_id=NEW.matter_id
          AND artifact.content_hash=source_row.content_sha256 AND artifact.byte_size=source_row.byte_size
          AND artifact.source_input_hash=source_row.task_input_hash
          AND receipt.task_id=source_row.task_id AND receipt.input_hash=source_row.task_input_hash
          AND receipt.result_status='SUCCEEDED' AND receipt.external_submission_state='SUBMITTED'
          AND receipt.cost_minor_units=6 AND receipt.external_calls=1
          AND task.skill_id='image_visual_ocr' AND head.status='SUCCEEDED') <> 1 THEN
        RAISE EXCEPTION 'visual recovery requires the original successful OCR receipt';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_visual_recovery_guard BEFORE INSERT
    ON public.case_agent_visual_recovery_candidates FOR EACH ROW
    EXECUTE FUNCTION public.guard_case_agent_visual_recovery();
CREATE TRIGGER case_agent_visual_recovery_immutable BEFORE UPDATE OR DELETE
    ON public.case_agent_visual_recovery_candidates FOR EACH ROW
    EXECUTE FUNCTION public.prohibit_case_agent_sealed_response_recovery_mutation();

ALTER TABLE public.case_agent_visual_recovery_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_visual_recovery_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_visual_recovery_access ON public.case_agent_visual_recovery_candidates
 USING (firm_id::text=current_setting('app.firm_id',true) AND EXISTS (
    SELECT 1 FROM public.matter_actor_roles role
    JOIN public.users principal ON principal.user_id=role.user_id AND principal.firm_id=role.firm_id
    WHERE role.matter_id=case_agent_visual_recovery_candidates.matter_id
      AND role.firm_id=case_agent_visual_recovery_candidates.firm_id
      AND role.user_id::text=current_setting('app.actor_id',true)
      AND role.revoked_at IS NULL AND principal.status='ACTIVE'
      AND role.role IN ('LEAD_LAWYER','COLLABORATING_LAWYER','REVIEWER','ASSISTANT','SYSTEM_WORKER')
 ))
 WITH CHECK (firm_id::text=current_setting('app.firm_id',true)
             AND recovered_by::text=current_setting('app.actor_id',true));
REVOKE ALL ON public.case_agent_visual_recovery_candidates FROM PUBLIC,
    lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
GRANT SELECT, INSERT ON public.case_agent_visual_recovery_candidates TO lawcase_agent_worker;
GRANT SELECT ON public.case_agent_visual_recovery_candidates TO lawcase_web_application;
COMMIT;
