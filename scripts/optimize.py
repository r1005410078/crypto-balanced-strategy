#!/usr/bin/env python3
import argparse
import json
import multiprocessing as mp
import statistics
import time
from datetime import datetime
from itertools import product
from pathlib import Path

from engine import (
    DEFAULT_SYMBOLS,
    align_ohlc,
    backtest,
    load_data,
    load_profiles,
    resolve_regime_symbol,
    save_profiles,
)

# Worker context for multiprocessing
_WORKER_CTX = {}


def _candidate_grid(mode="full"):
    if mode == "quick":
        return product(
            [20],           # lb_fast
            [50, 60],       # lb_slow
            [100, 120],     # sma_filter
            [1],            # k
            [2.8],          # atr_mult
            [0.55, 0.60],   # max_w_core
            [0.30, 0.35],   # max_w_alt
            [20],           # vol_lb
            [0.28, 0.35],   # target_vol
            [3, 7],         # rebalance_every
            [200],          # regime_sma
            [0.30, 0.40],   # risk_off_exposure
            [14],           # atr_period
            [0.001],        # fee
            [0.0005],       # slip
        )
    return product(
        [20, 30],        # lb_fast
        [50, 60, 80],    # lb_slow
        [80, 100, 120],  # sma_filter
        [1, 2],          # k
        [2.2, 2.8, 3.2], # atr_mult
        [0.55, 0.60],    # max_w_core
        [0.30, 0.35],    # max_w_alt
        [20],            # vol_lb
        [0.28, 0.35],    # target_vol
        [1, 3, 7],       # rebalance_every
        [200],           # regime_sma
        [0.30, 0.40],    # risk_off_exposure
        [14],            # atr_period
        [0.001],         # fee
        [0.0005],        # slip
    )


def _build_params(row):
    (
        lb_fast,
        lb_slow,
        sma_filter,
        k,
        atr_mult,
        max_w_core,
        max_w_alt,
        vol_lb,
        target_vol,
        rebalance_every,
        regime_sma,
        risk_off_exposure,
        atr_period,
        fee,
        slip,
    ) = row
    return {
        "lb_fast": lb_fast,
        "lb_slow": lb_slow,
        "sma_filter": sma_filter,
        "k": k,
        "atr_mult": atr_mult,
        "max_w_core": max_w_core,
        "max_w_alt": max_w_alt,
        "vol_lb": vol_lb,
        "target_vol": target_vol,
        "rebalance_every": rebalance_every,
        "regime_sma": regime_sma,
        "risk_off_exposure": risk_off_exposure,
        "atr_period": atr_period,
        "fee": fee,
        "slip": slip,
    }


def _oos_folds(n, warmup, fold_days=180, fold_count=3):
    folds = []
    for i in range(fold_count):
        end = n - (fold_count - 1 - i) * fold_days
        start = end - fold_days
        if start <= warmup:
            continue
        folds.append((start, end))
    return folds


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _score(insample, oos_list):
    # Robust stats for unstable crypto regimes:
    # use median OOS fold return (not annualized CAGR) + clipped risk metrics.
    ret_list = [x["return"] for x in oos_list]
    cagr_list = [x["cagr"] for x in oos_list]
    sharpe_list = [x["sharpe"] for x in oos_list]
    mdd_list = [x["max_drawdown"] for x in oos_list]
    turn_list = [x["avg_daily_turnover"] for x in oos_list]

    oos_ret_med = statistics.median(ret_list)
    oos_cagr_med = statistics.median(cagr_list)
    oos_sharpe_med = statistics.median(sharpe_list)
    oos_mdd_worst = min(mdd_list)
    oos_turn_med = statistics.median(turn_list)
    oos_pos_rate = sum(1 for x in oos_list if x["return"] > 0) / len(oos_list)

    # Calmar-like on return basis to avoid short-window annualization blowups
    oos_calmar_like = oos_ret_med / abs(oos_mdd_worst) if oos_mdd_worst < 0 else 0.0

    # Winsorize objective terms to reduce outlier dominance
    ret_eff = _clip(oos_ret_med, -0.35, 0.50)
    sharpe_eff = _clip(oos_sharpe_med, -1.50, 2.50)
    calmar_eff = _clip(oos_calmar_like, -1.00, 2.50)
    ins_sharpe_eff = _clip(insample["sharpe"], -1.50, 2.50)

    # OOS-first objective (dimensionless blended score)
    score = 0.50 * ret_eff + 0.20 * sharpe_eff + 0.20 * calmar_eff + 0.10 * oos_pos_rate
    score += 0.03 * ins_sharpe_eff

    # Penalties for instability and friction
    score -= max(0.0, abs(oos_mdd_worst) - 0.22) * 1.8
    score -= max(0.0, oos_turn_med - 0.12) * 0.8

    return {
        "score": score,
        "oos_ret_med": oos_ret_med,
        "oos_cagr_med": oos_cagr_med,
        "oos_sharpe_med": oos_sharpe_med,
        "oos_mdd_worst": oos_mdd_worst,
        "oos_turn_med": oos_turn_med,
        "oos_pos_rate": oos_pos_rate,
    }


