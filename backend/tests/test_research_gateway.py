import unittest

from case_kernel.research_gateway import PublicResearchGateway, ResearchBlocked


class PublicResearchGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = PublicResearchGateway()

    def test_plan_blocks_sensitive_case_data_and_selects_registered_sources(self) -> None:
        with self.assertRaisesRegex(ResearchBlocked, "CN_MOBILE"):
            self.gateway.prepare_plan(issue="民间借贷利息", proposed_query="张某 13800138000 已付利息")

        plan = self.gateway.prepare_plan(
            issue="民间借贷利率保护与过渡规则",
            proposed_query="民间借贷 2020年8月20日 过渡规则 一年期LPR",
        )
        self.assertIn("SPC-PRIVATE-LENDING-2020-SECOND-REVISION", plan.candidate_source_ids)
        self.assertIn("CFETS-LPR-HISTORY", plan.candidate_source_ids)

    def test_external_request_requires_lawyer_confirmation_and_registered_https_domain(self) -> None:
        plan = self.gateway.prepare_plan(issue="LPR", proposed_query="一年期贷款市场报价利率 历史数据")
        with self.assertRaisesRegex(ResearchBlocked, "explicitly authorize"):
            self.gateway.authorize_public_request(
                plan_id=plan.plan_id,
                source_id="CFETS-LPR-HISTORY",
                target_url="https://www.shibor.org/r/cms/shibor/chinamoney/html/shiborOrg/lpr-shibor-history-download.html",
                requested_by="alpha_lead_lawyer",
                lawyer_confirmed=False,
            )
        with self.assertRaisesRegex(ResearchBlocked, "approved HTTPS"):
            self.gateway.authorize_public_request(
                plan_id=plan.plan_id,
                source_id="CFETS-LPR-HISTORY",
                target_url="https://example.com/lpr",
                requested_by="alpha_lead_lawyer",
                lawyer_confirmed=True,
            )

    def test_response_receipt_binds_to_authorized_url_and_hash(self) -> None:
        plan = self.gateway.prepare_plan(issue="LPR", proposed_query="一年期贷款市场报价利率 历史数据")
        request = self.gateway.authorize_public_request(
            plan_id=plan.plan_id,
            source_id="CFETS-LPR-HISTORY",
            target_url="https://www.shibor.org/r/cms/shibor/chinamoney/html/shiborOrg/lpr-shibor-history-download.html",
            requested_by="alpha_lead_lawyer",
            lawyer_confirmed=True,
        )
        receipt = self.gateway.record_response(
            request_id=request.request_id,
            source_url=request.target_url,
            response_body=b"synthetic source snapshot",
        )
        self.assertEqual(receipt.request_id, request.request_id)
        self.assertEqual(self.gateway.receipt(request.request_id).response_sha256, receipt.response_sha256)


if __name__ == "__main__":
    unittest.main()
