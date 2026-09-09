#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
SANDBOX="$TEST_ROOT/facilitator"
PID_FILE="$TEST_ROOT/fake.pid"
BASE_PORT=$((20000 + ($$ % 20000)))
REQUESTED_PORT=$((BASE_PORT + 1))

cleanup() {
  if [ -s "$PID_FILE" ]; then
    FAKE_PID="$(tr -d '[:space:]' < "$PID_FILE")"
    if [[ "$FAKE_PID" =~ ^[0-9]+$ ]]; then kill "$FAKE_PID" 2>/dev/null || true; fi
  fi
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

mkdir -p "$SANDBOX/x402-rs/target/release"
mkdir -p "$TEST_ROOT/home/.anicca-signing/x402-facilitator"
cp "$SOURCE_DIR/start.sh" "$SANDBOX/start.sh"
cp "$SOURCE_DIR/tests/fake-facilitator.mjs" \
  "$SANDBOX/x402-rs/target/release/x402-facilitator"
printf '# fixture lock\n' > "$SANDBOX/x402-rs/Cargo.lock"
printf 'FACILITATOR_PRIVATE_KEY=fixture-only\nFACILITATOR_ADDRESS=0x0000000000000000000000000000000000000001\n' \
  > "$TEST_ROOT/home/.anicca-signing/x402-facilitator/.env"
chmod +x "$SANDBOX/start.sh" "$SANDBOX/x402-rs/target/release/x402-facilitator"

jq -n \
  --argjson port "$BASE_PORT" \
  '{port:$port,host:"127.0.0.1",chains:{},schemes:[]}' \
  > "$SANDBOX/config.mainnet.json"
cp "$SANDBOX/config.mainnet.json" "$SANDBOX/config.json"
BEFORE_HASH="$(shasum -a 256 "$SANDBOX/config.mainnet.json" | awk '{print $1}')"

FAKE_FACILITATOR_PID_FILE="$PID_FILE" \
  HOME="$TEST_ROOT/home" \
  FACILITATOR_STATE_DIR="$TEST_ROOT/runtime-state" \
  X402_RS_ROOT="$SANDBOX/x402-rs" \
  GIG_CHAIN=base \
  PORT="$REQUESTED_PORT" \
  /bin/bash "$SANDBOX/start.sh" >/dev/null

curl -fsS "http://127.0.0.1:$REQUESTED_PORT/health" \
  | jq -e '.ok == true' >/dev/null
AFTER_HASH="$(shasum -a 256 "$SANDBOX/config.mainnet.json" | awk '{print $1}')"
[ "$BEFORE_HASH" = "$AFTER_HASH" ]
[ -f "$TEST_ROOT/runtime-state/config.base.${REQUESTED_PORT}.json" ]
[ ! -e "$SANDBOX/state" ]
STATE_MODE="$(stat -c '%a' "$TEST_ROOT/runtime-state" 2>/dev/null || stat -f '%Lp' "$TEST_ROOT/runtime-state")"
[ "$STATE_MODE" = "700" ]

printf 'PASS start.sh binds the requested port with external runtime state and without mutating canonical config\n'
