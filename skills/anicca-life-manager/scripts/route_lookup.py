#!/usr/bin/env python3
"""
route_lookup — scrape Google Maps Web for an accurate transit route.

Why this script exists:
  Google Directions API has dropped transit data in Japan (returns
  ZERO_RESULTS for every JP transit query as of ~2022). The web version of
  Google Maps still computes Tokyo (and global) transit correctly though —
  it just isn't reachable via the API. We use agent-browser to render
  https://www.google.com/maps/dir/<origin>/<dest>/data=!4m2!4m1!3e3 in real
  Chrome and parse the resulting transit panel.

Latency: ~5-8s (Chrome cold load + DOM render + snapshot). Acceptable for
PRE-COMPUTE on the lateness path (lateness_check runs once per heartbeat
and embeds the route in the ctx passed to Anicca's call) — but NOT
acceptable as a mid-call tool. Anicca herself does not call this script
during a live conversation.

Usage:
  python route_lookup.py --origin "35.679944,139.719578" \
                         --destination "中野セントラルパークサウス, 中野区, 東京都"
  → prints JSON: {ok, duration_min, summary, steps[], fare_yen, raw_text}

"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse


AGENT_BROWSER = os.environ.get("LIFE_MANAGER_AGENT_BROWSER", "agent-browser")


def _run_ab(args: list[str], timeout: int = 30) -> str:
    """Invoke agent-browser CLI and return its stdout."""
    out = subprocess.run(
        [AGENT_BROWSER, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(f"agent-browser {args[0]} failed: {out.stderr[:300]}")
    return out.stdout


def fetch_transit_route(origin: str, destination: str) -> dict:
    """Render Google Maps Web transit route + parse the first option."""
    dest_enc = urllib.parse.quote(destination)
    url = (
        f"https://www.google.com/maps/dir/{origin}/{dest_enc}"
        f"/data=!4m2!4m1!3e3"
    )
    _run_ab(["open", url], timeout=30)
    # Maps fetches transit data after JS render; give it a beat.
    time.sleep(7)
    snapshot = _run_ab(["snapshot"], timeout=20)

    # The first transit suggestion shows up as a single anchor / button whose
    # accessible name concatenates: "公共交通機関 N 分 HH:MM - HH:MM 電車 LINE_NAME ...
    # FROM_STATION から HH:MM ... PRICE 円 徒歩 N 分 ...". We isolate that
    # row and tear apart the metadata. Multiple options exist on the page;
    # take the FIRST (recommended) one.
    route_lines = [
        line.strip()
        for line in snapshot.splitlines()
        if "公共交通機関" in line and ("分 " in line or "時間" in line)
    ]
    if not route_lines:
        return {
            "ok": False,
            "reason": "no transit option in Google Maps snapshot",
            "origin": origin,
            "destination": destination,
        }

    first = route_lines[0]
    # Strip the leading marker like `link "..."` / `button "..."`.
    m = re.match(r"\s*-?\s*(?:link|button)\s+\"(.+?)\"\s*\[ref=", first)
    text = m.group(1) if m else first

    duration_m = re.search(r"公共交通機関\s+(?:(\d+)\s*時間\s*)?(\d+)\s*分", text)
    duration_min = None
    if duration_m:
        hours = int(duration_m.group(1)) if duration_m.group(1) else 0
        mins = int(duration_m.group(2))
        duration_min = hours * 60 + mins

    fare_m = re.search(r"(\d{2,4})\s*円", text)
    fare_yen = int(fare_m.group(1)) if fare_m else None

    walk_m = re.search(r"徒歩\s+(\d+)\s*分", text)
    walk_min = int(walk_m.group(1)) if walk_m else None

    # Lines like "電車 中央・総武線各停 、その後 電車 中央線 、その後 徒歩"
    legs: list[str] = []
    for chunk in re.findall(r"電車\s+([^、,]+?)(?=\s*、その後|\s*$|\s+\d|\s+徒歩|\s+ |・\d)", text):
        legs.append(chunk.strip())

    # Departure station: "FROM_STATION から HH:MM"
    dep_station = None
    dep_m = re.search(r"([^\s]+駅)\s*から\s*(\d{1,2}:\d{2})", text)
    if dep_m:
        dep_station = dep_m.group(1)
        dep_time = dep_m.group(2)
    else:
        dep_time = None

    # Times: "20:36 - 20:59"
    span = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text)

    summary = []
    if dep_station:
        summary.append(f"{dep_station}")
        if dep_time:
            summary[-1] += f" ({dep_time}発)"
    for leg in legs:
        summary.append(leg)
    if walk_min:
        summary.append(f"徒歩 {walk_min}分")

    return {
        "ok": True,
        "duration_min": duration_min,
        "fare_yen": fare_yen,
        "departure_station": dep_station,
        "departure_time": dep_time,
        "span": [span.group(1), span.group(2)] if span else None,
        "lines": legs,
        "walk_min_total": walk_min,
        "summary": " → ".join(summary) if summary else text[:200],
        "raw_text": text,
        "origin": origin,
        "destination": destination,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True, help="lat,lon")
    ap.add_argument("--destination", required=True, help="address or landmark")
    args = ap.parse_args()
    try:
        result = fetch_transit_route(args.origin, args.destination)
    except Exception as e:
        result = {"ok": False, "error": str(e), "origin": args.origin, "destination": args.destination}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
