#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from okx_auto_executor import OkxClient, build_rebalance_plan, normalize_target_alloc


class _FakeOkxClient(OkxClient):
    def __init__(self):
        super().__init__("k", "s", "p")
        self.calls = []

    def _request(self, method, path, *, params=None, payload=None, auth=False):
        self.calls.append({
            "method": method,
            "path": path,
            "params": params or {},
            "payload": payload or {},
            "auth": auth,
        })
        if path == "/api/v5/asset/balances":
            return [
                {"ccy": "USDT", "availBal": "12.5", "bal": "12.5"},
                {"ccy": "BTC", "availBal": "0", "bal": "0.001"},
            ]
        if path == "/api/v5/asset/transfer":
            return [{"transId": "123"}]
        return []


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

    def test_get_funding_balances(self):
        c = _FakeOkxClient()
        out = c.get_funding_balances(ccy="USDT")
        self.assertAlmostEqual(out["USDT"], 12.5, places=6)
        self.assertAlmostEqual(out["BTC"], 0.001, places=6)

    def test_transfer_funding_to_trading(self):
        c = _FakeOkxClient()
        out = c.transfer_funding_to_trading("USDT", 11.25)
        self.assertEqual(out["transId"], "123")
        last = c.calls[-1]
        self.assertEqual(last["path"], "/api/v5/asset/transfer")
        self.assertEqual(last["payload"]["ccy"], "USDT")
        self.assertEqual(last["payload"]["from"], "6")
        self.assertEqual(last["payload"]["to"], "18")


if __name__ == "__main__":
    unittest.main()
