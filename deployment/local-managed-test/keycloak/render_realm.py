#!/usr/bin/env python3
"""Render the local managed-test Keycloak realm without logging secrets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID


_HERE = Path(__file__).resolve().parent
_TEMPLATE = _HERE / "lawcase-test-realm.template.json"
_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}\Z")
_MARKER = re.compile(r"__LAWCASE_[A-Z0-9_]+__")
_FIXED_REALM = "lawcase-test"
_FIXED_CLIENT = "lawcase-web-workbench"


class RealmRenderError(RuntimeError):
    pass


def _required(
    environ: Mapping[str, str],
    name: str,
    *,
    minimum: int = 1,
    maximum: int = 4096,
) -> str:
    value = environ.get(name)
    if value is None or value != value.strip() or not minimum <= len(value) <= maximum:
        raise RealmRenderError(f"{name} is missing or invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RealmRenderError(f"{name} contains control characters")
    if value.startswith("REPLACE_WITH_"):
        raise RealmRenderError(f"{name} is still a placeholder")
    return value


def _fixed(environ: Mapping[str, str], name: str, expected: str) -> str:
    value = environ.get(name, expected)
    if value != expected:
        raise RealmRenderError(f"{name} must be {expected!r} for this test realm")
    return value


def _host(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name, maximum=253)
    if not _HOST.fullmatch(value) or value.lower() in {"localhost", "localhost.localdomain"}:
        raise RealmRenderError(f"{name} must be a DNS hostname without scheme, port, or path")
    return value.lower()


def _origin(host: str, port: int) -> str:
    suffix = "" if port == 443 else f":{port}"
    value = f"https://{host}{suffix}"
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.path or parsed.query or parsed.fragment:
        raise RealmRenderError("derived HTTPS origin is invalid")
    return value


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, str):
        rendered = value
        for marker, replacement in replacements.items():
            rendered = rendered.replace(marker, replacement)
        if _MARKER.search(rendered):
            raise RealmRenderError("realm template contains an unresolved marker")
        return rendered
    return value


def _secure_write(path: Path, payload: bytes) -> None:
    parent = path.parent
    if parent.is_symlink():
        raise RealmRenderError("output directory must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_mode = stat.S_IMODE(parent.stat().st_mode)
    if parent_mode & 0o077:
        raise RealmRenderError("output directory must not be accessible by group or others")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RealmRenderError("output path must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_env_file(path: Path) -> dict[str, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise RealmRenderError("environment file must be a regular 0600 file")
    if info.st_size > 128 * 1024:
        raise RealmRenderError("environment file is unexpectedly large")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise RealmRenderError(f"environment file line {line_number} is not KEY=VALUE")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            raise RealmRenderError(f"environment file line {line_number} has an invalid key")
        if name in values:
            raise RealmRenderError(f"environment file repeats {name}")
        if value.startswith(("'", '"')) or value.endswith(("'", '"')):
            raise RealmRenderError(f"environment file {name} must not use shell quoting")
        if "\x00" in value:
            raise RealmRenderError(f"environment file {name} contains a NUL byte")
        values[name] = value
    return values


def render(
    output: Path,
    contract_output: Path | None,
    environ: Mapping[str, str] | None = None,
) -> None:
    source = os.environ if environ is None else environ
    _fixed(source, "LAWCASE_KEYCLOAK_REALM", _FIXED_REALM)
    _fixed(source, "LAWCASE_KEYCLOAK_CLIENT_ID", _FIXED_CLIENT)
    workbench_host = _host(source, "LAWCASE_WORKBENCH_HOST")
    identity_host = _host(source, "LAWCASE_IDENTITY_HOST")
    try:
        port = int(_required(source, "LAWCASE_HTTPS_PORT", maximum=5))
    except ValueError as error:
        raise RealmRenderError("LAWCASE_HTTPS_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise RealmRenderError("LAWCASE_HTTPS_PORT is out of range")
    workbench_origin = _origin(workbench_host, port)
    identity_origin = _origin(identity_host, port)
    subject = _required(source, "LAWCASE_TEST_LEAD_SUBJECT", maximum=255)
    actor_id = _required(source, "LAWCASE_TEST_LEAD_ACTOR_ID", maximum=255)
    try:
        subject_uuid = UUID(subject)
        actor_uuid = UUID(actor_id)
    except ValueError as error:
        raise RealmRenderError("lead subject and actor id must be UUIDs") from error
    if subject_uuid != actor_uuid:
        raise RealmRenderError("lead subject and actor id must match in this deterministic test realm")
    username = _required(source, "LAWCASE_TEST_LEAD_USERNAME", minimum=3, maximum=64)
    if not _USERNAME.fullmatch(username):
        raise RealmRenderError("LAWCASE_TEST_LEAD_USERNAME is invalid")
    client_secret = _required(source, "LAWCASE_OIDC_CLIENT_SECRET", minimum=32, maximum=512)
    lead_password = _required(source, "LAWCASE_TEST_LEAD_PASSWORD", minimum=16, maximum=512)
    callback = f"{workbench_origin}/api/v1/auth/oidc/callback"
    template = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
    rendered = _replace(
        template,
        {
            "__LAWCASE_IDENTITY_ORIGIN__": identity_origin,
            "__LAWCASE_WORKBENCH_ORIGIN__": workbench_origin,
            "__LAWCASE_WORKBENCH_CALLBACK__": callback,
            "__LAWCASE_OIDC_CLIENT_SECRET__": client_secret,
            "__LAWCASE_TEST_LEAD_SUBJECT__": subject,
            "__LAWCASE_TEST_LEAD_USERNAME__": username,
            "__LAWCASE_TEST_LEAD_PASSWORD__": lead_password,
        },
    )
    payload = (json.dumps(rendered, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _secure_write(output.resolve(), payload)
    if contract_output is not None:
        issuer = f"{identity_origin}/realms/{_FIXED_REALM}"
        contract = {
            "realm": _FIXED_REALM,
            "client_id": _FIXED_CLIENT,
            "audience": _FIXED_CLIENT,
            "subject": subject,
            "username": username,
            "workbench_origin": workbench_origin,
            "identity_origin": identity_origin,
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/protocol/openid-connect/auth",
            "token_endpoint": f"{issuer}/protocol/openid-connect/token",
            "jwks_url": f"{issuer}/protocol/openid-connect/certs",
            "callback_uri": callback,
            "required_amr": ["mfa"],
            "signature_algorithm": "RS256",
        }
        _secure_write(
            contract_output.resolve(),
            (json.dumps(contract, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--contract-output", type=Path)
    args = parser.parse_args()
    try:
        environ = _read_env_file(args.env_file) if args.env_file is not None else None
        render(args.output, args.contract_output, environ)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RealmRenderError) as error:
        print(f"Keycloak realm render blocked: {error}", file=os.sys.stderr)
        return 2
    print("Keycloak realm and OIDC contract rendered; secret values were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
