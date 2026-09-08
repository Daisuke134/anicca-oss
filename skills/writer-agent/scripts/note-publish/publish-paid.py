"""Publish a note.com article as either FREE or PLAIN PAID (買い切り). Generalized CLI,
two-stage guard: without --arm the script configures the selected article type and tags, plus price
and paywall for paid articles, then STOPS before the one irreversible click; with --arm it clicks and
self-verifies.

The Writer money contract uses the plain-paid path at a fixed price supplied by its executable policy.
The generic CLI retains --free for callers outside that Writer contract. The other publisher in this
directory (publish.py) configures a different 無料+メンバーシップ+試し読み shape and is not used by this flow.

Every selector below was read off the live editor, not guessed:
  記事タイプ = input[name="is_paid"] value=free|paid   (click the label, not the input)
  price      = input[type=text][placeholder="300"]     (PRE-FILLED WITH 500 — must be overwritten)
  paywall    = "有料エリア設定" replaces 投稿する until a line is placed; inside it every paragraph
               gets its own 「ラインをこの場所に変更」 button and the current line shows
               「このラインより先を有料にする」

WHY --arm is safe to gate on (measured 2026-07-16, SKILL.md #74): 有料/price/paywall-line/tags are
TRANSIENT draft form state. note only commits them on 投稿する — reload the editor and they are gone
(price reads back NONE, paid radio unchecked). So stopping before the click leaves ZERO trace on
note; there is no "configure now, publish later" because there is nothing left to publish later.
--arm is therefore the only meaningful gate: everything before it is inspectable and reversible by
just not clicking; the moment 投稿する/更新する is clicked (inside the --arm branch, and ONLY there
in this file) the change is real and gets a mandatory self-verify against note's own API.

usage:
  publish-paid.py --key <draft key> --free [--tags "t1,t2,t3"] [--arm]
  publish-paid.py --key <draft key> --price <yen> [--after-chars 2500] [--tags "t1,t2,t3"] [--arm]

  --key          note draft key, e.g. nd963b86eeaa7                              (required)
  --free         publish the complete article for free; --price is not required
  --price        integer yen price                                              (required unless --free)
  --after-chars  free-part length before the paid paywall line, default 2500 — this is note.com's own
                 market shape (punimaru_dev ¥3,480 -> 2,529 chars free; shiro_life0 ¥100,000 -> 2,467).
                 RE-MEASURE per paid article, do not trust the default.
  --tags         comma-separated hashtags, no leading #, up to 5. Pick them by measuring, not vibes:
                 scripts/_shared/tag-counts.py <candidates...> returns real counts; avoid the giants
                 (#AI is 794k, you drown) and take what your buyers actually search.
  --arm          actually click 投稿する/更新する and self-verify via GET /api/v3/notes/{key}. Without
                 this flag the run stops right after printing FREE_ENDS_WITH/PAID_STARTS_WITH — the
                 last chance to eyeball the paywall boundary before anything becomes real.

stdout: machine-readable token lines only (TAGS_ENTERED=, PRICE_READBACK=, PAYWALL_PLACED=,
FREE_ENDS_WITH:, PAID_STARTS_WITH:, GUARD_STOPPED=, POSTED_CLICKED=, API_VERIFY,
FREE_PUBLISHED or PAID_PUBLISHED).
Diagnostics and FATAL errors go to stderr (FATAL: via SystemExit, same as every other script here).
exit 0 = either a clean guard-stop (no --arm) or an armed publish that verified against the API.
exit non-zero = something is wrong; with --arm this can mean the article WAS published but did not
verify — that fact is always printed before the non-zero exit, never hidden.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
# --- fail-closed PII gate wiring ---------------------------------------------------
# gate_run_dir raises SystemExit (non-zero) on a finding, a missing blocklist, an unreadable
# artifact, or ANY scanner error. No code path here turns a failure into a publish.
import sys as _pii_sys  # noqa: E402
from pathlib import Path as _PiiPath  # noqa: E402
_pii_sys.path.insert(0, str(next(
    _p / "_shared"
    for _p in _PiiPath(__file__).resolve().parents
    if (_p / "_shared" / "pii_gate.py").is_file()
)))
from pii_gate import gate_run_dir  # noqa: E402

import time
import urllib.error
import urllib.request

from cloakbrowser import launch_context
from publish_guard import assert_publish_allowed

WORK = os.path.join(
    os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")),
    "note-work",
)
PUBLICATION_GUARD = os.path.join(os.path.dirname(os.path.dirname(__file__)), "publication-guard.py")


class NoteMutationTrace:
    """Persist Note API mutation responses without recording credential headers."""

    def __init__(self, page, path: Path):
        self.path = path
        self.mutations = []
        page.on("response", self._record_response)

    def _record_response(self, response) -> None:
        request = response.request
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        if not request.url.startswith("https://note.com/api/"):
            return
        self.mutations.append(
            {
                "method": request.method,
                "url": request.url,
                "status": response.status,
                "post_data": request.post_data,
            }
        )

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "schema": "writer.note-publish-mutations",
                    "version": 1,
                    "mutations": self.mutations,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


class NoteNativePublishError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Note native publish HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class NoteBodyBlocks(HTMLParser):
    """Find top-level Note blocks and text lengths while preserving source bytes."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self, body: str):
        super().__init__(convert_charrefs=True)
        self.body = body
        self.line_starts = [0]
        for match in re.finditer("\n", body):
            self.line_starts.append(match.end())
        self.depth = 0
        self.current = None
        self.blocks = []
        self.body_length = 0

    def absolute_position(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def handle_starttag(self, tag, attrs) -> None:
        if self.depth == 0:
            values = dict(attrs)
            self.current = {
                "start": self.absolute_position(),
                "id": values.get("id") or values.get("name") or "",
                "visible_chars": 0,
            }
        if tag not in self.VOID_TAGS:
            self.depth += 1
        elif self.depth == 0 and self.current is not None:
            self.blocks.append(self.current)
            self.current = None

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag) -> None:
        if tag not in self.VOID_TAGS and self.depth > 0:
            self.depth -= 1
        if self.depth == 0 and self.current is not None:
            self.blocks.append(self.current)
            self.current = None

    def handle_data(self, data) -> None:
        self.body_length += len(data)
        if self.current is not None:
            self.current["visible_chars"] += len(data.strip())


