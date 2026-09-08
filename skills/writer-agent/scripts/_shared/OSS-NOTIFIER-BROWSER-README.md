# article loop shared scripts (spec #70, OSS self-containment)

Generic, env-driven replacements for the two Anicca-instance-specific dependencies the
article loop used to have (openclaw CLI, CloakBrowser). Both are opt-in: the loop must
keep running with neither configured.

## notifier.sh

Sends a Telegram alert via the Telegram Bot API directly (`curl` + `TELEGRAM_BOT_TOKEN`
+ `TELEGRAM_CHAT_ID`). No env vars set = silent no-op, not an error.

```bash
bash notifier.sh "message text"              # standalone
source notifier.sh; notify "message text"    # sourced, as a function
```

Get a bot token from [@BotFather](https://t.me/BotFather) (30 seconds, no approval),
and your chat id from [@userinfobot](https://t.me/userinfobot).

## ensure-browser.sh

Confirms a Chromium-family browser is listening on a CDP (Chrome DevTools Protocol)
port, launching one if not. This is the minimum any browser-driving loop needs; it does
NOT do session-cookie recovery, stealth fingerprinting, or idle-tab garbage collection
-- those are legitimate needs for a heavily-automated daily loop, but they depend on
whatever private browser automation stack you already run (the live Anicca instance
uses CloakBrowser for this, which is not part of this OSS repo). Write your own layer
on top of this script if you need it.

```bash
CDP_PORT=9222 BROWSER_BIN=/path/to/chrome BROWSER_PROFILE_DIR=~/.my-browser-profile \
  bash ensure-browser.sh
# prints ALIVE (already up) / RECOVERED (relaunched) / FAILED
```

`BROWSER_BIN` unset still lets the script detect ALIVE; it just cannot launch a
recovery browser without it.

`BROWSER_PROFILE_DIR` must be a **persistent** profile, not a fresh one every run: the
publish scripts in this repo assume an already-logged-in browser session (note / zenn /
substack / X / dev.to cookies). Provision the persistent profile through the
repository-owned browser runtime, then log in once before enabling the loop.
