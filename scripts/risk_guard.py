#!/usr/bin/env python3
import os
from pathlib import Path


def env_flag(name, default=True):
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _risk_exposure_pct(target_alloc):
    out = 0.0
    for k, v in (target_alloc or {}).items():
        if str(k).upper() == "USDT":
            continue
        out += float(v)
    return out * 100


def _total_order_usdt(plan):
    total = 0.0
    for od in (plan or {}).get("orders", []):
        total += float(od.get("notional_usdt", 0.0))
    return total


def evaluate_trade_guards(
    *,
    live,
    target_alloc,
    plan,
    price_errors_count=0,
    day_pnl_pct=None,
    kill_switch_file=None,
    max_risk_exposure_pct=95.0,
    max_total_order_usdt=5000.0,
    max_order_count=20,
    max_price_errors=0,
    max_daily_loss_pct=3.0,
):
    reasons = []
    exposure_pct = _risk_exposure_pct(target_alloc)
    order_total = _total_order_usdt(plan)
    order_count = len((plan or {}).get("orders", []))

    auto_enabled = env_flag("AUTO_TRADING_ENABLED", default=True)
    if live and not auto_enabled:
        reasons.append("AUTO_TRADING_ENABLED is false")

    if live and kill_switch_file and Path(kill_switch_file).exists():
        reasons.append(f"kill switch exists: {kill_switch_file}")

    if exposure_pct > float(max_risk_exposure_pct):
        reasons.append(
            f"risk exposure {exposure_pct:.2f}% exceeds max {float(max_risk_exposure_pct):.2f}%"
        )

    if order_total > float(max_total_order_usdt):
        reasons.append(
            f"total order notional {order_total:.2f} exceeds max {float(max_total_order_usdt):.2f}"
        )

    if order_count > int(max_order_count):
        reasons.append(f"order count {order_count} exceeds max {int(max_order_count)}")

    if int(price_errors_count) > int(max_price_errors):
        reasons.append(f"price errors {int(price_errors_count)} exceeds max {int(max_price_errors)}")

    if day_pnl_pct is not None and float(day_pnl_pct) <= -abs(float(max_daily_loss_pct)):
        reasons.append(
            f"daily pnl {float(day_pnl_pct):.2f}% breached loss limit {-abs(float(max_daily_loss_pct)):.2f}%"
        )

    return {
        "ok": len(reasons) == 0,
        "reasons": reasons,
        "metrics": {
            "risk_exposure_pct": round(exposure_pct, 4),
            "total_order_notional_usdt": round(order_total, 6),
            "order_count": order_count,
            "price_errors_count": int(price_errors_count),
            "day_pnl_pct": None if day_pnl_pct is None else round(float(day_pnl_pct), 4),
            "auto_trading_enabled": bool(auto_enabled),
            "live_mode": bool(live),
        },
    }


def summarize_execution(execution_rows):
    counts = {}
    for row in execution_rows or []:
        k = str(row.get("status", "UNKNOWN")).upper()
        counts[k] = counts.get(k, 0) + 1
    return counts
