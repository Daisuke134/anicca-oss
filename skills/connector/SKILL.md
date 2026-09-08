---
name: connector
description: Fill a rolling 28-day calendar with verified Tokyo events, prioritizing Luma, safe connpass discovery, and lightning talks.
---

# Connector

Use this skill when a user wants Life Manager to discover, rank, register, calendar, and report relevant events.

Read [README.md](README.md) before installation and [WORKER-CONTRACT.md](WORKER-CONTRACT.md) before changing runtime behavior. Keep one 30-minute native owner. Prefer YC hackathons, open lightning talks, AI, crypto, then startup events. Connpass discovery remains official-API read-only; UI application is actionable only when the local operator explicitly sets `LM_CONNECTOR_CONNPASS_AUTOMATED_SUBMIT_ALLOWED=true` in the shared connector env. Without that opt-in, retain the Telegram action handoff.

Never package credentials, identity files, browser state, runtime state, or provider receipts. Never claim registration without provider readback, Calendar readback, Telegram provider IDs, and a durable evidence bundle.
