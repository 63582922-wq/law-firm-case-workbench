from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from pypdf import PdfReader

from case_kernel.golden_defense_vertical_slice import (
    ACCEPT_GOLDEN_RECOMMENDATIONS,
    AppendOnlyAuditLog,
    GoldenVerticalSliceBlocked,
    open_current_submission,
    run_golden_vertical_slice,
    validate_bound_approval,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DefenceVerticalSliceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = TemporaryDirectory()
        cls.result = run_golden_vertical_slice(
            Path(cls.temporary.name) / "run",
            project_root=PROJECT_ROOT,
            synthetic_decision=ACCEPT_GOLDEN_RECOMMENDATIONS,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_complete_authoritative_slice_reports_all_six_measured_metrics(self) -> None:
        metrics = self.result.metrics
        dedup = metrics["page_deduplication"]
        self.assertEqual((dedup["input_pages"], dedup["canonical_pages"]), (88, 75))
        self.assertEqual(dedup["true_positive_exact_page_groups"], 13)
        self.assertEqual(dedup["missed_exact_page_groups"], 0)
        self.assertEqual(dedup["false_positive_exact_page_groups"], 0)
        self.assertEqual(dedup["false_removals"], 0)
        self.assertTrue(dedup["f3_near_pair_preserved"])

        extraction = metrics["information_extraction"]
        self.assertEqual((extraction["tp"], extraction["fp"], extraction["fn"]), (380, 0, 0))
        self.assertEqual((extraction["precision"], extraction["recall"]), (1.0, 1.0))
        self.assertEqual(extraction["source_ref_coverage"], 1.0)
        for kind in ("name", "nickname", "wechat_id", "bank_tail", "mobile_tail", "amount", "currency", "occurred_at", "source_page"):
            self.assertEqual(extraction["per_field"][kind]["precision"], 1.0)
            self.assertEqual(extraction["per_field"][kind]["recall"], 1.0)

        calculation = metrics["deterministic_calculation"]
        self.assertTrue(calculation["matching_to_cent"])
        self.assertEqual(calculation["mismatched_fields"], 0)
        self.assertEqual(calculation["scenario_count"], 4)
        self.assertEqual(calculation["checked_fields"], 341)
        self.assertEqual(calculation["scenarios"]["S-A-1"]["total_principal"], "310638.59")
        self.assertEqual(calculation["scenarios"]["S-A-1"]["total_interest_arrears"], "123297.31")

        consistency = metrics["cross_document_consistency"]
        self.assertEqual(consistency["detected_conflicts"], 4)
        self.assertEqual(consistency["missed_conflicts"], 0)
        self.assertEqual(consistency["false_positive_conflicts"], 0)
        self.assertEqual(consistency["localized_to_source"], 4)

        invalidation = metrics["invalidation_propagation"]
        self.assertEqual(invalidation["decision_mutations_tested"], 2)
        self.assertEqual(invalidation["actual_downstream_invalidations"], 14)
        self.assertEqual(invalidation["expected_downstream_invalidations"], 14)
        self.assertEqual(invalidation["old_packages_blocked"], 2)
        self.assertEqual(invalidation["current_pointers_cleared"], 2)
        self.assertEqual(invalidation["calculation_hashes_changed"], 2)

        provenance = metrics["provenance"]
        self.assertEqual(provenance["coverage"], 1.0)
        self.assertEqual(provenance["fully_traceable_objects"], provenance["traceable_objects_total"])
        self.assertEqual(provenance["source_refs_broken"], 0)
        self.assertEqual(provenance["calculation_amount_claims_checked"], 204)
        self.assertTrue(provenance["calculation_replay_match"])

    def test_exact_reviewed_pdf_bytes_are_the_only_two_locked_zip_members(self) -> None:
        current = json.loads(
            (self.result.output_root / "state" / "current_submission.json").read_text(encoding="utf-8")
        )
        approval = json.loads(self.result.approval_path.read_text(encoding="utf-8"))
        candidate = json.loads(
            (self.result.output_root / "candidates" / "candidate_manifest.json").read_text(encoding="utf-8")
        )
        internal = json.loads(self.result.internal_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(approval["candidate_hash"], candidate["candidate_hash"])
        self.assertEqual(current["candidate_hash"], candidate["candidate_hash"])
        self.assertEqual(internal["bundle"]["input_hash"], candidate["candidate_hash"])
        self.assertEqual(internal["bundle"]["approval_hash"], approval["approval_hash"])
        self.assertTrue(open_current_submission(self.result.output_root / "state", self.result.output_root / "audit"))
        with ZipFile(self.result.locked_zip_path) as archive:
            self.assertEqual(
                archive.namelist(),
                ["01_民事答辩要点底稿.pdf", "02_证据目录底稿.pdf"],
            )
            for member, item in zip(archive.namelist(), candidate["components"]):
                payload = archive.read(member)
                self.assertEqual(sha256(payload).hexdigest(), item["sha256"])
                self.assertGreaterEqual(len(PdfReader(self.result.output_root / "candidates" / member).pages), 1)

    def test_one_batch_approval_is_after_candidate_and_before_lock(self) -> None:
        events = AppendOnlyAuditLog(self.result.output_root / "audit").replay()
        types = [item.event_type for item in events]
        self.assertLess(types.index("CANDIDATE_CREATED"), types.index("HUMAN_DECISION_RECORDED"))
        self.assertLess(types.index("HUMAN_DECISION_RECORDED"), types.index("BUNDLE_COMPILED"))
        self.assertLess(types.index("BUNDLE_COMPILED"), types.index("SUBMISSION_LOCKED"))
        self.assertEqual(types.count("HUMAN_DECISION_RECORDED"), 1)
        self.assertEqual(self.result.metrics["approval"]["decision_count"], 10)

    def test_real_invalidation_trials_change_numbers_and_block_old_current(self) -> None:
        trials = self.result.metrics["invalidation_propagation"]["trials"]
        by_id = {item["decision_id"]: item for item in trials}
        self.assertEqual(by_id["D06_CASH_SWITCH"]["new_L1_principal"], "102824.12")
        self.assertEqual(by_id["D07_U2_SWITCH"]["new_L1_principal"], "163034.19")
        for trial in trials:
            self.assertTrue(trial["package_open_before_change"])
            self.assertTrue(trial["old_package_open_blocked"])
            self.assertTrue(trial["old_package_bytes_preserved"])
            self.assertTrue(trial["current_pointer_cleared"])
            self.assertEqual(trial["invalidated_count"], 7)

    def test_originals_are_read_only_and_gold_is_separate_from_material_input(self) -> None:
        gold = json.loads(self.result.gold_path.read_text(encoding="utf-8"))
        source_root = self.result.output_root / "materials" / "sources"
        self.assertNotIn("evaluator_gold", str(source_root))
        self.assertEqual(len(gold["source_files"]), 11)
        for item in gold["source_files"]:
            path = source_root / item["file_name"]
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), item["file_sha256"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_review_packet_or_approval_tampering_is_rejected(self) -> None:
        packet = json.loads(self.result.review_packet_path.read_text(encoding="utf-8"))
        approval = json.loads(self.result.approval_path.read_text(encoding="utf-8"))
        choices = approval["selected_choices"]
        tampered_packet = json.loads(json.dumps(packet))
        tampered_packet["decisions"][0]["options"][0]["consequence"] = "tampered"
        with self.assertRaisesRegex(GoldenVerticalSliceBlocked, "review packet hash"):
            validate_bound_approval(
                approval,
                candidate_hash=approval["candidate_hash"],
                review_packet=tampered_packet,
                expected_choices=choices,
            )
        tampered_approval = {**approval, "candidate_hash": "f" * 64}
        with self.assertRaisesRegex(GoldenVerticalSliceBlocked, "approval contents"):
            validate_bound_approval(
                tampered_approval,
                candidate_hash="f" * 64,
                review_packet=packet,
                expected_choices=choices,
            )

    def test_missing_explicit_approval_never_creates_a_locked_package(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "unapproved"
            with self.assertRaisesRegex(GoldenVerticalSliceBlocked, "no explicit synthetic approval"):
                run_golden_vertical_slice(
                    root,
                    project_root=PROJECT_ROOT,
                    synthetic_decision="",
                )
            self.assertFalse((root / "state" / "current_submission.json").exists())
            self.assertFalse((root / "locked_submission").exists())
            self.assertTrue((root / "candidates" / "candidate_manifest.json").is_file())

    def test_public_one_command_runs_from_fresh_output_and_prints_six_sections(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "cli-run"
            completed = subprocess.run(
                [
                    str(PROJECT_ROOT / "scripts" / "run_defense_vertical_slice"),
                    "--approve-synthetic-recommendation",
                    "--output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            for key in (
                "page_deduplication",
                "information_extraction",
                "deterministic_calculation",
                "cross_document_consistency",
                "invalidation_propagation",
                "provenance",
            ):
                self.assertIn(key, payload)
            self.assertTrue(Path(payload["locked_submission"]).is_file())
            self.assertTrue(Path(payload["run_report"]).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
