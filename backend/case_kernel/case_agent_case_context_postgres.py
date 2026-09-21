"""PostgreSQL source binding for deterministic whole-case review.

The adapter re-authorises the dedicated firm Worker, current run, current
graph, exact task input hash and exact input-ref order in one repeatable-read
transaction.  It then rebuilds the existing authoritative case-ledger
snapshot and the same canonical planning object hashes used by the planner.

No new 0040 input-binding table is needed for this slice: case-ledger rows are
fenced by the run's full ledger snapshot/version; posture, active work-plan,
legal-source snapshots and procedural events are immutable/versioned objects
whose exact IDs and canonical hashes are re-read here.  A stale head, changed
license, changed matter snapshot or unavailable object fails closed.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Any, Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .case_agent_case_context import (
    BoundCaseContextProjection,
    BoundCaseContextSource,
    CaseContextReviewBlocked,
    CaseContextSourceType,
)
from .case_agent_planner import PlanningInputStatus
from .case_agent_planning_snapshot import PlanningProjectionObjectType
from .case_agent_planning_snapshot_postgres import (
    CASE_LEDGER_SNAPSHOT_SCHEMA_VERSION,
    _ledger_planning_objects,
    _read_case_ledger,
    _read_legal_sources,
    _read_posture,
    _read_procedural_events,
    _read_work_plan,
)
from .models import Actor, Role


_PREFIX_TYPE: dict[str, PlanningProjectionObjectType] = {
    "fact": PlanningProjectionObjectType.CASE_FACT,
    "claim": PlanningProjectionObjectType.CASE_CLAIM,
    "issue": PlanningProjectionObjectType.DISPUTE_ISSUE,
    "transaction": PlanningProjectionObjectType.CASE_TRANSACTION,
    "posture-profile": PlanningProjectionObjectType.POSTURE_PROFILE,
    "work-plan-item": PlanningProjectionObjectType.WORK_PLAN_ITEM,
    "legal-source": PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
    "legal-rule": PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
    "legal-event": PlanningProjectionObjectType.PROCEDURAL_EVENT,
    "review-obligation": PlanningProjectionObjectType.REVIEW_OBLIGATION,
    "transaction-candidate": PlanningProjectionObjectType.TRANSACTION_CANDIDATE,
    "fact-candidate": PlanningProjectionObjectType.FACT_CANDIDATE,
}

_SOURCE_TYPE: dict[PlanningProjectionObjectType, CaseContextSourceType] = {
    PlanningProjectionObjectType.CASE_FACT: CaseContextSourceType.CASE_FACT,
    PlanningProjectionObjectType.CASE_CLAIM: CaseContextSourceType.CASE_CLAIM,
    PlanningProjectionObjectType.DISPUTE_ISSUE: CaseContextSourceType.DISPUTE_ISSUE,
    PlanningProjectionObjectType.CASE_TRANSACTION: CaseContextSourceType.CASE_TRANSACTION,
    PlanningProjectionObjectType.POSTURE_PROFILE: CaseContextSourceType.POSTURE_PROFILE,
    PlanningProjectionObjectType.WORK_PLAN_ITEM: CaseContextSourceType.WORK_PLAN_ITEM,
    PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE: CaseContextSourceType.VERIFIED_LEGAL_SOURCE,
    PlanningProjectionObjectType.APPROVED_LEGAL_RULE: CaseContextSourceType.APPROVED_LEGAL_RULE,
    PlanningProjectionObjectType.PROCEDURAL_EVENT: CaseContextSourceType.PROCEDURAL_EVENT,
    PlanningProjectionObjectType.REVIEW_OBLIGATION: CaseContextSourceType.REVIEW_OBLIGATION,
    PlanningProjectionObjectType.TRANSACTION_CANDIDATE: CaseContextSourceType.TRANSACTION_CANDIDATE,
    PlanningProjectionObjectType.FACT_CANDIDATE: CaseContextSourceType.FACT_CANDIDATE,
}


class PostgresCaseContextProjectionPort:
    """Resolve structured case inputs without accepting browser payload data.

    The same authoritative projection is shared by deterministic context
    review and strict lawyer analysis.  The concrete Tool identity remains a
    constructor-owned server policy so an adapter cannot borrow another
    task's projection merely because its input refs happen to match.
    """

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        required_tool_id: str = "review_case_context",
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-context PostgreSQL DSN is required")
        if (
            not isinstance(worker_actor, Actor)
            or worker_actor.roles != frozenset({Role.SYSTEM_WORKER})
        ):
            raise PermissionError(
                "case-context projection requires a dedicated SYSTEM_WORKER"
            )
        _uuid(worker_actor.actor_id, "case-context worker actor_id")
        _uuid(worker_actor.firm_id, "case-context worker firm_id")
        if required_tool_id not in {
            "review_case_context",
            "plan_authoritative_rule_research",
            "analyze_lawyer_decision_package",
        }:
            raise ValueError("case-context projection Tool identity is invalid")
        self._dsn = dsn.strip()
        self._worker = worker_actor
        self._required_tool_id = required_tool_id

    def __repr__(self) -> str:
        return "PostgresCaseContextProjectionPort(<tenant-scoped>)"

    def validate_request_repair_binding(self, request) -> None:
        with self._transaction() as connection:
            _read_task_binding(connection, worker=self._worker, run_id=request.run_id,
                task_id=request.task_id, task_input_hash=request.task_input_hash,
                input_refs=request.input_refs, required_tool_id=self._required_tool_id)
            row = connection.execute("""
                SELECT checkpoint.projection->'analysis_stage'->>'repaired_request_hash' AS repaired_hash
                FROM case_agent_runs run JOIN case_agent_checkpoints checkpoint
                  ON checkpoint.run_id=run.run_id AND checkpoint.firm_id=run.firm_id
                 AND checkpoint.matter_id=run.matter_id AND checkpoint.event_version=run.current_event_version
                WHERE run.run_id=%s AND run.firm_id=%s AND run.matter_id=%s
                """, (request.run_id, self._worker.firm_id, request.matter_id)).fetchone()
            if row is None or (row["repaired_hash"] is not None and row["repaired_hash"] != request.request_hash):
                raise CaseContextReviewBlocked("reviewed repaired request differs before submission")

    def project_case_context(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> BoundCaseContextProjection:
        _uuid(run_id, "case-context run_id")
        _uuid(task_id, "case-context task_id")
        if not isinstance(task_input_hash, str) or len(task_input_hash) != 64:
            raise CaseContextReviewBlocked(
                "case-context task input hash is invalid"
            )
        parsed_refs = _parse_refs(input_refs)
        with self._transaction() as connection:
            task = _read_task_binding(
                connection,
                worker=self._worker,
                run_id=run_id,
                task_id=task_id,
                task_input_hash=task_input_hash,
                input_refs=input_refs,
                required_tool_id=self._required_tool_id,
            )
            ledger = _read_case_ledger(
                connection,
                firm_id=self._worker.firm_id,
                matter_id=task["matter_id"],
            )
            if (
                ledger.snapshot.schema_version
                != CASE_LEDGER_SNAPSHOT_SCHEMA_VERSION
                or ledger.snapshot.matter_version != task["snapshot_matter_version"]
                or ledger.snapshot.snapshot_hash != task["snapshot_hash"]
            ):
                raise CaseContextReviewBlocked(
                    "case-context case ledger changed after the Agent run snapshot"
                )

            objects, _ = _ledger_planning_objects(ledger)
            posture, work_plan, legal, procedure = _read_optional_objects(
                connection,
                firm_id=self._worker.firm_id,
                matter_id=task["matter_id"],
                matter_version=ledger.snapshot.matter_version,
                requested_types=frozenset(item[0] for item in parsed_refs),
            )
            objects.extend(posture["objects"])
            objects.extend(work_plan["objects"])
            objects.extend(legal["objects"])
            objects.extend(procedure["objects"])
            object_index = {(item.object_type, item.object_id): item for item in objects}
            row_index = _row_index(
                ledger=ledger,
                posture=posture,
                work_plan=work_plan,
                legal=legal,
                procedure=procedure,
            )
            sources: list[BoundCaseContextSource] = []
            if any(kind is PlanningProjectionObjectType.REVIEW_OBLIGATION for kind, _ in parsed_refs):
                # Re-read current lifecycle decisions and original-page bindings
                # inside this same transaction; never accept browser note data.
                from .case_agent_planning_snapshot_postgres import read_authoritative_projection_in_transaction
                from .case_agent_review_obligations import bind_review_obligation, REVIEW_OBLIGATION_CODES
                current = read_authoritative_projection_in_transaction(connection,
                    firm_id=self._worker.firm_id, matter_id=task["matter_id"],
                    actor=self._worker, expected_case_snapshot=ledger.snapshot)
                authorized = frozenset(item.ref_id for item in current.objects)
                for item in current.objects:
                    if item.object_type is PlanningProjectionObjectType.REVIEW_OBLIGATION:
                        object_index[(item.object_type, item.object_id)] = item
                for signal in current.lawyer_signals:
                    if signal.code in REVIEW_OBLIGATION_CODES:
                        obligation = bind_review_obligation(signal=signal, authorized_refs=authorized)
                        row_index[(PlanningProjectionObjectType.REVIEW_OBLIGATION, obligation.obligation_id)] = {
                            "review_note": obligation.review_note, "code": obligation.code,
                            "source_ref_ids": obligation.source_ref_ids,
                            "decision_hash": obligation.decision_hash}
            from .case_agent_transaction_candidates import (read_transaction_candidates,
                transaction_candidate_planning_object, read_fact_candidates, fact_candidate_planning_object)
            for candidate_type, reader, binder in (
                (PlanningProjectionObjectType.TRANSACTION_CANDIDATE, read_transaction_candidates, transaction_candidate_planning_object),
                (PlanningProjectionObjectType.FACT_CANDIDATE, read_fact_candidates, fact_candidate_planning_object),
            ):
                if not any(kind is candidate_type for kind, _ in parsed_refs):
                    continue
                for candidate in reader(connection, firm_id=self._worker.firm_id,
                                                              matter_id=task["matter_id"]):
                    key = (candidate_type, candidate.candidate_id)
                    object_index[key] = binder(candidate)
                    row_index[key] = {"primary_text": candidate.primary_text, "secondary_text": candidate.secondary_text,
                        "signals": candidate.signals, "confidence": candidate.confidence}
            for input_ref, (object_type, object_id) in zip(
                input_refs, parsed_refs, strict=True
            ):
                planning_object = object_index.get((object_type, object_id))
                raw = row_index.get((object_type, object_id))
                if planning_object is None or raw is None:
                    raise CaseContextReviewBlocked(
                        "case-context input is not current in its authoritative ledger"
                    )
                sources.append(
                    _source_projection(
                        input_ref=input_ref,
                        object_type=object_type,
                        planning_object=planning_object,
                        raw=raw,
                    )
                )

        return BoundCaseContextProjection.build(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            firm_id=self._worker.firm_id,
            matter_id=task["matter_id"],
            matter_version=ledger.snapshot.matter_version,
            case_snapshot_hash=ledger.snapshot.snapshot_hash,
            input_refs=input_refs,
            sources=tuple(sources),
        )

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self._worker.firm_id,),
            )
            yield connection


def _read_task_binding(
    connection: Any,
    *,
    worker: Actor,
    run_id: str,
    task_id: str,
    task_input_hash: str,
    input_refs: tuple[str, ...],
    required_tool_id: str = "review_case_context",
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT task.matter_id, task.input_refs, task.input_hash, task.tool_id,
               task.graph_id, graph.graph_hash,
               graph.snapshot_matter_version AS graph_snapshot_matter_version,
               graph.snapshot_schema_version AS graph_snapshot_schema_version,
               graph.snapshot_hash AS graph_snapshot_hash,
               run.current_graph_id, run.current_graph_hash,
               run.snapshot_matter_version AS run_snapshot_matter_version,
               run.snapshot_schema_version AS run_snapshot_schema_version,
               run.snapshot_hash AS run_snapshot_hash,
               matter.version AS matter_version
        FROM case_agent_tasks task
        JOIN case_agent_task_graphs graph
          ON graph.graph_id = task.graph_id AND graph.run_id = task.run_id
         AND graph.firm_id = task.firm_id AND graph.matter_id = task.matter_id
        JOIN case_agent_runs run
          ON run.run_id = task.run_id AND run.firm_id = task.firm_id
         AND run.matter_id = task.matter_id
        JOIN matters matter
          ON matter.matter_id = task.matter_id AND matter.firm_id = task.firm_id
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
          AND task.input_hash = %s
        """,
        (worker.actor_id, run_id, task_id, worker.firm_id, task_input_hash),
    ).fetchone()
    if row is None:
        raise CaseContextReviewBlocked(
            "case-context task is unavailable to this SYSTEM_WORKER"
        )
    stored_refs = tuple(row["input_refs"])
    if stored_refs != input_refs:
        raise CaseContextReviewBlocked(
            "case-context input refs differ from the persisted task"
        )
    if row["tool_id"] != required_tool_id:
        raise CaseContextReviewBlocked(
            "case-context task tool differs from the projection port"
        )
    if (
        str(row["current_graph_id"]) != str(row["graph_id"])
        or str(row["current_graph_hash"]) != str(row["graph_hash"])
        or int(row["graph_snapshot_matter_version"])
        != int(row["matter_version"])
        or int(row["run_snapshot_matter_version"])
        != int(row["graph_snapshot_matter_version"])
        or str(row["graph_snapshot_schema_version"])
        != str(row["run_snapshot_schema_version"])
        or str(row["graph_snapshot_hash"]) != str(row["run_snapshot_hash"])
    ):
        raise CaseContextReviewBlocked(
            "case-context run, graph or matter snapshot is stale"
        )
    return {
        "matter_id": str(row["matter_id"]),
        "snapshot_matter_version": int(row["graph_snapshot_matter_version"]),
        "snapshot_hash": str(row["graph_snapshot_hash"]),
    }


