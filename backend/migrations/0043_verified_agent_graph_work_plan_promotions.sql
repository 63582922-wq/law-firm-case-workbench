-- Promote an independently verified Agent graph into a review-only 0030 plan.
--
-- The browser/model supplies neither graph hashes nor candidate items.  One
-- SYSTEM_WORKER transaction re-reads the current graph, PASSED receipt,
-- authoritative planning projection and current posture, then appends a
-- CANDIDATE.  Lead-lawyer activation remains a separate 0030 command.

BEGIN;

-- ``purpose`` becomes part of the canonical plan hash in this release.  A
-- purpose inferred after the fact from a legacy title/rationale would not be
-- covered by the already persisted plan_hash.  Refuse an in-place semantic
-- upgrade instead of making an old CANDIDATE/ACTIVE plan appear trustworthy.
-- Installations with pre-0043 plans need an explicit, audited invalidation and
-- re-planning migration before applying this release.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM case_work_plans LIMIT 1) THEN
        RAISE EXCEPTION USING
            MESSAGE = '0043 requires an empty case_work_plans table',
            DETAIL = 'Legacy plan hashes do not cover item purpose.',
            HINT = 'Invalidate legacy plans with an audited migration, then regenerate them.';
    END IF;
END;
$$;

ALTER TABLE case_work_plan_items
    ADD COLUMN purpose text NOT NULL;
ALTER TABLE case_work_plan_items
    ADD CONSTRAINT case_work_plan_items_purpose_valid CHECK (
        length(trim(purpose)) BETWEEN 1 AND 2000
    );

ALTER TABLE case_work_plans
    ALTER COLUMN objective_approval_id DROP NOT NULL,
    ADD COLUMN agent_goal_id uuid;
ALTER TABLE case_work_plans
    ADD CONSTRAINT case_work_plans_exact_objective_source CHECK (
        (objective_approval_id IS NOT NULL AND agent_goal_id IS NULL)
        OR (objective_approval_id IS NULL AND agent_goal_id IS NOT NULL)
    ),
    ADD CONSTRAINT case_work_plans_agent_goal_fk
        FOREIGN KEY (agent_goal_id, firm_id, matter_id)
        REFERENCES case_agent_goals(goal_id, firm_id, matter_id);

