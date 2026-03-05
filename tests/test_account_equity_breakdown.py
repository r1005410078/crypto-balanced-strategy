#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from account_equity_breakdown import (  # noqa: E402
    _build_text,
    _parse_funding_balances,
    _parse_trading_details,
    _simplify_bot_rows,
    _strategy_occupied_summary,
)


class AccountEquityBreakdownTests(unittest.TestCase):
    def test_parse_trading_details_filters_zero_and_sorts_by_eq_usd(self):
        row = {
            "details": [
                {"ccy": "USDT", "eq": "100", "eqUsd": "100", "availBal": "80", "stgyEq": "20", "frozenBal": "20"},
                {"ccy": "BTC", "eq": "0.002", "eqUsd": "140", "availBal": "0", "stgyEq": "0.002", "frozenBal": "0.002"},
                {"ccy": "ETH", "eq": "0", "eqUsd": "0", "availBal": "0", "stgyEq": "0", "frozenBal": "0"},
            ]
        }
        out = _parse_trading_details(row, top_n=10)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["ccy"], "BTC")
        self.assertEqual(out[1]["ccy"], "USDT")

    def test_strategy_occupied_summary(self):
        trading_assets = [
            {"ccy": "USDT", "eq": 100.0, "eq_usd": 100.0, "stgy_eq": 20.0, "frozen_bal": 20.0},
            {"ccy": "BTC", "eq": 0.002, "eq_usd": 140.0, "stgy_eq": 0.002, "frozen_bal": 0.002},
            {"ccy": "SOL", "eq_usd": 2.0, "stgy_eq": 0.0, "frozen_bal": 0.0},
        ]
        out = _strategy_occupied_summary(trading_assets)
        self.assertEqual(out["occupied_assets_count"], 2)
        self.assertAlmostEqual(out["occupied_eq_usd_total"], 160.0, places=6)
        self.assertAlmostEqual(out["usdt_stgy_eq"], 20.0, places=6)
        self.assertAlmostEqual(out["btc_stgy_eq"], 0.002, places=9)

    def test_parse_funding_balances(self):
        rows = [
            {"ccy": "USDT", "bal": "1.2", "availBal": "1.2", "frozenBal": "0"},
            {"ccy": "XAUT", "bal": "0.0001", "availBal": "0.0001", "frozenBal": "0"},
            {"ccy": "ZERO", "bal": "0", "availBal": "0", "frozenBal": "0"},
        ]
        out = _parse_funding_balances(rows, top_n=10)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["ccy"], "USDT")

    def test_simplify_bot_rows(self):
        rows = [
            {
                "algoId": "1",
                "algoOrdType": "spot_dca",
                "instId": "BTC-USDT",
                "state": "running",
                "investmentAmt": "100",
                "investmentCcy": "USDT",
            }
        ]
        out = _simplify_bot_rows(rows, top_n=5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["algo_id"], "1")
        self.assertEqual(out[0]["inst_id"], "BTC-USDT")

    def test_build_text_contains_key_sections(self):
        payload = {
            "as_of_local": "2026-03-05T00:00:00+08:00",
            "account_config": {"uid": "u", "acct_lv": "2", "pos_mode": "net_mode", "perm": "read_only,trade"},
            "trading_totals": {"total_eq_usd": 1000.0, "u_time": "1"},
            "strategy_occupied": {
                "occupied_assets_count": 1,
                "occupied_eq_usd_total": 100.0,
                "usdt_stgy_eq": 10.0,
                "usdt_frozen_bal": 10.0,
                "btc_stgy_eq": 0.0,
                "btc_frozen_bal": 0.0,
            },
            "trading_assets_top": [],
            "funding_assets_top": [],
            "running_bots": {"dca_pending_count": 0, "recurring_pending_count": 0, "grid_pending_count": 0, "total_running_count": 0},
            "spot_pending_orders_count": 0,
            "spot_recent_fills_count": 0,
        }
        txt = _build_text(payload)
        self.assertIn("Trading Equity:", txt)
        self.assertIn("Strategy Occupied:", txt)
        self.assertIn("Running Bots:", txt)


if __name__ == "__main__":
    unittest.main()