def _read_optional_objects(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    matter_version: int,
    requested_types: frozenset[PlanningProjectionObjectType],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    posture_state, posture_value, posture_object = _read_posture(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        configured=PlanningProjectionObjectType.POSTURE_PROFILE in requested_types,
    )
    _ = posture_state
    work_state, work_value, work_objects = _read_work_plan(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        matter_version=matter_version,
        configured=PlanningProjectionObjectType.WORK_PLAN_ITEM in requested_types,
    )
    _ = work_state
    legal_state, legal_objects = _read_legal_sources(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        configured=bool(
            {
                PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
            }
            & requested_types
        ),
    )
    _ = legal_state
    procedure_state, procedure_objects = _read_procedural_events(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        configured=PlanningProjectionObjectType.PROCEDURAL_EVENT in requested_types,
    )
    _ = procedure_state

    work_rows: tuple[dict[str, Any], ...] = ()
    if work_value is not None:
        work_rows = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT item.item_id, item.item_kind, item.readiness, item.title,
                       item.rationale, item.risk_if_omitted, item.confidence,
                       item.review_gate, item.delivery_target,
                       item.deliverable_kind, item.required_for_delivery,
                       item.is_primary_document
                FROM case_work_plan_items item
                WHERE item.plan_id = %s AND item.firm_id = %s
                  AND item.matter_id = %s
                ORDER BY item.sequence ASC, item.item_id ASC
                """,
                (work_value.plan_id, firm_id, matter_id),
            ).fetchall()
        )

    legal_source_objects = tuple(
        item
        for item in legal_objects
        if item.object_type is PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE
    )
    legal_rule_objects = tuple(
        item
        for item in legal_objects
        if item.object_type is PlanningProjectionObjectType.APPROVED_LEGAL_RULE
    )
    legal_rows: tuple[dict[str, Any], ...] = ()
    if legal_source_objects:
        legal_rows = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT snapshot_id, source_id, publisher, authority_level,
                       provision_locator, content_sha256
                FROM official_legal_source_snapshots
                WHERE firm_id = %s AND snapshot_id = ANY(%s::uuid[])
                  AND verification_status = 'VERIFIED'
                  AND license_status = 'ACTIVE'
                  AND license_review_hash IS NOT NULL
                ORDER BY snapshot_id ASC
                """,
                (firm_id, [item.object_id for item in legal_source_objects]),
            ).fetchall()
        )
    legal_rule_rows: tuple[dict[str, Any], ...] = ()
    if legal_rule_objects:
        legal_rule_rows = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT rule_version_id, rule_id, rule_version, issue_key,
                       trigger_event_kind, formula_kind, approval_hash
                FROM legal_rule_versions
                WHERE firm_id = %s AND rule_version_id = ANY(%s::uuid[])
                  AND status = 'APPROVED' AND approval_hash IS NOT NULL
                ORDER BY rule_version_id ASC
                """,
                (firm_id, [item.object_id for item in legal_rule_objects]),
            ).fetchall()
        )

    procedure_rows: tuple[dict[str, Any], ...] = ()
    if procedure_objects:
        procedure_rows = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT legal_event_id, event_kind, local_date, approval_hash
                FROM case_legal_events
                WHERE firm_id = %s AND matter_id = %s AND status = 'APPROVED'
                  AND legal_event_id = ANY(%s::uuid[])
                ORDER BY local_date ASC, legal_event_id ASC
                """,
                (firm_id, matter_id, [item.object_id for item in procedure_objects]),
            ).fetchall()
        )
    return (
        {
            "objects": (posture_object,) if posture_object is not None else (),
            "value": posture_value,
        },
        {"objects": work_objects, "value": work_value, "rows": work_rows},
        {
            "objects": legal_objects,
            "rows": legal_rows,
            "rule_rows": legal_rule_rows,
        },
        {"objects": procedure_objects, "rows": procedure_rows},
    )


