#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from okx_auto_executor import OkxApiError, OkxClient, _safe_float, build_rebalance_plan


def _to_base_symbol(sym):
    s = str(sym).upper().strip()
    s = s.replace("-", "")
    if s.endswith("USDT") and s != "USDT":
        return s[:-4]
    return s


def _to_inst_id(base_symbol):
    return f"{str(base_symbol).upper()}-USDT"


def _save_health_result(skill_root, payload):
    results = Path(skill_root) / "results"
    results.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = results / f"health_check_{ts}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def build_health_summary(checks):
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for c in checks:
        st = str(c.get("status", "WARN")).upper()
        counts[st] = counts.get(st, 0) + 1
    overall = "PASS"
    if counts["FAIL"] > 0:
        overall = "FAIL"
    elif counts["WARN"] > 0:
        overall = "WARN"
    return {"overall": overall, "counts": counts}


def _text_report(payload):
    lines = []
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"Overall: {payload['summary']['overall']}")
    for c in payload["checks"]:
        lines.append(f"- [{c['status']}] {c['name']}: {c['message']}")
    lines.append(
        "Counts: "
        f"PASS={payload['summary']['counts']['PASS']} "
        f"WARN={payload['summary']['counts']['WARN']} "
        f"FAIL={payload['summary']['counts']['FAIL']}"
    )
    lines.append(
        "Context: "
        f"symbol={payload['symbol']} "
        f"notional_usdt={payload['notional_usdt']} "
        f"trade_usdt={payload.get('trade_usdt')}"
    )
    return "\n".join(lines)


def run_health_check(
    *,
    symbol="BTCUSDT",
    notional_usdt=1.0,
    max_spread_bps=50.0,
    base_url="https://www.okx.com",
):
    checks = []
    api_key = os.environ.get("OKX_API_KEY", "").strip()
    api_secret = os.environ.get("OKX_API_SECRET", "").strip()
    api_passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    if not api_key or not api_secret or not api_passphrase:
        checks.append(
            {
                "name": "okx_env",
                "status": "FAIL",
                "message": "Missing OKX API env vars.",
            }
        )
        payload = {
            "generated_at": datetime.now().isoformat(),
            "symbol": symbol,
            "notional_usdt": float(notional_usdt),
            "checks": checks,
        }
        payload["summary"] = build_health_summary(checks)
        return payload

    base_sym = _to_base_symbol(symbol)
    inst_id = _to_inst_id(base_sym)
    client = OkxClient(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=api_passphrase,
        base_url=base_url,
    )

    balances = {}
    ticker = {}
    plan = {}

    try:
        balances = client.get_spot_balances()
        trade_usdt = _safe_float(balances.get("USDT", 0.0))
        checks.append(
            {
                "name": "okx_auth",
                "status": "PASS",
                "message": f"Auth success. trade_usdt={trade_usdt:.6f}",
            }
        )
    except OkxApiError as e:
        checks.append(
            {
                "name": "okx_auth",
                "status": "FAIL",
                "message": str(e),
            }
        )
        payload = {
            "generated_at": datetime.now().isoformat(),
            "symbol": symbol,
            "notional_usdt": float(notional_usdt),
            "checks": checks,
        }
        payload["summary"] = build_health_summary(checks)
        return payload

    try:
        ticker = client.get_ticker(inst_id)
        spread_bps = _safe_float(ticker.get("spread_bps"), 0.0)
        spread_status = "PASS" if spread_bps <= float(max_spread_bps) else "WARN"
        checks.append(
            {
                "name": "market_ticker",
                "status": "PASS",
                "message": f"{inst_id} price={_safe_float(ticker.get('price')):.6f} spread_bps={spread_bps:.4f}",
            }
        )
        checks.append(
            {
                "name": "market_spread",
                "status": spread_status,
                "message": (
                    f"spread_bps {spread_bps:.4f} "
                    f"{'within' if spread_status == 'PASS' else 'above'} max_spread_bps {float(max_spread_bps):.4f}"
                ),
            }
        )
    except OkxApiError as e:
        checks.append(
            {
                "name": "market_ticker",
                "status": "FAIL",
                "message": str(e),
            }
        )
        payload = {
            "generated_at": datetime.now().isoformat(),
            "symbol": symbol,
            "notional_usdt": float(notional_usdt),
            "checks": checks,
        }
        payload["summary"] = build_health_summary(checks)
        return payload

    plan = build_rebalance_plan(
        target_weights={base_sym: 1.0, "USDT": 0.0},
        balances=balances,
        prices={base_sym: _safe_float(ticker.get("price"), 0.0)},
        spreads_bps={base_sym: _safe_float(ticker.get("spread_bps"), 0.0)},
        min_order_usdt=float(notional_usdt),
        max_order_usdt=float(notional_usdt),
        max_spread_bps=float(max_spread_bps),
        allow_buy=True,
        allow_sell=False,
    )
    buy_orders = [
        x for x in plan.get("orders", []) if x.get("side") == "buy" and x.get("symbol") == base_sym
    ]
    if buy_orders:
        checks.append(
            {
                "name": "dryrun_plan",
                "status": "PASS",
                "message": f"Buy plan ready: {base_sym} notional={buy_orders[0].get('notional_usdt')}",
            }
        )
    else:
        checks.append(
            {
                "name": "dryrun_plan",
                "status": "WARN",
                "message": f"No buy order generated. skipped={plan.get('skipped', [])}",
            }
        )

    payload = {
        "generated_at": datetime.now().isoformat(),
        "symbol": symbol,
        "base_symbol": base_sym,
        "inst_id": inst_id,
        "notional_usdt": float(notional_usdt),
        "max_spread_bps": float(max_spread_bps),
        "trade_usdt": round(_safe_float(balances.get("USDT", 0.0)), 8),
        "checks": checks,
        "ticker": ticker,
        "plan": plan,
    }
    payload["summary"] = build_health_summary(checks)
    return payload


def main():
    p = argparse.ArgumentParser(
        description="Daily health check: 1 USDT dry-run connectivity check (no live orders)."
    )
    p.add_argument("--symbol", type=str, default="BTCUSDT")
    p.add_argument("--notional-usdt", type=float, default=1.0)
    p.add_argument("--max-spread-bps", type=float, default=50.0)
    p.add_argument("--base-url", type=str, default="https://www.okx.com")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--fail-on-warn", action="store_true")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    payload = run_health_check(
        symbol=args.symbol,
        notional_usdt=args.notional_usdt,
        max_spread_bps=args.max_spread_bps,
        base_url=args.base_url,
    )

    if not args.no_save_results:
        payload["results_file"] = _save_health_result(skill_root, payload)

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_text_report(payload))

    overall = payload["summary"]["overall"]
    if overall == "FAIL":
        raise SystemExit(1)
    if overall == "WARN" and args.fail_on_warn:
        raise SystemExit(2)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
