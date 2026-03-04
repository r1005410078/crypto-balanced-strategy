#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from signal_level_compare import (  # noqa: E402
    _bootstrap_outperform,
    _judge,
    _quantile,
    _recommend_allocation,
    _simulate_recurring_returns,
)


class SignalLevelCompareTests(unittest.TestCase):
    def test_quantile_basic(self):
        xs = [1, 2, 3, 4]
        self.assertAlmostEqual(_quantile(xs, 0.0), 1.0, places=6)
        self.assertAlmostEqual(_quantile(xs, 0.5), 2.5, places=6)
        self.assertAlmostEqual(_quantile(xs, 1.0), 4.0, places=6)

    def test_bootstrap_local_outperform(self):
        local = [0.01] * 120
        hot = [0.001] * 120
        out = _bootstrap_outperform(local, hot, iterations=200, block_size=7, confidence=0.95, seed=7)
        self.assertGreater(out["prob_local_outperform"], 0.95)
        self.assertGreater(out["ci_diff_cum_return"][0], 0.0)

    def test_judge_paths(self):
        self.assertEqual(
            _judge({"ci_diff_cum_return": [0.01, 0.03], "prob_local_outperform": 0.8}),
            "local_higher_with_confidence",
        )
        self.assertEqual(
            _judge({"ci_diff_cum_return": [-0.03, -0.01], "prob_local_outperform": 0.2}),
            "hot_higher_with_confidence",
        )
        self.assertEqual(
            _judge({"ci_diff_cum_return": [-0.02, 0.04], "prob_local_outperform": 0.65}),
            "local_higher_but_not_significant",
        )

    def test_allocation_gate_cap(self):
        gate = {"mode": "hold_cash", "risk_rising_used": True}
        weights = {"grid": 0.4, "spot_dca": 0.3, "recurring": 0.3}
        out = _recommend_allocation("hot_higher_with_confidence", gate, 1000.0, weights)
        self.assertAlmostEqual(out["hot_strategy_ratio"], 0.1, places=6)
        self.assertEqual(out["amounts_usdt"]["hot_strategy"], 100.0)

    def test_recurring_cost_reduces_returns(self):
        closes = [100 + i for i in range(100)]
        low_cost = _simulate_recurring_returns(closes, window_days=90, trade_cost=0.001)
        high_cost = _simulate_recurring_returns(closes, window_days=90, trade_cost=0.003)
        self.assertLess(sum(high_cost), sum(low_cost))


if __name__ == "__main__":
    unittest.main()
