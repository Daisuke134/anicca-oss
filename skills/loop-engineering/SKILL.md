---
name: loop-engineering
description: Use when building, fixing, releasing or operating a Life Manager loop, adding a marketplace lane, or deciding whether existing loop components must be reused.
---

# Loop Engineering

One architecture router, so no lane invents what another lane already owns.
This file routes; release and launchd work still requires the focused
`loop-development` subskill.

```text
loop config -> reusable recipe -> shared runtime -> provider adapter -> official provider
```

Dependencies run one way only. `runtime/` owns scheduling, admission, model
routing, checkpoint/resume, retries, replay prevention, receipts and recovery,
and knows no marketplace rules. A recipe owns one business lifecycle (Apply,
Negotiate, Storefront, Paid) and knows no DOM selector or credential. An adapter
owns only official observation, mutation and readback. A loop config selects a
recipe, provider, cadence and policy — it never implements a second runner,
retry engine, ledger, browser launcher or model client.

## One loop, two host adapters

Local/self-hosted and cloud run one logical loop from the same repository-owned
implementation. They share the loop ID, recipe, model/tool and provider
contracts, job/event/effect/receipt/outbox schemas, replay fence and tests. Only
the supervisor, durable-storage adapter, secret store and browser transport vary.

Host adapters implement the shared contracts; they do not own business
decisions, retry/replay policy or a second runner. Do not create local/cloud
copies, including temporary copies, and do not add deployment machinery such as
a Dockerfile unless the measured target runtime requires it. An unavailable host
capability fails closed instead of forking the workflow. The target shape and
migration status live in
`docs/superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md`.

## Route by task

| Task | Read |
|---|---|
| Change a loop, its cadence, release or plist | `skills/loop-development/SKILL.md` |
| Build or fix an Apply lane on any marketplace | `references/marketplace-apply-lane.md` |
| Build or fix a Paid/Fulfillment lane on any marketplace | `references/marketplace-paid-lane.md` |
| Decide whether a failure may end a wake, or add a retry | `references/transient-vs-fatal.md` |
| Decide whether a loop may earn on a platform, and what to ask a human for | `references/platform-automation-map.md` |
| Reuse the shared marketplace runtime | `skills/_shared/marketplace-core/scripts/` |
| Sell the same catalogue on a new platform | `skills/gig-work/profile/listings/catalog.json` |
| Lane ownership and parallelism rules | spec §6.2A, `docs/superpowers/specs/2026-08-22-life-manager-gig-economy-loop-design.md` |

## Before writing code

Search `runtime/`, existing recipes, provider adapters and `skills/_shared/`.
Reuse what is there. A new abstraction is prohibited for one speculative
consumer — the second real consumer is the extraction trigger.

For a new or migrated loop, name the shared core and smallest host adapters
before editing. Mixed business and host code is a boundary to extract, not a
reason to duplicate the loop.

## Lane ownership

Apply alone submits applications. Negotiate alone replies to buyer threads.
Storefront alone mutates listings. Paid alone fulfils orders. Effects are
disjoint by construction, so there is no cross-lane lock, shared queue, sibling
wait or effect arbitration. Each owner prevents replay from its own durable
state and reconciles an uncertain result from official state without pausing any
other owner.

## Completion

Completion is official provider readback, never process liveness, never a clean
exit code. A lane that ran and reported success without a receipt has not
completed.
