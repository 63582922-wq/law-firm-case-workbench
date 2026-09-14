-- Persistent control plane for the lawyer case Agent OS.
--
-- The event log is authoritative.  Mutable run/task/attempt rows are only
-- operational projections rebuilt from the exact case_agent_supervisor
-- reducer; they are never a second workflow state machine.

BEGIN;

CREATE TABLE case_agent_goals (
    goal_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    objective text NOT NULL CHECK (length(trim(objective)) BETWEEN 1 AND 4000),
    success_criteria jsonb NOT NULL CHECK (jsonb_typeof(success_criteria) = 'array'),
    constraints jsonb NOT NULL CHECK (jsonb_typeof(constraints) = 'array'),
    requested_by uuid NOT NULL,
    goal_hash char(64) NOT NULL CHECK (goal_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (goal_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (requested_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_agent_runs (
    run_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN (
        'CREATED', 'PLANNING', 'WAITING_APPROVAL', 'EXECUTING', 'WAITING_INPUT',
        'RECONCILIATION_REQUIRED', 'VERIFYING', 'READY_FOR_REVIEW', 'COMPLETED',
        'PAUSED', 'STALE', 'CANCELLED', 'FAILED'
    )),
    current_event_version bigint NOT NULL CHECK (current_event_version > 0),
    snapshot_matter_version integer NOT NULL CHECK (snapshot_matter_version > 0),
    snapshot_schema_version text NOT NULL CHECK (
        length(trim(snapshot_schema_version)) BETWEEN 1 AND 200
    ),
    snapshot_hash char(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    run_budget jsonb NOT NULL CHECK (jsonb_typeof(run_budget) = 'object'),
    current_graph_id uuid,
    current_graph_version bigint CHECK (current_graph_version > 0),
    current_graph_hash char(64) CHECK (current_graph_hash ~ '^[0-9a-f]{64}$'),
    projection_hash char(64) NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{64}$'),
    paused_from text,
    is_stale boolean NOT NULL DEFAULT false,
    is_cancelled boolean NOT NULL DEFAULT false,
    verification_hash char(64) CHECK (verification_hash ~ '^[0-9a-f]{64}$'),
    failure_code text,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (goal_id, firm_id, matter_id)
        REFERENCES case_agent_goals(goal_id, firm_id, matter_id),
    FOREIGN KEY (created_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (current_graph_id IS NULL AND current_graph_version IS NULL AND current_graph_hash IS NULL)
        OR (current_graph_id IS NOT NULL AND current_graph_version IS NOT NULL
            AND current_graph_hash IS NOT NULL)
    )
);

CREATE TABLE case_agent_events (
    event_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    event_type text NOT NULL CHECK (event_type IN (
        'RUN_CREATED', 'PLANNING_STARTED', 'PLANNING_FAILED',
        'PLANNING_RESULT_UNKNOWN', 'TASK_GRAPH_ACCEPTED', 'APPROVAL_GRANTED',
        'TASK_STARTED', 'TASK_RESULT_RECORDED', 'CASE_SNAPSHOT_CHANGED',
        'RUN_PAUSED', 'RUN_RESUMED', 'RUN_CANCELLED', 'VERIFICATION_STARTED',
        'VERIFICATION_PASSED', 'VERIFICATION_FAILED', 'RUN_COMPLETED'
    )),
    actor_id uuid NOT NULL,
    payload jsonb,
    event_hash char(64) NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL,
    persisted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, event_sequence),
    UNIQUE (run_id, event_sequence, firm_id, matter_id),
    UNIQUE (event_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    -- Dedicated SYSTEM_WORKER principals are ordinary ACTIVE users with only
    -- a SYSTEM_WORKER matter role; no synthetic actor can bypass this FK.
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)
);

-- Planning is itself an external, leased operation.  Its durable result is a
-- strict semantic proposal, not an executable graph.  A STARTED attempt whose
-- outcome is uncertain can only be reconciled; it is never automatically sent
-- to the provider again.
CREATE TABLE case_agent_planning_attempts (
    planning_attempt_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    planning_kind text NOT NULL CHECK (planning_kind IN ('PLAN', 'REPLAN')),
    status text NOT NULL CHECK (status IN (
        'CLAIMED', 'SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION', 'RECONCILING'
    )),
    attempt_version integer NOT NULL DEFAULT 1 CHECK (attempt_version > 0),
    planning_hash char(64) NOT NULL CHECK (planning_hash ~ '^[0-9a-f]{64}$'),
    matter_version integer NOT NULL CHECK (matter_version > 0),
    external_request_id uuid NOT NULL,
    lease_owner text NOT NULL CHECK (length(trim(lease_owner)) BETWEEN 1 AND 200),
    lease_token uuid NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    started_event_sequence bigint NOT NULL CHECK (started_event_sequence > 0),
    finished_event_sequence bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, planning_attempt_id, firm_id, matter_id),
    UNIQUE (run_id, external_request_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, started_event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id)
);

-- Provider-boundary history is separate from the mutable lease row.  Lease
-- heartbeats can therefore advance concurrently without racing the immutable
-- external ledger version consumed by the provider guard.
CREATE TABLE case_agent_planning_external_events (
    planning_external_event_id uuid PRIMARY KEY,
    planning_attempt_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    ledger_version integer NOT NULL CHECK (ledger_version IN (1, 2, 3)),
    status text NOT NULL CHECK (status IN (
        'SUBMISSION_STARTED', 'SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION'
    )),
    external_request_id uuid NOT NULL,
    provider_id text NOT NULL CHECK (length(trim(provider_id)) BETWEEN 1 AND 200),
    service_id text NOT NULL CHECK (length(trim(service_id)) BETWEEN 1 AND 200),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    output_hash char(64) CHECK (output_hash ~ '^[0-9a-f]{64}$'),
    error_code text CHECK (
        error_code IS NULL
        OR error_code ~ '^[A-Z][A-Z0-9._:-]{0,199}$'
    ),
    structured_proposal jsonb CHECK (
        structured_proposal IS NULL OR jsonb_typeof(structured_proposal) = 'object'
    ),
    recorded_by uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (planning_attempt_id, ledger_version),
    UNIQUE (run_id, external_request_id, ledger_version),
    FOREIGN KEY (run_id, planning_attempt_id, firm_id, matter_id)
        REFERENCES case_agent_planning_attempts(
            run_id, planning_attempt_id, firm_id, matter_id
        ),
    FOREIGN KEY (recorded_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (ledger_version = 1 AND status = 'SUBMISSION_STARTED'
            AND output_hash IS NULL AND error_code IS NULL
            AND structured_proposal IS NULL)
        OR (ledger_version = 2 AND status = 'SUCCEEDED'
            AND output_hash IS NOT NULL AND error_code IS NULL
            AND structured_proposal IS NOT NULL)
        OR (ledger_version = 2 AND status = 'FAILED'
            AND error_code IS NOT NULL AND structured_proposal IS NULL)
        OR (ledger_version = 2 AND status = 'UNKNOWN_SUBMISSION'
            AND output_hash IS NULL AND error_code IS NOT NULL
            AND structured_proposal IS NULL)
        OR (ledger_version = 3 AND status = 'SUCCEEDED'
            AND output_hash IS NOT NULL AND error_code IS NULL
            AND structured_proposal IS NOT NULL)
        OR (ledger_version = 3 AND status = 'FAILED'
            AND error_code IS NOT NULL AND structured_proposal IS NULL)
    )
);

-- Readiness is proved by a live worker process, not by the API merely having
-- constructed an Agent service object.  Heartbeats contain no case content;
-- they bind one firm-scoped service principal to the exact planner and
-- adapter catalog that are currently running.
CREATE TABLE case_agent_worker_heartbeats (
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    worker_id text NOT NULL CHECK (length(trim(worker_id)) BETWEEN 1 AND 200),
    actor_id uuid NOT NULL,
    planner_id text NOT NULL CHECK (length(trim(planner_id)) BETWEEN 1 AND 200),
    adapter_catalog_hash char(64) NOT NULL
        CHECK (adapter_catalog_hash ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > observed_at),
    heartbeat_version integer NOT NULL DEFAULT 1 CHECK (heartbeat_version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (firm_id, worker_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_agent_task_graphs (
    graph_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    graph_version bigint NOT NULL CHECK (graph_version > 0),
    goal_hash char(64) NOT NULL CHECK (goal_hash ~ '^[0-9a-f]{64}$'),
    snapshot_matter_version integer NOT NULL CHECK (snapshot_matter_version > 0),
    snapshot_schema_version text NOT NULL,
    snapshot_hash char(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    accepted_event_sequence bigint NOT NULL CHECK (accepted_event_sequence > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, graph_version),
    UNIQUE (graph_id, run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, accepted_event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id)
);

ALTER TABLE case_agent_runs
    ADD CONSTRAINT case_agent_runs_current_graph_fk
    FOREIGN KEY (current_graph_id, run_id, firm_id, matter_id)
    REFERENCES case_agent_task_graphs(graph_id, run_id, firm_id, matter_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE case_agent_tasks (
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    title text NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 500),
    purpose text NOT NULL CHECK (length(trim(purpose)) BETWEEN 1 AND 2000),
    rationale text NOT NULL CHECK (length(trim(rationale)) BETWEEN 1 AND 4000),
    input_refs jsonb NOT NULL CHECK (jsonb_typeof(input_refs) = 'array'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    skill_id text NOT NULL,
    skill_version text NOT NULL,
    tool_id text NOT NULL,
    tool_version text NOT NULL,
    adapter_id text NOT NULL,
    adapter_version text NOT NULL,
    granted_scopes jsonb NOT NULL CHECK (jsonb_typeof(granted_scopes) = 'array'),
    execution_mode text NOT NULL CHECK (execution_mode IN (
        'IN_PROCESS', 'ISOLATED_CONTAINER', 'NETWORK_CONNECTOR'
    )),
    network_policy text NOT NULL CHECK (network_policy IN ('DENY', 'EXACT_ALLOWLIST')),
    allowed_domains jsonb NOT NULL CHECK (jsonb_typeof(allowed_domains) = 'array'),
    sandbox_profile text NOT NULL,
    sandbox_policy_version text NOT NULL,
    sandbox_policy_hash char(64) NOT NULL CHECK (sandbox_policy_hash ~ '^[0-9a-f]{64}$'),
    reads_case_objects jsonb NOT NULL CHECK (jsonb_typeof(reads_case_objects) = 'array'),
    writes_managed_derivatives boolean NOT NULL,
    external_request_approval_required boolean NOT NULL,
    risk_level text NOT NULL CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH')),
    autonomy_level text NOT NULL CHECK (autonomy_level IN (
        'A0_OBSERVE', 'A1_PROPOSE', 'A2_INTERNAL_REVERSIBLE', 'A3_LAWYER_APPROVAL'
    )),
    approval_gate text NOT NULL CHECK (approval_gate IN (
        'NONE', 'MATERIAL_SCOPE', 'LAWYER_REVIEW', 'RELEASE_LOCK'
    )),
    retry_mode text NOT NULL CHECK (retry_mode IN (
        'IDEMPOTENT', 'BEFORE_EXTERNAL_SUBMISSION_ONLY', 'NEVER_AUTOMATIC'
    )),
    resource_budget jsonb NOT NULL CHECK (jsonb_typeof(resource_budget) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, task_id),
    UNIQUE (graph_id, sequence),
    UNIQUE (graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_task_graphs(graph_id, run_id, firm_id, matter_id)
);

CREATE TABLE case_agent_task_dependencies (
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    dependency_task_id uuid NOT NULL,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, task_id, dependency_task_id),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, dependency_task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    CHECK (task_id <> dependency_task_id)
);

CREATE TABLE case_agent_task_heads (
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN (
        'PENDING', 'READY', 'WAITING_APPROVAL', 'RUNNING', 'RETRYABLE', 'UNKNOWN',
        'SUCCEEDED', 'FAILED', 'STALE', 'CANCELLED'
    )),
    is_current boolean NOT NULL DEFAULT true,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    active_attempt_id uuid,
    head_event_version bigint NOT NULL CHECK (head_event_version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, task_id),
    UNIQUE (graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id)
);

CREATE TABLE case_agent_task_attempts (
    attempt_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    attempt_version integer NOT NULL DEFAULT 1 CHECK (attempt_version > 0),
    status text NOT NULL CHECK (status IN (
        'RUNNING', 'RECONCILING', 'SUCCEEDED', 'FAILED', 'UNKNOWN', 'STALE', 'CANCELLED'
    )),
    command_id char(64) NOT NULL CHECK (command_id ~ '^[0-9a-f]{64}$'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    retry_mode text NOT NULL CHECK (retry_mode IN (
        'IDEMPOTENT', 'BEFORE_EXTERNAL_SUBMISSION_ONLY', 'NEVER_AUTOMATIC'
    )),
    adapter_id text NOT NULL,
    adapter_version text NOT NULL,
    lease_owner text NOT NULL CHECK (length(trim(lease_owner)) BETWEEN 1 AND 200),
    lease_expires_at timestamptz NOT NULL,
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    external_approval_id uuid,
    external_request_id text,
    external_submission_state text NOT NULL CHECK (external_submission_state IN (
        'NOT_APPLICABLE', 'NOT_SUBMITTED', 'SUBMITTED', 'UNKNOWN'
    )),
    started_event_sequence bigint NOT NULL CHECK (started_event_sequence > 0),
    finished_event_sequence bigint CHECK (finished_event_sequence > started_event_sequence),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, graph_id, task_id, attempt_number),
    UNIQUE (attempt_id, run_id, firm_id, matter_id),
    UNIQUE (attempt_id, run_id, task_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, started_event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id)
);

ALTER TABLE case_agent_task_heads
    ADD CONSTRAINT case_agent_task_heads_active_attempt_fk
    FOREIGN KEY (active_attempt_id, run_id, task_id, firm_id, matter_id)
    REFERENCES case_agent_task_attempts(attempt_id, run_id, task_id, firm_id, matter_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE case_agent_approvals (
    approval_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    approval_kind text NOT NULL CHECK (approval_kind IN ('TASK', 'FINAL_REVIEW')),
    task_input_hash char(64),
    graph_hash char(64) NOT NULL CHECK (graph_hash ~ '^[0-9a-f]{64}$'),
    gate text,
    verification_hash char(64),
    artifact_manifest_hash char(64),
    approved_by uuid NOT NULL,
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    approved_at timestamptz NOT NULL,
    UNIQUE (approval_id, firm_id, matter_id),
    UNIQUE NULLS NOT DISTINCT (approval_id, run_id, task_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_task_graphs(graph_id, run_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (run_id, event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id),
    CHECK (
        (approval_kind = 'TASK' AND task_id IS NOT NULL
            AND task_input_hash ~ '^[0-9a-f]{64}$'
            AND gate IN ('NONE', 'MATERIAL_SCOPE', 'LAWYER_REVIEW', 'RELEASE_LOCK')
            AND verification_hash IS NULL AND artifact_manifest_hash IS NULL)
        OR (approval_kind = 'FINAL_REVIEW' AND task_id IS NULL
            AND task_input_hash IS NULL AND gate IS NULL
            AND verification_hash ~ '^[0-9a-f]{64}$'
            AND artifact_manifest_hash ~ '^[0-9a-f]{64}$')
    )
);

ALTER TABLE case_agent_approvals
    ADD CONSTRAINT case_agent_approvals_task_fk
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
    REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE case_agent_task_attempts
    ADD CONSTRAINT case_agent_task_attempts_external_approval_fk
    FOREIGN KEY (external_approval_id, run_id, task_id, firm_id, matter_id)
    REFERENCES case_agent_approvals(approval_id, run_id, task_id, firm_id, matter_id);

CREATE TABLE case_agent_task_receipts (
    receipt_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    task_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    adapter_id text NOT NULL,
    adapter_version text NOT NULL,
    result_status text NOT NULL CHECK (result_status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN')),
    external_submission_state text NOT NULL CHECK (external_submission_state IN (
        'NOT_APPLICABLE', 'NOT_SUBMITTED', 'SUBMITTED', 'UNKNOWN'
    )),
    output_hash char(64) CHECK (output_hash ~ '^[0-9a-f]{64}$'),
    error_code text,
    external_request_id text,
    runtime_seconds integer NOT NULL CHECK (runtime_seconds >= 0),
    cost_minor_units bigint NOT NULL CHECK (cost_minor_units >= 0),
    external_calls integer NOT NULL CHECK (external_calls >= 0),
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, event_sequence),
    UNIQUE (receipt_id, firm_id, matter_id),
    UNIQUE (receipt_id, run_id, firm_id, matter_id),
    FOREIGN KEY (attempt_id, run_id, task_id, firm_id, matter_id)
        REFERENCES case_agent_task_attempts(attempt_id, run_id, task_id, firm_id, matter_id),
    FOREIGN KEY (run_id, event_sequence, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id),
    CHECK (
        (result_status = 'SUCCEEDED' AND output_hash IS NOT NULL AND error_code IS NULL)
        OR (result_status = 'FAILED' AND output_hash IS NULL AND error_code IS NOT NULL)
        OR (result_status = 'UNKNOWN' AND output_hash IS NULL
            AND external_submission_state = 'UNKNOWN' AND external_request_id IS NOT NULL)
    )
);

-- Crossing an external boundary is recorded before transport.  This record is
-- deliberately separate from the final Tool receipt: after a worker crash it
-- is the durable proof that replay must reconcile rather than resend.
CREATE TABLE case_agent_external_submissions (
    submission_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    task_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    external_request_id text NOT NULL CHECK (
        length(trim(external_request_id)) BETWEEN 1 AND 500
    ),
    destination text NOT NULL CHECK (length(trim(destination)) BETWEEN 1 AND 500),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    -- A row is immutable evidence that submission started.  Confirmation or
    -- uncertainty belongs to the append-only result receipt/event, not an
    -- update to this boundary marker.
    submission_state text NOT NULL DEFAULT 'STARTED' CHECK (submission_state = 'STARTED'),
    recorded_by uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, attempt_id),
    UNIQUE (run_id, external_request_id),
    UNIQUE (submission_id, firm_id, matter_id),
    FOREIGN KEY (attempt_id, run_id, task_id, firm_id, matter_id)
        REFERENCES case_agent_task_attempts(attempt_id, run_id, task_id, firm_id, matter_id),
    FOREIGN KEY (recorded_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_agent_artifacts (
    artifact_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    receipt_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    artifact_kind text NOT NULL,
    content_hash char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    source_input_hash char(64) NOT NULL CHECK (source_input_hash ~ '^[0-9a-f]{64}$'),
    managed_derivative boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (artifact_id, firm_id, matter_id),
    FOREIGN KEY (receipt_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_task_receipts(receipt_id, run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id)
);

CREATE TABLE case_agent_checkpoints (
    checkpoint_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    event_version bigint NOT NULL CHECK (event_version > 0),
    projection_hash char(64) NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{64}$'),
    projection jsonb NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, event_version),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, event_version, firm_id, matter_id)
        REFERENCES case_agent_events(run_id, event_sequence, firm_id, matter_id)
);

-- Existing audit_events use matter-ledger versions.  Agent event versions are
-- a different domain and must not be smuggled into those columns.
CREATE TABLE case_agent_command_audits (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    command_name text NOT NULL CHECK (length(trim(command_name)) BETWEEN 1 AND 200),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    input_event_version bigint NOT NULL CHECK (input_event_version >= 0),
    output_event_version bigint NOT NULL CHECK (output_event_version >= input_event_version),
    event_id uuid,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (event_id, firm_id, matter_id)
        REFERENCES case_agent_events(event_id, firm_id, matter_id)
);

CREATE INDEX case_agent_runs_matter_updated_idx
    ON case_agent_runs(matter_id, updated_at DESC);
CREATE INDEX case_agent_events_replay_idx
    ON case_agent_events(run_id, event_sequence);
CREATE INDEX case_agent_dispatch_idx
    ON case_agent_task_heads(firm_id, matter_id, status, updated_at)
    WHERE is_current AND status IN ('READY', 'RETRYABLE', 'UNKNOWN');
CREATE INDEX case_agent_attempt_leases_idx
    ON case_agent_task_attempts(lease_expires_at)
    WHERE status IN ('RUNNING', 'RECONCILING');
CREATE INDEX case_agent_planning_leases_idx
    ON case_agent_planning_attempts(lease_expires_at)
    WHERE status IN ('CLAIMED', 'RECONCILING');
CREATE INDEX case_agent_worker_heartbeat_expiry_idx
    ON case_agent_worker_heartbeats(firm_id, expires_at DESC);

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_goals', 'case_agent_runs', 'case_agent_events',
        'case_agent_planning_attempts', 'case_agent_planning_external_events',
        'case_agent_worker_heartbeats',
        'case_agent_task_graphs', 'case_agent_tasks', 'case_agent_task_dependencies',
        'case_agent_task_heads', 'case_agent_task_attempts', 'case_agent_approvals',
        'case_agent_task_receipts', 'case_agent_external_submissions',
        'case_agent_artifacts', 'case_agent_checkpoints', 'case_agent_command_audits'
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

CREATE FUNCTION prohibit_case_agent_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent goals, events, graphs, tasks, approvals, receipts, artifacts and checkpoints are append-only';
END;
$$;

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_goals', 'case_agent_events', 'case_agent_task_graphs',
        'case_agent_tasks', 'case_agent_task_dependencies', 'case_agent_approvals',
        'case_agent_task_receipts', 'case_agent_external_submissions',
        'case_agent_artifacts', 'case_agent_checkpoints', 'case_agent_command_audits'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_history_mutation()',
            table_name || '_append_only', table_name
        );
    END LOOP;
END;
$$;

CREATE FUNCTION guard_case_agent_run_head() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.firm_id <> OLD.firm_id OR NEW.matter_id <> OLD.matter_id
       OR NEW.goal_id <> OLD.goal_id OR NEW.created_by <> OLD.created_by
       OR NEW.run_budget <> OLD.run_budget
       OR NEW.current_event_version <> OLD.current_event_version + 1 THEN
        RAISE EXCEPTION 'case Agent run head transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_runs_guard BEFORE UPDATE OR DELETE ON case_agent_runs
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_run_head();

CREATE FUNCTION guard_case_agent_task_head() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.firm_id <> OLD.firm_id OR NEW.matter_id <> OLD.matter_id
       OR NEW.run_id <> OLD.run_id OR NEW.task_id <> OLD.task_id
       OR NEW.graph_id <> OLD.graph_id
       OR NEW.head_event_version <= OLD.head_event_version THEN
        RAISE EXCEPTION 'case Agent task head transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_task_heads_guard BEFORE UPDATE OR DELETE ON case_agent_task_heads
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_task_head();

CREATE FUNCTION guard_case_agent_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.firm_id <> OLD.firm_id OR NEW.matter_id <> OLD.matter_id
       OR NEW.run_id <> OLD.run_id OR NEW.task_id <> OLD.task_id
       OR NEW.attempt_id <> OLD.attempt_id
       OR NEW.attempt_version <> OLD.attempt_version + 1 THEN
        RAISE EXCEPTION 'case Agent attempt transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_task_attempts_guard
    BEFORE UPDATE OR DELETE ON case_agent_task_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_attempt();

CREATE FUNCTION guard_case_agent_planning_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id OR NEW.run_id <> OLD.run_id
       OR NEW.planning_attempt_id <> OLD.planning_attempt_id
       OR NEW.external_request_id <> OLD.external_request_id
       OR NEW.planning_hash <> OLD.planning_hash
       OR NEW.matter_version <> OLD.matter_version
       OR NEW.attempt_version <> OLD.attempt_version + 1 THEN
        RAISE EXCEPTION 'case Agent planning attempt transition is invalid';
    END IF;
    IF NOT (
        (OLD.status = 'CLAIMED' AND NEW.status IN (
            'CLAIMED', 'SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION'
        ))
        OR (OLD.status = 'UNKNOWN_SUBMISSION' AND NEW.status = 'RECONCILING')
        OR (OLD.status = 'RECONCILING' AND NEW.status IN (
            'RECONCILING', 'SUCCEEDED', 'FAILED'
        ))
    ) THEN
        RAISE EXCEPTION 'case Agent planning status transition is invalid';
    END IF;
    IF NEW.lease_token <> OLD.lease_token AND NOT (
        (OLD.status = 'UNKNOWN_SUBMISSION' AND NEW.status = 'RECONCILING')
        OR (OLD.status = 'RECONCILING' AND NEW.status = 'RECONCILING'
            AND OLD.lease_expires_at <= now())
    ) THEN
        RAISE EXCEPTION 'case Agent planning lease token rotation is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_planning_attempts_guard
    BEFORE UPDATE OR DELETE ON case_agent_planning_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_planning_attempt();

CREATE FUNCTION guard_case_agent_worker_heartbeat() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.firm_id <> OLD.firm_id
       OR NEW.worker_id <> OLD.worker_id OR NEW.actor_id <> OLD.actor_id
       OR NEW.heartbeat_version <> OLD.heartbeat_version + 1
       OR NEW.observed_at <= OLD.observed_at THEN
        RAISE EXCEPTION 'case Agent worker heartbeat transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_worker_heartbeats_guard
    BEFORE UPDATE OR DELETE ON case_agent_worker_heartbeats
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_worker_heartbeat();

COMMIT;
