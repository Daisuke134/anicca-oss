#!/usr/bin/env bash
# gig_brake.sh — the operator brake for the gig loop.
#
# WHY THIS EXISTS (2026-08-07, two failures in twenty minutes):
#
#   23:39  An operator wrote a stop reason into configured runtime state/state/disk-writers.stop,
#          the flag launch_gig_worker.sh already honours. It was gone in ~60s.
#          disk-sentinel.sh:277-279 clears that flag whenever free space recovers.
#          That flag does not mean "stop"; it means "the disk is full", and it has
#          an owner (disk-sentinel) that clears it on ITS OWN schedule. Borrowing
#          another controller's flag means borrowing its clear condition.
#
#   23:50  The operator booted the pass out of launchd. launchctl showed it gone.
#          Five minutes later it was running: gig-healthcheck.sh -> plist-selfheal.sh
#          reconciles any hf-gig label that is installed-but-not-loaded back into
#          launchd every 300s. That guard is correct and must keep working.
#
# THE RULE THIS FILE ENCODES:
#   A brake holds only if nothing else manages it. So this flag has
#     (a) its own path, under a name no other job greps for,
#     (b) a recorded owner -- the human or agent who raised it -- and
#     (c) exactly one clear condition: that owner releases it, or the expiry that
#         same owner set at raise time elapses.
#   Nothing clears it for a reason unrelated to why it was raised. Free disk,
#   recovered heartbeats, plist reconciliation: none of them touch it.
#
# AND THE COUNTERWEIGHT:
#   A brake that can be forgotten is how earning dies quietly. disk-sentinel.sh:271-276
#   records the 2026-07-30 outage precisely: a stop flag latched at a clear point the
#   machine could not reach, with nothing but one log line per hour. Ten hours.
#   Two properties were BOTH required for that: unbounded hold, and silence.
#   So this brake removes both --
#     * bounded: expiry is mandatory, defaulted to 60min and capped at 480min, i.e.
#       strictly shorter than the incident it is preventing. Renewal is one command.
#     * loud: every refusal notifies, and gig-healthcheck.sh re-announces a held brake
#       every GIG_BRAKE_ALARM_MINUTES (default 30) for as long as it is held.
#   Expiry is not "something else managing it": it is a term the owner wrote down
#   themselves. That is the whole difference from the disk flag.
#
# FAIL-CLOSED ON DAMAGE: a brake file that is unreadable or malformed still blocks.
# Someone meant to stop the loop. The expiry safeguard is synthesised from the file
# mtime so even a corrupt brake cannot become an unbounded outage.
#
# Sourceable as a library, or run directly as a CLI:
#   gig_brake.sh raise --owner NAME --reason TEXT [--ttl-minutes N]
#   gig_brake.sh renew [--ttl-minutes N]
#   gig_brake.sh release --owner NAME
#   gig_brake.sh status          # exit 0 = held (loop must not run), 1 = not held
#   gig_brake.sh alarm           # re-announce a held brake / reap an expired one
#
# Test overrides: GIG_OPERATOR_BRAKE_FILE, GIG_BRAKE_LOG, GIG_BRAKE_NOTIFY_CMD,
#                 GIG_BRAKE_ALARM_MINUTES, GIG_BRAKE_TELEGRAM,
#                 GIG_BRAKE_TELEGRAM_SENDER.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/gig_paths.sh"
GIG_BRAKE_FILE="${GIG_OPERATOR_BRAKE_FILE:-$GIG_HOST_STATE_DIR/gig-work/operator.brake}"
GIG_BRAKE_LOG_FILE="${GIG_BRAKE_LOG:-$GIG_LOG_DIR/gig-operator-brake.log}"
GIG_BRAKE_DEFAULT_TTL_MINUTES="${GIG_BRAKE_DEFAULT_TTL_MINUTES:-60}"
GIG_BRAKE_MAX_TTL_MINUTES="${GIG_BRAKE_MAX_TTL_MINUTES:-480}"
GIG_BRAKE_ALARM_EVERY="${GIG_BRAKE_ALARM_MINUTES:-30}"
GIG_BRAKE_TELEGRAM_TARGET="${GIG_BRAKE_TELEGRAM:-${GIG_REPORT_CHAT:-}}"