def _row_index(
    *, ledger: Any, posture: dict[str, Any], work_plan: dict[str, Any],
    legal: dict[str, Any], procedure: dict[str, Any]
) -> dict[tuple[PlanningProjectionObjectType, str], dict[str, Any]]:
    result: dict[tuple[PlanningProjectionObjectType, str], dict[str, Any]] = {}
    for object_type, rows, key in (
        (PlanningProjectionObjectType.CASE_FACT, ledger.facts, "fact_id"),
        (PlanningProjectionObjectType.CASE_CLAIM, ledger.claims, "claim_id"),
        (PlanningProjectionObjectType.DISPUTE_ISSUE, ledger.issues, "issue_id"),
        (
            PlanningProjectionObjectType.CASE_TRANSACTION,
            ledger.transactions,
            "transaction_id",
        ),
        (
            PlanningProjectionObjectType.WORK_PLAN_ITEM,
            work_plan.get("rows", ()),
            "item_id",
        ),
        (
            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
            legal.get("rows", ()),
            "snapshot_id",
        ),
        (
            PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
            legal.get("rule_rows", ()),
            "rule_version_id",
        ),
        (
            PlanningProjectionObjectType.PROCEDURAL_EVENT,
            procedure.get("rows", ()),
            "legal_event_id",
        ),
    ):
        for row in rows:
            result[(object_type, str(row[key]))] = dict(row)
    posture_value = posture.get("value")
    if posture_value is not None:
        result[
            (PlanningProjectionObjectType.POSTURE_PROFILE, posture_value.profile_id)
        ] = {
            "case_type_code": posture_value.case_type_code,
            "procedure_stage": posture_value.procedure_stage,
            "represented_position": posture_value.represented_position,
            "authority_scope_code": posture_value.authority_scope_code,
            "engagement_state": posture_value.engagement_state,
        }
    return result


