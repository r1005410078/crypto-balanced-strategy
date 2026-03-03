#!/usr/bin/env python3
import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


def _parse_hhmm(text):
    parts = str(text).split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM value: {text}")
    hh = int(parts[0])
    mm = int(parts[1])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError(f"Invalid HH:MM value: {text}")
    return hh, mm


def _next_run_seconds(run_at_hhmm):
    hh, mm = _parse_hhmm(run_at_hhmm)
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _run_cycle(cycle_script, passthrough_args, cwd):
    cmd = [sys.executable, str(cycle_script)] + list(passthrough_args)
    ts = datetime.now().isoformat()
    print(f"[{ts}] run: {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout.rstrip(), flush=True)
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr, flush=True)
    print(f"[{datetime.now().isoformat()}] exit_code={proc.returncode}", flush=True)
    return proc.returncode


def main():
    p = argparse.ArgumentParser(
        description="Run auto_cycle.py on schedule for unattended trading."
    )
    p.add_argument("--cycle-script", type=str, default=None, help="Path to auto_cycle.py")
    p.add_argument("--cwd", type=str, default=None, help="Working directory")
    p.add_argument("--run-at", type=str, default="08:05", help="Daily local run time in HH:MM")
    p.add_argument("--interval-minutes", type=float, default=None, help="If set, run every N minutes")
    p.add_argument("--run-immediately", action="store_true")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    args, passthrough = p.parse_known_args()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    cycle_script = Path(args.cycle_script).resolve() if args.cycle_script else script_dir / "auto_cycle.py"
    cwd = Path(args.cwd).resolve() if args.cwd else skill_root

    if args.once:
        rc = _run_cycle(cycle_script, passthrough, cwd)
        raise SystemExit(rc)

    if args.run_immediately:
        _run_cycle(cycle_script, passthrough, cwd)

    try:
        while True:
            if args.interval_minutes is not None:
                wait_sec = max(1.0, float(args.interval_minutes) * 60.0)
            else:
                wait_sec = _next_run_seconds(args.run_at)
            print(
                f"[{datetime.now().isoformat()}] sleeping {int(wait_sec)}s before next cycle",
                flush=True,
            )
            time.sleep(wait_sec)
            _run_cycle(cycle_script, passthrough, cwd)
    except KeyboardInterrupt:
        print(f"[{datetime.now().isoformat()}] stopped by keyboard interrupt", flush=True)


if __name__ == "__main__":
    main()
