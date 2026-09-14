#!/usr/bin/env python3
"""Wait for the isolated KES server through its CA-verified HTTPS endpoint."""

from __future__ import annotations

import os
import ssl
import sys
import time
from urllib.request import urlopen


def main() -> int:
    ca = os.environ.get("LAWCASE_KES_CA", "/tls/ca.crt")
    context = ssl.create_default_context(cafile=ca)
    context.load_cert_chain(
        certfile=os.environ.get("LAWCASE_KES_CLIENT_CERT", "/tls/kes-client.crt"),
        keyfile=os.environ.get("LAWCASE_KES_CLIENT_KEY", "/tls/kes-client.key"),
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urlopen("https://kes:7373/v1/ready", context=context, timeout=3) as response:
                ready = response.status == 200
            # /v1/ready is deliberately unauthenticated for readiness.  The
            # status endpoint is policy-protected and therefore proves that
            # the presented MinIO client certificate maps to the allowed KES
            # identity, not merely that the server certificate is trusted.
            with urlopen("https://kes:7373/v1/status", context=context, timeout=3) as response:
                authorized = response.status == 200
                response.read(64 * 1024)
            if ready and authorized:
                print("KES CA-verified TLS and policy-authorized mTLS identity: PASS")
                return 0
        except Exception:
            time.sleep(2)
    print("KES did not become ready with the policy-authorized mTLS identity", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
