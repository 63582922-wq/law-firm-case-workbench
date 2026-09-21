-- Content proposals are not document packages, approvals or Worker jobs.
BEGIN;
CREATE TABLE case_agent_document_content_proposals (
    proposal_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    run_id uuid NOT NULL,
    predecessor_package_id uuid NOT NULL,
    expected_revision_number integer NOT NULL CHECK (expected_revision_number BETWEEN 1 AND 999),
    requested_by uuid NOT NULL,
    idempotency_key_hash text NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    candidate_content bytea NOT NULL CHECK (octet_length(candidate_content) BETWEEN 2 AND 2097152),
    review_manifest bytea NOT NULL CHECK (octet_length(review_manifest) BETWEEN 2 AND 2097152),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, requested_by, idempotency_key_hash),
    UNIQUE (proposal_id, firm_id, matter_id, run_id),
    FOREIGN KEY (requested_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (predecessor_package_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(package_id, run_id, firm_id, matter_id),
    CHECK (request_hash = encode(digest(review_manifest, 'sha256'), 'hex'))
);
CREATE INDEX content_proposals_matter_history
    ON case_agent_document_content_proposals
    (firm_id, matter_id, run_id, created_at DESC, proposal_id DESC);
ALTER TABLE case_agent_document_content_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_content_proposals FORCE ROW LEVEL SECURITY;
CREATE POLICY content_proposal_case_access ON case_agent_document_content_proposals
    TO lawcase_web_application
    USING (
        firm_id::text = current_setting('app.firm_id', true)
        AND EXISTS (
            SELECT 1 FROM users principal
            JOIN matter_actor_roles assignment
              ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
            WHERE principal.user_id::text = current_setting('app.actor_id', true)
              AND principal.firm_id = case_agent_document_content_proposals.firm_id
              AND principal.status = 'ACTIVE'
              AND assignment.matter_id = case_agent_document_content_proposals.matter_id
              AND assignment.revoked_at IS NULL
              AND assignment.role IN ('ASSISTANT','COLLABORATING_LAWYER','LEAD_LAWYER','REVIEWER')
        )
    ) WITH CHECK (
        firm_id::text = current_setting('app.firm_id', true)
        AND requested_by::text = current_setting('app.actor_id', true)
        AND EXISTS (
            SELECT 1 FROM users principal JOIN matter_actor_roles assignment
              ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
            WHERE principal.user_id = requested_by
              AND principal.firm_id = case_agent_document_content_proposals.firm_id
              AND principal.status = 'ACTIVE'
              AND assignment.matter_id = case_agent_document_content_proposals.matter_id
              AND assignment.revoked_at IS NULL
              AND assignment.role IN ('ASSISTANT','COLLABORATING_LAWYER','LEAD_LAWYER','REVIEWER')
        )
    );
CREATE TRIGGER content_proposals_append_only
    BEFORE UPDATE OR DELETE ON case_agent_document_content_proposals
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_document_revision_request_change();
REVOKE ALL ON case_agent_document_content_proposals
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
GRANT SELECT, INSERT ON case_agent_document_content_proposals TO lawcase_web_application;
COMMENT ON TABLE case_agent_document_content_proposals IS
    'Private immutable lawyer content proposals; no automatic execution or approval authority.';
CREATE TABLE case_agent_document_content_generation_reviews (
    review_id uuid PRIMARY KEY,
    proposal_id uuid NOT NULL UNIQUE,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    run_id uuid NOT NULL,
    root_package_id uuid NOT NULL,
    expected_revision_number integer NOT NULL CHECK (expected_revision_number BETWEEN 1 AND 999),
    candidate_hash text NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    binding_hash text NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    reviewed_by uuid NOT NULL,
    review_note text NOT NULL CHECK (length(btrim(review_note)) BETWEEN 1 AND 2000),
    idempotency_key_hash text NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    purpose text NOT NULL DEFAULT 'GENERATE_REVIEW_COPY' CHECK (purpose = 'GENERATE_REVIEW_COPY'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, reviewed_by, idempotency_key_hash),
    UNIQUE (review_id, firm_id, matter_id, run_id),
    FOREIGN KEY (proposal_id, firm_id, matter_id, run_id)
        REFERENCES case_agent_document_content_proposals(proposal_id, firm_id, matter_id, run_id),
    FOREIGN KEY (root_package_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(package_id, run_id, firm_id, matter_id),
    FOREIGN KEY (reviewed_by, firm_id) REFERENCES users(user_id, firm_id)
);
ALTER TABLE case_agent_document_content_generation_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_content_generation_reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY content_generation_review_access ON case_agent_document_content_generation_reviews
    TO lawcase_web_application
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM case_agent_document_content_proposals proposal
        WHERE proposal.proposal_id = case_agent_document_content_generation_reviews.proposal_id
          AND proposal.firm_id = case_agent_document_content_generation_reviews.firm_id
    ))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true)
        AND reviewed_by::text = current_setting('app.actor_id', true)
        AND EXISTS (
            SELECT 1 FROM users principal JOIN matter_actor_roles assignment
              ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
            WHERE principal.user_id = reviewed_by
              AND principal.firm_id = case_agent_document_content_generation_reviews.firm_id
              AND principal.status = 'ACTIVE'
              AND assignment.matter_id = case_agent_document_content_generation_reviews.matter_id
              AND assignment.revoked_at IS NULL AND assignment.role IN ('LEAD_LAWYER','REVIEWER')
        ));
CREATE TRIGGER content_generation_reviews_append_only
    BEFORE UPDATE OR DELETE ON case_agent_document_content_generation_reviews
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_document_revision_request_change();
REVOKE ALL ON case_agent_document_content_generation_reviews
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
GRANT SELECT, INSERT ON case_agent_document_content_generation_reviews TO lawcase_web_application;
COMMENT ON TABLE case_agent_document_content_generation_reviews IS
    'Lawyer authorization to generate a review copy only; not final approval or submission authority.';
CREATE TABLE case_agent_document_content_generation_jobs (
    review_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    run_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'READY' CHECK (state IN ('READY','LEASED','RENDERING','UNKNOWN','FAILED','SUCCEEDED')),
    claim_version integer NOT NULL DEFAULT 0 CHECK (claim_version BETWEEN 0 AND 3),
    claimed_by uuid,
    lease_expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (review_id, firm_id, matter_id, run_id)
        REFERENCES case_agent_document_content_generation_reviews(review_id, firm_id, matter_id, run_id),
    FOREIGN KEY (claimed_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK ((state = 'READY' AND claimed_by IS NULL AND lease_expires_at IS NULL AND claim_version = 0)
        OR (state <> 'READY' AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL AND claim_version > 0))
);
ALTER TABLE case_agent_document_content_generation_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_content_generation_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY content_generation_job_worker_access ON case_agent_document_content_generation_jobs
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM users principal JOIN matter_actor_roles assignment
          ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
        WHERE principal.user_id::text = current_setting('app.actor_id', true)
          AND principal.firm_id = case_agent_document_content_generation_jobs.firm_id
          AND principal.status = 'ACTIVE'
          AND assignment.matter_id = case_agent_document_content_generation_jobs.matter_id
          AND assignment.role = 'SYSTEM_WORKER' AND assignment.revoked_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM matter_actor_roles other_role
            WHERE other_role.user_id = principal.user_id AND other_role.firm_id = principal.firm_id
              AND other_role.matter_id = assignment.matter_id AND other_role.revoked_at IS NULL
              AND other_role.role <> 'SYSTEM_WORKER')
    ));
REVOKE ALL ON case_agent_document_content_generation_jobs
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
-- Applications cannot insert jobs directly; validated requests enqueue below.
GRANT SELECT, UPDATE ON case_agent_document_content_generation_jobs TO lawcase_agent_worker;
-- Human status reads expose no claim owner or private candidate bytes.
GRANT SELECT (review_id, firm_id, matter_id, run_id, state, lease_expires_at)
    ON case_agent_document_content_generation_jobs TO lawcase_web_application;
CREATE POLICY content_generation_job_lawyer_status
    ON case_agent_document_content_generation_jobs FOR SELECT TO lawcase_web_application
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM users principal JOIN matter_actor_roles assignment
          ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
        WHERE principal.user_id::text = current_setting('app.actor_id', true)
          AND principal.firm_id = case_agent_document_content_generation_jobs.firm_id
          AND principal.status = 'ACTIVE'
          AND assignment.matter_id = case_agent_document_content_generation_jobs.matter_id
          AND assignment.revoked_at IS NULL
          AND assignment.role IN ('ASSISTANT','COLLABORATING_LAWYER','LEAD_LAWYER','REVIEWER')
    ));
