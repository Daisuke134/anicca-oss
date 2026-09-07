#!/usr/bin/env bash
# anicca-daemon.sh — the supervised, self-updating entrypoint for a living Anicca.
#
# Run under a KeepAlive process supervisor (for example macOS launchd or Linux systemd). The
# supervisor restarts this script whenever it exits, so Anicca stands on its own — no human runs it
# by hand. On every (re)start it:
#   1. SELF-UPDATES: git pull the mother repo so this body always runs the latest motherboard.
#   2. ensures its own brain is up — the shared free-tier LLM router (Base/EVM self-pay, or a
#      readiness-probe-only wait for Franklin — see franklin-loop-revival REQ-004/§ENGINE-PARITY).
#   3. ensures its telemetry poster is up (reports to the dashboard).
#   4. exec's the ReAct loop in the FOREGROUND — when the loop exits, this script exits, and the
#      supervisor brings the whole body back (freshly updated).
#
# ANICCA_INSTANCE selects the brain+telemetry backend (§ENGINE-PARITY-FRANKLIN 2026-07-05, THINK
# routing fixed by franklin-loop-revival REQ-004 2026-07-08): default (unset or 'clawrouter') = the
# original EVM/Base ClawRouter + telemetry-poster.mjs path, UNCHANGED. 'franklin' = Franklin's OWN
# Solana wallet (~/.blockrun/.solana-session, resolved via resolve-identity.mjs, never touched
# here) for balance/tier purposes, but THINK now reaches the SAME shared :8402 LLM router as every
# other instance (Franklin no longer runs its own dedicated proxy binary) + the ed25519
# telemetry-post-franklin.mjs poster (one-shot script, looped here since it has no built-in interval).
#
# The loop itself is already crash-resilient (while-true + per-wake try/catch); this wrapper adds
# OS-level persistence (survives reboots/logout) and keeps every Anicca in sync with the mother.
set -uo pipefail

REPO="${ANICCA_REPO:-${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}}"
[ -n "$REPO" ] || { echo "Life Manager repository could not be resolved" >&2; exit 2; }
export ANICCA_HOME="${ANICCA_HOME:-$HOME/.anicca}"
INSTANCE="${ANICCA_INSTANCE:-clawrouter}"
# franklin-loop-revival REQ-004(a)/(c): PORT resolves to ClawRouter's 8402 for EVERY instance,
# including franklin — franklin no longer runs its own dedicated `franklin proxy` on 8403 (see step
# 2 below), so its PORT must no longer derive from FRANKLIN_PROXY_PORT at all.
PORT="${COMPUTE_PROXY_PORT:-8402}"
LOGDIR="$ANICCA_HOME/logs"; mkdir -p "$LOGDIR"

log() { echo "[$(date -u +%FT%TZ)] anicca-daemon: $*" >&2; }

# franklin2-daemon-identity REQ-001: pure, deterministic instance-classification predicate — matches
# 'franklin' (unchanged, original citizen) or 'franklin' followed by a digit-run (franklin2, franklin10,
# … future spawned Franklin siblings). Fails CLOSED (exit 1 / false) on anything else, including empty,
# case variants (Franklin2), and non-digit suffixes (franklinX) — those keep today's default/EVM path
# unchanged (REQ-003/REQ-004). Used at all 3 routing call sites below (brain-probe, telemetry-poster
# choice, wallet-address derivation) so they can never diverge. Parsing a fixed machine identifier format
# via a POSIX `case` pattern here is genuine parsing, not judgment (same class as the PORT parsing
# elsewhere in this file) — no LLM-replaceable decision is being made.
is_franklin_instance() {
  local suffix
  suffix="${1#franklin}"
  [ "$suffix" = "$1" ] && return 1  # no literal 'franklin' prefix at all (case-sensitive)
  case "$suffix" in
    '') return 0 ;;             # exactly 'franklin'
    *[!0-9]*) return 1 ;;       # suffix has a non-digit char anywhere -> fail closed
    *) return 0 ;;              # suffix is a non-empty digit-run -> 'franklin<N>'
  esac
}

# 1. self-update from the mother (fast-forward only; never clobber local state) ------------------
if [ -d "$REPO/.git" ]; then
  git -C "$REPO" fetch --quiet origin main 2>/dev/null \
    && git -C "$REPO" merge --ff-only origin/main 2>/dev/null \
    && log "self-updated to $(git -C "$REPO" rev-parse --short HEAD)" \
    || log "self-update skipped (offline or diverged)"
