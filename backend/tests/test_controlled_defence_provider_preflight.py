from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE = PROJECT_ROOT / "deployment" / "local-managed-test" / "probes" / "provider_probe.py"


class ControlledDefenceProviderPreflightTests(unittest.TestCase):
    def test_materials_configuration_requires_extraction_pair_without_live_probe(self) -> None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY": "sk-" + "x" * 38,
            "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY": "sk-" + "y" * 38,
            "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL": "deepseek-v4-pro",
            "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY": "qa_" + "z" * 38,
            "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID": "ws-lawyer-local",
        }
        for change, expected in (({}, 0), ({"LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY": ""}, 2),
                                 ({"LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL": "other"}, 2)):
            with self.subTest(change=tuple(change)):
                result = subprocess.run([sys.executable, str(PROBE), "--mode", "materials-configuration"],
                    cwd=PROJECT_ROOT, env={**environment, **change}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)
                payload = json.loads(result.stdout)
                self.assertNotIn(environment["LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY"], result.stdout)
                if expected == 0:
                    self.assertEqual([item["capability"] for item in payload["checks"]],
                                     ["planning", "ledger_extraction", "lawyer_analysis"])
                    self.assertTrue(all(item["network_calls"] == 0 for item in payload["checks"]))
                else:
                    self.assertEqual(payload["status"], "BLOCKED")

    def test_configuration_only_mode_has_no_model_probe_call(self) -> None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY": "qa_" + "x" * 38,
            "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID": "ws-lawyer-local",
        }
        completed = subprocess.run(
            [sys.executable, str(PROBE), "--mode", "controlled-defence"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["mode"], "controlled-defence")
        self.assertEqual(
            payload["checks"],
            [
                {
                    "provider": "qwen",
                    "capability": "lawyer_analysis",
                    "configuration_valid": True,
                    "network_calls": 0,
                    "endpoint_host_hash": payload["checks"][0]["endpoint_host_hash"],
                }
            ],
        )
        self.assertRegex(payload["checks"][0]["endpoint_host_hash"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
