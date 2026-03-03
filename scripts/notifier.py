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


def notify_all(payload, *, cli_urls=None, timeout=10):
    urls = parse_webhook_urls(cli_urls=cli_urls)
    if not urls:
        return []
    return [send_webhook(u, payload, timeout=timeout) for u in urls]
