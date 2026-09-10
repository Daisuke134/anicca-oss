#!/usr/bin/env bash
# Report loops whose real output ledgers stay still. Never restarts watched loops.
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)
STATE_ROOT="${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/f7-silence-check}"
MANIFEST="${F7_MANIFEST:-$REPO_ROOT/config/f7-loop-manifest.json}"
LEDGER="${F7_LEDGER:-$STATE_ROOT/self-heal-ledger.jsonl}"
COOLDOWN_ROOT="${F7_COOLDOWN_ROOT:-$STATE_ROOT/escalation-cooldown}"
COOLDOWN_SECONDS="${F7_COOLDOWN_SECONDS:-21600}"
TELEGRAM_SENDER="${F7_TELEGRAM_SENDER:-$REPO_ROOT/skills/_shared/send-telegram.sh}"

mkdir -p "$(dirname "$LEDGER")" "$COOLDOWN_ROOT"
[ -f "$MANIFEST" ] || { echo '{"status":"skipped","reason":"no_manifest"}'; exit 0; }

FINDINGS=$(python3 - "$MANIFEST" <<'PY'
import json, os, sys, time
try:
    manifest = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as error:
    print(f"F7_manifest\tmanifest_unreadable\t0\t0\t{type(error).__name__}")
    raise SystemExit
now = time.time()
for loop in manifest.get("loops", []):
    label = str(loop.get("label") or "").strip()
    ledgers = loop.get("ledgers") or []
    limit = float(loop.get("max_silence_hours") or 0)
    if not label or not ledgers or limit <= 0:
        continue
    ages = [(now - os.path.getmtime(path)) / 3600 for raw in ledgers
            if os.path.exists(path := os.path.expanduser(str(raw)))]
    if not ages:
        print(f"{label}\tno_ledger\t0\t{limit:.1f}\t-")
    elif min(ages) > limit:
        print(f"{label}\tsilent\t{min(ages):.1f}\t{limit:.1f}\t-")
PY
)

count=0
while IFS=$'\t' read -r label kind hours limit detail; do
  [ -n "$label" ] || continue
  count=$((count + 1))
  key=$(printf '%s' "F7_silent_barren_${label}" | tr -cs 'A-Za-z0-9_.-' '_')
  cooldown="$COOLDOWN_ROOT/$key.last"
  now=$(date +%s)
  last=$(cat "$cooldown" 2>/dev/null || echo 0)
  notify=1
  if [ $((now - last)) -lt "$COOLDOWN_SECONDS" ]; then notify=0; fi
  if [ "$kind" = "manifest_unreadable" ]; then
    message="🚨 F7 manifest unreadable ($detail); no loop can be watched"
  elif [ "$kind" = "no_ledger" ]; then
    message="🚨 $label has no readable output ledger; productivity cannot be judged"
  else
    message="🚨 $label produced nothing for ${hours}h (allowed ${limit}h)"
  fi
  receipt='{"status":"cooldown_suppressed"}'
  if [ "$notify" -eq 1 ]; then
    if receipt=$("$TELEGRAM_SENDER" "$message" 2>/dev/null); then
      printf '%s\n' "$now" > "$cooldown"
    else
      receipt='{"status":"failed"}'
    fi
  fi
  python3 - "$LEDGER" "$label" "$kind" "$hours" "$limit" "$message" "$receipt" <<'PY'
import json, re, sys, time
path, label, kind, hours, limit, message, raw_receipt = sys.argv[1:]
try:
    receipt = json.loads(raw_receipt)
except Exception:
    match = re.fullmatch(r"TELEGRAM_SENT=true MSGID=([^\s]+)", raw_receipt.strip())
    receipt = ({"status": "delivered", "message_ids": [match.group(1)]}
               if match else {"status": "invalid_receipt"})
row = {"ts": time.time(), "failure_class": "F7_silent_barren", "label": label,
       "kind": kind, "silent_hours": float(hours), "limit_hours": float(limit),
       "message": message, "telegram": receipt}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
PY
done <<< "$FINDINGS"

printf '{"status":"ok","findings":%d}\n' "$count"
