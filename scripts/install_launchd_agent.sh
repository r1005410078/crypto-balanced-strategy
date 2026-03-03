#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DST_PLIST="$HOME/Library/LaunchAgents/com.crypto-balanced-strategy.auto.plist"
LABEL="com.crypto-balanced-strategy.auto"
VARIANT="${1:-balanced}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/install_launchd_agent.sh [balanced|conservative|aggressive|adaptive]
  bash scripts/install_launchd_agent.sh --variant [balanced|conservative|aggressive|adaptive]
EOF
}

if [[ "${1:-}" == "--variant" ]]; then
  VARIANT="${2:-}"
fi

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

mkdir -p "$(dirname "$DST_PLIST")"

python3 - "$SRC_PLIST" "$DST_PLIST" <<'PY'
import sys
from pathlib import Path

src = Path(sys.argv[1]).read_text()
dst = Path(sys.argv[2])

import os
text = src.replace("REPLACE_ME", "__PLACEHOLDER__")

vals = [
    os.environ["OKX_API_KEY"],
    os.environ["OKX_API_SECRET"],
    os.environ["OKX_API_PASSPHRASE"],
]
for v in vals:
    text = text.replace("__PLACEHOLDER__", v, 1)

dst.write_text(text)
print(str(dst))
PY

plutil -lint "$DST_PLIST" >/dev/null

launchctl bootout "gui/$(id -u)" "$DST_PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST_PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "variant: $VARIANT"
echo "installed: $DST_PLIST"
echo "status: launchctl print gui/$(id -u)/$LABEL"
echo "logs: /tmp/crypto-balanced-strategy-auto.out.log /tmp/crypto-balanced-strategy-auto.err.log"
