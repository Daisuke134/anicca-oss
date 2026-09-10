---
name: anicca-warmup-flip
description: "Daily loop that seeds Postiz warmup dates, promotes accounts to live after seven days, and reports every promotion to Telegram."
metadata:
  tags: warmup, postiz, newsletter, growth
  requires:
    bins: [python3]
    env: [TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID]
---

# Anicca Warmup Auto-Flip

For new TikTok/IG/YT accounts: 7-day soft-launch (post_mode=draft + auto-music)
then auto-flip to direct_post live mode. Per Spec Part L.

## Local and cloud execution

The lifecycle registry owns scheduling in both runtimes. Local macOS runs the
repository entrypoint through `lm-loop`; cloud runs the same entrypoint and sets
`LIFE_MANAGER_STATE_ROOT` to its durable mounted state. Neither runtime uses an
OpenClaw checkout or Slack.

For a manual local wake from a Life Manager release:

`bash skills/anicca-warmup-flip/scripts/run.sh`

## Mutation

postiz-integrations.json: warmup_started_at (= today if missing) +
warmup_phase (= 'live' after 7d).

Post scripts must read warmup_phase to decide DRAFT vs DIRECT_POST.

The first canonical wake copies legacy state when needed. State writes are locked
and atomic. A failed Telegram delivery stays in a private durable outbox and is
retried on the next wake before the lock is released.
