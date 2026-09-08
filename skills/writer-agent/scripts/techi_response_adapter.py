#!/usr/bin/env python3
"""TECHi-specific authenticated application-status adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from email.utils import getaddresses
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claim_store import _text, canonicalize_url


AUTHOR_API = "https://www.techi.com/api/account/author-application"
AUTHOR_PROGRAM = "https://www.techi.com/authors/apply"
VENV_CLOAK_PYTHON = Path(os.environ.get("WRITER_BROWSER_PYTHON", sys.executable))


class ResponseUnavailable(RuntimeError):
    pass


def matches(opportunity: dict[str, Any]) -> bool:
    program_url = str(opportunity.get("official_program_url") or "").strip()
    return (
        opportunity.get("state") == "SUBMITTED"
        and opportunity.get("publisher") == "TECHi Author Program"
        and program_url in {AUTHOR_PROGRAM, AUTHOR_PROGRAM + "/"}
        and bool(str(opportunity.get("submission_id") or "").strip())
    )


def parse_envelope(
    opportunity: dict[str, Any], envelope: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(envelope, dict) or envelope.get("http_status") != 200:
        raise ResponseUnavailable("TECHi author application endpoint is unavailable")
    body = envelope.get("body")
    if not isinstance(body, dict):
        raise ResponseUnavailable("TECHi author application receipt is invalid")
    if "application" not in body:
        raise ResponseUnavailable("TECHi author application receipt is missing application")
    application = body["application"]
    if application is None:
        return None
    if not isinstance(application, dict):
        raise ResponseUnavailable("TECHi author application receipt is invalid")
    submission_id = _text(opportunity.get("submission_id"), "submission_id")
    provider_id = str(application.get("id") or "").strip()
    provider_status = str(application.get("status") or "").strip().lower()
    if provider_id != submission_id or not provider_status:
        raise ResponseUnavailable("TECHi application identity/status differs from submission")
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "provider": "techi-author", "provider_status": provider_status,
        "submission_id": provider_id, "url": AUTHOR_API,
        "retrieved_sha256": hashlib.sha256(canonical).hexdigest(),
        "excerpt": f"Author application {provider_id} is {provider_status}.",
        "payload": body,
    }


def _authenticated_techi(message: dict[str, Any]) -> bool:
    raw = str(message.get("gmail_authentication_results") or "").strip()
    headers = [
        line.strip() for line in raw.splitlines()
        if line.strip().lower().startswith("mx.google.com;")
    ]
    if len(headers) != 1:
        return False
    clauses = [clause.strip().lower() for clause in headers[0].split(";")]
    if not clauses or clauses[0] != "mx.google.com":
        return False

    requirements = {
        "dkim": r"(?:^|\s)header\.i=@techi\.com(?:\s|$)",
        "spf": r"(?:^|\s)smtp\.mailfrom=info@techi\.com(?:\s|$)",
        "dmarc": r"(?:^|\s)header\.from=techi\.com(?:\s|$)",
    }
    for method, aligned_property in requirements.items():
        matching = [
            clause for clause in clauses[1:]
            if re.match(rf"^{method}=pass(?:\s|$)", clause)
        ]
        if len(matching) != 1 or not re.search(aligned_property, matching[0]):
            return False
    return True


def correlates(opportunity: dict[str, Any], message: dict[str, Any]) -> bool:
    if not matches(opportunity):
        return False
    exact_evidence = str(opportunity.get("submission_evidence_id") or "").strip()
    exact_confirmation = str(
        opportunity.get("confirmation_gmail_message_id") or ""
    ).strip()
    if not exact_evidence or not exact_confirmation:
        return False
    if str(message.get("submission_evidence_id") or "").strip() != exact_evidence:
        return False
    if str(
        message.get("submission_confirmation_gmail_message_id") or ""
    ).strip() != exact_confirmation:
        return False
    if not _authenticated_techi(message):
        return False
    confirmation = str(
        message.get("submission_confirmation_rfc_message_id") or ""
    ).strip()
    reply_chain = " ".join((
        str(message.get("in_reply_to") or ""),
        str(message.get("references") or ""),
    ))
    if confirmation and confirmation in reply_chain.split():
        return True
    confirmation_recipients = {
        address.lower()
        for _name, address in getaddresses([
            str(message.get("submission_confirmation_recipient") or "")
        ])
        if address
    }
    if not confirmation_recipients:
        return False
    confirmation_recipient = str(
        message.get("submission_confirmation_recipient") or ""
    ).strip()
    recipients = {
        address.lower()
        for _name, address in getaddresses([str(message.get("to") or "")])
        if address
    }
    subject = str(message.get("subject") or "").strip().casefold()
    observed_raw = str(
        message.get("submission_confirmation_observed_at") or ""
    ).strip()
    try:
        observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
        received = datetime.fromtimestamp(
            int(str(message.get("gmail_internal_date") or "")) / 1000,
            tz=timezone.utc,
        )
    except (TypeError, ValueError):
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return bool(
        confirmation_recipient
        and bool(confirmation_recipients & recipients)
        and subject.startswith("re: [author apply]")
        and received >= observed
    )


def poll(opportunity: dict[str, Any]) -> dict[str, Any] | None:
    """Read the authenticated status for the exact recorded provider ID."""
    if not matches(opportunity):
        return None
    submission_id = _text(opportunity.get("submission_id"), "submission_id")
    if not VENV_CLOAK_PYTHON.is_file():
        raise ResponseUnavailable("Writer browser Python is unavailable for TECHi status")
    cdp_url = os.environ.get("WRITER_CDP_URL", "http://127.0.0.1:9222")
    script = f'''
import base64, json, sys
from playwright.sync_api import sync_playwright
p = sync_playwright().start()
pg = None
try:
    try:
        browser = p.chromium.connect_over_cdp({cdp_url!r})
    except Exception as error:
        print("CDP_UNREACHABLE:" + str(error)); sys.exit(1)
    if not browser.contexts:
        print("CDP_UNREACHABLE:no browser context"); sys.exit(1)
    pg = browser.contexts[0].new_page()
    pg.goto("https://www.techi.com/authors/apply/", wait_until="domcontentloaded", timeout=40000)
    result = pg.evaluate("""async () => {{
        const response = await fetch('/api/account/author-application', {{
            credentials: 'same-origin', cache: 'no-store'
        }});
        let body = null;
        try {{ body = await response.json(); }} catch (_) {{}}
        return {{http_status: response.status, body}};
    }}""")
    encoded = base64.b64encode(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).decode("ascii")
    print("TECHI_STATUS_B64:" + encoded)
finally:
    if pg is not None:
        pg.close()
    p.stop()
'''
    try:
        completed = subprocess.run(
            [str(VENV_CLOAK_PYTHON), "-c", script], capture_output=True, text=True,
            timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ResponseUnavailable(type(error).__name__) from error
    encoded = next(
        (line.removeprefix("TECHI_STATUS_B64:") for line in completed.stdout.splitlines()
         if line.startswith("TECHI_STATUS_B64:")), None,
    )
    if completed.returncode != 0 or encoded is None:
        reason = next(
            (line for line in completed.stdout.splitlines() if line.startswith("CDP_UNREACHABLE:")),
            completed.stderr.strip()[-300:] or "TECHi status driver returned no receipt",
        )
        raise ResponseUnavailable(reason)
    try:
        envelope = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as error:
        raise ResponseUnavailable("TECHi status receipt is unreadable") from error
    return parse_envelope(opportunity, envelope)


def normalize(opportunity: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    if not matches(opportunity):
        raise ResponseUnavailable("opportunity is not the explicit TECHi application")
    if not isinstance(raw, dict):
        raise ResponseUnavailable("provider status is not an object")
    provider = _text(raw.get("provider"), "provider")
    submission_id = _text(raw.get("submission_id"), "submission_id")
    if submission_id != str(opportunity.get("submission_id") or "").strip():
        raise ResponseUnavailable("provider status submission identity mismatch")
    raw_url = _text(raw.get("url"), "url")
    if raw_url != AUTHOR_API:
        raise ResponseUnavailable("provider status URL is outside the official publisher")
    try:
        url = canonicalize_url(raw_url, "rss")
    except ValueError as error:
        raise ResponseUnavailable("provider status URL is invalid") from error
    digest = _text(raw.get("retrieved_sha256"), "retrieved_sha256").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ResponseUnavailable("provider status receipt hash is invalid")
    excerpt = _text(raw.get("excerpt"), "excerpt")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise ResponseUnavailable("provider status payload is invalid")
    status = _text(raw.get("provider_status"), "provider_status").lower()
    classification = {
        "pending": "PENDING", "approved": "ACCEPTED", "accepted": "ACCEPTED",
        "rejected": "DECLINED", "declined": "DECLINED",
    }.get(status)
    if classification is None:
        raise ResponseUnavailable(f"unsupported provider status: {status}")
    normalized = {
        "provider": provider, "provider_status": status, "classification": classification,
        "submission_id": submission_id, "url": url, "retrieved_sha256": digest,
        "excerpt": excerpt, "payload": payload,
    }
    application_id = str(opportunity.get("application_id") or "").strip()
    durable_provider_id = str(opportunity.get("provider_submission_id") or "").strip()
    if application_id and durable_provider_id != submission_id:
        raise ResponseUnavailable("durable Application provider identity mismatch")
    provider_submission_id = durable_provider_id or submission_id
    if application_id:
        normalized["transition_payload"] = {
            "application_id": application_id,
            "provider_submission_id": provider_submission_id,
        }
    return normalized
