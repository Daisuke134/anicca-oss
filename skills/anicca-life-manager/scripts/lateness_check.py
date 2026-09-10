#!/usr/bin/env python3
"""
lateness check — the core of the never-be-late loop.

Combines (a) gcal departure times [gcal_departures.py] with (b) the user's live
location [loco /loc/latest] and decides whether to fire a realtime "you must
leave NOW" phone call.

Decision (pure, unit-tested in decide()):
  late risk  ⟺  the next event's departBy is within LEAD minutes (or past)
                AND the user is still home (within radius) AND not moving.

On late risk: place a realtime lateness-mode call (Twilio <-> Gemini Live, the
imokenet bridge) with a context string describing the event + deadline, and
report to Slack. (Stakeholder renraku = task #18, layered on top.)
"""
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import base64
import gzip
import shutil
import fcntl
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
LIFE_MANAGER_HOME = Path(os.environ.get(
    "LIFE_MANAGER_HOME", str(Path.home() / ".local" / "state" / "life-manager"),
))
LIFE_MANAGER_STATE_ROOT = Path(os.environ.get(
    "LIFE_MANAGER_STATE_ROOT", str(LIFE_MANAGER_HOME / "lateness-heartbeat"),
))
LOOP_STATE_DIR = LIFE_MANAGER_STATE_ROOT / "state"
ANICCA_HOME = Path(os.environ.get("ANICCA_HOME", str(LIFE_MANAGER_HOME)))
ENV_PATH = Path(os.environ.get(
    "LIFE_MANAGER_ENV_FILE",
    str(ANICCA_HOME / ".env"),
))
ENV = ENV_PATH.read_text() if ENV_PATH.is_file() else ""
URL_FILE = Path(os.environ.get(
    "LIFE_MANAGER_CALL_BRIDGE_URL_FILE",
    str(ANICCA_HOME / "state" / "call-bridge" / "public_url.txt"),
))
DEPART_SCRIPT = SCRIPT_DIR / "gcal_departures.py"
LOCATION_STATE_DIR = ANICCA_HOME / "state" / "location"
HEARTBEAT_LOG_MAX_BYTES = int(os.environ.get("LIFE_MANAGER_HEARTBEAT_LOG_MAX_BYTES", 64 * 1024 * 1024))


def _next_heartbeat_archive(ledger: Path, stamp: str) -> Path:
    base = ledger.with_name(f"{ledger.stem}.{stamp}.jsonl.gz")
    candidate = base
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = ledger.with_name(f"{ledger.stem}.{stamp}.{suffix}.jsonl.gz")
    return candidate


def _archive_heartbeat_source(source: Path, ledger: Path, stamp: str) -> None:
    archive = _next_heartbeat_archive(ledger, stamp)
    temporary = archive.with_name(f".{archive.name}.{os.getpid()}.tmp")
    with source.open("rb") as input_file, gzip.open(temporary, "wb") as output_file:
        shutil.copyfileobj(input_file, output_file)
    os.replace(temporary, archive)
    source.unlink()


def _rotate_heartbeat_log_if_needed(ledger: Path) -> None:
    """Archive oversized heartbeat JSONL without dropping any event lines."""
    rotating = None
    temporary = None
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        restored = False
        for orphan in sorted(ledger.parent.glob(f".{ledger.name}.*.rotating")):
            if not restored and (not ledger.exists() or ledger.stat().st_size == 0):
                os.replace(orphan, ledger)
                restored = True
            else:
                _archive_heartbeat_source(orphan, ledger, stamp)
        if not ledger.exists() or ledger.stat().st_size <= HEARTBEAT_LOG_MAX_BYTES:
            return
        rotating = ledger.with_name(f".{ledger.name}.{stamp}.rotating")
        os.replace(ledger, rotating)
        ledger.touch()
        _archive_heartbeat_source(rotating, ledger, stamp)
    except Exception:
        # Preserve the rotating source and active ledger on any failure; the
        # next heartbeat can retry without losing the receipt history.
        try:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        except Exception:
            pass
        try:
            if rotating is not None and rotating.exists() and (
                not ledger.exists() or ledger.stat().st_size == 0
            ):
                os.replace(rotating, ledger)
        except Exception:
            pass

# Routine event summaries that always happen at home — when gcal location is empty,
# auto-resolve to profile.identity.homeAddress instead of letting the LLM fabricate
# a station name (the "Shinagawa 駅 行って sleep" bug, 2026-05-31).
ROUTINE_AT_HOME_PATTERNS = (
    "sleep", "睡眠", "就寝", "寝る",
    "wake", "起床",
    "meditat", "瞑想", "座禅",
    "breakfast", "朝食", "朝ごはん",
    "lunch", "昼食", "昼ごはん",
    "dinner", "夕食", "晩ごはん",
    "meal", "食事",
)


def is_routine_at_home(summary: str) -> bool:
    s = (summary or "").lower()
    return any(p in s for p in ROUTINE_AT_HOME_PATTERNS)


