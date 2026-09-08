#!/usr/bin/env python3
"""Read-only authenticated Coconala DOM collector for the Gig delivery queue."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import fcntl
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import select
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import websockets

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from gig_paths import BROWSER_DIR, RUNNER_DIR  # noqa: E402

try:
    from delivery_cadence import inquiries_from_dom as normalize_inquiries
except ModuleNotFoundError:  # imported directly by the unit-test loader
    _cadence_spec = importlib.util.spec_from_file_location(
        "delivery_cadence", Path(__file__).with_name("delivery_cadence.py")
    )
    if _cadence_spec is None or _cadence_spec.loader is None:
        raise
    _cadence = importlib.util.module_from_spec(_cadence_spec)
    _cadence_spec.loader.exec_module(_cadence)
    normalize_inquiries = _cadence.inquiries_from_dom

try:
    from connector_outbox import ConnectorOutbox, outgoing_sha256
except ModuleNotFoundError:  # imported directly by the unit-test loader
    _outbox_spec = importlib.util.spec_from_file_location(
        "connector_outbox", Path(__file__).with_name("connector_outbox.py")
    )
    if _outbox_spec is None or _outbox_spec.loader is None:
        raise
    _outbox = importlib.util.module_from_spec(_outbox_spec)
    _outbox_spec.loader.exec_module(_outbox)
    ConnectorOutbox = _outbox.ConnectorOutbox
    outgoing_sha256 = _outbox.outgoing_sha256


_RETAINER_MODULE = None
_POSTING_MODULE = None
_DM_COLLECT_MODULE = None


def _dm_collect_module():
    global _DM_COLLECT_MODULE
    if _DM_COLLECT_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "coconala_dm_collect_for_negotiate", Path(__file__).with_name("coconala_dm_collect.py")
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load coconala_dm_collect")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DM_COLLECT_MODULE = module
    return _DM_COLLECT_MODULE


def merge_verified_dm_attachments(dom: dict[str, Any], document: dict[str, Any]) -> None:
    """Bind downloaded buyer files to the exact semantic message, fail closed."""
    index = {
        str(row.get("url") or ""): row
        for row in document.get("attachment_index", []) if isinstance(row, dict)
    }
    semantic_rows = [
        row for row in dom.get("messages", []) if isinstance(row, dict)
    ]
    document_rows = [
        row for row in document.get("messages", []) if isinstance(row, dict)
    ]
    semantic_messages = {
        str(row.get("message_id") or ""): row
        for row in semantic_rows if row.get("message_id")
    }
    for message in document_rows:
        if not isinstance(message, dict) or message.get("side") != "buyer":
            continue
        attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
        if not attachments:
            continue
        target = semantic_messages.get(str(message.get("message_id") or ""))
        if target is None and not message.get("message_id"):
            exact = [
                row for row in semantic_rows
                if str(row.get("body") or "") == str(message.get("text") or "")
                and (
                    row.get("side") == "buyer"
                    or row.get("author_path") != dom.get("own_user_path")
                )
            ]
            if len(exact) == 1:
                target = exact[0]
        if target is None:
            raise CollectorUnhealthy("dm_attachment_message_identity_changed")
        verified: list[dict[str, Any]] = []
        for attachment in attachments:
            row = index.get(str(attachment.get("url") or "")) if isinstance(attachment, dict) else None
            if (
                not isinstance(row, dict) or row.get("error")
                or type(row.get("bytes")) is not int or row["bytes"] < 1
                or type(row.get("sha256")) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
            ):
                raise CollectorUnhealthy("dm_attachment_unverified")
            verified.append({
                "filename": safe_filename(row.get("filename")),
                "content_type": str(row.get("content_type") or "application/octet-stream"),
                "size_bytes": row["bytes"], "sha256": row["sha256"],
            })
        target["verified_attachments"] = verified


def enrich_verified_dm_attachments(
    dom: dict[str, Any], *, helper: Path, thread_id: str, observed_at: str,
) -> None:
    """Use the existing authenticated DM collector before semantic judgement."""
    state_root = Path(os.environ.get("GIG_STATE_DIR") or (Path.home() / "gig"))
    project_root = state_root / "direct-message-materials" / safe_name(thread_id)
    secure_directory(project_root)
    result = _dm_collect_module().collect(
        helper=helper, project_root=project_root, buyer="", thread_id=thread_id,
        observed_at=observed_at, owner=os.environ.get("CLOAK_BROWSER_OWNER") or None,
        fetch_attachments=True,
    )
    if result.get("ok") is not True or result.get("attachment_errors"):
        raise CollectorUnhealthy("dm_attachment_capture_failed")
    try:
        document = json.loads(Path(str(result["path"])).read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CollectorUnhealthy("dm_attachment_evidence_invalid") from error
    merge_verified_dm_attachments(dom, document)


def merge_durable_dm_attachments(dom: dict[str, Any], thread_id: str) -> None:
    """Rebind the already verified manifest during the final pre-click read."""
    state_root = Path(os.environ.get("GIG_STATE_DIR") or (Path.home() / "gig"))
    path = (
        state_root / "direct-message-materials" / safe_name(thread_id)
        / "source" / "dm" / f"thread-{safe_name(thread_id)}-full.json"
    )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CollectorUnhealthy("dm_attachment_evidence_invalid") from error
    merge_verified_dm_attachments(dom, document)


def _posting_module():
    """Load posting_source lazily, same shape as the retainer loader."""
    global _POSTING_MODULE
    if _POSTING_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "posting_source", Path(__file__).with_name("posting_source.py")
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load posting_source")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _POSTING_MODULE = module
    return _POSTING_MODULE


def install_project_posting(project_id: str, projects_root: Path) -> dict[str, Any] | None:
    """Put the posting we applied to inside the project we won.

    The posting is harvested at application time, when no project directory exists yet.
    This is the moment it can be filed: an order the collector can see is an order whose
    project exists. Absence of a stored posting is normal (a direct offer never had one),
    so it is reported as ``None`` rather than raised -- the money-critical talkroom read
    must not fail because a posting is missing.
    """
    posting = _posting_module()
    try:
        return posting.install_posting(
            posting.DEFAULT_STORE,
            projects_root.expanduser().resolve() / str(project_id),
            str(project_id),
        )
    except (posting.PostingError, OSError):
        return None


def persist_project_proposal(
    project_id: str, talkroom_id: str, projects_root: Path, offer: dict[str, Any], observed_at: str,
) -> Path | None:
    """Persist the seller's pre-purchase offer inside the purchased project."""
    body = str(offer.get("body") or "").strip()
    if not body:
        return None
    root = projects_root / project_id / "source" / "proposal"
    secure_directory(root)
    path = root / f"offer-{safe_name(talkroom_id)}.json"
    atomic_json(path, {
        "version": 1,
        "source": "authenticated_coconala_offer_page",
        "observed_at": observed_at,
        "talkroom_id": talkroom_id,
        "request_id": offer.get("request_id"),
        "url": urlsplit(str(offer.get("url") or "")).path,
        "direct_message_reference": urlsplit(str(offer.get("direct_message_url") or "")).path or None,
        "title": str(offer.get("title") or "").strip(),
        "body": body,
        "body_bytes": len(body.encode("utf-8")),
    })
    return path


def _retainer_module():
    """Load retainer_thread lazily so this collector keeps its import shape."""
    global _RETAINER_MODULE
    if _RETAINER_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "retainer_thread", Path(__file__).with_name("retainer_thread.py")
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load retainer_thread")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RETAINER_MODULE = module
    return _RETAINER_MODULE


RETAINER_APPLICATIONS_URL = (
    "https://coconala.com/mypage/job_matching/applied/outsource_applications"
)
OPEN_ORDERS_URL = "https://coconala.com/mypage/received_orders/open"
REQUESTS_URL = "https://coconala.com/mypage/received_orders/requests"
MESSAGES_URL = "https://coconala.com/message"
B1_INBOX_URL = "https://coconala.com/message?fromMyPage=true"
CONNECTOR_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "config" / "connectors" / "coconala.json"
ACTIONABLE = re.compile(r"修正|改善|成果物|納品|提出|追加(?:して|撮影)|誤検出|見逃し|エラー|バグ|全361")
AGREEMENT = re.compile(r"問題ありません|問題ない|確認済み|OK|ＯＫ|大丈夫|承認|受け入れました|受け入れます")
BUYER_FORMAL_DELIVERY_HOLD_REASON = "buyer_explicit_formal_delivery_hold"
_FORMAL_DELIVERY_JA = r"正式\s*(?:な\s*)?納品"
_FORMAL_DELIVERY_EN = r"formal(?:ly)?\s+deliver(?:y)?"
_BUYER_FORMAL_DELIVERY_HOLD_JA = re.compile(
    rf"(?:{_FORMAL_DELIVERY_JA}.{{0,20}}(?:取り消|取消|キャンセル|中止|やめ|しない|送らない|不要|待って|保留)"
    rf"|{_FORMAL_DELIVERY_JA}.{{0,20}}(?:まだ|終わり次第|完了後)"
    rf"|(?:まだ|終わり次第|完了後).{{0,20}}{_FORMAL_DELIVERY_JA})"
)
_BUYER_FORMAL_DELIVERY_HOLD_EN = re.compile(
    rf"(?:(?:do\s*not|don't|dont|hold|cancel|retract|wait|not\s+yet|not\s+ready|please\s+wait).{{0,40}}{_FORMAL_DELIVERY_EN}"
    rf"|{_FORMAL_DELIVERY_EN}.{{0,40}}(?:do\s*not|don't|dont|hold|cancel|retract|wait|not\s+yet|not\s+ready|please\s+wait))",
    re.IGNORECASE,
)
_BUYER_FORMAL_DELIVERY_RELEASE_JA = re.compile(
    rf"{_FORMAL_DELIVERY_JA}\s*(?:を\s*)?(?:お願いします|してください|して下さい|しても(?:大丈夫|よい|良い))"
)
_BUYER_FORMAL_DELIVERY_RELEASE_EN = re.compile(
    rf"(?:\b(?:you\s+may|please|go\s+ahead(?:\s+with|\s+and)?|proceed\s+with)\s+{_FORMAL_DELIVERY_EN}\b"
    rf"|\b{_FORMAL_DELIVERY_EN}\s+(?:please|now|is\s+(?:okay|fine))\b)",
    re.IGNORECASE,
)


def buyer_formal_delivery_directive(messages: Any) -> dict[str, Any]:
    """Reduce full chronological buyer history to a safe delivery directive."""
    hold = False
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("side") != "buyer":
            continue
        text = str(message.get("text") or "")
        if not text:
            continue
        # A contradictory single message remains a hold; a later buyer message
        # can release it on the next chronological iteration.
        if _BUYER_FORMAL_DELIVERY_HOLD_JA.search(text) or _BUYER_FORMAL_DELIVERY_HOLD_EN.search(text):
            hold = True
        elif _BUYER_FORMAL_DELIVERY_RELEASE_JA.search(text) or _BUYER_FORMAL_DELIVERY_RELEASE_EN.search(text):
            hold = False
    return {
        "buyer_formal_delivery_hold": hold,
        "buyer_formal_delivery_hold_reason": BUYER_FORMAL_DELIVERY_HOLD_REASON if hold else None,
    }


# A5: coconala runs its OWN clock on a purchased order -- if the seller writes nothing in
# the talkroom within 48h the platform cancels the transaction and the fee is gone. That
# clock is not the delivery date and is not derivable from anything we hold: the banner
# below is the only place the marketplace states it, and the marketplace is the one that
# enforces it. A locally recomputed "purchase time + 48h" would be our number, not theirs.
# The banner is also self-retracting -- coconala stops rendering it the moment any seller
# message exists -- which is why nothing here needs a flag to remember that we replied.
AUTO_CANCEL_NOTICE = re.compile(
    r"[^\n]*\d+時間以内[^\n]*自動的に取引がキャンセル[^\n]*(?:\n[^\n]*期限[^\n]*)?"
)
CONTACT_DEADLINE = re.compile(
    r"期限[：:]?\s*(?:(20\d{2})[/年])?\s*(\d{1,2})[/月]\s*(\d{1,2})日?\s*(\d{1,2}):(\d{2})"
)
JST = ZoneInfo("Asia/Tokyo")

