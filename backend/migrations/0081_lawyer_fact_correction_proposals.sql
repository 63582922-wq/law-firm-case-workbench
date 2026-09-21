-- Private append-only proposals, never authoritative facts or approvals.
BEGIN;
CREATE TABLE case_agent_fact_correction_proposals (
    proposal_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    extraction_candidate_id uuid NOT NULL,
    expected_matter_version integer NOT NULL CHECK (expected_matter_version > 0),
    revision_number integer NOT NULL CHECK (revision_number BETWEEN 1 AND 999),
    predecessor_proposal_id uuid,
    requested_by uuid NOT NULL,
    idempotency_key_hash text NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    proposal_content bytea NOT NULL CHECK (octet_length(proposal_content) BETWEEN 2 AND 262144),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, requested_by, idempotency_key_hash),
    UNIQUE (extraction_candidate_id, revision_number),
    UNIQUE (proposal_id, extraction_candidate_id, firm_id, matter_id),
    FOREIGN KEY (extraction_candidate_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_extraction_candidates(extraction_candidate_id, firm_id, matter_id),
    FOREIGN KEY (predecessor_proposal_id, extraction_candidate_id, firm_id, matter_id)
        REFERENCES case_agent_fact_correction_proposals(proposal_id, extraction_candidate_id, firm_id, matter_id),
    FOREIGN KEY (requested_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    CHECK ((revision_number = 1) = (predecessor_proposal_id IS NULL))
);
ALTER TABLE case_agent_fact_correction_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_fact_correction_proposals FORCE ROW LEVEL SECURITY;
CREATE POLICY fact_correction_lawyer_access ON case_agent_fact_correction_proposals
    TO lawcase_web_application
    USING (
        firm_id::text = current_setting('app.firm_id', true)
        AND EXISTS (
            SELECT 1 FROM users u JOIN matter_actor_roles a
              ON a.user_id = u.user_id AND a.firm_id = u.firm_id
            WHERE u.user_id::text = current_setting('app.actor_id', true)
              AND u.firm_id = case_agent_fact_correction_proposals.firm_id
              AND u.status = 'ACTIVE' AND a.revoked_at IS NULL
              AND a.matter_id = case_agent_fact_correction_proposals.matter_id
              AND a.role IN ('LEAD_LAWYER','REVIEWER')
        )
    ) WITH CHECK (
        firm_id::text = current_setting('app.firm_id', true)
        AND requested_by::text = current_setting('app.actor_id', true)
        AND EXISTS (
            SELECT 1 FROM users u JOIN matter_actor_roles a
              ON a.user_id = u.user_id AND a.firm_id = u.firm_id
            WHERE u.user_id = requested_by
              AND u.firm_id = case_agent_fact_correction_proposals.firm_id
              AND u.status = 'ACTIVE' AND a.revoked_at IS NULL
              AND a.matter_id = case_agent_fact_correction_proposals.matter_id
              AND a.role IN ('LEAD_LAWYER','REVIEWER')
        )
    );
CREATE FUNCTION guard_case_agent_fact_correction_proposal()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    current_version integer;
    prior case_agent_fact_correction_proposals%ROWTYPE;
    candidate case_agent_ledger_extraction_candidates%ROWTYPE;
    batch case_agent_ledger_extraction_batches%ROWTYPE;
    body jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'fact correction proposals are immutable';
    END IF;
    SELECT version INTO current_version FROM matters
     WHERE matter_id = NEW.matter_id AND firm_id = NEW.firm_id FOR UPDATE;
    IF current_version IS DISTINCT FROM NEW.expected_matter_version THEN
        RAISE EXCEPTION 'fact correction matter version differs';
    END IF;
    SELECT * INTO candidate FROM case_agent_ledger_extraction_candidates
     WHERE extraction_candidate_id = NEW.extraction_candidate_id
       AND matter_id = NEW.matter_id AND firm_id = NEW.firm_id;
    IF NOT FOUND OR candidate.candidate_kind <> 'FACT' THEN
        RAISE EXCEPTION 'fact correction requires exact original FACT';
    END IF;
    SELECT * INTO batch FROM case_agent_ledger_extraction_batches
     WHERE extraction_batch_id = candidate.extraction_batch_id
       AND matter_id = NEW.matter_id AND firm_id = NEW.firm_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'fact correction source batch missing'; END IF;
    SELECT * INTO prior FROM case_agent_fact_correction_proposals
     WHERE extraction_candidate_id = NEW.extraction_candidate_id
       AND matter_id = NEW.matter_id AND firm_id = NEW.firm_id
     ORDER BY revision_number DESC LIMIT 1;
    IF NEW.revision_number <> COALESCE(prior.revision_number, 0) + 1
       OR NEW.predecessor_proposal_id IS DISTINCT FROM prior.proposal_id THEN
        RAISE EXCEPTION 'fact correction predecessor changed';
    END IF;
    body := convert_from(NEW.proposal_content, 'UTF8')::jsonb;
    IF jsonb_typeof(body) IS DISTINCT FROM 'object'
       OR (SELECT count(*) FROM jsonb_object_keys(body)) <> 8
       OR body->>'schema_version' IS DISTINCT FROM 'lawyer-fact-correction-proposal-v1'
       OR body->>'review_status' IS DISTINCT FROM 'NEEDS_LAWYER_REVIEW'
       OR body->'court_ready' IS DISTINCT FROM 'false'::jsonb
       OR body->>'original_artifact_hash' IS DISTINCT FROM batch.artifact_content_sha256::text
       OR body->>'source_hash' IS DISTINCT FROM batch.source_hash::text
       OR body->'original_candidate' IS DISTINCT FROM candidate.candidate_payload
       OR jsonb_typeof(body->'revised_text') IS DISTINCT FROM 'string'
       OR jsonb_typeof(body->'reason') IS DISTINCT FROM 'string'
       OR length(btrim(body->>'revised_text')) NOT BETWEEN 1 AND 4000
       OR length(btrim(body->>'reason')) NOT BETWEEN 1 AND 2000
       OR btrim(body->>'revised_text') = btrim(candidate.candidate_payload->>'fact_text') THEN
        RAISE EXCEPTION 'fact correction proposal binding differs';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER fact_correction_proposal_guard
    BEFORE INSERT OR UPDATE OR DELETE ON case_agent_fact_correction_proposals
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_fact_correction_proposal();
REVOKE ALL ON case_agent_fact_correction_proposals
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
-- The source-reverification save command is wired. Only append/read is
-- permitted; RLS and the immutable source/version guard apply to every write.
GRANT SELECT, INSERT ON case_agent_fact_correction_proposals TO lawcase_web_application;
REVOKE ALL ON FUNCTION guard_case_agent_fact_correction_proposal() FROM PUBLIC;
COMMIT;
