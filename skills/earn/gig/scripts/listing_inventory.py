#!/usr/bin/env python3
"""listing_inventory.py -- gig-loop spec Task C1 (docs/loop-engineering/26-...md SS CC').

Ground truth for "what is this account currently selling", read from the live
storefront -- not from shuppin.jsonl, which listing_ledger.py already documents as
drift-prone (the 2026-07-27 incident: three callers derived three different listing
counts from the same file). This module's own first real run found the same shape of
drift again: listing_ledger.published_listings said 16 ids were ever marked
shuppin_published; the live page showed 11 (10 after C1/C2 archived one). It never
subtracted a listing that was later archived (Coconala route /services/archive/<id>,
used by B0's capacity-recovery path), so a slot that was recycled counted forever --
fixed in C3 (2026-08-09), which taught listing_ledger.count() to net shuppin_archived
ids out of published_listings. The live page is still the only place that answers "does
this listing exist right now" for the other half of drift this cross-reference exists to
catch: ids the ledger never recorded a shuppin_published for at all.

Capability-fit judgment is deliberately NOT made here. Per building-agents, judgement
belongs to the model, not a hardcoded rule -- this collector's job is only to hand the
model a grounded inventory instead of letting it guess or re-scrape. collect() writes a
context file (listing-fit-context.json) with the exact prompt text a schema-bound owner
may use. Any optional model invocation belongs to the direct loop that needs the verdict.

Usage:
  listing_inventory.py collect --out ~/gig/listings-inventory.json
  listing_inventory.py collect --out - --offline      # ledger-only, skip the browser
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

STATE_DIR = Path(os.environ.get("GIG_STATE_DIR", str(Path.home() / "gig")))
LISTINGS_URL = "https://coconala.com/mypage/services_lists"
CARD_DELIMITER = "編集する 公開設定 シェア"
LIST_HEADER = "出品サービス一覧"
SERVICE_SCOPE_END = "ココナラの安心保証"
MAX_PAGES = 10  # bounded: the platform's own listing cap is 20, ~10 cards/page
IDENTITY = "coconala:kosuke"
BROWSER_GUARD = os.path.expanduser("~/.config/ai/bin/browser-guard.sh")

# The account's own documented no-go list (domain-skills/coconala.md, "## 応募":
# "実写動画の編集は請けない -- 過去に1評価. 判断根拠は application_planner.py の
# prompt が正本"). Handed to the fit judge verbatim so it does not have to rediscover
# a lesson this account already paid for once.
KNOWN_NO_GO = [
    "実写動画の編集（実写ショート動画編集など）: 過去に受注し★1評価を受けた。"
    "domain-skills/coconala.md '## 応募' と application_planner.py の prompt が正本。",
]


# --- deterministic parsing (unit-testable without a live browser) -------------------

def extract_own_service_ids(hrefs: list[str]) -> list[str]:
    """The public /services/<id> link is the first of 3 per card; keeps DOM order."""
    out: list[str] = []
    for href in hrefs:
        m = re.fullmatch(r"/services/(\d+)", href)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def parse_list_page(rendered_text: str, service_ids: list[str]) -> list[dict]:
    """Split one services_lists page's innerText into one dict per card.

    Card order in the rendered text matches href order (both are document order): the
    first card on this account's real page priced 5,000 and titled the PowerPoint
    listing, matching service_id 94000015's own known ledger title on the same probe
    this parser was written against (see tests/fixtures/services_lists_page1.json).
    """
    body = rendered_text.split(LIST_HEADER, 1)
    body = body[1] if len(body) == 2 else rendered_text
    chunks = body.split(CARD_DELIMITER)[:-1]  # trailing chunk is pagination, not a card
    skip_tokens = {"公開中", "受付中", "受付休止中", "非公開", "下書き", "定期購入可"}
    cards = []
    for sid, chunk in zip(service_ids, chunks):
        status_m = re.search(r"(受付休止中|受付中|公開中|非公開|下書き)", chunk)
        price_m = re.search(r"([\d,]+)\s*\n\s*円", chunk)
        title = ""
        for ln in (line.strip() for line in chunk.splitlines()):
            if not ln or ln in skip_tokens or ln == "円" or re.fullmatch(r"[\d,]+", ln):
                continue
            title = ln
            break
        cards.append({
            "service_id": sid,
            "title": title,
            "price_jpy": int(price_m.group(1).replace(",", "")) if price_m else None,
            "state": status_m.group(1) if status_m else None,
        })
    return cards


def parse_category_breadcrumb(rendered_text: str) -> str | None:
    """The public /services/<id> page opens 'ホーム\\n<cat>\\n...\\n<title>\\n<catch>\\n評価'.

    Breadcrumb depth varies (2-3 levels), so the boundary is found from the other end:
    the two lines directly before '評価' are always the listing's own title + catchphrase
    (Coconala requires both), so everything between 'ホーム' and those two lines is the
    breadcrumb, whatever its depth.
    """
    lines = [ln.strip() for ln in rendered_text.splitlines() if ln.strip()]
    try:
        home_idx = lines.index("ホーム")
        rating_idx = next(i for i, ln in enumerate(lines) if ln.startswith("評価"))
    except (ValueError, StopIteration):
        return None
    crumbs = lines[home_idx + 1: max(home_idx + 1, rating_idx - 2)]
    return "/".join(crumbs) if crumbs else None


def parse_sales_count(rendered_text: str) -> int | None:
    """Parse the public page's sales count without turning unknown into zero."""
    match = re.search(r"販売実績\s*([0-9０-９][0-9０-９,，]*)\s*件", rendered_text or "")
    return int(match.group(1).replace(",", "").replace("，", "")) if match else None


