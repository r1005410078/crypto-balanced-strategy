#!/usr/bin/env python3
import argparse
import json
from datetime import datetime
from pathlib import Path

from engine import (
    DEFAULT_SYMBOLS,
    align_ohlc,
    backtest,
    load_data,
    load_profiles,
    resolve_regime_symbol,
)


def _summarize(metrics):
    return {
        "return_pct": round(metrics["return"] * 100, 2),
        "cagr_pct": round(metrics["cagr"] * 100, 2),
        "max_drawdown_pct": round(metrics["max_drawdown"] * 100, 2),
        "sharpe": round(metrics["sharpe"], 3),
        "avg_daily_turnover": round(metrics["avg_daily_turnover"], 4),
    }


def _alloc_to_capital(allocation, capital_cny):
    return {k: round(v * capital_cny, 2) for k, v in allocation.items()}


def _tranches_for_profile(profile):
    if profile == "stable":
        return [0.4, 0.3, 0.3]
    if profile == "stable_short_balanced":
        return [0.35, 0.35, 0.3]
    # shield
    return [0.5, 0.5]


def build_execution_checklist(
    active_profile,
    latest_alloc,
    capital_cny,
    switched,
    switch_reasons,
    risk_features,
    active_check_metrics,
):
    usdt_w = float(latest_alloc.get("USDT", 0.0))
    risk_exposure = max(0.0, 1.0 - usdt_w)
    capital_plan_cny = _alloc_to_capital(latest_alloc, capital_cny)

    mode = "hold_cash" if risk_exposure < 0.01 else "deploy"
    actions = []
    guardrails = []

    actions.append(
        {
            "step": 1,
            "type": "status",
            "instruction": (
                f"Active profile: {active_profile}"
                + (" (just switched)" if switched else "")
            ),
            "reasons": switch_reasons,
        }
    )

    if mode == "hold_cash":
        actions.append(
            {
                "step": 2,
                "type": "hold",
                "instruction": "No risk assets in latest allocation; keep capital in USDT/cash-equivalent.",
                "capital_plan_cny": {"USDT": capital_plan_cny.get("USDT", 0.0)},
            }
        )
        actions.append(
            {
                "step": 3,
                "type": "monitor",
                "instruction": (
                    "Re-run switcher daily. Deploy only when non-cash allocation appears "
                    "for active profile."
                ),
            }
        )
    else:
        risk_assets = [(k, v) for k, v in latest_alloc.items() if k != "USDT" and v > 0.0]
        risk_assets.sort(key=lambda x: x[1], reverse=True)
        risk_weight_sum = sum(v for _, v in risk_assets)
        tranches = _tranches_for_profile(active_profile)

        actions.append(
            {
                "step": 2,
                "type": "allocate",
                "instruction": "Use staged entries to reduce timing risk.",
                "risk_exposure_pct": round(risk_exposure * 100, 2),
                "tranches": tranches,
            }
        )

        step_id = 3
        for i, tr in enumerate(tranches, start=1):
            orders = []
            for asset, w in risk_assets:
                rel_w = w / risk_weight_sum if risk_weight_sum > 0 else 0.0
                amt = capital_cny * risk_exposure * tr * rel_w
                orders.append(
                    {
                        "asset": asset,
                        "amount_cny": round(amt, 2),
                        "weight_of_total_capital_pct": round(risk_exposure * tr * rel_w * 100, 2),
                    }
                )
            actions.append(
                {
                    "step": step_id,
                    "type": "entry_tranche",
                    "instruction": f"Execute tranche {i}/{len(tranches)}",
                    "orders": orders,
                }
            )
            step_id += 1

    guardrails.append(
        {
            "rule": "regime_risk",
            "instruction": (
                "If BTC close remains below SMA200 and 60-day drawdown worsens, force profile to stable_shield."
            ),
            "close": risk_features["close"],
            "sma200": risk_features["sma200"],
            "drawdown60_pct": risk_features["drawdown60_pct"],
        }
    )
    guardrails.append(
        {
            "rule": "risk_budget",
            "instruction": "If live drawdown exceeds strategy 120-day drawdown by 30%, cut risk exposure to 0.",
            "strategy_mdd120_pct": round(active_check_metrics["max_drawdown"] * 100, 2),
            "trigger_live_drawdown_pct": round(abs(active_check_metrics["max_drawdown"]) * 1.3 * 100, 2),
        }
    )

    return {
        "mode": mode,
        "risk_exposure_pct": round(risk_exposure * 100, 2),
        "capital_plan_cny": capital_plan_cny,
        "actions": actions,
        "guardrails": guardrails,
        "next_check_command": "python3 scripts/profile_switcher.py --capital-cny 10000 --confirmations 2 --check-window 120 --signal-window 365",
    }


