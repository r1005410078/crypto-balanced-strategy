#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from profile_switcher import (
    apply_confirmation,
    build_execution_checklist,
    decide_target_profile,
)


class ProfileSwitcherTests(unittest.TestCase):
    def test_decide_keep_base(self):
        target, reasons = decide_target_profile(
            stable_ret=-0.01,
            short_ret=-0.005,
            risk_rising=False,
        )
        self.assertEqual(target, "stable")
        self.assertTrue(reasons)

    def test_decide_switch_short(self):
        target, _ = decide_target_profile(
            stable_ret=-0.05,
            short_ret=-0.01,
            risk_rising=False,
        )
        self.assertEqual(target, "stable_short_balanced")

    def test_decide_switch_shield(self):
        target, _ = decide_target_profile(
            stable_ret=-0.05,
            short_ret=-0.03,
            risk_rising=True,
        )
        self.assertEqual(target, "stable_shield")

    def test_no_shield_without_risk_rising(self):
        target, _ = decide_target_profile(
            stable_ret=-0.05,
            short_ret=-0.03,
            risk_rising=False,
        )
        self.assertEqual(target, "stable_short_balanced")

    def test_confirmation_first_signal_pending_only(self):
        active, pending, count, switched = apply_confirmation(
            active_profile="stable",
            pending_target=None,
            pending_count=0,
            target_profile="stable_short_balanced",
            confirmations=2,
        )
        self.assertEqual(active, "stable")
        self.assertEqual(pending, "stable_short_balanced")
        self.assertEqual(count, 1)
        self.assertFalse(switched)

    def test_confirmation_second_signal_switch(self):
        active, pending, count, switched = apply_confirmation(
            active_profile="stable",
            pending_target="stable_short_balanced",
            pending_count=1,
            target_profile="stable_short_balanced",
            confirmations=2,
        )
        self.assertEqual(active, "stable_short_balanced")
        self.assertIsNone(pending)
        self.assertEqual(count, 0)
        self.assertTrue(switched)

    def test_clear_pending_when_target_returns_active(self):
        active, pending, count, switched = apply_confirmation(
            active_profile="stable",
            pending_target="stable_short_balanced",
            pending_count=1,
            target_profile="stable",
            confirmations=2,
        )
        self.assertEqual(active, "stable")
        self.assertIsNone(pending)
        self.assertEqual(count, 0)
        self.assertFalse(switched)

    def test_execution_checklist_hold_cash_mode(self):
        checklist = build_execution_checklist(
            active_profile="stable",
            latest_alloc={"BTCUSDT": 0.0, "USDT": 1.0},
            capital_cny=10000,
            switched=False,
            switch_reasons=["base profile within threshold"],
            risk_features={"close": 60000, "sma200": 65000, "drawdown60_pct": -15.0},
            active_check_metrics={"max_drawdown": -0.03},
        )
        self.assertEqual(checklist["mode"], "hold_cash")
        self.assertAlmostEqual(checklist["risk_exposure_pct"], 0.0, places=6)
        self.assertIn("actions", checklist)
        self.assertGreaterEqual(len(checklist["actions"]), 2)

    def test_execution_checklist_deploy_mode(self):
        checklist = build_execution_checklist(
            active_profile="stable_short_balanced",
            latest_alloc={"BTCUSDT": 0.6, "ETHUSDT": 0.2, "USDT": 0.2},
            capital_cny=10000,
            switched=True,
            switch_reasons=["switch to short profile"],
            risk_features={"close": 100000, "sma200": 90000, "drawdown60_pct": -5.0},
            active_check_metrics={"max_drawdown": -0.05},
        )
        self.assertEqual(checklist["mode"], "deploy")
        self.assertGreater(checklist["risk_exposure_pct"], 0.0)
        tranche_steps = [x for x in checklist["actions"] if x.get("type") == "entry_tranche"]
        self.assertGreaterEqual(len(tranche_steps), 2)


if __name__ == "__main__":
    unittest.main()
