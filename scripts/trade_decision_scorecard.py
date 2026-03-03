#!/usr/bin/env python3
import argparse
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from okx_auto_executor import OkxClient, _safe_float


LOCAL_TZ = timezone(timedelta(hours=8))


def _to_local_iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(LOCAL_TZ).isoformat()


def fetch_spot_fills(client, pages=3, page_size=100):
    rows = []
    after = None
    for _ in range(max(1, int(pages))):
        batch = client.get_fills_history(inst_type="SPOT", limit=page_size, after=after)
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1].get("billId")
    rows.sort(key=lambda x: int(x.get("ts", "0")))
    return rows


def _fee_to_usdt(fill, base_symbol, px):
    fee = abs(_safe_float(fill.get("fee"), 0.0))
    fee_ccy = str(fill.get("feeCcy", "")).upper()
    if fee_ccy == "USDT":
        return fee
    if fee_ccy == base_symbol and px > 0:
        return fee * px
    return 0.0


def compute_trade_metrics(fills):
    lots = defaultdict(deque)
    realized = 0.0
    win_cnt = 0
    close_cnt = 0
    fee_usdt_total = 0.0
    gross_notional = 0.0
    buy_cnt = 0
    sell_cnt = 0
    notionals = []
    holding_days = []
    per_symbol = defaultdict(lambda: {"realized": 0.0, "close_cnt": 0, "win_cnt": 0})

    usable = []
    for f in fills:
        inst = str(f.get("instId", ""))
        if "-" not in inst:
            continue
        base, quote = inst.split("-", 1)
        if quote != "USDT":
            continue
        side = str(f.get("side", "")).lower()
        px = _safe_float(f.get("fillPx") or f.get("px"), 0.0)
        sz = _safe_float(f.get("fillSz") or f.get("sz"), 0.0)
        if side not in {"buy", "sell"} or px <= 0 or sz <= 0:
            continue
        ts_ms = int(f.get("ts") or 0)
        fee_usdt = _fee_to_usdt(f, base, px)
        fee_usdt_total += fee_usdt
        notional = px * sz
        gross_notional += notional
        notionals.append(notional)
        usable.append(f)

        if side == "buy":
            buy_cnt += 1
            unit_cost = (notional + fee_usdt) / sz
            lots[base].append([sz, unit_cost, ts_ms])
            continue

        sell_cnt += 1
        qty_left = sz
        proceeds_per_unit = px - (fee_usdt / sz)
        while qty_left > 1e-12 and lots[base]:
            lqty, lcost, lts = lots[base][0]
            take = min(qty_left, lqty)
            pnl = (proceeds_per_unit - lcost) * take
            realized += pnl
            per_symbol[base]["realized"] += pnl
            close_cnt += 1
            per_symbol[base]["close_cnt"] += 1
            if pnl > 0:
                win_cnt += 1
                per_symbol[base]["win_cnt"] += 1
            if lts > 0 and ts_ms > lts:
                holding_days.append((ts_ms - lts) / 1000 / 3600 / 24)
            lqty -= take
            qty_left -= take
            if lqty <= 1e-12:
                lots[base].popleft()
            else:
                lots[base][0][0] = lqty

    start_ts = int(usable[0].get("ts") or 0) if usable else 0
    end_ts = int(usable[-1].get("ts") or 0) if usable else 0
    win_rate = (win_cnt / close_cnt) if close_cnt > 0 else None
    avg_hold_days = (sum(holding_days) / len(holding_days)) if holding_days else None
    median_notional = sorted(notionals)[len(notionals) // 2] if notionals else 0.0
    max_notional = max(notionals) if notionals else 0.0
    fee_bps = (fee_usdt_total / gross_notional * 10000) if gross_notional > 0 else None

    return {
        "fills_count": len(usable),
        "period": {
            "start_ms": start_ts,
            "end_ms": end_ts,
            "start_local": _to_local_iso(start_ts),
            "end_local": _to_local_iso(end_ts),
        },
        "buy_count": buy_cnt,
        "sell_count": sell_cnt,
        "closed_lots": close_cnt,
        "win_rate": win_rate,
        "realized_pnl_usdt": realized,
        "gross_notional_usdt": gross_notional,
        "fee_usdt_total": fee_usdt_total,
        "fee_bps": fee_bps,
        "avg_holding_days": avg_hold_days,
        "median_fill_notional_usdt": median_notional,
        "max_fill_notional_usdt": max_notional,
        "symbol_breakdown": {
            k: {
                "realized_pnl_usdt": v["realized"],
                "closed_lots": v["close_cnt"],
                "win_rate": (v["win_cnt"] / v["close_cnt"]) if v["close_cnt"] > 0 else None,
            }
            for k, v in per_symbol.items()
        },
    }


def score_metrics(metrics, equity_usdt=None):
    realized = _safe_float(metrics.get("realized_pnl_usdt"), 0.0)
    win_rate = metrics.get("win_rate")
    fee_bps = metrics.get("fee_bps")
    turnover_ratio = None
    gross_notional = _safe_float(metrics.get("gross_notional_usdt"), 0.0)
    if equity_usdt and equity_usdt > 0:
        turnover_ratio = gross_notional / equity_usdt

    if realized >= 20:
        s_profit = 40
    elif realized >= 5:
        s_profit = 32
    elif realized >= 0:
        s_profit = 26
    elif realized >= -10:
        s_profit = 18
    else:
        s_profit = 8

    if win_rate is None:
        s_win = 10
    elif win_rate >= 0.7:
        s_win = 20
    elif win_rate >= 0.55:
        s_win = 16
    elif win_rate >= 0.45:
        s_win = 12
    elif win_rate >= 0.35:
        s_win = 8
    else:
        s_win = 4

    if fee_bps is None:
        s_cost = 10
    elif fee_bps <= 5:
        s_cost = 20
    elif fee_bps <= 10:
        s_cost = 17
    elif fee_bps <= 20:
        s_cost = 13
    elif fee_bps <= 35:
        s_cost = 8
    else:
        s_cost = 4

    s_disc = 20
    if turnover_ratio is not None:
        if turnover_ratio > 8:
            s_disc -= 8
        elif turnover_ratio > 4:
            s_disc -= 5
        elif turnover_ratio > 2:
            s_disc -= 2
    med = _safe_float(metrics.get("median_fill_notional_usdt"), 0.0)
    mx = _safe_float(metrics.get("max_fill_notional_usdt"), 0.0)
    if med > 0:
        r = mx / med
        if r > 6:
            s_disc -= 4
        elif r > 3:
            s_disc -= 2
    s_disc = max(0, s_disc)

    total = s_profit + s_win + s_cost + s_disc
    if total >= 90:
        grade = "A"
    elif total >= 80:
        grade = "B"
    elif total >= 70:
        grade = "C"
    elif total >= 60:
        grade = "D"
    else:
        grade = "E"

    return {
        "total_100": total,
        "grade": grade,
        "profitability_40": s_profit,
        "winrate_20": s_win,
        "cost_20": s_cost,
        "discipline_20": s_disc,
        "turnover_vs_equity_x": turnover_ratio,
    }


def _load_latest_switch(skill_root, switch_file=None):
    if switch_file:
        p = Path(switch_file)
        return json.loads(p.read_text()), str(p)
    files = sorted((skill_root / "results").glob("switch_*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not files:
        return None, None
    p = files[0]
    return json.loads(p.read_text()), str(p)


def _compute_equity_usdt(client, balances):
    prices = {}
    equity = _safe_float(balances.get("USDT"), 0.0)
    for sym, qty in balances.items():
        if sym == "USDT":
            continue
        if _safe_float(qty) <= 0:
            continue
        tk = client.get_ticker(f"{sym}-USDT")
        px = _safe_float(tk.get("price"), 0.0)
        prices[sym] = px
        equity += _safe_float(qty) * px
    return equity, prices


def build_recommendations(metrics, score, switch_payload):
    recs = []
    if metrics["fills_count"] < 10:
        recs.append("样本量较小（<10 笔），建议继续记录后再做更强结论。")
    if metrics.get("fee_bps") is not None and metrics["fee_bps"] > 10:
        recs.append("交易成本偏高，优先降低频率并检查手续费档位。")
    if score.get("turnover_vs_equity_x") is not None and score["turnover_vs_equity_x"] > 3:
        recs.append("换手偏高，建议提高入场阈值，减少微小仓位交易。")
    med = _safe_float(metrics.get("median_fill_notional_usdt"), 0.0)
    mx = _safe_float(metrics.get("max_fill_notional_usdt"), 0.0)
    if med > 0 and mx / med > 4:
        recs.append("单笔规模离散较大，建议统一分批规则，避免情绪化加仓。")

    if switch_payload:
        mode = switch_payload.get("execution_checklist", {}).get("mode")
        alloc = switch_payload.get("active_signal", {}).get("latest_alloc", {})
        usdt_w = _safe_float(alloc.get("USDT"), 0.0)
        if mode == "hold_cash" and usdt_w >= 0.99:
            recs.append("当前策略为防守态（100% USDT），继续等待非 USDT 信号再开仓。")
        elif mode == "deploy":
            recs.append("当前策略允许部署风险仓，可按分批计划执行并跟踪滑点。")
    if not recs:
        recs.append("执行质量稳定，维持当前纪律并持续跟踪。")
    return recs


def _markdown_report(payload):
    m = payload["trade_stats"]
    s = payload["score"]
    lines = []
    lines.append(f"# 交易决策评分卡 ({payload['generated_at_local']})")
    lines.append("")
    lines.append(f"- 统计区间: {payload['period_local']['start']} ~ {payload['period_local']['end']}")
    lines.append(f"- 成交笔数: {payload['fills_count']} (买 {m['buy_count']} / 卖 {m['sell_count']})")
    lines.append(f"- 已实现收益: {m['realized_pnl_usdt']} USDT")
    lines.append(f"- 总分: {s['total_100']}/100 (等级 {s['grade']})")
    lines.append("")
    lines.append("## 评分明细")
    lines.append(f"- 收益质量: {s['profitability_40']}/40")
    lines.append(f"- 胜率表现: {s['winrate_20']}/20")
    lines.append(f"- 成本控制: {s['cost_20']}/20")
    lines.append(f"- 交易纪律: {s['discipline_20']}/20")
    lines.append("")
    lines.append("## 关键指标")
    lines.append(f"- 胜率: {m['win_rate']}")
    lines.append(f"- 手续费: {m['fee_usdt_total']} USDT ({m['fee_bps']} bps)")
    lines.append(f"- 平均持有天数: {m['avg_holding_days']}")
    lines.append(f"- 换手/权益: {s['turnover_vs_equity_x']}")
    lines.append("")
    lines.append("## 策略上下文")
    lines.append(f"- active_profile: {payload['strategy_context']['active_profile']}")
    lines.append(f"- execution_mode: {payload['strategy_context']['execution_mode']}")
    lines.append(f"- latest_alloc: {payload['strategy_context']['latest_alloc']}")
    lines.append("")
    lines.append("## 建议动作")
    for r in payload["recommendations"]:
        lines.append(f"- {r}")
    lines.append("")
    lines.append("## 最近成交")
    for row in payload["recent_fills"]:
        lines.append(
            f"- {row['ts_local']} | {row['instId']} | {row['side']} | px={row['fillPx']} | sz={row['fillSz']} | fee={row['fee']} {row['feeCcy']}"
        )
    return "\n".join(lines) + "\n"


def _save_files(skill_root, payload, write_json=True, write_md=True):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = skill_root / "results"
    results.mkdir(parents=True, exist_ok=True)
    out = {}
    if write_json:
        p = results / f"decision_scorecard_{ts}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        out["json"] = str(p)
    if write_md:
        p = results / f"decision_scorecard_{ts}.md"
        p.write_text(_markdown_report(payload))
        out["md"] = str(p)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=3, help="How many API pages to pull (100 fills per page).")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--switch-file", type=str, default=None)
    p.add_argument("--equity-usdt", type=float, default=None)
    p.add_argument("--format", choices=["json", "md", "both"], default="both")
    args = p.parse_args()

    api_key = os.environ.get("OKX_API_KEY", "").strip()
    api_secret = os.environ.get("OKX_API_SECRET", "").strip()
    api_passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    if not api_key or not api_secret or not api_passphrase:
        raise SystemExit("Missing OKX API env vars: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE")

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    client = OkxClient(api_key=api_key, api_secret=api_secret, passphrase=api_passphrase)

    fills = fetch_spot_fills(client, pages=args.pages, page_size=args.page_size)
    metrics = compute_trade_metrics(fills)

    balances = client.get_spot_balances()
    equity_usdt = args.equity_usdt
    equity_source = "arg"
    if equity_usdt is None:
        equity_usdt, _ = _compute_equity_usdt(client, balances)
        equity_source = "okx_balance_ticker"

    score = score_metrics(metrics, equity_usdt=equity_usdt)

    switch_payload, switch_path = _load_latest_switch(skill_root, args.switch_file)
    strategy_context = {
        "switch_file": switch_path,
        "active_profile": switch_payload.get("active_profile") if switch_payload else None,
        "execution_mode": switch_payload.get("execution_checklist", {}).get("mode") if switch_payload else None,
        "latest_alloc": switch_payload.get("active_signal", {}).get("latest_alloc") if switch_payload else None,
        "risk_features": switch_payload.get("risk_features") if switch_payload else None,
    }

    recs = build_recommendations(metrics, score, switch_payload)
    recent = []
    for f in fills[-10:]:
        recent.append(
            {
                "ts_local": _to_local_iso(int(f.get("ts") or 0)),
                "instId": f.get("instId"),
                "side": f.get("side"),
                "fillPx": f.get("fillPx"),
                "fillSz": f.get("fillSz"),
                "fee": f.get("fee"),
                "feeCcy": f.get("feeCcy"),
            }
        )

    payload = {
        "generated_at_local": datetime.now(tz=LOCAL_TZ).isoformat(),
        "fills_count": metrics["fills_count"],
        "period_local": {
            "start": metrics["period"]["start_local"],
            "end": metrics["period"]["end_local"],
        },
        "trade_stats": {
            "buy_count": metrics["buy_count"],
            "sell_count": metrics["sell_count"],
            "closed_lots": metrics["closed_lots"],
            "win_rate": round(metrics["win_rate"], 4) if metrics["win_rate"] is not None else None,
            "realized_pnl_usdt": round(metrics["realized_pnl_usdt"], 4),
            "gross_notional_usdt": round(metrics["gross_notional_usdt"], 4),
            "fee_usdt_total": round(metrics["fee_usdt_total"], 6),
            "fee_bps": round(metrics["fee_bps"], 2) if metrics["fee_bps"] is not None else None,
            "avg_holding_days": round(metrics["avg_holding_days"], 2) if metrics["avg_holding_days"] is not None else None,
            "max_fill_notional_usdt": round(metrics["max_fill_notional_usdt"], 4),
            "median_fill_notional_usdt": round(metrics["median_fill_notional_usdt"], 4),
            "equity_usdt_reference": round(equity_usdt, 6) if equity_usdt is not None else None,
            "equity_source": equity_source,
        },
        "score": {
            "total_100": score["total_100"],
            "grade": score["grade"],
            "profitability_40": score["profitability_40"],
            "winrate_20": score["winrate_20"],
            "cost_20": score["cost_20"],
            "discipline_20": score["discipline_20"],
            "turnover_vs_equity_x": round(score["turnover_vs_equity_x"], 3)
            if score["turnover_vs_equity_x"] is not None
            else None,
        },
        "symbol_breakdown": {
            k: {
                "realized_pnl_usdt": round(v["realized_pnl_usdt"], 4),
                "closed_lots": v["closed_lots"],
                "win_rate": round(v["win_rate"], 4) if v["win_rate"] is not None else None,
            }
            for k, v in metrics["symbol_breakdown"].items()
        },
        "strategy_context": strategy_context,
        "recommendations": recs,
        "recent_fills": recent,
    }

    files = _save_files(
        skill_root,
        payload,
        write_json=args.format in {"json", "both"},
        write_md=args.format in {"md", "both"},
    )
    payload["results_files"] = files
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
