"""Production persistence ports for the independent case-Agent runner.

The control-plane event stream remains authoritative.  This module adds only
recoverable wake-up leasing and hash-bound adapters for the first read-only
PDF/Office skills.  It accepts opaque compiled references, never browser paths,
URLs, commands or raw document text.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Iterator, Protocol
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_skill_adapters import (
    BoundCommonDocument,
    CaseAgentSkillAdapterBlocked,
    EvidenceProjectionAuthorization,
    ReviewCandidateStagingRequest,
    StagedReviewCandidate,
)
from .case_agent_supervisor import ArtifactReceipt
from .case_agent_verifier import (
    ArtifactVerificationRejected,
    CanonicalJsonArtifactVerifier,
    FIRST_RELEASE_EXECUTABLE_REVIEW_CANDIDATE_SCHEMAS,
    ManagedArtifactRead,
)
from .case_agent_ledger_extraction import ExtractionSourceMode
from .case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_TOOL_ID,
    LedgerExtractionPageProjection,
)
from .common_document_reader import CommonDocumentFormat, MaterializedDocumentSource
from .models import Actor, Role
from .web_object_store import (
    StoredCaseAgentMaterial,
    StoredCaseAgentReviewCandidate,
)


class CaseAgentRuntimePersistenceBlocked(RuntimeError):
    """The durable queue or a private execution binding is not trustworthy."""


_INPUT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INCIDENT_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_HUMAN_EVIDENCE_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)

# A production Worker must prove the complete executable schema before it can
# enter the consumption loop and publish a heartbeat.  This is intentionally a
# contract, not a generic ``SELECT 1``: missing executable control, memory or
# decision and pre-planning memory contracts through 0036 must keep the Web capability closed even
# when PostgreSQL itself is reachable.
_CASE_AGENT_RUNTIME_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "matters": frozenset({"matter_id", "firm_id", "version"}),
    "users": frozenset({"user_id", "firm_id", "status"}),
    "matter_actor_roles": frozenset(
        {"matter_id", "firm_id", "user_id", "role", "revoked_at"}
    ),
    "case_facts": frozenset({"fact_id", "firm_id", "matter_id", "status"}),
    "case_claims": frozenset({"claim_id", "firm_id", "matter_id", "status"}),
    "case_transactions": frozenset(
        {"transaction_id", "firm_id", "matter_id", "status"}
    ),
    "evidence_original_files": frozenset(
        {"evidence_file_id", "firm_id", "matter_id", "original_file_sha256"}
    ),
    "evidence_pages": frozenset(
        {"evidence_page_id", "evidence_file_id", "firm_id", "matter_id"}
    ),
    "web_evidence_original_source_objects": frozenset(
        {
            "evidence_file_id",
            "firm_id",
            "matter_id",
            "source_object_key",
            "source_object_sha256",
        }
    ),
    "case_agent_runs": frozenset(
        {
            "run_id",
            "firm_id",
            "matter_id",
            "goal_id",
            "status",
            "current_event_version",
            "current_graph_id",
            "snapshot_matter_version",
            "snapshot_hash",
            "is_stale",
            "is_cancelled",
            "created_by",
        }
    ),
    "case_agent_goals": frozenset(
        {
            "goal_id",
            "firm_id",
            "matter_id",
            "requested_by",
            "goal_hash",
            "requested_deliverables",
            "active_plan_execution",
        }
    ),
    "case_agent_events": frozenset(
        {
            "event_id",
            "run_id",
            "firm_id",
            "matter_id",
            "event_sequence",
            "event_type",
            "actor_id",
            "event_hash",
        }
    ),
    "case_agent_task_graphs": frozenset(
        {"graph_id", "run_id", "firm_id", "matter_id", "graph_hash"}
    ),
    "case_agent_tasks": frozenset(
        {
            "graph_id",
            "task_id",
            "run_id",
            "firm_id",
            "matter_id",
            "input_refs",
            "input_hash",
            "tool_id",
        }
    ),
    "case_agent_task_dependencies": frozenset(
        {
            "graph_id",
            "task_id",
            "dependency_task_id",
            "run_id",
            "firm_id",
            "matter_id",
        }
    ),
    "case_agent_task_heads": frozenset(
        {
            "graph_id",
            "task_id",
            "run_id",
            "firm_id",
            "matter_id",
            "status",
            "is_current",
        }
    ),
    "case_agent_task_receipts": frozenset(
        {
            "receipt_id",
            "run_id",
            "attempt_id",
            "task_id",
            "firm_id",
            "matter_id",
            "input_hash",
            "result_status",
            "output_hash",
        }
    ),
    "case_agent_artifacts": frozenset(
        {
            "artifact_id",
            "run_id",
            "receipt_id",
            "firm_id",
            "matter_id",
            "artifact_kind",
            "content_hash",
            "source_input_hash",
        }
    ),
    "case_agent_checkpoints": frozenset(
        {
            "checkpoint_id",
            "run_id",
            "firm_id",
            "matter_id",
            "event_version",
            "projection_hash",
        }
    ),
    "case_agent_command_audits": frozenset(
        {
            "audit_id",
            "run_id",
            "firm_id",
            "matter_id",
            "actor_id",
            "command_name",
            "request_hash",
            "input_event_version",
            "output_event_version",
        }
    ),
    "case_agent_run_inbox": frozenset(
        {
            "run_id",
            "firm_id",
            "matter_id",
            "inbox_status",
            "observed_event_version",
            "available_at",
            "lease_owner",
            "lease_token",
            "lease_expires_at",
            "inbox_version",
        }
    ),
    "case_agent_material_objects": frozenset(
        {
            "material_object_id",
            "firm_id",
            "matter_id",
            "admitted_format",
            "media_type",
            "content_sha256",
            "byte_size",
            "source_object_key",
            "source_reference_hash",
            "inspection_hash",
        }
    ),
    "case_agent_material_object_tombstones": frozenset(
        {"material_object_id", "firm_id", "matter_id", "revoked_at"}
    ),
    "case_agent_review_candidates": frozenset(
        {
            "artifact_id",
            "run_id",
            "graph_id",
            "task_id",
            "firm_id",
            "matter_id",
            "task_input_hash",
            "artifact_kind",
            "content_sha256",
            "byte_size",
            "review_status",
            "source_object_key",
            "receipt_hash",
        }
    ),
    "case_agent_sealed_response_recovery_candidates": frozenset(
        {
            "recovery_id",
            "artifact_id",
            "run_id",
            "graph_id",
            "task_id",
            "firm_id",
            "matter_id",
            "task_input_hash",
            "candidate_content_sha256",
            "source_run_event_version",
            "source_snapshot_hash",
            "recovery_kind",
            "recovery_policy_hash",
            "external_request_id",
            "request_hash",
            "response_sha256",
            "archive_sha256",
            "failure_code",
        }
    ),
    "case_agent_worker_heartbeats": frozenset(
        {
            "firm_id",
            "worker_id",
            "actor_id",
            "planner_id",
            "adapter_catalog_hash",
            "verifier_actor_id",
            "verifier_id",
            "verifier_version",
            "verifier_policy_hash",
            "observed_at",
            "expires_at",
        }
    ),
    "case_agent_verification_attempts": frozenset(
        {
            "verification_attempt_id",
            "run_id",
            "graph_id",
            "firm_id",
            "matter_id",
            "execution_actor_id",
            "verifier_actor_id",
            "policy_hash",
        }
    ),
    "case_agent_verification_receipts": frozenset(
        {
            "verification_receipt_id",
            "verification_attempt_id",
            "run_id",
            "firm_id",
            "matter_id",
            "verification_hash",
        }
    ),
    "case_agent_lawyer_decision_signals": frozenset(
        {
            "signal_id",
            "run_id",
            "graph_id",
            "task_id",
            "firm_id",
            "matter_id",
            "is_current",
            "superseded_at",
            "graph_hash",
            "task_input_hash",
            "source_ref_ids",
            "decision_hash",
        }
    ),
    "case_agent_memory_retrieval_audits": frozenset(
        {
            "retrieval_id",
            "firm_id",
            "matter_id",
            "actor_id",
            "run_id",
            "task_id",
            "scope_hash",
        }
    ),
    "case_agent_memory_checkpoints": frozenset(
        {
            "checkpoint_id",
            "firm_id",
            "matter_id",
            "owner_actor_id",
            "run_id",
            "sequence",
            "case_snapshot_hash",
            "plan_hash",
            "task_state_hash",
            "checkpoint_hash",
        }
    ),
    "case_agent_planning_memory_enrichments": frozenset(
        {
            "enrichment_id",
            "firm_id",
            "matter_id",
            "run_id",
            "goal_id",
            "case_snapshot_hash",
            "base_planning_hash",
            "query_contract_hash",
            "items",
            "receipt_hash",
            "created_by_worker",
        }
    ),
    # 0030 + 0043 + 0046 + 0047 are executable runtime dependencies.  A PASSED graph
    # with extracted ledger candidates must now pause before plan promotion,
    # and only a durable post-review snapshot refresh may drive REPLAN.  A
    # database missing that bridge must never heartbeat as ready.
    "case_work_plans": frozenset(
        {
            "plan_id",
            "firm_id",
            "matter_id",
            "status",
            "planned_matter_version",
            "profile_id",
            "profile_version",
            "profile_hash",
            "objective_approval_id",
            "agent_goal_id",
            "objective_hash",
            "plan_hash",
        }
    ),
    "case_work_plan_items": frozenset(
        {
            "item_id",
            "plan_id",
            "firm_id",
            "matter_id",
            "item_kind",
            "readiness",
            "title",
            "purpose",
            "rationale",
            "delivery_target",
            "deliverable_kind",
        }
    ),
    "case_work_plan_context_references": frozenset(
        {
            "plan_id",
            "firm_id",
            "matter_id",
            "source_type",
            "source_id",
            "source_version",
            "source_hash",
            "reference_use",
        }
    ),
    "case_work_plan_item_references": frozenset(
        {"plan_id", "item_id", "firm_id", "matter_id", "reference_role"}
    ),
    "case_work_plan_item_prerequisites": frozenset(
        {
            "plan_id",
            "item_id",
            "prerequisite_item_id",
            "firm_id",
            "matter_id",
        }
    ),
    "case_work_plan_heads": frozenset(
        {"matter_id", "firm_id", "latest_plan_version", "current_plan_id"}
    ),
    "case_work_plan_events": frozenset(
        {
            "plan_id",
            "firm_id",
            "matter_id",
            "event_sequence",
            "event_type",
            "effective_status",
        }
    ),
    "case_agent_work_plan_promotions": frozenset(
        {
            "promotion_id",
            "plan_id",
            "firm_id",
            "matter_id",
            "run_id",
            "graph_id",
            "graph_hash",
            "snapshot_hash",
            "goal_id",
            "verification_receipt_id",
            "verification_hash",
        }
    ),
    "case_agent_work_plan_input_bindings": frozenset(
        {
            "binding_id",
            "plan_id",
            "promotion_id",
            "firm_id",
            "matter_id",
            "input_ref",
            "object_type",
            "object_id",
            "object_version",
            "content_hash",
            "source_status",
            "reference_use",
            "binding_hash",
        }
    ),
    "case_agent_active_plan_execution_runs": frozenset(
        {
            "execution_id",
            "firm_id",
            "matter_id",
            "plan_id",
            "plan_hash",
            "activated_matter_version",
            "source_run_id",
            "run_id",
            "requested_by",
            "execution_attempt",
            "supersedes_execution_id",
            "retry_reason_code",
            "created_at",
        }
    ),
    "case_work_plan_item_reviews": frozenset(
        {
            "review_id",
            "plan_id",
            "item_id",
            "firm_id",
            "matter_id",
            "plan_hash",
            "decision",
            "reviewed_by",
            "request_hash",
            "reviewed_matter_version",
        }
    ),
    "case_agent_snapshot_refresh_requests": frozenset(
        {
            "refresh_request_id",
            "source_outbox_id",
            "source_audit_event_id",
            "extraction_batch_id",
            "run_id",
            "firm_id",
            "matter_id",
            "source_matter_version",
            "target_matter_version",
            "request_status",
            "applied_event_id",
            "applied_event_sequence",
            "applied_by",
        }
    ),
    "case_agent_ledger_exception_groups": frozenset(
        {
            "exception_group_id",
            "extraction_batch_id",
            "run_id",
            "firm_id",
            "matter_id",
            "group_key_hash",
            "candidate_set_hash",
            "candidate_count",
            "source_policy",
            "risk_policy",
        }
    ),
    "case_agent_ledger_exception_group_members": frozenset(
        {
            "exception_group_id",
            "extraction_batch_id",
            "extraction_candidate_id",
            "firm_id",
            "matter_id",
            "candidate_hash",
        }
    ),
    "case_agent_ledger_exception_group_decisions": frozenset(
        {
            "exception_decision_id",
            "exception_group_id",
            "extraction_batch_id",
            "run_id",
            "firm_id",
            "matter_id",
            "bound_group_key_hash",
            "bound_candidate_set_hash",
            "bound_candidate_count",
            "decision",
            "reason_code",
            "expected_matter_version",
            "decided_by",
            "idempotency_key",
            "request_hash",
        }
    ),
    "case_agent_ledger_exception_decision_events": frozenset(
        {
            "exception_decision_event_id",
            "exception_decision_id",
            "exception_group_id",
            "extraction_batch_id",
            "run_id",
            "firm_id",
            "matter_id",
            "event_type",
            "matter_version",
            "actor_id",
            "request_hash",
            "payload",
        }
    ),
}

_CASE_AGENT_RUNTIME_REQUIRED_TRIGGERS = frozenset(
    {
        "case_agent_runs_wake_worker_after_insert",
        "case_agent_runs_wake_worker_after_event",
        "case_agent_run_inbox_guard",
        "case_agent_verification_attempt_principals_guard",
        "case_agent_verification_receipt_principals_guard",
        "case_agent_verification_attempts_append_only",
        "case_agent_verification_receipts_append_only",
        "case_agent_lawyer_decision_signals_guard",
        "case_agent_memory_retrieval_run_scope_guard",
        "case_agent_memory_checkpoint_guard",
        "case_agent_planning_memory_enrichment_guard",
        "case_agent_planning_memory_enrichments_append_only",
        "case_work_plans_guard",
        "case_work_plan_items_append_only",
        "case_work_plan_context_refs_append_only",
        "case_work_plan_item_refs_append_only",
        "case_work_plan_prerequisites_append_only",
        "case_work_plan_events_append_only",
        "case_work_plan_heads_guard",
        "case_agent_work_plan_promotion_valid",
        "case_work_plan_agent_goal_promotion_required",
        "case_agent_work_plan_promotions_append_only",
        "case_agent_work_plan_input_bindings_append_only",
        "case_work_plan_item_reviews_append_only",
        "case_work_plan_item_review_valid",
        "case_work_plan_adverse_review_activation_block",
        "case_agent_ledger_confirmation_enqueues_snapshot_refresh",
        "case_agent_snapshot_refresh_wakes_worker",
        "case_agent_snapshot_refresh_requests_guard",
        "case_agent_work_plan_open_ledger_review_block",
        "case_work_plan_open_ledger_review_activation_block",
        "case_agent_ledger_exception_groups_after_staging",
        "case_agent_ledger_exception_decision_validate",
        "case_agent_ledger_exception_decision_audit",
        "case_agent_ledger_exception_groups_complete",
        "case_agent_ledger_exception_members_complete",
        "case_agent_ledger_exception_candidates_complete",
        "case_agent_exception_decision_unblocks_snapshot_refresh",
        "case_agent_exception_only_resolution_enqueues_snapshot_refresh",
        "case_agent_ledger_exception_groups_append_only",
        "case_agent_ledger_exception_members_append_only",
        "case_agent_ledger_exception_decisions_append_only",
        "case_agent_ledger_exception_events_append_only",
        "case_agent_active_plan_execution_runs_guard",
        "case_agent_active_plan_execution_runs_append_only",
        "case_agent_sealed_response_recovery_candidates_validate",
        "case_agent_sealed_response_recovery_candidates_append_only",
    }
)

_CASE_AGENT_RUNTIME_REQUIRED_FUNCTIONS = frozenset(
    {
        "case_agent_ledger_extraction_batch_review_status",
        "case_agent_ledger_extraction_run_staging_complete",
        "case_agent_ledger_extraction_run_review_resolved",
        "case_agent_ledger_extraction_current_review_version",
    }
)


def preflight_case_agent_runtime_contract(
    *,
    execution_dsn: str,
    verifier_dsn: str,
    firm_id: str,
    execution_actor_id: str,
    verifier_actor_id: str,
) -> None:
    """Fail before heartbeat unless both principals see the production contract.

    The execution connection requires the full executable surface through
    0053 (including governed memory, verified-graph plan promotion, the
    post-ledger-review replan bridge and terminal exception routing) and the
    fixed same-firm principals.  The verifier connection separately proves it
    can read the review-candidate lineage and write the verification ledger.
    Runtime repositories still re-authorize each matter/task after this global
    startup fence.
    """

    for value, label in (
        (firm_id, "firm_id"),
        (execution_actor_id, "execution_actor_id"),
        (verifier_actor_id, "verifier_actor_id"),
    ):
        _uuid(value, label)
    if execution_actor_id == verifier_actor_id:
        raise CaseAgentRuntimePersistenceBlocked(
            "execution and verifier identities must differ"
        )
    if (
        not isinstance(execution_dsn, str)
        or not execution_dsn.strip()
        or not isinstance(verifier_dsn, str)
        or not verifier_dsn.strip()
        or execution_dsn == verifier_dsn
    ):
        raise CaseAgentRuntimePersistenceBlocked(
            "execution and verifier PostgreSQL principals must be distinct"
        )

    try:
        with _runtime_preflight_connection(execution_dsn, firm_id) as connection:
            _require_runtime_schema_contract(connection)
            _require_preflight_principal(
                connection,
                firm_id=firm_id,
                actor_id=execution_actor_id,
                label="execution",
            )
            _require_preflight_principal(
                connection,
                firm_id=firm_id,
                actor_id=verifier_actor_id,
                label="verifier",
            )
            _require_all_firm_matters_bound(
                connection,
                firm_id=firm_id,
                execution_actor_id=execution_actor_id,
                verifier_actor_id=verifier_actor_id,
            )
            # Exercise the exact persistent inbox read surface even if no run
            # currently exists.  RLS and table privileges are therefore part
            # of readiness instead of first-run surprises.
            connection.execute(
                """
                SELECT run_id
                FROM case_agent_run_inbox
                WHERE firm_id = %s
                ORDER BY available_at, updated_at, run_id
                LIMIT 1
                """,
                (firm_id,),
            ).fetchall()

        with _runtime_preflight_connection(verifier_dsn, firm_id) as connection:
            _require_preflight_principal(
                connection,
                firm_id=firm_id,
                actor_id=verifier_actor_id,
                label="verifier",
            )
            _require_verifier_runtime_access(connection, firm_id=firm_id)
    except CaseAgentRuntimePersistenceBlocked:
        raise
    except Exception as error:
        raise CaseAgentRuntimePersistenceBlocked(
            "case Agent runtime contract preflight failed"
        ) from error


@contextmanager
def _runtime_preflight_connection(dsn: str, firm_id: str) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
        yield connection


def _require_runtime_schema_contract(connection: Any) -> None:
    rows = connection.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        ORDER BY table_name, ordinal_position
        """,
        (list(_CASE_AGENT_RUNTIME_REQUIRED_COLUMNS),),
    ).fetchall()
    observed: dict[str, set[str]] = {}
    for row in rows:
        observed.setdefault(str(row["table_name"]), set()).add(
            str(row["column_name"])
        )
    missing = {
        table_name: sorted(required.difference(observed.get(table_name, set())))
        for table_name, required in _CASE_AGENT_RUNTIME_REQUIRED_COLUMNS.items()
        if not required.issubset(observed.get(table_name, set()))
    }
    if missing:
        raise CaseAgentRuntimePersistenceBlocked(
            "case Agent runtime migrations through 0035 and required extensions through 0053, including sealed-response recovery 0077, are not installed"
        )

    trigger_rows = connection.execute(
        """
        SELECT trigger.tgname AS trigger_name
        FROM pg_catalog.pg_trigger AS trigger
        JOIN pg_catalog.pg_class AS relation
          ON relation.oid = trigger.tgrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND NOT trigger.tgisinternal
          AND trigger.tgname = ANY(%s)
        """,
        (list(_CASE_AGENT_RUNTIME_REQUIRED_TRIGGERS),),
    ).fetchall()
    observed_triggers = {str(row["trigger_name"]) for row in trigger_rows}
    if not _CASE_AGENT_RUNTIME_REQUIRED_TRIGGERS.issubset(observed_triggers):
        raise CaseAgentRuntimePersistenceBlocked(
            "case Agent runtime migration guards through 0035 and required extensions through 0053, including sealed-response recovery 0077, are not installed"
        )

    function_rows = connection.execute(
        """
        SELECT routine_name
        FROM information_schema.routines
        WHERE routine_schema = 'public' AND routine_name = ANY(%s)
        """,
        (list(_CASE_AGENT_RUNTIME_REQUIRED_FUNCTIONS),),
    ).fetchall()
    observed_functions = {str(row["routine_name"]) for row in function_rows}
    if not _CASE_AGENT_RUNTIME_REQUIRED_FUNCTIONS.issubset(observed_functions):
        raise CaseAgentRuntimePersistenceBlocked(
            "case Agent runtime review proofs through 0047 and verifier authority 0060 are not installed"
        )

    rls_rows = connection.execute(
        """
        SELECT relname, relrowsecurity, relforcerowsecurity
        FROM pg_catalog.pg_class
        JOIN pg_catalog.pg_namespace
          ON pg_namespace.oid = pg_class.relnamespace
        WHERE pg_namespace.nspname = 'public'
          AND pg_class.relkind = 'r'
          AND relname = ANY(%s)
        """,
        (list(_CASE_AGENT_RUNTIME_REQUIRED_COLUMNS),),
    ).fetchall()
    rls = {
        str(row["relname"]): (
            bool(row["relrowsecurity"]), bool(row["relforcerowsecurity"])
        )
        for row in rls_rows
    }
    if any(rls.get(table_name) != (True, True) for table_name in _CASE_AGENT_RUNTIME_REQUIRED_COLUMNS):
        raise CaseAgentRuntimePersistenceBlocked(
            "case Agent runtime requires forced tenant row-level security"
        )

    _require_active_plan_execution_security_contract(connection)
    _require_verifier_snapshot_authority_contract(connection)