-- A Worker may read only the proposal attached to its own live claim before
-- or during rendering. These SELECT policies do not grant review or enqueue.
CREATE POLICY content_generation_review_claim_read
    ON case_agent_document_content_generation_reviews FOR SELECT TO lawcase_agent_worker
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM case_agent_document_content_generation_jobs job
        WHERE job.review_id = case_agent_document_content_generation_reviews.review_id
          AND job.firm_id = case_agent_document_content_generation_reviews.firm_id
          AND job.matter_id = case_agent_document_content_generation_reviews.matter_id
          AND job.run_id = case_agent_document_content_generation_reviews.run_id
          AND job.state IN ('LEASED','RENDERING') AND job.lease_expires_at > now()
          AND job.claimed_by::text = current_setting('app.actor_id', true)
    ));
CREATE POLICY content_proposal_claim_read
    ON case_agent_document_content_proposals FOR SELECT TO lawcase_agent_worker
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM case_agent_document_content_generation_reviews review
        JOIN case_agent_document_content_generation_jobs job
          ON job.review_id = review.review_id AND job.firm_id = review.firm_id
         AND job.matter_id = review.matter_id AND job.run_id = review.run_id
        WHERE review.proposal_id = case_agent_document_content_proposals.proposal_id
          AND review.firm_id = case_agent_document_content_proposals.firm_id
          AND review.matter_id = case_agent_document_content_proposals.matter_id
          AND review.run_id = case_agent_document_content_proposals.run_id
          AND job.state IN ('LEASED','RENDERING') AND job.lease_expires_at > now()
          AND job.claimed_by::text = current_setting('app.actor_id', true)
    ));
GRANT SELECT ON case_agent_document_content_proposals,
    case_agent_document_content_generation_reviews TO lawcase_agent_worker;
CREATE FUNCTION guard_content_generation_job_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF (NEW.review_id, NEW.firm_id, NEW.matter_id, NEW.run_id) IS DISTINCT FROM
       (OLD.review_id, OLD.firm_id, OLD.matter_id, OLD.run_id) THEN
        RAISE EXCEPTION 'content generation job scope is immutable';
    END IF;
    IF NEW.state = 'LEASED' AND NEW.claim_version = OLD.claim_version + 1
       AND NEW.claimed_by::text = current_setting('app.actor_id', true)
       AND NEW.lease_expires_at > now() AND NEW.lease_expires_at <= now() + interval '10 minutes'
       AND (OLD.state = 'READY' OR (OLD.state = 'LEASED' AND OLD.lease_expires_at <= now())) THEN
        NEW.updated_at := now(); RETURN NEW;
    END IF;
    IF (NEW.claimed_by, NEW.claim_version, NEW.lease_expires_at) IS DISTINCT FROM
       (OLD.claimed_by, OLD.claim_version, OLD.lease_expires_at) THEN
        RAISE EXCEPTION 'content generation claim fence differs';
    END IF;
    IF OLD.state = 'UNKNOWN' AND NEW.state = 'SUCCEEDED' AND EXISTS (
        SELECT 1 FROM case_agent_document_content_recoveries recovery
        JOIN case_agent_document_revision_requests request
          ON request.request_id = recovery.request_id AND request.firm_id = recovery.firm_id
         AND request.matter_id = recovery.matter_id AND request.run_id = recovery.run_id
        WHERE recovery.review_id = OLD.review_id AND recovery.firm_id = OLD.firm_id
          AND recovery.matter_id = OLD.matter_id AND recovery.run_id = OLD.run_id
          AND request.content_generation_review_id = OLD.review_id
          AND recovery.claim_version = OLD.claim_version AND recovery.executed_by = OLD.claimed_by
          AND recovery.verified_by::text = current_setting('app.actor_id', true)
    ) THEN
        NEW.updated_at := now(); RETURN NEW;
    END IF;
    IF ((OLD.state = 'RENDERING' AND NEW.state = 'SUCCEEDED')
        OR (OLD.state IN ('LEASED','RENDERING') AND NEW.state = 'FAILED'))
       AND EXISTS (
         SELECT 1 FROM case_agent_document_revision_requests request
         JOIN case_agent_document_revision_receipts receipt ON receipt.request_id = request.request_id
          AND receipt.firm_id = request.firm_id AND receipt.matter_id = request.matter_id
         WHERE request.content_generation_review_id = OLD.review_id AND request.firm_id = OLD.firm_id
           AND request.matter_id = OLD.matter_id AND request.run_id = OLD.run_id
           AND receipt.executed_by = OLD.claimed_by
           AND ((NEW.state = 'SUCCEEDED' AND receipt.outcome = 'PASSED'
                 AND receipt.verified_by::text = current_setting('app.actor_id', true))
             OR (NEW.state = 'FAILED' AND receipt.outcome = 'FAILED'
                 AND receipt.executed_by::text = current_setting('app.actor_id', true)))
       ) THEN
        NEW.updated_at := now(); RETURN NEW;
    END IF;
    IF (OLD.state = 'LEASED' AND NEW.state = 'RENDERING'
        AND OLD.lease_expires_at > now() AND OLD.claimed_by::text = current_setting('app.actor_id', true))
       OR (OLD.state = 'RENDERING' AND NEW.state = 'UNKNOWN'
        AND (OLD.lease_expires_at <= now() OR OLD.claimed_by::text = current_setting('app.actor_id', true)))
       OR (OLD.state = 'LEASED' AND NEW.state = 'FAILED' AND OLD.claim_version = 3 AND OLD.lease_expires_at <= now()) THEN
        NEW.updated_at := now(); RETURN NEW;
    END IF;
    RAISE EXCEPTION 'content generation job transition is not allowed';
END;
$$;
CREATE TRIGGER content_generation_job_transition_guard BEFORE UPDATE
    ON case_agent_document_content_generation_jobs FOR EACH ROW
    EXECUTE FUNCTION guard_content_generation_job_transition();
CREATE TABLE case_agent_document_content_generation_job_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_id uuid NOT NULL REFERENCES case_agent_document_content_generation_jobs(review_id),
    firm_id uuid NOT NULL,
    from_state text,
    to_state text NOT NULL,
    claim_version integer NOT NULL,
    changed_by uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (changed_by, firm_id) REFERENCES users(user_id, firm_id)
);
ALTER TABLE case_agent_document_content_generation_job_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_content_generation_job_events FORCE ROW LEVEL SECURITY;
CREATE POLICY content_generation_job_event_access ON case_agent_document_content_generation_job_events
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM case_agent_document_content_generation_jobs job
        WHERE job.review_id = case_agent_document_content_generation_job_events.review_id
          AND job.firm_id = case_agent_document_content_generation_job_events.firm_id
    ));
REVOKE ALL ON case_agent_document_content_generation_job_events
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
GRANT SELECT ON case_agent_document_content_generation_job_events TO lawcase_agent_worker;
CREATE TRIGGER content_generation_job_events_append_only BEFORE UPDATE OR DELETE
    ON case_agent_document_content_generation_job_events FOR EACH ROW
    EXECUTE FUNCTION prohibit_case_agent_document_revision_request_change();
CREATE FUNCTION record_content_generation_job_transition() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    INSERT INTO case_agent_document_content_generation_job_events
        (review_id, firm_id, from_state, to_state, claim_version, changed_by)
    VALUES (NEW.review_id, NEW.firm_id, CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.state END,
        NEW.state, NEW.claim_version, NULLIF(current_setting('app.actor_id', true), '')::uuid);
    RETURN NEW;
END;
$$;
CREATE TRIGGER content_generation_job_transition_audit AFTER INSERT OR UPDATE
    ON case_agent_document_content_generation_jobs FOR EACH ROW
    EXECUTE FUNCTION record_content_generation_job_transition();
-- Keep a single revision-request identity for future package/receipt lineage.
-- Body requests must never enter the legacy template compiler's inbox.
ALTER TABLE case_agent_document_revision_requests
    ADD COLUMN content_generation_review_id uuid UNIQUE,
    ADD CONSTRAINT document_revision_content_review_scope FOREIGN KEY
        (content_generation_review_id, firm_id, matter_id, run_id)
        REFERENCES case_agent_document_content_generation_reviews(review_id, firm_id, matter_id, run_id);
DROP TRIGGER case_agent_document_revision_requests_insert_guard ON case_agent_document_revision_requests;
CREATE TRIGGER case_agent_document_revision_requests_insert_guard
    BEFORE INSERT ON case_agent_document_revision_requests FOR EACH ROW
    WHEN (NEW.content_generation_review_id IS NULL)
    EXECUTE FUNCTION validate_case_agent_document_revision_request_insert();
