#!/usr/bin/env bash
# Contract test for _instance_paths.sh: queue/account paths remain compatible,
# while ledgers follow the injected Life Manager state root and stay isolated.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0

# case 1: unset ANICCA_INSTANCE => must match the pre-split defaults exactly
unset ANICCA_INSTANCE EARN_LEDGER LIFE_MANAGER_STATE_ROOT
source "$DIR/_instance_paths.sh"
[ "$CLIP_QUEUE" = "$HOME/clips/queue" ] || { echo "FAIL: default QUEUE = $CLIP_QUEUE"; FAIL=1; }
[ "$CLIP_POSTED" = "$HOME/clips/posted" ] || { echo "FAIL: default POSTED = $CLIP_POSTED"; FAIL=1; }
[ "$CLIP_ACCTS" = "$HOME/.cloak/clip-accounts.json" ] || { echo "FAIL: default ACCTS = $CLIP_ACCTS"; FAIL=1; }
[ "$CLIP_LEDGER" = "$HOME/.local/state/life-manager/clip-loop/clip-earn-ledger.jsonl" ] || { echo "FAIL: default LEDGER = $CLIP_LEDGER"; FAIL=1; }

# case 2: ANICCA_INSTANCE=clawrouter => suffixed, non-overlapping with default
(
  export ANICCA_INSTANCE=clawrouter
  unset EARN_LEDGER LIFE_MANAGER_STATE_ROOT
  source "$DIR/_instance_paths.sh"
  [ "$CLIP_QUEUE" = "$HOME/clips/queue-clawrouter" ] || { echo "FAIL: clawrouter QUEUE = $CLIP_QUEUE"; exit 1; }
  [ "$CLIP_POSTED" = "$HOME/clips/posted-clawrouter" ] || { echo "FAIL: clawrouter POSTED = $CLIP_POSTED"; exit 1; }
  [ "$CLIP_ACCTS" = "$HOME/.cloak/clip-accounts-clawrouter.json" ] || { echo "FAIL: clawrouter ACCTS = $CLIP_ACCTS"; exit 1; }
  [ "$CLIP_LEDGER" = "$HOME/.local/state/life-manager/clip-loop/clip-earn-ledger-clawrouter.jsonl" ] || { echo "FAIL: clawrouter LEDGER = $CLIP_LEDGER"; exit 1; }
  [ "$CLIP_QUEUE" != "$HOME/clips/queue" ] || { echo "FAIL: clawrouter queue collides with default"; exit 1; }
  [ "$CLIP_LEDGER" != "$HOME/.local/state/life-manager/clip-loop/clip-earn-ledger.jsonl" ] || { echo "FAIL: clawrouter ledger collides with default"; exit 1; }
) || FAIL=1

# case 3: an injected state root preserves the instance suffix
(
  export ANICCA_INSTANCE=clawrouter
  export LIFE_MANAGER_STATE_ROOT=/tmp/life-manager-clip-test
  unset EARN_LEDGER
  source "$DIR/_instance_paths.sh"
  [ "$CLIP_LEDGER" = "/tmp/life-manager-clip-test/clip-earn-ledger-clawrouter.jsonl" ] || { echo "FAIL: injected state root ignored: $CLIP_LEDGER"; exit 1; }
) || FAIL=1

# case 4: EARN_LEDGER override still wins over the injected state root
(
  export ANICCA_INSTANCE=clawrouter
  export LIFE_MANAGER_STATE_ROOT=/tmp/life-manager-clip-test
  export EARN_LEDGER=/tmp/custom-ledger.jsonl
  source "$DIR/_instance_paths.sh"
  [ "$CLIP_LEDGER" = "/tmp/custom-ledger.jsonl" ] || { echo "FAIL: EARN_LEDGER override ignored: $CLIP_LEDGER"; exit 1; }
) || FAIL=1

if [ "$FAIL" = "0" ]; then echo "ALL PASS"; else echo "TESTS FAILED"; exit 1; fi
