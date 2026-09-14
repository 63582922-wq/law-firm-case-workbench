-- PostgreSQL 16+; apply after 0072_lawyer_decision_package_document_source.sql.
--
-- A successful Agent document task is immutable.  This migration adds an
-- append-only, zero-external-call revision chain for forward-only template
-- upgrades before final lawyer review.  The browser can request a revision,
-- but cannot select the template, sources, bytes, Worker or verifier.

BEGIN;

ALTER TABLE case_agent_reviewable_document_packages
    DROP CONSTRAINT case_agent_reviewable_document_packages_graph_id_task_id_key;

ALTER TABLE case_agent_reviewable_document_packages
    ADD COLUMN generation_mode text NOT NULL DEFAULT 'INITIAL_AGENT_TASK'
        CHECK (generation_mode IN (
            'INITIAL_AGENT_TASK', 'DETERMINISTIC_TEMPLATE_REVISION'
        )),
    ADD COLUMN revision_number integer NOT NULL DEFAULT 1
        CHECK (revision_number BETWEEN 1 AND 1000),
    ADD COLUMN root_package_id uuid,
    ADD COLUMN supersedes_package_id uuid,
    ADD COLUMN revision_request_id uuid,
    ADD COLUMN requested_by uuid;

ALTER TABLE case_agent_reviewable_document_packages
    ADD CONSTRAINT case_agent_reviewable_document_package_revision_shape CHECK (
        (
            generation_mode = 'INITIAL_AGENT_TASK'
            AND revision_number = 1
            AND root_package_id IS NULL
            AND supersedes_package_id IS NULL
            AND revision_request_id IS NULL
            AND requested_by IS NULL
        ) OR (
            generation_mode = 'DETERMINISTIC_TEMPLATE_REVISION'
            AND revision_number BETWEEN 2 AND 1000
            AND root_package_id IS NOT NULL
            AND supersedes_package_id IS NOT NULL
            AND revision_request_id IS NOT NULL
            AND requested_by IS NOT NULL
        )
    ),
    ADD CONSTRAINT case_agent_reviewable_document_package_root_fkey
        FOREIGN KEY (root_package_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(
            package_id, run_id, firm_id, matter_id
        ),
    ADD CONSTRAINT case_agent_reviewable_document_package_predecessor_fkey
        FOREIGN KEY (supersedes_package_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(
            package_id, run_id, firm_id, matter_id
        ),
    ADD CONSTRAINT case_agent_reviewable_document_package_requester_fkey
        FOREIGN KEY (requested_by, firm_id) REFERENCES users(user_id, firm_id),
    ADD CONSTRAINT case_agent_reviewable_document_package_request_unique
        UNIQUE (revision_request_id),
    ADD CONSTRAINT case_agent_reviewable_document_package_tenant_identity_unique
        UNIQUE (package_id, firm_id, matter_id);

CREATE UNIQUE INDEX case_agent_reviewable_document_package_initial_task_unique
    ON case_agent_reviewable_document_packages(graph_id, task_id)
    WHERE generation_mode = 'INITIAL_AGENT_TASK';

CREATE INDEX case_agent_reviewable_document_package_root_idx
    ON case_agent_reviewable_document_packages(
        root_package_id, revision_number DESC, created_at DESC
    ) WHERE generation_mode = 'DETERMINISTIC_TEMPLATE_REVISION';

CREATE TABLE case_agent_document_revision_requests (
    request_id uuid PRIMARY KEY,
    idempotency_key_hash char(64) NOT NULL
        CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    run_id uuid NOT NULL,
    root_package_id uuid NOT NULL,
    predecessor_package_id uuid NOT NULL,
    expected_revision_number integer NOT NULL
        CHECK (expected_revision_number BETWEEN 1 AND 999),
    target_template_id text NOT NULL CHECK (
        target_template_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
    ),
    target_template_version text NOT NULL CHECK (
        target_template_version ~
        '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
    ),
    target_template_hash char(64) NOT NULL
        CHECK (target_template_hash ~ '^[0-9a-f]{64}$'),
    source_package_receipt_hash char(64) NOT NULL
        CHECK (source_package_receipt_hash ~ '^[0-9a-f]{64}$'),
    requested_by uuid NOT NULL,
    request_hash char(64) NOT NULL
        CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (firm_id, idempotency_key_hash),
    UNIQUE (request_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (root_package_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(
            package_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (predecessor_package_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(
            package_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (requested_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (root_package_id <> predecessor_package_id OR expected_revision_number = 1)
);

ALTER TABLE case_agent_reviewable_document_packages
    ADD CONSTRAINT case_agent_reviewable_document_package_revision_request_fkey
        FOREIGN KEY (revision_request_id, firm_id, matter_id)
        REFERENCES case_agent_document_revision_requests(
            request_id, firm_id, matter_id
        );

CREATE TABLE case_agent_document_revision_inbox (
    request_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'READY'
        CHECK (state IN ('READY', 'LEASED', 'QUIET')),
    claimed_by uuid,
    lease_expires_at timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    updated_at timestamptz NOT NULL DEFAULT now(),

    FOREIGN KEY (request_id, firm_id, matter_id)
        REFERENCES case_agent_document_revision_requests(
            request_id, firm_id, matter_id
        ),
    FOREIGN KEY (claimed_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (state = 'LEASED' AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state IN ('READY', 'QUIET') AND claimed_by IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE INDEX case_agent_document_revision_inbox_ready_idx
    ON case_agent_document_revision_inbox(
        firm_id, state, available_at, updated_at, request_id
    );

CREATE TABLE case_agent_document_revision_receipts (
    receipt_id uuid PRIMARY KEY,
    request_id uuid NOT NULL UNIQUE,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('PASSED', 'FAILED', 'UNKNOWN')),
    successor_package_id uuid,
    successor_package_receipt_hash char(64),
    failure_code text,
    external_calls integer NOT NULL DEFAULT 0 CHECK (external_calls = 0),
    executed_by uuid NOT NULL,
    verified_by uuid,
    receipt_hash char(64) NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (receipt_id, firm_id, matter_id),
    FOREIGN KEY (request_id, firm_id, matter_id)
        REFERENCES case_agent_document_revision_requests(
            request_id, firm_id, matter_id
        ),
    FOREIGN KEY (successor_package_id, firm_id, matter_id)
        REFERENCES case_agent_reviewable_document_packages(
            package_id, firm_id, matter_id
        ),
    FOREIGN KEY (executed_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (verified_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (
            outcome = 'PASSED'
            AND successor_package_id IS NOT NULL
            AND successor_package_receipt_hash ~ '^[0-9a-f]{64}$'
            AND failure_code IS NULL
            AND verified_by IS NOT NULL
            AND verified_by <> executed_by
        ) OR (
            outcome IN ('FAILED', 'UNKNOWN')
            AND successor_package_id IS NULL
            AND successor_package_receipt_hash IS NULL
            AND failure_code ~ '^[A-Z][A-Z0-9_]{2,79}$'
            AND verified_by IS NULL
        )
    )
);

CREATE FUNCTION validate_case_agent_document_revision_request_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    predecessor record;
    root_package record;
    actual_latest record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'case-agent-document-revision:' || NEW.firm_id::text || ':' ||
        NEW.root_package_id::text,
        0
    ));
    IF NEW.requested_by IS DISTINCT FROM
       NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
        RAISE EXCEPTION 'document revision requester differs from transaction identity';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM users principal
        JOIN matter_actor_roles role
          ON role.user_id = principal.user_id AND role.firm_id = principal.firm_id
        WHERE principal.user_id = NEW.requested_by
          AND principal.firm_id = NEW.firm_id
          AND principal.status = 'ACTIVE'
          AND role.matter_id = NEW.matter_id
          AND role.role IN (
              'ASSISTANT', 'COLLABORATING_LAWYER', 'LEAD_LAWYER', 'REVIEWER'
          )
          AND role.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'document revision requires an active matter lawyer';
    END IF;

    SELECT package.*, run.status AS run_status, run.is_stale, run.is_cancelled,
           run.current_graph_id, run.snapshot_matter_version,
           matter.version AS matter_version
      INTO predecessor
      FROM case_agent_reviewable_document_packages package
      JOIN case_agent_runs run
        ON run.run_id = package.run_id AND run.firm_id = package.firm_id
       AND run.matter_id = package.matter_id
      JOIN matters matter
        ON matter.matter_id = package.matter_id AND matter.firm_id = package.firm_id
     WHERE package.package_id = NEW.predecessor_package_id
       AND package.run_id = NEW.run_id AND package.firm_id = NEW.firm_id
       AND package.matter_id = NEW.matter_id;

    SELECT * INTO root_package
      FROM case_agent_reviewable_document_packages package
     WHERE package.package_id = NEW.root_package_id
       AND package.run_id = NEW.run_id AND package.firm_id = NEW.firm_id
       AND package.matter_id = NEW.matter_id;

    SELECT candidate.* INTO actual_latest
      FROM case_agent_reviewable_document_packages candidate
      LEFT JOIN case_agent_document_revision_receipts receipt
        ON receipt.successor_package_id = candidate.package_id
       AND receipt.firm_id = candidate.firm_id
       AND receipt.matter_id = candidate.matter_id
       AND receipt.outcome = 'PASSED'
     WHERE candidate.package_id = NEW.root_package_id
        OR (
            candidate.root_package_id = NEW.root_package_id
            AND receipt.receipt_id IS NOT NULL
        )
     ORDER BY candidate.revision_number DESC
     LIMIT 1;

    IF predecessor IS NULL OR root_package IS NULL OR actual_latest IS NULL
       OR root_package.generation_mode <> 'INITIAL_AGENT_TASK'
       OR root_package.revision_number <> 1
       OR predecessor.package_id IS DISTINCT FROM actual_latest.package_id
       OR predecessor.revision_number <> NEW.expected_revision_number
       OR (
            predecessor.package_id <> NEW.root_package_id
            AND predecessor.root_package_id IS DISTINCT FROM NEW.root_package_id
       )
       OR predecessor.package_receipt_hash <> NEW.source_package_receipt_hash
       OR predecessor.run_status <> 'READY_FOR_REVIEW'
       OR predecessor.is_stale OR predecessor.is_cancelled
       OR predecessor.current_graph_id IS DISTINCT FROM predecessor.graph_id
       OR predecessor.snapshot_matter_version <> predecessor.matter_version
       OR predecessor.template_id <> NEW.target_template_id
       OR (
            predecessor.template_version = NEW.target_template_version
            AND predecessor.template_hash = NEW.target_template_hash
       ) THEN
        RAISE EXCEPTION 'document revision request is not based on the current review package';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM case_agent_document_revision_requests existing_request
        JOIN case_agent_document_revision_inbox existing_inbox
          ON existing_inbox.request_id = existing_request.request_id
         AND existing_inbox.firm_id = existing_request.firm_id
         AND existing_inbox.matter_id = existing_request.matter_id
        LEFT JOIN case_agent_document_revision_receipts existing_receipt
          ON existing_receipt.request_id = existing_request.request_id
         AND existing_receipt.firm_id = existing_request.firm_id
         AND existing_receipt.matter_id = existing_request.matter_id
        WHERE existing_request.root_package_id = NEW.root_package_id
          AND existing_request.predecessor_package_id = NEW.predecessor_package_id
          AND existing_request.firm_id = NEW.firm_id
          AND existing_request.matter_id = NEW.matter_id
          AND existing_receipt.receipt_id IS NULL
          AND existing_inbox.state IN ('READY', 'LEASED')
    ) THEN
        RAISE EXCEPTION 'document revision already has an active request';
    END IF;
    IF NEW.request_hash IS DISTINCT FROM encode(
        digest(
            concat_ws(
                '|',
                'case-agent-document-revision-request-v1',
                NEW.request_id::text,
                NEW.firm_id::text,
                NEW.matter_id::text,
                NEW.run_id::text,
                NEW.root_package_id::text,
                NEW.predecessor_package_id::text,
                NEW.expected_revision_number::text,
                NEW.target_template_id,
                NEW.target_template_version,
                NEW.target_template_hash,
                NEW.source_package_receipt_hash,
                NEW.requested_by::text,
                NEW.idempotency_key_hash
            )::bytea,
            'sha256'
        ),
        'hex'
    ) THEN
        RAISE EXCEPTION 'document revision request hash is invalid';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM case_agent_verification_attempts attempt
        JOIN case_agent_verification_receipts receipt
          ON receipt.verification_attempt_id = attempt.verification_attempt_id
         AND receipt.run_id = attempt.run_id
         AND receipt.firm_id = attempt.firm_id
         AND receipt.matter_id = attempt.matter_id
        JOIN case_agent_runs run
          ON run.run_id = attempt.run_id AND run.firm_id = attempt.firm_id
         AND run.matter_id = attempt.matter_id
        WHERE attempt.run_id = NEW.run_id AND attempt.firm_id = NEW.firm_id
          AND attempt.matter_id = NEW.matter_id
          AND receipt.outcome = 'PASSED'
          AND receipt.verification_hash = run.verification_hash
          AND receipt.graph_hash = run.current_graph_hash
          AND receipt.snapshot_hash = run.snapshot_hash
          AND receipt.artifact_lineage @> jsonb_build_array(
                jsonb_build_object('artifact_id', root_package.candidate_artifact_id::text),
                jsonb_build_object('artifact_id', root_package.editable_artifact_id::text),
                jsonb_build_object('artifact_id', root_package.review_pdf_artifact_id::text)
          )
    ) THEN
        RAISE EXCEPTION 'document revision root package is not in the current PASSED lineage';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_document_revision_requests_insert_guard
    BEFORE INSERT ON case_agent_document_revision_requests
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_document_revision_request_insert();

CREATE FUNCTION enqueue_case_agent_document_revision_request()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    INSERT INTO case_agent_document_revision_inbox(
        request_id, firm_id, matter_id
    ) VALUES (NEW.request_id, NEW.firm_id, NEW.matter_id);
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_document_revision_requests_enqueue
    AFTER INSERT ON case_agent_document_revision_requests
    FOR EACH ROW EXECUTE FUNCTION enqueue_case_agent_document_revision_request();

CREATE FUNCTION prohibit_case_agent_document_revision_request_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION 'case Agent document revision requests are append-only';
END;
$$;

CREATE TRIGGER case_agent_document_revision_requests_append_only
    BEFORE UPDATE OR DELETE ON case_agent_document_revision_requests
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_document_revision_request_change();

DROP TRIGGER case_agent_reviewable_document_packages_insert_guard
    ON case_agent_reviewable_document_packages;

CREATE TRIGGER case_agent_reviewable_document_packages_initial_insert_guard
    BEFORE INSERT ON case_agent_reviewable_document_packages
    FOR EACH ROW
    WHEN (NEW.generation_mode = 'INITIAL_AGENT_TASK')
    EXECUTE FUNCTION validate_case_agent_reviewable_document_package_insert();

CREATE FUNCTION validate_case_agent_reviewable_document_revision_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    revision_request record;
    predecessor record;
    root_package record;
    inbox_row record;
BEGIN
    IF NEW.generation_mode <> 'DETERMINISTIC_TEMPLATE_REVISION' THEN
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
    SELECT * INTO inbox_row
      FROM case_agent_document_revision_inbox inbox
     WHERE inbox.request_id = NEW.revision_request_id
       AND inbox.firm_id = NEW.firm_id AND inbox.matter_id = NEW.matter_id;

    IF revision_request IS NULL OR predecessor IS NULL OR root_package IS NULL
       OR inbox_row IS NULL OR inbox_row.state <> 'LEASED'
       OR inbox_row.claimed_by IS DISTINCT FROM NEW.staged_by
       OR inbox_row.lease_expires_at <= now()
       OR revision_request.root_package_id IS DISTINCT FROM NEW.root_package_id
       OR revision_request.predecessor_package_id IS DISTINCT FROM NEW.supersedes_package_id
       OR revision_request.requested_by IS DISTINCT FROM NEW.requested_by
       OR revision_request.expected_revision_number <> predecessor.revision_number
       OR NEW.revision_number <> predecessor.revision_number + 1
       OR root_package.generation_mode <> 'INITIAL_AGENT_TASK'
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
       OR (NEW.template_version = predecessor.template_version
           AND NEW.template_hash = predecessor.template_hash)
       OR EXISTS (
            SELECT 1
            FROM case_agent_reviewable_document_packages successor
            JOIN case_agent_document_revision_receipts successor_receipt
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
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_reviewable_document_packages_revision_insert_guard
    BEFORE INSERT ON case_agent_reviewable_document_packages
    FOR EACH ROW
    WHEN (NEW.generation_mode = 'DETERMINISTIC_TEMPLATE_REVISION')
    EXECUTE FUNCTION validate_case_agent_reviewable_document_revision_insert();

CREATE FUNCTION validate_case_agent_document_revision_receipt_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    revision_request record;
    successor record;
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
           OR successor.generation_mode <> 'DETERMINISTIC_TEMPLATE_REVISION'
           OR successor.supersedes_package_id IS DISTINCT FROM
                revision_request.predecessor_package_id
           OR successor.root_package_id IS DISTINCT FROM
                revision_request.root_package_id
           OR EXISTS (
                SELECT 1
                FROM case_agent_reviewable_document_packages prior_successor
                JOIN case_agent_document_revision_receipts prior_receipt
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
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_document_revision_receipts_insert_guard
    BEFORE INSERT ON case_agent_document_revision_receipts
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_document_revision_receipt_insert();

CREATE FUNCTION quiet_case_agent_document_revision_inbox()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    UPDATE case_agent_document_revision_inbox
       SET state = 'QUIET', claimed_by = NULL, lease_expires_at = NULL,
           updated_at = now()
     WHERE request_id = NEW.request_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_document_revision_receipts_quiet_inbox
    AFTER INSERT ON case_agent_document_revision_receipts
    FOR EACH ROW EXECUTE FUNCTION quiet_case_agent_document_revision_inbox();

CREATE FUNCTION prohibit_case_agent_document_revision_receipt_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION 'case Agent document revision receipts are append-only';
END;
$$;

CREATE TRIGGER case_agent_document_revision_receipts_append_only
    BEFORE UPDATE OR DELETE ON case_agent_document_revision_receipts
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_document_revision_receipt_change();

ALTER TABLE case_agent_document_revision_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_revision_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_document_revision_requests_firm_isolation
    ON case_agent_document_revision_requests
    USING (firm_id::text = (SELECT current_setting('app.firm_id', true)))
    WITH CHECK (firm_id::text = (SELECT current_setting('app.firm_id', true)));

ALTER TABLE case_agent_document_revision_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_revision_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_document_revision_inbox_firm_isolation
    ON case_agent_document_revision_inbox
    USING (firm_id::text = (SELECT current_setting('app.firm_id', true)))
    WITH CHECK (firm_id::text = (SELECT current_setting('app.firm_id', true)));

ALTER TABLE case_agent_document_revision_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_revision_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_document_revision_receipts_firm_isolation
    ON case_agent_document_revision_receipts
    USING (firm_id::text = (SELECT current_setting('app.firm_id', true)))
    WITH CHECK (firm_id::text = (SELECT current_setting('app.firm_id', true)));

REVOKE ALL ON TABLE case_agent_document_revision_requests FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_document_revision_inbox FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_document_revision_receipts FROM PUBLIC;

GRANT SELECT, INSERT ON case_agent_document_revision_requests
    TO lawcase_web_application;
GRANT SELECT ON case_agent_document_revision_inbox,
                case_agent_document_revision_receipts
    TO lawcase_web_application;

GRANT SELECT ON case_agent_document_revision_requests,
                case_agent_document_revision_receipts
    TO lawcase_agent_worker;
GRANT SELECT, UPDATE ON case_agent_document_revision_inbox
    TO lawcase_agent_worker;
GRANT INSERT ON case_agent_document_revision_receipts
    TO lawcase_agent_worker;

GRANT SELECT ON case_agent_document_revision_requests,
                case_agent_document_revision_inbox
    TO lawcase_agent_verifier;
GRANT INSERT, SELECT ON case_agent_document_revision_receipts
    TO lawcase_agent_verifier;

COMMENT ON TABLE case_agent_document_revision_requests IS
    'Immutable lawyer command to compile the next review-only document version from the same still-current verified sources and the server current template.';
COMMENT ON TABLE case_agent_document_revision_inbox IS
    'Recoverable zero-network Worker inbox projection for deterministic document revisions.';
COMMENT ON TABLE case_agent_document_revision_receipts IS
    'Append-only terminal outcome; PASSED requires an independently re-read successor package and a distinct verifier Worker.';

COMMIT;
