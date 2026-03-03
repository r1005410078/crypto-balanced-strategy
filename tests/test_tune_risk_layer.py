#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tune_risk_layer import (
    _constraint_penalty,
    _role_anchor_penalty,
    _role_spec,
    _score_candidate,
    _within_role_bounds,
)


class TuneRiskLayerTests(unittest.TestCase):
    def _make_metrics(self, ret120, ret365, mdd365, sharpe365, turn365):
        return {
            120: {
                "return": ret120,
                "cagr": ret120,
                "max_drawdown": min(0.0, ret120),
                "sharpe": 1.0,
                "avg_daily_turnover": 0.01,
            },
            180: {
                "return": max(0.0, ret120 / 2),
                "cagr": max(0.0, ret120 / 2),
                "max_drawdown": min(0.0, ret120),
                "sharpe": 1.0,
                "avg_daily_turnover": 0.01,
            },
            365: {
                "return": ret365,
                "cagr": ret365,
                "max_drawdown": mdd365,
                "sharpe": sharpe365,
                "avg_daily_turnover": turn365,
            },
            730: {
                "return": ret365 * 1.4,
                "cagr": ret365 * 0.7,
                "max_drawdown": mdd365 * 1.2,
                "sharpe": max(-1.0, sharpe365 - 0.2),
                "avg_daily_turnover": turn365,
            },
        }

    def test_constraint_penalty_zero_when_feasible(self):
        spec = _role_spec("stable")
        metrics = self._make_metrics(
            ret120=-0.03,
            ret365=0.35,
            mdd365=-0.12,
            sharpe365=1.5,
            turn365=0.02,
        )
        self.assertAlmostEqual(_constraint_penalty(metrics, spec), 0.0, places=8)

    def test_constraint_penalty_positive_when_violated(self):
        spec = _role_spec("stable")
        metrics = self._make_metrics(
            ret120=-0.12,
            ret365=0.05,
            mdd365=-0.30,
            sharpe365=0.3,
            turn365=0.10,
        )
        self.assertGreater(_constraint_penalty(metrics, spec), 0.0)

    def test_score_prefers_better_return_and_sharpe(self):
        spec = _role_spec("stable")
        good = self._make_metrics(
            ret120=-0.01,
            ret365=0.40,
            mdd365=-0.14,
            sharpe365=1.8,
            turn365=0.02,
        )
        bad = self._make_metrics(
            ret120=-0.06,
            ret365=0.15,
            mdd365=-0.18,
            sharpe365=0.8,
            turn365=0.03,
        )
        self.assertGreater(_score_candidate(good, spec), _score_candidate(bad, spec))

    def test_within_role_bounds(self):
        spec = _role_spec("stable_shield")
        self.assertTrue(_within_role_bounds(spec, 0.12, 0.10, 3, 220))
        self.assertFalse(_within_role_bounds(spec, 0.35, 0.10, 3, 220))

    def test_anchor_penalty_near_anchor_is_smaller(self):
        spec = _role_spec("stable_short_balanced")
        near = _role_anchor_penalty(spec, 0.20, 0.20, 1, 220)
        far = _role_anchor_penalty(spec, 0.30, 0.10, 3, 180)
        self.assertLess(near, far)


if __name__ == "__main__":
    unittest.main()
