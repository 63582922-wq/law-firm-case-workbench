"""PostgreSQL projection for lawyer review of staged ledger extraction.

The read path is deliberately separate from the immutable staging/promotion
store.  It reads only the fields required by the browser-safe application
service, authorises the human against the matter under forced RLS, and marks a
batch current only when the matter is still at its source version or the sole
``V -> V+1`` transition is the verified work-plan promotion from the same
Agent run.

Hashes, private object keys, provider metadata, prompts and internal task
references are neither selected nor returned.  The command path delegates to
the existing PostgreSQL promotion store, which re-reads and re-verifies the
complete eligible lane in its own transaction before writing confirmed ledger
records.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from case_kernel.case_ledger_postgres import _authorize_matter_read
from case_kernel.case_agent_ledger_exception_review_postgres import (
    PostgresCaseLedgerExceptionReviewStore,
)
from case_kernel.models import Actor, Role

from .web_agent_ledger_extraction_review import (
    CaseLedgerExtractionReviewBatchProjection,
    CaseLedgerExtractionReviewCandidateProjection,
    CaseLedgerExtractionReviewExcerptProjection,
    WebAgentLedgerExtractionReviewBlocked,
)


class WebAgentLedgerExtractionReviewPersistenceBlocked(
    WebAgentLedgerExtractionReviewBlocked
):
    """The durable review projection is absent, inconsistent or unavailable."""


def preflight_web_ledger_confirmation_session_authority(*, dsn: str) -> None:
    """Fail closed unless the complete 0048 Web authority boundary is live.

    This probe runs through the application DSN before routes are mounted.  It
    verifies effective privileges, not only migration names: the login role
    must be the Web application role, must not inherit the NOLOGIN definer,
    must be unable to mutate authoritative review tables directly, and must
    be able to execute only the narrow session-bound commands.  All catalog
    reads are non-mutating and deliberately independent of a case/firm GUC.
    """

    if (
        not isinstance(dsn, str)
        or not dsn
        or dsn != dsn.strip()
        or len(dsn) > _MAX_DSN_LENGTH
        or "\x00" in dsn
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "Web 台账复核数据库身份配置无效"
        )
    required_functions = {
        "authorize_case_agent_ledger_extraction_low_risk_confirmation": 6,
        "finalize_case_agent_ledger_extraction_low_risk_confirmation": 2,
        "decide_case_agent_ledger_exception_group_from_web_session": 9,
    }
    protected_tables = {
        "case_agent_ledger_extraction_session_approvals",
        "case_agent_ledger_extraction_batches",
        "case_agent_ledger_extraction_staging_events",
        "case_agent_ledger_extraction_candidates",
        "case_agent_ledger_extraction_candidate_pages",
        "case_agent_ledger_extraction_promotions",
        "case_agent_ledger_extraction_batch_confirmations",
        "case_agent_ledger_exception_groups",
        "case_agent_ledger_exception_group_members",
        "case_agent_ledger_exception_group_decisions",
        "case_agent_ledger_exception_decision_events",
    }
    force_rls_tables = protected_tables | {"web_sessions"}
    required_triggers = {
        "case_agent_ledger_extraction_session_approvals_append_only",
        "case_facts_session_bound_extraction_target_immutable",
        "case_transactions_session_bound_extraction_target_immutable",
    }
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            role = connection.execute(
                """
                SELECT current_user AS current_role,
                       owner.rolcanlogin AS owner_can_login,
                       owner.rolinherit AS owner_inherits,
                       owner.rolsuper AS owner_is_super,
                       owner.rolbypassrls AS owner_bypasses_rls,
                       pg_catalog.pg_has_role(
                           current_user, owner.oid, 'MEMBER'
                       ) AS application_is_owner_member
                  FROM pg_catalog.pg_roles owner
                 WHERE owner.rolname = 'lawcase_ledger_confirmation_owner'
                """
            ).fetchone()
            if not isinstance(role, Mapping) or (
                role.get("current_role") != "lawcase_web_application"
                or role.get("owner_can_login") is not False
                or role.get("owner_inherits") is not False
                or role.get("owner_is_super") is not False
                or role.get("owner_bypasses_rls") is not False
                or role.get("application_is_owner_member") is not False
            ):
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核数据库角色边界不完整"
                )

            functions = connection.execute(
                """
                SELECT procedure.proname, procedure.pronargs,
                       procedure.prosecdef,
                       owner.rolname AS owner_name,
                       procedure.proconfig,
                       pg_catalog.has_function_privilege(
                           current_user, procedure.oid, 'EXECUTE'
                       ) AS application_can_execute,
                       EXISTS (
                           SELECT 1
                             FROM pg_catalog.aclexplode(
                                 COALESCE(
                                     procedure.proacl,
                                     pg_catalog.acldefault(
                                         'f'::"char", procedure.proowner
                                     )
                                 )
                             ) acl
                            WHERE acl.grantee = 0
                              AND acl.privilege_type = 'EXECUTE'
                       ) AS public_can_execute
                  FROM pg_catalog.pg_proc procedure
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = procedure.pronamespace
                  JOIN pg_catalog.pg_roles owner
                    ON owner.oid = procedure.proowner
                 WHERE namespace.nspname = 'public'
                   AND procedure.proname = ANY(%s)
                """,
                (list(required_functions),),
            ).fetchall()
            function_rows: dict[str, Mapping[str, Any]] = {}
            for row in functions:
                if not isinstance(row, Mapping):
                    raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                        "Web 台账复核命令元数据无效"
                    )
                name = str(row.get("proname", ""))
                if name in function_rows:
                    raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                        "Web 台账复核命令签名重复"
                    )
                function_rows[name] = row
            if set(function_rows) != set(required_functions):
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核会话命令未完整安装"
                )
            for name, argument_count in required_functions.items():
                row = function_rows[name]
                config = row.get("proconfig")
                if (
                    int(row.get("pronargs", -1)) != argument_count
                    or row.get("prosecdef") is not True
                    or row.get("owner_name")
                    != "lawcase_ledger_confirmation_owner"
                    or not isinstance(config, (list, tuple))
                    or "search_path=pg_catalog" not in config
                    or row.get("application_can_execute") is not True
                    or row.get("public_can_execute") is not False
                ):
                    raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                        "Web 台账复核会话命令权限漂移"
                    )

            relations = connection.execute(
                """
                SELECT relation.relname, relation.relrowsecurity,
                       relation.relforcerowsecurity,
                       owner.rolname AS owner_name,
                       pg_catalog.has_table_privilege(
                           current_user, relation.oid, 'INSERT'
                       ) AS application_can_insert,
                       pg_catalog.has_table_privilege(
                           current_user, relation.oid, 'UPDATE'
                       ) AS application_can_update,
                       pg_catalog.has_table_privilege(
                           current_user, relation.oid, 'DELETE'
                       ) AS application_can_delete,
                       pg_catalog.has_table_privilege(
                           current_user, relation.oid, 'TRUNCATE'
                       ) AS application_can_truncate
                  FROM pg_catalog.pg_class relation
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN pg_catalog.pg_roles owner
                    ON owner.oid = relation.relowner
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = ANY(%s)
                """,
                (list(force_rls_tables),),
            ).fetchall()
            relation_rows = {
                str(row.get("relname", "")): row
                for row in relations
                if isinstance(row, Mapping)
            }
            if set(relation_rows) != force_rls_tables or any(
                row.get("relrowsecurity") is not True
                or row.get("relforcerowsecurity") is not True
                for row in relation_rows.values()
            ):
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核必须强制租户隔离"
                )
            if relation_rows[
                "case_agent_ledger_extraction_session_approvals"
            ].get("owner_name") != "lawcase_ledger_confirmation_owner":
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核会话授权表所有者漂移"
                )
            for table in protected_tables:
                row = relation_rows[table]
                if any(
                    row.get(field) is not False
                    for field in (
                        "application_can_insert",
                        "application_can_update",
                        "application_can_delete",
                        "application_can_truncate",
                    )
                ):
                    raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                        "Web 应用角色仍可绕过台账复核命令"
                    )

            owner_grants = connection.execute(
                """
                WITH required_table(table_name, privilege) AS (
                    VALUES
                        ('web_sessions', 'SELECT'),
                        ('users', 'SELECT'),
                        ('matters', 'SELECT'),
                        ('matter_actor_roles', 'SELECT'),
                        ('command_idempotency', 'SELECT'),
                        ('command_idempotency', 'INSERT'),
                        ('case_agent_ledger_extraction_session_approvals', 'SELECT'),
                        ('case_agent_ledger_extraction_session_approvals', 'INSERT'),
                        ('case_agent_ledger_extraction_batches', 'SELECT'),
                        ('case_agent_ledger_extraction_candidates', 'SELECT'),
                        ('case_agent_ledger_extraction_candidate_pages', 'SELECT'),
                        ('case_agent_ledger_extraction_promotions', 'SELECT'),
                        ('case_agent_ledger_extraction_promotions', 'INSERT'),
                        ('case_agent_ledger_extraction_batch_confirmations', 'SELECT'),
                        ('case_agent_ledger_extraction_batch_confirmations', 'INSERT'),
                        ('case_agent_runs', 'SELECT'),
                        ('case_agent_task_graphs', 'SELECT'),
                        ('case_agent_tasks', 'SELECT'),
                        ('case_agent_verification_attempts', 'SELECT'),
                        ('case_agent_verification_receipts', 'SELECT'),
                        ('case_agent_work_plan_promotions', 'SELECT'),
                        ('audit_events', 'SELECT'),
                        ('audit_events', 'INSERT'),
                        ('outbox_events', 'INSERT'),
                        ('evidence_pages', 'SELECT'),
                        ('evidence_original_files', 'SELECT'),
                        ('case_facts', 'SELECT'),
                        ('case_facts', 'INSERT'),
                        ('case_transactions', 'SELECT'),
                        ('case_transactions', 'INSERT'),
                        ('case_agent_ledger_exception_groups', 'SELECT'),
                        ('case_agent_ledger_exception_group_members', 'SELECT'),
                        ('case_agent_ledger_exception_group_decisions', 'SELECT'),
                        ('case_agent_ledger_exception_group_decisions', 'INSERT'),
                        ('case_agent_ledger_exception_decision_events', 'SELECT'),
                        ('case_agent_ledger_exception_decision_events', 'INSERT'),
                        ('case_agent_snapshot_refresh_requests', 'SELECT'),
                        ('case_agent_snapshot_refresh_requests', 'INSERT'),
                        ('case_agent_snapshot_refresh_requests', 'UPDATE'),
                        ('case_agent_run_inbox', 'SELECT'),
                        ('case_agent_run_inbox', 'UPDATE')
                ), required_column(table_name, column_name, privilege) AS (
                    VALUES
                        ('matters', 'version', 'UPDATE'),
                        ('matters', 'updated_at', 'UPDATE'),
                        ('web_sessions', 'session_id', 'UPDATE'),
                        ('users', 'user_id', 'UPDATE'),
                        ('matter_actor_roles', 'user_id', 'UPDATE'),
                        ('case_agent_runs', 'run_id', 'UPDATE')
                ), required_function(signature) AS (
                    VALUES
                        ('public.case_agent_ledger_extraction_current_review_version(uuid,uuid,uuid)'),
                        ('public.case_agent_ledger_extraction_batch_review_status(uuid,uuid,uuid)'),
                        ('public.case_agent_ledger_extraction_target_matches_candidate(uuid,uuid,uuid,uuid,text,uuid)'),
                        ('public.validate_case_agent_ledger_exception_group_integrity(uuid,uuid,uuid)'),
                        ('public.case_agent_ledger_extraction_run_review_resolved(uuid,uuid,uuid)'),
                        ('public.case_agent_ledger_extraction_run_staging_complete(uuid,uuid,uuid)')
                ), table_checks AS (
                    SELECT required_table.table_name,
                           required_table.privilege,
                           COALESCE(pg_catalog.has_table_privilege(
                               'lawcase_ledger_confirmation_owner',
                               relation.oid, required_table.privilege
                           ), false) AS permitted
                      FROM required_table
                      LEFT JOIN pg_catalog.pg_class relation
                        ON relation.relname = required_table.table_name
                       AND relation.relnamespace = 'public'::regnamespace
                ), column_checks AS (
                    SELECT required_column.table_name,
                           required_column.column_name,
                           COALESCE(pg_catalog.has_column_privilege(
                               'lawcase_ledger_confirmation_owner',
                               relation.oid, required_column.column_name,
                               required_column.privilege
                           ), false) AS permitted
                      FROM required_column
                      LEFT JOIN pg_catalog.pg_class relation
                        ON relation.relname = required_column.table_name
                       AND relation.relnamespace = 'public'::regnamespace
                ), function_checks AS (
                    SELECT signature,
                           COALESCE(pg_catalog.has_function_privilege(
                               'lawcase_ledger_confirmation_owner',
                               pg_catalog.to_regprocedure(signature), 'EXECUTE'
                           ), false) AS permitted
                      FROM required_function
                )
                SELECT COALESCE(
                           (SELECT bool_and(permitted) FROM table_checks),
                           false
                       ) AS owner_table_grants_complete,
                       COALESCE(
                           (SELECT bool_and(permitted) FROM column_checks),
                           false
                       ) AS owner_column_grants_complete,
                       COALESCE(
                           (SELECT bool_and(permitted) FROM function_checks),
                           false
                       ) AS owner_function_grants_complete
                """
            ).fetchone()
            if not isinstance(owner_grants, Mapping) or any(
                owner_grants.get(field) is not True
                for field in (
                    "owner_table_grants_complete",
                    "owner_column_grants_complete",
                    "owner_function_grants_complete",
                )
            ):
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核命令所有者授权不完整"
                )

            policies = connection.execute(
                """
                SELECT policy.polcmd,
                       role.rolname AS policy_role,
                       pg_catalog.pg_get_expr(
                           policy.polqual, policy.polrelid
                       ) AS using_expression
                  FROM pg_catalog.pg_policy policy
                  JOIN pg_catalog.pg_class relation
                    ON relation.oid = policy.polrelid
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN LATERAL pg_catalog.unnest(policy.polroles)
                    policy_role(role_oid) ON true
                  JOIN pg_catalog.pg_roles role
                    ON role.oid = policy_role.role_oid
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = 'web_sessions'
                   AND policy.polname =
                        'web_sessions_ledger_confirmation_exact_session'
                """
            ).fetchall()
            if len(policies) != 1 or not isinstance(policies[0], Mapping):
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核会话选择策略缺失"
                )
            policy = policies[0]
            if (
                policy.get("polcmd") != "r"
                or policy.get("policy_role")
                != "lawcase_ledger_confirmation_owner"
                or "app.web_session_id"
                not in str(policy.get("using_expression", ""))
            ):
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核会话选择策略漂移"
                )

            triggers = connection.execute(
                """
                SELECT trigger_row.tgname, trigger_row.tgenabled
                  FROM pg_catalog.pg_trigger trigger_row
                  JOIN pg_catalog.pg_class relation
                    ON relation.oid = trigger_row.tgrelid
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE namespace.nspname = 'public'
                   AND trigger_row.tgname = ANY(%s)
                   AND NOT trigger_row.tgisinternal
                """,
                (list(required_triggers),),
            ).fetchall()
            trigger_map = {
                str(row.get("tgname", "")): str(row.get("tgenabled", ""))
                for row in triggers
                if isinstance(row, Mapping)
            }
            if set(trigger_map) != required_triggers or any(
                state not in {"O", "A"} for state in trigger_map.values()
            ):
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "Web 台账复核不可变守卫未启用"
                )
    except WebAgentLedgerExtractionReviewPersistenceBlocked:
        raise
    except Exception as error:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "Web 台账复核数据库身份预检失败"
        ) from error


_HUMAN_READ_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)
_MAX_DSN_LENGTH = 4_096
_MAX_BATCHES = 100
_MAX_CANDIDATES_PER_BATCH = 500
_FACT_FIELDS = frozenset(
    {
        "candidate_hash",
        "kind",
        "source_refs",
        "evidence_page_ids",
        "confidence",
        "conflict_codes",
        "risk_codes",
        "supporting_excerpts",
        "fact_text",
    }
)
_TRANSACTION_FIELDS = frozenset(
    {
        "candidate_hash",
        "kind",
        "source_refs",
        "evidence_page_ids",
        "confidence",
        "conflict_codes",
        "risk_codes",
        "supporting_excerpts",
        "local_date",
        "date_precision",
        "amount",
        "currency",
        "direction",
        "payer_label",
        "payee_label",
        "channel",
        "transaction_reference",
    }
)
_DATE_PRECISION_LABELS = {
    "EXACT_DATE": None,
    "MONTH_ONLY": "月份待核对",
    "YEAR_ONLY": "年份待核对",
    "UNKNOWN": "日期待核对",
}
_DIRECTION_LABELS = {
    "OUTGOING": "付款",
    "INCOMING": "收款",
    "UNKNOWN": "收付方向待核对",
}
_CHANNEL_LABELS = {
    "WECHAT": "微信",
    "BANK": "银行",
    "CASH": "现金",
    "CHAT_RECORD": "聊天记录",
    "LOAN_INSTRUMENT": "借款凭证",
    "OTHER": "其他渠道",
}


class PostgresAgentLedgerExtractionReviewStore:
    """Read staged review batches and delegate one full-lane confirmation."""

    def __init__(
        self,
        dsn: str,
        *,
        confirmation_store: object | None = None,
        confirmation_store_factory: Callable[[Actor], object] | None = None,
        exception_review_store: object | None = None,
        configured_firm_ids: frozenset[str] | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if (
            not isinstance(dsn, str)
            or not dsn.strip()
            or dsn != dsn.strip()
            or len(dsn) > _MAX_DSN_LENGTH
            or "\x00" in dsn
        ):
            raise ValueError("Agent ledger review PostgreSQL DSN is invalid")
        if (confirmation_store is None) == (confirmation_store_factory is None):
            raise ValueError(
                "exactly one Agent ledger confirmation store boundary is required"
            )
        if confirmation_store is not None and not callable(
            getattr(confirmation_store, "confirm_low_risk_batch", None)
        ):
            raise ValueError("Agent ledger confirmation store is invalid")
        if confirmation_store_factory is not None and not callable(
            confirmation_store_factory
        ):
            raise ValueError("Agent ledger confirmation store factory is invalid")
        if confirmation_store_factory is not None:
            if (
                not isinstance(configured_firm_ids, frozenset)
                or not configured_firm_ids
            ):
                raise ValueError("Agent ledger confirmation firm mapping is required")
            configured_firms = frozenset(
                _uuid(value, "configured firm_id") for value in configured_firm_ids
            )
        elif configured_firm_ids is not None:
            raise ValueError(
                "fixed Agent ledger confirmation store cannot declare firm mapping"
            )
        else:
            configured_firms = None
        if connection_factory is not None and not callable(connection_factory):
            raise ValueError("Agent ledger review connection factory is invalid")
        exception_store = exception_review_store or PostgresCaseLedgerExceptionReviewStore(
            dsn
        )
        if not all(
            callable(getattr(exception_store, method, None))
            for method in (
                "list_exception_groups",
                "list_exception_group_members",
                "read_batch_state",
                "decide_exception_group",
            )
        ):
            raise ValueError("Agent ledger exception review store is invalid")
        self._dsn = dsn
        self._confirmation_store = confirmation_store
        self._confirmation_store_factory = confirmation_store_factory
        self._configured_firm_ids = configured_firms
        self._exception_review_store = exception_store
        self._connection_factory = connection_factory or self._open_connection

    def is_available_for_actor(self, *, actor: Actor) -> bool:
        try:
            _validate_actor(actor, write=False)
        except WebAgentLedgerExtractionReviewBlocked:
            return False
        return (
            True
            if self._configured_firm_ids is None
            else actor.firm_id in self._configured_firm_ids
        )

    def list_review_batches(
        self, *, matter_id: str, actor: Actor
    ) -> tuple[CaseLedgerExtractionReviewBatchProjection, ...]:
        matter = _uuid(matter_id, "matter_id")
        _validate_actor(actor, write=False)
        try:
            with self._read_transaction(actor.firm_id) as connection:
                _authorize_matter_read(
                    connection,
                    actor=actor,
                    matter_id=matter,
                    allowed_roles=_HUMAN_READ_ROLES,
                )
                batch_rows = connection.execute(
                    _BATCH_SQL,
                    (actor.firm_id, matter, _MAX_BATCHES + 1),
                ).fetchall()
                batches = _mapping_rows(batch_rows, "extraction batches")
                if len(batches) > _MAX_BATCHES:
                    raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                        "当前案件的材料提取批次过多，请联系管理员归档历史批次"
                    )
                if not batches:
                    return ()
                batch_ids = tuple(
                    _uuid(row.get("extraction_batch_id"), "extraction_batch_id")
                    for row in batches
                )
                if len(set(batch_ids)) != len(batch_ids):
                    raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                        "材料提取批次投影重复"
                    )
                candidate_rows = connection.execute(
                    _CANDIDATE_SQL,
                    (actor.firm_id, matter, list(batch_ids)),
                ).fetchall()
                candidates = _mapping_rows(candidate_rows, "extraction candidates")
        except WebAgentLedgerExtractionReviewBlocked:
            raise
        except (PermissionError, KeyError):
            raise
        except Exception:
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取批次暂时无法从案件数据库读取"
            ) from None

        by_batch: dict[str, list[CaseLedgerExtractionReviewCandidateProjection]] = {
            batch_id: [] for batch_id in batch_ids
        }
        for row in candidates:
            batch_id = _uuid(row.get("extraction_batch_id"), "candidate batch_id")
            if batch_id not in by_batch:
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "材料提取候选不属于当前批次投影"
                )
            by_batch[batch_id].append(_candidate_projection(row))
            if len(by_batch[batch_id]) > _MAX_CANDIDATES_PER_BATCH:
                raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                    "材料提取候选超过单批次安全上限"
                )
        return tuple(
            _batch_projection(row, candidates=tuple(by_batch[batch_id]))
            for row, batch_id in zip(batches, batch_ids, strict=True)
        )

    def confirm_low_risk_batch(self, **kwargs: Any) -> object:
        """Preserve the real OIDC/MFA lead actor at the command boundary."""

        actor = kwargs.get("actor")
        _validate_actor(actor, write=True)
        if not self.is_available_for_actor(actor=actor):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "当前律所尚未配置材料提取批次复核能力"
            )
        confirmation_store = (
            self._confirmation_store
            if self._confirmation_store is not None
            else self._confirmation_store_factory(actor)
        )
        if not callable(getattr(confirmation_store, "confirm_low_risk_batch", None)):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取批次确认服务未安全装配"
            )
        return confirmation_store.confirm_low_risk_batch(**kwargs)

    def list_exception_groups(self, **kwargs: Any) -> tuple[Any, ...]:
        actor = kwargs.get("actor")
        _validate_actor(actor, write=False)
        if not self.is_available_for_actor(actor=actor):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "当前律所尚未配置材料提取异常组复核能力"
            )
        result = self._exception_review_store.list_exception_groups(**kwargs)
        if not isinstance(result, tuple):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取异常组投影无效"
            )
        return result

    def list_exception_group_members(self, **kwargs: Any) -> object:
        actor = kwargs.get("actor")
        _validate_actor(actor, write=False)
        if not self.is_available_for_actor(actor=actor):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "当前律所尚未配置材料提取异常组复核能力"
            )
        return self._exception_review_store.list_exception_group_members(**kwargs)

    def read_exception_batch_state(self, **kwargs: Any) -> object:
        actor = kwargs.get("actor")
        _validate_actor(actor, write=False)
        if not self.is_available_for_actor(actor=actor):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "当前律所尚未配置材料提取异常组复核能力"
            )
        return self._exception_review_store.read_batch_state(**kwargs)

    def decide_exception_group(self, **kwargs: Any) -> object:
        actor = kwargs.get("actor")
        _validate_actor(actor, write=True)
        if not self.is_available_for_actor(actor=actor):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "当前律所尚未配置材料提取异常组复核能力"
            )
        return self._exception_review_store.decide_exception_group(**kwargs)

    def _open_connection(self):
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[Any]:
        with self._connection_factory() as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (firm_id,)
            )
            yield connection


def _batch_projection(
    row: Mapping[str, Any],
    *,
    candidates: tuple[CaseLedgerExtractionReviewCandidateProjection, ...],
) -> CaseLedgerExtractionReviewBatchProjection:
    batch_id = _uuid(row.get("extraction_batch_id"), "extraction_batch_id")
    source_version = _positive_int(row.get("source_matter_version"), "source version")
    staged_version = _positive_int(row.get("staged_matter_version"), "staged version")
    current_version = _positive_int(row.get("current_matter_version"), "current version")
    candidate_count = _bounded_count(row.get("candidate_count"), "candidate count")
    low_risk_count = _bounded_count(
        row.get("eligible_candidate_count"), "eligible candidate count"
    )
    if len(candidates) != candidate_count:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选数量与批次记录不一致"
        )
    projected_low_risk = sum(
        item.review_lane == "BULK_PROMOTION_ELIGIBLE" for item in candidates
    )
    if projected_low_risk != low_risk_count:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取低风险分组与批次记录不一致"
        )
    confirmed_version_raw = row.get("confirmed_matter_version")
    confirmed_version = (
        None
        if confirmed_version_raw is None
        else _positive_int(confirmed_version_raw, "confirmed version")
    )
    confirmed_at = _optional_datetime(row.get("confirmed_at"), "confirmed_at")
    if (confirmed_version is None) != (confirmed_at is None):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取批次确认记录不完整"
        )
    confirmation_count = row.get("confirmed_candidate_count")
    if confirmed_version is not None:
        if _bounded_count(confirmation_count, "confirmed candidate count") != low_risk_count:
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取批次确认数量不一致"
            )
    elif confirmation_count is not None:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取批次确认状态不一致"
        )
    run_status = str(row.get("run_status", ""))
    run_is_current = (
        run_status in {"READY_FOR_REVIEW", "COMPLETED"}
        and row.get("is_stale") is False
        and row.get("is_cancelled") is False
        and str(row.get("current_graph_id")) == str(row.get("graph_id"))
    )
    current_review_version_raw = row.get("current_review_version")
    current_review_version = (
        None
        if current_review_version_raw is None
        else _positive_int(current_review_version_raw, "current review version")
    )
    promotion_count = _bounded_count(row.get("promotion_count"), "promotion count")
    is_current = (
        confirmed_version is None
        and promotion_count == 0
        and staged_version == source_version
        and run_is_current
        and row.get("run_staging_complete") is True
        and current_review_version == current_version
    )
    exception_review_is_current = (
        run_is_current
        and row.get("run_staging_complete") is True
        and current_review_version == current_version
    )
    return CaseLedgerExtractionReviewBatchProjection(
        extraction_batch_id=batch_id,
        run_id=_uuid(row.get("run_id"), "run_id"),
        matter_id=_uuid(row.get("matter_id"), "matter_id"),
        current_matter_version=current_version,
        source_matter_version=source_version,
        candidate_count=candidate_count,
        low_risk_count=low_risk_count,
        exception_count=candidate_count - low_risk_count,
        staged_at=_datetime(row.get("created_at"), "created_at"),
        is_current=is_current,
        exception_review_is_current=exception_review_is_current,
        confirmed_matter_version=confirmed_version,
        confirmed_at=confirmed_at,
        candidates=candidates,
    )


def _candidate_projection(
    row: Mapping[str, Any],
) -> CaseLedgerExtractionReviewCandidateProjection:
    candidate_kind = str(row.get("candidate_kind", ""))
    if candidate_kind not in {"FACT", "TRANSACTION"}:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选类型无效"
        )
    review_lane = str(row.get("review_lane", ""))
    eligible = row.get("eligible_for_bulk_promotion")
    if review_lane == "BULK_PROMOTION_ELIGIBLE" and eligible is not True:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取低风险分组无效"
        )
    if review_lane == "EXCEPTION_REVIEW" and eligible is not False:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取例外分组无效"
        )
    if review_lane not in {"BULK_PROMOTION_ELIGIBLE", "EXCEPTION_REVIEW"}:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取复核分组无效"
        )
    if row.get("review_status") != "NEEDS_LAWYER_REVIEW":
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选复核状态无效"
        )
    reason_codes = _string_tuple(row.get("review_reason_codes"), "review reasons")
    if (
        review_lane == "BULK_PROMOTION_ELIGIBLE" and reason_codes
    ) or (
        review_lane == "EXCEPTION_REVIEW" and not reason_codes
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选复核原因与分组不一致"
        )
    payload = row.get("candidate_payload")
    if not isinstance(payload, Mapping):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选内容无效"
        )
    payload = dict(payload)
    expected_fields = _FACT_FIELDS if candidate_kind == "FACT" else _TRANSACTION_FIELDS
    if frozenset(payload) != expected_fields or payload.get("kind") != candidate_kind:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选内容结构无效"
        )
    candidate_hash = payload.get("candidate_hash")
    if (
        not isinstance(candidate_hash, str)
        or len(candidate_hash) != 64
        or any(character not in "0123456789abcdef" for character in candidate_hash)
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选绑定哈希无效"
        )
    confidence = _confidence(row.get("confidence"))
    if _confidence(payload.get("confidence")) != confidence:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取候选置信度不一致"
        )
    page_rows = row.get("source_pages")
    if not isinstance(page_rows, list) or not 1 <= len(page_rows) <= 20:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取来源页面无效"
        )
    pages: dict[str, int] = {}
    for value in page_rows:
        if not isinstance(value, Mapping):
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取来源页面无效"
            )
        page_id = _uuid(value.get("evidence_page_id"), "evidence_page_id")
        if page_id in pages:
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取来源页面重复"
            )
        pages[page_id] = _positive_int(value.get("page_number"), "page number")
    payload_page_ids = _uuid_list(payload.get("evidence_page_ids"), "evidence page ids")
    source_refs = _string_tuple(payload.get("source_refs"), "source refs")
    if (
        tuple(sorted(pages)) != payload_page_ids
        or source_refs != tuple(f"evidence-page:{value}" for value in payload_page_ids)
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取来源页面与候选内容不一致"
        )
    raw_excerpts = payload.get("supporting_excerpts")
    if not isinstance(raw_excerpts, list) or len(raw_excerpts) != len(pages):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取来源摘录不完整"
        )
    excerpts: list[CaseLedgerExtractionReviewExcerptProjection] = []
    seen_excerpt_pages: set[str] = set()
    for value in raw_excerpts:
        if not isinstance(value, Mapping) or frozenset(value) != {
            "evidence_page_id",
            "text",
        }:
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取来源摘录无效"
            )
        page_id = _uuid(value.get("evidence_page_id"), "excerpt page id")
        if page_id not in pages or page_id in seen_excerpt_pages:
            raise WebAgentLedgerExtractionReviewPersistenceBlocked(
                "材料提取来源摘录与页面不一致"
            )
        seen_excerpt_pages.add(page_id)
        excerpts.append(
            CaseLedgerExtractionReviewExcerptProjection(
                evidence_page_id=page_id,
                page_number=pages[page_id],
                text=_text(value.get("text"), "supporting excerpt", 2_000),
            )
        )
    if seen_excerpt_pages != set(pages):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取来源摘录未覆盖全部页面"
        )
    return CaseLedgerExtractionReviewCandidateProjection(
        extraction_candidate_id=_uuid(
            row.get("extraction_candidate_id"), "extraction_candidate_id"
        ),
        candidate_kind=candidate_kind,
        summary=(
            _fact_summary(payload)
            if candidate_kind == "FACT"
            else _transaction_summary(payload)
        ),
        confidence=confidence,
        review_lane=review_lane,
        review_reason_codes=reason_codes,
        excerpts=tuple(excerpts),
    )


def _fact_summary(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("fact_text"), "fact text", 2_000)


def _transaction_summary(payload: Mapping[str, Any]) -> str:
    precision = str(payload.get("date_precision", ""))
    if precision not in _DATE_PRECISION_LABELS:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取交易日期精度无效"
        )
    local_date = payload.get("local_date")
    if precision == "EXACT_DATE":
        date_label = _iso_date(local_date)
    elif local_date is None:
        date_label = str(_DATE_PRECISION_LABELS[precision])
    else:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取交易日期与精度不一致"
        )
    amount = _positive_decimal_text(payload.get("amount"))
    currency = _currency(payload.get("currency"))
    direction = str(payload.get("direction", ""))
    channel = str(payload.get("channel", ""))
    if direction not in _DIRECTION_LABELS or channel not in _CHANNEL_LABELS:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "材料提取交易方向或渠道无效"
        )
    payer = _optional_text(payload.get("payer_label"), "payer label", 500)
    payee = _optional_text(payload.get("payee_label"), "payee label", 500)
    parties = " → ".join(value for value in (payer, payee) if value)
    if not parties:
        parties = "收付款人待核对"
    parts = (
        date_label,
        f"{amount} {currency}",
        _DIRECTION_LABELS[direction],
        parties,
        _CHANNEL_LABELS[channel],
    )
    return _text(" · ".join(parts), "transaction summary", 2_000)


def _validate_actor(actor: object, *, write: bool) -> Actor:
    if not isinstance(actor, Actor):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked("律师身份无效")
    _uuid(actor.actor_id, "actor_id")
    _uuid(actor.firm_id, "firm_id")
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(
        _HUMAN_READ_ROLES
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "当前身份不能读取材料提取批次"
        )
    if write and Role.LEAD_LAWYER not in actor.roles:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "仅主办律师可以确认低风险候选组"
        )
    return actor


def _mapping_rows(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(row, Mapping) for row in value
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            f"{label} projection is invalid"
        )
    return list(value)


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            f"{label} is invalid"
        ) from None


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is invalid")
    return value


def _bounded_count(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_CANDIDATES_PER_BATCH:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is invalid")
    return value


def _datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is invalid")
    return value


def _optional_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is invalid")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is duplicated")
    return result


def _uuid_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is invalid")
    result = tuple(_uuid(item, label) for item in value)
    if result != tuple(sorted(set(result))):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is invalid")
    return result


def _confidence(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float, Decimal))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "extraction confidence is invalid"
        )
    return round(float(value), 5)


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(f"{label} is invalid")
    return value


def _optional_text(value: object, label: str, maximum: int) -> str | None:
    return None if value is None else _text(value, label, maximum)


def _positive_decimal_text(value: object) -> str:
    if not isinstance(value, str):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "transaction amount is invalid"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "transaction amount is invalid"
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "transaction amount is invalid"
        )
    return value


def _currency(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isalpha()
        or value != value.upper()
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "transaction currency is invalid"
        )
    return value


def _iso_date(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 10
        or value[4:5] != "-"
        or value[7:8] != "-"
    ):
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "transaction date is invalid"
        )
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise WebAgentLedgerExtractionReviewPersistenceBlocked(
            "transaction date is invalid"
        ) from None
    return value


_BATCH_SQL = """
    SELECT batch.extraction_batch_id, batch.run_id, batch.graph_id,
           batch.firm_id, batch.matter_id, batch.source_matter_version,
           batch.staged_matter_version, batch.candidate_count,
           batch.eligible_candidate_count, batch.created_at,
           matter.version AS current_matter_version,
           run.current_graph_id, run.status AS run_status,
           run.is_stale, run.is_cancelled,
           confirmation.confirmed_candidate_count,
           confirmation.confirmed_matter_version,
           confirmation.confirmed_at,
           case_agent_ledger_extraction_current_review_version(
               batch.extraction_batch_id, batch.firm_id, batch.matter_id
           ) AS current_review_version,
           case_agent_ledger_extraction_run_staging_complete(
               batch.run_id, batch.firm_id, batch.matter_id
           ) AS run_staging_complete,
           (
               SELECT count(*)::integer
               FROM case_agent_ledger_extraction_promotions promotion
               WHERE promotion.extraction_batch_id = batch.extraction_batch_id
                 AND promotion.firm_id = batch.firm_id
                 AND promotion.matter_id = batch.matter_id
           ) AS promotion_count
    FROM case_agent_ledger_extraction_batches batch
    JOIN matters matter
      ON matter.matter_id = batch.matter_id
     AND matter.firm_id = batch.firm_id
    JOIN case_agent_runs run
      ON run.run_id = batch.run_id
     AND run.firm_id = batch.firm_id
     AND run.matter_id = batch.matter_id
    LEFT JOIN case_agent_ledger_extraction_batch_confirmations confirmation
      ON confirmation.extraction_batch_id = batch.extraction_batch_id
     AND confirmation.firm_id = batch.firm_id
     AND confirmation.matter_id = batch.matter_id
    WHERE batch.firm_id = %s AND batch.matter_id = %s
    ORDER BY batch.created_at DESC, batch.extraction_batch_id DESC
    LIMIT %s
