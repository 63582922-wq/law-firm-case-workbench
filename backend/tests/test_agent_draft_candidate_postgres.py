from __future__ import annotations

from hashlib import sha256
import json
import unittest

from case_kernel.agent_draft_candidate_postgres import AgentDraftCandidateSpec, PostgresAgentDraftCandidateStore


class AgentDraftCandidateStoreTests(unittest.TestCase):
    def test_staging_reauthenticates_and_reparses_the_encrypted_candidate(self) -> None:
        raw = json.dumps({"schema_version":"agent-docx-draft-v1","title":"草稿","sections":[{"heading":"意见","paragraphs":["文本"],"source_refs":["fact:1"]}]}, ensure_ascii=False, separators=(",", ":")).encode()
        digest = sha256(raw).hexdigest()
        store = PostgresAgentDraftCandidateStore("postgresql://not-used.invalid/lawcase_test", artifact_reader=lambda key, expected: raw)
        candidate = store._validated_candidate(AgentDraftCandidateSpec("DOCX", "case-agent", "1.0.0", f"{digest[:2]}/{digest[2:4]}/{digest}.lca", digest, len(raw), "a" * 64))
        self.assertEqual(candidate.skill_id, "document_drafting")
        self.assertEqual(len(candidate.review_hash), 64)


if __name__ == "__main__":
    unittest.main()
