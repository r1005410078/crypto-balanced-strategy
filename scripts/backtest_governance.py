#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from engine import (
    DEFAULT_SYMBOLS,
    align_ohlc,
    backtest,
    load_data,
    load_profiles,
    resolve_regime_symbol,
)


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _status_from_checks(baseline, checks):
    # Conservative decisioning from backtest-expert philosophy.
    if baseline["return"] <= 0 or baseline["max_drawdown"] <= -0.30:
        return "ABANDON"

    weak = 0
    if checks["friction_pass_rate"] < 0.67:
        weak += 1
    if checks["sensitivity_pass_rate"] < 0.60:
        weak += 1
    if checks["window_positive_rate"] < 0.67:
        weak += 1
    if checks["walk_forward_positive_rate"] < 0.67:
        weak += 1

    if weak >= 2:
        return "REFINE"
    return "DEPLOY_CANDIDATE"


def _summarize(metrics):
    return {
        "return_pct": round(metrics["return"] * 100, 2),
        "cagr_pct": round(metrics["cagr"] * 100, 2),
        "max_drawdown_pct": round(metrics["max_drawdown"] * 100, 2),
        "sharpe": round(metrics["sharpe"], 3),
        "avg_daily_turnover": round(metrics["avg_daily_turnover"], 4),
    }


def _run_friction_stress(data, params, regime_symbol, window_days):
    scenarios = []
    pass_cnt = 0
    # Baseline, 1.5x, 2.0x cost stress
    for m in [1.0, 1.5, 2.0]:
        p = dict(params)
        p["fee"] = float(params["fee"]) * m
        p["slip"] = float(params["slip"]) * m
        r = backtest(data, params=p, window_days=window_days, regime_symbol=regime_symbol)
        ok = (r["return"] > 0) and (r["max_drawdown"] > -0.30)
        if ok:
            pass_cnt += 1
        scenarios.append({
            "cost_multiplier": m,
            "metrics": _summarize(r),
            "pass": ok,
        })
    return scenarios, pass_cnt / len(scenarios)


def _run_param_sensitivity(data, params, regime_symbol, window_days):
    # Perturb key parameters around baseline and look for plateau.
    base = dict(params)
    variants = []

    lb_fast = int(base["lb_fast"])
    lb_slow = int(base["lb_slow"])
    sma_filter = int(base["sma_filter"])
    atr_mult = float(base["atr_mult"])
    target_vol = float(base["target_vol"])
    risk_off = float(base["risk_off_exposure"])

    def add_variant(label, mutate):
        p = dict(base)
        mutate(p)
        if p["lb_slow"] <= p["lb_fast"]:
            return
        variants.append((label, p))

    add_variant("base", lambda p: None)
    add_variant("lb_fast_-20pct", lambda p: p.update(lb_fast=max(5, int(lb_fast * 0.8))))
    add_variant("lb_fast_+20pct", lambda p: p.update(lb_fast=int(lb_fast * 1.2)))
    add_variant("lb_slow_-20pct", lambda p: p.update(lb_slow=max(lb_fast + 5, int(lb_slow * 0.8))))
    add_variant("lb_slow_+20pct", lambda p: p.update(lb_slow=int(lb_slow * 1.2)))
    add_variant("sma_-20pct", lambda p: p.update(sma_filter=max(40, int(sma_filter * 0.8))))
    add_variant("sma_+20pct", lambda p: p.update(sma_filter=int(sma_filter * 1.2)))
    add_variant("atr_-15pct", lambda p: p.update(atr_mult=round(max(1.0, atr_mult * 0.85), 3)))
    add_variant("atr_+15pct", lambda p: p.update(atr_mult=round(atr_mult * 1.15, 3)))
    add_variant("target_vol_-15pct", lambda p: p.update(target_vol=round(max(0.10, target_vol * 0.85), 3)))
    add_variant("target_vol_+15pct", lambda p: p.update(target_vol=round(min(0.80, target_vol * 1.15), 3)))
    add_variant("risk_off_-25pct", lambda p: p.update(risk_off_exposure=round(max(0.05, risk_off * 0.75), 3)))
    add_variant("risk_off_+25pct", lambda p: p.update(risk_off_exposure=round(min(1.0, risk_off * 1.25), 3)))

    results = []
    pass_cnt = 0
    for label, p in variants:
        r = backtest(data, params=p, window_days=window_days, regime_symbol=regime_symbol)
        ok = (r["return"] > 0) and (r["max_drawdown"] > -0.30)
        if ok:
            pass_cnt += 1
        results.append({
            "variant": label,
            "metrics": _summarize(r),
            "pass": ok,
        })

    return results, pass_cnt / len(results)


