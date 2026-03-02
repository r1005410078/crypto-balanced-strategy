#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from profile_switcher import apply_confirmation, decide_target_profile


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


if __name__ == "__main__":
    unittest.main()
