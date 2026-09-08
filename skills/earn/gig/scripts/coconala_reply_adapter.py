#!/usr/bin/env python3
"""Thin Coconala adapter for the shared marketplace Reply kernel."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]


def _load(name: str):
    path = HERE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"coconala_reply_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


snapshot = _load("coconala_queue_snapshot")
reply_browser = _load("coconala_reply_browser")
requested_estimate = _load("requested_estimate")


def _load_shared(name: str):
    path = REPO_ROOT / "skills/_shared/marketplace-core/scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"coconala_shared_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reply_planner = _load_shared("reply_planner")
reply_grounding = _load_shared("reply_grounding")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_id(context: Mapping[str, Any]) -> str:
    conversation = context.get("conversation")
    if not isinstance(conversation, list) or not conversation:
        raise RuntimeError("coconala_conversation_empty")
    latest = conversation[-1]
    if not isinstance(latest, Mapping):
        raise RuntimeError("coconala_latest_event_invalid")
    value = str(latest.get("message_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise RuntimeError("coconala_latest_event_invalid")
    return value


class CoconalaReplyAdapter:
    def __init__(
        self,
        *,
        state_root: Path,
        inventory_reader: Callable[[], list[dict[str, Any]]] | None = None,
        thread_reader: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] | None = None,
        sender: Callable[[str, str, str], dict[str, str]] | None = None,
        cdp_helper: Path | None = None,
        estimate_composer: Any = None,
        estimate_browser_factory: Any = None,
    ):
        self.state_root = Path(state_root)
        self.cdp_helper = cdp_helper or (
            REPO_ROOT / "skills/browser/scripts/cdp_default_tab.py"
        )
        self._inventory_reader = inventory_reader or self._read_inventory
        self._thread_reader = thread_reader or self._read_thread
        self._sender = sender or self._send
        self.estimate_composer = estimate_composer
        self.estimate_browser_factory = (
            estimate_browser_factory or requested_estimate._default_browser_factory
        )
        self._contexts: dict[str, dict[str, Any]] = {}
        self._raw_threads: dict[str, dict[str, Any]] = {}
        self._receipts: dict[str, dict[str, str]] = {}

    def _read_inventory(self) -> list[dict[str, Any]]:
        dom = snapshot.inspect_page_with_retry(
            self.cdp_helper, snapshot.MESSAGES_URL,
            snapshot.MESSAGES_EXPRESSION, None, hidden=True,
        )
        snapshot.validate_inbox_coverage(dom)
        return snapshot.inquiries_from_dom(dom)

    def _read_thread(self, thread_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        url = f"https://coconala.com/mypage/direct_message/{thread_id}"
        with reply_browser.CoconalaCdpReplyBrowser(
            self.cdp_helper, url, hidden=True, background=False,
        ) as browser:
            result = browser.read_before()
            if not isinstance(browser.raw, dict):
                raise RuntimeError("coconala_thread_dom_missing")
            self._raw_threads[thread_id] = browser.raw
            return result

    def _send(self, thread_id: str, body: str, expected_event: str) -> dict[str, str]:
        url = f"https://coconala.com/mypage/direct_message/{thread_id}"
        with reply_browser.CoconalaCdpReplyBrowser(
            self.cdp_helper, url, hidden=True, background=False,
        ) as browser:
            context, _before = browser.read_before()
            if _event_id(context) != expected_event:
                raise RuntimeError("coconala_thread_changed")
            browser.fill(body)
            browser.click()
            after = browser.read_after()
            if after.get("status") == "read_failed":
                raise RuntimeError("coconala_reply_reconcile_unknown")
            final_context, _bounded = browser._read()
        wanted = reply_browser.outgoing_sha256(body)
        for row in reversed(final_context.get("conversation") or []):
            if not isinstance(row, Mapping) or row.get("side") != "seller":
                continue
            if reply_browser.outgoing_sha256(str(row.get("body") or "")) == wanted:
                return {
                    "provider_receipt_id": str(row["message_id"]),
                    "observed_at": _now(),
                }
        raise RuntimeError("coconala_reply_reconcile_unknown")

    def _observation(self, thread_id: str) -> dict[str, str]:
        context, _bounded = self._thread_reader(thread_id)
        self._contexts[thread_id] = context
        return {
            "provider": "coconala",
            "account_id": "default",
            "thread_id": thread_id,
            "latest_event_id": _event_id(context),
            "observed_at": _now(),
        }

    def observe_threads(self) -> list[dict[str, str]]:
        rows = []
        for item in self._inventory_reader():
            thread_id = str(item.get("talkroom_id") or "").strip()
            latest = str(item.get("last_message_identity_sha256") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
                raise RuntimeError("coconala_thread_identity_invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", latest):
                raise RuntimeError("coconala_event_identity_invalid")
            rows.append({
                "provider": "coconala", "account_id": "default",
                "thread_id": thread_id, "latest_event_id": latest,
                "observed_at": _now(),
            })
        return rows

    def observe_one(self, thread_id: str) -> dict[str, str]:
        return self._observation(thread_id)

    def context(self, thread_id: str) -> dict[str, Any]:
        if thread_id not in self._contexts:
            self._observation(thread_id)
        context = dict(self._contexts[thread_id])
        conversation = context.get("conversation")
        if not isinstance(conversation, list):
            raise RuntimeError("coconala_conversation_invalid")
        normalized = []
        for row in conversation:
            if not isinstance(row, Mapping) or row.get("side") not in {"buyer", "seller"}:
                raise RuntimeError("coconala_conversation_invalid")
            normalized.append({**dict(row), "role": row["side"]})
        context["conversation"] = normalized
        context["thread_id"] = thread_id
        context["decision_required"] = True
        return context

    def semantic_dom(self, thread_id: str) -> dict[str, Any]:
        if thread_id not in self._raw_threads:
            self._observation(thread_id)
        return self._raw_threads[thread_id]

    def official_application_context(self, thread_id: str) -> dict[str, Any] | None:
        url = f"https://coconala.com/mypage/direct_message/{thread_id}"
        with reply_browser.CoconalaCdpReplyBrowser(
            self.cdp_helper, url, hidden=True, background=False,
        ) as browser:
            context, _bounded = browser._read()
            applications = browser._find_verified_applications(
                context["counterparty_user_id"], context.get("_own_user_path"),
            )
        if not applications:
            return None
        return (
            {"application": applications[0]}
            if len(applications) == 1
            else {"applications": applications}
        )

    def mutate(self, intent: dict[str, Any]) -> None:
        if intent.get("action") == "estimate":
            self._send_estimate(intent)
            return
        if intent.get("action") != "reply":
            raise RuntimeError("coconala_reply_action_unsupported")
        body = intent.get("payload", {}).get("body")
        if not isinstance(body, str) or not body.strip():
            raise RuntimeError("coconala_reply_body_invalid")
        self._receipts[intent["effect_key"]] = self._sender(
            intent["thread_id"], body.strip(), intent["latest_event_id"],
        )

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        cached = self._receipts.get(intent["effect_key"])
        if cached is not None:
            return {"verified": True, **cached}
        if intent.get("action") == "estimate":
            return self._readback_estimate(intent)
        body = intent.get("payload", {}).get("body")
        if not isinstance(body, str):
            return {"authoritative_absent": True}
        context, _bounded = self._thread_reader(intent["thread_id"])
        wanted = reply_browser.outgoing_sha256(body)
        for row in reversed(context.get("conversation") or []):
            if not isinstance(row, Mapping) or row.get("side") != "seller":
                continue
            if reply_browser.outgoing_sha256(str(row.get("body") or "")) == wanted:
                return {
                    "verified": True,
                    "provider_receipt_id": str(row["message_id"]),
                    "observed_at": _now(),
                }
        return {"authoritative_absent": True}

    @staticmethod
    def _semantic_terms(payload: Mapping[str, Any]) -> dict[str, Any]:
        fields = ("title", "content", "quantity", "price_jpy", "delivery_days", "purchase_plan")
        terms = {field: payload.get(field) for field in fields}
        if any(value is None for value in terms.values()):
            raise RuntimeError("coconala_estimate_terms_invalid")
        return terms

    def _estimate_observation(
        self, intent: Mapping[str, Any], terms: Mapping[str, Any], *, hidden: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = intent["payload"]
        thread_url = f"https://coconala.com/mypage/direct_message/{intent['thread_id']}"
        estimate_url = requested_estimate.sanitize_estimate_url(payload.get("_estimate_url"))
        if estimate_url is None:
            raise RuntimeError("coconala_estimate_url_invalid")
        with self.estimate_browser_factory(
            self.cdp_helper, thread_url, estimate_url, hidden,
        ) as browser:
            browser.semantic_context_required = True
            context, observation = browser.read_thread_context()
        expected = str(payload.get("_semantic_context_sha256") or "")
        if requested_estimate.semantic_context_sha256(context.get("conversation") or []) != expected:
            raise RuntimeError("coconala_estimate_context_changed")
        offer_date = date.fromisoformat(str(payload.get("_offer_date") or ""))
        materialized = requested_estimate.materialize_delivery_content(dict(terms), offer_date)
        outcome = requested_estimate.classify_delivery(
            pre_click_cards=[],
            post_click_cards=observation.get("structured_offers") or [],
            terms=materialized,
            click_started_at=None,
            today=offer_date,
            request_sent_at=payload.get("_request_sent_at"),
            own_user_path=observation.get("own_user_path"),
        )
        return observation, outcome

    def _readback_estimate(self, intent: dict[str, Any]) -> dict[str, Any]:
        try:
            terms = self._semantic_terms(intent["payload"])
            _observation, outcome = self._estimate_observation(intent, terms, hidden=True)
        except Exception:
            return {"authoritative_absent": False}
        cards = outcome.get("cards") if isinstance(outcome, Mapping) else None
        if outcome.get("status") not in {"verified", "already_delivered"} or not isinstance(cards, list) or len(cards) != 1:
            return {"authoritative_absent": outcome.get("status") == "not_required"}
        card = cards[0]
        receipt_id = str(card.get("offer_url") or "").strip()
        if not receipt_id:
            return {"authoritative_absent": False}
        return {"verified": True, "provider_receipt_id": receipt_id, "observed_at": _now()}

    def _send_estimate(self, intent: dict[str, Any]) -> None:
        if self.estimate_composer is None:
            raise RuntimeError("coconala_estimate_composer_unavailable")
        payload = intent["payload"]
        semantic_terms = self._semantic_terms(payload)
        thread_url = f"https://coconala.com/mypage/direct_message/{intent['thread_id']}"
        estimate_url = requested_estimate.sanitize_estimate_url(payload.get("_estimate_url"))
        if estimate_url is None:
            raise RuntimeError("coconala_estimate_url_invalid")
        offer_date = date.fromisoformat(str(payload.get("_offer_date") or ""))
        with self.estimate_browser_factory(
            self.cdp_helper, thread_url, estimate_url, False,
        ) as browser:
            browser.semantic_context_required = True
            context, before = browser.read_thread_context()
            context["semantic_estimate_terms"] = semantic_terms
            if requested_estimate.semantic_context_sha256(context.get("conversation") or []) != payload.get("_semantic_context_sha256"):
                raise RuntimeError("coconala_estimate_context_changed")
            form = browser.open_form()
            if not requested_estimate.validate_form_contract(form):
                raise RuntimeError("coconala_estimate_form_invalid")
            context["live_form"] = form
            master = self.estimate_composer.select_category("master", context, form)
            master_form = browser.select_master(master)
            sub = self.estimate_composer.select_category("sub", context, master_form)
            category_form = browser.select_sub(sub)
            typ = (
                None if requested_estimate._category_type_optional(category_form)
                else self.estimate_composer.select_category("type", context, category_form)
            )
            context["live_form"] = category_form
            terms = self.estimate_composer.terms_with_categories(
                context, master=master, sub=sub, typ=typ,
            )
            terms = requested_estimate.validate_estimate_terms(terms, context)
            terms = requested_estimate.materialize_delivery_content(terms, offer_date)
            selected = browser.fill(
                terms, requested_estimate.completion_date(terms, offer_date).isoformat(),
            )
            if not requested_estimate.validate_selected_categories(
                selected.get("selected_categories"), terms,
                selected.get("category_type_contract"),
            ):
                raise RuntimeError("coconala_estimate_category_mismatch")
            if not requested_estimate.validate_form_selection(browser.read_form(), terms):
                raise RuntimeError("coconala_estimate_form_selection_mismatch")
            browser.first_submit()
            confirmation = browser.read_confirmation()
            if not requested_estimate.validate_confirmation(confirmation, terms, today=offer_date):
                raise RuntimeError("coconala_estimate_confirmation_mismatch")
            fresh = requested_estimate._fresh_context_before_click(
                browser, before.get("own_user_path"),
            )
            if requested_estimate.semantic_context_sha256(fresh.get("conversation") or []) != payload.get("_semantic_context_sha256"):
                raise RuntimeError("coconala_estimate_context_changed")
            click_started_at = int(datetime.now(timezone.utc).timestamp())
            clicks_before = int(getattr(browser, "final_clicks", 0))
            try:
                browser.final_submit(confirmation, terms, today=offer_date)
            except Exception:
                if int(getattr(browser, "final_clicks", 0)) > clicks_before:
                    return
                raise
            try:
                after = browser.read_after()
                outcome = requested_estimate.classify_delivery(
                    pre_click_cards=[], post_click_cards=after.get("structured_offers") or [],
                    terms=terms, click_started_at=click_started_at,
                    today=offer_date, request_sent_at=payload.get("_request_sent_at"),
                    own_user_path=after.get("own_user_path"),
                )
            except Exception:
                return
        cards = outcome.get("cards") if isinstance(outcome, Mapping) else None
        if outcome.get("status") == "verified" and isinstance(cards, list) and len(cards) == 1:
            receipt_id = str(cards[0].get("offer_url") or "").strip()
            if receipt_id:
                self._receipts[intent["effect_key"]] = {
                    "provider_receipt_id": receipt_id, "observed_at": _now(),
                }

    def close(self) -> None:
        return None


class CoconalaSemanticComposer:
    def __init__(self, adapter: CoconalaReplyAdapter, judge: Any):
        self.adapter = adapter
        self.judge = judge

    def __call__(self, context: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(context.get("thread_id") or "").strip()
        if not thread_id:
            raise RuntimeError("coconala_thread_identity_invalid")
        url = f"https://coconala.com/mypage/direct_message/{thread_id}"
        receipt = self.judge(self.adapter.semantic_dom(thread_id), url)
        judgement = receipt.get("judgement") if isinstance(receipt, Mapping) else None
        if not isinstance(judgement, Mapping):
            raise RuntimeError("coconala_semantic_receipt_invalid")
        if judgement.get("required_official_context") == "application":
            official_context = self.adapter.official_application_context(thread_id)
            if official_context is None:
                return dict(judgement)
            receipt = self.judge(
                self.adapter.semantic_dom(thread_id), url,
                official_context=official_context,
            )
            judgement = receipt.get("judgement") if isinstance(receipt, Mapping) else None
            if not isinstance(judgement, Mapping):
                raise RuntimeError("coconala_semantic_receipt_invalid")
        result = dict(judgement)
        if result.get("next_action") == "send_estimate":
            terms = result.get("estimate_terms")
            raw = self.adapter.semantic_dom(thread_id)
            if not isinstance(terms, Mapping):
                raise RuntimeError("coconala_estimate_terms_invalid")
            evidence_ids = result.get("evidence_message_ids") or []
            conversation = context.get("conversation") or []
            source = next((row for row in reversed(conversation) if row.get("message_id") in evidence_ids), None)
            estimate_url = requested_estimate.sanitize_estimate_url(raw.get("estimate_url"))
            if not isinstance(source, Mapping) or estimate_url is None:
                raise RuntimeError("coconala_estimate_source_invalid")
            result["estimate_terms"] = {
                **dict(terms),
                "_semantic_context_sha256": receipt["context_sha256"],
                "_request_sent_at": source.get("sent_at"),
                "_estimate_url": estimate_url,
                "_offer_date": datetime.now().astimezone().date().isoformat(),
            }
        return result


def build(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--cdp-helper", required=True, type=Path)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--estimate-schema", required=True, type=Path)
    parser.add_argument(
        "--candidate-profile", type=Path,
        default=Path.home() / ".config/anicca/job-search/profile.json",
    )
    parser.add_argument(
        "--provider-profile", type=Path,
        default=Path.home() / ".config/anicca/crowdworks/public-profile.json",
    )
    args = parser.parse_args(argv)
    root = args.state_root.expanduser().resolve()
    adapter = CoconalaReplyAdapter(
        state_root=root, cdp_helper=args.cdp_helper.expanduser().resolve(),
        estimate_composer=requested_estimate.RequestedEstimateComposer(
            runner=args.runner, schema=args.estimate_schema, workdir=REPO_ROOT,
            temp_root=root / "estimate-model-tmp",
        ),
    )
    grounding = reply_grounding.build_reply_grounding(
        candidate_profile_path=args.candidate_profile,
        provider_profile_path=args.provider_profile,
    )
    semantic = requested_estimate.SemanticJudge(
        runner=args.runner, schema=args.schema, workdir=REPO_ROOT,
        evidence_root=root / "semantic-evidence",
        seller_facts=grounding["prompt_facts"],
    )
    return adapter, reply_planner.ReplyPlanner(
        CoconalaSemanticComposer(adapter, semantic)
    )
