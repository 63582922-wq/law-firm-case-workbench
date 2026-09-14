#!/usr/bin/env python3
"""Exercise real Authorization Code + PKCE + TOTP on a disposable fresh realm.

This probe intentionally enrolls the realm's synthetic user in TOTP.  It must
therefore never be used against the persistent realm prepared for a human
acceptance run.  No direct grant, hardcoded MFA claim, token print, or TOTP
secret file is used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import struct
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class AcceptanceBlocked(RuntimeError):
    pass


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self._current = {
                "id": values.get("id"),
                "action": values.get("action"),
                "inputs": {},
            }
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None and values.get("name"):
            self._current["inputs"][values["name"]] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def _private_json(path: Path, *, label: str) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise AcceptanceBlocked(f"{label} must be a regular 0600 file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptanceBlocked(f"{label} is unreadable") from error
    if not isinstance(value, dict):
        raise AcceptanceBlocked(f"{label} is malformed")
    return value


def _forms(body: bytes) -> list[dict[str, Any]]:
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AcceptanceBlocked("Keycloak login form is not UTF-8") from error
    parser = _Forms()
    parser.feed(decoded)
    return parser.forms


def _one_form(body: bytes, form_id: str) -> dict[str, Any]:
    matches = [form for form in _forms(body) if form.get("id") == form_id]
    if len(matches) != 1 or not matches[0].get("action"):
        raise AcceptanceBlocked(f"Keycloak did not present {form_id}")
    return matches[0]


def _location(headers: bytes) -> str | None:
    try:
        lines = headers.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise AcceptanceBlocked("Keycloak returned malformed headers") from error
    values = [line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("location:")]
    return values[-1] if values else None


def _curl_quote(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise AcceptanceBlocked("unsafe value reached the HTTPS probe")
    return json.dumps(value, ensure_ascii=False)


class _Https:
    def __init__(self, *, ca_file: Path, root: Path) -> None:
        self._ca_file = ca_file
        self._root = root
        self._counter = 0

    def request(
        self,
        *,
        stage: str,
        url: str,
        cookies: Path,
        data: list[tuple[str, str]] | None = None,
        basic_user: str | None = None,
    ) -> tuple[bytes, bytes]:
        self._counter += 1
        headers = self._root / f"{self._counter:02d}.headers"
        body = self._root / f"{self._counter:02d}.body"
        lines = [
            "silent",
            "show-error",
            "fail-with-body",
            f"cacert = {_curl_quote(str(self._ca_file))}",
            f"cookie-jar = {_curl_quote(str(cookies))}",
            f"dump-header = {_curl_quote(str(headers))}",
            f"output = {_curl_quote(str(body))}",
            f"url = {_curl_quote(url)}",
        ]
        if cookies.exists():
            lines.append(f"cookie = {_curl_quote(str(cookies))}")
        if basic_user is not None:
            lines.append(f"user = {_curl_quote(basic_user)}")
        for key, value in data or []:
            lines.append(f"data-urlencode = {_curl_quote(f'{key}={value}')}")
        result = subprocess.run(
            ["curl", "--config", "-"],
            input="\n".join(lines) + "\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AcceptanceBlocked(f"HTTPS request failed during {stage}")
        for path in (headers, body, cookies):
            if path.exists():
                os.chmod(path, 0o600)
        return headers.read_bytes(), body.read_bytes()


def _totp(secret: str) -> tuple[int, str]:
    counter = int(time.time() // 30)
    digest = hmac.new(secret.encode("utf-8"), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return counter, str(code).zfill(6)


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _begin_authorization(
    *,
    https: _Https,
    stage: str,
    authorization_endpoint: str,
    callback: str,
    client_id: str,
    username: str,
    password: str,
    cookies: Path,
) -> tuple[str, str, str, bytes, bytes]:
    verifier = secrets.token_urlsafe(64)[:86]
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    query = urlencode(
        {
            "response_type": "code",
            "response_mode": "query",
            "client_id": client_id,
            "redirect_uri": callback,
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    _, body = https.request(
        stage=f"{stage} authorization",
        url=f"{authorization_endpoint}?{query}",
        cookies=cookies,
    )
    login = _one_form(body, "kc-form-login")
    headers, body = https.request(
        stage=f"{stage} password",
        url=login["action"],
        cookies=cookies,
        data=[("username", username), ("password", password), ("credentialId", "")],
    )
    return verifier, state, nonce, headers, body


def _follow_required_action(
    *,
    https: _Https,
    stage: str,
    callback: str,
    cookies: Path,
    headers: bytes,
    body: bytes,
) -> tuple[bytes, bytes]:
    for index in range(4):
        if _forms(body):
            return headers, body
        location = _location(headers)
        if location is None or location.startswith(callback):
            return headers, body
        headers, body = https.request(
            stage=f"{stage} required action {index + 1}",
            url=location,
            cookies=cookies,
        )
    raise AcceptanceBlocked("Keycloak required-action chain is unexpectedly long")


def _callback_query(location: str | None, *, callback: str, state: str, issuer: str) -> dict[str, list[str]]:
    if location is None or not location.startswith(callback):
        raise AcceptanceBlocked("authorization did not return the exact callback")
    try:
        query = parse_qs(urlsplit(location).query, strict_parsing=True)
    except ValueError as error:
        raise AcceptanceBlocked("authorization callback is malformed") from error
    if set(query) != {"code", "state", "iss"}:
        raise AcceptanceBlocked(f"callback parameter allowlist failed: {sorted(query)}")
    if query["state"] != [state] or query["iss"] != [issuer] or len(query["code"]) != 1:
        raise AcceptanceBlocked("authorization callback correlation failed")
    return query


def _exchange(
    *,
    https: _Https,
    stage: str,
    token_endpoint: str,
    callback: str,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    cookies: Path,
) -> dict[str, Any]:
    _, body = https.request(
        stage=stage,
        url=token_endpoint,
        cookies=cookies,
        basic_user=f"{client_id}:{client_secret}",
        data=[
            ("grant_type", "authorization_code"),
            ("code", code),
            ("redirect_uri", callback),
            ("code_verifier", verifier),
        ],
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise AcceptanceBlocked("OIDC token response is malformed") from error
    if not isinstance(payload, dict):
        raise AcceptanceBlocked("OIDC token response is malformed")
    return payload


def _verify_id_token(
    *,
    compact: object,
    jwks: dict[str, Any],
    issuer: str,
    client_id: str,
    subject: str,
    nonce: str,
) -> None:
    if not isinstance(compact, str) or len(compact) > 16 * 1024:
        raise AcceptanceBlocked("OIDC response has no bounded ID token")
    parts = compact.split(".")
    if len(parts) != 3:
        raise AcceptanceBlocked("ID token is not a compact JWT")
    try:
        header = json.loads(_b64url(parts[0]))
        claims = json.loads(_b64url(parts[1]))
        signature = _b64url(parts[2])
    except (ValueError, json.JSONDecodeError) as error:
        raise AcceptanceBlocked("ID token is malformed") from error
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AcceptanceBlocked("ID token is malformed")
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise AcceptanceBlocked("ID token is not RS256 with a key id")
    keys = [
        key
        for key in jwks.get("keys", [])
        if isinstance(key, dict)
        and key.get("kid") == header["kid"]
        and key.get("kty") == "RSA"
        and key.get("use") == "sig"
        and key.get("alg") == "RS256"
    ]
    if len(keys) != 1:
        raise AcceptanceBlocked("exact RS256 signing key is unavailable")
    try:
        modulus = int.from_bytes(_b64url(keys[0]["n"]), "big")
        exponent = int.from_bytes(_b64url(keys[0]["e"]), "big")
        public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        public_key.verify(
            signature,
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as error:
        raise AcceptanceBlocked("ID token RS256 signature is invalid") from error
    required = {"iss", "sub", "aud", "exp", "nbf", "iat", "auth_time", "amr", "nonce"}
    missing = sorted(required.difference(claims))
    if missing:
        raise AcceptanceBlocked(f"ID token is missing claims: {missing}")
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    now = int(time.time())
    checks = {
        "issuer": claims["iss"] == issuer,
        "subject": claims["sub"] == subject,
        "audience": client_id in audiences,
        "authorized party": len(audiences) == 1 or claims.get("azp") == client_id,
        "numeric dates": all(type(claims[name]) is int for name in ("exp", "nbf", "iat", "auth_time")),
        "active time window": claims["nbf"] <= now + 30 < claims["exp"] + 30,
        "issued/authenticated time": claims["auth_time"] <= claims["iat"] + 30 <= now + 60,
        "maximum lifetime": claims["exp"] - claims["iat"] <= 3_630,
        "maximum authentication age": now - claims["auth_time"] <= 43_230,
        "real MFA AMR": isinstance(claims["amr"], list) and "mfa" in claims["amr"],
        "nonce": claims["nonce"] == nonce,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise AcceptanceBlocked(f"ID token contract failed: {failed}")


def run(*, realm_file: Path, contract_file: Path, ca_file: Path) -> None:
    if shutil.which("curl") is None:
        raise AcceptanceBlocked("curl is required")
    realm = _private_json(realm_file, label="rendered realm")
    contract = _private_json(contract_file, label="OIDC contract")
    clients = [item for item in realm.get("clients", []) if item.get("clientId") == contract.get("client_id")]
    users = [item for item in realm.get("users", []) if item.get("id") == contract.get("subject")]
    if len(clients) != 1 or len(users) != 1:
        raise AcceptanceBlocked("realm does not match the OIDC contract")
    client = clients[0]
    user = users[0]
    credentials = user.get("credentials")
    if not isinstance(credentials, list) or len(credentials) != 1:
        raise AcceptanceBlocked("fresh synthetic user password is unavailable")
    work = Path(tempfile.mkdtemp(prefix="lawcase-keycloak-acceptance-"))
    os.chmod(work, 0o700)
    try:
        https = _Https(ca_file=ca_file, root=work)
        cookies_one = work / "enrollment.cookies"
        verifier_one, state_one, _, headers, body = _begin_authorization(
            https=https,
            stage="TOTP enrollment",
            authorization_endpoint=contract["authorization_endpoint"],
            callback=contract["callback_uri"],
            client_id=contract["client_id"],
            username=user["username"],
            password=credentials[0]["value"],
            cookies=cookies_one,
        )
        headers, body = _follow_required_action(
            https=https,
            stage="TOTP enrollment",
            callback=contract["callback_uri"],
            cookies=cookies_one,
            headers=headers,
            body=body,
        )
        setup = _one_form(body, "kc-totp-settings-form")
        raw_secret = setup["inputs"].get("totpSecret")
        if not isinstance(raw_secret, str) or not raw_secret:
            raise AcceptanceBlocked("Keycloak did not issue a TOTP enrollment secret")
        enrollment_counter, enrollment_code = _totp(raw_secret)
        headers, body = https.request(
            stage="TOTP enrollment confirmation",
            url=setup["action"],
            cookies=cookies_one,
            data=[
                ("totp", enrollment_code),
                ("totpSecret", raw_secret),
                ("userLabel", "Disposable protocol acceptance"),
                ("logout-sessions", "on"),
            ],
        )
        headers, body = _follow_required_action(
            https=https,
            stage="post-enrollment",
            callback=contract["callback_uri"],
            cookies=cookies_one,
            headers=headers,
            body=body,
        )
        forms = _forms(body)
        if forms:
            profile = _one_form(body, "kc-update-profile-form")
            headers, body = https.request(
                stage="synthetic profile completion",
                url=profile["action"],
                cookies=cookies_one,
                data=[
                    ("email", user["email"]),
                    ("firstName", user["firstName"]),
                    ("lastName", user["lastName"]),
                ],
            )
        first_callback = _callback_query(
            _location(headers),
            callback=contract["callback_uri"],
            state=state_one,
            issuer=contract["issuer"],
        )
        _exchange(
            https=https,
            stage="enrollment authorization-code consumption",
            token_endpoint=contract["token_endpoint"],
            callback=contract["callback_uri"],
            client_id=contract["client_id"],
            client_secret=client["secret"],
            code=first_callback["code"][0],
            verifier=verifier_one,
            cookies=cookies_one,
        )
        while int(time.time() // 30) <= enrollment_counter:
            time.sleep(0.2)

        cookies_two = work / "verification.cookies"
        verifier, state, nonce, headers, body = _begin_authorization(
            https=https,
            stage="password plus TOTP",
            authorization_endpoint=contract["authorization_endpoint"],
            callback=contract["callback_uri"],
            client_id=contract["client_id"],
            username=user["username"],
            password=credentials[0]["value"],
            cookies=cookies_two,
        )
        otp_form = _one_form(body, "kc-otp-login-form")
        _, otp_code = _totp(raw_secret)
        headers, _ = https.request(
            stage="real TOTP second factor",
            url=otp_form["action"],
            cookies=cookies_two,
            data=[
                ("selectedCredentialId", otp_form["inputs"].get("selectedCredentialId", "")),
                ("otp", otp_code),
            ],
        )
        callback = _callback_query(
            _location(headers),
            callback=contract["callback_uri"],
            state=state,
            issuer=contract["issuer"],
        )
        token = _exchange(
            https=https,
            stage="verified authorization-code exchange",
            token_endpoint=contract["token_endpoint"],
            callback=contract["callback_uri"],
            client_id=contract["client_id"],
            client_secret=client["secret"],
            code=callback["code"][0],
            verifier=verifier,
            cookies=cookies_two,
        )
        _, jwks_body = https.request(
            stage="JWKS retrieval",
            url=contract["jwks_url"],
            cookies=cookies_two,
        )
        try:
            jwks = json.loads(jwks_body)
        except json.JSONDecodeError as error:
            raise AcceptanceBlocked("JWKS is malformed") from error
        _verify_id_token(
            compact=token.get("id_token"),
            jwks=jwks,
            issuer=contract["issuer"],
            client_id=contract["client_id"],
            subject=contract["subject"],
            nonce=nonce,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realm-file", required=True, type=Path)
    parser.add_argument("--contract-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument(
        "--fresh-disposable-realm",
        action="store_true",
        help="acknowledge that this consumes the synthetic user's first TOTP enrollment",
    )
    args = parser.parse_args()
    if not args.fresh_disposable_realm:
        print("Acceptance blocked: --fresh-disposable-realm acknowledgement is required")
        return 2
    try:
        run(realm_file=args.realm_file, contract_file=args.contract_file, ca_file=args.ca_file)
    except (OSError, KeyError, TypeError, AcceptanceBlocked) as error:
        print(f"Keycloak authorization acceptance failed: {error}")
        return 1
    print("Keycloak production Authorization Code + PKCE + real TOTP PASS")
    print("RS256 and iss/sub/aud/exp/nbf/iat/auth_time/amr/nonce claims PASS")
    print("Callback allowlist code/state/iss PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
