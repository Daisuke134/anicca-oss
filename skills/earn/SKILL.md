---
name: earn
description: Anicca earn skill (GATE-0). The automaton loop calls run.sh each wake to discover and execute an earn (x402 / 0xwork / litcoin / nookplot), then VERIFIES it on-chain (tx receipt 0x1 + USDC before/after delta) and appends one line to state/earn-ledger.jsonl. One profitable wake (net>0 AND status 0x1) is the real launch gate. Use when wiring earn into the agent loop, recording an earn outcome, or verifying a profitable wake.
---

# earn — Anicca's GATE-0 money-maker

Spec: `docs/superpowers/specs/anicca/patches/A-earn-gate0.patch.md` (26 A3 / 27 A-earn).

Canonical runtime home: `$LIFE_MANAGER_REPO/skills/earn/` (this dir is the committed source of truth,
shipped via the anicca-products repo so the contract lands on main/aniccaai.com; the runtime
syncs it into the agent body). The registry slot `earn` flips `declared -> live` once a real
profitable wake is verified on-chain.

## What it does (no human, no Claude in the loop)
1. The automaton loop invokes `run.sh` each wake.
2. **discover** mode (default): records a narrate line (no tx) so the ledger shows the wake. Never GATE-0.
3. **execute** mode: the agent did an on-chain earn this wake → `run.sh` verifies the receipt
   (`../_shared/lib/verify-tx.mjs` → `eth_getTransactionReceipt` → `0x1`), records the line with the derived
   `net_usdc`, and only declares **GATE-0 MET** when net>0 AND status is `0x1`.

## Entrypoint
```bash
# discovery wake (no on-chain action yet)
EARN_SOURCE=x402 ./run.sh

# executed earn (the automaton sets these after a real on-chain receipt)
EARN_MODE=execute EARN_TX=0x<64hex> EARN_SOURCE=x402 \
  EARN_AMOUNT=<gross_usdc> EARN_COST=<cost_usdc> EARN_TASK=<id> ./run.sh
```

Env (mirrors the report skill): `/opt/anicca.env` sourced if present; wallet privkey var named
by `PKVAR` (default `BLOCKRUN_WALLET_KEY`) derives the wallet address for the USDC delta proof.
Optional: `BASE_RPC_URL`, `USDC_ADDRESS`, `EARN_LEDGER`, `WAKE_ID`.

## Ledger (`state/earn-ledger.jsonl`, append-only)
```json
{"ts":...,"wallet":"0x..","source":"x402","task":"...","earn_usdc":0.5,"cost_usdc":0.05,
 "net_usdc":0.45,"tx":"0x..","status":"0x1","wake":"..."}
```
`../_shared/lib/ledger.mjs` never rewrites prior lines (immutability). `isProfitable()` is the single
GATE-0 classifier. `narrate`-only lines (no `tx`) never count.

## P1 fail-closed CUMULATIVE guard (spec §3 P1 / §4) — the one-line pattern for every earn skill
`isProfitable()` only judges ONE pass. `../_shared/lib/earn-guard.mjs` adds the CUMULATIVE layer the
spec's hard invariant actually needs: 稼ぐ額 >>> 使う額 over TIME, not per pass. It sums `net_usdc`
across the whole ledger for a `{wallet, source}` scope (per-skill) AND a `{wallet}`-only scope
(per-agent, every source combined) and HALTs — fail-closed — the instant either cumulative total
would go negative, or the instant any matching line's `earn_usdc`/`cost_usdc`/`net_usdc` isn't a real
number (a malformed line is treated as unverifiable, never as harmless/zero).