def extract_public_service_scope(rendered_text: str) -> str | None:
    """Keep only this service's official page section, never recommendations/profile."""
    if rendered_text.count(SERVICE_SCOPE_END) != 1:
        return None
    scope = rendered_text.split(SERVICE_SCOPE_END, 1)[0].strip()
    return scope or None


def write_storefront_observation(
    inventory: list[dict],
    *,
    output_path: Path | str | None = None,
    observed_at: str | None = None,
) -> dict:
    services = []
    for row in inventory:
        service_id = str(row.get("service_id") or "").strip()
        if not service_id.isdigit():
            continue
        services.append({
            "service_id": service_id,
            "state": row.get("state") if isinstance(row.get("state"), str) else None,
            "price_jpy": row.get("price_jpy") if type(row.get("price_jpy")) is int and row["price_jpy"] >= 0 else None,
            "sales_count": row.get("sales_count") if type(row.get("sales_count")) is int and row["sales_count"] >= 0 else None,
        })
    services.sort(key=lambda row: row["service_id"])
    canonical = json.dumps(services, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload = {
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "services": services,
        "live_listings_count": sum(row["state"] == "公開中" for row in services),
        "service_count": len(services),
        "content_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    destination = Path(output_path or STATE_DIR / "storefront-observation.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
    return payload


def new_cards_only(cards: list[dict], seen_ids: set[str]) -> list[dict]:
    """Cards from one page whose service_id was not already collected on a prior page.

    Needed because Coconala clamps /page:N past the real last page back onto the last
    page's cards instead of 404ing (observed 2026-08-09: page:3 repeated page:2's single
    card) -- "this page has ids" cannot terminate the pager, only "this page added
    nothing new" can.
    """
    return [c for c in cards if c["service_id"] not in seen_ids]


def cross_reference_ledger(live_ids: list[str]) -> dict:
    """Where the live page and the self-reported ledger disagree, and by how much."""
    import listing_ledger

    ledger_path = STATE_DIR / "shuppin.jsonl"
    counts = listing_ledger.count_ledger(ledger_path)
    # Same set listing_ledger.count() derives internally (published minus archived) --
    # taken from the shared helper instead of re-scanning load_events() here, so this
    # module cannot drift from the ledger's own taxonomy the way it did before C3 (that
    # re-derivation is exactly why an archived id never stopped showing up here either).
    published_ever = set(listing_ledger.published_ids(ledger_path))
    live = set(live_ids)
    return {
        "ledger_published_listings_count": counts.published_listings,
        "live_listings_count": len(live),
        "ledger_claims_published_but_not_live_now": sorted(published_ever - live),
        "live_now_but_ledger_never_recorded_shuppin_published": sorted(live - published_ever),
    }


def build_fit_judgment_context(inventory: list[dict]) -> dict:
    """The deterministic input a model-judgement step (owned by C2) would consume.

    Not invoked here -- see module docstring. This is the "TODO judgment file" left
    for C2, which needs the fit verdict to decide what to delist.
    """
    return {
        "task": (
            "For each listing below, judge whether this account can actually execute "
            "an order of that kind end-to-end (fit=executable), cannot (fit=not_executable), "
            "or there is not enough evidence to say (fit=uncertain). Ground the judgement in "
            "this account's real delivered project history (~/gig/projects/*/artifacts, "
            "domain-skills/coconala.md), not in the listing copy's own claims."
        ),
        "known_no_go": KNOWN_NO_GO,
        "listings": inventory,
        "output_schema": {
            "results": [{"service_id": "string", "fit": "executable|not_executable|uncertain", "reason": "string"}]
        },
    }


# --- live browser collection ---------------------------------------------------------

async def _call(ws, method: str, params: dict, cid: int) -> dict:
    await ws.send(json.dumps({"id": cid, "method": method, "params": params}))
    while True:
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
        if response.get("id") == cid:
            return response


async def _wait_for_load(ws, deadline: float, start_cid: int) -> tuple[bool, int]:
    cid = start_cid
    loop = asyncio.get_event_loop()
    while loop.time() < deadline:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=min(1.0, remaining)))
        except (asyncio.TimeoutError, json.JSONDecodeError):
            continue
        if response.get("method") == "Page.loadEventFired":
            return True, cid
    while loop.time() < deadline:
        response = await _call(
            ws, "Runtime.evaluate",
            {"expression": "document.readyState", "returnByValue": True}, cid,
        )
        cid += 1
        if response.get("result", {}).get("result", {}).get("value") == "complete":
            return True, cid
        await asyncio.sleep(0.5)
    return False, cid

