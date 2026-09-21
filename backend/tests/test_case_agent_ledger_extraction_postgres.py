from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4
import unittest
from types import SimpleNamespace

from case_kernel.case_agent_ledger_extraction import (
    CaseLedgerExtractionCandidate,
    CaseLedgerExtractionSourcePage,
    ExtractionCandidateKind,
    ExtractionDatePrecision,
    ExtractionRiskCode,
    ExtractionSourceMode,
    ExtractionSupportingExcerpt,
    ExtractionTransactionChannel,
    ExtractionTransactionDirection,
    build_case_ledger_extraction_candidate,
    parse_case_ledger_extraction_candidate,
)
from case_kernel.case_agent_ledger_extraction_postgres import (
    CaseLedgerExtractionStagingBlocked,
    PostgresCaseLedgerExtractionPromotionStore,
    PostgresCaseLedgerExtractionStagingStore,
    _ledger_confirmation_source_binding_hash,
)
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt, VersionConflict
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.case_ledger_postgres import _payload_hash
from case_kernel.models import Actor, Role

from tests.test_case_ledger_postgres import (
    FakeConnection,
    FakeConnectionContext,
    FakeResult,
)


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _ObjectStore:
    def read_case_agent_review_candidate(self, *_args, **_kwargs) -> bytes:
        raise AssertionError("test overrides the private object re-read")


class _CurrentLedgerConnection(FakeConnection):
    def __init__(self, *, facts=(), transactions=()):
        super().__init__()
        self._facts = list(facts)
        self._transactions = list(transactions)

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT original_text, evidence_links FROM case_facts"):
            self.executed.append((normalized, params))
            return FakeResult(rows=self._facts)
        if normalized.startswith("SELECT local_date, date_precision, amount"):
            self.executed.append((normalized, params))
            return FakeResult(rows=self._transactions)
        return super().execute(sql, params)


class _DiscoveryConnection(FakeConnection):
    def __init__(self, *, snapshot_version, lineage, candidates):
        super().__init__()
        self.snapshot_version = snapshot_version
        self.lineage = lineage
        self.candidates = candidates

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if normalized.startswith(
            "SELECT agent_run.snapshot_matter_version, receipt.artifact_lineage"
        ):
            self.executed.append((normalized, params))
            return FakeResult(
                row={
                    "snapshot_matter_version": self.snapshot_version,
                    "artifact_lineage": self.lineage,
                }
            )
        if normalized.startswith("SELECT artifact_id, task_id, content_sha256"):
            self.executed.append((normalized, params))
            return FakeResult(rows=self.candidates)
        return super().execute(sql, params)


class _PriorStagingConnection(FakeConnection):
    def __init__(self, *, request_hash, response, candidate_count):
        super().__init__()
        self.request_hash = request_hash
        self.response = response
        self.candidate_count = candidate_count

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(
                row={
                    "request_hash": self.request_hash,
                    "response_json": self.response,
                }
            )
        if normalized.startswith("SELECT candidate_count FROM"):
            return FakeResult(row={"candidate_count": self.candidate_count})
        return FakeResult()


