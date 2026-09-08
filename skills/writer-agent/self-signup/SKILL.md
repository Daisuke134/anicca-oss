---
name: article-self-signup-bootstrap
description: >
  Bootstrap fresh publishing accounts (note.com / Dev.to / Substack / Zenn / X) for the
  article earn loop using the loop's own email, following the proven no-human ig-account-create
  pattern (Gmail plus-address + gog-gmail OTP/magic-link read + isolated CDP context on the
  shared daily-driver browser). Implements spec §7.55 zero-config bootstrap principle.
triggers:
  - self-signup bootstrap
  - create article publishing account
  - bootstrap note/devto/substack account
metadata:
  status: INFRASTRUCTURE + RESEARCHED RUNBOOKS — live E2E NOT YET completed for any platform
  spec: docs/superpowers/specs/2026-07-14-article-earn-loop-ssot.md §7.55 + §7.6 #64
  canonical_pattern: ~/.openclaw/skills/ig-account-create (reused directly, not duplicated)
---

# article-self-signup-bootstrap

## What this is (and honestly, is not yet)

Per spec §7.55 (zero-config bootstrap principle): a human hands over only a payout rail; the
loop creates its own publishing accounts. `ig-account-create` proved the pattern works
end-to-end for Instagram (2026-06-29, @aiclipsvault, email-only, 0 phone, 0 captcha, 0 human).
This skill's job is to reuse that SAME proven infrastructure — the CDP driver scripts are
already 100% generic (verified by reading them: `cdp.py` and `cdp_incognito.py` have zero
Instagram-specific code) — for the 5 article-loop platforms: note.com, Dev.to, Substack,
Zenn, X.

**Honest status (2026-07-17)**: this pass built the shared helper + researched each
platform's real signup mechanic (crwl for static research + one live exploratory pass on
Substack), but did **not** complete a live end-to-end signup for any platform. Each
platform's actual DOM flow needs the same kind of iterative, screenshot-driven work
ig-account-create itself needed (its own SKILL.md documents multiple hard-won "gotchas" —
autocomplete corrupting the email field, hidden duplicate DOM inputs, custom comboboxes
needing trusted clicks, etc.). That work is real and platform-specific; it should not be
rushed or faked. Below is what's real and what's still open, split so a future pass (or a
different agent) can pick up exactly where this left off.

## Shared infrastructure (built, tested)

- `scripts/gen-plus-address.sh <platform-tag>` — prints a fresh unique Gmail plus-address
  (`keiodaisuke+<tag>-<rand>@gmail.com`). Tested: produces a fresh unique address per call.
- CDP driver: reuse `~/.openclaw/skills/ig-account-create/scripts/cdp.py` and
  `cdp_incognito.py` directly (do NOT copy/fork them — verified generic, zero IG coupling).
  `cdp_incognito.py new <url>` opens an isolated browser context (own cookie jar) on the
  shared daily-driver (CDP :9222) so a fresh signup renders even though the daily-driver is
  already logged into the live `aniccabuddha.substack.com`/`anicca123`/etc accounts. Always
  `cdp_incognito.py close <ctx_id>` when done — never leave orphaned contexts on Dais's
  shared browser.
- OTP / magic-link read: `gog gmail search --account "$WRITER_GMAIL_ACCOUNT" "<query>
  in:anywhere newer_than:1h" --max 3 --plain` (needs `GOG_KEYRING_PASSWORD` in
  `LIFE_MANAGER_ENV_FILE`). The local default is
  `~/.local/state/life-manager/.env`; cloud injects the same variable contract.

## Per-platform research (real, from crwl + one live pass — 2026-07-17)

