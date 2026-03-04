#!/usr/bin/env python3
import argparse
import json
import math
import random
import statistics
from datetime import datetime
from pathlib import Path

from engine import (
    DEFAULT_SYMBOLS,
    BacktestError,
    align_ohlc,
    apply_caps,
    calc_atr,
    load_data,
    load_profiles,
    resolve_regime_symbol,
    scale_to_target_vol,
)
from okx_hot_strategy_advisor import _load_main_strategy_gate, _load_total_usdt_live


def _parse_windows(text):
    out = []
    for x in str(text or "").split(","):
        x = x.strip()
        if not x:
            continue
        out.append(int(x))
    if not out:
        raise ValueError("windows cannot be empty")
    return out


def _quantile(values, q):
    if not values:
        return 0.0
    xs = sorted(float(x) for x in values)
    if q <= 0:
        return xs[0]
    if q >= 1:
        return xs[-1]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def _cum_return(returns):
    eq = 1.0
    for r in returns:
        eq *= 1.0 + float(r)
    return eq - 1.0


def _corr(a, b):
    if len(a) != len(b) or len(a) < 3:
        return 0.0
    ma = statistics.mean(a)
    mb = statistics.mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 1e-12 or vb <= 1e-12:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def _annual_sharpe(returns):
    if len(returns) < 2:
        return 0.0
    mu = statistics.mean(returns)
    sd = statistics.pstdev(returns)
    if sd <= 1e-12:
        return 0.0
    return (mu * 365.0) / (sd * math.sqrt(365.0))


def _series_metrics(returns, benchmark=None, exposures=None):
    up = sum(1 for x in returns if x > 0) / max(1, len(returns))
    cum = _cum_return(returns)
    ann_factor = 365.0 / max(1, len(returns))
    cagr = (1.0 + cum) ** ann_factor - 1.0
    sharpe = _annual_sharpe(returns)
    out = {
        "cum_return": cum,
        "cagr": cagr,
        "sharpe": sharpe,
        "positive_day_rate": up,
    }
    if exposures is not None and len(exposures) == len(returns):
        out["active_exposure_rate"] = sum(1 for x in exposures if x > 1e-6) / max(1, len(exposures))
        out["avg_gross_exposure"] = statistics.mean(exposures) if exposures else 0.0
    if benchmark is not None and len(benchmark) == len(returns):
        up_idx = [i for i, br in enumerate(benchmark) if br > 0]
        dn_idx = [i for i, br in enumerate(benchmark) if br < 0]
        if up_idx:
            out["up_capture"] = statistics.mean(returns[i] for i in up_idx) / max(
                1e-9, statistics.mean(benchmark[i] for i in up_idx)
            )
        if dn_idx:
            bdn = statistics.mean(benchmark[i] for i in dn_idx)
            out["down_capture"] = statistics.mean(returns[i] for i in dn_idx) / min(-1e-9, bdn)
        out["corr_to_btc"] = _corr(returns, benchmark)
    return out


def _simulate_local_returns(data, params, window_days, regime_symbol):
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
    start = max(warmup, n - int(window_days))
    end = n
    if start >= end:
        raise BacktestError("Invalid local backtest range")

    prev = {s: 0.0 for s in symbols}
    highest_close = {s: None for s in symbols}
    day_returns = []
    exposures = []

    for t in range(start, end):
        if (t - start) % rebalance_every != 0:
            w = dict(prev)
        else:
            w = {s: 0.0 for s in symbols}
            moms = []
            vol_map = {}
            for s in symbols:
                m_fast = closes[s][t - 1] / closes[s][t - 1 - lb_fast] - 1.0
                m_slow = closes[s][t - 1] / closes[s][t - 1 - lb_slow] - 1.0
                score = 0.6 * m_fast + 0.4 * m_slow
                sma_v = sum(closes[s][t - sma_filter : t]) / sma_filter
                trend = closes[s][t - 1] > sma_v
                vol_map[s] = max(1e-6, statistics.pstdev(rets[s][t - vol_lb : t]))
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
                regime_v = sum(closes[regime_symbol][t - regime_sma : t]) / regime_sma
                risk_on = closes[regime_symbol][t - 1] > regime_v
                if not risk_on:
                    gross = sum(w.values())
                    if gross > 0:
                        sc = min(1.0, risk_off_exposure / gross)
                        for s in w:
                            w[s] *= sc

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
        day_r = sum(w[s] * rets[s][t] for s in symbols) - tc
        day_returns.append(day_r)
        exposures.append(sum(w.values()))
        prev = w
    return day_returns, exposures


