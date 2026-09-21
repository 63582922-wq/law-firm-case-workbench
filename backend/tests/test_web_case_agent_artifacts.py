from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import case_api.web_case_agent_artifacts as review_module
from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_case_agent_artifacts import (
    PostgresWebCaseAgentArtifactReviewService,
    WebCaseAgentArtifactReview,
    WebCaseAgentArtifactReviewBlocked,
)
from case_kernel.case_agent_supervisor import ArtifactReceipt
from case_kernel.case_agent_sealed_response_recovery import (
    SEALED_RESPONSE_RECOVERY_POLICY_HASH,
)
from case_kernel.case_agent_verifier import (
    ArtifactFormatReceipt,
    ArtifactLineageReceipt,
    ManagedArtifactRead,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _Cursor:
    def __init__(self, row=None) -> None:
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        if self._row is None:
            return []
        return self._row if isinstance(self._row, list) else [self._row]


class _Connection:
    def __init__(self, row) -> None:
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, parameters=None):
        self.calls.append((" ".join(sql.split()), parameters))
        if "FROM case_agent_review_candidates" in sql:
            return _Cursor(self.row)
        return _Cursor()


class _ObjectStore:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls = []

    def read_case_agent_review_candidate(self, stored, *, artifact_id):
        self.calls.append((stored, artifact_id))
        return self.content