ABSOLUTE_URL = re.compile(r"^https?://", re.I)
FIGURE_ELEMENT = re.compile(r"<figure\b[^>]*>.*?</figure>", re.S | re.I)
IMG_ELEMENT = re.compile(r"<img\b[^>]*>", re.I)
IMG_SRC = re.compile(r"""\bsrc\s*=\s*["']([^"']*)["']""", re.I)
IMG_ALT = re.compile(r"""\balt\s*=\s*["']([^"']*)["']""", re.I)
FIGCAPTION_TEXT = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption>", re.S | re.I)
OPEN_TAG_ATTR = re.compile(r"""\b(name|id)\s*=\s*["']([^"']*)["']""", re.I)
EDITOR_ONLY_IMG_ATTRS = re.compile(
    r"""\s+(?:contenteditable|draggable)\s*=\s*["'][^"']*["']""", re.I
)
TAG_STRIP = re.compile(r"<[^>]+>")


def _text_of(fragment: str) -> str:
    return " ".join(TAG_STRIP.sub("", fragment).split())


def normalize_note_publish_body(
    body: str, *, resolve_asset_url=None
) -> tuple[str, dict]:
    """Degrade body elements note's paid publish surface has never accepted.

    MEASURED, not guessed (2026-08-08, authenticated read-only GETs of the failing
    draft plus the four paid articles note has actually accepted at ¥500 —
    n190c1d92bf10, n7a0eac82f085, n84aed983c96c, n2fb2c506deda).  Element inventory
    of both halves of all five, split on the `separator` note itself stores:

      * every `<img>` in every accepted half, free or paid, has an absolute
        `https://assets.st-note.com/...` src;
      * the rejected paid half of `n47735d9811e8` carries
        `<img src="headline-image.png">` and `<img src="body-diagram.png">` —
        bare run-directory filenames that were never uploaded anywhere;
      * note's own stored render of that same draft DELETES exactly those two
        `<img>` elements while keeping the three note-hosted ones, so note can
        represent the hosted images and cannot represent these.

    That is the only element class present in the rejected paid half and absent
    from all four accepted paid halves.  It is also exactly the shape of an
    asymmetry where `draft_save` succeeds and the paid PUT returns
    `422 本文に利用できない内容が含まれています。`: the draft surface stores
    what it is sent and sanitises on read; the publish surface validates.

    TRANSFORM, DO NOT DELETE.  The `<figure>` becomes a `<p>` carrying the SAME
    `name`/`id`, so the top-level block count and every block id are unchanged and
    the separator/boundary arithmetic is untouched.  When the caller can resolve
    the asset to a public https URL the figure degrades into an anchor to it —
    a shape note has accepted inside a paid half (`n7a0eac82f085` publishes
    `<a href="https://raw.githubusercontent.com/..." target="_blank"
    rel="nofollow noopener">` behind its paywall).  Otherwise it degrades to the
    caption text, so the reader still gets what the image was for.

    Also strips the editor-only `contenteditable`/`draggable` attributes from
    `<img>`: they appear in zero accepted bodies, note deletes them from its own
    render of this article, and they carry no reader meaning.  This is the second
    ranked candidate, cleared in the same pass at no extra cost; the image src is
    the primary one.
    """
    report = {
        "images_degraded": [],
        "images_linked": 0,
        "editor_only_attrs_stripped": 0,
    }

    def _degraded_inner(src: str, label: str) -> str:
        resolved = ""
        if resolve_asset_url is not None:
            candidate = str(resolve_asset_url(src) or "")
            if candidate.startswith("https://"):
                resolved = candidate
        text = label or resolved or src
        if resolved:
            report["images_linked"] += 1
            href = resolved.replace("&", "&amp;").replace('"', "&quot;")
            return (
                f'<a href="{href}" target="_blank" rel="nofollow noopener">{text}</a>'
            )
        return text

    def _figure(match: re.Match) -> str:
        element = match.group(0)
        img = IMG_ELEMENT.search(element)
        if img is None:
            return element
        src_match = IMG_SRC.search(img.group(0))
        src = src_match.group(1) if src_match else ""
        if ABSOLUTE_URL.match(src.strip()):
            return element
        open_tag = element[: element.index(">") + 1]
        attrs = dict((k.lower(), v) for k, v in OPEN_TAG_ATTR.findall(open_tag))
        caption = FIGCAPTION_TEXT.search(element)
        alt_match = IMG_ALT.search(img.group(0))
        label = _text_of(caption.group(1)) if caption else ""
        if not label and alt_match:
            label = _text_of(alt_match.group(1))
        report["images_degraded"].append(src)
        carried = "".join(
            f' {key}="{attrs[key]}"' for key in ("name", "id") if attrs.get(key)
        )
        return f"<p{carried}>{_degraded_inner(src, label)}</p>"

    normalized = FIGURE_ELEMENT.sub(_figure, body)

    def _bare_image(match: re.Match) -> str:
        element = match.group(0)
        src_match = IMG_SRC.search(element)
        src = src_match.group(1) if src_match else ""
        if ABSOLUTE_URL.match(src.strip()):
            return element
        alt_match = IMG_ALT.search(element)
        label = _text_of(alt_match.group(1)) if alt_match else ""
        report["images_degraded"].append(src)
        return _degraded_inner(src, label)

    normalized = IMG_ELEMENT.sub(_bare_image, normalized)

    def _strip_editor_attrs(match: re.Match) -> str:
        element = match.group(0)
        cleaned, count = EDITOR_ONLY_IMG_ATTRS.subn("", element)
        report["editor_only_attrs_stripped"] += count
        return cleaned

    normalized = IMG_ELEMENT.sub(_strip_editor_attrs, normalized)
    report["changed"] = normalized != body
    return normalized, report


