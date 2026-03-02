#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_governance import _status_from_checks


class GovernanceTests(unittest.TestCase):
    def test_status_abandon_on_negative_baseline(self):
        baseline = {"return": -0.01, "max_drawdown": -0.10}
        checks = {
            "friction_pass_rate": 1.0,
            "sensitivity_pass_rate": 1.0,
            "window_positive_rate": 1.0,
            "walk_forward_positive_rate": 1.0,
        }
        self.assertEqual(_status_from_checks(baseline, checks), "ABANDON")

    def test_status_refine_when_many_weak_checks(self):
        baseline = {"return": 0.2, "max_drawdown": -0.12}
        checks = {
            "friction_pass_rate": 0.4,
            "sensitivity_pass_rate": 0.5,
            "window_positive_rate": 0.5,
            "walk_forward_positive_rate": 1.0,
        }
        self.assertEqual(_status_from_checks(baseline, checks), "REFINE")

    def test_status_deploy_candidate(self):
        baseline = {"return": 0.2, "max_drawdown": -0.12}
        checks = {
            "friction_pass_rate": 0.8,
            "sensitivity_pass_rate": 0.8,
            "window_positive_rate": 0.8,
            "walk_forward_positive_rate": 0.8,
        }
        self.assertEqual(_status_from_checks(baseline, checks), "DEPLOY_CANDIDATE")


if __name__ == "__main__":
    unittest.main()