def _require_verifier_snapshot_authority_contract(connection: Any) -> None:
    row = connection.execute(
        """
        SELECT
            pg_catalog.pg_get_userbyid(procedure.proowner) AS owner_name,
            procedure.prosecdef AS security_definer,
            pg_catalog.has_function_privilege(
                'lawcase_agent_verifier',
                'public.authorize_case_agent_verification_snapshot(uuid,uuid,uuid,integer)',
                'EXECUTE'
            ) AS verifier_execute,
            pg_catalog.has_function_privilege(
                'lawcase_agent_worker',
                'public.authorize_case_agent_verification_snapshot(uuid,uuid,uuid,integer)',
                'EXECUTE'
            ) AS worker_execute,
            pg_catalog.has_function_privilege(
                'lawcase_web_application',
                'public.authorize_case_agent_verification_snapshot(uuid,uuid,uuid,integer)',
                'EXECUTE'
            ) AS web_execute
            , pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.outbox_events',
                'firm_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.outbox_events',
                'matter_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.outbox_events',
                'aggregate_version', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.outbox_events',
                'event_type', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.outbox_events',
                'payload', 'INSERT'
            ) AS verifier_outbox_insert
            , pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.command_idempotency',
                'firm_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.command_idempotency',
                'matter_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.command_idempotency',
                'actor_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.command_idempotency',
                'command_name', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.command_idempotency',
                'idempotency_key', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.command_idempotency',
                'request_hash', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_verifier', 'public.command_idempotency',
                'response_json', 'INSERT'
            ) AS verifier_idempotency_insert
            , pg_catalog.has_table_privilege(
                'lawcase_agent_verifier',
                'public.outbox_events',
                'UPDATE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_agent_verifier',
                'public.outbox_events',
                'DELETE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_agent_verifier',
                'public.outbox_events',
                'TRUNCATE'
            ) AS verifier_outbox_mutate
            , pg_catalog.has_table_privilege(
                'lawcase_agent_verifier',
                'public.command_idempotency',
                'UPDATE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_agent_verifier',
                'public.command_idempotency',
                'DELETE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_agent_verifier',
                'public.command_idempotency',
                'TRUNCATE'
            ) AS verifier_idempotency_mutate
        FROM pg_catalog.pg_proc procedure
        WHERE procedure.oid = pg_catalog.to_regprocedure(
            'public.authorize_case_agent_verification_snapshot(uuid,uuid,uuid,integer)'
        )
        """
    ).fetchone()
    if (
        row is None
        or row["owner_name"] != "lawcase_schema_owner"
        or not bool(row["security_definer"])
        or not bool(row["verifier_execute"])
        or bool(row["worker_execute"])
        or bool(row["web_execute"])
        or not bool(row["verifier_outbox_insert"])
        or not bool(row["verifier_idempotency_insert"])
        or bool(row["verifier_outbox_mutate"])
        or bool(row["verifier_idempotency_mutate"])
    ):
        raise CaseAgentRuntimePersistenceBlocked(
            "0060 verifier snapshot authority is missing or not least privilege"
        )