async def _cdp_connect(ws_url: str):
    """Open one CDP socket, retrying the browser's refusal to upgrade.

    Four lanes share this browser, and under that contention the endpoint answers the
    websocket upgrade with HTTP 500 or drops the connection before the first frame. It is
    the browser declining to talk right now, not an answer about the page, so it is retried
    like every other transient read in this loop -- five attempts three seconds apart. A
    refusal that survives all five is raised unchanged, so the wake still reports what it
    saw. See skills/loop-engineering/references/transient-vs-fatal.md.
    """
    import websockets

    # The window is deliberately wider than the loop's usual five-by-three. CDP allows one
    # websocket per target, so a refusal usually means a sibling lane is holding the page this
    # wake wants, and a sibling's single browser step can take as long as the thirty-second tab
    # timeout that bounds it. Twelve seconds of waiting expired inside one sibling step and
    # four wakes in five were lost to it. Forty seconds outlasts one such step.
    #
    # This is a mitigation, not the cure. The cure is one browser context per lane, which the
    # spec already requires (6.2A PAR-1) and which this loop does not yet have.
    last: Exception | None = None
    for attempt in range(8):
        try:
            return await websockets.connect(
                ws_url, ping_interval=None, open_timeout=10, max_size=40 * 1024 * 1024)
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as error:
            last = error
            if attempt < 7:
                await asyncio.sleep(5)
    raise last


async def _eval_json(ws_url: str, url: str, expression: str) -> dict:
    async with await _cdp_connect(ws_url) as ws:
        cid = 1
        await _call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": url}})); cid += 1
        deadline = asyncio.get_event_loop().time() + 15
        await _wait_for_load(ws, deadline, cid)
        r = await _call(ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                  "awaitPromise": True}, cid)
        raw = r.get("result", {}).get("result", {}).get("value", "") or ""
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}


# The tabs observed on the most recent list page. A listing left every page of this list while
# still rendering to a buyer, and whichever tab holds it is the answer.
LAST_PAGE_TABS: list[dict] = []
# What each page of the seller list returned on the most recent walk.
PAGE_WALK: list[dict] = []
# Raw hrefs of every card on the most recent page read, before any filtering.
LAST_RAW_HREFS: list[str] = []


