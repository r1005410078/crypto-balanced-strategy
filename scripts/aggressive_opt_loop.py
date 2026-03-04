#!/usr/bin/env python3
import argparse
import json
import multiprocessing as mp
import random
import time
from datetime import datetime
from pathlib import Path

from engine import DEFAULT_SYMBOLS, backtest, load_data, load_profiles, resolve_regime_symbol, save_profiles


_CTX = {}


GRID = {
    "lb_fast": [10, 15, 20, 30, 40],
    "lb_slow": [30, 40, 50, 60, 80, 100],
    "sma_filter": [60, 80, 100, 120, 160, 200],
    "k": [1, 2, 3],
    "atr_mult": [1.8, 2.2, 2.6, 2.8, 3.2, 3.8],
    "max_w_core": [0.55, 0.6, 0.7, 0.8],
    "max_w_alt": [0.25, 0.3, 0.35, 0.4, 0.5],
    "vol_lb": [20],
    "target_vol": [0.25, 0.28, 0.35, 0.45, 0.55, 0.65],
    "rebalance_every": [1, 2, 3, 5, 7],
    "regime_sma": [80, 120, 160, 200, 240],
    "risk_off_exposure": [0.05, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0],
    "atr_period": [14],
    "fee": [0.001],
    "slip": [0.0005],
}


PARAM_ORDER = [
    "lb_fast",
    "lb_slow",
    "sma_filter",
    "k",
    "atr_mult",
    "max_w_core",
    "max_w_alt",
    "vol_lb",
    "target_vol",
    "rebalance_every",
    "regime_sma",
    "risk_off_exposure",
    "atr_period",
    "fee",
    "slip",
]


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _parse_int_list(text):
    out = []
    for x in str(text).split(","):
        x = x.strip()
        if not x:
            continue
        out.append(int(x))
    if not out:
        raise ValueError("empty list")
    return out


def _is_valid(params):
    if int(params["lb_slow"]) <= int(params["lb_fast"]):
        return False
    if float(params["max_w_alt"]) > float(params["max_w_core"]):
        return False
    return True


def _random_candidate(rng):
    p = {}
    for k in PARAM_ORDER:
        p[k] = rng.choice(GRID[k])
    return p


def _mutate_candidate(base, rng):
    p = dict(base)
    n_mut = rng.randint(2, 5)
    keys = rng.sample(PARAM_ORDER, n_mut)
    for k in keys:
        vals = GRID[k]
        cur = p.get(k, vals[0])
        if cur not in vals:
            p[k] = rng.choice(vals)
            continue
        idx = vals.index(cur)
        lo = max(0, idx - 1)
        hi = min(len(vals) - 1, idx + 1)
        p[k] = vals[rng.randint(lo, hi)]
    return p


def _score_metrics(metrics):
    m60 = metrics[60]
    m120 = metrics[120]
    m180 = metrics.get(180, m120)
    m365 = metrics.get(365, m180)
    m730 = metrics.get(730, m365)

    score = (
        0.28 * _clip(m60["return"], -0.25, 0.30)
        + 0.25 * _clip(m120["return"], -0.30, 0.40)
        + 0.10 * _clip(m180["return"], -0.30, 0.50)
        + 0.20 * _clip(m365["return"], -0.40, 0.80)
        + 0.10 * _clip(m120["sharpe"], -2.0, 3.0)
        + 0.07 * _clip(m365["sharpe"], -2.0, 3.0)
    )
    score -= max(0.0, abs(m120["max_drawdown"]) - 0.18) * 1.8
    score -= max(0.0, abs(m365["max_drawdown"]) - 0.28) * 1.2
    score -= max(0.0, m120["avg_daily_turnover"] - 0.18) * 0.8
    score -= max(0.0, m365["avg_daily_turnover"] - 0.18) * 0.6
    # Extra long-window penalty to avoid selecting short-term-only candidates.
    score -= max(0.0, -m730["return"]) * 0.7
    score -= max(0.0, abs(m730["max_drawdown"]) - 0.35) * 1.6
    return score


def _is_satisfied(metrics):
    m60 = metrics[60]
    m120 = metrics[120]
    m365 = metrics.get(365, m120)
    return (
        m60["return"] >= 0.02
        and m120["return"] >= 0.06
        and m120["sharpe"] >= 0.35
        and m120["max_drawdown"] >= -0.15
        and m365["return"] >= 0.18
        and m365["max_drawdown"] >= -0.28
    )


