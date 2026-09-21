"""Deterministic, server-owned identities for lawyer Web Agent runs.

The browser supplies an idempotency key, never a run identifier.  Keeping the
derivation in this dependency-light module lets recovery orchestration compute
the exact run that the ordinary control service will create, including after a
response is lost, without introducing a circular import through ``web_app``.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from case_kernel.models import Actor


def derive_web_case_agent_entity_id(
    *, actor: Actor, matter_id: str, idempotency_key: str, entity: str
) -> str:
    """Return the deterministic goal/run/first-event identity for one intent."""

    identity_key = (
        f"lawcase-agent:{actor.firm_id}:{matter_id}:"
        f"{actor.actor_id}:{idempotency_key}"
    )
    suffix = "event:1" if entity == "event" else entity
    if entity not in {"goal", "run", "event"}:
        raise ValueError("case Agent entity kind is invalid")
    return str(uuid5(NAMESPACE_URL, f"{identity_key}:{suffix}"))


__all__ = ("derive_web_case_agent_entity_id",)
