-- Case-ledger persistence boundary. PostgreSQL 16+; contains no case data.
-- Apply only after 0001_core.sql. Every query must SET LOCAL app.firm_id.

BEGIN;

CREATE TABLE case_facts (
    fact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    original_text text NOT NULL CHECK (length(trim(original_text)) > 0),
    origin text NOT NULL CHECK (origin IN ('PLAINTIFF_PLEADING', 'DEFENDANT_STATEMENT', 'AGENT_CANDIDATE', 'ASSISTANT_ENTRY')),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'CONFIRMED', 'DISPUTED', 'DENIED', 'INVALIDATED')),
    evidence_links jsonb NOT NULL CHECK (jsonb_typeof(evidence_links) = 'array' AND jsonb_array_length(evidence_links) > 0),
    decision_hash char(64) CHECK (decision_hash IS NULL OR decision_hash ~ '^[0-9a-f]{64}$'),
    decided_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fact_id, firm_id),
    UNIQUE (fact_id, firm_id, matter_id),
    CHECK (
        (status = 'CANDIDATE' AND decision_hash IS NULL AND decided_by IS NULL)
        OR (status <> 'CANDIDATE' AND decision_hash IS NOT NULL AND decided_by IS NOT NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (decided_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_claims (
    claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    original_claim_text text NOT NULL CHECK (length(trim(original_claim_text)) > 0),
    claimed_amount numeric(18,2),
    currency char(3),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'CONFIRMED_SCOPE', 'INVALIDATED')),
    evidence_links jsonb NOT NULL CHECK (jsonb_typeof(evidence_links) = 'array' AND jsonb_array_length(evidence_links) > 0),
    confirmation_hash char(64) CHECK (confirmation_hash IS NULL OR confirmation_hash ~ '^[0-9a-f]{64}$'),
    confirmed_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (claim_id, firm_id),
    UNIQUE (claim_id, firm_id, matter_id),
    CHECK ((claimed_amount IS NULL AND currency IS NULL) OR (claimed_amount >= 0 AND currency ~ '^[A-Z]{3}$')),
    CHECK (
        (status = 'CANDIDATE' AND confirmation_hash IS NULL AND confirmed_by IS NULL)
        OR (status <> 'CANDIDATE' AND confirmation_hash IS NOT NULL AND confirmed_by IS NOT NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_claim_responses (
    claim_response_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    claim_id uuid NOT NULL,
    position text NOT NULL CHECK (position IN ('ADMIT', 'PARTIALLY_ADMIT', 'DISPUTE', 'OUTSIDE_SCOPE')),
    partial_amount numeric(18,2),
    currency char(3),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((position = 'PARTIALLY_ADMIT' AND partial_amount >= 0 AND currency ~ '^[A-Z]{3}$') OR (position <> 'PARTIALLY_ADMIT' AND partial_amount IS NULL AND currency IS NULL)),
    UNIQUE (matter_id, claim_id),
    UNIQUE (claim_response_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (claim_id, firm_id, matter_id) REFERENCES case_claims(claim_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

-- Normalized links prevent a response from referencing a fact in another
-- matter or tenant. The command service requires at least one confirmed fact.
CREATE TABLE case_claim_response_facts (
    claim_response_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_response_id, fact_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (claim_response_id, firm_id, matter_id)
        REFERENCES case_claim_responses(claim_response_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id, firm_id, matter_id)
        REFERENCES case_facts(fact_id, firm_id, matter_id)
);

CREATE TABLE case_dispute_issues (
    issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    question text NOT NULL CHECK (length(trim(question)) > 0),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'CONFIRMED', 'INVALIDATED')),
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (issue_id, firm_id, matter_id),
    CHECK (
        (status = 'CONFIRMED' AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status <> 'CONFIRMED' AND approval_hash IS NULL AND approved_by IS NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_dispute_issue_claims (
    issue_id uuid NOT NULL,
    claim_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (issue_id, claim_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (issue_id, firm_id, matter_id)
        REFERENCES case_dispute_issues(issue_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (claim_id, firm_id, matter_id)
        REFERENCES case_claims(claim_id, firm_id, matter_id)
);

CREATE TABLE case_dispute_issue_facts (
    issue_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (issue_id, fact_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (issue_id, firm_id, matter_id)
        REFERENCES case_dispute_issues(issue_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id, firm_id, matter_id)
        REFERENCES case_facts(fact_id, firm_id, matter_id)
);

CREATE TABLE case_transactions (
    transaction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    local_date date,
    date_precision text NOT NULL CHECK (date_precision IN ('EXACT_DATE', 'MONTH_ONLY', 'YEAR_ONLY', 'UNKNOWN')),
    amount numeric(18,6) NOT NULL CHECK (amount > 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    direction text NOT NULL CHECK (direction IN ('OUTGOING', 'INCOMING', 'UNKNOWN')),
    payer_label text,
    payee_label text,
    channel text NOT NULL CHECK (channel IN ('WECHAT', 'BANK', 'CASH', 'CHAT_RECORD', 'LOAN_INSTRUMENT', 'OTHER')),
    transaction_reference text,
    evidence_links jsonb NOT NULL CHECK (jsonb_typeof(evidence_links) = 'array' AND jsonb_array_length(evidence_links) > 0),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'CONFIRMED', 'INVALIDATED')),
    confirmation_hash char(64) CHECK (confirmation_hash IS NULL OR confirmation_hash ~ '^[0-9a-f]{64}$'),
    confirmed_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transaction_id, firm_id),
    UNIQUE (transaction_id, firm_id, matter_id),
    CHECK ((date_precision = 'EXACT_DATE' AND local_date IS NOT NULL) OR (date_precision <> 'EXACT_DATE' AND local_date IS NULL)),
    CHECK (
        (status = 'CANDIDATE' AND confirmation_hash IS NULL AND confirmed_by IS NULL)
        OR (status <> 'CANDIDATE' AND confirmation_hash IS NOT NULL AND confirmed_by IS NOT NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_payment_classifications (
    classification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    transaction_id uuid NOT NULL REFERENCES case_transactions(transaction_id),
    origin text NOT NULL CHECK (origin IN ('PLAINTIFF_PLEADING', 'DEFENDANT_STATEMENT', 'AGENT_CANDIDATE', 'ASSISTANT_ENTRY')),
    nature text NOT NULL CHECK (nature IN ('DISBURSEMENT', 'REPAYMENT_UNSPECIFIED', 'INTEREST_PAYMENT', 'PRINCIPAL_REPAYMENT', 'REFUND', 'FEE', 'UNRELATED')),
    same_day_sequence integer CHECK (same_day_sequence IS NULL OR same_day_sequence > 0),
    evidence_links jsonb NOT NULL CHECK (jsonb_typeof(evidence_links) = 'array' AND jsonb_array_length(evidence_links) > 0),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'INVALIDATED')),
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (classification_id, firm_id, matter_id),
    CHECK (
        (status = 'APPROVED' AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status <> 'APPROVED' AND approval_hash IS NULL AND approved_by IS NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (transaction_id, firm_id, matter_id)
        REFERENCES case_transactions(transaction_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

-- Allocation rows are normalized so currency and per-obligation uniqueness are
-- queryable. The command service verifies that financial allocations equal the
-- source transaction amount in the same advisory-locked transaction.
CREATE TABLE case_payment_allocations (
    classification_id uuid NOT NULL,
    obligation_id text NOT NULL CHECK (length(trim(obligation_id)) > 0),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    amount numeric(18,6) NOT NULL CHECK (amount > 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (classification_id, obligation_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (classification_id, firm_id, matter_id)
        REFERENCES case_payment_classifications(classification_id, firm_id, matter_id) ON DELETE CASCADE
);

CREATE TABLE case_transaction_duplicate_groups (
    duplicate_group_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'SAME_ECONOMIC_EVENT', 'DISTINCT_EVENTS', 'INVALIDATED')),
    canonical_transaction_id uuid,
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (duplicate_group_id, firm_id, matter_id),
    CHECK (
        (status = 'CANDIDATE' AND canonical_transaction_id IS NULL AND approval_hash IS NULL AND approved_by IS NULL)
        OR (status = 'SAME_ECONOMIC_EVENT' AND canonical_transaction_id IS NOT NULL AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status = 'DISTINCT_EVENTS' AND canonical_transaction_id IS NULL AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status = 'INVALIDATED' AND canonical_transaction_id IS NULL AND approval_hash IS NULL AND approved_by IS NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (canonical_transaction_id, firm_id, matter_id)
        REFERENCES case_transactions(transaction_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_transaction_duplicate_members (
    duplicate_group_id uuid NOT NULL,
    transaction_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (duplicate_group_id, transaction_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (duplicate_group_id, firm_id, matter_id)
        REFERENCES case_transaction_duplicate_groups(duplicate_group_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (transaction_id, firm_id, matter_id)
        REFERENCES case_transactions(transaction_id, firm_id, matter_id)
);

CREATE UNIQUE INDEX case_payment_classifications_one_approved_per_transaction
    ON case_payment_classifications (matter_id, transaction_id)
    WHERE status = 'APPROVED';

CREATE INDEX case_facts_matter_status_idx ON case_facts (matter_id, status, created_at);
CREATE INDEX case_claims_matter_status_idx ON case_claims (matter_id, status, created_at);
CREATE INDEX case_dispute_issues_matter_status_idx ON case_dispute_issues (matter_id, status, created_at);
CREATE INDEX case_transactions_matter_date_idx ON case_transactions (matter_id, local_date, transaction_id);
CREATE INDEX case_payment_classifications_matter_status_idx ON case_payment_classifications (matter_id, status, created_at);
CREATE INDEX case_duplicate_groups_matter_status_idx ON case_transaction_duplicate_groups (matter_id, status, created_at);

ALTER TABLE case_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_facts FORCE ROW LEVEL SECURITY;
ALTER TABLE case_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_claims FORCE ROW LEVEL SECURITY;
ALTER TABLE case_claim_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_claim_responses FORCE ROW LEVEL SECURITY;
ALTER TABLE case_claim_response_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_claim_response_facts FORCE ROW LEVEL SECURITY;
ALTER TABLE case_dispute_issues ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_dispute_issues FORCE ROW LEVEL SECURITY;
ALTER TABLE case_dispute_issue_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_dispute_issue_claims FORCE ROW LEVEL SECURITY;
ALTER TABLE case_dispute_issue_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_dispute_issue_facts FORCE ROW LEVEL SECURITY;
ALTER TABLE case_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_transactions FORCE ROW LEVEL SECURITY;
ALTER TABLE case_payment_classifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_payment_classifications FORCE ROW LEVEL SECURITY;
ALTER TABLE case_payment_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_payment_allocations FORCE ROW LEVEL SECURITY;
ALTER TABLE case_transaction_duplicate_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_transaction_duplicate_groups FORCE ROW LEVEL SECURITY;
ALTER TABLE case_transaction_duplicate_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_transaction_duplicate_members FORCE ROW LEVEL SECURITY;

CREATE POLICY case_facts_firm_isolation ON case_facts USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_claims_firm_isolation ON case_claims USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_claim_responses_firm_isolation ON case_claim_responses USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_claim_response_facts_firm_isolation ON case_claim_response_facts USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_dispute_issues_firm_isolation ON case_dispute_issues USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_dispute_issue_claims_firm_isolation ON case_dispute_issue_claims USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_dispute_issue_facts_firm_isolation ON case_dispute_issue_facts USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_transactions_firm_isolation ON case_transactions USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_payment_classifications_firm_isolation ON case_payment_classifications USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_payment_allocations_firm_isolation ON case_payment_allocations USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_transaction_duplicate_groups_firm_isolation ON case_transaction_duplicate_groups USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_transaction_duplicate_members_firm_isolation ON case_transaction_duplicate_members USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
