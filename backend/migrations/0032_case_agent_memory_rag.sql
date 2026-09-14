-- Governed, ACL-first memory and PostgreSQL full-text retrieval for the
-- lawyer Agent OS.  Apply after 0031_agent_control_plane.sql.
--
-- The record/version/source tables are authoritative and append-only.  The
-- tsvector is only a disposable accelerator: a caller must first resolve an
-- authorized record-id/version/hash set from the authoritative rows, constrain
-- full-text search to that set, and re-authorize every hit before returning it.

BEGIN;

CREATE TABLE case_agent_memory_permission_groups (
    group_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    scope_kind text NOT NULL CHECK (scope_kind IN ('MATTER', 'FIRM')),
    matter_id uuid,
    label_hash char(64) NOT NULL CHECK (label_hash ~ '^[0-9a-f]{64}$'),
    policy_hash char(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (group_id, firm_id),
    FOREIGN KEY (created_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    CHECK (
        (scope_kind = 'MATTER' AND matter_id IS NOT NULL)
        OR (scope_kind = 'FIRM' AND matter_id IS NULL)
    )
);

-- Memberships are grants, never mutable ACL rows.  Revocation is an earlier,
-- append-only denial row so stale caches fail closed before any index search.
CREATE TABLE case_agent_memory_group_memberships (
    membership_id uuid PRIMARY KEY,
    group_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    member_actor_id uuid NOT NULL,
    granted_by uuid NOT NULL,
    grant_hash char(64) NOT NULL CHECK (grant_hash ~ '^[0-9a-f]{64}$'),
    granted_at timestamptz NOT NULL,
    UNIQUE (membership_id, firm_id),
    UNIQUE (membership_id, firm_id, group_id, member_actor_id),
    UNIQUE (group_id, member_actor_id, grant_hash),
    FOREIGN KEY (group_id, firm_id)
        REFERENCES case_agent_memory_permission_groups(group_id, firm_id),
    FOREIGN KEY (member_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (granted_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_agent_memory_group_member_denials (
    denial_id uuid PRIMARY KEY,
    membership_id uuid NOT NULL,
    group_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    member_actor_id uuid NOT NULL,
    denied_by uuid NOT NULL,
    reason_code text NOT NULL CHECK (length(trim(reason_code)) BETWEEN 1 AND 100),
    denial_hash char(64) NOT NULL CHECK (denial_hash ~ '^[0-9a-f]{64}$'),
    denied_at timestamptz NOT NULL,
    UNIQUE (membership_id),
    UNIQUE (denial_id, firm_id),
    FOREIGN KEY (membership_id, firm_id, group_id, member_actor_id)
        REFERENCES case_agent_memory_group_memberships(
            membership_id, firm_id, group_id, member_actor_id
        ),
    FOREIGN KEY (group_id, firm_id)
        REFERENCES case_agent_memory_permission_groups(group_id, firm_id),
    FOREIGN KEY (member_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (denied_by, firm_id) REFERENCES users(user_id, firm_id)
);

-- Only a sanitized, content-addressed derivative may cross a case boundary.
CREATE TABLE case_agent_published_knowledge_objects (
    published_object_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    object_sha256 char(64) NOT NULL CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    storage_object_key text NOT NULL CHECK (
        storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    sanitization_manifest_hash char(64) NOT NULL CHECK (
        sanitization_manifest_hash ~ '^[0-9a-f]{64}$'
    ),
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    media_type text NOT NULL CHECK (length(trim(media_type)) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (published_object_id, firm_id),
    UNIQUE (published_object_id, firm_id, object_sha256, content_sha256),
    UNIQUE (firm_id, object_sha256),
    CHECK (
        storage_object_key = substring(object_sha256 FROM 1 FOR 2) || '/' ||
            substring(object_sha256 FROM 3 FOR 2) || '/' || object_sha256 || '.lca'
    )
);

CREATE TABLE case_agent_knowledge_publication_reviews (
    review_id uuid PRIMARY KEY,
    publication_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    source_matter_id uuid NOT NULL,
    candidate_approval_hash char(64) NOT NULL CHECK (
        candidate_approval_hash ~ '^[0-9a-f]{64}$'
    ),
    reviewer_id uuid NOT NULL,
    review_order text NOT NULL CHECK (review_order IN ('FIRST', 'SECOND')),
    decision text NOT NULL CHECK (decision = 'APPROVED'),
    review_hash char(64) NOT NULL CHECK (review_hash ~ '^[0-9a-f]{64}$'),
    reviewed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (publication_id, review_order),
    UNIQUE (publication_id, reviewer_id),
    UNIQUE (
        publication_id, firm_id, source_matter_id, reviewer_id,
        candidate_approval_hash, review_hash, review_order
    ),
    FOREIGN KEY (source_matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (reviewer_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_agent_knowledge_publications (
    publication_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    target text NOT NULL CHECK (target IN ('LAWYER_PERSONAL', 'FIRM_KNOWLEDGE')),
    source_matter_id uuid NOT NULL,
    published_object_id uuid NOT NULL,
    published_content_hash char(64) NOT NULL CHECK (
        published_content_hash ~ '^[0-9a-f]{64}$'
    ),
    published_source_object_hash char(64) NOT NULL CHECK (
        published_source_object_hash ~ '^[0-9a-f]{64}$'
    ),
    provenance_hash char(64) NOT NULL CHECK (provenance_hash ~ '^[0-9a-f]{64}$'),
    owner_actor_id uuid,
    source_object_hashes char(64)[] NOT NULL CHECK (
        cardinality(source_object_hashes) BETWEEN 1 AND 1000
    ),
    anonymization_review text NOT NULL CHECK (anonymization_review = 'PASSED'),
    conflict_review text NOT NULL CHECK (conflict_review = 'PASSED'),
    confidentiality_review text NOT NULL CHECK (confidentiality_review = 'PASSED'),
    first_approved_by uuid NOT NULL,
    second_approved_by uuid NOT NULL,
    first_review_hash char(64) NOT NULL CHECK (first_review_hash ~ '^[0-9a-f]{64}$'),
    second_review_hash char(64) NOT NULL CHECK (second_review_hash ~ '^[0-9a-f]{64}$'),
    approved_permission_group_ids uuid[] NOT NULL,
    first_review_order text NOT NULL DEFAULT 'FIRST' CHECK (first_review_order = 'FIRST'),
    second_review_order text NOT NULL DEFAULT 'SECOND' CHECK (second_review_order = 'SECOND'),
    publication_policy_version text NOT NULL CHECK (
        length(trim(publication_policy_version)) BETWEEN 1 AND 200
    ),
    publication_policy_hash char(64) NOT NULL CHECK (
        publication_policy_hash ~ '^[0-9a-f]{64}$'
    ),
    approved_at timestamptz NOT NULL,
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (publication_id, firm_id),
    FOREIGN KEY (source_matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (published_object_id, firm_id)
        REFERENCES case_agent_published_knowledge_objects(published_object_id, firm_id),
    FOREIGN KEY (
        published_object_id, firm_id, published_source_object_hash,
        published_content_hash
    ) REFERENCES case_agent_published_knowledge_objects(
        published_object_id, firm_id, object_sha256, content_sha256
    ),
    FOREIGN KEY (owner_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (first_approved_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (second_approved_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (
        publication_id, firm_id, source_matter_id, first_approved_by,
        approval_hash, first_review_hash, first_review_order
    ) REFERENCES case_agent_knowledge_publication_reviews(
        publication_id, firm_id, source_matter_id, reviewer_id,
        candidate_approval_hash, review_hash, review_order
    ),
    FOREIGN KEY (
        publication_id, firm_id, source_matter_id, second_approved_by,
        approval_hash, second_review_hash, second_review_order
    ) REFERENCES case_agent_knowledge_publication_reviews(
        publication_id, firm_id, source_matter_id, reviewer_id,
        candidate_approval_hash, review_hash, review_order
    ),
    CHECK (first_approved_by <> second_approved_by),
    CHECK (first_review_hash <> second_review_hash),
    CHECK (
        (target = 'LAWYER_PERSONAL' AND owner_actor_id IS NOT NULL
            AND owner_actor_id <> first_approved_by
            AND owner_actor_id <> second_approved_by
            AND cardinality(approved_permission_group_ids) = 0)
        OR (target = 'FIRM_KNOWLEDGE' AND owner_actor_id IS NULL
            AND cardinality(approved_permission_group_ids) BETWEEN 1 AND 1000)
    )
);

CREATE TABLE case_agent_knowledge_publication_groups (
    publication_id uuid NOT NULL,
    group_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (publication_id, group_id),
    FOREIGN KEY (publication_id, firm_id)
        REFERENCES case_agent_knowledge_publications(publication_id, firm_id),
    FOREIGN KEY (group_id, firm_id)
        REFERENCES case_agent_memory_permission_groups(group_id, firm_id)
);

CREATE FUNCTION validate_case_agent_publication_groups() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    publication_target text;
    approved_group_ids uuid[];
    persisted_group_ids uuid[];
BEGIN
    SELECT target, approved_permission_group_ids
      INTO publication_target, approved_group_ids
    FROM case_agent_knowledge_publications publication
    WHERE publication.publication_id = NEW.publication_id
      AND publication.firm_id = NEW.firm_id;

    SELECT COALESCE(array_agg(publication_group.group_id ORDER BY publication_group.group_id), ARRAY[]::uuid[])
      INTO persisted_group_ids
    FROM case_agent_knowledge_publication_groups publication_group
    JOIN case_agent_memory_permission_groups permission_group
      ON permission_group.group_id = publication_group.group_id
     AND permission_group.firm_id = publication_group.firm_id
    WHERE publication_group.publication_id = NEW.publication_id
      AND publication_group.firm_id = NEW.firm_id
      AND permission_group.scope_kind = 'FIRM';

    IF publication_target IS NULL
       OR persisted_group_ids <> (
           SELECT COALESCE(array_agg(value ORDER BY value), ARRAY[]::uuid[])
           FROM unnest(approved_group_ids) value
       ) THEN
        RAISE EXCEPTION 'publication groups must exactly match the approved publication scope';
    END IF;
    RETURN NEW;
END;
$$;
CREATE CONSTRAINT TRIGGER case_agent_publication_groups_guard
    AFTER INSERT ON case_agent_knowledge_publications
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_publication_groups();

-- PUBLIC_LEGAL effective periods are lawyer-governed authority data, not free
-- metadata copied from a model.  A registration binds one exact approved
-- period and authority tier to an exact set of current official snapshots.
CREATE TABLE case_agent_public_legal_authority_registrations (
    registration_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    authority text NOT NULL CHECK (authority IN (
        'PRIMARY_LAW', 'JUDICIAL_INTERPRETATION', 'OFFICIAL_CASE'
    )),
    effective_from date NOT NULL,
    effective_to date,
    publication_approval_hash char(64) NOT NULL CHECK (
        publication_approval_hash ~ '^[0-9a-f]{64}$'
    ),
    registration_hash char(64) NOT NULL CHECK (
        registration_hash ~ '^[0-9a-f]{64}$'
    ),
    approved_by uuid NOT NULL,
    approved_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (registration_id, firm_id),
    UNIQUE (registration_hash, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE case_agent_public_legal_authority_sources (
    registration_id uuid NOT NULL,
    snapshot_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    snapshot_content_sha256 char(64) NOT NULL CHECK (
        snapshot_content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    snapshot_verification_hash char(64) NOT NULL CHECK (
        snapshot_verification_hash ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (registration_id, snapshot_id),
    FOREIGN KEY (registration_id, firm_id)
        REFERENCES case_agent_public_legal_authority_registrations(
            registration_id, firm_id
        ),
    FOREIGN KEY (snapshot_id, firm_id, snapshot_content_sha256)
        REFERENCES official_legal_source_snapshots(
            snapshot_id, firm_id, content_sha256
        )
);

CREATE FUNCTION validate_case_agent_public_legal_authority_source() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM case_agent_public_legal_authority_registrations registration
        JOIN official_legal_source_snapshots snapshot
          ON snapshot.snapshot_id = NEW.snapshot_id
         AND snapshot.firm_id = NEW.firm_id
         AND snapshot.content_sha256 = NEW.snapshot_content_sha256
        WHERE registration.registration_id = NEW.registration_id
          AND registration.firm_id = NEW.firm_id
          AND snapshot.verification_hash = NEW.snapshot_verification_hash
          AND snapshot.verification_status = 'VERIFIED'
          AND snapshot.license_status = 'ACTIVE'
          AND snapshot.authority_level = registration.authority
    ) THEN
        RAISE EXCEPTION 'public legal authority registration source is not an exact current official snapshot';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_public_legal_authority_source_guard
    BEFORE INSERT ON case_agent_public_legal_authority_sources
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_public_legal_authority_source();

CREATE TABLE case_agent_memory_record_versions (
    record_id uuid NOT NULL,
    record_version integer NOT NULL CHECK (record_version > 0),
    -- PUBLIC_LEGAL has no semantic tenant in the domain model, but every
    -- physical snapshot still has a custodian firm for RLS and source binding.
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    semantic_firm_id uuid,
    layer text NOT NULL CHECK (layer IN (
        'RUN_WORKING', 'CASE_LONG_TERM', 'LAWYER_PERSONAL',
        'FIRM_KNOWLEDGE', 'PUBLIC_LEGAL'
    )),
    status text NOT NULL CHECK (status IN (
        'CANDIDATE', 'CONFIRMED', 'PUBLISHED', 'SUPERSEDED', 'REVOKED', 'DELETED'
    )),
    authority text NOT NULL CHECK (authority IN (
        'CASE_EVIDENCE', 'CONFIRMED_CASE_LEDGER', 'LAWYER_NOTE', 'FIRM_SOP',
        'PRIMARY_LAW', 'JUDICIAL_INTERPRETATION', 'OFFICIAL_CASE'
    )),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    content_object_key text NOT NULL CHECK (
        content_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    -- This bounded normalized lexeme document is produced from the encrypted
    -- source by a trusted extractor; it is not the source document body.
    search_document text NOT NULL CHECK (
        octet_length(search_document) BETWEEN 1 AND 1048576
    ),
    search_document_hash char(64) NOT NULL CHECK (
        search_document_hash ~ '^[0-9a-f]{64}$'
    ),
    extractor_id text NOT NULL CHECK (length(trim(extractor_id)) BETWEEN 1 AND 200),
    extractor_version text NOT NULL CHECK (
        length(trim(extractor_version)) BETWEEN 1 AND 200
    ),
    indexing_receipt_hash char(64) NOT NULL CHECK (
        indexing_receipt_hash ~ '^[0-9a-f]{64}$'
    ),
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', search_document)
    ) STORED,
    governance_matter_id uuid NOT NULL,
    matter_id uuid,
    owner_actor_id uuid,
    run_id uuid,
    task_id uuid,
    case_type_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
    procedure_stages text[] NOT NULL DEFAULT ARRAY[]::text[],
    issue_tags text[] NOT NULL DEFAULT ARRAY[]::text[],
    effective_from date,
    effective_to date,
    known_from timestamptz NOT NULL,
    known_to timestamptz,
    publication_id uuid,
    publication_approval_hash char(64),
    source_authority_registry_hash char(64),
    provenance_hash char(64) NOT NULL CHECK (provenance_hash ~ '^[0-9a-f]{64}$'),
    written_by uuid NOT NULL,
    updated_at timestamptz NOT NULL,
    persisted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, record_version),
    UNIQUE (record_id, record_version, firm_id),
    FOREIGN KEY (semantic_firm_id) REFERENCES firms(firm_id),
    FOREIGN KEY (governance_matter_id, firm_id)
        REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (owner_actor_id, firm_id)
        REFERENCES users(user_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (publication_id, firm_id)
        REFERENCES case_agent_knowledge_publications(publication_id, firm_id),
    FOREIGN KEY (source_authority_registry_hash, firm_id)
        REFERENCES case_agent_public_legal_authority_registrations(
            registration_hash, firm_id
        ),
    FOREIGN KEY (written_by, firm_id)
        REFERENCES users(user_id, firm_id),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
    CHECK (known_to IS NULL OR known_to > known_from),
    CHECK (updated_at >= known_from),
    CHECK (
        content_object_key = substring(content_sha256 FROM 1 FOR 2) || '/' ||
            substring(content_sha256 FROM 3 FOR 2) || '/' || content_sha256 || '.lca'
    ),
    CHECK (
        (layer = 'RUN_WORKING' AND semantic_firm_id = firm_id
            AND matter_id = governance_matter_id AND owner_actor_id IS NOT NULL
            AND run_id IS NOT NULL AND status IN (
                'CANDIDATE', 'CONFIRMED', 'SUPERSEDED', 'REVOKED', 'DELETED'
            ) AND publication_id IS NULL AND publication_approval_hash IS NULL
            AND source_authority_registry_hash IS NULL)
        OR (layer = 'CASE_LONG_TERM' AND semantic_firm_id = firm_id
            AND matter_id = governance_matter_id AND owner_actor_id IS NULL AND run_id IS NULL
            AND task_id IS NULL AND status IN (
                'CONFIRMED', 'SUPERSEDED', 'REVOKED', 'DELETED'
            ) AND publication_id IS NULL AND publication_approval_hash IS NULL
            AND source_authority_registry_hash IS NULL)
        OR (layer = 'LAWYER_PERSONAL' AND semantic_firm_id = firm_id
            AND matter_id IS NULL AND owner_actor_id IS NOT NULL AND run_id IS NULL
            AND task_id IS NULL AND status IN (
                'PUBLISHED', 'SUPERSEDED', 'REVOKED', 'DELETED'
            ) AND publication_id IS NOT NULL
            AND publication_approval_hash ~ '^[0-9a-f]{64}$'
            AND source_authority_registry_hash IS NULL)
        OR (layer = 'FIRM_KNOWLEDGE' AND semantic_firm_id = firm_id
            AND matter_id IS NULL AND owner_actor_id IS NULL AND run_id IS NULL
            AND task_id IS NULL AND status IN (
                'PUBLISHED', 'SUPERSEDED', 'REVOKED', 'DELETED'
            ) AND publication_id IS NOT NULL
            AND publication_approval_hash ~ '^[0-9a-f]{64}$'
            AND source_authority_registry_hash IS NULL)
        OR (layer = 'PUBLIC_LEGAL' AND semantic_firm_id IS NULL
            AND matter_id IS NULL AND owner_actor_id IS NULL AND run_id IS NULL
            AND task_id IS NULL AND status IN (
                'PUBLISHED', 'SUPERSEDED', 'REVOKED', 'DELETED'
            ) AND publication_id IS NULL
            AND effective_from IS NOT NULL
            AND publication_approval_hash ~ '^[0-9a-f]{64}$'
            AND source_authority_registry_hash ~ '^[0-9a-f]{64}$')
    )
);

CREATE TABLE case_agent_memory_record_groups (
    record_id uuid NOT NULL,
    record_version integer NOT NULL,
    group_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, record_version, group_id),
    FOREIGN KEY (record_id, record_version, firm_id)
        REFERENCES case_agent_memory_record_versions(
            record_id, record_version, firm_id
        ),
    FOREIGN KEY (group_id, firm_id)
        REFERENCES case_agent_memory_permission_groups(group_id, firm_id)
);

CREATE TABLE case_agent_memory_source_refs (
    source_ref_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id uuid NOT NULL,
    record_version integer NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    source_type text NOT NULL CHECK (source_type IN (
        'EVIDENCE_PAGE', 'CASE_FACT', 'CASE_CLAIM', 'CASE_TRANSACTION',
        'PUBLISHED_KNOWLEDGE_OBJECT',
        'OFFICIAL_LEGAL_SNAPSHOT', 'OFFICIAL_CASE_SNAPSHOT'
    )),
    source_id uuid NOT NULL,
    source_version text NOT NULL CHECK (length(trim(source_version)) BETWEEN 1 AND 300),
    source_record_hash char(64) NOT NULL CHECK (
        source_record_hash ~ '^[0-9a-f]{64}$'
    ),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    location_kind text NOT NULL CHECK (location_kind IN (
        'OBJECT', 'DOCUMENT_PAGE', 'IMAGE_REGION', 'PARAGRAPH', 'SHEET_RANGE',
        'MEDIA_TIME_RANGE', 'WEB_FRAGMENT'
    )),
    exposure text NOT NULL CHECK (exposure IN (
        'CASE_PRIVATE', 'PUBLISHED_SANITIZED', 'PUBLIC_OFFICIAL'
    )),
    page_number integer CHECK (page_number > 0),
    normalized_box numeric(12,9)[],
    paragraph_label text,
    sheet_name text,
    cell_range text,
    start_millis bigint,
    end_millis bigint,
    source_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        record_id, record_version, source_type, source_id, source_version,
        source_record_hash, content_sha256
    ),
    FOREIGN KEY (record_id, record_version, firm_id)
        REFERENCES case_agent_memory_record_versions(
            record_id, record_version, firm_id
        ),
    CHECK (
        (location_kind IN ('DOCUMENT_PAGE', 'IMAGE_REGION') AND page_number IS NOT NULL)
        OR (location_kind NOT IN ('DOCUMENT_PAGE', 'IMAGE_REGION') AND page_number IS NULL)
    ),
    CHECK (
        (location_kind = 'IMAGE_REGION' AND cardinality(normalized_box) = 4
            AND normalized_box[1] >= 0 AND normalized_box[1] < normalized_box[3]
            AND normalized_box[3] <= 1 AND normalized_box[2] >= 0
            AND normalized_box[2] < normalized_box[4] AND normalized_box[4] <= 1)
        OR (location_kind <> 'IMAGE_REGION' AND normalized_box IS NULL)
    ),
    CHECK (
        (location_kind = 'PARAGRAPH' AND length(trim(paragraph_label)) BETWEEN 1 AND 200)
        OR (location_kind <> 'PARAGRAPH' AND paragraph_label IS NULL)
    ),
    CHECK (
        (location_kind = 'SHEET_RANGE' AND length(trim(sheet_name)) BETWEEN 1 AND 200
            AND length(trim(cell_range)) BETWEEN 1 AND 100)
        OR (location_kind <> 'SHEET_RANGE' AND sheet_name IS NULL AND cell_range IS NULL)
    ),
    CHECK (
        (location_kind = 'MEDIA_TIME_RANGE' AND start_millis >= 0
            AND end_millis > start_millis)
        OR (location_kind <> 'MEDIA_TIME_RANGE'
            AND start_millis IS NULL AND end_millis IS NULL)
    ),
    CHECK (
        (location_kind = 'WEB_FRAGMENT' AND source_url ~ '^https://')
        OR (location_kind <> 'WEB_FRAGMENT' AND source_url IS NULL)
    )
);

CREATE TABLE case_agent_memory_tombstones (
    tombstone_id uuid PRIMARY KEY,
    record_id uuid NOT NULL,
    blocked_through_version integer NOT NULL CHECK (blocked_through_version > 0),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    terminal_status text NOT NULL CHECK (terminal_status IN ('REVOKED', 'DELETED')),
    reason_code text NOT NULL CHECK (length(trim(reason_code)) BETWEEN 1 AND 100),
    denied_by uuid NOT NULL,
    denied_at timestamptz NOT NULL,
    tombstone_hash char(64) NOT NULL CHECK (tombstone_hash ~ '^[0-9a-f]{64}$'),
    UNIQUE (record_id, blocked_through_version),
    UNIQUE (tombstone_id, firm_id),
    FOREIGN KEY (denied_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (record_id, blocked_through_version, firm_id)
        REFERENCES case_agent_memory_record_versions(record_id, record_version, firm_id)
        DEFERRABLE INITIALLY DEFERRED
);

-- Optional actor/group denials can invalidate access immediately without
-- rewriting an immutable record or waiting for an index refresh.
CREATE TABLE case_agent_memory_access_denials (
    denial_id uuid PRIMARY KEY,
    record_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    subject_kind text NOT NULL CHECK (subject_kind IN ('ACTOR', 'GROUP')),
    subject_id uuid NOT NULL,
    actor_subject_id uuid,
    group_subject_id uuid,
    denied_by uuid NOT NULL,
    reason_code text NOT NULL CHECK (length(trim(reason_code)) BETWEEN 1 AND 100),
    denial_hash char(64) NOT NULL CHECK (denial_hash ~ '^[0-9a-f]{64}$'),
    denied_at timestamptz NOT NULL,
    UNIQUE (record_id, subject_kind, subject_id),
    FOREIGN KEY (denied_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (actor_subject_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (group_subject_id, firm_id)
        REFERENCES case_agent_memory_permission_groups(group_id, firm_id),
    CHECK (
        (subject_kind = 'ACTOR' AND actor_subject_id = subject_id
            AND group_subject_id IS NULL)
        OR (subject_kind = 'GROUP' AND group_subject_id = subject_id
            AND actor_subject_id IS NULL)
    )
);

CREATE TABLE case_agent_memory_record_heads (
    record_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    current_version integer NOT NULL CHECK (current_version > 0),
    current_status text NOT NULL CHECK (current_status IN (
        'CANDIDATE', 'CONFIRMED', 'PUBLISHED', 'SUPERSEDED', 'REVOKED', 'DELETED'
    )),
    current_content_sha256 char(64) NOT NULL CHECK (
        current_content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    current_provenance_hash char(64) NOT NULL CHECK (
        current_provenance_hash ~ '^[0-9a-f]{64}$'
    ),
    updated_at timestamptz NOT NULL,
    UNIQUE (record_id, firm_id),
    FOREIGN KEY (record_id, current_version, firm_id)
        REFERENCES case_agent_memory_record_versions(
            record_id, record_version, firm_id
        ) DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE case_agent_memory_access_denials
    ADD CONSTRAINT case_agent_memory_access_denials_record_fk
    FOREIGN KEY (record_id, firm_id)
    REFERENCES case_agent_memory_record_heads(record_id, firm_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE case_agent_memory_retrieval_audits (
    retrieval_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    run_id uuid,
    task_id uuid,
    query_hash char(64) NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),
    query_fingerprint char(64) NOT NULL CHECK (query_fingerprint ~ '^[0-9a-f]{64}$'),
    grant_hash char(64) NOT NULL CHECK (grant_hash ~ '^[0-9a-f]{64}$'),
    matter_version integer NOT NULL CHECK (matter_version > 0),
    scope_hash char(64) NOT NULL CHECK (scope_hash ~ '^[0-9a-f]{64}$'),
    authorized_record_refs jsonb NOT NULL CHECK (
        jsonb_typeof(authorized_record_refs) = 'array'
        AND jsonb_array_length(authorized_record_refs) BETWEEN 1 AND 1000
    ),
    returned_record_refs jsonb NOT NULL CHECK (
        jsonb_typeof(returned_record_refs) = 'array'
    ),
    search_mode text NOT NULL CHECK (search_mode = 'POSTGRES_FTS_V1'),
    authorized_at timestamptz NOT NULL,
    verified_at timestamptz NOT NULL CHECK (verified_at >= authorized_at),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (retrieval_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    CHECK (task_id IS NULL OR run_id IS NOT NULL)
);

CREATE FUNCTION validate_case_agent_memory_retrieval_run_scope() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM case_agent_runs run
        WHERE run.run_id = NEW.run_id
          AND run.firm_id = NEW.firm_id
          AND run.matter_id = NEW.matter_id
          AND run.created_by = NEW.actor_id
    ) THEN
        RAISE EXCEPTION 'memory retrieval is outside the lawyer Agent run';
    END IF;
    IF NEW.task_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM case_agent_tasks task
        WHERE task.run_id = NEW.run_id
          AND task.task_id = NEW.task_id
          AND task.firm_id = NEW.firm_id
          AND task.matter_id = NEW.matter_id
    ) THEN
        RAISE EXCEPTION 'memory retrieval task is outside the Agent run';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_memory_retrieval_run_scope_guard
    BEFORE INSERT ON case_agent_memory_retrieval_audits
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_memory_retrieval_run_scope();

CREATE TABLE case_agent_memory_checkpoints (
    checkpoint_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    owner_actor_id uuid NOT NULL,
    run_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    previous_checkpoint_hash char(64),
    case_snapshot_hash char(64) NOT NULL CHECK (case_snapshot_hash ~ '^[0-9a-f]{64}$'),
    plan_hash char(64) NOT NULL CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    task_state_hash char(64) NOT NULL CHECK (task_state_hash ~ '^[0-9a-f]{64}$'),
    unresolved_question_ids uuid[] NOT NULL,
    retrieval_scope_hashes char(64)[] NOT NULL,
    occurred_at timestamptz NOT NULL,
    checkpoint_hash char(64) NOT NULL CHECK (checkpoint_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, sequence),
    UNIQUE (checkpoint_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (owner_actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    CHECK (
        (sequence = 1 AND previous_checkpoint_hash IS NULL)
        OR (sequence > 1 AND previous_checkpoint_hash ~ '^[0-9a-f]{64}$')
    )
);

CREATE UNIQUE INDEX case_agent_memory_checkpoint_hash_unique
    ON case_agent_memory_checkpoints(firm_id, matter_id, run_id, checkpoint_hash);

CREATE FUNCTION validate_case_agent_memory_checkpoint() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    run_owner uuid;
    run_snapshot_hash char(64);
    run_graph_hash char(64);
    expected_task_state_hash char(64);
    expected_previous_hash char(64);
    expected_sequence integer;
BEGIN
    SELECT run.created_by, run.snapshot_hash, run.current_graph_hash
      INTO run_owner, run_snapshot_hash, run_graph_hash
    FROM case_agent_runs run
    WHERE run.run_id = NEW.run_id
      AND run.firm_id = NEW.firm_id
      AND run.matter_id = NEW.matter_id;
    IF run_owner IS NULL OR run_owner <> NEW.owner_actor_id
       OR run_snapshot_hash <> NEW.case_snapshot_hash
       OR run_graph_hash IS NULL OR run_graph_hash <> NEW.plan_hash THEN
        RAISE EXCEPTION 'memory checkpoint differs from the current Agent run';
    END IF;

    SELECT run.projection_hash
      INTO expected_task_state_hash
    FROM case_agent_runs run
    WHERE run.run_id = NEW.run_id
      AND run.firm_id = NEW.firm_id
      AND run.matter_id = NEW.matter_id;
    IF expected_task_state_hash <> NEW.task_state_hash THEN
        RAISE EXCEPTION 'memory checkpoint task state is not current';
    END IF;

    IF EXISTS (
        SELECT 1 FROM unnest(NEW.unresolved_question_ids) question_id
        WHERE NOT EXISTS (
            SELECT 1 FROM case_agent_task_heads head
            WHERE head.run_id = NEW.run_id
              AND head.firm_id = NEW.firm_id
              AND head.matter_id = NEW.matter_id
              AND head.task_id = question_id
              AND head.status = 'WAITING_APPROVAL'
              AND head.is_current = true
        )
    ) THEN
        RAISE EXCEPTION 'memory checkpoint contains an unknown unresolved question';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(NEW.retrieval_scope_hashes) scope_hash
        WHERE NOT EXISTS (
            SELECT 1 FROM case_agent_memory_retrieval_audits retrieval
            WHERE retrieval.firm_id = NEW.firm_id
              AND retrieval.matter_id = NEW.matter_id
              AND retrieval.actor_id = NEW.owner_actor_id
              AND retrieval.run_id = NEW.run_id
              AND retrieval.scope_hash = scope_hash
        )
    ) THEN
        RAISE EXCEPTION 'memory checkpoint contains an unauthorized retrieval scope';
    END IF;

    SELECT checkpoint.sequence + 1, checkpoint.checkpoint_hash
      INTO expected_sequence, expected_previous_hash
    FROM case_agent_memory_checkpoints checkpoint
    WHERE checkpoint.firm_id = NEW.firm_id
      AND checkpoint.matter_id = NEW.matter_id
      AND checkpoint.run_id = NEW.run_id
    ORDER BY checkpoint.sequence DESC
    LIMIT 1;
    IF expected_sequence IS NULL THEN
        expected_sequence := 1;
        expected_previous_hash := NULL;
    END IF;
    IF NEW.sequence <> expected_sequence
       OR NEW.previous_checkpoint_hash IS DISTINCT FROM expected_previous_hash THEN
        RAISE EXCEPTION 'memory checkpoint does not extend the current hash chain';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_memory_checkpoint_guard
    BEFORE INSERT ON case_agent_memory_checkpoints
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_memory_checkpoint();

CREATE INDEX case_agent_memory_fts_idx
    ON case_agent_memory_record_versions USING gin(search_vector);
CREATE INDEX case_agent_memory_current_scope_idx
    ON case_agent_memory_record_versions(
        firm_id, layer, matter_id, owner_actor_id, run_id, record_id
    );
CREATE INDEX case_agent_memory_record_groups_lookup_idx
    ON case_agent_memory_record_groups(firm_id, group_id, record_id, record_version);
CREATE INDEX case_agent_memory_membership_lookup_idx
    ON case_agent_memory_group_memberships(firm_id, member_actor_id, group_id);
CREATE INDEX case_agent_memory_denial_lookup_idx
    ON case_agent_memory_access_denials(firm_id, record_id, subject_kind, subject_id);

CREATE FUNCTION validate_case_agent_memory_source_ref() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    record_layer text;
    record_matter_id uuid;
    record_authority text;
    record_publication_id uuid;
    record_effective_from date;
    record_effective_to date;
    record_publication_approval_hash char(64);
    record_source_authority_registry_hash char(64);
    source_ok boolean;
BEGIN
    SELECT layer, matter_id, authority, publication_id, effective_from,
           effective_to, publication_approval_hash,
           source_authority_registry_hash
      INTO record_layer, record_matter_id, record_authority,
           record_publication_id, record_effective_from, record_effective_to,
           record_publication_approval_hash,
           record_source_authority_registry_hash
    FROM case_agent_memory_record_versions
    WHERE record_id = NEW.record_id AND record_version = NEW.record_version
      AND firm_id = NEW.firm_id;

    IF NEW.source_type = 'EVIDENCE_PAGE' THEN
        SELECT EXISTS (
            SELECT 1 FROM evidence_pages page
            WHERE page.evidence_page_id = NEW.source_id
              AND page.firm_id = NEW.firm_id
              AND page.matter_id = record_matter_id
              AND page.rendered_page_sha256 = NEW.source_record_hash
              AND page.rendered_page_sha256 = NEW.content_sha256
              AND page.page_number = NEW.page_number
        ) INTO source_ok;
        IF record_layer NOT IN ('RUN_WORKING', 'CASE_LONG_TERM')
           OR NEW.exposure <> 'CASE_PRIVATE' OR NOT source_ok THEN
            RAISE EXCEPTION 'memory evidence source is not an authoritative same-case page';
        END IF;
    ELSIF NEW.source_type IN ('CASE_FACT', 'CASE_CLAIM', 'CASE_TRANSACTION') THEN
        IF NEW.source_type = 'CASE_FACT' THEN
            SELECT EXISTS (
                SELECT 1 FROM case_facts fact
                WHERE fact.fact_id = NEW.source_id AND fact.firm_id = NEW.firm_id
                  AND fact.matter_id = record_matter_id AND fact.status = 'CONFIRMED'
                  AND fact.decision_hash = NEW.source_record_hash
                  AND encode(digest(
                      convert_to(
                          jsonb_build_object(
                              'schema_version', 'memory-case-fact-v1',
                              'fact_id', fact.fact_id,
                              'matter_id', fact.matter_id,
                              'original_text', fact.original_text,
                              'origin', fact.origin,
                              'status', fact.status,
                              'evidence_links', fact.evidence_links,
                              'decision_hash', fact.decision_hash
                          )::text,
                          'UTF8'
                      ),
                      'sha256'
                  ), 'hex') = NEW.content_sha256
            ) INTO source_ok;
        ELSIF NEW.source_type = 'CASE_CLAIM' THEN
            SELECT EXISTS (
                SELECT 1 FROM case_claims claim
                WHERE claim.claim_id = NEW.source_id AND claim.firm_id = NEW.firm_id
                  AND claim.matter_id = record_matter_id
                  AND claim.status = 'CONFIRMED_SCOPE'
                  AND claim.confirmation_hash = NEW.source_record_hash
                  AND encode(digest(
                      convert_to(
                          jsonb_build_object(
                              'schema_version', 'memory-case-claim-v1',
                              'claim_id', claim.claim_id,
                              'matter_id', claim.matter_id,
                              'original_claim_text', claim.original_claim_text,
                              'claimed_amount', claim.claimed_amount,
                              'currency', claim.currency,
                              'status', claim.status,
                              'evidence_links', claim.evidence_links,
                              'confirmation_hash', claim.confirmation_hash
                          )::text,
                          'UTF8'
                      ),
                      'sha256'
                  ), 'hex') = NEW.content_sha256
            ) INTO source_ok;
        ELSE
            SELECT EXISTS (
                SELECT 1 FROM case_transactions transaction_row
                WHERE transaction_row.transaction_id = NEW.source_id
                  AND transaction_row.firm_id = NEW.firm_id
                  AND transaction_row.matter_id = record_matter_id
                  AND transaction_row.status = 'CONFIRMED'
                  AND transaction_row.confirmation_hash = NEW.source_record_hash
                  AND encode(digest(
                      convert_to(
                          jsonb_build_object(
                              'schema_version', 'memory-case-transaction-v1',
                              'transaction_id', transaction_row.transaction_id,
                              'matter_id', transaction_row.matter_id,
                              'local_date', transaction_row.local_date,
                              'date_precision', transaction_row.date_precision,
                              'amount', transaction_row.amount,
                              'currency', transaction_row.currency,
                              'direction', transaction_row.direction,
                              'payer_label', transaction_row.payer_label,
                              'payee_label', transaction_row.payee_label,
                              'channel', transaction_row.channel,
                              'transaction_reference', transaction_row.transaction_reference,
                              'evidence_links', transaction_row.evidence_links,
                              'status', transaction_row.status,
                              'confirmation_hash', transaction_row.confirmation_hash
                          )::text,
                          'UTF8'
                      ),
                      'sha256'
                  ), 'hex') = NEW.content_sha256
            ) INTO source_ok;
        END IF;
        IF record_layer NOT IN ('RUN_WORKING', 'CASE_LONG_TERM')
           OR NEW.exposure <> 'CASE_PRIVATE' OR NOT source_ok THEN
            RAISE EXCEPTION 'memory ledger source is not a confirmed same-case object';
        END IF;
    ELSIF NEW.source_type = 'PUBLISHED_KNOWLEDGE_OBJECT' THEN
        SELECT EXISTS (
            SELECT 1
            FROM case_agent_knowledge_publications publication
            JOIN case_agent_published_knowledge_objects object_row
              ON object_row.published_object_id = publication.published_object_id
             AND object_row.firm_id = publication.firm_id
            WHERE publication.publication_id = record_publication_id
              AND publication.firm_id = NEW.firm_id
              AND object_row.published_object_id = NEW.source_id
              AND publication.approval_hash = NEW.source_record_hash
              AND object_row.object_sha256 = NEW.content_sha256
        ) INTO source_ok;
        IF record_layer NOT IN ('LAWYER_PERSONAL', 'FIRM_KNOWLEDGE')
           OR NEW.exposure <> 'PUBLISHED_SANITIZED' OR NOT source_ok THEN
            RAISE EXCEPTION 'memory publication source is not the approved sanitized object';
        END IF;
    ELSE
        SELECT EXISTS (
            SELECT 1
            FROM case_agent_public_legal_authority_registrations registration
            JOIN case_agent_public_legal_authority_sources authority_source
              ON authority_source.registration_id = registration.registration_id
             AND authority_source.firm_id = registration.firm_id
            JOIN official_legal_source_snapshots snapshot
              ON snapshot.snapshot_id = authority_source.snapshot_id
             AND snapshot.firm_id = authority_source.firm_id
             AND snapshot.content_sha256 = authority_source.snapshot_content_sha256
            WHERE registration.registration_hash = record_source_authority_registry_hash
              AND registration.firm_id = NEW.firm_id
              AND registration.authority = record_authority
              AND registration.effective_from = record_effective_from
              AND registration.effective_to IS NOT DISTINCT FROM record_effective_to
              AND registration.publication_approval_hash = record_publication_approval_hash
              AND snapshot.snapshot_id = NEW.source_id
              AND snapshot.firm_id = NEW.firm_id
              AND snapshot.verification_hash = NEW.source_record_hash
              AND authority_source.snapshot_verification_hash = NEW.source_record_hash
              AND snapshot.content_sha256 = NEW.content_sha256
              AND snapshot.verification_status = 'VERIFIED'
              AND snapshot.license_status = 'ACTIVE'
              AND snapshot.authority_level = record_authority
              AND snapshot.official_url = NEW.source_url
        ) INTO source_ok;
        IF record_layer <> 'PUBLIC_LEGAL'
           OR NEW.exposure <> 'PUBLIC_OFFICIAL' OR NOT source_ok
           OR (record_authority = 'OFFICIAL_CASE'
               AND NEW.source_type <> 'OFFICIAL_CASE_SNAPSHOT')
           OR (record_authority IN ('PRIMARY_LAW', 'JUDICIAL_INTERPRETATION')
               AND NEW.source_type <> 'OFFICIAL_LEGAL_SNAPSHOT') THEN
            RAISE EXCEPTION 'public legal memory is not bound to a current verified official snapshot';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_memory_source_ref_guard
    BEFORE INSERT ON case_agent_memory_source_refs
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_memory_source_ref();

CREATE FUNCTION validate_case_agent_memory_dependencies() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    group_count integer;
    source_count integer;
    incompatible_group_count integer;
BEGIN
    SELECT count(*) INTO source_count
    FROM case_agent_memory_source_refs source
    WHERE source.record_id = NEW.record_id
      AND source.record_version = NEW.record_version
      AND source.firm_id = NEW.firm_id;
    IF source_count < 1 THEN
        RAISE EXCEPTION 'memory version requires at least one authoritative source';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM case_agent_memory_record_heads head
        WHERE head.record_id = NEW.record_id AND head.firm_id = NEW.firm_id
          AND head.current_version >= NEW.record_version
    ) THEN
        RAISE EXCEPTION 'memory version is not reachable from its guarded head';
    END IF;

    SELECT count(*) INTO group_count
    FROM case_agent_memory_record_groups record_group
    JOIN case_agent_memory_permission_groups permission_group
      ON permission_group.group_id = record_group.group_id
     AND permission_group.firm_id = record_group.firm_id
    WHERE record_group.record_id = NEW.record_id
      AND record_group.record_version = NEW.record_version
      AND record_group.firm_id = NEW.firm_id
      AND (
          (NEW.layer = 'CASE_LONG_TERM'
              AND permission_group.scope_kind = 'MATTER'
              AND permission_group.matter_id = NEW.matter_id)
          OR (NEW.layer = 'FIRM_KNOWLEDGE'
              AND permission_group.scope_kind = 'FIRM')
      );
    IF NEW.layer IN ('CASE_LONG_TERM', 'FIRM_KNOWLEDGE') AND group_count < 1 THEN
        RAISE EXCEPTION 'governed case/firm memory requires an explicit compatible group';
    END IF;
    SELECT count(*) INTO incompatible_group_count
    FROM case_agent_memory_record_groups record_group
    LEFT JOIN case_agent_memory_permission_groups permission_group
      ON permission_group.group_id = record_group.group_id
     AND permission_group.firm_id = record_group.firm_id
    WHERE record_group.record_id = NEW.record_id
      AND record_group.record_version = NEW.record_version
      AND record_group.firm_id = NEW.firm_id
      AND NOT (
          (NEW.layer = 'CASE_LONG_TERM'
              AND permission_group.scope_kind = 'MATTER'
              AND permission_group.matter_id = NEW.matter_id)
          OR (NEW.layer = 'FIRM_KNOWLEDGE'
              AND permission_group.scope_kind = 'FIRM')
      );
    IF NEW.layer IN ('CASE_LONG_TERM', 'FIRM_KNOWLEDGE')
       AND incompatible_group_count > 0 THEN
        RAISE EXCEPTION 'memory version contains an incompatible permission group';
    END IF;
    IF NEW.layer = 'FIRM_KNOWLEDGE' AND (
        EXISTS (
            SELECT 1
            FROM case_agent_memory_record_groups record_group
            WHERE record_group.record_id = NEW.record_id
              AND record_group.record_version = NEW.record_version
              AND record_group.firm_id = NEW.firm_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM case_agent_knowledge_publication_groups publication_group
                  WHERE publication_group.publication_id = NEW.publication_id
                    AND publication_group.firm_id = NEW.firm_id
                    AND publication_group.group_id = record_group.group_id
              )
        )
        OR EXISTS (
            SELECT 1
            FROM case_agent_knowledge_publication_groups publication_group
            WHERE publication_group.publication_id = NEW.publication_id
              AND publication_group.firm_id = NEW.firm_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM case_agent_memory_record_groups record_group
                  WHERE record_group.record_id = NEW.record_id
                    AND record_group.record_version = NEW.record_version
                    AND record_group.firm_id = NEW.firm_id
                    AND record_group.group_id = publication_group.group_id
              )
        )
    ) THEN
        RAISE EXCEPTION 'firm memory groups must exactly match its approved publication';
    END IF;
    IF NEW.layer IN ('RUN_WORKING', 'LAWYER_PERSONAL', 'PUBLIC_LEGAL')
       AND EXISTS (
           SELECT 1 FROM case_agent_memory_record_groups record_group
           WHERE record_group.record_id = NEW.record_id
             AND record_group.record_version = NEW.record_version
       ) THEN
        RAISE EXCEPTION 'this memory layer cannot carry permission groups';
    END IF;
    RETURN NEW;
END;
$$;
CREATE CONSTRAINT TRIGGER case_agent_memory_dependencies_guard
    AFTER INSERT ON case_agent_memory_record_versions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_memory_dependencies();

CREATE FUNCTION guard_case_agent_memory_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    version_status text;
    version_content_hash char(64);
    version_provenance_hash char(64);
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'memory head transition is invalid';
    END IF;
    IF TG_OP = 'INSERT' AND NEW.current_version <> 1 THEN
        RAISE EXCEPTION 'the first memory head version must be one';
    END IF;
    IF TG_OP = 'UPDATE' AND (
       NEW.firm_id <> OLD.firm_id OR NEW.record_id <> OLD.record_id
       OR NEW.current_version <> OLD.current_version + 1) THEN
        RAISE EXCEPTION 'memory head transition is invalid';
    END IF;
    SELECT status, content_sha256, provenance_hash
      INTO version_status, version_content_hash, version_provenance_hash
    FROM case_agent_memory_record_versions version
    WHERE version.record_id = NEW.record_id
      AND version.record_version = NEW.current_version
      AND version.firm_id = NEW.firm_id;
    IF version_status IS NULL OR version_status <> NEW.current_status
       OR version_content_hash <> NEW.current_content_sha256
       OR version_provenance_hash <> NEW.current_provenance_hash THEN
        RAISE EXCEPTION 'memory head does not match its authoritative version';
    END IF;
    IF NEW.current_status IN ('REVOKED', 'DELETED') AND NOT EXISTS (
        SELECT 1 FROM case_agent_memory_tombstones tombstone
        WHERE tombstone.record_id = NEW.record_id
          AND tombstone.firm_id = NEW.firm_id
          AND tombstone.blocked_through_version >= NEW.current_version
          AND tombstone.terminal_status = NEW.current_status
    ) THEN
        RAISE EXCEPTION 'memory denial/tombstone must be durable before revocation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_memory_record_heads_guard
    BEFORE INSERT OR UPDATE OR DELETE ON case_agent_memory_record_heads
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_memory_head();

CREATE FUNCTION prohibit_case_agent_memory_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent memory history is append-only';
END;
$$;

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_memory_permission_groups',
        'case_agent_memory_group_memberships',
        'case_agent_memory_group_member_denials',
        'case_agent_published_knowledge_objects',
        'case_agent_knowledge_publication_reviews',
        'case_agent_knowledge_publications',
        'case_agent_knowledge_publication_groups',
        'case_agent_public_legal_authority_registrations',
        'case_agent_public_legal_authority_sources',
        'case_agent_memory_record_versions',
        'case_agent_memory_record_groups',
        'case_agent_memory_source_refs',
        'case_agent_memory_tombstones',
        'case_agent_memory_access_denials',
        'case_agent_memory_retrieval_audits',
        'case_agent_memory_checkpoints'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_memory_history_mutation()',
            table_name || '_append_only', table_name
        );
    END LOOP;
END;
$$;

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_memory_permission_groups',
        'case_agent_memory_group_memberships',
        'case_agent_memory_group_member_denials',
        'case_agent_published_knowledge_objects',
        'case_agent_knowledge_publication_reviews',
        'case_agent_knowledge_publications',
        'case_agent_knowledge_publication_groups',
        'case_agent_public_legal_authority_registrations',
        'case_agent_public_legal_authority_sources',
        'case_agent_memory_record_versions',
        'case_agent_memory_record_groups',
        'case_agent_memory_source_refs',
        'case_agent_memory_tombstones',
        'case_agent_memory_access_denials',
        'case_agent_memory_record_heads',
        'case_agent_memory_retrieval_audits',
        'case_agent_memory_checkpoints'
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

COMMIT;
