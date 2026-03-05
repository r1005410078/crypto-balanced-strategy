#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from okx_auto_executor import OkxClient, _safe_float


def _is_nonzero(v, eps=1e-12):
    return abs(float(v)) > eps


def _parse_trading_details(balance_row, top_n=15):
    details = (balance_row or {}).get("details") or []
    rows = []
    for d in details:
        ccy = str(d.get("ccy", "")).upper().strip()
        if not ccy:
            continue
        row = {
            "ccy": ccy,
            "eq": _safe_float(d.get("eq"), 0.0),
            "eq_usd": _safe_float(d.get("eqUsd"), 0.0),
            "avail_bal": _safe_float(d.get("availBal"), 0.0),
            "cash_bal": _safe_float(d.get("cashBal"), 0.0),
            "stgy_eq": _safe_float(d.get("stgyEq"), 0.0),
            "frozen_bal": _safe_float(d.get("frozenBal"), 0.0),
            "ord_frozen": _safe_float(d.get("ordFrozen"), 0.0),
            "upl": _safe_float(d.get("upl"), 0.0),
            "u_time": d.get("uTime"),
        }
        if not any(
            _is_nonzero(row[k])
            for k in ("eq", "eq_usd", "avail_bal", "cash_bal", "stgy_eq", "frozen_bal", "ord_frozen", "upl")
        ):
            continue
        rows.append(row)

    rows.sort(key=lambda x: (abs(x["eq_usd"]), abs(x["eq"])), reverse=True)
    return rows[: max(1, int(top_n))]


def _parse_funding_balances(rows, top_n=15):
    out = []
    for r in rows or []:
        ccy = str(r.get("ccy", "")).upper().strip()
        if not ccy:
            continue
        item = {
            "ccy": ccy,
            "bal": _safe_float(r.get("bal"), 0.0),
            "avail_bal": _safe_float(r.get("availBal"), 0.0),
            "frozen_bal": _safe_float(r.get("frozenBal"), 0.0),
        }
        if not any(_is_nonzero(item[k]) for k in ("bal", "avail_bal", "frozen_bal")):
            continue
        out.append(item)
    out.sort(key=lambda x: abs(x["bal"]), reverse=True)
    return out[: max(1, int(top_n))]


def _strategy_occupied_summary(trading_assets):
    occupied = [
        x
        for x in (trading_assets or [])
        if _is_nonzero(x.get("stgy_eq", 0.0)) or _is_nonzero(x.get("frozen_bal", 0.0))
    ]
    eq_usd_total = 0.0
    for x in occupied:
        eq = abs(_safe_float(x.get("eq"), 0.0))
        eq_usd = abs(_safe_float(x.get("eq_usd"), 0.0))
        occupied_qty = max(abs(_safe_float(x.get("stgy_eq"), 0.0)), abs(_safe_float(x.get("frozen_bal"), 0.0)))
        if eq > 0 and eq_usd > 0:
            eq_usd_total += eq_usd * min(1.0, occupied_qty / eq)
        elif eq_usd > 0:
            eq_usd_total += eq_usd
        elif str(x.get("ccy", "")).upper() == "USDT":
            eq_usd_total += occupied_qty
    usdt = next((x for x in occupied if x.get("ccy") == "USDT"), None)
    btc = next((x for x in occupied if x.get("ccy") == "BTC"), None)
    return {
        "occupied_assets_count": len(occupied),
        "occupied_eq_usd_total": eq_usd_total,
        "usdt_stgy_eq": _safe_float((usdt or {}).get("stgy_eq"), 0.0),
        "usdt_frozen_bal": _safe_float((usdt or {}).get("frozen_bal"), 0.0),
        "btc_stgy_eq": _safe_float((btc or {}).get("stgy_eq"), 0.0),
        "btc_frozen_bal": _safe_float((btc or {}).get("frozen_bal"), 0.0),
        "occupied_assets": occupied,
    }


