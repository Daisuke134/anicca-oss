# note-publish troubleshooting

## note-cookies.json extraction returns 0 cookies (extract-note-cookies.py)
Symptom: `extract-note-cookies.py` prints `extracted 0 note.com cookies` and overwrites
`$WRITER_STATE_DIR/note-work/note-cookies.json` with `{}`, even though the daily-driver is logged into
note.com and actively browsing. Root cause (measured 2026-07-16): Chromium batches writes of the
in-memory cookie jar to the on-disk `Default/Cookies` sqlite file — the on-disk copy can be
completely empty (`PRAGMA page_count` mostly freelist, `SELECT count(*) FROM cookies` = 0) for
hours while the live browser still holds valid session cookies in memory. Reading the on-disk file
(what `extract-note-cookies.py` does) proves nothing about the live session.

Fix: pull cookies from the LIVE browser over CDP instead of the disk file — this reads what the
browser actually holds right now, not its last flush:

```python
from playwright.sync_api import sync_playwright
import json
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    ctx = b.contexts[0]
    note_cookies = {c["name"]: c["value"] for c in ctx.cookies() if c["domain"].endswith("note.com")}
    json.dump(note_cookies, open(os.path.expanduser("~/.local/state/life-manager/writer/note-work/note-cookies.json"), "w"))
    b.close()
```

Run with `$WRITER_BROWSER_PYTHON` (the managed Writer runtime with Playwright). This is read-only
against the live daily-driver — it does not open a new tab, close anything, or touch the existing
session, so it is safe to run any time note-cookies.json looks stale or empty.

General law: when a derived-from-disk cache (cookie file, session dump, config snapshot) looks
empty or wrong, check the LIVE process first before concluding the source is actually gone —
the disk copy can lag the live state by hours.
