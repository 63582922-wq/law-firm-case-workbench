"""Authoritative PostgreSQL binding for dynamic case-Agent document tasks.

The external planner is allowed to select only one opaque ``work-plan-item``
reference.  This repository expands that aggregate reference into the exact
current, lawyer-confirmed sources recorded by the active dynamic work plan.
It never accepts source text, a template, a URL, an object key or a party role
from the browser/model.

The read is deliberately fail-closed.  A changed matter version, inactive
work plan, replaced posture profile, stale source hash, unsupported source
type or unavailable official-source text prevents drafting before any model
request is built.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Iterator, Mapping, Protocol
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_document_delivery import (
    AuthoritativeDocumentSource,
    CaseAgentDocumentDeliveryBlocked,
    DocumentSourceKind,
    DynamicDocumentTaskBinding,
    ReviewableDocumentFormat,
    ReviewableDocumentTemplate,
    ReviewableDocumentTemplateRegistry,
)
from .case_agent_lawyer_analysis import (
    LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
    LawyerAnalysisBlocked,
    parse_lawyer_decision_package_candidate,
)
from .legal_provision_parser import (
    LegalProvisionDocumentProjectionBlocked,
    project_registered_legal_source_for_document,
)
from .case_agent_supervisor import ArtifactReceipt
from .case_agent_verifier import ManagedArtifactAccessPort, ManagedArtifactRead
from .case_work_plan import (
    CaseWorkPlanItem,
    DeliveryTarget,
    ReviewGate,
    WorkPlanItemKind,
    WorkPlanReadiness,
    WorkPlanReference,
    WorkPlanReferenceUse,
    WorkPlanSourceType,
)
from .case_work_plan_postgres import assert_case_work_plan_references_current
from .case_agent_planning_snapshot_postgres import (
    planning_work_plan_item_content_hash,
)
from .case_ledger_postgres import _payload_hash
from .models import Actor, Role


class PostgresDocumentBindingBlocked(RuntimeError):
    """The compiled document task cannot be bound to a current case snapshot."""


class VerifiedOfficialSourceTextPort(Protocol):
    """Authenticate one official snapshot and return bounded plain text.

    Implementations must read the private hash-bound object, verify the exact
    content hash and media type, and extract only the reviewed provision.  A
    database locator by itself is never treated as text.
    """

    def read_verified_source_text(
        self,
        *,
        firm_id: str,
        matter_id: str,
        snapshot_id: str,
        storage_object_key: str,
        content_sha256: str,
        content_media_type: str,
        provision_locator: str,
    ) -> str: ...


class VerifiedLawyerDecisionPackagePort(Protocol):
    """Read the exact independently PASSED package from a source Agent run."""

    def read_verified_lawyer_decision_package(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
    ) -> AuthoritativeDocumentSource: ...


class PostgresVerifiedLawyerDecisionPackagePort:
    """Bind a PASSED verification lineage to private candidate bytes.

    The document Worker cannot nominate an artifact id or object key.  This
    port selects the one lawyer-decision package named by the source run,
    proves that the independent verification receipt includes its exact
    lineage, and delegates the private-object read to the verifier-owned
    artifact access port.
    """

    def __init__(
        self,
        *,
        dsn: str,
        verifier_actor: Actor,
        execution_actor_id: str,
        artifact_access: ManagedArtifactAccessPort,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("verified lawyer-package PostgreSQL DSN is required")
        _worker(verifier_actor)
        _uuid(execution_actor_id, "execution_actor_id")
        if verifier_actor.actor_id == execution_actor_id:
            raise ValueError("lawyer-package verifier must differ from execution Worker")
        if not callable(getattr(artifact_access, "read_managed_artifact", None)):
            raise ValueError("verified lawyer-package artifact access is required")
        self._dsn = dsn
        self._verifier = verifier_actor
        self._execution_actor_id = execution_actor_id
        self._artifact_access = artifact_access

    def read_verified_lawyer_decision_package(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
    ) -> AuthoritativeDocumentSource:
        for value, label in (
            (firm_id, "firm_id"),
            (matter_id, "matter_id"),
            (run_id, "source_run_id"),
        ):
            _uuid(value, label)
        if firm_id != self._verifier.firm_id:
            raise PermissionError("lawyer decision package belongs to another firm")
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (firm_id,)
            )
            rows = connection.execute(
                """
                SELECT artifact.artifact_id, artifact.artifact_kind,
                       artifact.content_hash, artifact.byte_size,
                       artifact.source_input_hash, artifact.managed_derivative,
                       verification.verification_hash
                FROM case_agent_verification_receipts verification
                CROSS JOIN LATERAL jsonb_array_elements(
                    verification.artifact_lineage
                ) AS lineage(value)
                JOIN case_agent_artifacts artifact
                  ON artifact.run_id = verification.run_id
                 AND artifact.firm_id = verification.firm_id
                 AND artifact.matter_id = verification.matter_id
                 AND artifact.artifact_id::text = lineage.value->>'artifact_id'
                 AND artifact.artifact_kind = lineage.value->>'artifact_kind'
                 AND artifact.content_hash = lineage.value->>'content_hash'
                 AND artifact.source_input_hash =
                     lineage.value->>'source_input_hash'
                 AND artifact.byte_size =
                     (lineage.value->>'byte_size')::bigint
                JOIN case_agent_task_receipts receipt
                  ON receipt.receipt_id = artifact.receipt_id
                 AND receipt.run_id = artifact.run_id
                 AND receipt.firm_id = artifact.firm_id
                 AND receipt.matter_id = artifact.matter_id
                 AND receipt.result_status = 'SUCCEEDED'
                JOIN case_agent_runs run
                  ON run.run_id = artifact.run_id
                 AND run.firm_id = artifact.firm_id
                 AND run.matter_id = artifact.matter_id
                JOIN case_agent_tasks task
                  ON task.run_id = receipt.run_id
                 AND task.task_id = receipt.task_id
                 AND task.graph_id = run.current_graph_id
                 AND task.firm_id = receipt.firm_id
                 AND task.matter_id = receipt.matter_id
                 AND task.input_hash = receipt.input_hash
                JOIN matter_actor_roles verifier_role
                  ON verifier_role.firm_id = verification.firm_id
                 AND verifier_role.matter_id = verification.matter_id
                 AND verifier_role.user_id = %s
                 AND verifier_role.role = 'SYSTEM_WORKER'
                 AND verifier_role.revoked_at IS NULL
                JOIN users verifier_user
                  ON verifier_user.user_id = verifier_role.user_id
                 AND verifier_user.firm_id = verifier_role.firm_id
                 AND verifier_user.status = 'ACTIVE'
                JOIN matter_actor_roles execution_role
                  ON execution_role.firm_id = verification.firm_id
                 AND execution_role.matter_id = verification.matter_id
                 AND execution_role.user_id = %s
                 AND execution_role.role = 'SYSTEM_WORKER'
                 AND execution_role.revoked_at IS NULL
                JOIN users execution_user
                  ON execution_user.user_id = execution_role.user_id
                 AND execution_user.firm_id = execution_role.firm_id
                 AND execution_user.status = 'ACTIVE'
                WHERE verification.run_id = %s
                  AND verification.firm_id = %s
                  AND verification.matter_id = %s
                  AND verification.outcome = 'PASSED'
                  AND verification.verifier_actor_id = %s
                  AND verification.execution_actor_id = %s
                  AND artifact.artifact_kind = %s
                ORDER BY verification.persisted_at DESC, artifact.artifact_id ASC
                LIMIT 2
                """,
                (
                    self._verifier.actor_id,
                    self._execution_actor_id,
                    run_id,
                    firm_id,
                    matter_id,
                    self._verifier.actor_id,
                    self._execution_actor_id,
                    LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
                ),
            ).fetchall()
        if len(rows) != 1:
            raise PostgresDocumentBindingBlocked(
                "source Agent run lacks one independently verified lawyer decision package"
            )
        row = rows[0]
        artifact = ArtifactReceipt(
            artifact_id=str(row["artifact_id"]),
            artifact_kind=str(row["artifact_kind"]),
            content_hash=str(row["content_hash"]),
            byte_size=int(row["byte_size"]),
            source_input_hash=str(row["source_input_hash"]),
            managed_derivative=bool(row["managed_derivative"]),
        )
        artifact.validate()
        try:
            managed = self._artifact_access.read_managed_artifact(
                firm_id=firm_id,
                matter_id=matter_id,
                run_id=run_id,
                artifact=artifact,
            )
        except Exception as error:
            raise PostgresDocumentBindingBlocked(
                "verified lawyer decision package could not be authenticated"
            ) from error
        if not isinstance(managed, ManagedArtifactRead):
            raise PostgresDocumentBindingBlocked(
                "verified lawyer decision package read is invalid"
            )
        managed.validate()
        if (
            managed.artifact_id != artifact.artifact_id
            or managed.artifact_kind != LAWYER_DECISION_PACKAGE_ARTIFACT_KIND
            or managed.source_input_hash != artifact.source_input_hash
            or len(managed.content) != artifact.byte_size
            or sha256(managed.content).hexdigest() != artifact.content_hash
            or managed.media_type != "application/json"
        ):
            raise PostgresDocumentBindingBlocked(
                "verified lawyer decision package bytes differ from lineage"
            )
        try:
            parse_lawyer_decision_package_candidate(managed.content)
            text = managed.content.decode("utf-8")
        except (LawyerAnalysisBlocked, UnicodeDecodeError) as error:
            raise PostgresDocumentBindingBlocked(
                "verified lawyer decision package schema is invalid"
            ) from error
        return AuthoritativeDocumentSource(
            input_ref=f"lawyer-decision-package:{artifact.artifact_id}",
            source_kind=DocumentSourceKind.VERIFIED_LAWYER_DECISION_PACKAGE,
            source_version=f"verified-{str(row['verification_hash'])}",
            source_hash=artifact.content_hash,
            label="已独立验证的律师决策包候选（仅供律师复核）",
            text=text,
        )


class PostgresDynamicDocumentBindingPort:
    """Resolve one running/reconciling document task in one repeatable read."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        templates: ReviewableDocumentTemplateRegistry,
        official_source_text: VerifiedOfficialSourceTextPort,
        verified_lawyer_package: VerifiedLawyerDecisionPackagePort,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("document binding PostgreSQL DSN is required")
        _worker(worker_actor)
        if not isinstance(templates, ReviewableDocumentTemplateRegistry):
            raise ValueError("document template registry is invalid")
        if not callable(getattr(official_source_text, "read_verified_source_text", None)):
            raise ValueError("verified official-source text port is required")
        if not callable(
            getattr(
                verified_lawyer_package,
                "read_verified_lawyer_decision_package",
                None,
            )
        ):
            raise ValueError("verified lawyer decision-package port is required")
        self._dsn = dsn
        self._worker = worker_actor
        self._templates = templates
        self._official_source_text = official_source_text
        self._verified_lawyer_package = verified_lawyer_package

    def resolve_document_task(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
        expected_format: ReviewableDocumentFormat,
    ) -> DynamicDocumentTaskBinding:
        _uuid(run_id, "run_id")
        _uuid(task_id, "task_id")
        _uuid(attempt_id, "attempt_id")
        _sha256(task_input_hash, "task_input_hash")
        if not isinstance(expected_format, ReviewableDocumentFormat):
            raise PostgresDocumentBindingBlocked("document output format is invalid")
        item_id = _single_work_plan_item_ref(input_refs)
        required_tool = (
            "draft_reviewable_docx_package"
            if expected_format is ReviewableDocumentFormat.DOCX
            else "draft_reviewable_xlsx_package"
        )
        with self._transaction() as connection:
            task = self._read_current_task(
                connection,
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                task_input_hash=task_input_hash,
                input_refs=input_refs,
                required_tool=required_tool,
                item_id=item_id,
                expected_format=expected_format,
            )
            plan, item = self._read_active_plan_item(
                connection,
                matter_id=task["matter_id"],
                current_matter_version=task["matter_version"],
                item_id=item_id,
            )
            item_hash = planning_work_plan_item_content_hash(
                plan_id=plan["plan_id"],
                plan_hash=plan["plan_hash"],
                row=item,
            )
            expected_task_id = str(
                uuid5(
                    UUID(task["graph_id"]),
                    "active-work-plan-execution-task-v1:"
                    f"{item_id}:{item_hash}:{item['deliverable_kind']}",
                )
            )
            if (
                task["execution_plan_id"] != plan["plan_id"]
                or task["execution_plan_hash"] != plan["plan_hash"]
                or task["execution_item_id"] != item_id
                or task["execution_item_hash"] != item_hash
                or task["execution_deliverable_kind"]
                != str(item["deliverable_kind"])
                or task["execution_output_format"] != expected_format.value
                or expected_task_id != task_id
            ):
                raise PostgresDocumentBindingBlocked(
                    "document task is not the exact server-compiled active-plan execution item"
                )
            template = self._templates.get(str(item["deliverable_kind"]))
            if template.output_format is not expected_format:
                raise PostgresDocumentBindingBlocked(
                    "active work-plan template format differs from the compiled task"
                )
            return self._assemble_binding(
                connection,
                task=task,
                plan=plan,
                item=item,
                item_id=item_id,
                input_refs=input_refs,
                template=template,
                run_id=run_id,
                task_id=task_id,
                task_input_hash=task_input_hash,
            )

    def resolve_content_proposal_binding(
        self, *, actor: Actor, package_id: str, matter_id: str, run_id: str,
    ) -> DynamicDocumentTaskBinding:
        """Re-read sources for a human edit without creating a Worker attempt."""
        for value, label in ((package_id, "package_id"), (matter_id, "matter_id"), (run_id, "run_id")):
            _uuid(value, label)
        if not isinstance(actor, Actor) or actor.firm_id != self._worker.firm_id or Role.SYSTEM_WORKER in actor.roles:
            raise PermissionError("content proposal requires a same-firm human")
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT package.*, package.case_snapshot_hash AS snapshot_hash,
                          matter.version AS matter_version, task.input_refs,
                          execution.source_run_id AS execution_source_run_id
                   FROM case_agent_reviewable_document_packages package
                   JOIN case_agent_runs run ON run.run_id = package.run_id
                     AND run.firm_id = package.firm_id AND run.matter_id = package.matter_id
                   JOIN matters matter ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
                   JOIN case_agent_tasks task ON task.graph_id = package.graph_id AND task.task_id = package.task_id
                     AND task.run_id = run.run_id AND task.firm_id = run.firm_id AND task.matter_id = run.matter_id
                   JOIN case_agent_task_heads head ON head.graph_id = task.graph_id AND head.task_id = task.task_id
                     AND head.run_id = run.run_id AND head.firm_id = run.firm_id AND head.matter_id = run.matter_id
                   JOIN case_agent_active_plan_execution_runs execution ON execution.run_id = run.run_id
                     AND execution.firm_id = run.firm_id AND execution.matter_id = run.matter_id
                   WHERE package.package_id = %s AND package.firm_id = %s
                     AND package.matter_id = %s AND package.run_id = %s
                     AND run.status = 'READY_FOR_REVIEW' AND NOT run.is_stale AND NOT run.is_cancelled
                     AND run.current_graph_id = package.graph_id
                     AND run.snapshot_matter_version = matter.version
                     AND run.snapshot_hash = package.case_snapshot_hash
                     AND task.input_hash = package.task_input_hash
                     AND head.is_current AND head.status = 'SUCCEEDED'
                     AND EXISTS (SELECT 1 FROM users principal JOIN matter_actor_roles role
                       ON role.user_id = principal.user_id AND role.firm_id = principal.firm_id
                       WHERE principal.user_id = %s AND principal.firm_id = package.firm_id
                         AND principal.status = 'ACTIVE' AND role.matter_id = package.matter_id
                         AND role.revoked_at IS NULL
                         AND role.role IN ('ASSISTANT','COLLABORATING_LAWYER','LEAD_LAWYER','REVIEWER'))
                     AND EXISTS (SELECT 1 FROM users worker JOIN matter_actor_roles role
                       ON role.user_id = worker.user_id AND role.firm_id = worker.firm_id
                       WHERE worker.user_id = %s AND worker.firm_id = package.firm_id
                         AND worker.status = 'ACTIVE' AND role.matter_id = package.matter_id
                         AND role.revoked_at IS NULL AND role.role = 'SYSTEM_WORKER')""",
                (package_id, actor.firm_id, matter_id, run_id, actor.actor_id, self._worker.actor_id),
            ).fetchone()
            if row is None:
                raise PostgresDocumentBindingBlocked("content proposal package is stale or unauthorized")
            task = dict(row)
            task["matter_id"] = str(row["matter_id"])
            refs = tuple(str(ref) for ref in row["input_refs"])
            item_id = _single_work_plan_item_ref(refs)
            plan, item = self._read_active_plan_item(connection, matter_id=matter_id, current_matter_version=int(row["matter_version"]), item_id=item_id)
            if (str(plan["plan_id"]), str(plan["plan_hash"]), item_id) != (str(row["work_plan_id"]), str(row["work_plan_hash"]), str(row["work_plan_item_id"])):
                raise PostgresDocumentBindingBlocked("content proposal active plan changed")
            template = self._templates.get(str(row["deliverable_kind"]))
            binding = self._assemble_binding(
                connection, task=task, plan=plan, item=item, item_id=item_id,
                input_refs=refs, template=template, run_id=run_id,
                task_id=str(row["task_id"]), task_input_hash=str(row["task_input_hash"]),
            )
            if binding.binding_hash != str(row["binding_hash"]):
                raise PostgresDocumentBindingBlocked("content proposal sources or template changed")
            return binding

    def resolve_document_revision(
        self,
        *,
        request_id: str,
        predecessor_package_id: str,
        expected_revision_number: int,
        content_claim_version: int | None = None,
    ) -> DynamicDocumentTaskBinding:
        """Rebind a claimed template or content revision to current sources."""

        _uuid(request_id, "request_id")
        _uuid(predecessor_package_id, "predecessor_package_id")
        if content_claim_version is not None and (type(content_claim_version) is not int or not 1 <= content_claim_version <= 3):
            raise PostgresDocumentBindingBlocked("content revision claim version is invalid")
        if not 1 <= expected_revision_number <= 999:
            raise PostgresDocumentBindingBlocked(
                "document predecessor revision is invalid"
            )
        with self._transaction() as connection:
            task = self._read_revision_task(
                connection,
                request_id=request_id,
                predecessor_package_id=predecessor_package_id,
                expected_revision_number=expected_revision_number,
                content_claim_version=content_claim_version,
            )
            input_refs = tuple(str(item) for item in task["input_refs"])
            item_id = _single_work_plan_item_ref(input_refs)
            plan, item = self._read_active_plan_item(
                connection,
                matter_id=task["matter_id"],
                current_matter_version=task["matter_version"],
                item_id=item_id,
            )
            if (
                plan["plan_id"] != task["work_plan_id"]
                or plan["plan_hash"] != task["work_plan_hash"]
                or item["item_id"] != task["work_plan_item_id"]
            ):
                raise PostgresDocumentBindingBlocked(
                    "document revision no longer uses the current active plan item"
                )
            template = self._templates.get(str(item["deliverable_kind"]))
            if (
                template.template_id != task["target_template_id"]
                or template.template_version != task["target_template_version"]
                or template.template_hash != task["target_template_hash"]
                or template.output_format.value != task["output_format"]
            ):
                raise PostgresDocumentBindingBlocked(
                    "document revision target differs from the installed template"
                )
            return self._assemble_binding(
                connection,
                task=task,
                plan=plan,
                item=item,
                item_id=item_id,
                input_refs=input_refs,
                template=template,
                run_id=task["run_id"],
                task_id=task["task_id"],
                task_input_hash=task["task_input_hash"],
            )

    def _assemble_binding(
        self,
        connection: Any,
        *,
        task: Mapping[str, Any],
        plan: Mapping[str, Any],
        item: Mapping[str, Any],
        item_id: str,
        input_refs: tuple[str, ...],
        template: ReviewableDocumentTemplate,
        run_id: str,
        task_id: str,
        task_input_hash: str,
    ) -> DynamicDocumentTaskBinding:
        actor = self._worker
        try:
            assert_case_work_plan_references_current(
                connection,
                actor=actor,
                matter_id=str(task["matter_id"]),
                plan_id=str(plan["plan_id"]),
            )
        except Exception as error:
            raise PostgresDocumentBindingBlocked(
                "active work-plan sources changed; drafting is stale"
            ) from error
        profile = self._read_current_profile(
            connection,
            matter_id=str(task["matter_id"]),
            profile_id=str(plan["profile_id"]),
            profile_hash=str(plan["profile_hash"]),
        )
        reference_bindings = self._read_item_references(
            connection,
            matter_id=str(task["matter_id"]),
            plan_id=str(plan["plan_id"]),
            item_id=item_id,
        )
        sources = [
            _posture_source(profile),
            _work_plan_item_source(plan, item, input_refs[0]),
        ]
        seen = {source.input_ref for source in sources}
        for _reference_role, reference in reference_bindings:
            if reference.source_type in {
                WorkPlanSourceType.POSTURE_PROFILE,
                WorkPlanSourceType.LAWYER_OBJECTIVE,
            }:
                continue
            source = self._read_source(
                connection,
                matter_id=str(task["matter_id"]),
                reference=reference,
            )
            if source.input_ref not in seen:
                sources.append(source)
                seen.add(source.input_ref)
        if template.deliverable_kind in {
            "CASE_REVIEW_MEMO",
            "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
            "DEFENCE_STATEMENT",
        }:
            decision_package = (
                self._verified_lawyer_package.read_verified_lawyer_decision_package(
                    firm_id=self._worker.firm_id,
                    matter_id=str(task["matter_id"]),
                    run_id=str(task["execution_source_run_id"]),
                )
            )
            if decision_package.input_ref in seen:
                raise PostgresDocumentBindingBlocked(
                    "verified lawyer decision package source is duplicated"
                )
            sources.append(decision_package)
        binding = DynamicDocumentTaskBinding(
            firm_id=self._worker.firm_id,
            matter_id=str(task["matter_id"]),
            run_id=run_id,
            graph_id=str(task["graph_id"]),
            task_id=task_id,
            task_input_hash=task_input_hash,
            case_snapshot_hash=str(task["snapshot_hash"]),
            work_plan_id=str(plan["plan_id"]),
            work_plan_hash=str(plan["plan_hash"]),
            work_plan_status=str(plan["status"]),
            work_plan_item=_case_work_plan_item(item, reference_bindings),
            posture_profile_id=str(profile["profile_id"]),
            posture_profile_hash=str(profile["profile_hash"]),
            template=template,
            sources=tuple(sources),
        )
        try:
            binding.validate()
        except CaseAgentDocumentDeliveryBlocked as error:
            raise PostgresDocumentBindingBlocked(str(error)) from error
        return binding

    def _read_revision_task(
        self,
        connection: Any,
        *,
        request_id: str,
        predecessor_package_id: str,
        expected_revision_number: int,
        content_claim_version: int | None = None,
    ) -> dict[str, Any]:
        inbox_join = """JOIN case_agent_document_revision_inbox inbox
              ON inbox.request_id = request.request_id
             AND inbox.firm_id = request.firm_id AND inbox.matter_id = request.matter_id"""
        inbox_key, claim_filter, extra = "inbox.request_id", "", ()
        if content_claim_version is not None:
            inbox_join = """JOIN case_agent_document_content_generation_jobs inbox
              ON inbox.review_id = request.content_generation_review_id
             AND inbox.firm_id = request.firm_id AND inbox.matter_id = request.matter_id
             AND inbox.run_id = request.run_id"""
            inbox_key, claim_filter, extra = "inbox.review_id", "AND inbox.claim_version = %s", (content_claim_version,)
        row = connection.execute(
            f"""
            SELECT request.request_id, request.predecessor_package_id,
                   request.expected_revision_number,
                   request.target_template_id, request.target_template_version,
                   request.target_template_hash,
                   request.source_package_receipt_hash,
                   inbox.state AS inbox_state, inbox.claimed_by,
                   inbox.lease_expires_at,
                   package.run_id, package.graph_id, package.task_id,
                   package.attempt_id, package.matter_id, package.task_input_hash,
                   package.case_snapshot_hash AS snapshot_hash,
                   package.work_plan_id, package.work_plan_hash,
                   package.work_plan_item_id, package.output_format,
                   package.package_receipt_hash,
                   run.status AS run_status, run.is_stale, run.is_cancelled,
                   run.current_graph_id, run.snapshot_matter_version,
                   matter.version AS matter_version,
                   task.input_refs, task.input_hash, task.tool_id,
                   head.status AS head_status, head.is_current,
                   attempt.status AS attempt_status,
                   execution.source_run_id AS execution_source_run_id,
                   worker_user.status AS worker_status,
                   bool_and(worker_role.role = 'SYSTEM_WORKER') AS worker_only,
                   count(*) FILTER (WHERE worker_role.revoked_at IS NULL)
                       AS active_role_count
            FROM case_agent_document_revision_requests request
            {inbox_join}
            JOIN case_agent_reviewable_document_packages package
              ON package.package_id = request.predecessor_package_id
             AND package.run_id = request.run_id
             AND package.firm_id = request.firm_id
             AND package.matter_id = request.matter_id
            JOIN case_agent_runs run
              ON run.run_id = package.run_id AND run.firm_id = package.firm_id
             AND run.matter_id = package.matter_id
            JOIN matters matter
              ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
            JOIN case_agent_tasks task
              ON task.graph_id = package.graph_id AND task.task_id = package.task_id
             AND task.run_id = package.run_id AND task.firm_id = package.firm_id
             AND task.matter_id = package.matter_id
            JOIN case_agent_task_heads head
              ON head.graph_id = task.graph_id AND head.task_id = task.task_id
             AND head.run_id = task.run_id AND head.firm_id = task.firm_id
             AND head.matter_id = task.matter_id
            JOIN case_agent_task_attempts attempt
              ON attempt.attempt_id = package.attempt_id
             AND attempt.graph_id = task.graph_id AND attempt.task_id = task.task_id
             AND attempt.run_id = task.run_id AND attempt.firm_id = task.firm_id
             AND attempt.matter_id = task.matter_id
            JOIN case_agent_active_plan_execution_runs execution
              ON execution.run_id = run.run_id AND execution.firm_id = run.firm_id
             AND execution.matter_id = run.matter_id
            JOIN users worker_user
              ON worker_user.user_id = %s AND worker_user.firm_id = run.firm_id
            JOIN matter_actor_roles worker_role
              ON worker_role.user_id = worker_user.user_id
             AND worker_role.firm_id = worker_user.firm_id
             AND worker_role.matter_id = run.matter_id
             AND worker_role.revoked_at IS NULL
            WHERE request.request_id = %s AND request.firm_id = %s
              AND request.predecessor_package_id = %s
              AND inbox.lease_expires_at > clock_timestamp()
              {claim_filter}
            GROUP BY request.request_id, {inbox_key}, package.package_id,
                     run.run_id, matter.matter_id, task.graph_id, task.task_id,
                     head.graph_id, head.task_id, head.run_id, head.firm_id,
                     head.matter_id, attempt.attempt_id, execution.execution_id,
                     worker_user.user_id
            """,
            (
                self._worker.actor_id,
                request_id,
                self._worker.firm_id,
                predecessor_package_id,
                *extra,
            ),
        ).fetchone()
        if row is None:
            raise PostgresDocumentBindingBlocked(
                "document revision request is unavailable to this Worker"
            )
        expected_tool = (
            "draft_reviewable_docx_package"
            if str(row["output_format"]) == ReviewableDocumentFormat.DOCX.value
            else "draft_reviewable_xlsx_package"
        )
        if (
            int(row["expected_revision_number"]) != expected_revision_number
            or str(row["predecessor_package_id"]) != predecessor_package_id
            or str(row["source_package_receipt_hash"])
                != str(row["package_receipt_hash"])
            or row["inbox_state"] != "LEASED"
            or str(row["claimed_by"]) != self._worker.actor_id
            or row["lease_expires_at"] is None
            or row["run_status"] != "READY_FOR_REVIEW"
            or bool(row["is_stale"])
            or bool(row["is_cancelled"])
            or str(row["current_graph_id"]) != str(row["graph_id"])
            or int(row["snapshot_matter_version"]) != int(row["matter_version"])
            or str(row["input_hash"]) != str(row["task_input_hash"])
            or row["head_status"] != "SUCCEEDED"
            or not bool(row["is_current"])
            or row["attempt_status"] != "SUCCEEDED"
            or str(row["tool_id"]) != expected_tool
            or row["worker_status"] != "ACTIVE"
            or not bool(row["worker_only"])
            or int(row["active_role_count"]) != 1
        ):
            raise PostgresDocumentBindingBlocked(
                "document revision request is stale or not current"
            )
        refs = row["input_refs"]
        if not isinstance(refs, list):
            raise PostgresDocumentBindingBlocked(
                "document revision task references are invalid"
            )
        return {
            "run_id": str(row["run_id"]),
            "graph_id": str(row["graph_id"]),
            "task_id": str(row["task_id"]),
            "attempt_id": str(row["attempt_id"]),
            "task_input_hash": str(row["task_input_hash"]),
            "snapshot_hash": str(row["snapshot_hash"]),
            "matter_id": str(row["matter_id"]),
            "matter_version": int(row["matter_version"]),
            "input_refs": refs,
            "work_plan_id": str(row["work_plan_id"]),
            "work_plan_hash": str(row["work_plan_hash"]),
            "work_plan_item_id": str(row["work_plan_item_id"]),
            "output_format": str(row["output_format"]),
            "execution_source_run_id": str(row["execution_source_run_id"]),
            "target_template_id": str(row["target_template_id"]),
            "target_template_version": str(row["target_template_version"]),
            "target_template_hash": str(row["target_template_hash"]),
        }

    def _read_current_task(
        self,
        connection: Any,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
        required_tool: str,
        item_id: str,
        expected_format: ReviewableDocumentFormat,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT task.graph_id, task.matter_id, task.input_refs, task.input_hash,
                   task.tool_id, run.snapshot_hash, run.snapshot_matter_version,
                   matter.version AS matter_version, attempt.status AS attempt_status,
                   execution.plan_id AS execution_plan_id,
                   execution.plan_hash AS execution_plan_hash,
                   execution.source_run_id AS execution_source_run_id,
                   execution_item.item->>'item_id' AS execution_item_id,
                   execution_item.item->>'item_hash' AS execution_item_hash,
                   execution_item.item->>'deliverable_kind'
                       AS execution_deliverable_kind,
                   execution_item.item->>'output_format'
                       AS execution_output_format
            FROM case_agent_tasks task
            JOIN case_agent_runs run
              ON run.run_id = task.run_id AND run.firm_id = task.firm_id
             AND run.matter_id = task.matter_id
            JOIN matters matter
              ON matter.matter_id = task.matter_id AND matter.firm_id = task.firm_id
            JOIN case_agent_active_plan_execution_runs execution
              ON execution.run_id = run.run_id AND execution.firm_id = run.firm_id
             AND execution.matter_id = run.matter_id
            JOIN case_agent_goals goal
              ON goal.goal_id = run.goal_id AND goal.firm_id = run.firm_id
             AND goal.matter_id = run.matter_id
            JOIN LATERAL jsonb_array_elements(
                goal.active_plan_execution->'items'
            ) WITH ORDINALITY AS execution_item(item, item_sequence)
              ON execution_item.item->>'item_id' = %s
             AND execution_item.item->>'output_format' = %s
            JOIN case_agent_task_heads head
              ON head.graph_id = task.graph_id AND head.task_id = task.task_id
             AND head.run_id = task.run_id AND head.firm_id = task.firm_id
             AND head.matter_id = task.matter_id AND head.is_current
            JOIN case_agent_task_attempts attempt
              ON attempt.attempt_id = %s AND attempt.run_id = task.run_id
             AND attempt.graph_id = task.graph_id AND attempt.task_id = task.task_id
             AND attempt.firm_id = task.firm_id AND attempt.matter_id = task.matter_id
            JOIN matter_actor_roles worker_role
              ON worker_role.matter_id = task.matter_id
             AND worker_role.firm_id = task.firm_id
             AND worker_role.user_id = %s
             AND worker_role.role = 'SYSTEM_WORKER'
             AND worker_role.revoked_at IS NULL
            JOIN users worker
              ON worker.user_id = worker_role.user_id
             AND worker.firm_id = worker_role.firm_id AND worker.status = 'ACTIVE'
            WHERE task.run_id = %s AND task.task_id = %s AND task.firm_id = %s
              AND task.input_hash = %s AND task.tool_id = %s
              AND task.graph_id = run.current_graph_id
              AND execution.plan_id::text = goal.active_plan_execution->>'plan_id'
              AND execution.plan_hash::text = goal.active_plan_execution->>'plan_hash'
              AND execution.source_run_id::text =
                  goal.active_plan_execution->>'source_run_id'
              AND task.sequence = execution_item.item_sequence
              AND (
                    SELECT count(*)
                    FROM case_agent_tasks exact_task
                    WHERE exact_task.run_id = run.run_id
                      AND exact_task.graph_id = run.current_graph_id
                      AND exact_task.firm_id = run.firm_id
                      AND exact_task.matter_id = run.matter_id
                  ) = jsonb_array_length(goal.active_plan_execution->'items')
              AND head.status IN ('RUNNING', 'UNKNOWN')
              AND attempt.status IN ('RUNNING', 'RECONCILING')
            """,
            (
                item_id,
                expected_format.value,
                attempt_id,
                self._worker.actor_id,
                run_id,
                task_id,
                self._worker.firm_id,
                task_input_hash,
                required_tool,
            ),
        ).fetchone()
        if row is None:
            raise PostgresDocumentBindingBlocked(
                "compiled document task is not current for this SYSTEM_WORKER"
            )
        stored_refs = tuple(row["input_refs"])
        if stored_refs != input_refs:
            raise PostgresDocumentBindingBlocked(
                "compiled document task references differ from persistence"
            )
        if int(row["snapshot_matter_version"]) != int(row["matter_version"]):
            raise PostgresDocumentBindingBlocked(
                "case changed after Agent planning; document task is stale"
            )
        return {
            "graph_id": str(row["graph_id"]),
            "matter_id": str(row["matter_id"]),
            "snapshot_hash": str(row["snapshot_hash"]),
            "matter_version": int(row["matter_version"]),
            "execution_plan_id": str(row["execution_plan_id"]),
            "execution_plan_hash": str(row["execution_plan_hash"]),
            "execution_source_run_id": str(row["execution_source_run_id"]),
            "execution_item_id": str(row["execution_item_id"]),
            "execution_item_hash": str(row["execution_item_hash"]),
            "execution_deliverable_kind": str(
                row["execution_deliverable_kind"]
            ),
            "execution_output_format": str(row["execution_output_format"]),
        }

    def _read_active_plan_item(
        self,
        connection: Any,
        *,
        matter_id: str,
        current_matter_version: int,
        item_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        row = connection.execute(
            """
            SELECT plan.plan_id, plan.plan_version, plan.status, plan.plan_hash,
                   plan.profile_id, plan.profile_hash, plan.activated_matter_version,
                   item.item_id, item.sequence, item.item_kind, item.readiness,
                   item.title, item.purpose, item.rationale, item.risk_if_omitted,
                   item.confidence, item.review_gate, item.delivery_target,
                   item.deliverable_kind, item.required_for_delivery,
                   item.is_primary_document,
                   ARRAY(
                       SELECT prerequisite.prerequisite_item_id::text
                       FROM case_work_plan_item_prerequisites prerequisite
                       WHERE prerequisite.plan_id = plan.plan_id
                         AND prerequisite.item_id = item.item_id
                         AND prerequisite.firm_id = plan.firm_id
                         AND prerequisite.matter_id = plan.matter_id
                       ORDER BY prerequisite.prerequisite_item_id
                   ) AS prerequisites
            FROM case_work_plan_heads head
            JOIN case_work_plans plan
              ON plan.plan_id = head.current_plan_id AND plan.firm_id = head.firm_id
             AND plan.matter_id = head.matter_id
            JOIN case_work_plan_items item
              ON item.plan_id = plan.plan_id AND item.firm_id = plan.firm_id
             AND item.matter_id = plan.matter_id
            WHERE head.matter_id = %s AND head.firm_id = %s
              AND item.item_id = %s AND plan.status = 'ACTIVE'
              AND plan.activated_matter_version = %s
            """,
            (matter_id, self._worker.firm_id, item_id, current_matter_version),
        ).fetchone()
        if row is None:
            raise PostgresDocumentBindingBlocked(
                "compiled document item is not in the current active work plan"
            )
        data = dict(row)
        plan = {
            key: data[key]
            for key in (
                "plan_id",
                "plan_version",
                "status",
                "plan_hash",
                "profile_id",
                "profile_hash",
                "activated_matter_version",
            )
        }
        plan.update({key: str(plan[key]) for key in ("plan_id", "profile_id")})
        plan.update({key: str(plan[key]) for key in ("plan_hash", "profile_hash", "status")})
        item = {
            key: data[key]
            for key in (
                "item_id",
                "sequence",
                "item_kind",
                "readiness",
                "title",
                "purpose",
                "rationale",
                "risk_if_omitted",
                "confidence",
                "review_gate",
                "delivery_target",
                "deliverable_kind",
                "required_for_delivery",
                "is_primary_document",
                "prerequisites",
            )
        }
        item["item_id"] = str(item["item_id"])
        return plan, item

    def _read_current_profile(
        self,
        connection: Any,
        *,
        matter_id: str,
        profile_id: str,
        profile_hash: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT profile.profile_id, profile.profile_version, profile.profile_hash,
                   profile.case_type_code, profile.procedure_stage,
                   profile.represented_position, profile.authority_scope_code,
                   profile.engagement_state, party.display_label
            FROM case_posture_profile_heads head
            JOIN case_posture_profiles profile
              ON profile.profile_id = head.current_profile_id
             AND profile.firm_id = head.firm_id AND profile.matter_id = head.matter_id
            JOIN case_party_versions party
              ON party.party_version_id = profile.represented_party_version_id
             AND party.firm_id = profile.firm_id AND party.matter_id = profile.matter_id
            WHERE head.matter_id = %s AND head.firm_id = %s
              AND profile.profile_id = %s AND profile.profile_hash = %s
              AND profile.status = 'CONFIRMED' AND profile.engagement_state = 'ACTIVE'
            """,
            (matter_id, self._worker.firm_id, profile_id, profile_hash),
        ).fetchone()
        if row is None:
            raise PostgresDocumentBindingBlocked(
                "represented party posture changed; document task is stale"
            )
        result = dict(row)
        result["profile_id"] = str(result["profile_id"])
        return result

    def _read_item_references(
        self,
        connection: Any,
        *,
        matter_id: str,
        plan_id: str,
        item_id: str,
    ) -> tuple[tuple[str, WorkPlanReference], ...]:
        rows = connection.execute(
            """
            SELECT reference_role, source_type, source_id, source_version,
                   source_hash, reference_use
            FROM case_work_plan_item_references
            WHERE plan_id = %s AND item_id = %s AND matter_id = %s AND firm_id = %s
            ORDER BY reference_role, source_type, source_id, source_version,
                     source_hash, reference_use
            """,
            (plan_id, item_id, matter_id, self._worker.firm_id),
        ).fetchall()
        if not rows:
            raise PostgresDocumentBindingBlocked(
                "active document work-plan item has no governed sources"
            )
        references: dict[
            tuple[str, str, str, str, str, str],
            tuple[str, WorkPlanReference],
        ] = {}
        for row in rows:
            reference_role = str(row["reference_role"])
            if reference_role not in {"TRIGGER", "SOURCE"}:
                raise PostgresDocumentBindingBlocked(
                    "document work-plan reference role is unsupported"
                )
            try:
                reference = WorkPlanReference(
                    source_type=WorkPlanSourceType(str(row["source_type"])),
                    source_id=str(row["source_id"]),
                    source_version=str(row["source_version"]),
                    source_hash=str(row["source_hash"]),
                    use=WorkPlanReferenceUse(str(row["reference_use"])),
                )
            except ValueError as error:
                raise PostgresDocumentBindingBlocked(
                    "document work-plan source type is unsupported"
                ) from error
            identity = (
                reference_role,
                reference.source_type.value,
                reference.source_id,
                reference.source_version,
                reference.source_hash,
                reference.use.value,
            )
            references[identity] = (reference_role, reference)
        return tuple(references[key] for key in sorted(references))

    def _read_source(
        self,
        connection: Any,
        *,
        matter_id: str,
        reference: WorkPlanReference,
    ) -> AuthoritativeDocumentSource:
        source_type = reference.source_type
        params = (reference.source_id, matter_id, self._worker.firm_id)
        if source_type is WorkPlanSourceType.AGENT_TASK_INPUT:
            return self._read_promoted_input_source(
                connection,
                matter_id=matter_id,
                reference=reference,
            )
        if source_type is WorkPlanSourceType.CASE_FACT:
            row = connection.execute(
                """SELECT original_text FROM case_facts
                   WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
                     AND status = 'CONFIRMED' AND decision_hash = %s""",
                (*params, reference.source_hash),
            ).fetchone()
            return _source(reference, DocumentSourceKind.CONFIRMED_FACT, "已确认事实", row, "original_text")
        if source_type is WorkPlanSourceType.CLAIM:
            row = connection.execute(
                """SELECT claim.original_claim_text, claim.claimed_amount, claim.currency,
                          response.position, response.partial_amount, response.currency AS partial_currency
                   FROM case_claims claim
                   LEFT JOIN case_claim_responses response
                     ON response.claim_id = claim.claim_id AND response.firm_id = claim.firm_id
                    AND response.matter_id = claim.matter_id
                   WHERE claim.claim_id = %s AND claim.matter_id = %s AND claim.firm_id = %s
                     AND claim.status = 'CONFIRMED_SCOPE' AND claim.confirmation_hash = %s""",
                (*params, reference.source_hash),
            ).fetchone()
            return _structured_source(reference, DocumentSourceKind.CONFIRMED_CLAIM, "已确认诉请范围", row)
        if source_type is WorkPlanSourceType.DISPUTE_ISSUE:
            row = connection.execute(
                """SELECT question FROM case_dispute_issues
                   WHERE issue_id = %s AND matter_id = %s AND firm_id = %s
                     AND status = 'CONFIRMED' AND approval_hash = %s""",
                (*params, reference.source_hash),
            ).fetchone()
            return _source(reference, DocumentSourceKind.CONFIRMED_ISSUE, "已确认争点", row, "question")
        if source_type is WorkPlanSourceType.TRANSACTION:
            row = connection.execute(
                """SELECT transaction.local_date, transaction.date_precision,
                          transaction.amount, transaction.currency, transaction.direction,
                          transaction.payer_label, transaction.payee_label,
                          transaction.channel, transaction.transaction_reference,
                          classification.nature, classification.same_day_sequence
                   FROM case_transactions transaction
                   LEFT JOIN case_payment_classifications classification
                     ON classification.transaction_id = transaction.transaction_id
                    AND classification.firm_id = transaction.firm_id
                    AND classification.matter_id = transaction.matter_id
                    AND classification.status = 'APPROVED'
                   WHERE transaction.transaction_id = %s
                     AND transaction.matter_id = %s AND transaction.firm_id = %s
                     AND transaction.status = 'CONFIRMED'
                     AND transaction.confirmation_hash = %s""",
                (*params, reference.source_hash),
            ).fetchone()
            return _structured_source(reference, DocumentSourceKind.CONFIRMED_TRANSACTION, "已确认交易", row)
        if source_type is WorkPlanSourceType.LEGAL_EVENT:
            row = connection.execute(
                """SELECT event_kind, local_date FROM case_legal_events
                   WHERE legal_event_id = %s AND matter_id = %s AND firm_id = %s
                     AND status = 'APPROVED' AND approval_hash = %s""",
                (*params, reference.source_hash),
            ).fetchone()
            return _structured_source(reference, DocumentSourceKind.CONFIRMED_PROCEDURAL_EVENT, "已确认法律事件", row)
        if source_type is WorkPlanSourceType.LEGAL_RULE_VERSION:
            row = connection.execute(
                """SELECT rule_id, rule_version, issue_key, effective_from, effective_to,
                          trigger_event_kind, formula_kind, base_annual_rate,
                          rate_multiplier, derived_annual_rate, required_fact_keys,
                          transition_rule_versions, conflict_set, priority
                   FROM legal_rule_versions
                   WHERE rule_version_id = %s AND firm_id = %s
                     AND status = 'APPROVED' AND approval_hash = %s""",
                (reference.source_id, self._worker.firm_id, reference.source_hash),
            ).fetchone()
            return _structured_source(reference, DocumentSourceKind.APPROVED_LEGAL_RULE, "已批准法律规则", row)
        if source_type is WorkPlanSourceType.LEGAL_SOURCE_SNAPSHOT:
            row = connection.execute(
                """SELECT snapshot_id, source_id, publisher, authority_level, official_url,
                          provision_locator, content_sha256, content_media_type,
                          storage_object_key
                   FROM official_legal_source_snapshots
                   WHERE snapshot_id = %s AND firm_id = %s
                     AND verification_status = 'VERIFIED' AND license_status = 'ACTIVE'
                     AND content_sha256 = %s""",
                (reference.source_id, self._worker.firm_id, reference.source_hash),
            ).fetchone()
            if row is None:
                raise PostgresDocumentBindingBlocked("verified legal source is unavailable")
            try:
                text = self._official_source_text.read_verified_source_text(
                    firm_id=self._worker.firm_id,
                    matter_id=matter_id,
                    snapshot_id=str(row["snapshot_id"]),
                    storage_object_key=str(row["storage_object_key"]),
                    content_sha256=str(row["content_sha256"]),
                    content_media_type=str(row["content_media_type"]),
                    provision_locator=str(row["provision_locator"]),
                )
            except Exception as error:
                raise PostgresDocumentBindingBlocked(
                    "verified legal source text could not be authenticated"
                ) from error
            try:
                projection = project_registered_legal_source_for_document(
                    source_id=str(row["source_id"]),
                    provision_locator=str(row["provision_locator"]),
                    literal_text=text,
                )
            except LegalProvisionDocumentProjectionBlocked as error:
                raise PostgresDocumentBindingBlocked(
                    "verified legal source cannot produce a bounded registered provision extract"
                ) from error
            metadata = {
                "publisher": row["publisher"],
                "authority_level": row["authority_level"],
                "official_url": row["official_url"],
                "provision_locator": row["provision_locator"],
                "reviewed_text": (
                    projection.reviewed_text if projection is not None else text
                ),
            }
            if projection is not None:
                metadata["source_projection"] = {
                    "schema_version": projection.schema_version,
                    "source_id": projection.source_id,
                    "provision_labels": list(projection.provision_labels),
                    "reviewed_text_sha256": projection.reviewed_text_sha256,
                }
            return _document_source(
                reference,
                DocumentSourceKind.VERIFIED_LEGAL_SOURCE,
                "已核验官方法源",
                _json_text(metadata),
            )
        if source_type is WorkPlanSourceType.CALCULATION_RUN:
            run = connection.execute(
                """SELECT total_interest_accrued, total_interest_paid,
                          remaining_principal, remaining_unpaid_interest,
                          unapplied_payments, generated_at
                   FROM calculation_runs
                   WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                     AND status = 'VERIFIED' AND output_hash = %s""",
                (*params, reference.source_hash),
            ).fetchone()
            if run is None:
                raise PostgresDocumentBindingBlocked("verified calculation is unavailable")
            lines = connection.execute(
                """SELECT line_sequence, period_start, period_end, opening_principal,
                          annual_rate, day_count, accrued_interest, closing_principal,
                          accrued_unpaid_interest, source_rule_version, evidence_ids
                   FROM calculation_line_items
                   WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                   ORDER BY line_sequence""",
                params,
            ).fetchall()
            allocations = connection.execute(
                """SELECT allocation_sequence, payment_event_id, effective_date,
                          payment_amount, allocated_interest, allocated_principal,
                          unapplied_amount, payment_application, evidence_ids
                   FROM calculation_payment_allocations
                   WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                   ORDER BY allocation_sequence""",
                params,
            ).fetchall()
            return _document_source(
                reference,
                DocumentSourceKind.APPROVED_CALCULATION,
                "已独立复算的利息结果",
                _json_text(
                    {
                        "summary": dict(run),
                        "periods": [dict(value) for value in lines],
                        "payment_allocations": [dict(value) for value in allocations],
                    }
                ),
            )
        if source_type is WorkPlanSourceType.EVIDENCE_PAGE:
            row = connection.execute(
                """SELECT page.page_number, original.original_file_sha256,
                          original.original_label, decision.disposition,
                          decision.reason
                   FROM evidence_pages page
                   JOIN evidence_original_files original
                     ON original.evidence_file_id = page.evidence_file_id
                    AND original.firm_id = page.firm_id AND original.matter_id = page.matter_id
                   JOIN evidence_page_decisions decision
                     ON decision.evidence_page_id = page.evidence_page_id
                    AND decision.firm_id = page.firm_id AND decision.matter_id = page.matter_id
                   WHERE page.evidence_page_id = %s AND page.matter_id = %s
                     AND page.firm_id = %s AND original.original_file_sha256 = %s
                     AND decision.status = 'APPROVED' AND decision.disposition = 'INCLUDE'""",
                (*params, reference.source_hash),
            ).fetchone()
            return _structured_source(reference, DocumentSourceKind.APPROVED_EVIDENCE_ITEM, "已纳入证据页", row)
        raise PostgresDocumentBindingBlocked(
            "active document work-plan cites a source unsupported by this release"
        )

    def _read_promoted_input_source(
        self,
        connection: Any,
        *,
        matter_id: str,
        reference: WorkPlanReference,
    ) -> AuthoritativeDocumentSource:
        """Safely unwrap one immutable 0043 binding into current source data.

        The work-plan reference hashes the promotion binding, not the client
        record itself.  Recompute the exact planning-object hash from the
        authoritative ledger before exposing any source text.  Raw materials
        and every unsupported projection type remain unavailable.
        """

        binding = connection.execute(
            """
            SELECT binding.object_type, binding.object_id, binding.object_version,
                   binding.content_hash, binding.source_status,
                   binding.reference_use, binding.binding_hash,
                   promotion.snapshot_matter_version
            FROM case_agent_work_plan_input_bindings binding
            JOIN case_agent_work_plan_promotions promotion
              ON promotion.promotion_id = binding.promotion_id
             AND promotion.plan_id = binding.plan_id
             AND promotion.firm_id = binding.firm_id
             AND promotion.matter_id = binding.matter_id
            JOIN case_work_plans plan
              ON plan.plan_id = binding.plan_id
             AND plan.firm_id = binding.firm_id
             AND plan.matter_id = binding.matter_id
            JOIN case_work_plan_heads head
              ON head.current_plan_id = plan.plan_id
             AND head.firm_id = plan.firm_id
             AND head.matter_id = plan.matter_id
            WHERE binding.binding_id = %s
              AND binding.matter_id = %s AND binding.firm_id = %s
              AND binding.object_version = %s
              AND binding.binding_hash = %s
              AND binding.reference_use = %s
              AND plan.status = 'ACTIVE'
            """,
            (
                reference.source_id,
                matter_id,
                self._worker.firm_id,
                reference.source_version,
                reference.source_hash,
                reference.use.value,
            ),
        ).fetchone()
        if binding is None:
            raise PostgresDocumentBindingBlocked(
                "promoted Agent source is not bound to the current active plan"
            )
        object_type = str(binding["object_type"])
        expected = {
            "CASE_FACT": (WorkPlanReferenceUse.FACT, "CONFIRMED"),
            "DISPUTE_ISSUE": (WorkPlanReferenceUse.FACT, "CONFIRMED"),
            "CASE_TRANSACTION": (
                WorkPlanReferenceUse.TRANSACTION,
                "CONFIRMED",
            ),
            "CASE_CLAIM": (WorkPlanReferenceUse.CLAIM_SCOPE, "CONFIRMED"),
            "EVIDENCE_PAGE": (WorkPlanReferenceUse.EVIDENCE, "CONFIRMED"),
            "VERIFIED_LEGAL_SOURCE": (
                WorkPlanReferenceUse.LEGAL_AUTHORITY,
                "LOCKED",
            ),
            "APPROVED_LEGAL_RULE": (WorkPlanReferenceUse.LEGAL_RULE, "LOCKED"),
        }.get(object_type)
        if (
            expected is None
            or str(binding["source_status"]) != expected[1]
            or str(binding["reference_use"]) != expected[0].value
        ):
            raise PostgresDocumentBindingBlocked(
                "promoted Agent source type is not approved for document disclosure"
            )
        if object_type in {"CASE_FACT", "CASE_TRANSACTION", "CASE_CLAIM"} and (
            str(binding["object_version"])
            != f"v{int(binding['snapshot_matter_version'])}"
        ):
            raise PostgresDocumentBindingBlocked(
                "promoted ledger source version differs from the verified planning snapshot"
            )
        if object_type == "DISPUTE_ISSUE":
            row = connection.execute(
                """
                SELECT issue_id, question, status, approval_hash, approved_by
                FROM case_dispute_issues
                WHERE issue_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (binding["object_id"], matter_id, self._worker.firm_id),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != "CONFIRMED"
                or row["approval_hash"] is None
                or row["approved_by"] is None
                or _payload_hash(
                    {
                        "schema_version": "planning-dispute-issue-v1",
                        **dict(row),
                    }
                )
                != str(binding["content_hash"])
            ):
                raise PostgresDocumentBindingBlocked(
                    "promoted dispute issue changed after the verified planning snapshot"
                )
            return self._read_source(
                connection,
                matter_id=matter_id,
                reference=WorkPlanReference(
                    source_type=WorkPlanSourceType.DISPUTE_ISSUE,
                    source_id=str(row["issue_id"]),
                    source_version=str(binding["object_version"]),
                    source_hash=str(row["approval_hash"]),
                    use=WorkPlanReferenceUse.FACT,
                ),
            )
        if object_type == "EVIDENCE_PAGE":
            row = connection.execute(
                """
                SELECT page.evidence_page_id, page.evidence_file_id, page.page_number,
                       page.rendered_page_sha256, original.original_file_sha256,
                       original.media_type, decision.decision_id, decision.disposition,
                       decision.status AS decision_status, decision.approval_hash
                FROM evidence_pages page
                JOIN evidence_original_files original
                  ON original.evidence_file_id = page.evidence_file_id
                 AND original.firm_id = page.firm_id
                 AND original.matter_id = page.matter_id
                LEFT JOIN LATERAL (
                  SELECT item.decision_id, item.disposition, item.status, item.approval_hash
                  FROM evidence_page_decisions item
                  WHERE item.evidence_page_id = page.evidence_page_id
                    AND item.firm_id = page.firm_id AND item.matter_id = page.matter_id
                    AND item.status <> 'INVALIDATED'
                  ORDER BY CASE WHEN item.status = 'APPROVED' THEN 0 ELSE 1 END,
                           item.created_at DESC, item.decision_id DESC
                  LIMIT 1
                ) decision ON true
                WHERE page.evidence_page_id = %s
                  AND page.matter_id = %s AND page.firm_id = %s
                """,
                (binding["object_id"], matter_id, self._worker.firm_id),
            ).fetchone()
            if (
                row is None
                or str(row["decision_status"]) != "APPROVED"
                or row["approval_hash"] is None
                or str(binding["object_version"])
                != f"v{int(binding['snapshot_matter_version'])}"
                or _payload_hash(
                    {
                        "schema_version": "planning-evidence-page-v1",
                        **dict(row),
                    }
                )
                != str(binding["content_hash"])
            ):
                raise PostgresDocumentBindingBlocked(
                    "promoted evidence page changed after the verified planning snapshot"
                )
            return self._read_source(
                connection,
                matter_id=matter_id,
                reference=WorkPlanReference(
                    source_type=WorkPlanSourceType.EVIDENCE_PAGE,
                    source_id=str(row["evidence_page_id"]),
                    source_version=str(binding["object_version"]),
                    # A catalogue identifies an admitted original page.  It
                    # must bind to the immutable original PDF, not a
                    # best-effort render hash: ordinary PDF intake does not
                    # create a raster for every page, and a missing raster
                    # must never turn a confirmed page into a fake one.
                    source_hash=str(row["original_file_sha256"]),
                    use=WorkPlanReferenceUse.EVIDENCE,
                ),
            )
        if object_type == "CASE_FACT":
            row = connection.execute(
                """
                SELECT fact_id, original_text, origin, status,
                       jsonb_array_length(evidence_links) AS evidence_count,
                       decision_hash, decided_by, evidence_links,
                       to_jsonb(case_facts)->>'correction_candidate_id' AS correction_candidate_id
                FROM case_facts
                WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'CONFIRMED' AND decision_hash IS NOT NULL
                """,
                (binding["object_id"], matter_id, self._worker.firm_id),
            ).fetchone()
            if row is None:
                raise PostgresDocumentBindingBlocked(
                    "promoted confirmed fact is no longer available"
                )
            planning_row = {
                **dict(row),
                "fact_id": str(row["fact_id"]),
                "decided_by": (
                    str(row["decided_by"])
                    if row["decided_by"] is not None
                    else None
                ),
            }
            expected_content_hash = _payload_hash(
                {
                    "schema_version": "planning-case-fact-v1",
                    **planning_row,
                }
            )
            if expected_content_hash != str(binding["content_hash"]):
                raise PostgresDocumentBindingBlocked(
                    "promoted fact changed after the verified planning snapshot"
                )
            direct_reference = WorkPlanReference(
                source_type=WorkPlanSourceType.CASE_FACT,
                source_id=str(row["fact_id"]),
                source_version=str(binding["object_version"]),
                source_hash=str(row["decision_hash"]),
                use=WorkPlanReferenceUse.FACT,
            )
            return self._read_source(
                connection,
                matter_id=matter_id,
                reference=direct_reference,
            )
        if object_type == "CASE_CLAIM":
            row = connection.execute(
                """
                SELECT claim.claim_id, claim.original_claim_text,
                       claim.claimed_amount, claim.currency, claim.status,
                       jsonb_array_length(claim.evidence_links) AS evidence_count,
                       claim.confirmation_hash, claim.confirmed_by,
                       response.claim_response_id, response.position,
                       response.partial_amount, response.currency AS response_currency,
                       response.approval_hash AS response_approval_hash,
                       response.approved_by AS response_approved_by,
                       COALESCE(
                           ARRAY(
                               SELECT response_fact.fact_id
                               FROM case_claim_response_facts response_fact
                               WHERE response_fact.claim_response_id = response.claim_response_id
                                 AND response_fact.matter_id = claim.matter_id
                                 AND response_fact.firm_id = claim.firm_id
                               ORDER BY response_fact.fact_id ASC
                           ),
                           ARRAY[]::uuid[]
                       ) AS confirmed_fact_ids
                FROM case_claims claim
                LEFT JOIN case_claim_responses response
                  ON response.claim_id = claim.claim_id
                 AND response.matter_id = claim.matter_id
                 AND response.firm_id = claim.firm_id
                WHERE claim.claim_id = %s AND claim.matter_id = %s AND claim.firm_id = %s
                  AND claim.status = 'CONFIRMED_SCOPE'
                  AND claim.confirmation_hash IS NOT NULL
                """,
                (binding["object_id"], matter_id, self._worker.firm_id),
            ).fetchone()
            if (
                row is None
                or row["claim_response_id"] is None
                or row["response_approval_hash"] is None
                or row["response_approved_by"] is None
                or not row["confirmed_fact_ids"]
            ):
                raise PostgresDocumentBindingBlocked(
                    "promoted confirmed claim has no approved, fact-bound response"
                )
            response = {
                "claim_response_id": str(row["claim_response_id"]),
                "position": row["position"],
                "partial_amount": row["partial_amount"],
                "currency": row["response_currency"],
                "confirmed_fact_ids": tuple(
                    str(value) for value in row["confirmed_fact_ids"]
                ),
                "approval_hash": row["response_approval_hash"],
                "approved_by": str(row["response_approved_by"]),
            }
            planning_row = {
                "claim_id": str(row["claim_id"]),
                "original_claim_text": row["original_claim_text"],
                "claimed_amount": row["claimed_amount"],
                "currency": row["currency"],
                "status": row["status"],
                "evidence_count": row["evidence_count"],
                "confirmation_hash": row["confirmation_hash"],
                "confirmed_by": str(row["confirmed_by"]),
                "response": response,
            }
            expected_content_hash = _payload_hash(
                {
                    "schema_version": "planning-case-claim-v1",
                    **planning_row,
                }
            )
            if expected_content_hash != str(binding["content_hash"]):
                raise PostgresDocumentBindingBlocked(
                    "promoted claim changed after the verified planning snapshot"
                )
            direct_reference = WorkPlanReference(
                source_type=WorkPlanSourceType.CLAIM,
                source_id=str(row["claim_id"]),
                source_version=str(binding["object_version"]),
                source_hash=str(row["confirmation_hash"]),
                use=WorkPlanReferenceUse.CLAIM_SCOPE,
            )
            return self._read_source(
                connection,
                matter_id=matter_id,
                reference=direct_reference,
            )
        if object_type == "VERIFIED_LEGAL_SOURCE":
            row = connection.execute(
                """
                SELECT source.snapshot_id, source.content_sha256
                FROM official_legal_source_snapshots source
                WHERE source.snapshot_id = %s AND source.firm_id = %s
                  AND source.verification_status = 'VERIFIED'
                  AND source.license_status = 'ACTIVE'
                  AND source.license_review_hash IS NOT NULL
                  AND source.content_sha256 = %s
                """,
                (
                    binding["object_id"],
                    self._worker.firm_id,
                    binding["content_hash"],
                ),
            ).fetchone()
            bundle_reference = connection.execute(
                """
                SELECT segment.segment_id
                FROM case_legal_bundles bundle
                JOIN case_legal_bundle_segments segment
                  ON segment.bundle_id = bundle.bundle_id
                 AND segment.firm_id = bundle.firm_id
                 AND segment.matter_id = bundle.matter_id
                WHERE bundle.firm_id = %s AND bundle.matter_id = %s
                  AND bundle.status = 'APPROVED'
                  AND (
                      (segment.source_snapshot_id = %s AND segment.source_sha256 = %s)
                      OR (
                          segment.parameter_source_snapshot_id = %s
                          AND segment.parameter_source_sha256 = %s
                      )
                  )
                LIMIT 1
                """,
                (
                    self._worker.firm_id,
                    matter_id,
                    binding["object_id"],
                    binding["content_hash"],
                    binding["object_id"],
                    binding["content_hash"],
                ),
            ).fetchone()
            if (
                row is None
                or bundle_reference is None
                or str(binding["object_version"]) != "v1"
                or str(row["snapshot_id"]) != str(binding["object_id"])
                or str(row["content_sha256"]) != str(binding["content_hash"])
            ):
                raise PostgresDocumentBindingBlocked(
                    "promoted verified legal source changed after the planning snapshot"
                )
            return self._read_source(
                connection,
                matter_id=matter_id,
                reference=WorkPlanReference(
                    source_type=WorkPlanSourceType.LEGAL_SOURCE_SNAPSHOT,
                    source_id=str(row["snapshot_id"]),
                    source_version="v1",
                    source_hash=str(row["content_sha256"]),
                    use=WorkPlanReferenceUse.LEGAL_AUTHORITY,
                ),
            )
        if object_type == "APPROVED_LEGAL_RULE":
            row = connection.execute(
                """
                SELECT rule_version_id, rule_version, status, approval_hash
                FROM legal_rule_versions
                WHERE rule_version_id = %s AND firm_id = %s
                """,
                (binding["object_id"], self._worker.firm_id),
            ).fetchone()
            bundle_reference = connection.execute(
                """
                SELECT segment.segment_id
                FROM case_legal_bundles bundle
                JOIN case_legal_bundle_segments segment
                  ON segment.bundle_id = bundle.bundle_id
                 AND segment.firm_id = bundle.firm_id
                 AND segment.matter_id = bundle.matter_id
                WHERE bundle.firm_id = %s AND bundle.matter_id = %s
                  AND bundle.status = 'APPROVED'
                  AND segment.rule_version_id = %s
                  AND segment.rule_version = %s
                LIMIT 1
                """,
                (
                    self._worker.firm_id,
                    matter_id,
                    binding["object_id"],
                    None if row is None else row["rule_version"],
                ),
            ).fetchone()
            if (
                row is None
                or bundle_reference is None
                or str(row["rule_version_id"]) != str(binding["object_id"])
                or str(binding["object_version"]) != "v1"
                or row["status"] != "APPROVED"
                or str(row["approval_hash"]) != str(binding["content_hash"])
            ):
                raise PostgresDocumentBindingBlocked(
                    "promoted approved legal rule changed after the planning snapshot"
                )
            return self._read_source(
                connection,
                matter_id=matter_id,
                reference=WorkPlanReference(
                    source_type=WorkPlanSourceType.LEGAL_RULE_VERSION,
                    source_id=str(row["rule_version_id"]),
                    source_version=str(row["rule_version"]),
                    source_hash=str(row["approval_hash"]),
                    use=WorkPlanReferenceUse.LEGAL_RULE,
                ),
            )
        row = connection.execute(
            """
            SELECT transaction_id, local_date, date_precision, amount, currency,
                   direction, payer_label, payee_label, channel,
                   transaction_reference, status,
                   jsonb_array_length(evidence_links) AS evidence_count,
                   confirmation_hash, confirmed_by
            FROM case_transactions
            WHERE transaction_id = %s AND matter_id = %s AND firm_id = %s
              AND status = 'CONFIRMED' AND confirmation_hash IS NOT NULL
            """,
            (binding["object_id"], matter_id, self._worker.firm_id),
        ).fetchone()
        if row is None:
            raise PostgresDocumentBindingBlocked(
                "promoted confirmed transaction is no longer available"
            )
        planning_row = {
            **dict(row),
            "transaction_id": str(row["transaction_id"]),
            "confirmed_by": (
                str(row["confirmed_by"]) if row["confirmed_by"] is not None else None
            ),
        }
        expected_content_hash = _payload_hash(
            {
                "schema_version": "planning-case-transaction-v1",
                **planning_row,
            }
        )
        if expected_content_hash != str(binding["content_hash"]):
            raise PostgresDocumentBindingBlocked(
                "promoted transaction changed after the verified planning snapshot"
            )
        direct_reference = WorkPlanReference(
            source_type=WorkPlanSourceType.TRANSACTION,
            source_id=str(row["transaction_id"]),
            source_version=str(binding["object_version"]),
            source_hash=str(row["confirmation_hash"]),
            use=WorkPlanReferenceUse.TRANSACTION,
        )
        return self._read_source(
            connection,
            matter_id=matter_id,
            reference=direct_reference,
        )

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self._worker.firm_id,),
            )
            # Content-revision job RLS needs both firm and the actual worker.
            # Firm-only reads silently hide the otherwise valid claimed job.
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)",
                (self._worker.actor_id,),
            )
            yield connection


