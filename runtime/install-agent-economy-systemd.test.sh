#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
RELEASE="$TEMP_ROOT/loops/releases/$(printf 'a%.0s' {1..40})"
HOME_DIR="$TEMP_ROOT/home"
CONFIG="$TEMP_ROOT/config"
CALLS="$TEMP_ROOT/systemctl.calls"
mkdir -p "$RELEASE/skills/agent-economy" "$HOME_DIR" "$CONFIG"
printf '#!/bin/sh\nexit 0\n' >"$RELEASE/skills/agent-economy/launch.sh"
chmod +x "$RELEASE/skills/agent-economy/launch.sh"
cat >"$TEMP_ROOT/systemctl" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >>"$SYSTEMCTL_CALLS"
if [ "$2" = "is-active" ]; then
  [ -f "$SYSTEMCTL_STATE" ]
  exit $?
fi
if [ "$2" = "enable" ]; then
  : >"$SYSTEMCTL_STATE"
fi
exit 0
EOF
chmod +x "$TEMP_ROOT/systemctl"

run_install() {
  HOME="$HOME_DIR" XDG_CONFIG_HOME="$CONFIG" SYSTEMCTL_BIN="$TEMP_ROOT/systemctl" \
    SYSTEMCTL_CALLS="$CALLS" SYSTEMCTL_STATE="$TEMP_ROOT/systemctl.state" \
    bash "$ROOT/runtime/install-agent-economy-systemd.sh" "$RELEASE" "$HOME_DIR/life-manager"
}

run_install >/dev/null
UNIT="$CONFIG/systemd/user/life-manager-agent-economy.service"
[ -f "$UNIT" ]
grep -F "ANICCA_HOME=$HOME_DIR/life-manager/agent-economy/instance" "$UNIT" >/dev/null
grep -F "ExecStart=\"$RELEASE/skills/agent-economy/launch.sh\"" "$UNIT" >/dev/null
[ "$(stat -f '%Lp' "$UNIT" 2>/dev/null || stat -c '%a' "$UNIT")" = "600" ]
FIRST_HASH="$(shasum -a 256 "$UNIT" | awk '{print $1}')"
run_install >/dev/null
[ "$FIRST_HASH" = "$(shasum -a 256 "$UNIT" | awk '{print $1}')" ]
[ "$(grep -c '^--user restart ' "$CALLS" || true)" = "0" ]
grep -F -- '--user enable --now life-manager-agent-economy.service' "$CALLS" >/dev/null
printf 'systemd Local install: PASS\n'