ADDR_PATTERNS = (
    re.compile(r"(〒\d{3}-\d{4}\s*[^,;()\n　]+)"),
    re.compile(r"((?:北海道|東京都|京都府|大阪府|[^\s]{1,3}県)[^\s,;()\n　]{2,40})"),
    re.compile(r"([A-Z]{3,8}\s+〒?\d{0,3}-?\d{0,4}\s*[^\s,;()\n　]+)"),
    re.compile(r"([一-龯ぁ-んァ-ヶ]{2,8}駅)"),
)


def _extract_address(text):
    if not text:
        return None
    for pat in ADDR_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def resolve_event_destination(event):
    """Return (address, kind) for the event's destination.
      kind = 'explicit'           → gcal event had a location field
             'home_routine'       → routine event (sleep/wake/etc), forced to home
             'summary_extracted'  → regex-pulled JP address from summary/desc
             'unknown'            → no location; LLM must ASK, not fabricate
    """
    loc = (event or {}).get("location") or ""
    if loc.strip():
        return loc.strip(), "explicit"
    if is_routine_at_home(event.get("summary", "")):
        return prof.home_address(), "home_routine"
    extracted = (
        _extract_address(event.get("summary", ""))
        or _extract_address(event.get("description", ""))
    )
    if extracted:
        return extracted, "summary_extracted"
    return None, "unknown"

# Home base + all personal data come from the per-user profile (OSS-general).
# The repository-local adapter is canonical; the old OpenClaw path remains only
# as a migration fallback while loaded legacy loops are kept running.
REPO_SHARED = Path(__file__).resolve().parents[2] / "_shared"
sys.path.insert(0, str(REPO_SHARED))
import anicca_profile as prof  # noqa: E402

try:
    _hlat, _hlon = prof.home_latlon()
except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
    _hlat, _hlon = None, None
HOME_LAT = float(os.environ.get("LATE_HOME_LAT") or _hlat) if (os.environ.get("LATE_HOME_LAT") or _hlat) is not None else None
HOME_LON = float(os.environ.get("LATE_HOME_LON") or _hlon) if (os.environ.get("LATE_HOME_LON") or _hlon) is not None else None
HOME_RADIUS_M = float(os.environ.get("LATE_HOME_RADIUS_M", "300"))
LEAD_MIN = int(os.environ.get("LATE_LEAD_MIN", "8"))         # call ~this far before the real leave time
NUDGE_MIN = int(os.environ.get("LATE_NUDGE_MIN", "20"))      # gentle Slack nudge this far before
MOVING_VEL = float(os.environ.get("LATE_MOVING_VEL", "0.8")) # m/s -> considered moving
QUIET_OVERRIDE_MIN = int(os.environ.get("LATE_QUIET_OVERRIDE_MIN", "30"))  # imminent wake/action call punches through quiet hours
STALE_MIN = int(os.environ.get("LATE_STALE_MIN", "10"))      # Telegram Live Location updates 1-5s while sharing; >10m stale = bot died or user stopped sharing

# Capafy reject R2 fix: bound the "RELENTLESS" call loop (was 6 → 3).
RELENTLESS_MAX_DEFAULT = 3


def life_manager_enabled(profile: dict) -> bool:
    """profile.json の lifeManager.enabled。未指定なら True(既定ON)。False で全停止 (Capafy reject R2: pause/stop手段)。"""
    lm = (profile or {}).get("lifeManager") or {}
    return lm.get("enabled", True) is not False


def env(name, default=""):
    if name in os.environ:
        return os.environ[name]
    m = re.search(rf"^{name}=(.*)$", ENV, re.M)
    return (m.group(1).strip().strip('"').strip("'") if m else default)


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def arrival_radius_for(dest_addr, default=400):
    """Event-type aware "you have arrived" threshold (meters).

    Home is recognised by string-containing the user's own home_address
    from profile (no hard-coded ward name). Falls back to defaults for
    stations / airports / specific street addresses.
    """
    if not dest_addr:
        return default
    a = dest_addr
    if "駅" in a:
        return 200            # station — within walking distance of any platform
    try:
        home = prof.home_address() or ""
    except Exception:
        home = ""
    if home and (home in a or a in home):
        return 100            # home — string-match the user's actual address
    if any(k in a for k in ("空港", "Airport", "airport")):
        return 800            # airport terminal — wide
    if any(k in a for k in ("〒", "丁目", "番地")):
        return 80             # specific street address — building-level
    return default


