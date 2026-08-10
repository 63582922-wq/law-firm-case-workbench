from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0021_agent_draft_candidates.sql"


class AgentDraftCandidatesMigrationTests(unittest.TestCase):
    def test_candidates_keep_prose_out_of_sql_and_bind_exact_review_to_proposal(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE agent_draft_candidates", sql)
        self.assertIn("content_object_key", sql)
        self.assertIn("content_sha256", sql)
        self.assertIn("review_hash", sql)
        self.assertIn("proposal_id uuid REFERENCES agent_action_proposals", sql)
        self.assertIn("approval_hash = review_hash", sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("Agent draft candidates permit only one exact review decision", sql)
        self.assertNotIn("prompt_text", sql)
        self.assertNotIn("document_body", sql)


if __name__ == "__main__":
    unittest.main()
