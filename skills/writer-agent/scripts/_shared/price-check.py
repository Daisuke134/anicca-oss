#!/usr/bin/env python3
"""price-check.py — measure the niche before every paid publish (spec #37).

Bake the MEASUREMENT, not the number. Outputs machine-readable JSON with:
  - popular paid articles in the niche (price / likes / author)
  - author economics (hasCircle / followerCount / noteCount) for each paid author
  - our own creator stats
  - a SUGGESTION (buy_once vs membership + price) derived from measured rules:
      * membership only after our noteCount exceeds the measured minimum noteCount
        among membership holders in the niche (never assume a fixed number)
      * suggested price = median of same-niche paid prices (like_count >= like_floor),
        clamped to [300, 3000] for explainer-type articles (we sell explanation,
        not an income promise -- do not copy promise-priced articles)
The calling agent reads the JSON and makes the final call; this script decides nothing
by itself and never touches the browser.

Usage:
  price-check.py --query "Claude Code" [--my-urlname your-note-name] [--size 20] [--like-floor 100]
stdout: one JSON object. Diagnostics on stderr. Non-zero exit on API failure.
"""
import argparse
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0"}


def get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def search_notes(query: str, size: int):
    q = urllib.parse.quote(query)
    d = get(f"https://note.com/api/v3/searches?context=note&q={q}&size={size}&sort=popular")
    notes = d.get("data", {}).get("notes", {})
    return notes.get("contents", notes if isinstance(notes, list) else [])


def creator(urlname: str):
    d = get(f"https://note.com/api/v2/creators/{urllib.parse.quote(urlname)}")["data"]
    return {
        "urlname": urlname,
        "noteCount": d.get("noteCount"),
        "followerCount": d.get("followerCount"),
        "hasCircle": d.get("hasCircle"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--my-urlname", default=os.environ.get("NOTE_URLNAME", ""))
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--like-floor", type=int, default=100)
    a = ap.parse_args()
    if not a.my_urlname:
        ap.error("NOTE_URLNAME or --my-urlname is required")

    items = search_notes(a.query, a.size)
    paid = []
    for n in items:
        price = n.get("price") or 0
        if price > 0:
            paid.append({
                "title": (n.get("name") or "")[:60],
                "price": price,
                "like_count": n.get("like_count") or 0,
                "urlname": (n.get("user") or {}).get("urlname"),
            })
    authors = {}
    for p in paid:
        u = p["urlname"]
        if u and u not in authors:
            try:
                authors[u] = creator(u)
            except Exception as e:  # keep measuring; report the hole honestly
                print(f"WARN creator fetch failed for {u}: {e}", file=sys.stderr)
                authors[u] = {"urlname": u, "error": str(e)}
    me = creator(a.my_urlname)

    member_counts = [
        c["noteCount"] for c in authors.values()
        if c.get("hasCircle") and isinstance(c.get("noteCount"), int)
    ]
    membership_floor = min(member_counts) if member_counts else None
    eligible = membership_floor is not None and (me.get("noteCount") or 0) >= membership_floor

    liked = [p["price"] for p in paid if p["like_count"] >= a.like_floor]
    price_pool = liked or [p["price"] for p in paid]
    suggested = None
    if price_pool:
        suggested = int(min(max(statistics.median(price_pool), 300), 3000))

    out = {
        "query": a.query,
        "paid_articles_measured": paid,
        "authors": list(authors.values()),
        "me": me,
        "membership_noteCount_floor_measured": membership_floor,
        "suggestion": {
            "format": "membership" if eligible else "buy_once",
            "price": suggested,
            "basis": f"median of {len(price_pool)} paid prices"
                     f" (like_floor={a.like_floor}, clamp 300-3000, explainer pricing)",
        },
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
