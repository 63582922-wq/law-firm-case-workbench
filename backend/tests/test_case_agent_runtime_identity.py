from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from case_kernel.case_agent_runtime_identity import (
    CASE_AGENT_PLANNER_ID,
    ExactCaseAgentRuntimeReadiness,
    FIRST_RELEASE_ALLOWED_ADAPTER_CATALOG_HASHES,
    FIRST_RELEASE_ADAPTER_CATALOG_HASH,
    FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASH,
    FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASHES,
    FIRST_RELEASE_LEDGER_ADAPTER_CATALOG_HASHES,
    FIRST_RELEASE_LAWYER_ANALYSIS_ADAPTER_CATALOG_HASHES,
    FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH,
    FIRST_RELEASE_RESEARCH_DOCUMENT_ADAPTER_CATALOG_HASH,
    FIRST_RELEASE_RESEARCH_VISUAL_ADAPTER_CATALOG_HASH,
    FIRST_RELEASE_RESEARCH_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
    FIRST_RELEASE_VISUAL_ADAPTER_CATALOG_HASH,
    FIRST_RELEASE_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
    first_release_adapter_catalog_hash,
    case_agent_worker_id,
)
from case_kernel.case_agent_case_context_adapters import (
    CASE_CONTEXT_REVIEW_MANIFEST,
)
from case_kernel.case_agent_legal_research_plan_adapters import (
    LEGAL_RESEARCH_PLANNING_MANIFEST,
)
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_MANIFEST,
)
from case_kernel.case_agent_document_adapters import (
    DOCX_DOCUMENT_DELIVERY_MANIFEST,
    XLSX_DOCUMENT_DELIVERY_MANIFEST,
)
from case_kernel.case_agent_research_adapters import PUBLIC_WEB_RESEARCH_MANIFEST
from case_kernel.qwen_visual_ocr_adapter import QWEN_VISUAL_OCR_MANIFEST
from case_kernel.case_agent_skill_adapters import (
    COMMON_DOCUMENT_READER_MANIFEST,
    PDF_TEXT_READER_MANIFEST,
)
from case_kernel.case_agent_worker import _adapter_catalog_hash
from case_kernel.case_agent_verifier import (
    FIRST_RELEASE_VERIFIER_ID,
    FIRST_RELEASE_VERIFIER_POLICY_HASH,
    FIRST_RELEASE_VERIFIER_VERSION,
)


class _Store:
    def __init__(self, heartbeat, *, reachable=True, principals_ready=True):
        self.heartbeat = heartbeat
        self.reachable = reachable
        self.principals_ready = principals_ready
        self.calls = []

    def probe_case_agent_store(self, **kwargs):
        self.calls.append({"probe": kwargs})
        return self.reachable

    def probe_case_agent_matter_principals(self, **kwargs):
        self.calls.append({"principals": kwargs})
        return self.principals_ready

    def latest_worker_heartbeat(self, **kwargs):
        self.calls.append(kwargs)
        return self.heartbeat