fi
# Skill code executes directly from this immutable Life Manager release. ANICCA_HOME owns only
# per-instance identity, state, and logs; it is never a second source-code checkout or copied tree.
export LIFE_MANAGER_SKILLS_ROOT="${LIFE_MANAGER_SKILLS_ROOT:-$REPO/skills}"
export LIFE_MANAGER_SKILLS_STATE_ROOT="${LIFE_MANAGER_SKILLS_STATE_ROOT:-$ANICCA_HOME/state/skills}"
export EARN_STATE_ROOT="${EARN_STATE_ROOT:-$LIFE_MANAGER_SKILLS_STATE_ROOT/earn}"
export EARN_LEDGER="${EARN_LEDGER:-$EARN_STATE_ROOT/earn-ledger.jsonl}"
export ANICCA_STATE_DIR="${ANICCA_STATE_DIR:-$ANICCA_HOME/state}"
export ANICCA_REPO="$REPO"
mkdir -p "$EARN_STATE_ROOT"
# 2. brain: start this instance's own OpenAI-compatible proxy on $PORT if not already answering.
if is_franklin_instance "$INSTANCE"; then
  # franklin-loop-revival REQ-004(b)/REQ-005/PROP-016 (2026-07-08): Franklin's brain is the
  # ALREADY-RUNNING, SEPARATELY-launchd shared free-tier LLM-router job on :8402 (its own
  # RunAtLoad+KeepAlive plist, confirmed live — free-tier-only, no shared-wallet credential
  # configured at all). Franklin's daemon reaches it ONLY as a same-machine loopback HTTP client
  # (the PORT/OPENAI_BASE_URL fix above) — it NEVER spawns the old dedicated @blockrun/franklin
  # CLI's own "proxy" subcommand, the shared router's own binary, or any other process for this
  # instance, and NEVER reads any other instance's own env/wallet/key material to do so (that
  # would be exactly the cross-instance leakage REQ-005 forbids —
  # the non-franklin branch's own, separately-designed use of those stays untouched below and
  # never executes for INSTANCE=franklin). Retired: the port-8403 spawn of that CLI's proxy
  # subcommand with --model/--no-fallback flags (verified live 2026-07-05, now dead per
  # FIND-006/FIND-009) — this branch is AT MOST a readiness probe, a pure no-op otherwise, since
  # bringing the shared router up is exclusively ITS OWN separate launchd job's responsibility,
  # never Franklin's. export kept ONLY for runtime/dashboard/telemetry-post-franklin.mjs's own
  # labeling (unchanged, out of scope for this requirement — the poster is independent of THINK
  # routing).
  export FRANKLIN_FREE_MODEL="${FRANKLIN_FREE_MODEL:-nvidia/llama-4-maverick}"
  if ! curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
    log "waiting for the shared LLM router on :$PORT (its own launchd job brings it up — Franklin's daemon never spawns its own brain)"
  fi
