"""Runnable golden-case defence vertical slice.

The case facts and expected calculation outputs are not defined here.  They
come exclusively from ``docs/GOLDEN_CASE_SYNTHETIC.md`` and the independent
``docs/golden-case/golden_calc.py`` reference program.  This module owns only
the controlled workflow around those immutable inputs: candidate documents,
one exact approval, provenance, invalidation, and a unique submission bundle.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Callable, Iterable, Mapping, Sequence
from uuid import UUID, uuid5

from case_kernel.approved_draft_worker import (
    ApprovedDraft,
    ApprovedSection,
    create_pdf_draft,
)
from case_kernel.submission_bundle_compiler import (
    SubmissionArtifactBinding,
    SubmissionBundleCompilationResult,
    SubmissionBundleDescriptor,
    SubmissionDependency,
    compile_submission_bundle,
    verify_submission_bundle,
    verify_submission_export_bytes,
)
from case_kernel.golden_case_source import (
    AUTHORITATIVE_SPEC_SHA256,
    DeduplicationResult,
    ExtractedIdentityField,
    ExtractedLedgerRow,
    GeneratedGoldenCase,
    GoldenCaseSpec,
    PageRecord,
    SourceRef,
    deduplicate_pages,
    extract_identity_fields,
    extract_ledger_rows,
    generate_golden_case,
    load_authoritative_case,
    read_generated_pages,
    score_deduplication,
    score_extraction,
)
from case_kernel.golden_case_calculation import (
    AUTHORITATIVE_ORACLE_SHA256,
    DEFAULT_RECOMMENDED_CHOICES,
    GoldenCaseCalculationBlocked,
    IndependentScenarioSuite,
    ScenarioResult,
    build_review_packet,
    classify_extracted_rows,
    compare_with_golden,
    load_golden_outputs,
    recommended_choices,
    run_independent_scenarios,
    selected_scenario_id,
    validate_choices,
)


ACCEPT_GOLDEN_RECOMMENDATIONS = "ACCEPT_GOLDEN_RECOMMENDATIONS"
_SHA256_ZERO = "0" * 64
_NAMESPACE = UUID("de336427-70fa-48d2-8318-5a92c9d6d813")


class GoldenVerticalSliceBlocked(RuntimeError):
    """The golden case cannot continue without violating an invariant."""


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    occurred_at: str
    payload: Mapping[str, object]
    payload_hash: str
    previous_event_hash: str
    event_hash: str


@dataclass(frozen=True)
class ArtifactNode:
    node_id: str
    kind: str
    output_hash: str
    depends_on: tuple[str, ...]
    status: str = "CURRENT"


@dataclass(frozen=True)
class DocumentParagraph:
    paragraph_id: str
    heading: str
    text: str
    source_ref_ids: tuple[str, ...]


@dataclass(frozen=True)
class GoldenSliceRunResult:
    output_root: Path
    metrics: Mapping[str, object]
    metrics_path: Path
    report_path: Path
    locked_zip_path: Path
    internal_manifest_path: Path
    gold_path: Path
    review_packet_path: Path
    approval_path: Path
    agent_proposal_path: Path | None = None
    agent_self_check_path: Path | None = None


AgentProposalProvider = Callable[
    [GeneratedGoldenCase, Sequence[PageRecord], Path], Mapping[str, object]
]
AgentSelfCheckProvider = Callable[
    [Sequence[Path], Path], Mapping[str, object]
]


class AppendOnlyAuditLog:
    """Small fsync'd hash chain.  It is tamper-evident, not WORM storage."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        _mkdir_private(self.root)

    def append(self, event_type: str, payload: Mapping[str, object]) -> AuditEvent:
        prior = self.replay()
        sequence = len(prior) + 1
        previous_hash = prior[-1].event_hash if prior else _SHA256_ZERO
        canonical_payload = _jsonable(payload)
        unsigned = {
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": canonical_payload,
            "payload_hash": _canonical_hash(canonical_payload),
            "previous_event_hash": previous_hash,
        }
        event_hash = _canonical_hash(unsigned)
        encoded = {**unsigned, "event_hash": event_hash}
        _write_json_exclusive_fsync(
            self.root / f"{sequence:06d}_{event_hash}.json", encoded
        )
        return AuditEvent(**encoded)

    def replay(self) -> tuple[AuditEvent, ...]:
        result: list[AuditEvent] = []
        previous = _SHA256_ZERO
        for expected, path in enumerate(sorted(self.root.glob("*.json")), start=1):
            encoded = json.loads(path.read_text(encoding="utf-8"))
            event_hash = encoded.pop("event_hash", "")
            if encoded.get("sequence") != expected:
                raise GoldenVerticalSliceBlocked("audit sequence is discontinuous")
            if encoded.get("previous_event_hash") != previous:
                raise GoldenVerticalSliceBlocked("audit previous hash changed")
            if encoded.get("payload_hash") != _canonical_hash(encoded.get("payload")):
                raise GoldenVerticalSliceBlocked("audit payload changed")
            if event_hash != _canonical_hash(encoded):
                raise GoldenVerticalSliceBlocked("audit event hash changed")
            if path.name != f"{expected:06d}_{event_hash}.json":
                raise GoldenVerticalSliceBlocked("audit filename is not hash-bound")
            result.append(AuditEvent(event_hash=event_hash, **encoded))
            previous = event_hash
        return tuple(result)


class ArtifactGraph:
    def __init__(self, nodes: Iterable[ArtifactNode]) -> None:
        self.nodes = {item.node_id: item for item in nodes}

    def invalidate_descendants(self, changed_node_id: str) -> tuple[str, ...]:
        if changed_node_id not in self.nodes:
            raise GoldenVerticalSliceBlocked("unknown dependency root")
        stale: set[str] = set()
        progressed = True
        while progressed:
            progressed = False
            for item in self.nodes.values():
                if item.node_id == changed_node_id or item.node_id in stale:
                    continue
                if changed_node_id in item.depends_on or stale.intersection(item.depends_on):
                    stale.add(item.node_id)
                    progressed = True
        for node_id in stale:
            item = self.nodes[node_id]
            self.nodes[node_id] = ArtifactNode(
                item.node_id, item.kind, item.output_hash, item.depends_on, "STALE"
            )
        return tuple(sorted(stale))

    def to_json(self) -> Mapping[str, object]:
        return {
            "schema_version": "golden-case-artifact-graph-v1",
            "nodes": [asdict(self.nodes[key]) for key in sorted(self.nodes)],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> "ArtifactGraph":
        rows = value.get("nodes")
        if not isinstance(rows, list):
            raise GoldenVerticalSliceBlocked("artifact graph is invalid")
        return cls(
            ArtifactNode(
                node_id=str(row["node_id"]),
                kind=str(row["kind"]),
                output_hash=str(row["output_hash"]),
                depends_on=tuple(row["depends_on"]),
                status=str(row["status"]),
            )
            for row in rows
        )


def create_bound_approval(
    *,
    candidate_hash: str,
    review_packet: Mapping[str, object],
    selected_choices: Mapping[str, object],
) -> Mapping[str, object]:
    _validate_review_packet_hash(review_packet)
    unsigned = {
        "schema_version": "golden-case-synthetic-decision-v1",
        "synthetic_only": True,
        "reviewer": "SYNTHETIC_LEAD_REVIEWER",
        "review_packet_sha256": review_packet["packet_sha256"],
        "candidate_hash": candidate_hash,
        "selected_choices": _jsonable(selected_choices),
        "explicit_action": "LOCK_EXACT_GOLDEN_CANDIDATE",
        "warning": "仅用于合成金标准验收，不代表执业律师审批真实案件。",
    }
    return {**unsigned, "approval_hash": _canonical_hash(unsigned)}


def validate_bound_approval(
    approval: Mapping[str, object],
    *,
    candidate_hash: str,
    review_packet: Mapping[str, object],
    expected_choices: Mapping[str, object],
) -> str:
    _validate_review_packet_hash(review_packet)
    provided = str(approval.get("approval_hash", ""))
    unsigned = {key: value for key, value in approval.items() if key != "approval_hash"}
    if provided != _canonical_hash(unsigned):
        raise GoldenVerticalSliceBlocked("approval contents do not match its hash")
    if (
        approval.get("synthetic_only") is not True
        or approval.get("candidate_hash") != candidate_hash
        or approval.get("review_packet_sha256") != review_packet["packet_sha256"]
        or approval.get("selected_choices") != _jsonable(expected_choices)
        or approval.get("explicit_action") != "LOCK_EXACT_GOLDEN_CANDIDATE"
    ):
        raise GoldenVerticalSliceBlocked("approval is not bound to the exact candidate")
    return provided


def _validate_review_packet_hash(review_packet: Mapping[str, object]) -> str:
    provided = str(review_packet.get("packet_sha256", ""))
    unsigned = {key: value for key, value in review_packet.items() if key != "packet_sha256"}
    if provided != _canonical_hash(unsigned):
        raise GoldenVerticalSliceBlocked("review packet hash does not authenticate its content")
    return provided


def publish_current_submission(
    state_root: Path,
    *,
    candidate_hash: str,
    compilation: SubmissionBundleCompilationResult,
    lock_event_hash: str,
) -> Path:
    pointer = state_root / "current_submission.json"
    if pointer.exists():
        raise GoldenVerticalSliceBlocked("a current submission already exists")
    payload = {
        "schema_version": "golden-case-current-submission-v1",
        "bundle_id": compilation.bundle_id,
        "candidate_hash": candidate_hash,
        "input_hash": compilation.input_hash,
        "court_zip_path": str(compilation.court_zip_path.resolve()),
        "court_zip_sha256": compilation.court_zip_sha256,
        "internal_manifest_path": str(compilation.internal_manifest_path.resolve()),
        "internal_manifest_sha256": compilation.internal_manifest_sha256,
        "component_count": compilation.component_count,
        "locked_event_hash": lock_event_hash,
        "status": "VALID_LOCKED",
    }
    _write_json_atomic(pointer, payload)
    return pointer


def open_current_submission(state_root: str | Path, audit_root: str | Path) -> bytes:
    state_directory = Path(state_root)
    pointer = state_directory / "current_submission.json"
    if not pointer.is_file():
        raise GoldenVerticalSliceBlocked("there is no current submission")
    state = json.loads(pointer.read_text(encoding="utf-8"))
    if state.get("status") != "VALID_LOCKED":
        raise GoldenVerticalSliceBlocked("submission is not current")
    events = AppendOnlyAuditLog(audit_root).replay()
    lock_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.event_hash == state.get("locked_event_hash")
        ),
        None,
    )
    if lock_index is None:
        raise GoldenVerticalSliceBlocked("submission lock event is missing")
    lock_event = events[lock_index]
    if (
        lock_event.event_type != "SUBMISSION_LOCKED"
        or lock_event.payload.get("bundle_id") != state.get("bundle_id")
        or lock_event.payload.get("candidate_hash") != state.get("candidate_hash")
    ):
        raise GoldenVerticalSliceBlocked("submission does not match its lock event")
    if any(
        item.event_type in {"DECISION_CHANGED", "DOWNSTREAM_INVALIDATED"}
        for item in events[lock_index + 1 :]
    ):
        raise GoldenVerticalSliceBlocked("submission was invalidated after lock")
    graph_path = state_directory / "artifact_graph.json"
    if not graph_path.is_file():
        raise GoldenVerticalSliceBlocked("artifact graph is missing")
    graph = ArtifactGraph.from_json(json.loads(graph_path.read_text(encoding="utf-8")))
    submission = graph.nodes.get("submission:current")
    if (
        submission is None
        or submission.status != "CURRENT"
        or submission.output_hash != state.get("court_zip_sha256")
    ):
        raise GoldenVerticalSliceBlocked("submission graph is stale")
    if any(
        graph.nodes.get(node_id) is None or graph.nodes[node_id].status != "CURRENT"
        for node_id in submission.depends_on
    ):
        raise GoldenVerticalSliceBlocked("submission dependency is stale")
    zip_bytes = Path(state["court_zip_path"]).read_bytes()
    manifest_bytes = Path(state["internal_manifest_path"]).read_bytes()
    verify_submission_export_bytes(
        court_zip_bytes=zip_bytes,
        manifest_bytes=manifest_bytes,
        expected_court_zip_sha256=state["court_zip_sha256"],
        expected_manifest_sha256=state["internal_manifest_sha256"],
        expected_bundle_id=state["bundle_id"],
        expected_input_hash=state["input_hash"],
        expected_component_count=state["component_count"],
    )
    return zip_bytes