# Populated by gig_brake_read.
GIG_BRAKE_STATE=""       # held | expired | absent
GIG_BRAKE_OWNER=""
GIG_BRAKE_REASON=""
GIG_BRAKE_RAISED_AT=""
GIG_BRAKE_EXPIRES_AT=""
GIG_BRAKE_MALFORMED=0

gig_brake_log() {
  mkdir -p "$(dirname "$GIG_BRAKE_LOG_FILE")" 2>/dev/null || true
  printf '%s gig-brake: %s\n' "$(date '+%F %T')" "$*" >> "$GIG_BRAKE_LOG_FILE" 2>/dev/null || true
}

# Never fails the caller. A notify outage must not become a gate outage.
gig_brake_notify() {
  local message="$1"
  if [ -n "${GIG_BRAKE_NOTIFY_CMD:-}" ]; then
    "$GIG_BRAKE_NOTIFY_CMD" "$message" >/dev/null 2>&1 || gig_brake_log "notify-failed: $message"
    return 0
  fi
  local sender="${GIG_BRAKE_TELEGRAM_SENDER:-$SCRIPT_DIR/../../../_shared/send-telegram.sh}"
  if [ -x "$sender" ]; then
    if [ -n "$GIG_BRAKE_TELEGRAM_TARGET" ]; then
      "$sender" "$message" "$GIG_BRAKE_TELEGRAM_TARGET" >/dev/null 2>&1 \
        || gig_brake_log "notify-failed: $message"
    else
      "$sender" "$message" >/dev/null 2>&1 || gig_brake_log "notify-failed: $message"
    fi
  else
    gig_brake_log "notify-unavailable: $message"
  fi
}

gig_brake_field() {
  # Reads one key=value line. Value keeps everything after the first '='.
  sed -n "s/^$1=//p" "$GIG_BRAKE_FILE" 2>/dev/null | head -1
}

# Loads the brake into GIG_BRAKE_* and echoes nothing.
gig_brake_read() {
  GIG_BRAKE_STATE="absent"
  GIG_BRAKE_OWNER=""; GIG_BRAKE_REASON=""; GIG_BRAKE_RAISED_AT=""; GIG_BRAKE_EXPIRES_AT=""
  GIG_BRAKE_MALFORMED=0
  [ -e "$GIG_BRAKE_FILE" ] || return 0

  GIG_BRAKE_OWNER="$(gig_brake_field owner)"
  GIG_BRAKE_REASON="$(gig_brake_field reason)"
  GIG_BRAKE_RAISED_AT="$(gig_brake_field raised_at)"
  GIG_BRAKE_EXPIRES_AT="$(gig_brake_field expires_at)"

  # Damage does not release the brake. It only loses us the owner's own terms,
  # which we replace with the shortest defensible ones so it still cannot latch.
  local mtime
  case "$GIG_BRAKE_EXPIRES_AT" in
    ''|*[!0-9]*)
      GIG_BRAKE_MALFORMED=1
      mtime="$(stat -f %m "$GIG_BRAKE_FILE" 2>/dev/null || stat -c %Y "$GIG_BRAKE_FILE" 2>/dev/null || echo 0)"
      GIG_BRAKE_EXPIRES_AT=$(( mtime + GIG_BRAKE_DEFAULT_TTL_MINUTES * 60 ))
      ;;
  esac
  [ -n "$GIG_BRAKE_OWNER" ] || { GIG_BRAKE_MALFORMED=1; GIG_BRAKE_OWNER="UNRECORDED"; }
  [ -n "$GIG_BRAKE_REASON" ] || { GIG_BRAKE_MALFORMED=1; GIG_BRAKE_REASON="UNRECORDED"; }

  if [ "$(date +%s)" -ge "$GIG_BRAKE_EXPIRES_AT" ]; then
    GIG_BRAKE_STATE="expired"
  else
    GIG_BRAKE_STATE="held"
  fi
  return 0
}

