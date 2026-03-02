#!/usr/bin/env python3
import json
import math
import os
import statistics
import time
import urllib.request
from pathlib import Path

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"]
CORE_SYMBOLS = {"BTCUSDT", "ETHUSDT"}


class BacktestError(RuntimeError):
    pass


def _default_cache_dir() -> Path:
    return Path.home() / ".agents" / "skills" / "crypto-balanced-strategy" / "cache"


def resolve_regime_symbol(symbols, preferred="BTCUSDT"):
    if preferred in symbols:
        return preferred
    for s in DEFAULT_SYMBOLS:
        if s in symbols:
            return s
    if not symbols:
        raise BacktestError("No symbols provided")
    return symbols[0]


def fetch_klines(symbol: str, interval="1d", limit: int = 1000, use_cache=True, cache_dir=None, ttl_hours=6):
    if cache_dir is None:
        cache_dir = _default_cache_dir()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}_{interval}_{limit}.json"

    if use_cache and cache_path.exists():
        age_sec = max(0.0, time.time() - cache_path.stat().st_mtime)
        if age_sec <= ttl_hours * 3600:
            return json.loads(cache_path.read_text())

    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    last_err = None
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode())
            cache_path.write_text(json.dumps(data))
            return data
        except Exception as e:
            last_err = e
            time.sleep(0.4 * (i + 1))

    if use_cache and cache_path.exists():
        # Fallback to stale cache if online request failed
        return json.loads(cache_path.read_text())

    raise BacktestError(f"Failed to fetch klines for {symbol}: {last_err}")


def load_data(symbols, limit=1000, use_cache=True, cache_dir=None, ttl_hours=6):
    return {
        s: fetch_klines(s, limit=limit, use_cache=use_cache, cache_dir=cache_dir, ttl_hours=ttl_hours)
        for s in symbols
    }


def align_ohlc(data):
    symbols = list(data.keys())
    if not symbols:
        raise BacktestError("No symbols available in data")

    closes = {s: [float(x[4]) for x in data[s]] for s in symbols}
    highs = {s: [float(x[2]) for x in data[s]] for s in symbols}
    lows = {s: [float(x[3]) for x in data[s]] for s in symbols}

    n = min(len(v) for v in closes.values())
    if n < 260:
        raise BacktestError("Insufficient data length for strategy warmup")

    for s in symbols:
        closes[s] = closes[s][-n:]
        highs[s] = highs[s][-n:]
        lows[s] = lows[s][-n:]

    rets = {s: [0.0] + [closes[s][i] / closes[s][i - 1] - 1 for i in range(1, n)] for s in symbols}
    return symbols, n, closes, highs, lows, rets


def calc_atr(highs, lows, closes, t, period=14):
    if t <= period:
        return None
    trs = []
    for i in range(t - period + 1, t + 1):
        prev_close = closes[i - 1]
        tr = max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
        trs.append(tr)
    return sum(trs) / period


def apply_caps(weights, symbols, core_cap, alt_cap):
    out = dict(weights)
    for s in symbols:
        cap = core_cap if s in CORE_SYMBOLS else alt_cap
        out[s] = min(out.get(s, 0.0), cap)
    sw = sum(out.values())
    if sw > 1.0:
        for s in out:
            out[s] /= sw
    return out


def scale_to_target_vol(weights, sym_vol, target_vol_annual):
    var_daily = 0.0
    for s, w in weights.items():
        var_daily += (w ** 2) * (sym_vol[s] ** 2)
    port_vol_annual = math.sqrt(var_daily) * math.sqrt(365)
    if port_vol_annual <= 1e-9:
        return weights
    scale = min(1.0, target_vol_annual / port_vol_annual)
    return {s: w * scale for s, w in weights.items()}


def _compute_metrics(curve):
    if len(curve) < 2:
        return {"return": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "vol": 0.0, "sharpe": 0.0}

    daily = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    peak = curve[0]
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)

    ann_factor = 365 / max(1, len(daily))
    cagr = curve[-1] ** ann_factor - 1
    vol = statistics.pstdev(daily) * math.sqrt(365)
    sharpe = (statistics.mean(daily) * 365) / (vol + 1e-9)
    return {
        "return": curve[-1] - 1,
        "cagr": cagr,
        "max_drawdown": mdd,
        "vol": vol,
        "sharpe": sharpe,
    }


