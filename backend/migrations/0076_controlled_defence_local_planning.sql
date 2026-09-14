-- Immutable, server-governed planning evidence for the first-release
-- defendant-response route.  This is intentionally separate from the
-- provider-boundary ledger: no row in this table represents, permits or
-- disguises an outbound model request.

BEGIN;

CREATE TABLE public.case_agent_planning_local_events (
    planning_local_event_id uuid PRIMARY KEY,
    planning_attempt_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    planner_id text NOT NULL CHECK (
        planner_id = 'controlled-first-release-defence-planner-v1'
    ),
    planning_hash char(64) NOT NULL CHECK (planning_hash ~ '^[0-9a-f]{64}$'),
    proposal_hash char(64) NOT NULL CHECK (proposal_hash ~ '^[0-9a-f]{64}$'),
    structured_proposal jsonb NOT NULL CHECK (
        jsonb_typeof(structured_proposal) = 'object'
    ),
    recorded_by uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (planning_attempt_id),
    UNIQUE (run_id, planning_attempt_id, firm_id, matter_id),
    FOREIGN KEY (run_id, planning_attempt_id, firm_id, matter_id)
        REFERENCES public.case_agent_planning_attempts(
            run_id, planning_attempt_id, firm_id, matter_id
        ),
    FOREIGN KEY (recorded_by, firm_id)
        REFERENCES public.users(user_id, firm_id)
);

ALTER TABLE public.case_agent_planning_local_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_planning_local_events FORCE ROW LEVEL SECURITY;

CREATE POLICY case_agent_planning_local_events_firm_isolation
    ON public.case_agent_planning_local_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE TRIGGER case_agent_planning_local_events_append_only
    BEFORE UPDATE OR DELETE ON public.case_agent_planning_local_events
    FOR EACH ROW EXECUTE FUNCTION public.prohibit_case_agent_history_mutation();

REVOKE ALL ON TABLE public.case_agent_planning_local_events
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
GRANT SELECT, INSERT ON TABLE public.case_agent_planning_local_events
    TO lawcase_agent_worker;

COMMIT;
