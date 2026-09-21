-- Immutable, review-only document packages produced by the dynamic case Agent.
--
-- One package binds the canonical structured candidate, its editable DOCX/XLSX
-- and the isolated PDF preview.  Private object locators are server-only.  A
-- row is not an approved pleading, a formal case-ledger fact or a court filing,
-- and this migration deliberately never advances matters.version.

BEGIN;

-- 0031 did not need graph_id in the attempt identity.  Reviewable document
-- packages bind all five task coordinates, so expose the already-existing
-- immutable tuple as a referenced key.
ALTER TABLE case_agent_task_attempts
    ADD CONSTRAINT case_agent_task_attempts_exact_graph_identity_unique
    UNIQUE (attempt_id, graph_id, task_id, run_id, firm_id, matter_id);

CREATE FUNCTION case_agent_document_source_refs_are_canonical(value jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT jsonb_typeof(value) = 'array'
       AND jsonb_array_length(value) BETWEEN 1 AND 400
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements_text(value) AS refs(ref)
           WHERE ref !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
       )
       AND value = COALESCE(
           (
               SELECT jsonb_agg(ref ORDER BY ref)
               FROM (
                   SELECT DISTINCT ref
                   FROM jsonb_array_elements_text(value) AS refs(ref)
               ) canonical
           ),
           '[]'::jsonb
       );
$$;