def _case_work_plan_item(
    row: dict[str, Any],
    reference_bindings: tuple[tuple[str, WorkPlanReference], ...],
) -> CaseWorkPlanItem:
    prerequisites = tuple(str(value) for value in row.get("prerequisites", ()))
    trigger_refs = tuple(
        reference for role, reference in reference_bindings if role == "TRIGGER"
    )
    source_refs = tuple(
        reference for role, reference in reference_bindings if role == "SOURCE"
    )
    return CaseWorkPlanItem(
        item_id=str(row["item_id"]),
        sequence=int(row["sequence"]),
        kind=WorkPlanItemKind(str(row["item_kind"])),
        readiness=WorkPlanReadiness(str(row["readiness"])),
        title=str(row["title"]),
        purpose=str(row["purpose"]),
        rationale=str(row["rationale"]),
        prerequisites=prerequisites,
        trigger_refs=trigger_refs,
        source_refs=source_refs,
        risk_if_omitted=str(row["risk_if_omitted"]),
        confidence=float(row["confidence"]),
        review_gate=ReviewGate(str(row["review_gate"])),
        delivery_target=DeliveryTarget(str(row["delivery_target"])),
        deliverable_kind=str(row["deliverable_kind"]),
        required_for_delivery=bool(row["required_for_delivery"]),
        is_primary_document=bool(row["is_primary_document"]),
    )


