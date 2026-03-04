#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from okx_hot_strategy_advisor import (  # noqa: E402
    _compute_budget,
    _parse_strategy_categories,
    _rank_strategies,
    _risk_level,
)


class HotStrategyAdvisorTests(unittest.TestCase):
    def test_risk_level(self):
        self.assertEqual(_risk_level(["SPOT"]), "low")
        self.assertEqual(_risk_level(["SPOT", "MARGIN"]), "medium")
        self.assertEqual(_risk_level(["SWAP"]), "high")

    def test_parse_strategy_categories(self):
        app_state = {
            "appContext": {
                "initialProps": {
                    "topTabData": {
                        "strategyCategories": {
                            "spot_dca": {
                                "strategyType": "spot_dca",
                                "category": "average",
                                "instTypeList": "SPOT",
                                "mpEnabled": "1",
                                "optimalValue": "9.1",
                                "userCount": "1000",
                                "stage": "online",
                            }
                        }
                    }
                }
            }
        }
        rows = _parse_strategy_categories(app_state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy_type"], "spot_dca")
        self.assertEqual(rows[0]["user_count"], 1000)
        self.assertTrue(rows[0]["mp_enabled"])

    def test_rank_strategies_filters_high_risk(self):
        rows = [
            {
                "strategy_type": "contract_grid",
                "category": "grid",
                "inst_types": ["SWAP"],
                "mp_enabled": True,
                "optimal_value": 80.0,
                "user_count": 50000,
                "stage": "online",
            },
            {
                "strategy_type": "spot_dca",
                "category": "average",
                "inst_types": ["SPOT"],
                "mp_enabled": True,
                "optimal_value": 9.0,
                "user_count": 20000,
                "stage": "online",
            },
        ]
        out = _rank_strategies(rows, allow_derivatives=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["strategy_type"], "spot_dca")

    def test_compute_budget_hold_cash_blocks_ratio(self):
        gate = {"mode": "hold_cash", "risk_rising_used": True}
        budget, blocked = _compute_budget(
            total_usdt=1000,
            main_gate=gate,
            default_ratio=0.05,
            max_budget_usdt=200,
            sandbox_usdt=0.0,
        )
        self.assertTrue(blocked)
        self.assertAlmostEqual(budget, 0.0, places=6)

    def test_compute_budget_with_sandbox(self):
        gate = {"mode": "hold_cash", "risk_rising_used": True}
        budget, blocked = _compute_budget(
            total_usdt=1000,
            main_gate=gate,
            default_ratio=0.05,
            max_budget_usdt=200,
            sandbox_usdt=25.0,
        )
        self.assertTrue(blocked)
        self.assertAlmostEqual(budget, 25.0, places=6)


if __name__ == "__main__":
    unittest.main()