`record.mjs`'s JS API (`record()`) already returns this as `{ line, profitable, halt }` — nothing else
to wire if a skill only ever calls `record()` directly. For a shell entrypoint, add ONE line mirroring
the existing kill-switch idiom (`polymarket-trade/run.sh`'s shared external KILL check), checked
BEFORE doing anything that wake:
```bash
if ! node "$HERE/../_shared/lib/earn-guard.mjs" check "$WALLET" "$SOURCE" "$LEDGER"; then
  echo "P1 GUARD: cumulative net breach — HALT (fail-closed), skipping wake."; exit 0
fi
```
`$SOURCE` may be `""` to check the per-agent (wallet-wide) scope only, when the wake doesn't yet know
which specific source it will use this pass. Exit 0 = solvent, exit 1 = HALT (reason on stderr).

**Do NOT gate this call on `$WALLET` being non-empty** (e.g. `if [ -n "$WALLET" ] && ! node ...`).
That was FIND-A (adversary round 2): when identity resolution fails, `$WALLET` is empty, the `&&`
short-circuits, and the guard never runs at all — the wake proceeds and its loss gets recorded under
a literal `"unknown"` wallet, invisible to every real-wallet-scoped check forever. ALWAYS call the
guard unconditionally; `earn-guard.mjs check` itself fail-closed HALTs (exit 1, reason
`missing-wallet`) on an empty/missing wallet — a broken identity is exactly when the wake must NOT
proceed, never a reason to skip the check.

**Wired today:**
- `earn/run.sh` — the guard clause above, checked once at the very top (wallet-wide scope; every
  strategy branch — 0xwork/yield/swap/hl/token — inherits it for free).
- `polymarket-trade/redeem.py` — after EVERY redeemed condition, `check_cumulative_halt()` calls the
  same CLI (scope `{wallet: DEPOSIT_WALLET, source: "polymarket-redeem"}`); on HALT it writes
  `~/.local/state/life-manager/polymarket/KILL`, so the trading entrypoint's shared kill-switch check stops the
  NEXT pass. `PM_KILL_SWITCH` may select another installation-owned absolute path, but both writers and readers
  receive that same path and reject any value that resolves inside the repository.

**Not yet wired** (these don't append to `state/earn-ledger.jsonl` yet, so there's nothing cumulative
to guard): `hl-trade`, `sol-trade`, `x402-sell`. Once any of them starts calling `record.mjs`/
`record_ledger_line`-style writes, add the one-liner above at its own pass boundary.

## UBI (spec 28 §0) — share OWN earnings with AI + human recipients
After a wake records **PROFITABLE** (real external USDC, `isProfitable`), `run.sh` calls
`../ubi/distribute-ubi.mjs`: it sends `UBI_SHARE_BPS` (default 10.00%) of THIS wake's net, split equally,
to sibling-AI child wallets (`self/spawn/state/children.jsonl`) + a human allow-list
(`UBI_HUMAN_WALLETS` / `state/ubi-recipients.json`), via one ERC20 USDC `transfer` each
(`0xa9059cbb`, USDC `0x8335…2913`, 6dp — ctx7-verified). It funds ONLY from Anicca's OWN wallet and
touches ZERO user identity (earn-side of the spec 28 §3 wall). Idempotent per `wake`; never sends
more than earned; `UBI_DRY_RUN=1`/below-min/no-recipients → record `dry`/`skipped` (NO fake send).
Audit trail: append-only `state/ubi-ledger.jsonl` (`{kind:"ubi",wake,outcome,txs:[{to,tx,status}]}`)
— separate from the earn ledger so the GATE-0 classifier is untouched.

## Verify (independent agent)
```bash
node -e "import('../_shared/lib/verify-tx.mjs').then(m=>m.receiptStatus('0x<tx>')).then(console.log)"  # -> 0x1
node -e "import('../_shared/lib/usdc.mjs').then(m=>m.usdcBalance('0x<wallet>')).then(console.log)"      # before/after delta>0
node --test lib/__tests__/*.test.mjs __tests__/*.test.js                                            # record + P1 halt wiring
node --test ../_shared/lib/__tests__/earn-guard.test.js                                             # cumulative guard, pure
```
Acceptance: wallet USDC `after-before>0` for the wake + a ledger line whose Base receipt is `0x1`.
Narration alone FAILS (HARD 0.24/0.31).
