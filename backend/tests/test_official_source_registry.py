from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse
import unittest

from case_kernel.research_gateway import PUBLIC_SOURCES


REGISTRY = Path(__file__).resolve().parents[2] / "knowledge" / "official_sources" / "registry.json"


class OfficialSourceRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.public_sources = {item.source_id: item for item in PUBLIC_SOURCES}

    def test_discovery_ids_are_canonical_runtime_source_ids(self) -> None:
        ids = [item["source_id"] for item in self.registry["sources"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(ids).issubset(self.public_sources))

    def test_all_registered_and_fallback_urls_are_https_allowlisted(self) -> None:
        for item in self.registry["sources"]:
            source = self.public_sources[item["source_id"]]
            for key in ("official_url", "fallback_official_url", "official_data_api"):
                if key not in item:
                    continue
                parsed = urlparse(item[key])
                self.assertEqual(parsed.scheme, "https")
                self.assertIn(parsed.hostname, source.allowed_domains)

    def test_discovery_probe_never_promotes_a_source_to_formal_use(self) -> None:
        for item in self.registry["sources"]:
            self.assertEqual(item["formal_snapshot_status"], "NOT_CAPTURED")
            self.assertFalse(item["may_enter_formal_bundle"])


if __name__ == "__main__":
    unittest.main()
