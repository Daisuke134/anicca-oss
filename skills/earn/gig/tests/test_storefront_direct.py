"""Focused boundary checks for the dedicated Storefront owner."""

from __future__ import annotations

import argparse
import json
import os
import pytest
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import storefront_direct as direct  # noqa: E402
import listing_inventory  # noqa: E402
import coconala_reply_browser as reply_browser  # noqa: E402
import reply_transcript  # noqa: E402
from test_storefront_root import _bundle as _storefront_bundle  # noqa: E402


def test_storefront_proposal_runner_class_is_accepted_and_toolless(tmp_path):
    runner_dir = SCRIPTS.parents[3] / "runtime/agent-runner"
    sys.path.insert(0, str(runner_dir))
    import agent_runner

    help_result = subprocess.run(
        [sys.executable, str(runner_dir / "agent_runner.py"), "--help"],
        text=True, capture_output=True, check=False,
    )
    assert help_result.returncode == 0
    assert "storefront-proposal-agent" in help_result.stdout

    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    command = agent_runner.command_for(
        "codex", "codex", {},
        {"provider": "codex", "model": "gpt-5.6-terra", "effort": "medium"},
        argparse.Namespace(
            task_class="storefront-proposal-agent", schema=schema,
            workdir=tmp_path, image=[], read_only=False,
        ),
        "bounded storefront proposal", {}, tmp_path / "result.json", 60, None,
        prompt_via_stdin=True,
    )
    sandbox = command.index("--sandbox")
    assert command[sandbox:sandbox + 2] == ["--sandbox", "read-only"]
    for feature in ("shell_tool", "code_mode_host", "unified_exec"):
        index = command.index(feature)
        assert command[index - 1:index + 1] == ["--disable", feature]


def test_capability_evidence_default_comes_only_from_operator_env(monkeypatch):
    monkeypatch.delenv("GIG_STOREFRONT_CAPABILITY_EVIDENCE", raising=False)
    assert direct.build_parser().parse_args([]).capability_evidence == []

    monkeypatch.setenv("GIG_STOREFRONT_CAPABILITY_EVIDENCE", os.pathsep.join(("a", "b")))
    assert direct.build_parser().parse_args([]).capability_evidence == [Path("a"), Path("b")]


def test_official_service_identity_flows_from_inquiry_to_payment(tmp_path):
    context, _ = reply_browser.thread_state({
        "url": "https://coconala.com/mypage/direct_message/123",
        "title": "メッセージ詳細 | ココナラ", "container_present": True,
        "own_user_path": "/users/12345", "service_urls": ["/services/4371816"],
        "messages": [{"message_id": "m1", "author_path": "/users/67890",
                      "sent_at": "2026-09-01 20:00:00", "body": "相談です"}],
    }, "https://coconala.com/mypage/direct_message/123")
    transcript = reply_transcript.transcript_row(
        talkroom_id="123", context=context, outgoing_body="承知しました",
        outgoing_hash="a" * 64, sent_at=10, status="composed",
    )
    transcripts = tmp_path / "transcripts.jsonl"
    transcripts.write_text(json.dumps(transcript, ensure_ascii=False) + "\n")
    earnings = tmp_path / "earnings.jsonl"
    earnings.write_text(json.dumps({"talkroom_id": "123", "requestId": "order-1",
                                    "idem_key": "receipt-1", "jpy": 1000,
                                    "net_of_fee": True, "ts": 20}) + "\n")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")

    result = direct._join_funnel(
        tmp_path / "state", [{"service_id": "4371816", "service_version_sha256": "b" * 64}],
        transcripts, empty, earnings, tmp_path / "projects", 30,
        negotiate_run_log=tmp_path / "missing.jsonl",
    )

    assert result["by_origin"]["storefront"] == {
        "inquiries": 1, "payments": 1, "net_jpy": 1000.0,
    }
    events = [json.loads(line) for line in (tmp_path / "state/funnel-events.jsonl").read_text().splitlines()]
    assert {event["service_id"] for event in events} == {"4371816"}
    replay = direct._join_funnel(
        tmp_path / "state", [{"service_id": "4371816", "service_version_sha256": "b" * 64}],
        transcripts, empty, earnings, tmp_path / "projects", 31,
        negotiate_run_log=tmp_path / "missing.jsonl",
    )
    assert replay["appended"] == 0


def test_catalog_baseline_keeps_latest_official_known_metrics(tmp_path):
    analytics = tmp_path / "analytics.jsonl"
    known = {
        "views": {"status": "known", "value": 7},
        "favorites": {"status": "known", "value": 1},
        "purchases": {"status": "known", "value": 0},
    }
    unavailable = {
        name: {"status": "unavailable", "value": None}
        for name in ("views", "favorites", "purchases")
    }
    analytics.write_text(
        json.dumps({"service_id": "1", "official": True, "observed_at_epoch": 1,
                    "metrics": known}) + "\n"
        + json.dumps({"service_id": "1", "official": False, "observed_at_epoch": 2,
                      "metrics": unavailable}) + "\n"
    )

    baseline = direct._catalog_conversion_baseline(
        analytics,
        [{"service_id": "1", "title": "service", "category": "cat",
          "price_jpy": 1000, "state": "published", "service_version_sha256": "a" * 64}],
    )

    assert baseline["services"][0]["views"] == 7
    assert baseline["services"][0]["observed_at_epoch"] == 1


def test_transient_official_inventory_failure_retries_without_success_claim():
    assert direct._storefront_failure_disposition(
        "official_inventory_empty_or_invalid"
    ) == ("pending", 0)
    assert direct._storefront_failure_disposition("unexpected") == ("failed", 1)


