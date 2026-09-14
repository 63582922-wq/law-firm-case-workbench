-- Private, verified staging for source-bound fact/transaction extraction.
--
-- This is intentionally not a formal case_facts/case_transactions write path.
-- A model/browser never writes into these tables.  The server first proves a
-- passed independent verification receipt, current run/graph/task lineage and
-- immutable artifact bytes, then stores every proposal in a private review
-- projection.  Only high-confidence, source-proven records without conflicts
-- or risks enter the bulk-review lane.  Lawyer confirmation remains a later,
-- separate matter-ledger command.

BEGIN;

CREATE TABLE case_agent_ledger_extraction_batches (
    extraction_batch_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    verification_receipt_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    source_matter_version integer NOT NULL CHECK (source_matter_version > 0),
    staged_matter_version integer NOT NULL CHECK (staged_matter_version > 0),
    artifact_content_sha256 char(64) NOT NULL CHECK (artifact_content_sha256 ~ '^[0-9a-f]{64}$'),
    source_hash char(64) NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    task_input_hash char(64) NOT NULL CHECK (task_input_hash ~ '^[0-9a-f]{64}$'),
    candidate_count integer NOT NULL CHECK (candidate_count BETWEEN 0 AND 500),
    eligible_candidate_count integer NOT NULL CHECK (eligible_candidate_count BETWEEN 0 AND 500),
    staged_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (artifact_id),
    UNIQUE (extraction_batch_id, firm_id, matter_id),
    UNIQUE (extraction_batch_id, artifact_id, run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (artifact_id, firm_id, matter_id)
        REFERENCES case_agent_artifacts(artifact_id, firm_id, matter_id),
    FOREIGN KEY (verification_receipt_id)
        REFERENCES case_agent_verification_receipts(verification_receipt_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (staged_by, firm_id) REFERENCES users(user_id, firm_id),
    -- Private staging is not an authoritative case-ledger mutation.  Its
    -- receipt remains bound to the run snapshot version and does not consume
    -- the version increment needed by the verified work-plan candidate.
    CHECK (staged_matter_version = source_matter_version),
    CHECK (eligible_candidate_count <= candidate_count)
);

-- Private, append-only audit for automatic staging.  ``audit_events`` and
-- ``outbox_events`` are reserved for authoritative matter-version changes;
-- writing either here would falsely claim a V -> V+1 ledger transition.
CREATE TABLE case_agent_ledger_extraction_staging_events (
    staging_event_id uuid PRIMARY KEY,
    extraction_batch_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    event_type text NOT NULL CHECK (
        event_type = 'VERIFIED_EXTRACTION_STAGED_PRIVATE'
    ),
    source_matter_version integer NOT NULL CHECK (source_matter_version > 0),
    staged_matter_version integer NOT NULL CHECK (staged_matter_version > 0),
    actor_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (
        length(trim(idempotency_key)) BETWEEN 1 AND 200
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (extraction_batch_id, event_type),
    UNIQUE (
        firm_id, matter_id, actor_id, event_type, idempotency_key
    ),
    FOREIGN KEY (
        extraction_batch_id, artifact_id, run_id, firm_id, matter_id
    ) REFERENCES case_agent_ledger_extraction_batches(
        extraction_batch_id, artifact_id, run_id, firm_id, matter_id
    ),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (staged_matter_version = source_matter_version)
);

CREATE TABLE case_agent_ledger_extraction_candidates (
    extraction_candidate_id uuid PRIMARY KEY,
    extraction_batch_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    candidate_hash char(64) NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    candidate_kind text NOT NULL CHECK (candidate_kind IN ('FACT', 'TRANSACTION')),
    confidence numeric(6,5) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    review_lane text NOT NULL CHECK (review_lane IN ('BULK_PROMOTION_ELIGIBLE', 'EXCEPTION_REVIEW')),
    eligible_for_bulk_promotion boolean NOT NULL,
    review_reason_codes text[] NOT NULL CHECK (
        review_reason_codes <@ ARRAY[
            'POSSIBLE_DUPLICATE',
            'PARTY_AMBIGUOUS',
            'DATE_AMBIGUOUS',
            'AMOUNT_AMBIGUOUS',
            'CROSS_PAGE_CONFLICT',
            'CONTRADICTS_CASE_LEDGER',
            'OCR_DERIVED',
            'LOW_CONFIDENCE',
            'LEGAL_CONCLUSION_RISK',
            'INCOMPLETE_TRANSACTION',
            'UNTRUSTED_TEXT',
            'BELOW_BULK_CONFIDENCE_THRESHOLD',
            'NON_NATIVE_SOURCE',
            'SOURCE_TEXT_NOT_REVERIFIED',
            'CURRENT_LEDGER_CONFLICT_OR_DUPLICATE'
        ]::text[]
        AND cardinality(review_reason_codes) <= 15
    ),
    candidate_payload jsonb NOT NULL CHECK (jsonb_typeof(candidate_payload) = 'object'),
    review_status text NOT NULL CHECK (review_status = 'NEEDS_LAWYER_REVIEW'),
    formal_fact boolean NOT NULL DEFAULT false CHECK (formal_fact = false),
    formal_transaction boolean NOT NULL DEFAULT false CHECK (formal_transaction = false),
    legal_conclusion boolean NOT NULL DEFAULT false CHECK (legal_conclusion = false),
    evidence_decision boolean NOT NULL DEFAULT false CHECK (evidence_decision = false),
    court_ready boolean NOT NULL DEFAULT false CHECK (court_ready = false),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (extraction_candidate_id, firm_id, matter_id),
    UNIQUE (
        extraction_candidate_id, extraction_batch_id, firm_id, matter_id
    ),
    UNIQUE (extraction_batch_id, candidate_hash),
    FOREIGN KEY (extraction_batch_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_extraction_batches(extraction_batch_id, firm_id, matter_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    CHECK (
        (review_lane = 'BULK_PROMOTION_ELIGIBLE'
            AND eligible_for_bulk_promotion = true
            AND cardinality(review_reason_codes) = 0)
        OR (review_lane = 'EXCEPTION_REVIEW'
            AND eligible_for_bulk_promotion = false
            AND cardinality(review_reason_codes) > 0)
    )
);

CREATE TABLE case_agent_ledger_extraction_candidate_pages (
    extraction_candidate_id uuid NOT NULL,
    evidence_page_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    -- The authorized plaintext projection is version-bound as well as the
    -- original evidence file.  Promotion re-reads it before creating a
    -- formal ledger candidate.
    source_text_sha256 char(64) NOT NULL CHECK (source_text_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (extraction_candidate_id, evidence_page_id),
    FOREIGN KEY (extraction_candidate_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_extraction_candidates(extraction_candidate_id, firm_id, matter_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)
);

-- Promotion is an immutable mapping, not an update of the extracted record.
-- It proves which private review candidate produced which formal ledger
-- object.  The preview path produces CANDIDATE targets; the separate explicit
-- lead-lawyer low-risk batch command may produce CONFIRMED targets, but only
-- with the exact batch confirmation receipt enforced below.
CREATE TABLE case_agent_ledger_extraction_promotions (
    extraction_promotion_id uuid PRIMARY KEY,
    extraction_batch_id uuid NOT NULL,
    extraction_candidate_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    target_object_type text NOT NULL CHECK (target_object_type IN ('FACT', 'TRANSACTION')),
    target_object_id uuid NOT NULL,
    promoted_matter_version integer NOT NULL CHECK (promoted_matter_version > 0),
    lawyer_batch_decision_hash char(64) NOT NULL CHECK (lawyer_batch_decision_hash ~ '^[0-9a-f]{64}$'),
    promoted_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (extraction_candidate_id),
    UNIQUE (extraction_promotion_id, firm_id, matter_id),
    FOREIGN KEY (extraction_batch_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_extraction_batches(extraction_batch_id, firm_id, matter_id),
    FOREIGN KEY (
        extraction_candidate_id, extraction_batch_id, firm_id, matter_id
    ) REFERENCES case_agent_ledger_extraction_candidates(
        extraction_candidate_id, extraction_batch_id, firm_id, matter_id
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (promoted_by, firm_id) REFERENCES users(user_id, firm_id)
);

-- This receipt represents one explicit lead-lawyer action over the visible
-- low-risk group.  It never includes EXCEPTION_REVIEW records and is separate
-- from the immutable model/Worker staging records.
CREATE TABLE case_agent_ledger_extraction_batch_confirmations (
    extraction_batch_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    lawyer_batch_decision_hash char(64) NOT NULL CHECK (lawyer_batch_decision_hash ~ '^[0-9a-f]{64}$'),
    confirmed_candidate_count integer NOT NULL CHECK (confirmed_candidate_count BETWEEN 1 AND 500),
    confirmed_matter_version integer NOT NULL CHECK (confirmed_matter_version > 0),
    confirmed_by uuid NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (extraction_batch_id, firm_id, matter_id),
    FOREIGN KEY (extraction_batch_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_extraction_batches(extraction_batch_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

-- A normal foreign key cannot model the typed target above.  A trigger is the
-- PostgreSQL-safe equivalent: it binds the target's type, tenant, matter,
-- origin and still-candidate status in the same insert transaction.  A
-- subquery-based CHECK is not valid PostgreSQL here and would not make the
-- polymorphic reference safe.
CREATE FUNCTION case_agent_ledger_extraction_target_matches_candidate(
    input_candidate_id uuid,
    input_batch_id uuid,
    input_firm_id uuid,
    input_matter_id uuid,
    input_target_type text,
    input_target_id uuid
)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE candidate_kind text;
DECLARE candidate_payload jsonb;
DECLARE expected_evidence_links jsonb;
DECLARE expected_page_count integer;
BEGIN
    SELECT candidate.candidate_kind, candidate.candidate_payload
      INTO candidate_kind, candidate_payload
      FROM case_agent_ledger_extraction_candidates candidate
     WHERE candidate.extraction_candidate_id = input_candidate_id
       AND candidate.extraction_batch_id = input_batch_id
       AND candidate.firm_id = input_firm_id
       AND candidate.matter_id = input_matter_id;
    IF NOT FOUND
       OR candidate_kind IS DISTINCT FROM input_target_type
       OR candidate_payload->>'kind' IS DISTINCT FROM candidate_kind
       OR jsonb_typeof(candidate_payload->'evidence_page_ids') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;

    SELECT jsonb_agg(
               jsonb_build_object(
                   'evidence_id', page.evidence_page_id::text,
                   'original_file_sha256', source.original_file_sha256,
                   'page_number', page.page_number,
                   'region_id', NULL,
                   'original_label', source.original_label
               ) ORDER BY page.page_number, page.evidence_page_id
           ), count(*)::integer
      INTO expected_evidence_links, expected_page_count
      FROM case_agent_ledger_extraction_candidate_pages candidate_page
      JOIN evidence_pages page
        ON page.evidence_page_id = candidate_page.evidence_page_id
       AND page.firm_id = candidate_page.firm_id
       AND page.matter_id = candidate_page.matter_id
      JOIN evidence_original_files source
        ON source.evidence_file_id = page.evidence_file_id
       AND source.firm_id = page.firm_id
       AND source.matter_id = page.matter_id
     WHERE candidate_page.extraction_candidate_id = input_candidate_id
       AND candidate_page.firm_id = input_firm_id
       AND candidate_page.matter_id = input_matter_id;
    IF expected_evidence_links IS NULL
       OR expected_page_count IS DISTINCT FROM jsonb_array_length(
            candidate_payload->'evidence_page_ids'
       )
       OR EXISTS (
            SELECT 1
              FROM jsonb_array_elements_text(
                    candidate_payload->'evidence_page_ids'
              ) expected_page(evidence_page_id)
             WHERE NOT EXISTS (
                    SELECT 1
                      FROM case_agent_ledger_extraction_candidate_pages candidate_page
                     WHERE candidate_page.extraction_candidate_id = input_candidate_id
                       AND candidate_page.evidence_page_id::text = expected_page.evidence_page_id
                       AND candidate_page.firm_id = input_firm_id
                       AND candidate_page.matter_id = input_matter_id
             )
       ) THEN
        RETURN false;
    END IF;

    IF input_target_type = 'FACT' THEN
        RETURN EXISTS (
            SELECT 1 FROM case_facts target
            WHERE target.fact_id = input_target_id
              AND target.firm_id = input_firm_id
              AND target.matter_id = input_matter_id
              AND target.origin = 'AGENT_CANDIDATE'
              AND target.status IN ('CANDIDATE', 'CONFIRMED')
              AND target.original_text = candidate_payload->>'fact_text'
              AND target.evidence_links = expected_evidence_links
        );
    ELSIF input_target_type = 'TRANSACTION' THEN
        RETURN EXISTS (
            SELECT 1 FROM case_transactions target
            WHERE target.transaction_id = input_target_id
              AND target.firm_id = input_firm_id
              AND target.matter_id = input_matter_id
              AND target.status IN ('CANDIDATE', 'CONFIRMED')
              AND target.local_date IS NOT DISTINCT FROM
                    (candidate_payload->>'local_date')::date
              AND target.date_precision = candidate_payload->>'date_precision'
              AND target.amount = (candidate_payload->>'amount')::numeric
              AND target.currency = candidate_payload->>'currency'
              AND target.direction = candidate_payload->>'direction'
              AND target.payer_label IS NOT DISTINCT FROM
                    candidate_payload->>'payer_label'
              AND target.payee_label IS NOT DISTINCT FROM
                    candidate_payload->>'payee_label'
              AND target.channel = candidate_payload->>'channel'
              AND target.transaction_reference IS NOT DISTINCT FROM
                    candidate_payload->>'transaction_reference'
              AND target.evidence_links = expected_evidence_links
        );
    END IF;
    RETURN false;
END;
$$;

CREATE FUNCTION enforce_case_agent_ledger_extraction_promotion_target()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT case_agent_ledger_extraction_target_matches_candidate(
        NEW.extraction_candidate_id,
        NEW.extraction_batch_id,
        NEW.firm_id,
        NEW.matter_id,
        NEW.target_object_type,
        NEW.target_object_id
    ) THEN
        RAISE EXCEPTION 'promotion target differs from its extraction candidate';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_ledger_extraction_promotions_target_integrity
    BEFORE INSERT ON case_agent_ledger_extraction_promotions
    FOR EACH ROW EXECUTE FUNCTION enforce_case_agent_ledger_extraction_promotion_target();

-- The target may be confirmed by the one explicit low-risk batch command.
-- This deferred constraint runs at commit, after that command has inserted all
-- mappings and its single batch receipt, and proves neither SQL caller can
-- forge a confirmed target/mapping combination.
CREATE FUNCTION enforce_case_agent_ledger_extraction_confirmation_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_status text;
DECLARE target_hash char(64);
DECLARE target_by uuid;
DECLARE receipt_hash char(64);
DECLARE receipt_by uuid;
DECLARE receipt_count integer;
DECLARE receipt_version integer;
DECLARE eligible_count integer;
DECLARE candidate_is_eligible boolean;
BEGIN
    -- Repeat the complete content/evidence binding at commit.  A caller with
    -- SQL write rights cannot insert a valid mapping and then rewrite its
    -- target later in the same transaction.
    IF NOT case_agent_ledger_extraction_target_matches_candidate(
        NEW.extraction_candidate_id,
        NEW.extraction_batch_id,
        NEW.firm_id,
        NEW.matter_id,
        NEW.target_object_type,
        NEW.target_object_id
    ) THEN
        RAISE EXCEPTION 'extraction promotion target changed before commit';
    END IF;
    IF NEW.target_object_type = 'FACT' THEN
        SELECT status, decision_hash, decided_by
          INTO target_status, target_hash, target_by
          FROM case_facts WHERE fact_id = NEW.target_object_id
           AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    ELSE
        SELECT status, confirmation_hash, confirmed_by
          INTO target_status, target_hash, target_by
          FROM case_transactions WHERE transaction_id = NEW.target_object_id
           AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    END IF;
    IF target_status = 'CANDIDATE' THEN
        RETURN NEW;
    END IF;
    SELECT eligible_for_bulk_promotion INTO candidate_is_eligible
      FROM case_agent_ledger_extraction_candidates
     WHERE extraction_candidate_id = NEW.extraction_candidate_id
       AND extraction_batch_id = NEW.extraction_batch_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF target_status IS DISTINCT FROM 'CONFIRMED'
       OR candidate_is_eligible IS DISTINCT FROM true
       OR target_hash IS DISTINCT FROM NEW.lawyer_batch_decision_hash
       OR target_by IS DISTINCT FROM NEW.promoted_by THEN
        RAISE EXCEPTION 'confirmed extraction target does not bind batch decision';
    END IF;
    SELECT lawyer_batch_decision_hash, confirmed_by, confirmed_candidate_count,
           confirmed_matter_version
      INTO receipt_hash, receipt_by, receipt_count, receipt_version
      FROM case_agent_ledger_extraction_batch_confirmations
     WHERE extraction_batch_id = NEW.extraction_batch_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    SELECT eligible_candidate_count INTO eligible_count
      FROM case_agent_ledger_extraction_batches
     WHERE extraction_batch_id = NEW.extraction_batch_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF receipt_hash IS DISTINCT FROM NEW.lawyer_batch_decision_hash
       OR receipt_by IS DISTINCT FROM NEW.promoted_by
       OR receipt_count IS DISTINCT FROM eligible_count
       OR receipt_version IS DISTINCT FROM NEW.promoted_matter_version THEN
        RAISE EXCEPTION 'confirmed extraction batch receipt differs';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER case_agent_ledger_extraction_confirmed_target_integrity
    AFTER INSERT ON case_agent_ledger_extraction_promotions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_case_agent_ledger_extraction_confirmation_integrity();

-- A receipt is valid only when every eligible candidate, and no exception
-- candidate, has exactly one immutable promotion.  The per-promotion trigger
-- above cannot by itself detect an omitted eligible row, so the batch receipt
-- has its own deferred completeness constraint.
CREATE FUNCTION enforce_case_agent_ledger_extraction_batch_confirmation_completeness()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE eligible_count integer;
DECLARE promotion_count integer;
BEGIN
    SELECT eligible_candidate_count INTO eligible_count
      FROM case_agent_ledger_extraction_batches
     WHERE extraction_batch_id = NEW.extraction_batch_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    SELECT count(*) INTO promotion_count
      FROM case_agent_ledger_extraction_promotions
     WHERE extraction_batch_id = NEW.extraction_batch_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF eligible_count IS DISTINCT FROM NEW.confirmed_candidate_count
       OR promotion_count IS DISTINCT FROM NEW.confirmed_candidate_count
       OR EXISTS (
            SELECT 1
              FROM case_agent_ledger_extraction_candidates candidate
             WHERE candidate.extraction_batch_id = NEW.extraction_batch_id
               AND candidate.firm_id = NEW.firm_id
               AND candidate.matter_id = NEW.matter_id
               AND candidate.eligible_for_bulk_promotion
               AND NOT EXISTS (
                    SELECT 1
                      FROM case_agent_ledger_extraction_promotions promotion
                     WHERE promotion.extraction_candidate_id = candidate.extraction_candidate_id
                       AND promotion.extraction_batch_id = candidate.extraction_batch_id
                       AND promotion.firm_id = candidate.firm_id
                       AND promotion.matter_id = candidate.matter_id
               )
       )
       OR EXISTS (
            SELECT 1
              FROM case_agent_ledger_extraction_promotions promotion
              JOIN case_agent_ledger_extraction_candidates candidate
                ON candidate.extraction_candidate_id = promotion.extraction_candidate_id
               AND candidate.extraction_batch_id = promotion.extraction_batch_id
               AND candidate.firm_id = promotion.firm_id
               AND candidate.matter_id = promotion.matter_id
             WHERE promotion.extraction_batch_id = NEW.extraction_batch_id
               AND promotion.firm_id = NEW.firm_id
               AND promotion.matter_id = NEW.matter_id
               AND NOT candidate.eligible_for_bulk_promotion
       )
       OR EXISTS (
            SELECT 1
              FROM case_agent_ledger_extraction_promotions promotion
              LEFT JOIN case_facts fact
                ON promotion.target_object_type = 'FACT'
               AND fact.fact_id = promotion.target_object_id
               AND fact.firm_id = promotion.firm_id
               AND fact.matter_id = promotion.matter_id
              LEFT JOIN case_transactions transaction_row
                ON promotion.target_object_type = 'TRANSACTION'
               AND transaction_row.transaction_id = promotion.target_object_id
               AND transaction_row.firm_id = promotion.firm_id
               AND transaction_row.matter_id = promotion.matter_id
             WHERE promotion.extraction_batch_id = NEW.extraction_batch_id
               AND promotion.firm_id = NEW.firm_id
               AND promotion.matter_id = NEW.matter_id
               AND (
                    promotion.lawyer_batch_decision_hash IS DISTINCT FROM
                        NEW.lawyer_batch_decision_hash
                    OR promotion.promoted_by IS DISTINCT FROM NEW.confirmed_by
                    OR promotion.promoted_matter_version IS DISTINCT FROM
                        NEW.confirmed_matter_version
                    OR (
                        promotion.target_object_type = 'FACT'
                        AND (
                            fact.status IS DISTINCT FROM 'CONFIRMED'
                            OR fact.decision_hash IS DISTINCT FROM
                                NEW.lawyer_batch_decision_hash
                            OR fact.decided_by IS DISTINCT FROM NEW.confirmed_by
                        )
                    )
                    OR (
                        promotion.target_object_type = 'TRANSACTION'
                        AND (
                            transaction_row.status IS DISTINCT FROM 'CONFIRMED'
                            OR transaction_row.confirmation_hash IS DISTINCT FROM
                                NEW.lawyer_batch_decision_hash
                            OR transaction_row.confirmed_by IS DISTINCT FROM NEW.confirmed_by
                        )
                    )
                    OR promotion.target_object_type NOT IN ('FACT', 'TRANSACTION')
               )
       ) THEN
        RAISE EXCEPTION 'confirmed extraction batch is incomplete or includes exception candidates';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER case_agent_ledger_extraction_batch_confirmation_complete
    AFTER INSERT ON case_agent_ledger_extraction_batch_confirmations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_case_agent_ledger_extraction_batch_confirmation_completeness();

CREATE INDEX case_agent_ledger_extraction_candidates_matter_idx
    ON case_agent_ledger_extraction_candidates (
        firm_id, matter_id, created_at, extraction_candidate_id
    );
CREATE INDEX case_agent_ledger_extraction_batches_run_task_idx
    ON case_agent_ledger_extraction_batches (run_id, graph_id, task_id);
CREATE INDEX case_agent_ledger_extraction_batches_verification_idx
    ON case_agent_ledger_extraction_batches (verification_receipt_id);
CREATE INDEX case_agent_ledger_extraction_staging_events_run_idx
    ON case_agent_ledger_extraction_staging_events (
        run_id, artifact_id, firm_id, matter_id
    );
CREATE INDEX case_agent_ledger_extraction_staging_events_actor_idx
    ON case_agent_ledger_extraction_staging_events (actor_id, firm_id);
CREATE INDEX case_agent_ledger_extraction_candidate_pages_evidence_idx
    ON case_agent_ledger_extraction_candidate_pages (
        evidence_page_id, firm_id, matter_id
    );
CREATE INDEX case_agent_ledger_extraction_promotions_batch_idx
    ON case_agent_ledger_extraction_promotions (
        extraction_batch_id, firm_id, matter_id
    );
CREATE INDEX case_agent_ledger_extraction_promotions_target_idx
    ON case_agent_ledger_extraction_promotions (
        target_object_type, target_object_id, firm_id, matter_id
    );

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_ledger_extraction_batches',
        'case_agent_ledger_extraction_staging_events',
        'case_agent_ledger_extraction_candidates',
        'case_agent_ledger_extraction_candidate_pages',
        'case_agent_ledger_extraction_promotions',
        'case_agent_ledger_extraction_batch_confirmations'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (firm_id::text = current_setting(''app.firm_id'', true)) WITH CHECK (firm_id::text = current_setting(''app.firm_id'', true))',
            table_name || '_firm_isolation', table_name
        );
    END LOOP;
END;
$$;

CREATE FUNCTION prohibit_case_agent_ledger_extraction_staging_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent ledger extraction staging is append-only';
END;
$$;

CREATE TRIGGER case_agent_ledger_extraction_batches_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_extraction_batches
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_extraction_staging_mutation();
CREATE TRIGGER case_agent_ledger_extraction_staging_events_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_extraction_staging_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_extraction_staging_mutation();
CREATE TRIGGER case_agent_ledger_extraction_candidates_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_extraction_candidates
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_extraction_staging_mutation();
CREATE TRIGGER case_agent_ledger_extraction_candidate_pages_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_extraction_candidate_pages
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_extraction_staging_mutation();
CREATE TRIGGER case_agent_ledger_extraction_promotions_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_extraction_promotions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_extraction_staging_mutation();
CREATE TRIGGER case_agent_ledger_extraction_batch_confirmations_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_extraction_batch_confirmations
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_extraction_staging_mutation();

REVOKE ALL ON TABLE case_agent_ledger_extraction_batches FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_extraction_staging_events FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_extraction_candidates FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_extraction_candidate_pages FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_extraction_promotions FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_extraction_batch_confirmations FROM PUBLIC;
REVOKE ALL ON FUNCTION case_agent_ledger_extraction_target_matches_candidate(
    uuid, uuid, uuid, uuid, text, uuid
) FROM PUBLIC;
REVOKE ALL ON FUNCTION enforce_case_agent_ledger_extraction_promotion_target() FROM PUBLIC;
REVOKE ALL ON FUNCTION enforce_case_agent_ledger_extraction_confirmation_integrity() FROM PUBLIC;
REVOKE ALL ON FUNCTION enforce_case_agent_ledger_extraction_batch_confirmation_completeness() FROM PUBLIC;

COMMIT;
