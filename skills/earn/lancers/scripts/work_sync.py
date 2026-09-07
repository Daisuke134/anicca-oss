#!/usr/bin/env python3
"""Bounded, read-only canonical Lancers sales-source observer."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode, urlsplit

HERE = Path(__file__).resolve().parent
SKILLS_ROOT = HERE.parents[2]
REPO_ROOT = SKILLS_ROOT.parent
AGENT_RUNNER = REPO_ROOT / "runtime" / "agent-runner" / "agent_runner.py"
REPLY_SCHEMA = SKILLS_ROOT / "gig-work" / "schemas" / "reply_composition.schema.json"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("application_tick_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


application_tick = _load("anicca_lancers_work_sync_tick", HERE / "application_tick.py")
CDP_URL, DEFAULT_STATE_PATH = application_tick.CDP_URL, application_tick.DEFAULT_STATE_PATH
MAX_BOARD_PAGES = MAX_MESSAGE_PAGES = 20
TICK_TIMEOUT_SECONDS = 120


class SourceFailure(RuntimeError): pass


class ReplySemanticUncertain(SourceFailure):
    def __init__(self, remaining_work: Sequence[str]):
        self.remaining_work = [str(item).strip() for item in remaining_work if str(item).strip()]
        super().__init__("reply_semantic_uncertain")


def _id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise SourceFailure("provider_response_invalid")
    return str(value).strip()


def _digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        raise SourceFailure("provider_response_invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def _write_state(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True); path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
        os.replace(temporary, path); path.chmod(0o600)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _proposal_context(page: Any, detail: Mapping[str, Any], verified_proposals: set[str]) -> Optional[Mapping[str, Any]]:
    with_value = detail.get("with")
    if not isinstance(with_value, Mapping) or not isinstance(with_value.get("proposal"), Mapping): return None
    proposal_id = _id(with_value["proposal"].get("id"))
    if proposal_id not in verified_proposals: raise SourceFailure("proposal_receipt_unverified")
    page.goto(f"https://www.lancers.jp/work/proposal/{quote(proposal_id, safe='')}", wait_until="domcontentloaded", timeout=20_000)
    value = page.evaluate("""() => { const terms={}; for (const node of document.querySelectorAll("dt")) terms[node.innerText.trim()]=node.nextElementSibling?.innerText?.trim(); const label=[...document.querySelectorAll("em")].find(node=>node.innerText.trim()==="提案文 :"); return {path:location.pathname, amount:terms["契約金額 (税抜) :"], due:terms["予定納期 :"], project_id:terms["依頼番号:"], proposal_text:label?.parentElement?.nextElementSibling?.innerText?.trim()}; }""")
    if not isinstance(value, Mapping) or value.get("path") != f"/work/proposal/{proposal_id}": raise SourceFailure("proposal_terms_unavailable")
    amount_text, due_text, text = value.get("amount"), value.get("due"), value.get("proposal_text")
    amount_digits = "".join(character for character in str(amount_text) if character.isdigit())
    due = str(due_text).replace("/", "-")
    if not amount_digits or int(amount_digits) <= 0 or len(due) != 10 or not isinstance(text, str) or not text.strip() or len(text) > 10000: raise SourceFailure("proposal_terms_unavailable")
    job = with_value.get("job"); expected_project = _id(job.get("id")) if isinstance(job, Mapping) and job.get("id") is not None else None
    if expected_project is not None and str(value.get("project_id")) != expected_project: raise SourceFailure("proposal_terms_conflict")
    return {"proposal_id": proposal_id, "project_id": value.get("project_id"), "price_jpy": int(amount_digits), "delivery_due_on": due, "proposal_text": text.strip()}


def _compose_reply(board: Mapping[str, Any], messages: Sequence[Mapping[str, Any]], state_path: Path, grounding: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    conversation = []
    for row in sorted(messages, key=lambda item: int(_id(item.get("id"))))[-20:]:
        body = row.get("description")
        if not isinstance(body, str) or not body.strip() or len(body) > 10000: raise SourceFailure("provider_response_invalid")
        conversation.append({"side": "buyer" if row.get("is_required_reply") is True else "seller", "body": body[:4000]})
    prompt = """Lancersの購入前会話を判断する。Coconalaで実運用するsingle semantic judgementを使う。\n必須:\n- next_actionは、相手へ今返す必要がある時だけreply、感謝・了解・seller返答待ちなど返信不要ならwait、明確な辞退・連絡停止ならstop。迷いがあればuncertaintyへ書き、返信本文を作らない。\n- replyでは最新buyer発言の質問・依頼へ冒頭から直接答え、明示された質問を省略しない。\n- verified_proposalがある応募threadでは、その価格・納期・提案本文だけを応募条件の正本にする。canonical_productは関連するstorefront相談だけに使い、応募条件と混ぜない。\n- 会話とgroundingにある検証済み事実だけを使う。価格、納期、実績、対応能力を作らない。必要な未確定事項だけ質問を1つまで含める。\n- Lancers月額報酬はclientがscope、volume、frequency、conditions、初月と翌月以降の金額を合意後、仮払い付き公式offerを送り、sellerが承諾して始まる。条件が具体的に合意済みでbuyerが購入意思を示した時だけ、clientへ公式offer送付を依頼する。sellerがofferを送れると書かない。\n- 最新発言への回答だけで完結するならCTA、見積り、納期、質問を自発的に追加しない。購入を催促しない。\n- 必須能力外なら、代替提案や質問を付けず正直かつ丁寧に辞退する。単語が出ただけ、任意作業、難しさ、低予算、実績不足では辞退しない。\n- 外部連絡先、内部用語、未依頼の成果物を含めない。reply_bodyは1000文字以内。wait/stopではreply_bodyをnullにする。\nCONTEXT:\n""" + json.dumps({"board": {"title": board.get("title"), "description": board.get("description")}, "conversation": conversation, "grounding": grounding or {}}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with tempfile.TemporaryDirectory(prefix=".lancers-reply-", dir=state_path.parent) as temporary:
        evidence = Path(temporary) / "evidence"
        completed = subprocess.run([sys.executable, str(AGENT_RUNNER), "--task-class", "composition-agent", "--prompt-stdin", "--schema", str(REPLY_SCHEMA), "--evidence-dir", str(evidence), "--task-label", "lancers-sales-reply", "--loop", "lancers-sales", "--workdir", str(SKILLS_ROOT.parent)], input=prompt, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)
        if completed.returncode != 0: raise SourceFailure("reply_composer_failed")
        try:
            summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8")); result_path = Path(str(summary["result_path"])).resolve(); result_path.relative_to(evidence.resolve())
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, KeyError, TypeError, ValueError): raise SourceFailure("reply_composer_failed") from None
    if not isinstance(result, Mapping) or result.get("next_action") not in {"reply", "wait", "stop"} or not isinstance(result.get("uncertainty"), list): raise SourceFailure("reply_contract_invalid")
    if result["uncertainty"]: raise ReplySemanticUncertain(result["uncertainty"])
    body = result.get("reply_body")
    if result["next_action"] != "reply":
        if body is not None: raise SourceFailure("reply_contract_invalid")
        return None
    if not isinstance(body, str) or not body.strip() or len(body.strip()) > 1000: raise SourceFailure("reply_contract_invalid")
    return body.strip()


def _verified_proposals(state_path: Path) -> set[str]:
    ledger = Path(state_path).with_name("marketplace-ledger.sqlite3")
    if not ledger.is_file(): raise SourceFailure("application_receipts_unavailable")
    connection = None
    try:
        connection = sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)
        rows = connection.execute("SELECT external_id FROM marketplace_events WHERE platform=? AND event_type=?", ("lancers", "application_verified")).fetchall()
        values = [_id(row[0]) for row in rows]
    except (IndexError, TypeError, SourceFailure, sqlite3.Error, OSError):
        raise SourceFailure("application_receipts_unavailable") from None
    finally:
        if connection is not None:
            try: connection.close()
            except sqlite3.Error: pass
    if len(values) != len(set(values)): raise SourceFailure("application_receipts_conflict")
    return set(values)


def _fetch(page: Any, path: str) -> Any:
    value = page.evaluate(
        """async (path) => { const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 20000); try { const response = await fetch(path, {method: "GET", credentials: "same-origin", signal: controller.signal}); const text = await response.text(); return response.ok && text.length <= 1048576 ? {ok: true, body: JSON.parse(text)} : {ok: false}; } catch (_) { return {ok: false}; } finally { clearTimeout(timer); } }""",
        path,
    )
    if not isinstance(value, Mapping) or value.get("ok") is not True:
        raise SourceFailure("provider_response_invalid")
    return value.get("body")


def _pages(fetch: Callable[[str], Any], route: Callable[[Optional[str]], str], check: Callable[[Mapping[str, Any]], tuple[str, str]], maximum: int, errors: tuple[str, str, str]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []; seen: set[str] = set(); cursor: Optional[str] = None
    for _ in range(maximum):
        page = fetch(route(cursor))
        if not isinstance(page, list) or not all(isinstance(row, Mapping) for row in page): raise SourceFailure("provider_response_invalid")
        if not page: return rows
        for row in page:
            identity, _ = check(row)
            if identity in seen: raise SourceFailure(errors[0])
            seen.add(identity); rows.append(row)
        if len(page) < 20: return rows
        next_cursor = check(page[-1])[1]
        if next_cursor == cursor: raise SourceFailure(errors[1])
        cursor = next_cursor
    raise SourceFailure(errors[2])


def _message_rows(fetch: Callable[[str], Any], board_id: str) -> list[Mapping[str, Any]]:
    def check(message: Mapping[str, Any]) -> tuple[str, str]:
        message_id = _id(message.get("id"))
        if _id(message.get("board_id")) != board_id or not isinstance(message.get("is_required_reply"), bool) or "send_user" not in message or "modified" not in message: raise SourceFailure("provider_response_invalid")
        _id(message["modified"]); return message_id, message_id
    route = lambda cursor: f"/v1/message_api/boards/{quote(board_id, safe='')}/messages?{urlencode({'limit': 20, **({'message_id': cursor, 'direction': 'prev'} if cursor else {})})}"
    return _pages(fetch, route, check, MAX_MESSAGE_PAGES, ("duplicate_message_id", "message_cursor_stalled", "message_page_limit_reached"))


def _messages(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return sorted(({"message_id": _id(row["id"]), "content_sha256": _digest(row)} for row in rows), key=lambda row: row["message_id"])


def _snapshot(fetch: Callable[[str], Any], verified_proposals: set[str], private_boards: Optional[list[Any]] = None) -> dict[str, Any]:
    def check(board: Mapping[str, Any]) -> tuple[str, str]:
        board_id = _id(board.get("id")); unread = board.get("unread_count")
        if not isinstance(board.get("is_required_reply"), bool) or isinstance(unread, bool) or not isinstance(unread, int) or unread < 0 or "modified" not in board: raise SourceFailure("provider_response_invalid")
        return board_id, _id(board["modified"])
    boards = _pages(fetch, lambda cursor: f"/v1/message_api/boards/?{urlencode({'limit': 20, **({'modified': cursor} if cursor else {})})}", check, MAX_BOARD_PAGES, ("duplicate_board_id", "board_cursor_stalled", "board_page_limit_reached"))

    output, storefront_contracts, replies, unread, applications = [], [], 0, 0, 0
    for board in boards:
        board_id = _id(board["id"])
        detail = fetch(f"/v1/message_api/boards/{quote(board_id, safe='')}")
        if not isinstance(detail, Mapping) or _id(detail.get("id")) != board_id:
            raise SourceFailure("provider_response_invalid")
        with_value = detail.get("with", {})
        if not isinstance(with_value, Mapping):
            raise SourceFailure("provider_response_invalid")
        relations = []
        for key in ("proposal", "job", "serviceItemContract"):
            related = with_value.get(key)
            if related is not None and not isinstance(related, Mapping): raise SourceFailure("provider_response_invalid")
            relations.append(None if related is None or "id" not in related else _id(related["id"]))
        proposal, job, contract = relations
        raw_messages = _message_rows(fetch, board_id); messages = _messages(raw_messages)
        if private_boards is not None: private_boards.append((board, detail, raw_messages))
        replies += int(board["is_required_reply"]); unread += board["unread_count"]
        applications += int(proposal in verified_proposals)
        if contract is not None:
            storefront_contracts.append({"source_kind": "storefront", "provider_id": contract, "board_id": board_id, "detail_path": f"/v1/message_api/boards/{board_id}", "funding_status": "requires_detail_readback"})
        source_refs = [
            {"source_kind": kind, "provider_id": provider_id}
            for kind, provider_id in (("proposal", proposal), ("job", job), ("storefront_contract", contract))
            if provider_id is not None
        ]
        origin = "application" if proposal in verified_proposals else "storefront" if contract is not None else "inquiry"
        buyer_last_message_id = max((row["message_id"] for row in messages), key=int) if board["is_required_reply"] and messages else None
        output.append({
            "board_id": board_id, "origin": origin, "source_refs": source_refs,
            "buyer_last_message_id": buyer_last_message_id,
            "content_sha256": _digest({"board": board, "detail": detail}),
            "message_ids": [row["message_id"] for row in messages], "messages": messages,
        })
    return {"ok": True, "logged_in": True, "source_complete": True, "board_count": len(boards), "required_reply_count": replies, "unread_count": unread, "application_board_count": applications, "storefront_contract_candidate_count": len(storefront_contracts), "storefront_contract_candidates": storefront_contracts, "boards": sorted(output, key=lambda row: row["board_id"])}


def _contract_sources(page: Any) -> dict[str, Any]:
    def visit(path: str) -> None:
        page.goto(f"https://www.lancers.jp{path}", wait_until="domcontentloaded", timeout=20_000)
        parsed = urlsplit(str(page.url))
        if (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment) != ("https", "www.lancers.jp", path, "", ""):
            raise SourceFailure("contract_source_unavailable")

    visit("/monthly_work_contracts/lancer/offers")
    offers = page.evaluate("""() => ({empty: document.body.innerText.includes("申請されたオファーはありません"), hrefs: [...document.querySelectorAll('a[href^="/monthly_work_contracts/lancer/offers/"]')].map(a => a.getAttribute('href'))})""")
    if not isinstance(offers, Mapping) or not isinstance(offers.get("empty"), bool) or not isinstance(offers.get("hrefs"), list):
        raise SourceFailure("monthly_offer_source_unavailable")
    raw_offer_ids = []
    for href in offers["hrefs"]:
        match = re.fullmatch(r"/monthly_work_contracts/lancer/offers/([0-9]+)", str(href or ""))
        if match is None: raise SourceFailure("monthly_offer_source_unavailable")
        raw_offer_ids.append(match.group(1))
    offer_ids = list(dict.fromkeys(raw_offer_ids))
    if bool(offer_ids) == offers["empty"]:
        raise SourceFailure("monthly_offer_source_conflict")

    visit("/mypage/proposals/all/working")
    projects = page.evaluate("""() => [...document.querySelectorAll("li.p-mypage-work__media.c-media-job")].map(card => ({href: card.querySelector('a.c-link.c-link--black')?.getAttribute('href'), status: card.querySelector('.c-media-job__status--active')?.innerText?.trim()}))""")
    if not isinstance(projects, list) or not all(isinstance(row, Mapping) for row in projects):
        raise SourceFailure("contract_source_unavailable")
    project_ids = []
    for row in projects:
        match = re.fullmatch(r"/work/detail/([0-9]+)", str(row.get("href") or ""))
        if match is None or row.get("status") != "進行中":
            raise SourceFailure("contract_source_unavailable")
        project_ids.append(match.group(1))
    if len(project_ids) != len(set(project_ids)):
        raise SourceFailure("contract_source_conflict")

    visit("/monthly_work_contracts/lancer")
    monthly = page.evaluate("""() => ({empty: document.body.innerText.includes("申請された契約はありません"), hrefs: [...document.querySelectorAll('a[href^="/monthly_work_contracts/lancer/"]')].map(a => a.getAttribute('href'))})""")
    if not isinstance(monthly, Mapping) or not isinstance(monthly.get("hrefs"), list):
        raise SourceFailure("contract_source_unavailable")
    monthly_ids = []
    for href in monthly["hrefs"]:
        if href == "/monthly_work_contracts/lancer/offers":
            continue
        match = re.fullmatch(r"/monthly_work_contracts/lancer/([0-9]+)", str(href or ""))
        if match is None:
            raise SourceFailure("contract_source_unavailable")
        monthly_ids.append(match.group(1))
    if len(monthly_ids) != len(set(monthly_ids)) or not monthly_ids and monthly.get("empty") is not True:
        raise SourceFailure("contract_source_conflict")
    candidates = [
        {"source_kind": "project", "provider_id": project_id, "board_id": None, "detail_path": f"/work/detail/{project_id}", "funding_status": "requires_detail_readback"}
        for project_id in project_ids
    ] + [
        {"source_kind": "monthly", "provider_id": contract_id, "board_id": None, "detail_path": f"/monthly_work_contracts/lancer/{contract_id}", "funding_status": "requires_detail_readback"}
        for contract_id in monthly_ids
    ]
    incoming = [{"provider_id": offer_id, "detail_path": f"/monthly_work_contracts/lancer/offers/{offer_id}"} for offer_id in offer_ids]
    return {"incoming_monthly_offer_count": len(incoming), "incoming_monthly_offers": incoming, "project_working_count": len(project_ids), "monthly_contract_count": len(monthly_ids), "contract_candidates": candidates}


def _proposal_pipeline(page: Any, receipt_count: int) -> dict[str, int]:
    path = "/mypage/proposals/limit:100/sort:Proposal.id/direction:DESC"
    response = page.goto("https://www.lancers.jp" + path, wait_until="domcontentloaded", timeout=20_000)
    parsed = urlsplit(str(page.url))
    if response is None or response.status != 200 or (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment) != ("https", "www.lancers.jp", path, "", ""):
        raise SourceFailure("proposal_pipeline_unavailable")
    value = page.evaluate("""() => ({
      total: document.querySelector("li.p-mypage-pagers__list-item")?.innerText?.trim(),
      rows: [...document.querySelectorAll("li.p-mypage-work__media.c-media-job")].map(card => ({
        href: card.querySelector('a[href^="/work/detail/"]')?.getAttribute("href"),
        status: [...card.querySelectorAll(".c-media-job__statuses > .c-media-job__status")][1]?.innerText?.replace(/\\s+/g, " ")?.trim()
      }))
    })""")
    if not isinstance(value, Mapping) or not isinstance(value.get("rows"), list):
        raise SourceFailure("proposal_pipeline_unavailable")
    match = re.fullmatch(r"([0-9][0-9,]*)件中[0-9][0-9,]*-[0-9][0-9,]*件表示", "".join(str(value.get("total") or "").split()))
    total = int(match.group(1).replace(",", "")) if match else -1
    counts = {key: 0 for key in ("open", "selecting", "canceled", "ended", "working", "unknown")}
    project_ids = []
    for row in value["rows"]:
        if not isinstance(row, Mapping): raise SourceFailure("proposal_pipeline_unavailable")
        project = re.fullmatch(r"/work/detail/([0-9]+)", str(row.get("href") or ""))
        status = row.get("status")
        if project is None or not isinstance(status, str): raise SourceFailure("proposal_pipeline_unavailable")
        project_ids.append(project.group(1))
        key = "open" if status.startswith("残り ") else {"選定中": "selecting", "キャンセル": "canceled", "終了": "ended", "進行中": "working"}.get(status, "unknown")
        counts[key] += 1
    if total < 0 or total > 100 or total != len(project_ids) or len(project_ids) != len(set(project_ids)):
        raise SourceFailure("proposal_pipeline_incomplete")
    return {"current_count": total, "receipt_count": receipt_count, "unlisted_receipt_count": max(0, receipt_count - total), **{f"{key}_count": count for key, count in counts.items()}}


def _finance_source(page: Any) -> dict[str, Any]:
    path = "/mypage/payment"
    response = page.goto("https://www.lancers.jp" + path, wait_until="domcontentloaded", timeout=20_000)
    if response is None or response.status != 200 or urlsplit(str(page.url)).path != path: raise SourceFailure("finance_source_unavailable")
    amount = page.locator("dl.p-mypage-payment__amount")
    text = " ".join(amount.inner_text().split()) if amount.count() == 1 else ""
    match = re.fullmatch(r"現在のランサーズ口座残高 ([0-9][0-9,]*) 円", text)
    if match is None: raise SourceFailure("finance_source_unavailable")
    balance = int(match.group(1).replace(",", ""))
    empty = page.get_by_text("履歴はありません", exact=True).count() == 1 and page.get_by_text("ランサーズ口座に入金・出金の履歴があると表示されます", exact=True).count() == 1
    if empty and page.locator("table").count() == 0 and balance == 0:
        return {"source_complete": True, "payment_history_count": 0, "account_balance_jpy": 0, "received_gross_jpy": 0}
    return {"source_complete": False, "error": "finance_detail_readback_required"}


def _read_surfaces(page: Any, verified_proposals: set[str], private_boards: list[Any]) -> dict[str, Any]:
    """Every provider read behind the seven inventory surfaces.

    Reply mutation belongs exclusively to reply_adapter.py and the Reply owner.
    """
    result = _snapshot(lambda path: _fetch(page, path), verified_proposals, private_boards)
    result.update(_contract_sources(page))
    result["contract_candidates"] += result.pop("storefront_contract_candidates")
    result["contract_candidates"].sort(key=lambda row: (row["source_kind"], row["provider_id"]))
    result["contract_candidate_count"] = len(result["contract_candidates"])
    result["proposal_pipeline"] = _proposal_pipeline(page, len(verified_proposals))
    result["finance"] = _finance_source(page)
    return result


def read_only_inventory(*, state_path: Path = DEFAULT_STATE_PATH, browser_factory: Optional[Callable[[str], Any]] = None) -> dict[str, Any]:
    """ELZ-L01 preflight: read the seven surfaces and leave the marketplace untouched.

    Unlike run_tick this writes no state file, so running it twice cannot change
    what the second read observes.
    """
    browser = page = None
    logged_in = False
    result = _failed("observer_unavailable")
    try:
        verified_proposals = _verified_proposals(Path(state_path))
        with application_tick.account_lock(Path(state_path).with_name("preflight.json")):
            browser, page = application_tick._open_owned_page(browser_factory)
            if not application_tick._production_account_ready(page):
                raise SourceFailure("account_unavailable")
            logged_in = True
            result = _read_surfaces(page, verified_proposals, [])
            result["logged_in"] = True
    except SourceFailure as error:
        result = _failed(str(error), logged_in)
    except Exception as error:
        result = _failed("account_lock_busy" if type(error).__name__ == "_AccountLockBusy" else "observer_unavailable", logged_in)
    finally:
        if not _cleanup(page, browser):
            result = _failed("cleanup_failed", logged_in)
    return result


def preflight(*, state_path: Path = DEFAULT_STATE_PATH, browser_factory: Optional[Callable[[str], Any]] = None) -> dict[str, Any]:
    """ELZ-L01: two read-only passes must observe the same inventory."""
    reads: list[dict[str, Any]] = []
    for index in (1, 2):
        inventory = read_only_inventory(state_path=state_path, browser_factory=browser_factory)
        if not inventory.get("ok"):
            return {"ok": False, "atom": "ELZ-L01", "status": "FAIL", "failed_read": index, "error": inventory.get("error"), "logged_in": inventory.get("logged_in", False)}
        reads.append(inventory)
    digests = [_digest(read) for read in reads]
    identical = digests[0] == digests[1]
    return {
        "ok": identical,
        "atom": "ELZ-L01",
        "status": "PASS" if identical else "FAIL",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "identical": identical,
        "inventory_sha256": digests,
        "reads": [
            {
                "index": index + 1,
                "logged_in": read["logged_in"],
                "source_complete": read["source_complete"],
                "board_count": read["board_count"],
                "unread_count": read["unread_count"],
                "required_reply_count": read["required_reply_count"],
                "application_board_count": read["application_board_count"],
                "contract_candidate_count": read["contract_candidate_count"],
                "incoming_monthly_offer_count": read["incoming_monthly_offer_count"],
                "proposal_pipeline": read["proposal_pipeline"],
                "finance": read["finance"],
            }
            for index, read in enumerate(reads)
        ],
        "provider_effect": 0,
        "provider_effect_proof": "read_only_inventory reaches no write path; asserted by test_read_only_inventory_cannot_reach_the_reply_post",
    }


def _cleanup(page: Any, browser: Any) -> bool:
    try: page_ok = page is None or bool(application_tick._close_owned_page(page))
    except Exception: page_ok = False
    try: getattr(getattr(browser, "_anicca_playwright_runtime", None), "stop", lambda: None)()
    except Exception: return False
    return page_ok


def _failed(error: str, logged_in: bool = False) -> dict[str, Any]:
    return {"ok": False, "logged_in": logged_in, "source_complete": False, "error": error}


def run_tick(*, state_path: Path = DEFAULT_STATE_PATH, browser_factory: Optional[Callable[[str], Any]] = None) -> dict[str, Any]:
    browser = page = None
    logged_in = False
    result = _failed("observer_unavailable")
    try:
        verified_proposals = _verified_proposals(Path(state_path))
        with application_tick.account_lock(Path(state_path).with_name("work-sync.json")):
            browser, page = application_tick._open_owned_page(browser_factory)
            if not application_tick._production_account_ready(page):
                raise SourceFailure("account_unavailable")
            logged_in = True
            result = _read_surfaces(page, verified_proposals, [])
            result["reply_action"] = {"status": "owned_by_reply_lane"}
            _write_state(Path(state_path).with_name("contracts.json"), {
                "source_complete": True,
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "board_count": result["board_count"],
                "unread_count": result["unread_count"],
                "required_reply_count": result["required_reply_count"],
                "application_board_count": result["application_board_count"],
                "boards": result["boards"],
                "reply_status": result["reply_action"]["status"],
                "proposal_pipeline": result["proposal_pipeline"],
                "finance": result["finance"],
                "project_working_count": result["project_working_count"],
                "monthly_contract_count": result["monthly_contract_count"],
                "incoming_monthly_offer_count": result["incoming_monthly_offer_count"],
                "incoming_monthly_offers": result["incoming_monthly_offers"],
                "storefront_contract_candidate_count": result["storefront_contract_candidate_count"],
                "contract_candidate_count": result["contract_candidate_count"],
                "contract_candidates": result["contract_candidates"],
            })
    except SourceFailure as error:
        result = _failed(str(error), logged_in)
    except Exception as error:
        result = _failed("account_lock_busy" if type(error).__name__ == "_AccountLockBusy" else "observer_unavailable", logged_in)
    finally:
        if not _cleanup(page, browser):
            result = _failed("cleanup_failed", logged_in)
    return result


def _watchdog(command: Sequence[str], timeout: float) -> dict[str, Any]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        return _failed("tick_timeout")
    _kill_group(process)
    if stderr or len(stdout.splitlines()) != 1:
        return _failed("worker_failed")
    try: result = json.loads(stdout)
    except (TypeError, ValueError): return _failed("worker_failed")
    if not isinstance(result, Mapping) or type(result.get("ok")) is not bool:
        return _failed("worker_failed")
    if process.returncode != (0 if result["ok"] else 1):
        return _failed("worker_failed")
    return result


def _kill_group(process: subprocess.Popen[str]) -> None:
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: return
    try: process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        try: process.wait(timeout=1)
        except subprocess.TimeoutExpired: pass


def main(argv: Optional[Sequence[str]] = None, *, output_stream: Any = None, browser_factory: Optional[Callable[[str], Any]] = None, worker_command: Optional[Sequence[str]] = None, timeout: float = TICK_TIMEOUT_SECONDS) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--json", action="store_true", required=True)
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preflight", action="store_true", help="ELZ-L01: read the inventory twice, write nothing")
    args = parser.parse_args(argv)
    if args.preflight:
        result = preflight(state_path=Path(args.state_path), browser_factory=browser_factory)
    else:
        result = run_tick(state_path=Path(args.state_path), browser_factory=browser_factory) if args.worker else _watchdog(worker_command or [sys.executable, str(Path(__file__).resolve()), "--worker", "--json", "--state-path", args.state_path], timeout)
    output = output_stream or sys.stdout
    output.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
