-- Encrypted, lawyer-reviewable structured Agent draft candidates.
-- Candidate prose remains in the managed encrypted object store. PostgreSQL
-- retains only content addressing, exact review inputs, approval and the
-- subsequently created append-only Agent proposal relationship.

BEGIN;

CREATE TABLE agent_draft_candidates (
    candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    document_kind text NOT NULL CHECK (document_kind IN ('DOCX', 'XLSX')),
    agent_id text NOT NULL CHECK (length(trim(agent_id)) > 0),
    agent_version text NOT NULL CHECK (length(trim(agent_version)) > 0),
    skill_id text NOT NULL CHECK (skill_id IN ('document_drafting', 'spreadsheet_ledger')),
    tool_id text NOT NULL CHECK (tool_id IN ('create_reviewable_docx_draft', 'create_reviewable_xlsx_ledger')),
    content_object_key text NOT NULL CHECK (content_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\\.lca$'),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    content_bytes bigint NOT NULL CHECK (content_bytes > 0 AND content_bytes <= 2097152),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    review_hash char(64) NOT NULL CHECK (review_hash ~ '^[0-9a-f]{64}$'),
    rationale_hash char(64) NOT NULL CHECK (rationale_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'REJECTED')),
    registered_by uuid NOT NULL REFERENCES users(user_id),
    approved_by uuid REFERENCES users(user_id),
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz,
    run_id uuid REFERENCES agent_runs(run_id),
    proposal_id uuid REFERENCES agent_action_proposals(proposal_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, firm_id, matter_id),
    UNIQUE (matter_id, content_sha256),
    UNIQUE (proposal_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (registered_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (content_object_key = substring(content_sha256 from 1 for 2) || '/' || substring(content_sha256 from 3 for 2) || '/' || content_sha256 || '.lca'),
    CHECK ((document_kind = 'DOCX' AND skill_id = 'document_drafting' AND tool_id = 'create_reviewable_docx_draft') OR (document_kind = 'XLSX' AND skill_id = 'spreadsheet_ledger' AND tool_id = 'create_reviewable_xlsx_ledger')),
    CHECK ((status = 'CANDIDATE' AND approved_by IS NULL AND approval_hash IS NULL AND approved_at IS NULL AND run_id IS NULL AND proposal_id IS NULL) OR (status = 'REJECTED' AND approved_by IS NULL AND approval_hash IS NULL AND approved_at IS NULL AND run_id IS NULL AND proposal_id IS NULL) OR (status = 'APPROVED' AND approved_by IS NOT NULL AND approval_hash = review_hash AND approved_at IS NOT NULL AND run_id IS NOT NULL AND proposal_id IS NOT NULL))
);

CREATE INDEX agent_draft_candidates_matter_status_idx ON agent_draft_candidates (matter_id, status, created_at DESC);

ALTER TABLE agent_draft_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_draft_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_draft_candidates_firm_isolation ON agent_draft_candidates USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION restrict_agent_draft_candidate_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Agent draft candidates cannot be deleted';
    END IF;
    IF OLD.status <> 'CANDIDATE' OR NEW.status NOT IN ('APPROVED', 'REJECTED')
       OR (NEW.status = 'APPROVED' AND (NEW.approval_hash IS DISTINCT FROM OLD.review_hash OR NEW.approved_by IS NULL OR NEW.approved_at IS NULL OR NEW.run_id IS NULL OR NEW.proposal_id IS NULL))
       OR (NEW.status = 'REJECTED' AND (NEW.approved_by IS NOT NULL OR NEW.approval_hash IS NOT NULL OR NEW.approved_at IS NOT NULL OR NEW.run_id IS NOT NULL OR NEW.proposal_id IS NOT NULL))
       OR (to_jsonb(NEW) - ARRAY['status', 'approved_by', 'approval_hash', 'approved_at', 'run_id', 'proposal_id']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'approved_by', 'approval_hash', 'approved_at', 'run_id', 'proposal_id']) THEN
        RAISE EXCEPTION 'Agent draft candidates permit only one exact review decision';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER agent_draft_candidates_guard BEFORE UPDATE OR DELETE ON agent_draft_candidates FOR EACH ROW EXECUTE FUNCTION restrict_agent_draft_candidate_mutation();

COMMIT;
