-- Dynamic, source-bound case work plans and submission-plan bindings.
--
-- No procedural role maps to a fixed checklist here.  A plan is a versioned
-- candidate derived from the current posture, claim/procedure facts and
-- reviewed legal authority.  Only its exact lawyer-confirmed projection may
-- define required court documents.

BEGIN;

CREATE TABLE case_work_plans (
    plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    plan_version bigint NOT NULL CHECK (plan_version > 0),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'ACTIVE', 'STALE', 'SUPERSEDED')),
    planned_matter_version integer NOT NULL CHECK (planned_matter_version > 0),
    profile_id uuid NOT NULL,
    profile_version bigint NOT NULL CHECK (profile_version > 0),
    profile_hash char(64) NOT NULL CHECK (profile_hash ~ '^[0-9a-f]{64}$'),
    objective_approval_id uuid NOT NULL,
    objective_hash char(64) NOT NULL CHECK (objective_hash ~ '^[0-9a-f]{64}$'),
    claim_scope_hash char(64) NOT NULL CHECK (claim_scope_hash ~ '^[0-9a-f]{64}$'),
    procedure_context_hash char(64) NOT NULL CHECK (procedure_context_hash ~ '^[0-9a-f]{64}$'),
    legal_context_hash char(64) NOT NULL CHECK (legal_context_hash ~ '^[0-9a-f]{64}$'),
    context_hash char(64) NOT NULL CHECK (context_hash ~ '^[0-9a-f]{64}$'),
    candidate_input_hash char(64) NOT NULL CHECK (candidate_input_hash ~ '^[0-9a-f]{64}$'),
    plan_hash char(64) NOT NULL CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    agent_id text NOT NULL CHECK (length(trim(agent_id)) BETWEEN 1 AND 200),
    agent_version text NOT NULL CHECK (length(trim(agent_version)) BETWEEN 1 AND 100),
    generated_at timestamptz NOT NULL,
    required_court_document_kinds jsonb NOT NULL CHECK (
        jsonb_typeof(required_court_document_kinds) = 'array'
    ),
    primary_court_document_kind text CHECK (
        primary_court_document_kind IS NULL
        OR primary_court_document_kind ~ '^[A-Z][A-Z0-9_]{1,119}$'
    ),
    supersedes_plan_id uuid,
    registered_by uuid NOT NULL,
    confirmed_by uuid,
    confirmation_hash char(64),
    confirmed_at timestamptz,
    activated_matter_version integer CHECK (activated_matter_version > 0),
    stale_reason_code text CHECK (
        stale_reason_code IS NULL OR stale_reason_code ~ '^[A-Z][A-Z0-9_]{1,119}$'
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (matter_id, plan_version),
    UNIQUE (plan_id, firm_id, matter_id),
    UNIQUE (matter_id, plan_hash),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (profile_id, firm_id, matter_id)
        REFERENCES case_posture_profiles(profile_id, firm_id, matter_id),
    FOREIGN KEY (objective_approval_id, firm_id, matter_id)
        REFERENCES approvals(approval_id, firm_id, matter_id),
    FOREIGN KEY (supersedes_plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id),
    FOREIGN KEY (registered_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (status = 'CANDIDATE' AND confirmed_by IS NULL AND confirmation_hash IS NULL
            AND confirmed_at IS NULL AND activated_matter_version IS NULL
            AND stale_reason_code IS NULL)
        OR (status = 'ACTIVE' AND confirmed_by IS NOT NULL AND confirmation_hash = plan_hash
            AND confirmed_at IS NOT NULL AND activated_matter_version IS NOT NULL
            AND stale_reason_code IS NULL)
        OR (status IN ('STALE', 'SUPERSEDED') AND confirmed_by IS NOT NULL
            AND confirmation_hash = plan_hash AND confirmed_at IS NOT NULL
            AND activated_matter_version IS NOT NULL AND stale_reason_code IS NOT NULL)
    )
);