def _simulate_recurring_returns(closes, window_days, trade_cost=0.0015):
    arr = closes[-(window_days + 1) :]
    cash = 1.0
    btc = 0.0
    dca = 1.0 / max(1, window_days)
    out = []
    prev_eq = 1.0
    for t in range(1, len(arr)):
        px = arr[t]
        invest = min(dca, cash)
        if invest > 0:
            btc += invest * (1.0 - trade_cost) / px
            cash -= invest
        eq = cash + btc * px
        out.append(eq / prev_eq - 1.0)
        prev_eq = eq
    return out


def _simulate_spot_dca_returns(closes, window_days, trade_cost=0.0015):
    arr = closes[-(window_days + 1) :]
    cash = 1.0
    btc = 0.0
    avg_cost = 0.0
    safety_used = 0
    base = max(0.005, min(0.02, (1.0 / max(1, window_days)) * 8.0))
    out = []
    prev_eq = 1.0
    for i in range(1, len(arr)):
        px = arr[i]
        if btc == 0:
            lookback = arr[max(0, i - 7) : i]
            recent_high = max(lookback) if lookback else px
            pullback = (px / recent_high - 1.0) <= -0.02
            momentum = (px / arr[max(0, i - 20)] - 1.0) > 0 if i >= 20 else False
            if (pullback or momentum) and cash > 0:
                invest = min(base, cash)
                qty = invest * (1.0 - trade_cost) / px
                btc = qty
                cash -= invest
                avg_cost = px
                safety_used = 0
        else:
            pnl = px / avg_cost - 1.0
            if pnl >= 0.06 or pnl <= -0.07:
                cash += btc * px * (1.0 - trade_cost)
                btc = 0.0
                avg_cost = 0.0
                safety_used = 0
            else:
                next_drop = -0.02 * (safety_used + 1)
                if pnl <= next_drop and safety_used < 3 and cash > 0:
                    invest = min(base * (1.0 + 0.5 * safety_used), cash)
                    qty = invest * (1.0 - trade_cost) / px
                    avg_cost = (btc * avg_cost + qty * px) / (btc + qty)
                    btc += qty
                    cash -= invest
                    safety_used += 1
        eq = cash + btc * px
        out.append(eq / prev_eq - 1.0)
        prev_eq = eq
    return out


def _simulate_grid_returns(closes, window_days, trade_cost=0.0015):
    arr = closes[-(window_days + 1) :]
    cash = 0.5
    btc = 0.5 / arr[0]
    step = 0.20 / 12.0
    last_ref = arr[0]
    out = []
    prev_eq = 1.0
    for i in range(1, len(arr)):
        px = arr[i]
        move = px / last_ref - 1.0
        unit = 1.0 / 12.0
        while move <= -step and cash > 1e-8:
            invest = min(unit / 2.0, cash)
            if invest <= 0:
                break
            btc += invest * (1.0 - trade_cost) / px
            cash -= invest
            last_ref *= 1.0 - step
            move = px / last_ref - 1.0
        while move >= step and btc > 1e-12:
            qty = min((unit / 2.0) / px, btc)
            if qty <= 0:
                break
            cash += qty * px * (1.0 - trade_cost)
            btc -= qty
            last_ref *= 1.0 + step
            move = px / last_ref - 1.0
        eq = cash + btc * px
        out.append(eq / prev_eq - 1.0)
        prev_eq = eq
    return out


def _normalize_weights(weights):
    out = {}
    s = 0.0
    for k, v in weights.items():
        vv = max(0.0, float(v))
        out[k] = vv
        s += vv
    if s <= 1e-12:
        return {k: 1.0 / len(out) for k in out}
    return {k: v / s for k, v in out.items()}