def evaluate_market_risk(data, regime_symbol):
    _, _, closes, _, _, _ = align_ohlc(data)
    px = closes[regime_symbol]
    sma200 = sum(px[-200:]) / 200
    ret20 = px[-1] / px[-20] - 1
    drawdown_60 = px[-1] / max(px[-60:]) - 1

    # Use only price-based proxies; no news feed dependency.
    risk_rising = (px[-1] < sma200) and (ret20 < 0 or drawdown_60 < -0.12)
    return {
        "regime_symbol": regime_symbol,
        "close": round(px[-1], 4),
        "sma200": round(sma200, 4),
        "ret20_pct": round(ret20 * 100, 2),
        "drawdown60_pct": round(drawdown_60 * 100, 2),
        "risk_rising": bool(risk_rising),
    }


def decide_target_profile(
    stable_ret,
    short_ret,
    risk_rising,
    base_profile="stable",
    short_profile="stable_short_balanced",
    shield_profile="stable_shield",
    short_threshold=-0.03,
    shield_threshold=-0.015,
):
    target = base_profile
    reasons = []
    if stable_ret < short_threshold:
        target = short_profile
        reasons.append(
            f"{base_profile} return({stable_ret:.4f}) < short_threshold({short_threshold:.4f})"
        )
        if short_ret < shield_threshold and risk_rising:
            target = shield_profile
            reasons.append(
                f"{short_profile} return({short_ret:.4f}) < shield_threshold({shield_threshold:.4f}) "
                f"and risk_rising={risk_rising}"
            )
    if not reasons:
        reasons.append("base profile within threshold")
    return target, reasons


def apply_confirmation(active_profile, pending_target, pending_count, target_profile, confirmations):
    if confirmations <= 1:
        switched = target_profile != active_profile
        return target_profile, None, 0, switched

    if target_profile == active_profile:
        return active_profile, None, 0, False

    if pending_target == target_profile:
        pending_count += 1
    else:
        pending_target = target_profile
        pending_count = 1

    if pending_count >= confirmations:
        return target_profile, None, 0, True
    return active_profile, pending_target, pending_count, False


def _default_state(base_profile):
    return {
        "active_profile": base_profile,
        "pending_target": None,
        "pending_count": 0,
        "updated_at": None,
    }