def decide(now, location, departures, home=None, home_radius_m=None, dest=None, arrive_radius_m=400):
    """Pure decision. Returns dict {action, reason, event}.
    action in {'call', 'ok', 'no-events', 'no-location', 'stale-location'}.
    home=(lat,lon) overrides module HOME_*; dest=(lat,lon) of the next event lets us
    detect 'already arrived' (so we don't nag once he's there)."""
    if not departures:
        return {"action": "no-events", "reason": "no upcoming events", "event": None}
    if not location:
        return {"action": "no-location", "reason": "no location fix", "event": None}

    home_lat, home_lon = home if home else (HOME_LAT, HOME_LON)
    radius = home_radius_m if home_radius_m is not None else HOME_RADIUS_M
    nxt = departures[0]  # earliest departBy
    depart = datetime.fromisoformat(nxt["departByIso"]).astimezone(JST)
    mins = (depart - now).total_seconds() / 60
    dist = haversine_m(home_lat, home_lon, location["lat"], location["lon"])
    at_home = dist <= radius
    vel = location.get("vel")
    moving = vel is not None and vel >= MOVING_VEL
    age_min = (now.timestamp() - location["tst"]) / 60

    # STALE-LOCATION RULE (Telegram Live Location source, 2026-05-31).
    # Telegram publishes every 1-5s while user is sharing Live Location.
    # Stale > STALE_MIN → bot died OR user stopped sharing.
    #
    # 2026-06-01 fix: gate the stale-CALL on "next event is imminent". The
    # previous version always returned action=call regardless of how far away
    # the next event was — i.e. at 17:00 it would ring the phone about a 23:00
    # Sleep event just because Telegram Live Location was off. That's hostile
    # to the user.
    #
    #   stale AND mins_to_depart <= LEAD_MIN * 3  → CALL ("imminent + can't see you")
    #   stale AND mins_to_depart >  LEAD_MIN * 3  → silent OK ("come back later")
    if age_min > STALE_MIN:
        mins_to_depart_for_stale = (depart - now).total_seconds() / 60
        if mins_to_depart_for_stale > LEAD_MIN * 3:
            return {"action": "ok",
                    "reason": (f"location {int(age_min)}m stale, but next event "
                               f"in {int(mins_to_depart_for_stale)}m — defer call"),
                    "event": nxt}
        return {"action": "call",
                "reason": f"location {int(age_min)}m stale — Telegram Live Location may be off; calling to confirm where the user is",
                "event": nxt}

    # UNIFIED model: departBy is computed from his CURRENT location's ETA (not home),
    # so this works wherever he is. "Leave now" = it's time to leave THIS spot to
    # arrive on time. (gcal_departures sets travelMin/departBy from current_origin.)
    travel = nxt.get("travelMin")

    # Routine-at-home events (wake / sleep / meditation / meds / home-run) happen
    # AT home — "being at the destination" is EXACTLY when we must call to prompt
    # the action (Dais 2026-06-09: "even if next event is in the same place they
    # should call me... wake up and meditate and go for a run"). So we do NOT take
    # the arrived→ok shortcut for these; we fall through to the departBy timing
    # check below, which fires action=call when it's time to do the action.
    is_action_at_place = (
        is_routine_at_home(nxt.get("summary", ""))
        or nxt.get("travelSource") in ("routine_at_home", "baked")
    )

    # Already at / near the destination -> nothing to do (TRAVEL events only).
    # Use real distance to dest, NOT travelMin (travelMin=0 also for 'baked').
    if dest and not is_action_at_place:
        dist_dest = haversine_m(dest[0], dest[1], location["lat"], location["lon"])
        if dist_dest <= arrive_radius_m:
            return {"action": "ok", "reason": f"arrived ({int(dist_dest)}m from dest)", "event": nxt}

    # Actively moving toward it (on foot/train). If departBy already passed he's
    # running behind → fire a realtime GUIDE call (Google-Maps-style "you won't make
    # it, hurry"). Otherwise he's on track → reassess next tick.
    if moving:
        if mins <= 0:
            return {"action": "guide",
                    "reason": f"移動中だが departBy を {int(-mins)}m 超過 — 間に合わせるため急かす",
                    "event": nxt}
        return {"action": "ok", "reason": f"en route, moving (vel={vel}) — 間に合う見込み", "event": nxt}

    # Stationary + fresh: if it's time to leave from where he is now, call & guide.
    if mins <= LEAD_MIN:
        return {"action": "call",
                "reason": f"departBy in {int(mins)}m — must leave now (travel {travel}m from current location)",
                "event": nxt}
    if mins <= NUDGE_MIN:
        return {"action": "nudge",
                "reason": f"departBy in {int(mins)}m — get ready to leave soon",
                "event": nxt}
    return {"action": "ok",
            "reason": f"departBy in {int(mins)}m, travel {travel}m, moving={moving}",
            "event": nxt}


