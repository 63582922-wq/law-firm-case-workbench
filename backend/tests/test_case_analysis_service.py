"""实用模式与案件分析服务层的验收测试。

覆盖：
- 三档门禁（硬红线 / 自动修复 / 标记放行）与数字剔除；
- 提示词契约（不要求模型输出正式数字）；
- 报告渲染完整 8 节；
- 服务层：参数级数字注入、证据缺口挂起、降级路径、注入式传输层的完整流水线。
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.case_analysis_service import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_MODEL_NOT_CONFIGURED,
    AnalysisRequest,
    compute_engine_numbers,
    run_analysis,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_config(root: Path, *, evidence_pending: bool = False) -> Path:
    config = {
        "schema": "shadow-case-config-v1",
        "lpr_4x_monthly_rate": "0.01",
        "interest_cutoff": "2025-06-14",
        "debts": [
            {"debt_id": "L1", "principal": "100000.00", "disbursed_on": "2019-10-19",
             "agreed_monthly_rate": "0.015", "due_on": "2019-12-19"},
            {"debt_id": "L2", "principal": "50000.00", "disbursed_on": "2020-03-19",
             "agreed_monthly_rate": "0.015", "due_on": "2020-09-19",
             **({"evidence_pending": True} if evidence_pending else {})},
        ],
    }
    path = root / "case_config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def _make_materials(root: Path) -> Path:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas as rl_canvas

    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except Exception:  # noqa: BLE001 - 已注册
        pass
    materials = root / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    canvas = rl_canvas.Canvas(str(materials / "起诉状.pdf"), pagesize=(595, 842))
    canvas.setFont("STSong-Light", 12)
    canvas.drawString(60, 780, "原告主张被告偿还借款本金及利息。")
    canvas.save()
    return materials


class FakeTransport:
    """确定性替身：OCR 返回固定文本，分析返回可解析的合法结构。"""

    def __init__(self, *, analysis: dict | None = None) -> None:
        self.calls: list[str] = []
        self._analysis = analysis or {
            "schema": "lawyer-practical-analysis-v1",
            "case_posture": {"summary": "被告主张已超额支付利息，约定月利率1.5%超过上限。"},
            "facts": [{"fact": "2019-10-19 归还本金", "source": "起诉状.pdf 第1页"}],
            "issues": [{"issue": "利率上限", "why_it_matters": "影响利息",
                        "our_position": "按起诉时上限", "evidence": ["起诉状.pdf 第1页"]}],
            "review_notes": ["微信凭证仅供参考，建议调取后台数据"],
        }

    def ocr_pages(self, pages, authorized, ledger):
        self.calls.append("ocr")
        return []

    def call_analysis(self, *, instruction, ledger, purpose="lawyer-analysis",
                      max_output_tokens=24576):
        self.calls.append("analysis")
        ledger.append(purpose=purpose, provider="fake", model="fake", region="cn",
                      retention="不保存", payload_sha256="0" * 64, status="ok",
                      cost_cny="0.010000")
        return self._analysis


class ServiceTests(unittest.TestCase):
    def test_engine_numbers_from_confirmed_parameters(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _write_config(Path(tmp))
            numbers, note = compute_engine_numbers(config)
            self.assertEqual(numbers["L1 未偿本金"], "100000.00")
            self.assertEqual(numbers["L2 未偿本金"], "50000.00")
            self.assertEqual(numbers["合计本金"], "150000.00")
            self.assertIn("模型未参与任何计算", note)
            self.assertIn("付款冲抵后的净额", note)

    def test_engine_numbers_suspend_evidence_pending_debt(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _write_config(Path(tmp), evidence_pending=True)
            numbers, note = compute_engine_numbers(config)
            self.assertNotIn("L2 未偿本金", numbers)
            self.assertEqual(numbers["合计本金"], "100000.00")
            self.assertIn("L2", note)
            self.assertIn("挂起", note)

    def test_engine_numbers_without_config_returns_reason(self) -> None:
        numbers, note = compute_engine_numbers(None)
        self.assertEqual(numbers, {})
        self.assertIn("未提供案件计算参数", note)

    def test_degraded_without_preflight_still_reports_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root)
            materials = _make_materials(root)
            result = run_analysis(AnalysisRequest(
                case_id="c1", materials_dir=materials, output_root=root / "out",
                case_number="（2026）测试号", case_config_path=config,
            ))
            self.assertEqual(result.status, STATUS_MODEL_NOT_CONFIGURED)
            self.assertEqual(result.engine_numbers["合计本金"], "150000.00")
            self.assertIn("正式数字", result.report_md)
            self.assertTrue((root / "out" / "决策包.md").is_file())

    def test_full_pipeline_with_injected_transport(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root)
            materials = _make_materials(root)
            preflight = root / "preflight.json"
            preflight.write_text(json.dumps({
                "purpose": "case_analysis", "sent_fields": {"page_files": []},
                "provider": "aliyun", "model": "qwen3-vl-plus", "region": "cn-beijing",
                "retention": "不保存", "budget_cap_cny": "2", "confirmed": "true",
            }, ensure_ascii=False), encoding="utf-8")
            env_file = root / "model.env"
            env_file.write_text("LAWCASE_AGENT_WORKER_QWEN_API_KEY=x\n"
                                "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID=ws-x\n", encoding="utf-8")
            transport = FakeTransport()
            progress: list[tuple[str, int]] = []
            result = run_analysis(AnalysisRequest(
                case_id="c1", materials_dir=materials, output_root=root / "out",
                case_number="（2026）测试号", case_config_path=config,
                preflight_path=preflight, env_file=env_file, transport=transport,
                progress=lambda stage, percent: progress.append((stage, percent)),
            ))
            self.assertEqual(result.status, STATUS_COMPLETED)
            self.assertEqual(result.gate_level, "MARK_FOR_REVIEW")
            self.assertEqual(sorted(transport.calls), ["analysis"])  # 无授权图片则跳过 OCR
            self.assertEqual(result.calls, 1)
            self.assertIn("争点矩阵", result.report_md)
            self.assertIn("150000.00", result.report_md)  # 引擎数字已注入报告
            self.assertNotIn("1.5%", result.report_md)    # 模型数字被剔除
            self.assertTrue(progress)
            self.assertEqual(progress[-1][1], 100)

    def test_hard_redline_in_model_output_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root)
            materials = _make_materials(root)
            preflight = root / "preflight.json"
            preflight.write_text(json.dumps({
                "purpose": "case_analysis", "sent_fields": {"page_files": []},
                "provider": "aliyun", "model": "qwen3-vl-plus", "region": "cn-beijing",
                "retention": "不保存", "budget_cap_cny": "2", "confirmed": "true",
            }, ensure_ascii=False), encoding="utf-8")
            env_file = root / "model.env"
            env_file.write_text("LAWCASE_AGENT_WORKER_QWEN_API_KEY=x\n"
                                "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID=ws-x\n", encoding="utf-8")
            transport = FakeTransport(analysis={"case_posture": {"summary": "我已批准该方案"}})
            result = run_analysis(AnalysisRequest(
                case_id="c1", materials_dir=materials, output_root=root / "out",
                case_number="（2026）测试号", case_config_path=config,
                preflight_path=preflight, env_file=env_file, transport=transport,
            ))
            self.assertEqual(result.gate_level, "HARD_BLOCKED")
            self.assertEqual(result.status, STATUS_BLOCKED)
            self.assertEqual(result.analysis, {})
            self.assertEqual(result.engine_numbers["合计本金"], "150000.00")


if __name__ == "__main__":
    unittest.main()
