#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from health_check_dryrun import _to_base_symbol, build_health_summary  # noqa: E402


class HealthCheckDryRunTests(unittest.TestCase):
    def test_to_base_symbol(self):
        self.assertEqual(_to_base_symbol("BTCUSDT"), "BTC")
        self.assertEqual(_to_base_symbol("eth-usdt"), "ETH")
        self.assertEqual(_to_base_symbol("SOL"), "SOL")

    def test_build_health_summary_pass(self):
        out = build_health_summary(
            [
                {"status": "PASS"},
                {"status": "PASS"},
            ]
        )
        self.assertEqual(out["overall"], "PASS")
        self.assertEqual(out["counts"]["PASS"], 2)

    def test_build_health_summary_warn_and_fail(self):
        out_warn = build_health_summary(
            [
                {"status": "PASS"},
                {"status": "WARN"},
            ]
        )
        self.assertEqual(out_warn["overall"], "WARN")

        out_fail = build_health_summary(
            [
                {"status": "PASS"},
                {"status": "FAIL"},
                {"status": "WARN"},
            ]
        )
        self.assertEqual(out_fail["overall"], "FAIL")
        self.assertEqual(out_fail["counts"]["FAIL"], 1)


if __name__ == "__main__":
    unittest.main()
