-- Active lifecycle for the bounded routes recorded by migration 0047.
--
-- 0047's RESOLVED batch status means that every immutable exception group has
-- been routed.  It does not mean that a request to re-extract, obtain more
-- evidence, or defer review has been completed.  This migration materializes
-- those three routes as current ACTIVE follow-ups and projects only their
-- current heads.  Historical events remain append-only and never accumulate
-- as permanently active planning signals.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
         WHERE rolname = 'lawcase_ledger_confirmation_owner'
           AND NOT rolcanlogin AND NOT rolinherit
           AND NOT rolsuper AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION
            'lawcase_ledger_confirmation_owner must remain a NOLOGIN NOINHERIT NOBYPASSRLS role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
         WHERE rolname = 'lawcase_web_application'
    ) THEN
        RAISE EXCEPTION 'lawcase_web_application role is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
         WHERE rolname = 'lawcase_agent_worker'
    ) THEN
        RAISE EXCEPTION 'lawcase_agent_worker role is missing';
    END IF;
    IF pg_catalog.pg_has_role(
        'lawcase_web_application',
        'lawcase_ledger_confirmation_owner',
        'MEMBER'
    ) OR pg_catalog.pg_has_role(
        'lawcase_agent_worker',
        'lawcase_ledger_confirmation_owner',
        'MEMBER'
    ) THEN
        RAISE EXCEPTION
            'application roles must not inherit the ledger lifecycle owner';
    END IF;
END;
$$;

-- 0049 is also an upgrade migration.  A database that already applied 0046
-- and 0047 will not re-run those historical CREATE FUNCTION statements.
-- The lifecycle definers below deliberately use pg_catalog-only search paths,
-- so every older trigger/helper they can reach must carry its own trusted
-- search path before the first 0049 backfill or trigger invocation.  Revoke
-- untrusted schema creation first so `public` cannot be shadowed by either
-- application role or by the isolated definer owner.
REVOKE CREATE ON SCHEMA public
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker,
         lawcase_ledger_confirmation_owner;

ALTER FUNCTION public.enqueue_case_agent_snapshot_refresh_from_ledger_confirmation()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.wake_case_agent_run_for_snapshot_refresh()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.guard_case_agent_snapshot_refresh_request()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.validate_case_agent_ledger_exception_decision()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.audit_case_agent_ledger_exception_decision()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.case_agent_ledger_extraction_target_matches_candidate(
    uuid, uuid, uuid, uuid, text, uuid
) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.enforce_case_agent_ledger_extraction_promotion_target()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.enforce_case_agent_ledger_extraction_confirmation_integrity()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.materialize_case_agent_ledger_exception_groups(
    uuid, uuid, uuid
) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.validate_case_agent_ledger_exception_group_integrity(
    uuid, uuid, uuid
) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.enforce_case_agent_ledger_exception_group_integrity()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.case_agent_ledger_extraction_batch_review_status(
    uuid, uuid, uuid
) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.case_agent_ledger_extraction_run_staging_complete(
    uuid, uuid, uuid
) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.case_agent_ledger_extraction_current_review_version(
    uuid, uuid, uuid
) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.case_agent_ledger_extraction_run_review_resolved(
    uuid, uuid, uuid
) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.unblock_case_agent_snapshot_refresh_after_exception_decision()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.normalize_case_agent_snapshot_refresh_review_gate()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.enqueue_case_agent_snapshot_refresh_from_exception_only_run()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.block_unresolved_ledger_review_work_plan_promotion()
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.block_unresolved_ledger_review_work_plan_activation()
    SET search_path = pg_catalog, public, pg_temp;

-- Existing 0048 installations may predate the row-lock entitlements added to
-- the source migration.  PostgreSQL requires UPDATE privilege on at least one
-- column for SELECT ... FOR SHARE; grant only immutable identity columns to
-- the NOLOGIN owner before lifecycle backfill takes any authority/run locks.
GRANT UPDATE (session_id) ON TABLE public.web_sessions
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (user_id) ON TABLE public.users
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (user_id) ON TABLE public.matter_actor_roles
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (run_id) ON TABLE public.case_agent_runs
    TO lawcase_ledger_confirmation_owner;

-- Preserve run lineage in later composite foreign keys.  The batch id is
-- already globally unique; this redundant named key lets PostgreSQL enforce
-- the exact batch/run/tenant tuple rather than trusting application code.
ALTER TABLE public.case_agent_ledger_extraction_batches
    ADD CONSTRAINT case_agent_ledger_extraction_batches_id_run_tenant_unique
    UNIQUE (extraction_batch_id, run_id, firm_id, matter_id);

-- A control transfer changes only the Agent orchestration cursor.  It does
-- not invent a legal-ledger mutation, so its audit record remains on the
-- current matter version just like the allowlisted 0044 item review.
ALTER TABLE public.audit_events
    DROP CONSTRAINT audit_events_version_transition_valid;
ALTER TABLE public.audit_events
    ADD CONSTRAINT audit_events_version_transition_valid CHECK (
        output_version > input_version
        OR (
            event_type IN (
                'CASE_WORK_PLAN_ITEM_REVIEWED',
                'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED'
            )
            AND output_version = input_version
        )
    ) NOT VALID;
ALTER TABLE public.audit_events
    VALIDATE CONSTRAINT audit_events_version_transition_valid;


CREATE FUNCTION public.case_agent_ledger_exception_followup_request_hash(
    input_matter_id uuid,
    input_expected_version integer,
    input_followup_id uuid,
    input_action text,
    input_managed_evidence_request_id uuid,
    input_reextraction_batch_id uuid,
    input_managed_evidence_sources jsonb,
    input_reason_note text
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $$
    WITH source_set AS (
        SELECT COALESCE(pg_catalog.string_agg(
            source.value->>'object_type' || ':' ||
                (source.value->>'object_id')::uuid::text,
            E'\n' ORDER BY source.value->>'object_type',
                (source.value->>'object_id')::uuid
        ), '') AS canonical_sources
          FROM pg_catalog.jsonb_array_elements(
              COALESCE(input_managed_evidence_sources, '[]'::jsonb)
          ) source(value)
    )
    SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
        'case-ledger-exception-followup-request-v2' || E'\n' ||
        input_matter_id::text || E'\n' ||
        input_expected_version::text || E'\n' ||
        input_followup_id::text || E'\n' ||
        input_action || E'\n' ||
        COALESCE(input_managed_evidence_request_id::text, '') || E'\n' ||
        COALESCE(input_reextraction_batch_id::text, '') || E'\n' ||
        pg_catalog.octet_length(source_set.canonical_sources)::text || ':' ||
        source_set.canonical_sources || E'\n' ||
        pg_catalog.octet_length(COALESCE(input_reason_note, ''))::text || ':' ||
        COALESCE(input_reason_note, ''),
        'UTF8'
    ), 'sha256'), 'hex')::char(64)
      FROM source_set
$$;

CREATE FUNCTION public.case_agent_ledger_exception_control_transfer_request_hash(
    input_matter_id uuid,
    input_expected_version integer,
    input_replacement_run_id uuid
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
        'case-ledger-exception-control-transfer-v1' || E'\n' ||
        input_matter_id::text || E'\n' ||
        input_expected_version::text || E'\n' ||
        input_replacement_run_id::text,
        'UTF8'
    ), 'sha256'), 'hex')::char(64)
$$;

CREATE FUNCTION public.resolve_case_agent_ledger_exception_followup_from_web_session(
    input_session_id uuid,
    input_matter_id uuid,
    input_followup_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_action text,
    input_managed_evidence_request_id uuid,
    input_managed_evidence_sources jsonb,
    input_reason_note text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    session_row record;
    followup_row record;
    prior_row record;
    normalized_note text;
    normalized_sources jsonb;
    expected_request_hash char(64);
    new_state text;
    new_event_type text;
    new_event_id uuid;
    new_event_hash char(64);
    audit_id uuid;
    next_version integer;
    response_json jsonb;
    source_item jsonb;
    source_row record;
    source_binding_id uuid;
    source_binding_hash char(64);
    source_set_hash char(64);
    source_count integer := 0;
BEGIN
    normalized_note := NULLIF(pg_catalog.btrim(input_reason_note), '');
    normalized_sources := COALESCE(input_managed_evidence_sources, '[]'::jsonb);
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_action NOT IN (
            'CONFIRM_MORE_EVIDENCE', 'RESUME', 'WITHDRAW', 'SUPERSEDE'
       )
       OR normalized_note IS NULL
       OR pg_catalog.length(normalized_note) > 500
       OR pg_catalog.octet_length(normalized_note) > 2000
       OR normalized_note ~ '[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F]'
       OR pg_catalog.jsonb_typeof(normalized_sources) <> 'array'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'ledger exception follow-up command input is invalid';
    END IF;
    expected_request_hash :=
        public.case_agent_ledger_exception_followup_request_hash(
            input_matter_id, input_expected_version, input_followup_id,
            input_action, input_managed_evidence_request_id, NULL,
            normalized_sources,
            normalized_note
        );
    IF input_request_hash IS DISTINCT FROM expected_request_hash THEN
        RAISE EXCEPTION 'ledger exception follow-up request hash differs';
    END IF;

    -- The tenant and actor come only from the opaque, live server session.
    PERFORM pg_catalog.set_config('app.firm_id', '', true);
    PERFORM pg_catalog.set_config(
        'app.web_session_id', input_session_id::text, true
    );
    SELECT session.session_id, session.firm_id, session.user_id,
           session.issuer, session.authenticated_at, session.expires_at
      INTO session_row
      FROM public.web_sessions session
     WHERE session.session_id = input_session_id
       AND session.revoked_at IS NULL
       AND session.expires_at > pg_catalog.clock_timestamp()
       AND session.authenticated_at <= pg_catalog.clock_timestamp()
       AND session.issuer LIKE 'https://%'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception follow-up requires a live OIDC/MFA Web session';
    END IF;
    PERFORM pg_catalog.set_config(
        'app.firm_id', session_row.firm_id::text, true
    );

    -- Stable lock order: exact session -> matter -> user -> role -> head.
    PERFORM 1
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception follow-up matter is missing';
    END IF;
    PERFORM 1
      FROM public.users actor
     WHERE actor.user_id = session_row.user_id
       AND actor.firm_id = session_row.firm_id
       AND actor.status = 'ACTIVE'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception follow-up actor is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.matter_id = input_matter_id
       AND role_binding.firm_id = session_row.firm_id
       AND role_binding.user_id = session_row.user_id
       AND role_binding.role = 'LEAD_LAWYER'
       AND role_binding.revoked_at IS NULL
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception follow-up requires the current lead lawyer';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        session_row.user_id::text || '|' || input_matter_id::text ||
        '|RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP|' || input_idempotency_key,
        0
    ));

    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = session_row.firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = session_row.user_id
       AND idempotency.command_name =
            'RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP'
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                input_idempotency_key
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                (input_expected_version + 1)::text
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXCEPTION_FOLLOWUP'
           OR prior_row.response_json->>'object_id' IS DISTINCT FROM
                input_followup_id::text
           OR NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_followup_events event
                  JOIN public.audit_events audit
                    ON audit.event_id = event.audit_event_id
                   AND audit.firm_id = event.firm_id
                   AND audit.matter_id = event.matter_id
                 WHERE event.followup_id = input_followup_id
                   AND event.firm_id = session_row.firm_id
                   AND event.matter_id = input_matter_id
                   AND event.actor_id = session_row.user_id
                   AND event.web_session_id = input_session_id
                   AND event.request_hash = input_request_hash
                   AND event.idempotency_key = input_idempotency_key
                   AND event.audit_event_id =
                        (prior_row.response_json->>'audit_event_id')::uuid
                   AND audit.input_version = input_expected_version
                   AND audit.output_version = input_expected_version + 1
           ) THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger exception follow-up idempotency replay differs',
                ERRCODE = 'P4092';
        END IF;
        RETURN prior_row.response_json;
    END IF;

    SELECT followup.followup_kind, followup.subject_hash,
           followup.origin_exception_decision_id,
           followup.origin_exception_group_id,
           followup.origin_extraction_batch_id, followup.origin_run_id,
           head.current_state, head.head_sequence,
           request.evidence_request_id AS managed_evidence_request_id
           , request.created_at AS managed_evidence_requested_at
      INTO followup_row
      FROM public.case_agent_ledger_exception_followups followup
      JOIN public.case_agent_ledger_exception_followup_heads head
        ON head.followup_id = followup.followup_id
       AND head.firm_id = followup.firm_id
       AND head.matter_id = followup.matter_id
      LEFT JOIN public.case_agent_ledger_exception_managed_evidence_requests request
        ON request.followup_id = followup.followup_id
       AND request.firm_id = followup.firm_id
       AND request.matter_id = followup.matter_id
     WHERE followup.followup_id = input_followup_id
       AND followup.firm_id = session_row.firm_id
       AND followup.matter_id = input_matter_id
     FOR UPDATE OF head;
    IF NOT FOUND OR followup_row.current_state <> 'ACTIVE' THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception follow-up is not current and active',
            ERRCODE = 'P4092';
    END IF;

    IF input_action = 'CONFIRM_MORE_EVIDENCE' THEN
        IF followup_row.followup_kind <> 'MORE_EVIDENCE'
           OR input_managed_evidence_request_id IS NULL
           OR input_managed_evidence_request_id IS DISTINCT FROM
                followup_row.managed_evidence_request_id THEN
            RAISE EXCEPTION
                'more-evidence confirmation differs from its managed request';
        END IF;
        IF pg_catalog.jsonb_array_length(normalized_sources) NOT BETWEEN 1 AND 100
           OR EXISTS (
                SELECT 1
                  FROM pg_catalog.jsonb_array_elements(normalized_sources) source(value)
                 WHERE pg_catalog.jsonb_typeof(source.value) <> 'object'
                    OR (SELECT pg_catalog.count(*)
                          FROM pg_catalog.jsonb_object_keys(source.value)) <> 2
                    OR NOT (source.value ? 'object_type')
                    OR NOT (source.value ? 'object_id')
                    OR source.value->>'object_type' NOT IN (
                        'EVIDENCE_FILE', 'MATERIAL_OBJECT'
                    )
                    OR source.value->>'object_id' !~
                        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           ) OR (
                SELECT pg_catalog.count(*)
                  FROM pg_catalog.jsonb_array_elements(normalized_sources)
           ) <> (
                SELECT pg_catalog.count(DISTINCT (
                    source.value->>'object_type',
                    source.value->>'object_id'
                ))
                  FROM pg_catalog.jsonb_array_elements(normalized_sources) source(value)
           ) THEN
            RAISE EXCEPTION
                'more-evidence confirmation requires a non-empty exact managed source set';
        END IF;
        new_event_type := 'MORE_EVIDENCE_CONFIRMED';
        new_state := 'SATISFIED';
    ELSIF input_action = 'RESUME' THEN
        IF followup_row.followup_kind <> 'DEFERRED_REVIEW'
           OR input_managed_evidence_request_id IS NOT NULL
           OR normalized_sources <> '[]'::jsonb THEN
            RAISE EXCEPTION 'only a deferred follow-up can be resumed';
        END IF;
        new_event_type := 'DEFER_RESUMED';
        new_state := 'RESUMED';
    ELSIF input_action = 'WITHDRAW' THEN
        IF input_managed_evidence_request_id IS NOT NULL
           OR normalized_sources <> '[]'::jsonb THEN
            RAISE EXCEPTION 'withdraw cannot bind an evidence request';
        END IF;
        new_event_type := 'FOLLOWUP_WITHDRAWN';
        new_state := 'WITHDRAWN';
    ELSIF input_action = 'SUPERSEDE' THEN
        IF input_managed_evidence_request_id IS NOT NULL
           OR normalized_sources <> '[]'::jsonb THEN
            RAISE EXCEPTION 'supersede cannot bind an evidence request';
        END IF;
        new_event_type := 'FOLLOWUP_SUPERSEDED';
        new_state := 'SUPERSEDED';
    END IF;

    UPDATE public.matters matter
       SET version = matter.version + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
       AND matter.version = input_expected_version
     RETURNING matter.version INTO next_version;
    IF next_version IS NULL THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception follow-up matter version is stale',
            ERRCODE = 'P4091';
    END IF;

    IF input_action = 'CONFIRM_MORE_EVIDENCE' THEN
        FOR source_item IN
            SELECT source.value
              FROM pg_catalog.jsonb_array_elements(normalized_sources) source(value)
             ORDER BY source.value->>'object_type',
                      (source.value->>'object_id')::uuid
        LOOP
            IF source_item->>'object_type' = 'EVIDENCE_FILE' THEN
                SELECT original.original_file_sha256 AS content_hash,
                       original.created_at
                  INTO source_row
                  FROM public.evidence_original_files original
                 WHERE original.evidence_file_id =
                        (source_item->>'object_id')::uuid
                   AND original.firm_id = session_row.firm_id
                   AND original.matter_id = input_matter_id
                   AND original.created_at >
                        followup_row.managed_evidence_requested_at
                   AND NOT EXISTS (
                        SELECT 1
                          FROM public.evidence_original_files successor
                         WHERE successor.supersedes_file_id =
                                original.evidence_file_id
                           AND successor.firm_id = original.firm_id
                           AND successor.matter_id = original.matter_id
                   );
            ELSE
                SELECT material.content_sha256 AS content_hash,
                       material.created_at
                  INTO source_row
                  FROM public.case_material_objects material
                 WHERE material.material_object_id =
                        (source_item->>'object_id')::uuid
                   AND material.firm_id = session_row.firm_id
                   AND material.matter_id = input_matter_id
                   AND material.created_at >
                        followup_row.managed_evidence_requested_at;
            END IF;
            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'managed evidence source is missing, unrelated, or predates its request';
            END IF;
            source_binding_id := public.gen_random_uuid();
            source_binding_hash := pg_catalog.encode(public.digest(
                pg_catalog.convert_to(pg_catalog.jsonb_build_object(
                    'schema_version',
                        'case-ledger-managed-evidence-source-binding-v1',
                    'evidence_request_id',
                        input_managed_evidence_request_id,
                    'followup_id', input_followup_id,
                    'source_type', source_item->>'object_type',
                    'source_object_id', source_item->>'object_id',
                    'source_content_hash', source_row.content_hash
                )::text, 'UTF8'
            ), 'sha256'), 'hex');
            INSERT INTO public.case_agent_ledger_exception_evidence_source_bindings (
                source_binding_id, evidence_request_id, followup_id,
                firm_id, matter_id, source_type, source_object_id,
                source_content_hash, bound_by, expected_matter_version,
                request_hash, binding_hash
            ) VALUES (
                source_binding_id, input_managed_evidence_request_id,
                input_followup_id, session_row.firm_id, input_matter_id,
                source_item->>'object_type',
                (source_item->>'object_id')::uuid, source_row.content_hash,
                session_row.user_id, input_expected_version,
                input_request_hash, source_binding_hash
            );
            source_count := source_count + 1;
        END LOOP;
        SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
                   pg_catalog.string_agg(
                       binding.source_type || ':' ||
                       binding.source_object_id::text || ':' ||
                       binding.source_content_hash,
                       E'\n' ORDER BY binding.source_type,
                           binding.source_object_id
                   ), 'UTF8'
               ), 'sha256'), 'hex')
          INTO source_set_hash
          FROM public.case_agent_ledger_exception_evidence_source_bindings binding
         WHERE binding.evidence_request_id =
                input_managed_evidence_request_id
           AND binding.followup_id = input_followup_id
           AND binding.firm_id = session_row.firm_id
           AND binding.matter_id = input_matter_id;
        IF source_count < 1 OR source_set_hash IS NULL THEN
            RAISE EXCEPTION 'managed evidence source binding is incomplete';
        END IF;
    END IF;

    new_event_id := public.gen_random_uuid();
    audit_id := public.gen_random_uuid();
    new_event_hash := pg_catalog.encode(public.digest(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema_version', 'case-ledger-exception-followup-event-v1',
            'followup_id', input_followup_id,
            'event_sequence', followup_row.head_sequence + 1,
            'event_type', new_event_type,
            'state_after', new_state,
            'managed_evidence_request_id', input_managed_evidence_request_id,
            'managed_evidence_source_set_hash', source_set_hash,
            'managed_evidence_source_count', source_count,
            'reason_note', normalized_note,
            'request_hash', input_request_hash
        )::text, 'UTF8'), 'sha256'
    ), 'hex');
    INSERT INTO public.audit_events (
        event_id, firm_id, matter_id, actor_id, event_type,
        input_version, output_version, request_id, payload
    ) VALUES (
        audit_id, session_row.firm_id, input_matter_id, session_row.user_id,
        'CASE_LEDGER_EXCEPTION_' || new_event_type,
        input_expected_version, next_version, public.gen_random_uuid(),
        pg_catalog.jsonb_build_object(
            'followup_id', input_followup_id,
            'followup_event_id', new_event_id,
            'origin_exception_decision_id',
                followup_row.origin_exception_decision_id,
            'origin_run_id', followup_row.origin_run_id,
            'origin_extraction_batch_id',
                followup_row.origin_extraction_batch_id,
            'action', input_action,
            'state_after', new_state,
            'managed_evidence_source_set_hash', source_set_hash,
            'managed_evidence_source_count', source_count,
            'reason_note_sha256', pg_catalog.encode(public.digest(
                pg_catalog.convert_to(normalized_note, 'UTF8'), 'sha256'
            ), 'hex'),
            'formal_ledger_write', false,
            'legal_conclusion', false
        )
    );
    INSERT INTO public.case_agent_ledger_exception_followup_events (
        followup_event_id, followup_id, event_sequence, firm_id, matter_id,
        subject_hash, event_type, state_after, actor_id, web_session_id,
        expected_matter_version, managed_evidence_request_id,
        managed_evidence_source_set_hash, managed_evidence_source_count,
        reason_note, idempotency_key, request_hash, event_hash, audit_event_id
    ) VALUES (
        new_event_id, input_followup_id, followup_row.head_sequence + 1,
        session_row.firm_id, input_matter_id, followup_row.subject_hash,
        new_event_type, new_state, session_row.user_id, input_session_id,
        input_expected_version,
        CASE WHEN input_action = 'CONFIRM_MORE_EVIDENCE'
             THEN input_managed_evidence_request_id ELSE NULL END,
        source_set_hash,
        CASE WHEN input_action = 'CONFIRM_MORE_EVIDENCE'
             THEN source_count ELSE NULL END,
        normalized_note, input_idempotency_key, input_request_hash,
        new_event_hash, audit_id
    );
    UPDATE public.case_agent_ledger_exception_followup_heads head
       SET current_state = new_state,
           head_event_id = new_event_id,
           head_sequence = head.head_sequence + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE head.followup_id = input_followup_id
       AND head.firm_id = session_row.firm_id
       AND head.matter_id = input_matter_id
       AND head.current_state = 'ACTIVE'
       AND head.head_sequence = followup_row.head_sequence;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception follow-up head changed before commit',
            ERRCODE = 'P4092';
    END IF;
    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        session_row.firm_id, input_matter_id, next_version,
        'CASE_LEDGER_EXCEPTION_' || new_event_type,
        pg_catalog.jsonb_build_object(
            'audit_event_id', audit_id,
            'object_id', input_followup_id,
            'followup_event_id', new_event_id
        )
    );
    response_json := pg_catalog.jsonb_build_object(
        'command_name', 'RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP',
        'idempotency_key', input_idempotency_key,
        'matter_id', input_matter_id::text,
        'matter_version', next_version,
        'audit_event_id', audit_id::text,
        'object_type', 'CASE_LEDGER_EXCEPTION_FOLLOWUP',
        'object_id', input_followup_id::text
    );
    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        session_row.firm_id, input_matter_id, session_row.user_id,
        'RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP', input_idempotency_key,
        input_request_hash, response_json
    );
    RETURN response_json;
