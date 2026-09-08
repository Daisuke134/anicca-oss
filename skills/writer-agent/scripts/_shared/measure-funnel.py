#!/usr/bin/env python3
"""measure-funnel.py — spec #21/#24 step 1 (measure). Reads which articles are actually
LIVE and measures real engagement/sales per platform, once. NEVER estimate or fabricate a
number: if a metric truly cannot be measured today, emit null + a reason string. Appends
one JSON line per (date, article, platform) to state/funnel.jsonl.

WHICH articles are "live": production calls pass --ledger, --run-id, and --topic-id.
The script requires the canonical exact8 set of published:true, reality PASS rows and derives
all platform identities from those immutable receipts. The older --live-manifest option remains
only for backward-compatible manual diagnostics; scheduled learning never depends on a hand list.

Per-platform measurement (MEASURED 2026-07-16 against real live articles -- this is what
actually works today, not a guess):
  - note:      GET https://note.com/api/v3/notes/{key} (public, no auth) -> like_count,
               price, is_limited, status. Revenue/sales: NOT reachable -- 4 guessed
               creator-dashboard/sales endpoints all 404'd or returned nothing
               (/api/v1/text_notes/{key}/sales, /api/v2/notes/{key}/purchases,
               /api/v1/creators/dashboard/sales, /api/v2/creators/{id}/stats). Reported as
               null + reason, not guessed -- do not trust like_count as a revenue proxy.
  - X:         no free view/like API -- DOM read via the daily-driver browser (CDP :9222),
               parsing the aria-label text ("N like, M views") on the status page.
               Requires CDP reachable + logged in; null + reason otherwise.
  - Substack:  GET /api/v1/drafts/{id} (works even once published) gives is_published /
               post_date / title, but reaction_count / comment_count / restacks all come
               back None through this endpoint -- no working public stats endpoint found;
               null + reason for engagement. is_published/post_date ARE measurable.
  - zenn:      GET https://zenn.dev/api/articles?username=X returns liked_count per
               article, BUT the username query param is MEASURED to be ignored (a bogus
               username returns the identical site-wide latest-48 result set) -- filter
               client-side on each article's own user.username instead of trusting the
               query. If no article in the (site-wide) result matches, null + reason
               (most likely: nothing published live on zenn yet, still draft-only).

CLI:
    measure-funnel.py [--live-manifest <path>] [--out <path>]
stdout: one JSON line summary {"date": "...", "measured": N, "results": [...]}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_MANIFEST = os.path.expanduser(
    os.path.join(os.environ.get("ARTICLE_STATE_DIR", os.environ.get("WRITER_STATE_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "state"))), "live-articles.json")
)
DEFAULT_OUT = os.path.expanduser(
    os.path.join(os.environ.get("ARTICLE_STATE_DIR", os.environ.get("WRITER_STATE_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "state"))), "funnel.jsonl")
)
UA = "Mozilla/5.0"
EXACT8 = (
    "note/ja",
    "zenn-article/ja",
    "devto/en",
    "substack/ja",
    "substack/en",
    "x-article/ja",
    "x-article/en",
    "x-post/ja",
)


def http_get_json(url: str, headers: dict | None = None) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None


def measure_note(entry: dict) -> dict:
    key = entry["id"]
    data = http_get_json(f"https://note.com/api/v3/notes/{key}")
    if not data or "data" not in data:
        return {"error": f"note.com/api/v3/notes/{key} unreachable or malformed response"}
    d = data["data"]
    return {
        "like_count": d.get("like_count"),
        "price": d.get("price"),
        "is_limited": d.get("is_limited"),
        "status": d.get("status"),
        "revenue": None,
        "revenue_reason": (
            "note creator-dashboard/sales API not found (4 guessed endpoints all "
            "404'd or returned nothing 2026-07-16); would need reverse-engineering "
            "the logged-in dashboard's real network calls to measure this"
        ),
    }


def measure_x(entry: dict) -> dict:
    handle = entry.get("handle")
    status_id = entry["id"]
    if not handle:
        return {"error": "no 'handle' in manifest entry -- X status URL needs it"}
    url = entry.get("live_url") or f"https://x.com/{handle}/status/{status_id}"
    vc = os.environ.get("WRITER_BROWSER_PYTHON", sys.executable)
    if not os.path.exists(vc):
        return {"error": f"Writer browser Python not found at {vc}"}
    script = f"""