async def _fetch_list_page(
    cdp_base: str | None, page: int, *, ws_url: str | None = None,
) -> list[dict]:
    url = LISTINGS_URL if page == 1 else f"{LISTINGS_URL}/page:{page}"
    expression = (
        "JSON.stringify({cards:[...document.querySelectorAll('#serviceListContent "
        ".serviceListContentBox')].map(card=>({"
        "href:card.querySelector('a[href^=\"/services/\"]')?.getAttribute('href')||'',"
        "state_controls:[...card.querySelectorAll('a,button,[role=button],[role=menuitem],"
        "input[type=checkbox],select')]"
        ".slice(0,20).map(e=>({tag:e.tagName,type:e.type||null,"
        "label:((e.innerText||e.getAttribute('aria-label')||e.getAttribute('title')||'')+'').trim().slice(0,32),"
        "href:e.getAttribute('href')||null,id:e.id||null,cls:((e.className||'')+'').slice(0,60),"
        "context:/js_change-open-status/.test((e.className||'')+'')?(()=>{let n=e,best='';"
        "for(let i=0;i<5&&n;i++){const t=((n.innerText||'')+'').trim();"
        "if(t.length>best.length&&t.length<=400)best=t;n=n.parentElement}return best})():null})),"
        "text:card.innerText||''})),"
        # Every card is returned with its raw href. A filter demanding an exact /services/<id>
        # dropped cards silently, and a listing that is live for buyers went missing from the
        # catalogue for hours with no way to see why.
        "raw_hrefs:[...document.querySelectorAll('#serviceListContent .serviceListContentBox')]"
        ".map(card=>card.querySelector('a[href*=\"/services/\"]')?.getAttribute('href')||'')"
        ".slice(0,40),"
        # A listing vanished from every page of this list while still rendering to a buyer, so
        # the list's own tabs are recorded: whichever one holds it is the answer.
        "tabs:[...document.querySelectorAll('a,button,[role=tab]')]"
        ".map(e=>({label:((e.innerText||'')+'').trim().slice(0,24),"
        "href:e.getAttribute('href')||null}))"
        ".filter(e=>e.label&&(/services_lists|status|public|open|draft|stop/i.test(e.href||'')"
        "||/公開|下書き|停止|非公開|審査/.test(e.label))).slice(0,20)})"
    )
    # This list renders its cards progressively, so a single read catches whatever exists at
    # that instant: one walk returned 3, 8, 2 and 0 cards across four pages and missed the
    # fourteenth listing entirely, which then looked like it had left the catalogue. The page is
    # read until its card count stops growing, and the largest reading is the one kept.
    if ws_url is not None:
        best: list[dict] = []
        stable = 0
        data: dict = {}
        for _ in range(8):
            data = await _eval_json(ws_url, url, expression)
            cards = data.get("cards") or []
            if len(cards) > len(best):
                best, stable = cards, 0
            else:
                stable += 1
                if stable >= 2:
                    break
            await asyncio.sleep(1.0)
        LAST_PAGE_TABS[:] = data.get("tabs") or []
        LAST_RAW_HREFS[:] = data.get("raw_hrefs") or []
        return best
    if cdp_base is None:
        raise ValueError("cdp_base_required_without_ws")
    from cdp_nav_snapshot import hidden_page_target

    os.environ["CLOAK_CDP_BASE_URL"] = cdp_base
    async with hidden_page_target(url) as ws_url:
        data = await _eval_json(ws_url, url, expression)
    LAST_PAGE_TABS[:] = data.get("tabs") or []
    return data.get("cards", [])


async def _fetch_category(
    cdp_base: str | None, service_id: str, *, ws_url: str | None = None,
) -> dict:
    url = f"https://coconala.com/services/{service_id}"
    if ws_url is not None:
        text = ""
        scope = ""
        for attempt in range(3):
            data = await _eval_json(
                ws_url,
                url,
                "JSON.stringify({text:document.body ? document.body.innerText.slice(0,120000) : ''})",
            )
            text = str(data.get("text") or "")
            scope = extract_public_service_scope(text) or ""
            if scope:
                break
            if attempt < 2:
                await asyncio.sleep(0.75)
        return {
            "category": parse_category_breadcrumb(text), "sales_count": parse_sales_count(text),
            "public_url": url, "public_text": scope,
            "public_content_sha256": hashlib.sha256(scope.encode()).hexdigest(),
        }
    if cdp_base is None:
        raise ValueError("cdp_base_required_without_ws")
    from cdp_nav_snapshot import hidden_page_target

    os.environ["CLOAK_CDP_BASE_URL"] = cdp_base
    async with hidden_page_target(url) as ws_url:
        import websockets
        async with websockets.connect(ws_url, ping_interval=None, open_timeout=10, max_size=40 * 1024 * 1024) as ws:
            cid = 1
            await _call(ws, "Page.enable", {}, cid); cid += 1
            await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": url}})); cid += 1
            deadline = asyncio.get_event_loop().time() + 15
            await _wait_for_load(ws, deadline, cid)
            r = await _call(ws, "Runtime.evaluate", {
                "expression": "document.body ? document.body.innerText.slice(0,3000) : ''",
                "returnByValue": True,
            }, cid)
            text = r.get("result", {}).get("result", {}).get("value", "") or ""
    scope = extract_public_service_scope(text) or ""
    return {
        "category": parse_category_breadcrumb(text), "sales_count": parse_sales_count(text),
        "public_url": url, "public_text": scope,
        "public_content_sha256": hashlib.sha256(scope.encode()).hexdigest(),
    }


