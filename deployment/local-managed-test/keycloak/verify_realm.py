#!/usr/bin/env python3
"""Fail-closed static verification for a rendered lawcase-test realm."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
from urllib.parse import urlsplit


def _only(items: list[dict], key: str, value: str, label: str) -> dict:
    matches = [item for item in items if item.get(key) == value]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {label}")
    return matches[0]


def _enabled(mapper: dict, name: str) -> bool:
    return mapper.get("config", {}).get(name) == "true"


def verify(path: Path, expected_workbench_origin: str | None) -> dict[str, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("rendered realm must be a regular 0600 file")
    realm = json.loads(path.read_text(encoding="utf-8"))
    if realm.get("realm") != "lawcase-test" or realm.get("enabled") is not True:
        raise ValueError("realm identity is invalid")
    if realm.get("sslRequired") != "all" or realm.get("defaultSignatureAlgorithm") != "RS256":
        raise ValueError("realm TLS/signature policy is invalid")
    if not isinstance(realm.get("notBefore"), int):
        raise ValueError("realm must emit an explicit nbf boundary")
    if realm.get("registrationAllowed") or realm.get("resetPasswordAllowed") or realm.get("rememberMe"):
        raise ValueError("realm exposes an unintended user-controlled login path")
    if realm.get("browserFlow") != "lawcase-browser-mfa":
        raise ValueError("realm does not bind the mandatory MFA flow")
    flows = realm.get("authenticationFlows")
    if not isinstance(flows, list):
        raise ValueError("authentication flows are missing")
    flow = _only(flows, "alias", "lawcase-browser-mfa", "browser MFA flow")
    executions = flow.get("authenticationExecutions")
    if not isinstance(executions, list) or len(executions) != 2:
        raise ValueError("browser MFA flow must contain exactly password and TOTP")
    password = _only(executions, "authenticator", "auth-username-password-form", "password execution")
    otp = _only(executions, "authenticator", "auth-otp-form", "TOTP execution")
    if any(item.get("requirement") != "REQUIRED" for item in (password, otp)):
        raise ValueError("password and TOTP must both be REQUIRED")
    configs = realm.get("authenticatorConfig")
    if not isinstance(configs, list):
        raise ValueError("authenticator AMR configs are missing")
    otp_config = _only(configs, "alias", otp.get("authenticatorConfig"), "TOTP AMR config")
    if otp_config.get("config", {}).get("default.reference.value") != "mfa":
        raise ValueError("TOTP execution does not produce the mfa AMR reference")
    clients = realm.get("clients")
    if not isinstance(clients, list):
        raise ValueError("clients are missing")
    client = _only(clients, "clientId", "lawcase-web-workbench", "workbench client")
    if client.get("publicClient") is not False or client.get("clientAuthenticatorType") != "client-secret":
        raise ValueError("workbench client is not confidential")
    if not client.get("standardFlowEnabled") or client.get("implicitFlowEnabled") or client.get("directAccessGrantsEnabled"):
        raise ValueError("workbench client grant policy is invalid")
    if client.get("attributes", {}).get("pkce.code.challenge.method") != "S256":
        raise ValueError("workbench client does not require PKCE S256")
    if client.get("attributes", {}).get("id.token.signed.response.alg") != "RS256":
        raise ValueError("workbench ID token algorithm is not RS256")
    if client.get("attributes", {}).get("exclude.session.state.from.auth.response") != "true":
        raise ValueError("authorization response includes a callback parameter rejected by the workbench")
    redirects = client.get("redirectUris")
    if not isinstance(redirects, list) or len(redirects) != 1:
        raise ValueError("workbench client must have one exact callback")
    callback = redirects[0]
    parsed_callback = urlsplit(callback)
    if parsed_callback.scheme != "https" or parsed_callback.path != "/api/v1/auth/oidc/callback":
        raise ValueError("workbench callback is invalid")
    origin = f"{parsed_callback.scheme}://{parsed_callback.netloc}"
    if expected_workbench_origin is not None and origin != expected_workbench_origin:
        raise ValueError("workbench callback origin does not match the expected origin")
    if client.get("webOrigins") != [origin]:
        raise ValueError("workbench Web origin is not exact")
    mappers = client.get("protocolMappers")
    if not isinstance(mappers, list):
        raise ValueError("OIDC protocol mappers are missing")
    amr = _only(mappers, "protocolMapper", "oidc-amr-mapper", "AMR mapper")
    audience = _only(mappers, "protocolMapper", "oidc-audience-mapper", "audience mapper")
    auth_time = _only(mappers, "name", "lawcase-auth-time", "auth_time mapper")
    not_before = _only(mappers, "name", "lawcase-not-before", "nbf mapper")
    if not _enabled(amr, "id.token.claim"):
        raise ValueError("AMR is not included in the ID token")
    if audience.get("config", {}).get("included.client.audience") != "lawcase-web-workbench" or not _enabled(audience, "id.token.claim"):
        raise ValueError("workbench audience is not included in the ID token")
    if auth_time.get("protocolMapper") != "oidc-usersessionmodel-note-mapper" or auth_time.get("config", {}).get("user.session.note") != "AUTH_TIME" or not _enabled(auth_time, "id.token.claim"):
        raise ValueError("auth_time is not included in the ID token")
    if not_before.get("protocolMapper") != "oidc-hardcoded-claim-mapper" or not_before.get("config", {}).get("claim.value") != "0" or not_before.get("config", {}).get("jsonType.label") != "long" or not _enabled(not_before, "id.token.claim"):
        raise ValueError("nbf=0 is not included in the ID token")
    users = realm.get("users")
    if not isinstance(users, list) or len(users) != 1:
        raise ValueError("realm must contain exactly one test user")
    user = users[0]
    if user.get("id") != "22222222-2222-4222-8222-222222222222":
        raise ValueError("test OIDC subject is not the fixed lead actor")
    if user.get("requiredActions") != ["CONFIGURE_TOTP"]:
        raise ValueError("test user does not require first-login TOTP enrollment")
    forbidden = json.dumps(mappers, ensure_ascii=False).lower()
    if "firm_id" in forbidden or "active_human_roles" in forbidden or "lead_lawyer" in forbidden:
        raise ValueError("browser token must not carry firm or application-role authority")
    return {
        "issuer_origin": realm.get("attributes", {}).get("frontendUrl", ""),
        "callback": callback,
        "subject": user["id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("realm", type=Path)
    parser.add_argument("--expected-workbench-origin")
    args = parser.parse_args()
    try:
        result = verify(args.realm, args.expected_workbench_origin)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Keycloak realm verification failed: {error}")
        return 1
    print("Keycloak realm contract PASS")
    print(f"issuer origin: {result['issuer_origin']}")
    print(f"callback: {result['callback']}")
    print(f"fixed subject: {result['subject']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
