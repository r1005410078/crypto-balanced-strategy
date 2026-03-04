#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_tier_cycle import (  # noqa: E402
    _build_auto_cycle_cmd,
    _build_hot_advisor_cmd,
    _derive_flags,
    _is_network_error_text,
    _summarize_hot_advice,
    _wait_for_network_recovery,
    decide_tier,
)


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

    def test_is_network_error_text(self):
        self.assertTrue(_is_network_error_text("Network is unreachable"))
        self.assertTrue(_is_network_error_text("urlopen error timed out"))
        self.assertFalse(_is_network_error_text("Missing OKX_API_KEY"))

    def test_wait_for_network_recovery(self):
        checks = {"count": 0}

        def fake_probe(_h, _p, _t):
            checks["count"] += 1
            return checks["count"] >= 3

        def fake_sleep(_s):
            return None

        timeline = {"now": 0.0}

        def fake_now():
            timeline["now"] += 0.5
            return timeline["now"]

        ok = _wait_for_network_recovery(
            host="www.okx.com",
            port=443,
            timeout_sec=1.0,
            interval_sec=0.2,
            max_wait_sec=10.0,
            probe_fn=fake_probe,
            sleep_fn=fake_sleep,
            now_fn=fake_now,
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(checks["count"], 3)

    def test_build_hot_advisor_cmd(self):
        args = SimpleNamespace(
            hot_advice_source_url="https://www.okx.com/en-us/trading-bot",
            hot_advice_top_n=3,
            hot_advice_default_ratio=0.05,
            hot_advice_max_budget_usdt=140.0,
            hot_advice_sandbox_usdt=25.0,
            hot_advice_allow_derivatives=False,
            no_save_results=True,
        )
        cmd = _build_hot_advisor_cmd(args, Path("/tmp/okx_hot_strategy_advisor.py"))
        joined = " ".join(cmd)
        self.assertIn("--top-n 3", joined)
        self.assertIn("--sandbox-usdt 25.0", joined)
        self.assertIn("--no-save-results", joined)

    def test_summarize_hot_advice(self):
        payload = {
            "budget": {"recommended_budget_usdt": 25.0, "hold_cash_block": True},
            "results_file": "/tmp/hot.json",
            "selected": [
                {"strategy_type": "spot_dca", "allocation_usdt": 8.1, "risk_level": "low"},
                {"strategy_type": "grid", "allocation_usdt": 7.9, "risk_level": "low"},
            ],
        }
        s = _summarize_hot_advice(payload)
        self.assertAlmostEqual(s["recommended_budget_usdt"], 25.0, places=6)
        self.assertTrue(s["hold_cash_block"])
        self.assertEqual(s["selected"][0]["strategy_type"], "spot_dca")


if __name__ == "__main__":
    unittest.main()