def build_paid_publish_payload(
    note: dict,
    *,
    price: int,
    after_chars: int,
    tags: list[str],
    resolve_asset_url=None,
    normalization_report: dict | None = None,
) -> dict:
    """Build the Note paid-article PUT payload note has actually accepted.

    Recovered from an accepted publish receipt on the configured Note account:
    nineteen keys, `separator` = the id of the LAST FREE top-level block, `pay_body`
    starting at the block after it.  note stored `separator`
    "ec072720-e1e2-4d03-a893-477022e422c8", which is exactly that block, and the same
    holds for the notes published through note's own editor (n7a0eac82f085,
    n84aed983c96c, n2fb2c506deda).  No `image_keys`: n190c1d92bf10's paid half is a
    `<figure>` whose note-hosted image is live and hash-verified, published without
    the field ever being declared.  Do not add fields note has not been seen to accept.
    """
    draft = note.get("note_draft") if isinstance(note.get("note_draft"), dict) else {}
    body = str(draft.get("body") or note.get("body") or "")
    body, normalization = normalize_note_publish_body(
        body, resolve_asset_url=resolve_asset_url
    )
    if normalization_report is not None:
        normalization_report.clear()
        normalization_report.update(normalization)
    parsed = NoteBodyBlocks(body)
    parsed.feed(body)
    blocks = parsed.blocks
    if len(blocks) < 2:
        raise SystemExit("FATAL: paid Note requires at least two body blocks")
    visible_chars = 0
    separator_index = None
    for index, block in enumerate(blocks[:-1]):
        visible_chars += int(block["visible_chars"])
        if visible_chars >= after_chars:
            separator_index = index
            break
    if separator_index is None:
        separator_index = len(blocks) - 2
    separator_block = blocks[separator_index]
    paid_block = blocks[separator_index + 1]
    separator = str(separator_block["id"])
    if not separator:
        raise SystemExit("FATAL: paid Note separator block has no stable id")
    boundary_start = int(paid_block["start"])
    separator_start = int(separator_block["start"])
    if separator not in body[separator_start : separator_start + 500]:
        raise SystemExit("FATAL: paid Note separator is absent from original HTML")
    hashtags = [f"#{tag.lstrip('#')}" for tag in tags if tag.lstrip("#")]
    return {
        "author_ids": [],
        "body_length": parsed.body_length,
        "disable_comment": bool(note.get("disable_comment", False)),
        "exclude_from_creator_top": bool(note.get("exclude_from_creator_top", False)),
        "exclude_ai_learning_reward": bool(note.get("exclude_ai_learning_reward", False)),
        "free_body": body[:boundary_start],
        "hashtags": hashtags,
        "index": bool(note.get("index")),
        "is_refund": bool(note.get("is_refund", False)),
        "limited": False,
        "magazine_ids": [],
        "magazine_keys": [],
        "name": str(note.get("name") or draft.get("name") or ""),
        "pay_body": body[boundary_start:],
        "price": price,
        "send_notifications_flag": bool(note.get("send_notifications_flag", True)),
        "separator": separator,
        "slug": str(note.get("slug") or ""),
        "status": "published",
    }


