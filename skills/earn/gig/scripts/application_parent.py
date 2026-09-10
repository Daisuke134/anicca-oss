#!/usr/bin/env python3
"""Parent-owned, single-target application commit boundary.

The model produces only decisions. This module performs deterministic validation,
durable intent fencing, and the serialized browser effect sequence through an injected
parent effect adapter. It intentionally never imports the old eligibility gate: deciding
whether a listing is feasible remains model judgment.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import application_effect_fence as fence
import gig_disk_guard
import application_snapshot as snapshot_contract
from application_planner import validate_decisions
from listing_inventory import _cdp_connect
from market_snapshot import MARKET_FIELDS, parse_market

try:
    import websockets
except ImportError as error:  # pragma: no cover - production dependency failure
    raise RuntimeError("application_parent_requires_websockets") from error


class ParentContractError(ValueError):
    """A deterministic boundary contract is incomplete or unsafe."""


class CrashInjected(RuntimeError):
    """Test-only interruption after a durable boundary checkpoint."""


def _publish_instant_work_events(ledger_path: Path, pass_id: str) -> None:
    """Project and publish verified application facts without owning the outcome.

    The application ledger is the business truth.  Reporting is a separate, retryable
    side effect: a projector/reporter failure must leave the confirmed application intact
    so the next wake's startup recovery can retry it.
    """
    if os.environ.get("GIG_INSTANT_REPORTS_ENABLED", "1") != "1":
        return
    gig_dir = Path(ledger_path).parent
    scripts_dir = Path(__file__).resolve().parent
    projector = scripts_dir / "work_event_projector.py"
    reporter = scripts_dir / "apply_telegram_report.py"
    try:
        projected = subprocess.run(
            [
                sys.executable,
                str(projector),
                "--gig-dir",
                str(gig_dir),
                "--applications",
                str(ledger_path),
                "--pass-id",
                str(pass_id),
                "--output",
                str(gig_dir / "work-events.jsonl"),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if projected.returncode != 0:
            return
        subprocess.run(
            [
                sys.executable,
                str(reporter),
                "--gig-dir",
                str(gig_dir),
                "--telegram-database",
                str(gig_dir / "telegram-outbox.sqlite3"),
            ],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


_CLOSED_DETAIL_CATEGORY = "募集終了"
DEFAULT_DISCOVERY_SHARDS = 4
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 600.0
LEASE_COMMAND_TIMEOUT_SECONDS = 35


class ReadbackScanTimeout(ParentContractError):
    """A CDP timeout while walking an UNRELATED entry during an exact-id readback scan.

    _official_readback_async visits other applicants' offer pages to find one exact
    request id. A hang on one of THOSE pages is not this candidate's own action wedging
    -- see readback_inconclusive_row / readback_inconclusive_result. Misattributing it as
    a strike quarantined 24 eligible candidates, incl. two ¥100,000 jobs, on 2026-08-09
    (§FG' scout; §FK' port). Also raised when a bounded walk exhausts its page budget
    with a next link remaining: truncation proves nothing about absence.
    """


_REQUEST_URL = re.compile(r"^/requests/([0-9]+)$")
_APPLIED_OFFERS_URL = "https://coconala.com/mypage/job_matching/applied/offers"

_APPLIED_OFFERS_PATH = "/mypage/job_matching/applied/offers"
_COCONALA_HOSTS = {"coconala.com", "www.coconala.com"}
# Bounded, not infinite: a real applied-offers history can run to hundreds of entries and
# every extra page costs one more live navigate. 10 pages (~200 rows at ~20/page) comfortably
# covers a day's application volume for one exact id without letting one pass scan forever.
_APPLIED_OFFERS_MAX_PAGES = 10

SUBMIT_REQUIRED = "submit_required"
HARD_PROHIBITED = "hard_prohibited"
DUPLICATE_FENCED = "duplicate_fenced"


def _official_offer_price_bounds(text: object) -> tuple[int | None, int | None]:
    """Parse only Coconala's official numeric offer limits, never listing semantics."""

    value = str(text or "")

    def amount(match: re.Match[str], number_group: int, unit_group: int) -> int:
        number = int(match.group(number_group).replace(",", ""))
        return number * (10_000 if match.group(unit_group) == "万" else 1)

    minimum_match = re.search(r"最低提案価格は\s*([0-9][0-9,]*)\s*(万)?円", value)
    maximum_match = re.search(r"提案額は\s*([0-9][0-9,]*)\s*(万)?円まで", value)
    minimum = amount(minimum_match, 1, 2) if minimum_match else None
    maximum = amount(maximum_match, 1, 2) if maximum_match else None
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ParentContractError("official_offer_price_bounds_invalid")
    return minimum, maximum


def _price_within_official_bounds(price_jpy: int, text: object) -> int:
    minimum, maximum = _official_offer_price_bounds(text)
    price = int(price_jpy)
    if minimum is not None:
        price = max(price, minimum)
    if maximum is not None:
        price = min(price, maximum)
    return price


PRICING_BASIS = "planner_selected_v1"


def _money_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def commercial_offer_price(
    detail: dict[str, object], *, planner_price_jpy: int,
) -> int | None:
    """Preserve the planner's listing-grounded price; the form clamps official bounds."""
    return _money_or_none(planner_price_jpy)


def commercial_proposal_text(
    planner_text: str, *, price_jpy: int, deliver_date: str
) -> str:
    """Turn semantic planner copy into a decisive, zero-question offer."""
    burden_markers = (
        "いただけますか", "教えてください", "ご共有ください", "ご教示ください",
        "確認させてください", "確認が必要", "分かりかね", "判断できません",
    )
    pieces = re.split(r"(?<=[。！？!?])", str(planner_text or ""))
    kept = [
        piece.strip()
        for piece in pieces
        if piece.strip()
        and "?" not in piece
        and "？" not in piece
        and not any(marker in piece for marker in burden_markers)
    ]
    body = "".join(kept).strip()
    if body.startswith("対応可能です。"):
        body = body.removeprefix("対応可能です。").lstrip()
    if len(body) < 100:
        body += (
            "ご要望に沿った完成物まで責任を持って進めます。着手後は必要な情報をトークルームで整理し、"
            "進捗を分かりやすく共有します。初回納品の段階からそのままご利用いただける品質に整えます。"
            "途中で認識差が生じないよう、成果物の目的と完成条件をこちらで整理してから制作します。"
            "使いやすさと保守性も含め、納品後の運用で困らない形に仕上げます。"
        )
    body = body[:2_800]
    closing = (
        f"上記内容を{price_jpy:,}円で対応し、{deliver_date}までに納品します。"
        "納品後も、契約範囲内でご納得いただけるまで修正に対応します。"
    )
    return f"対応可能です。{body}{closing}"


def apply_commercial_offer_contract(
    snapshot: dict[str, object], decisions: dict[str, object]
) -> dict[str, object]:
    details = {
        str(detail["request_id"]): detail
        for detail in snapshot["request_details"]
        if isinstance(detail, dict)
    }
    rows: list[object] = []
    for raw in decisions["decisions"]:
        if not isinstance(raw, dict) or raw.get("business_class") != SUBMIT_REQUIRED:
            rows.append(raw)
            continue
        detail = details[str(raw["request_id"])]
        price = commercial_offer_price(
            detail,
            planner_price_jpy=int(raw["price_jpy"]),
        )
        if price is None:
            continue
        row = dict(raw)
        row["price_jpy"] = price
        row["proposal_text"] = commercial_proposal_text(
            str(raw["proposal_text"]),
            price_jpy=price,
            deliver_date=str(raw["deliver_date"]),
        )
        rows.append(row)
    return {"decisions": rows}


async def settle_after_click(read, predicate, *, deadline_seconds: float = 15.0, interval: float = 0.25):
    """Read the page until the click has visibly landed, or the deadline passes.

    The parent used to sleep a fixed 0.75 seconds after clicking 応募する and read once. A
    submit that navigated more slowly than that was scored as "still on the form", so
    click_submit retried — and by then the browser had reached the applied-offers list, where
    there is correctly no 応募する button. A submit that had worked came back as
    application_応募する_button_missing, and nothing was written to applied.jsonl. Seventy
    hours of no recorded applications sit on top of that one guess about page speed.

    Returns the last observation either way: a click that genuinely did nothing still has to
    be screenshotted and reported, not raised through.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    observed = await read()
    while loop.time() < deadline:
        if predicate(observed.get("url"), observed.get("body")):
            return observed
        await asyncio.sleep(interval)
        observed = await read()
    return observed


def submit_landed(url: object, body: object = None) -> bool:
    """Did the submit click land us on the page Coconala shows after a successful apply?

    This used to also require the string 応募しました in the body. That string is a toast:
    it is gone by the time the page is read. On 2026-08-05 the loop's own evidence showed 41
    failures named application_応募する_button_missing, 37 of them sitting on the
    applied-offers page — the success destination — with our live applications visible on it
    (450,000円 and 200,000円 proposals among them). The submit had worked; the toast check
    scored it as failed; the retry then correctly found no 応募する button on a page that has
    none, and the pass died.

    Dropping the toast does not weaken anything. Landing is the site's answer to the click,
    and the proof is _official_readback_async, which loads this same page and requires the
    offer id to appear under a[href*="/mypage/offers/"]. Verifying twice where one of the two
    checks is a disappearing string is how a working lane got recorded as a dead one.

    `body` is kept in the signature because callers pass it and because the next person will
    want to know it was considered and deliberately not used.
    """
    parsed = urlsplit(str(url or ""))
    if parsed.hostname not in _COCONALA_HOSTS:
        return False
    return parsed.path.rstrip("/") == _APPLIED_OFFERS_PATH

def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class LeaseHandle:
    """Fenced parent lease with a heartbeat thread and unconditional release.

    The handle is deliberately the only component that knows the lease executable.
    Planner code receives no lease values or environment-derived browser route.
    """

    def __init__(
        self,
        *,
        lease_script: Path,
        task: str,
        heartbeat_seconds: float = 20.0,
    ) -> None:
        if not task.strip():
            raise ParentContractError("lease_task_required")
        if heartbeat_seconds <= 0 or heartbeat_seconds > 30:
            raise ParentContractError("lease_heartbeat_seconds_invalid")
        self.lease_script = Path(lease_script)
        self.task = task
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.value: dict[str, object] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_error: str | None = None
        # Guards self.value across recycle() (main thread) and _heartbeat_loop
        # (background thread) so a beat can never carry a fence read before a
        # swap into a lease script call that lands after the swap.
        self._value_lock = threading.Lock()

    def _run(self, *arguments: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(self.lease_script), *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=LEASE_COMMAND_TIMEOUT_SECONDS,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            raise ParentContractError("lease_command_no_json")
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as error:
            raise ParentContractError("lease_command_invalid_json") from error
        if completed.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("reason") if isinstance(result, dict) else "unknown"
            raise ParentContractError(f"lease_command_failed:{reason}")
        return result

    @property
    def lease_fence(self) -> dict[str, object]:
        if self.value is None:
            raise ParentContractError("lease_not_acquired")
        return {
            "task": self.task,
            "token": self.value["token"],
            "generation": self.value["generation"],
        }

    @property
    def ws_url(self) -> str:
        if self.value is None:
            raise ParentContractError("lease_not_acquired")
        ws_url = self.value.get("ws")
        if not isinstance(ws_url, str) or not ws_url.startswith("ws://"):
            raise ParentContractError("lease_ws_invalid")
        return ws_url

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            with self._value_lock:
                fence_value = self.lease_fence
                for attempt in range(2):
                    try:
                        self._run(
                            "heartbeat",
                            self.task,
                            "--token",
                            str(fence_value["token"]),
                            "--generation",
                            str(fence_value["generation"]),
                        )
                        break
                    except subprocess.TimeoutExpired as error:
                        # acquire() may briefly hold the shared lease ledger while it
                        # replaces a dead browser context. One 35-second timeout does
                        # not invalidate this task's token; confirm it once before
                        # failing the irreversible-effect fence.
                        if attempt == 0 and not self._stop.is_set():
                            continue
                        self._heartbeat_error = f"{type(error).__name__}:{error}"
                        return
                    except Exception as error:  # A real fence error stays fail-closed.
                        self._heartbeat_error = f"{type(error).__name__}:{error}"
                        return

    def assert_healthy(self) -> None:
        if self._heartbeat_error is not None:
            raise ParentContractError(f"lease_heartbeat_failed:{self._heartbeat_error}")

    def _rollback_enter(self, acquired: dict[str, object], fenced: bool) -> None:
        self._stop.set()
        try:
            if self._thread is not None:
                try:
                    self._thread.join(timeout=LEASE_COMMAND_TIMEOUT_SECONDS + 1.0)
                except BaseException:
                    pass
            if fenced:
                self._run(
                    "release",
                    self.task,
                    "--token",
                    str(acquired["token"]),
                    "--generation",
                    str(acquired["generation"]),
                )
            else:
                self._run("release", self.task)
        except BaseException:
            pass
        finally:
            self.value = self._thread = self._heartbeat_error = None

    def __enter__(self) -> "LeaseHandle":
        value = self._run("acquire", self.task)
        token = value.get("token")
        generation = value.get("generation")
        ws_url = value.get("ws")
        if (
            not isinstance(token, str)
            or re.fullmatch(r"[0-9a-f]{32}", token) is None
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not isinstance(ws_url, str)
            or not ws_url.startswith("ws://")
        ):
            fenced = (
                isinstance(token, str)
                and re.fullmatch(r"[0-9a-f]{32}", token) is not None
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and generation >= 1
            )
            self._rollback_enter(value, fenced)
            raise ParentContractError("lease_acquire_contract_invalid")
        self.value = value
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"application-lease-heartbeat-{self.task}",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            self._rollback_enter(value, True)
            raise
        return self

    def recycle(self) -> str:
        """Trade a dead target for a live one, same task, new fence.

        A submission page can kill its renderer and take the leased target's CDP endpoint
        with it (measured 2026-08-06: snapshot walked 36 sources, then every submission
        timed out on the shared target). Release and re-acquire under the same task name;
        the lease script disposes the old context and builds a fresh one. The whole
        release -> acquire -> swap sequence holds _value_lock so the heartbeat thread
        can never read the old fence and issue its subprocess call after the swap
        (that carried the old token into the new lease row -> lease_fence_mismatch ->
        heartbeat thread dies -> assert_healthy kills the pass; measured 2026-08-08).
        """
        if self.value is None:
            raise ParentContractError("lease_not_acquired")
        with self._value_lock:
            fence_value = self.lease_fence
            self._run(
                "release",
                self.task,
                "--token",
                str(fence_value["token"]),
                "--generation",
                str(fence_value["generation"]),
            )
            value = self._run("acquire", self.task)
            ws_url = value.get("ws")
            if not isinstance(ws_url, str) or not ws_url.startswith("ws://"):
                raise ParentContractError("lease_recycle_contract_invalid")
            self.value = value
        return ws_url

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self._stop.set()
        heartbeat_error: BaseException | None = None
        if self._thread is not None:
            self._thread.join(timeout=LEASE_COMMAND_TIMEOUT_SECONDS + 1.0)
            if self._thread.is_alive():
                heartbeat_error = ParentContractError("lease_heartbeat_thread_stuck")
        try:
            self.assert_healthy()
        except BaseException as error:
            if heartbeat_error is None:
                heartbeat_error = error
        release_error: Exception | None = None
        if self.value is not None:
            try:
                fence_value = self.lease_fence
                self._run(
                    ("park" if os.environ.get("CLOAK_CONTEXT_PARK_ON_IDLE") == "1"
                     else "release"),
                    self.task,
                    "--token",
                    str(fence_value["token"]),
                    "--generation",
                    str(fence_value["generation"]),
                )
            except Exception as error:
                release_error = error
        if release_error is not None and exc_type is None:
            if heartbeat_error is not None:
                raise heartbeat_error
            raise ParentContractError(f"lease_release_failed:{release_error}")
        if heartbeat_error is not None and exc_type is None:
            raise heartbeat_error
        return False


class ParentEffects(Protocol):
    """The only effect surface the parent commit algorithm accepts."""

    @contextlib.contextmanager
    def target_lock(self) -> Iterator[None]: ...

    def reextract_detail(self, request_id: str) -> dict[str, object]: ...

    def open_form(self, request_id: str) -> None: ...

    def adjust_offer_price(self, request_id: str, price_jpy: int) -> int: ...

    def fill_form(self, request_id: str, proposal_text: str, price_jpy: int, deliver_date: str) -> None: ...

    def readback_form(self, request_id: str) -> dict[str, object]: ...

    def click_confirm(self, request_id: str) -> None: ...

    def click_submit(self, request_id: str) -> None: ...

    def authoritative_exact_id_readback(self, request_id: str) -> bool: ...

    def canonical_ledger_append(self, row: dict[str, object]) -> None: ...

    def crash_if_requested(self, checkpoint: str) -> None: ...


def _target_lock_path(ws_url: str) -> Path:
    target_id = urlsplit(ws_url).path.rstrip("/").split("/")[-1]
    if not target_id:
        raise ParentContractError("leased_target_id_missing")
    leases_file = Path(os.path.expanduser(os.environ.get(
        "CLOAK_CONTEXT_LEASES_FILE", "~/.cloak/vault/leases.json"
    )))
    return leases_file.parent / "operations" / f"{target_id}.lock"


def _page_index(url: str) -> int:
    pages = [value for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True) if key == "page"]
    if not pages:
        return 1
    if len(pages) != 1 or re.fullmatch(r"[1-9][0-9]*", pages[0]) is None:
        raise ParentContractError("source_page_index_invalid")
    return int(pages[0])


def _is_expected_offer_form_url(request_id: str, url: object) -> bool:
    """Accept Coconala's cache-busting `_t` query, but no identity-changing route."""
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"coconala.com", "www.coconala.com"}
        or parsed.path.rstrip("/") != f"/offers/add/{request_id}"
        or parsed.fragment
    ):
        return False
    query = parse_qsl(parsed.query, keep_blank_values=True)
    return all(key == "_t" and value for key, value in query)


def _mouse_click_event_params(x: float, y: float) -> tuple[dict[str, object], ...]:
    return (
        {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0, "clickCount": 0},
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1},
    )


_LIFECYCLE_FIELDS = ("page_state", "accepting_control", "deadline_state", "deadline_value", "form_state")
_LIFECYCLE_ALLOWED = ({"present", "not_found", "unknown"}, {"present", "absent", "unknown"}, {"future", "expired", "unknown"}, None, {"present", "absent", "unknown"})

def _japan_today() -> dt.date:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()


def _lifecycle_digest(request_id: object, canonical_url: object, **fields: object) -> str:
    return hashlib.sha256(snapshot_contract.canonical_json_bytes({"request_id": str(request_id), "canonical_url": str(canonical_url), **{field: fields[field] for field in _LIFECYCLE_FIELDS}})).hexdigest()


