"""In-memory test repository preserving the persistence semantics required later."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import RLock
from typing import Callable, Protocol

from .errors import IdempotencyConflict, VersionConflict
from .models import Actor, AuditEvent, CommandReceipt, Matter


@dataclass(frozen=True)
class StoredCommand:
    payload_hash: str
    receipt: CommandReceipt


class MatterStore(Protocol):
    """Persistence port. Concrete adapters must preserve one-transaction command semantics."""

    def create(self, *, matter: Matter, actor: Actor, idempotency_key: str) -> CommandReceipt: ...

    def get(self, matter_id: str, *, firm_id: str | None = None) -> Matter: ...

    def audit_events(self, matter_id: str, *, firm_id: str | None = None) -> list[AuditEvent]: ...

    def mutate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        command_name: str,
        idempotency_key: str,
        expected_version: int,
        payload: dict[str, str],
        mutate_matter: Callable[[Matter], AuditEvent],
    ) -> CommandReceipt: ...


class InMemoryMatterStore:
    """A test-only repository with atomic version and idempotency checks under one lock."""

    def __init__(self) -> None:
        self._matters: dict[str, Matter] = {}
        self._audit_events: list[AuditEvent] = []
        self._commands: dict[tuple[str, str, str, str], StoredCommand] = {}
        self._lock = RLock()

    def create(self, *, matter: Matter, actor: Actor, idempotency_key: str) -> CommandReceipt:
        """Create a matter with the same idempotency and audit semantics as every mutation."""
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        payload = {"command": "CREATE_MATTER", "title": matter.title}
        payload_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        command_key = (actor.actor_id, matter.matter_id, "CREATE_MATTER", idempotency_key)
        with self._lock:
            prior = self._commands.get(command_key)
            if prior:
                if prior.payload_hash != payload_hash:
                    raise IdempotencyConflict("idempotency key was reused with different input")
                return prior.receipt
            if matter.matter_id in self._matters:
                raise ValueError(f"matter already exists: {matter.matter_id}")
            self._matters[matter.matter_id] = deepcopy(matter)
            event = AuditEvent.create(
                matter=matter,
                actor=actor,
                event_type="MATTER_CREATED",
                input_version=0,
                payload={"title": matter.title},
            )
            self._audit_events.append(event)
            receipt = CommandReceipt(
                command_name="CREATE_MATTER",
                idempotency_key=idempotency_key,
                matter_id=matter.matter_id,
                matter_version=matter.version,
                audit_event_id=event.event_id,
            )
            self._commands[command_key] = StoredCommand(payload_hash, receipt)
            return receipt

    def get(self, matter_id: str, *, firm_id: str | None = None) -> Matter:
        with self._lock:
            matter = self._matters[matter_id]
            return deepcopy(matter)

    def audit_events(self, matter_id: str, *, firm_id: str | None = None) -> list[AuditEvent]:
        with self._lock:
            matter = self._matters[matter_id]
            return [deepcopy(event) for event in self._audit_events if event.matter_id == matter_id]

    def mutate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        command_name: str,
        idempotency_key: str,
        expected_version: int,
        payload: dict[str, str],
        mutate_matter: Callable[[Matter], AuditEvent],
    ) -> CommandReceipt:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        payload_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        command_key = (actor.actor_id, matter_id, command_name, idempotency_key)

        with self._lock:
            prior = self._commands.get(command_key)
            if prior:
                if prior.payload_hash != payload_hash:
                    raise IdempotencyConflict("idempotency key was reused with different input")
                return prior.receipt

            matter = self._matters[matter_id]
            if matter.version != expected_version:
                raise VersionConflict(
                    f"expected matter version {expected_version}, current version is {matter.version}"
                )
            event = mutate_matter(matter)
            self._audit_events.append(event)
            receipt = CommandReceipt(
                command_name=command_name,
                idempotency_key=idempotency_key,
                matter_id=matter_id,
                matter_version=matter.version,
                audit_event_id=event.event_id,
            )
            self._commands[command_key] = StoredCommand(payload_hash, receipt)
            return receipt
