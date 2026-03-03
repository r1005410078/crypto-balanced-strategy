#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from risk_guard import evaluate_trade_guards, summarize_execution  # noqa: E402


class RiskGuardTests(unittest.TestCase):
    def test_guards_pass_in_dry_run(self):
        res = evaluate_trade_guards(
            live=False,
            target_alloc={"USDT": 1.0},
            plan={"orders": []},
        )
        self.assertTrue(res["ok"])

    def test_guards_block_when_auto_disabled_in_live(self):
        old = os.environ.get("AUTO_TRADING_ENABLED")
        os.environ["AUTO_TRADING_ENABLED"] = "false"
        try:
            res = evaluate_trade_guards(
                live=True,
                target_alloc={"BTC": 0.5, "USDT": 0.5},
                plan={"orders": [{"notional_usdt": 100.0}]},
            )
            self.assertFalse(res["ok"])
            self.assertTrue(any("AUTO_TRADING_ENABLED" in x for x in res["reasons"]))
        finally:
            if old is None:
                os.environ.pop("AUTO_TRADING_ENABLED", None)
            else:
                os.environ["AUTO_TRADING_ENABLED"] = old

    def test_guards_block_daily_loss(self):
        res = evaluate_trade_guards(
            live=True,
            target_alloc={"USDT": 1.0},
            plan={"orders": []},
            day_pnl_pct=-4.0,
            max_daily_loss_pct=3.0,
        )
        self.assertFalse(res["ok"])
        self.assertTrue(any("daily pnl" in x for x in res["reasons"]))

    def test_guards_block_on_kill_switch(self):
        with tempfile.TemporaryDirectory() as td:
            ks = Path(td) / "kill_switch"
            ks.write_text("1\n")
            res = evaluate_trade_guards(
                live=True,
                target_alloc={"USDT": 1.0},
                plan={"orders": []},
                kill_switch_file=str(ks),
            )
            self.assertFalse(res["ok"])
            self.assertTrue(any("kill switch" in x for x in res["reasons"]))

    def test_execution_summary(self):
        counts = summarize_execution(
            [
                {"status": "SUBMITTED"},
                {"status": "SUBMITTED"},
                {"status": "FAILED"},
            ]
        )
        self.assertEqual(counts.get("SUBMITTED"), 2)
        self.assertEqual(counts.get("FAILED"), 1)


if __name__ == "__main__":
    unittest.main()
