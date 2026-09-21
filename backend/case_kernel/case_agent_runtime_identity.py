"""Exact public identity of the first production case-Agent runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Callable, Mapping, Protocol
from uuid import UUID

from .case_agent_skill_adapters import (
    COMMON_DOCUMENT_READER_MANIFEST,
    PDF_TEXT_READER_MANIFEST,
)
from .case_agent_research_adapters import PUBLIC_WEB_RESEARCH_MANIFEST
from .qwen_visual_ocr_adapter import QWEN_VISUAL_OCR_MANIFEST
from .case_agent_case_context_adapters import CASE_CONTEXT_REVIEW_MANIFEST
from .case_agent_legal_research_plan_adapters import (
    LEGAL_RESEARCH_PLANNING_MANIFEST,
)
from .case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_MANIFEST,
)
from .case_agent_lawyer_analysis_adapters import QWEN_LAWYER_ANALYSIS_MANIFEST
from .case_agent_document_adapters import (
    DOCX_DOCUMENT_DELIVERY_MANIFEST,
    XLSX_DOCUMENT_DELIVERY_MANIFEST,
)
from .case_agent_verifier import (
    FIRST_RELEASE_VERIFIER_ID,
    FIRST_RELEASE_VERIFIER_POLICY_HASH,
    FIRST_RELEASE_VERIFIER_VERSION,
)
from .controlled_defence_case_agent_planner import CONTROLLED_DEFENCE_ROUTER_ID


# Match the production composition root, not the router's fallback provider.
CASE_AGENT_PLANNER_ID = CONTROLLED_DEFENCE_ROUTER_ID


def case_agent_worker_id(firm_id: str) -> str:
    """One stable process identity per firm; distinct from its user UUID."""

    try:
        normalized = str(UUID(firm_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("case-Agent firm id must be a UUID") from error
    return f"case-agent-worker:{normalized}"


def first_release_adapter_catalog_hash(
    *,
    controlled_web_search: bool = False,
    visual_ocr: bool = False,
    dynamic_documents: bool = False,
    case_ledger_extraction: bool = False,
    lawyer_analysis: bool = False,
) -> str:
    manifests = {
        PDF_TEXT_READER_MANIFEST.tool_id: PDF_TEXT_READER_MANIFEST,
        COMMON_DOCUMENT_READER_MANIFEST.tool_id: COMMON_DOCUMENT_READER_MANIFEST,
        CASE_CONTEXT_REVIEW_MANIFEST.tool_id: CASE_CONTEXT_REVIEW_MANIFEST,
        LEGAL_RESEARCH_PLANNING_MANIFEST.tool_id: (
            LEGAL_RESEARCH_PLANNING_MANIFEST
        ),
    }
    if controlled_web_search:
        manifests[PUBLIC_WEB_RESEARCH_MANIFEST.tool_id] = PUBLIC_WEB_RESEARCH_MANIFEST
    if visual_ocr:
        manifests[QWEN_VISUAL_OCR_MANIFEST.tool_id] = QWEN_VISUAL_OCR_MANIFEST
    if dynamic_documents:
        manifests[DOCX_DOCUMENT_DELIVERY_MANIFEST.tool_id] = (
            DOCX_DOCUMENT_DELIVERY_MANIFEST
        )
        manifests[XLSX_DOCUMENT_DELIVERY_MANIFEST.tool_id] = (
            XLSX_DOCUMENT_DELIVERY_MANIFEST
        )
    if case_ledger_extraction:
        manifests[DEEPSEEK_LEDGER_EXTRACTION_MANIFEST.tool_id] = (
            DEEPSEEK_LEDGER_EXTRACTION_MANIFEST
        )
    if lawyer_analysis:
        manifests[QWEN_LAWYER_ANALYSIS_MANIFEST.tool_id] = (
            QWEN_LAWYER_ANALYSIS_MANIFEST
        )
    payload = [
        {
            "tool_id": tool_id,
            "adapter_id": manifest.adapter_id,
            "adapter_version": manifest.adapter_version,
            "execution_mode": manifest.execution_mode.value,
            "sandbox_policy_version": manifest.sandbox_policy_version,
            "sandbox_policy_hash": manifest.sandbox_policy_hash,
            "network_capable": manifest.network_capable,
            "supports_reconciliation": manifest.supports_reconciliation,
        }
        for tool_id, manifest in sorted(manifests.items())
    ]
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


FIRST_RELEASE_ADAPTER_CATALOG_HASH = first_release_adapter_catalog_hash()
FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH = first_release_adapter_catalog_hash(
    controlled_web_search=True
)
FIRST_RELEASE_VISUAL_ADAPTER_CATALOG_HASH = first_release_adapter_catalog_hash(
    visual_ocr=True
)
FIRST_RELEASE_RESEARCH_VISUAL_ADAPTER_CATALOG_HASH = (
    first_release_adapter_catalog_hash(
        controlled_web_search=True,
        visual_ocr=True,
    )
)
FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASH = first_release_adapter_catalog_hash(
    dynamic_documents=True
)
FIRST_RELEASE_RESEARCH_DOCUMENT_ADAPTER_CATALOG_HASH = (
    first_release_adapter_catalog_hash(
        controlled_web_search=True,
        dynamic_documents=True,
    )
)
FIRST_RELEASE_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH = (
    first_release_adapter_catalog_hash(
        visual_ocr=True,
        dynamic_documents=True,
    )
)
FIRST_RELEASE_RESEARCH_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH = (
    first_release_adapter_catalog_hash(
        controlled_web_search=True,
        visual_ocr=True,
        dynamic_documents=True,
    )
)
FIRST_RELEASE_LEDGER_ADAPTER_CATALOG_HASHES = frozenset(
    first_release_adapter_catalog_hash(
        controlled_web_search=controlled_web_search,
        visual_ocr=visual_ocr,
        dynamic_documents=dynamic_documents,
        case_ledger_extraction=True,
        lawyer_analysis=lawyer_analysis,
    )
    for controlled_web_search in (False, True)
    for visual_ocr in (False, True)
    for dynamic_documents in (False, True)
    for lawyer_analysis in (False, True)
)
FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASHES = frozenset(
    first_release_adapter_catalog_hash(
        controlled_web_search=controlled_web_search,
        visual_ocr=visual_ocr,
        dynamic_documents=True,
        case_ledger_extraction=case_ledger_extraction,
        lawyer_analysis=lawyer_analysis,
    )
    for controlled_web_search in (False, True)
    for visual_ocr in (False, True)
    for case_ledger_extraction in (False, True)
    for lawyer_analysis in (False, True)
)
FIRST_RELEASE_LAWYER_ANALYSIS_ADAPTER_CATALOG_HASHES = frozenset(
    first_release_adapter_catalog_hash(
        controlled_web_search=controlled_web_search,
        visual_ocr=visual_ocr,
        dynamic_documents=dynamic_documents,
        case_ledger_extraction=case_ledger_extraction,
        lawyer_analysis=True,
    )
    for controlled_web_search in (False, True)
    for visual_ocr in (False, True)
    for dynamic_documents in (False, True)
    for case_ledger_extraction in (False, True)
)
FIRST_RELEASE_ALLOWED_ADAPTER_CATALOG_HASHES = frozenset(
    {
        FIRST_RELEASE_ADAPTER_CATALOG_HASH,
        FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH,
        FIRST_RELEASE_VISUAL_ADAPTER_CATALOG_HASH,
        FIRST_RELEASE_RESEARCH_VISUAL_ADAPTER_CATALOG_HASH,
        FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASH,
        FIRST_RELEASE_RESEARCH_DOCUMENT_ADAPTER_CATALOG_HASH,
        FIRST_RELEASE_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
        FIRST_RELEASE_RESEARCH_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH,
    }
) | FIRST_RELEASE_LEDGER_ADAPTER_CATALOG_HASHES | (
    FIRST_RELEASE_LAWYER_ANALYSIS_ADAPTER_CATALOG_HASHES
)


class _HeartbeatStore(Protocol):
    def latest_worker_heartbeat(self, *, firm_id: str, worker_id: str) -> object | None: ...

    def probe_case_agent_store(self, *, firm_id: str) -> bool: ...

class ExactCaseAgentRuntimeReadiness:
    """Accept only the configured execution and independent-verifier runtime."""

    def __init__(
        self,
        *,
        store: _HeartbeatStore,
        matter_principal_probe: Callable[..., bool],
        worker_actor_ids_by_firm: Mapping[str, str],
        verifier_actor_ids_by_firm: Mapping[str, str],
        clock=None,
    ) -> None:
        if not callable(getattr(store, "latest_worker_heartbeat", None)):
            raise ValueError("case-Agent heartbeat store is invalid")
        if not callable(getattr(store, "probe_case_agent_store", None)):
            raise ValueError("case-Agent heartbeat store probe is invalid")
        if not callable(matter_principal_probe):
            raise ValueError("case-Agent matter-principal probe is invalid")
        if not isinstance(worker_actor_ids_by_firm, Mapping) or not worker_actor_ids_by_firm:
            raise ValueError("case-Agent Worker mapping is required")
        if not isinstance(verifier_actor_ids_by_firm, Mapping) or not verifier_actor_ids_by_firm:
            raise ValueError("case-Agent verifier mapping is required")
        self._store = store
        self._matter_principal_probe = matter_principal_probe
        self._workers = {
            str(UUID(str(firm_id))): str(UUID(str(actor_id)))
            for firm_id, actor_id in worker_actor_ids_by_firm.items()
        }
        self._verifiers = {
            str(UUID(str(firm_id))): str(UUID(str(actor_id)))
            for firm_id, actor_id in verifier_actor_ids_by_firm.items()
        }
        if set(self._workers) != set(self._verifiers) or any(
            self._workers[firm_id] == self._verifiers[firm_id]
            for firm_id in self._workers
        ):
            raise ValueError("case-Agent execution/verifier mappings are inconsistent")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, firm_id: str) -> bool:
        """General Agent readiness accepts every first-release catalog."""

        return self._ready(
            firm_id,
            allowed_catalog_hashes=FIRST_RELEASE_ALLOWED_ADAPTER_CATALOG_HASHES,
        )

    def ledger_ready(self, firm_id: str) -> bool:
        """Require the same live runtime plus the ledger extraction adapter."""

        return self._ready(
            firm_id,
            allowed_catalog_hashes=FIRST_RELEASE_LEDGER_ADAPTER_CATALOG_HASHES,
        )

    def document_ready(self, firm_id: str) -> bool:
        """Require DOCX/XLSX delivery, with or without the ledger adapter."""

        return self._ready(
            firm_id,
            allowed_catalog_hashes=FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASHES,
        )

    def _ready(
        self,
        firm_id: str,
        *,
        allowed_catalog_hashes: frozenset[str],
    ) -> bool:
        try:
            normalized_firm = str(UUID(firm_id))
            expected_actor = self._workers[normalized_firm]
            expected_verifier = self._verifiers[normalized_firm]
            observed_at = self._clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                return False
            if self._store.probe_case_agent_store(firm_id=normalized_firm) is not True:
                return False
            if self._matter_principal_probe(
                firm_id=normalized_firm,
                execution_actor_id=expected_actor,
                verifier_actor_id=expected_verifier,
            ) is not True:
                return False
            heartbeat = self._store.latest_worker_heartbeat(
                firm_id=normalized_firm,
                worker_id=case_agent_worker_id(normalized_firm),
            )
            return bool(
                heartbeat is not None
                and heartbeat.firm_id == normalized_firm
                and heartbeat.worker_id == case_agent_worker_id(normalized_firm)
                and heartbeat.actor_id == expected_actor
                and heartbeat.planner_id == CASE_AGENT_PLANNER_ID
                and heartbeat.adapter_catalog_hash
                in allowed_catalog_hashes
                and heartbeat.verifier_actor_id == expected_verifier
                and heartbeat.verifier_actor_id != heartbeat.actor_id
                and heartbeat.verifier_id == FIRST_RELEASE_VERIFIER_ID
                and heartbeat.verifier_version == FIRST_RELEASE_VERIFIER_VERSION
                and heartbeat.verifier_policy_hash
                == FIRST_RELEASE_VERIFIER_POLICY_HASH
                and heartbeat.observed_at <= observed_at < heartbeat.expires_at
            )
        except Exception:
            return False


__all__ = (
    "CASE_AGENT_PLANNER_ID",
    "ExactCaseAgentRuntimeReadiness",
    "FIRST_RELEASE_ALLOWED_ADAPTER_CATALOG_HASHES",
    "FIRST_RELEASE_ADAPTER_CATALOG_HASH",
    "FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASH",
    "FIRST_RELEASE_DOCUMENT_ADAPTER_CATALOG_HASHES",
    "FIRST_RELEASE_LEDGER_ADAPTER_CATALOG_HASHES",
    "FIRST_RELEASE_LAWYER_ANALYSIS_ADAPTER_CATALOG_HASHES",
    "FIRST_RELEASE_RESEARCH_ADAPTER_CATALOG_HASH",
    "FIRST_RELEASE_RESEARCH_DOCUMENT_ADAPTER_CATALOG_HASH",
    "FIRST_RELEASE_RESEARCH_VISUAL_ADAPTER_CATALOG_HASH",
    "FIRST_RELEASE_RESEARCH_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH",
    "FIRST_RELEASE_VISUAL_ADAPTER_CATALOG_HASH",
    "FIRST_RELEASE_VISUAL_DOCUMENT_ADAPTER_CATALOG_HASH",
    "case_agent_worker_id",
    "first_release_adapter_catalog_hash",
)