ORDERS_EXPRESSION = r'''JSON.stringify({url:location.href,title:document.title,cards:[...document.querySelectorAll("a[href*='/talkrooms/']")].map(a=>a.closest('.d-providerTalkroomCassette')).filter(Boolean).filter((x,i,a)=>a.indexOf(x)===i).map(card=>{const room=card.querySelector("a[href*='/talkrooms/']");const title=card.querySelector('.d-providerTalkroomCassetteSpHeading_title')||room;const lines=card.innerText.split('\n').map(x=>x.trim()).filter(Boolean);const priceNode=card.querySelector('.d-providerTalkroomCassettePrice_price')||[...card.querySelectorAll('.d-providerTalkroomCassetteSpDetail_info')].find(x=>x.querySelector('.d-providerTalkroomCassetteSpDetail_yenIcon'));const priceText=priceNode?(priceNode.innerText||'').trim():null;return {text:card.innerText,talkroom_url:room&&room.href,buyer:lines[1]||'',title:(title&&title.innerText.trim())||lines[0]||'',price_text:priceText,price_source:priceText?'structured_order_label':'missing_structured_price'}})})'''
ORDERS_ONLY_EXPRESSION = r'''(()=>{const container=document.querySelector('.d-transactionListProviderMain')||document.querySelector('main.c-layoutMypage #c-main .c-content');const cards=container?[...container.querySelectorAll("a[href*='/talkrooms/']")].map(a=>a.closest('.d-providerTalkroomCassette')).filter(Boolean).filter((x,i,a)=>a.indexOf(x)===i).map(card=>{const room=card.querySelector("a[href*='/talkrooms/']");const title=card.querySelector('.d-providerTalkroomCassetteSpHeading_title')||room;const lines=card.innerText.split('\n').map(x=>x.trim()).filter(Boolean);const priceNode=card.querySelector('.d-providerTalkroomCassettePrice_price')||[...card.querySelectorAll('.d-providerTalkroomCassetteSpDetail_info')].find(x=>x.querySelector('.d-providerTalkroomCassetteSpDetail_yenIcon'));const priceText=priceNode?(priceNode.innerText||'').trim():null;return {text:card.innerText,talkroom_url:room&&room.href,buyer:lines[1]||'',title:(title&&title.innerText.trim())||lines[0]||'',price_text:priceText,price_source:priceText?'structured_order_label':'missing_structured_price'}}):[];const empty_state_present=cards.length===0&&!!container&&[...container.querySelectorAll('[data-testid="order-empty"],[data-testid="empty-state"],[data-testid="empty"],[data-order-state="empty"],[class*="empty"],[class*="Empty"]')].some(x=>/(?:受注|取引中|該当).*(?:ありません|なし)/.test((x.innerText||'').trim()));return JSON.stringify({url:location.href,title:document.title,container_present:!!container,empty_state_present,cards})})()'''
QUOTES_EXPRESSION = r'''JSON.stringify({url:location.href,title:document.title,cards:[...document.querySelectorAll("a[href*='/customize/requests/']")].map(a=>a.closest('.d-transactionListProviderMain_item')||a.closest('li')||a.parentElement).filter(Boolean).filter((x,i,a)=>a.indexOf(x)===i).map(card=>{const req=card.querySelector("a[href*='/customize/requests/']");const buyer=card.querySelector("a[href*='/users/']");const proposal=card.querySelector("a[href*='/customize/offers/add/']");return {text:card.innerText,request_url:req&&req.href,buyer:buyer&&buyer.innerText.trim(),title:req&&req.innerText.trim(),proposal_url:proposal&&proposal.href}})})'''
MESSAGES_EXPRESSION = r'''(()=>{const title=document.title;const cards=[...document.querySelectorAll("a.c-messageItemWrap[href*='/mypage/direct_message/']")].map(room=>({talkroom_url:room.href,title:'purchase_preorder_message',last_message_side:'',unread:!!room.querySelector('[aria-label*="未読"],[class*="unread"],[class*="Unread"]')}));return JSON.stringify({url:location.href,title,container_present:!!document.querySelector('main.c-layoutMypage #c-main .c-content'),not_found_present:/404|ページが見つかりません|お探しのページ/.test(title)||!!document.querySelector('[class*="not-found"],[class*="notFound"]'),error_present:/エラー|error|メンテナンス/i.test(title)||!!document.querySelector('[class*="error-page"],[class*="errorPage"]'),cards})})()'''
B1_MESSAGES_EXPRESSION = r'''JSON.stringify({url:location.href,title:document.title,not_found:/ご指定のページが見つかりませんでした|ページが見つかりません/.test((document.title||'')+' '+(document.querySelector('h1')?.innerText||'')),cards:[...document.querySelectorAll("a[href*='/talkrooms/']")].map(a=>a.closest('li[data-talkroom-id],li,[data-talkroom-id]')||a.parentElement).filter(Boolean).filter((x,i,a)=>a.indexOf(x)===i).map(card=>{const room=card.querySelector("a[href*='/talkrooms/']");const text=(card.innerText||'').trim();const unread=!!card.querySelector('[aria-label*="未読"],[class*="unread"],[class*="Unread"]');const declared=(card.getAttribute('data-last-message-side')||'').toLowerCase();const messages=[...card.querySelectorAll('.d-talkroomMessage')].filter(m=>{const owner=m.closest('li[data-talkroom-id],li,[data-talkroom-id]');return !owner||owner===card});const last=messages[messages.length-1];const lastSide=declared==='buyer'||declared==='seller'?declared:(last?(last.classList.contains('d-talkroomMessage-isOthers')?'buyer':'seller'):'');return {talkroom_url:room&&room.href,title:(room&&room.innerText||text.split('\\n')[0]||'').trim(),last_message_side:lastSide,unread}})})'''
B1_INBOX_COVERAGE_EXPRESSION = r'''(async()=>{const sleep=ms=>new Promise(r=>setTimeout(r,ms)),direct=false,sel=direct?"a[href*='/talkrooms/']":"a[href*='/talkrooms/']",root=document.querySelector('main.c-layoutMypage #c-main .c-content')||document.body,records=new Map();let stable=0,iterations=0,lastHeight=-1,lastKeys='';const read=()=>[...document.querySelectorAll(sel)].map(a=>{const card=direct?a:(a.closest('li[data-talkroom-id],li,[data-talkroom-id]')||a.parentElement),room=direct?a:card?.querySelector("a[href*='/talkrooms/']"),url=room?.href||a.href;if(!url)return null;const text=(card?.innerText||'').trim(),messages=[...(card?.querySelectorAll('.d-talkroomMessage')||[])],last=messages[messages.length-1],declared=(card?.getAttribute('data-last-message-side')||'').toLowerCase();return {talkroom_url:url,title:direct?'purchase_preorder_message':(room?.innerText||text.split('\n')[0]||'').trim(),last_message_side:direct?'':(declared==='buyer'||declared==='seller'?declared:(last?.classList.contains('d-talkroomMessage-isOthers')?'buyer':'seller')),unread:!!card?.querySelector('[aria-label*="未読"],[class*="unread"],[class*="Unread"]')}}).filter(Boolean);for(;iterations<20&&stable<2;iterations++){for(const row of read())records.set(new URL(row.talkroom_url,location.origin).pathname,row);const keys=[...records.keys()].sort().join('|'),height=Math.max(root.scrollHeight,document.body.scrollHeight);stable=keys===lastKeys&&height===lastHeight?stable+1:0;lastKeys=keys;lastHeight=height;window.scrollTo(0,document.body.scrollHeight);await sleep(100)}const container=root,text=(container.innerText||'').trim(),empty_state_present=/^(メッセージはありません|メッセージがありません|該当するメッセージはありません)$/.test(text)||!!container.querySelector('[data-testid="message-empty"],[data-message-state="empty"]');const rows=[...records.values()];return JSON.stringify({cards:rows,cards_count:rows.length,empty_state_present,coverage_complete:stable>=2,termination_reason:rows.length===0&&empty_state_present?'empty_state':'fixed_point',iterations})})()'''
DIRECT_MESSAGE_EXPRESSION = r'''(()=>{const title=document.title;const container=document.querySelector('.js_thread-wrapper');const rows=container?[...container.querySelectorAll('.threadColomun')]:[];const messageRows=rows.filter(row=>row.querySelector('.threadMessage'));const own=document.querySelector('.sidebar-profile a[href*="/users/"]');const path=a=>a?new URL(a.href,location.origin).pathname:null;const messages=messageRows.map(row=>{const author=row.querySelector('.threadUser a[href*="/users/"]');const time=row.querySelector('.threadPostTime');const body=row.querySelector('.js-translateMessageOriginalMessage')||row.querySelector('.threadMessage');return{message_id:row.getAttribute('data-message-id')||row.id||null,author_path:path(author),sent_at:(time&&time.innerText||'').trim()||null,body:(body&&body.innerText)||''}});const estimate_url=([...document.querySelectorAll('a[href]')].map(a=>path(a)).find(value=>/^\/direct_offers\/add\/[A-Za-z0-9_-]+$/.test(value||''))||null);const service_urls=[...new Set([...document.querySelectorAll('a[href]')].map(a=>path(a)).filter(value=>/^\/services\/\d+$/.test(value||'')))];const structured_offers=[...rows.flatMap(row=>[...row.querySelectorAll('.message-customize')].map(card=>({card,row})))].map(({card,row})=>{const offer=card.closest('.threadMessage')||card;const link=offer.querySelector('.customize-title-link[href]');const text=(offer.innerText||'').replace(/\s+/g,' ').trim();const titleNode=offer.querySelector('.customize-title');const contentNode=offer.querySelector('p.customize-content.wa_add-mt-4')||offer.querySelector('.customize-content');const price=(text.match(/提案額\s*([0-9][0-9,]*)\s*円/)||[])[1];const completion=(text.match(/完了予定日\s*(20\d{2}[\\/-]\d{1,2}[\\/-]\d{1,2}|20\d{2}年\d{1,2}月\d{1,2}日)/)||[])[1]||null;const time=row.querySelector('.threadPostTime');return{offer_url:path(link),message_kind:(card.querySelector('.message-customize-title')||card).innerText.includes('見積り提案をしました')?'見積り提案をしました':'',title:(titleNode&&titleNode.innerText||'').trim()||null,content:(contentNode&&contentNode.innerText||'').trim()||null,price_jpy:price?Number(price.replace(/,/g,'')):null,completion_date:completion?completion.replace(/[年月]/g,'-').replace('日','').replace(/\//g,'-'):null,sent_at:(time&&time.innerText||'').trim()||null}}).filter(card=>card.offer_url||card.message_kind);return JSON.stringify({url:location.href,title,container_present:!!container,not_found_present:/404|ページが見つかりません|お探しのページ/.test(title)||!!document.querySelector('[class*="not-found"],[class*="notFound"]'),error_present:/エラー|error|メンテナンス/i.test(title)||!!document.querySelector('[class*="error-page"],[class*="errorPage"]'),own_user_path:path(own),estimate_url,service_urls,structured_offers,messages})})()'''
DIRECT_INBOX_COVERAGE_EXPRESSION = r'''(async()=>{
const sleep=ms=>new Promise(r=>setTimeout(r,ms)),
isB1=location.pathname==='/message'&&new URL(location.href).searchParams.get('fromMyPage')==='true',
direct=location.pathname==='/message'&&!isB1,
sel=direct?"a.c-messageItemWrap[href*='/mypage/direct_message/']":null,
records=new Map(),pageLimit=10;
let pagesObserved=0,iterations=0,terminationReason='pagination_limit',paginationNextPresent=null,
 paginationContainerPresent=false,paginationCurrentPresent=false,paginationTerminalProven=false,
 paginationCurrentPage=null,paginationHighestPage=null,pageCounts=[];
const digest=async text=>{const data=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));return [...new Uint8Array(data)].map(x=>x.toString(16).padStart(2,'0')).join('')};
const read=async()=>sel?await Promise.all([...document.querySelectorAll(sel)].map(async a=>{
 const card=a,room=a,url=room?.href||a.href;
 if(!url)return null;
 const preview=(card?.innerText||'').trim();
 const message=a.__vue__?._props?.message||null,
 identityFields=message&&typeof message.body==='string'?{directMessagesRoomId:message.directMessagesRoomId??null,fromUserId:message.fromUserId??null,createdAt:message.createdAt??null,body:message.body}:null,
 unread=!!card?.querySelector('[aria-label*="未読"],[class*="unread"],[class*="Unread"]')||(typeof message?.unreadCount==='number'&&message.unreadCount>0);
 return {talkroom_url:url,title:'purchase_preorder_message',counterparty_name:preview.split('\n')[0].trim().slice(0,100),last_message_side:'',unread,preview_sha256:await digest(preview),last_message_identity_fields:identityFields};
})).then(rows=>rows.filter(Boolean)):[];
while(pagesObserved<pageLimit){
 pagesObserved++;
 let stable=0,lastHeight=-1,lastKeys='';
 for(let pageIterations=0;pageIterations<20&&stable<2;pageIterations++){
  for(const row of await read())records.set(new URL(row.talkroom_url,location.origin).pathname,row);
  iterations++;
  const keys=[...records.keys()].sort().join('|'),root=document.querySelector('main.c-layoutMypage #c-main .c-content')||document.body,height=Math.max(root.scrollHeight,document.body.scrollHeight);
  stable=keys===lastKeys&&height===lastHeight?stable+1:0;
  lastKeys=keys;lastHeight=height;window.scrollTo(0,document.body.scrollHeight);await sleep(100);
 }
 const pageCards=await read();
 pageCounts.push(new Set(pageCards.map(row=>row.talkroom_url)).size);
 const pageRoot=document.querySelector('main.c-layoutMypage #c-main .c-content')||document.body,
 pageText=(pageRoot.innerText||'').trim(),
 pageEmpty=/^(メッセージはありません|メッセージがありません|該当するメッセージはありません)$/.test(pageText)||!!pageRoot.querySelector('[data-testid="message-empty"],[data-message-state="empty"]');
 if(pageCards.length===0&&records.size===0&&pageEmpty){terminationReason='empty_state';break}
 if(pageCards.length===0&&!pageEmpty){terminationReason='pagination_selector_empty';break}
 let paginator=null,current=null,next=null;
 for(let wait=0;wait<20;wait++){
  paginator=document.querySelector('.c-pagination');
  current=paginator?.querySelector('.pagination-link-current')||document.querySelector('.pagination-link-current');
  next=paginator?.querySelector('button.pagination-next')||document.querySelector('button.pagination-next');
  if(paginator&&current&&next)break;
  await sleep(100);
 }
 paginationContainerPresent=!!paginator;
 paginationCurrentPresent=!!current;
 if(!paginator||!current){terminationReason='pagination_control_missing';break}
 const pageNumber=node=>{const raw=node?.getAttribute?.('data-page')||node?.innerText||node?.textContent||'',match=String(raw).trim().match(/^\d+$/);return match?Number(match[0]):null};
 paginationCurrentPage=pageNumber(current);
 const pageNumbers=[...(paginator.querySelectorAll?.('a,button,li,[data-page]')||[])].map(pageNumber).filter(Number.isInteger);
 paginationHighestPage=pageNumbers.length?Math.max(...pageNumbers):null;
 paginationNextPresent=!!next&&!next.disabled&&next.getAttribute('aria-disabled')!=='true';
 if(!paginationNextPresent){
  const pageNumbersKnown=paginationCurrentPage!==null&&paginationHighestPage!==null,
   finalPageByNumber=pageNumbersKnown&&paginationCurrentPage===paginationHighestPage,
   finalPageByCount=!pageNumbersKnown&&paginationCurrentPage===null&&paginationHighestPage===null&&pageCards.length>0&&pageCards.length<30;
  paginationTerminalProven=finalPageByNumber||finalPageByCount;
  terminationReason=paginationTerminalProven?'pagination_end':'pagination_terminal_unproven';
  break
 }
 const before=location.href,beforeKeys=(await read()).map(row=>row.talkroom_url).sort().join('|');next.click();let navigated=false;
 for(let wait=0;wait<50;wait++){await sleep(100);if(location.href!==before&&(await read()).map(row=>row.talkroom_url).sort().join('|')!==beforeKeys){navigated=true;break}}
 if(!navigated){terminationReason='pagination_navigation_timeout';break}
 await sleep(100);
}
const container=document.querySelector('main.c-layoutMypage #c-main .c-content')||document.body,text=(container.innerText||'').trim(),empty_state_present=/^(メッセージはありません|メッセージがありません|該当するメッセージはありません)$/.test(text)||!!container.querySelector('[data-testid="message-empty"],[data-message-state="empty"]'),rows=[...records.values()],terminal=rows.length===0&&empty_state_present?'empty_state':terminationReason;
 return JSON.stringify({cards:rows,cards_count:rows.length,empty_state_present,coverage_complete:(terminal==='pagination_end'&&paginationTerminalProven)||terminal==='empty_state',termination_reason:terminal,iterations,pagination_pages:pagesObserved,page_counts:pageCounts,pagination_container_present:paginationContainerPresent,pagination_current_present:paginationCurrentPresent,pagination_terminal_proven:paginationTerminalProven,pagination_current_page:paginationCurrentPage,pagination_highest_page:paginationHighestPage,pagination_next_present:paginationNextPresent});
})()'''
INBOX_COVERAGE_EXPRESSION = DIRECT_INBOX_COVERAGE_EXPRESSION
TALKROOM_EXPRESSION = r'''(async()=>{const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));let jump=null;for(let i=0;i<40&&!jump;i++){jump=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').includes('最新のメッセージに移動'));if(!jump)await wait(100)}if(jump){jump.click();await new Promise(resolve=>setTimeout(resolve,1000))}for(let i=0;i<100&&document.querySelectorAll('.d-talkroomMessage').length===0;i++){await wait(100)}const messages=[...document.querySelectorAll('.d-talkroomMessage')];const last=messages[messages.length-1];if(last)last.scrollIntoView({block:'center'});const formalDelivery=document.querySelector('.d-messageFormButtonArea_item-deliveryCheck input[type=checkbox]');const composeTextarea=document.querySelector('textarea[placeholder="メッセージを入力"]');const currentStep=(document.querySelector('.d-talkroomStep_label-current')?.innerText||'').trim();const transactionState=currentStep==='進行中'?'取引中':(currentStep==='納品送付'?'納品確認待ち':(currentStep==='取引完了'?'取引完了':'unknown'));const subscriptionControl=[...document.querySelectorAll('a,button')].some(x=>(x.innerText||'').trim()==='定期購入を終了する');const fileType=n=>{const x=(n||'').toLowerCase();if(x.endsWith('.png'))return'image/png';if(x.endsWith('.jpg')||x.endsWith('.jpeg'))return'image/jpeg';if(x.endsWith('.pdf'))return'application/pdf';if(x.endsWith('.zip'))return'application/zip';return'application/octet-stream'};return JSON.stringify({url:location.href,title:document.title,transaction_state:transactionState,talkroom_step_label:currentStep,delivery_date:((document.body.innerText.match(/納品予定日(?:が登録されました。)?[：:"「]*\s*(20\d{2}\/\d{2}\/\d{2})/)||[])[1]||null),auto_cancel_notice:((document.body.innerText.match(/[^\n]*\d+\u6642\u9593\u4ee5\u5185[^\n]*\u81ea\u52d5\u7684\u306b\u53d6\u5f15\u304c\u30ad\u30e3\u30f3\u30bb\u30eb[^\n]*(?:\n[^\n]*\u671f\u9650[^\n]*)?/)||[])[0]||null),formal_delivery_control_checked:!!(formalDelivery&&formalDelivery.checked),formal_delivery_control_disabled:!!(formalDelivery&&formalDelivery.disabled),subscription_control_present:subscriptionControl,compose_draft_length:(composeTextarea&&composeTextarea.value||'').length,compose_draft_text:(composeTextarea&&composeTextarea.value||'').slice(0,500).replace(/[\uD800-\uDBFF]$/,''),checked_checkbox_count:document.querySelectorAll('input[type=checkbox]:checked').length,offer_url:([...(document.querySelectorAll("a[href*='/mypage/offers/'],a[href*='/direct_offers/edit/'],a[href*='/customize/offers/']"))][0]||{}).href||null,messages:messages.map(m=>{const side=m.classList.contains('d-talkroomMessage-isOthers')?'buyer':((m.innerText||'').trim().startsWith('自分')?'seller':'system');const text=(m.querySelector('.d-normalMessage')?.innerText||'').trim();const attachments=[...m.querySelectorAll('.d-talkroomMessage_attachedFilesItem')].map((f,i)=>{const filename=(f.querySelector('.tooltip-content')?.innerText||f.querySelector('.d-attachedFileName')?.innerText||'attachment').replace(/\s+/g,' ').trim();const size_text=(f.querySelector('.d-attachedFileSize')?.innerText||'').trim();const href=f.querySelector('a[href]')?.href||null;return{filename,content_type:fileType(filename),size_text,href,reference:`message:${m.id||'unknown'}:attachment:${i}`}});return{side,text,attachments}})})})()'''
TALKROOM_FULL_EXPRESSION = r'''(async()=>{const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));for(let i=0;i<100&&document.querySelectorAll('.d-talkroomMessage').length===0;i++)await wait(100);let previous=-1,stable=0,iterations=0;for(;iterations<80&&stable<5;iterations++){const rows=[...document.querySelectorAll('.d-talkroomMessage')];const first=rows[0];if(first){first.scrollIntoView({block:'start'});let node=first.parentElement;while(node){if(node.scrollHeight>node.clientHeight)node.scrollTop=0;node=node.parentElement}}window.scrollTo(0,0);await wait(250);const count=document.querySelectorAll('.d-talkroomMessage').length;if(count===previous)stable++;else{previous=count;stable=0}}const messages=[...document.querySelectorAll('.d-talkroomMessage')];const formalDelivery=document.querySelector('.d-messageFormButtonArea_item-deliveryCheck input[type=checkbox]');const composeTextarea=document.querySelector('textarea[placeholder="メッセージを入力"]');const currentStep=(document.querySelector('.d-talkroomStep_label-current')?.innerText||'').trim();const transactionState=currentStep==='進行中'?'取引中':(currentStep==='納品送付'?'納品確認待ち':(currentStep==='取引完了'?'取引完了':'unknown'));const subscriptionControl=[...document.querySelectorAll('a,button')].some(x=>(x.innerText||'').trim()==='定期購入を終了する');const fileType=n=>{const x=(n||'').toLowerCase();if(x.endsWith('.png'))return'image/png';if(x.endsWith('.jpg')||x.endsWith('.jpeg'))return'image/jpeg';if(x.endsWith('.pdf'))return'application/pdf';if(x.endsWith('.zip'))return'application/zip';return'application/octet-stream'};return JSON.stringify({url:location.href,title:document.title,transaction_state:transactionState,talkroom_step_label:currentStep,delivery_date:((document.body.innerText.match(/納品予定日(?:が登録されました。)?[：:"「]*\s*(20\d{2}\/\d{2}\/\d{2})/)||[])[1]||null),auto_cancel_notice:((document.body.innerText.match(/[^\n]*\d+\u6642\u9593\u4ee5\u5185[^\n]*\u81ea\u52d5\u7684\u306b\u53d6\u5f15\u304c\u30ad\u30e3\u30f3\u30bb\u30eb[^\n]*(?:\n[^\n]*\u671f\u9650[^\n]*)?/)||[])[0]||null),formal_delivery_control_checked:!!(formalDelivery&&formalDelivery.checked),formal_delivery_control_disabled:!!(formalDelivery&&formalDelivery.disabled),subscription_control_present:subscriptionControl,compose_draft_length:(composeTextarea&&composeTextarea.value||'').length,compose_draft_text:(composeTextarea&&composeTextarea.value||'').slice(0,500).replace(/[\uD800-\uDBFF]$/,''),checked_checkbox_count:document.querySelectorAll('input[type=checkbox]:checked').length,offer_url:([...(document.querySelectorAll("a[href*='/mypage/offers/'],a[href*='/direct_offers/edit/'],a[href*='/customize/offers/']"))][0]||{}).href||null,history_complete:stable>=5,history_iterations:iterations,message_count:messages.length,messages:messages.map(m=>{const side=m.classList.contains('d-talkroomMessage-isOthers')?'buyer':((m.innerText||'').trim().startsWith('自分')?'seller':'system');const text=(m.querySelector('.d-normalMessage')?.innerText||'').trim();const sentAt=(m.querySelector('time,.d-talkroomMessage_time,[class*=time]')?.innerText||'').trim()||null;const messageId=m.id||m.getAttribute('data-message-id')||null;const attachments=[...m.querySelectorAll('.d-talkroomMessage_attachedFilesItem')].map((f,i)=>{const filename=(f.querySelector('.tooltip-content')?.innerText||f.querySelector('.d-attachedFileName')?.innerText||'attachment').replace(/\s+/g,' ').trim();const size_text=(f.querySelector('.d-attachedFileSize')?.innerText||'').trim();const href=f.querySelector('a[href]')?.href||null;return{filename,content_type:fileType(filename),size_text,href,reference:`message:${messageId||'unknown'}:attachment:${i}`}});return{message_id:messageId,side,sent_at:sentAt,text,attachments}})})})()'''
TALKROOM_ATTACHMENT_EXPRESSION = r'''(async()=>{const limit=16*1024*1024;let used=0;const rows=[];const enc=bytes=>{let s='';for(let i=0;i<bytes.length;i+=32768)s+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(s)};const messages=[...document.querySelectorAll('.d-talkroomMessage')];for(const m of messages){if(!m.classList.contains('d-talkroomMessage-isOthers'))continue;const files=[...m.querySelectorAll('.d-talkroomMessage_attachedFilesItem')];for(let i=0;i<files.length;i++){const f=files[i];const a=f.querySelector('a[href]');const row={reference:`message:${m.id||'unknown'}:attachment:${i}`,capture_error:null};if(!a?.href){row.capture_error='attachment_href_missing';rows.push(row);continue}try{const response=await fetch(a.href,{credentials:'include',signal:AbortSignal.timeout(10000)});if(!response.ok)throw new Error(`http_${response.status}`);const buffer=await response.arrayBuffer();if(used+buffer.byteLength>limit){row.capture_error='attachment_capture_limit';rows.push(row);continue}used+=buffer.byteLength;row.content_type=response.headers.get('content-type')||null;row.size_bytes=buffer.byteLength;row.data_base64=enc(new Uint8Array(buffer))}catch(error){row.capture_error=error?.name==='TimeoutError'?'attachment_fetch_timeout':String(error?.message||error).slice(0,160)}rows.push(row)}}return JSON.stringify(rows)})()'''
OFFER_EXPRESSION = r'''JSON.stringify({url:location.href,title:document.title,request_id:((document.body.innerText.match(/No\.(\d+)/)||[])[1]||(([...document.querySelectorAll("a[href*='/requests/']")].map(a=>a.href).join(' ').match(/\/requests\/(\d+)/)||[])[1])||null),direct_message_url:([...(document.querySelectorAll("a[href*='/mypage/direct_message/']"))][0]||{}).href||null,body:(document.body.innerText||'').trim().slice(0,20000)})'''
_CURRENT_STEP_JS = "const currentStep=(document.querySelector('.d-talkroomStep_label-current')?.innerText||'').trim();const transactionState=currentStep==='進行中'?'取引中':(currentStep==='納品送付'?'納品確認待ち':(currentStep==='取引完了'?'取引完了':'unknown'));"
_TERMINAL_AWARE_STEP_JS = "const currentStep=(document.querySelector('.d-talkroomStep_label-current')?.innerText||'').trim();const flowText=(document.querySelector('.d-talkroomStep')?.innerText||'').trim();const transactionState=currentStep==='進行中'?'取引中':(currentStep==='納品送付'?'納品確認待ち':(currentStep==='取引完了'?'取引完了':(/評価完了|取引完了/.test(flowText)?'取引完了':'unknown')));"
TALKROOM_EXPRESSION = TALKROOM_EXPRESSION.replace(_CURRENT_STEP_JS, _TERMINAL_AWARE_STEP_JS)
TALKROOM_FULL_EXPRESSION = TALKROOM_FULL_EXPRESSION.replace(_CURRENT_STEP_JS, _TERMINAL_AWARE_STEP_JS)

TRANSIENT_NAVIGATION_ERROR = "authenticated tab did not finish navigation"
NAVIGATION_RETRY_ATTEMPTS = 2


def _is_transient_tab_open_error(error: RuntimeError) -> bool:
    message = str(error)
    return (
        message == TRANSIENT_NAVIGATION_ERROR
        or (
            message.startswith("failed to open authenticated hidden target:")
            and "timed out" in message.lower()
        )
    )
NAVIGATION_READY_STATES = frozenset({"interactive", "complete"})


class CollectorUnhealthy(RuntimeError):
    """A page observation cannot safely be interpreted as an empty queue."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(f"collector_unhealthy:{reason}")
        self.details = dict(details or {})


def load_connector_manifest(path: Path = CONNECTOR_MANIFEST_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CollectorUnhealthy("invalid_connector_manifest") from error
    if (
        not isinstance(value, dict)
        or value.get("enabled") is not True
        or value.get("authorization_source") != "user_confirmed"
        or value.get("policy_lookup_at_runtime") is not False
        or value.get("terms_lookup_at_runtime") is not False
        or value.get("revoked_at") is not None
        or value.get("inbox", {}).get("url") != MESSAGES_URL
    ):
        raise CollectorUnhealthy("invalid_connector_manifest")
    return value


def validate_page_identity(
    dom: dict[str, Any], *, expected_url: str, expected_title: str
) -> None:
    current_url = str(dom.get("url") or "")
    title = str(dom.get("title") or "")
    current_path = urlsplit(current_url).path.rstrip("/") or "/"
    expected_path = urlsplit(expected_url).path.rstrip("/") or "/"
    if current_path.startswith("/login") or "ログイン" in title:
        raise CollectorUnhealthy("login")
    if dom.get("not_found_present") is True or re.search(
        r"404|ページが見つかりません|お探しのページ", title
    ):
        raise CollectorUnhealthy("not_found")
    if dom.get("error_present") is True or re.search(r"エラー|error|メンテナンス", title, re.I):
        raise CollectorUnhealthy("error_page")
    if current_path != expected_path:
        raise CollectorUnhealthy("unexpected_url")
    if expected_title not in title:
        raise CollectorUnhealthy("unexpected_title")
    if dom.get("container_present") is not True:
        raise CollectorUnhealthy("missing_container")


def navigation_state_ready(loaded: dict[str, Any], expected_url: str | None = None) -> bool:
    """Return true once the expected authenticated DOM can be evaluated.

    Coconala may keep network activity open after the page has reached the
    interactive state.  Requiring ``complete`` turns that normal transient
    into a false navigation failure even though the DOM and screenshot are
    already usable.  The URL/path and non-blank guard remain fail-closed.
    """
    current_url = str(loaded.get("url") or "")
    reached_expected = not expected_url or urlsplit(current_url).path == urlsplit(expected_url).path
    return (
        reached_expected
        and current_url not in ("", "about:blank")
        and loaded.get("ready") in NAVIGATION_READY_STATES
    )


def validate_inbox_dom(dom: dict[str, Any]) -> bool:
    """B1 must retain its query-bound paid-talkroom inbox route."""
    parsed = urlsplit(str(dom.get("url") or ""))
    return (
        dom.get("not_found") is not True
        and dom.get("not_found_present") is not True
        and parsed.scheme == "https"
        and parsed.hostname == "coconala.com"
        and parsed.path == "/message"
        and parse_qs(parsed.query, keep_blank_values=True) == {"fromMyPage": ["true"]}
    )


def coverage_expression_for_route(expected_url: str | None) -> str | None:
    """Keep direct pagination coverage separate from B1's legacy talkroom coverage."""
    parsed = urlsplit(str(expected_url or ""))
    if parsed.path != "/message":
        return None
    if parse_qs(parsed.query, keep_blank_values=True).get("fromMyPage") == ["true"]:
        return B1_INBOX_COVERAGE_EXPRESSION
    return DIRECT_INBOX_COVERAGE_EXPRESSION


def validate_orders_dom(dom: dict[str, Any]) -> dict[str, Any]:
    """Reject an unauthenticated or semantically empty open-orders page."""
    parsed = urlsplit(str(dom.get("url") or ""))
    title = str(dom.get("title") or "")
    if parsed.path.startswith("/login") or "ログイン" in title:
        raise CollectorUnhealthy("orders_login")
    if dom.get("not_found_present") is True or re.search(r"404|ページが見つかりません|お探しのページ", title):
        raise CollectorUnhealthy("orders_not_found")
    if dom.get("error_present") is True or re.search(r"エラー|error|メンテナンス", title, re.I):
        raise CollectorUnhealthy("orders_error_page")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "coconala.com"
        or parsed.path != urlsplit(OPEN_ORDERS_URL).path
        or parsed.query
    ):
        raise CollectorUnhealthy("orders_unexpected_url")
    if dom.get("container_present") is not True:
        raise CollectorUnhealthy("orders_missing_container")
    cards = dom.get("cards")
    if not isinstance(cards, list):
        raise CollectorUnhealthy("orders_cards_missing")
    if not all(isinstance(card, dict) for card in cards):
        raise CollectorUnhealthy("orders_cards_invalid")
    if not cards and dom.get("empty_state_present") is not True:
        raise CollectorUnhealthy("orders_empty_without_semantic_marker")
    return {
        "coverage_complete": True,
        "termination_reason": "empty_state" if not cards else None,
    }