END;
$$;

CREATE FUNCTION public.case_agent_ledger_exception_reextraction_task_binding_request_hash(
    input_matter_id uuid,
    input_expected_version integer,
    input_followup_id uuid,
    input_run_id uuid,
    input_graph_id uuid,
    input_task_id uuid
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
        'case-ledger-exception-reextraction-task-binding-v1' || E'\n' ||
        input_matter_id::text || E'\n' ||
        input_expected_version::text || E'\n' ||
        input_followup_id::text || E'\n' ||
        input_run_id::text || E'\n' ||
        input_graph_id::text || E'\n' ||
        input_task_id::text,
        'UTF8'
    ), 'sha256'), 'hex')::char(64)
$$;

CREATE FUNCTION public.bind_case_agent_ledger_exception_reextraction_task_from_worker(
    input_worker_id uuid,
    input_firm_id uuid,
    input_matter_id uuid,
    input_followup_id uuid,
    input_run_id uuid,
    input_graph_id uuid,
    input_task_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_matter_version integer;
    worker_is_dedicated boolean;
    prior_row record;
    prior_binding_row record;
    followup_row record;
    task_row record;
    expected_request_hash char(64);
    required_source_count integer;
    required_source_set_hash char(64);
    new_binding_id uuid;
    new_binding_hash char(64);
    next_binding_sequence integer;
    response_json jsonb;
BEGIN
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'ledger re-extraction task binding input is invalid';
    END IF;
    expected_request_hash :=
        public.case_agent_ledger_exception_reextraction_task_binding_request_hash(
            input_matter_id, input_expected_version, input_followup_id,
            input_run_id, input_graph_id, input_task_id
        );
    IF input_request_hash IS DISTINCT FROM expected_request_hash THEN
        RAISE EXCEPTION 'ledger re-extraction task binding hash differs';
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', input_firm_id::text, true);

    SELECT matter.version
      INTO current_matter_version
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = input_firm_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction task binding matter is missing';
    END IF;
    PERFORM 1
      FROM public.users worker
     WHERE worker.user_id = input_worker_id
       AND worker.firm_id = input_firm_id
       AND worker.status = 'ACTIVE'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger re-extraction task binding worker is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.user_id = input_worker_id
       AND role_binding.firm_id = input_firm_id
       AND role_binding.matter_id = input_matter_id
       AND role_binding.revoked_at IS NULL
     FOR SHARE;
    SELECT pg_catalog.count(*) = 1
           AND pg_catalog.bool_and(role_binding.role = 'SYSTEM_WORKER')
      INTO worker_is_dedicated
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.user_id = input_worker_id
       AND role_binding.firm_id = input_firm_id
       AND role_binding.matter_id = input_matter_id
       AND role_binding.revoked_at IS NULL;
    IF worker_is_dedicated IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'ledger re-extraction task binding requires the dedicated SYSTEM_WORKER';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        input_worker_id::text || '|' || input_matter_id::text ||
        '|BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK|' || input_idempotency_key,
        0
    ));

    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = input_firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = input_worker_id
       AND idempotency.command_name =
            'BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK'
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                input_expected_version::text
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXCEPTION_REEXTRACTION_TASK_BINDING'
           OR NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_reextraction_task_bindings binding
                 WHERE binding.task_binding_id =
                        (prior_row.response_json->>'object_id')::uuid
                   AND binding.followup_id = input_followup_id
                   AND binding.run_id = input_run_id
                   AND binding.graph_id = input_graph_id
                   AND binding.task_id = input_task_id
                   AND binding.firm_id = input_firm_id
                   AND binding.matter_id = input_matter_id
                   AND binding.bound_by = input_worker_id
                   AND binding.request_hash = input_request_hash
           ) THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger re-extraction task binding idempotency replay differs',
                ERRCODE = 'P4092';
        END IF;
        RETURN prior_row.response_json;
    END IF;
    IF current_matter_version <> input_expected_version THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction task binding matter version is stale',
            ERRCODE = 'P4091';
    END IF;

    SELECT followup.origin_exception_decision_id,
           followup.origin_exception_group_id,
           followup.origin_extraction_batch_id,
           followup.origin_run_id,
           control_assignment.control_run_id,
           control_head.current_state AS control_state,
           control_head.current_control_assignment_id,
           decision.decided_at,
           origin_batch.graph_id AS origin_graph_id,
           origin_graph.graph_version AS origin_graph_version,
           head.current_state
      INTO followup_row
      FROM public.case_agent_ledger_exception_followups followup
      JOIN public.case_agent_ledger_exception_followup_heads head
        ON head.followup_id = followup.followup_id
       AND head.firm_id = followup.firm_id
       AND head.matter_id = followup.matter_id
      JOIN public.case_agent_ledger_exception_control_heads control_head
        ON control_head.firm_id = followup.firm_id
       AND control_head.matter_id = followup.matter_id
      JOIN public.case_agent_ledger_exception_control_assignments
            control_assignment
        ON control_assignment.control_assignment_id =
                control_head.current_control_assignment_id
       AND control_assignment.firm_id = control_head.firm_id
       AND control_assignment.matter_id = control_head.matter_id
       AND control_assignment.state_after = control_head.current_state
       AND control_assignment.assignment_sequence =
                control_head.head_sequence
      JOIN public.case_agent_ledger_exception_group_decisions decision
        ON decision.exception_decision_id =
                followup.origin_exception_decision_id
       AND decision.firm_id = followup.firm_id
       AND decision.matter_id = followup.matter_id
      JOIN public.case_agent_ledger_extraction_batches origin_batch
        ON origin_batch.extraction_batch_id =
                followup.origin_extraction_batch_id
       AND origin_batch.firm_id = followup.firm_id
       AND origin_batch.matter_id = followup.matter_id
      JOIN public.case_agent_task_graphs origin_graph
        ON origin_graph.graph_id = origin_batch.graph_id
       AND origin_graph.run_id = origin_batch.run_id
       AND origin_graph.firm_id = origin_batch.firm_id
       AND origin_graph.matter_id = origin_batch.matter_id
     WHERE followup.followup_id = input_followup_id
       AND followup.firm_id = input_firm_id
       AND followup.matter_id = input_matter_id
       AND followup.followup_kind = 'REEXTRACTION'
     FOR SHARE OF head, control_head;
    IF NOT FOUND OR followup_row.current_state <> 'ACTIVE' THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction task binding follow-up is not active',
            ERRCODE = 'P4092';
    END IF;

    SELECT binding_head.current_task_binding_id,
           binding_head.binding_sequence,
           binding_head.current_graph_version,
           current_binding.run_id,
           current_binding.graph_id,
           current_binding.task_id,
           current_binding.expected_matter_version,
           current_binding.bound_at
      INTO prior_binding_row
      FROM public.case_agent_ledger_exception_reextraction_task_binding_heads
            binding_head
      JOIN public.case_agent_ledger_exception_reextraction_task_bindings
            current_binding
        ON current_binding.task_binding_id =
                binding_head.current_task_binding_id
       AND current_binding.followup_id = binding_head.followup_id
       AND current_binding.firm_id = binding_head.firm_id
       AND current_binding.matter_id = binding_head.matter_id
     WHERE binding_head.followup_id = input_followup_id
       AND binding_head.firm_id = input_firm_id
       AND binding_head.matter_id = input_matter_id
     FOR UPDATE OF binding_head;
    IF FOUND THEN
        next_binding_sequence := prior_binding_row.binding_sequence + 1;
    ELSE
        next_binding_sequence := 1;
    END IF;

    SELECT task.input_refs, task.input_hash, task.skill_id, task.skill_version,
           task.tool_id, task.tool_version,
           graph.graph_version, graph.graph_hash,
           graph.snapshot_matter_version AS graph_snapshot_version,
           graph.created_at,
           accepted_event.occurred_at AS accepted_at,
           run.current_graph_id, run.current_graph_version,
           run.current_graph_hash,
           run.snapshot_matter_version AS run_snapshot_version,
           run.is_stale, run.is_cancelled,
           head.status AS task_status, head.is_current
      INTO task_row
      FROM public.case_agent_tasks task
      JOIN public.case_agent_task_graphs graph
        ON graph.graph_id = task.graph_id
       AND graph.run_id = task.run_id
       AND graph.firm_id = task.firm_id
       AND graph.matter_id = task.matter_id
      JOIN public.case_agent_events accepted_event
        ON accepted_event.run_id = graph.run_id
       AND accepted_event.event_sequence = graph.accepted_event_sequence
       AND accepted_event.firm_id = graph.firm_id
       AND accepted_event.matter_id = graph.matter_id
       AND accepted_event.event_type = 'TASK_GRAPH_ACCEPTED'
      JOIN public.case_agent_runs run
        ON run.run_id = task.run_id
       AND run.firm_id = task.firm_id
       AND run.matter_id = task.matter_id
      JOIN public.case_agent_task_heads head
        ON head.graph_id = task.graph_id
       AND head.task_id = task.task_id
       AND head.run_id = task.run_id
       AND head.firm_id = task.firm_id
       AND head.matter_id = task.matter_id
     WHERE task.run_id = input_run_id
       AND task.graph_id = input_graph_id
       AND task.task_id = input_task_id
       AND task.firm_id = input_firm_id
       AND task.matter_id = input_matter_id
     FOR SHARE OF run;
    IF NOT FOUND
       OR followup_row.control_state <> 'HEALTHY'
       OR input_run_id <> followup_row.control_run_id
       OR (
            input_run_id = followup_row.origin_run_id
            AND task_row.graph_version <= followup_row.origin_graph_version
       )
       OR task_row.accepted_at <= followup_row.decided_at
       OR task_row.current_graph_id <> input_graph_id
       OR task_row.current_graph_version <> task_row.graph_version
       OR task_row.current_graph_hash <> task_row.graph_hash
       OR task_row.graph_snapshot_version <> input_expected_version
       OR task_row.run_snapshot_version <> input_expected_version
       OR task_row.is_stale OR task_row.is_cancelled
       OR task_row.is_current IS DISTINCT FROM true
       OR task_row.task_status IN ('STALE', 'CANCELLED')
       OR task_row.skill_id <> 'case_ledger_extraction'
       OR task_row.skill_version <> '1.0.0'
       OR task_row.tool_id <> 'extract_case_ledger'
       OR task_row.tool_version <> '1.0.0' THEN
        RAISE EXCEPTION
            're-extraction task is not a current newer exact extraction graph';
    END IF;
    IF next_binding_sequence > 1 AND (
        input_expected_version < prior_binding_row.expected_matter_version
        OR task_row.accepted_at <= prior_binding_row.bound_at
        OR (
            input_expected_version =
                prior_binding_row.expected_matter_version
            AND (
                prior_binding_row.run_id <> input_run_id
                OR task_row.graph_version <=
                    prior_binding_row.current_graph_version
            )
        )
    ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 're-extraction task rebind is not a newer current graph',
            ERRCODE = 'P4092';
    END IF;

    SELECT pg_catalog.count(DISTINCT page.evidence_page_id)::integer,
           pg_catalog.encode(public.digest(pg_catalog.convert_to(
               pg_catalog.string_agg(
                   DISTINCT 'evidence-page:' || page.evidence_page_id::text,
                   E'\n' ORDER BY 'evidence-page:' || page.evidence_page_id::text
               ), 'UTF8'
           ), 'sha256'), 'hex')
      INTO required_source_count, required_source_set_hash
      FROM public.case_agent_ledger_exception_group_members member
      JOIN public.case_agent_ledger_extraction_candidate_pages page
        ON page.extraction_candidate_id = member.extraction_candidate_id
       AND page.firm_id = member.firm_id
       AND page.matter_id = member.matter_id
     WHERE member.exception_group_id =
            followup_row.origin_exception_group_id
       AND member.extraction_batch_id =
            followup_row.origin_extraction_batch_id
       AND member.firm_id = input_firm_id
       AND member.matter_id = input_matter_id;
    IF required_source_count < 1 OR required_source_set_hash IS NULL
       OR pg_catalog.jsonb_array_length(task_row.input_refs) <>
            required_source_count
       OR (SELECT pg_catalog.count(DISTINCT ref.value)
             FROM pg_catalog.jsonb_array_elements_text(task_row.input_refs) ref(value)
          ) <> required_source_count
       OR EXISTS (
            SELECT 1
              FROM pg_catalog.jsonb_array_elements_text(task_row.input_refs) ref(value)
             WHERE NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_group_members member
                  JOIN public.case_agent_ledger_extraction_candidate_pages page
                    ON page.extraction_candidate_id =
                            member.extraction_candidate_id
                   AND page.firm_id = member.firm_id
                   AND page.matter_id = member.matter_id
                 WHERE member.exception_group_id =
                        followup_row.origin_exception_group_id
                   AND member.extraction_batch_id =
                        followup_row.origin_extraction_batch_id
                   AND member.firm_id = input_firm_id
                   AND member.matter_id = input_matter_id
                   AND ref.value =
                        'evidence-page:' || page.evidence_page_id::text
             )
       ) THEN
        RAISE EXCEPTION
            're-extraction task does not exactly cover every origin exception source page';
    END IF;

    new_binding_id := public.gen_random_uuid();
    new_binding_hash := pg_catalog.encode(public.digest(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema_version',
                'case-ledger-exception-reextraction-task-binding-v2',
            'followup_id', input_followup_id,
            'origin_exception_decision_id',
                followup_row.origin_exception_decision_id,
            'binding_sequence', next_binding_sequence,
            'supersedes_task_binding_id',
                prior_binding_row.current_task_binding_id,
            'run_id', input_run_id,
            'graph_id', input_graph_id,
            'graph_version', task_row.graph_version,
            'graph_hash', task_row.graph_hash,
            'task_id', input_task_id,
            'task_input_hash', task_row.input_hash,
            'required_source_set_hash', required_source_set_hash,
            'required_source_count', required_source_count,
            'request_hash', input_request_hash
        )::text, 'UTF8'
    ), 'sha256'), 'hex');
    INSERT INTO public.case_agent_ledger_exception_reextraction_task_bindings (
        task_binding_id, followup_id, origin_exception_decision_id,
        binding_sequence, supersedes_task_binding_id,
        run_id, graph_id, graph_version, graph_hash, task_id,
        task_input_hash, required_source_set_hash, required_source_count,
        firm_id, matter_id, bound_by, expected_matter_version,
        idempotency_key, request_hash, binding_hash
    ) VALUES (
        new_binding_id, input_followup_id,
        followup_row.origin_exception_decision_id,
        next_binding_sequence, prior_binding_row.current_task_binding_id,
        input_run_id, input_graph_id, task_row.graph_version,
        task_row.graph_hash, input_task_id, task_row.input_hash,
        required_source_set_hash, required_source_count,
        input_firm_id, input_matter_id, input_worker_id,
        input_expected_version, input_idempotency_key,
        input_request_hash, new_binding_hash
    );
    IF next_binding_sequence = 1 THEN
        INSERT INTO public.case_agent_ledger_exception_reextraction_task_binding_heads (
            followup_id, firm_id, matter_id, current_task_binding_id,
            binding_sequence, current_run_id, current_graph_version,
            expected_matter_version
        ) VALUES (
            input_followup_id, input_firm_id, input_matter_id,
            new_binding_id, next_binding_sequence, input_run_id,
            task_row.graph_version, input_expected_version
        );
    ELSE
        UPDATE public.case_agent_ledger_exception_reextraction_task_binding_heads head
           SET current_task_binding_id = new_binding_id,
               binding_sequence = next_binding_sequence,
               current_run_id = input_run_id,
               current_graph_version = task_row.graph_version,
               expected_matter_version = input_expected_version,
               updated_at = pg_catalog.clock_timestamp()
         WHERE head.followup_id = input_followup_id
           AND head.firm_id = input_firm_id
           AND head.matter_id = input_matter_id
           AND head.current_task_binding_id =
                prior_binding_row.current_task_binding_id
           AND head.binding_sequence = prior_binding_row.binding_sequence;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                MESSAGE = 're-extraction task binding head changed before rebind',
                ERRCODE = 'P4092';
        END IF;
    END IF;
    response_json := pg_catalog.jsonb_build_object(
        'command_name', 'BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK',
        'idempotency_key', input_idempotency_key,
        'matter_id', input_matter_id::text,
        'matter_version', input_expected_version,
        'object_type', 'CASE_LEDGER_EXCEPTION_REEXTRACTION_TASK_BINDING',
        'object_id', new_binding_id::text
    );
    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        input_firm_id, input_matter_id, input_worker_id,
        'BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK', input_idempotency_key,
        input_request_hash, response_json
    );
    RETURN response_json;
END;
$$;

