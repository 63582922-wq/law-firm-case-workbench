from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.lpr_source_parser import LprSourceParseBlocked, parse_captured_lpr_snapshot
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.official_source_capture import (
    OfficialHttpResponse,
    capture_authorized_official_source,
)
from case_kernel.research_gateway import PublicResearchGateway


class StaticTransport:
    def __init__(self, *, url: str, media_type: str, body: bytes) -> None:
        self.response = OfficialHttpResponse(
            status_code=200,
            final_url=url,
            media_type=media_type,
            headers={"content-type": media_type},
            body=body,
            peer_ip="8.8.8.8",
        )

    def fetch(self, *, url: str, max_bytes: int) -> OfficialHttpResponse:
        return self.response


class LprSourceParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="lpr-parser-test-")
        self.root = Path(self.temporary.name)
        self.case_root = self.root / "case"
        self.case_root.mkdir()
        self.store = LocalEncryptedArtifactStore(
            self.root / "managed",
            key_id="synthetic-lpr-key-v1",
            encryption_key=b"r" * 32,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, *, url: str, media_type: str, body: bytes):
        gateway = PublicResearchGateway()
        plan = gateway.prepare_plan(issue="LPR", proposed_query="一年期贷款市场报价利率 历史数据")
        request = gateway.authorize_public_request(
            plan_id=plan.plan_id,
            source_id="CFETS-LPR-HISTORY",
            target_url=url,
            requested_by="synthetic_lawyer",
            lawyer_confirmed=True,
        )
        return capture_authorized_official_source(
            request=request,
            gateway=gateway,
            artifact_store=self.store,
            case_root=str(self.case_root),
            transport=StaticTransport(url=url, media_type=media_type, body=body),
            now=request.authorized_at + timedelta(seconds=1),
        )

    def json_body(self, records: list[dict]) -> bytes:
        return json.dumps(
            {
                "head": {"rep_code": "200"},
                "data": {"baseCurveCfgList": ["1Y", "5Y"]},
                "records": records,
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def test_json_records_are_sorted_and_form_non_overlapping_effective_intervals(self) -> None:
        url = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN"
        capture = self.capture(
            url=url,
            media_type="application/json",
            body=self.json_body(
                [
                    {"showDateCN": "2020-08-20", "1Y": "3.85", "5Y": "4.65"},
                    {"showDateCN": "2019-08-20", "1Y": "4.25", "5Y": "4.85"},
                ]
            ),
        )
        parsed = parse_captured_lpr_snapshot(capture=capture, artifact_store=self.store)
        self.assertEqual([item.publication_date.isoformat() for item in parsed.observations], ["2019-08-20", "2020-08-20"])
        self.assertEqual(parsed.observations[0].effective_until.isoformat(), "2020-08-20")
        self.assertIsNone(parsed.observations[1].effective_until)
        self.assertEqual(parsed.observations[0].one_year_rate, Decimal("0.042500"))
        self.assertEqual(parsed.review_status, "HUMAN_REVIEW_REQUIRED")

    def test_official_announcement_html_yields_one_candidate_observation(self) -> None:
        url = "https://www.chinamoney.com.cn/chinese/bklprmkn2/20190820/1365569.html"
        body = """<!doctype html><html><body><h1>2019年8月20日贷款市场报价利率公告</h1><p>中国人民银行授权全国银行间同业拆借中心公布，2019年8月20日贷款市场报价利率（LPR）为：1年期LPR为4.25%，5年期以上LPR为4.85%。以上LPR在下一次发布LPR之前有效。</p></body></html>""".encode("utf-8")
        capture = self.capture(url=url, media_type="text/html", body=body)
        parsed = parse_captured_lpr_snapshot(capture=capture, artifact_store=self.store)
        self.assertEqual(len(parsed.observations), 1)
        self.assertEqual(parsed.observations[0].publication_date.isoformat(), "2019-08-20")
        self.assertEqual(parsed.observations[0].one_year_rate, Decimal("0.042500"))

    def test_duplicate_dates_empty_records_and_out_of_range_rates_are_blocked(self) -> None:
        url = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN"
        cases = (
            (
                [
                    {"showDateCN": "2020-08-20", "1Y": "3.85", "5Y": "4.65"},
                    {"showDateCN": "2020-08-20", "1Y": "3.85", "5Y": "4.65"},
                ],
                "duplicate",
            ),
            ([], "1 to 24"),
            ([{"showDateCN": "2020-08-20", "1Y": "25", "5Y": "4.65"}], "outside"),
        )
        for records, message in cases:
            with self.subTest(message=message):
                capture = self.capture(
                    url=url,
                    media_type="application/json",
                    body=self.json_body(records),
                )
                with self.assertRaisesRegex(LprSourceParseBlocked, message):
                    parse_captured_lpr_snapshot(capture=capture, artifact_store=self.store)

    def test_history_landing_page_without_rate_rows_cannot_be_misused_as_parameter_data(self) -> None:
        url = "https://www.chinamoney.com.cn/r/cms/chinese/chinamoney/html/currency/lpr-shibor-history-download.html"
        capture = self.capture(
            url=url,
            media_type="text/html",
            body=b"<!doctype html><html><body><h1>LPR history query</h1></body></html>",
        )
        with self.assertRaisesRegex(LprSourceParseBlocked, "exactly one"):
            parse_captured_lpr_snapshot(capture=capture, artifact_store=self.store)


if __name__ == "__main__":
    unittest.main()
