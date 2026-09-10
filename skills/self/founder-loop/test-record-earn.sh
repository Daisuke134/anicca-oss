#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# VSDD oracle for record-earn.mjs (G1.1-A2, external-inflow model). THE GOAL: an earning is ONLY external USDC credited
# to the founder wallet (from ∉ my wallets), summed by block cursor — a self-transfer can NEVER fabricate an earning.
# Plus all sprint-1..5 anti-fake invariants (seam-gating, wallet pin, ledger realpath, env-independent prod root,
# fail-closed, atomic cursor advance).
set -uo pipefail
M="$LIFE_MANAGER_REPO/skills/self/founder-loop/record-earn.mjs"
FW="0x810f6d61f7606deee2657d3083e150a222bc29c5"
A3="0xa3CDd4Ec6b94F01826Aaf90a6d5538A2Aa8C4C21"   # automaton = MY wallet (internal)
EXT="0x1111111111111111111111111111111111111111" # external payer
fails=0
ok(){ [ "$1" = 1 ] || { echo "  - FAIL $2"; fails=$((fails+1)); }; }
mkdir_dir(){ local d; d="$(mktemp -d)"; mkdir -p "$d/state"; printf '{"address":"%s"}' "$1" > "$d/wallet.json"; echo "$d"; }

# ---------- STATIC ----------
src="$(cat "$M")"
src_code="$(sed 's|//.*||' "$M")"   # FIND-703: strip line comments so a comment's "TEST &&"/"TEST ?" cannot false-pass the seam gate
for seam in FOUNDER_WALLET FOUNDER_LEDGER FOUNDER_CURSOR FOUNDER_BLOCK_NOW FOUNDER_LOGS_JSON FOUNDER_RAW_LOGS_JSON BASE_RPC_URL HOME; do
  bad="$(grep -n "process\.env\.$seam" <<<"$src_code" | grep -vE "TEST (&&|\?)" || true)"
  ok "$([ -z "$bad" ] && echo 1 || echo 0)" "STATIC: $seam read is TEST-gated (${bad:-none})"
done
ok "$(grep -q 'const FOUNDER_DIR = TEST' <<<"$src" && echo 1 || echo 0)" "STATIC: FOUNDER_DIR test seam remains TEST-gated"
ok "$(grep -q "renameSync" <<<"$src" && echo 1 || echo 0)" "STATIC: cursor written atomically (renameSync)"
ok "$(grep -q 'realpathSync(FOUNDER_DIR)' <<<"$src" && echo 1 || echo 0)" "STATIC: ledger realpath symlink-deref — INV-3"
ok "$(grep -q 'LIFE_MANAGER_STATE_ROOT' <<<"$src" && grep -q 'founder-loop-cadence' <<<"$src" && echo 1 || echo 0)" "STATIC: prod root follows the canonical registry-owned state contract"
ok "$(grep -q 'must equal the canonical' <<<"$src" && echo 1 || echo 0)" "STATIC: production rejects a redirected state root"
ok "$(grep -q 'MY_WALLETS' <<<"$src" && echo 1 || echo 0)" "STATIC: external-payer check (MY_WALLETS) present — INV-7"

# ---------- BEHAVIORAL ----------
# 1. first run (no cursor) → init cursor, record nothing
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_BLOCK_NOW=200 node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ ! -s "$T/state/earn-ledger.jsonl" ] && [ "$(cat "$T/state/block-cursor.txt" 2>/dev/null)" = "200" ] && echo 1 || echo 0)" "first run: cursor init=200, NO row (rc=$rc — $OUT)"

# 2. EXTERNAL payment → recorded; cursor advances (no re-scan = no double-count)
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$EXT\",\"value\":5}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ "$(jq -r '.earn_usdc' "$T/state/earn-ledger.jsonl" 2>/dev/null | tail -1)" = "5" ] && [ "$(cat "$T/state/block-cursor.txt")" = "200" ] && echo 1 || echo 0)" "external +5 recorded, cursor→200 (rc=$rc)"

# 3. ★THE GOAL★ self-transfer only → ZERO earning, no row
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$A3\",\"value\":9}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ ! -s "$T/state/earn-ledger.jsonl" ] && echo 1 || echo 0)" "self-transfer ONLY: no earning (a self-payment can't fake income) — INV-7 (rc=$rc)"

# 4. MIXED → only external counted
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$EXT\",\"value\":5},{\"from\":\"$A3\",\"value\":3},{\"from\":\"$FW\",\"value\":2}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ "$(jq -r '.earn_usdc' "$T/state/earn-ledger.jsonl" 2>/dev/null | tail -1)" = "5" ] && echo 1 || echo 0)" "mixed: only external 5 counted (self 3+2 ignored, rc=$rc)"

# 5. corrupt cursor → fail-closed
T="$(mkdir_dir "$FW")"; printf 'abc' > "$T/state/block-cursor.txt"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_BLOCK_NOW=200 node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && [ ! -s "$T/state/earn-ledger.jsonl" ] && echo 1 || echo 0)" "corrupt cursor → fail-closed (rc=$rc)"