# ---- IO ----
def get_location():
    """Read latest Telegram Live Location fix from ~/.local/state/life-manager/state/location/<user_id>.json.

    Bot writes one file per user (key = telegram user id). If multiple files exist
    we take the freshest. None = no Live Location sharing active → upstream calls
    will trigger a stale-location call asking user to /share location.

    Freshness semantics:
      tst         = the *measurement* timestamp Telegram tagged on this fix
      received_at = wall-clock time the bot last saved a signal
    For "is the share alive?" we use received_at (= bot got a message), because
    msg.edit_date can be stale or None on edited_message live updates.
    We expose received_at as the canonical age in 'tst' so the rest of the code
    treats stale = bot stopped getting signals.
    """
    if not LOCATION_STATE_DIR.exists():
        return None
    # Only Telegram live-location files are bare <telegram_user_id>.json (all digits).
    # realtime_guide.py writes guide_state_*.json + itinerary_*.json into the SAME
    # dir; those have no top-level lat/lon. Without this filter get_location() picks
    # the freshest non-location file → KeyError → None → heartbeat reports
    # no-location forever even when a fresh GPS fix exists (regression 2026-06-08).
    files = sorted(
        (p for p in LOCATION_STATE_DIR.glob("*.json") if p.stem.isdigit()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not files:
        return None
    try:
        rec = json.loads(files[0].read_text())
        signal_ts = rec.get("received_at") or rec["tst"]
        return {
            "lat": rec["lat"],
            "lon": rec["lon"],
            "tst": signal_ts,            # = received_at, the right thing for staleness
            "measured_at": rec["tst"],   # raw Telegram measurement timestamp (info only)
            "vel": None,
            "acc": rec.get("accuracy_m"),
        }
    except Exception:
        return None


def geocode_place(address):
    """Address/place string -> (lat, lon) for arrival detection. None on failure."""
    key = env("GOOGLE_API_KEY")
    if not (address and key):
        return None
    try:
        q = urllib.parse.urlencode({"address": address, "key": key, "language": "ja"})
        with urllib.request.urlopen(f"https://maps.googleapis.com/maps/api/geocode/json?{q}", timeout=8) as r:
            j = json.loads(r.read().decode())
        loc = j["results"][0]["geometry"]["location"]
        return (loc["lat"], loc["lng"])
    except Exception:
        return None


def reverse_geocode(loc):
    """lat/lon -> short JP place name (for a location-aware call message)."""
    key = env("GOOGLE_API_KEY")
    if not (loc and key):
        return None
    try:
        q = urllib.parse.urlencode({"latlng": f"{loc['lat']},{loc['lon']}", "key": key, "language": "ja"})
        with urllib.request.urlopen(f"https://maps.googleapis.com/maps/api/geocode/json?{q}", timeout=8) as r:
            j = json.loads(r.read().decode())
        comps = j["results"][0]["address_components"]
        # prefer ward + neighbourhood (e.g. <city/区> + <neighbourhood>) over full address
        parts = [c["long_name"] for c in comps
                 if any(t in c["types"] for t in ("sublocality_level_1", "sublocality_level_2", "locality"))]
        return "".join(parts[-2:]) if parts else j["results"][0]["formatted_address"]
    except Exception:
        return None


def get_departures():
    out = subprocess.run([sys.executable, str(DEPART_SCRIPT)], capture_output=True, text=True, timeout=120)
    try:
        return json.loads(out.stdout)
    except Exception:
        print(f"[late] departures parse failed: {out.stderr[:200]}", file=sys.stderr)
        return []


def _twilio_call_status(call_sid: str):
    """GET /Accounts/{sid}/Calls/{call_sid}.json — for RELENTLESS outcome polling."""
    acct = env("TWILIO_ACCOUNT_SID")
    tok = env("TWILIO_AUTH_TOKEN")
    if not (acct and tok and call_sid):
        return None
    try:
        import base64
        url = f"https://api.twilio.com/2010-04-01/Accounts/{acct}/Calls/{call_sid}.json"
        auth = base64.b64encode(f"{acct}:{tok}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as exn:
        print(f"[late] twilio status fetch failed: {exn}", file=sys.stderr)
        return None


def _wait_for_call_outcome(call_sid: str, deadline_sec: int = 120):
    """Poll Twilio until the call reaches a terminal state. Returns (status, duration_sec)."""
    terminal = {"completed", "no-answer", "busy", "failed", "canceled"}
    end = time.time() + deadline_sec
    last = ("unknown", 0)
    while time.time() < end:
        d = _twilio_call_status(call_sid)
        if not d:
            time.sleep(3); continue
        status = d.get("status") or "unknown"
        dur = int(d.get("duration") or 0)
        last = (status, dur)
        if status in terminal:
            return last
        time.sleep(3)
    return last


def _wait_for_available_call(call_sid: str):
    """Poll a real call only; an unavailable optional phone channel returns immediately."""
    if call_sid is None:
        return None
    return _wait_for_call_outcome(call_sid, deadline_sec=120)


def _user_moved(origin_loc, fresh_loc, threshold_m: int):
    """True if user moved more than threshold_m between origin_loc and fresh_loc."""
    if not (origin_loc and fresh_loc):
        return False
    try:
        d = haversine_m(origin_loc["lat"], origin_loc["lon"],
                        fresh_loc["lat"], fresh_loc["lon"])
        return d >= threshold_m
    except Exception:
        return False


def gemini_reachable():
    """Pre-flight: is Gemini API callable? If billing is in dunning / key blocked (403),
    placing the call only yields Twilio's robotic 'application error' (no Charon). Skip + alert.
    Returns (ok: bool, reason: str)."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    if not key:
        return False, "no GEMINI/GOOGLE_API_KEY"
    try:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
            data=b'{"contents":[{"parts":[{"text":"ping"}]}]}',
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return (r.status == 200), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")[:120]
        except Exception:
            pass
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def place_lateness_call(ctx):
    """Fire the lateness-mode call.

    A managed dial-out endpoint is optional.  When one is not configured, keep
    the lateness/Telegram path alive and skip the unavailable phone channel.
    """
    # Pre-flight: skip a doomed call (Twilio robotic "application error") when Gemini Live is down.
    ok, why = gemini_reachable()
    if not ok:
        slack(f"📵 lateness call SKIPPED — Gemini Live unreachable ({why}). No Charon voice would connect. "
              f"Fix: resolve Google Cloud billing (dunning) or swap to a funded GEMINI_API_KEY. ctx: {ctx[:120]}")
        print(f"[late] call skipped — gemini unreachable: {why}")
        return None
    base = os.environ.get("ANICCA_PHONE_DIALOUT_URL", "").strip().rstrip("/")
    if not base:
        print("[late] call skipped — no managed dial-out endpoint")
        return None
    to = os.environ.get("LATE_PHONE") or prof.phone() or os.environ.get("DAIS_PHONE_SMS_INTL")
    # Build Gemini Live system_instruction with location + route awareness.
    # sutando /call expects {to, message}; message is passed verbatim as the
    # Gemini Live system_instruction (= "purpose" param in TwiML chain).
    message = _build_anicca_voice_prompt(ctx, prof.name() or "the user")
    body = json.dumps({"to": to, "message": message}).encode()
    req = urllib.request.Request(
        f"{base}/call",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        return resp.get("callSid") or resp.get("call_sid")
    except (OSError, ValueError) as exc:
        print(f"[late] call skipped — dial-out unavailable: {exc}")
        return None


def _build_anicca_voice_prompt(ctx: str, name: str) -> str:
    """Compose system_instruction for sutando + Gemini Live.

    Constraint: Twilio rejects TwiML URLs > 4000 chars (error 21205).
    Sutando encodes the entire message as a query param, so this prompt
    must stay < 1500 chars raw (URL-encoded ~4500 max).

    Reads live GPS + active itinerary, builds a tight Japanese persona.
    """
    base_dir = ANICCA_HOME / "state" / "location"  # GPS
    guide_dir = ANICCA_HOME / "state" / "guide"    # itinerary
    uid = os.environ.get("LIFE_MANAGER_TELEGRAM_USER_ID") or os.environ.get("DAIS_TELEGRAM_USER_ID")
    if not uid:
        raise RuntimeError("LIFE_MANAGER_TELEGRAM_USER_ID is required")
    parts = [
        f"你 は アニッチャ、 {name}さん の 男友達。 電話 中。",
        "声 = 落ち着いた 男 (Charon)。 1ターン 1-2文 だけ。",
        "話したら 必ず 沈黙 して 相手 の 返事 を 待つ。 遮ら ない。",
        "「ok」「分かった」 で 「了解、また」 → hang_up。",
        "100% 日本語、 絵文字/マークダウン 禁止。",
        f"最初: 「{name}さん、アニッチャ です」 → 理由1文 → 沈黙。",
    ]
    try:
        loc = json.loads((base_dir / f"{uid}.json").read_text())
        age = int(time.time() - loc.get("received_at", 0))
        parts.append(f"位置: {loc['lat']:.4f},{loc['lon']:.4f} (GPS {loc.get('accuracy_m')}m, {age}s前)")
    except Exception:
        pass
    try:
        itin = json.loads((guide_dir / f"itinerary_{uid}.json").read_text())
        it = itin.get("itinerary", {})
        parts.append(
            f"次予定: {itin.get('query','?')} 着、 経路 {it.get('duration',0)//60}分、 乗換 {it.get('transfers',0)}回"
        )
    except Exception:
        pass
    if ctx:
        # cap ctx to avoid blowing the URL budget
        parts.append(f"判定: {ctx[:400]}")
    out = "\n".join(parts)
    # Hard cap (= 1500 chars), in case ctx is long. Twilio URL limit safety.
    return out[:1500]


def _read_url_file(p: Path) -> str:
    try:
        return p.read_text().strip()
    except Exception:
        return ""


def slack(text):
    try:
        req = urllib.request.Request("https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": env("SLACK_CHANNEL_ID"), "text": text}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {env('SLACK_BOT_TOKEN')}"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"[late] slack failed: {e}", file=sys.stderr)


def _in_quiet_hours(now):
    """Return True if `now` falls inside the user's quiet window.

    profile.alarm.quietHoursStart / quietHoursEnd are "HH:MM" strings (JST).
    Window can cross midnight (e.g. 23:30 -> 05:30).
    Defaults: no quiet hours = always active.
    """
    try:
        start = prof.get("alarm.quietHoursStart", "")
        end = prof.get("alarm.quietHoursEnd", "")
    except Exception:
        return False
    if not start or not end:
        return False
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
    except Exception:
        return False
    cur_min = now.hour * 60 + now.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if start_min <= end_min:                # same-day window (e.g. 13:00-15:00)
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min  # cross-midnight


def main():
    now = datetime.now(JST)
    # Capafy reject R2: 全停止スイッチ。lifeManager.enabled:false で routine call/mail を止める。
    try:
        if not life_manager_enabled(prof.load_profile()):
            print(json.dumps({"action": "disabled", "reason": "lifeManager.enabled=false"}, ensure_ascii=False))
            return
    except Exception:
        pass  # profile読込失敗時は既定ON（従来動作維持）
    loc_now = get_location()
    deps = get_departures()
    # Quiet hours silence routine polling — BUT an imminent action event
    # (wake / meditation / meds / sleep) must punch through, because the whole
    # point of a wake call is to fire while the user would otherwise be asleep
    # (Dais 2026-06-09: "they are not calling me when i wake up. when i sleep").
    # Override: if the next event's departBy is within QUIET_OVERRIDE_MIN, run.
    if _in_quiet_hours(now):
        override = False
        if deps:
            nxt0 = deps[0]
            try:
                dep0 = datetime.fromisoformat(nxt0["departByIso"]).astimezone(JST)
                mins0 = (dep0 - now).total_seconds() / 60
                if -5 <= mins0 <= QUIET_OVERRIDE_MIN and (
                    is_routine_at_home(nxt0.get("summary", ""))
                    or nxt0.get("travelSource") in ("routine_at_home", "baked")
                ):
                    override = True
            except Exception:
                pass
        if not override:
            print(json.dumps({"action": "quiet-hours", "reason": "user is asleep"}, ensure_ascii=False))
            return
    dest_addr, dest_kind = (None, "unknown")
    if deps:
        dest_addr, dest_kind = resolve_event_destination(deps[0])
    dest = geocode_place(dest_addr) if dest_addr else None
    radius = arrival_radius_for(dest_addr)
    d = decide(now, loc_now, deps, dest=dest, arrive_radius_m=radius)
    print(json.dumps({k: (v if k != "event" else (v or {}).get("summary")) for k, v in d.items()}, ensure_ascii=False))

    # 100%-call-coverage ledger. With HORIZON_H bumped to 168 h (1 week)
    # we need a grep-able trail to verify every gcal event in the horizon
    # was seen by at least one heartbeat and that decide() acted on every
    # one whose depart_by came up. One jsonl line per heartbeat; never
    # slack-noisy. To audit: jq 'select(.n_events > 0)' on the jsonl,
    # cross-check against the same week's gcal export.
    try:
        ledger = LOOP_STATE_DIR / "heartbeat_log.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        lock = ledger.with_name(f".{ledger.name}.lock")
        with lock.open("a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            _rotate_heartbeat_log_if_needed(ledger)
            with ledger.open("a") as f:
                f.write(json.dumps({
                    "ts": now.isoformat(),
                    "n_events": len(deps),
                    "events": [
                        {
                            "summary": e.get("summary"),
                            "startIso": e.get("startIso"),
                            "departByIso": e.get("departByIso"),
                            "travelSource": e.get("travelSource"),
                            "minutesUntilDepart": e.get("minutesUntilDepart"),
                        }
                        for e in deps
                    ],
                    "decided_action": d.get("action"),
                    "decided_event": (d.get("event") or {}).get("summary"),
                }, ensure_ascii=False) + "\n")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception as ex:
        print(f"[late] ledger write failed: {ex}", file=sys.stderr)
    if d["action"] == "nudge":
        e = d["event"]
        depart = datetime.fromisoformat(e["departByIso"]).astimezone(JST).strftime("%H:%M")
        once_path = LOOP_STATE_DIR / "nudge_sent.json"
        once_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            done = json.loads(once_path.read_text())
        except Exception:
            done = []
        key = f"{e['summary']}|{e['departByIso']}"
        if key not in done:
            loc = f"（{e['location']}）" if e.get("location") else ""
            slack(f"⏰ そろそろ準備を。『{e['summary']}』{loc}は {depart} に家を出る予定。")
            done.append(key)
            once_path.write_text(json.dumps(done, ensure_ascii=False))
            print(f"[late] nudge sent for {e['summary']}")

    if d["action"] == "guide":
        # Moving but behind schedule → realtime Google-Maps-style hurry-up call.
        e = d["event"]
        guide_addr, _ = resolve_event_destination(e)
        try:
            subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "guide_me_now.py"), guide_addr or ""], timeout=120)
            slack(f"🧭 移動中催促コール: {d['reason']} → {e['summary']}")
            print(f"[late] guide call fired for {e['summary']}")
        except Exception as ex:
            print(f"[late] guide failed: {ex}")

    if d["action"] == "call":
        e = d["event"]
        # Race lock: a single 5-min cron tick should not fire two POST /dialouts
        # for the same event. Also prevents 'overlapping cron x cron' (= cron
        # 06:50 still running when 06:55 fires) from double-ringing the phone.
        # We keep a per-event timestamp; entries older than RACE_LOCK_SEC are
        # treated as expired (= next tick can legitimately call again).
        lock_path = LOOP_STATE_DIR / "active_call_loop.json"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            locks = json.loads(lock_path.read_text())
        except Exception:
            locks = {}
        RACE_LOCK_SEC = int(os.environ.get("LATE_RACE_LOCK_SEC", "60"))
        lock_key = f"{e['summary']}|{e['departByIso']}"
        last_ts = locks.get(lock_key, 0)
        if (now.timestamp() - last_ts) < RACE_LOCK_SEC:
            print(json.dumps({
                "action": "ok",
                "reason": f"race-lock active ({int(now.timestamp() - last_ts)}s ago)",
                "event_summary": e["summary"],
            }, ensure_ascii=False))
            return
        locks[lock_key] = int(now.timestamp())
        # Trim stale entries so the file doesn't grow forever
        cutoff = now.timestamp() - 86400
        locks = {k: v for k, v in locks.items() if v >= cutoff}
        lock_path.write_text(json.dumps(locks, ensure_ascii=False, indent=2))

        start = datetime.fromisoformat(e["startIso"]).astimezone(JST).strftime("%H:%M")
        depart_dt = datetime.fromisoformat(e["departByIso"]).astimezone(JST)
        depart = depart_dt.strftime("%H:%M")
        place = reverse_geocode(loc_now) if loc_now else None
        place = place or "現在地不明"
        travel = e.get("travelMin")
        travel_str = f"そこから約{travel}分、" if travel else ""

        # Resolve destination explicitly. Routine events at home get
        # profile.home_address() so the LLM never fabricates a station name
        # (the "Shinagawa 駅 for sleep" bug, 2026-05-31).
        dest_addr, dest_kind = resolve_event_destination(e)

        # Pre-compute the actual transit route via Google Maps Web. Skip when
        # destination unknown (would just hallucinate). For home_routine we look
        # it up against the home address.
        route_block = ""
        if loc_now and dest_addr:
            try:
                origin_str = f"{loc_now['lat']},{loc_now['lon']}"
                r = subprocess.run(
                    [sys.executable,
                     str(Path(__file__).resolve().parent / "route_lookup.py"),
                     "--origin", origin_str,
                     "--destination", dest_addr],
                    capture_output=True, text=True, timeout=45,
                )
                if r.returncode == 0:
                    route = json.loads(r.stdout)
                    if route.get("ok") and route.get("summary"):
                        bits = [f"推奨ルート(Google Maps): {route['summary']}"]
                        if route.get("duration_min"):
                            bits.append(f"所要 {route['duration_min']}分")
                        if route.get("fare_yen"):
                            bits.append(f"運賃 {route['fare_yen']}円")
                        if route.get("departure_time"):
                            bits.append(f"次発 {route['departure_time']}")
                        route_block = "。" + "、".join(bits)
            except Exception as exn:
                print(f"[late] route_lookup failed: {exn}", file=sys.stderr)

        # Destination phrasing — never let the LLM guess.
        if dest_kind == "explicit":
            dest_line = f"、場所は {dest_addr}"
        elif dest_kind == "home_routine":
            dest_line = f"、場所は自宅 ({dest_addr})"
        else:
            dest_line = (
                "、場所は Google カレンダーに未記入。"
                " {name}に直接『どこでやる予定?』と聞いてから案内すること。"
                " 駅名や住所を勝手に推測してはいけない"
            )

        # Action phrasing depends on event type — home-routine events don't
        # require leaving anywhere; they require waking up / starting the
        # activity. Travel-required events require physical movement.
        if dest_kind == "home_routine":
            ev_kind = (e.get("summary") or "").lower()
            if any(k in ev_kind for k in ("sleep", "睡眠", "就寝")):
                action_line = (
                    f"。今は寝る時刻。 まだ起きてるなら寝床へ行くよう促す。"
                    f" {depart} までに横にならないとパフォーマンス落ちる。"
                )
            elif any(k in ev_kind for k in ("wake", "起床", "🛏")):
                action_line = (
                    f"。これは起床コール。 まだ寝てるなら今すぐ起き上がるよう促す。"
                    f" 上半身を起こす → 水を飲む → 顔を洗う、 ステップごと案内。"
                )
            elif any(k in ev_kind for k in ("meditat", "瞑想", "座禅")):
                action_line = (
                    f"。瞑想開始時刻が近い。 自宅にいるなら瞑想スペースへ。"
                    f" すでに座ってるなら呼吸に集中するよう促す。"
                )
            elif any(k in ev_kind for k in ("running", "🏃", "jog")):
                action_line = (
                    f"。ランニング開始時刻。 自宅にいるなら靴履いて玄関へ。"
                    f" 今出れば {start} に走り始められる。"
                )
            elif any(k in ev_kind for k in ("meal", "breakfast", "朝食", "朝ごはん",
                                            "lunch", "昼食", "昼ごはん",
                                            "dinner", "夕食", "晩ごはん", "食事")):
                action_line = (
                    f"。食事時刻。 何を食べる予定か聞いて、 ステップを促す。"
                )
            else:
                action_line = (
                    f"。自宅で行う予定。 場所は自宅、 移動は不要。"
                    f" 何を始めるべきか聞いて促す。"
                )
            ctx = (
                f"次の予定『{e['summary']}』は {start} 開始"
                + dest_line
                + f"。今{prof.name() or 'the user'}は{place}にいる"
                + action_line
                + " 自宅にいない場合は自宅へ戻るよう促す (= 自宅予定なので)。"
            )
        else:
            ctx = (
                f"次の予定『{e['summary']}』は {start} 開始"
                + dest_line
                + f"。今{prof.name() or 'the user'}は{place}にいて、{travel_str}{depart} までに出ないと間に合わない"
                + route_block
                + "。出発地は『家』ではなく上記の現在地。そこから出発するよう案内する。"
                + " 場所が自宅なら自宅へ戻る案内、explicit なら最寄駅まで歩いて電車で。"
                + " ルート情報は Google Maps の実データ — 駅名と線名はそのまま使い、"
                + " 自分の記憶で言い換えない。"
            )
        # Substitute {name} placeholder in ctx with profile.identity.preferredName
        # so the model sees the actual name instead of literal "{name}".
        ctx = ctx.replace("{name}", prof.name() or "you")

        sid = place_lateness_call(ctx)
        slack(f"🏃 遅刻防止コール: {d['reason']} → {e['summary']} (call {sid})")
        print(f"[late] placed lateness call sid={sid}")

        # RELENTLESS within-tick loop. HARD RULE: if the user did not pick up
        # OR did not start moving after the first call, KEEP CALLING in the
        # SAME heartbeat — do not wait for the next 5-min cron. It is
        # Anicca's job to actually move the user. Stop only when the user
        # provably moved (>= moveDetectionMeters from the origin) or after
        # MAX attempts. Skipped entirely for "guide" (already-moving) and
        # routine-only nudges.
        MAX = int(os.environ.get("LATE_RELENTLESS_MAX", str(RELENTLESS_MAX_DEFAULT)))
        GAP_NOPICKUP_SEC = int(os.environ.get("LATE_RELENTLESS_GAP_NOPICKUP", "60"))
        GAP_PICKUP_NOMOVE_SEC = int(os.environ.get("LATE_RELENTLESS_GAP_PICKUP_NOMOVE", "120"))
        MOVE_THRESHOLD_M = int(prof.get("alarm.moveDetectionMeters", 300))
        origin_loc = loc_now  # captured before any call placed

        for attempt in range(2, MAX + 1):
            outcome = _wait_for_available_call(sid)
            if outcome is None:
                print("[late] RELENTLESS skipped — phone channel unavailable")
                break
            # Poll Twilio for outcome of the previous attempt (max 2 min).
            status, dur = outcome
            pickup_seems_real = dur >= 25  # call held > 25s ≈ heard Anicca
            print(f"[late] RELENTLESS attempt #{attempt-1}/{MAX}: status={status} dur={dur}s pickup_real={pickup_seems_real}")

            # Give the location bridge a moment to push a fresh fix.
            time.sleep(20)
            fresh = get_location()
            moved = _user_moved(origin_loc, fresh, MOVE_THRESHOLD_M)
            if moved:
                slack(f"✅ {prof.name() or 'user'} 動き出した — RELENTLESS exit (attempt {attempt-1})")
                print(f"[late] RELENTLESS exit: user moved >{MOVE_THRESHOLD_M}m")
                break

            # Not moving → call again. Pick gap based on whether prev call seemed answered.
            gap = GAP_PICKUP_NOMOVE_SEC if pickup_seems_real else GAP_NOPICKUP_SEC
            print(f"[late] RELENTLESS no-move — re-dial in {gap}s")
            time.sleep(gap)

            # Augment ctx with attempt counter so the model knows this is escalation.
            re_ctx = ctx + f" これで{attempt}回目の連絡。 前回出てくれなかった/動いてくれてない。 もっと強く promote する。"
            sid = place_lateness_call(re_ctx)
            slack(f"🔁 RELENTLESS #{attempt}/{MAX}: {e['summary']} (call {sid})")
            print(f"[late] RELENTLESS placed attempt {attempt} sid={sid}")
        else:
            slack(f"⚠️ RELENTLESS exhausted {MAX} attempts on {e['summary']} — escalate via mail")
            print(f"[late] RELENTLESS exhausted {MAX} attempts")

        # If departBy already passed and he's still home, he WILL be late.
        # Don't hard-code the recipient: emit a RENRAKU_NEEDED block with the
        # event's own context and let the agent (Anicca) find who to notify and
        # how, per this specific schedule. Emit once per event/day (dedup).
        mins_to_depart = (depart_dt - now).total_seconds() / 60
        if mins_to_depart <= 0:
            sent_path = LOOP_STATE_DIR / "renraku_sent.json"
            sent_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                sent = json.loads(sent_path.read_text())
            except Exception:
                sent = []
            key = f"{e['summary']}|{e['departByIso']}"
            if key not in sent:
                minutes_late = max(5, int(-mins_to_depart))
                ctx = {
                    "summary": e["summary"], "location": e.get("location"),
                    "startIso": e["startIso"], "minutesLate": minutes_late,
                    "attendees": e.get("attendees") or [],
                    "organizer": e.get("organizer"),
                    "description": e.get("description") or "",
                    "htmlLink": e.get("htmlLink"),
                }
                print("RENRAKU_NEEDED " + json.dumps(ctx, ensure_ascii=False))
                sent.append(key)
                sent_path.write_text(json.dumps(sent, ensure_ascii=False))


if __name__ == "__main__":
    main()
