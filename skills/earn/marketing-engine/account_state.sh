#!/usr/bin/env bash

resolve_ig_account_field() {
  local state_file="$1"
  local field="$2"
  local python_bin="${IG_ACCOUNT_STATE_PYTHON:-$(command -v python3 2>/dev/null)}"
  [ -n "$python_bin" ] || { printf '%s\n' 'python3 is required' >&2; return 2; }
  "$python_bin" - "$state_file" "$field" <<'PY' 2>/dev/null
import json
import sys

path, field = sys.argv[1:3]
try:
    with open(path) as f:
        accounts = json.load(f)
except Exception as exc:
    print(f"account state unreadable: {exc}", file=sys.stderr)
    raise SystemExit(2)

if not isinstance(accounts, list):
    print("account state must be a JSON list", file=sys.stderr)
    raise SystemExit(2)

usable = []
for account in accounts:
    status = str(account.get("status") or "").lower()
    if not (status.startswith("ready") or status.startswith("warming") or status.endswith("_ready")):
        continue
    if any(word in status for word in ("poison", "frozen", "blocked")):
        continue
    if any(account.get(key) for key in ("poisoned", "poisoned_at", "frozen_at", "blocked_at")):
        continue
    if not account.get("handle"):
        continue
    usable.append(account)

if usable:
    value = usable[-1].get(field)
    if value is not None:
        print(value)
PY
}

count_ig_usable_accounts() {
  local state_file="$1"
  local python_bin="${IG_ACCOUNT_STATE_PYTHON:-$(command -v python3 2>/dev/null)}"
  [ -n "$python_bin" ] || { printf '%s\n' 'python3 is required' >&2; return 2; }
  "$python_bin" - "$state_file" <<'PY' 2>/dev/null
import json
import sys

try:
    with open(sys.argv[1]) as f:
        accounts = json.load(f)
except Exception:
    accounts = []

count = 0
for account in accounts if isinstance(accounts, list) else []:
    status = str(account.get("status") or "").lower()
    if not (status.startswith("ready") or status.startswith("warming") or status.endswith("_ready")):
        continue
    if any(word in status for word in ("poison", "frozen", "blocked")):
        continue
    if any(account.get(key) for key in ("poisoned", "poisoned_at", "frozen_at", "blocked_at")):
        continue
    count += 1
print(count)
PY
}

resolve_ig_handle() {
  resolve_ig_account_field "$1" handle
}

resolve_ig_port() {
  resolve_ig_account_field "$1" port
}

resolve_ig_session_owner() {
  resolve_ig_account_field "$1" session_owner
}

resolve_ig_started_warming() {
  resolve_ig_account_field "$1" started_warming
}

ig_warming_day() {
  local started_warming="${1:-}"
  local today="${2:-}"
  local python_bin="${IG_ACCOUNT_STATE_PYTHON:-$(command -v python3 2>/dev/null)}"
  [ -n "$python_bin" ] || { printf '%s\n' 'python3 is required' >&2; return 2; }
  "$python_bin" - "$started_warming" "$today" <<'PY' 2>/dev/null
import datetime
import sys

started_raw, today_raw = sys.argv[1:3]
try:
    started = datetime.date.fromisoformat(started_raw)
    today = datetime.date.fromisoformat(today_raw) if today_raw else datetime.date.today()
    elapsed = (today - started).days
    print(elapsed + 1 if elapsed >= 0 else 0)
except (TypeError, ValueError):
    print(0)
PY
}

ig_provision_reason() {
  local handle="$1"
  local cooked_marker="$2"
  if [ -f "$cooked_marker" ]; then
    printf '%s\n' "cooked-marker"
  elif [ -z "$handle" ]; then
    printf '%s\n' "no-active-account"
  fi
}