# 6. INV-1 shared wallet rejected
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_WALLET="$A3" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$EXT\",\"value\":9}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && echo 1 || echo 0)" "INV-1: shared automaton wallet rejected (rc=$rc)"

# 7. INV-1 pin: non-shared, non-expected wallet rejected
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_WALLET="$EXT" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && echo 1 || echo 0)" "INV-1 pin: non-expected wallet rejected (rc=$rc)"

# 8. INV-3 dashboard render path rejected
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_LEDGER=$LIFE_MANAGER_REPO/runtime/dashboard/earn.jsonl FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$EXT\",\"value\":9}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && echo 1 || echo 0)" "INV-3: runtime/dashboard ledger rejected (rc=$rc)"

# 9. INV-3 symlink escape rejected
T="$(mkdir_dir "$FW")"; EVIL="$(mktemp -d)"; rm -rf "$T/state"; ln -s "$EVIL" "$T/state"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$EXT\",\"value\":9}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && [ ! -s "$EVIL/earn-ledger.jsonl" ] && echo 1 || echo 0)" "INV-3: symlinked state/ escaping founder dir rejected (rc=$rc)"

# 10. missing wallet.json → fail-closed
T="$(mktemp -d)"; mkdir -p "$T/state"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_BLOCK_NOW=200 node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && echo 1 || echo 0)" "missing wallet.json → fail-closed (rc=$rc)"

# 11. block height backwards → fail-closed
T="$(mkdir_dir "$FW")"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=300 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && echo 1 || echo 0)" "block backwards (now<cursor) → fail-closed (rc=$rc)"

# 12. PROD uses only the explicit canonical state root, never a planted legacy home.
PLANT="$(mktemp -d)"; TARGET="$(mkdir_dir "$FW")"; echo 100 > "$TARGET/state/block-cursor.txt"
LIFE_MANAGER_STATE_ROOT="$TARGET" HOME="$PLANT" node "$M" >/dev/null 2>&1
ok "$([ ! -e "$PLANT/.anicca-founder" ] && echo 1 || echo 0)" "PROD: explicit canonical root ignores planted legacy home"

# ----- RAW eth_getLogs parse path (FIND-602: the real from-slice/BigInt/topic checks, never tested before) -----
TT="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"   # Transfer topic0
USDCADDR="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TO_F="0x000000000000000000000000${FW#0x}"                  # padded to-topic = founder
FROM_EXT="0x000000000000000000000000${EXT#0x}"; FROM_A3="0x000000000000000000000000${A3#0x}"
V5="0x4c4b40"   # 5,000,000 = 5 USDC (6 decimals)

# 13. RAW: external payer decoded from a real log → recorded 5 (exercises from-slice + BigInt decode = INV-7 heart)
T="$(mkdir_dir "$FW")"
RAW="[{\"address\":\"$USDCADDR\",\"topics\":[\"$TT\",\"$FROM_EXT\",\"$TO_F\"],\"data\":\"$V5\"}]"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_RAW_LOGS_JSON="$RAW" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ "$(jq -r '.earn_usdc' "$T/state/earn-ledger.jsonl" 2>/dev/null | tail -1)" = "5" ] && echo 1 || echo 0)" "RAW parse: external 5 USDC decoded from real log (rc=$rc — $OUT)"

# 14. RAW: a self-transfer log → 0 (the from-extraction must correctly identify MY wallet)
T="$(mkdir_dir "$FW")"
RAW="[{\"address\":\"$USDCADDR\",\"topics\":[\"$TT\",\"$FROM_A3\",\"$TO_F\"],\"data\":\"$V5\"}]"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_RAW_LOGS_JSON="$RAW" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ ! -s "$T/state/earn-ledger.jsonl" ] && echo 1 || echo 0)" "RAW parse: self-transfer log → 0 (rc=$rc)"

# 15. RAW: non-USDC address + to≠founder + wrong topic0 are ALL dropped (don't trust the RPC filter — FIND-603)
T="$(mkdir_dir "$FW")"; TO_OTHER="0x0000000000000000000000001111111111111111111111111111111111111111"
RAW="[{\"address\":\"0xDEADbeef00000000000000000000000000000000\",\"topics\":[\"$TT\",\"$FROM_EXT\",\"$TO_F\"],\"data\":\"$V5\"},{\"address\":\"$USDCADDR\",\"topics\":[\"$TT\",\"$FROM_EXT\",\"$TO_OTHER\"],\"data\":\"$V5\"},{\"address\":\"$USDCADDR\",\"topics\":[\"0xdeadbeef\",\"$FROM_EXT\",\"$TO_F\"],\"data\":\"$V5\"}]"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_RAW_LOGS_JSON="$RAW" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ ! -s "$T/state/earn-ledger.jsonl" ] && echo 1 || echo 0)" "RAW parse: non-USDC + to≠founder + wrong-topic0 all dropped (rc=$rc)"