CREATE FUNCTION public.satisfy_case_agent_ledger_reextraction_followup_from_worker(
    input_worker_id uuid,
    input_firm_id uuid,
    input_matter_id uuid,
    input_followup_id uuid,
    input_reextraction_batch_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    worker_is_dedicated boolean;
    followup_row record;
    task_binding_row record;
    batch_row record;
    prior_row record;
    expected_request_hash char(64);
    binding_id uuid;
    binding_hash char(64);
    new_event_id uuid;
    new_event_hash char(64);
    audit_id uuid;
    next_version integer;
    current_required_source_count integer;
    current_required_source_set_hash char(64);
    response_json jsonb;
BEGIN
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'ledger re-extraction follow-up input is invalid';
    END IF;
    expected_request_hash :=
        public.case_agent_ledger_exception_followup_request_hash(
            input_matter_id, input_expected_version, input_followup_id,
            'SATISFY_REEXTRACTION', NULL, input_reextraction_batch_id,
            '[]'::jsonb, NULL
        );
    IF input_request_hash IS DISTINCT FROM expected_request_hash THEN
        RAISE EXCEPTION 'ledger re-extraction request hash differs';
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', input_firm_id::text, true);

    -- Worker and Web commands both lock the matter before actor-role rows.
    PERFORM 1
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = input_firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger re-extraction matter is missing';
    END IF;
    PERFORM 1
      FROM public.users worker
     WHERE worker.user_id = input_worker_id
       AND worker.firm_id = input_firm_id
       AND worker.status = 'ACTIVE'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger re-extraction worker is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.user_id = input_worker_id
       AND role_binding.firm_id = input_firm_id
       AND role_binding.matter_id = input_matter_id
       AND role_binding.revoked_at IS NULL
     FOR SHARE;
    SELECT pg_catalog.count(*) = 1
           AND pg_catalog.bool_and(role_binding.role = 'SYSTEM_WORKER')
      INTO worker_is_dedicated
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.user_id = input_worker_id
       AND role_binding.firm_id = input_firm_id
       AND role_binding.matter_id = input_matter_id
       AND role_binding.revoked_at IS NULL;
    IF worker_is_dedicated IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'ledger re-extraction requires the dedicated active SYSTEM_WORKER';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        input_worker_id::text || '|' || input_matter_id::text ||
        '|SATISFY_LEDGER_EXCEPTION_REEXTRACTION_FOLLOWUP|' ||
        input_idempotency_key,
        0
    ));

    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = input_firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = input_worker_id
       AND idempotency.command_name =
            'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_FOLLOWUP'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_FOLLOWUP'
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                input_idempotency_key
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                (input_expected_version + 1)::text
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXCEPTION_FOLLOWUP'
           OR prior_row.response_json->>'object_id' IS DISTINCT FROM
                input_followup_id::text
           OR NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_reextraction_bindings binding
                  JOIN public.case_agent_ledger_exception_followup_events event
                    ON event.reextraction_binding_id =
                            binding.reextraction_binding_id
                   AND event.firm_id = binding.firm_id
                   AND event.matter_id = binding.matter_id
                  JOIN public.audit_events audit
                    ON audit.event_id = event.audit_event_id
                   AND audit.firm_id = event.firm_id
                   AND audit.matter_id = event.matter_id
                 WHERE binding.followup_id = input_followup_id
                   AND binding.reextraction_batch_id =
                        input_reextraction_batch_id
                   AND binding.firm_id = input_firm_id
                   AND binding.matter_id = input_matter_id
                   AND binding.bound_by = input_worker_id
                   AND event.request_hash = input_request_hash
                   AND event.idempotency_key = input_idempotency_key
                   AND event.audit_event_id =
                        (prior_row.response_json->>'audit_event_id')::uuid
                   AND audit.input_version = input_expected_version
                   AND audit.output_version = input_expected_version + 1
           ) THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger re-extraction idempotency replay differs',
                ERRCODE = 'P4092';
        END IF;
        RETURN prior_row.response_json;
    END IF;

    SELECT followup.followup_kind, followup.subject_hash,
           followup.origin_exception_decision_id,
           followup.origin_exception_group_id,
           followup.origin_extraction_batch_id, followup.origin_run_id,
           decision.decided_at AS origin_decided_at,
           head.current_state, head.head_sequence
      INTO followup_row
      FROM public.case_agent_ledger_exception_followups followup
      JOIN public.case_agent_ledger_exception_group_decisions decision
        ON decision.exception_decision_id =
                followup.origin_exception_decision_id
       AND decision.firm_id = followup.firm_id
       AND decision.matter_id = followup.matter_id
      JOIN public.case_agent_ledger_exception_followup_heads head
        ON head.followup_id = followup.followup_id
       AND head.firm_id = followup.firm_id
       AND head.matter_id = followup.matter_id
     WHERE followup.followup_id = input_followup_id
       AND followup.firm_id = input_firm_id
       AND followup.matter_id = input_matter_id
     FOR UPDATE OF head;
    IF NOT FOUND
       OR followup_row.followup_kind <> 'REEXTRACTION'
       OR followup_row.current_state <> 'ACTIVE' THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction follow-up is not current and active',
            ERRCODE = 'P4092';
    END IF;

    SELECT task_binding.task_binding_id, task_binding.run_id,
           task_binding.graph_id, task_binding.graph_version,
           task_binding.graph_hash, task_binding.task_id,
           task_binding.task_input_hash,
           task_binding.required_source_set_hash,
           task_binding.required_source_count,
           task_binding.bound_at
      INTO task_binding_row
      FROM public.case_agent_ledger_exception_reextraction_task_bindings task_binding
     WHERE task_binding.followup_id = input_followup_id
       AND task_binding.origin_exception_decision_id =
            followup_row.origin_exception_decision_id
       AND task_binding.run_id = followup_row.origin_run_id
       AND task_binding.firm_id = input_firm_id
       AND task_binding.matter_id = input_matter_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            're-extraction follow-up has no exact server-bound task';
    END IF;

    SELECT pg_catalog.count(DISTINCT page.evidence_page_id)::integer,
           pg_catalog.encode(public.digest(pg_catalog.convert_to(
               pg_catalog.string_agg(
                   DISTINCT 'evidence-page:' || page.evidence_page_id::text,
                   E'\n' ORDER BY 'evidence-page:' || page.evidence_page_id::text
               ), 'UTF8'
           ), 'sha256'), 'hex')
      INTO current_required_source_count, current_required_source_set_hash
      FROM public.case_agent_ledger_exception_group_members member
      JOIN public.case_agent_ledger_extraction_candidate_pages page
        ON page.extraction_candidate_id = member.extraction_candidate_id
       AND page.firm_id = member.firm_id
       AND page.matter_id = member.matter_id
     WHERE member.exception_group_id =
            followup_row.origin_exception_group_id
       AND member.extraction_batch_id =
            followup_row.origin_extraction_batch_id
       AND member.firm_id = input_firm_id
       AND member.matter_id = input_matter_id;
    IF current_required_source_count < 1
       OR current_required_source_set_hash IS NULL
       OR current_required_source_count <>
            task_binding_row.required_source_count
       OR current_required_source_set_hash <>
            task_binding_row.required_source_set_hash THEN
        RAISE EXCEPTION
            're-extraction task binding no longer matches the immutable origin source set';
    END IF;

    SELECT batch.extraction_batch_id, batch.run_id, batch.graph_id,
           batch.task_id, batch.task_input_hash,
           batch.verification_receipt_id, batch.created_at,
           receipt.outcome, run.current_graph_id,
           run.current_graph_version, run.current_graph_hash,
           run.snapshot_matter_version,
           graph.graph_version, graph.graph_hash,
           task.input_hash, task.input_refs, task.skill_id,
           task.skill_version, task.tool_id, task.tool_version
      INTO batch_row
      FROM public.case_agent_ledger_extraction_batches batch
      JOIN public.case_agent_runs run
        ON run.run_id = batch.run_id
       AND run.firm_id = batch.firm_id
       AND run.matter_id = batch.matter_id
      JOIN public.case_agent_task_graphs graph
        ON graph.graph_id = batch.graph_id
       AND graph.run_id = run.run_id
       AND graph.firm_id = run.firm_id
       AND graph.matter_id = run.matter_id
      JOIN public.case_agent_tasks task
        ON task.graph_id = batch.graph_id
       AND task.task_id = batch.task_id
       AND task.run_id = batch.run_id
       AND task.firm_id = batch.firm_id
       AND task.matter_id = batch.matter_id
      JOIN public.case_agent_verification_receipts receipt
        ON receipt.verification_receipt_id = batch.verification_receipt_id
       AND receipt.run_id = run.run_id
       AND receipt.firm_id = run.firm_id
       AND receipt.matter_id = run.matter_id
       AND receipt.outcome = 'PASSED'
       AND receipt.verification_hash = run.verification_hash
       AND receipt.snapshot_hash = run.snapshot_hash
       AND receipt.graph_hash = graph.graph_hash
     WHERE batch.extraction_batch_id = input_reextraction_batch_id
       AND batch.firm_id = input_firm_id
       AND batch.matter_id = input_matter_id
       AND batch.extraction_batch_id <>
            followup_row.origin_extraction_batch_id
       AND batch.run_id = task_binding_row.run_id
       AND batch.graph_id = task_binding_row.graph_id
       AND batch.task_id = task_binding_row.task_id
       AND batch.task_input_hash = task_binding_row.task_input_hash
       AND batch.created_at >= task_binding_row.bound_at;
    IF NOT FOUND
       OR batch_row.run_id <> followup_row.origin_run_id
       OR batch_row.current_graph_id <> task_binding_row.graph_id
       OR batch_row.current_graph_version <> task_binding_row.graph_version
       OR batch_row.current_graph_hash <> task_binding_row.graph_hash
       OR batch_row.graph_version <> task_binding_row.graph_version
       OR batch_row.graph_hash <> task_binding_row.graph_hash
       OR batch_row.snapshot_matter_version <> input_expected_version
       OR batch_row.input_hash <> task_binding_row.task_input_hash
       OR batch_row.skill_id <> 'case_ledger_extraction'
       OR batch_row.skill_version <> '1.0.0'
       OR batch_row.tool_id <> 'extract_case_ledger'
       OR batch_row.tool_version <> '1.0.0'
       OR pg_catalog.jsonb_array_length(batch_row.input_refs) <>
            task_binding_row.required_source_count
       OR (SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
                pg_catalog.string_agg(ref.value, E'\n' ORDER BY ref.value),
                'UTF8'
            ), 'sha256'), 'hex')
             FROM pg_catalog.jsonb_array_elements_text(
                 batch_row.input_refs
             ) ref(value)
          ) <> task_binding_row.required_source_set_hash
       OR NOT public.case_agent_ledger_extraction_run_staging_complete(
            batch_row.run_id, input_firm_id, input_matter_id
       ) THEN
        RAISE EXCEPTION
            're-extraction batch is not the exact bound verified and fully staged task';
    END IF;

    UPDATE public.matters matter
       SET version = matter.version + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = input_firm_id
       AND matter.version = input_expected_version
     RETURNING matter.version INTO next_version;
    IF next_version IS NULL THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction matter version is stale',
            ERRCODE = 'P4091';
    END IF;

    binding_id := public.gen_random_uuid();
    binding_hash := pg_catalog.encode(public.digest(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema_version', 'case-ledger-exception-reextraction-binding-v1',
            'followup_id', input_followup_id,
            'origin_exception_decision_id',
                followup_row.origin_exception_decision_id,
            'task_binding_id', task_binding_row.task_binding_id,
            'reextraction_batch_id', input_reextraction_batch_id,
            'reextraction_run_id', batch_row.run_id,
            'verification_receipt_id', batch_row.verification_receipt_id
        )::text, 'UTF8'), 'sha256'
    ), 'hex');
    INSERT INTO public.case_agent_ledger_exception_reextraction_bindings (
        reextraction_binding_id, followup_id,
        task_binding_id, origin_exception_decision_id, reextraction_batch_id,
        reextraction_run_id, verification_receipt_id, firm_id, matter_id,
        bound_by, binding_hash
    ) VALUES (
        binding_id, input_followup_id,
        task_binding_row.task_binding_id,
        followup_row.origin_exception_decision_id,
        input_reextraction_batch_id, batch_row.run_id,
        batch_row.verification_receipt_id, input_firm_id, input_matter_id,
        input_worker_id, binding_hash
    );

    new_event_id := public.gen_random_uuid();
    audit_id := public.gen_random_uuid();
    new_event_hash := pg_catalog.encode(public.digest(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema_version', 'case-ledger-exception-followup-event-v1',
            'followup_id', input_followup_id,
            'event_sequence', followup_row.head_sequence + 1,
            'event_type', 'REEXTRACTION_VERIFIED_AND_STAGED',
            'state_after', 'SATISFIED',
            'reextraction_binding_id', binding_id,
            'request_hash', input_request_hash
        )::text, 'UTF8'), 'sha256'
    ), 'hex');
    INSERT INTO public.audit_events (
        event_id, firm_id, matter_id, actor_id, event_type,
        input_version, output_version, request_id, payload
    ) VALUES (
        audit_id, input_firm_id, input_matter_id, input_worker_id,
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_VERIFIED_AND_STAGED',
        input_expected_version, next_version, public.gen_random_uuid(),
        pg_catalog.jsonb_build_object(
            'followup_id', input_followup_id,
            'followup_event_id', new_event_id,
            'origin_exception_decision_id',
                followup_row.origin_exception_decision_id,
            'origin_run_id', followup_row.origin_run_id,
            'origin_extraction_batch_id',
                followup_row.origin_extraction_batch_id,
            'reextraction_task_binding_id',
                task_binding_row.task_binding_id,
            'reextraction_binding_id', binding_id,
            'reextraction_batch_id', input_reextraction_batch_id,
            'verification_receipt_id', batch_row.verification_receipt_id,
            'formal_ledger_write', false,
            'legal_conclusion', false
        )
    );
    INSERT INTO public.case_agent_ledger_exception_followup_events (
        followup_event_id, followup_id, event_sequence, firm_id, matter_id,
        subject_hash, event_type, state_after, actor_id,
        expected_matter_version, reextraction_binding_id, idempotency_key,
        request_hash, event_hash, audit_event_id
    ) VALUES (
        new_event_id, input_followup_id, followup_row.head_sequence + 1,
        input_firm_id, input_matter_id, followup_row.subject_hash,
        'REEXTRACTION_VERIFIED_AND_STAGED', 'SATISFIED', input_worker_id,
        input_expected_version, binding_id, input_idempotency_key,
        input_request_hash, new_event_hash, audit_id
    );
    UPDATE public.case_agent_ledger_exception_followup_heads head
       SET current_state = 'SATISFIED', head_event_id = new_event_id,
           head_sequence = head.head_sequence + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE head.followup_id = input_followup_id
       AND head.firm_id = input_firm_id
       AND head.matter_id = input_matter_id
       AND head.current_state = 'ACTIVE'
       AND head.head_sequence = followup_row.head_sequence;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction follow-up changed before commit',
            ERRCODE = 'P4092';
    END IF;
    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        input_firm_id, input_matter_id, next_version,
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_VERIFIED_AND_STAGED',
        pg_catalog.jsonb_build_object(
            'audit_event_id', audit_id,
            'object_id', input_followup_id,
            'followup_event_id', new_event_id
        )
    );
    response_json := pg_catalog.jsonb_build_object(
        'command_name',
            'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_FOLLOWUP',
        'idempotency_key', input_idempotency_key,
        'matter_id', input_matter_id::text,
        'matter_version', next_version,
        'audit_event_id', audit_id::text,
        'object_type', 'CASE_LEDGER_EXCEPTION_FOLLOWUP',
        'object_id', input_followup_id::text
    );
    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        input_firm_id, input_matter_id, input_worker_id,
        'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_FOLLOWUP',
        input_idempotency_key, input_request_hash, response_json
    );
    RETURN response_json;
END;
$$;

CREATE FUNCTION public.case_agent_ledger_exception_reextraction_set_request_hash(
    input_matter_id uuid,
    input_expected_version integer,
    input_run_id uuid,
    input_graph_id uuid
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
        'case-ledger-exception-reextraction-set-v1' || E'\n' ||
        input_matter_id::text || E'\n' ||
        input_expected_version::text || E'\n' ||
        input_run_id::text || E'\n' ||
        input_graph_id::text,
        'UTF8'
    ), 'sha256'), 'hex')::char(64)
$$;

