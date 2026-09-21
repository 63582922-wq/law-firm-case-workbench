from copy import deepcopy
import unittest
from case_kernel.case_agent_issue_discovery import parse_issue_discovery, IssueDiscoveryBlocked, issue_discovery_schema


class IssueDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"fact:docket": object(), "transaction-candidate:a": object(), "transaction-candidate:b": object()}
        self.payload = {"issues": [{"key": "payment", "title": "记录是否重复",
            "question": "两份材料是否记载同一笔支付？", "our_position": "避免漏计可证明的清偿。",
            "opponent_position": "可能主张重复记录不得重复扣减。",
            "source_refs": ["transaction-candidate:a", "transaction-candidate:b"], "needs_lawyer_decision": True}],
            "source_dispositions": [
                {"source_ref": "fact:docket", "disposition": "BACKGROUND", "issue_keys": [], "reason": "案号用于识别案件，不构成实体争点。"},
                {"source_ref": "transaction-candidate:a", "disposition": "ISSUE_RELEVANT", "issue_keys": ["payment"], "reason": "核对重复记录。"},
                {"source_ref": "transaction-candidate:b", "disposition": "ISSUE_RELEVANT", "issue_keys": ["payment"], "reason": "核对另一份材料的对应记录。"}]}

    def parse(self, value=None, source_hash="a" * 64):
        return parse_issue_discovery(value or self.payload, source_hash=source_hash, sources=self.sources)

    def test_cross_source_issue_does_not_turn_docket_into_risk(self):
        result = self.parse()
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(len(result.source_dispositions), 3)
        self.assertEqual(result.source_dispositions[0].disposition, "BACKGROUND")
        self.assertEqual(result.source_dispositions[0].issue_ids, ())
        self.assertNotIn("fact:docket", result.issues[0].source_refs)
        self.assertEqual(result, self.parse())
        self.assertNotEqual(result.issues[0].issue_id, self.parse(source_hash="b" * 64).issues[0].issue_id)

    def test_omitted_source_and_foreign_reference_rejected(self):
        value = deepcopy(self.payload)
        value["source_dispositions"].pop()
        with self.assertRaises(IssueDiscoveryBlocked): self.parse(value)
        value = deepcopy(self.payload)
        value["issues"][0]["source_refs"].append("foreign:record")
        with self.assertRaises(IssueDiscoveryBlocked): self.parse(value)

    def test_background_cannot_hide_an_issue_link(self):
        value = deepcopy(self.payload)
        value["source_dispositions"][1].update(disposition="BACKGROUND", issue_keys=[])
        with self.assertRaises(IssueDiscoveryBlocked): self.parse(value)

    def test_generated_issue_is_not_formal_approval(self):
        value = deepcopy(self.payload)
        value["issues"][0]["approved"] = True
        with self.assertRaises(IssueDiscoveryBlocked): self.parse(value)
        schema = issue_discovery_schema(tuple(self.sources))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["issues"]["maxItems"], 20)
        self.assertEqual(schema["properties"]["source_dispositions"]["minItems"], 3)
