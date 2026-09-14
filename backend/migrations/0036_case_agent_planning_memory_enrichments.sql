-- Durable, minimal pre-planning memory enrichment receipts.
--
-- Retrieval itself remains in the 0032 ACL-first FTS ledger.  This table does
-- not store a query body, source body, prompt, object key or URL.  It binds the
-- verified result to exactly one run owner, goal, case snapshot, base planning
-- projection and planning purpose.  Current authorization is always re-read
-- before a receipt is returned to a planning provider.

BEGIN;

CREATE TABLE case_agent_planning_memory_enrichments (
    enrichment_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    run_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    goal_hash char(64) NOT NULL CHECK (goal_hash ~ '^[0-9a-f]{64}$'),
    owner_actor_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'DYNAMIC_CASE_PLANNING'),
    case_snapshot_hash char(64) NOT NULL CHECK (
        case_snapshot_hash ~ '^[0-9a-f]{64}$'
    ),
    case_snapshot_version integer NOT NULL CHECK (case_snapshot_version > 0),
    case_snapshot_schema_version text NOT NULL CHECK (
        length(trim(case_snapshot_schema_version)) BETWEEN 1 AND 200
    ),
    base_planning_hash char(64) NOT NULL CHECK (
        base_planning_hash ~ '^[0-9a-f]{64}$'
    ),
    query_hash char(64) NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),
    query_contract_hash char(64) NOT NULL CHECK (
        query_contract_hash ~ '^[0-9a-f]{64}$'
    ),
    query_fingerprint char(64) NOT NULL CHECK (
        query_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    retrieval_id uuid NOT NULL,
    retrieval_scope_hash char(64) NOT NULL CHECK (
        retrieval_scope_hash ~ '^[0-9a-f]{64}$'
    ),
    owner_grant_hash char(64) NOT NULL CHECK (
        owner_grant_hash ~ '^[0-9a-f]{64}$'
    ),
    owner_roles text[] NOT NULL CHECK (
        cardinality(owner_roles) BETWEEN 1 AND 5
        AND owner_roles <@ ARRAY[
            'ASSISTANT', 'COLLABORATING_LAWYER', 'LEAD_LAWYER', 'REVIEWER',
            'FIRM_ADMIN'
        ]::text[]
        AND owner_roles && ARRAY[
            'ASSISTANT', 'COLLABORATING_LAWYER', 'LEAD_LAWYER', 'REVIEWER'
        ]::text[]
    ),
    permission_group_ids uuid[] NOT NULL,
    -- Each item is bounded to immutable ids/hashes, <=300 bytes of normalized
    -- trusted-extractor summary and authoritative source coordinates.  It must
    -- never contain the full indexed document or encrypted source content.
    items jsonb NOT NULL CHECK (
        jsonb_typeof(items) = 'array'
        AND jsonb_array_length(items) BETWEEN 1 AND 20
    ),
    items_hash char(64) NOT NULL CHECK (items_hash ~ '^[0-9a-f]{64}$'),
    retrieved_at timestamptz NOT NULL,
    verified_at timestamptz NOT NULL CHECK (verified_at >= retrieved_at),
    final_verified_at timestamptz NOT NULL CHECK (
        final_verified_at >= verified_at
    ),
    receipt_hash char(64) NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    created_by_worker uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (enrichment_id, firm_id, matter_id),
    UNIQUE (
        run_id, purpose, case_snapshot_hash, base_planning_hash,
        query_contract_hash, query_fingerprint, owner_grant_hash, items_hash
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (goal_id, firm_id, matter_id)
        REFERENCES case_agent_goals(goal_id, firm_id, matter_id),
    FOREIGN KEY (owner_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (created_by_worker, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (retrieval_id, firm_id, matter_id)
        REFERENCES case_agent_memory_retrieval_audits(
            retrieval_id, firm_id, matter_id
        )
);

CREATE FUNCTION validate_case_agent_planning_memory_enrichment() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bound_run case_agent_runs%ROWTYPE;
    retrieval case_agent_memory_retrieval_audits%ROWTYPE;
    current_owner_roles text[];
    current_permission_group_ids uuid[];
    item jsonb;
    source jsonb;
BEGIN
    SELECT * INTO bound_run
    FROM case_agent_runs run
    WHERE run.run_id = NEW.run_id
      AND run.firm_id = NEW.firm_id
      AND run.matter_id = NEW.matter_id;
    IF NOT FOUND
       OR bound_run.goal_id <> NEW.goal_id
       OR bound_run.created_by <> NEW.owner_actor_id
       OR bound_run.snapshot_hash <> NEW.case_snapshot_hash
       OR bound_run.snapshot_matter_version <> NEW.case_snapshot_version
       OR bound_run.snapshot_schema_version <> NEW.case_snapshot_schema_version THEN
        RAISE EXCEPTION 'planning memory enrichment is outside its Agent run';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM matters matter
        WHERE matter.matter_id = NEW.matter_id
          AND matter.firm_id = NEW.firm_id
          AND matter.version = NEW.case_snapshot_version
    ) THEN
        RAISE EXCEPTION 'planning memory enrichment case version is stale';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM users worker
        JOIN matter_actor_roles worker_role
          ON worker_role.user_id = worker.user_id
         AND worker_role.firm_id = worker.firm_id
         AND worker_role.matter_id = NEW.matter_id
         AND worker_role.role = 'SYSTEM_WORKER'
         AND worker_role.revoked_at IS NULL
        WHERE worker.user_id = NEW.created_by_worker
          AND worker.firm_id = NEW.firm_id
          AND worker.status = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION 'planning memory enrichment worker is not currently authorized';
    END IF;
    SELECT COALESCE(
        array_agg(DISTINCT owner_role.role ORDER BY owner_role.role)
            FILTER (WHERE owner_role.role IS NOT NULL),
        ARRAY[]::text[]
    ) INTO current_owner_roles
    FROM users owner
    LEFT JOIN matter_actor_roles owner_role
      ON owner_role.user_id = owner.user_id
     AND owner_role.firm_id = owner.firm_id
     AND owner_role.matter_id = NEW.matter_id
     AND owner_role.revoked_at IS NULL
     AND owner_role.role IN (
         'ASSISTANT', 'COLLABORATING_LAWYER', 'LEAD_LAWYER', 'REVIEWER',
         'FIRM_ADMIN'
     )
    WHERE owner.user_id = NEW.owner_actor_id
      AND owner.firm_id = NEW.firm_id
      AND owner.status = 'ACTIVE';
    IF current_owner_roles IS NULL OR current_owner_roles <> NEW.owner_roles THEN
        RAISE EXCEPTION 'planning memory enrichment owner authority is stale';
    END IF;
    SELECT COALESCE(
        array_agg(DISTINCT permission_group.group_id ORDER BY permission_group.group_id),
        ARRAY[]::uuid[]
    ) INTO current_permission_group_ids
    FROM case_agent_memory_group_memberships membership
    JOIN case_agent_memory_permission_groups permission_group
      ON permission_group.group_id = membership.group_id
     AND permission_group.firm_id = membership.firm_id
    WHERE membership.firm_id = NEW.firm_id
      AND membership.member_actor_id = NEW.owner_actor_id
      AND (
          permission_group.scope_kind = 'FIRM'
          OR permission_group.matter_id = NEW.matter_id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM case_agent_memory_group_member_denials denial
          WHERE denial.membership_id = membership.membership_id
            AND denial.firm_id = membership.firm_id
      );
    IF cardinality(NEW.permission_group_ids) <> (
        SELECT count(DISTINCT group_id)
        FROM unnest(NEW.permission_group_ids) AS group_id
    ) THEN
        RAISE EXCEPTION 'planning memory enrichment groups must be unique';
    END IF;
    IF current_permission_group_ids <> NEW.permission_group_ids THEN
        RAISE EXCEPTION 'planning memory enrichment group authority is stale';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM case_agent_goals goal
        WHERE goal.goal_id = NEW.goal_id
          AND goal.firm_id = NEW.firm_id
          AND goal.matter_id = NEW.matter_id
          AND goal.goal_hash = NEW.goal_hash
          AND goal.requested_by = NEW.owner_actor_id
    ) THEN
        RAISE EXCEPTION 'planning memory enrichment is outside its lawyer goal';
    END IF;
    SELECT * INTO retrieval
    FROM case_agent_memory_retrieval_audits audit
    WHERE audit.retrieval_id = NEW.retrieval_id
      AND audit.firm_id = NEW.firm_id
      AND audit.matter_id = NEW.matter_id;
    IF NOT FOUND
       OR retrieval.actor_id <> NEW.owner_actor_id
       OR retrieval.run_id <> NEW.run_id
       OR retrieval.task_id IS NOT NULL
       OR retrieval.query_hash <> NEW.query_hash
       OR retrieval.query_fingerprint <> NEW.query_fingerprint
       OR retrieval.grant_hash <> NEW.owner_grant_hash
       OR retrieval.matter_version <> NEW.case_snapshot_version
       OR retrieval.scope_hash <> NEW.retrieval_scope_hash
       OR retrieval.search_mode <> 'POSTGRES_FTS_V1'
       OR retrieval.authorized_at <> NEW.retrieved_at
       OR retrieval.verified_at <> NEW.verified_at
       OR NEW.final_verified_at < retrieval.verified_at THEN
        RAISE EXCEPTION 'planning memory enrichment differs from its ACL retrieval';
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(NEW.items) LOOP
        IF NOT item ?& ARRAY[
                'record_id', 'record_version', 'layer', 'authority',
                'content_hash', 'provenance_hash', 'summary', 'summary_hash',
                'source_refs', 'externally_disclosable',
                'external_authorization_hash', 'item_hash', 'source_ref_hash'
           ]
           OR EXISTS (
               SELECT 1
               FROM jsonb_object_keys(item) AS item_keys(key_name)
               WHERE key_name <> ALL (ARRAY[
                   'record_id', 'record_version', 'layer', 'authority',
                   'content_hash', 'provenance_hash', 'summary', 'summary_hash',
                   'source_refs', 'externally_disclosable',
                   'external_authorization_hash', 'item_hash', 'source_ref_hash'
               ])
           )
           OR item->>'record_id' !~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (item->>'record_version')::integer < 1
           OR item->>'layer' NOT IN (
                'CASE_LONG_TERM', 'LAWYER_PERSONAL', 'FIRM_KNOWLEDGE',
                'PUBLIC_LEGAL'
           )
           OR item->>'authority' NOT IN (
                'CASE_EVIDENCE', 'CONFIRMED_CASE_LEDGER', 'LAWYER_NOTE',
                'FIRM_SOP', 'PRIMARY_LAW', 'JUDICIAL_INTERPRETATION',
                'OFFICIAL_CASE'
           )
           OR item->>'content_hash' !~ '^[0-9a-f]{64}$'
           OR item->>'provenance_hash' !~ '^[0-9a-f]{64}$'
           OR item->>'summary_hash' !~ '^[0-9a-f]{64}$'
           OR item->>'item_hash' !~ '^[0-9a-f]{64}$'
           OR item->>'source_ref_hash' !~ '^[0-9a-f]{64}$'
           OR octet_length(item->>'summary') NOT BETWEEN 1 AND 300
           OR encode(
                digest(convert_to(item->>'summary', 'UTF8'), 'sha256'), 'hex'
              ) <> item->>'summary_hash'
           OR jsonb_typeof(item->'source_refs') <> 'array'
           OR jsonb_array_length(item->'source_refs') NOT BETWEEN 1 AND 50
           OR jsonb_typeof(item->'externally_disclosable') <> 'boolean'
           OR (
                item->>'layer' = 'CASE_LONG_TERM'
                AND (item->>'externally_disclosable')::boolean = false
                AND item->'external_authorization_hash' <> 'null'::jsonb
           )
           OR (
                item->>'layer' = 'CASE_LONG_TERM'
                AND (item->>'externally_disclosable')::boolean = true
                AND COALESCE(item->>'external_authorization_hash', '')
                    !~ '^[0-9a-f]{64}$'
           )
           OR (
                item->>'layer' <> 'CASE_LONG_TERM'
                AND (
                    (item->>'externally_disclosable')::boolean = false
                    OR item->'external_authorization_hash' <> 'null'::jsonb
                )
           )
           OR NOT EXISTS (
               SELECT 1
               FROM jsonb_array_elements(retrieval.returned_record_refs) returned
               WHERE returned->>'record_id' = item->>'record_id'
                 AND returned->>'content_hash' = item->>'content_hash'
           )
           OR NOT EXISTS (
               SELECT 1
               FROM jsonb_array_elements(retrieval.authorized_record_refs) authorized
               WHERE authorized->>'record_id' = item->>'record_id'
                 AND (authorized->>'record_version')::integer =
                     (item->>'record_version')::integer
                 AND authorized->>'content_hash' = item->>'content_hash'
                 AND authorized->>'provenance_hash' = item->>'provenance_hash'
           ) THEN
            RAISE EXCEPTION 'planning memory enrichment contains an invalid item';
        END IF;
        FOR source IN SELECT * FROM jsonb_array_elements(item->'source_refs') LOOP
            IF NOT source ?& ARRAY[
                    'source_type', 'source_id', 'source_version',
                    'content_hash', 'exposure', 'page_number'
               ]
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_object_keys(source) AS source_keys(key_name)
                   WHERE key_name <> ALL (ARRAY[
                       'source_type', 'source_id', 'source_version',
                       'content_hash', 'exposure', 'page_number'
                   ])
               )
               OR source->>'source_type' !~
                  '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
               OR length(trim(source->>'source_id')) NOT BETWEEN 1 AND 300
               OR length(trim(source->>'source_version')) NOT BETWEEN 1 AND 300
               OR source->>'content_hash' !~ '^[0-9a-f]{64}$'
               OR source->>'exposure' NOT IN (
                    'CASE_PRIVATE', 'PUBLISHED_SANITIZED', 'PUBLIC_OFFICIAL'
               )
               OR (
                    source->'page_number' <> 'null'::jsonb
                    AND (source->>'page_number')::integer < 1
               )
               OR (
                    item->>'layer' = 'CASE_LONG_TERM'
                    AND source->>'exposure' <> 'CASE_PRIVATE'
               )
               OR (
                    item->>'layer' IN ('LAWYER_PERSONAL', 'FIRM_KNOWLEDGE')
                    AND source->>'exposure' <> 'PUBLISHED_SANITIZED'
               )
               OR (
                    item->>'layer' = 'PUBLIC_LEGAL'
                    AND source->>'exposure' <> 'PUBLIC_OFFICIAL'
               ) THEN
                RAISE EXCEPTION 'planning memory enrichment source reference is invalid';
            END IF;
        END LOOP;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_planning_memory_enrichment_guard
    BEFORE INSERT ON case_agent_planning_memory_enrichments
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_planning_memory_enrichment();

CREATE FUNCTION prohibit_case_agent_planning_memory_enrichment_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'planning memory enrichments are append-only';
END;
$$;

CREATE TRIGGER case_agent_planning_memory_enrichments_append_only
    BEFORE UPDATE OR DELETE ON case_agent_planning_memory_enrichments
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_planning_memory_enrichment_mutation();

ALTER TABLE case_agent_planning_memory_enrichments ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_planning_memory_enrichments FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_planning_memory_enrichments_firm_isolation
    ON case_agent_planning_memory_enrichments
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE INDEX case_agent_planning_memory_enrichment_run_lookup
    ON case_agent_planning_memory_enrichments(
        firm_id, matter_id, run_id, purpose, created_at DESC
    );

COMMIT;
