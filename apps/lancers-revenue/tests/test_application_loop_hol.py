import copy
import hashlib
import io
import importlib.util
import inspect
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jsonschema


REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_LOOP_PATH = REPO_ROOT / "skills/earn/lancers/scripts/application_loop.py"


def _load_deployed_loop():
    spec = importlib.util.spec_from_file_location(
        "test_canonical_lancers_application_loop", CANONICAL_LOOP_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical_application_loop_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _opportunity(project_id: str, *, budget_min_minor: int = 100000) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "lancers_public_opportunity",
        "platform": "lancers",
        "external_id": project_id,
        "title": "月次SNS運用の外部委託",
        "description": (
            "依頼主の業種: 情報通信業\n"
            "依頼概要: SNS運用を毎月、外部委託でお願いしたいです。"
        ),
        "url": f"https://www.lancers.jp/work/detail/{project_id}",
        "category": "システム開発・運用",
        "budget_type": "fixed",
        "budget_min_minor": budget_min_minor,
        "budget_max_minor": 300000,
        "currency": "JPY",
        "buyer_external_id": f"buyer-{project_id}",
        "observed_at": "2026-08-13T12:00:00+00:00",
    }


def _proposal_text(price: int = 250000) -> str:
    return (
        "公開された要件を踏まえ、業務改善のためのWebシステムを整備し、少人数の担当者がSNSを兼務する状況でも作業が滞らない状態を目指します。"
        "最初の30日で現状整理、画面設計、試作、動作確認、引き継ぎ資料を納品します。"
        f"対象チャネルは2つ、月8本、修正は2回までとし、価格は{price}円、納期は2026-08-20です。"
        "毎月の運用支援と改善提案を継続範囲に含めます。現在の優先チャネルはこの2つでよろしいでしょうか？"
    )


def _eligible_decision(
    project_id: str = "6000001",
    *,
    price_jpy: int = 250000,
) -> dict[str, object]:
    return {
        "request_id": project_id,
        "business_class": "submit_required",
        "reason_codes": [],
        "proposal_text": _proposal_text(price_jpy),
        "price_jpy": price_jpy,
        "deliver_date": "2026-08-20",
    }


def _ineligible_decision(project_id: str) -> dict[str, object]:
    return {
        "request_id": project_id,
        "business_class": "hard_prohibited",
        "reason_codes": ["mandatory_human_presence", "SNS運用"],
        "proposal_text": None,
        "price_jpy": None,
        "deliver_date": None,
    }


def _approved_safety(*_args, **_kwargs) -> dict[str, object]:
    return {"safe_to_submit": True, "reason": "approved", "blocker_evidence": None}


def _rejected_safety(prompt, _evidence) -> dict[str, object]:
    payload = json.loads(prompt.split("\n", 1)[1])
    description = payload["public_opportunity"]["description"]
    blocker = description.split("依頼概要:", 1)[-1].strip()[:240]
    return {"safe_to_submit": False, "reason": "other_policy_blocker", "blocker_evidence": blocker}


class _FakeLocator:
    def __init__(self, *, text: str = "", attributes: dict[str, str] | None = None, children: dict[str, object] | None = None):
        self._text = text
        self._attributes = attributes or {}
        self._children = children or {}

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> "_FakeLocator":
        if index != 0:
            raise AssertionError(index)
        return self

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._attributes.get(name)

    def locator(self, selector: str) -> "_FakeLocator":
        return self._children[selector]  # type: ignore[return-value]


class _FakeProposalPage:
    def __init__(self, *, project_id: str, heading_text: str, project_href: str | None = None):
        proposal_id = "27808988"
        heading_href = f"/work/proposal/{proposal_id}"
        card_heading = _FakeLocator(
            text=heading_text,
            attributes={"href": heading_href},
        )
        card = _FakeLocator(
            attributes={"id": f"js-list-item-{proposal_id}"},
            children={"a.p-simpleProposal-list__heading-title": card_heading},
        )
        self.url = "https://www.lancers.jp/mypage/proposals"
        self._locators = {
            f'a[href="/work/detail/{project_id}"]': _FakeLocator(
                attributes={"href": project_href or f"/work/detail/{project_id}"},
            ),
            f'a[href^="/work/proposals/{project_id}/"][href$="?ref=mypage_control"]': _FakeLocator(
                text="提案をみる",
                attributes={
                    "href": f"/work/proposals/{project_id}/keiodaisuke?ref=mypage_control"
                },
            ),
            'meta[property="og:url"]': _FakeLocator(
                attributes={
                    "content": f"https://www.lancers.jp/work/proposals/{project_id}/keiodaisuke"
                },
            ),
            "a.p-simpleProposal-list__heading-title": _FakeLocator(
                text=heading_text,
                attributes={"href": heading_href},
            ),
            f"div#js-list-item-{proposal_id}": card,
        }

    def locator(self, selector: str) -> _FakeLocator:
        return self._locators[selector]

    def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url


class _FakeBrowser:
    def __init__(self, page: _FakeProposalPage):
        self.contexts = [self]
        self._page = page

    def new_page(self) -> _FakeProposalPage:
        return self._page


