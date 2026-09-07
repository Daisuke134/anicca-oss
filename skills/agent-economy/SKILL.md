---
name: agent-economy
description: Read-first control skill for verified external revenue, treasury policy, self-funded compute, and shelter graduation.
metadata:
  track: A
  status: dormant
  risk: safe
  requires:
    bins: [node]
---

# agent-economy

This is the public control-plane skill for a financially independent Life Manager agent. It does not
invent a wallet, receive a private key, or broadcast a payment when called. Its default pass reconciles
delayed receipts and reports the truth that the other earn slots use.

## Source of truth

- `lib/money-truth.mjs` joins delayed EVM receipt results without rewriting `earn-ledger.jsonl`.
- `receipt-reconciliations.jsonl` is the append-only tx-keyed correction sidecar for delayed receipts.
- `lib/treasury-policy.mjs` defines reserve, committed-liability, session-cap, and graduation rules.
- `../earn/taskmarket/` is the first work lane: one official TaskMarket job, bounded BlockRun image spend,
  submission readback, and a separate award observer.
- `../earn/x402-sell/` is the passive seller lane. Keep one stable product and measure external settlements;
  self-pay probes are not revenue.

## Compute policy

Franklin/BlockRun x402 is the primary self-funded compute rail. OpenRouter is optional only when an account
already has credits; this skill does not automate a browser credit purchase. A free model is an emergency
floor, never evidence that the agent is profitable.

The release-backed `agent-economy` daemon may create one empty owner-only EVM wallet under its own runtime
home. Creation moves no funds; funding remains an explicit external action.

## Money rules

1. Treat only externally verified receipts as revenue.
2. Preserve the reserve and committed liabilities before any spend.
3. Keep trading/yield in the surplus-only lane; paid work and stable service sales come first.
4. Do not claim graduation until 30-day external realized net covers compute plus shelter by the policy margin,
   liquid runway is at least 30 days, and human-paid inference is zero.

## Run

```bash
bash "$LIFE_MANAGER_REPO/skills/agent-economy/run.sh"
```

The output is one JSON summary. `unverified_external_rows` is visible but contributes zero to
`external_net_usdc`. A missing RPC receipt remains retryable; it is never silently treated as success.

Mutable ledgers and cost journals live under
`~/.local/state/life-manager/agent-economy/`. Set `AGENT_ECONOMY_STATE_ROOT` to override it.
Before the first registry-managed release, run `migrate-legacy-state.sh`; it copies the old earn,
compute, and shelter journals without deleting or overwriting their source.
