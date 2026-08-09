"""Guarded PostgreSQL persistence for official sources and case legal bundles."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
)
from .legal_rules import LegalEventKind
from .models import Actor, Role


_OFFICIAL_SOURCE_DOMAINS = frozenset(
    {
        "flk.npc.gov.cn",
        "wb.flk.npc.gov.cn",
        "court.gov.cn",
        "www.court.gov.cn",
        "gongbao.court.gov.cn",
        "chinamoney.com.cn",
        "www.chinamoney.com.cn",
    }
)
_OBJECT_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/([0-9a-f]{64})\.lca$")


class LegalAuthorityLevel(str, Enum):
    PRIMARY_LAW = "PRIMARY_LAW"
    JUDICIAL_INTERPRETATION = "JUDICIAL_INTERPRETATION"
    OFFICIAL_RATE_DATA = "OFFICIAL_RATE_DATA"
    OFFICIAL_CASE = "OFFICIAL_CASE"


class LegalRateFormulaKind(str, Enum):
    FIXED_ANNUAL_RATE = "FIXED_ANNUAL_RATE"
    LPR_MULTIPLE = "LPR_MULTIPLE"
    NO_INTEREST = "NO_INTEREST"


@dataclass(frozen=True)
class LegalBundleSegmentSelection:
    segment_id: str
    issue_key: str
    rule_version_id: str
    trigger_event_id: str
    start_date: date
    end_date: date
    applicability_anchor: str


@dataclass(frozen=True)
class PersistentLegalReviewSnapshot:
    matter_id: str
    matter_version: int
    sources: tuple[dict[str, Any], ...]
    rule_versions: tuple[dict[str, Any], ...]
    legal_events: tuple[dict[str, Any], ...]
    fact_bindings: tuple[dict[str, Any], ...]
    current_bundle: dict[str, Any] | None
    bundle_segments: tuple[dict[str, Any], ...]
    snapshot_hash: str


class PostgresLegalSourceStore:
    _LEAD_ROLES = frozenset({Role.LEAD_LAWYER})
    _SOURCE_REVIEW_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
    _READ_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )

    def __init__(
        self,
        dsn: str,
        *,
        official_source_reader: Callable[[str, str], bytes] | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._official_source_reader = official_source_reader

    def register_official_source_snapshot(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        source_id: str,
        publisher: str,
        authority_level: LegalAuthorityLevel,
        official_url: str,
        provision_locator: str,
        retrieved_at: datetime,
        content_sha256: str,
        content_media_type: str,
        storage_object_key: str,
        verification_hash: str,
        supersedes_snapshot_id: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._SOURCE_REVIEW_ROLES)
        for value, name in (
            (source_id, "source_id"),
            (publisher, "publisher"),
            (provision_locator, "provision_locator"),
            (content_media_type, "content_media_type"),
        ):
            _require_text(value, name)
        _validate_official_url(official_url)
        _validate_sha256("content_sha256", content_sha256)
        _validate_sha256("verification_hash", verification_hash)
        object_match = _OBJECT_KEY.fullmatch(storage_object_key)
        if object_match is None or object_match.group(1) != content_sha256:
            raise CaseLedgerPersistenceBlocked(
                "official source snapshot must use its encrypted content-addressed object"
            )
        if retrieved_at.tzinfo is None:
            raise CaseLedgerPersistenceBlocked("official source retrieval time must include a timezone")
        if self._official_source_reader is None:
            raise CaseLedgerPersistenceBlocked(
                "official source registration requires a configured encrypted-object verifier"
            )
        try:
            source_bytes = self._official_source_reader(storage_object_key, content_sha256)
        except Exception as error:
            raise CaseLedgerPersistenceBlocked(
                "official source encrypted object could not be authenticated"
            ) from error
        if not source_bytes or sha256(source_bytes).hexdigest() != content_sha256:
            raise CaseLedgerPersistenceBlocked(
                "official source encrypted object does not match its declared plaintext hash"
            )
        if supersedes_snapshot_id is not None:
            _validate_uuid("supersedes_snapshot_id", supersedes_snapshot_id)
        snapshot_id = str(uuid4())
        command_name = "REGISTER_OFFICIAL_LEGAL_SOURCE_SNAPSHOT"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "source_id": source_id.strip(),
            "publisher": publisher.strip(),
            "authority_level": authority_level,
            "official_url": official_url,
            "provision_locator": provision_locator.strip(),
            "retrieved_at": retrieved_at,
            "content_sha256": content_sha256,
            "content_media_type": content_media_type.strip(),
            "storage_object_key": storage_object_key,
            "verification_hash": verification_hash,
            "supersedes_snapshot_id": supersedes_snapshot_id,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._SOURCE_REVIEW_ROLES,
            )
            if prior is not None:
                return prior
            if supersedes_snapshot_id is not None:
                prior_source = connection.execute(
                    """
                    SELECT source_id, verification_status, license_status
                    FROM official_legal_source_snapshots
                    WHERE snapshot_id = %s AND firm_id = %s FOR UPDATE
                    """,
                    (supersedes_snapshot_id, actor.firm_id),
                ).fetchone()
                if prior_source is None:
                    raise KeyError(supersedes_snapshot_id)
                if prior_source["source_id"] != source_id.strip():
                    raise CaseLedgerPersistenceBlocked("a source snapshot may supersede only the same source")
                impacted_rows = connection.execute(
                    """
                    SELECT segment.matter_id
                    FROM case_legal_bundle_segments segment
                    JOIN case_legal_bundles bundle
                      ON bundle.bundle_id = segment.bundle_id
                     AND bundle.firm_id = segment.firm_id
                     AND bundle.matter_id = segment.matter_id
                    WHERE segment.firm_id = %s AND bundle.status = 'APPROVED'
                      AND (segment.source_snapshot_id = %s
                           OR segment.parameter_source_snapshot_id = %s)
                    FOR SHARE OF bundle
                    """,
                    (actor.firm_id, supersedes_snapshot_id, supersedes_snapshot_id),
                ).fetchall()
                impacted_matters = {str(row["matter_id"]) for row in impacted_rows}
                if impacted_matters - {matter_id}:
                    raise CaseLedgerPersistenceBlocked(
                        "source replacement affects approved bundles in other matters and requires bulk impact review"
                    )
                connection.execute(
                    """
                    UPDATE official_legal_source_snapshots
                    SET verification_status = 'SUPERSEDED'
                    WHERE snapshot_id = %s AND firm_id = %s
                    """,
                    (supersedes_snapshot_id, actor.firm_id),
                )
                connection.execute(
                    """
                    UPDATE legal_rule_versions
                    SET status = 'SUPERSEDED'
                    WHERE (source_snapshot_id = %s OR parameter_source_snapshot_id = %s)
                      AND firm_id = %s AND status = 'APPROVED'
                    """,
                    (supersedes_snapshot_id, supersedes_snapshot_id, actor.firm_id),
                )
            connection.execute(
                """
                INSERT INTO official_legal_source_snapshots (
                    snapshot_id, firm_id, source_id, publisher, authority_level,
                    official_url, provision_locator, retrieved_at, content_sha256,
                    content_media_type, storage_object_key, verification_status,
                    license_status, verified_by, verification_hash, supersedes_snapshot_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'VERIFIED', 'ACTIVE', %s, %s, %s)
                """,
                (
                    snapshot_id,
                    actor.firm_id,
                    source_id.strip(),
                    publisher.strip(),
                    authority_level.value,
                    official_url,
                    provision_locator.strip(),
                    retrieved_at,
                    content_sha256,
                    content_media_type.strip(),
                    storage_object_key,
                    actor.actor_id,
                    verification_hash,
                    supersedes_snapshot_id,
                ),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="OFFICIAL_LEGAL_SOURCE_SNAPSHOT_REGISTERED",
                object_type="OFFICIAL_LEGAL_SOURCE_SNAPSHOT",
                object_id=snapshot_id,
                audit_payload={
                    "snapshot_id": snapshot_id,
                    "source_id": source_id.strip(),
                    "official_url": official_url,
                    "content_sha256": content_sha256,
                    "verification_hash": verification_hash,
                },
                stale_submission=True,
            )

    def approve_rule_version(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        rule_id: str,
        rule_version: str,
        issue_key: str,
        source_snapshot_id: str,
        parameter_source_snapshot_id: str | None,
        parameter_evidence_locator: str | None,
        effective_from: date,
        effective_to: date | None,
        trigger_event_kind: LegalEventKind,
        formula_kind: LegalRateFormulaKind,
        base_annual_rate: Decimal | None,
        rate_multiplier: Decimal | None,
        required_fact_keys: tuple[str, ...],
        transition_rule_versions: tuple[str, ...],
        conflict_set: str | None,
        priority: int,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._LEAD_ROLES)
        for value, name in ((rule_id, "rule_id"), (rule_version, "rule_version"), (issue_key, "issue_key")):
            _require_text(value, name)
        _validate_uuid("source_snapshot_id", source_snapshot_id)
        if formula_kind is LegalRateFormulaKind.LPR_MULTIPLE:
            if parameter_source_snapshot_id is None or not parameter_evidence_locator:
                raise CaseLedgerPersistenceBlocked(
                    "LPR multiple rule requires an official rate snapshot and exact rate locator"
                )
            _validate_uuid("parameter_source_snapshot_id", parameter_source_snapshot_id)
            _require_text(parameter_evidence_locator, "parameter_evidence_locator")
            if parameter_source_snapshot_id == source_snapshot_id:
                raise CaseLedgerPersistenceBlocked(
                    "legal authority and LPR parameter must use distinct official source snapshots"
                )
        elif parameter_source_snapshot_id is not None or parameter_evidence_locator is not None:
            raise CaseLedgerPersistenceBlocked(
                "only an LPR multiple rule may carry an external rate parameter source"
            )
        _validate_sha256("approval_hash", approval_hash)
        if effective_to is not None and effective_from >= effective_to:
            raise CaseLedgerPersistenceBlocked("legal rule effective interval must be non-empty")
        if priority < 0:
            raise CaseLedgerPersistenceBlocked("legal rule priority cannot be negative")
        derived_rate = _derive_rate(formula_kind, base_annual_rate, rate_multiplier)
        normalized_required = _unique_texts(required_fact_keys, "required_fact_keys")
        normalized_transitions = _unique_texts(transition_rule_versions, "transition_rule_versions")
        rule_version_id = str(uuid4())
        command_name = "APPROVE_LEGAL_RULE_VERSION"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "rule_id": rule_id.strip(),
            "rule_version": rule_version.strip(),
            "issue_key": issue_key.strip(),
            "source_snapshot_id": source_snapshot_id,
            "parameter_source_snapshot_id": parameter_source_snapshot_id,
            "parameter_evidence_locator": (
                parameter_evidence_locator.strip() if parameter_evidence_locator else None
            ),
            "effective_from": effective_from,
            "effective_to": effective_to,
            "trigger_event_kind": trigger_event_kind,
            "formula_kind": formula_kind,
            "base_annual_rate": base_annual_rate,
            "rate_multiplier": rate_multiplier,
            "derived_annual_rate": derived_rate,
            "required_fact_keys": normalized_required,
            "transition_rule_versions": normalized_transitions,
            "conflict_set": conflict_set.strip() if conflict_set else None,
            "priority": priority,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._LEAD_ROLES,
            )
            if prior is not None:
                return prior
            source = connection.execute(
                """
                SELECT content_sha256, verification_status, license_status, authority_level
                FROM official_legal_source_snapshots
                WHERE snapshot_id = %s AND firm_id = %s FOR SHARE
                """,
                (source_snapshot_id, actor.firm_id),
            ).fetchone()
            if source is None:
                raise KeyError(source_snapshot_id)
            if source["verification_status"] != "VERIFIED" or source["license_status"] != "ACTIVE":
                raise CaseLedgerPersistenceBlocked("legal rule requires a verified active official source snapshot")
            if formula_kind is LegalRateFormulaKind.LPR_MULTIPLE and source["authority_level"] not in {
                LegalAuthorityLevel.PRIMARY_LAW.value,
                LegalAuthorityLevel.JUDICIAL_INTERPRETATION.value,
            }:
                raise CaseLedgerPersistenceBlocked(
                    "LPR multiple rule requires a primary-law or judicial-interpretation authority source"
                )
            parameter_source = None
            if parameter_source_snapshot_id is not None:
                parameter_source = connection.execute(
                    """
                    SELECT content_sha256, verification_status, license_status, authority_level
                    FROM official_legal_source_snapshots
                    WHERE snapshot_id = %s AND firm_id = %s FOR SHARE
                    """,
                    (parameter_source_snapshot_id, actor.firm_id),
                ).fetchone()
                if parameter_source is None:
                    raise KeyError(parameter_source_snapshot_id)
                if (
                    parameter_source["verification_status"] != "VERIFIED"
                    or parameter_source["license_status"] != "ACTIVE"
                    or parameter_source["authority_level"]
                    != LegalAuthorityLevel.OFFICIAL_RATE_DATA.value
                ):
                    raise CaseLedgerPersistenceBlocked(
                        "LPR parameter requires a verified active official rate-data snapshot"
                    )
            connection.execute(
                """
                INSERT INTO legal_rule_versions (
                    rule_version_id, firm_id, rule_id, rule_version, issue_key,
                    source_snapshot_id, parameter_source_snapshot_id,
                    parameter_evidence_locator, effective_from, effective_to, trigger_event_kind,
                    formula_kind, base_annual_rate, rate_multiplier, derived_annual_rate,
                    required_fact_keys, transition_rule_versions, conflict_set, priority,
                    status, approved_by, approval_hash, approved_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, 'APPROVED', %s, %s, now())
                """,
                (
                    rule_version_id,
                    actor.firm_id,
                    rule_id.strip(),
                    rule_version.strip(),
                    issue_key.strip(),
                    source_snapshot_id,
                    parameter_source_snapshot_id,
                    parameter_evidence_locator.strip() if parameter_evidence_locator else None,
                    effective_from,
                    effective_to,
                    trigger_event_kind.value,
                    formula_kind.value,
                    base_annual_rate,
                    rate_multiplier,
                    derived_rate,
                    json.dumps(normalized_required, ensure_ascii=False),
                    json.dumps(normalized_transitions, ensure_ascii=False),
                    conflict_set.strip() if conflict_set else None,
                    priority,
                    actor.actor_id,
                    approval_hash,
                ),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="LEGAL_RULE_VERSION_APPROVED",
                object_type="LEGAL_RULE_VERSION",
                object_id=rule_version_id,
                audit_payload={
                    "rule_version_id": rule_version_id,
                    "rule_version": rule_version.strip(),
                    "source_snapshot_id": source_snapshot_id,
                    "parameter_source_snapshot_id": parameter_source_snapshot_id,
                    "parameter_source_sha256": (
                        parameter_source["content_sha256"] if parameter_source else None
                    ),
                    "derived_annual_rate": format(derived_rate, "f"),
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def approve_legal_event(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        event_kind: LegalEventKind,
        local_date: date,
        evidence_ids: tuple[str, ...],
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._LEAD_ROLES)
        _validate_sha256("approval_hash", approval_hash)
        normalized_evidence = _unique_texts(evidence_ids, "evidence_ids")
        if not normalized_evidence:
            raise CaseLedgerPersistenceBlocked("approved legal event requires evidence")
        for evidence_page_id in normalized_evidence:
            _validate_uuid("evidence_page_id", evidence_page_id)
        legal_event_id = str(uuid4())
        command_name = "APPROVE_CASE_LEGAL_EVENT"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "event_kind": event_kind,
            "local_date": local_date,
            "evidence_ids": normalized_evidence,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._LEAD_ROLES,
            )
            if prior is not None:
                return prior
            evidence_rows = connection.execute(
                """
                SELECT evidence_page_id
                FROM evidence_pages
                WHERE evidence_page_id = ANY(%s)
                  AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (list(normalized_evidence), matter_id, actor.firm_id),
            ).fetchall()
            available_evidence = {
                str(row["evidence_page_id"]) for row in evidence_rows
            }
            missing_evidence = sorted(set(normalized_evidence) - available_evidence)
            if missing_evidence:
                raise CaseLedgerPersistenceBlocked(
                    "approved legal event evidence pages must all belong to the same matter: "
                    + ", ".join(missing_evidence)
                )
            connection.execute(
                """
                INSERT INTO case_legal_events (
                    legal_event_id, firm_id, matter_id, event_kind, local_date,
                    status, approved_by, approval_hash, approved_at
                ) VALUES (%s, %s, %s, %s, %s, 'APPROVED', %s, %s, now())
                """,
                (
                    legal_event_id,
                    actor.firm_id,
                    matter_id,
                    event_kind.value,
                    local_date,
                    actor.actor_id,
                    approval_hash,
                ),
            )
            for evidence_page_id in normalized_evidence:
                connection.execute(
                    """
                    INSERT INTO case_legal_event_evidence_pages (
                        legal_event_id, evidence_page_id, firm_id, matter_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (legal_event_id, evidence_page_id, actor.firm_id, matter_id),
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="CASE_LEGAL_EVENT_APPROVED",
                object_type="CASE_LEGAL_EVENT",
                object_id=legal_event_id,
                audit_payload={
                    "legal_event_id": legal_event_id,
                    "event_kind": event_kind.value,
                    "local_date": local_date.isoformat(),
                    "evidence_page_ids": normalized_evidence,
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def approve_legal_fact_binding(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        fact_key: str,
        fact_id: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._LEAD_ROLES
        )
        _require_text(fact_key, "fact_key")
        _validate_uuid("fact_id", fact_id)
        _validate_sha256("approval_hash", approval_hash)
        normalized_key = fact_key.strip()
        binding_id = str(uuid4())
        command_name = "APPROVE_CASE_LEGAL_FACT_BINDING"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "fact_key": normalized_key,
            "fact_id": fact_id,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._LEAD_ROLES,
            )
            if prior is not None:
                return prior
            fact = connection.execute(
                """
                SELECT status, decision_hash
                FROM case_facts
                WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (fact_id, matter_id, actor.firm_id),
            ).fetchone()
            if fact is None:
                raise KeyError(fact_id)
            if fact["status"] != "CONFIRMED" or not fact["decision_hash"]:
                raise CaseLedgerPersistenceBlocked(
                    "legal fact binding requires a confirmed, human-decided case fact"
                )
            connection.execute(
                """
                UPDATE case_legal_fact_bindings
                SET status = 'STALE', stale_at = now(),
                    stale_reason = '同一法律事实键已绑定至新的确认事实。'
                WHERE matter_id = %s AND firm_id = %s AND fact_key = %s
                  AND status = 'APPROVED'
                """,
                (matter_id, actor.firm_id, normalized_key),
            )
            connection.execute(
                """
                INSERT INTO case_legal_fact_bindings (
                    binding_id, firm_id, matter_id, fact_key, fact_id, status,
                    approved_by, approval_hash, approved_at
                ) VALUES (%s, %s, %s, %s, %s, 'APPROVED', %s, %s, now())
                """,
                (
                    binding_id,
                    actor.firm_id,
                    matter_id,
                    normalized_key,
                    fact_id,
                    actor.actor_id,
                    approval_hash,
                ),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="CASE_LEGAL_FACT_BINDING_APPROVED",
                object_type="CASE_LEGAL_FACT_BINDING",
                object_id=binding_id,
                audit_payload={
                    "binding_id": binding_id,
                    "fact_key": normalized_key,
                    "fact_id": fact_id,
                    "fact_decision_hash": fact["decision_hash"],
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def approve_case_legal_bundle(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        segments: tuple[LegalBundleSegmentSelection, ...],
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._LEAD_ROLES)
        _validate_sha256("approval_hash", approval_hash)
        normalized_segments = _validate_segment_selections(segments)
        command_name = "APPROVE_CASE_LEGAL_BUNDLE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "segments": tuple(asdict(segment) for segment in normalized_segments),
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._LEAD_ROLES,
            )
            if prior is not None:
                return prior
            rule_rows = connection.execute(
                """
                SELECT rule.rule_version_id, rule.rule_version, rule.issue_key,
                       rule.effective_from, rule.effective_to, rule.trigger_event_kind,
                       rule.derived_annual_rate, rule.status AS rule_status,
                       rule.required_fact_keys, rule.formula_kind,
                       rule.source_snapshot_id, rule.approval_hash AS rule_approval_hash,
                       rule.parameter_source_snapshot_id, rule.parameter_evidence_locator,
                       source.content_sha256, source.verification_status, source.license_status
                FROM legal_rule_versions rule
                JOIN official_legal_source_snapshots source
                  ON source.snapshot_id = rule.source_snapshot_id AND source.firm_id = rule.firm_id
                WHERE rule.rule_version_id = ANY(%s) AND rule.firm_id = %s
                FOR SHARE OF rule, source
                """,
                ([segment.rule_version_id for segment in normalized_segments], actor.firm_id),
            ).fetchall()
            rules = {str(row["rule_version_id"]): row for row in rule_rows}
            if len(rules) != len({segment.rule_version_id for segment in normalized_segments}):
                raise CaseLedgerPersistenceBlocked("one or more selected legal rule versions are unavailable")
            parameter_source_ids = {
                str(rule["parameter_source_snapshot_id"])
                for rule in rules.values()
                if rule["parameter_source_snapshot_id"] is not None
            }
            parameter_sources: dict[str, dict[str, Any]] = {}
            if parameter_source_ids:
                parameter_rows = connection.execute(
                    """
                    SELECT snapshot_id, content_sha256, verification_status,
                           license_status, authority_level
                    FROM official_legal_source_snapshots
                    WHERE snapshot_id = ANY(%s) AND firm_id = %s
                    FOR SHARE
                    """,
                    (sorted(parameter_source_ids), actor.firm_id),
                ).fetchall()
                parameter_sources = {
                    str(row["snapshot_id"]): row for row in parameter_rows
                }
                if len(parameter_sources) != len(parameter_source_ids):
                    raise CaseLedgerPersistenceBlocked(
                        "one or more selected rate parameter sources are unavailable"
                    )
            for rule in rules.values():
                parameter_id = rule["parameter_source_snapshot_id"]
                parameter = (
                    parameter_sources[str(parameter_id)] if parameter_id is not None else None
                )
                rule["parameter_content_sha256"] = (
                    parameter["content_sha256"] if parameter else None
                )
                rule["parameter_verification_status"] = (
                    parameter["verification_status"] if parameter else None
                )
                rule["parameter_license_status"] = (
                    parameter["license_status"] if parameter else None
                )
                rule["parameter_authority_level"] = (
                    parameter["authority_level"] if parameter else None
                )
            required_fact_keys = {
                fact_key
                for rule in rules.values()
                for fact_key in (rule["required_fact_keys"] or [])
            }
            if required_fact_keys:
                binding_rows = connection.execute(
                    """
                    SELECT binding.fact_key, binding.fact_id,
                           binding.status AS binding_status,
                           fact.status AS fact_status, fact.decision_hash
                    FROM case_legal_fact_bindings binding
                    JOIN case_facts fact
                      ON fact.fact_id = binding.fact_id
                     AND fact.firm_id = binding.firm_id
                     AND fact.matter_id = binding.matter_id
                    WHERE binding.matter_id = %s AND binding.firm_id = %s
                      AND binding.fact_key = ANY(%s)
                      AND binding.status = 'APPROVED'
                    FOR SHARE
                    """,
                    (matter_id, actor.firm_id, sorted(required_fact_keys)),
                ).fetchall()
                valid_fact_keys = {
                    row["fact_key"]
                    for row in binding_rows
                    if row["binding_status"] == "APPROVED"
                    and row["fact_status"] == "CONFIRMED"
                    and row["decision_hash"]
                }
                missing_fact_keys = sorted(required_fact_keys - valid_fact_keys)
                if missing_fact_keys:
                    raise CaseLedgerPersistenceBlocked(
                        "legal rule prerequisites are not bound to confirmed facts: "
                        + ", ".join(missing_fact_keys)
                    )
            event_rows = connection.execute(
                """
                SELECT legal_event_id, event_kind, local_date, status
                FROM case_legal_events
                WHERE legal_event_id = ANY(%s) AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                ([segment.trigger_event_id for segment in normalized_segments], matter_id, actor.firm_id),
            ).fetchall()
            events = {str(row["legal_event_id"]): row for row in event_rows}
            if len(events) != len({segment.trigger_event_id for segment in normalized_segments}):
                raise CaseLedgerPersistenceBlocked("one or more selected legal trigger events are unavailable")
            resolved: list[dict[str, Any]] = []
            for segment in normalized_segments:
                rule = rules[segment.rule_version_id]
                event = events[segment.trigger_event_id]
                if rule["rule_status"] != "APPROVED":
                    raise CaseLedgerPersistenceBlocked("selected legal rule version is not current")
                if rule["verification_status"] != "VERIFIED" or rule["license_status"] != "ACTIVE":
                    raise CaseLedgerPersistenceBlocked("selected legal rule source is not verified and active")
                if rule["formula_kind"] == LegalRateFormulaKind.LPR_MULTIPLE.value and (
                    rule["parameter_verification_status"] != "VERIFIED"
                    or rule["parameter_license_status"] != "ACTIVE"
                    or rule["parameter_authority_level"]
                    != LegalAuthorityLevel.OFFICIAL_RATE_DATA.value
                ):
                    raise CaseLedgerPersistenceBlocked(
                        "selected LPR rule parameter source is not verified official rate data"
                    )
                if rule["issue_key"] != segment.issue_key:
                    raise CaseLedgerPersistenceBlocked("bundle issue key does not match the selected rule version")
                if event["status"] != "APPROVED" or event["event_kind"] != rule["trigger_event_kind"]:
                    raise CaseLedgerPersistenceBlocked("selected trigger event does not satisfy the legal rule")
                if event["local_date"] < rule["effective_from"] or (
                    rule["effective_to"] is not None and event["local_date"] >= rule["effective_to"]
                ):
                    raise CaseLedgerPersistenceBlocked("selected trigger event falls outside the legal rule period")
                resolved.append({"selection": segment, "rule": rule, "event": event})
            bundle_version_row = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                FROM case_legal_bundles WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            bundle_version = bundle_version_row["next_version"]
            canonical = tuple(
                {
                    **asdict(item["selection"]),
                    "rule_version": item["rule"]["rule_version"],
                    "source_snapshot_id": str(item["rule"]["source_snapshot_id"]),
                    "source_sha256": item["rule"]["content_sha256"],
                    "parameter_source_snapshot_id": (
                        str(item["rule"]["parameter_source_snapshot_id"])
                        if item["rule"]["parameter_source_snapshot_id"] is not None
                        else None
                    ),
                    "parameter_source_sha256": item["rule"]["parameter_content_sha256"],
                    "parameter_evidence_locator": item["rule"]["parameter_evidence_locator"],
                    "annual_rate": Decimal(item["rule"]["derived_annual_rate"]),
                    "trigger_date": item["event"]["local_date"],
                }
                for item in resolved
            )
            input_hash = _payload_hash(
                {"matter_id": matter_id, "version": bundle_version, "segments": canonical}
            )
            bundle_hash = _payload_hash(
                {
                    "matter_id": matter_id,
                    "version": bundle_version,
                    "input_hash": input_hash,
                    "approval_hash": approval_hash,
                    "approved_by": actor.actor_id,
                }
            )
            stale_reason = "新的案件法律规则包已获批准。"
            connection.execute(
                """
                UPDATE calculation_runs
                SET status = 'STALE', stale_at = now(), stale_reason = %s
                WHERE matter_id = %s AND firm_id = %s AND status = 'VERIFIED'
                """,
                (stale_reason, matter_id, actor.firm_id),
            )
            connection.execute(
                """
                UPDATE calculation_scenarios
                SET status = 'STALE', stale_at = now(), stale_reason = %s
                WHERE matter_id = %s AND firm_id = %s AND status = 'APPROVED'
                """,
                (stale_reason, matter_id, actor.firm_id),
            )
            connection.execute(
                """
                UPDATE case_legal_bundles
                SET status = 'STALE', stale_at = now(), stale_reason = %s
                WHERE matter_id = %s AND firm_id = %s AND status = 'APPROVED'
                """,
                (stale_reason, matter_id, actor.firm_id),
            )
            bundle_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_legal_bundles (
                    bundle_id, firm_id, matter_id, version, status, input_hash,
                    bundle_hash, approved_by, approval_hash, approved_at
                ) VALUES (%s, %s, %s, %s, 'APPROVED', %s, %s, %s, %s, now())
                """,
                (
                    bundle_id,
                    actor.firm_id,
                    matter_id,
                    bundle_version,
                    input_hash,
                    bundle_hash,
                    actor.actor_id,
                    approval_hash,
                ),
            )
            seen_rules: set[str] = set()
            for item in resolved:
                segment = item["selection"]
                rule = item["rule"]
                event = item["event"]
                if segment.rule_version_id not in seen_rules:
                    connection.execute(
                        """
                        INSERT INTO case_legal_bundle_rule_versions (
                            bundle_id, firm_id, matter_id, issue_key, rule_version,
                            source_snapshot_id, source_sha256, trigger_event_id, trigger_date
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            bundle_id,
                            actor.firm_id,
                            matter_id,
                            segment.issue_key,
                            rule["rule_version"],
                            str(rule["source_snapshot_id"]),
                            rule["content_sha256"],
                            segment.trigger_event_id,
                            event["local_date"],
                        ),
                    )
                    seen_rules.add(segment.rule_version_id)
                connection.execute(
                    """
                    INSERT INTO case_legal_bundle_segments (
                        bundle_id, segment_id, firm_id, matter_id, issue_key,
                        rule_version_id, rule_version, source_snapshot_id, source_sha256,
                        parameter_source_snapshot_id, parameter_source_sha256,
                        parameter_evidence_locator, trigger_event_id,
                        start_date, end_date, annual_rate,
                        applicability_anchor, approval_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        bundle_id,
                        segment.segment_id,
                        actor.firm_id,
                        matter_id,
                        segment.issue_key,
                        segment.rule_version_id,
                        rule["rule_version"],
                        str(rule["source_snapshot_id"]),
                        rule["content_sha256"],
                        (
                            str(rule["parameter_source_snapshot_id"])
                            if rule["parameter_source_snapshot_id"] is not None
                            else None
                        ),
                        rule["parameter_content_sha256"],
                        rule["parameter_evidence_locator"],
                        segment.trigger_event_id,
                        segment.start_date,
                        segment.end_date,
                        Decimal(rule["derived_annual_rate"]),
                        segment.applicability_anchor.strip(),
                        approval_hash,
                    ),
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="CASE_LEGAL_BUNDLE_APPROVED",
                object_type="CASE_LEGAL_BUNDLE",
                object_id=bundle_id,
                audit_payload={
                    "bundle_id": bundle_id,
                    "version": bundle_version,
                    "input_hash": input_hash,
                    "bundle_hash": bundle_hash,
                    "segment_count": len(resolved),
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
                stale_calculations=False,
            )

    def get_legal_review_snapshot(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> PersistentLegalReviewSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter = connection.execute(
                "SELECT version FROM matters WHERE matter_id = %s AND firm_id = %s",
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            sources = connection.execute(
                """
                SELECT snapshot_id, source_id, publisher, authority_level,
                       official_url, provision_locator, retrieved_at, content_sha256,
                       content_media_type, verification_status, license_status,
                       verified_by, verification_hash, supersedes_snapshot_id
                FROM official_legal_source_snapshots
                WHERE firm_id = %s
                ORDER BY source_id, retrieved_at DESC
                """,
                (actor.firm_id,),
            ).fetchall()
            rules = connection.execute(
                """
                SELECT rule_version_id, rule_id, rule_version, issue_key,
                       source_snapshot_id, parameter_source_snapshot_id,
                       parameter_evidence_locator, effective_from, effective_to,
                       trigger_event_kind, formula_kind, base_annual_rate,
                       rate_multiplier, derived_annual_rate, required_fact_keys,
                       transition_rule_versions, conflict_set, priority, status,
                       approved_by, approval_hash, approved_at
                FROM legal_rule_versions
                WHERE firm_id = %s
                ORDER BY issue_key, priority DESC, effective_from, rule_version
                """,
                (actor.firm_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT event.legal_event_id, event.event_kind, event.local_date,
                       ARRAY(
                           SELECT link.evidence_page_id::text
                           FROM case_legal_event_evidence_pages link
                           WHERE link.legal_event_id = event.legal_event_id
                             AND link.firm_id = event.firm_id
                             AND link.matter_id = event.matter_id
                           ORDER BY link.evidence_page_id
                       ) AS evidence_ids,
                       status, approved_by, approval_hash, approved_at
                FROM case_legal_events event
                WHERE event.matter_id = %s AND event.firm_id = %s
                ORDER BY event.local_date, event.event_kind, event.legal_event_id
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            fact_bindings = connection.execute(
                """
                SELECT binding_id, fact_key, fact_id, status, approved_by,
                       approval_hash, approved_at, stale_at, stale_reason
                FROM case_legal_fact_bindings
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY fact_key, approved_at DESC, binding_id
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            bundle = connection.execute(
                """
                SELECT bundle_id, version, input_hash, bundle_hash,
                       approved_by, approval_hash, approved_at
                FROM case_legal_bundles
                WHERE matter_id = %s AND firm_id = %s AND status = 'APPROVED'
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            segments = []
            if bundle is not None:
                segments = connection.execute(
                    """
                    SELECT segment_id, issue_key, rule_version_id, rule_version,
                           source_snapshot_id, source_sha256,
                           parameter_source_snapshot_id, parameter_source_sha256,
                           parameter_evidence_locator, trigger_event_id,
                           start_date, end_date, annual_rate, applicability_anchor,
                           approval_hash
                    FROM case_legal_bundle_segments
                    WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                    ORDER BY start_date, segment_id
                    """,
                    (bundle["bundle_id"], matter_id, actor.firm_id),
                ).fetchall()
        payload = {
            "matter_id": matter_id,
            "matter_version": matter["version"],
            "sources": tuple(_serialize_legal_row(row) for row in sources),
            "rule_versions": tuple(_serialize_legal_row(row) for row in rules),
            "legal_events": tuple(_serialize_legal_row(row) for row in events),
            "fact_bindings": tuple(_serialize_legal_row(row) for row in fact_bindings),
            "current_bundle": _serialize_legal_row(bundle) if bundle is not None else None,
            "bundle_segments": tuple(_serialize_legal_row(row) for row in segments),
        }
        return PersistentLegalReviewSnapshot(snapshot_hash=_payload_hash(payload), **payload)

    @staticmethod
    def _validate_command(
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        roles: frozenset[Role],
    ) -> None:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, roles)
        _require_positive_version(expected_version)

    @staticmethod
    def _begin(
        connection: psycopg.Connection,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        command_name: str,
        payload_hash: str,
        allowed_roles: frozenset[Role],
    ) -> CaseLedgerCommandReceipt | None:
        _advisory_lock(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
        )
        prior = _prior_receipt(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if prior is not None:
            return prior
        _authorize_and_lock_matter(
            connection,
            actor=actor,
            matter_id=matter_id,
            expected_version=expected_version,
            allowed_roles=allowed_roles,
        )
        return None

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _validate_official_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_SOURCE_DOMAINS:
        raise CaseLedgerPersistenceBlocked("formal legal sources require a registered official HTTPS domain")
    if parsed.username or parsed.password or parsed.fragment:
        raise CaseLedgerPersistenceBlocked("official source URL contains unsupported credentials or fragment")


def _derive_rate(
    formula_kind: LegalRateFormulaKind,
    base_annual_rate: Decimal | None,
    rate_multiplier: Decimal | None,
) -> Decimal:
    if formula_kind is LegalRateFormulaKind.NO_INTEREST:
        if base_annual_rate is not None or rate_multiplier is not None:
            raise CaseLedgerPersistenceBlocked("NO_INTEREST cannot carry rate operands")
        return Decimal("0")
    if base_annual_rate is None:
        raise CaseLedgerPersistenceBlocked("rate formula requires a base annual rate")
    if base_annual_rate < 0 or base_annual_rate > 1:
        raise CaseLedgerPersistenceBlocked("base annual rate must be between zero and one")
    if formula_kind is LegalRateFormulaKind.FIXED_ANNUAL_RATE:
        if rate_multiplier is not None:
            raise CaseLedgerPersistenceBlocked("fixed annual rate cannot carry a multiplier")
        return base_annual_rate
    if rate_multiplier is None or rate_multiplier <= 0:
        raise CaseLedgerPersistenceBlocked("LPR multiple formula requires a positive multiplier")
    derived = base_annual_rate * rate_multiplier
    if derived > 1:
        raise CaseLedgerPersistenceBlocked("derived annual rate exceeds the v1 rate domain")
    return derived


def _unique_texts(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip() for value in values if value.strip()}))
    if len(normalized) != len(values):
        raise CaseLedgerPersistenceBlocked(f"{field_name} must be non-empty and unique")
    return normalized


def _validate_segment_selections(
    values: tuple[LegalBundleSegmentSelection, ...],
) -> tuple[LegalBundleSegmentSelection, ...]:
    if not values:
        raise CaseLedgerPersistenceBlocked("case legal bundle requires at least one calculation segment")
    for item in values:
        _validate_uuid("segment_id", item.segment_id)
        _validate_uuid("rule_version_id", item.rule_version_id)
        _validate_uuid("trigger_event_id", item.trigger_event_id)
        _require_text(item.issue_key, "issue_key")
        _require_text(item.applicability_anchor, "applicability_anchor")
        if item.start_date >= item.end_date:
            raise CaseLedgerPersistenceBlocked("legal bundle segment interval must be non-empty")
    if len({item.segment_id for item in values}) != len(values):
        raise CaseLedgerPersistenceBlocked("legal bundle segment ids must be unique")
    ordered = tuple(sorted(values, key=lambda item: (item.start_date, item.end_date, item.segment_id)))
    for previous, current in zip(ordered, ordered[1:]):
        if previous.end_date != current.start_date:
            raise CaseLedgerPersistenceBlocked("legal bundle calculation segments must be continuous")
    return ordered


def _serialize_legal_row(row: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            serialized[key] = format(value, "f")
        elif isinstance(value, (date, datetime)):
            serialized[key] = value.isoformat()
        elif isinstance(value, list):
            serialized[key] = tuple(value)
        elif key.endswith("_id") and value is not None:
            serialized[key] = str(value)
        else:
            serialized[key] = value
    return serialized
