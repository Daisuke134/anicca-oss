#!/usr/bin/env python3
"""Bounded CDP browser port for Coconala pre-contract direct messages."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import websockets


def _load_local(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    import coconala_queue_snapshot as collector
except ModuleNotFoundError:  # imported directly by unit-test loaders
    collector = _load_local("coconala_queue_snapshot")

try:
    from connector_outbox import outgoing_sha256
except ModuleNotFoundError:  # imported directly by unit-test loaders
    outgoing_sha256 = _load_local("connector_outbox").outgoing_sha256


POST_CLICK_DIAGNOSTIC_EXPRESSION = r'''(()=>{const textarea=document.querySelector('#DirectMessageBody');const button=document.querySelector('button.js_handle-submit');const visible=element=>!!(element&&element.offsetParent!==null);const errorSelectors=['[role="alert"]','.alert-danger','.error-message','.form-error','.validation-error'];const errors=new Set(errorSelectors.flatMap(selector=>[...document.querySelectorAll(selector)]).filter(visible));return{ok:true,textarea_length:textarea?textarea.value.length:null,button_disabled:button?!!button.disabled:null,visible_error_count:errors.size,url_path:location.pathname}})()'''
MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FORM_ANALYSIS_SETTLE_SECONDS = 1.0
NATIVE_SUBMIT_TIMEOUT_SECONDS = 305.0
MAX_READ_AFTER_REFRESHES = 3
SERVER_VALIDATION_CATEGORIES = frozenset({
    "external_contact",
    "message_validation",
    "sending_unavailable",
    "other_validation",
    "no_validation",
})
USER_PATH_RE = re.compile(r"^/users/(\d+)$")
MAX_APPLICATION_CARDS = 32
# One page holds at most MAX_APPLICATION_CARDS, so this is a ceiling on how many applications the
# account may have before the walk gives up. It was 32 — about a thousand applications — and we
# passed it, which showed up as a thread that reported collector_unhealthy on every wake rather
# than as anything about that thread. The walk still has to be exhaustive: its caller refuses when
# a counterparty matches more than one card, so stopping at the first match would answer a
# different question.
MAX_APPLICATION_PAGES = 400
APPLICATION_LIST_PATH = "/mypage/job_matching/applied/offers"
APPLIED_APPLICATIONS_URL = f"https://coconala.com{APPLICATION_LIST_PATH}"
APPLICATION_LIST_EXPRESSION = r'''(()=>{const path=a=>{try{const u=new URL(a.href,location.origin);return u.origin==='https://coconala.com'?u.pathname:null}catch(_){return null}};const bounded=(s,limit)=>{const text=(s||'').trim(),bytes=new TextEncoder().encode(text).length;return{value:text.slice(0,limit),bytes,overflow:bytes>limit}};const here=new URL(location.href),current=Number(here.searchParams.get('page')||'1'),later=[...document.querySelectorAll('a[href]')].map(a=>{try{const u=new URL(a.href,location.origin),page=Number(u.searchParams.get('page'));return u.origin==='https://coconala.com'&&u.pathname==='/mypage/job_matching/applied/offers'&&Number.isInteger(page)&&page>current?{href:u.href,page}:null}catch(_){return null}}).filter(Boolean),next=later.find(x=>x.page===current+1);return JSON.stringify({url:location.href,title:document.title,cards:[...document.querySelectorAll("a[href*='/mypage/offers/']")].map(a=>{const card=a.closest('.d-offerContents_contentsWrapper'),buyer=card?.querySelector("a[href*='/users/']"),title=bounded(card?.querySelector('.d-offerContents_applicationTitle')?.innerText,512);return{offer_url:path(a),buyer_user_path:path(buyer),title:title.value,title_byte_length:title.bytes,title_overflow:title.overflow}}).filter(x=>x.offer_url),next_href:next?next.href:null,pagination_later_page_present:later.length>0})})()'''
APPLICATION_OFFER_EXPRESSION = r'''(()=>{const path=a=>{try{const u=new URL(a.href,location.origin);return u.origin==='https://coconala.com'?u.pathname:null}catch(_){return null}};const bounded=(s,limit)=>{const text=(s||'').trim(),bytes=new TextEncoder().encode(text).length;return{value:text.slice(0,limit),bytes,overflow:bytes>limit}};const users=[...document.querySelectorAll("a[href*='/users/']")].map(path).filter(Boolean),proposal=bounded(document.querySelector('#OfferContent')?.value,4096);return JSON.stringify({url:location.href,user_paths:users.slice(0,16),user_paths_count:users.length,user_paths_overflow:users.length>16,request_id:document.querySelector('#OfferRequestId')?.value||null,offer_price:document.querySelector('#OfferPrice')?.value||null,expire_date:document.querySelector("input[name='data[Offer][expire_date]']")?.value||null,proposal_body:proposal.value,proposal_body_byte_length:proposal.bytes,proposal_body_overflow:proposal.overflow})})()'''


class BrowserSendFailure(RuntimeError):
    def __init__(self, code: str, network: list[dict[str, Any]]):
        super().__init__(code)
        self.code = code
        self.network = network


class NetworkSummary:
    """Retain bounded request outcomes without bodies, headers, or query strings."""

    def __init__(self, hostname: str, *, expected_path: str | None = None):
        self.hostname = hostname
        self.expected_path = expected_path
        self._requests: dict[str, dict[str, Any]] = {}

    def observe(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        request_id = str(params.get("requestId") or "")
        if method == "Network.requestWillBeSent":
            request = params.get("request")
            if not request_id or not isinstance(request, dict):
                return
            http_method = str(request.get("method") or "").upper()
            parsed = urlsplit(str(request.get("url") or ""))
            if (
                (
                    self.expected_path is None
                    and http_method not in MUTATING_HTTP_METHODS
                )
                or (
                    self.expected_path is not None
                    and http_method != "POST"
                )
                or parsed.hostname not in {self.hostname, f"www.{self.hostname}"}
                or (
                    self.expected_path is not None
                    and parsed.path != self.expected_path
                )
            ):
                return
            self._requests[request_id] = {
                "method": http_method,
                "path": parsed.path or "/",
                "status": None,
                "outcome": "pending",
            }
            return
        row = self._requests.get(request_id)
        if row is None:
            return
        if method == "Network.responseReceived":
            response = params.get("response")
            status = response.get("status") if isinstance(response, dict) else None
            if isinstance(status, (int, float)):
                row["status"] = int(status)
        elif method == "Network.loadingFinished":
            row["outcome"] = "finished"
        elif method == "Network.loadingFailed":
            row["outcome"] = "failed"
            error_text = str(params.get("errorText") or "")
            match = re.fullmatch(r"(?:net::)?(ERR_[A-Z0-9_]+)", error_text)
            row["failure"] = match.group(1) if match else "network_error"

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in list(self._requests.values())[:10]]

    def settled(self) -> bool:
        return bool(self._requests) and all(
            row["outcome"] in {"finished", "failed"}
            for row in self._requests.values()
        )


def fill_expression(body: str) -> str:
    """Fill the reply box, and if it is not there, say what the page had instead.

    The selector is a legacy CakePHP id/name pair. When it stops matching -- a redesign, a
    logged-out redirect, an archived thread -- every reply fails, and until 2026-08-06 the
    only thing recorded was the string "missing_message_input". Eight buyers went unanswered,
    the oldest since 08-01, and no evidence anywhere could say whether the box had moved or
    the page was not the thread at all.

    The failure never guesses at a different box: writing into the wrong textarea would send
    a stranger's message to the wrong person. It reports the candidates and the URL so the
    next pass's evidence contains the fix.
    """
    encoded = json.dumps(body, ensure_ascii=False)
    return (
        "(()=>{const input=document.querySelector("
        "'#DirectMessageBody[name=\"data[DirectMessage][body]\"]');"
        "if(!input)return{ok:false,error:'missing_message_input',"
        "location:location.href,title:document.title,"
        "candidates:[...document.querySelectorAll('textarea,input[type=text],[contenteditable=true]')]"
        ".slice(0,12).map(e=>({tag:e.tagName,id:e.id||null,name:e.getAttribute('name')||null,"
        "cls:(e.className||'').slice(0,80),placeholder:e.getAttribute('placeholder')||null})),"
        "forms:[...document.querySelectorAll('form')].slice(0,6)"
        ".map(f=>f.getAttribute('action')||null)};"
        "const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
        f"setter.call(input,{encoded});input.dispatchEvent(new Event('input',{{bubbles:true}}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));input.focus();"
        "return{ok:input.value.length>0}})()"
    )


def _utc(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise collector.CollectorUnhealthy("missing_message_timestamp") from error
    if parsed.tzinfo is None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            raise collector.CollectorUnhealthy("missing_message_timestamp")
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return parsed.astimezone(timezone.utc).isoformat()


def thread_state(
    dom: dict[str, Any], expected_url: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split live DOM into transient model context and hash-only evidence."""
    collector.validate_page_identity(
        dom, expected_url=expected_url, expected_title="メッセージ詳細"
    )
    own_user_path = str(dom.get("own_user_path") or "")
    if not own_user_path:
        raise collector.CollectorUnhealthy("missing_sender_identity")
    if USER_PATH_RE.fullmatch(own_user_path) is None:
        raise collector.CollectorUnhealthy("invalid_sender_identity")
    messages = dom.get("messages") if isinstance(dom.get("messages"), list) else []
    context_rows: list[dict[str, str]] = []
    fingerprint_rows: list[dict[str, str]] = []
    seller_hashes: list[str] = []
    seller_messages: list[dict[str, str]] = []
    seller_sent_at: str | None = None
    latest_buyer_sent_at: str | None = None
    last_sender = ""
    counterparty_paths: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise collector.CollectorUnhealthy("invalid_message_row")
        author_path = str(message.get("author_path") or "")
        body = message.get("body")
        if not author_path:
            raise collector.CollectorUnhealthy("missing_sender_identity")
        if type(body) is not str:
            raise collector.CollectorUnhealthy("missing_message_body")
        raw_sent_at = str(message.get("sent_at") or "").strip()
        sent_at = _utc(raw_sent_at)
        side = "seller" if author_path == own_user_path else "buyer"
        if side == "buyer":
            if USER_PATH_RE.fullmatch(author_path) is None:
                raise collector.CollectorUnhealthy("invalid_counterparty_identity")
            counterparty_paths.add(author_path)
        digest = outgoing_sha256(body)
        message_id = str(message.get("message_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", message_id):
            canonical = json.dumps(
                {"author": author_path, "body": body, "index": index, "sent_at": raw_sent_at},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            message_id = "sha256_" + hashlib.sha256(canonical.encode()).hexdigest()
        context_row: dict[str, Any] = {
            "side": side, "role": side, "message_id": message_id,
            "sent_at": raw_sent_at, "body": body,
        }
        if isinstance(message.get("verified_attachments"), list):
            context_row["verified_attachments"] = message["verified_attachments"]
        context_rows.append(context_row)
        fingerprint_rows.append({"side": side, "sent_at": sent_at, "body_sha256": digest})
        last_sender = side
        if side == "seller":
            seller_hashes.append(digest)
            seller_messages.append({"body_sha256": digest, "sent_at": sent_at})
            seller_sent_at = sent_at
        else:
            latest_buyer_sent_at = sent_at
    if len(counterparty_paths) != 1:
        raise collector.CollectorUnhealthy("counterparty_identity_ambiguous")
    counterparty_user_id = next(iter(counterparty_paths)).rsplit("/", 1)[-1]
    service_urls = dom.get("service_urls") if isinstance(dom.get("service_urls"), list) else []
    service_ids = {match.group(1) for value in service_urls
                   if (match := re.fullmatch(r"/services/(\d+)", str(value or "")))}
    path = urlsplit(expected_url).path
    match = re.fullmatch(r"/mypage/direct_message/([A-Za-z0-9_-]+)", path)
    if not match:
        raise collector.CollectorUnhealthy("unexpected_url")
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        {"conversation": context_rows, "counterparty_user_id": counterparty_user_id,
         "official_service_id": next(iter(service_ids)) if len(service_ids) == 1 else None,
         "_own_user_path": own_user_path},
        {
            "talkroom_id": match.group(1),
            "url": f"https://coconala.com{path}",
            "fingerprint": fingerprint,
            "seller_count": len(seller_hashes),
            "seller_message_hashes": seller_hashes,
            "seller_messages": seller_messages,
            "seller_sent_at": seller_sent_at,
            "latest_buyer_sent_at": latest_buyer_sent_at,
            "last_sender": last_sender,
        },
    )


def direct_message_path(thread_url: str) -> str:
    parsed = urlsplit(thread_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"coconala.com", "www.coconala.com"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid Coconala thread URL")
    match = re.fullmatch(r"/mypage/direct_message/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise ValueError("unexpected Coconala direct-message path")
    return f"/mypage/direct_message/{match.group(1)}"


def native_submit_path(thread_url: str) -> str:
    return direct_message_path(thread_url).replace(
        "/mypage/direct_message/", "/mypage/direct_message_ajax/", 1
    )


def submit_expression(thread_url: str, outgoing_hash: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", outgoing_hash):
        raise ValueError("invalid outgoing hash")
    parsed = urlsplit(thread_url)
    expected_form_path = json.dumps(direct_message_path(thread_url))
    expected_origin = json.dumps(f"https://{parsed.hostname}")
    expected_submit_url = json.dumps(
        f"https://{parsed.hostname}{native_submit_path(thread_url)}"
    )
    expected_hash = json.dumps(outgoing_hash)
    return (
        "(async()=>{"
        f"const expectedFormPath={expected_form_path};"
        f"const expectedOrigin={expected_origin};"
        f"const expectedSubmitUrl={expected_submit_url};"
        f"const expectedHash={expected_hash};"
        r"const horizontalWhitespace="
        r"/[\u0009\u000B\u000C\u001C-\u0020\u0085\u00A0\u1680"
        r"\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+/gu;"
        r"const edgeWhitespace="
        r"/^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680"
        r"\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+|"
        r"[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680"
        r"\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+$/gu;"
        r"const normalize=value=>value.normalize('NFC').replace(/\r\n/g,'\n')"
        r".replace(horizontalWhitespace,' ').replace(edgeWhitespace,'');"
        "const inspect=()=>{"
        "if(location.origin!==expectedOrigin)"
        "return{ok:false,error:'unexpected_page_origin'};"
        "const controls=[...document.querySelectorAll("
        "'[name=\"data[DirectMessage][body]\"]')];"
        "if(controls.length!==1)"
        "return{ok:false,error:'unexpected_message_control_count'};"
        "const input=controls[0];"
        "if(input.tagName!=='TEXTAREA'||input.id!=='DirectMessageBody')"
        "return{ok:false,error:'unexpected_message_input'};"
        "const form=input.form;"
        "if(!form)return{ok:false,error:'missing_message_form'};"
        "const action=new URL(form.action,location.href);"
        "if(action.origin!==expectedOrigin||action.pathname!==expectedFormPath"
        "||action.search!==''||action.hash!=='')"
        "return{ok:false,error:'unexpected_form_action'};"
        "if((form.method||'').toUpperCase()!=='POST')"
        "return{ok:false,error:'unexpected_form_method'};"
        "if((form.enctype||'').toLowerCase()"
        "!=='application/x-www-form-urlencoded')"
        "return{ok:false,error:'unexpected_form_encoding'};"
        "const target=(form.target||'').toLowerCase();"
        "if(target!==''&&target!=='_self')"
        "return{ok:false,error:'unexpected_form_target'};"
        "const buttons=[...document.querySelectorAll('button.js_handle-submit')];"
        "if(buttons.length!==1||buttons[0].form!==form)"
        "return{ok:false,error:'missing_send_button'};"
        "const button=buttons[0];"
        "if(button.disabled)return{ok:false,error:'disabled_send_button'};"
        "if(!/メッセージを送信する/.test("
        "(button.innerText||button.getAttribute('aria-label')||'').trim()))"
        "return{ok:false,error:'unexpected_send_button'};"
        "const normalized=normalize(input.value);"
        "if(!normalized)return{ok:false,error:'empty_message_input'};"
        "return{ok:true,form,input,normalized};"
        "};"
        "const initial=inspect();"
        "if(!initial.ok)return initial;"
        "const digest=await crypto.subtle.digest("
        "'SHA-256',new TextEncoder().encode(initial.normalized));"
        "const actualHash=[...new Uint8Array(digest)]"
        ".map(value=>value.toString(16).padStart(2,'0')).join('');"
        "const current=inspect();"
        "if(!current.ok)return current;"
        "if(current.form!==initial.form||current.input!==initial.input"
        "||current.normalized!==initial.normalized)"
        "return{ok:false,error:'message_form_changed'};"
        "if(actualHash!==expectedHash)"
        "return{ok:false,error:'outgoing_hash_mismatch'};"
        "const serializer=window.jQuery;"
        "if(typeof serializer!=='function'||!serializer.fn"
        "||typeof serializer.fn.formToArray!=='function')"
        "return{ok:false,error:'missing_form_serializer'};"
        "let fields;"
        "try{fields=serializer(current.form).formToArray(false);}"
        "catch(error){return{ok:false,error:'form_serialization_failed'};}"
        "if(!Array.isArray(fields))"
        "return{ok:false,error:'unexpected_serialized_form'};"
        "for(const field of fields){"
        "if(!field||typeof field.name!=='string'||!field.name"
        "||field.name.length>500)"
        "return{ok:false,error:'unexpected_serialized_field'};"
        "if(field.value instanceof Blob)"
        "return{ok:false,error:'unexpected_attachment'};"
        "if(typeof field.value!=='string'&&typeof field.value!=='number')"
        "return{ok:false,error:'unexpected_serialized_value'};"
        "}"
        "if(typeof serializer.param!=='function'||!serializer.ajaxSettings)"
        "return{ok:false,error:'missing_param_serializer'};"
        "let encodedBody;"
        "try{encodedBody=serializer.param("
        "fields,serializer.ajaxSettings.traditional);}"
        "catch(error){return{ok:false,error:'param_serialization_failed'};}"
        "if(typeof encodedBody!=='string')"
        "return{ok:false,error:'unexpected_encoded_form'};"
        "const parsedBody=new URLSearchParams(encodedBody);"
        "const bodies=parsedBody.getAll('data[DirectMessage][body]');"
        "if(bodies.length!==1||typeof bodies[0]!=='string'"
        "||normalize(bodies[0])!==current.normalized)"
        "return{ok:false,error:'unexpected_form_data'};"
        "const response=await fetch(expectedSubmitUrl,{"
        "method:'POST',body:encodedBody,credentials:'same-origin',redirect:'follow',"
        "headers:{'Content-Type':"
        "'application/x-www-form-urlencoded; charset=UTF-8',"
        "'Accept':'application/json, text/javascript, */*; q=0.01',"
        "'X-Requested-With':'XMLHttpRequest'}});"
        "let payload;"
        "try{payload=await response.json();}"
        "catch(error){return{ok:false,error:'invalid_submit_response',"
        "http_status:response.status};}"
        "if(!response.ok||!payload||payload.status!=='ok'){"
        "let validationText='';"
        "try{validationText=JSON.stringify("
        "payload&&payload.validationErrorMsg!==undefined"
        "?payload.validationErrorMsg:'');}catch(error){}"
        "let validationCategory='no_validation';"
        "if(/外部|連絡先|メール|電話|URL|リンク|SNS|アドレス|誘導/i"
        ".test(validationText))validationCategory='external_contact';"
        "else if(/本文|入力|必須|空|文字/i.test(validationText))"
        "validationCategory='message_validation';"
        "else if(/送信.*でき|利用.*でき|制限|停止|禁止|ブロック/i"
        ".test(validationText))validationCategory='sending_unavailable';"
        "else if(validationText&&validationText!=='\"\"')"
        "validationCategory='other_validation';"
        "return{ok:false,error:'submit_rejected',http_status:response.status,"
        "validation_category:validationCategory};"
        "}"
        "return{ok:true};"
        "})()"
    )


def evaluate_failure_message(value: dict[str, Any]) -> str:
    """Carry the page's own diagnosis into the exception.

    fill_expression was taught to return the URL, the title, the candidate inputs and the
    form actions, and the failure still read "missing_message_input" and nothing else,
    because this raised str(value["error"]) and dropped every other key. Eight buyers stayed
    unanswered for days behind a message that named a symptom and no cause.
    """
    error = str(value.get("error") or "browser expression failed")
    extra = [
        f"{key}={json.dumps(value[key], ensure_ascii=False)}"
        for key in ("location", "title", "candidates", "forms")
        if value.get(key)
    ]
    return f"{error} ({'; '.join(extra)})" if extra else error


async def _evaluate(ws_url: str, expression: str) -> dict[str, Any]:
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10) as ws:
        result = await collector.call(
            ws,
            1,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
    value = result.get("result", {}).get("value")
    if not isinstance(value, dict):
        raise RuntimeError("browser expression returned no structured result")
    if value.get("ok") is not True:
        raise RuntimeError(evaluate_failure_message(value))
    return value


async def _call_with_network_events(
    ws: Any,
    request_id: int,
    method: str,
    params: dict[str, Any],
    network: NetworkSummary,
    *,
    timeout_seconds: float = 30.0,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    deadline = monotonic() + timeout_seconds
    await ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{method} exceeded its total deadline")
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        network.observe(response)
        if response.get("id") == request_id:
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return response.get("result", {})


async def _drain_network_events(
    ws: Any,
    network: NetworkSummary,
    timeout_seconds: float = NATIVE_SUBMIT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if network.settled():
            return network.rows()
        try:
            response = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=min(0.25, deadline - time.monotonic()))
            )
        except TimeoutError:
            continue
        network.observe(response)
    return network.rows()


async def _click(
    ws_url: str, thread_url: str, outgoing_hash: str
) -> list[dict[str, Any]]:
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10) as ws:
        network = NetworkSummary(
            "coconala.com",
            expected_path=native_submit_path(thread_url),
        )
        stage = "network_enable"
        try:
            await _call_with_network_events(ws, 1, "Network.enable", {}, network)
            stage = "bring_to_front"
            await _call_with_network_events(ws, 2, "Page.bringToFront", {}, network)
            stage = "submit_form"
            result = await _call_with_network_events(
                ws,
                3,
                "Runtime.evaluate",
                {
                    "expression": submit_expression(thread_url, outgoing_hash),
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                network,
                timeout_seconds=NATIVE_SUBMIT_TIMEOUT_SECONDS,
            )
            value = result.get("result", {}).get("value")
            if not isinstance(value, dict):
                raise RuntimeError("browser expression returned no structured result")
            if value.get("ok") is not True:
                code = str(value.get("error") or "browser expression failed")
                category = str(value.get("validation_category") or "")
                if code == "submit_rejected" and category in SERVER_VALIDATION_CATEGORIES:
                    raise BrowserSendFailure(
                        f"submit_rejected_{category}", network.rows()
                    )
                raise RuntimeError(code)
            stage = "refresh_thread"
            await _call_with_network_events(
                ws,
                4,
                "Page.navigate",
                {"url": thread_url},
                network,
            )
            stage = "network_drain"
            return await _drain_network_events(ws, network)
        except BrowserSendFailure:
            raise
        except Exception as error:
            raise BrowserSendFailure(f"{stage}_failed", network.rows()) from error


class CoconalaCdpReplyBrowser:
    """One fresh authenticated tab held across read/fill/click/read."""

    def __init__(
        self,
        helper: Path,
        thread_url: str,
        *,
        verify_attempts: int = 40,
        verify_timeout_seconds: float = 10.0,
        hidden: bool = False,
        background: bool = True,
    ):
        self.helper = helper
        self.thread_url = thread_url
        self.verify_attempts = verify_attempts
        self.verify_timeout_seconds = verify_timeout_seconds
        self.hidden = hidden
        self.background = background
        self.tab: Any = None
        self.before: dict[str, Any] | None = None
        self.outgoing_hash = ""
        self.send_network: list[dict[str, Any]] = []
        self.send_error_code = ""
        self.required_official_context = "none"
        self.semantic_context_sha256: str | None = None
        self.semantic_expected_last_sender = "buyer"
        self.raw: dict[str, Any] | None = None

    def __enter__(self) -> "CoconalaCdpReplyBrowser":
        self.tab = collector.DefaultTab(
            self.helper,
            self.thread_url,
            hidden=self.hidden,
            background=self.background and not self.hidden,
        )
        self.tab.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        if self.tab is not None:
            self.tab.__exit__(*args)

    def _read(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.tab is None:
            raise RuntimeError("browser tab is not open")
        raw = asyncio.run(collector.inspect_message_page(
            self.tab.ws, collector.DIRECT_MESSAGE_EXPRESSION, self.thread_url,
        ))
        thread_id = direct_message_path(self.thread_url).rsplit("/", 1)[-1]
        collector.merge_durable_dm_attachments(raw, thread_id)
        self.raw = raw
        return thread_state(raw, self.thread_url)

    def _navigate_to(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"coconala.com", "www.coconala.com"}:
            raise collector.CollectorUnhealthy("application_navigation_origin")
        asyncio.run(self._fresh_navigation(30.0, url))

    def _application_page(self, url: str, expression: str) -> dict[str, Any]:
        self._navigate_to(url)
        if self.tab is None:
            raise RuntimeError("browser tab is not open")
        return asyncio.run(collector.inspect_message_page(self.tab.ws, expression, url))

    def _read_applied_page(self, url: str = APPLIED_APPLICATIONS_URL) -> dict[str, Any]:
        return self._application_page(url, APPLICATION_LIST_EXPRESSION)

    def _read_offer_page(self, offer_url: str) -> dict[str, Any]:
        return self._application_page(offer_url, APPLICATION_OFFER_EXPRESSION)

    @staticmethod
    def _canonical_offer_url(value: Any) -> str | None:
        raw = str(value or "")
        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError:
            return None
        same_origin = (
            parsed.scheme == "https"
            and parsed.hostname in {"coconala.com", "www.coconala.com"}
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        )
        relative_path = raw.startswith("/") and not raw.startswith("//") and not parsed.netloc and not parsed.scheme
        if (parsed.query or parsed.fragment or not re.fullmatch(r"/mypage/offers/(\d+)", parsed.path) or not (relative_path or same_origin)):
            return None
        return f"https://coconala.com{parsed.path}"

    @classmethod
    def _verified_application(
        cls, card: dict[str, Any], detail: dict[str, Any], counterparty_user_id: str,
        own_user_path: str | None = None,
    ) -> dict[str, Any] | None:
        offer_url = cls._canonical_offer_url(card.get("offer_url"))
        if offer_url is None or str(detail.get("url") or "") != offer_url:
            return None
        title = str(card.get("title") or "").strip()
        proposal_body = detail.get("proposal_body")
        if (
            not title
            or card.get("title_overflow") is not False
            or card.get("title_byte_length") != len(title.encode("utf-8"))
            or len(title.encode("utf-8")) > 512
            or type(proposal_body) is not str
            or not proposal_body.strip()
            or detail.get("proposal_body_overflow") is not False
            or detail.get("proposal_body_byte_length") != len(proposal_body.strip().encode("utf-8"))
            or len(proposal_body.strip().encode("utf-8")) > 4096
        ):
            return None
        paths = detail.get("user_paths") if isinstance(detail.get("user_paths"), list) else []
        if (
            detail.get("user_paths_overflow") is not False
            or detail.get("user_paths_count") != len(paths)
        ):
            return None
        requester_path = f"/users/{counterparty_user_id}"
        exact_paths: set[str] = set()
        for value in paths:
            if not isinstance(value, str) or USER_PATH_RE.fullmatch(value) is None:
                return None
            exact_paths.add(value)
        if own_user_path is not None:
            if USER_PATH_RE.fullmatch(own_user_path) is None:
                return None
            exact_paths.discard(own_user_path)
        if exact_paths != {requester_path}:
            return None
        request_id = str(detail.get("request_id") or "")
        offer_id = urlsplit(offer_url).path.rsplit("/", 1)[-1]
        price = str(detail.get("offer_price") or "")
        deliver_date = str(detail.get("expire_date") or "")
        try:
            date.fromisoformat(deliver_date)
        except ValueError:
            return None
        if not re.fullmatch(r"\d+", request_id) or not re.fullmatch(r"\d+", offer_id) or not re.fullmatch(r"[1-9]\d*", price) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deliver_date):
            return None
        return {
            "request_id": request_id,
            "offer_id": offer_id,
            "requester_user_id": counterparty_user_id,
            "title": title,
            "proposal_body": proposal_body.strip(),
            "price_jpy": int(price),
            "deliver_date": deliver_date,
            "offer_url": offer_url,
        }

    @staticmethod
    def _application_page_url(value: Any) -> tuple[str, int] | None:
        parsed = urlsplit(str(value or ""))
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https" or parsed.hostname not in {"coconala.com", "www.coconala.com"}
            or parsed.username is not None or parsed.password is not None or port not in {None, 443}
            or parsed.path.rstrip("/") != APPLICATION_LIST_PATH or parsed.fragment
        ):
            return None
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if not query:
            page = 1
        elif len(query) == 1 and query[0][0] == "page" and re.fullmatch(r"[1-9]\d*", query[0][1]):
            page = int(query[0][1])
        else:
            return None
        canonical = f"https://coconala.com{APPLICATION_LIST_PATH}"
        return (canonical if page == 1 else f"{canonical}?page={page}", page)

    @classmethod
    def _strict_next_page(cls, current: Any, candidate: Any) -> str | None:
        current_page = cls._application_page_url(current)
        candidate_page = cls._application_page_url(candidate)
        if current_page is None or candidate_page is None or candidate_page[1] != current_page[1] + 1:
            return None
        return candidate_page[0]

    def _application_cards_for_counterparty(
        self, counterparty_user_id: str,
    ) -> dict[str, dict[str, Any]]:
        current_url = APPLIED_APPLICATIONS_URL
        visited: set[str] = set()
        cards_by_url: dict[str, dict[str, Any]] = {}
        pages_walked = 0
        for pages_walked in range(1, MAX_APPLICATION_PAGES + 1):
            expected = self._application_page_url(current_url)
            if expected is None or current_url in visited:
                raise collector.CollectorUnhealthy("application_pagination_invalid")
            visited.add(current_url)
            page = self._read_applied_page(current_url)
            actual = self._application_page_url(page.get("url"))
            if (
                actual is None
                or actual[0] != expected[0]
                or "応募・スカウト管理" not in str(page.get("title") or "")
                or not isinstance(page.get("cards"), list)
                or not page["cards"]
                or "next_href" not in page
                or type(page.get("pagination_later_page_present")) is not bool
                or (page.get("next_href") is None and page["pagination_later_page_present"])
                or (page.get("next_href") is not None and not page["pagination_later_page_present"])
            ):
                raise collector.CollectorUnhealthy("application_pagination_invalid")
            if len(page["cards"]) > MAX_APPLICATION_CARDS:
                raise collector.CollectorUnhealthy("application_page_truncated")
            for card in page["cards"]:
                if not isinstance(card, dict) or card.get("buyer_user_path") != f"/users/{counterparty_user_id}":
                    continue
                offer_url = self._canonical_offer_url(card.get("offer_url"))
                if offer_url is not None and offer_url not in cards_by_url:
                    cards_by_url[offer_url] = card
            next_href = page.get("next_href")
            if next_href is None:
                break
            current_url = self._strict_next_page(actual[0], next_href) or ""
            if not current_url or current_url in visited:
                raise collector.CollectorUnhealthy("application_pagination_invalid")
        else:
            raise collector.CollectorUnhealthy(
                f"application_pagination_truncated:walked={pages_walked}:cap={MAX_APPLICATION_PAGES}")
        return cards_by_url

    def _find_verified_application(
        self, counterparty_user_id: str, own_user_path: str | None = None,
    ) -> dict[str, Any] | None:
        cards_by_url = self._application_cards_for_counterparty(counterparty_user_id)
        if len(cards_by_url) > 1:
            raise collector.CollectorUnhealthy("ambiguous_application")
        if not cards_by_url:
            return None
        offer_url, card = next(iter(cards_by_url.items()))
        detail = self._read_offer_page(offer_url)
        return self._verified_application(card, detail, counterparty_user_id, own_user_path)

    def _find_verified_applications(
        self, counterparty_user_id: str, own_user_path: str | None = None,
    ) -> list[dict[str, Any]]:
        cards_by_url = self._application_cards_for_counterparty(counterparty_user_id)
        applications: list[dict[str, Any]] = []
        for offer_url, card in sorted(cards_by_url.items()):
            verified = self._verified_application(
                card, self._read_offer_page(offer_url),
                counterparty_user_id, own_user_path,
            )
            if verified is None:
                raise collector.CollectorUnhealthy("application_candidate_invalid")
            applications.append(verified)
        return applications

    def _round_trip_thread(
        self, initial_context: dict[str, Any], initial_bounded: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._navigate_to(self.thread_url)
        fresh_context, fresh_bounded = self._read()
        if (fresh_bounded.get("fingerprint") != initial_bounded.get("fingerprint") or fresh_bounded.get("last_sender") != "buyer" or fresh_context.get("counterparty_user_id") != initial_context.get("counterparty_user_id") or fresh_context.get("_own_user_path") != initial_context.get("_own_user_path")):
            raise collector.CollectorUnhealthy("thread_changed")
        self.before = fresh_bounded
        return fresh_context, fresh_bounded

    async def _fresh_navigation(self, timeout_seconds: float, url: str | None = None) -> None:
        if self.tab is None:
            raise RuntimeError("browser tab is not open")
        if timeout_seconds <= 0:
            raise TimeoutError("fresh thread navigation exceeded its total deadline")
        target_url = url or self.thread_url
        if url is None:
            direct_message_path(target_url)

        async def navigate() -> None:
            async with websockets.connect(
                self.tab.ws,
                ping_interval=None,
                open_timeout=timeout_seconds,
            ) as ws:
                result = await collector.call(
                    ws,
                    1,
                    "Page.navigate",
                    {"url": target_url},
                )
                if not isinstance(result, dict) or result.get("errorText"):
                    raise RuntimeError("fresh thread navigation failed")

        await asyncio.wait_for(navigate(), timeout=timeout_seconds)

    def read_before(self) -> tuple[dict[str, Any], dict[str, Any]]:
        context, bounded = self._read()
        self.before = bounded
        needs_application = self.required_official_context == "application"
        if bounded.get("last_sender") != "buyer" or not needs_application:
            return context, bounded
        failure: Exception | None = None
        verified: dict[str, Any] | None = None
        try:
            verified = self._find_verified_application(context["counterparty_user_id"], context.get("_own_user_path"))
        except Exception as error:
            failure = error
        fresh_context, fresh_bounded = self._round_trip_thread(context, bounded)
        if failure is not None:
            raise failure
        if verified is not None:
            fresh_context["verified_application"] = verified
        return fresh_context, fresh_bounded

    def verify_semantic_freshness(self) -> None:
        """Refuse a click when the official conversation changed after judgement."""
        expected = str(self.semantic_context_sha256 or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            return
        context, bounded = self._read()
        rows = []
        for row in context.get("conversation", []):
            if not isinstance(row, dict):
                continue
            normalized: dict[str, Any] = {
                "message_id": str(row.get("message_id") or ""),
                "role": str(row.get("role") or row.get("side") or ""),
                "sent_at": str(row.get("sent_at") or ""),
                "body": str(row.get("body") or ""),
            }
            if isinstance(row.get("verified_attachments"), list):
                normalized["verified_attachments"] = row["verified_attachments"]
            rows.append(normalized)
        actual = hashlib.sha256(json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        expected_last_sender = str(self.semantic_expected_last_sender or "buyer")
        if actual != expected or bounded.get("last_sender") != expected_last_sender:
            raise ValueError("semantic_context_changed")

    def fill(self, body: str) -> None:
        if self.tab is None:
            raise RuntimeError("browser tab is not open")
        asyncio.run(_evaluate(self.tab.ws, fill_expression(body)))
        time.sleep(FORM_ANALYSIS_SETTLE_SECONDS)
        self.outgoing_hash = outgoing_sha256(body)

    def click(self) -> None:
        if self.tab is None or not self.outgoing_hash:
            raise RuntimeError("browser tab and outgoing hash are required")
        try:
            self.send_network = asyncio.run(
                _click(self.tab.ws, self.thread_url, self.outgoing_hash)
            )
        except BrowserSendFailure as error:
            self.send_network = error.network
            self.send_error_code = error.code
            raise

    def failure_evidence(self, error: Exception) -> dict[str, Any]:
        code = self.send_error_code or (
            "post_click_read_failed" if self.send_network else "click_transport_failed"
        )
        return {
            "status": "read_failed",
            "send_network": self.send_network,
            "browser_error_code": code,
        }

    def read_after(self) -> dict[str, Any]:
        if self.before is None or not self.outgoing_hash:
            raise RuntimeError("before state and outgoing body are required")
        last: dict[str, Any] = {"status": "read_failed"}
        deadline = time.monotonic() + self.verify_timeout_seconds
        refreshes = 0
        for _ in range(self.verify_attempts):
            if time.monotonic() >= deadline:
                break
            try:
                _, last = self._read()
            except Exception:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.25, remaining))
                continue
            if (
                int(last["seller_count"]) == int(self.before["seller_count"]) + 1
                and list(last["seller_message_hashes"]).count(self.outgoing_hash)
                == list(self.before.get("seller_message_hashes") or []).count(
                    self.outgoing_hash
                ) + 1
            ):
                last["send_network"] = self.send_network
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if refreshes < MAX_READ_AFTER_REFRESHES:
                refreshes += 1
                try:
                    asyncio.run(self._fresh_navigation(remaining))
                except Exception:
                    last["status"] = "read_failed"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
            time.sleep(min(0.25, remaining))
        last["status"] = "read_failed"
        last["send_network"] = self.send_network
        try:
            last["send_diagnostic"] = asyncio.run(
                _evaluate(self.tab.ws, POST_CLICK_DIAGNOSTIC_EXPRESSION)
            )
        except Exception:
            last["send_diagnostic"] = {"diagnostic_unavailable": True}
        return last
