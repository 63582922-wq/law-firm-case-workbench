"""Pure, versioned interpretation of the historical OCR cost-field drift.

No I/O or authority is granted here. A persistence caller must obtain bindings
from current authorised rows, re-read the immutable source, and append the
returned envelope separately. This is NOT an Agent verification receipt and
must never be inserted into the ordinary successful-artifact ledger.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
from uuid import UUID, uuid5

from .case_agent_verifier import (
    ArtifactVerificationRejected,
    _validate_visual_page_candidate_payload,
)


SCHEMA = "visual-candidate-cost-contract-recovery-v1"
POLICY = "visual-candidate-cost-separation-v1"
FAILURE = "ARTIFACT_VISUAL_CONTRACT_INVALID"


class VisualCandidateRecoveryBlocked(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return sha256(value).hexdigest()


@dataclass(frozen=True)
class VisualRecoverySource:
    firm_id: str
    matter_id: str
    matter_version: int
    run_id: str
    run_event_version: int
    run_status: str
    failure_code: str
    task_id: str
    task_status: str
    attempt_id: str
    artifact_id: str
    content_sha256: str
    byte_size: int
    task_input_hash: str
    external_request_id: str
    cost_minor_units: int
    input_refs: tuple[str, ...]


@dataclass(frozen=True)
class VisualRecoveryDraft:
    recovery_id: str
    content_sha256: str
    payload: bytes = field(repr=False)


def prepare_visual_candidate_recovery(*, source: VisualRecoverySource,
                                      original: bytes) -> VisualRecoveryDraft:
    """Rebind, validate and preserve cost metadata without mutating the source.

    Current DB state and object authentication are caller preconditions; this
    pure function additionally rejects accidental cross-case/stale-input mixing.
    It cannot prove semantic completeness or legal correctness of OCR text.
    """
    try:
        for value in (source.firm_id, source.matter_id, source.run_id, source.task_id,
                      source.attempt_id, source.artifact_id, source.external_request_id):
            if str(UUID(value)) != value:
                raise ValueError("noncanonical UUID")
        for value in (source.matter_version, source.run_event_version, source.byte_size):
            if type(value) is not int or value <= 0:
                raise ValueError("invalid version or size")
        if (source.run_status != "FAILED" or source.failure_code != FAILURE
                or source.task_status != "SUCCEEDED"
                or type(source.cost_minor_units) is not int or source.cost_minor_units != 6):
            raise ValueError("not the supported failed-verification/successful-OCR state")
        if (not isinstance(original, bytes) or len(original) > 32 * 1024 * 1024
                or len(original) != source.byte_size or _digest(original) != source.content_sha256):
            raise ValueError("original byte binding differs")
        payload = json.loads(original)
        if _canonical(payload) != original:
            raise ValueError("original is not canonical JSON")
        if (payload.get("cost_basis") != "CONSERVATIVE_RESERVED_EXPOSURE_NOT_INVOICE"
                or type(payload.get("cost_reserve_minor_units")) is not int
                or payload["cost_reserve_minor_units"] != source.cost_minor_units):
            raise ValueError("not the exact historical cost extension")
        provenance = payload["provenance"]
        for key in ("firm_id", "matter_id", "matter_version", "run_id", "task_id",
                    "attempt_id", "external_request_id"):
            if provenance[key] != getattr(source, key):
                raise ValueError(f"{key} differs")
        if (payload["task_input_hash"] != source.task_input_hash
                or payload["external_request_id"] != source.external_request_id
                or tuple(provenance["input_refs"]) != source.input_refs):
            raise ValueError("task/source binding differs")
        # The immutable original remains intact; only this new interpretation is
        # normalized. All source/candidate hashes are still independently checked.
        cost = {key: payload[key] for key in ("cost_basis", "cost_reserve_minor_units")}
        interpreted = {key: value for key, value in payload.items() if key not in cost}
        _validate_visual_page_candidate_payload(interpreted)
        envelope = {
            "schema_version": SCHEMA, "interpretation_policy": POLICY,
            "review_status": "NEEDS_LAWYER_REVIEW", "court_ready": False,
            "formal_fact": False, "legal_conclusion": False,
            "completeness_status": "NOT_VERIFIED_AGAINST_ORIGINAL_PAGE",
            "historical_run_status": source.run_status,
            "historical_failure_code": source.failure_code,
            "source_run_event_version": source.run_event_version,
            "source_artifact_id": source.artifact_id,
            "source_content_sha256": source.content_sha256,
            "source_byte_size": source.byte_size,
            "retained_cost_metadata": cost,
            "interpreted_content_sha256": _digest(_canonical(interpreted)),
            "interpreted_candidate": interpreted,
        }
        content = _canonical(envelope)
        content_hash = _digest(content)
        return VisualRecoveryDraft(
            recovery_id=str(uuid5(UUID(source.artifact_id), f"{POLICY}:{content_hash}")),
            content_sha256=content_hash, payload=content,
        )
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError,
            ArtifactVerificationRejected) as error:
        raise VisualCandidateRecoveryBlocked("OCR recovery source or contract differs") from error
