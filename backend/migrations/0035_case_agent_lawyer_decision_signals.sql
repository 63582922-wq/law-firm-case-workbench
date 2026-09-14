-- Governed lawyer corrections for dynamic Agent re-planning.
--
-- A rejected task is recorded as one immutable, source-bound planning signal
-- and one supervisor event.  The browser cannot supply category, status,
-- source refs, hashes or a replacement task graph.

BEGIN;

ALTER TABLE case_agent_events
    DROP CONSTRAINT case_agent_events_event_type_check;
ALTER TABLE case_agent_events
    ADD CONSTRAINT case_agent_events_event_type_check CHECK (event_type IN (
        'RUN_CREATED', 'PLANNING_STARTED', 'PLANNING_FAILED',
        'PLANNING_RESULT_UNKNOWN', 'TASK_GRAPH_ACCEPTED', 'APPROVAL_GRANTED',
        'LAWYER_PLAN_CORRECTION_RECORDED',
        'TASK_STARTED', 'TASK_RESULT_RECORDED', 'CASE_SNAPSHOT_CHANGED',
        'RUN_PAUSED', 'RUN_RESUMED', 'RUN_CANCELLED', 'VERIFICATION_STARTED',
        'VERIFICATION_PASSED', 'VERIFICATION_FAILED', 'RUN_COMPLETED'
    ));

CREATE TABLE case_agent_lawyer_decision_signals (
    signal_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    signal_version bigint NOT NULL CHECK (signal_version > 0),
    decision_code text NOT NULL CHECK (decision_code IN (
        'LAWYER_REJECT_WRONG_SCOPE',
        'LAWYER_REJECT_MISSING_MATERIAL',
        'LAWYER_REJECT_WRONG_FACT_ASSUMPTION',
        'LAWYER_REJECT_WRONG_LEGAL_DIRECTION',
        'LAWYER_REJECT_DUPLICATE_OR_UNNECESSARY',
        'LAWYER_REJECT_OTHER'
    )),
    category text NOT NULL CHECK (category IN (
        'PROCEEDING', 'PARTY_POSTURE', 'WORK_PLAN', 'CONFIRMED_FACT', 'LEGAL_GAP'
    )),
    signal_status text NOT NULL CHECK (signal_status IN (
        'CONFIRMED', 'DISPUTED', 'OPEN', 'BLOCKED'
    )),
    summary text NOT NULL CHECK (length(trim(summary)) BETWEEN 1 AND 1000),
    source_ref_ids jsonb NOT NULL CHECK (
        jsonb_typeof(source_ref_ids) = 'array'
        AND jsonb_array_length(source_ref_ids) BETWEEN 1 AND 100
    ),
    task_input_hash char(64) NOT NULL CHECK (task_input_hash ~ '^[0-9a-f]{64}$'),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    decision_hash char(64) NOT NULL CHECK (decision_hash ~ '^[0-9a-f]{64}$'),
    recorded_event_sequence bigint NOT NULL CHECK (recorded_event_sequence > 1),
    recorded_by uuid NOT NULL,
    decided_at timestamptz NOT NULL,
    supersedes_signal_id uuid,
    is_current boolean NOT NULL DEFAULT true,
    superseded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (signal_id, firm_id, matter_id),
    UNIQUE (run_id, signal_id, firm_id, matter_id),
    UNIQUE (firm_id, matter_id, subject_hash, signal_version),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, recorded_event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id),
    FOREIGN KEY (recorded_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (supersedes_signal_id, firm_id, matter_id)
        REFERENCES case_agent_lawyer_decision_signals(signal_id, firm_id, matter_id),
    CHECK (
        (is_current AND superseded_at IS NULL)
        OR (NOT is_current AND superseded_at IS NOT NULL)
    ),
    CHECK (supersedes_signal_id IS NULL OR signal_version > 1)
);

CREATE UNIQUE INDEX case_agent_lawyer_signal_current_subject_idx
    ON case_agent_lawyer_decision_signals(firm_id, matter_id, subject_hash)
    WHERE is_current;
CREATE INDEX case_agent_lawyer_signal_projection_idx
    ON case_agent_lawyer_decision_signals(
        firm_id, matter_id, is_current, decided_at, signal_id
    );

ALTER TABLE case_agent_lawyer_decision_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_lawyer_decision_signals FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_lawyer_decision_signals_firm_isolation
    ON case_agent_lawyer_decision_signals
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION guard_case_agent_lawyer_decision_signal()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.signal_id <> OLD.signal_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.graph_id <> OLD.graph_id
       OR NEW.task_id <> OLD.task_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.signal_version <> OLD.signal_version
       OR NEW.decision_code <> OLD.decision_code
       OR NEW.category <> OLD.category
       OR NEW.signal_status <> OLD.signal_status
       OR NEW.summary <> OLD.summary
       OR NEW.source_ref_ids <> OLD.source_ref_ids
       OR NEW.task_input_hash <> OLD.task_input_hash
       OR NEW.graph_hash <> OLD.graph_hash
       OR NEW.subject_hash <> OLD.subject_hash
       OR NEW.decision_hash <> OLD.decision_hash
       OR NEW.recorded_event_sequence <> OLD.recorded_event_sequence
       OR NEW.recorded_by <> OLD.recorded_by
       OR NEW.decided_at <> OLD.decided_at
       OR NEW.supersedes_signal_id IS DISTINCT FROM OLD.supersedes_signal_id
       OR NOT OLD.is_current
       OR NEW.is_current
       OR NEW.superseded_at IS NULL THEN
        RAISE EXCEPTION 'case Agent lawyer decision history transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_lawyer_decision_signals_guard
    BEFORE UPDATE OR DELETE ON case_agent_lawyer_decision_signals
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_lawyer_decision_signal();

COMMIT;