def _hard_pass(
    metrics,
    min_return_120=None,
    min_return_180=None,
    min_return_365=None,
    min_return_730=None,
    max_drawdown_730_abs=None,
):
    m120 = metrics.get(120)
    m180 = metrics.get(180)
    m365 = metrics.get(365)
    m730 = metrics.get(730)
    if min_return_120 is not None:
        if m120 is None or m120["return"] < float(min_return_120):
            return False
    if min_return_180 is not None:
        if m180 is None or m180["return"] < float(min_return_180):
            return False
    if min_return_365 is not None:
        if m365 is None or m365["return"] < float(min_return_365):
            return False
    if min_return_730 is not None:
        if m730 is None or m730["return"] < float(min_return_730):
            return False
    if max_drawdown_730_abs is not None:
        if m730 is None or abs(m730["max_drawdown"]) > float(max_drawdown_730_abs):
            return False
    return True


def _eval_candidate(
    params,
    windows,
    data,
    regime_symbol,
    min_return_120=None,
    min_return_180=None,
    min_return_365=None,
    min_return_730=None,
    max_drawdown_730_abs=None,
):
    if not _is_valid(params):
        return None
    metrics = {}
    for w in windows:
        metrics[w] = backtest(data, params=params, window_days=w, regime_symbol=regime_symbol)
    if not _hard_pass(
        metrics,
        min_return_120=min_return_120,
        min_return_180=min_return_180,
        min_return_365=min_return_365,
        min_return_730=min_return_730,
        max_drawdown_730_abs=max_drawdown_730_abs,
    ):
        return None
    score = _score_metrics(metrics)
    return {
        "params": params,
        "score": score,
        "metrics": metrics,
        "satisfied": _is_satisfied(metrics),
    }


def _init_worker(
    data,
    regime_symbol,
    windows,
    min_return_120,
    min_return_180,
    min_return_365,
    min_return_730,
    max_drawdown_730_abs,
):
    _CTX["data"] = data
    _CTX["regime_symbol"] = regime_symbol
    _CTX["windows"] = list(windows)
    _CTX["min_return_120"] = min_return_120
    _CTX["min_return_180"] = min_return_180
    _CTX["min_return_365"] = min_return_365
    _CTX["min_return_730"] = min_return_730
    _CTX["max_drawdown_730_abs"] = max_drawdown_730_abs


def _worker_eval(params):
    return _eval_candidate(
        params,
        windows=_CTX["windows"],
        data=_CTX["data"],
        regime_symbol=_CTX["regime_symbol"],
        min_return_120=_CTX.get("min_return_120"),
        min_return_180=_CTX.get("min_return_180"),
        min_return_365=_CTX.get("min_return_365"),
        min_return_730=_CTX.get("min_return_730"),
        max_drawdown_730_abs=_CTX.get("max_drawdown_730_abs"),
    )


def _fmt_candidate(row):
    out_metrics = {}
    for w, m in row["metrics"].items():
        out_metrics[str(w)] = {
            "return_pct": round(m["return"] * 100, 2),
            "cagr_pct": round(m["cagr"] * 100, 2),
            "max_drawdown_pct": round(m["max_drawdown"] * 100, 2),
            "sharpe": round(m["sharpe"], 3),
            "avg_daily_turnover": round(m["avg_daily_turnover"], 4),
        }
    return {
        "score": round(row["score"], 4),
        "satisfied": bool(row["satisfied"]),
        "params": row["params"],
        "metrics": out_metrics,
    }


