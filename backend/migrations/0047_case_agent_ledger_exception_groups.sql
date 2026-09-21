-- Immutable, server-grouped terminal routing for 0042 exception candidates.
--
-- A browser can name one opaque group and one bounded route.  It cannot name
-- candidate ids/hashes, change group membership, or promote an exception into
-- facts/transactions.  Groups are derived from candidate kind, canonical
-- reason codes and fixed source/risk policies after private staging.  A batch
-- is RESOLVED only when its complete exception lane is terminal and its
-- low-risk lane is either EMPTY or backed by the exact 0042 confirmation.

BEGIN;

CREATE FUNCTION case_agent_ledger_exception_canonical_reasons(input_codes text[])
RETURNS text[] LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT COALESCE(array_agg(DISTINCT code ORDER BY code), ARRAY[]::text[])
      FROM unnest(input_codes) AS code
$$;

CREATE FUNCTION case_agent_ledger_exception_source_policy(input_codes text[])
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE
        WHEN case_agent_ledger_exception_canonical_reasons(input_codes) && ARRAY[
            'OCR_DERIVED', 'UNTRUSTED_TEXT', 'NON_NATIVE_SOURCE',
            'SOURCE_TEXT_NOT_REVERIFIED'
        ]::text[] THEN 'SOURCE_REVERIFICATION_REQUIRED'
        ELSE 'NATIVE_SOURCE_REVIEW'
    END
$$;

CREATE FUNCTION case_agent_ledger_exception_risk_policy(input_codes text[])
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE
        WHEN case_agent_ledger_exception_canonical_reasons(input_codes) && ARRAY[
            'POSSIBLE_DUPLICATE', 'CURRENT_LEDGER_CONFLICT_OR_DUPLICATE'
        ]::text[] THEN 'DUPLICATE_REVIEW'
        WHEN case_agent_ledger_exception_canonical_reasons(input_codes) && ARRAY[
            'CROSS_PAGE_CONFLICT', 'CONTRADICTS_CASE_LEDGER',
            'LEGAL_CONCLUSION_RISK', 'CURRENT_LEDGER_CONFLICT_OR_DUPLICATE'
        ]::text[] THEN 'LEGAL_OR_LEDGER_CONFLICT_REVIEW'
        WHEN case_agent_ledger_exception_canonical_reasons(input_codes) && ARRAY[
            'PARTY_AMBIGUOUS', 'DATE_AMBIGUOUS', 'AMOUNT_AMBIGUOUS',
            'INCOMPLETE_TRANSACTION'
        ]::text[] THEN 'MISSING_FIELDS_OR_AMBIGUITY_REVIEW'
        ELSE 'LOW_CONFIDENCE_REVIEW'
    END
$$;

CREATE TABLE case_agent_ledger_exception_groups (
    exception_group_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_batch_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    candidate_kind text NOT NULL CHECK (candidate_kind IN ('FACT', 'TRANSACTION')),
    canonical_reason_codes text[] NOT NULL CHECK (
        cardinality(canonical_reason_codes) BETWEEN 1 AND 15
        AND canonical_reason_codes =
            case_agent_ledger_exception_canonical_reasons(canonical_reason_codes)
    ),
    source_policy text NOT NULL CHECK (source_policy IN (
        'NATIVE_SOURCE_REVIEW', 'SOURCE_REVERIFICATION_REQUIRED'
    )),
    risk_policy text NOT NULL CHECK (risk_policy IN (
        'DUPLICATE_REVIEW', 'LEGAL_OR_LEDGER_CONFLICT_REVIEW',
        'MISSING_FIELDS_OR_AMBIGUITY_REVIEW', 'LOW_CONFIDENCE_REVIEW'
    )),
    group_key_hash char(64) NOT NULL CHECK (group_key_hash ~ '^[0-9a-f]{64}$'),
    candidate_set_hash char(64) NOT NULL CHECK (
        candidate_set_hash ~ '^[0-9a-f]{64}$'
    ),
    candidate_count integer NOT NULL CHECK (candidate_count BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exception_group_id, extraction_batch_id, firm_id, matter_id),
    UNIQUE (extraction_batch_id, group_key_hash),
    UNIQUE (
        extraction_batch_id, candidate_kind, canonical_reason_codes,
        source_policy, risk_policy
    ),
    FOREIGN KEY (extraction_batch_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_extraction_batches(
            extraction_batch_id, firm_id, matter_id
        ),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    CHECK (
        source_policy =
            case_agent_ledger_exception_source_policy(canonical_reason_codes)
        AND risk_policy =
            case_agent_ledger_exception_risk_policy(canonical_reason_codes)
    )
);

CREATE TABLE case_agent_ledger_exception_group_members (
    exception_group_id uuid NOT NULL,
    extraction_batch_id uuid NOT NULL,
    extraction_candidate_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    candidate_hash char(64) NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (exception_group_id, extraction_candidate_id),
    UNIQUE (extraction_candidate_id),
    FOREIGN KEY (
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ) REFERENCES case_agent_ledger_exception_groups(
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ),
    FOREIGN KEY (
        extraction_candidate_id, extraction_batch_id, firm_id, matter_id
    ) REFERENCES case_agent_ledger_extraction_candidates(
        extraction_candidate_id, extraction_batch_id, firm_id, matter_id
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)
);

CREATE TABLE case_agent_ledger_exception_group_decisions (
    exception_decision_id uuid PRIMARY KEY,
    exception_group_id uuid NOT NULL UNIQUE,
    extraction_batch_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    bound_group_key_hash char(64) NOT NULL CHECK (
        bound_group_key_hash ~ '^[0-9a-f]{64}$'
    ),
    bound_candidate_set_hash char(64) NOT NULL CHECK (
        bound_candidate_set_hash ~ '^[0-9a-f]{64}$'
    ),
    bound_candidate_count integer NOT NULL CHECK (
        bound_candidate_count BETWEEN 1 AND 500
    ),
    decision text NOT NULL CHECK (decision IN (
        'REJECT_AS_DUPLICATE', 'REQUEST_REEXTRACTION',
        'REQUEST_MORE_EVIDENCE', 'DEFER_WITH_REASON'
    )),
    reason_code text NOT NULL CHECK (reason_code IN (
        'DUPLICATE_CONFIRMED', 'SOURCE_QUALITY_INSUFFICIENT',
        'EXTRACTION_CONFLICT', 'EVIDENCE_GAP',
        'PARTY_DATE_AMOUNT_UNCLEAR', 'AWAITING_CLIENT_INPUT',
        'AWAITING_EXTERNAL_RECORD', 'NEEDS_LEAD_REVIEW'
    )),
    reason_note text CHECK (
        reason_note IS NULL OR (
            reason_note = btrim(reason_note)
            AND length(reason_note) BETWEEN 1 AND 500
            AND octet_length(reason_note) <= 2000
            AND reason_note !~ '[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F]'
        )
    ),
    decision_hash char(64) NOT NULL CHECK (decision_hash ~ '^[0-9a-f]{64}$'),
    expected_matter_version integer NOT NULL CHECK (expected_matter_version > 0),
    decided_by uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (
        length(trim(idempotency_key)) BETWEEN 1 AND 200
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    decided_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exception_decision_id, firm_id, matter_id),
    UNIQUE (firm_id, matter_id, decided_by, idempotency_key),
    FOREIGN KEY (
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ) REFERENCES case_agent_ledger_exception_groups(
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (decided_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (decision = 'REJECT_AS_DUPLICATE'
            AND reason_code = 'DUPLICATE_CONFIRMED')
        OR (decision = 'REQUEST_REEXTRACTION'
            AND reason_code IN (
                'SOURCE_QUALITY_INSUFFICIENT', 'EXTRACTION_CONFLICT'
            ))
        OR (decision = 'REQUEST_MORE_EVIDENCE'
            AND reason_code IN ('EVIDENCE_GAP', 'PARTY_DATE_AMOUNT_UNCLEAR'))
        OR (decision = 'DEFER_WITH_REASON'
            AND reason_code IN (
                'AWAITING_CLIENT_INPUT', 'AWAITING_EXTERNAL_RECORD',
                'NEEDS_LEAD_REVIEW'
            ) AND reason_note IS NOT NULL)
    )
);

CREATE TABLE case_agent_ledger_exception_decision_events (
    exception_decision_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    exception_decision_id uuid NOT NULL UNIQUE,
    exception_group_id uuid NOT NULL,
    extraction_batch_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    event_type text NOT NULL CHECK (
        event_type = 'CASE_LEDGER_EXTRACTION_EXCEPTION_GROUP_ROUTED'
    ),
    matter_version integer NOT NULL CHECK (matter_version > 0),
    actor_id uuid NOT NULL,
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exception_decision_event_id, firm_id, matter_id),
    FOREIGN KEY (exception_decision_id, firm_id, matter_id)
        REFERENCES case_agent_ledger_exception_group_decisions(
            exception_decision_id, firm_id, matter_id
        ),
    FOREIGN KEY (
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ) REFERENCES case_agent_ledger_exception_groups(
        exception_group_id, extraction_batch_id, firm_id, matter_id
    ),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)
);

