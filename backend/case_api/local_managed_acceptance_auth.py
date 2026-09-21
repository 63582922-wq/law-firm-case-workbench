"""Synthetic lead-lawyer bootstrap for the isolated local acceptance stack.

This is not a production authentication alternative. It exists only so the
loopback-only, synthetic acceptance deployment can exercise the real browser
session, database authorization and audit path without repeatedly sending a
human through Keycloak MFA. Production composition remains fail-closed unless
the exact local gate is explicitly enabled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Request

from case_kernel.models import Actor, Role

from .persistent_identity import AuthenticationMethod, ServerIdentityContext
from .web_session import WebSessionAuthority, WebSessionBlocked, WebSessionGrant


_LOCAL_ACCEPTANCE_ORIGIN = "https://workbench.127.0.0.1.nip.io"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class LocalManagedAcceptanceSessionBootstrap:
    """Issue an ordinary opaque session for the fixed synthetic lead actor."""

    def __init__(
        self,
        *,
        public_origin: str,
        issuer: str,
        actor: Actor,
        session_authority: WebSessionAuthority,
        clock=None,
    ) -> None:
        if public_origin != _LOCAL_ACCEPTANCE_ORIGIN:
            raise ValueError("local acceptance authentication requires the fixed loopback origin")
        parsed_issuer = urlsplit(issuer)
        if (
            parsed_issuer.scheme != "https"
            or parsed_issuer.hostname != "identity.127.0.0.1.nip.io"
            or parsed_issuer.username is not None
            or parsed_issuer.password is not None
            or parsed_issuer.query
            or parsed_issuer.fragment
        ):
            raise ValueError("local acceptance authentication requires the fixed loopback issuer")
        if not isinstance(actor, Actor) or actor.roles != frozenset({Role.LEAD_LAWYER}):
            raise ValueError("local acceptance authentication requires one fixed lead lawyer")
        if not all(
            callable(getattr(session_authority, method, None))
            for method in ("issue", "resolve", "revoke", "clear_cookies")
        ):
            raise ValueError("local acceptance session authority is invalid")
        self._public_origin = public_origin
        self._issuer = issuer
        self._actor = actor
        self._session_authority = session_authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, *, request: Request) -> tuple[ServerIdentityContext, WebSessionGrant]:
        """Bootstrap only a safe, same-origin request with no bearer credential."""

        if not isinstance(request, Request) or request.method.upper() not in _SAFE_METHODS:
            raise WebSessionBlocked("local acceptance bootstrap requires a safe request")
        if request.headers.get("host", "") != urlsplit(self._public_origin).netloc:
            raise WebSessionBlocked("local acceptance bootstrap host is invalid")
        origin = request.headers.get("origin")
        if origin is not None and origin != self._public_origin:
            raise WebSessionBlocked("local acceptance bootstrap origin is invalid")
        if request.headers.get("authorization"):
            raise WebSessionBlocked("local acceptance bootstrap rejects bearer credentials")

        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise WebSessionBlocked("local acceptance bootstrap clock is invalid")
        now = now.astimezone(timezone.utc)
        issuance_identity = ServerIdentityContext(
            actor=self._actor,
            session_id=str(uuid4()),
            issuer=self._issuer,
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        grant = self._session_authority.issue(identity=issuance_identity)
        identity = ServerIdentityContext(
            actor=self._actor,
            session_id=grant.session_id,
            issuer=self._issuer,
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=now,
            expires_at=grant.expires_at,
        )
        identity.validate(now=now)
        return identity, grant


__all__ = ("LocalManagedAcceptanceSessionBootstrap",)
