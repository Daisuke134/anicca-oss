# CFO hourly operator skill

This skill runs one repository-owned CFO pass and exits. It is the operator-facing wrapper for
`apps/life-manager/scripts/cfo-hourly-local.js`; launchd owns the one-hour cadence.

## Contract

- Invoke `skills/cfo/run.sh` from the canonical Life Manager checkout.
- Code is resolved from the same immutable repository release as this wrapper. No second CFO app copy
  or `LIFE_MANAGER_APP_DIR` override is used.
- Credentials are read from `LIFE_MANAGER_ENV_FILE` (default:
  `~/.local/state/life-manager/.env`) and are never printed or written to loop state.
- `LM_CFO_UID`, `TELEGRAM_ALERT_CHAT_ID`, and `TELEGRAM_BOT_TOKEN` are the shared-loop contract.
  `LM_CFO_TELEGRAM_CHAT_ID` remains accepted as a standalone chat-ID fallback; token ownership has one
  canonical name, `TELEGRAM_BOT_TOKEN`.
- State is outside the code release at `CFO_STATE_DIR` (default:
  `~/.local/state/life-manager/life-manager-cfo-hourly`). The wrapper and Node process use this exact
  same directory. Agent Economy revenue defaults to its repository-managed local state under
  `~/.local/state/life-manager/agent-economy`; `LM_AGENT_ECONOMY_STATE_ROOT` or `REVENUE_RECEIPT_JOURNAL` may select another
  self-hosted instance. Marketplace receipt journals are optional and explicitly configured with
  `LM_CFO_MARKETPLACE_RECEIPTS`. The wrapper records only the runner's redacted status envelope in
  `last-result.json`.
- A failure produces a fixed redacted status envelope and a non-zero exit. It never invents a
  financial amount, retries out of band, or logs raw provider/error payloads.

The pass is single-writer: do not run another CFO, `cfo-daily`, or financial-report loop against the
same snapshot/delivery tables.
