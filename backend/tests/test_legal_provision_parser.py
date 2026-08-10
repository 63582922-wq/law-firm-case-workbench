from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.legal_provision_parser import (
    LegalProvisionParseBlocked,
    parse_civil_code_borrowing_provisions,
    parse_private_lending_2015_original,
    parse_private_lending_first_revision,
    parse_private_lending_second_revision,
)
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.official_source_capture import OfficialHttpResponse, capture_authorized_official_source
from case_kernel.research_gateway import PublicResearchGateway


class HtmlTransport:
    def __init__(self, url: str, body: bytes) -> None:
        self.response = OfficialHttpResponse(
            status_code=200,
            final_url=url,
            media_type="text/html",
            headers={"content-type": "text/html"},
            body=body,
            peer_ip="8.8.8.8",
        )

    def fetch(self, *, url: str, max_bytes: int) -> OfficialHttpResponse:
        return self.response


class LegalProvisionParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="legal-provision-parser-test-")
        self.root = Path(self.temporary.name)
        self.case_root = self.root / "case"
        self.case_root.mkdir()
        self.store = LocalEncryptedArtifactStore(
            self.root / "managed",
            key_id="synthetic-law-key-v1",
            encryption_key=b"j" * 32,
        )
        self.url = "https://www.court.gov.cn/zixun/xiangqing/282621.html"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, body: bytes):
        return self.capture_source(
            source_id="SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
            url=self.url,
            issue="民间借贷利率保护与过渡规则",
            query="民间借贷 2020年8月20日 过渡规则 一年期LPR",
            body=body,
        )

    def capture_source(self, *, source_id: str, url: str, issue: str, query: str, body: bytes):
        gateway = PublicResearchGateway()
        plan = gateway.prepare_plan(issue=issue, proposed_query=query)
        request = gateway.authorize_public_request(
            plan_id=plan.plan_id,
            source_id=source_id,
            target_url=url,
            requested_by="synthetic_lawyer",
            lawyer_confirmed=True,
        )
        return capture_authorized_official_source(
            request=request,
            gateway=gateway,
            artifact_store=self.store,
            case_root=str(self.case_root),
            transport=HtmlTransport(url, body),
            now=request.authorized_at + timedelta(seconds=1),
        )

    def document(self, *, article_25_marker: str = "合同成立时") -> bytes:
        html = f"""<!doctype html><html><body>
        <section>无关司法解释 第二十五条 这是无关条文。第三十一条 仍然无关。</section>
        <article>
        <h1>最高人民法院 关于审理民间借贷案件适用法律若干问题的规定</h1>
        <p>根据2020年8月18日决定第一次修正，根据2020年12月23日决定第二次修正。</p>
        <p>第一条 本规定适用于民间借贷。</p>
        <p>第二十四条 借贷双方没有约定利息，出借人主张支付利息的，人民法院不予支持。自然人之间借贷对利息约定不明，出借人主张支付利息的，人民法院不予支持。</p>
        <p>第二十五条 出借人请求借款人按照合同约定利率支付利息的，人民法院应予支持，但是双方约定的利率超过{article_25_marker}一年期贷款市场报价利率四倍的除外。前款所称一年期贷款市场报价利率，是指自2019年8月20日起每月发布的数据。</p>
        <p>第二十六条 借据、收据、欠条等债权凭证载明的借款金额，一般认定为本金。预先在本金中扣除利息的，人民法院应当将实际出借的金额认定为本金。</p>
        <p>第二十七条 借贷双方对前期借款本息结算后将利息计入后期借款本金，超过部分的利息，不应认定为后期借款本金。</p>
        <p>第二十八条 借贷双方对逾期利率有约定的，从其约定。既未约定借期内利率，也未约定逾期利率的，参照当时一年期贷款市场报价利率标准计算。约定了借期内利率但是未约定逾期利率的，按照借期内利率计算。</p>
        <p>第二十九条 出借人与借款人既约定了逾期利率，又约定了违约金或者其他费用，总计超过合同成立时一年期贷款市场报价利率四倍的部分，人民法院不予支持。</p>
        <p>第三十条 借款人可以提前偿还借款。</p>
        <p>第三十一条 本规定施行后新受理的一审案件适用本规定。2020年8月20日之后新受理且合同成立在此前的案件，合同成立至2020年8月19日按请求审查；此后部分适用起诉时本规定的利率保护标准。以本规定为准。</p>
        </article></body></html>"""
        return html.encode("utf-8")

    def test_parser_anchors_inside_republished_document_and_extracts_the_interest_rule_set(self) -> None:
        parsed = parse_private_lending_second_revision(
            capture=self.capture(self.document()), artifact_store=self.store
        )
        self.assertEqual(parsed.version_label, "2020年第二次修正")
        self.assertEqual(
            [item.provision_label for item in parsed.provisions],
            ["第二十四条", "第二十五条", "第二十六条", "第二十七条", "第二十八条", "第二十九条", "第三十一条"],
        )
        self.assertIn("没有约定利息", parsed.provisions[0].normalized_text)
        self.assertIn("合同成立时", parsed.provisions[1].normalized_text)
        self.assertIn("预先在本金中扣除利息", parsed.provisions[2].normalized_text)
        self.assertIn("逾期利率", parsed.provisions[4].normalized_text)
        self.assertIn("适用起诉时", parsed.provisions[-1].normalized_text)
        self.assertNotIn("无关条文", parsed.provisions[1].normalized_text)
        self.assertEqual(parsed.review_status, "HUMAN_REVIEW_REQUIRED")

    def test_missing_temporal_marker_or_version_history_is_blocked(self) -> None:
        capture = self.capture(self.document(article_25_marker="当前"))
        with self.assertRaisesRegex(LegalProvisionParseBlocked, "合同成立时"):
            parse_private_lending_second_revision(capture=capture, artifact_store=self.store)

        missing_version = self.document().replace("第二次修正".encode(), "版本缺失".encode())
        capture = self.capture(missing_version)
        with self.assertRaisesRegex(LegalProvisionParseBlocked, "version history"):
            parse_private_lending_second_revision(capture=capture, artifact_store=self.store)

    def test_civil_code_parser_extracts_contract_formation_and_interest_articles(self) -> None:
        url = "https://www.court.gov.cn/zixun/xiangqing/233181.html"
        body = """<!doctype html><html><body><h1>中华人民共和国民法典</h1><p>（2020年5月28日第十三届全国人民代表大会第三次会议通过）</p><p>第六百七十九条 自然人之间的借款合同，自贷款人提供借款时成立。</p><p>第六百八十条 禁止高利放贷。借款合同对支付利息没有约定的，视为没有利息。借款合同对支付利息约定不明确，自然人之间借款的，视为没有利息。</p><p>第六百八十一条 保证合同另行规定。</p></body></html>""".encode("utf-8")
        gateway = PublicResearchGateway()
        plan = gateway.prepare_plan(issue="民法典借款利息", proposed_query="民法典 借款利息 高利放贷")
        request = gateway.authorize_public_request(
            plan_id=plan.plan_id,
            source_id="CN-CIVIL-CODE-680",
            target_url=url,
            requested_by="synthetic_lawyer",
            lawyer_confirmed=True,
        )
        capture = capture_authorized_official_source(
            request=request,
            gateway=gateway,
            artifact_store=self.store,
            case_root=str(self.case_root),
            transport=HtmlTransport(url, body),
            now=request.authorized_at + timedelta(seconds=1),
        )
        parsed = parse_civil_code_borrowing_provisions(capture=capture, artifact_store=self.store)
        self.assertEqual([item.provision_label for item in parsed.provisions], ["第六百七十九条", "第六百八十条"])
        self.assertIn("贷款人提供借款时成立", parsed.provisions[0].normalized_text)
        self.assertIn("禁止高利放贷", parsed.provisions[1].normalized_text)

    def test_first_revision_parser_preserves_historical_filing_time_rule(self) -> None:
        url = "https://www.court.gov.cn/zixun/xiangqing/249031.html"
        body = """<!doctype html><html><body><h1>最高人民法院 关于审理民间借贷案件适用法律若干问题的规定</h1><p>根据2020年8月18日决定修正，修正自2020年8月20日起施行。</p><p>第二十六条 合同约定利率超过合同成立时一年期贷款市场报价利率四倍的除外。一年期贷款市场报价利率自2019年8月20日起发布。</p><p>第二十七条 本金按实际出借认定。</p><p>第三十二条 借贷行为发生在2019年8月20日之前的，可参照原告起诉时一年期贷款市场报价利率四倍确定受保护的利率上限。以本解释为准。</p></body></html>""".encode("utf-8")
        capture = self.capture_source(
            source_id="SPC-PRIVATE-LENDING-2020-FIRST-REVISION",
            url=url,
            issue="民间借贷历史文本",
            query="民间借贷 2020年第一次修正 历史文本 过渡规则",
            body=body,
        )
        parsed = parse_private_lending_first_revision(capture=capture, artifact_store=self.store)
        self.assertEqual([item.provision_label for item in parsed.provisions], ["第二十六条", "第三十二条"])
        self.assertIn("原告起诉时", parsed.provisions[1].normalized_text)

    def test_2015_parser_keeps_unpaid_and_voluntarily_paid_interest_paths_separate(self) -> None:
        url = "https://gongbao.court.gov.cn/Details/48786dea74c9545c2f4fb27254ca08.html"
        body = """<!doctype html><html><body><p>法释〔2015〕18号，自2015年9月1日起施行。</p><h1>最高人民法院 关于审理民间借贷案件适用法律若干问题的规定</h1><p>第二十六条 借贷双方约定的利率未超过年利率24%，依法支持；超过年利率36%的部分无效，借款人请求返还已支付的超过部分，依法支持。</p><p>第二十七条 实际出借金额认定本金。</p><p>第三十一条 借款人自愿支付利息后又以不当得利请求返还的，不予支持，但超过年利率36%部分除外。</p><p>第三十二条 本规定施行时间另行规定。</p></body></html>""".encode("utf-8")
        capture = self.capture_source(
            source_id="SPC-PRIVATE-LENDING-2015-ORIGINAL",
            url=url,
            issue="民间借贷2015年司法解释已付利息",
            query="民间借贷 2015年司法解释 年利率24% 年利率36% 已付利息",
            body=body,
        )
        parsed = parse_private_lending_2015_original(capture=capture, artifact_store=self.store)
        self.assertIn("返还已支付", parsed.provisions[0].normalized_text)
        self.assertIn("自愿支付", parsed.provisions[1].normalized_text)
        self.assertNotEqual(parsed.provisions[0].semantic_sha256, parsed.provisions[1].semantic_sha256)


if __name__ == "__main__":
    unittest.main()
