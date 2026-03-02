#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from optimize import _clip, _score


class OptimizeTests(unittest.TestCase):
    def test_clip(self):
        self.assertEqual(_clip(2.0, -1.0, 1.0), 1.0)
        self.assertEqual(_clip(-2.0, -1.0, 1.0), -1.0)
        self.assertEqual(_clip(0.5, -1.0, 1.0), 0.5)

    def test_score_is_robust_to_outlier(self):
        insample = {"sharpe": 1.0}
        # one huge outlier fold + one flat fold
        oos = [
            {"return": 2.0, "cagr": 20.0, "sharpe": 8.0, "max_drawdown": -0.08, "avg_daily_turnover": 0.02},
            {"return": 0.0, "cagr": 0.0, "sharpe": 0.0, "max_drawdown": -0.07, "avg_daily_turnover": 0.02},
        ]
        s = _score(insample, oos)
        # Score should stay bounded and not explode with annualized outlier
        self.assertLess(s["score"], 2.0)
        self.assertGreater(s["score"], -2.0)
        self.assertIn("oos_ret_med", s)


if __name__ == "__main__":
    unittest.main()
