"""Persistent, versioned case posture facts for dynamic Agent planning.

The store records four different meanings instead of collapsing them into an
``is_plaintiff`` flag:

* the stable represented case party;
* the current court proceeding and its procedural stage;
* that party's position in that exact proceeding; and
* the firm's current engagement for that party and proceeding.

Only a lead lawyer may confirm a new meaning.  Confirmations append a version,
advance the matter version and preserve an idempotency/audit/outbox receipt in
one transaction.  A SYSTEM_WORKER may read the current profile but cannot write
it.  The profile is an input to a separate, law-and-record-aware planner; this
module intentionally contains no fixed material or work-product mapping.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any, Iterator
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
    _validate_command_identity,
    _validate_sha256,
    _validate_uuid,
)
from .models import Actor, Role


_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_CASE_TYPE_CODE = re.compile(r"^[A-Z][A-Z0-9_.]{1,127}$")


@dataclass(frozen=True)
class CasePartyConfirmation:
    party_kind: str
    display_label: str
    basis_hash: str


@dataclass(frozen=True)
class CourtProceedingConfirmation:
    forum_type: str
    case_type_code: str
    procedure_stage: str
    basis_hash: str


@dataclass(frozen=True)
class CourtPartyPositionConfirmation:
    proceeding_id: str
    party_id: str
    position_code: str
    basis_hash: str


@dataclass(frozen=True)
class FirmEngagementConfirmation:
    proceeding_id: str
    represented_party_id: str
    authority_scope_code: str
    engagement_state: str
    basis_hash: str


@dataclass(frozen=True)
class CasePostureProfileSnapshot:
    profile_id: str
    matter_id: str
    profile_version: int
    effective_status: str
    profile_hash: str
    represented_party_id: str
    represented_party_version_id: str
    represented_party_display_label: str
    represented_party_kind: str
    proceeding_id: str
    proceeding_version_id: str
    forum_type: str
    position_id: str
    position_version_id: str
    engagement_id: str
    engagement_version_id: str
    case_type_code: str
    procedure_stage: str
    represented_position: str
    authority_scope_code: str
    engagement_state: str
    confirmed_matter_version: int
    supersedes_profile_id: str | None
    confirmed_by: str


class PostgresCasePostureStore:
    """PostgreSQL 16+ adapter for confirmed posture facts and profiles."""

    _WRITE_ROLES = frozenset({Role.LEAD_LAWYER})
    _READ_ROLES = frozenset(
        {
            Role.ASSISTANT,
            Role.COLLABORATING_LAWYER,
            Role.LEAD_LAWYER,
            Role.REVIEWER,
            Role.FIRM_ADMIN,
            Role.SYSTEM_WORKER,
        }
    )

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def confirm_party(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        confirmation: CasePartyConfirmation,
        party_id: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        self._validate_write(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        normalized = CasePartyConfirmation(
            party_kind=_normalize_code("party_kind", confirmation.party_kind),
            display_label=_normalize_label(confirmation.display_label),
            basis_hash=_hash_value("party basis_hash", confirmation.basis_hash),
        )
        if party_id is not None:
            _validate_uuid("party_id", party_id)
        command = "CONFIRM_CASE_PARTY_VERSION"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "party_id": party_id,
            "party_kind": normalized.party_kind,
            "display_label": normalized.display_label,
            "basis_hash": normalized.basis_hash,
        }

        def apply(connection: psycopg.Connection) -> tuple[str, dict[str, Any]]:
            stable_id = party_id or str(uuid4())
            if party_id is None:
                connection.execute(
                    "INSERT INTO case_parties (party_id, firm_id, matter_id) VALUES (%s, %s, %s)",
                    (stable_id, actor.firm_id, matter_id),
                )
                next_version = 1
            else:
                head = connection.execute(
                    """
                    SELECT latest_version FROM case_party_heads
                    WHERE party_id = %s AND matter_id = %s AND firm_id = %s
                    FOR UPDATE
                    """,
                    (stable_id, matter_id, actor.firm_id),
                ).fetchone()
                if head is None:
                    raise KeyError(stable_id)
                next_version = int(head["latest_version"]) + 1
            version_id = str(uuid4())
            meaning_hash = _meaning_hash(
                "case-party-v1",
                actor.firm_id,
                matter_id,
                stable_id,
                next_version,
                normalized.party_kind,
                normalized.display_label,
                normalized.basis_hash,
            )
            connection.execute(
                """
                INSERT INTO case_party_versions (
                    party_version_id, party_id, firm_id, matter_id, party_version,
                    party_kind, display_label, basis_hash, meaning_hash,
                    confirmed_matter_version, confirmed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    stable_id,
                    actor.firm_id,
                    matter_id,
                    next_version,
                    normalized.party_kind,
                    normalized.display_label,
                    normalized.basis_hash,
                    meaning_hash,
                    expected_version,
                    actor.actor_id,
                ),
            )
            if next_version == 1:
                connection.execute(
                    """
                    INSERT INTO case_party_heads (
                        party_id, firm_id, matter_id, latest_version, current_version_id
                    ) VALUES (%s, %s, %s, 1, %s)
                    """,
                    (stable_id, actor.firm_id, matter_id, version_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE case_party_heads
                    SET latest_version = %s, current_version_id = %s, updated_at = now()
                    WHERE party_id = %s AND matter_id = %s AND firm_id = %s
                    """,
                    (next_version, version_id, stable_id, matter_id, actor.firm_id),
                )
            return stable_id, {
                "party_id": stable_id,
                "party_version_id": version_id,
                "party_version": next_version,
                "party_kind": normalized.party_kind,
                "meaning_hash": meaning_hash,
            }

        return self._command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            command_name=command,
            payload=payload,
            object_type="CASE_PARTY",
            event_type="CASE_PARTY_VERSION_CONFIRMED",
            apply=apply,
        )

    def confirm_proceeding(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        confirmation: CourtProceedingConfirmation,
        proceeding_id: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        self._validate_write(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        normalized = CourtProceedingConfirmation(
            forum_type=_normalize_code("forum_type", confirmation.forum_type),
            case_type_code=_normalize_case_type(confirmation.case_type_code),
            procedure_stage=_normalize_code("procedure_stage", confirmation.procedure_stage),
            basis_hash=_hash_value("proceeding basis_hash", confirmation.basis_hash),
        )
        if proceeding_id is not None:
            _validate_uuid("proceeding_id", proceeding_id)
        command = "CONFIRM_COURT_PROCEEDING_VERSION"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "proceeding_id": proceeding_id,
            "forum_type": normalized.forum_type,
            "case_type_code": normalized.case_type_code,
            "procedure_stage": normalized.procedure_stage,
            "basis_hash": normalized.basis_hash,
        }

        def apply(connection: psycopg.Connection) -> tuple[str, dict[str, Any]]:
            stable_id = proceeding_id or str(uuid4())
            if proceeding_id is None:
                connection.execute(
                    "INSERT INTO court_proceedings (proceeding_id, firm_id, matter_id) VALUES (%s, %s, %s)",
                    (stable_id, actor.firm_id, matter_id),
                )
                next_version = 1
            else:
                head = connection.execute(
                    """
                    SELECT latest_version FROM court_proceeding_heads
                    WHERE proceeding_id = %s AND matter_id = %s AND firm_id = %s
                    FOR UPDATE
                    """,
                    (stable_id, matter_id, actor.firm_id),
                ).fetchone()
                if head is None:
                    raise KeyError(stable_id)
                next_version = int(head["latest_version"]) + 1
            version_id = str(uuid4())
            meaning_hash = _meaning_hash(
                "court-proceeding-v1",
                actor.firm_id,
                matter_id,
                stable_id,
                next_version,
                normalized.forum_type,
                normalized.case_type_code,
                normalized.procedure_stage,
                normalized.basis_hash,
            )
            connection.execute(
                """
                INSERT INTO court_proceeding_versions (
                    proceeding_version_id, proceeding_id, firm_id, matter_id,
                    proceeding_version, forum_type, case_type_code, procedure_stage,
                    basis_hash, meaning_hash, confirmed_matter_version, confirmed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    stable_id,
                    actor.firm_id,
                    matter_id,
                    next_version,
                    normalized.forum_type,
                    normalized.case_type_code,
                    normalized.procedure_stage,
                    normalized.basis_hash,
                    meaning_hash,
                    expected_version,
                    actor.actor_id,
                ),
            )
            if next_version == 1:
                connection.execute(
                    """
                    INSERT INTO court_proceeding_heads (
                        proceeding_id, firm_id, matter_id, latest_version, current_version_id
                    ) VALUES (%s, %s, %s, 1, %s)
                    """,
                    (stable_id, actor.firm_id, matter_id, version_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE court_proceeding_heads
                    SET latest_version = %s, current_version_id = %s, updated_at = now()
                    WHERE proceeding_id = %s AND matter_id = %s AND firm_id = %s
                    """,
                    (next_version, version_id, stable_id, matter_id, actor.firm_id),
                )
            return stable_id, {
                "proceeding_id": stable_id,
                "proceeding_version_id": version_id,
                "proceeding_version": next_version,
                "case_type_code": normalized.case_type_code,
                "procedure_stage": normalized.procedure_stage,
                "meaning_hash": meaning_hash,
            }

        return self._command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            command_name=command,
            payload=payload,
            object_type="COURT_PROCEEDING",
            event_type="COURT_PROCEEDING_VERSION_CONFIRMED",
            apply=apply,
        )

    def confirm_position(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        confirmation: CourtPartyPositionConfirmation,
        position_id: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        self._validate_write(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        _validate_uuid("proceeding_id", confirmation.proceeding_id)
        _validate_uuid("party_id", confirmation.party_id)
        if position_id is not None:
            _validate_uuid("position_id", position_id)
        normalized = CourtPartyPositionConfirmation(
            proceeding_id=confirmation.proceeding_id,
            party_id=confirmation.party_id,
            position_code=_normalize_code("position_code", confirmation.position_code),
            basis_hash=_hash_value("position basis_hash", confirmation.basis_hash),
        )
        command = "CONFIRM_COURT_PARTY_POSITION_VERSION"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "position_id": position_id,
            **normalized.__dict__,
        }

        def apply(connection: psycopg.Connection) -> tuple[str, dict[str, Any]]:
            stable_id = position_id or str(uuid4())
            if position_id is None:
                _require_stable_upstreams(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    proceeding_id=normalized.proceeding_id,
                    party_id=normalized.party_id,
                )
                connection.execute(
                    """
                    INSERT INTO court_party_positions (
                        position_id, firm_id, matter_id, proceeding_id, party_id
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        stable_id,
                        actor.firm_id,
                        matter_id,
                        normalized.proceeding_id,
                        normalized.party_id,
                    ),
                )
                next_version = 1
            else:
                head = connection.execute(
                    """
                    SELECT h.latest_version, p.proceeding_id, p.party_id
                    FROM court_party_position_heads h
                    JOIN court_party_positions p
                      ON p.position_id = h.position_id AND p.firm_id = h.firm_id
                     AND p.matter_id = h.matter_id
                    WHERE h.position_id = %s AND h.matter_id = %s AND h.firm_id = %s
                    FOR UPDATE OF h
                    """,
                    (stable_id, matter_id, actor.firm_id),
                ).fetchone()
                if head is None:
                    raise KeyError(stable_id)
                if (
                    str(head["proceeding_id"]) != normalized.proceeding_id
                    or str(head["party_id"]) != normalized.party_id
                ):
                    raise CaseLedgerPersistenceBlocked(
                        "a stable court-party position cannot change its proceeding or party"
                    )
                next_version = int(head["latest_version"]) + 1
            version_id = str(uuid4())
            meaning_hash = _meaning_hash(
                "court-party-position-v1",
                actor.firm_id,
                matter_id,
                stable_id,
                next_version,
                normalized.proceeding_id,
                normalized.party_id,
                normalized.position_code,
                normalized.basis_hash,
            )
            connection.execute(
                """
                INSERT INTO court_party_position_versions (
                    position_version_id, position_id, firm_id, matter_id,
                    position_version, position_code, basis_hash, meaning_hash,
                    confirmed_matter_version, confirmed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    stable_id,
                    actor.firm_id,
                    matter_id,
                    next_version,
                    normalized.position_code,
                    normalized.basis_hash,
                    meaning_hash,
                    expected_version,
                    actor.actor_id,
                ),
            )
            _upsert_version_head(
                connection,
                table="court_party_position_heads",
                entity_column="position_id",
                entity_id=stable_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                next_version=next_version,
                version_id=version_id,
            )
            return stable_id, {
                "position_id": stable_id,
                "position_version_id": version_id,
                "position_version": next_version,
                "proceeding_id": normalized.proceeding_id,
                "party_id": normalized.party_id,
                "position_code": normalized.position_code,
                "meaning_hash": meaning_hash,
            }

        return self._command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            command_name=command,
            payload=payload,
            object_type="COURT_PARTY_POSITION",
            event_type="COURT_PARTY_POSITION_VERSION_CONFIRMED",
            apply=apply,
        )

    def confirm_engagement(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        confirmation: FirmEngagementConfirmation,
        engagement_id: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        self._validate_write(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        _validate_uuid("proceeding_id", confirmation.proceeding_id)
        _validate_uuid("represented_party_id", confirmation.represented_party_id)
        if engagement_id is not None:
            _validate_uuid("engagement_id", engagement_id)
        state = confirmation.engagement_state.strip().upper()
        if state not in {"ACTIVE", "ENDED"}:
            raise ValueError("engagement_state must be ACTIVE or ENDED")
        normalized = FirmEngagementConfirmation(
            proceeding_id=confirmation.proceeding_id,
            represented_party_id=confirmation.represented_party_id,
            authority_scope_code=_normalize_code(
                "authority_scope_code", confirmation.authority_scope_code
            ),
            engagement_state=state,
            basis_hash=_hash_value("engagement basis_hash", confirmation.basis_hash),
        )
        command = "CONFIRM_FIRM_ENGAGEMENT_VERSION"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "engagement_id": engagement_id,
            **normalized.__dict__,
        }

        def apply(connection: psycopg.Connection) -> tuple[str, dict[str, Any]]:
            stable_id = engagement_id or str(uuid4())
            if engagement_id is None:
                _require_stable_upstreams(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    proceeding_id=normalized.proceeding_id,
                    party_id=normalized.represented_party_id,
                )
                connection.execute(
                    """
                    INSERT INTO firm_engagements (
                        engagement_id, firm_id, matter_id, proceeding_id, represented_party_id
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        stable_id,
                        actor.firm_id,
                        matter_id,
                        normalized.proceeding_id,
                        normalized.represented_party_id,
                    ),
                )
                next_version = 1
            else:
                head = connection.execute(
                    """
                    SELECT h.latest_version, e.proceeding_id, e.represented_party_id
                    FROM firm_engagement_heads h
                    JOIN firm_engagements e
                      ON e.engagement_id = h.engagement_id AND e.firm_id = h.firm_id
                     AND e.matter_id = h.matter_id
                    WHERE h.engagement_id = %s AND h.matter_id = %s AND h.firm_id = %s
                    FOR UPDATE OF h
                    """,
                    (stable_id, matter_id, actor.firm_id),
                ).fetchone()
                if head is None:
                    raise KeyError(stable_id)
                if (
                    str(head["proceeding_id"]) != normalized.proceeding_id
                    or str(head["represented_party_id"]) != normalized.represented_party_id
                ):
                    raise CaseLedgerPersistenceBlocked(
                        "a stable firm engagement cannot change its proceeding or represented party"
                    )
                next_version = int(head["latest_version"]) + 1
            version_id = str(uuid4())
            meaning_hash = _meaning_hash(
                "firm-engagement-v1",
                actor.firm_id,
                matter_id,
                stable_id,
                next_version,
                normalized.proceeding_id,
                normalized.represented_party_id,
                normalized.authority_scope_code,
                normalized.engagement_state,
                normalized.basis_hash,
            )
            connection.execute(
                """
                INSERT INTO firm_engagement_versions (
                    engagement_version_id, engagement_id, firm_id, matter_id,
                    engagement_version, authority_scope_code, engagement_state,
                    basis_hash, meaning_hash, confirmed_matter_version, confirmed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    stable_id,
                    actor.firm_id,
                    matter_id,
                    next_version,
                    normalized.authority_scope_code,
                    normalized.engagement_state,
                    normalized.basis_hash,
                    meaning_hash,
                    expected_version,
                    actor.actor_id,
                ),
            )
            _upsert_version_head(
                connection,
                table="firm_engagement_heads",
                entity_column="engagement_id",
                entity_id=stable_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                next_version=next_version,
                version_id=version_id,
            )
            return stable_id, {
                "engagement_id": stable_id,
                "engagement_version_id": version_id,
                "engagement_version": next_version,
                "proceeding_id": normalized.proceeding_id,
                "represented_party_id": normalized.represented_party_id,
                "authority_scope_code": normalized.authority_scope_code,
                "engagement_state": normalized.engagement_state,
                "meaning_hash": meaning_hash,
            }

        return self._command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            command_name=command,
            payload=payload,
            object_type="FIRM_ENGAGEMENT",
            event_type="FIRM_ENGAGEMENT_VERSION_CONFIRMED",
            apply=apply,
        )

    def confirm_current_profile(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        represented_party_id: str,
        proceeding_id: str,
        position_id: str,
        engagement_id: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_write(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        for label, value in (
            ("represented_party_id", represented_party_id),
            ("proceeding_id", proceeding_id),
            ("position_id", position_id),
            ("engagement_id", engagement_id),
        ):
            _validate_uuid(label, value)
        command = "CONFIRM_CURRENT_CASE_POSTURE_PROFILE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "represented_party_id": represented_party_id,
            "proceeding_id": proceeding_id,
            "position_id": position_id,
            "engagement_id": engagement_id,
        }

        def apply(connection: psycopg.Connection) -> tuple[str, dict[str, Any]]:
            upstream = connection.execute(
                """
                SELECT pv.party_version_id, pv.display_label,
                       cpv.proceeding_version_id, cpv.case_type_code, cpv.procedure_stage,
                       ppv.position_version_id, ppv.position_code,
                       fev.engagement_version_id, fev.authority_scope_code, fev.engagement_state
                FROM case_party_heads ph
                JOIN case_party_versions pv
                  ON pv.party_version_id = ph.current_version_id
                 AND pv.party_id = ph.party_id AND pv.firm_id = ph.firm_id
                 AND pv.matter_id = ph.matter_id
                JOIN court_proceeding_heads cph
                  ON cph.proceeding_id = %s AND cph.firm_id = ph.firm_id
                 AND cph.matter_id = ph.matter_id
                JOIN court_proceeding_versions cpv
                  ON cpv.proceeding_version_id = cph.current_version_id
                 AND cpv.proceeding_id = cph.proceeding_id AND cpv.firm_id = cph.firm_id
                 AND cpv.matter_id = cph.matter_id
                JOIN court_party_positions pp
                  ON pp.position_id = %s AND pp.proceeding_id = cph.proceeding_id
                 AND pp.party_id = ph.party_id AND pp.firm_id = ph.firm_id
                 AND pp.matter_id = ph.matter_id
                JOIN court_party_position_heads pph
                  ON pph.position_id = pp.position_id AND pph.firm_id = pp.firm_id
                 AND pph.matter_id = pp.matter_id
                JOIN court_party_position_versions ppv
                  ON ppv.position_version_id = pph.current_version_id
                 AND ppv.position_id = pph.position_id AND ppv.firm_id = pph.firm_id
                 AND ppv.matter_id = pph.matter_id
                JOIN firm_engagements fe
                  ON fe.engagement_id = %s AND fe.proceeding_id = cph.proceeding_id
                 AND fe.represented_party_id = ph.party_id AND fe.firm_id = ph.firm_id
                 AND fe.matter_id = ph.matter_id
                JOIN firm_engagement_heads feh
                  ON feh.engagement_id = fe.engagement_id AND feh.firm_id = fe.firm_id
                 AND feh.matter_id = fe.matter_id
                JOIN firm_engagement_versions fev
                  ON fev.engagement_version_id = feh.current_version_id
                 AND fev.engagement_id = feh.engagement_id AND fev.firm_id = feh.firm_id
                 AND fev.matter_id = feh.matter_id
                WHERE ph.party_id = %s AND ph.matter_id = %s AND ph.firm_id = %s
                """,
                (
                    proceeding_id,
                    position_id,
                    engagement_id,
                    represented_party_id,
                    matter_id,
                    actor.firm_id,
                ),
            ).fetchone()
            if upstream is None:
                raise CaseLedgerPersistenceBlocked(
                    "a case posture profile requires compatible current party, proceeding, position and engagement versions"
                )
            if upstream["engagement_state"] != "ACTIVE":
                raise CaseLedgerPersistenceBlocked(
                    "a case posture profile requires a current active firm engagement"
                )

            head = connection.execute(
                """
                SELECT latest_profile_version, current_profile_id
                FROM case_posture_profile_heads
                WHERE matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            next_version = 1 if head is None else int(head["latest_profile_version"]) + 1
            latest = connection.execute(
                """
                SELECT profile_id FROM case_posture_profiles
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY profile_version DESC LIMIT 1
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            supersedes = None if latest is None else str(latest["profile_id"])
            profile_id = str(uuid4())
            profile_hash = _profile_hash(
                firm_id=actor.firm_id,
                matter_id=matter_id,
                profile_version=next_version,
                represented_party_id=represented_party_id,
                represented_party_version_id=str(upstream["party_version_id"]),
                proceeding_id=proceeding_id,
                proceeding_version_id=str(upstream["proceeding_version_id"]),
                position_id=position_id,
                position_version_id=str(upstream["position_version_id"]),
                engagement_id=engagement_id,
                engagement_version_id=str(upstream["engagement_version_id"]),
                case_type_code=str(upstream["case_type_code"]),
                procedure_stage=str(upstream["procedure_stage"]),
                represented_position=str(upstream["position_code"]),
                authority_scope_code=str(upstream["authority_scope_code"]),
                engagement_state=str(upstream["engagement_state"]),
            )
            connection.execute(
                """
                INSERT INTO case_posture_profiles (
                    profile_id, firm_id, matter_id, profile_version, status,
                    represented_party_id, represented_party_version_id,
                    proceeding_id, proceeding_version_id,
                    position_id, position_version_id,
                    engagement_id, engagement_version_id,
                    case_type_code, procedure_stage, represented_position,
                    authority_scope_code, engagement_state, profile_hash,
                    confirmed_matter_version, supersedes_profile_id, confirmed_by
                ) VALUES (
                    %s, %s, %s, %s, 'CONFIRMED', %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    profile_id,
                    actor.firm_id,
                    matter_id,
                    next_version,
                    represented_party_id,
                    upstream["party_version_id"],
                    proceeding_id,
                    upstream["proceeding_version_id"],
                    position_id,
                    upstream["position_version_id"],
                    engagement_id,
                    upstream["engagement_version_id"],
                    upstream["case_type_code"],
                    upstream["procedure_stage"],
                    upstream["position_code"],
                    upstream["authority_scope_code"],
                    upstream["engagement_state"],
                    profile_hash,
                    expected_version,
                    supersedes,
                    actor.actor_id,
                ),
            )
            if supersedes is not None:
                connection.execute(
                    """
                    INSERT INTO case_posture_profile_events (
                        profile_id, firm_id, matter_id, event_sequence, effective_status,
                        cause_kind, cause_object_id, actor_id
                    )
                    SELECT %s, %s, %s, COALESCE(max(event_sequence), 0) + 1,
                           'SUPERSEDED', 'PROFILE_REPLACED', %s, %s
                    FROM case_posture_profile_events WHERE profile_id = %s
                    """,
                    (
                        supersedes,
                        actor.firm_id,
                        matter_id,
                        profile_id,
                        actor.actor_id,
                        supersedes,
                    ),
                )
            connection.execute(
                """
                INSERT INTO case_posture_profile_events (
                    profile_id, firm_id, matter_id, event_sequence, effective_status,
                    cause_kind, cause_object_id, actor_id
                ) VALUES (%s, %s, %s, 1, 'CURRENT', 'PROFILE_CONFIRMED', %s, %s)
                """,
                (profile_id, actor.firm_id, matter_id, profile_id, actor.actor_id),
            )
            if head is None:
                connection.execute(
                    """
                    INSERT INTO case_posture_profile_heads (
                        matter_id, firm_id, latest_profile_version, current_profile_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (matter_id, actor.firm_id, next_version, profile_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE case_posture_profile_heads
                    SET latest_profile_version = %s, current_profile_id = %s, updated_at = now()
                    WHERE matter_id = %s AND firm_id = %s
                    """,
                    (next_version, profile_id, matter_id, actor.firm_id),
                )
            return profile_id, {
                "profile_id": profile_id,
                "profile_version": next_version,
                "profile_hash": profile_hash,
                "represented_party_id": represented_party_id,
                "represented_party_version_id": str(upstream["party_version_id"]),
                "proceeding_id": proceeding_id,
                "proceeding_version_id": str(upstream["proceeding_version_id"]),
                "position_id": position_id,
                "position_version_id": str(upstream["position_version_id"]),
                "engagement_id": engagement_id,
                "engagement_version_id": str(upstream["engagement_version_id"]),
                "case_type_code": str(upstream["case_type_code"]),
                "procedure_stage": str(upstream["procedure_stage"]),
                "represented_position": str(upstream["position_code"]),
            }

        return self._command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            command_name=command,
            payload=payload,
            object_type="CASE_POSTURE_PROFILE",
            event_type="CASE_POSTURE_PROFILE_CONFIRMED",
            apply=apply,
        )

    def get_current_profile(
        self, *, matter_id: str, actor: Actor
    ) -> CasePostureProfileSnapshot | None:
        return self._get_profile(matter_id=matter_id, actor=actor, current_only=True)

    def get_latest_profile_state(
        self, *, matter_id: str, actor: Actor
    ) -> CasePostureProfileSnapshot | None:
        return self._get_profile(matter_id=matter_id, actor=actor, current_only=False)

    def _get_profile(
        self, *, matter_id: str, actor: Actor, current_only: bool
    ) -> CasePostureProfileSnapshot | None:
        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key="read-case-posture-profile"
        )
        _require_roles(actor, self._READ_ROLES)
        with self._transaction(actor.firm_id, read_only=True) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            where_current = "AND h.current_profile_id = p.profile_id" if current_only else ""
            row = connection.execute(
                f"""
                SELECT p.*, pv.display_label AS represented_party_display_label,
                       pv.party_kind AS represented_party_kind,
                       cpv.forum_type AS forum_type,
                       latest.effective_status
                FROM case_posture_profile_heads h
                JOIN case_posture_profiles p
                  ON p.matter_id = h.matter_id AND p.firm_id = h.firm_id
                JOIN case_party_versions pv
                  ON pv.party_version_id = p.represented_party_version_id
                 AND pv.party_id = p.represented_party_id AND pv.firm_id = p.firm_id
                 AND pv.matter_id = p.matter_id
                JOIN court_proceeding_versions cpv
                  ON cpv.proceeding_version_id = p.proceeding_version_id
                 AND cpv.proceeding_id = p.proceeding_id AND cpv.firm_id = p.firm_id
                 AND cpv.matter_id = p.matter_id
                JOIN LATERAL (
                    SELECT e.effective_status
                    FROM case_posture_profile_events e
                    WHERE e.profile_id = p.profile_id
                    ORDER BY e.event_sequence DESC LIMIT 1
                ) latest ON true
                WHERE h.matter_id = %s AND h.firm_id = %s {where_current}
                ORDER BY p.profile_version DESC LIMIT 1
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            return None if row is None else _snapshot(row)

    def _validate_write(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> None:
        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        if Role.SYSTEM_WORKER in actor.roles:
            raise PermissionError("SYSTEM_WORKER is read-only for case posture meanings")
        _require_roles(actor, self._WRITE_ROLES)
        _require_positive_version(expected_version)

    def _command(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        command_name: str,
        payload: dict[str, Any],
        object_type: str,
        event_type: str,
        apply: Any,
    ) -> CaseLedgerCommandReceipt:
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
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
                allowed_roles=self._WRITE_ROLES,
            )
            object_id, audit_payload = apply(connection)
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type=event_type,
                object_type=object_type,
                object_id=object_id,
                audit_payload=audit_payload,
                stale_submission=True,
                stale_calculations=True,
            )

    @contextmanager
    def _transaction(
        self, firm_id: str, *, read_only: bool = False
    ) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            if read_only:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _normalize_code(label: str, value: str) -> str:
    normalized = value.strip().upper()
    if not _CODE.fullmatch(normalized):
        raise ValueError(f"{label} must use a canonical uppercase code")
    return normalized


def _normalize_case_type(value: str) -> str:
    normalized = value.strip().upper()
    if not _CASE_TYPE_CODE.fullmatch(normalized):
        raise ValueError("case_type_code must use a canonical uppercase namespaced code")
    return normalized


def _normalize_label(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("display_label must contain 1 to 200 characters")
    return normalized


def _hash_value(label: str, value: str) -> str:
    _validate_sha256(label, value)
    return value


def _meaning_hash(kind: str, *values: object) -> str:
    return sha256("|".join((kind, *(str(value) for value in values))).encode("utf-8")).hexdigest()


def _profile_hash(
    *,
    firm_id: str,
    matter_id: str,
    profile_version: int,
    represented_party_id: str,
    represented_party_version_id: str,
    proceeding_id: str,
    proceeding_version_id: str,
    position_id: str,
    position_version_id: str,
    engagement_id: str,
    engagement_version_id: str,
    case_type_code: str,
    procedure_stage: str,
    represented_position: str,
    authority_scope_code: str,
    engagement_state: str,
) -> str:
    return _meaning_hash(
        "case-posture-profile-v1",
        firm_id,
        matter_id,
        profile_version,
        represented_party_id,
        represented_party_version_id,
        proceeding_id,
        proceeding_version_id,
        position_id,
        position_version_id,
        engagement_id,
        engagement_version_id,
        case_type_code,
        procedure_stage,
        represented_position,
        authority_scope_code,
        engagement_state,
    )


def _require_stable_upstreams(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    proceeding_id: str,
    party_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT 1
        FROM court_proceedings cp
        JOIN case_parties p
          ON p.party_id = %s AND p.firm_id = cp.firm_id AND p.matter_id = cp.matter_id
        WHERE cp.proceeding_id = %s AND cp.matter_id = %s AND cp.firm_id = %s
        """,
        (party_id, proceeding_id, matter_id, actor.firm_id),
    ).fetchone()
    if row is None:
        raise CaseLedgerPersistenceBlocked(
            "the proceeding and party must be stable objects in this matter"
        )


def _upsert_version_head(
    connection: psycopg.Connection,
    *,
    table: str,
    entity_column: str,
    entity_id: str,
    firm_id: str,
    matter_id: str,
    next_version: int,
    version_id: str,
) -> None:
    allowed = {
        ("court_party_position_heads", "position_id"),
        ("firm_engagement_heads", "engagement_id"),
    }
    if (table, entity_column) not in allowed:
        raise ValueError("unsupported case posture version head")
    if next_version == 1:
        connection.execute(
            f"""
            INSERT INTO {table} (
                {entity_column}, firm_id, matter_id, latest_version, current_version_id
            ) VALUES (%s, %s, %s, 1, %s)
            """,
            (entity_id, firm_id, matter_id, version_id),
        )
    else:
        connection.execute(
            f"""
            UPDATE {table}
            SET latest_version = %s, current_version_id = %s, updated_at = now()
            WHERE {entity_column} = %s AND matter_id = %s AND firm_id = %s
            """,
            (next_version, version_id, entity_id, matter_id, firm_id),
        )


def _snapshot(row: dict[str, Any]) -> CasePostureProfileSnapshot:
    return CasePostureProfileSnapshot(
        profile_id=str(row["profile_id"]),
        matter_id=str(row["matter_id"]),
        profile_version=int(row["profile_version"]),
        effective_status=str(row["effective_status"]),
        profile_hash=str(row["profile_hash"]),
        represented_party_id=str(row["represented_party_id"]),
        represented_party_version_id=str(row["represented_party_version_id"]),
        represented_party_display_label=str(row["represented_party_display_label"]),
        represented_party_kind=str(row["represented_party_kind"]),
        proceeding_id=str(row["proceeding_id"]),
        proceeding_version_id=str(row["proceeding_version_id"]),
        forum_type=str(row["forum_type"]),
        position_id=str(row["position_id"]),
        position_version_id=str(row["position_version_id"]),
        engagement_id=str(row["engagement_id"]),
        engagement_version_id=str(row["engagement_version_id"]),
        case_type_code=str(row["case_type_code"]),
        procedure_stage=str(row["procedure_stage"]),
        represented_position=str(row["represented_position"]),
        authority_scope_code=str(row["authority_scope_code"]),
        engagement_state=str(row["engagement_state"]),
        confirmed_matter_version=int(row["confirmed_matter_version"]),
        supersedes_profile_id=(
            None if row["supersedes_profile_id"] is None else str(row["supersedes_profile_id"])
        ),
        confirmed_by=str(row["confirmed_by"]),
    )
