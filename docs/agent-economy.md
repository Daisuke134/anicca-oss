# Agent Economy

Agent Economy is Life Manager's first financially independent loop. One Life Manager instance creates one isolated
citizen identity and one Base wallet. The citizen may earn externally verified revenue, record its compute and
hosting costs, and pay those costs from its own earned balance. It never borrows the user's wallet and never counts
an owner deposit, self-transfer, pending award, or model claim as revenue.

Actual profit is not a repository-installation requirement. A new citizen starts on the verified free compute route.
Paid compute is enabled only when an official external receipt proves revenue owned by that citizen and the treasury
policy still satisfies its reserve and per-session cap. Without that proof, the route fails closed to free compute.

## One implementation, two runtimes

```text
repository-owned Agent Economy contracts
├── identity + wallet
├── earning and cost adapters
├── FinancialRecord + provider receipts
├── treasury and compute-routing policy
├── Telegram transition/daily delivery
└── bounded wake + next-wake scheduling
    ├── Local: private filesystem state + launchd/systemd adapter
    └── Cloud: tenant Postgres/private signer + Railway worker adapter
```

Local and Cloud run the same wake and economic contracts. They differ only in supervisor, private-state store and
signer adapter. Neither runtime imports code from OpenClaw, Hermes, Franklin, another checkout, or a user-specific
home directory. The Cloud runtime does not require the user's computer to stay online.

## Local setup

```bash
git clone https://github.com/Daisuke134/life-manager.git
cd life-manager
./install.sh
./bin/lm-loop status agent-economy-loop
```

`./install.sh` creates and preserves one citizen identity and wallet, installs the repository-owned compute proxy and
Agent Economy owner when daemon installation is enabled, and starts the loop. Use
`LIFE_MANAGER_INSTALL_DAEMON=0 ./install.sh` for a daemon-free installation check. Private identity, signer, state,
receipts and logs stay under the selected `LIFE_MANAGER_HOME`, outside Git.

No owner wallet or ChatGPT subscription is required by the Agent Economy contract. Optional earning providers may
need their own credentials. A missing optional provider reports `setup_required` without blocking wallet-native lanes.

## Cloud setup

The unavoidable first Telegram `/start` creates or resumes the tenant. Provisioning creates exactly one encrypted
citizen wallet and one idempotent initial Agent Economy job. The Railway capability worker executes one bounded shared
wake, records its terminal receipt, and transactionally schedules the next wake. `/economy` is an optional status and
emergency-control view; ordinary operation needs no command and no local device.

## Telegram experience

Life Manager reports verified transitions immediately and sends a deduplicated daily financial snapshot. Unchanged
wakes remain silent.

```text
Agent Economy
Status: running on free compute
Wallet: 0x12…89ab (Base)
Verified external revenue: USDC 0.00
Compute cost: USDC 0.00
Hosting cost: USDC 0.00
Reserve/runway: not yet funded by earnings
Next action: continue wallet-native earning lanes
```

Immediate messages cover verified revenue, refund/chargeback, compute or hosting payment, reserve breach, funding-mode
change and a terminal inability to continue. Telegram delivery is claimed durably and stores the provider message ID;
an unknown send is quarantined rather than blindly retried.

## Current truth

- Local zero-command citizen bootstrap and supervised wake are implemented.
- Cloud `/start` provisioning, encrypted signer boundary, bounded wake scheduling and `/economy` projection are
  implemented and production-proven.
- Local and Cloud financial transitions share the same receipt-backed Telegram contract.
- Free bootstrap and receipt/reserve/session-gated paid routing are implemented and tested without spending real funds.
- Live profit, a real self-funded compute purchase, other-loop funding and citizen replication are not cleanup gates
  and are not claimed here.

The detailed architecture, evidence and remaining portability work live in
[`docs/superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md`](superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md).
