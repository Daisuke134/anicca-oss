#!/usr/bin/env python3
"""Thin Lancers adapter for the shared marketplace Reply kernel."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, urlsplit


HERE = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
EXTERNAL_CDP_URL = "http://127.0.0.1:9222"
BOOKING_ORIGIN = "https://yoyaku.triplek-rh.workers.dev"
BOOKING_ACTION_VERSION = "external-booking-v1"
URL_PATTERN = re.compile(r"https://[^\s<>]+")
SPEC = importlib.util.spec_from_file_location("anicca_lancers_reply_work_sync", HERE / "work_sync.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("work_sync_unavailable")
work_sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = work_sync
SPEC.loader.exec_module(work_sync)

PLANNER_SPEC = importlib.util.spec_from_file_location(
    "anicca_shared_reply_planner",
    HERE.parents[2] / "_shared/marketplace-core/scripts/reply_planner.py",
)
if PLANNER_SPEC is None or PLANNER_SPEC.loader is None:
    raise RuntimeError("reply_planner_unavailable")
reply_planner = importlib.util.module_from_spec(PLANNER_SPEC)
sys.modules[PLANNER_SPEC.name] = reply_planner
PLANNER_SPEC.loader.exec_module(reply_planner)

GROUNDING_SPEC = importlib.util.spec_from_file_location(
    "anicca_shared_reply_grounding",
    HERE.parents[2] / "_shared/marketplace-core/scripts/reply_grounding.py",
)
if GROUNDING_SPEC is None or GROUNDING_SPEC.loader is None:
    raise RuntimeError("reply_grounding_unavailable")
reply_grounding = importlib.util.module_from_spec(GROUNDING_SPEC)
sys.modules[GROUNDING_SPEC.name] = reply_grounding
GROUNDING_SPEC.loader.exec_module(reply_grounding)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _message_body(value: Any) -> str:
    """Normalize the provider's CRLF storage without changing message content."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _gog_bin() -> str:
    path = os.pathsep.join(filter(None, [
        os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin",
    ]))
    binary = shutil.which("gog", path=path)
    if not binary:
        raise work_sync.SourceFailure("calendar_read_unavailable")
    return binary


def _private_env_value(name: str) -> str:
    present = os.environ.get(name)
    if present:
        return present
    path = Path(os.environ.get(
        "LIFE_MANAGER_PRIVATE_ENV",
        Path.home() / ".local/state/life-manager/.env",
    )).expanduser()
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise OSError
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, encoded = line.partition("=")
            if separator and key.strip() == name:
                values = shlex.split(encoded, comments=True, posix=True)
                if len(values) == 1 and values[0]:
                    return values[0]
                break
    except (OSError, ValueError):
        pass
    raise work_sync.SourceFailure("calendar_credential_unavailable")


