#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d /tmp/founder-migrate.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
SRC="$WORK/legacy"; DST="$WORK/canonical"
mkdir -p "$SRC/state"
printf '{"address":"0x1111111111111111111111111111111111111111"}\n' > "$SRC/wallet.json"
printf 'old state\n' > "$SRC/STATE.md"
printf '{"earn_usdc":1}\n' > "$SRC/state/earn-ledger.jsonl"

first=$(bash "$HERE/migrate-legacy-state.sh" "$SRC" "$DST")
cmp "$SRC/wallet.json" "$DST/wallet.json"
cmp "$SRC/state/earn-ledger.jsonl" "$DST/state/earn-ledger.jsonl"
printf 'canonical wins\n' > "$DST/STATE.md"
second=$(bash "$HERE/migrate-legacy-state.sh" "$SRC" "$DST")
grep -q 'canonical wins' "$DST/STATE.md"
grep -q '"copied":3' <<<"$first"
grep -q '"copied":0' <<<"$second"
test "$(stat -f%Lp "$DST/wallet.json")" = 600
test "$(stat -f%Lp "$DST/state")" = 700
ln -s "$WORK/outside" "$SRC/state/escape"
if bash "$HERE/migrate-legacy-state.sh" "$SRC" "$WORK/rejected" >/dev/null 2>&1; then exit 1; fi
rm "$SRC/state/escape"
mv "$SRC/wallet.json" "$SRC/wallet.real"
ln -s "$SRC/wallet.real" "$SRC/wallet.json"
if bash "$HERE/migrate-legacy-state.sh" "$SRC" "$WORK/rejected-root" >/dev/null 2>&1; then exit 1; fi
echo 'PASS: founder legacy state copies once, preserves target, rejects symlinks, keeps private modes'
