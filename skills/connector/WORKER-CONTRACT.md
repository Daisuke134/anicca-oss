# Connector bounded worker contract

Life Manager local owns the rolling 28-day goal, local state, receipts, report, and 30-minute schedule. You are one bounded worker pass, not the owner of those concerns.

Start by reading the current all-calendar busy inventory through installed `gog`; no static availability input is valid. Use the shared daily-driver selected by `CLOAK_CDP_BASE_URL` (default `http://127.0.0.1:9222`) only through the existing Connector browser rail. Never fall back to a different endpoint or profile. The rail opens and closes only its own page. Do not navigate, close, or clean any page that predates your Connector-owned page.

Use the existing local modules rather than recreating their behavior:

- `connector-events-pack.js` for Luma discovery, authenticated page handling, inventory, natural-language ranking, and grounded goal/serendipity evaluation.
- `luma-rsvp-adapter.js`, `luma-confirmation-mail.js`, and `luma-ticket-qr.js` for registration, confirmation, and ticket evidence.
- `transport/calendar-gog.js`, `google-calendar-busy-inventory.js`, and `connector-calendar-sync.js` for all-calendar reads and idempotent Calendar synchronization.
- `outbound-evidence.js`, `outbound-success.js`, and `connector-coverage-telegram.js` for receipt validation and human-facing reporting.

Rank verified candidates by the Connector profile: YC hackathons, open lightning-talk opportunities, AI, crypto, and startup events first. Apply only to strong or moderate matches. Try Luma and connpass first, then use Peatix, Meetup, Doorkeeper, Eventbrite, TechPlay, and Kokuchpro to fill remaining open slots. Connpass discovery uses its official v2 API. Connpass UI application is allowed only when the local operator has explicitly set `LM_CONNECTOR_CONNPASS_AUTOMATED_SUBMIT_ALLOWED=true` in the shared connector env; otherwise use the Telegram action boundary. Never use the discovery API as a write path, crawl connpass pages, or bypass the existing provider/readback contract.

The only completion state is verified rolling coverage with `open=0`. A candidate, source, date, authentication, or tool failure does not become success: preserve a continuation for the next bounded pass and move to the next safe action according to the existing Connector state machines. Do not send a Telegram message or claim a registration, Calendar entry, QR, or completion without the existing verified receipt and readback contracts.

Never print environment values, ownership tokens, cookie material, passwords, private keys, or raw provider errors. Never start or terminate the browser process and never invoke launchd from this worker.
