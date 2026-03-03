#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


TIER_PRESETS = {
    "conservative": {
        "auto_transfer_usdt": True,
        "funding_reserve_usdt": 100,
        "min_transfer_usdt": 10,
        "max_risk_exposure_pct": 60,
        "max_total_order_usdt": 400,
        "max_order_count": 4,
        "max_price_errors": 0,
        "max_daily_loss_pct": 1.5,
    },
    "balanced": {
        "auto_transfer_usdt": True,
        "funding_reserve_usdt": 50,
        "min_transfer_usdt": 10,
        "max_risk_exposure_pct": 80,
        "max_total_order_usdt": 900,
        "max_order_count": 8,
        "max_price_errors": 0,
        "max_daily_loss_pct": 2.5,
    },
    "aggressive": {
        "auto_transfer_usdt": True,
        "funding_reserve_usdt": 20,
        "min_transfer_usdt": 10,
        "max_risk_exposure_pct": 95,
        "max_total_order_usdt": 2000,
        "max_order_count": 12,
        "max_price_errors": 1,
        "max_daily_loss_pct": 4.5,
    },
}


def _default_tier_state(initial_tier="conservative"):
    return {
        "version": 1,
        "current_tier": initial_tier,
        "normal_risk_streak": 0,
        "deploy_streak": 0,
        "updated_at": None,
    }


def _load_tier_state(path, initial_tier="conservative"):
    p = Path(path)
    if not p.exists():
        return _default_tier_state(initial_tier=initial_tier)
    payload = json.loads(p.read_text())
    out = _default_tier_state(initial_tier=initial_tier)
    out.update(payload)
    return out


def _save_tier_state(path, state):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def _derive_flags(switch_payload):
    checklist = switch_payload.get("execution_checklist", {})
    mode = str(checklist.get("mode", "")).strip()
    risk_rising = bool(
        switch_payload.get(
            "risk_rising_used",
            switch_payload.get("risk_features", {}).get("risk_rising", False),
        )
    )
    risk_exposure_pct = float(checklist.get("risk_exposure_pct", 0.0))
    return {
        "mode": mode,
        "risk_rising": risk_rising,
        "risk_exposure_pct": risk_exposure_pct,
        "is_deploy": (mode == "deploy" and risk_exposure_pct > 0),
    }


def decide_tier(
    state,
    flags,
    *,
    promote_days=2,
    allow_aggressive=False,
    aggressive_promote_days=5,
):
    out_state = dict(state or {})
    current = str(out_state.get("current_tier", "conservative"))
    normal_streak = int(out_state.get("normal_risk_streak", 0))
    deploy_streak = int(out_state.get("deploy_streak", 0))

    if flags.get("risk_rising"):
        normal_streak = 0
    else:
        normal_streak += 1

    if flags.get("is_deploy"):
        deploy_streak += 1
    else:
        deploy_streak = 0

    next_tier = current
    reasons = []

    if flags.get("risk_rising"):
        next_tier = "conservative"
        reasons.append("risk_rising=true -> force conservative")
    elif not flags.get("is_deploy"):
        if current == "aggressive":
            next_tier = "balanced"
            reasons.append("mode not deploy -> demote aggressive to balanced")
        elif current == "balanced":
            next_tier = "conservative"
            reasons.append("mode not deploy -> demote balanced to conservative")
    else:
        if current == "conservative":
            if normal_streak >= max(1, int(promote_days)) and deploy_streak >= max(1, int(promote_days)):
                next_tier = "balanced"
                reasons.append(
                    f"normal_risk_streak={normal_streak} and deploy_streak={deploy_streak} >= {int(promote_days)}"
                )
        elif current == "balanced" and allow_aggressive:
            if (
                normal_streak >= max(1, int(aggressive_promote_days))
                and deploy_streak >= max(1, int(aggressive_promote_days))
            ):
                next_tier = "aggressive"
                reasons.append(
                    "stable deploy streak reached aggressive threshold "
                    f"({int(aggressive_promote_days)} days)"
                )

    if not reasons:
        reasons.append("keep current tier")

    out_state["current_tier"] = next_tier
    out_state["normal_risk_streak"] = normal_streak
    out_state["deploy_streak"] = deploy_streak
    out_state["updated_at"] = datetime.now().isoformat()
    return out_state, {
        "tier_before": current,
        "tier_after": next_tier,
        "promoted": next_tier != current and TIER_ORDER[next_tier] > TIER_ORDER[current],
        "demoted": next_tier != current and TIER_ORDER[next_tier] < TIER_ORDER[current],
        "reasons": reasons,
        "normal_risk_streak": normal_streak,
        "deploy_streak": deploy_streak,
    }


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
    # Always save switch result so this wrapper can pass switch-file to auto_cycle.
    return cmd


