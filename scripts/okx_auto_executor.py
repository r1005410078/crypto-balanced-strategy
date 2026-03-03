#!/usr/bin/env python3
"""
Execute strategy allocation on OKX spot account.

Safety defaults:
- Dry-run by default (no live orders unless --live is passed)
- Per-order notional cap
- Min order threshold
- Spread guard before placing orders
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class OkxApiError(RuntimeError):
    pass


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_float(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _format_num(v, decimals=8) -> str:
    s = f"{v:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _to_base_symbol(sym: str) -> str:
    s = str(sym).upper()
    if s != "USDT" and s.endswith("USDT") and len(s) > 4:
        return s[:-4]
    return s


def _to_inst_id(base_sym: str) -> str:
    return f"{base_sym}-USDT"


def normalize_target_alloc(latest_alloc: dict) -> dict:
    out = {}
    for raw_sym, w in (latest_alloc or {}).items():
        sym = _to_base_symbol(raw_sym)
        out[sym] = out.get(sym, 0.0) + max(0.0, _safe_float(w))
    total = sum(out.values())
    if total <= 0:
        return {"USDT": 1.0}
    return {k: v / total for k, v in out.items()}


def _build_switch_cmd(args, switcher_path: Path):
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


@dataclass
class RebalanceOrder:
    symbol: str
    inst_id: str
    side: str
    notional_usdt: float
    size: float
    spread_bps: float

    def as_dict(self):
        return {
            "symbol": self.symbol,
            "inst_id": self.inst_id,
            "side": self.side,
            "notional_usdt": round(self.notional_usdt, 4),
            "size": round(self.size, 8),
            "spread_bps": round(self.spread_bps, 2),
        }


def build_rebalance_plan(
    *,
    target_weights: dict,
    balances: dict,
    prices: dict,
    spreads_bps: dict,
    min_order_usdt: float,
    max_order_usdt: float,
    max_spread_bps: float,
    allow_buy: bool = True,
    allow_sell: bool = True,
):
    symbols = sorted(set(target_weights.keys()) | set(balances.keys()))
    if "USDT" not in symbols:
        symbols.append("USDT")

    current_values = {}
    equity = 0.0
    for sym in symbols:
        qty = _safe_float(balances.get(sym, 0.0))
        if sym == "USDT":
            v = qty
        else:
            px = _safe_float(prices.get(sym, 0.0))
            if px <= 0:
                v = 0.0
            else:
                v = qty * px
        current_values[sym] = v
        equity += v

    if equity <= 0:
        return {
            "equity_usdt": 0.0,
            "current_values_usdt": current_values,
            "target_values_usdt": {},
            "diffs_usdt": {},
            "orders": [],
            "skipped": [{"reason": "no_equity"}],
        }

    target_values = {}
    diffs = {}
    for sym in symbols:
        tw = _safe_float(target_weights.get(sym, 0.0))
        tv = tw * equity
        target_values[sym] = tv
        diffs[sym] = tv - current_values.get(sym, 0.0)

    sell_candidates = []
    for sym in symbols:
        if sym == "USDT":
            continue
        diff = _safe_float(diffs.get(sym, 0.0))
        if diff >= -min_order_usdt:
            continue
        sell_candidates.append((sym, abs(diff)))
    sell_candidates.sort(key=lambda x: x[1], reverse=True)

    buy_candidates = []
    for sym in symbols:
        if sym == "USDT":
            continue
        diff = _safe_float(diffs.get(sym, 0.0))
        if diff <= min_order_usdt:
            continue
        buy_candidates.append((sym, diff))
    buy_candidates.sort(key=lambda x: x[1], reverse=True)

    orders = []
    skipped = []
    planned_sell_usdt = 0.0

    for sym, needed in sell_candidates:
        if not allow_sell:
            skipped.append({"symbol": sym, "side": "sell", "reason": "sell_disabled"})
            continue
        px = _safe_float(prices.get(sym, 0.0))
        qty = _safe_float(balances.get(sym, 0.0))
        if px <= 0 or qty <= 0:
            skipped.append({"symbol": sym, "side": "sell", "reason": "missing_price_or_qty"})
            continue
        spread = _safe_float(spreads_bps.get(sym, 0.0))
        if spread > max_spread_bps:
            skipped.append(
                {
                    "symbol": sym,
                    "side": "sell",
                    "reason": "spread_too_wide",
                    "spread_bps": round(spread, 2),
                }
            )
            continue

        notional = min(needed, max_order_usdt, qty * px)
        if notional < min_order_usdt:
            skipped.append({"symbol": sym, "side": "sell", "reason": "below_min_order"})
            continue
        size = _clip(notional / px, 0.0, qty)
        real_notional = size * px
        if real_notional < min_order_usdt:
            skipped.append({"symbol": sym, "side": "sell", "reason": "below_min_after_round"})
            continue

        orders.append(
            RebalanceOrder(
                symbol=sym,
                inst_id=_to_inst_id(sym),
                side="sell",
                notional_usdt=real_notional,
                size=size,
                spread_bps=spread,
            )
        )
        planned_sell_usdt += real_notional

    available_usdt = _safe_float(balances.get("USDT", 0.0)) + planned_sell_usdt
    for sym, needed in buy_candidates:
        if not allow_buy:
            skipped.append({"symbol": sym, "side": "buy", "reason": "buy_disabled"})
            continue
        px = _safe_float(prices.get(sym, 0.0))
        if px <= 0:
            skipped.append({"symbol": sym, "side": "buy", "reason": "missing_price"})
            continue
        spread = _safe_float(spreads_bps.get(sym, 0.0))
        if spread > max_spread_bps:
            skipped.append(
                {
                    "symbol": sym,
                    "side": "buy",
                    "reason": "spread_too_wide",
                    "spread_bps": round(spread, 2),
                }
            )
            continue

        notional = min(needed, max_order_usdt, available_usdt)
        if notional < min_order_usdt:
            skipped.append({"symbol": sym, "side": "buy", "reason": "insufficient_usdt_or_too_small"})
            continue

        size = notional  # OKX market buy with tgtCcy=quote_ccy uses quote amount.
        orders.append(
            RebalanceOrder(
                symbol=sym,
                inst_id=_to_inst_id(sym),
                side="buy",
                notional_usdt=notional,
                size=size,
                spread_bps=spread,
            )
        )
        available_usdt -= notional

    current_weights = {k: (v / equity if equity > 0 else 0.0) for k, v in current_values.items()}
    target_weights_out = {k: _safe_float(v, 0.0) for k, v in target_weights.items()}
    return {
        "equity_usdt": round(equity, 6),
        "current_values_usdt": {k: round(v, 6) for k, v in current_values.items()},
        "target_values_usdt": {k: round(v, 6) for k, v in target_values.items()},
        "diffs_usdt": {k: round(v, 6) for k, v in diffs.items()},
        "current_weights": {k: round(v, 6) for k, v in current_weights.items()},
        "target_weights": {k: round(v, 6) for k, v in target_weights_out.items()},
        "orders": [o.as_dict() for o in orders],
        "skipped": skipped,
    }


class OkxClient:
    def __init__(
        self,
        api_key,
        api_secret,
        passphrase,
        *,
        demo=False,
        timeout=15,
        base_url="https://www.okx.com",
        user_agent=None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.demo = bool(demo)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.user_agent = (
            user_agent
            or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )

    def _sign(self, ts: str, method: str, request_path: str, body: str) -> str:
        prehash = f"{ts}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _request(self, method: str, path: str, *, params=None, payload=None, auth=False):
        params = params or {}
        payload = payload or {}
        qs = urlencode(params)
        request_path = path if not qs else f"{path}?{qs}"
        url = f"{self.base_url}{request_path}"
        body = json.dumps(payload, separators=(",", ":")) if method.upper() != "GET" else ""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if auth:
            ts = _utc_ts()
            headers["OK-ACCESS-KEY"] = self.api_key
            headers["OK-ACCESS-PASSPHRASE"] = self.passphrase
            headers["OK-ACCESS-TIMESTAMP"] = ts
            headers["OK-ACCESS-SIGN"] = self._sign(ts, method, request_path, body)
            if self.demo:
                headers["x-simulated-trading"] = "1"

        req = Request(
            url=url,
            data=(body.encode("utf-8") if method.upper() != "GET" else None),
            headers=headers,
            method=method.upper(),
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as e:
            raise OkxApiError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')}") from e
        except URLError as e:
            raise OkxApiError(f"Network error: {e}") from e

        data = json.loads(raw)
        if str(data.get("code", "0")) != "0":
            raise OkxApiError(f"OKX API error code={data.get('code')} msg={data.get('msg')} data={data.get('data')}")
        return data.get("data", [])

    def get_spot_balances(self) -> dict:
        rows = self._request("GET", "/api/v5/account/balance", auth=True)
        out = {}
        for r in rows:
            for d in r.get("details", []):
                ccy = str(d.get("ccy", "")).upper()
                if not ccy:
                    continue
                avail = _safe_float(d.get("availBal"), 0.0)
                if avail <= 0:
                    avail = _safe_float(d.get("cashBal"), 0.0)
                if avail <= 0:
                    continue
                out[ccy] = out.get(ccy, 0.0) + avail
        return out

    def get_ticker(self, inst_id: str) -> dict:
        rows = self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id}, auth=False)
        if not rows:
            raise OkxApiError(f"Empty ticker for {inst_id}")
        r = rows[0]
        bid = _safe_float(r.get("bidPx"), 0.0)
        ask = _safe_float(r.get("askPx"), 0.0)
        last = _safe_float(r.get("last"), 0.0)
        px = last if last > 0 else ((bid + ask) / 2 if bid > 0 and ask > 0 else 0.0)
        spread_bps = 0.0
        if bid > 0 and ask > 0 and ask >= bid:
            mid = (ask + bid) / 2
            if mid > 0:
                spread_bps = (ask - bid) / mid * 10000
        return {
            "inst_id": inst_id,
            "price": px,
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
        }

    def place_market_order(self, *, inst_id: str, side: str, size: float, cl_ord_id: str):
        payload = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "market",
            "sz": _format_num(size, 8),
            "clOrdId": cl_ord_id[:32],
        }
        if side == "buy":
            payload["tgtCcy"] = "quote_ccy"
        rows = self._request("POST", "/api/v5/trade/order", payload=payload, auth=True)
        return rows[0] if rows else {"result": "ok"}

    def get_fills_history(self, *, inst_type="SPOT", limit=100, after=None):
        params = {
            "instType": str(inst_type).upper(),
            "limit": str(int(limit)),
        }
        if after:
            params["after"] = str(after)
        return self._request("GET", "/api/v5/trade/fills-history", params=params, auth=True)


def _load_or_run_switch_payload(args, script_dir: Path):
    if args.switch_file:
        return json.loads(Path(args.switch_file).read_text()), f"file:{args.switch_file}"
    switcher_path = script_dir / "profile_switcher.py"
    cmd = _build_switch_cmd(args, switcher_path)
    raw = subprocess.check_output(cmd, text=True)
    return json.loads(raw), " ".join(cmd)


def _save_exec_result(skill_root: Path, payload: dict) -> str:
    results_dir = skill_root / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"okx_exec_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--switch-file", type=str, default=None, help="Use existing switch_*.json instead of running switcher.")
    p.add_argument("--live", action="store_true", help="Place live orders. Default is dry-run.")
    p.add_argument("--demo", action="store_true", help="Use OKX simulated trading header.")
    p.add_argument("--min-order-usdt", type=float, default=10.0)
    p.add_argument("--max-order-usdt", type=float, default=1000.0)
    p.add_argument("--max-spread-bps", type=float, default=20.0)
    p.add_argument("--allow-buy", action="store_true", default=False, help="Allow buy orders. Disabled by default for safety.")
    p.add_argument("--allow-sell", action="store_true", default=False, help="Allow sell orders. Disabled by default for safety.")
    p.add_argument("--no-save-results", action="store_true")
    p.add_argument("--base-url", type=str, default="https://www.okx.com")
    p.add_argument(
        "--user-agent",
        type=str,
        default=None,
        help="Optional custom User-Agent for HTTP requests.",
    )

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
    p.add_argument("--state-file", type=str, default=None)
    p.add_argument("--no-save-state", action="store_true")
    p.add_argument("--no-save-switch-results", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    if not args.allow_buy and not args.allow_sell:
        raise SystemExit("Refusing to run: set --allow-sell and/or --allow-buy explicitly.")

    api_key = os.environ.get("OKX_API_KEY", "").strip()
    api_secret = os.environ.get("OKX_API_SECRET", "").strip()
    api_passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    if not api_key or not api_secret or not api_passphrase:
        raise SystemExit("Missing OKX API env vars: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE")

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    switch_payload, switch_source = _load_or_run_switch_payload(args, script_dir)

    target_alloc = normalize_target_alloc(switch_payload["active_signal"]["latest_alloc"])
    client = OkxClient(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=api_passphrase,
        demo=args.demo,
        base_url=args.base_url,
        user_agent=args.user_agent,
    )
    balances = client.get_spot_balances()

    symbols_for_price = sorted({k for k in target_alloc.keys() if k != "USDT"} | {k for k in balances.keys() if k != "USDT"})
    prices = {}
    spreads = {}
    price_errors = []
    for sym in symbols_for_price:
        inst = _to_inst_id(sym)
        try:
            tk = client.get_ticker(inst)
            prices[sym] = _safe_float(tk["price"], 0.0)
            spreads[sym] = _safe_float(tk["spread_bps"], 0.0)
        except Exception as e:  # network/exchange error is recorded and the symbol is skipped.
            price_errors.append({"symbol": sym, "inst_id": inst, "error": str(e)})

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

    execution = []
    dry_run = not args.live
    for i, od in enumerate(plan["orders"], start=1):
        side = od["side"]
        cl_ord_id = f"cbs{int(datetime.now().timestamp())}{i:02d}"
        if dry_run:
            execution.append(
                {
                    "status": "DRY_RUN",
                    "order": od,
                }
            )
            continue
        try:
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

    out = {
        "generated_at": datetime.now().isoformat(),
        "mode": "LIVE" if args.live else "DRY_RUN",
        "switch_source": switch_source,
        "active_profile": switch_payload.get("active_profile"),
        "execution_mode": switch_payload.get("execution_checklist", {}).get("mode"),
        "target_alloc": target_alloc,
        "balances": balances,
        "prices": prices,
        "spreads_bps": {k: round(v, 4) for k, v in spreads.items()},
        "price_errors": price_errors,
        "plan": plan,
        "execution": execution,
    }
    if not args.no_save_results:
        out["results_file"] = _save_exec_result(skill_root, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
