---
name: self/spawn
description: Self-replication — a profitable parent Anicca births a child instance on DO/Akash with its OWN wallet + OWN AgentMail inbox; the child runs its first earn wake without parent instruction. Gate is deterministic (wallet >= threshold + rate-limit + concurrency cap); no human in the loop.
metadata:
  track: A
  spec: 26 A4 / 27 A-self-spawn
  entrypoint: run.sh
  parallel_safe: true
  requires:
    bins: [bash, jq, node, curl, openssl, python3]
    py: [eth_account, pycryptodome]
    env_optional: [DIGITALOCEAN_TOKEN, AGENTMAIL_API_KEY, ANICCA_WALLET_JSON, TELEMETRY_URL, ANICCA_CHILD_PREFIX, ANICCA_GEO, AKASH_KEY_NAME]
---

# self/spawn

## Why
spec27 A-self-spawn: a profitable Anicca should birth a child so the colony compounds. The child is
**pure Anicca lineage** (no Dais keys) — its own wallet, its own inbox, its own earn wake. This is the
`live/die` primitive that makes takeoff possible.

## Gate (deterministic — `lib/spawn-decision.js`, node:test-covered)
```
eligible = balance_usdc >= minBalance (20)          # own survival first
        && no child spawned in the last 14 days     # rate-limit
        && children < maxChildren (1)               # parent caps concurrency
otherwise -> dormant (silent), exit 0, reason in {low_balance, rate_limited, max_children}
```
Order is balance -> rate-limit -> cap (a broke parent never spawns, whatever else is true).

## Flow (real run, when eligible)
```
1. resolveStateDir() -> DURABLE state dir (lib/state-path.js, fail-closed: refuses /tmp). children.jsonl lives here.
2. read parent balance (state/wallet.json) + colony (state/children.jsonl)
3. decideSpawn(...)  -> eligible?  (pure, tested)
4. nextChildId(...)  -> anicca-cNNN  (monotonic, gap-safe, tested; prefix ANICCA_CHILD_PREFIX-overridable)
5. scripts/gen-wallet.sh         -> child secp256k1 wallet (600-perm temp; DISTINCT from parent, asserted;
                                    address derives identically under ethers v6 — cross-checked)
6. POST AgentMail /v0/inboxes    -> child's own inbox (AGENTMAIL_API_KEY)
7. append PROVISIONAL row to state/children.jsonl  (never lose track)
8. provision droplet (DO API, cloud-init user_data=scripts/cloud-init.sh) or lease (scripts/deploy-akash.sh)
   -> PROVIDER_ID. The droplet's cloud-init writes systemd units (clawrouter + automaton) and
   `systemctl enable --now` them, so `systemctl is-active automaton` == active on first boot.
   automaton.service boots with AUTOMATON_GOAL=earn => the child's OWN wake discovers+executes
   earn before it ever reports (not a telemetry-only heartbeat).
9. REGISTER ON LIVE DASHBOARD: scripts/sign-telemetry.py signs the child's FIRST heartbeat with its OWN
   key (EIP-191, signer==id, byte-identical to telemetry-verify.js) and POSTs to ${TELEMETRY_URL}
   (default https://aniccaai.com/.netlify/functions/telemetry). A 202 = the child now has an `instances`
   row that dashboard-sync aggregates -> the child APPEARS on aniccaai.com under its own wallet addr.
   Non-202 aborts (no fake registration).
10. append FINAL row {status:"active", provider_id, wake_action:"earn", earn_on_wake:true, dashboard_id,
    telemetry_status:202} to the DURABLE children.jsonl -> proves earn-on-wake + dashboard presence on disk.
11. print CHILD_ID / CHILD_WALLET / CHILD_INBOX / PROVIDER_ID / TELEMETRY_STATUS / DASHBOARD_ID
```
Seed: transfer $1 USDC parent->child so the child can pay its first earn wake (the child wakes on its own).

## LIVE E2E proof (2026-06-16, executed by builder on an ISOLATED test state, then cleaned up)
A real test child was born end-to-end (no human in loop, no fake) and every fact was independently re-checked:
| fact | value | re-check |
|---|---|---|
| child wallet (≠ genesis) | `0xac3aaf49eeb2ed7e23b86bbbd1ed3d2e0a20702d` | genesis = `0xa3CDd4Ec…C4C21` |
| child AgentMail inbox | `anicca-vtest001@agentmail.to` | GET `/v0/inboxes` |
| DO droplet (cloud-init automaton) | id `577986258` image `ubuntu-24-04-x64` size `s-2vcpu-2gb` | GET `/v2/droplets/577986258` |
| live telemetry POST | `202` | child signed its own EIP-191 payload |
| live dashboard | child `0xac3aaf49…` host=do status=alive (alive→5) | GET `/.netlify/functions/dashboard-sync` |
Then DESTROYED (no orphan paid instance): droplet deleted (DO 204, GET→not_found), inbox deleted, test
state dir removed. Run on an isolated `~/.hermes/state-spawn-test` so the live colony ledger stayed at 1
(anicca-c001, droplet 577904740 active). Earn-gating is honest: the child is BORN + WIRED to earn
(AUTOMATON_GOAL=earn), but ACTUAL earning is the same demand-gated GATE-0 as the parent — no child is
claimed to have earned.

## Three gaps closed (2026-06-16, verifier rejections)
- **child systemctl active**: `cloud-init.js` (apps/landing/netlify/functions/_lib) now writes
  `/etc/systemd/system/{clawrouter,automaton}.service` + `systemctl daemon-reload && enable --now`
  (Q6 step 6, verbatim). The child runs a real, restart-always service — not just an installed build.
- **child earns on its own wake**: automaton.service `ExecStart=node dist/index.js --run` with
  `Environment=AUTOMATON_GOAL=earn`; the colony row records `wake_action:"earn"` / `earn_on_wake:true`.
- **children.jsonl persisted live**: `resolveStateDir()` is fail-closed against /tmp; the ledger
  defaults to `~/.local/state/life-manager/agent-economy/instance/state` (host) /
  `/var/lib/anicca` (`StateDirectory=anicca`, droplet) — durable.

## Usage
```bash
bash run.sh --dry-run        # gate-only: prints decision JSON, touches NOTHING. exit 0.
bash run.sh                  # real run (host=do by default; --host=akash for sovereign)
ANICCA_SPAWN_HOST=akash bash run.sh
```

## NO FAKE RUN (HARD 0.24)
`--dry-run` performs ZERO side effects and says so. A real run reports success **only** after a real
provider id + a child wallet **distinct** from the parent exist; any failed step exits non-zero and
leaves an honest `provisioning`/`failed` ledger row (never a fabricated success).

## NO HUMAN IN LOOP (HARD 0.20)
Never asks "spawn OK?" or "where to host?" — the gate decides and the provider auto-bids. The child's
identity ("Daughter of Anicca <hash>") and the $1 seed are fixed, not confirmed.

## Verify (spec27 A-self-spawn)
- `node --test lib/__tests__/*.test.js` -> all pass.
- `bash run.sh --dry-run` below threshold -> `eligible:false` + reason, no ledger written.
- (runtime, post-fund) real run -> child droplet active + `CHILD_WALLET != parent` on basescan +
  the child appears on aniccaai.com/dashboard (it POSTs telemetry from its own report skill).

## Collision
This skill edits ONLY `skills/self/spawn/**`. It does NOT touch `skills/registry.json`, `install.sh`,
or any landing file. Flipping this slot's registry `status` to `"live"` is a separate one-line change
Foundation makes once runtime E2E lands.
