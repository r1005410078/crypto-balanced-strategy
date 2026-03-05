#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_execution_report import (
    _build_live_holdings_snapshot,
    _build_brief_report,
    _build_holdings_adjustment,
    _build_summary_payload,
    _build_text_report,
    _load_holdings_data,
    _save_portfolio_snapshot,
)


class DailyExecutionReportTests(unittest.TestCase):
    class _FakeClient:
        def get_spot_balances(self):
            return {"USDT": 100, "BTC": 0.01}

        def get_funding_balances(self):
            return {"USDT": 50, "ETH": 0.2}

        def get_ticker(self, inst_id):
            if inst_id == "BTC-USDT":
                return {"price": 60000}
            if inst_id == "ETH-USDT":
                return {"price": 3000}
            raise ValueError(inst_id)

    class _FakeClientWithStrategyEq:
        def _request(self, method, path, *, params=None, payload=None, auth=False):
            if method == "GET" and path == "/api/v5/account/balance" and auth:
                return [
                    {
                        "totalEq": "520",
                        "uTime": "1772696464000",
                        "details": [
                            {
                                "ccy": "USDT",
                                "availBal": "100",
                                "cashBal": "100",
                                "eq": "160",
                                "stgyEq": "60",
                                "frozenBal": "60",
                            },
                            {
                                "ccy": "BTC",
                                "availBal": "0",
                                "cashBal": "0",
                                "eq": "0.005",
                                "stgyEq": "0.005",
                                "frozenBal": "0.005",
                            },
                        ],
                    }
                ]
            raise ValueError((method, path, params, payload, auth))

        def get_spot_balances(self):
            return {"USDT": 100, "BTC": 0.01}

        def get_funding_balances(self):
            return {"USDT": 10}

        def get_ticker(self, inst_id):
            if inst_id == "BTC-USDT":
                return {"price": 70000}
            raise ValueError(inst_id)

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
        self.assertFalse(out["holdings_snapshot_synced"])
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

    def test_build_live_holdings_snapshot_includes_funding(self):
        snap = _build_live_holdings_snapshot(self._FakeClient(), include_funding=True)
        self.assertEqual(snap["snapshot_source"], "okx_live")
        self.assertTrue(snap["include_funding"])
        self.assertAlmostEqual(snap["trade_balances"]["USDT"], 100.0, places=6)
        self.assertAlmostEqual(snap["funding_balances"]["USDT"], 50.0, places=6)
        self.assertAlmostEqual(snap["total_estimated_value_usdt"], 1350.0, places=6)

    def test_build_live_holdings_snapshot_includes_strategy_equity_by_default(self):
        snap = _build_live_holdings_snapshot(
            self._FakeClientWithStrategyEq(),
            include_funding=True,
        )
        self.assertTrue(snap["include_strategy_equity"])
        self.assertEqual(snap["trade_balance_basis"], "equity")
        self.assertAlmostEqual(snap["trade_balances_equity"]["USDT"], 160.0, places=6)
        self.assertAlmostEqual(snap["trade_balances_strategy"]["USDT"], 60.0, places=6)
        self.assertAlmostEqual(snap["trade_balances_frozen"]["BTC"], 0.005, places=9)
        # equity basis => USDT 160 + BTC 0.005*70000 + funding 10 = 520
        self.assertAlmostEqual(snap["total_estimated_value_usdt"], 520.0, places=6)

    def test_build_live_holdings_snapshot_can_exclude_strategy_equity(self):
        snap = _build_live_holdings_snapshot(
            self._FakeClientWithStrategyEq(),
            include_funding=True,
            include_strategy_equity=False,
        )
        self.assertFalse(snap["include_strategy_equity"])
        self.assertEqual(snap["trade_balance_basis"], "available")
        self.assertAlmostEqual(snap["trade_balances"]["USDT"], 100.0, places=6)
        # available basis => USDT 100 + funding 10
        self.assertAlmostEqual(snap["total_estimated_value_usdt"], 110.0, places=6)

    def test_load_holdings_data_auto_fallback_snapshot_when_env_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "portfolio_snapshot.json"
            p.write_text(
                json.dumps(
                    {
                        "snapshot_source": "snapshot_file",
                        "assets": [{"symbol": "USDT", "estimated_value_usdt": 100}],
                        "total_estimated_value_usdt": 100,
                    },
                    ensure_ascii=False,
                )
            )
            with patch.dict(os.environ, {}, clear=True):
                snap, source, live_error = _load_holdings_data(
                    skill_root=tmp,
                    holdings_source="auto",
                    include_funding=True,
                    live_base_url="https://www.okx.com",
                    live_user_agent=None,
                )
        self.assertIsNotNone(snap)
        self.assertTrue(source.endswith("portfolio_snapshot.json"))
        self.assertIn("Missing OKX API env vars", live_error)

    def test_load_holdings_data_live_raises_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                _load_holdings_data(
                    skill_root=str(ROOT),
                    holdings_source="live",
                    include_funding=True,
                    live_base_url="https://www.okx.com",
                    live_user_agent=None,
                )

    def test_save_portfolio_snapshot(self):
        snap = {
            "snapshot_source": "okx_live",
            "assets": [{"symbol": "USDT", "estimated_value_usdt": 123.4}],
            "total_estimated_value_usdt": 123.4,
        }
        with tempfile.TemporaryDirectory() as tmp:
            out_path = _save_portfolio_snapshot(tmp, snap)
            data = json.loads(Path(out_path).read_text())
        self.assertTrue(out_path.endswith("portfolio_snapshot.json"))
        self.assertEqual(data["snapshot_source"], "okx_live")
        self.assertAlmostEqual(data["total_estimated_value_usdt"], 123.4, places=6)


if __name__ == "__main__":
    unittest.main()