# 16. multiple external payers + a 0-value transfer → sum of externals only (FIND-604)
T="$(mkdir_dir "$FW")"; EXT2="0x2222222222222222222222222222222222222222"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$EXT\",\"value\":5},{\"from\":\"$EXT2\",\"value\":2.5},{\"from\":\"$EXT\",\"value\":0}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ "$(jq -r '.earn_usdc' "$T/state/earn-ledger.jsonl" 2>/dev/null | tail -1)" = "7.5" ] && echo 1 || echo 0)" "multiple external payers + 0-value → 7.5 (rc=$rc)"

# 17. STATIC: scans the FINALIZED head, not eth_blockNumber (no reorg over/double-count) — FIND-601
ok "$(grep -qF '["finalized", false]' <<<"$src" && echo 1 || echo 0)" "STATIC: scans the FINALIZED head (eth_getBlockByNumber finalized), not un-finalized latest — FIND-601"

# ----- blockNow REAL path via a mock JSON-RPC (FIND-705: no FOUNDER_BLOCK_NOW — exercise eth_getBlockByNumber finalized) -----
MOCKJS="$(dirname "$M")/mock-rpc.mjs"; PORT=8731
RAWLOG="[{\"address\":\"$USDCADDR\",\"topics\":[\"$TT\",\"$FROM_EXT\",\"$TO_F\"],\"data\":\"$V5\"}]"
startmock(){ MOCK_FINALIZED="$1" MOCK_LOGS="${2:-[]}" MOCK_PORT=$PORT node "$MOCKJS" >/dev/null 2>&1 & MPID=$!; sleep 1; }
stopmock(){ kill $MPID 2>/dev/null; wait $MPID 2>/dev/null; }

# 18. REAL path: finalized=200 + external log → records 5, cursor→200 (real finalized-decode + real getLogs parse)
T="$(mkdir_dir "$FW")"; startmock 0xc8 "$RAWLOG"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 BASE_RPC_URL="http://127.0.0.1:$PORT" node "$M" 2>&1)"; rc=$?; stopmock
ok "$([ $rc -eq 0 ] && [ "$(jq -r '.earn_usdc' "$T/state/earn-ledger.jsonl" 2>/dev/null | tail -1)" = "5" ] && [ "$(cat "$T/state/block-cursor.txt")" = "200" ] && echo 1 || echo 0)" "REAL RPC: finalized=200 + ext log → +5, cursor=200 (rc=$rc — $OUT)"

# 19. REAL path: finalized=null → fail-closed
T="$(mkdir_dir "$FW")"; startmock null
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 BASE_RPC_URL="http://127.0.0.1:$PORT" node "$M" 2>&1)"; rc=$?; stopmock
ok "$([ $rc -ne 0 ] && echo 1 || echo 0)" "REAL RPC: finalized=null → fail-closed (rc=$rc)"

# 20. REAL path: finalized.number=null → fail-closed
T="$(mkdir_dir "$FW")"; startmock numbernull
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 BASE_RPC_URL="http://127.0.0.1:$PORT" node "$M" 2>&1)"; rc=$?; stopmock
ok "$([ $rc -ne 0 ] && echo 1 || echo 0)" "REAL RPC: finalized.number=null → fail-closed (rc=$rc)"

# 21. FIND-701: a filter-passing log with malformed data="0x" reaches BigInt → fail-closed, no row
T="$(mkdir_dir "$FW")"
RAWBAD="[{\"address\":\"$USDCADDR\",\"topics\":[\"$TT\",\"$FROM_EXT\",\"$TO_F\"],\"data\":\"0x\"}]"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_RAW_LOGS_JSON="$RAWBAD" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -ne 0 ] && [ ! -s "$T/state/earn-ledger.jsonl" ] && echo 1 || echo 0)" "FIND-701: malformed data=0x (filter-passing) → fail-closed (rc=$rc)"

# 22. 2026-07-12 fix: the forensically-identified funding/bridge address (source of the two
# misrecorded "x402 earn $22.97"/"$7.98" ledger rows) must NEVER count as an external earning again.
T="$(mkdir_dir "$FW")"; FUNDER="0xf70da97812cb96acdf810712aa562db8dfa3dbef"
OUT="$(FOUNDER_TEST=1 FOUNDER_DIR="$T" FOUNDER_CURSOR=100 FOUNDER_BLOCK_NOW=200 FOUNDER_LOGS_JSON="[{\"from\":\"$FUNDER\",\"value\":22.97},{\"from\":\"$EXT\",\"value\":3}]" node "$M" 2>&1)"; rc=$?
ok "$([ $rc -eq 0 ] && [ "$(jq -r '.earn_usdc' "$T/state/earn-ledger.jsonl" 2>/dev/null | tail -1)" = "3" ] && echo 1 || echo 0)" "2026-07-12: known funding/bridge address (0xf70da978…) excluded — only the real external 3 counts, not 25.97 (rc=$rc)"

[ $fails -eq 0 ] && { echo "PASS — founder record-earn (external-inflow + finalized + raw-parse): external-only (self=0, INV-7) + real-log parse (from-slice/BigInt/to/address re-verify) + finalized-head (no reorg) + seam-gating + wallet-pin + realpath + env-independent root + fail-closed + atomic cursor"; exit 0; } || { echo "FAIL ($fails)"; exit 1; }