"""


_CANDIDATE_SQL = """
    SELECT candidate.extraction_batch_id,
           candidate.extraction_candidate_id,
           candidate.candidate_kind,
           candidate.confidence::double precision AS confidence,
           candidate.review_lane,
           candidate.eligible_for_bulk_promotion,
           candidate.review_reason_codes,
           candidate.review_status,
           candidate.candidate_payload,
           jsonb_agg(
               jsonb_build_object(
                   'evidence_page_id', page.evidence_page_id::text,
                   'page_number', page.page_number
               ) ORDER BY page.evidence_page_id
           ) AS source_pages
    FROM case_agent_ledger_extraction_candidates candidate
    JOIN case_agent_ledger_extraction_candidate_pages candidate_page
      ON candidate_page.extraction_candidate_id = candidate.extraction_candidate_id
     AND candidate_page.firm_id = candidate.firm_id
     AND candidate_page.matter_id = candidate.matter_id
    JOIN evidence_pages page
      ON page.evidence_page_id = candidate_page.evidence_page_id
     AND page.firm_id = candidate_page.firm_id
     AND page.matter_id = candidate_page.matter_id
    WHERE candidate.firm_id = %s
      AND candidate.matter_id = %s
      AND candidate.extraction_batch_id = ANY(%s::uuid[])
    GROUP BY candidate.extraction_batch_id,
             candidate.extraction_candidate_id,
             candidate.candidate_kind,
             candidate.confidence,
             candidate.review_lane,
             candidate.eligible_for_bulk_promotion,
             candidate.review_reason_codes,
             candidate.review_status,
             candidate.candidate_payload,
             candidate.created_at
    ORDER BY candidate.extraction_batch_id,
             candidate.created_at,
             candidate.extraction_candidate_id
"""


__all__ = (
    "PostgresAgentLedgerExtractionReviewStore",
    "WebAgentLedgerExtractionReviewPersistenceBlocked",
    "preflight_web_ledger_confirmation_session_authority",
)
