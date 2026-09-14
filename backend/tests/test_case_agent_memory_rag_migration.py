from pathlib import Path
import unittest


class CaseAgentMemoryRagMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = (
            Path(__file__).parents[1]
            / "migrations"
            / "0032_case_agent_memory_rag.sql"
        )
        cls.sql = cls.path.read_text(encoding="utf-8")

    def test_authoritative_versions_sources_acl_and_heads_exist(self):
        for table in (
            "case_agent_memory_permission_groups",
            "case_agent_memory_group_memberships",
            "case_agent_memory_group_member_denials",
            "case_agent_published_knowledge_objects",
            "case_agent_knowledge_publication_reviews",
            "case_agent_knowledge_publications",
            "case_agent_public_legal_authority_registrations",
            "case_agent_public_legal_authority_sources",
            "case_agent_memory_record_versions",
            "case_agent_memory_record_groups",
            "case_agent_memory_source_refs",
            "case_agent_memory_tombstones",
            "case_agent_memory_access_denials",
            "case_agent_memory_record_heads",
            "case_agent_memory_retrieval_audits",
            "case_agent_memory_checkpoints",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"'{table}'", self.sql)
        self.assertIn("case Agent memory history is append-only", self.sql)
        self.assertIn("the first memory head version must be one", self.sql)
        self.assertIn("NEW.current_version <> OLD.current_version + 1", self.sql)
        self.assertIn("memory head does not match its authoritative version", self.sql)
        self.assertIn("validate_case_agent_memory_checkpoint", self.sql)
        self.assertIn("memory checkpoint differs from the current Agent run", self.sql)
        self.assertIn("memory checkpoint does not extend the current hash chain", self.sql)
        self.assertIn("memory checkpoint contains an unauthorized retrieval scope", self.sql)

    def test_retrieval_audit_is_bound_to_optional_run_and_task(self):
        self.assertIn("run_id uuid", self.sql)
        self.assertIn("task_id uuid", self.sql)
        self.assertIn("validate_case_agent_memory_retrieval_run_scope", self.sql)
        self.assertIn("memory retrieval is outside the lawyer Agent run", self.sql)
        self.assertIn("memory retrieval task is outside the Agent run", self.sql)

    def test_every_memory_table_is_forced_rls(self):
        self.assertIn("ALTER TABLE %I FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("current_setting(''app.firm_id'', true)", self.sql)
        self.assertIn("case_agent_memory_record_versions", self.sql)
        # The physical custodian is an RLS tenant even though PUBLIC_LEGAL has
        # no semantic firm in the domain record.
        self.assertIn("semantic_firm_id IS NULL", self.sql)
        self.assertIn("firm_id::text = current_setting", self.sql)

    def test_source_refs_are_database_bound_not_free_json(self):
        self.assertIn("validate_case_agent_memory_source_ref", self.sql)
        self.assertIn("FROM evidence_pages page", self.sql)
        self.assertIn("FROM case_facts fact", self.sql)
        self.assertIn("FROM case_claims claim", self.sql)
        self.assertIn("FROM case_transactions transaction_row", self.sql)
        self.assertIn("case_agent_published_knowledge_objects", self.sql)
        self.assertIn("case_agent_knowledge_publication_reviews", self.sql)
        self.assertIn("first_review_hash", self.sql)
        self.assertIn("second_review_hash", self.sql)
        self.assertIn("JOIN official_legal_source_snapshots snapshot", self.sql)
        self.assertIn("snapshot.verification_status = 'VERIFIED'", self.sql)
        self.assertIn("snapshot.license_status = 'ACTIVE'", self.sql)
        self.assertIn("snapshot.authority_level = record_authority", self.sql)
        self.assertIn("snapshot.official_url = NEW.source_url", self.sql)
        self.assertIn(
            "registration.effective_from = record_effective_from", self.sql
        )
        self.assertIn(
            "registration.effective_to IS NOT DISTINCT FROM record_effective_to",
            self.sql,
        )

    def test_publication_and_memory_groups_are_exact_approved_scopes(self):
        self.assertIn("approved_permission_group_ids uuid[] NOT NULL", self.sql)
        self.assertIn(
            "publication groups must exactly match the approved publication scope",
            self.sql,
        )
        self.assertIn(
            "firm memory groups must exactly match its approved publication",
            self.sql,
        )
        self.assertIn("memory version contains an incompatible permission group", self.sql)

    def test_revocation_precedes_terminal_head_and_membership_denial_wins(self):
        guard_position = self.sql.index("CREATE FUNCTION guard_case_agent_memory_head")
        tombstone_check = self.sql.index("memory denial/tombstone must be durable", guard_position)
        self.assertGreater(tombstone_check, guard_position)
        self.assertIn("case_agent_memory_group_member_denials", self.sql)
        self.assertIn("case_agent_memory_access_denials", self.sql)
        self.assertIn("blocked_through_version >= NEW.current_version", self.sql)

    def test_fts_is_explicitly_an_accelerator_and_vector_is_not_claimed(self):
        self.assertIn("search_vector tsvector GENERATED ALWAYS", self.sql)
        self.assertIn("USING gin(search_vector)", self.sql)
        self.assertIn("search_document_hash", self.sql)
        self.assertIn("indexing_receipt_hash", self.sql)
        self.assertIn("search_mode = 'POSTGRES_FTS_V1'", self.sql)
        self.assertNotIn(" vector(", self.sql.lower())
        self.assertNotIn("CREATE EXTENSION vector", self.sql)

    def test_sql_parses_with_pglast_when_available(self):
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        self.assertTrue(parse_sql(self.sql))


if __name__ == "__main__":
    unittest.main()
