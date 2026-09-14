from __future__ import annotations

import unittest
from uuid import uuid4

from case_kernel.case_agent_ledger_exception_followup import (
    LedgerExceptionFollowupAction,
    LedgerExceptionFollowupBlocked,
    LedgerExceptionFollowupKind,
    ManagedEvidenceSourceRef,
    ManagedEvidenceSourceType,
    control_transfer_request_hash,
    followup_request_hash,
    reextraction_set_satisfaction_request_hash,
    reextraction_task_binding_request_hash,
    validate_followup_action,
)


def _id() -> str:
    return str(uuid4())


class LedgerExceptionFollowupDomainTests(unittest.TestCase):
    def test_more_evidence_requires_nonempty_unique_managed_sources(self) -> None:
        request_id = _id()
        source = ManagedEvidenceSourceRef(
            ManagedEvidenceSourceType.EVIDENCE_FILE,
            _id(),
        )
        self.assertEqual(
            validate_followup_action(
                kind=LedgerExceptionFollowupKind.MORE_EVIDENCE,
                action=LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE,
                managed_evidence_request_id=request_id,
                managed_evidence_sources=(source,),
                reextraction_batch_id=None,
                reason_note="已核对新增来源",
            ),
            "已核对新增来源",
        )
        for sources in ((), (source, source)):
            with self.subTest(sources=sources), self.assertRaises(
                LedgerExceptionFollowupBlocked
            ):
                validate_followup_action(
                    kind=LedgerExceptionFollowupKind.MORE_EVIDENCE,
                    action=LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE,
                    managed_evidence_request_id=request_id,
                    managed_evidence_sources=sources,
                    reextraction_batch_id=None,
                    reason_note="已核对新增来源",
                )

    def test_withdraw_and_supersede_close_any_kind_but_resume_only_defer(self) -> None:
        for kind in LedgerExceptionFollowupKind:
            for action in (
                LedgerExceptionFollowupAction.WITHDRAW,
                LedgerExceptionFollowupAction.SUPERSEDE,
            ):
                self.assertEqual(
                    validate_followup_action(
                        kind=kind,
                        action=action,
                        managed_evidence_request_id=None,
                        reextraction_batch_id=None,
                        reason_note="主办律师明确终止",
                    ),
                    "主办律师明确终止",
                )
        with self.assertRaises(LedgerExceptionFollowupBlocked):
            validate_followup_action(
                kind=LedgerExceptionFollowupKind.REEXTRACTION,
                action=LedgerExceptionFollowupAction.RESUME,
                managed_evidence_request_id=None,
                reextraction_batch_id=None,
                reason_note="继续",
            )

    def test_source_set_hash_is_order_independent_and_exact(self) -> None:
        first = ManagedEvidenceSourceRef(
            ManagedEvidenceSourceType.MATERIAL_OBJECT,
            _id(),
        )
        second = ManagedEvidenceSourceRef(
            ManagedEvidenceSourceType.EVIDENCE_FILE,
            _id(),
        )
        arguments = dict(
            matter_id=_id(),
            expected_version=9,
            followup_id=_id(),
            action=LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE,
            managed_evidence_request_id=_id(),
            reason_note="来源集合",
        )
        self.assertEqual(
            followup_request_hash(
                **arguments,
                managed_evidence_sources=(first, second),
            ),
            followup_request_hash(
                **arguments,
                managed_evidence_sources=(second, first),
            ),
        )

    def test_reextraction_task_binding_hash_binds_all_lineage_ids(self) -> None:
        arguments = dict(
            matter_id=_id(),
            expected_version=11,
            followup_id=_id(),
            run_id=_id(),
            graph_id=_id(),
            task_id=_id(),
        )
        baseline = reextraction_task_binding_request_hash(**arguments)
        for field in ("followup_id", "run_id", "graph_id", "task_id"):
            changed = dict(arguments)
            changed[field] = _id()
            self.assertNotEqual(
                baseline,
                reextraction_task_binding_request_hash(**changed),
            )

    def test_reextraction_set_hash_has_no_caller_selected_subset(self) -> None:
        parameters = dict(
            matter_id=_id(),
            expected_version=11,
            run_id=_id(),
            graph_id=_id(),
        )
        baseline = reextraction_set_satisfaction_request_hash(**parameters)
        for field in ("run_id", "graph_id"):
            changed = dict(parameters)
            changed[field] = _id()
            self.assertNotEqual(
                baseline,
                reextraction_set_satisfaction_request_hash(**changed),
            )

    def test_control_transfer_hash_binds_server_selected_run(self) -> None:
        arguments = {
            "matter_id": _id(),
            "expected_version": 11,
            "replacement_run_id": _id(),
        }
        baseline = control_transfer_request_hash(**arguments)
        changed = dict(arguments, replacement_run_id=_id())
        self.assertNotEqual(
            baseline,
            control_transfer_request_hash(**changed),
        )


if __name__ == "__main__":
    unittest.main()