DROP TRIGGER case_agent_document_revision_requests_enqueue ON case_agent_document_revision_requests;
CREATE TRIGGER case_agent_document_revision_requests_enqueue
    AFTER INSERT ON case_agent_document_revision_requests FOR EACH ROW
    WHEN (NEW.content_generation_review_id IS NULL)
    EXECUTE FUNCTION enqueue_case_agent_document_revision_request();

CREATE FUNCTION validate_document_content_revision_request_insert() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    source record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'case-agent-document-revision:' || NEW.firm_id::text || ':' || NEW.root_package_id::text, 0));
    IF NEW.requested_by IS DISTINCT FROM NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
        RAISE EXCEPTION 'content revision requester differs from transaction identity';
    END IF;
    SELECT predecessor.*, review.reviewed_by, review.binding_hash AS reviewed_binding_hash,
           review.expected_revision_number AS reviewed_revision_number,
           review.root_package_id AS reviewed_root_package_id,
           proposal.expected_revision_number AS proposed_revision_number
      INTO source
      FROM case_agent_document_content_generation_reviews review
      JOIN case_agent_document_content_proposals proposal
        ON proposal.proposal_id = review.proposal_id AND proposal.firm_id = review.firm_id
       AND proposal.matter_id = review.matter_id AND proposal.run_id = review.run_id
      JOIN case_agent_reviewable_document_packages predecessor
        ON predecessor.package_id = proposal.predecessor_package_id AND predecessor.firm_id = proposal.firm_id
       AND predecessor.matter_id = proposal.matter_id AND predecessor.run_id = proposal.run_id
      JOIN users principal ON principal.user_id = review.reviewed_by
       AND principal.firm_id = review.firm_id AND principal.status = 'ACTIVE'
     WHERE review.review_id = NEW.content_generation_review_id AND review.firm_id = NEW.firm_id
       AND review.matter_id = NEW.matter_id AND review.run_id = NEW.run_id
       AND review.purpose = 'GENERATE_REVIEW_COPY'
       AND EXISTS (SELECT 1 FROM matter_actor_roles assignment
         WHERE assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
           AND assignment.matter_id = NEW.matter_id AND assignment.revoked_at IS NULL
           AND assignment.role IN ('LEAD_LAWYER','REVIEWER'));
    IF source IS NULL
       OR source.reviewed_by IS DISTINCT FROM NEW.requested_by
       OR source.package_id IS DISTINCT FROM NEW.predecessor_package_id
       OR source.reviewed_root_package_id IS DISTINCT FROM NEW.root_package_id
       OR COALESCE(source.root_package_id, source.package_id) IS DISTINCT FROM NEW.root_package_id
       OR source.revision_number <> NEW.expected_revision_number
       OR source.reviewed_revision_number <> NEW.expected_revision_number
       OR source.proposed_revision_number <> NEW.expected_revision_number
       OR source.binding_hash <> source.reviewed_binding_hash
       OR source.package_receipt_hash <> NEW.source_package_receipt_hash
       OR source.template_id <> NEW.target_template_id
       OR source.template_version <> NEW.target_template_version
       OR source.template_hash <> NEW.target_template_hash
       OR source.output_format <> 'DOCX' THEN
        RAISE EXCEPTION 'content revision differs from its authorized predecessor';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM case_agent_runs run
        JOIN matters matter ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
        JOIN case_agent_reviewable_document_packages root ON root.package_id = NEW.root_package_id
          AND root.run_id = run.run_id AND root.firm_id = run.firm_id AND root.matter_id = run.matter_id
        JOIN case_agent_verification_attempts attempt ON attempt.run_id = run.run_id
          AND attempt.firm_id = run.firm_id AND attempt.matter_id = run.matter_id
        JOIN case_agent_verification_receipts receipt
          ON receipt.verification_attempt_id = attempt.verification_attempt_id
         AND receipt.run_id = attempt.run_id AND receipt.firm_id = attempt.firm_id
         AND receipt.matter_id = attempt.matter_id
        WHERE run.run_id = NEW.run_id AND run.firm_id = NEW.firm_id AND run.matter_id = NEW.matter_id
          AND run.status = 'READY_FOR_REVIEW' AND NOT run.is_stale AND NOT run.is_cancelled
          AND run.current_graph_id = source.graph_id AND run.snapshot_matter_version = matter.version
          AND run.snapshot_hash = source.case_snapshot_hash
          AND root.generation_mode = 'INITIAL_AGENT_TASK' AND root.revision_number = 1
          AND root.graph_id = source.graph_id AND root.task_id = source.task_id
          AND receipt.outcome = 'PASSED' AND receipt.verification_hash = run.verification_hash
          AND receipt.graph_hash = run.current_graph_hash AND receipt.snapshot_hash = run.snapshot_hash
          AND receipt.artifact_lineage @> jsonb_build_array(
              jsonb_build_object('artifact_id', root.candidate_artifact_id::text),
              jsonb_build_object('artifact_id', root.editable_artifact_id::text),
              jsonb_build_object('artifact_id', root.review_pdf_artifact_id::text))
    ) OR NEW.predecessor_package_id IS DISTINCT FROM (
        SELECT package.package_id FROM case_agent_reviewable_document_packages package
        WHERE package.firm_id = NEW.firm_id AND package.matter_id = NEW.matter_id AND package.run_id = NEW.run_id
          AND (package.package_id = NEW.root_package_id OR (package.root_package_id = NEW.root_package_id
            AND EXISTS (SELECT 1 FROM case_agent_document_revision_current_results receipt
              WHERE receipt.successor_package_id = package.package_id AND receipt.firm_id = package.firm_id
                AND receipt.matter_id = package.matter_id AND receipt.outcome = 'PASSED')))
        ORDER BY package.revision_number DESC LIMIT 1
    ) THEN
        RAISE EXCEPTION 'content revision is not based on the current verified package';
    END IF;
    IF NEW.request_hash IS DISTINCT FROM encode(digest(concat_ws('|',
        'case-agent-document-content-revision-request-v1', NEW.request_id::text, NEW.firm_id::text,
        NEW.matter_id::text, NEW.run_id::text, NEW.root_package_id::text, NEW.predecessor_package_id::text,
        NEW.expected_revision_number::text, NEW.target_template_id, NEW.target_template_version,
        NEW.target_template_hash, NEW.source_package_receipt_hash, NEW.requested_by::text,
        NEW.idempotency_key_hash, NEW.content_generation_review_id::text)::bytea, 'sha256'), 'hex') THEN
        RAISE EXCEPTION 'content revision request hash is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER document_content_revision_requests_insert_guard
    BEFORE INSERT ON case_agent_document_revision_requests FOR EACH ROW
    WHEN (NEW.content_generation_review_id IS NOT NULL)
    EXECUTE FUNCTION validate_document_content_revision_request_insert();
-- SECURITY DEFINER does not bypass FORCE RLS after the migration owner is
-- demoted. Only its trigger may insert a READY job for the validated request
-- of the current actor. This grants no application INSERT or human UPDATE.
CREATE POLICY content_generation_job_request_enqueue
    ON case_agent_document_content_generation_jobs FOR INSERT TO lawcase_schema_owner
    WITH CHECK (
        firm_id::text = current_setting('app.firm_id', true)
        AND state = 'READY' AND claim_version = 0
        AND claimed_by IS NULL AND lease_expires_at IS NULL
        AND EXISTS (
            SELECT 1 FROM case_agent_document_revision_requests request
            WHERE request.content_generation_review_id = case_agent_document_content_generation_jobs.review_id
              AND request.firm_id = case_agent_document_content_generation_jobs.firm_id
              AND request.matter_id = case_agent_document_content_generation_jobs.matter_id
              AND request.run_id = case_agent_document_content_generation_jobs.run_id
              AND request.requested_by::text = current_setting('app.actor_id', true)
        )
    );
-- The initial audit insert runs in the same human transaction, before any
-- Worker claim. Do not broaden the human's job visibility to make it pass.
CREATE POLICY content_generation_job_initial_event
    ON case_agent_document_content_generation_job_events FOR INSERT TO lawcase_schema_owner
    WITH CHECK (
        firm_id::text = current_setting('app.firm_id', true)
        AND changed_by::text = current_setting('app.actor_id', true)
        AND from_state IS NULL AND to_state = 'READY' AND claim_version = 0
        AND EXISTS (
            SELECT 1 FROM case_agent_document_revision_requests request
            WHERE request.content_generation_review_id = case_agent_document_content_generation_job_events.review_id
              AND request.firm_id = case_agent_document_content_generation_job_events.firm_id
              AND request.requested_by = case_agent_document_content_generation_job_events.changed_by
        )
    );