def load_state(path, base_profile):
    if not path.exists():
        return _default_state(base_profile)
    payload = json.loads(path.read_text())
    st = _default_state(base_profile)
    st.update(payload)
    return st


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def _save_switch_result(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"switch_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-profile", type=str, default="stable")
    p.add_argument("--short-profile", type=str, default="stable_short_balanced")
    p.add_argument("--shield-profile", type=str, default="stable_shield")
    p.add_argument("--check-window", type=int, default=120)
    p.add_argument("--signal-window", type=int, default=365)
    p.add_argument("--short-threshold", type=float, default=-0.03)
    p.add_argument("--shield-threshold", type=float, default=-0.015)
    p.add_argument("--risk-mode", choices=["auto", "normal", "rising"], default="auto")
    p.add_argument("--confirmations", type=int, default=2)
    p.add_argument("--capital-cny", type=float, default=10000)
    p.add_argument("--state-file", type=str, default=None)
    p.add_argument("--no-save-state", action="store_true")
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

    for n in [args.base_profile, args.short_profile, args.shield_profile]:
        if n not in profiles:
            raise SystemExit(f"Unknown profile: {n}. Available: {', '.join(sorted(profiles.keys()))}")

    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=skill_root / "cache",
        ttl_hours=args.cache_ttl_hours,
    )
    regime_symbol = resolve_regime_symbol(symbols, args.regime_symbol)

    check_metrics = {}
    for name in [args.base_profile, args.short_profile, args.shield_profile]:
        check_metrics[name] = backtest(
            data,
            params=profiles[name],
            window_days=args.check_window,
            regime_symbol=regime_symbol,
        )

    risk_features = evaluate_market_risk(data, regime_symbol)
    if args.risk_mode == "rising":
        risk_rising = True
    elif args.risk_mode == "normal":
        risk_rising = False
    else:
        risk_rising = risk_features["risk_rising"]

    target_profile, reasons = decide_target_profile(
        stable_ret=check_metrics[args.base_profile]["return"],
        short_ret=check_metrics[args.short_profile]["return"],
        risk_rising=risk_rising,
        base_profile=args.base_profile,
        short_profile=args.short_profile,
        shield_profile=args.shield_profile,
        short_threshold=args.short_threshold,
        shield_threshold=args.shield_threshold,
    )

    state_path = (
        Path(args.state_file)
        if args.state_file
        else skill_root / "results" / "profile_switch_state.json"
    )
    prev_state = load_state(state_path, args.base_profile)
    active_profile, pending_target, pending_count, switched = apply_confirmation(
        active_profile=prev_state["active_profile"],
        pending_target=prev_state["pending_target"],
        pending_count=int(prev_state["pending_count"]),
        target_profile=target_profile,
        confirmations=max(1, args.confirmations),
    )

    next_state = {
        "active_profile": active_profile,
        "pending_target": pending_target,
        "pending_count": pending_count,
        "updated_at": datetime.now().isoformat(),
    }
    state_file = None
    if not args.no_save_state:
        state_file = save_state(state_path, next_state)

    signal_metrics = backtest(
        data,
        params=profiles[active_profile],
        window_days=args.signal_window,
        regime_symbol=regime_symbol,
    )
    final_cny = args.capital_cny * (1 + signal_metrics["return"])

    out = {
        "capital_cny": args.capital_cny,
        "base_profile": args.base_profile,
        "short_profile": args.short_profile,
        "shield_profile": args.shield_profile,
        "check_window": args.check_window,
        "signal_window": args.signal_window,
        "short_threshold": args.short_threshold,
        "shield_threshold": args.shield_threshold,
        "risk_mode": args.risk_mode,
        "risk_rising_used": risk_rising,
        "risk_features": risk_features,
        "target_profile": target_profile,
        "switch_reasons": reasons,
        "confirmations": max(1, args.confirmations),
        "switched": switched,
        "state_before": prev_state,
        "state_after": next_state,
        "state_file": state_file,
        "check_metrics": {
            k: _summarize(v) for k, v in check_metrics.items()
        },
        "active_profile": active_profile,
        "active_signal": {
            **_summarize(signal_metrics),
            "final_cny": round(final_cny, 2),
            "profit_cny": round(final_cny - args.capital_cny, 2),
            "latest_alloc": signal_metrics["latest_alloc"],
            "regime_symbol_used": signal_metrics["regime_symbol"],
            "params_used": profiles[active_profile],
        },
    }
    out["execution_checklist"] = build_execution_checklist(
        active_profile=active_profile,
        latest_alloc=signal_metrics["latest_alloc"],
        capital_cny=args.capital_cny,
        switched=switched,
        switch_reasons=reasons,
        risk_features=risk_features,
        active_check_metrics=check_metrics[active_profile],
    )

    if not args.no_save_results:
        out["results_file"] = _save_switch_result(skill_root, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