async def collect_live(identity: str = IDENTITY, *, ws_url: str | None = None) -> list[dict]:
    if ws_url is not None and not ws_url.startswith("ws://"):
        raise ValueError("leased_ws_url_invalid")
    cdp_base = None if ws_url is not None else subprocess.run(
        [BROWSER_GUARD, "acquire", identity], capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        inventory: list[dict] = []
        seen_ids: set[str] = set()
        PAGE_WALK.clear()
        for page in range(1, MAX_PAGES + 1):
            live_cards = await _fetch_list_page(cdp_base, page, ws_url=ws_url)
            cards = []
            for live_card in live_cards:
                match = re.fullmatch(r"/services/(\d+)", str(live_card.get("href") or ""))
                if match:
                    parsed = parse_list_page(
                        str(live_card.get("text") or "") + CARD_DELIMITER,
                        [match.group(1)],
                    )
                    for row in parsed:
                        # Observed only. The listing-state adapter binds the real control from here.
                        row["state_controls"] = live_card.get("state_controls") or []
                    cards.extend(parsed)
            new_cards = new_cards_only(cards, seen_ids)
            # Twice today a page was declared empty and the conclusion drawn from its absence
            # was wrong, so each page records what it actually returned before the walk stops.
            PAGE_WALK[:] = PAGE_WALK + [{
                "page": page, "url": LISTINGS_URL if page == 1 else f"{LISTINGS_URL}/page:{page}",
                "observed_ids": [str(row.get("service_id") or "") for row in cards],
                "new_ids": [str(row.get("service_id") or "") for row in new_cards],
                "raw_hrefs": list(LAST_RAW_HREFS),
            }]
            if not new_cards:
                break
            seen_ids.update(c["service_id"] for c in new_cards)
            inventory.extend(new_cards)
        for card in inventory:
            card.update(await _fetch_category(cdp_base, card["service_id"], ws_url=ws_url))
        return inventory
    finally:
        if ws_url is None:
            subprocess.run([BROWSER_GUARD, "release", identity], capture_output=True, text=True)


def observe_storefront(
    *, output_path: Path | str | None = None, ws_url: str | None = None,
    include_contract_sources: bool = False,
) -> dict:
    rows = asyncio.run(collect_live(ws_url=ws_url))
    payload = write_storefront_observation(rows, output_path=output_path)
    if include_contract_sources:
        payload["_contract_sources"] = rows
    return payload


# --- CLI -------------------------------------------------------------------------------

def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    collect_p = sub.add_parser("collect")
    collect_p.add_argument("--out", required=True, help="output path, or - for stdout")
    collect_p.add_argument("--offline", action="store_true", help="skip the live browser read")
    args = parser.parse_args(argv)

    if args.offline:
        inventory: list[dict] = []
    else:
        inventory = asyncio.run(collect_live())

    result = {
        "inventory": inventory,
        "ledger_cross_reference": cross_reference_ledger([c["service_id"] for c in inventory]),
        "fit_judgment": {
            "status": "not_wired",
            "reason": (
                "Capability-fit is model judgement (C1 scope is the deterministic inventory "
                "only). fit_judgment_context_path below has the exact prompt/schema for the "
                "owning direct loop may add a schema-bound judgment when needed."
            ),
        },
    }
    out_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out == "-":
        print(out_text)
    else:
        out_path = Path(args.out)
        out_path.write_text(out_text + "\n", encoding="utf-8")
        context_path = out_path.with_name("listing-fit-context.json")
        context_path.write_text(
            json.dumps(build_fit_judgment_context(inventory), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result["fit_judgment"]["context_path"] = str(context_path)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
