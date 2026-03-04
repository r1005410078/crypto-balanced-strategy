#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DST_PLIST="$HOME/Library/LaunchAgents/com.crypto-balanced-strategy.auto.plist"
LABEL="com.crypto-balanced-strategy.auto"
VARIANT="adaptive"
KICKSTART="false"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/install_launchd_agent.sh [adaptive|balanced|conservative|aggressive]
  bash scripts/install_launchd_agent.sh --variant [adaptive|balanced|conservative|aggressive]
  bash scripts/install_launchd_agent.sh [--variant adaptive] [--kickstart|--no-kickstart]
Default variant: adaptive (dual-sleeve unattended production path).
EOF
}

while [[ $# -gt 0 ]]; do
  case "${1:-}" in
    -h|--help)
      usage
      exit 0
      ;;
    --variant)
      if [[ $# -lt 2 ]]; then
        usage
        echo "missing value for --variant" >&2
        exit 1
      fi
      VARIANT="$2"
      shift 2
      continue
      ;;
    --kickstart)
      KICKSTART="true"
      shift
      continue
      ;;
    --no-kickstart)
      KICKSTART="false"
      shift
      continue
      ;;
    balanced|conservative|aggressive|adaptive)
      VARIANT="$1"
      shift
      continue
      ;;
    *)
      usage
      echo "invalid argument: $1" >&2
      exit 1
      ;;
  esac
done

case "$VARIANT" in
  balanced)
    SRC_PLIST="$ROOT_DIR/scripts/com.crypto-balanced-strategy.auto.balanced.plist"
    ;;
  conservative)
    SRC_PLIST="$ROOT_DIR/scripts/com.crypto-balanced-strategy.auto.conservative.plist"
    ;;
  aggressive)
    SRC_PLIST="$ROOT_DIR/scripts/com.crypto-balanced-strategy.auto.aggressive.plist"
    ;;
  adaptive)
    SRC_PLIST="$ROOT_DIR/scripts/com.crypto-balanced-strategy.auto.adaptive.plist"
    ;;
  *)
    usage
    echo "invalid variant: $VARIANT" >&2
    exit 1
    ;;
esac

if [[ ! -f "$SRC_PLIST" ]]; then
  echo "missing plist template: $SRC_PLIST" >&2
  exit 1
fi

need_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "missing env: $name" >&2
    exit 1
  fi
}

need_env OKX_API_KEY
need_env OKX_API_SECRET
need_env OKX_API_PASSPHRASE
PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "missing executable python3 in PATH" >&2
  exit 1
fi

mkdir -p "$(dirname "$DST_PLIST")"

PYTHON_BIN="$PYTHON_BIN" python3 - "$SRC_PLIST" "$DST_PLIST" <<'PY'
import sys
import os
import plistlib
from pathlib import Path

src_path = Path(sys.argv[1])
dst_path = Path(sys.argv[2])
plist = plistlib.loads(src_path.read_bytes())

args = list(plist.get("ProgramArguments", []))
if args and args[0] == "/usr/bin/python3":
    args[0] = os.environ["PYTHON_BIN"]
plist["ProgramArguments"] = args

env = dict(plist.get("EnvironmentVariables", {}))
env["OKX_API_KEY"] = os.environ["OKX_API_KEY"]
env["OKX_API_SECRET"] = os.environ["OKX_API_SECRET"]
env["OKX_API_PASSPHRASE"] = os.environ["OKX_API_PASSPHRASE"]
tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
tg_chats = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
if tg_token:
    env["TELEGRAM_BOT_TOKEN"] = tg_token
if tg_chats:
    env["TELEGRAM_CHAT_IDS"] = tg_chats
plist["EnvironmentVariables"] = env

dst_path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False))
print(str(dst_path))
PY

plutil -lint "$DST_PLIST" >/dev/null

launchctl bootout "gui/$(id -u)" "$DST_PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST_PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
if [[ "$KICKSTART" == "true" ]]; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
fi

echo "variant: $VARIANT"
echo "installed: $DST_PLIST"
echo "kickstart_now: $KICKSTART"
echo "status: launchctl print gui/$(id -u)/$LABEL"
echo "logs: /tmp/crypto-balanced-strategy-auto.out.log /tmp/crypto-balanced-strategy-auto.err.log"
if [[ "$KICKSTART" != "true" ]]; then
  echo "manual run now: launchctl kickstart -k gui/$(id -u)/$LABEL"
fi