def _load_latest_hot_weights(skill_root):
    results_dir = Path(skill_root) / "results"
    files = sorted(results_dir.glob("hot_strategy_advice_*.json"))
    if not files:
        return {"grid": 1 / 3, "spot_dca": 1 / 3, "recurring": 1 / 3}, None
    latest = files[-1]
    payload = json.loads(latest.read_text())
    selected = payload.get("selected") or []
    if not selected:
        return {"grid": 1 / 3, "spot_dca": 1 / 3, "recurring": 1 / 3}, str(latest)

    score_map = {}
    for row in selected:
        st = str(row.get("strategy_type", "")).strip()
        if st in {"grid", "spot_dca", "recurring"}:
            score_map[st] = float(row.get("score", 0.0))
    for st in ("grid", "spot_dca", "recurring"):
        score_map.setdefault(st, 0.0)
    return _normalize_weights(score_map), str(latest)


def _weighted_mix_series(series_map, weights):
    n = min(len(v) for v in series_map.values())
    out = []
    for i in range(n):
        out.append(sum(float(weights.get(k, 0.0)) * float(series_map[k][i]) for k in series_map))
    return out


def _block_sample(values, block_size, rng):
    n = len(values)
    out = []
    while len(out) < n:
        start = rng.randrange(0, n)
        for j in range(block_size):
            out.append(values[(start + j) % n])
            if len(out) >= n:
                break
    return out


def _bootstrap_outperform(local_rets, hot_rets, iterations, block_size, confidence, seed):
    rng = random.Random(seed)
    wins = 0
    diff_samples = []
    local_samples = []
    hot_samples = []
    for _ in range(int(iterations)):
        l = _block_sample(local_rets, block_size, rng)
        h = _block_sample(hot_rets, block_size, rng)
        cl = _cum_return(l)
        ch = _cum_return(h)
        local_samples.append(cl)
        hot_samples.append(ch)
        d = cl - ch
        diff_samples.append(d)
        if d > 0:
            wins += 1
    alpha = 1.0 - float(confidence)
    lo_q = alpha / 2.0
    hi_q = 1.0 - lo_q
    return {
        "iterations": int(iterations),
        "block_size": int(block_size),
        "confidence": float(confidence),
        "prob_local_outperform": wins / max(1, int(iterations)),
        "ci_local_cum_return": [_quantile(local_samples, lo_q), _quantile(local_samples, hi_q)],
        "ci_hot_cum_return": [_quantile(hot_samples, lo_q), _quantile(hot_samples, hi_q)],
        "ci_diff_cum_return": [_quantile(diff_samples, lo_q), _quantile(diff_samples, hi_q)],
        "median_diff_cum_return": _quantile(diff_samples, 0.5),
    }


def _judge(bootstrap_result):
    lo, hi = bootstrap_result["ci_diff_cum_return"]
    p = bootstrap_result["prob_local_outperform"]
    if lo > 0:
        return "local_higher_with_confidence"
    if hi < 0:
        return "hot_higher_with_confidence"
    if p >= 0.6:
        return "local_higher_but_not_significant"
    if p <= 0.4:
        return "hot_higher_but_not_significant"
    return "inconclusive"


