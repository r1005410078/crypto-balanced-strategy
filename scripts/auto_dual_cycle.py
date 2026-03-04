#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from auto_cycle import (
    _build_market_snapshot,
    _compute_equity_usdt,
    _cycle_status,
    _execute_orders,
    _load_env_key,
    _transfer_funding_if_needed,
)
from auto_state import (
    compute_cycle_fingerprint,
    day_pnl_snapshot,
    ensure_day_start_equity,
    file_lock,
    load_state,
    record_cycle,
    save_state,
    should_skip_cycle,
)
from auto_tier_cycle import (
    TIER_PRESETS,
    _derive_flags,
    _is_network_related_error,
    _load_tier_state,
    _save_tier_state,
    _wait_for_network_recovery,
    decide_tier,
)
from engine import DEFAULT_SYMBOLS, backtest, load_data, load_profiles
from notifier import notify_all
from okx_auto_executor import OkxClient, _safe_float, build_rebalance_plan, normalize_target_alloc
from risk_guard import evaluate_trade_guards, summarize_execution


def _next_dual_result_path(skill_root):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return results_dir / f"auto_dual_cycle_{ts}.json"


def _save_dual_result(skill_root, payload):
    p = Path(payload.get("results_file") or _next_dual_result_path(skill_root))
    payload["results_file"] = str(p)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def _save_switch_overlay(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = results_dir / f"switch_dual_{ts}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def _build_switch_cmd(args, switcher_path):
    cmd = [
        sys.executable,
        str(switcher_path),
        "--capital-cny",
        str(args.capital_cny),
        "--confirmations",
        str(args.confirmations),
        "--check-window",
        str(args.check_window),
        "--signal-window",
        str(args.signal_window),
        "--short-threshold",
        str(args.short_threshold),
        "--shield-threshold",
        str(args.shield_threshold),
        "--risk-mode",
        args.risk_mode,
        "--base-profile",
        args.base_profile,
        "--short-profile",
        args.short_profile,
        "--shield-profile",
        args.shield_profile,
        "--symbols",
        args.symbols,
        "--regime-symbol",
        args.regime_symbol,
        "--limit",
        str(args.limit),
        "--cache-ttl-hours",
        str(args.cache_ttl_hours),
    ]
    if args.no_cache:
        cmd.append("--no-cache")
    if args.switch_state_file:
        cmd.extend(["--state-file", args.switch_state_file])
    if args.no_save_switch_state:
        cmd.append("--no-save-state")
    if args.no_save_switch_results:
        cmd.append("--no-save-results")
    return cmd


def _load_or_run_switch_payload(args, script_dir, skill_root):
    if args.switch_file:
        p = Path(args.switch_file)
        return json.loads(p.read_text()), f"file:{p}", str(p)

    switcher = script_dir / "profile_switcher.py"
    cmd = _build_switch_cmd(args, switcher)
    payload = json.loads(subprocess.check_output(cmd, text=True, cwd=str(skill_root)))
    switch_file = payload.get("results_file")
    if not switch_file:
        switch_file = _save_switch_overlay(skill_root, payload)
    return payload, " ".join(cmd), str(switch_file)


def _compute_aggressive_signal(args, *, skill_root):
    profiles = load_profiles(skill_root)
    if args.aggressive_profile not in profiles:
        raise SystemExit(f"Unknown aggressive profile: {args.aggressive_profile}")

    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    data = load_data(
        symbols,
        limit=args.limit,
        use_cache=not args.no_cache,
        cache_dir=Path(skill_root) / "cache",
        ttl_hours=args.cache_ttl_hours,
    )
    params = dict(profiles[args.aggressive_profile])
    res = backtest(
        data,
        params=params,
        window_days=args.aggressive_signal_window_days,
        regime_symbol=args.regime_symbol,
    )
    alloc = normalize_target_alloc(res.get("latest_alloc", {}))
    return {
        "profile": args.aggressive_profile,
        "window_days": int(args.aggressive_signal_window_days),
        "alloc": alloc,
        "metrics": {
            "return_pct": round(res["return"] * 100, 2),
            "cagr_pct": round(res["cagr"] * 100, 2),
            "max_drawdown_pct": round(res["max_drawdown"] * 100, 2),
            "sharpe": round(res["sharpe"], 3),
            "avg_daily_turnover": round(res["avg_daily_turnover"], 4),
        },
        "params_used": params,
    }


def resolve_budget_split(equity_usdt, primary_budget_usdt, aggressive_budget_usdt):
    eq = max(0.0, float(equity_usdt or 0.0))
    ag_req = max(0.0, float(aggressive_budget_usdt or 0.0))
    if primary_budget_usdt is None:
        pr_req = max(0.0, eq - ag_req)
        primary_mode = "auto_remaining"
    else:
        pr_req = max(0.0, float(primary_budget_usdt))
        primary_mode = "fixed"

    if eq <= 0:
        return {
            "equity_usdt": 0.0,
            "primary_mode": primary_mode,
            "requested_primary_budget_usdt": round(pr_req, 8),
            "requested_aggressive_budget_usdt": round(ag_req, 8),
            "primary_budget_usdt": 0.0,
            "aggressive_budget_usdt": 0.0,
            "scale": 0.0,
        }

    total_req = pr_req + ag_req
    scale = 1.0
    if total_req > eq and total_req > 0:
        scale = eq / total_req
    pr = pr_req * scale
    ag = ag_req * scale
    return {
        "equity_usdt": round(eq, 8),
        "primary_mode": primary_mode,
        "requested_primary_budget_usdt": round(pr_req, 8),
        "requested_aggressive_budget_usdt": round(ag_req, 8),
        "primary_budget_usdt": round(pr, 8),
        "aggressive_budget_usdt": round(ag, 8),
        "scale": round(scale, 8),
    }


def resolve_budget_split_by_ratio(equity_usdt, aggressive_ratio, primary_ratio=None):
    eq = max(0.0, float(equity_usdt or 0.0))
    ag_r = max(0.0, min(1.0, float(aggressive_ratio or 0.0)))
    if primary_ratio is None:
        pr_r = max(0.0, 1.0 - ag_r)
        primary_mode = "ratio_auto_remaining"
    else:
        pr_r = max(0.0, min(1.0, float(primary_ratio)))
        primary_mode = "ratio_fixed"

    req_sum = pr_r + ag_r
    scale = 1.0
    if req_sum > 1.0 and req_sum > 0:
        scale = 1.0 / req_sum
    pr_r_eff = pr_r * scale
    ag_r_eff = ag_r * scale
    pr = eq * pr_r_eff
    ag = eq * ag_r_eff
    return {
        "equity_usdt": round(eq, 8),
        "primary_mode": primary_mode,
        "requested_primary_ratio": round(pr_r, 8),
        "requested_aggressive_ratio": round(ag_r, 8),
        "primary_ratio": round(pr_r_eff, 8),
        "aggressive_ratio": round(ag_r_eff, 8),
        "requested_primary_budget_usdt": round(eq * pr_r, 8),
        "requested_aggressive_budget_usdt": round(eq * ag_r, 8),
        "primary_budget_usdt": round(pr, 8),
        "aggressive_budget_usdt": round(ag, 8),
        "scale": round(scale, 8),
    }


def blend_targets(primary_signal_alloc, aggressive_signal_alloc, budget_split):
    eq = max(0.0, float(budget_split.get("equity_usdt", 0.0)))
    pr = max(0.0, float(budget_split.get("primary_budget_usdt", 0.0)))
    ag = max(0.0, float(budget_split.get("aggressive_budget_usdt", 0.0)))
    if eq <= 0:
        return {"USDT": 1.0}

    values = {}
    for alloc, budget in ((primary_signal_alloc, pr), (aggressive_signal_alloc, ag)):
        for sym, w in (alloc or {}).items():
            s = str(sym).upper()
            if s == "USDT":
                continue
            ww = max(0.0, _safe_float(w, 0.0))
            if ww <= 0:
                continue
            values[s] = values.get(s, 0.0) + budget * ww

    out = {}
    risk_sum = 0.0
    for sym, usdt_value in values.items():
        w = max(0.0, usdt_value / eq)
        if w <= 0:
            continue
        out[sym] = w
        risk_sum += w

    if risk_sum > 1.0 and risk_sum > 0:
        for sym in list(out.keys()):
            out[sym] = out[sym] / risk_sum
        risk_sum = 1.0

    out["USDT"] = max(0.0, 1.0 - risk_sum)
    total = sum(out.values())
    if total > 0:
        for k in list(out.keys()):
            out[k] = out[k] / total
    return out


def resolve_dual_budget(args, equity_usdt):
    if args.aggressive_ratio is not None:
        return resolve_budget_split_by_ratio(
            equity_usdt=equity_usdt,
            aggressive_ratio=args.aggressive_ratio,
            primary_ratio=args.primary_ratio,
        )
    return resolve_budget_split(
        equity_usdt=equity_usdt,
        primary_budget_usdt=args.primary_budget_usdt,
        aggressive_budget_usdt=args.aggressive_budget_usdt,
    )


def should_notify(out):
    """
    Notify only on:
    - at least one submitted live order (trade success), or
    - actionable failures that likely require manual intervention.

    Do not notify for normal no-trade cycles (noop/skipped/blocked without orders).
    """
    status = str(out.get("cycle_status", "")).lower()
    counts = out.get("execution_counts") or {}
    submitted = int(counts.get("SUBMITTED", 0))
    failed = int(counts.get("FAILED", 0))
    blocked = int(counts.get("BLOCKED", 0))
    orders = (out.get("plan") or {}).get("orders") or []
    price_errors = out.get("price_errors") or []

    if submitted > 0:
        return True
    if failed > 0:
        return True
    if status in {"failed", "partial"}:
        return True
    if blocked > 0 and (len(orders) > 0 or len(price_errors) > 0):
        return True
    return False


def _build_cycle_fingerprint_payload(
    *,
    switch_payload,
    aggressive_profile,
    budget_split,
    merged_target,
    selected_tier,
):
    normal_profile = switch_payload.get("active_profile")
    return {
        "active_profile": f"{normal_profile}+{aggressive_profile}",
        "target_profile": f"{normal_profile}+{aggressive_profile}",
        "execution_checklist": {
            "mode": "hold_cash"
            if _safe_float(merged_target.get("USDT"), 0.0) >= 0.999
            else "deploy"
        },
        "active_signal": {
            "latest_alloc": merged_target,
            "params_used": {
                "normal_profile": normal_profile,
                "aggressive_profile": aggressive_profile,
                "selected_tier": selected_tier,
                "budget_split": budget_split,
            },
        },
    }


def _run_once(args, ignored_args):
    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    tier_state_file = (
        Path(args.tier_state_file)
        if args.tier_state_file
        else skill_root / "results" / "auto_tier_state.json"
    )
    state_file = (
        Path(args.state_file)
        if args.state_file
        else skill_root / "results" / "auto_state.json"
    )
    lock_file = (
        Path(args.lock_file)
        if args.lock_file
        else skill_root / "results" / "auto_cycle.lock"
    )
    kill_switch_file = (
        Path(args.kill_switch_file)
        if args.kill_switch_file
        else skill_root / "results" / "kill_switch"
    )

    with file_lock(lock_file, timeout_sec=args.lock_timeout_sec):
        state = load_state(state_file)

        switch_payload, switch_source, switch_file = _load_or_run_switch_payload(args, script_dir, skill_root)
        flags = _derive_flags(switch_payload)
        tier_state = _load_tier_state(tier_state_file, initial_tier=args.initial_tier)
        updated_tier_state, decision = decide_tier(
            tier_state,
            flags,
            promote_days=args.promote_days,
            allow_aggressive=args.allow_aggressive,
            aggressive_promote_days=args.aggressive_promote_days,
        )
        selected_tier = updated_tier_state["current_tier"]
        preset = dict(TIER_PRESETS[selected_tier])

        primary_signal = normalize_target_alloc(switch_payload["active_signal"]["latest_alloc"])
        aggressive_signal = _compute_aggressive_signal(args, skill_root=skill_root)
        aggressive_alloc = normalize_target_alloc(aggressive_signal["alloc"])

        client = OkxClient(
            api_key=_load_env_key("OKX_API_KEY"),
            api_secret=_load_env_key("OKX_API_SECRET"),
            passphrase=_load_env_key("OKX_API_PASSPHRASE"),
            demo=args.demo,
            base_url=args.base_url,
            user_agent=args.user_agent,
        )

        transfer_args = SimpleNamespace(
            auto_transfer_usdt=bool(preset.get("auto_transfer_usdt", True)),
            transfer_in_dry_run=bool(args.transfer_in_dry_run),
            funding_reserve_usdt=float(preset.get("funding_reserve_usdt", 0.0)),
            min_transfer_usdt=float(preset.get("min_transfer_usdt", 10.0)),
            live=bool(args.live),
        )
        transfer_result = _transfer_funding_if_needed(client, transfer_args)
        balances = client.get_spot_balances()

        market_seed = {}
        market_seed.update(primary_signal)
        market_seed.update(aggressive_alloc)
        prices, spreads, price_errors = _build_market_snapshot(client, market_seed, balances)
        equity_usdt = _compute_equity_usdt(balances, prices)

        budget_split = resolve_dual_budget(args, equity_usdt)
        merged_target = blend_targets(primary_signal, aggressive_alloc, budget_split)

        finger_payload = _build_cycle_fingerprint_payload(
            switch_payload=switch_payload,
            aggressive_profile=args.aggressive_profile,
            budget_split=budget_split,
            merged_target=merged_target,
            selected_tier=selected_tier,
        )
        today = datetime.now().date().isoformat()
        fingerprint = compute_cycle_fingerprint(finger_payload, day=today)

        skipped = (not args.force) and should_skip_cycle(state, fingerprint)
        if skipped:
            out = {
                "generated_at": datetime.now().isoformat(),
                "mode": "LIVE" if args.live else "DRY_RUN",
                "cycle_status": "skipped",
                "skip_reason": "duplicate_cycle_fingerprint",
                "cycle_fingerprint": fingerprint,
                "state_file": str(state_file),
                "selected_tier": selected_tier,
                "switch_source": switch_source,
                "switch_file": switch_file,
                "normal_profile": switch_payload.get("active_profile"),
                "aggressive_profile": args.aggressive_profile,
                "target_alloc": merged_target,
                "budget_split": budget_split,
                "ignored_args": ignored_args,
            }
            record_cycle(
                state,
                fingerprint=fingerprint,
                status="noop",
                details={"skip_reason": "duplicate_cycle_fingerprint"},
            )
            save_state(state_file, state)
            return out

        plan = build_rebalance_plan(
            target_weights=merged_target,
            balances=balances,
            prices=prices,
            spreads_bps=spreads,
            min_order_usdt=args.min_order_usdt,
            max_order_usdt=args.max_order_usdt,
            max_spread_bps=args.max_spread_bps,
            allow_buy=args.allow_buy,
            allow_sell=args.allow_sell,
        )

        start_equity = ensure_day_start_equity(state, today, plan.get("equity_usdt", 0.0))
        day_pnl = day_pnl_snapshot(state, today, plan.get("equity_usdt", 0.0))
        guards = evaluate_trade_guards(
            live=args.live,
            target_alloc=merged_target,
            plan=plan,
            price_errors_count=len(price_errors),
            day_pnl_pct=day_pnl.get("pnl_pct"),
            kill_switch_file=str(kill_switch_file),
            max_risk_exposure_pct=float(preset["max_risk_exposure_pct"]),
            max_total_order_usdt=float(preset["max_total_order_usdt"]),
            max_order_count=int(preset["max_order_count"]),
            max_price_errors=int(preset["max_price_errors"]),
            max_daily_loss_pct=float(preset["max_daily_loss_pct"]),
        )

        if args.live and not guards["ok"]:
            execution = [{"status": "BLOCKED", "reason": ", ".join(guards["reasons"])}]
        else:
            execution = _execute_orders(client, plan.get("orders", []), live=args.live)
        execution_counts = summarize_execution(execution)
        cycle_status = _cycle_status(
            skipped=False,
            live=args.live,
            guards_ok=guards["ok"],
            order_count=len(plan.get("orders", [])),
            execution_counts=execution_counts,
        )

        out = {
            "generated_at": datetime.now().isoformat(),
            "mode": "LIVE" if args.live else "DRY_RUN",
            "cycle_status": cycle_status,
            "cycle_fingerprint": fingerprint,
            "state_file": str(state_file),
            "lock_file": str(lock_file),
            "kill_switch_file": str(kill_switch_file),
            "switch_source": switch_source,
            "switch_file": switch_file,
            "normal_profile": switch_payload.get("active_profile"),
            "aggressive_profile": args.aggressive_profile,
            "selected_tier": selected_tier,
            "tier_preset": preset,
            "decision": decision,
            "flags": flags,
            "normal_signal_alloc": primary_signal,
            "aggressive_signal": aggressive_signal,
            "budget_split": budget_split,
            "target_alloc": merged_target,
            "funding_transfer": transfer_result,
            "balances": balances,
            "prices": prices,
            "spreads_bps": {k: round(v, 4) for k, v in spreads.items()},
            "price_errors": price_errors,
            "plan": plan,
            "risk_guards": guards,
            "day_pnl": day_pnl,
            "day_start_equity_usdt": start_equity,
            "execution": execution,
            "execution_counts": execution_counts,
            "ignored_args": ignored_args,
        }
        record_cycle(
            state,
            fingerprint=fingerprint,
            status=cycle_status,
            details={
                "mode": out["mode"],
                "results_file": out.get("results_file"),
                "execution_counts": execution_counts,
            },
        )
        save_state(state_file, state)

        # Persist adaptive tier state only after execution path is complete.
        _save_tier_state(tier_state_file, updated_tier_state)

        notify_triggered = False
        out["notification_policy"] = "fills_or_actionable_failures_only"
        if not args.no_save_results:
            out["results_file"] = str(_next_dual_result_path(skill_root))

        if not args.no_notify and should_notify(out):
            notify_payload = {
                "event": "auto_cycle",
                "generated_at": out["generated_at"],
                "mode": out["mode"],
                "cycle_status": out["cycle_status"],
                "active_profile": f"{out['normal_profile']}+{out['aggressive_profile']}",
                "target_profile": out["selected_tier"],
                "execution_counts": execution_counts,
                "risk_ok": guards.get("ok"),
                "risk_reasons": guards.get("reasons"),
                "results_file": out.get("results_file"),
            }
            out["notify_results"] = notify_all(
                notify_payload,
                cli_urls=args.notify_webhook,
                timeout=args.notify_timeout_sec,
            )
            notify_triggered = True
        out["notify_triggered"] = notify_triggered
        if not args.no_save_results:
            _save_dual_result(skill_root, out)

        return out


def _build_parser():
    p = argparse.ArgumentParser(
        description="Run normal adaptive strategy + aggressive strategy in parallel sleeves and execute one merged rebalance."
    )
    p.add_argument("--live", action="store_true")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--base-url", type=str, default="https://www.okx.com")
    p.add_argument("--user-agent", type=str, default=None)
    p.add_argument("--allow-buy", dest="allow_buy", action="store_true", default=True)
    p.add_argument("--no-allow-buy", dest="allow_buy", action="store_false")
    p.add_argument("--allow-sell", dest="allow_sell", action="store_true", default=True)
    p.add_argument("--no-allow-sell", dest="allow_sell", action="store_false")

    p.add_argument("--min-order-usdt", type=float, default=10.0)
    p.add_argument("--max-order-usdt", type=float, default=1000.0)
    p.add_argument("--max-spread-bps", type=float, default=20.0)

    p.add_argument("--tier-state-file", type=str, default=None)
    p.add_argument("--initial-tier", choices=["conservative", "balanced", "aggressive"], default="conservative")
    p.add_argument("--promote-days", type=int, default=2)
    p.add_argument("--allow-aggressive", action="store_true")
    p.add_argument("--aggressive-promote-days", type=int, default=5)
    p.add_argument("--kill-switch-file", type=str, default=None)

    p.add_argument("--state-file", type=str, default=None)
    p.add_argument("--lock-file", type=str, default=None)
    p.add_argument("--lock-timeout-sec", type=float, default=10.0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--transfer-in-dry-run", action="store_true", default=False)

    p.add_argument("--notify-webhook", action="append", default=[])
    p.add_argument("--notify-timeout-sec", type=int, default=10)
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--no-save-results", action="store_true")

    p.add_argument(
        "--network-recover-retry",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    p.add_argument("--network-recover-max-wait-minutes", type=float, default=360.0)
    p.add_argument("--network-check-host", type=str, default="www.okx.com")
    p.add_argument("--network-check-port", type=int, default=443)
    p.add_argument("--network-check-timeout-sec", type=float, default=3.0)
    p.add_argument("--network-check-interval-sec", type=float, default=20.0)

    p.add_argument("--switch-file", type=str, default=None)
    p.add_argument("--no-save-switch-results", action="store_true")

    # Normal strategy switcher args
    p.add_argument("--capital-cny", type=float, default=10000)
    p.add_argument("--confirmations", type=int, default=2)
    p.add_argument("--check-window", type=int, default=120)
    p.add_argument("--signal-window", type=int, default=365)
    p.add_argument("--short-threshold", type=float, default=-0.03)
    p.add_argument("--shield-threshold", type=float, default=-0.015)
    p.add_argument("--risk-mode", choices=["auto", "normal", "rising"], default="auto")
    p.add_argument("--base-profile", type=str, default="stable")
    p.add_argument("--short-profile", type=str, default="stable_short_balanced")
    p.add_argument("--shield-profile", type=str, default="stable_shield")
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--switch-state-file", type=str, default=None)
    p.add_argument("--no-save-switch-state", action="store_true")
    p.add_argument("--no-cache", action="store_true")

    # Aggressive sleeve
    p.add_argument("--aggressive-profile", type=str, default="aggressive")
    p.add_argument("--aggressive-signal-window-days", type=int, default=365)
    p.add_argument("--aggressive-budget-usdt", type=float, default=200.0)
    p.add_argument(
        "--aggressive-ratio",
        type=float,
        default=None,
        help="Optional aggressive sleeve ratio of total equity (0~1). If set, ratio mode is used.",
    )
    p.add_argument(
        "--primary-ratio",
        type=float,
        default=None,
        help="Optional normal sleeve ratio of total equity (0~1). Used with --aggressive-ratio.",
    )
    p.add_argument(
        "--primary-budget-usdt",
        type=float,
        default=None,
        help="Normal strategy sleeve budget. If omitted, use equity - aggressive_budget.",
    )
    return p


def main():
    p = _build_parser()
    args, ignored_args = p.parse_known_args()

    if not _load_env_key("OKX_API_KEY"):
        raise SystemExit("Missing OKX_API_KEY")
    if not _load_env_key("OKX_API_SECRET"):
        raise SystemExit("Missing OKX_API_SECRET")
    if not _load_env_key("OKX_API_PASSPHRASE"):
        raise SystemExit("Missing OKX_API_PASSPHRASE")

    max_wait_sec = max(0.0, float(args.network_recover_max_wait_minutes) * 60.0)
    started_at = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            out = _run_once(args, ignored_args)
            out["network_recovery"] = {
                "enabled": bool(args.network_recover_retry),
                "attempts": attempt,
                "retried": attempt > 1,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return
        except Exception as e:
            if not args.network_recover_retry or not _is_network_related_error(e):
                raise
            elapsed = time.monotonic() - started_at
            remain = max_wait_sec - elapsed
            if remain <= 0:
                raise SystemExit(
                    f"Network recovery retry timeout after {args.network_recover_max_wait_minutes} minutes. "
                    f"Last error: {e}"
                )
            print(
                f"[{datetime.now().isoformat()}] network-related failure detected, waiting for recovery "
                f"(attempt={attempt}, remaining_sec={int(remain)}): {e}",
                file=sys.stderr,
                flush=True,
            )
            recovered = _wait_for_network_recovery(
                host=args.network_check_host,
                port=args.network_check_port,
                timeout_sec=args.network_check_timeout_sec,
                interval_sec=args.network_check_interval_sec,
                max_wait_sec=remain,
            )
            if not recovered:
                raise SystemExit(
                    f"Network not recovered within {args.network_recover_max_wait_minutes} minutes."
                )


if __name__ == "__main__":
    main()
