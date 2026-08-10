from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.public_case_catalog import (
    AcquisitionMode,
    PublicCaseCatalog,
    PublicCaseCatalogBlocked,
)


CATALOG = Path(__file__).resolve().parents[2] / "knowledge" / "official_cases" / "registry.json"


class PublicCaseCatalogTests(unittest.TestCase):
    def test_real_official_metadata_catalog_loads_without_formal_or_bulk_use(self) -> None:
        catalog = PublicCaseCatalog.load(CATALOG)

        self.assertEqual(catalog.schema_version, "official-public-case-catalog-v1")
        self.assertGreaterEqual(len(catalog.candidates), 6)
        self.assertTrue(all(not item.formal_use_allowed for item in catalog.candidates))
        self.assertTrue(all(not source.bulk_capture_allowed for source in catalog.sources))
        self.assertTrue(all(not source.full_text_repository_allowed for source in catalog.sources))
        self.assertTrue(all(not source.commercial_training_allowed for source in catalog.sources))
        self.assertIn(
            AcquisitionMode.LAWYER_INITIATED_DOWNLOAD,
            next(source for source in catalog.sources if source.source_id == "PEOPLES_COURT_CASE_DATABASE").approved_acquisition_modes,
        )

    def test_candidate_cannot_escape_source_domain_or_enable_formal_use(self) -> None:
        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
        payload["candidates"][0]["official_url"] = "https://example.com/judgment"
        with self.assertRaisesRegex(PublicCaseCatalogBlocked, "outside the source allowlist"):
            self._load(payload)

        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
        payload["candidates"][0]["formal_use_allowed"] = True
        with self.assertRaisesRegex(PublicCaseCatalogBlocked, "cannot enter formal use"):
            self._load(payload)

    def test_unlicensed_source_cannot_claim_bulk_repository_or_training_rights(self) -> None:
        for field in ("bulk_capture_allowed", "full_text_repository_allowed", "commercial_training_allowed"):
            payload = json.loads(CATALOG.read_text(encoding="utf-8"))
            payload["sources"][0][field] = True
            with self.subTest(field=field), self.assertRaisesRegex(
                PublicCaseCatalogBlocked, "capability must remain disabled"
            ):
                self._load(payload)

    def test_unknown_fields_and_duplicate_identifiers_fail_closed(self) -> None:
        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
        payload["candidates"][0]["case_text"] = "must never be embedded"
        with self.assertRaisesRegex(PublicCaseCatalogBlocked, "fields are invalid"):
            self._load(payload)

        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
        payload["candidates"][1]["candidate_id"] = payload["candidates"][0]["candidate_id"]
        with self.assertRaisesRegex(PublicCaseCatalogBlocked, "duplicate public-case candidate"):
            self._load(payload)

    def _load(self, payload: dict[str, object]) -> PublicCaseCatalog:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return PublicCaseCatalog.load(path)


if __name__ == "__main__":
    unittest.main()
