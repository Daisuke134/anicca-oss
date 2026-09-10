---
name: alpaca-investment
description: Run one bounded Life Manager Alpaca paper, real-account shadow, or live investment pass and exit.
---

# Alpaca Investment

Run one mode-isolated investment pass. Observe official broker state before deciding, allow the model to choose
`TRADE` or `NO_TRADE`, enforce risk and duplicate fences in deterministic code, reconcile every uncertain effect
by stable client order ID, persist receipts outside the release, report the official result to Telegram, and
exit. `shadow` observes a real account but cannot submit; `live` requires explicit mode-specific credentials and
may submit only through the fixed risk boundary. Never describe paper P&L or deposits as revenue, promise returns,
silently switch modes, or run multiple live writers for one account. Third-party setup and recovery are documented
in `self-host/README.md`.
