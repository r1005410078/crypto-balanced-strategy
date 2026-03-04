#!/usr/bin/env python3
import os
import sys
import unittest
from unittest.mock import patch

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from notifier import _payload_to_text, notify_all, parse_telegram_targets, parse_webhook_urls  # noqa: E402


class NotifierTests(unittest.TestCase):
    def test_parse_webhook_urls_dedup(self):
        with patch.dict(os.environ, {"AUTO_WEBHOOK_URLS": "https://a, https://b,https://a"}, clear=True):
            out = parse_webhook_urls(cli_urls=["https://b", "https://c"])
        self.assertEqual(out, ["https://b", "https://c", "https://a"])

    def test_parse_telegram_targets_from_env(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "bot_token", "TELEGRAM_CHAT_IDS": "1001,1002,1001"},
            clear=True,
        ):
            token, ids = parse_telegram_targets()
        self.assertEqual(token, "bot_token")
        self.assertEqual(ids, ["1001", "1002"])

    def test_notify_all_with_both_channels(self):
        payload = {"event": "unit_test"}
        with patch("notifier.send_webhook") as m_web, patch("notifier.send_telegram") as m_tg:
            m_web.side_effect = lambda u, p, timeout=10: {"ok": True, "channel": "webhook", "url": u}
            m_tg.side_effect = lambda token, cid, p, timeout=10: {"ok": True, "channel": "telegram", "chat_id": cid}
            with patch.dict(
                os.environ,
                {
                    "AUTO_WEBHOOK_URLS": "https://a",
                    "TELEGRAM_BOT_TOKEN": "bot_token",
                    "TELEGRAM_CHAT_IDS": "1001,1002",
                },
                clear=True,
            ):
                out = notify_all(payload, cli_urls=["https://b"])
        self.assertEqual(len(out), 4)
        channels = sorted([x["channel"] for x in out])
        self.assertEqual(channels, ["telegram", "telegram", "webhook", "webhook"])

    def test_payload_to_text_auto_cycle_is_human_readable(self):
        payload = {
            "event": "auto_cycle",
            "generated_at": "2026-03-04T14:06:04",
            "mode": "LIVE",
            "cycle_status": "blocked",
            "active_profile": "stable",
            "target_profile": "stable",
            "execution_counts": {"BLOCKED": 1},
            "risk_ok": False,
            "risk_reasons": ["daily pnl breached"],
            "results_file": "/tmp/auto_cycle_x.json",
        }
        text = _payload_to_text(payload)
        self.assertIn("自动交易执行", text)
        self.assertIn("状态: blocked", text)
        self.assertIn("执行结果: BLOCKED=1", text)
        self.assertIn("风控: 阻断", text)
        self.assertNotIn("{", text)

    def test_payload_to_text_hot_advice_is_human_readable(self):
        payload = {
            "event": "hot_strategy_advice",
            "generated_at": "2026-03-04T14:05:10",
            "auto_tier_selected_tier": "conservative",
            "status": "ok",
            "summary": {
                "recommended_budget_usdt": 25,
                "hold_cash_block": True,
                "selected": [
                    {"strategy_type": "grid", "allocation_usdt": 10, "risk_level": "low"},
                    {"strategy_type": "spot_dca", "allocation_usdt": 8, "risk_level": "low"},
                ],
            },
        }
        text = _payload_to_text(payload)
        self.assertIn("热门策略建议", text)
        self.assertIn("建议预算: 25 USDT", text)
        self.assertIn("主策略闸门: 阻断 (hold_cash)", text)
        self.assertIn("- grid 约 10U (low)", text)
        self.assertNotIn("{", text)


if __name__ == "__main__":
    unittest.main()
