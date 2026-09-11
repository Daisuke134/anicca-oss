#!/usr/bin/env bash
# Life Manager install — bootstraps the Life Manager automaton body into a runtime root on
# the user's always-on machine. Idempotent: safe to re-run. Self-host / OSS path.
#
# Registry-driven: every capability lives as a SLOT in this repository's
# skills/registry.json (the SSOT). Runtime state is separate; skill code is never copied.
#
# What this does:
#   1. Verify system deps (git, jq, node, npm, python3)
#   2. Install frozen repository dependencies from lockfiles
#   3. Scaffold the runtime root + first isolated citizen/wallet (never overwrite)
#   4. Validate every declared slot in the repository
#   5. Optionally register and start the host daemon
#   6. Print the installed autonomous state
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
    plan)
      shift
      exec node "$REPO_ROOT/apps/life-manager/scripts/product-onboarding-plan.js" --host local "$@"
      ;;
    *)
      echo "[install] unknown product '$1'; supported: plan, coconala, connector, fundraiser, job-hunter" >&2
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
for bin in git jq node npm python3; do
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
  (cd "$REPO_ROOT/runtime/compute-proxy" && npm ci --no-audit --no-fund)
  (cd "$REPO_ROOT/apps/life-manager" && npm ci --no-audit --no-fund)
  green "  ✓ root + runtime/compute-proxy + apps/life-manager npm lockfiles installed"
else
  yellow "  • dependency install disabled by LIFE_MANAGER_INSTALL_DEPS=0"
fi
echo

# ─── 3. runtime root + env ─────────────────────────────────────────────
cyan "[3/6] preparing runtime root…"
mkdir -p "$ANICCA_HOME"/{state,identity,logs}
mkdir -p "$ANICCA_HOME/state/skills/earn"
green "  ✓ $ANICCA_HOME"

if [ -d "$ANICCA_HOME/skills" ]; then
  ANICCA_HOME="$ANICCA_HOME" LIFE_MANAGER_SKILLS_STATE_ROOT="$ANICCA_HOME/state/skills" \
    "$REPO_ROOT/runtime/migrate-legacy-skill-state.sh"
  yellow "  ✎ copied legacy skill state; old source retained until release cutover is verified."
fi

if [ ! -f "$ANICCA_HOME/.env" ]; then
  if [ -f "$REPO_ROOT/.env.example" ]; then
    cp "$REPO_ROOT/.env.example" "$ANICCA_HOME/.env"
  else
    : > "$ANICCA_HOME/.env"
  fi
  chmod 600 "$ANICCA_HOME/.env"
  yellow "  ✎ created $ANICCA_HOME/.env — add only the provider credentials for loops you choose."
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
You are Life Manager's Agent Economy citizen. Use only your own isolated wallet, start on free
compute, keep an honest receipt ledger, and never count attempted or pending revenue as earned.
Paid compute is allowed only after verified external revenue covers the configured reserve and cap.
Call run_skill slot "earn" on each wake and fail closed when identity, authority or funds are absent.
GENESIS
  fi
  green "  ✓ $ANICCA_HOME/identity/genesis.md  (hustle genesis)"
else
  green "  ✓ $ANICCA_HOME/identity/genesis.md  (preserved)"
fi

CITIZEN_RESULT="$("$(command -v node)" "$REPO_ROOT/runtime/bootstrap-local-citizen.cjs" "$LIFE_MANAGER_HOME")"
green "  ✓ citizen $(printf '%s' "$CITIZEN_RESULT" | jq -r '.citizen_id')"
green "  ✓ own Base wallet $(printf '%s' "$CITIZEN_RESULT" | jq -r '.wallet_address')"
echo

# ─── 4. registry-owned skill validation ────────────────────────────────
cyan "[4/6] validating repository skills…"
if [ ! -f "$REGISTRY" ]; then
  red "  ✗ registry not found at $REGISTRY."
  exit 3
