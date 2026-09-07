#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"
STATE_ROOT="${AGENT_ECONOMY_STATE_ROOT:-${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager}/agent-economy}"
LEDGER="${EARN_LEDGER:-$STATE_ROOT/earn-ledger.jsonl}"
CORRECTIONS="${RECEIPT_CORRECTIONS:-$STATE_ROOT/receipt-reconciliations.jsonl}"
CANDIDATE_INBOX="${REVENUE_RECEIPT_INBOX:-$STATE_ROOT/revenue-receipts.inbox.jsonl}"
REVENUE_JOURNAL="${REVENUE_RECEIPT_JOURNAL:-$STATE_ROOT/revenue-receipts.jsonl}"
COMPUTE_COST_LOG="${COMPUTE_COST_LOG:-$STATE_ROOT/compute-receipts.jsonl}"
SHELTER_COST_LEDGER="${SHELTER_COST_LEDGER:-$STATE_ROOT/shelter-cost.jsonl}"

mkdir -p "$STATE_ROOT"

/usr/bin/env node "$HERE/reconcile-receipts.mjs" "$LEDGER" "$CORRECTIONS" "$CANDIDATE_INBOX" "$REVENUE_JOURNAL" >/dev/null
exec /usr/bin/env node "$HERE/status.mjs" "$LEDGER" "$CORRECTIONS" "$COMPUTE_COST_LOG" "$SHELTER_COST_LEDGER" "$REVENUE_JOURNAL"