CREATE FUNCTION enqueue_document_content_revision_request() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    INSERT INTO case_agent_document_content_generation_jobs (review_id, firm_id, matter_id, run_id)
    VALUES (NEW.content_generation_review_id, NEW.firm_id, NEW.matter_id, NEW.run_id);
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION enqueue_document_content_revision_request()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
CREATE TRIGGER document_content_revision_requests_enqueue
    AFTER INSERT ON case_agent_document_revision_requests FOR EACH ROW
    WHEN (NEW.content_generation_review_id IS NOT NULL)
    EXECUTE FUNCTION enqueue_document_content_revision_request();
-- Body packages share the existing chain, but carry their exact render claim.
ALTER TABLE case_agent_reviewable_document_packages
    ADD COLUMN content_generation_claim_version integer,
    DROP CONSTRAINT case_agent_reviewable_document_packages_generation_mode_check,
    DROP CONSTRAINT case_agent_reviewable_document_package_revision_shape,
    ADD CONSTRAINT case_agent_reviewable_document_packages_generation_mode_check CHECK (
        generation_mode IN ('INITIAL_AGENT_TASK','DETERMINISTIC_TEMPLATE_REVISION','LAWYER_CONTENT_REVISION')),
    ADD CONSTRAINT case_agent_reviewable_document_package_revision_shape CHECK (
        (generation_mode = 'INITIAL_AGENT_TASK' AND revision_number = 1
         AND root_package_id IS NULL AND supersedes_package_id IS NULL
         AND revision_request_id IS NULL AND requested_by IS NULL AND content_generation_claim_version IS NULL)
        OR (generation_mode IN ('DETERMINISTIC_TEMPLATE_REVISION','LAWYER_CONTENT_REVISION')
         AND revision_number BETWEEN 2 AND 1000 AND root_package_id IS NOT NULL
         AND supersedes_package_id IS NOT NULL AND revision_request_id IS NOT NULL AND requested_by IS NOT NULL
         AND ((generation_mode = 'DETERMINISTIC_TEMPLATE_REVISION' AND content_generation_claim_version IS NULL)
           OR (generation_mode = 'LAWYER_CONTENT_REVISION' AND content_generation_claim_version IS NOT NULL
             AND content_generation_claim_version BETWEEN 1 AND 3)))
    );
CREATE FUNCTION validate_case_agent_document_content_package_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    revision_request record;
    predecessor record;
    root_package record;
    inbox_row record;
    review_row record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'case-agent-document-revision:' || NEW.firm_id::text || ':' || NEW.root_package_id::text, 0));
    IF NEW.generation_mode <> 'LAWYER_CONTENT_REVISION' THEN
        RAISE EXCEPTION 'document revision insert mode is invalid';
    END IF;
    IF NEW.staged_by IS DISTINCT FROM
       NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
        RAISE EXCEPTION 'document revision Worker differs from transaction identity';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM users principal
        JOIN matter_actor_roles role
          ON role.user_id = principal.user_id AND role.firm_id = principal.firm_id
        WHERE principal.user_id = NEW.staged_by AND principal.firm_id = NEW.firm_id
          AND principal.status = 'ACTIVE' AND role.matter_id = NEW.matter_id
          AND role.role = 'SYSTEM_WORKER' AND role.revoked_at IS NULL
    ) OR EXISTS (
        SELECT 1 FROM matter_actor_roles role
        WHERE role.user_id = NEW.staged_by AND role.firm_id = NEW.firm_id
          AND role.matter_id = NEW.matter_id AND role.role <> 'SYSTEM_WORKER'
          AND role.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'document revision requires a dedicated active SYSTEM_WORKER';
    END IF;

    SELECT request.* INTO revision_request
      FROM case_agent_document_revision_requests request
     WHERE request.request_id = NEW.revision_request_id
       AND request.firm_id = NEW.firm_id AND request.matter_id = NEW.matter_id
       AND request.run_id = NEW.run_id;
    SELECT * INTO predecessor
      FROM case_agent_reviewable_document_packages package
     WHERE package.package_id = NEW.supersedes_package_id
       AND package.firm_id = NEW.firm_id AND package.matter_id = NEW.matter_id
       AND package.run_id = NEW.run_id;
    SELECT * INTO root_package
      FROM case_agent_reviewable_document_packages package
     WHERE package.package_id = NEW.root_package_id
       AND package.firm_id = NEW.firm_id AND package.matter_id = NEW.matter_id
       AND package.run_id = NEW.run_id;
    SELECT job.* INTO inbox_row
      FROM case_agent_document_content_generation_jobs job
     WHERE job.review_id = revision_request.content_generation_review_id
       AND job.firm_id = NEW.firm_id AND job.matter_id = NEW.matter_id
       AND job.run_id = NEW.run_id FOR UPDATE;
    SELECT review.* INTO review_row
      FROM case_agent_document_content_generation_reviews review
      JOIN users principal ON principal.user_id = review.reviewed_by
       AND principal.firm_id = review.firm_id AND principal.status = 'ACTIVE'
     WHERE review.review_id = revision_request.content_generation_review_id
       AND review.firm_id = NEW.firm_id AND review.matter_id = NEW.matter_id AND review.run_id = NEW.run_id
       AND review.purpose = 'GENERATE_REVIEW_COPY'
       AND EXISTS (SELECT 1 FROM matter_actor_roles assignment
         WHERE assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
           AND assignment.matter_id = NEW.matter_id AND assignment.revoked_at IS NULL
           AND assignment.role IN ('LEAD_LAWYER','REVIEWER'));

    IF revision_request IS NULL OR predecessor IS NULL OR root_package IS NULL
       OR review_row IS NULL OR revision_request.content_generation_review_id IS NULL
       OR review_row.reviewed_by IS DISTINCT FROM NEW.requested_by
       OR review_row.root_package_id IS DISTINCT FROM NEW.root_package_id
       OR review_row.expected_revision_number <> NEW.revision_number - 1
       OR review_row.candidate_hash IS DISTINCT FROM NEW.candidate_hash
       OR review_row.binding_hash IS DISTINCT FROM NEW.binding_hash
       OR NEW.binding_hash IS DISTINCT FROM predecessor.binding_hash
       OR NEW.output_format <> 'DOCX'
       OR inbox_row IS NULL OR inbox_row.state <> 'RENDERING'
       OR NEW.content_generation_claim_version IS DISTINCT FROM inbox_row.claim_version
       OR inbox_row.claimed_by IS DISTINCT FROM NEW.staged_by
       OR inbox_row.lease_expires_at <= clock_timestamp()
       OR revision_request.root_package_id IS DISTINCT FROM NEW.root_package_id
       OR revision_request.predecessor_package_id IS DISTINCT FROM NEW.supersedes_package_id
       OR revision_request.requested_by IS DISTINCT FROM NEW.requested_by
       OR revision_request.expected_revision_number <> predecessor.revision_number
       OR NEW.revision_number <> predecessor.revision_number + 1
       OR root_package.generation_mode <> 'INITIAL_AGENT_TASK' OR root_package.revision_number <> 1
       OR predecessor.package_id <> NEW.root_package_id
          AND predecessor.root_package_id IS DISTINCT FROM NEW.root_package_id
       OR revision_request.target_template_id <> NEW.template_id
       OR revision_request.target_template_version <> NEW.template_version
       OR revision_request.target_template_hash <> NEW.template_hash
       OR revision_request.source_package_receipt_hash <> predecessor.package_receipt_hash
       OR NEW.graph_id IS DISTINCT FROM predecessor.graph_id
       OR NEW.task_id IS DISTINCT FROM predecessor.task_id
       OR NEW.attempt_id IS DISTINCT FROM predecessor.attempt_id
       OR NEW.task_input_hash IS DISTINCT FROM predecessor.task_input_hash
       OR NEW.case_snapshot_hash IS DISTINCT FROM predecessor.case_snapshot_hash
       OR NEW.source_set_hash IS DISTINCT FROM predecessor.source_set_hash
       OR NEW.authorized_source_refs IS DISTINCT FROM predecessor.authorized_source_refs
       OR NEW.authorized_source_manifest IS DISTINCT FROM predecessor.authorized_source_manifest
       OR NEW.work_plan_id IS DISTINCT FROM predecessor.work_plan_id
       OR NEW.work_plan_hash IS DISTINCT FROM predecessor.work_plan_hash
       OR NEW.work_plan_item_id IS DISTINCT FROM predecessor.work_plan_item_id
       OR NEW.posture_profile_id IS DISTINCT FROM predecessor.posture_profile_id
       OR NEW.posture_profile_hash IS DISTINCT FROM predecessor.posture_profile_hash
       OR NEW.deliverable_kind IS DISTINCT FROM predecessor.deliverable_kind
       OR NEW.output_format IS DISTINCT FROM predecessor.output_format
       OR NEW.template_id IS DISTINCT FROM predecessor.template_id
       OR NEW.template_version IS DISTINCT FROM predecessor.template_version
       OR NEW.template_hash IS DISTINCT FROM predecessor.template_hash
       OR EXISTS (
            SELECT 1
            FROM case_agent_reviewable_document_packages successor
            JOIN case_agent_document_revision_current_results successor_receipt
              ON successor_receipt.successor_package_id = successor.package_id
             AND successor_receipt.firm_id = successor.firm_id
             AND successor_receipt.matter_id = successor.matter_id
             AND successor_receipt.outcome = 'PASSED'
            WHERE successor.supersedes_package_id = predecessor.package_id
       ) THEN
        RAISE EXCEPTION 'document revision differs from its current governed request';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM case_agent_runs run
        JOIN matters matter
          ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
        JOIN case_agent_task_graphs graph
          ON graph.graph_id = NEW.graph_id AND graph.run_id = run.run_id
         AND graph.firm_id = run.firm_id AND graph.matter_id = run.matter_id
        JOIN case_agent_task_heads head
          ON head.graph_id = NEW.graph_id AND head.task_id = NEW.task_id
         AND head.run_id = run.run_id AND head.firm_id = run.firm_id
         AND head.matter_id = run.matter_id
        JOIN case_agent_task_attempts attempt
          ON attempt.attempt_id = NEW.attempt_id AND attempt.graph_id = NEW.graph_id
         AND attempt.task_id = NEW.task_id AND attempt.run_id = run.run_id
         AND attempt.firm_id = run.firm_id AND attempt.matter_id = run.matter_id
        JOIN case_work_plan_heads plan_head
          ON plan_head.matter_id = run.matter_id AND plan_head.firm_id = run.firm_id
         AND plan_head.current_plan_id = NEW.work_plan_id
        JOIN case_work_plans plan
          ON plan.plan_id = plan_head.current_plan_id AND plan.firm_id = run.firm_id
         AND plan.matter_id = run.matter_id
        JOIN case_posture_profile_heads profile_head
          ON profile_head.matter_id = run.matter_id AND profile_head.firm_id = run.firm_id
         AND profile_head.current_profile_id = NEW.posture_profile_id
        JOIN case_posture_profiles profile
          ON profile.profile_id = profile_head.current_profile_id
         AND profile.firm_id = run.firm_id AND profile.matter_id = run.matter_id
        WHERE run.run_id = NEW.run_id AND run.firm_id = NEW.firm_id
          AND run.matter_id = NEW.matter_id AND run.status = 'READY_FOR_REVIEW'
          AND NOT run.is_stale AND NOT run.is_cancelled
          AND run.current_graph_id = NEW.graph_id
          AND run.snapshot_hash = NEW.case_snapshot_hash
          AND graph.snapshot_hash = NEW.case_snapshot_hash
          AND matter.version = run.snapshot_matter_version
          AND head.status = 'SUCCEEDED' AND head.is_current
          AND attempt.status = 'SUCCEEDED'
          AND plan.status = 'ACTIVE' AND plan.plan_hash = NEW.work_plan_hash
          AND plan.profile_id = NEW.posture_profile_id
          AND plan.profile_hash = NEW.posture_profile_hash
          AND plan.activated_matter_version = run.snapshot_matter_version
          AND profile.status = 'CONFIRMED'
          AND profile.profile_hash = NEW.posture_profile_hash
    ) THEN
        RAISE EXCEPTION 'document revision no longer belongs to the current review state';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM case_agent_verification_attempts attempt
        JOIN case_agent_verification_receipts receipt
          ON receipt.verification_attempt_id = attempt.verification_attempt_id
         AND receipt.run_id = attempt.run_id AND receipt.firm_id = attempt.firm_id
         AND receipt.matter_id = attempt.matter_id
        JOIN case_agent_runs run ON run.run_id = attempt.run_id
         AND run.firm_id = attempt.firm_id AND run.matter_id = attempt.matter_id
        WHERE run.run_id = NEW.run_id AND run.firm_id = NEW.firm_id AND run.matter_id = NEW.matter_id
          AND receipt.outcome = 'PASSED' AND receipt.verification_hash = run.verification_hash
          AND receipt.graph_hash = run.current_graph_hash AND receipt.snapshot_hash = run.snapshot_hash
          AND receipt.artifact_lineage @> jsonb_build_array(
            jsonb_build_object('artifact_id', root_package.candidate_artifact_id::text),
            jsonb_build_object('artifact_id', root_package.editable_artifact_id::text),
            jsonb_build_object('artifact_id', root_package.review_pdf_artifact_id::text))
    ) THEN
        RAISE EXCEPTION 'content revision root is not in the current verified lineage';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_document_content_packages_insert_guard
    BEFORE INSERT ON case_agent_reviewable_document_packages FOR EACH ROW
    WHEN (NEW.generation_mode = 'LAWYER_CONTENT_REVISION')
    EXECUTE FUNCTION validate_case_agent_document_content_package_insert();