def _save_result(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = results_dir / f"aggressive_loop_opt_{ts}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def main():
    p = argparse.ArgumentParser(description="Iterative loop optimization for aggressive profile.")
    p.add_argument("--profile", type=str, default="aggressive")
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--windows", type=str, default="60,120,180,365,730")
    p.add_argument("--rounds", type=int, default=8)
    p.add_argument("--candidates-per-round", type=int, default=600)
    p.add_argument("--local-ratio", type=float, default=0.6, help="Share of candidates mutated from current best.")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--write-profile", action="store_true")
    p.add_argument("--stop-on-satisfied", action="store_true", default=True)
    p.add_argument("--no-stop-on-satisfied", dest="stop_on_satisfied", action="store_false")
    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--hard-min-return-120", type=float, default=None)
    p.add_argument("--hard-min-return-180", type=float, default=None)
    p.add_argument("--hard-min-return-365", type=float, default=None)
    p.add_argument("--hard-min-return-730", type=float, default=None)
    p.add_argument("--hard-max-drawdown-730-abs", type=float, default=None)
    args = p.parse_args()

    t0 = time.time()
    rng = random.Random(args.seed)
    windows = _parse_int_list(args.windows)
    if 60 not in windows or 120 not in windows:
        raise SystemExit("windows must include 60 and 120")

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    profiles = load_profiles(skill_root)
    if args.profile not in profiles:
        raise SystemExit(f"Unknown profile: {args.profile}")

    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=skill_root / "cache",
        ttl_hours=args.cache_ttl_hours,
    )
    regime_symbol = resolve_regime_symbol(symbols, args.regime_symbol)

    baseline_params = dict(profiles[args.profile])
    baseline = _eval_candidate(
        baseline_params,
        windows=windows,
        data=data,
        regime_symbol=regime_symbol,
        min_return_120=args.hard_min_return_120,
        min_return_180=args.hard_min_return_180,
        min_return_365=args.hard_min_return_365,
        min_return_730=args.hard_min_return_730,
        max_drawdown_730_abs=args.hard_max_drawdown_730_abs,
    )
    best = baseline
    rounds = []

    for rd in range(1, max(1, int(args.rounds)) + 1):
        n_total = max(20, int(args.candidates_per_round))
        n_local = int(n_total * _clip(float(args.local_ratio), 0.0, 1.0))
        n_random = n_total - n_local

        anchor_params = dict(best["params"]) if best is not None else dict(baseline_params)
        cand = []
        # Keep current best in pool explicitly.
        cand.append(dict(anchor_params))
        while len(cand) < (1 + n_local):
            c = _mutate_candidate(anchor_params, rng)
            if _is_valid(c):
                cand.append(c)
        while len(cand) < n_total:
            c = _random_candidate(rng)
            if _is_valid(c):
                cand.append(c)

        rows = []
        jobs = max(1, int(args.jobs))
        if jobs == 1:
            for c in cand:
                r = _eval_candidate(
                    c,
                    windows=windows,
                    data=data,
                    regime_symbol=regime_symbol,
                    min_return_120=args.hard_min_return_120,
                    min_return_180=args.hard_min_return_180,
                    min_return_365=args.hard_min_return_365,
                    min_return_730=args.hard_min_return_730,
                    max_drawdown_730_abs=args.hard_max_drawdown_730_abs,
                )
                if r is not None:
                    rows.append(r)
        else:
            with mp.Pool(
                processes=jobs,
                initializer=_init_worker,
                initargs=(
                    data,
                    regime_symbol,
                    windows,
                    args.hard_min_return_120,
                    args.hard_min_return_180,
                    args.hard_min_return_365,
                    args.hard_min_return_730,
                    args.hard_max_drawdown_730_abs,
                ),
            ) as pool:
                for r in pool.imap_unordered(_worker_eval, cand, chunksize=32):
                    if r is not None:
                        rows.append(r)

        rows.sort(key=lambda x: x["score"], reverse=True)
        round_best = rows[0] if rows else best
        if best is None:
            improved = bool(round_best is not None)
        else:
            improved = bool(round_best is not None and round_best["score"] > best["score"] + 1e-9)
        if improved:
            best = round_best

        rounds.append(
            {
                "round": rd,
                "candidate_count": len(rows),
                "improved": bool(improved),
                "best_score": round(best["score"], 6) if best else None,
                "round_best_score": round(round_best["score"], 6) if round_best else None,
                "best_satisfied": bool(best["satisfied"]) if best else False,
                "top3": [_fmt_candidate(x) for x in rows[:3]],
            }
        )
        if args.stop_on_satisfied and best is not None and best["satisfied"]:
            break

    updated_profile = None
    if args.write_profile and best is not None:
        profiles[args.profile] = dict(best["params"])
        save_profiles(skill_root, profiles)
        updated_profile = args.profile

    out = {
        "generated_at": datetime.now().isoformat(),
        "profile": args.profile,
        "symbols": symbols,
        "regime_symbol": regime_symbol,
        "windows": windows,
        "rounds_requested": int(args.rounds),
        "rounds_executed": len(rounds),
        "candidates_per_round": int(args.candidates_per_round),
        "hard_constraints": {
            "min_return_120": args.hard_min_return_120,
            "min_return_180": args.hard_min_return_180,
            "min_return_365": args.hard_min_return_365,
            "min_return_730": args.hard_min_return_730,
            "max_drawdown_730_abs": args.hard_max_drawdown_730_abs,
        },
        "baseline": _fmt_candidate(baseline) if baseline else None,
        "best": _fmt_candidate(best) if best else None,
        "satisfied": bool(best["satisfied"]) if best else False,
        "updated_profile": updated_profile,
        "elapsed_sec": round(time.time() - t0, 2),
        "round_details": rounds,
    }
    if not args.no_save_results:
        out["results_file"] = _save_result(skill_root, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