CREATE FUNCTION materialize_case_agent_ledger_exception_groups(
    input_batch_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE batch_run_id uuid;
DECLARE existing_group_count integer;
BEGIN
    SELECT run_id INTO batch_run_id
     FROM case_agent_ledger_extraction_batches
     WHERE extraction_batch_id = input_batch_id
       AND firm_id = input_firm_id AND matter_id = input_matter_id;
    IF batch_run_id IS NULL THEN
        RAISE EXCEPTION 'exception grouping batch is missing';
    END IF;
    SELECT count(*)::integer INTO existing_group_count
      FROM case_agent_ledger_exception_groups
     WHERE extraction_batch_id = input_batch_id
       AND firm_id = input_firm_id AND matter_id = input_matter_id;
    IF existing_group_count > 0 THEN
        RETURN;
    END IF;

    INSERT INTO case_agent_ledger_exception_groups (
        extraction_batch_id, run_id, firm_id, matter_id, candidate_kind,
        canonical_reason_codes, source_policy, risk_policy, group_key_hash,
        candidate_set_hash, candidate_count
    )
    SELECT input_batch_id, batch_run_id, input_firm_id, input_matter_id,
           grouped.candidate_kind, grouped.reason_codes,
           case_agent_ledger_exception_source_policy(grouped.reason_codes),
           case_agent_ledger_exception_risk_policy(grouped.reason_codes),
           encode(digest(convert_to(
               grouped.candidate_kind || E'\n'
               || array_to_string(grouped.reason_codes, ',') || E'\n'
               || case_agent_ledger_exception_source_policy(
                    grouped.reason_codes
                  ) || E'\n'
               || case_agent_ledger_exception_risk_policy(
                    grouped.reason_codes
                  ),
               'UTF8'
           ), 'sha256'), 'hex'),
           grouped.candidate_set_hash, grouped.candidate_count
      FROM (
          SELECT candidate_kind,
                 case_agent_ledger_exception_canonical_reasons(
                     review_reason_codes
                 ) AS reason_codes,
                 encode(digest(convert_to(
                     string_agg(candidate_hash, ',' ORDER BY candidate_hash),
                     'UTF8'
                 ), 'sha256'), 'hex') AS candidate_set_hash,
                 count(*)::integer AS candidate_count
            FROM case_agent_ledger_extraction_candidates
           WHERE extraction_batch_id = input_batch_id
             AND firm_id = input_firm_id AND matter_id = input_matter_id
             AND review_lane = 'EXCEPTION_REVIEW'
             AND eligible_for_bulk_promotion = false
           GROUP BY candidate_kind,
                    case_agent_ledger_exception_canonical_reasons(
                        review_reason_codes
                    )
      ) grouped;

    INSERT INTO case_agent_ledger_exception_group_members (
        exception_group_id, extraction_batch_id, extraction_candidate_id,
        firm_id, matter_id, candidate_hash
    )
    SELECT exception_group.exception_group_id, input_batch_id,
           candidate.extraction_candidate_id, input_firm_id, input_matter_id,
           candidate.candidate_hash
      FROM case_agent_ledger_extraction_candidates candidate
      JOIN case_agent_ledger_exception_groups exception_group
        ON exception_group.extraction_batch_id = candidate.extraction_batch_id
       AND exception_group.firm_id = candidate.firm_id
       AND exception_group.matter_id = candidate.matter_id
       AND exception_group.candidate_kind = candidate.candidate_kind
       AND exception_group.canonical_reason_codes =
            case_agent_ledger_exception_canonical_reasons(
                candidate.review_reason_codes
            )
       AND exception_group.source_policy =
            case_agent_ledger_exception_source_policy(
                candidate.review_reason_codes
            )
       AND exception_group.risk_policy =
            case_agent_ledger_exception_risk_policy(
                candidate.review_reason_codes
            )
     WHERE candidate.extraction_batch_id = input_batch_id
       AND candidate.firm_id = input_firm_id
       AND candidate.matter_id = input_matter_id
       AND candidate.review_lane = 'EXCEPTION_REVIEW'
       AND candidate.eligible_for_bulk_promotion = false;
END;
$$;

CREATE FUNCTION validate_case_agent_ledger_exception_group_integrity(
    input_batch_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE batch_total integer;
DECLARE batch_eligible integer;
DECLARE stored_total integer;
DECLARE stored_eligible integer;
DECLARE exception_total integer;
DECLARE member_total integer;
DECLARE invalid_group_count integer;
BEGIN
    SELECT candidate_count, eligible_candidate_count
      INTO batch_total, batch_eligible
      FROM case_agent_ledger_extraction_batches
     WHERE extraction_batch_id = input_batch_id
       AND firm_id = input_firm_id AND matter_id = input_matter_id;
    IF batch_total IS NULL THEN RETURN false; END IF;
    SELECT count(*)::integer,
           count(*) FILTER (WHERE eligible_for_bulk_promotion)::integer,
           count(*) FILTER (
               WHERE review_lane = 'EXCEPTION_REVIEW'
                 AND eligible_for_bulk_promotion = false
           )::integer
      INTO stored_total, stored_eligible, exception_total
      FROM case_agent_ledger_extraction_candidates
     WHERE extraction_batch_id = input_batch_id
       AND firm_id = input_firm_id AND matter_id = input_matter_id;
    SELECT count(*)::integer INTO member_total
      FROM case_agent_ledger_exception_group_members
     WHERE extraction_batch_id = input_batch_id
       AND firm_id = input_firm_id AND matter_id = input_matter_id;
    SELECT count(*)::integer INTO invalid_group_count
      FROM case_agent_ledger_exception_groups exception_group
     WHERE exception_group.extraction_batch_id = input_batch_id
       AND exception_group.firm_id = input_firm_id
       AND exception_group.matter_id = input_matter_id
       AND (
          exception_group.candidate_count IS DISTINCT FROM (
              SELECT count(*)::integer
                FROM case_agent_ledger_exception_group_members member
               WHERE member.exception_group_id = exception_group.exception_group_id
                 AND member.extraction_batch_id = exception_group.extraction_batch_id
                 AND member.firm_id = exception_group.firm_id
                 AND member.matter_id = exception_group.matter_id
          )
          OR exception_group.candidate_set_hash IS DISTINCT FROM (
              SELECT encode(digest(convert_to(
                         string_agg(member.candidate_hash, ','
                                    ORDER BY member.candidate_hash),
                         'UTF8'
                     ), 'sha256'), 'hex')
                FROM case_agent_ledger_exception_group_members member
               WHERE member.exception_group_id = exception_group.exception_group_id
                 AND member.extraction_batch_id = exception_group.extraction_batch_id
                 AND member.firm_id = exception_group.firm_id
                 AND member.matter_id = exception_group.matter_id
          )
          OR EXISTS (
              SELECT 1
                FROM case_agent_ledger_exception_group_members member
                JOIN case_agent_ledger_extraction_candidates candidate
                  ON candidate.extraction_candidate_id =
                        member.extraction_candidate_id
                 AND candidate.extraction_batch_id = member.extraction_batch_id
                 AND candidate.firm_id = member.firm_id
                 AND candidate.matter_id = member.matter_id
               WHERE member.exception_group_id = exception_group.exception_group_id
                 AND (
                     candidate.review_lane <> 'EXCEPTION_REVIEW'
                     OR candidate.eligible_for_bulk_promotion <> false
                     OR candidate.candidate_hash <> member.candidate_hash
                     OR candidate.candidate_kind <> exception_group.candidate_kind
                     OR case_agent_ledger_exception_canonical_reasons(
                            candidate.review_reason_codes
                        ) <> exception_group.canonical_reason_codes
                 )
          )
       );
    RETURN batch_total = stored_total
       AND batch_eligible = stored_eligible
       AND exception_total = member_total
       AND invalid_group_count = 0
       AND NOT EXISTS (
           SELECT 1
             FROM case_agent_ledger_extraction_candidates candidate
            WHERE candidate.extraction_batch_id = input_batch_id
              AND candidate.firm_id = input_firm_id
              AND candidate.matter_id = input_matter_id
              AND candidate.review_lane = 'EXCEPTION_REVIEW'
              AND NOT EXISTS (
                  SELECT 1
                    FROM case_agent_ledger_exception_group_members member
                   WHERE member.extraction_candidate_id =
                            candidate.extraction_candidate_id
                     AND member.extraction_batch_id = candidate.extraction_batch_id
                     AND member.firm_id = candidate.firm_id
                     AND member.matter_id = candidate.matter_id
              )
       );
END;
$$;

CREATE FUNCTION enforce_case_agent_ledger_exception_group_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE batch_id uuid;
DECLARE firm uuid;
DECLARE matter uuid;
BEGIN
    batch_id := COALESCE(NEW.extraction_batch_id, OLD.extraction_batch_id);
    firm := COALESCE(NEW.firm_id, OLD.firm_id);
    matter := COALESCE(NEW.matter_id, OLD.matter_id);
    IF NOT validate_case_agent_ledger_exception_group_integrity(
        batch_id, firm, matter
    ) THEN
        RAISE EXCEPTION 'exception groups do not bind the complete immutable lane';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION validate_case_agent_ledger_exception_decision()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE exception_group case_agent_ledger_exception_groups%ROWTYPE;
DECLARE current_version integer;
DECLARE actor_is_lead boolean;
BEGIN
    SELECT * INTO exception_group
     FROM case_agent_ledger_exception_groups
     WHERE exception_group_id = NEW.exception_group_id
       AND extraction_batch_id = NEW.extraction_batch_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF NOT FOUND
       OR exception_group.run_id <> NEW.run_id
       OR exception_group.group_key_hash <> NEW.bound_group_key_hash
       OR exception_group.candidate_set_hash <> NEW.bound_candidate_set_hash
       OR exception_group.candidate_count <> NEW.bound_candidate_count
       OR NOT validate_case_agent_ledger_exception_group_integrity(
            NEW.extraction_batch_id, NEW.firm_id, NEW.matter_id
       ) THEN
        RAISE EXCEPTION 'exception decision differs from its immutable group';
    END IF;
    SELECT matter.version,
           EXISTS (
               SELECT 1
                 FROM matter_actor_roles role_binding
                 JOIN users actor
                   ON actor.user_id = role_binding.user_id
                  AND actor.firm_id = role_binding.firm_id
                WHERE role_binding.matter_id = matter.matter_id
                  AND role_binding.firm_id = matter.firm_id
                  AND role_binding.user_id = NEW.decided_by
                  AND role_binding.role = 'LEAD_LAWYER'
                  AND role_binding.revoked_at IS NULL
                  AND actor.status = 'ACTIVE'
           )
      INTO current_version, actor_is_lead
      FROM matters matter
     WHERE matter.matter_id = NEW.matter_id AND matter.firm_id = NEW.firm_id
     FOR UPDATE;
    IF current_version IS DISTINCT FROM NEW.expected_matter_version THEN
        RAISE EXCEPTION 'exception decision expected matter version is stale';
    END IF;
    IF actor_is_lead IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'exception decision requires an active lead lawyer';
    END IF;
    IF NEW.decision = 'REJECT_AS_DUPLICATE'
       AND exception_group.risk_policy <> 'DUPLICATE_REVIEW' THEN
        RAISE EXCEPTION 'only a duplicate-risk group can be rejected as duplicate';
    ELSIF NEW.decision = 'REQUEST_REEXTRACTION'
       AND exception_group.source_policy <> 'SOURCE_REVERIFICATION_REQUIRED'
       AND exception_group.risk_policy NOT IN (
           'LEGAL_OR_LEDGER_CONFLICT_REVIEW', 'LOW_CONFIDENCE_REVIEW'
       ) THEN
        RAISE EXCEPTION 'exception group does not support re-extraction';
    ELSIF NEW.decision = 'REQUEST_MORE_EVIDENCE'
       AND exception_group.risk_policy NOT IN (
           'MISSING_FIELDS_OR_AMBIGUITY_REVIEW',
           'LEGAL_OR_LEDGER_CONFLICT_REVIEW'
       ) THEN
        RAISE EXCEPTION 'exception group does not support an evidence request';
    END IF;
    NEW.decision_hash := encode(digest(convert_to(jsonb_build_object(
        'candidate_count', NEW.bound_candidate_count,
        'candidate_set_hash', NEW.bound_candidate_set_hash,
        'decision', NEW.decision,
        'group_id', NEW.exception_group_id,
        'group_key_hash', NEW.bound_group_key_hash,
        'lawyer_id', NEW.decided_by,
        'reason_code', NEW.reason_code,
        'reason_note', NEW.reason_note,
        'schema_version', 'case-ledger-exception-group-decision-v1'
    )::text, 'UTF8'), 'sha256'), 'hex');
    RETURN NEW;
END;
$$;

CREATE FUNCTION audit_case_agent_ledger_exception_decision()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    INSERT INTO case_agent_ledger_exception_decision_events (
        exception_decision_id, exception_group_id, extraction_batch_id,
        run_id, firm_id, matter_id, event_type, matter_version, actor_id,
        request_hash, payload
    ) VALUES (
        NEW.exception_decision_id, NEW.exception_group_id,
        NEW.extraction_batch_id, NEW.run_id, NEW.firm_id, NEW.matter_id,
        'CASE_LEDGER_EXTRACTION_EXCEPTION_GROUP_ROUTED',
        NEW.expected_matter_version, NEW.decided_by, NEW.request_hash,
        jsonb_build_object(
            'exception_group_id', NEW.exception_group_id,
            'extraction_batch_id', NEW.extraction_batch_id,
            'run_id', NEW.run_id,
            'decision', NEW.decision,
            'reason_code', NEW.reason_code,
            'decision_hash', NEW.decision_hash,
            'candidate_count', NEW.bound_candidate_count,
            'formal_ledger_write', false,
            'legal_conclusion', false
        )
    );
    RETURN NEW;
END;
$$;

CREATE FUNCTION case_agent_ledger_extraction_batch_review_status(
    input_batch_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS TABLE (
    low_risk_lane_status text,
    batch_status text,
    exception_group_count integer,
    decided_exception_group_count integer
) LANGUAGE sql STABLE AS $$
    WITH review AS (
        SELECT batch.eligible_candidate_count,
               batch.candidate_count - batch.eligible_candidate_count
                    AS exception_candidate_count,
               count(DISTINCT exception_group.exception_group_id)::integer
                    AS group_count,
               count(DISTINCT decision.exception_decision_id)::integer
                    AS decision_count,
               count(DISTINCT member.extraction_candidate_id)::integer
                    AS member_count,
               confirmation.confirmed_candidate_count
          FROM case_agent_ledger_extraction_batches batch
          LEFT JOIN case_agent_ledger_extraction_batch_confirmations confirmation
            ON confirmation.extraction_batch_id = batch.extraction_batch_id
           AND confirmation.firm_id = batch.firm_id
           AND confirmation.matter_id = batch.matter_id
          LEFT JOIN case_agent_ledger_exception_groups exception_group
            ON exception_group.extraction_batch_id = batch.extraction_batch_id
           AND exception_group.firm_id = batch.firm_id
           AND exception_group.matter_id = batch.matter_id
          LEFT JOIN case_agent_ledger_exception_group_members member
            ON member.exception_group_id = exception_group.exception_group_id
           AND member.extraction_batch_id = exception_group.extraction_batch_id
           AND member.firm_id = exception_group.firm_id
           AND member.matter_id = exception_group.matter_id
          LEFT JOIN case_agent_ledger_exception_group_decisions decision
            ON decision.exception_group_id = exception_group.exception_group_id
           AND decision.extraction_batch_id = exception_group.extraction_batch_id
           AND decision.firm_id = exception_group.firm_id
           AND decision.matter_id = exception_group.matter_id
         WHERE batch.extraction_batch_id = input_batch_id
           AND batch.firm_id = input_firm_id
           AND batch.matter_id = input_matter_id
         GROUP BY batch.eligible_candidate_count, batch.candidate_count,
                  confirmation.confirmed_candidate_count
    ), state AS (
        SELECT *,
               CASE
                   WHEN eligible_candidate_count = 0 THEN 'EMPTY'
                   WHEN confirmed_candidate_count = eligible_candidate_count
                        THEN 'CONFIRMED'
                   ELSE 'OPEN'
               END AS low_status,
               (
                   exception_candidate_count = 0
                   OR (
                       group_count > 0
                       AND group_count = decision_count
                       AND member_count = exception_candidate_count
                   )
               ) AS exceptions_terminal
          FROM review
    )
    SELECT low_status,
           CASE
               WHEN low_status IN ('EMPTY', 'CONFIRMED')
                    AND exceptions_terminal THEN 'RESOLVED'
               WHEN low_status = 'CONFIRMED' AND NOT exceptions_terminal
                    THEN 'LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN'
               WHEN exception_candidate_count > 0 THEN 'EXCEPTIONS_OPEN'
               ELSE 'LOW_RISK_OPEN'
           END,
           group_count,
           decision_count
      FROM state
$$;

-- Prove that 0042 has staged every extraction artifact in the current PASSED
-- verification receipt before any lawyer command can advance the matter.  A
-- partial multi-artifact stage is false; a verified run with no extraction
-- artifact is vacuously complete and must not block ordinary plan promotion.
CREATE FUNCTION case_agent_ledger_extraction_run_staging_complete(
    input_run_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    WITH current_receipt AS (
        SELECT receipt.verification_receipt_id, receipt.artifact_lineage
          FROM case_agent_runs run
          JOIN case_agent_task_graphs graph
            ON graph.graph_id = run.current_graph_id
           AND graph.run_id = run.run_id
           AND graph.firm_id = run.firm_id
           AND graph.matter_id = run.matter_id
          JOIN case_agent_verification_attempts attempt
            ON attempt.run_id = run.run_id
           AND attempt.graph_id = run.current_graph_id
           AND attempt.firm_id = run.firm_id
           AND attempt.matter_id = run.matter_id
          JOIN case_agent_verification_receipts receipt
            ON receipt.verification_attempt_id = attempt.verification_attempt_id
           AND receipt.run_id = run.run_id
           AND receipt.firm_id = run.firm_id
           AND receipt.matter_id = run.matter_id
           AND receipt.outcome = 'PASSED'
           AND receipt.verification_hash = run.verification_hash
           AND receipt.snapshot_hash = run.snapshot_hash
           AND receipt.graph_hash = graph.graph_hash
         WHERE run.run_id = input_run_id
           AND run.firm_id = input_firm_id
           AND run.matter_id = input_matter_id
           AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
           AND NOT run.is_stale AND NOT run.is_cancelled
    ), expected AS (
        SELECT receipt.verification_receipt_id,
               lineage.item->>'artifact_id' AS artifact_id
          FROM current_receipt receipt
          CROSS JOIN LATERAL jsonb_array_elements(
              receipt.artifact_lineage
          ) lineage(item)
         WHERE lineage.item->>'artifact_kind' =
                'CASE_LEDGER_EXTRACTION_CANDIDATE'
    ), staged AS (
        SELECT batch.verification_receipt_id,
               batch.artifact_id::text AS artifact_id
          FROM case_agent_ledger_extraction_batches batch
          JOIN current_receipt receipt
            ON receipt.verification_receipt_id =
                    batch.verification_receipt_id
         WHERE batch.run_id = input_run_id
           AND batch.firm_id = input_firm_id
           AND batch.matter_id = input_matter_id
    )
    SELECT CASE
        WHEN (SELECT count(*) FROM current_receipt) <> 1 THEN false
        WHEN EXISTS (
            SELECT 1 FROM expected
             WHERE artifact_id IS NULL OR artifact_id = ''
        ) THEN false
        WHEN (SELECT count(*) FROM expected) <>
             (SELECT count(DISTINCT artifact_id) FROM expected) THEN false
        ELSE
            (SELECT count(*) FROM staged) = (SELECT count(*) FROM expected)
            AND NOT EXISTS (
                SELECT 1 FROM expected
                 WHERE NOT EXISTS (
                     SELECT 1 FROM staged
                      WHERE staged.artifact_id = expected.artifact_id
                        AND staged.verification_receipt_id =
                            expected.verification_receipt_id
                 )
            )
            AND NOT EXISTS (
                SELECT 1 FROM staged
                 WHERE NOT EXISTS (
                     SELECT 1 FROM expected
                      WHERE expected.artifact_id = staged.artifact_id
                        AND expected.verification_receipt_id =
                            staged.verification_receipt_id
                 )
            )
    END
$$;

-- A later artifact from the same verified run may be reviewed after an
-- earlier low-risk batch has advanced the matter.  This helper accepts that
-- current version only when every intervening authoritative V -> V+1 event is
-- an exact 0042 confirmation from the same run.  Any unrelated ledger event,
-- missing receipt or graph change returns NULL rather than weakening normal
-- optimistic concurrency.
CREATE FUNCTION case_agent_ledger_extraction_current_review_version(
    input_batch_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS integer LANGUAGE plpgsql STABLE AS $$
DECLARE source_version integer;
DECLARE current_version integer;
DECLARE source_run_id uuid;
DECLARE source_graph_id uuid;
DECLARE current_graph_id uuid;
DECLARE source_run_status text;
DECLARE source_run_stale boolean;
DECLARE source_run_cancelled boolean;
DECLARE transition_count integer;
DECLARE valid_transition_count integer;
DECLARE distinct_input_count integer;
DECLARE distinct_output_count integer;
DECLARE first_input integer;
DECLARE last_output integer;
BEGIN
    SELECT batch.source_matter_version, batch.run_id, batch.graph_id,
           matter.version, run.current_graph_id, run.status,
           run.is_stale, run.is_cancelled
      INTO source_version, source_run_id, source_graph_id, current_version,
           current_graph_id, source_run_status, source_run_stale,
           source_run_cancelled
      FROM case_agent_ledger_extraction_batches batch
      JOIN matters matter
        ON matter.matter_id = batch.matter_id AND matter.firm_id = batch.firm_id
      JOIN case_agent_runs run
        ON run.run_id = batch.run_id AND run.firm_id = batch.firm_id
       AND run.matter_id = batch.matter_id
      JOIN case_agent_task_graphs graph
        ON graph.graph_id = run.current_graph_id AND graph.run_id = run.run_id
       AND graph.firm_id = run.firm_id AND graph.matter_id = run.matter_id
      JOIN case_agent_verification_receipts receipt
        ON receipt.verification_receipt_id = batch.verification_receipt_id
       AND receipt.run_id = run.run_id AND receipt.firm_id = run.firm_id
       AND receipt.matter_id = run.matter_id AND receipt.outcome = 'PASSED'
       AND receipt.verification_hash = run.verification_hash
       AND receipt.snapshot_hash = run.snapshot_hash
       AND receipt.graph_hash = graph.graph_hash
     WHERE batch.extraction_batch_id = input_batch_id
       AND batch.firm_id = input_firm_id
       AND batch.matter_id = input_matter_id;
    IF source_version IS NULL OR current_version < source_version
       OR current_graph_id <> source_graph_id
       OR source_run_status NOT IN ('READY_FOR_REVIEW', 'COMPLETED')
       OR source_run_stale OR source_run_cancelled
       OR NOT case_agent_ledger_extraction_run_staging_complete(
            source_run_id, input_firm_id, input_matter_id
       ) THEN
        RETURN NULL;
    END IF;
    IF current_version = source_version THEN
        RETURN current_version;
    END IF;
    SELECT count(*)::integer,
           count(*) FILTER (
               WHERE audit.event_type =
                        'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED'
                 AND audit.output_version = audit.input_version + 1
                 AND confirmed_batch.run_id = source_run_id
                 AND confirmed_batch.graph_id = source_graph_id
                 AND confirmation.confirmed_matter_version =
                        audit.output_version
                 AND confirmation.confirmed_candidate_count =
                        confirmed_batch.eligible_candidate_count
                 AND audit.payload->>'extraction_batch_id' =
                        confirmed_batch.extraction_batch_id::text
           )::integer,
           count(DISTINCT audit.input_version)::integer,
           count(DISTINCT audit.output_version)::integer,
           min(audit.input_version), max(audit.output_version)
      INTO transition_count, valid_transition_count, distinct_input_count,
           distinct_output_count, first_input, last_output
      FROM audit_events audit
      LEFT JOIN case_agent_ledger_extraction_batches confirmed_batch
        ON confirmed_batch.extraction_batch_id::text =
                audit.payload->>'extraction_batch_id'
       AND confirmed_batch.firm_id = audit.firm_id
       AND confirmed_batch.matter_id = audit.matter_id
      LEFT JOIN case_agent_ledger_extraction_batch_confirmations confirmation
        ON confirmation.extraction_batch_id =
                confirmed_batch.extraction_batch_id
       AND confirmation.firm_id = confirmed_batch.firm_id
       AND confirmation.matter_id = confirmed_batch.matter_id
     WHERE audit.firm_id = input_firm_id
       AND audit.matter_id = input_matter_id
       AND audit.output_version = audit.input_version + 1
       AND audit.input_version >= source_version
       AND audit.output_version <= current_version;
    IF transition_count <> current_version - source_version
       OR valid_transition_count <> transition_count
       OR distinct_input_count <> transition_count
       OR distinct_output_count <> transition_count
       OR first_input <> source_version
       OR last_output <> current_version THEN
        RETURN NULL;
    END IF;
    RETURN current_version;
END;
$$;

CREATE FUNCTION case_agent_ledger_extraction_run_review_resolved(
    input_run_id uuid,
    input_firm_id uuid,
    input_matter_id uuid
)
RETURNS boolean LANGUAGE sql STABLE AS $$
    WITH current_receipt AS (
        SELECT receipt.verification_receipt_id
          FROM case_agent_runs run
          JOIN case_agent_task_graphs graph
            ON graph.graph_id = run.current_graph_id
           AND graph.run_id = run.run_id
           AND graph.firm_id = run.firm_id
           AND graph.matter_id = run.matter_id
          JOIN case_agent_verification_attempts attempt
            ON attempt.run_id = run.run_id
           AND attempt.graph_id = run.current_graph_id
           AND attempt.firm_id = run.firm_id
           AND attempt.matter_id = run.matter_id
          JOIN case_agent_verification_receipts receipt
            ON receipt.verification_attempt_id = attempt.verification_attempt_id
           AND receipt.run_id = run.run_id
           AND receipt.firm_id = run.firm_id
           AND receipt.matter_id = run.matter_id
           AND receipt.outcome = 'PASSED'
           AND receipt.verification_hash = run.verification_hash
           AND receipt.snapshot_hash = run.snapshot_hash
           AND receipt.graph_hash = graph.graph_hash
         WHERE run.run_id = input_run_id
           AND run.firm_id = input_firm_id
           AND run.matter_id = input_matter_id
    ), review AS (
        SELECT batch.extraction_batch_id,
               status.batch_status,
               validate_case_agent_ledger_exception_group_integrity(
                   batch.extraction_batch_id, batch.firm_id, batch.matter_id
               ) AS group_integrity
          FROM case_agent_ledger_extraction_batches batch
          JOIN current_receipt receipt
            ON receipt.verification_receipt_id =
                    batch.verification_receipt_id
          CROSS JOIN LATERAL case_agent_ledger_extraction_batch_review_status(
              batch.extraction_batch_id, batch.firm_id, batch.matter_id
          ) status
         WHERE batch.run_id = input_run_id
           AND batch.firm_id = input_firm_id
           AND batch.matter_id = input_matter_id
    )
    SELECT CASE
        WHEN NOT case_agent_ledger_extraction_run_staging_complete(
            input_run_id, input_firm_id, input_matter_id
        ) THEN false
        WHEN NOT EXISTS (SELECT 1 FROM review) THEN true
        ELSE
            NOT EXISTS (
                SELECT 1 FROM review
                 WHERE review.batch_status <> 'RESOLVED'
                    OR review.group_integrity IS DISTINCT FROM true
            )
    END
$$;

CREATE FUNCTION group_case_agent_ledger_exceptions_after_staging()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM materialize_case_agent_ledger_exception_groups(
        NEW.extraction_batch_id, NEW.firm_id, NEW.matter_id
    );
    RETURN NEW;
END;
$$;

-- Backfill existing immutable 0042 batches before automatic grouping begins.
SELECT materialize_case_agent_ledger_exception_groups(
    batch.extraction_batch_id, batch.firm_id, batch.matter_id
)
  FROM case_agent_ledger_extraction_batches batch
 WHERE EXISTS (
     SELECT 1 FROM case_agent_ledger_extraction_candidates candidate
      WHERE candidate.extraction_batch_id = batch.extraction_batch_id
        AND candidate.firm_id = batch.firm_id
        AND candidate.matter_id = batch.matter_id
        AND candidate.review_lane = 'EXCEPTION_REVIEW'
 );

CREATE TRIGGER case_agent_ledger_exception_groups_after_staging
    AFTER INSERT ON case_agent_ledger_extraction_staging_events
    FOR EACH ROW EXECUTE FUNCTION group_case_agent_ledger_exceptions_after_staging();

CREATE TRIGGER case_agent_ledger_exception_decision_validate
    BEFORE INSERT ON case_agent_ledger_exception_group_decisions
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_ledger_exception_decision();

CREATE TRIGGER case_agent_ledger_exception_decision_audit
    AFTER INSERT ON case_agent_ledger_exception_group_decisions
    FOR EACH ROW EXECUTE FUNCTION audit_case_agent_ledger_exception_decision();

CREATE CONSTRAINT TRIGGER case_agent_ledger_exception_groups_complete
    AFTER INSERT ON case_agent_ledger_exception_groups
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_case_agent_ledger_exception_group_integrity();
CREATE CONSTRAINT TRIGGER case_agent_ledger_exception_members_complete
    AFTER INSERT ON case_agent_ledger_exception_group_members
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_case_agent_ledger_exception_group_integrity();
CREATE CONSTRAINT TRIGGER case_agent_ledger_exception_candidates_complete
    AFTER INSERT ON case_agent_ledger_extraction_candidates
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_case_agent_ledger_exception_group_integrity();

-- Replace 0046's provisional promotion-null heuristic.  Exception candidates
-- intentionally never receive a fake promotion; executable readiness is
-- derived only from the same run-wide review proof used by the consumer and
-- the 0043/0044 database blockers.
CREATE OR REPLACE FUNCTION enqueue_case_agent_snapshot_refresh_from_ledger_confirmation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE source_run_id uuid;
DECLARE source_batch_id uuid;
DECLARE source_version integer;
DECLARE has_open_exceptions boolean;
DECLARE new_refresh_request_id uuid;
DECLARE initial_status text;
BEGIN
    IF NEW.event_type <> 'CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED' THEN
        RETURN NEW;
    END IF;
    IF NEW.matter_id IS NULL OR NEW.aggregate_version IS NULL
       OR jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
       OR NEW.payload->>'audit_event_id' IS NULL
       OR NEW.payload->>'object_id' IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation outbox binding is incomplete';
    END IF;
    SELECT batch.run_id, batch.extraction_batch_id, audit.input_version
      INTO source_run_id, source_batch_id, source_version
      FROM audit_events audit
      JOIN case_agent_ledger_extraction_batches batch
        ON batch.extraction_batch_id::text = NEW.payload->>'object_id'
       AND batch.firm_id = audit.firm_id
       AND batch.matter_id = audit.matter_id
      JOIN case_agent_ledger_extraction_batch_confirmations confirmation
        ON confirmation.extraction_batch_id = batch.extraction_batch_id
       AND confirmation.firm_id = batch.firm_id
       AND confirmation.matter_id = batch.matter_id
     WHERE audit.event_id = (NEW.payload->>'audit_event_id')::uuid
       AND audit.firm_id = NEW.firm_id AND audit.matter_id = NEW.matter_id
       AND audit.event_type = NEW.event_type
       AND audit.output_version = NEW.aggregate_version
       AND audit.output_version = audit.input_version + 1
       AND audit.payload->>'extraction_batch_id' =
            batch.extraction_batch_id::text
       AND confirmation.confirmed_matter_version = NEW.aggregate_version
       AND confirmation.confirmed_candidate_count =
            batch.eligible_candidate_count;
    IF source_run_id IS NULL OR source_batch_id IS NULL
       OR source_version IS NULL THEN
        RAISE EXCEPTION 'ledger confirmation outbox differs from its batch and audit';
    END IF;
    SELECT COALESCE(bool_or(
               status.batch_status IN (
                   'EXCEPTIONS_OPEN',
                   'LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN'
               )
           ), false)
      INTO has_open_exceptions
      FROM case_agent_ledger_extraction_batches batch
      CROSS JOIN LATERAL case_agent_ledger_extraction_batch_review_status(
          batch.extraction_batch_id, batch.firm_id, batch.matter_id
      ) status
     WHERE batch.run_id = source_run_id AND batch.firm_id = NEW.firm_id
       AND batch.matter_id = NEW.matter_id;
    initial_status := CASE
        WHEN case_agent_ledger_extraction_run_review_resolved(
            source_run_id, NEW.firm_id, NEW.matter_id
        ) THEN 'PENDING'
        WHEN has_open_exceptions THEN 'BLOCKED_BY_OPEN_EXCEPTIONS'
        ELSE 'BLOCKED_BY_OPEN_REVIEW'
    END;
    INSERT INTO case_agent_snapshot_refresh_requests (
        source_outbox_id, source_audit_event_id, extraction_batch_id, run_id,
        firm_id, matter_id, source_matter_version, target_matter_version,
        request_status
    ) VALUES (
        NEW.outbox_id, (NEW.payload->>'audit_event_id')::uuid,
        source_batch_id, source_run_id, NEW.firm_id, NEW.matter_id,
        source_version, NEW.aggregate_version, initial_status
    ) RETURNING refresh_request_id INTO new_refresh_request_id;
    UPDATE case_agent_snapshot_refresh_requests prior
       SET request_status = 'SUPERSEDED', updated_at = now()
     WHERE prior.run_id = source_run_id AND prior.firm_id = NEW.firm_id
       AND prior.matter_id = NEW.matter_id
       AND prior.refresh_request_id <> new_refresh_request_id
       AND prior.target_matter_version <= NEW.aggregate_version
       AND prior.request_status IN (
           'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
           'BLOCKED_BY_OPEN_EXCEPTIONS'
       );
    RETURN NEW;
END;
$$;

-- 0046 refreshes are run-scoped: no request may become executable from a
-- single resolved artifact while sibling batches from that run remain open.
CREATE OR REPLACE FUNCTION guard_case_agent_snapshot_refresh_request()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.refresh_request_id <> OLD.refresh_request_id
       OR NEW.source_outbox_id <> OLD.source_outbox_id
       OR NEW.source_audit_event_id <> OLD.source_audit_event_id
       OR NEW.extraction_batch_id <> OLD.extraction_batch_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.source_matter_version <> OLD.source_matter_version
       OR NEW.target_matter_version <> OLD.target_matter_version
       OR NEW.updated_at <= OLD.updated_at THEN
        RAISE EXCEPTION 'case Agent snapshot refresh transition is invalid';
    END IF;
    IF OLD.request_status = 'PENDING' AND NEW.request_status = 'APPLIED' THEN
        RETURN NEW;
    END IF;
    IF OLD.request_status IN (
           'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
           'BLOCKED_BY_OPEN_EXCEPTIONS'
       ) AND NEW.request_status = 'SUPERSEDED' AND EXISTS (
        SELECT 1 FROM case_agent_snapshot_refresh_requests newer
         WHERE newer.run_id = OLD.run_id AND newer.firm_id = OLD.firm_id
           AND newer.matter_id = OLD.matter_id
           AND newer.target_matter_version > OLD.target_matter_version
    ) THEN
        RETURN NEW;
    END IF;
    IF OLD.request_status IN (
           'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
       ) AND NEW.request_status = 'PENDING'
       AND case_agent_ledger_extraction_run_review_resolved(
            OLD.run_id, OLD.firm_id, OLD.matter_id
       )
       AND NOT EXISTS (
           SELECT 1 FROM case_agent_snapshot_refresh_requests newer
            WHERE newer.run_id = OLD.run_id AND newer.firm_id = OLD.firm_id
              AND newer.matter_id = OLD.matter_id
              AND newer.target_matter_version > OLD.target_matter_version
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'case Agent snapshot refresh transition is invalid';
END;
$$;

CREATE FUNCTION unblock_case_agent_snapshot_refresh_after_exception_decision()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NOT case_agent_ledger_extraction_run_review_resolved(
        NEW.run_id, NEW.firm_id, NEW.matter_id
    ) THEN
        RETURN NEW;
    END IF;
    UPDATE case_agent_snapshot_refresh_requests request
       SET request_status = 'SUPERSEDED', updated_at = now()
     WHERE request.run_id = NEW.run_id AND request.firm_id = NEW.firm_id
       AND request.matter_id = NEW.matter_id
       AND request.request_status IN (
           'PENDING', 'BLOCKED_BY_OPEN_REVIEW',
           'BLOCKED_BY_OPEN_EXCEPTIONS'
       )
       AND request.target_matter_version < (
           SELECT max(latest.target_matter_version)
             FROM case_agent_snapshot_refresh_requests latest
            WHERE latest.run_id = NEW.run_id
              AND latest.firm_id = NEW.firm_id
              AND latest.matter_id = NEW.matter_id
       );
    UPDATE case_agent_snapshot_refresh_requests request
       SET request_status = 'PENDING', updated_at = now()
     WHERE request.run_id = NEW.run_id AND request.firm_id = NEW.firm_id
       AND request.matter_id = NEW.matter_id
       AND request.request_status IN (
           'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
       )
       AND request.target_matter_version = (
           SELECT max(latest.target_matter_version)
             FROM case_agent_snapshot_refresh_requests latest
            WHERE latest.run_id = NEW.run_id
              AND latest.firm_id = NEW.firm_id
              AND latest.matter_id = NEW.matter_id
       );
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_exception_decision_unblocks_snapshot_refresh
    AFTER INSERT ON case_agent_ledger_exception_group_decisions
    FOR EACH ROW EXECUTE FUNCTION
        unblock_case_agent_snapshot_refresh_after_exception_decision();

-- Exceptions may be routed before the low-risk click.  In that order the
-- 0046 outbox trigger initially sees unpromoted exception candidates and
-- inserts a blocked request.  Re-evaluate the same run-wide proof on request
-- insertion so the already-terminal exception lane cannot remain stuck.
CREATE FUNCTION normalize_case_agent_snapshot_refresh_review_gate()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NEW.request_status IN (
        'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
    ) AND case_agent_ledger_extraction_run_review_resolved(
        NEW.run_id, NEW.firm_id, NEW.matter_id
    ) AND NOT EXISTS (
        SELECT 1 FROM case_agent_snapshot_refresh_requests newer
         WHERE newer.run_id = NEW.run_id AND newer.firm_id = NEW.firm_id
           AND newer.matter_id = NEW.matter_id
           AND newer.target_matter_version > NEW.target_matter_version
    ) THEN
        UPDATE case_agent_snapshot_refresh_requests
           SET request_status = 'PENDING', updated_at = now()
         WHERE refresh_request_id = NEW.refresh_request_id
           AND request_status IN (
               'BLOCKED_BY_OPEN_REVIEW', 'BLOCKED_BY_OPEN_EXCEPTIONS'
           );
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_snapshot_refresh_review_gate
    AFTER INSERT ON case_agent_snapshot_refresh_requests
    FOR EACH ROW EXECUTE FUNCTION
        normalize_case_agent_snapshot_refresh_review_gate();

CREATE FUNCTION enqueue_case_agent_snapshot_refresh_from_exception_only_run()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE source_run_id uuid;
DECLARE source_batch_id uuid;
DECLARE source_version integer;
BEGIN
    IF NEW.event_type <> 'CASE_LEDGER_EXTRACTION_EXCEPTION_ONLY_RUN_RESOLVED' THEN
        RETURN NEW;
    END IF;
    SELECT (audit.payload->>'run_id')::uuid,
           (audit.payload->>'extraction_batch_id')::uuid,
           audit.input_version
      INTO source_run_id, source_batch_id, source_version
      FROM audit_events audit
     WHERE audit.event_id = (NEW.payload->>'audit_event_id')::uuid
       AND audit.firm_id = NEW.firm_id AND audit.matter_id = NEW.matter_id
       AND audit.event_type = NEW.event_type
       AND audit.output_version = NEW.aggregate_version
       AND audit.output_version = audit.input_version + 1;
    IF source_run_id IS NULL OR source_batch_id IS NULL
       OR NOT case_agent_ledger_extraction_run_review_resolved(
            source_run_id, NEW.firm_id, NEW.matter_id
       )
       OR EXISTS (
           SELECT 1 FROM case_agent_ledger_extraction_batches batch
            WHERE batch.run_id = source_run_id AND batch.firm_id = NEW.firm_id
              AND batch.matter_id = NEW.matter_id
              AND batch.eligible_candidate_count > 0
       ) THEN
        RAISE EXCEPTION 'exception-only resolution outbox is not run-complete';
    END IF;
    INSERT INTO case_agent_snapshot_refresh_requests (
        source_outbox_id, source_audit_event_id, extraction_batch_id, run_id,
        firm_id, matter_id, source_matter_version, target_matter_version,
        request_status
    ) VALUES (
        NEW.outbox_id, (NEW.payload->>'audit_event_id')::uuid,
        source_batch_id, source_run_id, NEW.firm_id, NEW.matter_id,
        source_version, NEW.aggregate_version, 'PENDING'
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_exception_only_resolution_enqueues_snapshot_refresh
    AFTER INSERT ON outbox_events
    FOR EACH ROW
    WHEN (NEW.event_type =
        'CASE_LEDGER_EXTRACTION_EXCEPTION_ONLY_RUN_RESOLVED')
    EXECUTE FUNCTION
        enqueue_case_agent_snapshot_refresh_from_exception_only_run();

CREATE OR REPLACE FUNCTION block_unresolved_ledger_review_work_plan_promotion()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT case_agent_ledger_extraction_run_review_resolved(
        NEW.run_id, NEW.firm_id, NEW.matter_id
    ) THEN
        RAISE EXCEPTION 'Agent work plan promotion is blocked by open ledger review';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION block_unresolved_ledger_review_work_plan_activation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_run_id uuid;
BEGIN
    IF OLD.status <> 'CANDIDATE' OR NEW.status <> 'ACTIVE' THEN
        RETURN NEW;
    END IF;
    SELECT promotion.run_id INTO source_run_id
      FROM case_agent_work_plan_promotions promotion
     WHERE promotion.plan_id = NEW.plan_id AND promotion.firm_id = NEW.firm_id
       AND promotion.matter_id = NEW.matter_id;
    IF source_run_id IS NOT NULL
       AND NOT case_agent_ledger_extraction_run_review_resolved(
            source_run_id, NEW.firm_id, NEW.matter_id
       ) THEN
        RAISE EXCEPTION 'work plan activation is blocked by open ledger review';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION prohibit_case_agent_ledger_exception_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent ledger exception records are append-only';
END;
$$;

CREATE TRIGGER case_agent_ledger_exception_groups_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_exception_groups
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_exception_mutation();
CREATE TRIGGER case_agent_ledger_exception_members_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_exception_group_members
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_exception_mutation();
CREATE TRIGGER case_agent_ledger_exception_decisions_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_exception_group_decisions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_exception_mutation();
CREATE TRIGGER case_agent_ledger_exception_events_append_only
    BEFORE UPDATE OR DELETE ON case_agent_ledger_exception_decision_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_ledger_exception_mutation();

CREATE INDEX case_agent_ledger_exception_groups_batch_idx
    ON case_agent_ledger_exception_groups (
        firm_id, matter_id, extraction_batch_id, exception_group_id
    );
CREATE INDEX case_agent_ledger_exception_groups_run_idx
    ON case_agent_ledger_exception_groups (
        firm_id, matter_id, run_id, exception_group_id
    );
CREATE INDEX case_agent_ledger_exception_decisions_run_idx
    ON case_agent_ledger_exception_group_decisions (
        firm_id, matter_id, run_id, decided_at
    );

ALTER TABLE case_agent_ledger_exception_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_ledger_exception_groups FORCE ROW LEVEL SECURITY;
ALTER TABLE case_agent_ledger_exception_group_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_ledger_exception_group_members FORCE ROW LEVEL SECURITY;
ALTER TABLE case_agent_ledger_exception_group_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_ledger_exception_group_decisions FORCE ROW LEVEL SECURITY;
ALTER TABLE case_agent_ledger_exception_decision_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_ledger_exception_decision_events FORCE ROW LEVEL SECURITY;

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_ledger_exception_groups',
        'case_agent_ledger_exception_group_members',
        'case_agent_ledger_exception_group_decisions',
        'case_agent_ledger_exception_decision_events'
    ] LOOP
        EXECUTE format(
            'CREATE POLICY %I_firm_isolation ON %I '
            'USING (firm_id::text = current_setting(''app.firm_id'', true)) '
            'WITH CHECK (firm_id::text = current_setting(''app.firm_id'', true))',
            table_name, table_name
        );
    END LOOP;
END;
$$;

REVOKE ALL ON TABLE case_agent_ledger_exception_groups FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_exception_group_members FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_exception_group_decisions FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_ledger_exception_decision_events FROM PUBLIC;
REVOKE ALL ON FUNCTION materialize_case_agent_ledger_exception_groups(
    uuid, uuid, uuid
) FROM PUBLIC;

COMMIT;
