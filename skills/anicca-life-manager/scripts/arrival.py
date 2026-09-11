#!/usr/bin/env python3
"""anicca-arrival-mail — detect user arrival + notify stakeholders.

Reads:
  - lateness_check.get_location()           Telegram live fix
  - gcal next 4h via gog
  - lateness_check.arrival_radius_for(dest) event-type radius
  - state/notified.json                     per-event dedup

For each upcoming event with attendees/organizer, when current location
is within arrival_radius, sends a polite "I'm here" Gmail and records.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
LIFE_MANAGER_HOME = Path(os.environ.get(
    "LIFE_MANAGER_HOME", str(Path.home() / ".local" / "state" / "life-manager"),
))
ANICCA_HOME = Path(os.environ.get("ANICCA_HOME", str(LIFE_MANAGER_HOME)))
sys.path.insert(0, str(REPO_ROOT / "skills" / "_shared"))
sys.path.insert(0, str(SCRIPT_DIR))
import anicca_profile as prof  # noqa: E402

# Reuse the same resolver + radius logic as the lateness pipeline.
from lateness_check import (    # noqa: E402
    get_location, geocode_place, haversine_m,
    resolve_event_destination, arrival_radius_for, _in_quiet_hours,
)

JST = timezone(timedelta(hours=9))
ENV_PATH = Path(os.environ.get("LIFE_MANAGER_ENV_FILE", str(ANICCA_HOME / ".env")))
ENV = ENV_PATH.read_text() if ENV_PATH.is_file() else ""
STATE_FILE = ANICCA_HOME / "state" / "arrival" / "notified.json"
HORIZON_HOURS = int(os.environ.get("ARRIVAL_HORIZON_HOURS", "4"))
STATE_RETENTION_DAYS = int(os.environ.get("ARRIVAL_STATE_RETENTION_DAYS", "30"))


def env(name, default=""):
    m = re.search(rf"^{name}=(.*)$", ENV, re.M)
    return (m.group(1).strip().strip('"').strip("'") if m else default)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def prune_state(state, now):
    cutoff = (now - timedelta(days=STATE_RETENTION_DAYS)).date()
    pruned = {}
    for key, value in state.items():
        try:
            _, day = key.rsplit("|", 1)
            if datetime.strptime(day, "%Y-%m-%d").date() < cutoff:
                continue
        except Exception:
            pass
        pruned[key] = value
    return pruned


def save_state(s):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(s, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent, delete=False, encoding="utf-8") as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(STATE_FILE)


def list_events(hours):
    acct = env("GOG_ACCOUNT") or prof.google_account()
    to = (datetime.now(JST) + timedelta(hours=hours)).strftime("%Y-%m-%d")
    out = subprocess.run(
        [os.environ.get("LIFE_MANAGER_GOG", "gog"), "calendar", "events", "list", "-j",
         "--account", acct, "--from", "today", "--to", to,
         "--all-pages", "--max", "100"],
        capture_output=True, text=True,
        env={**os.environ, "GOG_KEYRING_PASSWORD": env("GOG_KEYRING_PASSWORD"),
             "GOG_ACCOUNT": acct},
        timeout=45,
    )
    if out.returncode != 0:
        return []
    try:
        d = json.loads(out.stdout)
        items = d if isinstance(d, list) else d.get("events", d.get("items", []))
    except Exception:
        return []
    rows = []
    now = datetime.now(JST)
    for ev in items:
        s = (ev.get("start") or {}).get("dateTime")
        if not s:
            continue
        start = datetime.fromisoformat(s).astimezone(JST)
        if start < now - timedelta(hours=1) or start > now + timedelta(hours=hours):
            continue
        rows.append({
            "id": ev["id"],
            "summary": ev.get("summary") or "",
            "location": ev.get("location") or "",
            "start": start,
            "attendees": [a.get("email") for a in (ev.get("attendees") or []) if a.get("email")],
            "organizer": (ev.get("organizer") or {}).get("email"),
        })
    rows.sort(key=lambda r: r["start"])
    return rows


def send_mail(to_list, subject, body):
    acct = env("GOG_ACCOUNT") or prof.google_account()
    if not (acct and to_list):
        return False
    cmd = [
        os.environ.get("LIFE_MANAGER_GOG", "gog"), "gmail", "send",
        "--account", acct,
        "--to", ",".join(to_list),
        "--subject", subject,
        "--body-file", "-",
    ]
    out = subprocess.run(
        cmd, input=body, capture_output=True, text=True,
        env={**os.environ,
             "GOG_ACCOUNT": acct,
             "GOG_KEYRING_PASSWORD": env("GOG_KEYRING_PASSWORD")},
        timeout=45,
    )
    return out.returncode == 0


def main():
    now = datetime.now(JST)
    if _in_quiet_hours(now):
        print(json.dumps({"action": "quiet-hours"}))
        return

    loc = get_location()
    if not loc:
        print(json.dumps({"action": "no-location"}))
        return

    state = load_state()
    state = prune_state(state, now)
    today = now.strftime("%Y-%m-%d")
    events = list_events(HORIZON_HOURS)
    sent = []

    for ev in events:
        if not (ev["organizer"] or ev["attendees"]):
            continue  # no one to notify
        addr, kind = resolve_event_destination(ev)
        if kind == "home_routine" or kind == "unknown":
            continue
        key = f"{ev['id']}|{today}"
        if state.get(key):
            continue
        dest_geo = geocode_place(addr)
        if not dest_geo:
            continue
        dist = haversine_m(dest_geo[0], dest_geo[1], loc["lat"], loc["lon"])
        radius = arrival_radius_for(addr)
        if dist > radius:
            continue

        # Compose mail
        name = prof.name() or "I"
        time_str = now.strftime("%H:%M JST")
        summary = ev["summary"][:60]
        recipients = [r for r in [ev["organizer"], *ev["attendees"]]
                      if r and "@" in r and r.lower() != (env("GOG_ACCOUNT") or "").lower()]
        if not recipients:
            continue
        body = (
            f"Hi,\n\n"
            f"This is Anicca, on behalf of {name}.\n\n"
            f"Confirming arrival at the event『{summary}』 — landed at "
            f"{time_str} (~{int(dist)}m from the venue centre).\n\n"
            f"{name} will reach out personally during the meeting if anything "
            f"comes up.\n\n"
            f"— Anicca\n"
            f"   (autonomous AI agent that tracks {name}'s schedule + sends "
            f"these confirmations automatically)\n"
        )
        subj = f"Arrived: {summary} ({time_str})"
        if send_mail(recipients, subj, body):
            state[key] = {"ts": int(now.timestamp()), "to": recipients,
                          "summary": summary, "dist_m": int(dist)}
            sent.append({"event": summary, "recipients": recipients, "dist_m": int(dist)})

    save_state(state)
    print(json.dumps({"checked": len(events), "sent": sent}, ensure_ascii=False))


if __name__ == "__main__":
    main()