CREATE FUNCTION public.satisfy_case_agent_ledger_reextraction_set_from_worker(
    input_worker_id uuid,
    input_firm_id uuid,
    input_matter_id uuid,
    input_run_id uuid,
    input_graph_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    worker_is_dedicated boolean;
    current_matter_version integer;
    prior_row record;
    control_row record;
    run_row record;
    active_followup_count integer;
    verified_followup_count integer;
    resolution_bindings jsonb;
    anchor_followup_id uuid;
    anchor_reextraction_batch_id uuid;
    anchor_event_id uuid;
    audit_id uuid;
    next_version integer;
    item jsonb;
    event_id uuid;
    fulfillment_id uuid;
    fulfillment_hash char(64);
    event_hash char(64);
    response_json jsonb;
BEGIN
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$'
       OR input_request_hash IS DISTINCT FROM
            public.case_agent_ledger_exception_reextraction_set_request_hash(
                input_matter_id, input_expected_version,
                input_run_id, input_graph_id
            ) THEN
        RAISE EXCEPTION 'ledger re-extraction set input is invalid';
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', input_firm_id::text, true);

    SELECT matter.version
      INTO current_matter_version
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = input_firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger re-extraction set matter is missing';
    END IF;
    PERFORM 1
      FROM public.users worker
     WHERE worker.user_id = input_worker_id
       AND worker.firm_id = input_firm_id
       AND worker.status = 'ACTIVE'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger re-extraction set worker is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.user_id = input_worker_id
       AND role_binding.firm_id = input_firm_id
       AND role_binding.matter_id = input_matter_id
       AND role_binding.revoked_at IS NULL
     FOR SHARE;
    SELECT pg_catalog.count(*) = 1
           AND pg_catalog.bool_and(role_binding.role = 'SYSTEM_WORKER')
      INTO worker_is_dedicated
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.user_id = input_worker_id
       AND role_binding.firm_id = input_firm_id
       AND role_binding.matter_id = input_matter_id
       AND role_binding.revoked_at IS NULL;
    IF worker_is_dedicated IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'ledger re-extraction set requires the dedicated SYSTEM_WORKER';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        input_worker_id::text || '|' || input_matter_id::text ||
        '|SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET|' ||
        input_idempotency_key,
        0
    ));

    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = input_firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = input_worker_id
       AND idempotency.command_name =
            'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET'
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                input_idempotency_key
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                (input_expected_version + 1)::text
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET'
           OR prior_row.response_json->>'object_id' IS DISTINCT FROM
                input_graph_id::text
           OR NOT EXISTS (
                SELECT 1
                  FROM public.audit_events audit
                 WHERE audit.event_id =
                        (prior_row.response_json->>'audit_event_id')::uuid
                   AND audit.firm_id = input_firm_id
                   AND audit.matter_id = input_matter_id
                   AND audit.actor_id = input_worker_id
                   AND audit.event_type =
                        'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED'
                   AND audit.input_version = input_expected_version
                   AND audit.output_version = input_expected_version + 1
                   AND audit.payload->>'run_id' = input_run_id::text
                   AND audit.payload->>'graph_id' = input_graph_id::text
                   AND audit.payload->>'request_hash' = input_request_hash
           ) THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger re-extraction set idempotency replay differs',
                ERRCODE = 'P4092';
        END IF;
        RETURN prior_row.response_json;
    END IF;
    IF current_matter_version <> input_expected_version THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction set matter version is stale',
            ERRCODE = 'P4091';
    END IF;

    SELECT head.current_control_assignment_id,
           head.current_state, head.head_sequence,
           assignment.control_run_id
      INTO control_row
      FROM public.case_agent_ledger_exception_control_heads head
      JOIN public.case_agent_ledger_exception_control_assignments assignment
        ON assignment.control_assignment_id =
                head.current_control_assignment_id
       AND assignment.firm_id = head.firm_id
       AND assignment.matter_id = head.matter_id
       AND assignment.state_after = head.current_state
       AND assignment.assignment_sequence = head.head_sequence
     WHERE head.firm_id = input_firm_id
       AND head.matter_id = input_matter_id
     FOR UPDATE OF head;
    IF NOT FOUND OR control_row.current_state <> 'HEALTHY'
       OR control_row.control_run_id <> input_run_id THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction set control requires recovery or differs',
            ERRCODE = 'P4092';
    END IF;

    SELECT run.current_graph_id, run.current_graph_version,
           run.current_graph_hash, run.snapshot_matter_version,
           run.snapshot_hash, run.verification_hash,
           run.status, run.is_stale, run.is_cancelled,
           graph.graph_version, graph.graph_hash,
           graph.snapshot_matter_version AS graph_snapshot_version,
           receipt.verification_receipt_id
      INTO run_row
      FROM public.case_agent_runs run
      JOIN public.case_agent_task_graphs graph
        ON graph.graph_id = run.current_graph_id
       AND graph.run_id = run.run_id
       AND graph.firm_id = run.firm_id
       AND graph.matter_id = run.matter_id
      JOIN public.case_agent_verification_receipts receipt
        ON receipt.run_id = run.run_id
       AND receipt.firm_id = run.firm_id
       AND receipt.matter_id = run.matter_id
       AND receipt.outcome = 'PASSED'
       AND receipt.verification_hash = run.verification_hash
       AND receipt.snapshot_hash = run.snapshot_hash
       AND receipt.graph_hash = graph.graph_hash
     WHERE run.run_id = input_run_id
       AND run.firm_id = input_firm_id
       AND run.matter_id = input_matter_id
       AND run.current_graph_id = input_graph_id
     FOR SHARE OF run;
    IF NOT FOUND
       OR run_row.current_graph_version <> run_row.graph_version
       OR run_row.current_graph_hash <> run_row.graph_hash
       OR run_row.snapshot_matter_version <> input_expected_version
       OR run_row.graph_snapshot_version <> input_expected_version
       OR run_row.status NOT IN ('READY_FOR_REVIEW', 'COMPLETED')
       OR run_row.is_stale OR run_row.is_cancelled
       OR NOT public.case_agent_ledger_extraction_run_staging_complete(
            input_run_id, input_firm_id, input_matter_id
       ) THEN
        RAISE EXCEPTION
            'ledger re-extraction set is not the current PASSED fully staged graph';
    END IF;

    -- Lock the complete server-discovered set.  The Worker supplies no
    -- follow-up or batch subset, so one missing binding/batch aborts all.
    PERFORM 1
      FROM public.case_agent_ledger_exception_followups followup
      JOIN public.case_agent_ledger_exception_followup_heads head
        ON head.followup_id = followup.followup_id
       AND head.firm_id = followup.firm_id
       AND head.matter_id = followup.matter_id
     WHERE followup.firm_id = input_firm_id
       AND followup.matter_id = input_matter_id
       AND followup.followup_kind = 'REEXTRACTION'
       AND head.current_state = 'ACTIVE'
     ORDER BY followup.followup_id
     FOR UPDATE OF head;
    GET DIAGNOSTICS active_followup_count = ROW_COUNT;
    IF active_followup_count < 1 THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction set has no active follow-ups',
            ERRCODE = 'P4092';
    END IF;

    WITH active AS (
        SELECT followup.followup_id,
               followup.origin_exception_decision_id,
               followup.origin_exception_group_id,
               followup.origin_extraction_batch_id,
               followup.origin_run_id,
               followup.subject_hash,
               head.head_sequence,
               binding.task_binding_id,
               binding.task_id,
               binding.task_input_hash,
               binding.required_source_set_hash,
               binding.required_source_count,
               binding.bound_at
          FROM public.case_agent_ledger_exception_followups followup
          JOIN public.case_agent_ledger_exception_followup_heads head
            ON head.followup_id = followup.followup_id
           AND head.firm_id = followup.firm_id
           AND head.matter_id = followup.matter_id
           AND head.current_state = 'ACTIVE'
          JOIN public.case_agent_ledger_exception_reextraction_task_binding_heads
                binding_head
            ON binding_head.followup_id = followup.followup_id
           AND binding_head.firm_id = followup.firm_id
           AND binding_head.matter_id = followup.matter_id
          JOIN public.case_agent_ledger_exception_reextraction_task_bindings binding
            ON binding.task_binding_id = binding_head.current_task_binding_id
           AND binding.followup_id = binding_head.followup_id
           AND binding.firm_id = binding_head.firm_id
           AND binding.matter_id = binding_head.matter_id
           AND binding.run_id = input_run_id
           AND binding.graph_id = input_graph_id
           AND binding.graph_version = run_row.graph_version
           AND binding.graph_hash = run_row.graph_hash
         WHERE followup.firm_id = input_firm_id
           AND followup.matter_id = input_matter_id
           AND followup.followup_kind = 'REEXTRACTION'
    ), verified AS (
        SELECT active.*,
               batch.extraction_batch_id,
               batch.verification_receipt_id
          FROM active
          JOIN public.case_agent_tasks task
            ON task.graph_id = input_graph_id
           AND task.task_id = active.task_id
           AND task.run_id = input_run_id
           AND task.firm_id = input_firm_id
           AND task.matter_id = input_matter_id
           AND task.input_hash = active.task_input_hash
           AND task.skill_id = 'case_ledger_extraction'
           AND task.skill_version = '1.0.0'
           AND task.tool_id = 'extract_case_ledger'
           AND task.tool_version = '1.0.0'
           AND pg_catalog.jsonb_array_length(task.input_refs) =
                active.required_source_count
           AND (SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
                       pg_catalog.string_agg(ref.value, E'\n' ORDER BY ref.value),
                       'UTF8'
                   ), 'sha256'), 'hex')
                  FROM pg_catalog.jsonb_array_elements_text(task.input_refs) ref(value)
               ) = active.required_source_set_hash
          JOIN LATERAL (
              SELECT candidate_batch.extraction_batch_id,
                     candidate_batch.verification_receipt_id
                FROM public.case_agent_ledger_extraction_batches candidate_batch
               WHERE candidate_batch.run_id = input_run_id
                 AND candidate_batch.graph_id = input_graph_id
                 AND candidate_batch.task_id = active.task_id
                 AND candidate_batch.firm_id = input_firm_id
                 AND candidate_batch.matter_id = input_matter_id
                 AND candidate_batch.task_input_hash = active.task_input_hash
                 AND candidate_batch.verification_receipt_id =
                        run_row.verification_receipt_id
                 AND candidate_batch.created_at >= active.bound_at
          ) batch ON true
          JOIN LATERAL (
              SELECT pg_catalog.count(DISTINCT page.evidence_page_id)::integer
                        AS source_count,
                     pg_catalog.encode(public.digest(pg_catalog.convert_to(
                         pg_catalog.string_agg(
                             DISTINCT 'evidence-page:' ||
                                page.evidence_page_id::text,
                             E'\n' ORDER BY 'evidence-page:' ||
                                page.evidence_page_id::text
                         ), 'UTF8'
                     ), 'sha256'), 'hex') AS source_set_hash
                FROM public.case_agent_ledger_exception_group_members member
                JOIN public.case_agent_ledger_extraction_candidate_pages page
                  ON page.extraction_candidate_id =
                        member.extraction_candidate_id
                 AND page.firm_id = member.firm_id
                 AND page.matter_id = member.matter_id
               WHERE member.exception_group_id =
                        active.origin_exception_group_id
                 AND member.extraction_batch_id =
                        active.origin_extraction_batch_id
                 AND member.firm_id = input_firm_id
                 AND member.matter_id = input_matter_id
          ) source
            ON source.source_count = active.required_source_count
           AND source.source_set_hash = active.required_source_set_hash
    )
    SELECT pg_catalog.count(*)::integer,
           pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'followup_id', verified.followup_id,
               'origin_exception_decision_id',
                    verified.origin_exception_decision_id,
               'origin_extraction_batch_id',
                    verified.origin_extraction_batch_id,
               'subject_hash', verified.subject_hash,
               'head_sequence', verified.head_sequence,
               'task_binding_id', verified.task_binding_id,
               'task_id', verified.task_id,
               'extraction_batch_id', verified.extraction_batch_id,
               'verification_receipt_id', verified.verification_receipt_id
           ) ORDER BY verified.followup_id)
      INTO verified_followup_count, resolution_bindings
      FROM verified;
    IF verified_followup_count <> active_followup_count
       OR resolution_bindings IS NULL
       OR pg_catalog.jsonb_array_length(resolution_bindings) <>
            active_followup_count THEN
        RAISE EXCEPTION
            'ledger re-extraction set is incomplete or contains a duplicate task batch';
    END IF;

    anchor_followup_id :=
        (resolution_bindings->0->>'followup_id')::uuid;
    anchor_reextraction_batch_id :=
        (resolution_bindings->0->>'extraction_batch_id')::uuid;
    anchor_event_id := public.gen_random_uuid();
    audit_id := public.gen_random_uuid();

    UPDATE public.matters matter
       SET version = matter.version + 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = input_firm_id
       AND matter.version = input_expected_version
     RETURNING matter.version INTO next_version;
    IF next_version IS NULL THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger re-extraction set matter version changed before commit',
            ERRCODE = 'P4091';
    END IF;

    INSERT INTO public.audit_events (
        event_id, firm_id, matter_id, actor_id, event_type,
        input_version, output_version, request_id, payload
    ) VALUES (
        audit_id, input_firm_id, input_matter_id, input_worker_id,
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED',
        input_expected_version, next_version, public.gen_random_uuid(),
        pg_catalog.jsonb_build_object(
            'run_id', input_run_id,
            'graph_id', input_graph_id,
            'followup_count', active_followup_count,
            'bindings', resolution_bindings,
            'followup_id', anchor_followup_id,
            'followup_event_id', anchor_event_id,
            'request_hash', input_request_hash,
            'formal_ledger_write', false,
            'legal_conclusion', false
        )
    );

    FOR item IN
        SELECT value
          FROM pg_catalog.jsonb_array_elements(resolution_bindings)
    LOOP
        fulfillment_id := public.gen_random_uuid();
        event_id := CASE
            WHEN (item->>'followup_id')::uuid = anchor_followup_id
            THEN anchor_event_id
            ELSE public.gen_random_uuid()
        END;
        fulfillment_hash := pg_catalog.encode(public.digest(
            pg_catalog.convert_to(pg_catalog.jsonb_build_object(
                'schema_version',
                    'case-ledger-exception-reextraction-binding-v2',
                'followup_id', item->>'followup_id',
                'origin_exception_decision_id',
                    item->>'origin_exception_decision_id',
                'task_binding_id', item->>'task_binding_id',
                'reextraction_batch_id', item->>'extraction_batch_id',
                'reextraction_run_id', input_run_id,
                'verification_receipt_id',
                    item->>'verification_receipt_id',
                'set_request_hash', input_request_hash
            )::text, 'UTF8'), 'sha256'
        ), 'hex');
        INSERT INTO public.case_agent_ledger_exception_reextraction_bindings (
            reextraction_binding_id, followup_id, task_binding_id,
            origin_exception_decision_id, reextraction_batch_id,
            reextraction_run_id, verification_receipt_id,
            firm_id, matter_id, bound_by, binding_hash
        ) VALUES (
            fulfillment_id, (item->>'followup_id')::uuid,
            (item->>'task_binding_id')::uuid,
            (item->>'origin_exception_decision_id')::uuid,
            (item->>'extraction_batch_id')::uuid, input_run_id,
            (item->>'verification_receipt_id')::uuid,
            input_firm_id, input_matter_id, input_worker_id,
            fulfillment_hash
        );
        event_hash := pg_catalog.encode(public.digest(pg_catalog.convert_to(
            pg_catalog.jsonb_build_object(
                'schema_version', 'case-ledger-exception-followup-event-v2',
                'followup_id', item->>'followup_id',
                'event_sequence', (item->>'head_sequence')::integer + 1,
                'event_type', 'REEXTRACTION_VERIFIED_AND_STAGED',
                'state_after', 'SATISFIED',
                'reextraction_binding_id', fulfillment_id,
                'set_request_hash', input_request_hash
            )::text, 'UTF8'), 'sha256'
        ), 'hex');
        INSERT INTO public.case_agent_ledger_exception_followup_events (
            followup_event_id, followup_id, event_sequence,
            firm_id, matter_id, subject_hash, event_type, state_after,
            actor_id, expected_matter_version, reextraction_binding_id,
            idempotency_key, request_hash, event_hash, audit_event_id
        ) VALUES (
            event_id, (item->>'followup_id')::uuid,
            (item->>'head_sequence')::integer + 1,
            input_firm_id, input_matter_id, (item->>'subject_hash')::char(64),
            'REEXTRACTION_VERIFIED_AND_STAGED', 'SATISFIED',
            input_worker_id, input_expected_version, fulfillment_id,
            input_idempotency_key, input_request_hash, event_hash, audit_id
        );
        UPDATE public.case_agent_ledger_exception_followup_heads head
           SET current_state = 'SATISFIED',
               head_event_id = event_id,
               head_sequence = head.head_sequence + 1,
               updated_at = pg_catalog.clock_timestamp()
         WHERE head.followup_id = (item->>'followup_id')::uuid
           AND head.firm_id = input_firm_id
           AND head.matter_id = input_matter_id
           AND head.current_state = 'ACTIVE'
           AND head.head_sequence = (item->>'head_sequence')::integer;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger re-extraction set head changed before commit',
                ERRCODE = 'P4092';
        END IF;
    END LOOP;

    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        input_firm_id, input_matter_id, next_version,
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED',
        pg_catalog.jsonb_build_object(
            'audit_event_id', audit_id,
            'object_id', anchor_followup_id,
            'followup_event_id', anchor_event_id,
            'run_id', input_run_id,
            'graph_id', input_graph_id,
            'followup_count', active_followup_count,
            'extraction_batch_id', anchor_reextraction_batch_id
        )
    );
    response_json := pg_catalog.jsonb_build_object(
        'command_name', 'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET',
        'idempotency_key', input_idempotency_key,
        'matter_id', input_matter_id::text,
        'matter_version', next_version,
        'audit_event_id', audit_id::text,
        'object_type', 'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET',
        'object_id', input_graph_id::text,
        'followup_count', active_followup_count
    );
    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        input_firm_id, input_matter_id, input_worker_id,
        'SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET',
        input_idempotency_key, input_request_hash, response_json
    );
    RETURN response_json;
END;
$$;

CREATE FUNCTION public.enqueue_case_agent_snapshot_refresh_from_exception_followup()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    lifecycle_row record;
    new_refresh_request_id uuid;
BEGIN
    IF NEW.event_type NOT IN (
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_VERIFIED_AND_STAGED',
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED',
        'CASE_LEDGER_EXCEPTION_MORE_EVIDENCE_CONFIRMED',
        'CASE_LEDGER_EXCEPTION_DEFER_RESUMED',
        'CASE_LEDGER_EXCEPTION_FOLLOWUP_WITHDRAWN',
        'CASE_LEDGER_EXCEPTION_FOLLOWUP_SUPERSEDED'
    ) THEN
        RETURN NEW;
    END IF;
    IF NEW.matter_id IS NULL
       OR NEW.aggregate_version IS NULL
       OR pg_catalog.jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
       OR NEW.payload->>'audit_event_id' IS NULL
       OR NEW.payload->>'object_id' IS NULL
       OR NEW.payload->>'followup_event_id' IS NULL THEN
        RAISE EXCEPTION 'ledger exception follow-up outbox is incomplete';
    END IF;
    SELECT followup.followup_id,
           CASE
               WHEN NEW.event_type =
                    'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED'
               THEN reextraction.reextraction_run_id
               ELSE control_assignment.control_run_id
           END AS refresh_run_id,
           CASE
               WHEN NEW.event_type =
                    'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED'
               THEN reextraction.reextraction_batch_id
               ELSE NULL
           END AS refresh_extraction_batch_id,
           CASE
               WHEN NEW.event_type =
                    'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED'
               THEN NULL
               ELSE control_head.current_control_assignment_id
           END AS refresh_control_assignment_id,
           control_head.current_state AS control_state,
           audit.input_version, audit.output_version,
           event.followup_event_id
      INTO lifecycle_row
      FROM public.case_agent_ledger_exception_followups followup
      JOIN public.case_agent_ledger_exception_followup_events event
        ON event.followup_id = followup.followup_id
       AND event.firm_id = followup.firm_id
       AND event.matter_id = followup.matter_id
      JOIN public.case_agent_ledger_exception_followup_heads head
        ON head.followup_id = followup.followup_id
       AND head.firm_id = followup.firm_id
       AND head.matter_id = followup.matter_id
       AND head.head_event_id = event.followup_event_id
       AND head.current_state <> 'ACTIVE'
      JOIN public.audit_events audit
        ON audit.event_id = event.audit_event_id
       AND audit.firm_id = event.firm_id
       AND audit.matter_id = event.matter_id
       AND audit.event_type = NEW.event_type
      JOIN public.case_agent_ledger_exception_control_heads control_head
        ON control_head.firm_id = followup.firm_id
       AND control_head.matter_id = followup.matter_id
      JOIN public.case_agent_ledger_exception_control_assignments
            control_assignment
        ON control_assignment.control_assignment_id =
                control_head.current_control_assignment_id
       AND control_assignment.firm_id = control_head.firm_id
       AND control_assignment.matter_id = control_head.matter_id
       AND control_assignment.state_after = control_head.current_state
       AND control_assignment.assignment_sequence =
                control_head.head_sequence
      LEFT JOIN public.case_agent_ledger_exception_reextraction_bindings
            reextraction
        ON reextraction.reextraction_binding_id = event.reextraction_binding_id
       AND reextraction.followup_id = event.followup_id
       AND reextraction.firm_id = event.firm_id
       AND reextraction.matter_id = event.matter_id
     WHERE followup.followup_id = (NEW.payload->>'object_id')::uuid
       AND followup.firm_id = NEW.firm_id
       AND followup.matter_id = NEW.matter_id
       AND event.followup_event_id =
            (NEW.payload->>'followup_event_id')::uuid
       AND event.audit_event_id = (NEW.payload->>'audit_event_id')::uuid
       AND audit.output_version = NEW.aggregate_version
       AND audit.output_version = audit.input_version + 1
       AND audit.payload->>'followup_id' = followup.followup_id::text
       AND audit.payload->>'followup_event_id' =
            event.followup_event_id::text
       AND (
            NEW.event_type <>
                'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED'
            OR (
                reextraction.reextraction_run_id =
                    (NEW.payload->>'run_id')::uuid
                AND reextraction.reextraction_batch_id =
                    (NEW.payload->>'extraction_batch_id')::uuid
                AND audit.payload->>'run_id' = NEW.payload->>'run_id'
                AND audit.payload->>'graph_id' = NEW.payload->>'graph_id'
            )
       );
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception follow-up outbox differs from its current event';
    END IF;

    -- A failed control run cannot consume a refresh.  Explicit lawyer
    -- WITHDRAW/SUPERSEDE remains allowed so counsel can close the blocker;
    -- if any ACTIVE work remains, the visible recovery action must transfer
    -- control before automated work resumes.
    IF lifecycle_row.control_state = 'RECOVERY_REQUIRED'
       AND NEW.event_type <>
            'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED' THEN
        RETURN NEW;
    END IF;

    INSERT INTO public.case_agent_snapshot_refresh_requests (
        source_outbox_id, source_audit_event_id, extraction_batch_id,
        control_assignment_id, run_id,
        firm_id, matter_id, source_matter_version, target_matter_version,
        request_status
    ) VALUES (
        NEW.outbox_id, (NEW.payload->>'audit_event_id')::uuid,
        lifecycle_row.refresh_extraction_batch_id,
        lifecycle_row.refresh_control_assignment_id,
        lifecycle_row.refresh_run_id, NEW.firm_id, NEW.matter_id,
        lifecycle_row.input_version, NEW.aggregate_version, 'PENDING'
    ) RETURNING refresh_request_id INTO new_refresh_request_id;

    -- Retain every immutable source receipt while exposing only the newest
    -- cursor for this run to the 0046 worker.
    UPDATE public.case_agent_snapshot_refresh_requests prior
       SET request_status = 'SUPERSEDED',
           updated_at = pg_catalog.clock_timestamp()
     WHERE prior.run_id = lifecycle_row.refresh_run_id
       AND prior.firm_id = NEW.firm_id
       AND prior.matter_id = NEW.matter_id
       AND prior.refresh_request_id <> new_refresh_request_id
       AND prior.target_matter_version < NEW.aggregate_version
       AND prior.request_status IN (
            'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
            'BLOCKED_BY_OPEN_EXCEPTIONS'
       );
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_exception_followup_enqueues_snapshot_refresh
    AFTER INSERT
    ON public.outbox_events
    FOR EACH ROW
    WHEN (NEW.event_type IN (
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_VERIFIED_AND_STAGED',
        'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED',
        'CASE_LEDGER_EXCEPTION_MORE_EVIDENCE_CONFIRMED',
        'CASE_LEDGER_EXCEPTION_DEFER_RESUMED',
        'CASE_LEDGER_EXCEPTION_FOLLOWUP_WITHDRAWN',
        'CASE_LEDGER_EXCEPTION_FOLLOWUP_SUPERSEDED'
    ))
    EXECUTE FUNCTION
        public.enqueue_case_agent_snapshot_refresh_from_exception_followup();

-- A matter can receive an authoritative write from a historical run that is
-- not the ACTIVE follow-up control run.  Existing 0046/0047 bridges refresh
-- only the source run.  After those alphabetically earlier outbox triggers
-- run, create one server-owned control-run refresh outbox only when the exact
-- target version is still absent.  This prevents a manual follow-up or a
-- later run from stranding current task bindings on an older matter snapshot.
CREATE FUNCTION public.enqueue_case_agent_exception_control_run_refresh_outbox()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    control_row record;
    source_audit_id uuid;
BEGIN
    IF NEW.event_type = 'CASE_LEDGER_EXCEPTION_CONTROL_RUN_REFRESH_REQUESTED'
       OR NEW.matter_id IS NULL
       OR NEW.aggregate_version IS NULL
       OR pg_catalog.jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
       OR NEW.payload->>'audit_event_id' IS NULL
       OR NEW.payload->>'audit_event_id' !~
            '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$' THEN
        RETURN NEW;
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', NEW.firm_id::text, true);
    source_audit_id := (NEW.payload->>'audit_event_id')::uuid;
    PERFORM 1
      FROM public.audit_events audit
     WHERE audit.event_id = source_audit_id
       AND audit.firm_id = NEW.firm_id
       AND audit.matter_id = NEW.matter_id
       AND audit.output_version = NEW.aggregate_version
       AND audit.output_version = audit.input_version + 1;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_followup_heads followup_head
         WHERE followup_head.firm_id = NEW.firm_id
           AND followup_head.matter_id = NEW.matter_id
           AND followup_head.current_state = 'ACTIVE'
    ) THEN
        RETURN NEW;
    END IF;
    SELECT assignment.control_run_id AS run_id,
           head.current_control_assignment_id AS control_assignment_id,
           head.current_state
      INTO control_row
      FROM public.case_agent_ledger_exception_control_heads head
      JOIN public.case_agent_ledger_exception_control_assignments assignment
        ON assignment.control_assignment_id =
                head.current_control_assignment_id
       AND assignment.firm_id = head.firm_id
       AND assignment.matter_id = head.matter_id
       AND assignment.state_after = head.current_state
       AND assignment.assignment_sequence = head.head_sequence
     WHERE head.firm_id = NEW.firm_id
       AND head.matter_id = NEW.matter_id
     FOR SHARE OF head;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'active ledger exception follow-ups lack a control head';
    ELSIF control_row.current_state = 'RECOVERY_REQUIRED' THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_snapshot_refresh_requests request
         WHERE request.run_id = control_row.run_id
           AND request.firm_id = NEW.firm_id
           AND request.matter_id = NEW.matter_id
           AND request.target_matter_version = NEW.aggregate_version
    ) THEN
        RETURN NEW;
    END IF;
    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        NEW.firm_id, NEW.matter_id, NEW.aggregate_version,
        'CASE_LEDGER_EXCEPTION_CONTROL_RUN_REFRESH_REQUESTED',
        pg_catalog.jsonb_build_object(
            'audit_event_id', source_audit_id,
            'source_outbox_id', NEW.outbox_id,
            'run_id', control_row.run_id,
            'control_assignment_id', control_row.control_assignment_id
        )
    );
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.enqueue_case_agent_exception_control_run_refresh()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    source_row record;
    audit_row record;
    new_refresh_request_id uuid;