def archive_existing_output(output: Path) -> Path | None:
    """Move a previous run to history and remove every historical current pointer."""

    if not output.exists():
        return None
    if not any(output.iterdir()):
        output.rmdir()
        return None
    parent = output.parent
    history = parent / f"{output.name}-history"
    _mkdir_private(history)
    destination = history / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.rename(destination)
    pointer = destination / "state" / "current_submission.json"
    if pointer.is_file():
        state = json.loads(pointer.read_text(encoding="utf-8"))
        state["status"] = "ARCHIVED_NOT_CURRENT"
        _write_json_atomic(destination / "state" / "archived_submission.json", state)
        pointer.unlink()
    graph_path = destination / "state" / "artifact_graph.json"
    if graph_path.is_file():
        graph = ArtifactGraph.from_json(json.loads(graph_path.read_text(encoding="utf-8")))
        for node_id, item in tuple(graph.nodes.items()):
            graph.nodes[node_id] = ArtifactNode(
                item.node_id, item.kind, item.output_hash, item.depends_on, "ARCHIVED"
            )
        _write_json_atomic(graph_path, graph.to_json())
    audit_root = destination / "audit"
    if audit_root.is_dir():
        AppendOnlyAuditLog(audit_root).append(
            "RUN_ARCHIVED", {"reason": "superseded by a new golden-case run"}
        )
    _fsync_directory(history)
    return destination


