#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from engine import (
    apply_caps,
    backtest,
    resolve_regime_symbol,
    scale_to_target_vol,
)


def _make_kline(close, high=None, low=None):
    if high is None:
        high = close * 1.01
    if low is None:
        low = close * 0.99
    # Binance kline shape compatibility (we only use idx 2/3/4)
    return [0, str(close), str(high), str(low), str(close), "0", 0, "0", 0, "0", "0", "0"]


def _build_data(n=500):
    # deterministic synthetic trends
    data = {}
    for sym, drift in [
        ("BTCUSDT", 0.0006),
        ("ETHUSDT", 0.0005),
        ("SOLUSDT", 0.0004),
        ("BNBUSDT", 0.0003),
        ("LINKUSDT", 0.0002),
    ]:
        px = 100.0
        arr = []
        for _ in range(n):
            px *= (1 + drift)
            arr.append(_make_kline(px))
        data[sym] = arr
    return data


class EngineTests(unittest.TestCase):
    def test_resolve_regime_symbol_fallback(self):
        syms = ["ETHUSDT", "SOLUSDT"]
        self.assertEqual(resolve_regime_symbol(syms, preferred="BTCUSDT"), "ETHUSDT")

    def test_apply_caps_and_vol_scale(self):
        w = {"BTCUSDT": 0.8, "ETHUSDT": 0.4, "SOLUSDT": 0.2}
        capped = apply_caps(w, list(w.keys()), core_cap=0.6, alt_cap=0.3)
        self.assertLessEqual(capped["BTCUSDT"], 0.6)
        self.assertLessEqual(capped["ETHUSDT"], 0.6)
        self.assertLessEqual(capped["SOLUSDT"], 0.3)

        vol = {"BTCUSDT": 0.02, "ETHUSDT": 0.02, "SOLUSDT": 0.02}
        scaled = scale_to_target_vol(capped, vol, target_vol_annual=0.10)
        self.assertLessEqual(sum(scaled.values()), sum(capped.values()) + 1e-9)

    def test_backtest_smoke(self):
        params = {
            "lb_fast": 20,
            "lb_slow": 60,
            "sma_filter": 100,
            "k": 1,
            "atr_mult": 2.8,
            "max_w_core": 0.6,
            "max_w_alt": 0.3,
            "vol_lb": 20,
            "target_vol": 0.35,
            "rebalance_every": 3,
            "regime_sma": 200,
            "risk_off_exposure": 0.4,
            "atr_period": 14,
            "fee": 0.001,
            "slip": 0.0005,
        }
        res = backtest(_build_data(), params=params, window_days=180)
        self.assertIn("return", res)
        self.assertIn("latest_alloc", res)
        self.assertLessEqual(sum(v for k, v in res["latest_alloc"].items() if k != "USDT"), 1.0 + 1e-9)


if __name__ == "__main__":
    unittest.main()