def _fetch_running_bot_rows(client, *, limit=20):
    out = {}

    dca = client._request(
        "GET",
        "/api/v5/tradingBot/dca/orders-algo-pending",
        params={"limit": str(int(limit))},
        auth=True,
    )
    out["dca_pending"] = dca

    recurring = client._request(
        "GET",
        "/api/v5/tradingBot/recurring/orders-algo-pending",
        params={"limit": str(int(limit))},
        auth=True,
    )
    out["recurring_pending"] = recurring

    grid = client._request(
        "GET",
        "/api/v5/tradingBot/grid/orders-algo-pending",
        params={"algoOrdType": "grid", "limit": str(int(limit))},
        auth=True,
    )
    out["grid_pending"] = grid

    return out


def _simplify_bot_rows(rows, top_n=10):
    out = []
    for r in (rows or [])[: max(1, int(top_n))]:
        out.append(
            {
                "algo_id": r.get("algoId"),
                "algo_type": r.get("algoOrdType"),
                "inst_id": r.get("instId"),
                "state": r.get("state"),
                "investment_amt": r.get("investmentAmt"),
                "investment_ccy": r.get("investmentCcy"),
                "avg_px": r.get("avgPx"),
                "float_profit": r.get("floatProfit"),
                "total_pnl": r.get("totalPnl"),
                "tp_px": r.get("tpPx"),
                "sl_px": r.get("slPx"),
                "completed_cycles": r.get("completedCycles"),
                "c_time": r.get("cTime"),
                "u_time": r.get("uTime"),
            }
        )
    return out


def _build_text(payload):
    cfg = payload["account_config"]
    totals = payload["trading_totals"]
    occ = payload["strategy_occupied"]
    lines = []
    lines.append(f"As Of: {payload['as_of_local']}")
    lines.append(
        "Account: "
        f"uid={cfg.get('uid')} acctLv={cfg.get('acct_lv')} posMode={cfg.get('pos_mode')} perm={cfg.get('perm')}"
    )
    lines.append(
        "Trading Equity: "
        f"totalEq={totals.get('total_eq_usd'):.6f} USD (uTime={totals.get('u_time')})"
    )
    lines.append(
        "Strategy Occupied: "
        f"assets={occ.get('occupied_assets_count')} eqUsd(approx)={occ.get('occupied_eq_usd_total'):.6f} "
        f"| USDT(stgy/frozen)={occ.get('usdt_stgy_eq'):.6f}/{occ.get('usdt_frozen_bal'):.6f} "
        f"| BTC(stgy/frozen)={occ.get('btc_stgy_eq'):.12f}/{occ.get('btc_frozen_bal'):.12f}"
    )

    lines.append("Trading Assets (top):")
    for row in payload.get("trading_assets_top", []):
        lines.append(
            "- "
            f"{row['ccy']}: eq={row['eq']:.12f} (~{row['eq_usd']:.6f} USD), "
            f"avail={row['avail_bal']:.12f}, stgy={row['stgy_eq']:.12f}, frozen={row['frozen_bal']:.12f}"
        )
    if not payload.get("trading_assets_top"):
        lines.append("- none")

    lines.append("Funding Assets (top):")
    for row in payload.get("funding_assets_top", []):
        lines.append(
            "- "
            f"{row['ccy']}: bal={row['bal']:.12f}, avail={row['avail_bal']:.12f}, frozen={row['frozen_bal']:.12f}"
        )
    if not payload.get("funding_assets_top"):
        lines.append("- none")

    bots = payload.get("running_bots", {})
    lines.append(
        "Running Bots: "
        f"dca={bots.get('dca_pending_count', 0)} "
        f"recurring={bots.get('recurring_pending_count', 0)} "
        f"grid={bots.get('grid_pending_count', 0)} "
        f"total={bots.get('total_running_count', 0)}"
    )
    for row in bots.get("dca_pending_top", []):
        lines.append(
            "- DCA "
            f"algoId={row.get('algo_id')} {row.get('inst_id')} state={row.get('state')} "
            f"invest={row.get('investment_amt')} {row.get('investment_ccy')} "
            f"float={row.get('float_profit')} totalPnl={row.get('total_pnl')}"
        )

    lines.append(
        "Spot Activity: "
        f"pending_orders={payload.get('spot_pending_orders_count', 0)} "
        f"recent_fills={payload.get('spot_recent_fills_count', 0)}"
    )
    return "\n".join(lines)