def _run_window_robustness(data, params, regime_symbol, windows):
    rows = []
    pos = 0
    for w in windows:
        r = backtest(data, params=params, window_days=w, regime_symbol=regime_symbol)
        ok = (r["return"] > 0)
        if ok:
            pos += 1
        rows.append({"window_days": w, "metrics": _summarize(r), "positive": ok})
    return rows, pos / len(rows)


def _run_walk_forward(data, params, regime_symbol, fold_days=180, fold_count=2):
    _, n, _, _, _, _ = align_ohlc(data)

    rows = []
    pos = 0
    for i in range(fold_count):
        end = n - (fold_count - 1 - i) * fold_days
        start = end - fold_days
        if start < 0:
            continue
        r = backtest(data, params=params, start_index=start, end_index=end, regime_symbol=regime_symbol)
        ok = r["return"] > 0
        if ok:
            pos += 1
        rows.append({
            "fold": i + 1,
            "start_index": start,
            "end_index": end,
            "metrics": _summarize(r),
            "positive": ok,
        })

    if not rows:
        return [], 0.0
    return rows, pos / len(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", type=str, default="stable")
    p.add_argument("--hypothesis", type=str, default="")
    p.add_argument("--capital-cny", type=float, default=10000)
    p.add_argument("--window-days", type=int, default=365)
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    skill_root = Path(__file__).resolve().parent.parent
    profiles = load_profiles(skill_root)
    if args.profile not in profiles:
        raise SystemExit(f"Unknown profile: {args.profile}. Available: {', '.join(sorted(profiles.keys()))}")

    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=skill_root / "cache",
        ttl_hours=args.cache_ttl_hours,
    )
    regime_symbol = resolve_regime_symbol(symbols, args.regime_symbol)
    params = dict(profiles[args.profile])

    baseline = backtest(data, params=params, window_days=args.window_days, regime_symbol=regime_symbol)
    friction_rows, friction_pass = _run_friction_stress(data, params, regime_symbol, args.window_days)
    sensitivity_rows, sensitivity_pass = _run_param_sensitivity(data, params, regime_symbol, args.window_days)
    window_rows, window_pos_rate = _run_window_robustness(data, params, regime_symbol, [120, 365, 730])
    wf_rows, wf_pos_rate = _run_walk_forward(data, params, regime_symbol, fold_days=180, fold_count=2)

    checks = {
        "friction_pass_rate": round(friction_pass, 3),
        "sensitivity_pass_rate": round(sensitivity_pass, 3),
        "window_positive_rate": round(window_pos_rate, 3),
        "walk_forward_positive_rate": round(wf_pos_rate, 3),
    }
    decision = _status_from_checks(baseline, checks)

    confidence = _clip(
        0.35
        + 0.25 * checks["friction_pass_rate"]
        + 0.20 * checks["sensitivity_pass_rate"]
        + 0.10 * checks["window_positive_rate"]
        + 0.10 * checks["walk_forward_positive_rate"],
        0.0,
        1.0,
    )

    final_cny = args.capital_cny * (1 + baseline["return"])

    out = {
        "profile": args.profile,
        "hypothesis": args.hypothesis,
        "hypothesis_provided": bool(args.hypothesis.strip()),
        "capital_cny": args.capital_cny,
        "baseline": {
            **_summarize(baseline),
            "final_cny": round(final_cny, 2),
            "profit_cny": round(final_cny - args.capital_cny, 2),
            "latest_alloc": baseline["latest_alloc"],
        },
        "robustness_checks": checks,
        "decision": decision,
        "confidence": round(confidence, 3),
        "friction_stress": friction_rows,
        "parameter_sensitivity": {
            "pass_rate": round(sensitivity_pass, 3),
            "variants": sensitivity_rows,
        },
        "window_robustness": window_rows,
        "walk_forward": wf_rows,
        "regime_symbol_used": regime_symbol,
        "params_used": params,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