GRANT SELECT, UPDATE (state) ON case_agent_document_content_generation_jobs TO lawcase_agent_verifier;
CREATE POLICY content_generation_review_verifier_read
    ON case_agent_document_content_generation_reviews FOR SELECT TO lawcase_agent_verifier
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM case_agent_document_content_generation_jobs job
        WHERE job.review_id = case_agent_document_content_generation_reviews.review_id
          AND job.firm_id = case_agent_document_content_generation_reviews.firm_id
          AND job.matter_id = case_agent_document_content_generation_reviews.matter_id
          AND job.run_id = case_agent_document_content_generation_reviews.run_id
          AND job.state IN ('RENDERING','SUCCEEDED')
    ));
GRANT SELECT ON case_agent_document_content_generation_reviews TO lawcase_agent_verifier;
CREATE OR REPLACE FUNCTION validate_case_agent_document_revision_receipt_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    revision_request record;
    successor record;
    content_job record;
    content_review record;
BEGIN
    SELECT request.* INTO revision_request
      FROM case_agent_document_revision_requests request
     WHERE request.request_id = NEW.request_id
       AND request.firm_id = NEW.firm_id AND request.matter_id = NEW.matter_id;
    IF revision_request IS NULL THEN
        RAISE EXCEPTION 'document revision receipt has no governed request';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'case-agent-document-revision:' || NEW.firm_id::text || ':' ||
        revision_request.root_package_id::text,
        0
    ));

    IF NEW.outcome = 'PASSED' THEN
        IF NEW.verified_by IS DISTINCT FROM
           NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
            RAISE EXCEPTION 'document revision verifier differs from transaction identity';
        END IF;
        SELECT package.* INTO successor
          FROM case_agent_reviewable_document_packages package
         WHERE package.package_id = NEW.successor_package_id
           AND package.firm_id = NEW.firm_id AND package.matter_id = NEW.matter_id
           AND package.revision_request_id = NEW.request_id;
        IF successor IS NULL
           OR successor.package_receipt_hash <> NEW.successor_package_receipt_hash
           OR successor.staged_by IS DISTINCT FROM NEW.executed_by
           OR successor.requested_by IS DISTINCT FROM revision_request.requested_by
           OR successor.generation_mode <> (CASE WHEN revision_request.content_generation_review_id IS NULL
                THEN 'DETERMINISTIC_TEMPLATE_REVISION' ELSE 'LAWYER_CONTENT_REVISION' END)
           OR successor.supersedes_package_id IS DISTINCT FROM
                revision_request.predecessor_package_id
           OR successor.root_package_id IS DISTINCT FROM
                revision_request.root_package_id
           OR EXISTS (
                SELECT 1
                FROM case_agent_reviewable_document_packages prior_successor
                JOIN case_agent_document_revision_current_results prior_receipt
                  ON prior_receipt.successor_package_id = prior_successor.package_id
                 AND prior_receipt.firm_id = prior_successor.firm_id
                 AND prior_receipt.matter_id = prior_successor.matter_id
                 AND prior_receipt.outcome = 'PASSED'
                WHERE prior_successor.firm_id = NEW.firm_id
                  AND prior_successor.matter_id = NEW.matter_id
                  AND (
                    prior_successor.supersedes_package_id =
                        successor.supersedes_package_id
                    OR (
                        prior_successor.root_package_id = successor.root_package_id
                        AND prior_successor.revision_number = successor.revision_number
                    )
                  )
           )
           OR NOT EXISTS (
                SELECT 1
                FROM users verifier
                JOIN matter_actor_roles role
                  ON role.user_id = verifier.user_id AND role.firm_id = verifier.firm_id
                WHERE verifier.user_id = NEW.verified_by
                  AND verifier.firm_id = NEW.firm_id
                  AND verifier.status = 'ACTIVE'
                  AND role.matter_id = NEW.matter_id
                  AND role.role = 'SYSTEM_WORKER'
                  AND role.revoked_at IS NULL
           ) OR EXISTS (
                SELECT 1 FROM matter_actor_roles extra
                WHERE extra.user_id = NEW.verified_by
                  AND extra.firm_id = NEW.firm_id
                  AND extra.matter_id = NEW.matter_id
                  AND extra.role <> 'SYSTEM_WORKER'
                  AND extra.revoked_at IS NULL
           ) THEN
            RAISE EXCEPTION 'document revision success receipt is invalid';
        END IF;
    ELSE
        IF NEW.executed_by IS DISTINCT FROM
           NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
            RAISE EXCEPTION 'document revision failure recorder differs from transaction identity';
        END IF;
    END IF;
    IF revision_request.content_generation_review_id IS NOT NULL THEN
        SELECT job.* INTO content_job FROM case_agent_document_content_generation_jobs job
         WHERE job.review_id = revision_request.content_generation_review_id
           AND job.firm_id = NEW.firm_id AND job.matter_id = NEW.matter_id
           AND job.run_id = revision_request.run_id FOR UPDATE;
        IF content_job IS NULL OR content_job.claimed_by IS DISTINCT FROM NEW.executed_by THEN
            RAISE EXCEPTION 'content receipt has no matching render owner';
        END IF;
        IF NEW.outcome = 'PASSED' THEN
            SELECT review.* INTO content_review FROM case_agent_document_content_generation_reviews review
              JOIN users reviewer ON reviewer.user_id = review.reviewed_by
               AND reviewer.firm_id = review.firm_id AND reviewer.status = 'ACTIVE'
             WHERE review.review_id = content_job.review_id AND review.firm_id = NEW.firm_id
               AND review.matter_id = NEW.matter_id AND review.run_id = revision_request.run_id
               AND review.purpose = 'GENERATE_REVIEW_COPY'
               AND EXISTS (SELECT 1 FROM matter_actor_roles role
                 WHERE role.user_id = reviewer.user_id AND role.firm_id = reviewer.firm_id
                   AND role.matter_id = NEW.matter_id AND role.revoked_at IS NULL
                   AND role.role IN ('LEAD_LAWYER','REVIEWER'));
            IF content_job.state <> 'RENDERING' OR content_job.lease_expires_at <= clock_timestamp()
               OR content_review IS NULL
               OR content_review.reviewed_by IS DISTINCT FROM successor.requested_by
               OR content_review.candidate_hash IS DISTINCT FROM successor.candidate_hash
               OR content_review.binding_hash IS DISTINCT FROM successor.binding_hash
               OR successor.content_generation_claim_version IS DISTINCT FROM content_job.claim_version
               OR NOT EXISTS (
                 SELECT 1 FROM case_agent_runs run
                 JOIN matters matter ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
                 JOIN case_work_plan_heads plan_head ON plan_head.matter_id = run.matter_id
                   AND plan_head.firm_id = run.firm_id AND plan_head.current_plan_id = successor.work_plan_id
                 JOIN case_posture_profile_heads profile_head ON profile_head.matter_id = run.matter_id
                   AND profile_head.firm_id = run.firm_id AND profile_head.current_profile_id = successor.posture_profile_id
                 WHERE run.run_id = revision_request.run_id AND run.firm_id = NEW.firm_id
                   AND run.matter_id = NEW.matter_id AND run.status = 'READY_FOR_REVIEW'
                   AND NOT run.is_stale AND NOT run.is_cancelled
                   AND run.current_graph_id = successor.graph_id AND run.snapshot_hash = successor.case_snapshot_hash
                   AND run.snapshot_matter_version = matter.version
               ) THEN
                RAISE EXCEPTION 'content success receipt is stale or not authorized';
            END IF;
        ELSIF NEW.outcome = 'UNKNOWN' THEN
            IF content_job.state NOT IN ('RENDERING','UNKNOWN') THEN
                RAISE EXCEPTION 'content unknown receipt has no rendering attempt';
            END IF;
        ELSIF NEW.outcome = 'FAILED' THEN
            IF content_job.state NOT IN ('LEASED','RENDERING')
               OR content_job.lease_expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'content failure receipt no longer owns a known stage';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE FUNCTION finish_content_generation_from_receipt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    content_review_id uuid;
    terminal_state text;