def _save_result(skill_root, payload):
    results_dir = Path(skill_root) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"account_equity_breakdown_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def _build_client_from_env(*, base_url, user_agent):
    api_key = os.environ.get("OKX_API_KEY", "").strip()
    api_secret = os.environ.get("OKX_API_SECRET", "").strip()
    api_passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    if not api_key or not api_secret or not api_passphrase:
        raise SystemExit("Missing OKX API env vars: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE")
    return OkxClient(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=api_passphrase,
        base_url=base_url,
        user_agent=user_agent,
    )


def main():
    p = argparse.ArgumentParser(
        description="Read-only OKX account equity breakdown: available vs strategy-occupied balances + running bots."
    )
    p.add_argument("--top-assets", type=int, default=15)
    p.add_argument("--fills-limit", type=int, default=20)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--base-url", type=str, default="https://www.okx.com")
    p.add_argument("--user-agent", type=str, default=None)
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    client = _build_client_from_env(base_url=args.base_url, user_agent=args.user_agent)

    cfg_rows = client._request("GET", "/api/v5/account/config", auth=True)
    cfg = cfg_rows[0] if cfg_rows else {}

    bal_rows = client._request("GET", "/api/v5/account/balance", auth=True)
    bal_row = bal_rows[0] if bal_rows else {}
    trading_assets = _parse_trading_details(bal_row, top_n=args.top_assets)
    occ = _strategy_occupied_summary(trading_assets)

    funding_rows = client._request("GET", "/api/v5/asset/balances", auth=True)
    funding_assets = _parse_funding_balances(funding_rows, top_n=args.top_assets)

    bots = _fetch_running_bot_rows(client, limit=20)
    dca_rows = bots.get("dca_pending", [])
    recurring_rows = bots.get("recurring_pending", [])
    grid_rows = bots.get("grid_pending", [])

    pending_spot_orders = client._request(
        "GET",
        "/api/v5/trade/orders-pending",
        params={"instType": "SPOT"},
        auth=True,
    )
    recent_spot_fills = client.get_fills_history(inst_type="SPOT", limit=max(1, int(args.fills_limit)))

    payload = {
        "as_of_local": datetime.now().astimezone().isoformat(),
        "account_config": {
            "uid": cfg.get("uid"),
            "acct_lv": cfg.get("acctLv"),
            "pos_mode": cfg.get("posMode"),
            "perm": cfg.get("perm"),
            "level": cfg.get("level"),
        },
        "trading_totals": {
            "total_eq_usd": _safe_float(bal_row.get("totalEq"), 0.0),
            "u_time": bal_row.get("uTime"),
        },
        "strategy_occupied": occ,
        "trading_assets_top": trading_assets,
        "funding_assets_top": funding_assets,
        "running_bots": {
            "dca_pending_count": len(dca_rows),
            "recurring_pending_count": len(recurring_rows),
            "grid_pending_count": len(grid_rows),
            "total_running_count": len(dca_rows) + len(recurring_rows) + len(grid_rows),
            "dca_pending_top": _simplify_bot_rows(dca_rows, top_n=10),
            "recurring_pending_top": _simplify_bot_rows(recurring_rows, top_n=10),
            "grid_pending_top": _simplify_bot_rows(grid_rows, top_n=10),
        },
        "spot_pending_orders_count": len(pending_spot_orders),
        "spot_recent_fills_count": len(recent_spot_fills),
        "spot_recent_fills_top": recent_spot_fills[:10],
    }

    if not args.no_save_results:
        payload["results_file"] = _save_result(skill_root, payload)

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_build_text(payload))


if __name__ == "__main__":
    main()
