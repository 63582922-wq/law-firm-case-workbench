-- Approved case legal bundles and deterministic formal calculation lineage.
-- Apply after 0004_evidence_derivative_runs.sql.

BEGIN;

CREATE TABLE case_legal_bundles (
    bundle_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('APPROVED', 'STALE')),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    bundle_hash char(64) NOT NULL CHECK (bundle_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz NOT NULL,
    stale_at timestamptz,
    stale_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bundle_id, firm_id, matter_id),
    UNIQUE (matter_id, version),
    CHECK (
        (status = 'APPROVED' AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'STALE' AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE UNIQUE INDEX case_legal_bundles_one_approved_per_matter
    ON case_legal_bundles (matter_id) WHERE status = 'APPROVED';

CREATE TABLE case_legal_bundle_rule_versions (
    bundle_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    issue_key text NOT NULL CHECK (length(trim(issue_key)) > 0),
    rule_version text NOT NULL CHECK (length(trim(rule_version)) > 0),
    source_snapshot_id text NOT NULL CHECK (length(trim(source_snapshot_id)) > 0),
    source_sha256 char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    trigger_event_id text NOT NULL CHECK (length(trim(trigger_event_id)) > 0),
    trigger_date date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bundle_id, issue_key, rule_version),
    UNIQUE (bundle_id, rule_version),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (bundle_id, firm_id, matter_id)
        REFERENCES case_legal_bundles(bundle_id, firm_id, matter_id) ON DELETE CASCADE
);

CREATE TABLE calculation_scenarios (
    scenario_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    obligation_id text NOT NULL CHECK (length(trim(obligation_id)) > 0),
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('APPROVED', 'STALE')),
    start_date date NOT NULL,
    end_date date NOT NULL,
    currency char(3) NOT NULL CHECK (currency = 'CNY'),
    allocation_policy text NOT NULL CHECK (allocation_policy IN ('INTEREST_THEN_PRINCIPAL', 'PRINCIPAL_THEN_INTEREST')),
    legal_bundle_id uuid NOT NULL,
    legal_bundle_hash char(64) NOT NULL CHECK (legal_bundle_hash ~ '^[0-9a-f]{64}$'),
    transaction_snapshot_hash char(64) NOT NULL CHECK (transaction_snapshot_hash ~ '^[0-9a-f]{64}$'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz NOT NULL DEFAULT now(),
    stale_at timestamptz,
    stale_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scenario_id, firm_id, matter_id),
    UNIQUE (matter_id, obligation_id, version),
    CHECK (start_date < end_date),
    CHECK (
        (status = 'APPROVED' AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'STALE' AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (legal_bundle_id, firm_id, matter_id)
        REFERENCES case_legal_bundles(bundle_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE UNIQUE INDEX calculation_scenarios_one_approved_per_obligation
    ON calculation_scenarios (matter_id, obligation_id) WHERE status = 'APPROVED';

CREATE TABLE calculation_scenario_events (
    scenario_id uuid NOT NULL,
    transaction_id uuid NOT NULL,
    classification_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    effective_date date NOT NULL,
    same_day_sequence integer NOT NULL CHECK (same_day_sequence > 0),
    event_kind text NOT NULL CHECK (event_kind IN ('DISBURSEMENT', 'PAYMENT')),
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    currency char(3) NOT NULL CHECK (currency = 'CNY'),
    payment_application text NOT NULL CHECK (payment_application IN ('BY_POLICY', 'INTEREST_ONLY', 'PRINCIPAL_ONLY')),
    evidence_ids jsonb NOT NULL CHECK (jsonb_typeof(evidence_ids) = 'array' AND jsonb_array_length(evidence_ids) > 0),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scenario_id, transaction_id),
    UNIQUE (scenario_id, effective_date, same_day_sequence),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (scenario_id, firm_id, matter_id)
        REFERENCES calculation_scenarios(scenario_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (transaction_id, firm_id, matter_id)
        REFERENCES case_transactions(transaction_id, firm_id, matter_id),
    FOREIGN KEY (classification_id, firm_id, matter_id)
        REFERENCES case_payment_classifications(classification_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE calculation_rule_segments (
    segment_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    annual_rate numeric(18,12) NOT NULL CHECK (annual_rate >= 0 AND annual_rate <= 1),
    source_rule_version text NOT NULL CHECK (length(trim(source_rule_version)) > 0),
    applicability_anchor text NOT NULL CHECK (length(trim(applicability_anchor)) > 0),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scenario_id, segment_id),
    UNIQUE (scenario_id, segment_id, firm_id, matter_id),
    UNIQUE (scenario_id, start_date),
    CHECK (start_date < end_date),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (scenario_id, firm_id, matter_id)
        REFERENCES calculation_scenarios(scenario_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE calculation_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    scenario_id uuid NOT NULL,
    scenario_version integer NOT NULL CHECK (scenario_version > 0),
    status text NOT NULL CHECK (status IN ('VERIFIED', 'STALE')),
    engine_version text NOT NULL CHECK (length(trim(engine_version)) > 0),
    legal_bundle_id uuid NOT NULL,
    legal_bundle_hash char(64) NOT NULL CHECK (legal_bundle_hash ~ '^[0-9a-f]{64}$'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    output_hash char(64) NOT NULL CHECK (output_hash ~ '^[0-9a-f]{64}$'),
    independent_check_hash char(64) NOT NULL CHECK (independent_check_hash ~ '^[0-9a-f]{64}$'),
    total_interest_accrued numeric(18,2) NOT NULL,
    total_interest_paid numeric(18,2) NOT NULL,
    remaining_principal numeric(18,2) NOT NULL,
    remaining_unpaid_interest numeric(18,2) NOT NULL,
    unapplied_payments numeric(18,2) NOT NULL,
    generated_at timestamptz NOT NULL,
    stale_at timestamptz,
    stale_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, firm_id, matter_id),
    UNIQUE (scenario_id, input_hash, output_hash),
    CHECK (
        (status = 'VERIFIED' AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'STALE' AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (scenario_id, firm_id, matter_id)
        REFERENCES calculation_scenarios(scenario_id, firm_id, matter_id),
    FOREIGN KEY (legal_bundle_id, firm_id, matter_id)
        REFERENCES case_legal_bundles(bundle_id, firm_id, matter_id)
);

CREATE UNIQUE INDEX calculation_runs_one_verified_per_scenario
    ON calculation_runs (scenario_id) WHERE status = 'VERIFIED';

CREATE TABLE calculation_line_items (
    run_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    line_sequence integer NOT NULL CHECK (line_sequence > 0),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    opening_principal numeric(18,2) NOT NULL,
    annual_rate numeric(18,12) NOT NULL,
    day_count integer NOT NULL CHECK (day_count > 0),
    accrued_interest numeric(18,2) NOT NULL,
    closing_principal numeric(18,2) NOT NULL,
    accrued_unpaid_interest numeric(18,2) NOT NULL,
    rule_segment_id uuid NOT NULL,
    source_rule_version text NOT NULL,
    evidence_ids jsonb NOT NULL CHECK (jsonb_typeof(evidence_ids) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, line_sequence),
    CHECK (period_start < period_end),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES calculation_runs(run_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (scenario_id, rule_segment_id, firm_id, matter_id)
        REFERENCES calculation_rule_segments(scenario_id, segment_id, firm_id, matter_id)
);

CREATE TABLE calculation_payment_allocations (
    run_id uuid NOT NULL,
    allocation_sequence integer NOT NULL CHECK (allocation_sequence > 0),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    payment_event_id text NOT NULL CHECK (length(trim(payment_event_id)) > 0),
    effective_date date NOT NULL,
    payment_amount numeric(18,2) NOT NULL,
    allocated_interest numeric(18,2) NOT NULL,
    allocated_principal numeric(18,2) NOT NULL,
    unapplied_amount numeric(18,2) NOT NULL,
    payment_application text NOT NULL CHECK (payment_application IN ('BY_POLICY', 'INTEREST_ONLY', 'PRINCIPAL_ONLY')),
    evidence_ids jsonb NOT NULL CHECK (jsonb_typeof(evidence_ids) = 'array' AND jsonb_array_length(evidence_ids) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, allocation_sequence),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES calculation_runs(run_id, firm_id, matter_id) ON DELETE CASCADE
);

CREATE INDEX calculation_scenarios_matter_status_idx ON calculation_scenarios (matter_id, status, created_at DESC);
CREATE INDEX calculation_runs_matter_status_idx ON calculation_runs (matter_id, status, created_at DESC);

ALTER TABLE case_legal_bundles ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_legal_bundles FORCE ROW LEVEL SECURITY;
ALTER TABLE case_legal_bundle_rule_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_legal_bundle_rule_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE calculation_scenarios ENABLE ROW LEVEL SECURITY;
ALTER TABLE calculation_scenarios FORCE ROW LEVEL SECURITY;
ALTER TABLE calculation_scenario_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE calculation_scenario_events FORCE ROW LEVEL SECURITY;
ALTER TABLE calculation_rule_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE calculation_rule_segments FORCE ROW LEVEL SECURITY;
ALTER TABLE calculation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE calculation_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE calculation_line_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE calculation_line_items FORCE ROW LEVEL SECURITY;
ALTER TABLE calculation_payment_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE calculation_payment_allocations FORCE ROW LEVEL SECURITY;

CREATE POLICY case_legal_bundles_firm_isolation ON case_legal_bundles USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_legal_bundle_rule_versions_firm_isolation ON case_legal_bundle_rule_versions USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY calculation_scenarios_firm_isolation ON calculation_scenarios USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY calculation_scenario_events_firm_isolation ON calculation_scenario_events USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY calculation_rule_segments_firm_isolation ON calculation_rule_segments USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY calculation_runs_firm_isolation ON calculation_runs USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY calculation_line_items_firm_isolation ON calculation_line_items USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY calculation_payment_allocations_firm_isolation ON calculation_payment_allocations USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
