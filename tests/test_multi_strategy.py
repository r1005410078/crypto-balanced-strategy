#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from multi_strategy_advisor import _combine_allocations, _softmax_weights, _strategy_score


class MultiStrategyTests(unittest.TestCase):
    def test_softmax_weights_fallback(self):
        w = _softmax_weights([("a", -0.2), ("b", -0.1)])
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        self.assertEqual(w["b"], 1.0)

    def test_combine_allocations(self):
        sw = {"s1": 0.6, "s2": 0.4}
        allocs = {
            "s1": {"BTCUSDT": 0.5, "USDT": 0.5},
            "s2": {"ETHUSDT": 0.25, "USDT": 0.75},
        }
        m = _combine_allocations(sw, allocs)
        self.assertAlmostEqual(sum(m.values()), 1.0, places=4)
        self.assertIn("USDT", m)
        self.assertGreater(m["USDT"], 0.0)

    def test_strategy_score_robust(self):
        metrics = {
            120: {"return": 0.10, "max_drawdown": -0.12, "sharpe": 1.2, "avg_daily_turnover": 0.04},
            365: {"return": 0.30, "max_drawdown": -0.18, "sharpe": 1.6, "avg_daily_turnover": 0.05},
            730: {"return": 0.40, "max_drawdown": -0.20, "sharpe": 1.4, "avg_daily_turnover": 0.06},
        }
        s = _strategy_score(metrics)
        self.assertGreater(s, 0.0)


if __name__ == "__main__":
    unittest.main()
