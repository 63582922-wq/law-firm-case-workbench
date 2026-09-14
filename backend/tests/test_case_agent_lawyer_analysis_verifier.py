from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from case_kernel.case_agent_lawyer_analysis import (
    LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
    LAWYER_DECISION_PACKAGE_SCHEMA,
    compile_lawyer_decision_package_candidate,
    parse_lawyer_analysis_provider_response,
    prepare_lawyer_analysis_request,
)
from case_kernel.case_agent_supervisor import (
    AgentEventType,
    ArtifactReceipt,
    ExternalSubmissionState,
    ResultStatus,
    TaskResultPayload,
    reduce_agent_event,
)
from case_kernel.case_agent_verifier import (
    CanonicalJsonArtifactVerifier,
    ManagedArtifactRead,
    VerificationOutcome,
    build_first_release_case_agent_run_verifier,
)

from backend.tests import test_case_agent_lawyer_analysis as analysis_fixture
from backend.tests import test_case_agent_supervisor as supervisor_fixture


def _digest(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return sha256(value).hexdigest()


def _candidate() -> tuple[object, bytes, dict[str, object]]:
    projection = analysis_fixture._projection()
    contract, request = prepare_lawyer_analysis_request(
        projection=projection,
        task_id=analysis_fixture.TASK_ID,
        attempt_id=analysis_fixture.ATTEMPT_ID,
        endpoint_host=analysis_fixture.HOST,
    )
    parsed = parse_lawyer_analysis_provider_response(
        analysis_fixture._provider_response(
            analysis_fixture._valid_core(projection)
        ),
        contract=contract,
    )
    payload = compile_lawyer_decision_package_candidate(
        projection=projection,
        contract=contract,
        parsed=parsed,
        external_request_id=request.external_request_id,
        request_hash=request.request_hash,
    )
    return projection, payload, json.loads(payload)


class _ArtifactAccess:
    def __init__(self, content_by_id: dict[str, bytes]) -> None:
        self.content_by_id = content_by_id

    def read_managed_artifact(self, *, artifact, **_kwargs) -> ManagedArtifactRead:
        return ManagedArtifactRead(
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.artifact_kind,
            content=self.content_by_id[artifact.artifact_id],
            source_input_hash=artifact.source_input_hash,
            object_receipt_hash=_digest(f"object:{artifact.artifact_id}"),
            media_type="application/json",
        )


class LawyerDecisionPackageVerifierTests(unittest.TestCase):
    def _format_verifier(self) -> CanonicalJsonArtifactVerifier:
        return CanonicalJsonArtifactVerifier(
            artifact_kind=LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
            allowed_schema_versions=(LAWYER_DECISION_PACKAGE_SCHEMA,),
            payload_kind=LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
        )

    def _managed(self, payload: bytes, task_input_hash: str) -> ManagedArtifactRead:
        return ManagedArtifactRead(
            artifact_id=str(uuid4()),
            artifact_kind=LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
            content=payload,
            source_input_hash=task_input_hash,
            object_receipt_hash=_digest("lawyer-package-object"),
            media_type="application/json",
        )

    def test_format_verifier_accepts_only_the_strict_review_package(self) -> None:
        projection, payload, value = _candidate()
        receipt = self._format_verifier().verify(
            self._managed(payload, projection.task_input_hash)
        )
        self.assertEqual(
            receipt.artifact_kind, LAWYER_DECISION_PACKAGE_ARTIFACT_KIND
        )
        self.assertEqual(
            receipt.declared_external_request_id, value["external_request_id"]
        )

        value["decision_requests"][0]["disposition"] = "AGENT_APPROVED"
        changed = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with self.assertRaisesRegex(
            Exception, "ARTIFACT_LAWYER_PACKAGE_CONTRACT_INVALID"
        ):
            self._format_verifier().verify(
                self._managed(changed, projection.task_input_hash)
            )

    def _run_verification(self, *, omit_last_task_ref: bool):
        helper = supervisor_fixture.CaseAgentSupervisorTests(methodName="runTest")
        helper.setUp()
        projection, payload, value = _candidate()
        task_refs = (
            projection.input_refs[:-1]
            if omit_last_task_ref
            else projection.input_refs
        )
        task = helper.external_task()
        task = replace(
            task,
            input_refs=task_refs,
            input_hash=projection.task_input_hash,
            capability=replace(task.capability, reads_case_objects=task_refs),
        )
        state = helper.state_with_graph(helper.graph((task,)))
        state = helper.approve(state, task.task_id, sequence=3)
        state = helper.start_ready_task(state, sequence=4)
        artifact = ArtifactReceipt(
            artifact_id=str(uuid4()),
            artifact_kind=LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
            content_hash=_digest(payload),
            byte_size=len(payload),
            source_input_hash=projection.task_input_hash,
            managed_derivative=False,
        )
        result = helper.result_receipt(
            state,
            task_id=task.task_id,
            status=ResultStatus.SUCCEEDED,
            external_state=ExternalSubmissionState.SUBMITTED,
            external_request_id=str(value["external_request_id"]),
            artifacts=(artifact,),
            external_calls=1,
        )
        state = reduce_agent_event(
            state,
            helper.event(
                5,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(result),
            ),
        )
        return build_first_release_case_agent_run_verifier(
            artifact_access=_ArtifactAccess({artifact.artifact_id: payload}),
            clock=lambda: datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        ).verify(
            verification_attempt_id=str(uuid4()),
            state=state,
            verifier_actor_id=str(uuid4()),
            execution_actor_id=str(uuid4()),
        )

    def test_full_run_verifier_binds_every_source_and_external_request(self) -> None:
        receipt = self._run_verification(omit_last_task_ref=False)
        self.assertEqual(receipt.outcome, VerificationOutcome.PASSED)

    def test_full_run_verifier_rejects_source_set_mismatch(self) -> None:
        receipt = self._run_verification(omit_last_task_ref=True)
        self.assertEqual(receipt.outcome, VerificationOutcome.FAILED)
        self.assertEqual(
            receipt.error_code,
            "ARTIFACT_LAWYER_PACKAGE_SOURCE_BINDING_INVALID",
        )


if __name__ == "__main__":
    unittest.main()