# THE predicate every gate calls. 0 = held, the loop must not run.
gig_brake_is_held() {
  gig_brake_read
  [ "$GIG_BRAKE_STATE" = "held" ]
}

gig_brake_describe() {
  local left=$(( (GIG_BRAKE_EXPIRES_AT - $(date +%s)) / 60 ))
  printf 'owner=%s reason=%s expires_in_min=%s%s' \
    "$GIG_BRAKE_OWNER" "$GIG_BRAKE_REASON" "$left" \
    "$([ "$GIG_BRAKE_MALFORMED" = 1 ] && printf ' MALFORMED-BRAKE-FILE' || true)"
}

# Called by a gate that just refused to run. Logs always, notifies rate-limited so
# an hourly pass plus a 5-minute healthcheck cannot turn one stop into a flood.
gig_brake_refuse() {
  local consumer="$1"
  gig_brake_log "REFUSED $consumer ($(gig_brake_describe))"
  gig_brake_alarm_if_due "$consumer"
}

gig_brake_alarm_if_due() {
  local consumer="${1:-gig-loop}"
  local stamp="$GIG_BRAKE_FILE.lastalarm"
  local now last
  now="$(date +%s)"
  last="$(cat "$stamp" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ $(( now - last )) -lt $(( GIG_BRAKE_ALARM_EVERY * 60 )) ]; then
    return 0
  fi
  printf '%s\n' "$now" > "$stamp" 2>/dev/null || true
  gig_brake_notify "🛑 GIG LOOP HELD (not earning) — $consumer refused to run.
$(gig_brake_describe)
brake: $GIG_BRAKE_FILE
release: bash $HOME/life-manager/skills/earn/gig/scripts/gig_brake.sh release --owner $GIG_BRAKE_OWNER"
}

# Expiry is the owner's own term coming due, not an unrelated controller clearing
# the flag. The file is kept as a record; only the block is lifted, loudly.
gig_brake_reap_if_expired() {
  gig_brake_read
  [ "$GIG_BRAKE_STATE" = "expired" ] || return 1
  local archive="$GIG_BRAKE_FILE.expired-$(date +%Y%m%dT%H%M%S)"
  mv "$GIG_BRAKE_FILE" "$archive" 2>/dev/null || return 1
  rm -f "$GIG_BRAKE_FILE.lastalarm" 2>/dev/null || true
  gig_brake_log "EXPIRED -> $archive ($(gig_brake_describe))"
  gig_brake_notify "▶️ GIG BRAKE EXPIRED — the loop is free to run again.
$(gig_brake_describe)
record: $archive
If the reason still stands, raise it again NOW."
  return 0
}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
gig_brake_cli() {
  local cmd="${1:-status}"; shift || true
  local owner="" reason="" ttl=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --owner) owner="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --ttl-minutes) ttl="${2:-}"; shift 2 ;;
      *) echo "gig_brake: unknown argument: $1" >&2; return 64 ;;
    esac
  done

  case "$cmd" in
    raise)
      [ -n "$owner" ] || { echo "gig_brake: --owner is mandatory (a brake with no owner has no one to clear it)" >&2; return 64; }
      [ -n "$reason" ] || { echo "gig_brake: --reason is mandatory (a brake with no reason is how the 10h outage happened)" >&2; return 64; }
      ttl="${ttl:-$GIG_BRAKE_DEFAULT_TTL_MINUTES}"
      case "$ttl" in ''|*[!0-9]*) echo "gig_brake: --ttl-minutes must be a positive integer" >&2; return 64 ;; esac
      [ "$ttl" -gt 0 ] || { echo "gig_brake: --ttl-minutes must be positive" >&2; return 64; }
      if [ "$ttl" -gt "$GIG_BRAKE_MAX_TTL_MINUTES" ]; then
        echo "gig_brake: --ttl-minutes $ttl exceeds the ${GIG_BRAKE_MAX_TTL_MINUTES}min cap; renew instead of latching" >&2
        return 64
      fi
      mkdir -p "$(dirname "$GIG_BRAKE_FILE")" || return 2
      local now expires
      now="$(date +%s)"; expires=$(( now + ttl * 60 ))
      # Single line per field: a reason with newlines would truncate the parse.
      printf 'owner=%s\nreason=%s\nraised_at=%s\nexpires_at=%s\n' \
        "$(printf '%s' "$owner" | tr '\n' ' ')" \
        "$(printf '%s' "$reason" | tr '\n' ' ')" \
        "$now" "$expires" > "$GIG_BRAKE_FILE" || return 2
      rm -f "$GIG_BRAKE_FILE.lastalarm" 2>/dev/null || true
      gig_brake_log "RAISED by $owner for ${ttl}min: $reason"
      gig_brake_notify "🛑 GIG BRAKE RAISED by $owner for ${ttl}min — the loop will refuse to run.