def _run_switcher(args, script_dir, skill_root):
    switcher = script_dir / "profile_switcher.py"
    cmd = _build_switch_cmd(args, switcher)
    raw = subprocess.check_output(cmd, text=True)
    payload = json.loads(raw)
    switch_file = payload.get("results_file")
    if not switch_file:
        results_dir = skill_root / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = results_dir / f"switch_wrapped_{ts}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        switch_file = str(p)
    return payload, str(switch_file), " ".join(cmd)


def _build_auto_cycle_cmd(args, auto_cycle_script, switch_file, tier, kill_switch_file, passthrough):
    preset = TIER_PRESETS[tier]
    cmd = [sys.executable, str(auto_cycle_script), "--switch-file", str(switch_file)]
    if args.live:
        cmd.append("--live")
    if args.demo:
        cmd.append("--demo")
    if args.base_url:
        cmd.extend(["--base-url", args.base_url])
    if args.user_agent:
        cmd.extend(["--user-agent", args.user_agent])

    cmd.extend(["--kill-switch-file", str(kill_switch_file)])
    if args.no_save_results:
        cmd.append("--no-save-results")

    if args.allow_buy:
        cmd.append("--allow-buy")
    else:
        cmd.append("--no-allow-buy")
    if args.allow_sell:
        cmd.append("--allow-sell")
    else:
        cmd.append("--no-allow-sell")

    if args.state_file:
        cmd.extend(["--state-file", str(args.state_file)])
    if args.lock_file:
        cmd.extend(["--lock-file", str(args.lock_file)])
    if args.lock_timeout_sec is not None:
        cmd.extend(["--lock-timeout-sec", str(args.lock_timeout_sec)])
    if args.force:
        cmd.append("--force")

    if args.no_notify:
        cmd.append("--no-notify")
    else:
        for u in args.notify_webhook:
            cmd.extend(["--notify-webhook", u])
        cmd.extend(["--notify-timeout-sec", str(args.notify_timeout_sec)])

    if preset.get("auto_transfer_usdt", False):
        cmd.append("--auto-transfer-usdt")
    else:
        cmd.append("--no-auto-transfer-usdt")
    if args.transfer_in_dry_run:
        cmd.append("--transfer-in-dry-run")
    cmd.extend(["--funding-reserve-usdt", str(preset["funding_reserve_usdt"])])
    cmd.extend(["--min-transfer-usdt", str(preset["min_transfer_usdt"])])
    cmd.extend(["--max-risk-exposure-pct", str(preset["max_risk_exposure_pct"])])
    cmd.extend(["--max-total-order-usdt", str(preset["max_total_order_usdt"])])
    cmd.extend(["--max-order-count", str(preset["max_order_count"])])
    cmd.extend(["--max-price-errors", str(preset["max_price_errors"])])
    cmd.extend(["--max-daily-loss-pct", str(preset["max_daily_loss_pct"])])

    if passthrough:
        cmd.extend(passthrough)
    return cmd