def inspect_orders_when_ready(
    helper: Path, evidence_dir: Path, *, hidden: bool, attempts: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retry only a correctly routed orders page whose semantic DOM is still loading."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    transient = {"orders_missing_container", "orders_empty_without_semantic_marker"}
    for attempt in range(attempts):
        dom = inspect_page_with_retry(
            helper, OPEN_ORDERS_URL, ORDERS_ONLY_EXPRESSION,
            None if hidden else evidence_dir / "received-orders-open.png",
            hidden=hidden,
        )
        try:
            return dom, validate_orders_dom(dom)
        except CollectorUnhealthy as error:
            reason = str(error).removeprefix("collector_unhealthy:")
            if reason not in transient or attempt == attempts - 1:
                raise
            time.sleep(1)
    raise AssertionError("unreachable orders readiness retry")


def dedupe_inbox_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one bounded record per stable talkroom path across virtualized rounds."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for card in cards:
        path = str(urlsplit(str(card.get("talkroom_url") or "")).path)
        if path and path not in seen:
            seen.add(path)
            result.append(card)
    return result


MAX_PAGINATION_PAGES = 10
MAX_PAGE_COUNT = 30


def direct_inbox_coverage_expression(page_limit: int = 10) -> str:
    """Return direct-inbox coverage bounded to the requested page count."""
    if type(page_limit) is not int or not 1 <= page_limit <= MAX_PAGINATION_PAGES:
        raise ValueError("page_limit must be an integer from 1 to MAX_PAGINATION_PAGES")
    if page_limit == MAX_PAGINATION_PAGES:
        return DIRECT_INBOX_COVERAGE_EXPRESSION
    return DIRECT_INBOX_COVERAGE_EXPRESSION.replace(
        "pageLimit=10", f"pageLimit={page_limit}", 1,
    )


def bounded_pagination_metadata(dom: dict[str, Any]) -> tuple[int | None, list[int] | None, bool]:
    """Return bounded page metadata and whether the source supplied any metadata."""
    pages = dom.get("pagination_pages")
    counts = dom.get("page_counts")
    supplied = pages is not None or counts is not None
    if not supplied:
        return None, None, False
    if (
        isinstance(pages, bool)
        or not isinstance(pages, int)
        or not 1 <= pages <= MAX_PAGINATION_PAGES
        or not isinstance(counts, list)
        or len(counts) != pages
        or any(
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= MAX_PAGE_COUNT
            for count in counts
        )
    ):
        return None, None, True
    return pages, list(counts), True


def bounded_pagination_page_numbers(
    dom: dict[str, Any],
) -> tuple[int | None, int | None, bool, bool]:
    """Return bounded current/highest page numbers, supplied, and validity."""
    current = dom.get("pagination_current_page")
    highest = dom.get("pagination_highest_page")
    supplied = current is not None or highest is not None
    valid = (
        isinstance(current, int) and not isinstance(current, bool)
        and 1 <= current <= MAX_PAGINATION_PAGES
        and isinstance(highest, int) and not isinstance(highest, bool)
        and 1 <= highest <= MAX_PAGINATION_PAGES
    )
    return (current if valid else None, highest if valid else None, supplied, valid)


def validate_inbox_coverage(dom: dict[str, Any], previous_count: int | None = None) -> dict[str, Any]:
    """Reject an authenticated-but-unenumerated inbox as collector unhealthy."""
    def unhealthy(reason: str) -> None:
        observed_url = str(dom.get("url") or "")
        is_b1 = (
            urlsplit(observed_url).path == "/message"
            and parse_qs(urlsplit(observed_url).query, keep_blank_values=True).get("fromMyPage") == ["true"]
        )
        raise CollectorUnhealthy(
            reason,
            source_receipt(
                source="b1_inbox" if is_b1 else "direct_inbox",
                requested_url=observed_url or None,
                observed_at=str(dom.get("observed_at") or datetime.now(timezone.utc).isoformat()),
                dom=dom,
                previous_count=previous_count,
            ),
        )

    cards = dom.get("cards") if isinstance(dom.get("cards"), list) else []
    cards = dedupe_inbox_cards([card for card in cards if isinstance(card, dict)])
    ids = {
        str(urlsplit(str(card.get("talkroom_url") or "")).path)
        for card in cards if isinstance(card, dict) and card.get("talkroom_url")
    }
    count = len(ids)
    observed_count = dom.get("cards_count")
    if observed_count is not None and int(observed_count) != count:
        unhealthy("inbox_card_count_mismatch")
    pagination_pages, page_counts, pagination_metadata_supplied = bounded_pagination_metadata(dom)
    if pagination_metadata_supplied and (pagination_pages is None or page_counts is None):
        unhealthy("inbox_pagination_metadata_invalid")
    current_page, highest_page, page_numbers_supplied, page_numbers_valid = bounded_pagination_page_numbers(dom)
    if page_numbers_supplied and not page_numbers_valid:
        unhealthy("inbox_pagination_page_numbers_invalid")
    complete = dom.get("coverage_complete") is True
    reason = str(dom.get("termination_reason") or "")
    if not complete or reason not in {"fixed_point", "pagination_end", "empty_state"}:
        unhealthy("inbox_coverage_incomplete")
    terminal_proven = dom.get("pagination_terminal_proven") is True
    page_count_proves_end = (
        page_counts is not None
        and bool(page_counts)
        and 0 < page_counts[-1] < MAX_PAGE_COUNT
        and sum(page_counts) == count
        and not page_numbers_supplied
    )
    page_number_proves_end = (
        page_numbers_valid and current_page == highest_page
        and page_counts is not None and sum(page_counts) == count
    )
    if reason == "pagination_end" and (
        not pagination_metadata_supplied
        or dom.get("pagination_container_present") is not True
        or dom.get("pagination_current_present") is not True
        or not terminal_proven
        or not (page_count_proves_end or page_number_proves_end)
        or dom.get("pagination_next_present") is not False
    ):
        unhealthy("inbox_pagination_terminal_unproven")
    if dom.get("pagination_next_present") is True and complete:
        unhealthy("inbox_pagination_incomplete")
    if count == 0 and (dom.get("empty_state_present") is not True or reason != "empty_state"):
        unhealthy("inbox_empty_without_semantic_marker")
    if previous_count and count == 0:
        unhealthy("inbox_coverage_collapsed")
    return {
        "cards_count": count,
        "termination_reason": reason,
        "iterations": int(dom.get("iterations") or 0),
        "coverage_complete": True,
        "pagination_pages": pagination_pages,
        "page_counts": page_counts,
        "pagination_terminal_proven": terminal_proven,
        "pagination_current_page": current_page if page_numbers_valid else None,
        "pagination_highest_page": highest_page if page_numbers_valid else None,
    }


def _direct_evidence_files(evidence_root: Path, suffix: str) -> list[Path]:
    return sorted(
        (path for prefix in ("gig-pass-*", "reply-detector-*")
         for path in evidence_root.glob(f"{prefix}/{suffix}")),
        key=lambda path: path.stat().st_mtime_ns, reverse=True,
    )


def previous_coverage_count(evidence_root: Path, current_evidence_dir: Path | None = None) -> int | None:
    """Return newest successful direct receipt from either worker family."""
    receipts = _direct_evidence_files(evidence_root, "live-dom/direct-inbox-route.json")
    for path in receipts:
        if current_evidence_dir is not None and path.parent == current_evidence_dir:
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8")).get("coverage_receipt")
            count = receipt.get("cards_count") if isinstance(receipt, dict) else None
            if receipt.get("coverage_complete") is True and isinstance(count, int) and count >= 0:
                return count
        except (OSError, ValueError, AttributeError):
            continue
    return None


def reusable_semantic_receipt(
    prior: Any, dom: dict[str, Any], semantic_judge: Any, semantic_module: Any,
) -> dict[str, Any] | None:
    """Reuse one current semantic SSOT only while the official conversation is unchanged."""
    receipt = prior.get("semantic_receipt") if isinstance(prior, dict) else None
    current = getattr(semantic_judge, "receipt_current", None)
    judgement = receipt.get("judgement") if isinstance(receipt, dict) else None
    if (
        not callable(current)
        or not current(receipt)
        or not isinstance(judgement, dict)
        or judgement.get("required_official_context") == "application"
    ):
        return None
    try:
        context_sha256 = semantic_module.semantic_context_sha256(
            semantic_module.semantic_conversation(dom)
        )
    except Exception:
        return None
    return receipt if receipt.get("context_sha256") == context_sha256 else None


_DIRECT_READBACK_FIELDS = (
    "last_message_side", "reply_required", "next_action", "buyer_sent_at",
    "seller_sent_at", "message_id", "message_sha256", "stable_ordinal",
    "last_message_identity_sha256", "thread_read_at",
    "estimate_required", "estimate_url", "estimate_request_identity",
    "estimate_request_sent_at", "estimate_request_sha256", "estimate_failure",
    "sending_unavailable", "reply_unavailable_reason",
    "negotiation_intent", "semantic_receipt", "semantic_context_sha256",
    "semantic_reply_body", "semantic_estimate_terms", "semantic_failure",
    "semantic_candidate_action", "semantic_official_context_required",
)
DIRECT_REVALIDATION_HORIZON_SECONDS = 1800
DIRECT_REVALIDATION_BATCH_SIZE = 1
EMPTY_OFFICIAL_CONTEXT_SHA256 = hashlib.sha256(b"null").hexdigest()


def _direct_revalidation_priority(
    prior: dict[str, Any], pending_index: int | None, needs_official_application: bool,
) -> int:
    if pending_index is not None:
        return 0
    if needs_official_application:
        return 1
    if (
        prior.get("next_action") == "semantic_pending"
        and prior.get("semantic_failure") == "semantic_receipt_pending"
    ):
        return 2
    return 3 if prior.get("last_message_side") == "buyer" else 4


def estimate_pending_thread_order(database: Path, manifest: Path) -> dict[str, int]:
    return {
        str(row["thread_id"]): index
        for index, row in enumerate(
            ConnectorOutbox(database, manifest).estimate_pending_actions()
        )
    }


def _valid_direct_readback(row: dict[str, Any]) -> bool:
    side = row.get("last_message_side")
    expected = {"buyer": (True, "reply"), "seller": (False, "observe")}.get(side)
    intent = row.get("negotiation_intent")
    if (
        side == "buyer"
        and intent is None
        and row.get("next_action") not in {"officially_unrepliable", "stop_contact"}
    ):
        return False
    if intent is not None and intent not in {
        "question", "clarify", "ready_to_estimate", "counter",
        "reject", "stop", "concern", "unclear",
        "negotiating", "ready_to_buy", "explicit_estimate_request",
        "gratitude", "considering", "declined", "stop_contact",
        "seller_last", "unknown",
    }:
        return False
    sending_unavailable = row.get("sending_unavailable")
    cache_identity_present = any(
        key in row for key in ("preview_sha256", "last_message_identity_sha256", "thread_read_at")
    )
    if side == "buyer" and cache_identity_present and type(sending_unavailable) is not bool:
        return False
    if sending_unavailable is not None and type(sending_unavailable) is not bool:
        return False
    terminal = None
    if (
        side == "buyer"
        and sending_unavailable is True
        and row.get("reply_unavailable_reason") == "counterparty_restricted"
    ):
        terminal = ("counterparty_restricted", True)
    elif row.get("next_action") == "stop_contact" and side in {"buyer", "seller"}:
        reason = row.get("reply_unavailable_reason")
        if reason not in {
            "buyer_refused", "fraud_or_identity_concern", "buyer_requested_stop",
            "declined", "stop_contact",
        }:
            return False
        terminal = (reason, False)
    if terminal is not None:
        reason, sending_unavailable = terminal
        if (row.get("reply_unavailable_reason") != reason
                or row.get("sending_unavailable") is not sending_unavailable):
            return False
        expected = (False, str(row.get("next_action")))
    elif (
        sending_unavailable is True and side != "seller"
    ) or row.get("reply_unavailable_reason") is not None:
        return False
    semantic_receipt = row.get("semantic_receipt")
    semantic_judgement = (
        semantic_receipt.get("judgement")
        if isinstance(semantic_receipt, dict)
        else None
    )
    if (
        side == "buyer"
        and isinstance(semantic_judgement, dict)
        and semantic_judgement.get("next_action") == "wait"
        and semantic_judgement.get("conversation_state") == intent
        and semantic_judgement.get("uncertainty") == []
    ):
        expected = (False, "observe")
    if row.get("estimate_required") is True:
        expected = (False, "requested_estimate")
        if not re.fullmatch(r"https://coconala\.com/direct_offers/add/[A-Za-z0-9_-]+", str(row.get("estimate_url") or "")):
            return False
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(row.get("estimate_request_identity") or "")):
            return False
        if _optional_sent_at(row.get("estimate_request_sent_at")) is None:
            return False
    elif row.get("next_action") == "ready_to_estimate" and intent == "ready_to_estimate":
        expected = (False, "ready_to_estimate")
    elif row.get("next_action") == "clarify" and side == "buyer":
        expected = (True, "clarify")
    elif row.get("next_action") in {"semantic_failed", "semantic_pending"}:
        expected = (False, str(row.get("next_action")))
    if (expected is None or type(row.get("reply_required")) is not bool
            or (row.get("reply_required"), row.get("next_action")) != expected):
        return False
    identity = bool(
        re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(row.get("message_id") or ""))
        or re.fullmatch(r"[0-9a-f]{64}", str(row.get("message_sha256") or ""))
    )
    sent_at = _optional_sent_at(row.get("buyer_sent_at" if side == "buyer" else "seller_sent_at"))
    return sent_at is not None and identity


def _direct_revalidation_age(row: dict[str, Any], observed_at: str) -> float | None:
    read_at = _optional_sent_at(row.get("thread_read_at"))
    if read_at is None:
        return None
    try:
        age = (datetime.fromisoformat(observed_at) - datetime.fromisoformat(read_at)).total_seconds()
    except ValueError:
        return None
    return age if age >= 0 else None


def previous_direct_snapshot(
    evidence_root: Path, current_evidence_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load only the newest complete, bounded direct readback cache."""
    for snapshot_path in _direct_evidence_files(evidence_root, "marketplace-snapshot.json"):
        if current_evidence_dir is not None and snapshot_path.parent / "live-dom" == current_evidence_dir:
            continue
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return {}
        if not isinstance(snapshot, dict):
            return {}
        if snapshot.get("collector_mode") in {"orders-only", "selected-talkroom-only"}:
            continue
        if "inquiries" not in snapshot:
            return {}
        coverage = snapshot.get("source_receipt")
        if not isinstance(coverage, dict):
            coverage = snapshot.get("direct_inbox_receipt")
        if not isinstance(coverage, dict) or coverage.get("coverage_complete") is not True:
            return {}
        inquiries = snapshot.get("inquiries")
        if not isinstance(inquiries, list):
            return {}
        count = coverage.get("cards_count")
        if type(count) is not int or count < 0 or count != len(inquiries):
            return {}
        valid: dict[str, dict[str, Any]] = {}
        seen_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        for row in inquiries:
            if not isinstance(row, dict):
                continue
            talkroom_id = str(row.get("talkroom_id") or "")
            if re.fullmatch(r"[A-Za-z0-9_-]+", talkroom_id):
                if talkroom_id in seen_ids:
                    duplicate_ids.add(talkroom_id)
                    valid.pop(talkroom_id, None)
                else:
                    seen_ids.add(talkroom_id)
            if talkroom_id in duplicate_ids:
                continue
            if any(key in row for key in ("preview", "preview_text", "raw_preview")):
                continue
            preview_sha256 = str(row.get("preview_sha256") or "")
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]+", talkroom_id)
                or safe_coconala_url(row.get("talkroom_url"))
                != f"https://coconala.com/mypage/direct_message/{talkroom_id}"
                or not re.fullmatch(r"[0-9a-f]{64}", preview_sha256)
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("last_message_identity_sha256") or ""))
                or not _valid_direct_readback(row)
            ):
                continue
            valid[talkroom_id] = row
        return valid
    return {}


def atomic_json(path: Path, value: Any) -> None:
    secure_directory(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(name, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def secure_write_bytes(path: Path, value: bytes) -> None:
    secure_directory(path.parent)
    path.write_bytes(value)
    path.chmod(0o600)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recover_captured_attachment(
    project_root: Path, filename: str,
) -> tuple[str, str, int] | None:
    """Bytes an earlier pass already fetched, found by this module's own naming.

    Captured attachments are written as ``<sha256[:12]>-<filename>`` by
    ``persist_latest_paid_buyer_reply`` below, so that directory is already a
    content-addressed index and each stored name carries the digest to verify
    itself against.  Order 91000002, 2026-08-07: five images downloaded at 22:00
    were recorded at 22:21 as ``attachment_download_not_observed`` while sitting
    on disk, because nothing asked the disk.

    Matched by exact suffix rather than by glob: buyer filenames are arbitrary
    text, and a ``[`` in one would quietly change what a pattern means.

    One filename can name two different stored files -- storage is keyed by
    content, not by name.  Choosing between them would be a guess, so the answer
    is ``None``: the same answer as never having fetched it, which is true.
    """
    directory = project_root / "source" / "buyer-attachments"
    suffix = f"-{filename}"
    found: dict[str, tuple[str, int]] = {}
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.name.endswith(suffix):
            continue
        prefix = path.name[: -len(suffix)]
        if not re.fullmatch(r"[0-9a-f]{12}", prefix) or not path.is_file():
            continue
        try:
            digest = sha256_file(path)
            size = path.stat().st_size
        except OSError:
            continue
        if digest.startswith(prefix):
            found[digest] = (str(path), size)
    if len(found) != 1:
        return None
    digest, (stored_path, size) = next(iter(found.items()))
    return stored_path, digest, size


def buyer_request_identity(manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the buyer sent, with our fetch quality taken back out.

    ``feedback_sha256`` answers exactly one question -- is this the same request
    the buyer made -- so anything in the hashed input that we author rather than
    observe makes the answer depend on the hour.  Measured on 91000002,
    2026-08-07: one buyer message, unchanged and never re-sent, hashed 832bd54f
    at 22:00 and 47ae9126 at 22:21.  The whole difference was five
    ``capture_error`` strings, five null ``source_path``s, and five sizes that
    had been multiplied out of a display label.  2f12101e records a confirmed
    delivery against the digest of the moment, so the next reading of the same
    message parsed as a new request and the order rebuilt -- the loop that
    commit exists to end.

    Two fields survive, and both belong to the buyer:
      * ``filename`` -- what they called it, already echoed into feedback_text;
      * ``download_reference`` -- coconala's own address for that attachment on
        that message (``message:<message-id>:attachment:<n>``).

    The content hash is deliberately NOT here, though it is the better identity
    for a *file*.  It is knowable only after a successful fetch while the request
    exists before the fetch, so including it would move the digest the first time
    an old attachment finally downloads -- the same defect deferred rather than
    fixed.  A sent message cannot be edited on coconala, so different bytes mean
    a different message mean a different reference: the reference already carries
    the discrimination the hash would add, and carries it from first sight.  The
    hash stays in the manifest for readers who want the bytes.

    Where no reference can be parsed this degrades to filename, which is the
    discrimination ``feedback_text`` already provides by itself.  No order
    observed on 2026-08-07 was in that state.
    """
    return [
        {
            "filename": row.get("filename"),
            "download_reference": row.get("download_reference"),
        }
        for row in manifest
    ]


def safe_filename(value: Any) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "attachment")).strip()[:255]
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    return text or "attachment"


def safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    text = re.sub(r"https?://\S+", "[redacted-url]", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def safe_coconala_url(value: Any) -> str | None:
    parsed = urlsplit(str(value or ""))
    if parsed.hostname not in ("coconala.com", "www.coconala.com"):
        return None
    query = ""
    if parsed.path == "/message" and parse_qs(parsed.query, keep_blank_values=True).get("fromMyPage") == ["true"]:
        query = "?fromMyPage=true"
    return f"https://coconala.com{parsed.path}{query}"


def source_receipt(
    *,
    source: str,
    requested_url: Any,
    observed_at: str,
    dom: dict[str, Any],
    previous_count: int | None = None,
) -> dict[str, Any]:
    """Build a bounded, secret-free receipt for one authenticated source read."""
    cards = dom.get("cards") if isinstance(dom.get("cards"), list) else None
    cards_count = None
    if cards is not None:
        cards_count = len(dedupe_inbox_cards([card for card in cards if isinstance(card, dict)]))
    current_url = str(dom.get("url") or "")
    title = str(dom.get("title") or "")
    login_redirect = None
    if current_url or title:
        login_redirect = urlsplit(current_url).path.startswith("/login") or "ログイン" in title
    termination_reason = str(dom.get("termination_reason") or "")
    if termination_reason not in {"fixed_point", "pagination_end", "empty_state"}:
        termination_reason = None
    pagination_pages, page_counts, _ = bounded_pagination_metadata(dom)
    current_page, highest_page, _, _ = bounded_pagination_page_numbers(dom)
    return {
        "version": 1,
        "source": str(source),
        "requested_route": safe_coconala_url(requested_url),
        "final_route": safe_coconala_url(current_url),
        "login_redirect": login_redirect,
        "container_found": dom.get("container_present") if isinstance(dom.get("container_present"), bool) else None,
        "cards_count": cards_count,
        "empty_state_present": dom.get("empty_state_present") if isinstance(dom.get("empty_state_present"), bool) else None,
        "coverage_complete": dom.get("coverage_complete") is True,
        "termination_reason": termination_reason,
        "iterations": int(dom["iterations"]) if isinstance(dom.get("iterations"), int) else None,
        "previous_count": previous_count if isinstance(previous_count, int) else None,
        "pagination_pages": pagination_pages,
        "page_counts": page_counts,
        "pagination_terminal_proven": dom.get("pagination_terminal_proven") if isinstance(dom.get("pagination_terminal_proven"), bool) else None,
        "pagination_current_page": current_page,
        "pagination_highest_page": highest_page,
        "selector_version": "coconala-queue-snapshot-v1",
        "observed_at": str(observed_at),
    }
def safe_download_reference(href: Any, fallback: Any = None) -> str | None:
    if href:
        parsed = urlsplit(str(href))
        if parsed.hostname in ("coconala.com", "www.coconala.com") and re.fullmatch(
            r"/(?:uploaded_files|attachments)/[A-Za-z0-9_./-]+", parsed.path
        ):
            return parsed.path
    candidate = str(fallback or "")
    if re.fullmatch(r"message:[A-Za-z0-9_-]+:attachment:\d+", candidate):
        return candidate
    return None


def contact_deadline_from_notice(notice: Any, observed_at: str) -> str | None:
    """Read coconala's own auto-cancel deadline out of the banner it renders.

    ★ Read, never compute. ★ The banner writes the deadline as 「期限：8/9 23:00」 -- a
    JST wall clock with no year, because the platform only ever shows a date days away.
    The year is the single thing not on the page, so it is taken from the observation
    itself and rolled forward only across a year boundary (a banner seen on 12/31 that
    says 1/2 means next year). Everything else -- month, day, hour, minute, and the fact
    that a clock is running at all -- comes from the marketplace that enforces it.

    Returns None whenever the banner is absent or unparseable. Absent is the normal case:
    coconala stops rendering it once any seller message exists in the room, so None means
    either "no clock" or "clock already satisfied", and both mean the same to the queue.
    """
    match = CONTACT_DEADLINE.search(str(notice or ""))
    if not match:
        return None
    year_text, month, day, hour, minute = match.groups()
    try:
        observed = datetime.fromisoformat(str(observed_at)).astimezone(JST)
    except (TypeError, ValueError):
        return None
    try:
        if year_text:
            deadline = datetime(
                int(year_text), int(month), int(day), int(hour), int(minute), tzinfo=JST
            )
        else:
            deadline = datetime(
                observed.year, int(month), int(day), int(hour), int(minute), tzinfo=JST
            )
            # Only a December->January wrap justifies moving the year. Anything else that
            # lands in the past is a stale or misread banner, and inventing a year for it
            # would manufacture a deadline eleven months away that nobody is enforcing.
            if (deadline - observed).days < -300:
                deadline = deadline.replace(year=observed.year + 1)
    except ValueError:
        return None
    return deadline.isoformat()


def formal_delivery_from_dom(talkroom: dict[str, Any]) -> bool:
    # The checkbox is cleared after a successful submit.  The post-submit
    # transaction step is the durable live-DOM ground truth; while still in
    # 取引中, an unrelated checked checkbox must never count as delivery.
    return talkroom.get("transaction_state") == "納品確認待ち"


SELLER_SENT_MESSAGE_LIMIT = 10
BUYER_RECENT_MESSAGE_LIMIT = 10


def minimize_talkroom_dom(talkroom: dict[str, Any], talkroom_id: str, observed_at: str) -> dict[str, Any]:
    messages = talkroom.get("messages") if isinstance(talkroom.get("messages"), list) else []
    latest_actionable = -1
    latest_seller_attachment = -1
    buyer_attachments: list[dict[str, Any]] = []
    buyer_message_indexes: list[int] = []
    buyer_agreement_indexes: list[int] = []
    seller_message_observed = False
    # A3: what we sent, not just that we sent something. seller_message_observed
    # stays a bool for the existing contact-rule check; this is the readback of
    # the actual text/attachments so a delivered file like
    # sample-game-guide-v1.docx shows up as sent, not just inferred.
    seller_sent_messages: list[dict[str, Any]] = []
    # A4: mirror of A3 on the buyer side. Same loop, same fields, so a revision
    # request with attachments (e.g. IMG_0001/IMG_0002) shows up as a readback
    # entry instead of only feeding the existing actionable/attachment flags.
    buyer_recent_messages: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
        if message.get("side") == "seller":
            # A5: coconala's contact rule is satisfied by ANY seller message, with or
            # without a file. latest_seller_attachment answers a different question --
            # has the buyer seen an artifact -- and stays -1 in a room where we have
            # written five times in plain text, so it cannot stand in for "we spoke".
            seller_message_observed = True
            full_text = " ".join(str(message.get("text") or "").split())
            seller_sent_messages.append({
                "text": safe_text(message.get("text"), 300),
                "text_sha256": hashlib.sha256(full_text.encode()).hexdigest(),
                "attachments": [
                    safe_filename(attachment.get("filename"))
                    for attachment in attachments if isinstance(attachment, dict)
                ],
                "sent_at_label": safe_text(message.get("sent_at"), 64) or None,
            })
        if message.get("side") == "seller" and attachments:
            latest_seller_attachment = index
        if message.get("side") == "buyer" and (ACTIONABLE.search(str(message.get("text") or "")) or attachments):
            latest_actionable = index
        if message.get("side") == "buyer":
            buyer_message_indexes.append(index)
            buyer_recent_messages.append({
                "text": safe_text(message.get("text"), 300),
                "attachments": [
                    safe_filename(attachment.get("filename"))
                    for attachment in attachments if isinstance(attachment, dict)
                ],
                "sent_at_label": safe_text(message.get("sent_at"), 64) or None,
            })
            if AGREEMENT.search(str(message.get("text") or "")):
                buyer_agreement_indexes.append(index)
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                filename = safe_filename(attachment.get("filename"))
                content_type = str(attachment.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
                buyer_attachments.append({
                    "filename": filename,
                    "content_type": content_type[:100],
                    # Only the capture paths set ``size_bytes``, and both set it
                    # from the bytes they hold.  The page's own label is kept as
                    # a label; multiplying it out made 2306867 of a 2124659-byte
                    # file and reported that as if it had been measured.
                    "size_bytes": attachment.get("size_bytes"),
                    "size_display": safe_text(attachment.get("size_text"), 32) or None,
                    "download_reference": safe_download_reference(attachment.get("href"), attachment.get("reference")),
                })
    seller_attachment_after = latest_seller_attachment > latest_actionable
    buyer_agreement_observed = any(index > latest_seller_attachment for index in buyer_agreement_indexes)
    buyer_reply_after_artifact_observed = latest_seller_attachment >= 0 and any(
        index > latest_seller_attachment for index in buyer_message_indexes
    )
    buyer_formal_delivery = buyer_formal_delivery_directive(messages)
    parsed_url = urlsplit(str(talkroom.get("url") or ""))
    # A5 (docs/loop-engineering/26-gig-loop-asis-tobe-plan.md section CC'): the room's
    # own type -- 定期購入 (subscription, e.g. talkroom 90000004 / 買い手C) vs 一括
    # (one_shot) -- lived nowhere in state, so nothing downstream could tell a
    # subscription room apart from a stuck one-shot room. subscription_control_present
    # is the site's own UI control (an anchor/button whose exact text is
    # 定期購入を終了する) -- the same selector coconala_formal_delivery_browser.py's
    # state_expression already uses to detect subscription rooms -- never page text,
    # which a buyer message could echo. A 定期購入 room has no step bar, so its
    # transaction_state is always "unknown"; a real (non-unknown) transaction_state is
    # therefore only ever seen on a one-shot room. Absent both signals, fail closed to
    # "unknown" rather than guess.
    if talkroom.get("subscription_control_present") is True:
        room_contract_kind = "subscription"
    elif str(talkroom.get("transaction_state") or "unknown") != "unknown":
        room_contract_kind = "one_shot"
    else:
        room_contract_kind = "unknown"
    return {
        "talkroom_id": str(talkroom_id),
        "url": f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}" if parsed_url.netloc else None,
        "transaction_state": talkroom.get("transaction_state") or "unknown",
        # What "unknown" was derived from. A subscription room legitimately has no step bar, so
        # this separates "no step bar" from "a label nothing maps yet" without reading the code -
        # a distinction that cost two deploys to make from the outside.
        "talkroom_step_label": safe_text(talkroom.get("talkroom_step_label"), 40),
        "delivery_date": iso_date(talkroom.get("delivery_date")),
        "formal_delivery_control_checked": talkroom.get("formal_delivery_control_checked") is True,
        "formal_delivery_control_disabled": talkroom.get("formal_delivery_control_disabled") is True,
        "room_contract_kind": room_contract_kind,
        "compose_draft_length": int(talkroom.get("compose_draft_length") or 0),
        "compose_draft_text": safe_text(talkroom.get("compose_draft_text"), 500),
        "formal_delivery_confirmed": formal_delivery_from_dom(talkroom),
        "buyer_feedback_pending_artifact": latest_actionable > latest_seller_attachment,
        "buyer_visible_artifact_observed": seller_attachment_after,
        "buyer_agreement_observed": buyer_agreement_observed,
        "buyer_reply_after_artifact_observed": buyer_reply_after_artifact_observed,
        **buyer_formal_delivery,
        "buyer_attachments": buyer_attachments,
        # A5: the marketplace's own cancellation clock, kept verbatim next to the parsed
        # value so a later reader can check our parse against the sentence it came from.
        "seller_message_observed": seller_message_observed,
        "seller_sent_messages": seller_sent_messages[-SELLER_SENT_MESSAGE_LIMIT:],
        "buyer_recent_messages": buyer_recent_messages[-BUYER_RECENT_MESSAGE_LIMIT:],
        "auto_cancel_notice": safe_text(talkroom.get("auto_cancel_notice"), 300) or None,
        "contact_deadline": contact_deadline_from_notice(
            talkroom.get("auto_cancel_notice"), observed_at
        ),
        "offer_reference": safe_download_reference(talkroom.get("offer_url")) or (
            urlsplit(str(talkroom.get("offer_url") or "")).path or None
        ),
        "observed_at": observed_at,
    }


def persist_talkroom_history(
    talkroom: dict[str, Any], project_id: str, projects_root: Path,
    talkroom_id: str, observed_at: str,
) -> dict[str, Any]:
    """Append newly observed raw talkroom messages to the stable project."""
    if talkroom.get("history_complete") is not True:
        raise CollectorUnhealthy("talkroom_history_incomplete")
    messages = talkroom.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CollectorUnhealthy("talkroom_history_empty")
    root = projects_root.expanduser().resolve() / str(project_id)
    ledger = root / "source" / "talkroom" / "messages.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger.with_suffix(ledger.suffix + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _persist_talkroom_history_locked(
            talkroom, ledger, talkroom_id, observed_at, messages)