reason: $reason
brake: $GIG_BRAKE_FILE"
      echo "brake raised: $GIG_BRAKE_FILE (expires in ${ttl}min)"
      ;;
    renew)
      [ -e "$GIG_BRAKE_FILE" ] || { echo "gig_brake: no brake to renew at $GIG_BRAKE_FILE" >&2; return 1; }
      gig_brake_read
      ttl="${ttl:-$GIG_BRAKE_DEFAULT_TTL_MINUTES}"
      case "$ttl" in ''|*[!0-9]*) echo "gig_brake: --ttl-minutes must be a positive integer" >&2; return 64 ;; esac
      [ "$ttl" -le "$GIG_BRAKE_MAX_TTL_MINUTES" ] || { echo "gig_brake: --ttl-minutes exceeds ${GIG_BRAKE_MAX_TTL_MINUTES}min cap" >&2; return 64; }
      gig_brake_cli raise --owner "$GIG_BRAKE_OWNER" --reason "$GIG_BRAKE_REASON" --ttl-minutes "$ttl"
      ;;
    release)
      [ -e "$GIG_BRAKE_FILE" ] || { echo "gig_brake: no brake at $GIG_BRAKE_FILE"; return 0; }
      gig_brake_read
      [ -n "$owner" ] || { echo "gig_brake: --owner is mandatory to release (held by: $GIG_BRAKE_OWNER)" >&2; return 64; }
      rm -f "$GIG_BRAKE_FILE" "$GIG_BRAKE_FILE.lastalarm" || return 2
      gig_brake_log "RELEASED by $owner (was held by $GIG_BRAKE_OWNER: $GIG_BRAKE_REASON)"
      gig_brake_notify "▶️ GIG BRAKE RELEASED by $owner (was: $GIG_BRAKE_REASON) — the loop may run again."
      echo "brake released"
      ;;
    status)
      gig_brake_read
      case "$GIG_BRAKE_STATE" in
        held)    echo "HELD    $(gig_brake_describe)"; return 0 ;;
        expired) echo "EXPIRED $(gig_brake_describe) (no longer blocking; file kept as record)"; return 1 ;;
        *)       echo "ABSENT  no operator brake at $GIG_BRAKE_FILE"; return 1 ;;
      esac
      ;;
    alarm)
      if gig_brake_reap_if_expired; then echo "brake expired and reaped"; return 1; fi
      if gig_brake_is_held; then gig_brake_alarm_if_due "gig-healthcheck"; echo "HELD $(gig_brake_describe)"; return 0; fi
      return 1
      ;;
    *)
      echo "usage: gig_brake.sh {raise|renew|release|status|alarm} [--owner N] [--reason T] [--ttl-minutes N]" >&2
      return 64
      ;;
  esac
}

# Only act as a CLI when executed, never when sourced.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  gig_brake_cli "$@"
  exit $?
fi