class WebCaseAgentArtifactReviewTests(unittest.TestCase):
    def test_lawyer_topics_keep_uploaded_source_text_out_of_worklist_titles(self):
        evidence_sentence = (
            "原告证据目录载明：借条照片、银行流水及微信记录证明借款交付、"
            "利息约定及双方资金往来。"
        )
        self.assertEqual(
            review_module._lawyer_topic_title(evidence_sentence),
            "证据目录所列材料的真实性、关联性与证明力",
        )
        self.assertEqual(
            review_module._opponent_position_title(
                f"围绕“{evidence_sentence}”可能提出与本方相反的事实或法律解释"
            ),
            "对方可能否认或重新解释：证据目录所列材料的真实性、关联性与证明力",
        )
        self.assertEqual(
            review_module._decision_title(
                f"律师如何处理“{evidence_sentence}”", has_formal_issues=False
            ),
            "请律师决定：是否把“证据目录所列材料的真实性、关联性与证明力”纳入正式争点整理",
        )
        self.assertEqual(
            review_module._action_title(
                {
                    "action": f"围绕“{evidence_sentence}”完成证据对应、相反材料核对和律师取舍记录",
                    "blocked_by": [],
                }
            ),
            "围绕“证据目录所列材料的真实性、关联性与证明力”完成证据核对与取舍记录",
        )

    def test_extraction_view_keeps_unconfirmed_sources_and_duplicate_records(self):
        import test_case_agent_ledger_extraction as fixture_module
        from case_kernel.case_agent_ledger_extraction import (CaseLedgerExtractionCandidate,
            ExtractionCandidateKind, ExtractionSupportingExcerpt, ExtractionDatePrecision,
            ExtractionTransactionDirection, ExtractionTransactionChannel, build_case_ledger_extraction_candidate)
        fixture = fixture_module.CaseLedgerExtractionTests()
        fixture.setUp()
        transaction = CaseLedgerExtractionCandidate(kind=ExtractionCandidateKind.TRANSACTION,
            source_refs=(fixture.page.input_ref,), evidence_page_ids=(fixture.page_id,),
            confidence=0.95, conflict_codes=(), risk_codes=(),
            supporting_excerpts=(ExtractionSupportingExcerpt(fixture.page_id, "2020年1月1日转账100元"),),
            local_date="2020-01-01", date_precision=ExtractionDatePrecision.EXACT_DATE,
            amount="100.00", currency="CNY", direction=ExtractionTransactionDirection.OUTGOING,
            payer_label="甲", payee_label="乙", channel=ExtractionTransactionChannel.WECHAT,
            transaction_reference="A")
        raw, _ = build_case_ledger_extraction_candidate(task_input_hash="a" * 64,
            source_pages=(fixture.page,), candidates=(fixture._fact(), transaction,
                replace(transaction, transaction_reference="B"), replace(transaction, currency="HKD")))
        payload = json.loads(raw)
        review = review_module._project_payload(artifact_id=self.artifact_id,
            artifact_kind="CASE_LEDGER_EXTRACTION_CANDIDATE", payload=payload)
        self.assertEqual([len(section.items) for section in review.sections], [1, 3])
        self.assertEqual(sum(item.badge == "可能重复，待核对" for item in review.sections[1].items), 2)
        self.assertTrue(all(item.sources[0].source_id == fixture.page_id
            for section in review.sections for item in section.items))
        self.assertEqual(json.loads(raw), payload)
        payload["candidates"][0]["confidence"] = 1.0
        with self.assertRaises(WebCaseAgentArtifactReviewBlocked):
            review_module._project_payload(artifact_id=self.artifact_id,
                artifact_kind="CASE_LEDGER_EXTRACTION_CANDIDATE", payload=payload)

    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.firm_id = _id()
        self.actor_id = _id()
        self.matter_id = _id()
        self.run_id = _id()
        self.task_id = _id()
        self.page_id = _id()
        self.artifact_id = _id()
        self.input_hash = sha256(b"task input").hexdigest()
        self.payload = _canonical(
            {
                "court_ready": False,
                "evidence_decision": False,
                "formal_fact": False,
                "formal_transaction": False,
                "headline": "本案当前争议与缺口核对",
                "legal_conclusion": False,
                "open_questions": [
                    {
                        "question_id": "ctx-question-0123456789abcdef01234567",
                        "question": "请确认该笔付款应当抵扣本金还是利息。",
                        "source_refs": [f"evidence-page:{self.page_id}"],
                    }
                ],
                "review_status": "NEEDS_LAWYER_REVIEW",
                "schema_version": "agent-case-context-review-candidate-v1",
                "sections": [
                    {
                        "items": [
                            {
                                "confidence": 0.93,
                                "detail": "一项付款记录与原告诉请金额之间仍需核对。",
                                "item_id": "transaction-gap-1",
                                "review_reason": "涉及本金抵扣",
                                "source_refs": [f"evidence-page:{self.page_id}"],
                                "title": "付款记录需要核对",
                            }
                        ],
                        "section_id": "gaps",
                        "severity": "HIGH",
                        "title": "待补充与待确认",
                    }
                ],
                "source_hash": sha256(b"source").hexdigest(),
                "summary_counts": {"high": 1},
                "task_input_hash": self.input_hash,
            }
        )
        content_hash = sha256(self.payload).hexdigest()
        artifact = ArtifactReceipt(
            artifact_id=self.artifact_id,
            artifact_kind="CASE_CONTEXT_REVIEW_CANDIDATE",
            content_hash=content_hash,
            byte_size=len(self.payload),
            source_input_hash=self.input_hash,
            managed_derivative=False,
        )
        managed = ManagedArtifactRead(
            artifact_id=self.artifact_id,
            artifact_kind=artifact.artifact_kind,
            content=b"",
            source_input_hash=self.input_hash,
            object_receipt_hash=sha256(b"object receipt").hexdigest(),
            media_type="application/json",
        )
        format_receipt = ArtifactFormatReceipt(
            artifact_kind=artifact.artifact_kind,
            format_verifier_id="case-context-format",
            format_verifier_version="1.0.0",
            observed_content_hash=content_hash,
            format_verification_hash=sha256(b"format receipt").hexdigest(),
        )
        lineage = ArtifactLineageReceipt.build(
            artifact=artifact,
            task_id=self.task_id,
            managed=managed,
            format_receipt=format_receipt,
        )
        self.row = {
            "artifact_id": self.artifact_id,
            "artifact_kind": artifact.artifact_kind,
            "content_sha256": content_hash,
            "byte_size": len(self.payload),
            "task_input_hash": self.input_hash,
            "review_status": "NEEDS_LAWYER_REVIEW",
            "source_object_key": "private/object/key",
            "source_object_version_id": "opaque-version",
            "artifact_lineage": [asdict(lineage)],
        }
        self.identity = ServerIdentityContext(
            actor=Actor(
                self.actor_id,
                self.firm_id,
                frozenset({Role.LEAD_LAWYER}),
            ),
            session_id=_id(),
            issuer="https://identity.lawfirm.test",
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=self.now - timedelta(minutes=5),
            expires_at=self.now + timedelta(hours=1),
        )

    def _service(self, row=None):
        connection = _Connection(self.row if row is None else row)
        store = _ObjectStore(self.payload)
        service = PostgresWebCaseAgentArtifactReviewService(
            dsn="postgresql://not-used.invalid/test",
            object_store=store,
            connection_factory=lambda *_args, **_kwargs: connection,
        )
        return service, connection, store

    def test_context_view_preserves_producer_title_limit(self) -> None:
        from case_api.web_case_agent_artifacts import _case_context_sections

        payload = json.loads(self.payload)
        title = "案" * 500
        payload["sections"][0]["items"][0]["title"] = title
        _, sections = _case_context_sections(payload)
        self.assertEqual(sections[-1].items[0].title, title)
        self.assertEqual(len(sections[-1].items[0].sources), 1)
        payload["sections"][0]["items"][0]["title"] = title + "案"
        with self.assertRaises(WebCaseAgentArtifactReviewBlocked):
            _case_context_sections(payload)

    def test_verified_current_artifact_becomes_a_source_linked_lawyer_view(self) -> None:
        service, connection, store = self._service()
        review = service.read_review(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=self.run_id,
            artifact_id=self.artifact_id,
        )
        self.assertEqual(review.title, "案件台账核对（非整案分析）")
        self.assertIn("1 个不同来源对象", review.review_notice)
        self.assertIn("待确认项为零不代表案件无风险", review.review_notice)
        self.assertEqual(review.sections[0].severity, "HIGH")
        self.assertEqual(review.sections[0].title, "待律师决定")
        self.assertEqual(
            review.sections[0].items[0].detail,
            "请确认该笔付款应当抵扣本金还是利息。",
        )
        self.assertEqual(
            review.sections[1].items[0].sources[0].evidence_page_id,
            self.page_id,
        )
        self.assertEqual(len(store.calls), 1)
        sql = next(sql for sql, _ in connection.calls if "FROM case_agent_review_candidates" in sql)
        self.assertIn("receipt.outcome = 'PASSED'", sql)
        self.assertIn("run.current_graph_id = candidate.graph_id", sql)
        self.assertIn("role.revoked_at IS NULL", sql)

    def test_review_obligation_source_is_not_presented_as_confirmed_fact(self) -> None:
        source_id = _id()
        source = review_module._source(f"review-obligation:{source_id}")
        self.assertEqual(source.source_kind, "review-obligation")
        self.assertEqual(source.source_id, source_id)
        self.assertEqual(source.label, "待核事项（非确认事实）")
        self.assertIsNone(source.evidence_page_id)

    def test_posture_only_coverage_is_derived_without_mutating_candidate(self) -> None:
        payload = json.loads(self.payload)
        payload["open_questions"] = []
        source = f"posture-profile:{_id()}"
        payload["sections"][0]["items"][0]["source_refs"] = [source]
        payload["summary_counts"] = {"total_sources": 999, "open_questions": 0}
        before = _canonical(payload)
        review = review_module._project_payload(artifact_id=self.artifact_id,
            artifact_kind="CASE_CONTEXT_REVIEW_CANDIDATE", payload=payload)
        self.assertIn("本次仅覆盖代理档案", review.review_notice)
        self.assertIn("1 个不同来源对象", review.review_notice)
        self.assertNotIn("999", review.review_notice)
        self.assertLessEqual(len(review.review_notice), 1000)
        self.assertEqual(_canonical(payload), before)
        self.assertEqual(review.sections[0].items[0].sources[0].source_id, source.split(":")[1])

    def test_sealed_recovery_lineage_is_review_only_and_requires_the_fixed_policy(self) -> None:
        recovery_row = {
            "artifact_id": self.artifact_id,
            "artifact_kind": "LAWYER_DECISION_PACKAGE_CANDIDATE",
            "content_sha256": sha256(self.payload).hexdigest(),
            "byte_size": len(self.payload),
            "task_input_hash": self.input_hash,
            "candidate_content_sha256": sha256(self.payload).hexdigest(),
            "recovery_kind": "SEALED_RESPONSE_REPARSE",
            "recovery_policy_hash": SEALED_RESPONSE_RECOVERY_POLICY_HASH,
            "source_run_event_version": 8,
        }
        lineage = review_module._authorised_lineage(
            recovery_row, artifact_id=self.artifact_id
        )
        self.assertTrue(lineage.recovery_review_only)
        self.assertEqual(lineage.artifact_kind, "LAWYER_DECISION_PACKAGE_CANDIDATE")

        invalid = dict(recovery_row)
        invalid["recovery_policy_hash"] = sha256(b"other-policy").hexdigest()
        with self.assertRaisesRegex(WebCaseAgentArtifactReviewBlocked, "恢复链"):
            review_module._authorised_lineage(invalid, artifact_id=self.artifact_id)

    def test_approved_legal_rule_reference_is_a_viewable_source(self) -> None:
        source = review_module._source(f"legal-rule:{_id()}")
        self.assertEqual(source.source_kind, "legal-rule")
        self.assertEqual(source.label, "已核准法律规则")

    def test_listed_sealed_recovery_requires_the_original_failed_boundary(self) -> None:
        recovery_row = {
            # psycopg returns PostgreSQL UUID columns as UUID objects in the
            # live runtime, rather than as test-double strings.
            "artifact_id": UUID(self.artifact_id),
            "artifact_kind": "LAWYER_DECISION_PACKAGE_CANDIDATE",
        }
        service, connection, _ = self._service(row=recovery_row)
        items = service.list_sealed_recovery_artifacts(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=self.run_id,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "封存模型响应恢复 · 律师决策包候选")
        sql = next(
            sql
            for sql, _ in connection.calls
            if "case_agent_sealed_response_recovery_candidates" in sql
        )
        self.assertIn("run.status = 'WAITING_INPUT'", sql)
        self.assertIn("run.current_event_version = recovery.source_run_event_version", sql)
        self.assertIn("head.status = 'FAILED'", sql)
        self.assertIn("receipt.error_code = 'LAWYER_ANALYSIS_OUTPUT_REJECTED'", sql)
        self.assertIn("COUNT(*)", sql)
        self.assertNotIn("JOIN case_agent_artifacts artifact", sql)

    def test_sealed_recovery_reads_with_a_clear_non_promoting_notice(self) -> None:
        payload = _canonical(
            {
                "court_ready": False,
                "evidence_decision": False,
                "formal_fact": False,
                "formal_transaction": False,
                "legal_conclusion": False,
                "review_status": "NEEDS_LAWYER_REVIEW",
            }
        )
        recovery_row = {
            "artifact_id": self.artifact_id,
            "artifact_kind": "LAWYER_DECISION_PACKAGE_CANDIDATE",
            "content_sha256": sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "task_input_hash": self.input_hash,
            "review_status": "NEEDS_LAWYER_REVIEW",
            "source_object_key": "private/object/key",
            "source_object_version_id": "opaque-version",
            "recovery_kind": "SEALED_RESPONSE_REPARSE",
            "candidate_content_sha256": sha256(payload).hexdigest(),
            "recovery_policy_hash": SEALED_RESPONSE_RECOVERY_POLICY_HASH,
            "source_run_event_version": 8,
        }

        class RecoveryConnection(_Connection):
            def execute(self, sql, parameters=None):
                self.calls.append((" ".join(sql.split()), parameters))
                if "JOIN case_agent_artifacts artifact" in sql:
                    return _Cursor(None)
                if "case_agent_sealed_response_recovery_candidates" in sql:
                    return _Cursor(recovery_row)
                return _Cursor()

        connection = RecoveryConnection(recovery_row)
        store = _ObjectStore(payload)
        service = PostgresWebCaseAgentArtifactReviewService(
            dsn="postgresql://not-used.invalid/test",
            object_store=store,
            connection_factory=lambda *_args, **_kwargs: connection,
        )
        projected = WebCaseAgentArtifactReview(
            artifact_id=self.artifact_id,
            artifact_type="LAWYER_DECISION_PACKAGE_CANDIDATE",
            title="律师决策包候选",
            review_notice="ordinary notice",
            sections=(),
        )
        with patch.object(review_module, "_project_payload", return_value=projected):
            review = service.read_review(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.artifact_id,
            )
        self.assertIn("历史模型响应", review.review_notice)
        self.assertIn("原 Agent 任务仍处于受控阻断状态", review.review_notice)
        self.assertEqual(len(store.calls), 1)

    def test_confirmed_fact_source_is_bound_to_its_authoritative_evidence_page(self) -> None:
        fact_id = _id()
        payload = json.loads(self.payload.decode("utf-8"))
        payload["open_questions"][0]["source_refs"] = [f"fact:{fact_id}"]
        payload["sections"][0]["items"][0]["source_refs"] = [f"fact:{fact_id}"]
        review = review_module._project_payload(
            artifact_id=self.artifact_id,
            artifact_kind="CASE_CONTEXT_REVIEW_CANDIDATE",
            payload=payload,
        )

        bound = review_module._bind_structured_sources_to_evidence_pages(
            review=review,
            source_page_map={("fact", fact_id): self.page_id},
        )

        self.assertEqual(
            bound.sections[0].items[0].sources[0].evidence_page_id,
            self.page_id,
        )
        self.assertEqual(
            bound.sections[0].items[0].sources[0].source_id,
            fact_id,
        )

    def test_direct_case_object_without_a_live_evidence_page_is_not_shown(self) -> None:
        fact_id = _id()
        payload = json.loads(self.payload.decode("utf-8"))
        payload["open_questions"][0]["source_refs"] = [f"fact:{fact_id}"]
        payload["sections"][0]["items"][0]["source_refs"] = [f"fact:{fact_id}"]
        review = review_module._project_payload(
            artifact_id=self.artifact_id,
            artifact_kind="CASE_CONTEXT_REVIEW_CANDIDATE",
            payload=payload,
        )

        with self.assertRaisesRegex(
            WebCaseAgentArtifactReviewBlocked,
            "无法回到本案原始证据页",
        ):
            review_module._bind_structured_sources_to_evidence_pages(
                review=review,
                source_page_map={},
            )

    def test_missing_or_unverified_artifact_is_not_read_from_object_storage(self) -> None:
        service, _, store = self._service(row={})
        # The fake connection uses an explicit sentinel for no row.
        service._connect = lambda *_args, **_kwargs: _Connection(None)
        with self.assertRaisesRegex(WebCaseAgentArtifactReviewBlocked, "不存在"):
            service.read_review(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.artifact_id,
            )
        self.assertEqual(store.calls, [])

    def test_tampered_lineage_is_rejected_before_private_object_read(self) -> None:
        tampered = dict(self.row)
        tampered_lineage = [dict(self.row["artifact_lineage"][0])]
        tampered_lineage[0]["content_hash"] = sha256(b"other").hexdigest()
        tampered["artifact_lineage"] = tampered_lineage
        service, _, store = self._service(row=tampered)
        with self.assertRaisesRegex(WebCaseAgentArtifactReviewBlocked, "复核记录"):
            service.read_review(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.artifact_id,
            )
        self.assertEqual(store.calls, [])

    def test_non_mfa_identity_is_rejected_before_database_access(self) -> None:
        service, connection, store = self._service()
        identity = ServerIdentityContext(
            **{
                **self.identity.__dict__,
                "authentication_method": AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
            }
        )
        with self.assertRaisesRegex(WebCaseAgentArtifactReviewBlocked, "MFA"):
            service.read_review(
                identity=identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.artifact_id,
            )
        self.assertEqual(connection.calls, [])
        self.assertEqual(store.calls, [])

    def test_lawyer_decision_package_projects_decisions_actions_and_call_receipt(self) -> None:
        issue_ref = f"issue:{_id()}"
        fact_ref = f"fact:{_id()}"
        payload = {
            "source_catalog": [
                {"source_ref": issue_ref, "title": "货款余额及违约责任"},
                {"source_ref": fact_ref, "title": "已确认付款事实"},
            ],
            "executive_assessment": {
                "case_posture": "现有材料支持继续核对未付款项，但仍缺少完整履行材料。",
                "working_direction": "先补齐交付与催告证据，再由律师选择诉前推进路径。",
                "top_risk_issue_refs": [issue_ref],
            },
            "issues": [
                {
                    "issue_ref": issue_ref,
                    "title": "货款余额及违约责任",
                    "priority": "HIGH",
                    "evidence_status": "PARTIAL",
                    "assessment": "现有证据部分支持。有利点：已有合同与付款。",
                    "missing_evidence": ["完整交付凭证"],
                    "supporting_source_refs": [issue_ref, fact_ref],
                    "adverse_source_refs": [],
                    "authority_refs": [],
                }
            ],
            "adversarial_analysis": [
                {
                    "opponent_position": "对方可能主张货物未完整交付",
                    "position_status": "FORESEEABLE_NOT_ASSERTED",
                    "why_it_may_work": "现有材料未覆盖全部交付过程。",
                    "rebuttal_route": "补充签收、物流和对账材料。",
                    "residual_risk": "补证失败时证明力仍不足。",
                    "source_refs": [issue_ref, fact_ref],
                    "authority_refs": [],
                }
            ],
            "strategy_options": [
                {
                    "strategy_id": "EVIDENCE_FIRST",
                    "title": "证据优先路径",
                    "objective": "先补齐关键证据再推进。",
                    "conditions": ["能够取得交付材料"],
                    "execution_risks": ["补证耗时"],
                    "tradeoff_note": "稳健但推进较慢。",
                    "issue_refs": [issue_ref],
                },
                {
                    "strategy_id": "CONTROLLED_ADVANCE",
                    "title": "受控推进路径",
                    "objective": "保留缺口提示并同步推进。",
                    "conditions": ["律师接受当前证明风险"],
                    "execution_risks": ["对方抗辩空间较大"],
                    "tradeoff_note": "速度更快但风险更高。",
                    "issue_refs": [issue_ref],
                },
            ],
            "client_questions": [
                {
                    "question_id": "QUESTION-01",
                    "question": "请补充完整交付凭证。",
                    "why_it_matters": "关系到未付款抗辩。",
                    "source_refs": [issue_ref],
                }
            ],
            "action_plan": [
                {
                    "action_id": "ACTION-01",
                    "action": "核对交付、付款和催告证据。",
                    "reason": "形成争点对应关系。",
                    "blocked_by": ["完整交付凭证"],
                    "priority": "NOW",
                    "source_refs": [issue_ref, fact_ref],
                }
            ],
            "decision_requests": [
                {
                    "decision_id": "DECISION-01",
                    "question": "律师选择先补证还是受控推进",
                    "reason": "两条路径的速度与证明风险不同。",
                    "agent_lean": "SELECT_A",
                    "source_refs": [issue_ref],
                }
            ],
            "provider_receipt": {
                "model_id": "qwen3.7-plus",
                "prompt_tokens": 1200,
                "completion_tokens": 800,
                "total_tokens": 2000,
                "cost_minor_units": 2,
                "retry_count": 0,
            },
        }
        with patch.object(
            review_module,
            "parse_lawyer_decision_package_candidate",
            return_value=payload,
        ):
            review = review_module._project_payload(
                artifact_id=self.artifact_id,
                artifact_kind="LAWYER_DECISION_PACKAGE_CANDIDATE",
                payload=payload,
            )

        self.assertEqual(review.title, "律师决策包候选")
        self.assertEqual(
            [section.title for section in review.sections],
            [
                "当前可用范围",
                "律师先看",
                "争点与证据风险",
                "对方可能主张与反制",
                "策略路径与取舍",
                "需要当事人补充",
                "律师行动清单",
                "必须由律师决定",
            ],
        )
        self.assertEqual(review.sections[2].items[0].sources[0].label, "货款余额及违约责任")
        self.assertEqual(
            review.sections[2].items[0].badge,
            "优先核对 · 证据仅部分支持 · 法源待核验",
        )
        self.assertEqual(review.sections[0].items[1].title, "法律依据尚未核验")
        self.assertEqual(review.sections[6].items[0].title, "补齐并核验：完整交付凭证")
        self.assertNotIn("qwen3.7-plus", json.dumps(asdict(review), ensure_ascii=False))
        self.assertNotIn("自动重试", json.dumps(asdict(review), ensure_ascii=False))
        self.assertNotIn("request_hash", json.dumps(asdict(review), ensure_ascii=False))
        payload["client_questions"].append({
            "question_id": "QUESTION-02", "question": "请当事人补充核实并提供原始材料：有效法源依据",
            "why_it_matters": "法律路径尚待核验。", "source_refs": [issue_ref]})
        frozen = _canonical(payload)
        _, sections = review_module._lawyer_decision_package_sections(payload)
        client = next(section for section in sections if section.section_id == "lawyer-client-questions")
        research = next(section for section in sections if section.section_id == "lawyer-research-questions")
        self.assertEqual(len(client.items), 1)
        self.assertEqual(research.items[0].title, "核验：有效法源依据")
        self.assertIn("不向当事人索取", research.items[0].badge)
        self.assertEqual(_canonical(payload), frozen)


if __name__ == "__main__":
    unittest.main()
