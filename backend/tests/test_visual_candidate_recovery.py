from dataclasses import replace
from hashlib import sha256
import json
import unittest

import backend.tests.test_qwen_visual_ocr_adapter as adapter_fixture
from case_kernel.visual_candidate_recovery import (
    VisualRecoverySource, VisualCandidateRecoveryBlocked,
    prepare_visual_candidate_recovery,
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


class VisualCandidateRecoveryTests(unittest.TestCase):
    def setUp(self):
        factory = adapter_fixture.QwenVisualOcrAdapterTests()
        binding, projection = factory.binding()
        staging = adapter_fixture.Staging()
        outcome = factory.adapter(binding, projection, staging=staging).execute(
            context=adapter_fixture.Context(binding))
        self.payload = json.loads(staging.requests[0].payload)
        self.payload.update(cost_basis="CONSERVATIVE_RESERVED_EXPOSURE_NOT_INVOICE",
                            cost_reserve_minor_units=6)
        self.raw = canonical(self.payload)
        self.source = VisualRecoverySource(
            firm_id=binding.firm_id, matter_id=binding.matter_id,
            matter_version=binding.matter_version, run_id=binding.run_id,
            run_event_version=10, run_status="FAILED",
            failure_code="ARTIFACT_VISUAL_CONTRACT_INVALID", task_id=binding.task_id,
            task_status="SUCCEEDED", attempt_id=binding.attempt_id,
            artifact_id=outcome.artifacts[0].artifact_id,
            content_sha256=sha256(self.raw).hexdigest(), byte_size=len(self.raw),
            task_input_hash=binding.task_input_hash, external_request_id=binding.external_request_id,
            cost_minor_units=6, input_refs=binding.input_refs,
        )

    def test_deterministic_draft_preserves_original_cost_and_history(self):
        result = prepare_visual_candidate_recovery(source=self.source, original=self.raw)
        self.assertEqual(result, prepare_visual_candidate_recovery(source=self.source, original=self.raw))
        envelope = json.loads(result.payload)
        self.assertEqual(envelope["historical_run_status"], "FAILED")
        self.assertEqual(envelope["source_content_sha256"], sha256(self.raw).hexdigest())
        self.assertEqual(envelope["retained_cost_metadata"]["cost_reserve_minor_units"], 6)
        self.assertEqual(envelope["interpreted_candidate"]["pages"], self.payload["pages"])
        self.assertFalse(envelope["court_ready"])
        self.assertEqual(envelope["completeness_status"], "NOT_VERIFIED_AGAINST_ORIGINAL_PAGE")
        self.assertEqual(canonical(self.payload), self.raw)

    def test_stale_cross_case_and_wrong_terminal_bindings_rejected(self):
        changes = ({"run_status": "VERIFYING"}, {"failure_code": "OTHER_FAILURE"},
                   {"task_status": "FAILED"}, {"matter_version": self.source.matter_version + 1},
                   {"matter_id": self.source.run_id}, {"input_refs": ()},
                   {"cost_minor_units": True}, {"byte_size": len(self.raw) + 1},
                   {"task_input_hash": "0" * 64}, {"run_event_version": True})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(VisualCandidateRecoveryBlocked):
                prepare_visual_candidate_recovery(source=replace(self.source, **change), original=self.raw)

    def test_additional_corruption_not_repaired_by_cost_separation(self):
        for mutate in (
            lambda value: value.update(cost_reserve_minor_units=True),
            lambda value: value.update(formal_fact=True),
            lambda value: value.update(unknown="must not drop"),
            lambda value: value["pages"][0]["text_blocks"][0].update(text="changed without source hash"),
            lambda value: value.pop("cost_basis"),
        ):
            value = json.loads(self.raw)
            mutate(value)
            raw = canonical(value)
            source = replace(self.source, content_sha256=sha256(raw).hexdigest(), byte_size=len(raw))
            with self.assertRaises(VisualCandidateRecoveryBlocked):
                prepare_visual_candidate_recovery(source=source, original=raw)

    def test_noncanonical_duplicate_keys_are_not_silently_accepted(self):
        raw = self.raw[:-1] + b',"cost_reserve_minor_units":6}'
        source = replace(self.source, content_sha256=sha256(raw).hexdigest(), byte_size=len(raw))
        with self.assertRaises(VisualCandidateRecoveryBlocked):
            prepare_visual_candidate_recovery(source=source, original=raw)