class ExactCaseAgentRuntimeReadinessTests(unittest.TestCase):
    def test_planner_identity_matches_production_router_not_fallback(self) -> None:
        from case_kernel.controlled_defence_case_agent_planner import (
            ControlledDefencePlanningRouter,
        )

        self.assertEqual(CASE_AGENT_PLANNER_ID, ControlledDefencePlanningRouter.planner_id)
        self.assertNotEqual(CASE_AGENT_PLANNER_ID, "deepseek")

    def test_base_catalog_matches_the_actual_always_registered_adapters(self) -> None:
        class Adapter:
            def __init__(self, manifest):
                self.manifest = manifest

        base = {
            manifest.tool_id: Adapter(manifest)
            for manifest in (
                PDF_TEXT_READER_MANIFEST,
                COMMON_DOCUMENT_READER_MANIFEST,
                CASE_CONTEXT_REVIEW_MANIFEST,
                LEGAL_RESEARCH_PLANNING_MANIFEST,
            )
        }
        expected = (
            (FIRST_RELEASE_ADAPTER_CATALOG_HASH, ()),
            (
                FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH,
                (PUBLIC_WEB_RESEARCH_MANIFEST,),
            ),
            (
                FIRST_RELEASE_VISUAL_ADAPTER_CATALOG_HASH,
                (QWEN_VISUAL_OCR_MANIFEST,),
            ),
            (
                FIRST_RELEASE_RESEARCH_VISUAL_ADAPTER_CATALOG_HASH,
                (PUBLIC_WEB_RESEARCH_MANIFEST, QWEN_VISUAL_OCR_MANIFEST),
            ),
            (
                FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASH,
                (DOCX_DOCUMENT_DELIVERY_MANIFEST, XLSX_DOCUMENT_DELIVERY_MANIFEST),
            ),
            (
                FIRST_RELEASE_RESEARCH_DOCUMENT_ADAPTER_CATALOG_HASH,
                (
                    PUBLIC_WEB_RESEARCH_MANIFEST,
                    DOCX_DOCUMENT_DELIVERY_MANIFEST,
                    XLSX_DOCUMENT_DELIVERY_MANIFEST,
                ),
            ),
            (
                FIRST_RELEASE_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
                (
                    QWEN_VISUAL_OCR_MANIFEST,
                    DOCX_DOCUMENT_DELIVERY_MANIFEST,
                    XLSX_DOCUMENT_DELIVERY_MANIFEST,
                ),
            ),
            (
                FIRST_RELEASE_RESEARCH_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
                (
                    PUBLIC_WEB_RESEARCH_MANIFEST,
                    QWEN_VISUAL_OCR_MANIFEST,
                    DOCX_DOCUMENT_DELIVERY_MANIFEST,
                    XLSX_DOCUMENT_DELIVERY_MANIFEST,
                ),
            ),
        )
        for expected_hash, optional_manifests in expected:
            with self.subTest(expected_hash=expected_hash):
                adapters = dict(base)
                adapters.update(
                    {
                        manifest.tool_id: Adapter(manifest)
                        for manifest in optional_manifests
                    }
                )
                self.assertEqual(expected_hash, _adapter_catalog_hash(adapters))

        for controlled_web_search in (False, True):
            for visual_ocr in (False, True):
                for dynamic_documents in (False, True):
                    with self.subTest(
                        controlled_web_search=controlled_web_search,
                        visual_ocr=visual_ocr,
                        dynamic_documents=dynamic_documents,
                        case_ledger_extraction=True,
                    ):
                        adapters = dict(base)
                        for enabled, manifest in (
                            (controlled_web_search, PUBLIC_WEB_RESEARCH_MANIFEST),
                            (visual_ocr, QWEN_VISUAL_OCR_MANIFEST),
                        ):
                            if enabled:
                                adapters[manifest.tool_id] = Adapter(manifest)
                        if dynamic_documents:
                            for manifest in (
                                DOCX_DOCUMENT_DELIVERY_MANIFEST,
                                XLSX_DOCUMENT_DELIVERY_MANIFEST,
                            ):
                                adapters[manifest.tool_id] = Adapter(manifest)
                        adapters[DEEPSEEK_LEDGER_EXTRACTION_MANIFEST.tool_id] = (
                            Adapter(DEEPSEEK_LEDGER_EXTRACTION_MANIFEST)
                        )
                        expected_hash = first_release_adapter_catalog_hash(
                            controlled_web_search=controlled_web_search,
                            visual_ocr=visual_ocr,
                            dynamic_documents=dynamic_documents,
                            case_ledger_extraction=True,
                        )
                        self.assertIn(
                            expected_hash,
                            FIRST_RELEASE_LEDGER_ADAPTER_CATALOG_HASHES,
                        )
                        self.assertEqual(
                            expected_hash, _adapter_catalog_hash(adapters)
                        )

    def test_requires_exact_actor_planner_catalog_and_firm_worker_id(self) -> None:
        firm_id, actor_id, verifier_actor_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        now = datetime.now(timezone.utc)
        heartbeat = SimpleNamespace(
            firm_id=firm_id,
            worker_id=case_agent_worker_id(firm_id),
            actor_id=actor_id,
            planner_id=CASE_AGENT_PLANNER_ID,
            adapter_catalog_hash=FIRST_RELEASE_ADAPTER_CATALOG_HASH,
            verifier_actor_id=verifier_actor_id,
            verifier_id=FIRST_RELEASE_VERIFIER_ID,
            verifier_version=FIRST_RELEASE_VERIFIER_VERSION,
            verifier_policy_hash=FIRST_RELEASE_VERIFIER_POLICY_HASH,
            observed_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(seconds=60),
        )
        store = _Store(heartbeat)
        readiness = ExactCaseAgentRuntimeReadiness(
            store=store,
            matter_principal_probe=store.probe_case_agent_matter_principals,
            worker_actor_ids_by_firm={firm_id: actor_id},
            verifier_actor_ids_by_firm={firm_id: verifier_actor_id},
            clock=lambda: now,
        )
        self.assertTrue(readiness(firm_id))
        self.assertEqual(
            store.calls,
            [
                {"probe": {"firm_id": firm_id}},
                {
                    "principals": {
                        "firm_id": firm_id,
                        "execution_actor_id": actor_id,
                        "verifier_actor_id": verifier_actor_id,
                    }
                },
                {"firm_id": firm_id, "worker_id": case_agent_worker_id(firm_id)},
            ],
        )
        for field, value in (
            ("actor_id", str(uuid4())),
            ("planner_id", "another-planner"),
            ("planner_id", "deepseek"),
            ("adapter_catalog_hash", "0" * 64),
            ("verifier_actor_id", str(uuid4())),
            ("verifier_id", "another-verifier"),
            ("verifier_version", "9.9.9"),
            ("verifier_policy_hash", "0" * 64),
        ):
            bad = SimpleNamespace(**{**vars(heartbeat), field: value})
            bad_store = _Store(bad)
            self.assertFalse(
                ExactCaseAgentRuntimeReadiness(
                    store=bad_store,
                    matter_principal_probe=(
                        bad_store.probe_case_agent_matter_principals
                    ),
                    worker_actor_ids_by_firm={firm_id: actor_id},
                    verifier_actor_ids_by_firm={firm_id: verifier_actor_id},
                    clock=lambda: now,
                )(firm_id)
            )

    def test_general_runtime_can_be_ready_while_ledger_runtime_is_not(self) -> None:
        firm_id, actor_id, verifier_actor_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        now = datetime.now(timezone.utc)

        def readiness_for(catalog_hash: str) -> ExactCaseAgentRuntimeReadiness:
            heartbeat = SimpleNamespace(
                firm_id=firm_id,
                worker_id=case_agent_worker_id(firm_id),
                actor_id=actor_id,
                planner_id=CASE_AGENT_PLANNER_ID,
                adapter_catalog_hash=catalog_hash,
                verifier_actor_id=verifier_actor_id,
                verifier_id=FIRST_RELEASE_VERIFIER_ID,
                verifier_version=FIRST_RELEASE_VERIFIER_VERSION,
                verifier_policy_hash=FIRST_RELEASE_VERIFIER_POLICY_HASH,
                observed_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(seconds=60),
            )
            store = _Store(heartbeat)
            return ExactCaseAgentRuntimeReadiness(
                store=store,
                matter_principal_probe=store.probe_case_agent_matter_principals,
                worker_actor_ids_by_firm={firm_id: actor_id},
                verifier_actor_ids_by_firm={firm_id: verifier_actor_id},
                clock=lambda: now,
            )

        base = readiness_for(FIRST_RELEASE_ADAPTER_CATALOG_HASH)
        self.assertTrue(base(firm_id))
        self.assertFalse(base.ledger_ready(firm_id))

        ledger = readiness_for(
            first_release_adapter_catalog_hash(case_ledger_extraction=True)
        )
        self.assertTrue(ledger(firm_id))
        self.assertTrue(ledger.ledger_ready(firm_id))

    def test_document_readiness_requires_document_adapters_and_accepts_ledger(self) -> None:
        firm_id, actor_id, verifier_actor_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        now = datetime.now(timezone.utc)

        def readiness_for(catalog_hash: str) -> ExactCaseAgentRuntimeReadiness:
            heartbeat = SimpleNamespace(
                firm_id=firm_id,
                worker_id=case_agent_worker_id(firm_id),
                actor_id=actor_id,
                planner_id=CASE_AGENT_PLANNER_ID,
                adapter_catalog_hash=catalog_hash,
                verifier_actor_id=verifier_actor_id,
                verifier_id=FIRST_RELEASE_VERIFIER_ID,
                verifier_version=FIRST_RELEASE_VERIFIER_VERSION,
                verifier_policy_hash=FIRST_RELEASE_VERIFIER_POLICY_HASH,
                observed_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(seconds=60),
            )
            store = _Store(heartbeat)
            return ExactCaseAgentRuntimeReadiness(
                store=store,
                matter_principal_probe=store.probe_case_agent_matter_principals,
                worker_actor_ids_by_firm={firm_id: actor_id},
                verifier_actor_ids_by_firm={firm_id: verifier_actor_id},
                clock=lambda: now,
            )

        self.assertFalse(
            readiness_for(FIRST_RELEASE_ADAPTER_CATALOG_HASH).document_ready(
                firm_id
            )
        )
        self.assertFalse(
            readiness_for(
                first_release_adapter_catalog_hash(case_ledger_extraction=True)
            ).document_ready(firm_id)
        )
        self.assertTrue(
            readiness_for(FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASH).document_ready(
                firm_id
            )
        )
        self.assertTrue(
            readiness_for(
                first_release_adapter_catalog_hash(
                    dynamic_documents=True,
                    case_ledger_extraction=True,
                )
            ).document_ready(firm_id)
        )

    def test_optional_research_catalog_is_exact_and_does_not_replace_base(self) -> None:
        self.assertEqual(
            FIRST_RELEASE_ADAPTER_CATALOG_HASH,
            first_release_adapter_catalog_hash(controlled_web_search=False),
        )
        self.assertEqual(
            FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH,
            first_release_adapter_catalog_hash(controlled_web_search=True),
        )
        self.assertNotEqual(
            FIRST_RELEASE_ADAPTER_CATALOG_HASH,
            FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH,
        )
        self.assertEqual(
            FIRST_RELEASE_VISUAL_ADAPTER_CATALOG_HASH,
            first_release_adapter_catalog_hash(visual_ocr=True),
        )
        self.assertEqual(
            FIRST_RELEASE_RESEARCH_VISUAL_ADAPTER_CATALOG_HASH,
            first_release_adapter_catalog_hash(
                controlled_web_search=True,
                visual_ocr=True,
            ),
        )
        self.assertEqual(
            len(
                {
                    FIRST_RELEASE_ADAPTER_CATALOG_HASH,
                    FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH,
                    FIRST_RELEASE_VISUAL_ADAPTER_CATALOG_HASH,
                    FIRST_RELEASE_RESEARCH_VISUAL_ADAPTER_CATALOG_HASH,
                    FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASH,
                    FIRST_RELEASE_RESEARCH_DOCUMENT_ADAPTER_CATALOG_HASH,
                    FIRST_RELEASE_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
                    FIRST_RELEASE_RESEARCH_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
                }
            ),
            8,
        )
        self.assertEqual(len(FIRST_RELEASE_LEDGER_ADAPTER_CATALOG_HASHES), 16)
        self.assertEqual(len(FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASHES), 16)
        self.assertEqual(
            len(FIRST_RELEASE_LAWYER_ANALYSIS_ADAPTER_CATALOG_HASHES), 16
        )
        self.assertEqual(len(FIRST_RELEASE_ALLOWED_ADAPTER_CATALOG_HASHES), 32)
        self.assertIn(
            first_release_adapter_catalog_hash(lawyer_analysis=True),
            FIRST_RELEASE_LAWYER_ANALYSIS_ADAPTER_CATALOG_HASHES,
        )
        self.assertEqual(
            FIRST_RELEASE_RESEARCH_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
            first_release_adapter_catalog_hash(
                controlled_web_search=True,
                visual_ocr=True,
                dynamic_documents=True,
            ),
        )

    def test_exact_readiness_accepts_the_optional_research_catalog_heartbeat(self) -> None:
        firm_id, actor_id, verifier_actor_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        now = datetime.now(timezone.utc)
        heartbeat = SimpleNamespace(
            firm_id=firm_id,
            worker_id=case_agent_worker_id(firm_id),
            actor_id=actor_id,
            planner_id=CASE_AGENT_PLANNER_ID,
            adapter_catalog_hash=FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH,
            verifier_actor_id=verifier_actor_id,
            verifier_id=FIRST_RELEASE_VERIFIER_ID,
            verifier_version=FIRST_RELEASE_VERIFIER_VERSION,
            verifier_policy_hash=FIRST_RELEASE_VERIFIER_POLICY_HASH,
            observed_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(seconds=60),
        )
        store = _Store(heartbeat)
        readiness = ExactCaseAgentRuntimeReadiness(
            store=store,
            matter_principal_probe=store.probe_case_agent_matter_principals,
            worker_actor_ids_by_firm={firm_id: actor_id},
            verifier_actor_ids_by_firm={firm_id: verifier_actor_id},
            clock=lambda: now,
        )
        self.assertTrue(readiness(firm_id))

    def test_exact_readiness_accepts_each_configured_catalog(self) -> None:
        for catalog_hash in FIRST_RELEASE_ALLOWED_ADAPTER_CATALOG_HASHES:
            with self.subTest(catalog_hash=catalog_hash):
                firm_id, actor_id, verifier_actor_id = (
                    str(uuid4()), str(uuid4()), str(uuid4())
                )
                now = datetime.now(timezone.utc)
                heartbeat = SimpleNamespace(
                    firm_id=firm_id,
                    worker_id=case_agent_worker_id(firm_id),
                    actor_id=actor_id,
                    planner_id=CASE_AGENT_PLANNER_ID,
                    adapter_catalog_hash=catalog_hash,
                    verifier_actor_id=verifier_actor_id,
                    verifier_id=FIRST_RELEASE_VERIFIER_ID,
                    verifier_version=FIRST_RELEASE_VERIFIER_VERSION,
                    verifier_policy_hash=FIRST_RELEASE_VERIFIER_POLICY_HASH,
                    observed_at=now - timedelta(seconds=1),
                    expires_at=now + timedelta(seconds=60),
                )
                store = _Store(heartbeat)
                readiness = ExactCaseAgentRuntimeReadiness(
                    store=store,
                    matter_principal_probe=store.probe_case_agent_matter_principals,
                    worker_actor_ids_by_firm={firm_id: actor_id},
                    verifier_actor_ids_by_firm={firm_id: verifier_actor_id},
                    clock=lambda: now,
                )
                self.assertTrue(readiness(firm_id))

    def test_configuration_rejects_self_verifier_or_unmatched_firms(self) -> None:
        firm_id, actor_id = str(uuid4()), str(uuid4())
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            store = _Store(None)
            ExactCaseAgentRuntimeReadiness(
                store=store,
                matter_principal_probe=store.probe_case_agent_matter_principals,
                worker_actor_ids_by_firm={firm_id: actor_id},
                verifier_actor_ids_by_firm={firm_id: actor_id},
            )

    def test_store_probe_must_be_live_even_when_heartbeat_is_fresh(self) -> None:
        firm_id, actor_id, verifier_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        store = _Store(None, reachable=False)
        readiness = ExactCaseAgentRuntimeReadiness(
            store=store,
            matter_principal_probe=store.probe_case_agent_matter_principals,
            worker_actor_ids_by_firm={firm_id: actor_id},
            verifier_actor_ids_by_firm={firm_id: verifier_id},
        )
        self.assertFalse(readiness(firm_id))

    def test_fresh_heartbeat_does_not_override_missing_matter_principal_readiness(self) -> None:
        firm_id, actor_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        store = _Store(None, principals_ready=False)
        readiness = ExactCaseAgentRuntimeReadiness(
            store=store,
            matter_principal_probe=store.probe_case_agent_matter_principals,
            worker_actor_ids_by_firm={firm_id: actor_id},
            verifier_actor_ids_by_firm={firm_id: verifier_id},
        )
        self.assertFalse(readiness(firm_id))


if __name__ == "__main__":
    unittest.main()
