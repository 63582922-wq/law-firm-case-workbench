from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from case_api.web_jwks import CachedHttpsJwksProvider, JwksFetchPolicy, WebJwksBlocked


class _Fetcher:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def fetch(self, *, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
        self.calls.append({"url": url, "timeout_seconds": timeout_seconds, "max_bytes": max_bytes})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class WebJwksTests(unittest.TestCase):
    def test_policy_requires_one_canonical_https_issuer_origin(self) -> None:
        with self.assertRaises(ValueError):
            JwksFetchPolicy(issuer="http://id.example.test", jwks_url="http://id.example.test/keys")
        with self.assertRaises(ValueError):
            JwksFetchPolicy(
                issuer="https://id.example.test/realm",
                jwks_url="https://keys.example.test/realm/keys",
            )
        policy = JwksFetchPolicy(
            issuer="https://id.example.test/realm",
            jwks_url="https://id.example.test/realm/keys",
        )
        self.assertEqual(policy.jwks_url, "https://id.example.test/realm/keys")

    def test_cache_is_bounded_defensive_and_unknown_key_path_can_force_refresh(self) -> None:
        now = datetime(2026, 8, 11, 10, tzinfo=timezone.utc)
        clock = lambda: now
        first = json.dumps({"keys": [{"kid": "before"}]}).encode()
        second = json.dumps({"keys": [{"kid": "after"}]}).encode()
        fetcher = _Fetcher([first, second])
        provider = CachedHttpsJwksProvider(
            policy=JwksFetchPolicy(
                issuer="https://id.example.test/realm",
                jwks_url="https://id.example.test/realm/keys",
                refresh_interval=timedelta(minutes=5),
            ),
            fetcher=fetcher,
            clock=clock,
        )

        document = provider.load_jwks()
        document["keys"][0]["kid"] = "mutated"
        self.assertEqual(provider.load_jwks()["keys"][0]["kid"], "before")
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(provider.refresh_jwks()["keys"][0]["kid"], "after")
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(fetcher.calls[0]["url"], "https://id.example.test/realm/keys")

    def test_malformed_or_unavailable_document_blocks_without_reusing_expired_keys(self) -> None:
        now = datetime(2026, 8, 11, 10, tzinfo=timezone.utc)
        mutable_now = [now]
        fetcher = _Fetcher(
            [
                json.dumps({"keys": [{"kid": "current"}]}).encode(),
                RuntimeError("network detail must not leak"),
            ]
        )
        provider = CachedHttpsJwksProvider(
            policy=JwksFetchPolicy(
                issuer="https://id.example.test/realm",
                jwks_url="https://id.example.test/realm/keys",
                refresh_interval=timedelta(seconds=15),
            ),
            fetcher=fetcher,
            clock=lambda: mutable_now[0],
        )
        provider.load_jwks()
        mutable_now[0] = now + timedelta(seconds=16)
        with self.assertRaises(WebJwksBlocked) as blocked:
            provider.load_jwks()
        self.assertNotIn("network detail", str(blocked.exception))

        malformed = CachedHttpsJwksProvider(
            policy=JwksFetchPolicy(
                issuer="https://id.example.test/realm",
                jwks_url="https://id.example.test/realm/keys",
            ),
            fetcher=_Fetcher([b'{"keys": [{"kid": "one"}, {"kid": "two"}], "keys": []}']),
            clock=lambda: now,
        )
        with self.assertRaises(WebJwksBlocked):
            malformed.load_jwks()


if __name__ == "__main__":
    unittest.main()