def _persist_talkroom_history_locked(
    talkroom: dict[str, Any], ledger: Path, talkroom_id: str,
    observed_at: str, messages: list[Any],
) -> dict[str, Any]:
    known: set[str] = set()
    try:
        for line in ledger.read_text(encoding="utf-8").split("\n"):
            row = json.loads(line)
            if isinstance(row, dict) and isinstance(row.get("content_sha256"), str):
                known.add(row["content_sha256"])
    except (OSError, json.JSONDecodeError):
        pass
    appended: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        attachments = []
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            attachments.append({
                key: attachment.get(key)
                for key in ("filename", "content_type", "size_text", "href", "reference")
            })
        canonical = {
            "side": message.get("side"),
            "sent_at": message.get("sent_at"),
            "text": str(message.get("text") or ""),
            "attachments": attachments,
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        content_sha = hashlib.sha256(encoded).hexdigest()
        if content_sha in known:
            continue
        message_id = str(message.get("message_id") or f"sha256:{content_sha}")
        appended.append({
            "version": 1,
            "source": "coconala_live_talkroom",
            "talkroom_id": str(talkroom_id),
            "message_id": message_id,
            "observed_at": observed_at,
            "content_sha256": content_sha,
            **canonical,
        })
        known.add(content_sha)
    if appended:
        with ledger.open("a", encoding="utf-8") as handle:
            for row in appended:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    _, ledger_sha = _bytes_sha_for_history(ledger)
    return {
        "history_complete": True,
        "message_count": len(messages),
        "new_message_count": len(appended),
        "messages_path": str(ledger),
        "messages_sha256": ledger_sha,
    }


def talkroom_with_persisted_history(
    talkroom: dict[str, Any], project_id: str, projects_root: Path,
    talkroom_id: str,
) -> dict[str, Any]:
    """Use the append-only official history, preferring fresh captured bytes."""
    root = projects_root.expanduser().resolve() / str(project_id)
    ledger = root / "source" / "talkroom" / "messages.jsonl"
    current: dict[str, dict[str, Any]] = {}
    for message in talkroom.get("messages") or []:
        if not isinstance(message, dict):
            continue
        attachments = [{
            key: attachment.get(key)
            for key in ("filename", "content_type", "size_text", "href", "reference")
        } for attachment in message.get("attachments") or [] if isinstance(attachment, dict)]
        canonical = {
            "side": message.get("side"), "sent_at": message.get("sent_at"),
            "text": str(message.get("text") or ""), "attachments": attachments,
        }
        digest = hashlib.sha256(json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        current[digest] = message
    merged: list[dict[str, Any]] = []
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or str(row.get("talkroom_id") or "") != str(talkroom_id):
            continue
        fresh = current.pop(str(row.get("content_sha256") or ""), None)
        merged.append({**fresh, "observed_at": row.get("observed_at")} if fresh else row)
    merged.extend(current.values())
    return {**talkroom, "messages": merged, "history_complete": True, "message_count": len(merged)}


def _paid_message_content_sha256(message: dict[str, Any]) -> str:
    """Return the capture-independent content key used by the talkroom ledger."""
    attachments = [
        {
            key: attachment.get(key)
            for key in ("filename", "content_type", "size_text", "href", "reference")
        }
        for attachment in message.get("attachments") or []
        if isinstance(attachment, dict)
    ]
    canonical = {
        "side": message.get("side"),
        "sent_at": message.get("sent_at"),
        "text": str(message.get("text") or ""),
        "attachments": attachments,
    }
    return hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _durable_paid_messages(
    project_root: Path, talkroom_id: str, current: Any,
) -> list[dict[str, Any]]:
    """Merge the current capture with the append-only official talkroom ledger.

    Coconala can render only the newest slice of a long room.  The seller-attachment
    cursor and the feedback digest must therefore run over the durable history, not
    over whichever slice happened to be visible in this poll.  The ledger's content
    key is the same one written by ``persist_talkroom_history``; the fresh row wins
    so a newly captured attachment body is available without changing identity.
    """
    current_rows = [row for row in current if isinstance(row, dict)] if isinstance(current, list) else []
    by_content = {_paid_message_content_sha256(row): row for row in current_rows}
    ledger = project_root / "source" / "talkroom" / "messages.jsonl"
    merged: list[dict[str, Any]] = []
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or str(row.get("talkroom_id") or "") != str(talkroom_id):
            continue
        content_sha = str(row.get("content_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha):
            content_sha = _paid_message_content_sha256(row)
        fresh = by_content.pop(content_sha, None)
        if fresh is None:
            merged.append(row)
        else:
            merged.append({**row, **fresh, "observed_at": row.get("observed_at") or fresh.get("observed_at")})
    merged.extend(by_content.values())
    return merged or current_rows


def _stable_paid_message_identity(message: dict[str, Any]) -> str:
    """Return an opaque, durable identity for one buyer-authored message."""
    message_id = str(message.get("message_id") or "").strip()
    if message_id and message_id != "unknown":
        return f"message:{message_id}"
    return f"content:{_paid_message_content_sha256(message)}"


def _stable_paid_feedback_digest(
    talkroom_id: str, stage: str, buyer_messages: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Hash message identities, never capture timestamps or download outcomes."""
    identities = [_stable_paid_message_identity(message) for message in buyer_messages]
    payload = {
        "version": 1,
        "talkroom_id": str(talkroom_id),
        "stage": stage,
        "message_identities": identities,
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return digest, identities


def _bytes_sha_for_history(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _sanitize_paid_feedback(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[redacted-email]", text)

    def strip_url_secrets(match: re.Match[str]) -> str:
        parsed = urlsplit(match.group(0))
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    text = re.sub(r"https?://[^\s]+", strip_url_secrets, text)
    return text[:8000].strip()


def _buyer_statement_rows(messages: Any, observed_at: str) -> list[dict[str, Any]]:
    """Every buyer statement in this snapshot, sanitized and content-addressed.

    Deliberately NOT windowed. The seller-attachment cursor answers "which request
    is current"; it never answers "which facts are requirements". Order 91000002,
    2026-08-07: the buyer stated the deck size and the source photos once, we
    delivered, and the next snapshot -- which only kept messages after our own
    attachment -- overwrote the file that held them. A fact stated once has to
    survive every later poll, so accumulation reads the whole conversation.
    """
    rows: list[dict[str, Any]] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("side") != "buyer":
            continue
        text = _sanitize_paid_feedback(message.get("text"))
        names: list[str] = []
        for attachment in message.get("attachments") or []:
            if isinstance(attachment, dict):
                names.append(safe_filename(attachment.get("filename")))
        if not text and not names:
            continue
        canonical = {"text": text, "attachments": names}
        rows.append({
            "sha256": hashlib.sha256(json.dumps(
                canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "sent_at": str(message.get("sent_at") or "") or None,
            # A live capture carries no observation time of its own, so the poll's
            # is the first sighting. Our append-only talkroom ledger DOES carry one
            # per message, and it is the earlier and truer answer -- without this,
            # re-seeding from the ledger would stamp every recovered message with
            # today's clock and call a months-old request brand new.
            "first_observed_at": str(message.get("observed_at") or "") or observed_at,
            "text": text,
            "attachments": names,
        })
    return rows


def _ledger_statement_rows(project_root: Path, observed_at: str) -> list[dict[str, Any]]:
    """Seed the accumulation from the talkroom ledger this project already keeps.

    ``persist_talkroom_history`` has been appending every observed message since long
    before requirements accumulated, so an order that starts accumulating today would
    otherwise begin with only what the current capture window still renders -- for a
    delivered order, that is nothing the buyer said before the delivery. Reading our own
    append-only ledger recovers them without a single extra page load.
    """
    ledger = project_root / "source" / "talkroom" / "messages.jsonl"
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    messages: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            messages.append(row)
    return _buyer_statement_rows(messages, observed_at)


def _merge_accumulated(
    existing: Any, snapshot_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Union of what we already recorded and what this snapshot shows, in order.

    Content-addressed like ``persist_talkroom_history``: a repeated poll of the
    same message adds nothing, and ``first_observed_at`` keeps the earliest
    sighting rather than the latest.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in existing if isinstance(existing, list) else []:
        if not isinstance(row, dict):
            continue
        normalized = {
            **row,
            "text": str(row.get("text") or ""),
            "attachments": row.get("attachments") if isinstance(row.get("attachments"), list) else [],
        }
        digest = hashlib.sha256(json.dumps(
            {"text": normalized["text"], "attachments": normalized["attachments"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        normalized["sha256"] = digest
        seen.add(digest)
        merged.append(normalized)
    for row in snapshot_rows:
        if row["sha256"] in seen:
            continue
        seen.add(row["sha256"])
        merged.append(row)
    digest = hashlib.sha256(json.dumps(
        [row.get("sha256") for row in merged],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return merged, digest


_LEGACY_SOURCE_STAGE = {
    "latest_buyer_reply_after_artifact": "revision",
    "latest_buyer_request_before_first_delivery": "initial_request",
}


def _existing_stage(payload: Any) -> str | None:
    """Stage of a requirements file already on disk, including pre-C3a files.

    Files written before C3a carry no ``buyer_feedback_stage``, but the old code
    could only ever produce ``latest_buyer_reply_after_artifact`` -- so their
    ``source`` names the stage exactly. Live project 90000000's sidecar is one of
    these. Reading them as stage-less would both bump their mtime on the first
    unchanged poll and, worse, let a truncated capture demote them.
    """
    if not isinstance(payload, dict):
        return None
    stage = payload.get("buyer_feedback_stage")
    if isinstance(stage, str) and stage:
        return stage
    return _LEGACY_SOURCE_STAGE.get(str(payload.get("source") or ""))


def _existing_requirements(requirements_path: Path) -> dict[str, Any]:
    """The sidecar already on disk, or ``{}`` when there is none to read."""
    try:
        payload = json.loads(requirements_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _request_named_by_existing_sidecar(requirements_path: Path) -> dict[str, Any] | None:
    """Re-surface the current request the durable sidecar already names.

    Returning ``None`` from the truncated-capture branch in
    ``persist_latest_paid_buyer_reply`` used to be harmless because nothing
    downstream needed a digest. It is not harmless now.
    ``buyer_feedback_pending_artifact`` is computed by ``minimize_talkroom_dom``
    from the SAME capture window that lost our attachment, so it stays True,
    while the order carries no ``buyer_feedback_sha256`` at all -- and
    ``delivery_cadence._buyer_feedback_processed()`` cannot return true without
    one. "Pending with no digest" is then a blocker no amount of building can
    ever clear: measured 2026-08-07 on order 90000004 (JPY 2,500, paid,
    delivered), which was routed to ``work_required`` and rebuilt a new artifact
    version at 09:01, 11:01 and 20:02 and would never have stopped on its own.

    Nothing is written and nothing is inferred. The sidecar on disk is the
    durable record of what the buyer currently asked for, written by this same
    function on a poll whose capture window did include our attachment; naming
    it again reports a fact we already hold. If it names no request, the honest
    answer is still ``None``.
    """
    payload = _existing_requirements(requirements_path)
    digest = str(payload.get("feedback_sha256") or "")
    stage = _existing_stage(payload)
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or not stage:
        return None
    return {
        "requirements_path": str(requirements_path),
        "feedback_sha256": digest,
        "feedback_identity_sha256": str(payload.get("feedback_identity_sha256") or "") or None,
        "feedback_message_identities": payload.get("feedback_message_identities") or [],
        "stage": stage,
    }


def request_first_observed_at(
    accumulated: Any, current_rows: list[dict[str, Any]], observed_at: str,
) -> str:
    """When the buyer last added to what they are asking for right now.

    ``observed_at`` is OUR clock.  It moves every time the sidecar is rewritten,
    including rewrites that change nothing the buyer said, and
    ``delivery_cadence._buyer_feedback_processed`` compares the finished
    artifact's mtime against it.  Order 91000002 measured 2026-08-08 is what that
    costs: one buyer message, sent once at 22:00 JST on 2026-08-07 and never
    re-sent, was re-hashed at 23:31 when c4d73e52 changed what goes into the
    digest.  The digest changed, so the unchanged-poll early return below did not
    fire, so the rewrite carried a fresh ``observed_at`` -- and v5, finished and
    accepted at 22:07, seven minutes AFTER the buyer asked, became retroactively
    older than the request it answered.  The order read as unprocessed feedback
    and went back to the builder every pass for the next nine hours with the
    finished file sitting on disk.  ★ A6 stabilised the digest; this stabilises
    the clock the digest is read next to, which is the same defect from the other
    side. ★

    Nothing new is measured.  ``_merge_accumulated`` is content-addressed and
    already keeps the EARLIEST ``first_observed_at`` for every buyer statement,
    and the current request is made of those same statements -- so its age is a
    fact about the buyer that survives every rewrite, including a change to the
    hashing rule, because the rows are keyed on text and filenames rather than on
    the digest.

    ★ LATEST, not earliest. ★ The window that defines the current request grows
    when the buyer speaks again, and the earliest sighting of the oldest sentence
    in it would then backdate a request they have just added to -- declaring an
    older artifact an answer to something written after it.  The most recent
    statement in the request is the one an artifact has to postdate.

    Falls back to ``observed_at`` when the accumulation cannot name every current
    statement: a request whose age we cannot establish is treated as new rather
    than quietly backdated.
    """
    wanted = {str(row.get("sha256") or "") for row in current_rows}
    wanted.discard("")
    if not wanted:
        return observed_at
    seen: dict[str, str] = {}
    for row in accumulated if isinstance(accumulated, list) else []:
        if not isinstance(row, dict):
            continue
        digest = str(row.get("sha256") or "")
        first = str(row.get("first_observed_at") or "")
        if digest in wanted and first:
            seen.setdefault(digest, first)
    if len(seen) != len(wanted):
        return observed_at
    return max(seen.values())


def _first_delivery_already_happened(project_root: Path, talkroom: dict[str, Any]) -> bool:
    """Authoritative readback that this order is past its first delivery.

    Either witness is derived from marketplace state:
      * the live transaction is in the post-formal-delivery state;
      * a requirements sidecar written from an observed seller attachment says
        ``revision``.

    A file in the local ``artifacts/`` directory is deliberately not evidence:
    the builder creates it before the browser lane sends anything to the buyer.

    Total by construction: any unreadable state means "we cannot corroborate a
    delivery", which leaves the caller on its normal path. This runs inside the
    collector's single try/except, where a raise fails the whole snapshot.
    """
    try:
        existing = json.loads(
            (project_root / "requirements" / "live-buyer-reply.json").read_text(encoding="utf-8")
        )
        if _existing_stage(existing) == "revision":
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return formal_delivery_from_dom(talkroom)


def persist_latest_paid_buyer_reply(
    talkroom: dict[str, Any], project_id: str, projects_root: Path, observed_at: str,
    *, source_talkroom_id: str | None = None,
) -> dict[str, Any] | None:
    """Store buyer work input only in the owner-only stable project requirements.

    Marketplace snapshots remain bounded and never contain raw message bodies.
    The sidecar is content-addressed and is not rewritten on an unchanged poll.

    Two stages, one file. C3a (measured 2026-08-01 on order 90000004, ¥2,500,
    paid and undelivered): this is the ONLY writer of buyer text into a project,
    and it used to hard-require a prior *seller attachment*. An order we have
    never delivered to has none, so the buyer's opening request was written
    nowhere -- while the queue still routed the order into PAID_WORK and told the
    builder to work from requirements that did not exist. The seller-attachment
    cursor is now what it always meant: the boundary after which a buyer message
    is a revision. Before the first delivery there is no boundary, and the latest
    buyer message is the requirement.

    ``stage`` is ``initial_request`` (building v1) or ``revision`` (the buyer has
    already seen an artifact) and travels to the builder in the bounded packet.
    """
    identity = str(project_id)
    if identity in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", identity):
        raise CollectorUnhealthy("invalid_paid_project_identity")
    project_root = projects_root.expanduser().resolve() / identity
    messages = _durable_paid_messages(
        project_root, str(source_talkroom_id or project_id), talkroom.get("messages"),
    )
    requirements_path = project_root / "requirements" / "live-buyer-reply.json"
    existing_payload = _existing_requirements(requirements_path)
    previous = existing_payload.get("accumulated_requirements")
    # A list we cannot read is worth exactly as much as no list. Live 91000002 was
    # found on 2026-08-08 carrying accumulated_requirements as bare strings, and
    # later carrying none at all -- neither shape has a sha256 to match a current
    # statement against, so request_first_observed_at above would fall back to
    # "now" and discard a finished v6 on the next poll. Our own append-only
    # message ledger still holds when each of those sentences was first seen, so
    # the recovery is a re-read of a durable local fact, not a new measurement.
    if not any(
        isinstance(row, dict) and row.get("sha256")
        for row in (previous if isinstance(previous, list) else [])
    ):
        previous = _ledger_statement_rows(project_root, observed_at)
    accumulated, accumulated_sha256 = _merge_accumulated(
        previous, _buyer_statement_rows(messages, observed_at),
    )

    def keep_accumulation_only() -> None:
        """Grow the ledger without touching the current-request fields.

        Every branch below that cannot name a current request still has to keep
        what the buyer said. Only an existing sidecar is updated: creating one
        here would invent a requirements file for an order that has none, which
        is the failure the caller's ``None`` return exists to report.
        """
        if not existing_payload or accumulated_sha256 == existing_payload.get("accumulated_sha256"):
            return
        atomic_json(requirements_path, {
            **existing_payload,
            "accumulated_requirements": accumulated,
            "accumulated_sha256": accumulated_sha256,
            "accumulated_observed_at": observed_at,
        })

    latest_seller_attachment = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
        if message.get("side") == "seller" and attachments:
            latest_seller_attachment = index
    if latest_seller_attachment < 0 and _first_delivery_already_happened(project_root, talkroom):
        # The stage is monotonic. TALKROOM_EXPRESSION maps only the messages the
        # page currently renders, and coconala lazy-loads older ones on scroll-up,
        # so in a long talkroom our own past attachment can fall outside the
        # capture window. Demoting to initial_request on that would OVERWRITE the
        # buyer's current feedback with a pre-delivery message -- the only record
        # of what they asked for -- and tell the builder there is no prior
        # artifact. A capture that cannot see a delivery we know happened is a
        # capture defect, not news. Before C3a this same miss was a harmless
        # `return None`; keep it harmless -- which now means naming the request
        # the sidecar already holds instead of dropping it (see
        # _request_named_by_existing_sidecar). A capture defect must not read
        # downstream as "the buyer's current request is unknown".
        keep_accumulation_only()
        return _request_named_by_existing_sidecar(requirements_path)
    if latest_seller_attachment < 0:
        # Nothing has been delivered yet: every buyer message is still the brief,
        # so the latest one is the current requirement.
        stage = "initial_request"
        source = "latest_buyer_request_before_first_delivery"
        candidates = messages
    else:
        # Unchanged revision path: a buyer message that predates our artifact is
        # stale, and only what they said AFTER seeing it is actionable feedback.
        stage = "revision"
        source = "latest_buyer_reply_after_artifact"
        candidates = messages[latest_seller_attachment + 1:]
    buyer_messages = [
        message for message in candidates
        if isinstance(message, dict) and message.get("side") == "buyer"
    ]
    if not buyer_messages:
        keep_accumulation_only()
        # The current DOM window can omit older buyer rows even though the complete request was
        # already captured durably.  Re-name that exact sidecar so the Paid parent can resume its
        # project owner; returning None silently strands the room as pending forever.
        return _request_named_by_existing_sidecar(requirements_path)
    feedback_parts: list[str] = []
    attachment_manifest: list[dict[str, Any]] = []
    for message in buyer_messages:
        body = _sanitize_paid_feedback(message.get("text"))
        if body:
            feedback_parts.append(body)
        attachments = message.get("attachments")
        if not isinstance(attachments, list):
            continue
        names: list[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            filename = safe_filename(attachment.get("filename"))
            names.append(filename)
            captured_path: str | None = None
            captured_sha256: str | None = None
            captured_size: int | None = None
            encoded = attachment.get("data_base64")
            if isinstance(encoded, str) and encoded:
                try:
                    captured_bytes = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    captured_bytes = b""
                if captured_bytes and len(captured_bytes) <= 16 * 1024 * 1024:
                    captured_sha256 = hashlib.sha256(captured_bytes).hexdigest()
                    source_path = project_root / "source" / "buyer-attachments" / (
                        f"{captured_sha256[:12]}-{filename}"
                    )
                    if not source_path.is_file() or sha256_file(source_path) != captured_sha256:
                        secure_write_bytes(source_path, captured_bytes)
                    captured_path = str(source_path)
                    captured_size = len(captured_bytes)
            if captured_path is None:
                # This pass did not fetch it; an earlier pass may already have.
                # Asking the disk is what turns "we failed to download" back into
                # the truth, which is that the file has been here since 22:00.
                recovered = recover_captured_attachment(project_root, filename)
                if recovered is not None:
                    captured_path, captured_sha256, captured_size = recovered
            attachment_manifest.append({
                "filename": filename,
                "content_type": str(
                    attachment.get("content_type") or "application/octet-stream"
                )[:100],
                # A measurement of bytes we hold, or nothing.  There is no third
                # option: the page renders "2.2MB", and 2.2 * 1048576 = 2306867
                # is a rounded label wearing the costume of an observation.
                "size_bytes": captured_size,
                "size_display": safe_text(attachment.get("size_text"), 32) or None,
                "download_reference": safe_download_reference(
                    attachment.get("href"), attachment.get("reference")
                ),
                "source_path": captured_path,
                "sha256": captured_sha256,
                "capture_error": attachment.get("capture_error") if captured_path is None else None,
            })
        if names:
            feedback_parts.append("添付ファイル: " + "、".join(names))
    feedback_text = "\n\n".join(feedback_parts).strip()
    if not feedback_text:
        keep_accumulation_only()
        return None
    legacy_feedback_sha256 = hashlib.sha256(json.dumps({
        "feedback_text": feedback_text,
        # The envelope is left exactly as it was on purpose.  An order with no
        # attachments hashes the same bytes it hashed before this change, so the
        # orders whose stored handled/confirmed digests still matched on
        # 2026-08-07 keep matching and nothing is asked to rebuild.
        "attachments": buyer_request_identity(attachment_manifest),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    feedback_identity_sha256, feedback_message_identities = _stable_paid_feedback_digest(
        str(source_talkroom_id or project_id), stage, buyer_messages,
    )
    # Existing sidecars predate the stable message identity. Keep their digest while
    # the request text is unchanged so a deployment cannot rebuild an already accepted
    # artifact. A genuinely changed request switches to the stable digest immediately.
    legacy_compatibility = (
        re.fullmatch(r"[0-9a-f]{64}", str(existing_payload.get("feedback_sha256") or ""))
        and existing_payload.get("feedback_identity_version") is None
        and existing_payload.get("feedback_text") == feedback_text
        and _existing_stage(existing_payload) == stage
    )
    feedback_sha256 = (
        str(existing_payload.get("feedback_sha256"))
        if legacy_compatibility else feedback_identity_sha256
    )
    # Both must match: the same text read at a different stage (our first
    # delivery landed between polls) is a different requirement. A pre-C3a file
    # carries its stage in `source`, so an unchanged poll over a legacy sidecar
    # still returns early without rewriting it. The accumulation is part of that
    # comparison: a poll that reveals an older buyer message the previous
    # capture window had scrolled past is news even when the current request is
    # unchanged.
    # The manifest is compared too, and it is not part of the digest any more.
    # That is the point: a stable identity must not freeze a stale record of
    # where the bytes are.  The builder reads ``source_path`` out of this file,
    # so leaving it null forever because the digest happened to match is how
    # 90000004 came to ask its buyer for a spec that was already on disk.
    # Derived from the accumulation, so a rewrite cannot move it while the buyer
    # is silent -- see request_first_observed_at for what moving it cost.
    feedback_first_observed_at = request_first_observed_at(
        accumulated, _buyer_statement_rows(buyer_messages, observed_at), observed_at,
    )
    if (
        existing_payload.get("feedback_sha256") == feedback_sha256
        and _existing_stage(existing_payload) == stage
        and existing_payload.get("accumulated_sha256") == accumulated_sha256
        and existing_payload.get("attachments") == attachment_manifest
        and existing_payload.get("feedback_identity_version") == 1
        and existing_payload.get("feedback_identity_sha256") == feedback_identity_sha256
        and existing_payload.get("feedback_message_identities") == feedback_message_identities
        # A sidecar written before this field existed has to gain it once; after
        # that the value is stable, so this cannot become a per-poll rewrite.
        and existing_payload.get("feedback_first_observed_at") == feedback_first_observed_at
    ):
        return {
            "requirements_path": str(requirements_path),
            "feedback_sha256": feedback_sha256,
            "feedback_identity_sha256": feedback_identity_sha256,
            "feedback_message_identities": feedback_message_identities,
            "stage": stage,
        }
    atomic_json(requirements_path, {
        "version": 1,
        "source": source,
        "buyer_feedback_stage": stage,
        "project_id": str(project_id),
        "talkroom_id": str(source_talkroom_id or project_id),
        "observed_at": observed_at,
        "feedback_first_observed_at": feedback_first_observed_at,
        "feedback_sha256": feedback_sha256,
        "feedback_identity_version": 1,
        "feedback_identity_sha256": feedback_identity_sha256,
        "feedback_message_identities": feedback_message_identities,
        "legacy_feedback_sha256": legacy_feedback_sha256 if feedback_sha256 != legacy_feedback_sha256 else None,
        "feedback_text": feedback_text,
        "attachments": attachment_manifest,
        # Append-only: the current request narrows, the requirements never do.
        "accumulated_requirements": accumulated,
        "accumulated_sha256": accumulated_sha256,
        "accumulated_observed_at": observed_at,
    })
    return {
        "requirements_path": str(requirements_path),
        "feedback_sha256": feedback_sha256,
        "feedback_identity_sha256": feedback_identity_sha256,
        "feedback_message_identities": feedback_message_identities,
        "stage": stage,
    }


def persist_purchased_offer_brief(
    project_id: str, talkroom_id: str, projects_root: Path, observed_at: str,
    title: Any, offer: Any,
) -> dict[str, Any] | None:
    """Let a purchased offer start work even before the buyer writes in chat."""
    brief = _sanitize_paid_feedback(title)
    if not brief or not isinstance(offer, dict) or not offer.get("body"):
        return None
    root = projects_root.expanduser().resolve() / str(project_id)
    path = root / "requirements" / "live-buyer-reply.json"
    identity = f"purchased-offer:{talkroom_id}"
    digest = hashlib.sha256(f"{identity}\n{brief}".encode()).hexdigest()
    statement_sha = hashlib.sha256(json.dumps(
        {"text": brief, "attachments": []},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    accumulated_sha = hashlib.sha256(json.dumps(
        [statement_sha], ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    existing_payload = _existing_requirements(path)
    existing = _request_named_by_existing_sidecar(path)
    if existing is not None and existing_payload.get("source") != "purchased_offer_before_first_buyer_message":
        return existing
    if (existing is not None and existing_payload.get("feedback_sha256") == digest
            and existing_payload.get("accumulated_sha256") == accumulated_sha):
        return existing
    atomic_json(path, {
        "version": 1,
        "source": "purchased_offer_before_first_buyer_message",
        "buyer_feedback_stage": "initial_request",
        "project_id": str(project_id),
        "talkroom_id": str(talkroom_id),
        "observed_at": observed_at,
        "feedback_first_observed_at": observed_at,
        "feedback_sha256": digest,
        "feedback_identity_version": 1,
        "feedback_identity_sha256": digest,
        "feedback_message_identities": [identity],
        "feedback_text": brief,
        "attachments": [],
        "accumulated_requirements": [{
            "sha256": statement_sha,
            "sent_at": None,
            "first_observed_at": observed_at,
            "text": brief,
            "attachments": [],
        }],
        "accumulated_sha256": accumulated_sha,
        "accumulated_observed_at": observed_at,
    })
    return {
        "requirements_path": str(path),
        "feedback_sha256": digest,
        "feedback_identity_sha256": digest,
        "feedback_message_identities": [identity],
        "stage": "initial_request",
    }


class DefaultTab:
    def __init__(
        self,
        helper: Path,
        url: str,
        *,
        hidden: bool = False,
        background: bool = False,
        owner: str | None = None,
    ):
        self.helper = helper
        self.url = url
        self.hidden = hidden
        self.background = background
        self.owner = owner or os.environ.get("CLOAK_BROWSER_OWNER", "")
        if not self.owner:
            raise ValueError("CLOAK_BROWSER_OWNER is required for a default CDP tab")
        self.target_id = ""
        self.ws = ""
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> "DefaultTab":
        if not self.hidden:
            arguments = [
                "python3",
                str(self.helper),
                "open",
                self.url,
                "--owner",
                self.owner,
            ]
            if self.background:
                arguments.append("--background")
            try:
                result = subprocess.run(
                    arguments,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=25, check=True,
                )
            except subprocess.CalledProcessError as error:
                detail = error.stderr or error.stdout or ""
                try:
                    reason = json.loads(detail.splitlines()[-1]).get("reason")
                except (IndexError, AttributeError, json.JSONDecodeError):
                    reason = None
                raise RuntimeError(
                    f"failed to open authenticated default tab: {reason or 'helper exit 1'}"
                ) from error
            row = json.loads(result.stdout.splitlines()[-1])
            if not row.get("ok") or not row.get("target_id") or not row.get("ws"):
                raise RuntimeError(f"failed to open authenticated default tab: {row}")
            self.target_id = row["target_id"]
            self.ws = row["ws"]
            return self
        self.process = subprocess.Popen(
            [
                "python3",
                str(self.helper),
                "serve-hidden",
                self.url,
                "--owner",
                self.owner,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        if self.process.stdout is None or not select.select([self.process.stdout], [], [], 25)[0]:
            self._stop_process()
            raise RuntimeError("failed to open authenticated hidden target: timed out")
        line = self.process.stdout.readline()
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            self._stop_process()
            raise RuntimeError("failed to open authenticated hidden target: invalid response") from error
        if (
            not row.get("ok")
            or not row.get("hidden")
            or not row.get("target_id")
            or not row.get("ws")
        ):
            self._stop_process()
            raise RuntimeError(f"failed to open authenticated hidden target: {row}")
        self.target_id = row["target_id"]
        self.ws = row["ws"]
        return self

    def _stop_process(self) -> None:
        if self.process is None:
            return
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def __exit__(self, *_: object) -> None:
        if self.hidden:
            self._stop_process()
        elif self.target_id:
            try:
                subprocess.run(
                    [
                        "python3",
                        str(self.helper),
                        "close",
                        self.target_id,
                        "--owner",
                        self.owner,
                    ],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                # DOM capture is already complete. A stale temporary target is
                # cleanup debt, not evidence that the authenticated read failed.
                pass


async def call(
    ws: Any, request_id: int, method: str, params: dict[str, Any],
    event_sink: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    await ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if response.get("id") == request_id:
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return response.get("result", {})
        if event_sink is not None:
            event_sink.append(response)


def allowlisted_cdp_events(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip a CDP trace to download/navigation evidence without auth material."""
    events: list[dict[str, Any]] = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        if method == "Browser.downloadWillBegin":
            events.append({"method": method, "params": {
                "guid": params.get("guid"),
                "url": str(params.get("url") or "").split("?", 1)[0],
                "suggestedFilename": params.get("suggestedFilename"),
            }})
        elif method == "Browser.downloadProgress":
            events.append({"method": method, "params": {
                "guid": params.get("guid"),
                "state": params.get("state"),
                "receivedBytes": params.get("receivedBytes"),
                "totalBytes": params.get("totalBytes"),
            }})
        elif method == "Network.responseReceived":
            response = params.get("response") if isinstance(params.get("response"), dict) else {}
            headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
            disposition = next(
                (str(value) for key, value in headers.items() if str(key).lower() == "content-disposition"),
                None,
            )
            events.append({
                "method": method,
                "params": {
                    "requestId": params.get("requestId"),
                    "type": params.get("type"),
                    "response": {
                        "url": str(response.get("url") or "").split("?", 1)[0],
                        "status": response.get("status"),
                        "mimeType": response.get("mimeType"),
                        "contentDisposition": disposition,
                    },
                },
            })
    return events


async def collect_cdp_events(ws: Any, seconds: float = 2.0) -> list[dict[str, Any]]:
    """Collect raw CDP events for a bounded interval after an attachment input."""
    events: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        except asyncio.TimeoutError:
            break
        events.append(event)
    return events


async def capture_click_downloads(
    ws: Any, request_id: int, talkroom: dict[str, Any], probe_reference: str | None = None,
    project_root: Path | None = None,
) -> int:
    """Capture buyer attachment controls that expose no href."""
    download_parent = None
    if project_root is not None:
        download_parent = project_root / "delivery" / ".attachment-downloads"
        download_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="gig-buyer-attachments-", dir=download_parent,
    ) as directory:
        try:
            await call(ws, request_id, "Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": directory,
                "eventsEnabled": True,
            })
        except RuntimeError:
            await call(ws, request_id, "Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": directory,
            })
        request_id += 1
        await call(ws, request_id, "Network.enable", {})
        request_id += 1
        for message_index, message in enumerate(talkroom.get("messages") or []):
            if not isinstance(message, dict) or message.get("side") != "buyer":
                continue
            for attachment_index, attachment in enumerate(message.get("attachments") or []):
                if not isinstance(attachment, dict) or attachment.get("data_base64"):
                    continue
                recovered = recover_captured_attachment(
                    project_root, safe_filename(attachment.get("filename")),
                ) if project_root is not None else None
                if recovered is not None:
                    continue
                before = {path.name for path in Path(directory).iterdir()}
                # Coconala's no-href attachment control ignores synthetic DOM
                # clicks. Every production capture therefore needs one trusted
                # CDP input click; limiting it to an opt-in probe made normal
                # wakes observe metadata but never the downloaded bytes.
                probe = True
                if probe:
                    targets_before = await call(ws, request_id, "Target.getTargets", {})
                    request_id += 1
                expression = f"""JSON.stringify((()=>{{
                  const messages=[...document.querySelectorAll('.d-talkroomMessage')];
                  const message=messages[{message_index}];
                  const items=message?[...message.querySelectorAll('.d-talkroomMessage_attachedFilesItem')]:[];
                  const item=items[{attachment_index}];
                  if(!item)return {{ok:false,error:'attachment_control_missing'}};
                  const control=item.querySelector('a,button,[role=button]')||item;
                  return {{ok:!!control}};
                }})())"""
                synthetic_raw_events: list[dict[str, Any]] = []
                clicked = await call(ws, request_id, "Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True,
                }, synthetic_raw_events if probe else None)
                request_id += 1
                if probe:
                    synthetic_raw_events.extend(await collect_cdp_events(ws))
                synthetic_events = allowlisted_cdp_events(synthetic_raw_events)
                clicked_raw = clicked.get("result", {}).get("value")
                try:
                    clicked_value = json.loads(clicked_raw) if isinstance(clicked_raw, str) else {}
                except json.JSONDecodeError:
                    clicked_value = {}
                if clicked_value.get("ok") is not True:
                    attachment["capture_error"] = str(
                        clicked_value.get("error") or "attachment_click_failed"
                    )
                    continue
                if probe:
                    item_control = await call(ws, request_id, "Runtime.evaluate", {
                        "expression": f"""(()=>{{
                          const messages=[...document.querySelectorAll('.d-talkroomMessage')];
                          const item=messages[{message_index}]?.querySelectorAll('.d-talkroomMessage_attachedFilesItem')[{attachment_index}];
                          return item||null;
                        }})()""",
                    })
                    request_id += 1
                    item_object_id = item_control.get("result", {}).get("objectId")
                    item_geometry_value: dict[str, Any] = {"ok": False}
                    if item_object_id:
                        item_geometry = await call(ws, request_id, "Runtime.callFunctionOn", {
                            "objectId": item_object_id,
                            "functionDeclaration": """function(){
                              this.scrollIntoView({block:'center',inline:'center'});
                              const r=this.getBoundingClientRect();
                              const x=r.left+r.width/2;
                              const y=r.top+r.height/2;
                              return {ok:x>=1&&x<=innerWidth-1&&y>=1&&y<=innerHeight-1,
                                x,y,width:r.width,height:r.height,
                                viewport_width:innerWidth,viewport_height:innerHeight};
                            }""",
                            "returnByValue": True,
                        })
                        request_id += 1
                        await asyncio.sleep(0.5)
                        value = item_geometry.get("result", {}).get("value")
                        if isinstance(value, dict):
                            item_geometry_value = value
                    trusted_events: list[dict[str, Any]] = []
                    geometry_value: dict[str, Any] = {"ok": False}
                    if item_geometry_value.get("ok"):
                        hover_point = {
                            "x": item_geometry_value["x"], "y": item_geometry_value["y"],
                        }
                        await call(ws, request_id, "Input.dispatchMouseEvent", {
                            "type": "mouseMoved", **hover_point,
                        }, trusted_events)
                        request_id += 1
                        await asyncio.sleep(0.25)
                        download_control = await call(ws, request_id, "Runtime.evaluate", {
                            "expression": f"""(()=>{{
                              const messages=[...document.querySelectorAll('.d-talkroomMessage')];
                              const item=messages[{message_index}]?.querySelectorAll('.d-talkroomMessage_attachedFilesItem')[{attachment_index}];
                              return [...(item?.querySelectorAll('.d-attachedFileControls_button')||[])]
                                .find(button=>button.querySelector('.coconala-icon.-download'))||null;
                            }})()""",
                        })
                        request_id += 1
                        download_object_id = download_control.get("result", {}).get("objectId")
                        if download_object_id:
                            download_geometry = await call(ws, request_id, "Runtime.callFunctionOn", {
                                "objectId": download_object_id,
                                "functionDeclaration": """function(){
                                  const r=this.getBoundingClientRect();
                                  const x=r.left+r.width/2;
                                  const y=r.top+r.height/2;
                                  return {ok:r.width>0&&r.height>0&&x>=1&&x<=innerWidth-1&&y>=1&&y<=innerHeight-1,
                                    x,y,width:r.width,height:r.height,
                                    viewport_width:innerWidth,viewport_height:innerHeight};
                                }""",
                                "returnByValue": True,
                            })
                            request_id += 1
                            value = download_geometry.get("result", {}).get("value")
                            if isinstance(value, dict):
                                geometry_value = value
                    if geometry_value.get("ok"):
                        point = {"x": geometry_value["x"], "y": geometry_value["y"]}
                        await call(ws, request_id, "Input.dispatchMouseEvent", {
                            "type": "mousePressed", "button": "left", "clickCount": 1, **point,
                        }, trusted_events)
                        request_id += 1
                        await call(ws, request_id, "Input.dispatchMouseEvent", {
                            "type": "mouseReleased", "button": "left", "clickCount": 1, **point,
                        }, trusted_events)
                        request_id += 1
                        trusted_events.extend(await collect_cdp_events(ws))
                    targets_after = await call(ws, request_id, "Target.getTargets", {})
                    request_id += 1
                    simplify_targets = lambda result: sorted(
                        {
                            (str(row.get("type") or ""), str(row.get("url") or "").split("?", 1)[0])
                            for row in result.get("targetInfos", []) if isinstance(row, dict)
                        }
                    )
                    target_set_before = set(simplify_targets(targets_before))
                    target_set_after = set(simplify_targets(targets_after))
                    attachment["download_probe"] = {
                        "reference": attachment.get("reference"),
                        "synthetic": {"events": synthetic_events},
                        "trusted": {"geometry": geometry_value, "events": allowlisted_cdp_events(trusted_events)},
                        "target_count_before": len(target_set_before),
                        "target_count_after": len(target_set_after),
                        "targets_added": sorted(target_set_after - target_set_before),
                        "targets_removed": sorted(target_set_before - target_set_after),
                    }
                downloaded: Path | None = None
                previous_size = -1
                stable_polls = 0
                for _ in range(80):
                    candidates = [
                        path for path in Path(directory).iterdir()
                        if path.is_file() and path.name not in before
                        and not path.name.endswith((".crdownload", ".download"))
                    ]
                    if candidates:
                        candidate = max(candidates, key=lambda path: path.stat().st_mtime_ns)
                        size = candidate.stat().st_size
                        stable_polls = stable_polls + 1 if size > 0 and size == previous_size else 0
                        previous_size = size
                        if stable_polls >= 2:
                            downloaded = candidate
                            break
                    await asyncio.sleep(0.25)
                if downloaded is None:
                    attachment["capture_error"] = "attachment_download_not_observed"
                    continue
                payload = downloaded.read_bytes()
                if len(payload) > 16 * 1024 * 1024:
                    attachment["capture_error"] = "attachment_capture_limit"
                    continue
                attachment["data_base64"] = base64.b64encode(payload).decode("ascii")
                attachment["size_bytes"] = len(payload)
                attachment["capture_error"] = None
                downloaded.unlink(missing_ok=True)
    return request_id


async def inspect_page(
    ws_url: str,
    expression: str,
    screenshot: Path | None,
    expected_url: str | None = None,
    capture_buyer_attachments: bool = False,
    previous_count: int | None = None,
    coverage_expression: str | None = None,
    validate_coverage: bool = True,
    attachment_project_root: Path | None = None,
) -> dict[str, Any]:
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10, max_size=160 * 1024 * 1024) as ws:
        request_id = 1
        for _ in range(40):
            state = await call(ws, request_id, "Runtime.evaluate", {
                "expression": "JSON.stringify({url:location.href,ready:document.readyState})",
                "returnByValue": True,
            })
            request_id += 1
            loaded = json.loads(state.get("result", {}).get("value") or "{}")
            if navigation_state_ready(loaded, expected_url):
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("authenticated tab did not finish navigation")
        evaluated = await call(ws, request_id, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        request_id += 1
        raw = evaluated.get("result", {}).get("value")
        if not isinstance(raw, str):
            raise RuntimeError("DOM expression did not return JSON text")
        value = json.loads(raw)
        selected_coverage_expression = (
            coverage_expression
            if coverage_expression is not None
            else coverage_expression_for_route(expected_url)
        )
        if selected_coverage_expression is not None:
            coverage = await call(ws, request_id, "Runtime.evaluate", {
                "expression": selected_coverage_expression,
                "returnByValue": True,
                "awaitPromise": True,
            })
            request_id += 1
            coverage_raw = coverage.get("result", {}).get("value")
            if not isinstance(coverage_raw, str):
                raise CollectorUnhealthy("inbox_coverage_missing")
            value.update(json.loads(coverage_raw))
            if validate_coverage:
                value["coverage_receipt"] = validate_inbox_coverage(value, previous_count)
        if capture_buyer_attachments:
            captured = await call(ws, request_id, "Runtime.evaluate", {
                "expression": TALKROOM_ATTACHMENT_EXPRESSION,
                "returnByValue": True,
                "awaitPromise": True,
            })
            request_id += 1
            captured_raw = captured.get("result", {}).get("value")
            captured_rows = json.loads(captured_raw) if isinstance(captured_raw, str) else []
            captured_by_reference = {
                str(row.get("reference")): row
                for row in captured_rows if isinstance(row, dict) and row.get("reference")
            }
            for message in value.get("messages") or []:
                if not isinstance(message, dict) or message.get("side") != "buyer":
                    continue
                for attachment in message.get("attachments") or []:
                    if not isinstance(attachment, dict):
                        continue
                    row = captured_by_reference.get(str(attachment.get("reference") or ""))
                    if row is not None:
                        attachment.update(row)
            request_id = await capture_click_downloads(
                ws, request_id, value, os.environ.get("GIG_ATTACHMENT_PROBE_REFERENCE") or None,
                attachment_project_root,
            )
        if screenshot is not None:
            shot = await call(ws, request_id, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            secure_write_bytes(screenshot, base64.b64decode(shot["data"]))
        return value


async def inspect_message_page(
    ws_url: str,
    expression: str,
    expected_url: str,
    previous_count: int | None = None,
    coverage_expression: str | None = None,
    validate_coverage: bool = True,
) -> dict[str, Any]:
    """Read private-message DOM without persisting customer-visible pixels."""
    kwargs = {"previous_count": previous_count} if previous_count is not None else {}
    if coverage_expression is not None:
        kwargs["coverage_expression"] = coverage_expression
    kwargs["validate_coverage"] = validate_coverage
    return await inspect_page(ws_url, expression, None, expected_url, **kwargs)


FAILURE_DIAGNOSTIC_EXPRESSION = r"""JSON.stringify((()=>{
  const text=((document.body&&document.body.innerText)||'').replace(/\s+/g,' ').trim();
  const count=selector=>document.querySelectorAll(selector).length;
  const resources=performance.getEntriesByType('resource').slice(-32).map(row=>({
    url:String(row.name||''),initiator_type:String(row.initiatorType||''),
    duration_ms:Number.isFinite(row.duration)?Math.round(row.duration*10)/10:null
  }));
  return {url:location.href,title:document.title,ready_state:document.readyState,
    body_text:text.slice(0,8192),body_text_overflow:text.length>8192,
    selector_counts:{
      direct_message_links:count("a[href*='/mypage/direct_message/']"),
      talkroom_links:count("a[href*='/talkrooms/']"),
      message_container:count('.d-messageList,.d-directMessageList,[class*=messageList]'),
      pagination_links:count("a[href*='page=']"),
      error_banners:count('[role=alert],.alert,.error,[class*=error]')
    },resources};
})())"""


async def _capture_failure_diagnostic_on_ws(
    ws: Any, evidence_dir: Path, error: Exception,
) -> None:
    evaluated = await call(ws, 9001, "Runtime.evaluate", {
        "expression": FAILURE_DIAGNOSTIC_EXPRESSION,
        "returnByValue": True,
        "awaitPromise": True,
    })
    raw = evaluated.get("result", {}).get("value")
    value = json.loads(raw) if isinstance(raw, str) else {}
    if not isinstance(value, dict):
        value = {}
    resources: list[dict[str, Any]] = []
    for row in value.get("resources") or []:
        if not isinstance(row, dict):
            continue
        url = safe_coconala_url(row.get("url"))
        if url is None:
            continue
        duration = row.get("duration_ms")
        resources.append({
            "url": url,
            "initiator_type": safe_text(row.get("initiator_type"), 40),
            "duration_ms": duration if isinstance(duration, (int, float)) else None,
        })
    raw_counts = value.get("selector_counts")
    selector_counts = {
        key: raw_counts.get(key)
        if isinstance(raw_counts, dict)
        and isinstance(raw_counts.get(key), int)
        and not isinstance(raw_counts.get(key), bool)
        and raw_counts.get(key) >= 0
        else None
        for key in (
            "direct_message_links", "talkroom_links", "message_container",
            "pagination_links", "error_banners",
        )
    }
    diagnostic = {
        "version": 1,
        "error_type": type(error).__name__,
        "error": safe_text(str(error), 300),
        "url": safe_coconala_url(value.get("url")),
        "title": safe_text(value.get("title"), 300),
        "ready_state": safe_text(value.get("ready_state"), 40),
        "body_text": safe_text(value.get("body_text"), 8192),
        "body_text_overflow": value.get("body_text_overflow") is True,
        "selector_counts": selector_counts,
        "resources": resources[:32],
    }
    atomic_json(evidence_dir / "failure-diagnostic.json", diagnostic)
    screenshot = await call(
        ws, 9002, "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": False},
    )
    encoded = screenshot.get("data")
    if not isinstance(encoded, str):
        raise RuntimeError("failure screenshot unavailable")
    try:
        png = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise RuntimeError("failure screenshot invalid") from error
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 20 * 1024 * 1024:
        raise RuntimeError("failure screenshot invalid")
    secure_write_bytes(
        evidence_dir / "failure-screenshot.png", png,
    )


async def capture_failure_diagnostic(
    ws_url: str, evidence_dir: Path, error: Exception,
) -> None:
    """Persist owner-only browser truth only after a collector failure."""
    async with websockets.connect(
        ws_url, ping_interval=None, open_timeout=10, max_size=50 * 1024 * 1024,
    ) as ws:
        await _capture_failure_diagnostic_on_ws(ws, evidence_dir, error)


def inspect_page_with_retry(
    helper: Path,
    url: str,
    expression: str,
    screenshot: Path | None,
    *,
    attempts: int = NAVIGATION_RETRY_ATTEMPTS,
    hidden: bool = False,
    capture_buyer_attachments: bool = False,
    previous_count: int | None = None,
    coverage_expression: str | None = None,
    validate_coverage: bool = True,
    attachment_project_root: Path | None = None,
) -> dict[str, Any]:
    """Retry only the known transient navigation timeout with fresh tabs."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            # A new context per attempt guarantees a failed navigation cannot
            # leak state into the retry; __exit__ closes every tab.
            with DefaultTab(helper, url, hidden=hidden) as tab:
                inspection = inspect_page(
                    tab.ws, expression, screenshot, url,
                    capture_buyer_attachments=capture_buyer_attachments,
                    previous_count=previous_count,
                    coverage_expression=coverage_expression,
                    validate_coverage=validate_coverage,
                    attachment_project_root=attachment_project_root,
                )
                if capture_buyer_attachments:
                    inspection = asyncio.wait_for(inspection, timeout=45)
                return asyncio.run(inspection)
        except TimeoutError:
            if not capture_buyer_attachments:
                raise
            # Attachment bytes are useful but must never strand the whole paid room.
            # A fresh tab still captures the complete message/attachment metadata;
            # persist_latest_paid_buyer_reply then reconnects files already on disk.
            with DefaultTab(helper, url, hidden=hidden) as tab:
                return asyncio.run(inspect_page(
                    tab.ws, expression, screenshot, url,
                    capture_buyer_attachments=False,
                    previous_count=previous_count,
                    coverage_expression=coverage_expression,
                    validate_coverage=validate_coverage,
                    attachment_project_root=attachment_project_root,
                ))
        except subprocess.TimeoutExpired:
            if attempt == attempts - 1:
                raise
        except RuntimeError as exc:
            if not _is_transient_tab_open_error(exc) or attempt == attempts - 1:
                raise
    raise AssertionError("unreachable navigation retry state")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def parse_price(text: str) -> int:
    value = first_match(r"([0-9][0-9,]*)\s*円", text)
    return int(value.replace(",", "")) if value else 0


def parse_order_price(value: Any) -> tuple[int | None, str]:
    """Parse only a single observed order-price field, never first-yen wins."""
    text = str(value or "")
    matches = re.findall(r"([0-9][0-9,]*)\s*円", text)
    if len(matches) == 1:
        return int(matches[0].replace(",", "")), "structured_order_label"
    if matches:
        return None, "ambiguous_structured_price"
    return None, "missing_structured_price"


def order_price_from_card(card: dict[str, Any]) -> tuple[int | None, str]:
    structured = card.get("price_text")
    if structured:
        return parse_order_price(structured)
    text = str(card.get("text") or "")
    matches = re.findall(r"([0-9][0-9,]*)\s*円", text)
    # Card text also contains seller-message previews and achievement metrics;
    # without the labeled order-price node, every amount is untrusted.
    if len(matches) > 1:
        return None, "ambiguous_card_text"
    return None, "missing_structured_price" if matches else "missing_price"


def iso_date(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("/", "-")


def orders_from_dom(dom: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for card in dom.get("cards", []):
        href = card.get("talkroom_url") or ""
        talkroom_id = first_match(r"/talkrooms/(\d+)", href)
        if not talkroom_id or talkroom_id in seen:
            continue
        seen.add(talkroom_id)
        text = card.get("text") or ""
        due_text = text.split("販売日", 1)[0]
        price_jpy, price_source = order_price_from_card(card)
        result.append({
            "contract_id": f"talkroom:{talkroom_id}",
            "talkroom_id": talkroom_id,
            "buyer": safe_text(card.get("buyer"), 100),
            "title": safe_text(card.get("title"), 300),
            "price_jpy": price_jpy,
            "price_source": price_source,
            "delivery_date": iso_date(first_match(r"(20\d{2}/\d{2}/\d{2})", due_text)),
            "status": "paid" if "取引中" in text else "unknown",
            "marketplace_url": safe_coconala_url(href),
        })
    return result


def canonical_message_identity_sha256(
    fields: Any, *, thread_id: str | None = None,
) -> str | None:
    """Build the one identity digest shared by head and direct-thread collectors.

    The identity contract is deliberately ``thread_id + body``: repeated
    identical text in one thread is one semantic request and therefore dedupes.
    The raw fields exist only in the in-memory CDP result.  Callers persist only
    this digest, so head-only snapshots never contain customer message text.
    """
    if not isinstance(fields, dict) or not isinstance(fields.get("body"), str):
        return None
    identity_thread = str(
        thread_id or fields.get("thread_id") or fields.get("talkroom_id")
        or fields.get("directMessagesRoomId") or ""
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity_thread):
        return None
    body = fields["body"]
    canonical = {"thread_id": identity_thread, "body": body}
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def quotes_from_dom(dom: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for card in dom.get("cards", []):
        href = card.get("request_url") or ""
        request_id = first_match(r"/customize/requests/(\d+)", href)
        if not request_id or request_id in seen:
            continue
        seen.add(request_id)
        text = card.get("text") or ""
        result.append({
            "contract_id": f"request:{request_id}",
            "request_id": request_id,
            "buyer": safe_text(card.get("buyer"), 100),
            "title": safe_text(card.get("title"), 300),
            "price_jpy": parse_price(text),
            "proposal_due": iso_date(first_match(r"(20\d{2}/\d{2}/\d{2})", text)),
            "status": "proposal_required" if "要提案" in text else "unknown",
            "marketplace_url": safe_coconala_url(href),
            "proposal_url": safe_coconala_url(card.get("proposal_url")),
        })
    return result


def inquiries_from_dom(dom: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose the generic inquiry normalizer through the Coconala adapter."""
    validate_page_identity(dom, expected_url=MESSAGES_URL, expected_title="メッセージ")
    rows = normalize_inquiries(dom)
    cards_by_id = {
        match.group(1): card
        for card in dom.get("cards", []) if isinstance(card, dict)
        for match in [re.fullmatch(
            r"/(?:talkrooms|mypage/direct_message)/([A-Za-z0-9_-]+)",
            urlsplit(str(card.get("talkroom_url") or "")).path.rstrip("/"),
        )] if match
    }
    for row in rows:
        card = cards_by_id.get(str(row.get("talkroom_id") or ""), {})
        digest = str(card.get("preview_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            row["preview_sha256"] = digest
        name = safe_text(card.get("counterparty_name"), 100)
        if name:
            row["counterparty_name"] = name
        identity_fields = card.get("last_message_identity_fields")
        if isinstance(identity_fields, dict):
            identity_fields = {
                **identity_fields,
                "thread_id": identity_fields.get("thread_id") or str(row.get("talkroom_id") or ""),
            }
        identity = canonical_message_identity_sha256(identity_fields)
        if identity is None:
            supplied = str(card.get("last_message_identity_sha256") or "")
            identity = supplied if re.fullmatch(r"[0-9a-f]{64}", supplied) else None
        if identity is not None:
            row["last_message_identity_sha256"] = identity
    return rows


_HEAD_ONLY_INQUIRY_FIELDS = (
    "talkroom_id", "talkroom_url", "title", "reply_required", "next_action",
    "unread", "last_message_side", "preview_sha256", "counterparty_name",
    "last_message_identity_sha256", "buyer_sent_at", "seller_sent_at",
    "last_message_at", "thread_read_at",
)


def _head_only_inquiry_projection(inquiries: Any) -> list[dict[str, Any]]:
    """Keep only bounded dispatch metadata from a first-page inquiry result."""
    projected: list[dict[str, Any]] = []
    for inquiry in inquiries if isinstance(inquiries, list) else []:
        if not isinstance(inquiry, dict):
            continue
        row = {
            key: inquiry[key]
            for key in _HEAD_ONLY_INQUIRY_FIELDS
            if key in inquiry
        }
        if row.get("title") not in {None, "purchase_preorder_message"}:
            row.pop("title", None)
        projected.append(row)
    return projected


def retainer_applications_from_dom(dom: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the applied-retainer tab through its own module (A4)."""
    validate_page_identity(
        {**dom, "container_present": dom.get("container_present")},
        expected_url=RETAINER_APPLICATIONS_URL,
        expected_title=_retainer_module().APPLICATIONS_TITLE,
    )
    return _retainer_module().applications_from_dom(dom)


def b1_inquiries_from_dom(dom: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only query-route paid talkrooms for the deterministic B1 sweep."""
    if not validate_inbox_dom(dom):
        raise CollectorUnhealthy("unexpected_b1_inbox")
    title = str(dom.get("title") or "")
    if "メッセージ" not in title:
        raise CollectorUnhealthy("unexpected_title")
    return normalize_inquiries(dom)


def _optional_sent_at(value: Any) -> str | None:
    """A send time in UTC, or None when the DOM did not give a usable one.

    Same shapes the buyer branch accepts -- ISO with an offset, or the site's naive
    ``YYYY-MM-DD HH:MM:SS`` which is Tokyo time -- but unparseable input is an absence
    here rather than an error.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            return None
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return parsed.astimezone(timezone.utc).isoformat()


_REQUESTED_ESTIMATE_MODULE: Any = None


def _requested_estimate_module() -> Any:
    global _REQUESTED_ESTIMATE_MODULE
    if _REQUESTED_ESTIMATE_MODULE is None:
        path = Path(__file__).with_name("requested_estimate.py")
        spec = importlib.util.spec_from_file_location("requested_estimate_collector", path)
        if spec is None or spec.loader is None:
            raise CollectorUnhealthy("requested_estimate_module_missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REQUESTED_ESTIMATE_MODULE = module
    return _REQUESTED_ESTIMATE_MODULE


def verified_application_context(cdp_helper: Path, thread_url: str) -> dict[str, Any]:
    """Read one exact official seller application without composing or sending."""
    path = Path(__file__).with_name("coconala_reply_browser.py")
    spec = importlib.util.spec_from_file_location("semantic_application_reader", path)
    if spec is None or spec.loader is None:
        raise CollectorUnhealthy("application_reader_missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.CoconalaCdpReplyBrowser(
        cdp_helper, thread_url, hidden=True, background=True,
    ) as browser:
        context, _bounded = browser._read()
        applications = browser._find_verified_applications(
            context["counterparty_user_id"], context.get("_own_user_path"),
        )
    if not applications:
        raise CollectorUnhealthy("application_not_found")
    return (
        {"application": applications[0]}
        if len(applications) == 1
        else {"applications": applications}
    )


def _estimate_url_from_verified_application(
    official_context: Any, requested_estimate: Any,
) -> str | None:
    if not isinstance(official_context, dict):
        return None
    application = official_context.get("application")
    applications = (
        [application]
        if isinstance(application, dict)
        else official_context.get("applications")
    )
    if not isinstance(applications, list) or not applications:
        return None
    requesters = {
        str(candidate.get("requester_user_id") or "")
        for candidate in applications if isinstance(candidate, dict)
    }
    if len(requesters) != 1 or len(applications) != sum(
        isinstance(candidate, dict) for candidate in applications
    ):
        return None
    requester = next(iter(requesters))
    if not re.fullmatch(r"[1-9]\d*", requester):
        return None
    return requested_estimate.sanitize_estimate_url(f"/direct_offers/add/{requester}")


def direct_message_event(
    dom: dict[str, Any], expected_url: str, *, semantic_judge: Any = None,
    semantic_effects_enabled: bool = False,
    official_context_provider: Any = None,
) -> dict[str, Any]:
    """Return bounded reply identity for the latest direct-message row."""
    validate_page_identity(dom, expected_url=expected_url, expected_title="メッセージ詳細")
    messages = dom.get("messages") if isinstance(dom.get("messages"), list) else []
    if not messages:
        result = {
            "last_message_side": "", "reply_required": False, "next_action": "observe",
            "negotiation_intent": "unclear",
            "estimate_required": False, "estimate_url": None,
            "estimate_request_identity": None, "estimate_request_sent_at": None,
            "estimate_request_sha256": None,
        }
        if "sending_unavailable" in dom:
            result["sending_unavailable"] = dom.get("sending_unavailable") is True
        return result
    last = messages[-1]
    if not isinstance(last, dict):
        raise CollectorUnhealthy("invalid_last_message")
    own_user_path = str(dom.get("own_user_path") or "")
    author_path = str(last.get("author_path") or "")
    if not own_user_path or not author_path:
        raise CollectorUnhealthy("missing_sender_identity")
    side = "seller" if own_user_path == author_path else "buyer"
    marker_present = "sending_unavailable" in dom
    result: dict[str, Any] = {
        "last_message_side": side,
        "reply_required": side == "buyer",
        "next_action": "reply" if side == "buyer" else "observe",
    }
    sent_at = _optional_sent_at(last.get("sent_at"))
    if side == "buyer":
        if sent_at is None:
            raise CollectorUnhealthy("missing_buyer_sent_at")
        result["buyer_sent_at"] = sent_at
    else:
        if sent_at is not None:
            result["seller_sent_at"] = sent_at
    message_id = str(last.get("message_id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", message_id):
        result["message_id"] = message_id
    else:
        raw_body = last.get("body")
        if type(raw_body) is not str:
            raise CollectorUnhealthy("missing_message_identity")
        try:
            result["message_sha256"] = outgoing_sha256(raw_body)
        except (TypeError, ValueError) as error:
            raise CollectorUnhealthy("missing_message_identity") from error
        result["stable_ordinal"] = sum(
            1 for previous in messages[:-1]
            if isinstance(previous, dict) and previous.get("sent_at") == last.get("sent_at")
        )
    identity_fields = last.get("last_message_identity_fields")
    expected_thread_id_match = re.fullmatch(
        r"/(?:talkrooms|mypage/direct_message)/([A-Za-z0-9_-]+)",
        urlsplit(expected_url).path.rstrip("/"),
    )
    expected_thread_id = (
        expected_thread_id_match.group(1) if expected_thread_id_match else None
    )
    if isinstance(identity_fields, dict):
        identity_fields = {
            **identity_fields,
            "thread_id": identity_fields.get("thread_id") or expected_thread_id,
        }
    else:
        identity_fields = {
            "message_id": message_id or None,
            "sent_at": last.get("sent_at"),
            "body": last.get("body"),
            "thread_id": expected_thread_id,
        }
    identity = canonical_message_identity_sha256(identity_fields)
    if identity is None:
        raise CollectorUnhealthy("missing_message_identity")
    result["last_message_identity_sha256"] = identity
    requested_estimate = (
        _requested_estimate_module() if semantic_judge is not None else None
    )
    receipt: Any = None
    if semantic_judge is None:
        result.update({
            "reply_required": False,
            "next_action": "semantic_failed",
            "estimate_required": False,
            "semantic_failure": "semantic_judge_missing",
        })
    else:
        try:
            projection_dom = dom
            receipt = semantic_judge(dom, expected_url)
            judgement = receipt.get("judgement") if isinstance(receipt, dict) else None
            needs_semantic_application = (
                isinstance(judgement, dict)
                and judgement.get("required_official_context") == "application"
            )
            needs_estimate_route = (
                isinstance(judgement, dict)
                and judgement.get("next_action") == "send_estimate"
                and not dom.get("estimate_url")
            )
            if needs_semantic_application or needs_estimate_route:
                if not callable(official_context_provider):
                    raise RuntimeError("official_context_provider_missing")
                official_context = official_context_provider(expected_url)
                if needs_semantic_application:
                    retry_started = time.monotonic()
                    try:
                        receipt = semantic_judge(
                            dom, expected_url, official_context=official_context,
                        )
                    except requested_estimate.SemanticJudgementError:
                        if time.monotonic() - retry_started >= 60:
                            raise
                        receipt = semantic_judge(
                            dom, expected_url, official_context=official_context,
                        )
                if not dom.get("estimate_url"):
                    verified_estimate_url = _estimate_url_from_verified_application(
                        official_context, requested_estimate,
                    )
                    if verified_estimate_url is not None:
                        projection_dom = {**dom, "estimate_url": verified_estimate_url}
            result.update(requested_estimate.project_semantic_receipt(
                projection_dom, expected_url, receipt,
            ))
            if (
                not semantic_effects_enabled
                and result.get("next_action") in {"reply", "clarify", "requested_estimate"}
            ):
                result.update({
                    "semantic_candidate_action": result["next_action"],
                    "reply_required": False,
                    "estimate_required": False,
                    "next_action": "semantic_pending",
                    "semantic_failure": "semantic_cutover_pending",
                })
        except Exception as error:
            code = str(error).strip().split(" ", 1)[0][:120] or type(error).__name__
            result.update({
                "reply_required": False,
                "next_action": "semantic_failed",
                "estimate_required": False,
                "semantic_failure": code,
            })
            if isinstance(receipt, dict):
                judgement = receipt.get("judgement")
                result["semantic_receipt"] = receipt
                result["semantic_context_sha256"] = receipt.get("context_sha256")
                if isinstance(judgement, dict):
                    result["negotiation_intent"] = judgement.get("conversation_state")
                    result["semantic_official_context_required"] = judgement.get(
                        "required_official_context"
                    )
    if marker_present:
        result["sending_unavailable"] = dom.get("sending_unavailable") is True
    if marker_present and dom.get("sending_unavailable") is True and side == "buyer":
        result.update({
            "sending_unavailable": True,
            "reply_required": False,
            "next_action": "officially_unrepliable",
            "estimate_required": False,
            "reply_unavailable_reason": "counterparty_restricted",
        })
        result.pop("estimate_blocked", None)
        result.pop("estimate_failure", None)
    elif marker_present and result.get("next_action") == "stop_contact":
        result["sending_unavailable"] = False
    return result


_DIRECT_THREAD_HEAD_FIELDS = (
    "talkroom_id", "talkroom_url", "last_message_identity_sha256",
    "last_message_side", "buyer_sent_at", "seller_sent_at", "reply_required",
    "sending_unavailable", "reply_unavailable_reason",
)


def direct_thread_head_projection(
    dom: dict[str, Any], expected_url: str, *, talkroom_id: str,
) -> dict[str, Any]:
    """Project one exact direct-thread head without semantic judgement or prose."""
    event = direct_message_event(dom, expected_url, semantic_judge=None)
    row = {
        key: event[key]
        for key in _DIRECT_THREAD_HEAD_FIELDS
        if key in event
    }
    row["talkroom_id"] = talkroom_id
    row["talkroom_url"] = expected_url
    row["reply_required"] = row.get("last_message_side") == "buyer"
    return row


def last_message_side_from_dom(talkroom: dict[str, Any]) -> str:
    messages = talkroom.get("messages") if isinstance(talkroom.get("messages"), list) else []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("side") in {"buyer", "seller"}:
            return str(message["side"])
    return ""


def enrich_order(order: dict[str, Any], talkroom: dict[str, Any], offer: dict[str, Any] | None) -> None:
    for field in (
        "buyer_feedback_sha256", "buyer_feedback_requirements_path", "buyer_feedback_stage",
        "buyer_feedback_identity_sha256", "buyer_feedback_message_identities",
    ):
        if talkroom.get(field) not in (None, ""):
            order[field] = talkroom[field]
    order["buyer_feedback_pending_artifact"] = talkroom.get("buyer_feedback_pending_artifact") is True
    order["buyer_visible_artifact_observed"] = talkroom.get("buyer_visible_artifact_observed") is True
    order["buyer_agreement_observed"] = talkroom.get("buyer_agreement_observed") is True
    order["buyer_reply_after_artifact_observed"] = talkroom.get("buyer_reply_after_artifact_observed") is True
    order["buyer_formal_delivery_hold"] = talkroom.get("buyer_formal_delivery_hold") is True
    order["buyer_formal_delivery_hold_reason"] = (
        BUYER_FORMAL_DELIVERY_HOLD_REASON
        if order["buyer_formal_delivery_hold"]
        else None
    )
    order["buyer_attachments"] = list(talkroom.get("buyer_attachments") or [])
    order["formal_delivery_observed"] = talkroom.get("formal_delivery_confirmed") is True
    order["formal_delivery_control_disabled"] = talkroom.get("formal_delivery_control_disabled") is True
    # A5: observation only -- B4 (never attempt formal one-shot delivery in a
    # subscription room) reads this field in a separate task; nothing branches on it here.
    order["room_contract_kind"] = talkroom.get("room_contract_kind") or "unknown"
    order["compose_draft_length"] = int(talkroom.get("compose_draft_length") or 0)
    order["compose_draft_text"] = str(talkroom.get("compose_draft_text") or "")
    # A5: two separate clocks now reach the queue. delivery_date is when the work is due;
    # contact_deadline is when the marketplace cancels the order for silence. An order can
    # be comfortably inside the first and hours from the second.
    order["seller_message_observed"] = talkroom.get("seller_message_observed") is True
    order["seller_sent_messages"] = list(talkroom.get("seller_sent_messages") or [])
    order["buyer_recent_messages"] = list(talkroom.get("buyer_recent_messages") or [])
    order["contact_deadline"] = talkroom.get("contact_deadline")
    order["contact_deadline_notice"] = talkroom.get("auto_cancel_notice")
    order["talkroom_state"] = talkroom.get("transaction_state")
    order["talkroom_observed_at"] = talkroom.get("observed_at")
    order["talkroom_evidence_sha256"] = talkroom.get("evidence_sha256")
    order["talkroom_screenshot_sha256"] = talkroom.get("screenshot_sha256")
    order["talkroom_evidence_file"] = talkroom.get("evidence_file")
    if not order.get("delivery_date"):
        order["delivery_date"] = talkroom.get("delivery_date")
    offer_url = talkroom.get("offer_reference")
    if offer_url:
        offer_id = first_match(r"/(?:offers|direct_offers/edit|customize/offers)/(\d+)", offer_url)
        if offer_id:
            order["contract_id"] = f"offer:{offer_id}" if "/mypage/offers/" in offer_url else f"direct-offer:{offer_id}"
    if offer:
        request_id = offer.get("request_id")
        if request_id:
            order["request_id"] = request_id


def load_selected_order_input(value: Any, talkroom_id: str) -> dict[str, Any]:
    """Load and bind the one preliminary order allowed into selected mode."""
    if isinstance(value, dict):
        selected = dict(value)
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("selected_order_input_required")
        try:
            candidate = Path(raw)
            raw_json = candidate.read_text(encoding="utf-8") if candidate.is_file() else raw
            selected = json.loads(raw_json)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
            raise ValueError("invalid_selected_order_input") from error
        if not isinstance(selected, dict):
            raise ValueError("invalid_selected_order_input")
        selected = dict(selected)

    selected_talkroom_id = selected.get("talkroom_id")
    if str(selected_talkroom_id or "") != talkroom_id:
        raise ValueError("selected_order_talkroom_mismatch")
    selected["talkroom_id"] = talkroom_id
    return selected


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--cdp-helper", type=Path,
                        default=BROWSER_DIR / "scripts" / "cdp_default_tab.py")
    parser.add_argument("--projects-root", type=Path, default=Path.home() / "gig/projects")
    parser.add_argument(
        "--semantic-runner", type=Path,
        default=RUNNER_DIR / "agent_runner.py",
    )
    parser.add_argument(
        "--semantic-schema", type=Path,
        default=Path(__file__).resolve().parents[1] / "schemas/reply_semantic_judgement.schema.json",
    )
    parser.add_argument("--semantic-workdir", type=Path, default=Path.home())
    parser.add_argument("--semantic-timeout-seconds", type=int, default=240)
    parser.add_argument("--semantic-effects-enabled", action="store_true")
    parser.add_argument(
        "--database", type=Path,
        default=Path.home() / "gig/connector-outbox.sqlite3",
    )
    parser.add_argument(
        "--manifest", type=Path,
        default=Path(__file__).resolve().parents[1] / "config/connectors/coconala.json",
    )
    parser.add_argument(
        "--mode",
        choices=(
            "full", "orders-only", "selected-talkroom-only",
            "direct-inbox-only", "direct-inbox-head-only",
            "direct-thread-only", "direct-thread-head-only",
        ),
        default="full",
    )
    parser.add_argument("--talkroom-id")
    parser.add_argument("--project-id")
    parser.add_argument("--selected-order-input")
    target_mode = parser.add_mutually_exclusive_group()
    target_mode.add_argument(
        "--hidden-no-screenshot", action="store_true",
        dest="hidden_no_screenshot",
        help="Use session-owned hidden targets and persist DOM evidence without visible screenshots.",
    )
    target_mode.add_argument(
        "--visible-with-screenshot", action="store_false",
        dest="hidden_no_screenshot",
        help="Explicitly opt into visible default-context tabs and screenshots.",
    )
    parser.set_defaults(hidden_no_screenshot=True)
    return parser


def main() -> int:
    args = argument_parser().parse_args()
    mode = args.mode if isinstance(getattr(args, "mode", None), str) else "full"
    selected_order: dict[str, Any] | None = None
    if mode in {
        "selected-talkroom-only", "direct-thread-only", "direct-thread-head-only",
    }:
        talkroom_id = str(getattr(args, "talkroom_id", "") or "")
        if mode in {"direct-thread-only", "direct-thread-head-only"} and not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", talkroom_id,
        ):
            print(json.dumps({"status": "failed", "error": "invalid_talkroom_id", "read_only": True}))
            return 1
        project_id = str(getattr(args, "project_id", "") or "")
        if mode == "selected-talkroom-only" and not re.fullmatch(r"\d+", talkroom_id):
            print(json.dumps({"status": "failed", "error": "invalid_talkroom_id", "read_only": True}))
            return 1
        if mode == "selected-talkroom-only" and (
            project_id in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", project_id)
        ):
            print(json.dumps({"status": "failed", "error": "invalid_project_id", "read_only": True}))
            return 1
        if mode == "selected-talkroom-only":
            try:
                selected_order = load_selected_order_input(
                    getattr(args, "selected_order_input", None), talkroom_id,
                )
            except ValueError as error:
                print(json.dumps({"status": "failed", "error": str(error), "read_only": True}))
                return 1
    else:
        talkroom_id = ""
        project_id = ""
    secure_directory(args.evidence_dir)
    observed_at = datetime.now(timezone.utc).isoformat()
    hidden = args.hidden_no_screenshot
    semantic_judge: Any = None
    if mode in {"full", "direct-inbox-only", "direct-thread-only"}:
        semantic_module = _requested_estimate_module()
        try:
            semantic_judge = semantic_module.SemanticJudge(
                runner=args.semantic_runner,
                schema=args.semantic_schema,
                workdir=args.semantic_workdir,
                evidence_root=args.evidence_dir / "semantic",
                timeout_seconds=args.semantic_timeout_seconds,
            )
        except Exception as semantic_setup_error:
            def semantic_judge(
                *_args: Any, _error: Exception = semantic_setup_error, **_kwargs: Any,
            ) -> Any:
                raise RuntimeError("semantic_judge_setup_failed") from _error
    source = (
        "orders" if mode == "orders-only"
        else "selected_talkroom" if mode == "selected-talkroom-only"
        else "direct_thread" if mode in {"direct-thread-only", "direct-thread-head-only"}
        else "direct_inbox" if mode in {"direct-inbox-only", "direct-inbox-head-only"}
        else None
    )
    requested_url = OPEN_ORDERS_URL if mode == "orders-only" else (
        f"https://coconala.com/talkrooms/{talkroom_id}" if mode == "selected-talkroom-only"
        else f"https://coconala.com/mypage/direct_message/{talkroom_id}"
        if mode in {"direct-thread-only", "direct-thread-head-only"} else None
    )
    if mode in {"direct-inbox-only", "direct-inbox-head-only"}:
        requested_url = MESSAGES_URL
    source_dom: dict[str, Any] = {}

    def screenshot(path: Path) -> Path | None:
        return None if hidden else path

    def mode_snapshot(mode_name: str, sources: list[str], **fields: Any) -> dict[str, Any]:
        return {
            "version": 1,
            "source": (
                "authenticated_coconala_hidden_default_context_dom"
                if hidden else "authenticated_coconala_default_context_dom"
            ),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "collector_mode": mode_name,
            "observed_sources": sources,
            **fields,
            "evidence_dir": str(args.evidence_dir.resolve()),
            "read_only": True,
        }

    try:
        load_connector_manifest()
        if mode == "orders-only":
            orders_dom, orders_coverage = inspect_orders_when_ready(
                args.cdp_helper, args.evidence_dir, hidden=hidden,
            )
            source_dom = orders_dom
            orders = orders_from_dom(orders_dom)
            if len(orders) != len(orders_dom["cards"]):
                raise CollectorUnhealthy("orders_card_coverage_mismatch")
            receipt = source_receipt(
                source="orders", requested_url=OPEN_ORDERS_URL, observed_at=observed_at,
                dom={**orders_dom, **orders_coverage},
            )
            atomic_json(args.evidence_dir / "received-orders-open.json", {
                "observed_at": observed_at, "orders": orders, "source_receipt": receipt,
            })
            atomic_json(args.evidence_dir / "source-receipt.json", receipt)
            snapshot = mode_snapshot(
                "orders-only", ["orders"], open_orders_list_observed=True,
                orders=orders, source_receipt=receipt,
            )
            atomic_json(args.output, snapshot)
            print(json.dumps({
                "status": "success", "collector_mode": mode, "orders": len(orders),
                "output": str(args.output), "evidence_dir": str(args.evidence_dir),
            }, ensure_ascii=False))
            return 0

        if mode == "selected-talkroom-only":
            talkroom_url = f"https://coconala.com/talkrooms/{talkroom_id}"
            raw_talkroom = inspect_page_with_retry(
                args.cdp_helper, talkroom_url, TALKROOM_FULL_EXPRESSION,
                screenshot(args.evidence_dir / f"talkroom-{safe_name(talkroom_id)}.png"),
                hidden=False,
                capture_buyer_attachments=True,
                attachment_project_root=args.projects_root.expanduser().resolve() / project_id,
            )
            source_dom = raw_talkroom
            if safe_coconala_url(raw_talkroom.get("url")) != talkroom_url:
                raise CollectorUnhealthy("selected_talkroom_route_mismatch")
            history = persist_talkroom_history(
                raw_talkroom, project_id, args.projects_root, talkroom_id, observed_at,
            )
            complete_talkroom = talkroom_with_persisted_history(
                raw_talkroom, project_id, args.projects_root, talkroom_id,
            )
            talkroom_path = args.evidence_dir / f"talkroom-{safe_name(talkroom_id)}.json"
            talkroom = minimize_talkroom_dom(complete_talkroom, talkroom_id, observed_at)
            talkroom["evidence_file"] = str(talkroom_path)
            atomic_json(talkroom_path, talkroom)
            talkroom["evidence_sha256"] = sha256_file(talkroom_path)
            atomic_json(args.evidence_dir / f"talkroom-preflight-{safe_name(talkroom_id)}.json", history)
            install_project_posting(project_id, args.projects_root)
            feedback = persist_latest_paid_buyer_reply(
                complete_talkroom, project_id, args.projects_root, observed_at,
                source_talkroom_id=talkroom_id,
            )
            offer = None
            if feedback is None and talkroom.get("offer_reference"):
                offer_url = f"https://coconala.com{talkroom['offer_reference']}"
                offer = inspect_page_with_retry(
                    args.cdp_helper, offer_url, OFFER_EXPRESSION,
                    screenshot(args.evidence_dir / f"offer-{safe_name(talkroom_id)}.png"),
                    hidden=hidden,
                )
                persist_project_proposal(project_id, talkroom_id, args.projects_root, offer, observed_at)
                atomic_json(args.evidence_dir / f"offer-{safe_name(talkroom_id)}.json", {
                    "request_id": offer.get("request_id"),
                    "url": urlsplit(str(offer.get("url") or "")).path,
                })
            if feedback is None:
                feedback = persist_purchased_offer_brief(
                    project_id, talkroom_id, args.projects_root, observed_at,
                    (selected_order or {}).get("title"), offer,
                )
            if feedback is not None:
                talkroom["buyer_feedback_requirements_path"] = feedback["requirements_path"]
                talkroom["buyer_feedback_sha256"] = feedback["feedback_sha256"]
                talkroom["buyer_feedback_identity_sha256"] = feedback.get("feedback_identity_sha256")
                talkroom["buyer_feedback_message_identities"] = feedback.get("feedback_message_identities", [])
                talkroom["buyer_feedback_stage"] = feedback["stage"]
            merged_order = dict(selected_order or {})
            enrich_order(merged_order, talkroom, offer)
            merged_order["selection_stage"] = "targeted"
            merged_order["targeted_readback_required"] = False
            receipt = source_receipt(
                source="selected_talkroom", requested_url=talkroom_url, observed_at=observed_at,
                dom={
                    **raw_talkroom,
                    "coverage_complete": raw_talkroom.get("history_complete") is True,
                    "termination_reason": "fixed_point" if raw_talkroom.get("history_complete") is True else None,
                },
            )
            atomic_json(args.evidence_dir / "source-receipt.json", receipt)
            snapshot = mode_snapshot(
                "selected-talkroom-only", ["selected_talkroom"], talkroom_id=talkroom_id,
                talkroom=talkroom, orders=[merged_order], source_receipt=receipt,
            )
            atomic_json(args.output, snapshot)
            print(json.dumps({
                "status": "success", "collector_mode": mode, "talkroom_id": talkroom_id,
                "output": str(args.output), "evidence_dir": str(args.evidence_dir),
            }, ensure_ascii=False))
            return 0

        if mode == "direct-thread-head-only":
            # This read-only preflight owns one exact direct-thread tab.  It
            # deliberately does not construct a semantic provider and stores
            # only the bounded identity projection plus the official receipt.
            thread_url = f"https://coconala.com/mypage/direct_message/{talkroom_id}"
            with DefaultTab(args.cdp_helper, thread_url, hidden=hidden) as tab:
                inquiry_dom = asyncio.run(inspect_message_page(
                    tab.ws, DIRECT_MESSAGE_EXPRESSION, thread_url,
                ))
            source_dom = inquiry_dom
            inquiry = direct_thread_head_projection(
                inquiry_dom, thread_url, talkroom_id=talkroom_id,
            )
            receipt = source_receipt(
                source="direct_thread", requested_url=thread_url,
                observed_at=observed_at, dom=inquiry_dom,
            )
            atomic_json(args.evidence_dir / "direct-thread-route.json", {
                "url": thread_url, "not_found": False, "observed_at": observed_at,
                "source_receipt": receipt,
            })
            atomic_json(args.evidence_dir / "inquiries.json", {
                "observed_at": observed_at, "inquiries": [inquiry],
            })
            snapshot = mode_snapshot(
                "direct-thread-head-only", ["direct_thread"],
                talkroom_id=talkroom_id, inquiries=[inquiry], orders=[],
                source_receipt=receipt, semantic_ssot=False, head_only=True,
            )
            atomic_json(args.output, snapshot)
            print(json.dumps({
                "status": "success", "collector_mode": mode,
                "talkroom_id": talkroom_id, "inquiries": 1,
                "semantic_ssot": False, "head_only": True, "read_only": True,
                "output": str(args.output), "evidence_dir": str(args.evidence_dir),
            }, ensure_ascii=False))
            return 0

        if mode == "direct-thread-only":
            # Targeted dispatch owns one fresh default-context tab and never
            # navigates through the inbox.  Keeping the URL construction here
            # (rather than accepting an arbitrary URL) is the route fence for
            # the fast path.
            thread_url = f"https://coconala.com/mypage/direct_message/{talkroom_id}"
            with DefaultTab(args.cdp_helper, thread_url, hidden=hidden) as tab:
                inquiry_dom = asyncio.run(inspect_message_page(
                    tab.ws, DIRECT_MESSAGE_EXPRESSION, thread_url,
                ))
            enrich_verified_dm_attachments(
                inquiry_dom, helper=args.cdp_helper, thread_id=talkroom_id,
                observed_at=observed_at,
            )
            source_dom = inquiry_dom
            inquiry = {
                "talkroom_id": talkroom_id,
                "talkroom_url": thread_url,
                "title": "purchase_preorder_message",
            }
            inquiry.update(direct_message_event(
                inquiry_dom,
                thread_url,
                semantic_judge=semantic_judge,
                semantic_effects_enabled=args.semantic_effects_enabled,
                official_context_provider=lambda exact_url: verified_application_context(
                    args.cdp_helper, exact_url,
                ),
            ))
            inquiry["thread_read_at"] = observed_at
            receipt = source_receipt(
                source="direct_thread", requested_url=thread_url,
                observed_at=observed_at, dom=inquiry_dom,
            )
            atomic_json(args.evidence_dir / "direct-thread-route.json", {
                "url": thread_url, "not_found": False, "observed_at": observed_at,
                "source_receipt": receipt,
            })
            atomic_json(args.evidence_dir / "inquiries.json", {
                "observed_at": observed_at, "inquiries": [inquiry],
            })
            snapshot = mode_snapshot(
                "direct-thread-only", ["direct_thread"],
                talkroom_id=talkroom_id,
                inquiries=[inquiry], orders=[], source_receipt=receipt,
                semantic_ssot=True,
            )
            atomic_json(args.output, snapshot)
            print(json.dumps({
                "status": "success", "collector_mode": mode,
                "talkroom_id": talkroom_id, "inquiries": 1,
                "semantic_ssot": True, "output": str(args.output),
                "evidence_dir": str(args.evidence_dir),
            }, ensure_ascii=False))
            return 0

        if mode == "direct-inbox-head-only":
            with DefaultTab(args.cdp_helper, MESSAGES_URL, hidden=hidden) as tab:
                messages_dom = asyncio.run(inspect_message_page(
                    tab.ws, MESSAGES_EXPRESSION, MESSAGES_URL,
                    coverage_expression=direct_inbox_coverage_expression(1),
                    validate_coverage=False,
                ))
            # A bounded head is intentionally never a complete inbox receipt,
            # even when the first page happens to expose a terminal paginator.
            messages_dom["coverage_complete"] = False
            messages_dom["head_only"] = True
            inquiries = _head_only_inquiry_projection(inquiries_from_dom(messages_dom))
            receipt = source_receipt(
                source="direct_inbox", requested_url=MESSAGES_URL,
                observed_at=observed_at, dom=messages_dom,
            )
            atomic_json(args.evidence_dir / "direct-inbox-route.json", {
                "url": MESSAGES_URL, "not_found": False, "observed_at": observed_at,
                "coverage_receipt": receipt,
            })
            atomic_json(args.evidence_dir / "inquiries.json", {
                "observed_at": observed_at, "inquiries": inquiries,
            })
            snapshot = mode_snapshot(
                "direct-inbox-head-only", ["direct_inbox"],
                inquiries=inquiries, orders=[], source_receipt=receipt,
                semantic_ssot=False, head_only=True,
            )
            atomic_json(args.output, snapshot)
            print(json.dumps({
                "status": "success", "collector_mode": mode,
                "captured_at": snapshot["captured_at"],
                "inquiries": len(inquiries), "head_only": True,
                "read_only": True, "output": str(args.output),
                "evidence_dir": str(args.evidence_dir),
            }, ensure_ascii=False))
            return 0

        if mode == "direct-inbox-only":
            prior_inbox_count = previous_coverage_count(
                args.evidence_dir.parent.parent, args.evidence_dir,
            )
            with DefaultTab(args.cdp_helper, MESSAGES_URL, hidden=hidden) as tab:
                direct_coverage_args = (
                    {"previous_count": prior_inbox_count}
                    if prior_inbox_count is not None else {}
                )
                try:
                    messages_dom = asyncio.run(inspect_message_page(
                        tab.ws, MESSAGES_EXPRESSION, MESSAGES_URL,
                        **direct_coverage_args,
                    ))
                    source_dom = messages_dom
                    coverage_receipt = validate_inbox_coverage(
                        messages_dom, previous_count=prior_inbox_count,
                    )
                    messages_dom["coverage_receipt"] = coverage_receipt
                    inquiries = inquiries_from_dom(messages_dom)
                except Exception as error:
                    try:
                        asyncio.run(capture_failure_diagnostic(
                            tab.ws, args.evidence_dir, error,
                        ))
                    except Exception as diagnostic_error:
                        atomic_json(
                            args.evidence_dir / "failure-diagnostic-unavailable.json",
                            {"version": 1, "error_type": type(diagnostic_error).__name__},
                        )
                    raise
            current_unread = {
                match.group(1): card["unread"]
                for card in messages_dom.get("cards", []) if isinstance(card, dict)
                for match in [re.fullmatch(
                    r"/(?:talkrooms|mypage/direct_message)/([A-Za-z0-9_-]+)",
                    urlsplit(str(card.get("talkroom_url") or "")).path.rstrip("/"),
                )] if match and type(card.get("unread")) is bool
            }
            previous_rows = previous_direct_snapshot(
                args.evidence_dir.parent.parent, args.evidence_dir,
            )
            pending_estimate_order = (
                estimate_pending_thread_order(args.database, args.manifest)
                if isinstance(getattr(args, "database", None), Path)
                and isinstance(getattr(args, "manifest", None), Path)
                else {}
            )
            changed_ids = {
                str(inquiry.get("talkroom_id") or "")
                for inquiry in inquiries
                if (
                    previous_rows.get(str(inquiry.get("talkroom_id") or "")) is None
                    or previous_rows[str(inquiry.get("talkroom_id") or "")].get("last_message_identity_sha256")
                    != inquiry.get("last_message_identity_sha256")
                )
            }
            reusable_ids: set[str] = set()
            revalidation_candidates: list[tuple[int, int, float, float | None, str]] = []
            for inquiry in inquiries:
                talkroom_id = str(inquiry.get("talkroom_id") or "")
                prior = previous_rows.get(talkroom_id)
                digest = str(inquiry.get("preview_sha256") or "")
                if (
                    prior is not None and current_unread.get(talkroom_id) is False
                    and re.fullmatch(r"[0-9a-f]{64}", digest)
                    and prior.get("last_message_identity_sha256") == inquiry.get("last_message_identity_sha256")
                    and _valid_direct_readback(prior)
                ):
                    reusable_ids.add(talkroom_id)
                    age = _direct_revalidation_age(prior, observed_at)
                    # Structured estimate cards can appear without changing the
                    # inbox preview; never reuse a requested-estimate readback.
                    receipt_current = bool(
                        callable(getattr(semantic_judge, "receipt_current", None))
                        and semantic_judge.receipt_current(prior.get("semantic_receipt"))
                    )
                    prior_receipt = prior.get("semantic_receipt")
                    prior_judgement = (
                        prior_receipt.get("judgement")
                        if isinstance(prior_receipt, dict)
                        else None
                    )
                    needs_official_application = (
                        prior.get("semantic_official_context_required") == "application"
                        or isinstance(prior_judgement, dict)
                        and prior_judgement.get("required_official_context") == "application"
                    )
                    if (
                        not receipt_current
                        or needs_official_application
                        or (
                            args.semantic_effects_enabled
                            and prior.get("semantic_failure") == "semantic_cutover_pending"
                        )
                        or prior.get("semantic_failure") == "missing_estimate_url"
                        or (
                            isinstance(prior.get("semantic_receipt"), dict)
                            and prior["semantic_receipt"].get("official_context_sha256")
                            != EMPTY_OFFICIAL_CONTEXT_SHA256
                        )
                        or prior.get("estimate_required") is True
                        or prior.get("negotiation_intent") is None
                        or age is None
                        or age >= DIRECT_REVALIDATION_HORIZON_SECONDS
                    ):
                        message_at = _optional_sent_at(
                            prior.get(
                                "buyer_sent_at"
                                if prior.get("last_message_side") == "buyer"
                                else "seller_sent_at"
                            )
                        )
                        try:
                            message_epoch = datetime.fromisoformat(message_at).timestamp() if message_at else 0.0
                        except ValueError:
                            message_epoch = 0.0
                        pending_index = pending_estimate_order.get(talkroom_id)
                        priority = _direct_revalidation_priority(
                            prior, pending_index, needs_official_application,
                        )
                        revalidation_candidates.append(
                            (priority, pending_index or 0, -message_epoch, age, talkroom_id)
                        )
            revalidation_limit = 0 if changed_ids else DIRECT_REVALIDATION_BATCH_SIZE
            due_ids = {
                talkroom_id for _, _, _, _, talkroom_id in sorted(
                    revalidation_candidates,
                    key=lambda item: (
                        item[0], item[1], item[2], item[3] is not None,
                        -(item[3] or 0), item[4],
                    ),
                )[:revalidation_limit]
            }
            reusable_ids.difference_update(due_ids)
            thread_readback_count = 0
            thread_reused_count = 0
            for inquiry in inquiries:
                talkroom_id = str(inquiry.get("talkroom_id") or "")
                prior = previous_rows.get(talkroom_id)
                if talkroom_id in reusable_ids:
                    for field in _DIRECT_READBACK_FIELDS:
                        if field in prior:
                            inquiry[field] = prior[field]
                    officially_restricted = (
                        inquiry.get("last_message_side") == "buyer"
                        and inquiry.get("sending_unavailable") is True
                        and inquiry.get("reply_unavailable_reason") == "counterparty_restricted"
                    )
                    receipt_current = bool(
                        callable(getattr(semantic_judge, "receipt_current", None))
                        and semantic_judge.receipt_current(prior.get("semantic_receipt"))
                    )
                    if (
                        receipt_current
                        and prior.get("semantic_failure") == "semantic_receipt_pending"
                    ):
                        restored = semantic_module.restore_cached_semantic_projection(
                            inquiry, prior.get("semantic_receipt"),
                        )
                        if restored is not None:
                            inquiry.update(restored)
                    if officially_restricted:
                        inquiry.update({
                            "reply_required": False,
                            "next_action": "officially_unrepliable",
                            "estimate_required": False,
                            "reply_unavailable_reason": "counterparty_restricted",
                        })
                    if not receipt_current:
                        inquiry.update({
                            "reply_required": False,
                            "next_action": "semantic_pending",
                            "estimate_required": False,
                            "semantic_failure": "semantic_receipt_pending",
                        })
                    thread_reused_count += 1
                    continue
                with DefaultTab(args.cdp_helper, inquiry["talkroom_url"], hidden=hidden) as tab:
                    inquiry_dom = asyncio.run(inspect_message_page(
                        tab.ws, DIRECT_MESSAGE_EXPRESSION, inquiry["talkroom_url"],
                    ))
                enrich_verified_dm_attachments(
                    inquiry_dom, helper=args.cdp_helper, thread_id=talkroom_id,
                    observed_at=observed_at,
                )
                cached_receipt = reusable_semantic_receipt(
                    prior, inquiry_dom, semantic_judge, semantic_module,
                )
                thread_semantic_judge = semantic_judge
                if cached_receipt is not None:
                    def thread_semantic_judge(
                        *_args: Any, _receipt: dict[str, Any] = cached_receipt,
                        **_kwargs: Any,
                    ) -> dict[str, Any]:
                        return _receipt
                inquiry.update(direct_message_event(
                    inquiry_dom, inquiry["talkroom_url"], semantic_judge=thread_semantic_judge,
                    semantic_effects_enabled=args.semantic_effects_enabled,
                    official_context_provider=lambda thread_url: verified_application_context(
                        args.cdp_helper, thread_url,
                    ),
                ))
                inquiry["thread_read_at"] = observed_at
                thread_readback_count += 1
            if thread_readback_count + thread_reused_count != len(inquiries):
                raise CollectorUnhealthy("direct_thread_readback_accounting_mismatch")
            thread_changed_buyer_count = sum(
                str(inquiry.get("talkroom_id") or "") in changed_ids
                and inquiry.get("last_message_side") == "buyer"
                for inquiry in inquiries
            )
            coverage_receipt["thread_readback_count"] = thread_readback_count
            coverage_receipt["thread_reused_count"] = thread_reused_count
            coverage_receipt["thread_revalidation_horizon_seconds"] = DIRECT_REVALIDATION_HORIZON_SECONDS
            coverage_receipt["thread_revalidation_due_count"] = len(revalidation_candidates)
            coverage_receipt["thread_revalidated_count"] = len(due_ids)
            coverage_receipt["estimate_pending_revalidated_count"] = len(
                due_ids.intersection(pending_estimate_order)
            )
            coverage_receipt["thread_changed_count"] = len(changed_ids)
            coverage_receipt["thread_changed_buyer_count"] = thread_changed_buyer_count
            coverage_receipt["thread_revalidation_limit"] = revalidation_limit
            atomic_json(args.evidence_dir / "direct-inbox-route.json", {
                "url": MESSAGES_URL, "not_found": False, "observed_at": observed_at,
                "coverage_receipt": coverage_receipt,
            })
            atomic_json(args.evidence_dir / "inquiries.json", {
                "observed_at": observed_at, "inquiries": inquiries,
                "thread_readback_count": thread_readback_count,
                "thread_reused_count": thread_reused_count,
            })
            receipt = source_receipt(
                source="direct_inbox", requested_url=MESSAGES_URL,
                observed_at=observed_at, dom=messages_dom,
                previous_count=prior_inbox_count,
            )
            receipt.update({
                "thread_readback_count": thread_readback_count,
                "thread_reused_count": thread_reused_count,
                "thread_revalidation_horizon_seconds": DIRECT_REVALIDATION_HORIZON_SECONDS,
                "thread_revalidation_due_count": len(revalidation_candidates),
                "thread_revalidated_count": len(due_ids),
                "thread_changed_count": len(changed_ids),
                "thread_changed_buyer_count": thread_changed_buyer_count,
                "thread_revalidation_limit": revalidation_limit,
            })
            atomic_json(args.evidence_dir / "source-receipt.json", receipt)
            snapshot = mode_snapshot(
                "direct-inbox-only", ["direct_inbox"],
                inquiries=inquiries, orders=[], source_receipt=receipt,
                semantic_ssot=True,
                thread_readback_count=thread_readback_count,
                thread_reused_count=thread_reused_count,
            )
            atomic_json(args.output, snapshot)
            print(json.dumps({
                "status": "success", "collector_mode": mode,
                "inquiries": len(inquiries), "output": str(args.output),
                "evidence_dir": str(args.evidence_dir),
            }, ensure_ascii=False))
            return 0

        prior_inbox_count = previous_coverage_count(
            args.evidence_dir.parent.parent, args.evidence_dir,
        )
        orders_dom = inspect_page_with_retry(
            args.cdp_helper, OPEN_ORDERS_URL, ORDERS_EXPRESSION,
            screenshot(args.evidence_dir / "received-orders-open.png"),
            hidden=hidden,
        )
        orders = orders_from_dom(orders_dom)
        atomic_json(args.evidence_dir / "received-orders-open.json", {"observed_at": observed_at, "orders": orders})

        quotes_dom = inspect_page_with_retry(
            args.cdp_helper, REQUESTS_URL, QUOTES_EXPRESSION,
            screenshot(args.evidence_dir / "received-orders-requests.png"),
            hidden=hidden,
        )
        quotes = quotes_from_dom(quotes_dom)
        atomic_json(args.evidence_dir / "received-orders-requests.json", {"observed_at": observed_at, "quotes": quotes})

        # B1 audits the query-bound paid-talkroom inbox.  It is intentionally
        # separate from pre-contract direct messages so the two URL families
        # cannot be silently substituted for one another.
        with DefaultTab(args.cdp_helper, B1_INBOX_URL, hidden=hidden) as tab:
            b1_messages_dom = asyncio.run(inspect_message_page(
                tab.ws, B1_MESSAGES_EXPRESSION, B1_INBOX_URL,
            ))
        if not validate_inbox_dom(b1_messages_dom):
            raise RuntimeError("B1 inbox route mismatch, query loss, or not found")
        inbox = {"url": B1_INBOX_URL, "not_found": False, "observed_at": observed_at,
                 "coverage_receipt": b1_messages_dom.get("coverage_receipt")}
        atomic_json(args.evidence_dir / "inbox-route.json", inbox)
        b1_inquiries = b1_inquiries_from_dom(b1_messages_dom)
        atomic_json(args.evidence_dir / "b1-inquiries.json", {
            "observed_at": observed_at, "inquiries": b1_inquiries,
        })

        # Uncontracted/open conversations are a first-class cadence queue.  We
        # only persist bounded ids/URLs/title and reply-required state; the
        # worker reopens each talkroom and reads the current DOM before sending.
        with DefaultTab(args.cdp_helper, MESSAGES_URL, hidden=hidden) as tab:
            direct_coverage_args = (
                {"previous_count": prior_inbox_count}
                if prior_inbox_count is not None else {}
            )
            messages_dom = asyncio.run(inspect_message_page(
                tab.ws, MESSAGES_EXPRESSION, MESSAGES_URL,
                **direct_coverage_args,
            ))
        inquiries = inquiries_from_dom(messages_dom)
        atomic_json(args.evidence_dir / "direct-inbox-route.json", {
            "url": MESSAGES_URL, "not_found": False, "observed_at": observed_at,
            "coverage_receipt": messages_dom.get("coverage_receipt"),
        })
        # The list page's unread badge is only a hint.  Re-open each room and
        # ground reply_required in the live last-message side so buyer-last
        # conversations are not missed or mislabelled by nested card markup.
        for inquiry in inquiries:
            with DefaultTab(args.cdp_helper, inquiry["talkroom_url"], hidden=hidden) as tab:
                inquiry_dom = asyncio.run(inspect_message_page(
                    tab.ws, DIRECT_MESSAGE_EXPRESSION, inquiry["talkroom_url"],
                ))
            inquiry.update(direct_message_event(
                inquiry_dom, inquiry["talkroom_url"], semantic_judge=semantic_judge,
                semantic_effects_enabled=args.semantic_effects_enabled,
                official_context_provider=lambda thread_url: verified_application_context(
                    args.cdp_helper, thread_url,
                ),
            ))
            inquiry["thread_read_at"] = observed_at
        atomic_json(args.evidence_dir / "inquiries.json", {"observed_at": observed_at, "inquiries": inquiries})

        for order in orders:
            talkroom_screenshot = screenshot(
                args.evidence_dir / f"talkroom-{safe_name(order['talkroom_id'])}.png"
            )
            raw_talkroom = inspect_page_with_retry(
                args.cdp_helper, order["marketplace_url"], TALKROOM_FULL_EXPRESSION,
                talkroom_screenshot,
                # A hidden target reports a zero-sized viewport and cannot
                # receive trusted pointer input. Attachment capture therefore
                # uses one self-owned visible default-context tab, while the
                # remaining read-only collector pages stay hidden.
                hidden=False,
                capture_buyer_attachments=True,
            )
            download_probes = [
                attachment["download_probe"]
                for message in raw_talkroom.get("messages") or []
                if isinstance(message, dict)
                for attachment in message.get("attachments") or []
                if isinstance(attachment, dict) and isinstance(attachment.get("download_probe"), dict)
            ]
            if download_probes:
                atomic_json(
                    args.evidence_dir / f"attachment-download-probe-{safe_name(order['talkroom_id'])}.json",
                    {
                        "version": 1,
                        "observed_at": observed_at,
                        "talkroom_id": str(order["talkroom_id"]),
                        "probes": download_probes,
                    },
                )
            project_id = str(order.get("request_id") or order["talkroom_id"])
            history = persist_talkroom_history(
                raw_talkroom, project_id, args.projects_root,
                str(order["talkroom_id"]), observed_at,
            )
            complete_talkroom = talkroom_with_persisted_history(
                raw_talkroom, project_id, args.projects_root, str(order["talkroom_id"]),
            )
            talkroom_path = args.evidence_dir / f"talkroom-{safe_name(order['talkroom_id'])}.json"
            talkroom = minimize_talkroom_dom(complete_talkroom, order["talkroom_id"], observed_at)
            talkroom["evidence_file"] = str(talkroom_path)
            if talkroom_screenshot is not None:
                talkroom["screenshot_sha256"] = sha256_file(talkroom_screenshot)
            atomic_json(talkroom_path, talkroom)
            talkroom["evidence_sha256"] = sha256_file(talkroom_path)
            offer = None
            offer_details = None
            if talkroom.get("offer_reference"):
                offer_url = f"https://coconala.com{talkroom['offer_reference']}"
                offer_details = inspect_page_with_retry(
                    args.cdp_helper, offer_url, OFFER_EXPRESSION,
                    screenshot(args.evidence_dir / f"offer-{safe_name(order['talkroom_id'])}.png"),
                    hidden=hidden,
                )
                persist_project_proposal(
                    project_id, str(order["talkroom_id"]), args.projects_root, offer_details, observed_at,
                )
                offer = {"request_id": offer_details.get("request_id"), "url": urlsplit(str(offer_details.get("url") or "")).path}
                atomic_json(args.evidence_dir / f"offer-{safe_name(order['talkroom_id'])}.json", offer)
            enrich_order(order, talkroom, offer)
            atomic_json(
                args.evidence_dir / f"talkroom-preflight-{safe_name(order['talkroom_id'])}.json",
                history,
            )
            # The posting lands in the project, not in this bounded snapshot: the
            # snapshot's order rows are an allowlisted transport, and the builder reads
            # the project.
            install_project_posting(project_id, args.projects_root)
            feedback = persist_latest_paid_buyer_reply(
                complete_talkroom, project_id, args.projects_root, observed_at,
                source_talkroom_id=str(order["talkroom_id"]),
            )
            if feedback is None:
                feedback = persist_purchased_offer_brief(
                    project_id, str(order["talkroom_id"]), args.projects_root, observed_at,
                    order.get("title"), offer_details,
                )
            if feedback is not None:
                order["buyer_feedback_requirements_path"] = feedback["requirements_path"]
                order["buyer_feedback_sha256"] = feedback["feedback_sha256"]
                order["buyer_feedback_identity_sha256"] = feedback.get("feedback_identity_sha256")
                order["buyer_feedback_message_identities"] = feedback.get("feedback_message_identities", [])
                order["buyer_feedback_stage"] = feedback["stage"]

        # A4: applications already submitted to 継続 listings live on their own tab
        # and were swept by nothing. A3 bans NEW retainer applications; it does not
        # abandon the ones already open. This read is deliberately non-fatal: the
        # money-critical delivery queue must not go down because a follow-through
        # tab changed its markup, so a failure is recorded and degrades only the
        # retainer lane instead of the whole pass.
        retainer_applications: list[dict[str, Any]] | None = None
        retainer_collector_error: str | None = None
        try:
            with DefaultTab(args.cdp_helper, RETAINER_APPLICATIONS_URL, hidden=hidden) as tab:
                retainer_dom = asyncio.run(inspect_message_page(
                    tab.ws,
                    _retainer_module().APPLICATIONS_EXPRESSION,
                    RETAINER_APPLICATIONS_URL,
                ))
            retainer_applications = retainer_applications_from_dom(retainer_dom)
            atomic_json(args.evidence_dir / "retainer-applications.json", {
                "observed_at": observed_at,
                "url": RETAINER_APPLICATIONS_URL,
                "filters": retainer_dom.get("filters"),
                "applications": retainer_applications,
            })
        except Exception as error:  # noqa: BLE001 - recorded, never swallowed
            retainer_collector_error = f"{type(error).__name__}:{str(error)[:200]}"
            atomic_json(args.evidence_dir / "retainer-applications-failure.json", {
                "observed_at": observed_at, "error": retainer_collector_error,
            })

        # The snapshot is not complete until every authenticated DOM read above has
        # finished.  Using the collection start time here made a healthy, slow
        # collector fail the shared paid/reply freshness gate after a few minutes.
        captured_at = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "version": 1,
            "source": (
                "authenticated_coconala_hidden_default_context_dom"
                if hidden else "authenticated_coconala_default_context_dom"
            ),
            "captured_at": captured_at,
            "inbox": inbox,
            "direct_inbox_receipt": messages_dom.get("coverage_receipt"),
            "orders": orders,
            "quotes": quotes,
            "inquiries": inquiries,
            "b1_inquiries": b1_inquiries,
            "retainer_applications": retainer_applications,
            "retainer_collector_error": retainer_collector_error,
            "evidence_dir": str(args.evidence_dir.resolve()),
            "read_only": True,
        }
        atomic_json(args.output, snapshot)
    except Exception as error:
        failure = {"status": "failed", "error": str(error), "read_only": True}
        receipt = error.details if isinstance(error, CollectorUnhealthy) and error.details else None
        if receipt is None and source is not None:
            receipt = source_receipt(
                source=source,
                requested_url=requested_url,
                observed_at=observed_at,
                dom=source_dom,
            )
        if receipt is not None:
            failure["source_receipt"] = receipt
            atomic_json(args.evidence_dir / "source-receipt.json", receipt)
        atomic_json(args.evidence_dir / "snapshot-failure.json", failure)
        print(json.dumps(failure, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "success", "orders": len(orders), "quotes": len(quotes), "inquiries": len(inquiries),
                      "b1_inquiries": len(b1_inquiries),
                      "output": str(args.output), "evidence_dir": str(args.evidence_dir)}, ensure_ascii=False))
    return 0

# The offer card is meaningful only with the rendered row's author proof.  Keep
# the existing compact expression and add the three fields at module load time;
# collector callers still use one expression and no page template is queried.
DIRECT_MESSAGE_EXPRESSION = (
    DIRECT_MESSAGE_EXPRESSION
    .replace(
        "const title=document.title;const container=",
        "const title=document.title;const normalizedBodyText=((document.body&&document.body.innerText)||'').replace(/\\s+/g,' ').trim();const sending_unavailable=normalizedBodyText.includes('相手の方は現在ココナラの利用を制限されているため、メッセージのやりとりができません。');const container=",
    )
    .replace(
        "const own=document.querySelector('.sidebar-profile a[href*=\"/users/\"]');const path=a=>a?new URL(a.href,location.origin).pathname:null;",
        "const own=document.querySelector('.sidebar-profile a[href*=\"/users/\"]');const path=a=>a?new URL(a.href,location.origin).pathname:null;const ownPath=path(own);",
    )
    .replace(
        "const offer=card.closest('.threadMessage')||card;const link=offer.querySelector('.customize-title-link[href]');const text=",
        "const offer=card.closest('.threadMessage')||card;const link=offer.querySelector('.customize-title-link[href]');const author=row.querySelector('.threadUser a[href*=\"/users/\"]');const authorPath=path(author);const text=",
    )
    .replace(
        "sent_at:(time&&time.innerText||'').trim()||null}}).filter(card=>card.offer_url||card.message_kind);",
        "sent_at:(time&&time.innerText||'').trim()||null,author_path:authorPath,sender_side:ownPath&&authorPath===ownPath?'seller':authorPath?'buyer':null}}).filter(card=>card.offer_url||card.message_kind);",
    )
    .replace("own_user_path:path(own),estimate_url", "own_user_path:ownPath,sending_unavailable,estimate_url")
    .replace(
        "完了予定日\\s*(20\\d{2}[\\\\/-]\\d{1,2}[\\\\/-]\\d{1,2}|20\\d{2}年\\d{1,2}月\\d{1,2}日)",
        "完了予定日\\s*[：:]?\\s*(20\\d{2}[\\\\/-]\\d{1,2}[\\\\/-]\\d{1,2}|20\\d{2}年\\d{1,2}月\\d{1,2}日)",
    )
)

if __name__ == "__main__":
    raise SystemExit(main())