CREATE TABLE case_agent_work_plan_promotions (
    promotion_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL UNIQUE,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    graph_version bigint NOT NULL CHECK (graph_version > 0),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    snapshot_matter_version integer NOT NULL CHECK (snapshot_matter_version > 0),
    snapshot_schema_version text NOT NULL CHECK (
        length(trim(snapshot_schema_version)) BETWEEN 1 AND 200
    ),
    snapshot_hash char(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    goal_id uuid NOT NULL,
    goal_hash char(64) NOT NULL CHECK (goal_hash ~ '^[0-9a-f]{64}$'),
    posture_profile_id uuid NOT NULL,
    posture_profile_version bigint NOT NULL CHECK (posture_profile_version > 0),
    posture_profile_hash char(64) NOT NULL CHECK (
        posture_profile_hash ~ '^[0-9a-f]{64}$'
    ),
    verification_receipt_id uuid NOT NULL UNIQUE,
    verification_hash char(64) NOT NULL CHECK (
        verification_hash ~ '^[0-9a-f]{64}$'
    ),
    verifier_actor_id uuid NOT NULL,
    execution_actor_id uuid NOT NULL,
    task_count integer NOT NULL CHECK (task_count BETWEEN 1 AND 200),
    promoted_by uuid NOT NULL,
    promoted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (promotion_id, firm_id, matter_id),
    UNIQUE (plan_id, promotion_id, firm_id, matter_id),
    FOREIGN KEY (plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_task_graphs(graph_id, run_id, firm_id, matter_id),
    FOREIGN KEY (goal_id, firm_id, matter_id)
        REFERENCES case_agent_goals(goal_id, firm_id, matter_id),
    FOREIGN KEY (verification_receipt_id)
        REFERENCES case_agent_verification_receipts(verification_receipt_id),
    FOREIGN KEY (posture_profile_id, firm_id, matter_id)
        REFERENCES case_posture_profiles(profile_id, firm_id, matter_id),
    FOREIGN KEY (verifier_actor_id, firm_id)
        REFERENCES users(user_id, firm_id),
    FOREIGN KEY (execution_actor_id, firm_id)
        REFERENCES users(user_id, firm_id),
    FOREIGN KEY (promoted_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (verifier_actor_id <> execution_actor_id)
);

CREATE TABLE case_agent_work_plan_input_bindings (
    binding_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    promotion_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    input_ref text NOT NULL CHECK (
        length(trim(input_ref)) BETWEEN 1 AND 200
        AND input_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
    ),
    object_type text NOT NULL CHECK (object_type IN (
        'MATERIAL_OBJECT', 'EVIDENCE_PAGE', 'CASE_FACT', 'CASE_CLAIM',
        'DISPUTE_ISSUE', 'CASE_TRANSACTION', 'POSTURE_PROFILE',
        'WORK_PLAN_ITEM', 'VERIFIED_LEGAL_SOURCE', 'PROCEDURAL_EVENT'
    )),
    object_id uuid NOT NULL,
    object_version text NOT NULL CHECK (
        length(trim(object_version)) BETWEEN 1 AND 200
    ),
    content_hash char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    source_status text NOT NULL CHECK (source_status IN (
        'AVAILABLE', 'CONFIRMED', 'REVIEW_REQUIRED', 'DISPUTED',
        'OPEN', 'BLOCKED', 'LOCKED'
    )),
    source_type text NOT NULL DEFAULT 'AGENT_TASK_INPUT'
        CHECK (source_type = 'AGENT_TASK_INPUT'),
    reference_use text NOT NULL CHECK (
        reference_use IN (
            'MATERIAL', 'EVIDENCE', 'FACT', 'CLAIM_SCOPE', 'TRANSACTION',
            'POSTURE', 'WORK_PLAN', 'LEGAL_AUTHORITY', 'COURT_EVENT'
        )
    ),
    binding_hash char(64) NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, binding_id),
    UNIQUE (promotion_id, input_ref),
    UNIQUE (binding_id, plan_id, firm_id, matter_id),
    FOREIGN KEY (plan_id, promotion_id, firm_id, matter_id)
        REFERENCES case_agent_work_plan_promotions(
            plan_id, promotion_id, firm_id, matter_id
        ) ON DELETE CASCADE,
    FOREIGN KEY (
        plan_id, source_type, binding_id, object_version,
        binding_hash, reference_use
    ) REFERENCES case_work_plan_context_references(
        plan_id, source_type, source_id, source_version,
        source_hash, reference_use
    )
);

CREATE INDEX case_agent_work_plan_promotions_matter_idx
    ON case_agent_work_plan_promotions (firm_id, matter_id, promoted_at DESC);
CREATE INDEX case_agent_work_plan_promotions_graph_idx
    ON case_agent_work_plan_promotions (run_id, graph_id);
CREATE INDEX case_work_plans_agent_goal_idx
    ON case_work_plans (agent_goal_id, firm_id, matter_id)
    WHERE agent_goal_id IS NOT NULL;
CREATE INDEX case_agent_work_plan_promotions_goal_idx
    ON case_agent_work_plan_promotions (goal_id, firm_id, matter_id);
CREATE INDEX case_agent_work_plan_promotions_posture_idx
    ON case_agent_work_plan_promotions (posture_profile_id, firm_id, matter_id);
CREATE INDEX case_agent_work_plan_promotions_verifier_actor_idx
    ON case_agent_work_plan_promotions (verifier_actor_id, firm_id);
CREATE INDEX case_agent_work_plan_promotions_execution_actor_idx
    ON case_agent_work_plan_promotions (execution_actor_id, firm_id);
CREATE INDEX case_agent_work_plan_promotions_promoted_by_idx
    ON case_agent_work_plan_promotions (promoted_by, firm_id);
CREATE INDEX case_agent_work_plan_input_bindings_object_idx
    ON case_agent_work_plan_input_bindings (
        firm_id, matter_id, object_type, object_id
    );

CREATE FUNCTION validate_case_agent_work_plan_promotion()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM case_work_plans plan
        JOIN case_agent_runs run
          ON run.run_id = NEW.run_id
         AND run.firm_id = NEW.firm_id AND run.matter_id = NEW.matter_id
        JOIN case_agent_task_graphs graph
          ON graph.graph_id = NEW.graph_id AND graph.run_id = NEW.run_id
         AND graph.firm_id = NEW.firm_id AND graph.matter_id = NEW.matter_id
        JOIN case_agent_verification_receipts receipt
          ON receipt.verification_receipt_id = NEW.verification_receipt_id
         AND receipt.run_id = NEW.run_id
         AND receipt.firm_id = NEW.firm_id AND receipt.matter_id = NEW.matter_id
        JOIN case_agent_goals goal
          ON goal.goal_id = NEW.goal_id
         AND goal.firm_id = NEW.firm_id AND goal.matter_id = NEW.matter_id
        JOIN case_posture_profiles posture
          ON posture.profile_id = NEW.posture_profile_id
         AND posture.firm_id = NEW.firm_id AND posture.matter_id = NEW.matter_id
        JOIN case_posture_profile_heads posture_head
          ON posture_head.current_profile_id = posture.profile_id
         AND posture_head.firm_id = posture.firm_id
         AND posture_head.matter_id = posture.matter_id
        WHERE plan.plan_id = NEW.plan_id
          AND plan.firm_id = NEW.firm_id AND plan.matter_id = NEW.matter_id
          AND plan.status = 'CANDIDATE'
          AND plan.required_court_document_kinds = '[]'::jsonb
          AND plan.primary_court_document_kind IS NULL
          AND plan.agent_goal_id = goal.goal_id
          AND plan.objective_approval_id IS NULL
          AND plan.objective_hash = goal.goal_hash
          AND plan.planned_matter_version = run.snapshot_matter_version
          AND plan.planned_matter_version = graph.snapshot_matter_version
          AND plan.profile_id = NEW.posture_profile_id
          AND plan.profile_version = NEW.posture_profile_version
          AND plan.profile_hash = NEW.posture_profile_hash
          AND run.goal_id = goal.goal_id
          AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
          AND NOT run.is_stale AND NOT run.is_cancelled
          AND run.current_graph_id = graph.graph_id
          AND run.current_graph_version = graph.graph_version
          AND run.current_graph_hash = graph.graph_hash
          AND run.snapshot_hash = graph.snapshot_hash
          AND run.snapshot_schema_version = graph.snapshot_schema_version
          AND run.verification_hash = receipt.verification_hash
          AND receipt.outcome = 'PASSED'
          AND receipt.graph_hash = graph.graph_hash
          AND receipt.snapshot_hash = graph.snapshot_hash
          AND receipt.verification_hash = NEW.verification_hash
          AND receipt.verifier_actor_id = NEW.verifier_actor_id
          AND receipt.execution_actor_id = NEW.execution_actor_id
          AND graph.graph_version = NEW.graph_version
          AND graph.graph_hash = NEW.graph_hash
          AND graph.snapshot_matter_version = NEW.snapshot_matter_version
          AND graph.snapshot_schema_version = NEW.snapshot_schema_version
          AND graph.snapshot_hash = NEW.snapshot_hash
          AND goal.goal_hash = NEW.goal_hash
          AND posture.status = 'CONFIRMED'
          AND posture.profile_version = NEW.posture_profile_version
          AND posture.profile_hash = NEW.posture_profile_hash
          AND NEW.task_count = (
              SELECT count(*)
              FROM case_agent_tasks task
              WHERE task.graph_id = NEW.graph_id AND task.run_id = NEW.run_id
                AND task.firm_id = NEW.firm_id AND task.matter_id = NEW.matter_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM case_agent_tasks task
              CROSS JOIN LATERAL jsonb_array_elements_text(task.input_refs) input_ref
              WHERE task.graph_id = NEW.graph_id AND task.run_id = NEW.run_id
                AND task.firm_id = NEW.firm_id AND task.matter_id = NEW.matter_id
                AND NOT EXISTS (
                    SELECT 1 FROM case_agent_work_plan_input_bindings binding
                    WHERE binding.plan_id = NEW.plan_id
                      AND binding.promotion_id = NEW.promotion_id
                      AND binding.firm_id = NEW.firm_id
                      AND binding.matter_id = NEW.matter_id
                      AND binding.input_ref = input_ref.value
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM case_agent_work_plan_input_bindings binding
              WHERE binding.plan_id = NEW.plan_id
                AND binding.promotion_id = NEW.promotion_id
                AND binding.firm_id = NEW.firm_id
                AND binding.matter_id = NEW.matter_id
                AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_tasks task
                    CROSS JOIN LATERAL jsonb_array_elements_text(task.input_refs) input_ref
                    WHERE task.graph_id = NEW.graph_id AND task.run_id = NEW.run_id
                      AND task.firm_id = NEW.firm_id
                      AND task.matter_id = NEW.matter_id
                      AND input_ref.value = binding.input_ref
                )
          )
    ) THEN
        RAISE EXCEPTION 'work plan promotion differs from its PASSED Agent graph';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER case_agent_work_plan_promotion_valid
    AFTER INSERT ON case_agent_work_plan_promotions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_work_plan_promotion();

CREATE FUNCTION require_agent_goal_work_plan_promotion()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.agent_goal_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM case_agent_work_plan_promotions promotion
        WHERE promotion.plan_id = NEW.plan_id
          AND promotion.firm_id = NEW.firm_id
          AND promotion.matter_id = NEW.matter_id
    ) THEN
        RAISE EXCEPTION 'Agent-goal work plan requires a verified graph promotion';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER case_work_plan_agent_goal_promotion_required
    AFTER INSERT ON case_work_plans
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION require_agent_goal_work_plan_promotion();

CREATE FUNCTION prohibit_case_agent_work_plan_promotion_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Agent work plan promotion provenance is append-only';
END;
$$;

CREATE TRIGGER case_agent_work_plan_promotions_append_only
    BEFORE UPDATE OR DELETE ON case_agent_work_plan_promotions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_work_plan_promotion_mutation();
CREATE TRIGGER case_agent_work_plan_input_bindings_append_only
    BEFORE UPDATE OR DELETE ON case_agent_work_plan_input_bindings
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_work_plan_promotion_mutation();

ALTER TABLE case_agent_work_plan_promotions ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_work_plan_promotions FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_work_plan_promotions_firm_isolation
    ON case_agent_work_plan_promotions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

ALTER TABLE case_agent_work_plan_input_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_work_plan_input_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_work_plan_input_bindings_firm_isolation
    ON case_agent_work_plan_input_bindings
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE case_agent_work_plan_promotions FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_work_plan_input_bindings FROM PUBLIC;

COMMIT;
