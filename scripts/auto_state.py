#!/usr/bin/env python3
import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import fcntl


DEFAULT_STATE = {
    "version": 1,
    "last_cycle": {},
    "daily": {},
    "history": [],
}


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_state():
    return json.loads(json.dumps(DEFAULT_STATE))


def load_state(path):
    p = Path(path)
    if not p.exists():
        return default_state()
    data = json.loads(p.read_text())
    out = default_state()
    out.update(data)
    return out


def save_state(path, state):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return str(p)


def compute_cycle_fingerprint(switch_payload, *, day=None):
    active_signal = switch_payload.get("active_signal", {})
    execution_checklist = switch_payload.get("execution_checklist", {})
    source = {
        "day": day or datetime.now().date().isoformat(),
        "active_profile": switch_payload.get("active_profile"),
        "target_profile": switch_payload.get("target_profile"),
        "execution_mode": execution_checklist.get("mode"),
        "latest_alloc": active_signal.get("latest_alloc", {}),
        "params_used": active_signal.get("params_used", {}),
    }
    raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def should_skip_cycle(state, fingerprint):
    last = (state or {}).get("last_cycle", {})
    if not last:
        return False
    if last.get("fingerprint") != fingerprint:
        return False
    # Block only exact repeated completed/no-op cycles.
    return last.get("status") in {"executed", "noop"}


def ensure_day_start_equity(state, day, equity_usdt):
    daily = state.setdefault("daily", {})
    if day not in daily:
        daily[day] = {
            "start_equity_usdt": float(equity_usdt),
            "updated_at": _utc_now_iso(),
        }
    return float(daily[day].get("start_equity_usdt", equity_usdt))


def day_pnl_snapshot(state, day, current_equity_usdt):
    daily = state.setdefault("daily", {})
    row = daily.get(day) or {}
    start_equity = row.get("start_equity_usdt")
    if start_equity is None or float(start_equity) <= 0:
        return {
            "day": day,
            "start_equity_usdt": None,
            "current_equity_usdt": float(current_equity_usdt),
            "pnl_usdt": None,
            "pnl_pct": None,
        }
    start_equity = float(start_equity)
    current_equity_usdt = float(current_equity_usdt)
    pnl = current_equity_usdt - start_equity
    pnl_pct = pnl / start_equity * 100
    return {
        "day": day,
        "start_equity_usdt": start_equity,
        "current_equity_usdt": current_equity_usdt,
        "pnl_usdt": round(pnl, 6),
        "pnl_pct": round(pnl_pct, 4),
    }


def record_cycle(state, *, fingerprint, status, details):
    now = _utc_now_iso()
    entry = {
        "timestamp": now,
        "fingerprint": fingerprint,
        "status": status,
        "details": details or {},
    }
    state["last_cycle"] = entry
    hist = state.setdefault("history", [])
    hist.append(entry)
    if len(hist) > 200:
        del hist[:-200]
    return entry


@contextmanager
def file_lock(lock_path, timeout_sec=10.0, poll_sec=0.1):
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o644)
    start = time.time()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if timeout_sec is not None and (time.time() - start) >= timeout_sec:
                    raise TimeoutError(f"Could not acquire lock within {timeout_sec}s: {p}")
                time.sleep(max(0.01, poll_sec))
        yield str(p)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
