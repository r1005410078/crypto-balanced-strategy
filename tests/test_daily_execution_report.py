#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_execution_report import (
    _build_brief_report,
    _build_holdings_adjustment,
    _build_summary_payload,
    _build_text_report,
)


class DailyExecutionReportTests(unittest.TestCase):
    def _base_switch_payload(self):
        return {
            "active_profile": "stable",
            "target_profile": "stable",
            "switched": False,
            "check_metrics": {
                "stable": {"return_pct": -0.6},
                "stable_short_balanced": {"return_pct": -1.2},
                "stable_shield": {"return_pct": -0.5},
            },
            "active_signal": {
                "return_pct": 20.0,
                "max_drawdown_pct": -8.0,
                "sharpe": 1.5,
                "latest_alloc": {"USDT": 1.0},
            },
            "execution_checklist": {
                "mode": "hold_cash",
                "risk_exposure_pct": 0.0,
                "capital_plan_cny": {"USDT": 10000.0},
                "actions": [],
                "guardrails": [{"rule": "risk_budget", "instruction": "Cut risk if breached."}],
                "next_check_command": "python3 scripts/profile_switcher.py",
            },
        }

    def test_build_summary_payload_hold(self):
        payload = self._base_switch_payload()
        out = _build_summary_payload(
            payload,
            invoked_cmd="cmd",
            holdings_adjustment=None,
            holdings_path=None,
        )
        self.assertEqual(out["summary"]["mode"], "hold_cash")
        self.assertFalse(out["summary"]["action_required"])
        self.assertTrue(out["instructions"])

    def test_text_report_contains_key_lines(self):
        payload = self._base_switch_payload()
        out = _build_summary_payload(
            payload,
            invoked_cmd="cmd",
            holdings_adjustment=None,
            holdings_path=None,
        )
        txt = _build_text_report(out)
        self.assertIn("Profile:", txt)
        self.assertIn("Instructions:", txt)
        self.assertIn("Next Check:", txt)

    def test_brief_report_three_lines(self):
        payload = self._base_switch_payload()
        out = _build_summary_payload(
            payload,
            invoked_cmd="cmd",
            holdings_adjustment=None,
            holdings_path=None,
        )
        brief = _build_brief_report(out)
        lines = [x for x in brief.splitlines() if x.strip()]
        self.assertEqual(len(lines), 3)
        self.assertIn("档位:", lines[0])
        self.assertIn("动作:", lines[1])
        self.assertIn("金额:", lines[2])

    def test_holdings_adjustment_diff(self):
        snap = {
            "total_estimated_value_usdt": 1000,
            "assets": [
                {"symbol": "BTC", "estimated_value_usdt": 900},
                {"symbol": "USDT", "estimated_value_usdt": 100},
            ],
        }
        model = {"BTCUSDT": 0.0, "USDT": 1.0}
        adj = _build_holdings_adjustment(snap, model_alloc=model, capital_cny=10000)
        btc = next(x for x in adj["actions"] if x["symbol"] == "BTC")
        usdt = next(x for x in adj["actions"] if x["symbol"] == "USDT")
        self.assertEqual(btc["suggestion"], "reduce")
        self.assertEqual(usdt["suggestion"], "add")


if __name__ == "__main__":
    unittest.main()
