#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from trade_decision_scorecard import compute_trade_metrics, score_metrics


class TradeDecisionScorecardTests(unittest.TestCase):
    def test_compute_trade_metrics_simple_profit(self):
        fills = [
            {
                "instId": "BTC-USDT",
                "side": "buy",
                "fillPx": "100",
                "fillSz": "1",
                "fee": "-0",
                "feeCcy": "USDT",
                "ts": "1000",
            },
            {
                "instId": "BTC-USDT",
                "side": "sell",
                "fillPx": "110",
                "fillSz": "1",
                "fee": "-0",
                "feeCcy": "USDT",
                "ts": "2000",
            },
        ]
        m = compute_trade_metrics(fills)
        self.assertEqual(m["fills_count"], 2)
        self.assertAlmostEqual(m["realized_pnl_usdt"], 10.0, places=6)
        self.assertEqual(m["buy_count"], 1)
        self.assertEqual(m["sell_count"], 1)
        self.assertEqual(m["closed_lots"], 1)
        self.assertAlmostEqual(m["win_rate"], 1.0, places=6)

    def test_score_metrics_high_quality(self):
        metrics = {
            "realized_pnl_usdt": 30.0,
            "win_rate": 0.8,
            "fee_bps": 8.0,
            "gross_notional_usdt": 1000.0,
            "median_fill_notional_usdt": 100.0,
            "max_fill_notional_usdt": 250.0,
        }
        s = score_metrics(metrics, equity_usdt=600.0)
        self.assertGreaterEqual(s["total_100"], 85)
        self.assertEqual(s["grade"], "A")

    def test_score_metrics_penalize_discipline(self):
        metrics = {
            "realized_pnl_usdt": 2.0,
            "win_rate": 0.4,
            "fee_bps": 30.0,
            "gross_notional_usdt": 10000.0,
            "median_fill_notional_usdt": 10.0,
            "max_fill_notional_usdt": 150.0,
        }
        s = score_metrics(metrics, equity_usdt=500.0)
        self.assertLessEqual(s["discipline_20"], 12)
        self.assertLessEqual(s["total_100"], 70)


if __name__ == "__main__":
    unittest.main()