class _WebAuthorityConnection(FakeConnection):
    def __init__(
        self,
        *,
        field: str,
        value: dict | None = None,
        error: Exception | None = None,
    ):
        super().__init__()
        self._field = field
        self._value = value
        self._error = error

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT set_config('app.firm_id'"):
            return FakeResult(row={"set_config": params[0]})
        if f") AS {self._field}" in normalized:
            if self._error is not None:
                raise self._error
            return FakeResult(row={self._field: self._value})
        return FakeResult()


class PostgresCaseLedgerExtractionStagingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.run_id = str(uuid4())
        self.artifact_id = str(uuid4())
        self.task_id = str(uuid4())
        self.store = PostgresCaseLedgerExtractionStagingStore(
            dsn="postgresql://not-used.invalid/lawcase_workbench_test",
            worker_actor=self.actor, object_store=_ObjectStore(),
        )
        self.page_id = str(uuid4())
        page = CaseLedgerExtractionSourcePage(
            input_ref=f"evidence-page:{self.page_id}", evidence_page_id=self.page_id,
            source_file_sha256=digest("source-file"), page_number=1,
            source_text_sha256=digest("native text"), source_mode=ExtractionSourceMode.NATIVE_TEXT,
        )
        eligible = CaseLedgerExtractionCandidate(
            kind=ExtractionCandidateKind.FACT, source_refs=(page.input_ref,),
            evidence_page_ids=(self.page_id,), confidence=0.99,
            conflict_codes=(), risk_codes=(), fact_text="[合成] 流水显示一笔付款。",
            supporting_excerpts=(ExtractionSupportingExcerpt(self.page_id, "native text"),),
        )
        review_only = CaseLedgerExtractionCandidate(
            kind=ExtractionCandidateKind.FACT, source_refs=(page.input_ref,),
            evidence_page_ids=(self.page_id,), confidence=0.99,
            conflict_codes=(), risk_codes=(ExtractionRiskCode.OCR_DERIVED,),
            fact_text="[合成] OCR 文字仅供复核。",
            supporting_excerpts=(ExtractionSupportingExcerpt(self.page_id, "native text"),),
        )
        self.content, self.source_hash = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"), source_pages=(page,),
            candidates=(eligible, review_only),
        )

    def _stage_content(self, *, connection, content, key="extract-stage-extra"):
        binding = {
            "artifact_id": self.artifact_id,
            "graph_id": str(uuid4()),
            "task_id": self.task_id,
            "verification_receipt_id": str(uuid4()),
            "content_sha256": sha256(content).hexdigest(),
            "input_refs": [f"evidence-page:{self.page_id}"],
        }
        with (
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                return_value=FakeConnectionContext(connection),
            ),
            patch.object(self.store, "_read_verified_binding", return_value=binding),
            patch.object(self.store, "_read_object", return_value=content),
            patch.object(self.store, "_verify_source_pages", return_value=True),
        ):
            return self.store.stage_verified_artifact(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key=key,
                run_id=self.run_id,
                artifact_id=self.artifact_id,
            )

    def test_discovery_matches_all_passed_lineage_extraction_artifacts_exactly(self) -> None:
        artifact_ids = tuple(sorted((self.artifact_id, str(uuid4()))))
        candidates = [
            {
                "artifact_id": artifact_id,
                "task_id": self.task_id,
                "content_sha256": digest(artifact_id),
            }
            for artifact_id in artifact_ids
        ]
        lineage = [
            {
                "artifact_id": row["artifact_id"],
                "artifact_kind": "CASE_LEDGER_EXTRACTION_CANDIDATE",
                "task_id": row["task_id"],
                "content_hash": row["content_sha256"],
            }
            for row in candidates
        ]
        connection = _DiscoveryConnection(
            snapshot_version=1,
            lineage=lineage,
            candidates=candidates,
        )
        with patch(
            "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            discovered = self.store.discover_verified_artifact_ids(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                run_id=self.run_id,
            )
        self.assertEqual(discovered, artifact_ids)

        missing_candidate = _DiscoveryConnection(
            snapshot_version=1,
            lineage=lineage,
            candidates=candidates[:1],
        )
        with (
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                return_value=FakeConnectionContext(missing_candidate),
            ),
            self.assertRaisesRegex(
                CaseLedgerExtractionStagingBlocked, "lineage and private candidates differ"
            ),
        ):
            self.store.discover_verified_artifact_ids(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                run_id=self.run_id,
            )

    def test_stages_only_eligible_records_without_writing_formal_ledger(self) -> None:
        connection = FakeConnection()
        binding = {
            "artifact_id": self.artifact_id,
            "graph_id": str(uuid4()), "task_id": self.task_id,
            "verification_receipt_id": str(uuid4()),
            "content_sha256": sha256(self.content).hexdigest(),
            "input_refs": [f"evidence-page:{self.page_id}"],
        }
        with (
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                return_value=FakeConnectionContext(connection),
            ),
            patch.object(self.store, "_read_verified_binding", return_value=binding),
            patch.object(self.store, "_read_object", return_value=self.content),
            patch.object(self.store, "_verify_source_pages", return_value=False),
        ):
            result = self.store.stage_verified_artifact(
                matter_id=self.matter_id, actor=self.actor, expected_version=1,
                idempotency_key="extract-stage-001", run_id=self.run_id,
                artifact_id=self.artifact_id,
            )
        self.assertEqual(result.staged_candidate_count, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO case_agent_ledger_extraction_batches", sql)
        self.assertIn("INSERT INTO case_agent_ledger_extraction_candidates", sql)
        self.assertIn("INSERT INTO case_agent_ledger_extraction_candidate_pages", sql)
        self.assertNotIn("INSERT INTO case_facts", sql)
        self.assertNotIn("INSERT INTO case_transactions", sql)
        self.assertIn("INSERT INTO case_agent_ledger_extraction_staging_events", sql)
        self.assertNotIn("INSERT INTO audit_events", sql)
        self.assertNotIn("INSERT INTO outbox_events", sql)
        self.assertNotIn("UPDATE matters SET version = version + 1", sql)
        self.assertIn("INSERT INTO command_idempotency", sql)
        batch_params = next(
            params for statement, params in connection.executed
            if "INSERT INTO case_agent_ledger_extraction_batches" in statement
        )
        self.assertEqual(batch_params[8:10], (1, 1))
        self.assertEqual(result.receipt.matter_version, 1)
        candidate_params = [
            params for statement, params in connection.executed
            if "INSERT INTO case_agent_ledger_extraction_candidates" in statement
        ]
        self.assertEqual(len(candidate_params), 2)
        self.assertEqual(
            {params[7] for params in candidate_params}, {"EXCEPTION_REVIEW"}
        )
        self.assertEqual(
            {tuple(params[9]) for params in candidate_params},
            {
                ("SOURCE_TEXT_NOT_REVERIFIED",),
                ("OCR_DERIVED", "SOURCE_TEXT_NOT_REVERIFIED"),
            },
        )

    def test_private_staging_commit_lost_replays_exact_receipt_without_version_check(self) -> None:
        key = "verified-stage-replay"
        batch_id = str(uuid4())
        event_id = str(uuid4())
        request_hash = _payload_hash({
            "matter_id": self.matter_id,
            "expected_version": 1,
            "run_id": self.run_id,
            "artifact_id": self.artifact_id,
        })
        response = {
            "command_name": "STAGE_CASE_LEDGER_EXTRACTION",
            "idempotency_key": key,
            "matter_id": self.matter_id,
            "matter_version": 1,
            "audit_event_id": event_id,
            "object_type": "CASE_LEDGER_EXTRACTION_BATCH",
            "object_id": batch_id,
        }
        connection = _PriorStagingConnection(
            request_hash=request_hash,
            response=response,
            candidate_count=2,
        )
        with patch(
            "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            replayed = self.store.stage_verified_artifact(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key=key,
                run_id=self.run_id,
                artifact_id=self.artifact_id,
            )
        self.assertEqual(replayed.receipt, CaseLedgerCommandReceipt(**response))
        self.assertEqual(replayed.staged_candidate_count, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("FROM matters", sql)
        self.assertNotIn("INSERT INTO case_agent_ledger_extraction_batches", sql)
        self.assertNotIn("UPDATE matters", sql)

    def test_private_staging_requires_the_configured_dedicated_worker(self) -> None:
        wrong_worker = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        with self.assertRaisesRegex(PermissionError, "configured Worker"):
            self.store.stage_verified_artifact(
                matter_id=self.matter_id,
                actor=wrong_worker,
                expected_version=1,
                idempotency_key="wrong-worker",
                run_id=self.run_id,
                artifact_id=self.artifact_id,
            )

    def test_stages_exception_candidates_alongside_verified_bulk_lane(self) -> None:
        connection = FakeConnection()
        binding = {
            "artifact_id": self.artifact_id,
            "graph_id": str(uuid4()), "task_id": self.task_id,
            "verification_receipt_id": str(uuid4()),
            "content_sha256": sha256(self.content).hexdigest(),
            "input_refs": [f"evidence-page:{self.page_id}"],
        }
        with (
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                return_value=FakeConnectionContext(connection),
            ),
            patch.object(self.store, "_read_verified_binding", return_value=binding),
            patch.object(self.store, "_read_object", return_value=self.content),
            patch.object(self.store, "_verify_source_pages", return_value=True),
        ):
            self.store.stage_verified_artifact(
                matter_id=self.matter_id, actor=self.actor, expected_version=1,
                idempotency_key="extract-stage-002", run_id=self.run_id,
                artifact_id=self.artifact_id,
            )
        candidate_params = [
            params for statement, params in connection.executed
            if "INSERT INTO case_agent_ledger_extraction_candidates" in statement
        ]
        self.assertEqual(len(candidate_params), 2)
        self.assertEqual(
            {(params[7], params[8]) for params in candidate_params},
            {
                ("BULK_PROMOTION_ELIGIBLE", True),
                ("EXCEPTION_REVIEW", False),
            },
        )
        reason_codes = {
            (params[7], tuple(params[9])) for params in candidate_params
        }
        self.assertIn(("BULK_PROMOTION_ELIGIBLE", ()), reason_codes)
        self.assertIn(("EXCEPTION_REVIEW", ("OCR_DERIVED",)), reason_codes)

    def test_current_confirmed_fact_duplicate_or_same_page_is_exception_only(self) -> None:
        for original_text in (
            "[合成] 流水显示一笔付款。",
            "[合成] 同页已有但措辞不同的事实。",
        ):
            with self.subTest(original_text=original_text):
                connection = _CurrentLedgerConnection(
                    facts=(
                        {
                            "original_text": original_text,
                            "evidence_links": [
                                {"evidence_id": self.page_id}
                            ],
                        },
                    )
                )
                self._stage_content(
                    connection=connection,
                    content=self.content,
                    key=f"fact-collision-{len(original_text)}",
                )
                rows = [
                    params
                    for statement, params in connection.executed
                    if "INSERT INTO case_agent_ledger_extraction_candidates" in statement
                ]
                first = next(
                    params
                    for params in rows
                    if "CURRENT_LEDGER_CONFLICT_OR_DUPLICATE" in params[9]
                )
                self.assertEqual(first[7], "EXCEPTION_REVIEW")
                self.assertFalse(first[8])

    def test_current_transaction_key_or_reference_duplicate_is_exception_only(self) -> None:
        page = CaseLedgerExtractionSourcePage(
            input_ref=f"evidence-page:{self.page_id}",
            evidence_page_id=self.page_id,
            source_file_sha256=digest("source-file"),
            page_number=1,
            source_text_sha256=digest("native text"),
            source_mode=ExtractionSourceMode.NATIVE_TEXT,
        )
        transaction = CaseLedgerExtractionCandidate(
            kind=ExtractionCandidateKind.TRANSACTION,
            source_refs=(page.input_ref,),
            evidence_page_ids=(self.page_id,),
            confidence=0.99,
            conflict_codes=(),
            risk_codes=(),
            supporting_excerpts=(
                ExtractionSupportingExcerpt(self.page_id, "native text"),
            ),
            local_date="2019-06-17",
            date_precision=ExtractionDatePrecision.EXACT_DATE,
            amount="100.00",
            currency="CNY",
            direction=ExtractionTransactionDirection.OUTGOING,
            payer_label="被告",
            payee_label="原告",
            channel=ExtractionTransactionChannel.WECHAT,
            transaction_reference="wx-ref-001",
        )
        content, _ = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"),
            source_pages=(page,),
            candidates=(transaction,),
        )
        base = {
            "local_date": date(2019, 6, 17),
            "date_precision": "EXACT_DATE",
            "amount": Decimal("100.00"),
            "currency": "CNY",
            "direction": "OUTGOING",
            "payer_label": "其他标签",
            "payee_label": "其他标签",
            "channel": "WECHAT",
            "transaction_reference": "different-ref",
            "evidence_links": [{"evidence_id": str(uuid4())}],
        }
        reference_only = {
            **base,
            "local_date": date(2020, 1, 1),
            "amount": Decimal("200.00"),
            "transaction_reference": "WX-REF-001",
        }
        for label, current in (("key", base), ("reference", reference_only)):
            with self.subTest(label=label):
                connection = _CurrentLedgerConnection(transactions=(current,))
                self._stage_content(
                    connection=connection,
                    content=content,
                    key=f"transaction-collision-{label}",
                )
                params = next(
                    params
                    for statement, params in connection.executed
                    if "INSERT INTO case_agent_ledger_extraction_candidates" in statement
                )
                self.assertEqual(params[7], "EXCEPTION_REVIEW")
                self.assertFalse(params[8])
                self.assertEqual(
                    tuple(params[9]),
                    ("CURRENT_LEDGER_CONFLICT_OR_DUPLICATE",),
                )

    def test_source_text_reader_is_required_for_bulk_lane_and_checks_literal_excerpt(self) -> None:
        artifact = parse_case_ledger_extraction_candidate(self.content)
        connection = SimpleNamespace(
            execute=lambda *_args, **_kwargs: SimpleNamespace(
                fetchall=lambda: [{
                    "evidence_page_id": self.page_id, "page_number": 1,
                    "original_file_sha256": digest("source-file"),
                }]
            )
        )
        self.assertFalse(self.store._verify_source_pages(
            connection, matter_id=self.matter_id, artifact=artifact
        ))
        self.store._source_reader = SimpleNamespace(
            read_evidence_page_text=lambda **_kwargs: "native text"
        )
        self.assertTrue(self.store._verify_source_pages(
            connection, matter_id=self.matter_id, artifact=artifact
        ))
        self.store._source_reader = SimpleNamespace(
            read_evidence_page_text=lambda **_kwargs: "different text"
        )
        with self.assertRaises(CaseLedgerExtractionStagingBlocked):
            self.store._verify_source_pages(connection, matter_id=self.matter_id, artifact=artifact)

    def test_promotion_rechecks_source_text_and_literal_excerpt(self) -> None:
        artifact = parse_case_ledger_extraction_candidate(self.content)
        candidate = artifact["candidates"][0]
        candidate_id = str(uuid4())
        links = {
            candidate_id: (
                EvidenceLink(
                    evidence_id=self.page_id, original_file_sha256=digest("source-file"),
                    page_number=1, region_id=None, original_label="流水.pdf",
                ),
            )
        }
        source_hashes = {candidate_id: {self.page_id: digest("native text")}}
        no_reader = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test"
        )
        with self.assertRaises(CaseLedgerExtractionStagingBlocked):
            no_reader._verify_promotable_source_text(
                actor=Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})),
                matter_id=self.matter_id,
                candidates=[{
                    "extraction_candidate_id": candidate_id,
                    "candidate_payload": candidate,
                }],
                source_links=links, source_text_hashes=source_hashes,
                task_binding={},
            )
        verified = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test",
            evidence_page_text_reader=SimpleNamespace(
                read_evidence_page_text=lambda **_kwargs: "native text"
            ),
        )
        verified._verify_promotable_source_text(
            actor=Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})),
            matter_id=self.matter_id,
            candidates=[{
                "extraction_candidate_id": candidate_id,
                "candidate_payload": candidate,
            }],
            source_links=links, source_text_hashes=source_hashes,
            task_binding={},
        )
        mismatched = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test",
            evidence_page_text_reader=SimpleNamespace(
                read_evidence_page_text=lambda **_kwargs: "changed text"
            ),
        )
        with self.assertRaises(CaseLedgerExtractionStagingBlocked):
            mismatched._verify_promotable_source_text(
                actor=Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})),
                matter_id=self.matter_id,
                candidates=[{
                    "extraction_candidate_id": candidate_id,
                    "candidate_payload": candidate,
                }],
                source_links=links, source_text_hashes=source_hashes,
                task_binding={},
            )

    def test_only_lead_lawyer_can_confirm_and_exact_replay_succeeds(self) -> None:
        promotion = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test"
        )
        batch_id = str(uuid4())
        for role in (Role.ASSISTANT, Role.SYSTEM_WORKER):
            with self.subTest(role=role), self.assertRaises(PermissionError):
                promotion.confirm_low_risk_batch(
                    matter_id=self.matter_id,
                    actor=Actor(str(uuid4()), self.firm_id, frozenset({role})),
                    expected_version=1,
                    idempotency_key=f"forbidden-{role.value}",
                    extraction_batch_id=batch_id,
                )

        lead = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        key = "lead-confirm-replay"
        request_hash = _payload_hash({
            "matter_id": self.matter_id,
            "expected_version": 1,
            "extraction_batch_id": batch_id,
        })
        response = {
            "command_name": "CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH",
            "idempotency_key": key,
            "matter_id": self.matter_id,
            "matter_version": 2,
            "audit_event_id": str(uuid4()),
            "object_type": "CASE_LEDGER_EXTRACTION_BATCH",
            "object_id": batch_id,
        }
        connection = _PriorStagingConnection(
            request_hash=request_hash,
            response=response,
            candidate_count=1,
        )
        with patch(
            "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = promotion.confirm_low_risk_batch(
                matter_id=self.matter_id,
                actor=lead,
                expected_version=1,
                idempotency_key=key,
                extraction_batch_id=batch_id,
            )
        self.assertEqual(receipt, CaseLedgerCommandReceipt(**response))

    def test_batch_review_requires_complete_run_staging_and_exact_version_chain(self) -> None:
        batch_id = str(uuid4())
        run_id = str(uuid4())
        graph_id = str(uuid4())
        task_id = str(uuid4())
        artifact_id = str(uuid4())
        lead = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )

        class ReviewFenceConnection:
            def __init__(inner_self, *, current_version, staging_complete):
                inner_self.current_version = current_version
                inner_self.staging_complete = staging_complete

            def execute(inner_self, sql, _params=None):
                normalized = " ".join(sql.split())
                if normalized.startswith(
                    "SELECT batch.extraction_batch_id, batch.run_id"
                ):
                    return FakeResult(
                        row={
                            "extraction_batch_id": batch_id,
                            "run_id": run_id,
                            "graph_id": graph_id,
                            "task_id": task_id,
                            "artifact_id": artifact_id,
                            "candidate_count": 1,
                            "eligible_candidate_count": 1,
                            "source_matter_version": 7,
                            "staged_matter_version": 7,
                            "current_graph_id": graph_id,
                            "task_input_hash": digest("task"),
                            "task_input_refs": [],
                            "current_review_version": inner_self.current_version,
                            "run_staging_complete": inner_self.staging_complete,
                        }
                    )
                raise AssertionError(
                    "candidate rows must not be read before review fences pass"
                )

        for current_version, staging_complete in ((None, True), (8, False)):
            with self.subTest(
                current_version=current_version,
                staging_complete=staging_complete,
            ), self.assertRaisesRegex(VersionConflict, "stale"):
                PostgresCaseLedgerExtractionPromotionStore._read_current_batch(
                    ReviewFenceConnection(
                        current_version=current_version,
                        staging_complete=staging_complete,
                    ),
                    actor=lead,
                    matter_id=self.matter_id,
                    expected_version=8,
                    extraction_batch_id=batch_id,
                )

    def test_cross_firm_lead_is_rejected_by_matter_authorization(self) -> None:
        promotion = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test"
        )
        actor = Actor(
            str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER})
        )
        connection = FakeConnection()
        with (
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                return_value=FakeConnectionContext(connection),
            ),
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres._authorize_and_lock_matter",
                side_effect=PermissionError("matter is not accessible to this firm"),
            ),
            self.assertRaisesRegex(PermissionError, "this firm"),
        ):
            promotion.confirm_low_risk_batch(
                matter_id=self.matter_id,
                actor=actor,
                expected_version=1,
                idempotency_key="cross-firm-confirm",
                extraction_batch_id=str(uuid4()),
            )

    def test_web_confirmation_uses_session_bound_authorizer_and_finalizer(self) -> None:
        lead = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        session_id = str(uuid4())
        batch_id = str(uuid4())
        approval_id = str(uuid4())
        run_id = str(uuid4())
        task_id = str(uuid4())
        page_id = str(uuid4())
        candidate_id = str(uuid4())
        candidate_hash = digest("candidate")
        source_hash = digest("native source text")
        key = "web-session-confirm-001"
        receipt_value = {
            "command_name": "CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH",
            "idempotency_key": key,
            "matter_id": self.matter_id,
            "matter_version": 2,
            "audit_event_id": str(uuid4()),
            "object_type": "CASE_LEDGER_EXTRACTION_BATCH",
            "object_id": batch_id,
        }
        authorization = _WebAuthorityConnection(
            field="authorization",
            value={"status": "AUTHORIZED", "approval_id": approval_id},
        )
        source_read = FakeConnection()
        finalization = _WebAuthorityConnection(
            field="receipt", value=receipt_value
        )
        batch = {
            "run_id": run_id,
            "task_id": task_id,
            "task_input_hash": digest("task input"),
        }
        candidates = [
            {
                "extraction_candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "candidate_kind": "FACT",
                "candidate_payload": {
                    "fact_text": "[synthetic] one verified fact",
                    "evidence_page_ids": [page_id],
                    "supporting_excerpts": [
                        {"evidence_page_id": page_id, "text": "source text"}
                    ],
                },
            }
        ]
        links = {
            candidate_id: (
                EvidenceLink(
                    evidence_id=page_id,
                    original_file_sha256=digest("file"),
                    page_number=1,
                    region_id=None,
                    original_label="[synthetic] source.pdf",
                ),
            )
        }
        source_hashes = {candidate_id: {page_id: source_hash}}
        expected_source_binding = _ledger_confirmation_source_binding_hash(
            extraction_batch_id=batch_id,
            batch=batch,
            candidates=candidates,
            source_text_hashes=source_hashes,
        )
        promotion = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test",
            evidence_page_text_reader=SimpleNamespace(
                read_evidence_page_text=lambda **_kwargs: "source text"
            ),
        )
        connections = iter(
            (
                FakeConnectionContext(authorization),
                FakeConnectionContext(source_read),
                FakeConnectionContext(finalization),
            )
        )
        with (
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                side_effect=lambda *_args, **_kwargs: next(connections),
            ),
            patch.object(
                promotion,
                "_read_current_batch",
                return_value=(batch, candidates),
            ) as read_batch,
            patch.object(
                promotion,
                "_read_source_links",
                return_value=(links, source_hashes),
            ) as read_sources,
            patch.object(
                promotion, "_verify_promotable_source_text"
            ) as verify_sources,
        ):
            receipt = promotion.confirm_low_risk_batch(
                matter_id=self.matter_id,
                actor=lead,
                session_id=session_id,
                expected_version=1,
                idempotency_key=key,
                extraction_batch_id=batch_id,
            )

        self.assertEqual(receipt, CaseLedgerCommandReceipt(**receipt_value))
        authorize_call = next(
            (statement, params)
            for statement, params in authorization.executed
            if "authorize_case_agent_ledger_extraction_low_risk_confirmation" in statement
        )
        self.assertEqual(
            authorize_call[1][0:5],
            (session_id, self.matter_id, batch_id, 1, key),
        )
        self.assertEqual(
            authorize_call[1][5],
            _payload_hash(
                {
                    "matter_id": self.matter_id,
                    "expected_version": 1,
                    "extraction_batch_id": batch_id,
                }
            ),
        )
        finalize_call = next(
            (statement, params)
            for statement, params in finalization.executed
            if "finalize_case_agent_ledger_extraction_low_risk_confirmation" in statement
        )
        self.assertEqual(finalize_call[1], (approval_id, expected_source_binding))
        read_batch.assert_called_once()
        read_sources.assert_called_once()
        self.assertIs(read_batch.call_args.kwargs["lock_rows"], False)
        self.assertIs(read_sources.call_args.kwargs["lock_rows"], False)
        verify_sources.assert_called_once()

    def test_completed_web_confirmation_replay_does_not_reread_source(self) -> None:
        lead = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        session_id = str(uuid4())
        batch_id = str(uuid4())
        key = "web-session-confirm-replay"
        receipt_value = {
            "command_name": "CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH",
            "idempotency_key": key,
            "matter_id": self.matter_id,
            "matter_version": 2,
            "audit_event_id": str(uuid4()),
            "object_type": "CASE_LEDGER_EXTRACTION_BATCH",
            "object_id": batch_id,
        }
        authorization = _WebAuthorityConnection(
            field="authorization",
            value={"status": "COMPLETED", "receipt": receipt_value},
        )
        promotion = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test"
        )
        with (
            patch(
                "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                return_value=FakeConnectionContext(authorization),
            ),
            patch.object(
                promotion,
                "_read_current_batch",
                side_effect=AssertionError("replay must not read source bindings"),
            ),
        ):
            receipt = promotion.confirm_low_risk_batch(
                matter_id=self.matter_id,
                actor=lead,
                session_id=session_id,
                expected_version=1,
                idempotency_key=key,
                extraction_batch_id=batch_id,
            )
        self.assertEqual(receipt, CaseLedgerCommandReceipt(**receipt_value))

    def test_database_session_role_or_tenant_rejection_cannot_reach_source_or_write(self) -> None:
        lead = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        promotion = PostgresCaseLedgerExtractionPromotionStore(
            "postgresql://not-used.invalid/lawcase_workbench_test"
        )
        for reason in (
            "expired or revoked session",
            "revoked lead role",
            "cross-firm matter",
        ):
            with self.subTest(reason=reason):
                authorization = _WebAuthorityConnection(
                    field="authorization", error=PermissionError(reason)
                )
                with (
                    patch(
                        "case_kernel.case_agent_ledger_extraction_postgres.psycopg.connect",
                        return_value=FakeConnectionContext(authorization),
                    ),
                    patch.object(
                        promotion,
                        "_read_current_batch",
                        side_effect=AssertionError(
                            "rejected authority must not read source bindings"
                        ),
                    ),
                    self.assertRaisesRegex(PermissionError, reason),
                ):
                    promotion.confirm_low_risk_batch(
                        matter_id=self.matter_id,
                        actor=lead,
                        session_id=str(uuid4()),
                        expected_version=1,
                        idempotency_key=f"web-denied-{reason.replace(' ', '-')}",
                        extraction_batch_id=str(uuid4()),
                    )