def _require_active_plan_execution_security_contract(connection: Any) -> None:
    ownership = connection.execute(
        """
        SELECT
            pg_catalog.pg_get_userbyid(execution_table.relowner)
                AS execution_table_owner,
            pg_catalog.pg_get_userbyid(goal_table.relowner) AS goal_table_owner
        FROM pg_catalog.pg_class execution_table
        JOIN pg_catalog.pg_namespace execution_namespace
          ON execution_namespace.oid = execution_table.relnamespace
        JOIN pg_catalog.pg_class goal_table
          ON goal_table.relname = 'case_agent_goals'
        JOIN pg_catalog.pg_namespace goal_namespace
          ON goal_namespace.oid = goal_table.relnamespace
         AND goal_namespace.nspname = 'public'
        WHERE execution_namespace.nspname = 'public'
          AND execution_table.relname = 'case_agent_active_plan_execution_runs'
          AND execution_table.relkind = 'r'
        """
    ).fetchone()
    if (
        ownership is None
        or ownership["execution_table_owner"] != "lawcase_schema_owner"
        or ownership["goal_table_owner"] != "lawcase_schema_owner"
    ):
        raise CaseAgentRuntimePersistenceBlocked(
            "0053 active-plan execution tables require the managed schema owner"
        )

    function_rows = connection.execute(
        """
        SELECT procedure.proname,
               pg_catalog.pg_get_userbyid(procedure.proowner) AS owner_name,
               procedure.prosecdef
        FROM pg_catalog.pg_proc procedure
        JOIN pg_catalog.pg_namespace namespace
          ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'public'
          AND procedure.oid = ANY(ARRAY[
              pg_catalog.to_regprocedure(
                  'public.case_agent_requested_deliverables_valid(jsonb)'
              ),
              pg_catalog.to_regprocedure(
                  'public.case_agent_active_plan_execution_valid(jsonb,jsonb)'
              ),
              pg_catalog.to_regprocedure(
                  'public.guard_case_agent_active_plan_execution_run()'
              )
          ])
        ORDER BY procedure.proname
        """
    ).fetchall()
    function_security = {
        str(row["proname"]): (str(row["owner_name"]), bool(row["prosecdef"]))
        for row in function_rows
    }
    expected_function_security = {
        "case_agent_active_plan_execution_valid": (
            "lawcase_schema_owner",
            False,
        ),
        "case_agent_requested_deliverables_valid": (
            "lawcase_schema_owner",
            False,
        ),
        "guard_case_agent_active_plan_execution_run": (
            "lawcase_schema_owner",
            True,
        ),
    }
    if function_security != expected_function_security:
        raise CaseAgentRuntimePersistenceBlocked(
            "0053 active-plan execution functions have unsafe owner or definer mode"
        )

    privileges = connection.execute(
        """
        SELECT
            pg_catalog.has_table_privilege(
                'lawcase_web_application',
                'public.case_agent_active_plan_execution_runs', 'SELECT'
            ) AS web_execution_select,
            pg_catalog.has_table_privilege(
                'lawcase_web_application',
                'public.case_agent_active_plan_execution_runs', 'INSERT'
            ) AS web_execution_insert,
            pg_catalog.has_table_privilege(
                'lawcase_web_application',
                'public.case_agent_active_plan_execution_runs',
                'UPDATE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_web_application',
                'public.case_agent_active_plan_execution_runs',
                'DELETE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_web_application',
                'public.case_agent_active_plan_execution_runs',
                'TRUNCATE'
            ) AS web_execution_mutate,
            pg_catalog.has_table_privilege(
                'lawcase_agent_worker',
                'public.case_agent_active_plan_execution_runs', 'SELECT'
            ) AS worker_execution_select,
            pg_catalog.has_table_privilege(
                'lawcase_agent_worker',
                'public.case_agent_active_plan_execution_runs',
                'INSERT'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_agent_worker',
                'public.case_agent_active_plan_execution_runs',
                'UPDATE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_agent_worker',
                'public.case_agent_active_plan_execution_runs',
                'DELETE'
            ) OR pg_catalog.has_table_privilege(
                'lawcase_agent_worker',
                'public.case_agent_active_plan_execution_runs',
                'TRUNCATE'
            ) AS worker_execution_mutate,
            pg_catalog.has_table_privilege(
                'lawcase_web_application', 'public.case_work_plan_heads', 'SELECT'
            ) AND pg_catalog.has_table_privilege(
                'lawcase_web_application', 'public.case_work_plans', 'SELECT'
            ) AND pg_catalog.has_table_privilege(
                'lawcase_web_application', 'public.case_work_plan_items', 'SELECT'
            ) AND pg_catalog.has_table_privilege(
                'lawcase_web_application',
                'public.case_agent_work_plan_promotions', 'SELECT'
            ) AS web_active_plan_read,
            pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'goal_id', 'SELECT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'firm_id', 'SELECT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'matter_id', 'SELECT'
            ) AS web_goal_identity_read,
            pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.matters', 'version', 'UPDATE'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_runs', 'run_id', 'UPDATE'
            ) AS web_lock_entitlements,
            pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'requested_deliverables', 'SELECT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'requested_deliverables', 'INSERT'
            ) AS web_requested_deliverables_write,
            pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'active_plan_execution', 'SELECT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'active_plan_execution', 'INSERT'
            ) AS web_active_execution_write,
            pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'requested_deliverables', 'UPDATE'
            ) OR pg_catalog.has_column_privilege(
                'lawcase_web_application', 'public.case_agent_goals',
                'active_plan_execution', 'UPDATE'
            ) AS web_goal_extension_update,
            pg_catalog.has_column_privilege(
                'lawcase_agent_worker', 'public.case_agent_goals',
                'requested_deliverables', 'SELECT'
            ) AND pg_catalog.has_column_privilege(
                'lawcase_agent_worker', 'public.case_agent_goals',
                'active_plan_execution', 'SELECT'
            ) AS worker_goal_extension_select,
            pg_catalog.has_column_privilege(
                'lawcase_agent_worker', 'public.case_agent_goals',
                'requested_deliverables', 'INSERT'
            ) OR pg_catalog.has_column_privilege(
                'lawcase_agent_worker', 'public.case_agent_goals',
                'requested_deliverables', 'UPDATE'
            ) OR pg_catalog.has_column_privilege(
                'lawcase_agent_worker', 'public.case_agent_goals',
                'active_plan_execution', 'INSERT'
            ) OR pg_catalog.has_column_privilege(
                'lawcase_agent_worker', 'public.case_agent_goals',
                'active_plan_execution', 'UPDATE'
            ) AS worker_goal_extension_write,
            pg_catalog.has_function_privilege(
                'lawcase_web_application',
                'public.case_agent_requested_deliverables_valid(jsonb)',
                'EXECUTE'
            ) AND pg_catalog.has_function_privilege(
                'lawcase_web_application',
                'public.case_agent_active_plan_execution_valid(jsonb,jsonb)',
                'EXECUTE'
            ) AS web_validation_execute,
            pg_catalog.has_function_privilege(
                'lawcase_web_application',
                'public.guard_case_agent_active_plan_execution_run()',
                'EXECUTE'
            ) AS web_guard_execute,
            pg_catalog.has_function_privilege(
                'lawcase_agent_worker',
                'public.case_agent_requested_deliverables_valid(jsonb)',
                'EXECUTE'
            ) OR pg_catalog.has_function_privilege(
                'lawcase_agent_worker',
                'public.case_agent_active_plan_execution_valid(jsonb,jsonb)',
                'EXECUTE'
            ) OR pg_catalog.has_function_privilege(
                'lawcase_agent_worker',
                'public.guard_case_agent_active_plan_execution_run()',
                'EXECUTE'
            ) AS worker_0053_function_execute
        """
    ).fetchone()
    if (
        privileges is None
        or not bool(privileges["web_execution_select"])
        or not bool(privileges["web_execution_insert"])
        or bool(privileges["web_execution_mutate"])
        or not bool(privileges["worker_execution_select"])
        or bool(privileges["worker_execution_mutate"])
        or not bool(privileges["web_active_plan_read"])
        or not bool(privileges["web_goal_identity_read"])
        or not bool(privileges["web_lock_entitlements"])
        or not bool(privileges["web_requested_deliverables_write"])
        or not bool(privileges["web_active_execution_write"])
        or bool(privileges["web_goal_extension_update"])
        or not bool(privileges["worker_goal_extension_select"])
        or bool(privileges["worker_goal_extension_write"])
        or not bool(privileges["web_validation_execute"])
        or bool(privileges["web_guard_execute"])
        or bool(privileges["worker_0053_function_execute"])
    ):
        raise CaseAgentRuntimePersistenceBlocked(
            "0053 active-plan execution runtime ACL is not least privilege"
        )


def _require_verifier_runtime_access(connection: Any, *, firm_id: str) -> None:
    """Exercise only the verifier's independent control/artifact surface.

    The execution principal proves the full migration contract above.  The
    verifier DSN must independently read run lineage and candidates and must
    be able to append attempt/receipt/event/checkpoint/audit rows during real
    verification, but it need not receive case-ledger, evidence-source or
    working-memory privileges merely to pass startup.
    """

    for table_name in (
        "case_agent_runs",
        "case_agent_events",
        "case_agent_task_graphs",
        "case_agent_tasks",
        "case_agent_task_receipts",
        "case_agent_artifacts",
        "case_agent_review_candidates",
        "case_agent_verification_attempts",
        "case_agent_verification_receipts",
        "case_agent_checkpoints",
        "case_agent_command_audits",
    ):
        connection.execute(
            f"SELECT 1 FROM {table_name} WHERE firm_id = %s LIMIT 1",
            (firm_id,),
        ).fetchall()
    privilege = connection.execute(
        """
        SELECT
            pg_catalog.has_function_privilege(
                current_user,
                'public.authorize_case_agent_verification_snapshot(uuid,uuid,uuid,integer)',
                'EXECUTE'
            ) AS verifier_snapshot_execute,
            pg_catalog.has_column_privilege(
                current_user, 'public.outbox_events', 'firm_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.outbox_events', 'matter_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.outbox_events', 'aggregate_version', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.outbox_events', 'event_type', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.outbox_events', 'payload', 'INSERT'
            ) AS verifier_outbox_insert,
            pg_catalog.has_column_privilege(
                current_user, 'public.command_idempotency', 'firm_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.command_idempotency', 'matter_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.command_idempotency', 'actor_id', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.command_idempotency', 'command_name', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.command_idempotency', 'idempotency_key', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.command_idempotency', 'request_hash', 'INSERT'
            ) AND pg_catalog.has_column_privilege(
                current_user, 'public.command_idempotency', 'response_json', 'INSERT'
            ) AS verifier_idempotency_insert
        """
    ).fetchone()
    if (
        privilege is None
        or not bool(privilege["verifier_snapshot_execute"])
        or not bool(privilege["verifier_outbox_insert"])
        or not bool(privilege["verifier_idempotency_insert"])
    ):
        raise CaseAgentRuntimePersistenceBlocked(
            "verifier cannot execute the governed snapshot and append boundary"
        )


def _require_preflight_principal(
    connection: Any,
    *,
    firm_id: str,
    actor_id: str,
    label: str,
) -> None:
    row = connection.execute(
        """
        SELECT principal.user_id, principal.firm_id, principal.status,
               EXISTS (
                   SELECT 1
                   FROM matter_actor_roles role
                   WHERE role.user_id = principal.user_id
                     AND role.firm_id = principal.firm_id
                     AND role.revoked_at IS NULL
                     AND role.role <> 'SYSTEM_WORKER'
               ) AS has_active_non_worker_role
        FROM users principal
        WHERE principal.user_id = %s AND principal.firm_id = %s
        """,
        (actor_id, firm_id),
    ).fetchone()
    if (
        row is None
        or str(row["user_id"]) != actor_id
        or str(row["firm_id"]) != firm_id
        or row["status"] != "ACTIVE"
        or bool(row["has_active_non_worker_role"])
    ):
        raise CaseAgentRuntimePersistenceBlocked(
            f"case Agent {label} principal is not a dedicated active SYSTEM_WORKER"
        )


