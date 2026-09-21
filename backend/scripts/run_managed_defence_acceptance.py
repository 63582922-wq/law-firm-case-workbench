#!/usr/bin/env python3
"""Execute one bounded, synthetic, managed defendant-response acceptance run.

This is deliberately an operational acceptance command rather than another
offline fixture.  It admits the frozen 88-page source set through the actual
Web intake services, writes the minimum governed case inputs, allows exactly
one fresh lawyer-analysis task, and then uses the activated plan to create a
review-only civil defence statement.  It never retries a provider submission,
locks a bundle, signs a document, or sends material outside the managed stack.

The command is valid only for the fixed loopback synthetic-acceptance runtime.
It must never be pointed at a client matter or a production firm.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any, AsyncIterable, Callable, Iterable, Mapping
import urllib.request
from uuid import NAMESPACE_URL, UUID, uuid5
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
import psycopg
from psycopg.rows import dict_row


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from case_api.persistent_identity import (  # noqa: E402
    AuthenticationMethod,
    ServerIdentityContext,
)
from case_api.web_runtime import (  # noqa: E402
    WebRuntimeComposition,
    build_web_runtime_composition,
    load_web_runtime_settings,
)
from case_api.web_case_posture import _stage_key as _web_posture_stage_key  # noqa: E402
from case_api.web_case_agent_run_identity import (  # noqa: E402
    derive_web_case_agent_entity_id,
)
from case_kernel.case_agent_lawyer_analysis_adapters import (  # noqa: E402
    LAWYER_ANALYSIS_SKILL_ID,
)
from case_kernel.case_agent_supervisor import AgentDeliverableKind  # noqa: E402
from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore  # noqa: E402
from case_kernel.evidence_refs import EvidenceLink  # noqa: E402
from case_kernel.fact_claim_ledger import (  # noqa: E402
    AssertionOrigin,
    ClaimResponsePosition,
    FactStatus,
)
from case_kernel.golden_case_source import (  # noqa: E402
    GeneratedGoldenCase,
    GeneratedFile,
    generate_golden_case,
    load_authoritative_case,
    read_generated_pages,
)
from case_kernel.legal_source_postgres import (  # noqa: E402
    LegalAuthorityLevel,
    LegalBundleSegmentSelection,
    LegalEventKind,
    LegalRateFormulaKind,
)
from case_kernel.models import Actor, Matter, Role  # noqa: E402
from case_kernel.official_source_private_store import (  # noqa: E402
    OfficialSourceObjectStateUnknown,
    OfficialSourceObjectStoreBlocked,
    extract_literal_official_source_text,
)


_PRIMARY_ACCEPTANCE_NAME = "managed-defence-single-call-v1"


def _acceptance_evidence_scope() -> dict[str, object]:
    """Do not promote this seeded integration fixture to raw-case acceptance."""
    return {
        "schema_version": "managed-defence-evidence-scope-v1",
        "scope": "SEEDED_FACTS_TO_REVIEW_DOCUMENT",
        "facts_preconfirmed_by_harness": True,
        "source_fixture_contains_machine_readable_markers": True,
        "proves_unassisted_material_understanding": False,
        "proves_visual_ocr": False,
        "proves_lawyer_user_acceptance": False,
        "commercial_release_accepted": False,
    }


_CONTRACT_REPAIR_ACCEPTANCE_NAME = "managed-defence-single-call-v2"
_FINAL_RUNTIME_ACCEPTANCE_NAME = "managed-defence-single-call-v3"
_SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME = "managed-defence-source-bound-numeric-v4"
_M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME = "m1-source-qualified-final-acceptance"
_M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME = (
    "m2-source-qualified-post-reconciliation-acceptance"
)
_M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME = (
    "m3-current-runtime-full-flow-acceptance"
)
_M4_FULL_DELIVERY_ACCEPTANCE_NAME = (
    "m4-current-runtime-full-delivery-acceptance"
)
_M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME = (
    "m5-current-runtime-full-delivery-fresh-source-acceptance"
)
_M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME = (
    "m6-current-runtime-full-delivery-confirmed-evidence-acceptance"
)
_M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME = (
    "m7-current-runtime-full-delivery-fresh-source-confirmed-evidence-acceptance"
)
_M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME = (
    "m8-current-runtime-full-delivery-post-parser-repair-acceptance"
)
_M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME = (
    "m9-discovery-issue-legal-reconfirmation-final-delivery-acceptance"
)
_M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME = (
    "m10-discovery-issue-legal-reconfirmation-full-delivery-acceptance"
)
_M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME = (
    "m11-discovery-issue-legal-reconfirmation-fresh-source-full-delivery-acceptance"
)
_SOURCE_QUALIFIED_ACCEPTANCE_NAMES = frozenset(
    {
        _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME,
        _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME,
        _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME,
        _M4_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
        _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
        _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
        _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
    }
)
_ACCEPTANCE_SCENARIOS = frozenset(
    {
        _PRIMARY_ACCEPTANCE_NAME,
        _CONTRACT_REPAIR_ACCEPTANCE_NAME,
        _FINAL_RUNTIME_ACCEPTANCE_NAME,
        _SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME,
        _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME,
        _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME,
        _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME,
        _M4_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
        _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
        _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
        _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
    }
)
# This command is one-shot and process-local. ``main`` may move it only to a
# declared, isolated acceptance fixture before any identity or composition is
# constructed; arbitrary scenario labels are intentionally impossible.
_ACCEPTANCE_NAME = _PRIMARY_ACCEPTANCE_NAME
# The national database's otherwise canonical PDF is intentionally not used
# here: its certificate is presently expired in the managed acceptance clock.
# This exact Supreme People's Procuratorate public page is the registered,
# TLS-verified fallback for CN-CIVIL-CODE-680. The command still requires a
# live, certificate-validated, unredirected response and preserves its exact
# bytes.
_OFFICIAL_SOURCE_URL = "https://www.spp.gov.cn/zdgz/202006/t20200602_463886.shtml"
_OFFICIAL_SOURCE_HOST = "www.spp.gov.cn"
_OFFICIAL_SOURCE_PUBLISHER = "最高人民检察院官网公开文本"
_OFFICIAL_SOURCE_PROVISION_LOCATOR = (
    "《中华人民共和国民法典》第六百七十九条、第六百八十条；"
    "本合成验收仅作受控来源绑定，具体条文适用仍待律师复核。"
)
_OFFICIAL_SOURCE_MEDIA_TYPE = "text/html"
_OFFICIAL_SOURCE_USER_AGENT = "LawCaseWorkbench-OfficialSourceFetch/0.1"
_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256 = (
    "baf60cd0e4f3e3ffc2ca6533731fc8faa246db0b47b694ef00fa0053cf56b462"
)
_OFFICIAL_SOURCE_FROZEN_BYTES = 488_833
_OFFICIAL_SOURCE_REQUIRED_MARKERS = (
    "中华人民共和国民法典",
    "第六百七十九条",
    "第六百八十条",
)
_M1_OFFICIAL_SOURCE_URL = (
    "https://tjca.miit.gov.cn/zwgk/zcwj/flfg/art/2020/"
    "art_20cf1a2e1b854924b5caa744c8045d1f.html"
)
_M1_OFFICIAL_SOURCE_HOST = "tjca.miit.gov.cn"
_M1_OFFICIAL_SOURCE_PUBLISHER = "工业和信息化部天津市通信管理局官网公开文本"
_M1_OFFICIAL_SOURCE_PROVISION_LOCATOR = (
    "《中华人民共和国民法典》第六百七十九条、第六百八十条；"
    "本合成验收仅作受控来源绑定，具体条文适用仍待律师复核。"
)
_M1_OFFICIAL_SOURCE_MEDIA_TYPE = "text/html"
_M1_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256 = (
    "2efc5b6c1a6ac1e5b509f93a94d93326c81c7fe1d2e4525120cf13a417255277"
)
_M1_OFFICIAL_SOURCE_FROZEN_BYTES = 1_164_455
_M1_OFFICIAL_SOURCE_REQUIRED_MARKERS = _OFFICIAL_SOURCE_REQUIRED_MARKERS
_FIXED_BUNDLE_END = date(2026, 9, 4)
_MAX_OFFICIAL_SOURCE_BYTES = 8 * 1024 * 1024
# M2 may use the already verified, exact same-firm public-law snapshot only
# from a bounded capture window.  This is not a substitute for production
# source review: it makes a transient public-web outage unable to erase a
# cryptographically authenticated source that was captured for this same
# bounded acceptance hours earlier.
_M2_FIRM_FROZEN_SOURCE_MAX_AGE = timedelta(hours=24)
_M2_FIRM_FROZEN_SOURCE_MAX_CLOCK_SKEW = timedelta(minutes=5)
_POLL_SECONDS = 2.0
_MAX_WAIT_SECONDS = 1_200
# The frozen 11-file intake advances the matter from version 1 to 12.  Every
# governed pre-Agent command then advances exactly once.  Keeping these input
# versions fixed makes an interrupted, already-committed command replay its
# original receipt rather than accidentally changing its idempotency payload.
_MATERIAL_COMPLETE_VERSION = 12
_POSTURE_COMPLETE_VERSION = _MATERIAL_COMPLETE_VERSION + 5
_FACTS_COMPLETE_VERSION = _POSTURE_COMPLETE_VERSION + 10
_GOVERNED_INPUT_COMPLETE_VERSION = _FACTS_COMPLETE_VERSION + 3
_LEGAL_CONTEXT_COMPLETE_VERSION = _GOVERNED_INPUT_COMPLETE_VERSION + 4
_RESPONSE_SOURCE_SCOPE_RULE_ID = "PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE"
_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "1.0.0"
# Rule versions are firm-scoped and source-bound.  M1 uses a different,
# immutable Civil Code snapshot from V4, so it must never impersonate V4's
# source-bound rule version in the same synthetic firm.
_M1_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M1-SOURCE-2026-09-04"
_M2_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M2-SOURCE-2026-09-04"
_M3_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M3-SOURCE-2026-09-05"
_M4_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M4-SOURCE-2026-09-11"
_M5_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M5-SOURCE-2026-09-11"
_M6_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M6-SOURCE-2026-09-11"
_M7_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M7-SOURCE-2026-09-11"
_M8_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M8-SOURCE-2026-09-11"
_M9_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M9-SOURCE-2026-09-11"
_M10_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M10-SOURCE-2026-09-11"
_M11_RESPONSE_SOURCE_SCOPE_RULE_VERSION = "M11-SOURCE-2026-09-11"
_FIRST_RUN_OBJECTIVE = "围绕已确认被告一审民间借贷诉请形成可审阅的律师决策包与民事答辩状候选。"
_FIRST_RUN_SUCCESS_CRITERIA = (
    "输出来源绑定、待律师决定的律师案件决策包",
    "仅在主办律师激活动态计划后生成民事答辩状 DOCX/PDF 审阅候选",
)
_FIRST_RUN_CONSTRAINTS = (
    "首轮最多一笔外部模型调用，且只允许律师决策包任务使用",
    "不得自动确认事实、金额、法律结论、终审、锁定或对外提交",
    "原始材料不因规划或律师决策包任务离开私有证据链",
)


class ManagedDefenceAcceptanceBlocked(RuntimeError):
    """The durable acceptance state must be retained rather than retried."""


@dataclass(frozen=True)
class _OfficialSourceSpec:
    """One exact public-law snapshot permitted by one synthetic scenario.

    This exists so a later corrected acceptance cannot silently replace the
    source attached to an already-created matter.  The legacy globals remain
    readable for the historic v1--v4 test contracts and their focused tests.
    """

    source_id: str
    publisher: str
    official_url: str
    host: str
    provision_locator: str
    content_media_type: str
    frozen_content_sha256: str
    frozen_bytes: int
    required_markers: tuple[str, ...]
    license_basis: str


def _current_official_source_spec() -> _OfficialSourceSpec:
    """Return the immutable source contract for the selected scenario.

    M1 is a new, source-qualified matter.  It is not a retry, rewrite, or
    source substitution for the V4 matter, whose historic source remains
    intentionally immutable and blocked from document generation.
    """

    if _ACCEPTANCE_NAME in _SOURCE_QUALIFIED_ACCEPTANCE_NAMES:
        return _OfficialSourceSpec(
            source_id="CN-CIVIL-CODE-680",
            publisher=_M1_OFFICIAL_SOURCE_PUBLISHER,
            official_url=_M1_OFFICIAL_SOURCE_URL,
            host=_M1_OFFICIAL_SOURCE_HOST,
            provision_locator=_M1_OFFICIAL_SOURCE_PROVISION_LOCATOR,
            content_media_type=_M1_OFFICIAL_SOURCE_MEDIA_TYPE,
            frozen_content_sha256=_M1_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256,
            frozen_bytes=_M1_OFFICIAL_SOURCE_FROZEN_BYTES,
            required_markers=_M1_OFFICIAL_SOURCE_REQUIRED_MARKERS,
            license_basis=(
                "工业和信息化部天津市通信管理局官网公开的法律全文；"
                "本合成验收仅用于内部来源完整性验证，不再发布原文。"
            ),
        )
    return _OfficialSourceSpec(
        source_id="CN-CIVIL-CODE-680",
        publisher=_OFFICIAL_SOURCE_PUBLISHER,
        official_url=_OFFICIAL_SOURCE_URL,
        host=_OFFICIAL_SOURCE_HOST,
        provision_locator=_OFFICIAL_SOURCE_PROVISION_LOCATOR,
        content_media_type=_OFFICIAL_SOURCE_MEDIA_TYPE,
        frozen_content_sha256=_OFFICIAL_SOURCE_FROZEN_CONTENT_SHA256,
        frozen_bytes=_OFFICIAL_SOURCE_FROZEN_BYTES,
        required_markers=_OFFICIAL_SOURCE_REQUIRED_MARKERS,
        license_basis=(
            "最高人民检察院官网公开的法律文本；"
            "本合成验收仅用于内部来源完整性验证，不再发布原文。"
        ),
    )


def _response_source_scope_rule_identity() -> tuple[str, str]:
    """Return the source-bound rule identity for the selected fixed scenario."""

    if _ACCEPTANCE_NAME == _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME:
        version = _M1_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME:
        version = _M2_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME:
        version = _M3_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M4_FULL_DELIVERY_ACCEPTANCE_NAME:
        version = _M4_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME:
        version = _M5_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME:
        version = _M6_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME:
        version = _M7_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME:
        version = _M8_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME:
        version = _M9_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME:
        version = _M10_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    elif _ACCEPTANCE_NAME == _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME:
        version = _M11_RESPONSE_SOURCE_SCOPE_RULE_VERSION
    else:
        version = _RESPONSE_SOURCE_SCOPE_RULE_VERSION
    return (
        _RESPONSE_SOURCE_SCOPE_RULE_ID,
        version,
    )


@dataclass(frozen=True)
class AcceptedInput:
    file_name: str
    content_sha256: str
    page_count: int
    route: str


@dataclass(frozen=True)
class _PersistedIntakeRecord:
    """A non-sensitive completion receipt used only for fixed-fixture recovery.

    The acceptance command never reads an object key, source bytes, browser
    session secret, or model credential from this record.  It deliberately
    binds recovery to the immutable original identity *and* its content hash:
    two separately admitted originals may have exactly the same bytes.
    """

    file_name: str
    content_sha256: str
    page_count: int
    route: str
    completion_matter_version: int


@dataclass(frozen=True)
class _FirmFrozenOfficialSourceReference:
    """One already-approved, same-firm matter binding for a public source."""

    matter_id: str
    retrieved_at: datetime


def _validate_firm_frozen_source_recency(
    retrieved_at: datetime, *, scenario_label: str
) -> None:
    """Keep an exact reused public source fresh and honestly timestamped."""

    if retrieved_at.tzinfo is None:
        raise ManagedDefenceAcceptanceBlocked(
            f"{scenario_label} 同律所冻结法源缺少时区明确的捕获时间；未继续模型运行。"
        )
    now = _safe_now()
    if retrieved_at > now + _M2_FIRM_FROZEN_SOURCE_MAX_CLOCK_SKEW:
        raise ManagedDefenceAcceptanceBlocked(
            f"{scenario_label} 同律所冻结法源捕获时间晚于受控时钟；未继续模型运行。"
        )
    if now - retrieved_at > _M2_FIRM_FROZEN_SOURCE_MAX_AGE:
        raise ManagedDefenceAcceptanceBlocked(
            f"{scenario_label} 同律所冻结法源已超过 24 小时复用窗口；未继续模型运行。"
        )


def _validate_m2_firm_frozen_source_recency(retrieved_at: datetime) -> None:
    _validate_firm_frozen_source_recency(retrieved_at, scenario_label="M2")


def _validate_m3_firm_frozen_source_recency(retrieved_at: datetime) -> None:
    _validate_firm_frozen_source_recency(retrieved_at, scenario_label="M3")


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _select_acceptance_scenario(name: str) -> None:
    """Select one declared synthetic fixture before any durable operation.

    v2 is not a retry of v1, v3 is not a retry of v2, and v4 is a single
    post-policy-repair acceptance rather than a retry of v3. M1 is a
    source-qualified acceptance whose one external result is now immutable
    and indeterminate. M2 is a separately named post-reconciliation case with
    the same independently frozen source contract, a new matter/run namespace
    and its own rule version; it never amends, recovers or resends M1. M3 is a
    separate, current-runtime full-flow case whose source preflight may read
    only a fresh, same-firm approved public-law object. Keeping the selection
    here, rather than accepting an arbitrary CLI name or environment value,
    makes those distinctions auditable.
    """

    global _ACCEPTANCE_NAME
    if name not in _ACCEPTANCE_SCENARIOS:
        raise ManagedDefenceAcceptanceBlocked("合成验收场景不在固定允许列表中。")
    _ACCEPTANCE_NAME = name


def _acceptance_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"lawcase:{_ACCEPTANCE_NAME}:{label}"))


def _requested_deliverables_for_acceptance() -> tuple[AgentDeliverableKind, ...]:
    """Return the closed, server-sorted delivery request for this fixture.

    M4 is intentionally a new matter/run namespace, not a fourth attempt at
    an earlier model request.  It validates the lawyer-facing four-output
    journey against the same bounded analysis contract as M3.
    """

    if _ACCEPTANCE_NAME in {
        _M4_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
        _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
        _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
        _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
    }:
        return (
            AgentDeliverableKind.CASE_REVIEW_MEMO,
            AgentDeliverableKind.DEFENCE_STATEMENT,
            AgentDeliverableKind.EVIDENCE_CATALOGUE,
            AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST,
        )
    return (AgentDeliverableKind.DEFENCE_STATEMENT,)


def _uses_current_runtime_source_preflight() -> bool:
    return _ACCEPTANCE_NAME in {
        _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME,
        _M4_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
        _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
        _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
        _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
    }


def _key(label: str) -> str:
    """Return one deterministic key accepted by every Web command boundary.

    The acceptance runner invokes services directly, but those services retain
    the browser's strict idempotency-key contract.  A readable ``name:label``
    key was accepted by the material intake adapters yet rejected by posture
    confirmation because ``:`` is not a browser-safe key character.  Hashing
    the stable internal label preserves replay identity without weakening that
    common boundary or leaking source labels into persistence.
    """

    if not isinstance(label, str) or not label:
        raise ManagedDefenceAcceptanceBlocked("合成验收幂等键标签无效。")
    digest = sha256(f"{_ACCEPTANCE_NAME}|{label}".encode("utf-8")).hexdigest()
    return f"managed-defence-{digest}"


def _safe_now() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_mapping(env_file: Path | None) -> dict[str, str]:
    """Build the Web composition inputs without widening the acceptance boundary.

    The command normally runs *inside* the local managed API container.  That
    container already receives only the Web runtime values it is authorized to
    hold; mounting the host's complete ``local-managed.env`` there would also
    expose Worker-only provider credentials.  An explicit file remains
    supported for an isolated operator invocation, but the in-stack command
    deliberately composes from its injected environment alone.
    """

    resolved = dict(os.environ)
    if env_file is None:
        return resolved
    if not env_file.is_file():
        raise ManagedDefenceAcceptanceBlocked("指定的受管验收环境文件不存在。")
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not key.replace("_", "").isalnum():
            raise ManagedDefenceAcceptanceBlocked("本地受管验收环境文件格式无效。")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        resolved.setdefault(key, value)
    return resolved


def _build_composition(env_file: Path | None) -> WebRuntimeComposition:
    settings = load_web_runtime_settings(_runtime_mapping(env_file))
    if not settings.local_managed_acceptance_auth_bypass:
        raise ManagedDefenceAcceptanceBlocked(
            "该命令只允许在固定 loopback 合成验收身份开启时运行。"
        )
    if not settings.document_worker_enabled:
        raise ManagedDefenceAcceptanceBlocked("受管文书渲染 Worker 未启用。")
    if (
        not settings.local_managed_acceptance_firm_id
        or not settings.local_managed_acceptance_lead_actor_id
    ):
        raise ManagedDefenceAcceptanceBlocked("合成验收律师身份未配置。")
    composition = build_web_runtime_composition(settings)
    dependencies = composition.api_dependencies
    if (
        dependencies.case_agent_control_service is None
        or dependencies.dynamic_case_plan_service is None
        or dependencies.case_agent_document_review_service is None
    ):
        raise ManagedDefenceAcceptanceBlocked("受管 Agent、动态计划或文书复核服务未就绪。")
    return composition


def _issue_fixture_identity(
    composition: WebRuntimeComposition,
) -> tuple[ServerIdentityContext, str]:
    settings = composition.settings
    assert settings.local_managed_acceptance_firm_id is not None
    assert settings.local_managed_acceptance_lead_actor_id is not None
    now = _safe_now()
    actor = Actor(
        actor_id=settings.local_managed_acceptance_lead_actor_id,
        firm_id=settings.local_managed_acceptance_firm_id,
        roles=frozenset({Role.LEAD_LAWYER}),
    )
    issuance = ServerIdentityContext(
        actor=actor,
        session_id=_acceptance_id(f"session-issuance:{now.isoformat()}"),
        issuer=settings.oidc_issuer,
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    grant = composition.api_dependencies.session_authority.issue(identity=issuance)
    identity = ServerIdentityContext(
        actor=actor,
        session_id=grant.session_id,
        issuer=settings.oidc_issuer,
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now,
        expires_at=grant.expires_at,
    )
    identity.validate(now=now)
    return identity, grant.session_id


def _matter_id(identity: ServerIdentityContext) -> str:
    return _acceptance_id(f"matter:{identity.actor.firm_id}")


def _create_new_matter(
    composition: WebRuntimeComposition, identity: ServerIdentityContext
) -> Matter:
    matter_id = _matter_id(identity)
    store = composition.api_dependencies.matter_store
    try:
        existing = store.get(matter_id, firm_id=identity.actor.firm_id)
    except KeyError:
        existing = None
    if existing is not None:
        raise ManagedDefenceAcceptanceBlocked(
            "固定合成验收案件已经存在；为避免重传材料或创建第二次模型调用，本次未继续。"
        )
    matter = Matter(
        matter_id=matter_id,
        firm_id=identity.actor.firm_id,
        title="合成验收｜民间借贷一审被告应诉（88页）",
    )
    receipt = store.create(
        matter=matter,
        actor=identity.actor,
        idempotency_key=_key("create-matter"),
    )
    if receipt.matter_id != matter_id or receipt.matter_version != 1:
        raise ManagedDefenceAcceptanceBlocked("合成验收案件创建回执不一致。")
    return store.get(matter_id, firm_id=identity.actor.firm_id)


def _current_matter(
    composition: WebRuntimeComposition, identity: ServerIdentityContext
) -> Matter:
    return composition.api_dependencies.matter_store.get(
        _matter_id(identity), firm_id=identity.actor.firm_id
    )


def _expected_input(item: GeneratedFile) -> AcceptedInput:
    if item.media_type == "application/pdf":
        route = "EVIDENCE_ORIGINAL"
    elif item.media_type == "image/jpeg":
        route = "VISUAL_OCR"
    else:
        raise ManagedDefenceAcceptanceBlocked("冻结合成案卷出现不支持的材料类型。")
    return AcceptedInput(
        file_name=item.file_name,
        content_sha256=item.file_sha256,
        page_count=item.page_count,
        route=route,
    )


def _common_material_reservation_key(item: GeneratedFile) -> str:
    """Return a replay-safe key for this exact frozen original occurrence.

    A byte hash proves content identity, not original identity.  A party can
    supply a photograph and a duplicate copy as two separately preserved
    originals, so a source occurrence must be part of the reservation key.
    Reusing this exact logical source remains idempotent; a different source
    with identical bytes remains a distinct immutable upload.
    """

    if item.media_type != "image/jpeg" or not item.logical_code.strip():
        raise ManagedDefenceAcceptanceBlocked("图片材料的冻结来源身份无效。")
    return _key(f"common-image:{item.logical_code}:{item.file_sha256}")


def _assert_accepted_prefix(
    generated: GeneratedGoldenCase,
    accepted_prefix: tuple[AcceptedInput, ...],
) -> None:
    if len(accepted_prefix) > len(generated.ingest_order):
        raise ManagedDefenceAcceptanceBlocked("已接收材料数超出冻结案卷。")
    files = {item.file_name: item for item in generated.files}
    for index, accepted in enumerate(accepted_prefix):
        name = generated.ingest_order[index]
        expected = _expected_input(files[name])
        if accepted != expected:
            raise ManagedDefenceAcceptanceBlocked("已接收材料与冻结案卷顺序或原件身份不一致。")


async def _single_chunk(value: bytes) -> AsyncIterable[bytes]:
    yield value


async def _admit_sources(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    generated: GeneratedGoldenCase,
    *,
    accepted_prefix: tuple[AcceptedInput, ...] = (),
) -> tuple[AcceptedInput, ...]:
    files = {item.file_name: item for item in generated.files}
    source_root = Path(generated.sources_root)
    _assert_accepted_prefix(generated, accepted_prefix)
    accepted: list[AcceptedInput] = list(accepted_prefix)
    for name in generated.ingest_order[len(accepted_prefix):]:
        item = files[name]
        content = (source_root / item.file_name).read_bytes()
        if sha256(content).hexdigest() != item.file_sha256:
            raise ManagedDefenceAcceptanceBlocked("冻结合成原件哈希发生变化。")
        expected_version = _current_matter(composition, identity).version
        if item.media_type == "application/pdf":
            slot = composition.material_upload_service.create_slot(
                identity=identity,
                matter_id=_matter_id(identity),
                expected_version=expected_version,
                client_filename=item.file_name,
                declared_content_length=len(content),
            )
            receipt = await composition.material_upload_service.accept_content(
                identity=identity,
                matter_id=_matter_id(identity),
                upload_id=slot.upload_id,
                chunks=_single_chunk(content),
            )
            if (
                receipt.content_sha256 != item.file_sha256
                or receipt.page_count != item.page_count
                or receipt.matter_version != expected_version + 1
            ):
                raise ManagedDefenceAcceptanceBlocked("PDF 原件接收回执与冻结清单不一致。")
            accepted.append(
                AcceptedInput(
                    file_name=item.file_name,
                    content_sha256=receipt.content_sha256,
                    page_count=receipt.page_count,
                    route="EVIDENCE_ORIGINAL",
                )
            )
            continue
        if item.media_type != "image/jpeg":
            raise ManagedDefenceAcceptanceBlocked("冻结合成案卷出现不支持的材料类型。")
        key = _common_material_reservation_key(item)
        slot = composition.common_material_upload_service.create_slot(
            identity=identity,
            matter_id=_matter_id(identity),
            expected_version=expected_version,
            client_filename=item.file_name,
            declared_byte_size=len(content),
            declared_media_type=item.media_type,
            idempotency_key=key,
        )
        receipt = await composition.common_material_upload_service.accept_content(
            identity=identity,
            matter_id=_matter_id(identity),
            upload_id=slot.upload_id,
            idempotency_key=key,
            chunks=_single_chunk(content),
        )
        if (
            receipt.content_sha256 != item.file_sha256
            or receipt.matter_version != expected_version + 1
            or receipt.formal_fact
            or receipt.formal_transaction
            or receipt.legal_conclusion
            or receipt.evidence_decision
            or receipt.court_ready
        ):
            raise ManagedDefenceAcceptanceBlocked("图片材料接收边界与合成验收约束不一致。")
        accepted.append(
            AcceptedInput(
                file_name=item.file_name,
                content_sha256=receipt.content_sha256,
                page_count=item.page_count,
                route=str(receipt.route.value),
            )
        )
    if (
        len(accepted) != len(generated.files)
        or sum(item.page_count for item in accepted)
        != sum(item.page_count for item in generated.files)
    ):
        raise ManagedDefenceAcceptanceBlocked("合成案卷没有完整进入受管接收链。")
    evidence = composition.evidence_manifest_store.get_evidence_snapshot(
        matter_id=_matter_id(identity), actor=identity.actor
    )
    expected_evidence = Counter(
        (item.file_name, item.file_sha256, item.media_type, item.page_count)
        for item in generated.files
    )
    try:
        observed_evidence = Counter(
            (
                str(row["original_label"]),
                str(row["original_file_sha256"]),
                str(row["media_type"]),
                int(row["page_count"]),
            )
            for row in evidence.original_files
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ManagedDefenceAcceptanceBlocked("证据账本原件记录无法与冻结案卷核验。") from error
    if (
        observed_evidence != expected_evidence
        or len(evidence.pages) != sum(item.page_count for item in generated.files)
    ):
        raise ManagedDefenceAcceptanceBlocked("证据账本原件身份或页数与冻结案卷不一致。")
    return tuple(accepted)


def _evidence_link(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    generated: GeneratedGoldenCase,
    *,
    source_code: str,
    page_number: int,
) -> EvidenceLink:
    source_file = next(
        (
            item
            for item in generated.files
            if item.source_code == source_code and item.media_type == "application/pdf"
        ),
        None,
    )
    if source_file is None:
        raise ManagedDefenceAcceptanceBlocked("合成验收事实没有对应的 PDF 原件。")
    snapshot = composition.evidence_manifest_store.get_evidence_snapshot(
        matter_id=_matter_id(identity), actor=identity.actor
    )
    original = next(
        (
            row
            for row in snapshot.original_files
            if str(row.get("original_file_sha256")) == source_file.file_sha256
        ),
        None,
    )
    if original is None:
        raise ManagedDefenceAcceptanceBlocked("合成验收事实原件未进入证据账本。")
    page = next(
        (
            row
            for row in snapshot.pages
            if str(row.get("evidence_file_id")) == str(original.get("evidence_file_id"))
            and int(row.get("page_number", 0)) == page_number
        ),
        None,
    )
    if page is None:
        raise ManagedDefenceAcceptanceBlocked("合成验收事实页未进入证据账本。")
    return EvidenceLink(
        evidence_id=str(page["evidence_page_id"]),
        original_file_sha256=source_file.file_sha256,
        page_number=page_number,
        region_id=None,
        original_label=str(original["original_label"]),
    )


def _confirm_posture(
    composition: WebRuntimeComposition, identity: ServerIdentityContext
) -> None:
    receipt = composition.case_posture_service.confirm_complete_posture(
        identity=identity,
        matter_id=_matter_id(identity),
        expected_version=_MATERIAL_COMPLETE_VERSION,
        idempotency_key=_key("confirm-defendant-first-instance-posture"),
        party_kind="NATURAL_PERSON",
        display_label="王强（合成被告）",
        forum_type="PEOPLE_COURT",
        case_type_code="CIVIL.PRIVATE_LENDING",
        procedure_stage="FIRST_INSTANCE",
        position_code="DEFENDANT",
        authority_scope_code="GENERAL_AUTHORITY",
        engagement_state="ACTIVE",
    )
    if receipt.matter_version != _POSTURE_COMPLETE_VERSION:
        raise ManagedDefenceAcceptanceBlocked("被告一审代理情境未完整确认。")


def _confirmed_facts(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    generated: GeneratedGoldenCase,
) -> tuple[str, ...]:
    facts: tuple[tuple[str, AssertionOrigin, tuple[EvidenceLink, ...]], ...] = (
        (
            "起诉状候选主张第二笔借款为205,000元并称本金分文未还；该主张与银行流水所示第二笔200,000元借款存在待律师核对的差异。",
            AssertionOrigin.PLAINTIFF_PLEADING,
            (
                _evidence_link(composition, identity, generated, source_code="F1", page_number=2),
                _evidence_link(composition, identity, generated, source_code="F6", page_number=4),
            ),
        ),
        (
            "银行流水显示2019年6月3日周建国向王强转款300,000.00 CNY，摘要为借款。",
            AssertionOrigin.ASSISTANT_ENTRY,
            (_evidence_link(composition, identity, generated, source_code="F6", page_number=3),),
        ),
        (
            "银行流水显示2019年11月15日周建国向王强转款200,000.00 CNY，摘要为借款。",
            AssertionOrigin.ASSISTANT_ENTRY,
            (_evidence_link(composition, identity, generated, source_code="F6", page_number=4),),
        ),
        (
            "被告答辩候选提出已归还本金若干，并指出8,000.00 HKD不得直接并入人民币合计；该内容保留为待律师核对的抗辩线索，不是已确认法律结论。",
            AssertionOrigin.DEFENDANT_STATEMENT,
            (
                _evidence_link(composition, identity, generated, source_code="F1", page_number=5),
                _evidence_link(composition, identity, generated, source_code="F4", page_number=30),
            ),
        ),
        (
            "微信记录显示2022年9月10日王强向周建国转款50,000.00 CNY，摘要为周转款；付款性质需要结合证据由律师判断。",
            AssertionOrigin.DEFENDANT_STATEMENT,
            (_evidence_link(composition, identity, generated, source_code="F4", page_number=32),),
        ),
    )
    store: PostgresCaseLedgerStore = composition.case_ledger_store
    fact_ids: list[str] = []
    for index, (text, origin, links) in enumerate(facts, start=1):
        candidate_version = _POSTURE_COMPLETE_VERSION + (index - 1) * 2
        candidate = store.create_fact_candidate(
            matter_id=_matter_id(identity),
            actor=identity.actor,
            expected_version=candidate_version,
            idempotency_key=_key(f"fact-candidate:{index:02d}"),
            original_text=text,
            origin=origin,
            evidence_links=links,
        )
        decision = store.decide_fact(
            matter_id=_matter_id(identity),
            fact_id=candidate.object_id,
            actor=identity.actor,
            expected_version=candidate_version + 1,
            idempotency_key=_key(f"fact-confirm:{index:02d}"),
            status=FactStatus.CONFIRMED,
            decision_hash=_canonical_hash(
                {
                    "fixture": _ACCEPTANCE_NAME,
                    "kind": "synthetic-fact-confirmation",
                    "fact_id": candidate.object_id,
                    "evidence": [asdict(link) for link in links],
                }
            ),
        )
        if decision.object_id != candidate.object_id:
            raise ManagedDefenceAcceptanceBlocked("合成验收事实确认回执不一致。")
        fact_ids.append(candidate.object_id)
    return tuple(fact_ids)


_M6_CATALOGUE_EVIDENCE_SCOPE = frozenset(
    {
        ("F1", 2),
        ("F1", 5),
        ("F4", 30),
        ("F4", 32),
        ("F6", 3),
        ("F6", 4),
    }
)


def _confirm_m6_catalogue_evidence_scope(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    generated: GeneratedGoldenCase,
) -> None:
    """Confirm the exact pages that support the bounded synthetic facts.

    M6 deliberately exercises the user decision that M5 left open: an evidence
    catalogue cannot be manufactured from all uploaded pages, nor from model
    suggestions.  The fixture lead-lawyer identity confirms only the six
    original pages already bound to the five synthetic facts.  This is a
    synthetic acceptance action, not evidence assessment, lawyer final review,
    or a submission decision.
    """

    if _ACCEPTANCE_NAME not in {
        _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
        _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
        _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
    }:
        return
    service = composition.api_dependencies.evidence_review_service
    if service is None:
        raise ManagedDefenceAcceptanceBlocked("证据范围确认服务未就绪。")
    current = _current_matter(composition, identity)
    page_set = service.pages(
        identity=identity,
        matter_id=current.matter_id,
        expected_version=current.version,
        limit=100,
    )
    if page_set.has_more or page_set.total_count != 88 or len(page_set.items) != 88:
        raise ManagedDefenceAcceptanceBlocked("合成证据页集合不满足固定的 88 页范围。")
    source_by_label = {item.file_name: item.source_code for item in generated.files}
    selected: list[dict[str, object]] = []
    for item in page_set.items:
        source_code = source_by_label.get(str(item.get("original_label", "")))
        page_number = item.get("page_number")
        if (source_code, page_number) in _M6_CATALOGUE_EVIDENCE_SCOPE:
            selected.append(item)
    if len(selected) != len(_M6_CATALOGUE_EVIDENCE_SCOPE):
        raise ManagedDefenceAcceptanceBlocked("证据目录的合成页范围未能精确映射到事实来源。")
    decision_ids: list[str] = []
    expected_version = current.version
    for item in sorted(
        selected,
        key=lambda value: (str(value["original_label"]), int(value["page_number"])),
    ):
        if item.get("decision") is not None or item.get("pending_decision") is not None:
            raise ManagedDefenceAcceptanceBlocked("合成证据页已存在决定，禁止混合或覆盖范围。")
        page_id = str(item["evidence_page_id"])
        receipt = service.create_page_decision_candidate(
            identity=identity,
            matter_id=current.matter_id,
            evidence_page_id=page_id,
            expected_version=expected_version,
            disposition="INCLUDE",
            reason=(
                "全合成验收：该原始页已绑定至当前已确认案情，"
                "纳入目录仅供律师继续核对证明目的和证据三性。"
            ),
            idempotency_key=_key(f"m6-evidence-candidate:{page_id}"),
        )
        if receipt.matter_version != expected_version + 1:
            raise ManagedDefenceAcceptanceBlocked("证据范围候选版本回执不一致。")
        decision_ids.append(str(receipt.object_id))
        expected_version = receipt.matter_version
    confirmation = service.confirm_page_decisions_batch(
        identity=identity,
        matter_id=current.matter_id,
        decision_ids=tuple(decision_ids),
        expected_version=expected_version,
        idempotency_key=_key("m6-confirm-evidence-scope"),
    )
    if confirmation.matter_version != expected_version + 1:
        raise ManagedDefenceAcceptanceBlocked("证据范围确认版本回执不一致。")


def _confirm_claim_and_response(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    fact_ids: tuple[str, ...],
) -> str:
    store: PostgresCaseLedgerStore = composition.case_ledger_store
    candidate = store.create_claim_candidate_from_confirmed_facts(
        matter_id=_matter_id(identity),
        actor=identity.actor,
        expected_version=_FACTS_COMPLETE_VERSION,
        idempotency_key=_key("claim-candidate"),
        original_claim_text="原告主张被告返还第二笔借款205,000.00元并主张本金未还；具体范围以送达材料和已确认事实为限。",
        claimed_amount=Decimal("205000.00"),
        currency="CNY",
        confirmed_fact_ids=(fact_ids[0], fact_ids[2]),
    )
    confirmation_hash = _canonical_hash(
        {
            "fixture": _ACCEPTANCE_NAME,
            "kind": "synthetic-claim-scope-confirmation",
            "claim_id": candidate.object_id,
            "fact_ids": (fact_ids[0], fact_ids[2]),
        }
    )
    scope = store.confirm_claim_scope(
        matter_id=_matter_id(identity),
        claim_id=candidate.object_id,
        actor=identity.actor,
        expected_version=_FACTS_COMPLETE_VERSION + 1,
        idempotency_key=_key("claim-scope-confirm"),
        confirmation_hash=confirmation_hash,
    )
    response = store.set_claim_response(
        matter_id=_matter_id(identity),
        claim_id=candidate.object_id,
        actor=identity.actor,
        expected_version=_FACTS_COMPLETE_VERSION + 2,
        idempotency_key=_key("claim-response-dispute"),
        position=ClaimResponsePosition.DISPUTE,
        confirmed_fact_ids=(fact_ids[0], fact_ids[3], fact_ids[4]),
        partial_amount=None,
        currency=None,
        approval_hash=_canonical_hash(
            {
                "fixture": _ACCEPTANCE_NAME,
                "kind": "synthetic-defendant-response",
                "claim_id": candidate.object_id,
                "fact_ids": (fact_ids[0], fact_ids[3], fact_ids[4]),
            }
        ),
    )
    _assert_claim_response_receipts(candidate, scope, response)
    return candidate.object_id


def _assert_claim_response_receipts(
    candidate: object, scope: object, response: object
) -> None:
    """Verify the distinct immutable receipts in the claim-response chain.

    A claim scope confirmation returns the claim identifier, while recording a
    defendant response creates a separate ``CLAIM_RESPONSE`` record.  Treating
    the latter as if it reused the claim identifier turns a successful,
    committed response into a false acceptance failure.
    """

    candidate_id = getattr(candidate, "object_id", None)
    if (
        getattr(candidate, "object_type", None) != "CLAIM"
        or not isinstance(candidate_id, str)
        or getattr(scope, "object_type", None) != "CLAIM"
        or getattr(scope, "object_id", None) != candidate_id
        or getattr(response, "object_type", None) != "CLAIM_RESPONSE"
        or not isinstance(getattr(response, "object_id", None), str)
        or getattr(response, "object_id", None) == candidate_id
        or getattr(scope, "matter_id", None) != getattr(candidate, "matter_id", None)
        or getattr(response, "matter_id", None) != getattr(candidate, "matter_id", None)
    ):
        raise ManagedDefenceAcceptanceBlocked("合成验收诉请回应回执不一致。")


class _RejectOfficialSourceRedirects(urllib.request.HTTPRedirectHandler):
    """Keep the acceptance source binding identical to its registered URL."""

    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ManagedDefenceAcceptanceBlocked("官方法源请求发生未授权跳转。")


def _validate_frozen_official_source_content(content: bytes) -> None:
    source = _current_official_source_spec()
    if not isinstance(content, bytes) or len(content) > _MAX_OFFICIAL_SOURCE_BYTES:
        raise ManagedDefenceAcceptanceBlocked("官方法源超出受控字节上限。")
    if len(content) != source.frozen_bytes or (
        sha256(content).hexdigest() != source.frozen_content_sha256
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "官方法源与已审核的冻结来源快照不一致，验收未启动模型调用。"
        )
    try:
        source_text = extract_literal_official_source_text(
            body=content,
            content_media_type=source.content_media_type,
        )
    except (OfficialSourceObjectStoreBlocked, ValueError) as error:
        raise ManagedDefenceAcceptanceBlocked(
            "官方法源没有可安全读取的可见正文，验收未启动模型调用。"
        ) from error
    if any(marker not in source_text for marker in source.required_markers):
        raise ManagedDefenceAcceptanceBlocked("官方法源缺少已登记的民法典借款条款标记。")


def _download_official_source() -> tuple[bytes, datetime]:
    source = _current_official_source_spec()
    request = urllib.request.Request(
        source.official_url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "User-Agent": _OFFICIAL_SOURCE_USER_AGENT,
        },
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(_RejectOfficialSourceRedirects())
        with opener.open(request, timeout=25) as response:  # noqa: S310
            final_url = str(response.geturl())
            if final_url != source.official_url:
                raise ManagedDefenceAcceptanceBlocked("官方法源最终地址与已登记来源不一致。")
            if response.status != 200:
                raise ManagedDefenceAcceptanceBlocked("官方法源未返回成功响应。")
            media_type = str(response.headers.get_content_type()).lower()
            content = response.read(_MAX_OFFICIAL_SOURCE_BYTES + 1)
    except ManagedDefenceAcceptanceBlocked:
        raise
    except Exception as error:
        raise ManagedDefenceAcceptanceBlocked("官方法源当前无法受控下载，验收未启动模型调用。") from error
    if media_type != source.content_media_type:
        raise ManagedDefenceAcceptanceBlocked("官方法源响应媒体类型不符合已登记来源约束。")
    _validate_frozen_official_source_content(content)
    return content, _safe_now()


def _find_firm_frozen_official_source_reference(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
) -> _FirmFrozenOfficialSourceReference | None:
    """Read one exact, approved same-firm binding without enumerating objects.

    The returned matter identifier is not user material and is never exposed
    by this command. It only authorizes a subsequent exact-object read for the
    already-public, hash-pinned official source. A missing or malformed record
    is a hard stop rather than a reason to broaden the query or fetch from the
    public web during recovery.
    """

    source = _current_official_source_spec()
    sql = """
        SELECT segment.matter_id::text AS matter_id,
               source.retrieved_at AS retrieved_at
          FROM official_legal_source_snapshots source
          JOIN case_legal_bundle_segments segment
            ON segment.firm_id = source.firm_id
           AND segment.source_snapshot_id = source.snapshot_id
           AND segment.source_sha256 = source.content_sha256
          JOIN case_legal_bundles bundle
            ON bundle.firm_id = segment.firm_id
           AND bundle.matter_id = segment.matter_id
           AND bundle.bundle_id = segment.bundle_id
         WHERE source.firm_id = %s
           AND source.source_id = %s
           AND source.content_sha256 = %s
           AND source.publisher = %s
           AND source.authority_level = %s
           AND source.official_url = %s
           AND source.provision_locator = %s
           AND source.content_media_type = %s
           AND source.verification_status = 'VERIFIED'
           AND source.license_status = 'ACTIVE'
           AND bundle.status = 'APPROVED'
         ORDER BY bundle.approved_at DESC, segment.matter_id ASC
         LIMIT 1
    """
    try:
        with psycopg.connect(
            composition.settings.app_postgres_dsn, row_factory=dict_row
        ) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                connection.execute(
                    "SELECT set_config('app.firm_id', %s, true)",
                    (identity.actor.firm_id,),
                )
                connection.execute(
                    "SELECT set_config('app.actor_id', %s, true)",
                    (identity.actor.actor_id,),
                )
                row = connection.execute(
                    sql,
                    (
                        identity.actor.firm_id,
                        source.source_id,
                        source.frozen_content_sha256,
                        source.publisher,
                        LegalAuthorityLevel.PRIMARY_LAW.value,
                        source.official_url,
                        source.provision_locator,
                        source.content_media_type,
                    ),
                ).fetchone()
    except Exception as error:
        raise ManagedDefenceAcceptanceBlocked(
            "无法只读核验同律所已认证官方法源绑定；未继续模型运行。"
        ) from error
    if row is None:
        return None
    try:
        matter_id = str(row["matter_id"])
        UUID(matter_id)
        retrieved_at = row["retrieved_at"]
        if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at is invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise ManagedDefenceAcceptanceBlocked(
            "同律所已认证官方法源绑定回执无效；未继续模型运行。"
        ) from error
    return _FirmFrozenOfficialSourceReference(
        matter_id=matter_id,
        retrieved_at=retrieved_at,
    )


def _recover_frozen_official_source(
    objects: Any,
    *,
    firm_id: str,
    matter_id: str,
) -> tuple[bytes, datetime, Any] | None:
    """Reuse only the exact immutable source snapshot before contacting public web.

    The object was captured during the same fixed acceptance and is bound to
    this firm/matter/hash.  Its object-store timestamp records the private
    capture moment; it never impersonates a fresh public-web fetch.
    """

    source = _current_official_source_spec()
    stored = objects.find_existing_official_source(
        firm_id=firm_id,
        matter_id=matter_id,
        content_sha256=source.frozen_content_sha256,
        content_media_type=source.content_media_type,
        byte_size=source.frozen_bytes,
    )
    if stored is None:
        return None
    if stored.stored_at is None:
        raise ManagedDefenceAcceptanceBlocked(
            "已认证官方法源缺少不可变捕获时间，验收未启动模型调用。"
        )
    content, media_type = objects.read_official_source(
        firm_id=firm_id,
        matter_id=matter_id,
        ledger_object_key=stored.ledger_object_key,
        expected_sha256=source.frozen_content_sha256,
        expected_media_type=source.content_media_type,
    )
    if (
        media_type != source.content_media_type
        or stored.content_sha256 != source.frozen_content_sha256
        or stored.byte_size != source.frozen_bytes
    ):
        raise ManagedDefenceAcceptanceBlocked("已认证官方法源对象回执不一致。")
    _validate_frozen_official_source_content(content)
    return content, stored.stored_at, stored


def _recover_firm_frozen_official_source(
    objects: Any,
    *,
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
) -> tuple[bytes, datetime, Any] | None:
    """Copy one read-authenticated public source into a fixed recovery matter.

    This path is intentionally narrower than ordinary source reuse: it can
    run only for ADR-0069's v4, ADR-0076's M2, or ADR-0079's M3 source
    checkpoint, reads a source object whose exact governance fields and
    firm-bound legal bundle were verified in a read-only database transaction,
    and writes a new matter-bound copy with the normal no-overwrite
    object-store contract. It never forwards a client document, enumerates
    object storage, invokes a model, or contacts public web infrastructure.
    """

    if _ACCEPTANCE_NAME not in {
        _SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME,
        _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME,
        _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME,
        _M4_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
        _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
    }:
        raise ManagedDefenceAcceptanceBlocked(
            "同律所法源恢复仅允许 v4、M2、M3、M4、M5、M6、M7 或 M8 受控验收使用。"
        )
    source = _current_official_source_spec()
    reference = _find_firm_frozen_official_source_reference(composition, identity)
    if reference is None:
        return None
    if _ACCEPTANCE_NAME == _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME:
        _validate_m2_firm_frozen_source_recency(reference.retrieved_at)
    elif _ACCEPTANCE_NAME in {
        _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME,
        _M4_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
    }:
        _validate_m3_firm_frozen_source_recency(reference.retrieved_at)
    current_matter_id = _matter_id(identity)
    if reference.matter_id == current_matter_id:
        raise ManagedDefenceAcceptanceBlocked(
            "同律所法源恢复没有形成独立已审批来源绑定；未继续模型运行。"
        )
    content, media_type = objects.read_official_source(
        firm_id=identity.actor.firm_id,
        matter_id=reference.matter_id,
        ledger_object_key=(
            f"{source.frozen_content_sha256[:2]}/"
            f"{source.frozen_content_sha256[2:4]}/"
            f"{source.frozen_content_sha256}.lca"
        ),
        expected_sha256=source.frozen_content_sha256,
        expected_media_type=source.content_media_type,
    )
    if media_type != source.content_media_type:
        raise ManagedDefenceAcceptanceBlocked("同律所官方法源对象媒体类型不一致。")
    _validate_frozen_official_source_content(content)
    stored = _store_or_recover_official_source(
        objects,
        content=content,
        firm_id=identity.actor.firm_id,
        matter_id=current_matter_id,
        content_sha256=source.frozen_content_sha256,
        content_media_type=source.content_media_type,
    )
    if (
        stored.content_sha256 != source.frozen_content_sha256
        or stored.media_type != source.content_media_type
        or stored.byte_size != source.frozen_bytes
    ):
        raise ManagedDefenceAcceptanceBlocked("同律所官方法源复制回执不一致。")
    return content, reference.retrieved_at, stored


def _store_or_recover_official_source(
    objects: Any,
    *,
    content: bytes,
    firm_id: str,
    matter_id: str,
    content_sha256: str,
    content_media_type: str,
) -> Any:
    """Persist exact source bytes once, then reconcile a known-ambiguous write.

    ``IfNoneMatch`` keeps the object immutable.  Consequently a prior timeout
    or an already-committed compatibility failure must never trigger a second
    upload attempt: only the deterministic, matter-bound object is read back.
    """

    try:
        return objects.put_official_source(
            content,
            firm_id=firm_id,
            matter_id=matter_id,
            content_sha256=content_sha256,
            content_media_type=content_media_type,
        )
    except OfficialSourceObjectStateUnknown:
        try:
            return objects.recover_official_source(
                firm_id=firm_id,
                matter_id=matter_id,
                content_sha256=content_sha256,
                content_media_type=content_media_type,
                byte_size=len(content),
            )
        except OfficialSourceObjectStoreBlocked as error:
            raise ManagedDefenceAcceptanceBlocked(
                "官方法源私有对象写入结果未知，且只读恢复未通过；未继续模型运行。"
            ) from error
    except OfficialSourceObjectStoreBlocked as error:
        raise ManagedDefenceAcceptanceBlocked(
            "官方法源私有存储校验未通过；未继续模型运行。"
        ) from error


def _run_legal_stage(stage: str, action: Callable[[], Any]) -> Any:
    """Keep every pre-Agent legal step explainable without exposing source data."""

    try:
        return action()
    except ManagedDefenceAcceptanceBlocked:
        raise
    except Exception as error:
        raise ManagedDefenceAcceptanceBlocked(
            f"官方法源治理在“{stage}”失败（{type(error).__name__}）；未继续模型运行。"
        ) from error


def _confirm_legal_context(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    generated: GeneratedGoldenCase,
    *,
    firm_frozen_source_recovery_only: bool = False,
    m1_frozen_source_recovery_only: bool = False,
    m2_firm_frozen_source_recovery_only: bool = False,
    m3_firm_frozen_source_preflight: bool = False,
) -> None:
    source_spec = _current_official_source_spec()
    if sum(
        (
            firm_frozen_source_recovery_only,
            m1_frozen_source_recovery_only,
            m2_firm_frozen_source_recovery_only,
            m3_firm_frozen_source_preflight,
        )
    ) > 1:
        raise ManagedDefenceAcceptanceBlocked("两种固定法源恢复模式不能同时使用。")
    if (
        firm_frozen_source_recovery_only
        and _ACCEPTANCE_NAME != _SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
    ):
        raise ManagedDefenceAcceptanceBlocked("固定法源恢复仅允许 v4 受控验收使用。")
    if (
        m1_frozen_source_recovery_only
        and _ACCEPTANCE_NAME != _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
    ):
        raise ManagedDefenceAcceptanceBlocked("M1 固定法源恢复仅允许 M1 受控验收使用。")
    if (
        m2_firm_frozen_source_recovery_only
        and _ACCEPTANCE_NAME != _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
    ):
        raise ManagedDefenceAcceptanceBlocked("M2 固定法源恢复仅允许 M2 受控验收使用。")
    if (
        m3_firm_frozen_source_preflight
        and not _uses_current_runtime_source_preflight()
    ):
        raise ManagedDefenceAcceptanceBlocked("M3/M4 固定法源预检仅允许当前受控验收使用。")
    objects = composition.official_source_adapters.objects
    recovered = _run_legal_stage(
        "恢复已认证冻结法源快照",
        lambda: _recover_frozen_official_source(
            objects,
            firm_id=identity.actor.firm_id,
            matter_id=_matter_id(identity),
        ),
    )
    if recovered is None and (
        firm_frozen_source_recovery_only
        or m2_firm_frozen_source_recovery_only
        or (
            m3_firm_frozen_source_preflight
            and _ACCEPTANCE_NAME
            not in {
                _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
                _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
                _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
                _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
                _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
                _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
            }
        )
    ):
        recovered = _run_legal_stage(
            "只读核验并复制同律所冻结法源",
            lambda: _recover_firm_frozen_official_source(
                objects,
                composition=composition,
                identity=identity,
            ),
        )
    if recovered is None:
        if firm_frozen_source_recovery_only:
            raise ManagedDefenceAcceptanceBlocked(
                "v4 预模型恢复找不到可审计的同律所冻结法源；未重新访问公网或模型。"
            )
        if m1_frozen_source_recovery_only:
            raise ManagedDefenceAcceptanceBlocked(
                "M1 预模型恢复找不到同案已认证冻结法源；未重新访问公网或模型。"
            )
        if m2_firm_frozen_source_recovery_only:
            raise ManagedDefenceAcceptanceBlocked(
                "M2 预模型恢复找不到 24 小时内可审计的同律所冻结法源；"
                "未重新访问公网或模型。"
            )
        if (
            m3_firm_frozen_source_preflight
            and _ACCEPTANCE_NAME
            not in {
                _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
                _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
                _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
                _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
                _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
                _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
            }
        ):
            raise ManagedDefenceAcceptanceBlocked(
                "M3 来源预检找不到 24 小时内可审计的同律所冻结法源；"
                "未重新访问公网或模型。"
            )
        content, retrieved_at = _run_legal_stage(
            "获取已登记官方原文", _download_official_source
        )
        content_hash = sha256(content).hexdigest()
        stored = _run_legal_stage(
            "认证私有法源对象",
            lambda: _store_or_recover_official_source(
                objects,
                content=content,
                firm_id=identity.actor.firm_id,
                matter_id=_matter_id(identity),
                content_sha256=content_hash,
                content_media_type=source_spec.content_media_type,
            ),
        )
    else:
        content, retrieved_at, stored = recovered
        content_hash = source_spec.frozen_content_sha256
    if (
        stored.content_sha256 != content_hash
        or stored.media_type != source_spec.content_media_type
    ):
        raise ManagedDefenceAcceptanceBlocked("官方法源私有存储回执不一致。")
    source = _run_legal_stage(
        "登记已认证官方来源快照",
        lambda: composition.legal_store.register_official_source_snapshot(
            matter_id=_matter_id(identity),
            actor=identity.actor,
            expected_version=_GOVERNED_INPUT_COMPLETE_VERSION,
            idempotency_key=_key("official-law-source"),
            source_id=source_spec.source_id,
            publisher=source_spec.publisher,
            authority_level=LegalAuthorityLevel.PRIMARY_LAW,
            official_url=source_spec.official_url,
            provision_locator=source_spec.provision_locator,
            retrieved_at=retrieved_at,
            content_sha256=content_hash,
            content_media_type=source_spec.content_media_type,
            storage_object_key=stored.ledger_object_key,
            verification_hash=_canonical_hash(
                {
                    "fixture": _ACCEPTANCE_NAME,
                    "kind": "official-source-bytes-verified",
                    "content_sha256": content_hash,
                    "url": source_spec.official_url,
                    "host": source_spec.host,
                    "required_markers": source_spec.required_markers,
                }
            ),
            license_basis=source_spec.license_basis,
            license_review_hash=_canonical_hash(
                {
                    "fixture": _ACCEPTANCE_NAME,
                    "kind": "official-source-internal-use-review",
                    "content_sha256": content_hash,
                }
            ),
        ),
    )
    loan_page = _run_legal_stage(
        "绑定借款证据页",
        lambda: _evidence_link(
            composition, identity, generated, source_code="F6", page_number=3
        ),
    )
    event = _run_legal_stage(
        "登记经证据锚定的法律事件",
        lambda: composition.legal_store.approve_legal_event(
            matter_id=_matter_id(identity),
            actor=identity.actor,
            expected_version=_GOVERNED_INPUT_COMPLETE_VERSION + 1,
            idempotency_key=_key("legal-event-loan-anchor"),
            event_kind=LegalEventKind.CONTRACT_SIGNED,
            local_date=date(2019, 6, 3),
            evidence_ids=(loan_page.evidence_id,),
            approval_hash=_canonical_hash(
                {
                    "fixture": _ACCEPTANCE_NAME,
                    "kind": "synthetic-legal-event-anchor",
                    "evidence_page_id": loan_page.evidence_id,
                }
            ),
        ),
    )
    rule = _run_legal_stage(
        "批准受限法源规则版本",
        lambda: composition.legal_store.approve_rule_version(
            matter_id=_matter_id(identity),
            actor=identity.actor,
            expected_version=_GOVERNED_INPUT_COMPLETE_VERSION + 2,
            idempotency_key=_key("legal-rule-response-source-scope"),
            rule_id=_response_source_scope_rule_identity()[0],
            rule_version=_response_source_scope_rule_identity()[1],
            issue_key=_response_source_scope_rule_identity()[0],
            source_snapshot_id=source.object_id,
            parameter_source_snapshot_id=None,
            parameter_evidence_locator=None,
            effective_from=date(2019, 1, 1),
            effective_to=None,
            trigger_event_kind=LegalEventKind.CONTRACT_SIGNED,
            formula_kind=LegalRateFormulaKind.NO_INTEREST,
            base_annual_rate=None,
            rate_multiplier=None,
            required_fact_keys=(),
            transition_rule_versions=(),
            conflict_set=None,
            priority=1,
            approval_hash=_canonical_hash(
                {
                    "fixture": _ACCEPTANCE_NAME,
                    "kind": "synthetic-legal-rule-source-scope",
                    "source_snapshot_id": source.object_id,
                }
            ),
        ),
    )
    bundle = _run_legal_stage(
        "批准案件法源包",
        lambda: composition.legal_store.approve_case_legal_bundle(
            matter_id=_matter_id(identity),
            actor=identity.actor,
            expected_version=_GOVERNED_INPUT_COMPLETE_VERSION + 3,
            idempotency_key=_key("legal-bundle-response-source-scope"),
            segments=(
                LegalBundleSegmentSelection(
                    segment_id=_acceptance_id("legal-bundle-segment"),
                    issue_key="PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE",
                    rule_version_id=rule.object_id,
                    trigger_event_id=event.object_id,
                    start_date=date(2019, 6, 3),
                    end_date=_FIXED_BUNDLE_END,
                    applicability_anchor="仅建立一审被告应诉来源审阅边界；不生成或确认金额、利率或时效结论。",
                ),
            ),
            approval_hash=_canonical_hash(
                {
                    "fixture": _ACCEPTANCE_NAME,
                    "kind": "synthetic-legal-bundle-source-scope",
                    "rule_version_id": rule.object_id,
                    "legal_event_id": event.object_id,
                }
            ),
        ),
    )
    if not (source.object_id and event.object_id and rule.object_id and bundle.object_id):
        raise ManagedDefenceAcceptanceBlocked("合成验收法源治理回执不完整。")


_PRE_MODEL_RESUME_COUNTERS = {
    "case_posture_profiles": "profile_id",
    "case_facts": "fact_id",
    "case_claims": "claim_id",
    "case_claim_responses": "claim_response_id",
    "case_legal_events": "legal_event_id",
    "case_legal_bundles": "bundle_id",
    "case_work_plans": "plan_id",
    "case_agent_goals": "goal_id",
    "case_agent_runs": "run_id",
}
_PRE_MODEL_RESUME_TABLES = tuple(_PRE_MODEL_RESUME_COUNTERS)
# The Web application role must not read Worker-owned task, planning-event or
# external-submission tables.  Every such child record has a foreign key to a
# case_agent_runs row, so proving the run count is zero proves no such child
# can exist without widening the Web database grant.


def _read_pre_model_resume_state(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
) -> tuple[tuple[_PersistedIntakeRecord, ...], Mapping[str, int]]:
    """Read the fixed-fixture intake boundary without exposing private objects.

    This is not a general browser recovery endpoint.  It is a local,
    loopback-only operational guard that is usable only after the normal
    acceptance command has stopped *before* its first Agent run.  The query
    holds no document bytes, object locator, session secret or provider data.
    """

    matter_id = _matter_id(identity)
    dsn = composition.settings.app_postgres_dsn
    count_sql = "SELECT " + ",\n".join(
        f"(SELECT count({column}) FROM {table} WHERE firm_id = %s AND matter_id = %s) AS {table}"
        for table, column in _PRE_MODEL_RESUME_COUNTERS.items()
    )
    count_parameters: list[str] = []
    for _ in _PRE_MODEL_RESUME_COUNTERS:
        count_parameters.extend((identity.actor.firm_id, matter_id))
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.firm_id', %s, true)",
                    (identity.actor.firm_id,),
                )
                connection.execute(
                    "SELECT set_config('app.actor_id', %s, true)",
                    (identity.actor.actor_id,),
                )
                rows = connection.execute(
                    """
                    SELECT display_name AS file_name,
                           admitted_content_sha256 AS content_sha256,
                           admitted_page_count AS page_count,
                           'EVIDENCE_ORIGINAL'::text AS route,
                           evidence_matter_version AS completion_matter_version,
                           status
                      FROM web_material_upload_slots
                     WHERE firm_id = %s AND matter_id = %s
                    UNION ALL
                    SELECT display_name AS file_name,
                           admitted_content_sha256 AS content_sha256,
                           1 AS page_count,
                           route,
                           result_matter_version AS completion_matter_version,
                           status
                      FROM web_common_material_uploads
                     WHERE firm_id = %s AND matter_id = %s
                     ORDER BY completion_matter_version, file_name
                    """,
                    (
                        identity.actor.firm_id,
                        matter_id,
                        identity.actor.firm_id,
                        matter_id,
                    ),
                ).fetchall()
                counts_row = connection.execute(count_sql, count_parameters).fetchone()
    except Exception as error:
        raise ManagedDefenceAcceptanceBlocked("受管验收无法安全读取材料恢复边界。") from error
    if counts_row is None:
        raise ManagedDefenceAcceptanceBlocked("受管验收材料恢复状态为空。")
    records: list[_PersistedIntakeRecord] = []
    for row in rows:
        if str(row["status"]) != "COMPLETED":
            raise ManagedDefenceAcceptanceBlocked("固定验收案件存在未终结材料接收，禁止续跑。")
        try:
            records.append(
                _PersistedIntakeRecord(
                    file_name=str(row["file_name"]),
                    content_sha256=str(row["content_sha256"]),
                    page_count=int(row["page_count"]),
                    route=str(row["route"]),
                    completion_matter_version=int(row["completion_matter_version"]),
                )
            )
        except (TypeError, ValueError) as error:
            raise ManagedDefenceAcceptanceBlocked("固定验收材料恢复记录无效。") from error
    counts = {name: int(counts_row[name]) for name in _PRE_MODEL_RESUME_TABLES}
    return tuple(records), counts


def _validate_pre_model_resume_prefix(
    generated: GeneratedGoldenCase,
    *,
    current_matter_version: int,
    records: tuple[_PersistedIntakeRecord, ...],
    downstream_counts: Mapping[str, int],
) -> tuple[AcceptedInput, ...]:
    """Accept only an exact, strictly pre-model immutable intake prefix."""

    unexpected = sorted(
        name for name, value in downstream_counts.items() if type(value) is not int or value != 0
    )
    if unexpected:
        raise ManagedDefenceAcceptanceBlocked(
            "固定验收已离开纯材料接收阶段，禁止以恢复模式创建或继续 Agent 运行。"
        )
    if not records or len(records) > len(generated.ingest_order):
        raise ManagedDefenceAcceptanceBlocked("固定验收不存在可安全续接的材料检查点。")
    ordered = tuple(sorted(records, key=lambda item: item.completion_matter_version))
    expected_versions = tuple(range(2, len(ordered) + 2))
    if tuple(item.completion_matter_version for item in ordered) != expected_versions:
        raise ManagedDefenceAcceptanceBlocked("固定验收材料版本序列不连续，禁止续跑。")
    accepted = tuple(
        AcceptedInput(
            file_name=item.file_name,
            content_sha256=item.content_sha256,
            page_count=item.page_count,
            route=item.route,
        )
        for item in ordered
    )
    _assert_accepted_prefix(generated, accepted)
    if current_matter_version != len(accepted) + 1:
        raise ManagedDefenceAcceptanceBlocked("固定验收案件版本与已接收原件前缀不一致。")
    return accepted


def _validate_completed_material_checkpoint(
    generated: GeneratedGoldenCase,
    *,
    records: tuple[_PersistedIntakeRecord, ...],
) -> tuple[AcceptedInput, ...]:
    """Require the complete immutable 11-file checkpoint before input replay."""

    if len(records) != len(generated.ingest_order):
        raise ManagedDefenceAcceptanceBlocked(
            "受控输入恢复只能从完整冻结材料检查点开始。"
        )
    ordered = tuple(sorted(records, key=lambda item: item.completion_matter_version))
    expected_versions = tuple(range(2, len(ordered) + 2))
    if tuple(item.completion_matter_version for item in ordered) != expected_versions:
        raise ManagedDefenceAcceptanceBlocked("受控输入恢复的材料版本序列不连续。")
    accepted = tuple(
        AcceptedInput(
            file_name=item.file_name,
            content_sha256=item.content_sha256,
            page_count=item.page_count,
            route=item.route,
        )
        for item in ordered
    )
    _assert_accepted_prefix(generated, accepted)
    return accepted


def _governed_input_command_sequence() -> tuple[tuple[str, str], ...]:
    """Return the only server-command prefix allowed before the first Agent run."""

    posture_key = _key("confirm-defendant-first-instance-posture")
    commands: list[tuple[str, str]] = [
        ("CONFIRM_CASE_PARTY_VERSION", _web_posture_stage_key(posture_key, "party")),
        (
            "CONFIRM_COURT_PROCEEDING_VERSION",
            _web_posture_stage_key(posture_key, "proceeding"),
        ),
        (
            "CONFIRM_COURT_PARTY_POSITION_VERSION",
            _web_posture_stage_key(posture_key, "position"),
        ),
        (
            "CONFIRM_FIRM_ENGAGEMENT_VERSION",
            _web_posture_stage_key(posture_key, "engagement"),
        ),
        (
            "CONFIRM_CURRENT_CASE_POSTURE_PROFILE",
            _web_posture_stage_key(posture_key, "profile"),
        ),
    ]
    for index in range(1, 6):
        commands.extend(
            (
                ("CREATE_FACT_CANDIDATE", _key(f"fact-candidate:{index:02d}")),
                ("DECIDE_FACT", _key(f"fact-confirm:{index:02d}")),
            )
        )
    commands.extend(
        (
            ("CREATE_CLAIM_CANDIDATE_FROM_CONFIRMED_FACTS", _key("claim-candidate")),
            ("CONFIRM_CLAIM_SCOPE", _key("claim-scope-confirm")),
            ("SET_CLAIM_RESPONSE", _key("claim-response-dispute")),
        )
    )
    return tuple(commands)


def _read_governed_input_command_prefix(
    composition: WebRuntimeComposition, identity: ServerIdentityContext
) -> tuple[tuple[str, str, int], ...]:
    """Read only the fixed pre-Agent command receipts, never source content."""

    sequence = _governed_input_command_sequence()
    names = tuple(sorted({name for name, _ in sequence}))
    placeholders = ", ".join("%s" for _ in names)
    sql = f"""
        SELECT command_name,
               idempotency_key,
               (response_json->>'matter_version')::integer AS matter_version
          FROM command_idempotency
         WHERE firm_id = %s
           AND matter_id = %s
           AND command_name IN ({placeholders})
         ORDER BY (response_json->>'matter_version')::integer, command_id
    """
    try:
        with psycopg.connect(composition.settings.app_postgres_dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.firm_id', %s, true)",
                    (identity.actor.firm_id,),
                )
                connection.execute(
                    "SELECT set_config('app.actor_id', %s, true)",
                    (identity.actor.actor_id,),
                )
                rows = connection.execute(
                    sql,
                    (identity.actor.firm_id, _matter_id(identity), *names),
                ).fetchall()
    except Exception as error:
        raise ManagedDefenceAcceptanceBlocked("受控输入恢复无法读取命令回执边界。") from error
    try:
        return tuple(
            (str(row["command_name"]), str(row["idempotency_key"]), int(row["matter_version"]))
            for row in rows
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ManagedDefenceAcceptanceBlocked("受控输入恢复命令回执无效。") from error


def _m1_post_event_rule_command_sequence() -> tuple[tuple[str, str, int], ...]:
    """Return the only M1 state that may resume between event and rule approval."""

    return (
        (
            "REGISTER_OFFICIAL_LEGAL_SOURCE_SNAPSHOT",
            _key("official-law-source"),
            _GOVERNED_INPUT_COMPLETE_VERSION + 1,
        ),
        (
            "APPROVE_CASE_LEGAL_EVENT",
            _key("legal-event-loan-anchor"),
            _GOVERNED_INPUT_COMPLETE_VERSION + 2,
        ),
    )


def _read_m1_post_event_rule_command_prefix(
    composition: WebRuntimeComposition, identity: ServerIdentityContext
) -> tuple[tuple[str, str, int], ...]:
    """Read only the two fixed M1 legal receipts needed for exact recovery."""

    sequence = _m1_post_event_rule_command_sequence()
    names = tuple(sorted({name for name, _, _ in sequence}))
    placeholders = ", ".join("%s" for _ in names)
    sql = f"""
        SELECT command_name,
               idempotency_key,
               (response_json->>'matter_version')::integer AS matter_version
          FROM command_idempotency
         WHERE firm_id = %s
           AND matter_id = %s
           AND command_name IN ({placeholders})
         ORDER BY (response_json->>'matter_version')::integer, command_id
    """
    try:
        with psycopg.connect(composition.settings.app_postgres_dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.firm_id', %s, true)",
                    (identity.actor.firm_id,),
                )
                connection.execute(
                    "SELECT set_config('app.actor_id', %s, true)",
                    (identity.actor.actor_id,),
                )
                rows = connection.execute(
                    sql,
                    (identity.actor.firm_id, _matter_id(identity), *names),
                ).fetchall()
    except Exception as error:
        raise ManagedDefenceAcceptanceBlocked("M1 法源恢复无法读取法律命令回执边界。") from error
    try:
        return tuple(
            (str(row["command_name"]), str(row["idempotency_key"]), int(row["matter_version"]))
            for row in rows
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ManagedDefenceAcceptanceBlocked("M1 法源恢复命令回执无效。") from error


def _validate_governed_input_resume_prefix(
    *,
    current_matter_version: int,
    command_receipts: tuple[tuple[str, str, int], ...],
    counts: Mapping[str, int],
) -> int:
    """Permit only an exact no-Agent prefix of deterministic governed inputs."""

    blockers = (
        "case_legal_events",
        "case_legal_bundles",
        "case_work_plans",
        "case_agent_goals",
        "case_agent_runs",
    )
    unexpected = sorted(
        name for name in blockers if type(counts.get(name)) is not int or counts[name] != 0
    )
    if unexpected:
        raise ManagedDefenceAcceptanceBlocked(
            "固定验收已离开可恢复的受控输入阶段，禁止继续或创建 Agent 运行。"
        )
    expected = _governed_input_command_sequence()
    if len(command_receipts) > len(expected):
        raise ManagedDefenceAcceptanceBlocked("受控输入命令数超过冻结验收合同。")
    expected_receipts = tuple(
        (name, key, _MATERIAL_COMPLETE_VERSION + index)
        for index, (name, key) in enumerate(expected[: len(command_receipts)], start=1)
    )
    if command_receipts != expected_receipts:
        raise ManagedDefenceAcceptanceBlocked("受控输入命令前缀与冻结验收合同不一致。")
    input_count = len(command_receipts)
    expected_counts = {
        "case_posture_profiles": 1 if input_count >= 5 else 0,
        "case_facts": min(5, max(0, (input_count - 4) // 2)),
        "case_claims": 1 if input_count >= 16 else 0,
        "case_claim_responses": 1 if input_count >= 18 else 0,
    }
    for name, expected_count in expected_counts.items():
        if type(counts.get(name)) is not int or counts[name] != expected_count:
            raise ManagedDefenceAcceptanceBlocked("受控输入投影与命令前缀不一致。")
    if current_matter_version != _MATERIAL_COMPLETE_VERSION + input_count:
        raise ManagedDefenceAcceptanceBlocked("受控输入案件版本与命令前缀不一致。")
    return input_count


def _validate_m1_post_event_rule_resume_prefix(
    *,
    current_matter_version: int,
    command_receipts: tuple[tuple[str, str, int], ...],
    legal_command_receipts: tuple[tuple[str, str, int], ...],
    counts: Mapping[str, int],
) -> None:
    """Permit one exact M1 checkpoint after source/event and before rule approval.

    This is a failure-recovery boundary, not a general legal-workflow resume:
    it requires the exact M1 governed input receipts, source receipt and
    evidence-anchored event receipt, with no rule bundle, plan, goal or Agent
    run.  Missing or changed state stops before any external model submission.
    """

    if _ACCEPTANCE_NAME != _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME:
        raise ManagedDefenceAcceptanceBlocked("M1 规则恢复仅允许 M1 受控验收使用。")
    expected_governed = tuple(
        (name, key, _MATERIAL_COMPLETE_VERSION + index)
        for index, (name, key) in enumerate(_governed_input_command_sequence(), start=1)
    )
    if command_receipts != expected_governed:
        raise ManagedDefenceAcceptanceBlocked("M1 规则恢复的受控输入命令前缀不一致。")
    expected_counts = {
        "case_posture_profiles": 1,
        "case_facts": 5,
        "case_claims": 1,
        "case_claim_responses": 1,
        "case_legal_events": 1,
        "case_legal_bundles": 0,
        "case_work_plans": 0,
        "case_agent_goals": 0,
        "case_agent_runs": 0,
    }
    if any(
        type(counts.get(name)) is not int or counts[name] != expected
        for name, expected in expected_counts.items()
    ):
        raise ManagedDefenceAcceptanceBlocked("M1 规则恢复的案件投影不等于固定检查点。")
    if legal_command_receipts != _m1_post_event_rule_command_sequence():
        raise ManagedDefenceAcceptanceBlocked("M1 规则恢复的法源命令前缀不一致。")
    if current_matter_version != _GOVERNED_INPUT_COMPLETE_VERSION + 2:
        raise ManagedDefenceAcceptanceBlocked("M1 规则恢复的案件版本不等于固定检查点。")


def _validate_created_defence_run_resume_state(
    *,
    current_matter_version: int,
    command_receipts: tuple[tuple[str, str, int], ...],
    counts: Mapping[str, int],
    run: object,
    expected_run_id: str,
) -> str:
    """Classify one exact, still-pre-model controlled-defence run.

    The ordinary recovery point is ``CREATED`` at event version 1.  A Worker
    may, however, safely compile the server-owned graph and execute its one
    deterministic case-context task before this operational command attaches.
    The only later resumable point is therefore the exact event-version-5
    lawyer-analysis approval gate.  It is validated again against the durable
    task graph before the command can approve the sole model task.

    Neither state permits a prior provider submission. Runner incident audit
    rows intentionally remain outside this check: they preserve the original
    local failure without advancing the Agent state machine.
    """

    expected_counts = {
        "case_posture_profiles": 1,
        "case_facts": 5,
        "case_claims": 1,
        "case_claim_responses": 1,
        "case_legal_events": 1,
        "case_legal_bundles": 1,
        "case_work_plans": 0,
        "case_agent_goals": 1,
        "case_agent_runs": 1,
    }
    if current_matter_version != _LEGAL_CONTEXT_COMPLETE_VERSION:
        raise ManagedDefenceAcceptanceBlocked(
            "已创建运行恢复的案件版本不等于固定法源输入检查点。"
        )
    if any(
        type(counts.get(name)) is not int or counts[name] != expected
        for name, expected in expected_counts.items()
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "已创建运行恢复的案件投影与固定验收合同不一致。"
        )
    expected_receipts = tuple(
        (name, key, _MATERIAL_COMPLETE_VERSION + index)
        for index, (name, key) in enumerate(
            _governed_input_command_sequence(), start=1
        )
    )
    if command_receipts != expected_receipts:
        raise ManagedDefenceAcceptanceBlocked(
            "已创建运行恢复的受控输入命令前缀不一致。"
        )
    required_values = {
        "run_id": expected_run_id,
        "objective": _FIRST_RUN_OBJECTIVE,
        "snapshot_matter_version": _LEGAL_CONTEXT_COMPLETE_VERSION,
        "open_decision_count": 0,
        "input_snapshot_status": "CURRENT",
    }
    if any(getattr(run, name, None) != value for name, value in required_values.items()):
        raise ManagedDefenceAcceptanceBlocked(
            "已创建运行不再处于模型调用前的固定受控状态。"
        )
    if (
        getattr(run, "failure_code", None) is not None
        or getattr(run, "failure_message", None) is not None
        or getattr(run, "active_plan_execution", None) is not False
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "已创建运行包含不允许续接的任务、失败或计划状态。"
        )
    if getattr(run, "status", None) == "CREATED":
        created_values = {
            "version": 1,
            "progress_completed": 0,
            "progress_total": 0,
            "open_approval_count": 0,
            "artifact_count": 0,
        }
        if any(
            getattr(run, name, None) != value
            for name, value in created_values.items()
        ) or getattr(run, "current_work", None) is not None:
            raise ManagedDefenceAcceptanceBlocked(
                "已创建运行不再处于模型调用前的固定受控状态。"
            )
        return "CREATED"
    if getattr(run, "status", None) == "WAITING_APPROVAL":
        waiting_values = {
            "version": 5,
            "progress_completed": 1,
            "progress_total": 2,
            "open_approval_count": 1,
            "artifact_count": 1,
        }
        current_work = getattr(run, "current_work", None)
        if any(
            getattr(run, name, None) != value
            for name, value in waiting_values.items()
        ) or getattr(current_work, "status", None) != "WAITING_APPROVAL":
            raise ManagedDefenceAcceptanceBlocked(
                "已创建运行不再处于模型调用前的固定受控状态。"
            )
        return "WAITING_APPROVAL"
    raise ManagedDefenceAcceptanceBlocked(
        "已创建运行不再处于模型调用前的固定受控状态。"
    )


def _load_created_defence_run_resume(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    control: object,
) -> tuple[tuple[AcceptedInput, ...], object]:
    """Verify and return the one pre-model run without creating another one."""

    with TemporaryDirectory(prefix="lawcase-managed-defence-created-run-") as temporary:
        generated = generate_golden_case(
            Path(temporary), load_authoritative_case(PROJECT_ROOT)
        )
        if len(generated.files) != 11 or len(read_generated_pages(generated)) != 88:
            raise ManagedDefenceAcceptanceBlocked("冻结合成案卷不满足 88 页、11 文件合同。")
        try:
            current = _current_matter(composition, identity)
        except KeyError as error:
            raise ManagedDefenceAcceptanceBlocked("固定合成验收案件不存在，禁止恢复运行。") from error
        records, counts = _read_pre_model_resume_state(composition, identity)
        accepted = _validate_completed_material_checkpoint(generated, records=records)
        # A complete prefix makes _admit_sources read-only: no new slot,
        # upload or source mutation is reachable after the exact prefix check.
        accepted = asyncio.run(
            _admit_sources(
                composition,
                identity,
                generated,
                accepted_prefix=accepted,
            )
        )
        expected_run_id = derive_web_case_agent_entity_id(
            actor=identity.actor,
            matter_id=current.matter_id,
            idempotency_key=_key("first-run"),
            entity="run",
        )
        try:
            run = control.get_run(
                identity=identity,
                matter_id=current.matter_id,
                run_id=expected_run_id,
            )
        except KeyError as error:
            raise ManagedDefenceAcceptanceBlocked(
                "固定的首轮受管运行不存在，禁止创建替代运行。"
            ) from error
        resume_state = _validate_created_defence_run_resume_state(
            current_matter_version=current.version,
            command_receipts=_read_governed_input_command_prefix(
                composition, identity
            ),
            counts=counts,
            run=run,
            expected_run_id=expected_run_id,
        )
        if resume_state == "WAITING_APPROVAL":
            _validate_waiting_defence_analysis_approval_resume(
                control,
                identity,
                run_id=expected_run_id,
            )
        return accepted, run


def _prepare_case(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    *,
    resume_pre_model_intake: bool = False,
    resume_pre_agent_inputs: bool = False,
    resume_v4_pre_model_source: bool = False,
    resume_m1_pre_model_source: bool = False,
    resume_m1_post_event_rule: bool = False,
    resume_m2_pre_model_source: bool = False,
    m3_firm_frozen_source_preflight: bool = False,
) -> tuple[AcceptedInput, ...]:
    if (
        m3_firm_frozen_source_preflight
        and not _uses_current_runtime_source_preflight()
    ):
        raise ManagedDefenceAcceptanceBlocked("M3/M4 固定法源预检仅允许当前受控验收使用。")
    if m3_firm_frozen_source_preflight and (
        resume_pre_model_intake
        or (
            resume_pre_agent_inputs
            and _ACCEPTANCE_NAME
            != _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
        )
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "M3 固定法源预检只能用于新的独立合成案件，禁止续接既有案件。"
        )
    if resume_pre_model_intake and (
        resume_pre_agent_inputs
        or resume_v4_pre_model_source
        or resume_m1_pre_model_source
        or resume_m1_post_event_rule
        or resume_m2_pre_model_source
        or m3_firm_frozen_source_preflight
    ):
        raise ManagedDefenceAcceptanceBlocked("两种合成验收恢复模式不能同时使用。")
    if sum(
        (
            resume_v4_pre_model_source,
            resume_m1_pre_model_source,
            resume_m1_post_event_rule,
            resume_m2_pre_model_source,
            m3_firm_frozen_source_preflight,
        )
    ) > 1:
        raise ManagedDefenceAcceptanceBlocked("两种固定法源恢复模式不能同时使用。")
    if (
        (
            resume_v4_pre_model_source
            or resume_m1_pre_model_source
            or resume_m1_post_event_rule
            or resume_m2_pre_model_source
        )
        and not resume_pre_agent_inputs
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "固定法源恢复必须从完整、已核验的受控输入检查点开始。"
        )
    with TemporaryDirectory(prefix="lawcase-managed-defence-input-") as temporary:
        generated = generate_golden_case(
            Path(temporary), load_authoritative_case(PROJECT_ROOT)
        )
        if len(generated.files) != 11 or len(read_generated_pages(generated)) != 88:
            raise ManagedDefenceAcceptanceBlocked("冻结合成案卷不满足 88 页、11 文件合同。")
        accepted_prefix: tuple[AcceptedInput, ...] = ()
        downstream_counts: Mapping[str, int] | None = None
        if resume_pre_model_intake or resume_pre_agent_inputs:
            try:
                current = _current_matter(composition, identity)
            except KeyError as error:
                raise ManagedDefenceAcceptanceBlocked("固定验收案件不存在，禁止恢复模式。") from error
            records, downstream_counts = _read_pre_model_resume_state(composition, identity)
            if resume_pre_model_intake:
                accepted_prefix = _validate_pre_model_resume_prefix(
                    generated,
                    current_matter_version=current.version,
                    records=records,
                    downstream_counts=downstream_counts,
                )
            else:
                accepted_prefix = _validate_completed_material_checkpoint(
                    generated, records=records
                )
        accepted = asyncio.run(
            _admit_sources(
                composition,
                identity,
                generated,
                accepted_prefix=accepted_prefix,
            )
        )
        if resume_pre_agent_inputs:
            assert downstream_counts is not None
            current = _current_matter(composition, identity)
            command_receipts = _read_governed_input_command_prefix(
                composition, identity
            )
            if resume_m1_post_event_rule:
                _validate_m1_post_event_rule_resume_prefix(
                    current_matter_version=current.version,
                    command_receipts=command_receipts,
                    legal_command_receipts=_read_m1_post_event_rule_command_prefix(
                        composition, identity
                    ),
                    counts=downstream_counts,
                )
            else:
                _validate_governed_input_resume_prefix(
                    current_matter_version=current.version,
                    command_receipts=command_receipts,
                    counts=downstream_counts,
                )
        _confirm_posture(composition, identity)
        facts = _confirmed_facts(composition, identity, generated)
        _confirm_claim_and_response(composition, identity, facts)
        expected_legal_start_version = (
            _GOVERNED_INPUT_COMPLETE_VERSION + 2
            if resume_m1_post_event_rule
            else _GOVERNED_INPUT_COMPLETE_VERSION
        )
        if _current_matter(composition, identity).version != expected_legal_start_version:
            raise ManagedDefenceAcceptanceBlocked("受控输入未收敛到固定的 Agent 前版本。")
        _confirm_legal_context(
            composition,
            identity,
            generated,
            firm_frozen_source_recovery_only=resume_v4_pre_model_source,
            m1_frozen_source_recovery_only=(
                resume_m1_pre_model_source or resume_m1_post_event_rule
            ),
            m2_firm_frozen_source_recovery_only=resume_m2_pre_model_source,
            m3_firm_frozen_source_preflight=m3_firm_frozen_source_preflight,
        )
        if _current_matter(composition, identity).version != _LEGAL_CONTEXT_COMPLETE_VERSION:
            raise ManagedDefenceAcceptanceBlocked("受控法源输入未收敛到固定的 Agent 前版本。")
        _confirm_m6_catalogue_evidence_scope(composition, identity, generated)
    return accepted


def _assert_task_boundary(
    control: object,
    identity: ServerIdentityContext,
    *,
    run_id: str,
    approval_id: str,
    phase: str,
) -> None:
    store = getattr(control, "_store", None)
    if store is None or not callable(getattr(store, "replay_run", None)):
        raise ManagedDefenceAcceptanceBlocked("受管 Agent 运行状态无法进行服务端验收。")
    state = store.replay_run(
        matter_id=_matter_id(identity), actor=identity.actor, run_id=run_id
    )
    task = next(
        (
            item
            for item in state.tasks
            if item.spec.task_id == approval_id and item.status.value == "WAITING_APPROVAL"
        ),
        None,
    )
    if task is None:
        raise ManagedDefenceAcceptanceBlocked("待批准任务与当前受管运行不一致。")
    skill_id = task.spec.skill.skill_id
    network_policy = getattr(task.spec.capability.network_policy, "value", None)
    if phase == "analysis":
        if skill_id != LAWYER_ANALYSIS_SKILL_ID or network_policy != "EXACT_ALLOWLIST":
            raise ManagedDefenceAcceptanceBlocked("首轮待批准任务不是受限的律师决策包模型分析。")
        return
    if phase == "document":
        if skill_id == LAWYER_ANALYSIS_SKILL_ID or network_policy != "DENY":
            raise ManagedDefenceAcceptanceBlocked("文书轮出现了不允许的外部模型任务。")
        return
    raise ManagedDefenceAcceptanceBlocked("验收任务阶段无效。")


def _validate_waiting_defence_analysis_approval_resume(
    control: object,
    identity: ServerIdentityContext,
    *,
    run_id: str,
) -> None:
    """Prove the sole approval gate is still before the one model submission.

    This is deliberately stricter than accepting a generic
    ``WAITING_APPROVAL`` run.  It recognizes exactly the server-compiled
    defence graph after its deterministic case-context task completed and
    before the sole networked lawyer-analysis task received a start event.
    """

    store = getattr(control, "_store", None)
    if store is None or not callable(getattr(store, "replay_run", None)):
        raise ManagedDefenceAcceptanceBlocked("无法读取受管 Agent 的持久任务边界。")
    state = store.replay_run(
        matter_id=_matter_id(identity), actor=identity.actor, run_id=run_id
    )
    if (
        getattr(getattr(state, "status", None), "value", None) != "WAITING_APPROVAL"
        or getattr(state, "event_version", None) != 5
        or getattr(getattr(state, "budget_usage", None), "external_calls", None) != 0
        or tuple(
            getattr(item, "value", item)
            for item in getattr(getattr(state, "goal", None), "requested_deliverables", ())
        )
        != (AgentDeliverableKind.DEFENCE_STATEMENT.value,)
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "受管运行不在可证明零模型提交的律师分析审批门。"
        )
    tasks = tuple(getattr(state, "tasks", ()))
    if len(tasks) != 2:
        raise ManagedDefenceAcceptanceBlocked("模型前受管任务图不符合固定应诉合同。")
    context_tasks = tuple(
        item
        for item in tasks
        if getattr(getattr(getattr(item, "spec", None), "skill", None), "skill_id", None)
        == "case_context_review"
    )
    analysis_tasks = tuple(
        item
        for item in tasks
        if getattr(getattr(getattr(item, "spec", None), "skill", None), "skill_id", None)
        == LAWYER_ANALYSIS_SKILL_ID
    )
    if len(context_tasks) != 1 or len(analysis_tasks) != 1:
        raise ManagedDefenceAcceptanceBlocked("模型前受管任务图出现了未授权任务。")
    context_task = context_tasks[0]
    analysis_task = analysis_tasks[0]
    context_policy = getattr(
        getattr(getattr(context_task, "spec", None), "capability", None),
        "network_policy",
        None,
    )
    analysis_policy = getattr(
        getattr(getattr(analysis_task, "spec", None), "capability", None),
        "network_policy",
        None,
    )
    context_receipts = tuple(getattr(context_task, "receipts", ()))
    if (
        getattr(getattr(context_task, "status", None), "value", None) != "SUCCEEDED"
        or getattr(context_policy, "value", None) != "DENY"
        or getattr(context_task, "attempt_count", None) != 1
        or len(context_receipts) != 1
        or getattr(context_receipts[0], "external_calls", None) != 0
        or getattr(
            getattr(context_receipts[0], "external_submission_state", None),
            "value",
            None,
        )
        != "NOT_APPLICABLE"
    ):
        raise ManagedDefenceAcceptanceBlocked("本地案件情境任务不符合零外发恢复合同。")
    if (
        getattr(getattr(analysis_task, "status", None), "value", None)
        != "WAITING_APPROVAL"
        or getattr(analysis_policy, "value", None) != "EXACT_ALLOWLIST"
        or getattr(analysis_task, "attempt_count", None) != 0
        or tuple(getattr(analysis_task, "receipts", ()))
        or tuple(getattr(getattr(analysis_task, "spec", None), "dependency_ids", ()))
        != (getattr(getattr(context_task, "spec", None), "task_id", None),)
    ):
        raise ManagedDefenceAcceptanceBlocked("律师分析任务不再处于模型提交前审批门。")
    artifacts = tuple(getattr(state, "artifacts", ()))
    if (
        len(artifacts) != 1
        or getattr(artifacts[0], "artifact_kind", None)
        != "CASE_CONTEXT_REVIEW_CANDIDATE"
        or getattr(artifacts[0], "managed_derivative", None) is not False
    ):
        raise ManagedDefenceAcceptanceBlocked("模型前运行成果不符合固定案件情境合同。")
    approvals = control.list_approvals(
        identity=identity, matter_id=_matter_id(identity), run_id=run_id
    )
    if (
        len(approvals) != 1
        or getattr(approvals[0], "approval_id", None)
        != getattr(getattr(analysis_task, "spec", None), "task_id", None)
        or getattr(approvals[0], "status", None) != "OPEN"
    ):
        raise ManagedDefenceAcceptanceBlocked("律师分析审批卡与固定受管任务不一致。")
    _assert_task_boundary(
        control,
        identity,
        run_id=run_id,
        approval_id=approvals[0].approval_id,
        phase="analysis",
    )


def _wait_for_run(
    control: object,
    identity: ServerIdentityContext,
    *,
    run_id: str,
    phase: str,
    deadline: float,
) -> object:
    approved: set[str] = set()
    while time.monotonic() < deadline:
        run = control.get_run(
            identity=identity, matter_id=_matter_id(identity), run_id=run_id
        )
        status = run.status
        if status == "READY_FOR_REVIEW":
            return run
        if status == "WAITING_APPROVAL":
            approvals = control.list_approvals(
                identity=identity, matter_id=_matter_id(identity), run_id=run_id
            )
            if len(approvals) != 1 or approvals[0].approval_id in approved:
                raise ManagedDefenceAcceptanceBlocked("受管运行出现非预期或重复的任务审批。")
            approval = approvals[0]
            _assert_task_boundary(
                control,
                identity,
                run_id=run_id,
                approval_id=approval.approval_id,
                phase=phase,
            )
            control.submit_approval(
                identity=identity,
                matter_id=_matter_id(identity),
                run_id=run_id,
                approval_id=approval.approval_id,
                approved=True,
                note=(
                    "合成验收夹具确认：仅允许本次受管内部任务继续；"
                    "不构成真实律师意见、终审、锁定或对外提交。"
                ),
                expected_run_version=run.version,
                idempotency_key=_key(f"{phase}-task-approval:{approval.approval_id}"),
                now=_safe_now(),
            )
            approved.add(approval.approval_id)
            continue
        if status in {
            "FAILED",
            "STALE",
            "CANCELLED",
            "RECONCILIATION_REQUIRED",
            "WAITING_INPUT",
            "PAUSED",
        }:
            reason = run.failure_code or run.failure_message or status
            raise ManagedDefenceAcceptanceBlocked(
                f"受管 {phase} 运行进入受控阻断状态：{reason}"
            )
        time.sleep(_POLL_SECONDS)
    raise ManagedDefenceAcceptanceBlocked(f"受管 {phase} 运行在固定等待上限内未完成。")


def _assert_observed_run_boundary(
    control: object,
    identity: ServerIdentityContext,
    *,
    run_id: str,
    phase: str,
) -> int:
    """Read the durable task graph and measured budget after verification.

    This is intentionally stronger than trusting the browser projection: the
    acceptance result must prove which task shapes actually survived planning
    and how many external calls the supervisor recorded.
    """

    store = getattr(control, "_store", None)
    if store is None or not callable(getattr(store, "replay_run", None)):
        raise ManagedDefenceAcceptanceBlocked("无法读取受管 Agent 的持久任务边界。")
    state = store.replay_run(
        matter_id=_matter_id(identity), actor=identity.actor, run_id=run_id
    )
    external_calls = int(state.budget_usage.external_calls)
    external_tasks = tuple(
        item
        for item in state.tasks
        if getattr(item.spec.capability.network_policy, "value", None)
        == "EXACT_ALLOWLIST"
    )
    if phase == "analysis":
        if (
            external_calls != 1
            or len(external_tasks) != 1
            or external_tasks[0].spec.skill.skill_id != LAWYER_ANALYSIS_SKILL_ID
        ):
            raise ManagedDefenceAcceptanceBlocked(
                "首轮没有实测为唯一律师决策包模型调用。"
            )
        return external_calls
    if phase == "document":
        if external_calls != 0 or external_tasks:
            raise ManagedDefenceAcceptanceBlocked(
                "文书轮出现了不允许的外部调用或网络任务。"
            )
        return external_calls
    raise ManagedDefenceAcceptanceBlocked("运行边界验收阶段无效。")


def _wait_for_candidate_plan(
    dynamic_plan_service: object,
    identity: ServerIdentityContext,
    *,
    deadline: float,
) -> object:
    while time.monotonic() < deadline:
        plan = dynamic_plan_service.current_plan(
            identity=identity, matter_id=_matter_id(identity)
        )
        if plan is not None and plan.status == "CANDIDATE" and plan.inputs_current:
            return plan
        if plan is not None and plan.status == "STALE":
            raise ManagedDefenceAcceptanceBlocked("动态办案计划在激活前已失效。")
        time.sleep(_POLL_SECONDS)
    raise ManagedDefenceAcceptanceBlocked("律师决策包未在固定等待上限内形成动态计划候选。")


def _activate_exact_defence_plan(
    dynamic_plan_service: object,
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    plan: object,
) -> None:
    delivery_kinds = tuple(
        sorted(
            item.deliverable_kind
            for item in plan.items
            if item.deliverable_kind is not None
        )
    )
    expected_delivery_kinds = tuple(
        item.value for item in _requested_deliverables_for_acceptance()
    )
    if delivery_kinds != expected_delivery_kinds:
        raise ManagedDefenceAcceptanceBlocked(
            "动态计划没有精确收敛为本次办案目标要求的候选成果，未启动文书轮。"
        )
    for item in plan.items:
        receipt = dynamic_plan_service.decide_item(
            identity=identity,
            matter_id=_matter_id(identity),
            plan_id=plan.plan_id,
            item_id=item.item_id,
            expected_version=plan.current_matter_version,
            idempotency_key=_key(f"plan-review:{item.item_id}"),
            decision="APPROVE",
            reason_code="VERIFIED_BY_COUNSEL",
            readiness_override=None,
            required_for_delivery_override=None,
        )
        if receipt.decision_status != "APPROVED":
            raise ManagedDefenceAcceptanceBlocked("合成夹具未能完成动态计划事项复核。")
    refreshed = dynamic_plan_service.current_plan(
        identity=identity, matter_id=_matter_id(identity)
    )
    if refreshed is None or not refreshed.can_activate:
        raise ManagedDefenceAcceptanceBlocked("动态办案计划没有满足受控激活前置条件。")
    current = _current_matter(composition, identity)
    if current.version != refreshed.current_matter_version:
        raise ManagedDefenceAcceptanceBlocked("动态计划激活前案件版本不一致。")
    activation = dynamic_plan_service.activate_current_plan(
        identity=identity,
        matter_id=current.matter_id,
        expected_version=current.version,
        idempotency_key=_key("activate-defence-plan"),
    )
    if activation.status != "ACTIVE" or activation.matter_version != current.version + 1:
        raise ManagedDefenceAcceptanceBlocked("动态办案计划激活回执不一致。")


def _document_artifact_ids(
    composition: WebRuntimeComposition,
    control: object,
    identity: ServerIdentityContext,
    run_id: str,
) -> dict[str, str]:
    artifacts = control.list_artifacts(
        identity=identity, matter_id=_matter_id(identity), run_id=run_id
    )
    service = composition.api_dependencies.case_agent_document_review_service
    assert service is not None
    candidates: dict[str, str] = {}
    for item in artifacts:
        if item.artifact_type != "REVIEWABLE_DOCUMENT_CANDIDATE_JSON":
            continue
        review = service.read_review(
            identity=identity,
            matter_id=_matter_id(identity),
            run_id=run_id,
            artifact_id=item.artifact_id,
        )
        candidates[review.deliverable_kind] = item.artifact_id
    expected = {item.value for item in _requested_deliverables_for_acceptance()}
    if set(candidates) != expected:
        raise ManagedDefenceAcceptanceBlocked(
            "文书轮没有形成与办案目标逐项对应的可复核候选。"
        )
    return candidates


def _verify_reviewable_document(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    *,
    run_id: str,
    artifact_id: str,
    expected_deliverable_kind: AgentDeliverableKind,
) -> dict[str, object]:
    service = composition.api_dependencies.case_agent_document_review_service
    assert service is not None
    review = service.read_review(
        identity=identity,
        matter_id=_matter_id(identity),
        run_id=run_id,
        artifact_id=artifact_id,
    )
    if (
        review.deliverable_kind != expected_deliverable_kind.value
        or review.version_status != "CURRENT"
        or not review.download_ready
        or review.review_pdf_page_count < 1
    ):
        raise ManagedDefenceAcceptanceBlocked("答辩状候选未通过受管文书读取前置条件。")
    editable = service.download(
        identity=identity,
        matter_id=_matter_id(identity),
        run_id=run_id,
        artifact_id=artifact_id,
        file_role="editable",
    )
    preview = service.download(
        identity=identity,
        matter_id=_matter_id(identity),
        run_id=run_id,
        artifact_id=artifact_id,
        file_role="pdf-preview",
    )
    try:
        with ZipFile(BytesIO(editable.content)) as archive:
            structure_name = (
                "word/document.xml"
                if review.output_format == "DOCX"
                else "xl/workbook.xml"
            )
            editable_structure = archive.read(structure_name).decode("utf-8")
    except (BadZipFile, KeyError, UnicodeDecodeError) as error:
        raise ManagedDefenceAcceptanceBlocked("DOCX 候选无法作为可编辑文书读取。") from error
    try:
        pdf = PdfReader(BytesIO(preview.content))
        pdf_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as error:
        raise ManagedDefenceAcceptanceBlocked("PDF 候选无法作为律师审阅件读取。") from error
    if (
        not editable_structure.strip()
        or not pdf_text.strip()
        or len(pdf.pages) != review.review_pdf_page_count
    ):
        raise ManagedDefenceAcceptanceBlocked("候选成果缺少可读的编辑件或 PDF 审阅结构。")
    return {
        "artifact_id": artifact_id,
        "deliverable_kind": review.deliverable_kind,
        "title": review.title,
        "template_version": review.template_version,
        "docx_sha256": sha256(editable.content).hexdigest(),
        "docx_bytes": len(editable.content),
        "pdf_sha256": sha256(preview.content).hexdigest(),
        "pdf_pages": len(pdf.pages),
        "candidate_mark_verified": True,
    }


def _validate_acceptance_recovery_mode(
    *,
    resume_pre_model_intake: bool,
    resume_pre_agent_inputs: bool,
    resume_created_defence_run: bool,
    resume_v4_pre_model_source: bool = False,
    resume_m1_pre_model_source: bool = False,
    resume_m1_post_event_rule: bool = False,
    resume_m2_pre_model_source: bool = False,
) -> None:
    """Keep recovery at an exact pre-provider boundary for each fixed scenario."""

    if sum(
        (
            resume_pre_model_intake,
            resume_pre_agent_inputs,
            resume_created_defence_run,
            resume_v4_pre_model_source,
            resume_m1_pre_model_source,
            resume_m1_post_event_rule,
            resume_m2_pre_model_source,
        )
    ) > 1:
        raise ManagedDefenceAcceptanceBlocked("合成验收恢复模式不能同时使用。")
    if resume_v4_pre_model_source:
        if _ACCEPTANCE_NAME != _SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME:
            raise ManagedDefenceAcceptanceBlocked(
                "冻结法源预模型恢复仅允许 v4 受控验收使用。"
            )
        return
    if resume_m1_pre_model_source:
        if _ACCEPTANCE_NAME != _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME:
            raise ManagedDefenceAcceptanceBlocked(
                "M1 冻结法源预模型恢复仅允许 M1 受控验收使用。"
            )
        return
    if resume_m1_post_event_rule:
        if _ACCEPTANCE_NAME != _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME:
            raise ManagedDefenceAcceptanceBlocked(
                "M1 事件后规则恢复仅允许 M1 受控验收使用。"
            )
        return
    if resume_m2_pre_model_source:
        if _ACCEPTANCE_NAME != _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME:
            raise ManagedDefenceAcceptanceBlocked(
                "M2 固定法源预模型恢复仅允许 M2 受控验收使用。"
            )
        return
    if _ACCEPTANCE_NAME == _CONTRACT_REPAIR_ACCEPTANCE_NAME and (
        resume_pre_model_intake or resume_created_defence_run
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "受控 v2 只允许新建独立合成案件，或从零 Agent、零外发的完整受控输入检查点恢复。"
        )
    if _ACCEPTANCE_NAME in {
        _FINAL_RUNTIME_ACCEPTANCE_NAME,
        _SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME,
        _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME,
        _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME,
        _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME,
        _M4_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME,
        _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME,
        _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME,
        _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
        _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
    } and any(
        (resume_pre_model_intake, resume_pre_agent_inputs, resume_created_defence_run)
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "受控最终验收只能新建一次独立案件，禁止续接任何既有运行。"
        )
    if _ACCEPTANCE_NAME == _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME and (
        resume_pre_model_intake or resume_created_defence_run
    ):
        raise ManagedDefenceAcceptanceBlocked(
            "M6 只允许从零 Agent、零外发的完整受控输入检查点恢复。"
        )


def _confirm_discovered_issue_and_reconfirm_legal_bundle(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
) -> tuple[object, object]:
    """Record the deliberate human bridge between discovery and final analysis.

    This helper is only for the isolated M9 synthetic acceptance.  The first
    Agent run has already produced a reviewable discovery candidate; this
    routine intentionally does not promote model output.  It models the two
    separate lead-lawyer commands the Web journey exposes: confirm a specific
    issue from current facts/claim, then approve a fresh current legal bundle.
    Both commands advance the matter version, which is why the final Agent run
    is necessarily a new run rather than a retry of the discovery run.
    """

    ledger: PostgresCaseLedgerStore = composition.case_ledger_store
    matter_id = _matter_id(identity)
    snapshot = ledger.get_case_snapshot(matter_id=matter_id, actor=identity.actor)
    claims = tuple(
        item for item in snapshot.claims if item.get("status") == "CONFIRMED_SCOPE"
    )
    facts = tuple(item for item in snapshot.facts if item.get("status") == "CONFIRMED")
    if len(claims) != 1 or len(facts) < 2 or snapshot.issues:
        raise ManagedDefenceAcceptanceBlocked(
            "M9 发现后律师确认前的诉请、事实或争点状态不符合固定夹具。"
        )
    issue = ledger.create_dispute_issue_candidate(
        matter_id=matter_id,
        actor=identity.actor,
        expected_version=snapshot.version,
        idempotency_key=_key("m9-lawyer-issue-candidate"),
        question="第二笔借款诉请金额与银行流水记录不一致，应核对其范围及已还款抗辩。",
        claim_ids=(str(claims[0]["claim_id"]),),
        confirmed_fact_ids=(str(facts[0]["fact_id"]), str(facts[1]["fact_id"])),
    )
    issue_confirmation = ledger.confirm_dispute_issue(
        matter_id=matter_id,
        issue_id=issue.object_id,
        actor=identity.actor,
        expected_version=snapshot.version + 1,
        idempotency_key=_key("m9-lawyer-issue-confirm"),
        approval_hash=_canonical_hash(
            {
                "fixture": _ACCEPTANCE_NAME,
                "kind": "lead-lawyer-confirms-discovered-issue",
                "issue_id": issue.object_id,
                "claim_id": claims[0]["claim_id"],
                "fact_ids": (facts[0]["fact_id"], facts[1]["fact_id"]),
            }
        ),
    )
    if issue_confirmation.matter_version != snapshot.version + 2:
        raise ManagedDefenceAcceptanceBlocked("M9 律师确认争点回执没有推进当前案件版本。")

    legal_snapshot = composition.legal_store.get_legal_review_snapshot(
        matter_id=matter_id, actor=identity.actor
    )
    rule_id, rule_version = _response_source_scope_rule_identity()
    matching_rules = tuple(
        item
        for item in legal_snapshot.rule_versions
        if item.get("rule_id") == rule_id
        and item.get("rule_version") == rule_version
        and item.get("status") == "APPROVED"
    )
    matching_events = tuple(
        item
        for item in legal_snapshot.legal_events
        if item.get("event_kind") == LegalEventKind.CONTRACT_SIGNED.value
        and item.get("status") == "APPROVED"
    )
    if len(matching_rules) != 1 or len(matching_events) != 1:
        raise ManagedDefenceAcceptanceBlocked(
            "M9 律师确认争点后找不到可重新核对的当前规则或关键日期。"
        )
    current = _current_matter(composition, identity)
    if current.version != issue_confirmation.matter_version:
        raise ManagedDefenceAcceptanceBlocked("M9 法源包重确认前案件版本已变化。")
    bundle = composition.legal_store.approve_case_legal_bundle(
        matter_id=matter_id,
        actor=identity.actor,
        expected_version=current.version,
        idempotency_key=_key("m9-lawyer-legal-bundle-reconfirm"),
        segments=(
            LegalBundleSegmentSelection(
                segment_id=_acceptance_id("m9-reconfirmed-legal-bundle-segment"),
                issue_key=rule_id,
                rule_version_id=str(matching_rules[0]["rule_version_id"]),
                trigger_event_id=str(matching_events[0]["legal_event_id"]),
                start_date=date(2019, 6, 3),
                end_date=_FIXED_BUNDLE_END,
                applicability_anchor=(
                    "主办律师在确认争点后重新核对本案依据；仅建立当前来源审阅边界，"
                    "不生成或确认金额、利率或法律结论。"
                ),
            ),
        ),
        approval_hash=_canonical_hash(
            {
                "fixture": _ACCEPTANCE_NAME,
                "kind": "lead-lawyer-reconfirms-current-legal-bundle",
                "issue_id": issue.object_id,
                "rule_version_id": matching_rules[0]["rule_version_id"],
                "legal_event_id": matching_events[0]["legal_event_id"],
            }
        ),
    )
    if bundle.matter_version != current.version + 1:
        raise ManagedDefenceAcceptanceBlocked("M9 当前法源包重确认回执没有推进案件版本。")
    return issue_confirmation, bundle


def _run_acceptance(
    composition: WebRuntimeComposition,
    identity: ServerIdentityContext,
    *,
    max_wait_seconds: int,
    resume_pre_model_intake: bool = False,
    resume_pre_agent_inputs: bool = False,
    resume_created_defence_run: bool = False,
    resume_v4_pre_model_source: bool = False,
    resume_m1_pre_model_source: bool = False,
    resume_m1_post_event_rule: bool = False,
    resume_m2_pre_model_source: bool = False,
) -> dict[str, object]:
    # Intake must never race a missing matter.  The fixed identifier is also a
    # hard no-retry boundary: a normal invocation blocks if the record already
    # exists.  Explicit recovery is limited to either a material-only prefix
    # or an exact, receipt-verified pre-Agent input prefix; neither may exist
    # beside any Agent run or external submission.
    _validate_acceptance_recovery_mode(
        resume_pre_model_intake=resume_pre_model_intake,
        resume_pre_agent_inputs=resume_pre_agent_inputs,
        resume_created_defence_run=resume_created_defence_run,
        resume_v4_pre_model_source=resume_v4_pre_model_source,
        resume_m1_pre_model_source=resume_m1_pre_model_source,
        resume_m1_post_event_rule=resume_m1_post_event_rule,
        resume_m2_pre_model_source=resume_m2_pre_model_source,
    )
    if resume_created_defence_run:
        control = composition.api_dependencies.case_agent_control_service
        dynamic_plan_service = composition.api_dependencies.dynamic_case_plan_service
        assert control is not None and dynamic_plan_service is not None
        accepted, first = _load_created_defence_run_resume(
            composition, identity, control
        )
    elif resume_pre_model_intake:
        accepted = _prepare_case(
            composition, identity, resume_pre_model_intake=True
        )
    elif resume_pre_agent_inputs:
        accepted = _prepare_case(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            m3_firm_frozen_source_preflight=(
                _ACCEPTANCE_NAME
                == _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
            ),
        )
    elif resume_v4_pre_model_source:
        accepted = _prepare_case(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_v4_pre_model_source=True,
        )
    elif resume_m1_pre_model_source:
        accepted = _prepare_case(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_m1_pre_model_source=True,
        )
    elif resume_m1_post_event_rule:
        accepted = _prepare_case(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_m1_post_event_rule=True,
        )
    elif resume_m2_pre_model_source:
        accepted = _prepare_case(
            composition,
            identity,
            resume_pre_agent_inputs=True,
            resume_m2_pre_model_source=True,
        )
    else:
        _create_new_matter(composition, identity)
        if _uses_current_runtime_source_preflight():
            accepted = _prepare_case(
                composition,
                identity,
                m3_firm_frozen_source_preflight=True,
            )
        else:
            accepted = _prepare_case(composition, identity)
    if not resume_created_defence_run:
        control = composition.api_dependencies.case_agent_control_service
        dynamic_plan_service = composition.api_dependencies.dynamic_case_plan_service
        assert control is not None and dynamic_plan_service is not None
        current = _current_matter(composition, identity)
        first = control.create_run(
            identity=identity,
            matter_id=current.matter_id,
            objective=_FIRST_RUN_OBJECTIVE,
            success_criteria=_FIRST_RUN_SUCCESS_CRITERIA,
            constraints=_FIRST_RUN_CONSTRAINTS,
            expected_matter_version=current.version,
            idempotency_key=_key("first-run"),
            now=_safe_now(),
            requested_deliverables=_requested_deliverables_for_acceptance(),
        )
    deadline = time.monotonic() + max_wait_seconds
    first_ready = _wait_for_run(
        control, identity, run_id=first.run_id, phase="analysis", deadline=deadline
    )
    first_external_calls = _assert_observed_run_boundary(
        control, identity, run_id=first.run_id, phase="analysis"
    )
    first_artifacts = control.list_artifacts(
        identity=identity, matter_id=_matter_id(identity), run_id=first.run_id
    )
    if not any(
        item.artifact_type == "LAWYER_DECISION_PACKAGE_CANDIDATE"
        for item in first_artifacts
    ):
        raise ManagedDefenceAcceptanceBlocked("首轮未形成可复核律师案件决策包。")
    discovery_run: dict[str, object] | None = None
    if _ACCEPTANCE_NAME in {
        _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME,
        _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME,
        _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME,
    }:
        # The discovery run is immutable historical evidence.  A human issue
        # confirmation invalidates its bundle, so the final analysis must be a
        # distinct run with a fresh snapshot and a separately approved budget.
        discovery_run = {
            "run_id": first.run_id,
            "status": first_ready.status,
            "artifact_count": len(first_artifacts),
            "observed_external_calls": first_external_calls,
            "automatic_retry": False,
        }
        _confirm_discovered_issue_and_reconfirm_legal_bundle(composition, identity)
        current = _current_matter(composition, identity)
        first = control.create_run(
            identity=identity,
            matter_id=current.matter_id,
            objective=(
                "基于律师已确认争点和当前重新核对的依据，形成可审阅的风险分析、"
                "补证清单、证据目录与民事答辩状候选。"
            ),
            success_criteria=(
                "输出来源绑定、待律师决定的律师案件决策包",
                "仅在主办律师激活动态计划后生成四类内部审阅候选",
            ),
            constraints=_FIRST_RUN_CONSTRAINTS,
            expected_matter_version=current.version,
            idempotency_key=_key("m9-final-analysis-run"),
            now=_safe_now(),
            requested_deliverables=_requested_deliverables_for_acceptance(),
        )
        first_ready = _wait_for_run(
            control, identity, run_id=first.run_id, phase="analysis", deadline=deadline
        )
        first_external_calls = _assert_observed_run_boundary(
            control, identity, run_id=first.run_id, phase="analysis"
        )
        first_artifacts = control.list_artifacts(
            identity=identity, matter_id=_matter_id(identity), run_id=first.run_id
        )
        if not any(
            item.artifact_type == "LAWYER_DECISION_PACKAGE_CANDIDATE"
            for item in first_artifacts
        ):
            raise ManagedDefenceAcceptanceBlocked(
                "M9 最终研判未形成可复核律师案件决策包。"
            )
    plan = _wait_for_candidate_plan(dynamic_plan_service, identity, deadline=deadline)
    _activate_exact_defence_plan(dynamic_plan_service, composition, identity, plan)
    current = _current_matter(composition, identity)
    second = control.execute_active_plan(
        identity=identity,
        matter_id=current.matter_id,
        expected_matter_version=current.version,
        idempotency_key=_key("second-document-run"),
        now=_safe_now(),
    )
    second_ready = _wait_for_run(
        control, identity, run_id=second.run_id, phase="document", deadline=deadline
    )
    second_external_calls = _assert_observed_run_boundary(
        control, identity, run_id=second.run_id, phase="document"
    )
    document_ids = _document_artifact_ids(
        composition, control, identity, second.run_id
    )
    documents = {
        kind.value: _verify_reviewable_document(
            composition,
            identity,
            run_id=second.run_id,
            artifact_id=document_ids[kind.value],
            expected_deliverable_kind=kind,
        )
        for kind in _requested_deliverables_for_acceptance()
    }
    return {
        "status": "PASS_SYNTHETIC_MANAGED_DEFENCE_ACCEPTANCE",
        "evidence_scope": _acceptance_evidence_scope(),
        "synthetic_only": True,
        "acceptance_scenario": _ACCEPTANCE_NAME,
        "preceding_failed_scenario": (
            _PRIMARY_ACCEPTANCE_NAME
            if _ACCEPTANCE_NAME == _CONTRACT_REPAIR_ACCEPTANCE_NAME
            else None
        ),
        "preceding_indeterminate_scenario": (
            _CONTRACT_REPAIR_ACCEPTANCE_NAME
            if _ACCEPTANCE_NAME == _FINAL_RUNTIME_ACCEPTANCE_NAME
            else None
        ),
        "preceding_policy_repaired_scenario": (
            _FINAL_RUNTIME_ACCEPTANCE_NAME
            if _ACCEPTANCE_NAME == _SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
            else None
        ),
        "preceding_source_gate_blocked_scenario": (
            _SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME
            if _ACCEPTANCE_NAME == _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
            else None
        ),
        "preceding_external_result_uncertain_scenario": (
            _M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
            if _ACCEPTANCE_NAME == _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
            else None
        ),
        "preceding_sealed_response_replay_scenario": (
            _M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME
            if _ACCEPTANCE_NAME == _M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME
            else None
        ),
        "current_runtime_fresh_source_preflight": _uses_current_runtime_source_preflight(),
        "pre_model_source_recovery": (
            resume_v4_pre_model_source
            or resume_m1_pre_model_source
            or resume_m1_post_event_rule
            or resume_m2_pre_model_source
        ),
        "pre_model_source_recovery_kind": (
            "v4-firm-frozen-source"
            if resume_v4_pre_model_source
            else (
                "m1-same-matter-frozen-source"
                if resume_m1_pre_model_source
                else (
                    "m1-post-event-rule"
                    if resume_m1_post_event_rule
                    else (
                        "m2-same-firm-frozen-source"
                        if resume_m2_pre_model_source
                        else None
                    )
                )
            )
        ),
        "matter_id": _matter_id(identity),
        "input": {
            "file_count": len(accepted),
            "page_count": sum(item.page_count for item in accepted),
            "accepted": [asdict(item) for item in accepted],
        },
        "first_run": {
            "run_id": first.run_id,
            "status": first_ready.status,
            "artifact_count": len(first_artifacts),
            "observed_external_calls": first_external_calls,
            "automatic_retry": False,
        },
        "discovery_run": discovery_run,
        "second_run": {
            "run_id": second.run_id,
            "status": second_ready.status,
            "observed_external_calls": second_external_calls,
            "automatic_retry": False,
        },
        "documents": documents,
        "not_proven": (
            "浏览器律师旅程",
            "执业律师对真实匿名案卷的认可",
            "真实案件的官方法源捕获、人工审核与效力确认",
            "最终锁定、签署或法院提交",
            "跨租户、备份恢复与供应商治理",
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只执行一次受控的 88 页合成被告应诉 Agent 验收。"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "仅供隔离运维环境显式提供；受管 API 容器默认只使用其已注入的 "
            "Web 配置，不读取或挂载 Worker 密钥。"
        ),
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=900,
        help="整个受管链的固定最大等待时间，最大 1200 秒。",
    )
    scenario = parser.add_mutually_exclusive_group()
    scenario.add_argument(
        "--contract-repair-v2",
        action="store_true",
        help=(
            "仅运行 ADR-0066 定义的独立 v2 合成验收；它使用不同固定案件身份，"
            "最多一笔新模型调用，绝不续接或重试 v1。若 v2 在创建 Agent 前停止，"
            "仅可配合完整受控输入恢复进行一次状态核验后的续接。"
        ),
    )
    scenario.add_argument(
        "--runtime-recovery-v3",
        action="store_true",
        help=(
            "仅运行 ADR-0067 定义的独立 v3 最终合成验收；它只能在稳定运行环境门槛"
            "已核验后执行一次，绝不续接或重试外发状态未知的 v2。"
        ),
    )
    scenario.add_argument(
        "--source-bound-numeric-v4",
        action="store_true",
        help=(
            "仅运行 ADR-0069 定义的独立 v4 最终合成验收；它只在 v3 封存响应已通过"
            "来源绑定数字策略的只读回放、运行门槛已核验后执行一次，绝不续接或重试 v3。"
        ),
    )
    scenario.add_argument(
        "--source-qualified-m1-final",
        action="store_true",
        help=(
            "仅运行 ADR-0073 定义的 M1 源码可读最终合成验收；它使用独立案件和"
            "固定、可见正文的政府公开法源快照，最多一笔新模型调用，绝不改写或续接 V4。"
        ),
    )
    scenario.add_argument(
        "--source-qualified-m2-final",
        action="store_true",
        help=(
            "仅运行 ADR-0075 定义的 M2 独立合成验收；M1 的唯一外发结果已封存，"
            "M2 使用新的固定案件、同一已冻结可读法源和独立规则版本，最多一笔新模型调用，"
            "绝不恢复、续接、改写或重发 M1。"
        ),
    )
    scenario.add_argument(
        "--source-qualified-m3-current-runtime",
        action="store_true",
        help=(
            "仅运行 ADR-0079 定义的 M3 当前运行时完整合成验收；它使用独立固定案件，"
            "只读取 24 小时内同律所已审批、同哈希的公共法源并复制到本案私有链，"
            "最多一笔新模型调用，绝不续接、改写或重发 M1/M2。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m4-current-runtime",
        action="store_true",
        help=(
            "仅用于 M4：新建独立 88 页合成案卷，以一次受控分析调用验证案件审阅、"
            "答辩状、证据目录和补证清单四类候选的来源绑定与下载。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m5-fresh-source",
        action="store_true",
        help=(
            "仅用于 M5：在 M4 因同律所法源超过复用窗口停止后，新建独立合成案卷，"
            "重新获取已登记官方原文并以一次受控分析调用验证四类候选成果。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m6-confirmed-evidence",
        action="store_true",
        help=(
            "仅用于 M6：新建独立合成案卷，复用 24 小时内已认证的同律所公共法源，"
            "并在模型分析前由固定律师验收身份确认六个已绑定事实来源页，"
            "以一次受控分析调用验证四类候选均可生成和下载。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m7-fresh-source-confirmed-evidence",
        action="store_true",
        help=(
            "仅用于 M7：在 M6 因法源复用窗口停止且未触发模型后，新建独立合成案卷，"
            "重新认证已登记官方原文，并在分析前确认六个已绑定事实来源页，"
            "以一次受控分析调用验证四类候选均可生成和下载。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m8-post-parser-repair",
        action="store_true",
        help=(
            "仅用于 M8：M7 的唯一模型响应已由当前严格解析器证明可接收，"
            "新建独立合成案卷，以一次受控分析调用验证修复后的四类候选成果。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m9-discovery-to-final",
        action="store_true",
        help=(
            "仅用于 M9：新建独立合成案卷，先以一次受控调用形成来源绑定的问题发现候选；"
            "由固定律师验收身份确认争点并重新确认当前依据包后，再以一次独立受控调用"
            "形成最终研判，随后验证四类候选成果和下载。两次调用分别对应两次明确的人工作业，"
            "不是自动重试。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m10-discovery-to-final-full",
        action="store_true",
        help=(
            "仅用于 M10：M9 已证明发现、争点确认、依据重确认与最终研判的双运行顺序，"
            "但未把已确认的六页材料纳入证据目录。本次使用新的独立合成案件，"
            "在首次发现前完成同一受限页级确认，并验证完整四件套。"
        ),
    )
    scenario.add_argument(
        "--full-delivery-m11-discovery-to-final-fresh-source",
        action="store_true",
        help=(
            "仅用于 M11：M10 在模型调用前被网络故障拦截，且其案件壳按防重规则不可覆盖。"
            "本次新建独立合成案件，重新认证当前官方原文，并在首次发现前确认六个受限证据页，"
            "验证发现、争点确认、依据重确认和完整四件套。"
        ),
    )
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument(
        "--resume-pre-model-intake",
        action="store_true",
        help=(
            "仅恢复已确认、尚未创建任何 Agent 运行的固定合成案件材料前缀；"
            "不会重传已完成原件，也不会自动重试任何模型调用。"
        ),
    )
    recovery.add_argument(
        "--resume-pre-agent-inputs",
        action="store_true",
        help=(
            "仅恢复完整材料检查点之后、尚未创建任何 Agent 运行的固定受控输入命令前缀；"
            "必须逐条核验命令回执、案件版本和投影，且不会重试任何模型调用。"
        ),
    )
    recovery.add_argument(
        "--resume-created-defence-run",
        action="store_true",
        help=(
            "仅续接固定首轮在 CREATED、版本 1 且尚未提交模型时的受管运行；"
            "逐项核验完整材料、受控输入、法源检查点和运行状态，不创建第二次调用。"
        ),
    )
    recovery.add_argument(
        "--recover-v4-pre-model-source",
        action="store_true",
        help=(
            "仅用于 ADR-0069 的 v4：在零法律事件、零 Agent 运行、零外发的精确版本 30 "
            "检查点，读取同律所已审批的同哈希官方法源并复制到本案私有链；不访问公网，"
            "不重发模型。"
        ),
    )
    recovery.add_argument(
        "--recover-m1-pre-model-source",
        action="store_true",
        help=(
            "仅用于 ADR-0073 的 M1：在零法律事件、零 Agent 运行、零外发的精确版本 30 "
            "检查点，读取同案已认证的冻结法源对象并完成来源登记；不访问公网、不新建案件，"
            "随后只允许 M1 原定的一次模型调用。"
        ),
    )
    recovery.add_argument(
        "--recover-m1-post-event-rule",
        action="store_true",
        help=(
            "仅用于 ADR-0073 的 M1：仅当同案准确停在版本 32、来源与借款事件均已登记、"
            "规则包/Agent/外发均为零时，复用同案冻结法源继续规则版本审批；不访问公网、"
            "不新建案件，随后只允许 M1 原定的一次模型调用。"
        ),
    )
    recovery.add_argument(
        "--recover-m2-pre-model-source",
        action="store_true",
        help=(
            "仅用于 ADR-0076 的 M2：在完整受控输入、零法律事件/Agent/外发的精确版本 30 "
            "检查点，从 24 小时内同律所已审批、同哈希的公共法源对象复制到本案私有链；"
            "不访问公网、不新建案件，随后只允许 M2 原定的一次模型调用。"
        ),
    )
    arguments = parser.parse_args()
    if not 30 <= arguments.max_wait_seconds <= _MAX_WAIT_SECONDS:
        parser.error("--max-wait-seconds 必须在 30 到 1200 之间。")
    if arguments.contract_repair_v2:
        _select_acceptance_scenario(_CONTRACT_REPAIR_ACCEPTANCE_NAME)
    elif arguments.runtime_recovery_v3:
        _select_acceptance_scenario(_FINAL_RUNTIME_ACCEPTANCE_NAME)
    elif arguments.source_bound_numeric_v4:
        _select_acceptance_scenario(_SOURCE_BOUND_NUMERIC_ACCEPTANCE_NAME)
    elif arguments.source_qualified_m1_final:
        _select_acceptance_scenario(_M1_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME)
    elif arguments.source_qualified_m2_final:
        _select_acceptance_scenario(_M2_SOURCE_QUALIFIED_FINAL_ACCEPTANCE_NAME)
    elif arguments.source_qualified_m3_current_runtime:
        _select_acceptance_scenario(_M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME)
    elif arguments.full_delivery_m4_current_runtime:
        _select_acceptance_scenario(_M4_FULL_DELIVERY_ACCEPTANCE_NAME)
    elif arguments.full_delivery_m5_fresh_source:
        _select_acceptance_scenario(_M5_FULL_DELIVERY_FRESH_SOURCE_ACCEPTANCE_NAME)
    elif arguments.full_delivery_m6_confirmed_evidence:
        _select_acceptance_scenario(
            _M6_FULL_DELIVERY_WITH_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
        )
    elif arguments.full_delivery_m7_fresh_source_confirmed_evidence:
        _select_acceptance_scenario(
            _M7_FULL_DELIVERY_FRESH_SOURCE_CONFIRMED_EVIDENCE_ACCEPTANCE_NAME
        )
    elif arguments.full_delivery_m8_post_parser_repair:
        _select_acceptance_scenario(
            _M8_FULL_DELIVERY_POST_PARSER_REPAIR_ACCEPTANCE_NAME
        )
    elif arguments.full_delivery_m9_discovery_to_final:
        _select_acceptance_scenario(
            _M9_DISCOVERY_TO_FINAL_DELIVERY_ACCEPTANCE_NAME
        )
    elif arguments.full_delivery_m10_discovery_to_final_full:
        _select_acceptance_scenario(
            _M10_DISCOVERY_TO_FINAL_FULL_DELIVERY_ACCEPTANCE_NAME
        )
    elif arguments.full_delivery_m11_discovery_to_final_fresh_source:
        _select_acceptance_scenario(
            _M11_DISCOVERY_TO_FINAL_FRESH_SOURCE_FULL_DELIVERY_ACCEPTANCE_NAME
        )
    try:
        _validate_acceptance_recovery_mode(
            resume_pre_model_intake=arguments.resume_pre_model_intake,
            resume_pre_agent_inputs=arguments.resume_pre_agent_inputs,
            resume_created_defence_run=arguments.resume_created_defence_run,
            resume_v4_pre_model_source=arguments.recover_v4_pre_model_source,
            resume_m1_pre_model_source=arguments.recover_m1_pre_model_source,
            resume_m1_post_event_rule=arguments.recover_m1_post_event_rule,
            resume_m2_pre_model_source=arguments.recover_m2_pre_model_source,
        )
    except ManagedDefenceAcceptanceBlocked as error:
        parser.error(str(error))
    composition: WebRuntimeComposition | None = None
    session_id: str | None = None
    try:
        composition = _build_composition(
            arguments.env_file.resolve() if arguments.env_file is not None else None
        )
        identity, session_id = _issue_fixture_identity(composition)
        result = _run_acceptance(
            composition,
            identity,
            max_wait_seconds=arguments.max_wait_seconds,
            resume_pre_model_intake=arguments.resume_pre_model_intake,
            resume_pre_agent_inputs=arguments.resume_pre_agent_inputs,
            resume_created_defence_run=arguments.resume_created_defence_run,
            resume_v4_pre_model_source=arguments.recover_v4_pre_model_source,
            resume_m1_pre_model_source=arguments.recover_m1_pre_model_source,
            resume_m1_post_event_rule=arguments.recover_m1_post_event_rule,
            resume_m2_pre_model_source=arguments.recover_m2_pre_model_source,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except ManagedDefenceAcceptanceBlocked as error:
        print(
            json.dumps(
                {
                    "status": "BLOCKED_SYNTHETIC_MANAGED_DEFENCE_ACCEPTANCE",
                    "reason": str(error),
                    "automatic_retry": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "FAILED_SYNTHETIC_MANAGED_DEFENCE_ACCEPTANCE",
                    "reason": "服务端验收发生未分类故障；持久状态已保留，未创建自动重试。",
                    "automatic_retry": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    finally:
        if session_id is not None and composition is not None:
            try:
                composition.api_dependencies.session_authority.revoke(
                    session_id=session_id
                )
            except Exception:
                # The synthetic run must never hide a durable acceptance result
                # because a short-lived local session could not be revoked.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
