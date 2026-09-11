#!/usr/bin/env python3
"""
gcal departures engine — for each upcoming calendar event, compute the time
the user must LEAVE BY, working backwards from the event start:

    departBy = event_start - travel_time(origin -> location) - prep_buffer

Travel-time aware. Home base + all personal data come from the per-user profile
(identity/profile.json). Travel uses the Google Directions API (transit) when
available, and falls back to a conservative per-area estimate otherwise.

Outputs JSON to stdout: a list of {summary, startIso, location, travelMin,
prepMin, departByIso, travelSource} sorted by departBy.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
LIFE_MANAGER_HOME = Path(os.environ.get(
    "LIFE_MANAGER_HOME", str(Path.home() / ".local" / "state" / "life-manager"),
))
ANICCA_HOME = Path(os.environ.get("ANICCA_HOME", str(LIFE_MANAGER_HOME)))
sys.path.insert(0, str(REPO_ROOT / "skills" / "_shared"))
import anicca_profile as prof  # noqa: E402

JST = timezone(timedelta(hours=9))
HOME = prof.home_address()
LOCATION_STATE_DIR = ANICCA_HOME / "state" / "location"
LOCATION_FRESH_MIN = int(os.environ.get("LATE_LOCATION_FRESH_MIN", "45"))

# Routine event summaries that always happen AT HOME — kept in sync with
# lateness_check.ROUTINE_AT_HOME_PATTERNS. These get travel_min=0 when user is
# already at/near home, instead of the default 45-min travel estimate (the
# "called at 04:55 to leave for meditation" bug, Dais 2026-05-31).
ROUTINE_AT_HOME_PATTERNS = (
    "sleep", "睡眠", "就寝", "寝る",
    "wake", "起床",
    "meditat", "瞑想", "座禅",
    "breakfast", "朝食", "朝ごはん",
    "lunch", "昼食", "昼ごはん",
    "dinner", "夕食", "晩ごはん",
    "meal", "食事",
    "running", "🏃", "jog",  # home-base running route
)
# Travel blocks created by anicca-travel-fill — purely visual; we never fire
# lateness calls for these because the *destination* event after them already
# has depart_by computed with travel time baked in (the "double-count" bug).
TRAVEL_EVENT_PREFIXES = ("🚆", "🚌", "🚶", "🚇", "移動")


def is_travel_block(summary):
    s = (summary or "").strip()
    return any(s.startswith(p) for p in TRAVEL_EVENT_PREFIXES)


def is_routine_at_home(summary: str) -> bool:
    s = (summary or "").lower()
    return any(p in s for p in ROUTINE_AT_HOME_PATTERNS)
PREP_MIN = int(os.environ.get("LATE_PREP_MIN", "15"))        # get-ready buffer (home day-blocks)
ARRIVE_EARLY_MIN = int(os.environ.get("LATE_ARRIVE_EARLY_MIN", "5"))  # be there N min early
LEAD_MIN = int(os.environ.get("LATE_LEAD_MIN", "5"))  # depart_by lead before arrival_target

# Event-type-specific arrival buffer (Dais 2026-05-31 HARD RULE #19 + spec §4):
# Each user can override via profile.alarm.eventStyles[type].buffer in identity/profile.json.
EVENT_TYPE_BUFFER = {
    "airport_intl": 180,  # 国際線
    "airport_dom": 60,    # 国内線
    "shinkansen": 15,
    "hospital": 30,
    "remote": 5,          # HARD RULE (Dais 2026-06-01): Zoom/Meet — always call 10min
                          #   before event start. buf=5 + LEAD_MIN=5 = call at start-10.
    "exam": 60,
    "wake": 0,            # Wake event = the event start IS the action
    "sleep": 10,          # gentle reminder 10min before
    "meditation": 5,
    "running": 5,
    "lt": 15,
    "comedy": 25,         # 15min + 10min 受付 buffer
    "work": 15,
    "default": 15,
}


def classify_event_type(summary, description):
    """Classify event type from summary + description for buffer calculation.
    Mirrors _shared/lib/gcal-policy.sh classifier. Returns one of EVENT_TYPE_BUFFER keys."""
    s = f"{summary or ''} {description or ''}".lower()
    if any(k in s for k in ["✈", "international", "国際線", "narita t1", "narita t2", "haneda intl"]):
        return "airport_intl"
    if any(k in s for k in ["国内線", "domestic", "搭乗"]) or re.search(r"\b(jal|ana)\b", s):
        return "airport_dom"
    if any(k in s for k in ["新幹線", "shinkansen", "のぞみ", "ひかり", "こだま"]):
        return "shinkansen"
    if any(k in s for k in ["病院", "clinic", "hospital", "診察"]):
        return "hospital"
    if any(k in s for k in ["zoom", "google meet", "remote", "オンライン"]):
        return "remote"
    if any(k in s for k in ["🛏", "wake", "起床"]):
        return "wake"
    if any(k in s for k in ["😴", "sleep", "就寝"]):
        return "sleep"
    if any(k in s for k in ["🧘", "meditation", "瞑想"]):
        return "meditation"
    if any(k in s for k in ["🏃", "running", "jog", "ジョギング"]):
        return "running"
    if any(k in s for k in ["卒業式", "試験", "exam", "entrance"]):
        return "exam"
    if any(k in s for k in ["🎤", "lt", "ライブ", "登壇"]):
        return "lt"
    if any(k in s for k in ["🎭", "寄席", "comedy", "お笑い"]):
        return "comedy"
    if any(k in s for k in ["💼", "day job", "仕事", "naist"]):
        return "work"
    return "default"


def buffer_for_event(summary, description):
    """Return arrival_buffer (minutes before event start to be physically there)."""
    return EVENT_TYPE_BUFFER.get(classify_event_type(summary, description), 15)
HORIZON_H = int(os.environ.get("LATE_HORIZON_H", "168"))     # look-ahead window (= 7 days)
# Dual-horizon — list every event up to HORIZON_H (so the heartbeat sees the
# whole week), but only hit Google Directions for events within
# PRECOMPUTE_H. Beyond the precompute window we use AREA_ESTIMATE / static
# fallback. This keeps API cost flat (~$1/day) when horizon grew from 18 h
# to 168 h, and 5-day-out transit timetables aren't reliable anyway.
PRECOMPUTE_H = int(os.environ.get("LATE_PRECOMPUTE_H", "12"))
ENV_PATH = Path(os.environ.get("LIFE_MANAGER_ENV_FILE", str(ANICCA_HOME / ".env")))
ENV = ENV_PATH.read_text() if ENV_PATH.is_file() else ""
JST_RE = re.compile(r"出発[:：]\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|\d{1,2}:\d{2})")


def parse_departure(desc, start):
    """If Anicca already baked a departure time into the event description
    (HARD RULE #7: '出発:HH:MM ... 開始:HH:MM'), trust it as the leave-by time."""
    if not desc:
        return None
    m = JST_RE.search(desc)
    if not m:
        return None
    raw = m.group(1)
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw).astimezone(JST)
        hh, mm = map(int, raw.split(":"))
        return start.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        return None