CREATE TABLE case_work_plan_items (
    item_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 200),
    item_kind text NOT NULL CHECK (item_kind IN (
        'MATERIAL_REQUEST', 'RESEARCH_TASK', 'PROCEDURAL_TASK', 'CALCULATION',
        'DOCUMENT_CANDIDATE', 'REVIEW', 'DEADLINE_RISK'
    )),
    readiness text NOT NULL CHECK (readiness IN (
        'ACTIONABLE', 'NEEDS_RESEARCH', 'NEEDS_INFORMATION'
    )),
    title text NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 500),
    rationale text NOT NULL CHECK (length(trim(rationale)) BETWEEN 1 AND 4000),
    risk_if_omitted text NOT NULL CHECK (length(trim(risk_if_omitted)) BETWEEN 1 AND 2000),
    confidence numeric(6,5) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    review_gate text NOT NULL CHECK (review_gate IN (
        'LEAD_LAWYER_CONFIRMATION', 'EVIDENCE_REVIEW', 'LEGAL_AUTHORITY_REVIEW',
        'PROCEDURE_REVIEW', 'CALCULATION_REVIEW'
    )),
    delivery_target text NOT NULL CHECK (delivery_target IN (
        'NOT_APPLICABLE', 'INTERNAL_WORK_PRODUCT', 'CLIENT_DELIVERABLE', 'COURT_SUBMISSION'
    )),
    deliverable_kind text CHECK (
        deliverable_kind IS NULL OR deliverable_kind ~ '^[A-Z][A-Z0-9_]{1,119}$'
    ),
    required_for_delivery boolean NOT NULL,
    is_primary_document boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, item_id),
    UNIQUE (plan_id, sequence),
    UNIQUE (item_id, plan_id, firm_id, matter_id),
    FOREIGN KEY (plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id) ON DELETE CASCADE,
    CHECK (
        (item_kind = 'DOCUMENT_CANDIDATE' AND delivery_target <> 'NOT_APPLICABLE'
            AND deliverable_kind IS NOT NULL)
        OR (item_kind <> 'DOCUMENT_CANDIDATE' AND delivery_target = 'NOT_APPLICABLE'
            AND deliverable_kind IS NULL AND NOT required_for_delivery AND NOT is_primary_document)
    ),
    CHECK (NOT required_for_delivery OR (
        readiness = 'ACTIONABLE' AND delivery_target = 'COURT_SUBMISSION'
    )),
    CHECK (NOT is_primary_document OR required_for_delivery)
);

CREATE UNIQUE INDEX case_work_plan_required_document_kind_unique
    ON case_work_plan_items(plan_id, deliverable_kind)
    WHERE required_for_delivery;
CREATE UNIQUE INDEX case_work_plan_one_primary_document
    ON case_work_plan_items(plan_id) WHERE is_primary_document;

