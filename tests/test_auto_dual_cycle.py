#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_dual_cycle import (  # noqa: E402
    blend_targets,
    resolve_budget_split,
    resolve_budget_split_by_ratio,
    should_notify,
)


class AutoDualCycleTests(unittest.TestCase):
    def test_resolve_budget_split_auto_primary(self):
        b = resolve_budget_split(
            equity_usdt=1200.0,
            primary_budget_usdt=None,
            aggressive_budget_usdt=200.0,
        )
        self.assertEqual(b["primary_mode"], "auto_remaining")
        self.assertAlmostEqual(b["primary_budget_usdt"], 1000.0, places=6)
        self.assertAlmostEqual(b["aggressive_budget_usdt"], 200.0, places=6)
        self.assertAlmostEqual(b["scale"], 1.0, places=6)

    def test_resolve_budget_split_scales_if_over_equity(self):
        b = resolve_budget_split(
            equity_usdt=1000.0,
            primary_budget_usdt=900.0,
            aggressive_budget_usdt=400.0,
        )
        self.assertAlmostEqual(b["primary_budget_usdt"] + b["aggressive_budget_usdt"], 1000.0, places=6)
        self.assertLess(b["scale"], 1.0)

    def test_blend_targets_preserves_budget_weights(self):
        budget = resolve_budget_split(1000.0, 800.0, 200.0)
        out = blend_targets(
            primary_signal_alloc={"BTC": 1.0, "USDT": 0.0},
            aggressive_signal_alloc={"ETH": 1.0, "USDT": 0.0},
            budget_split=budget,
        )
        self.assertAlmostEqual(out["BTC"], 0.8, places=6)
        self.assertAlmostEqual(out["ETH"], 0.2, places=6)
        self.assertAlmostEqual(out["USDT"], 0.0, places=6)

    def test_blend_targets_handles_cash_signals(self):
        budget = resolve_budget_split(1000.0, 800.0, 200.0)
        out = blend_targets(
            primary_signal_alloc={"USDT": 1.0},
            aggressive_signal_alloc={"USDT": 1.0},
            budget_split=budget,
        )
        self.assertAlmostEqual(out["USDT"], 1.0, places=6)
        self.assertEqual(len(out.keys()), 1)

    def test_resolve_budget_split_by_ratio(self):
        b = resolve_budget_split_by_ratio(
            equity_usdt=1200.0,
            aggressive_ratio=0.3,
            primary_ratio=None,
        )
        self.assertEqual(b["primary_mode"], "ratio_auto_remaining")
        self.assertAlmostEqual(b["primary_ratio"], 0.7, places=6)
        self.assertAlmostEqual(b["aggressive_ratio"], 0.3, places=6)
        self.assertAlmostEqual(b["primary_budget_usdt"], 840.0, places=6)
        self.assertAlmostEqual(b["aggressive_budget_usdt"], 360.0, places=6)

    def test_resolve_budget_split_by_ratio_scaled(self):
        b = resolve_budget_split_by_ratio(
            equity_usdt=1000.0,
            aggressive_ratio=0.8,
            primary_ratio=0.6,
        )
        self.assertLess(b["scale"], 1.0)
        self.assertAlmostEqual(b["primary_ratio"] + b["aggressive_ratio"], 1.0, places=6)
        self.assertAlmostEqual(b["primary_budget_usdt"] + b["aggressive_budget_usdt"], 1000.0, places=6)

    def test_should_notify_no_trade_cycle(self):
        out = {
            "cycle_status": "noop",
            "execution_counts": {},
            "plan": {"orders": []},
            "price_errors": [],
        }
        self.assertFalse(should_notify(out))

    def test_should_notify_on_submitted(self):
        out = {
            "cycle_status": "executed",
            "execution_counts": {"SUBMITTED": 1},
            "plan": {"orders": [{"inst_id": "BTC-USDT"}]},
            "price_errors": [],
        }
        self.assertTrue(should_notify(out))

    def test_should_notify_on_failed(self):
        out = {
            "cycle_status": "failed",
            "execution_counts": {"FAILED": 1},
            "plan": {"orders": [{"inst_id": "BTC-USDT"}]},
            "price_errors": [],
        }
        self.assertTrue(should_notify(out))

    def test_should_not_notify_on_non_actionable_blocked(self):
        out = {
            "cycle_status": "blocked",
            "execution_counts": {"BLOCKED": 1},
            "plan": {"orders": []},
            "price_errors": [],
        }
        self.assertFalse(should_notify(out))


if __name__ == "__main__":
    unittest.main()