def build_paid_draft_save_payload(publish_payload: dict) -> dict:
    """Persist the paid split on note's own draft, before the irreversible PUT.

    Copied from i0switch/ThreadsOS `NoteApiPlaywrightClient.saveDraft`
    (`src/adapters/note-api/playwright-client.ts`): when a paid split is
    requested it puts `free_body`, `pay_body`, `separator`, `limited: true` and
    `price` on `POST /api/v1/text_notes/draft_save?id=...&is_temp_saved=true`
    and only then issues `PUT /api/v1/text_notes/{id}`.  Nothing here is
    invented; the key set and the `limited` value are that client's.

    MEASURED REASON THIS MATTERS (2026-08-07, authenticated read-only GET):
    note's draft object carries a `separator` slot of its own.  For the rejected
    article `n47735d9811e8`, `note_draft.separator` is `null` while the PUT
    asserted `d44c0a65-1a70-415e-b959-27294926cdc7`, so note was asked to
    publish a boundary its own draft state had never been told about.

    The body is NOT regenerated: it is exactly `free_body + pay_body`, the split
    this run resolved from the body note itself is holding, so the id in
    `separator` cannot go stale between the draft_save and the PUT.
    """
    return {
        "body": publish_payload["free_body"] + publish_payload["pay_body"],
        "body_length": publish_payload["body_length"],
        "free_body": publish_payload["free_body"],
        "index": publish_payload["index"],
        "is_lead_form": False,
        "limited": True,
        "name": publish_payload["name"],
        "pay_body": publish_payload["pay_body"],
        "price": publish_payload["price"],
        "separator": publish_payload["separator"],
    }


