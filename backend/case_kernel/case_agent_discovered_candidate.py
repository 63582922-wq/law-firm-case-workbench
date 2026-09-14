"""Versioned, independently readable output for source-bound discovered issues."""
from dataclasses import asdict
from hashlib import sha256
import json
from uuid import UUID

from .case_agent_discovered_analysis import build_discovered_analysis_contract, validate_discovered_analysis_core
from .case_agent_case_context import BoundCaseContextProjection, BoundCaseContextSource, CaseContextSourceType
from .case_agent_planner import PlanningInputStatus
from .case_agent_lawyer_analysis import (LawyerAnalysisBlocked, _json_bytes, _candidate_source,
    _reject_duplicate_object, _reject_json_constant, LAWYER_ANALYSIS_MAX_CANDIDATE_BYTES,
    LAWYER_ANALYSIS_PROVIDER_ID, LAWYER_ANALYSIS_SERVICE_ID, LAWYER_ANALYSIS_MODEL_ID,
    LAWYER_ANALYSIS_PROVIDER_VERSION, price_qwen37_minor_units,
    LAWYER_ANALYSIS_MAX_OUTPUT_TOKENS)

SCHEMA = "agent-discovered-lawyer-analysis-candidate-v1"
FLAGS = {"formal_fact": False, "formal_transaction": False, "legal_conclusion": False,
    "evidence_decision": False, "court_ready": False, "official_numeric_result_authored_by_model": False}


def compile_discovered_candidate(*, projection, contract, parsed, external_request_id, request_hash):
    current = build_discovered_analysis_contract(projection)
    if current != contract:
        raise LawyerAnalysisBlocked("discovered candidate source contract differs")
    discovery = validate_discovered_analysis_core(parsed.core, contract=contract)
    receipt = {"provider_id": LAWYER_ANALYSIS_PROVIDER_ID, "service_id": LAWYER_ANALYSIS_SERVICE_ID,
        "model_id": LAWYER_ANALYSIS_MODEL_ID, "provider_version": LAWYER_ANALYSIS_PROVIDER_VERSION,
        "request_hash": request_hash, "provider_response_sha256": parsed.provider_response_sha256,
        "provider_response_id_hash": sha256(parsed.provider_response_id.encode()).hexdigest(),
        "prompt_tokens": parsed.prompt_tokens, "completion_tokens": parsed.completion_tokens,
        "total_tokens": parsed.total_tokens, "cost_minor_units": parsed.cost_minor_units, "retry_count": 0}
    value = {"schema_version": SCHEMA, "review_status": "NEEDS_LAWYER_REVIEW", **FLAGS,
        "task_input_hash": projection.task_input_hash, "source_hash": contract.source_hash,
        "external_request_id": external_request_id, "projection": asdict(projection),
        "source_catalog": [_candidate_source(source) for source in projection.sources],
        "analysis": parsed.core, "discovery": asdict(discovery), "provider_receipt": receipt}
    payload = _json_bytes(value)
    parse_discovered_candidate(payload)
    return payload


def parse_discovered_candidate(payload):
    if not isinstance(payload, bytes) or not 2 <= len(payload) <= LAWYER_ANALYSIS_MAX_CANDIDATE_BYTES:
        raise LawyerAnalysisBlocked("discovered candidate size differs")
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_object, parse_constant=_reject_json_constant)
        if not isinstance(value, dict) or _json_bytes(value) != payload:
            raise ValueError("noncanonical candidate")
        expected = {"schema_version", "review_status", *FLAGS, "task_input_hash", "source_hash",
            "external_request_id", "projection", "source_catalog", "analysis", "discovery", "provider_receipt"}
        if set(value) != expected or value["schema_version"] != SCHEMA or value["review_status"] != "NEEDS_LAWYER_REVIEW":
            raise ValueError("candidate fields differ")
        if any(value[key] is not False for key in FLAGS):
            raise ValueError("candidate cannot authorize formal use")
        UUID(value["external_request_id"])
        raw = value["projection"]
        sources = tuple(BoundCaseContextSource(**{**source,
            "source_type": CaseContextSourceType(source["source_type"]),
            "status": PlanningInputStatus(source["status"]), "signals": tuple(source["signals"])})
            for source in raw["sources"])
        projection = BoundCaseContextProjection(**{**raw, "sources": sources, "input_refs": tuple(raw["input_refs"])})
        projection.validate()
        contract = build_discovered_analysis_contract(projection)
        if (value["source_hash"] != contract.source_hash or value["task_input_hash"] != projection.task_input_hash
                or value["source_catalog"] != [_candidate_source(source) for source in sources]):
            raise ValueError("candidate source binding differs")
        discovery = validate_discovered_analysis_core(value["analysis"], contract=contract)
        if _json_bytes(asdict(discovery)) != _json_bytes(value["discovery"]):
            raise ValueError("candidate proposed issue binding differs")
        receipt = value["provider_receipt"]
        if set(receipt) != {"provider_id", "service_id", "model_id", "provider_version", "request_hash",
            "provider_response_sha256", "provider_response_id_hash", "prompt_tokens", "completion_tokens",
            "total_tokens", "cost_minor_units", "retry_count"}:
            raise ValueError("provider receipt fields differ")
        for key, expected in (("provider_id", LAWYER_ANALYSIS_PROVIDER_ID), ("service_id", LAWYER_ANALYSIS_SERVICE_ID),
            ("model_id", LAWYER_ANALYSIS_MODEL_ID), ("provider_version", LAWYER_ANALYSIS_PROVIDER_VERSION)):
            if receipt[key] != expected: raise ValueError("provider identity differs")
        for key in ("request_hash", "provider_response_sha256", "provider_response_id_hash"):
            digest = receipt[key]
            if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ValueError("provider digest differs")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_minor_units", "retry_count"):
            if type(receipt[key]) is not int or receipt[key] < 0: raise ValueError("provider usage differs")
        if (receipt["retry_count"] != 0 or receipt["prompt_tokens"] > 1000000
                or receipt["completion_tokens"] > LAWYER_ANALYSIS_MAX_OUTPUT_TOKENS
                or receipt["total_tokens"] != receipt["prompt_tokens"] + receipt["completion_tokens"]
                or receipt["cost_minor_units"] != price_qwen37_minor_units(receipt["prompt_tokens"], receipt["completion_tokens"])):
            raise ValueError("provider cost or usage differs")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise LawyerAnalysisBlocked("discovered candidate failed independent validation") from exc
    return value
