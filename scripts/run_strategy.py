#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from engine import DEFAULT_SYMBOLS, backtest, load_data, load_profiles


def _build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", type=str, default="stable")
    p.add_argument("--capital-cny", type=float, default=10000)
    p.add_argument("--window-days", type=int, default=365)
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--no-cache", action="store_true")

    # Optional overrides (None means use profile value)
    p.add_argument("--lb-fast", type=int)
    p.add_argument("--lb-slow", type=int)
    p.add_argument("--sma-filter", type=int)
    p.add_argument("--k", type=int)
    p.add_argument("--atr-mult", type=float)
    p.add_argument("--max-w-core", type=float)
    p.add_argument("--max-w-alt", type=float)
    p.add_argument("--vol-lb", type=int)
    p.add_argument("--target-vol", type=float)
    p.add_argument("--rebalance-every", type=int)
    p.add_argument("--regime-sma", type=int)
    p.add_argument("--risk-off-exposure", type=float)
    p.add_argument("--atr-period", type=int)
    p.add_argument("--fee", type=float)
    p.add_argument("--slip", type=float)
    return p


def _merge_params(profile_params, args):
    params = dict(profile_params)
    mapping = {
        "lb_fast": args.lb_fast,
        "lb_slow": args.lb_slow,
        "sma_filter": args.sma_filter,
        "k": args.k,
        "atr_mult": args.atr_mult,
        "max_w_core": args.max_w_core,
        "max_w_alt": args.max_w_alt,
        "vol_lb": args.vol_lb,
        "target_vol": args.target_vol,
        "rebalance_every": args.rebalance_every,
        "regime_sma": args.regime_sma,
        "risk_off_exposure": args.risk_off_exposure,
        "atr_period": args.atr_period,
        "fee": args.fee,
        "slip": args.slip,
    }
    for k, v in mapping.items():
        if v is not None:
            params[k] = v
    return params


def main():
    parser = _build_parser()
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    profiles = load_profiles(skill_root)
    if args.profile not in profiles:
        raise SystemExit(f"Unknown profile: {args.profile}. Available: {', '.join(sorted(profiles.keys()))}")

    params = _merge_params(profiles[args.profile], args)
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]

    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=skill_root / "cache",
        ttl_hours=args.cache_ttl_hours,
    )

    res = backtest(
        data,
        params=params,
        window_days=args.window_days,
        regime_symbol=args.regime_symbol,
    )

    final_cap = args.capital_cny * (1 + res["return"])
    out = {
        "profile": args.profile,
        "window_days": args.window_days,
        "capital_cny": args.capital_cny,
        "final_cny": round(final_cap, 2),
        "profit_cny": round(final_cap - args.capital_cny, 2),
        "return_pct": round(res["return"] * 100, 2),
        "cagr_pct": round(res["cagr"] * 100, 2),
        "max_drawdown_pct": round(res["max_drawdown"] * 100, 2),
        "vol_pct": round(res["vol"] * 100, 2),
        "sharpe": round(res["sharpe"], 3),
        "avg_daily_turnover": round(res["avg_daily_turnover"], 4),
        "bars": res["bars"],
        "regime_symbol_used": res["regime_symbol"],
        "latest_alloc": res["latest_alloc"],
        "params_used": params,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
