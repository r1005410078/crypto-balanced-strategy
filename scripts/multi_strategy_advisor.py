#!/usr/bin/env python3
import argparse
import json
import math
from datetime import datetime
from pathlib import Path

from engine import DEFAULT_SYMBOLS, backtest, load_data, load_profiles, resolve_regime_symbol


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _parse_int_list(text):
    vals = []
    for x in text.split(","):
        x = x.strip()
        if not x:
            continue
        vals.append(int(x))
    if not vals:
        raise ValueError("Empty window list")
    return vals


def _find_latest_opt_params(skill_root):
    results_dir = Path(skill_root) / "results"
    if not results_dir.exists():
        return None, None
    files = sorted(results_dir.glob("optimize_*.json"))
    if not files:
        return None, None
    latest = files[-1]
    payload = json.loads(latest.read_text())
    best = payload.get("best") or {}
    params = best.get("params")
    return params, str(latest)


def _load_ensemble_profiles(skill_root):
    p = Path(skill_root) / "ensemble_profiles.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _save_ensemble_profiles(skill_root, profiles):
    p = Path(skill_root) / "ensemble_profiles.json"
    p.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def _save_ensemble_result(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"ensemble_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def _strategy_score(metrics_by_window):
    # Local-opt objective: robust and stable, not extreme return chase.
    w365 = metrics_by_window.get(365)
    if w365 is None:
        # fallback to longest window
        longest = sorted(metrics_by_window.keys())[-1]
        w365 = metrics_by_window[longest]
    w120 = metrics_by_window.get(120, w365)

    ret = w365["return"]
    mdd = w365["max_drawdown"]
    sharpe = w365["sharpe"]
    turn = w365["avg_daily_turnover"]
    calmar_like = ret / abs(mdd) if mdd < 0 else 0.0

    # consistency across windows
    pos_rate = sum(1 for v in metrics_by_window.values() if v["return"] > 0) / len(metrics_by_window)

    score = (
        0.45 * _clip(ret, -0.25, 0.60)
        + 0.20 * _clip(sharpe, -1.5, 2.5)
        + 0.20 * _clip(calmar_like, -1.0, 2.5)
        + 0.10 * pos_rate
        + 0.05 * _clip(w120["return"], -0.20, 0.40)
    )
    score -= max(0.0, abs(mdd) - 0.22) * 1.5
    score -= max(0.0, turn - 0.10) * 0.8
    return score


def _softmax_weights(scored, temperature=3.0):
    # Use positive scores only; fallback to best one if all non-positive.
    positives = [(n, s) for n, s in scored if s > 0]
    if not positives:
        best = max(scored, key=lambda x: x[1])[0]
        return {n: (1.0 if n == best else 0.0) for n, _ in scored}

    vals = [s for _, s in positives]
    m = max(vals)
    exps = [(n, math.exp((s - m) * temperature)) for n, s in positives]
    sm = sum(v for _, v in exps)
    out = {n: v / sm for n, v in exps}
    for n, _ in scored:
        out.setdefault(n, 0.0)
    return out


def _combine_allocations(strategy_weights, strategy_allocs):
    assets = set()
    for alloc in strategy_allocs.values():
        assets.update(alloc.keys())
    out = {a: 0.0 for a in assets}
    for s, w in strategy_weights.items():
        alloc = strategy_allocs[s]
        for a, aw in alloc.items():
            out[a] += w * aw

    # Normalize if tiny floating drift
    sw = sum(out.values())
    if sw > 1.0:
        for a in out:
            out[a] /= sw
    out = {k: round(v, 4) for k, v in out.items()}
    return dict(sorted(out.items(), key=lambda x: x[1], reverse=True))


def _alloc_to_capital(allocation, capital_cny):
    return {k: round(v * capital_cny, 2) for k, v in allocation.items()}


def _evaluate_one(name, params, data, windows, signal_window, regime_symbol):
    metrics_by_window = {}
    for w in windows:
        metrics_by_window[w] = backtest(data, params=params, window_days=w, regime_symbol=regime_symbol)

    signal_metrics = backtest(data, params=params, window_days=signal_window, regime_symbol=regime_symbol)
    score = _strategy_score(metrics_by_window)
    return {
        "name": name,
        "score": score,
        "params": params,
        "metrics": {
            str(w): {
                "return_pct": round(metrics_by_window[w]["return"] * 100, 2),
                "max_drawdown_pct": round(metrics_by_window[w]["max_drawdown"] * 100, 2),
                "sharpe": round(metrics_by_window[w]["sharpe"], 3),
                "avg_daily_turnover": round(metrics_by_window[w]["avg_daily_turnover"], 4),
            }
            for w in windows
        },
        "signal": {
            "window_days": signal_window,
            "latest_alloc": signal_metrics["latest_alloc"],
            "regime_symbol_used": signal_metrics["regime_symbol"],
        },
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profiles", type=str, default="stable,balanced,aggressive")
    p.add_argument("--include-latest-opt", action="store_true")
    p.add_argument("--use-merged-profile", type=str, default=None)
    p.add_argument("--write-merged-profile", type=str, default=None)
    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--capital-cny", type=float, default=10000)
    p.add_argument("--windows", type=str, default="120,365,730")
    p.add_argument("--signal-window", type=int, default=365)
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    profiles = load_profiles(skill_root)
    ensemble_profiles = _load_ensemble_profiles(skill_root)

    if args.use_merged_profile:
        if args.use_merged_profile not in ensemble_profiles:
            raise SystemExit(
                f"Unknown merged profile: {args.use_merged_profile}. "
                f"Available: {', '.join(sorted(ensemble_profiles.keys()))}"
            )
        ep = ensemble_profiles[args.use_merged_profile]
        profile_names = ep.get("profiles", [])
        include_latest_opt = bool(ep.get("include_latest_opt", False))
        windows = [int(x) for x in ep.get("windows", [120, 365, 730])]
        signal_window = int(ep.get("signal_window", args.signal_window))
        symbols = [str(x).upper() for x in ep.get("symbols", DEFAULT_SYMBOLS)]
        regime_symbol_pref = str(ep.get("regime_symbol", args.regime_symbol)).upper()
    else:
        profile_names = [x.strip() for x in args.profiles.split(",") if x.strip()]
        include_latest_opt = bool(args.include_latest_opt)
        windows = _parse_int_list(args.windows)
        signal_window = args.signal_window
        symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
        regime_symbol_pref = args.regime_symbol

    strategy_params = {}
    for n in profile_names:
        if n not in profiles:
            raise SystemExit(f"Unknown profile: {n}. Available: {', '.join(sorted(profiles.keys()))}")
        strategy_params[n] = profiles[n]

    latest_opt_file = None
    if include_latest_opt:
        opt_params, latest_opt_file = _find_latest_opt_params(skill_root)
        if opt_params:
            strategy_params["latest_opt"] = opt_params

    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=skill_root / "cache",
        ttl_hours=args.cache_ttl_hours,
    )
    regime_symbol = resolve_regime_symbol(symbols, regime_symbol_pref)

    evaluations = []
    for name, params in strategy_params.items():
        evaluations.append(
            _evaluate_one(
                name=name,
                params=params,
                data=data,
                windows=windows,
                signal_window=signal_window,
                regime_symbol=regime_symbol,
            )
        )

    scored = [(x["name"], x["score"]) for x in evaluations]
    strat_weights = _softmax_weights(scored, temperature=3.0)
    strat_weights = {k: round(v, 4) for k, v in strat_weights.items()}

    strategy_allocs = {x["name"]: x["signal"]["latest_alloc"] for x in evaluations}
    merged_alloc = _combine_allocations(strat_weights, strategy_allocs)
    cny_plan = _alloc_to_capital(merged_alloc, args.capital_cny)

    merged_profile_file = None
    updated_merged_profile = None
    if args.write_merged_profile:
        ensemble_profiles[args.write_merged_profile] = {
            "created_at": datetime.now().isoformat(),
            "profiles": profile_names,
            "include_latest_opt": bool(include_latest_opt),
            "windows": windows,
            "signal_window": signal_window,
            "symbols": symbols,
            "regime_symbol": regime_symbol,
            "strategy_weights": strat_weights,
            "latest_opt_file": latest_opt_file,
        }
        merged_profile_file = _save_ensemble_profiles(skill_root, ensemble_profiles)
        updated_merged_profile = args.write_merged_profile

    out = {
        "source_merged_profile": args.use_merged_profile,
        "capital_cny": args.capital_cny,
        "windows": windows,
        "signal_window": signal_window,
        "symbols": symbols,
        "regime_symbol": regime_symbol,
        "latest_opt_file": latest_opt_file,
        "updated_merged_profile": updated_merged_profile,
        "merged_profile_file": merged_profile_file,
        "strategy_scores": {x["name"]: round(x["score"], 4) for x in evaluations},
        "strategy_weights": strat_weights,
        "merged_allocation": merged_alloc,
        "capital_plan_cny": cny_plan,
        "strategies": evaluations,
    }

    if not args.no_save_results:
        out["results_file"] = _save_ensemble_result(skill_root, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