| Platform | Signup mechanic (researched) | Tractability | Status |
|---|---|---|---|
| **Substack** | `substack.com/explore?action=signup` does NOT show a bare email form first — it opens an onboarding funnel starting with a "トピックを3つ選択" (pick 3 topics) screen, THEN presumably an email step (not yet reached). Existing LOGIN already uses email magic-link + `gog gmail` in this codebase (proven, see Substack Stripe/session precedent in spec §7.3 U3), so the underlying OTP mechanism is proven — the multi-step onboarding funnel is the new unknown. | High (email-only, proven OTP mechanism, just needs the funnel mapped) | Explored live 1 step (topic-picker screen, screenshotted), context closed cleanly, no account created |
| **Dev.to** | `dev.to/enter?state=new-user` offers OAuth (Apple/Facebook/GitHub/Google/MyMLH/X) OR native email+password. Native path avoids needing a pre-existing OAuth identity. | High (native email path, Forem-based, no known aggressive anti-bot history) | Researched only (crwl), no live pass yet |
| **note.com** | `note.com/signup` offers "メールで登録" (email) alongside Google/X/Apple OAuth. Native email path exists. This codebase already has deep note.com automation experience (publish-note.sh's login flow, note-mcp) to draw DOM patterns from. | Medium-high (email path exists, but note.com is the platform this project has seen the most anti-automation friction on historically — session/cookie handling is already non-trivial for LOGIN) | Researched only, no live pass yet |
| **Zenn** | No native email signup found — Zenn's own site is GitHub-branded throughout (`github.com/zenn-dev` links, GitHub OAuth is the standard onboarding path for Japanese dev-blog platforms of this type). Direct `/login` URL 404'd in this research pass (likely a client-side-routed SPA path, needs a real browser hit, not a static crawl, to confirm). | Low — requires the AI to already have its OWN GitHub account, which is itself a meta-bootstrap dependency, not a platform-native email signup | **Blocked on a prerequisite**: AI's own GitHub account. Out of this pass's scope. |
| **X (Twitter)** | Not researched live this pass. X is known industry-wide (and within this project's own `anicca-tt-account-create`/`tiktok-account-factory` precedent for a comparable anti-bot tier) to require phone verification much more aggressively than email-only platforms, plus heavier CAPTCHA. `anicca-tt-account-create`'s own status is "SCAFFOLDED — disabled until Dais D-01 4-signup" (needs SMSPOOL_API_KEY, SADCAPTCHA_API_KEY, dedicated proxy) for a comparably-defended platform. | Low — same anti-bot tier as TikTok, needs dedicated SMS/captcha infra this project has already gated behind explicit Dais-provided API keys | **Deferred**, matches spec §7.55's own KYC/SMS-tier-platform treatment (default OFF, opt-in once infra exists) |

## Recommended next steps (not done in this pass — scope note)

Each of Substack / Dev.to / note.com needs its OWN dedicated live-exploration pass (screenshot
each step, handle the platform's specific DOM quirks, verify the account is actually LIVE by
navigating to the public profile logged-out) — the same amount of iterative work
`ig-account-create` itself needed. Doing all three properly in one sitting risks either
rushing (producing a broken/unverified "success") or burning a very large tool-call budget on
live browser automation without a clear stopping point. Recommend splitting as three follow-up
tasks (#64a substack / #64b devto / #64c note), each scoped like #58a/#69 were in this session:
one platform, live-verified end to end, own commit.

Zenn (needs AI's own GitHub account first) and X (needs SMS/captcha infra, matches the
TikTok precedent) are out of scope for a bare email-only bootstrap and should stay deferred
under the same "KYC 不要 loop のみ既定 ON" principle spec §7.55 already establishes for gig
work.

## State / creds (when a live signup succeeds)

Follow `ig-account-create`'s own convention: save to
`~/.cloak/article-self-signup-<platform>.json` — `{platform, email, password, username,
status: LIVE, created_at}`. Never write these into `.env` defaults (§7.55: "OSS版では .env の
デフォルトではなく bootstrap ステップの成果物になる") — they are per-installer bootstrap
output, referenced by the `NOTE_USER_ID` / `NOTE_URLNAME` / `ZENN_ACCOUNT` /
`DEVTO_ACCOUNT_HANDLE` / `SUBSTACK_PUBLICATION` env vars documented in
`~/profitable-claude/.env.example` (spec #58a).
