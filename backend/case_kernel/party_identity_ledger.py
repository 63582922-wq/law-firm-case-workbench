"""Evidence-bound parties, aliases, accounts, and representation for synthetic Alpha."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
import json
from uuid import uuid4

from .evidence_refs import EvidenceLink, EvidenceReferenceBlocked, validate_evidence_links
from .models import Actor, Role


class PartyIdentityBlocked(ValueError):
    """A party, identifier, or representation lacks a lawyer-confirmed basis."""


class PartyStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


class LitigationRole(str, Enum):
    PLAINTIFF = "PLAINTIFF"
    DEFENDANT = "DEFENDANT"
    THIRD_PARTY = "THIRD_PARTY"
    REPRESENTATIVE = "REPRESENTATIVE"
    OTHER = "OTHER"


class IdentifierKind(str, Enum):
    LEGAL_NAME = "LEGAL_NAME"
    WECHAT_NICKNAME = "WECHAT_NICKNAME"
    WECHAT_ACCOUNT = "WECHAT_ACCOUNT"
    BANK_ACCOUNT = "BANK_ACCOUNT"
    PHONE = "PHONE"
    OTHER = "OTHER"


class IdentifierStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


class RepresentationScope(str, Enum):
    FULL_AUTHORITY = "FULL_AUTHORITY"
    PARTIAL_AUTHORITY = "PARTIAL_AUTHORITY"
    DOCUMENT_RECEIPT = "DOCUMENT_RECEIPT"
    OTHER = "OTHER"


class RepresentationStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class Party:
    party_id: str
    display_name: str
    role: LitigationRole
    evidence_links: tuple[EvidenceLink, ...]
    status: PartyStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class PartyIdentifier:
    identifier_id: str
    party_id: str
    kind: IdentifierKind
    original_value: str
    normalization_key: str
    evidence_links: tuple[EvidenceLink, ...]
    status: IdentifierStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class Representation:
    representation_id: str
    represented_party_id: str
    representative_party_id: str
    scope: RepresentationScope
    evidence_links: tuple[EvidenceLink, ...]
    status: RepresentationStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class PartyIdentitySnapshot:
    snapshot_id: str
    ledger_version: int
    input_hash: str
    parties: tuple[Party, ...]
    identifiers: tuple[PartyIdentifier, ...]
    representations: tuple[Representation, ...]


class PartyIdentityLedger:
    """Keeps observations separate from lawyer-confirmed identity relationships."""

    def __init__(self) -> None:
        self._parties: dict[str, Party] = {}
        self._identifiers: dict[str, PartyIdentifier] = {}
        self._representations: dict[str, Representation] = {}
        self._version = 1

    @property
    def version(self) -> int:
        return self._version

    def add_party_candidate(
        self,
        actor: Actor,
        *,
        display_name: str,
        role: LitigationRole,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> Party:
        _require_candidate_role(actor)
        _require_text(display_name, "party display name")
        _validate_evidence(evidence_links)
        party = Party(
            party_id=f"party_{uuid4().hex}",
            display_name=display_name.strip(),
            role=role,
            evidence_links=evidence_links,
            status=PartyStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._parties[party.party_id] = party
        self._version += 1
        return party

    def confirm_party(self, actor: Actor, *, party_id: str, confirmation_hash: str) -> Party:
        _require_lead(actor)
        _require_text(confirmation_hash, "party confirmation hash")
        party = self._require_party(party_id)
        if party.status is not PartyStatus.CANDIDATE:
            raise PartyIdentityBlocked("only a party candidate can be confirmed")
        confirmed = Party(
            party_id=party.party_id,
            display_name=party.display_name,
            role=party.role,
            evidence_links=party.evidence_links,
            status=PartyStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._parties[party_id] = confirmed
        self._version += 1
        return confirmed

    def invalidate_party(self, actor: Actor, *, party_id: str, reason_hash: str) -> Party:
        _require_lead(actor)
        _require_text(reason_hash, "party invalidation hash")
        party = self._require_party(party_id)
        if party.status is PartyStatus.INVALIDATED:
            raise PartyIdentityBlocked("an invalidated party must be rebuilt from original evidence")
        invalidated = Party(
            party_id=party.party_id,
            display_name=party.display_name,
            role=party.role,
            evidence_links=party.evidence_links,
            status=PartyStatus.INVALIDATED,
            confirmed_by=actor.actor_id,
            confirmation_hash=reason_hash,
        )
        self._parties[party_id] = invalidated
        self._invalidate_party_dependents({party_id})
        self._version += 1
        return invalidated

    def add_identifier_candidate(
        self,
        actor: Actor,
        *,
        party_id: str,
        kind: IdentifierKind,
        original_value: str,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> PartyIdentifier:
        _require_candidate_role(actor)
        party = self._require_party(party_id)
        if party.status is PartyStatus.INVALIDATED:
            raise PartyIdentityBlocked("an identifier cannot attach to an invalidated party")
        _require_text(original_value, "identifier value")
        _validate_evidence(evidence_links)
        identifier = PartyIdentifier(
            identifier_id=f"identifier_{uuid4().hex}",
            party_id=party_id,
            kind=kind,
            original_value=original_value.strip(),
            normalization_key=_normalize_identifier(kind, original_value),
            evidence_links=evidence_links,
            status=IdentifierStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._identifiers[identifier.identifier_id] = identifier
        self._version += 1
        return identifier

    def confirm_identifier(self, actor: Actor, *, identifier_id: str, confirmation_hash: str) -> PartyIdentifier:
        _require_lead(actor)
        _require_text(confirmation_hash, "identifier confirmation hash")
        identifier = self._require_identifier(identifier_id)
        if identifier.status is not IdentifierStatus.CANDIDATE:
            raise PartyIdentityBlocked("only an identifier candidate can be confirmed")
        party = self._require_party(identifier.party_id)
        if party.status is not PartyStatus.CONFIRMED:
            raise PartyIdentityBlocked("an identifier requires a lawyer-confirmed party")
        conflicts = [
            existing
            for existing in self._identifiers.values()
            if existing.status is IdentifierStatus.CONFIRMED
            and existing.kind is identifier.kind
            and existing.normalization_key == identifier.normalization_key
            and existing.party_id != identifier.party_id
        ]
        if conflicts:
            raise PartyIdentityBlocked("this confirmed identifier conflicts with another confirmed party")
        confirmed = PartyIdentifier(
            identifier_id=identifier.identifier_id,
            party_id=identifier.party_id,
            kind=identifier.kind,
            original_value=identifier.original_value,
            normalization_key=identifier.normalization_key,
            evidence_links=identifier.evidence_links,
            status=IdentifierStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._identifiers[identifier_id] = confirmed
        self._version += 1
        return confirmed

    def add_representation_candidate(
        self,
        actor: Actor,
        *,
        represented_party_id: str,
        representative_party_id: str,
        scope: RepresentationScope,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> Representation:
        _require_candidate_role(actor)
        if represented_party_id == representative_party_id:
            raise PartyIdentityBlocked("a party cannot represent itself")
        self._require_party(represented_party_id)
        self._require_party(representative_party_id)
        _validate_evidence(evidence_links)
        representation = Representation(
            representation_id=f"representation_{uuid4().hex}",
            represented_party_id=represented_party_id,
            representative_party_id=representative_party_id,
            scope=scope,
            evidence_links=evidence_links,
            status=RepresentationStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._representations[representation.representation_id] = representation
        self._version += 1
        return representation

    def confirm_representation(self, actor: Actor, *, representation_id: str, confirmation_hash: str) -> Representation:
        _require_lead(actor)
        _require_text(confirmation_hash, "representation confirmation hash")
        representation = self._representations.get(representation_id)
        if representation is None or representation.status is not RepresentationStatus.CANDIDATE:
            raise PartyIdentityBlocked("only a representation candidate can be confirmed")
        if self._require_party(representation.represented_party_id).status is not PartyStatus.CONFIRMED:
            raise PartyIdentityBlocked("a representation requires a confirmed represented party")
        if self._require_party(representation.representative_party_id).status is not PartyStatus.CONFIRMED:
            raise PartyIdentityBlocked("a representation requires a confirmed representative party")
        confirmed = Representation(
            representation_id=representation.representation_id,
            represented_party_id=representation.represented_party_id,
            representative_party_id=representation.representative_party_id,
            scope=representation.scope,
            evidence_links=representation.evidence_links,
            status=RepresentationStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._representations[representation_id] = confirmed
        self._version += 1
        return confirmed

    def build_formal_snapshot(self, actor: Actor) -> PartyIdentitySnapshot:
        _require_lead(actor)
        parties = tuple(sorted((party for party in self._parties.values() if party.status is PartyStatus.CONFIRMED), key=lambda item: item.party_id))
        if not parties:
            raise PartyIdentityBlocked("a formal identity snapshot requires at least one confirmed party")
        confirmed_party_ids = {party.party_id for party in parties}
        identifiers = tuple(sorted((item for item in self._identifiers.values() if item.status is IdentifierStatus.CONFIRMED), key=lambda item: item.identifier_id))
        if any(item.party_id not in confirmed_party_ids for item in identifiers):
            raise PartyIdentityBlocked("a formal identifier cannot point to an unconfirmed party")
        _assert_no_identifier_conflicts(identifiers)
        representations = tuple(sorted((item for item in self._representations.values() if item.status is RepresentationStatus.CONFIRMED), key=lambda item: item.representation_id))
        if any(
            item.represented_party_id not in confirmed_party_ids or item.representative_party_id not in confirmed_party_ids
            for item in representations
        ):
            raise PartyIdentityBlocked("a formal representation cannot point to an unconfirmed party")
        payload = {"version": self._version, "parties": parties, "identifiers": identifiers, "representations": representations}
        return PartyIdentitySnapshot(
            snapshot_id=f"party_identity_snapshot_{uuid4().hex}",
            ledger_version=self._version,
            input_hash=_hash_payload(payload),
            parties=parties,
            identifiers=identifiers,
            representations=representations,
        )

    def _invalidate_party_dependents(self, party_ids: set[str]) -> None:
        for identifier_id, identifier in list(self._identifiers.items()):
            if identifier.party_id in party_ids and identifier.status is not IdentifierStatus.INVALIDATED:
                self._identifiers[identifier_id] = PartyIdentifier(
                    identifier_id=identifier.identifier_id,
                    party_id=identifier.party_id,
                    kind=identifier.kind,
                    original_value=identifier.original_value,
                    normalization_key=identifier.normalization_key,
                    evidence_links=identifier.evidence_links,
                    status=IdentifierStatus.INVALIDATED,
                    confirmed_by=None,
                    confirmation_hash=None,
                )
        for representation_id, representation in list(self._representations.items()):
            if {representation.represented_party_id, representation.representative_party_id} & party_ids and representation.status is not RepresentationStatus.INVALIDATED:
                self._representations[representation_id] = Representation(
                    representation_id=representation.representation_id,
                    represented_party_id=representation.represented_party_id,
                    representative_party_id=representation.representative_party_id,
                    scope=representation.scope,
                    evidence_links=representation.evidence_links,
                    status=RepresentationStatus.INVALIDATED,
                    confirmed_by=None,
                    confirmation_hash=None,
                )

    def _require_party(self, party_id: str) -> Party:
        party = self._parties.get(party_id)
        if party is None:
            raise PartyIdentityBlocked("unknown party")
        return party

    def _require_identifier(self, identifier_id: str) -> PartyIdentifier:
        identifier = self._identifiers.get(identifier_id)
        if identifier is None:
            raise PartyIdentityBlocked("unknown party identifier")
        return identifier


def _normalize_identifier(kind: IdentifierKind, value: str) -> str:
    normalized = "".join(value.strip().casefold().split())
    if kind in {IdentifierKind.PHONE, IdentifierKind.BANK_ACCOUNT}:
        normalized = normalized.replace("-", "")
    if not normalized:
        raise PartyIdentityBlocked("identifier value is required")
    return normalized


def _assert_no_identifier_conflicts(identifiers: tuple[PartyIdentifier, ...]) -> None:
    owner_by_key: dict[tuple[IdentifierKind, str], str] = {}
    for identifier in identifiers:
        key = (identifier.kind, identifier.normalization_key)
        owner = owner_by_key.get(key)
        if owner is not None and owner != identifier.party_id:
            raise PartyIdentityBlocked("formal identity snapshot contains a confirmed identifier conflict")
        owner_by_key[key] = identifier.party_id


def _validate_evidence(links: tuple[EvidenceLink, ...]) -> None:
    try:
        validate_evidence_links(links)
    except EvidenceReferenceBlocked as error:
        raise PartyIdentityBlocked(str(error)) from error


def _require_candidate_role(actor: Actor) -> None:
    if not actor.roles & {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER}:
        raise PartyIdentityBlocked("actor does not have a permitted role for this party-identity action")


def _require_lead(actor: Actor) -> None:
    if Role.LEAD_LAWYER not in actor.roles:
        raise PartyIdentityBlocked("lead lawyer role is required")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise PartyIdentityBlocked(f"{label} is required")


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(val) for key, val in asdict(item).items()}
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(normalize(value) for value in item)
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in item]
        return item

    encoded = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