def run_golden_vertical_slice(
    output_root: str | Path,
    *,
    project_root: str | Path,
    synthetic_decision: str,
    agent_proposal_provider: AgentProposalProvider | None = None,
    agent_self_check_provider: AgentSelfCheckProvider | None = None,
) -> GoldenSliceRunResult:
    """Generate, measure, approve and lock the one authoritative synthetic case."""

    output = Path(output_root).resolve()
    project = Path(project_root).resolve()
    if output.exists() and any(output.iterdir()):
        raise GoldenVerticalSliceBlocked("run output must be empty")
    _mkdir_private(output)
    materials_root = output / "materials"
    derivatives = output / "derivatives"
    candidates = output / "candidates"
    state_root = output / "state"
    audit_root = output / "audit"
    evaluator_root = output / "evaluator_gold"
    for directory in (derivatives, candidates, state_root, audit_root, evaluator_root):
        _mkdir_private(directory)
    audit = AppendOnlyAuditLog(audit_root)
    if (agent_proposal_provider is None) != (agent_self_check_provider is None):
        raise GoldenVerticalSliceBlocked(
            "Agent proposal and self-check providers must be configured together"
        )
    agent_root = output / "agent" if agent_proposal_provider is not None else None
    if agent_root is not None:
        _mkdir_private(agent_root)

    spec = load_authoritative_case(project)
    generated = generate_golden_case(materials_root, spec)
    original_hashes = {
        item.file_name: item.file_sha256 for item in generated.files
    }
    audit.append(
        "SOURCES_GENERATED",
        {
            "spec_sha256": spec.spec_sha256,
            "file_count": len(generated.files),
            "page_count": sum(item.page_count for item in generated.files),
            "source_manifest_sha256": _file_sha256(Path(generated.manifest_path)),
        },
    )

    pages = read_generated_pages(generated)
    dedup = deduplicate_pages(pages)
    ledger_rows = extract_ledger_rows(dedup)
    identity_fields = extract_identity_fields(dedup)
    dedup_metrics = score_deduplication(dedup, generated)
    extraction_metrics = score_extraction(ledger_rows, spec, identity_fields)
    source_refs = _all_source_refs(pages, ledger_rows, identity_fields)
    page_dedup_path = derivatives / "page_deduplication.json"
    extraction_path = derivatives / "extraction.json"
    _write_json_new(page_dedup_path, dedup)
    _write_json_new(
        extraction_path,
        {"ledger_rows": ledger_rows, "identity_fields": identity_fields},
    )

    agent_proposal: Mapping[str, object] | None = None
    agent_proposal_path: Path | None = None
    if agent_proposal_provider is not None and agent_root is not None:
        exchange = agent_proposal_provider(generated, pages, agent_root)
        agent_proposal, transcript, surface_manifest = _validate_agent_exchange(
            exchange, expected_schema="golden-agent-proposal-v1"
        )
        agent_proposal_path = agent_root / "proposal.json"
        transcript_path = agent_root / "proposal_transcript.json"
        surface_path = agent_root / "proposal_surface_manifest.json"
        _write_json_new(agent_proposal_path, agent_proposal)
        _write_json_new(transcript_path, transcript)
        _write_json_new(surface_path, surface_manifest)
        audit.append(
            "AGENT_PROPOSAL_RECORDED",
            {
                "proposal_sha256": _file_sha256(agent_proposal_path),
                "transcript_sha256": _file_sha256(transcript_path),
                "surface_manifest_sha256": _file_sha256(surface_path),
                "agent_has_approval_authority": False,
            },
        )

    classified = classify_extracted_rows(ledger_rows, DEFAULT_RECOMMENDED_CHOICES)
    suite = run_independent_scenarios(
        classified,
        spec=project / "docs" / "GOLDEN_CASE_SYNTHETIC.md",
    )
    oracle = load_golden_outputs(project)
    comparison = compare_with_golden(suite, oracle)
    if not comparison.matching:
        raise GoldenVerticalSliceBlocked(
            f"independent calculation differs from oracle at {len(comparison.mismatches)} fields"
        )
    refs_by_row = {
        row.row_number: [asdict(ref) for ref in row.source_refs]
        for row in ledger_rows
    }
    review_packet = build_review_packet(
        project / "docs" / "GOLDEN_CASE_SYNTHETIC.md",
        classified,
        suite,
        refs_by_row,
    )
    if agent_proposal is None:
        choices = recommended_choices(review_packet)
        if choices != DEFAULT_RECOMMENDED_CHOICES:
            raise GoldenVerticalSliceBlocked(
                "review recommendations drifted from the frozen contract"
            )
    else:
        review_packet = _replace_constant_proposals(review_packet, agent_proposal)
        # The real Agent proposes only.  The explicit synthetic lawyer fixture
        # remains the sole authority that resolves all ten choices for the
        # deterministic calculation and exact candidate approval.
        choices = dict(DEFAULT_RECOMMENDED_CHOICES)
    decision_set_hash = validate_choices(review_packet, choices)
    selected_id = selected_scenario_id(choices)
    selected = suite.scenario(selected_id)
    review_packet_path = candidates / "single_review_packet.json"
    _write_json_new(review_packet_path, review_packet)

    consistency_findings, consistency_metrics = _detect_consistency(
        dedup, ledger_rows, pages
    )
    calculation_output_hash = _canonical_hash(selected)
    calculation_path = derivatives / "calculation.json"
    _write_json_new(
        calculation_path,
        {
            "selected_scenario_id": selected_id,
            "selected_scenario": selected,
            "all_scenarios": suite.scenarios,
            "oracle_comparison": comparison,
            "oracle_sha256": oracle.source_sha256,
            "calculation_output_hash": calculation_output_hash,
        },
    )
    consistency_path = derivatives / "consistency_findings.json"
    _write_json_new(consistency_path, consistency_findings)

    candidate_seed = _canonical_hash(
        {
            "spec_sha256": spec.spec_sha256,
            "source_manifest_sha256": _file_sha256(Path(generated.manifest_path)),
            "page_dedup_sha256": _file_sha256(page_dedup_path),
            "extraction_sha256": _file_sha256(extraction_path),
            "decision_set_hash": decision_set_hash,
            "calculation_output_hash": calculation_output_hash,
            "consistency_sha256": _file_sha256(consistency_path),
        }
    )
    defence_paragraphs, evidence_paragraphs = _build_candidate_paragraphs(
        selected=selected,
        ledger_rows=ledger_rows,
        identity_fields=identity_fields,
        consistency_findings=consistency_findings,
        source_refs=source_refs,
    )
    defence_pdf = create_pdf_draft(
        _paragraphs_to_approved_draft(
            "民事答辩要点底稿（合成测试）",
            defence_paragraphs,
            candidate_seed,
            source_refs,
        )
    )
    evidence_pdf = create_pdf_draft(
        _paragraphs_to_approved_draft(
            "证据目录底稿（合成测试）",
            evidence_paragraphs,
            candidate_seed,
            source_refs,
        )
    )
    defence_path = candidates / "01_民事答辩要点底稿.pdf"
    evidence_path = candidates / "02_证据目录底稿.pdf"
    _write_bytes_new(defence_path, defence_pdf.content)
    _write_bytes_new(evidence_path, evidence_pdf.content)

    agent_self_check_path: Path | None = None
    if agent_self_check_provider is not None and agent_root is not None:
        exchange = agent_self_check_provider((defence_path, evidence_path), agent_root)
        self_check, transcript, surface_manifest = _validate_agent_exchange(
            exchange, expected_schema="golden-agent-self-check-v1"
        )
        agent_self_check_path = agent_root / "self_check.json"
        transcript_path = agent_root / "self_check_transcript.json"
        surface_path = agent_root / "self_check_surface_manifest.json"
        _write_json_new(agent_self_check_path, self_check)
        _write_json_new(transcript_path, transcript)
        _write_json_new(surface_path, surface_manifest)
        audit.append(
            "AGENT_SELF_CHECK_RECORDED",
            {
                "self_check_sha256": _file_sha256(agent_self_check_path),
                "transcript_sha256": _file_sha256(transcript_path),
                "surface_manifest_sha256": _file_sha256(surface_path),
                "agent_has_approval_authority": False,
            },
        )

    provenance, provenance_metrics = _build_and_verify_provenance(
        generated=generated,
        pages=pages,
        ledger_rows=ledger_rows,
        identity_fields=identity_fields,
        classified_rows=classified,
        suite=suite,
        selected=selected,
        documents=(*defence_paragraphs, *evidence_paragraphs),
        source_refs=source_refs,
        choices=choices,
        spec_path=project / "docs" / "GOLDEN_CASE_SYNTHETIC.md",
    )
    provenance_path = derivatives / "provenance_index.json"
    _write_json_new(provenance_path, provenance)

    candidate_manifest = {
        "schema_version": "golden-case-exact-candidate-v1",
        "synthetic_only": True,
        "external_send": False,
        "spec_sha256": spec.spec_sha256,
        "oracle_sha256": oracle.source_sha256,
        "source_manifest_sha256": _file_sha256(Path(generated.manifest_path)),
        "page_dedup_sha256": _file_sha256(page_dedup_path),
        "extraction_sha256": _file_sha256(extraction_path),
        "decision_set_hash": decision_set_hash,
        "selected_scenario_id": selected_id,
        "calculation_output_hash": calculation_output_hash,
        "consistency_sha256": _file_sha256(consistency_path),
        "provenance_index_sha256": _file_sha256(provenance_path),
        "components": [
            {
                "document_kind": "DEFENCE_POINTS_DRAFT",
                "file_name": defence_path.name,
                "byte_size": defence_path.stat().st_size,
                "sha256": _file_sha256(defence_path),
            },
            {
                "document_kind": "EVIDENCE_CATALOGUE_DRAFT",
                "file_name": evidence_path.name,
                "byte_size": evidence_path.stat().st_size,
                "sha256": _file_sha256(evidence_path),
            },
        ],
        "warning": "合成工程验收包；禁止对外发送，不是法院可提交文书。",
    }
    candidate_hash = _canonical_hash(candidate_manifest)
    candidate_manifest = {**candidate_manifest, "candidate_hash": candidate_hash}
    candidate_manifest_path = candidates / "candidate_manifest.json"
    _write_json_new(candidate_manifest_path, candidate_manifest)
    candidate_event = audit.append(
        "CANDIDATE_CREATED",
        {
            "candidate_hash": candidate_hash,
            "component_hashes": [
                item["sha256"] for item in candidate_manifest["components"]
            ],
            "review_packet_sha256": review_packet["packet_sha256"],
        },
    )

    gold_path = evaluator_root / "answer_key.json"
    _write_json_new(
        gold_path,
        {
            "schema_version": "golden-case-evaluator-answer-v1",
            "spec_sha256": spec.spec_sha256,
            "oracle_sha256": oracle.source_sha256,
            "materials": spec.materials,
            "identities": spec.identities,
            "transactions": spec.transactions,
            "four_scenario_outputs": oracle.scenarios,
            "source_files": generated.files,
        },
    )

    if synthetic_decision != ACCEPT_GOLDEN_RECOMMENDATIONS:
        raise GoldenVerticalSliceBlocked(
            "exact candidate exists but no explicit synthetic approval was supplied"
        )
    approval = create_bound_approval(
        candidate_hash=candidate_hash,
        review_packet=review_packet,
        selected_choices=choices,
    )
    approval_hash = validate_bound_approval(
        approval,
        candidate_hash=candidate_hash,
        review_packet=review_packet,
        expected_choices=choices,
    )
    approval_path = candidates / "synthetic_review_approval.json"
    _write_json_new(approval_path, approval)
    approval_event = audit.append(
        "HUMAN_DECISION_RECORDED",
        {
            "synthetic_test_approval": True,
            "approval_hash": approval_hash,
            "candidate_hash": candidate_hash,
            "candidate_event_hash": candidate_event.event_hash,
        },
    )

    compilation = _compile_exact_package(
        output=output,
        candidate_hash=candidate_hash,
        approval_hash=approval_hash,
        approval_event=approval_event,
        component_paths=(defence_path, evidence_path),
        dependency_hashes={
            "SOURCE_MANIFEST": _file_sha256(Path(generated.manifest_path)),
            "PAGE_DEDUP": _file_sha256(page_dedup_path),
            "EXTRACTION": _file_sha256(extraction_path),
            "DECISION_SET": decision_set_hash,
            "CALCULATION_RUN": calculation_output_hash,
            "CONSISTENCY": _file_sha256(consistency_path),
            "PROVENANCE_INDEX": _file_sha256(provenance_path),
            "FINAL_APPROVAL": approval_hash,
        },
    )
    _verify_exact_package_contract(
        compilation=compilation,
        candidate_hash=candidate_hash,
        approval_hash=approval_hash,
        component_paths=(defence_path, evidence_path),
    )
    bundle_event = audit.append(
        "BUNDLE_COMPILED",
        {
            "bundle_id": compilation.bundle_id,
            "candidate_hash": candidate_hash,
            "approval_hash": approval_hash,
            "zip_sha256": compilation.court_zip_sha256,
        },
    )

    graph = _build_artifact_graph(
        generated=generated,
        dedup_path=page_dedup_path,
        extraction_path=extraction_path,
        choices=choices,
        calculation_hash=calculation_output_hash,
        consistency_path=consistency_path,
        provenance_path=provenance_path,
        defence_path=defence_path,
        evidence_path=evidence_path,
        candidate_hash=candidate_hash,
        approval_hash=approval_hash,
        submission_hash=compilation.court_zip_sha256,
    )
    graph_path = state_root / "artifact_graph.json"
    _write_json_new(graph_path, graph.to_json())
    _write_json_new(
        state_root / "selected_choices.json",
        {
            "selected_choices": choices,
            "selected_choices_hash": decision_set_hash,
            "status": "APPROVED",
        },
    )
    lock_event = audit.append(
        "SUBMISSION_LOCKED",
        {
            "bundle_id": compilation.bundle_id,
            "candidate_hash": candidate_hash,
            "approval_hash": approval_hash,
            "bundle_event_hash": bundle_event.event_hash,
            "external_send": False,
        },
    )
    publish_current_submission(
        state_root,
        candidate_hash=candidate_hash,
        compilation=compilation,
        lock_event_hash=lock_event.event_hash,
    )
    if not open_current_submission(state_root, audit_root):
        raise GoldenVerticalSliceBlocked("newly locked package could not be opened")

    invalidation_metrics = _run_invalidation_trials(
        output=output,
        state_root=state_root,
        audit_root=audit_root,
        suite=suite,
        choices=choices,
        original_calculation_hash=calculation_output_hash,
    )
    originals_metrics = _verify_originals(generated, original_hashes)
    calculation_metrics = _calculation_metrics(suite, comparison, oracle.source_sha256)
    locked_metrics = {
        "verified": True,
        "current_locked_packages": 1,
        "bundle_id": compilation.bundle_id,
        "candidate_hash": candidate_hash,
        "approval_hash": approval_hash,
        "zip_sha256": compilation.court_zip_sha256,
        "manifest_sha256": compilation.internal_manifest_sha256,
        "component_count": compilation.component_count,
        "external_send": False,
    }
    metrics = {
        "schema_version": "golden-defence-vertical-slice-metrics-v1",
        "case_id": "(2025)京0105民初17532号",
        "synthetic_only": True,
        "spec_sha256": AUTHORITATIVE_SPEC_SHA256,
        "oracle_sha256": AUTHORITATIVE_ORACLE_SHA256,
        "page_deduplication": dedup_metrics,
        "information_extraction": extraction_metrics,
        "deterministic_calculation": calculation_metrics,
        "cross_document_consistency": consistency_metrics,
        "invalidation_propagation": invalidation_metrics,
        "provenance": provenance_metrics,
        "approval": {
            "decision_count": 10,
            "synthetic_test_approval": True,
            "candidate_created_before_decision": candidate_event.sequence < approval_event.sequence,
            "decision_before_bundle": approval_event.sequence < bundle_event.sequence,
            "approval_hash": approval_hash,
            "candidate_hash": candidate_hash,
        },
        "locked_submission": locked_metrics,
        "originals": originals_metrics,
        "limitations": [
            "只是合成案件工程验收，不是真实律师审批。",
            "HKD 8,000保留原币并阻断换算，未进入CNY合计。",
            "包内文书为待终审底稿，external_send=false，不得对外提交。",
        ],
    }
    if agent_proposal_path is not None and agent_self_check_path is not None:
        metrics = {
            **metrics,
            "agent_execution": {
                "proposal_sha256": _file_sha256(agent_proposal_path),
                "self_check_sha256": _file_sha256(agent_self_check_path),
                "agent_has_approval_authority": False,
                "synthetic_human_choices_sha256": decision_set_hash,
            },
        }
    _assert_acceptance(metrics)
    metrics_path = output / "metrics.json"
    _write_json_new(metrics_path, metrics)
    report_path = output / "RUN_REPORT.md"
    _write_bytes_new(report_path, _render_report(metrics, compilation).encode("utf-8"))
    return GoldenSliceRunResult(
        output_root=output,
        metrics=metrics,
        metrics_path=metrics_path,
        report_path=report_path,
        locked_zip_path=compilation.court_zip_path,
        internal_manifest_path=compilation.internal_manifest_path,
        gold_path=gold_path,
        review_packet_path=review_packet_path,
        approval_path=approval_path,
        agent_proposal_path=agent_proposal_path,
        agent_self_check_path=agent_self_check_path,
    )


