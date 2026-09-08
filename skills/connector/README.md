# Life Manager Connector

Connector maintains a rolling 28-day view of a user's real calendar and runs one bounded pass every 30 minutes. It ranks Tokyo events in this order: YC hackathons, open lightning-talk opportunities, AI, crypto, startup. It applies only to verified strong or moderate matches.

Luma and connpass are the first-priority lanes. When neither can fill an open slot, the same Connector pass continues through Peatix, Meetup, Doorkeeper, Eventbrite, TechPlay, and Kokuchpro. Connpass discovery uses only the official v2 API. Until connpass explicitly permits automated participation for the user's own account, Connector performs zero connpass submissions and sends normalized candidate URLs and slot facts to Telegram.

## Public profile

Copy `examples/public-profile.json` to `apps/life-manager/config/connector/<tenant-id>.json`, then replace the public references with references owned by your local installation. Keep secrets and personal identity data outside the repository.

## Install on macOS

Requirements are Node.js, `gog`, a running user-owned CloakBrowser daily-driver at CDP `:9222`, and a mode-0600 environment file containing only the allowlisted variables accepted by `lib/load-connector-env.js`.

From an existing checkout, one command builds an immutable release, installs only the
Connector owner, starts one bounded pass, and prints its status:

```sh
./install.sh connector
```

Re-running the same command upgrades and restarts Connector without replacing private
configuration, state, browser sessions, Calendar receipts, or Telegram receipts.

Render first; the renderer never changes launchd:

```sh
mkdir -p "$HOME/.local/state/life-manager/rendered-launchd" "$HOME/Library/LaunchAgents"
bash skills/connector/render-launchd.sh \
  --output-dir "$HOME/.local/state/life-manager/rendered-launchd" \
  --repo-root "$(pwd -P)" \
  --life-manager-home "$HOME/.local/state/life-manager" \
  --connector-env-file "$HOME/.local/state/life-manager/.env"
cp "$HOME/.local/state/life-manager/rendered-launchd/ai.anicca.life-manager-connector-native.plist" \
  "$HOME/Library/LaunchAgents/ai.anicca.life-manager-connector-native.plist"
bin/launchctl-safe bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.anicca.life-manager-connector-native.plist"
```

The installed plist owns exactly one label and uses `StartInterval=1800`. Use only `bin/launchctl-safe` for live launchd operations.

## Uninstall

```sh
bin/launchctl-safe bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.anicca.life-manager-connector-native.plist"
rm "$HOME/Library/LaunchAgents/ai.anicca.life-manager-connector-native.plist"
```

Uninstall removes the exact launchd plist only. Runtime state and receipts remain local for audit and are never part of the open-source package.
