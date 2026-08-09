-- Firm-scoped official legal source snapshots, approved rule versions and case bundle segments.
-- Apply after 0005_formal_calculations.sql.

BEGIN;

CREATE TABLE official_legal_source_snapshots (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    source_id text NOT NULL CHECK (length(trim(source_id)) > 0),
    publisher text NOT NULL CHECK (length(trim(publisher)) > 0),
    authority_level text NOT NULL CHECK (authority_level IN ('PRIMARY_LAW', 'JUDICIAL_INTERPRETATION', 'OFFICIAL_RATE_DATA', 'OFFICIAL_CASE')),
    official_url text NOT NULL CHECK (official_url ~ '^https://'),
    provision_locator text NOT NULL CHECK (length(trim(provision_locator)) > 0),
    retrieved_at timestamptz NOT NULL,
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    content_media_type text NOT NULL CHECK (length(trim(content_media_type)) > 0),
    storage_object_key text NOT NULL CHECK (storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'),
    verification_status text NOT NULL CHECK (verification_status IN ('VERIFIED', 'SUPERSEDED')),
    license_status text NOT NULL CHECK (license_status IN ('ACTIVE', 'REVOKED')),
    verified_by uuid NOT NULL REFERENCES users(user_id),
    verification_hash char(64) NOT NULL CHECK (verification_hash ~ '^[0-9a-f]{64}$'),
    supersedes_snapshot_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, firm_id),
    UNIQUE (snapshot_id, firm_id, content_sha256),
    UNIQUE (firm_id, source_id, content_sha256),
    FOREIGN KEY (verified_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (supersedes_snapshot_id, firm_id)
        REFERENCES official_legal_source_snapshots(snapshot_id, firm_id)
);

CREATE TABLE legal_rule_versions (
    rule_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    rule_id text NOT NULL CHECK (length(trim(rule_id)) > 0),
    rule_version text NOT NULL CHECK (length(trim(rule_version)) > 0),
    issue_key text NOT NULL CHECK (length(trim(issue_key)) > 0),
    source_snapshot_id uuid NOT NULL,
    parameter_source_snapshot_id uuid,
    parameter_evidence_locator text,
    effective_from date NOT NULL,
    effective_to date,
    trigger_event_kind text NOT NULL CHECK (trigger_event_kind IN ('CONTRACT_SIGNED', 'DISBURSEMENT', 'PAYMENT', 'DEFAULT', 'CLAIM_FILED', 'CASE_ACCEPTED', 'JUDGMENT')),
    formula_kind text NOT NULL CHECK (formula_kind IN ('FIXED_ANNUAL_RATE', 'LPR_MULTIPLE', 'NO_INTEREST')),
    base_annual_rate numeric(18,12),
    rate_multiplier numeric(18,12),
    derived_annual_rate numeric(18,12) NOT NULL CHECK (derived_annual_rate >= 0 AND derived_annual_rate <= 1),
    required_fact_keys jsonb NOT NULL CHECK (jsonb_typeof(required_fact_keys) = 'array'),
    transition_rule_versions jsonb NOT NULL CHECK (jsonb_typeof(transition_rule_versions) = 'array'),
    conflict_set text,
    priority integer NOT NULL,
    status text NOT NULL CHECK (status IN ('APPROVED', 'SUPERSEDED')),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, firm_id),
    UNIQUE (firm_id, rule_id, rule_version),
    CHECK (effective_to IS NULL OR effective_from < effective_to),
    CHECK (
        (formula_kind = 'FIXED_ANNUAL_RATE' AND base_annual_rate IS NOT NULL AND rate_multiplier IS NULL
            AND parameter_source_snapshot_id IS NULL AND parameter_evidence_locator IS NULL
            AND derived_annual_rate = base_annual_rate)
        OR (formula_kind = 'LPR_MULTIPLE' AND base_annual_rate IS NOT NULL AND rate_multiplier IS NOT NULL
            AND rate_multiplier > 0 AND parameter_source_snapshot_id IS NOT NULL
            AND parameter_evidence_locator IS NOT NULL
            AND length(trim(parameter_evidence_locator)) > 0
            AND derived_annual_rate = base_annual_rate * rate_multiplier)
        OR (formula_kind = 'NO_INTEREST' AND base_annual_rate IS NULL AND rate_multiplier IS NULL
            AND parameter_source_snapshot_id IS NULL AND parameter_evidence_locator IS NULL
            AND derived_annual_rate = 0)
    ),
    FOREIGN KEY (source_snapshot_id, firm_id)
        REFERENCES official_legal_source_snapshots(snapshot_id, firm_id),
    FOREIGN KEY (parameter_source_snapshot_id, firm_id)
        REFERENCES official_legal_source_snapshots(snapshot_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_legal_events (
    legal_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    event_kind text NOT NULL CHECK (event_kind IN ('CONTRACT_SIGNED', 'DISBURSEMENT', 'PAYMENT', 'DEFAULT', 'CLAIM_FILED', 'CASE_ACCEPTED', 'JUDGMENT')),
    local_date date NOT NULL,
    status text NOT NULL CHECK (status IN ('APPROVED', 'STALE')),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz NOT NULL,
    stale_at timestamptz,
    stale_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (legal_event_id, firm_id, matter_id),
    CHECK (
        (status = 'APPROVED' AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'STALE' AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_legal_event_evidence_pages (
    legal_event_id uuid NOT NULL,
    evidence_page_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (legal_event_id, evidence_page_id),
    FOREIGN KEY (legal_event_id, firm_id, matter_id)
        REFERENCES case_legal_events(legal_event_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id)
);

-- A rule may depend on normalized case conditions, but those condition keys
-- must never be asserted by the browser or inferred directly by a model.  A
-- lead lawyer binds each key to a confirmed, evidence-linked case fact.
CREATE TABLE case_legal_fact_bindings (
    binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    fact_key text NOT NULL CHECK (length(trim(fact_key)) > 0),
    fact_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('APPROVED', 'STALE')),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz NOT NULL,
    stale_at timestamptz,
    stale_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (binding_id, firm_id, matter_id),
    CHECK (
        (status = 'APPROVED' AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'STALE' AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (fact_id, firm_id, matter_id)
        REFERENCES case_facts(fact_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_legal_bundle_segments (
    bundle_id uuid NOT NULL,
    segment_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    issue_key text NOT NULL CHECK (length(trim(issue_key)) > 0),
    rule_version_id uuid NOT NULL,
    rule_version text NOT NULL CHECK (length(trim(rule_version)) > 0),
    source_snapshot_id uuid NOT NULL,
    source_sha256 char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    parameter_source_snapshot_id uuid,
    parameter_source_sha256 char(64) CHECK (parameter_source_sha256 IS NULL OR parameter_source_sha256 ~ '^[0-9a-f]{64}$'),
    parameter_evidence_locator text,
    trigger_event_id uuid NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    annual_rate numeric(18,12) NOT NULL CHECK (annual_rate >= 0 AND annual_rate <= 1),
    applicability_anchor text NOT NULL CHECK (length(trim(applicability_anchor)) > 0),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bundle_id, segment_id),
    UNIQUE (bundle_id, segment_id, firm_id, matter_id),
    UNIQUE (bundle_id, start_date),
    CHECK (start_date < end_date),
    CHECK (
        (parameter_source_snapshot_id IS NULL AND parameter_source_sha256 IS NULL AND parameter_evidence_locator IS NULL)
        OR (parameter_source_snapshot_id IS NOT NULL AND parameter_source_sha256 IS NOT NULL
            AND parameter_evidence_locator IS NOT NULL AND length(trim(parameter_evidence_locator)) > 0)
    ),
    FOREIGN KEY (bundle_id, firm_id, matter_id)
        REFERENCES case_legal_bundles(bundle_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (rule_version_id, firm_id)
        REFERENCES legal_rule_versions(rule_version_id, firm_id),
    FOREIGN KEY (source_snapshot_id, firm_id, source_sha256)
        REFERENCES official_legal_source_snapshots(snapshot_id, firm_id, content_sha256),
    FOREIGN KEY (parameter_source_snapshot_id, firm_id, parameter_source_sha256)
        REFERENCES official_legal_source_snapshots(snapshot_id, firm_id, content_sha256),
    FOREIGN KEY (trigger_event_id, firm_id, matter_id)
        REFERENCES case_legal_events(legal_event_id, firm_id, matter_id)
);

CREATE INDEX official_legal_source_snapshots_source_idx
    ON official_legal_source_snapshots (firm_id, source_id, retrieved_at DESC);
CREATE INDEX legal_rule_versions_issue_idx
    ON legal_rule_versions (firm_id, issue_key, status, effective_from DESC);
CREATE INDEX case_legal_events_matter_idx
    ON case_legal_events (matter_id, status, event_kind, local_date);
CREATE INDEX case_legal_event_evidence_pages_matter_idx
    ON case_legal_event_evidence_pages (matter_id, evidence_page_id);
CREATE UNIQUE INDEX case_legal_fact_bindings_current_idx
    ON case_legal_fact_bindings (matter_id, fact_key)
    WHERE status = 'APPROVED';
CREATE INDEX case_legal_fact_bindings_fact_idx
    ON case_legal_fact_bindings (matter_id, fact_id, status);
CREATE INDEX case_legal_bundle_segments_bundle_idx
    ON case_legal_bundle_segments (matter_id, bundle_id, start_date);

ALTER TABLE official_legal_source_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE official_legal_source_snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE legal_rule_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_rule_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE case_legal_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_legal_events FORCE ROW LEVEL SECURITY;
ALTER TABLE case_legal_event_evidence_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_legal_event_evidence_pages FORCE ROW LEVEL SECURITY;
ALTER TABLE case_legal_fact_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_legal_fact_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE case_legal_bundle_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_legal_bundle_segments FORCE ROW LEVEL SECURITY;

CREATE POLICY official_legal_source_snapshots_firm_isolation ON official_legal_source_snapshots
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY legal_rule_versions_firm_isolation ON legal_rule_versions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_legal_events_firm_isolation ON case_legal_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_legal_event_evidence_pages_firm_isolation ON case_legal_event_evidence_pages
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_legal_fact_bindings_firm_isolation ON case_legal_fact_bindings
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_legal_bundle_segments_firm_isolation ON case_legal_bundle_segments
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