def _source_projection(
    *, input_ref: str, object_type: PlanningProjectionObjectType,
    planning_object: Any, raw: dict[str, Any]
) -> BoundCaseContextSource:
    primary, secondary, signals, confidence = _display_fields(object_type, raw)
    return BoundCaseContextSource(
        input_ref=input_ref,
        source_type=_SOURCE_TYPE[object_type],
        object_id=planning_object.object_id,
        object_version=planning_object.object_version,
        content_hash=planning_object.content_hash,
        status=planning_object.status,
        primary_text=_bounded(primary, 4_000),
        secondary_text=_bounded(secondary, 8_000),
        signals=tuple(sorted(set(signals))),
        confidence=confidence,
    )


def _display_fields(
    object_type: PlanningProjectionObjectType, row: dict[str, Any]
) -> tuple[str, str, tuple[str, ...], float | None]:
    if object_type in {PlanningProjectionObjectType.TRANSACTION_CANDIDATE, PlanningProjectionObjectType.FACT_CANDIDATE}:
        return row["primary_text"], row["secondary_text"], row["signals"], row["confidence"]
    if object_type is PlanningProjectionObjectType.REVIEW_OBLIGATION:
        return (str(row["review_note"]),
            "仅为未决复核事项，不是已确认事实。原页引用：" + "、".join(row["source_ref_ids"]),
            (str(row["code"]),), None)
    if object_type is PlanningProjectionObjectType.CASE_FACT:
        return (
            str(row["original_text"]),
            f"状态：{row['status']}；来源：{row['origin']}；关联证据：{row['evidence_count']}处。",
            (str(row["status"]),),
            None,
        )
    if object_type is PlanningProjectionObjectType.CASE_CLAIM:
        response = row.get("response")
        response_text = "尚无已批准答复口径"
        signals = [str(row["status"])]
        if isinstance(response, dict):
            response_text = f"答复口径：{response['position']}"
            signals.append(str(response["position"]))
        return (
            str(row["original_claim_text"]),
            f"诉请金额：{_scalar(row.get('claimed_amount'))} {row.get('currency') or '币种未明确'}；"
            f"范围状态：{row['status']}；{response_text}；关联证据：{row['evidence_count']}处。",
            tuple(signals),
            None,
        )
    if object_type is PlanningProjectionObjectType.DISPUTE_ISSUE:
        return (
            str(row["question"]),
            f"状态：{row['status']}；关联诉请：{len(row['claim_ids'])}项；"
            f"关联已确认事实：{len(row['confirmed_fact_ids'])}项。",
            (str(row["status"]),),
            None,
        )
    if object_type is PlanningProjectionObjectType.CASE_TRANSACTION:
        when = _scalar(row.get("local_date")) or "日期待确认"
        amount = _scalar(row.get("amount")) or "金额待确认"
        currency = str(row.get("currency") or "币种待确认")
        return (
            f"{when}｜{row.get('direction') or '方向待确认'}｜{amount} {currency}",
            f"{row.get('payer_label') or '付款方待确认'} → {row.get('payee_label') or '收款方待确认'}；"
            f"渠道：{row.get('channel') or '待确认'}；流水号：{row.get('transaction_reference') or '未记录'}；"
            f"状态：{row['status']}；关联证据：{row['evidence_count']}处。",
            (str(row["status"]),),
            None,
        )
    if object_type is PlanningProjectionObjectType.POSTURE_PROFILE:
        return (
            f"代理身份：{row['represented_position']}｜程序阶段：{row['procedure_stage']}",
            f"案件类型：{row['case_type_code']}；授权范围：{row['authority_scope_code']}；"
            f"委托状态：{row['engagement_state']}。代理身份仅作为研判输入，不直接映射固定文书。",
            ("CURRENT_POSTURE",),
            None,
        )
    if object_type is PlanningProjectionObjectType.WORK_PLAN_ITEM:
        signals = [str(row["item_kind"]), str(row["readiness"])]
        if row.get("required_for_delivery"):
            signals.append("REQUIRED_FOR_DELIVERY")
        if row.get("is_primary_document"):
            signals.append("PRIMARY_DOCUMENT")
        return (
            str(row["title"]),
            f"当前状态：{row['readiness']}；说明：{row['rationale']}；"
            f"遗漏风险：{row['risk_if_omitted']}；复核门：{row['review_gate']}。",
            tuple(signals),
            float(row["confidence"]),
        )
    if object_type is PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE:
        return (
            f"{row['publisher']}｜{row['provision_locator']}",
            f"法源编号：{row['source_id']}；效力类型：{row['authority_level']}；"
            "该快照已核验且许可有效，但本案适用性仍须结合事件与期间判断。",
            (str(row["authority_level"]), "VERIFIED", "LICENSE_ACTIVE"),
            None,
        )
    if object_type is PlanningProjectionObjectType.APPROVED_LEGAL_RULE:
        return (
            f"已批准规则：{row['rule_id']}｜{row['rule_version']}",
            f"规则键：{row['issue_key']}；触发事件：{row['trigger_event_kind']}；"
            f"规则类型：{row['formula_kind']}。该规则已绑定当前法源和批准记录，"
            "但本案适用、参数取值和任何计算结论仍须由规则引擎及律师复核。",
            ("APPROVED", "RULE_BINDING", str(row["formula_kind"])),
            None,
        )
    if object_type is PlanningProjectionObjectType.PROCEDURAL_EVENT:
        return (
            f"{_scalar(row['local_date'])}｜{row['event_kind']}",
            "该程序事件已由律师批准；任何期限计算仍需独立规则引擎和律师确认。",
            (str(row["event_kind"]), "APPROVED"),
            None,
        )
    raise CaseContextReviewBlocked("case-context object type is unsupported")


