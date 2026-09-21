from pathlib import Path
import re
import unittest

from case_kernel.case_agent_planning_snapshot import PlanningProjectionObjectType
from case_kernel.case_agent_work_plan_promotion import _REFERENCE_USE


class ApprovedRulePlanBindingMigrationTests(unittest.TestCase):
    def test_database_source_types_match_typed_contract(self):
        sql = (Path(__file__).parents[1] / 'migrations' /
               '0087_approved_rule_plan_bindings.sql').read_text()
        allowed = sql.split('CHECK (object_type IN (', 1)[1].split('))', 1)[0]
        self.assertEqual(set(re.findall(r"'([A-Z_]+)'", allowed)),
                         {item.value for item in PlanningProjectionObjectType})
        self.assertIn("source_status = 'LOCKED' AND reference_use = 'LEGAL_AUTHORITY'", sql)
        self.assertIn('BEGIN;', sql)
        self.assertIn('COMMIT;', sql)
        self.assertNotIn('UPDATE ', sql)
        self.assertNotIn('GRANT ', sql)

    def test_persisted_uses_match_actual_promotion_mapping(self):
        sql = (Path(__file__).parents[1] / 'migrations' /
               '0088_plan_binding_legal_rule_use.sql').read_text()
        allowed = sql.split('CHECK (reference_use IN (', 1)[1].split('))', 1)[0]
        self.assertEqual(set(re.findall(r"'([A-Z_]+)'", allowed)),
                         {use.value for use in _REFERENCE_USE.values()})
        rule_use = _REFERENCE_USE[PlanningProjectionObjectType.APPROVED_LEGAL_RULE]
        self.assertIn(f"source_status = 'LOCKED' AND reference_use = '{rule_use.value}'", sql)
