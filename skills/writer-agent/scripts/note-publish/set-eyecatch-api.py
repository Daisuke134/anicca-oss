#!/usr/bin/env python3
"""Set one note draft eyecatch through the authenticated upload API."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from note_mcp.api.images import upload_eyecatch_image
from note_mcp.models import Session


async def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: set-eyecatch-api.py IMAGE")
    target = os.environ.get("NOTE_KEY", "")
    if not re.fullmatch(r"n[a-z0-9]+", target):
        raise SystemExit("FATAL: NOTE_KEY required")
    image = Path(sys.argv[1])
    cookies = json.loads(
        (Path.home() / ".cloak/note-work/note-cookies.json").read_text(
            encoding="utf-8"
        )
    )
    session = Session(
        cookies=cookies,
        user_id=os.environ["NOTE_USER_ID"],
        username=os.environ["NOTE_URLNAME"],
        created_at=int(time.time()),
    )
    uploaded = await upload_eyecatch_image(session, str(image), target)
    url = str(uploaded.url)
    if not url.startswith("https://assets.st-note.com/"):
        raise SystemExit("FATAL: note eyecatch API returned an invalid asset URL")
    print(f"EYECATCH_IN_EDITOR: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