import sys, time
from playwright.sync_api import sync_playwright
p = sync_playwright().start()
try:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
except Exception as e:
    print("CDP_UNREACHABLE:" + str(e)); sys.exit(1)
ctx = b.contexts[0]; pg = ctx.new_page()
try:
    pg.goto({url!r}, wait_until="domcontentloaded", timeout=40000)
    time.sleep(5)
    aria = pg.evaluate('''()=>{{
        const els = [...document.querySelectorAll('[aria-label]')];
        const m = els.find(e => /views/i.test(e.getAttribute('aria-label')||''));
        return m ? m.getAttribute('aria-label') : '';
    }}''')
    print("ARIA:" + aria)
finally:
    pg.close()
"""
    import subprocess
    proc = subprocess.run([vc, "-c", script], capture_output=True, text=True, timeout=60)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("ARIA:")), None)
    if proc.returncode != 0 or not line:
        reason = proc.stdout.strip() or proc.stderr.strip()[-300:] or "no aria-label matched"
        return {"error": f"X DOM read failed: {reason}"}
    aria_text = line[len("ARIA:"):]
    likes = re.search(r"([\d,]+)\s*[Ll]ikes?", aria_text)
    views = re.search(r"([\d,]+)\s*views", aria_text)
    if not views:
        return {"error": f"X DOM read returned no parseable views count: {aria_text!r}"}
    return {
        "likes": int(likes.group(1).replace(",", "")) if likes else None,
        "views": int(views.group(1).replace(",", "")),
        "raw_aria_label": aria_text,
    }


def measure_substack(entry: dict) -> dict:
    post_id = entry["id"]
    cookie = os.environ.get("SUBSTACK_SESSION_COOKIE", "")
    if not cookie:
        return {"error": "SUBSTACK_SESSION_COOKIE missing (configure LIFE_MANAGER_ENV_FILE first)"}
    pub = os.environ.get("SUBSTACK_PUBLICATION", "aniccabuddha.substack.com")
    data = http_get_json(f"https://{pub}/api/v1/drafts/{post_id}", headers={"Cookie": cookie})
    if not data:
        return {"error": f"{pub}/api/v1/drafts/{post_id} unreachable or malformed response"}
    return {
        "is_published": data.get("is_published"),
        "post_date": data.get("post_date"),
        "title": data.get("title"),
        "reaction_count": None,
        "reaction_count_reason": (
            "reaction_count/comment_count/restacks all come back None through "
            "GET /api/v1/drafts/{id} (measured 2026-07-16); no working public stats "
            "endpoint found for Substack"
        ),
    }


def measure_zenn(entry: dict) -> dict:
    username = entry["id"]  # the zenn handle, e.g. "daisuke134"
    slug = entry.get("slug")
    data = http_get_json(f"https://zenn.dev/api/articles?username={username}")
    if not data or "articles" not in data:
        return {"error": "zenn.dev/api/articles unreachable or malformed response"}
    # the username query param is MEASURED to be ignored by this endpoint (a bogus
    # username returns the identical site-wide latest-48 feed) -- filter client-side.
    match = next(
        (
            a
            for a in data["articles"]
            if a.get("user", {}).get("username") == username and (not slug or a.get("slug") == slug)
        ),
        None,
    )
    if not match:
        return {
            "error": (
                f"no article by username={username!r} found in zenn's returned feed "
                "(that endpoint's username filter is measured to not actually filter -- "
                "most likely nothing is live on zenn yet, still draft-only)"
            )
        }
    return {
        "liked_count": match.get("liked_count"),
        "comments_count": match.get("comments_count"),
        "bookmarked_count": match.get("bookmarked_count"),
        "published_at": match.get("published_at"),
    }


def measure_devto(entry: dict) -> dict:
    article_id = str(entry["id"])
    data = http_get_json(f"https://dev.to/api/articles/{article_id}")
    if not data or str(data.get("id", "")) != article_id:
        return {
            "error": (
                f"Dev.to article id={article_id!r} is unreachable or "
                "does not match the owned public receipt"
            )
        }
    return {
        "public_reactions_count": data.get("public_reactions_count"),
        "comments_count": data.get("comments_count"),
        "page_views_count": data.get("page_views_count"),
        "published_at": data.get("published_at"),
    }


MEASURERS = {
    "note": measure_note,
    "x": measure_x,
    "x-article": measure_x,
    "x-post": measure_x,
    "substack": measure_substack,
    "zenn": measure_zenn,
    "zenn-article": measure_zenn,
    "devto": measure_devto,
}


def derive_manifest(
    ledger_rows: list[dict],
    *,
    run_id: str,
    topic_id: str,
) -> list[dict]:
    current = [
        row
        for row in ledger_rows
        if row.get("run_id") == run_id
        and row.get("topic_id") == topic_id
        and row.get("published") is True
        and row.get("reality_gate") == "PASS"
    ]
    by_pair: dict[str, dict] = {}
    for row in current:
        pair = f"{row.get('platform')}/{row.get('lang')}"
        if pair not in EXACT8 or pair in by_pair:
            raise ValueError("exact8 ledger is missing, duplicated, or ambiguous")
        by_pair[pair] = row
    if set(by_pair) != set(EXACT8):
        raise ValueError("exact8 ledger is missing, duplicated, or ambiguous")

    result = []
    for pair in EXACT8:
        row = by_pair[pair]
        live_url = str(row["live_url"])
        parsed = urlparse(live_url)
        parts = [part for part in parsed.path.split("/") if part]
        public_id = str(row["public_id"])
        platform = pair.rsplit("/", 1)[0]
        entry = {
            "run_id": run_id,
            "topic_id": topic_id,
            "pair": pair,
            "platform": platform,
            "id": public_id,
            "title": str(row.get("title") or topic_id),
            "live_url": live_url,
        }
        if pair == "zenn-article/ja":
            if len(parts) < 3 or parts[1] != "articles":
                raise ValueError("exact8 Zenn URL is invalid")
            entry["id"] = parts[0]
            entry["slug"] = public_id
        elif pair.startswith("x-"):
            if not parts:
                raise ValueError("exact8 X URL is invalid")
            entry["handle"] = parts[0]
        result.append(entry)
    return result


def measure_entries(
    entries: list[dict],
    out_path: Path,
    *,
    measured_at: str | None = None,
    sleep_seconds: float = 0.5,
) -> list[dict]:
    timestamp = measured_at or datetime.now(timezone.utc).isoformat()
    today = timestamp[:10]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with out_path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            platform = str(entry.get("platform", ""))
            function = MEASURERS.get(platform)
            measured = (
                function(entry)
                if function is not None
                else {"error": f"unknown platform: {platform!r}"}
            )
            row = {
                "date": today,
                "run_id": entry.get("run_id"),
                "topic_id": entry.get("topic_id"),
                "pair": entry.get("pair"),
                "title": entry.get("title"),
                "platform": platform,
                "id": entry.get("id"),
                "live_url": entry.get("live_url"),
                "measured": measured,
                "measured_at": timestamp,
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            results.append(row)
            if sleep_seconds:
                time.sleep(sleep_seconds)
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--live-manifest", default=DEFAULT_MANIFEST)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--ledger")
    p.add_argument("--run-id")
    p.add_argument("--topic-id")
    args = p.parse_args(argv)

    if args.ledger or args.run_id or args.topic_id:
        if not all((args.ledger, args.run_id, args.topic_id)):
            raise SystemExit(
                "FATAL: --ledger, --run-id, and --topic-id are required together"
            )
        rows = []
        for line in Path(args.ledger).read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        entries = derive_manifest(
            rows,
            run_id=args.run_id,
            topic_id=args.topic_id,
        )
    else:
        manifest_path = Path(args.live_manifest)
        if not manifest_path.exists():
            raise SystemExit(f"FATAL: live manifest not found: {manifest_path}")
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))

    out_path = Path(args.out)
    measured_at = datetime.now(timezone.utc).isoformat()
    results = measure_entries(
        entries,
        out_path,
        measured_at=measured_at,
    )
    print(
        json.dumps(
            {
                "date": measured_at[:10],
                "measured": len(results),
                "results": results,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
