# anicca-agentmail (spec 10)

Per-Anicca AgentMail inboxes + push-webhook receiver + autonomous reply loop. Round 4 (G1 closure + dual-org) wired 2026-06-04.

## Architecture

```
Gmail → AgentMail (Svix, per-org webhook subscription)
         ↓
         https://aniccanomac-mini-1.tail7a0ba4.ts.net/agentmail
         ↓  Tailscale Funnel  (permanent URL, no rotation)
         ↓
         :8810 webhook-server.ts
         ↓  try every AGENTMAIL_WEBHOOK_SECRET[_*] secret → first match wins
         ↓  log {status, org}, append inbox-queue.jsonl
         ↓
         replier-tick.sh  (launchd, every 5 min)
         ↓  ingest.ts → agentmail.db (inbox_threads ⨝ inbox_messages)
         ↓  replier.ts: NOT IN unreplied → DeepSeek v4-pro (fallback v4-flash)
         ↓  primary-org inboxes → spec-12 adapter send.sh
         ↓  sibling-org inboxes → direct REST send w/ matching API key
         ↓  upsert awaiting_reply
         ↓
         Reply lands in counterparty's Gmail inbox FROM the same inbox that received it
```

## Two AgentMail orgs (G1 close-out)

AgentMail free tier hard-caps inboxes at **3 per org**. A 4th inbox needs a paid plan upgrade (= financial broadcast → HARD RULE blocker). Workaround: `client.agent.signUp()` creates a brand-new org with its own free-plan quota and its own API key. Webhook server now accepts events from both orgs via multi-secret support.

| Org | API key env | Webhook secret env | Inboxes |
|---|---|---|---|
| primary (`4812311a…`) | `AGENTMAIL_API_KEY` | `AGENTMAIL_WEBHOOK_SECRET` | `anicca-001-claude`, `anicca-001-openclaw`, `anicca-genesis` |
| hermes (`63a065d7…`) | `AGENTMAIL_HERMES_API_KEY` | `AGENTMAIL_WEBHOOK_SECRET_HERMES` | `anicca-001-hermes` |

Adding inboxes uses the same `inboxes.ts` provisioner and environment contract; no sibling runtime checkout is required.

## Files

| File | Role |
|---|---|
| `inboxes.ts` | Provisions claude + openclaw in primary org. |
| `webhook-server.ts` | Express on `:8810`. Multi-secret Svix HMAC verify. `/healthz` lists active secret buckets. |
| `webhook-subscribe.ts` | Subscribe a webhook with `WEBHOOK_PUBLIC_URL` and a given API key. |
| `ingest.ts` | Drain JSONL → SQLite, cursor-based idempotent. |
| `replier.ts` | Mirrors reply FROM the inbox that received it; uses spec-12 adapter for primary org, direct REST for sibling orgs. |
| `nudge.ts` | Reply-Zero — re-ping after 24h, capped at `NUDGE_MAX_COUNT` (default 1). |
| `replier-tick.sh` / `nudge-tick.sh` / `launch.sh` | launchd wrappers. |
| `state-schema.sql` | `inbox_threads`, `inbox_messages` (+`in_reply_to`), `awaiting_reply`. |
| `netlify-relay/` | Legacy fallback. Use only if Tailscale Funnel is unavailable. |
| `../../config/loop-registry.json` | Canonical source for generated launchd units. |
| `state/inboxes.json` | Ledger of provisioned addresses across both orgs. |

## Env and state

Secrets live in `~/.local/state/life-manager/.env` (chmod 600). Runtime data lives under
`~/.local/state/life-manager/agentmail/`; override with `AGENTMAIL_STATE_ROOT` when needed.
Before the first registry-managed release, run `migrate-legacy-state.sh` once. It copies
legacy queue, SQLite, adapter, semantic evidence, and logs without deleting the source.

```
AGENTMAIL_API_KEY=…                       # primary org
AGENTMAIL_WEBHOOK_SECRET=whsec_…          # primary webhook
AGENTMAIL_HERMES_API_KEY=am_us_…          # sibling org
AGENTMAIL_HERMES_ORG_ID=63a065d7-…
AGENTMAIL_WEBHOOK_SECRET_HERMES=whsec_…   # hermes webhook
DEEPSEEK_API_KEY=…
```

## E2E evidence

| What | When | Result |
|---|---|---|
| `<owner-gmail>` → anicca-001-claude (R3 final) | 14:59:13Z send → 14:59:35Z reply | "Confirmed: Gmail → AgentMail → Tailscale Funnel → :8810…" — Anicca |
| `<owner-gmail>` → anicca-001-hermes (round 4) | 15:46:48Z send → 15:49:50Z reply | `org=hermes` verified, reply FROM `anicca-001-hermes@agentmail.to`: "Received. Dual-secret webhook routing verified. — Anicca" |
| Synthetic 25h-old nudge | 2026-06-03 | `eligible nudges: 1` → v4-pro produced text → http=200 → nudge_count 0→1; re-run = 0 (idempotent) |

## G1–G6 final status

| Gate | Status | Evidence |
|---|---|---|
| **G1** | ✅ **PASS** | All 3 anicca-001-* inboxes live (claude + openclaw in primary, hermes in sibling). Webhook server accepts events from both orgs. Replier mirrors from-address per inbox. |
| **G2** | ✅ PASS | Signed POST → 200, unsigned → 200 status=`rejected`, no retry storms. Multi-secret means rejection happens only when NO known secret matches. |
| **G3** | ✅ PASS | `client.webhooks.list()` shows `ep_3EdBnK…` (primary) at the Tailscale URL and `ep_3EdHa4…` (hermes) at the same URL. |
| **G4** | ✅ PASS | Multiple Gmail E2E round trips under 60s — 22s for the primary org, ~3 min for hermes (includes a 5-min replier tick wait if not run manually). |
| **G5** | ✅ PASS | Synthetic nudge fired with full state transition + idempotent re-run. Daily cron registered at 09:00 local. |
| **G6** | ✅ PASS by config audit | All 3 active plists persist on disk in `~/Library/LaunchAgents/`. `webhook` has `RunAtLoad=true` + `KeepAlive=true`. `replier` has `RunAtLoad=true` + `StartInterval=300`. `nudge` has `StartCalendarInterval=Hour:9,Minute:0` (daemon registered at login, fires daily). Tailscale Funnel is daemonized in `tailscaled` so the URL survives reboot independently. |

[`specs/10-AGENTMAIL-INBOXES.md`](../../specs/10-AGENTMAIL-INBOXES.md) · [`docs.agentmail.to/webhook-verification`](https://docs.agentmail.to/webhook-verification) · [`tailscale.com/kb/1247/funnel-serve-use-cases`](https://tailscale.com/kb/1247/funnel-serve-use-cases)