def _recommend_allocation(verdict, gate, total_usdt, hot_weights):
    if verdict == "local_higher_with_confidence":
        hot_ratio = 0.05
    elif verdict == "hot_higher_with_confidence":
        hot_ratio = 0.25
    elif verdict == "local_higher_but_not_significant":
        hot_ratio = 0.10
    elif verdict == "hot_higher_but_not_significant":
        hot_ratio = 0.15
    else:
        hot_ratio = 0.10

    gate_block = (gate.get("mode") == "hold_cash") or bool(gate.get("risk_rising_used"))
    if gate_block:
        hot_ratio = min(hot_ratio, 0.10)
    local_ratio = max(0.0, 1.0 - hot_ratio)

    out = {
        "local_strategy_ratio": round(local_ratio, 4),
        "hot_strategy_ratio": round(hot_ratio, 4),
        "hot_internal_ratio": {k: round(v, 4) for k, v in hot_weights.items()},
        "gate_block": bool(gate_block),
        "gate_mode": gate.get("mode"),
        "risk_rising_used": gate.get("risk_rising_used"),
    }
    if total_usdt is not None:
        hot_amt = float(total_usdt) * hot_ratio
        out["amounts_usdt"] = {
            "local_strategy": round(float(total_usdt) * local_ratio, 2),
            "hot_strategy": round(hot_amt, 2),
            "hot_grid": round(hot_amt * hot_weights.get("grid", 0.0), 2),
            "hot_spot_dca": round(hot_amt * hot_weights.get("spot_dca", 0.0), 2),
            "hot_recurring": round(hot_amt * hot_weights.get("recurring", 0.0), 2),
        }
    return out