CREATE FUNCTION case_agent_document_source_refs_hash(value jsonb)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT encode(
        digest(
            convert_to(
                COALESCE((
                    SELECT string_agg(ref, E'\n' ORDER BY ordinal)
                    FROM jsonb_array_elements_text(value)
                         WITH ORDINALITY AS refs(ref, ordinal)
                ), ''),
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
$$;

CREATE FUNCTION case_agent_document_source_manifest_is_valid(value jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT jsonb_typeof(value) = 'array'
       AND jsonb_array_length(value) BETWEEN 1 AND 400
       AND jsonb_array_length(value) = (
           SELECT count(DISTINCT entry ->> 'input_ref')
           FROM jsonb_array_elements(value) AS sources(entry)
       )
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(value) AS sources(entry)
           WHERE jsonb_typeof(entry) <> 'object'
              OR ARRAY(
                    SELECT key
                    FROM jsonb_object_keys(entry) AS keys(key)
                    ORDER BY key
                 ) <> ARRAY[
                    'input_ref', 'label', 'source_hash', 'source_kind',
                    'source_version', 'text_sha256'
                 ]::text[]
              OR entry ->> 'input_ref' !~
                    '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
              OR entry ->> 'source_kind' NOT IN (
                    'POSTURE_PROFILE', 'WORK_PLAN_ITEM', 'CONFIRMED_FACT',
                    'CONFIRMED_CLAIM', 'CONFIRMED_ISSUE',
                    'CONFIRMED_TRANSACTION', 'VERIFIED_LEGAL_SOURCE',
                    'APPROVED_LEGAL_RULE', 'APPROVED_CALCULATION',
                    'CONFIRMED_PROCEDURAL_EVENT', 'APPROVED_EVIDENCE_ITEM'
                 )
              OR entry ->> 'source_version' !~
                    '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
              OR entry ->> 'source_hash' !~ '^[0-9a-f]{64}$'
              OR entry ->> 'text_sha256' !~ '^[0-9a-f]{64}$'
              OR length(entry ->> 'label') NOT BETWEEN 1 AND 240
              OR entry ->> 'label' <> btrim(entry ->> 'label')
       );
$$;

CREATE FUNCTION case_agent_document_source_manifest_refs(value jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT COALESCE(
        jsonb_agg(entry ->> 'input_ref' ORDER BY entry ->> 'input_ref'),
        '[]'::jsonb
    )
    FROM jsonb_array_elements(value) AS sources(entry);
$$;

CREATE TABLE case_agent_reviewable_document_packages (
    package_id uuid PRIMARY KEY,
    idempotency_key char(64) NOT NULL
        CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    task_input_hash char(64) NOT NULL
        CHECK (task_input_hash ~ '^[0-9a-f]{64}$'),
    case_snapshot_hash char(64) NOT NULL
        CHECK (case_snapshot_hash ~ '^[0-9a-f]{64}$'),
    binding_hash char(64) NOT NULL
        CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    source_set_hash char(64) NOT NULL
        CHECK (source_set_hash ~ '^[0-9a-f]{64}$'),
    authorized_source_refs jsonb NOT NULL CHECK (
        case_agent_document_source_refs_are_canonical(authorized_source_refs)
    ),
    authorized_source_refs_hash char(64) NOT NULL CHECK (
        authorized_source_refs_hash ~ '^[0-9a-f]{64}$'
    ),
    authorized_source_manifest jsonb NOT NULL CHECK (
        case_agent_document_source_manifest_is_valid(authorized_source_manifest)
    ),
    candidate_hash char(64) NOT NULL
        CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),

    work_plan_id uuid NOT NULL,
    work_plan_hash char(64) NOT NULL
        CHECK (work_plan_hash ~ '^[0-9a-f]{64}$'),
    work_plan_item_id uuid NOT NULL,
    posture_profile_id uuid NOT NULL,
    posture_profile_hash char(64) NOT NULL
        CHECK (posture_profile_hash ~ '^[0-9a-f]{64}$'),
    template_id text NOT NULL CHECK (
        template_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
    ),
    template_version text NOT NULL CHECK (
        template_version ~
        '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
    ),
    template_hash char(64) NOT NULL
        CHECK (template_hash ~ '^[0-9a-f]{64}$'),
    deliverable_kind text NOT NULL
        CHECK (deliverable_kind ~ '^[A-Z][A-Z0-9_]{1,119}$'),
    output_format text NOT NULL CHECK (output_format IN ('DOCX', 'XLSX')),
    review_status text NOT NULL
        CHECK (review_status = 'NEEDS_LAWYER_REVIEW'),

    candidate_artifact_id uuid NOT NULL,
    candidate_artifact_kind text NOT NULL CHECK (
        candidate_artifact_kind = 'REVIEWABLE_DOCUMENT_CANDIDATE_JSON'
    ),
    candidate_media_type text NOT NULL CHECK (
        candidate_media_type = 'application/json'
    ),
    candidate_content_sha256 char(64) NOT NULL
        CHECK (candidate_content_sha256 ~ '^[0-9a-f]{64}$'),
    candidate_byte_size bigint NOT NULL
        CHECK (candidate_byte_size BETWEEN 2 AND 4194304),
    candidate_object_key text NOT NULL CHECK (
        candidate_object_key ~
        '^case-agent-document-packages/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}/candidate/[0-9a-f]{64}\.lca$'
    ),
    candidate_object_version_id text,

    editable_artifact_id uuid NOT NULL,
    editable_artifact_kind text NOT NULL CHECK (
        editable_artifact_kind = 'REVIEWABLE_DOCUMENT_EDITABLE'
    ),
    editable_media_type text NOT NULL CHECK (
        editable_media_type IN (
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    ),
    editable_sha256 char(64) NOT NULL
        CHECK (editable_sha256 ~ '^[0-9a-f]{64}$'),
    editable_byte_size bigint NOT NULL
        CHECK (editable_byte_size BETWEEN 1 AND 67108864),
    editable_object_key text NOT NULL CHECK (
        editable_object_key ~
        '^case-agent-document-packages/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}/editable/[0-9a-f]{64}\.lca$'
    ),
    editable_object_version_id text,

    review_pdf_artifact_id uuid NOT NULL,
    review_pdf_artifact_kind text NOT NULL CHECK (
        review_pdf_artifact_kind = 'REVIEWABLE_DOCUMENT_PDF_PREVIEW'
    ),
    review_pdf_media_type text NOT NULL CHECK (
        review_pdf_media_type = 'application/pdf'
    ),
    review_pdf_sha256 char(64) NOT NULL
        CHECK (review_pdf_sha256 ~ '^[0-9a-f]{64}$'),
    review_pdf_byte_size bigint NOT NULL
        CHECK (review_pdf_byte_size BETWEEN 5 AND 134217728),
    review_pdf_page_count integer NOT NULL
        CHECK (review_pdf_page_count BETWEEN 1 AND 10000),
    review_pdf_object_key text NOT NULL CHECK (
        review_pdf_object_key ~
        '^case-agent-document-packages/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}/pdf-preview/[0-9a-f]{64}\.lca$'
    ),
    review_pdf_object_version_id text,

    render_verification_hash char(64) NOT NULL
        CHECK (render_verification_hash ~ '^[0-9a-f]{64}$'),
    review_input_hash char(64) NOT NULL
        CHECK (review_input_hash ~ '^[0-9a-f]{64}$'),
    package_receipt_hash char(64) NOT NULL
        CHECK (package_receipt_hash ~ '^[0-9a-f]{64}$'),
    staged_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (firm_id, idempotency_key),
    UNIQUE (graph_id, task_id),
    UNIQUE (package_id, run_id, firm_id, matter_id),
    UNIQUE (candidate_artifact_id),
    UNIQUE (editable_artifact_id),
    UNIQUE (review_pdf_artifact_id),
    UNIQUE (candidate_artifact_id, run_id, firm_id, matter_id),
    UNIQUE (editable_artifact_id, run_id, firm_id, matter_id),
    UNIQUE (review_pdf_artifact_id, run_id, firm_id, matter_id),

    FOREIGN KEY (matter_id, firm_id)
        REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (attempt_id, graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_task_attempts(
            attempt_id, graph_id, task_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (work_plan_id, firm_id, matter_id)
        REFERENCES case_work_plans(plan_id, firm_id, matter_id),
    FOREIGN KEY (work_plan_item_id, work_plan_id, firm_id, matter_id)
        REFERENCES case_work_plan_items(item_id, plan_id, firm_id, matter_id),
    FOREIGN KEY (posture_profile_id, firm_id, matter_id)
        REFERENCES case_posture_profiles(profile_id, firm_id, matter_id),
    FOREIGN KEY (staged_by, firm_id)
        REFERENCES users(user_id, firm_id),

    CHECK (
        (output_format = 'DOCX' AND editable_media_type =
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        OR
        (output_format = 'XLSX' AND editable_media_type =
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    ),
    CHECK (
        candidate_artifact_id <> editable_artifact_id
        AND candidate_artifact_id <> review_pdf_artifact_id
        AND editable_artifact_id <> review_pdf_artifact_id
    ),
    CHECK (
        authorized_source_refs_hash =
            case_agent_document_source_refs_hash(authorized_source_refs)
    ),
    CHECK (
        authorized_source_refs =
            case_agent_document_source_manifest_refs(authorized_source_manifest)
    ),
    CHECK (split_part(candidate_object_key, '/', 3) = firm_id::text),
    CHECK (split_part(candidate_object_key, '/', 4) = matter_id::text),
    CHECK (split_part(candidate_object_key, '/', 5) = package_id::text),
    CHECK (split_part(candidate_object_key, '/', 7) = candidate_content_sha256 || '.lca'),
    CHECK (split_part(editable_object_key, '/', 3) = firm_id::text),
    CHECK (split_part(editable_object_key, '/', 4) = matter_id::text),
    CHECK (split_part(editable_object_key, '/', 5) = package_id::text),
    CHECK (split_part(editable_object_key, '/', 7) = editable_sha256 || '.lca'),
    CHECK (split_part(review_pdf_object_key, '/', 3) = firm_id::text),
    CHECK (split_part(review_pdf_object_key, '/', 4) = matter_id::text),
    CHECK (split_part(review_pdf_object_key, '/', 5) = package_id::text),
    CHECK (split_part(review_pdf_object_key, '/', 7) = review_pdf_sha256 || '.lca'),
    CHECK (
        candidate_object_version_id IS NULL OR (
            length(candidate_object_version_id) BETWEEN 1 AND 512
            AND candidate_object_version_id = btrim(candidate_object_version_id)
            AND candidate_object_version_id !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        editable_object_version_id IS NULL OR (
            length(editable_object_version_id) BETWEEN 1 AND 512
            AND editable_object_version_id = btrim(editable_object_version_id)
            AND editable_object_version_id !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        review_pdf_object_version_id IS NULL OR (
            length(review_pdf_object_version_id) BETWEEN 1 AND 512
            AND review_pdf_object_version_id = btrim(review_pdf_object_version_id)
            AND review_pdf_object_version_id !~ '[[:cntrl:]]'
        )
    )
);

COMMENT ON TABLE case_agent_reviewable_document_packages IS
    'Immutable Agent candidate package: structured JSON plus editable Office plus PDF preview; never a formal or court-submitted document.';
COMMENT ON COLUMN case_agent_reviewable_document_packages.candidate_object_key IS
    'Private server locator; never project through a browser API.';
COMMENT ON COLUMN case_agent_reviewable_document_packages.editable_object_key IS
    'Private server locator; never project through a browser API.';
COMMENT ON COLUMN case_agent_reviewable_document_packages.review_pdf_object_key IS
    'Private server locator; never project through a browser API.';
COMMENT ON COLUMN case_agent_reviewable_document_packages.authorized_source_manifest IS
    'Immutable non-text manifest of the exact server-expanded drafting sources; source_set_hash binds this manifest including each text digest.';

CREATE INDEX case_agent_reviewable_document_packages_run_idx
    ON case_agent_reviewable_document_packages
    (run_id, created_at, package_id);
CREATE INDEX case_agent_reviewable_document_packages_matter_idx
    ON case_agent_reviewable_document_packages
    (firm_id, matter_id, created_at, package_id);
CREATE INDEX case_agent_reviewable_document_packages_plan_idx
    ON case_agent_reviewable_document_packages
    (work_plan_id, work_plan_item_id, created_at);
CREATE INDEX case_agent_reviewable_document_packages_attempt_idx
    ON case_agent_reviewable_document_packages (attempt_id);
CREATE INDEX case_agent_reviewable_document_packages_posture_idx
    ON case_agent_reviewable_document_packages (posture_profile_id);
CREATE INDEX case_agent_reviewable_document_packages_staged_by_idx
    ON case_agent_reviewable_document_packages (staged_by, firm_id);

CREATE FUNCTION validate_case_agent_reviewable_document_package_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    task_row record;
    plan_row record;
BEGIN
    IF NEW.staged_by IS DISTINCT FROM
       NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
        RAISE EXCEPTION 'reviewable document staging principal differs from transaction identity';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM matter_actor_roles role
        JOIN users principal
          ON principal.user_id = role.user_id AND principal.firm_id = role.firm_id
        WHERE role.firm_id = NEW.firm_id
          AND role.matter_id = NEW.matter_id
          AND role.user_id = NEW.staged_by
          AND role.role = 'SYSTEM_WORKER'
          AND role.revoked_at IS NULL
          AND principal.status = 'ACTIVE'
    ) OR EXISTS (
        SELECT 1 FROM matter_actor_roles role
        WHERE role.firm_id = NEW.firm_id
          AND role.matter_id = NEW.matter_id
          AND role.user_id = NEW.staged_by
          AND role.role <> 'SYSTEM_WORKER'
          AND role.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'reviewable document staging requires a dedicated active SYSTEM_WORKER';
    END IF;

    SELECT run.current_graph_id, run.current_graph_hash,
           run.snapshot_hash, run.snapshot_matter_version,
           run.status AS run_status, run.is_stale, run.is_cancelled,
           graph.graph_hash, graph.snapshot_hash AS graph_snapshot_hash,
           task.input_hash, task.input_refs, task.writes_managed_derivatives,
           task.approval_gate, head.active_attempt_id, head.status AS head_status,
           head.is_current, attempt.status AS attempt_status,
           matter.version AS matter_version
      INTO task_row
      FROM case_agent_runs run
      JOIN case_agent_task_graphs graph
        ON graph.graph_id = NEW.graph_id AND graph.run_id = run.run_id
       AND graph.firm_id = run.firm_id AND graph.matter_id = run.matter_id
      JOIN case_agent_tasks task
        ON task.graph_id = graph.graph_id AND task.task_id = NEW.task_id
       AND task.run_id = run.run_id AND task.firm_id = run.firm_id
       AND task.matter_id = run.matter_id
      JOIN case_agent_task_heads head
        ON head.graph_id = task.graph_id AND head.task_id = task.task_id
       AND head.run_id = task.run_id AND head.firm_id = task.firm_id
       AND head.matter_id = task.matter_id
      JOIN case_agent_task_attempts attempt
        ON attempt.attempt_id = NEW.attempt_id
       AND attempt.graph_id = task.graph_id AND attempt.task_id = task.task_id
       AND attempt.run_id = task.run_id AND attempt.firm_id = task.firm_id
       AND attempt.matter_id = task.matter_id
      JOIN matters matter
        ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
     WHERE run.run_id = NEW.run_id AND run.firm_id = NEW.firm_id
       AND run.matter_id = NEW.matter_id;

    IF task_row IS NULL
       OR task_row.current_graph_id IS DISTINCT FROM NEW.graph_id
       OR task_row.current_graph_hash IS DISTINCT FROM task_row.graph_hash
       OR task_row.snapshot_hash IS DISTINCT FROM NEW.case_snapshot_hash
       OR task_row.graph_snapshot_hash IS DISTINCT FROM NEW.case_snapshot_hash
       OR task_row.input_hash IS DISTINCT FROM NEW.task_input_hash
       OR task_row.run_status <> 'EXECUTING'
       OR task_row.is_stale OR task_row.is_cancelled
       OR task_row.matter_version <> task_row.snapshot_matter_version
       OR NOT task_row.writes_managed_derivatives
       OR task_row.approval_gate <> 'LAWYER_REVIEW'
       OR NOT task_row.is_current
       OR task_row.head_status <> 'RUNNING'
       OR task_row.active_attempt_id IS DISTINCT FROM NEW.attempt_id
       OR task_row.attempt_status <> 'RUNNING'
       OR task_row.input_refs IS DISTINCT FROM jsonb_build_array(
            'work-plan-item:' || NEW.work_plan_item_id::text
          ) THEN
        RAISE EXCEPTION 'reviewable document differs from the current running Agent task';
    END IF;

    SELECT plan.status, plan.plan_hash, plan.profile_id, plan.profile_hash,
           plan.activated_matter_version, item.item_kind, item.readiness,
           item.delivery_target, item.deliverable_kind,
           plan_head.current_plan_id, profile_head.current_profile_id,
           profile.status AS profile_status,
           profile.profile_hash AS actual_profile_hash
      INTO plan_row
      FROM case_work_plans plan
      JOIN case_work_plan_heads plan_head
        ON plan_head.matter_id = plan.matter_id AND plan_head.firm_id = plan.firm_id
      JOIN case_work_plan_items item
        ON item.plan_id = plan.plan_id AND item.item_id = NEW.work_plan_item_id
       AND item.firm_id = plan.firm_id AND item.matter_id = plan.matter_id
      JOIN case_posture_profiles profile
        ON profile.profile_id = NEW.posture_profile_id
       AND profile.firm_id = plan.firm_id AND profile.matter_id = plan.matter_id
      JOIN case_posture_profile_heads profile_head
        ON profile_head.matter_id = profile.matter_id
       AND profile_head.firm_id = profile.firm_id
     WHERE plan.plan_id = NEW.work_plan_id AND plan.firm_id = NEW.firm_id
       AND plan.matter_id = NEW.matter_id;

    IF plan_row IS NULL
       OR plan_row.status <> 'ACTIVE'
       OR plan_row.current_plan_id IS DISTINCT FROM NEW.work_plan_id
       OR plan_row.plan_hash IS DISTINCT FROM NEW.work_plan_hash
       OR plan_row.profile_id IS DISTINCT FROM NEW.posture_profile_id
       OR plan_row.profile_hash IS DISTINCT FROM NEW.posture_profile_hash
       OR plan_row.actual_profile_hash IS DISTINCT FROM NEW.posture_profile_hash
       OR plan_row.current_profile_id IS DISTINCT FROM NEW.posture_profile_id
       OR plan_row.profile_status <> 'CONFIRMED'
       OR plan_row.activated_matter_version <> task_row.snapshot_matter_version
       OR plan_row.item_kind <> 'DOCUMENT_CANDIDATE'
       OR plan_row.readiness <> 'ACTIONABLE'
       OR plan_row.delivery_target = 'NOT_APPLICABLE'
       OR plan_row.deliverable_kind IS DISTINCT FROM NEW.deliverable_kind THEN
        RAISE EXCEPTION 'reviewable document differs from the active dynamic work plan';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_reviewable_document_packages_insert_guard
    BEFORE INSERT ON case_agent_reviewable_document_packages
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_reviewable_document_package_insert();

CREATE FUNCTION prohibit_case_agent_reviewable_document_package_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent reviewable document packages are append-only';
END;
$$;

CREATE TRIGGER case_agent_reviewable_document_packages_append_only
    BEFORE UPDATE OR DELETE ON case_agent_reviewable_document_packages
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_reviewable_document_package_change();

ALTER TABLE case_agent_reviewable_document_packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_reviewable_document_packages FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_reviewable_document_packages_firm_isolation
    ON case_agent_reviewable_document_packages
    USING (firm_id::text = (SELECT current_setting('app.firm_id', true)))
    WITH CHECK (firm_id::text = (SELECT current_setting('app.firm_id', true)));

REVOKE ALL ON TABLE case_agent_reviewable_document_packages FROM PUBLIC;

COMMIT;
