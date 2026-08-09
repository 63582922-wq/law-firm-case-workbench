"""Server-derived identity contract for the separate persistent preview API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

from fastapi import Request

from case_kernel.models import Actor


class AuthenticationMethod(str, Enum):
    OIDC_MFA = "OIDC_MFA"
    OS_BOUND_LOCAL_SESSION = "OS_BOUND_LOCAL_SESSION"


class PersistentAuthenticationBlocked(PermissionError):
    """No valid strong server-side identity is available for persistent access."""


@dataclass(frozen=True)
class ServerIdentityContext:
    actor: Actor
    session_id: str
    issuer: str
    authentication_method: AuthenticationMethod
    authenticated_at: datetime
    expires_at: datetime

    def validate(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        for label, value in (
            ("actor_id", self.actor.actor_id),
            ("firm_id", self.actor.firm_id),
            ("session_id", self.session_id),
        ):
            try:
                UUID(value)
            except (TypeError, ValueError) as error:
                raise PersistentAuthenticationBlocked(f"persistent identity requires UUID {label}") from error
        if self.authenticated_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise PersistentAuthenticationBlocked("persistent identity timestamps must be timezone-aware")
        if not self.issuer.strip():
            raise PersistentAuthenticationBlocked("persistent identity issuer is required")
        if self.authenticated_at > current:
            raise PersistentAuthenticationBlocked("persistent identity authentication time is in the future")
        if self.expires_at <= current:
            raise PersistentAuthenticationBlocked("persistent identity session has expired")


class ServerIdentityResolver(Protocol):
    """Implemented by desktop OS-bound session or an OIDC/MFA middleware."""

    async def resolve(self, request: Request) -> ServerIdentityContext: ...


class RejectAllIdentityResolver:
    async def resolve(self, request: Request) -> ServerIdentityContext:
        del request
        raise PersistentAuthenticationBlocked("persistent identity provider is not configured")
