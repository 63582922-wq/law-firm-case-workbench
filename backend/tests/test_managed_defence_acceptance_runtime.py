import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(
    os.environ.get("LAWCASE_TEST_REPOSITORY_ROOT", str(ROOT))
).resolve()


class ManagedDefenceAcceptanceRuntimeTests(unittest.TestCase):
    def test_seeded_fixture_reports_limited_evidence_scope(self):
        acceptance = self._load_acceptance_module()
        scope = acceptance._acceptance_evidence_scope()
        self.assertEqual(scope["scope"], "SEEDED_FACTS_TO_REVIEW_DOCUMENT")
        self.assertTrue(scope["facts_preconfirmed_by_harness"])
        self.assertTrue(scope["source_fixture_contains_machine_readable_markers"])
        for field in ("proves_unassisted_material_understanding", "proves_visual_ocr",
                      "proves_lawyer_user_acceptance", "commercial_release_accepted"):
            self.assertIs(scope[field], False)

    def _load_acceptance_module(self):
        path = ROOT / "backend/scripts/run_managed_defence_acceptance.py"
        name = "_lawcase_managed_defence_acceptance_runtime_test"
        spec = importlib.util.spec_from_file_location(name, path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module

    def test_in_stack_acceptance_uses_only_injected_web_environment(self):
        acceptance = self._load_acceptance_module()
        with patch.dict(
            os.environ,
            {"LAWCASE_WEB_RUNTIME_MODE": "PRODUCTION_WEB"},
            clear=True,
        ):
            resolved = acceptance._runtime_mapping(None)
        self.assertEqual(resolved, {"LAWCASE_WEB_RUNTIME_MODE": "PRODUCTION_WEB"})

    def test_explicit_operator_env_file_merges_without_overriding_process_values(self):
        acceptance = self._load_acceptance_module()
        with TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "acceptance.env"
            env_file.write_text(
                "LAWCASE_ACCEPTANCE_MODE=file\nLAWCASE_ACCEPTANCE_ONLY=file\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"LAWCASE_ACCEPTANCE_MODE": "process"},
                clear=True,
            ):
                resolved = acceptance._runtime_mapping(env_file)
        self.assertEqual(resolved["LAWCASE_ACCEPTANCE_MODE"], "process")
        self.assertEqual(resolved["LAWCASE_ACCEPTANCE_ONLY"], "file")

    def test_missing_explicit_operator_env_file_fails_closed(self):
        acceptance = self._load_acceptance_module()
        with TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.env"
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "环境文件不存在"
            ):
                acceptance._runtime_mapping(missing)

    def test_acceptance_creates_its_fixed_matter_before_synthetic_intake(self):
        acceptance = self._load_acceptance_module()
        calls: list[str] = []

        def create_matter(unused_composition, unused_identity):
            del unused_composition, unused_identity
            calls.append("matter")
            return object()

        def prepare_case(unused_composition, unused_identity):
            del unused_composition, unused_identity
            calls.append("intake")
            raise acceptance.ManagedDefenceAcceptanceBlocked("stop-after-order-check")

        with patch.object(acceptance, "_create_new_matter", side_effect=create_matter):
            with patch.object(acceptance, "_prepare_case", side_effect=prepare_case):
                with self.assertRaisesRegex(
                    acceptance.ManagedDefenceAcceptanceBlocked, "stop-after-order-check"
                ):
                    acceptance._run_acceptance(
                        object(), object(), max_wait_seconds=30
                    )
        self.assertEqual(calls, ["matter", "intake"])

    def test_duplicate_image_bytes_keep_distinct_frozen_reservation_identity(self):
        acceptance = self._load_acceptance_module()
        digest = "a" * 64
        original = acceptance.GeneratedFile(
            logical_code="F2a",
            source_code="F2",
            file_name="借条1_照片.jpg",
            media_type="image/jpeg",
            page_count=1,
            file_sha256=digest,
        )
        duplicate = acceptance.GeneratedFile(
            logical_code="F2b",
            source_code="F2",
            file_name="借条1_照片_副本.jpg",
            media_type="image/jpeg",
            page_count=1,
            file_sha256=digest,
        )
        original_key = acceptance._common_material_reservation_key(original)
        duplicate_key = acceptance._common_material_reservation_key(duplicate)
        self.assertNotEqual(original_key, duplicate_key)
        self.assertRegex(original_key, r"^[A-Za-z0-9._~-]{16,128}$")
        self.assertRegex(duplicate_key, r"^[A-Za-z0-9._~-]{16,128}$")

    def test_acceptance_keys_are_deterministic_and_browser_safe(self):
        acceptance = self._load_acceptance_module()
        label = "confirm-defendant-first-instance-posture"
        key = acceptance._key(label)
        self.assertEqual(key, acceptance._key(label))
        self.assertRegex(key, r"^[A-Za-z0-9._~-]{16,128}$")
        self.assertIsNone(re.search(r"[:\\s]", key))

    def test_v2_v3_and_v4_use_distinct_identities_and_recovery_boundaries(self):
        acceptance = self._load_acceptance_module()
        v1_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._CONTRACT_REPAIR_ACCEPTANCE_NAME
        )

        self.assertEqual(
            acceptance._ACCEPTANCE_NAME,
            acceptance._CONTRACT_REPAIR_ACCEPTANCE_NAME,
        )
        self.assertNotEqual(
            v1_matter,
            acceptance._acceptance_id("matter:firm-1"),
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "固定允许列表"
        ):
            acceptance._select_acceptance_scenario("managed-defence-single-call-v4")
        acceptance._validate_acceptance_recovery_mode(
            resume_pre_model_intake=False,
            resume_pre_agent_inputs=True,
            resume_created_defence_run=False,
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "零 Agent、零外发"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=True,
                resume_pre_agent_inputs=False,
                resume_created_defence_run=False,
            )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "零 Agent、零外发"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=False,
                resume_pre_agent_inputs=False,
                resume_created_defence_run=True,
            )

        v2_matter = acceptance._acceptance_id("matter:firm-1")
        acceptance._select_acceptance_scenario(
            acceptance._FINAL_RUNTIME_ACCEPTANCE_NAME
        )
        self.assertEqual(
            acceptance._ACCEPTANCE_NAME,
            acceptance._FINAL_RUNTIME_ACCEPTANCE_NAME,
        )
        self.assertNotEqual(v1_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertNotEqual(v2_matter, acceptance._acceptance_id("matter:firm-1"))
        for recovery_mode in (
            {
                "resume_pre_model_intake": True,
                "resume_pre_agent_inputs": False,
                "resume_created_defence_run": False,
            },
            {
                "resume_pre_model_intake": False,
                "resume_pre_agent_inputs": True,
                "resume_created_defence_run": False,
            },
            {
                "resume_pre_model_intake": False,
                "resume_pre_agent_inputs": False,
                "resume_created_defence_run": True,
            },
        ):
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "只能新建一次"
            ):
                acceptance._validate_acceptance_recovery_mode(**recovery_mode)

        v3_matter = acceptance._acceptance_id("matter:firm-1")
        acceptance._select_acceptance_scenario(
            acceptance._SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
        )
        self.assertEqual(
            acceptance._ACCEPTANCE_NAME,
            acceptance._SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME,
        )
        self.assertNotEqual(v1_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertNotEqual(v2_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertNotEqual(v3_matter, acceptance._acceptance_id("matter:firm-1"))
        acceptance._validate_acceptance_recovery_mode(
            resume_pre_model_intake=False,
            resume_pre_agent_inputs=False,
            resume_created_defence_run=False,
            resume_v4_pre_model_source=True,
        )
        for recovery_mode in (
            {
                "resume_pre_model_intake": True,
                "resume_pre_agent_inputs": False,
                "resume_created_defence_run": False,
            },
            {
                "resume_pre_model_intake": False,
                "resume_pre_agent_inputs": True,
                "resume_created_defence_run": False,
            },
            {
                "resume_pre_model_intake": False,
                "resume_pre_agent_inputs": False,
                "resume_created_defence_run": True,
            },
        ):
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "只能新建一次"
            ):
                acceptance._validate_acceptance_recovery_mode(**recovery_mode)

    def test_m1_source_qualified_final_acceptance_is_distinct_and_has_only_exact_source_recovery(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
        )
        v4_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        source = acceptance._current_official_source_spec()

        self.assertNotEqual(v4_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertEqual(
            source.official_url,
            "https://tjca.miit.gov.cn/zwgk/zcwj/flfg/art/2020/"
            "art_20cf1a2e1b854924b5caa744c8045d1f.html",
        )
        self.assertEqual(source.content_media_type, "text/html")
        self.assertEqual(source.frozen_bytes, 1_164_455)
        self.assertEqual(len(source.frozen_content_sha256), 64)
        self.assertTrue(
            {"中华人民共和国民法典", "第六百七十九条", "第六百八十条"}
            .issubset(source.required_markers)
        )
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            (
                "PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE",
                "M1-SOURCE-2026-09-04",
            ),
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "只能新建一次"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=False,
                resume_pre_agent_inputs=True,
                resume_created_defence_run=False,
            )
        acceptance._validate_acceptance_recovery_mode(
            resume_pre_model_intake=False,
            resume_pre_agent_inputs=False,
            resume_created_defence_run=False,
            resume_m1_pre_model_source=True,
        )
        acceptance._validate_acceptance_recovery_mode(
            resume_pre_model_intake=False,
            resume_pre_agent_inputs=False,
            resume_created_defence_run=False,
            resume_m1_post_event_rule=True,
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "仅允许 v4"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=False,
                resume_pre_agent_inputs=False,
                resume_created_defence_run=False,
                resume_v4_pre_model_source=True,
            )

    def test_m2_is_a_new_source_qualified_single_call_case_not_an_m1_recovery(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        m1_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        source = acceptance._current_official_source_spec()

        self.assertNotEqual(m1_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertEqual(
            source.official_url,
            "https://tjca.miit.gov.cn/zwgk/zcwj/flfg/art/2020/"
            "art_20cf1a2e1b854924b5caa744c8045d1f.html",
        )
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            (
                "PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE",
                "M2-SOURCE-2026-09-04",
            ),
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "只能新建一次"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=False,
                resume_pre_agent_inputs=True,
                resume_created_defence_run=False,
            )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "仅允许 M1"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=False,
                resume_pre_agent_inputs=False,
                resume_created_defence_run=False,
                resume_m1_pre_model_source=True,
            )
        acceptance._validate_acceptance_recovery_mode(
            resume_pre_model_intake=False,
            resume_pre_agent_inputs=False,
            resume_created_defence_run=False,
            resume_m2_pre_model_source=True,
        )

    def test_m2_same_firm_frozen_source_requires_a_recent_capture(self):
        acceptance = self._load_acceptance_module()
        captured_at = datetime(2026, 9, 4, 14, 11, tzinfo=timezone.utc)
        with patch.object(
            acceptance,
            "_safe_now",
            return_value=captured_at + timedelta(hours=23, minutes=59),
        ):
            acceptance._validate_m2_firm_frozen_source_recency(captured_at)
        with patch.object(
            acceptance,
            "_safe_now",
            return_value=captured_at + timedelta(hours=24, seconds=1),
        ):
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "超过 24 小时"
            ):
                acceptance._validate_m2_firm_frozen_source_recency(captured_at)
        with patch.object(
            acceptance,
            "_safe_now",
            return_value=captured_at,
        ):
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "晚于受控时钟"
            ):
                acceptance._validate_m2_firm_frozen_source_recency(
                    captured_at + timedelta(minutes=6)
                )

    def test_m3_is_a_distinct_current_runtime_case_with_fresh_source_preflight(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        m2_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME
        )
        self.assertNotEqual(m2_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            (
                "PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE",
                "M3-SOURCE-2026-09-05",
            ),
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "只能新建一次"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=False,
                resume_pre_agent_inputs=True,
                resume_created_defence_run=False,
            )
        captured_at = datetime(2026, 9, 4, 14, 11, tzinfo=timezone.utc)
        with patch.object(
            acceptance,
            "_safe_now",
            return_value=captured_at + timedelta(hours=23, minutes=59),
        ):
            acceptance._validate_m3_firm_frozen_source_recency(captured_at)
        with patch.object(
            acceptance,
            "_safe_now",
            return_value=captured_at + timedelta(hours=24, seconds=1),
        ):
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "M3.*超过 24 小时"
            ):
                acceptance._validate_m3_firm_frozen_source_recency(captured_at)

    def test_m3_new_case_uses_fresh_source_preflight_before_any_run(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME
        )
        composition = SimpleNamespace(
            api_dependencies=SimpleNamespace(
                case_agent_control_service=Mock(),
                dynamic_case_plan_service=Mock(),
            )
        )
        identity = object()
        with patch.object(acceptance, "_create_new_matter") as create_matter:
            with patch.object(
                acceptance,
                "_prepare_case",
                side_effect=acceptance.ManagedDefenceAcceptanceBlocked("stop"),
            ) as prepare_case:
                with self.assertRaisesRegex(acceptance.ManagedDefenceAcceptanceBlocked, "stop"):
                    acceptance._run_acceptance(
                        composition, identity, max_wait_seconds=30
                    )
        create_matter.assert_called_once_with(composition, identity)
        prepare_case.assert_called_once_with(
            composition,
            identity,
            m3_firm_frozen_source_preflight=True,
        )

    def test_m4_is_a_distinct_full_delivery_case_with_four_sorted_outputs(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME
        )
        m3_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M4_FULL_DELIVERY_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m3_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            tuple(item.value for item in acceptance._requested_deliverables_for_acceptance()),
            (
                "CASE_REVIEW_MEMO",
                "DEFENCE_STATEMENT",
                "EVIDENCE_CATALOGUE",
                "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
            ),
        )
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M4-SOURCE-2026-09-11"),
        )

    def test_m5_is_distinct_from_stale_source_m4_and_allows_fresh_capture_path(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M4_FULL_DELIVERY_ACCEPTANCE_NAME
        )
        m4_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m4_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M5-SOURCE-2026-09-11"),
        )

    def test_m6_is_a_distinct_full_delivery_case_with_a_narrow_confirmed_evidence_scope(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME
        )
        m5_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m5_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M6-SOURCE-2026-09-11"),
        )
        self.assertEqual(
            acceptance._M6_CATALOGUE_EVIDENCE_SCOPE,
            frozenset(
                {
                    ("F1", 2), ("F1", 5), ("F4", 30),
                    ("F4", 32), ("F6", 3), ("F6", 4),
                }
            ),
        )
        acceptance._validate_acceptance_recovery_mode(
            resume_pre_model_intake=False,
            resume_pre_agent_inputs=True,
            resume_created_defence_run=False,
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "M6 只允许"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=True,
                resume_pre_agent_inputs=False,
                resume_created_defence_run=False,
            )

    def test_m7_is_a_new_fresh_source_case_after_m6_without_reusing_its_blocked_state(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
        )
        m6_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m6_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M7-SOURCE-2026-09-11"),
        )

    def test_m8_is_an_independent_post_parser_repair_case(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
        )
        m7_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m7_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M8-SOURCE-2026-09-11"),
        )

    def test_m9_is_a_distinct_two_run_discovery_then_final_delivery_case(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME
        )
        m8_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m8_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M9-SOURCE-2026-09-11"),
        )
        self.assertEqual(
            tuple(item.value for item in acceptance._requested_deliverables_for_acceptance()),
            (
                "CASE_REVIEW_MEMO",
                "DEFENCE_STATEMENT",
                "EVIDENCE_CATALOGUE",
                "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
            ),
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "受控最终验收"
        ):
            acceptance._validate_acceptance_recovery_mode(
                resume_pre_model_intake=True,
                resume_pre_agent_inputs=False,
                resume_created_defence_run=False,
            )

    def test_m10_is_a_new_full_delivery_case_after_m9_evidence_scope_repair(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME
        )
        m9_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m9_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M10-SOURCE-2026-09-11"),
        )
        self.assertEqual(
            acceptance._M6_CATALOGUE_EVIDENCE_SCOPE,
            frozenset(
                {
                    ("F1", 2), ("F1", 5), ("F4", 30),
                    ("F4", 32), ("F6", 3), ("F6", 4),
                }
            ),
        )

    def test_m11_is_a_distinct_fresh_source_full_delivery_case_after_m10(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME
        )
        m10_matter = acceptance._acceptance_id("matter:firm-1")

        acceptance._select_acceptance_scenario(
            acceptance._M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME
        )

        self.assertNotEqual(m10_matter, acceptance._acceptance_id("matter:firm-1"))
        self.assertTrue(acceptance._uses_current_runtime_source_preflight())
        self.assertEqual(
            acceptance._response_source_scope_rule_identity(),
            ("PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE", "M11-SOURCE-2026-09-11"),
        )
        self.assertEqual(
            tuple(item.value for item in acceptance._requested_deliverables_for_acceptance()),
            (
                "CASE_REVIEW_MEMO",
                "DEFENCE_STATEMENT",
                "EVIDENCE_CATALOGUE",
                "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
            ),
        )


    def test_claim_response_receipt_is_distinct_from_its_claim(self):
        acceptance = self._load_acceptance_module()
        candidate = SimpleNamespace(
            object_type="CLAIM",
            object_id="claim-1",
            matter_id="matter-1",
        )
        scope = SimpleNamespace(
            object_type="CLAIM",
            object_id="claim-1",
            matter_id="matter-1",
        )
        response = SimpleNamespace(
            object_type="CLAIM_RESPONSE",
            object_id="claim-response-1",
            matter_id="matter-1",
        )
        acceptance._assert_claim_response_receipts(candidate, scope, response)
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "诉请回应回执不一致"
        ):
            acceptance._assert_claim_response_receipts(candidate, scope, candidate)

    def test_official_source_fetch_uses_the_registered_fetch_identity(self):
        acceptance = self._load_acceptance_module()
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.geturl.return_value = acceptance._OFFICIAL_SOURCE_URL
        response.status = 200
        response.headers.get_content_type.return_value = "text/html"
        body = ("<!doctype html><html><body>" + "\n".join(
            acceptance._OFFICIAL_SOURCE_REQUIRED_MARKERS
        ) + "</body></html>").encode("utf-8")
        response.read.return_value = body
        opener = Mock()
        opener.open.return_value = response
        with patch.object(
            acceptance,
            "_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256",
            sha256(body).hexdigest(),
        ), patch.object(
            acceptance, "_OFFICIAL_SOURCE_FROZEN_BYTES", len(body)
        ), patch.object(
            acceptance.urllib.request, "build_opener", return_value=opener
        ):
            acceptance._download_official_source()
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.get_header("User-agent"),
            acceptance._OFFICIAL_SOURCE_USER_AGENT,
        )
        self.assertIn("Fetch", acceptance._OFFICIAL_SOURCE_USER_AGENT)

    def test_frozen_source_rejects_markers_hidden_only_in_a_page_shell(self):
        acceptance = self._load_acceptance_module()
        body = (
            "<!doctype html><html><head><script>"
            + "\n".join(acceptance._OFFICIAL_SOURCE_REQUIRED_MARKERS)
            + "</script></head><body><div id='application'></div></body></html>"
        ).encode("utf-8")
        with patch.object(
            acceptance,
            "_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256",
            sha256(body).hexdigest(),
        ), patch.object(
            acceptance, "_OFFICIAL_SOURCE_FROZEN_BYTES", len(body)
        ):
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "可安全读取的可见正文"
            ):
                acceptance._validate_frozen_official_source_content(body)

    def test_existing_frozen_source_is_recovered_without_public_web_fetch(self):
        acceptance = self._load_acceptance_module()
        body = ("<!doctype html><html><body>" + "\n".join(
            acceptance._OFFICIAL_SOURCE_REQUIRED_MARKERS
        ) + "</body></html>").encode("utf-8")
        stored = SimpleNamespace(
            stored_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
            ledger_object_key="aa/bb/" + ("a" * 64) + ".lca",
            content_sha256=sha256(body).hexdigest(),
            byte_size=len(body),
        )
        objects = Mock()
        objects.find_existing_official_source.return_value = stored
        objects.read_official_source.return_value = (body, "text/html")

        with patch.object(
            acceptance,
            "_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256",
            sha256(body).hexdigest(),
        ), patch.object(
            acceptance, "_OFFICIAL_SOURCE_FROZEN_BYTES", len(body)
        ):
            recovered = acceptance._recover_frozen_official_source(
                objects, firm_id="firm-1", matter_id="matter-1"
            )

        self.assertEqual(recovered, (body, stored.stored_at, stored))
        objects.find_existing_official_source.assert_called_once()
        objects.read_official_source.assert_called_once_with(
            firm_id="firm-1",
            matter_id="matter-1",
            ledger_object_key=stored.ledger_object_key,
            expected_sha256=sha256(body).hexdigest(),
            expected_media_type="text/html",
        )

    def test_firm_frozen_source_recovery_copies_only_the_exact_verified_public_bytes(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
        )
        firm_id = "11111111-1111-4111-8111-111111111111"
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id=firm_id,
                actor_id="22222222-2222-4222-8222-222222222222",
            )
        )
        source_matter_id = "33333333-3333-4333-8333-333333333333"
        body = ("<!doctype html><html><body>" + "\n".join(
            acceptance._OFFICIAL_SOURCE_REQUIRED_MARKERS
        ) + "</body></html>").encode("utf-8")
        source_object = SimpleNamespace(
            content_sha256=sha256(body).hexdigest(),
            byte_size=len(body),
            media_type="text/html",
        )
        objects = Mock()
        objects.read_official_source.return_value = (body, "text/html")
        objects.put_official_source.return_value = source_object
        reference = acceptance._FirmFrozenOfficialSourceReference(
            matter_id=source_matter_id,
            retrieved_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        )
        composition = SimpleNamespace(
            settings=SimpleNamespace(app_postgres_dsn="postgresql://test")
        )
        digest = sha256(body).hexdigest()

        with patch.object(
            acceptance,
            "_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256",
            digest,
        ), patch.object(
            acceptance, "_OFFICIAL_SOURCE_FROZEN_BYTES", len(body)
        ), patch.object(
            acceptance,
            "_find_firm_frozen_official_source_reference",
            return_value=reference,
        ):
            recovered = acceptance._recover_firm_frozen_official_source(
                objects,
                composition=composition,
                identity=identity,
            )

        self.assertEqual(recovered, (body, reference.retrieved_at, source_object))
        objects.read_official_source.assert_called_once_with(
            firm_id=firm_id,
            matter_id=source_matter_id,
            ledger_object_key=f"{digest[:2]}/{digest[2:4]}/{digest}.lca",
            expected_sha256=digest,
            expected_media_type="text/html",
        )
        self.assertEqual(
            objects.put_official_source.call_args.kwargs["matter_id"],
            acceptance._matter_id(identity),
        )

    def test_m2_firm_frozen_source_recovery_copies_only_recent_exact_bytes(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        firm_id = "11111111-1111-4111-8111-111111111111"
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id=firm_id,
                actor_id="22222222-2222-4222-8222-222222222222",
            )
        )
        source_matter_id = "33333333-3333-4333-8333-333333333333"
        body = ("<!doctype html><html><body>" + "\n".join(
            acceptance._M1_OFFICIAL_SOURCE_REQUIRED_MARKERS
        ) + "</body></html>").encode("utf-8")
        digest = sha256(body).hexdigest()
        captured_at = datetime(2026, 9, 4, 14, 11, tzinfo=timezone.utc)
        source_object = SimpleNamespace(
            content_sha256=digest,
            byte_size=len(body),
            media_type="text/html",
        )
        objects = Mock()
        objects.read_official_source.return_value = (body, "text/html")
        objects.put_official_source.return_value = source_object
        reference = acceptance._FirmFrozenOfficialSourceReference(
            matter_id=source_matter_id,
            retrieved_at=captured_at,
        )
        composition = SimpleNamespace(
            settings=SimpleNamespace(app_postgres_dsn="postgresql://test")
        )

        with patch.object(
            acceptance,
            "_M1_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256",
            digest,
        ), patch.object(
            acceptance, "_M1_OFFICIAL_SOURCE_FROZEN_BYTES", len(body)
        ), patch.object(
            acceptance,
            "_find_firm_frozen_official_source_reference",
            return_value=reference,
        ), patch.object(
            acceptance,
            "_safe_now",
            return_value=captured_at + timedelta(hours=1),
        ):
            recovered = acceptance._recover_firm_frozen_official_source(
                objects,
                composition=composition,
                identity=identity,
            )

        self.assertEqual(recovered, (body, captured_at, source_object))
        objects.read_official_source.assert_called_once_with(
            firm_id=firm_id,
            matter_id=source_matter_id,
            ledger_object_key=f"{digest[:2]}/{digest[2:4]}/{digest}.lca",
            expected_sha256=digest,
            expected_media_type="text/html",
        )
        self.assertEqual(
            objects.put_official_source.call_args.kwargs["matter_id"],
            acceptance._matter_id(identity),
        )

    def test_v4_source_recovery_never_falls_back_to_public_web(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
        )
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id="11111111-1111-4111-8111-111111111111",
                actor_id="22222222-2222-4222-8222-222222222222",
            )
        )
        composition = SimpleNamespace(
            official_source_adapters=SimpleNamespace(objects=Mock())
        )
        composition.official_source_adapters.objects.find_existing_official_source.return_value = None
        with patch.object(
            acceptance,
            "_recover_firm_frozen_official_source",
            return_value=None,
        ), patch.object(acceptance, "_download_official_source") as download:
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "未重新访问公网或模型"
            ):
                acceptance._confirm_legal_context(
                    composition,
                    identity,
                    object(),
                    firm_frozen_source_recovery_only=True,
                )
        download.assert_not_called()

    def test_m2_source_recovery_never_falls_back_to_public_web(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id="11111111-1111-4111-8111-111111111111",
                actor_id="22222222-2222-4222-8222-222222222222",
            )
        )
        composition = SimpleNamespace(
            official_source_adapters=SimpleNamespace(objects=Mock())
        )
        composition.official_source_adapters.objects.find_existing_official_source.return_value = None
        with patch.object(
            acceptance,
            "_recover_firm_frozen_official_source",
            return_value=None,
        ), patch.object(acceptance, "_download_official_source") as download:
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "M2 预模型恢复找不到"
            ):
                acceptance._confirm_legal_context(
                    composition,
                    identity,
                    object(),
                    m2_firm_frozen_source_recovery_only=True,
                )
        download.assert_not_called()

    def test_m3_source_preflight_never_falls_back_to_public_web(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME
        )
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id="11111111-1111-4111-8111-111111111111",
                actor_id="22222222-2222-4222-8222-222222222222",
            )
        )
        composition = SimpleNamespace(
            official_source_adapters=SimpleNamespace(objects=Mock())
        )
        composition.official_source_adapters.objects.find_existing_official_source.return_value = None
        with patch.object(
            acceptance,
            "_recover_firm_frozen_official_source",
            return_value=None,
        ), patch.object(acceptance, "_download_official_source") as download:
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "M3 来源预检找不到"
            ):
                acceptance._confirm_legal_context(
                    composition,
                    identity,
                    object(),
                    m3_firm_frozen_source_preflight=True,
                )
        download.assert_not_called()

    def test_m1_source_recovery_never_falls_back_to_public_web(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id="11111111-1111-4111-8111-111111111111",
                actor_id="22222222-2222-4222-8222-222222222222",
            )
        )
        composition = SimpleNamespace(
            official_source_adapters=SimpleNamespace(objects=Mock())
        )
        composition.official_source_adapters.objects.find_existing_official_source.return_value = None
        with patch.object(acceptance, "_download_official_source") as download:
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "未重新访问公网或模型"
            ):
                acceptance._confirm_legal_context(
                    composition,
                    identity,
                    object(),
                    m1_frozen_source_recovery_only=True,
                )
        download.assert_not_called()

    def test_ambiguous_official_source_write_uses_only_read_only_recovery(self):
        acceptance = self._load_acceptance_module()
        objects = Mock()
        objects.put_official_source.side_effect = acceptance.OfficialSourceObjectStateUnknown(
            "indeterminate write"
        )
        expected = SimpleNamespace(content_sha256="a" * 64)
        objects.recover_official_source.return_value = expected

        result = acceptance._store_or_recover_official_source(
            objects,
            content=b"official source",
            firm_id="firm-1",
            matter_id="matter-1",
            content_sha256="a" * 64,
            content_media_type="text/html",
        )

        self.assertIs(result, expected)
        objects.put_official_source.assert_called_once()
        objects.recover_official_source.assert_called_once_with(
            firm_id="firm-1",
            matter_id="matter-1",
            content_sha256="a" * 64,
            content_media_type="text/html",
            byte_size=len(b"official source"),
        )

    def test_legal_stage_sanitizes_unclassified_failure_before_model_execution(self):
        acceptance = self._load_acceptance_module()

        def broken_stage():
            raise ValueError("private implementation detail")

        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked,
            "登记官方来源.*ValueError.*未继续模型运行",
        ) as captured:
            acceptance._run_legal_stage("登记官方来源", broken_stage)
        self.assertNotIn("private implementation detail", str(captured.exception))

    def test_resume_accepts_only_exact_pre_model_intake_prefix(self):
        acceptance = self._load_acceptance_module()
        pdf = acceptance.GeneratedFile(
            logical_code="F1",
            source_code="F1",
            file_name="法院送达材料.pdf",
            media_type="application/pdf",
            page_count=2,
            file_sha256="b" * 64,
        )
        image = acceptance.GeneratedFile(
            logical_code="F2a",
            source_code="F2",
            file_name="借条1_照片.jpg",
            media_type="image/jpeg",
            page_count=1,
            file_sha256="c" * 64,
        )
        generated = acceptance.GeneratedGoldenCase(
            schema_version="test",
            root="/test",
            sources_root="/test/sources",
            manifest_path="/test/manifest.json",
            spec_sha256="d" * 64,
            files=(pdf, image),
            ingest_order=(pdf.file_name, image.file_name),
        )
        prefix = (
            acceptance._PersistedIntakeRecord(
                file_name=pdf.file_name,
                content_sha256=pdf.file_sha256,
                page_count=pdf.page_count,
                route="EVIDENCE_ORIGINAL",
                completion_matter_version=2,
            ),
        )
        accepted = acceptance._validate_pre_model_resume_prefix(
            generated,
            current_matter_version=2,
            records=prefix,
            downstream_counts={"case_agent_runs": 0},
        )
        self.assertEqual(accepted, (acceptance._expected_input(pdf),))
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "离开纯材料接收阶段"
        ):
            acceptance._validate_pre_model_resume_prefix(
                generated,
                current_matter_version=2,
                records=prefix,
                downstream_counts={"case_agent_runs": 1},
            )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "不一致"
        ):
            acceptance._validate_pre_model_resume_prefix(
                generated,
                current_matter_version=2,
                records=(
                    acceptance._PersistedIntakeRecord(
                        file_name=pdf.file_name,
                        content_sha256="e" * 64,
                        page_count=pdf.page_count,
                        route="EVIDENCE_ORIGINAL",
                        completion_matter_version=2,
                    ),
                ),
                downstream_counts={"case_agent_runs": 0},
            )

    def test_resume_accepts_a_complete_material_checkpoint_before_agent_work(self):
        acceptance = self._load_acceptance_module()
        pdf = acceptance.GeneratedFile(
            logical_code="F1",
            source_code="F1",
            file_name="法院送达材料.pdf",
            media_type="application/pdf",
            page_count=2,
            file_sha256="a" * 64,
        )
        image = acceptance.GeneratedFile(
            logical_code="F2a",
            source_code="F2",
            file_name="借条1_照片.jpg",
            media_type="image/jpeg",
            page_count=1,
            file_sha256="b" * 64,
        )
        generated = acceptance.GeneratedGoldenCase(
            schema_version="test",
            root="/test",
            sources_root="/test/sources",
            manifest_path="/test/manifest.json",
            spec_sha256="c" * 64,
            files=(pdf, image),
            ingest_order=(pdf.file_name, image.file_name),
        )
        records = tuple(
            acceptance._PersistedIntakeRecord(
                file_name=item.file_name,
                content_sha256=item.file_sha256,
                page_count=item.page_count,
                route=acceptance._expected_input(item).route,
                completion_matter_version=index,
            )
            for index, item in enumerate((pdf, image), start=2)
        )
        self.assertEqual(
            acceptance._validate_pre_model_resume_prefix(
                generated,
                current_matter_version=3,
                records=records,
                downstream_counts={"case_agent_runs": 0},
            ),
            (acceptance._expected_input(pdf), acceptance._expected_input(image)),
        )

    def test_full_material_ledger_includes_pdf_and_image_originals(self):
        acceptance = self._load_acceptance_module()
        pdf = acceptance.GeneratedFile(
            logical_code="F1",
            source_code="F1",
            file_name="法院送达材料.pdf",
            media_type="application/pdf",
            page_count=2,
            file_sha256="a" * 64,
        )
        image = acceptance.GeneratedFile(
            logical_code="F2a",
            source_code="F2",
            file_name="借条1_照片.jpg",
            media_type="image/jpeg",
            page_count=1,
            file_sha256="b" * 64,
        )
        generated = acceptance.GeneratedGoldenCase(
            schema_version="test",
            root="/test",
            sources_root="/test/sources",
            manifest_path="/test/manifest.json",
            spec_sha256="c" * 64,
            files=(pdf, image),
            ingest_order=(pdf.file_name, image.file_name),
        )
        snapshot = SimpleNamespace(
            original_files=(
                {
                    "original_label": pdf.file_name,
                    "original_file_sha256": pdf.file_sha256,
                    "media_type": pdf.media_type,
                    "page_count": pdf.page_count,
                },
                {
                    "original_label": image.file_name,
                    "original_file_sha256": image.file_sha256,
                    "media_type": image.media_type,
                    "page_count": image.page_count,
                },
            ),
            pages=({}, {}, {}),
        )
        composition = SimpleNamespace(
            evidence_manifest_store=SimpleNamespace(
                get_evidence_snapshot=lambda **unused: snapshot
            )
        )
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id="11111111-1111-4111-8111-111111111111"
            )
        )
        accepted = (
            acceptance._expected_input(pdf),
            acceptance._expected_input(image),
        )
        self.assertEqual(
            asyncio.run(
                acceptance._admit_sources(
                    composition,
                    identity,
                    generated,
                    accepted_prefix=accepted,
                )
            ),
            accepted,
        )

    def test_resume_state_query_is_one_parameterized_select(self):
        acceptance = self._load_acceptance_module()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.transaction.return_value.__enter__.return_value = None
        connection.transaction.return_value.__exit__.return_value = False
        empty_rows = Mock()
        empty_rows.fetchall.return_value = []
        counts = Mock()
        counts.fetchone.return_value = {
            table: 0 for table in acceptance._PRE_MODEL_RESUME_TABLES
        }
        connection.execute.side_effect = [Mock(), Mock(), empty_rows, counts]
        composition = SimpleNamespace(
            settings=SimpleNamespace(app_postgres_dsn="postgresql://test")
        )
        identity = SimpleNamespace(
            actor=SimpleNamespace(
                firm_id="11111111-1111-4111-8111-111111111111",
                actor_id="22222222-2222-4222-8222-222222222222",
            )
        )
        with patch.object(acceptance.psycopg, "connect", return_value=connection):
            records, downstream_counts = acceptance._read_pre_model_resume_state(
                composition, identity
            )
        self.assertEqual(records, ())
        self.assertTrue(all(value == 0 for value in downstream_counts.values()))
        self.assertNotIn("case_agent_task_attempts", acceptance._PRE_MODEL_RESUME_TABLES)
        self.assertNotIn(
            "case_agent_external_submissions", acceptance._PRE_MODEL_RESUME_TABLES
        )
        query = connection.execute.call_args_list[2].args[0]
        self.assertTrue(query.lstrip().startswith("SELECT "))
        count_query = connection.execute.call_args_list[3].args[0]
        self.assertIn("count(goal_id)", count_query)
        self.assertIn("count(run_id)", count_query)
        self.assertEqual(
            len(connection.execute.call_args_list[3].args[1]),
            2 * len(acceptance._PRE_MODEL_RESUME_TABLES),
        )

    def test_pre_agent_resume_requires_an_exact_governed_input_prefix(self):
        acceptance = self._load_acceptance_module()
        sequence = acceptance._governed_input_command_sequence()
        receipts = tuple(
            (name, key, acceptance._MATERIAL_COMPLETE_VERSION + index)
            for index, (name, key) in enumerate(sequence, start=1)
        )
        counts = {table: 0 for table in acceptance._PRE_MODEL_RESUME_TABLES}
        counts.update(
            {
                "case_posture_profiles": 1,
                "case_facts": 5,
                "case_claims": 1,
                "case_claim_responses": 1,
            }
        )
        self.assertEqual(
            acceptance._validate_governed_input_resume_prefix(
                current_matter_version=acceptance._GOVERNED_INPUT_COMPLETE_VERSION,
                command_receipts=receipts,
                counts=counts,
            ),
            len(sequence),
        )
        invalid = list(receipts)
        invalid[-1] = (invalid[-1][0], "managed-defence-wrong", invalid[-1][2])
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "命令前缀"
        ):
            acceptance._validate_governed_input_resume_prefix(
                current_matter_version=acceptance._GOVERNED_INPUT_COMPLETE_VERSION,
                command_receipts=tuple(invalid),
                counts=counts,
            )

    def test_pre_agent_resume_blocks_after_legal_or_agent_progress(self):
        acceptance = self._load_acceptance_module()
        counts = {table: 0 for table in acceptance._PRE_MODEL_RESUME_TABLES}
        counts["case_legal_events"] = 1
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "离开可恢复"
        ):
            acceptance._validate_governed_input_resume_prefix(
                current_matter_version=acceptance._MATERIAL_COMPLETE_VERSION,
                command_receipts=(),
                counts=counts,
            )

    def test_m1_post_event_rule_recovery_requires_exact_ledger_prefix(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        governed_receipts = tuple(
            (name, key, acceptance._MATERIAL_COMPLETE_VERSION + index)
            for index, (name, key) in enumerate(
                acceptance._governed_input_command_sequence(), start=1
            )
        )
        counts = {table: 0 for table in acceptance._PRE_MODEL_RESUME_TABLES}
        counts.update(
            {
                "case_posture_profiles": 1,
                "case_facts": 5,
                "case_claims": 1,
                "case_claim_responses": 1,
                "case_legal_events": 1,
            }
        )
        acceptance._validate_m1_post_event_rule_resume_prefix(
            current_matter_version=acceptance._GOVERNED_INPUT_COMPLETE_VERSION + 2,
            command_receipts=governed_receipts,
            legal_command_receipts=acceptance._m1_post_event_rule_command_sequence(),
            counts=counts,
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "法源命令前缀"
        ):
            acceptance._validate_m1_post_event_rule_resume_prefix(
                current_matter_version=acceptance._GOVERNED_INPUT_COMPLETE_VERSION + 2,
                command_receipts=governed_receipts,
                legal_command_receipts=(),
                counts=counts,
            )

    def test_created_defence_run_resume_requires_an_exact_pre_model_boundary(self):
        acceptance = self._load_acceptance_module()
        receipts = tuple(
            (name, key, acceptance._MATERIAL_COMPLETE_VERSION + index)
            for index, (name, key) in enumerate(
                acceptance._governed_input_command_sequence(), start=1
            )
        )
        counts = {
            "case_posture_profiles": 1,
            "case_facts": 5,
            "case_claims": 1,
            "case_claim_responses": 1,
            "case_legal_events": 1,
            "case_legal_bundles": 1,
            "case_work_plans": 0,
            "case_agent_goals": 1,
            "case_agent_runs": 1,
        }
        run = SimpleNamespace(
            run_id="run-1",
            objective=acceptance._FIRST_RUN_OBJECTIVE,
            status="CREATED",
            version=1,
            snapshot_matter_version=acceptance._LEGAL_CONTEXT_COMPLETE_VERSION,
            progress_completed=0,
            progress_total=0,
            open_decision_count=0,
            open_approval_count=0,
            artifact_count=0,
            input_snapshot_status="CURRENT",
            failure_code=None,
            failure_message=None,
            current_work=None,
            active_plan_execution=False,
        )
        self.assertEqual(
            acceptance._validate_created_defence_run_resume_state(
                current_matter_version=acceptance._LEGAL_CONTEXT_COMPLETE_VERSION,
                command_receipts=receipts,
                counts=counts,
                run=run,
                expected_run_id=run.run_id,
            ),
            "CREATED",
        )
        waiting = SimpleNamespace(
            **{
                **run.__dict__,
                "status": "WAITING_APPROVAL",
                "version": 5,
                "progress_completed": 1,
                "progress_total": 2,
                "open_approval_count": 1,
                "artifact_count": 1,
                "current_work": SimpleNamespace(status="WAITING_APPROVAL"),
            }
        )
        self.assertEqual(
            acceptance._validate_created_defence_run_resume_state(
                current_matter_version=acceptance._LEGAL_CONTEXT_COMPLETE_VERSION,
                command_receipts=receipts,
                counts=counts,
                run=waiting,
                expected_run_id=run.run_id,
            ),
            "WAITING_APPROVAL",
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "模型调用前"
        ):
            acceptance._validate_created_defence_run_resume_state(
                current_matter_version=acceptance._LEGAL_CONTEXT_COMPLETE_VERSION,
                command_receipts=receipts,
                counts=counts,
                run=SimpleNamespace(**{**run.__dict__, "version": 2}),
                expected_run_id=run.run_id,
            )

    def test_waiting_defence_resume_requires_exact_zero_model_approval_gate(self):
        acceptance = self._load_acceptance_module()
        context_task = SimpleNamespace(
            spec=SimpleNamespace(
                task_id="context-task",
                skill=SimpleNamespace(skill_id="case_context_review"),
                capability=SimpleNamespace(network_policy=SimpleNamespace(value="DENY")),
            ),
            status=SimpleNamespace(value="SUCCEEDED"),
            attempt_count=1,
            receipts=(
                SimpleNamespace(
                    external_calls=0,
                    external_submission_state=SimpleNamespace(
                        value="NOT_APPLICABLE"
                    ),
                ),
            ),
        )
        analysis_task = SimpleNamespace(
            spec=SimpleNamespace(
                task_id="analysis-task",
                skill=SimpleNamespace(skill_id=acceptance.LAWYER_ANALYSIS_SKILL_ID),
                capability=SimpleNamespace(
                    network_policy=SimpleNamespace(value="EXACT_ALLOWLIST")
                ),
                dependency_ids=("context-task",),
            ),
            status=SimpleNamespace(value="WAITING_APPROVAL"),
            attempt_count=0,
            receipts=(),
        )
        state = SimpleNamespace(
            status=SimpleNamespace(value="WAITING_APPROVAL"),
            event_version=5,
            budget_usage=SimpleNamespace(external_calls=0),
            goal=SimpleNamespace(
                requested_deliverables=(
                    acceptance.AgentDeliverableKind.DEFENCE_STATEMENT,
                )
            ),
            tasks=(context_task, analysis_task),
            artifacts=(
                SimpleNamespace(
                    artifact_kind="CASE_CONTEXT_REVIEW_CANDIDATE",
                    managed_derivative=False,
                ),
            ),
        )
        control = SimpleNamespace(
            _store=SimpleNamespace(replay_run=Mock(return_value=state)),
            list_approvals=Mock(
                return_value=(
                    SimpleNamespace(approval_id="analysis-task", status="OPEN"),
                )
            ),
        )
        identity = SimpleNamespace(actor=object())
        with patch.object(acceptance, "_matter_id", return_value="matter-1"):
            with patch.object(acceptance, "_assert_task_boundary") as boundary:
                acceptance._validate_waiting_defence_analysis_approval_resume(
                    control, identity, run_id="run-1"
                )
        boundary.assert_called_once_with(
            control,
            identity,
            run_id="run-1",
            approval_id="analysis-task",
            phase="analysis",
        )
        state.budget_usage = SimpleNamespace(external_calls=1)
        with patch.object(acceptance, "_matter_id", return_value="matter-1"):
            with self.assertRaisesRegex(
                acceptance.ManagedDefenceAcceptanceBlocked, "零模型提交"
            ):
                acceptance._validate_waiting_defence_analysis_approval_resume(
                    control, identity, run_id="run-1"
                )

    def test_created_defence_run_resume_rejects_nonfixed_run_version(self):
        acceptance = self._load_acceptance_module()
        receipts = tuple(
            (name, key, acceptance._MATERIAL_COMPLETE_VERSION + index)
            for index, (name, key) in enumerate(
                acceptance._governed_input_command_sequence(), start=1
            )
        )
        counts = {
            "case_posture_profiles": 1,
            "case_facts": 5,
            "case_claims": 1,
            "case_claim_responses": 1,
            "case_legal_events": 1,
            "case_legal_bundles": 1,
            "case_work_plans": 0,
            "case_agent_goals": 1,
            "case_agent_runs": 1,
        }
        run = SimpleNamespace(
            run_id="run-1",
            objective=acceptance._FIRST_RUN_OBJECTIVE,
            status="CREATED",
            version=2,
            snapshot_matter_version=acceptance._LEGAL_CONTEXT_COMPLETE_VERSION,
            progress_completed=0,
            progress_total=0,
            open_decision_count=0,
            open_approval_count=0,
            artifact_count=0,
            input_snapshot_status="CURRENT",
            failure_code=None,
            failure_message=None,
            current_work=None,
            active_plan_execution=False,
        )
        with self.assertRaisesRegex(
            acceptance.ManagedDefenceAcceptanceBlocked, "模型调用前"
        ):
            acceptance._validate_created_defence_run_resume_state(
                current_matter_version=acceptance._LEGAL_CONTEXT_COMPLETE_VERSION,
                command_receipts=receipts,
                counts=counts,
                run=run,
                expected_run_id=run.run_id,
            )

    def test_resume_does_not_create_a_second_matter(self):
        acceptance = self._load_acceptance_module()
        composition = SimpleNamespace(
            api_dependencies=SimpleNamespace(
                case_agent_control_service=Mock(),
                dynamic_case_plan_service=Mock(),
            )
        )
        identity = object()
        with patch.object(acceptance, "_create_new_matter") as create_matter:
            with patch.object(acceptance, "_prepare_case", return_value=()) as prepare_case:
                with patch.object(
                    acceptance,
                    "_current_matter",
                    return_value=SimpleNamespace(matter_id="matter", version=1),
                ):
                    with self.assertRaisesRegex(acceptance.ManagedDefenceAcceptanceBlocked, "stop"):
                        with patch.object(
                            acceptance,
                            "_wait_for_run",
                            side_effect=acceptance.ManagedDefenceAcceptanceBlocked("stop"),
                        ):
                            acceptance._run_acceptance(
                                composition,
                                identity,
                                max_wait_seconds=30,
                                resume_pre_model_intake=True,
                            )
        create_matter.assert_not_called()
        prepare_case.assert_called_once_with(
            composition, identity, resume_pre_model_intake=True
        )

    def test_v4_source_recovery_does_not_create_a_second_matter(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
        )
        composition = SimpleNamespace(
            api_dependencies=SimpleNamespace(
                case_agent_control_service=Mock(),
                dynamic_case_plan_service=Mock(),
            )
        )
        identity = object()
        with patch.object(acceptance, "_create_new_matter") as create_matter:
            with patch.object(
                acceptance,
                "_prepare_case",
                side_effect=acceptance.ManagedDefenceAcceptanceBlocked("stop"),
            ) as prepare_case:
                with self.assertRaisesRegex(acceptance.ManagedDefenceAcceptanceBlocked, "stop"):
                    acceptance._run_acceptance(
                        composition,
                        identity,
                        max_wait_seconds=30,
                        resume_v4_pre_model_source=True,
                    )
        create_matter.assert_not_called()
        prepare_case.assert_called_once_with(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_v4_pre_model_source=True,
        )

    def test_m1_source_recovery_does_not_create_a_second_matter(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        composition = SimpleNamespace(
            api_dependencies=SimpleNamespace(
                case_agent_control_service=Mock(),
                dynamic_case_plan_service=Mock(),
            )
        )
        identity = object()
        with patch.object(acceptance, "_create_new_matter") as create_matter:
            with patch.object(
                acceptance,
                "_prepare_case",
                side_effect=acceptance.ManagedDefenceAcceptanceBlocked("stop"),
            ) as prepare_case:
                with self.assertRaisesRegex(acceptance.ManagedDefenceAcceptanceBlocked, "stop"):
                    acceptance._run_acceptance(
                        composition,
                        identity,
                        max_wait_seconds=30,
                        resume_m1_pre_model_source=True,
                    )
        create_matter.assert_not_called()
        prepare_case.assert_called_once_with(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_m1_pre_model_source=True,
        )

    def test_m1_post_event_rule_recovery_does_not_create_a_second_matter(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        composition = SimpleNamespace(
            api_dependencies=SimpleNamespace(
                case_agent_control_service=Mock(),
                dynamic_case_plan_service=Mock(),
            )
        )
        identity = object()
        with patch.object(acceptance, "_create_new_matter") as create_matter:
            with patch.object(
                acceptance,
                "_prepare_case",
                side_effect=acceptance.ManagedDefenceAcceptanceBlocked("stop"),
            ) as prepare_case:
                with self.assertRaisesRegex(acceptance.ManagedDefenceAcceptanceBlocked, "stop"):
                    acceptance._run_acceptance(
                        composition,
                        identity,
                        max_wait_seconds=30,
                        resume_m1_post_event_rule=True,
                    )
        create_matter.assert_not_called()
        prepare_case.assert_called_once_with(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_m1_post_event_rule=True,
        )

    def test_m2_source_recovery_does_not_create_a_second_matter(self):
        acceptance = self._load_acceptance_module()
        acceptance._select_acceptance_scenario(
            acceptance._M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
        )
        composition = SimpleNamespace(
            api_dependencies=SimpleNamespace(
                case_agent_control_service=Mock(),
                dynamic_case_plan_service=Mock(),
            )
        )
        identity = object()
        with patch.object(acceptance, "_create_new_matter") as create_matter:
            with patch.object(
                acceptance,
                "_prepare_case",
                side_effect=acceptance.ManagedDefenceAcceptanceBlocked("stop"),
            ) as prepare_case:
                with self.assertRaisesRegex(acceptance.ManagedDefenceAcceptanceBlocked, "stop"):
                    acceptance._run_acceptance(
                        composition,
                        identity,
                        max_wait_seconds=30,
                        resume_m2_pre_model_source=True,
                    )
        create_matter.assert_not_called()
        prepare_case.assert_called_once_with(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_m2_pre_model_source=True,
        )

    def test_managed_api_runtime_mounts_only_the_authenticated_synthetic_spec(self):
        dockerfile = (
            REPOSITORY_ROOT / "deployment/local-managed-test/api.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("RUN mkdir -p /app/docs", dockerfile)
        self.assertNotIn("COPY docs /app/docs", dockerfile)
        self.assertNotIn("COPY docs/GOLDEN_CASE_SYNTHETIC.md", dockerfile)
        dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("docs/", dockerignore)
        compose = (
            REPOSITORY_ROOT / "deployment/local-managed-test/compose.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "../../docs/GOLDEN_CASE_SYNTHETIC.md:/app/docs/GOLDEN_CASE_SYNTHETIC.md:ro",
            compose,
        )


if __name__ == "__main__":
    unittest.main()
