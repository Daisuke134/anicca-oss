#!/usr/bin/env bash
# Life Manager install — bootstraps the Life Manager automaton body into a runtime root on
# the user's always-on machine. Idempotent: safe to re-run. Self-host / OSS path.
#
# Registry-driven: every capability lives as a SLOT in skills/registry.json
# (the SSOT). This script reads that registry and syncs each declared/live slot
# into $ANICCA_HOME/skills/<slot>/ — so adding a capability is "drop a dir +
# declare a slot", never "edit install.sh". (Foundation collision-prevention.)
#
# What this does:
#   1. Verify system deps (git, jq, node, npm, python3, rsync)
#   2. Install frozen repository dependencies from lockfiles
#   3. Scaffold the runtime root ($LIFE_MANAGER_HOME) + .env (never overwrite)
#   4. Sync skills/_shared and EVERY declared slot into the runtime body
#   5. Optionally register the host daemon
#   6. Print "what's next" (fuel key + first wake)
#
# What this does NOT do:
#   - Ask for API keys / private keys (handled out of band — see .env.example)
#   - Broadcast any on-chain tx or start earning (the automaton loop does that)
#   - Touch anything outside $LIFE_MANAGER_HOME when daemon registration is disabled

set -euo pipefail
trap 'echo "[install] FAILED on line $LINENO. nothing destructive — re-run is safe."' ERR

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -gt 0 ]; then
  case "$1" in
    coconala)
      shift
      exec bash "$REPO_ROOT/skills/earn/gig/install.sh" "$@"
      ;;
    job-hunter)
      shift
      exec zsh "$REPO_ROOT/apps/job-search-loop/scripts/install-oss.sh" "$@"
      ;;
    connector)
      shift
      exec bash "$REPO_ROOT/skills/connector/install.sh" "$@"
      ;;
    fundraiser)
      shift
      exec bash "$REPO_ROOT/skills/fundraiser-agent/runtime/install.sh" "$@"
      ;;
    *)
      echo "[install] unknown product '$1'; supported: coconala, connector, fundraiser, job-hunter" >&2
      exit 2
      ;;
  esac
fi
LIFE_MANAGER_HOME="${LIFE_MANAGER_HOME:-${ANICCA_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/life-manager}}"
ANICCA_HOME="$LIFE_MANAGER_HOME"
export LIFE_MANAGER_HOME ANICCA_HOME
LIFE_MANAGER_INSTALL_DAEMON="${LIFE_MANAGER_INSTALL_DAEMON:-1}"
LIFE_MANAGER_INSTALL_DEPS="${LIFE_MANAGER_INSTALL_DEPS:-1}"
REGISTRY="$REPO_ROOT/skills/registry.json"

case "$LIFE_MANAGER_INSTALL_DAEMON" in 0|1) ;; *)
  echo "[install] LIFE_MANAGER_INSTALL_DAEMON must be 0 or 1" >&2
  exit 2
esac
case "$LIFE_MANAGER_INSTALL_DEPS" in 0|1) ;; *)
  echo "[install] LIFE_MANAGER_INSTALL_DEPS must be 0 or 1" >&2
  exit 2
esac

