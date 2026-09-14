"""Forward-only, zero-network revisions of verified Agent document packages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import logging
import re
from difflib import unified_diff
from typing import Any, Mapping, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_document_binding_postgres import PostgresDocumentBindingBlocked
from .case_agent_document_delivery import (
    DynamicDocumentTaskBinding,
    ReviewableDocumentFormat,
    ReviewableDocumentCandidate,
    ReviewableDocumentTemplateRegistry,
    build_deterministic_case_review_memo_candidate,
    build_deterministic_defence_statement_candidate,
    build_deterministic_evidence_catalogue_candidate,
    build_deterministic_payment_ledger_candidate,
    build_deterministic_supplementary_evidence_checklist_candidate,
    canonical_document_candidate_bytes,
    visible_document_source_labels,
)
from .case_agent_document_delivery_postgres import (
    CaseAgentDocumentPackageBlocked,
    PostgresReviewableDocumentPackageAccessPort,
    PostgresReviewableDocumentPackageStore,
    ReviewableDocumentPackageStagingRequest,
    ReviewableDocumentPackageRead,
    StagedReviewableDocumentPackage,
    authorized_document_source_manifest,
)
from .models import Actor, Role
from .reviewable_draft_worker import (
    ReviewOfficeConversionBlocked,
    ReviewOfficeConversionUnknown,
    ReviewOfficeConverter,
    ReviewableOfficeDraft,
    create_reviewable_docx_draft,
    create_reviewable_xlsx_ledger,
)


class CaseAgentDocumentRevisionBlocked(RuntimeError):
    """A document revision command or Worker result is not safely bound."""


def current_document_result_relation(connection: Any) -> str:
    """Allowlisted schema capability, never fallback after an access failure."""
    row = connection.execute(
        "SELECT to_regclass('public.case_agent_document_revision_current_results') IS NOT NULL AS recovery_results_available"
    ).fetchone()
    if row is None or type(row.get("recovery_results_available")) is not bool:
        raise CaseAgentDocumentRevisionBlocked("document result schema capability is unavailable")
    return ("case_agent_document_revision_current_results" if row["recovery_results_available"]
            else "case_agent_document_revision_receipts")


class CaseAgentDocumentRevisionConflict(CaseAgentDocumentRevisionBlocked):
    """The lawyer reviewed an older document version."""


@dataclass(frozen=True)
class LawyerParagraphChange:
    """A proposed edit, never an approved document paragraph or new fact."""

    section_index: int
    paragraph_index: int
    expected_text_hash: str
    replacement_text: str
    reason: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class PreparedLawyerDocumentRevision:
    """In-memory revision proposal; not a stored or approved package."""

    request_hash: str
    predecessor_content_hash: str
    candidate_content: bytes
    review_manifest: bytes


def prepare_lawyer_document_revision(
    *,
    actor: Actor,
    binding: DynamicDocumentTaskBinding,
    changes: tuple[LawyerParagraphChange, ...],
    expected_candidate_hash: str,
    current_candidate_bytes: bytes,
) -> PreparedLawyerDocumentRevision:
    """Apply explicit edits and reparse through the existing document contract.

    Caller authorization to the particular case remains mandatory. This has no
    database or network effects and does not reuse the predecessor's approval.
    """
    from .case_agent_document_delivery import parse_reviewable_document_candidate

    _human(actor)
    if not isinstance(binding, DynamicDocumentTaskBinding):
        raise CaseAgentDocumentRevisionBlocked("document revision binding is invalid")
    binding.validate()
    if actor.firm_id != binding.firm_id:
        raise PermissionError("document revision actor and binding belong to different firms")
    previews = preview_lawyer_paragraph_changes(
        binding=binding, changes=changes,
        expected_candidate_hash=expected_candidate_hash,
        current_candidate_bytes=current_candidate_bytes,
    )
    predecessor = parse_reviewable_document_candidate(current_candidate_bytes, binding=binding)
    payload = json.loads(canonical_document_candidate_bytes(predecessor))
    for change in changes:
        payload["sections"][change.section_index]["paragraphs"][change.paragraph_index] = {
            "text": change.replacement_text,
            "source_refs": list(change.source_refs),
        }
    candidate = parse_reviewable_document_candidate(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"), binding=binding
    )
    manifest = {
        "schema_version": "lawyer-document-content-revision-v1",
        "firm_id": actor.firm_id,
        "matter_id": binding.matter_id,
        "run_id": binding.run_id,
        "task_id": binding.task_id,
        "requested_by": actor.actor_id,
        "binding_hash": binding.binding_hash,
        "predecessor_content_hash": expected_candidate_hash,
        "candidate_hash": candidate.candidate_hash,
        "changes": [asdict(change) for change in changes],
        "preview": previews,
        "status": "NEEDS_SOURCE_AND_LAWYER_REVIEW",
        "court_ready": False,
    }
    review_manifest = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return PreparedLawyerDocumentRevision(
        request_hash=sha256(review_manifest).hexdigest(),
        predecessor_content_hash=expected_candidate_hash,
        candidate_content=canonical_document_candidate_bytes(candidate),
        review_manifest=review_manifest,
    )


def preview_lawyer_paragraph_changes(
    *,
    binding: DynamicDocumentTaskBinding,
    changes: tuple[LawyerParagraphChange, ...],
    expected_candidate_hash: str,
    current_candidate_bytes: bytes,
) -> tuple[Mapping[str, object], ...]:
    """Build a bounded review preview against an independently loaded version.

    The caller must load candidate bytes and binding from the same authorized
    package. This function has no persistence, approval, rendering or network
    effects. Source membership is not proof that the edited assertion is true.
    """
    from .case_agent_document_delivery import parse_reviewable_document_candidate

    if not isinstance(current_candidate_bytes, bytes) or sha256(current_candidate_bytes).hexdigest() != expected_candidate_hash:
        raise CaseAgentDocumentRevisionConflict("document candidate changed before paragraph edit")
    candidate = parse_reviewable_document_candidate(current_candidate_bytes, binding=binding)
    if candidate.output_format is not ReviewableDocumentFormat.DOCX:
        raise CaseAgentDocumentRevisionBlocked("paragraph edits require a DOCX candidate")
    sections = candidate.sections
    authorized_source_refs = frozenset(source.input_ref for source in binding.sources)
    if not isinstance(changes, tuple) or not 1 <= len(changes) <= 50:
        raise CaseAgentDocumentRevisionBlocked("paragraph change count is invalid")
    seen: set[tuple[int, int]] = set()
    previews: list[Mapping[str, object]] = []
    for change in changes:
        if not isinstance(change, LawyerParagraphChange):
            raise CaseAgentDocumentRevisionBlocked("paragraph change is invalid")
        location = (change.section_index, change.paragraph_index)
        if any(type(index) is not int or index < 0 for index in location) or location in seen:
            raise CaseAgentDocumentRevisionBlocked("paragraph change location is invalid or duplicated")
        seen.add(location)
        try:
            original = sections[change.section_index].paragraphs[change.paragraph_index]
        except IndexError as error:
            raise CaseAgentDocumentRevisionBlocked("paragraph change location is outside the document") from error
        if sha256(original.text.encode("utf-8")).hexdigest() != change.expected_text_hash:
            raise CaseAgentDocumentRevisionConflict("paragraph text changed before edit")
        if (
            not isinstance(change.replacement_text, str)
            or not 1 <= len(change.replacement_text.strip()) <= 8000
            or not isinstance(change.reason, str)
            or not 1 <= len(change.reason.strip()) <= 1000
            or any(ord(char) < 32 and char not in "\n\t" for char in change.replacement_text + change.reason)
            or change.replacement_text == original.text
        ):
            raise CaseAgentDocumentRevisionBlocked("paragraph replacement or reason is invalid")
        if (
            not isinstance(change.source_refs, tuple)
            or not change.source_refs
            or any(not isinstance(ref, str) or ref not in authorized_source_refs for ref in change.source_refs)
            or len(set(change.source_refs)) != len(change.source_refs)
        ):
            raise CaseAgentDocumentRevisionBlocked("paragraph edit sources are not authorized")
        previews.append({
            "section_index": change.section_index,
            "paragraph_index": change.paragraph_index,
            "before": original.text,
            "after": change.replacement_text,
            "reason": change.reason,
            "source_refs": change.source_refs,
            "diff": "\n".join(unified_diff(original.text.splitlines(), change.replacement_text.splitlines(), fromfile="修改前", tofile="修改后", lineterm="")),
            "status": "NEEDS_SOURCE_AND_LAWYER_REVIEW",
            "court_ready": False,
        })
    return tuple(previews)


_HUMAN_ROLES = frozenset(
    {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,159}$")
_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_REVISION_MODE = "DETERMINISTIC_TEMPLATE_REVISION"
_LOG = logging.getLogger(__name__)


class _DocumentRevisionRuntimeUnavailable(RuntimeError):
    """Sanitized runtime-stage marker; the originating exception stays local."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"document revision runtime stage unavailable: {stage}")


@dataclass(frozen=True)
class DocumentRevisionState:
    root_package_id: str
    current_package_id: str
    current_candidate_artifact_id: str
    requested_artifact_id: str
    deliverable_kind: str
    output_format: ReviewableDocumentFormat
    revision_number: int
    template_id: str
    template_version: str
    template_hash: str
    package_receipt_hash: str
    current_revision_request_id: str | None
    installed_template_version: str
    installed_template_hash: str
    version_status: str
    request_status: str | None
    request_id: str | None
    can_request_revision: bool
    run_status: str
    root_candidate_artifact_id: str | None = None


@dataclass(frozen=True)
class DocumentRevisionClaim:
    request_id: str
    request_hash: str
    root_package_id: str
    predecessor_package_id: str
    expected_revision_number: int
    requested_by: str
    run_id: str
    matter_id: str
    graph_id: str
    task_id: str
    attempt_id: str
    task_input_hash: str
    attempt_count: int


class DocumentRevisionBindingPort(Protocol):
    def resolve_document_revision(
        self,
        *,
        request_id: str,
        predecessor_package_id: str,
        expected_revision_number: int,
        content_claim_version: int | None = None,
    ) -> DynamicDocumentTaskBinding: ...


def verify_lawyer_document_revision(
    *, binding: DynamicDocumentTaskBinding, predecessor_content: bytes,
    candidate_content: bytes, review_manifest: bytes, request_hash: str,
    requested_by: str,
) -> ReviewableDocumentCandidate:
    """Rebuild the stored edit from its predecessor, without granting approval."""
    from .case_agent_document_delivery import parse_reviewable_document_candidate

    _uuid(requested_by, "proposal author")
    binding.validate()
    if any(not isinstance(value, bytes) or not 2 <= len(value) <= 2 * 1024 * 1024
           for value in (predecessor_content, candidate_content, review_manifest)):
        raise CaseAgentDocumentRevisionBlocked("content revision bytes exceed limits")
    if sha256(review_manifest).hexdigest() != request_hash:
        raise CaseAgentDocumentRevisionBlocked("content revision manifest hash differs")
    try:
        manifest = json.loads(review_manifest)
        if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in (
            ("schema_version", "lawyer-document-content-revision-v1"),
            ("firm_id", binding.firm_id), ("matter_id", binding.matter_id),
            ("run_id", binding.run_id), ("task_id", binding.task_id),
            ("binding_hash", binding.binding_hash), ("requested_by", requested_by),
            ("predecessor_content_hash", sha256(predecessor_content).hexdigest()),
            ("status", "NEEDS_SOURCE_AND_LAWYER_REVIEW"), ("court_ready", False),
        )):
            raise ValueError("revision scope differs")
        raw_changes = manifest["changes"]
        if not isinstance(raw_changes, list) or not 1 <= len(raw_changes) <= 50:
            raise ValueError("revision changes invalid")
        changes = tuple(LawyerParagraphChange(**{**change, "source_refs": tuple(change["source_refs"])})
                        for change in raw_changes)
        preview = preview_lawyer_paragraph_changes(binding=binding, changes=changes,
                    expected_candidate_hash=manifest["predecessor_content_hash"], current_candidate_bytes=predecessor_content)
        if _canonical_hash(preview) != _canonical_hash(manifest["preview"]):
            raise ValueError("revision preview differs")
        predecessor = parse_reviewable_document_candidate(predecessor_content, binding=binding)
        payload = json.loads(canonical_document_candidate_bytes(predecessor))
        for change in changes:
            payload["sections"][change.section_index]["paragraphs"][change.paragraph_index] = {
                "text": change.replacement_text, "source_refs": list(change.source_refs),
            }
        expected = parse_reviewable_document_candidate(json.dumps(payload, ensure_ascii=False).encode("utf-8"), binding=binding)
        candidate = parse_reviewable_document_candidate(candidate_content, binding=binding)
        if (canonical_document_candidate_bytes(expected) != canonical_document_candidate_bytes(candidate)
                or candidate.candidate_hash != manifest["candidate_hash"]):
            raise ValueError("revision candidate differs from explicit changes")
        return candidate
    except (ValueError, TypeError, KeyError, IndexError) as error:
        raise CaseAgentDocumentRevisionBlocked("content revision cannot be independently reconstructed") from error


def render_content_revision_candidate(
    *, candidate: ReviewableDocumentCandidate, binding: DynamicDocumentTaskBinding,
    converter: ReviewOfficeConverter,
) -> ReviewableOfficeDraft:
    """Compile explicit revised text, never rebuild it from the template.

    The coordinator must claim a durable authorized job before calling this
    function and preserve UNKNOWN conversion outcomes without automatic retry.
    Returned bytes are review-only; this function never stages a package.
    """
    from .case_agent_document_delivery import parse_reviewable_document_candidate

    if binding.template.output_format is not ReviewableDocumentFormat.DOCX:
        raise CaseAgentDocumentRevisionBlocked("content revision rendering requires DOCX")
    verified = parse_reviewable_document_candidate(canonical_document_candidate_bytes(candidate), binding=binding)
    if verified.candidate_hash != candidate.candidate_hash:
        raise CaseAgentDocumentRevisionBlocked("content revision candidate hash differs")
    return create_reviewable_docx_draft(
        verified.to_docx_input(visible_document_source_labels(binding)), converter=converter,
    )


