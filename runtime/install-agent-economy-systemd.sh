#!/usr/bin/env bash
set -euo pipefail

RELEASE_ROOT="${1:?release root is required}"
LIFE_MANAGER_HOME="${2:?Life Manager home is required}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-$(command -v systemctl || true)}"
[ -x "$SYSTEMCTL_BIN" ] || { echo "systemctl is required for Linux self-host" >&2; exit 2; }
[ -x "$RELEASE_ROOT/skills/agent-economy/launch.sh" ] \
  || { echo "Agent Economy launcher missing from release" >&2; exit 2; }

case "$RELEASE_ROOT$LIFE_MANAGER_HOME" in
  *$'\n'*|*'"'*|*'\\'*) echo "runtime paths may not contain newlines, quotes, or backslashes" >&2; exit 2 ;;
esac

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/life-manager-agent-economy.service"
INSTANCE_HOME="$LIFE_MANAGER_HOME/agent-economy/instance"
mkdir -p "$UNIT_DIR" "$LIFE_MANAGER_HOME/agent-economy/logs"
TEMP="$(mktemp "$UNIT_DIR/.life-manager-agent-economy.XXXXXX")"
trap 'rm -f "$TEMP"' EXIT

cat >"$TEMP" <<EOF
[Unit]
Description=Life Manager Agent Economy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="ANICCA_REPO=$RELEASE_ROOT"
Environment="ANICCA_CODE_ROOT=$RELEASE_ROOT"
Environment="ANICCA_RELEASE_ROOT=$(cd "$RELEASE_ROOT/../.." && pwd -P)"
Environment="ANICCA_HOME=$INSTANCE_HOME"
Environment="LIFE_MANAGER_HOME=$LIFE_MANAGER_HOME"
ExecStart="$RELEASE_ROOT/skills/agent-economy/launch.sh"
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
chmod 600 "$TEMP"

CHANGED=1
if [ -f "$UNIT_PATH" ] && cmp -s "$TEMP" "$UNIT_PATH"; then
  CHANGED=0
else
  mv "$TEMP" "$UNIT_PATH"
fi

WAS_ACTIVE=0
"$SYSTEMCTL_BIN" --user is-active --quiet life-manager-agent-economy.service && WAS_ACTIVE=1 || true
"$SYSTEMCTL_BIN" --user daemon-reload
"$SYSTEMCTL_BIN" --user enable --now life-manager-agent-economy.service
if [ "$CHANGED" = "1" ] && [ "$WAS_ACTIVE" = "1" ]; then
  "$SYSTEMCTL_BIN" --user restart life-manager-agent-economy.service
fi
"$SYSTEMCTL_BIN" --user is-active --quiet life-manager-agent-economy.service
printf 'Agent Economy systemd owner active\n'
