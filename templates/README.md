# templates/

Launchd plist templates that `install.sh` copies into
`~/Library/LaunchAgents/` after substituting the user's `$HOME` and
`$ANICCA_HOME` paths.

These daemons keep the personal-life-leader stack alive:

- `ai.anicca.tg-loc-bot.plist`  → the Telegram Live-Location bot that
  writes `~/.openclaw/state/location/<user_id>.json`. Every cron tick
  of `anicca-life-manager` reads from those files.

It has `KeepAlive=true` so launchd restarts it automatically on
crash, with a 10 s throttle to prevent thrash.

The `__HOME__` and `__ANICCA_HOME__` tokens are replaced by `install.sh`
at install time.

To install manually:

```bash
sed -e "s|__HOME__|$HOME|g" -e "s|__ANICCA_HOME__|$HOME/.openclaw|g" \
    templates/ai.anicca.tg-loc-bot.plist > \
    ~/Library/LaunchAgents/ai.anicca.tg-loc-bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.anicca.tg-loc-bot.plist
```