BEGIN
    IF NEW.matter_id IS NULL
       OR NEW.aggregate_version IS NULL
       OR pg_catalog.jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
       OR NEW.payload->>'audit_event_id' IS NULL
       OR NEW.payload->>'source_outbox_id' IS NULL
       OR NEW.payload->>'run_id' IS NULL
       OR NEW.payload->>'control_assignment_id' IS NULL THEN
        RAISE EXCEPTION 'control-run refresh outbox is incomplete';
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', NEW.firm_id::text, true);
    SELECT source.outbox_id
      INTO source_row
      FROM public.outbox_events source
     WHERE source.outbox_id = (NEW.payload->>'source_outbox_id')::uuid
       AND source.firm_id = NEW.firm_id
       AND source.matter_id = NEW.matter_id
       AND source.aggregate_version = NEW.aggregate_version
       AND source.event_type <>
            'CASE_LEDGER_EXCEPTION_CONTROL_RUN_REFRESH_REQUESTED'
       AND source.payload->>'audit_event_id' =
            NEW.payload->>'audit_event_id';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'control-run refresh source outbox differs';
    END IF;
    SELECT audit.event_id, audit.input_version, audit.output_version
      INTO audit_row
      FROM public.audit_events audit
     WHERE audit.event_id = (NEW.payload->>'audit_event_id')::uuid
       AND audit.firm_id = NEW.firm_id
       AND audit.matter_id = NEW.matter_id
       AND audit.output_version = NEW.aggregate_version
       AND audit.output_version = audit.input_version + 1;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_control_heads head
          JOIN public.case_agent_ledger_exception_control_assignments assignment
            ON assignment.control_assignment_id =
                    head.current_control_assignment_id
           AND assignment.firm_id = head.firm_id
           AND assignment.matter_id = head.matter_id
           AND assignment.state_after = head.current_state
           AND assignment.assignment_sequence = head.head_sequence
         WHERE head.firm_id = NEW.firm_id
           AND head.matter_id = NEW.matter_id
           AND head.current_state = 'HEALTHY'
           AND head.current_control_assignment_id =
                (NEW.payload->>'control_assignment_id')::uuid
           AND assignment.control_run_id =
                (NEW.payload->>'run_id')::uuid
           AND EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_followup_heads
                        followup_head
                 WHERE followup_head.firm_id = head.firm_id
                   AND followup_head.matter_id = head.matter_id
                   AND followup_head.current_state = 'ACTIVE'
           )
    ) THEN
        RAISE EXCEPTION 'control-run refresh no longer has an active authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_snapshot_refresh_requests request
         WHERE request.run_id = (NEW.payload->>'run_id')::uuid
           AND request.firm_id = NEW.firm_id
           AND request.matter_id = NEW.matter_id
           AND request.target_matter_version = NEW.aggregate_version
    ) THEN
        RETURN NEW;
    END IF;
    INSERT INTO public.case_agent_snapshot_refresh_requests (
        source_outbox_id, source_audit_event_id, extraction_batch_id,
        control_assignment_id, run_id,
        firm_id, matter_id, source_matter_version, target_matter_version,
        request_status
    ) VALUES (
        NEW.outbox_id, audit_row.event_id,
        NULL,
        (NEW.payload->>'control_assignment_id')::uuid,
        (NEW.payload->>'run_id')::uuid,
        NEW.firm_id, NEW.matter_id, audit_row.input_version,
        NEW.aggregate_version, 'PENDING'
    ) RETURNING refresh_request_id INTO new_refresh_request_id;
    UPDATE public.case_agent_snapshot_refresh_requests prior
       SET request_status = 'SUPERSEDED',
           updated_at = pg_catalog.clock_timestamp()
     WHERE prior.run_id = (NEW.payload->>'run_id')::uuid
       AND prior.firm_id = NEW.firm_id
       AND prior.matter_id = NEW.matter_id
       AND prior.refresh_request_id <> new_refresh_request_id
       AND prior.target_matter_version < NEW.aggregate_version
       AND prior.request_status IN (
            'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
            'BLOCKED_BY_OPEN_EXCEPTIONS'
       );
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_exception_control_run_refresh_materializes
    AFTER INSERT ON public.outbox_events
    FOR EACH ROW
    WHEN (NEW.event_type =
        'CASE_LEDGER_EXCEPTION_CONTROL_RUN_REFRESH_REQUESTED')
    EXECUTE FUNCTION
        public.enqueue_case_agent_exception_control_run_refresh();

CREATE TRIGGER zz_case_agent_exception_control_run_refresh_fanout
    AFTER INSERT ON public.outbox_events
    FOR EACH ROW
    EXECUTE FUNCTION
        public.enqueue_case_agent_exception_control_run_refresh_outbox();

-- The current control head is the only replanning cursor for every ACTIVE
-- follow-up in its matter.  Cancellation is allowed only after a server-side
-- transfer (or after all follow-ups close), never by making the live cursor
-- terminal first.
CREATE FUNCTION public.block_active_exception_control_run_cancellation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.event_type <> 'RUN_CANCELLED' THEN
        RETURN NEW;
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', NEW.firm_id::text, true);
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_control_heads control_head
          JOIN public.case_agent_ledger_exception_control_assignments assignment
            ON assignment.control_assignment_id =
                    control_head.current_control_assignment_id
           AND assignment.firm_id = control_head.firm_id
           AND assignment.matter_id = control_head.matter_id
         WHERE assignment.control_run_id = NEW.run_id
           AND control_head.firm_id = NEW.firm_id
           AND control_head.matter_id = NEW.matter_id
           AND EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_followup_heads
                        followup_head
                 WHERE followup_head.firm_id = control_head.firm_id
                   AND followup_head.matter_id = control_head.matter_id
                   AND followup_head.current_state = 'ACTIVE'
           )
    ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'Agent run cancellation is blocked by an active exception follow-up',
            ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_active_exception_control_run_cancel_block
    BEFORE INSERT ON public.case_agent_events
    FOR EACH ROW
    WHEN (NEW.event_type = 'RUN_CANCELLED')
    EXECUTE FUNCTION
        public.block_active_exception_control_run_cancellation();

CREATE FUNCTION public.case_agent_ledger_exception_group_subject_hash(
    input_exception_group_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS char(64)
LANGUAGE sql
STABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
        'case-ledger-exception-subject-v2' || E'\n' ||
        exception_group.candidate_kind || E'\n' ||
        exception_group.group_key_hash || E'\n' ||
        exception_group.candidate_set_hash,
        'UTF8'
    ), 'sha256'), 'hex')::char(64)
      FROM public.case_agent_ledger_exception_groups exception_group
     WHERE exception_group.exception_group_id = input_exception_group_id
       AND exception_group.firm_id = input_firm_id
       AND exception_group.matter_id = input_matter_id
$$;

