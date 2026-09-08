#!/usr/bin/env python3
"""embed-mermaid-substack — replace every ```mermaid fence in a markdown file with an
uploaded Substack image, so publish-substack.sh (or any other Substack draft path) can
ship it. Substack (like note) does not render mermaid — it must be a PNG.

GENERIC: unlike scripts/substack-publish/substack-publish.py (hardcoded to the
"automaton" article's free/paid split and pre-rendered note assets), this takes ANY
markdown file, finds ```mermaid fences in document order, and handles them itself —
no assumptions about headings, sections, or a paired note publish having already run.

STDLIB ONLY — no venv needed. Pipeline per diagram:
  1. kroki POST -> PNG. urllib.request with an explicit User-Agent (kroki 403s the bare
     urllib default UA — MEASURED, same fix note-stage2-publish.py already uses).
  2. Substack's own image API: POST https://{publication}/api/v1/image, form-encoded
     body {"image": "data:image/png;base64,..."}, auth = the SUBSTACK_SESSION_COOKIE
     cookie header (same auth scripts/publish-substack.sh already uses for drafts) ->
     returns {"url": "https://substack-post-media.s3.amazonaws.com/..."}.
     Confirmed by reading https://raw.githubusercontent.com/ma2za/python-substack/main/
     substack/api.py `get_image()`: `data={"image": image}` posted with requests (i.e.
     application/x-www-form-urlencoded, not JSON) to `{publication_url}/api/v1/image`.
  3. the ```mermaid fence is replaced with `![figure](<that URL>)`.

PADDING (task #13, MEASURED 2026-07-16): kroki renders a flowchart TD portrait (e.g.
276x606 for this article's fig1) and Substack stretches EVERY body image to its fixed
728px reader column regardless of any width/height markdown or HTML attempts (unlike
note.com, which honors an explicit embed width via generate_image_html — that trick does
not exist here). A raw 276x606 PNG stretched to 728 wide would render at ~1598px tall on
screen, filling the page (same failure class as SKILL.md rule 71). Fix: BEFORE upload,
pad the PNG's own canvas horizontally (transparent, centered, diagram pixels untouched --
never upscale, only add side margin) so its OWN aspect ratio already satisfies the
728-wide budget: `min_w = ceil(h * DISPLAY_WIDTH / MAX_DISPLAY_HEIGHT)`. Measured: fig1
276x606 -> padded 520x606 -> on-screen height at 728 wide = 848px (<900 budget, margin
under verify-preview.py's own 950 gate too). Requires Pillow (not stdlib) -- declared and
checked explicitly at the call site (FATAL with an install command if missing), not
silently borrowed from another venv.

Env (same names/source as scripts/publish-substack.sh): SUBSTACK_SESSION_COOKIE
(required, full Cookie header value), SUBSTACK_PUBLICATION (default
aniccabuddha.substack.com). Load from LIFE_MANAGER_ENV_FILE before running, or export them.

Assets + the upload cache are PERSISTENT, never /tmp (repo rule): default assets dir is
~/.cloak/note-work/<slug>-substack-assets/, cache is
~/.cloak/note-work/substack-img-cache.json, so a re-run does not re-upload an image it
already has a URL for.

CLI:
    embed-mermaid-substack.py --markdown-file <src.md> --out <dst.md> [--assets-dir <dir>]
stdout: one JSON line — {"diagrams": N, "urls": [...], "out": "<dst.md>"}
Exit 1 (FATAL) if any kroki render comes back too small to be a real PNG (kroki's own
error-blob-on-parse-failure, e.g. unquoted parens in an edge label — same guard as
devto-publish/render-en-diagrams.py), or if SUBSTACK_SESSION_COOKIE is missing.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HTTP_DIR = Path(__file__).resolve().parents[1] / "substack-publish"
if str(HTTP_DIR) not in sys.path:
    sys.path.insert(0, str(HTTP_DIR))
from substack_http import json_request as substack_json_request

MIN_PNG_BYTES = 2000  # below this, kroki returned an error blob, not a real diagram
CACHE_FILE = os.path.expanduser("~/.cloak/note-work/substack-img-cache.json")
KROKI_UA = "Mozilla/5.0"  # kroki 403s urllib's bare default UA — measured

DISPLAY_WIDTH = 728       # Substack stretches every body image to this reader-column width
MAX_DISPLAY_HEIGHT = 850  # on-screen height budget after that stretch (task #13; margin
                           # under both the <900 target and verify-preview.py's own 950 gate)


def fatal(msg: str) -> None:
    raise SystemExit(f"FATAL: {msg}")


def pad_to_fit(png_path: Path) -> None:
    """Pad a portrait diagram's canvas horizontally (transparent, centered) so that when
    Substack stretches it to DISPLAY_WIDTH, its on-screen height stays under
    MAX_DISPLAY_HEIGHT. Never upscales the diagram itself -- only adds side margin. No-op
    if the image already fits. Overwrites png_path in place."""
    try:
        from PIL import Image
    except ImportError:
        fatal(
            "Pillow (PIL) is required to pad diagrams for Substack — install it: "
            f"{sys.executable} -m pip install Pillow"
        )
    im = Image.open(png_path)
    w, h = im.size
    min_w = math.ceil(h * DISPLAY_WIDTH / MAX_DISPLAY_HEIGHT)
    if min_w <= w:
        return
    im = im.convert("RGBA")
    canvas = Image.new("RGBA", (min_w, h), (0, 0, 0, 0))
    canvas.paste(im, ((min_w - w) // 2, 0), im)
    canvas.save(png_path)


def render_mermaid_to_png(source: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        "https://kroki.io/mermaid/png",
        data=source.strip().encode("utf-8"),
        headers={"Content-Type": "text/plain", "User-Agent": KROKI_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            out_path.write_bytes(resp.read())
    except urllib.error.URLError as e:
        fatal(f"kroki request failed for {out_path.name}: {e}")
    if out_path.stat().st_size < MIN_PNG_BYTES:
        fatal(
            f"kroki did not return a real PNG for {out_path.name} "
            f"({out_path.stat().st_size} bytes) — likely a mermaid parse error "
            "(e.g. unquoted parens in an |edge label|); fix the source and re-run."
        )


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        return json.load(open(CACHE_FILE, encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"))


def upload_image(png_path: str, publication: str, cookie: str) -> str:
    data_uri = "data:image/png;base64," + base64.b64encode(Path(png_path).read_bytes()).decode("ascii")
    body = urllib.parse.urlencode({"image": data_uri}).encode("utf-8")
    try:
        result = substack_json_request(
            "POST",
            f"https://{publication}/api/v1/image",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0",
            },
            timeout=60,
            body=body,
        )
    except urllib.error.HTTPError as e:
        fatal(f"Substack image upload failed for {png_path}: HTTP {e.code} {e.read()[:300]!r}")
    except urllib.error.URLError as e:
        fatal(f"Substack image upload failed for {png_path}: {e}")
    except OSError as e:
        fatal(f"Substack image upload failed for {png_path}: {e}")
    url = result.get("url")
    if not url:
        fatal(f"Substack image upload for {png_path} returned no url: {result!r}")
    return url


def upload(png_path: str, cache: dict, publication: str, cookie: str) -> str:
    if png_path not in cache:
        cache[png_path] = upload_image(png_path, publication, cookie)
        save_cache(cache)
        time.sleep(1.5)  # pace uploads to avoid Substack 429
    return cache[png_path]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--markdown-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--assets-dir")
    p.add_argument("--headline-image")
    p.add_argument("--body-image", action="append", default=[])
    args = p.parse_args(argv)

    src_path = Path(args.markdown_file)
    if not src_path.exists():
        fatal(f"--markdown-file not found: {args.markdown_file}")
    md = src_path.read_text(encoding="utf-8")

    diagrams = list(re.finditer(r"```mermaid\n(.*?)```", md, re.S))
    headline_path = Path(args.headline_image) if args.headline_image else None
    body_paths = [Path(value) for value in args.body_image]
    if headline_path is not None and not headline_path.is_file():
        fatal(f"--headline-image not found: {headline_path}")
    if body_paths and len(body_paths) != len(diagrams):
        fatal(
            "the number of immutable --body-image files must match "
            "the Mermaid diagrams"
        )
    for body_path in body_paths:
        if not body_path.is_file():
            fatal(f"--body-image not found: {body_path}")
    if not diagrams and headline_path is None:
        Path(args.out).write_text(md, encoding="utf-8")
        print(json.dumps({"diagrams": 0, "urls": [], "out": args.out}))
        return 0

    cookie = os.environ.get("SUBSTACK_SESSION_COOKIE", "")
    if not cookie:
        fatal("SUBSTACK_SESSION_COOKIE missing (configure LIFE_MANAGER_ENV_FILE first)")
    publication = os.environ.get("SUBSTACK_PUBLICATION", "aniccabuddha.substack.com")

    slug = src_path.stem
    assets_dir = Path(args.assets_dir or os.path.expanduser(f"~/.cloak/note-work/{slug}-substack-assets"))
    assets_dir.mkdir(parents=True, exist_ok=True)

    cache = load_cache()
    headline_url = (
        upload(str(headline_path), cache, publication, cookie)
        if headline_path is not None
        else ""
    )
    urls: list[str] = []
    for i, m in enumerate(diagrams, start=1):
        if body_paths:
            png_path = body_paths[i - 1]
        else:
            png_path = assets_dir / f"fig{i}.png"
            render_mermaid_to_png(m.group(1), png_path)
            pad_to_fit(png_path)  # task #13: never let a raw portrait kroki PNG go out
        url = upload(str(png_path), cache, publication, cookie)
        urls.append(url)

    def replace(m):
        idx = replace.n
        replace.n += 1
        return f"![figure]({urls[idx]})"
    replace.n = 0
    out_md = re.sub(r"```mermaid\n.*?```", replace, md, flags=re.S)
    if headline_url:
        frontmatter = re.match(
            r"^---\r?\n.*?\r?\n---(?:\r?\n|$)", out_md, re.S
        )
        insertion = frontmatter.end() if frontmatter else 0
        remainder = out_md[insertion:]
        heading = re.match(r"([ \t]*#\s+.*(?:\r?\n|$))", remainder)
        if heading:
            insertion += heading.end()
        out_md = (
            out_md[:insertion]
            + f"\n![Headline image]({headline_url})\n\n"
            + out_md[insertion:].lstrip("\r\n")
        )

    Path(args.out).write_text(out_md, encoding="utf-8")
    print(json.dumps({
        "diagrams": len(diagrams),
        "headline_url": headline_url or None,
        "urls": urls,
        "out": args.out,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
