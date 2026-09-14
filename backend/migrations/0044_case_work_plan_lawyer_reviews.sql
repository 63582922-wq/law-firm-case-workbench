-- Append-only lawyer review signals for immutable dynamic work-plan items.
--
-- A review never mutates the 0030 candidate and never advances the matter
-- version.  APPROVE is evidence that counsel inspected one exact item.
-- REQUEST_CHANGE or REJECT permanently blocks that candidate from activation;
-- a Worker must promote a new independently verified graph instead of
-- pretending that the immutable candidate was edited in place.

BEGIN;

-- 0001 assumed every audit event mutates the matter aggregate.  Item review
-- is intentionally non-mutating, so permit equality for this one allowlisted
-- event while preserving the strict version increase for every other event.
ALTER TABLE audit_events
    ADD CONSTRAINT audit_events_version_transition_valid CHECK (
        output_version > input_version
        OR (
            event_type = 'CASE_WORK_PLAN_ITEM_REVIEWED'
            AND output_version = input_version
        )
    ) NOT VALID;
ALTER TABLE audit_events
    VALIDATE CONSTRAINT audit_events_version_transition_valid;
ALTER TABLE audit_events
    DROP CONSTRAINT audit_events_output_version_check;

CREATE TABLE case_work_plan_item_reviews (
    review_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL,
    item_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    plan_hash char(64) NOT NULL CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    decision text NOT NULL CHECK (
        decision IN ('APPROVE', 'REQUEST_CHANGE', 'REJECT')
    ),
    reason_code text NOT NULL CHECK (reason_code IN (
        'VERIFIED_BY_COUNSEL', 'NOT_APPLICABLE',
        'SUPERSEDED_BY_EVIDENCE', 'REQUIRES_FURTHER_RESEARCH',
        'PROCEDURAL_POSTURE_CHANGED', 'INCORRECT_SOURCE_BINDING'
    )),
    readiness_override text CHECK (
        readiness_override IS NULL OR readiness_override IN (
            'ACTIONABLE', 'NEEDS_RESEARCH', 'NEEDS_INFORMATION'
        )
    ),
    required_for_delivery_override boolean,
    reviewed_by uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (
        length(idempotency_key) BETWEEN 16 AND 128
        AND idempotency_key ~ '^[A-Za-z0-9._~-]+$'
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    reviewed_matter_version integer NOT NULL CHECK (reviewed_matter_version > 0),
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, item_id),
    UNIQUE (firm_id, matter_id, reviewed_by, idempotency_key),
    UNIQUE (review_id, firm_id, matter_id),
    FOREIGN KEY (plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id),
    FOREIGN KEY (item_id, plan_id, firm_id, matter_id)
        REFERENCES case_work_plan_items(item_id, plan_id, firm_id, matter_id),
    FOREIGN KEY (reviewed_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (decision = 'REQUEST_CHANGE' AND (
            readiness_override IS NOT NULL
            OR required_for_delivery_override IS NOT NULL
        ))
        OR (decision <> 'REQUEST_CHANGE'
            AND readiness_override IS NULL
            AND required_for_delivery_override IS NULL)
    )
);

CREATE INDEX case_work_plan_item_reviews_matter_idx
    ON case_work_plan_item_reviews (firm_id, matter_id, reviewed_at DESC);
CREATE INDEX case_work_plan_item_reviews_item_fk_idx
    ON case_work_plan_item_reviews (item_id, plan_id, firm_id, matter_id);

CREATE FUNCTION validate_case_work_plan_item_review()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM case_work_plans plan
        JOIN case_work_plan_heads head
          ON head.matter_id = plan.matter_id AND head.firm_id = plan.firm_id
        JOIN matters matter
          ON matter.matter_id = plan.matter_id AND matter.firm_id = plan.firm_id
        WHERE plan.plan_id = NEW.plan_id
          AND plan.firm_id = NEW.firm_id AND plan.matter_id = NEW.matter_id
          AND plan.status = 'CANDIDATE'
          AND plan.plan_hash = NEW.plan_hash
          AND plan.plan_version = head.latest_plan_version
          AND plan.planned_matter_version + 1 = matter.version
    ) THEN
        RAISE EXCEPTION 'work plan item review requires the exact current candidate';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER case_work_plan_item_review_valid
    AFTER INSERT ON case_work_plan_item_reviews
    DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW EXECUTE FUNCTION validate_case_work_plan_item_review();

CREATE FUNCTION block_adversely_reviewed_case_work_plan_activation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'CANDIDATE' AND NEW.status = 'ACTIVE' AND EXISTS (
        SELECT 1
        FROM case_work_plan_item_reviews review
        WHERE review.plan_id = NEW.plan_id
          AND review.firm_id = NEW.firm_id
          AND review.matter_id = NEW.matter_id
          AND review.decision IN ('REQUEST_CHANGE', 'REJECT')
    ) THEN
        RAISE EXCEPTION 'work plan has a lawyer change or rejection request';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_work_plan_adverse_review_activation_block
    BEFORE UPDATE ON case_work_plans
    FOR EACH ROW EXECUTE FUNCTION block_adversely_reviewed_case_work_plan_activation();

CREATE FUNCTION prohibit_case_work_plan_item_review_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case work plan item reviews are append-only';
END;
$$;

CREATE TRIGGER case_work_plan_item_reviews_append_only
    BEFORE UPDATE OR DELETE ON case_work_plan_item_reviews
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_work_plan_item_review_mutation();

ALTER TABLE case_work_plan_item_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_item_reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY case_work_plan_item_reviews_firm_isolation
    ON case_work_plan_item_reviews
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE case_work_plan_item_reviews FROM PUBLIC;

COMMIT;
