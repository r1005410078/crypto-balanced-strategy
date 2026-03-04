#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from aggressive_opt_loop import _hard_pass, _is_satisfied, _is_valid, _score_metrics  # noqa: E402


class AggressiveOptLoopTests(unittest.TestCase):
    def test_is_valid_basic_constraints(self):
        ok = {
            "lb_fast": 20,
            "lb_slow": 60,
            "max_w_core": 0.6,
            "max_w_alt": 0.3,
        }
        bad1 = dict(ok, lb_slow=10)
        bad2 = dict(ok, max_w_alt=0.8)
        self.assertTrue(_is_valid(ok))
        self.assertFalse(_is_valid(bad1))
        self.assertFalse(_is_valid(bad2))

    def test_score_prefers_better_short_term(self):
        good = {
            60: {"return": 0.06, "sharpe": 0.9, "max_drawdown": -0.08, "avg_daily_turnover": 0.05},
            120: {"return": 0.12, "sharpe": 1.0, "max_drawdown": -0.12, "avg_daily_turnover": 0.06},
            180: {"return": 0.14, "sharpe": 0.9, "max_drawdown": -0.15, "avg_daily_turnover": 0.07},
            365: {"return": 0.25, "sharpe": 1.1, "max_drawdown": -0.2, "avg_daily_turnover": 0.08},
            730: {"return": 0.4, "sharpe": 0.8, "max_drawdown": -0.28, "avg_daily_turnover": 0.09},
        }
        bad = {
            60: {"return": -0.02, "sharpe": -0.2, "max_drawdown": -0.2, "avg_daily_turnover": 0.25},
            120: {"return": 0.01, "sharpe": 0.0, "max_drawdown": -0.3, "avg_daily_turnover": 0.2},
            180: {"return": 0.02, "sharpe": 0.1, "max_drawdown": -0.3, "avg_daily_turnover": 0.22},
            365: {"return": 0.05, "sharpe": 0.2, "max_drawdown": -0.4, "avg_daily_turnover": 0.2},
            730: {"return": 0.1, "sharpe": 0.2, "max_drawdown": -0.5, "avg_daily_turnover": 0.18},
        }
        self.assertGreater(_score_metrics(good), _score_metrics(bad))

    def test_satisfied_threshold(self):
        good = {
            60: {"return": 0.03, "sharpe": 0.4, "max_drawdown": -0.1, "avg_daily_turnover": 0.05},
            120: {"return": 0.08, "sharpe": 0.5, "max_drawdown": -0.12, "avg_daily_turnover": 0.06},
            365: {"return": 0.2, "sharpe": 0.8, "max_drawdown": -0.2, "avg_daily_turnover": 0.07},
        }
        bad = {
            60: {"return": 0.0, "sharpe": 0.4, "max_drawdown": -0.1, "avg_daily_turnover": 0.05},
            120: {"return": 0.08, "sharpe": 0.5, "max_drawdown": -0.12, "avg_daily_turnover": 0.06},
            365: {"return": 0.2, "sharpe": 0.8, "max_drawdown": -0.2, "avg_daily_turnover": 0.07},
        }
        self.assertTrue(_is_satisfied(good))
        self.assertFalse(_is_satisfied(bad))

    def test_hard_pass_long_term_constraints(self):
        metrics = {
            120: {"return": 0.03, "sharpe": 0.6, "max_drawdown": -0.08, "avg_daily_turnover": 0.05},
            180: {"return": 0.05, "sharpe": 0.7, "max_drawdown": -0.12, "avg_daily_turnover": 0.06},
            365: {"return": 0.22, "sharpe": 0.9, "max_drawdown": -0.2, "avg_daily_turnover": 0.08},
            730: {"return": 0.35, "sharpe": 0.8, "max_drawdown": -0.3, "avg_daily_turnover": 0.09},
        }
        self.assertTrue(
            _hard_pass(
                metrics,
                min_return_120=0.02,
                min_return_180=0.03,
                min_return_365=0.15,
                min_return_730=0.1,
                max_drawdown_730_abs=0.35,
            )
        )
        self.assertFalse(_hard_pass(metrics, min_return_120=0.04))
        self.assertFalse(_hard_pass(metrics, min_return_180=0.06))
        self.assertFalse(_hard_pass(metrics, min_return_730=0.4))
        self.assertFalse(_hard_pass(metrics, max_drawdown_730_abs=0.25))


if __name__ == "__main__":
    unittest.main()
