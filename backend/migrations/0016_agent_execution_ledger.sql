-- Append-only, case-scoped Agent planning and Tool execution audit ledger.
-- Plans do not authorize a Tool by themselves; the existing Skill Registry,
-- case scopes and lawyer approval gate remain mandatory at execution time.

BEGIN;

CREATE TABLE agent_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    requested_by uuid NOT NULL REFERENCES users(user_id),
    agent_id text NOT NULL CHECK (length(trim(agent_id)) > 0),
    agent_version text NOT NULL CHECK (length(trim(agent_version)) > 0),
    policy_manifest_hash char(64) NOT NULL CHECK (policy_manifest_hash ~ '^[0-9a-f]{64}$'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    input_matter_version integer NOT NULL CHECK (input_matter_version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (requested_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE agent_action_proposals (
    proposal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    run_id uuid NOT NULL REFERENCES agent_runs(run_id),
    sequence integer NOT NULL CHECK (sequence > 0 AND sequence <= 100),
    skill_id text NOT NULL CHECK (length(trim(skill_id)) > 0),
    skill_version text NOT NULL CHECK (length(trim(skill_version)) > 0),
    tool_id text NOT NULL CHECK (length(trim(tool_id)) > 0),
    approval_gate text NOT NULL CHECK (approval_gate IN ('NONE', 'MATERIAL_SCOPE', 'LAWYER_REVIEW', 'RELEASE_LOCK')),
    required_scopes jsonb NOT NULL CHECK (jsonb_typeof(required_scopes) = 'array'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    rationale_hash char(64) NOT NULL CHECK (rationale_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, sequence),
    UNIQUE (proposal_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id) REFERENCES agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)
);

CREATE TABLE agent_tool_execution_receipts (
    receipt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    proposal_id uuid NOT NULL REFERENCES agent_action_proposals(proposal_id),
    executor_id uuid NOT NULL REFERENCES users(user_id),
    status text NOT NULL CHECK (status IN ('SUCCEEDED', 'BLOCKED', 'FAILED', 'STALE_RESULT')),
    output_hash char(64) CHECK (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$'),
    error_code text CHECK (error_code IS NULL OR length(trim(error_code)) > 0),
    executed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (receipt_id, firm_id, matter_id),
    CHECK ((status = 'SUCCEEDED' AND output_hash IS NOT NULL AND error_code IS NULL)
       OR (status <> 'SUCCEEDED' AND output_hash IS NULL AND error_code IS NOT NULL)),
    FOREIGN KEY (proposal_id, firm_id, matter_id) REFERENCES agent_action_proposals(proposal_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (executor_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE INDEX agent_runs_matter_created_idx ON agent_runs (matter_id, created_at DESC);
CREATE INDEX agent_action_proposals_run_sequence_idx ON agent_action_proposals (run_id, sequence);
CREATE INDEX agent_tool_execution_receipts_proposal_idx ON agent_tool_execution_receipts (proposal_id, executed_at DESC);

ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_action_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_action_proposals FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_tool_execution_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tool_execution_receipts FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_runs_firm_isolation ON agent_runs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY agent_action_proposals_firm_isolation ON agent_action_proposals
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY agent_tool_execution_receipts_firm_isolation ON agent_tool_execution_receipts
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION prohibit_agent_execution_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'agent execution ledger is append-only';
END;
$$;

CREATE TRIGGER agent_runs_append_only BEFORE UPDATE OR DELETE ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION prohibit_agent_execution_ledger_mutation();
CREATE TRIGGER agent_action_proposals_append_only BEFORE UPDATE OR DELETE ON agent_action_proposals
    FOR EACH ROW EXECUTE FUNCTION prohibit_agent_execution_ledger_mutation();
CREATE TRIGGER agent_tool_execution_receipts_append_only BEFORE UPDATE OR DELETE ON agent_tool_execution_receipts
    FOR EACH ROW EXECUTE FUNCTION prohibit_agent_execution_ledger_mutation();

COMMIT;
