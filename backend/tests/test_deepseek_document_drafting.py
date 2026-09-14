from __future__ import annotations

from hashlib import sha256
import json
import unittest

from case_kernel.deepseek_case_agent_planner import (
    DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
)
from case_kernel.deepseek_document_drafting import (
    DeepSeekDocumentDraftConfig,
    DeepSeekDocumentDraftCredentials,
    DeepSeekDocumentDraftProvider,
    DeepSeekDocumentDraftRejected,
    DeepSeekDocumentDraftUnknownSubmission,
)
from backend.tests.test_case_agent_document_delivery import (
    CaseAgentDocumentDeliveryTests,
)
from case_kernel.case_agent_document_delivery import build_document_draft_request


class DeepSeekDocumentDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        helper = CaseAgentDocumentDeliveryTests()
        self.binding = helper.binding()
        self.candidate = helper.response(self.binding)
        self.request = build_document_draft_request(self.binding)
        self.config = DeepSeekDocumentDraftConfig(
            endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
            model="deepseek-chat",
            allowed_models=("deepseek-chat",),
        )

    def response(self) -> bytes:
        return json.dumps(
            {
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": self.candidate.decode()},
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def provider(self, transport):
        return DeepSeekDocumentDraftProvider(
            credentials=DeepSeekDocumentDraftCredentials("k" * 40),
            config=self.config,
            transport=transport,
        )

    def test_prepared_request_is_server_bound_and_credential_free(self):
        provider = self.provider(lambda *_: self.response())
        prepared = provider.prepare(request=self.request, binding=self.binding)
        self.assertEqual(sha256(prepared.body).hexdigest(), prepared.request_hash)
        self.assertNotIn("k" * 40, prepared.body.decode())
        body = json.loads(prepared.body)
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(
            user["output_contract"]["binding"]["binding_hash"],
            self.binding.binding_hash,
        )
        self.assertEqual(
            user["output_contract"]["binding"],
            json.loads(self.candidate)["binding"],
        )
        self.assertEqual(
            user["output_contract"]["title"],
            self.binding.template.title_label,
        )
        authorized = user["authorized_document_request"]
        self.assertEqual(
            authorized["review_contract"]["title"],
            self.binding.template.title_label,
        )

    def test_single_provider_result_becomes_review_only_candidate(self):
        calls = []

        def transport(endpoint, headers, body, timeout):
            calls.append((endpoint, headers, body, timeout))
            return self.response()

        provider = self.provider(transport)
        prepared = provider.prepare(request=self.request, binding=self.binding)
        candidate = provider.send(prepared=prepared, binding=self.binding)
        self.assertEqual(len(calls), 1)
        self.assertEqual(candidate.review_status, "NEEDS_LAWYER_REVIEW")
        self.assertEqual(candidate.deliverable_kind, "DEFENCE_STATEMENT")

    def test_timeout_is_unknown_and_provider_does_not_retry(self):
        calls = 0

        def transport(*_):
            nonlocal calls
            calls += 1
            raise TimeoutError("unknown")

        provider = self.provider(transport)
        prepared = provider.prepare(request=self.request, binding=self.binding)
        with self.assertRaises(DeepSeekDocumentDraftUnknownSubmission):
            provider.send(prepared=prepared, binding=self.binding)
        self.assertEqual(calls, 1)

    def test_truncated_or_wrong_model_response_is_rejected(self):
        for value in (
            {
                "model": "deepseek-chat",
                "choices": [{"finish_reason": "length", "message": {"content": self.candidate.decode()}}],
            },
            {
                "model": "another-model",
                "choices": [{"finish_reason": "stop", "message": {"content": self.candidate.decode()}}],
            },
        ):
            provider = self.provider(
                lambda *_, value=value: json.dumps(value).encode()
            )
            prepared = provider.prepare(request=self.request, binding=self.binding)
            with self.assertRaises(DeepSeekDocumentDraftRejected):
                provider.send(prepared=prepared, binding=self.binding)

    def test_provider_rejects_candidate_with_unbound_source(self):
        value = json.loads(self.candidate)
        value["sections"][0]["paragraphs"][0]["source_refs"] = ["source:999"]
        self.candidate = json.dumps(value).encode()
        provider = self.provider(lambda *_: self.response())
        prepared = provider.prepare(request=self.request, binding=self.binding)
        with self.assertRaisesRegex(DeepSeekDocumentDraftRejected, "outside"):
            provider.send(prepared=prepared, binding=self.binding)


if __name__ == "__main__":
    unittest.main()
