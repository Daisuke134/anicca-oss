---
name: ubi
description: Anicca UBI (basic-income) distribution skill — the OUTFLOW engine. Splits a share of earned funds and sends it to AI + human recipients via on-chain USDC (execute-ubi.py / distribute-ubi.mjs), email wallets (Crossmint), or direct bank transfer (gmo-furikomi BulkTransfer). The earn skill calls distribute-ubi.mjs after a profitable wake; the bank/ubi watchers reconcile and pay queued recipients with at-most-once + at-least-once money-safety. Use when wiring or running UBI distribution, recording a payout, or verifying a distribution.
---

# ubi — Anicca's basic-income distribution (OUTFLOW)

Split from the `earn` skill (2026-06-21): `earn` = INFLOW (make money), `ubi` = OUTFLOW (give money). The only earn→ubi link is `earn/run.sh` calling `../ubi/distribute-ubi.mjs` after a PROFITABLE wake.

## Modules
| File | Role |
|---|---|
| `distribute-ubi.mjs` | Core: re-derive net from the earn-line (lib/ubi.mjs, pure+tested), plan the split, shell `execute-ubi.py` for the real ERC20 send. |
| `execute-ubi.py` | Real USDC transfer on Base (anicca's own key; no human). Spawned via `__dirname` (path-stable). |
| `bank-watcher.mjs` / `bank-payout-watcher.mjs` | ③ bank-direct UBI: atomic-claim → GMO BulkTransfer → completion poll. VCSDD-converged (no double-pay / no drop). |
| `gmo-furikomi.mjs` | GMO あおぞら 一括振込 (BulkTransfer) API request builder + submit. |
| `fern-payout.mjs` | (Fern dead — replace with active rail.) |
| `ubi-watcher-owner` / `ubi-payout-watcher.mjs` | Finite scheduled owner that pays queued recipients using the shared Life Manager runtime. |
| `lib/ubi.mjs` | Pure: buildRecipients / planUbi / alreadyDone. |
| `lib/bank-fanout.mjs` / `lib/bank-recipients.mjs` | Bank fan-out planning + recipient parsing. |

## Shared infra (`../_shared/lib/`)
`ledger.mjs` `usdc.mjs` `transfer.mjs` `identity-guard.mjs` `verify-tx.mjs` — used by both earn and ubi. Import as `../_shared/lib/X.mjs` (root modules) or `../../_shared/lib/X.mjs` (from `lib/`).

## Money-safety invariants (do not regress)
- at-most-once: atomic CAS claim; a re-queued submitted row (ref=) is REFUSED by parseBankRecipient (no auto-redispatch).
- at-least-once: failed → needs_review (NEVER auto-requeue irreversible money); stuck processing rows are flagged, never silently dropped.
- own-funds only: identity-guard fails closed if user-PII env leaks into the process.

## Tests
`__tests__/` — distribute-ubi, ubi, bank-watcher, bank-payout-watcher, bank-fanout, bank-recipients, gmo-furikomi, bank-chain.integration. Run: `node --test skills/ubi/__tests__/*.test.js`.
