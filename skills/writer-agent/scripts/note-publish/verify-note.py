#!/usr/bin/env python3
"""Deterministic VERIFY evidence for a published note article (general, takes the note key).
Gathers truth a script CAN measure (API price/is_limited/can_read/eyecatch) + a NO-cookie visitor
screenshot for the AGENT to LOOK at. Never trusts the owner view. Usage: verify-note.py <noteKey> [expectGated]"""
import json, os, sys, time, urllib.request
from cloakbrowser import launch_context

KEY = sys.argv[1] if len(sys.argv) > 1 else "na3a631e63d1a"
NOTE_URLNAME = os.environ.get("NOTE_URLNAME", "").strip()
if not NOTE_URLNAME:
    raise SystemExit("NOTE_URLNAME is required")
EXPECT_GATED = (sys.argv[2].lower() in ("1", "true", "gated")) if len(sys.argv) > 2 else True
WORK = os.path.join(os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")), "note-work"); os.makedirs(WORK, exist_ok=True)
SHOT = f"{WORK}/verify-{KEY}.png"

# 1) API truth
req = urllib.request.Request(f"https://note.com/api/v3/notes/{KEY}", headers={"User-Agent": "Mozilla/5.0"})
d = json.load(urllib.request.urlopen(req, timeout=20)).get("data", {})
price = d.get("price"); is_limited = d.get("is_limited"); can_read = d.get("can_read")
eyecatch = bool(d.get("eyecatch")); name = (d.get("name") or "")[:50]

# 2) visitor screenshot (NO cookies)
shot_ok = False
try:
    ctx = launch_context(headless=True, humanize=False)
    pg = ctx.new_page(); pg.set_viewport_size({"width": 1100, "height": 1200})
    pg.goto(f"https://note.com/{NOTE_URLNAME}/n/{KEY}", wait_until="domcontentloaded", timeout=45000); time.sleep(6)
    pg.evaluate("window.scrollTo(0,0)"); time.sleep(1); pg.screenshot(path=SHOT); shot_ok = True
    ctx.close()
except Exception as e:
    print("screenshot err:", str(e)[:60])

# 3) deterministic PASS = eyecatch set AND (if gated → can_read false)
det_pass = eyecatch and (can_read is False if EXPECT_GATED else True)
print(json.dumps({
    "key": KEY, "name": name, "price": price, "is_limited": is_limited,
    "can_read": can_read, "eyecatch": eyecatch, "expect_gated": EXPECT_GATED,
    "screenshot": SHOT if shot_ok else None,
    "DETERMINISTIC": "PASS" if det_pass else "FAIL",
    "AGENT_MUST": "Read the screenshot + judge: layout, images not crushed, 目次=big titles, headings intact, lang pure",
}, ensure_ascii=False, indent=1))
sys.exit(0 if det_pass else 1)
