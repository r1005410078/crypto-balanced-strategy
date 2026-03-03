#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from okx_auto_executor import OkxClient, _safe_float


def _load_holdings_snapshot_file(skill_root):
    path = Path(skill_root) / "portfolio_snapshot.json"
    if not path.exists():
        return None, None
    return json.loads(path.read_text()), str(path)


def _to_inst_id(base_sym):
    return f"{str(base_sym).upper()}-USDT"


def _merge_balances(*rows):
    out = {}
    for row in rows:
        if not row:
            continue
        for k, v in row.items():
            sym = str(k).upper().strip()
            if not sym:
                continue
            qty = _safe_float(v, 0.0)
            if qty <= 0:
                continue
            out[sym] = out.get(sym, 0.0) + qty
    return out


def _build_live_holdings_snapshot(client, *, include_funding=True):
    spot_balances = client.get_spot_balances()
    funding_balances = client.get_funding_balances() if include_funding else {}
    balances = _merge_balances(spot_balances, funding_balances)
    assets = []
    unpriced_assets = []
    total = 0.0

    for sym in sorted(balances.keys()):
        qty = _safe_float(balances.get(sym, 0.0), 0.0)
        if qty <= 0:
            continue
        price = 1.0 if sym == "USDT" else 0.0
        if sym != "USDT":
            try:
                tk = client.get_ticker(_to_inst_id(sym))
                price = _safe_float(tk.get("price", 0.0), 0.0)
            except Exception as e:
                unpriced_assets.append({"symbol": sym, "error": str(e)})
                continue
            if price <= 0:
                unpriced_assets.append({"symbol": sym, "error": "invalid_price"})
                continue

        est_value = qty * price
        total += est_value
        assets.append(
            {
                "symbol": sym,
                "quantity": qty,
                "estimated_price_usdt": price,
                "estimated_value_usdt": est_value,
            }
        )

    now = datetime.now()
    return {
        "snapshot_source": "okx_live",
        "snapshot_date": now.date().isoformat(),
        "snapshot_time_local": now.isoformat(),
        "include_funding": bool(include_funding),
        "trade_balances": spot_balances,
        "funding_balances": funding_balances,
        "assets": assets,
        "unpriced_assets": unpriced_assets,
        "total_estimated_value_usdt": total,
    }


def _build_okx_client_from_env(*, base_url, user_agent, client_factory=OkxClient):
    api_key = os.environ.get("OKX_API_KEY", "").strip()
    api_secret = os.environ.get("OKX_API_SECRET", "").strip()
    api_passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    if not api_key or not api_secret or not api_passphrase:
        raise RuntimeError("Missing OKX API env vars: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE")
    return client_factory(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=api_passphrase,
        base_url=base_url,
        user_agent=user_agent,
    )


def _load_holdings_data(
    *,
    skill_root,
    holdings_source,
    include_funding,
    live_base_url,
    live_user_agent,
    client_factory=OkxClient,
):
    live_error = None
    if holdings_source in {"auto", "live"}:
        try:
            client = _build_okx_client_from_env(
                base_url=live_base_url,
                user_agent=live_user_agent,
                client_factory=client_factory,
            )
            snapshot = _build_live_holdings_snapshot(client, include_funding=include_funding)
            return snapshot, "okx_live", None
        except Exception as e:
            live_error = str(e)
            if holdings_source == "live":
                raise

    if holdings_source in {"auto", "snapshot"}:
        snapshot, path = _load_holdings_snapshot_file(skill_root)
        if snapshot is not None:
            return snapshot, path, live_error

    return None, None, live_error


def _normalize_holdings_weights(snapshot):
    assets = snapshot.get("assets", [])
    # Prefer explicit value field to build comparable weights.
    vals = {}
    total = 0.0
    for a in assets:
        sym = str(a.get("symbol", "")).upper()
        if not sym:
            continue
        v = float(a.get("estimated_value_usdt", 0.0))
        if v <= 0:
            continue
        vals[sym] = vals.get(sym, 0.0) + v
        total += v
    if total <= 0:
        return {}
    return {k: v / total for k, v in vals.items()}


def _normalize_model_alloc(model_alloc):
    out = {}
    for raw_sym, w in model_alloc.items():
        sym = str(raw_sym).upper()
        if sym != "USDT" and sym.endswith("USDT") and len(sym) > 4:
            sym = sym[:-4]
        out[sym] = out.get(sym, 0.0) + float(w)
    return out


