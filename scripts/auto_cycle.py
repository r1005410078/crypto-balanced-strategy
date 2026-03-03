#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

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
from notifier import notify_all
from okx_auto_executor import (
    OkxClient,
    build_rebalance_plan,
    normalize_target_alloc,
    _safe_float,
)
from risk_guard import evaluate_trade_guards, summarize_execution


def _to_inst_id(base_symbol):
    return f"{str(base_symbol).upper()}-USDT"


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


def _load_or_run_switch_payload(args, script_dir):
    if args.switch_file:
        p = Path(args.switch_file)
        return json.loads(p.read_text()), f"file:{p}"
    switcher = script_dir / "profile_switcher.py"
    cmd = _build_switch_cmd(args, switcher)
    raw = subprocess.check_output(cmd, text=True)
    return json.loads(raw), " ".join(cmd)


def _load_env_key(name):
    return os.environ.get(name, "").strip()


def _save_cycle_result(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = results_dir / f"auto_cycle_{ts}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def _transfer_funding_if_needed(client, args):
    funding = client.get_funding_balances(ccy="USDT")
    funding_usdt = _safe_float(funding.get("USDT", 0.0))
    plan_amt = max(0.0, funding_usdt - max(0.0, args.funding_reserve_usdt))
    out = {
        "enabled": bool(args.auto_transfer_usdt),
        "execute_in_dry_run": bool(args.transfer_in_dry_run),
        "funding_usdt_before": round(funding_usdt, 8),
        "planned_transfer_usdt": round(plan_amt, 8),
        "min_transfer_usdt": round(float(args.min_transfer_usdt), 8),
        "status": "SKIPPED",
        "transfer": None,
    }
    if not args.auto_transfer_usdt:
        out["status"] = "DISABLED"
        return out
    if plan_amt < float(args.min_transfer_usdt):
        out["status"] = "TOO_SMALL"
        return out
    if not args.live and not args.transfer_in_dry_run:
        out["status"] = "DRY_RUN"
        return out
    transfer = client.transfer_funding_to_trading("USDT", plan_amt)
    out["status"] = "SUBMITTED"
    out["transfer"] = transfer
    return out


def _build_market_snapshot(client, target_alloc, balances):
    symbols_for_price = sorted(
        {k for k in target_alloc.keys() if k != "USDT"} | {k for k in balances.keys() if k != "USDT"}
    )
    prices = {}
    spreads = {}
    price_errors = []
    for sym in symbols_for_price:
        inst = _to_inst_id(sym)
        try:
            tk = client.get_ticker(inst)
            prices[sym] = _safe_float(tk.get("price"), 0.0)
            spreads[sym] = _safe_float(tk.get("spread_bps"), 0.0)
        except Exception as e:
            price_errors.append({"symbol": sym, "inst_id": inst, "error": str(e)})
    return prices, spreads, price_errors


def _execute_orders(client, orders, live):
    execution = []
    for i, od in enumerate(orders, start=1):
        cl_ord_id = f"cbsa{int(datetime.now().timestamp())}{i:03d}"
        if not live:
            execution.append({"status": "DRY_RUN", "order": od})
            continue
        try:
            side = od["side"]
            if side == "buy":
                resp = client.place_market_order(
                    inst_id=od["inst_id"],
                    side="buy",
                    size=_safe_float(od["notional_usdt"]),
                    cl_ord_id=cl_ord_id,
                )
            else:
                resp = client.place_market_order(
                    inst_id=od["inst_id"],
                    side="sell",
                    size=_safe_float(od["size"]),
                    cl_ord_id=cl_ord_id,
                )
            execution.append({"status": "SUBMITTED", "order": od, "exchange": resp})
        except Exception as e:
            execution.append({"status": "FAILED", "order": od, "error": str(e)})
    return execution


def _cycle_status(*, skipped, live, guards_ok, order_count, execution_counts):
    if skipped:
        return "skipped"
    if live and not guards_ok:
        return "blocked"
    if order_count <= 0:
        return "noop"
    if not live:
        return "dry_run"
    if execution_counts.get("FAILED", 0) > 0 and execution_counts.get("SUBMITTED", 0) <= 0:
        return "failed"
    if execution_counts.get("FAILED", 0) > 0:
        return "partial"
    return "executed"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="Place live orders, default is dry-run.")
    p.add_argument("--demo", action="store_true", help="Use OKX simulated trading header.")
    p.add_argument("--base-url", type=str, default="https://www.okx.com")
    p.add_argument("--user-agent", type=str, default=None)

    p.add_argument("--allow-buy", dest="allow_buy", action="store_true", default=True)
    p.add_argument("--no-allow-buy", dest="allow_buy", action="store_false")
    p.add_argument("--allow-sell", dest="allow_sell", action="store_true", default=True)
    p.add_argument("--no-allow-sell", dest="allow_sell", action="store_false")

    p.add_argument("--min-order-usdt", type=float, default=10.0)
    p.add_argument("--max-order-usdt", type=float, default=1000.0)
    p.add_argument("--max-spread-bps", type=float, default=20.0)

    p.add_argument("--auto-transfer-usdt", dest="auto_transfer_usdt", action="store_true", default=True)
    p.add_argument("--no-auto-transfer-usdt", dest="auto_transfer_usdt", action="store_false")
    p.add_argument("--transfer-in-dry-run", action="store_true", default=False)
    p.add_argument("--min-transfer-usdt", type=float, default=10.0)
    p.add_argument("--funding-reserve-usdt", type=float, default=0.0)

    p.add_argument("--max-risk-exposure-pct", type=float, default=95.0)
    p.add_argument("--max-total-order-usdt", type=float, default=5000.0)
    p.add_argument("--max-order-count", type=int, default=20)
    p.add_argument("--max-price-errors", type=int, default=0)
    p.add_argument("--max-daily-loss-pct", type=float, default=3.0)
    p.add_argument("--kill-switch-file", type=str, default=None)

    p.add_argument("--state-file", type=str, default=None)
    p.add_argument("--lock-file", type=str, default=None)
    p.add_argument("--lock-timeout-sec", type=float, default=10.0)
    p.add_argument("--force", action="store_true", help="Ignore idempotency skip for identical cycle signal.")

    p.add_argument("--notify-webhook", action="append", default=[])
    p.add_argument("--notify-timeout-sec", type=int, default=10)
    p.add_argument("--no-notify", action="store_true")

    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--switch-file", type=str, default=None)
    p.add_argument("--no-save-switch-results", action="store_true")

    # Passthrough switcher args.
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

    args = p.parse_args()

    api_key = _load_env_key("OKX_API_KEY")
    api_secret = _load_env_key("OKX_API_SECRET")
    api_passphrase = _load_env_key("OKX_API_PASSPHRASE")
    if not api_key or not api_secret or not api_passphrase:
        raise SystemExit("Missing OKX API env vars: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE")

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    state_file = Path(args.state_file) if args.state_file else skill_root / "results" / "auto_state.json"
    lock_file = Path(args.lock_file) if args.lock_file else skill_root / "results" / "auto_cycle.lock"
    kill_switch_file = (
        Path(args.kill_switch_file) if args.kill_switch_file else skill_root / "results" / "kill_switch"
    )

    with file_lock(lock_file, timeout_sec=args.lock_timeout_sec):
        state = load_state(state_file)

        switch_payload, switch_source = _load_or_run_switch_payload(args, script_dir)
        today = datetime.now().date().isoformat()
        fingerprint = compute_cycle_fingerprint(switch_payload, day=today)

        skipped = (not args.force) and should_skip_cycle(state, fingerprint)
        if skipped:
            out = {
                "generated_at": datetime.now().isoformat(),
                "mode": "LIVE" if args.live else "DRY_RUN",
                "cycle_status": "skipped",
                "skip_reason": "duplicate_cycle_fingerprint",
                "cycle_fingerprint": fingerprint,
                "state_file": str(state_file),
                "switch_source": switch_source,
                "active_profile": switch_payload.get("active_profile"),
                "target_profile": switch_payload.get("target_profile"),
            }
            record_cycle(
                state,
                fingerprint=fingerprint,
                status="noop",
                details={"skip_reason": "duplicate_cycle_fingerprint"},
            )
            save_state(state_file, state)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        target_alloc = normalize_target_alloc(switch_payload["active_signal"]["latest_alloc"])
        client = OkxClient(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=api_passphrase,
            demo=args.demo,
            base_url=args.base_url,
            user_agent=args.user_agent,
        )

        transfer_result = _transfer_funding_if_needed(client, args)
        balances = client.get_spot_balances()
        prices, spreads, price_errors = _build_market_snapshot(client, target_alloc, balances)
        plan = build_rebalance_plan(
            target_weights=target_alloc,
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
            target_alloc=target_alloc,
            plan=plan,
            price_errors_count=len(price_errors),
            day_pnl_pct=day_pnl.get("pnl_pct"),
            kill_switch_file=str(kill_switch_file),
            max_risk_exposure_pct=args.max_risk_exposure_pct,
            max_total_order_usdt=args.max_total_order_usdt,
            max_order_count=args.max_order_count,
            max_price_errors=args.max_price_errors,
            max_daily_loss_pct=args.max_daily_loss_pct,
        )

        execution = []
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
            "switch_source": switch_source,
            "state_file": str(state_file),
            "lock_file": str(lock_file),
            "kill_switch_file": str(kill_switch_file),
            "active_profile": switch_payload.get("active_profile"),
            "target_profile": switch_payload.get("target_profile"),
            "switch_payload": switch_payload,
            "target_alloc": target_alloc,
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
        }
        if not args.no_save_results:
            out["results_file"] = _save_cycle_result(skill_root, out)

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

        if not args.no_notify:
            notify_payload = {
                "event": "auto_cycle",
                "generated_at": out["generated_at"],
                "mode": out["mode"],
                "cycle_status": cycle_status,
                "active_profile": out.get("active_profile"),
                "target_profile": out.get("target_profile"),
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

        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
