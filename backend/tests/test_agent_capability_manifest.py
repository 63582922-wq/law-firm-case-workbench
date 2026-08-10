from __future__ import annotations

from pathlib import Path
import unittest

from case_kernel.agent_capability_manifest import (
    SCHEMA_VERSION,
    build_case_agent_capability_manifest,
    render_case_agent_capability_manifest,
)


class AgentCapabilityManifestTests(unittest.TestCase):
    def test_manifest_contains_policy_metadata_without_operational_secrets(self) -> None:
        manifest = build_case_agent_capability_manifest()
        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        self.assertEqual([item["skill_id"] for item in manifest["skills"]], sorted(item["skill_id"] for item in manifest["skills"]))
        self.assertTrue(any(item["maturity"] == "IMPLEMENTED" for item in manifest["skills"]))
        self.assertTrue(any(item["maturity"] == "GATED" for item in manifest["skills"]))
        encoded = render_case_agent_capability_manifest()
        self.assertNotIn("postgres", encoded.casefold())
        self.assertNotIn("token", encoded.casefold())

    def test_web_manifest_is_exactly_generated_from_policy_registry(self) -> None:
        root = Path(__file__).resolve().parents[2]
        artifact = root / "web" / "src" / "lib" / "case-skill-manifest.json"
        self.assertEqual(artifact.read_text(encoding="utf-8"), render_case_agent_capability_manifest())


if __name__ == "__main__":
    unittest.main()