def _build_holdings_adjustment(snapshot, model_alloc, capital_cny):
    if snapshot is None:
        return None
    current_w = _normalize_holdings_weights(snapshot)
    target_w = _normalize_model_alloc(model_alloc)
    if not current_w:
        return {
            "has_snapshot": True,
            "note": "Snapshot exists but missing usable estimated values.",
            "actions": [],
        }

    keys = sorted(set(current_w.keys()) | set(target_w.keys()))
    actions = []
    for sym in keys:
        c = float(current_w.get(sym, 0.0))
        t = float(target_w.get(sym, 0.0))
        diff = t - c
        if abs(diff) < 0.01:
            advice = "keep"
        elif diff > 0:
            advice = "add"
        else:
            advice = "reduce"
        actions.append(
            {
                "symbol": sym,
                "current_weight_pct": round(c * 100, 2),
                "target_weight_pct": round(t * 100, 2),
                "diff_weight_pct": round(diff * 100, 2),
                "suggestion": advice,
                "suggested_amount_cny": round(abs(diff) * capital_cny, 2),
            }
        )

    return {
        "has_snapshot": True,
        "snapshot_total_usdt": float(snapshot.get("total_estimated_value_usdt", 0.0)),
        "snapshot_date": snapshot.get("snapshot_date"),
        "snapshot_time_local": snapshot.get("snapshot_time_local"),
        "actions": actions,
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
    if args.state_file:
        cmd.extend(["--state-file", args.state_file])
    if args.no_save_state:
        cmd.append("--no-save-state")
    if args.no_save_switch_results:
        cmd.append("--no-save-results")
    return cmd


def _save_daily_report(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"daily_report_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def _save_portfolio_snapshot(skill_root, snapshot):
    path = Path(skill_root) / "portfolio_snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def _build_text_report(payload):
    summary = payload["summary"]
    check = payload["check_metrics"]
    checklist = payload["execution_checklist"]
    lines = []
    lines.append(f"Date: {payload['generated_at']}")
    lines.append(
        f"Profile: active={summary['active_profile']} target={summary['target_profile']} switched={summary['switched']}"
    )
    lines.append(
        f"Mode: {summary['mode']} | Risk Exposure: {summary['risk_exposure_pct']}% | Action Required: {summary['action_required']}"
    )
    lines.append(
        f"Check120: stable={check['stable']['return_pct']}% short={check['stable_short_balanced']['return_pct']}% shield={check['stable_shield']['return_pct']}%"
    )
    lines.append(
        f"Signal365: return={summary['signal_return_pct']}% mdd={summary['signal_max_drawdown_pct']}% sharpe={summary['signal_sharpe']}"
    )
    lines.append(f"Holdings Source: {payload.get('holdings_source') or 'none'}")
    lines.append(f"Holdings Snapshot Synced: {bool(payload.get('holdings_snapshot_synced'))}")
    if payload.get("holdings_live_error"):
        lines.append(f"Holdings Live Fallback: {payload['holdings_live_error']}")
    lines.append("Instructions:")
    for row in payload["instructions"]:
        lines.append(f"- {row}")
    lines.append(f"Next Check: {checklist['next_check_command']}")
    return "\n".join(lines)


def _build_brief_report(payload):
    summary = payload["summary"]
    checklist = payload["execution_checklist"]
    switched = "是" if summary["switched"] else "否"

    if checklist["mode"] == "hold_cash":
        action_line = "动作: 不下单（继续观望）"
        usdt_amt = checklist["capital_plan_cny"].get("USDT", 0.0)
        amount_line = f"金额: 风险仓 0.00 CNY；现金/USDT {usdt_amt:.2f} CNY"
    else:
        action_line = "动作: 下单（按分批执行）"
        risk_budget = 0.0
        for k, v in checklist["capital_plan_cny"].items():
            if k != "USDT":
                risk_budget += float(v)
        first_tranche_amt = 0.0
        for action in checklist.get("actions", []):
            if action.get("type") == "entry_tranche":
                first_tranche_amt = sum(float(x.get("amount_cny", 0.0)) for x in action.get("orders", []))
                break
        amount_line = (
            f"金额: 今日首批 {first_tranche_amt:.2f} CNY；风险预算总计 {risk_budget:.2f} CNY"
        )

    profile_line = (
        f"档位: 当前 {summary['active_profile']} | 目标 {summary['target_profile']} | 已切换 {switched}"
    )
    return "\n".join([profile_line, action_line, amount_line])


def _build_summary_payload(
    switch_payload,
    invoked_cmd,
    holdings_adjustment=None,
    holdings_path=None,
    holdings_source=None,
    holdings_live_error=None,
    holdings_snapshot_synced=False,
):
    checklist = switch_payload["execution_checklist"]
    active_signal = switch_payload["active_signal"]
    instructions = []
    if checklist["mode"] == "hold_cash":
        usdt_amt = checklist["capital_plan_cny"].get("USDT", 0.0)
        instructions.append(f"Hold cash/USDT: {usdt_amt:.2f} CNY equivalent.")
        instructions.append("Do not open risk positions until allocation leaves 100% USDT.")
    else:
        instructions.append("Deploy staged entries based on checklist tranches.")
        for action in checklist["actions"]:
            if action.get("type") != "entry_tranche":
                continue
            order_desc = []
            for od in action.get("orders", []):
                order_desc.append(f"{od['asset']} {od['amount_cny']:.2f} CNY")
            if order_desc:
                instructions.append(f"{action['instruction']}: " + ", ".join(order_desc))

    for g in checklist.get("guardrails", []):
        instructions.append(f"Guardrail[{g['rule']}]: {g['instruction']}")

    if holdings_adjustment and holdings_adjustment.get("actions"):
        for row in holdings_adjustment["actions"]:
            if row["suggestion"] == "keep":
                continue
            instructions.append(
                f"HoldingsAdjust[{row['symbol']}]: {row['suggestion']} {row['suggested_amount_cny']:.2f} CNY "
                f"(current {row['current_weight_pct']}% -> target {row['target_weight_pct']}%)"
            )

    summary = {
        "active_profile": switch_payload["active_profile"],
        "target_profile": switch_payload["target_profile"],
        "switched": switch_payload["switched"],
        "mode": checklist["mode"],
        "risk_exposure_pct": checklist["risk_exposure_pct"],
        "action_required": checklist["mode"] != "hold_cash",
        "signal_return_pct": active_signal["return_pct"],
        "signal_max_drawdown_pct": active_signal["max_drawdown_pct"],
        "signal_sharpe": active_signal["sharpe"],
        "latest_alloc": active_signal["latest_alloc"],
    }

    return {
        "generated_at": datetime.now().isoformat(),
        "summary": summary,
        "check_metrics": switch_payload["check_metrics"],
        "execution_checklist": checklist,
        "holdings_source": holdings_source,
        "holdings_live_error": holdings_live_error,
        "holdings_snapshot_synced": bool(holdings_snapshot_synced),
        "holdings_snapshot_file": holdings_path,
        "holdings_adjustment": holdings_adjustment,
        "instructions": instructions,
        "switch_command": invoked_cmd,
        "switch_result_file": switch_payload.get("results_file"),
    }


def main():
    p = argparse.ArgumentParser()
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
    p.add_argument("--state-file", type=str, default=None)
    p.add_argument("--no-save-state", action="store_true")
    p.add_argument("--no-save-switch-results", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--format", choices=["text", "json", "brief"], default="text")
    p.add_argument(
        "--holdings-source",
        choices=["auto", "live", "snapshot"],
        default="auto",
        help="auto: try OKX live holdings first, fallback to portfolio_snapshot.json.",
    )
    p.add_argument(
        "--holdings-include-funding",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include funding account balances for live holdings.",
    )
    p.add_argument("--holdings-live-base-url", type=str, default="https://www.okx.com")
    p.add_argument("--holdings-live-user-agent", type=str, default=None)
    p.add_argument(
        "--sync-holdings-snapshot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When live holdings are used, write them back to portfolio_snapshot.json.",
    )
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    switcher_path = script_dir / "profile_switcher.py"
    cmd = _build_switch_cmd(args, switcher_path)

    raw = subprocess.check_output(cmd, text=True)
    switch_payload = json.loads(raw)
    holdings_snapshot, holdings_path, holdings_live_error = _load_holdings_data(
        skill_root=skill_root,
        holdings_source=args.holdings_source,
        include_funding=args.holdings_include_funding,
        live_base_url=args.holdings_live_base_url,
        live_user_agent=args.holdings_live_user_agent,
    )
    holdings_snapshot_synced = False
    if (
        args.sync_holdings_snapshot
        and holdings_snapshot is not None
        and (holdings_snapshot.get("snapshot_source") == "okx_live")
    ):
        holdings_path = _save_portfolio_snapshot(skill_root, holdings_snapshot)
        holdings_snapshot_synced = True

    holdings_adjustment = _build_holdings_adjustment(
        snapshot=holdings_snapshot,
        model_alloc=switch_payload["active_signal"]["latest_alloc"],
        capital_cny=args.capital_cny,
    )
    report = _build_summary_payload(
        switch_payload,
        invoked_cmd=" ".join(cmd),
        holdings_adjustment=holdings_adjustment,
        holdings_path=holdings_path,
        holdings_source=(holdings_snapshot or {}).get("snapshot_source", holdings_path),
        holdings_live_error=holdings_live_error,
        holdings_snapshot_synced=holdings_snapshot_synced,
    )

    if not args.no_save_results:
        report["results_file"] = _save_daily_report(skill_root, report)

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.format == "brief":
        print(_build_brief_report(report))
    else:
        print(_build_text_report(report))


if __name__ == "__main__":
    main()