def _build_text(payload):
    lines = []
    lines.append("Signal-Level Comparison (Local vs Hot)")
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"Profile: {payload['profile']}")
    lines.append(f"Windows: {payload['windows']}")
    lines.append("")
    lines.append(f"Verdict: {payload['verdict']}")
    lines.append(f"P(Local > Hot): {payload['bootstrap_365']['prob_local_outperform']:.2%}")
    lo, hi = payload["bootstrap_365"]["ci_diff_cum_return"]
    lines.append(f"95% CI(Local-Hot cumulative return): [{lo:.2%}, {hi:.2%}]")
    lines.append("")
    alloc = payload["allocation_recommendation"]
    lines.append("Allocation Recommendation:")
    lines.append(
        f"- local={alloc['local_strategy_ratio']:.0%}, hot={alloc['hot_strategy_ratio']:.0%} "
        f"(gate_block={alloc['gate_block']}, mode={alloc['gate_mode']})"
    )
    if "amounts_usdt" in alloc:
        am = alloc["amounts_usdt"]
        lines.append(
            f"- USDT amounts: local={am['local_strategy']} hot={am['hot_strategy']} "
            f"(grid={am['hot_grid']}, spot_dca={am['hot_spot_dca']}, recurring={am['hot_recurring']})"
        )
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(
        description="Signal-level compare local strategy vs hot-strategy proxy with bootstrap confidence intervals."
    )
    p.add_argument("--profile", type=str, default="stable")
    p.add_argument("--windows", type=str, default="180,365,730")
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--bootstrap-iters", type=int, default=2000)
    p.add_argument("--bootstrap-block", type=int, default=7)
    p.add_argument("--confidence", type=float, default=0.95)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--total-usdt", type=float, default=None)
    p.add_argument("--format", choices=["json", "text"], default="text")
    p.add_argument("--no-save-results", action="store_true")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    windows = _parse_windows(args.windows)

    profiles = load_profiles(skill_root)
    if args.profile not in profiles:
        raise SystemExit(f"Unknown profile: {args.profile}")
    params = profiles[args.profile]
    fee = float(params.get("fee", 0.001))
    slip = float(params.get("slip", 0.0005))
    trade_cost = max(0.0, fee + slip)
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]

    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=skill_root / "cache",
        ttl_hours=args.cache_ttl_hours,
    )
    regime_symbol = resolve_regime_symbol(symbols, args.regime_symbol)
    btc_rets = align_ohlc(data)[5].get(regime_symbol, [])

    closes_btc = [float(x[4]) for x in data[regime_symbol]]
    hot_weights, hot_weights_source = _load_latest_hot_weights(skill_root)

    per_window = {}
    local_series_365 = None
    hot_series_365 = None
    btc_series_365 = None
    local_exp_365 = None
    for w in windows:
        local_rets, local_exp = _simulate_local_returns(data, params, w, regime_symbol)
        grid_rets = _simulate_grid_returns(closes_btc, w, trade_cost=trade_cost)
        dca_rets = _simulate_spot_dca_returns(closes_btc, w, trade_cost=trade_cost)
        rec_rets = _simulate_recurring_returns(closes_btc, w, trade_cost=trade_cost)
        hot_rets = _weighted_mix_series(
            {"grid": grid_rets, "spot_dca": dca_rets, "recurring": rec_rets},
            hot_weights,
        )
        m = min(len(local_rets), len(hot_rets))
        local_rets = local_rets[-m:]
        local_exp = local_exp[-m:]
        hot_rets = hot_rets[-m:]
        bench = btc_rets[-m:] if len(btc_rets) >= m else None

        per_window[str(w)] = {
            "local": _series_metrics(local_rets, benchmark=bench, exposures=local_exp),
            "hot_mix_proxy": _series_metrics(hot_rets, benchmark=bench),
            "hot_components_proxy": {
                "grid": _series_metrics(grid_rets[-m:], benchmark=bench),
                "spot_dca": _series_metrics(dca_rets[-m:], benchmark=bench),
                "recurring": _series_metrics(rec_rets[-m:], benchmark=bench),
            },
        }
        if w == 365:
            local_series_365 = local_rets
            hot_series_365 = hot_rets
            btc_series_365 = bench
            local_exp_365 = local_exp

    if local_series_365 is None:
        pick = str(min(windows, key=lambda x: abs(x - 365)))
        local_series_365 = []
        hot_series_365 = []
        btc_series_365 = None
        local_exp_365 = None
        # Reconstruct from already computed metrics isn't possible; rerun for nearest window.
        w = int(pick)
        local_rets, local_exp = _simulate_local_returns(data, params, w, regime_symbol)
        grid_rets = _simulate_grid_returns(closes_btc, w, trade_cost=trade_cost)
        dca_rets = _simulate_spot_dca_returns(closes_btc, w, trade_cost=trade_cost)
        rec_rets = _simulate_recurring_returns(closes_btc, w, trade_cost=trade_cost)
        hot_rets = _weighted_mix_series(
            {"grid": grid_rets, "spot_dca": dca_rets, "recurring": rec_rets},
            hot_weights,
        )
        m = min(len(local_rets), len(hot_rets))
        local_series_365 = local_rets[-m:]
        hot_series_365 = hot_rets[-m:]
        local_exp_365 = local_exp[-m:]
        btc_series_365 = btc_rets[-m:] if len(btc_rets) >= m else None

    boot = _bootstrap_outperform(
        local_series_365,
        hot_series_365,
        iterations=args.bootstrap_iters,
        block_size=args.bootstrap_block,
        confidence=args.confidence,
        seed=args.seed,
    )
    verdict = _judge(boot)

    gate = _load_main_strategy_gate(skill_root)
    total_usdt = args.total_usdt
    if total_usdt is None:
        total_usdt = _load_total_usdt_live()

    allocation = _recommend_allocation(verdict, gate, total_usdt, hot_weights)

    out = {
        "generated_at": datetime.now().isoformat(),
        "profile": args.profile,
        "windows": windows,
        "regime_symbol": regime_symbol,
        "method": {
            "comparison": "signal_level",
            "hot_backtest": "proxy_simulation_for_grid_spot_dca_recurring",
            "confidence_method": "block_bootstrap",
            "bootstrap_iters": args.bootstrap_iters,
            "bootstrap_block": args.bootstrap_block,
            "confidence": args.confidence,
            "capital_normalization": "Both local and hot are normalized to initial equity = 1.0",
            "window_alignment": "Same trailing daily windows and end date for both local/hot.",
            "trade_cost_alignment": {
                "fee": fee,
                "slip": slip,
                "total_per_turnover": trade_cost,
            },
        },
        "hot_weights_source": hot_weights_source,
        "hot_internal_weights": hot_weights,
        "per_window": per_window,
        "bootstrap_365": boot,
        "verdict": verdict,
        "gate": gate,
        "allocation_recommendation": allocation,
    }

    if not args.no_save_results:
        results_dir = skill_root / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = results_dir / f"signal_compare_{ts}.json"
        fp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        out["results_file"] = str(fp)

    if args.format == "json":
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(_build_text(out))


if __name__ == "__main__":
    main()