def _require_all_firm_matters_bound(
    connection: Any,
    *,
    firm_id: str,
    execution_actor_id: str,
    verifier_actor_id: str,
) -> None:
    """Reject a paper-ready firm with any matter the runtime cannot claim.

    New matters receive both grants atomically in ``PostgresMatterStore``.  A
    deployment may nevertheless contain pre-existing matters created before
    that contract.  Those matters require an explicit administrative backfill;
    startup must not hide them behind a fresh process heartbeat.
    """

    row = connection.execute(
        """
        SELECT count(*) AS unbound_matter_count
        FROM matters matter
        WHERE matter.firm_id = %s
          AND (
            NOT EXISTS (
                SELECT 1
                FROM matter_actor_roles role
                JOIN users principal
                  ON principal.user_id = role.user_id
                 AND principal.firm_id = role.firm_id
                WHERE role.matter_id = matter.matter_id
                  AND role.firm_id = matter.firm_id
                  AND role.user_id = %s
                  AND role.role = 'SYSTEM_WORKER'
                  AND role.revoked_at IS NULL
                  AND principal.status = 'ACTIVE'
            )
            OR NOT EXISTS (
                SELECT 1
                FROM matter_actor_roles role
                JOIN users principal
                  ON principal.user_id = role.user_id
                 AND principal.firm_id = role.firm_id
                WHERE role.matter_id = matter.matter_id
                  AND role.firm_id = matter.firm_id
                  AND role.user_id = %s
                  AND role.role = 'SYSTEM_WORKER'
                  AND role.revoked_at IS NULL
                  AND principal.status = 'ACTIVE'
            )
          )
        """,
        (firm_id, execution_actor_id, verifier_actor_id),
    ).fetchone()
    if row is None or int(row["unbound_matter_count"]) != 0:
        raise CaseAgentRuntimePersistenceBlocked(
            "one or more matters are not bound to the configured case Agent principals"
        )


class PostgresCaseAgentMatterPrincipalReadiness:
    """Firm readiness for the exact server-owned execution/verifier pair.

    This small adapter deliberately lives outside the event store.  Web
    readiness may inspect service-principal provisioning, but the browser
    cannot choose either identity and no generic heartbeat can substitute for
    the exact pair or for matter-level grants.
    """

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent readiness PostgreSQL DSN is required")
        self._dsn = dsn

    def __call__(
        self,
        *,
        firm_id: str,
        execution_actor_id: str,
        verifier_actor_id: str,
    ) -> bool:
        try:
            _uuid(firm_id, "firm_id")
            _uuid(execution_actor_id, "execution_actor_id")
            _uuid(verifier_actor_id, "verifier_actor_id")
            if execution_actor_id == verifier_actor_id:
                return False
            with _runtime_preflight_connection(self._dsn, firm_id) as connection:
                _require_preflight_principal(
                    connection,
                    firm_id=firm_id,
                    actor_id=execution_actor_id,
                    label="execution",
                )
                _require_preflight_principal(
                    connection,
                    firm_id=firm_id,
                    actor_id=verifier_actor_id,
                    label="verifier",
                )
                _require_all_firm_matters_bound(
                    connection,
                    firm_id=firm_id,
                    execution_actor_id=execution_actor_id,
                    verifier_actor_id=verifier_actor_id,
                )
            return True
        except Exception:
            return False