def save_paid_split_draft(
    numeric_id: int,
    payload: dict,
    cookies: dict[str, str],
    *,
    opener=urllib.request.urlopen,
) -> dict:
    """Save the split to note's draft surface. This can never publish anything.

    Fail-closed, because this is the one write this file makes that is NOT the
    guarded publish: a draft_save carrying `status` would be a publish wearing a
    draft's URL, so refuse it before the request is built.  `is_temp_saved=true`
    plus the absence of `status` is the same shape the draft surface answered
    `201` with `result: true` for on 2026-08-07, after which the scratch draft
    read back `status draft`, `price 0`, `publish_at null` and returned `404`
    anonymously (`config/note-422-draft-surface-observation.json`).
    """
    if "status" in payload:
        raise SystemExit("FATAL: a note draft_save must never carry a status field")
    request = urllib.request.Request(
        f"https://note.com/api/v1/text_notes/draft_save?id={numeric_id}&is_temp_saved=true",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        headers=note_editor_headers(cookies),
        method="POST",
    )
    try:
        with opener(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise NoteNativePublishError(error.code, body) from error


def note_editor_headers(cookies: dict[str, str]) -> dict[str, str]:
    """The editor-origin header set note's own client sends.

    Kept in one place because it is load-bearing, not cosmetic: note's
    `DELETE /api/v1/notes/n/{key}` answers `422` without `Origin`, `Referer` and
    `X-Requested-With` and `200` with them, so a note `422` is not always about
    content.  Both the draft_save and the PUT must carry the same set.
    """
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
        "Origin": "https://editor.note.com",
        "Referer": "https://editor.note.com/",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookies.get("XSRF-TOKEN"):
        headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
    return headers


def put_paid_note(
    numeric_id: int,
    payload: dict,
    cookies: dict[str, str],
    *,
    opener=urllib.request.urlopen,
) -> dict:
    """Execute Note's publisher-native authenticated PUT once."""
    headers = note_editor_headers(cookies)
    request = urllib.request.Request(
        f"https://note.com/api/v1/text_notes/{numeric_id}",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        headers=headers,
        method="PUT",
    )
    try:
        with opener(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise NoteNativePublishError(error.code, body) from error


def get_authenticated_note(
    key: str,
    cookies: dict[str, str],
    *,
    opener=urllib.request.urlopen,
) -> dict:
    request = urllib.request.Request(
        f"https://note.com/api/v3/notes/{key}",
        headers={
            "Accept": "application/json",
            "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
            "User-Agent": "Mozilla/5.0",
        },
    )
    with opener(request, timeout=30) as response:
        return json.loads(response.read())["data"]


def run_media_url_resolver(run_dir: Path):
    """Resolve a run-local image filename to the public URL the run already staged.

    Read-only, and never invents a URL: it uses the same
    `gates/media-urls.json` receipt the Dev.to and Zenn publishers already
    consume, so a `<figure>` note cannot host degrades into a link to the exact
    bytes the other destinations serve. A missing or malformed receipt returns a
    resolver that resolves nothing, and the caller then degrades to caption text
    rather than to a guessed URL.
    """
    try:
        receipt = json.loads(
            (run_dir / "gates" / "media-urls.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return lambda src: ""
    by_name: dict[str, str] = {}
    for asset in receipt.get("assets", []) if isinstance(receipt, dict) else []:
        if not isinstance(asset, dict):
            continue
        url = str(asset.get("url") or "")
        name = os.path.basename(str(asset.get("path") or ""))
        if name and url.startswith("https://"):
            by_name[name] = url
    return lambda src: by_name.get(os.path.basename(str(src).split("?")[0]), "")


def write_native_effect(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def verify_note_publication(args, managed_publication: bool) -> int:
    req = urllib.request.Request(
        f"https://note.com/api/v3/notes/{args.key}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    d = json.load(urllib.request.urlopen(req, timeout=25))["data"]
    got_price, got_limited, got_trial = (
        d.get("price"),
        d.get("is_limited"),
        d.get("is_trial"),
    )
    status = d.get("status")
    print(
        f"API_VERIFY key={args.key} price={got_price} "
        f"is_limited={got_limited} is_trial={got_trial} status={status}"
    )
    if args.free:
        ok = (
            got_price in (None, 0)
            and got_limited is False
            and got_trial is False
            and status == "published"
        )
        print(f"FREE_PUBLISHED verified={str(ok).lower()} key={args.key} price={got_price}")
    else:
        ok = (
            got_price == args.price
            and got_limited is False
            and got_trial is False
            and status == "published"
        )
        print(f"PAID_PUBLISHED key={args.key} price={got_price} verified={str(ok).lower()}")
    if not ok:
        raise SystemExit("FATAL: Note public API readback does not match publish intent")
    if managed_publication:
        reconciled = subprocess.run(
            [sys.executable, PUBLICATION_GUARD, "reconcile", "--pair", "note/ja"],
            check=False,
            text=True,
            capture_output=True,
        )
        if reconciled.returncode != 0:
            raise SystemExit(
                reconciled.stderr.strip()
                or "FATAL: note live but receipt reconciliation failed"
            )
        print(f"PUBLICATION_RECEIPT={reconciled.stdout.strip()}")
    return 0

def open_authenticated_note_page(cookies):
    """Open an isolated CloakBrowser context so shared CDP failures cannot close it."""
    context = launch_context(headless=True, humanize=False)
    context.add_cookies(
        [
            {
                "name": key,
                "value": value,
                "domain": ".note.com",
                "path": "/",
                "secure": True,
            }
            for key, value in cookies.items()
        ]
    )
    return context, context.new_page()


def click_publish_button(page, label: str) -> str:
    """Dispatch one trusted Playwright click for the visible publish control."""
    page.get_by_role("button", name=label, exact=True).click(timeout=6000)
    return label


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--key", help="note draft key, e.g. nd963b86eeaa7")
    p.add_argument("--free", action="store_true", help="publish the complete article for free")
    p.add_argument("--price", type=int, help="integer yen price (required unless --free)")
    p.add_argument("--after-chars", type=int, default=2500,
                    help="free-part length before the paid paywall line (default 2500; re-measure per article)")
    p.add_argument("--tags", default="", help="comma-separated hashtags, no leading #, up to 5")
    p.add_argument("--arm", action="store_true",
                    help="click 投稿する/更新する and self-verify. Without this flag the script stops "
                         "before the click (no trace left on note — see module docstring).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # Fail-closed PII gate: this repairs/republishes a LIVE article, so re-scan the frozen
    # artifacts before touching it. An unset ARTICLE_RUN_DIR is itself a refusal.
    gate_run_dir("note-publish-paid", os.environ.get("ARTICLE_RUN_DIR", ""), pair="note/ja")
    if not args.key:
        raise SystemExit("FATAL: --key is required, e.g. --key nd963b86eeaa7")
    if args.free and args.price is not None:
        raise SystemExit("FATAL: --free and --price are mutually exclusive")
    if not args.free and (not args.price or args.price <= 0):
        raise SystemExit("FATAL: --price is required and must be a positive integer yen amount unless --free is set")
    tags = [t.strip().lstrip("#") for t in args.tags.split(",") if t.strip()][:5]
    managed_publication = False

    # The two-factor click guard protects authorization; this run-scoped guard protects
    # idempotency.  Ask note's API about the stable draft key before opening the editor.  If a
    # previous process died after the click but before ledger append, it repairs the receipt and
    # returns without another 更新する click.
    if args.arm:
        checked = subprocess.run(
            [sys.executable, PUBLICATION_GUARD, "preflight", "--pair", "note/ja",
             "--target-kind", "note-key", "--target", args.key],
            check=False, text=True, capture_output=True,
        )
        if checked.returncode != 0:
            raise SystemExit(checked.stderr.strip() or "FATAL: publication resume guard refused note")
        decision = json.loads(checked.stdout)
        managed_publication = decision.get("action") != "manual-unmanaged"
        if decision.get("action") == "skip-live":
            print(f"ALREADY_LIVE_SKIP url={decision['live_url']}")
            return 0

    ck = json.load(open(WORK + "/note-cookies.json"))
    if args.arm and not args.free:
        assert_publish_allowed()
        note = get_authenticated_note(args.key, ck)
        run_dir = Path(os.environ["ARTICLE_RUN_DIR"])
        normalization: dict = {}
        payload = build_paid_publish_payload(
            note,
            price=args.price,
            after_chars=args.after_chars,
            tags=tags,
            resolve_asset_url=run_media_url_resolver(run_dir),
            normalization_report=normalization,
        )
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        effect_path = run_dir / "gates" / "note-native-effect.json"
        effect = {
            "schema": "writer.note-native-effect",
            "version": 1,
            "pair": "note/ja",
            "stable_target": args.key,
            "numeric_id": int(note["id"]),
            "payload_sha256": payload_sha256,
            "state": "intent",
            # Evidence, not payload: what the publish path had to degrade before
            # asking note to publish. A future 422 with an empty report means the
            # image class was not the cause and the next candidate is due.
            "body_normalization": normalization,
        }
        write_native_effect(effect_path, effect)
        # Persist the split on note's own draft FIRST, so the publish is not the
        # only place the boundary has ever existed.  Fail closed: if note will
        # not hold the split, do not ask it to publish one.
        draft_payload = build_paid_draft_save_payload(payload)
        save_paid_split_draft(int(note["id"]), draft_payload, ck)
        effect["draft_split_saved"] = True
        effect["draft_save_separator"] = draft_payload["separator"]
        write_native_effect(effect_path, effect)
        try:
            response = put_paid_note(int(note["id"]), payload, ck)
        except NoteNativePublishError as error:
            effect["state"] = "rejected"
            effect["http_status"] = error.status
            effect["response_body"] = error.body[:2000]
            write_native_effect(effect_path, effect)
            raise
        effect["state"] = "response"
        # Keep the flag as evidence, never as the verdict.  The one PUT note has
        # accepted -- configured canonical Note URL and publish_at
        # 2026-08-07T00:32:19+09:00, ¥500 -- answered HTTP 200 with no truthy
        # `data.result` and published the article anyway.  Treating that as a
        # logical failure reported the only success as FATAL.  Whether the
        # article is live is decided below, by reading note back.
        effect["result"] = bool(response.get("data", {}).get("result"))
        write_native_effect(effect_path, effect)
        print(f"POSTED_CLICKED=native-put payload_sha256={payload_sha256}")
        return verify_note_publication(args, managed_publication)
    pg = None
    isolated_context = None
    mutation_trace = None
    try:
        isolated_context, pg = open_authenticated_note_page(ck)
        run_dir = os.environ.get("ARTICLE_RUN_DIR")
        if run_dir:
            mutation_trace = NoteMutationTrace(
                pg, Path(run_dir) / "gates" / "note-publish-mutations.json"
            )
        pg.set_viewport_size({"width": 1280, "height": 1100})
        pg.goto(f"https://editor.note.com/notes/{args.key}/edit/", wait_until="domcontentloaded", timeout=45000)
        for _ in range(25):
            if "公開に進む" in pg.evaluate("()=>document.body.innerText"):
                break
            time.sleep(1)
        time.sleep(2)
        for b in pg.query_selector_all("button,a"):
            if (b.text_content() or "").strip() == "公開に進む":
                b.click()
                time.sleep(4)
                break

        # 1) 記事タイプ = 無料 or 有料. note requires clicking the label, not the input itself.
        article_type = "free" if args.free else "paid"
        if pg.evaluate("""(value)=>{const r=document.querySelector(`input[name="is_paid"][value="${value}"]`);
            if(!r) return false; (r.closest('label')||r).click(); return true;}""", article_type) is not True:
            raise SystemExit(f"FATAL: {article_type} radio not found")
        time.sleep(2)

        # 1b) hashtags — MEASURED 2026-07-16: tags set through the API (stage2's update_article) are
        # WIPED the moment this form is submitted, because at publish time the browser form is
        # authoritative and its tag field is empty if not filled here. Type them into THIS form, in
        # THIS run. Pick them by measuring (scripts/_shared/tag-counts.py), not vibes.
        if tags:
            inp = pg.query_selector('input[placeholder="ハッシュタグを追加する"]')
            if not inp:
                raise SystemExit("FATAL: hashtag input not found")
            for tg in tags:
                inp.click()
                inp.fill(tg)
                time.sleep(1.0)
                pg.keyboard.press("Enter")
                time.sleep(1.4)
            print("TAGS_ENTERED=" + ",".join(tags))

        if not args.free:
            # 2) price — React ignores a plain .value assignment, so go through the native setter and
            #    fire input/change; then READ IT BACK. "I set it" and "it is set" are different facts.
            pg.evaluate("""(p)=>{const i=[...document.querySelectorAll('input[type=text]')].find(x=>x.placeholder==='300');
                const set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                set.call(i,''); i.dispatchEvent(new Event('input',{bubbles:true}));
                set.call(i,p);  i.dispatchEvent(new Event('input',{bubbles:true}));
                i.dispatchEvent(new Event('change',{bubbles:true}));}""", str(args.price))
            time.sleep(2)
            back = pg.evaluate("""()=>{const i=[...document.querySelectorAll('input[type=text]')].find(x=>x.placeholder==='300');return i?i.value:'';}""")
            print(f"PRICE_READBACK={back}")
            if back != str(args.price):
                raise SystemExit(f"FATAL: price did not stick (wanted {args.price}, editor shows {back})")

            # 3) 有料エリア設定 → place the line after the first paragraph past --after-chars
            if not pg.evaluate("""()=>{const b=[...document.querySelectorAll('button,a')].find(x=>(x.textContent||'').trim()==='有料エリア設定'&&x.offsetParent!==null);
                if(b){b.click();return true;} return false;}"""):
                raise SystemExit("FATAL: 有料エリア設定 button not found")
            time.sleep(4)

            placed = pg.evaluate("""(after)=>{
                // Walk the overlay in document order, accumulating the text a reader would see for free,
                // and click the first 「ラインをこの場所に変更」 that appears once we are past `after`.
                const btns=[...document.querySelectorAll('button')].filter(b=>(b.textContent||'').trim()==='ラインをこの場所に変更');
                if(!btns.length) return {ok:false, reason:'no line buttons'};
                let chars=0, chosen=null, idx=-1;
                const walker=document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                let n;
                while(n=walker.nextNode()){
                  if(n.tagName==='BUTTON' && (n.textContent||'').trim()==='ラインをこの場所に変更'){
                    if(chars>=after){ chosen=n; idx=btns.indexOf(n); break; }
                  } else if(n.children.length===0 && n.textContent){
                    const t=n.textContent.trim();
                    if(t && t!=='ラインをこの場所に変更' && t!=='このラインより先を有料にする') chars+=t.length;
                  }
                }
                if(!chosen){ chosen=btns[btns.length-1]; idx=btns.length-1; }
                chosen.scrollIntoView({block:'center'}); chosen.click();
                return {ok:true, free_chars_before_line:chars, button_index:idx, total_buttons:btns.length};
            }""", args.after_chars)
            print("PAYWALL_PLACED=" + json.dumps(placed, ensure_ascii=False))
            if not placed.get("ok"):
                raise SystemExit(f"FATAL: could not place the paywall line: {placed}")
            time.sleep(3)

            # Show where the line actually landed, in the article's own words, BEFORE anything is
            # published. This is the only chance to see the boundary — see module docstring for why
            # there is no "set it now, inspect it later" for this transient form state.
            around = pg.evaluate("""()=>{
                const marker=[...document.querySelectorAll('*')].find(e=>(e.textContent||'').trim()==='このラインより先を有料にする'&&e.children.length===0);
                if(!marker) return null;
                const txt=[]; const walk=document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                let n, hit=false, before=[], after=[];
                while(n=walk.nextNode()){
                  if(n===marker){hit=true; continue;}
                  if(n.children.length===0 && n.textContent){
                    const t=n.textContent.trim();
                    if(!t||t==='ラインをこの場所に変更'||t==='このラインより先を有料にする') continue;
                    (hit?after:before).push(t);
                  }
                }
                return {free_tail: before.slice(-3).join(' / ').slice(-260), paid_head: after.slice(0,3).join(' / ').slice(0,260)};
            }""")
            if around:
                print("FREE_ENDS_WITH: " + around["free_tail"])
                print("PAID_STARTS_WITH: " + around["paid_head"])

        if not args.arm:
            print("GUARD_STOPPED=true (no --arm; 投稿する was never clicked, nothing changed on note)")
            return 0

        # 4) publish — the only irreversible step, and the only place this file clicks
        # 投稿する/更新する. Guarded exactly like publish.py for both free and paid articles.
        # 投稿する appears on a draft and 更新する once the article is already public.
        assert_publish_allowed()
        publish_label = pg.evaluate("""()=>{const el=[...document.querySelectorAll('button,a,div[role=button],span')]
            .find(b=>['投稿する','更新する'].includes((b.textContent||'').trim())&&b.offsetParent!==null);
            if(el){el.scrollIntoView({block:'center'}); return (el.textContent||'').trim();} return '';}""")
        posted = click_publish_button(pg, publish_label) if publish_label else ""
        print(f"POSTED_CLICKED={posted or False}")
        if not posted:
            raise SystemExit("FATAL: neither 投稿する nor 更新する found in the publish settings")
        time.sleep(9)
        # This is diagnostic evidence only.  The publish click is already
        # irreversible, so a redirected/closed editor must never prevent the
        # authoritative API readback and reconciliation below.
        try:
            pg.screenshot(path=WORK + f"/{article_type}-published-{args.key}.png", full_page=False)
        except Exception as error:
            print(f"POST_CLICK_SCREENSHOT_UNAVAILABLE={type(error).__name__}", file=sys.stderr)
    finally:
        if mutation_trace is not None:
            mutation_trace.write()
        if pg is not None:
            try:
                pg.close()
            except Exception:
                pass
        if isolated_context is not None:
            try:
                isolated_context.close()
            except Exception:
                pass

    # 5) Ask note, not the editor. Only the API proves which article type is live.
    return verify_note_publication(args, managed_publication)


if __name__ == "__main__":
    sys.exit(main())