def _evaluate_candidate(params, data, regime_symbol, n, fold_days, fold_count, min_valid_folds):
    if params["lb_slow"] <= params["lb_fast"]:
        return None

    warmup = max(
        params["lb_slow"],
        params["sma_filter"],
        params["vol_lb"],
        params["regime_sma"],
        params["atr_period"],
    ) + 3

    folds = _oos_folds(n, warmup, fold_days=fold_days, fold_count=fold_count)
    if len(folds) < min_valid_folds:
        return None

    insample = backtest(data, params=params, regime_symbol=regime_symbol)
    oos_list = []
    for s, e in folds:
        oos_list.append(backtest(data, params=params, start_index=s, end_index=e, regime_symbol=regime_symbol))

    ss = _score(insample, oos_list)
    return {
        "params": params,
        "insample": insample,
        "oos": {
            "fold_count": len(oos_list),
            "return_pct_list": [round(x["return"] * 100, 2) for x in oos_list],
            "return_pct_median": round(ss["oos_ret_med"] * 100, 2),
            "cagr_pct_median": round(ss["oos_cagr_med"] * 100, 2),
            "sharpe_median": round(ss["oos_sharpe_med"], 3),
            "max_drawdown_pct_worst": round(ss["oos_mdd_worst"] * 100, 2),
            "avg_daily_turnover_median": round(ss["oos_turn_med"], 4),
            "positive_fold_rate": round(ss["oos_pos_rate"], 3),
        },
        "score": ss["score"],
    }


def _init_worker(data, regime_symbol, n, fold_days, fold_count, min_valid_folds):
    _WORKER_CTX["data"] = data
    _WORKER_CTX["regime_symbol"] = regime_symbol
    _WORKER_CTX["n"] = n
    _WORKER_CTX["fold_days"] = fold_days
    _WORKER_CTX["fold_count"] = fold_count
    _WORKER_CTX["min_valid_folds"] = min_valid_folds


def _eval_worker(row):
    params = _build_params(row)
    return _evaluate_candidate(
        params,
        data=_WORKER_CTX["data"],
        regime_symbol=_WORKER_CTX["regime_symbol"],
        n=_WORKER_CTX["n"],
        fold_days=_WORKER_CTX["fold_days"],
        fold_count=_WORKER_CTX["fold_count"],
        min_valid_folds=_WORKER_CTX["min_valid_folds"],
    )


def _format_top(candidates, top):
    out = []
    for c in candidates[: max(1, top)]:
        out.append({
            "score": round(c["score"], 4),
            "params": c["params"],
            "insample": {
                "cagr_pct": round(c["insample"]["cagr"] * 100, 2),
                "max_drawdown_pct": round(c["insample"]["max_drawdown"] * 100, 2),
                "sharpe": round(c["insample"]["sharpe"], 3),
                "avg_daily_turnover": round(c["insample"]["avg_daily_turnover"], 4),
            },
            "oos": c["oos"],
        })
    return out


