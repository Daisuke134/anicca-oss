#!/usr/bin/env python3
"""realtime_guide — 24/7 daemon that guides a user through an itinerary
by Telegram (text + voice note), with Twilio relentless call as the fallback.

Loop (every 10 sec):
  1. Read latest live location from STATE_DIR/<user_id>.json
  2. Read active itinerary from STATE_DIR/itinerary_<user_id>.json
     (set by lateness_check.py before each event, OR by Dais via /guide)
  3. Determine which leg the user is currently on/transitioning between
  4. Push step transitions to Telegram (text)
  5. If user hasn't moved by leave_at + 5min: start Twilio relentless call

State separation:
  STATE_DIR/<user_id>.json                 - live location (existing)
  STATE_DIR/itinerary_<user_id>.json       - active itinerary (NEW)
  STATE_DIR/guide_state_<user_id>.json     - current step + last pings (NEW)

Works in:
  - Life Manager local (= user-owned host, portable state root)
  - Anicca cloud (= Daytona sandbox per user, same script + per-sandbox state)

BP cite:
  - core.telegram.org/bots/api#sendmessage (+ sendVoice + sendVideoNote)
  - twilio.com/docs/voice canonical
  - en.wikipedia.org/wiki/Haversine_formula
  - Dais 2026-06-07 verbatim ("15 minutes before relentless call", "10 min early")
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

LIFE_MANAGER_HOME = Path(os.environ.get(
    "LIFE_MANAGER_STATE_ROOT",
    os.environ.get("LIFE_MANAGER_HOME", str(Path.home() / ".local" / "state" / "life-manager")),
))
STATE_DIR = LIFE_MANAGER_HOME / "state" / "location"   # GPS only (<uid>.json) — never write guide files here
STATE_DIR.mkdir(parents=True, exist_ok=True)
# itinerary_*.json + guide_state_*.json live in a SEPARATE dir so they never
# poison get_location()'s glob over state/location/ (regression 2026-06-08).
GUIDE_DIR = LIFE_MANAGER_HOME / "state" / "guide"
GUIDE_DIR.mkdir(parents=True, exist_ok=True)
ENV_PATH = Path(os.environ.get("LIFE_MANAGER_ENV_FILE", LIFE_MANAGER_HOME / ".env"))

# Load env
if ENV_PATH.exists():
    for ln in ENV_PATH.read_text().splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_NUMBER")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s realtime-guide %(levelname)s %(message)s",
)
log = logging.getLogger("realtime-guide")

# Per Anicca standard
BUFFER_MIN = 10          # arrive 10 min early
RELENTLESS_LEAD_MIN = 15  # start calling 15 min before leave_at
TICK_SEC = 10
STATION_REACH_M = 80     # within 80m of station = "arrived"
STATION_APPROACH_M = 250  # within 250m = "approaching"
DEST_REACH_M = 40        # within 40m of final dest = "arrived"
TRAIN_SPEED_KMH = 25     # >= 25 km/h = "on train"
WRONG_DIR_PING_M = 30    # only ping if user moved >= 30m in wrong dir


# ── haversine + geo helpers ─────────────────────────────────────
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters between two lat/lon points."""
    R = 6371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2 (0=N, 90=E, ...)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ── Telegram send ──────────────────────────────────────────────
def tg_send_text(chat_id: int | str, text: str) -> None:
    if not TG_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN missing")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
    except urllib.error.HTTPError as e:
        log.error("tg send failed: %s %s", e.code, e.read()[:200])