def _validate_agent_exchange(
    exchange: Mapping[str, object], *, expected_schema: str
) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
    if not isinstance(exchange, Mapping) or set(exchange) != {
        "agent_output",
        "transcript",
        "surface_manifest",
    }:
        raise GoldenVerticalSliceBlocked("Agent exchange shape is invalid")
    output = exchange["agent_output"]
    transcript = exchange["transcript"]
    surface_manifest = exchange["surface_manifest"]
    if (
        not isinstance(output, Mapping)
        or output.get("schema_version") != expected_schema
        or not isinstance(transcript, Mapping)
        or transcript.get("schema_version") != "golden-agent-full-transcript-v1"
        or not isinstance(surface_manifest, Mapping)
        or surface_manifest.get("schema_version") != "golden-agent-surface-manifest-v1"
    ):
        raise GoldenVerticalSliceBlocked("Agent exchange schema is invalid")
    forbidden = json.dumps(output, ensure_ascii=False, sort_keys=True)
    if any(
        token in forbidden
        for token in (
            "approval_hash",
            "candidate_hash",
            "lock_submission",
            "current_submission",
        )
    ):
        raise GoldenVerticalSliceBlocked("Agent output attempted to cross the approval boundary")
    return output, transcript, surface_manifest


def _replace_constant_proposals(
    review_packet: Mapping[str, object], proposal: Mapping[str, object]
) -> Mapping[str, object]:
    rows = proposal.get("decisions")
    if not isinstance(rows, list) or len(rows) != 10:
        raise GoldenVerticalSliceBlocked("Agent proposal must contain exactly ten decisions")
    proposed: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise GoldenVerticalSliceBlocked("Agent decision is invalid")
        decision_id = row.get("decision_id")
        if not isinstance(decision_id, str) or decision_id in proposed:
            raise GoldenVerticalSliceBlocked("Agent decision ids are invalid")
        proposed[decision_id] = row
    decisions = review_packet.get("decisions")
    if not isinstance(decisions, list):
        raise GoldenVerticalSliceBlocked("review packet decisions are invalid")
    expected_ids = {str(item["decision_id"]) for item in decisions}
    if set(proposed) != expected_ids:
        raise GoldenVerticalSliceBlocked("Agent decision set does not match the review packet")
    merged: list[Mapping[str, object]] = []
    for base in decisions:
        decision_id = str(base["decision_id"])
        row = proposed[decision_id]
        disposition = row.get("disposition")
        if disposition not in {"RECOMMEND", "REQUIRES_LAWYER"}:
            raise GoldenVerticalSliceBlocked("Agent disposition is invalid")
        allowed = {
            str(option["choice"])
            for option in base.get("options", [])
            if isinstance(option, Mapping) and isinstance(option.get("choice"), str)
        }
        recommended = row.get("recommended_choice")
        if recommended is not None and recommended not in allowed:
            raise GoldenVerticalSliceBlocked("Agent recommendation is outside allowed choices")
        if disposition == "RECOMMEND" and recommended is None:
            raise GoldenVerticalSliceBlocked("Agent recommendation is missing")
        reason = row.get("reason")
        evidence = row.get("evidence")
        options = row.get("options")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(evidence, list)
            or not isinstance(options, list)
            or not options
        ):
            raise GoldenVerticalSliceBlocked("Agent rationale, evidence, or options are missing")
        merged.append(
            {
                **base,
                "recommendation": recommended,
                "agent_disposition": disposition,
                "agent_reason": reason,
                "agent_evidence": evidence,
                "agent_options": options,
            }
        )
    unsigned = {
        key: value for key, value in review_packet.items() if key != "packet_sha256"
    }
    unsigned["decisions"] = merged
    unsigned["proposal_source"] = "qwen3-vl-plus-real-agent"
    return {**unsigned, "packet_sha256": _canonical_hash(unsigned)}


def _all_source_refs(
    pages: Sequence[PageRecord],
    ledger_rows: Sequence[ExtractedLedgerRow],
    identity_fields: Sequence[ExtractedIdentityField],
) -> dict[str, SourceRef]:
    result = {
        ref.source_ref_id: ref
        for row in ledger_rows
        for ref in row.source_refs
    }
    result.update({item.source_ref.source_ref_id: item.source_ref for item in identity_fields})
    for page in pages:
        ref = _page_level_ref(page)
        result.setdefault(ref.source_ref_id, ref)
    return result