def _build_summary_rows(topn):
    rows = []
    for i, c in enumerate(topn, start=1):
        rows.append({
            "rank": i,
            "score": round(c["score"], 4),
            "lb_fast": c["params"]["lb_fast"],
            "lb_slow": c["params"]["lb_slow"],
            "sma_filter": c["params"]["sma_filter"],
            "k": c["params"]["k"],
            "atr_mult": c["params"]["atr_mult"],
            "target_vol": c["params"]["target_vol"],
            "rebalance_every": c["params"]["rebalance_every"],
            "risk_off_exposure": c["params"]["risk_off_exposure"],
            "oos_return_pct_median": c["oos"]["return_pct_median"],
            "oos_max_drawdown_pct_worst": c["oos"]["max_drawdown_pct_worst"],
            "oos_sharpe_median": c["oos"]["sharpe_median"],
            "oos_positive_fold_rate": c["oos"]["positive_fold_rate"],
            "insample_cagr_pct": round(c["insample"]["cagr"] * 100, 2),
            "insample_max_drawdown_pct": round(c["insample"]["max_drawdown"] * 100, 2),
            "insample_sharpe": round(c["insample"]["sharpe"], 3),
            "insample_avg_daily_turnover": round(c["insample"]["avg_daily_turnover"], 4),
        })
    return rows


def _save_results(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"optimize_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def _save_summary(skill_root, summary_rows):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"summary_{ts}.json"
    payload = {"generated_at": datetime.now().isoformat(), "rows": summary_rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", type=str, default="stable")
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--fold-days", type=int, default=180)
    p.add_argument("--fold-count", type=int, default=3)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--write-profile", type=str)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--chunksize", type=int, default=24)
    p.add_argument("--min-valid-folds", type=int, default=2)
    p.add_argument("--grid-mode", choices=["full", "quick"], default="full")
    p.add_argument("--quick-grid", action="store_true")
    p.add_argument("--no-save-results", action="store_true")
    args = p.parse_args()

    t0 = time.time()
    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent

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
    _, n, _, _, _, _ = align_ohlc(data)

    grid_mode = "quick" if args.quick_grid else args.grid_mode
    rows = list(_candidate_grid(mode=grid_mode))
    candidates = []

    jobs = max(1, args.jobs)
    if jobs == 1:
        for row in rows:
            params = _build_params(row)
            c = _evaluate_candidate(
                params,
                data,
                regime_symbol,
                n,
                args.fold_days,
                args.fold_count,
                max(1, args.min_valid_folds),
            )
            if c is not None:
                candidates.append(c)
    else:
        with mp.Pool(
            processes=jobs,
            initializer=_init_worker,
            initargs=(data, regime_symbol, n, args.fold_days, args.fold_count, max(1, args.min_valid_folds)),
        ) as pool:
            for c in pool.imap_unordered(_eval_worker, rows, chunksize=max(1, args.chunksize)):
                if c is not None:
                    candidates.append(c)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    topn = _format_top(candidates, args.top)
    summary_rows = _build_summary_rows(candidates[: max(1, args.top)])

    updated_profile = None
    if args.write_profile:
        if args.write_profile not in profiles:
            raise SystemExit(f"Cannot write unknown profile: {args.write_profile}")
        if not candidates:
            raise SystemExit("No valid candidates found; cannot write profile")
        profiles[args.write_profile] = candidates[0]["params"]
        save_profiles(skill_root, profiles)
        updated_profile = args.write_profile

    out = {
        "searched_candidates": len(candidates),
        "symbols": symbols,
        "regime_symbol": regime_symbol,
        "fold_days": args.fold_days,
        "fold_count": args.fold_count,
        "min_valid_folds": max(1, args.min_valid_folds),
        "grid_mode": grid_mode,
        "jobs": jobs,
        "best": topn[0] if topn else None,
        "top": topn,
        "summary": summary_rows,
        "updated_profile": updated_profile,
        "elapsed_sec": round(time.time() - t0, 2),
    }

    if not args.no_save_results:
        out["results_file"] = _save_results(skill_root, out)
        out["summary_file"] = _save_summary(skill_root, summary_rows)

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
