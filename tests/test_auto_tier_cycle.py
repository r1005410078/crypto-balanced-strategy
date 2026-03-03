#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_tier_cycle import _build_auto_cycle_cmd, _derive_flags, decide_tier  # noqa: E402


class AutoTierCycleTests(unittest.TestCase):
    def test_derive_flags(self):
        payload = {
            "risk_rising_used": False,
            "execution_checklist": {"mode": "deploy", "risk_exposure_pct": 35.0},
        }
        f = _derive_flags(payload)
        self.assertEqual(f["mode"], "deploy")
        self.assertFalse(f["risk_rising"])
        self.assertTrue(f["is_deploy"])

    def test_promote_conservative_to_balanced(self):
        st = {
            "current_tier": "conservative",
            "normal_risk_streak": 1,
            "deploy_streak": 1,
        }
        flags = {"mode": "deploy", "risk_rising": False, "is_deploy": True}
        next_state, decision = decide_tier(st, flags, promote_days=2, allow_aggressive=False)
        self.assertEqual(next_state["current_tier"], "balanced")
        self.assertTrue(decision["promoted"])

    def test_demote_to_conservative_on_risk_rising(self):
        st = {
            "current_tier": "balanced",
            "normal_risk_streak": 3,
            "deploy_streak": 3,
        }
        flags = {"mode": "deploy", "risk_rising": True, "is_deploy": True}
        next_state, decision = decide_tier(st, flags, promote_days=2, allow_aggressive=False)
        self.assertEqual(next_state["current_tier"], "conservative")
        self.assertTrue(decision["demoted"])

    def test_keep_balanced_without_threshold(self):
        st = {
            "current_tier": "balanced",
            "normal_risk_streak": 2,
            "deploy_streak": 2,
        }
        flags = {"mode": "deploy", "risk_rising": False, "is_deploy": True}
        next_state, decision = decide_tier(st, flags, promote_days=2, allow_aggressive=False)
        self.assertEqual(next_state["current_tier"], "balanced")
        self.assertFalse(decision["promoted"])

    def test_promote_to_aggressive_when_allowed(self):
        st = {
            "current_tier": "balanced",
            "normal_risk_streak": 4,
            "deploy_streak": 4,
        }
        flags = {"mode": "deploy", "risk_rising": False, "is_deploy": True}
        next_state, decision = decide_tier(
            st,
            flags,
            promote_days=2,
            allow_aggressive=True,
            aggressive_promote_days=5,
        )
        self.assertEqual(next_state["current_tier"], "aggressive")
        self.assertTrue(decision["promoted"])

    def test_build_auto_cycle_cmd_uses_kill_switch_path(self):
        args = SimpleNamespace(
            live=False,
            demo=False,
            base_url="https://www.okx.com",
            user_agent=None,
            no_save_results=True,
            allow_buy=True,
            allow_sell=True,
            state_file=None,
            lock_file=None,
            lock_timeout_sec=10.0,
            force=False,
            no_notify=True,
            notify_webhook=[],
            notify_timeout_sec=10,
            transfer_in_dry_run=False,
        )
        cmd = _build_auto_cycle_cmd(
            args,
            auto_cycle_script=Path("/tmp/auto_cycle.py"),
            switch_file="/tmp/switch.json",
            tier="conservative",
            kill_switch_file=Path("/tmp/kill_switch"),
            passthrough=[],
        )
        joined = " ".join(cmd)
        self.assertIn("--kill-switch-file /tmp/kill_switch", joined)
        self.assertNotIn("None", joined)


if __name__ == "__main__":
    unittest.main()