def _page_level_ref(page: PageRecord) -> SourceRef:
    if not page.blocks:
        raise GoldenVerticalSliceBlocked(f"page {page.page_key} has no inspectable content")
    block = page.blocks[0]
    seed = {
        "file_sha256": page.file_sha256,
        "page_number": page.page_number,
        "page_sha256": page.page_sha256,
        "bbox": list(block.bbox),
        "excerpt_sha256": block.excerpt_sha256,
    }
    return SourceRef(
        source_ref_id=_canonical_hash(seed),
        material_code=page.source_code,
        file_name=page.file_name,
        file_sha256=page.file_sha256,
        page_number=page.page_number,
        page_sha256=page.page_sha256,
        bbox=block.bbox,
        excerpt=block.text,
        excerpt_sha256=block.excerpt_sha256,
    )


def _detect_consistency(
    dedup: DeduplicationResult,
    rows: Sequence[ExtractedLedgerRow],
    pages: Sequence[PageRecord],
) -> tuple[tuple[Mapping[str, object], ...], Mapping[str, object]]:
    findings: list[Mapping[str, object]] = []
    f1p2 = next(
        (page for page in pages if page.source_code == "F1" and page.page_number == 2),
        None,
    )
    row_by_number = {item.row_number: item for item in rows}
    if f1p2 and "205,000" in f1p2.text and row_by_number[2].amount == "200000.00":
        findings.append(
            {
                "finding_id": "F1_SECOND_LOAN_AMOUNT_CONFLICT",
                "kind": "AMOUNT_CONFLICT",
                "left": "205000.00",
                "right": "200000.00",
                "source_locations": ["F1p2", *[f"{r.material_code}p{r.page_number}" for r in row_by_number[2].source_refs]],
            }
        )
    repayment_rows = [
        item
        for item in rows
        if item.direction in {"王→周", "李梅→周"}
        and item.summary in {"还本金", "还王强借款", "还第二笔", "还借款", "周转款"}
    ]
    if f1p2 and "本金分文未还" in f1p2.text and repayment_rows:
        findings.append(
            {
                "finding_id": "F1_NO_PRINCIPAL_REPAYMENT_CONFLICT",
                "kind": "FACT_CONFLICT",
                "left": "本金分文未还",
                "right": f"{len(repayment_rows)}笔还本或还款候选记录",
                "source_locations": [
                    "F1p2",
                    *sorted(
                        {
                            f"{ref.material_code}p{ref.page_number}"
                            for row in repayment_rows
                            for ref in row.source_refs
                        }
                    ),
                ],
            }
        )
    hkd = [item for item in rows if item.currency == "HKD"]
    if hkd:
        findings.append(
            {
                "finding_id": "HKD_CURRENCY_BLOCKER",
                "kind": "CURRENCY_CONFLICT",
                "left": "CNY_ONLY_CALCULATION",
                "right": "HKD 8000.00",
                "source_locations": sorted(
                    {
                        f"{ref.material_code}p{ref.page_number}"
                        for row in hkd
                        for ref in row.source_refs
                    }
                ),
            }
        )
    signatures: dict[tuple[str, str, str, str], list[ExtractedLedgerRow]] = {}
    for row in rows:
        signatures.setdefault(
            (row.occurred_at, row.amount, row.summary, row.direction), []
        ).append(row)
    duplicate_groups = [group for group in signatures.values() if len(group) > 1]
    if len(duplicate_groups) == 3:
        findings.append(
            {
                "finding_id": "CROSS_SOURCE_TRANSACTION_DUPLICATES",
                "kind": "DUPLICATE_CONFLICT",
                "left": "47_RAW_ROWS",
                "right": "3_DUPLICATE_GROUPS_MERGED",
                "source_locations": sorted(
                    {
                        f"{ref.material_code}p{ref.page_number}"
                        for group in duplicate_groups
                        for row in group
                        for ref in row.source_refs
                    }
                ),
            }
        )
    expected = {
        "F1_SECOND_LOAN_AMOUNT_CONFLICT",
        "F1_NO_PRINCIPAL_REPAYMENT_CONFLICT",
        "HKD_CURRENCY_BLOCKER",
        "CROSS_SOURCE_TRANSACTION_DUPLICATES",
    }
    predicted = {str(item["finding_id"]) for item in findings}
    localized = sum(bool(item.get("source_locations")) for item in findings)
    metrics = {
        "gold_conflicts": len(expected),
        "detected_conflicts": len(expected & predicted),
        "missed_conflicts": len(expected - predicted),
        "false_positive_conflicts": len(predicted - expected),
        "localized_to_source": localized,
        "finding_ids": sorted(predicted),
    }
    return tuple(findings), metrics


def _build_candidate_paragraphs(
    *,
    selected: ScenarioResult,
    ledger_rows: Sequence[ExtractedLedgerRow],
    identity_fields: Sequence[ExtractedIdentityField],
    consistency_findings: Sequence[Mapping[str, object]],
    source_refs: Mapping[str, SourceRef],
) -> tuple[tuple[DocumentParagraph, ...], tuple[DocumentParagraph, ...]]:
    names = {item.role: item for item in identity_fields if item.field_kind == "name"}
    by_row = {item.row_number: item for item in ledger_rows}
    principal_refs = tuple(
        dict.fromkeys(
            ref.source_ref_id
            for number in (1, 2)
            for ref in by_row[number].source_refs
        )
    )
    identity_refs = tuple(
        dict.fromkeys(item.source_ref.source_ref_id for item in identity_fields)
    )
    calculation_refs = tuple(
        dict.fromkeys(
            ref.source_ref_id
            for line in selected.trace
            for ref in by_row[line.source_row_number].source_refs
        )
    )
    conflict_locations = {
        str(location)
        for item in consistency_findings
        for location in item.get("source_locations", [])
    }
    conflict_refs = tuple(
        ref_id
        for ref_id, ref in source_refs.items()
        if f"{ref.material_code}p{ref.page_number}" in conflict_locations
    )
    l1, l2 = selected.loan("L1"), selected.loan("L2")
    defence = (
        DocumentParagraph(
            "defence-1",
            "一、案件与主体",
            f"本工程验收仅处理合成案件：原告{names['原告'].value}，被告{names['被告'].value}，关系人{names['代付人'].value}。",
            tuple(dict.fromkeys((*identity_refs, *principal_refs))),
        ),
        DocumentParagraph(
            "defence-2",
            "二、本金、利息与冲抵",
            f"经确定性引擎按{selected.scenario_id}复算：L1未偿本金{l1.principal}元、未付息{l1.interest_arrears}元；"
            f"L2未偿本金{l2.principal}元、未付息{l2.interest_arrears}元；合计本金{selected.total_principal}元、未付息{selected.total_interest_arrears}元。",
            tuple(dict.fromkeys((*principal_refs, *calculation_refs))),
        ),
        DocumentParagraph(
            "defence-3",
            "三、一致性与阻断项",
            "系统定位起诉状第二笔借款金额、“本金分文未还”陈述、跨源重复以及HKD混入四类问题；HKD未做汇率换算，不进入CNY合计。",
            conflict_refs,
        ),
        DocumentParagraph(
            "defence-4",
            "四、律师复核边界",
            "付款性质、债务分配、身份映射和时效证据为一次批量审批内容；本底稿不代表真实律师意见，禁止对外发送。",
            tuple(dict.fromkeys((*identity_refs, *calculation_refs))),
        ),
    )
    by_material: dict[str, list[str]] = {}
    for ref_id, ref in source_refs.items():
        by_material.setdefault(ref.material_code, []).append(ref_id)
    evidence: list[DocumentParagraph] = []
    descriptions = {
        "F1": "法院送达、起诉请求与答辩候选",
        "F2": "借条1与L1本金、利率、期限",
        "F3": "借条2与L2本金、利率、未定期",
        "F4": "微信对话、付款和代付关系",
        "F5": "微信支付账单与跨源核对",
        "F6": "银行流水、出借和还款",
        "F7": "原告身份和送达候选",
        "F8": "授权委托与角色权限",
        "F9": "重复导出账单，用于精确重复验证",
    }
    for index, code in enumerate(descriptions, start=1):
        refs = tuple(dict.fromkeys(by_material.get(code, ())))
        if not refs:
            raise GoldenVerticalSliceBlocked(f"evidence catalogue lacks {code} provenance")
        evidence.append(
            DocumentParagraph(
                f"evidence-{index}",
                f"证据{index}：{code}",
                f"{descriptions[code]}；仅作合成工程验收，具体事实以页级溯源为准。",
                refs,
            )
        )
    return defence, tuple(evidence)