def _posture_source(row: dict[str, Any]) -> AuthoritativeDocumentSource:
    return AuthoritativeDocumentSource(
        input_ref=f"posture-profile:{row['profile_id']}",
        source_kind=DocumentSourceKind.POSTURE_PROFILE,
        source_version=f"v{int(row['profile_version'])}",
        source_hash=str(row["profile_hash"]),
        label="当前已确认代理身份与程序阶段",
        text=_json_text(
            {
                "represented_party": row["display_label"],
                "represented_position": row["represented_position"],
                "procedure_stage": row["procedure_stage"],
                "case_type_code": row["case_type_code"],
                "authority_scope_code": row["authority_scope_code"],
                "engagement_state": row["engagement_state"],
            }
        ),
    )


def _work_plan_item_source(
    plan: dict[str, Any], row: dict[str, Any], input_ref: str
) -> AuthoritativeDocumentSource:
    return AuthoritativeDocumentSource(
        input_ref=input_ref,
        source_kind=DocumentSourceKind.WORK_PLAN_ITEM,
        source_version=f"v{int(plan['plan_version'])}",
        source_hash=str(plan["plan_hash"]),
        label="当前已确认动态办案计划事项",
        text=_json_text(
            {
                "title": row["title"],
                "purpose": row["purpose"],
                "rationale": row["rationale"],
                "risk_if_omitted": row["risk_if_omitted"],
                "delivery_target": row["delivery_target"],
                "deliverable_kind": row["deliverable_kind"],
                "required_for_delivery": row["required_for_delivery"],
                "is_primary_document": row["is_primary_document"],
            }
        ),
    )


