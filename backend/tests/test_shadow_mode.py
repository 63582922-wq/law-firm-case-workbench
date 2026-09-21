"""Shadow test mode acceptance tests: S1-S7 gates, adversarial cases and the
generic engine identity proof (docs/SHADOW_MODE_ACCEPTANCE.md)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from PIL import Image
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

from case_kernel.shadow_engine import (
    ShadowDebt,
    ShadowEngineConfig,
    ShadowEngineBlocked,
    ShadowRow,
    identity_proof,
    run_engine,
)
from case_kernel.shadow_mode import (
    AmountSanitizer,
    BlockingRecord,
    RequestLedger,
    ShadowBlocked,
    ShadowGateFailed,
    assert_no_formal_outputs,
    attempt_formal_lock,
    build_import_manifest,
    compare_expected_csv,
    expand_preflight_authorization,
    resolve_refs,
    run_shadow_case,
    scan_identifiers,
    validate_preflight,
)
from case_kernel.shadow_live_transport import _image_pixels_payload

PROJECT_ROOT = Path(__file__).resolve().parents[2]
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def make_pdf(path: Path, lines: list[str]) -> None:
    canvas = rl_canvas.Canvas(str(path), pagesize=(595, 842))
    canvas.setFont("STSong-Light", 12)
    y = 800
    for line in lines:
        canvas.drawString(60, y, line)
        y -= 20
    canvas.save()


def make_jpeg(path: Path, description: str) -> None:
    image = Image.new("RGB", (300, 200), "white")
    exif = Image.Exif()
    exif[270] = description
    image.save(path, format="JPEG", exif=exif)


ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_CHECK_CHARS = "10X98765432"


def id_check_digit(body_17: str) -> str:
    """GB 11643-1999 check digit for an 18-digit national ID number."""
    return ID_CHECK_CHARS[int(body_17) % 11]


def luhn_check_digit(body: str) -> str:
    total = 0
    for index, char in enumerate(reversed(body)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


ID_17 = "11010519680202431"
ID_18 = ID_17 + id_check_digit(ID_17)
MOBILE = "13800138000"
BANK_15 = "622202123456789"
BANK_16 = BANK_15 + luhn_check_digit(BANK_15)


def build_materials(root: Path, *, masked: bool = False) -> Path:
    materials = root / "materials"
    materials.mkdir()
    if masked:
        make_pdf(materials / "银行流水.pdf", [
            "2024-01-05 转账 300,000.00 CNY 借款 周建国→王强",
            "2024-07-01 转账 100,000.00 CNY 还借款 王强→周建国",
            "证件 110105********2431 手机 138****8000 银行卡 6222 **** **** 7890",
        ])
    else:
        make_pdf(materials / "银行流水.pdf", [
            "2024-01-05 转账 300,000.00 CNY 借款 周建国→王强",
            "2024-07-01 转账 100,000.00 CNY 还借款 王强→周建国",
            f"身份证 {ID_18} 手机 {MOBILE} 银行卡 {BANK_16}",
        ])
    make_jpeg(materials / "借条.jpg", "借条 300,000 元 月利率1% 2024-01-05")
    config = {
        "schema": "shadow-case-config-v1",
        "lpr_4x_monthly_rate": "0.01",
        "interest_cutoff": "2025-06-14",
        "debts": [
            {"debt_id": "L1", "principal": "300000.00",
             "disbursed_on": "2024-01-05", "agreed_monthly_rate": "0.01", "due_on": None},
        ],
    }
    (materials / "case_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return materials


def proposal_payload(materials_root: Path, *, bad_refs: bool = False,
                     narrative: str = "") -> dict:
    manifest_sha = sha256((materials_root / "银行流水.pdf").read_bytes()).hexdigest()
    rows = [
        {
            "row_id": "1", "date": "2024-01-05", "channel": "银行转账",
            "amount": "300000.00", "currency": "CNY", "direction": "周→王",
            "classification": "本金出借", "debt_id": "L1", "memo": "借款",
            "source_ref": {"file_sha256": manifest_sha, "page": 1,
                           "file_name": "银行流水.pdf"},
        },
        {
            "row_id": "2", "date": "2024-07-01", "channel": "银行转账",
            "amount": "100000.00", "currency": "CNY", "direction": "王→周",
            "classification": "还本", "debt_id": "L1", "memo": "还借款",
            "source_ref": {"file_sha256": manifest_sha, "page": 1,
                           "file_name": "银行流水.pdf"},
        },
    ]
    if bad_refs:
        rows[1]["source_ref"] = {"file_sha256": "f" * 64, "page": 99}
    return {
        "schema": "shadow-proposal-v1",
        "rows": rows,
        "narrative": narrative,
        "decisions": [
            {"decision_id": "D01", "title": "测试决策", "recommended": "确认",
             "reason": "依据银行流水第1页与借条照片形成证据链。", "options": [], "source_refs": []},
        ],
    }


class S1Tests(unittest.TestCase):
    def test_scan_identifiers_detects_all_three_kinds(self) -> None:
        findings = scan_identifiers(f"{ID_18} {MOBILE} {BANK_16}")
        kinds = sorted(item["pattern"] for item in findings)
        self.assertEqual(kinds, ["BANK_CARD", "ID_CARD", "MOBILE"])

    def test_scan_identifiers_ignores_masked_values(self) -> None:
        findings = scan_identifiers("110105********2431 138****8000 6222 **** **** 7890")
        self.assertEqual(findings, [])

    def test_s1_blocks_unmasked_materials(self) -> None:
        with TemporaryDirectory() as tmp:
            materials = build_materials(Path(tmp))
            with self.assertRaises(ShadowBlocked) as context:
                build_import_manifest(materials)
            self.assertIn("S1", str(context.exception))

    def test_s1_passes_masked_materials(self) -> None:
        with TemporaryDirectory() as tmp:
            materials = build_materials(Path(tmp), masked=True)
            entries, pages, findings = build_import_manifest(materials)
            self.assertEqual(findings, [])
            self.assertEqual(len(entries), 2)
            self.assertEqual(sum(item.page_count for item in entries), 2)


class ImportRecursionTests(unittest.TestCase):
    def test_recursive_import_with_nested_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            nested = materials / "法院送达资料" / "子目录"
            nested.mkdir(parents=True)
            make_pdf(nested / "嵌套文书.pdf", ["2024-02-01 转账 1,000.00 CNY 测试"])
            entries, pages, findings = build_import_manifest(materials)
            self.assertEqual(len(entries), 3)
            names = sorted(item.file_name for item in entries)
            self.assertIn("法院送达资料/子目录/嵌套文书.pdf", names)
            self.assertEqual(len(pages), 3)


class S2Tests(unittest.TestCase):
    def test_attempt_formal_lock_is_always_blocked(self) -> None:
        with self.assertRaises(ShadowBlocked):
            attempt_formal_lock(Path("."))

    def test_forbidden_artifacts_detected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assert_no_formal_outputs(root)  # empty tree passes
            (root / "locked_submission").mkdir()
            with self.assertRaises(ShadowGateFailed):
                assert_no_formal_outputs(root)
            root.joinpath("locked_submission").rmdir()
            (root / "current_submission.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ShadowGateFailed):
                assert_no_formal_outputs(root)


class S3Tests(unittest.TestCase):
    def test_resolve_refs_blocks_fabricated(self) -> None:
        with TemporaryDirectory() as tmp:
            materials = build_materials(Path(tmp), masked=True)
            entries, _, _ = build_import_manifest(materials)
            good = {"file_sha256": entries[0].sha256, "page": 1}
            bad = [
                {"file_sha256": "f" * 64, "page": 1},
                {"file_sha256": entries[0].sha256, "page": 99},
                {"file_sha256": entries[0].sha256},
            ]
            resolved, unresolved = resolve_refs([good, *bad], entries)
            self.assertEqual(len(resolved), 1)
            self.assertEqual(len(unresolved), 3)

    def test_resolve_refs_requires_hash_after_local_canonicalization(self) -> None:
        with TemporaryDirectory() as tmp:
            materials = build_materials(Path(tmp), masked=True)
            entries, _, _ = build_import_manifest(materials)
            _, unresolved = resolve_refs(
                [{"file_name": entries[0].file_name, "page": 1}], entries
            )
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["reason"], "file_sha256 is required")

    def test_s3_blocked_run_leaves_blocking_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            output = root / "run"
            proposal = proposal_payload(materials)
            good = proposal["rows"][0]["source_ref"]
            proposal["decisions"][0]["source_refs"] = [
                {"file_sha256": "f" * 64, "page": 1},            # 不存在文件
                {"file_sha256": good["file_sha256"], "page": 99},  # 越界页码
                {"file_sha256": "0" * 64, "page": 1},            # 错误哈希
            ]
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            outcome = run_shadow_case(materials=materials, output_root=output,
                                      proposal_file=proposal_path)
            self.assertEqual(outcome.exit_code, 0)
            self.assertTrue((output / "blocking_log.json").is_file())
            log = json.loads((output / "blocking_log.json").read_text(encoding="utf-8"))
            self.assertEqual(len(log), 3)  # 三条虚构引用全部被拦截
            visible = json.loads((output / "agent" / "proposal.json").read_text(encoding="utf-8"))
            self.assertEqual(visible["decisions"], [])

    def test_s3_replaces_agent_excerpt_with_local_page_text(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            output = root / "run"
            proposal = proposal_payload(materials)
            proposal["rows"][0]["excerpt"] = "模型自行改写的摘录"
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            outcome = run_shadow_case(materials=materials, output_root=output,
                                      proposal_file=proposal_path)
            self.assertEqual(outcome.exit_code, 0)
            visible = json.loads((output / "agent" / "proposal.json").read_text(encoding="utf-8"))
            self.assertNotEqual(visible["rows"][0]["excerpt"], "模型自行改写的摘录")
            self.assertEqual(visible["rows"][0]["excerpt_origin"], "LOCAL_PAGE_TEXT")

    def test_s3_blocks_when_no_agent_row_can_be_locally_anchored(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            output = root / "run"
            proposal = proposal_payload(materials)
            proposal["rows"] = [proposal["rows"][0]]
            proposal["rows"][0]["date"] = "2025-12-31"
            proposal["rows"][0]["memo"] = "页面没有这段文字"
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ShadowBlocked) as context:
                run_shadow_case(materials=materials, output_root=output,
                                proposal_file=proposal_path)
            self.assertIn("S3", str(context.exception))
            log = json.loads((output / "blocking_log.json").read_text(encoding="utf-8"))
            self.assertEqual(log[0]["gate"], "S3 引用硬拦截")


class S4Tests(unittest.TestCase):
    def test_sanitizer_blocks_engine_mismatched_amounts(self) -> None:
        sanitizer = AmountSanitizer({"310638.59", "100000.00"}, set())
        cleaned, blocked = sanitizer.sanitize("合计 280,000.00 元，已还 100,000.00 元")
        self.assertEqual(blocked, ["280000.00"])
        self.assertIn("100,000.00", cleaned)
        self.assertIn("[金额已拦截]", cleaned)

    def test_s4_blocked_run_for_agent_calculated_total(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            output = root / "run"
            proposal = proposal_payload(materials, narrative="我方合计主张 280,000.00 元。")
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ShadowBlocked) as context:
                run_shadow_case(materials=materials, output_root=output,
                                proposal_file=proposal_path)
            self.assertIn("S4", str(context.exception))
            log = json.loads((output / "blocking_log.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item["gate"] == "S4 金额纪律" for item in log))


class S5Tests(unittest.TestCase):
    def test_missing_preflight_field_blocked(self) -> None:
        with self.assertRaises(ShadowBlocked):
            validate_preflight({"purpose": "propose", "confirmed": "true"})

    def test_unconfirmed_preflight_blocked(self) -> None:
        with self.assertRaises(ShadowBlocked):
            validate_preflight({"purpose": "propose", "sent_fields": [], "provider": "x",
                                "model": "m", "region": "cn", "retention": "0",
                                "budget_cap_cny": "1"})

    def test_all_imported_preflight_expands_to_exact_manifest_files(self) -> None:
        with TemporaryDirectory() as tmp:
            materials = build_materials(Path(tmp), masked=True)
            entries, _, _ = build_import_manifest(materials)
            preflight = {
                "purpose": "propose", "sent_fields": {"all_imported_pages": True},
                "provider": "aliyun", "model": "qwen3-vl-plus", "region": "cn-beijing",
                "retention": "0", "budget_cap_cny": "2", "confirmed": "true",
            }
            expanded = expand_preflight_authorization(preflight, entries)
            self.assertEqual(expanded["sent_fields"]["page_files"],
                             [entry.file_name for entry in entries])

    def test_image_request_payload_strips_exif_before_model_send(self) -> None:
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "material.jpg"
            description = "metadata must stay local"
            make_jpeg(image_path, description)
            payload, mime, width, height = _image_pixels_payload(image_path)
            self.assertEqual(mime, "image/png")
            self.assertEqual((width, height), (300, 200))
            self.assertNotIn(description.encode("utf-8"), payload)
            with Image.open(io.BytesIO(payload)) as reloaded:
                self.assertEqual(reloaded.getexif().get(270), None)

    def test_dry_run_makes_no_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            output = root / "run"
            proposal = proposal_payload(materials)
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            outcome = run_shadow_case(materials=materials, output_root=output,
                                      proposal_file=proposal_path)
            ledger = json.loads((output / "request_ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger, [])
            s5 = [g for g in outcome.gate_statuses if g.gate.startswith("S5")][0]
            self.assertEqual(s5.status, "PASS")
            self.assertEqual(s5.numbers["calls"], 0)

    def test_missing_config_stops_after_source_bound_candidate_card(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            (materials / "case_config.json").unlink()
            proposal_path = root / "proposal.json"
            proposal_path.write_text(
                json.dumps(proposal_payload(materials), ensure_ascii=False), encoding="utf-8"
            )
            outcome = run_shadow_case(materials=materials, output_root=root / "run",
                                      proposal_file=proposal_path)
            self.assertEqual(outcome.exit_code, 2)
            candidate_path = root / "run" / "agent" / "case_config_candidate.json"
            self.assertTrue(candidate_path.is_file())
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertEqual(candidate["status"], "NEEDS_LAWYER_CONFIRMATION")
            self.assertEqual(len(candidate["debt_candidates"]), 1)
            self.assertNotIn("interest_cutoff", candidate)
            self.assertTrue((root / "run" / "shadow_report.md").is_file())
            self.assertFalse(any(path.suffix == ".zip" for path in (root / "run").rglob("*")))


class FakeTransport:
    """Records calls; honors preflight sent_fields page filtering (S5 OCR)."""

    def __init__(self, proposal: dict) -> None:
        self.proposal = proposal
        self.calls: list[dict] = []

    def run(self, preflight, pages, ledger) -> str:
        allowed = set(preflight.get("sent_fields", {}).get("page_files", []))
        sent_pages = [page for page in pages if page.file_name in allowed]
        self.calls.append({"sent_pages": [page.file_name for page in sent_pages]})
        ledger.append(purpose=preflight["purpose"], payload_sha256="fake", status="ok")
        return json.dumps(self.proposal, ensure_ascii=False)


class S5LiveTests(unittest.TestCase):
    def test_live_branch_preflight_and_ledger(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            output = root / "run"
            preflight = {
                "purpose": "propose", "sent_fields": {"page_files": ["银行流水.pdf"]},
                "provider": "aliyun", "model": "qwen3-vl-plus", "region": "cn-beijing",
                "retention": "0", "budget_cap_cny": "2", "confirmed": "true",
            }
            confirm = root / "confirm.json"
            confirm.write_text(json.dumps(preflight, ensure_ascii=False), encoding="utf-8")
            transport = FakeTransport(proposal_payload(materials))
            outcome = run_shadow_case(materials=materials, output_root=output,
                                      proposal_file=None, confirm_data_path=confirm,
                                      transport=transport)
            ledger = json.loads((output / "request_ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger), 2)  # preflight row + call row
            self.assertEqual(transport.calls[0]["sent_pages"], ["银行流水.pdf"])
            s5 = [g for g in outcome.gate_statuses if g.gate.startswith("S5")][0]
            self.assertEqual(s5.status, "PASS")

    def test_unauthorized_page_never_sent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            preflight = {
                "purpose": "propose", "sent_fields": {"page_files": ["银行流水.pdf"]},
                "provider": "aliyun", "model": "qwen3-vl-plus", "region": "cn-beijing",
                "retention": "0", "budget_cap_cny": "2", "confirmed": "true",
            }
            transport = FakeTransport(proposal_payload(materials))
            confirm = root / "confirm.json"
            confirm.write_text(json.dumps(preflight, ensure_ascii=False), encoding="utf-8")
            run_shadow_case(materials=materials, output_root=root / "run",
                            confirm_data_path=confirm, transport=transport)
            sent = " ".join(page for call in transport.calls for page in call["sent_pages"])
            self.assertIn("银行流水.pdf", sent)
            self.assertNotIn("借条.jpg", sent)  # 未授权页面未发送


class S6Tests(unittest.TestCase):
    def test_expected_csv_planted_mismatches_detected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            csv_path = root / "expected.csv"
            csv_path.write_text(
                "row_id,date,channel,amount,currency,direction,classification,debt_id,note\n"
                "1,2024-01-05,银行转账,300000.00,CNY,周→王,本金出借,L1,借款\n"
                "2,2024-07-01,银行转账,99999.00,CNY,王→周,还本,L1,金额故意改错\n"
                "3,2024-08-01,现金,1000.00,CNY,王→周,还本,L1,多余行\n",
                encoding="utf-8",
            )
            output = root / "run"
            proposal = proposal_payload(materials)
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            outcome = run_shadow_case(materials=materials, output_root=output,
                                      proposal_file=proposal_path, expected_csv=csv_path)
            report = (output / "shadow_report.md").read_text(encoding="utf-8")
            self.assertIn("mismatch=1", report)
            self.assertIn("missing=1", report)
            s6 = [g for g in outcome.gate_statuses if g.gate.startswith("S6")][0]
            self.assertEqual(s6.numbers["mismatch"], 1)
            self.assertEqual(s6.numbers["missing"], 1)


class EngineTests(unittest.TestCase):
    def test_identity_proof_reproduces_oracle_to_the_cent(self) -> None:
        proof = identity_proof(PROJECT_ROOT)
        self.assertEqual(proof["identity_proof"], "PASS", msg=str(proof))
        self.assertEqual(proof["mismatches"], [])
        self.assertGreaterEqual(proof["checked_fields"], 90)

    def test_undesignated_repayment_follows_due_order(self) -> None:
        config = ShadowEngineConfig(final_date=date(2025, 6, 14))
        debts = {
            "B": ShadowDebt("B", Decimal("100000"), date(2024, 1, 1), Decimal("0.01"),
                            due_on=date(2025, 1, 1)),
            "A": ShadowDebt("A", Decimal("100000"), date(2024, 1, 1), Decimal("0.01"),
                            due_on=date(2024, 6, 1)),
        }
        rows = [
            ShadowRow("1", date(2024, 1, 1), "银行", Decimal("100000"), "CNY", "出",
                      "本金出借", "A", "借A"),
            ShadowRow("2", date(2024, 1, 1), "银行", Decimal("100000"), "CNY", "出",
                      "本金出借", "B", "借B"),
            ShadowRow("3", date(2024, 7, 1), "银行", Decimal("50000"), "CNY", "还",
                      "还本", None, "未指定"),
        ]
        result = run_engine(rows, debts, config)
        # A (due 2024-06-01) receives the undesignated repayment first;
        # 先息后本: 100,000 at 1%/month for 182 days = 6,066.67 interest,
        # so 43,933.33 goes to principal.
        self.assertEqual(result.loan("A").principal, Decimal("56066.67"))
        # cutoff-date accrual on the reduced principal 2024-07-01..2025-06-14
        self.assertEqual(result.loan("A").interest_arrears, Decimal("6503.73"))
        self.assertEqual(result.loan("B").principal, Decimal("100000.00"))

    def test_non_cny_row_is_blocked_from_calculation(self) -> None:
        config = ShadowEngineConfig(final_date=date(2025, 6, 14))
        debts = {
            "A": ShadowDebt("A", Decimal("100000"), date(2024, 1, 1), Decimal("0.01")),
        }
        rows = [
            ShadowRow("1", date(2024, 1, 1), "银行", Decimal("100000"), "CNY", "出",
                      "本金出借", "A", "借A"),
            ShadowRow("2", date(2024, 2, 1), "微信", Decimal("8000"), "HKD", "还",
                      "还本", "A", "外币还款"),
        ]
        result = run_engine(rows, debts, config)
        self.assertEqual(result.blocked_row_ids, ["2"])


class EndToEndTests(unittest.TestCase):
    def test_cli_dry_run_succeeds_and_marks_shadow(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = build_materials(root, masked=True)
            proposal = proposal_payload(materials)
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [
                    str(PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"),
                    str(PROJECT_ROOT / "backend" / "scripts" / "run_shadow_case.py"),
                    "--materials", str(materials),
                    "--proposal-file", str(proposal_path),
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            output = json.loads(completed.stdout)
            report = Path(output["report"]).read_text(encoding="utf-8")
            self.assertIn("影子模式，不可提交", report)
            self.assertIn("S1 脱敏完整性", report)

    def test_cli_attempt_lock_exits_two(self) -> None:
        completed = subprocess.run(
            [
                str(PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"),
                str(PROJECT_ROOT / "backend" / "scripts" / "run_shadow_case.py"),
                "--attempt-lock",
            ],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("BLOCKED", completed.stderr)


if __name__ == "__main__":
    unittest.main()