class ApplicationLoopHolTests(unittest.TestCase):
    def test_default_proposal_reader_accepts_mutable_display_name(self):
        application_loop = _load_deployed_loop()
        project_id = "5585503"
        page = _FakeProposalPage(
            project_id=project_id,
            heading_text="SNS・AI業務設計室 さんの提案",
        )

        self.assertEqual(
            application_loop.application_tick._default_proposal_reader(page, project_id),
            {"proposal_id": "27808988", "project_id": project_id},
        )

    def test_default_proposal_reader_rejects_malformed_heading(self):
        application_loop = _load_deployed_loop()
        project_id = "5585503"
        page = _FakeProposalPage(project_id=project_id, heading_text="提案")

        self.assertEqual(
            application_loop.application_tick._default_proposal_reader(page, project_id),
            {},
        )

    def test_run_live_tick_reconciles_target_pending_descriptor_only(self):
        application_loop = _load_deployed_loop()
        application_tick = application_loop.application_tick
        other_project_id = "5585496"
        target_project_id = "5586112"
        markers = {
            project_id: hashlib.sha256(
                f"lancers:application:{project_id}".encode()
            ).hexdigest()
            for project_id in (other_project_id, target_project_id)
        }
        state = {
            "fingerprints": list(markers.values()),
            "pending": {
                markers[other_project_id]: {
                    "proposal_id": "27800001",
                    "content_sha256": hashlib.sha256(b"other").hexdigest(),
                    "amount_minor": 110000,
                    "delivery_due_on": "2026-09-01",
                    "project_id": other_project_id,
                },
                markers[target_project_id]: {
                    "proposal_id": None,
                    "content_sha256": hashlib.sha256(b"target").hexdigest(),
                    "amount_minor": 98000,
                    "delivery_due_on": "2026-09-10",
                    "project_id": target_project_id,
                },
            },
        }
        receipts = []
        page = _FakeProposalPage(project_id=target_project_id, heading_text="unused")
        browser = _FakeBrowser(page)
        remaining_pending = None

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "application.json"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with patch.object(
                application_tick,
                "_production_account_ready",
                return_value=True,
            ), patch.object(
                application_tick,
                "read_pending_descriptor",
                return_value={
                    "project_id": other_project_id,
                    "amount_minor": 110000,
                    "delivery_due_on": "2026-09-01",
                },
            ), patch.object(
                application_tick,
                "read_pending_descriptors",
                create=True,
                return_value=[
                    {
                        "project_id": other_project_id,
                        "amount_minor": 110000,
                        "delivery_due_on": "2026-09-01",
                    },
                    {
                        "project_id": target_project_id,
                        "amount_minor": 98000,
                        "delivery_due_on": "2026-09-10",
                    },
                ],
            ):
                result = application_tick.run_live_tick(
                    project_id=target_project_id,
                    proposal_text="pending reconciliation",
                    proposed_amount_minor=98000,
                    delivery_due_on="2026-09-10",
                    state_path=state_path,
                    browser_factory=lambda _url: browser,
                    ledger_writer=receipts.append,
                    now=lambda: "2026-08-13T12:00:00Z",
                    submitter_override=lambda *_args: self.fail("pending_submit"),
                    readback_override=lambda _proposal_id, project_id: {
                        "proposal_id": "27808988",
                        "project_id": project_id,
                    },
                )
            remaining_pending = json.loads(state_path.read_text(encoding="utf-8"))["pending"]

        self.assertTrue(result.ok)
        self.assertFalse(result.submitted)
        self.assertTrue(result.application_verified)
        self.assertEqual(result.project_id, target_project_id)
        self.assertEqual(result.provider_proposal_id, "27808988")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["opportunity_external_id"], target_project_id)
        self.assertEqual(remaining_pending.keys(), {markers[other_project_id]})

    def test_discovery_query_rotates_by_utc_half_hour_slot(self):
        application_loop = _load_deployed_loop()
        calls = []
        clock_calls = []

        def discoverer(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "error": None, "opportunities": []}

        def clock_for(value):
            return lambda: (clock_calls.append(value) or value)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # One slot per query, whatever the list length. Hard-coding ten broke the moment the
            # queries were re-derived from the catalogue, which is a change this test should not
            # have an opinion about.
            total = len(application_loop.DISCOVERY_QUERIES)
            slots = [datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc) + timedelta(minutes=30 * index)
                     for index in range(total)]
            for slot in slots:
                application_loop.run_loop(state_path=root / "application.json", evidence_root=root / "evidence", discoverer=discoverer, clock=clock_for(slot))
            same_slot = slots[3]
            for _ in range(2):
                application_loop.run_loop(state_path=root / "application.json", evidence_root=root / "evidence", discoverer=discoverer, clock=clock_for(same_slot))
            application_loop.run_loop(state_path=root / "application.json", evidence_root=root / "evidence", discoverer=discoverer, clock=clock_for(same_slot), query="explicit-query")

        # Every query is used exactly once across consecutive half-hour slots, starting wherever
        # the slot arithmetic lands rather than at index 0.
        used = [call["query"] for call in calls[:total]]
        assert sorted(used) == sorted(application_loop.DISCOVERY_QUERIES)
        start = application_loop.DISCOVERY_QUERIES.index(used[0])
        expected = [application_loop.DISCOVERY_QUERIES[(start + i) % total] for i in range(total)]
        self.assertEqual(used, expected)
        self.assertEqual(calls[total]["query"], calls[total + 1]["query"])
        self.assertEqual(calls[total + 2]["query"], "explicit-query")
        self.assertEqual(len(calls), len(clock_calls))
        self.assertTrue(all(call["limit"] == 20 for call in calls))

    def test_default_discovery_skips_a_query_containing_only_claimed_projects(self):
        application_loop = _load_deployed_loop(); calls = []
        responses = [
            {"ok": True, "error": None, "opportunities": [_opportunity("5583089")]},
            {"ok": True, "error": None, "opportunities": [_opportunity("5587000")]},
        ]
        def discover(**kwargs):
            calls.append(kwargs["query"])
            return responses.pop(0)
        with patch.object(application_loop.status, "run_discovery", side_effect=discover), patch.object(application_loop.application_tick, "state_has_claim", side_effect=lambda _path, project_id: project_id == "5583089"):
            result = application_loop._run_default_discovery(datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc), 20.0, Path("/tmp/application.json"))
        # Two consecutive queries from wherever the slot lands: the first is skipped because every
        # project in it is already claimed, the second supplies the fresh one.
        total = len(application_loop.DISCOVERY_QUERIES)
        start = application_loop.DISCOVERY_QUERIES.index(calls[0])
        self.assertEqual(calls, [application_loop.DISCOVERY_QUERIES[(start + i) % total] for i in range(2)])
        self.assertEqual(result["opportunities"][0]["external_id"], "5587000")

    def test_empty_normalized_discovery_is_noop_but_other_errors_fail(self):
        application_loop = _load_deployed_loop()
        for payload in (
            {"ok": False, "error": "no_normalized_opportunities", "opportunities": []},
            {"ok": False, "error": "lancers_provider_error", "opportunities": []},
        ):
            calls = []
            def discoverer(**_kwargs):
                calls.append(1)
                return payload
            with tempfile.TemporaryDirectory() as directory:
                result = application_loop.run_loop(state_path=Path(directory) / "application.json", evidence_root=Path(directory) / "evidence", discoverer=discoverer, planner=lambda *_args, **_kwargs: self.fail("planner_called"), submitter=lambda *_args, **_kwargs: self.fail("submitter_called"), clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc))
            self.assertEqual(calls, [1])
            self.assertEqual(result["ok"], payload["error"] == "no_normalized_opportunities")
            self.assertEqual(result.get("reason") if result["ok"] else result["error"], "no_eligible_project" if result["ok"] else "lancers_provider_error")
            if result["ok"]:
                self.assertFalse(result["submitted"])
                self.assertEqual(tuple(result[key] for key in ("observed_count", "eligible_count", "verified_count")), (0, 0, 0))

    def test_invoke_planner_uses_canonical_agent_runner_arguments(self):
        application_loop = _load_deployed_loop()
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return type("Completed", (), {"returncode": 0})()

        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            evidence.mkdir()
            result_path = evidence / "result.json"
            result_path.write_text(json.dumps({"decisions": []}), encoding="utf-8")
            (evidence / "summary.json").write_text(
                json.dumps({"status": "success", "result_path": str(result_path)}),
                encoding="utf-8",
            )
            prompt = application_loop.build_planner_prompt([_opportunity("6000001")], datetime(2026, 8, 13, tzinfo=timezone.utc))
            with patch.object(
                application_loop.subprocess, "run", side_effect=fake_run
            ):
                result = application_loop.invoke_planner(prompt, evidence)
            runtime_schema = json.loads((evidence / "planner-runtime.schema.json").read_text(encoding="utf-8"))

        command = calls[0][0]
        self.assertNotIn("--timeout-seconds", command)
        for argument in (
            "--task-class",
            "application-intent-planner",
            "--prompt-stdin",
            "--schema",
            str(evidence / "planner-runtime.schema.json"),
            "--evidence-dir",
            str(evidence),
            "--task-label",
            "lancers-application-intent",
            "--loop",
            "lancers-application",
            "--workdir",
            str(application_loop.SKILLS_ROOT.parent),
            # The planner runs on the explicit escalation route; without this the runner refuses.
            "--escalation-reason",
            application_loop.ESCALATION_REASON,
        ):
            self.assertIn(argument, command)
        self.assertEqual(result, {"decisions": []})
        decisions = runtime_schema["properties"]["decisions"]
        self.assertEqual((decisions["minItems"], decisions["maxItems"]), (1, 1))
        self.assertEqual(decisions["items"]["properties"]["request_id"]["enum"], ["6000001"])

    def test_normal_tick_submits_every_eligible_project_in_ranked_order(self):
        application_loop = _load_deployed_loop()
        submitter_project_ids = []
        opportunities = [_opportunity("6000001"), _opportunity("6000002")]

        def discoverer(**kwargs):
            return {"ok": True, "error": None, "opportunities": opportunities}

        def planner(prompt, evidence):
            return {
                "decisions": [
                    _eligible_decision("6000001"),
                    _eligible_decision("6000002"),
                ]
            }

        def submitter(**kwargs):
            submitter_project_ids.append(kwargs["project_id"])
            return {
                "ok": True,
                "submitted": True,
                "application_verified": True,
                "project_id": kwargs["project_id"],
                "provider_proposal_id": f"proposal-{kwargs['project_id']}",
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = application_loop.run_loop(
                state_path=root / "application.json",
                evidence_root=root / "evidence",
                discoverer=discoverer,
                planner=planner,
                safety_verifier=_approved_safety,
                submitter=submitter,
                clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            )

        # One tick now submits every eligible project rather than stopping after the first: the
        # result shape is built for it (_batch_summary, verified/blocked lists). What must still
        # hold is that each project is submitted exactly once, in the planner's order.
        self.assertEqual(submitter_project_ids, ["6000001", "6000002"])
        self.assertEqual(len(set(submitter_project_ids)), len(submitter_project_ids))
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["verified_count"], 2)
        with tempfile.TemporaryDirectory() as directory:
            partial = application_loop.run_loop(
                state_path=Path(directory) / "application.json", evidence_root=Path(directory) / "evidence",
                discoverer=discoverer, planner=lambda *_args: {"decisions": [_eligible_decision("6000001")]},
                safety_verifier=lambda *_args: self.fail("safety_called"), submitter=submitter,
                clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            )
        # A planner handed two rows and returning one has judged one row, and that judgement is
        # worth acting on. Discarding both was measured on 2026-09-07 as planner_contract_invalid
        # 1235 times -- more than every other lane failure combined -- and is the same mistake as
        # test_one_bad_budget_row_does_not_discard_two_good_rows below, one row later.
        # The mismatch still has to be visible, so it is reported per row rather than as a count:
        # the judged row submits, the unjudged one is named as failed.
        self.assertNotIn("error", partial)
        self.assertEqual(partial["eligible_count"], 1)
        self.assertEqual(partial["verified_count"], 1)
        by_id = {report["project_id"]: report for report in partial["decision_reports"]}
        self.assertEqual(by_id["6000001"]["outcome"], "application_verified")
        self.assertEqual(by_id["6000002"]["outcome"], "failed")
        self.assertEqual(by_id["6000002"]["error"], "planner_contract_invalid")

    def test_one_bad_budget_row_does_not_discard_two_good_rows(self):
        # skills/earn/lancers/scripts/application_loop.py::_filter_claimed_rows used to
        # `raise ValueError` for the whole batch when any single row had a malformed
        # budget shape, which surfaced as planner_contract_invalid with zero judgements
        # even though the other rows in the same batch were perfectly valid.
        application_loop = _load_deployed_loop()
        bad_row = _opportunity("6000000")
        bad_row["budget_min_minor"] = 500000  # exceeds budget_max_minor: invalid shape
        opportunities = [bad_row, _opportunity("6000001"), _opportunity("6000002")]

        def discoverer(**kwargs):
            return {"ok": True, "error": None, "opportunities": opportunities}

        def planner(prompt, evidence):
            return {"decisions": [_ineligible_decision("6000001"), _ineligible_decision("6000002")]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = application_loop.run_loop(
                state_path=root / "application.json",
                evidence_root=root / "evidence",
                discoverer=discoverer,
                planner=planner,
                safety_verifier=_approved_safety,
                submitter=lambda **_kwargs: self.fail("submitter_called"),
                clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            )

        self.assertNotEqual(result.get("error"), "planner_contract_invalid")
        self.assertEqual(result.get("observed_count"), 3)
        reports = result.get("decision_reports") or []
        self.assertEqual(len(reports), 3)
        judged = {report["project_id"]: report for report in reports}
        self.assertEqual(judged["6000000"]["error"], "invalid_observed_budget")
        self.assertEqual(judged["6000001"]["business_class"], "hard_prohibited")
        self.assertEqual(judged["6000002"]["business_class"], "hard_prohibited")

    def test_non_proposal_task_does_not_reach_the_proposal_planner(self):
        application_loop = _load_deployed_loop()
        task = _opportunity("6000004")
        task["budget_type"] = "bounty"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = application_loop.run_loop(
                state_path=root / "application.json",
                evidence_root=root / "evidence",
                discoverer=lambda **_kwargs: {
                    "ok": True,
                    "error": None,
                    "opportunities": [task],
                },
                planner=lambda *_args: self.fail("planner_called"),
                safety_verifier=lambda *_args: self.fail("safety_called"),
                submitter=lambda **_kwargs: self.fail("submitter_called"),
                clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "no_eligible_project")
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(
            result["decision_reports"][0]["error"],
            "unsupported_application_workflow",
        )

    def test_normal_tick_preserves_coconala_planner_order(self):
        application_loop = _load_deployed_loop()
        project_ids = ["6000001", "6000002", "6000003"]
        opportunities = [_opportunity(project_id) for project_id in project_ids]
        decisions = [
            _eligible_decision("6000003", price_jpy=230000),
            _eligible_decision("6000001", price_jpy=250000),
            _eligible_decision("6000002", price_jpy=220000),
        ]
        submitted = []

        def discoverer(**_kwargs):
            return {"ok": True, "error": None, "opportunities": opportunities}

        def planner(_prompt, _evidence):
            return {"decisions": decisions}

        def submitter(**kwargs):
            submitted.append(kwargs["project_id"])
            return {"ok": True, "submitted": True, "application_verified": True, "project_id": kwargs["project_id"], "provider_proposal_id": "9000011"}

        with tempfile.TemporaryDirectory() as directory:
            result = application_loop.run_loop(state_path=Path(directory) / "application.json", evidence_root=Path(directory) / "evidence", discoverer=discoverer, planner=planner, safety_verifier=_approved_safety, submitter=submitter, clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc))

        self.assertEqual(result["eligible_count"], 3)
        # Ranking is still the contract -- the highest-ranked project goes first -- but the tick no
        # longer stops there. Assert the head, and that nothing is submitted twice.
        self.assertEqual(submitted[0], "6000003")
        self.assertEqual(sorted(submitted), ["6000001", "6000002", "6000003"])
        self.assertEqual(len(set(submitted)), len(submitted))

    def test_reconcile_only_reconciles_every_pending_application_without_discovery_or_submit(self):
        application_loop = _load_deployed_loop()
        project_ids = ["5585496", "5586112"]
        state = {"fingerprints": [], "pending": {}}
        for project_id in project_ids:
            marker = hashlib.sha256(
                f"lancers:application:{project_id}".encode()
            ).hexdigest()
            state["fingerprints"].append(marker)
            state["pending"][marker] = {
                "proposal_id": None,
                "content_sha256": hashlib.sha256(
                    f"proposal:{project_id}".encode()
                ).hexdigest(),
                "amount_minor": 250000,
                "delivery_due_on": "2026-09-10",
                "project_id": project_id,
            }

        called_project_ids = []

        def run_live_tick(**kwargs):
            called_project_ids.append(kwargs["project_id"])
            return {
                "ok": False,
                "error": "submission_uncertain",
                "project_id": kwargs["project_id"],
            }

        def forbidden(*_args, **_kwargs):
            raise AssertionError("discovery_or_submit_called")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "application.json"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            original_state = copy.deepcopy(json.loads(state_path.read_text()))
            with patch.object(
                application_loop.application_tick,
                "run_live_tick",
                side_effect=run_live_tick,
            ), patch.object(
                application_loop.status, "run_discovery", side_effect=forbidden
            ), patch.object(
                application_loop, "_plan_and_submit", side_effect=forbidden
            ), patch.object(application_loop, "_submit", side_effect=forbidden):
                result = application_loop.run_reconcile_only(state_path)

                self.assertEqual(called_project_ids, project_ids)
                self.assertEqual(result["reconciled_project_ids"], project_ids)
                self.assertEqual(result["verified_project_ids"], [])
                self.assertEqual(result["unresolved_project_ids"], project_ids)
                self.assertFalse(result["submitted"])
                self.assertEqual(json.loads(state_path.read_text()), original_state)

                called_project_ids.clear()
                self.assertEqual(
                    application_loop.main(
                        ["--json", "--reconcile-only", "--state-path", str(state_path)],
                        stdout=io.StringIO(),
                    ),
                    1,
                )
                self.assertEqual(called_project_ids, project_ids)

    def test_uncertain_pending_is_quarantined_without_blocking_new_verified_application(self):
        application_loop = _load_deployed_loop()
        discovery_calls = []
        planner_calls = []
        submitter_project_ids = []
        opportunities = [_opportunity("5585496"), _opportunity("6000001")]
        proposal_text = (
            "公開された要件を踏まえ、業務改善のためのWebシステムを整備し、少人数の担当者がSNSを兼務する状況でも作業が滞らない状態を目指します。"
            "最初の30日で現状整理、画面設計、試作、動作確認、引き継ぎ資料を納品します。"
            "対象チャネルは2つ、月8本、修正は2回までとし、価格は250000円、納期は2026-08-20です。"
            "毎月の運用支援と改善提案を継続範囲に含めます。現在の優先チャネルはこの2つでよろしいでしょうか？"
        )

        def discoverer(**kwargs):
            discovery_calls.append(kwargs)
            return {"ok": True, "error": None, "opportunities": opportunities}

        def planner(prompt, evidence):
            planner_calls.append((prompt, evidence))
            return {
                "decisions": [
                    {
                        "request_id": "6000001",
                        "business_class": "submit_required",
                        "reason_codes": [],
                        "proposal_text": proposal_text,
                        "price_jpy": 250000,
                        "deliver_date": "2026-08-20",
                    }
                ]
            }

        def submitter(**kwargs):
            submitter_project_ids.append(kwargs["project_id"])
            return {
                "ok": True,
                "submitted": True,
                "application_verified": True,
                "project_id": kwargs["project_id"],
                "provider_proposal_id": "9000001",
            }

        pending = {
            "project_id": "5585496",
            "amount_minor": 250000,
            "delivery_due_on": "2026-09-10",
            "proposal_id": None,
        }
        pending_result = application_loop.ApplicationLoopResult(
            False,
            error="submission_uncertain",
            project_id="5585496",
            unresolved_project_id="5585496",
        )
        fixed_clock = lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = None
            with patch.object(
                application_loop.application_tick,
                "read_pending_descriptor",
                return_value=pending,
            ), patch.object(
                application_loop,
                "_reconcile_pending",
                return_value=pending_result,
            ), patch.object(
                application_loop.application_tick,
                "state_has_claim",
                side_effect=lambda _state_path, project_id: project_id == "5585496",
            ):
                result = application_loop.run_loop(
                    state_path=root / "application.json",
                    evidence_root=root / "evidence",
                    discoverer=discoverer,
                    planner=planner,
                    safety_verifier=_approved_safety,
                    submitter=submitter,
                    clock=fixed_clock,
                )

        self.assertEqual(len(discovery_calls), 1)
        self.assertEqual(submitter_project_ids, ["6000001"])
        self.assertNotIn("5585496", submitter_project_ids)
        self.assertEqual(len(planner_calls), 1)
        snapshot = json.loads(planner_calls[0][0].split("SNAPSHOT:\n", 1)[1])
        self.assertEqual(
            [row["external_id"] for row in snapshot["opportunities"]], ["6000001"]
        )
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["verified_project_ids"], ["6000001"])
        self.assertEqual(result["verified_provider_proposal_ids"], ["9000001"])
        self.assertEqual(result["unresolved_project_id"], "5585496")

    def test_low_price_feasible_work_is_submitted_without_margin_gate(self):
        application_loop = _load_deployed_loop()
        submitter_project_ids = []
        opportunities = [_opportunity("6000001", budget_min_minor=98000)]

        def discoverer(**kwargs):
            return {"ok": True, "error": None, "opportunities": opportunities}

        def planner(prompt, evidence):
            return {"decisions": [_eligible_decision(price_jpy=1)]}

        def submitter(**kwargs):
            submitter_project_ids.append(kwargs["project_id"])
            return {
                "ok": True,
                "submitted": True,
                "application_verified": True,
                "project_id": kwargs["project_id"],
                "provider_proposal_id": "9000002",
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                application_loop.application_tick,
                "read_pending_descriptor",
                return_value=None,
            ), patch.object(
                application_loop.application_tick,
                "state_has_claim",
                return_value=False,
            ):
                result = application_loop.run_loop(
                    state_path=root / "application.json",
                    evidence_root=root / "evidence",
                    discoverer=discoverer,
                    planner=planner,
                    submitter=submitter,
                    clock=lambda: datetime(
                        2026, 8, 13, 12, 0, tzinfo=timezone.utc
                    ),
                )

        self.assertNotIn("error", result)
        self.assertEqual(submitter_project_ids, ["6000001"])

    def test_hard_prohibition_requires_exact_public_evidence(self):
        application_loop = _load_deployed_loop()
        submitter_project_ids = []
        opportunities = [_opportunity("6000001")]

        def discoverer(**kwargs):
            return {"ok": True, "error": None, "opportunities": opportunities}

        invalid = _ineligible_decision("6000001")
        invalid["reason_codes"][1] = "公開本文にない捏造引用"

        def planner(prompt, evidence):
            return {"decisions": [invalid]}

        def submitter(**kwargs):
            submitter_project_ids.append(kwargs["project_id"])
            return {
                "ok": True,
                "submitted": True,
                "application_verified": True,
                "project_id": kwargs["project_id"],
                "provider_proposal_id": "9000003",
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                application_loop.application_tick,
                "read_pending_descriptor",
                return_value=None,
            ), patch.object(
                application_loop.application_tick,
                "state_has_claim",
                return_value=False,
            ):
                result = application_loop.run_loop(
                    state_path=root / "application.json",
                    evidence_root=root / "evidence",
                    discoverer=discoverer,
                    planner=planner,
                    submitter=submitter,
                    clock=lambda: datetime(
                        2026, 8, 13, 12, 0, tzinfo=timezone.utc
                    ),
                )

        # The fabricated-quote check still holds -- nothing is submitted -- but it is reported per
        # row now instead of failing the whole batch, so one bad decision cannot discard good ones.
        self.assertEqual(submitter_project_ids, [])
        self.assertTrue(result["ok"])
        report = result["decision_reports"][0]
        self.assertEqual(report["project_id"], "6000001")
        self.assertEqual(report["business_class"], "invalid")
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["error"], "planner_contract_invalid")

    def test_claimed_pending_projects_are_excluded_before_planning_even_over_batch_limit(self):
        application_loop = _load_deployed_loop()
        planner_snapshots = []
        submitter_project_ids = []
        opportunities = [
            _opportunity("5585496"),
            _opportunity("5586112"),
            _opportunity("6000001"),
        ] + [_opportunity(str(7000000 + index)) for index in range(18)]

        def discoverer(**kwargs):
            return {"ok": True, "error": None, "opportunities": opportunities}

        def planner(prompt, evidence):
            snapshot = json.loads(prompt.split("SNAPSHOT:\n", 1)[1])
            planner_snapshots.append(snapshot)
            return {
                "decisions": [
                    _eligible_decision(row["external_id"])
                    if row["external_id"] == "6000001"
                    else _ineligible_decision(row["external_id"])
                    for row in snapshot["opportunities"]
                ]
            }

        def submitter(**kwargs):
            submitter_project_ids.append(kwargs["project_id"])
            return {
                "ok": True,
                "submitted": True,
                "application_verified": True,
                "project_id": kwargs["project_id"],
                "provider_proposal_id": "9000004",
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                application_loop.application_tick,
                "read_pending_descriptor",
                return_value=None,
            ), patch.object(
                application_loop.application_tick,
                "state_has_claim",
                side_effect=lambda _state_path, project_id: project_id
                in {"5585496", "5586112"},
            ):
                result = application_loop.run_loop(
                    state_path=root / "application.json",
                    evidence_root=root / "evidence",
                    discoverer=discoverer,
                    planner=planner,
                    safety_verifier=_approved_safety,
                    submitter=submitter,
                    clock=lambda: datetime(
                        2026, 8, 13, 12, 0, tzinfo=timezone.utc
                    ),
                )

        self.assertNotIn("error", result)
        planned_project_ids = [
            row["external_id"] for row in planner_snapshots[0]["opportunities"]
        ]
        self.assertNotIn("5585496", planned_project_ids)
        self.assertNotIn("5586112", planned_project_ids)
        self.assertIn("6000001", planned_project_ids)
        self.assertEqual(submitter_project_ids, ["6000001"])
        self.assertEqual(result["verified_project_ids"], ["6000001"])

    def test_discovery_batch_over_max_opportunities_is_sliced_not_rejected(self):
        """A wake that observes more than MAX_OPPORTUNITIES rows (exhaustive discovery unions
        every query with no cap) must still get a judged batch instead of failing the whole
        turn as planner_contract_invalid before the planner is ever called."""
        application_loop = _load_deployed_loop()
        planner_snapshots = []
        opportunities = [
            _opportunity(str(8000000 + index))
            for index in range(application_loop.MAX_OPPORTUNITIES + 5)
        ]

        def discoverer(**kwargs):
            return {"ok": True, "error": None, "opportunities": opportunities}

        def planner(prompt, evidence):
            snapshot = json.loads(prompt.split("SNAPSHOT:\n", 1)[1])
            planner_snapshots.append(snapshot)
            return {
                "decisions": [
                    _ineligible_decision(row["external_id"])
                    for row in snapshot["opportunities"]
                ]
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                application_loop.application_tick,
                "read_pending_descriptor",
                return_value=None,
            ), patch.object(
                application_loop.application_tick,
                "state_has_claim",
                return_value=False,
            ):
                result = application_loop.run_loop(
                    state_path=root / "application.json",
                    evidence_root=root / "evidence",
                    discoverer=discoverer,
                    planner=planner,
                    submitter=lambda *_args, **_kwargs: self.fail("submitter_called"),
                    clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
                )

        self.assertNotIn("error", result)
        self.assertEqual(len(planner_snapshots), 1)
        self.assertEqual(
            len(planner_snapshots[0]["opportunities"]),
            application_loop.MAX_OPPORTUNITIES,
        )

    def test_schema_and_runtime_share_coconala_business_contract(self):
        application_loop = _load_deployed_loop()
        schema = json.loads(
            Path(application_loop.SCHEMA_PATH).read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(schema)
        row = _opportunity("6000001", budget_min_minor=98000)
        valid_submit = _eligible_decision(price_jpy=1)
        valid_prohibited = _ineligible_decision("6000001")
        submit_with_reason = copy.deepcopy(valid_submit)
        submit_with_reason["reason_codes"] = ["weak_portfolio"]
        prohibited_without_evidence = copy.deepcopy(valid_prohibited)
        prohibited_without_evidence["reason_codes"] = ["mandatory_human_presence"]
        cases = (
            ("submit_low_price_valid", valid_submit, True, []),
            ("submit_reason_forbidden", submit_with_reason, True, ["planner_failed"]),
            ("hard_prohibited_valid", valid_prohibited, True, []),
            ("hard_prohibited_evidence_required", prohibited_without_evidence, True, ["planner_failed"]),
        )

        for name, decision, schema_expected, runtime_expected in cases:
            payload = {"decisions": [decision]}
            schema_valid = validator.is_valid(payload)
            self.assertEqual(schema_valid, schema_expected, name)
            self.assertEqual(
                application_loop.validate_decisions(
                    [row], payload, datetime(2026, 8, 13, tzinfo=timezone.utc)
                ),
                runtime_expected,
                name,
            )

    def test_single_and_non_b2b_feasible_work_remains_submit_required(self):
        application_loop = _load_deployed_loop()
        staff_proxy = _opportunity("6000001")
        staff_proxy["description"] = "依頼主の業種: 情報通信業\n依頼概要: 少人数の担当者がSNSを兼務しています。"
        one_off = _opportunity("6000001")
        one_off["description"] = "依頼主の業種: 情報通信業\n依頼概要: SNS投稿を1回お願いします。"
        cases = (("staff_proxy", staff_proxy), ("one_off", one_off))

        for name, opportunity in cases:
            submitted = []

            def discoverer(**_kwargs):
                return {"ok": True, "error": None, "opportunities": [opportunity]}

            def planner(*_args):
                return {"decisions": [_eligible_decision()]}

            def submitter(**kwargs):
                submitted.append(kwargs["project_id"])
                return {"ok": True, "submitted": True, "application_verified": True, "project_id": kwargs["project_id"], "provider_proposal_id": "9000010"}

            with tempfile.TemporaryDirectory() as directory, patch.object(application_loop.application_tick, "read_pending_descriptor", return_value=None), patch.object(application_loop.application_tick, "state_has_claim", return_value=False):
                result = application_loop.run_loop(state_path=Path(directory) / "application.json", evidence_root=Path(directory) / "evidence", discoverer=discoverer, planner=planner, submitter=submitter, clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc))

            self.assertEqual(submitted, ["6000001"], name)
            self.assertEqual(result.get("verified_count"), 1, name)

    def test_prompt_checks_delivery_capability_before_business_priority(self):
        application_loop = _load_deployed_loop()
        prompt = application_loop.build_planner_prompt(
            [_opportunity("6000001")],
            datetime(2026, 8, 13, tzinfo=timezone.utc),
        )

        self.assertLess(prompt.index("納品可能性をpriorityより先に確定"), prompt.index("納品可能性を確定した後の優先順"))
        self.assertIn("完成動画そのものの生成・編集・書き出しが必須ならvideo_or_animation", prompt)
        self.assertIn("企画・構成・台本・文章だけで完成動画制作が不要ならvideo_or_animationではない", prompt)
        self.assertIn("hard prohibition必須案件を継続・AI・高報酬・低予算・簡単そうという理由でsubmit_requiredへ変えない", prompt)

    def test_capacity_refuses_on_work_in_flight_or_an_unusable_snapshot(self):
        application_loop = _load_deployed_loop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "application.json"
            (root / "contracts.json").write_text(json.dumps({"source_complete": True, "observed_at": "2026-08-14T03:30:00Z", "project_working_count": 0, "monthly_contract_count": 0, "storefront_contract_candidate_count": 0, "contract_candidate_count": 0}))
            now = datetime(2026, 8, 14, 3, 35, tzinfo=timezone.utc)

            # The gate is no longer "N applications per day, counted from the ledger" -- it is "do
            # not take on more while work is already in flight", read from a fresh official
            # snapshot. A clean snapshot with nothing in flight is the only case that may apply.
            self.assertIsNone(application_loop._capacity_reason(state, now))

            snapshot = json.loads((root / "contracts.json").read_text())
            snapshot.update(project_working_count=1, contract_candidate_count=1)
            (root / "contracts.json").write_text(json.dumps(snapshot))
            self.assertEqual(application_loop._capacity_reason(state, now), "capacity_details_required")

            # A snapshot the provider did not finish, or one that cannot be read at all, must
            # refuse rather than assume free capacity.
            snapshot.update(source_complete=False)
            (root / "contracts.json").write_text(json.dumps(snapshot))
            self.assertEqual(application_loop._capacity_reason(state, now), "capacity_source_unavailable")
            (root / "contracts.json").write_text("{ not json")
            self.assertEqual(application_loop._capacity_reason(state, now), "capacity_source_unavailable")

    def test_schema_has_no_lancers_only_qualification_gate(self):
        schema = json.loads((REPO_ROOT / "skills/gig-work/schemas/application_decisions.schema.json").read_text(encoding="utf-8"))
        properties = schema["properties"]["decisions"]["items"]["properties"]
        self.assertNotIn("qualification", properties)
        self.assertEqual(properties["business_class"]["enum"], ["submit_required", "hard_prohibited"])