fi
SLOT_KEYS=$(jq -r '.slots | keys[]' "$REGISTRY")
LIVE=0; DECLARED_ONLY=0
while IFS= read -r slot; do
  [ -z "$slot" ] && continue
  dir=$(jq -r --arg k "$slot" '.slots[$k].dir' "$REGISTRY")
  status=$(jq -r --arg k "$slot" '.slots[$k].status' "$REGISTRY")
  entry=$(jq -r --arg k "$slot" '.slots[$k].entrypoint' "$REGISTRY")
  if [ "$dir" = "null" ] || [ -z "$dir" ]; then
    yellow "  • $slot  [$status]  (no executable directory)"
    DECLARED_ONLY=$((DECLARED_ONLY+1))
    continue
  fi
  src="$REPO_ROOT/$dir"
  if [ ! -d "$src" ]; then
    if [ "$status" = "live" ]; then
      red "  ✗ $slot — live dir $dir missing in repository"
      exit 3
    fi
    yellow "  • $slot  [$status]  (dir $dir not implemented)"
    DECLARED_ONLY=$((DECLARED_ONLY+1))
    continue
  fi
  if [ "$status" = "live" ]; then
    if [ "$entry" = "null" ] || [ -z "$entry" ] || [ ! -x "$src/$entry" ]; then
      red "  ✗ $slot — live entrypoint $dir/$entry is missing or not executable"
      exit 3
    fi
    green "  ✓ $slot  [live]  -> repo:$dir/$entry"
    LIVE=$((LIVE+1))
  else
    yellow "  • $slot  [$status]  (reserved, entrypoint $entry pending)"
    DECLARED_ONLY=$((DECLARED_ONLY+1))
  fi
done <<< "$SLOT_KEYS"
echo
green "  validated $LIVE live slot(s), $DECLARED_ONLY reserved slot(s); no code copied."
echo

# ─── 5. supervised, self-updating daemon (optional host mutation) ──────
cyan "[5/6] daemon registration…"
if [ "$LIFE_MANAGER_INSTALL_DAEMON" = "1" ]; then
  LOOPS_KEEP_RELEASES=2 "$REPO_ROOT/bin/cut-loop-release.sh" HEAD >/dev/null
  RELEASE_ROOT="$(readlink "${LOOPS_ROOT:-$HOME/loops}/current")"
  if [ "$(uname)" = "Darwin" ]; then
    LIFE_MANAGER_HOME="$LIFE_MANAGER_HOME" LIFE_MANAGER_APPLY_TARGET=compute-proxy \
      LIFE_MANAGER_RELEASE_ROOT="$RELEASE_ROOT" "$RELEASE_ROOT/bin/lm-loop" apply >/dev/null
    green "  ✓ repository compute proxy loaded (ai.anicca.compute-proxy)"
    LIFE_MANAGER_HOME="$LIFE_MANAGER_HOME" LIFE_MANAGER_APPLY_TARGET=agent-economy-loop \
      LIFE_MANAGER_RELEASE_ROOT="$RELEASE_ROOT" "$RELEASE_ROOT/bin/lm-loop" apply >/dev/null
    AGENT_ECONOMY_STATE="$(LIFE_MANAGER_HOME="$LIFE_MANAGER_HOME" LIFE_MANAGER_RELEASE_ROOT="$RELEASE_ROOT" \
      "$RELEASE_ROOT/bin/lm-loop" status agent-economy-loop | jq -r '.[0].launchd_state')"
    case "$AGENT_ECONOMY_STATE" in
      loaded-*) ;;
      *) red "  ✗ unexpected Agent Economy state: $AGENT_ECONOMY_STATE"; exit 4 ;;
    esac
    green "  ✓ Agent Economy owner installed and running (ai.anicca.agent-economy-loop)"
  else
    bash "$RELEASE_ROOT/runtime/install-agent-economy-systemd.sh" "$RELEASE_ROOT" "$LIFE_MANAGER_HOME" >/dev/null
    green "  ✓ Agent Economy owner installed and running (systemd user service)"
  fi
else
  green "  ✓ disabled (LIFE_MANAGER_INSTALL_DAEMON=0); no LaunchAgent/system service changed"
fi
echo

# ─── 6. summary ────────────────────────────────────────────────────────
cyan "[6/6] done."
echo
green "Installed and verified:"
cat <<EOM
  This default self-host install prepares one isolated Agent Economy citizen and starts
  only Agent Economy when daemon installation is enabled. It does not silently start all
  14 product loops. Re-running ./install.sh preserves the citizen, wallet and private state.

  Agent Economy starts on repository-owned free compute. Paid compute remains disabled
  until verified external revenue belongs to this citizen and passes reserve/session caps.
  Sending owner funds is neither required nor treated as earned revenue.

  Other product loops need their own provider account, credentials, KYC or browser login.
  Supported guided installers today:
    ./install.sh coconala
    ./install.sh connector
    ./install.sh fundraiser
    ./install.sh job-hunter

  Inspect every registered runtime job without starting it:
    ./bin/lm-loop status all
    ./bin/lm-loop doctor

  The hosted Cloud product is already active. Local and Cloud use this same repository,
  but only loops with a proven host adapter and configured tenant are started on that host.

  Repo: https://github.com/Daisuke134/life-manager
EOM
echo
green "Life Manager install complete."