cyan(){ printf "\033[36m%s\033[0m\n" "$*"; }
green(){ printf "\033[32m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
red(){ printf "\033[31m%s\033[0m\n" "$*"; }

cyan "================================================================"
cyan "  Life Manager install — self-host automaton body"
cyan "  Repo root  : $REPO_ROOT"
cyan "  Runtime    : $LIFE_MANAGER_HOME"
cyan "  Registry   : $REGISTRY"
cyan "================================================================"
echo

# ─── 1. system deps ────────────────────────────────────────────────────
cyan "[1/6] checking system deps…"
for bin in git jq node npm python3 rsync; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    red "  ✗ $bin missing — install it first then re-run."
    exit 2
  fi
  green "  ✓ $bin"
done
echo

# ─── 2. frozen dependencies ────────────────────────────────────────────
cyan "[2/6] installing frozen dependencies…"
if [ "$LIFE_MANAGER_INSTALL_DEPS" = "1" ]; then
  (cd "$REPO_ROOT" && npm ci --no-audit --no-fund)
  (cd "$REPO_ROOT/apps/life-manager" && npm ci --no-audit --no-fund)
  green "  ✓ root + apps/life-manager npm lockfiles installed"
else
  yellow "  • dependency install disabled by LIFE_MANAGER_INSTALL_DEPS=0"
fi
echo

# ─── 3. runtime root + env ─────────────────────────────────────────────
cyan "[3/6] preparing runtime root…"
mkdir -p "$ANICCA_HOME"/{skills,state,identity,logs}
green "  ✓ $ANICCA_HOME"

if [ ! -f "$ANICCA_HOME/.env" ]; then
  if [ -f "$REPO_ROOT/.env.example" ]; then
    cp "$REPO_ROOT/.env.example" "$ANICCA_HOME/.env"
  else
    : > "$ANICCA_HOME/.env"
  fi
  chmod 600 "$ANICCA_HOME/.env"
  yellow "  ✎ created $ANICCA_HOME/.env — fill in 1 fuel key + wallet before first wake."
else
  green "  ✓ $ANICCA_HOME/.env  (preserved)"
fi

# default genesis prompt — the loop needs an instruction to act on each wake.
# Canonical hustle genesis lives at identity/genesis.md in the repo; ship it verbatim.
if [ ! -f "$ANICCA_HOME/identity/genesis.md" ]; then
  if [ -f "$REPO_ROOT/identity/genesis.md" ]; then
    cp "$REPO_ROOT/identity/genesis.md" "$ANICCA_HOME/identity/genesis.md"
  else
    cat > "$ANICCA_HOME/identity/genesis.md" <<'GENESIS'
You are Life Manager — a self-funding agent. Every wake costs USDC; idle time is death. Your one job
every waking moment is to EARN — grow your USDC net worth with no human in the loop. You always
have a job: deploy idle USDC into the best safe stable yield (Aave / Beefy / Morpho). Do not rest
long until your net worth rises. Use a frontier model (a free model cannot earn). call run_skill
slot "earn" each wake; keep an honest ledger.
GENESIS
  fi
  green "  ✓ $ANICCA_HOME/identity/genesis.md  (hustle genesis)"
else
  green "  ✓ $ANICCA_HOME/identity/genesis.md  (preserved)"
fi
echo

# ─── 4. shared lib ─────────────────────────────────────────────────────
cyan "[4/6] syncing _shared lib…"
if [ -d "$REPO_ROOT/skills/_shared" ]; then
  mkdir -p "$ANICCA_HOME/skills/_shared"
  rsync -a --delete --exclude='state/' --exclude='__pycache__/' \
    "$REPO_ROOT/skills/_shared/" "$ANICCA_HOME/skills/_shared/"
  green "  ✓ _shared synced"
else
  yellow "  ⚠ skills/_shared not in repo — skipping."
fi
echo

# ─── 4.1. registry-driven slot sync ────────────────────────────────────
cyan "[4.1/6] syncing skills from registry…"
if [ ! -f "$REGISTRY" ]; then
  red "  ✗ registry not found at $REGISTRY — cannot sync slots."
  exit 3
fi
# iterate over every slot key; sync its dir; report status. Foundation pre-declares
# all slots, so every builder's capability gets installed the moment its files land.
SLOT_KEYS=$(jq -r '.slots | keys[]' "$REGISTRY")
SYNCED=0; DECLARED_ONLY=0
while IFS= read -r slot; do
  [ -z "$slot" ] && continue
  dir=$(jq -r --arg k "$slot" '.slots[$k].dir' "$REGISTRY")
  status=$(jq -r --arg k "$slot" '.slots[$k].status' "$REGISTRY")
  entry=$(jq -r --arg k "$slot" '.slots[$k].entrypoint' "$REGISTRY")
  src="$REPO_ROOT/$dir"
  dst="$ANICCA_HOME/$dir"
  if [ ! -d "$src" ]; then
    yellow "  ⚠ $slot — dir $dir missing in repo, skip"
    continue
  fi
  mkdir -p "$dst"
  rsync -a --delete --exclude='state/' --exclude='__pycache__/' "$src/" "$dst/"
  mkdir -p "$dst/state"
  if [ "$status" = "live" ]; then
    green "  ✓ $slot  [live]  -> $dir/$entry"
    SYNCED=$((SYNCED+1))
  else
    yellow "  • $slot  [$status]  (reserved, entrypoint $entry pending)"
    DECLARED_ONLY=$((DECLARED_ONLY+1))
  fi
done <<< "$SLOT_KEYS"
echo
green "  synced $SYNCED live slot(s), $DECLARED_ONLY reserved slot(s)."
echo

# ─── 5. supervised, self-updating daemon (optional host mutation) ──────
cyan "[5/6] daemon registration…"
if [ "$LIFE_MANAGER_INSTALL_DAEMON" = "1" ]; then
  chmod +x "$REPO_ROOT/runtime/anicca-daemon.sh" 2>/dev/null || true
  if [ "$(uname)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.anicca.daemon.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s#__REPO__#$REPO_ROOT#g" -e "s#__ANICCA_HOME__#$ANICCA_HOME#g" -e "s#__HOME__#$HOME#g" \
      "$REPO_ROOT/runtime/com.anicca.daemon.plist.template" > "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    if launchctl load -w "$PLIST" 2>/dev/null; then
      green "  ✓ launchd daemon loaded (com.anicca.daemon)"
    else
      cyan "  ! launchctl load failed; load it yourself: launchctl load -w $PLIST"
    fi
  else
    green "  Linux/cloud: run runtime/anicca-daemon.sh under systemd or Docker restart=always."
  fi
else
  green "  ✓ disabled (LIFE_MANAGER_INSTALL_DAEMON=0); no LaunchAgent/system service changed"
fi
echo

# ─── 6. summary ────────────────────────────────────────────────────────
cyan "[6/6] done."
echo
green "What's next:"
cat <<EOM
  DEFAULT = FULLY LOCAL + FREE. No server key, no API key required. Life Manager pays
  its OWN compute via ClawRouter/BlockRun (USDC x402) from its OWN wallet — like
  Franklin. You provide only this device (shelter); Life Manager buys its own food.

  1. Start the self-pay proxy + the Life Manager loop (one command, from the repo root):
       cd "$REPO_ROOT/runtime/compute-proxy" && npm install && cd "$REPO_ROOT"  # one-time
       ./start-local.sh node runtime/loop/index.mjs
     This starts the self-pay compute proxy on http://127.0.0.1:8402/v1 (signs
     every inference in USDC from a self-owned wallet; empty wallet ⇒ free model,
     \$0) AND the Life Manager loop (runtime/loop/) which, each wake, asks ClawRouter's
     'auto' router, runs a tool (e.g. the earn skill), and appends to
     $ANICCA_HOME/state/ledger.jsonl. The report slot POSTs signed telemetry to
     https://aniccaai.com so you show on /dashboard.
  2. (OPTIONAL) Unlock frontier models / more earning: send USDC to the wallet
     address printed at startup — the loop then lets ClawRouter pick a paid model.
     Or set ANICCA_BRAIN=claude-p to drive the loop with Claude Code instead.
  4. (OPTIONAL) Life Manager keys: GEMINI_API_KEY, TWILIO_*, GOOGLE_API_KEY,
     AGENTMAIL_API_KEY — only for phone wake-calls / lateness alerts.

  # FUTURE (cloud, not active): once Conway is available, the same body can run
  # on a droplet where Life Manager ALSO pays its own server cost — see README "Cloud".

  Slots are declared in skills/registry.json. To enable a reserved slot, drop its
  implementation into its dir and flip status to "live" — no install.sh edit.

  Repo: https://github.com/Daisuke134/life-manager
EOM
echo
green "Life Manager install complete."