BEGIN
    SELECT content_generation_review_id INTO content_review_id
      FROM case_agent_document_revision_requests
     WHERE request_id = NEW.request_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF content_review_id IS NULL THEN RETURN NEW; END IF;
    terminal_state := CASE WHEN NEW.outcome = 'PASSED' THEN 'SUCCEEDED' ELSE NEW.outcome END;
    UPDATE case_agent_document_content_generation_jobs
       SET state = terminal_state, updated_at = now()
     WHERE review_id = content_review_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id
       AND state IS DISTINCT FROM terminal_state;
    RETURN NEW;
END;
$$;
CREATE TRIGGER content_generation_receipt_finishes_job
    AFTER INSERT ON case_agent_document_revision_receipts FOR EACH ROW
    EXECUTE FUNCTION finish_content_generation_from_receipt();
-- Recovery evidence is separate from the original one-result-per-request ledger.
-- Deliberately inaccessible to runtime roles until effective-result consumers,
-- the independent recovery writer and atomic job transition are wired together.
CREATE TABLE case_agent_document_content_recoveries (
    recovery_id uuid PRIMARY KEY,
    schema_version text NOT NULL DEFAULT 'document-content-recovery-v1'
        CHECK (schema_version = 'document-content-recovery-v1'),
    request_id uuid NOT NULL UNIQUE,
    review_id uuid NOT NULL,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    run_id uuid NOT NULL,
    original_receipt_id uuid,
    original_receipt_hash text,
    successor_package_id uuid NOT NULL,
    successor_package_receipt_hash text NOT NULL CHECK (successor_package_receipt_hash ~ '^[0-9a-f]{64}$'),
    claim_version integer NOT NULL CHECK (claim_version BETWEEN 1 AND 3),
    executed_by uuid NOT NULL,
    verified_by uuid NOT NULL,
    verification_hash text NOT NULL CHECK (verification_hash ~ '^[0-9a-f]{64}$'),
    recovery_hash text NOT NULL CHECK (recovery_hash ~ '^[0-9a-f]{64}$'),
    external_calls integer NOT NULL DEFAULT 0 CHECK (external_calls = 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (executed_by <> verified_by),
    CHECK ((original_receipt_id IS NULL AND original_receipt_hash IS NULL)
        OR (original_receipt_id IS NOT NULL AND original_receipt_hash IS NOT NULL
            AND original_receipt_hash ~ '^[0-9a-f]{64}$')),
    FOREIGN KEY (request_id, firm_id, matter_id)
        REFERENCES case_agent_document_revision_requests(request_id, firm_id, matter_id),
    FOREIGN KEY (review_id, firm_id, matter_id, run_id)
        REFERENCES case_agent_document_content_generation_reviews(review_id, firm_id, matter_id, run_id),
    FOREIGN KEY (original_receipt_id, firm_id, matter_id)
        REFERENCES case_agent_document_revision_receipts(receipt_id, firm_id, matter_id),
    FOREIGN KEY (successor_package_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(package_id, firm_id, matter_id),
    FOREIGN KEY (executed_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (verified_by, firm_id) REFERENCES users(user_id, firm_id)
);
ALTER TABLE case_agent_document_content_recoveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_content_recoveries FORCE ROW LEVEL SECURITY;
REVOKE ALL ON case_agent_document_content_recoveries
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
CREATE TRIGGER content_recoveries_append_only BEFORE UPDATE OR DELETE
    ON case_agent_document_content_recoveries FOR EACH ROW
    EXECUTE FUNCTION prohibit_case_agent_document_revision_receipt_change();
COMMENT ON TABLE case_agent_document_content_recoveries IS
    'Unreleased forward-only recovery evidence; no runtime grants/policies or effective-result activation yet.';
CREATE VIEW case_agent_document_revision_current_results WITH (security_invoker = true) AS
    SELECT receipt.receipt_id, receipt.request_id, receipt.firm_id, receipt.matter_id, receipt.outcome,
           receipt.successor_package_id, receipt.successor_package_receipt_hash, receipt.failure_code,
           receipt.external_calls, receipt.executed_by, receipt.verified_by, receipt.receipt_hash,
           receipt.created_at, 'ORIGINAL'::text AS result_origin
      FROM case_agent_document_revision_receipts receipt
     WHERE NOT EXISTS (SELECT 1 FROM case_agent_document_content_recoveries recovery
         WHERE recovery.request_id = receipt.request_id AND recovery.firm_id = receipt.firm_id
           AND recovery.matter_id = receipt.matter_id)
       AND EXISTS (SELECT 1 FROM users principal JOIN matter_actor_roles assignment
            ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
            WHERE principal.user_id::text = current_setting('app.actor_id', true)
              AND principal.firm_id = receipt.firm_id AND principal.status = 'ACTIVE'
              AND assignment.matter_id = receipt.matter_id AND assignment.revoked_at IS NULL
              AND assignment.role IN ('ASSISTANT','COLLABORATING_LAWYER','LEAD_LAWYER','REVIEWER','SYSTEM_WORKER'))
    UNION ALL
    SELECT recovery.recovery_id, recovery.request_id, recovery.firm_id, recovery.matter_id, 'PASSED'::text,
           recovery.successor_package_id, recovery.successor_package_receipt_hash, NULL::text,
           recovery.external_calls, recovery.executed_by, recovery.verified_by, recovery.recovery_hash,
           recovery.created_at, 'RECOVERY'::text
      FROM case_agent_document_content_recoveries recovery;
REVOKE ALL ON case_agent_document_revision_current_results
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;

CREATE FUNCTION guard_document_content_recovery_insert() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    request_row case_agent_document_revision_requests%ROWTYPE;
    job_row case_agent_document_content_generation_jobs%ROWTYPE;
    package_row case_agent_reviewable_document_packages%ROWTYPE;
    original_row case_agent_document_revision_receipts%ROWTYPE;
    expected_verification text;
    expected_recovery text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed'
       OR NEW.firm_id::text IS DISTINCT FROM current_setting('app.firm_id', true)
       OR NEW.verified_by::text IS DISTINCT FROM current_setting('app.actor_id', true) THEN
        RAISE EXCEPTION 'content recovery transaction identity or isolation is invalid';
    END IF;
    -- Match completion's run/matter-before-root order. Then use the original
    -- recorder's receipt-before-root order; never acquire a run lock after root.
    PERFORM lock_document_content_recovery_context(NEW.firm_id, NEW.matter_id, NEW.run_id);
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'document-revision-receipt:' || NEW.firm_id::text || ':' || NEW.request_id::text, 0));
    SELECT * INTO request_row FROM case_agent_document_revision_requests
     WHERE request_id = NEW.request_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id
       AND run_id = NEW.run_id AND content_generation_review_id = NEW.review_id;
    IF request_row IS NULL THEN RAISE EXCEPTION 'content recovery request is unavailable'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'case-agent-document-revision:' || NEW.firm_id::text || ':' || request_row.root_package_id::text, 0));
    SELECT * INTO job_row FROM case_agent_document_content_generation_jobs
     WHERE review_id = NEW.review_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id
       AND run_id = NEW.run_id FOR UPDATE;
    IF job_row IS NULL OR job_row.state <> 'UNKNOWN'
       OR job_row.claim_version IS DISTINCT FROM NEW.claim_version
       OR job_row.claimed_by IS DISTINCT FROM NEW.executed_by THEN
        RAISE EXCEPTION 'content recovery does not match an unknown claim';
    END IF;
    SELECT * INTO original_row FROM case_agent_document_revision_receipts
     WHERE request_id = NEW.request_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF original_row IS NULL THEN
        IF NEW.original_receipt_id IS NOT NULL OR NEW.original_receipt_hash IS NOT NULL THEN
            RAISE EXCEPTION 'content recovery invents an original receipt';
        END IF;
    ELSIF original_row.outcome <> 'UNKNOWN'
       OR original_row.receipt_id IS DISTINCT FROM NEW.original_receipt_id
       OR original_row.receipt_hash IS DISTINCT FROM NEW.original_receipt_hash
       OR original_row.executed_by IS DISTINCT FROM NEW.executed_by THEN
        RAISE EXCEPTION 'content recovery original receipt differs';
    END IF;
    SELECT * INTO package_row FROM case_agent_reviewable_document_packages
     WHERE package_id = NEW.successor_package_id AND firm_id = NEW.firm_id
       AND matter_id = NEW.matter_id AND run_id = NEW.run_id;
    IF package_row IS NULL OR package_row.generation_mode <> 'LAWYER_CONTENT_REVISION'
       OR package_row.revision_request_id IS DISTINCT FROM NEW.request_id
       OR package_row.root_package_id IS DISTINCT FROM request_row.root_package_id
       OR package_row.supersedes_package_id IS DISTINCT FROM request_row.predecessor_package_id
       OR package_row.revision_number <> request_row.expected_revision_number + 1
       OR package_row.content_generation_claim_version IS DISTINCT FROM NEW.claim_version
       OR package_row.staged_by IS DISTINCT FROM NEW.executed_by
       OR package_row.package_receipt_hash IS DISTINCT FROM NEW.successor_package_receipt_hash THEN
        RAISE EXCEPTION 'content recovery package differs from original authorization';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(ARRAY[NEW.executed_by, NEW.verified_by]) AS participant(user_id)
        WHERE NOT EXISTS (SELECT 1 FROM users principal JOIN matter_actor_roles assignment
            ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
            WHERE principal.user_id = participant.user_id AND principal.firm_id = NEW.firm_id
              AND principal.status = 'ACTIVE' AND assignment.matter_id = NEW.matter_id
              AND assignment.revoked_at IS NULL AND assignment.role = 'SYSTEM_WORKER')
           OR EXISTS (SELECT 1 FROM matter_actor_roles extra WHERE extra.user_id = participant.user_id
              AND extra.firm_id = NEW.firm_id AND extra.matter_id = NEW.matter_id
              AND extra.revoked_at IS NULL AND extra.role <> 'SYSTEM_WORKER')) THEN
        RAISE EXCEPTION 'content recovery requires dedicated active execution and verification identities';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM case_agent_document_content_generation_reviews review
        JOIN users reviewer ON reviewer.user_id = review.reviewed_by AND reviewer.firm_id = review.firm_id
        WHERE review.review_id = NEW.review_id AND review.firm_id = NEW.firm_id
          AND review.matter_id = NEW.matter_id AND review.run_id = NEW.run_id
          AND review.purpose = 'GENERATE_REVIEW_COPY' AND reviewer.status = 'ACTIVE'
          AND review.candidate_hash = package_row.candidate_hash AND review.binding_hash = package_row.binding_hash
          AND review.reviewed_by = package_row.requested_by AND package_row.requested_by = request_row.requested_by
          AND EXISTS (SELECT 1 FROM matter_actor_roles assignment WHERE assignment.user_id = reviewer.user_id
              AND assignment.firm_id = NEW.firm_id AND assignment.matter_id = NEW.matter_id
              AND assignment.revoked_at IS NULL AND assignment.role IN ('LEAD_LAWYER','REVIEWER'))) THEN
        RAISE EXCEPTION 'content recovery lawyer authorization is stale';
    END IF;
    IF request_row.predecessor_package_id IS DISTINCT FROM (
        SELECT package.package_id FROM case_agent_reviewable_document_packages package
         WHERE package.firm_id = NEW.firm_id AND package.matter_id = NEW.matter_id AND package.run_id = NEW.run_id
           AND (package.package_id = request_row.root_package_id OR (
                package.root_package_id = request_row.root_package_id AND EXISTS (
                    SELECT 1 FROM case_agent_document_revision_current_results result
                     WHERE result.successor_package_id = package.package_id AND result.firm_id = NEW.firm_id
                       AND result.matter_id = NEW.matter_id AND result.outcome = 'PASSED'
                       AND result.successor_package_receipt_hash = package.package_receipt_hash)))
         ORDER BY package.revision_number DESC LIMIT 1) THEN
        RAISE EXCEPTION 'content recovery predecessor is no longer current';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM case_agent_runs run
        JOIN matters matter ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
        JOIN case_work_plan_heads plan_head ON plan_head.matter_id = run.matter_id AND plan_head.firm_id = run.firm_id
        JOIN case_posture_profile_heads profile_head ON profile_head.matter_id = run.matter_id AND profile_head.firm_id = run.firm_id
        WHERE run.run_id = NEW.run_id AND run.firm_id = NEW.firm_id AND run.matter_id = NEW.matter_id
          AND run.status = 'READY_FOR_REVIEW' AND NOT run.is_stale AND NOT run.is_cancelled
          AND run.current_graph_id = package_row.graph_id AND run.snapshot_hash = package_row.case_snapshot_hash
          AND run.snapshot_matter_version = matter.version AND plan_head.current_plan_id = package_row.work_plan_id
          AND profile_head.current_profile_id = package_row.posture_profile_id) THEN
        RAISE EXCEPTION 'content recovery run or source context is stale';
    END IF;
    -- Exact UTF-8 unit-separator contracts; no JSON whitespace or locale dependency.
    expected_verification := encode(digest(convert_to(concat_ws(chr(31),
        'document-content-recovery-files-v1', NEW.firm_id::text, NEW.matter_id::text, NEW.run_id::text,
        NEW.request_id::text, NEW.successor_package_id::text, NEW.successor_package_receipt_hash,
        package_row.candidate_content_sha256::text, package_row.editable_sha256::text,
        package_row.review_pdf_sha256::text, NEW.verified_by::text), 'UTF8'), 'sha256'), 'hex');
    expected_recovery := encode(digest(convert_to(concat_ws(chr(31),
        NEW.schema_version, NEW.recovery_id::text, NEW.request_id::text, request_row.request_hash,
        NEW.review_id::text, NEW.firm_id::text, NEW.matter_id::text, NEW.run_id::text,
        COALESCE(NEW.original_receipt_id::text, 'ABSENT'), COALESCE(NEW.original_receipt_hash, 'ABSENT'),
        NEW.successor_package_id::text, NEW.successor_package_receipt_hash, NEW.claim_version::text,
        NEW.executed_by::text, NEW.verified_by::text, expected_verification, '0'), 'UTF8'), 'sha256'), 'hex');
    IF NEW.verification_hash IS DISTINCT FROM expected_verification OR NEW.recovery_hash IS DISTINCT FROM expected_recovery THEN
        RAISE EXCEPTION 'content recovery evidence digest differs';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION guard_document_content_recovery_insert()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
