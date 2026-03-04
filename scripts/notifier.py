#!/usr/bin/env python3
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_webhook_urls(cli_urls=None):
    urls = []
    for u in cli_urls or []:
        v = str(u).strip()
        if v:
            urls.append(v)
    raw = os.environ.get("AUTO_WEBHOOK_URLS", "")
    for x in raw.split(","):
        v = x.strip()
        if v:
            urls.append(v)
    # Keep order, remove duplicates.
    seen = set()
    out = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def parse_telegram_targets(cli_chat_ids=None, bot_token=None):
    token = str(bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()

    chat_ids = []
    for cid in cli_chat_ids or []:
        v = str(cid).strip()
        if v:
            chat_ids.append(v)
    raw = os.environ.get("TELEGRAM_CHAT_IDS", "")
    for x in raw.split(","):
        v = x.strip()
        if v:
            chat_ids.append(v)

    seen = set()
    unique_ids = []
    for cid in chat_ids:
        if cid in seen:
            continue
        seen.add(cid)
        unique_ids.append(cid)
    return token, unique_ids


def _fmt_num(v, digits=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.{digits}f}"


def _truncate(text, max_chars):
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...(truncated)"


def _format_auto_cycle(payload):
    lines = ["自动交易执行"]
    ts = payload.get("generated_at")
    if ts:
        lines.append(f"时间: {ts}")
    mode = payload.get("mode")
    status = payload.get("cycle_status")
    if mode or status:
        lines.append(f"模式: {mode or '-'} | 状态: {status or '-'}")
    active = payload.get("active_profile")
    target = payload.get("target_profile")
    if active or target:
        if active == target:
            lines.append(f"策略档位: {active or '-'}")
        else:
            lines.append(f"策略档位: {active or '-'} -> {target or '-'}")

    execution_counts = payload.get("execution_counts")
    if isinstance(execution_counts, dict) and execution_counts:
        parts = [f"{k}={v}" for k, v in execution_counts.items()]
        lines.append("执行结果: " + ", ".join(parts))

    risk_ok = payload.get("risk_ok")
    if risk_ok is not None:
        lines.append("风控: " + ("通过" if bool(risk_ok) else "阻断"))
    reasons = payload.get("risk_reasons") or []
    if reasons:
        reason_text = "; ".join(str(x) for x in reasons[:3])
        lines.append("原因: " + reason_text)

    results_file = payload.get("results_file")
    if results_file:
        lines.append(f"结果文件: {results_file}")
    return "\n".join(lines)


def _format_hot_strategy_advice(payload):
    lines = ["热门策略建议"]
    ts = payload.get("generated_at")
    if ts:
        lines.append(f"时间: {ts}")
    tier = payload.get("auto_tier_selected_tier")
    if tier:
        lines.append(f"当前档位: {tier}")
    status = payload.get("status")
    if status:
        lines.append(f"状态: {status}")

    summary = payload.get("summary")
    if isinstance(summary, dict):
        budget = summary.get("recommended_budget_usdt")
        if budget is not None:
            lines.append(f"建议预算: {_fmt_num(budget)} USDT")
        hold_cash_block = summary.get("hold_cash_block")
        if hold_cash_block is not None:
            lines.append("主策略闸门: " + ("阻断 (hold_cash)" if hold_cash_block else "通过"))
        selected = summary.get("selected") or []
        if selected:
            lines.append("推荐策略:")
            for row in selected[:3]:
                if not isinstance(row, dict):
                    continue
                stype = row.get("strategy_type") or "-"
                alloc = row.get("allocation_usdt")
                risk = row.get("risk_level")
                row_text = f"- {stype}"
                if alloc is not None:
                    row_text += f" 约 {_fmt_num(alloc)}U"
                if risk:
                    row_text += f" ({risk})"
                lines.append(row_text)

    err = payload.get("error")
    if err:
        lines.append(f"错误: {err}")
    results_file = payload.get("auto_tier_results_file")
    if results_file:
        lines.append(f"关联结果: {results_file}")
    return "\n".join(lines)


def _format_generic(payload):
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return str(payload)
    lines = []
    for key in ("event", "generated_at", "status", "message", "source", "mode"):
        if key in payload and payload.get(key) not in (None, ""):
            lines.append(f"{key}: {payload.get(key)}")
    # Append simple scalar fields not already included.
    used = {x.split(":", 1)[0].strip() for x in lines}
    for k, v in payload.items():
        if k in used:
            continue
        if isinstance(v, (str, int, float, bool)):
            lines.append(f"{k}: {v}")
    if lines:
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _payload_to_text(payload, max_chars=3800):
    if isinstance(payload, dict):
        event = str(payload.get("event", "")).strip().lower()
        if event == "auto_cycle":
            return _truncate(_format_auto_cycle(payload), max_chars)
        if event == "hot_strategy_advice":
            return _truncate(_format_hot_strategy_advice(payload), max_chars)
    return _truncate(_format_generic(payload), max_chars)


def send_webhook(url, payload, timeout=10):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url=url,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "crypto-balanced-strategy-auto-notifier/1.0",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        return {"ok": True, "url": url, "status": getattr(resp, "status", 200), "response": raw[:500]}
    except HTTPError as e:
        return {"ok": False, "url": url, "error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}"}
    except URLError as e:
        return {"ok": False, "url": url, "error": f"Network error: {e}"}
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}


def send_telegram(bot_token, chat_id, payload, timeout=10):
    token = str(bot_token or "").strip()
    chat = str(chat_id or "").strip()
    if not token:
        return {"ok": False, "channel": "telegram", "chat_id": chat, "error": "missing_bot_token"}
    if not chat:
        return {"ok": False, "channel": "telegram", "chat_id": chat, "error": "missing_chat_id"}

    body = json.dumps(
        {
            "chat_id": chat,
            "text": _payload_to_text(payload),
            "disable_web_page_preview": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = Request(
        url=api_url,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "crypto-balanced-strategy-auto-notifier/1.0",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        return {
            "ok": True,
            "channel": "telegram",
            "chat_id": chat,
            "status": getattr(resp, "status", 200),
            "response": raw[:500],
        }
    except HTTPError as e:
        return {
            "ok": False,
            "channel": "telegram",
            "chat_id": chat,
            "error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}",
        }
    except URLError as e:
        return {"ok": False, "channel": "telegram", "chat_id": chat, "error": f"Network error: {e}"}
    except Exception as e:
        return {"ok": False, "channel": "telegram", "chat_id": chat, "error": str(e)}


def notify_all(payload, *, cli_urls=None, timeout=10, telegram_bot_token=None, telegram_chat_ids=None):
    out = []
    urls = parse_webhook_urls(cli_urls=cli_urls)
    for u in urls:
        out.append(send_webhook(u, payload, timeout=timeout))

    tg_token, tg_ids = parse_telegram_targets(
        cli_chat_ids=telegram_chat_ids,
        bot_token=telegram_bot_token,
    )
    for cid in tg_ids:
        out.append(send_telegram(tg_token, cid, payload, timeout=timeout))
    return out
