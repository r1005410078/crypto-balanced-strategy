#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_state import (  # noqa: E402
    compute_cycle_fingerprint,
    day_pnl_snapshot,
    ensure_day_start_equity,
    file_lock,
    load_state,
    record_cycle,
    save_state,
    should_skip_cycle,
)


class AutoStateTests(unittest.TestCase):
    def test_load_default_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "missing.json"
            st = load_state(p)
            self.assertEqual(st["version"], 1)
            self.assertIn("last_cycle", st)

    def test_record_and_skip_duplicate(self):
        switch_payload = {
            "active_profile": "stable",
            "target_profile": "stable",
            "active_signal": {"latest_alloc": {"USDT": 1.0}, "params_used": {"k": 1}},
            "execution_checklist": {"mode": "hold_cash"},
        }
        fp = compute_cycle_fingerprint(switch_payload, day="2026-03-03")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            st = load_state(p)
            record_cycle(st, fingerprint=fp, status="executed", details={})
            self.assertTrue(should_skip_cycle(st, fp))

    def test_day_equity_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            st = load_state(p)
            start = ensure_day_start_equity(st, "2026-03-03", 1000.0)
            self.assertAlmostEqual(start, 1000.0, places=6)
            snap = day_pnl_snapshot(st, "2026-03-03", 970.0)
            self.assertAlmostEqual(snap["pnl_usdt"], -30.0, places=6)
            self.assertAlmostEqual(snap["pnl_pct"], -3.0, places=6)

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "auto_state.json"
            st = load_state(p)
            st["daily"]["2026-03-03"] = {"start_equity_usdt": 1234.5}
            save_state(p, st)
            st2 = load_state(p)
            self.assertAlmostEqual(st2["daily"]["2026-03-03"]["start_equity_usdt"], 1234.5, places=6)

    def test_file_lock_context(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "lockfile.lock"
            with file_lock(lock_path, timeout_sec=1.0):
                self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