def _source(
    reference: WorkPlanReference,
    kind: DocumentSourceKind,
    label: str,
    row: Any,
    key: str,
) -> AuthoritativeDocumentSource:
    if row is None:
        raise PostgresDocumentBindingBlocked(f"{label} is unavailable")
    return _document_source(reference, kind, label, str(row[key]))


def _structured_source(
    reference: WorkPlanReference,
    kind: DocumentSourceKind,
    label: str,
    row: Any,
) -> AuthoritativeDocumentSource:
    if row is None:
        raise PostgresDocumentBindingBlocked(f"{label} is unavailable")
    return _document_source(reference, kind, label, _json_text(dict(row)))


def _document_source(
    reference: WorkPlanReference,
    kind: DocumentSourceKind,
    label: str,
    text: str,
) -> AuthoritativeDocumentSource:
    prefix = {
        WorkPlanSourceType.CASE_FACT: "fact",
        WorkPlanSourceType.CLAIM: "claim",
        WorkPlanSourceType.DISPUTE_ISSUE: "issue",
        WorkPlanSourceType.TRANSACTION: "transaction",
        WorkPlanSourceType.LEGAL_EVENT: "legal-event",
        WorkPlanSourceType.LEGAL_RULE_VERSION: "legal-rule",
        WorkPlanSourceType.LEGAL_SOURCE_SNAPSHOT: "legal-source",
        WorkPlanSourceType.CALCULATION_RUN: "calculation",
        WorkPlanSourceType.EVIDENCE_PAGE: "evidence-page",
    }.get(reference.source_type)
    if prefix is None:
        raise PostgresDocumentBindingBlocked("document source prefix is unsupported")
    result = AuthoritativeDocumentSource(
        input_ref=f"{prefix}:{reference.source_id}",
        source_kind=kind,
        source_version=reference.source_version,
        source_hash=reference.source_hash,
        label=label,
        text=text,
    )
    try:
        result.validate()
    except CaseAgentDocumentDeliveryBlocked as error:
        raise PostgresDocumentBindingBlocked(str(error)) from error
    return result


def _single_work_plan_item_ref(input_refs: tuple[str, ...]) -> str:
    if not isinstance(input_refs, tuple) or len(input_refs) != 1:
        raise PostgresDocumentBindingBlocked(
            "document task must cite exactly one active work-plan item"
        )
    value = input_refs[0]
    prefix = "work-plan-item:"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise PostgresDocumentBindingBlocked("document task input is not a work-plan item")
    item_id = value[len(prefix) :]
    _uuid(item_id, "work_plan_item_id")
    return item_id


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"unsupported authoritative document value: {type(value).__name__}")


def _worker(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise ValueError("document binding requires a dedicated SYSTEM_WORKER")
    _uuid(actor.actor_id, "worker actor_id")
    _uuid(actor.firm_id, "worker firm_id")


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise PostgresDocumentBindingBlocked(f"{label} is invalid") from None


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise PostgresDocumentBindingBlocked(f"{label} is invalid")


__all__ = (
    "PostgresDocumentBindingBlocked",
    "PostgresDynamicDocumentBindingPort",
    "PostgresVerifiedLawyerDecisionPackagePort",
    "VerifiedLawyerDecisionPackagePort",
    "VerifiedOfficialSourceTextPort",
)
