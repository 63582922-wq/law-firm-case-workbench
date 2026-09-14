-- Independent, append-only verification receipts for unified Agent runs.
--
-- VERIFICATION_STARTED remains an event-stream transition.  The terminal row
-- below and VERIFICATION_PASSED/FAILED are committed by one store transaction,
-- so a receipt can never claim a terminal outcome that the aggregate did not
-- accept.  Artifact lineage contains hashes and verifier identities only;
-- object keys, paths and extracted legal text are excluded.

BEGIN;

ALTER TABLE case_agent_worker_heartbeats
    ADD COLUMN verifier_actor_id uuid,
    ADD COLUMN verifier_id text,
    ADD COLUMN verifier_version text,
    ADD COLUMN verifier_policy_hash char(64);

ALTER TABLE case_agent_worker_heartbeats
    ADD CONSTRAINT case_agent_worker_heartbeat_verifier_actor_fk
        FOREIGN KEY (verifier_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    ADD CONSTRAINT case_agent_worker_heartbeat_verifier_binding_check CHECK (
        verifier_actor_id IS NOT NULL
        AND verifier_actor_id <> actor_id
        AND verifier_id ~ '^[A-Za-z][A-Za-z0-9._:-]{0,199}$'
        AND verifier_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'
        AND verifier_policy_hash ~ '^[0-9a-f]{64}$'
    ) NOT VALID;

CREATE TABLE case_agent_verification_attempts (
    verification_attempt_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    execution_actor_id uuid NOT NULL,
    verifier_actor_id uuid NOT NULL,
    verifier_id text NOT NULL CHECK (
        verifier_id ~ '^[A-Za-z][A-Za-z0-9._:-]{0,199}$'
    ),
    verifier_version text NOT NULL CHECK (
        verifier_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'
    ),
    policy_hash char(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    snapshot_hash char(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    started_event_sequence bigint NOT NULL CHECK (started_event_sequence > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, graph_hash),
    UNIQUE (verification_attempt_id, run_id, firm_id, matter_id),
    UNIQUE (
        verification_attempt_id, verifier_actor_id, execution_actor_id,
        firm_id, matter_id
    ),
    UNIQUE (run_id, started_event_sequence, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_task_graphs(graph_id, run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, started_event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id),
    FOREIGN KEY (execution_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (verifier_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (verifier_actor_id <> execution_actor_id)
);

CREATE TABLE case_agent_verification_receipts (
    verification_receipt_id uuid PRIMARY KEY,
    verification_attempt_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('PASSED', 'FAILED')),
    verifier_id text NOT NULL CHECK (
        verifier_id ~ '^[A-Za-z][A-Za-z0-9._:-]{0,199}$'
    ),
    verifier_version text NOT NULL CHECK (
        verifier_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'
    ),
    policy_hash char(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    snapshot_hash char(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    task_receipts_hash char(64) NOT NULL CHECK (task_receipts_hash ~ '^[0-9a-f]{64}$'),
    artifact_manifest_hash char(64) NOT NULL CHECK (artifact_manifest_hash ~ '^[0-9a-f]{64}$'),
    artifact_lineage jsonb NOT NULL CHECK (jsonb_typeof(artifact_lineage) = 'array'),
    error_code text CHECK (
        error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{2,79}$'
    ),
    verification_hash char(64) NOT NULL CHECK (verification_hash ~ '^[0-9a-f]{64}$'),
    verifier_actor_id uuid NOT NULL,
    execution_actor_id uuid NOT NULL,
    verified_at timestamptz NOT NULL,
    terminal_event_sequence bigint NOT NULL CHECK (terminal_event_sequence > 0),
    persisted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (verification_attempt_id),
    UNIQUE (run_id, verification_hash),
    UNIQUE (run_id, terminal_event_sequence, firm_id, matter_id),
    FOREIGN KEY (verification_attempt_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_verification_attempts(
            verification_attempt_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (
        verification_attempt_id, verifier_actor_id, execution_actor_id,
        firm_id, matter_id
    ) REFERENCES case_agent_verification_attempts(
        verification_attempt_id, verifier_actor_id, execution_actor_id,
        firm_id, matter_id
    ),
    FOREIGN KEY (run_id, terminal_event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id),
    FOREIGN KEY (verifier_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (execution_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (verifier_actor_id <> execution_actor_id),
    CHECK (
        (outcome = 'PASSED' AND error_code IS NULL)
        OR (outcome = 'FAILED' AND error_code IS NOT NULL AND artifact_lineage = '[]'::jsonb)
    )
);

CREATE INDEX case_agent_verification_receipts_run_idx
    ON case_agent_verification_receipts (run_id, persisted_at);

CREATE FUNCTION require_independent_case_agent_verifier_principals()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actor_id uuid;
BEGIN
    IF NEW.verifier_actor_id = NEW.execution_actor_id THEN
        RAISE EXCEPTION 'case Agent verifier must be independent from execution';
    END IF;
    FOREACH actor_id IN ARRAY ARRAY[
        NEW.verifier_actor_id, NEW.execution_actor_id
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM matter_actor_roles role
            JOIN users principal
              ON principal.user_id = role.user_id
             AND principal.firm_id = role.firm_id
            WHERE role.firm_id = NEW.firm_id
              AND role.matter_id = NEW.matter_id
              AND role.user_id = actor_id
              AND role.role = 'SYSTEM_WORKER'
              AND role.revoked_at IS NULL
              AND principal.status = 'ACTIVE'
        ) OR EXISTS (
            SELECT 1
            FROM matter_actor_roles role
            WHERE role.firm_id = NEW.firm_id
              AND role.matter_id = NEW.matter_id
              AND role.user_id = actor_id
              AND role.role <> 'SYSTEM_WORKER'
              AND role.revoked_at IS NULL
        ) THEN
            RAISE EXCEPTION 'case Agent verification principal is not a dedicated active SYSTEM_WORKER';
        END IF;
    END LOOP;
    IF NEW.verifier_actor_id IS DISTINCT FROM
       NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
        RAISE EXCEPTION 'case Agent verifier principal differs from transaction identity';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_verification_attempt_principals_guard
    BEFORE INSERT ON case_agent_verification_attempts
    FOR EACH ROW EXECUTE FUNCTION require_independent_case_agent_verifier_principals();
CREATE TRIGGER case_agent_verification_receipt_principals_guard
    BEFORE INSERT ON case_agent_verification_receipts
    FOR EACH ROW EXECUTE FUNCTION require_independent_case_agent_verifier_principals();

ALTER TABLE case_agent_verification_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_verification_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_verification_attempts_firm_isolation
    ON case_agent_verification_attempts
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

ALTER TABLE case_agent_verification_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_verification_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_verification_receipts_firm_isolation
    ON case_agent_verification_receipts
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION prohibit_case_agent_verification_history_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent verification history is append-only';
END;
$$;

CREATE TRIGGER case_agent_verification_attempts_append_only
    BEFORE UPDATE OR DELETE ON case_agent_verification_attempts
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_verification_history_mutation();
CREATE TRIGGER case_agent_verification_receipts_append_only
    BEFORE UPDATE OR DELETE ON case_agent_verification_receipts
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_verification_history_mutation();

REVOKE ALL ON TABLE case_agent_verification_attempts FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_verification_receipts FROM PUBLIC;

COMMIT;
