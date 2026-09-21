-- A correction enters the existing fact ledger as CANDIDATE, never approval.
BEGIN;
ALTER TABLE case_facts
    ADD COLUMN correction_proposal_id uuid,
    ADD COLUMN correction_candidate_id uuid,
    ADD CONSTRAINT fact_correction_pair CHECK
        ((correction_proposal_id IS NULL) = (correction_candidate_id IS NULL)),
    ADD CONSTRAINT fact_correction_source FOREIGN KEY
        (correction_proposal_id, correction_candidate_id, firm_id, matter_id)
        REFERENCES case_agent_fact_correction_proposals
        (proposal_id, extraction_candidate_id, firm_id, matter_id),
    ADD CONSTRAINT fact_correction_unique_proposal UNIQUE (correction_proposal_id),
    ADD CONSTRAINT fact_correction_unique_candidate UNIQUE (correction_candidate_id);

CREATE FUNCTION guard_fact_correction_candidate_lineage()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    proposal case_agent_fact_correction_proposals%ROWTYPE;
    body jsonb;
    current_version integer;
    source_links jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        IF OLD.correction_proposal_id IS NOT NULL THEN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'linked correction facts cannot be deleted';
            END IF;
            IF ROW(NEW.fact_id, NEW.firm_id, NEW.matter_id, NEW.original_text,
                   NEW.origin, NEW.evidence_links, NEW.correction_proposal_id, NEW.correction_candidate_id)
               IS DISTINCT FROM ROW(OLD.fact_id, OLD.firm_id, OLD.matter_id, OLD.original_text,
                   OLD.origin, OLD.evidence_links, OLD.correction_proposal_id, OLD.correction_candidate_id) THEN
                RAISE EXCEPTION 'correction fact source and text are immutable';
            END IF;
        ELSIF TG_OP = 'UPDATE' AND NEW.correction_proposal_id IS NOT NULL THEN
            RAISE EXCEPTION 'correction lineage must be inserted with its fact';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF NEW.correction_proposal_id IS NULL THEN RETURN NEW; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM users u JOIN matter_actor_roles a
          ON a.user_id=u.user_id AND a.firm_id=u.firm_id
        WHERE u.user_id::text=current_setting('app.actor_id',true)
          AND u.firm_id=NEW.firm_id AND u.status='ACTIVE'
          AND a.matter_id=NEW.matter_id AND a.revoked_at IS NULL
          AND a.role IN ('LEAD_LAWYER','REVIEWER')
    ) THEN RAISE EXCEPTION 'current matter lawyer required'; END IF;
    SELECT version INTO current_version FROM matters
      WHERE matter_id=NEW.matter_id AND firm_id=NEW.firm_id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM case_agent_ledger_extraction_promotions
        WHERE extraction_candidate_id=NEW.correction_candidate_id
          AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id) THEN
        RAISE EXCEPTION 'original candidate already entered the ledger';
    END IF;
    SELECT * INTO proposal FROM case_agent_fact_correction_proposals
      WHERE extraction_candidate_id=NEW.correction_candidate_id
        AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id
      ORDER BY revision_number DESC LIMIT 1;
    IF NOT FOUND OR proposal.proposal_id IS DISTINCT FROM NEW.correction_proposal_id
       OR proposal.expected_matter_version IS DISTINCT FROM current_version THEN
        RAISE EXCEPTION 'current correction proposal required';
    END IF;
    body := convert_from(proposal.proposal_content,'UTF8')::jsonb;
    SELECT jsonb_agg(jsonb_build_object(
        'evidence_id', p.evidence_page_id, 'original_file_sha256', f.original_file_sha256,
        'page_number', p.page_number, 'region_id', NULL, 'original_label', f.original_label)
        ORDER BY p.page_number,p.evidence_page_id)
      INTO source_links
      FROM case_agent_ledger_extraction_candidate_pages cp
      JOIN evidence_pages p ON p.evidence_page_id=cp.evidence_page_id
        AND p.firm_id=cp.firm_id AND p.matter_id=cp.matter_id
      JOIN evidence_original_files f ON f.evidence_file_id=p.evidence_file_id
        AND f.firm_id=p.firm_id AND f.matter_id=p.matter_id
      WHERE cp.extraction_candidate_id=NEW.correction_candidate_id
        AND cp.firm_id=NEW.firm_id AND cp.matter_id=NEW.matter_id;
    IF NEW.status <> 'CANDIDATE' OR NEW.origin <> 'ASSISTANT_ENTRY'
       OR NEW.original_text IS DISTINCT FROM body->>'revised_text'
       OR source_links IS NULL OR NEW.evidence_links IS DISTINCT FROM source_links THEN
        RAISE EXCEPTION 'correction must enter as source-bound unapproved fact';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER fact_correction_candidate_lineage_guard
    BEFORE INSERT OR UPDATE OR DELETE ON case_facts FOR EACH ROW
    EXECUTE FUNCTION guard_fact_correction_candidate_lineage();
REVOKE ALL ON FUNCTION guard_fact_correction_candidate_lineage() FROM PUBLIC;

CREATE FUNCTION guard_extraction_promotion_after_correction()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM matters WHERE matter_id=NEW.matter_id AND firm_id=NEW.firm_id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM case_facts WHERE correction_candidate_id=NEW.extraction_candidate_id
        AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id) THEN
        RAISE EXCEPTION 'corrected candidate already entered the ledger';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER extraction_promotion_correction_guard
    BEFORE INSERT ON case_agent_ledger_extraction_promotions FOR EACH ROW
    EXECUTE FUNCTION guard_extraction_promotion_after_correction();
REVOKE ALL ON FUNCTION guard_extraction_promotion_after_correction() FROM PUBLIC;
COMMIT;
