#!/usr/bin/env python3
"""Publish the one assigned Japanese X Post with a durable effect fence."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
# --- fail-closed PII gate wiring ---------------------------------------------------
# gate_* raise SystemExit (non-zero) on a finding, a missing blocklist, an unreadable
# artifact, or ANY scanner error. There is no code path where a failure means publish.
import sys as _pii_sys  # noqa: E402
from pathlib import Path as _PiiPath  # noqa: E402
_pii_sys.path.insert(0, str(next(
    _p / "_shared"
    for _p in _PiiPath(__file__).resolve().parents
    if (_p / "_shared" / "pii_gate.py").is_file()
)))
from pii_gate import gate_files, gate_run_dir, gate_text  # noqa: E402,F401

from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")


class XPostRefused(RuntimeError):
    pass


def validate_text(value: str) -> None:
    if not value or len(value) > 280:
        raise XPostRefused("X Post must contain 1..280 characters")
    # The frozen X Post contract requires one measurable self-hosted product
    # CTA, and publication-state initialization has already rerun cta-gate on
    # these exact bytes. Do not contradict that revenue contract by banning
    # every URL at the final side-effect boundary.


def assigned_date(slots: dict[str, Any], run_id: str) -> str:
    matches = [
        date_jst
        for date_jst, slot in slots.get("slots", {}).items()
        if isinstance(slot, dict)
        and slot.get("status") == "ASSIGNED"
        and slot.get("owner_run_id") == run_id
        and slot.get("date_jst") == date_jst
    ]
    if len(matches) != 1:
        raise XPostRefused("X Post slot is missing or ambiguous")
    return matches[0]


def _status_id(value: str) -> str:
    match = re.search(r"/status/([0-9]+)(?:$|[?#])", value)
    return match.group(1) if match else ""


def effect_fence(
    run_id: str,
    date_jst: str,
    status_ids: list[str],
    effect_started_at: str,
) -> dict[str, Any]:
    if any(
        not isinstance(item, str)
        or not item.isascii()
        or not item.isdigit()
        or int(item) <= 0
        for item in status_ids
    ):
        raise XPostRefused("pre-effect X status IDs are malformed")
    canonical = sorted(set(status_ids), key=int)
    return {
        "version": 1,
        "run_id": run_id,
        "assigned_date_jst": date_jst,
        "effect_started_at": effect_started_at,
        "pre_effect_status_ids": canonical,
        "pre_effect_high_water_status_id": canonical[-1] if canonical else "0",
        "phase": "effect-possible",
        "target": "",
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _guard(command: str) -> dict[str, Any]:
    guard = Path(__file__).parents[1] / "publication-guard.py"
    args = [
        sys.executable,
        str(guard),
        command,
        "--pair",
        "x-post/ja",
    ]
    if command == "preflight":
        state = json.loads(
            Path(os.environ["ARTICLE_PUBLICATION_STATE"]).read_text(
                encoding="utf-8"
            )
        )
        args.extend(
            [
                "--target-kind",
                "x-post-slot",
                "--target",
                str(state["run_id"]),
            ]
        )
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise XPostRefused(result.stderr.strip() or "X Post guard refused")
    return json.loads(result.stdout)


def reconcile_existing_effect(
    fence_path: Path,
) -> dict[str, Any] | None:
    """Resolve a prior possible effect without ever opening the composer."""
    if not fence_path.is_file():
        return None
    try:
        fence = json.loads(fence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise XPostRefused("existing X Post effect fence is unreadable") from error
    if not isinstance(fence, dict):
        raise XPostRefused("existing X Post effect fence is malformed")
    phase = fence.get("phase")
    if phase == "confirmed-no-effect":
        return None
    if phase not in {"effect-possible", "target-known"}:
        raise XPostRefused("existing X Post effect fence is malformed")
    result = _guard("recover-ambiguous")
    if result.get("status") != "live" and result.get("action") != "skip-live":
        raise XPostRefused(
            "X Post existing effect readback did not become live"
        )
    return result


def _account_url(state: dict[str, Any]) -> str:
    identities = state.get("destination_identities", {})
    account = str(identities.get("x-post/ja", "")).strip().lstrip("@") \
        if isinstance(identities, dict) else ""
    if not account:
        raise XPostRefused("persisted X Post destination identity is required")
    if not account.isascii() or not re.fullmatch(r"[A-Za-z0-9_]+", account):
        raise XPostRefused("configured X account is invalid")
    return f"https://x.com/{account}"


def _same_account(url: str, expected: str) -> bool:
    parsed = urlparse(url)
    expected_parsed = urlparse(expected)
    segments = [part for part in parsed.path.split("/") if part]
    return (
        parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        and len(segments) == 1
        and segments[0].lower()
        == expected_parsed.path.strip("/").lower()
    )


def _authenticated_profile_matches(page: Any, expected: str) -> bool:
    profile = page.locator('[data-testid="AppTabBar_Profile_Link"]')
    href = profile.first.get_attribute("href") if profile.count() else ""
    return bool(href) and _same_account(urljoin("https://x.com", href), expected)


def _timeline_rows(page) -> list[dict[str, str]]:
    rows = page.locator('[data-testid="tweet"]').evaluate_all(
        """tweets => tweets.map(tweet => ({
          status_href: (tweet.querySelector('a[href*="/status/"]') || {}).href || '',
          created_at: ((tweet.querySelector('time[datetime]') || {}).dateTime || '')
        }))"""
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise XPostRefused("X account timeline snapshot is malformed")
    return rows


def _created_status_id(responses: list[Any]) -> str:
    for response in responses:
        try:
            payload = response.json()
            value = str(
                payload["data"]["create_tweet"]["tweet_results"]["result"]["rest_id"]
            )
        except Exception:
            continue
        if value.isascii() and value.isdigit() and int(value) > 0:
            return value
    return ""


def publish() -> dict[str, Any]:
    state_path = Path(os.environ.get("ARTICLE_PUBLICATION_STATE", ""))
    if not state_path.is_file():
        raise XPostRefused("ARTICLE_PUBLICATION_STATE is required")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    run_id = str(state.get("run_id", ""))
    pair = state.get("pairs", {}).get("x-post/ja", {})
    if pair.get("target") != run_id:
        raise XPostRefused("X Post stable slot does not match run identity")
    artifact = Path(str(state.get("x_post", {}).get("path", "")))
    text = artifact.read_text(encoding="utf-8").strip()
    validate_text(text)
    slots_path = Path(str(state.get("ledger_path", ""))).parent / "x-post-slots.json"
    slots = json.loads(slots_path.read_text(encoding="utf-8"))
    date_jst = assigned_date(slots, run_id)
    if datetime.now(JST).date().isoformat() != date_jst:
        raise XPostRefused("X Post assigned slot is not the current JST date")
    fence_path = Path(str(state["run_dir"])) / "gates/x-post-effect.json"
    existing = reconcile_existing_effect(fence_path)
    if existing is not None:
        return existing
    decision = _guard("preflight")
    if decision.get("action") == "skip-live":
        return decision
    account_url = _account_url(state)

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    page = None
    try:
        browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
        page = browser.contexts[0].new_page()
        page.goto(account_url, wait_until="domcontentloaded", timeout=50000)
        page.wait_for_timeout(4000)
        if not _same_account(page.url, account_url):
            raise XPostRefused("authenticated X timeline identity does not match")
        if not _authenticated_profile_matches(page, account_url):
            raise XPostRefused("authenticated X profile identity does not match persisted destination")
        rows = _timeline_rows(page)
        ids = [
            status
            for row in rows
            if (status := _status_id(str(row.get("status_href", ""))))
        ]
        if not ids:
            raise XPostRefused("X pre-effect timeline is empty")
        page.goto(
            "https://x.com/compose/post",
            wait_until="domcontentloaded",
            timeout=50000,
        )
        # The composer mounts after navigation; counting immediately raced it
        # and refused a perfectly available editor twice on 2026-07-26 (a
        # manual check moments later found exactly one).
        try:
            page.wait_for_selector(
                '[role="dialog"] [data-testid="tweetTextarea_0"]',
                state="visible",
                timeout=30000,
            )
        except Exception:
            pass
        editor = page.locator(
            '[role="dialog"]:visible [data-testid="tweetTextarea_0"]'
        )
        if editor.count() != 1:
            raise XPostRefused("X Post editor is unavailable")
        editor.fill(text)
        responses: list[Any] = []
        page.on(
            "response",
            lambda response: responses.append(response)
            if "CreateTweet" in response.url
            else None,
        )
        fence = effect_fence(
            run_id,
            date_jst,
            ids,
            datetime.now(timezone.utc).isoformat(),
        )
        _atomic_write(fence_path, fence)
        button = page.locator(
            '[role="dialog"]:visible [data-testid="tweetButton"]'
        )
        if button.count() != 1:
            fence["phase"] = "confirmed-no-effect"
            _atomic_write(fence_path, fence)
            raise XPostRefused("X Post button is unavailable")
        button.evaluate("(node) => node.click()")
        page.wait_for_timeout(6000)
        status_id = _created_status_id(responses)
        if status_id:
            fence.update(
                phase="target-known",
                target=f"{account_url}/status/{status_id}",
                status_id=status_id,
            )
            _atomic_write(fence_path, fence)
        result = _guard("reconcile")
        if result.get("action") != "skip-live":
            raise XPostRefused("X Post timeline readback did not become live")
        return result
    finally:
        if page is not None:
            page.close()
        playwright.stop()


def main() -> int:
    if sys.argv[1:] != ["go"]:
        print("usage: publish.py go", file=sys.stderr)
        return 2
    # Fail-closed PII gate at the publish boundary: scan the frozen artifacts this target is
    # about to make public. An unset ARTICLE_RUN_DIR is itself a refusal.
    gate_run_dir("x-post-go", os.environ.get("ARTICLE_RUN_DIR", ""), pair="x-post/ja")
    try:
        result = publish()
    except (XPostRefused, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