def current_origin():
    """Travel origin = user's live Telegram Live Location.
    Returns (origin_str, kind) — kind ∈ {'telegram_fresh', 'home_fallback'}.

    Telegram bot writes ~/.local/state/life-manager/state/location/<user_id>.json every 1-5s
    while user is sharing Live Location. >LOCATION_FRESH_MIN stale or no file
    → fall back to HOME (caller is informed via the returned kind so it can
    flag the LLM)."""
    if not LOCATION_STATE_DIR.exists():
        return HOME, "home_fallback"
    files = sorted(LOCATION_STATE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return HOME, "home_fallback"
    try:
        d = json.loads(files[0].read_text())
        # received_at = wall-clock when bot last saved a signal. This is the right
        # freshness proxy because msg.edit_date on Telegram live updates can be
        # None or stale, while received_at always reflects "the share is alive".
        signal_ts = d.get("received_at") or d["tst"]
        age_min = (datetime.now(JST).timestamp() - signal_ts) / 60
        if age_min <= LOCATION_FRESH_MIN:
            return f"{d['lat']},{d['lon']}", "telegram_fresh"
    except Exception:
        pass
    return HOME, "home_fallback"

# Rough travel-minute estimate to common Tokyo areas (API-fallback only,
# imprecise — only used when Google Directions fails).
AREA_ESTIMATE = {
    "新宿": 15, "信濃町": 8, "四谷": 12, "渋谷": 28, "中野": 32, "大塚": 38,
    "池袋": 30, "中野坂上": 25, "なかの": 32, "高田馬場": 20, "東京": 30,
}
DEFAULT_TRAVEL_MIN = 45


def env(name, default=""):
    m = re.search(rf"^{name}=(.*)$", ENV, re.M)
    return (m.group(1).strip().strip('"').strip("'") if m else default)


def fetch_events():
    acct = env("GOG_ACCOUNT", "") or prof.google_account()
    # +1 day so we include the rollover when HORIZON_H lands inside a partial day
    to = (datetime.now(JST) + timedelta(hours=HORIZON_H) + timedelta(days=1)).strftime("%Y-%m-%d")
    # CRITICAL: --all-pages is required. `gog calendar events list` defaults to
    # --max=10 (single page). On busy days the actual MUST-attend events (e.g.
    # MUFG day job at 09:00, recurring) get pushed off the first page by other
    # ad-hoc events created earlier the same day. Today (2026-05-29) the morning
    # wake-up loop missed because 10 Anicca test events occupied page 1 and the
    # 9-17 MUFG day-job slid to page 2 -> gcal_departures returned no events ->
    # lateness_check returned "no-events" for the entire morning. Always page.
    out = subprocess.run(
        [os.environ.get("LIFE_MANAGER_GOG", "gog"), "calendar", "events", "list", "-j",
         "--account", acct, "--from", "today", "--to", to,
         "--all-pages", "--max", "250"],
        capture_output=True, text=True,
        env={**os.environ, "GOG_KEYRING_PASSWORD": env("GOG_KEYRING_PASSWORD"),
             "GOG_ACCOUNT": acct},
    )
    if out.returncode != 0:
        print(f"[gcal] gog failed: {out.stderr[:200]}", file=sys.stderr)
        return []
    d = json.loads(out.stdout)
    return d if isinstance(d, list) else d.get("events", d.get("items", []))


def _directions(dest, mode, key, origin, extra=None):
    params = {"origin": origin, "destination": dest, "mode": mode, "language": "ja", "key": key}
    if extra:
        params.update(extra)
    url = "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read().decode())
    if d.get("status") == "OK":
        return d["routes"][0]["legs"][0]["duration"]["value"] // 60
    return None


def directions_travel_min(dest, origin):
    """Real travel minutes origin->dest. Prefer transit; if the (legacy) transit
    feed has no route, fall back to driving x a Tokyo transit factor (walk+wait+
    train ~1.4x drive). Returns None to let the caller use the area estimate."""
    key = env("GOOGLE_API_KEY")
    if not key or not dest:
        return None
    try:
        t = _directions(dest, "transit", key, origin, {"departure_time": "now"})
        if t is not None:
            return t
    except Exception:
        pass
    try:
        d = _directions(dest, "driving", key, origin)
        if d is not None:
            return max(5, round(d * 1.4))
    except Exception:
        pass
    return None


def estimate_travel_min(text):
    for area, mins in AREA_ESTIMATE.items():
        if area in (text or ""):
            return mins
    return DEFAULT_TRAVEL_MIN


def travel_min(location, summary, origin, *, allow_api=True):
    """allow_api=False forces the static-estimate fallback. The caller flips
    this off for events beyond PRECOMPUTE_H so the heartbeat doesn't burn
    Google Directions calls on the back half of a 7-day horizon."""
    if allow_api:
        api = directions_travel_min(location, origin)
        if api is not None:
            return api, "api"
    return estimate_travel_min(f"{location or ''} {summary or ''}"), "estimate_far"


def main():
    now = datetime.now(JST)
    origin, origin_kind = current_origin()
    rows = []
    for e in fetch_events():
        s = e.get("start", {})
        dt = s.get("dateTime")
        if not dt:
            continue  # skip all-day blocks
        start = datetime.fromisoformat(dt).astimezone(JST)
        if start < now or start > now + timedelta(hours=HORIZON_H):
            continue
        summary = e.get("summary") or ""
        if is_travel_block(summary):
            continue  # visual-only block; the destination event handles depart_by
        loc = e.get("location")
        desc = e.get("description") or ""

        baked = parse_departure(desc, start)
        if baked is not None:
            # Anicca already computed the leave time (HARD RULE #7) — trust it,
            # do NOT subtract travel again (that was the "called too early" bug).
            depart = baked
            tmin, src = 0, "baked"
        elif is_routine_at_home(summary):
            # Wake / sleep / meditation / meal / home-running — happens at home.
            # No travel needed regardless of whether gcal-heal already filled in
            # the home address as location. Buffer = event-type specific.
            buf = buffer_for_event(summary, desc)
            tmin, src = 0, "routine_at_home"
            depart = start - timedelta(minutes=tmin + buf + LEAD_MIN)
        else:
            # Compute the real leave time from where Dais IS now (Dais 2026-05-31 spec §4):
            #   arrival_target = event.start - event_type_buffer
            #   depart_by = arrival_target - travel - LEAD_MIN
            # Event-type-specific buffer (airport 60-180min, sleep 10min, wake 0, lt 15, etc).
            #
            # Only hit Google Directions for events inside PRECOMPUTE_H — anything
            # farther out uses the static area-estimate. The transit timetable for
            # next week isn't stable yet and the precise minute drift doesn't matter
            # until the heartbeat re-evaluates closer to event start.
            allow_api = start <= now + timedelta(hours=PRECOMPUTE_H)
            tmin, src = travel_min(loc, summary, origin, allow_api=allow_api)
            buf = buffer_for_event(summary, desc) if loc else PREP_MIN  # located: type-aware; home: prep
            depart = start - timedelta(minutes=tmin + buf + LEAD_MIN)
        att = [a.get("email") for a in (e.get("attendees") or []) if a.get("email")]
        rows.append({
            "summary": summary, "startIso": start.isoformat(), "location": loc,
            "travelMin": tmin, "travelOrigin": origin, "travelOriginKind": origin_kind,
            "prepMin": (0 if baked else (ARRIVE_EARLY_MIN if loc else PREP_MIN)),
            "departByIso": depart.isoformat(), "travelSource": src,
            "minutesUntilDepart": int((depart - now).total_seconds() // 60),
            # context for dynamic renraku (who to notify, found per event)
            "description": (e.get("description") or "")[:600],
            "attendees": att,
            "organizer": (e.get("organizer") or {}).get("email"),
            "htmlLink": e.get("htmlLink"),
        })
    rows.sort(key=lambda r: r["departByIso"])
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
