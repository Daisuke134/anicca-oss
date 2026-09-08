#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# economy/ubi/run.sh — Channel B (colony spec §5.2/§5.3): computes + LOGS a UBI-contribute plan and a
# gojo (mutual-aid) rescue plan from REAL data, but NEVER executes a transfer itself. The gate logic
# lives in ubi.js (contribute/distributeAI, pure + unit-tested); this script only gathers real inputs,
# calls the gate, and records the decision. Actually MOVING money is a separate, explicit, later step
# (skills/ubi/execute-ubi.py, already built) — this run is the "should we, and how much" computation,
# never the "do it".
#
# Inputs (read-only, no private key touched):
#   - realized profit + liquid balance for THIS instance: reuses skills/self/telemetry-collect.sh's
#     already-verified output (Task #1) for balance, and the live aniccaai.com dashboard-sync
#     (already fixed+verified, Task #1) for monthly realized revenue — no new on-chain code invented.
#   - other colony instances' balances: same telemetry.json files, to find anyone below the §5.2
#     survival floor ($0.50).
# Output: $LIFE_MANAGER_REPO/skills/economy/ubi/state/{contribute,gojo}-log.jsonl (one line per run, ALWAYS
# written, whether the decision was to act or to no-op — fail-closed decisions are logged too).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_STATE_ROOT="${LIFE_MANAGER_SKILLS_STATE_ROOT:-${ANICCA_HOME:-$HOME/.local/state/life-manager}/state/skills}"
STATE="${UBI_STATE_ROOT:-$SKILLS_STATE_ROOT/economy/ubi}"; mkdir -p "$STATE"
CONTRIB_LOG="$STATE/contribute-log.jsonl"
GOJO_LOG="$STATE/gojo-log.jsonl"

