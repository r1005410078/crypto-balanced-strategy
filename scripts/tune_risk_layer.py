#!/usr/bin/env python3
import argparse
import json
from datetime import datetime
from itertools import product
from pathlib import Path

from engine import (
    DEFAULT_SYMBOLS,
    backtest,
    load_data,
    load_profiles,
    resolve_regime_symbol,
    save_profiles,
)


DEFAULT_ROLE_SPECS = {
    "stable": {
        "min_ret120": -0.05,
        "min_ret365": 0.25,
        "max_mdd365": 0.18,
        "max_turn365": 0.045,
        "min_sharpe365": 1.20,
        "target_vol_min": 0.20,
        "target_vol_max": 0.40,
        "risk_off_min": 0.05,
        "risk_off_max": 0.30,
        "rebalance_min": 1,
        "rebalance_max": 3,
        "regime_sma_min": 160,
        "regime_sma_max": 220,
        "anchor": {"target_vol": 0.24, "risk_off_exposure": 0.20, "rebalance_every": 1, "regime_sma": 200},
        "anchor_strength": 0.15,
    },
    "stable_short_balanced": {
        "min_ret120": -0.04,
        "min_ret365": 0.20,
        "max_mdd365": 0.15,
        "max_turn365": 0.035,
        "min_sharpe365": 1.30,
        "target_vol_min": 0.16,
        "target_vol_max": 0.30,
        "risk_off_min": 0.10,
        "risk_off_max": 0.40,
        "rebalance_min": 1,
        "rebalance_max": 3,
        "regime_sma_min": 180,
        "regime_sma_max": 240,
        "anchor": {"target_vol": 0.20, "risk_off_exposure": 0.20, "rebalance_every": 1, "regime_sma": 220},
        "anchor_strength": 0.60,
    },
    "stable_shield": {
        "min_ret120": -0.025,
        "min_ret365": 0.10,
        "max_mdd365": 0.10,
        "max_turn365": 0.020,
        "min_sharpe365": 1.00,
        "target_vol_min": 0.10,
        "target_vol_max": 0.24,
        "risk_off_min": 0.05,
        "risk_off_max": 0.20,
        "rebalance_min": 2,
        "rebalance_max": 7,
        "regime_sma_min": 180,
        "regime_sma_max": 260,
        "anchor": {"target_vol": 0.12, "risk_off_exposure": 0.08, "rebalance_every": 7, "regime_sma": 240},
        "anchor_strength": 0.90,
    },
}


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
        raise ValueError("Empty int list")
    return vals


def _parse_float_list(text):
    vals = []
    for x in text.split(","):
        x = x.strip()
        if not x:
            continue
        vals.append(float(x))
    if not vals:
        raise ValueError("Empty float list")
    return vals


def _summarize(metrics):
    return {
        "return_pct": round(metrics["return"] * 100, 2),
        "cagr_pct": round(metrics["cagr"] * 100, 2),
        "max_drawdown_pct": round(metrics["max_drawdown"] * 100, 2),
        "sharpe": round(metrics["sharpe"], 3),
        "avg_daily_turnover": round(metrics["avg_daily_turnover"], 4),
    }


def _constraint_penalty(metrics_by_window, spec):
    m120 = metrics_by_window[120]
    m365 = metrics_by_window[365]
    penalty = 0.0

    if m120["return"] < spec["min_ret120"]:
        penalty += (spec["min_ret120"] - m120["return"]) * 2.0
    if m365["return"] < spec["min_ret365"]:
        penalty += (spec["min_ret365"] - m365["return"]) * 2.5
    if abs(m365["max_drawdown"]) > spec["max_mdd365"]:
        penalty += (abs(m365["max_drawdown"]) - spec["max_mdd365"]) * 2.0
    if m365["avg_daily_turnover"] > spec["max_turn365"]:
        penalty += (m365["avg_daily_turnover"] - spec["max_turn365"]) * 1.8
    if m365["sharpe"] < spec["min_sharpe365"]:
        penalty += (spec["min_sharpe365"] - m365["sharpe"]) * 0.6
    return penalty


