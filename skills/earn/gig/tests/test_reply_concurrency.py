"""Contract tests for fast, durable Coconala inbox discovery."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import argparse
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


GIG_ROOT = Path(__file__).resolve().parents[1]
OUTBOX_PATH = GIG_ROOT / "scripts" / "connector_outbox.py"
SNAPSHOT_PATH = GIG_ROOT / "scripts" / "coconala_queue_snapshot.py"
DETECTOR_PATH = GIG_ROOT / "scripts" / "reply_detector.py"
REQUESTED_ESTIMATE_PATH = GIG_ROOT / "scripts" / "requested_estimate.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


outbox = _load_module("gig_connector_outbox_reply_concurrency_test", OUTBOX_PATH)
snapshot = _load_module("gig_queue_snapshot_reply_concurrency_test", SNAPSHOT_PATH)
detector = _load_module("gig_reply_detector_reply_concurrency_test", DETECTOR_PATH)
requested_estimate = _load_module(
    "gig_requested_estimate_reply_concurrency_test", REQUESTED_ESTIMATE_PATH,
)


def test_default_tab_open_failure_preserves_helper_reason(monkeypatch, tmp_path):
    failure = snapshot.subprocess.CalledProcessError(
        1,
        ["python3", "cdp_default_tab.py", "open"],
        output='{"ok": false, "reason": "Target.createTarget: rate limited"}\n',
        stderr="",
    )

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(snapshot.subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="Target.createTarget: rate limited"):
        with snapshot.DefaultTab(
            tmp_path / "cdp_default_tab.py",
            "https://coconala.com/talkrooms/18223833",
            owner="paid-direct-18223833",
        ):
            pass


def test_attachment_download_temp_lives_under_project(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    observed = {}
    real_temporary_directory = snapshot.tempfile.TemporaryDirectory

    def recording_temporary_directory(*args, **kwargs):
        observed["dir"] = kwargs.get("dir")
        return real_temporary_directory(*args, **kwargs)

    async def fake_call(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(snapshot.tempfile, "TemporaryDirectory", recording_temporary_directory)
    monkeypatch.setattr(snapshot, "call", fake_call)

    asyncio.run(snapshot.capture_click_downloads(object(), 1, {"messages": []}, project_root=project_root))

    assert Path(observed["dir"]).is_relative_to(project_root)


def test_continuous_evidence_prunes_completed_only(tmp_path):
    evidence = tmp_path / "reply-run"
    probes = evidence / "continuous"
    workers = probes / "workers"
    for index in range(12):
        marker = probes / f"probe-{index}" / "head-snapshot.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
    for index in range(20):
        marker = workers / f"worker-{index}" / "result.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
    active = workers / "active"
    active.mkdir()
    link = workers / "linked"
    link.symlink_to(active, target_is_directory=True)

    detector._prune_continuous_evidence(evidence)

    assert len(list(probes.glob("probe-*"))) == 8
    assert len([path for path in workers.iterdir() if path.name.startswith("worker-")]) == 16
    assert active.is_dir()
    assert link.is_symlink()


def _fake_targeted_scripts(
    tmp_path, *, next_action="reply", semantic_failure=None,
    orders_mode="empty", head_identity="b" * 64,
    estimate_required=False, bad_readback=False,
):
    """Fake every subprocess while preserving the real outbox lifecycle."""
    script = tmp_path / "fake_targeted_stage.py"
    calls = tmp_path / "calls.jsonl"
    send_record = tmp_path / "send-record.jsonl"
    connector_path = GIG_ROOT / "scripts" / "connector_outbox.py"
    script.write_text(textwrap.dedent(f"""
        import importlib.util, json, sys, time
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("fake_outbox", {str(connector_path)!r})
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        argv = sys.argv[1:]
        captured_at = (
            "2020-01-01T00:00:00+00:00" if {orders_mode!r} == "stale"
            else time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        )
        with open({str(calls)!r}, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\\n")
        def value(flag):
            return Path(argv[argv.index(flag) + 1])
        if argv and argv[0] == "build":
            snapshot = json.loads(value("--snapshot").read_text())
            row = snapshot["inquiries"][0] if snapshot.get("inquiries") else None
            items = []
            if row and row.get("reply_required") is True:
                item = {{
                    "platform": "coconala", "priority": "P1", "event_type": "buyer_message",
                    "event_key": "coconala:message:v1:123:buyer-2",
                    "coordination_key": "coconala:123",
                    "covered_event_keys": ["coconala:message:v1:123:buyer-2"],
                    "talkroom_id": "123", "talkroom_url": "https://coconala.com/mypage/direct_message/123",
                    "origin_at": row["buyer_sent_at"], "detected_at": snapshot["captured_at"],
                    "next_action": row.get("next_action", "reply"),
                }}
                if row.get("semantic_reply_body"):
                    item["semantic_reply_body"] = row["semantic_reply_body"]
                    item["semantic_context_sha256"] = row["semantic_context_sha256"]
                items.append(item)
            value("--output").write_text(json.dumps({{
                "status": "ready" if items else "queue_empty", "errors": [], "items": items,
                "semantic_ssot": snapshot.get("semantic_ssot") is True,
            }}))
        elif argv and argv[0] == "enqueue":
            queue = json.loads(value("--queue").read_text())
            db = module.ConnectorOutbox(value("--database"), value("--manifest"))
            for item in queue.get("items", []):
                db.enqueue(event_key=item["event_key"], thread_id=item["talkroom_id"],
                           thread_url=item["talkroom_url"], observed_at=int(time.time()))
        elif argv and argv[0] == "build-paid":
            output = value("--output")
            if {orders_mode!r} == "invalid":
                output.write_text(json.dumps({{"version": 1, "fences": "invalid"}}))
            elif {orders_mode!r} == "paid":
                output.write_text(json.dumps({{
                    "version": 1, "fences": [{{
                        "id": "paid-conversation-write:coconala:123",
                        "state": "open", "platform": "coconala",
                        "identities": {{"talkroom_id": "123"}},
                        "capabilities": ["conversation_write"],
                        "reason": "paid room", "opened_at": "2026-08-19T00:00:00+00:00",
                        "release": {{"kind": "event", "event": "paid_order_closed:123"}},
                    }}],
                }}))
            else:
                output.write_text(json.dumps({{"version": 1, "fences": []}}))
        elif "--queue" in argv and "--output" in argv:
            db = module.ConnectorOutbox(value("--database"), value("--manifest"))
            event_key = "coconala:message:v1:123:buyer-2"
            lifecycle = db.action_lifecycle_for_event(event_key, "123")
            action = db.pending_action_for_thread("123")
            action_id = int(action["action_id"]) if action else 1
            if {orders_mode!r} == "paid":
                lane = {{
                    "status": "completed", "replied": 0, "reconciled": 0,
                    "pending_verify": 0, "reconcile_pending": 0, "already_delivered": 0,
                    "nothing_to_say": 0, "failed": 0, "blocked": 1, "deferred": 0, "dlq": 0,
                    "errors": ["paid_talkroom_write_refused"], "events": [], "dlq_events": [],
                }}
            elif lifecycle is not None and lifecycle.get("state") == "replied":
                lane = {{
                    "status": "completed", "replied": 0, "reconciled": 0,
                    "pending_verify": 0, "reconcile_pending": 0, "already_delivered": 1,
                    "nothing_to_say": 0, "failed": 0, "blocked": 0, "deferred": 0, "dlq": 0,
                    "errors": [], "events": [], "dlq_events": [],
                }}
            elif {bad_readback!r}:
                lane = {{
                    "status": "completed", "replied": 1, "reconciled": 0,
                    "pending_verify": 0, "reconcile_pending": 0, "already_delivered": 0,
                    "nothing_to_say": 0, "failed": 0, "blocked": 0, "deferred": 0, "dlq": 0,
                    "errors": [], "events": [], "dlq_events": [],
                }}
            elif lifecycle is not None and lifecycle.get("state") == "pending":
                owner = "fake-lane"
                now = int(time.time())
                claimed = db.claim(owner=owner, now=now, lease_seconds=30, action_id=action_id)
                intent = db.prepare_intent(
                    action_id, owner=owner, fencing_token=int(claimed["fencing_token"]),
                    outgoing_body="回答です", now=now, origin_at=now,
                )
                db.mark_click_started(
                    action_id, int(intent["revision"]), owner=owner,
                    fencing_token=int(claimed["fencing_token"]), now=now,
                )
                db.reconcile(
                    action_id, thread_url="https://coconala.com/mypage/direct_message/123",
                    outgoing_hash=intent["outgoing_hash"], seller_sent_at=now,
                    last_sender="seller", observed_at=now, authoritative_absent=False,
                )
                revision = int(db.get_action(action_id)["revision"])
                with open({str(send_record)!r}, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({{"action_id": action_id, "event_key": event_key}}) + "\\n")
                lane = {{
                    "status": "completed", "replied": 1, "reconciled": 0,
                    "pending_verify": 0, "reconcile_pending": 0, "already_delivered": 0,
                    "nothing_to_say": 0, "failed": 0, "blocked": 0, "deferred": 0, "dlq": 0,
                    "errors": [], "events": [{{
                        "status": "replied", "action_id": action_id, "revision": revision,
                        "talkroom_id": "123", "origin_at": "2026-08-19T00:01:00+00:00",
                        "seller_sent_at": "2026-08-19T00:02:00+00:00",
                    }}], "dlq_events": [],
                }}
            else:
                lane = {{
                    "status": "pending", "replied": 0, "reconciled": 0,
                    "pending_verify": 1, "reconcile_pending": 0, "already_delivered": 0,
                    "nothing_to_say": 0, "failed": 0, "blocked": 0, "deferred": 0, "dlq": 0,
                    "errors": [], "events": [], "dlq_events": [],
                }}
            value("--output").write_text(json.dumps(lane))
        elif "--mode" in argv:
            output = value("--output")
            output.parent.mkdir(parents=True, exist_ok=True)
            mode = argv[argv.index("--mode") + 1]
            if mode == "orders-only" and {orders_mode!r} == "failed":
                raise SystemExit(9)
            if mode == "orders-only":
                complete = {orders_mode!r} != "missing"
                output.write_text(json.dumps({{
                    "version": 1, "collector_mode": "orders-only", "semantic_ssot": False,
                    "captured_at": captured_at, "read_only": True,
                    "orders": [], "inquiries": [],
                    "source_receipt": {{"source": "orders", "coverage_complete": complete,
                        "open_orders_list_observed": complete}},
                }}))
            elif mode == "direct-inbox-head-only":
                output.write_text(json.dumps({{
                    "version": 1, "collector_mode": "direct-inbox-head-only", "semantic_ssot": False,
                    "captured_at": captured_at, "read_only": True,
                    "head_only": True, "orders": [],
                    "source_receipt": {{"source": "direct_inbox", "coverage_complete": False}},
                    "inquiries": [{{
                        "talkroom_id": "123", "talkroom_url": "https://coconala.com/mypage/direct_message/123",
                        "last_message_side": "buyer", "last_message_identity_sha256": {head_identity!r},
                    }}],
                }}))
            elif mode == "direct-thread-head-only":
                output.write_text(json.dumps({{
                    "version": 1, "collector_mode": "direct-thread-head-only", "semantic_ssot": False,
                    "captured_at": captured_at, "read_only": True, "head_only": True,
                    "orders": [], "source_receipt": {{"source": "direct_thread"}},
                    "inquiries": [{{
                        "talkroom_id": "123", "talkroom_url": "https://coconala.com/mypage/direct_message/123",
                        "last_message_side": "buyer", "reply_required": True,
                        "buyer_sent_at": "2026-08-19T00:01:00+00:00",
                        "last_message_identity_sha256": {head_identity!r},
                    }}],
                }}))
            else:
                output.write_text(json.dumps({{
                    "version": 1, "collector_mode": "direct-thread-only", "semantic_ssot": True,
                    "captured_at": captured_at, "read_only": True,
                    "orders": [], "source_receipt": {{"source": "direct_thread"}},
                    "inquiries": [{{
                        "talkroom_id": "123", "talkroom_url": "https://coconala.com/mypage/direct_message/123",
                        "last_message_side": "buyer", "reply_required": {next_action!r} == "reply",
                        "estimate_required": {estimate_required!r},
                        "next_action": {next_action!r}, "buyer_sent_at": "2026-08-19T00:01:00+00:00",
                        "message_id": "buyer-2", "last_message_identity_sha256": "b" * 64,
                        "estimate_request_identity": "buyer-2",
                        "semantic_receipt": {{"judgement": {{"next_action": {next_action!r}}}}},
                        "semantic_failure": {semantic_failure!r},
                        "semantic_context_sha256": "a" * 64,
                        "semantic_reply_body": "回答です" if {next_action!r} == "reply" else None,
                    }}],
                }}))
    """), encoding="utf-8")
    return script, calls, send_record


def _targeted_args(tmp_path, script):
    fences = tmp_path / "fences.json"
    fences.write_text(json.dumps({"version": 1, "fences": []}), encoding="utf-8")
    return argparse.Namespace(
        snapshot_script=script, queue_script=script, lane_script=script,
        fence_script=script, fences=fences, database=tmp_path / "outbox.sqlite3",
        manifest=GIG_ROOT / "config" / "connectors" / "coconala.json",
        runner=GIG_ROOT.parents[2] / "runtime/agent-runner/agent_runner.py",
        semantic_schema=GIG_ROOT / "schemas" / "reply_semantic_judgement.schema.json",
        estimate_schema=GIG_ROOT / "schemas" / "estimate_category_selection.schema.json",
        schema=GIG_ROOT / "schemas" / "reply_composition.schema.json",
        cdp_helper=GIG_ROOT / "scripts" / "cdp_default_tab.py",
        semantic_effects_enabled=True,
    )


def _seed_inbox_action(args):
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    inbox_event_key = outbox.coconala_inbox_event_key("123", "b" * 64)
    action = database.enqueue(
        event_key=inbox_event_key,
        thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=int(time.time()),
    )
    return int(action["action_id"]), inbox_event_key


def _seed_verified_estimate(args, thread_id, request_identity):
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    event_key = outbox.coconala_estimate_event_key(thread_id, request_identity)
    now = int(time.time())
    action = database.enqueue_estimate(
        event_key=event_key, thread_id=thread_id,
        thread_url=f"https://coconala.com/mypage/direct_message/{thread_id}",
        observed_at=now,
    )
    claimed = database.claim(
        owner="estimate-proof", now=now + 1, lease_seconds=30,
        action_id=int(action["action_id"]),
    )
    assert claimed is not None
    intent = database.prepare_intent(
        int(action["action_id"]), owner="estimate-proof",
        fencing_token=int(claimed["fencing_token"]), outgoing_body="見積もりです",
        now=now + 2, origin_at=now + 1,
    )
    database.mark_click_started(
        int(action["action_id"]), int(intent["revision"]), owner="estimate-proof",
        fencing_token=int(claimed["fencing_token"]), now=now + 3,
    )
    database.reconcile(
        int(action["action_id"]),
        thread_url=f"https://coconala.com/mypage/direct_message/{thread_id}",
        outgoing_hash=str(intent["outgoing_hash"]), seller_sent_at=now + 4,
        last_sender="seller", observed_at=now + 5, authoritative_absent=False,
    )
    return event_key, int(action["action_id"]), int(intent["revision"])


def test_targeted_thread_reaches_official_readback(tmp_path):
    script, calls, send_record = _fake_targeted_scripts(tmp_path)
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence", run_id="run-1",
    )
    assert result["status"] == "completed"
    assert result["replied"] == 1
    assert result["thread_id"] == "123"
    assert result["official_readback"] == 1
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    collect = [
        argv for argv in recorded
        if "--mode" in argv
        and argv[argv.index("--mode") + 1] in {
            "direct-thread-only", "direct-inbox-head-only",
        }
    ]
    assert collect
    assert any(
        argv[argv.index("--mode") + 1] == "direct-thread-only"
        and "123" in argv for argv in collect
    )
    evidence = tmp_path / "evidence"
    saved_snapshot = json.loads((evidence / "marketplace-snapshot.json").read_text())
    saved_queue = json.loads((evidence / "reply-queue.json").read_text())
    saved_lane = json.loads((evidence / "reply-lane-result.json").read_text())
    assert saved_snapshot["collector_mode"] == "direct-thread-only"
    assert saved_snapshot["semantic_ssot"] is True
    assert [item["talkroom_id"] for item in saved_snapshot["inquiries"]] == ["123"]
    assert saved_queue["status"] == "ready"
    assert len(saved_queue["items"]) == 1
    assert saved_queue["items"][0]["talkroom_id"] == "123"
    assert saved_lane["status"] == "completed"
    assert saved_lane["replied"] == 1
    assert len(send_record.read_text().splitlines()) == 1


def test_targeted_replay_has_zero_second_effect(tmp_path):
    script, _calls, send_record = _fake_targeted_scripts(tmp_path)
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    first = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence-1", run_id="run-1",
    )
    second = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence-2", run_id="run-2",
    )
    assert first["official_readback"] == 1
    assert second["replied"] == 0
    assert second["duplicate_effect"] == 0
    assert len(send_record.read_text().splitlines()) == 1


def test_intentional_no_send_closes_exact_claim_without_reply(tmp_path):
    script, _calls, _send_record = _fake_targeted_scripts(tmp_path, next_action="wait")
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence", run_id="run-wait",
    )
    assert result["replied"] == 0
    assert result["closed_without_send"] == 1
    assert outbox.ConnectorOutbox(args.database, args.manifest).pending_actions() == []


def test_semantic_failure_stays_pending_without_send(tmp_path):
    script, _calls, _send_record = _fake_targeted_scripts(
        tmp_path, semantic_failure="runner_failed",
    )
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence", run_id="run-failed",
    )
    assert result["status"] == "pending"
    assert result["replied"] == 0
    assert result["pending"] == 1
    assert result["closed_without_send"] == 0
    assert outbox.ConnectorOutbox(args.database, args.manifest).pending_actions()


def test_targeted_paid_fence_hands_off_without_effect(tmp_path):
    script, _calls, _send_record = _fake_targeted_scripts(tmp_path, orders_mode="paid")
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    args.fences.write_text(json.dumps({
        "version": 1,
        "fences": [{
            "id": "paid-conversation-write:coconala:123",
            "state": "open", "platform": "coconala",
            "identities": {"talkroom_id": "123"},
            "capabilities": ["conversation_write"],
            "reason": "paid room", "opened_at": "2026-08-19T00:00:00+00:00",
            "release": {"kind": "event", "event": "paid_order_closed:123"},
        }],
    }), encoding="utf-8")
    result = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence", run_id="run-paid",
    )
    assert result["status"] == "paid_handoff"
    assert result["replied"] == 0
    assert result["blocked"] == 0
    assert result["closed_without_send"] == 1
    assert result["pending"] == 0
    assert result["official_readback"] == 0


def test_orders_proof_is_fresh_and_precedes_estimate_effect(tmp_path, monkeypatch):
    script, calls, send_record = _fake_targeted_scripts(
        tmp_path, next_action="requested_estimate", estimate_required=True,
    )
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    observed_modes_at_estimate = []

    def fake_estimate(snapshot, *, args, owner, now, **kwargs):
        recorded = [json.loads(line) for line in calls.read_text().splitlines()]
        observed_modes_at_estimate.append([
            argv[argv.index("--mode") + 1]
            for argv in recorded if "--mode" in argv
        ])
        estimate_event_key, estimate_action_id, estimate_revision = _seed_verified_estimate(
            args, "123", "buyer-2",
        )
        return {
            "estimate_required": 1, "estimate_effect": 1,
            "estimate_readback": 1, "estimate_pending": 0,
            "estimate_failed": 0, "estimate_events": [{
                "thread_id": "123", "status": "verified",
                "event_key": estimate_event_key, "action_id": estimate_action_id,
                "revision": estimate_revision, "effect": 1, "official_readback": 1,
            }], "errors": [],
        }

    monkeypatch.setattr(detector, "run_requested_estimate", fake_estimate)
    result = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence", run_id="run-estimate-order",
    )
    assert result["status"] == "completed"
    assert observed_modes_at_estimate == [[
        "direct-thread-head-only", "direct-thread-only",
        "direct-thread-head-only", "orders-only",
    ]]
    assert not send_record.exists() or not send_record.read_text().strip()


@pytest.mark.parametrize("orders_mode", ["stale"])
def test_incomplete_or_stale_orders_proof_blocks_normal_effect(tmp_path, orders_mode):
    script, _calls, send_record = _fake_targeted_scripts(
        tmp_path, orders_mode=orders_mode,
    )
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=tmp_path / "evidence", run_id=f"run-orders-{orders_mode}",
    )
    assert result["status"] == "pending"
    assert result["replied"] == 0
    assert not send_record.exists() or not send_record.read_text().strip()


def _run_targeted_case(args, action_id, inbox_event_key, evidence, run_id):
    return detector.run_targeted_thread(
        args, action_id=action_id, inbox_event_key=inbox_event_key,
        thread_id="123", evidence=evidence, run_id=run_id,
    )


@pytest.mark.parametrize("orders_mode", ["paid", "missing", "invalid", "failed"])
def test_fresh_orders_proof_blocks_estimate_and_reply_effects(
    tmp_path, monkeypatch, orders_mode,
):
    script, _calls, send_record = _fake_targeted_scripts(
        tmp_path, orders_mode=orders_mode, estimate_required=True,
    )
    args = _targeted_args(tmp_path, script)
    # A caller-provided open registry is deliberately stale and must not be used.
    args.fences.write_text(json.dumps({
        "version": 1, "fences": [{"state": "open", "identities": {"talkroom_id": "123"}}],
    }), encoding="utf-8")
    estimate_calls = []
    monkeypatch.setattr(
        detector, "run_requested_estimate",
        lambda *a, **k: estimate_calls.append(True) or {
            "estimate_required": 1, "estimate_effect": 1, "estimate_readback": 1,
            "estimate_pending": 0, "estimate_failed": 0, "estimate_events": [{
                "thread_id": "123", "status": "verified",
                "event_key": "coconala:estimate:v1:123:buyer-2",
            }], "errors": [],
        },
    )
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = _run_targeted_case(
        args, action_id, inbox_event_key, tmp_path / "evidence", "run-proof-" + orders_mode,
    )
    assert result.get("replied", 0) == 0
    assert result.get("estimate_effect", 0) == 0
    assert result.get("official_readback", 0) == 0
    assert not estimate_calls
    assert not send_record.exists()


def test_current_empty_registry_allows_normal_effect_and_ignores_stale_caller_fence(tmp_path):
    script, _calls, send_record = _fake_targeted_scripts(tmp_path, orders_mode="empty")
    args = _targeted_args(tmp_path, script)
    args.fences.write_text(json.dumps({
        "version": 1, "fences": [{"state": "open", "identities": {"talkroom_id": "123"}}],
    }), encoding="utf-8")
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = _run_targeted_case(
        args, action_id, inbox_event_key, tmp_path / "evidence", "run-empty",
    )
    assert result["status"] == "completed"
    assert result["replied"] == 1
    assert len(send_record.read_text().splitlines()) == 1


def test_current_empty_registry_allows_estimate_effect(tmp_path, monkeypatch):
    script, _calls, send_record = _fake_targeted_scripts(
        tmp_path, next_action="requested_estimate", estimate_required=True,
    )
    args = _targeted_args(tmp_path, script)
    estimate_event_key, estimate_action_id, estimate_revision = _seed_verified_estimate(
        args, "123", "buyer-2",
    )
    monkeypatch.setattr(
        detector, "run_requested_estimate",
        lambda *a, **k: {
            "estimate_required": 1, "estimate_effect": 1, "estimate_readback": 1,
            "estimate_pending": 0, "estimate_failed": 0, "estimate_events": [{
                "thread_id": "123", "status": "verified",
                "event_key": estimate_event_key, "action_id": estimate_action_id,
                "revision": estimate_revision, "effect": 1, "official_readback": 1,
            }], "errors": [],
        },
    )
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = _run_targeted_case(
        args, action_id, inbox_event_key, tmp_path / "evidence", "run-estimate",
    )
    assert result["status"] == "completed"
    assert result["estimate_effect"] == 1
    assert result["estimate_readback"] == 1
    assert result["replied"] == 0
    assert not send_record.exists()


def test_full_pass_fresh_paid_fence_blocks_estimate_effect(tmp_path, monkeypatch):
    script, _calls, _send_record = _fake_targeted_scripts(
        tmp_path, next_action="requested_estimate", estimate_required=True,
        orders_mode="paid",
    )
    args = _targeted_args(tmp_path, script)
    estimate_calls = []

    def fake_estimate(filtered_snapshot, **kwargs):
        estimate_calls.append(filtered_snapshot)
        assert not any(
            isinstance(item, dict) and item.get("estimate_required") is True
            for item in filtered_snapshot.get("inquiries", [])
        )
        return {
            "estimate_required": 0, "estimate_effect": 0,
            "estimate_readback": 0, "estimate_pending": 0,
            "estimate_failed": 0, "estimate_events": [], "errors": [],
        }

    monkeypatch.setattr(detector, "run_requested_estimate", fake_estimate)
    snapshot_value = {
        "collector_mode": "direct-inbox-only",
        "semantic_ssot": True,
        "inquiries": [{
            "talkroom_id": "123",
            "talkroom_url": "https://coconala.com/mypage/direct_message/123",
            "last_message_side": "buyer",
            "estimate_required": True,
            "next_action": "requested_estimate",
        }],
    }
    result = detector._run_effect_pipeline(
        args, snapshot=snapshot_value, evidence=tmp_path / "evidence",
        run_id="full-paid-estimate",
    )
    assert len(estimate_calls) == 1
    assert result["estimate_effect"] == 0
    assert result["estimate_readback"] == 0
    assert result["estimate_pending"] >= 1
    assert result["status"] != "completed"


def test_targeted_official_readback_must_match_exact_action_id():
    lane = {
        "status": "completed", "replied": 1, "reconciled": 0,
        "pending_verify": 0, "reconcile_pending": 0,
        "events": [{
            "status": "replied", "action_id": 999, "revision": 1,
            "talkroom_id": "123",
            "origin_at": "2026-08-19T00:01:00+00:00",
            "seller_sent_at": "2026-08-19T00:02:00+00:00",
        }],
    }
    result = detector._targeted_effect_result(
        lane, thread_id="123", action_id=42,
    )
    assert result["status"] != "completed"
    assert result["replied"] == 0
    assert result["official_readback"] == 0
    assert result["effect"] == 0
    assert result["pending"] >= 1


def test_targeted_official_readback_must_match_exact_revision():
    lane = {
        "status": "completed", "replied": 1, "reconciled": 0,
        "pending_verify": 0, "reconcile_pending": 0,
        "events": [{
            "status": "replied", "action_id": 42, "revision": 1,
            "talkroom_id": "123",
            "origin_at": "2026-08-19T00:01:00+00:00",
            "seller_sent_at": "2026-08-19T00:02:00+00:00",
        }],
    }
    result = detector._targeted_effect_result(
        lane, thread_id="123", action_id=42, expected_revision=2,
    )
    assert result["status"] != "completed"
    assert result["replied"] == 0
    assert result["official_readback"] == 0
    assert result["effect"] == 0
    assert result["pending"] >= 1


def test_targeted_official_readback_accepts_exact_post_enqueue_revision():
    lane = {
        "status": "completed", "replied": 1, "reconciled": 0,
        "pending_verify": 0, "reconcile_pending": 0,
        "events": [{
            "status": "replied", "action_id": 42, "revision": 2,
            "talkroom_id": "123",
            "origin_at": "2026-08-19T00:01:00+00:00",
            "seller_sent_at": "2026-08-19T00:02:00+00:00",
        }],
    }
    result = detector._targeted_effect_result(
        lane, thread_id="123", action_id=42, expected_revision=2,
    )
    assert result["status"] == "completed"
    assert result["replied"] == 1
    assert result["official_readback"] == 1
    assert result["effect"] == 1


def test_intentional_no_send_close_rejects_advanced_revision(tmp_path):
    script, _calls, _send_record = _fake_targeted_scripts(tmp_path, next_action="wait")
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    advanced_event_key = "coconala:message:v1:123:buyer-2"
    advanced = database.enqueue(
        event_key=advanced_event_key,
        thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=int(time.time()) + 1,
    )
    assert int(advanced["revision"]) == 2
    closed = detector._targeted_close_no_send(
        database=args.database, manifest=args.manifest,
        action_id=action_id, thread_id="123", inbox_event_key=inbox_event_key,
        expected_revision=1, reason="wait", run_id="run-stale-close",
    )
    assert closed is None
    assert database.pending_actions()


@pytest.mark.parametrize(
    ("estimate_effect", "estimate_readback", "estimate_events"),
    [
        (1, 0, []),
        (1, 1, [{
            "thread_id": "999", "status": "verified",
            "event_key": "coconala:estimate:v1:999:buyer-2",
        }]),
        (1, 1, [{
            "thread_id": "123", "status": "verified",
            "event_key": "coconala:estimate:v1:123:buyer-old",
        }]),
        (1, 1, [{
            "thread_id": "123", "status": "verified",
            "event_key": "not-an-estimate-key",
        }]),
        (1, 2, [{
            "thread_id": "123", "status": "verified",
            "event_key": "coconala:estimate:v1:123:buyer-2",
        }]),
        (1, 1, [{
            "thread_id": "123", "status": "verified",
            "event_key": "coconala:estimate:v1:123:buyer-2",
        }, {
            "thread_id": "123", "status": "verified",
            "event_key": "coconala:estimate:v1:123:buyer-2",
        }]),
        (2, 2, [{
            "thread_id": "123", "status": "verified",
            "event_key": "coconala:estimate:v1:123:buyer-2",
        }]),
    ],
)
def test_unverified_estimate_effect_is_recoverable_pending(
    estimate_effect, estimate_readback, estimate_events,
):
    result = detector.merge_estimate_metrics(
        {
            "status": "completed", "effect": 0, "official_readback": 0,
            "pending": 0, "errors": [],
        },
        {
            "estimate_required": 1, "estimate_effect": estimate_effect,
            "estimate_readback": estimate_readback,
            "estimate_pending": 0, "estimate_failed": 0,
            "estimate_events": estimate_events, "errors": [],
        },
        normal_actionable=0, expected_thread="123",
        expected_event_keys={"coconala:estimate:v1:123:buyer-2"},
    )
    assert result["status"] == "reconcile_pending"
    assert result["estimate_effect"] == 0
    assert result["estimate_readback"] == 0
    assert result["estimate_pending"] >= 1
    assert result["effect"] == 0


def test_full_pass_filters_fenced_and_invalid_estimate_candidates_before_effect(
    tmp_path, monkeypatch,
):
    script, _calls, _send_record = _fake_targeted_scripts(tmp_path)
    args = _targeted_args(tmp_path, script)
    looked_up = []

    def fake_fence(_path, thread_id):
        looked_up.append(thread_id)
        return thread_id == "123"

    monkeypatch.setattr(detector, "_paid_fence_open_for_thread", fake_fence)
    estimate_calls = []
    estimate_event_key, estimate_action_id, estimate_revision = _seed_verified_estimate(
        args, "456", "buyer-4",
    )

    def fake_estimate(filtered_snapshot, **kwargs):
        estimate_calls.append(filtered_snapshot)
        assert [
            item["talkroom_id"] for item in filtered_snapshot["inquiries"]
        ] == ["456"]
        return {
            "estimate_required": 1, "estimate_effect": 1,
            "estimate_readback": 1, "estimate_pending": 0,
            "estimate_failed": 0, "estimate_events": [{
                "thread_id": "456", "status": "verified",
                "event_key": estimate_event_key, "action_id": estimate_action_id,
                "revision": estimate_revision, "effect": 1, "official_readback": 1,
            }], "errors": [],
        }

    monkeypatch.setattr(detector, "run_requested_estimate", fake_estimate)
    snapshot_value = {"inquiries": [
        {
            "talkroom_id": "123", "estimate_required": True,
            "estimate_request_identity": "buyer-2",
        },
        {
            "talkroom_id": "456", "estimate_required": True,
            "estimate_request_identity": "buyer-4",
        },
        {
            "talkroom_id": "bad id", "estimate_required": True,
            "estimate_request_identity": "buyer-bad",
        },
        {
            "estimate_required": True,
            "estimate_request_identity": "buyer-missing",
        },
    ]}
    result = detector._run_effect_pipeline(
        args, snapshot=snapshot_value, evidence=tmp_path / "evidence",
        run_id="mixed-estimates",
    )
    assert looked_up == ["123", "456"]
    assert len(estimate_calls) == 1
    assert result["estimate_required"] == 4
    assert result["estimate_effect"] == 1
    assert result["estimate_readback"] == 1
    assert result["estimate_pending"] >= 3
    assert result["errors"].count("estimate_thread_invalid") == 2
    assert "estimate_effect_blocked_paid_fence" in result["errors"]
    wrong_fenced_proof = detector.merge_estimate_metrics(
        {"status": "completed", "effect": 0, "official_readback": 0,
         "pending": 0, "errors": []},
        {"estimate_required": 2, "estimate_effect": 2,
         "estimate_readback": 2, "estimate_pending": 0,
         "estimate_failed": 0, "errors": [], "estimate_events": [
             {"thread_id": "456", "status": "verified",
              "event_key": "coconala:estimate:v1:456:buyer-4"},
             {"thread_id": "123", "status": "verified",
              "event_key": "coconala:estimate:v1:123:buyer-2"},
         ]},
        normal_actionable=0,
        expected_event_keys={"coconala:estimate:v1:456:buyer-4"},
    )
    assert wrong_fenced_proof["estimate_effect"] == 0
    assert wrong_fenced_proof["status"] == "reconcile_pending"


def test_estimate_partial_success_keeps_new_effect_with_readback_only_event():
    result = detector.merge_estimate_metrics(
        {"status": "completed", "effect": 0, "official_readback": 0,
         "pending": 0, "errors": []},
        {"estimate_required": 2, "estimate_effect": 1,
         "estimate_readback": 2, "estimate_pending": 0,
         "estimate_failed": 0, "errors": [], "estimate_events": [
             {"thread_id": "123", "status": "already_delivered",
              "event_key": "coconala:estimate:v1:123:buyer-old",
              "effect": 0, "official_readback": 1,
              "action_id": 41, "revision": 1},
             {"thread_id": "456", "status": "verified",
              "event_key": "coconala:estimate:v1:456:buyer-new",
              "effect": 1, "official_readback": 1,
              "action_id": 42, "revision": 1},
         ]},
        normal_actionable=0,
        expected_event_keys={
            "coconala:estimate:v1:123:buyer-old",
            "coconala:estimate:v1:456:buyer-new",
        },
        expected_event_bindings={
            "coconala:estimate:v1:123:buyer-old": (41, 1),
            "coconala:estimate:v1:456:buyer-new": (42, 1),
        },
    )
    assert result["status"] == "completed"
    assert result["estimate_effect"] == 1
    assert result["estimate_readback"] == 2
    assert result["effect"] == 1
    assert result["official_readback"] == 2


def test_estimate_forged_old_action_cannot_substantiate_effect():
    result = detector.merge_estimate_metrics(
        {"status": "completed", "effect": 0, "official_readback": 0,
         "pending": 0, "errors": []},
        {"estimate_required": 1, "estimate_effect": 1,
         "estimate_readback": 1, "estimate_pending": 0,
         "estimate_failed": 0, "errors": [], "estimate_events": [{
             "thread_id": "456", "status": "verified",
             "event_key": "coconala:estimate:v1:456:buyer-new",
             "effect": 1, "official_readback": 1,
             "action_id": 41, "revision": 1,
         }]},
        normal_actionable=0,
        expected_event_keys={"coconala:estimate:v1:456:buyer-new"},
        expected_event_bindings={"coconala:estimate:v1:456:buyer-new": (42, 1)},
    )
    assert result["status"] == "reconcile_pending"
    assert result["estimate_effect"] == 0
    assert result["estimate_readback"] == 0
    assert result["pending"] >= 1


def test_only_latest_coalesced_estimate_binds_current_revision(tmp_path):
    script, _calls, _send_record = _fake_targeted_scripts(tmp_path)
    args = _targeted_args(tmp_path, script)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    thread_id = "123"
    thread_url = "https://coconala.com/mypage/direct_message/123"
    old_key = outbox.coconala_estimate_event_key(thread_id, "buyer-old")
    new_key = outbox.coconala_estimate_event_key(thread_id, "buyer-new")
    action = database.enqueue_estimate(
        event_key=old_key, thread_id=thread_id, thread_url=thread_url, observed_at=100,
    )
    advanced = database.enqueue_estimate(
        event_key=new_key, thread_id=thread_id, thread_url=thread_url, observed_at=100,
    )
    assert advanced["action_id"] == action["action_id"]
    assert advanced["revision"] == 2
    claimed = database.claim(
        owner="latest-estimate", now=102, lease_seconds=30,
        action_id=int(action["action_id"]),
    )
    assert claimed is not None
    intent = database.prepare_intent(
        int(action["action_id"]), owner="latest-estimate",
        fencing_token=int(claimed["fencing_token"]), outgoing_body="新しい見積もり",
        now=103, origin_at=102, store_outgoing_body=True,
    )
    database.mark_click_started(
        int(action["action_id"]), int(intent["revision"]), owner="latest-estimate",
        fencing_token=int(claimed["fencing_token"]), now=104,
    )
    database.reconcile(
        int(action["action_id"]), thread_url=thread_url,
        outgoing_hash=str(intent["outgoing_hash"]), seller_sent_at=105,
        last_sender="seller", observed_at=106, authoritative_absent=False,
    )

    readback = database.verified_estimate_after_request(thread_id, 1)
    assert readback is not None
    assert readback["event_key"] == new_key
    bindings = detector._expected_estimate_bindings(args.database, {old_key, new_key})
    assert bindings == {new_key: (int(action["action_id"]), 2)}

    result = detector.merge_estimate_metrics(
        {"status": "completed", "effect": 0, "official_readback": 0,
         "pending": 0, "errors": []},
        {"estimate_required": 2, "estimate_effect": 1,
         "estimate_readback": 1, "estimate_pending": 1,
         "estimate_failed": 0, "errors": [], "estimate_events": [
             {"thread_id": thread_id, "status": "reconcile_pending",
              "event_key": old_key, "effect": 0, "official_readback": 0},
             {"thread_id": thread_id, "status": "verified",
              "event_key": new_key, "action_id": int(action["action_id"]),
              "revision": 2, "effect": 1, "official_readback": 1},
         ]},
        normal_actionable=0, expected_thread=thread_id,
        expected_event_keys={old_key, new_key}, expected_event_bindings=bindings,
    )
    assert result["status"] == "reconcile_pending"
    assert result["estimate_effect"] == 1
    assert result["estimate_readback"] == 1


def test_missing_expected_estimate_event_cannot_complete_with_zero_counts():
    old_key = "coconala:estimate:v1:123:buyer-old"
    new_key = "coconala:estimate:v1:456:buyer-new"
    result = detector.merge_estimate_metrics(
        {"status": "completed", "effect": 0, "official_readback": 0,
         "pending": 0, "errors": []},
        {"estimate_required": 2, "estimate_effect": 1,
         "estimate_readback": 1, "estimate_pending": 0,
         "estimate_failed": 0, "errors": [], "estimate_events": [{
             "thread_id": "123", "status": "verified", "event_key": old_key,
             "action_id": 41, "revision": 1, "effect": 1, "official_readback": 1,
         }]},
        normal_actionable=0,
        expected_event_keys={old_key, new_key},
        expected_event_bindings={old_key: (41, 1), new_key: (42, 1)},
    )
    assert result["status"] == "reconcile_pending"
    assert result["estimate_effect"] == 0
    assert result["estimate_readback"] == 0
    assert result["estimate_pending"] >= 1


def test_already_delivered_without_exact_estimate_binding_is_recoverable_pending():
    thread_id = "123"
    event_key = outbox.coconala_estimate_event_key(thread_id, "buyer-old")

    class Database:
        def action_lifecycle_for_event(self, key, received_thread_id):
            assert (key, received_thread_id) == (event_key, thread_id)
            return {"state": "replied", "dlq_at": None}

        def verified_estimate_after_request(self, received_thread_id, request_sent_at):
            assert (received_thread_id, request_sent_at) == (thread_id, 1)
            return {
                "event_key": outbox.coconala_estimate_event_key(thread_id, "buyer-new"),
                "action_id": 42, "revision": 2,
            }

    result = requested_estimate.execute_requested_estimate(
        {"talkroom_id": thread_id, "talkroom_url": "https://coconala.com/mypage/direct_message/123",
         "estimate_request_identity": "buyer-old",
         "estimate_request_sent_at": "1970-01-01T00:00:01+00:00"},
        database=Database(), composer=object(), browser_factory=object(),
        helper=None, owner="test", now=10,
    )
    assert result["status"] == "reconcile_pending"
    assert result["pending"] == 1
    assert result["official_readback"] == 0
    assert result["errors"] == ["estimate_already_delivered_binding_missing"]


def test_fresh_proof_is_ordered_after_head_and_before_effect(tmp_path):
    script, calls, _send_record = _fake_targeted_scripts(tmp_path)
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = _run_targeted_case(
        args, action_id, inbox_event_key, tmp_path / "evidence", "run-order",
    )
    assert result["status"] == "completed"
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    labels = []
    for argv in commands:
        if argv and argv[0] == "build":
            labels.append("queue_build")
        elif argv and argv[0] == "enqueue":
            labels.append("enqueue")
        elif argv and argv[0] == "build-paid":
            labels.append("fence")
        elif "--mode" in argv:
            labels.append(argv[argv.index("--mode") + 1])
        elif "--queue" in argv:
            labels.append("lane")
    assert labels.index("direct-thread-only") < labels.index("queue_build")
    assert labels.index("queue_build") < labels.index("enqueue")
    enqueue_index = labels.index("enqueue")
    assert any(
        index > enqueue_index
        for index, label in enumerate(labels)
        if label == "direct-thread-head-only"
    )
    assert labels.index("direct-thread-head-only") < labels.index("orders-only")
    assert labels.index("orders-only") < labels.index("fence") < labels.index("lane")


def test_newer_head_identity_prevents_send_and_no_send_closure(tmp_path):
    script, _calls, send_record = _fake_targeted_scripts(
        tmp_path, next_action="wait", head_identity="c" * 64,
    )
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = _run_targeted_case(
        args, action_id, inbox_event_key, tmp_path / "evidence", "run-new-head",
    )
    assert result["status"] == "pending"
    assert result["closed_without_send"] == 0
    assert not send_record.exists()
    assert outbox.ConnectorOutbox(args.database, args.manifest).pending_actions()


def test_wrong_inbox_identity_cannot_close_exact_action(tmp_path):
    script, _calls, _send_record = _fake_targeted_scripts(tmp_path, next_action="wait")
    args = _targeted_args(tmp_path, script)
    action_id, _inbox_event_key = _seed_inbox_action(args)
    wrong_key = outbox.coconala_inbox_event_key("123", "c" * 64)
    result = _run_targeted_case(
        args, action_id, wrong_key, tmp_path / "evidence", "run-wrong-key",
    )
    assert result["closed_without_send"] == 0
    assert result["replied"] == 0
    assert outbox.ConnectorOutbox(args.database, args.manifest).pending_actions()


@pytest.mark.parametrize("next_action", ["future_action", ""])
def test_unknown_or_missing_semantic_action_stays_pending(tmp_path, next_action):
    script, _calls, _send_record = _fake_targeted_scripts(
        tmp_path, next_action=next_action,
    )
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = _run_targeted_case(
        args, action_id, inbox_event_key, tmp_path / "evidence", "run-unknown",
    )
    assert result["status"] == "pending"
    assert result["closed_without_send"] == 0
    assert outbox.ConnectorOutbox(args.database, args.manifest).pending_actions()


def test_positive_reply_without_official_readback_is_recoverable_pending(tmp_path):
    script, _calls, send_record = _fake_targeted_scripts(tmp_path, bad_readback=True)
    args = _targeted_args(tmp_path, script)
    action_id, inbox_event_key = _seed_inbox_action(args)
    result = _run_targeted_case(
        args, action_id, inbox_event_key, tmp_path / "evidence", "run-bad-readback",
    )
    assert result["status"] != "completed"
    assert result["official_readback"] == 0
    assert result.get("replied", 0) == 0
    assert result.get("effect", 0) == 0
    assert result["pending"] >= 1
    assert not send_record.exists()


def test_inbox_event_key_is_thread_bound_and_rejects_non_sha_identity():
    assert outbox.coconala_inbox_event_key("123", "a" * 64) == (
        "coconala:inbox:v1:123:sha256_v1:" + "a" * 64
    )
    assert outbox.validate_coconala_event_key(
        "coconala:inbox:v1:123:sha256_v1:" + "a" * 64, "123"
    ).startswith("coconala:inbox:v1:123:")
    with pytest.raises(ValueError):
        outbox.coconala_inbox_event_key("123", "not-a-sha")
    with pytest.raises(ValueError):
        outbox.validate_coconala_event_key(
            "coconala:inbox:v1:123:sha256_v1:" + "a" * 64, "999"
        )


def test_head_and_direct_thread_identity_use_one_canonical_builder():
    """Representative outputs from both real expressions must share identity."""
    head_expression = snapshot.direct_inbox_coverage_expression(1)
    assert all(
        field in head_expression
        for field in ("directMessagesRoomId", "fromUserId", "createdAt", "body")
    )
    assert "last_message_identity_fields" in head_expression
    assert all(
        field in snapshot.DIRECT_MESSAGE_EXPRESSION
        for field in ("message_id", "author_path", "sent_at", "body")
    )
    head_fields = {
        "directMessagesRoomId": 123,
        "fromUserId": 456,
        "createdAt": 1_755_520_860_000,
        "body": "質問です",
    }
    head_dom = {
        "url": snapshot.MESSAGES_URL,
        "title": "メッセージ",
        "container_present": True,
        "coverage_complete": False,
        "cards": [{
            "talkroom_url": "https://coconala.com/mypage/direct_message/123",
            "title": "purchase_preorder_message",
            "last_message_side": "buyer",
            "unread": True,
            "preview_sha256": "a" * 64,
            "last_message_identity_fields": head_fields,
        }],
    }
    head_inquiry = snapshot.inquiries_from_dom(head_dom)[0]
    direct_dom = {
        "url": "https://coconala.com/mypage/direct_message/123",
        "title": "メッセージ詳細",
        "container_present": True,
        "own_user_path": "/users/seller",
        "messages": [{
            "message_id": "buyer-2", "author_path": "/users/buyer",
            "sent_at": "2026-08-19T00:01:00+00:00", "body": "質問です",
        }],
    }
    direct_inquiry = snapshot.direct_message_event(
        direct_dom, "https://coconala.com/mypage/direct_message/123",
        semantic_judge=None,
    )
    assert head_inquiry["last_message_identity_sha256"] == direct_inquiry[
        "last_message_identity_sha256"
    ]


def test_head_expression_reads_one_page_without_changing_full_expression():
    assert snapshot.direct_inbox_coverage_expression() == snapshot.DIRECT_INBOX_COVERAGE_EXPRESSION
    head = snapshot.direct_inbox_coverage_expression(1)
    assert "pageLimit=1" in head
    assert "pageLimit=10" not in head
    with pytest.raises(ValueError):
        snapshot.direct_inbox_coverage_expression(0)


def test_head_only_snapshot_is_bounded_read_only_and_semantic_free(tmp_path, monkeypatch, capsys):
    output = tmp_path / "head.json"
    evidence = tmp_path / "evidence"
    opened = []
    seen = {}
    forbidden_fields = {
        "body", "preview", "preview_text", "raw_preview", "seller_id",
        "seller_name", "seller_user_id", "user_id", "user_name", "user_identity",
    }
    forbidden_sentinels = {
        "BUYER_BODY_SENTINEL", "BUYER_PREVIEW_SENTINEL", "SELLER_ID_SENTINEL",
        "SELLER_NAME_SENTINEL", "SELLER_USER_ID_SENTINEL", "USER_ID_SENTINEL",
        "USER_NAME_SENTINEL", "USER_IDENTITY_SENTINEL",
    }

    class FakeTab:
        ws = "ws://head-only"

        def __init__(self, helper, url, *, hidden=False, **kwargs):
            seen["tab"] = (helper, url, hidden, kwargs)

        def __enter__(self):
            opened.append(True)
            return self

        def __exit__(self, *args):
            return None

    async def fake_inspect_message_page(ws_url, expression, expected_url, **kwargs):
        seen["inspect"] = (ws_url, expression, expected_url, kwargs)
        return {
            "url": snapshot.MESSAGES_URL,
            "title": "メッセージ",
            "container_present": True,
            "cards": [{
                "talkroom_url": "https://coconala.com/mypage/direct_message/123",
                "title": "purchase_preorder_message",
                "last_message_side": "",
                "unread": True,
                "preview_sha256": "a" * 64,
                "last_message_identity_sha256": "b" * 64,
                "body": "BUYER_BODY_SENTINEL",
                "preview": "BUYER_PREVIEW_SENTINEL",
                "preview_text": "BUYER_PREVIEW_SENTINEL",
                "raw_preview": "BUYER_PREVIEW_SENTINEL",
                "seller_id": "SELLER_ID_SENTINEL",
                "seller_name": "SELLER_NAME_SENTINEL",
                "seller_user_id": "SELLER_USER_ID_SENTINEL",
                "user_id": "USER_ID_SENTINEL",
                "user_name": "USER_NAME_SENTINEL",
                "user_identity": "USER_IDENTITY_SENTINEL",
            }],
            "cards_count": 1,
            "coverage_complete": True,
            "termination_reason": "pagination_end",
        }

    def unsafe_inquiries_from_dom(dom):
        card = dom["cards"][0]
        return [{
            "talkroom_id": "123",
            "talkroom_url": card["talkroom_url"],
            "title": "purchase_preorder_message",
            "reply_required": True,
            "next_action": "reply",
            "preview_sha256": card["preview_sha256"],
            "last_message_identity_sha256": card["last_message_identity_sha256"],
            "body": card["body"],
            "preview": card["preview"],
            "preview_text": card["preview_text"],
            "raw_preview": card["raw_preview"],
            "seller_id": card["seller_id"],
            "seller_name": card["seller_name"],
            "seller_user_id": card["seller_user_id"],
            "user_id": card["user_id"],
            "user_name": card["user_name"],
            "user_identity": card["user_identity"],
        }]

    def semantic_must_not_load():
        raise AssertionError("head-only must not initialize SemanticJudge")

    monkeypatch.setattr(snapshot, "DefaultTab", FakeTab)
    monkeypatch.setattr(snapshot, "inspect_message_page", fake_inspect_message_page)
    monkeypatch.setattr(snapshot, "inquiries_from_dom", unsafe_inquiries_from_dom)
    monkeypatch.setattr(snapshot, "load_connector_manifest", lambda: {})
    monkeypatch.setattr(snapshot, "_requested_estimate_module", semantic_must_not_load)
    monkeypatch.setattr(
        sys, "argv", [
            "coconala_queue_snapshot.py",
            "--output", str(output),
            "--evidence-dir", str(evidence),
            "--mode", "direct-inbox-head-only",
        ],
    )

    assert snapshot.main() == 0
    cli_output = capsys.readouterr().out
    cli = json.loads(cli_output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    evidence_payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in evidence.rglob("*.json")
    ]

    assert cli["collector_mode"] == "direct-inbox-head-only"
    assert isinstance(cli["captured_at"], str) and cli["captured_at"]
    assert cli["inquiries"] == 1
    assert cli["head_only"] is True
    assert cli["read_only"] is True
    assert saved["collector_mode"] == "direct-inbox-head-only"
    assert saved["head_only"] is True
    assert saved["semantic_ssot"] is False
    assert saved["read_only"] is True
    assert saved["source_receipt"]["source"] == "direct_inbox"
    assert saved["source_receipt"]["coverage_complete"] is False
    assert seen["tab"][1] == snapshot.MESSAGES_URL
    assert seen["inspect"][3]["coverage_expression"] == snapshot.direct_inbox_coverage_expression(1)
    assert seen["inspect"][3]["validate_coverage"] is False
    assert len(opened) == 1
    persisted = [saved, *evidence_payloads]
    serialized = json.dumps(persisted, ensure_ascii=False)
    assert not forbidden_sentinels.intersection(serialized.split('"'))
    assert not forbidden_sentinels.intersection(cli_output.split('"'))

    def assert_allowed_keys(value):
        if isinstance(value, dict):
            assert not forbidden_fields.intersection(value)
            for child in value.values():
                assert_allowed_keys(child)
        elif isinstance(value, list):
            for child in value:
                assert_allowed_keys(child)

    for payload in persisted:
        assert_allowed_keys(payload)
    assert_allowed_keys(cli)


def test_direct_thread_head_only_is_exact_and_semantic_free(tmp_path, monkeypatch, capsys):
    output = tmp_path / "thread-head.json"
    evidence = tmp_path / "evidence"
    opened = []
    seen = {}
    forbidden = "BUYER_BODY_SENTINEL"

    class FakeTab:
        ws = "ws://thread-head-only"

        def __init__(self, helper, url, *, hidden=False, **kwargs):
            opened.append((helper, url, hidden, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    async def fake_inspect_message_page(ws_url, expression, expected_url, **kwargs):
        seen["inspect"] = (ws_url, expression, expected_url, kwargs)
        return {
            "url": expected_url,
            "title": "メッセージ詳細",
            "container_present": True,
            "own_user_path": "/users/seller",
            "messages": [{
                "message_id": "buyer-2",
                "author_path": "/users/buyer",
                "sent_at": "2026-08-19T00:01:00+00:00",
                "body": forbidden,
            }],
        }

    class SemanticMustNotLoad:
        class SemanticJudge:
            def __init__(self, **kwargs):
                raise AssertionError("direct-thread-head-only must not initialize SemanticJudge")

    def semantic_module_must_not_load():
        raise AssertionError("direct-thread-head-only must not load semantic module")

    monkeypatch.setattr(snapshot, "DefaultTab", FakeTab)
    monkeypatch.setattr(snapshot, "inspect_message_page", fake_inspect_message_page)
    monkeypatch.setattr(snapshot, "load_connector_manifest", lambda: {})
    monkeypatch.setattr(snapshot, "_requested_estimate_module", semantic_module_must_not_load)
    monkeypatch.setattr(
        sys, "argv", [
            "coconala_queue_snapshot.py", "--output", str(output),
            "--evidence-dir", str(evidence), "--mode", "direct-thread-head-only",
            "--talkroom-id", "123",
        ],
    )

    assert snapshot.main() == 0
    cli = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text(encoding="utf-8"))
    persisted = [saved, *(
        json.loads(path.read_text(encoding="utf-8"))
        for path in evidence.rglob("*.json")
    )]
    assert cli["collector_mode"] == "direct-thread-head-only"
    assert cli["semantic_ssot"] is False
    assert saved["collector_mode"] == "direct-thread-head-only"
    assert saved["semantic_ssot"] is False
    assert saved["read_only"] is True
    assert saved["inquiries"] == [{
        "talkroom_id": "123",
        "talkroom_url": "https://coconala.com/mypage/direct_message/123",
        "last_message_identity_sha256": saved["inquiries"][0]["last_message_identity_sha256"],
        "last_message_side": "buyer",
        "buyer_sent_at": "2026-08-19T00:01:00+00:00",
        "reply_required": True,
    }]
    assert saved["source_receipt"]["source"] == "direct_thread"
    assert seen["inspect"][2] == "https://coconala.com/mypage/direct_message/123"
    assert opened and opened[0][1] == seen["inspect"][2]
    serialized = json.dumps(persisted, ensure_ascii=False)
    assert forbidden not in serialized


def test_targeted_preflight_uses_exact_thread_head_not_inbox_list(tmp_path, monkeypatch):
    output = tmp_path / "targeted-head.json"
    calls = []
    list_identity = "a" * 64
    exact_identity = "b" * 64
    args = SimpleNamespace(
        snapshot_script=SNAPSHOT_PATH,
        database=tmp_path / "outbox.sqlite3",
        manifest=GIG_ROOT / "config" / "connectors" / "coconala.json",
    )

    def fake_run(step, command):
        calls.append((step, command))
        mode = command[command.index("--mode") + 1]
        output_path = Path(command[command.index("--output") + 1])
        thread_id = command[command.index("--talkroom-id") + 1] if "--talkroom-id" in command else "123"
        if mode == "direct-inbox-head-only":
            row = {
                "talkroom_id": thread_id,
                "talkroom_url": "https://coconala.com/mypage/direct_message/123",
                "last_message_identity_sha256": list_identity,
                "last_message_side": "seller",
            }
            snapshot_value = {
                "collector_mode": mode, "head_only": True, "read_only": True,
                "semantic_ssot": False,
                "inquiries": [row],
            }
        else:
            row = {
                "talkroom_id": thread_id,
                "talkroom_url": "https://coconala.com/mypage/direct_message/123",
                "last_message_identity_sha256": exact_identity,
                "last_message_side": "buyer",
                "buyer_sent_at": "2026-08-19T00:01:00+00:00",
                "reply_required": True,
            }
            snapshot_value = {
                "collector_mode": mode, "head_only": True, "read_only": True,
                "semantic_ssot": False,
                "inquiries": [row],
                "source_receipt": {"source": "direct_thread"},
            }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(snapshot_value), encoding="utf-8")

    monkeypatch.setattr(detector, "_run", fake_run)
    row = detector._collect_targeted_head(
        args, evidence=tmp_path / "evidence", thread_id="123",
        identity_sha256=exact_identity,
    )

    assert row["last_message_identity_sha256"] == exact_identity
    assert len(calls) == 1
    command = calls[0][1]
    assert command[command.index("--mode") + 1] == "direct-thread-head-only"
    assert command[command.index("--talkroom-id") + 1] == "123"


def test_direct_thread_only_owns_exact_url_and_emits_one_semantic_inquiry(
    tmp_path, monkeypatch, capsys,
):
    output = tmp_path / "thread.json"
    evidence = tmp_path / "evidence"
    opened = []

    class FakeTab:
        ws = "ws://thread-only"

        def __init__(self, helper, url, *, hidden=False, **kwargs):
            opened.append((helper, url, hidden, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class FakeSemanticModule:
        class SemanticJudge:
            def __init__(self, **kwargs):
                pass

    async def fake_inspect_message_page(ws_url, expression, expected_url, **kwargs):
        return {
            "url": expected_url, "title": "メッセージ詳細",
            "container_present": True, "own_user_path": "/users/seller",
            "messages": [{
                "message_id": "buyer-2", "author_path": "/users/buyer",
                "sent_at": "2026-08-19T00:01:00+00:00", "body": "質問です",
            }],
        }

    monkeypatch.setattr(snapshot, "DefaultTab", FakeTab)
    monkeypatch.setattr(snapshot, "inspect_message_page", fake_inspect_message_page)
    monkeypatch.setattr(snapshot, "load_connector_manifest", lambda: {})
    monkeypatch.setattr(snapshot, "enrich_verified_dm_attachments", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(snapshot, "_requested_estimate_module", lambda: FakeSemanticModule)
    monkeypatch.setattr(snapshot, "direct_message_event", lambda dom, url, **kwargs: {
        "last_message_side": "buyer", "reply_required": True, "next_action": "reply",
        "buyer_sent_at": "2026-08-19T00:01:00+00:00", "message_id": "buyer-2",
        "last_message_identity_sha256": "b" * 64,
        "semantic_receipt": {"judgement": {"next_action": "reply"}},
        "semantic_context_sha256": "a" * 64, "semantic_reply_body": "回答です",
    })
    monkeypatch.setattr(
        sys, "argv", [
            "coconala_queue_snapshot.py", "--output", str(output),
            "--evidence-dir", str(evidence), "--mode", "direct-thread-only",
            "--talkroom-id", "123", "--semantic-effects-enabled",
        ],
    )

    assert snapshot.main() == 0
    cli = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert cli["collector_mode"] == "direct-thread-only"
    assert cli["semantic_ssot"] is True
    assert saved["semantic_ssot"] is True
    assert len(saved["inquiries"]) == 1
    assert saved["inquiries"][0]["talkroom_url"] == (
        "https://coconala.com/mypage/direct_message/123"
    )
    assert saved["inquiries"][0]["last_message_identity_sha256"] == "b" * 64
    assert opened and opened[0][1] == saved["inquiries"][0]["talkroom_url"]

    monkeypatch.setattr(
        sys, "argv", [
            "coconala_queue_snapshot.py", "--output", str(output),
            "--evidence-dir", str(evidence), "--mode", "direct-thread-only",
            "--talkroom-id", "../123",
        ],
    )
    assert snapshot.main() == 1
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["error"] == "invalid_talkroom_id"


def _supervisor_args(tmp_path):
    no_contact_registry = tmp_path / "no-contact.json"
    no_contact_registry.write_text('{"version":1,"entries":[]}\n', encoding="utf-8")
    return argparse.Namespace(
        database=tmp_path / "supervisor.sqlite3",
        manifest=GIG_ROOT / "config" / "connectors" / "coconala.json",
        evidence_dir=tmp_path / "evidence",
        no_contact_registry=no_contact_registry,
        poll_seconds=0.01,
        workers=2,
        reconcile_seconds=300,
    )


def _head_row(identity, *, thread_id="123"):
    return {
        "talkroom_id": thread_id,
        "talkroom_url": f"https://coconala.com/mypage/direct_message/{thread_id}",
        "unread": True,
        "reply_required": True,
        "last_message_side": "buyer",
        "last_message_identity_sha256": identity,
    }


def _seed_pending_inbox_event(database, identity):
    event_key = outbox.coconala_inbox_event_key("123", identity)
    database.enqueue(
        event_key=event_key, thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=1_755_555_200,
    )
    return event_key


def test_targeted_pending_projection_keeps_inbox_identity_when_fallback_is_newer(tmp_path):
    """A fallback queue write must not hide an older exact inbox target."""
    database = outbox.ConnectorOutbox(
        tmp_path / "projection.sqlite3", GIG_ROOT / "config" / "connectors" / "coconala.json",
    )
    inbox_event_key = _seed_pending_inbox_event(database, "a" * 64)
    fallback_event_key = outbox.coconala_fallback_event_key(
        thread_id="123", buyer_sent_at=1_755_555_201, ordinal=0, raw_body="buyer text",
    )
    database.enqueue(
        event_key=fallback_event_key, thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=1_755_555_201,
    )

    assert database.pending_actions()[0]["event_key"] == fallback_event_key
    targeted = database.pending_targeted_actions()
    assert len(targeted) == 1
    assert targeted[0]["event_key"] == inbox_event_key


def test_supervise_probe_starts_follow_fixed_monotonic_deadline_and_exact_boundary(
    tmp_path, monkeypatch,
):
    args = _supervisor_args(tmp_path)
    args.poll_seconds = 0.05
    stop = asyncio.Event()
    clock = SimpleNamespace(now=0.0)
    starts = []
    probe_durations = [0.02, 0.02, 0.05, 0.07, 0.01, 0.01]
    original_wait_for = asyncio.wait_for

    monkeypatch.setattr(
        detector, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time),
    )

    async def deterministic_wait_for(awaitable, timeout):
        if (
            asyncio.current_task().get_name() == "gig-reply-producer"
            and timeout <= args.poll_seconds + 1e-9
        ):
            awaitable.close()
            clock.now += timeout
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(detector.asyncio, "wait_for", deterministic_wait_for)

    async def probe():
        starts.append(clock.now)
        clock.now += probe_durations[len(starts) - 1]
        await asyncio.sleep(0)
        if len(starts) == len(probe_durations):
            stop.set()
        return {"inquiries": [], "captured_at": "2026-08-19T00:00:00+00:00"}

    async def worker(item):
        del item

    async def reconcile():
        return None

    asyncio.run(detector.supervise_replies(
        args, probe=probe, worker=worker, reconcile=reconcile, stop=stop,
    ))

    assert starts == pytest.approx([0.0, 0.05, 0.10, 0.15, 0.22, 0.28], abs=1e-9)


def test_supervise_slow_workers_overlap_and_claim_second_before_first_finishes(tmp_path):
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    stop = asyncio.Event()
    second_claimed = asyncio.Event()
    first_finished = asyncio.Event()
    worker_started = {}
    worker_finished = {}
    claim_times = {}
    active = 0
    max_active = 0
    probe_count = 0

    async def probe():
        nonlocal probe_count
        probe_count += 1
        if probe_count == 1:
            return {"inquiries": [_head_row("a" * 64)], "captured_at": "2026-08-19T00:00:00+00:00"}
        if probe_count == 2:
            return {"inquiries": [_head_row("b" * 64, thread_id="456")], "captured_at": "2026-08-19T00:00:01+00:00"}
        await stop.wait()
        return {"inquiries": [], "captured_at": "2026-08-19T00:00:02+00:00"}

    async def worker(item):
        nonlocal active, max_active
        identity = item["identity_sha256"]
        worker_started[identity] = time.monotonic_ns()
        active += 1
        max_active = max(max_active, active)
        if identity == "a" * 64:
            await second_claimed.wait()
        else:
            event = database.action_lifecycle_for_event(item["event_key"], item["thread_id"])
            assert event is not None
            claim_times[identity] = time.monotonic_ns()
            second_claimed.set()
        worker_finished[identity] = time.monotonic_ns()
        active -= 1
        if len(worker_finished) == 2:
            first_finished.set()
            stop.set()

    async def reconcile():
        return None

    asyncio.run(detector.supervise_replies(
        args, probe=probe, worker=worker, reconcile=reconcile, stop=stop,
    ))

    assert claim_times["b" * 64] < worker_finished["a" * 64]
    assert max(worker_started.values()) < min(worker_finished.values())
    assert max_active == 2
    assert first_finished.is_set()


def test_head_snapshot_collector_has_a_bounded_process_timeout(tmp_path, monkeypatch):
    calls = []
    snapshot = tmp_path / "head-snapshot.json"

    def run(step, arguments, **kwargs):
        calls.append((step, kwargs.get("timeout")))
        snapshot.write_text(json.dumps({
            "collector_mode": "direct-inbox-head-only",
            "head_only": True,
            "inquiries": [],
        }), encoding="utf-8")

    monkeypatch.setattr(detector, "_run", run)
    args = argparse.Namespace(
        snapshot_script=tmp_path / "snapshot.py",
        database=tmp_path / "outbox.sqlite3",
        manifest=tmp_path / "manifest.json",
    )

    result = detector._collect_head_snapshot(args, tmp_path)

    assert result["inquiries"] == []
    assert calls == [("head_collect", 45)]


def test_supervise_restart_replays_pending_inbox_once(tmp_path):
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    event_key = outbox.coconala_inbox_event_key("123", "a" * 64)
    action = database.enqueue(
        event_key=event_key, thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=1_755_555_200,
    )
    stop = asyncio.Event()
    dispatched = []

    async def probe():
        return {"inquiries": [_head_row("a" * 64)], "captured_at": "2026-08-19T00:00:00+00:00"}

    async def worker(item):
        dispatched.append(item)
        stop.set()

    async def reconcile():
        return None

    asyncio.run(detector.supervise_replies(
        args, probe=probe, worker=worker, reconcile=reconcile, stop=stop,
    ))

    assert len(dispatched) == 1
    assert dispatched[0]["action_id"] == action["action_id"]
    assert dispatched[0]["event_key"] == event_key
    assert dispatched[0]["thread_id"] == "123"
    assert dispatched[0]["identity_sha256"] == "a" * 64


def test_supervise_does_not_dispatch_dlq_pending_head_on_repeated_probes(tmp_path):
    """A quarantined pending row is not revived by the same fresh inbox head."""
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    identity = "a" * 64
    event_key = outbox.coconala_inbox_event_key("123", identity)
    action = database.enqueue(
        event_key=event_key, thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=1_755_555_200,
    )
    with sqlite3.connect(args.database) as connection:
        connection.execute(
            "UPDATE connector_actions SET state='pending',dlq_at=?,updated_at=? WHERE action_id=?",
            (1_755_555_201, 1_755_555_201, action["action_id"]),
        )

    stop = asyncio.Event()
    probes = 0
    dispatched = []
    results = []

    async def probe():
        nonlocal probes
        probes += 1
        if probes >= 3:
            stop.set()
        return {"inquiries": [_head_row(identity)], "captured_at": "2026-08-19T00:00:00+00:00"}

    async def worker(item):
        dispatched.append(item)
        results.append({"status": "unexpected_dispatch", "action_id": item["action_id"]})

    async def reconcile():
        return None

    asyncio.run(detector.supervise_replies(
        args, probe=probe, worker=worker, reconcile=reconcile, stop=stop,
    ))

    assert probes >= 3
    assert dispatched == []
    assert results == []
    lifecycle = database.action_lifecycle_for_event(event_key, "123")
    assert lifecycle == {
        "state": "pending", "dlq_at": 1_755_555_201,
        "closure": None, "reason": None, "rejection_code": None,
    }


def test_supervise_replied_duplicate_head_has_no_second_effect(tmp_path):
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    event_key = outbox.coconala_inbox_event_key("123", "a" * 64)
    action = database.enqueue(
        event_key=event_key, thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=1_755_555_200,
    )
    with sqlite3.connect(args.database) as connection:
        connection.execute(
            "UPDATE connector_actions SET state='replied',updated_at=? WHERE action_id=?",
            (1_755_555_201, action["action_id"]),
        )
    stop = asyncio.Event()
    dispatched = []
    probes = 0

    async def probe():
        nonlocal probes
        probes += 1
        if probes > 1:
            stop.set()
        return {"inquiries": [_head_row("a" * 64)], "captured_at": "2026-08-19T00:00:00+00:00"}

    async def worker(item):
        dispatched.append(item)

    async def reconcile():
        return None

    asyncio.run(detector.supervise_replies(
        args, probe=probe, worker=worker, reconcile=reconcile, stop=stop,
    ))

    assert dispatched == []


def test_supervise_rebinds_stale_buyer_identity_once_and_replay_is_idempotent(tmp_path):
    """A stale worker result follows the latest buyer event on the same action."""
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    old_identity = "a" * 64
    new_identity = "b" * 64
    old_event_key = _seed_pending_inbox_event(database, old_identity)
    new_event_key = outbox.coconala_inbox_event_key("123", new_identity)
    mismatch = {
        "status": "pending",
        "errors": ["targeted_inbox_identity_changed"],
        "current_identity_sha256": new_identity,
        "current_last_message_side": "buyer",
    }
    stop = asyncio.Event()
    dispatched = []
    effect_identities = []

    async def probe():
        # No fresh observation: consume only durable A, then its rebound B.
        return {"inquiries": [], "captured_at": "2026-08-19T00:00:00+00:00"}

    async def worker(item):
        dispatched.append(item)
        if item["identity_sha256"] == old_identity:
            return mismatch
        assert item["identity_sha256"] == new_identity
        effect_identities.append(item["identity_sha256"])
        stop.set()
        return {"status": "completed"}

    async def reconcile():
        return None

    asyncio.run(detector.supervise_replies(
        args, probe=probe, worker=worker, reconcile=reconcile, stop=stop,
    ))

    assert [item["event_key"] for item in dispatched] == [old_event_key, new_event_key]
    assert effect_identities == [new_identity]

    pending = database.pending_actions()
    assert len(pending) == 1
    assert pending[0]["action_id"] == dispatched[0]["action_id"]
    assert pending[0]["revision"] == 2
    # pending_actions resolves the event by newest connector_events rowid.
    assert pending[0]["event_key"] == new_event_key
    with sqlite3.connect(args.database) as connection:
        active_count = connection.execute(
            """SELECT COUNT(*) FROM connector_actions
               WHERE platform='coconala' AND thread_id=?
                 AND state IN ('pending','claimed','intent_ready','reconcile_pending')
                 AND dlq_at IS NULL""",
            ("123",),
        ).fetchone()[0]
    assert active_count == 1

    replay = detector._supervisor_rebind_targeted_work(
        database, dispatched[0], mismatch, now=1_755_555_202,
    )
    assert replay is not None
    assert replay["event_key"] == new_event_key
    assert database.get_action(dispatched[0]["action_id"])["revision"] == 2
    assert database.pending_actions()[0]["event_key"] == new_event_key
    with sqlite3.connect(args.database) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM connector_events WHERE action_id=?",
            (dispatched[0]["action_id"],),
        ).fetchone()[0]
    assert event_count == 2
    assert len(dispatched) == 2


def test_supervise_dispatches_durable_latest_identity_not_stale_list_card(tmp_path):
    """A stale inbox-card identity cannot displace a newer durable thread head."""
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    old_identity = "a" * 64
    new_identity = "b" * 64
    _seed_pending_inbox_event(database, old_identity)
    database.enqueue(
        event_key=outbox.coconala_inbox_event_key("123", new_identity),
        thread_id="123", thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=1_755_555_201,
    )
    stop = asyncio.Event()
    dispatched = []

    async def probe():
        return {"inquiries": [_head_row(old_identity)]}

    async def worker(item):
        dispatched.append(item)
        stop.set()
        return {"status": "completed"}

    async def reconcile():
        return None

    asyncio.run(detector.supervise_replies(
        args, probe=probe, worker=worker, reconcile=reconcile, stop=stop,
    ))

    assert [item["identity_sha256"] for item in dispatched] == [new_identity]


def test_supervise_rebind_seller_last_closes_stale_action_without_dispatch(tmp_path):
    """A stale seller-last observation closes A and never creates buyer work B."""
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    old_identity = "a" * 64
    new_identity = "b" * 64
    old_event_key = _seed_pending_inbox_event(database, old_identity)
    new_event_key = outbox.coconala_inbox_event_key("123", new_identity)
    mismatch = {
        "status": "pending",
        "errors": ["targeted_inbox_identity_changed"],
        "current_identity_sha256": new_identity,
        "current_last_message_side": "seller",
    }
    work = detector._supervisor_work_from_action(database.pending_actions()[0])
    assert work is not None
    rebound = detector._supervisor_rebind_targeted_work(
        database, work, mismatch, now=1_755_555_201,
    )

    assert rebound is None
    assert database.pending_actions() == []
    assert database.closed_actions(closure="nothing_to_say")
    with sqlite3.connect(args.database) as connection:
        events = connection.execute(
            "SELECT event_key FROM connector_events WHERE action_id=?",
            (work["action_id"],),
        ).fetchall()
    assert [row[0] for row in events] == [old_event_key]
    assert new_event_key not in {row[0] for row in events}


def test_supervise_rebinds_unknown_head_side_for_authoritative_thread_read(tmp_path):
    """A head-only null side rebinds; the full thread read still gates sending."""
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    old_identity = "a" * 64
    new_identity = "b" * 64
    old_event_key = _seed_pending_inbox_event(database, old_identity)
    work = detector._supervisor_work_from_action(database.pending_actions()[0])
    assert work is not None

    rebound = detector._supervisor_rebind_targeted_work(
        database,
        work,
        {
            "status": "pending",
            "errors": ["targeted_inbox_identity_changed"],
            "current_identity_sha256": new_identity,
            "current_last_message_side": "",
        },
        now=1_755_555_201,
    )

    assert rebound is not None
    assert rebound["event_key"] == outbox.coconala_inbox_event_key("123", new_identity)
    assert rebound["expected_revision"] == 2
    pending = database.pending_targeted_actions()
    assert [row["event_key"] for row in pending] == [rebound["event_key"]]
    assert old_event_key != rebound["event_key"]


def test_supervise_rebind_seller_last_does_not_close_newer_buyer_revision(tmp_path):
    """A buyer coalesced after the stale seller result remains pending."""
    args = _supervisor_args(tmp_path)
    database = outbox.ConnectorOutbox(args.database, args.manifest)
    old_identity = "a" * 64
    new_identity = "b" * 64
    old_event_key = _seed_pending_inbox_event(database, old_identity)
    new_event_key = outbox.coconala_inbox_event_key("123", new_identity)
    mismatch = {
        "status": "pending",
        "errors": ["targeted_inbox_identity_changed"],
        "current_identity_sha256": new_identity,
        "current_last_message_side": "seller",
    }

    work = detector._supervisor_work_from_action(database.pending_actions()[0])
    assert work is not None
    assert work["expected_revision"] == 1

    # This is the race window: B is observed and coalesced after A's worker
    # result, but before the supervisor tries to close the stale seller result.
    action = database.enqueue(
        event_key=new_event_key, thread_id="123",
        thread_url="https://coconala.com/mypage/direct_message/123",
        observed_at=1_755_555_201,
    )
    assert action["revision"] == 2

    rebound = detector._supervisor_rebind_targeted_work(
        database, work, mismatch, now=1_755_555_202,
    )

    assert rebound is None
    pending = database.pending_actions()
    assert len(pending) == 1
    assert pending[0]["revision"] == 2
    assert pending[0]["event_key"] == new_event_key
    assert database.closed_actions(closure="nothing_to_say") == []
    with sqlite3.connect(args.database) as connection:
        events = connection.execute(
            "SELECT event_key FROM connector_events WHERE action_id=? ORDER BY rowid",
            (work["action_id"],),
        ).fetchall()
    assert [row[0] for row in events] == [old_event_key, new_event_key]
@pytest.fixture(autouse=True)
def _healthy_disk_for_concurrency_tests(monkeypatch):
    monkeypatch.setattr(detector, "disk_headroom_ok", lambda: True)
