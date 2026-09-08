from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_wait_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def test_accumulation_normalizes_legacy_text_only_digest() -> None:
    snapshot = load("coconala_queue_snapshot")
    text = "Legacy buyer requirement"
    legacy = {"text": text, "attachments": [], "sha256": hashlib.sha256(text.encode()).hexdigest()}

    rows, digest = snapshot._merge_accumulated([legacy], [])

    canonical = hashlib.sha256(json.dumps(
        {"text": text, "attachments": []},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert rows[0]["sha256"] == canonical
    assert digest == hashlib.sha256(json.dumps(
        [canonical], ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def test_talkroom_readback_retries_transient_tab_open_timeout(monkeypatch) -> None:
    snapshot = load("coconala_queue_snapshot")
    attempts = []

    class Tab:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise __import__("subprocess").TimeoutExpired(["cdp", "open"], 25)
            return SimpleNamespace(ws="ws://ready")

        def __exit__(self, *_args):
            return False

    async def inspect(*_args, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr(snapshot, "DefaultTab", Tab)
    monkeypatch.setattr(snapshot, "inspect_page", inspect)

    assert snapshot.inspect_page_with_retry(Path("helper"), "https://example.test", "1", None) == {"ok": True}
    assert len(attempts) == 2


def test_talkroom_readback_retries_hidden_helper_transport_timeout(monkeypatch) -> None:
    snapshot = load("coconala_queue_snapshot")
    attempts = []

    class Tab:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError(
                    "failed to open authenticated hidden target: "
                    "{'ok': False, 'reason': 'URLError: <urlopen error "
                    "[Errno 60] Operation timed out>'}"
                )
            return SimpleNamespace(ws="ws://ready")

        def __exit__(self, *_args):
            return False

    async def inspect(*_args, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr(snapshot, "DefaultTab", Tab)
    monkeypatch.setattr(snapshot, "inspect_page", inspect)

    assert snapshot.inspect_page_with_retry(
        Path("helper"), "https://example.test", "1", None
    ) == {"ok": True}
    assert len(attempts) == 2


def test_buyer_attachment_fetch_has_a_finite_timeout() -> None:
    snapshot = load("coconala_queue_snapshot")

    assert "AbortSignal.timeout(" in snapshot.TALKROOM_ATTACHMENT_EXPRESSION
    assert "attachment_fetch_timeout" in snapshot.TALKROOM_ATTACHMENT_EXPRESSION


def test_talkroom_readback_falls_back_to_metadata_when_attachment_capture_times_out(monkeypatch) -> None:
    snapshot = load("coconala_queue_snapshot")
    captures = []

    class Tab:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return SimpleNamespace(ws="ws://ready")

        def __exit__(self, *_args):
            return False

    async def inspect(*_args, **kwargs):
        captures.append(kwargs["capture_buyer_attachments"])
        if kwargs["capture_buyer_attachments"]:
            raise TimeoutError
        return {"messages": [{"attachments": [{"filename": "banner.png"}]}]}

    monkeypatch.setattr(snapshot, "DefaultTab", Tab)
    monkeypatch.setattr(snapshot, "inspect_page", inspect)

    result = snapshot.inspect_page_with_retry(
        Path("helper"), "https://example.test", "1", None,
        capture_buyer_attachments=True,
    )

    assert captures == [True, False]
    assert result["messages"][0]["attachments"][0]["filename"] == "banner.png"


def test_paid_failure_preserves_machine_readable_step_and_diagnostic_detail() -> None:
    paid = load("paid_direct")
    error = paid.Failure("file_builder", "isolated_file_owner")
    assert error.step == "file_builder"
    assert error.detail == "isolated_file_owner"
    assert str(error) == "isolated_file_owner"


def test_paid_subprocess_failure_preserves_bounded_stderr() -> None:
    paid = load("paid_direct")
    with pytest.raises(paid.Failure) as caught:
        paid._run([
            sys.executable, "-c",
            "import sys; sys.stderr.write('source census startup failed'); raise SystemExit(7)",
        ], "file_builder")
    assert caught.value.step == "file_builder"
    assert "source census startup failed" in caught.value.detail


def test_isolated_paid_runner_preserves_runtime_package_layout(tmp_path: Path) -> None:
    paid = load("paid_direct")
    runner = Path(__file__).resolve().parents[4] / "runtime" / "agent-runner" / "agent_runner.py"
    schema = Path(__file__).resolve().parents[1] / "schemas" / "gig_step_result.schema.json"
    isolated_runner, isolated_schema = paid._stage_isolated_agent_runtime(
        runner, schema, tmp_path / "isolated",
    )
    assert isolated_runner == tmp_path / "isolated/runtime/agent-runner/agent_runner.py"
    assert isolated_schema.is_file()
    completed = __import__("subprocess").run(
        [sys.executable, str(isolated_runner), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_failed_paid_workspace_with_runner_evidence_is_preserved(tmp_path: Path) -> None:
    paid = load("paid_direct")
    root = tmp_path / "projects" / "123"
    root.mkdir(parents=True)
    workspace = None
    with pytest.raises(RuntimeError, match="boom"):
        with paid._project_workspace(root, "paid-source-census-") as raw:
            workspace = Path(raw)
            evidence = workspace / "evidence" / "attempt-01.stderr.log"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("runner failed before summary\n", encoding="utf-8")
            raise RuntimeError("boom")
    assert workspace is not None and workspace.is_dir()
    assert (workspace / "evidence" / "attempt-01.stderr.log").is_file()


def blocked_project(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "project"
    feedback = "a" * 64
    requirement = {"text": "Use the named provider and report the result.", "attachments": []}
    requirement_sha = hashlib.sha256(json.dumps(
        requirement, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    requirements_sha = hashlib.sha256(json.dumps(
        [requirement_sha], ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    desired = {"provider_state": "awaiting_reply"}
    digest = hashlib.sha256(json.dumps(
        desired, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    semantic_contract = {
        "decision": "actionable",
        "mode": "remote",
        "feedback_sha256": feedback,
        "requirements_sha256": requirements_sha,
        "required_output": "Report the completed provider outcome.",
        "required_effect": "Wait for and process the provider response.",
        "required_assets": [],
    }
    semantic_sha = hashlib.sha256(json.dumps(
        semantic_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    write_json(root / "requirements/live-buyer-reply.json", {
        "feedback_sha256": feedback,
        "accumulated_requirements": [{**requirement, "sha256": requirement_sha}],
        "accumulated_sha256": requirements_sha,
    })
    write_json(root / "delivery/paid-remote-intent.json", {
        "buyer_feedback_sha256": feedback,
        "requirements_sha256": requirements_sha,
        "target": "https://provider.example/status",
        "desired_state": desired,
        "desired_state_sha256": digest,
        "semantic_contract_sha256": semantic_sha,
    })
    write_json(root / "delivery/paid-remote-result.json", {
        "status": "blocked",
        "buyer_feedback_sha256": feedback,
        "requirements_sha256": requirements_sha,
        "target": "https://provider.example/status",
        "authenticated": True,
        "observed_state": desired,
        "after_state_digest": digest,
        "semantic_contract_sha256": semantic_sha,
        "blocker": "The provider has acknowledged the request but has not replied.",
        "business_outcome": {
            "required_effect_satisfied": False,
            "required_output_satisfied": False,
            "remaining_work": ["Wait for the provider reply."],
            "official_receipts": [{
                "provider": "provider.example",
                "kind": "inbox_thread_state",
                "url": "https://provider.example/status",
                "readback": "The request is acknowledged and no reply is present.",
            }],
        },
    })
    write_json(root / "context/paid-work-decision.json", semantic_contract)
    return root, feedback, digest


def test_current_blocked_remote_result_is_a_valid_wait(tmp_path):
    remote = load("paid_remote_result")
    root, feedback, digest = blocked_project(tmp_path)

    result = remote.validate_wait(root, feedback, digest, pass_start=0)

    assert result["status"] == "blocked"
    assert result["business_outcome"]["required_effect_satisfied"] is False


def test_completed_result_cannot_be_a_wait(tmp_path):
    remote = load("paid_remote_result")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["status"] = "ok"
    write_json(result_path, result)

    with pytest.raises(ValueError, match="not an external wait"):
        remote.validate_wait(root, feedback, digest, pass_start=0)


def test_business_outcome_accepts_provider_kind_result_as_official_source():
    paid = load("paid_direct")
    outcome = {
        "required_effect_satisfied": True,
        "required_output_satisfied": True,
        "remaining_work": [],
        "official_receipts": [{
            "effect_key": "gmail:reply:1",
            "official_url": "https://mail.google.com/mail/u/0/#inbox/thread",
            "provider": "Google Gmail",
            "kind": "authenticated_dom_readback",
            "result": "Exact sent message is visible in the official thread.",
            "exact_readback": True,
        }],
    }

    assert paid._validated_business_outcome({"business_outcome": outcome}) == outcome


def test_business_outcome_effect_match_ignores_descriptive_receipt_metadata():
    paid = load("paid_direct")
    builder = {
        "required_effect_satisfied": True,
        "required_output_satisfied": True,
        "remaining_work": [],
        "official_receipts": [{
            "effect_key": "gmail:reply:1",
            "official_url": "https://mail.google.com/mail/u/0/#inbox/thread",
            "provider": "Google Gmail",
            "kind": "authenticated_dom_readback",
            "result": "Exact sent message is visible.",
            "exact_readback": True,
        }],
    }
    verifier = {
        "required_effect_satisfied": True,
        "required_output_satisfied": True,
        "remaining_work": [],
        "official_receipts": [{
            "effect_key": "gmail:reply:1",
            "official_url": "https://mail.google.com/mail/u/0/#inbox/thread",
            "readback_source": "Authenticated Gmail DOM message node",
            "exact_readback": True,
        }],
    }

    assert paid._business_outcomes_match_effects(builder, verifier) is True


def test_formal_approval_survives_later_seller_acknowledgement(tmp_path):
    paid = load("paid_direct")
    root = tmp_path / "18130722"
    write_json(root / "state.json", {"talkroom_id": "18130722"})

    def row(side: str, message_id: str, text_value: str) -> dict:
        value = {
            "version": 1,
            "source": "coconala_live_talkroom",
            "talkroom_id": "18130722",
            "message_id": message_id,
            "observed_at": "2026-08-24T00:00:00Z",
            "side": side,
            "sent_at": None,
            "text": text_value,
            "attachments": [],
        }
        value["content_sha256"] = paid._official_content_sha256(value)
        return value

    buyer_row = row("buyer", "buyer-approved", "Approved; share the project and formally deliver.")
    seller_row = row("seller", "seller-ack", "Acknowledged; I will share it.")
    messages = root / "source/talkroom/messages.jsonl"
    messages.parent.mkdir(parents=True)
    messages.write_text(
        "\n".join(json.dumps(value, ensure_ascii=False) for value in (buyer_row, seller_row)) + "\n",
        encoding="utf-8",
    )
    latest = paid._latest_official_identity(root, "18130722")
    latest_buyer = paid._latest_official_buyer_identity(root, "18130722")
    decision = {
        "decision": "actionable",
        "mode": "file",
        "feedback_sha256": "a" * 64,
        "requirements_sha256": "b" * 64,
        "latest_message_identity": latest,
        "required_output": "Share the approved project package.",
        "required_effect": "Formally deliver after the share.",
        "required_assets": [{
            "asset_id": "project_package",
            "kind": "linked_asset",
            "minimum_count": 1,
            "buyer_visible_purpose": "Download the approved project package.",
            "source_authority": "builder",
            "archive_required": True,
        }],
        "delivery_stage": "formal",
        "formal_approval_evidence": latest_buyer,
        "unresolved": [],
    }

    assert latest["side"] == "seller"
    assert latest_buyer["side"] == "buyer"
    assert paid._validate_paid_decision(
        decision, "a" * 64, "b" * 64, latest, latest_buyer,
    ) == decision


def test_file_prepare_creates_missing_project_delivery_directory(tmp_path, monkeypatch):
    paid = load("paid_direct")
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(
        paid.paid_remote_result, "requirements_digest", lambda *_args: "a" * 64,
    )
    monkeypatch.setattr(
        paid.delivery_queue, "evidence_path", lambda *_args: tmp_path / "stable.json",
    )

    def observe_delivery(*_args):
        assert (root / "delivery").is_dir()
        raise RuntimeError("observed")

    monkeypatch.setattr(paid, "_validate_file_authorization", observe_delivery)

    with pytest.raises(RuntimeError, match="observed"):
        paid._prepare_file(
            SimpleNamespace(delivery_evidence_dir=tmp_path, projects_root=tmp_path),
            tmp_path / "item.json", root, {}, tmp_path / "base", "b" * 64,
        )


def test_concurrent_talkroom_history_persistence_does_not_duplicate_rows(tmp_path):
    queue = load("coconala_queue_snapshot")
    talkroom = {
        "history_complete": True,
        "messages": [{"message_id": "m1", "side": "buyer", "text": "hello\u2028world",
                      "sent_at": None, "attachments": []}],
    }
    barrier = threading.Barrier(2)

    def persist():
        barrier.wait()
        queue.persist_talkroom_history(
            talkroom, "18214856", tmp_path, "18214856", "2026-08-30T00:00:00Z")

    workers = [threading.Thread(target=persist) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    ledger = tmp_path / "18214856/source/talkroom/messages.jsonl"
    assert len([line for line in ledger.read_text(encoding="utf-8").split("\n") if line]) == 1


def test_paid_reader_preserves_unicode_line_separator_inside_json_string(tmp_path):
    paid = load("paid_direct")
    root = tmp_path / "18214856"
    ledger = root / "source/talkroom/messages.jsonl"
    ledger.parent.mkdir(parents=True)
    row = {
        "version": 1, "source": "coconala_live_talkroom", "talkroom_id": "18214856",
        "message_id": "m1", "observed_at": "2026-08-30T00:00:00Z",
        "content_sha256": "0" * 64, "side": "buyer", "sent_at": None,
        "text": "hello\u2028world", "attachments": [],
    }
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    ledger.write_text(encoded, encoding="utf-8")
    write_json(root / "state.json", {"talkroom_id": "18214856"})
    rows = paid._official_message_rows(root, "18214856")

    assert rows[0]["message_id"] == "m1"
    assert rows[0]["text"] == "hello\u2028world"


def test_prior_artifact_candidates_include_only_project_receipt_linked_zips(tmp_path):
    paid = load("paid_direct")
    root = tmp_path / "project"
    delivery_zip = root / "delivery" / "current.zip"
    receipt_zip = root / "deliverables" / "approved" / "final.zip"
    unrelated_zip = root / "deliverables" / "unrelated.zip"
    for path in (delivery_zip, receipt_zip, unrelated_zip):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    write_json(root / "acceptance" / "upload-receipt.json", {
        "status": "uploaded", "artifact": str(receipt_zip),
    })

    assert paid._prior_artifact_candidates(root) == [delivery_zip, receipt_zip]


def test_formal_handoff_does_not_carry_superseded_complaints_forward():
    paid = load("paid_direct")

    instruction = paid._file_customer_message_instruction()

    assert "latest buyer-side message" in instruction
    assert "later explicit buyer approval supersedes" in instruction


def test_next_artifact_version_includes_receipt_linked_prior_candidates(tmp_path):
    paid = load("paid_direct")
    root = tmp_path / "project"
    (root / "delivery").mkdir(parents=True)
    write_json(root / "state.json", {"current_version": "v97"})
    prior = root / "prior" / "approved-v107-package.zip"
    prior.parent.mkdir()
    prior.write_bytes(b"zip")

    assert paid._next_artifact_version(root, [prior]) == "v108"


def test_delivery_cadence_accepts_oversize_linked_asset_and_latest_buyer_approval(tmp_path, monkeypatch):
    cadence = load("delivery_cadence")
    artifact = tmp_path / "package-v1.zip"
    artifact.write_bytes(b"linked package")
    acceptance = tmp_path / "acceptance.json"
    write_json(acceptance, {"status": "PASS"})
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    buyer = {"message_id": "buyer-approval", "content_sha256": "a" * 64, "side": "buyer"}
    seller = {"message_id": "seller-ack", "content_sha256": "b" * 64, "side": "seller"}
    monkeypatch.setattr(cadence, "MARKETPLACE_ARTIFACT_MAX_BYTES", 0)
    item = {
        "artifact_path": str(artifact), "artifact_version": "v1",
        "acceptance_status": "PASS", "acceptance_evidence_path": str(acceptance),
        "package_sha256": digest, "blockers": [],
        "required_assets": [{"asset_id": "package", "kind": "linked_asset", "minimum_count": 1}],
        "artifact_assets": [{"asset_id": "package", "type": "linked_asset", "path": str(artifact)}],
        "formal_approval_evidence": buyer,
        "latest_message_identity": seller,
        "latest_buyer_message_identity": buyer,
    }

    assert cadence._artifact_ready(item) is True
    assert cadence.delivery_decision(item)["mode"] == "formal"


def test_review_ready_acceptance_routes_to_progress_not_formal(tmp_path):
    cadence = load("delivery_cadence")
    queue = load("delivery_queue")
    artifact = tmp_path / "draft-v1.wav"
    artifact.write_bytes(b"review draft")
    acceptance = tmp_path / "acceptance.json"
    write_json(acceptance, {"status": "REVIEW_READY"})
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    item = {"talkroom_id": "123", "buyer_feedback_stage": "initial_request"}
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    write_json(queue.evidence_path(evidence_root, item), {
        "status": "REVIEW_READY",
        "project_root": str(tmp_path),
        "artifact_path": str(artifact),
        "artifact_version": "v1",
        "acceptance_status": "REVIEW_READY",
        "acceptance_evidence_path": str(acceptance),
        "package_sha256": digest,
    })

    evidence, blockers = queue.delivery_gate(item, evidence_root, tmp_path / "projects")
    decision = cadence.delivery_decision({**item, **evidence, "blockers": blockers})

    assert "missing_acceptance_evidence" not in blockers
    assert decision["mode"] == "progress"
    assert decision["formal_delivery_checkbox"] is False


def test_formal_browser_accepts_latest_buyer_approval_and_linked_asset():
    formal = load("coconala_formal_delivery_browser")
    buyer = {"message_id": "buyer-approval", "content_sha256": "a" * 64, "side": "buyer"}
    queue = {
        "formal_approval_evidence": buyer,
        "latest_message_identity": {
            "message_id": "seller-ack", "content_sha256": "b" * 64, "side": "seller",
        },
        "latest_buyer_message_identity": buyer,
        "delivery_evidence": {
            "required_assets": [{"asset_id": "package", "kind": "linked_asset", "minimum_count": 1}],
            "artifact_assets": [{"asset_id": "package", "type": "linked_asset"}],
        },
    }

    assert formal._formal_approval_ready(queue) is True
    assert formal._linked_asset_delivery(queue["delivery_evidence"]) is True


def test_paid_queue_accepts_completed_linked_formal_readback():
    evidence = load("paid_queue_evidence")
    linked = {
        "required_assets": [{"asset_id": "package", "kind": "linked_asset", "minimum_count": 1}],
        "artifact_assets": [{"asset_id": "package", "type": "linked_asset"}],
    }

    assert evidence._linked_asset_delivery(linked) is True
    assert evidence._formal_transaction_state_ready("取引完了") is True


def test_paid_queue_accepts_linked_contract_when_dom_proves_uploaded_attachment(tmp_path):
    evidence = load("paid_queue_evidence")
    artifact = tmp_path / "review-v3.zip"
    artifact.write_bytes(b"review package")
    screenshot = tmp_path / "paid-queue-screenshot.png"
    screenshot.write_bytes(b"png")
    live_dom = tmp_path / "paid-queue-live-dom.json"
    write_json(live_dom, {
        "url": "https://coconala.com/talkrooms/18223833",
        "sent": True,
        "formal_delivery_control_checked": False,
        "latest_seller_attachment": {
            "filename": artifact.name,
            "size_bytes": artifact.stat().st_size,
            "message": "review package uploaded",
        },
    })
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    delta = ["review package uploaded"]
    write_json(tmp_path / "paid-queue-evidence.json", {
        "sent": True,
        "formal_delivery_checkbox": False,
        "captured_at": "2026-09-08T15:00:00Z",
        "screenshot_path": str(screenshot),
        "live_dom_path": str(live_dom),
        "artifact_basename": artifact.name,
        "artifact_version": "v3",
        "package_sha256": digest,
        "acceptance_delta": delta,
        "talkroom_id": "18223833",
        "expected_url": "https://coconala.com/talkrooms/18223833",
    })
    expected = {
        "talkroom_id": "18223833",
        "marketplace_url": "https://coconala.com/talkrooms/18223833",
        "delivery_action": "progress",
        "delivery_evidence": {
            "artifact_path": str(artifact),
            "artifact_version": "v3",
            "package_sha256": digest,
            "acceptance_delta": delta,
            "customer_message": "review package uploaded",
            "required_assets": [
                {"asset_id": "package", "kind": "linked_asset", "minimum_count": 1},
            ],
            "artifact_assets": [
                {"asset_id": "package", "type": "linked_asset", "path": str(artifact)},
            ],
        },
    }

    assert evidence.validate_paid_queue(tmp_path, expected) == (True, [])


def test_reported_formal_cycle_accepts_exact_linked_message_readback(tmp_path, monkeypatch):
    paid = load("paid_direct")
    projects = tmp_path / "projects"
    root = projects / "18130722"
    artifact = root / "delivery" / "package-v1.zip"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"package")
    message = "正式な納品とさせていただきます。"
    event = {
        "event": "FORMAL_DELIVERY_CONFIRMED", "project_id": "18130722",
        "talkroom_id": "18130722", "linked_asset_delivery": True,
        "seller_attachment_readback": None, "seller_message_readback": message,
        "artifact_path": str(artifact), "artifact_bytes": artifact.stat().st_size,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    (root / "events.jsonl").write_text(json.dumps(event, ensure_ascii=False) + "\n")
    feedback = "c" * 64
    write_json(root / "state.json", {
        "formal_delivery_confirmed": True,
        "handled_buyer_feedback_sha256": feedback,
    })
    monkeypatch.setattr(paid.delivery_project, "resolve_project_root", lambda *_args: root)
    item = {
        "talkroom_id": "18130722", "formal_delivery_observed": False,
        "talkroom_state": "取引完了", "buyer_feedback_pending_artifact": True,
        "buyer_feedback_sha256": feedback,
        "seller_messages": [{"text": message, "attachments": []}],
    }

    assert paid._reported_formal_cycle(SimpleNamespace(projects_root=projects), item) == root


def test_wait_accepts_supplementary_receipt_when_another_has_readback(tmp_path):
    remote = load("paid_remote_result")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["business_outcome"]["official_receipts"].insert(0, {
        "provider": "provider.example",
        "kind": "completion_page",
        "url": "https://provider.example/thanks",
        "title": "Request received",
    })
    write_json(result_path, result)

    assert remote.validate_wait(root, feedback, digest, pass_start=0)["status"] == "blocked"


def test_wait_accepts_exact_readback_receipt_shape(tmp_path):
    remote = load("paid_remote_result")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["business_outcome"]["official_receipts"] = [{
        "provider": "provider.example",
        "kind": "inbox_thread_state",
        "official_url": "https://provider.example/status",
        "readback_source": "provider API",
        "exact_readback": True,
    }]
    write_json(result_path, result)

    assert remote.validate_wait(root, feedback, digest, pass_start=0)["status"] == "blocked"


def test_wait_accepts_legacy_result_as_readback_and_kind(tmp_path):
    remote = load("paid_remote_result")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["business_outcome"]["official_receipts"] = [{
        "provider": "provider.example",
        "url": "https://provider.example/status",
        "result": "Official status page shows the request is still pending.",
    }]
    write_json(result_path, result)

    assert remote.validate_wait(root, feedback, digest, pass_start=0)["status"] == "blocked"


def test_paid_direct_maps_valid_blocked_owner_to_pending(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)

    assert paid._remote_owner_checkpoint(
        "blocked", root, feedback, digest, pass_start=0,
    ) == "pending"


def test_paid_direct_maps_verified_authentication_blocker_to_pending(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["authenticated"] = False
    result["business_outcome"]["official_receipts"] = [{
        "provider": "provider.example",
        "kind": "seller_login_recovery_readback",
        "url": "https://provider.example/login",
        "readback": "The provider rendered no authenticated owner view or login form.",
    }]
    write_json(result_path, result)

    assert paid._remote_owner_checkpoint(
        "blocked", root, feedback, digest, pass_start=0,
    ) == "pending"


@pytest.mark.parametrize("kind", [
    "authenticated_identity_readback",
    "official_authenticated_identity_readback",
])
def test_paid_direct_maps_verified_identity_authentication_blocker_to_pending(
        tmp_path, kind):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["authenticated"] = False
    result["business_outcome"]["official_receipts"] = [{
        "provider": "provider.example",
        "kind": kind,
        "url": "https://provider.example/identity",
        "readback": "The official identity surface is not authenticated.",
    }]
    write_json(result_path, result)

    assert paid._remote_owner_checkpoint(
        "blocked", root, feedback, digest, pass_start=0,
    ) == "pending"


def test_remote_wait_rejects_unauthenticated_non_authentication_blocker(tmp_path):
    remote = load("paid_remote_result")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["authenticated"] = False
    write_json(result_path, result)

    with pytest.raises(ValueError, match="remote wait target mismatch"):
        remote.validate_wait(root, feedback, digest, pass_start=0)


def test_normalizer_repairs_missing_blocker_from_first_remaining_work(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("blocker")
    write_json(result_path, result)

    paid._normalize_builder_result(root)

    normalized = json.loads(result_path.read_text(encoding="utf-8"))
    assert normalized["blocker"] == "Wait for the provider reply."
    assert paid.paid_remote_result.validate_wait(
        root, feedback, digest, pass_start=0,
    )["status"] == "blocked"


def test_normalizer_does_not_invent_blocker_for_malformed_remaining_work(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    result_path = root / "delivery/paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("blocker")
    result["business_outcome"]["remaining_work"] = [{"text": "Wait for the provider reply."}]
    write_json(result_path, result)
    intent = json.loads((root / "delivery/paid-remote-intent.json").read_text(encoding="utf-8"))
    write_json(root / "evidence/agent-PAID_REMOTE_OWNER/wait.json", {
        "authenticated": True,
        "target": intent["target"],
        "requirements_sha256": intent["requirements_sha256"],
        "observed_state": intent["desired_state"],
    })

    paid._normalize_builder_result(root)

    normalized = json.loads(result_path.read_text(encoding="utf-8"))
    assert "blocker" not in normalized
    with pytest.raises(ValueError, match="not an external wait"):
        paid.paid_remote_result.validate_wait(root, feedback, digest, pass_start=0)


def test_remote_owner_prompt_includes_exact_cycle_account_owner_policy(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)
    directive = "Do not contact the buyer for another verification code; use an authorized reusable account."
    write_json(root / "context/paid-file-operator-policy.json", {
        "version": 1,
        "authorized_by": "account_owner",
        "request_id": root.name,
        "buyer_feedback_sha256": feedback,
        "requirements_sha256": requirements_sha,
        "directives": [directive],
    })

    prompt = paid._repair_prompt(
        root, tmp_path / "item.json", feedback, requirements_sha,
        False, tmp_path / "cdp.py",
    )

    assert directive in prompt
    assert "account-owner policy" in prompt


def test_remote_owner_prompt_searches_complete_repo_and_valid_shared_tools(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)

    prompt = paid._repair_prompt(
        root, tmp_path / "item.json", feedback, requirements_sha,
        False, tmp_path / "cdp.py",
    )

    assert f"search {paid.REPO_ROOT} with rg" in prompt
    assert str(paid.REPO_ROOT / "skills/_shared/resource_resolver.py") in prompt
    assert str(paid.REPO_ROOT / "skills/browser/with-browser.sh") in prompt
    assert "write the durable result immediately before any optional exploration" in prompt
    assert "do not exhaustively inspect unrelated historical attachments or messages" in prompt
    assert "write status=blocked and a nonempty blocker in paid-remote-result.json" in prompt
    assert "every wait receipt include nonempty provider, kind, and url or official_url fields" in prompt
    assert "readback_source and exact_readback=true" in prompt


def test_remote_stage_leaves_coconala_delivery_to_verified_connector(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)

    for verifier in (False, True):
        prompt = paid._repair_prompt(
            root, tmp_path / "item.json", feedback, requirements_sha,
            verifier, tmp_path / "cdp.py",
        )
        assert "does not mean the Coconala message is already sent" in prompt
        assert "Never require or guess a fixed Coconala profile ID" in prompt
        assert "code-owned Coconala connector" in prompt


def test_paid_clients_use_independent_parallel_readbacks_and_browser_targets(tmp_path, monkeypatch):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)

    prompt = paid._repair_prompt(
        root, tmp_path / "item.json", feedback, requirements_sha,
        False, tmp_path / "cdp.py",
    )

    assert paid.PAID_MAX_PARALLEL_READBACKS == paid.PAID_MAX_PARALLEL_PROJECTS
    assert "All independent paid projects run concurrently" in prompt
    assert "serialize every read, mutation, and readback" not in prompt

    owners = []
    collector_output = {}

    def collector(_args, _mode, output, *_rest):
        collector_output["path"] = output
        return ["collector"]

    def run(_command, _step, **kwargs):
        owners.append(kwargs.get("env", {}).get("CLOAK_BROWSER_OWNER"))
        write_json(collector_output["path"], {"orders": [{"talkroom_id": "18211957"}]})

    monkeypatch.setattr(paid, "_collector", collector)
    monkeypatch.setattr(paid, "_run", run)
    monkeypatch.setattr(paid, "_row", lambda _snapshot, _room: {"talkroom_id": "18211957"})
    monkeypatch.setattr(paid, "_reclaim_browser_owner", lambda *_args: None)
    args = SimpleNamespace(evidence_dir=tmp_path, cdp_lock_dir=tmp_path / "locks")

    paid._targeted(args, {"talkroom_id": "18211957"}, 0)

    assert owners == ["paid-direct-18211957"]


def test_targeted_readback_reclaims_its_stale_owner_before_open(tmp_path, monkeypatch):
    paid = load("paid_direct")
    events = []
    collector_output = {}

    def reclaim(_args, owner):
        events.append(("reclaim", owner))

    def collector(_args, _mode, output, *_rest):
        collector_output["path"] = output
        return ["collector"]

    def run(_command, _step, **_kwargs):
        events.append(("open", _kwargs["env"]["CLOAK_BROWSER_OWNER"]))
        write_json(collector_output["path"], {"orders": [{"talkroom_id": "18223833"}]})

    monkeypatch.setattr(paid, "_reclaim_browser_owner", reclaim)
    monkeypatch.setattr(paid, "_collector", collector)
    monkeypatch.setattr(paid, "_run", run)
    monkeypatch.setattr(paid, "_row", lambda _snapshot, _room: {"talkroom_id": "18223833"})
    args = SimpleNamespace(evidence_dir=tmp_path, cdp_lock_dir=tmp_path / "locks")

    paid._targeted(args, {"talkroom_id": "18223833"}, 0)

    assert events == [
        ("reclaim", "paid-direct-18223833"),
        ("open", "paid-direct-18223833"),
    ]


def test_file_presend_reclaims_targeted_owner_before_open(tmp_path, monkeypatch):
    paid = load("paid_direct")
    root = tmp_path / "project"
    root.mkdir()
    feedback = "a" * 64
    requirements_sha = "b" * 64
    events = []

    monkeypatch.setattr(paid, "_paid_project_root", lambda *_args: root)
    monkeypatch.setattr(paid, "_file_mode", lambda *_args: True)
    monkeypatch.setattr(
        paid.paid_remote_result, "requirements_digest", lambda *_args: requirements_sha,
    )
    monkeypatch.setattr(paid.delivery_queue, "evidence_path", lambda *_args: tmp_path / "stable.json")
    monkeypatch.setattr(
        paid, "_validate_file_authorization", lambda *_args: {
            "artifact_version": "v1", "package_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr(
        paid, "_reclaim_browser_owner",
        lambda _args, owner: events.append(("reclaim", owner)),
    )
    monkeypatch.setattr(paid, "_collector", lambda *_args: ["collector"])

    def stop_after_open(_command, _step, **kwargs):
        events.append(("open", kwargs.get("env", {}).get("CLOAK_BROWSER_OWNER")))
        raise RuntimeError("stop after presend open")

    monkeypatch.setattr(paid, "_run", stop_after_open)
    args = SimpleNamespace(
        evidence_dir=tmp_path, delivery_evidence_dir=tmp_path,
        projects_root=tmp_path, cdp_lock_dir=tmp_path / "locks",
    )
    prepared = {
        "talkroom_id": "18223833", "buyer_feedback_sha256": feedback,
        "requirements_sha256": requirements_sha,
    }

    with pytest.raises(RuntimeError, match="stop after presend open"):
        paid._write_file_effect(args, tmp_path / "item.json", tmp_path / "result.json", prepared)

    assert events == [
        ("reclaim", "paid-direct-18223833"),
        ("open", "paid-direct-18223833"),
    ]


def test_remote_verifier_prompt_persists_decision_before_optional_exploration(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)

    prompt = paid._repair_prompt(
        root, tmp_path / "item.json", feedback, requirements_sha,
        True, tmp_path / "cdp.py",
    )

    assert "write that result immediately before any optional exploration" in prompt
    assert "do not exhaustively inspect unrelated historical attachments or messages" in prompt


def test_remote_owner_prompt_reconciles_project_effect_receipts_before_mutation(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)

    prompt = paid._repair_prompt(
        root, tmp_path / "item.json", feedback, requirements_sha,
        False, tmp_path / "cdp.py",
    )

    assert "project-owned external-effect receipts" in prompt
    assert "official provider and matching bookkeeping readback" in prompt
    assert "Never repeat an effect whose receipt is already verified" in prompt


def test_paid_agent_creates_authorized_missing_resources_instead_of_asking_buyer(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)
    identity = {"message_id": "m1", "content_sha256": "d" * 64, "side": "buyer"}

    decision_prompt = paid._decision_prompt(
        tmp_path / "context.json", "a" * 64, feedback, requirements_sha,
        identity, identity,
    ).decode()
    owner_prompt = paid._repair_prompt(
        root, tmp_path / "item.json", feedback, requirements_sha,
        False, tmp_path / "cdp.py",
    )

    for prompt in (decision_prompt, owner_prompt):
        assert "available=false proves only that no reusable resource is registered" in prompt
        assert "create or recover the seller-owned resource autonomously" in prompt
        assert "Never ask the buyer to supply a seller-owned account" in prompt


def test_orders_observation_retries_until_semantic_container_is_ready(tmp_path, monkeypatch):
    snapshot = load("coconala_queue_snapshot")
    calls = []
    responses = [
        {
            "url": snapshot.OPEN_ORDERS_URL,
            "title": "受注管理",
            "container_present": False,
            "cards": [],
            "empty_state_present": False,
        },
        {
            "url": snapshot.OPEN_ORDERS_URL,
            "title": "受注管理",
            "container_present": True,
            "cards": [{"talkroom_url": "https://coconala.com/talkrooms/1"}],
            "empty_state_present": False,
        },
    ]

    def inspect(*_args, **_kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(snapshot, "inspect_page_with_retry", inspect)
    monkeypatch.setattr(snapshot.time, "sleep", lambda _seconds: None)

    dom, coverage = snapshot.inspect_orders_when_ready(
        tmp_path / "cdp.py", tmp_path / "evidence", hidden=True,
    )

    assert len(calls) == 2
    assert dom["container_present"] is True
    assert coverage["coverage_complete"] is True


def test_decision_prompt_scopes_required_assets_to_current_bounded_output(tmp_path):
    paid = load("paid_direct")

    identity = {"message_id": "m1", "content_sha256": "d" * 64, "side": "buyer"}
    prompt = paid._decision_prompt(
        tmp_path / "context.json", "a" * 64, "b" * 64, "c" * 64,
        identity, identity,
    ).decode()

    assert "current bounded output" in prompt
    assert "future event" in prompt
    assert "Do not hide a required asset only in unresolved" not in prompt


def test_decision_prompt_keeps_live_system_revisions_remote_and_url_only(tmp_path):
    paid = load("paid_direct")

    identity = {"message_id": "m1", "content_sha256": "d" * 64, "side": "buyer"}
    prompt = paid._decision_prompt(
        tmp_path / "context.json", "a" * 64, "b" * 64, "c" * 64,
        identity, identity,
    ).decode()

    assert "already-published live system" in prompt
    assert "choose remote until the live revision and its official verification are complete" in prompt
    assert "send its verified HTTPS review URL without a file attachment" in prompt
    assert "explicitly asks for source files, an archive, or a download" in prompt


def test_selected_talkroom_readback_uses_visible_transport_for_attachments(tmp_path, monkeypatch):
    snapshot = load("coconala_queue_snapshot")
    seen = []
    args = SimpleNamespace(
        mode="selected-talkroom-only",
        talkroom_id="18211957",
        project_id="18211957",
        selected_order_input={"talkroom_id": "18211957"},
        hidden_no_screenshot=True,
        evidence_dir=tmp_path / "evidence",
        output=tmp_path / "snapshot.json",
        projects_root=tmp_path / "projects",
        cdp_helper=tmp_path / "cdp.py",
    )

    monkeypatch.setattr(
        snapshot, "argument_parser",
        lambda: SimpleNamespace(parse_args=lambda: args),
    )
    monkeypatch.setattr(snapshot, "load_connector_manifest", lambda: None)

    def stop_after_transport_selection(*_args, **kwargs):
        seen.append(kwargs["hidden"])
        raise RuntimeError("stop after transport selection")

    monkeypatch.setattr(snapshot, "inspect_page_with_retry", stop_after_transport_selection)

    assert snapshot.main() == 1
    assert seen == [False]


def test_current_remote_wait_is_fresh(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    mtime = (root / "delivery/paid-remote-result.json").stat().st_mtime

    assert paid._remote_wait_is_fresh(root, feedback, digest, now=mtime + 10) is True


def test_stale_paid_answer_must_not_hide_current_officially_read_back_remote_completion(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    result_path = root / "delivery" / "paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    current_message = "Current remote completion\nread back officially."
    result.update({
        "status": "completed",
        "buyer_feedback_sha256": feedback,
        "customer_message": current_message,
        "business_outcome": {
            "required_effect_satisfied": True,
            "required_output_satisfied": True,
            "remaining_work": [],
        },
    })
    write_json(result_path, result)
    write_json(root / "delivery" / "paid-answer.json", {
        "status": "answer", "message": "Older stale paid answer.",
    })
    intent_path = root / "delivery" / "paid-remote-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent.pop("mode", None)
    write_json(intent_path, intent)
    item = {
        "request_id": root.name,
        "talkroom_id": "current-room",
        "buyer_feedback_sha256": feedback,
        "seller_messages": [{"text": " Current remote completion read back officially. "}],
        "formal_delivery_observed": False,
        "formal_delivery_confirmed": False,
        "talkroom_state": "取引中",
        "transaction_state": "取引中",
    }

    assert paid._reported_remote_cycle(
        SimpleNamespace(projects_root=tmp_path), item,
    ) == root


def _reported_remote_completion_case(
    tmp_path: Path, *, result_message: str, answer_message: str = "Older stale paid answer.",
):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    result_path = root / "delivery" / "paid-remote-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update({
        "status": "completed",
        "buyer_feedback_sha256": feedback,
        "customer_message": result_message,
        "business_outcome": {
            "required_effect_satisfied": True,
            "required_output_satisfied": True,
            "remaining_work": [],
        },
    })
    write_json(result_path, result)
    write_json(root / "delivery" / "paid-answer.json", {
        "status": "answer", "message": answer_message,
    })
    intent_path = root / "delivery" / "paid-remote-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent.pop("mode", None)
    write_json(intent_path, intent)
    item = {
        "request_id": root.name,
        "talkroom_id": "current-room",
        "buyer_feedback_sha256": feedback,
        "seller_messages": [{"text": f" {(result_message or answer_message)} "}],
        "formal_delivery_observed": False,
        "formal_delivery_confirmed": False,
        "talkroom_state": "取引中",
        "transaction_state": "取引中",
    }
    return paid, root, feedback, SimpleNamespace(projects_root=tmp_path), item


def test_result_replay_policy_freshness_uses_remote_result_checkpoint(tmp_path):
    paid, root, feedback, args, item = _reported_remote_completion_case(
        tmp_path, result_message="Current remote completion.",
    )
    result_path = root / "delivery" / "paid-remote-result.json"
    answer_path = root / "delivery" / "paid-answer.json"
    policy_path = root / "context" / "paid-file-operator-policy.json"
    requirements_sha256 = paid.paid_remote_result.requirements_digest(root, feedback)
    write_json(policy_path, {
        "version": 1,
        "authorized_by": "account_owner",
        "request_id": root.name,
        "buyer_feedback_sha256": feedback,
        "requirements_sha256": requirements_sha256,
        "directives": ["Reconcile the current remote result."],
    })
    result_ns = result_path.stat().st_mtime_ns
    os.utime(policy_path, ns=(result_ns + 1_000_000, result_ns + 1_000_000))
    os.utime(answer_path, ns=(result_ns + 2_000_000, result_ns + 2_000_000))

    assert paid._reported_remote_cycle(args, item) is None


def test_completed_remote_result_requires_its_own_customer_message(tmp_path):
    paid, _root, _feedback, args, item = _reported_remote_completion_case(
        tmp_path, result_message="", answer_message="Fallback message must be rejected.",
    )

    assert paid._reported_remote_cycle(args, item) is None


def test_answer_decision_cannot_be_bypassed_by_untyped_remote_result_replay(tmp_path, monkeypatch):
    paid, _root, _feedback, args, item = _reported_remote_completion_case(
        tmp_path, result_message="Current remote completion.",
    )
    item.update({
        "buyer_feedback_pending_artifact": True,
        "buyer_visible_artifact_observed": False,
    })
    monkeypatch.setattr(
        paid, "_current_paid_decision",
        lambda *_args: {"decision": "actionable", "mode": "answer"},
    )

    assert paid._reported_remote_cycle(args, item) is None


def test_remote_wait_expires_after_recheck_interval(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    mtime = (root / "delivery/paid-remote-result.json").stat().st_mtime

    assert paid._remote_wait_is_fresh(root, feedback, digest, now=mtime + 3601) is False


def test_remote_wait_from_older_release_resumes_on_new_capability(tmp_path, monkeypatch):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    result = root / "delivery/paid-remote-result.json"
    release = tmp_path / "release"
    manifest = release / "RELEASE.json"
    write_json(manifest, {"sha": "a" * 40})
    result_mtime = result.stat().st_mtime
    os.utime(manifest, (result_mtime + 1, result_mtime + 1))
    monkeypatch.setattr(paid, "REPO_ROOT", release)

    assert paid._remote_wait_is_fresh(root, feedback, digest, now=result_mtime + 10) is False


def test_remote_wait_resumes_when_exact_cycle_policy_changes(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    result = root / "delivery/paid-remote-result.json"
    policy = root / "context/paid-file-operator-policy.json"
    write_json(policy, {"version": 1})
    result_mtime = result.stat().st_mtime
    os.utime(policy, (result_mtime + 1, result_mtime + 1))

    assert paid._remote_wait_is_fresh(root, feedback, digest, now=result_mtime + 10) is False


def test_future_dated_remote_wait_is_not_fresh(tmp_path):
    paid = load("paid_direct")
    root, feedback, digest = blocked_project(tmp_path)
    mtime = (root / "delivery/paid-remote-result.json").stat().st_mtime

    assert paid._remote_wait_is_fresh(root, feedback, digest, now=mtime - 1) is False


def test_current_wait_is_reused_before_semantic_decision(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)

    assert paid._remote_wait_before_decision(
        root, {"buyer_feedback_sha256": feedback}, now=None,
    ) is True


def test_newer_exact_cycle_operator_policy_invalidates_remote_wait(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)
    result = root / "delivery/paid-remote-result.json"
    policy = root / "context/paid-file-operator-policy.json"
    write_json(policy, {
        "version": 1,
        "authorized_by": "account_owner",
        "request_id": root.name,
        "buyer_feedback_sha256": feedback,
        "requirements_sha256": requirements_sha,
        "directives": ["Reconcile newly verified project evidence."],
    })
    policy.touch()
    result.touch()
    policy.touch()

    assert paid._remote_wait_before_decision(
        root, {"buyer_feedback_sha256": feedback}, now=None,
    ) is False


def test_newer_operator_policy_invalidates_reported_answer_checkpoint(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    answer = root / "delivery" / "paid-answer.json"
    write_json(answer, {"status": "answer", "message": "old result"})
    requirements_sha = paid.paid_remote_result.requirements_digest(root, feedback)
    policy = root / "context" / "paid-file-operator-policy.json"
    write_json(policy, {"version": 1, "authorized_by": "account_owner",
        "request_id": root.name, "buyer_feedback_sha256": feedback,
        "requirements_sha256": requirements_sha, "directives": ["Continue external work."]})
    answer.touch(); policy.touch()

    assert paid._operator_policy_newer_than(
        root, {"buyer_feedback_sha256": feedback}, answer,
    ) is True


def test_normalizer_restores_feedback_alias_and_canonical_digest(tmp_path):
    paid = load("paid_direct")
    root, feedback, _digest = blocked_project(tmp_path)
    intent_path = root / "delivery/paid-remote-intent.json"
    result_path = root / "delivery/paid-remote-result.json"
    intent = json.loads(intent_path.read_text())
    result = json.loads(result_path.read_text())
    result["status"] = "completed"
    intent.pop("buyer_feedback_sha256")
    result.pop("buyer_feedback_sha256")
    intent["feedback_sha256"] = feedback
    result["feedback_sha256"] = feedback
    after = root / "evidence/agent-PAID_REMOTE_OWNER/after.json"
    write_json(after, {
        "authenticated": True,
        "target": intent["target"],
        "observed_state": intent["desired_state"],
    })
    result["after_evidence"] = str(after.relative_to(root))
    for record in (intent, result):
        for key in ("desired_state_sha256", "desired_digest", "after_state_digest", "observed_digest"):
            if key in record:
                record[key] = "0" * 64
    write_json(intent_path, intent)
    write_json(result_path, result)

    paid._normalize_builder_result(root)

    intent = json.loads(intent_path.read_text())
    result = json.loads(result_path.read_text())
    digest = paid.paid_remote_result._sha(intent["desired_state"])
    assert intent["buyer_feedback_sha256"] == feedback
    assert result["buyer_feedback_sha256"] == feedback
    assert intent["desired_state_sha256"] == intent["desired_digest"] == digest
    assert result["desired_state_sha256"] == result["desired_digest"] == digest
    assert result["after_state_digest"] == result["observed_digest"] == digest
    assert result["status"] == "ok"
    assert result["verified_after"] is True


def test_paid_project_executor_runs_different_owners_in_parallel():
    paid = load("paid_direct")
    active = 0
    maximum = 0
    lock = threading.Lock()

    def work():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    with paid._paid_project_executor() as executor:
        futures = [executor.submit(work) for _ in range(2)]
        for future in futures:
            future.result()

    assert maximum == 2


def test_paid_model_runner_runs_different_projects_in_parallel(tmp_path, monkeypatch):
    paid = load("paid_direct")
    projects = tmp_path / "gig" / "projects"
    roots = [projects / "one", projects / "two"]
    for root in roots:
        root.mkdir(parents=True)
    active = 0
    maximum = 0
    guard = threading.Lock()

    def run(_command, _step):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return "ok"

    monkeypatch.setattr(paid, "_run", run)
    monkeypatch.setattr(paid, "_private_model_runner", lambda _root, command, _label: command)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(paid._run_private_model_serialized, root, ["agent"], "label", "step")
                   for root in roots]
        assert [future.result() for future in futures] == ["ok", "ok"]

    assert maximum == 2


def test_paid_effect_owner_lease_is_held_through_run(tmp_path, monkeypatch):
    paid = load("paid_direct")
    root = tmp_path / "gig" / "projects" / "one"
    root.mkdir(parents=True)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def run(command, step):
        calls.append((command, step))
        entered.set()
        assert release.wait(timeout=1)
        return "ok"

    monkeypatch.setattr(paid, "_run", run)
    monkeypatch.setattr(paid, "_private_model_runner", lambda _root, command, _label: command)
    worker_result = []

    def invoke_first():
        try:
            worker_result.append(("ok", paid._run_private_model_serialized(
                root, ["first"], "label", "step", effect_owner=True,
            )))
        except BaseException as error:
            worker_result.append(("error", error))

    worker = threading.Thread(
        target=invoke_first,
        daemon=True,
    )
    second_result = []
    second_done = threading.Event()

    def invoke_second():
        try:
            second_result.append(("ok", paid._run_private_model_serialized(
                root, ["second"], "label", "step", effect_owner=True,
            )))
        except BaseException as error:
            second_result.append(("error", error))
        finally:
            second_done.set()

    second = threading.Thread(target=invoke_second, daemon=True)
    try:
        worker.start()
        assert entered.wait(timeout=1)
        second.start()
        assert second_done.wait(timeout=1), "effect-owner lease blocked instead of failing fast"
        assert len(second_result) == 1
        assert second_result[0][0] == "error"
        assert isinstance(second_result[0][1], paid.Failure)
        assert second_result[0][1].step == "remote_owner_busy"
        assert calls == [(["first"], "step")]
    finally:
        release.set()
        worker.join(timeout=1)
        second.join(timeout=1)
    assert not worker.is_alive()
    assert worker_result == [("ok", "ok")]


def test_paid_model_runner_does_not_wait_on_legacy_lock(tmp_path, monkeypatch):
    paid = load("paid_direct")
    root = tmp_path / "gig" / "projects" / "one"
    root.mkdir(parents=True)
    lock_path = root.parents[1] / ".paid-model-runner.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    called = threading.Event()
    result = []

    def run(_command, _step):
        called.set()
        return "ok"

    monkeypatch.setattr(paid, "_run", run)
    monkeypatch.setattr(paid, "_private_model_runner", lambda _root, command, _label: command)
    worker = threading.Thread(
        target=lambda: result.append(
            paid._run_private_model_serialized(root, ["agent"], "label", "step")
        ),
        daemon=True,
    )
    try:
        worker.start()
        assert called.wait(timeout=1), "legacy Paid lock blocked the model runner"
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert result == ["ok"]
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        worker.join(timeout=1)


def test_answer_receipt_does_not_close_pending_buyer_artifact():
    paid = load("paid_direct")

    assert paid._answer_cycle_may_close({
        "buyer_feedback_pending_artifact": True,
        "buyer_visible_artifact_observed": False,
    }) is False
    assert paid._answer_cycle_may_close({
        "buyer_feedback_pending_artifact": False,
        "buyer_visible_artifact_observed": False,
    }) is True


def test_successful_external_artifact_receipt_is_reverified():
    paid = load("paid_direct")
    instruction = paid._owner_tool_result_instruction()

    assert "status=success" in instruction
    assert "independently verify every declared artifact and acceptance hash" in instruction
    assert "commercial-use evidence" in instruction
    assert "not an automatic approval" in instruction


def test_resumed_file_owner_refreshes_controller_tool_results(tmp_path):
    paid = load("paid_direct")
    root, staging = tmp_path / "root", tmp_path / "staging"
    (root / "context").mkdir(parents=True)
    (staging / "context").mkdir(parents=True)
    artifact = root / "delivery" / "artifact.zip"
    acceptance = root / "acceptance" / "acceptance.json"
    rights = root / "evidence" / "rights.json"
    for path, content in ((artifact, b"zip"), (acceptance, b"accept"), (rights, b"rights")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    success = {
        "status": "success",
        "artifact": {"path": str(artifact), "sha256": hashlib.sha256(b"zip").hexdigest()},
        "acceptance": {"path": str(acceptance), "sha256": hashlib.sha256(b"accept").hexdigest()},
        "rights_and_correspondence": {
            "path": str(rights), "sha256": hashlib.sha256(b"rights").hexdigest(),
        },
    }
    (root / "context" / "paid-tool-results.json").write_text(json.dumps(success))
    (staging / "context" / "paid-tool-results.json").write_text('{"status":"failed"}')

    paid._refresh_owner_controller_context(root, staging)

    refreshed = json.loads((staging / "context" / "paid-tool-results.json").read_text())
    for field in ("artifact", "acceptance", "rights_and_correspondence"):
        copied = Path(refreshed[field]["path"])
        assert copied.is_file()
        copied.relative_to(staging)


def test_empty_tool_request_cannot_overwrite_success_receipt(tmp_path):
    paid = load("paid_direct")
    staging, root = tmp_path / "staging", tmp_path / "root"
    (staging / "delivery").mkdir(parents=True)
    (root / "context").mkdir(parents=True)
    (staging / "delivery" / "paid-tool-requests.json").write_text(
        '{"version":1,"requests":[]}'
    )
    (staging / "delivery" / "paid-tool-results.json").write_text(
        '{"version":1,"results":[]}'
    )
    success = '{"version":1,"status":"success"}'
    (root / "context" / "paid-tool-results.json").write_text(success)

    paid._persist_owner_tool_failure(staging, root)

    assert (root / "context" / "paid-tool-results.json").read_text() == success


def test_empty_tool_request_is_consumed_as_no_request(tmp_path):
    paid = load("paid_direct")
    (tmp_path / "delivery").mkdir()
    request = tmp_path / "delivery" / "paid-tool-requests.json"
    request.write_text('{"version":1,"requests":[]}')

    assert paid._execute_owner_tool_requests(tmp_path, tmp_path) == 0
    assert not request.exists()


def test_fresh_owner_staging_tolerates_precreated_context_directory(tmp_path):
    paid = load("paid_direct")
    root, staging = tmp_path / "root", tmp_path / "staging"
    for name in ("requirements", "source", "context"):
        (root / name).mkdir(parents=True)
    (root / "context" / "current.json").write_text("{}")
    (root / "state.json").write_text("{}")
    (staging / "context").mkdir(parents=True)

    paid._prepare_file_owner_staging(root, root / "context" / "current.json", staging)

    assert (staging / "context" / "current.json").is_file()


def test_single_member_archive_uses_its_actual_utf8_name(tmp_path):
    paid = load("paid_direct")
    archive = tmp_path / "review.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("硝子色の恋_review_v3.mp3", b"audio")

    name, data = paid._archive_member_data(archive, "硝子色の恋_review_v1.mp3")

    assert name == "硝子色の恋_review_v3.mp3"
    assert data == b"audio"


def test_repair_finding_only_applies_to_rejected_artifact_hash():
    paid = load("paid_direct")
    state = {
        "state": "REPAIR_PENDING",
        "mode": "file",
        "artifact_sha256": "a" * 64,
    }

    assert paid._repair_finding_applies(state, "a" * 64) is True
    assert paid._repair_finding_applies(state, "b" * 64) is False


def test_paid_preflight_uses_one_shared_browser_lock(tmp_path, monkeypatch):
    paid = load("paid_direct")
    called = []
    monkeypatch.setattr(paid, "_run", lambda command, step: called.append((command, step)) or "ok")
    args = SimpleNamespace(cdp_lock_dir=tmp_path / ".cdp-gig.lock")

    assert paid._run_paid_preflight(args, ["collector"]) == "ok"
    assert called == [(["collector"], "remote_resume")]
    assert (tmp_path / ".paid-preflight-browser.lock").is_file()


def test_effect_process_diagnostic_keeps_returncode_and_bounded_output():
    paid = load("paid_direct")
    process = SimpleNamespace(returncode=-9, stdout="out", stderr="err")

    assert paid._effect_process_diagnostic(process) == {
        "returncode": -9,
        "stdout_tail": "out",
        "stderr_tail": "err",
    }


def test_paid_effect_child_does_not_take_global_browser_lock(tmp_path):
    paid = load("paid_direct")
    args = SimpleNamespace(**{
        name: tmp_path / name for name in (
            "run_with_cdp_lock", "evidence_dir", "projects_root", "collector",
                "answer_browser", "formal_browser", "cancel_browser", "delivery_evidence_dir", "cdp_helper",
            "context_compiler", "dm_collector", "agent_runner", "runner_schema",
            "artifact_schema", "cdp_lock_dir",
        )
    }, today="2026-08-24")

    command = paid._effect_command(
        args, tmp_path / "item-18183618-prepared.json", tmp_path / "result.json",
    )

    assert command[0] == sys.executable
    assert "--write-item" in command
    assert str(args.run_with_cdp_lock) not in command


def test_paid_child_env_scopes_browser_owner_by_talkroom(tmp_path):
    paid = load("paid_direct")
    args = SimpleNamespace(cdp_lock_dir=tmp_path / "legacy-lock")

    env = paid._fresh_child_env(args, owner="paid-direct-18183618")

    assert env["CLOAK_BROWSER_OWNER"] == "paid-direct-18183618"
    assert "GIG_CDP_LOCK_HELD" not in env


def test_paid_browser_owners_are_scoped_by_talkroom():
    paid = load("paid_direct")

    assert paid._paid_browser_owners("18183618") == (
        "paid-direct-18183618",
        "paid-direct-18183618-remote-builder",
        "paid-direct-18183618-remote-verifier",
    )


def test_paid_reclaims_only_the_current_talkroom_tabs(tmp_path, monkeypatch):
    paid = load("paid_direct")
    calls = []
    monkeypatch.setattr(
        paid.subprocess, "run",
        lambda argv, **kwargs: calls.append((argv, kwargs["env"]["CLOAK_BROWSER_OWNER"])),
    )

    paid._reclaim_paid_tabs(
        SimpleNamespace(cdp_helper=tmp_path / "cdp_default_tab.py", cdp_lock_dir=tmp_path),
        "18183618",
    )

    assert [(call[0][-1], call[1]) for call in calls] == [
        (owner, owner) for owner in paid._paid_browser_owners("18183618")
    ]


def test_paid_reclaims_parent_owner_without_touching_siblings(tmp_path, monkeypatch):
    paid = load("paid_direct")
    calls = []
    monkeypatch.setattr(paid.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    paid._reclaim_browser_owner(
        SimpleNamespace(cdp_helper=tmp_path / "cdp_default_tab.py", cdp_lock_dir=tmp_path),
        "gig-paid-direct",
    )

    assert calls == [[sys.executable, str(tmp_path / "cdp_default_tab.py"),
                      "close-owned", "--owner", "gig-paid-direct"]]


def test_effect_process_diagnostic_is_bounded():
    paid = load("paid_direct")
    process = SimpleNamespace(returncode=75, stdout="x" * 2500, stderr="deferred_cdp_busy")

    diagnostic = paid._effect_process_diagnostic(process)

    assert diagnostic == {
        "returncode": 75,
        "stdout_tail": "x" * 2000,
        "stderr_tail": "deferred_cdp_busy",
    }


def test_paid_admission_selects_independent_projects_in_same_wake(tmp_path):
    paid = load("paid_direct")
    args = SimpleNamespace(projects_root=tmp_path)
    items = [
        {"talkroom_id": "101", "buyer": "buyer-a"},
        {"talkroom_id": "102", "buyer": "buyer-b"},
    ]

    admitted = paid._admitted_paid_projects(args, items)

    assert [item["talkroom_id"] for item in admitted] == ["101", "102"]


def test_paid_admission_includes_all_available_orders_beyond_worker_width(tmp_path):
    paid = load("paid_direct")
    args = SimpleNamespace(projects_root=tmp_path)
    items = [
        {"talkroom_id": str(101 + index), "buyer": f"buyer-{index}"}
        for index in range(9)
    ]

    admitted = paid._admitted_paid_projects(args, items)

    assert [item["talkroom_id"] for item in admitted] == [str(101 + index) for index in range(9)]


def test_paid_observation_does_not_exclude_ryu_talkroom(tmp_path, monkeypatch):
    paid = load("paid_direct")
    evidence = tmp_path / "orders"
    args = SimpleNamespace(collector=tmp_path / "collector", projects_root=tmp_path,
                           cdp_helper=tmp_path / "cdp", today="2026-09-04")
    snapshot = {
        "version": 1,
        "source": "authenticated_coconala_default_context_dom",
        "captured_at": "2026-09-04T12:00:00+00:00",
        "collector_mode": "orders-only",
        "observed_sources": ["orders"],
        "open_orders_list_observed": True,
        "orders": [{
            "talkroom_id": "18211957",
            "contract_id": "talkroom:18211957",
            "marketplace_url": "https://coconala.com/talkrooms/18211957",
            "status": "unknown",
            "buyer": "Ryu0820119",
        }],
        "source_receipt": {
            "source": "orders",
            "requested_route": "https://coconala.com/mypage/received_orders/open",
            "final_route": "https://coconala.com/mypage/received_orders/open",
            "login_redirect": False,
            "coverage_complete": True,
            "cards_count": 1,
            "empty_state_present": False,
        },
        "read_only": True,
    }
    write_json(evidence / "orders-only-snapshot.json", snapshot)
    monkeypatch.setattr(paid, "_collector", lambda *_args, **_kwargs: ["collector"])
    monkeypatch.setattr(paid, "_run", lambda *_args, **_kwargs: "")

    observed = paid.observe_orders(args, evidence)

    assert [item["talkroom_id"] for item in observed] == ["18211957"]


def test_paid_admission_skips_future_timed_retry_for_actionable_project(tmp_path):
    paid = load("paid_direct")
    args = SimpleNamespace(projects_root=tmp_path)
    items = [
        {"talkroom_id": "101", "buyer": "buyer-a"},
        {"talkroom_id": "102", "buyer": "buyer-b"},
    ]
    for item in items:
        root = tmp_path / item["talkroom_id"]
        root.mkdir(parents=True)
        write_json(root / "state.json", {"talkroom_id": item["talkroom_id"]})
    write_json(tmp_path / "101/context/paid-retry.json", {
        "version": 1,
        "status": "timed_retry",
        "retry_not_before": "2999-01-01T00:00:00+00:00",
        "reason": "provider_attempt_limit",
    })

    admitted = paid._admitted_paid_projects(args, items)

    assert [item["talkroom_id"] for item in admitted] == ["102"]


def test_paid_admission_orders_project_scoped_priority_without_excluding_others(tmp_path):
    paid = load("paid_direct")
    args = SimpleNamespace(projects_root=tmp_path)
    items = [
        {"talkroom_id": "101", "buyer": "buyer-a", "delivery_date": "2026-08-01"},
        {"talkroom_id": "102", "buyer": "buyer-b", "delivery_date": "2026-08-31"},
    ]
    for item in items:
        root = tmp_path / item["talkroom_id"]
        root.mkdir(parents=True)
        write_json(root / "state.json", {"talkroom_id": item["talkroom_id"]})
    write_json(tmp_path / "102/context/paid-priority.json", {
        "version": 1,
        "priority": 0,
        "authorized_by": "account_owner",
        "reason": "current_paid_closure_cursor",
    })

    admitted = paid._admitted_paid_projects(args, items)

    assert [item["talkroom_id"] for item in admitted] == ["102", "101"]


def test_queued_paid_project_keeps_parent_pending():
    paid = load("paid_direct")
    rows = {
        "101": {"status": "completed"},
        "102": {"status": "queued"},
    }

    assert paid._paid_pending_count(rows) == 1
    assert paid._paid_parent_status(failed=0, pending=1) == "pending"


def test_review_ready_undeterminable_ships_only_at_final_review_round():
    paid = load("paid_direct")

    assert paid._review_ready_may_ship("undeterminable", True, paid.MAX_FILE_REVIEW_ITERATIONS)
    assert not paid._review_ready_may_ship("undeterminable", False, paid.MAX_FILE_REVIEW_ITERATIONS)
    assert not paid._review_ready_may_ship("semantic_refusal", True, paid.MAX_FILE_REVIEW_ITERATIONS)
    assert not paid._review_ready_may_ship("needs_revision", True, paid.MAX_FILE_REVIEW_ITERATIONS)
    assert paid._shipment_basis_authorized("max_review_iterations_review_ready", "undeterminable")
    assert not paid._shipment_basis_authorized("max_review_iterations_review_ready", "needs_revision")


def test_paid_runner_contract_matches_runtime_terra_route():
    paid = load("paid_direct")
    runtime_config = json.loads(
        (SCRIPTS.parents[3] / "runtime" / "agent-runner" / "config.json").read_text()
    )
    escalation_route = runtime_config["task_classes"]["escalation-agent"]["candidates"]

    assert paid.PAID_DECISION_MODEL == "gpt-5.6-terra"
    assert paid.PAID_FILE_MODEL == "gpt-5.6-terra"
    assert ("codex", "gpt-5.6-terra") in paid.PAID_RUNNER_CANDIDATES
    assert {
        (candidate["provider"], candidate["model"])
        for candidate in escalation_route
    } <= paid.PAID_RUNNER_CANDIDATES


def test_normalize_acceptance_repairs_archive_member_bookkeeping(tmp_path):
    paid = load("paid_direct")
    root = tmp_path / "project"
    (root / "delivery").mkdir(parents=True)
    (root / "acceptance").mkdir()
    (root / "context").mkdir()
    write_json(root / "context" / "paid-work-decision.json", {"required_assets": []})
    member = b"member audio"
    archive = root / "delivery" / "draft-v1.zip"
    import zipfile
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("draft-v1.wav", member)
    acceptance = root / "acceptance" / "acceptance-v1.json"
    write_json(acceptance, {"status": "REVIEW_READY", "acceptance_delta": ["review"]})
    write_json(root / "delivery" / "paid-work-result.json", {
        "status": "REVIEW_READY",
        "artifact_path": str(archive),
        "acceptance_evidence_path": str(acceptance),
        "acceptance_status": "REVIEW_READY",
        "acceptance_delta": ["review"],
        "required_assets": [],
        "artifact_assets": [{
            "asset_id": "draft", "path": str(archive), "archive_member": "draft-v1.wav",
            "bytes": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "mime_type": "application/zip", "provenance": {"kind": "builder"},
        }],
    })

    paid._normalize_acceptance_delta(root)
    asset = json.loads((root / "delivery" / "paid-work-result.json").read_text())["artifact_assets"][0]

    assert asset["bytes"] == len(member)
    assert asset["sha256"] == hashlib.sha256(member).hexdigest()
    assert asset["mime_type"] in {"audio/wav", "audio/x-wav"}
    assert isinstance(asset["provenance"], str)


def test_run_bounded_does_not_wait_for_grandchild_inherited_pipe():
    paid = load("paid_direct")
    started = time.monotonic()

    result = paid._run_bounded([
        sys.executable, "-c",
        "import subprocess; subprocess.Popen(['sleep', '1.5']); print('done')",
    ], timeout=1)

    assert result.returncode == 0
    assert result.stdout.strip() == "done"
    assert time.monotonic() - started < 1


def test_runner_loop_id_uses_managed_control_plane_identity(monkeypatch):
    paid = load("paid_direct")
    monkeypatch.setenv("LIFE_MANAGER_LOOP_ID", "hf-gig-paid-direct")
    assert paid._runner_loop_id() == "hf-gig-paid-direct"


def test_remote_verifier_accepts_its_single_evidence_reference():
    paid = load("paid_direct")
    result = {"verifier_evidence": "remote-verifier-evidence.json"}

    references = getattr(paid, "_verifier_evidence_references", lambda _result: [])(result)

    assert references == [("verifier_evidence", "remote-verifier-evidence.json")]


def test_remote_owner_cannot_treat_one_invalid_candidate_as_exhaustion():
    source = (SCRIPTS / "paid_direct.py").read_text(encoding="utf-8")
    assert "One invalid, private, unreachable, or unverified candidate is not batch exhaustion" in source
    assert "Do not finalize a partial batch after a command timeout or interruption" in source
    assert "A classification revision preserves the prior effect's semantic_contract_sha256" in source


def test_normalize_acceptance_absolutizes_project_relative_asset_path(tmp_path):
    paid = load("paid_direct")
    root = tmp_path / "project"
    (root / "delivery").mkdir(parents=True)
    (root / "acceptance").mkdir(); (root / "context").mkdir()
    archive = root / "delivery" / "draft-v1.zip"; archive.write_bytes(b"zip")
    acceptance = root / "acceptance" / "acceptance-v1.json"
    write_json(acceptance, {"status": "PASS", "acceptance_delta": ["ready"]})
    assets = [{"asset_id": "draft", "kind": "linked_asset", "minimum_count": 1,
               "buyer_visible_purpose": "download", "source_authority": "builder",
               "archive_required": True}]
    write_json(root / "context" / "paid-work-decision.json", {"required_assets": assets})
    write_json(root / "delivery" / "paid-work-result.json", {
        "status": "ok", "artifact_path": str(archive),
        "acceptance_evidence_path": str(acceptance), "acceptance_status": "PASS",
        "acceptance_delta": ["ready"], "required_assets": assets,
        "artifact_assets": [{"asset_id": "draft", "path": "delivery/draft-v1.zip",
            "bytes": 3, "sha256": hashlib.sha256(b"zip").hexdigest(),
            "mime_type": "application/zip", "provenance": "builder"}],
    })

    paid._normalize_acceptance_delta(root)
    asset = json.loads((root / "delivery" / "paid-work-result.json").read_text())["artifact_assets"][0]
    assert asset["path"] == str(archive.resolve())