class PostgresCaseAgentRunnerIncidentSink:
    """Append sanitized runner incidents to the existing control-plane audit.

    Only an allowlisted-shaped code and opaque UUIDs are stored.  Exception
    text, case content, paths, object keys and provider payloads never cross
    this boundary.  The deterministic request hash makes crash/retry inserts
    idempotent without pretending that an incident changed the run state.
    """

    def __init__(self, *, dsn: str, actor: Actor) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent incident PostgreSQL DSN is required")
        _worker_actor(actor)
        self._dsn = dsn
        self._actor = actor

    def record(
        self,
        *,
        firm_id: str,
        worker_id: str,
        run_id: str | None,
        matter_id: str | None,
        code: str,
        occurred_at: datetime,
    ) -> None:
        _uuid(firm_id, "firm_id")
        if firm_id != self._actor.firm_id:
            raise PermissionError("runner incident belongs to another firm")
        _code(worker_id, "worker_id")
        if run_id is None or matter_id is None:
            raise CaseAgentRuntimePersistenceBlocked(
                "run and matter are required for a durable runner incident"
            )
        _uuid(run_id, "run_id")
        _uuid(matter_id, "matter_id")
        if not isinstance(code, str) or _INCIDENT_CODE.fullmatch(code) is None:
            raise CaseAgentRuntimePersistenceBlocked("runner incident code is invalid")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise CaseAgentRuntimePersistenceBlocked(
                "runner incident time must be timezone-aware"
            )
        request_hash = sha256(
            json.dumps(
                {
                    "schema_version": "case-agent-runner-incident-v1",
                    "firm_id": firm_id,
                    "worker_id": worker_id,
                    "run_id": run_id,
                    "matter_id": matter_id,
                    "code": code,
                    "occurred_at": occurred_at.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        with _tenant_transaction(self._dsn, firm_id, read_only=False) as connection:
            persisted = connection.execute(
                """
                INSERT INTO case_agent_command_audits (
                    run_id, firm_id, matter_id, actor_id, command_name,
                    request_hash, input_event_version, output_event_version,
                    event_id, payload, occurred_at
                )
                SELECT run.run_id, run.firm_id, run.matter_id, %s,
                       'CASE_AGENT_RUNNER_INCIDENT', %s,
                       run.current_event_version, run.current_event_version,
                       NULL, %s::jsonb, %s
                FROM case_agent_runs run
                JOIN matter_actor_roles worker_role
                  ON worker_role.matter_id = run.matter_id
                 AND worker_role.firm_id = run.firm_id
                 AND worker_role.user_id = %s
                 AND worker_role.role = 'SYSTEM_WORKER'
                 AND worker_role.revoked_at IS NULL
                JOIN users worker_user
                  ON worker_user.user_id = worker_role.user_id
                 AND worker_user.firm_id = worker_role.firm_id
                 AND worker_user.status = 'ACTIVE'
                WHERE run.run_id = %s AND run.firm_id = %s
                  AND run.matter_id = %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_command_audits audit
                    WHERE audit.run_id = run.run_id
                      AND audit.firm_id = run.firm_id
                      AND audit.matter_id = run.matter_id
                      AND audit.command_name = 'CASE_AGENT_RUNNER_INCIDENT'
                      AND audit.request_hash = %s
                  )
                """,
                (
                    self._actor.actor_id,
                    request_hash,
                    json.dumps(
                        {"code": code, "worker_id": worker_id},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    occurred_at,
                    self._actor.actor_id,
                    run_id,
                    firm_id,
                    matter_id,
                    request_hash,
                ),
            )
            if persisted.rowcount not in {0, 1}:
                raise CaseAgentRuntimePersistenceBlocked(
                    "runner incident persistence returned an invalid result"
                )
            if persisted.rowcount == 0:
                authorized = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM case_agent_command_audits
                        WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                          AND command_name = 'CASE_AGENT_RUNNER_INCIDENT'
                          AND request_hash = %s
                    ) AS already_recorded
                    """,
                    (run_id, firm_id, matter_id, request_hash),
                ).fetchone()
                if authorized is None or not bool(authorized["already_recorded"]):
                    raise CaseAgentRuntimePersistenceBlocked(
                        "runner incident run or SYSTEM_WORKER binding is unavailable"
                    )


@dataclass(frozen=True)
class AgentRunInboxClaim:
    run_id: str
    firm_id: str
    matter_id: str
    observed_event_version: int
    inbox_version: int
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime

    def validate(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("firm_id", self.firm_id),
            ("matter_id", self.matter_id),
            ("lease_token", self.lease_token),
        ):
            _uuid(value, label)
        _code(self.lease_owner, "lease_owner")
        if self.observed_event_version < 1 or self.inbox_version < 1:
            raise CaseAgentRuntimePersistenceBlocked("inbox versions must be positive")
        if (
            self.lease_expires_at.tzinfo is None
            or self.lease_expires_at.utcoffset() is None
        ):
            raise CaseAgentRuntimePersistenceBlocked("inbox lease expiry must be timezone-aware")


class CaseAgentRunInbox(Protocol):
    def claim_next_run(self, *, lease_owner: str, lease_seconds: int) -> AgentRunInboxClaim | None: ...

    def settle_run(
        self,
        claim: AgentRunInboxClaim,
        *,
        quiet: bool,
        retry_after_seconds: int = 0,
    ) -> bool: ...


class PostgresCaseAgentRunInbox:
    """Fair, restart-safe run wake projection for exactly one firm Worker."""

    def __init__(self, *, dsn: str, actor: Actor) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent Worker PostgreSQL DSN is required")
        _worker_actor(actor)
        self._dsn = dsn
        self._actor = actor

    def claim_next_run(
        self, *, lease_owner: str, lease_seconds: int = 120
    ) -> AgentRunInboxClaim | None:
        _code(lease_owner, "lease_owner")
        if not 30 <= lease_seconds <= 900:
            raise ValueError("run inbox lease must be between 30 and 900 seconds")
        token = str(uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        with self._transaction() as connection:
            candidate = connection.execute(
                """
                SELECT inbox.run_id, inbox.matter_id
                FROM case_agent_run_inbox inbox
                JOIN matter_actor_roles role
                  ON role.matter_id = inbox.matter_id
                 AND role.firm_id = inbox.firm_id
                 AND role.user_id = %s
                 AND role.role = 'SYSTEM_WORKER'
                 AND role.revoked_at IS NULL
                JOIN users worker
                  ON worker.user_id = role.user_id
                 AND worker.firm_id = role.firm_id
                 AND worker.status = 'ACTIVE'
                WHERE inbox.firm_id = %s
                  AND inbox.available_at <= now()
                  AND (
                    inbox.inbox_status = 'READY'
                    OR (inbox.inbox_status = 'LEASED'
                        AND inbox.lease_expires_at <= now())
                  )
                  -- A pre-0050 recovery run discovered during upgrade is an
                  -- immutable quarantine, never ordinary queue work.
                  AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_ledger_exception_recovery_quarantines
                          quarantine
                    WHERE quarantine.run_id = inbox.run_id
                      AND quarantine.firm_id = inbox.firm_id
                      AND quarantine.matter_id = inbox.matter_id
                  )
                  -- Old Web code may have begun RUN_CREATED before the 0050
                  -- scan and commit after cutover.  Its exact server-owned
                  -- recovery shape remains permanently fenced unless a
                  -- durable intent exists; ACTIVE follow-up state is not the
                  -- safety boundary.
                  AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_runs legacy_recovery_run
                    JOIN case_agent_goals legacy_recovery_goal
                      ON legacy_recovery_goal.goal_id =
                            legacy_recovery_run.goal_id
                     AND legacy_recovery_goal.firm_id =
                            legacy_recovery_run.firm_id
                     AND legacy_recovery_goal.matter_id =
                            legacy_recovery_run.matter_id
                    WHERE legacy_recovery_run.run_id = inbox.run_id
                      AND legacy_recovery_run.firm_id = inbox.firm_id
                      AND legacy_recovery_run.matter_id = inbox.matter_id
                      AND legacy_recovery_goal.objective =
                        '恢复本案异常材料后续工作并基于当前权威台账继续研判'
                      AND legacy_recovery_goal.success_criteria =
                        pg_catalog.jsonb_build_array(
                          '接管全部待完成异常分流工作',
                          '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                          '全部后续工作完成后基于当前案件版本重新规划'
                        )
                      AND legacy_recovery_goal.constraints =
                        pg_catalog.jsonb_build_array(
                          '不得自动确认正式事实、法律口径或对外提交',
                          '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
                        )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM case_agent_ledger_exception_recovery_intents intent
                        WHERE intent.replacement_run_id = inbox.run_id
                          AND intent.firm_id = inbox.firm_id
                          AND intent.matter_id = inbox.matter_id
                      )
                  )
                  -- An intent run is never claimable merely because ACTIVE
                  -- follow-ups later disappeared.  Its current outcome and
                  -- the authoritative control head must both prove transfer.
                  AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_ledger_exception_recovery_intents intent
                    JOIN case_agent_ledger_exception_recovery_intent_heads
                          intent_head
                      ON intent_head.recovery_intent_id =
                            intent.recovery_intent_id
                     AND intent_head.firm_id = intent.firm_id
                     AND intent_head.matter_id = intent.matter_id
                    WHERE intent.firm_id = inbox.firm_id
                      AND intent.matter_id = inbox.matter_id
                      AND intent.replacement_run_id = inbox.run_id
                      AND (
                        intent_head.current_outcome <> 'TRANSFERRED'
                        OR NOT EXISTS (
                            SELECT 1
                            FROM case_agent_runs recovery_run
                            JOIN case_agent_goals recovery_goal
                              ON recovery_goal.goal_id = recovery_run.goal_id
                             AND recovery_goal.firm_id = recovery_run.firm_id
                             AND recovery_goal.matter_id = recovery_run.matter_id
                            WHERE recovery_run.run_id = intent.replacement_run_id
                              AND recovery_run.firm_id = intent.firm_id
                              AND recovery_run.matter_id = intent.matter_id
                              AND recovery_run.goal_id = intent.recovery_goal_id
                              AND recovery_run.created_by = intent.actor_id
                              AND recovery_goal.goal_hash =
                                    intent.recovery_goal_hash
                              AND recovery_goal.requested_by = intent.actor_id
                              AND recovery_goal.objective =
                                '恢复本案异常材料后续工作并基于当前权威台账继续研判'
                              AND recovery_goal.success_criteria =
                                pg_catalog.jsonb_build_array(
                                  '接管全部待完成异常分流工作',
                                  '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                                  '全部后续工作完成后基于当前案件版本重新规划'
                                )
                              AND recovery_goal.constraints =
                                pg_catalog.jsonb_build_array(
                                  '不得自动确认正式事实、法律口径或对外提交',
                                  '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
                                )
                        )
                        OR NOT EXISTS (
                            SELECT 1
                            FROM case_agent_ledger_exception_control_heads
                                  intent_control_head
                            JOIN case_agent_ledger_exception_control_assignments
                                  intent_assignment
                              ON intent_assignment.control_assignment_id =
                                    intent_control_head.current_control_assignment_id
                             AND intent_assignment.firm_id =
                                    intent_control_head.firm_id
                             AND intent_assignment.matter_id =
                                    intent_control_head.matter_id
                             AND intent_assignment.state_after =
                                    intent_control_head.current_state
                             AND intent_assignment.assignment_sequence =
                                    intent_control_head.head_sequence
                            WHERE intent_control_head.firm_id = intent.firm_id
                              AND intent_control_head.matter_id = intent.matter_id
                              AND intent_control_head.current_state = 'HEALTHY'
                              AND intent_control_head.current_control_assignment_id =
                                    intent_head.transfer_control_assignment_id
                              AND intent_assignment.control_run_id = inbox.run_id
                        )
                      )
                  )
                  -- Once a run has been a governed control cursor it remains
                  -- non-runnable after supersession, even when all follow-ups
                  -- have reached terminal states.
                  AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_ledger_exception_control_assignments
                          historical_assignment
                    WHERE historical_assignment.firm_id = inbox.firm_id
                      AND historical_assignment.matter_id = inbox.matter_id
                      AND historical_assignment.control_run_id = inbox.run_id
                      AND NOT EXISTS (
                        SELECT 1
                        FROM case_agent_ledger_exception_control_heads
                              historical_control_head
                        JOIN case_agent_ledger_exception_control_assignments
                              current_assignment
                          ON current_assignment.control_assignment_id =
                                historical_control_head.current_control_assignment_id
                         AND current_assignment.firm_id =
                                historical_control_head.firm_id
                         AND current_assignment.matter_id =
                                historical_control_head.matter_id
                         AND current_assignment.state_after =
                                historical_control_head.current_state
                         AND current_assignment.assignment_sequence =
                                historical_control_head.head_sequence
                        WHERE historical_control_head.firm_id = inbox.firm_id
                          AND historical_control_head.matter_id = inbox.matter_id
                          AND historical_control_head.current_state = 'HEALTHY'
                          AND current_assignment.control_run_id = inbox.run_id
                      )
                  )
                  -- A materially missing source or a required re-extraction
                  -- still governs execution. A lawyer's DEFERRED_REVIEW,
                  -- however, records an excluded candidate for the material
                  -- review queue; it is not a factual premise and must not
                  -- strand an otherwise source-bounded analysis run.
                  AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_ledger_exception_followup_heads followup_head
                    JOIN case_agent_ledger_exception_followups followup
                      ON followup.followup_id = followup_head.followup_id
                     AND followup.firm_id = followup_head.firm_id
                     AND followup.matter_id = followup_head.matter_id
                    JOIN case_agent_ledger_exception_control_heads control_head
                      ON control_head.firm_id = followup_head.firm_id
                     AND control_head.matter_id = followup_head.matter_id
                    JOIN case_agent_ledger_exception_control_assignments
                          control_assignment
                      ON control_assignment.control_assignment_id =
                            control_head.current_control_assignment_id
                     AND control_assignment.firm_id = control_head.firm_id
                     AND control_assignment.matter_id = control_head.matter_id
                     AND control_assignment.state_after =
                            control_head.current_state
                     AND control_assignment.assignment_sequence =
                            control_head.head_sequence
                    WHERE followup_head.firm_id = inbox.firm_id
                      AND followup_head.matter_id = inbox.matter_id
                      AND followup_head.current_state = 'ACTIVE'
                      AND followup.followup_kind IN ('REEXTRACTION', 'MORE_EVIDENCE')
                      AND (
                        control_head.current_state <> 'HEALTHY'
                        OR (control_assignment.control_run_id <> inbox.run_id
                          AND NOT public.case_agent_is_bounded_pending_review_analysis(
                              inbox.run_id, inbox.firm_id, inbox.matter_id))
                      )
                  )
                ORDER BY inbox.available_at, inbox.updated_at, inbox.run_id
                LIMIT 1
                """,
                (self._actor.actor_id, self._actor.firm_id),
            ).fetchone()
            if candidate is None:
                return None

            # Transfer takes the same transaction-scoped lock before it
            # touches either inbox.  Whichever transaction wins is fully
            # ordered: a winning claim makes transfer reject an unexpired
            # lease; a winning transfer parks the old run before recheck.
            connection.execute(
                """
                SELECT pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        %s::text || '|' || %s::text ||
                        '|CASE_LEDGER_EXCEPTION_CONTROL_CLAIM',
                        0
                    )
                )
                """,
                (self._actor.firm_id, str(candidate["matter_id"])),
            )
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT inbox.run_id
                    FROM case_agent_run_inbox inbox
                    JOIN matter_actor_roles role
                      ON role.matter_id = inbox.matter_id
                     AND role.firm_id = inbox.firm_id
                     AND role.user_id = %s
                     AND role.role = 'SYSTEM_WORKER'
                     AND role.revoked_at IS NULL
                    JOIN users worker
                      ON worker.user_id = role.user_id
                     AND worker.firm_id = role.firm_id
                     AND worker.status = 'ACTIVE'
                    WHERE inbox.run_id = %s
                      AND inbox.matter_id = %s
                      AND inbox.firm_id = %s
                      AND inbox.available_at <= now()
                      AND (
                        inbox.inbox_status = 'READY'
                        OR (inbox.inbox_status = 'LEASED'
                            AND inbox.lease_expires_at <= now())
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM case_agent_ledger_exception_recovery_quarantines
                              quarantine
                        WHERE quarantine.run_id = inbox.run_id
                          AND quarantine.firm_id = inbox.firm_id
                          AND quarantine.matter_id = inbox.matter_id
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM case_agent_runs legacy_recovery_run
                        JOIN case_agent_goals legacy_recovery_goal
                          ON legacy_recovery_goal.goal_id =
                                legacy_recovery_run.goal_id
                         AND legacy_recovery_goal.firm_id =
                                legacy_recovery_run.firm_id
                         AND legacy_recovery_goal.matter_id =
                                legacy_recovery_run.matter_id
                        WHERE legacy_recovery_run.run_id = inbox.run_id
                          AND legacy_recovery_run.firm_id = inbox.firm_id
                          AND legacy_recovery_run.matter_id = inbox.matter_id
                          AND legacy_recovery_goal.objective =
                            '恢复本案异常材料后续工作并基于当前权威台账继续研判'
                          AND legacy_recovery_goal.success_criteria =
                            pg_catalog.jsonb_build_array(
                              '接管全部待完成异常分流工作',
                              '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                              '全部后续工作完成后基于当前案件版本重新规划'
                            )
                          AND legacy_recovery_goal.constraints =
                            pg_catalog.jsonb_build_array(
                              '不得自动确认正式事实、法律口径或对外提交',
                              '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
                            )
                          AND NOT EXISTS (
                            SELECT 1
                            FROM case_agent_ledger_exception_recovery_intents intent
                            WHERE intent.replacement_run_id = inbox.run_id
                              AND intent.firm_id = inbox.firm_id
                              AND intent.matter_id = inbox.matter_id
                          )
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM case_agent_ledger_exception_recovery_intents intent
                        JOIN case_agent_ledger_exception_recovery_intent_heads
                              intent_head
                          ON intent_head.recovery_intent_id =
                                intent.recovery_intent_id
                         AND intent_head.firm_id = intent.firm_id
                         AND intent_head.matter_id = intent.matter_id
                        WHERE intent.firm_id = inbox.firm_id
                          AND intent.matter_id = inbox.matter_id
                          AND intent.replacement_run_id = inbox.run_id
                          AND (
                            intent_head.current_outcome <> 'TRANSFERRED'
                            OR NOT EXISTS (
                                SELECT 1
                                FROM case_agent_runs recovery_run
                                JOIN case_agent_goals recovery_goal
                                  ON recovery_goal.goal_id = recovery_run.goal_id
                                 AND recovery_goal.firm_id = recovery_run.firm_id
                                 AND recovery_goal.matter_id = recovery_run.matter_id
                                WHERE recovery_run.run_id =
                                        intent.replacement_run_id
                                  AND recovery_run.firm_id = intent.firm_id
                                  AND recovery_run.matter_id = intent.matter_id
                                  AND recovery_run.goal_id =
                                        intent.recovery_goal_id
                                  AND recovery_run.created_by = intent.actor_id
                                  AND recovery_goal.goal_hash =
                                        intent.recovery_goal_hash
                                  AND recovery_goal.requested_by = intent.actor_id
                                  AND recovery_goal.objective =
                                    '恢复本案异常材料后续工作并基于当前权威台账继续研判'
                                  AND recovery_goal.success_criteria =
                                    pg_catalog.jsonb_build_array(
                                      '接管全部待完成异常分流工作',
                                      '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                                      '全部后续工作完成后基于当前案件版本重新规划'
                                    )
                                  AND recovery_goal.constraints =
                                    pg_catalog.jsonb_build_array(
                                      '不得自动确认正式事实、法律口径或对外提交',
                                      '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
                                    )
                            )
                            OR NOT EXISTS (
                                SELECT 1
                                FROM case_agent_ledger_exception_control_heads
                                      intent_control_head
                                JOIN case_agent_ledger_exception_control_assignments
                                      intent_assignment
                                  ON intent_assignment.control_assignment_id =
                                        intent_control_head.current_control_assignment_id
                                 AND intent_assignment.firm_id =
                                        intent_control_head.firm_id
                                 AND intent_assignment.matter_id =
                                        intent_control_head.matter_id
                                 AND intent_assignment.state_after =
                                        intent_control_head.current_state
                                 AND intent_assignment.assignment_sequence =
                                        intent_control_head.head_sequence
                                WHERE intent_control_head.firm_id = intent.firm_id
                                  AND intent_control_head.matter_id = intent.matter_id
                                  AND intent_control_head.current_state = 'HEALTHY'
                                  AND intent_control_head.current_control_assignment_id =
                                        intent_head.transfer_control_assignment_id
                                  AND intent_assignment.control_run_id = inbox.run_id
                            )
                          )
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM case_agent_ledger_exception_control_assignments
                              historical_assignment
                        WHERE historical_assignment.firm_id = inbox.firm_id
                          AND historical_assignment.matter_id = inbox.matter_id
                          AND historical_assignment.control_run_id = inbox.run_id
                          AND NOT EXISTS (
                            SELECT 1
                            FROM case_agent_ledger_exception_control_heads
                                  historical_control_head
                            JOIN case_agent_ledger_exception_control_assignments
                                  current_assignment
                              ON current_assignment.control_assignment_id =
                                    historical_control_head.current_control_assignment_id
                             AND current_assignment.firm_id =
                                    historical_control_head.firm_id
                             AND current_assignment.matter_id =
                                    historical_control_head.matter_id
                             AND current_assignment.state_after =
                                    historical_control_head.current_state
                             AND current_assignment.assignment_sequence =
                                    historical_control_head.head_sequence
                            WHERE historical_control_head.firm_id = inbox.firm_id
                              AND historical_control_head.matter_id = inbox.matter_id
                              AND historical_control_head.current_state = 'HEALTHY'
                              AND current_assignment.control_run_id = inbox.run_id
                          )
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM case_agent_ledger_exception_followup_heads
                              followup_head
                        JOIN case_agent_ledger_exception_followups followup
                          ON followup.followup_id = followup_head.followup_id
                         AND followup.firm_id = followup_head.firm_id
                         AND followup.matter_id = followup_head.matter_id
                        JOIN case_agent_ledger_exception_control_heads
                              control_head
                          ON control_head.firm_id = followup_head.firm_id
                         AND control_head.matter_id = followup_head.matter_id
                        JOIN case_agent_ledger_exception_control_assignments
                              control_assignment
                          ON control_assignment.control_assignment_id =
                                control_head.current_control_assignment_id
                         AND control_assignment.firm_id = control_head.firm_id
                         AND control_assignment.matter_id = control_head.matter_id
                         AND control_assignment.state_after =
                                control_head.current_state
                         AND control_assignment.assignment_sequence =
                                control_head.head_sequence
                        WHERE followup_head.firm_id = inbox.firm_id
                          AND followup_head.matter_id = inbox.matter_id
                          AND followup_head.current_state = 'ACTIVE'
                          AND followup.followup_kind IN ('REEXTRACTION', 'MORE_EVIDENCE')
                          AND (
                            control_head.current_state <> 'HEALTHY'
                            OR (control_assignment.control_run_id <> inbox.run_id
                              AND NOT public.case_agent_is_bounded_pending_review_analysis(
                                  inbox.run_id, inbox.firm_id, inbox.matter_id))
                          )
                      )
                    FOR UPDATE OF inbox SKIP LOCKED
                )
                UPDATE case_agent_run_inbox inbox
                SET inbox_status = 'LEASED', lease_owner = %s, lease_token = %s,
                    lease_expires_at = %s, inbox_version = inbox.inbox_version + 1,
                    updated_at = now()
                FROM candidate
                WHERE inbox.run_id = candidate.run_id
                RETURNING inbox.run_id, inbox.firm_id, inbox.matter_id,
                          inbox.observed_event_version, inbox.inbox_version,
                          inbox.lease_owner, inbox.lease_token,
                          inbox.lease_expires_at
                """,
                (
                    self._actor.actor_id,
                    str(candidate["run_id"]),
                    str(candidate["matter_id"]),
                    self._actor.firm_id,
                    lease_owner,
                    token,
                    expires_at,
                ),
            ).fetchone()
        if row is None:
            return None
        claim = AgentRunInboxClaim(
            run_id=str(row["run_id"]),
            firm_id=str(row["firm_id"]),
            matter_id=str(row["matter_id"]),
            observed_event_version=int(row["observed_event_version"]),
            inbox_version=int(row["inbox_version"]),
            lease_owner=str(row["lease_owner"]),
            lease_token=str(row["lease_token"]),
            lease_expires_at=row["lease_expires_at"],
        )
        claim.validate()
        return claim

    def settle_run(
        self,
        claim: AgentRunInboxClaim,
        *,
        quiet: bool,
        retry_after_seconds: int = 0,
    ) -> bool:
        claim.validate()
        if claim.firm_id != self._actor.firm_id:
            raise PermissionError("run inbox claim belongs to another firm")
        if not isinstance(quiet, bool) or not 0 <= retry_after_seconds <= 3600:
            raise ValueError("run inbox settlement is invalid")
        if quiet and retry_after_seconds:
            raise ValueError("a quiet inbox cannot also request retry")
        status = "QUIET" if quiet else "READY"
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE case_agent_run_inbox
                SET inbox_status = %s,
                    available_at = now() + make_interval(secs => %s),
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    inbox_version = inbox_version + 1, updated_at = now()
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND inbox_status = 'LEASED' AND lease_owner = %s
                  AND lease_token = %s AND inbox_version = %s
                  AND observed_event_version = %s
                  AND lease_expires_at > now()
                """,
                (
                    status,
                    retry_after_seconds,
                    claim.run_id,
                    claim.firm_id,
                    claim.matter_id,
                    claim.lease_owner,
                    claim.lease_token,
                    claim.inbox_version,
                    claim.observed_event_version,
                ),
            )
            return updated.rowcount == 1

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self._actor.firm_id,),
            )
            yield connection


class PostgresEvidenceProjectionAuthorizationPort:
    """Resolve page refs against the exact current task and run creator."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        required_tool: str = "extract_pdf_text",
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent Worker PostgreSQL DSN is required")
        _worker_actor(worker_actor)
        if (
            not isinstance(required_tool, str)
            or required_tool != required_tool.strip()
            or not 1 <= len(required_tool) <= 200
            or any(ord(character) < 32 for character in required_tool)
        ):
            raise ValueError("evidence projection required tool is invalid")
        self._dsn = dsn
        self._worker = worker_actor
        # Fixed at server composition.  Neither a model nor browser can choose
        # which compiled tool binding this projection will accept.
        self._required_tool = required_tool

    def resolve_evidence_projection(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> EvidenceProjectionAuthorization:
        binding = self._resolve_task(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
            required_tool=self._required_tool,
        )
        page_ids = tuple(_opaque_ref(value, "evidence-page") for value in input_refs)
        roles = frozenset(Role(value) for value in binding["requested_by_roles"])
        actor = Actor(
            actor_id=binding["requested_by"],
            firm_id=self._worker.firm_id,
            roles=roles,
        )
        result = EvidenceProjectionAuthorization.build(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
            matter_id=binding["matter_id"],
            authorized_actor=actor,
            evidence_page_ids=page_ids,
        )
        result.validate()
        return result

    def _resolve_task(self, **kwargs: Any) -> dict[str, Any]:
        with _tenant_transaction(self._dsn, self._worker.firm_id, read_only=True) as connection:
            row = _read_current_task_binding(
                connection, worker=self._worker, include_human=True, **kwargs
            )
        return row


class PostgresLedgerExtractionProjectionPort:
    """Project native task pages plus OCR text from direct visual dependencies.

    Dependency artifacts are accepted only when the current graph records the
    direct edge, the visual task is SUCCEEDED, the task receipt and artifact
    row agree, the private object verifies, and the complete canonical visual
    candidate passes the same strict format verifier used at run completion.
    """

    _VISUAL_KIND = "VISUAL_PAGE_REVIEW_CANDIDATE"
    _VISUAL_SCHEMA = "agent-visual-page-candidate-bundle-v1"

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        native_projection_port: object,
        object_store: object,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent Worker PostgreSQL DSN is required")
        _worker_actor(worker_actor)
        if not callable(getattr(native_projection_port, "project_pdf_pages", None)):
            raise ValueError("native ledger projection port is invalid")
        if not callable(getattr(object_store, "read_case_agent_review_candidate", None)):
            raise ValueError("ledger dependency object store is invalid")
        self._dsn = dsn
        self._worker = worker_actor
        self._native = native_projection_port
        self._object_store = object_store
        self._visual_verifier = CanonicalJsonArtifactVerifier(
            artifact_kind=self._VISUAL_KIND,
            allowed_schema_versions=(self._VISUAL_SCHEMA,),
            payload_kind=self._VISUAL_KIND,
        )

    def __repr__(self) -> str:
        return "PostgresLedgerExtractionProjectionPort(<dependency-bound>)"

    def project_ledger_pages(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> tuple[LedgerExtractionPageProjection, ...]:
        with _tenant_transaction(
            self._dsn, self._worker.firm_id, read_only=True
        ) as connection:
            binding = _read_current_task_binding(
                connection,
                worker=self._worker,
                run_id=run_id,
                task_id=task_id,
                task_input_hash=task_input_hash,
                input_refs=input_refs,
                required_tool=DEEPSEEK_LEDGER_EXTRACTION_TOOL_ID,
                include_human=True,
            )
            visual_rows = connection.execute(
                """
                SELECT dependency_task.task_id, dependency_task.input_refs,
                       dependency_task.input_hash, head.status,
                       candidate.artifact_id, candidate.content_sha256,
                       candidate.byte_size, candidate.source_object_key,
                       candidate.source_object_version_id,
                       candidate.receipt_hash, receipt.receipt_id
                FROM case_agent_task_dependencies dependency
                JOIN case_agent_tasks dependency_task
                  ON dependency_task.graph_id = dependency.graph_id
                 AND dependency_task.task_id = dependency.dependency_task_id
                 AND dependency_task.run_id = dependency.run_id
                 AND dependency_task.firm_id = dependency.firm_id
                 AND dependency_task.matter_id = dependency.matter_id
                JOIN case_agent_task_heads head
                  ON head.graph_id = dependency_task.graph_id
                 AND head.task_id = dependency_task.task_id
                 AND head.run_id = dependency_task.run_id
                 AND head.firm_id = dependency_task.firm_id
                 AND head.matter_id = dependency_task.matter_id
                 AND head.is_current
                LEFT JOIN case_agent_review_candidates candidate
                  ON candidate.graph_id = dependency_task.graph_id
                 AND candidate.task_id = dependency_task.task_id
                 AND candidate.run_id = dependency_task.run_id
                 AND candidate.firm_id = dependency_task.firm_id
                 AND candidate.matter_id = dependency_task.matter_id
                 AND candidate.task_input_hash = dependency_task.input_hash
                 AND candidate.artifact_kind = %s
                 AND candidate.review_status = 'NEEDS_LAWYER_REVIEW'
                LEFT JOIN case_agent_artifacts artifact
                  ON artifact.artifact_id = candidate.artifact_id
                 AND artifact.run_id = candidate.run_id
                 AND artifact.firm_id = candidate.firm_id
                 AND artifact.matter_id = candidate.matter_id
                 AND artifact.artifact_kind = candidate.artifact_kind
                 AND artifact.content_hash = candidate.content_sha256
                 AND artifact.source_input_hash = candidate.task_input_hash
                LEFT JOIN case_agent_task_receipts receipt
                  ON receipt.receipt_id = artifact.receipt_id
                 AND receipt.run_id = artifact.run_id
                 AND receipt.task_id = dependency_task.task_id
                 AND receipt.firm_id = artifact.firm_id
                 AND receipt.matter_id = artifact.matter_id
                 AND receipt.input_hash = dependency_task.input_hash
                 AND receipt.result_status = 'SUCCEEDED'
                WHERE dependency.graph_id = %s
                  AND dependency.task_id = %s
                  AND dependency.run_id = %s
                  AND dependency.firm_id = %s
                  AND dependency.matter_id = %s
                  AND dependency_task.tool_id = 'understand_visual_page'
                ORDER BY dependency_task.task_id, candidate.artifact_id
                """,
                (
                    self._VISUAL_KIND,
                    binding["graph_id"],
                    task_id,
                    run_id,
                    self._worker.firm_id,
                    binding["matter_id"],
                ),
            ).fetchall()

        native_pages = self._native.project_pdf_pages(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
        )
        if not isinstance(native_pages, tuple) or len(native_pages) != len(input_refs):
            raise CaseAgentRuntimePersistenceBlocked(
                "native ledger projection differs from the compiled task"
            )
        projected: dict[str, LedgerExtractionPageProjection] = {}
        for input_ref, page in zip(input_refs, native_pages, strict=True):
            item = LedgerExtractionPageProjection(
                input_ref=input_ref,
                evidence_page_id=getattr(page, "evidence_page_id", ""),
                source_file_sha256=getattr(page, "source_file_sha256", ""),
                page_number=getattr(page, "page_number", 0),
                extracted_text=getattr(page, "extracted_text", None),
                extracted_text_sha256=getattr(page, "extracted_text_sha256", ""),
                source_mode=ExtractionSourceMode.NATIVE_TEXT,
            )
            item.validate()
            projected[input_ref] = item

        seen_dependency_tasks: set[str] = set()
        for row in visual_rows:
            dependency_task_id = str(row["task_id"])
            if dependency_task_id in seen_dependency_tasks:
                raise CaseAgentRuntimePersistenceBlocked(
                    "visual dependency produced ambiguous artifacts"
                )
            seen_dependency_tasks.add(dependency_task_id)
            if (
                row["status"] != "SUCCEEDED"
                or row["artifact_id"] is None
                or row["receipt_id"] is None
            ):
                raise CaseAgentRuntimePersistenceBlocked(
                    "visual dependency is not durably successful"
                )
            dependency_refs = tuple(row["input_refs"])
            if len(dependency_refs) != 1:
                raise CaseAgentRuntimePersistenceBlocked(
                    "visual dependency page binding is invalid"
                )
            dependency_ref = dependency_refs[0]
            stored = StoredCaseAgentReviewCandidate(
                object_key=row["source_object_key"],
                content_sha256=row["content_sha256"],
                byte_size=int(row["byte_size"]),
                object_version_id=row["source_object_version_id"],
            )
            try:
                content = self._object_store.read_case_agent_review_candidate(
                    stored, artifact_id=str(row["artifact_id"])
                )
            except Exception as error:
                raise CaseAgentRuntimePersistenceBlocked(
                    "visual dependency candidate could not be re-read"
                ) from error
            managed = ManagedArtifactRead(
                artifact_id=str(row["artifact_id"]),
                artifact_kind=self._VISUAL_KIND,
                content=content,
                source_input_hash=row["input_hash"],
                object_receipt_hash=row["receipt_hash"],
                media_type="application/json",
            )
            managed.validate()
            self._visual_verifier.verify(managed)
            value = json.loads(content)
            if (
                value["provenance"]["run_id"] != run_id
                or value["provenance"]["task_id"] != dependency_task_id
                or value["provenance"]["matter_id"] != binding["matter_id"]
                or value["task_input_hash"] != row["input_hash"]
                or value["pages"][0]["input_ref"] != dependency_ref
            ):
                raise CaseAgentRuntimePersistenceBlocked(
                    "visual dependency lineage differs from the current graph"
                )
            page = value["pages"][0]
            text = "\n".join(block["text"] for block in page["text_blocks"]).strip()
            if not text:
                raise CaseAgentRuntimePersistenceBlocked(
                    "visual dependency contains no OCR text"
                )
            item = LedgerExtractionPageProjection(
                input_ref=dependency_ref,
                evidence_page_id=page["evidence_page_id"],
                source_file_sha256=page["source_file_sha256"],
                page_number=page["page_number"],
                extracted_text=text,
                extracted_text_sha256=sha256(text.encode("utf-8")).hexdigest(),
                source_mode=ExtractionSourceMode.OCR,
            )
            item.validate()
            if dependency_ref in projected:
                # An explicit successful OCR dependency supersedes blank or
                # lossy native extraction for the same registered page.
                projected[dependency_ref] = item
            else:
                projected[dependency_ref] = item
        if len(projected) > 200:
            raise CaseAgentRuntimePersistenceBlocked(
                "ledger extraction dependency projection exceeds the page limit"
            )
        return tuple(projected[key] for key in sorted(projected))


class _CaseAgentMaterialObjectStore(Protocol):
    def materialize_case_agent_material(
        self, stored: StoredCaseAgentMaterial, *, destination: str | Path
    ) -> Path: ...


class _CaseAgentCandidateObjectStore(Protocol):
    def put_case_agent_review_candidate(self, content: bytes, **kwargs: Any) -> StoredCaseAgentReviewCandidate: ...

    def verify_case_agent_review_candidate(
        self, stored: StoredCaseAgentReviewCandidate, *, artifact_id: str
    ) -> None: ...

    def read_case_agent_review_candidate(
        self, stored: StoredCaseAgentReviewCandidate, *, artifact_id: str
    ) -> bytes: ...


@dataclass(frozen=True)
class _MaterialRecord:
    input_ref: str
    material_object_id: str
    content_sha256: str
    byte_size: int
    admitted_format: CommonDocumentFormat
    media_type: str
    source_object_key: str
    source_object_version_id: str | None


class _CommonDocumentLease(AbstractContextManager[tuple[BoundCommonDocument, ...]]):
    def __init__(
        self,
        *,
        worker_root: Path,
        object_store: _CaseAgentMaterialObjectStore,
        records: tuple[_MaterialRecord, ...],
    ) -> None:
        self._worker_root = worker_root
        self._object_store = object_store
        self._records = records
        self._materialization_root: Path | None = None
        self._bindings: tuple[BoundCommonDocument, ...] | None = None

    def __enter__(self) -> tuple[BoundCommonDocument, ...]:
        if self._materialization_root is not None:
            raise CaseAgentRuntimePersistenceBlocked("material lease cannot be entered twice")
        root = Path(tempfile.mkdtemp(prefix="case-agent-office-", dir=self._worker_root))
        root.chmod(0o700)
        self._materialization_root = root
        result: list[BoundCommonDocument] = []
        try:
            for index, record in enumerate(self._records, start=1):
                suffix = ".docx" if record.admitted_format is CommonDocumentFormat.DOCX else ".xlsx"
                destination = root / f"source-{index:03d}{suffix}"
                stored = StoredCaseAgentMaterial(
                    object_key=record.source_object_key,
                    content_sha256=record.content_sha256,
                    byte_size=record.byte_size,
                    media_type=record.media_type,
                    admitted_format=record.admitted_format.value,
                    object_version_id=record.source_object_version_id,
                )
                path = self._object_store.materialize_case_agent_material(
                    stored, destination=destination
                )
                source = MaterializedDocumentSource(
                    source_object_id=record.material_object_id,
                    source_object_version=(
                        record.source_object_version_id
                        or f"sha256:{record.content_sha256}"
                    ),
                    materialization_root=root,
                    path=path,
                    byte_size=record.byte_size,
                    content_sha256=record.content_sha256,
                    admitted_format=record.admitted_format,
                )
                result.append(BoundCommonDocument(record.input_ref, source))
            self._bindings = tuple(result)
            return self._bindings
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        root, self._materialization_root = self._materialization_root, None
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


class PostgresCommonDocumentInputPort:
    """Resolve exact ``material-object:<uuid>`` refs and erase all files."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        object_store: _CaseAgentMaterialObjectStore,
        worker_root: str | Path,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent Worker PostgreSQL DSN is required")
        _worker_actor(worker_actor)
        if not callable(getattr(object_store, "materialize_case_agent_material", None)):
            raise ValueError("case-Agent material object store is invalid")
        self._worker_root = _private_worker_root(worker_root)
        self._dsn = dsn
        self._worker = worker_actor
        self._object_store = object_store

    def open_common_documents(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> _CommonDocumentLease:
        material_ids = tuple(_opaque_ref(value, "material-object") for value in input_refs)
        with _tenant_transaction(self._dsn, self._worker.firm_id, read_only=True) as connection:
            binding = _read_current_task_binding(
                connection,
                worker=self._worker,
                run_id=run_id,
                task_id=task_id,
                task_input_hash=task_input_hash,
                input_refs=input_refs,
                required_tool="parse_office_document",
                include_human=False,
            )
            rows = connection.execute(
                """
                SELECT material.material_object_id, material.content_sha256,
                       material.byte_size, material.admitted_format,
                       material.media_type, material.source_object_key,
                       material.source_object_version_id
                FROM case_agent_material_objects material
                LEFT JOIN case_agent_material_object_tombstones tombstone
                  ON tombstone.material_object_id = material.material_object_id
                 AND tombstone.firm_id = material.firm_id
                 AND tombstone.matter_id = material.matter_id
                WHERE material.firm_id = %s AND material.matter_id = %s
                  AND material.material_object_id = ANY(%s)
                  AND tombstone.material_object_id IS NULL
                """,
                (self._worker.firm_id, binding["matter_id"], list(material_ids)),
            ).fetchall()
        by_id = {str(row["material_object_id"]): row for row in rows}
        if set(by_id) != set(material_ids):
            raise CaseAgentRuntimePersistenceBlocked(
                "one or more compiled Office material objects are unavailable"
            )
        records = tuple(
            _MaterialRecord(
                input_ref=input_ref,
                material_object_id=material_id,
                content_sha256=by_id[material_id]["content_sha256"],
                byte_size=int(by_id[material_id]["byte_size"]),
                admitted_format=CommonDocumentFormat(by_id[material_id]["admitted_format"]),
                media_type=by_id[material_id]["media_type"],
                source_object_key=by_id[material_id]["source_object_key"],
                source_object_version_id=by_id[material_id]["source_object_version_id"],
            )
            for input_ref, material_id in zip(input_refs, material_ids, strict=True)
        )
        return _CommonDocumentLease(
            worker_root=self._worker_root,
            object_store=self._object_store,
            records=records,
        )


class PostgresReviewCandidateStagingPort:
    """Stage canonical candidate bytes privately, then append hash metadata."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        object_store: _CaseAgentCandidateObjectStore,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent Worker PostgreSQL DSN is required")
        _worker_actor(worker_actor)
        for method in (
            "put_case_agent_review_candidate",
            "verify_case_agent_review_candidate",
        ):
            if not callable(getattr(object_store, method, None)):
                raise ValueError("case-Agent candidate object store is invalid")
        self._dsn = dsn
        self._worker = worker_actor
        self._object_store = object_store

    def stage_review_candidate(
        self, request: ReviewCandidateStagingRequest
    ) -> StagedReviewCandidate:
        request.validate()
        artifact_id = str(uuid5(UUID(request.task_id), request.idempotency_key))
        receipt = StagedReviewCandidate.build(request, artifact_id=artifact_id)
        with _tenant_transaction(self._dsn, self._worker.firm_id, read_only=True) as connection:
            binding = _read_current_task_binding(
                connection,
                worker=self._worker,
                run_id=request.run_id,
                task_id=request.task_id,
                task_input_hash=request.task_input_hash,
                input_refs=None,
                required_tool=None,
                include_human=False,
            )
            prior = _read_candidate(connection, firm_id=self._worker.firm_id, idempotency_key=request.idempotency_key)
        if prior is not None:
            return self._verified_prior(prior, request=request)

        stored = self._object_store.put_case_agent_review_candidate(
            request.payload,
            firm_id=self._worker.firm_id,
            matter_id=binding["matter_id"],
            artifact_id=artifact_id,
            content_sha256=request.content_sha256,
        )
        if (
            stored.content_sha256 != request.content_sha256
            or stored.byte_size != request.byte_size
        ):
            raise CaseAgentRuntimePersistenceBlocked(
                "candidate object-store receipt differs from the staging request"
            )
        try:
            with _tenant_transaction(self._dsn, self._worker.firm_id, read_only=False) as connection:
                # Reauthorize the exact task in the insertion transaction.
                current = _read_current_task_binding(
                    connection,
                    worker=self._worker,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    task_input_hash=request.task_input_hash,
                    input_refs=None,
                    required_tool=None,
                    include_human=False,
                )
                if current["graph_id"] != binding["graph_id"]:
                    raise CaseAgentRuntimePersistenceBlocked(
                        "task graph changed before candidate staging"
                    )
                connection.execute(
                    """
                    INSERT INTO case_agent_review_candidates (
                        artifact_id, idempotency_key, run_id, graph_id, task_id,
                        firm_id, matter_id, task_input_hash, source_hash,
                        artifact_kind, media_type, content_sha256, byte_size,
                        review_status, source_object_key,
                        source_object_version_id, receipt_hash, staged_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        artifact_id,
                        request.idempotency_key,
                        request.run_id,
                        binding["graph_id"],
                        request.task_id,
                        self._worker.firm_id,
                        binding["matter_id"],
                        request.task_input_hash,
                        request.source_hash,
                        request.artifact_kind,
                        request.media_type,
                        request.content_sha256,
                        request.byte_size,
                        request.review_status,
                        stored.object_key,
                        stored.object_version_id,
                        receipt.receipt_hash,
                        self._worker.actor_id,
                    ),
                )
            return receipt
        except psycopg.IntegrityError:
            # A concurrent identical task may have committed first.  Only an
            # exact, object-verified idempotent row is accepted as success.
            with _tenant_transaction(self._dsn, self._worker.firm_id, read_only=True) as connection:
                prior = _read_candidate(
                    connection,
                    firm_id=self._worker.firm_id,
                    idempotency_key=request.idempotency_key,
                )
            if prior is None:
                raise
            return self._verified_prior(prior, request=request)

    def _verified_prior(
        self, row: dict[str, Any], *, request: ReviewCandidateStagingRequest
    ) -> StagedReviewCandidate:
        prior = StagedReviewCandidate(
            artifact_id=str(row["artifact_id"]),
            idempotency_key=row["idempotency_key"],
            artifact_kind=row["artifact_kind"],
            content_sha256=row["content_sha256"],
            byte_size=int(row["byte_size"]),
            source_hash=row["source_hash"],
            task_input_hash=row["task_input_hash"],
            review_status=row["review_status"],
            receipt_hash=row["receipt_hash"],
        )
        prior.validate_against(request)
        stored = StoredCaseAgentReviewCandidate(
            object_key=row["source_object_key"],
            content_sha256=row["content_sha256"],
            byte_size=int(row["byte_size"]),
            object_version_id=row["source_object_version_id"],
        )
        self._object_store.verify_case_agent_review_candidate(
            stored, artifact_id=prior.artifact_id
        )
        return prior


class PostgresManagedArtifactAccessPort:
    """Independently authorise and re-read an exact staged candidate.

    The execution adapter's object receipt and bytes are not accepted as
    input.  A separate verifier database principal must prove its active
    SYSTEM_WORKER matter role, the current run/graph/task lineage and the
    immutable 0033 candidate row before S3 is read again.
    """

    _FIRST_RELEASE_KINDS = frozenset(
        artifact_kind
        for artifact_kind, _schema_version
        in FIRST_RELEASE_EXECUTABLE_REVIEW_CANDIDATE_SCHEMAS
    )

    def __init__(
        self,
        *,
        dsn: str,
        verifier_actor: Actor,
        execution_actor_id: str,
        object_store: _CaseAgentCandidateObjectStore,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent verifier PostgreSQL DSN is required")
        _worker_actor(verifier_actor)
        _uuid(execution_actor_id, "execution actor_id")
        if verifier_actor.actor_id == execution_actor_id:
            raise ValueError("independent verifier must differ from execution Worker")
        for method in (
            "verify_case_agent_review_candidate",
            "read_case_agent_review_candidate",
        ):
            if not callable(getattr(object_store, method, None)):
                raise ValueError("case-Agent verifier object store is invalid")
        self._dsn = dsn
        self._verifier = verifier_actor
        self._execution_actor_id = execution_actor_id
        self._object_store = object_store

    def read_managed_artifact(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
        artifact: ArtifactReceipt,
    ) -> ManagedArtifactRead:
        if not isinstance(artifact, ArtifactReceipt):
            raise ArtifactVerificationRejected("ARTIFACT_OBJECT_INVALID")
        try:
            artifact.validate()
            _uuid(firm_id, "firm_id")
            _uuid(matter_id, "matter_id")
            _uuid(run_id, "run_id")
        except Exception:
            raise ArtifactVerificationRejected("ARTIFACT_OBJECT_INVALID") from None
        if firm_id != self._verifier.firm_id:
            raise PermissionError("verification artifact belongs to another firm")
        if artifact.artifact_kind not in self._FIRST_RELEASE_KINDS:
            raise ArtifactVerificationRejected("ARTIFACT_VERIFIER_NOT_REGISTERED")

        with _tenant_transaction(self._dsn, firm_id, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT candidate.artifact_id, candidate.artifact_kind,
                       candidate.content_sha256, candidate.byte_size,
                       candidate.task_input_hash, candidate.review_status,
                       candidate.receipt_hash, candidate.source_object_key,
                       candidate.source_object_version_id
                FROM case_agent_review_candidates candidate
                JOIN case_agent_runs run
                  ON run.run_id = candidate.run_id
                 AND run.firm_id = candidate.firm_id
                 AND run.matter_id = candidate.matter_id
                 AND run.current_graph_id = candidate.graph_id
                JOIN case_agent_tasks task
                  ON task.graph_id = candidate.graph_id
                 AND task.task_id = candidate.task_id
                 AND task.run_id = candidate.run_id
                 AND task.firm_id = candidate.firm_id
                 AND task.matter_id = candidate.matter_id
                 AND task.input_hash = candidate.task_input_hash
                JOIN matter_actor_roles verifier_role
                  ON verifier_role.matter_id = candidate.matter_id
                 AND verifier_role.firm_id = candidate.firm_id
                 AND verifier_role.user_id = %s
                 AND verifier_role.role = 'SYSTEM_WORKER'
                 AND verifier_role.revoked_at IS NULL
                JOIN users verifier_user
                  ON verifier_user.user_id = verifier_role.user_id
                 AND verifier_user.firm_id = verifier_role.firm_id
                 AND verifier_user.status = 'ACTIVE'
                JOIN matter_actor_roles execution_role
                  ON execution_role.matter_id = candidate.matter_id
                 AND execution_role.firm_id = candidate.firm_id
                 AND execution_role.user_id = %s
                 AND execution_role.role = 'SYSTEM_WORKER'
                 AND execution_role.revoked_at IS NULL
                JOIN users execution_user
                  ON execution_user.user_id = execution_role.user_id
                 AND execution_user.firm_id = execution_role.firm_id
                 AND execution_user.status = 'ACTIVE'
                WHERE candidate.artifact_id = %s
                  AND candidate.run_id = %s
                  AND candidate.firm_id = %s
                  AND candidate.matter_id = %s
                """,
                (
                    self._verifier.actor_id,
                    self._execution_actor_id,
                    artifact.artifact_id,
                    run_id,
                    firm_id,
                    matter_id,
                ),
            ).fetchone()
        if row is None:
            raise ArtifactVerificationRejected("ARTIFACT_OBJECT_UNAVAILABLE")
        if (
            str(row["artifact_id"]) != artifact.artifact_id
            or row["artifact_kind"] != artifact.artifact_kind
            or row["content_sha256"] != artifact.content_hash
            or int(row["byte_size"]) != artifact.byte_size
            or row["task_input_hash"] != artifact.source_input_hash
            or row["review_status"] != "NEEDS_LAWYER_REVIEW"
        ):
            raise ArtifactVerificationRejected("ARTIFACT_LINEAGE_MISMATCH")

        stored = StoredCaseAgentReviewCandidate(
            object_key=row["source_object_key"],
            content_sha256=row["content_sha256"],
            byte_size=int(row["byte_size"]),
            object_version_id=row["source_object_version_id"],
        )
        try:
            content = self._object_store.read_case_agent_review_candidate(
                stored, artifact_id=artifact.artifact_id
            )
        except Exception as error:
            # Storage outages and ambiguous reads stay indeterminate in the
            # verifier.  Do not convert infrastructure failure into a known
            # legal/artifact rejection.
            raise CaseAgentRuntimePersistenceBlocked(
                "managed candidate could not be independently re-read"
            ) from error
        return ManagedArtifactRead(
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.artifact_kind,
            content=content,
            source_input_hash=artifact.source_input_hash,
            object_receipt_hash=row["receipt_hash"],
            media_type="application/json",
        )


def _read_current_task_binding(
    connection: Any,
    *,
    worker: Actor,
    run_id: str,
    task_id: str,
    task_input_hash: str,
    input_refs: tuple[str, ...] | None,
    required_tool: str | None,
    include_human: bool,
) -> dict[str, Any]:
    _uuid(run_id, "run_id")
    _uuid(task_id, "task_id")
    _sha256(task_input_hash, "task_input_hash")
    if input_refs is not None:
        _input_refs(input_refs)
    worker_row = connection.execute(
        """
        SELECT task.graph_id, task.matter_id, task.input_refs, task.input_hash,
               task.tool_id, goal.requested_by
        FROM case_agent_tasks task
        JOIN case_agent_runs run
          ON run.run_id = task.run_id AND run.firm_id = task.firm_id
         AND run.matter_id = task.matter_id
        JOIN case_agent_goals goal
          ON goal.goal_id = run.goal_id AND goal.firm_id = run.firm_id
         AND goal.matter_id = run.matter_id
        JOIN matter_actor_roles worker_role
          ON worker_role.matter_id = task.matter_id
         AND worker_role.firm_id = task.firm_id
         AND worker_role.user_id = %s
         AND worker_role.role = 'SYSTEM_WORKER'
         AND worker_role.revoked_at IS NULL
        JOIN users worker_user
          ON worker_user.user_id = worker_role.user_id
         AND worker_user.firm_id = worker_role.firm_id
         AND worker_user.status = 'ACTIVE'
        WHERE task.run_id = %s AND task.task_id = %s AND task.firm_id = %s
          AND task.input_hash = %s AND run.current_graph_id = task.graph_id
        """,
        (worker.actor_id, run_id, task_id, worker.firm_id, task_input_hash),
    ).fetchone()
    if worker_row is None:
        raise CaseAgentRuntimePersistenceBlocked(
            "compiled task is unavailable to this SYSTEM_WORKER"
        )
    stored_refs = tuple(worker_row["input_refs"])
    if input_refs is not None and stored_refs != input_refs:
        raise CaseAgentRuntimePersistenceBlocked(
            "compiled task input references differ from persistence"
        )
    if required_tool is not None and worker_row["tool_id"] != required_tool:
        raise CaseAgentRuntimePersistenceBlocked(
            "compiled task tool differs from the runtime port"
        )
    result = {
        "graph_id": str(worker_row["graph_id"]),
        "matter_id": str(worker_row["matter_id"]),
        "requested_by": str(worker_row["requested_by"]),
    }
    if include_human:
        human_rows = connection.execute(
            """
            SELECT role.role
            FROM matter_actor_roles role
            JOIN users human
              ON human.user_id = role.user_id AND human.firm_id = role.firm_id
            WHERE role.matter_id = %s AND role.firm_id = %s
              AND role.user_id = %s AND role.revoked_at IS NULL
              AND role.role = ANY(%s) AND human.status = 'ACTIVE'
            ORDER BY role.role
            """,
            (
                result["matter_id"],
                worker.firm_id,
                result["requested_by"],
                [role.value for role in _HUMAN_EVIDENCE_ROLES],
            ),
        ).fetchall()
        roles = tuple(row["role"] for row in human_rows)
        if not roles:
            raise CaseAgentRuntimePersistenceBlocked(
                "run creator no longer has an active human evidence role"
            )
        result["requested_by_roles"] = roles
    return result


def _read_candidate(
    connection: Any, *, firm_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    return connection.execute(
        """
        SELECT artifact_id, idempotency_key, artifact_kind, content_sha256,
               byte_size, source_hash, task_input_hash, review_status,
               receipt_hash, source_object_key, source_object_version_id
        FROM case_agent_review_candidates
        WHERE firm_id = %s AND idempotency_key = %s
        """,
        (firm_id, idempotency_key),
    ).fetchone()


@contextmanager
def _tenant_transaction(
    dsn: str, firm_id: str, *, read_only: bool
) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        if read_only:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
        yield connection


def _private_worker_root(value: str | Path) -> Path:
    try:
        root = Path(value)
    except TypeError as error:
        raise ValueError("case-Agent Worker root is invalid") from error
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("case-Agent Worker root must be an existing absolute directory")
    metadata = os.lstat(root)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("case-Agent Worker root must be private")
    return root.resolve(strict=True)


def _opaque_ref(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise CaseAgentRuntimePersistenceBlocked(
            f"compiled input is not a {prefix} reference"
        )
    identifier = value[len(prefix) + 1 :]
    _uuid(identifier, f"{prefix} identifier")
    if value != f"{prefix}:{identifier}":
        raise CaseAgentRuntimePersistenceBlocked("compiled input reference is not canonical")
    return identifier


def _input_refs(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or not 1 <= len(values) <= 500 or len(set(values)) != len(values):
        raise CaseAgentRuntimePersistenceBlocked("compiled input refs are invalid")
    if any(not isinstance(value, str) or _INPUT_REF.fullmatch(value) is None for value in values):
        raise CaseAgentRuntimePersistenceBlocked("compiled input ref syntax is invalid")


def _worker_actor(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError("runtime requires a dedicated SYSTEM_WORKER identity")
    _uuid(actor.actor_id, "worker actor_id")
    _uuid(actor.firm_id, "worker firm_id")


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseAgentRuntimePersistenceBlocked(f"{label} must be a UUID") from error


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CaseAgentRuntimePersistenceBlocked(f"{label} must be a SHA-256 digest")


def _code(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 200
        or any(ord(character) < 32 for character in value)
    ):
        raise CaseAgentRuntimePersistenceBlocked(f"{label} is invalid")