def backtest(
    data,
    params,
    window_days=None,
    start_index=None,
    end_index=None,
    regime_symbol="BTCUSDT",
):
    symbols, n, closes, highs, lows, rets = align_ohlc(data)
    regime_symbol = resolve_regime_symbol(symbols, regime_symbol)

    lb_fast = int(params["lb_fast"])
    lb_slow = int(params["lb_slow"])
    sma_filter = int(params["sma_filter"])
    k = max(1, int(params["k"]))
    atr_mult = float(params["atr_mult"])
    max_w_core = float(params["max_w_core"])
    max_w_alt = float(params["max_w_alt"])
    vol_lb = int(params["vol_lb"])
    target_vol = float(params["target_vol"])
    rebalance_every = max(1, int(params["rebalance_every"]))
    regime_sma = int(params["regime_sma"])
    risk_off_exposure = float(params["risk_off_exposure"])
    atr_period = int(params.get("atr_period", 14))
    fee = float(params["fee"])
    slip = float(params["slip"])

    warmup = max(lb_slow, sma_filter, vol_lb, regime_sma, atr_period) + 3
    start = warmup
    if end_index is None:
        end = n
    else:
        end = min(n, int(end_index))
    if start_index is not None:
        start = max(start, int(start_index))
    if window_days is not None:
        start = max(start, end - int(window_days))
    if start >= end:
        raise BacktestError("Invalid backtest range after warmup/window constraints")

    eq = 1.0
    curve = [eq]
    prev = {s: 0.0 for s in symbols}
    highest_close = {s: None for s in symbols}
    latest_w = {s: 0.0 for s in symbols}
    turnover_sum = 0.0

    for t in range(start, end):
        if (t - start) % rebalance_every != 0:
            w = dict(prev)
        else:
            w = {s: 0.0 for s in symbols}
            moms = []
            vol_map = {}
            for s in symbols:
                m_fast = closes[s][t - 1] / closes[s][t - 1 - lb_fast] - 1
                m_slow = closes[s][t - 1] / closes[s][t - 1 - lb_slow] - 1
                score = 0.6 * m_fast + 0.4 * m_slow
                sma_v = sum(closes[s][t - sma_filter:t]) / sma_filter
                trend = closes[s][t - 1] > sma_v
                vol = statistics.pstdev(rets[s][t - vol_lb:t])
                vol_map[s] = max(vol, 1e-6)
                moms.append((s, score, trend))

            elig = [x for x in moms if x[2] and x[1] > 0]
            elig.sort(key=lambda x: x[1], reverse=True)
            elig = elig[:k]

            if elig:
                inv = [(s, 1.0 / vol_map[s]) for s, _, _ in elig]
                sm = sum(v for _, v in inv)
                for s, v in inv:
                    w[s] = v / sm

                w = apply_caps(w, symbols, max_w_core, max_w_alt)
                w = scale_to_target_vol(w, vol_map, target_vol)

                regime_v = sum(closes[regime_symbol][t - regime_sma:t]) / regime_sma
                risk_on = closes[regime_symbol][t - 1] > regime_v
                if not risk_on:
                    gross = sum(w.values())
                    if gross > 0:
                        sc = min(1.0, risk_off_exposure / gross)
                        for s in w:
                            w[s] *= sc

        # ATR trailing stop
        for s in symbols:
            if prev[s] > 0:
                if highest_close[s] is None:
                    highest_close[s] = closes[s][t - 1]
                highest_close[s] = max(highest_close[s], closes[s][t - 1])
                atr = calc_atr(highs[s], lows[s], closes[s], t - 1, period=atr_period)
                if atr is not None:
                    stop_px = highest_close[s] - atr_mult * atr
                    if closes[s][t - 1] < stop_px:
                        w[s] = 0.0
            if w[s] == 0.0:
                highest_close[s] = None

        sw = sum(w.values())
        if sw > 1.0:
            for s in w:
                w[s] /= sw

        tc = sum(abs(w[s] - prev[s]) for s in symbols) * (fee + slip)
        turnover_sum += sum(abs(w[s] - prev[s]) for s in symbols)
        day_r = sum(w[s] * rets[s][t] for s in symbols) - tc

        eq *= (1 + day_r)
        curve.append(eq)
        prev = w
        latest_w = w

    metrics = _compute_metrics(curve)
    cash = max(0.0, 1.0 - sum(latest_w.values()))
    metrics.update({
        "avg_daily_turnover": turnover_sum / max(1, len(curve) - 1),
        "latest_alloc": {**{k: round(v, 4) for k, v in latest_w.items()}, "USDT": round(cash, 4)},
        "regime_symbol": regime_symbol,
        "bars": len(curve) - 1,
    })
    return metrics


def load_profiles(skill_root):
    path = Path(skill_root) / "profiles.json"
    if not path.exists():
        raise BacktestError(f"Missing profiles file: {path}")
    return json.loads(path.read_text())


def save_profiles(skill_root, profiles):
    path = Path(skill_root) / "profiles.json"
    path.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + os.linesep)
