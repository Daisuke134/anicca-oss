#!/usr/bin/env python3
"""Read-only, bounded discovery of public Lancers projects.

The command intentionally has no login, cookie, browser, or persistence path.
It emits a deterministic JSON envelope so provider blocks can be measured rather
than mistaken for an empty market.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from html.parser import HTMLParser
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, TextIO
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


_SCRIPT_DIR = Path(__file__).resolve().parent
_ENDPOINT = "https://www.lancers.jp/work/search"
_USER_AGENT = "anicca-lancers-public-discovery/1.0"
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_QUERY_LENGTH = 200
_MISSING = object()
_DETAIL_RE = re.compile(r"^/work/detail/([1-9][0-9]{0,511})$")
_CLIENT_RE = re.compile(r"^/client/([A-Za-z0-9_.-]{1,128})$")
_ADAPTER_MODULE_NAME = "_anicca_lancers_adapter_status_v1"


class LancersProviderError(RuntimeError):
    """Stable provider failure with no URL, body, or credential detail."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise LancersProviderError(code) from None


def _clean_text(value: str) -> str:
    value = value.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


class _SearchParser(HTMLParser):
    """Extract the stable card fields observed on the public search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._card: Optional[Dict[str, Any]] = None
        self._card_depth = 0
        self._capture: Optional[tuple[str, int, List[str]]] = None
        self._price_depth: Optional[int] = None
        self._title_tag_depth: Optional[int] = None

    @staticmethod
    def _attrs(attrs: Sequence[tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {name: value or "" for name, value in attrs}

    @staticmethod
    def _classes(attrs: Sequence[tuple[str, Optional[str]]]) -> set[str]:
        value = next((value for name, value in attrs if name == "class"), "") or ""
        return set(value.split())

    def _begin_capture(self, kind: str) -> None:
        # A card's title/description/category fields do not nest each other in
        # the current markup; keeping one active field also avoids collecting
        # hidden script text into buyer-facing descriptions.
        if self._capture is None:
            self._capture = (kind, self._depth, [])

    def _finish_capture(self) -> None:
        if self._capture is None or self._card is None:
            return
        kind, _start, chunks = self._capture
        text = _clean_text("".join(chunks))
        if kind == "title":
            # The current title anchor contains a nested tag list before the
            # actual title. Remove only the provider's known leading badges;
            # Japanese title text itself remains byte-for-byte intact.
            badge_lines = {
                "PR",
                "NEW",
                "2回目",
                "限定公開",
                "急募",
                "注目",
                "当選保証",
                "提案非公開",
            }
            title_lines = text.split("\n")
            while title_lines and title_lines[0].strip() in badge_lines:
                title_lines.pop(0)
            text = _clean_text("\n".join(title_lines))
            self._card["title"] = text
        elif kind == "description":
            self._card["description"] = text
        elif kind == "category":
            if text:
                self._card.setdefault("categories", []).append(text)
        elif kind == "buyer":
            self._card.setdefault("buyer_external_id", text)
        elif kind == "type":
            self._card["budget_type"] = text
        elif kind == "price_number" and text:
            self._card.setdefault("price_numbers", []).append(text)
        self._capture = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self._depth += 1
        classes = self._classes(attrs)
        attributes = self._attrs(attrs)
        if self._card is None and tag == "div" and "p-search-job-media" in classes:
            self._card = {"categories": [], "price_numbers": []}
            self._card_depth = self._depth
        if self._card is None:
            return

        if tag == "a":
            href = attributes.get("href", "")
            match = _DETAIL_RE.fullmatch(href)
            if match:
                self._card["id"] = match.group(1)
                self._card["url"] = "https://www.lancers.jp" + href
            if "p-search-job-media__title" in classes:
                self._begin_capture("title")
            elif (client := _CLIENT_RE.fullmatch(href)) is not None:
                self._card["buyer_external_id"] = client.group(1)
                self._begin_capture("buyer")
        if "js-job-show-description" in classes:
            self._begin_capture("description")
        elif "p-search-job__division" in classes:
            self._begin_capture("category")
        elif "c-badge__text" in classes:
            self._begin_capture("type")
        if "p-search-job-media__price" in classes:
            self._price_depth = self._depth
        elif self._price_depth is not None and "p-search-job-media__number" in classes:
            self._begin_capture("price_number")
        if (
            self._capture is not None
            and self._capture[0] == "title"
            and "p-search-job-media__tags" in classes
        ):
            self._title_tag_depth = self._depth

    def handle_endtag(self, tag: str) -> None:
        if self._card is not None:
            if self._title_tag_depth == self._depth:
                self._title_tag_depth = None
            if self._capture is not None and self._depth == self._capture[1]:
                self._finish_capture()
            if self._price_depth == self._depth:
                self._price_depth = None
            if self._depth == self._card_depth:
                card = self._card
                self._card = None
                self._card_depth = 0
                self._finish_card(card)
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture is not None and self._title_tag_depth is None:
            self._capture[2].append(data)

    def finish(self) -> None:
        """Flush a card when a provider fragment omits its closing wrapper."""

        if self._card is None:
            return
        if self._capture is not None:
            self._finish_capture()
        card = self._card
        self._card = None
        self._card_depth = 0
        self._finish_card(card)

    def _finish_card(self, card: Dict[str, Any]) -> None:
        # Store card objects only when the provider supplied a stable detail
        # link and title. Missing fields are left for the adapter to reject.
        card["categories"] = card.get("categories", [])
        card["price_numbers"] = card.get("price_numbers", [])
        self.cards.append(card)

    cards: List[Dict[str, Any]] = []


def _card_budget(card: Mapping[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    numbers = [str(value).replace(",", "").strip() for value in card.get("price_numbers", [])]
    numbers = [value for value in numbers if value]
    if not numbers:
        return None, None, None
    minimum = numbers[0]
    maximum = numbers[1] if len(numbers) > 1 else numbers[0]
    return minimum, maximum, "JPY"


def _public_budget_type(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    if normalized in {"プロジェクト", "固定", "fixed", "project"}:
        return "fixed"
    if normalized in {"タスク", "bounty"}:
        return "bounty"
    # Measured 2026-09-07 across three Lancers category pages: 96 recruit badges against 52
    # projects and 32 competitions. 求人 is the most common listing there and it was falling
    # through to "unknown", so every one reached the planner and came back
    # mandatory_human_presence -- a hiring post is an application for employment, with interviews,
    # not a proposal for a piece of work.
    if normalized in {"求人", "recruit"}:
        return "recruit"
    if normalized in {"コンペ", "contest", "competition"}:
        return "contest"
    if normalized in {"時間報酬", "時間単価", "hourly", "time"}:
        return "hourly"
    return "unknown"


def parse_search_html(html: str) -> List[Dict[str, object]]:
    """Parse public cards without executing scripts or retaining raw HTML."""

    if not isinstance(html, str):
        _fail("lancers_response_invalid")
    parser = _SearchParser()
    parser.cards = []
    # Lancers currently emits a long page with a few malformed/conditional
    # wrappers. Split at the stable card-root class so one malformed card does
    # not hide every later public opportunity.
    root_re = re.compile(
        r'<div\b[^>]*\bclass=["\'][^"\']*\bp-search-job-media\b[^"\']*["\'][^>]*>',
        re.IGNORECASE,
    )
    roots = list(root_re.finditer(html))
    fragments = (
        [html[m.start() : (roots[index + 1].start() if index + 1 < len(roots) else len(html))]
         for index, m in enumerate(roots)]
        if roots
        else [html]
    )
    try:
        for fragment in fragments:
            fragment_parser = _SearchParser()
            fragment_parser.cards = []
            fragment_parser.feed(fragment)
            fragment_parser.close()
            fragment_parser.finish()
            parser.cards.extend(fragment_parser.cards)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        _fail("lancers_response_invalid")
    cards: List[Dict[str, object]] = []
    for card in parser.cards:
        minimum, maximum, currency = _card_budget(card)
        categories = card.get("categories", [])
        category = categories[0] if categories else "unknown"
        values: Dict[str, object] = {
            "id": card.get("id"),
            "title": card.get("title", ""),
            "description": card.get("description", ""),
            "url": card.get("url", ""),
            "category": category,
            "budget_type": _public_budget_type(card.get("budget_type", "unknown")),
            "budget_min": minimum,
            "budget_max": maximum,
            "currency": currency,
            "buyer_external_id": card.get("buyer_external_id"),
        }
        cards.append(values)
    return cards


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = self._dl_depth = self._proposal_div_depth = 0; self._label = self._capture = None; self._proposal_chunks: list[str] = []; self.values: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        classes = set((dict(attrs).get("class") or "").split()); self._depth += 1
        if self._proposal_div_depth and tag == "div": self._proposal_div_depth += 1
        elif tag == "div" and "tableSummary__col--worksNum" in classes: self._proposal_div_depth = 1; self._proposal_chunks = []
        if tag == "dl": self._dl_depth = self._depth
        elif self._dl_depth and tag in {"dt", "dd"}: self._capture = (tag, self._depth, [])
        elif tag == "br" and self._capture: self._capture[2].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None and self._capture[1] == self._depth:
            kind, _depth, chunks = self._capture; text = _clean_text("".join(chunks))
            if kind == "dt": self._label = text if text and len(text) <= 120 else None
            elif text and self._label is not None: self.values[self._label] = text; self._label = None
            self._capture = None
        if tag == "div" and self._proposal_div_depth:
            self._proposal_div_depth -= 1
            if not self._proposal_div_depth:
                match = re.search(r"(?:^|\s)提案数\s*([0-9][0-9,]*)件(?:\s|$)", _clean_text("".join(self._proposal_chunks)))
                if match: self.values["提案数"] = f"{int(match.group(1).replace(',', ''))}件"
        if tag == "dl" and self._dl_depth == self._depth: self._dl_depth = 0
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture: self._capture[2].append(data)
        if self._proposal_div_depth: self._proposal_chunks.append(data)


def parse_detail_html(html: str) -> Dict[str, str]:
    if not isinstance(html, str):
        _fail("lancers_response_invalid")
    parser = _DetailParser()
    try:
        parser.feed(html)
        parser.close()
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        _fail("lancers_response_invalid")
    return parser.values


class _ClientProfileParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.capture_depth: Optional[int] = None
        self.chunks: List[str] = []
        self.order_rate: Optional[int] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.depth += 1
        classes = _SearchParser._classes(attrs)
        if tag == "div" and "c-table-summary__col-item" in classes:
            self.capture_depth, self.chunks = self.depth, []

    def handle_data(self, data: str) -> None:
        if self.capture_depth is not None:
            self.chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.capture_depth == self.depth:
            text = _clean_text("".join(self.chunks))
            match = re.search(r"(?:^|\n)発注率\s*\n?\s*([0-9]{1,3})\s*%", text)
            if match and 0 <= int(match.group(1)) <= 100:
                self.order_rate = int(match.group(1))
            self.capture_depth, self.chunks = None, []
        self.depth = max(0, self.depth - 1)


def parse_client_order_rate(html: str) -> Optional[int]:
    if not isinstance(html, str):
        _fail("lancers_response_invalid")
    parser = _ClientProfileParser()
    parser.feed(html)
    parser.close()
    return parser.order_rate


def _query(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail("lancers_invalid_argument")
    if any(unicodedata.category(character) == "Cc" for character in value):
        _fail("lancers_invalid_argument")
    normalized = value.strip()
    if len(normalized) > _MAX_QUERY_LENGTH:
        _fail("lancers_invalid_argument")
    return normalized or None


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        _fail("lancers_invalid_argument")
    return value


def _timeout(value: object) -> float:
    if type(value) not in (int, float):
        _fail("lancers_invalid_argument")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 60:
        _fail("lancers_invalid_argument")
    return parsed


def _canonical_detail_url(value: object) -> str:
    if not isinstance(value, str): _fail("lancers_invalid_argument")
    parsed, origin = urllib.parse.urlsplit(value.strip()), urllib.parse.urlsplit(_ENDPOINT)
    if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc) or parsed.query or parsed.fragment or parsed.username or parsed.password or _DETAIL_RE.fullmatch(parsed.path) is None: _fail("lancers_invalid_argument")
    return f"{origin.scheme}://{origin.netloc}{parsed.path}"


def fetch_public_html(*, query: Optional[str], limit: int, timeout: float, _detail_url: Optional[str] = None, _client_id: Optional[str] = None) -> str:
    """Fetch one bounded public page with no cookies or auth headers."""

    query = _query(query)
    limit = _limit(limit)
    timeout = _timeout(timeout)
    # type[]=project asks Lancers for proposals only. Measured 2026-09-07 on one keyword page:
    # without it, 30 recruit badges against 27 projects; with it, 0 recruit and 30 projects. The
    # 求人 postings were most of every page, they are applications for employment rather than for
    # a piece of work, and they were being fetched, normalised, filtered and counted before being
    # thrown away. Same request, thirty usable rows instead of twenty-seven buried in sixty.
    params = [("open", "1"), ("sort", "started_at"), ("limit", str(limit)), ("type[]", "project")]
    if query is not None:
        params.append(("keyword", query))
    encoded = urllib.parse.urlencode(params, encoding="utf-8", errors="strict")
    if _detail_url is not None and _client_id is not None:
        _fail("lancers_invalid_argument")
    if _detail_url is not None:
        _detail_url = _canonical_detail_url(_detail_url)
    client_url = None
    if _client_id is not None:
        if not isinstance(_client_id, str) or _CLIENT_RE.fullmatch("/client/" + _client_id) is None:
            _fail("lancers_invalid_argument")
        client_url = "https://www.lancers.jp/client/" + _client_id
    url = _detail_url or client_url or _ENDPOINT + "?" + encoded
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            final_url = getattr(response, "geturl", lambda: url)()
            if isinstance(final_url, str) and "/user/login" in final_url:
                _fail("lancers_provider_blocked")
            if _detail_url is not None and _canonical_detail_url(final_url) != _detail_url:
                _fail("lancers_http_error")
            if client_url is not None and final_url != client_url:
                _fail("lancers_http_error")
            if status in {401, 403, 429}:
                _fail("lancers_provider_blocked")
            if not isinstance(status, int) or status < 200 or status >= 300:
                _fail("lancers_http_error")
            chunks: List[bytes] = []
            total = 0
            while total <= _MAX_BODY_BYTES:
                chunk = response.read(min(64 * 1024, _MAX_BODY_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    _fail("lancers_response_too_large")
            body = b"".join(chunks)
    except LancersProviderError:
        raise
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 429}:
            _fail("lancers_provider_blocked")
        _fail("lancers_http_error")
    except (urllib.error.URLError, TimeoutError, OSError):
        _fail("lancers_network_error")
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("lancers_response_invalid")
    return ""


def fetch_public_client_order_rate(buyer_external_id: object, timeout: float = 20.0) -> Optional[int]:
    if not isinstance(buyer_external_id, str):
        return None
    html = fetch_public_html(query=None, limit=1, timeout=timeout, _client_id=buyer_external_id)
    return parse_client_order_rate(html)


def _enrich_cards(cards: Sequence[Dict[str, object]], timeout: float) -> tuple[int, int]:
    enriched = failed = 0
    for card in cards:
        try:
            fields = parse_detail_html(fetch_public_html(query=None, limit=1, timeout=timeout, _detail_url=_canonical_detail_url(card.get("url"))))
            if not fields:
                raise ValueError
            text = "\n".join(f"{label}: {value}" for label, value in fields.items())
            if len(text) > 2000: text = text[:1390] + "\n[…]\n" + text[-600:]
            card["description"] = text
            enriched += 1
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            failed += 1
    return enriched, failed


def _load_adapter() -> Any:
    path = _SCRIPT_DIR / "lancers_adapter.py"
    spec = importlib.util.spec_from_file_location(_ADAPTER_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        _fail("lancers_dependency_unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_ADAPTER_MODULE_NAME, _MISSING)
    sys.modules[_ADAPTER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is _MISSING:
            sys.modules.pop(_ADAPTER_MODULE_NAME, None)
        else:
            sys.modules[_ADAPTER_MODULE_NAME] = previous
    return module


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_observed_at(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            _fail("lancers_internal_error")
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.endswith(("Z", "z")):
        _fail("lancers_internal_error")
    return value


def run_discovery(
    *,
    query: Optional[str],
    limit: int,
    timeout: float,
    fetcher: Optional[Callable[..., str]] = None,
    observed_at: Optional[Callable[[], object] | str] = None,
) -> Dict[str, object]:
    """Run one discovery observation and return a redacted result envelope."""

    try:
        query = _query(query)
        limit = _limit(limit)
        timeout = _timeout(timeout)
        acquire = fetch_public_html if fetcher is None else fetcher
        html = acquire(query=query, limit=limit, timeout=timeout)
        cards = parse_search_html(html)
        # Some public pages ignore the ``limit`` query parameter. Keep the
        # returned observation bounded even when the provider sends a larger
        # page than requested.
        cards = cards[:limit]
        enriched, failed = _enrich_cards(cards, timeout)
        observation = _now() if observed_at is None else observed_at() if callable(observed_at) else observed_at
        observation = _validate_observed_at(observation)
        adapter = _load_adapter()
        normalized, rejected = adapter.normalize_projects(cards, observed_at=observation)
        payload: Dict[str, object] = {
            "ok": bool(normalized),
            "platform": "lancers",
            "source": "public_html",
            "provider_count": len(cards),
            "normalized_count": len(normalized),
            "rejected_count": len(rejected),
            "detail_enriched_count": enriched,
            "detail_failed_count": failed,
            "opportunities": normalized,
        }
        if not normalized:
            payload["ok"] = False
            payload["error"] = "no_normalized_opportunities"
        return payload
    except LancersProviderError as error:
        return {
            "ok": False,
            "platform": "lancers",
            "source": "public_html",
            "error": error.code if error.code.startswith("lancers_") else "lancers_internal_error",
        }
    except Exception:
        return {
            "ok": False,
            "platform": "lancers",
            "source": "public_html",
            "error": "lancers_internal_error",
        }


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, output: TextIO, errors: TextIO, **kwargs: Any) -> None:
        self._output = output
        self._errors = errors
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def _print_message(self, message: str, file: Optional[TextIO] = None) -> None:
        del file
        if message:
            self._errors.write("invalid_argument\n")


def _parser(stdout: TextIO, stderr: TextIO) -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__, output=stdout, errors=stderr)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=argparse.ArgumentParser
    )
    discovery = commands.add_parser("discovery", help="discover public projects")
    discovery.add_argument("--json", action="store_true", required=True)
    discovery.add_argument("--limit", type=int, default=20)
    discovery.add_argument("--query", default=None)
    discovery.add_argument("--timeout", type=float, default=20.0)
    return parser


def _write_json(stream: TextIO, value: Mapping[str, object]) -> None:
    stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        args = _parser(out, err).parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    if args.command != "discovery" or not args.json:
        _write_json(err, {"ok": False, "platform": "lancers", "source": "public_html", "error": "lancers_invalid_argument"})
        return 2
    payload = run_discovery(query=args.query, limit=args.limit, timeout=args.timeout)
    _write_json(out, payload)
    if not payload.get("ok"):
        error = payload.get("error", "lancers_discovery_failed")
        err.write(str(error) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