def twilio_call(to_phone: str, twiml_url: str) -> str | None:
    """Place a Twilio call. Returns SID or None on failure."""
    if not (TWILIO_SID and TWILIO_AUTH and TWILIO_FROM):
        log.warning("Twilio env not set; skipping relentless call")
        return None
    import base64
    creds = base64.b64encode(f"{TWILIO_SID}:{TWILIO_AUTH}".encode()).decode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json"
    body = urllib.parse.urlencode({
        "To": to_phone, "From": TWILIO_FROM, "Url": twiml_url, "Timeout": "30",
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Basic {creds}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
            return d.get("sid")
    except urllib.error.HTTPError as e:
        log.error("twilio failed: %s %s", e.code, e.read()[:200])
        return None


# ── state IO ───────────────────────────────────────────────────
def read_location(user_id: int) -> dict | None:
    f = STATE_DIR / f"{user_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def read_itinerary(user_id: int) -> dict | None:
    f = GUIDE_DIR / f"itinerary_{user_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def read_guide_state(user_id: int) -> dict:
    f = GUIDE_DIR / f"guide_state_{user_id}.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def write_guide_state(user_id: int, st: dict) -> None:
    f = GUIDE_DIR / f"guide_state_{user_id}.json"
    f.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def clear_active(user_id: int) -> None:
    for nm in (f"itinerary_{user_id}.json", f"guide_state_{user_id}.json"):
        f = GUIDE_DIR / nm
        if f.exists():
            f.unlink()


# ── leg description helpers ────────────────────────────────────
def describe_leg(leg: dict, *, idx: int, total: int, when_jst: str = "") -> str:
    mode = leg.get("mode", "?")
    ld = leg.get("duration", 0) // 60
    f = leg.get("from", {}).get("name", "?")
    t = leg.get("to", {}).get("name", "?")
    if mode == "WALK":
        return f"({idx+1}/{total}) {when_jst} 🚶 徒歩 {ld} 分: {f} → {t}"
    rt = leg.get("routeShortName") or leg.get("routeLongName") or ""
    hs = leg.get("headsign") or ""
    extra = f"  ({hs})" if hs else ""
    return f"({idx+1}/{total}) {when_jst} 🚇 {rt}{extra}: {f} → {t}  ({ld} 分)"


# ── core tick ──────────────────────────────────────────────────
def tick(user_id: int, chat_id: int | str, loc: dict, itin_blob: dict, now_ts: int) -> None:
    st = read_guide_state(user_id)
    user_lat, user_lon = loc["lat"], loc["lon"]
    itin = itin_blob["itinerary"]
    legs = itin.get("legs", [])
    if not legs:
        return

    arr = datetime.fromisoformat(itin_blob["arrive_by_iso"])
    travel_sec = itin["duration"]
    leave_at = arr - timedelta(seconds=travel_sec + BUFFER_MIN * 60)
    call_start = leave_at - timedelta(minutes=RELENTLESS_LEAD_MIN)
    now = datetime.now(JST)

    # ── PHASE 1: BEFORE leave_at ───────────────────────────────
    # Brief once at call_start
    if not st.get("briefed") and now >= call_start:
        when_jst = leave_at.strftime("%H:%M JST")
        first = legs[0]
        f_name = first.get("from", {}).get("name", "現在地")
        t_name = first.get("to", {}).get("name", "次")
        tg_send_text(
            chat_id,
            f"⏰ {when_jst} に 出発 です ({RELENTLESS_LEAD_MIN} 分 後)。\n"
            f"全 {len(legs)} step、 所要 {travel_sec // 60} 分、 "
            f"乗換 {itin.get('transfers',0)} 回。\n"
            f"最初: {first.get('mode')} {f_name} → {t_name}。"
        )
        st["briefed"] = True
        write_guide_state(user_id, st)

    # 5 min reminder
    if not st.get("five_min_reminder") and now >= (leave_at - timedelta(minutes=5)) and now < leave_at:
        tg_send_text(chat_id, f"あと 5 分 で 出発 です。 鍵 ・ Suica ・ phone OK?")
        st["five_min_reminder"] = True
        write_guide_state(user_id, st)

    # ── PHASE 2: relentless if not moving by leave_at + 5min ──
    if now >= (leave_at + timedelta(minutes=5)):
        origin_lat = itin_blob.get("origin_lat", user_lat)
        origin_lon = itin_blob.get("origin_lon", user_lon)
        dist_from_origin = haversine_m(user_lat, user_lon, origin_lat, origin_lon)
        last_call = st.get("last_relentless_call_ts", 0)
        if dist_from_origin < 50 and (now_ts - last_call) >= 60:
            # still at home, call them
            twiml = os.environ.get(
                "ANICCA_LEAVE_NOW_TWIML",
                "http://twimlets.com/echo?Twiml=" + urllib.parse.quote(
                    "<Response><Say language='ja-JP' voice='Polly.Mizuki'>"
                    "出発時刻です。 玄関を 出てください。"
                    "</Say></Response>"
                ),
            )
            sid = twilio_call(os.environ.get("DAIS_PHONE", ""), twiml)
            if sid:
                log.info("placed relentless call sid=%s", sid)
            st["last_relentless_call_ts"] = now_ts
            write_guide_state(user_id, st)

    # ── PHASE 3: step transitions during travel ───────────────
    if now < leave_at - timedelta(minutes=2):
        return  # too early to step-track

    cur_idx = st.get("cur_leg_idx", 0)
    if cur_idx >= len(legs):
        return  # done

    cur = legs[cur_idx]
    to = cur.get("to", {})
    to_lat, to_lon = to.get("lat"), to.get("lon")
    if to_lat is None or to_lon is None:
        return

    dist_to_end = haversine_m(user_lat, user_lon, to_lat, to_lon)
    mode = cur.get("mode", "")

    # approach ping (one-shot per leg)
    if dist_to_end <= STATION_APPROACH_M and dist_to_end > STATION_REACH_M:
        if not st.get(f"approach_{cur_idx}"):
            nxt = legs[cur_idx + 1] if cur_idx + 1 < len(legs) else None
            extra = ""
            if nxt:
                nxt_mode = nxt.get("mode")
                if nxt_mode == "WALK":
                    extra = f"\n次: 🚶 徒歩 {nxt.get('duration',0)//60} 分"
                else:
                    nrt = nxt.get("routeShortName") or nxt.get("routeLongName") or ""
                    nhs = nxt.get("headsign") or ""
                    extra = f"\n次: 🚇 {nrt} ({nhs})、 → {nxt.get('to',{}).get('name','?')}"
            tg_send_text(chat_id, f"あと 200m ほど で {to.get('name','次')}。 準備{extra}")
            st[f"approach_{cur_idx}"] = True
            write_guide_state(user_id, st)

    # reach -> advance leg
    reach = DEST_REACH_M if cur_idx == len(legs) - 1 else STATION_REACH_M
    if dist_to_end <= reach:
        # advance
        st["cur_leg_idx"] = cur_idx + 1
        write_guide_state(user_id, st)
        if cur_idx + 1 >= len(legs):
            # final arrival
            late = int((now - arr).total_seconds() / 60)
            if late <= -BUFFER_MIN:
                tg_send_text(chat_id, f"🎯 {to.get('name','目的地')} 着、 {-late} 分 早着 ✓")
            elif late <= 0:
                tg_send_text(chat_id, f"🎯 {to.get('name','目的地')} 着、 時間 通り ✓")
            else:
                tg_send_text(chat_id, f"⚠️ {to.get('name','目的地')} 着 ({late} 分 遅刻)。")
            # mark done — clear active itinerary
            clear_active(user_id)
            return
        else:
            nxt = legs[cur_idx + 1]
            tg_send_text(
                chat_id,
                f"✓ {to.get('name','?')} 着。 {describe_leg(nxt, idx=cur_idx+1, total=len(legs))}"
            )
        return

    # wrong-direction check during WALK legs
    if mode == "WALK" and st.get("last_lat") and st.get("last_lon"):
        moved = haversine_m(st["last_lat"], st["last_lon"], user_lat, user_lon)
        if moved >= WRONG_DIR_PING_M:
            tgt = bearing_deg(st["last_lat"], st["last_lon"], to_lat, to_lon)
            cur_b = bearing_deg(st["last_lat"], st["last_lon"], user_lat, user_lon)
            diff = abs(((tgt - cur_b + 180) % 360) - 180)
            if diff > 90 and (now_ts - st.get("last_wrong_dir_ts", 0)) >= 90:
                tg_send_text(chat_id, f"⚠️ 方向 反対 です。 振り返って {to.get('name','次')} へ。")
                st["last_wrong_dir_ts"] = now_ts
                write_guide_state(user_id, st)

    st["last_lat"] = user_lat
    st["last_lon"] = user_lon
    write_guide_state(user_id, st)


# ── main loop ──────────────────────────────────────────────────
def main_loop():
    log.info("realtime_guide starting (tick %ds)", TICK_SEC)
    while True:
        try:
            # Iterate every user with an itinerary file
            for f in STATE_DIR.glob("itinerary_*.json"):
                try:
                    user_id = int(f.stem.removeprefix("itinerary_"))
                except ValueError:
                    continue
                itin_blob = read_itinerary(user_id)
                if not itin_blob:
                    continue
                loc = read_location(user_id)
                if not loc:
                    continue
                # staleness check: skip if location > 10 min old
                if int(time.time()) - loc.get("received_at", 0) > 600:
                    continue
                # chat_id = user_id (same in DM)
                chat_id = itin_blob.get("chat_id", user_id)
                tick(user_id, chat_id, loc, itin_blob, int(time.time()))
        except Exception:
            log.exception("tick failure")
        time.sleep(TICK_SEC)


if __name__ == "__main__":
    main_loop()