def _score_candidate(metrics_by_window, spec):
    m120 = metrics_by_window[120]
    m180 = metrics_by_window.get(180, m120)
    m365 = metrics_by_window[365]
    m730 = metrics_by_window.get(730, m365)

    # Risk-adjusted blend with short-window awareness.
    score = (
        0.40 * _clip(m365["return"], -0.20, 0.60)
        + 0.20 * _clip(m365["sharpe"], -1.5, 2.5)
        + 0.15 * _clip(m120["return"], -0.20, 0.30)
        + 0.10 * _clip(m180["return"], -0.20, 0.40)
        + 0.10 * _clip(m730["return"], -0.40, 1.00)
        + 0.05 * _clip(m730["sharpe"], -1.5, 2.0)
    )

    # Soft penalties to favor smoother profiles.
    score -= max(0.0, abs(m365["max_drawdown"]) - spec["max_mdd365"]) * 1.5
    score -= max(0.0, m365["avg_daily_turnover"] - spec["max_turn365"]) * 1.0
    score -= max(0.0, abs(m120["max_drawdown"]) - 0.07) * 0.8
    return score


def _role_spec(name):
    return DEFAULT_ROLE_SPECS.get(name, DEFAULT_ROLE_SPECS["stable"])


def _within_role_bounds(role_spec, target_vol, risk_off, rebalance_every, regime_sma):
    return (
        role_spec["target_vol_min"] <= target_vol <= role_spec["target_vol_max"]
        and role_spec["risk_off_min"] <= risk_off <= role_spec["risk_off_max"]
        and role_spec["rebalance_min"] <= rebalance_every <= role_spec["rebalance_max"]
        and role_spec["regime_sma_min"] <= regime_sma <= role_spec["regime_sma_max"]
    )


def _role_anchor_penalty(role_spec, target_vol, risk_off, rebalance_every, regime_sma):
    anchor = role_spec.get("anchor", {})
    strength = float(role_spec.get("anchor_strength", 0.0))
    if strength <= 0.0 or not anchor:
        return 0.0

    dist = 0.0
    dist += abs(target_vol - float(anchor["target_vol"])) / 0.12
    dist += abs(risk_off - float(anchor["risk_off_exposure"])) / 0.30
    dist += abs(rebalance_every - int(anchor["rebalance_every"])) / 6.0
    dist += abs(regime_sma - int(anchor["regime_sma"])) / 100.0
    return dist * 0.08 * strength


def _evaluate_one_profile(
    name,
    base_params,
    role_spec,
    data,
    windows,
    regime_symbol,
    target_vols,
    risk_offs,
    rebalances,
    regime_smas,
    top=5,
):
    candidates = []
    for target_vol, risk_off, rebalance_every, regime_sma in product(
        target_vols, risk_offs, rebalances, regime_smas
    ):
        if not _within_role_bounds(
            role_spec,
            float(target_vol),
            float(risk_off),
            int(rebalance_every),
            int(regime_sma),
        ):
            continue

        params = dict(base_params)
        params["target_vol"] = float(target_vol)
        params["risk_off_exposure"] = float(risk_off)
        params["rebalance_every"] = int(rebalance_every)
        params["regime_sma"] = int(regime_sma)

        metrics = {}
        for w in windows:
            metrics[w] = backtest(
                data,
                params=params,
                window_days=w,
                regime_symbol=regime_symbol,
            )

        penalty = _constraint_penalty(metrics, role_spec)
        role_penalty = _role_anchor_penalty(
            role_spec,
            float(target_vol),
            float(risk_off),
            int(rebalance_every),
            int(regime_sma),
        )
        score = _score_candidate(metrics, role_spec) - penalty - role_penalty
        feasible = penalty <= 1e-12
        candidates.append(
            {
                "params": params,
                "score": score,
                "penalty": penalty,
                "role_penalty": role_penalty,
                "feasible": feasible,
                "metrics": metrics,
            }
        )

    # Prefer feasible, then score.
    candidates.sort(key=lambda x: (x["feasible"], x["score"]), reverse=True)
    best = candidates[0] if candidates else None
    topn = candidates[: max(1, top)]
    feasible_count = sum(1 for x in candidates if x["feasible"])

    def _format_row(x):
        return {
            "score": round(x["score"], 4),
            "penalty": round(x["penalty"], 4),
            "role_penalty": round(x["role_penalty"], 4),
            "feasible": x["feasible"],
            "params": {
                "target_vol": x["params"]["target_vol"],
                "risk_off_exposure": x["params"]["risk_off_exposure"],
                "rebalance_every": x["params"]["rebalance_every"],
                "regime_sma": x["params"]["regime_sma"],
            },
            "metrics": {str(w): _summarize(x["metrics"][w]) for w in windows},
        }

    return {
        "profile": name,
        "searched": len(candidates),
        "feasible_count": feasible_count,
        "role_spec": role_spec,
        "best": _format_row(best) if best else None,
        "top": [_format_row(x) for x in topn],
    }


