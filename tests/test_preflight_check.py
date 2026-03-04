#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from preflight_check import check_required_paths, run_preflight, summarize


class PreflightCheckTests(unittest.TestCase):
    def test_check_required_paths_reports_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "SKILL.md").write_text("x\n")
            out = check_required_paths(root, ["SKILL.md", "profiles.json"])
            self.assertEqual(out["status"], "FAIL")
            self.assertIn("profiles.json", out["missing"])

    def test_summarize_ready_flags(self):
        out = summarize(
            [
                {"name": "python_version", "status": "PASS"},
                {"name": "okx_env_vars", "status": "WARN"},
                {"name": "okx_read_access", "status": "PASS"},
            ]
        )
        self.assertTrue(out["ready_core"])
        self.assertFalse(out["ready_okx"])

    def test_run_preflight_core_ready_with_warn_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # required file skeleton
            (root / "SKILL.md").write_text("x\n")
            (root / "profiles.json").write_text("{}\n")
            (root / "dependencies.json").write_text(
                json.dumps(
                    {
                        "env": [
                            {"name": "OKX_API_KEY"},
                            {"name": "OKX_API_SECRET"},
                            {"name": "OKX_API_PASSPHRASE"},
                        ]
                    }
                )
                + "\n"
            )
            (root / "scripts").mkdir(parents=True, exist_ok=True)
            (root / "scripts" / "profile_switcher.py").write_text("x=1\n")
            (root / "scripts" / "okx_auto_executor.py").write_text("x=1\n")
            (root / "scripts" / "trade_decision_scorecard.py").write_text("x=1\n")
            (root / "scripts" / "auto_cycle.py").write_text("x=1\n")
            (root / "scripts" / "auto_daemon.py").write_text("x=1\n")
            (root / "scripts" / "auto_tier_cycle.py").write_text("x=1\n")
            (root / "scripts" / "health_check_dryrun.py").write_text("x=1\n")
            (root / "scripts" / "okx_hot_strategy_advisor.py").write_text("x=1\n")

            out = run_preflight(root, check_okx=False)
            self.assertTrue(out["summary"]["ready_core"])
            self.assertEqual(out["summary"]["counts"]["FAIL"], 0)


if __name__ == "__main__":
    unittest.main()