CREATE TRIGGER content_recovery_insert_guard BEFORE INSERT ON case_agent_document_content_recoveries
    FOR EACH ROW EXECUTE FUNCTION guard_document_content_recovery_insert();
CREATE FUNCTION finish_document_content_recovery() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    changed_rows integer;
BEGIN
    -- Invoker authority only. The guarded INSERT, state transition and existing
    -- append-only transition audit commit together, or none of them do.
    UPDATE case_agent_document_content_generation_jobs SET state = 'SUCCEEDED'
     WHERE review_id = NEW.review_id AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id
       AND run_id = NEW.run_id AND state = 'UNKNOWN' AND claim_version = NEW.claim_version
       AND claimed_by = NEW.executed_by;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'content recovery did not transition exactly its original unknown claim';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION finish_document_content_recovery()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
CREATE TRIGGER content_recovery_finishes_job AFTER INSERT ON case_agent_document_content_recoveries
    FOR EACH ROW EXECUTE FUNCTION finish_document_content_recovery();

-- Bring the two legacy template guards onto the same current-result contract,
-- without copying their large bodies or changing the applied 0073 migration.
DO $$
DECLARE
    signature text;
    definition text;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'public.validate_case_agent_document_revision_request_insert()',
        'public.validate_case_agent_reviewable_document_revision_insert()'
    ] LOOP
        definition := pg_get_functiondef(signature::regprocedure);
        IF position('case_agent_document_revision_receipts' IN definition) = 0 THEN
            RAISE EXCEPTION 'legacy revision guard has unexpected source: %', signature;
        END IF;
        EXECUTE replace(definition, 'case_agent_document_revision_receipts',
                                    'case_agent_document_revision_current_results');
    END LOOP;
