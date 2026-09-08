# Zenn deferred retry operations

Zenn rolling-window recovery is an asynchronous, Zenn-only path. The durable queue is the set of
`state/runs/*/gates/zenn-deferred.json` artifacts whose `status` is `pending` or `live-recorded`
(legacy `waiting` is accepted and handed off after exact8 state validation). Run pruning always protects
these non-terminal artifacts, even when they are older than the newest 30 run directories.

## Runtime contract

1. `article-daily.sh` persists all exact8 stable intents and creates the Zenn deferred artifact.
2. `zenn-deferred-control.py handoff` validates the exact8 run/topic/artifact boundary and
   fetches the real remote before validating the slug, canonical URL, title, and
   canonical source path, writes
   `status: pending`, and the daily wrapper exits without sleeping or respawning Claude.
3. `ai.anicca.article-zenn-retry` runs `zenn-deferred-worker.sh` every 300 seconds. The worker takes a
   non-blocking advisory lock, scans every run directory, and exits after one pass.
4. A closed window, Zenn 403/not-yet-live response, API/network error, or git push error stays pending
   and exits 0. Permanent canonical-source or artifact invariant failures are moved to `quarantined`,
   logged, and reported without blocking later queue items. A scan never reruns Claude, quality gates,
   or another platform.
5. Retriggering fetches and validates `origin/main`, builds a same-tree commit with `git commit-tree`,
   and pushes that commit directly to remote main. The current branch, local commits, index, and worktree
   are never included. A transient failure before any push attempt leaves the item pending and lets the
   scan inspect later items. Once a push subprocess starts, its success, rejection, timeout, or ambiguous
   client failure consumes the scan's one-push budget; no later item can push in that scan.
6. Once live, SSR reality-gate PASS records the canonical `zenn-article/ja` row once and changes state to
   `live-recorded`. `article-run-complete.py`, heartbeat, and successful Telegram delivery then change it
   to `complete`. A crash or failure at any boundary resumes from the saved receipt without republishing. A
   fully evidenced `complete` artifact is immutable and short-circuits before source, network, heartbeat,
   or notification work; it can never be downgraded to a non-terminal state.
7. Every scan logs queue count and oldest age. A queue of at least two items, or an item at least 24
   hours old, sends a rate-limited Telegram backlog advisory.

Completion and quarantine Telegram sends are at-least-once operations. A crash after Telegram accepts
a message but before the artifact update may produce a duplicate on the next scan; `run_id` in every
message is the operator-visible idempotency key.

## Install and inspect

```bash
bin/lm-loop status article-zenn-retry
bin/lm-loop watch article-zenn-retry
```

Expected idle state is `state = not running`, `last exit code = 0`, and `run interval = 300 seconds`.
Lifecycle changes are performed only through the final main-derived release and `bin/lm-loop apply --all`.
Do not run the removed foreground retry loop and do not manually append a Zenn ledger row.