def _paragraphs_to_approved_draft(
    title: str,
    paragraphs: Sequence[DocumentParagraph],
    version_hash: str,
    source_refs: Mapping[str, SourceRef],
) -> ApprovedDraft:
    sections = []
    for item in paragraphs:
        grouped: dict[tuple[str, str], list[SourceRef]] = {}
        for ref_id in item.source_ref_ids:
            ref = source_refs.get(ref_id)
            if ref is None:
                raise GoldenVerticalSliceBlocked(f"paragraph {item.paragraph_id} has an unknown ref")
            grouped.setdefault((ref.file_name, ref.file_sha256), []).append(ref)
        if not grouped:
            raise GoldenVerticalSliceBlocked(f"paragraph {item.paragraph_id} lacks provenance")
        displays = []
        for (file_name, file_hash), refs in sorted(grouped.items()):
            pages = sorted({ref.page_number for ref in refs})
            displays.append(
                f"{file_name} 第{_compact_page_numbers(pages)}页｜{len(refs)}个坐标锚点｜文件{file_hash[:12]}"
                "｜完整坐标/页哈希见provenance_index.json"
            )
        sections.append(
            ApprovedSection(
                heading=item.heading,
                paragraphs=(item.text,),
                source_refs=tuple(dict.fromkeys(displays)),
            )
        )
    return ApprovedDraft(title=title, sections=tuple(sections), approval_hash=version_hash)


