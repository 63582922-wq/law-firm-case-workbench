from __future__ import annotations

import json
import unittest

from case_kernel.agent_draft_review import (
    AgentDraftReviewBlocked,
    approve_agent_draft_candidate,
    prepare_docx_review_candidate,
    serialize_docx_candidate,
)
from case_kernel.agent_structured_draft import parse_docx_candidate
from case_kernel.models import Actor, Role


class AgentDraftReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = prepare_docx_review_candidate(parse_docx_candidate(json.dumps({
            "schema_version": "agent-docx-draft-v1", "title": "答辩状草稿",
            "sections": [{"heading": "意见", "paragraphs": ["请求核减不受保护利息。"], "source_refs": ["fact:interest"]}],
        }, ensure_ascii=False)))

    def test_only_lawyer_can_bind_exact_candidate_review_hash(self) -> None:
        lawyer = Actor("lawyer", "firm", frozenset({Role.LEAD_LAWYER}))
        approved = approve_agent_draft_candidate(candidate=self.candidate, lawyer=lawyer, supplied_review_hash=self.candidate.review_hash)
        self.assertEqual(approved.approved_by, "lawyer")
        self.assertEqual(approved.candidate.input_hash, self.candidate.input_hash)

    def test_canonical_payload_can_be_reparsed_without_an_approval_marker(self) -> None:
        structured = parse_docx_candidate(json.dumps({
            "schema_version": "agent-docx-draft-v1", "title": "答辩状草稿",
            "sections": [{"heading": "意见", "paragraphs": ["请求核减不受保护利息。"], "source_refs": ["fact:interest"]}],
        }, ensure_ascii=False))
        serialized = serialize_docx_candidate(structured)
        self.assertNotIn(b"review_hash", serialized)
        self.assertEqual(parse_docx_candidate(serialized).input_hash, structured.input_hash)

    def test_model_role_and_changed_hash_cannot_approve(self) -> None:
        model = Actor("assistant", "firm", frozenset({Role.ASSISTANT}))
        with self.assertRaisesRegex(AgentDraftReviewBlocked, "only a lead lawyer"):
            approve_agent_draft_candidate(candidate=self.candidate, lawyer=model, supplied_review_hash=self.candidate.review_hash)
        lawyer = Actor("lawyer", "firm", frozenset({Role.REVIEWER}))
        with self.assertRaisesRegex(AgentDraftReviewBlocked, "exact"):
            approve_agent_draft_candidate(candidate=self.candidate, lawyer=lawyer, supplied_review_hash="0" * 64)


if __name__ == "__main__":
    unittest.main()