# Refresh the 3-instance telemetry snapshots (Task #1, already verified against colony-status.sh).
bash "$HERE/../../self/telemetry-collect.sh" >/dev/null 2>&1 || true

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---- 1. contribute(): this instance's realized profit -> human UBI pool -----------------------
A3CDD4_ADDR=0xB9dd3B67921B354c656523d6851537988F31DD56
DASH_JSON=$(curl -s -m10 "https://aniccaai.com/.netlify/functions/dashboard-sync" 2>/dev/null)
LIQUID_JSON="$HOME/.automaton/state/telemetry.json"
LIQUID=$(python3 -c "import json;print(json.load(open('$LIQUID_JSON')).get('balance_usd', 0))" 2>/dev/null || echo 0)
REALIZED=$(python3 -c "
import json
try:
    d = json.loads('''$DASH_JSON''')
except Exception:
    print(0); raise SystemExit
row = next((r for r in d.get('leaderboard', []) if r.get('id','').lower() == '$A3CDD4_ADDR'.lower()), None)
print(row.get('monthly_revenue_usd', 0) if row else 0)
" 2>/dev/null || echo 0)

CONTRIB_RESULT=$(PYTHONWARNINGS=ignore node --input-type=module -e "
import { contribute } from '$HERE/ubi.js';
const r = contribute($REALIZED, $LIQUID);
console.log(JSON.stringify(r));
" 2>/dev/null)
[ -z "$CONTRIB_RESULT" ] && CONTRIB_RESULT='{"amount_usd":0,"reason":"contribute() call failed -- fail closed"}'
echo "[ubi] contribute: realized=\$$REALIZED liquid=\$$LIQUID -> $CONTRIB_RESULT"
python3 -c "
import json
rec = {'ts': '$(now_iso)', 'realized_profit_usd': $REALIZED, 'liquid_usd': $LIQUID, 'decision': json.loads('''$CONTRIB_RESULT''')}
with open('$CONTRIB_LOG', 'a') as f:
    f.write(json.dumps(rec) + chr(10))
"

# ---- 2. distributeAI() (gojo): is any known colony instance below the $0.50 survival floor -----
COLONY_WALLETS_JSON="$HERE/colony-wallets.json"
GOJO_RESULT=$(python3 -c "
import json

telemetry_files = {
    'anicca-a3cdd4': '$HOME/.automaton/state/telemetry.json',
    'Franklin': '$HOME/.blockrun/state/telemetry.json',
    'claude-p': '$HOME/.anicca-founder/state/telemetry.json',
}
SURVIVAL_FLOOR = 0.50
RESERVE = 5.0

def bal(path):
    try:
        d = json.load(open(path))
    except Exception:
        return None
    b = d.get('balance_usd')
    if b is None:
        nb = d.get('balance_native') or {}
        return nb.get('usdc')
    return b

def wallet(path):
    try:
        return json.load(open(path)).get('wallet', {}).get('address')
    except Exception:
        return None

me_bal = bal(telemetry_files['anicca-a3cdd4']) or 0
surplus_above_reserve = round(me_bal - RESERVE, 6)

broke = None
for name, path in telemetry_files.items():
    if name == 'anicca-a3cdd4':
        continue
    b = bal(path)
    if b is not None and b < SURVIVAL_FLOOR:
        broke = {'name': name, 'wallet': wallet(path), 'balance_usd': b}
        break

print(json.dumps({'surplus_above_reserve': surplus_above_reserve, 'broke': broke}))
" 2>/dev/null)

if [ -z "$GOJO_RESULT" ]; then
  echo "[ubi] gojo: could not evaluate colony state -- fail closed, no-op"
  python3 -c "
import json
rec = {'ts': '$(now_iso)', 'decision': {'amount_usd': 0, 'reason': 'colony state unavailable -- fail closed'}}
with open('$GOJO_LOG', 'a') as f:
    f.write(json.dumps(rec) + chr(10))
"
else
  BROKE=$(python3 -c "import json;d=json.loads('''$GOJO_RESULT''');print(json.dumps(d.get('broke')))" 2>/dev/null)
  SURPLUS=$(python3 -c "import json;print(json.loads('''$GOJO_RESULT''').get('surplus_above_reserve', 0))" 2>/dev/null)
  if [ "$BROKE" = "null" ] || [ -z "$BROKE" ]; then
    echo "[ubi] gojo: no colony member below the \$0.50 survival floor -- no-op"
    python3 -c "
import json
rec = {'ts': '$(now_iso)', 'surplus_above_reserve_usd': $SURPLUS, 'decision': {'amount_usd': 0, 'reason': 'no colony member below survival floor'}}
with open('$GOJO_LOG', 'a') as f:
    f.write(json.dumps(rec) + chr(10))
"
  else
    RECIPIENT_WALLET=$(python3 -c "import json;print(json.loads('''$BROKE''').get('wallet'))" 2>/dev/null)
    RECIPIENT_NAME=$(python3 -c "import json;print(json.loads('''$BROKE''').get('name'))" 2>/dev/null)
    REGISTRY=$([ -f "$COLONY_WALLETS_JSON" ] && cat "$COLONY_WALLETS_JSON" || echo '[]')
    GOJO_DECISION=$(PYTHONWARNINGS=ignore node --input-type=module -e "
import { distributeAI } from '$HERE/ubi.js';
const registry = $REGISTRY;
const r = distributeAI('$RECIPIENT_WALLET', $SURPLUS, registry, []);
console.log(JSON.stringify(r));
" 2>/dev/null)
    [ -z "$GOJO_DECISION" ] && GOJO_DECISION='{"amount_usd":0,"reason":"distributeAI() call failed -- fail closed"}'
    echo "[ubi] gojo: $RECIPIENT_NAME ($RECIPIENT_WALLET) below survival floor, sender surplus-above-reserve=\$$SURPLUS -> $GOJO_DECISION"
    AMT=$(python3 -c "import json;print(json.loads('''$GOJO_DECISION''').get('amount_usd', 0))" 2>/dev/null)
    if python3 -c "import sys; sys.exit(0 if float('$AMT') > 0 else 1)" 2>/dev/null; then
      echo "[ubi] PLAN ONLY, NOT EXECUTED: send \$$AMT to $RECIPIENT_NAME ($RECIPIENT_WALLET). Actually sending is a separate, explicit step (skills/ubi/execute-ubi.py) -- this run never calls it."
    fi
    python3 -c "
import json
rec = {'ts': '$(now_iso)', 'recipient': '$RECIPIENT_NAME', 'recipient_wallet': '$RECIPIENT_WALLET', 'surplus_above_reserve_usd': $SURPLUS, 'decision': json.loads('''$GOJO_DECISION'''), 'executed': False}
with open('$GOJO_LOG', 'a') as f:
    f.write(json.dumps(rec) + chr(10))
"
  fi
fi
exit 0
