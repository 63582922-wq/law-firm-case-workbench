from __future__ import annotations

from base64 import urlsafe_b64encode
import json
import unittest
from uuid import uuid4

from case_kernel.stable_pagination import (
    StablePaginationBlocked,
    decode_page_cursor,
    encode_page_cursor,
    validate_page_limit,
)


class StablePaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())

    def test_cursor_round_trip_is_canonical_version_and_matter_bound(self) -> None:
        token = encode_page_cursor(
            kind="FACTS",
            matter_id=self.matter_id,
            matter_version=7,
            sort_values=("2026-08-10T12:00:00Z", str(uuid4())),
        )
        cursor = decode_page_cursor(
            token,
            expected_kind="FACTS",
            expected_matter_id=self.matter_id,
        )
        self.assertEqual(cursor.matter_version, 7)
        self.assertEqual(len(cursor.sort_values), 2)
        with self.assertRaisesRegex(StablePaginationBlocked, "scope"):
            decode_page_cursor(
                token,
                expected_kind="TRANSACTIONS",
                expected_matter_id=self.matter_id,
            )
        with self.assertRaisesRegex(StablePaginationBlocked, "scope"):
            decode_page_cursor(
                token,
                expected_kind="FACTS",
                expected_matter_id=str(uuid4()),
            )

    def test_unknown_duplicate_and_noncanonical_fields_are_rejected(self) -> None:
        payload = {
            "kind": "FACTS",
            "matter_id": self.matter_id,
            "matter_version": 7,
            "sort_values": ["2026-08-10T12:00:00Z", str(uuid4())],
            "v": 1,
            "actor_id": str(uuid4()),
        }
        token = urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        with self.assertRaisesRegex(StablePaginationBlocked, "fields"):
            decode_page_cursor(
                token,
                expected_kind="FACTS",
                expected_matter_id=self.matter_id,
            )

        duplicate = (
            '{"kind":"FACTS","kind":"FACTS","matter_id":"'
            + self.matter_id
            + '","matter_version":7,"sort_values":["x"],"v":1}'
        )
        token = urlsafe_b64encode(duplicate.encode("utf-8")).decode("ascii").rstrip("=")
        with self.assertRaisesRegex(StablePaginationBlocked, "duplicate"):
            decode_page_cursor(
                token,
                expected_kind="FACTS",
                expected_matter_id=self.matter_id,
            )

    def test_page_size_and_sort_keys_are_bounded(self) -> None:
        self.assertEqual(validate_page_limit(1), 1)
        self.assertEqual(validate_page_limit(100), 100)
        for invalid in (True, 0, 101, "50"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(StablePaginationBlocked):
                    validate_page_limit(invalid)
        with self.assertRaises(StablePaginationBlocked):
            encode_page_cursor(
                kind="FACTS",
                matter_id=self.matter_id,
                matter_version=1,
                sort_values=("x" * 97,),
            )


if __name__ == "__main__":
    unittest.main()
