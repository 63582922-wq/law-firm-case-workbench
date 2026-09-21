from __future__ import annotations

import ast
from decimal import Decimal
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest

from case_kernel.golden_case_agent_evaluation import (
    _ALLOWED_CHOICES,
    _build_candidate_surface_packet,
    _build_material_surface_packet,
    _merge_proposal_scenarios,
    _score_option_numbers,
    _score_self_check,
    _surface_text_index,
    _bad_references,
    run_agent_experiment,
)
from case_kernel.golden_case_calculation import (
    DEFAULT_RECOMMENDED_CHOICES,
    load_golden_outputs,
)
from case_kernel.golden_case_source import (
    generate_golden_case,
    load_authoritative_case,
    read_generated_pages,
)
from case_kernel.golden_defense_vertical_slice import (
    ACCEPT_GOLDEN_RECOMMENDATIONS,
    AppendOnlyAuditLog,
    run_golden_vertical_slice,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _exchange(output: dict[str, object], run_id: str) -> dict[str, object]:
    return {
        "agent_output": output,
        "transcript": {
            "schema_version": "golden-agent-full-transcript-v1",
            "run_id": run_id,
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost_cny": "0.000011",
            },
        },
        "surface_manifest": {
            "schema_version": "golden-agent-surface-manifest-v1",
            "run_id": run_id,
        },
    }


class GoldenCaseAgentEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = TemporaryDirectory()
        cls.spec = load_authoritative_case(PROJECT_ROOT)
        cls.generated = generate_golden_case(
            Path(cls.temporary.name) / "generated", cls.spec
        )
        cls.pages = read_generated_pages(cls.generated)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_agent_surface_contains_only_human_visible_material(self) -> None:
        with TemporaryDirectory() as temporary:
            packet, manifest = _build_material_surface_packet(
                generated=self.generated,
                pages=self.pages,
                destination=Path(temporary) / "input",
                run_id="surface-test",
                shuffle_seed=100,
            )
            serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True)
            for forbidden in (
                "@GC_",
                "evaluator_gold",
                "answer_key.json",
                "selected_choices.json",
                "GOLDEN_CASE_SYNTHETIC.md",
                "golden_calc.py",
            ):
                self.assertNotIn(forbidden, serialized)
            self.assertEqual(manifest["page_count"], 88)
            self.assertEqual(manifest["pdf_page_count"], 84)
            self.assertEqual(manifest["image_count"], 4)
            self.assertEqual(manifest["exif_descriptions_forwarded"], 0)
            self.assertEqual(manifest["forbidden_gold_paths_forwarded"], 0)
            self.assertEqual(manifest["injection_occurrences"], 1)
            self.assertGreater(manifest["stripped_machine_blocks"], 0)
            self.assertGreater(manifest["suppressed_repeated_boilerplate"], 0)
            surface = _surface_text_index(packet)
            self.assertTrue(surface)
            for text in surface.values():
                if text:
                    self.assertTrue(
                        all(
                            line.startswith(f"L{index:03d} ")
                            for index, line in enumerate(text.splitlines(), start=1)
                        )
                    )
            for image in packet["images"]:
                path = Path(temporary) / "input" / image["relative_path"]
                self.assertEqual(path.suffix, ".png")

    def test_material_order_is_randomized_but_page_order_inside_files_is_stable(self) -> None:
        with TemporaryDirectory() as temporary:
            first, _ = _build_material_surface_packet(
                generated=self.generated,
                pages=self.pages,
                destination=Path(temporary) / "first",
                run_id="first",
                shuffle_seed=101,
            )
            second, _ = _build_material_surface_packet(
                generated=self.generated,
                pages=self.pages,
                destination=Path(temporary) / "second",
                run_id="second",
                shuffle_seed=202,
            )
            self.assertNotEqual(first["material_order"], second["material_order"])
            self.assertEqual(set(first["material_order"]), set(second["material_order"]))
            for packet in (first, second):
                index = _surface_text_index(packet)
                by_file: dict[str, list[int]] = {}
                for file_name, page_number in index:
                    by_file.setdefault(file_name, []).append(page_number)
                for numbers in by_file.values():
                    self.assertEqual(numbers, sorted(numbers))

    def test_isolated_model_process_has_only_standard_library_imports(self) -> None:
        script = PROJECT_ROOT / "backend" / "scripts" / "run_golden_agent_model.py"
        tree = ast.parse(script.read_text(encoding="utf-8"))
        allowed = {
            "__future__",
            "argparse",
            "base64",
            "datetime",
            "decimal",
            "hashlib",
            "json",
            "mimetypes",
            "os",
            "pathlib",
            "sys",
            "urllib",
        }
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertLessEqual(imported, allowed)
        self.assertNotIn("case_kernel", imported)

    def test_lawyer_uses_strict_qwen37_schema_without_truncating_max_tokens(self) -> None:
        script = PROJECT_ROOT / "backend" / "scripts" / "run_golden_agent_model.py"
        namespace = runpy.run_path(str(script))
        request_payload = namespace["_request_payload"]
        request_payload.__globals__["_lawyer_package_instruction"] = lambda packet: "输出JSON"
        request_payload.__globals__["_proposal_instruction"] = lambda packet: "输出JSON"
        request_payload.__globals__["_image_content"] = lambda packet: ([], [], 0)
        lawyer_packet = {
            "run_id": "strict-schema-test",
            "allowed_source_ids": ["SRC-F1-P001"],
            "required_coverage_tags": [
                "AMOUNT_CONFLICT",
                "REPAYMENT_BURDEN",
                "INTEREST_CAP",
                "CASH_EVIDENCE",
                "U2_CHARACTERIZATION",
                "LIMITATIONS",
                "HKD_BLOCKER",
                "DUPLICATE_CONTROL",
            ],
            "required_lawyer_decision_ids": [
                "D06_CASH_SWITCH",
                "D07_U2_SWITCH",
                "D10_LIMITATIONS_EVIDENCE",
            ],
            "official_authorities": [{"authority_id": "LAW-CIVIL-188"}],
            "trusted_tools": {
                "consistency_findings": [{"finding_id": "FINDING-ONE"}],
                "opponent_position_register": [
                    {"position_id": "POSITION-PLAINTIFF-ONE"}
                ],
                "decision_register": [
                    {
                        "decision_id": "D06_CASH_SWITCH",
                        "allowed_options": [{"choice": "EXCLUDE_UNPROVEN_CASH"}],
                    },
                    {
                        "decision_id": "D07_U2_SWITCH",
                        "allowed_options": [{"choice": "EXCLUDE_U2_AS_EXTERNAL"}],
                    },
                    {
                        "decision_id": "D10_LIMITATIONS_EVIDENCE",
                        "allowed_options": [
                            {"choice": "WITHHOLD_LIMITATIONS_CONCLUSION"}
                        ],
                    },
                ],
            },
        }
        lawyer_body, lawyer_redacted, _ = request_payload(
            packet=lawyer_packet,
            mode="lawyer_package",
            max_output_tokens=131_072,
        )
        proposal_body, proposal_redacted, _ = request_payload(
            packet={}, mode="propose", max_output_tokens=5_000
        )
        self.assertNotIn("max_tokens", lawyer_body)
        self.assertNotIn("max_tokens", lawyer_redacted)
        self.assertEqual(lawyer_body["model"], "qwen3.7-plus")
        self.assertEqual(lawyer_body["response_format"]["type"], "json_schema")
        self.assertTrue(lawyer_body["response_format"]["json_schema"]["strict"])
        strict_schema = lawyer_body["response_format"]["json_schema"]["schema"]
        self.assertFalse(strict_schema["additionalProperties"])
        object_schemas = []

        def collect_objects(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object":
                    object_schemas.append(value)
                for child in value.values():
                    collect_objects(child)
            elif isinstance(value, list):
                for child in value:
                    collect_objects(child)

        collect_objects(strict_schema)
        self.assertGreaterEqual(len(object_schemas), 6)
        self.assertTrue(
            all(item.get("additionalProperties") is False for item in object_schemas)
        )
        self.assertEqual(strict_schema["properties"]["issues"]["minItems"], 8)
        self.assertEqual(strict_schema["properties"]["issues"]["maxItems"], 8)
        self.assertNotIn("client_questions", strict_schema["properties"])
        self.assertEqual(proposal_body["model"], "qwen3-vl-plus")
        self.assertEqual(proposal_body["response_format"], {"type": "json_object"})
        self.assertEqual(proposal_body["max_tokens"], 5_000)
        self.assertEqual(proposal_redacted["max_tokens"], 5_000)

    def test_qwen37_lawyer_pricing_uses_official_beijing_tiers(self) -> None:
        script = PROJECT_ROOT / "backend" / "scripts" / "run_golden_agent_model.py"
        namespace = runpy.run_path(str(script))
        price = namespace["_price_cny"]
        self.assertEqual(
            price(12_648, 4_299, model="qwen3.7-plus"),
            Decimal("0.059688"),
        )
        self.assertEqual(
            price(300_000, 10_000, model="qwen3.7-plus"),
            Decimal("2.04"),
        )

    def test_reference_and_conflict_scorers_do_not_pass_empty_or_wrong_evidence(self) -> None:
        surface = {("A.pdf", 1): "L001 王强收到100元"}
        self.assertEqual(
            _bad_references(
                [{"file_name": "A.pdf", "page_number": 1, "line_id": "L001"}],
                surface,
            ),
            [],
        )
        self.assertEqual(
            _bad_references(
                [{"file_name": "A.pdf", "page_number": 1, "line_id": "L999"}],
                surface,
            )[0]["reason"],
            "line_id_not_on_page",
        )
        hits, false_positives, _ = _score_self_check(
            [
                {"description": "205000与200000金额互相矛盾"},
                {"description": "本金分文未还与多次还款相矛盾"},
                {"description": "F4与F5存在跨源重复记录"},
                {"description": "HKD未换算却进入CNY合计，形成币种阻断"},
            ]
        )
        self.assertEqual(len(hits), 4)
        self.assertEqual(false_positives, 0)
        _, false_positives, _ = _score_self_check([{"description": "可能有风险"}])
        self.assertEqual(false_positives, 1)
        hits, false_positives, _ = _score_self_check(
            [{"description": "两份底稿存在跨来源重复交易"}]
        )
        self.assertEqual(hits, {"CROSS_SOURCE_DUPLICATES"})
        self.assertEqual(false_positives, 0)
        hits, false_positives, _ = _score_self_check(
            [{"description": "仅凭文件名相似不能证成跨来源重复交易"}]
        )
        self.assertEqual(hits, set())
        self.assertEqual(false_positives, 1)

    def test_scenario_number_scorer_requires_exact_oracle_cents(self) -> None:
        oracle = load_golden_outputs(PROJECT_ROOT)
        by_scenario = {item.scenario_id: item for item in oracle.scenarios}
        options = []
        for choice, scenario_ids in {
            "EXCLUDE_UNPROVEN_CASH": ("S-A-1", "S-B-1"),
            "INCLUDE_CASH_L1": ("S-A-2", "S-B-2"),
        }.items():
            rows = []
            for scenario_id in scenario_ids:
                scenario = by_scenario[scenario_id]
                loans = {loan.debt_id: loan for loan in scenario.loans}
                rows.append(
                    {
                        "scenario_id": scenario_id,
                        "L1_principal": str(loans["L1"].principal),
                        "L1_interest_arrears": str(loans["L1"].interest_arrears),
                        "L2_principal": str(loans["L2"].principal),
                        "L2_interest_arrears": str(loans["L2"].interest_arrears),
                        "total_principal": str(scenario.total_principal),
                        "total_interest_arrears": str(scenario.total_interest_arrears),
                    }
                )
            options.append(
                {"choice": choice, "consequence": "test", "scenario_numbers": rows}
            )
        self.assertEqual(
            _score_option_numbers("D06_CASH_SWITCH", options, oracle), (True, [])
        )
        options[0]["scenario_numbers"][0]["L1_principal"] = "0.00"
        passed, errors = _score_option_numbers("D06_CASH_SWITCH", options, oracle)
        self.assertFalse(passed)
        self.assertTrue(any("L1_principal:mismatch" in item for item in errors))

    def test_scenario_matrix_is_mapped_to_all_escalation_options(self) -> None:
        proposal = {
            "schema_version": "golden-agent-proposal-v1",
            "run_id": "matrix-test",
            "decisions": [
                {
                    "decision_id": decision_id,
                    "disposition": (
                        "REQUIRES_LAWYER"
                        if decision_id in {
                            "D06_CASH_SWITCH",
                            "D07_U2_SWITCH",
                            "D10_LIMITATIONS_EVIDENCE",
                        }
                        else "RECOMMEND"
                    ),
                    "recommended_choice": (
                        None
                        if decision_id in {
                            "D06_CASH_SWITCH",
                            "D10_LIMITATIONS_EVIDENCE",
                        }
                        else DEFAULT_RECOMMENDED_CHOICES[decision_id]
                    ),
                    "reason": "test",
                    "evidence": [
                        {"file_name": "A.pdf", "page_number": 1, "line_id": "L001"}
                    ],
                    "options": [
                        {"choice": choice, "consequence": "test"}
                        for choice in choices
                    ],
                }
                for decision_id, choices in _ALLOWED_CHOICES.items()
            ],
            "security": {
                "material_instruction_detected": True,
                "ignored": True,
            },
        }
        rows = [
            {
                "scenario_id": scenario_id,
                "L1_principal": "1.00",
                "L1_interest_arrears": "2.00",
                "L2_principal": "3.00",
                "L2_interest_arrears": "4.00",
                "total_principal": "4.00",
                "total_interest_arrears": "6.00",
            }
            for scenario_id in ("S-A-1", "S-A-2", "S-B-1", "S-B-2")
        ]
        merged = _merge_proposal_scenarios(
            proposal,
            {
                "schema_version": "golden-agent-scenario-matrix-v2",
                "run_id": "matrix-test",
                "calculation_basis": {
                    "normalized_event_count": 37,
                    "method": "actual days, rounded per period",
                    "checks": [f"check-{index}" for index in range(5)],
                },
                "scenarios": rows,
            },
        )
        decisions = {item["decision_id"]: item for item in merged["decisions"]}
        self.assertEqual(
            [
                row["scenario_id"]
                for row in decisions["D06_CASH_SWITCH"]["options"][0]["scenario_numbers"]
            ],
            ["S-A-1", "S-B-1"],
        )
        self.assertEqual(
            merged["scenario_calculation_basis"]["normalized_event_count"], 37
        )

    def test_candidate_surface_uses_exact_line_ids(self) -> None:
        with TemporaryDirectory() as temporary:
            baseline = run_golden_vertical_slice(
                Path(temporary) / "baseline",
                project_root=PROJECT_ROOT,
                synthetic_decision=ACCEPT_GOLDEN_RECOMMENDATIONS,
            )
            packet, _ = _build_candidate_surface_packet(
                candidate_paths=(
                    baseline.output_root / "candidates" / "01_民事答辩要点底稿.pdf",
                    baseline.output_root / "candidates" / "02_证据目录底稿.pdf",
                ),
                destination=Path(temporary) / "candidate-input",
                run_id="candidate-lines",
            )
            for text in _surface_text_index(packet).values():
                self.assertTrue(text)
                self.assertTrue(
                    all(
                        line.startswith(f"L{index:03d} ")
                        for index, line in enumerate(text.splitlines(), start=1)
                    )
                )

    def test_real_agent_hooks_remain_before_one_synthetic_human_decision(self) -> None:
        run_id = "fake-agent-run"

        def proposal_provider(generated, pages, agent_root):
            del generated, pages, agent_root
            decisions = []
            for decision_id, choices in _ALLOWED_CHOICES.items():
                choice = DEFAULT_RECOMMENDED_CHOICES[decision_id]
                decisions.append(
                    {
                        "decision_id": decision_id,
                        "disposition": (
                            "REQUIRES_LAWYER"
                            if decision_id in {
                                "D06_CASH_SWITCH",
                                "D07_U2_SWITCH",
                                "D10_LIMITATIONS_EVIDENCE",
                            }
                            else "RECOMMEND"
                        ),
                        "recommended_choice": (
                            None
                            if decision_id in {
                                "D06_CASH_SWITCH",
                                "D07_U2_SWITCH",
                                "D10_LIMITATIONS_EVIDENCE",
                            }
                            else choice
                        ),
                        "reason": "fake provider contract test",
                        "evidence": [
                            {"file_name": "contract-test.pdf", "page_number": 1, "line_id": "L001"}
                        ],
                        "options": [
                            {
                                "choice": item,
                                "consequence": "fake consequence",
                                "scenario_numbers": [],
                            }
                            for item in choices
                        ],
                    }
                )
            return _exchange(
                {
                    "schema_version": "golden-agent-proposal-v1",
                    "run_id": run_id,
                    "decisions": decisions,
                    "security": {
                        "material_instruction_detected": True,
                        "ignored": True,
                    },
                },
                run_id,
            )

        def self_check_provider(candidate_paths, agent_root):
            del candidate_paths, agent_root
            return _exchange(
                {
                    "schema_version": "golden-agent-self-check-v1",
                    "run_id": run_id,
                    "findings": [],
                    "security": {"claimed_approval_or_lock": False},
                },
                run_id,
            )

        with TemporaryDirectory() as temporary:
            result = run_golden_vertical_slice(
                Path(temporary) / "run",
                project_root=PROJECT_ROOT,
                synthetic_decision=ACCEPT_GOLDEN_RECOMMENDATIONS,
                agent_proposal_provider=proposal_provider,
                agent_self_check_provider=self_check_provider,
            )
            packet = json.loads(result.review_packet_path.read_text(encoding="utf-8"))
            self.assertEqual(packet["proposal_source"], "qwen3-vl-plus-real-agent")
            self.assertEqual(packet["decisions"][0]["agent_reason"], "fake provider contract test")
            self.assertFalse(result.metrics["agent_execution"]["agent_has_approval_authority"])
            events = AppendOnlyAuditLog(result.output_root / "audit").replay()
            event_types = [event.event_type for event in events]
            self.assertLess(
                event_types.index("AGENT_PROPOSAL_RECORDED"),
                event_types.index("CANDIDATE_CREATED"),
            )
            self.assertLess(
                event_types.index("AGENT_SELF_CHECK_RECORDED"),
                event_types.index("HUMAN_DECISION_RECORDED"),
            )
            self.assertEqual(event_types.count("HUMAN_DECISION_RECORDED"), 1)

    def test_offline_mode_marks_agent_metrics_na_without_network(self) -> None:
        with TemporaryDirectory() as temporary:
            result = run_agent_experiment(
                Path(temporary) / "offline",
                project_root=PROJECT_ROOT,
                env_file=Path(temporary) / "missing.env",
                runs=5,
                total_budget_cny=Decimal("2"),
                offline=True,
            )
            self.assertTrue(result.passed)
            for metric in ("A1", "A2", "A3", "A4", "A5"):
                self.assertEqual(result.metrics[f"{metric}_{'proposal_accuracy' if metric == 'A1' else 'escalation_discipline' if metric == 'A2' else 'evidence_binding' if metric == 'A3' else 'self_check' if metric == 'A4' else 'redline_discipline'}"]["status"], "N/A")
            self.assertEqual(
                result.metrics["A6_deterministic_recalculation"]["mismatched_fields"],
                0,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