def _lifecycle_observation(detail: object) -> dict[str, object] | None:
    if not isinstance(detail, dict): return None
    if not all(field in detail for field in _LIFECYCLE_FIELDS):
        return None
    values = {field: detail[field] for field in _LIFECYCLE_FIELDS}
    if any(value not in allowed for value, allowed in zip(values.values(), _LIFECYCLE_ALLOWED) if allowed is not None): return None
    if values["deadline_value"] is not None and (not isinstance(values["deadline_value"], str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", values["deadline_value"]) is None):
        return None
    if values["deadline_value"] is not None:
        try: deadline = dt.date.fromisoformat(values["deadline_value"])
        except ValueError: return None
        if values["deadline_state"] != ("expired" if deadline < _japan_today() else "future"): return None
    request_id = str(detail.get("request_id") or "")
    canonical_url = str(detail.get("canonical_url") or "")
    try: canonical = snapshot_contract.canonical_request_url(canonical_url, request_id=request_id)
    except Exception: return None
    if canonical != canonical_url: return None
    values.update({"request_id": request_id, "canonical_url": canonical_url})
    digest = detail.get("lifecycle_sha256")
    if not isinstance(digest, str) or digest != _lifecycle_digest(request_id, canonical_url, **{field: values[field] for field in _LIFECYCLE_FIELDS}): return None
    values["lifecycle_sha256"] = digest
    return values


def _lifecycle_disposition(detail: object) -> tuple[str, list[str]]:
    observation = _lifecycle_observation(detail)
    if observation is None:
        if isinstance(detail, dict) and not any(field in detail for field in _LIFECYCLE_FIELDS):
            return ("legacy", [])
        return ("unknown", ["lifecycle_observation_invalid"])
    expected = {"page_state": "present", "accepting_control": "present", "deadline_state": "future", "form_state": "present"}
    reasons = [f"{field}:{observation[field]}" for field in expected if observation[field] != expected[field]]
    return ("open" if not reasons else ("official_unavailable" if observation["page_state"] == "not_found" or not any(observation[field] == "unknown" for field in expected) else "unknown"), reasons)


def _strict_next_page(current: str, candidate: object, *, path: str = "/requests") -> str | None:
    """Accept the official next page while retaining filters omitted by its href.

    Coconala's numbered pagination links contain only ``?page=N`` even when the
    current listing is filtered by ``recruiting=true&sort=new``.  The page number
    comes from the official DOM; every non-page query parameter remains owned by
    the current source URL so pagination cannot silently drop the open/new filter.
    """
    if not isinstance(candidate, str):
        return None
    before = urlsplit(current)
    after = urlsplit(candidate)
    if (
        before.scheme != "https"
        or after.scheme != "https"
        or before.hostname not in {"coconala.com", "www.coconala.com"}
        or after.hostname not in {"coconala.com", "www.coconala.com"}
        or before.path.rstrip("/") != path
        or after.path.rstrip("/") != path
    ):
        return None

    def position(parsed: object) -> tuple[list[tuple[str, str]], int] | None:
        assert hasattr(parsed, "query")
        stable: list[tuple[str, str]] = []
        page: int | None = None
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):  # type: ignore[attr-defined]
            if key != "page":
                stable.append((key, value))
                continue
            if page is not None or re.fullmatch(r"[1-9][0-9]*", value) is None:
                return None
            page = int(value)
        return sorted(stable), page or 1

    before_position = position(before)
    after_position = position(after)
    if before_position is None or after_position is None:
        return None
    before_stable, before_page = before_position
    after_stable, after_page = after_position
    if after_page <= before_page or (after_stable and after_stable != before_stable):
        return None
    retained = [
        (key, value)
        for key, value in parse_qsl(before.query, keep_blank_values=True)
        if key != "page"
    ]
    retained.append(("page", str(after_page)))
    return urlunsplit(("https", "coconala.com", after.path.rstrip("/"), urlencode(retained), ""))


class CdpParentEffects:
    """The only live browser adapter for the application commit boundary.

    Every CDP call reconnects to the *same leased target* and the outer target lock
    spans the complete commit.  This adapter deliberately has no hidden-target,
    model-command, or eligibility-policy path.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        evidence_dir: Path,
        ledger_path: Path,
        pass_id: str,
    ) -> None:
        if not ws_url.startswith("ws://"):
            raise ParentContractError("leased_ws_invalid")
        self.ws_url = ws_url
        self.evidence_dir = Path(evidence_dir)
        self.ledger_path = Path(ledger_path)
        self.pass_id = pass_id
        self._fresh_details: dict[str, dict[str, object]] = {}
        self._submitted_paths: dict[str, Path] = {}
        self._readback_paths: dict[str, Path] = {}
        self._form_state_async_result: dict[str, object] | None = None
        self.instant_work_event_publisher: Callable[[Path, str], None] | None = None
        # Wired by run(): trades a dead leased target for a fresh one (ParentLease.recycle).
        self.ws_recycler: Any = None
        self._target_lock_handle: Any = None

    def _acquire_target_lock(self) -> None:
        path = _target_lock_path(self.ws_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self._target_lock_handle = handle

    def _release_target_lock(self) -> None:
        handle, self._target_lock_handle = self._target_lock_handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def recover_wedged_target(self) -> bool:
        """Replace a dead target between candidates instead of losing the rest of the wake.

        The old operation lock is released BEFORE the recycler runs: the lease's release
        takes LOCK_EX on the same file to guard the dispose, and on 2026-08-06 06:51 the
        parent held that lock through the whole commit -- so it asked its own subprocess to
        acquire a lock it was itself holding, and the recovery deadlocked until the
        35-second subprocess limit killed it. Sequential replacement keeps the one-target
        rule: old lock gone, old context disposed, then the fresh target's lock is taken
        and guards the rest of the commit.
        """
        if not callable(self.ws_recycler):
            return False
        self._release_target_lock()
        self.ws_url = str(self.ws_recycler())
        self._acquire_target_lock()
        return True

    @contextlib.contextmanager
    def target_lock(self) -> Iterator[None]:
        self._acquire_target_lock()
        try:
            yield
        finally:
            # After a mid-commit recovery this is the NEW target's lock; the old one was
            # already released at the recovery boundary.
            self._release_target_lock()

    async def _call(
        self,
        ws: Any,
        method: str,
        params: dict[str, object],
        call_id: int,
        timeout_seconds: float = 30,
    ) -> dict[str, object]:
        await ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
        # The deadline covers the whole call, not each recv.
        #
        # Found by writing a peer that answers every request with the wrong call id: the loop
        # `continue`s past mismatched ids, so each individual recv returned instantly, the
        # per-recv timeout never fired, and _call spun forever. A browser that keeps talking
        # while never answering the question we asked is indistinguishable from a healthy one
        # under a per-recv timeout — and this loop has been losing whole passes to a CDP call
        # that never came back.
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ParentContractError(f"cdp_{method}_timeout_after_{timeout_seconds}s")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError as error:
                # asyncio's TimeoutError carries no message, which is why this arrived as
                # error:"" until 2026-08-05 and then as a bare TimeoutError afterwards. Every
                # CDP call in this file goes through here, so without the method name the
                # loop cannot tell a hung navigate from a hung screenshot — and it already
                # knows, in domain-skills, that Page.navigate from a filled composer stops
                # the renderer and takes CDP down with it. The name is what makes that
                # knowledge usable.
                raise ParentContractError(
                    f"cdp_{method}_timeout_after_{timeout_seconds}s"
                ) from error
            response = json.loads(raw)
            if response.get("id") != call_id:
                continue
            if response.get("error"):
                raise ParentContractError(f"cdp_{method}_failed")
            result = response.get("result")
            return result if isinstance(result, dict) else {}

    async def _ready(self, ws: Any, call_id: int) -> int:
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            result = await self._call(
                ws,
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
                call_id,
            )
            call_id += 1
            state = ((result.get("result") or {}) if isinstance(result, dict) else {}).get("value")
            if state in {"interactive", "complete"}:
                return call_id
            if asyncio.get_running_loop().time() >= deadline:
                raise ParentContractError("cdp_navigation_timeout")
            await asyncio.sleep(0.2)

    async def _navigate(self, ws: Any, url: str, call_id: int) -> int:
        await self._call(ws, "Page.navigate", {"url": url}, call_id)
        return await self._ready(ws, call_id + 1)

    async def _settle_on_offer_form(
        self, ws: Any, request_id: str, call_id: int, *, seconds: float = 8.0
    ) -> int:
        """Wait until the document is the one we navigated to, not the one we are leaving.

        `_ready` polls document.readyState, which is still 'complete' for the previous document
        until Page.navigate commits. The first evaluate after navigating therefore reads the old
        page's location, and the offer form was judged 'redirected' against it.

        Measured 2026-09-07: every observed Coconala listing reported form_state:absent with
        url='https://coconala.com/' and **an empty title** -- the real top page has a title, so
        the document had not rendered. Coconala had applied to nothing since 2026-09-02.

        Returning at the deadline is deliberate: a genuine redirect must still be reported, and
        the caller then records the settled URL and title rather than a half-navigated one.
        """
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            state, call_id = await self._eval_json(
                ws,
                "JSON.stringify({url:location.href,ready:document.readyState})",
                call_id,
            )
            if (
                _is_expected_offer_form_url(request_id, state.get("url"))
                and state.get("ready") in {"interactive", "complete"}
            ):
                return call_id
            if asyncio.get_running_loop().time() >= deadline:
                return call_id
            await asyncio.sleep(0.25)

    async def _navigate_retry_once(self, ws: Any, url: str, call_id: int) -> int:
        """Retry ONE hung navigate before giving up on it.

        A transient hang on a single page must not cost the whole readback walk; a target
        that is genuinely dead still fails on the second attempt. The retry uses a call id
        far past anything else in flight -- _call only ever matches on id and silently skips
        the rest, so a late straggler from the first attempt must not be mistaken for the
        retry's own answer.
        """
        try:
            return await self._navigate(ws, url, call_id)
        except ParentContractError as error:
            if "timeout" not in str(error):
                raise
            return await self._navigate(ws, url, call_id + 1000)

    async def _eval_json(self, ws: Any, expression: str, call_id: int) -> tuple[dict[str, object], int]:
        result = await self._call(
            ws,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            call_id,
        )
        value = ((result.get("result") or {}) if isinstance(result, dict) else {}).get("value")
        if not isinstance(value, str):
            raise ParentContractError("cdp_dom_value_missing")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ParentContractError("cdp_dom_json_invalid") from error
        if not isinstance(decoded, dict):
            raise ParentContractError("cdp_dom_object_invalid")
        return decoded, call_id + 1

    async def _screenshot(self, ws: Any, call_id: int) -> tuple[bytes, int]:
        result = await self._call(ws, "Page.captureScreenshot", {"format": "png"}, call_id)
        encoded = result.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ParentContractError("cdp_screenshot_missing")
        try:
            return base64.b64decode(encoded), call_id + 1
        except ValueError as error:
            raise ParentContractError("cdp_screenshot_invalid") from error

    @staticmethod
    def _safe_name(value: str) -> str:
        """A per-source filename that survives having its non-ASCII stripped.

        Coconala category ids are Japanese, and the old rule replaced every non-ASCII
        run with "-", so 「ロゴ作成・ロゴデザイン」 and 「外国語翻訳」 both sanitised to the
        same empty stem. Measured 2026-08-07 against the live objective: 87 source ids
        collapsed onto 26 filenames, 48 of them sharing a single one. Each source then
        overwrote the previous source's DOM and screenshot, so per-source evidence read
        back as missing -- which is where 57-61 search_source_not_observed a pass came
        from. The categories were searched; the proof overwrote itself. A digest of the
        raw id keeps the readable ASCII stem and still separates every source.
        """
        ascii_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        return f"{ascii_stem}-{digest}" if ascii_stem else f"source-{digest}"

    async def _source_async(self, source_id: str, url: str) -> tuple[dict[str, object], bytes]:
        async with await _cdp_connect(self.ws_url) as ws:
            call_id = 1
            await self._call(ws, "Page.enable", {}, call_id)
            call_id = await self._navigate_retry_once(ws, url, call_id + 1)
            page, call_id = await self._eval_json(
                ws,
                """JSON.stringify((()=>{
                  const text=document.body?.innerText||'';
                  const anchors=[...document.querySelectorAll('a[href]')];
                  const hrefs=anchors.map(a=>a.href);
                  const here=new URL(location.href);
                  const currentPage=Number(here.searchParams.get('page')||'1');
                  const cleanPath=value=>value.replace(/\\/+$/,'');
                  const next=anchors.find(a=>{
                    try {
                      const target=new URL(a.href,location.href);
                      return target.origin===here.origin
                        && cleanPath(target.pathname)===cleanPath(here.pathname)
                        && target.searchParams.has('page')
                        && Number(target.searchParams.get('page'))===currentPage+1;
                    } catch (_error) { return false; }
                  });
                  return {url:location.href,title:document.title,text,
                    hrefs,next_href:next?.href||null,
                    access_denied:document.title==='403 Forbidden'||document.title==='Access Denied',
                    not_found:/404|ページが見つかりません|お探しのページ/.test(document.title)};
                })())""",
                call_id,
            )
            screenshot, _ = await self._screenshot(ws, call_id)
        if page.get("access_denied") is True:
            raise ParentContractError(f"source_access_denied:{source_id}")
        if page.get("not_found") is True:
            # Carry what was actually seen. The decision is made from document.title alone, so
            # without the title a false positive on the pattern and a real missing page look
            # identical -- and the authenticated title is the only one that can be checked later.
            title = " ".join(str(page.get("title") or "").split())[:120]
            raise ParentContractError(f"source_not_found:{source_id} title={title!r}")
        return page, screenshot

    def collect_source(
        self, source_id: str, url: str, remaining: int
    ) -> tuple[dict[str, object], list[str], dict[str, str], str | None]:
        """Observe one source page and preserve hashes for the planner envelope."""
        page, screenshot = asyncio.run(self._source_async(source_id, url))
        current_url = str(page.get("url") or "")
        try:
            canonical_url = snapshot_contract.canonical_source_url(current_url)
        except ValueError as error:
            raise ParentContractError(f"source_redirect_or_invalid:{source_id}") from error
        hrefs = page.get("hrefs")
        if not isinstance(hrefs, list):
            raise ParentContractError(f"source_hrefs_missing:{source_id}")
        all_ids: list[str] = []
        seen: set[str] = set()
        for href in hrefs:
            try:
                request_id = snapshot_contract.canonical_request_url(href).rsplit("/", 1)[-1]
            except ValueError:
                continue
            if request_id not in seen:
                seen.add(request_id)
                all_ids.append(request_id)
        selected = all_ids[:max(0, remaining)]
        next_url = _strict_next_page(current_url, page.get("next_href"))
        # Pagination is official page state. Local batch truncation must not
        # fabricate a successor after the marketplace's terminal page.
        has_next = next_url is not None
        dom = {
            "url": canonical_url,
            "not_found": False,
            "observed": True,
            "title": str(page.get("title") or ""),
            "card_request_ids": selected,
            "next_url": next_url,
            "text": str(page.get("text") or ""),
        }
        name = self._safe_name(source_id)
        screenshot_path = self.evidence_dir / f"parent-B2-source-{name}-{_page_index(canonical_url)}.png"
        dom_path = self.evidence_dir / f"parent-B2-source-{name}-{_page_index(canonical_url)}.json"
        _atomic_bytes(screenshot_path, screenshot)
        _atomic_bytes(dom_path, snapshot_contract.canonical_json_bytes(dom) + b"\n")
        source = {
            "source_id": source_id,
            "url": canonical_url,
            "page_index": _page_index(canonical_url),
            "card_request_ids": selected,
            "has_next": has_next,
            "exhausted": not has_next,
            "screenshot_sha256": _sha256_bytes(screenshot),
            "dom_sha256": _sha256_bytes(dom_path.read_bytes()),
        }
        return source, selected, {
            "screenshot_path": str(screenshot_path.resolve()),
            "live_dom_path": str(dom_path.resolve()),
        }, next_url

    @staticmethod
    def _category_from_text(text: str, candidate: object) -> str:
        normalized = snapshot_contract.normalize_visible_text(text)
        lines = [line.strip() for line in normalized.splitlines()]
        for index, line in enumerate(lines[:-1]):
            if line in {"カテゴリー", "カテゴリ"}:
                for following in lines[index + 1:]:
                    if following:
                        return following
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        raise ParentContractError("detail_category_missing")

    async def _detail_async(self, request_id: str) -> dict[str, object]:
        request_url = f"https://coconala.com/requests/{request_id}"
        async with await _cdp_connect(self.ws_url) as ws:
            call_id = 1
            await self._call(ws, "Page.enable", {}, call_id)
            call_id = await self._navigate(ws, request_url, call_id + 1)
            page, _ = await self._eval_json(
                ws,
                """JSON.stringify((()=>{
                  const text=document.body?.innerText||'';
                  const visible=e=>{if(!e||e.offsetParent===null)return false;
                    const r=e.getBoundingClientRect(); return r.width>0&&r.height>0;};
                  const category=[...document.querySelectorAll(
                    '[class*="category" i],a[href*="category" i]'
                  )].map(e=>(e.innerText||'').trim()).find(Boolean)||null;
                  const title=(document.querySelector('h1')?.innerText||document.title||'').trim();
                  const control=[...document.querySelectorAll('button,a,[role="button"]')]
                    .find(e=>visible(e)&&(e.innerText||'').trim()==='応募する')||null;
                  const accepting_control=!!control;
                  // The route Coconala itself points the apply button at. Only its existence was
                  // ever read, so when /offers/add/<id> stopped working there was no way to tell a
                  // changed route from an account that may no longer offer.
                  const accepting_control_href=control?(control.getAttribute('href')||
                    control.closest('a')?.getAttribute('href')||''):'';
                  const accepting_control_tag=control?control.tagName.toLowerCase():'';
                  const row=[...document.querySelectorAll('.c-requestOutlineRow')].find(e=>
                    [...e.querySelectorAll('*')].some(child=>(child.innerText||'').trim()==='募集期限'))||null;
                  const deadline_text=row?.innerText||'';
                  const date=deadline_text.normalize('NFKC').match(
                    /締切日\\s*([0-9]{4})年\\s*([0-9]{1,2})月\\s*([0-9]{1,2})日/u);
                  const deadline_value=date?
                    `${date[1]}-${date[2].padStart(2,'0')}-${date[3].padStart(2,'0')}`:null;
                  const not_found=/^(404\\b|ページが見つかりません|お探しのページ|ご指定のページが見つかりませんでした)/u.test(title);
                  return {url:location.href,title,text,category,accepting:accepting_control,
                    accepting_control_href,accepting_control_tag,
                    accepting_control:accepting_control?'present':'absent',
                    page_state:not_found?'not_found':'present',deadline_value,deadline_text};
                })())""",
                call_id,
            )
        text = str(page.get("text") or "")
        title = str(page.get("title") or "").strip()
        # A server/WAF denial is not marketplace lifecycle evidence. Treating the
        # structural HTTP error page as a present listing with no form previously
        # converted temporary throttling into a permanent closed fast-skip.
        access_denied = title in {"403 Forbidden", "Access Denied"}
        page_state = (
            "unknown" if access_denied
            else "not_found" if title.startswith("ご指定のページが見つかりませんでした")
            else page.get("page_state")
        )
        if page_state not in _LIFECYCLE_ALLOWED[0]: page_state = "unknown"
        try:
            canonical_url = snapshot_contract.canonical_request_url(page.get("url"), request_id=request_id)
        except Exception:
            canonical_url = str(page.get("url") or "")
            page_state = "unknown"
        accepting_control = page.get("accepting_control")
        if accepting_control not in _LIFECYCLE_ALLOWED[1]: accepting_control = "present" if page.get("accepting") is True else "absent"
        deadline_value = page.get("deadline_value")
        if not isinstance(deadline_value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", deadline_value) is None: deadline_value = None
        if deadline_value is None:
            deadline_state = "unknown"
        else:
            try:
                deadline_state = "expired" if dt.date.fromisoformat(deadline_value) < _japan_today() else "future"
            except ValueError:
                deadline_state, deadline_value = "unknown", None
        structured = any(field in page for field in _LIFECYCLE_FIELDS) and not access_denied
        form_state = None
        if structured and page_state == "present":
            try:
                await self._form_state_async(request_id, navigate=True)
            except Exception as error:
                form_state = "absent" if str(error) in {"application_form_redirected", "application_form_controls_missing"} else "unknown"
                # Written beside the lifecycle row, never inside it: that row is content-hashed
                # over a fixed field list, so an extra key there fails contract validation.
                self._record_form_failure(request_id, error, control={
                    "href": str(page.get("accepting_control_href") or "")[:300],
                    "tag": str(page.get("accepting_control_tag") or "")[:20],
                })
            else:
                form_state = "present"
        accepting = (page_state == "present" and accepting_control == "present" and deadline_state == "future" and form_state == "present") if structured else page.get("accepting") is True
        try:
            category = self._category_from_text(text, page.get("category"))
        except ParentContractError:
            if accepting:
                raise
            category = _CLOSED_DETAIL_CATEGORY
        market = parse_market(text, now=time.time())
        result = {
            "request_id": request_id,
            "canonical_url": canonical_url,
            "title": str(page.get("title") or ""),
            "category": category,
            "visible_text": text,
            "accepting_applications": accepting,
            "budget_min_jpy": market.get("budget_lo_jpy"),
            "budget_max_jpy": market.get("budget_hi_jpy"),
            "applicants_count": market.get("applicants_at_bid"),
            "contracted_count": market.get("contracted_count"),
            "applicants": [],
            "observed_at": _utc_now(),
            # T3 (2026-08-09): ranking-only. application_snapshot._DETAIL_FIELDS
            # deliberately omits this key, so _normalise_detail drops it before the
            # planner ever sees the envelope -- it never becomes planner input, only
            # a sort key collect() reads once, locally, before build_envelope runs.
            "client_order_rate": market.get("client_order_rate"),
        }
        if structured:
            lifecycle = {
                "page_state": page_state,
                "accepting_control": accepting_control,
                "deadline_state": deadline_state,
                "deadline_value": deadline_value,
                "form_state": form_state or "unknown",
            }
            result.update(lifecycle)
            result["lifecycle_sha256"] = _lifecycle_digest(
                request_id, canonical_url, **lifecycle
            )
        return result

    def reextract_detail(self, request_id: str) -> dict[str, object]:
        detail = asyncio.run(self._detail_async(request_id))
        self._fresh_details[request_id] = detail
        return detail

    async def _form_state_async(
        self, request_id: str, *, navigate: bool
    ) -> dict[str, object]:
        expected_url = f"https://coconala.com/offers/add/{request_id}"
        async with await _cdp_connect(self.ws_url) as ws:
            call_id = 1
            await self._call(ws, "Page.enable", {}, call_id)
            if navigate:
                call_id = await self._navigate(ws, expected_url, call_id + 1)
                call_id = await self._settle_on_offer_form(ws, request_id, call_id + 1)
            else:
                call_id += 1
            state, call_id = await self._eval_json(
                ws,
                """JSON.stringify({url:location.href,title:document.title,
                  content:document.querySelector('textarea[name="data[Offer][content]"]')?.value||'',
                  price:document.querySelector('input[name="data[Offer][price]"]')?.value||'',
                  deliver_date:document.querySelector('input[name="data[Offer][expire_date]"]')?.value||'',
                  has_content:!!document.querySelector('textarea[name="data[Offer][content]"]'),
                  has_price:!!document.querySelector('input[name="data[Offer][price]"]'),
                  has_date:!!document.querySelector('input[name="data[Offer][expire_date]"]'),
                  price_constraints_text:(document.querySelector('input[name="data[Offer][price]"]')
                    ?.closest('.bl_form-group')?.innerText||'')})""",
                call_id,
            )
            screenshot, _ = await self._screenshot(ws, call_id)
        # Carry what was seen on the failing form. The caller collapses both of these into
        # form_state="absent", and the two have opposite fixes -- a redirect is a session or
        # routing problem, missing controls mean the provider changed the markup -- so without
        # this the lane can report "no listing is applicable" for days with no way to tell which.
        if not _is_expected_offer_form_url(request_id, state.get("url")):
            error = ParentContractError("application_form_redirected")
            error.observed = {"url": str(state.get("url") or "")[:300],
                              "title": str(state.get("title") or "")[:150]}
            raise error
        if not all(state.get(key) is True for key in ("has_content", "has_price", "has_date")):
            error = ParentContractError("application_form_controls_missing")
            error.observed = {
                "url": str(state.get("url") or "")[:300],
                "title": str(state.get("title") or "")[:150],
                **{key: bool(state.get(key)) for key in ("has_content", "has_price", "has_date")},
            }
            raise error
        form_path = self.evidence_dir / f"gig-{self.pass_id}-B2-{request_id}-form.png"
        _atomic_bytes(form_path, screenshot)
        return state

    def _record_form_failure(
        self, request_id: str, error: BaseException, control: dict[str, str] | None = None
    ) -> None:
        """Append one line naming why a listing's form was unusable. Never raises."""
        try:
            row = {
                "request_id": str(request_id),
                "error": str(error)[:120],
                "error_type": type(error).__name__,
                "observed": getattr(error, "observed", None),
                # What the listing page itself offers. If this disagrees with the route we
                # navigate to, the provider moved the form; if it agrees, the account cannot use it.
                "accepting_control": control,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            path = self.evidence_dir / "form-state-failures.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            return

    def open_form(self, request_id: str) -> None:
        self._form_state_async_result = asyncio.run(
            self._form_state_async(request_id, navigate=True)
        )

    def adjust_offer_price(self, request_id: str, price_jpy: int) -> int:
        state = self._form_state_async_result
        if not isinstance(state, dict) or not _is_expected_offer_form_url(
            request_id, state.get("url")
        ):
            raise ParentContractError("official_offer_price_bounds_unobserved")
        return _price_within_official_bounds(
            price_jpy, state.get("price_constraints_text")
        )

    def saved_nonlanding_submit_evidence(
        self, request_id: str, intent: dict[str, object]
    ) -> bool:
        """Find the original pass's bounded post-click non-landing proof."""
        lease = intent.get("lease_fence")
        task = lease.get("task") if isinstance(lease, dict) else None
        if not isinstance(task, str):
            return False
        match = re.search(r"gig-apply-direct-[0-9]+-[0-9]+", task)
        if match is None:
            return False
        origin_pass_id = match.group(0)
        apply_root = self.evidence_dir.parent.parent
        origin = apply_root / origin_pass_id
        if not origin.is_dir() or origin.parent != apply_root:
            return False
        name = f"gig-{origin_pass_id}-B2-{request_id}-submit-attempt.png"
        proofs = [path for path in origin.glob(f"*/{name}") if path.is_file()]
        return bool(proofs) and all(path.stat().st_size > 0 for path in proofs)

    async def _fill_async(
        self, request_id: str, proposal_text: str, price_jpy: int, deliver_date: str
    ) -> None:
        expected_url = f"https://coconala.com/offers/add/{request_id}"
        proposal_json = json.dumps(proposal_text, ensure_ascii=False)
        price_json = json.dumps(str(price_jpy))
        date_json = json.dumps(deliver_date)
        expression = f"""JSON.stringify((()=>{{
          const set=(el,value)=>{{const p=Object.getPrototypeOf(el);
            const d=Object.getOwnPropertyDescriptor(p,'value'); d?.set?.call(el,value);
            el.dispatchEvent(new Event('input',{{bubbles:true}})); el.dispatchEvent(new Event('change',{{bubbles:true}}));}};
          const content=document.querySelector('textarea[name="data[Offer][content]"]');
          const price=document.querySelector('input[name="data[Offer][price]"]');
          const date=document.querySelector('input[name="data[Offer][expire_date]"]');
          if(!content||!price||!date) return {{ok:false}};
          set(content,{proposal_json}); set(price,{price_json}); set(date,{date_json});
          return {{ok:true,url:location.href,proposal_text:content.value,price:price.value,deliver_date:date.value}};
        }})())"""
        async with await _cdp_connect(self.ws_url) as ws:
            call_id = 1
            await self._call(ws, "Page.enable", {}, call_id)
            state, _ = await self._eval_json(ws, expression, call_id + 1)
        if state.get("ok") is not True or not _is_expected_offer_form_url(request_id, state.get("url")):
            raise ParentContractError("application_form_fill_failed")

    def fill_form(self, request_id: str, proposal_text: str, price_jpy: int, deliver_date: str) -> None:
        asyncio.run(self._fill_async(request_id, proposal_text, price_jpy, deliver_date))

    def readback_form(self, request_id: str) -> dict[str, object]:
        state = asyncio.run(self._form_state_async(request_id, navigate=False))
        raw_price = str(state.get("price") or "")
        digits = re.sub(r"[^0-9]", "", raw_price)
        return {
            "proposal_text": state.get("content"),
            "price_jpy": int(digits) if digits else None,
            "deliver_date": state.get("deliver_date"),
        }

    # The terms-confirmation modal Coconala shows conditionally after the submit click
    # (measured 2026-08-10, gig-pass-1786284005-38359, 96000004-submit-attempt.png): title
    # 投稿前にご確認ください, a ToS bullet list, and a green 応募する button. Until it is
    # clicked the application never reaches the server, the exact-id readback correctly
    # reports absent, and the candidate ate an own-action wedge strike for a form that was
    # one click from done. Coconala also keeps a zero-size hidden copy of this modal in the
    # DOM. The title and button must both be visible, and button lookup must stop at the
    # visible modal root; walking farther reaches the underlying form's identical 応募する
    # and turns a submit into a backdrop click that merely closes the real modal.
    _TERMS_MODAL_JS = """JSON.stringify((()=>{
      const visible=e=>{if(!e||e.disabled)return false;
        const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0
          &&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;};
      const page={url:location.href,body:(document.body?.innerText||'').slice(-4000)};
      const title=[...document.querySelectorAll('*')].find(
        e=>visible(e)&&e.children.length===0&&(e.innerText||'').trim()==='投稿前にご確認ください');
      if(!title) return {modal:false,...page};
      const modal=title.closest('.js_components-modal,[role="dialog"]');
      const btn=modal?[...modal.querySelectorAll('button,a,[role="button"]')]
        .find(e=>visible(e)&&(e.innerText||'').trim()==='応募する')||null:null;
      if(!btn) return {modal:true,button:null,...page};
      btn.scrollIntoView({block:'center'});
      const r=btn.getBoundingClientRect();
      return {modal:true,button:{x:r.left+r.width/2,y:r.top+r.height/2},...page};
    })())"""

    async def _confirm_terms_modal(self, ws: Any, request_id: str, call_id: int) -> int:
        """Confirm the post-submit terms modal when it appears; do nothing when it doesn't.

        Polls briefly instead of sleeping a fixed amount: the modal may render a beat after
        the click, may never come at all (96000002 and 96000005 landed modal-free in the same
        passes that trapped 96000004), or the page may already have navigated to the applied
        list -- the landed check exits the poll without spending the budget. A modal that
        appeared but whose button never became clickable is its own explicit failure,
        submit_confirm_modal_failed, never a bare navigate timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        modal_seen = False
        while loop.time() < deadline:
            state, call_id = await self._eval_json(ws, self._TERMS_MODAL_JS, call_id)
            if state.get("modal") is True:
                modal_seen = True
                button = state.get("button")
                if isinstance(button, dict):
                    try:
                        x = float(button["x"])
                        y = float(button["y"])
                    except (KeyError, TypeError, ValueError) as error:
                        raise ParentContractError("submit_confirm_modal_failed") from error
                    await self._call(ws, "Page.bringToFront", {}, call_id)
                    call_id += 1
                    for params in _mouse_click_event_params(x, y):
                        await self._call(
                            ws,
                            "Input.dispatchMouseEvent",
                            params,
                            call_id,
                        )
                        call_id += 1
                    screenshot, call_id = await self._screenshot(ws, call_id)
                    _atomic_bytes(
                        self.evidence_dir / f"gig-{self.pass_id}-B2-{request_id}-modal-confirmed.png",
                        screenshot,
                    )
                    return call_id
            elif submit_landed(state.get("url"), state.get("body")):
                return call_id
            await asyncio.sleep(0.25)
        if modal_seen:
            raise ParentContractError("submit_confirm_modal_failed")
        return call_id

    async def _click_button_async(
        self, request_id: str, label: str, settle_predicate=None, confirm_modal: bool = False
    ) -> tuple[dict[str, object], bytes]:
        expected_url = f"https://coconala.com/offers/add/{request_id}"
        async with await _cdp_connect(self.ws_url) as ws:
            call_id = 1
            await self._call(ws, "Page.enable", {}, call_id)
            call_id += 1
            state: dict[str, object] = {}
            for _ in range(24):
                state, call_id = await self._eval_json(
                    ws,
                    """JSON.stringify((()=>{
                  const usable=e=>{if(!e||e.disabled||e.offsetParent===null)return false;
                    const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
                  const controls=[...document.querySelectorAll(
                    'button,a,[role="button"],input[type="submit"],input[type="button"]'
                  )];
                  const describe=e=>{const r=e.getBoundingClientRect(); return {
                    tag:e.tagName.toLowerCase(),label:(e.innerText||e.value||e.getAttribute('aria-label')||'').trim(),
                    role:e.getAttribute('role')||null,disabled:!!e.disabled,
                    rect:{left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}};};
                  const matches=controls.filter(e=>{
                    const text=(e.innerText||e.value||e.getAttribute('aria-label')||'').trim();
                    return usable(e)&&text===""" + json.dumps(label) + """;
                  });
                  if(!matches.length)return {url:location.href,button:null,controls:controls.map(describe)};
                  const control=matches.find(e=>{
                    const r=e.getBoundingClientRect();
                    return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;
                  })||matches[0];
                  control.scrollIntoView({block:'center'}); const r=control.getBoundingClientRect();
                  if(!(r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth))
                    return {url:location.href,button:null,controls:controls.map(describe)};
                  return {url:location.href,button:{x:r.left+r.width/2,y:r.top+r.height/2,
                    tag:control.tagName.toLowerCase(),label:(control.innerText||control.value||control.getAttribute('aria-label')||'').trim(),
                    role:control.getAttribute('role')||null,href:control.getAttribute('href')||null}};
                })())""",
                    call_id,
                )
                if _is_expected_offer_form_url(request_id, state.get("url")) and isinstance(state.get("button"), dict):
                    break
                await asyncio.sleep(0.25)
            if not _is_expected_offer_form_url(request_id, state.get("url")) or not isinstance(state.get("button"), dict):
                screenshot, _ = await self._screenshot(ws, call_id)
                prefix = self.evidence_dir / f"gig-{self.pass_id}-B2-{request_id}-{label}-control-missing"
                _atomic_json(prefix.with_suffix(".json"), {
                    "request_id": request_id,
                    "expected_label": label,
                    "dom": state,
                })
                _atomic_bytes(prefix.with_suffix(".png"), screenshot)
                raise ParentContractError(f"application_{label}_button_missing")
            button = state["button"]
            try:
                x = float(button["x"])
                y = float(button["y"])
            except (KeyError, TypeError, ValueError) as error:
                raise ParentContractError("application_button_coordinates_invalid") from error
            await self._call(ws, "Page.bringToFront", {}, call_id)
            call_id += 1
            for params in _mouse_click_event_params(x, y):
                await self._call(
                    ws,
                    "Input.dispatchMouseEvent",
                    params,
                    call_id,
                )
                call_id += 1
            if confirm_modal:
                call_id = await self._confirm_terms_modal(ws, request_id, call_id)
            read_expression = (
                "JSON.stringify({url:location.href,"
                "body:(document.body?.innerText||'').slice(-4000)})"
            )
            if settle_predicate is None:
                # No landing to wait for (the confirm click only opens a modal); keep the
                # original short settle so that path's timing is unchanged.
                await asyncio.sleep(0.75)
                after, call_id = await self._eval_json(ws, read_expression, call_id)
            else:
                # Poll until the click has visibly landed. The fixed 0.75s guess is what
                # scored working submits as failures for seventy hours.
                state = {"call_id": call_id}

                async def read_page():
                    observed, next_id = await self._eval_json(ws, read_expression, state["call_id"])
                    state["call_id"] = next_id
                    return observed

                after = await settle_after_click(read_page, settle_predicate)
                call_id = state["call_id"]
            screenshot, _ = await self._screenshot(ws, call_id)
        return after, screenshot

    def click_confirm(self, request_id: str) -> None:
        self._click_button_async_result = asyncio.run(self._click_button_async(request_id, "確認する"))

    def click_submit(self, request_id: str) -> None:
        # A submit is irreversible. If the landing is not observed, the outcome is unknown;
        # leave PREPARED for authoritative readback instead of clicking the same intent again.
        after, last_screenshot = asyncio.run(
            self._click_button_async(
                request_id, "応募する", settle_predicate=submit_landed, confirm_modal=True
            )
        )
        if submit_landed(after.get("url"), after.get("body")):
            path = self.evidence_dir / f"gig-{self.pass_id}-B2-{request_id}-submitted.png"
            _atomic_bytes(path, last_screenshot)
            self._submitted_paths[request_id] = path
            return
        path = self.evidence_dir / f"gig-{self.pass_id}-B2-{request_id}-submit-attempt.png"
        _atomic_bytes(path, last_screenshot)

    async def _official_readback_async(
        self, expected_ids: set[str], max_pages: int | None = None
    ) -> tuple[dict[str, object], bytes]:
        # Only a search for specific ids paginates past page 1. official_ids_for_snapshot
        # calls this with an empty expected_ids to build the pre-snapshot exclusion set on
        # every commit -- paginating that unconditionally would turn one cheap page-1 read
        # into (max pages)x the live navigations on every single pass for a set the ledger
        # already backstops (see ledger_applied_ids). A real target id search, in contrast,
        # is exactly the case that was stuck on page 1 forever (§FG'). The override exists
        # for the quarantine-release batch, whose absence proof must cover the WHOLE
        # history (~450 applications > 10 pages); the live per-candidate default stays put.
        if max_pages is None:
            max_pages = _APPLIED_OFFERS_MAX_PAGES if expected_ids else 1
        async with await _cdp_connect(self.ws_url) as ws:
            call_id = 1
            await self._call(ws, "Page.enable", {}, call_id)
            # This first navigate and the first list eval keep plain error semantics on
            # purpose: they run immediately after the candidate's own submit, so a hang HERE
            # is genuine evidence of the submit page having killed the renderer -- exactly
            # what the wedge strike exists to count. Everything after the first successful
            # page eval is neighbour-scan and converts to ReadbackScanTimeout.
            call_id = await self._navigate_retry_once(ws, _APPLIED_OFFERS_URL, call_id + 1)
            observed: set[str] = set()
            first_page: dict[str, object] | None = None
            first_screenshot = b""
            pages_walked = 0
            cards_seen = 0
            has_next_page = False
            truncated = False
            while True:
                try:
                    page, call_id = await self._eval_json(
                        ws,
                        # not_found tests document.title only, not body text: the
                        # applied-offers body is full of client-submitted job descriptions,
                        # and on 2026-08-08 one literally read "...404エラーを解消してください"
                        # -- a real applicant asking for help with THEIR OWN site's 404
                        # page. Concatenating body into this test scored a perfectly valid,
                        # fully-loaded page (20 offers, correct title, correct URL) as
                        # not_found and raised on every single pass. document.title is
                        # site-controlled, never client content; every sibling readback in
                        # this codebase (coconala_applied_readback.py,
                        # coconala_queue_snapshot.py, retainer_thread.py) tests title-only.
                        """JSON.stringify((()=>{
                          const anchors=[...document.querySelectorAll('a[href]')];
                          const next=anchors.find(a=>a.rel==='next'||/^(次へ|次のページ|次)$/u.test((a.innerText||'').trim()));
                          return {url:location.href,title:document.title,
                            offer_urls:[...document.querySelectorAll('a[href*="/mypage/offers/"]')]
                              .map(a=>a.href).filter((value,index,all)=>value&&all.indexOf(value)===index),
                            next_href:next?.href||null,
                            body:(document.body?.innerText||'').slice(0,12000),
                            access_denied:document.title==='403 Forbidden'||document.title==='Access Denied',
                            not_found:/404|ページが見つかりません|お探しのページ/.test(document.title)};
                        })())""",
                        call_id,
                    )
                except ParentContractError as error:
                    if pages_walked == 0 or "timeout" not in str(error):
                        raise
                    raise ReadbackScanTimeout(str(error)) from error
                parsed = urlsplit(str(page.get("url") or ""))
                if (
                    parsed.hostname not in {"coconala.com", "www.coconala.com"}
                    or parsed.path.rstrip("/") != _APPLIED_OFFERS_PATH
                    or page.get("access_denied") is True
                    or page.get("not_found") is True
                ):
                    # Evidence before the raise: without this, every one of the 20+ failed
                    # passes on 2026-08-08 raised with zero record of what the browser
                    # actually saw, and the false positive above went undiagnosed for hours.
                    # The screenshot itself is best-effort: in a dead-renderer scenario --
                    # exactly where evidence matters most -- captureScreenshot can raise
                    # (cdp_screenshot_missing / cdp_Page.captureScreenshot_timeout), and if
                    # that escaped it would (a) skip the JSON entirely and (b) replace
                    # official_readback_route_invalid with a cdp_*_timeout error, which
                    # cdp_wedged_row() then miscounts as a wedge against an innocent listing.
                    try:
                        unexpected_screenshot, call_id = await self._screenshot(ws, call_id)
                    except Exception:
                        unexpected_screenshot = b""
                    evidence_path = (
                        self.evidence_dir
                        / f"parent-B2-applied-readback-unexpected-route-{self.pass_id}-{int(time.time())}.json"
                    )
                    unexpected_screenshot_path = evidence_path.with_suffix(".png")
                    if unexpected_screenshot:
                        _atomic_bytes(unexpected_screenshot_path, unexpected_screenshot)
                    _atomic_json(
                        evidence_path,
                        {
                            "source": "code_owned_cdp_readback",
                            "landed_url": page.get("url"),
                            "expected_url": _APPLIED_OFFERS_URL,
                            "title": page.get("title"),
                            "access_denied": page.get("access_denied"),
                            "not_found": page.get("not_found"),
                            "body_sample": str(page.get("body") or ""),
                            "screenshot_path": (
                                str(unexpected_screenshot_path.resolve())
                                if unexpected_screenshot
                                else None
                            ),
                        },
                    )
                    if page.get("access_denied") is True:
                        raise ParentContractError("official_readback_access_denied")
                    raise ParentContractError("official_readback_route_invalid")
                if first_page is None:
                    first_screenshot, call_id = await self._screenshot(ws, call_id)
                    first_page = page
                pages_walked += 1
                urls = page.get("offer_urls")
                if not isinstance(urls, list):
                    raise ParentContractError("official_readback_offer_urls_missing")
                cards_seen += len(urls)
                for offer_url in urls:
                    if expected_ids and expected_ids.issubset(observed):
                        break
                    if not isinstance(offer_url, str) or re.fullmatch(
                        r"https://(?:www\.)?coconala\.com/mypage/offers/[0-9]+", offer_url
                    ) is None:
                        continue
                    try:
                        call_id = await self._navigate_retry_once(ws, offer_url, call_id)
                        detail, call_id = await self._eval_json(
                            ws,
                            """JSON.stringify({hidden:document.querySelector('#OfferRequestId')?.value||null,
                              hrefs:[...document.querySelectorAll('a[href*="/requests/"]')].map(a=>a.href),
                              body:(document.body?.innerText||'').slice(0,5000)})""",
                            call_id,
                        )
                    except ParentContractError as error:
                        if "timeout" not in str(error):
                            raise
                        raise ReadbackScanTimeout(str(error)) from error
                    request_id = str(detail.get("hidden") or "").strip()
                    if not request_id.isdigit():
                        for href in detail.get("hrefs") or []:
                            matched = _REQUEST_URL.fullmatch(urlsplit(str(href)).path.rstrip("/"))
                            if matched:
                                request_id = matched.group(1)
                                break
                    if request_id.isdigit():
                        observed.add(request_id)
                if expected_ids and expected_ids.issubset(observed):
                    break
                next_url = _strict_next_page(
                    str(page.get("url") or ""), page.get("next_href"), path=_APPLIED_OFFERS_PATH
                )
                has_next_page = next_url is not None
                if next_url is None:
                    break
                if pages_walked >= max_pages:
                    # The truncation signal is the next LINK existing, never a navigate the
                    # budget would only throw away.
                    truncated = True
                    break
                try:
                    call_id = await self._navigate_retry_once(ws, next_url, call_id)
                except ParentContractError as error:
                    if "timeout" not in str(error):
                        raise
                    raise ReadbackScanTimeout(str(error)) from error
        assert first_page is not None
        if truncated and expected_ids and not expected_ids.issubset(observed):
            # An exhausted page budget with a next link remaining proves NOTHING about
            # absence. Plain False here is the duplicate-application path: the release
            # tool would clear an already-applied id, and the PREPARED reconcile would
            # retire the marker and resubmit. Both callers treat this as inconclusive.
            raise ReadbackScanTimeout(
                f"official_readback_truncated_after_{pages_walked}_pages_next_page_remains"
            )
        payload = {
            "source": "code_owned_cdp_readback",
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "pass_id": self.pass_id,
            "url": _APPLIED_OFFERS_URL,
            "urls": [_APPLIED_OFFERS_URL],
            "title": first_page.get("title"),
            "observed": True,
            "not_found": False,
            "request_ids": sorted(observed, key=int),
            "expected_ids": sorted(expected_ids, key=int),
            "expected_request_ids": sorted(expected_ids, key=int),
            "applied_page_absent_request_ids": sorted(expected_ids - observed, key=int),
            "pages_walked": pages_walked,
            "cards_seen": cards_seen,
            "has_next_page": has_next_page,
            "missing_count": len(expected_ids - observed),
            "unresolved_count": 0,
            "body_sample": first_page.get("body") or "",
        }
        return payload, first_screenshot

    def _official_readback(
        self, expected_ids: set[str], path: Path, max_pages: int | None = None
    ) -> set[str]:
        payload, screenshot = asyncio.run(
            self._official_readback_async(expected_ids, max_pages=max_pages)
        )
        screenshot_path = path.with_suffix(".png")
        _atomic_bytes(screenshot_path, screenshot)
        payload["screenshot_path"] = str(screenshot_path.resolve())
        _atomic_json(path, payload)
        return {str(value) for value in payload["request_ids"]}

    def authoritative_exact_id_readback(self, request_id: str) -> bool:
        path = self.evidence_dir / f"parent-B2-applied-readback-{request_id}.json"
        observed = self._official_readback({request_id}, path)
        if request_id in observed:
            self._readback_paths[request_id] = path
            return True
        return False

    def official_ids_for_snapshot(self) -> list[str]:
        path = self.evidence_dir / "parent-B2-applied-history-before-snapshot.json"
        return sorted(self._official_readback(set(), path), key=int)

    def applied_ids_for_exclusion(self) -> list[str]:
        """Everything not worth inspecting again: what the site shows plus what we sent.

        The site page is authoritative for what Coconala currently displays and the ledger
        for what this loop has ever submitted; neither alone is the full set, and the site
        read is one page deep. A failed site read degrades to the ledger rather than
        inspecting hundreds of duplicates.
        """
        identifiers = ledger_applied_ids(self.ledger_path)
        try:
            identifiers.update(str(value) for value in self.official_ids_for_snapshot())
        except Exception:
            pass
        return sorted(identifiers, key=lambda value: (not value.isdigit(), value))

    def finalize_exact_readback(self, request_ids: set[str]) -> None:
        if not request_ids:
            return
        path = self.evidence_dir / "code-applied-readback.json"
        prior_verified: set[str] = set()
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("pass_id") == self.pass_id:
                previous_expected = previous.get("expected_ids")
                previous_observed = previous.get("request_ids")
                if not isinstance(previous_expected, list) or not isinstance(previous_observed, list):
                    previous_expected, previous_observed = [], []
                prior_expected = {
                    str(value) for value in previous_expected
                    if str(value).isdigit()
                }
                prior_observed = {
                    str(value) for value in previous_observed
                    if str(value).isdigit()
                }
                prior_verified = prior_expected & prior_observed
        except (OSError, json.JSONDecodeError):
            pass
        with self.target_lock():
            try:
                observed = self._official_readback(request_ids, path)
            except websockets.exceptions.ConnectionClosedError:
                if not self.recover_wedged_target():
                    raise
                observed = self._official_readback(request_ids, path)
        expected = {str(value) for value in request_ids}
        observed_ids = {str(value) for value in observed}
        current_verified = expected & observed_ids
        combined_expected = prior_verified | expected
        combined_observed = prior_verified | current_verified
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ParentContractError("final_exact_readback_artifact_invalid") from error
        if not isinstance(payload, dict):
            raise ParentContractError("final_exact_readback_artifact_invalid")
        payload["pass_id"] = self.pass_id
        payload["expected_ids"] = sorted(combined_expected, key=int)
        payload["request_ids"] = sorted(combined_observed, key=int)
        _atomic_json(path, payload)
        missing = combined_expected - combined_observed
        if missing:
            raise ParentContractError("final_exact_readback_missing:" + ",".join(sorted(missing, key=int)))

    def canonical_ledger_append(self, row: dict[str, object]) -> None:
        request_id = str(row["request_id"])
        detail = self._fresh_details.get(request_id)
        if detail is None:
            raise ParentContractError("ledger_detail_not_reextracted")
        market = parse_market(
            str(detail["visible_text"]), now=time.time(), bid_jpy=row["price_jpy"]
        )
        submitted = self._submitted_paths.get(request_id)
        readback = self._readback_paths.get(request_id)
        ledger_row: dict[str, object] = {
            "ts": int(time.time()),
            "pass_id": self.pass_id,
            "requestId": request_id,
            "bucket": "single",
            "status": "applied",
            "category": row["category"],
            "category_source": "dom",
            "title": row["title"],
            "price_jpy": row["price_jpy"],
            "deliver_date": row["deliver_date"],
            "url": row["url"],
            "evidence": submitted.name if submitted else None,
            "recorded_by": "application_parent",
            "submit_verified": True,
            "applied_page_verified": True,
            "applied_page_evidence": str(readback.resolve()) if readback else None,
        }
        for field in (
            "compensation_type", "weekly_days", "weekly_hours_min", "weekly_hours_max",
        ):
            ledger_row[field] = row.get(field)
        for field in MARKET_FIELDS:
            ledger_row[field] = market.get(field)
        ledger_row["market_source"] = "snapshot_detail"
        missing = market.get("missing")
        ledger_row["market_missing"] = dict(missing) if isinstance(missing, dict) else {}
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.ledger_path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
        # "a" is write-only, so the duplicate check below raised UnsupportedOperation after
        # the submit had already happened on Coconala: the application existed on the site and
        # no row existed locally. "a+" keeps the atomic append under the lock and can be read.
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            already_recorded = False
            for raw in handle:
                try:
                    existing = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if str(existing.get("requestId") or existing.get("request_id") or "") == request_id:
                    already_recorded = True
                    break
            if not already_recorded:
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(ledger_row, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        publisher = self.instant_work_event_publisher
        if callable(publisher):
            try:
                publisher(self.ledger_path, self.pass_id)
            except Exception:
                # The marketplace fact is already durable; notification retries on the
                # next wake's startup recovery.
                pass

    def crash_if_requested(self, checkpoint: str) -> None:
        return None


DEEP_SOURCE_ID = "single:new"
# Half the batch to the newest-first firehose. Measured 2026-08-07 over 36 passes:
# single:new returned 0 closed and 0 already-applied listings out of 37 observations,
# every other source returned 406 between them, and no listing arrives anywhere before
# it arrives there.
DEEP_SOURCE_SHARE = 0.5


def _source_capacity_plan(required: list[str], *, batch: int) -> dict[str, int]:
    """Decide how deep to read each source before a single page is loaded.

    Depth is the scarce resource: MAX_BATCH slots shared by every required source. The
    previous rule, max(1, remaining // remaining_sources) recomputed inside the loop,
    floors every source at one listing as soon as the program is larger than the batch
    -- 87 sources, 40 slots -- and then hands the whole unspent remainder to whichever
    source happens to sort last, because at the final source remaining_sources is 1.
    List position, not evidence, was doing the allocating.

    Measured 2026-08-07 across 36 passes of real evidence:
      single:keyword  sorts last, took 563 of 1785 observations (31.5%), 51.7% of them
                      already closed or already applied to, and has produced zero
                      applications in the loop's history.
      single:new      sorts first, was therefore capped at one listing per pass, took 37
                      observations, and produced 6 of the 26 confirmed applications.
    Page one of ?sort=new offers 40 listings in a single load, so reading it deep costs
    no extra page loads -- it is the same request with a larger slice taken.
    """
    plan = {source_id: 1 for source_id in required}
    if not plan:
        return plan
    deep_id = DEEP_SOURCE_ID if DEEP_SOURCE_ID in plan else str(required[0])
    # Fill the batch exactly when the program fits inside it; otherwise reserve a fixed
    # share, and let the caller's running clamp keep the total inside MAX_BATCH.
    plan[deep_id] = max(1, batch - len(plan) + 1, int(batch * DEEP_SOURCE_SHARE))
    return plan


HIGH_VALUE_BUDGET_JPY = 50_000
def _known_budget(detail: dict[str, object]) -> int | None:
    values = [detail.get("budget_max_jpy"), detail.get("budget_min_jpy")]
    known = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    return max(known) if known else None
def _queue_a(detail: dict[str, object]) -> bool:
    return any(
        isinstance(detail.get(field), int)
        and not isinstance(detail.get(field), bool)
        and detail[field] >= HIGH_VALUE_BUDGET_JPY
        for field in ("budget_max_jpy", "budget_min_jpy")
    )
def _candidate_rank_key(detail: dict[str, object]) -> tuple[int, int, float, float, int]:
    """Queue A first, then budget/rate/applicants/newest-id; never filters candidates."""
    budget = _known_budget(detail)
    rate = detail.get("client_order_rate")
    rate_value = float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else -1.0
    applicants = detail.get("applicants_count")
    applicants_value = float(applicants) if isinstance(applicants, int) and not isinstance(applicants, bool) else float("inf")
    request_id = str(detail.get("request_id") or "")
    request_number = int(request_id) if request_id.isdigit() else -1
    return (0 if _queue_a(detail) else 1, -(budget if budget is not None else -1), -rate_value, applicants_value, -request_number)
def _partition_required_sources(
    required: list[str], *, shard_count: int = DEFAULT_DISCOVERY_SHARDS
) -> list[list[str]]:
    """Partition source ids once, deterministically, without assigning a cursor twice."""
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or shard_count < 1:
        raise ParentContractError("discovery_shard_count_invalid")
    values = [str(value) for value in required]
    if not values or any(not value.strip() for value in values):
        raise ParentContractError("snapshot_required_sources_invalid")
    if len(set(values)) != len(values):
        raise ParentContractError("required_search_source_ids_duplicate")
    return [values[index::shard_count] for index in range(shard_count)]


def _detail_conflicts(existing: dict[str, object], candidate: dict[str, object]) -> bool:
    for field in ("content_sha256", "fresh_identity", "identity", "canonical_url"):
        if field in existing and field in candidate and existing[field] != candidate[field]:
            return True
    try:
        left = snapshot_contract._normalise_detail(existing)
        right = snapshot_contract._normalise_detail(candidate)
        return left["content_sha256"] != right["content_sha256"]
    except Exception as error:
        raise ParentContractError("shard_request_detail_invalid") from error


class CdpSnapshotCollector:
    """Collect a canonical batch from one leased target before model planning."""

    def __init__(
        self,
        effects: CdpParentEffects,
        *,
        pass_id: str,
        objective: dict[str, object],
        excluded_request_ids: set[str] | None = None,
        ineligible_cache: dict[str, object] | None = None,
        intent_store: fence.IntentStore | None = None,
        cursor_contract: dict[str, object] | None = None,
        discovery_effect_factory: Callable[[int], object] | None = None,
        discovery_shards: int = DEFAULT_DISCOVERY_SHARDS,
        discovery_timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    ) -> None:
        self.effects = effects
        self.pass_id = pass_id
        self.objective = objective
        # Requests the commit boundary will refuse on sight. Collecting them spends a
        # batch slot and a planner judgment on an outcome that cannot happen.
        self.excluded_request_ids = {str(value) for value in (excluded_request_ids or set())}
        self.ineligible_cache = ineligible_cache or {}
        self.intent_store = intent_store
        self.cursor_contract = cursor_contract
        if isinstance(discovery_shards, bool) or not isinstance(discovery_shards, int) or discovery_shards < 1:
            raise ParentContractError("discovery_shard_count_invalid")
        if (
            isinstance(discovery_timeout_seconds, bool)
            or not isinstance(discovery_timeout_seconds, (int, float))
            or discovery_timeout_seconds <= 0
        ):
            raise ParentContractError("discovery_timeout_seconds_invalid")
        self.discovery_effect_factory = discovery_effect_factory
        self.discovery_shards = discovery_shards
        self.discovery_timeout_seconds = float(discovery_timeout_seconds)
        self.discovery_failures: dict[int, str] = {}
        self.source_artifacts: dict[str, dict[str, str]] = {}
        self.raw_request_ids: set[str] = set()
        self.observed_already_applied_ids: set[str] = set()
        self.observed_quarantined_ids: set[str] = set()
        self.filtered_observation_results: dict[str, dict[str, object]] = {}
        self.lifecycle_observations: dict[str, dict[str, object]] = {}

    def observation_payload(self) -> dict[str, object]:
        key = lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value))
        return {
            "version": 1,
            "raw_request_ids": sorted(self.raw_request_ids, key=key),
            "already_applied_ids": sorted(self.observed_already_applied_ids, key=key),
            "quarantined_ids": sorted(self.observed_quarantined_ids, key=key),
            "filtered_results": [
                self.filtered_observation_results[request_id]
                for request_id in sorted(self.filtered_observation_results, key=key)
            ],
            "lifecycle_results": [
                self.lifecycle_observations[request_id]
                for request_id in sorted(self.lifecycle_observations, key=key)
            ],
        }

    @staticmethod
    def _source_url(source_id: str) -> str:
        if source_id == "single:new":
            return "https://coconala.com/requests?sort=new&recruiting=true"
        if source_id == "single:keyword":
            return "https://coconala.com/requests?keyword=AI&recruiting=true"
        prefix = "single:category:"
        if source_id.startswith(prefix) and source_id.removeprefix(prefix).strip():
            return "https://coconala.com/requests?" + urlencode({
                "keyword": source_id.removeprefix(prefix).strip(), "recruiting": "true",
            })
        raise ParentContractError(f"source_id_not_collectable:{source_id}")

    def collect(
        self,
        lease_fence: dict[str, object],
        *,
        cursor_contract: dict[str, object] | None = None,
        _raw: bool = False,
    ) -> dict[str, object]:
        if self.discovery_effect_factory is not None and not _raw:
            return self._collect_sharded(lease_fence, cursor_contract=cursor_contract)
        required = self.objective.get("required_search_source_ids")
        if not isinstance(required, list) or not required:
            raise ParentContractError("snapshot_required_sources_invalid")
        if cursor_contract is not None:
            self.cursor_contract = cursor_contract
        cursor_source = cursor_url = None
        prior_inspected_ids: set[str] = set()
        if self.cursor_contract is not None:
            if not isinstance(self.cursor_contract, dict):
                raise ParentContractError("cursor_contract_invalid")
            cursor_source = str(self.cursor_contract.get("source_id") or "")
            if cursor_source not in {str(value) for value in required}:
                raise ParentContractError("cursor_source_not_required")
            canonical = self._source_url(cursor_source)
            cursor_url = str(self.cursor_contract.get("next_url") or "")
            if cursor_url != canonical and _strict_next_page(canonical, cursor_url, path=urlsplit(canonical).path) is None:
                raise ParentContractError("cursor_next_url_invalid")
            previous = str(self.cursor_contract.get("previous_url") or "")
            if previous and ((previous != canonical and _strict_next_page(canonical, previous, path=urlsplit(canonical).path) is None) or _page_index(previous) >= _page_index(cursor_url)):
                raise ParentContractError("cursor_previous_url_invalid")
            raw_prior_ids = self.cursor_contract.get("prior_inspected_request_ids") or []
            if not isinstance(raw_prior_ids, list):
                raise ParentContractError("cursor_prior_inspected_request_ids_invalid")
            try:
                prior_inspected_ids = {
                    snapshot_contract.canonical_request_id(value)
                    for value in raw_prior_ids
                }
            except snapshot_contract.SnapshotContractError as error:
                raise ParentContractError(
                    "cursor_prior_inspected_request_ids_invalid"
                ) from error
        sources: list[dict[str, object]] = []
        details: list[dict[str, object]] = []
        selected_ids: set[str] = set()
        with self.effects.target_lock():
            # Prefer the union of site page and durable ledger; the site page alone is one
            # page deep and let ~25 already-applied listings per pass eat the batch.
            override = getattr(self, "_already_applied_override", None)
            if override is not None:
                already_applied = override
            else:
                widest = getattr(self.effects, "applied_ids_for_exclusion", None)
                already_applied = widest() if callable(widest) else self.effects.official_ids_for_snapshot()
            # already_applied is what the envelope reports; skipped is what the batch
            # actually declines to spend a slot on. Quarantined requests are not applied
            # to and must not be recorded as if they were.
            already_applied_set = set(already_applied)
            skipped = (
                already_applied_set
                | self.excluded_request_ids
                | prior_inspected_ids
            )
            plan = _source_capacity_plan(
                [str(value) for value in required], batch=snapshot_contract.MAX_BATCH
            )
            for raw_source_id in required:
                source_id = str(raw_source_id)
                # Never exceed the contract's batch, but always observe every required
                # source: a source seen with zero remaining room is observed and
                # contributes nothing, which is a full batch, not an unobserved source.
                source_capacity = min(
                    plan[source_id], max(0, snapshot_contract.MAX_BATCH - len(details))
                )
                page_url: str | None = cursor_url if source_id == cursor_source else self._source_url(source_id)
                source_ids: list[str] = []
                final_source: dict[str, object] | None = None
                final_artifacts: dict[str, str] | None = None
                visited_urls: set[str] = set()
                observed_once = False
                while page_url is not None and (
                    not observed_once or len(source_ids) < source_capacity
                ) and (self.cursor_contract is None or not observed_once):
                    if page_url in visited_urls:
                        raise ParentContractError(f"source_pagination_cycle:{source_id}")
                    visited_urls.add(page_url)
                    remaining = max(0, source_capacity - len(source_ids))
                    source, request_ids, artifacts, next_url = self.effects.collect_source(
                        source_id, page_url, remaining
                    )
                    observed_once = True
                    self.raw_request_ids.update(str(request_id) for request_id in request_ids)
                    self.observed_already_applied_ids.update(
                        str(request_id) for request_id in request_ids
                        if str(request_id) in already_applied_set
                    )
                    self.observed_quarantined_ids.update(
                        str(request_id) for request_id in request_ids
                        if str(request_id) in self.excluded_request_ids
                    )
                    unique_ids = [
                        request_id
                        for request_id in request_ids
                        if request_id not in selected_ids and request_id not in skipped
                    ]
                    for request_id in unique_ids:
                        selected_ids.add(request_id)
                        detail = self.effects.reextract_detail(request_id)
                        lifecycle_status, lifecycle_reasons = _lifecycle_disposition(detail)
                        if lifecycle_status not in {"open", "legacy"} and self.intent_store is not None:
                            durable_intent = self.intent_store.read(request_id)
                            if durable_intent is not None and (
                                durable_intent["state"] == fence.CONFIRMED
                                or (
                                    durable_intent["state"] == fence.PREPARED
                                    and not fence.is_pre_effect(durable_intent)
                                )
                            ):
                                # Account-state pages can hide the application form after
                                # submission.  A durable intent therefore takes precedence
                                # over describing that same page as a closed request.
                                self.observed_already_applied_ids.add(request_id)
                                continue
                        if lifecycle_status != "legacy":
                            observation = _lifecycle_observation(detail)
                            if observation is None:
                                lifecycle_status, lifecycle_reasons = "unknown", [
                                    "lifecycle_observation_invalid"
                                ]
                                lifecycle_row = {"request_id": request_id, "title": str(detail.get("title") or "")}
                            else:
                                lifecycle_row = {
                                    "request_id": request_id,
                                    "title": str(detail.get("title") or ""), "canonical_url": str(detail.get("canonical_url") or ""), "observed_at": detail.get("observed_at"),
                                    **{field: observation[field] for field in _LIFECYCLE_FIELDS},
                                    "lifecycle_sha256": _lifecycle_digest(
                                        request_id, detail.get("canonical_url"),
                                        **{field: observation[field] for field in _LIFECYCLE_FIELDS}
                                    ),
                                }
                            self.lifecycle_observations[request_id] = lifecycle_row
                            if lifecycle_status != "open":
                                self.lifecycle_observations[request_id]["status"] = (
                                    "officially_unavailable" if lifecycle_status == "official_unavailable" else lifecycle_status
                                )
                                self.lifecycle_observations[request_id]["reason_codes"] = lifecycle_reasons
                                continue
                        cached = self.ineligible_cache.get(request_id)
                        if isinstance(cached, dict):
                            current_hash = detail.get("content_sha256")
                            if not isinstance(current_hash, str):
                                try:
                                    current_hash = snapshot_contract._normalise_detail(detail)[
                                        "content_sha256"
                                    ]
                                except Exception:
                                    current_hash = None
                            if current_hash == cached.get("content_sha256"):
                                cached_result: dict[str, object] = {
                                    "request_id": request_id,
                                    "title": str(detail.get("title") or ""),
                                    "status": "cached_ineligible",
                                    "reason_codes": list(cached.get("reason_codes") or []),
                                }
                                if cached.get("business_class") == HARD_PROHIBITED:
                                    cached_result["business_class"] = HARD_PROHIBITED
                                self.filtered_observation_results[request_id] = cached_result
                                continue
                        # T3 (2026-08-09): whether a listing is still accepting
                        # applications is only knowable after this reextract, so the
                        # page load already happened -- but the planner call has not,
                        # and the planner is the scarcer resource (§FJ': ~45% of
                        # inspected candidates were closed/already-applied corpses
                        # eating a judgment slot each). Do not count a closed listing
                        # toward source_capacity, so the while loop below reads
                        # deeper into this source instead of stopping early on a
                        # stale page, and do not hand it to the planner at all.
                        if detail.get("accepting_applications") is False:
                            self.filtered_observation_results[request_id] = {
                                "request_id": request_id,
                                "title": str(detail.get("title") or ""),
                                "status": "closed",
                                "reason_codes": ["募集終了"],
                            }
                            continue
                        source_ids.append(request_id)
                        details.append(detail)
                    final_source = source
                    final_artifacts = artifacts
                    page_url = next_url
                if final_source is None or final_artifacts is None:
                    raise ParentContractError(f"source_not_observed:{source_id}")
                final_source["card_request_ids"] = source_ids
                final_has_next = final_source.get("has_next") is True or page_url is not None
                final_source["has_next"] = final_has_next
                final_source["exhausted"] = not final_has_next
                sources.append(final_source)
                self.source_artifacts[source_id] = final_artifacts
        # Preserve every candidate and spend the application cap in deterministic
        # high-value-first order. The planner receives this order, but the parent also
        # restores it at commit time because model output order is not authoritative.
        details.sort(key=_candidate_rank_key)
        collector = {
            "pass_id": self.pass_id,
            "lease_fence": lease_fence,
            "observed_at": _utc_now(),
            "objective": self.objective,
            "search_sources": sources,
            "request_details": details,
            # Exclusion is everything we have ever touched; this field carries only what
            # the snapshot contract accepts (decimal 募集 ids), so direct-message threads
            # suppress re-inspection without violating the envelope.
            "already_applied_ids": snapshot_applied_ids(already_applied),
        }
        return collector if _raw else snapshot_contract.build_envelope(collector)

    def _collect_sharded(
        self, lease_fence: dict[str, object], *, cursor_contract: dict[str, object] | None
    ) -> dict[str, object]:
        self.discovery_failures = {}
        required = self.objective.get("required_search_source_ids")
        if not isinstance(required, list) or not required:
            raise ParentContractError("snapshot_required_sources_invalid")
        required = [str(value) for value in required]
        effective_cursor = self.cursor_contract if cursor_contract is None else cursor_contract
        cursor_source = None
        if effective_cursor is not None:
            if not isinstance(effective_cursor, dict):
                raise ParentContractError("cursor_contract_invalid")
            cursor_source = str(effective_cursor.get("source_id") or "")
            if cursor_source not in set(required):
                raise ParentContractError("cursor_source_not_required")
        collection_required = [cursor_source] if cursor_source is not None else required
        with self.effects.target_lock():
            widest = getattr(self.effects, "applied_ids_for_exclusion", None)
            already_applied = widest() if callable(widest) else self.effects.official_ids_for_snapshot()
        shards = _partition_required_sources(
            collection_required,
            shard_count=min(self.discovery_shards, len(collection_required)),
        )

        def run(index: int):
            raw_context = self.discovery_effect_factory(index)
            context = raw_context if hasattr(raw_context, "__enter__") else contextlib.nullcontext(raw_context)
            with context as shard_effects:
                if shard_effects is self.effects:
                    raise ParentContractError("discovery_effect_shared_with_commit")
                if not shards[index]:
                    return shard_effects, {
                        "search_sources": [], "request_details": []
                    }, {}, {
                        "raw_request_ids": [], "already_applied_ids": [],
                        "quarantined_ids": [], "filtered_results": [], "lifecycle_results": [],
                    }
                shard = copy.copy(self)
                shard.effects = shard_effects
                shard.discovery_effect_factory = None
                shard.objective = {**self.objective, "required_search_source_ids": shards[index]}
                shard.cursor_contract = effective_cursor if cursor_source in shards[index] else None
                shard.source_artifacts = {}
                shard.raw_request_ids = set()
                shard.observed_already_applied_ids = set()
                shard.observed_quarantined_ids = set()
                shard.filtered_observation_results = {}
                shard.lifecycle_observations = {}
                shard._already_applied_override = already_applied
                raw = shard.collect(lease_fence, _raw=True)
                try:
                    snapshot_contract.build_envelope(copy.deepcopy(raw))
                except Exception as error:
                    raise ParentContractError("discovery_shard_snapshot_invalid") from error
                return shard_effects, raw, shard.source_artifacts, shard.observation_payload()

        pool = ThreadPoolExecutor(max_workers=len(shards), thread_name_prefix="gig-discovery")
        futures = {pool.submit(run, index): index for index in range(len(shards))}
        completed: list[
            tuple[int, object, dict[str, object], dict[str, dict[str, str]], dict[str, object]]
        ] = []
        failures: dict[int, str] = {}
        pending = set(futures)
        deadline = time.monotonic() + self.discovery_timeout_seconds
        try:
            while pending:
                remaining = max(0.0, deadline - time.monotonic())
                done, pending = wait(pending, timeout=remaining)
                if not done:
                    for future in pending:
                        index = futures[future]
                        failures[index] = "TimeoutError:discovery_shard_timeout"
                        future.cancel()
                    break
                for future in done:
                    index = futures[future]
                    try:
                        effect, raw, artifacts, observations = future.result()
                    except TimeoutError as error:
                        failures[index] = f"TimeoutError:{error}".rstrip(":")
                    except BaseException:
                        for future in pending:
                            future.cancel()
                        raise
                    else:
                        completed.append((index, effect, raw, artifacts, observations))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        self.discovery_failures = dict(sorted(failures.items()))
        results = sorted(completed, key=lambda item: item[0])
        if len({id(item[1]) for item in results}) != len(results):
            raise ParentContractError("discovery_effect_instance_shared")

        sources_by_id: dict[str, dict[str, object]] = {}
        details_by_id: dict[str, dict[str, object]] = {}
        artifacts_by_id: dict[str, dict[str, str]] = {}
        self.raw_request_ids = set()
        self.observed_already_applied_ids = set()
        self.observed_quarantined_ids = set()
        self.filtered_observation_results = {}
        self.lifecycle_observations = {}
        blocked_lifecycle_ids: set[str] = set()
        for _, _, raw, artifacts, observations in results:
            self.raw_request_ids.update(
                str(value) for value in observations.get("raw_request_ids", [])
            )
            self.observed_already_applied_ids.update(
                str(value) for value in observations.get("already_applied_ids", [])
            )
            self.observed_quarantined_ids.update(
                str(value) for value in observations.get("quarantined_ids", [])
            )
            for row in observations.get("filtered_results", []):
                if isinstance(row, dict) and str(row.get("request_id") or ""):
                    self.filtered_observation_results[str(row["request_id"])] = row
            for row in observations.get("lifecycle_results", []):
                if isinstance(row, dict) and str(row.get("request_id") or ""):
                    request_id = str(row["request_id"])
                    prior = self.lifecycle_observations.get(request_id)
                    non_open = row.get("status") is not None
                    conflict = prior is not None and any(prior.get(field) != row.get(field) for field in ("lifecycle_sha256", "status", "reason_codes"))
                    if prior is None or non_open:
                        self.lifecycle_observations[request_id] = row
                    if conflict and not non_open and prior.get("status") is None:
                        self.lifecycle_observations[request_id] = {"request_id": request_id, "title": str(row.get("title") or prior.get("title") or ""), "status": "unknown", "reason_codes": ["lifecycle_shard_conflict"]}
                    if non_open or conflict:
                        blocked_lifecycle_ids.add(request_id)
            for source in raw["search_sources"]:
                source_id = str(source["source_id"])
                if source_id in sources_by_id:
                    raise ParentContractError(f"shard_source_id_duplicate:{source_id}")
                sources_by_id[source_id] = dict(source)
                artifacts_by_id[source_id] = artifacts[source_id]
            for detail in raw["request_details"]:
                request_id = str(detail["request_id"])
                prior = details_by_id.get(request_id)
                if prior is not None:
                    if _detail_conflicts(prior, detail):
                        raise ParentContractError(f"shard_request_id_conflict:{request_id}")
                    continue
                details_by_id[request_id] = detail
        for request_id in blocked_lifecycle_ids:
            details_by_id.pop(request_id, None)
        completed_required = [source_id for source_id in collection_required if source_id in sources_by_id]
        if not completed_required:
            if failures:
                raise ParentContractError("discovery_all_shards_timeout")
            raise ParentContractError("shard_sources_incomplete")
        details = sorted(details_by_id.values(), key=_candidate_rank_key)[:snapshot_contract.MAX_BATCH]
        kept = {str(detail["request_id"]) for detail in details}
        sources = []
        seen_card_ids: set[str] = set()
        for source_id in completed_required:
            source = dict(sources_by_id[source_id])
            source["card_request_ids"] = [
                request_id for value in source["card_request_ids"]
                if (request_id := str(value)) in kept and request_id not in seen_card_ids
            ]
            seen_card_ids.update(source["card_request_ids"])
            sources.append(source)
        self.source_artifacts = artifacts_by_id
        objective = self.objective
        if cursor_source is not None or len(completed_required) != len(required):
            objective = {
                **self.objective,
                "required_search_source_ids": completed_required,
            }
        collector = {
            "pass_id": self.pass_id,
            "lease_fence": lease_fence,
            "observed_at": _utc_now(),
            "objective": objective,
            "search_sources": sources,
            "request_details": details,
            "already_applied_ids": snapshot_applied_ids(already_applied),
        }
        return snapshot_contract.build_envelope(collector)


def _fresh_detail(snapshot_detail: dict[str, object], fresh: object) -> bool:
    """Freshness is identity/hash/open-state only; it never rejudges eligibility."""
    try:
        reextracted = snapshot_contract._normalise_detail(fresh)
    except Exception:
        return False
    lifecycle_status, _ = _lifecycle_disposition(fresh)
    return (
        reextracted["request_id"] == snapshot_detail["request_id"]
        and reextracted["content_sha256"] == snapshot_detail["content_sha256"]
        and reextracted["accepting_applications"] is True
        and lifecycle_status == "open"
    )


def _offer_matches_readback(decision: dict[str, object], readback: object) -> bool:
    if not isinstance(readback, dict):
        return False
    return (
        readback.get("proposal_text") == decision["proposal_text"]
        and readback.get("price_jpy") == decision["price_jpy"]
        and readback.get("deliver_date") == decision["deliver_date"]
    )


def _application_row(detail: dict[str, object], decision: dict[str, object]) -> dict[str, object]:
    """The old B2 application shape is projected only from exact-ID confirmation."""
    return {
        "request_id": detail["request_id"],
        "bucket": "single",
        "category": detail["category"],
        "title": detail["title"],
        "price_jpy": decision["price_jpy"],
        "pricing_basis": PRICING_BASIS,
        "deliver_date": decision["deliver_date"],
        "url": detail["canonical_url"],
        "compensation_type": None,
        "weekly_days": None,
        "weekly_hours_min": None,
        "weekly_hours_max": None,
    }


def _application_row_from_intent(
    detail: dict[str, object], intent: dict[str, object],
) -> dict[str, object]:
    """Project a prior fenced offer during reconciliation, never a new judgment."""
    return {
        "request_id": detail["request_id"],
        "bucket": "single",
        "category": detail["category"],
        "title": detail["title"],
        "price_jpy": intent["price_jpy"],
        "pricing_basis": PRICING_BASIS,
        "deliver_date": intent["deliver_date"],
        "url": detail["canonical_url"],
        "compensation_type": None,
        "weekly_days": None,
        "weekly_hours_min": None,
        "weekly_hours_max": None,
    }


def _confirm_locked(store: fence.IntentStore, request_id: str, intent: dict[str, object]) -> dict[str, object]:
    if intent["state"] == fence.CONFIRMED:
        return intent
    if intent["state"] != fence.PREPARED:
        raise ParentContractError("intent_state_invalid")
    confirmed = {**intent, "state": fence.CONFIRMED}
    errors = fence.validate_intent(confirmed)
    if errors:
        raise ParentContractError(";".join(errors))
    fence._durable_replace(store.intent_path(request_id), confirmed)
    return confirmed


def _pre_submit_abort_result(
    store: fence.IntentStore,
    request_id: str,
    intent: dict[str, object],
    *,
    phase: str,
    error: BaseException | str,
) -> dict[str, str]:
    described = describe_error(error) if isinstance(error, BaseException) else {
        "error": str(error), "error_type": "FormReadbackMismatch", "error_at": phase,
    }
    reason = f"pre_submit_effect_not_started:{phase}:{described['error_type']}"
    store.retire_prepared_locked(request_id, expected_cas=intent["cas"], reason=reason)
    return {
        "request_id": request_id,
        "status": f"pre_submit_aborted:{phase}:{described['error_type']}",
        "error": described["error"],
        "error_at": described["error_at"],
    }


def commit_decisions(
    snapshot: object,
    decisions: object,
    *,
    store: fence.IntentStore,
    effects: ParentEffects,
    cap_override: int | None = None,
    attempt_budget_path: Path | None = None,
    attempt_budget_pass_id: str | None = None,
) -> list[dict[str, object]]:
    """Commit decisions under exactly one target lock and per-request durable locks."""
    snapshot_errors = snapshot_contract.validate_snapshot(snapshot)
    # require_complete=False: the parent already degraded a mistyped/omitted planner id
    # to a well-formed intersection upstream (see _degrade_id_mismatch); this must not
    # re-demand full coverage and re-kill the whole commit over the same one bad id.
    decision_errors = validate_decisions(snapshot, decisions, require_complete=False)
    if snapshot_errors or decision_errors:
        raise ParentContractError(";".join(snapshot_errors + decision_errors))
    assert isinstance(snapshot, dict) and isinstance(decisions, dict)
    detail_by_id = {
        detail["request_id"]: detail for detail in snapshot["request_details"]
    }
    already_applied = set(snapshot["already_applied_ids"])
    if cap_override is None:
        cap = snapshot["objective"]["max_applications"]
    elif (
        isinstance(cap_override, bool)
        or not isinstance(cap_override, int)
        or cap_override < 1
        or cap_override > snapshot_contract.MAX_APPLICATIONS_CEILING
    ):
        raise ParentContractError("application_cap_override_invalid")
    else:
        cap = cap_override
    submit_attempts = 0
    results: list[dict[str, object]] = []

    # This is deliberately outside the per-request loop: there is one leased target
    # for the whole commit section, never a hidden second target for a detail or form.
    wedge_counts = load_wedge_counts(store)
    with effects.target_lock():
        for decision in decisions["decisions"]:
            # 2026-08-06 06:03/06:08: one submission page killed its renderer and, because
            # this section drives one leased target for every candidate, all 35 remaining
            # candidates timed out on the same dead endpoint -- twice, since the pass-level
            # browser restart reran B2 into the identical page. The unit of recovery is the
            # target, between candidates. Sequential replacement (dispose, lease fresh,
            # continue) still never holds two targets at once, which is what the one-target
            # rule protects.
            if results and cdp_wedged_row(results[-1]):
                wedged_id = str(results[-1].get("request_id") or "")
                # A hang on an UNRELATED offer page during readback_inconclusive scanning is
                # not this candidate's own action wedging (§FG', 2026-08-09): still recover
                # the target below, but never strike the candidate for a neighbour's slow page.
                if wedged_id and not readback_inconclusive_row(results[-1]):
                    wedge_counts[wedged_id] = wedge_counts.get(wedged_id, 0) + 1
                    _set_wedge_count(store, wedged_id, wedge_counts[wedged_id])
                recover = getattr(effects, "recover_wedged_target", None)
                if callable(recover):
                    recover()
            request_id = decision["request_id"]
            detail = detail_by_id[request_id]
            fresh_detail: dict[str, object] | None = None
            business_class = decision["business_class"]
            if request_id in already_applied:
                results.append({
                    "request_id": request_id,
                    "status": "dedupe_already_applied",
                    "business_class": DUPLICATE_FENCED,
                })
                continue
            with store.locked(request_id):
                existing = store._read_locked(request_id)
                recovered_prepared = False
                if existing is not None:
                    existing_readback = False
                    if existing["state"] in {fence.CONFIRMED, fence.PREPARED}:
                        try:
                            existing_readback = effects.authoritative_exact_id_readback(
                                request_id
                            )
                        except ReadbackScanTimeout as error:
                            results.append(readback_inconclusive_result(request_id, error))
                            continue
                        except Exception as error:
                            results.append(submission_failure_result(request_id, error))
                            continue
                    if existing["state"] == fence.CONFIRMED:
                        if not existing_readback:
                            results.append({
                                "request_id": request_id,
                                "status": "confirmed_unverified",
                                "business_class": DUPLICATE_FENCED,
                            })
                            continue
                        application = _application_row_from_intent(detail, existing)
                        effects.canonical_ledger_append(application)
                        results.append({
                            "request_id": request_id,
                            "status": "reconciled_confirmed",
                            "business_class": DUPLICATE_FENCED,
                            "application": application,
                        })
                        continue
                    if existing["state"] == fence.PREPARED and existing_readback:
                        try:
                            effects.crash_if_requested("after_exact_readback")
                            application = _application_row_from_intent(detail, existing)
                            effects.canonical_ledger_append(application)
                            effects.crash_if_requested("after_ledger_append")
                            _confirm_locked(store, request_id, existing)
                            results.append({
                                "request_id": request_id,
                                "status": "reconciled_confirmed",
                                "business_class": DUPLICATE_FENCED,
                                "application": application,
                            })
                        except CrashInjected as error:
                            results.append({"request_id": request_id, "status": f"crash_injected:{error}"})
                        continue
                    if existing["state"] == fence.PREPARED:
                        retryable_pre_effect = fence.is_pre_effect(existing)
                        if existing.get("version") == fence.LEGACY_VERSION:
                            # A version-1 intent has no durable phase. Do not infer one
                            # from age, error text, or request ID. The authenticated fresh
                            # accepting form plus exact official-history absence is the
                            # authoritative proof that this account currently has no effect.
                            fresh_detail = effects.reextract_detail(request_id)
                            retryable_pre_effect = _fresh_detail(detail, fresh_detail)
                        elif existing.get("effect_phase") == fence.IRREVERSIBLE_ATTEMPT_STARTED:
                            # Require official absence (above), saved non-landing evidence,
                            # and a fresh accepting form. Never infer retry from age/errors/ID.
                            proof = getattr(effects, "saved_nonlanding_submit_evidence", None)
                            fresh_detail = effects.reextract_detail(request_id)
                            retryable_pre_effect = (
                                callable(proof)
                                and proof(request_id, existing) is True
                                and _fresh_detail(detail, fresh_detail)
                            )
                        if retryable_pre_effect:
                            # Versioned intent state proves no irreversible attempt began.
                            # For legacy intents, the same permission requires both exact
                            # official absence and a fresh accepting form.
                            store.retire_prepared_locked(
                                request_id,
                                expected_cas=existing["cas"],
                                reason=(
                                    "pre_effect_restart_official_exact_id_absent"
                                    if fence.is_pre_effect(existing)
                                    else (
                                        "legacy_prepared_official_absent_and_fresh_form_present"
                                        if existing.get("version") == fence.LEGACY_VERSION
                                        else "effect_started_nonlanding_official_absent_and_fresh_form_present"
                                    )
                                ),
                            )
                            existing = None
                        else:
                            # Legacy PREPARED or an effect-started intent may already have
                            # crossed the irreversible boundary. Never guess or blind retry.
                            results.append({
                                "request_id": request_id,
                                "status": "prepared_unconfirmed",
                                "business_class": DUPLICATE_FENCED,
                            })
                            continue
                    elif existing["state"] == fence.RETIRED_ABSENT:
                        # The prior unknown attempt remains in recovery-history; this
                        # current snapshot still has to pass every normal pre-click gate.
                        existing = None
                    else:
                        results.append({
                            "request_id": request_id,
                            "status": "prepared_unconfirmed",
                            "business_class": DUPLICATE_FENCED,
                        })
                        continue
                if business_class == HARD_PROHIBITED:
                    results.append({
                        "request_id": request_id,
                        "status": HARD_PROHIBITED,
                        "business_class": HARD_PROHIBITED,
                        "reason_codes": list(decision["reason_codes"]),
                    })
                    continue
                if business_class != SUBMIT_REQUIRED:
                    # validate_decisions rejects this before the lock; retain a guard here so
                    # future callers cannot silently turn an unknown class into a submit.
                    raise ParentContractError("business_class_not_submit_required")
                # A wedge quarantine applies only to a fresh submit. Existing durable
                # intent remains a stronger no-resubmit fence and must reconcile first.
                wedge_count = wedge_counts.get(str(request_id), 0)
                if wedge_count >= WEDGE_QUARANTINE_THRESHOLD:
                    results.append({
                        "request_id": request_id,
                        "status": f"quarantined_wedging_form:count={wedge_count}",
                    })
                    continue
                if attempt_budget_path is None and submit_attempts >= cap:
                    results.append({"request_id": request_id, "status": "cap_reached"})
                    continue
                # The exact original detail is reread under this same target lock before
                # the form is opened. Changed, closed, redirected, or malformed details
                # stay absent: prepared/form/click are all zero for this request.
                if fresh_detail is None:
                    fresh_detail = effects.reextract_detail(request_id)
                if not _fresh_detail(detail, fresh_detail):
                    results.append({"request_id": request_id, "status": "stale_snapshot"})
                    continue
                if attempt_budget_path is not None:
                    if not attempt_budget_pass_id:
                        raise ParentContractError("submit_attempt_budget_pass_id_required")
                    if not _reserve_submit_attempt(
                        attempt_budget_path,
                        pass_id=attempt_budget_pass_id,
                        cap=cap,
                    ):
                        results.append({"request_id": request_id, "status": "cap_reached"})
                        continue
                intent = fence.intent_payload(
                    request_id=request_id,
                    snapshot_sha256=snapshot["snapshot_sha256"],
                    proposal_text=decision["proposal_text"],
                    price_jpy=decision["price_jpy"],
                    deliver_date=decision["deliver_date"],
                    lease_fence=snapshot["lease_fence"],
                )
                fence._durable_replace(store.intent_path(request_id), intent)
                phase = "open_form"
                submit_started = False
                try:
                    effects.crash_if_requested("after_prepare")
                    effects.open_form(request_id)
                    effects.crash_if_requested("after_open")
                    adjusted_price = effects.adjust_offer_price(
                        request_id, int(decision["price_jpy"])
                    )
                    if adjusted_price != int(decision["price_jpy"]):
                        decision = dict(decision)
                        decision["price_jpy"] = adjusted_price
                        intent = fence.intent_payload(
                            request_id=request_id,
                            snapshot_sha256=snapshot["snapshot_sha256"],
                            proposal_text=decision["proposal_text"],
                            price_jpy=adjusted_price,
                            deliver_date=decision["deliver_date"],
                            lease_fence=snapshot["lease_fence"],
                        )
                        fence._durable_replace(store.intent_path(request_id), intent)
                    phase = "fill_form"
                    effects.fill_form(
                        request_id,
                        str(decision["proposal_text"]),
                        int(decision["price_jpy"]),
                        str(decision["deliver_date"]),
                    )
                    phase = "form_readback"
                    if not _offer_matches_readback(decision, effects.readback_form(request_id)):
                        results.append(_pre_submit_abort_result(
                            store, request_id, intent, phase="form_readback",
                            error="form_readback_mismatch",
                        ))
                        continue
                    effects.crash_if_requested("after_fill_readback")
                    phase = "click_confirm"
                    effects.click_confirm(request_id)
                    effects.crash_if_requested("after_confirm_click")
                    phase = "pre_submit_headroom"
                    if not gig_disk_guard.disk_headroom_ok():
                        results.append(_pre_submit_abort_result(
                            store,
                            request_id,
                            intent,
                            phase=phase,
                            error=ParentContractError("disk_headroom_low"),
                        ))
                        continue
                    if attempt_budget_path is None and submit_attempts >= cap:
                        results.append({"request_id": request_id, "status": "cap_reached"})
                        continue
                    if attempt_budget_path is None:
                        submit_attempts += 1
                    phase = "irreversible_attempt_marker"
                    intent = store.mark_irreversible_attempt_started_locked(
                        request_id, expected_cas=intent["cas"]
                    )
                    submit_started = True
                    effects.crash_if_requested("after_irreversible_attempt_marker")
                    phase = "click_submit"
                    effects.click_submit(request_id)
                    effects.crash_if_requested("after_submit_click")
                except CrashInjected as error:
                    results.append({"request_id": request_id, "status": f"crash_injected:{error}"})
                    continue
                except Exception as error:
                    submit_control_missing = (
                        isinstance(error, ParentContractError)
                        and str(error) == "application_応募する_button_missing"
                    )
                    if not submit_started or submit_control_missing:
                        results.append(_pre_submit_abort_result(
                            store,
                            request_id,
                            intent,
                            phase="click_submit_control" if submit_control_missing else phase,
                            error=error,
                        ))
                    elif isinstance(error, ParentContractError):
                        results.append({
                            "request_id": request_id,
                            "status": f"submission_failed:{error}",
                        })
                    else:
                        results.append(submission_failure_result(request_id, error))
                    continue
                # A generic success message is intentionally ignored. Only this exact
                # request ID on the official applied-history page permits confirmation.
                try:
                    exact_id_observed = effects.authoritative_exact_id_readback(request_id)
                except ReadbackScanTimeout as error:
                    results.append(readback_inconclusive_result(request_id, error))
                    continue
                except Exception as error:
                    # A submit can kill only the current renderer. Replace that target
                    # once and retry the independent official-history readback; never
                    # click the irreversible submit control again.
                    recover = getattr(effects, "recover_wedged_target", None)
                    is_cdp_timeout = (
                        isinstance(error, ParentContractError)
                        and str(error).startswith("cdp_")
                        and "_timeout_after_" in str(error)
                    )
                    try:
                        if not is_cdp_timeout or not callable(recover) or recover() is not True:
                            raise error
                        exact_id_observed = effects.authoritative_exact_id_readback(request_id)
                    except ReadbackScanTimeout as retry_error:
                        results.append(readback_inconclusive_result(request_id, retry_error))
                        continue
                    except Exception as retry_error:
                        # The submit outcome remains unknown. Preserve PREPARED for
                        # reconciliation and continue with the next candidate.
                        results.append(submission_failure_result(request_id, retry_error))
                        continue
                if not exact_id_observed:
                    results.append({
                        "request_id": request_id,
                        "status": "awaiting_exact_id_readback",
                        "business_class": DUPLICATE_FENCED,
                    })
                    continue
                try:
                    effects.crash_if_requested("after_exact_readback")
                    application = _application_row(detail, decision)
                    # Ledger first: after the official exact-ID readback, this is
                    # the revenue fact. A crash may leave PREPARED for later
                    # reconciliation, but can never leave CONFIRMED with no ledger.
                    effects.canonical_ledger_append(application)
                    effects.crash_if_requested("after_ledger_append")
                    _confirm_locked(store, request_id, intent)
                except CrashInjected as error:
                    results.append({"request_id": request_id, "status": f"crash_injected:{error}"})
                    continue
                results.append({
                    "request_id": request_id,
                    "status": "recovered_prepared_confirmed" if recovered_prepared else "confirmed",
                    "application": application,
                })
                if wedge_counts.pop(str(request_id), None) is not None:
                    _set_wedge_count(store, str(request_id), 0)
        # The loop only notices a wedge when it reaches the NEXT candidate; the last
        # candidate's wedge would otherwise never be counted.
        if results and cdp_wedged_row(results[-1]):
            wedged_id = str(results[-1].get("request_id") or "")
            if wedged_id and not readback_inconclusive_row(results[-1]):
                wedge_counts[wedged_id] = wedge_counts.get(wedged_id, 0) + 1
                _set_wedge_count(store, wedged_id, wedge_counts[wedged_id])
            recover = getattr(effects, "recover_wedged_target", None)
            if callable(recover):
                recover()
    return results


def _reserve_submit_attempt(path: Path, *, pass_id: str, cap: int) -> bool:
    """Atomically reserve one irreversible submit attempt across one direct wake."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ParentContractError("submit_attempt_budget_invalid") from error
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("pass_id") != pass_id
                or payload.get("cap") != cap
                or isinstance(payload.get("reserved_attempts"), bool)
                or not isinstance(payload.get("reserved_attempts"), int)
                or not 0 <= payload["reserved_attempts"] <= cap
            ):
                raise ParentContractError("submit_attempt_budget_invalid")
            reserved = payload["reserved_attempts"]
        else:
            reserved = 0
        if reserved >= cap:
            return False
        fence._durable_replace(path, {
            "version": 1,
            "pass_id": pass_id,
            "cap": cap,
            "reserved_attempts": reserved + 1,
        })
        return True


def project_legacy_b2(
    snapshot: dict[str, object],
    decisions: dict[str, object],
    results: list[dict[str, object]],
    *,
    context_path: Path | None = None,
    source_artifacts: dict[str, dict[str, str]] | None = None,
    evidence: list[str] | None = None,
    planner_missing_request_ids: list[str] | None = None,
) -> dict[str, object]:
    """Build the legacy B2 result only from parent data and exact-ID confirmation."""
    def legacy_outcome(decision: dict[str, object]) -> str:
        business_class = decision.get("business_class")
        if business_class == SUBMIT_REQUIRED:
            return "eligible"
        if business_class == HARD_PROHIBITED:
            return "ineligible"
        # Historical fixture/evidence compatibility is confined to this legacy output
        # projection; the planner and commit boundary require business_class.
        return str(decision.get("eligibility") or "")

    decision_by_id = {decision["request_id"]: decision for decision in decisions["decisions"]}
    inspected = []
    for detail in snapshot["request_details"]:
        decision = decision_by_id.get(detail["request_id"])
        if decision is None:
            # The planner dropped or mistyped this id (_degrade_id_mismatch); it is not
            # attempted this wake, not silently vanished -- see planner_missing_request_ids.
            continue
        reasons = decision["reason_codes"]
        inspected.append({
            "request_id": detail["request_id"],
            "bucket": "single",
            "url": detail["canonical_url"],
            "applicants": detail["applicants_count"],
            "contracted": detail["contracted_count"],
            "budget_max_jpy": detail["budget_max_jpy"],
            "compensation_type": None,
            "compensation_min_jpy": None,
            "compensation_max_jpy": None,
            "weekly_days": None,
            "weekly_hours_min": None,
            "weekly_hours_max": None,
            "remote": None,
            "synchronous_interview_required": None,
            "human_identity_required": None,
            "accepting_applications": detail["accepting_applications"],
            "outcome": legacy_outcome(decision),
            "reason": ",".join(reasons) if reasons else None,
        })
    search_sources = []
    for source in snapshot["search_sources"]:
        artifact = (source_artifacts or {}).get(str(source["source_id"]), {})
        search_sources.append({
            "source_id": source["source_id"],
            "url": source["url"],
            "screenshot_path": artifact.get(
                "screenshot_path", f"snapshot://{source['screenshot_sha256']}"
            ),
            "live_dom_path": artifact.get(
                "live_dom_path", f"snapshot://{source['dom_sha256']}"
            ),
            "inspected_count": len(source["card_request_ids"]),
            "has_next": source["has_next"],
            "exhausted": source["exhausted"],
        })
    applications = [
        result["application"]
        for result in results
        if result["status"] in {
            "confirmed", "recovered_prepared_confirmed", "reconciled_confirmed"
        }
    ]
    first_source = snapshot["search_sources"][0]
    first_artifact = (source_artifacts or {}).get(str(first_source["source_id"]), {})
    context_sha = (
        _sha256_bytes(context_path.read_bytes())
        if context_path is not None
        else str(snapshot["snapshot_sha256"])
    )
    return {
        "status": "ok",
        "summary": "parent-owned application commit projection",
        "evidence": evidence or [f"parent://application-snapshot/{snapshot['snapshot_sha256']}"],
        "planner_missing_request_ids": sorted(planner_missing_request_ids or []),
        "eligible_count": sum(
            1 for decision in decisions["decisions"]
            if decision.get("business_class") == SUBMIT_REQUIRED
            or decision.get("eligibility") == "eligible"
        ),
        "applications": applications,
        "current_b2": {
            "context_path": str(context_path) if context_path is not None else "parent://application-snapshot",
            "context_sha256": context_sha,
            "marketplace_url": first_source["url"],
            "marketplace_screenshot_path": first_artifact.get(
                "screenshot_path", f"snapshot://{first_source['screenshot_sha256']}"
            ),
            "marketplace_live_dom_path": first_artifact.get(
                "live_dom_path", f"snapshot://{first_source['dom_sha256']}"
            ),
            "inspected_requests": inspected,
            "search_sources": search_sources,
        },
    }


class FixtureEffects:
    """Deterministic no-browser adapter used only by the focused boundary tests."""

    def __init__(self, snapshot: dict[str, object], fixture: object) -> None:
        if not isinstance(fixture, dict):
            raise ParentContractError("fixture_must_be_object")
        self.snapshot = snapshot
        self.fixture = fixture
        self.detail_by_id = {detail["request_id"]: dict(detail) for detail in snapshot["request_details"]}
        for detail in self.detail_by_id.values():
            if not any(field in detail for field in _LIFECYCLE_FIELDS):
                detail.update(page_state="present", accepting_control="present", deadline_state="future", deadline_value=_japan_today().isoformat(), form_state="present")
                detail["lifecycle_sha256"] = _lifecycle_digest(detail["request_id"], detail["canonical_url"], **{field: detail[field] for field in _LIFECYCLE_FIELDS})
        self.fresh_details = fixture.get("fresh_details") or {}
        if not isinstance(self.fresh_details, dict):
            raise ParentContractError("fixture_fresh_details_invalid")
        self.official_ids = {str(value) for value in (fixture.get("official_applied_ids") or [])}
        self.crash_at = fixture.get("crash_at")
        self.target_lock_acquires = 0
        self.target_lock_releases = 0
        self.second_target_count = 0
        self.click_count = 0
        self.open_count = 0
        self.fill_count = 0
        self.exact_id_readback_ids: list[str] = []
        self.ledger: list[dict[str, object]] = []
        self._filled: dict[str, dict[str, object]] = {}

    @contextlib.contextmanager
    def target_lock(self) -> Iterator[None]:
        self.target_lock_acquires += 1
        try:
            yield
        finally:
            self.target_lock_releases += 1

    def reextract_detail(self, request_id: str) -> dict[str, object]:
        candidate = self.fresh_details.get(request_id, self.detail_by_id[request_id])
        if not isinstance(candidate, dict):
            raise ParentContractError("fixture_fresh_detail_invalid")
        return candidate

    def saved_nonlanding_submit_evidence(
        self, request_id: str, intent: dict[str, object]
    ) -> bool:
        request_ids = self.fixture.get("saved_nonlanding_submit_ids") or []
        return isinstance(request_ids, list) and request_id in {
            str(value) for value in request_ids
        }

    def open_form(self, request_id: str) -> None:
        self.open_count += 1

    def adjust_offer_price(self, request_id: str, price_jpy: int) -> int:
        constraints = self.fixture.get("offer_price_constraints") or {}
        text = constraints.get(request_id, "") if isinstance(constraints, dict) else ""
        return _price_within_official_bounds(price_jpy, text)

    def fill_form(self, request_id: str, proposal_text: str, price_jpy: int, deliver_date: str) -> None:
        self.fill_count += 1
        self._filled[request_id] = {
            "proposal_text": proposal_text,
            "price_jpy": price_jpy,
            "deliver_date": deliver_date,
        }

    def readback_form(self, request_id: str) -> dict[str, object]:
        override = (self.fixture.get("form_readbacks") or {}).get(request_id)
        return override if isinstance(override, dict) else self._filled[request_id]

    def click_confirm(self, request_id: str) -> None:
        self.click_count += 1

    def click_submit(self, request_id: str) -> None:
        self.click_count += 1

    def authoritative_exact_id_readback(self, request_id: str) -> bool:
        self.exact_id_readback_ids.append(request_id)
        return request_id in self.official_ids

    def canonical_ledger_append(self, row: dict[str, object]) -> None:
        request_id = str(row["request_id"])
        if not any(str(existing.get("request_id")) == request_id for existing in self.ledger):
            self.ledger.append(row)

    def crash_if_requested(self, checkpoint: str) -> None:
        if self.crash_at == checkpoint:
            raise CrashInjected(checkpoint)


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def commit_fixture(snapshot: dict[str, object], decisions: dict[str, object], root: Path, fixture: object) -> dict[str, object]:
    effects = FixtureEffects(snapshot, fixture)
    results = commit_decisions(snapshot, decisions, store=fence.IntentStore(root), effects=effects)
    return {
        "results": results,
        "click_count": effects.click_count,
        "open_count": effects.open_count,
        "exact_id_readback_ids": effects.exact_id_readback_ids,
        "target_lock_acquires": effects.target_lock_acquires,
        "target_lock_releases": effects.target_lock_releases,
        "second_target_count": effects.second_target_count,
        "ledger": effects.ledger,
        "legacy_b2": project_legacy_b2(snapshot, decisions, results),
    }


def _objective_from_b2_context(context: object) -> dict[str, object]:
    if not isinstance(context, dict):
        raise ParentContractError("b2_context_invalid")
    objective = {
        "target_applications": context.get("target_applications"),
        "max_applications": context.get("max_applications"),
        "required_search_source_ids": context.get("required_search_source_ids"),
    }
    target = objective["target_applications"]
    maximum = objective["max_applications"]
    sources = objective["required_search_source_ids"]
    if (
        isinstance(target, bool)
        or isinstance(maximum, bool)
        or not isinstance(target, int)
        or not isinstance(maximum, int)
        or target < 1
        or maximum < target
        or maximum > snapshot_contract.MAX_APPLICATIONS_CEILING
        or not isinstance(sources, list)
        or not sources
        or not all(isinstance(value, str) and value.strip() for value in sources)
    ):
        raise ParentContractError("b2_context_objective_invalid")
    return objective


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ParentContractError(f"{label}_unreadable") from error
    if not isinstance(value, dict):
        raise ParentContractError(f"{label}_invalid")
    return value


def default_planner_cache_path() -> Path:
    return Path.home() / "gig" / "b2-planner-cache.json"


# Version 2 invalidates decisions made before verified age-band/prefecture
# answers were available to the planner. Keeping version 1 would suppress a
# corrected request for seven days after the planner policy changed.
INELIGIBLE_CACHE_VERSION = 2
PLANNER_CACHE_VERSION = 2
INELIGIBLE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

def default_ineligible_cache_path() -> Path:
    return Path.home() / "gig" / "b2-ineligible-cache.json"


def load_ineligible_cache(
    cache_path: Path, *, now: float | None = None, ttl_seconds: float = INELIGIBLE_CACHE_TTL_SECONDS
) -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != INELIGIBLE_CACHE_VERSION:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    clock = time.time() if now is None else now
    valid: dict[str, dict[str, object]] = {}
    for request_id, value in entries.items():
        if not isinstance(request_id, str) or not request_id.isdigit() or not isinstance(value, dict):
            continue
        content_sha256 = value.get("content_sha256")
        reasons = value.get("reason_codes")
        try:
            judged_at = float(value["judged_at_epoch"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
            or not isinstance(reasons, list)
            or not reasons
            or not all(isinstance(reason, str) and reason.strip() for reason in reasons)
            or clock < judged_at
            or clock - judged_at > ttl_seconds
        ):
            continue
        entry: dict[str, object] = {
            "content_sha256": content_sha256,
            "reason_codes": list(reasons),
            "judged_at_epoch": judged_at,
        }
        business_class = value.get("business_class")
        if business_class is not None:
            if business_class != HARD_PROHIBITED:
                continue
            entry["business_class"] = HARD_PROHIBITED
        valid[request_id] = entry
    return valid


def record_ineligible_results(
    cache_path: Path,
    snapshot: dict[str, object],
    decisions: dict[str, object],
    results: list[dict[str, object]],
    *,
    now: float | None = None,
    ttl_seconds: float = INELIGIBLE_CACHE_TTL_SECONDS,
) -> None:
    entries = load_ineligible_cache(cache_path, now=now, ttl_seconds=ttl_seconds)
    details = {str(detail["request_id"]): detail for detail in snapshot["request_details"]}
    decision_by_id = {str(row["request_id"]): row for row in decisions["decisions"]}
    judged_at = time.time() if now is None else now
    for result in results:
        request_id = str(result.get("request_id"))
        detail = details.get(request_id)
        decision = decision_by_id.get(request_id)
        if (
            result.get("status") != HARD_PROHIBITED
            or result.get("business_class") != HARD_PROHIBITED
            or not isinstance(detail, dict)
            or not isinstance(decision, dict)
            or decision.get("business_class") != HARD_PROHIBITED
            or not isinstance(decision.get("reason_codes"), list)
            or not isinstance(detail.get("content_sha256"), str)
            or validate_decisions(
                snapshot, {"decisions": [decision]}, require_complete=False
            )
        ):
            continue
        entries[request_id] = {
            "content_sha256": detail["content_sha256"],
            "business_class": HARD_PROHIBITED,
            "reason_codes": list(decision["reason_codes"]),
            "judged_at_epoch": judged_at,
        }
    _atomic_json(cache_path, {"version": INELIGIBLE_CACHE_VERSION, "entries": entries})


def _content_fingerprint(snapshot: dict[str, object]) -> str:
    """Pass-independent identity of what the planner judged.

    D2 (2026-08-09): a pass that dies between the planner returning and the commit
    finishing threw away a real model call -- the next pass built a brand-new
    evidence_dir and called the planner again over what was usually the same
    listings. snapshot_sha256 cannot key a cross-pass cache: it is computed over the
    whole envelope, including pass_id, lease_fence and observed_at, so it changes on
    every single pass by construction (application_snapshot.build_envelope). This
    fingerprint instead uses each request_detail's own content_sha256, which
    application_snapshot.py already strips of applicant counts, related-job cards and
    relative-time labels -- "observation data, not contract data". Two passes that see
    byte-identical judgeable content get the same fingerprint even though every other
    envelope field differs.
    """
    details = sorted(
        (str(detail["request_id"]), str(detail["content_sha256"]))
        for detail in snapshot["request_details"]
    )
    already_applied = sorted(str(value) for value in snapshot["already_applied_ids"])
    payload = {"details": details, "already_applied_ids": already_applied}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_cached_decisions(
    cache_path: Path, content_key: str, *, ttl_seconds: float, now: float | None = None,
) -> dict[str, object] | None:
    """A crashed pass's planner decisions, reused iff content-identical and fresh.

    Fail-closed: a missing file, a parse error, a wrong version, a consumed cache, a
    mismatched content_key, an unreadable timestamp, or an expired one all return
    None -- the caller pays for a fresh (expensive) planner call rather than acting on
    a cache it cannot trust. There is no partial-trust path.
    """
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != PLANNER_CACHE_VERSION:
        return None
    if raw.get("consumed") is not False:
        return None
    if raw.get("content_key") != content_key:
        return None
    try:
        created_at_epoch = float(raw["created_at_epoch"])
    except (KeyError, TypeError, ValueError):
        return None
    clock = time.time() if now is None else now
    if clock - created_at_epoch > ttl_seconds or clock < created_at_epoch:
        return None
    decisions = raw.get("decisions")
    missing_ids = raw.get("planner_missing_request_ids")
    if not isinstance(decisions, dict) or not isinstance(missing_ids, list):
        return None
    return {
        "decisions": decisions,
        "planner_missing_request_ids": [str(value) for value in missing_ids],
    }


def save_planner_cache(
    cache_path: Path,
    content_key: str,
    decisions: dict[str, object],
    planner_missing_request_ids: list[str],
    pass_id: str,
) -> None:
    _atomic_json(cache_path, {
        "version": PLANNER_CACHE_VERSION,
        "content_key": content_key,
        "decisions": decisions,
        "planner_missing_request_ids": list(planner_missing_request_ids),
        "pass_id": pass_id,
        "created_at": _utc_now(),
        "created_at_epoch": time.time(),
        "consumed": False,
    })


def mark_planner_cache_consumed(cache_path: Path, content_key: str) -> None:
    """Best-effort: retire a cache entry once its decisions have fully committed.

    A crash during this call leaves ``consumed`` false, which is safe -- the next
    pass replays the same (already content-matched) decisions through the same
    durable per-request intent fence in commit_decisions, which is idempotent.
    A cache file that belongs to a different content_key (or is unreadable) is left
    untouched rather than clobbered.
    """
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict) or raw.get("content_key") != content_key:
        return
    raw["consumed"] = True
    _atomic_json(cache_path, raw)


def _record_planner_claims(
    intent_root: Path, snapshot: dict[str, object], *, status: str = "claimed",
    results: list[dict[str, object]] | None = None,
) -> None:
    """Persist resource ownership before model work; the intent fence still owns effects."""
    result_by_id = {
        str(row.get("request_id")): str(row.get("status") or "unknown")
        for row in (results or [])
    }
    for detail in snapshot["request_details"]:
        assert isinstance(detail, dict)
        request_id = str(detail["request_id"])
        payload = {
            "version": 1,
            "resource_key": f"application:{request_id}",
            "request_id": request_id,
            "content_sha256": detail["content_sha256"],
            "pass_id": snapshot["pass_id"],
            "status": status,
            "updated_at_epoch": int(time.time()),
        }
        if request_id in result_by_id:
            payload["result_status"] = result_by_id[request_id]
        fence._durable_replace(intent_root / "planner-claims" / f"{request_id}.json", payload)


def collect_snapshot_with_readonly_retry(
    collector: CdpSnapshotCollector, lease_fence: dict[str, object]
) -> dict[str, object]:
    """Retry only the pre-effect CDP snapshot; no offer form can be touched here."""
    last_error: OSError | None = None
    for _ in range(2):
        try:
            return collector.collect(lease_fence)
        except OSError as error:
            last_error = error
    assert last_error is not None
    raise ParentContractError(
        f"application_snapshot_transport_failed:{type(last_error).__name__}"
    ) from last_error


PLANNER_REQUESTS_PER_CONTEXT = 10


def _workers(name: str, default: int) -> int:
    """Parallelism, overridable so it can be proven rather than argued about.

    Both of these arrived on 2026-09-02 (`claim and parallelize apply planning`,
    `run apply effects on bounded workers`) and Coconala's last application is from that same day.
    Since then every listing fails the offer-form read with a *fully rendered* top page -- the shape
    of reading a page a sibling worker navigated, not of a redirect, a session failure or a markup
    change. Serial is how the lane worked when it last applied, so serial is the default until
    concurrent browser ownership is proven safe; the env var restores concurrency without a release.
    """
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value >= 1 else default


PLANNER_PARALLEL_WORKERS = _workers("GIG_PLANNER_PARALLEL_WORKERS", 1)
APPLICATION_EFFECT_WORKERS = _workers("GIG_APPLICATION_EFFECT_WORKERS", 1)


def _planner_subsnapshot(
    snapshot: dict[str, object], request_details: list[object]
) -> dict[str, object]:
    """Create a smaller, independently hash-bound planner view.

    This is a context transport boundary, not an eligibility selector: every
    immutable request remains model-judged exactly once, then the parent
    validates the combined decisions against the original snapshot before any
    browser effect is possible.
    """
    request_ids = {str(detail["request_id"]) for detail in request_details if isinstance(detail, dict)}
    source_rows: list[dict[str, object]] = []
    for raw_source in snapshot["search_sources"]:
        assert isinstance(raw_source, dict)
        selected_ids = [
            request_id for request_id in raw_source["card_request_ids"]
            if str(request_id) in request_ids
        ]
        if selected_ids:
            source_rows.append({**raw_source, "card_request_ids": selected_ids})
    collector = {
        "pass_id": snapshot["pass_id"],
        "lease_fence": snapshot["lease_fence"],
        "observed_at": snapshot["observed_at"],
        "objective": {
            **snapshot["objective"],
            "required_search_source_ids": [row["source_id"] for row in source_rows],
        },
        "search_sources": source_rows,
        "request_details": request_details,
        "already_applied_ids": snapshot["already_applied_ids"],
    }
    return snapshot_contract.build_envelope(collector)


def _degrade_id_mismatch(
    snapshot: dict[str, object], decisions: dict[str, object], *, allow_empty: bool = False
) -> tuple[dict[str, object], list[str]]:
    """Keep only independently valid rows; report every other expected ID as missing.

    22:27 (missing=[91000119] unexpected=[91000087], a digit typo) and 23:41
    (missing=[91000102] unexpected=[], an omission) each killed the whole B2 pass over one
    bad row. A typo'd ID, duplicate row, malformed offer, or non-verbatim prohibition
    evidence can never be acted on, but it also must not erase valid siblings. Each row is
    checked by the existing strict validator; invalid rows become explicit missing IDs and
    therefore failed_transient/effect 0 in Direct.
    """
    if not isinstance(decisions, dict) or not isinstance(decisions.get("decisions"), list):
        raise ParentContractError("application_intent_planner_contract:decisions_must_be_array")
    expected_ids = {str(item["request_id"]) for item in snapshot["request_details"]}
    rows = decisions["decisions"]
    id_counts: dict[str, int] = {}
    for row in rows:
        request_id = row.get("request_id") if isinstance(row, dict) else None
        if isinstance(request_id, str):
            id_counts[request_id] = id_counts.get(request_id, 0) + 1
    kept: list[object] = []
    # Four different things get a row dropped here, and they used to share one word.
    # "unexpected_request_ids" was printed whether the planner named a listing that was not in the
    # snapshot, named one twice, returned something that was not an object, or returned a perfectly
    # addressed row that failed schema validation. Measured 2026-08-18: 17 of 58 passes logged that
    # line and the reason could not be read off it, so the lane's drop to one application a day was
    # undiagnosable from the log. Same defect the B2 gate already documents: one red meaning several
    # things. Carry the reason with the id.
    dropped: list[tuple[str, str]] = []
    for row in rows:
        request_id = row.get("request_id") if isinstance(row, dict) else None
        row_errors = validate_decisions(
            snapshot, {"decisions": [row]}, require_complete=False
        ) if isinstance(row, dict) else ["decision_must_be_object"]
        if not isinstance(row, dict):
            reason = "not_an_object"
        elif not isinstance(request_id, str):
            reason = "request_id_not_a_string"
        elif request_id not in expected_ids:
            reason = "request_id_not_in_snapshot"
        elif id_counts.get(request_id) != 1:
            reason = f"request_id_repeated_{id_counts.get(request_id)}x"
        elif row_errors:
            reason = "schema:" + ",".join(str(error) for error in row_errors[:3])
        else:
            reason = ""
        if reason:
            dropped.append((str(request_id), reason))
        else:
            kept.append(row)
    clean_decisions = {"decisions": kept}
    if not kept and not allow_empty:
        raise ParentContractError(
            "application_intent_planner_contract:decisions_empty_after_row_sanitization"
        )
    if dropped:
        print(
            "planner_decision_dropped: "
            + "; ".join(f"{request_id}={reason}" for request_id, reason in sorted(set(dropped))),
            file=sys.stderr,
        )
    kept_ids = {str(row["request_id"]) for row in kept}
    return clean_decisions, sorted(expected_ids - kept_ids)


def _invoke_isolated_planner_once(
    *,
    runner: Path,
    schema: Path,
    snapshot: dict[str, object],
    evidence_dir: Path,
    workdir: Path,
    timeout_seconds: int,
) -> tuple[dict[str, object], list[str]]:
    """Run the data-only planner through agent-runner's stdin-only route."""
    from application_planner import planner_prompt

    planner_evidence = evidence_dir
    command = [
        sys.executable,
        str(runner),
        "--task-class", "application-intent-planner",
        "--prompt-stdin",
        "--schema", str(schema),
        "--evidence-dir", str(planner_evidence),
        "--task-label", "gig-B2-planner",
        "--loop", os.environ.get("LIFE_MANAGER_LOOP_ID", "hf-gig-apply-direct"),
        "--workdir", str(workdir),
        "--timeout-seconds", str(timeout_seconds),
        # This one call decides whether to apply and writes the proposal the client reads, so it
        # takes the explicit escalation route rather than the cheapest candidate that fits.
        "--escalation-reason",
        "application decision and client-facing proposal text come from this single call",
    ]
    completed = subprocess.run(
        command,
        input=planner_prompt(snapshot),
        text=True,
        capture_output=True,
        check=False,
        timeout=max(45, timeout_seconds + 30),
    )
    if completed.returncode != 0:
        raise ParentContractError(
            f"application_intent_planner_failed: {runner_failure_detail(completed)}"
        )
    summary = _read_json_object(planner_evidence / "summary.json", "planner_summary")
    if summary.get("status") != "success":
        raise ParentContractError("application_intent_planner_summary_not_success")
    raw_result_path = summary.get("result_path")
    try:
        result_path = Path(str(raw_result_path)).resolve()
        result_path.relative_to(planner_evidence.resolve())
    except (OSError, ValueError) as error:
        raise ParentContractError("application_intent_planner_result_unowned") from error
    decisions = _read_json_object(result_path, "planner_decisions")
    return _degrade_id_mismatch(snapshot, decisions, allow_empty=True)


def invoke_isolated_planner(
    *,
    runner: Path,
    schema: Path,
    snapshot: dict[str, object],
    evidence_dir: Path,
    workdir: Path,
    timeout_seconds: int,
) -> tuple[dict[str, object], list[str]]:
    """Model-judge every request in bounded context windows before any effect.

    Returns the well-formed decisions plus any request ids the planner dropped or
    mistyped across all batches (see _degrade_id_mismatch) -- not-attempted-this-wake,
    never silently vanished.
    """
    details = snapshot["request_details"]
    assert isinstance(details, list)
    if not details:
        return {"decisions": []}, []
    rows: list[object] = []
    missing_ids: list[str] = []
    starts = list(range(0, len(details), PLANNER_REQUESTS_PER_CONTEXT))
    pool = ThreadPoolExecutor(
        max_workers=min(PLANNER_PARALLEL_WORKERS, len(starts)),
        thread_name_prefix="application-planner",
    )
    planned = {
        start: pool.submit(
            _invoke_isolated_planner_once,
            runner=runner,
            schema=schema,
            snapshot=_planner_subsnapshot(
                snapshot, details[start : start + PLANNER_REQUESTS_PER_CONTEXT]
            ),
            evidence_dir=evidence_dir / "application-intent-planner" / f"batch-{start // PLANNER_REQUESTS_PER_CONTEXT + 1:02d}",
            workdir=workdir,
            timeout_seconds=timeout_seconds,
        )
        for start in starts
    }
    pool.shutdown(wait=True)
    for start in starts:
        batch = details[start : start + PLANNER_REQUESTS_PER_CONTEXT]
        batch_snapshot = _planner_subsnapshot(snapshot, batch)
        batch_decisions, batch_missing = planned[start].result()
        if batch_missing:
            missing_set = set(batch_missing)
            retry_details = [
                detail for detail in batch
                if isinstance(detail, dict) and str(detail.get("request_id")) in missing_set
            ]
            try:
                retry_decisions, batch_missing = _invoke_isolated_planner_once(
                    runner=runner,
                    schema=schema,
                    snapshot=_planner_subsnapshot(batch_snapshot, retry_details),
                    evidence_dir=(
                        evidence_dir / "application-intent-planner"
                        / f"batch-{start // PLANNER_REQUESTS_PER_CONTEXT + 1:02d}"
                        / "retry-missing"
                    ),
                    workdir=workdir,
                    timeout_seconds=timeout_seconds,
                )
                retry_rows = retry_decisions.get("decisions")
                assert isinstance(retry_rows, list)
                batch_rows = batch_decisions.get("decisions")
                assert isinstance(batch_rows, list)
                batch_decisions = {"decisions": [*batch_rows, *retry_rows]}
            except (ParentContractError, subprocess.SubprocessError, OSError) as error:
                print(f"planner_missing_retry_failed: {error}", file=sys.stderr)
        batch_rows = batch_decisions.get("decisions")
        assert isinstance(batch_rows, list)
        rows.extend(batch_rows)
        missing_ids.extend(batch_missing)
    decisions = apply_commercial_offer_contract(snapshot, {"decisions": rows})
    if not rows:
        raise ParentContractError(
            "application_intent_planner_contract:decisions_empty_after_id_sanitization"
        )
    errors = validate_decisions(snapshot, decisions, require_complete=False)
    if errors:
        raise ParentContractError("application_intent_planner_contract:" + ";".join(errors))
    return decisions, sorted(missing_ids)


def _run_parent_pipeline(
    *,
    lease: LeaseHandle,
    snapshot: dict[str, object],
    decisions: dict[str, object],
    effects: ParentEffects,
    intent_root: Path,
    cap_override: int | None = None,
    attempt_budget_path: Path | None = None,
    attempt_budget_pass_id: str | None = None,
) -> list[dict[str, object]]:
    """The observable acquire → snapshot → plan → one-target commit sequence."""
    if snapshot.get("lease_fence") != lease.lease_fence:
        raise ParentContractError("snapshot_lease_fence_mismatch")
    lease.assert_healthy()
    results = commit_decisions(
        snapshot,
        decisions,
        store=fence.IntentStore(intent_root),
        effects=effects,
        cap_override=cap_override,
        attempt_budget_path=attempt_budget_path,
        attempt_budget_pass_id=attempt_budget_pass_id,
    )
    lease.assert_healthy()
    return results


def _run_live_commits_parallel(
    *, snapshot: dict[str, object], decisions: dict[str, object], intent_root: Path,
    lease_script: Path, lease_task: str, heartbeat_seconds: float,
    evidence_dir: Path, ledger_path: Path, pass_id: str,
    cap_override: int | None, attempt_budget_path: Path | None,
) -> list[dict[str, object]]:
    """Commit independent request IDs on two fenced targets; preserve decision order."""
    rows = decisions["decisions"]
    assert isinstance(rows, list)
    if not rows:
        return []
    details = {
        str(detail["request_id"]): detail
        for detail in snapshot["request_details"]
        if isinstance(detail, dict)
    }

    def run(index: int, decision: object) -> tuple[int, dict[str, object]]:
        assert isinstance(decision, dict)
        request_id = str(decision["request_id"])
        with LeaseHandle(
            lease_script=lease_script,
            task=f"{lease_task}-commit-{request_id}",
            heartbeat_seconds=heartbeat_seconds,
        ) as worker_lease:
            worker_snapshot = _planner_subsnapshot(
                {**snapshot, "lease_fence": worker_lease.lease_fence},
                [details[request_id]],
            )
            effects = CdpParentEffects(
                ws_url=worker_lease.ws_url,
                evidence_dir=evidence_dir / "commit-workers" / request_id,
                ledger_path=ledger_path,
                pass_id=f"{pass_id}-commit-{request_id}",
            )
            effects.ws_recycler = worker_lease.recycle
            result = commit_decisions(
                worker_snapshot,
                {"decisions": [decision]},
                store=fence.IntentStore(intent_root),
                effects=effects,
                cap_override=cap_override,
                attempt_budget_path=attempt_budget_path,
                attempt_budget_pass_id=pass_id,
            )[0]
            worker_lease.assert_healthy()
            return index, result

    with ThreadPoolExecutor(
        max_workers=min(APPLICATION_EFFECT_WORKERS, len(rows)),
        thread_name_prefix="application-effect",
    ) as pool:
        jobs = [pool.submit(run, index, decision) for index, decision in enumerate(rows)]
        completed = [job.result() for job in jobs]
    return [row for _, row in sorted(completed)]


def _readonly_discovery_effect_factory(
    *,
    lease_script: Path,
    lease_task: str,
    evidence_dir: Path,
    ledger_path: Path,
    pass_id: str,
    heartbeat_seconds: float,
) -> Callable[[int], object]:
    lease_file_lock = threading.Lock()

    def factory(index: int) -> object:
        @contextlib.contextmanager
        def opened() -> Iterator[CdpParentEffects]:
            lease = LeaseHandle(
                lease_script=lease_script,
                task=f"{lease_task}-discovery-{index}",
                heartbeat_seconds=heartbeat_seconds,
            )
            with lease_file_lock:
                lease.__enter__()
            pending_error: BaseException | None = None
            try:
                yield CdpParentEffects(
                    ws_url=lease.ws_url,
                    evidence_dir=evidence_dir / "discovery" / f"shard-{index}",
                    ledger_path=ledger_path,
                    pass_id=f"{pass_id}-discovery-{index}",
                )
            except BaseException as error:
                pending_error = error
                raise
            else:
                try:
                    with lease_file_lock:
                        lease.assert_healthy()
                except BaseException as error:
                    pending_error = error
                    raise
            finally:
                try:
                    with lease_file_lock:
                        lease.__exit__(None, None, None)
                except BaseException:
                    if pending_error is None:
                        raise

        return opened()

    return factory


def run_parent(
    *,
    lease_script: Path,
    lease_task: str,
    context_path: Path,
    pass_id: str,
    evidence_dir: Path,
    intent_root: Path,
    ledger_path: Path,
    output_path: Path,
    planner_runner: Path | None,
    planner_schema: Path,
    planner_workdir: Path,
    planner_timeout_seconds: int,
    heartbeat_seconds: float,
    fixture: dict[str, object] | None = None,
    planner_cache_path: Path | None = None,
    planner_cache_ttl_seconds: float = 14400.0,
    ineligible_cache_path: Path | None = None,
    ineligible_cache_ttl_seconds: float = INELIGIBLE_CACHE_TTL_SECONDS,
    cursor_contract: dict[str, object] | None = None,
    all_eligible: bool = False,
    attempt_budget_path: Path | None = None,
) -> dict[str, object]:
    """Acquire once, snapshot, isolate model judgment, commit, and always release."""
    context = _read_json_object(context_path, "b2_context")
    objective = _objective_from_b2_context(context)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    planner_missing_request_ids: list[str] = []
    planner_content_key: str | None = None
    cache_path = planner_cache_path or default_planner_cache_path()
    ineligible_path = ineligible_cache_path
    with LeaseHandle(
        lease_script=lease_script,
        task=lease_task,
        heartbeat_seconds=heartbeat_seconds,
    ) as lease:
        if fixture is not None:
            collector = fixture.get("collector")
            if not isinstance(collector, dict):
                raise ParentContractError("fixture_collector_invalid")
            collector = {**collector, "pass_id": pass_id, "lease_fence": lease.lease_fence}
            snapshot = snapshot_contract.build_envelope(collector)
            raw_fixture_effects = fixture.get("effects") or {}
            effects: ParentEffects = FixtureEffects(snapshot, raw_fixture_effects)
            decisions = fixture.get("decisions")
            if not isinstance(decisions, dict):
                raise ParentContractError("fixture_decisions_invalid")
            source_artifacts: dict[str, dict[str, str]] = {}
        else:
            if planner_runner is None:
                raise ParentContractError("planner_runner_required")
            effects = CdpParentEffects(
                ws_url=lease.ws_url,
                evidence_dir=evidence_dir,
                ledger_path=ledger_path,
                pass_id=pass_id,
            )
            if hasattr(effects, "instant_work_event_publisher"):
                effects.instant_work_event_publisher = _publish_instant_work_events
                # Recover any verified rows left behind by an earlier wake before this
                # snapshot starts.  Projection and outbox event keys make this idempotent.
                _publish_instant_work_events(ledger_path, pass_id)
            effects.ws_recycler = lease.recycle
            collector = CdpSnapshotCollector(
                effects,
                pass_id=pass_id,
                objective=objective,
                excluded_request_ids=quarantined_request_ids(intent_root),
                intent_store=fence.IntentStore(intent_root),
                cursor_contract=cursor_contract,
                ineligible_cache=(
                    load_ineligible_cache(
                        ineligible_path, ttl_seconds=ineligible_cache_ttl_seconds
                    )
                    if ineligible_path is not None
                    else {}
                ),
                discovery_effect_factory=_readonly_discovery_effect_factory(
                    lease_script=lease_script,
                    lease_task=lease_task,
                    evidence_dir=evidence_dir,
                    ledger_path=ledger_path,
                    pass_id=pass_id,
                    heartbeat_seconds=heartbeat_seconds,
                ),
            )
            snapshot = collect_snapshot_with_readonly_retry(collector, lease.lease_fence)
            source_artifacts = collector.source_artifacts
            snapshot_path = evidence_dir / "application-snapshot.json"
            _atomic_json(snapshot_path, snapshot)
            _atomic_json(
                evidence_dir / "application-observations.json",
                collector.observation_payload(),
            )
            _record_planner_claims(intent_root, snapshot)
            # D2: a prior pass may have already paid for a planner call over this exact
            # judgeable content and then died before committing it (browser wedge kill,
            # OOM, launchd timeout). Reuse it instead of re-asking the model.
            if snapshot["request_details"]:
                planner_content_key = _content_fingerprint(snapshot)
                cached = load_cached_decisions(
                    cache_path, planner_content_key, ttl_seconds=planner_cache_ttl_seconds,
                )
            else:
                cached = None
            if cached is not None:
                decisions = cached["decisions"]
                planner_missing_request_ids = cached["planner_missing_request_ids"]
            elif not snapshot["request_details"]:
                decisions = {"decisions": []}
            else:
                decisions, planner_missing_request_ids = invoke_isolated_planner(
                    runner=planner_runner,
                    schema=planner_schema,
                    snapshot=snapshot,
                    evidence_dir=evidence_dir,
                    workdir=planner_workdir,
                    timeout_seconds=planner_timeout_seconds,
                )
                save_planner_cache(
                    cache_path, planner_content_key, decisions,
                    planner_missing_request_ids, pass_id,
                )
        snapshot_path = evidence_dir / "application-snapshot.json"
        if not snapshot_path.exists():
            _atomic_json(snapshot_path, snapshot)
        decisions_path = evidence_dir / "application-decisions.json"
        _atomic_json(decisions_path, decisions)
        cap_override = snapshot_contract.MAX_APPLICATIONS_CEILING if all_eligible else None
        if fixture is None:
            results = _run_live_commits_parallel(
                snapshot=snapshot, decisions=decisions, intent_root=intent_root,
                lease_script=lease_script, lease_task=lease_task,
                heartbeat_seconds=heartbeat_seconds, evidence_dir=evidence_dir,
                ledger_path=ledger_path, pass_id=pass_id, cap_override=cap_override,
                attempt_budget_path=attempt_budget_path,
            )
            lease.assert_healthy()
        else:
            results = _run_parent_pipeline(
                lease=lease, snapshot=snapshot, decisions=decisions, effects=effects,
                intent_root=intent_root, cap_override=cap_override,
                attempt_budget_path=attempt_budget_path,
                attempt_budget_pass_id=pass_id,
            )
        if fixture is None:
            _record_planner_claims(intent_root, snapshot, status="completed", results=results)
        if fixture is None and ineligible_path is not None:
            record_ineligible_results(
                ineligible_path,
                snapshot,
                decisions,
                results,
                ttl_seconds=ineligible_cache_ttl_seconds,
            )
        if fixture is None and planner_content_key is not None:
            # The whole commit ran to completion (no exception escaped): these
            # decisions are done, win or lose per-request. Stop offering them for
            # reuse so a later pass with unchanged content asks the planner fresh
            # instead of re-paying browser readback cost on already-settled rows.
            mark_planner_cache_consumed(cache_path, planner_content_key)
        if isinstance(effects, CdpParentEffects):
            confirmed_ids = {
                str(result["request_id"])
                for result in results
                if result.get("status") in CONFIRMED_STATUSES
            }
            effects.finalize_exact_readback(confirmed_ids)
        legacy_b2 = project_legacy_b2(
            snapshot,
            decisions,
            results,
            context_path=context_path,
            source_artifacts=source_artifacts,
            evidence=[str(snapshot_path.resolve()), str(decisions_path.resolve())],
            planner_missing_request_ids=planner_missing_request_ids,
        )
        _atomic_json(output_path, legacy_b2)
        _atomic_json(
            evidence_dir / "parent-commit.json",
            {
                "snapshot_sha256": snapshot["snapshot_sha256"],
                "planner_missing_request_ids": planner_missing_request_ids,
                "results": results,
            },
        )
        _atomic_json(
            evidence_dir / "summary.json",
            {
                "status": "success",
                "task_label": "gig-B2",
                "result_path": str(output_path.resolve()),
                "recorded_by": "application_parent",
            },
        )
        return {"snapshot": snapshot, "decisions": decisions, "results": results, "legacy_b2": legacy_b2}


def runner_failure_detail(completed: "subprocess.CompletedProcess[str]") -> str:
    """Why the runner failed, from the output that was already captured.

    The planner call runs with capture_output=True and then raised on a non-zero return code
    without reading either pipe. Ten failures on 2026-08-04/05 arrived as
    application_intent_planner_failed and nothing more, while the runner's own explanation
    sat in a variable that was thrown away.

    Kept to the tail because stack traces put the useful line last, flattened to one line
    because this lands in a JSONL log where a newline would split one failure into several
    rows, and never empty because an unexplained failure is the thing being removed.
    """
    stream = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    if not stream:
        return f"rc={completed.returncode} (runner produced no output)"
    tail = " ".join(stream[-900:].split())
    return f"rc={completed.returncode} {tail}"


def describe_error(error: BaseException) -> dict[str, str]:
    """Turn an exception into a failure that can name itself.

    The handler below used to record only str(error). Twenty-four of this lane's failures on
    2026-08-04/05 came out as {"ok":false,"error":""} — an exception raised without a message,
    with its type, its location and its traceback all thrown away. Five exception classes
    share that handler, so "" could have been any of them, and nothing could be grouped or
    counted or found.

    A failure that produces no distinguishing row is the same shape as the silence the paid
    lane spent the day removing, living inside the error channel. So: never empty, always
    typed, always located.
    """
    message = str(error).strip()
    if not message:
        message = f"{type(error).__name__} (no message)"

    # Prefer the deepest frame we own. The deepest frame overall for a malformed JSON file is
    # decoder.py:361, which tells nobody which of this file's 1800 lines called it.
    import os
    import sysconfig

    # Both sides are resolved before comparing. sysconfig reports the Homebrew symlink
    # (/opt/homebrew/opt/python@3.14/...) while tracebacks report the real Cellar path
    # (/opt/homebrew/Cellar/python@3.14/3.14.6/...), so a plain prefix match never fires and
    # every error would keep pointing at decoder.py. Prefix matching across a symlink is a
    # comparison of two different spellings of the same place.
    library_roots = tuple(
        os.path.realpath(path)
        for path in (
            sysconfig.get_paths().get("stdlib"),
            sysconfig.get_paths().get("platstdlib"),
            sysconfig.get_paths().get("purelib"),
        )
        if path
    )

    where = "unknown"
    frames: list[tuple[str, int]] = []
    tb = getattr(error, "__traceback__", None)
    while tb is not None:
        frames.append((tb.tb_frame.f_code.co_filename, tb.tb_lineno))
        tb = tb.tb_next
    if frames:
        ours = [f for f in frames if not os.path.realpath(f[0]).startswith(library_roots)]
        filename, lineno = (ours or frames)[-1]
        where = f"{Path(filename).name}:{lineno}"

    return {"error": message, "error_type": type(error).__name__, "error_at": where}


WEDGE_QUARANTINE_THRESHOLD = 3
WEDGE_QUARANTINE_TTL_SECONDS = 48 * 60 * 60
_WEDGE_COUNTS_LOCK = threading.RLock()


def _wedge_counts_path(store: "fence.IntentStore") -> Path:
    return Path(store.root) / "wedge-quarantine.json"


def quarantined_request_ids(intent_root: Path | str) -> set[str]:
    """Requests the commit boundary already refuses, so the batch can stop buying them.

    Quarantine was enforced at commit time only. Nothing kept a quarantined request out
    of the next snapshot, so the same listings were re-collected, re-inspected and
    re-judged every hour, and each one spent a batch slot plus a planner judgment on an
    outcome that was decided before the pass began. Measured 2026-08-07 over 36 passes
    of real evidence: 13 request_ids produced 120 quarantine events -- 3.33 of the 5.86
    eligible slots a pass has, against 1.03 confirmed applications -- and request 95000014
    was collected, judged eligible and quarantined in 29 consecutive passes.

    Live-lineage adaptation (§FK'): reads through the TTL-aware load_wedge_counts, so a
    quarantine the 48h TTL has already ended stops excluding the request from collection
    at the same moment it stops blocking the commit -- one loader, one meaning.
    """
    counts = load_wedge_counts(fence.IntentStore(intent_root))
    return {
        request_id
        for request_id, count in counts.items()
        if count >= WEDGE_QUARANTINE_THRESHOLD
    }


def _load_wedge_raw(store: "fence.IntentStore") -> dict[str, dict[str, object]]:
    """On-disk entries with their own recorded timestamp, undecayed.

    Only used by save_wedge_counts, to preserve each id's own updated_at across a save
    triggered by a *different* id wedging -- a fresh save must not silently reset the
    clock on ids nothing happened to.
    """
    try:
        raw = json.loads(_wedge_counts_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, dict) and "count" in value and "updated_at" in value
    }


def load_wedge_counts(store: "fence.IntentStore") -> dict[str, int]:
    """Effective wedge counts right now: expired and legacy no-timestamp entries decay away.

    Entries carry a "count" + "updated_at" since 2026-08-08. Before that, an increment
    only ever wrote a bare int -- measured live 2026-08-08: 37 entries, 18 of them at
    WEDGE_QUARANTINE_THRESHOLD, quarantined forever, because nothing recorded *when*
    they last wedged and nothing ever cleared them. A bare int carries no evidence of
    recency, so it decays immediately rather than being trusted as still-fresh -- those
    ids revive on the next load. A malformed entry (wrong type, missing field, unparseable)
    is the opposite risk: it could be hiding a genuinely live wedge behind corrupted data,
    so it stays quarantined at the threshold and gets logged instead of silently forgiven.
    """
    try:
        raw = json.loads(_wedge_counts_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    counts: dict[str, int] = {}
    decayed = False
    for key, value in raw.items():
        if isinstance(value, bool):
            print(f"wedge_quarantine_malformed_entry_kept_quarantined: {key}", file=sys.stderr)
            counts[str(key)] = WEDGE_QUARANTINE_THRESHOLD
            continue
        if isinstance(value, int):
            decayed = True  # legacy count-only entry: no timestamp, decays immediately
            continue
        if isinstance(value, dict):
            try:
                count = int(value["count"])
                updated_at = float(value["updated_at"])
            except (KeyError, TypeError, ValueError):
                print(f"wedge_quarantine_malformed_entry_kept_quarantined: {key}", file=sys.stderr)
                counts[str(key)] = WEDGE_QUARANTINE_THRESHOLD
                continue
            if now - updated_at >= WEDGE_QUARANTINE_TTL_SECONDS:
                decayed = True
                continue
            if count > 0:
                counts[str(key)] = count
            continue
        print(f"wedge_quarantine_malformed_entry_kept_quarantined: {key}", file=sys.stderr)
        counts[str(key)] = WEDGE_QUARANTINE_THRESHOLD
    if decayed:
        # Reap on read: every call site that clears a count on confirm skips its own save
        # when the id was already absent from the in-memory dict (pop() -> None), which is
        # exactly the case for an id that just decayed here. Without this, that id's stale
        # raw entry sits on disk forever even though every future load already ignores it.
        save_wedge_counts(store, counts)
    return counts


def save_wedge_counts(store: "fence.IntentStore", counts: dict[str, int]) -> None:
    path = _wedge_counts_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _load_wedge_raw(store)
    now = time.time()
    payload: dict[str, dict[str, object]] = {}
    for key, value in counts.items():
        if value <= 0:
            continue
        prior = previous.get(str(key))
        prior_count = prior.get("count") if prior else None
        updated_at = prior.get("updated_at") if (prior and prior_count == value) else now
        payload[str(key)] = {"count": value, "updated_at": updated_at}
    _atomic_json(path, payload)


def _set_wedge_count(store: "fence.IntentStore", request_id: str, count: int) -> None:
    """Update one resource without losing a sibling worker's counter."""
    with _WEDGE_COUNTS_LOCK:
        counts = load_wedge_counts(store)
        if count > 0:
            counts[request_id] = count
        else:
            counts.pop(request_id, None)
        save_wedge_counts(store, counts)


def ledger_applied_ids(ledger_path: Path) -> set[str]:
    """Every request this loop has ever applied to, from its own durable ledger.

    The site's applied-offers page is walked one page deep -- 20 rows -- so every
    application older than that was invisible to the collector's exclusion set. Measured
    2026-08-06: eight consecutive passes inspected only 69 unique jobs, 49 of them
    repeatedly, roughly 25 ineligibles per pass were 既応募, and confirmed stayed at zero
    for ten passes. The 40-slot inspection batch was being spent on work already done.

    Never raises: this runs before every snapshot, and a half-written ledger must not take
    the apply lane down -- it only means the site page is the sole exclusion this pass.
    """
    identifiers: set[str] = set()
    try:
        lines = Path(ledger_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return identifiers
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        value = row.get("requestId") or row.get("request_id")
        if value is not None and str(value).strip():
            identifiers.add(str(value).strip())
    return identifiers


def snapshot_applied_ids(identifiers: object) -> list[str]:
    """The subset of exclusions the snapshot contract will accept.

    The ledger holds 31 non-decimal identities (dm-96000006 and friends: direct-message
    threads, not 募集). They must still suppress re-inspection -- a thread we already
    answered is not fresh work -- but the snapshot contract requires decimal request ids,
    and feeding them through produced request_id_must_be_decimal and killed the lane.

    So the two sets are deliberately different sizes: exclusion is everything we have ever
    touched, the envelope field is only what its own contract can carry.
    """
    if not isinstance(identifiers, (list, tuple, set)):
        return []
    return sorted(
        {str(value) for value in identifiers if str(value).isdigit()},
        key=int,
    )


def cdp_wedged_row(row: dict[str, object]) -> bool:
    """Did this candidate die because its CDP endpoint stopped answering?

    Distinct from a business rejection: a form that refuses a proposal is a live page, and
    recycling the target on every refusal would tear down a working session for nothing.
    """
    text = f"{row.get('status') or ''} {row.get('error') or ''}"
    return "cdp_" in text and "timeout" in text


def readback_inconclusive_row(row: dict[str, object]) -> bool:
    """Was this row a hang on someone ELSE's page during a readback scan, not this
    candidate's own action?

    cdp_wedged_row cannot make this distinction on text alone -- the underlying
    ReadbackScanTimeout error message still contains "cdp_" and "timeout". This checks the
    status prefix commit_decisions gives that specific outcome, so the strike-attribution
    sites can recover the browser target (still a real hang, still worth recycling) without
    exiling the candidate for a neighbour's slow page.
    """
    return str(row.get("status") or "").startswith("readback_inconclusive")


CONFIRMED_STATUSES = ("confirmed", "recovered_prepared_confirmed", "reconciled_confirmed")


def commit_browser_wedged(results: list[dict[str, object]]) -> bool:
    """Did the browser die for every application this commit actually attempted?

    One leased target is held for the whole commit section, so a renderer that wedges on the
    first 応募 form takes every later candidate with it. On 2026-08-05 that produced three
    consecutive passes of {"ok":true,"results":34} with four CDP timeouts and no application,
    because scoring each candidate as a runtime failure still left the parent reporting
    success -- and the pass's browser restart is keyed on the parent failing.

    Deliberately narrow. No attempt at all is a thin market, not a fault, and a submission
    the form itself rejected is a business outcome a restart would neither fix nor explain.

    2026-08-08: gig-pass-1786201210-10819 raised this anyway on a commit that landed a real
    confirmed application (23 ineligible + 4 quarantined + 1 cdp timeout + 1 confirmed).
    "confirmed" matched none of the attempted-status prefixes below, so it was silently
    dropped from the tally and the lone timeout looked like 100% of "every attempt". A
    confirmed application in the same commit is the browser demonstrably working -- it can
    never be scored as fully wedged, no matter how many other candidates timed out.
    """
    if any(str(row.get("status") or "") in CONFIRMED_STATUSES for row in results):
        return False
    attempted = [
        row for row in results
        if str(row.get("status") or "").startswith(
            ("submission_runtime_failed", "submission_failed", "applied", "awaiting_",
             "readback_inconclusive", "pre_submit_aborted")
        )
    ]
    if not attempted:
        return False
    # readback_inconclusive rows deliberately never strike their candidate, but a commit in
    # which EVERY attempt died on a CDP timeout -- own-action or neighbour-scan -- is still a
    # dead browser, and hiding it here would let the loop repeat the same dead hour without
    # the pass-level restart. A truncation-inconclusive row (no cdp_ in its error) keeps
    # this False: an exhausted page budget is not a browser fault.
    return all(
        str(row.get("status") or "").startswith(
            ("submission_runtime_failed", "readback_inconclusive", "pre_submit_aborted")
        )
        and str(row.get("error") or "").startswith("cdp_")
        and "timeout" in str(row.get("error") or "")
        for row in attempted
    )


def submission_failure_result(request_id: str, error: BaseException) -> dict[str, str]:
    """One unapplied candidate, recorded so the cause survives the loop.

    The three handlers that use this all raised the same status string,
    submission_runtime_failed:ParentContractError. That class is raised from more than a dozen
    places here, so the status could not distinguish a click that never landed from a readback
    transport that died -- and the operator's question, why did this application not go
    through, had no answer anywhere in the evidence tree.
    """
    described = describe_error(error)
    return {
        "request_id": request_id,
        "status": f"submission_runtime_failed:{described['error_type']}",
        "error": described["error"],
        "error_at": described["error_at"],
    }


def readback_inconclusive_result(request_id: str, error: BaseException) -> dict[str, str]:
    """One candidate whose readback hit an unrelated entry's hang, not its own outcome.

    Deliberately a different status prefix from submission_failure_result: this row must
    never count as a wedge strike against `request_id` (readback_inconclusive_row), and the
    candidate's durable intent state is left exactly as it was for retry next pass -- neither
    confirmed nor quarantined off the misattributed hang of someone else's offer page.
    """
    described = describe_error(error)
    return {
        "request_id": request_id,
        "status": f"readback_inconclusive:{described['error_type']}",
        "error": described["error"],
        "error_at": described["error_at"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fixture = subparsers.add_parser("commit-fixture")
    fixture.add_argument("--snapshot", type=Path, required=True)
    fixture.add_argument("--decisions", type=Path, required=True)
    fixture.add_argument("--intent-root", type=Path, required=True)
    fixture.add_argument("--fixture", type=Path, required=True)
    fixture.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("run", help="run the parent-owned live B2 boundary")
    run.add_argument("--lease-script", type=Path, required=True)
    run.add_argument("--lease-task", required=True)
    run.add_argument("--context", type=Path, required=True)
    run.add_argument("--pass-id", required=True)
    run.add_argument("--evidence-dir", type=Path, required=True)
    run.add_argument("--intent-root", type=Path, required=True)
    run.add_argument("--ledger", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--planner-runner", type=Path)
    run.add_argument(
        "--planner-schema",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "schemas" / "application_decisions.schema.json",
    )
    run.add_argument("--planner-workdir", type=Path, default=Path.home())
    run.add_argument("--planner-timeout-seconds", type=int, default=900)
    run.add_argument("--heartbeat-seconds", type=float, default=20.0)
    run.add_argument(
        "--planner-cache", type=Path, default=None,
        help="durable cross-pass planner decision cache (default: ~/gig/b2-planner-cache.json)",
    )
    run.add_argument("--planner-cache-ttl-seconds", type=float, default=14400.0)
    run.add_argument(
        "--ineligible-cache", type=Path, default=default_ineligible_cache_path(),
        help="durable ineligible-request cache (default: ~/gig/b2-ineligible-cache.json)",
    )
    run.add_argument("--fixture", type=Path, help="test-only collector/decision/effect fixture")
    run.add_argument("--cursor-contract", type=Path)
    run.add_argument(
        "--all-eligible",
        action="store_true",
        help="use the existing 20-application ceiling instead of this snapshot's objective cap",
    )
    run.add_argument(
        "--attempt-budget",
        type=Path,
        help="shared same-pass irreversible submit-attempt budget",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            fixture_data = (
                _read_json_object(args.fixture, "fixture")
                if args.fixture is not None
                else None
            )
            cursor_contract = _read_json_object(args.cursor_contract, "cursor_contract") if args.cursor_contract is not None else None
            payload = run_parent(
                lease_script=args.lease_script,
                lease_task=args.lease_task,
                context_path=args.context,
                pass_id=args.pass_id,
                evidence_dir=args.evidence_dir,
                intent_root=args.intent_root,
                ledger_path=args.ledger,
                output_path=args.output,
                planner_runner=args.planner_runner,
                planner_schema=args.planner_schema,
                planner_workdir=args.planner_workdir,
                planner_timeout_seconds=args.planner_timeout_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                fixture=fixture_data,
                planner_cache_path=args.planner_cache,
                planner_cache_ttl_seconds=args.planner_cache_ttl_seconds,
                ineligible_cache_path=args.ineligible_cache,
                cursor_contract=cursor_contract,
                all_eligible=args.all_eligible,
                attempt_budget_path=args.attempt_budget,
            )
            if commit_browser_wedged(payload["results"]):
                # Not a successful commit. The pass restarts a wedged browser and retries,
                # and it keys that on this exit code -- reporting success here is what let
                # three consecutive passes attempt four applications, lose all four to CDP
                # timeouts, and record nothing while calling themselves fine.
                print(json.dumps(
                    {
                        "ok": False,
                        "error": "cdp_browser_wedged_for_every_attempt",
                        "error_type": "ParentContractError",
                        "results": len(payload["results"]),
                    },
                    separators=(",", ":"),
                ))
                return 1
            success: dict[str, object] = {"ok": True, "results": len(payload["results"])}
            wedged_request_ids = [
                str(row.get("request_id") or "")
                for row in payload["results"]
                if cdp_wedged_row(row)
            ]
            if wedged_request_ids:
                # Success-with-partials: some candidates in this commit wedged but at least
                # one confirmed (or the pass had other real judgments complete), so this is
                # not the all-attempts-wedged failure above. Surface the count for
                # healthchecks without failing a commit that otherwise did real work.
                success["wedged_request_ids"] = wedged_request_ids
            print(json.dumps(success, separators=(",", ":")))
            return 0
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        decisions = json.loads(args.decisions.read_text(encoding="utf-8"))
        fixture_data = json.loads(args.fixture.read_text(encoding="utf-8"))
        payload = commit_fixture(snapshot, decisions, args.intent_root, fixture_data)
        _atomic_json(args.output, payload)
        print(json.dumps({"ok": True}, separators=(",", ":")))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, ParentContractError, fence.IntentFenceError) as error:
        print(
            json.dumps({"ok": False, **describe_error(error)}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