def _save_auto_tier_result(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = results_dir / f"auto_tier_{ts}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(p)


TIER_ORDER = {"conservative": 1, "balanced": 2, "aggressive": 3}


def main():
    p = argparse.ArgumentParser(
        description="Adaptive tier wrapper: decides conservative/balanced/aggressive and runs auto_cycle."
    )
    p.add_argument("--live", action="store_true")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--base-url", type=str, default="https://www.okx.com")
    p.add_argument("--user-agent", type=str, default=None)
    p.add_argument("--allow-buy", dest="allow_buy", action="store_true", default=True)
    p.add_argument("--no-allow-buy", dest="allow_buy", action="store_false")
    p.add_argument("--allow-sell", dest="allow_sell", action="store_true", default=True)
    p.add_argument("--no-allow-sell", dest="allow_sell", action="store_false")

    p.add_argument("--tier-state-file", type=str, default=None)
    p.add_argument("--initial-tier", choices=["conservative", "balanced", "aggressive"], default="conservative")
    p.add_argument("--promote-days", type=int, default=2)
    p.add_argument("--allow-aggressive", action="store_true")
    p.add_argument("--aggressive-promote-days", type=int, default=5)
    p.add_argument("--kill-switch-file", type=str, default=None)

    p.add_argument("--state-file", type=str, default=None, help="Forward to auto_cycle state-file.")
    p.add_argument("--lock-file", type=str, default=None, help="Forward to auto_cycle lock-file.")
    p.add_argument("--lock-timeout-sec", type=float, default=10.0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--transfer-in-dry-run", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--notify-webhook", action="append", default=[])
    p.add_argument("--notify-timeout-sec", type=int, default=10)
    p.add_argument("--no-save-results", action="store_true")

    # Switcher args
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
    p.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,LINKUSDT")
    p.add_argument("--regime-symbol", type=str, default="BTCUSDT")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--cache-ttl-hours", type=int, default=6)
    p.add_argument("--switch-state-file", type=str, default=None)
    p.add_argument("--no-save-switch-state", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    args, passthrough = p.parse_known_args()

    if not os.environ.get("OKX_API_KEY", "").strip():
        raise SystemExit("Missing OKX_API_KEY")
    if not os.environ.get("OKX_API_SECRET", "").strip():
        raise SystemExit("Missing OKX_API_SECRET")
    if not os.environ.get("OKX_API_PASSPHRASE", "").strip():
        raise SystemExit("Missing OKX_API_PASSPHRASE")

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    auto_cycle = script_dir / "auto_cycle.py"
    kill_switch_file = Path(args.kill_switch_file) if args.kill_switch_file else skill_root / "results" / "kill_switch"
    tier_state_file = (
        Path(args.tier_state_file)
        if args.tier_state_file
        else skill_root / "results" / "auto_tier_state.json"
    )

    switch_payload, switch_file, switch_source = _run_switcher(args, script_dir, skill_root)
    flags = _derive_flags(switch_payload)

    tier_state = _load_tier_state(tier_state_file, initial_tier=args.initial_tier)
    updated_tier_state, decision = decide_tier(
        tier_state,
        flags,
        promote_days=args.promote_days,
        allow_aggressive=args.allow_aggressive,
        aggressive_promote_days=args.aggressive_promote_days,
    )
    _save_tier_state(tier_state_file, updated_tier_state)
    selected_tier = updated_tier_state["current_tier"]

    cycle_cmd = _build_auto_cycle_cmd(
        args,
        auto_cycle_script=auto_cycle,
        switch_file=switch_file,
        tier=selected_tier,
        kill_switch_file=kill_switch_file,
        passthrough=passthrough,
    )
    cycle_raw = subprocess.check_output(cycle_cmd, text=True)
    cycle_payload = json.loads(cycle_raw)

    out = {
        "generated_at": datetime.now().isoformat(),
        "tier_state_file": str(tier_state_file),
        "selected_tier": selected_tier,
        "tier_preset": TIER_PRESETS[selected_tier],
        "decision": decision,
        "flags": flags,
        "switch_source": switch_source,
        "switch_file": switch_file,
        "switch_summary": {
            "active_profile": switch_payload.get("active_profile"),
            "target_profile": switch_payload.get("target_profile"),
            "risk_rising_used": switch_payload.get("risk_rising_used"),
            "execution_mode": switch_payload.get("execution_checklist", {}).get("mode"),
            "risk_exposure_pct": switch_payload.get("execution_checklist", {}).get("risk_exposure_pct"),
        },
        "auto_cycle_command": " ".join(cycle_cmd),
        "auto_cycle_result": cycle_payload,
    }
    if not args.no_save_results:
        out["results_file"] = _save_auto_tier_result(skill_root, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