if __name__ == "__main__":
    unittest.main()


class ExhaustiveDiscoveryTests(unittest.TestCase):
    """The opt-in exhaustive path must see the whole board without widening the cheap tick."""

    def _rows(self, application_loop):
        # every query contributes one unique row plus one row they all share
        return {
            query: {"ok": True, "error": None, "opportunities": [
                {"external_id": str(1000 + index), "title": f"job{index}"},
                {"external_id": "9999", "title": "shared"},
            ]}
            for index, query in enumerate(application_loop.DISCOVERY_QUERIES)
        }

    def test_exhaustive_unions_every_query_and_deduplicates(self):
        application_loop = _load_deployed_loop()
        responses = self._rows(application_loop)
        calls = []

        def discover(**kwargs):
            calls.append(kwargs["query"])
            return responses[kwargs["query"]]

        with patch.object(application_loop.status, "run_discovery", side_effect=discover):
            result = application_loop._run_exhaustive_discovery(20.0)

        self.assertEqual(calls, list(application_loop.DISCOVERY_QUERIES))
        ids = sorted(row["external_id"] for row in result["opportunities"])
        self.assertEqual(len(ids), len(application_loop.DISCOVERY_QUERIES) + 1)
        self.assertEqual(ids.count("9999"), 1)
        self.assertTrue(result["ok"])
        self.assertNotIn("error", result)

    def test_exhaustive_refuses_to_report_a_partial_board(self):
        application_loop = _load_deployed_loop()

        def discover(**_kwargs):
            return {"ok": False, "error": "lancers_provider_error", "opportunities": []}

        with patch.object(application_loop.status, "run_discovery", side_effect=discover):
            result = application_loop._run_exhaustive_discovery(20.0)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "lancers_provider_error")

    def test_default_tick_is_unchanged_and_stays_opt_out(self):
        application_loop = _load_deployed_loop()
        self.assertIn("exhaustive", inspect.signature(application_loop.run_loop).parameters)
        self.assertIs(
            application_loop.run_loop.__kwdefaults__["exhaustive"], False,
        )

    def test_exhaustive_keeps_the_later_observation_of_a_duplicate(self):
        application_loop = _load_deployed_loop()
        queries = list(application_loop.DISCOVERY_QUERIES)
        responses = {q: {"ok": True, "error": None, "opportunities": []} for q in queries}
        responses[queries[0]]["opportunities"] = [
            {"external_id": "42", "title": "stale", "observed_at": "2026-08-31T09:00:00+00:00"},
        ]
        responses[queries[1]]["opportunities"] = [
            {"external_id": "42", "title": "fresh", "observed_at": "2026-08-31T10:00:00+00:00"},
        ]

        with patch.object(application_loop.status, "run_discovery",
                          side_effect=lambda **kw: responses[kw["query"]]):
            result = application_loop._run_exhaustive_discovery(20.0)

        rows = [r for r in result["opportunities"] if r["external_id"] == "42"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "fresh")

    def test_exhaustive_keeps_incumbent_when_timestamps_are_unusable(self):
        application_loop = _load_deployed_loop()
        queries = list(application_loop.DISCOVERY_QUERIES)
        responses = {q: {"ok": True, "error": None, "opportunities": []} for q in queries}
        responses[queries[0]]["opportunities"] = [{"external_id": "7", "title": "first"}]
        responses[queries[1]]["opportunities"] = [{"external_id": "7", "title": "second"}]

        with patch.object(application_loop.status, "run_discovery",
                          side_effect=lambda **kw: responses[kw["query"]]):
            result = application_loop._run_exhaustive_discovery(20.0)

        rows = [r for r in result["opportunities"] if r["external_id"] == "7"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "first")
