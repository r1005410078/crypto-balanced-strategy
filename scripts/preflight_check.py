#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from okx_auto_executor import OkxApiError, OkxClient


def check_python(min_major=3, min_minor=10):
    ok = sys.version_info >= (min_major, min_minor)
    return {
        "name": "python_version",
        "status": "PASS" if ok else "FAIL",
        "message": (
            f"Python {sys.version_info.major}.{sys.version_info.minor} detected; "
            f"required >= {min_major}.{min_minor}"
        ),
    }


def check_required_paths(skill_root, rel_paths):
    missing = [p for p in rel_paths if not (skill_root / p).exists()]
    return {
        "name": "required_files",
        "status": "PASS" if not missing else "FAIL",
        "message": "All required files found." if not missing else f"Missing: {', '.join(missing)}",
        "missing": missing,
    }


def check_results_dir(skill_root):
    results = skill_root / "results"
    try:
        results.mkdir(parents=True, exist_ok=True)
        probe = results / ".preflight_write_test"
        probe.write_text("ok\n")
        probe.unlink(missing_ok=True)
        return {
            "name": "results_dir_writable",
            "status": "PASS",
            "message": f"Writable: {results}",
        }
    except Exception as e:
        return {
            "name": "results_dir_writable",
            "status": "FAIL",
            "message": f"Cannot write to {results}: {e}",
        }


def check_env_vars(var_defs):
    missing = []
    for vd in var_defs:
        name = vd.get("name")
        if not name:
            continue
        if not os.environ.get(name, "").strip():
            missing.append(name)
    return {
        "name": "okx_env_vars",
        "status": "PASS" if not missing else "WARN",
        "message": "OKX env vars set." if not missing else f"Missing env vars: {', '.join(missing)}",
        "missing": missing,
    }


def check_okx_read_access(base_url="https://www.okx.com"):
    api_key = os.environ.get("OKX_API_KEY", "").strip()
    api_secret = os.environ.get("OKX_API_SECRET", "").strip()
    passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    if not api_key or not api_secret or not passphrase:
        return {
            "name": "okx_read_access",
            "status": "WARN",
            "message": "Skipped: missing OKX env vars.",
            "balance_ccy_count": 0,
        }

    try:
        client = OkxClient(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            base_url=base_url,
        )
        balances = client.get_spot_balances()
        return {
            "name": "okx_read_access",
            "status": "PASS",
            "message": "OKX read API auth succeeded.",
            "balance_ccy_count": len(balances),
        }
    except OkxApiError as e:
        return {
            "name": "okx_read_access",
            "status": "FAIL",
            "message": str(e),
            "balance_ccy_count": 0,
        }


def summarize(results):
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for r in results:
        status = r.get("status", "WARN")
        counts[status] = counts.get(status, 0) + 1
    ready_core = counts["FAIL"] == 0
    ready_okx = all(
        r.get("status") == "PASS"
        for r in results
        if r.get("name") in {"okx_env_vars", "okx_read_access"}
    )
    return {
        "counts": counts,
        "ready_core": ready_core,
        "ready_okx": ready_okx,
    }


def _load_dependencies(skill_root):
    path = skill_root / "dependencies.json"
    if not path.exists():
        return {}, str(path), False
    return json.loads(path.read_text()), str(path), True


def run_preflight(skill_root, check_okx=False, base_url="https://www.okx.com"):
    deps, deps_path, deps_found = _load_dependencies(skill_root)
    env_defs = deps.get("env", [])

    checks = [
        check_python(3, 10),
        check_required_paths(
            skill_root,
            [
                "SKILL.md",
                "profiles.json",
                "dependencies.json",
                "scripts/profile_switcher.py",
                "scripts/okx_auto_executor.py",
                "scripts/trade_decision_scorecard.py",
                "scripts/auto_cycle.py",
                "scripts/auto_daemon.py",
                "scripts/auto_tier_cycle.py",
                "scripts/health_check_dryrun.py",
            ],
        ),
        check_results_dir(skill_root),
        check_env_vars(env_defs),
    ]

    if check_okx:
        checks.append(check_okx_read_access(base_url=base_url))

    summary = summarize(checks)
    return {
        "generated_at": datetime.now().isoformat(),
        "skill_root": str(skill_root),
        "dependencies_file": deps_path,
        "dependencies_file_found": deps_found,
        "requires_skills": deps.get("requires_skills", []),
        "runtime": deps.get("runtime", {}),
        "okx_api_permissions": deps.get("okx_api_permissions", {}),
        "checks": checks,
        "summary": summary,
    }


def _text_report(payload):
    lines = []
    lines.append(f"Skill root: {payload['skill_root']}")
    lines.append(f"Dependencies file: {payload['dependencies_file']} (found={payload['dependencies_file_found']})")
    lines.append("Checks:")
    for r in payload["checks"]:
        lines.append(f"- [{r['status']}] {r['name']}: {r['message']}")
    s = payload["summary"]
    lines.append(
        f"Summary: PASS={s['counts']['PASS']} WARN={s['counts']['WARN']} FAIL={s['counts']['FAIL']} "
        f"| ready_core={s['ready_core']} ready_okx={s['ready_okx']}"
    )
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--skill-root", type=str, default=None)
    p.add_argument("--check-okx", action="store_true", help="Verify OKX read API auth if env vars are set.")
    p.add_argument("--base-url", type=str, default="https://www.okx.com")
    p.add_argument("--format", choices=["text", "json"], default="text")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = Path(args.skill_root).resolve() if args.skill_root else script_dir.parent

    payload = run_preflight(skill_root, check_okx=args.check_okx, base_url=args.base_url)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_text_report(payload))

    exit_code = 0 if payload["summary"]["ready_core"] else 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