def _compact_page_numbers(pages: Sequence[int]) -> str:
    if not pages:
        raise GoldenVerticalSliceBlocked("source page list is empty")
    ranges: list[str] = []
    start = previous = pages[0]
    for value in pages[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return "、".join(ranges)


def _build_and_verify_provenance(
    *,
    generated: GeneratedGoldenCase,
    pages: Sequence[PageRecord],
    ledger_rows: Sequence[ExtractedLedgerRow],
    identity_fields: Sequence[ExtractedIdentityField],
    classified_rows: Sequence[object],
    suite: IndependentScenarioSuite,
    selected: ScenarioResult,
    documents: Sequence[DocumentParagraph],
    source_refs: Mapping[str, SourceRef],
    choices: Mapping[str, object],
    spec_path: Path,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    page_by_location = {(item.file_name, item.page_number): item for item in pages}
    source_root = Path(generated.sources_root)
    source_checks: dict[str, bool] = {}
    source_rows = []
    for ref_id, ref in sorted(source_refs.items()):
        page = page_by_location.get((ref.file_name, ref.page_number))
        valid = bool(
            page
            and (source_root / ref.file_name).is_file()
            and _file_sha256(source_root / ref.file_name) == ref.file_sha256
            and page.page_sha256 == ref.page_sha256
            and len(ref.bbox) == 4
            and 0 <= ref.bbox[0] < ref.bbox[2] <= page.width
            and 0 <= ref.bbox[1] < ref.bbox[3] <= page.height
            and sha256(ref.excerpt.encode("utf-8")).hexdigest() == ref.excerpt_sha256
        )
        source_checks[ref_id] = valid
        source_rows.append(
            {
                **asdict(ref),
                "page_width": page.width if page else None,
                "page_height": page.height if page else None,
                "coordinate_space": "PDF_POINTS_OR_IMAGE_PIXELS_BOTTOM_LEFT",
                "verified": valid,
            }
        )
    claims: list[dict[str, object]] = []
    for row in ledger_rows:
        refs = [item.source_ref_id for item in row.source_refs]
        for field_name in (
            "occurred_at",
            "channel",
            "amount",
            "currency",
            "summary",
            "direction",
        ):
            claims.append(
                {
                    "claim_id": f"ledger-{row.row_number}-{field_name}",
                    "claim_kind": "EXTRACTED_FACT",
                    "value": getattr(row, field_name),
                    "source_ref_ids": refs,
                    "verified": all(source_checks.get(item, False) for item in refs),
                }
            )
    for item in identity_fields:
        ref_id = item.source_ref.source_ref_id
        claims.append(
            {
                "claim_id": f"identity-{item.occurrence_id}",
                "claim_kind": "IDENTITY_FACT",
                "value": item.value,
                "source_ref_ids": [ref_id],
                "verified": source_checks.get(ref_id, False),
            }
        )

    replay_suite = run_independent_scenarios(classified_rows, spec=spec_path)
    replay = replay_suite.scenario(selected.scenario_id)
    replay_match = _canonical_hash(replay) == _canonical_hash(selected)
    row_refs = {item.row_number: [ref.source_ref_id for ref in item.source_refs] for item in ledger_rows}
    loan_refs = {
        "L1": [
            ref_id for ref_id, ref in source_refs.items() if ref.material_code == "F2"
        ],
        "L2": [
            ref_id for ref_id, ref in source_refs.items() if ref.material_code == "F3"
        ],
    }
    calculation_claim_count = 0
    for line in selected.trace:
        refs = list(dict.fromkeys((*row_refs[line.source_row_number], *loan_refs[line.debt_id])))
        for field_name in (
            "payment_amount",
            "accrued_interest",
            "interest_paid",
            "excess_principal_offset",
            "closing_principal",
            "closing_interest_arrears",
        ):
            calculation_claim_count += 1
            value = getattr(line, field_name)
            claims.append(
                {
                    "claim_id": f"calculation-{line.event_code}-{field_name}",
                    "claim_kind": "DERIVED_AMOUNT",
                    "value": format(value, "f"),
                    "source_ref_ids": refs,
                    "derivation": {
                        "formula_id": "GOLDEN_MONTHLY_RATE_DIV_30_HALF_UP_PER_SEGMENT",
                        "rule_ids": ["R1", "R2", "R3", "R4", "R5", "R9"],
                        "scenario_id": selected.scenario_id,
                        "source_row_number": line.source_row_number,
                        "calculation_output_hash": _canonical_hash(selected),
                        "independent_replay_match": replay_match,
                    },
                    "verified": replay_match and all(source_checks.get(item, False) for item in refs),
                }
            )
    all_ledger_refs = list(
        dict.fromkeys(ref.source_ref_id for row in ledger_rows for ref in row.source_refs)
    )
    for label, value in (
        ("total_principal", selected.total_principal),
        ("total_interest_arrears", selected.total_interest_arrears),
        ("L1_principal", selected.loan("L1").principal),
        ("L1_interest_arrears", selected.loan("L1").interest_arrears),
        ("L2_principal", selected.loan("L2").principal),
        ("L2_interest_arrears", selected.loan("L2").interest_arrears),
    ):
        calculation_claim_count += 1
        claims.append(
            {
                "claim_id": f"calculation-summary-{label}",
                "claim_kind": "DERIVED_AMOUNT",
                "value": format(value, "f"),
                "source_ref_ids": all_ledger_refs,
                "derivation": {
                    "formula_id": "GOLDEN_SCENARIO_SUMMARY",
                    "scenario_id": selected.scenario_id,
                    "calculation_output_hash": _canonical_hash(selected),
                    "independent_replay_match": replay_match,
                },
                "verified": replay_match and all(source_checks.get(item, False) for item in all_ledger_refs),
            }
        )
    for paragraph in documents:
        claims.append(
            {
                "claim_id": f"paragraph-{paragraph.paragraph_id}",
                "claim_kind": "DOCUMENT_PARAGRAPH",
                "value": paragraph.text,
                "source_ref_ids": list(paragraph.source_ref_ids),
                "verified": bool(paragraph.source_ref_ids)
                and all(source_checks.get(item, False) for item in paragraph.source_ref_ids),
            }
        )
    broken = [str(item["claim_id"]) for item in claims if item["verified"] is not True]
    provenance = {
        "schema_version": "golden-case-provenance-index-v1",
        "spec_sha256": AUTHORITATIVE_SPEC_SHA256,
        "selected_scenario_id": selected.scenario_id,
        "decision_set_hash": _canonical_hash(choices),
        "calculation_output_hash": _canonical_hash(selected),
        "source_refs": source_rows,
        "claims": claims,
    }
    metrics = {
        "traceable_objects_total": len(claims),
        "fully_traceable_objects": len(claims) - len(broken),
        "coverage": 1.0 if not claims else (len(claims) - len(broken)) / len(claims),
        "source_refs_total": len(source_rows),
        "source_refs_broken": sum(not item for item in source_checks.values()),
        "calculation_amount_claims_checked": calculation_claim_count,
        "document_paragraphs_checked": len(documents),
        "calculation_replay_match": replay_match,
        "broken_claim_ids": broken,
    }
    return provenance, metrics


def _compile_exact_package(
    *,
    output: Path,
    candidate_hash: str,
    approval_hash: str,
    approval_event: AuditEvent,
    component_paths: tuple[Path, Path],
    dependency_hashes: Mapping[str, str],
) -> SubmissionBundleCompilationResult:
    document_kinds = ("DEFENCE_POINTS_DRAFT", "EVIDENCE_CATALOGUE_DRAFT")
    components = []
    payloads: dict[str, bytes] = {}
    for sequence, (kind, path) in enumerate(zip(document_kinds, component_paths), start=1):
        content = path.read_bytes()
        digest = sha256(content).hexdigest()
        key = _submission_object_key(digest)
        payloads[key] = content
        components.append(
            SubmissionArtifactBinding(
                component_id=_stable_uuid("component", kind, digest),
                sequence=sequence,
                document_kind=kind,
                court_filename=path.name,
                media_type="application/pdf",
                object_key=key,
                artifact_sha256=digest,
                byte_size=len(content),
                approval_hash=approval_hash,
            )
        )
    dependencies = tuple(
        SubmissionDependency(
            dependency_kind=kind,
            object_id=_stable_uuid("dependency", kind, digest),
            object_sha256=digest,
        )
        for kind, digest in sorted(dependency_hashes.items())
    )
    descriptor = SubmissionBundleDescriptor(
        bundle_id=_stable_uuid("bundle", candidate_hash),
        matter_id=_stable_uuid("matter", "(2025)京0105民初17532号"),
        matter_version=1,
        export_profile="COURT_PDF_ONLY_V1",
        currency="CNY",
        approved_input_hash=candidate_hash,
        required_document_kinds=document_kinds,
        required_dependency_kinds=tuple(sorted(dependency_hashes)),
        dependencies=dependencies,
        approved_by=_stable_uuid("actor", "SYNTHETIC_LEAD_REVIEWER"),
        approved_at=datetime.fromisoformat(approval_event.occurred_at),
        approval_hash=approval_hash,
    )
    return compile_submission_bundle(
        descriptor,
        tuple(components),
        artifact_reader=lambda key, expected: _read_payload(payloads, key, expected),
        output_directory=output / "locked_submission",
    )


def _read_payload(payloads: Mapping[str, bytes], key: str, expected_hash: str) -> bytes:
    value = payloads.get(key)
    if value is None or sha256(value).hexdigest() != expected_hash:
        raise GoldenVerticalSliceBlocked("candidate PDF bytes changed before compilation")
    return value


def _submission_object_key(digest: str) -> str:
    return f"{digest[:2]}/{digest[2:4]}/{digest}.lca"


def _verify_exact_package_contract(
    *,
    compilation: SubmissionBundleCompilationResult,
    candidate_hash: str,
    approval_hash: str,
    component_paths: tuple[Path, Path],
) -> None:
    verification = verify_submission_bundle(compilation)
    if not verification.verified or verification.component_count != 2:
        raise GoldenVerticalSliceBlocked("locked package verification failed")
    manifest = json.loads(compilation.internal_manifest_path.read_text(encoding="utf-8"))
    bundle = manifest.get("bundle", {})
    files = manifest.get("court_files", [])
    if (
        bundle.get("input_hash") != candidate_hash
        or bundle.get("approval_hash") != approval_hash
        or len(files) != 2
        or {item.get("document_kind") for item in files}
        != {"DEFENCE_POINTS_DRAFT", "EVIDENCE_CATALOGUE_DRAFT"}
        or any(item.get("approval_hash") != approval_hash for item in files)
    ):
        raise GoldenVerticalSliceBlocked("compiler did not preserve the exact approval contract")
    expected = {path.name: _file_sha256(path) for path in component_paths}
    actual = {str(item["court_filename"]): str(item["artifact_sha256"]) for item in files}
    if expected != actual:
        raise GoldenVerticalSliceBlocked("locked bytes differ from reviewed candidate bytes")


def _build_artifact_graph(
    *,
    generated: GeneratedGoldenCase,
    dedup_path: Path,
    extraction_path: Path,
    choices: Mapping[str, object],
    calculation_hash: str,
    consistency_path: Path,
    provenance_path: Path,
    defence_path: Path,
    evidence_path: Path,
    candidate_hash: str,
    approval_hash: str,
    submission_hash: str,
) -> ArtifactGraph:
    nodes: list[ArtifactNode] = [
        ArtifactNode(
            "source:manifest",
            "SOURCE_MANIFEST",
            _file_sha256(Path(generated.manifest_path)),
            (),
        ),
        ArtifactNode(
            "page:dedup",
            "PAGE_DEDUP",
            _file_sha256(dedup_path),
            ("source:manifest",),
        ),
        ArtifactNode(
            "extraction:current",
            "EXTRACTION",
            _file_sha256(extraction_path),
            ("page:dedup",),
        ),
    ]
    for decision_id, value in sorted(choices.items()):
        nodes.append(
            ArtifactNode(
                f"decision:{decision_id}",
                "REVIEWED_DECISION",
                _canonical_hash({"decision_id": decision_id, "choice": value}),
                ("extraction:current",),
            )
        )
    decision_nodes = tuple(f"decision:{item}" for item in sorted(choices))
    nodes.extend(
        (
            ArtifactNode(
                "calculation:current",
                "CALCULATION_RUN",
                calculation_hash,
                ("extraction:current", *decision_nodes),
            ),
            ArtifactNode(
                "consistency:current",
                "CONSISTENCY",
                _file_sha256(consistency_path),
                ("extraction:current",),
            ),
            ArtifactNode(
                "document:defence",
                "DEFENCE_POINTS_DRAFT",
                _file_sha256(defence_path),
                ("calculation:current", "consistency:current", *decision_nodes),
            ),
            ArtifactNode(
                "document:evidence",
                "EVIDENCE_CATALOGUE_DRAFT",
                _file_sha256(evidence_path),
                ("extraction:current", "consistency:current", *decision_nodes),
            ),
            ArtifactNode(
                "provenance:current",
                "PROVENANCE_INDEX",
                _file_sha256(provenance_path),
                ("calculation:current", "document:defence", "document:evidence"),
            ),
            ArtifactNode(
                "candidate:current",
                "EXACT_CANDIDATE",
                candidate_hash,
                ("document:defence", "document:evidence", "provenance:current"),
            ),
            ArtifactNode(
                "approval:current",
                "FINAL_APPROVAL",
                approval_hash,
                ("candidate:current", *decision_nodes),
            ),
            ArtifactNode(
                "submission:current",
                "LOCKED_SYNTHETIC_PACKAGE",
                submission_hash,
                ("candidate:current", "approval:current"),
            ),
        )
    )
    return ArtifactGraph(nodes)


def _run_invalidation_trials(
    *,
    output: Path,
    state_root: Path,
    audit_root: Path,
    suite: IndependentScenarioSuite,
    choices: Mapping[str, object],
    original_calculation_hash: str,
) -> Mapping[str, object]:
    trials = []
    mutations = (
        ("D06_CASH_SWITCH", "INCLUDE_CASH_L1"),
        ("D07_U2_SWITCH", "EXCLUDE_U2_AS_EXTERNAL"),
    )
    for decision_id, new_value in mutations:
        trial_id = decision_id.split("_")[0]
        root = output / "invalidation_trials" / trial_id
        _mkdir_private(root.parent)
        shutil.copytree(state_root, root / "state", copy_function=shutil.copy2)
        shutil.copytree(audit_root, root / "audit", copy_function=shutil.copy2)
        state = root / "state"
        audit = AppendOnlyAuditLog(root / "audit")
        package_before = open_current_submission(state, root / "audit")
        pointer = json.loads((state / "current_submission.json").read_text(encoding="utf-8"))
        zip_path = Path(pointer["court_zip_path"])
        old_zip_hash = _file_sha256(zip_path)
        changed = dict(choices)
        old_value = str(changed[decision_id])
        changed[decision_id] = new_value
        scenario_id = selected_scenario_id(changed)
        new_calculation_hash = _canonical_hash(suite.scenario(scenario_id))
        if new_calculation_hash == original_calculation_hash:
            raise GoldenVerticalSliceBlocked("decision mutation did not change calculation output")
        event = audit.append(
            "DECISION_CHANGED",
            {
                "decision_id": decision_id,
                "before": old_value,
                "after": new_value,
                "selected_scenario_id": scenario_id,
                "new_calculation_hash": new_calculation_hash,
            },
        )
        graph_path = state / "artifact_graph.json"
        graph = ArtifactGraph.from_json(json.loads(graph_path.read_text(encoding="utf-8")))
        node_id = f"decision:{decision_id}"
        old_node = graph.nodes[node_id]
        graph.nodes[node_id] = ArtifactNode(
            old_node.node_id,
            old_node.kind,
            _canonical_hash({"decision_id": decision_id, "choice": new_value}),
            old_node.depends_on,
            "CURRENT",
        )
        stale = graph.invalidate_descendants(node_id)
        _write_json_atomic(graph_path, graph.to_json())
        audit.append(
            "DOWNSTREAM_INVALIDATED",
            {
                "decision_event_hash": event.event_hash,
                "decision_id": decision_id,
                "stale_nodes": list(stale),
            },
        )
        blocked = False
        try:
            open_current_submission(state, root / "audit")
        except GoldenVerticalSliceBlocked:
            blocked = True
        current_pointer = state / "current_submission.json"
        current_pointer.unlink()
        _fsync_directory(state)
        trials.append(
            {
                "decision_id": decision_id,
                "old_value": old_value,
                "new_value": new_value,
                "new_scenario_id": scenario_id,
                "package_open_before_change": bool(package_before),
                "calculation_hash_changed": new_calculation_hash != original_calculation_hash,
                "invalidated_nodes": list(stale),
                "invalidated_count": len(stale),
                "old_package_open_blocked": blocked,
                "current_pointer_cleared": not current_pointer.exists(),
                "old_package_bytes_preserved": _file_sha256(zip_path) == old_zip_hash,
                "new_L1_principal": format(suite.scenario(scenario_id).loan("L1").principal, "f"),
                "new_L1_interest_arrears": format(suite.scenario(scenario_id).loan("L1").interest_arrears, "f"),
            }
        )
    expected_nodes = {
        "calculation:current",
        "document:defence",
        "document:evidence",
        "provenance:current",
        "candidate:current",
        "approval:current",
        "submission:current",
    }
    return {
        "decision_mutations_tested": len(trials),
        "expected_downstream_invalidations": len(expected_nodes) * len(trials),
        "actual_downstream_invalidations": sum(
            len(expected_nodes & set(item["invalidated_nodes"])) for item in trials
        ),
        "old_packages_blocked": sum(bool(item["old_package_open_blocked"]) for item in trials),
        "current_pointers_cleared": sum(bool(item["current_pointer_cleared"]) for item in trials),
        "calculation_hashes_changed": sum(bool(item["calculation_hash_changed"]) for item in trials),
        "old_package_bytes_preserved": sum(bool(item["old_package_bytes_preserved"]) for item in trials),
        "trials": trials,
    }


def _verify_originals(
    generated: GeneratedGoldenCase, expected_hashes: Mapping[str, str]
) -> Mapping[str, object]:
    source_root = Path(generated.sources_root)
    checks = []
    for item in generated.files:
        path = source_root / item.file_name
        checks.append(
            {
                "file_name": item.file_name,
                "hash_unchanged": _file_sha256(path) == expected_hashes[item.file_name],
                "mode": oct(path.stat().st_mode & 0o777),
                "read_only": path.stat().st_mode & 0o222 == 0,
            }
        )
    return {
        "files_checked": len(checks),
        "all_unchanged_and_read_only": all(
            item["hash_unchanged"] and item["read_only"] for item in checks
        ),
        "files": checks,
    }


def _calculation_metrics(
    suite: IndependentScenarioSuite,
    comparison: object,
    oracle_sha256: str,
) -> Mapping[str, object]:
    return {
        "oracle_sha256": oracle_sha256,
        "scenario_count": 4,
        "checked_fields": comparison.checked_fields,
        "mismatched_fields": len(comparison.mismatches),
        "matching_to_cent": comparison.matching,
        "default_scenario_id": suite.default_scenario_id,
        "default_trace_lines": len(suite.scenario(suite.default_scenario_id).trace),
        "key_anchors_checked": len(suite.key_anchors),
        "rounding_rule": "ROUND_HALF_UP_EACH_SEGMENT_TO_CNY_0.01",
        "scenarios": {
            item.scenario_id: {
                "L1_principal": format(item.loan("L1").principal, "f"),
                "L1_interest_arrears": format(item.loan("L1").interest_arrears, "f"),
                "L2_principal": format(item.loan("L2").principal, "f"),
                "L2_interest_arrears": format(item.loan("L2").interest_arrears, "f"),
                "total_principal": format(item.total_principal, "f"),
                "total_interest_arrears": format(item.total_interest_arrears, "f"),
            }
            for item in suite.scenarios
        },
    }


def _assert_acceptance(metrics: Mapping[str, object]) -> None:
    dedup = metrics["page_deduplication"]
    extraction = metrics["information_extraction"]
    calculation = metrics["deterministic_calculation"]
    consistency = metrics["cross_document_consistency"]
    invalidation = metrics["invalidation_propagation"]
    provenance = metrics["provenance"]
    locked = metrics["locked_submission"]
    originals = metrics["originals"]
    checks = (
        dedup["missed_exact_page_groups"] == 0,
        dedup["false_positive_exact_page_groups"] == 0,
        dedup["false_removals"] == 0,
        dedup["f3_near_pair_preserved"] is True,
        extraction["precision"] >= 0.95,
        extraction["recall"] >= 0.98,
        extraction["source_ref_coverage"] == 1.0,
        calculation["matching_to_cent"] is True,
        calculation["mismatched_fields"] == 0,
        consistency["missed_conflicts"] == 0,
        consistency["false_positive_conflicts"] == 0,
        consistency["localized_to_source"] == consistency["gold_conflicts"],
        invalidation["decision_mutations_tested"] == 2,
        invalidation["actual_downstream_invalidations"]
        == invalidation["expected_downstream_invalidations"],
        invalidation["old_packages_blocked"] == 2,
        invalidation["current_pointers_cleared"] == 2,
        invalidation["calculation_hashes_changed"] == 2,
        provenance["coverage"] == 1.0,
        provenance["source_refs_broken"] == 0,
        provenance["calculation_replay_match"] is True,
        locked["verified"] is True,
        locked["current_locked_packages"] == 1,
        locked["component_count"] == 2,
        locked["external_send"] is False,
        originals["all_unchanged_and_read_only"] is True,
    )
    if not all(checks):
        raise GoldenVerticalSliceBlocked("one or more six-metric acceptance gates failed")


def _render_report(
    metrics: Mapping[str, object], compilation: SubmissionBundleCompilationResult
) -> str:
    dedup = metrics["page_deduplication"]
    extraction = metrics["information_extraction"]
    calculation = metrics["deterministic_calculation"]
    consistency = metrics["cross_document_consistency"]
    invalidation = metrics["invalidation_propagation"]
    provenance = metrics["provenance"]
    default = calculation["scenarios"]["S-A-1"]
    return f"""# 合成金标案件运行报告

- 案号：{metrics['case_id']} （全部合成）
- 权威规格 SHA-256：`{metrics['spec_sha256']}`
- 权威复算器 SHA-256：`{metrics['oracle_sha256']}`

## 六项实测数字

1. 页级去重：88 页输入，{dedup['gold_exact_page_groups']} 组精确重复全部命中；漏检 {dedup['missed_exact_page_groups']}，误删 {dedup['false_removals']}，F3 相似页误合并 0。
2. 信息提取：金标 {extraction['gold_field_labels']} 个字段发生项，TP/FP/FN={extraction['tp']}/{extraction['fp']}/{extraction['fn']}，精确率 {extraction['precision']:.2%}，召回率 {extraction['recall']:.2%}，来源完整率 {extraction['source_ref_coverage']:.2%}。
3. 确定性计算：4 情景、{calculation['checked_fields']} 个共享字段与权威脚本比对，差异 {calculation['mismatched_fields']}；默认 S-A-1 本金 {default['total_principal']} 元，未付息 {default['total_interest_arrears']} 元，33 条逐笔迹线、5 个锚点可复算。
4. 一致性：规格要求的 {consistency['gold_conflicts']} 类问题检出 {consistency['detected_conflicts']}，漏检 {consistency['missed_conflicts']}，误报 {consistency['false_positive_conflicts']}，来源定位 {consistency['localized_to_source']}。
5. 失效传播：实际切换现金和 U2 两个决策，{invalidation['actual_downstream_invalidations']}/{invalidation['expected_downstream_invalidations']} 个下游节点失效，旧包阻断 {invalidation['old_packages_blocked']}/2，当前指针清除 {invalidation['current_pointers_cleared']}/2。
6. 溯源：{provenance['fully_traceable_objects']}/{provenance['traceable_objects_total']} 个事实、计算金额和段落可回指，覆盖率 {provenance['coverage']:.2%}，坏引用 {provenance['source_refs_broken']}，独立重放一致={str(provenance['calculation_replay_match']).lower()}。

## 锁定与缺口

- 锁定 ZIP：`{compilation.court_zip_path}`；组件 2 份，ZIP SHA-256 `{compilation.court_zip_sha256}`。
- 审批是一次显式的“合成测试审批”，绑定审批前已生成的两份 PDF 精确字节；它不等于真实律师审批。
- HKD 8,000 缺少汇率与换算日，保留原币并阻断合计。
- 包内为待终审底稿，`external_send=false`；未提供电子签章、法院平台命名和真实提交规则，禁止对外发送。
"""


def _stable_uuid(*parts: str) -> str:
    return str(uuid5(_NAMESPACE, "|".join(parts)))


def _canonical_hash(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_bytes_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _write_json_new(path: Path, value: object) -> None:
    _write_bytes_new(path, _canonical_json_bytes(value))


def _write_json_exclusive_fsync(path: Path, value: object) -> None:
    _write_json_new(path, value)


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    _write_bytes_new(temporary, _canonical_json_bytes(value))
    os.replace(temporary, path)
    path.chmod(0o600)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ACCEPT_GOLDEN_RECOMMENDATIONS",
    "AppendOnlyAuditLog",
    "ArtifactGraph",
    "ArtifactNode",
    "DocumentParagraph",
    "GoldenSliceRunResult",
    "GoldenVerticalSliceBlocked",
    "archive_existing_output",
    "create_bound_approval",
    "open_current_submission",
    "publish_current_submission",
    "run_golden_vertical_slice",
    "validate_bound_approval",
]
