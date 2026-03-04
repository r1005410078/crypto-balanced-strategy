#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from okx_auto_executor import OkxClient, _safe_float


DEFAULT_OKX_BOT_URL = "https://www.okx.com/en-us/trading-bot"


def _http_get_text(url, timeout_sec=20, user_agent=None):
    req = Request(url, headers={"User-Agent": user_agent or "Mozilla/5.0"})
    with urlopen(req, timeout=float(timeout_sec)) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_json_script(html, script_id):
    pattern = rf'<script[^>]+id="{re.escape(script_id)}"[^>]*>(.*?)</script>'
    m = re.search(pattern, html, flags=re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    if not raw:
        return None
    return json.loads(raw)


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_strategy_categories(app_state):
    ctx = ((app_state or {}).get("appContext") or {}).get("initialProps") or {}
    cats = (((ctx.get("topTabData") or {}).get("strategyCategories")) or {})
    out = []
    for strategy_type, row in cats.items():
        if not isinstance(row, dict):
            continue
        inst_types = [x.strip().upper() for x in str(row.get("instTypeList", "")).split(",") if x.strip()]
        out.append(
            {
                "strategy_type": str(row.get("strategyType") or strategy_type),
                "category": str(row.get("category", "")),
                "inst_types": sorted(set(inst_types)),
                "mp_enabled": str(row.get("mpEnabled", "0")) == "1",
                "optimal_value": _to_float(row.get("optimalValue", 0.0)),
                "user_count": _to_int(row.get("userCount", 0)),
                "stage": str(row.get("stage", "")),
            }
        )
    return out


def _risk_level(inst_types):
    s = set(inst_types or [])
    if {"SWAP", "FUTURES", "OPTION"} & s:
        return "high"
    if "MARGIN" in s:
        return "medium"
    return "low"


def _score_strategy(row, *, allow_derivatives=False):
    risk = _risk_level(row.get("inst_types", []))
    if (not allow_derivatives) and risk == "high":
        return None
    if row.get("stage") and row["stage"] != "online":
        return None
    if row.get("strategy_type") in {"signal_bot", "smart_iceberg", "twap"}:
        return None

    pop = math.log1p(max(0, int(row.get("user_count", 0))))
    perf = _to_float(row.get("optimal_value", 0.0))
    perf_score = math.tanh(perf / 20.0)
    mp_bonus = 0.4 if row.get("mp_enabled") else -0.2

    if risk == "low":
        risk_adj = 0.7
    elif risk == "medium":
        risk_adj = 0.2
    else:
        risk_adj = -0.8

    st = str(row.get("strategy_type", ""))
    style_bonus = {
        "recurring": 0.45,
        "spot_dca": 0.4,
        "smart_portfolio": 0.35,
        "grid": 0.2,
        "dcd_bot": 0.1,
        "contract_dca": -0.5,
        "contract_grid": -0.8,
        "arbitrage": -0.4,
        "smart_arbitrage": -0.5,
    }.get(st, 0.0)

    score = pop + perf_score + mp_bonus + risk_adj + style_bonus
    out = dict(row)
    out["risk_level"] = risk
    out["score"] = round(score, 6)
    return out


def _rank_strategies(rows, *, allow_derivatives=False):
    scored = []
    for row in rows:
        s = _score_strategy(row, allow_derivatives=allow_derivatives)
        if s is not None:
            scored.append(s)
    scored.sort(key=lambda x: (x["score"], x["user_count"]), reverse=True)
    return scored


def _build_param_template(strategy_type, allocation_usdt):
    amt = round(float(allocation_usdt), 2)
    if strategy_type == "recurring":
        return {
            "quote_ccy": "USDT",
            "frequency": "daily",
            "amount_per_cycle_usdt": round(max(5.0, amt / 7.0), 2),
            "horizon_days": 30,
            "max_total_usdt": amt,
        }
    if strategy_type == "spot_dca":
        return {
            "quote_ccy": "USDT",
            "entry_mode": "signal_or_pullback",
            "base_order_usdt": round(max(10.0, amt * 0.25), 2),
            "safety_order_count": 3,
            "safety_order_step_pct": 2.0,
            "take_profit_pct": 6.0,
            "stop_loss_pct": 7.0,
            "max_total_usdt": amt,
        }
    if strategy_type == "smart_portfolio":
        return {
            "quote_ccy": "USDT",
            "rebalance_mode": "weekly",
            "constituents": ["BTC", "ETH"],
            "weights": [0.7, 0.3],
            "max_total_usdt": amt,
        }
    if strategy_type == "grid":
        return {
            "quote_ccy": "USDT",
            "symbol": "BTC-USDT",
            "grid_count": 12,
            "lower_range_pct": 10.0,
            "upper_range_pct": 10.0,
            "take_profit_mode": "grid_exit",
            "max_total_usdt": amt,
        }
    return {
        "quote_ccy": "USDT",
        "max_total_usdt": amt,
        "note": "Use conservative defaults and strict stop-loss.",
    }


def _normalize_weights(scores):
    if not scores:
        return []
    vals = [max(0.01, float(x.get("score", 0.0))) for x in scores]
    total = sum(vals)
    return [v / total for v in vals]


def _load_main_strategy_gate(skill_root):
    switcher = Path(skill_root) / "scripts" / "profile_switcher.py"
    cmd = [
        sys.executable,
        str(switcher),
        "--capital-cny",
        "10000",
        "--confirmations",
        "2",
        "--check-window",
        "120",
        "--signal-window",
        "365",
    ]
    raw = subprocess.check_output(cmd, text=True)
    payload = json.loads(raw)
    mode = str(((payload.get("execution_checklist") or {}).get("mode")) or "")
    risk_rising = bool(payload.get("risk_rising_used", False))
    return {
        "mode": mode,
        "risk_rising_used": risk_rising,
        "active_profile": payload.get("active_profile"),
        "target_profile": payload.get("target_profile"),
        "switch_results_file": payload.get("results_file"),
    }


def _load_total_usdt_live():
    api_key = os.environ.get("OKX_API_KEY", "").strip()
    api_secret = os.environ.get("OKX_API_SECRET", "").strip()
    api_passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    if not api_key or not api_secret or not api_passphrase:
        return None
    client = OkxClient(api_key=api_key, api_secret=api_secret, passphrase=api_passphrase)
    spot = client.get_spot_balances()
    funding = client.get_funding_balances(ccy="USDT")
    return _safe_float(spot.get("USDT", 0.0)) + _safe_float(funding.get("USDT", 0.0))


def _compute_budget(
    *,
    total_usdt,
    main_gate,
    default_ratio,
    max_budget_usdt,
    sandbox_usdt,
):
    total = max(0.0, float(total_usdt or 0.0))
    ratio = max(0.0, float(default_ratio))
    hold_cash = (main_gate.get("mode") == "hold_cash") or bool(main_gate.get("risk_rising_used"))
    if hold_cash:
        ratio = 0.0
    budget = total * ratio
    if max_budget_usdt is not None:
        budget = min(budget, max(0.0, float(max_budget_usdt)))
    if sandbox_usdt is not None:
        budget = max(budget, max(0.0, float(sandbox_usdt)))
    return round(budget, 4), hold_cash


def _save_result(skill_root, payload):
    p = Path(skill_root) / "results"
    p.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = p / f"hot_strategy_advice_{ts}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(out)


def _build_text(payload):
    lines = []
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"Source: {payload['source_url']}")
    g = payload["main_strategy_gate"]
    lines.append(
        f"MainGate: mode={g['mode']} risk_rising={g['risk_rising_used']} active={g['active_profile']} target={g['target_profile']}"
    )
    b = payload["budget"]
    lines.append(
        f"Budget: total_usdt={b['total_usdt']:.4f} ratio={b['default_ratio']} recommended={b['recommended_budget_usdt']:.4f} hold_cash_block={b['hold_cash_block']}"
    )
    lines.append("Candidates:")
    for i, row in enumerate(payload["selected"], start=1):
        lines.append(
            f"{i}. {row['strategy_type']} score={row['score']:.4f} users={row['user_count']} risk={row['risk_level']} alloc={row['allocation_usdt']:.2f}"
        )
    if not payload["selected"]:
        lines.append("- none")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(
        description="Auto-select hot OKX strategy types and build conservative parameter templates."
    )
    p.add_argument("--source-url", type=str, default=DEFAULT_OKX_BOT_URL)
    p.add_argument("--top-n", type=int, default=3)
    p.add_argument("--allow-derivatives", action="store_true")
    p.add_argument("--default-ratio", type=float, default=0.05, help="Budget ratio vs total USDT when gate allows.")
    p.add_argument("--max-budget-usdt", type=float, default=140.0)
    p.add_argument("--sandbox-usdt", type=float, default=0.0, help="Minimum small test budget even in hold_cash mode.")
    p.add_argument("--total-usdt", type=float, default=None, help="Manual total USDT override.")
    p.add_argument("--skip-main-gate", action="store_true")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--no-save-results", action="store_true")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent

    html = _http_get_text(args.source_url)
    app_state = _extract_json_script(html, "appState")
    if app_state is None:
        raise SystemExit("Failed to parse appState from source page.")
    parsed = _parse_strategy_categories(app_state)
    ranked = _rank_strategies(parsed, allow_derivatives=args.allow_derivatives)

    gate = {
        "mode": "unknown",
        "risk_rising_used": None,
        "active_profile": None,
        "target_profile": None,
        "switch_results_file": None,
    }
    if not args.skip_main_gate:
        gate = _load_main_strategy_gate(skill_root)

    total_usdt = args.total_usdt
    if total_usdt is None:
        total_usdt = _load_total_usdt_live()
    if total_usdt is None:
        total_usdt = 0.0

    budget_usdt, hold_cash_block = _compute_budget(
        total_usdt=total_usdt,
        main_gate=gate,
        default_ratio=args.default_ratio,
        max_budget_usdt=args.max_budget_usdt,
        sandbox_usdt=args.sandbox_usdt,
    )

    selected = ranked[: max(0, int(args.top_n))]
    weights = _normalize_weights(selected)
    out_rows = []
    for row, w in zip(selected, weights):
        alloc = round(budget_usdt * w, 2)
        r = dict(row)
        r["allocation_usdt"] = alloc
        r["params_template"] = _build_param_template(row["strategy_type"], alloc)
        out_rows.append(r)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "source_url": args.source_url,
        "source_note": "Parsed from OKX trading-bot page appState (public SSR data).",
        "main_strategy_gate": gate,
        "budget": {
            "total_usdt": round(float(total_usdt), 8),
            "default_ratio": float(args.default_ratio),
            "max_budget_usdt": float(args.max_budget_usdt) if args.max_budget_usdt is not None else None,
            "sandbox_usdt": float(args.sandbox_usdt) if args.sandbox_usdt is not None else None,
            "hold_cash_block": bool(hold_cash_block),
            "recommended_budget_usdt": round(float(budget_usdt), 8),
        },
        "selected": out_rows,
        "all_ranked_count": len(ranked),
        "all_ranked": ranked,
    }
    if not args.no_save_results:
        payload["results_file"] = _save_result(skill_root, payload)

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_build_text(payload))


if __name__ == "__main__":
    main()
