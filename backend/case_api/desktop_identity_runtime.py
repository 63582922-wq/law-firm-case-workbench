"""Re-verify a saved desktop enrollment before creating a local session.

Trust readiness, Keychain credential validity and database matter authority are
separate gates. This module only establishes the enrolled person identity; it
does not open case routes or infer matter membership.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .desktop_enrollment import (
    DesktopEnrollment,
    DesktopEnrollmentBlocked,
    MACOS_KEYCHAIN_ENROLLMENT_ACCOUNT,
    MACOS_KEYCHAIN_INSTALLATION_ACCOUNT,
    MACOS_KEYCHAIN_SERVICE,
    MacOSKeychainDesktopEnrollmentProvider,
    SignedDesktopEnrollmentVerifier,
    create_enrolled_desktop_session_authority,
)
from .desktop_trust_bootstrap import DesktopEnrollmentTrustRuntime
from .persistent_identity import DesktopSessionAuthority, PersistentAuthenticationBlocked


@dataclass(frozen=True)
class DesktopIdentityRuntime:
    phase: str
    message: str
    enrollment_id: str | None = None
    expires_at: str | None = None
    session_authority: DesktopSessionAuthority | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _VerifiedEnrollmentProvider:
    enrollment: DesktopEnrollment = field(repr=False)

    def load(self) -> DesktopEnrollment:
        return self.enrollment


def load_desktop_identity(
    *,
    trust: DesktopEnrollmentTrustRuntime,
    bootstrap_token: str,
    clock: Callable[[], datetime] | None = None,
    platform_name: str | None = None,
    runner=None,
    token_factory=None,
) -> DesktopIdentityRuntime:
    if trust.phase == "NOT_CONFIGURED":
        return DesktopIdentityRuntime(
            phase="NOT_ENROLLED",
            message="生产信任目录尚未配置；未读取本机登记。",
        )
    if trust.phase != "READY" or trust.catalog is None:
        return blocked_desktop_identity("生产信任目录不可用；本机登记未装配。")
    try:
        verifier = SignedDesktopEnrollmentVerifier(
            trusted_catalog=trust.catalog,
            clock=clock,
        )
        provider = MacOSKeychainDesktopEnrollmentProvider(
            service=MACOS_KEYCHAIN_SERVICE,
            enrollment_account=MACOS_KEYCHAIN_ENROLLMENT_ACCOUNT,
            installation_secret_account=MACOS_KEYCHAIN_INSTALLATION_ACCOUNT,
            verifier=verifier,
            platform_name=platform_name,
            runner=runner,
        )
        enrollment = provider.load_optional()
        if enrollment is None:
            return DesktopIdentityRuntime(
                phase="NOT_ENROLLED",
                message="Keychain 中尚无律所签名登记。",
            )
        authority = create_enrolled_desktop_session_authority(
            enrollment_provider=_VerifiedEnrollmentProvider(enrollment),
            bootstrap_token=bootstrap_token,
            clock=clock,
            token_factory=token_factory,
        )
    except (DesktopEnrollmentBlocked, PersistentAuthenticationBlocked):
        return blocked_desktop_identity(
            "本机登记未通过当前信任目录、设备绑定或时效核验。"
        )
    return DesktopIdentityRuntime(
        phase="ENROLLED",
        message="本机登记已按当前信任目录重新验签；尚未核验本案数据库权限。",
        enrollment_id=enrollment.enrollment_id,
        expires_at=enrollment.expires_at.isoformat().replace("+00:00", "Z"),
        session_authority=authority,
    )


def blocked_desktop_identity(message: str) -> DesktopIdentityRuntime:
    return DesktopIdentityRuntime(phase="BLOCKED", message=message)
