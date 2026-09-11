"""CrowdWorks-local Google Form transport with a durable one-POST fence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_google_form_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "https" and (
        parsed.netloc == "forms.gle" and bool(parsed.path.strip("/"))
        or parsed.netloc == "docs.google.com"
        and re.fullmatch(r"/forms/d/e/[^/]+/viewform", parsed.path) is not None
    )


def receipt_path(state_root: Path, url_sha256: str) -> Path:
    return state_root / "external-actions" / f"{url_sha256}.json"


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _identity(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(value), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _bound_index_path(state_root: Path, binding: Mapping[str, Any]) -> Path:
    return state_root / "external-actions" / f"index-{_identity(binding)}.json"


def bound_receipt(state_root: Path, binding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Read a Paid-only receipt by immutable contract/form binding, never by URL alone."""
    index_path = _bound_index_path(state_root, binding)
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise RuntimeError("google_form_receipt_invalid") from None
    key = index.get("receipt_key") if isinstance(index, Mapping) else None
    if not isinstance(key, str) or not key:
        raise RuntimeError("google_form_receipt_invalid")
    try:
        receipt = json.loads(receipt_path(state_root, key).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if index.get("status") == "prepared":
            raise RuntimeError("google_form_submission_uncertain")
        raise RuntimeError("google_form_receipt_invalid") from None
    except (OSError, ValueError):
        raise RuntimeError("google_form_receipt_invalid") from None
    if not isinstance(receipt, Mapping) or receipt.get("binding") != dict(binding):
        raise RuntimeError("google_form_receipt_invalid")
    return receipt


def submit_once(*, browser: Any, state_root: Path, url: str, url_sha256: str,
                answer_fields: Callable[[Any], list[tuple[str, str]]],
                binding: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if not is_google_form_url(url) or hashlib.sha256(url.encode()).hexdigest() != url_sha256:
        raise RuntimeError("google_form_intent_invalid")
    immutable_binding = dict(binding) if binding is not None else None
    if immutable_binding is not None:
        prior = bound_receipt(state_root, immutable_binding)
        if prior is not None:
            if prior.get("confirmation_sha256"):
                return prior
            raise RuntimeError("google_form_submission_uncertain")
    else:
        path = receipt_path(state_root, url_sha256)
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(receipt, Mapping) and receipt.get("url_sha256") == url_sha256 and receipt.get("confirmation_sha256"):
                return receipt
            if isinstance(receipt, Mapping) and receipt.get("status") == "prepared":
                raise RuntimeError("google_form_submission_uncertain")
            raise RuntimeError("google_form_receipt_invalid")
    form = browser.contexts[0].new_page()
    try:
        form.goto(url, wait_until="domcontentloaded", timeout=20_000)
        form.wait_for_timeout(3_000)
        route = urlsplit(str(form.url))
        if route.scheme != "https" or route.netloc != "docs.google.com":
            raise RuntimeError("google_form_route_invalid")
        fields = answer_fields(form)
        if not isinstance(fields, list) or not all(isinstance(name, str) and isinstance(value, str)
                                                   for name, value in fields):
            raise RuntimeError("google_form_answer_invalid")
        action = str(form.locator("form").get_attribute("action") or "")
        action_route = urlsplit(action)
        if ((action_route.scheme, action_route.netloc) != ("https", "docs.google.com")
                or re.fullmatch(r"/forms/d/e/[^/]+/formResponse", action_route.path) is None):
            raise RuntimeError("google_form_action_invalid")
        for name in ("fvv", "draftResponse", "pageHistory", "fbzx"):
            locator = form.locator(f'input[name="{name}"]')
            if locator.count() == 1:
                fields.append((name, str(locator.input_value())))
        payload_sha256 = hashlib.sha256(urlencode(fields).encode()).hexdigest()
        if immutable_binding is not None:
            receipt_key = _identity({**immutable_binding, "submission_payload_sha256": payload_sha256})
            path = receipt_path(state_root, receipt_key)
            write_json(_bound_index_path(state_root, immutable_binding), {
                "version": 1, "status": "prepared", "receipt_key": receipt_key,
                "submission_payload_sha256": payload_sha256,
            })
        else:
            path = receipt_path(state_root, url_sha256)
            receipt_key = url_sha256
        prepared = {"version": 1, "status": "prepared", "url_sha256": url_sha256,
                    "submission_payload_sha256": payload_sha256, "prepared_at": now()}
        if immutable_binding is not None:
            prepared["binding"] = immutable_binding
        write_json(path, prepared)
        response = browser.contexts[0].request.post(action, data=urlencode(fields), headers={
            "Content-Type": "application/x-www-form-urlencoded", "Referer": form.url,
        }, timeout=30_000)
        body = response.text()
        if response.status != 200 or not any(marker in body for marker in (
                "回答を記録しました", "Your response has been recorded")):
            raise RuntimeError("google_form_submission_unverified")
        receipt = {**prepared, "status": "confirmed",
                   "confirmation_sha256": hashlib.sha256(body.encode()).hexdigest(), "observed_at": now()}
        write_json(path, receipt)
        if immutable_binding is not None:
            write_json(_bound_index_path(state_root, immutable_binding), {
                "version": 1, "status": "confirmed", "receipt_key": receipt_key,
                "submission_payload_sha256": payload_sha256,
            })
        return receipt
    finally:
        form.close()