class PostgresDocumentRevisionCommandStore:
    """Human command/read model; templates and source bytes stay server-owned."""

    def __init__(
        self,
        *,
        dsn: str,
        templates: ReviewableDocumentTemplateRegistry,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("document revision PostgreSQL DSN is required")
        if not isinstance(templates, ReviewableDocumentTemplateRegistry):
            raise ValueError("document revision template registry is invalid")
        self._dsn = dsn.strip()
        self._templates = templates

    def read_state(
        self,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> DocumentRevisionState:
        _human(actor)
        for value, label in (
            (matter_id, "matter_id"),
            (run_id, "run_id"),
            (artifact_id, "artifact_id"),
        ):
            _uuid(value, label)
        with _transaction(self._dsn, actor, read_only=True) as connection:
            return self._read_state(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=artifact_id,
            )

    def read_verified_content_candidate(
        self, *, actor: Actor, matter_id: str, run_id: str, artifact_id: str,
        proposal_id: str, binding: DynamicDocumentTaskBinding, predecessor_content: bytes,
    ) -> ReviewableDocumentCandidate:
        """Preparation for review/rendering; does not accept or enqueue an edit."""
        from .case_agent_document_delivery import parse_reviewable_document_candidate

        _human(actor)
        for value, label in ((matter_id, "matter_id"), (run_id, "run_id"),
                             (artifact_id, "artifact_id"), (proposal_id, "proposal_id")):
            _uuid(value, label)
        if (binding.firm_id, binding.matter_id, binding.run_id) != (actor.firm_id, matter_id, run_id):
            raise CaseAgentDocumentRevisionBlocked("content review binding scope differs")
        with _transaction(self._dsn, actor, read_only=True) as connection:
            state = self._read_state(connection, actor=actor, matter_id=matter_id,
                                     run_id=run_id, artifact_id=artifact_id)
            return self._verify_content_candidate(connection, state=state, actor=actor, matter_id=matter_id,
                run_id=run_id, proposal_id=proposal_id, binding=binding, predecessor_content=predecessor_content)

    def _verify_content_candidate(
        self, connection: Any, *, state: DocumentRevisionState, actor: Actor,
        matter_id: str, run_id: str, proposal_id: str, binding: DynamicDocumentTaskBinding,
        predecessor_content: bytes,
    ) -> ReviewableDocumentCandidate:
        from .case_agent_document_delivery import parse_reviewable_document_candidate

        if state.version_status != "CURRENT" or state.run_status != "READY_FOR_REVIEW":
            raise CaseAgentDocumentRevisionBlocked("content review predecessor is not current")
        row = connection.execute(
            """SELECT proposal.candidate_content, proposal.review_manifest,
                      proposal.request_hash, proposal.requested_by,
                      predecessor.candidate_hash AS predecessor_candidate_hash
               FROM case_agent_document_content_proposals proposal
               JOIN case_agent_reviewable_document_packages predecessor
                 ON predecessor.package_id = proposal.predecessor_package_id
                AND predecessor.firm_id = proposal.firm_id
                AND predecessor.matter_id = proposal.matter_id
                AND predecessor.run_id = proposal.run_id
               WHERE proposal.proposal_id = %s AND proposal.firm_id = %s
                 AND proposal.matter_id = %s AND proposal.run_id = %s
                 AND proposal.predecessor_package_id = %s AND proposal.expected_revision_number = %s
                 AND predecessor.binding_hash = %s""",
            (proposal_id, actor.firm_id, matter_id, run_id, state.current_package_id,
             state.revision_number, binding.binding_hash),
        ).fetchone()
        if row is None:
            raise CaseAgentDocumentRevisionBlocked("content proposal is not based on current package")
        predecessor = parse_reviewable_document_candidate(predecessor_content, binding=binding)
        if predecessor.candidate_hash != str(row["predecessor_candidate_hash"]):
            raise CaseAgentDocumentRevisionBlocked("content review predecessor differs from stored package")
        return verify_lawyer_document_revision(binding=binding, predecessor_content=predecessor_content,
            candidate_content=bytes(row["candidate_content"]), review_manifest=bytes(row["review_manifest"]),
            request_hash=str(row["request_hash"]), requested_by=str(row["requested_by"]))

    def read_authorized_content_candidate(
        self, *, actor: Actor, matter_id: str, run_id: str, artifact_id: str,
        review_id: str, binding: DynamicDocumentTaskBinding, predecessor_content: bytes,
    ) -> ReviewableDocumentCandidate:
        """Read a still-current authorization; not a durable job claim."""
        _human(actor)
        for value, label in ((matter_id, "matter_id"), (run_id, "run_id"),
                             (artifact_id, "artifact_id"), (review_id, "review_id")):
            _uuid(value, label)
        if (binding.firm_id, binding.matter_id, binding.run_id) != (actor.firm_id, matter_id, run_id):
            raise CaseAgentDocumentRevisionBlocked("authorized content scope differs")
        with _transaction(self._dsn, actor, read_only=True) as connection:
            state = self._read_state(connection, actor=actor, matter_id=matter_id,
                                     run_id=run_id, artifact_id=artifact_id)
            row = connection.execute(
                """SELECT review.proposal_id, review.candidate_hash
                   FROM case_agent_document_content_generation_reviews review
                   JOIN users reviewer ON reviewer.user_id = review.reviewed_by
                     AND reviewer.firm_id = review.firm_id AND reviewer.status = 'ACTIVE'
                   WHERE review.review_id = %s AND review.firm_id = %s
                     AND review.matter_id = %s AND review.run_id = %s
                     AND review.root_package_id = %s AND review.expected_revision_number = %s
                     AND review.binding_hash = %s AND review.purpose = 'GENERATE_REVIEW_COPY'
                     AND EXISTS (SELECT 1 FROM matter_actor_roles assignment
                       WHERE assignment.user_id = reviewer.user_id AND assignment.firm_id = reviewer.firm_id
                         AND assignment.matter_id = review.matter_id AND assignment.revoked_at IS NULL
                         AND assignment.role IN ('LEAD_LAWYER','REVIEWER'))""",
                (review_id, actor.firm_id, matter_id, run_id, state.root_package_id,
                 state.revision_number, binding.binding_hash),
            ).fetchone()
            if row is None:
                raise CaseAgentDocumentRevisionBlocked("generation authorization is unavailable or outdated")
            candidate = self._verify_content_candidate(connection, state=state, actor=actor,
                matter_id=matter_id, run_id=run_id, proposal_id=str(row["proposal_id"]),
                binding=binding, predecessor_content=predecessor_content)
            if candidate.candidate_hash != str(row["candidate_hash"]):
                raise CaseAgentDocumentRevisionBlocked("authorized candidate differs from stored proposal")
            return candidate

    def authorize_content_generation(
        self, *, actor: Actor, matter_id: str, run_id: str, artifact_id: str,
        proposal_id: str, expected_revision_number: int, idempotency_key: str,
        review_note: str, binding: DynamicDocumentTaskBinding, predecessor_content: bytes,
    ) -> str:
        """Record a human decision for a review copy, never a submission approval."""
        _human(actor)
        if not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise PermissionError("only a matter lead or reviewer can authorize generation")
        for value, label in ((matter_id, "matter_id"), (run_id, "run_id"),
                             (artifact_id, "artifact_id"), (proposal_id, "proposal_id")):
            _uuid(value, label)
        if type(expected_revision_number) is not int or not 1 <= expected_revision_number <= 999:
            raise CaseAgentDocumentRevisionBlocked("content review version is invalid")
        if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise CaseAgentDocumentRevisionBlocked("content review idempotency key is invalid")
        if (not isinstance(review_note, str) or not 1 <= len(review_note.strip()) <= 2000
                or any(ord(char) < 32 and char not in "\n\t" for char in review_note)):
            raise CaseAgentDocumentRevisionBlocked("content review note is invalid")
        if (binding.firm_id, binding.matter_id, binding.run_id) != (actor.firm_id, matter_id, run_id):
            raise CaseAgentDocumentRevisionBlocked("content review scope differs")
        key_hash = sha256(idempotency_key.encode("utf-8")).hexdigest()
        review_id = str(uuid5(NAMESPACE_URL, f"lawcase-content-review:{actor.firm_id}:{actor.actor_id}:{key_hash}"))
        note = review_note.strip()
        with _transaction(self._dsn, actor, read_only=False) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               (f"content-review:{actor.firm_id}:{actor.actor_id}:{key_hash}",))
            authority = connection.execute(
                """SELECT 1 FROM users principal JOIN matter_actor_roles assignment
                     ON assignment.user_id = principal.user_id AND assignment.firm_id = principal.firm_id
                   WHERE principal.user_id = %s AND principal.firm_id = %s AND principal.status = 'ACTIVE'
                     AND assignment.matter_id = %s AND assignment.revoked_at IS NULL
                     AND assignment.role IN ('LEAD_LAWYER','REVIEWER')""",
                (actor.actor_id, actor.firm_id, matter_id),
            ).fetchone()
            if authority is None:
                raise PermissionError("current matter generation review authority is required")
            state = self._read_state(connection, actor=actor, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id)
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               (f"case-agent-document-revision:{actor.firm_id}:{state.root_package_id}",))
            state = self._read_state(connection, actor=actor, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id)
            prior = connection.execute(
                """SELECT review_id, proposal_id, matter_id, run_id, root_package_id,
                          expected_revision_number, review_note
                   FROM case_agent_document_content_generation_reviews
                   WHERE firm_id = %s AND reviewed_by = %s AND idempotency_key_hash = %s""",
                (actor.firm_id, actor.actor_id, key_hash),
            ).fetchone()
            if prior is not None:
                if (str(prior["proposal_id"]), str(prior["matter_id"]), str(prior["run_id"]),
                    str(prior["root_package_id"]), int(prior["expected_revision_number"]), str(prior["review_note"])) != (
                        proposal_id, matter_id, run_id, state.root_package_id, expected_revision_number, note):
                    raise CaseAgentDocumentRevisionConflict("content generation review key was reused")
                return str(prior["review_id"])
            if state.revision_number != expected_revision_number:
                raise CaseAgentDocumentRevisionConflict("content review version changed")
            candidate = self._verify_content_candidate(connection, state=state, actor=actor, matter_id=matter_id,
                run_id=run_id, proposal_id=proposal_id, binding=binding, predecessor_content=predecessor_content)
            recorded = connection.execute(
                "SELECT review_id FROM case_agent_document_content_generation_reviews WHERE proposal_id = %s AND firm_id = %s",
                (proposal_id, actor.firm_id),
            ).fetchone()
            if recorded is not None:
                raise CaseAgentDocumentRevisionConflict("proposal already has a generation review")
            connection.execute(
                """INSERT INTO case_agent_document_content_generation_reviews
                   (review_id, proposal_id, firm_id, matter_id, run_id, root_package_id,
                    expected_revision_number, candidate_hash, binding_hash, reviewed_by, review_note, idempotency_key_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (review_id, proposal_id, actor.firm_id, matter_id, run_id, state.root_package_id,
                 expected_revision_number, candidate.candidate_hash, binding.binding_hash, actor.actor_id, note, key_hash),
            )
            self._register_content_revision_request(connection, actor=actor, matter_id=matter_id,
                run_id=run_id, state=state, review_id=review_id)
        return review_id

    @staticmethod
    def _register_content_revision_request(
        connection: Any, *, actor: Actor, matter_id: str, run_id: str,
        state: DocumentRevisionState, review_id: str,
    ) -> None:
        """Same transaction as the review; the database excludes template enqueue."""
        request_id = str(uuid5(UUID(review_id), "content-document-revision-request-v1"))
        key_hash = sha256(f"content-document-revision:{review_id}".encode("utf-8")).hexdigest()
        request_hash = _request_hash(request_id=request_id, firm_id=actor.firm_id,
            matter_id=matter_id, run_id=run_id, root_package_id=state.root_package_id,
            predecessor_package_id=state.current_package_id, expected_revision_number=state.revision_number,
            target_template_id=state.template_id, target_template_version=state.template_version,
            target_template_hash=state.template_hash, source_package_receipt_hash=state.package_receipt_hash,
            requested_by=actor.actor_id, idempotency_key_hash=key_hash, content_generation_review_id=review_id)
        connection.execute(
            """INSERT INTO case_agent_document_revision_requests
               (request_id, idempotency_key_hash, firm_id, matter_id, run_id, root_package_id,
                predecessor_package_id, expected_revision_number, target_template_id, target_template_version,
                target_template_hash, source_package_receipt_hash, requested_by, request_hash, content_generation_review_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (request_id, key_hash, actor.firm_id, matter_id, run_id, state.root_package_id,
             state.current_package_id, state.revision_number, state.template_id, state.template_version,
             state.template_hash, state.package_receipt_hash, actor.actor_id, request_hash, review_id),
        )

    def resolve_content_proposal(
        self, *, actor: Actor, matter_id: str, run_id: str,
        artifact_id: str, idempotency_key: str,
    ) -> str | None:
        """A missing committed row is unconfirmed, never permission to resend."""
        _human(actor)
        for value, label in ((matter_id, "matter_id"), (run_id, "run_id"), (artifact_id, "artifact_id")):
            _uuid(value, label)
        if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise CaseAgentDocumentRevisionBlocked("content proposal idempotency key is invalid")
        with _transaction(self._dsn, actor, read_only=True) as connection:
            state = self._read_state(connection, actor=actor, matter_id=matter_id,
                                     run_id=run_id, artifact_id=artifact_id)
            row = connection.execute(
                """SELECT proposal.proposal_id
                   FROM case_agent_document_content_proposals proposal
                   JOIN case_agent_reviewable_document_packages predecessor
                     ON predecessor.package_id = proposal.predecessor_package_id
                    AND predecessor.firm_id = proposal.firm_id
                    AND predecessor.matter_id = proposal.matter_id
                    AND predecessor.run_id = proposal.run_id
                   WHERE proposal.firm_id = %s AND proposal.matter_id = %s AND proposal.run_id = %s
                     AND proposal.requested_by = %s AND proposal.idempotency_key_hash = %s
                     AND COALESCE(predecessor.root_package_id, predecessor.package_id) = %s""",
                (actor.firm_id, matter_id, run_id, actor.actor_id,
                 sha256(idempotency_key.encode("utf-8")).hexdigest(), state.root_package_id),
            ).fetchone()
            return str(row["proposal_id"]) if row is not None else None

    def list_content_proposals(
        self, *, actor: Actor, matter_id: str, run_id: str,
        artifact_id: str, after: str | None = None,
    ) -> Mapping[str, object]:
        """Bounded discovery; body bytes are read only through the detail contract."""
        _human(actor)
        for value, label in ((matter_id, "matter_id"), (run_id, "run_id"), (artifact_id, "artifact_id")):
            _uuid(value, label)
        if after is not None:
            _uuid(after, "proposal cursor")
        with _transaction(self._dsn, actor, read_only=True) as connection:
            state = self._read_state(connection, actor=actor, matter_id=matter_id,
                                     run_id=run_id, artifact_id=artifact_id)
            rows = connection.execute(
                """WITH scoped_proposals AS (
                   SELECT proposal.proposal_id, proposal.expected_revision_number, proposal.created_at
                   FROM case_agent_document_content_proposals proposal
                   JOIN case_agent_reviewable_document_packages predecessor
                     ON predecessor.package_id = proposal.predecessor_package_id
                    AND predecessor.firm_id = proposal.firm_id
                    AND predecessor.matter_id = proposal.matter_id
                    AND predecessor.run_id = proposal.run_id
                   WHERE proposal.firm_id = %s AND proposal.matter_id = %s AND proposal.run_id = %s
                     AND COALESCE(predecessor.root_package_id, predecessor.package_id) = %s
                   )
                   SELECT proposal_id, expected_revision_number, created_at FROM scoped_proposals
                   WHERE (%s::uuid IS NULL OR (created_at, proposal_id) < (
                     SELECT created_at, proposal_id FROM scoped_proposals WHERE proposal_id = %s::uuid
                   ))
                   ORDER BY created_at DESC, proposal_id DESC LIMIT 21""",
                (actor.firm_id, matter_id, run_id, state.root_package_id, after, after),
            ).fetchall()
            items = [{"proposal_id": str(row["proposal_id"]),
                      "expected_revision_number": int(row["expected_revision_number"]),
                      "created_at": row["created_at"].isoformat(),
                      "status": "NEEDS_SOURCE_AND_LAWYER_REVIEW", "court_ready": False}
                     for row in rows[:20]]
            return {"items": items, "next_after": items[-1]["proposal_id"] if len(rows) > 20 else None}

    def read_content_proposal(
        self, *, actor: Actor, matter_id: str, run_id: str,
        artifact_id: str, proposal_id: str,
    ) -> Mapping[str, object]:
        """Read an immutable edit, never treating it as a successor package."""
        _human(actor)
        for value, label in ((matter_id, "matter_id"), (run_id, "run_id"),
                             (artifact_id, "artifact_id"), (proposal_id, "proposal_id")):
            _uuid(value, label)
        with _transaction(self._dsn, actor, read_only=True) as connection:
            state = self._read_state(connection, actor=actor, matter_id=matter_id,
                                     run_id=run_id, artifact_id=artifact_id)
            row = connection.execute(
                """SELECT proposal.proposal_id, proposal.predecessor_package_id,
                          proposal.expected_revision_number, proposal.requested_by,
                          proposal.created_at, proposal.request_hash, proposal.review_manifest
                   FROM case_agent_document_content_proposals proposal
                   JOIN case_agent_reviewable_document_packages predecessor
                     ON predecessor.package_id = proposal.predecessor_package_id
                    AND predecessor.firm_id = proposal.firm_id
                    AND predecessor.matter_id = proposal.matter_id
                    AND predecessor.run_id = proposal.run_id
                   WHERE proposal.proposal_id = %s AND proposal.firm_id = %s
                     AND proposal.matter_id = %s AND proposal.run_id = %s
                     AND COALESCE(predecessor.root_package_id, predecessor.package_id) = %s""",
                (proposal_id, actor.firm_id, matter_id, run_id, state.root_package_id),
            ).fetchone()
            if row is None:
                raise CaseAgentDocumentRevisionBlocked("content proposal is unavailable")
            raw = bytes(row["review_manifest"])
            if not 2 <= len(raw) <= 2 * 1024 * 1024 or sha256(raw).hexdigest() != str(row["request_hash"]):
                raise CaseAgentDocumentRevisionBlocked("content proposal manifest is invalid")
            try:
                manifest = json.loads(raw)
                if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in (
                    ("schema_version", "lawyer-document-content-revision-v1"),
                    ("firm_id", actor.firm_id), ("matter_id", matter_id), ("run_id", run_id),
                    ("requested_by", str(row["requested_by"])),
                    ("status", "NEEDS_SOURCE_AND_LAWYER_REVIEW"), ("court_ready", False),
                )):
                    raise ValueError("manifest scope mismatch")
                preview = manifest["preview"]
                if not isinstance(preview, list) or not 1 <= len(preview) <= 50:
                    raise ValueError("manifest preview is invalid")
                fields = ("section_index", "paragraph_index", "before", "after", "reason", "source_refs", "diff")
                changes = [{key: item[key] for key in fields} for item in preview]
            except (ValueError, TypeError, KeyError) as error:
                raise CaseAgentDocumentRevisionBlocked("content proposal cannot be projected") from error
            return {
                "proposal_id": str(row["proposal_id"]),
                "expected_revision_number": int(row["expected_revision_number"]),
                "created_at": row["created_at"].isoformat(),
                "based_on_current_version": (
                    str(row["predecessor_package_id"]) == state.current_package_id
                    and int(row["expected_revision_number"]) == state.revision_number
                    and state.version_status == "CURRENT"
                ),
                "status": "NEEDS_SOURCE_AND_LAWYER_REVIEW",
                "court_ready": False, "changes": changes,
                "generation_status": self._read_content_generation_status(
                    connection, actor=actor, matter_id=matter_id, run_id=run_id, proposal_id=proposal_id),
            }

    @staticmethod
    def _read_content_generation_status(connection: Any, *, actor: Actor, matter_id: str,
                                        run_id: str, proposal_id: str) -> str:
        results = current_document_result_relation(connection)
        row = connection.execute(
            f"""SELECT job.state, job.lease_expires_at > clock_timestamp() AS lease_live,
                      receipt.outcome, receipt.successor_package_id,
                      EXISTS (
                          SELECT 1 FROM case_agent_reviewable_document_packages package
                          WHERE package.revision_request_id = request.request_id
                            AND package.firm_id = review.firm_id
                            AND package.matter_id = review.matter_id AND package.run_id = review.run_id
                            AND package.generation_mode = 'LAWYER_CONTENT_REVISION'
                            AND package.root_package_id = request.root_package_id
                            AND package.supersedes_package_id = request.predecessor_package_id
                            AND package.revision_number = request.expected_revision_number + 1
                            AND package.candidate_hash = review.candidate_hash
                            AND package.binding_hash = review.binding_hash
                            AND package.requested_by = request.requested_by
                      ) AS registered_result
               FROM case_agent_document_content_generation_reviews review
               LEFT JOIN case_agent_document_revision_requests request
                 ON request.content_generation_review_id = review.review_id
                AND request.firm_id = review.firm_id AND request.matter_id = review.matter_id
                AND request.run_id = review.run_id
               LEFT JOIN case_agent_document_content_generation_jobs job
                 ON job.review_id = review.review_id AND job.firm_id = review.firm_id
                AND job.matter_id = review.matter_id AND job.run_id = review.run_id
               LEFT JOIN {results} receipt
                 ON receipt.request_id = request.request_id AND receipt.firm_id = request.firm_id
                AND receipt.matter_id = request.matter_id
               WHERE review.proposal_id = %s AND review.firm_id = %s
                 AND review.matter_id = %s AND review.run_id = %s""",
            (proposal_id, actor.firm_id, matter_id, run_id),
        ).fetchone()
        if row is None:
            return "NOT_AUTHORIZED"
        state, outcome = row.get("state"), row.get("outcome")
        # A matching registration is evidence to reconcile, not proof that the
        # private files are intact or that independent verification succeeded.
        if (row.get("registered_result") is True and outcome in {None, "UNKNOWN"}
                and (state == "UNKNOWN" or (state == "RENDERING" and row.get("lease_live") is False))):
            return "UNKNOWN_REGISTERED"
        if state == "SUCCEEDED" and outcome == "PASSED" and row.get("successor_package_id"):
            return "GENERATED_REVIEW_COPY"
        if outcome is not None or state in {None, "UNKNOWN", "SUCCEEDED"}:
            return "FAILED" if state == "FAILED" and outcome == "FAILED" else "UNKNOWN"
        if state == "READY":
            return "QUEUED"
        if state in {"LEASED", "RENDERING"}:
            if row.get("lease_live") is True:
                return "GENERATING"
            return "RECOVERING" if state == "LEASED" else "UNKNOWN"
        return "FAILED" if state == "FAILED" else "UNKNOWN"

    def read_registered_content_result(
        self, *, actor: Actor, matter_id: str, run_id: str, artifact_id: str, proposal_id: str,
    ) -> RegisteredContentResult | None:
        """Resolve one scoped registration, never release or repair its result."""
        _human(actor)
        for value in (matter_id, run_id, artifact_id, proposal_id):
            _uuid(value, "content reconciliation scope")
        with _transaction(self._dsn, actor, read_only=True) as connection:
            state = self._read_state(connection, actor=actor, matter_id=matter_id,
                                     run_id=run_id, artifact_id=artifact_id)
            rows = connection.execute(
                """SELECT package.package_id, package.candidate_artifact_id,
                          package.package_receipt_hash, request.request_id, package.root_package_id,
                          package.supersedes_package_id, package.revision_number,
                          package.candidate_hash, package.binding_hash, package.content_generation_claim_version,
                          request.request_hash, package.staged_by, to_jsonb(receipt) AS original_receipt
                   FROM case_agent_document_content_generation_reviews review
                   JOIN case_agent_document_revision_requests request
                     ON request.content_generation_review_id = review.review_id
                    AND request.firm_id = review.firm_id AND request.matter_id = review.matter_id
                    AND request.run_id = review.run_id
                   JOIN case_agent_reviewable_document_packages package
                     ON package.revision_request_id = request.request_id
                    AND package.firm_id = review.firm_id AND package.matter_id = review.matter_id
                    AND package.run_id = review.run_id
                    AND package.generation_mode = 'LAWYER_CONTENT_REVISION'
                    AND package.root_package_id = request.root_package_id
                    AND package.supersedes_package_id = request.predecessor_package_id
                    AND package.revision_number = request.expected_revision_number + 1
                    AND package.candidate_hash = review.candidate_hash AND package.binding_hash = review.binding_hash
                    AND package.requested_by = request.requested_by
                   JOIN case_agent_document_content_generation_jobs job
                     ON job.review_id = review.review_id AND job.firm_id = review.firm_id
                    AND job.matter_id = review.matter_id AND job.run_id = review.run_id
                   JOIN users reviewer ON reviewer.user_id = review.reviewed_by
                    AND reviewer.firm_id = review.firm_id AND reviewer.status = 'ACTIVE'
                   LEFT JOIN case_agent_document_revision_receipts receipt
                     ON receipt.request_id = request.request_id AND receipt.firm_id = request.firm_id
                    AND receipt.matter_id = request.matter_id
                   WHERE review.proposal_id = %s AND review.firm_id = %s
                     AND review.matter_id = %s AND review.run_id = %s AND package.root_package_id = %s
                     AND review.purpose = 'GENERATE_REVIEW_COPY'
                     AND (job.state = 'UNKNOWN' OR (job.state = 'RENDERING' AND job.lease_expires_at <= clock_timestamp()))
                     AND (receipt.receipt_id IS NULL OR receipt.outcome = 'UNKNOWN')
                     AND EXISTS (SELECT 1 FROM matter_actor_roles assignment
                         WHERE assignment.user_id = reviewer.user_id AND assignment.firm_id = review.firm_id
                           AND assignment.matter_id = review.matter_id AND assignment.revoked_at IS NULL
                           AND assignment.role IN ('LEAD_LAWYER','REVIEWER'))
                   LIMIT 2""",
                (proposal_id, actor.firm_id, matter_id, run_id, state.root_package_id),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise CaseAgentDocumentRevisionBlocked("content registration is ambiguous")
            row = rows[0]
            original = row.get("original_receipt")
            original_id = original_hash = None
            if original is not None:
                if not isinstance(original, dict):
                    raise CaseAgentDocumentRevisionBlocked("original unknown receipt is invalid")
                original_id = str(uuid5(UUID(str(row["request_id"])), "document-revision:UNKNOWN"))
                expected = {
                    "schema_version": "case-agent-document-revision-receipt-v1",
                    "receipt_id": original_id, "request_id": str(row["request_id"]),
                    "request_hash": str(row["request_hash"]), "firm_id": actor.firm_id, "matter_id": matter_id,
                    "outcome": "UNKNOWN", "successor_package_id": None,
                    "successor_package_receipt_hash": None, "failure_code": original.get("failure_code"),
                    "external_calls": 0, "executed_by": str(row["staged_by"]), "verified_by": None,
                }
                if (any(original.get(key) != value for key, value in expected.items()
                        if key not in {"schema_version", "request_hash"})
                        or not isinstance(expected["failure_code"], str)
                        or _FAILURE_CODE.fullmatch(expected["failure_code"]) is None):
                    raise CaseAgentDocumentRevisionBlocked("original unknown receipt coordinates differ")
                _uuid(expected["executed_by"], "unknown receipt execution identity")
                original_hash = _canonical_hash(expected)
                if original.get("receipt_hash") != original_hash:
                    raise CaseAgentDocumentRevisionBlocked("original unknown receipt hash differs")
            return RegisteredContentResult(
                package_id=str(row["package_id"]), candidate_artifact_id=str(row["candidate_artifact_id"]),
                receipt_hash=str(row["package_receipt_hash"]), request_id=str(row["request_id"]),
                root_package_id=str(row["root_package_id"]), predecessor_package_id=str(row["supersedes_package_id"]),
                revision_number=int(row["revision_number"]), candidate_hash=str(row["candidate_hash"]),
                binding_hash=str(row["binding_hash"]), claim_version=row["content_generation_claim_version"],
                original_receipt_id=original_id, original_receipt_hash=original_hash,
            )

    def save_content_proposal(
        self, *, actor: Actor, matter_id: str, run_id: str, artifact_id: str,
        expected_revision_number: int, idempotency_key: str,
        binding: DynamicDocumentTaskBinding, current_candidate_bytes: bytes,
        expected_candidate_hash: str, changes: tuple[LawyerParagraphChange, ...],
    ) -> str:
        """Persist a proposal only; never enqueue it as a template revision."""
        from .case_agent_document_delivery import parse_reviewable_document_candidate

        _human(actor)
        for value, label in ((matter_id, "matter_id"), (run_id, "run_id"), (artifact_id, "artifact_id")):
            _uuid(value, label)
        if type(expected_revision_number) is not int or not 1 <= expected_revision_number <= 999:
            raise CaseAgentDocumentRevisionBlocked("content proposal revision is invalid")
        if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise CaseAgentDocumentRevisionBlocked("content proposal idempotency key is invalid")
        if not isinstance(binding, DynamicDocumentTaskBinding) or (binding.matter_id, binding.run_id) != (matter_id, run_id):
            raise CaseAgentDocumentRevisionBlocked("content proposal scope differs from binding")
        prepared = prepare_lawyer_document_revision(
            actor=actor, binding=binding, changes=changes,
            expected_candidate_hash=expected_candidate_hash,
            current_candidate_bytes=current_candidate_bytes,
        )
        if max(len(prepared.candidate_content), len(prepared.review_manifest)) > 2 * 1024 * 1024:
            raise CaseAgentDocumentRevisionBlocked("content proposal exceeds storage limit")
        predecessor = parse_reviewable_document_candidate(current_candidate_bytes, binding=binding)
        key_hash = sha256(idempotency_key.encode()).hexdigest()
        proposal_id = str(uuid5(NAMESPACE_URL, f"lawcase-content-proposal:{actor.firm_id}:{actor.actor_id}:{key_hash}"))
        with _transaction(self._dsn, actor, read_only=False) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"content-proposal:{actor.firm_id}:{actor.actor_id}:{key_hash}",))
            prior = connection.execute(
                "SELECT proposal_id, request_hash, matter_id, run_id, expected_revision_number FROM case_agent_document_content_proposals WHERE firm_id = %s AND requested_by = %s AND idempotency_key_hash = %s",
                (actor.firm_id, actor.actor_id, key_hash),
            ).fetchone()
            if prior is not None:
                if (str(prior["request_hash"]), str(prior["matter_id"]), str(prior["run_id"]), int(prior["expected_revision_number"])) != (prepared.request_hash, matter_id, run_id, expected_revision_number):
                    raise CaseAgentDocumentRevisionConflict("content proposal idempotency key was reused")
                return str(prior["proposal_id"])
            state = self._read_state(connection, actor=actor, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id)
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"case-agent-document-revision:{actor.firm_id}:{state.root_package_id}",))
            state = self._read_state(connection, actor=actor, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id)
            if state.revision_number != expected_revision_number or state.version_status != "CURRENT" or state.run_status != "READY_FOR_REVIEW":
                raise CaseAgentDocumentRevisionConflict("document is not current for a content proposal")
            package = connection.execute(
                "SELECT binding_hash, candidate_hash FROM case_agent_reviewable_document_packages WHERE package_id = %s AND firm_id = %s AND matter_id = %s AND run_id = %s FOR SHARE",
                (state.current_package_id, actor.firm_id, matter_id, run_id),
            ).fetchone()
            if package is None or (str(package["binding_hash"]), str(package["candidate_hash"])) != (binding.binding_hash, predecessor.candidate_hash):
                raise CaseAgentDocumentRevisionConflict("content proposal predecessor does not match stored package")
            connection.execute(
                """INSERT INTO case_agent_document_content_proposals
                (proposal_id, firm_id, matter_id, run_id, predecessor_package_id,
                 expected_revision_number, requested_by, idempotency_key_hash,
                 request_hash, candidate_content, review_manifest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (proposal_id, actor.firm_id, matter_id, run_id, state.current_package_id,
                 expected_revision_number, actor.actor_id, key_hash, prepared.request_hash,
                 prepared.candidate_content, prepared.review_manifest),
            )
        return proposal_id

    def request_revision(
        self,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        artifact_id: str,
        expected_revision_number: int,
        idempotency_key: str,
    ) -> DocumentRevisionState:
        _human(actor)
        if not isinstance(expected_revision_number, int) or not 1 <= expected_revision_number <= 999:
            raise CaseAgentDocumentRevisionBlocked("document revision number is invalid")
        if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise CaseAgentDocumentRevisionBlocked("document revision idempotency key is invalid")
        idempotency_hash = sha256(idempotency_key.encode("utf-8")).hexdigest()
        with _transaction(self._dsn, actor, read_only=False, repeatable_read=True) as connection:
            state = self._read_state(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=artifact_id,
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"case-agent-document-revision:{actor.firm_id}:{state.root_package_id}",),
            )
            state = self._read_state(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=artifact_id,
            )
            if state.revision_number != expected_revision_number:
                raise CaseAgentDocumentRevisionConflict(
                    "document version changed before the revision request"
                )
            if state.version_status == "CURRENT":
                return state
            if state.version_status == "GENERATING":
                return state
            if not state.can_request_revision or state.run_status != "READY_FOR_REVIEW":
                raise CaseAgentDocumentRevisionBlocked(
                    "document package is not eligible for a template revision"
                )
            request_id = str(
                uuid5(
                    NAMESPACE_URL,
                    "lawcase-document-revision:"
                    f"{actor.firm_id}:{state.root_package_id}:{idempotency_hash}",
                )
            )
            request_hash = _request_hash(
                request_id=request_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                run_id=run_id,
                root_package_id=state.root_package_id,
                predecessor_package_id=state.current_package_id,
                expected_revision_number=state.revision_number,
                target_template_id=state.template_id,
                target_template_version=state.installed_template_version,
                target_template_hash=state.installed_template_hash,
                source_package_receipt_hash=state.package_receipt_hash,
                requested_by=actor.actor_id,
                idempotency_key_hash=idempotency_hash,
            )
            inserted = connection.execute(
                """
                INSERT INTO case_agent_document_revision_requests(
                    request_id, idempotency_key_hash, firm_id, matter_id,
                    run_id, root_package_id, predecessor_package_id,
                    expected_revision_number, target_template_id,
                    target_template_version, target_template_hash,
                    source_package_receipt_hash, requested_by, request_hash
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (firm_id, idempotency_key_hash) DO NOTHING
                RETURNING request_id, request_hash
                """,
                (
                    request_id,
                    idempotency_hash,
                    actor.firm_id,
                    matter_id,
                    run_id,
                    state.root_package_id,
                    state.current_package_id,
                    state.revision_number,
                    state.template_id,
                    state.installed_template_version,
                    state.installed_template_hash,
                    state.package_receipt_hash,
                    actor.actor_id,
                    request_hash,
                ),
            ).fetchone()
            if inserted is None:
                prior = connection.execute(
                    """
                    SELECT request_id, request_hash
                    FROM case_agent_document_revision_requests
                    WHERE firm_id = %s AND idempotency_key_hash = %s
                    """,
                    (actor.firm_id, idempotency_hash),
                ).fetchone()
                if (
                    prior is None
                    or str(prior["request_id"]) != request_id
                    or str(prior["request_hash"]) != request_hash
                ):
                    raise CaseAgentDocumentRevisionConflict(
                        "document revision idempotency key is already in use"
                    )
            return self._read_state(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=artifact_id,
            )

    def _read_state(
        self,
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> DocumentRevisionState:
        base = connection.execute(
            _BASE_PACKAGE_SQL,
            (
                actor.actor_id,
                actor.firm_id,
                matter_id,
                run_id,
                artifact_id,
                list(sorted(role.value for role in _HUMAN_ROLES)),
            ),
        ).fetchone()
        if base is None:
            raise CaseAgentDocumentRevisionBlocked(
                "document is unavailable, unverified, or outside the current matter"
            )
        _assert_base_lineage(base)
        results = current_document_result_relation(connection)
        latest = connection.execute(
            f"""
            SELECT package.*
            FROM case_agent_reviewable_document_packages package
            JOIN {results} receipt
              ON receipt.successor_package_id = package.package_id
             AND receipt.firm_id = package.firm_id
             AND receipt.matter_id = package.matter_id
             AND receipt.outcome = 'PASSED'
             AND receipt.successor_package_receipt_hash = package.package_receipt_hash
            WHERE package.root_package_id = %s
              AND package.firm_id = %s AND package.matter_id = %s
              AND package.run_id = %s
            ORDER BY package.revision_number DESC, package.created_at DESC
            LIMIT 1
            """,
            (str(base["package_id"]), actor.firm_id, matter_id, run_id),
        ).fetchone()
        current = base if latest is None else latest
        template = self._templates.get(str(current["deliverable_kind"]))
        request = connection.execute(
            f"""
            SELECT request.request_id, inbox.state AS inbox_state,
                   receipt.outcome
            FROM case_agent_document_revision_requests request
            JOIN case_agent_document_revision_inbox inbox
              ON inbox.request_id = request.request_id
             AND inbox.firm_id = request.firm_id
             AND inbox.matter_id = request.matter_id
            LEFT JOIN {results} receipt
              ON receipt.request_id = request.request_id
             AND receipt.firm_id = request.firm_id
             AND receipt.matter_id = request.matter_id
            WHERE request.root_package_id = %s
              AND request.predecessor_package_id = %s
              AND request.firm_id = %s AND request.matter_id = %s
              AND request.target_template_id = %s
              AND request.target_template_version = %s
              AND request.target_template_hash = %s
            ORDER BY request.created_at DESC, request.request_id DESC
            LIMIT 1
            """,
            (
                str(base["package_id"]),
                str(current["package_id"]),
                actor.firm_id,
                matter_id,
                template.template_id,
                template.template_version,
                template.template_hash,
            ),
        ).fetchone()
        is_current = (
            str(current["template_id"]) == template.template_id
            and str(current["template_version"]) == template.template_version
            and str(current["template_hash"]) == template.template_hash
        )
        request_status: str | None = None
        request_id: str | None = None
        if request is not None:
            request_id = str(request["request_id"])
            request_status = (
                str(request["outcome"])
                if request["outcome"] is not None
                else str(request["inbox_state"])
            )
        if is_current:
            version_status = "CURRENT"
        elif request_status in {"READY", "LEASED"}:
            version_status = "GENERATING"
        elif request_status == "FAILED":
            version_status = "FAILED"
        elif request_status == "UNKNOWN":
            version_status = "UNKNOWN"
        else:
            version_status = "UPDATE_REQUIRED"
        return DocumentRevisionState(
            root_package_id=str(base["package_id"]),
            root_candidate_artifact_id=str(base["candidate_artifact_id"]),
            current_package_id=str(current["package_id"]),
            current_candidate_artifact_id=str(current["candidate_artifact_id"]),
            requested_artifact_id=artifact_id,
            deliverable_kind=str(current["deliverable_kind"]),
            output_format=ReviewableDocumentFormat(str(current["output_format"])),
            revision_number=int(current["revision_number"]),
            template_id=str(current["template_id"]),
            template_version=str(current["template_version"]),
            template_hash=str(current["template_hash"]),
            package_receipt_hash=str(current["package_receipt_hash"]),
            current_revision_request_id=(
                str(current["revision_request_id"])
                if current.get("revision_request_id") is not None
                else None
            ),
            installed_template_version=template.template_version,
            installed_template_hash=template.template_hash,
            version_status=version_status,
            request_status=request_status,
            request_id=request_id,
            can_request_revision=(
                str(base["run_status"]) == "READY_FOR_REVIEW"
                and version_status in {"UPDATE_REQUIRED", "FAILED", "UNKNOWN"}
            ),
            run_status=str(base["run_status"]),
        )


@dataclass(frozen=True)
class ContentGenerationClaim:
    review_id: str
    matter_id: str
    run_id: str
    claim_version: int


@dataclass(frozen=True)
class ClaimedContentRevision:
    revision: DocumentRevisionClaim
    predecessor_candidate_artifact_id: str


class PostgresContentGenerationJobStore:
    """Durable pre-render fencing behind the explicit content-revision flag."""

    def __init__(self, *, dsn: str, worker_actor: Actor) -> None:
        _system_worker(worker_actor)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("content generation PostgreSQL DSN is required")
        self._dsn, self._worker = dsn, worker_actor

    def claim(self) -> ContentGenerationClaim | None:
        with _transaction(self._dsn, self._worker, read_only=False) as connection:
            connection.execute(
                """UPDATE case_agent_document_content_generation_jobs
                   SET state = CASE WHEN state = 'RENDERING' THEN 'UNKNOWN' ELSE 'FAILED' END, updated_at = now()
                   WHERE firm_id = %s AND lease_expires_at <= now()
                     AND (state = 'RENDERING' OR (state = 'LEASED' AND claim_version = 3))""",
                (self._worker.firm_id,),
            )
            row = connection.execute(
                """WITH candidate AS (
                     SELECT review_id FROM case_agent_document_content_generation_jobs
                     WHERE firm_id = %s AND claim_version < 3
                       AND (state = 'READY' OR (state = 'LEASED' AND lease_expires_at <= now()))
                     ORDER BY updated_at, review_id FOR UPDATE SKIP LOCKED LIMIT 1
                   ) UPDATE case_agent_document_content_generation_jobs job
                     SET state = 'LEASED', claimed_by = %s, claim_version = claim_version + 1,
                         lease_expires_at = now() + interval '10 minutes', updated_at = now()
                     FROM candidate WHERE job.review_id = candidate.review_id
                     RETURNING job.review_id, job.matter_id, job.run_id, job.claim_version""",
                (self._worker.firm_id, self._worker.actor_id),
            ).fetchone()
            return ContentGenerationClaim(str(row["review_id"]), str(row["matter_id"]),
                str(row["run_id"]), int(row["claim_version"])) if row is not None else None

    def begin_render(self, claim: ContentGenerationClaim) -> None:
        """Commit this marker before contacting the converter; stale owners fail."""
        self._transition(claim, "LEASED", "RENDERING", require_live_lease=True)

    def load_revision(self, claim: ContentGenerationClaim) -> ClaimedContentRevision:
        self._validate_claim(claim)
        with _transaction(self._dsn, self._worker, read_only=True) as connection:
            row = connection.execute(
                """SELECT request.request_id, request.request_hash, request.root_package_id,
                          request.predecessor_package_id, request.expected_revision_number, request.requested_by,
                          predecessor.graph_id, predecessor.task_id, predecessor.attempt_id, predecessor.task_input_hash,
                          predecessor.candidate_artifact_id
                   FROM case_agent_document_content_generation_jobs job
                   JOIN case_agent_document_revision_requests request
                     ON request.content_generation_review_id = job.review_id AND request.firm_id = job.firm_id
                    AND request.matter_id = job.matter_id AND request.run_id = job.run_id
                   JOIN case_agent_reviewable_document_packages predecessor
                     ON predecessor.package_id = request.predecessor_package_id AND predecessor.firm_id = request.firm_id
                    AND predecessor.matter_id = request.matter_id AND predecessor.run_id = request.run_id
                   WHERE job.review_id = %s AND job.firm_id = %s AND job.matter_id = %s AND job.run_id = %s
                     AND job.claimed_by = %s AND job.claim_version = %s AND job.state = 'LEASED'
                     AND job.lease_expires_at > clock_timestamp()""",
                (claim.review_id, self._worker.firm_id, claim.matter_id, claim.run_id, self._worker.actor_id, claim.claim_version),
            ).fetchone()
            if row is None:
                raise CaseAgentDocumentRevisionConflict("content revision claim no longer resolves a request")
            return ClaimedContentRevision(DocumentRevisionClaim(
                request_id=str(row["request_id"]), request_hash=str(row["request_hash"]),
                root_package_id=str(row["root_package_id"]), predecessor_package_id=str(row["predecessor_package_id"]),
                expected_revision_number=int(row["expected_revision_number"]), requested_by=str(row["requested_by"]),
                run_id=claim.run_id, matter_id=claim.matter_id, graph_id=str(row["graph_id"]), task_id=str(row["task_id"]),
                attempt_id=str(row["attempt_id"]), task_input_hash=str(row["task_input_hash"]), attempt_count=claim.claim_version,
            ), str(row["candidate_artifact_id"]))

    def read_claimed_candidate(
        self, claim: ContentGenerationClaim, *, binding: DynamicDocumentTaskBinding,
        predecessor_content: bytes,
    ) -> ReviewableDocumentCandidate:
        """Load the explicit authorized edit using the real Worker identity.

        This is a read, not permission to stage a package. The coordinator must
        commit begin_render before conversion and recheck the current lineage
        when registering the successor. No human identity is impersonated.
        """
        from .case_agent_document_delivery import parse_reviewable_document_candidate

        self._validate_claim(claim)
        binding.validate()
        if (binding.firm_id, binding.matter_id, binding.run_id) != (
            self._worker.firm_id, claim.matter_id, claim.run_id,
        ):
            raise CaseAgentDocumentRevisionBlocked("claimed content binding scope differs")
        with _transaction(self._dsn, self._worker, read_only=True) as connection:
            results = current_document_result_relation(connection)
            row = connection.execute(
                f"""SELECT proposal.candidate_content, proposal.review_manifest,
                          proposal.request_hash, proposal.requested_by,
                          predecessor.candidate_hash AS predecessor_candidate_hash,
                          review.candidate_hash AS authorized_candidate_hash
                   FROM case_agent_document_content_generation_jobs job
                   JOIN case_agent_document_content_generation_reviews review
                     ON review.review_id = job.review_id AND review.firm_id = job.firm_id
                    AND review.matter_id = job.matter_id AND review.run_id = job.run_id
                   JOIN case_agent_document_revision_requests revision_request
                     ON revision_request.content_generation_review_id = review.review_id
                    AND revision_request.firm_id = review.firm_id AND revision_request.matter_id = review.matter_id
                    AND revision_request.run_id = review.run_id
                   JOIN case_agent_document_content_proposals proposal
                     ON proposal.proposal_id = review.proposal_id AND proposal.firm_id = review.firm_id
                    AND proposal.matter_id = review.matter_id AND proposal.run_id = review.run_id
                   JOIN case_agent_reviewable_document_packages predecessor
                     ON predecessor.package_id = proposal.predecessor_package_id
                    AND predecessor.firm_id = proposal.firm_id AND predecessor.matter_id = proposal.matter_id
                    AND predecessor.run_id = proposal.run_id
                   JOIN case_agent_reviewable_document_packages root
                     ON root.package_id = review.root_package_id AND root.firm_id = review.firm_id
                    AND root.matter_id = review.matter_id AND root.run_id = review.run_id
                   JOIN case_agent_runs run
                     ON run.run_id = job.run_id AND run.firm_id = job.firm_id AND run.matter_id = job.matter_id
                   JOIN matters matter ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
                   JOIN users reviewer ON reviewer.user_id = review.reviewed_by
                     AND reviewer.firm_id = review.firm_id AND reviewer.status = 'ACTIVE'
                   WHERE job.review_id = %s AND job.firm_id = %s AND job.matter_id = %s AND job.run_id = %s
                     AND job.claimed_by = %s AND job.claim_version = %s
                     AND job.state = 'LEASED' AND job.lease_expires_at > now()
                     AND review.purpose = 'GENERATE_REVIEW_COPY'
                     AND review.binding_hash = %s AND predecessor.binding_hash = review.binding_hash
                     AND predecessor.task_id = %s
                     AND review.expected_revision_number = proposal.expected_revision_number
                     AND revision_request.predecessor_package_id = proposal.predecessor_package_id
                     AND revision_request.root_package_id = review.root_package_id
                     AND revision_request.expected_revision_number = review.expected_revision_number
                     AND predecessor.revision_number = review.expected_revision_number
                     AND root.generation_mode = 'INITIAL_AGENT_TASK' AND root.revision_number = 1
                     AND COALESCE(predecessor.root_package_id, predecessor.package_id) = root.package_id
                     AND run.status = 'READY_FOR_REVIEW' AND NOT run.is_stale AND NOT run.is_cancelled
                     AND run.current_graph_id = predecessor.graph_id AND root.graph_id = predecessor.graph_id
                     AND run.snapshot_matter_version = matter.version
                     AND EXISTS (SELECT 1 FROM matter_actor_roles assignment
                       WHERE assignment.user_id = reviewer.user_id AND assignment.firm_id = reviewer.firm_id
                         AND assignment.matter_id = job.matter_id AND assignment.revoked_at IS NULL
                         AND assignment.role IN ('LEAD_LAWYER','REVIEWER'))
                     AND predecessor.package_id = (
                       SELECT current_package.package_id FROM case_agent_reviewable_document_packages current_package
                       WHERE current_package.firm_id = job.firm_id AND current_package.matter_id = job.matter_id
                         AND current_package.run_id = job.run_id
                         AND (current_package.package_id = root.package_id OR (
                           current_package.root_package_id = root.package_id AND EXISTS (
                             SELECT 1 FROM {results} receipt
                             WHERE receipt.successor_package_id = current_package.package_id
                               AND receipt.firm_id = job.firm_id AND receipt.matter_id = job.matter_id
                               AND receipt.outcome = 'PASSED')))
                       ORDER BY current_package.revision_number DESC LIMIT 1)
                     AND EXISTS (
                       SELECT 1 FROM case_agent_verification_attempts attempt
                       JOIN case_agent_verification_receipts receipt
                         ON receipt.verification_attempt_id = attempt.verification_attempt_id
                        AND receipt.run_id = attempt.run_id AND receipt.firm_id = attempt.firm_id
                        AND receipt.matter_id = attempt.matter_id
                       WHERE attempt.run_id = job.run_id AND attempt.firm_id = job.firm_id
                         AND attempt.matter_id = job.matter_id AND receipt.outcome = 'PASSED'
                         AND receipt.verification_hash = run.verification_hash
                         AND receipt.graph_hash = run.current_graph_hash AND receipt.snapshot_hash = run.snapshot_hash
                         AND receipt.artifact_lineage @> jsonb_build_array(
                           jsonb_build_object('artifact_id', root.candidate_artifact_id::text),
                           jsonb_build_object('artifact_id', root.editable_artifact_id::text),
                           jsonb_build_object('artifact_id', root.review_pdf_artifact_id::text)))""",
                (claim.review_id, self._worker.firm_id, claim.matter_id, claim.run_id,
                 self._worker.actor_id, claim.claim_version, binding.binding_hash, binding.task_id),
            ).fetchone()
            if row is None:
                raise CaseAgentDocumentRevisionConflict("claimed content authorization is unavailable or outdated")
            predecessor = parse_reviewable_document_candidate(predecessor_content, binding=binding)
            if predecessor.candidate_hash != str(row["predecessor_candidate_hash"]):
                raise CaseAgentDocumentRevisionBlocked("claimed content predecessor differs from stored package")
            candidate = verify_lawyer_document_revision(binding=binding, predecessor_content=predecessor_content,
                candidate_content=bytes(row["candidate_content"]), review_manifest=bytes(row["review_manifest"]),
                request_hash=str(row["request_hash"]), requested_by=str(row["requested_by"]))
            if candidate.candidate_hash != str(row["authorized_candidate_hash"]):
                raise CaseAgentDocumentRevisionBlocked("claimed candidate differs from lawyer authorization")
            return candidate

    def mark_unknown(self, claim: ContentGenerationClaim) -> None:
        self._transition(claim, "RENDERING", "UNKNOWN", require_live_lease=False)

    def _transition(self, claim: ContentGenerationClaim, before: str, after: str, *, require_live_lease: bool) -> None:
        self._validate_claim(claim)
        with _transaction(self._dsn, self._worker, read_only=False) as connection:
            row = connection.execute(
                """UPDATE case_agent_document_content_generation_jobs SET state = %s, updated_at = now()
                   WHERE review_id = %s AND firm_id = %s AND matter_id = %s AND run_id = %s
                     AND claimed_by = %s AND claim_version = %s AND state = %s
                     AND (NOT %s OR lease_expires_at > now()) RETURNING review_id""",
                (after, claim.review_id, self._worker.firm_id, claim.matter_id, claim.run_id,
                 self._worker.actor_id, claim.claim_version, before, require_live_lease),
            ).fetchone()
            if row is None:
                raise CaseAgentDocumentRevisionConflict("content generation claim is expired or no longer owns this stage")

    @staticmethod
    def _validate_claim(claim: ContentGenerationClaim) -> None:
        if not isinstance(claim, ContentGenerationClaim) or type(claim.claim_version) is not int or not 1 <= claim.claim_version <= 3:
            raise CaseAgentDocumentRevisionBlocked("content generation claim is invalid")
        for value in (claim.review_id, claim.matter_id, claim.run_id):
            _uuid(value, "content generation claim scope")


class DocumentRevisionWorkerGroup:
    """Share a process, alternating priority and doing at most one job per cycle."""

    def __init__(self, *workers: Any) -> None:
        if not workers or any(not callable(getattr(worker, "run_cycle", None)) for worker in workers):
            raise ValueError("document revision consumers are invalid")
        self._workers = workers
        self._next = 0

    def run_cycle(self) -> bool:
        start = self._next
        self._next = (start + 1) % len(self._workers)
        for offset in range(len(self._workers)):
            try:
                if self._workers[(start + offset) % len(self._workers)].run_cycle():
                    return True
            except Exception:
                logging.getLogger(__name__).error("document revision consumer cycle unavailable")
        return False


def preflight_content_revision_runtime(*, execution_dsn: str, verifier_dsn: str,
                                       worker_actor: Actor, verifier_actor: Actor) -> None:
    """Read-only structural startup check, not a database acceptance test."""
    _system_worker(worker_actor)
    _system_worker(verifier_actor)
    if worker_actor.firm_id != verifier_actor.firm_id or worker_actor.actor_id == verifier_actor.actor_id:
        raise CaseAgentDocumentRevisionBlocked("content revision requires independent same-firm identities")
    for dsn, actor, role in ((execution_dsn, worker_actor, "lawcase_agent_worker"),
                             (verifier_dsn, verifier_actor, "lawcase_agent_verifier")):
        with _transaction(dsn, actor, read_only=True) as connection:
            row = connection.execute("""
                SELECT current_user = %s AS correct_role,
                    (SELECT count(*) = 4 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public' AND c.relrowsecurity AND c.relforcerowsecurity
                       AND c.relname IN ('case_agent_document_content_proposals',
                         'case_agent_document_content_generation_reviews',
                         'case_agent_document_content_generation_jobs',
                         'case_agent_document_content_generation_job_events')) AS protected_tables,
                    (SELECT count(*) = 3 FROM pg_trigger t
                     WHERE NOT t.tgisinternal AND t.tgenabled IN ('O','A')
                       AND ((t.tgrelid = to_regclass('public.case_agent_document_revision_requests')
                             AND t.tgname = 'document_content_revision_requests_enqueue')
                         OR (t.tgrelid = to_regclass('public.case_agent_document_content_generation_jobs')
                             AND t.tgname IN ('content_generation_job_transition_guard',
                                             'content_generation_job_transition_audit')))) AS active_triggers,
                    (SELECT count(*) = 1 FROM pg_attribute a
                     WHERE a.attrelid = to_regclass('public.case_agent_reviewable_document_packages')
                       AND a.attname = 'content_generation_claim_version' AND NOT a.attisdropped) AS claim_column,
                    has_table_privilege(current_user,
                        to_regclass('public.case_agent_document_content_generation_jobs'), 'SELECT') AS can_read,
                    NOT has_table_privilege(current_user,
                        to_regclass('public.case_agent_document_content_generation_jobs'), 'INSERT') AS cannot_insert
                """, (role,)).fetchone()
            if row is None or any(row.get(key) is not True for key in (
                "correct_role", "protected_tables", "active_triggers", "claim_column", "can_read", "cannot_insert"
            )):
                raise CaseAgentDocumentRevisionBlocked("content revision migration or runtime privileges are incomplete")


@dataclass(frozen=True)
class RegisteredContentResult:
    package_id: str
    candidate_artifact_id: str
    receipt_hash: str
    request_id: str
    root_package_id: str
    predecessor_package_id: str
    revision_number: int
    candidate_hash: str
    binding_hash: str
    claim_version: int
    original_receipt_id: str | None = None
    original_receipt_hash: str | None = None


class PostgresContentRevisionWorker:
    """One claimed edit through rendering, private registration and independent read.

    Runtime composition is opt-in after migration qualification. No template
    compiler or model is used to rewrite lawyer text.
    """

    def __init__(self, *, worker_actor: Actor, jobs: PostgresContentGenerationJobStore,
                 binding: DocumentRevisionBindingPort, converter: ReviewOfficeConverter,
                 package_store: PostgresReviewableDocumentPackageStore,
                 package_access: PostgresReviewableDocumentPackageAccessPort,
                 receipts: PostgresDocumentRevisionReceiptStore) -> None:
        _system_worker(worker_actor)
        if (jobs._worker != worker_actor or package_store._worker != worker_actor or receipts._worker != worker_actor
                or package_access._execution_actor_id != worker_actor.actor_id
                or package_access._verifier != receipts._verifier):
            raise ValueError("content consumer execution and independent verification identities differ")
        if not package_store._content_revisions_enabled:
            raise ValueError("content package registration is not enabled for this consumer")
        self._worker, self._jobs, self._binding = worker_actor, jobs, binding
        self._converter, self._packages, self._access, self._receipts = converter, package_store, package_access, receipts

    def run_cycle(self) -> bool:
        claim = self._jobs.claim()
        if claim is None:
            return False
        revision = None
        rendering = False
        try:
            loaded = self._jobs.load_revision(claim)
            revision = loaded.revision
            predecessor = self._access.read_package(firm_id=self._worker.firm_id, matter_id=claim.matter_id,
                run_id=claim.run_id, artifact_id=loaded.predecessor_candidate_artifact_id)
            if predecessor.package_id != revision.predecessor_package_id or predecessor.revision_number != revision.expected_revision_number:
                raise CaseAgentDocumentRevisionBlocked("content predecessor read differs from the claimed request")
            binding = self._binding.resolve_document_revision(request_id=revision.request_id,
                predecessor_package_id=revision.predecessor_package_id, expected_revision_number=revision.expected_revision_number,
                content_claim_version=claim.claim_version)
            candidate = self._jobs.read_claimed_candidate(claim, binding=binding, predecessor_content=predecessor.candidate.content)
            # Treat a lost begin-render acknowledgement conservatively too.
            rendering = True
            self._jobs.begin_render(claim)
            generated = render_content_revision_candidate(candidate=candidate, binding=binding, converter=self._converter)
            request = _build_revision_staging_request(claim=revision, binding=binding, candidate=candidate,
                generated=generated, content_claim_version=claim.claim_version)
            staged = self._packages.stage_package(request)
            package = self._access.read_package(firm_id=self._worker.firm_id, matter_id=claim.matter_id,
                run_id=claim.run_id, artifact_id=staged.candidate_artifact.artifact_id)
            if (package.package_id != staged.package_id or package.receipt_hash != staged.receipt_hash
                or package.generation_mode != "LAWYER_CONTENT_REVISION" or package.revision_request_id != revision.request_id
                or package.supersedes_package_id != revision.predecessor_package_id or package.root_package_id != revision.root_package_id
                or package.revision_number != revision.expected_revision_number + 1
                or package.content_generation_claim_version != claim.claim_version
                or package.candidate_hash != candidate.candidate_hash):
                raise CaseAgentDocumentRevisionBlocked("independent content package read differs from authorization")
        except ReviewOfficeConversionUnknown:
            self._finish(claim, revision, rendering=rendering, outcome="UNKNOWN", failure_code="DOCUMENT_RENDERER_RESULT_UNKNOWN")
            return True
        except (ReviewOfficeConversionBlocked, PostgresDocumentBindingBlocked, CaseAgentDocumentPackageBlocked, CaseAgentDocumentRevisionBlocked) as error:
            # Do not log document text, provider payloads or exception messages.
            import traceback
            # Binding/revision blockers use only fixed server-side reason strings.
            # Recording that reason is necessary to distinguish a current-state
            # guard from a renderer rejection without ever writing document text.
            safe_reason = (
                str(error)
                if isinstance(error, (PostgresDocumentBindingBlocked, CaseAgentDocumentPackageBlocked, CaseAgentDocumentRevisionBlocked))
                else "office conversion rejected"
            )
            _LOG.warning("content revision rejected class=%s reason=%s locations=%s", type(error).__name__, safe_reason,
                         tuple((frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)))
            self._finish(claim, revision, rendering=rendering, outcome="FAILED", failure_code="DOCUMENT_CONTENT_REVISION_REJECTED")
            return True
        except Exception:
            if rendering:
                self._finish(claim, revision, rendering=True, outcome="UNKNOWN", failure_code="DOCUMENT_CONTENT_RESULT_UNKNOWN")
            else:
                # No converter call. Leave this bounded lease to existing recovery.
                _LOG.warning("content revision preparation unavailable before rendering")
            return True
        # Keep acknowledgement loss separate from generation failure. A committed
        # PASSED receipt must not be replaced by a fabricated FAILED/UNKNOWN row.
        for attempt in range(2):
            try:
                # Replay only this exact idempotent receipt, never rendering or
                # registration. A committed result is read before any INSERT;
                # an absent result still requires the database's live-job guard.
                self._receipts.record(request_id=revision.request_id, request_hash=revision.request_hash, matter_id=revision.matter_id,
                    outcome="PASSED", successor_package_id=package.package_id, successor_package_receipt_hash=package.receipt_hash,
                    failure_code=None)
                break
            except CaseAgentDocumentRevisionBlocked:
                # Includes an immutable conflicting receipt. Retrying cannot
                # authorize a different result or revive an expired claim.
                self._preserve_unknown(claim)
                break
            except Exception:
                if attempt == 1:
                    self._preserve_unknown(claim)
                    _LOG.warning("content revision success receipt acknowledgement unavailable; no rerender")
        return True

    def _finish(self, claim: ContentGenerationClaim, revision: DocumentRevisionClaim | None, *,
                rendering: bool, outcome: str, failure_code: str) -> None:
        if revision is not None:
            try:
                self._receipts.record(request_id=revision.request_id, request_hash=revision.request_hash,
                    matter_id=revision.matter_id, outcome=outcome, successor_package_id=None,
                    successor_package_receipt_hash=None, failure_code=failure_code)
                return
            except Exception:
                _LOG.warning("content revision terminal receipt acknowledgement unavailable")
        if rendering:
            self._preserve_unknown(claim)

    def _preserve_unknown(self, claim: ContentGenerationClaim) -> None:
        try:
            # The database refuses this if PASSED already committed. Never reset
            # a terminal job or resend a converter request to recover a receipt.
            self._jobs.mark_unknown(claim)
        except Exception:
            _LOG.warning("content revision uncertainty marker unavailable; lease reconciliation required")


class PostgresDocumentRevisionWorker:
    """Claim, compile, render, stage and independently verify one revision."""

    def __init__(
        self,
        *,
        execution_dsn: str,
        verifier_dsn: str,
        worker_actor: Actor,
        verifier_actor: Actor,
        binding: DocumentRevisionBindingPort,
        converter: ReviewOfficeConverter,
        package_store: PostgresReviewableDocumentPackageStore,
        package_access: PostgresReviewableDocumentPackageAccessPort,
    ) -> None:
        _system_worker(worker_actor)
        _system_worker(verifier_actor)
        if worker_actor.firm_id != verifier_actor.firm_id or worker_actor.actor_id == verifier_actor.actor_id:
            raise ValueError("document revision execution and verifier identities are invalid")
        if not callable(getattr(binding, "resolve_document_revision", None)):
            raise ValueError("document revision binding port is invalid")
        if not callable(getattr(converter, "convert_generated_document", None)):
            raise ValueError("document revision converter is invalid")
        self._execution_dsn = execution_dsn
        self._verifier_dsn = verifier_dsn
        self._worker = worker_actor
        self._verifier = verifier_actor
        self._binding = binding
        self._converter = converter
        self._packages = package_store
        self._access = package_access

    def run_cycle(self) -> bool:
        claim = self._claim()
        if claim is None:
            return False
        try:
            staged = self._execute(claim)
            try:
                package = self._access.read_package(
                    firm_id=self._worker.firm_id,
                    matter_id=claim.matter_id,
                    run_id=claim.run_id,
                    artifact_id=staged.candidate_artifact.artifact_id,
                )
            except CaseAgentDocumentPackageBlocked:
                raise
            except Exception as error:
                raise _DocumentRevisionRuntimeUnavailable(
                    "INDEPENDENT_READ"
                ) from error
            if (
                package.package_id != staged.package_id
                or package.receipt_hash != staged.receipt_hash
                or package.generation_mode != _REVISION_MODE
                or package.revision_request_id != claim.request_id
                or package.supersedes_package_id != claim.predecessor_package_id
                or package.root_package_id != claim.root_package_id
                or package.revision_number != claim.expected_revision_number + 1
            ):
                raise CaseAgentDocumentRevisionBlocked(
                    "independent document revision verification differs from staging"
                )
            self._record_receipt(
                claim,
                outcome="PASSED",
                successor_package_id=package.package_id,
                successor_package_receipt_hash=package.receipt_hash,
                failure_code=None,
            )
        except ReviewOfficeConversionUnknown:
            self._record_receipt(
                claim,
                outcome="UNKNOWN",
                successor_package_id=None,
                successor_package_receipt_hash=None,
                failure_code="DOCUMENT_RENDERER_RESULT_UNKNOWN",
            )
        except (
            ReviewOfficeConversionBlocked,
            PostgresDocumentBindingBlocked,
            CaseAgentDocumentPackageBlocked,
            CaseAgentDocumentRevisionBlocked,
        ) as error:
            # All four controlled boundary exceptions use fixed server-owned
            # reason strings.  Recording that category is needed to repair a
            # rejected deterministic reissue without persisting document text,
            # source labels, provider responses, identifiers, or credentials.
            _LOG.warning(
                "document revision rejected class=%s reason=%s",
                type(error).__name__,
                str(error),
            )
            self._record_receipt(
                claim,
                outcome="FAILED",
                successor_package_id=None,
                successor_package_receipt_hash=None,
                failure_code="DOCUMENT_REVISION_REJECTED",
            )
        except _DocumentRevisionRuntimeUnavailable as error:
            _LOG.warning(
                "document revision runtime unavailable at stage %s on attempt %s",
                error.stage,
                claim.attempt_count,
            )
            if claim.attempt_count >= 3:
                self._record_receipt(
                    claim,
                    outcome="FAILED",
                    successor_package_id=None,
                    successor_package_receipt_hash=None,
                    failure_code=f"DOCUMENT_REVISION_RUNTIME_{error.stage}",
                )
            else:
                self._release(claim, retry_after_seconds=30)
        except Exception:
            _LOG.warning(
                "document revision runtime unavailable at an unclassified stage "
                "on attempt %s",
                claim.attempt_count,
            )
            if claim.attempt_count >= 3:
                self._record_receipt(
                    claim,
                    outcome="FAILED",
                    successor_package_id=None,
                    successor_package_receipt_hash=None,
                    failure_code="DOCUMENT_REVISION_RUNTIME_UNAVAILABLE",
                )
            else:
                self._release(claim, retry_after_seconds=30)
        return True

    def _claim(self) -> DocumentRevisionClaim | None:
        with _transaction(self._execution_dsn, self._worker, read_only=False) as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT inbox.request_id
                    FROM case_agent_document_revision_inbox inbox
                    WHERE inbox.firm_id = %s
                      AND inbox.attempt_count < 3
                      AND inbox.available_at <= now()
                      AND (
                          inbox.state = 'READY'
                          OR (inbox.state = 'LEASED' AND inbox.lease_expires_at <= now())
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM case_agent_document_revision_receipts receipt
                          WHERE receipt.request_id = inbox.request_id
                            AND receipt.firm_id = inbox.firm_id
                            AND receipt.matter_id = inbox.matter_id
                      )
                    ORDER BY inbox.available_at, inbox.updated_at, inbox.request_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE case_agent_document_revision_inbox inbox
                   SET state = 'LEASED', claimed_by = %s,
                       lease_expires_at = now() + interval '10 minutes',
                       attempt_count = inbox.attempt_count + 1,
                       updated_at = now()
                  FROM candidate
                 WHERE inbox.request_id = candidate.request_id
                RETURNING inbox.request_id, inbox.attempt_count
                """,
                (self._worker.firm_id, self._worker.actor_id),
            ).fetchone()
            if row is None:
                return None
            details = connection.execute(
                """
                SELECT request.*, package.graph_id, package.task_id,
                       package.attempt_id, package.task_input_hash
                FROM case_agent_document_revision_requests request
                JOIN case_agent_reviewable_document_packages package
                  ON package.package_id = request.predecessor_package_id
                 AND package.run_id = request.run_id
                 AND package.firm_id = request.firm_id
                 AND package.matter_id = request.matter_id
                WHERE request.request_id = %s AND request.firm_id = %s
                """,
                (row["request_id"], self._worker.firm_id),
            ).fetchone()
            if details is None:
                raise CaseAgentDocumentRevisionBlocked(
                    "claimed document revision request is unavailable"
                )
            return DocumentRevisionClaim(
                request_id=str(details["request_id"]),
                request_hash=str(details["request_hash"]),
                root_package_id=str(details["root_package_id"]),
                predecessor_package_id=str(details["predecessor_package_id"]),
                expected_revision_number=int(details["expected_revision_number"]),
                requested_by=str(details["requested_by"]),
                run_id=str(details["run_id"]),
                matter_id=str(details["matter_id"]),
                graph_id=str(details["graph_id"]),
                task_id=str(details["task_id"]),
                attempt_id=str(details["attempt_id"]),
                task_input_hash=str(details["task_input_hash"]),
                attempt_count=int(row["attempt_count"]),
            )

    def _execute(self, claim: DocumentRevisionClaim) -> StagedReviewableDocumentPackage:
        try:
            binding = self._binding.resolve_document_revision(
                request_id=claim.request_id,
                predecessor_package_id=claim.predecessor_package_id,
                expected_revision_number=claim.expected_revision_number,
            )
        except PostgresDocumentBindingBlocked:
            raise
        except Exception as error:
            raise _DocumentRevisionRuntimeUnavailable("SOURCE_BINDING") from error
        try:
            if binding.template.deliverable_kind == "CASE_REVIEW_MEMO":
                candidate = build_deterministic_case_review_memo_candidate(binding)
            elif binding.template.deliverable_kind == "SUPPLEMENTARY_EVIDENCE_CHECKLIST":
                candidate = build_deterministic_supplementary_evidence_checklist_candidate(binding)
            elif binding.template.deliverable_kind == "DEFENCE_STATEMENT":
                candidate = build_deterministic_defence_statement_candidate(binding)
            elif binding.template.deliverable_kind == "PAYMENT_LEDGER":
                candidate = build_deterministic_payment_ledger_candidate(binding)
            elif binding.template.deliverable_kind == "EVIDENCE_CATALOGUE":
                candidate = build_deterministic_evidence_catalogue_candidate(binding)
            else:
                raise CaseAgentDocumentRevisionBlocked(
                    "document revision deliverable has no deterministic compiler"
                )
            source_labels = visible_document_source_labels(binding)
        except (CaseAgentDocumentPackageBlocked, CaseAgentDocumentRevisionBlocked):
            raise
        except Exception as error:
            raise _DocumentRevisionRuntimeUnavailable("CANDIDATE_BUILD") from error
        try:
            if binding.template.output_format is ReviewableDocumentFormat.DOCX:
                generated = create_reviewable_docx_draft(
                    candidate.to_docx_input(source_labels), converter=self._converter
                )
            else:
                sheet, columns, rows = candidate.to_xlsx_input(source_labels)
                generated = create_reviewable_xlsx_ledger(
                    approval_hash=candidate.candidate_hash,
                    sheet_name=sheet,
                    columns=columns,
                    rows=rows,
                    converter=self._converter,
                )
        except (ReviewOfficeConversionBlocked, ReviewOfficeConversionUnknown):
            raise
        except Exception as error:
            raise _DocumentRevisionRuntimeUnavailable("OFFICE_RENDER") from error
        try:
            return self._packages.stage_package(_build_revision_staging_request(
                claim=claim, binding=binding, candidate=candidate, generated=generated))
        except CaseAgentDocumentPackageBlocked:
            raise
        except Exception as error:
            raise _DocumentRevisionRuntimeUnavailable("PACKAGE_STAGING") from error

    def _release(self, claim: DocumentRevisionClaim, *, retry_after_seconds: int) -> None:
        with _transaction(self._execution_dsn, self._worker, read_only=False) as connection:
            changed = connection.execute(
                """
                UPDATE case_agent_document_revision_inbox
                   SET state = 'READY', claimed_by = NULL, lease_expires_at = NULL,
                       available_at = now() + (%s * interval '1 second'),
                       updated_at = now()
                 WHERE request_id = %s AND firm_id = %s AND matter_id = %s
                   AND state = 'LEASED' AND claimed_by = %s
                """,
                (
                    retry_after_seconds,
                    claim.request_id,
                    self._worker.firm_id,
                    claim.matter_id,
                    self._worker.actor_id,
                ),
            ).rowcount
            if changed != 1:
                raise CaseAgentDocumentRevisionBlocked(
                    "document revision lease changed before retry scheduling"
                )

    def _record_receipt(
        self, claim: DocumentRevisionClaim, *, outcome: str,
        successor_package_id: str | None, successor_package_receipt_hash: str | None,
        failure_code: str | None,
    ) -> None:
        PostgresDocumentRevisionReceiptStore(
            execution_dsn=self._execution_dsn, verifier_dsn=self._verifier_dsn,
            worker_actor=self._worker, verifier_actor=self._verifier,
        ).record(request_id=claim.request_id, request_hash=claim.request_hash, matter_id=claim.matter_id,
            outcome=outcome, successor_package_id=successor_package_id,
            successor_package_receipt_hash=successor_package_receipt_hash, failure_code=failure_code)


def _build_revision_staging_request(
    *, claim: DocumentRevisionClaim, binding: DynamicDocumentTaskBinding,
    candidate: ReviewableDocumentCandidate, generated: ReviewableOfficeDraft,
    content_claim_version: int | None = None,
) -> ReviewableDocumentPackageStagingRequest:
    candidate_content = canonical_document_candidate_bytes(candidate)
    manifest = authorized_document_source_manifest(binding.sources)
    editable, pdf = generated.editable_artifact, generated.review_pdf
    idempotency_key = _canonical_hash({
        "schema_version": "case-agent-document-content-staging-v1" if content_claim_version is not None
        else "case-agent-document-revision-staging-v1",
        "request_hash": claim.request_hash, "candidate_hash": candidate.candidate_hash,
    })
    return ReviewableDocumentPackageStagingRequest(
        idempotency_key=idempotency_key,
        run_id=claim.run_id,
        graph_id=claim.graph_id,
        task_id=claim.task_id,
        attempt_id=claim.attempt_id,
        task_input_hash=claim.task_input_hash,
        case_snapshot_hash=binding.case_snapshot_hash,
        binding_hash=binding.binding_hash,
        source_set_hash=binding.source_set_hash,
        authorized_source_refs=tuple(
            sorted(source.input_ref for source in binding.sources)
        ),
        authorized_source_manifest=manifest,
        candidate_hash=candidate.candidate_hash,
        work_plan_id=binding.work_plan_id,
        work_plan_hash=binding.work_plan_hash,
        work_plan_item_id=binding.work_plan_item.item_id,
        posture_profile_id=binding.posture_profile_id,
        posture_profile_hash=binding.posture_profile_hash,
        template_id=binding.template.template_id,
        template_version=binding.template.template_version,
        template_hash=binding.template.template_hash,
        deliverable_kind=binding.template.deliverable_kind,
        output_format=binding.template.output_format,
        candidate_bytes=candidate_content,
        candidate_content_sha256=sha256(candidate_content).hexdigest(),
        editable_bytes=editable.content,
        editable_sha256=editable.content_sha256,
        editable_media_type=editable.media_type,
        review_pdf_bytes=pdf.pdf_content,
        review_pdf_sha256=pdf.pdf_sha256,
        review_pdf_page_count=pdf.page_count,
        render_verification_hash=pdf.render_verification_hash,
        review_input_hash=generated.review_input_hash,
        generation_mode="LAWYER_CONTENT_REVISION" if content_claim_version is not None else _REVISION_MODE,
        revision_number=claim.expected_revision_number + 1,
        root_package_id=claim.root_package_id,
        supersedes_package_id=claim.predecessor_package_id,
        revision_request_id=claim.request_id,
        requested_by=claim.requested_by,
        content_generation_claim_version=content_claim_version,
    )


def preflight_content_recovery_runtime(*, verifier_dsn: str, verifier_actor: Actor) -> None:
    """Read-only structural/ACL gate; not database recovery qualification."""
    _system_worker(verifier_actor)
    with _transaction(verifier_dsn, verifier_actor, read_only=True) as connection:
        row = connection.execute("""SELECT current_user = 'lawcase_agent_verifier' AS correct_role,
            has_table_privilege(current_user, 'case_agent_document_content_recoveries', 'SELECT')
              AND has_table_privilege(current_user, 'case_agent_document_content_recoveries', 'INSERT') AS append_access,
            NOT has_table_privilege(current_user, 'case_agent_document_content_recoveries', 'UPDATE,DELETE,TRUNCATE') AS immutable_access,
            has_table_privilege(current_user, 'case_agent_document_revision_current_results', 'SELECT') AS result_access,
            has_function_privilege(current_user, 'lock_document_content_recovery_context(uuid,uuid,uuid)', 'EXECUTE') AS lock_access,
            EXISTS (SELECT 1 FROM pg_class WHERE oid = 'case_agent_document_content_recoveries'::regclass
                AND relrowsecurity AND relforcerowsecurity) AS protected_table,
            EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lawcase_document_recovery_lock_owner'
                AND NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolinherit)
              AND NOT pg_has_role(current_user, 'lawcase_document_recovery_lock_owner', 'MEMBER') AS isolated_lock_owner,
            (SELECT count(*) = 3 FROM pg_trigger WHERE tgrelid = 'case_agent_document_content_recoveries'::regclass
                AND tgname IN ('content_recovery_insert_guard','content_recovery_finishes_job','content_recoveries_append_only')
                AND tgenabled IN ('O','A')) AS recovery_triggers""").fetchone()
        fields = ("correct_role", "append_access", "immutable_access", "result_access", "lock_access",
                  "protected_table", "isolated_lock_owner", "recovery_triggers")
        if row is None or any(row.get(field) is not True for field in fields):
            raise CaseAgentDocumentRevisionBlocked("content recovery schema and privilege boundary is unavailable")


class PostgresContentRevisionRecoveryStore:
    """One explicit original request, independent reread, append-only recovery.

    Disabled until runtime composition and effective-result readers are ready.
    This port has no converter, model, object writer or job-claim capability.
    """

    def __init__(self, *, verifier_dsn: str, worker_actor: Actor, verifier_actor: Actor,
                 package_access: PostgresReviewableDocumentPackageAccessPort, enabled: bool = False) -> None:
        _system_worker(worker_actor)
        _system_worker(verifier_actor)
        if (worker_actor.firm_id != verifier_actor.firm_id or worker_actor.actor_id == verifier_actor.actor_id
                or package_access._verifier != verifier_actor
                or package_access._execution_actor_id != worker_actor.actor_id):
            raise ValueError("content recovery identities are not independent and bound")
        if type(enabled) is not bool:
            raise ValueError("content recovery enablement must be explicit")
        self._dsn, self._worker, self._verifier = verifier_dsn, worker_actor, verifier_actor
        self._access, self._enabled = package_access, enabled

    def recover(self, *, matter_id: str, run_id: str, request_id: str) -> str:
        if not self._enabled:
            raise CaseAgentDocumentRevisionBlocked("content recovery runtime is not enabled")
        for value in (matter_id, run_id, request_id):
            _uuid(value, "content recovery scope")
        with _transaction(self._dsn, self._verifier, read_only=True) as connection:
            row = connection.execute(
                """SELECT request.request_hash, request.content_generation_review_id AS review_id,
                          request.root_package_id, request.predecessor_package_id, request.expected_revision_number,
                          package.package_id, package.package_receipt_hash, package.candidate_artifact_id,
                          package.candidate_hash, package.binding_hash, job.claim_version,
                          to_jsonb(receipt) AS original_receipt
                   FROM case_agent_document_revision_requests request
                   JOIN case_agent_document_content_generation_jobs job
                     ON job.review_id = request.content_generation_review_id AND job.firm_id = request.firm_id
                    AND job.matter_id = request.matter_id AND job.run_id = request.run_id
                   JOIN case_agent_reviewable_document_packages package
                     ON package.revision_request_id = request.request_id AND package.firm_id = request.firm_id
                    AND package.matter_id = request.matter_id AND package.run_id = request.run_id
                   LEFT JOIN case_agent_document_revision_receipts receipt
                     ON receipt.request_id = request.request_id AND receipt.firm_id = request.firm_id
                    AND receipt.matter_id = request.matter_id
                   WHERE request.request_id = %s AND request.firm_id = %s AND request.matter_id = %s
                     AND request.run_id = %s AND job.claimed_by = %s AND package.staged_by = job.claimed_by
                     AND job.state IN ('UNKNOWN','SUCCEEDED')
                     AND (receipt.receipt_id IS NULL OR receipt.outcome = 'UNKNOWN')""",
                (request_id, self._worker.firm_id, matter_id, run_id, self._worker.actor_id),
            ).fetchone()
        if row is None:
            raise CaseAgentDocumentRevisionBlocked("original content recovery request is unavailable")
        original = row["original_receipt"]
        original_id = original_hash = None
        if original is not None:
            original_id = str(uuid5(UUID(request_id), "document-revision:UNKNOWN"))
            expected_original = {
                "receipt_id": original_id, "request_id": request_id, "firm_id": self._worker.firm_id,
                "matter_id": matter_id, "outcome": "UNKNOWN", "successor_package_id": None,
                "successor_package_receipt_hash": None, "failure_code": original.get("failure_code") if isinstance(original, dict) else None,
                "external_calls": 0, "executed_by": self._worker.actor_id, "verified_by": None,
            }
            if (not isinstance(original, dict) or any(original.get(k) != v for k, v in expected_original.items())
                    or not isinstance(expected_original["failure_code"], str)
                    or _FAILURE_CODE.fullmatch(expected_original["failure_code"]) is None):
                raise CaseAgentDocumentRevisionBlocked("content recovery original receipt is invalid")
            original_hash = _canonical_hash({**expected_original,
                "schema_version": "case-agent-document-revision-receipt-v1", "request_hash": str(row["request_hash"])})
            if original.get("receipt_hash") != original_hash:
                raise CaseAgentDocumentRevisionBlocked("content recovery original receipt digest differs")
        package = self._access.read_package(firm_id=self._worker.firm_id, matter_id=matter_id,
            run_id=run_id, artifact_id=str(row["candidate_artifact_id"]))
        if (not isinstance(package, ReviewableDocumentPackageRead) or package.run_id != run_id
                or package.generation_mode != "LAWYER_CONTENT_REVISION" or package.revision_request_id != request_id
                or package.package_id != str(row["package_id"]) or package.receipt_hash != str(row["package_receipt_hash"])
                or package.candidate.artifact_id != str(row["candidate_artifact_id"])
                or package.root_package_id != str(row["root_package_id"])
                or package.supersedes_package_id != str(row["predecessor_package_id"])
                or package.revision_number != int(row["expected_revision_number"]) + 1
                or package.candidate_hash != str(row["candidate_hash"]) or package.binding_hash != str(row["binding_hash"])
                or type(row["claim_version"]) is not int or not 1 <= row["claim_version"] <= 3
                or package.content_generation_claim_version != row["claim_version"]):
            raise CaseAgentDocumentRevisionBlocked("content recovery independent package differs")
        recovery_id = str(uuid5(UUID(request_id), "document-content-recovery:v1"))
        def digest(parts: tuple[str, ...]) -> str:
            return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        verification_hash = digest(("document-content-recovery-files-v1", self._worker.firm_id,
            matter_id, run_id, request_id, package.package_id, package.receipt_hash,
            package.candidate.content_sha256, package.editable.content_sha256, package.review_pdf.content_sha256,
            self._verifier.actor_id))
        recovery_hash = digest(("document-content-recovery-v1", recovery_id, request_id, str(row["request_hash"]),
            str(row["review_id"]), self._worker.firm_id, matter_id, run_id, original_id or "ABSENT", original_hash or "ABSENT",
            package.package_id, package.receipt_hash, str(row["claim_version"]), self._worker.actor_id,
            self._verifier.actor_id, verification_hash, "0"))
        expected = dict(recovery_id=recovery_id, request_id=request_id, review_id=str(row["review_id"]),
            firm_id=self._worker.firm_id, matter_id=matter_id, run_id=run_id,
            original_receipt_id=original_id, original_receipt_hash=original_hash,
            successor_package_id=package.package_id, successor_package_receipt_hash=package.receipt_hash,
            claim_version=row["claim_version"], executed_by=self._worker.actor_id, verified_by=self._verifier.actor_id,
            verification_hash=verification_hash, recovery_hash=recovery_hash)
        with _transaction(self._dsn, self._verifier, read_only=False) as connection:
            # Dedicated idempotency lock precedes the trigger's run/matter locks.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"document-content-recovery:{self._worker.firm_id}:{request_id}",))
            prior = connection.execute(
                """SELECT recovery.*, job.state AS job_state
                   FROM case_agent_document_content_recoveries recovery
                   JOIN case_agent_document_content_generation_jobs job
                     ON job.review_id = recovery.review_id AND job.firm_id = recovery.firm_id
                    AND job.matter_id = recovery.matter_id AND job.run_id = recovery.run_id
                   WHERE recovery.request_id = %s AND recovery.firm_id = %s AND recovery.matter_id = %s""",
                (request_id, self._worker.firm_id, matter_id),
            ).fetchone()
            if prior is not None:
                if (any((str(prior.get(k)) if prior.get(k) is not None else None)
                        != (str(v) if v is not None else None) for k, v in expected.items())
                        or prior.get("job_state") != "SUCCEEDED"):
                    raise CaseAgentDocumentRevisionConflict("content recovery differs from its original result")
                return recovery_id
            connection.execute(
                """INSERT INTO case_agent_document_content_recoveries (
                    recovery_id, request_id, review_id, firm_id, matter_id, run_id,
                    original_receipt_id, original_receipt_hash, successor_package_id, successor_package_receipt_hash,
                    claim_version, executed_by, verified_by, verification_hash, recovery_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", tuple(expected.values()))
        return recovery_id


class PostgresDocumentRevisionReceiptStore:
    """Shared append-only result recording; not independent document verification."""

    def __init__(self, *, execution_dsn: str, verifier_dsn: str, worker_actor: Actor, verifier_actor: Actor) -> None:
        _system_worker(worker_actor)
        _system_worker(verifier_actor)
        if worker_actor.firm_id != verifier_actor.firm_id or worker_actor.actor_id == verifier_actor.actor_id:
            raise ValueError("document receipt execution and verification identities must differ within one firm")
        if any(not isinstance(dsn, str) or not dsn.strip() for dsn in (execution_dsn, verifier_dsn)):
            raise ValueError("document receipt database configuration is required")
        self._execution_dsn, self._verifier_dsn = execution_dsn, verifier_dsn
        self._worker, self._verifier = worker_actor, verifier_actor

    def record(
        self, *, request_id: str, request_hash: str, matter_id: str, outcome: str,
        successor_package_id: str | None, successor_package_receipt_hash: str | None,
        failure_code: str | None,
    ) -> None:
        _uuid(request_id, "revision receipt request")
        _uuid(matter_id, "revision receipt matter")
        if not isinstance(request_hash, str) or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None:
            raise CaseAgentDocumentRevisionBlocked("document receipt request hash is invalid")
        if outcome not in ("PASSED", "FAILED", "UNKNOWN"):
            raise CaseAgentDocumentRevisionBlocked("document receipt outcome is invalid")
        if outcome == "PASSED":
            if failure_code is not None or not isinstance(successor_package_receipt_hash, str) or re.fullmatch(
                r"[0-9a-f]{64}", successor_package_receipt_hash
            ) is None:
                raise CaseAgentDocumentRevisionBlocked("document success receipt coordinates are invalid")
            _uuid(successor_package_id, "revision receipt successor")
        elif (successor_package_id is not None or successor_package_receipt_hash is not None
              or not isinstance(failure_code, str) or _FAILURE_CODE.fullmatch(failure_code) is None):
            raise CaseAgentDocumentRevisionBlocked("document failure receipt coordinates are invalid")
        actor = self._verifier if outcome == "PASSED" else self._worker
        dsn = self._verifier_dsn if outcome == "PASSED" else self._execution_dsn
        receipt_id = str(uuid5(UUID(request_id), f"document-revision:{outcome}"))
        receipt_hash = _canonical_hash(
            {
                "schema_version": "case-agent-document-revision-receipt-v1",
                "receipt_id": receipt_id,
                "request_id": request_id,
                "request_hash": request_hash,
                "firm_id": self._worker.firm_id,
                "matter_id": matter_id,
                "outcome": outcome,
                "successor_package_id": successor_package_id,
                "successor_package_receipt_hash": successor_package_receipt_hash,
                "failure_code": failure_code,
                "external_calls": 0,
                "executed_by": self._worker.actor_id,
                "verified_by": self._verifier.actor_id if outcome == "PASSED" else None,
            }
        )
        expected = (receipt_id, outcome, successor_package_id, successor_package_receipt_hash, failure_code, receipt_hash)

        def read_result(connection: Any) -> tuple[object, ...] | None:
            row = connection.execute(
                """SELECT receipt_id, outcome, successor_package_id,
                          successor_package_receipt_hash, failure_code, receipt_hash
                   FROM case_agent_document_revision_receipts
                   WHERE request_id = %s AND firm_id = %s AND matter_id = %s""",
                (request_id, self._worker.firm_id, matter_id),
            ).fetchone()
            if row is None:
                return None
            return tuple(str(row[key]) if row[key] is not None else None for key in (
                "receipt_id", "outcome", "successor_package_id", "successor_package_receipt_hash", "failure_code", "receipt_hash",
            ))

        with _transaction(dsn, actor, read_only=False) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"document-revision-receipt:{self._worker.firm_id}:{request_id}",))
            prior = read_result(connection)
            if prior is not None:
                if prior != expected:
                    raise CaseAgentDocumentRevisionConflict("document revision already has a different result")
                return
            connection.execute(
                """
                INSERT INTO case_agent_document_revision_receipts(
                    receipt_id, request_id, firm_id, matter_id, outcome,
                    successor_package_id, successor_package_receipt_hash,
                    failure_code, external_calls, executed_by, verified_by,
                    receipt_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    receipt_id,
                    request_id,
                    self._worker.firm_id,
                    matter_id,
                    outcome,
                    successor_package_id,
                    successor_package_receipt_hash,
                    failure_code,
                    self._worker.actor_id,
                    self._verifier.actor_id if outcome == "PASSED" else None,
                    receipt_hash,
                ),
            )
            if read_result(connection) != expected:
                raise CaseAgentDocumentRevisionBlocked("document revision receipt differs from its idempotent result")


def _request_hash(**values: object) -> str:
    review_id = values.get("content_generation_review_id")
    ordered = (
        "case-agent-document-content-revision-request-v1" if review_id is not None
        else "case-agent-document-revision-request-v1",
        values["request_id"],
        values["firm_id"],
        values["matter_id"],
        values["run_id"],
        values["root_package_id"],
        values["predecessor_package_id"],
        values["expected_revision_number"],
        values["target_template_id"],
        values["target_template_version"],
        values["target_template_hash"],
        values["source_package_receipt_hash"],
        values["requested_by"],
        values["idempotency_key_hash"],
    )
    if review_id is not None:
        _uuid(str(review_id), "content generation review")
        ordered += (review_id,)
    return sha256("|".join(str(value) for value in ordered).encode("utf-8")).hexdigest()


def _assert_base_lineage(row: Mapping[str, Any]) -> None:
    raw = row["artifact_lineage"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise CaseAgentDocumentRevisionBlocked(
                "document verification lineage is invalid"
            ) from error
    if not isinstance(raw, list):
        raise CaseAgentDocumentRevisionBlocked("document verification lineage is invalid")
    expected = {
        str(row["candidate_artifact_id"]),
        str(row["editable_artifact_id"]),
        str(row["review_pdf_artifact_id"]),
    }
    actual = {
        str(item.get("artifact_id"))
        for item in raw
        if isinstance(item, dict) and item.get("artifact_id") in expected
    }
    if actual != expected:
        raise CaseAgentDocumentRevisionBlocked(
            "document root package is incomplete in the PASSED lineage"
        )


def _human(actor: Actor) -> None:
    if (
        not isinstance(actor, Actor)
        or Role.SYSTEM_WORKER in actor.roles
        or not actor.roles.intersection(_HUMAN_ROLES)
    ):
        raise PermissionError("document revision requires an active human matter role")
    _uuid(actor.actor_id, "actor_id")
    _uuid(actor.firm_id, "firm_id")


def _system_worker(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError("document revision processing requires a dedicated SYSTEM_WORKER")
    _uuid(actor.actor_id, "actor_id")
    _uuid(actor.firm_id, "firm_id")


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise CaseAgentDocumentRevisionBlocked(f"{label} is invalid") from None


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _transaction(
    dsn: str,
    actor: Actor,
    *,
    read_only: bool,
    repeatable_read: bool = False,
):
    class _Transaction:
        def __enter__(self):
            self.connection = psycopg.connect(dsn, row_factory=dict_row)
            if repeatable_read:
                self.connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ "
                    + ("READ ONLY" if read_only else "READ WRITE")
                )
            elif read_only:
                self.connection.execute("SET TRANSACTION READ ONLY")
            self.connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (actor.firm_id,)
            )
            self.connection.execute(
                "SELECT set_config('app.actor_id', %s, true)", (actor.actor_id,)
            )
            return self.connection

        def __exit__(self, exc_type, exc, traceback):
            try:
                if exc_type is None:
                    self.connection.commit()
                else:
                    self.connection.rollback()
            finally:
                self.connection.close()
            return False

    return _Transaction()


_BASE_PACKAGE_SQL = """
SELECT package.*, run.status AS run_status, receipt.artifact_lineage
FROM case_agent_reviewable_document_packages package
JOIN case_agent_runs run
  ON run.run_id = package.run_id AND run.firm_id = package.firm_id
 AND run.matter_id = package.matter_id
 AND run.current_graph_id = package.graph_id
 AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
 AND NOT run.is_stale AND NOT run.is_cancelled
JOIN case_agent_verification_attempts attempt
  ON attempt.run_id = run.run_id AND attempt.graph_id = run.current_graph_id
 AND attempt.firm_id = run.firm_id AND attempt.matter_id = run.matter_id
JOIN case_agent_verification_receipts receipt
  ON receipt.verification_attempt_id = attempt.verification_attempt_id
 AND receipt.run_id = attempt.run_id AND receipt.firm_id = attempt.firm_id
 AND receipt.matter_id = attempt.matter_id AND receipt.outcome = 'PASSED'
 AND receipt.verification_hash = run.verification_hash
 AND receipt.graph_hash = run.current_graph_hash
 AND receipt.snapshot_hash = run.snapshot_hash
JOIN users principal
  ON principal.user_id = %s AND principal.firm_id = package.firm_id
 AND principal.status = 'ACTIVE'
WHERE package.firm_id = %s AND package.matter_id = %s AND package.run_id = %s
  AND package.generation_mode = 'INITIAL_AGENT_TASK'
  AND package.review_status = 'NEEDS_LAWYER_REVIEW'
  AND %s IN (
      package.candidate_artifact_id,
      package.editable_artifact_id,
      package.review_pdf_artifact_id
  )
  AND EXISTS (
      SELECT 1 FROM matter_actor_roles role
      WHERE role.user_id = principal.user_id AND role.firm_id = package.firm_id
        AND role.matter_id = package.matter_id AND role.role = ANY(%s)
        AND role.revoked_at IS NULL
  )
LIMIT 1
"""


__all__ = (
    "CaseAgentDocumentRevisionBlocked",
    "CaseAgentDocumentRevisionConflict",
    "DocumentRevisionBindingPort",
    "DocumentRevisionClaim",
    "DocumentRevisionState",
    "PostgresDocumentRevisionCommandStore",
    "PostgresDocumentRevisionWorker",
)