def test_analytics_collection_degrades_instead_of_aborting_on_slow_browser_call(
    tmp_path, monkeypatch,
):
    """A browser call that keeps timing out must end this listing's analytics as
    unavailable, not raise out of `_collect_analytics` and abort the whole wake.

    Production wakes crashed with exit 1 because `subprocess.TimeoutExpired` from the
    default-tab "open" call escaped the retry loop on the very first attempt instead of
    being treated as a failed attempt.
    """
    def always_times_out(argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    monkeypatch.setattr(direct.subprocess, "run", always_times_out)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    result = direct._collect_analytics(
        state_dir, evidence_dir, 1_700_000_000, [direct.TARGET_SERVICE_ID],
    )

    assert result["metrics"]["views"] == {
        "status": "unavailable", "value": None,
        "reason": "official_readback_failed_after_retries",
    }
    rows = [json.loads(line) for line in
            (state_dir / "analytics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["official"] is False


def _args(tmp_path: Path):
    """Start from the real CLI contract so the fixture cannot drift from the runtime."""
    args = direct.build_parser().parse_args([])
    args.pass_id = "storefront-test"
    args.state_dir = tmp_path / "state"
    args.output = None
    args.operator_brake = tmp_path / "storefront.operator.brake"
    args.ensure_browser_script = tmp_path / "ensure-browser.sh"
    args.ensure_browser_script.write_text("#!/bin/sh\nprintf 'ALIVE\\n'\n", encoding="utf-8")
    args.lease_script = tmp_path / "lease.py"
    args.runner = tmp_path / "runner.py"
    args.schema = tmp_path / "schema.json"
    args.workdir = tmp_path
    args.timeout_seconds = 10
    args.capability_evidence = []
    args.effect = False
    # Never let a unit test reach the real outbox, receipts or state.
    args.telegram_database = tmp_path / "telegram-outbox.sqlite3"
    args.telegram_receipt_dir = tmp_path / "telegram-receipts"
    args.telegram_target = ""
    return args


@pytest.mark.parametrize("case", ("missing", "duplicate", "substituted"))
def test_invalid_official_contract_fails_before_downstream_work_and_releases_lease(
    tmp_path, monkeypatch, case,
):
    lease = {"ok": True, "ws": "ws://127.0.0.1/page/1", "token": "t", "generation": 2,
             "context_id": "c", "target_id": "p"}
    events = []
    downstream = []

    def lease_call(_script, command, task, value=None):
        events.append(command)
        if command == "release":
            return {"ok": True, "released": task}
        return lease

    def source(service_id):
        text = f"サービス内容\nscope {service_id}\n購入にあたってのお願い"
        return {
            "service_id": service_id, "public_url": f"https://coconala.com/services/{service_id}",
            "title": "OpenCV画像認識", "state": "公開中", "price_jpy": 20000,
            "category": "IT相談/プログラミング", "public_text": text,
            "public_content_sha256": direct.hashlib.sha256(text.encode()).hexdigest(),
        }

    primary_id = "99000001"
    duplicate_id = "99000003"
    substituted_id = "99000009"
    services = [{"service_id": primary_id}]
    if case == "missing":
        sources = []
    elif case == "duplicate":
        services = [{"service_id": primary_id}, {"service_id": duplicate_id}]
        sources = [source(primary_id), source(primary_id)]
    else:
        sources = [source(substituted_id)]

    def observe(*, output_path, ws_url, include_contract_sources=False):
        return {"service_count": len(services), "services": services,
                "content_sha256": "a" * 64, "observed_at": "2026-08-15T00:00:00+00:00",
                "_contract_sources": sources if include_contract_sources else []}

    monkeypatch.setattr(direct, "_lease", lease_call)
    def collect(*_args):
        downstream.append("competitors")
        return {"sources": []}

    def own_page(*_args, **_kwargs):
        downstream.append("own_page")
        return {"body": "FAQなし"}

    def recovery(*_args):
        downstream.append("recovery")
        return None

    def invoke(**_kwargs):
        downstream.append("judge")
        return judgement

    judgement = {
        "decision": "change", "service_id": direct.TARGET_SERVICE_ID, "changed_field": "FAQ",
        "before_value": "FAQ_ABSENT", "proposed_value": "Q. 準備は？\nA. 対象画像をご共有ください。",
        "hypothesis": "h", "competitor_evidence_paths": [], "capability_evidence_paths": [],
        "success_metric": "inquiries", "observation_window_days": 7,
        "no_op_reason": None, "experiment_key": "storefront:test", "uncertainty": [],
    }

    monkeypatch.setattr(direct, "_collect_competitors", collect)
    monkeypatch.setattr(direct, "_observe_own_page", own_page)
    monkeypatch.setattr(direct, "_pending_recovery", recovery)
    monkeypatch.setattr(direct, "_invoke_judge", invoke)
    monkeypatch.setattr(direct, "_guard_judgement", lambda *_args, **_kwargs: judgement)
    monkeypatch.setattr(direct, "_execute_faq_effect", lambda **_kwargs: downstream.append("effect"))
    monkeypatch.setattr(direct, "_dispatch_report", lambda *_args: {
        "status": "delivery_unknown", "message_id": None, "event_key": "synthetic",
    })
    monkeypatch.setattr(direct, "_preflight_storefront_bundle", lambda: None)
    monkeypatch.setattr(
        direct.subprocess, "run",
        lambda argv, **_kwargs: direct.subprocess.CompletedProcess(argv, 0, "ALIVE\n", ""),
    )
    import listing_inventory
    monkeypatch.setattr(listing_inventory, "observe_storefront", observe)

    args = _args(tmp_path)
    args.effect = True
    code, row = direct.run_once(args)

    assert code == 1
    assert row["status"] == "failed"
    assert row["reason"] == "official_service_contract_invalid"
    assert events == ["acquire", "release"]
    assert downstream == []


def test_storefront_brake_prevents_lease_and_observation(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.operator_brake.write_text("held")
    monkeypatch.setattr(direct, "_operator_brake_status", lambda _path: "held")
    blocked = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError())
    monkeypatch.setattr(direct, "_preflight_storefront_bundle", blocked)
    monkeypatch.setattr(direct, "_lease", blocked)
    monkeypatch.setattr(direct.subprocess, "run", blocked)

    code, row = direct.run_once(args)

    assert code == 0, row
    assert row["status"] == "operator_brake" and row["effect"] == 0


@pytest.mark.parametrize(
    ("brake_status", "expected"),
    (("held", "operator_brake"), ("free", None), ("failed", "operator_brake_check_failed")),
)
def test_storefront_effect_gate_combines_disk_and_expiring_brake(monkeypatch, tmp_path,
                                                                 brake_status, expected):
    args = _args(tmp_path)
    monkeypatch.setattr(direct, "disk_headroom_ok", lambda: True)
    monkeypatch.setattr(direct, "_operator_brake_status", lambda _path: brake_status)

    assert direct._effect_gate_reason(args) == expected


def test_incremental_storefront_wake_validates_inventory_releases_lease_and_persists(tmp_path, monkeypatch):
    """A successful no-op wake keeps the public observation lifecycle intact.

    The inventory boundary is synthetic, but contract-ledger and receipt persistence are
    production code.  No browser, private bundle, or network transport can run here.
    """
    # The production storefront lane always launches with GIG_STOREFRONT_ROOT set to a real
    # bundle path, which routes run_once() around the from-scratch `public_bootstrap` catalog
    # -import path (see storefront_direct.py's `public_bootstrap = not os.environ.get(
    # "GIG_STOREFRONT_ROOT", ...)`). Without this, the test's outcome depends on whichever
    # ambient shell happens to invoke pytest -- unset in a fresh worktree, it silently falls
    # into the bootstrap-import-agent branch and tries to run a real agent. Pin it explicitly so
    # this test exercises the same configured-root path the running loop actually takes.
    storefront_root = tmp_path / "storefront-bundle"
    _storefront_bundle(storefront_root)
    monkeypatch.setenv("GIG_STOREFRONT_ROOT", str(storefront_root))
    args = _args(tmp_path)
    args.incremental = True
    args.accounting_cutoff_epoch = 1_700_000_000
    args.listing_contract_dir = tmp_path / "listing-contracts"
    args.listing_contract_families = tmp_path / "families.json"
    args.reply_transcripts = tmp_path / "reply-transcripts.jsonl"
    args.applied = tmp_path / "applied.jsonl"
    args.earnings = tmp_path / "earnings.jsonl"
    args.projects_dir = tmp_path / "projects"
    args.negotiate_context_acks = tmp_path / "context-acks.jsonl"
    events: list[str] = []
    service_id = "90000001"
    # The incremental path now also binds a `_catalog_conversion_baseline` (2026-08-27
    # cbeb8838e, hardened 2026-08-28 4792e22c0), which requires an `"official": True` analytics
    # row with fully known views/favorites/purchases for every currently listed service, or the
    # whole wake fails closed with `storefront_catalog_baseline_incomplete`. Seed one so this
    # no-op wake reaches the same success path the running loop reaches once analytics is warm.
    args.state_dir.mkdir(parents=True)
    (args.state_dir / "analytics.jsonl").write_text(json.dumps({
        "service_id": service_id, "observed_at_epoch": 1_699_999_000, "official": True,
        "metrics": {"views": {"status": "known", "value": 1},
                    "favorites": {"status": "known", "value": 0},
                    "purchases": {"status": "known", "value": 0}},
    }) + "\n", encoding="utf-8")
    public_text = "サービス内容\nsynthetic scope\n購入にあたってのお願い"
    source = {
        "service_id": service_id,
        "public_url": f"https://coconala.com/services/{service_id}",
        "title": "synthetic listing",
        "state": "公開中",
        "price_jpy": 1000,
        "category": "IT相談/プログラミング",
        "public_text": public_text,
        "public_content_sha256": direct.hashlib.sha256(public_text.encode()).hexdigest(),
    }
    lease = {
        "ok": True, "ws": "ws://127.0.0.1/page/1", "token": "synthetic-token",
        "generation": 1, "context_id": "synthetic-context", "target_id": "synthetic-target",
    }

    def lease_call(_script, command, task, value=None):
        events.append(command)
        if command == "release":
            assert value == lease
            return {"ok": True, "released": task}
        return lease

    def browser(argv, **_kwargs):
        if argv[-1] == "close-owned":
            events.append("reclaim")
            return direct.subprocess.CompletedProcess(argv, 0, '{"ok":true}\n', "")
        assert argv == ["/bin/bash", str(args.ensure_browser_script)]
        events.append("browser")
        return direct.subprocess.CompletedProcess(argv, 0, "ALIVE\n", "")

    def observe(*, output_path, ws_url, include_contract_sources=False):
        assert ws_url == lease["ws"] and include_contract_sources is True
        events.append("observe")
        return {
            "service_count": 1, "services": [{"service_id": service_id}],
            "content_sha256": "a" * 64, "observed_at": "2026-08-19T00:00:00+00:00",
            "_contract_sources": [source],
        }

    listing_contract = {
        "service_id": service_id, "service_version_sha256": "b" * 64,
        "contract_key": "synthetic-contract", "offer": {}, "inquiry_playbook": {},
    }
    monkeypatch.setattr(direct, "_preflight_storefront_bundle", lambda: None)
    monkeypatch.setattr(direct.subprocess, "run", browser)
    monkeypatch.setattr(direct, "_lease", lease_call)
    monkeypatch.setattr(direct, "_load_listing_contracts", lambda *_args: [listing_contract])
    monkeypatch.setattr(direct, "_join_funnel", lambda *_args, **_kwargs: {"version": 1})
    monkeypatch.setattr(direct, "_allocate_portfolio", lambda *_args, **_kwargs: {"version": 1})
    monkeypatch.setattr(direct, "_dispatch_report", lambda *_args: {"status": "suppressed"})
    import listing_inventory
    monkeypatch.setattr(listing_inventory, "observe_storefront", observe)

    code, row = direct.run_once(args)

    assert code == 0, row
    assert row["status"] == "completed" and row["effect"] == row["readback"] == 0
    assert row["official_services_read"] == 1
    assert row["lease"]["released"] is True
    assert events == ["browser", "reclaim", "acquire", "observe", "release"]
    assert len((args.state_dir / "offer-contracts.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads((args.state_dir / "current.json").read_text(encoding="utf-8")) == row
    assert json.loads((args.state_dir / "wakes.jsonl").read_text(encoding="utf-8")) == row


def test_storefront_report_reuses_outbox_receipt_and_dedupes_noop(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.telegram_database = tmp_path / "telegram-outbox.sqlite3"
    args.telegram_receipt_dir = tmp_path / "telegram-delivery-receipts"
    args.telegram_target = "42"
    calls = []

    def send(message, *, chat_id, env_file=None):
        calls.append((message, chat_id, env_file))
        return SimpleNamespace(started=True, provider_id="provider-1", error=None)

    monkeypatch.setattr(direct, "send_via_shared_client", send)
    row = direct._receipt(
        "scheduled-1", status="completed", decision="no_op", effect=0, readback=0,
        duplicate=0, actionable=0, official_services_read=11,
        inventory_content_sha256="a" * 64,
    )
    first = direct._dispatch_report(args, row)
    second = direct._dispatch_report(args, {**row, "pass_id": "scheduled-2"})

    assert first == {"status": "sent", "message_id": "provider-1",
                     "event_key": second["event_key"]}
    assert second["status"] == "deduped" and second["message_id"] == "provider-1"
    assert len(calls) == 1
    receipts = list(args.telegram_receipt_dir.glob("*.json"))
    assert len(receipts) == 1 and receipts[0].stat().st_mode & 0o777 == 0o600
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["event_key"] == first["event_key"]
    assert receipt["message_id"] == "provider-1"


def test_storefront_source_has_no_openclaw_transport():
    source = (SCRIPTS / "storefront_direct.py").read_text(encoding="utf-8")
    assert "args.openclaw" not in source
    assert '"message", "send"' not in source
    assert "--openclaw" not in source
    assert "send_via_shared_client" in source


def test_persisted_wake_contains_transport_result_without_changing_effect(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.telegram_database = tmp_path / "telegram-outbox.sqlite3"
    monkeypatch.setattr(direct, "_dispatch_report", lambda _args, _row: {
        "status": "delivery_unknown", "message_id": None, "event_key": "stable-effect",
    })
    row = direct._receipt("effect-1", status="completed", effect=1, readback=1, duplicate=0)

    persisted = direct._persist_receipt(args, args.state_dir / "current.json", row)

    assert persisted["effect"] == persisted["readback"] == 1
    assert persisted["telegram"]["status"] == "delivery_unknown"
    assert json.loads((args.state_dir / "wakes.jsonl").read_text(encoding="utf-8")) == persisted


def test_direct_source_has_no_legacy_or_cross_lane_dependency():
    source = (SCRIPTS / "storefront_direct.py").read_text(encoding="utf-8")
    for forbidden in ("ai.hermes.gateway", "gig_pass.sh", "b0_objective", "b0_result_gate",
                      "shuppin.jsonl", "/operator.brake", "application_parent"):
        assert forbidden not in source
    assert "before_new_listing_draft" in source
    assert "pending_effect is None" in source
    assert "effect_already_this_wake" in source
    assert "draft_effect_this_wake" in source
    assert "retire_attempted_this_wake" in source
    assert "retire_effect_this_wake" in source
    # Assert the *intent* -- the effect fence precedes its guarded create/publish call on the
    # path that actually runs -- rather than comparing raw string offsets in this ~450k-character
    # file. A whole-file `source.index(...)` comparison is wrong on two counts: the searched-for
    # names also appear earlier as plain text inside a leading vocabulary comment (around line
    # 1418: "...create_or_claim_blank_draft" / "...publish_draft (published)..."), and each call
    # has a second, real call site that is not behind these fences at all (see below). Scoping the
    # search to start at the fenced branch's own unique anchor sidesteps both problems: it can only
    # match the call inside that branch, so the assertion is honest about which call site it covers.
    create_branch = source[source.index(
        'if create_proposal.get("decision") == "create" and args.effect:'
    ):]
    assert create_branch.index("before_blank_draft_create") < create_branch.index(
        "create_or_claim_blank_draft"
    )
    publish_branch = source[source.index("publication_guard = ("):]
    assert publish_branch.index("before_new_listing_publish") < publish_branch.index("publish_draft")

    # Both `create_or_claim_blank_draft` and `publish_draft` also have a second call site, on the
    # empty-shelf bootstrap path (`observed == 0` with a positive, known official demand score, in
    # the `category_record is None` branch of that bootstrap). That path is real -- it runs the
    # first time the storefront wakes with zero listings -- but it is not gated by
    # `before_blank_draft_create` / `before_new_listing_publish`; it is dormant only because the
    # shelf being non-empty (`observed == 0` is False) short-circuits it once any listing exists.
    # Pin the call count so a third, silently-unfenced call site cannot be added later without
    # this test noticing.
    assert source.count("create_or_claim_blank_draft(") == 2
    assert source.count("storefront_draft.publish_draft(") == 2


def test_launchagent_is_immutable_dedicated_and_storefront_braked(monkeypatch):
    """Assert the job that gets installed, not a template sitting beside it.

    This read a checked-in plist under launchd/ that had drifted from what
    production ran -- it was missing --full-interval-seconds -- so these
    properties were being asserted about a file nothing installs. They are worth
    asserting; they just have to be asserted about what the manifest renders.
    """
    import gig_release

    monkeypatch.setattr(gig_release, "OVERRIDES", Path("/nonexistent/install.json"))
    release = Path("/release")
    manifest, table = gig_release.settings(release)
    job = next(row for row in manifest["jobs"]
               if row["label"] == "ai.anicca.hf-gig-storefront-direct")
    data = gig_release.plist_for(job, table)
    argv = data["ProgramArguments"]
    env = data["EnvironmentVariables"]

    assert data["Label"] == "ai.anicca.hf-gig-storefront-direct"
    # Minute cadence with an auto-cadence full wake, not a thirty-minute scheduler.
    assert data["StartInterval"] == 60
    # Pin the interpreter literally. Comparing against table["PYTHON"] is what
    # plist_for() substituted from, so that assertion could never fail and a
    # regression in the manifest's default would go straight through.
    assert argv == ["/opt/homebrew/bin/python3",
                    f"{release}/skills/earn/gig/scripts/gig_disk_guard.py",
                    "/opt/homebrew/bin/python3",
                    f"{release}/skills/earn/gig/scripts/storefront_direct.py",
                    "--effect", "--auto-cadence", "--full-interval-seconds", "60"]
    assert env["GIG_OPERATOR_BRAKE_FILE"].endswith("/storefront.operator.brake")
    assert env["GIG_IGNORE_DISK_PRESSURE_BLOCK"] == "1"
    assert env["GIG_IGNORE_DISK_WRITERS_STOP"] == "1"
    assert env["GIG_DISK_HEADROOM_KIB"] == "524288"
    assert job["env"]["GIG_STOREFRONT_CAPABILITY_EVIDENCE"] == (
        "{{GIG_STOREFRONT_CAPABILITY_EVIDENCE}}"
    )
    assert "GIG_STOREFRONT_CAPABILITY_EVIDENCE" not in env
    assert not any("BUDGET" in key for key in env)
    serialized = json.dumps(data, ensure_ascii=False)
    for forbidden in ("hermes", "gig_pass.sh", '"/operator.brake"', "worktree"):
        assert forbidden not in serialized


def test_competitor_sources_are_fresh_owned_official_and_exclude_own(tmp_path, monkeypatch):
    import listing_inventory

    monkeypatch.setattr(direct, "COMPETITOR_SOURCES", (
        ("category", "https://coconala.com/categories/230/66"),
        ("service", "https://coconala.com/services/222"),
    ))

    async def observed(_ws, url, _expression):
        return {"url": url, "title": "official", "body": f"fresh body {url}"}

    monkeypatch.setattr(listing_inventory, "_eval_json", observed)
    manifest = direct._collect_competitors("ws://leased", tmp_path, {"111"})

    assert len(manifest["sources"]) == 2
    for source in manifest["sources"]:
        row = json.loads(Path(source["path"]).read_text())
        assert row["official"] is row["observed"] is True
        assert row["content_sha256"] == source["content_sha256"]
    monkeypatch.setattr(direct, "COMPETITOR_SOURCES", (("service", "https://coconala.com/services/111"),))
    try:
        direct._collect_competitors("ws://leased", tmp_path, {"111"})
    except RuntimeError as error:
        assert str(error) == "competitor_source_is_own_service"
    else:
        raise AssertionError("own service accepted as competitor")


def test_one_variable_guard_allows_other_service_and_fences_same_service(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    competitor = evidence / "competitor.json"
    competitor.write_text(json.dumps({"official": True, "observed": True,
                                      "observed_at_epoch": 100, "content_sha256": "a" * 64}))
    capability = tmp_path / "capability.json"
    capability.write_text("{}")
    manifest = {"sources": [{"path": str(competitor), "content_sha256": "a" * 64}]}
    value = {
        "decision": "change", "service_id": direct.TARGET_SERVICE_ID, "changed_field": "FAQ",
        "before_value": "FAQ_ABSENT", "proposed_value": "Q. 何を準備しますか？\nA. 対象画像と期待結果をご共有ください。",
        "hypothesis": "事前入力を明確にすると問い合わせ摩擦が下がる",
        "competitor_evidence_paths": [str(competitor)], "capability_evidence_paths": [str(capability)],
        "success_metric": "inquiries", "observation_window_days": 7,
        "no_op_reason": None, "experiment_key": None, "uncertainty": [],
    }
    effects = tmp_path / "effects.jsonl"

    accepted = direct._guard_judgement(
        value, own_page={"body": "FAQなし"}, competitor_manifest=manifest,
        capability_paths={str(capability)}, evidence_dir=evidence, effects_path=effects,
        minimum_epoch=100, now=200,
    )
    assert accepted["decision"] == "change"
    assert accepted["experiment_key"].startswith(f"storefront:v1:{direct.TARGET_SERVICE_ID}:FAQ:")

    effects.write_text(json.dumps({"status": "accepted", "effect": 1,
                                   "accepted_at_epoch": 190, "service_id": "other",
                                   "experiment_key": "other"}) + "\n")
    independent = direct._guard_judgement(
        value, own_page={"body": "FAQなし"}, competitor_manifest=manifest,
        capability_paths={str(capability)}, evidence_dir=evidence, effects_path=effects,
        minimum_epoch=100, now=200,
    )
    assert independent["decision"] == "change"

    effects.write_text(json.dumps({"status": "accepted", "effect": 1,
                                   "accepted_at_epoch": 190, "service_id": direct.TARGET_SERVICE_ID,
                                   "experiment_key": "different-experiment"}) + "\n")
    blocked = direct._guard_judgement(
        value, own_page={"body": "FAQなし"}, competitor_manifest=manifest,
        capability_paths={str(capability)}, evidence_dir=evidence, effects_path=effects,
        minimum_epoch=100, now=200,
    )
    assert blocked["decision"] == "no_op"
    assert blocked["no_op_reason"] == "service_cooldown_7d"


def test_judge_result_must_be_fresh_and_owned(tmp_path):
    runner = tmp_path / "runner.py"
    result = {"decision": "no_op", "service_id": None, "changed_field": None,
              "before_value": None, "proposed_value": None, "hypothesis": "insufficient evidence",
              "competitor_evidence_paths": [], "capability_evidence_paths": [],
              "success_metric": None, "observation_window_days": None,
              "no_op_reason": "insufficient evidence", "experiment_key": None, "uncertainty": []}
    runner.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "a=sys.argv;e=Path(a[a.index('--evidence-dir')+1]);e.mkdir(parents=True,exist_ok=True)\n"
        f"r=e/'result.json';r.write_text(json.dumps({result!r}))\n"
        "(e/'summary.json').write_text(json.dumps({'status':'success','result_path':str(r)}))\n"
    )
    evidence = tmp_path / "judge"
    value = direct._invoke_judge(
        runner=runner, schema=tmp_path / "schema.json", workdir=tmp_path, evidence_dir=evidence,
        own_page={"url": "https://coconala.com/services/91000001", "body": "own"},
        manifest={"sources": []}, capability_paths=set(), timeout_seconds=10,
    )
    assert value == result


def test_safe_noop_normalizes_non_effect_service_metadata(tmp_path):
    value = {
        "decision": "no_op", "service_id": direct.TARGET_SERVICE_ID,
        "changed_field": None, "before_value": None, "proposed_value": None,
        "hypothesis": "current FAQ already exists", "competitor_evidence_paths": [],
        "capability_evidence_paths": [], "success_metric": None,
        "observation_window_days": None, "no_op_reason": "nothing to change",
        "experiment_key": None, "uncertainty": [],
    }
    guarded = direct._guard_judgement(
        value, own_page={"body": "よくある質問"}, competitor_manifest={"sources": []},
        capability_paths=set(), evidence_dir=tmp_path, effects_path=tmp_path / "effects.jsonl",
        minimum_epoch=0, now=1,
    )
    assert guarded["decision"] == "no_op" and guarded["service_id"] is None


def test_a_service_with_an_open_experiment_is_not_selected_again(tmp_path):
    """The selector skips a listing whose experiment is still running rather than re-picking it.

    An earlier design returned that listing anyway with a guard reason, which made the
    measurement window read as work. When it is the only candidate the answer is nothing.
    """
    scorecard = tmp_path / "scorecard.json"
    scorecard.write_text(json.dumps({"priority_backlog": [
        {"priority": 1, "service_id": "91000001", "field": "image", "before": 0,
         "success_metric": "views_to_inquiry", "reason": "verified gap"},
    ]}))
    effects = tmp_path / "effects.jsonl"
    effects.write_text(json.dumps({
        "status": "accepted", "effect": 1, "service_id": "91000001",
        "changed_field": "FAQ", "experiment_key": "faq-live", "accepted_at_epoch": 1,
    }) + "\n")
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text(json.dumps({"experiment_key": "faq-live", "terminal": False}) + "\n")
    contracts = [{"service_id": "91000001", "service_version_sha256": "a" * 64}]
    # Far enough past the change that the seven-day hold on that listing has expired, so the
    # open experiment is the only thing keeping it out of the selection.
    now = 1 + 604_800 + 1

    assert direct._prepare_next_hypothesis(scorecard, effects, outcomes, contracts, now) is None

    outcomes.write_text(json.dumps({"experiment_key": "faq-live", "terminal": True}) + "\n")
    reopened = direct._prepare_next_hypothesis(scorecard, effects, outcomes, contracts, now)
    assert reopened is not None
    assert reopened["service_id"] == "91000001" and reopened["field"] == "image"

def test_presend_guard_rejects_a_stale_faq_absence():
    judgement = {"decision": "change", "service_id": direct.TARGET_SERVICE_ID,
                 "changed_field": "FAQ", "before_value": "FAQ_ABSENT"}
    try:
        direct._presend_guard(judgement, {"body": "現在のよくある質問"})
    except RuntimeError as error:
        assert str(error) == "presend_current_value_changed"
    else:
        raise AssertionError("stale FAQ absence accepted")


def test_service_contract_binds_official_scope_price_and_dedupes(tmp_path):
    text = "ホーム\nIT相談\n画像認識\nタイトル\nキャッチ\n評価 -\nサービス内容\nOpenCV PoC\n購入にあたってのお願い"
    source = {
        "service_id": "91000001", "public_url": "https://coconala.com/services/91000001",
        "title": "OpenCV画像認識", "state": "公開中", "price_jpy": 20000,
        "category": "IT相談/プログラミング", "public_text": text,
        "public_content_sha256": direct.hashlib.sha256(text.encode()).hexdigest(),
    }
    contract = direct._service_contract(source, "2026-08-15T00:00:00+00:00")
    changed = direct._service_contract({**source, "price_jpy": 30000}, "2026-08-15T00:01:00+00:00")
    assert contract["service_version_sha256"] != changed["service_version_sha256"]
    path = tmp_path / "offer-contracts.jsonl"
    assert direct._append_contract_once(path, contract) is True
    assert direct._append_contract_once(path, contract) is False
    assert len(path.read_text().splitlines()) == 1


def test_service_contract_accepts_current_paused_dashboard_state():
    cards = listing_inventory.parse_list_page(
        "出品サービス一覧\n受付休止中\n0\n5,000\n円\nサービス名\n編集する 公開設定 シェア\n",
        ["91000001"],
    )
    text = "サービス内容\nscope\n購入にあたってのお願い"
    contract = direct._service_contract({
        **cards[0], "public_url": "https://coconala.com/services/91000001",
        "category": "IT相談/プログラミング", "public_text": text,
        "public_content_sha256": direct.hashlib.sha256(text.encode()).hexdigest(),
    }, "2026-09-02T12:29:31+00:00")
    assert contract["state"] == "受付休止中"


def test_service_contract_requires_exact_coconala_heading_lines():
    invalid_texts = (
        "サービス内容\nOpenCV PoC",
        "購入にあたってのお願い\nOpenCV PoC",
        "サービス内容（補足）\n購入にあたってのお願い",
        "サービス内容\n購入にあたってのお願い（補足）",
        "前置きサービス内容\n購入にあたってのお願い",
        "サービス内容\n前置き購入にあたってのお願い",
    )
    for text in invalid_texts:
        source = {
            "service_id": "91000001", "public_url": "https://coconala.com/services/91000001",
            "title": "OpenCV画像認識", "state": "公開中", "price_jpy": 20000,
            "category": "IT相談/プログラミング", "public_text": text,
            "public_content_sha256": direct.hashlib.sha256(text.encode()).hexdigest(),
        }
        try:
            direct._service_contract(source, "2026-08-15T00:00:00+00:00")
        except RuntimeError as error:
            assert str(error) == "official_service_contract_invalid"
        else:
            raise AssertionError(f"near-match public text accepted: {text!r}")


def test_exact_faq_is_the_only_seller_field_and_public_delta():
    question, answer = direct._split_faq("よくある質問\n\nQ. 何を準備しますか？\nA. 対象画像をご共有ください。")
    base = [{"name": "data[Service][overview]", "value": "same", "checked": False}]
    before_form = {"url": "https://coconala.com/mypage/services/91000001", "fields": base}
    after_form = {"url": before_form["url"], "fields": base + [
        {"name": "data[Faq][0][question]", "value": question, "checked": False},
        {"name": "data[Faq][0][answer]", "value": answer, "checked": False},
    ]}
    direct._validate_form_delta(before_form, after_form, question, answer)
    direct._validate_public_acceptance(
        {"url": "https://coconala.com/services/91000001", "body": "before", "content_sha256": "a" * 64},
        {"url": "https://coconala.com/services/91000001", "body": question + answer, "content_sha256": "b" * 64},
        question, answer,
    )

    changed = {"url": before_form["url"], "fields": [
        {"name": "data[Service][overview]", "value": "different", "checked": False},
        *after_form["fields"][1:],
    ]}
    try:
        direct._validate_form_delta(before_form, changed, question, answer)
    except RuntimeError as error:
        assert str(error) == "seller_form_non_faq_changed"
    else:
        raise AssertionError("second changed field accepted")


def test_pending_effect_recovers_only_from_both_exact_public_values(tmp_path):
    intent_path = direct._effect_intent_path(tmp_path, "experiment")
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "status": "prepared", "question": "exact question", "answer": "exact answer",
        "experiment_key": "experiment",
    }))
    assert direct._pending_recovery(tmp_path, {"body": "exact question\nexact answer"})["intent_path"] == str(intent_path)
    assert direct._pending_recovery(tmp_path, {"body": "neither"}) is None
    try:
        direct._pending_recovery(tmp_path, {"body": "exact question only"})
    except RuntimeError as error:
        assert str(error) == "pending_effect_partial_public_readback"
    else:
        raise AssertionError("partial effect accepted")


def test_pending_recovery_skips_intent_superseded_by_confirmed_effect(tmp_path):
    intents = tmp_path / "effect-intents"
    intents.mkdir()
    base = {"service_id": "4302213", "changed_field": "title"}
    (intents / "old-prepared.json").write_text(json.dumps({
        **base, "status": "prepared", "prepared_at_epoch": 10,
    }))
    (intents / "new-confirmed.json").write_text(json.dumps({
        **base, "status": "confirmed", "prepared_at_epoch": 20,
    }))

    assert direct._pending_recovery(tmp_path, {"body": "latest confirmed public value"}) is None


def test_public_observation_expands_folded_faq_before_readback(tmp_path, monkeypatch):
    import listing_inventory

    seen = {}

    async def observed(_ws, url, expression):
        seen["expression"] = expression
        return {
            "url": url, "title": "official", "body": "exact question\nexact answer",
            "service_image_ids": [],
        }

    monkeypatch.setattr(listing_inventory, "_eval_json", observed)
    row = direct._observe_own_page("ws://leased", tmp_path)

    assert 'aria-controls^="serviceContentsFaqAnswer"' in seen["expression"]
    assert 'aria-expanded="false"' in seen["expression"]
    assert row["body"] == "exact question\nexact answer"
    assert row["service_image_count"] == 0


def test_effect_ledger_append_is_idempotent(tmp_path):
    ledger = tmp_path / "effects.jsonl"
    row = {"status": "accepted", "effect": 1, "experiment_key": "same"}
    assert direct._append_effect_once(ledger, row) is True
    assert direct._append_effect_once(ledger, row) is False
    assert len(ledger.read_text().splitlines()) == 1
    ledger.write_text("not-json\n")
    try:
        direct._append_effect_once(ledger, row)
    except RuntimeError as error:
        assert str(error) == "effect_ledger_invalid"
    else:
        raise AssertionError("corrupt effect ledger ignored")


def test_storefront_collects_its_own_evidence(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(direct.evidence_gc, "collect", lambda **kwargs: calls.append(kwargs))
    current = tmp_path / "evidence" / "storefront-direct-current"

    direct._collect_storefront_evidence(tmp_path, current)

    assert calls == [{"state_dir": tmp_path,
                      "evidence_root": tmp_path / "evidence",
                      "current_evidence_dir": current}]


def test_verified_image_contract_becomes_one_exact_image_judgement(monkeypatch):
    """The judgement itself; the contract validator has its own tests."""
    monkeypatch.setattr(direct, "_validate_image_mutation_contract", lambda contract, **_kwargs: None)
    contract = {
        "service_id": "91000001", "changed_field": "image",
        "proposed_value": {"asset_sha256": "a" * 64, "asset_path": "assets/hero.png"},
        "contract_sha256": "b" * 64, "success_metric": "views_to_inquiry",
        "observation_window_days": 14,
    }

    judgement = direct._image_judgement({
        "service_id": "91000001", "field": "image", "before": 0,
        "executable": True, "success_metric": "views_to_inquiry",
        "reason": "verified demand + owned quantified claim + 0 images",
    }, contract)

    assert judgement["changed_field"] == "image"
    assert judgement["service_id"] == "91000001"
    assert judgement["before_value"] == 0
    assert judgement["proposed_value"] == "a" * 64
    assert judgement["experiment_key"].startswith("storefront:v1:91000001:image:")
    assert judgement["no_op_reason"] is None


def test_image_form_and_public_readback_accept_only_one_image_delta(monkeypatch):
    monkeypatch.setattr(direct, "_validate_image_mutation_contract", lambda contract, **_kwargs: None)
    contract = {"service_id": "91000001", "changed_field": "image",
                "proposed_value": {"asset_sha256": "a" * 64, "asset_path": "assets/hero.png"}}
    base = [{"name": "data[Service][overview]", "value": "same", "checked": False}]
    before_form = {"url": "https://coconala.com/mypage/services/91000001", "fields": base}
    after_form = {"url": before_form["url"], "fields": base + [{
        "name": "data[UploadedFile][n1][image_files]", "value": "hero-final.png", "checked": False,
    }]}

    direct._validate_image_form_delta(before_form, after_form, contract)

    two_uploads = {"url": before_form["url"], "fields": after_form["fields"] + [{
        "name": "data[UploadedFile][n2][image_files]", "value": "second.png", "checked": False,
    }]}
    try:
        direct._validate_image_form_delta(before_form, two_uploads, contract)
    except RuntimeError as error:
        assert "seller_image_upload_field_invalid" in str(error)
    else:
        raise AssertionError("a second uploaded image was accepted")

    changed_elsewhere = {"url": before_form["url"], "fields": [
        {"name": "data[Service][overview]", "value": "different", "checked": False},
        after_form["fields"][1],
    ]}
    try:
        direct._validate_image_form_delta(before_form, changed_elsewhere, contract)
    except RuntimeError as error:
        assert "seller_image_non_image_changed" in str(error)
    else:
        raise AssertionError("a non-image field changed alongside the image")


def test_reopen_suspended_listings_covers_all_14_and_is_idempotent_when_public(tmp_path, monkeypatch):
    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "ok": True, "ws": "ws://leased", "target_id": "t1", "closed": "t1",
        }))

    async def fake_reopen(_ws_url, *, service_id):
        return {"service_id": service_id, "reopened": True, "readback_state": "公開中"}

    monkeypatch.setattr(direct.subprocess, "run", fake_run)
    monkeypatch.setattr(direct, "_reopen_one_suspended_service_async", fake_reopen)

    suspended = [{"service_id": str(90000000 + i), "state": direct.SUSPENDED_LISTING_STATE}
                 for i in range(14)]
    effects_path = tmp_path / "effects.jsonl"
    count = direct._reopen_suspended_listings(
        suspended, default_tab_script=Path("cdp_default_tab.py"),
        effects_path=effects_path, pass_id="pass-1",
    )
    assert count == 14
    assert len(effects_path.read_text().splitlines()) == 14

    public = [{**row, "state": "公開中"} for row in suspended]
    count_replay = direct._reopen_suspended_listings(
        public, default_tab_script=Path("cdp_default_tab.py"),
        effects_path=effects_path, pass_id="pass-2",
    )
    assert count_replay == 0


def test_create_min_interval_fills_the_shelf_in_hours_not_days(tmp_path):
    # The 20-listing cap minus the 14 already live leaves 6 free slots. One new creation
    # started per CREATE_MIN_INTERVAL_SECONDS must clear all 6 inside a single day.
    assert direct.CREATE_MIN_INTERVAL_SECONDS < 86_400
    remaining_slots = 20 - 14
    assert remaining_slots * direct.CREATE_MIN_INTERVAL_SECONDS <= 24 * 3_600

    state_dir = tmp_path
    now = int(time.time())
    (state_dir / "wakes.jsonl").write_text(json.dumps({
        "new_listing_draft": {
            "public_effect": 1,
            "candidate_key": "storefront:create:v1:abc123",
        },
        "observed_at_epoch": now,
    }) + "\n", encoding="utf-8")

    last_create = direct._last_published_create_epoch(state_dir)
    assert last_create == now

    # Immediately after a publish, spacing must still be closed: this is the runaway guard.
    assert (now - last_create) < direct.CREATE_MIN_INTERVAL_SECONDS

    # Once a full interval has elapsed, spacing opens again for the next of the 6 slots.
    later = now + direct.CREATE_MIN_INTERVAL_SECONDS
    assert (later - last_create) >= direct.CREATE_MIN_INTERVAL_SECONDS


def test_next_unused_demand_cluster_skips_an_already_published_family(tmp_path):
    # mobile_app_dev published as service 4386009 but families.json never gained a service
    # mapping for it, so `_family_traffic_without_sales` could never see it and the loop kept
    # reselecting the same already-public cluster every wake forever: nothing else marked it
    # consumed. `_family_already_published` reads the loop's own creation ledger instead,
    # which every CREATE always updates, and the caller feeds its cluster_key into the
    # existing dismissal set so `_next_unused_demand_cluster` advances to the next family.
    draft_ledger = tmp_path / "new-listing-drafts.jsonl"
    draft_ledger.write_text(json.dumps({
        "candidate_key": "storefront:create:v1:deadbeef",
        "capability_family": "mobile_app_dev",
        "status": "already_public",
        "public_effect": 0,
    }) + "\n", encoding="utf-8")

    assert direct._family_already_published(draft_ledger, "mobile_app_dev") is True
    assert direct._family_already_published(draft_ledger, "mvp_web_app_build") is False
    assert direct._family_already_published(tmp_path / "missing.jsonl", "mobile_app_dev") is False

    stuck_cluster = {
        "cluster_key": "storefront:demand:v1:mobile-app-dev-cluster", "status": "known",
        "score": 12, "median_price_jpy": 60_000, "capability_family": "mobile_app_dev",
    }
    next_cluster = {
        "cluster_key": "storefront:demand:v1:mvp-web-app-build-cluster", "status": "known",
        "score": 12, "median_price_jpy": 30_000, "capability_family": "mvp_web_app_build",
    }
    clusters = [stuck_cluster, next_cluster]

    # Before the fix's caller ever runs, the already-public family still wins the tie-break
    # (higher median_price_jpy), which is exactly how the loop deadlocked on it forever.
    assert direct._next_unused_demand_cluster(clusters, set())["capability_family"] == "mobile_app_dev"

    # Once its cluster_key is dismissed -- what the new `family_already_public` branch does
    # every wake it is reselected -- the next unused catalog family becomes reachable.
    dismissed = {stuck_cluster["cluster_key"]}
    selected = direct._next_unused_demand_cluster(clusters, dismissed)
    assert selected["capability_family"] == "mvp_web_app_build"


def test_create_proposal_prompt_requires_a_grounded_paid_option_on_create():
    # line_bot_dev's proposal returned paid_option_title/paid_option_price_jpy as null on a
    # `create` decision, which the JSON schema allows (both fields type ["string"/"integer",
    # "null"]) but `_seal_create_contract` rejects with storefront_create_content_invalid,
    # because catalog.json carries no paid_options field to ground one and the prompt never
    # told the model create requires it. The fix is at generation time, not validation: make
    # the requirement and a grounded source for it explicit in the prompt the model reads.
    source = {"service_id": "4355225", "service_version_sha256": "b" * 64}
    demand = {"evidence_path": "/tmp/demand-search-example.json"}
    listing_catalog_entry = {
        "title_ja": "LINE Bot・LINE予約システムを開発します",
        "tiers": [
            {"name": "ベーシック", "price_jpy": 50_000},
            {"name": "スタンダード", "price_jpy": 100_000},
        ],
        "deliverables": ["LINE Bot本体", "管理用スプレッドシート/カレンダー連携"],
        "faq": [{"q": "月額費用はかかりますか？", "a": "運用サポートプランをご用意しています(月額)。"}],
    }
    prompt, _ = direct._create_proposal_prompt(
        source, "line_bot_dev", {}, demand, set(), [],
        listing_catalog_entry=listing_catalog_entry,
    )
    assert "paid_option_title and paid_option_price_jpy must both be" in prompt
    assert "no_op` may leave every commercial field null" in prompt
    assert "never invent it" in prompt