CREATE TABLE case_work_plan_context_references (
    plan_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    source_type text NOT NULL CHECK (source_type ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    source_id uuid NOT NULL,
    source_version text NOT NULL CHECK (length(trim(source_version)) BETWEEN 1 AND 200),
    source_hash char(64) NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    reference_use text NOT NULL CHECK (reference_use ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, source_type, source_id, source_version, source_hash, reference_use),
    FOREIGN KEY (plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id) ON DELETE CASCADE
);

CREATE TABLE case_work_plan_item_references (
    plan_id uuid NOT NULL,
    item_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    reference_role text NOT NULL CHECK (reference_role IN ('TRIGGER', 'SOURCE')),
    source_type text NOT NULL CHECK (source_type ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    source_id uuid NOT NULL,
    source_version text NOT NULL CHECK (length(trim(source_version)) BETWEEN 1 AND 200),
    source_hash char(64) NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    reference_use text NOT NULL CHECK (reference_use ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        plan_id, item_id, reference_role, source_type, source_id,
        source_version, source_hash, reference_use
    ),
    FOREIGN KEY (item_id, plan_id, firm_id, matter_id)
        REFERENCES case_work_plan_items(item_id, plan_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (plan_id, source_type, source_id, source_version, source_hash, reference_use)
        REFERENCES case_work_plan_context_references(
            plan_id, source_type, source_id, source_version, source_hash, reference_use
        )
);

CREATE TABLE case_work_plan_item_prerequisites (
    plan_id uuid NOT NULL,
    item_id uuid NOT NULL,
    prerequisite_item_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, item_id, prerequisite_item_id),
    FOREIGN KEY (item_id, plan_id, firm_id, matter_id)
        REFERENCES case_work_plan_items(item_id, plan_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (prerequisite_item_id, plan_id, firm_id, matter_id)
        REFERENCES case_work_plan_items(item_id, plan_id, firm_id, matter_id),
    CHECK (item_id <> prerequisite_item_id)
);

CREATE TABLE case_work_plan_heads (
    matter_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    latest_plan_version bigint NOT NULL CHECK (latest_plan_version > 0),
    current_plan_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (matter_id, firm_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (current_plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id)
);

CREATE TABLE case_work_plan_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    event_sequence integer NOT NULL CHECK (event_sequence > 0),
    event_type text NOT NULL CHECK (event_type IN (
        'CANDIDATE_REGISTERED', 'PLAN_ACTIVATED', 'PLAN_STALE', 'PLAN_SUPERSEDED'
    )),
    effective_status text NOT NULL CHECK (effective_status IN (
        'CANDIDATE', 'ACTIVE', 'STALE', 'SUPERSEDED'
    )),
    actor_id uuid NOT NULL,
    cause_hash char(64) NOT NULL CHECK (cause_hash ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, event_sequence),
    UNIQUE (plan_id, effective_status),
    FOREIGN KEY (plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE UNIQUE INDEX case_work_plans_one_active_per_matter
    ON case_work_plans(matter_id) WHERE status = 'ACTIVE';
CREATE INDEX case_work_plans_matter_idx
    ON case_work_plans(matter_id, plan_version DESC);
CREATE INDEX case_work_plan_items_plan_idx
    ON case_work_plan_items(plan_id, sequence);
CREATE INDEX case_work_plan_refs_source_idx
    ON case_work_plan_context_references(matter_id, source_type, source_id);

ALTER TABLE submission_compilation_specs
    ADD COLUMN work_plan_id uuid,
    ADD COLUMN work_plan_hash char(64),
    ADD COLUMN posture_profile_id uuid,
    ADD COLUMN posture_profile_hash char(64);

ALTER TABLE submission_compilation_specs
    ADD CONSTRAINT submission_compilation_specs_work_plan_binding_valid CHECK (
        (work_plan_id IS NULL AND work_plan_hash IS NULL
            AND posture_profile_id IS NULL AND posture_profile_hash IS NULL)
        OR (work_plan_id IS NOT NULL AND work_plan_hash ~ '^[0-9a-f]{64}$'
            AND posture_profile_id IS NOT NULL AND posture_profile_hash ~ '^[0-9a-f]{64}$')
    ),
    ADD CONSTRAINT submission_compilation_specs_work_plan_fk
        FOREIGN KEY (work_plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id),
    ADD CONSTRAINT submission_compilation_specs_posture_profile_fk
        FOREIGN KEY (posture_profile_id, firm_id, matter_id)
        REFERENCES case_posture_profiles(profile_id, firm_id, matter_id);

ALTER TABLE case_work_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plans FORCE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_items FORCE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_context_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_context_references FORCE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_item_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_item_references FORCE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_item_prerequisites ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_item_prerequisites FORCE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_work_plan_events FORCE ROW LEVEL SECURITY;

CREATE POLICY case_work_plans_firm_isolation ON case_work_plans
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_work_plan_items_firm_isolation ON case_work_plan_items
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_work_plan_context_refs_firm_isolation ON case_work_plan_context_references
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_work_plan_item_refs_firm_isolation ON case_work_plan_item_references
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_work_plan_prerequisites_firm_isolation ON case_work_plan_item_prerequisites
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_work_plan_heads_firm_isolation ON case_work_plan_heads
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_work_plan_events_firm_isolation ON case_work_plan_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION guard_case_work_plan_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR OLD.status NOT IN ('CANDIDATE', 'ACTIVE')
       OR NOT ((OLD.status = 'CANDIDATE' AND NEW.status = 'ACTIVE')
            OR (OLD.status = 'ACTIVE' AND NEW.status IN ('STALE', 'SUPERSEDED')))
       OR (to_jsonb(NEW) - ARRAY[
            'status','confirmed_by','confirmation_hash','confirmed_at',
            'activated_matter_version','stale_reason_code','updated_at'
          ]) IS DISTINCT FROM
          (to_jsonb(OLD) - ARRAY[
            'status','confirmed_by','confirmation_hash','confirmed_at',
            'activated_matter_version','stale_reason_code','updated_at'
          ]) THEN
        RAISE EXCEPTION 'case work plan transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_work_plans_guard BEFORE UPDATE OR DELETE ON case_work_plans
    FOR EACH ROW EXECUTE FUNCTION guard_case_work_plan_mutation();

CREATE FUNCTION prohibit_case_work_plan_detail_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case work plan details and events are append-only';
END;
$$;
CREATE TRIGGER case_work_plan_items_append_only BEFORE UPDATE OR DELETE ON case_work_plan_items
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_work_plan_detail_mutation();
CREATE TRIGGER case_work_plan_context_refs_append_only BEFORE UPDATE OR DELETE ON case_work_plan_context_references
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_work_plan_detail_mutation();
CREATE TRIGGER case_work_plan_item_refs_append_only BEFORE UPDATE OR DELETE ON case_work_plan_item_references
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_work_plan_detail_mutation();
CREATE TRIGGER case_work_plan_prerequisites_append_only BEFORE UPDATE OR DELETE ON case_work_plan_item_prerequisites
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_work_plan_detail_mutation();
CREATE TRIGGER case_work_plan_events_append_only BEFORE UPDATE OR DELETE ON case_work_plan_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_work_plan_detail_mutation();

CREATE FUNCTION guard_case_work_plan_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    referenced_version bigint;
    referenced_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'case work plan head cannot be deleted';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        (to_jsonb(NEW) - ARRAY['latest_plan_version','current_plan_id','updated_at'])
            IS DISTINCT FROM
        (to_jsonb(OLD) - ARRAY['latest_plan_version','current_plan_id','updated_at'])
        OR NEW.latest_plan_version < OLD.latest_plan_version
    ) THEN
        RAISE EXCEPTION 'case work plan head transition is invalid';
    END IF;
    IF NEW.current_plan_id IS NOT NULL THEN
        SELECT plan_version, status INTO referenced_version, referenced_status
        FROM case_work_plans WHERE plan_id = NEW.current_plan_id;
        IF referenced_status <> 'ACTIVE' OR referenced_version > NEW.latest_plan_version THEN
            RAISE EXCEPTION 'current work plan head must reference an active known version';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_work_plan_heads_guard BEFORE INSERT OR UPDATE OR DELETE ON case_work_plan_heads
    FOR EACH ROW EXECUTE FUNCTION guard_case_work_plan_head();

COMMIT;