class LancersReplyAdapter:
    def __init__(self, state_path: Path, grounding: Mapping[str, Any] | None = None,
                 candidate_profile: Path | None = None,
                 external_cdp_url: str = EXTERNAL_CDP_URL):
        self.state_path = state_path
        self.browser = None
        self.page = None
        self._lock = None
        self._boards: dict[str, tuple[Mapping[str, Any], Mapping[str, Any], list[Mapping[str, Any]]]] = {}
        self._posted: dict[str, str] = {}
        self._verified_proposals: set[str] = set()
        self._grounding = dict(grounding or {})
        self.candidate_profile = candidate_profile or (
            Path.home() / ".config/anicca/job-search/profile.json"
        )
        self.external_cdp_url = external_cdp_url

    @staticmethod
    def _booking_url(messages: list[Mapping[str, Any]]) -> str | None:
        for row in reversed(messages):
            sender = row.get("send_user")
            if not isinstance(sender, Mapping) or sender.get("is_client") is not True:
                continue
            for candidate in URL_PATTERN.findall(str(row.get("description") or "")):
                parsed = urlsplit(candidate.rstrip("。、）)]"))
                try:
                    query = parse_qs(parsed.query, strict_parsing=True)
                except ValueError:
                    continue
                ids = query.get("lid", [])
                if (parsed.scheme == "https" and parsed.netloc == "yoyaku.triplek-rh.workers.dev"
                        and parsed.path == "/" and set(query) == {"lid"} and len(ids) == 1
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ids[0])
                        and not parsed.fragment):
                    return candidate.rstrip("。、）)]")
        return None

    def _open(self) -> None:
        if self.page is not None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = work_sync.application_tick.account_lock(
            self.state_path.with_name("reply-account.json")
        )
        self._lock.__enter__()
        try:
            self.browser, self.page = work_sync.application_tick._open_owned_page()
            if not work_sync.application_tick._production_account_ready(self.page):
                raise work_sync.SourceFailure("account_unavailable")
            self._verified_proposals = work_sync._verified_proposals(self.state_path)
        except Exception:
            self.close()
            raise

    def _fetch_messages(self, thread_id: str) -> list[Mapping[str, Any]]:
        self._open()
        return work_sync._message_rows(
            lambda route: work_sync._fetch(self.page, route), thread_id
        )

    def _row(self, thread_id: str, board: Mapping[str, Any], messages: list[Mapping[str, Any]]) -> dict[str, str]:
        latest = max(messages, key=lambda item: int(work_sync._id(item.get("id")))) if messages else None
        latest_event = work_sync._id(latest.get("id")) if latest else work_sync._id(board.get("modified"))
        return {
            "provider": "lancers",
            "account_id": "default",
            "thread_id": thread_id,
            "latest_event_id": latest_event,
            "observed_at": _now(),
        }

    def observe_threads(self) -> list[dict[str, str]]:
        self._open()
        private: list[Any] = []
        work_sync._snapshot(
            lambda route: work_sync._fetch(self.page, route),
            self._verified_proposals,
            private,
        )
        self._boards = {
            work_sync._id(board.get("id")): (board, detail, list(messages))
            for board, detail, messages in private
        }
        rows = []
        for thread_id, (board, _detail, messages) in self._boards.items():
            row = self._row(thread_id, board, messages)
            if self._booking_url(messages):
                row["decision_version"] = BOOKING_ACTION_VERSION
            rows.append(row)
        return rows

    def observe_one(self, thread_id: str) -> dict[str, str]:
        if thread_id not in self._boards:
            raise work_sync.SourceFailure("reply_thread_unavailable")
        board, detail, _messages = self._boards[thread_id]
        messages = self._fetch_messages(thread_id)
        self._boards[thread_id] = (board, detail, messages)
        row = self._row(thread_id, board, messages)
        if self._booking_url(messages):
            row["decision_version"] = BOOKING_ACTION_VERSION
        return row

    def context(self, thread_id: str) -> dict[str, Any]:
        board, detail, messages = self._boards[thread_id]
        conversation = []
        for row in sorted(messages, key=lambda item: int(work_sync._id(item.get("id"))))[-20:]:
            sender = row.get("send_user")
            if not isinstance(sender, Mapping) or type(sender.get("is_client")) is not bool:
                raise work_sync.SourceFailure("message_sender_identity_unavailable")
            conversation.append({
                "event_id": work_sync._id(row.get("id")),
                "role": "buyer" if sender["is_client"] else "seller",
                "body": str(row.get("description") or "").strip(),
            })
        proposal = None
        related = detail.get("with")
        if isinstance(related, Mapping):
            candidate = related.get("proposal")
            if isinstance(candidate, Mapping) and candidate.get("id") is not None:
                proposal_id = work_sync._id(candidate.get("id"))
                if proposal_id in self._verified_proposals:
                    proposal = work_sync._proposal_context(
                        self.page, detail, self._verified_proposals
                    )
        result = {
            "board": {"title": board.get("title"), "description": board.get("description")},
            "conversation": conversation,
            "reply_required": bool(conversation and conversation[-1]["role"] == "buyer"),
            "verified_proposal": proposal,
            "grounding": self._grounding,
        }
        booking_url = self._booking_url(messages)
        if booking_url:
            slot = self._choose_booking_slot(booking_url)
            result["decision_required"] = True
            result["required_action"] = {
                "action": "external_action",
                "payload": {
                    "kind": "schedule_meeting",
                    "url": booking_url,
                    "slot": slot,
                    "completion_body": (
                        "日程調整ありがとうございます。予約ページより30分の打ち合わせを予約しました。"
                        "当日はよろしくお願いいたします。"
                    ),
                },
            }
        return result

    def _external_page(self, url: str):
        runtime = getattr(self.browser, "_anicca_playwright_runtime", None)
        if runtime is None:
            raise work_sync.SourceFailure("external_browser_unavailable")
        browser = runtime.chromium.connect_over_cdp(self.external_cdp_url, timeout=10_000)
        if not browser.contexts:
            raise work_sync.SourceFailure("external_browser_unavailable")
        page = browser.contexts[0].new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if urlsplit(page.url)._replace(query="", fragment="").geturl() != BOOKING_ORIGIN + "/":
            page.close()
            raise work_sync.SourceFailure("external_booking_auth_required")
        return page

    def _candidate(self) -> Mapping[str, Any]:
        value = json.loads(self.candidate_profile.read_text(encoding="utf-8"))
        candidate = value.get("candidate") if isinstance(value, Mapping) else None
        if not isinstance(candidate, Mapping):
            raise work_sync.SourceFailure("candidate_profile_unavailable")
        return candidate

    def _calendar_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        account = str(self._candidate().get("application_email") or "")
        if not account:
            raise work_sync.SourceFailure("candidate_profile_unavailable")
        completed = subprocess.run(
            [_gog_bin(), "calendar", "events", "-a", account, "--json",
             "--from", start.astimezone(JST).isoformat(), "--to", end.astimezone(JST).isoformat(),
             "--max", "250", "--all-pages"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False,
            env={**os.environ, "GOG_ACCOUNT": account,
                 "GOG_KEYRING_PASSWORD": _private_env_value("GOG_KEYRING_PASSWORD")},
        )
        if completed.returncode != 0:
            raise work_sync.SourceFailure("calendar_read_unavailable")
        value = json.loads(completed.stdout or "{}")
        events = value.get("events") if isinstance(value, Mapping) else None
        if not isinstance(events, list):
            raise work_sync.SourceFailure("calendar_read_unavailable")
        return [dict(item) for item in events if isinstance(item, Mapping)]

    @staticmethod
    def _event_busy(event: Mapping[str, Any], start: datetime, end: datetime) -> bool:
        if str(event.get("status") or "").lower() == "cancelled":
            return False
        if str(event.get("transparency") or "").lower() == "transparent":
            return False
        first = event.get("start") if isinstance(event.get("start"), Mapping) else {}
        last = event.get("end") if isinstance(event.get("end"), Mapping) else {}
        first_value = first.get("dateTime") or first.get("date")
        last_value = last.get("dateTime") or last.get("date")
        if not first_value or not last_value:
            return False
        try:
            busy_start = datetime.fromisoformat(str(first_value).replace("Z", "+00:00"))
            busy_end = datetime.fromisoformat(str(last_value).replace("Z", "+00:00"))
        except ValueError:
            return False
        if busy_start.tzinfo is None:
            busy_start = busy_start.replace(tzinfo=JST)
        if busy_end.tzinfo is None:
            busy_end = busy_end.replace(tzinfo=JST)
        return start < busy_end and busy_start < end

    def _booking_snapshot(self, url: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        page = self._external_page(url)
        try:
            value = page.evaluate("""async () => {
              const mine = await fetch('/api/my-bookings');
              const today = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Tokyo'}).format(new Date());
              const slots = await fetch('/api/slots?date=' + today + '&days=15');
              return {mineStatus:mine.status, mine:await mine.json(), slotsStatus:slots.status,
                      slots:await slots.json()};
            }""")
        finally:
            page.close()
        if (not isinstance(value, Mapping) or value.get("mineStatus") != 200
                or value.get("slotsStatus") != 200):
            raise work_sync.SourceFailure("external_booking_readback_unavailable")
        bookings = value.get("mine", {}).get("bookings") if isinstance(value.get("mine"), Mapping) else None
        days = value.get("slots", {}).get("days") if isinstance(value.get("slots"), Mapping) else None
        if not isinstance(bookings, list) or not isinstance(days, list):
            raise work_sync.SourceFailure("external_booking_readback_unavailable")
        slots = [dict(slot) for day in days if isinstance(day, Mapping)
                 for slot in day.get("slots", []) if isinstance(slot, Mapping)
                 and slot.get("available") is True]
        return [dict(item) for item in bookings if isinstance(item, Mapping)], slots

    def _choose_booking_slot(self, url: str) -> dict[str, str]:
        bookings, slots = self._booking_snapshot(url)
        if bookings:
            existing = min(bookings, key=lambda item: str(item.get("slot_start") or ""))
            return {"start": str(existing["slot_start"]), "end": str(existing["slot_end"])}
        now = datetime.now(timezone.utc)
        events = self._calendar_events(now, now + timedelta(days=16))
        for slot in sorted(slots, key=lambda item: str(item.get("start") or "")):
            try:
                start = datetime.fromisoformat(str(slot["start"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(slot["end"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            local = start.astimezone(JST)
            if local.hour < 10 or local.hour >= 19 or start < now + timedelta(minutes=30):
                continue
            if not any(self._event_busy(event, start, end) for event in events):
                return {"start": start.isoformat(), "end": end.isoformat()}
        raise work_sync.SourceFailure("external_booking_no_free_slot")

    def _profile_fields(self) -> dict[str, str]:
        candidate = self._candidate()
        gender = {"male": "男性", "female": "女性", "other": "その他"}.get(
            str(candidate.get("gender") or "").lower()
        )
        fields = {"name": str(candidate.get("name_ja") or ""),
                  "birthdate": str(candidate.get("date_of_birth") or ""),
                  "gender": str(gender or "")}
        if not all(fields.values()):
            raise work_sync.SourceFailure("candidate_profile_unavailable")
        return fields

    def _calendar_contains(self, slot: Mapping[str, str]) -> bool:
        start = datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(slot["end"].replace("Z", "+00:00"))
        return any(self._event_busy(event, start, end)
                   for event in self._calendar_events(start - timedelta(minutes=1), end + timedelta(minutes=1)))

    def _book(self, url: str, slot: Mapping[str, str]) -> None:
        page = self._external_page(url)
        try:
            value = page.evaluate("""async ({start, fields}) => {
              const response = await fetch('/api/book', {method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({slot_start:start, ...fields})});
              return {status:response.status, body:await response.json().catch(()=>({}))};
            }""", {"start": slot["start"], "fields": self._profile_fields()})
        finally:
            page.close()
        if not isinstance(value, Mapping) or value.get("status") not in {200, 201}:
            raise work_sync.SourceFailure("external_booking_submission_uncertain")

    def mutate(self, intent: dict[str, Any]) -> None:
        if intent["action"] == "external_action":
            payload = intent["payload"]
            url, slot, body = payload.get("url"), payload.get("slot"), payload.get("completion_body")
            if not isinstance(url, str) or not isinstance(slot, Mapping) or not isinstance(body, str):
                raise work_sync.SourceFailure("external_booking_payload_invalid")
            bookings, _slots = self._booking_snapshot(url)
            booked = any(str(item.get("slot_start")) == str(slot.get("start"))
                         and str(item.get("slot_end")) == str(slot.get("end")) for item in bookings)
            if not booked:
                self._book(url, slot)
                bookings, _slots = self._booking_snapshot(url)
                booked = any(str(item.get("slot_start")) == str(slot.get("start"))
                             and str(item.get("slot_end")) == str(slot.get("end")) for item in bookings)
            if not booked or not self._calendar_contains(slot):
                raise work_sync.SourceFailure("external_booking_readback_unavailable")
            if not self._reply_exists(intent["thread_id"], body):
                self._post_reply(intent["thread_id"], intent["effect_key"], body)
            return
        if intent["action"] != "reply":
            raise work_sync.SourceFailure("lancers_estimate_unsupported")
        body = intent["payload"].get("body")
        if not isinstance(body, str) or not body.strip():
            raise work_sync.SourceFailure("reply_body_invalid")
        self._post_reply(intent["thread_id"], intent["effect_key"], body)

    def _post_reply(self, thread_id: str, effect_key: str, body: str) -> None:
        value = self.page.evaluate(
            """async ({path, body}) => { const form = new FormData(); form.append("description", body); form.append("rich_description", body); const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 20000); try { const response = await fetch(path, {method:"POST", credentials:"same-origin", body:form, signal:controller.signal}); const text = await response.text(); if (!response.ok || text.length > 1048576) return {ok:false}; let parsed={}; try { parsed=JSON.parse(text); } catch (_) {} return {ok:true, body:parsed}; } catch (_) { return {ok:false}; } finally { clearTimeout(timer); } }""",
            {"path": f"/v1/message_api/boards/{quote(thread_id, safe='')}/messages",
             "body": body.strip()},
        )
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            raise work_sync.SourceFailure("reply_submission_uncertain")
        response = value.get("body")
        if isinstance(response, Mapping) and isinstance(response.get("data"), Mapping):
            response = response["data"]
        if not isinstance(response, Mapping):
            raise work_sync.SourceFailure("reply_submission_uncertain")
        self._posted[effect_key] = work_sync._id(response.get("id"))

    def _reply_exists(self, thread_id: str, body: str) -> str | None:
        for row in self._fetch_messages(thread_id):
            sender = row.get("send_user")
            if (isinstance(sender, Mapping) and sender.get("is_client") is False
                    and _message_body(row.get("description")) == _message_body(body)):
                return work_sync._id(row.get("id"))
        return None

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        if intent.get("action") == "external_action":
            payload = intent["payload"]
            url, slot, body = payload.get("url"), payload.get("slot"), payload.get("completion_body")
            if not isinstance(url, str) or not isinstance(slot, Mapping) or not isinstance(body, str):
                return {"authoritative_absent": False}
            bookings, _slots = self._booking_snapshot(url)
            booked = any(str(item.get("slot_start")) == str(slot.get("start"))
                         and str(item.get("slot_end")) == str(slot.get("end")) for item in bookings)
            calendar = booked and self._calendar_contains(slot)
            message_id = self._reply_exists(intent["thread_id"], body)
            if booked and calendar and message_id:
                receipt_id = hashlib.sha256(
                    f"{slot['start']}:{message_id}".encode()
                ).hexdigest()[:32]
                return {"verified": True, "provider_receipt_id": receipt_id,
                        "observed_at": _now()}
            if booked and calendar and not message_id:
                return {"resume_required": True}
            if not booked and not message_id:
                return {"authoritative_absent": True}
            return {"authoritative_absent": False}
        body = intent["payload"].get("body")
        if not isinstance(body, str):
            return {"authoritative_absent": True}
        rows = self._fetch_messages(intent["thread_id"])
        provider_id = self._posted.get(intent["effect_key"])
        found = None
        for row in rows:
            if (work_sync._id(row.get("board_id")) == intent["thread_id"]
                    and _message_body(row.get("description")) == _message_body(body)):
                message_id = work_sync._id(row.get("id"))
                if provider_id is None or provider_id == message_id:
                    found = message_id
                    break
        if found is None:
            return {"authoritative_absent": True}
        return {"verified": True, "provider_receipt_id": found, "observed_at": _now()}

    def close(self) -> None:
        if self.page is not None:
            work_sync._cleanup(self.page, self.browser)
        self.page = self.browser = None
        if self._lock is not None:
            lock, self._lock = self._lock, None
            lock.__exit__(None, None, None)


def compose(context: dict[str, Any], state_path: Path) -> str | None:
    conversation = context.get("conversation") or []
    board = context["board"]
    messages = [
        {"id": item["event_id"], "description": item["body"],
         "is_required_reply": item["role"] == "buyer"}
        for item in conversation
    ]
    return work_sync._compose_reply(
        board, messages, state_path,
        {**dict(context.get("grounding") or {}),
         "verified_proposal": context.get("verified_proposal")},
    )


def build(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-path", required=True, type=Path)
    parser.add_argument(
        "--candidate-profile", type=Path,
        default=Path.home() / ".config/anicca/job-search/profile.json",
    )
    parser.add_argument(
        "--provider-profile", type=Path,
        default=Path.home() / ".config/anicca/crowdworks/public-profile.json",
    )
    args = parser.parse_args(argv)
    state_path = args.state_path.expanduser().resolve()
    grounding = reply_grounding.build_reply_grounding(
        candidate_profile_path=args.candidate_profile,
        provider_profile_path=args.provider_profile,
    )
    adapter = LancersReplyAdapter(
        state_path, grounding, args.candidate_profile.expanduser().resolve()
    )

    return adapter, reply_planner.ReplyPlanner(
        lambda context: compose(context, state_path)
    )
