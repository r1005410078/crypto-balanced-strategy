#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_cycle import _apply_strategy_budget, _build_switch_cmd, _cycle_status  # noqa: E402


class AutoCycleTests(unittest.TestCase):
    def test_cycle_status_live_executed(self):
        st = _cycle_status(
            skipped=False,
            live=True,
            guards_ok=True,
            order_count=2,
            execution_counts={"SUBMITTED": 2},
        )
        self.assertEqual(st, "executed")

    def test_cycle_status_live_blocked(self):
        st = _cycle_status(
            skipped=False,
            live=True,
            guards_ok=False,
            order_count=2,
            execution_counts={},
        )
        self.assertEqual(st, "blocked")

    def test_cycle_status_noop(self):
        st = _cycle_status(
            skipped=False,
            live=True,
            guards_ok=True,
            order_count=0,
            execution_counts={},
        )
        self.assertEqual(st, "noop")

    def test_build_switch_cmd(self):
        args = SimpleNamespace(
            capital_cny=10000,
            confirmations=2,
            check_window=120,
            signal_window=365,
            short_threshold=-0.03,
            shield_threshold=-0.015,
            risk_mode="auto",
            base_profile="stable",
            short_profile="stable_short_balanced",
            shield_profile="stable_shield",
            symbols="BTCUSDT,ETHUSDT",
            regime_symbol="BTCUSDT",
            limit=1000,
            cache_ttl_hours=6,
            no_cache=False,
            switch_state_file=None,
            no_save_switch_state=False,
            no_save_switch_results=False,
        )
        cmd = _build_switch_cmd(args, Path("/tmp/profile_switcher.py"))
        self.assertIn("--capital-cny", cmd)
        self.assertIn("--signal-window", cmd)
        self.assertIn("--symbols", cmd)

    def test_apply_strategy_budget_scales_risk_weights(self):
        target = {"BTC": 0.6, "ETH": 0.4}
        effective, info = _apply_strategy_budget(target, equity_usdt=1000, strategy_budget_usdt=200)
        self.assertTrue(info["enabled"])
        self.assertAlmostEqual(info["scale"], 0.2, places=6)
        self.assertAlmostEqual(effective["BTC"], 0.12, places=6)
        self.assertAlmostEqual(effective["ETH"], 0.08, places=6)
        self.assertAlmostEqual(effective["USDT"], 0.8, places=6)

    def test_apply_strategy_budget_none_keeps_target(self):
        target = {"BTC": 0.5, "USDT": 0.5}
        effective, info = _apply_strategy_budget(target, equity_usdt=1000, strategy_budget_usdt=None)
        self.assertFalse(info["enabled"])
        self.assertEqual(effective, target)


if __name__ == "__main__":
    unittest.main()
