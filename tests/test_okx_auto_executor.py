#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from okx_auto_executor import build_rebalance_plan, normalize_target_alloc


class OkxAutoExecutorTests(unittest.TestCase):
    def test_normalize_target_alloc(self):
        alloc = normalize_target_alloc({"BTCUSDT": 0.6, "ETHUSDT": 0.3, "USDT": 0.1})
        self.assertAlmostEqual(alloc["BTC"], 0.6, places=6)
        self.assertAlmostEqual(alloc["ETH"], 0.3, places=6)
        self.assertAlmostEqual(alloc["USDT"], 0.1, places=6)

    def test_build_rebalance_plan_hold_cash_sell_only(self):
        plan = build_rebalance_plan(
            target_weights={"USDT": 1.0},
            balances={"BTC": 0.01, "USDT": 10},
            prices={"BTC": 60000},
            spreads_bps={"BTC": 5.0},
            min_order_usdt=10,
            max_order_usdt=10000,
            max_spread_bps=20,
            allow_buy=False,
            allow_sell=True,
        )
        self.assertTrue(plan["orders"])
        od = plan["orders"][0]
        self.assertEqual(od["side"], "sell")
        self.assertEqual(od["symbol"], "BTC")
        self.assertGreater(od["notional_usdt"], 500)

    def test_build_rebalance_plan_skip_wide_spread(self):
        plan = build_rebalance_plan(
            target_weights={"BTC": 1.0},
            balances={"USDT": 1000},
            prices={"BTC": 60000},
            spreads_bps={"BTC": 55.0},
            min_order_usdt=10,
            max_order_usdt=1000,
            max_spread_bps=20,
            allow_buy=True,
            allow_sell=False,
        )
        self.assertEqual(len(plan["orders"]), 0)
        reasons = [x.get("reason") for x in plan["skipped"]]
        self.assertIn("spread_too_wide", reasons)


if __name__ == "__main__":
    unittest.main()