def _save_tune_result(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"risk_tune_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--profiles",
        type=str,
        default="stable,stable_short_balanced,stable_shield",
    )
    p.add_argument("--windows", type=str, default="120,180,365,730")
    p.add_argument("--target-vols", type=str, default="0.12,0.16,0.20,0.24,0.28,0.35")
    p.add_argument("--risk-offs", type=str, default="0.05,0.10,0.20,0.30,0.40,0.60")
    p.add_argument("--rebalances", type=str, default="1,2,3,7")
    p.add_argument("--regime-smas", type=str, default="160,180,200,220,240,260")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--write-profiles", action="store_true")
    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    profiles = load_profiles(skill_root)

    profile_names = [x.strip() for x in args.profiles.split(",") if x.strip()]
    for n in profile_names:
        if n not in profiles:
            raise SystemExit(f"Unknown profile: {n}. Available: {', '.join(sorted(profiles.keys()))}")

    windows = _parse_int_list(args.windows)
    if 120 not in windows or 365 not in windows:
        raise SystemExit("windows must include 120 and 365 for risk-layer constraints")

    target_vols = _parse_float_list(args.target_vols)
    risk_offs = _parse_float_list(args.risk_offs)
    rebalances = _parse_int_list(args.rebalances)
    regime_smas = _parse_int_list(args.regime_smas)

    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=skill_root / "cache",
        ttl_hours=args.cache_ttl_hours,
    )
    regime_symbol = resolve_regime_symbol(symbols, args.regime_symbol)

    results = []
    for n in profile_names:
        results.append(
            _evaluate_one_profile(
                name=n,
                base_params=profiles[n],
                role_spec=_role_spec(n),
                data=data,
                windows=windows,
                regime_symbol=regime_symbol,
                target_vols=target_vols,
                risk_offs=risk_offs,
                rebalances=rebalances,
                regime_smas=regime_smas,
                top=args.top,
            )
        )

    updated_profiles = []
    profiles_file = None
    if args.write_profiles:
        for r in results:
            b = r["best"]
            if not b:
                continue
            n = r["profile"]
            profiles[n]["target_vol"] = b["params"]["target_vol"]
            profiles[n]["risk_off_exposure"] = b["params"]["risk_off_exposure"]
            profiles[n]["rebalance_every"] = b["params"]["rebalance_every"]
            profiles[n]["regime_sma"] = b["params"]["regime_sma"]
            updated_profiles.append(n)
        save_profiles(skill_root, profiles)
        profiles_file = str(skill_root / "profiles.json")

    out = {
        "generated_at": datetime.now().isoformat(),
        "profiles": profile_names,
        "windows": windows,
        "search_space": {
            "target_vols": target_vols,
            "risk_offs": risk_offs,
            "rebalances": rebalances,
            "regime_smas": regime_smas,
        },
        "regime_symbol": regime_symbol,
        "updated_profiles": updated_profiles,
        "profiles_file": profiles_file,
        "results": results,
    }
    if not args.no_save_results:
        out["results_file"] = _save_tune_result(skill_root, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