else
  # ClawRouter (the real BlockRun router) on :8402. Why ClawRouter, not the old compute-proxy:
  # ClawRouter routes the nvidia/* FREE models with ZERO payment (verified: 3 free calls = $0.0000
  # USDC delta), while the old raw br.post proxy charged ~$0.02 x402 PER CALL even for "free" models —
  # bleeding the treasury ~$0.6/hr. ClawRouter still pays x402 from the wallet for PAID models, but
  # our tiers pin a free model, so routine compute = $0.
  ensure_brain() {
    command -v clawrouter >/dev/null 2>&1 || npm install -g @blockrun/clawrouter >/dev/null 2>&1 || true
    # The :8402 ClawRouter is SHARED with the OpenClaw gateway (openclaw.json baseUrl :8402), and
    # ClawRouter is :8402-only (no port split). So it MUST run on the OpenClaw instance's OWN wallet —
    # OpenClaw's many 'auto' (paid) crons then drain OpenClaw's wallet, not this self-paying anicca.
    # This loop pins a FREE model, so it costs $0 regardless of which wallet ClawRouter holds.
    local KEY; KEY=$(grep -E '^BLOCKRUN_WALLET_KEY=' "$HOME/.local/state/life-manager/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ')
    # #28: gated per-instance key (resolve-identity.mjs: EFFECTIVE_HOME first, legacy ONLY for the
    # rightful owner, foreign spawn -> empty). Never inline-read the shared $HOME/.automaton/wallet.json
    # — that let a foreign spawn pay ClawRouter's x402 compute from ANOTHER instance's REAL money.
    if [ -z "$KEY" ]; then KEY=$(node "$REPO/skills/earn/lib/resolve-identity.mjs" evm 2>/dev/null); fi
    BLOCKRUN_WALLET_KEY="$KEY" clawrouter >>"$LOGDIR/clawrouter.log" 2>&1 &
    for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1 && break; sleep 0.5; done
  }
  if ! curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
    log "starting ClawRouter on :$PORT (free models = \$0, paid via wallet x402)"
    ensure_brain
  fi
fi

# 3. telemetry poster: one instance (kill any stale one first so the dashboard never doubles) -----
if is_franklin_instance "$INSTANCE"; then
  # telemetry-post-franklin.mjs is a ONE-SHOT script (ed25519 signer over Franklin's own Solana key,
  # was previously appended to sol-trade/run.sh) — no built-in setInterval like telemetry-poster.mjs,
  # so loop it here every 120s (same cadence as the EVM poster) to keep Franklin alive on the dashboard.
  # franklin2-daemon-identity impl-review iteration-1 FIND-002 fix: pkill -f is scoped to THIS
  # instance's own $ANICCA_HOME (via the `--home` argv marker the poster is invoked with below, which
  # the script itself never reads/parses — it is present purely so this pattern can target it) so a
  # daemon restart of ONE Franklin-family instance can never kill ANOTHER concurrently-running
  # instance's in-flight poster process — both instances run the identical script path, so an
  # unscoped pattern would match both (confirmed: two live launchd jobs, ai.anicca.franklin-loop and
  # ai.anicca.franklin2-loop, each with their own distinct $ANICCA_HOME).
  pkill -f "dashboard/telemetry-post-franklin.mjs --home $ANICCA_HOME" 2>/dev/null || true
  # franklin2-daemon-identity impl-review iteration-3 FIND-001 fix: the iteration-2 one-time,
  # unscoped, end-anchored legacy-poster-cleanup pkill that used to run here is REMOVED. It was
  # meant to catch ONLY a stale poster LOOP left running by a pre-29023a55 daemon.sh (no --home argv
  # marker), but its end-anchored pattern also matched skills/earn/sol-trade/run.sh's own flagless,
  # `timeout 20`-bounded, short-lived one-shot telemetry POST — a currently-live, unmodified caller
  # of this identical script that never carries a --home marker and never will — on EVERY invocation,
  # forever, not only during a transient migration window. That made every daemon restart a chance to
  # SIGTERM a legitimate, in-flight, non-legacy process belonging to this SAME instance's own
  # sol-trade pass. No argv-shape pattern can safely tell "stale long-lived loop" apart from
  # "legitimate short-lived one-shot" here, so this is no longer done in code at all. The one-time
  # migration of any still-running pre-29023a55 legacy poster LOOP is now a documented, ONE-TIME
  # OPERATOR step performed once per instance at deploy — see behavioral-spec.md REQ-002(b)
  # "Deployment / migration runbook".
  ( export FRANKLIN_TELEMETRY_LOOP=1; while true; do node "$REPO/runtime/dashboard/telemetry-post-franklin.mjs" --home "$ANICCA_HOME" >>"$LOGDIR/poster.log" 2>&1; sleep 120; done ) &
else
  pkill -f "dashboard/telemetry-poster.mjs" 2>/dev/null || true
  sleep 1
  node "$REPO/runtime/dashboard/telemetry-poster.mjs" >>"$LOGDIR/poster.log" 2>&1 &
fi

# 4. brain endpoint + model the loop should use -------------------------------------------------
export OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1"
export OPENAI_API_KEY="${OPENAI_API_KEY:-x402-local}"
if is_franklin_instance "$INSTANCE"; then
  # franklin-loop-revival REQ-001: derive Franklin's OWN Solana wallet address (base58 pubkey) via
  # the gated per-instance resolve-identity.mjs::resolveSolanaSecret path (never bare-grepped, never
  # falls back to scanning another instance's dot-directory — REQ-005/REQ-006). A missing or
  # cryptographically malformed secret prints nothing (the helper warns to stderr and exits 0),
  # leaving ANICCA_WALLET_ADDRESS unset — non-fatal, balance.mjs/tier.mjs simply keep tier=broke.
  export ANICCA_WALLET_ADDRESS="${ANICCA_WALLET_ADDRESS:-$(node "$REPO/runtime/wallet-address-solana.mjs" 2>/dev/null)}"
else
  # derive the wallet address (viem, from the privateKey) via the helper, run where viem resolves.
  export ANICCA_WALLET_ADDRESS="${ANICCA_WALLET_ADDRESS:-$(cd "$REPO/runtime/compute-proxy" && node "$REPO/runtime/wallet-address.mjs" 2>/dev/null)}"
fi

log "exec loop (model tiers from config; funded=$(node -e 'import("'"$REPO"'/runtime/loop/config.mjs").then(m=>console.log(m.loadConfig(process.env,"").ANICCA_FUNDED_MODEL)).catch(()=>console.log("?"))' 2>/dev/null))"
# 5. run the loop in the foreground — its exit (crash/shutdown) ends this script; supervisor restarts.
exec node "$REPO/runtime/loop/index.mjs"