END;
$$;

-- Read-only access is required as soon as the view is present. No recovery
-- INSERT, job-lock privilege or recovery execution permission is granted here.
CREATE POLICY content_recovery_case_read ON case_agent_document_content_recoveries
    FOR SELECT TO lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier, lawcase_schema_owner
    USING (firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
        SELECT 1 FROM users principal JOIN matter_actor_roles assignment
          ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
        WHERE principal.user_id::text = current_setting('app.actor_id', true)
          AND principal.firm_id = case_agent_document_content_recoveries.firm_id
          AND principal.status = 'ACTIVE' AND assignment.revoked_at IS NULL
          AND assignment.matter_id = case_agent_document_content_recoveries.matter_id
          AND assignment.role IN ('ASSISTANT','COLLABORATING_LAWYER','LEAD_LAWYER','REVIEWER','SYSTEM_WORKER')
    ));
GRANT SELECT ON case_agent_document_content_recoveries, case_agent_document_revision_current_results
    TO lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
COMMENT ON TABLE case_agent_document_content_recoveries IS
    'Forward-only recovery evidence, case-scoped read only; runtime recovery writes remain disabled.';

CREATE ROLE lawcase_document_recovery_lock_owner NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS;
GRANT USAGE, CREATE ON SCHEMA public TO lawcase_document_recovery_lock_owner;
GRANT SELECT ON users, matter_actor_roles, matters, case_agent_runs TO lawcase_document_recovery_lock_owner;
GRANT UPDATE (matter_id) ON matters TO lawcase_document_recovery_lock_owner;
GRANT UPDATE (run_id) ON case_agent_runs TO lawcase_document_recovery_lock_owner;
CREATE FUNCTION lock_document_content_recovery_context(target_firm uuid, target_matter uuid, target_run uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF target_firm::text IS DISTINCT FROM current_setting('app.firm_id', true)
       OR NOT EXISTS (SELECT 1 FROM users principal JOIN matter_actor_roles assignment
           ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
           WHERE principal.user_id::text = current_setting('app.actor_id', true)
             AND principal.firm_id = target_firm AND principal.status = 'ACTIVE'
             AND assignment.matter_id = target_matter AND assignment.revoked_at IS NULL
             AND assignment.role = 'SYSTEM_WORKER'
             AND NOT EXISTS (SELECT 1 FROM matter_actor_roles extra WHERE extra.user_id = principal.user_id
                 AND extra.firm_id = target_firm AND extra.matter_id = target_matter
                 AND extra.revoked_at IS NULL AND extra.role <> 'SYSTEM_WORKER')) THEN
        RAISE EXCEPTION 'content recovery lock requires a dedicated case verifier';
    END IF;
    PERFORM 1 FROM case_agent_runs run JOIN matters matter
      ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
     WHERE run.run_id = target_run AND run.firm_id = target_firm AND run.matter_id = target_matter
     FOR UPDATE OF run, matter;
    IF NOT FOUND THEN RAISE EXCEPTION 'content recovery context is unavailable'; END IF;
END;
$$;
ALTER FUNCTION lock_document_content_recovery_context(uuid, uuid, uuid) OWNER TO lawcase_document_recovery_lock_owner;
REVOKE CREATE ON SCHEMA public FROM lawcase_document_recovery_lock_owner;
REVOKE ALL ON FUNCTION lock_document_content_recovery_context(uuid, uuid, uuid)
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker, lawcase_agent_verifier;
GRANT EXECUTE ON FUNCTION lock_document_content_recovery_context(uuid, uuid, uuid) TO lawcase_agent_verifier;
CREATE POLICY content_recovery_verifier_append ON case_agent_document_content_recoveries
    FOR INSERT TO lawcase_agent_verifier WITH CHECK (
        firm_id::text = current_setting('app.firm_id', true)
        AND verified_by::text = current_setting('app.actor_id', true)
        AND verified_by <> executed_by
        AND EXISTS (SELECT 1 FROM users principal JOIN matter_actor_roles assignment
            ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
            WHERE principal.user_id = verified_by AND principal.firm_id = case_agent_document_content_recoveries.firm_id
              AND principal.status = 'ACTIVE' AND assignment.matter_id = case_agent_document_content_recoveries.matter_id
              AND assignment.role = 'SYSTEM_WORKER' AND assignment.revoked_at IS NULL)
    );
GRANT INSERT ON case_agent_document_content_recoveries TO lawcase_agent_verifier;
CREATE POLICY content_recovery_review_read ON case_agent_document_content_generation_reviews
    FOR SELECT TO lawcase_agent_verifier USING (
        firm_id::text = current_setting('app.firm_id', true) AND EXISTS (
            SELECT 1 FROM case_agent_document_content_generation_jobs job
            WHERE job.review_id = case_agent_document_content_generation_reviews.review_id
              AND job.firm_id = case_agent_document_content_generation_reviews.firm_id
              AND job.matter_id = case_agent_document_content_generation_reviews.matter_id
              AND job.run_id = case_agent_document_content_generation_reviews.run_id
              AND job.state IN ('UNKNOWN','SUCCEEDED')
        )
    );
COMMENT ON TABLE case_agent_document_content_recoveries IS
    'Forward-only guarded verifier recovery evidence; explicit application recovery enablement is required.';
COMMIT;