def _parse_refs(
    input_refs: tuple[str, ...],
) -> tuple[tuple[PlanningProjectionObjectType, str], ...]:
    if (
        not isinstance(input_refs, tuple)
        or not input_refs
        or len(input_refs) > 500
        or len(input_refs) != len(set(input_refs))
    ):
        raise CaseContextReviewBlocked("case-context input refs are invalid")
    result: list[tuple[PlanningProjectionObjectType, str]] = []
    for value in input_refs:
        if not isinstance(value, str) or ":" not in value:
            raise CaseContextReviewBlocked(
                "case-context input ref has an unsupported prefix"
            )
        prefix, object_id = value.split(":", 1)
        object_type = _PREFIX_TYPE.get(prefix)
        if object_type is None:
            raise CaseContextReviewBlocked(
                "case-context Skill received a material or unknown input type"
            )
        _uuid(object_id, "case-context input object_id")
        result.append((object_type, object_id))
    return tuple(result)


def _bounded(value: object, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        return "未记录"
    if len(text) <= maximum:
        return text
    return text[: maximum - 8].rstrip() + "（已截断）"


def _scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, Decimal)):
        return str(value)
    return str(value)


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseContextReviewBlocked(f"{label} must be a UUID") from error


__all__ = ("PostgresCaseContextProjectionPort",)
