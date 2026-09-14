"""PostgreSQL command store for immutable 0047 exception groups.

The public command surface accepts no candidate body, candidate id, hash,
page id or subset.  It locks the matter, re-reads the complete server-owned
group, re-computes its two canonical hashes and then inserts one terminal
route.  Intermediate group decisions do not claim an authoritative matter
change.  The final all-exception run advances the matter exactly once; mixed
runs reuse the refresh request created by the 0042 low-risk confirmation.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .case_agent_ledger_exception_review import (
    LedgerExceptionBatchState,
    LedgerExceptionDecision,
    LedgerExceptionGroup,
    LedgerExceptionGroupMember,
    LedgerExceptionGroupMemberPage,
    LedgerExceptionMemberExcerpt,
    LedgerExceptionReason,
    LedgerExceptionReextractionCohortCapacityExceeded,
    LedgerExceptionReextractionSourceWindowExceeded,
    LedgerExceptionReviewBlocked,
    LedgerExceptionRiskPolicy,
    LedgerExceptionSourcePolicy,
    LedgerExtractionBatchReviewStatus,
    LedgerLowRiskLaneStatus,
    allowed_exception_decisions,
    canonical_reason_codes,
    exception_candidate_set_hash,
    exception_decision_request_hash,
    exception_group_key_hash,
    exception_group_summary,
    exception_risk_policy,
    exception_source_policy,
)
from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    _authorize_matter_read,
    _require_positive_version,
    _require_roles,
    _validate_command_identity,
    _validate_uuid,
)
from .errors import IdempotencyConflict, VersionConflict
from .models import Actor, Role


_READ_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)
_WRITE_ROLES = frozenset({Role.LEAD_LAWYER})
_COMMAND = "DECIDE_CASE_LEDGER_EXCEPTION_GROUP"
_EXCEPTION_VERSION_CONFLICT_SQLSTATE = "P4091"
_EXCEPTION_TERMINAL_INTENT_CONFLICT_SQLSTATE = "P4092"
_REEXTRACTION_SOURCE_WINDOW_EXCEEDED_SQLSTATE = "P6401"
_REEXTRACTION_COHORT_CAPACITY_EXCEEDED_SQLSTATE = "P9901"


class PostgresCaseLedgerExceptionReviewStore:
    """Read fixed exception groups and persist one full-group terminal route."""

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip() or dsn != dsn.strip():
            raise ValueError("case-Agent ledger exception PostgreSQL DSN is required")
        self._dsn = dsn

    def list_exception_groups(
        self,
        *,
        matter_id: str,
        actor: Actor,
        extraction_batch_id: str,
    ) -> tuple[LedgerExceptionGroup, ...]:
        _validate_command_identity(
            matter_id=matter_id,
            actor=actor,
            idempotency_key=f"read-ledger-exceptions:{extraction_batch_id}",
        )
        _require_roles(actor, _READ_ROLES)
        _validate_uuid("extraction_batch_id", extraction_batch_id)
        with _TenantTransaction(
            self._dsn, actor.firm_id, read_only=True
        ) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES,
            )
            groups = connection.execute(
                """
                SELECT exception_group.exception_group_id,
                       exception_group.extraction_batch_id,
                       exception_group.candidate_kind,
                       exception_group.canonical_reason_codes,
                       exception_group.source_policy,
                       exception_group.risk_policy,
                       exception_group.group_key_hash,
                       exception_group.candidate_set_hash,
                       exception_group.candidate_count,
                       decision.decision, decision.reason_code
                  FROM case_agent_ledger_exception_groups exception_group
                  LEFT JOIN case_agent_ledger_exception_group_decisions decision
                    ON decision.exception_group_id =
                            exception_group.exception_group_id
                   AND decision.extraction_batch_id =
                            exception_group.extraction_batch_id
                   AND decision.firm_id = exception_group.firm_id
                   AND decision.matter_id = exception_group.matter_id
                 WHERE exception_group.extraction_batch_id = %s
                   AND exception_group.firm_id = %s
                   AND exception_group.matter_id = %s
                 ORDER BY exception_group.candidate_kind,
                          exception_group.group_key_hash
                """,
                (extraction_batch_id, actor.firm_id, matter_id),
            ).fetchall()
            result = tuple(
                self._validate_group(
                    connection,
                    row=dict(row),
                    actor=actor,
                    matter_id=matter_id,
                )
                for row in groups
            )
            state = self._read_batch_state(
                connection,
                actor=actor,
                matter_id=matter_id,
                extraction_batch_id=extraction_batch_id,
            )
            if len(result) != state.exception_group_count:
                raise LedgerExceptionReviewBlocked(
                    "exception group projection differs from batch status"
                )
            if sum(group.decision is not None for group in result) != (
                state.decided_exception_group_count
            ):
                raise LedgerExceptionReviewBlocked(
                    "exception decision projection differs from batch status"
                )
            return result

    def list_exception_group_members(
        self,
        *,
        matter_id: str,
        actor: Actor,
        exception_group_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> LedgerExceptionGroupMemberPage:
        """Return a stable browser-safe page; ids/hashes stay server-private."""

        _validate_command_identity(
            matter_id=matter_id,
            actor=actor,
            idempotency_key=f"read-ledger-exception-group:{exception_group_id}",
        )
        _require_roles(actor, _READ_ROLES)
        _validate_uuid("exception_group_id", exception_group_id)
        if type(offset) is not int or not 0 <= offset < 500:
            raise LedgerExceptionReviewBlocked("exception member offset is invalid")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise LedgerExceptionReviewBlocked("exception member page size is invalid")
        with _TenantTransaction(
            self._dsn, actor.firm_id, read_only=True
        ) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES,
            )
            raw_group = connection.execute(
                """
                SELECT exception_group.exception_group_id,
                       exception_group.extraction_batch_id,
                       exception_group.candidate_kind,
                       exception_group.canonical_reason_codes,
                       exception_group.source_policy,
                       exception_group.risk_policy,
                       exception_group.group_key_hash,
                       exception_group.candidate_set_hash,
                       exception_group.candidate_count,
                       decision.decision, decision.reason_code
                  FROM case_agent_ledger_exception_groups exception_group
                  LEFT JOIN case_agent_ledger_exception_group_decisions decision
                    ON decision.exception_group_id =
                            exception_group.exception_group_id
                   AND decision.extraction_batch_id =
                            exception_group.extraction_batch_id
                   AND decision.firm_id = exception_group.firm_id
                   AND decision.matter_id = exception_group.matter_id
                 WHERE exception_group.exception_group_id = %s
                   AND exception_group.firm_id = %s
                   AND exception_group.matter_id = %s
                """,
                (exception_group_id, actor.firm_id, matter_id),
            ).fetchone()
            if raw_group is None:
                raise KeyError(exception_group_id)
            group = self._validate_group(
                connection,
                row=dict(raw_group),
                actor=actor,
                matter_id=matter_id,
            )
            rows = connection.execute(
                """
                SELECT candidate.extraction_candidate_id, candidate.candidate_kind, candidate.confidence,
                       candidate.review_reason_codes,
                       candidate.candidate_payload,
                       jsonb_agg(jsonb_build_object(
                           'evidence_page_id', page.evidence_page_id,
                           'page_number', page.page_number
                       ) ORDER BY page.page_number, page.evidence_page_id)
                           AS source_pages
                  FROM case_agent_ledger_exception_group_members member
                  JOIN case_agent_ledger_extraction_candidates candidate
                    ON candidate.extraction_candidate_id =
                            member.extraction_candidate_id
                   AND candidate.extraction_batch_id = member.extraction_batch_id
                   AND candidate.firm_id = member.firm_id
                   AND candidate.matter_id = member.matter_id
                  JOIN case_agent_ledger_extraction_candidate_pages candidate_page
                    ON candidate_page.extraction_candidate_id =
                            candidate.extraction_candidate_id
                   AND candidate_page.firm_id = candidate.firm_id
                   AND candidate_page.matter_id = candidate.matter_id
                  JOIN evidence_pages page
                    ON page.evidence_page_id = candidate_page.evidence_page_id
                   AND page.firm_id = candidate_page.firm_id
                   AND page.matter_id = candidate_page.matter_id
                 WHERE member.exception_group_id = %s
                   AND member.firm_id = %s AND member.matter_id = %s
                 GROUP BY member.candidate_hash,
                          candidate.extraction_candidate_id,
                          candidate.candidate_kind, candidate.confidence,
                          candidate.review_reason_codes,
                          candidate.candidate_payload
                 ORDER BY member.candidate_hash
                 OFFSET %s LIMIT %s
                """,
                (exception_group_id, actor.firm_id, matter_id, offset, limit),
            ).fetchall()
            members = tuple(
                _project_browser_safe_member(
                    row=dict(row), sequence=offset + index,
                    expected_kind=group.candidate_kind,
                    expected_reasons=group.reason_codes,
                )
                for index, row in enumerate(rows, start=1)
            )
            if offset >= group.candidate_count and group.candidate_count > 0:
                raise LedgerExceptionReviewBlocked(
                    "exception member offset exceeds the complete group"
                )
            next_offset = offset + len(members)
            return LedgerExceptionGroupMemberPage(
                group_id=exception_group_id,
                total_count=group.candidate_count,
                offset=offset,
                next_offset=(
                    next_offset if next_offset < group.candidate_count else None
                ),
                members=members,
            )

    def read_batch_state(
        self,
        *,
        matter_id: str,
        actor: Actor,
        extraction_batch_id: str,
    ) -> LedgerExceptionBatchState:
        _validate_command_identity(
            matter_id=matter_id,
            actor=actor,
            idempotency_key=f"read-ledger-exception-state:{extraction_batch_id}",
        )
        _require_roles(actor, _READ_ROLES)
        _validate_uuid("extraction_batch_id", extraction_batch_id)
        with _TenantTransaction(
            self._dsn, actor.firm_id, read_only=True
        ) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES,
            )
            return self._read_batch_state(
                connection,
                actor=actor,
                matter_id=matter_id,
                extraction_batch_id=extraction_batch_id,
            )

    def decide_exception_group(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        expected_version: int,
        idempotency_key: str,
        exception_group_id: str,
        decision: LedgerExceptionDecision,
        reason: LedgerExceptionReason,
        reason_note: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        """Route the complete current group without accepting a member subset."""

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_roles(actor, _WRITE_ROLES)
        _require_positive_version(expected_version)
        _validate_uuid("server_session_id", server_session_id)
        _validate_uuid("exception_group_id", exception_group_id)
        if not isinstance(decision, LedgerExceptionDecision):
            raise LedgerExceptionReviewBlocked("exception decision is invalid")
        if not isinstance(reason, LedgerExceptionReason):
            raise LedgerExceptionReviewBlocked("exception reason is invalid")
        normalized_note = None if reason_note is None else reason_note.strip()
        if normalized_note == "":
            normalized_note = None
        if normalized_note is not None and (
            len(normalized_note) > 500
            or len(normalized_note.encode("utf-8")) > 2_000
        ):
            raise LedgerExceptionReviewBlocked("exception decision note is invalid")
        request_hash = exception_decision_request_hash(
            matter_id=matter_id,
            expected_version=expected_version,
            exception_group_id=exception_group_id,
            decision=decision,
            reason=reason,
            reason_note=normalized_note,
        )
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            try:
                result = connection.execute(
                    """
                    SELECT public.decide_case_agent_ledger_exception_group_from_web_session(
                        %s,%s,%s,%s,%s,%s,%s,%s,%s
                    ) AS receipt
                    """,
                    (
                        server_session_id,
                        matter_id,
                        exception_group_id,
                        expected_version,
                        idempotency_key,
                        decision.value,
                        reason.value,
                        normalized_note,
                        request_hash,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                # Only the four explicit, stable SQLSTATEs owned by the
                # 0048/0049 command boundary are deterministic. Generic
                # P0001, driver, connection and commit failures retain their
                # original type so Web keeps the result UNKNOWN and
                # reconciles it.
                if error.sqlstate == _EXCEPTION_VERSION_CONFLICT_SQLSTATE:
                    raise VersionConflict(
                        "exception group review version changed"
                    ) from error
                if error.sqlstate == _EXCEPTION_TERMINAL_INTENT_CONFLICT_SQLSTATE:
                    raise IdempotencyConflict(
                        "exception group was decided by another intent"
                    ) from error
                if error.sqlstate == _REEXTRACTION_SOURCE_WINDOW_EXCEEDED_SQLSTATE:
                    raise LedgerExceptionReextractionSourceWindowExceeded(
                        "re-extraction source window exceeds 64 pages"
                    ) from error
                if error.sqlstate == _REEXTRACTION_COHORT_CAPACITY_EXCEEDED_SQLSTATE:
                    raise LedgerExceptionReextractionCohortCapacityExceeded(
                        "matter has reached the active re-extraction cohort limit"
                    ) from error
                raise
            if result is None or not isinstance(result.get("receipt"), dict):
                raise LedgerExceptionReviewBlocked(
                    "session-bound exception decision returned no receipt"
                )
            return _parse_exception_decision_receipt(
                result["receipt"],
                matter_id=matter_id,
                exception_group_id=exception_group_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )

    @staticmethod
    def _validate_group(
        connection: Any,
        *,
        row: dict[str, Any],
        actor: Actor,
        matter_id: str,
    ) -> LedgerExceptionGroup:
        group_id = str(row["exception_group_id"])
        batch_id = str(row["extraction_batch_id"])
        _validate_uuid("exception_group_id", group_id)
        _validate_uuid("extraction_batch_id", batch_id)
        reason_codes = canonical_reason_codes(row["canonical_reason_codes"])
        source_policy = LedgerExceptionSourcePolicy(str(row["source_policy"]))
        risk_policy = LedgerExceptionRiskPolicy(str(row["risk_policy"]))
        if exception_source_policy(reason_codes) is not source_policy:
            raise LedgerExceptionReviewBlocked(
                "exception source policy differs from canonical reasons"
            )
        if exception_risk_policy(reason_codes) is not risk_policy:
            raise LedgerExceptionReviewBlocked(
                "exception risk policy differs from canonical reasons"
            )
        candidate_kind = str(row["candidate_kind"])
        if candidate_kind not in {"FACT", "TRANSACTION"}:
            raise LedgerExceptionReviewBlocked("exception candidate kind is invalid")
        if exception_group_key_hash(
            candidate_kind=candidate_kind,
            reason_codes=reason_codes,
            source_policy=source_policy,
            risk_policy=risk_policy,
        ) != str(row["group_key_hash"]):
            raise LedgerExceptionReviewBlocked("exception group key hash is invalid")
        members = connection.execute(
            """
            SELECT member.candidate_hash, candidate.candidate_kind,
                   candidate.review_reason_codes, candidate.review_lane,
                   candidate.eligible_for_bulk_promotion
              FROM case_agent_ledger_exception_group_members member
              JOIN case_agent_ledger_extraction_candidates candidate
                ON candidate.extraction_candidate_id =
                        member.extraction_candidate_id
               AND candidate.extraction_batch_id = member.extraction_batch_id
               AND candidate.firm_id = member.firm_id
               AND candidate.matter_id = member.matter_id
             WHERE member.exception_group_id = %s
               AND member.extraction_batch_id = %s
               AND member.firm_id = %s AND member.matter_id = %s
             ORDER BY member.candidate_hash
            """,
            (group_id, batch_id, actor.firm_id, matter_id),
        ).fetchall()
        candidate_hashes: list[str] = []
        for member in members:
            if (
                str(member["candidate_kind"]) != candidate_kind
                or canonical_reason_codes(member["review_reason_codes"])
                != reason_codes
                or member["review_lane"] != "EXCEPTION_REVIEW"
                or member["eligible_for_bulk_promotion"] is not False
            ):
                raise LedgerExceptionReviewBlocked(
                    "exception group member differs from its fixed policy"
                )
            candidate_hashes.append(str(member["candidate_hash"]))
        candidate_count = int(row["candidate_count"])
        if len(candidate_hashes) != candidate_count or not 1 <= candidate_count <= 500:
            raise LedgerExceptionReviewBlocked(
                "exception group member count is invalid"
            )
        if exception_candidate_set_hash(candidate_hashes) != str(
            row["candidate_set_hash"]
        ):
            raise LedgerExceptionReviewBlocked(
                "exception group candidate set hash is invalid"
            )
        raw_decision = row.get("decision")
        raw_reason = row.get("reason_code")
        decision = None if raw_decision is None else LedgerExceptionDecision(raw_decision)
        decision_reason = (
            None if raw_reason is None else LedgerExceptionReason(raw_reason)
        )
        if (decision is None) != (decision_reason is None):
            raise LedgerExceptionReviewBlocked(
                "exception group decision projection is incomplete"
            )
        return LedgerExceptionGroup(
            group_id=group_id,
            extraction_batch_id=batch_id,
            candidate_kind=candidate_kind,
            reason_codes=reason_codes,
            source_policy=source_policy,
            risk_policy=risk_policy,
            candidate_count=candidate_count,
            summary=exception_group_summary(
                candidate_kind=candidate_kind,
                candidate_count=candidate_count,
                source_policy=source_policy,
                risk_policy=risk_policy,
            ),
            allowed_decisions=allowed_exception_decisions(
                reason_codes=reason_codes,
                source_policy=source_policy,
                risk_policy=risk_policy,
            ),
            decision=decision,
            decision_reason=decision_reason,
        )

    @staticmethod
    def _read_batch_state(
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        extraction_batch_id: str,
    ) -> LedgerExceptionBatchState:
        row = connection.execute(
            """
            SELECT batch.run_id, batch.matter_id, matter.version,
                   status.low_risk_lane_status, status.batch_status,
                   status.exception_group_count,
                   status.decided_exception_group_count,
                   validate_case_agent_ledger_exception_group_integrity(
                       batch.extraction_batch_id, batch.firm_id, batch.matter_id
                   ) AS group_integrity
              FROM case_agent_ledger_extraction_batches batch
              JOIN matters matter
                ON matter.matter_id = batch.matter_id
               AND matter.firm_id = batch.firm_id
              CROSS JOIN LATERAL
                   case_agent_ledger_extraction_batch_review_status(
                       batch.extraction_batch_id, batch.firm_id, batch.matter_id
                   ) status
             WHERE batch.extraction_batch_id = %s AND batch.firm_id = %s
               AND batch.matter_id = %s
            """,
            (extraction_batch_id, actor.firm_id, matter_id),
        ).fetchone()
        if row is None:
            raise KeyError(extraction_batch_id)
        if row["group_integrity"] is not True:
            raise LedgerExceptionReviewBlocked(
                "exception batch does not bind the complete candidate lane"
            )
        return LedgerExceptionBatchState(
            extraction_batch_id=extraction_batch_id,
            run_id=str(row["run_id"]),
            matter_id=str(row["matter_id"]),
            matter_version=int(row["version"]),
            low_risk_lane_status=LedgerLowRiskLaneStatus(
                str(row["low_risk_lane_status"])
            ),
            batch_status=LedgerExtractionBatchReviewStatus(
                str(row["batch_status"])
            ),
            exception_group_count=int(row["exception_group_count"]),
            decided_exception_group_count=int(
                row["decided_exception_group_count"]
            ),
        )


def _parse_exception_decision_receipt(
    value: dict[str, Any],
    *,
    matter_id: str,
    exception_group_id: str,
    expected_version: int,
    idempotency_key: str,
) -> CaseLedgerCommandReceipt:
    required = {
        "command_name",
        "idempotency_key",
        "matter_id",
        "matter_version",
        "audit_event_id",
        "object_type",
        "object_id",
    }
    if set(value) != required:
        raise LedgerExceptionReviewBlocked(
            "session-bound exception decision receipt shape is invalid"
        )
    if (
        value["command_name"] != _COMMAND
        or value["idempotency_key"] != idempotency_key
        or str(value["matter_id"]) != matter_id
    ):
        raise LedgerExceptionReviewBlocked(
            "session-bound exception decision receipt binding is invalid"
        )
    audit_event_id = str(value["audit_event_id"])
    object_id = str(value["object_id"])
    _validate_uuid("audit_event_id", audit_event_id)
    _validate_uuid("receipt object_id", object_id)
    try:
        matter_version = int(value["matter_version"])
    except (TypeError, ValueError) as error:
        raise LedgerExceptionReviewBlocked(
            "session-bound exception decision version is invalid"
        ) from error
    object_type = str(value["object_type"])
    if object_type == "CASE_LEDGER_EXCEPTION_GROUP":
        if object_id != exception_group_id or matter_version != expected_version:
            raise LedgerExceptionReviewBlocked(
                "intermediate exception decision receipt is invalid"
            )
    elif object_type == "CASE_LEDGER_EXTRACTION_RUN_REVIEW":
        if matter_version != expected_version + 1:
            raise LedgerExceptionReviewBlocked(
                "exception-only final decision receipt is invalid"
            )
    else:
        raise LedgerExceptionReviewBlocked(
            "session-bound exception decision object type is invalid"
        )
    return CaseLedgerCommandReceipt(
        command_name=_COMMAND,
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=matter_version,
        audit_event_id=audit_event_id,
        object_type=object_type,
        object_id=object_id,
    )


def _project_browser_safe_member(
    *,
    row: dict[str, Any],
    sequence: int,
    expected_kind: str,
    expected_reasons: tuple[str, ...],
) -> LedgerExceptionGroupMember:
    kind = str(row.get("candidate_kind", ""))
    reasons = canonical_reason_codes(row.get("review_reason_codes", ()))
    payload = row.get("candidate_payload")
    source_pages = row.get("source_pages")
    if (
        kind != expected_kind
        or reasons != expected_reasons
        or not isinstance(payload, dict)
        or payload.get("kind") != kind
        or not isinstance(source_pages, list)
        or not source_pages
    ):
        raise LedgerExceptionReviewBlocked(
            "exception member projection differs from its immutable group"
        )
    confidence = float(row.get("confidence"))
    if not 0 <= confidence <= 1:
        raise LedgerExceptionReviewBlocked(
            "exception member confidence is invalid"
        )
    raw_excerpts = payload.get("supporting_excerpts")
    if not isinstance(raw_excerpts, list) or not raw_excerpts:
        raise LedgerExceptionReviewBlocked(
            "exception member has no reviewable source excerpt"
        )
    excerpt_by_page: dict[str, str] = {}
    for raw in raw_excerpts:
        if not isinstance(raw, dict):
            raise LedgerExceptionReviewBlocked(
                "exception member source excerpt is invalid"
            )
        page_id = str(raw.get("evidence_page_id", ""))
        _validate_uuid("evidence_page_id", page_id)
        text = raw.get("text")
        if (
            page_id in excerpt_by_page
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 2_000
        ):
            raise LedgerExceptionReviewBlocked(
                "exception member source excerpt is invalid"
            )
        excerpt_by_page[page_id] = text.strip()
    excerpts: list[LedgerExceptionMemberExcerpt] = []
    for raw_page in source_pages:
        if not isinstance(raw_page, dict):
            raise LedgerExceptionReviewBlocked(
                "exception member source page is invalid"
            )
        page_id = str(raw_page.get("evidence_page_id", ""))
        _validate_uuid("evidence_page_id", page_id)
        page_number = raw_page.get("page_number")
        if (
            type(page_number) is not int
            or page_number < 1
            or page_id not in excerpt_by_page
        ):
            raise LedgerExceptionReviewBlocked(
                "exception member source page is not fully reviewable"
            )
        excerpts.append(
            LedgerExceptionMemberExcerpt(
                evidence_page_id=page_id,
                page_number=page_number,
                text=excerpt_by_page[page_id],
            )
        )
    if len(excerpts) != len(excerpt_by_page):
        raise LedgerExceptionReviewBlocked(
            "exception member excerpts differ from their page links"
        )
    if kind == "FACT":
        summary = payload.get("fact_text")
        if not isinstance(summary, str) or not summary.strip():
            raise LedgerExceptionReviewBlocked(
                "exception fact summary is invalid"
            )
        summary = summary.strip()
    else:
        amount = payload.get("amount")
        currency = payload.get("currency")
        local_date = payload.get("local_date") or "日期待核对"
        direction = {
            "OUTGOING": "付款",
            "INCOMING": "收款",
            "UNKNOWN": "收付方向待核对",
        }.get(str(payload.get("direction")))
        if amount is None or not isinstance(currency, str) or direction is None:
            raise LedgerExceptionReviewBlocked(
                "exception transaction summary is invalid"
            )
        summary = f"{local_date} · {direction} {currency} {amount}"
    if len(summary) > 1_000:
        raise LedgerExceptionReviewBlocked(
            "exception member summary exceeds the review boundary"
        )
    return LedgerExceptionGroupMember(
        extraction_candidate_id=str(row["extraction_candidate_id"]),
        sequence=sequence,
        candidate_kind=kind,
        summary=summary,
        confidence=confidence,
        reason_codes=reasons,
        excerpts=tuple(excerpts),
    )


class _TenantTransaction:
    def __init__(self, dsn: str, firm_id: str, *, read_only: bool = False) -> None:
        self._dsn = dsn
        self._firm_id = firm_id
        self._read_only = read_only
        self._context: Any = None

    def __enter__(self):
        self._context = psycopg.connect(self._dsn, row_factory=dict_row)
        connection = self._context.__enter__()
        if self._read_only:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        connection.execute(
            "SELECT set_config('app.firm_id', %s, true)", (self._firm_id,)
        )
        return connection

    def __exit__(self, exc_type, exc, traceback):
        return self._context.__exit__(exc_type, exc, traceback)


def preflight_case_agent_ledger_exception_review_schema(*, dsn: str, firm_id: str) -> None:
    """Fail closed unless the complete 0047 schema and FORCE RLS are present."""

    _validate_uuid("firm_id", firm_id)
    required_tables = {
        "case_agent_ledger_exception_groups",
        "case_agent_ledger_exception_group_members",
        "case_agent_ledger_exception_group_decisions",
        "case_agent_ledger_exception_decision_events",
    }
    required_functions = {
        "case_agent_ledger_extraction_batch_review_status",
        "case_agent_ledger_extraction_current_review_version",
        "case_agent_ledger_extraction_run_staging_complete",
        "case_agent_ledger_extraction_run_review_resolved",
    }
    try:
        with _TenantTransaction(dsn, firm_id) as connection:
            tables = {
                str(row["relname"]): (
                    bool(row["relrowsecurity"]), bool(row["relforcerowsecurity"])
                )
                for row in connection.execute(
                    """
                    SELECT relation.relname, relation.relrowsecurity,
                           relation.relforcerowsecurity
                      FROM pg_catalog.pg_class relation
                      JOIN pg_catalog.pg_namespace namespace
                        ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public'
                       AND relation.relname = ANY(%s)
                    """,
                    (list(required_tables),),
                ).fetchall()
            }
            if set(tables) != required_tables or any(
                value != (True, True) for value in tables.values()
            ):
                raise LedgerExceptionReviewBlocked(
                    "ledger exception review requires all FORCE RLS tables"
                )
            functions = {
                str(row["proname"])
                for row in connection.execute(
                    """
                    SELECT procedure.proname
                      FROM pg_catalog.pg_proc procedure
                      JOIN pg_catalog.pg_namespace namespace
                        ON namespace.oid = procedure.pronamespace
                     WHERE namespace.nspname = 'public'
                       AND procedure.proname = ANY(%s)
                    """,
                    (list(required_functions),),
                ).fetchall()
            }
            if functions != required_functions:
                raise LedgerExceptionReviewBlocked(
                    "ledger exception review proof functions are incomplete"
                )
    except LedgerExceptionReviewBlocked:
        raise
    except Exception as error:
        raise LedgerExceptionReviewBlocked(
            "ledger exception review schema preflight failed"
        ) from error


__all__ = (
    "PostgresCaseLedgerExceptionReviewStore",
    "preflight_case_agent_ledger_exception_review_schema",
)