CREATE TABLE public.case_agent_ledger_exception_followups (
    followup_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    origin_exception_decision_id uuid NOT NULL UNIQUE,
    origin_exception_group_id uuid NOT NULL,
    origin_extraction_batch_id uuid NOT NULL,
    origin_run_id uuid NOT NULL,
    control_extraction_batch_id uuid NOT NULL,
    control_run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    followup_kind text NOT NULL CHECK (followup_kind IN (
        'REEXTRACTION', 'MORE_EVIDENCE', 'DEFERRED_REVIEW'
    )),
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    created_matter_version integer NOT NULL CHECK (created_matter_version > 0),
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (followup_id, firm_id, matter_id),
    UNIQUE (followup_id, subject_hash, firm_id, matter_id),
    FOREIGN KEY (origin_exception_decision_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_group_decisions(
            exception_decision_id, firm_id, matter_id
        ),
    FOREIGN KEY (
        origin_exception_group_id, origin_extraction_batch_id,
        firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_exception_groups(
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ),
    FOREIGN KEY (origin_run_id, firm_id, matter_id)
        REFERENCES public.case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (
        control_extraction_batch_id, control_run_id, firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_extraction_batches(
        extraction_batch_id, run_id, firm_id, matter_id
    ),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id),
    FOREIGN KEY (created_by, firm_id)
        REFERENCES public.users(user_id, firm_id)
);

-- The origin/control columns above preserve what was true when a route was
-- created.  Effective control is matter-scoped and replaceable: a verified
-- run can legitimately fail later, while the lawyer's ACTIVE follow-up must
-- remain recoverable.  Every transition is append-only and one guarded head
-- is the only authority consumed by bind/satisfy/refresh code.
CREATE TABLE public.case_agent_ledger_exception_control_assignments (
    control_assignment_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    assignment_sequence integer NOT NULL CHECK (assignment_sequence > 0),
    control_run_id uuid NOT NULL,
    state_after text NOT NULL CHECK (
        state_after IN ('HEALTHY', 'RECOVERY_REQUIRED')
    ),
    transition_type text NOT NULL CHECK (
        transition_type IN (
            'INITIALIZED', 'RECOVERY_REQUIRED', 'TRANSFERRED'
        )
    ),
    supersedes_control_assignment_id uuid,
    source_exception_decision_id uuid,
    source_agent_event_id uuid,
    actor_id uuid NOT NULL,
    web_session_id uuid,
    expected_matter_version integer NOT NULL CHECK (
        expected_matter_version > 0
    ),
    reason_code text NOT NULL CHECK (
        reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'
    ),
    idempotency_key text NOT NULL CHECK (
        pg_catalog.length(idempotency_key) BETWEEN 16 AND 200
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    audit_event_id uuid,
    assigned_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (control_assignment_id, firm_id, matter_id),
    UNIQUE (control_assignment_id, control_run_id, firm_id, matter_id),
    UNIQUE (
        control_assignment_id, firm_id, matter_id,
        state_after, assignment_sequence
    ),
    UNIQUE (firm_id, matter_id, assignment_sequence),
    UNIQUE (firm_id, matter_id, actor_id, idempotency_key),
    UNIQUE (source_agent_event_id),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id),
    FOREIGN KEY (control_run_id, firm_id, matter_id)
        REFERENCES public.case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (
        supersedes_control_assignment_id, firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_exception_control_assignments(
        control_assignment_id, firm_id, matter_id
    ),
    FOREIGN KEY (source_exception_decision_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_group_decisions(
            exception_decision_id, firm_id, matter_id
        ),
    FOREIGN KEY (source_agent_event_id, firm_id, matter_id)
        REFERENCES public.case_agent_events(event_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (web_session_id)
        REFERENCES public.web_sessions(session_id),
    FOREIGN KEY (audit_event_id)
        REFERENCES public.audit_events(event_id),
    CHECK (
        (transition_type = 'INITIALIZED'
            AND state_after = 'HEALTHY'
            AND source_exception_decision_id IS NOT NULL
            AND source_agent_event_id IS NULL
            AND web_session_id IS NULL
            AND audit_event_id IS NULL
            AND reason_code = 'FOLLOWUP_ACTIVATED')
        OR (transition_type = 'RECOVERY_REQUIRED'
            AND state_after = 'RECOVERY_REQUIRED'
            AND supersedes_control_assignment_id IS NOT NULL
            AND source_exception_decision_id IS NULL
            AND source_agent_event_id IS NOT NULL
            AND web_session_id IS NULL
            AND audit_event_id IS NULL
            AND reason_code = 'CONTROL_RUN_VERIFICATION_FAILED')
        OR (transition_type = 'TRANSFERRED'
            AND state_after = 'HEALTHY'
            AND supersedes_control_assignment_id IS NOT NULL
            AND source_exception_decision_id IS NULL
            AND source_agent_event_id IS NULL
            AND web_session_id IS NOT NULL
            AND audit_event_id IS NOT NULL
            AND reason_code IN (
                'RECOVER_FAILED_CONTROL_RUN',
                'PROACTIVE_CONTROL_TRANSFER'
            ))
    )
);

CREATE TABLE public.case_agent_ledger_exception_control_heads (
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    current_control_assignment_id uuid NOT NULL UNIQUE,
    current_state text NOT NULL CHECK (
        current_state IN ('HEALTHY', 'RECOVERY_REQUIRED')
    ),
    head_sequence integer NOT NULL CHECK (head_sequence > 0),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (firm_id, matter_id),
    FOREIGN KEY (
        current_control_assignment_id, firm_id, matter_id,
        current_state, head_sequence
    ) REFERENCES public.case_agent_ledger_exception_control_assignments(
        control_assignment_id, firm_id, matter_id,
        state_after, assignment_sequence
    ),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id)
);

CREATE INDEX case_agent_ledger_exception_control_current_run_idx
    ON public.case_agent_ledger_exception_control_assignments(
        firm_id, matter_id, control_run_id, assignment_sequence
    );

-- 0046 originally required every refresh to point at a staged extraction
-- batch.  A replacement control run can be newly created and therefore has
-- no honest batch yet.  Bind that refresh through the current assignment
-- instead; exactly one lineage edge remains mandatory.
ALTER TABLE public.case_agent_snapshot_refresh_requests
    ALTER COLUMN extraction_batch_id DROP NOT NULL;
ALTER TABLE public.case_agent_snapshot_refresh_requests
    ADD COLUMN control_assignment_id uuid;
ALTER TABLE public.case_agent_snapshot_refresh_requests
    ADD CONSTRAINT case_agent_snapshot_refresh_requests_control_assignment_fk
    FOREIGN KEY (control_assignment_id, run_id, firm_id, matter_id)
    REFERENCES public.case_agent_ledger_exception_control_assignments(
        control_assignment_id, control_run_id, firm_id, matter_id
    );
ALTER TABLE public.case_agent_snapshot_refresh_requests
    ADD CONSTRAINT case_agent_snapshot_refresh_requests_one_source CHECK (
        (extraction_batch_id IS NOT NULL)::integer +
        (control_assignment_id IS NOT NULL)::integer = 1
    );

CREATE OR REPLACE FUNCTION public.guard_case_agent_snapshot_refresh_request()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE transition_valid boolean := false;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'case Agent snapshot refresh transition is invalid';
    END IF;
    transition_valid := (
        OLD.request_status = 'PENDING'
        AND NEW.request_status = 'APPLIED'
        AND NEW.applied_event_id IS NOT NULL
        AND NEW.applied_event_sequence IS NOT NULL
        AND NEW.applied_by IS NOT NULL
        AND NEW.applied_at IS NOT NULL
    ) OR (
        OLD.request_status IN (
            'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
            'BLOCKED_BY_OPEN_EXCEPTIONS'
        )
        AND NEW.request_status = 'SUPERSEDED'
        AND EXISTS (
            SELECT 1
              FROM public.case_agent_snapshot_refresh_requests replacement
             WHERE replacement.run_id = OLD.run_id
               AND replacement.firm_id = OLD.firm_id
               AND replacement.matter_id = OLD.matter_id
               AND replacement.refresh_request_id <> OLD.refresh_request_id
               AND replacement.target_matter_version >=
                    OLD.target_matter_version
        )
    );
    IF NEW.refresh_request_id <> OLD.refresh_request_id
       OR NEW.source_outbox_id <> OLD.source_outbox_id
       OR NEW.source_audit_event_id <> OLD.source_audit_event_id
       OR NEW.extraction_batch_id IS DISTINCT FROM OLD.extraction_batch_id
       OR NEW.control_assignment_id IS DISTINCT FROM
            OLD.control_assignment_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.source_matter_version <> OLD.source_matter_version
       OR NEW.target_matter_version <> OLD.target_matter_version
       OR NOT transition_valid
       OR NEW.updated_at <= OLD.updated_at THEN
        RAISE EXCEPTION 'case Agent snapshot refresh transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE public.case_agent_ledger_exception_managed_evidence_requests (
    evidence_request_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    followup_id uuid NOT NULL UNIQUE,
    origin_exception_decision_id uuid NOT NULL UNIQUE,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    acceptance_criteria jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(acceptance_criteria) = 'object'
        AND acceptance_criteria @> '{"new_source_required": true}'::jsonb
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    requested_by uuid NOT NULL,
    requested_matter_version integer NOT NULL CHECK (
        requested_matter_version > 0
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (evidence_request_id, firm_id, matter_id),
    UNIQUE (evidence_request_id, followup_id, firm_id, matter_id),
    FOREIGN KEY (followup_id, subject_hash, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_followups(
            followup_id, subject_hash, firm_id, matter_id
        ),
    FOREIGN KEY (origin_exception_decision_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_group_decisions(
            exception_decision_id, firm_id, matter_id
        ),
    FOREIGN KEY (requested_by, firm_id)
        REFERENCES public.users(user_id, firm_id)
);

CREATE TABLE public.case_agent_ledger_exception_evidence_source_bindings (
    source_binding_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    evidence_request_id uuid NOT NULL,
    followup_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    source_type text NOT NULL CHECK (
        source_type IN ('EVIDENCE_FILE', 'MATERIAL_OBJECT')
    ),
    source_object_id uuid NOT NULL,
    source_content_hash char(64) NOT NULL CHECK (
        source_content_hash ~ '^[0-9a-f]{64}$'
    ),
    bound_by uuid NOT NULL,
    expected_matter_version integer NOT NULL CHECK (
        expected_matter_version > 0
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    binding_hash char(64) NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    bound_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (evidence_request_id, source_type, source_object_id),
    UNIQUE (source_binding_id, firm_id, matter_id),
    FOREIGN KEY (evidence_request_id, followup_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_managed_evidence_requests(
            evidence_request_id, followup_id, firm_id, matter_id
        ),
    FOREIGN KEY (bound_by, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id)
);

CREATE INDEX case_agent_ledger_exception_evidence_source_matter_idx
    ON public.case_agent_ledger_exception_evidence_source_bindings(
        firm_id, matter_id, evidence_request_id, source_type, source_object_id
    );

CREATE TABLE public.case_agent_ledger_exception_followup_events (
    followup_event_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    followup_id uuid NOT NULL,
    event_sequence integer NOT NULL CHECK (event_sequence > 0),
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    event_type text NOT NULL CHECK (event_type IN (
        'FOLLOWUP_ACTIVATED',
        'REEXTRACTION_VERIFIED_AND_STAGED',
        'MORE_EVIDENCE_CONFIRMED',
        'DEFER_RESUMED',
        'FOLLOWUP_WITHDRAWN',
        'FOLLOWUP_SUPERSEDED'
    )),
    state_after text NOT NULL CHECK (state_after IN (
        'ACTIVE', 'SATISFIED', 'RESUMED', 'WITHDRAWN', 'SUPERSEDED'
    )),
    actor_id uuid NOT NULL,
    web_session_id uuid,
    expected_matter_version integer NOT NULL CHECK (
        expected_matter_version > 0
    ),
    managed_evidence_request_id uuid,
    managed_evidence_source_set_hash char(64) CHECK (
        managed_evidence_source_set_hash IS NULL
        OR managed_evidence_source_set_hash ~ '^[0-9a-f]{64}$'
    ),
    managed_evidence_source_count integer CHECK (
        managed_evidence_source_count IS NULL
        OR managed_evidence_source_count BETWEEN 1 AND 100
    ),
    reextraction_binding_id uuid,
    reason_note text CHECK (
        reason_note IS NULL OR (
            reason_note = pg_catalog.btrim(reason_note)
            AND pg_catalog.length(reason_note) BETWEEN 1 AND 500
            AND pg_catalog.octet_length(reason_note) <= 2000
            AND reason_note !~ '[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F]'
        )
    ),
    idempotency_key text NOT NULL CHECK (
        pg_catalog.length(idempotency_key) BETWEEN 1 AND 200
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    event_hash char(64) NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    audit_event_id uuid,
    occurred_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (followup_id, event_sequence),
    UNIQUE (followup_event_id, firm_id, matter_id),
    FOREIGN KEY (followup_id, subject_hash, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_followups(
            followup_id, subject_hash, firm_id, matter_id
        ),
    FOREIGN KEY (actor_id, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (web_session_id)
        REFERENCES public.web_sessions(session_id),
    FOREIGN KEY (managed_evidence_request_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_managed_evidence_requests(
            evidence_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (audit_event_id)
        REFERENCES public.audit_events(event_id),
    CHECK (
        (event_type = 'FOLLOWUP_ACTIVATED'
            AND state_after = 'ACTIVE'
            AND web_session_id IS NULL
            AND managed_evidence_request_id IS NULL
            AND managed_evidence_source_set_hash IS NULL
            AND managed_evidence_source_count IS NULL
            AND reextraction_binding_id IS NULL
            AND reason_note IS NULL
            AND audit_event_id IS NULL)
        OR (event_type = 'REEXTRACTION_VERIFIED_AND_STAGED'
            AND state_after = 'SATISFIED'
            AND web_session_id IS NULL
            AND managed_evidence_request_id IS NULL
            AND managed_evidence_source_set_hash IS NULL
            AND managed_evidence_source_count IS NULL
            AND reextraction_binding_id IS NOT NULL
            AND audit_event_id IS NOT NULL)
        OR (event_type = 'MORE_EVIDENCE_CONFIRMED'
            AND state_after = 'SATISFIED'
            AND web_session_id IS NOT NULL
            AND managed_evidence_request_id IS NOT NULL
            AND managed_evidence_source_set_hash IS NOT NULL
            AND managed_evidence_source_count IS NOT NULL
            AND reextraction_binding_id IS NULL
            AND reason_note IS NOT NULL
            AND audit_event_id IS NOT NULL)
        OR (event_type = 'DEFER_RESUMED'
            AND state_after = 'RESUMED'
            AND web_session_id IS NOT NULL
            AND managed_evidence_request_id IS NULL
            AND managed_evidence_source_set_hash IS NULL
            AND managed_evidence_source_count IS NULL
            AND reextraction_binding_id IS NULL
            AND reason_note IS NOT NULL
            AND audit_event_id IS NOT NULL)
        OR (event_type = 'FOLLOWUP_WITHDRAWN'
            AND state_after = 'WITHDRAWN'
            AND web_session_id IS NOT NULL
            AND managed_evidence_request_id IS NULL
            AND managed_evidence_source_set_hash IS NULL
            AND managed_evidence_source_count IS NULL
            AND reextraction_binding_id IS NULL
            AND reason_note IS NOT NULL
            AND audit_event_id IS NOT NULL)
        OR (event_type = 'FOLLOWUP_SUPERSEDED'
            AND state_after = 'SUPERSEDED'
            AND web_session_id IS NOT NULL
            AND managed_evidence_request_id IS NULL
            AND managed_evidence_source_set_hash IS NULL
            AND managed_evidence_source_count IS NULL
            AND reextraction_binding_id IS NULL
            AND reason_note IS NOT NULL
            AND audit_event_id IS NOT NULL)
    )
);

CREATE TABLE public.case_agent_ledger_exception_followup_heads (
    followup_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    current_state text NOT NULL CHECK (current_state IN (
        'ACTIVE', 'SATISFIED', 'RESUMED', 'WITHDRAWN', 'SUPERSEDED'
    )),
    head_event_id uuid NOT NULL UNIQUE,
    head_sequence integer NOT NULL CHECK (head_sequence > 0),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (followup_id, subject_hash, firm_id, matter_id),
    FOREIGN KEY (followup_id, subject_hash, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_followups(
            followup_id, subject_hash, firm_id, matter_id
        ),
    FOREIGN KEY (head_event_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_followup_events(
            followup_event_id, firm_id, matter_id
        )
);

CREATE UNIQUE INDEX case_agent_ledger_exception_followup_one_active_subject
    ON public.case_agent_ledger_exception_followup_heads(
        firm_id, matter_id, subject_hash
    )
    WHERE current_state = 'ACTIVE';
CREATE INDEX case_agent_ledger_exception_followup_active_matter_idx
    ON public.case_agent_ledger_exception_followup_heads(
        firm_id, matter_id, updated_at, followup_id
    )
    WHERE current_state = 'ACTIVE';
CREATE INDEX case_agent_ledger_exception_followup_events_history_idx
    ON public.case_agent_ledger_exception_followup_events(
        firm_id, matter_id, followup_id, event_sequence
    );

-- The task binding is created before execution and is immutable.  It is the
-- missing server-verifiable edge between the lawyer's exact exception route
-- and the later extraction batch; merely observing a later same-matter batch
-- is never sufficient.
CREATE TABLE public.case_agent_ledger_exception_reextraction_task_bindings (
    task_binding_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    followup_id uuid NOT NULL,
    origin_exception_decision_id uuid NOT NULL,
    binding_sequence integer NOT NULL CHECK (binding_sequence > 0),
    supersedes_task_binding_id uuid,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    graph_version bigint NOT NULL CHECK (graph_version > 0),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    task_id uuid NOT NULL,
    task_input_hash char(64) NOT NULL CHECK (
        task_input_hash ~ '^[0-9a-f]{64}$'
    ),
    required_source_set_hash char(64) NOT NULL CHECK (
        required_source_set_hash ~ '^[0-9a-f]{64}$'
    ),
    required_source_count integer NOT NULL CHECK (
        required_source_count BETWEEN 1 AND 500
    ),
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    bound_by uuid NOT NULL,
    expected_matter_version integer NOT NULL CHECK (
        expected_matter_version > 0
    ),
    idempotency_key text NOT NULL CHECK (
        pg_catalog.length(idempotency_key) BETWEEN 16 AND 128
        AND idempotency_key ~ '^[A-Za-z0-9._~-]+$'
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    binding_hash char(64) NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    bound_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (task_binding_id, firm_id, matter_id),
    UNIQUE (task_binding_id, followup_id, firm_id, matter_id),
    UNIQUE (followup_id, binding_sequence),
    UNIQUE (followup_id, run_id, graph_id, task_id, firm_id, matter_id),
    UNIQUE (firm_id, matter_id, bound_by, idempotency_key),
    FOREIGN KEY (followup_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_followups(
            followup_id, firm_id, matter_id
        ),
    FOREIGN KEY (origin_exception_decision_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_group_decisions(
            exception_decision_id, firm_id, matter_id
        ),
    FOREIGN KEY (
        supersedes_task_binding_id, followup_id, firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_exception_reextraction_task_bindings(
        task_binding_id, followup_id, firm_id, matter_id
    ),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES public.case_agent_tasks(
            graph_id, task_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (bound_by, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id)
);

CREATE INDEX case_agent_ledger_exception_reextraction_task_matter_idx
    ON public.case_agent_ledger_exception_reextraction_task_bindings(
        firm_id, matter_id, followup_id, run_id, graph_version
    );

CREATE TABLE public.case_agent_ledger_exception_reextraction_task_binding_heads (
    followup_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    current_task_binding_id uuid NOT NULL UNIQUE,
    binding_sequence integer NOT NULL CHECK (binding_sequence > 0),
    current_run_id uuid NOT NULL,
    current_graph_version bigint NOT NULL CHECK (current_graph_version > 0),
    expected_matter_version integer NOT NULL CHECK (
        expected_matter_version > 0
    ),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (followup_id, firm_id, matter_id),
    FOREIGN KEY (
        current_task_binding_id, followup_id, firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_exception_reextraction_task_bindings(
        task_binding_id, followup_id, firm_id, matter_id
    ),
    FOREIGN KEY (followup_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_followups(
            followup_id, firm_id, matter_id
        ),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id)
);

CREATE INDEX case_agent_ledger_exception_reextraction_task_head_matter_idx
    ON public.case_agent_ledger_exception_reextraction_task_binding_heads(
        firm_id, matter_id, expected_matter_version,
        current_run_id, current_graph_version, followup_id
    );

CREATE TABLE public.case_agent_ledger_exception_reextraction_bindings (
    reextraction_binding_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    followup_id uuid NOT NULL UNIQUE,
    task_binding_id uuid NOT NULL UNIQUE,
    origin_exception_decision_id uuid NOT NULL UNIQUE,
    reextraction_batch_id uuid NOT NULL,
    reextraction_run_id uuid NOT NULL,
    verification_receipt_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    bound_by uuid NOT NULL,
    binding_hash char(64) NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    bound_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (reextraction_binding_id, firm_id, matter_id),
    FOREIGN KEY (followup_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_followups(
            followup_id, firm_id, matter_id
        ),
    FOREIGN KEY (task_binding_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_reextraction_task_bindings(
            task_binding_id, firm_id, matter_id
        ),
    FOREIGN KEY (origin_exception_decision_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_group_decisions(
            exception_decision_id, firm_id, matter_id
        ),
    FOREIGN KEY (reextraction_batch_id, reextraction_run_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_extraction_batches(
            extraction_batch_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (verification_receipt_id)
        REFERENCES public.case_agent_verification_receipts(
            verification_receipt_id
        ),
    FOREIGN KEY (bound_by, firm_id)
        REFERENCES public.users(user_id, firm_id)
);

ALTER TABLE public.case_agent_ledger_exception_followup_events
    ADD CONSTRAINT case_agent_ledger_exception_followup_reextraction_binding_fk
    FOREIGN KEY (reextraction_binding_id, firm_id, matter_id)
    REFERENCES public.case_agent_ledger_exception_reextraction_bindings(
        reextraction_binding_id, firm_id, matter_id
    );

CREATE TABLE public.case_agent_ledger_exception_duplicate_dispositions (
    duplicate_disposition_id uuid PRIMARY KEY DEFAULT public.gen_random_uuid(),
    origin_exception_decision_id uuid NOT NULL UNIQUE,
    origin_exception_group_id uuid NOT NULL,
    origin_extraction_batch_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    decision_hash char(64) NOT NULL CHECK (decision_hash ~ '^[0-9a-f]{64}$'),
    decided_by uuid NOT NULL,
    decided_matter_version integer NOT NULL CHECK (
        decided_matter_version > 0
    ),
    supersedes_disposition_id uuid,
    recorded_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (duplicate_disposition_id, subject_hash, firm_id, matter_id),
    FOREIGN KEY (origin_exception_decision_id, firm_id, matter_id)
        REFERENCES public.case_agent_ledger_exception_group_decisions(
            exception_decision_id, firm_id, matter_id
        ),
    FOREIGN KEY (
        origin_exception_group_id, origin_extraction_batch_id,
        firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_exception_groups(
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ),
    FOREIGN KEY (decided_by, firm_id)
        REFERENCES public.users(user_id, firm_id),
    FOREIGN KEY (
        supersedes_disposition_id, subject_hash, firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_exception_duplicate_dispositions(
        duplicate_disposition_id, subject_hash, firm_id, matter_id
    )
);

CREATE TABLE public.case_agent_ledger_exception_duplicate_heads (
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    current_disposition_id uuid NOT NULL UNIQUE,
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (firm_id, matter_id, subject_hash),
    FOREIGN KEY (
        current_disposition_id, subject_hash, firm_id, matter_id
    ) REFERENCES public.case_agent_ledger_exception_duplicate_dispositions(
        duplicate_disposition_id, subject_hash, firm_id, matter_id
    ),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id)
);

CREATE INDEX case_agent_ledger_exception_duplicate_current_matter_idx
    ON public.case_agent_ledger_exception_duplicate_heads(
        firm_id, matter_id, subject_hash
    );

CREATE FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session(
    input_session_id uuid,
    input_matter_id uuid,
    input_replacement_run_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    session_row record;
    matter_version integer;
    prior_row record;
    current_row record;
    replacement_row record;
    next_assignment_id uuid;
    audit_id uuid;
    next_sequence integer;
    transition_reason text;
    response_json jsonb;
BEGIN
    IF input_expected_version IS NULL OR input_expected_version < 1
       OR input_idempotency_key IS NULL
       OR input_idempotency_key !~ '^[A-Za-z0-9._~-]{16,128}$'
       OR input_request_hash IS NULL
       OR input_request_hash !~ '^[0-9a-f]{64}$'
       OR input_request_hash IS DISTINCT FROM
            public.case_agent_ledger_exception_control_transfer_request_hash(
                input_matter_id, input_expected_version,
                input_replacement_run_id
            ) THEN
        RAISE EXCEPTION 'ledger exception control transfer input is invalid';
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', '', true);
    PERFORM pg_catalog.set_config(
        'app.web_session_id', input_session_id::text, true
    );
    SELECT session.session_id, session.firm_id, session.user_id,
           session.issuer, session.authenticated_at, session.expires_at
      INTO session_row
      FROM public.web_sessions session
     WHERE session.session_id = input_session_id
       AND session.revoked_at IS NULL
       AND session.expires_at > pg_catalog.clock_timestamp()
       AND session.authenticated_at <= pg_catalog.clock_timestamp()
       AND session.issuer LIKE 'https://%'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception control transfer requires a live OIDC/MFA Web session';
    END IF;
    PERFORM pg_catalog.set_config(
        'app.firm_id', session_row.firm_id::text, true
    );
    SELECT matter.version
      INTO matter_version
      FROM public.matters matter
     WHERE matter.matter_id = input_matter_id
       AND matter.firm_id = session_row.firm_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception control transfer matter is missing';
    END IF;
    PERFORM 1
      FROM public.users actor
     WHERE actor.user_id = session_row.user_id
       AND actor.firm_id = session_row.firm_id
       AND actor.status = 'ACTIVE'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception control transfer actor is inactive';
    END IF;
    PERFORM 1
      FROM public.matter_actor_roles role_binding
     WHERE role_binding.matter_id = input_matter_id
       AND role_binding.firm_id = session_row.firm_id
       AND role_binding.user_id = session_row.user_id
       AND role_binding.role = 'LEAD_LAWYER'
       AND role_binding.revoked_at IS NULL
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'ledger exception control transfer requires the current lead lawyer';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        session_row.user_id::text || '|' || input_matter_id::text ||
        '|TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL|' || input_idempotency_key,
        0
    ));

    SELECT idempotency.request_hash, idempotency.response_json
      INTO prior_row
      FROM public.command_idempotency idempotency
     WHERE idempotency.firm_id = session_row.firm_id
       AND idempotency.matter_id = input_matter_id
       AND idempotency.actor_id = session_row.user_id
       AND idempotency.command_name =
            'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL'
       AND idempotency.idempotency_key = input_idempotency_key;
    IF FOUND THEN
        IF prior_row.request_hash IS DISTINCT FROM input_request_hash
           OR prior_row.response_json->>'command_name' IS DISTINCT FROM
                'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL'
           OR prior_row.response_json->>'idempotency_key' IS DISTINCT FROM
                input_idempotency_key
           OR prior_row.response_json->>'matter_id' IS DISTINCT FROM
                input_matter_id::text
           OR prior_row.response_json->>'matter_version' IS DISTINCT FROM
                input_expected_version::text
           OR prior_row.response_json->>'object_type' IS DISTINCT FROM
                'CASE_LEDGER_EXCEPTION_CONTROL_ASSIGNMENT'
           OR prior_row.response_json->>'control_health' IS DISTINCT FROM
                'HEALTHY'
           OR NOT EXISTS (
                SELECT 1
                  FROM public.case_agent_ledger_exception_control_assignments
                        assignment
                  JOIN public.audit_events audit
                    ON audit.event_id = assignment.audit_event_id
                   AND audit.firm_id = assignment.firm_id
                   AND audit.matter_id = assignment.matter_id
                 WHERE assignment.control_assignment_id =
                        (prior_row.response_json->>'object_id')::uuid
                   AND assignment.firm_id = session_row.firm_id
                   AND assignment.matter_id = input_matter_id
                   AND assignment.control_run_id = input_replacement_run_id
                   AND assignment.transition_type = 'TRANSFERRED'
                   AND assignment.state_after = 'HEALTHY'
                   AND assignment.actor_id = session_row.user_id
                   AND assignment.web_session_id = input_session_id
                   AND assignment.expected_matter_version =
                        input_expected_version
                   AND assignment.request_hash = input_request_hash
                   AND audit.event_id =
                        (prior_row.response_json->>'audit_event_id')::uuid
                   AND audit.event_type =
                        'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED'
                   AND audit.input_version = input_expected_version
                   AND audit.output_version = input_expected_version
           ) THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger exception control transfer replay differs',
                ERRCODE = 'P4092';
        END IF;
        RETURN prior_row.response_json;
    END IF;
    IF matter_version <> input_expected_version THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control transfer matter version is stale',
            ERRCODE = 'P4091';
    END IF;

    SELECT head.current_control_assignment_id,
           head.current_state, head.head_sequence,
           assignment.control_run_id, assignment.assigned_at
      INTO current_row
      FROM public.case_agent_ledger_exception_control_heads head
      JOIN public.case_agent_ledger_exception_control_assignments assignment
        ON assignment.control_assignment_id =
                head.current_control_assignment_id
       AND assignment.firm_id = head.firm_id
       AND assignment.matter_id = head.matter_id
       AND assignment.state_after = head.current_state
       AND assignment.assignment_sequence = head.head_sequence
     WHERE head.firm_id = session_row.firm_id
       AND head.matter_id = input_matter_id
     FOR UPDATE OF head;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_followup_heads followup_head
         WHERE followup_head.firm_id = session_row.firm_id
           AND followup_head.matter_id = input_matter_id
           AND followup_head.current_state = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control transfer has no active authority',
            ERRCODE = 'P4092';
    END IF;
    IF current_row.control_run_id = input_replacement_run_id THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception replacement run is already current',
            ERRCODE = 'P4092';
    END IF;

    SELECT run.run_id, run.status, run.current_event_version,
           run.snapshot_matter_version, run.created_at
      INTO replacement_row
      FROM public.case_agent_runs run
     WHERE run.run_id = input_replacement_run_id
       AND run.firm_id = session_row.firm_id
       AND run.matter_id = input_matter_id
       AND run.created_by = session_row.user_id
       AND run.created_at > current_row.assigned_at
       AND run.snapshot_matter_version = input_expected_version
       AND run.status IN ('CREATED', 'PLANNING')
       AND run.current_event_version IN (1, 2)
       AND run.current_graph_id IS NULL
       AND run.current_graph_version IS NULL
       AND run.current_graph_hash IS NULL
       AND run.is_stale IS DISTINCT FROM true
       AND run.is_cancelled IS DISTINCT FROM true
       AND EXISTS (
            SELECT 1
              FROM public.case_agent_events created_event
             WHERE created_event.run_id = run.run_id
               AND created_event.firm_id = run.firm_id
               AND created_event.matter_id = run.matter_id
               AND created_event.event_sequence = 1
               AND created_event.event_type = 'RUN_CREATED'
               AND created_event.actor_id = session_row.user_id
       )
       AND (
            run.current_event_version = 1
            OR EXISTS (
                SELECT 1
                  FROM public.case_agent_events planning_event
                 WHERE planning_event.run_id = run.run_id
                   AND planning_event.firm_id = run.firm_id
                   AND planning_event.matter_id = run.matter_id
                   AND planning_event.event_sequence = 2
                   AND planning_event.event_type = 'PLANNING_STARTED'
            )
       )
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'replacement control must be a new current-snapshot run owned by the lead lawyer';
    END IF;

    next_assignment_id := public.gen_random_uuid();
    audit_id := public.gen_random_uuid();
    next_sequence := current_row.head_sequence + 1;
    transition_reason := CASE current_row.current_state
        WHEN 'RECOVERY_REQUIRED' THEN 'RECOVER_FAILED_CONTROL_RUN'
        ELSE 'PROACTIVE_CONTROL_TRANSFER'
    END;
    INSERT INTO public.audit_events (
        event_id, firm_id, matter_id, actor_id, event_type,
        input_version, output_version, request_id, payload
    ) VALUES (
        audit_id, session_row.firm_id, input_matter_id,
        session_row.user_id, 'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED',
        input_expected_version, input_expected_version,
        public.gen_random_uuid(), pg_catalog.jsonb_build_object(
            'control_assignment_id', next_assignment_id,
            'replacement_run_id', input_replacement_run_id,
            'supersedes_control_assignment_id',
                current_row.current_control_assignment_id,
            'reason_code', transition_reason,
            'request_hash', input_request_hash,
            'formal_ledger_write', false,
            'legal_conclusion', false
        )
    );
    INSERT INTO public.case_agent_ledger_exception_control_assignments (
        control_assignment_id, firm_id, matter_id, assignment_sequence,
        control_run_id, state_after, transition_type,
        supersedes_control_assignment_id, actor_id, web_session_id,
        expected_matter_version, reason_code, idempotency_key,
        request_hash, audit_event_id
    ) VALUES (
        next_assignment_id, session_row.firm_id, input_matter_id,
        next_sequence, input_replacement_run_id, 'HEALTHY', 'TRANSFERRED',
        current_row.current_control_assignment_id, session_row.user_id,
        input_session_id, input_expected_version, transition_reason,
        input_idempotency_key, input_request_hash, audit_id
    );
    UPDATE public.case_agent_ledger_exception_control_heads head
       SET current_control_assignment_id = next_assignment_id,
           current_state = 'HEALTHY',
           head_sequence = next_sequence,
           updated_at = pg_catalog.clock_timestamp()
     WHERE head.firm_id = session_row.firm_id
       AND head.matter_id = input_matter_id
       AND head.current_control_assignment_id =
            current_row.current_control_assignment_id
       AND head.head_sequence = current_row.head_sequence;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception control head changed before transfer',
            ERRCODE = 'P4092';
    END IF;
    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        session_row.firm_id, input_matter_id, input_expected_version,
        'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED',
        pg_catalog.jsonb_build_object(
            'audit_event_id', audit_id,
            'control_assignment_id', next_assignment_id,
            'replacement_run_id', input_replacement_run_id,
            'control_health', 'HEALTHY',
            'wakeup_source', 'CASE_AGENT_RUN_CREATED'
        )
    );
    response_json := pg_catalog.jsonb_build_object(
        'command_name', 'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL',
        'idempotency_key', input_idempotency_key,
        'matter_id', input_matter_id::text,
        'matter_version', input_expected_version,
        'audit_event_id', audit_id::text,
        'object_type', 'CASE_LEDGER_EXCEPTION_CONTROL_ASSIGNMENT',
        'object_id', next_assignment_id::text,
        'control_health', 'HEALTHY'
    );
    INSERT INTO public.command_idempotency (
        firm_id, matter_id, actor_id, command_name, idempotency_key,
        request_hash, response_json
    ) VALUES (
        session_row.firm_id, input_matter_id, session_row.user_id,
        'TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL', input_idempotency_key,
        input_request_hash, response_json
    );
    RETURN response_json;
END;
$$;

CREATE FUNCTION public.mark_case_agent_ledger_exception_control_recovery_required()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_row record;
    matter_version integer;
    next_assignment_id uuid;
    next_sequence integer;
    transition_hash char(64);
BEGIN
    IF NEW.event_type <> 'VERIFICATION_FAILED' THEN
        RETURN NEW;
    END IF;
    PERFORM pg_catalog.set_config('app.firm_id', NEW.firm_id::text, true);
    SELECT head.current_control_assignment_id,
           head.current_state, head.head_sequence,
           assignment.control_run_id
      INTO current_row
      FROM public.case_agent_ledger_exception_control_heads head
      JOIN public.case_agent_ledger_exception_control_assignments assignment
        ON assignment.control_assignment_id =
                head.current_control_assignment_id
       AND assignment.firm_id = head.firm_id
       AND assignment.matter_id = head.matter_id
       AND assignment.state_after = head.current_state
       AND assignment.assignment_sequence = head.head_sequence
     WHERE head.firm_id = NEW.firm_id
       AND head.matter_id = NEW.matter_id
     FOR UPDATE OF head;
    IF NOT FOUND OR current_row.control_run_id <> NEW.run_id
       OR current_row.current_state = 'RECOVERY_REQUIRED'
       OR NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads followup_head
             WHERE followup_head.firm_id = NEW.firm_id
               AND followup_head.matter_id = NEW.matter_id
               AND followup_head.current_state = 'ACTIVE'
       ) THEN
        RETURN NEW;
    END IF;
    SELECT matter.version
      INTO matter_version
      FROM public.matters matter
     WHERE matter.matter_id = NEW.matter_id
       AND matter.firm_id = NEW.firm_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'failed control run matter is missing';
    END IF;
    next_assignment_id := public.gen_random_uuid();
    next_sequence := current_row.head_sequence + 1;
    transition_hash := pg_catalog.encode(public.digest(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema_version',
                'case-ledger-exception-control-assignment-v1',
            'transition_type', 'RECOVERY_REQUIRED',
            'source_agent_event_id', NEW.event_id,
            'control_run_id', NEW.run_id,
            'matter_version', matter_version
        )::text, 'UTF8'
    ), 'sha256'), 'hex');
    INSERT INTO public.case_agent_ledger_exception_control_assignments (
        control_assignment_id, firm_id, matter_id, assignment_sequence,
        control_run_id, state_after, transition_type,
        supersedes_control_assignment_id, source_agent_event_id,
        actor_id, expected_matter_version, reason_code,
        idempotency_key, request_hash
    ) VALUES (
        next_assignment_id, NEW.firm_id, NEW.matter_id, next_sequence,
        NEW.run_id, 'RECOVERY_REQUIRED', 'RECOVERY_REQUIRED',
        current_row.current_control_assignment_id, NEW.event_id,
        NEW.actor_id, matter_version, 'CONTROL_RUN_VERIFICATION_FAILED',
        'agent-event:' || NEW.event_id::text, transition_hash
    );
    UPDATE public.case_agent_ledger_exception_control_heads head
       SET current_control_assignment_id = next_assignment_id,
           current_state = 'RECOVERY_REQUIRED',
           head_sequence = next_sequence,
           updated_at = pg_catalog.clock_timestamp()
     WHERE head.firm_id = NEW.firm_id
       AND head.matter_id = NEW.matter_id
       AND head.current_control_assignment_id =
            current_row.current_control_assignment_id
       AND head.head_sequence = current_row.head_sequence;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'failed control run head changed before recovery marker';
    END IF;
    INSERT INTO public.outbox_events (
        firm_id, matter_id, aggregate_version, event_type, payload
    ) VALUES (
        NEW.firm_id, NEW.matter_id, matter_version,
        'CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY_REQUIRED',
        pg_catalog.jsonb_build_object(
            'control_assignment_id', next_assignment_id,
            'source_agent_event_id', NEW.event_id,
            'control_health', 'RECOVERY_REQUIRED'
        )
    );
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.guard_case_agent_ledger_exception_control_head()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.head_sequence <> OLD.head_sequence + 1
       OR NEW.updated_at <= OLD.updated_at
       OR NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_control_assignments
                    assignment
             WHERE assignment.control_assignment_id =
                    NEW.current_control_assignment_id
               AND assignment.firm_id = NEW.firm_id
               AND assignment.matter_id = NEW.matter_id
               AND assignment.state_after = NEW.current_state
               AND assignment.assignment_sequence = NEW.head_sequence
               AND assignment.supersedes_control_assignment_id =
                    OLD.current_control_assignment_id
       ) THEN
        RAISE EXCEPTION
            'case Agent ledger exception control head transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.prohibit_case_agent_ledger_exception_lifecycle_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'case Agent ledger exception lifecycle records are append-only';
END;
$$;

CREATE FUNCTION public.guard_case_agent_ledger_exception_followup_head()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR OLD.current_state <> 'ACTIVE'
       OR NEW.current_state = 'ACTIVE'
       OR NEW.head_sequence <> OLD.head_sequence + 1
       OR NEW.updated_at <= OLD.updated_at
       OR NEW.followup_id <> OLD.followup_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.subject_hash <> OLD.subject_hash
       OR NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_events event
             WHERE event.followup_event_id = NEW.head_event_id
               AND event.followup_id = NEW.followup_id
               AND event.firm_id = NEW.firm_id
               AND event.matter_id = NEW.matter_id
               AND event.subject_hash = NEW.subject_hash
               AND event.event_sequence = NEW.head_sequence
               AND event.state_after = NEW.current_state
       ) THEN
        RAISE EXCEPTION 'case Agent ledger exception follow-up head transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.guard_case_agent_ledger_exception_duplicate_head()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.subject_hash <> OLD.subject_hash
       OR NEW.updated_at <= OLD.updated_at
       OR NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_duplicate_dispositions item
             WHERE item.duplicate_disposition_id = NEW.current_disposition_id
               AND item.firm_id = NEW.firm_id
               AND item.matter_id = NEW.matter_id
               AND item.subject_hash = NEW.subject_hash
               AND item.supersedes_disposition_id = OLD.current_disposition_id
       ) THEN
        RAISE EXCEPTION 'case Agent duplicate disposition head transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.guard_case_agent_ledger_exception_reextraction_task_binding_head()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.followup_id <> OLD.followup_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.binding_sequence <> OLD.binding_sequence + 1
       OR NEW.expected_matter_version < OLD.expected_matter_version
       OR (
            NEW.expected_matter_version = OLD.expected_matter_version
            AND (
                NEW.current_run_id <> OLD.current_run_id
                OR NEW.current_graph_version <= OLD.current_graph_version
            )
       )
       OR NEW.updated_at <= OLD.updated_at
       OR NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_reextraction_task_bindings binding
             WHERE binding.task_binding_id = NEW.current_task_binding_id
               AND binding.followup_id = NEW.followup_id
               AND binding.firm_id = NEW.firm_id
               AND binding.matter_id = NEW.matter_id
               AND binding.binding_sequence = NEW.binding_sequence
               AND binding.run_id = NEW.current_run_id
               AND binding.graph_version = NEW.current_graph_version
               AND binding.expected_matter_version =
                    NEW.expected_matter_version
               AND binding.supersedes_task_binding_id =
                    OLD.current_task_binding_id
       ) THEN
        RAISE EXCEPTION
            'case Agent ledger re-extraction task binding head transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_ledger_exception_followups_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_followups
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_control_assignments_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_control_assignments
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_control_heads_guard
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_control_heads
    FOR EACH ROW EXECUTE FUNCTION
        public.guard_case_agent_ledger_exception_control_head();
CREATE TRIGGER case_agent_ledger_exception_evidence_requests_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_managed_evidence_requests
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_evidence_sources_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_evidence_source_bindings
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_followup_events_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_followup_events
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_reextraction_bindings_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_reextraction_bindings
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_reextraction_tasks_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_reextraction_task_bindings
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_reextraction_task_heads_guard
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_reextraction_task_binding_heads
    FOR EACH ROW EXECUTE FUNCTION
        public.guard_case_agent_ledger_exception_reextraction_task_binding_head();
CREATE TRIGGER case_agent_ledger_exception_duplicate_dispositions_append_only
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_duplicate_dispositions
    FOR EACH ROW EXECUTE FUNCTION
        public.prohibit_case_agent_ledger_exception_lifecycle_mutation();
CREATE TRIGGER case_agent_ledger_exception_followup_heads_guard
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_followup_heads
    FOR EACH ROW EXECUTE FUNCTION
        public.guard_case_agent_ledger_exception_followup_head();
CREATE TRIGGER case_agent_ledger_exception_duplicate_heads_guard
    BEFORE UPDATE OR DELETE
    ON public.case_agent_ledger_exception_duplicate_heads
    FOR EACH ROW EXECUTE FUNCTION
        public.guard_case_agent_ledger_exception_duplicate_head();

CREATE TRIGGER case_agent_ledger_exception_control_failure_requires_recovery
    AFTER INSERT ON public.case_agent_events
    FOR EACH ROW
    WHEN (NEW.event_type = 'VERIFICATION_FAILED')
    EXECUTE FUNCTION
        public.mark_case_agent_ledger_exception_control_recovery_required();

CREATE FUNCTION public.initialize_case_agent_ledger_exception_lifecycle(
    input_exception_decision_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    decision_row record;
    subject char(64);
    followup_kind_value text;
    new_followup_id uuid;
    new_event_id uuid;
    new_event_hash char(64);
    new_evidence_request_id uuid;
    new_evidence_request_hash char(64);
    new_evidence_acceptance_criteria jsonb;
    prior_disposition_id uuid;
    new_disposition_id uuid;
    active_followup_count integer;
    current_control_row record;
    new_control_assignment_id uuid;
    new_control_sequence integer;
    required_source_count integer;
    required_source_set_hash char(64);
    active_reextraction_cohort_count integer;
    control_run_id_value uuid;
    control_extraction_batch_id_value uuid;
BEGIN
    PERFORM pg_catalog.set_config('app.firm_id', input_firm_id::text, true);
    SELECT decision.exception_decision_id,
           decision.exception_group_id,
           decision.extraction_batch_id,
           decision.run_id,
           decision.firm_id,
           decision.matter_id,
           decision.decision,
           decision.decision_hash,
           decision.expected_matter_version,
           decision.decided_by,
           decision.request_hash,
           exception_group.canonical_reason_codes,
           exception_group.source_policy,
           exception_group.risk_policy
      INTO decision_row
      FROM public.case_agent_ledger_exception_group_decisions decision
      JOIN public.case_agent_ledger_exception_groups exception_group
        ON exception_group.exception_group_id = decision.exception_group_id
       AND exception_group.extraction_batch_id = decision.extraction_batch_id
       AND exception_group.firm_id = decision.firm_id
       AND exception_group.matter_id = decision.matter_id
     WHERE decision.exception_decision_id = input_exception_decision_id
       AND decision.firm_id = input_firm_id
       AND decision.matter_id = input_matter_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger exception lifecycle origin decision is missing';
    END IF;
    subject := public.case_agent_ledger_exception_group_subject_hash(
        decision_row.exception_group_id,
        decision_row.firm_id,
        decision_row.matter_id
    );
    IF subject IS NULL THEN
        RAISE EXCEPTION 'ledger exception lifecycle lacks its immutable subject hashes';
    END IF;

    IF decision_row.decision = 'REJECT_AS_DUPLICATE' THEN
        IF EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_duplicate_dispositions item
             WHERE item.origin_exception_decision_id =
                    decision_row.exception_decision_id
        ) THEN
            RETURN;
        END IF;
        SELECT head.current_disposition_id
          INTO prior_disposition_id
          FROM public.case_agent_ledger_exception_duplicate_heads head
         WHERE head.firm_id = decision_row.firm_id
           AND head.matter_id = decision_row.matter_id
           AND head.subject_hash = subject
         FOR UPDATE;
        new_disposition_id := public.gen_random_uuid();
        INSERT INTO public.case_agent_ledger_exception_duplicate_dispositions (
            duplicate_disposition_id, origin_exception_decision_id,
            origin_exception_group_id, origin_extraction_batch_id,
            firm_id, matter_id, subject_hash, decision_hash, decided_by,
            decided_matter_version, supersedes_disposition_id
        ) VALUES (
            new_disposition_id, decision_row.exception_decision_id,
            decision_row.exception_group_id, decision_row.extraction_batch_id,
            decision_row.firm_id, decision_row.matter_id, subject,
            decision_row.decision_hash, decision_row.decided_by,
            decision_row.expected_matter_version, prior_disposition_id
        );
        IF prior_disposition_id IS NULL THEN
            INSERT INTO public.case_agent_ledger_exception_duplicate_heads (
                firm_id, matter_id, subject_hash, current_disposition_id
            ) VALUES (
                decision_row.firm_id, decision_row.matter_id, subject,
                new_disposition_id
            );
        ELSE
            UPDATE public.case_agent_ledger_exception_duplicate_heads head
               SET current_disposition_id = new_disposition_id,
                   updated_at = pg_catalog.clock_timestamp()
             WHERE head.firm_id = decision_row.firm_id
               AND head.matter_id = decision_row.matter_id
               AND head.subject_hash = subject
               AND head.current_disposition_id = prior_disposition_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'duplicate disposition head changed concurrently';
            END IF;
        END IF;
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_followups followup
         WHERE followup.origin_exception_decision_id =
                decision_row.exception_decision_id
    ) THEN
        RETURN;
    END IF;
    followup_kind_value := CASE decision_row.decision
        WHEN 'REQUEST_REEXTRACTION' THEN 'REEXTRACTION'
        WHEN 'REQUEST_MORE_EVIDENCE' THEN 'MORE_EVIDENCE'
        WHEN 'DEFER_WITH_REASON' THEN 'DEFERRED_REVIEW'
        ELSE NULL
    END;
    IF followup_kind_value IS NULL THEN
        RAISE EXCEPTION 'ledger exception route has no supported lifecycle';
    END IF;

    -- Effective control is one guarded matter head.  Immutable follow-up
    -- columns below retain their creation-time lineage only.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        decision_row.firm_id::text || '|' || decision_row.matter_id::text ||
        '|CASE_LEDGER_EXCEPTION_ACTIVE_CONTROL_RUN',
        0
    ));
    SELECT pg_catalog.count(*)::integer
      INTO active_followup_count
      FROM public.case_agent_ledger_exception_followup_heads head
     WHERE head.firm_id = decision_row.firm_id
       AND head.matter_id = decision_row.matter_id
       AND head.current_state = 'ACTIVE';
    SELECT head.current_control_assignment_id, head.current_state,
           head.head_sequence, assignment.control_run_id
      INTO current_control_row
      FROM public.case_agent_ledger_exception_control_heads head
      JOIN public.case_agent_ledger_exception_control_assignments assignment
        ON assignment.control_assignment_id =
                head.current_control_assignment_id
       AND assignment.firm_id = head.firm_id
       AND assignment.matter_id = head.matter_id
       AND assignment.state_after = head.current_state
       AND assignment.assignment_sequence = head.head_sequence
     WHERE head.firm_id = decision_row.firm_id
       AND head.matter_id = decision_row.matter_id
     FOR UPDATE OF head;
    IF active_followup_count > 0 AND NOT FOUND THEN
        RAISE EXCEPTION
            'active ledger exception follow-ups lack their current control head';
    END IF;
    IF active_followup_count = 0 THEN
        -- A new lifecycle begins only from the exact verified decision run.
        PERFORM 1
          FROM public.case_agent_runs control_run
          JOIN public.matters matter
            ON matter.matter_id = control_run.matter_id
           AND matter.firm_id = control_run.firm_id
          JOIN public.case_agent_run_inbox inbox
            ON inbox.run_id = control_run.run_id
           AND inbox.firm_id = control_run.firm_id
           AND inbox.matter_id = control_run.matter_id
         WHERE control_run.run_id = decision_row.run_id
           AND control_run.firm_id = decision_row.firm_id
           AND control_run.matter_id = decision_row.matter_id
           AND control_run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
           AND control_run.is_stale IS DISTINCT FROM true
           AND control_run.is_cancelled IS DISTINCT FROM true
           AND (
                control_run.snapshot_matter_version = matter.version
                OR EXISTS (
                    SELECT 1
                      FROM public.case_agent_snapshot_refresh_requests request
                     WHERE request.run_id = control_run.run_id
                       AND request.firm_id = control_run.firm_id
                       AND request.matter_id = control_run.matter_id
                       AND request.target_matter_version = matter.version
                       AND request.request_status = 'PENDING'
                )
           )
         FOR SHARE OF control_run, matter, inbox;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ledger exception control run is not safely refreshable',
                DETAIL = 'Select or recover one current non-stale, non-cancelled verified control run before applying 0049.';
        END IF;
        new_control_assignment_id := public.gen_random_uuid();
        IF current_control_row.current_control_assignment_id IS NULL THEN
            new_control_sequence := 1;
        ELSE
            new_control_sequence := current_control_row.head_sequence + 1;
        END IF;
        INSERT INTO public.case_agent_ledger_exception_control_assignments (
            control_assignment_id, firm_id, matter_id,
            assignment_sequence, control_run_id, state_after,
            transition_type, supersedes_control_assignment_id,
            source_exception_decision_id, actor_id,
            expected_matter_version, reason_code, idempotency_key,
            request_hash
        ) VALUES (
            new_control_assignment_id, decision_row.firm_id,
            decision_row.matter_id, new_control_sequence,
            decision_row.run_id, 'HEALTHY', 'INITIALIZED',
            current_control_row.current_control_assignment_id,
            decision_row.exception_decision_id, decision_row.decided_by,
            decision_row.expected_matter_version, 'FOLLOWUP_ACTIVATED',
            'origin:' || decision_row.exception_decision_id::text,
            decision_row.request_hash
        );
        IF current_control_row.current_control_assignment_id IS NULL THEN
            INSERT INTO public.case_agent_ledger_exception_control_heads (
                firm_id, matter_id, current_control_assignment_id,
                current_state, head_sequence
            ) VALUES (
                decision_row.firm_id, decision_row.matter_id,
                new_control_assignment_id, 'HEALTHY', new_control_sequence
            );
        ELSE
            UPDATE public.case_agent_ledger_exception_control_heads head
               SET current_control_assignment_id = new_control_assignment_id,
                   current_state = 'HEALTHY',
                   head_sequence = new_control_sequence,
                   updated_at = pg_catalog.clock_timestamp()
             WHERE head.firm_id = decision_row.firm_id
               AND head.matter_id = decision_row.matter_id
               AND head.current_control_assignment_id =
                    current_control_row.current_control_assignment_id
               AND head.head_sequence = current_control_row.head_sequence;
            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'ledger exception control head changed during initialization';
            END IF;
        END IF;
        current_control_row.current_control_assignment_id :=
            new_control_assignment_id;
        current_control_row.current_state := 'HEALTHY';
        current_control_row.head_sequence := new_control_sequence;
        current_control_row.control_run_id := decision_row.run_id;
    END IF;
    control_run_id_value := decision_row.run_id;
    control_extraction_batch_id_value := decision_row.extraction_batch_id;

    -- An unresolved route for the same source subject must be explicitly
    -- withdrawn or superseded first.  The migration never invents closure.
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_followup_heads head
         WHERE head.firm_id = decision_row.firm_id
           AND head.matter_id = decision_row.matter_id
           AND head.subject_hash = subject
           AND head.current_state = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION
            'an active ledger exception follow-up already owns this source subject';
    END IF;

    IF followup_kind_value = 'REEXTRACTION' THEN
        SELECT pg_catalog.count(DISTINCT page.evidence_page_id)::integer,
               pg_catalog.encode(public.digest(pg_catalog.convert_to(
                   pg_catalog.string_agg(
                       DISTINCT 'evidence-page:' || page.evidence_page_id::text,
                       E'\n' ORDER BY 'evidence-page:' ||
                            page.evidence_page_id::text
                   ), 'UTF8'
               ), 'sha256'), 'hex')
          INTO required_source_count, required_source_set_hash
          FROM public.case_agent_ledger_exception_group_members member
          JOIN public.case_agent_ledger_extraction_candidate_pages page
            ON page.extraction_candidate_id = member.extraction_candidate_id
           AND page.firm_id = member.firm_id
           AND page.matter_id = member.matter_id
         WHERE member.exception_group_id = decision_row.exception_group_id
           AND member.extraction_batch_id = decision_row.extraction_batch_id
           AND member.firm_id = decision_row.firm_id
           AND member.matter_id = decision_row.matter_id;
        IF required_source_count < 1 OR required_source_set_hash IS NULL THEN
            RAISE EXCEPTION
                're-extraction follow-up has no exact source pages';
        ELSIF required_source_count > 64 THEN
            RAISE EXCEPTION USING
                MESSAGE = 're-extraction follow-up exceeds the 64-page governed task window',
                DETAIL = 'REEXTRACTION_SOURCE_WINDOW_EXCEEDED',
                ERRCODE = 'P6401';
        END IF;
        WITH active_source_sets AS (
            SELECT pg_catalog.encode(public.digest(pg_catalog.convert_to(
                       pg_catalog.string_agg(
                           DISTINCT 'evidence-page:' ||
                                active_page.evidence_page_id::text,
                           E'\n' ORDER BY 'evidence-page:' ||
                                active_page.evidence_page_id::text
                       ), 'UTF8'
                   ), 'sha256'), 'hex') AS source_set_hash
              FROM public.case_agent_ledger_exception_followups active_followup
              JOIN public.case_agent_ledger_exception_followup_heads active_head
                ON active_head.followup_id = active_followup.followup_id
               AND active_head.firm_id = active_followup.firm_id
               AND active_head.matter_id = active_followup.matter_id
               AND active_head.current_state = 'ACTIVE'
              JOIN public.case_agent_ledger_exception_group_members active_member
                ON active_member.exception_group_id =
                        active_followup.origin_exception_group_id
               AND active_member.extraction_batch_id =
                        active_followup.origin_extraction_batch_id
               AND active_member.firm_id = active_followup.firm_id
               AND active_member.matter_id = active_followup.matter_id
              JOIN public.case_agent_ledger_extraction_candidate_pages
                    active_page
                ON active_page.extraction_candidate_id =
                        active_member.extraction_candidate_id
               AND active_page.firm_id = active_member.firm_id
               AND active_page.matter_id = active_member.matter_id
             WHERE active_followup.firm_id = decision_row.firm_id
               AND active_followup.matter_id = decision_row.matter_id
               AND active_followup.followup_kind = 'REEXTRACTION'
             GROUP BY active_followup.followup_id
        ), prospective AS (
            SELECT source_set_hash FROM active_source_sets
            UNION
            SELECT required_source_set_hash::text
        )
        SELECT pg_catalog.count(DISTINCT source_set_hash)::integer
          INTO active_reextraction_cohort_count
          FROM prospective;
        IF active_reextraction_cohort_count > 99 THEN
            RAISE EXCEPTION USING
                MESSAGE = 'active re-extraction obligations exceed the 99-cohort governed graph capacity',
                DETAIL = 'REEXTRACTION_GRAPH_COHORT_CAPACITY_EXCEEDED',
                ERRCODE = 'P9901';
        END IF;
    END IF;

    new_followup_id := public.gen_random_uuid();
    INSERT INTO public.case_agent_ledger_exception_followups (
        followup_id, origin_exception_decision_id,
        origin_exception_group_id, origin_extraction_batch_id,
        origin_run_id, control_extraction_batch_id, control_run_id,
        firm_id, matter_id, followup_kind, subject_hash,
        created_matter_version, created_by
    ) VALUES (
        new_followup_id, decision_row.exception_decision_id,
        decision_row.exception_group_id, decision_row.extraction_batch_id,
        decision_row.run_id, control_extraction_batch_id_value,
        control_run_id_value, decision_row.firm_id, decision_row.matter_id,
        followup_kind_value, subject, decision_row.expected_matter_version,
        decision_row.decided_by
    );

    IF followup_kind_value = 'MORE_EVIDENCE' THEN
        new_evidence_request_id := public.gen_random_uuid();
        new_evidence_acceptance_criteria := pg_catalog.jsonb_build_object(
            'new_source_required', true,
            'subject_hash', subject,
            'origin_reason_codes',
                pg_catalog.to_jsonb(decision_row.canonical_reason_codes),
            'source_policy', decision_row.source_policy,
            'risk_policy', decision_row.risk_policy
        );
        new_evidence_request_hash := pg_catalog.encode(public.digest(
            pg_catalog.convert_to(pg_catalog.jsonb_build_object(
                'schema_version', 'case-ledger-managed-evidence-request-v2',
                'followup_id', new_followup_id,
                'origin_exception_decision_id',
                    decision_row.exception_decision_id,
                'subject_hash', subject,
                'acceptance_criteria', new_evidence_acceptance_criteria
            )::text, 'UTF8'), 'sha256'
        ), 'hex');
        INSERT INTO public.case_agent_ledger_exception_managed_evidence_requests (
            evidence_request_id, followup_id, origin_exception_decision_id,
            firm_id, matter_id, subject_hash, acceptance_criteria,
            request_hash, requested_by, requested_matter_version
        ) VALUES (
            new_evidence_request_id, new_followup_id,
            decision_row.exception_decision_id, decision_row.firm_id,
            decision_row.matter_id, subject,
            new_evidence_acceptance_criteria, new_evidence_request_hash,
            decision_row.decided_by, decision_row.expected_matter_version
        );
    END IF;

    new_event_id := public.gen_random_uuid();
    new_event_hash := pg_catalog.encode(public.digest(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema_version', 'case-ledger-exception-followup-event-v1',
            'followup_id', new_followup_id,
            'event_sequence', 1,
            'event_type', 'FOLLOWUP_ACTIVATED',
            'state_after', 'ACTIVE',
            'origin_exception_decision_id', decision_row.exception_decision_id,
            'origin_decision_hash', decision_row.decision_hash,
            'subject_hash', subject,
            'control_run_id', control_run_id_value,
            'control_extraction_batch_id',
                control_extraction_batch_id_value
        )::text, 'UTF8'), 'sha256'
    ), 'hex');
    INSERT INTO public.case_agent_ledger_exception_followup_events (
        followup_event_id, followup_id, event_sequence, firm_id, matter_id,
        subject_hash, event_type, state_after, actor_id,
        expected_matter_version, idempotency_key, request_hash, event_hash
    ) VALUES (
        new_event_id, new_followup_id, 1, decision_row.firm_id,
        decision_row.matter_id, subject, 'FOLLOWUP_ACTIVATED', 'ACTIVE',
        decision_row.decided_by, decision_row.expected_matter_version,
        'origin:' || decision_row.exception_decision_id::text,
        decision_row.request_hash, new_event_hash
    );
    INSERT INTO public.case_agent_ledger_exception_followup_heads (
        followup_id, firm_id, matter_id, subject_hash, current_state,
        head_event_id, head_sequence
    ) VALUES (
        new_followup_id, decision_row.firm_id, decision_row.matter_id,
        subject, 'ACTIVE', new_event_id, 1
    );
END;
$$;

CREATE FUNCTION public.initialize_case_agent_ledger_exception_lifecycle_trigger()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM public.initialize_case_agent_ledger_exception_lifecycle(
        NEW.exception_decision_id, NEW.firm_id, NEW.matter_id
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_ledger_exception_decision_initializes_lifecycle
    AFTER INSERT
    ON public.case_agent_ledger_exception_group_decisions
    FOR EACH ROW EXECUTE FUNCTION
        public.initialize_case_agent_ledger_exception_lifecycle_trigger();

-- Backfill preserves every historical route.  If two historical active
-- follow-ups collide on one source subject, fail closed rather than guessing
-- which lawyer instruction superseded the other.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT decision.exception_decision_id,
               decision.firm_id, decision.matter_id
          FROM public.case_agent_ledger_exception_group_decisions decision
         ORDER BY decision.decided_at, decision.exception_decision_id
    LOOP
        PERFORM public.initialize_case_agent_ledger_exception_lifecycle(
            item.exception_decision_id, item.firm_id, item.matter_id
        );
    END LOOP;
END;
$$;

-- BEFORE triggers protect future promotion/activation only.  Refuse an
-- upgrade that would silently preserve a pre-0049 ACTIVE plan beside a newly
-- backfilled ACTIVE follow-up; invalidating that plan is a separate audited
-- lawyer-visible operation, not a migration guess.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_followup_heads head
          JOIN public.case_work_plans plan
            ON plan.firm_id = head.firm_id
           AND plan.matter_id = head.matter_id
           AND plan.status = 'ACTIVE'
         WHERE head.current_state = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION USING
            MESSAGE = '0049 refuses an ACTIVE work plan with an ACTIVE exception follow-up',
            DETAIL = 'Invalidate the legacy plan through an audited operation, then re-apply 0049.';
    END IF;
END;
$$;

-- 0049 deliberately fails closed until a future release can prove exact task
-- coverage for each active follow-up.  A prompt mentioning the follow-up is
-- not a verifiable obligation: neither promotion nor activation may proceed
-- while the matter owns an ACTIVE current head.  Closed history remains
-- queryable but is not a blocker.
CREATE FUNCTION public.block_active_exception_followup_work_plan_promotion()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_followup_heads head
         WHERE head.firm_id = NEW.firm_id
           AND head.matter_id = NEW.matter_id
           AND head.current_state = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'Agent work plan promotion is blocked by an active exception follow-up',
            ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_work_plan_active_exception_followup_block
    BEFORE INSERT ON public.case_agent_work_plan_promotions
    FOR EACH ROW EXECUTE FUNCTION
        public.block_active_exception_followup_work_plan_promotion();

CREATE FUNCTION public.block_active_exception_followup_work_plan_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.status = 'ACTIVE'
       AND EXISTS (
           SELECT 1
             FROM public.case_agent_ledger_exception_followup_heads head
            WHERE head.firm_id = NEW.firm_id
              AND head.matter_id = NEW.matter_id
              AND head.current_state = 'ACTIVE'
       ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'work plan activation is blocked by an active exception follow-up',
            ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_work_plan_active_exception_followup_activation_block
    BEFORE INSERT OR UPDATE ON public.case_work_plans
    FOR EACH ROW EXECUTE FUNCTION
        public.block_active_exception_followup_work_plan_activation();

ALTER TABLE public.case_agent_ledger_exception_followups
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_followups
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_control_assignments
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_control_assignments
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_control_heads
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_control_heads
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_managed_evidence_requests
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_managed_evidence_requests
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_evidence_source_bindings
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_evidence_source_bindings
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_followup_events
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_followup_events
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_followup_heads
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_followup_heads
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_reextraction_task_bindings
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_reextraction_task_bindings
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_reextraction_task_binding_heads
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_reextraction_task_binding_heads
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_reextraction_bindings
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_reextraction_bindings
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_duplicate_dispositions
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_duplicate_dispositions
    FORCE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_duplicate_heads
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_ledger_exception_duplicate_heads
    FORCE ROW LEVEL SECURITY;

CREATE POLICY case_agent_ledger_exception_followups_firm_isolation
    ON public.case_agent_ledger_exception_followups
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_control_assignments_firm_isolation
    ON public.case_agent_ledger_exception_control_assignments
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_control_heads_firm_isolation
    ON public.case_agent_ledger_exception_control_heads
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_evidence_requests_firm_isolation
    ON public.case_agent_ledger_exception_managed_evidence_requests
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_evidence_sources_firm_isolation
    ON public.case_agent_ledger_exception_evidence_source_bindings
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_followup_events_firm_isolation
    ON public.case_agent_ledger_exception_followup_events
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_followup_heads_firm_isolation
    ON public.case_agent_ledger_exception_followup_heads
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_reextraction_tasks_firm_isolation
    ON public.case_agent_ledger_exception_reextraction_task_bindings
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_reextraction_task_heads_firm_isolation
    ON public.case_agent_ledger_exception_reextraction_task_binding_heads
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_reextraction_bindings_firm_isolation
    ON public.case_agent_ledger_exception_reextraction_bindings
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_duplicate_dispositions_firm_isolation
    ON public.case_agent_ledger_exception_duplicate_dispositions
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));
CREATE POLICY case_agent_ledger_exception_duplicate_heads_firm_isolation
    ON public.case_agent_ledger_exception_duplicate_heads
    USING (firm_id::text = pg_catalog.current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = pg_catalog.current_setting('app.firm_id', true));

ALTER TABLE public.case_agent_ledger_exception_followups
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_control_assignments
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_control_heads
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_managed_evidence_requests
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_evidence_source_bindings
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_followup_events
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_followup_heads
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_reextraction_task_bindings
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_reextraction_task_binding_heads
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_reextraction_bindings
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_duplicate_dispositions
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER TABLE public.case_agent_ledger_exception_duplicate_heads
    OWNER TO lawcase_ledger_confirmation_owner;

ALTER FUNCTION public.case_agent_ledger_exception_group_subject_hash(
    uuid, uuid, uuid
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.case_agent_ledger_exception_control_transfer_request_hash(
    uuid, integer, uuid
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.prohibit_case_agent_ledger_exception_lifecycle_mutation()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.guard_case_agent_ledger_exception_control_head()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.guard_case_agent_ledger_exception_followup_head()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.guard_case_agent_ledger_exception_duplicate_head()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.guard_case_agent_ledger_exception_reextraction_task_binding_head()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.initialize_case_agent_ledger_exception_lifecycle(
    uuid, uuid, uuid
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.initialize_case_agent_ledger_exception_lifecycle_trigger()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.case_agent_ledger_exception_followup_request_hash(
    uuid, integer, uuid, text, uuid, uuid, jsonb, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.resolve_case_agent_ledger_exception_followup_from_web_session(
    uuid, uuid, uuid, integer, text, text, uuid, jsonb, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session(
    uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.case_agent_ledger_exception_reextraction_task_binding_request_hash(
    uuid, integer, uuid, uuid, uuid, uuid
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.bind_case_agent_ledger_exception_reextraction_task_from_worker(
    uuid, uuid, uuid, uuid, uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.satisfy_case_agent_ledger_reextraction_followup_from_worker(
    uuid, uuid, uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.case_agent_ledger_exception_reextraction_set_request_hash(
    uuid, integer, uuid, uuid
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.satisfy_case_agent_ledger_reextraction_set_from_worker(
    uuid, uuid, uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.enqueue_case_agent_snapshot_refresh_from_exception_followup()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.enqueue_case_agent_exception_control_run_refresh_outbox()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.enqueue_case_agent_exception_control_run_refresh()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.block_active_exception_control_run_cancellation()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.mark_case_agent_ledger_exception_control_recovery_required()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.block_active_exception_followup_work_plan_promotion()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.block_active_exception_followup_work_plan_activation()
    OWNER TO lawcase_ledger_confirmation_owner;

REVOKE ALL ON TABLE
    public.case_agent_ledger_exception_followups,
    public.case_agent_ledger_exception_control_assignments,
    public.case_agent_ledger_exception_control_heads,
    public.case_agent_ledger_exception_managed_evidence_requests,
    public.case_agent_ledger_exception_evidence_source_bindings,
    public.case_agent_ledger_exception_followup_events,
    public.case_agent_ledger_exception_followup_heads,
    public.case_agent_ledger_exception_reextraction_task_bindings,
    public.case_agent_ledger_exception_reextraction_task_binding_heads,
    public.case_agent_ledger_exception_reextraction_bindings,
    public.case_agent_ledger_exception_duplicate_dispositions,
    public.case_agent_ledger_exception_duplicate_heads
    FROM PUBLIC;
REVOKE CREATE ON SCHEMA public
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker,
         lawcase_ledger_confirmation_owner;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
    public.case_agent_ledger_exception_followups,
    public.case_agent_ledger_exception_control_assignments,
    public.case_agent_ledger_exception_control_heads,
    public.case_agent_ledger_exception_managed_evidence_requests,
    public.case_agent_ledger_exception_evidence_source_bindings,
    public.case_agent_ledger_exception_followup_events,
    public.case_agent_ledger_exception_followup_heads,
    public.case_agent_ledger_exception_reextraction_task_bindings,
    public.case_agent_ledger_exception_reextraction_task_binding_heads,
    public.case_agent_ledger_exception_reextraction_bindings,
    public.case_agent_ledger_exception_duplicate_dispositions,
    public.case_agent_ledger_exception_duplicate_heads
    FROM lawcase_web_application, lawcase_agent_worker;
GRANT SELECT ON TABLE
    public.case_agent_ledger_exception_followups,
    public.case_agent_ledger_exception_control_heads,
    public.case_agent_ledger_exception_managed_evidence_requests,
    public.case_agent_ledger_exception_evidence_source_bindings,
    public.case_agent_ledger_exception_followup_heads,
    public.case_agent_ledger_exception_reextraction_task_bindings,
    public.case_agent_ledger_exception_reextraction_task_binding_heads,
    public.case_agent_ledger_exception_reextraction_bindings,
    public.case_agent_ledger_exception_duplicate_dispositions,
    public.case_agent_ledger_exception_duplicate_heads
    TO lawcase_web_application, lawcase_agent_worker;
-- Session UUIDs are authentication factors, not runtime projection data.
-- Application code reads only the current-control join keys, while only the
-- Worker reads the immutable event columns used to verify the current head.
GRANT SELECT (
    control_assignment_id, firm_id, matter_id, assignment_sequence,
    control_run_id, state_after
) ON TABLE public.case_agent_ledger_exception_control_assignments
    TO lawcase_web_application, lawcase_agent_worker;
GRANT SELECT (
    followup_event_id, followup_id, event_sequence, firm_id, matter_id,
    subject_hash, state_after, expected_matter_version, event_hash
) ON TABLE public.case_agent_ledger_exception_followup_events
    TO lawcase_agent_worker;
GRANT SELECT ON TABLE
    public.users,
    public.matters,
    public.matter_actor_roles,
    public.case_agent_ledger_extraction_batches,
    public.case_agent_ledger_extraction_candidate_pages,
    public.case_agent_ledger_exception_groups,
    public.case_agent_ledger_exception_group_members,
    public.case_agent_ledger_exception_group_decisions,
    public.case_agent_runs,
    public.case_agent_task_heads,
    public.evidence_original_files,
    public.case_material_objects
    TO lawcase_web_application, lawcase_agent_worker;

REVOKE ALL ON FUNCTION
    public.resolve_case_agent_ledger_exception_followup_from_web_session(
        uuid, uuid, uuid, integer, text, text, uuid, jsonb, text, text
    ) FROM PUBLIC, lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.transfer_case_agent_ledger_exception_control_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ) FROM PUBLIC, lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.bind_case_agent_ledger_exception_reextraction_task_from_worker(
        uuid, uuid, uuid, uuid, uuid, uuid, uuid, integer, text, text
    ) FROM PUBLIC, lawcase_web_application;
REVOKE ALL ON FUNCTION
    public.satisfy_case_agent_ledger_reextraction_followup_from_worker(
        uuid, uuid, uuid, uuid, uuid, integer, text, text
    ) FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.satisfy_case_agent_ledger_reextraction_set_from_worker(
        uuid, uuid, uuid, uuid, uuid, integer, text, text
    ) FROM PUBLIC, lawcase_web_application;
GRANT EXECUTE ON FUNCTION
    public.resolve_case_agent_ledger_exception_followup_from_web_session(
        uuid, uuid, uuid, integer, text, text, uuid, jsonb, text, text
    ) TO lawcase_web_application;
GRANT EXECUTE ON FUNCTION
    public.transfer_case_agent_ledger_exception_control_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ) TO lawcase_web_application;
GRANT EXECUTE ON FUNCTION
    public.bind_case_agent_ledger_exception_reextraction_task_from_worker(
        uuid, uuid, uuid, uuid, uuid, uuid, uuid, integer, text, text
    ) TO lawcase_agent_worker;
GRANT EXECUTE ON FUNCTION
    public.satisfy_case_agent_ledger_reextraction_set_from_worker(
        uuid, uuid, uuid, uuid, uuid, integer, text, text
    ) TO lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.initialize_case_agent_ledger_exception_lifecycle(
        uuid, uuid, uuid
    ),
    public.initialize_case_agent_ledger_exception_lifecycle_trigger(),
    public.case_agent_ledger_exception_control_transfer_request_hash(
        uuid, integer, uuid
    ),
    public.prohibit_case_agent_ledger_exception_lifecycle_mutation(),
    public.guard_case_agent_ledger_exception_control_head(),
    public.guard_case_agent_ledger_exception_followup_head(),
    public.guard_case_agent_ledger_exception_duplicate_head(),
    public.guard_case_agent_ledger_exception_reextraction_task_binding_head(),
    public.enqueue_case_agent_snapshot_refresh_from_exception_followup(),
    public.enqueue_case_agent_exception_control_run_refresh_outbox(),
    public.enqueue_case_agent_exception_control_run_refresh(),
    public.block_active_exception_control_run_cancellation(),
    public.mark_case_agent_ledger_exception_control_recovery_required(),
    public.block_active_exception_followup_work_plan_promotion(),
    public.block_active_exception_followup_work_plan_activation()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

GRANT SELECT, INSERT ON TABLE
    public.case_agent_ledger_exception_followups,
    public.case_agent_ledger_exception_control_assignments,
    public.case_agent_ledger_exception_control_heads,
    public.case_agent_ledger_exception_managed_evidence_requests,
    public.case_agent_ledger_exception_evidence_source_bindings,
    public.case_agent_ledger_exception_followup_events,
    public.case_agent_ledger_exception_followup_heads,
    public.case_agent_ledger_exception_reextraction_task_bindings,
    public.case_agent_ledger_exception_reextraction_task_binding_heads,
    public.case_agent_ledger_exception_reextraction_bindings,
    public.case_agent_ledger_exception_duplicate_dispositions,
    public.case_agent_ledger_exception_duplicate_heads
    TO lawcase_ledger_confirmation_owner;
GRANT SELECT ON TABLE
    public.case_material_objects,
    public.case_agent_task_heads,
    public.case_agent_events,
    public.outbox_events
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE ON TABLE
    public.case_agent_ledger_exception_control_heads,
    public.case_agent_ledger_exception_followup_heads,
    public.case_agent_ledger_exception_duplicate_heads,
    public.case_agent_ledger_exception_reextraction_task_binding_heads
    TO lawcase_ledger_confirmation_owner;
-- Row-lock entitlements for mutable authority/run heads.  The isolated
-- NOLOGIN owner receives only one immutable identity column per upstream
-- relation; application roles neither inherit this role nor call its
-- internal helpers directly.
GRANT UPDATE (session_id) ON TABLE public.web_sessions
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (user_id) ON TABLE public.users
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (user_id) ON TABLE public.matter_actor_roles
    TO lawcase_ledger_confirmation_owner;
GRANT UPDATE (run_id) ON TABLE public.case_agent_runs
    TO lawcase_ledger_confirmation_owner;

COMMIT;
