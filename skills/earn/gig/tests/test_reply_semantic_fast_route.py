"""Contract tests for the bounded, tool-less reply semantic route."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


GIG_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = GIG_ROOT.parents[2] / "runtime/agent-runner/agent_runner.py"
RUNNER_CONFIG = GIG_ROOT.parents[2] / "runtime/agent-runner/config.json"
REQUESTED_ESTIMATE_PATH = GIG_ROOT / "scripts" / "requested_estimate.py"
QUEUE_SNAPSHOT_PATH = GIG_ROOT / "scripts" / "coconala_queue_snapshot.py"
LAUNCHD_PATH = GIG_ROOT / "config" / "launchd-jobs.json"
REPLY_BROWSER_PATH = GIG_ROOT / "scripts" / "coconala_reply_browser.py"
sys.path.insert(0, str(RUNNER_PATH.parent))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_module("gig_agent_runner_reply_semantic_test", RUNNER_PATH)
requested_estimate = _load_module(
    "gig_requested_estimate_reply_semantic_test", REQUESTED_ESTIMATE_PATH,
)
queue_snapshot = _load_module(
    "gig_queue_snapshot_reply_semantic_test", QUEUE_SNAPSHOT_PATH,
)
reply_browser = _load_module(
    "gig_reply_browser_attachment_context_test", REPLY_BROWSER_PATH,
)


def test_paused_storefront_contract_does_not_invalidate_the_ledger(tmp_path):
    fields = {
        "service_id": "123", "public_url": "https://coconala.com/services/123",
        "title": "Service", "state": "受付休止中", "price_jpy": 5000,
        "category": "IT・プログラミング・開発/Webサイト制作",
        "public_content_sha256": hashlib.sha256("scope".encode()).hexdigest(),
    }
    row = {
        "version": 1, **fields, "scope_text": "scope",
        "service_version_sha256": hashlib.sha256(json.dumps(
            fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
    }
    path = tmp_path / "offer-contracts.jsonl"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    assert requested_estimate.load_service_contracts(path) == []
    assert requested_estimate.load_service_contracts(path, latest_only=False) == [row]


def test_reply_semantic_route_uses_bounded_luna_candidate():
    config = json.loads(RUNNER_CONFIG.read_text(encoding="utf-8"))
    composition = config["task_classes"]["composition-agent"]
    route = config["task_classes"]["reply-semantic-agent"]

    assert route["route"] == "luna-medium-reply-semantic"
    assert route["timeout_seconds"] == 240
    assert route["token_reservation"] <= composition["token_reservation"]
    assert route["candidates"][0] == {
        "provider": "codex", "model": "gpt-5.6-luna", "effort": "medium",
        "timeout_seconds": 120, "profile_alias": "acct2",
    }
    # 2026-09-04 1604f332cc "fix(runner): give every task class a working Claude fallback" added
    # a claude-direct candidate to reply-semantic-agent (and 13 other task classes) because a
    # codex quota outage was silently idling them -- for this lane specifically, that meant new
    # buyer messages went unclassified and unanswered. The bounded primary candidate above is
    # unchanged; only a same-pattern-as-composition-agent fallback was appended.
    assert route["candidates"][1] == {"provider": "claude-direct", "model": "claude-sonnet-5"}
    assert len(route["candidates"]) == 2
    assert "reply-semantic-agent" in runner.TOOLLESS_TASK_CLASSES


def _minimal_runner_config(candidates, *, total_timeout=120):
    candidates = [
        {**candidate, **({"profile_alias": "acct2"} if candidate["provider"] == "codex" else {})}
        for candidate in candidates
    ]
    return {
        "version": 1,
        "timeout_seconds": total_timeout,
        "providers": {
            "codex": {"executable": "codex", "capabilities": {}, "profiles": {"acct2": {
                "automation_home": "/tmp/test-codex-home", "auth_file": "/tmp/test-auth.json",
            }}},
            "claude-direct": {"executable": "claude", "capabilities": {}},
            "hermes": {"executable": "hermes", "capabilities": {}},
        },
        "task_classes": {
            "reply-semantic-agent": {
                "route": "test-reply-semantic",
                "token_reservation": 1,
                "timeout_seconds": total_timeout,
                "candidates": candidates,
            },
        },
    }


def _run_runner_with_config(monkeypatch, tmp_path, config, process):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps({
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"const": True}},
    }), encoding="utf-8")
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(config_path))
    monkeypatch.setattr(runner, "run_provider_process", process)
    monkeypatch.setattr(runner, "provider_process_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runner, "append_usage_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("bounded semantic prompt"))
    monkeypatch.setattr(sys, "argv", [
        str(RUNNER_PATH), "--task-class", "reply-semantic-agent",
        "--prompt-stdin", "--schema", str(schema_path),
        "--evidence-dir", str(tmp_path / "evidence"),
        "--task-label", "semantic-fallback", "--loop", "test-loop",
        "--workdir", str(tmp_path),
    ])
    return runner.run()


def test_real_runner_caps_the_single_explicit_profile_candidate(tmp_path, monkeypatch):
    candidates = [
        {"provider": "codex", "model": "gpt-5.6-luna", "effort": "medium", "timeout_seconds": 40},
    ]
    config = _minimal_runner_config(candidates)
    attempts = []
    clock = {"now": 0.0}
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock["now"])

    def provider_process(command, *, stdout, stderr, timeout, **_kwargs):
        attempts.append((command[0], timeout))
        result_path = Path(command[command.index("-o") + 1])
        result_path.write_text('{"ok": true}', encoding="utf-8")
        return 0

    result = _run_runner_with_config(monkeypatch, tmp_path, config, provider_process)

    assert result == 0
    assert [(Path(executable).name, timeout) for executable, timeout in attempts] == [("codex", 40)]
    summary = json.loads((tmp_path / "evidence" / "summary.json").read_text())
    assert summary["attempt_count"] == 1
    assert summary["selected_provider"] == "codex"
    rows = [json.loads(line) for line in (tmp_path / "evidence" / "attempts.jsonl").read_text().splitlines()]
    assert [row["error_class"] for row in rows] == [None]


@pytest.mark.parametrize("invalid_timeout", [0, -1, True, 40.5, "40", None])
def test_candidate_timeout_is_positive_integer_or_absent(tmp_path, monkeypatch, invalid_timeout):
    candidate = {"provider": "codex", "model": "gpt-5.6-luna", "effort": "medium"}
    candidate["timeout_seconds"] = invalid_timeout
    config = _minimal_runner_config([candidate])
    launched = []

    def should_not_launch(*_args, **_kwargs):
        launched.append(True)
        raise AssertionError("invalid candidate timeout launched a provider")

    result = _run_runner_with_config(monkeypatch, tmp_path, config, should_not_launch)

    assert result == 2
    assert launched == []


def test_candidate_timeout_can_be_omitted(tmp_path, monkeypatch):
    candidate = {"provider": "codex", "model": "gpt-5.6-luna", "effort": "medium"}
    config = _minimal_runner_config([candidate])

    def provider_process(command, *, stdout, timeout, **_kwargs):
        assert 1 <= timeout <= 120
        result_path = Path(command[command.index("-o") + 1])
        result_path.write_text('{"ok": true}', encoding="utf-8")
        return 0

    assert _run_runner_with_config(monkeypatch, tmp_path, config, provider_process) == 0


def test_runner_cli_accepts_reply_semantic_task_class():
    result = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "reply-semantic-agent" in result.stdout


def test_reply_semantic_task_is_tool_starved_for_codex_and_claude(tmp_path):
    args = argparse.Namespace(
        task_class="reply-semantic-agent",
        schema=tmp_path / "schema.json",
        workdir=tmp_path,
        image=[],
        read_only=False,
    )
    args.schema.write_text("{}", encoding="utf-8")

    for task_class in runner.TOOLLESS_TASK_CLASSES:
        args.task_class = task_class
        codex_command = runner.command_for(
            "codex", "codex", {},
            {"provider": "codex", "model": "gpt-5.6-luna", "effort": "medium"},
            args, "bounded prompt", {}, tmp_path / f"result-{task_class}.json", 60, None,
            prompt_via_stdin=True,
        )
        sandbox_index = codex_command.index("--sandbox")
        assert codex_command[sandbox_index:sandbox_index + 2] == ["--sandbox", "read-only"]
        for feature in ("shell_tool", "code_mode_host", "unified_exec"):
            disable_index = codex_command.index(feature)
            assert codex_command[disable_index - 1:disable_index + 1] == ["--disable", feature]

    args.task_class = "tool-agent"
    ordinary_command = runner.command_for(
        "codex", "codex", {},
        {"provider": "codex", "model": "gpt-5.6-luna", "effort": "medium"},
        args, "bounded prompt", {}, tmp_path / "result-tool.json", 60, None,
        prompt_via_stdin=True,
    )
    assert not any(
        ordinary_command[index:index + 2] == ["--disable", feature]
        for index in range(len(ordinary_command) - 1)
        for feature in ("shell_tool", "code_mode_host", "unified_exec")
    )

    args.task_class = "reply-semantic-agent"
    claude_command = runner.command_for(
        "claude-direct", "claude", {},
        {"provider": "claude-direct", "model": "sonnet"},
        args, "bounded prompt", {}, tmp_path / "result-claude.json", 60, None,
        prompt_via_stdin=True,
    )
    tools_index = claude_command.index("--tools")
    assert claude_command[tools_index:tools_index + 2] == ["--tools", ""]


def test_semantic_judge_uses_fast_task_class_and_bounded_outer_timeout(tmp_path, monkeypatch):
    schema = GIG_ROOT / "schemas" / "reply_semantic_judgement.schema.json"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        evidence = Path(argv[argv.index("--evidence-dir") + 1])
        result_path = evidence / "result.json"
        result_path.write_text("{}", encoding="utf-8")
        (evidence / "summary.json").write_text(json.dumps({
            "status": "success", "result_path": str(result_path),
        }), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(requested_estimate.subprocess, "run", fake_run)
    monkeypatch.setattr(
        requested_estimate,
        "validate_semantic_judgement",
        lambda _payload, _rows: {"next_action": "reply"},
    )
    judge = requested_estimate.SemanticJudge(
        runner=RUNNER_PATH,
        schema=schema,
        workdir=tmp_path,
        evidence_root=tmp_path / "evidence",
    )
    judge({
        "url": "https://coconala.com/messages/123",
        "title": "メッセージ詳細",
        "container_present": True,
        "own_user_path": "/users/12345",
        "messages": [
            {"message_id": "seller-1", "author_path": "/users/seller",
             "body": "こんにちは", "sent_at": "2026-08-19T00:00:00Z"},
            {"message_id": "buyer-1", "author_path": "/users/buyer",
             "body": "質問です", "sent_at": "2026-08-19T00:01:00Z"},
        ],
    }, "https://coconala.com/messages/123")

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[argv.index("--task-class") + 1] == "reply-semantic-agent"
    assert argv[argv.index("--timeout-seconds") + 1] == "120"
    assert kwargs["timeout"] == 150


def test_semantic_judge_corrects_any_model_validation_error_once(tmp_path, monkeypatch):
    schema = GIG_ROOT / "schemas" / "reply_semantic_judgement.schema.json"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs["input"])
        evidence = Path(argv[argv.index("--evidence-dir") + 1])
        result_path = evidence / "result.json"
        result_path.write_text("{}", encoding="utf-8")
        (evidence / "summary.json").write_text(json.dumps({
            "status": "success", "result_path": str(result_path),
        }), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    validations = iter((requested_estimate.SemanticJudgementError(
        "semantic_content_evidence_invalid"
    ), {"next_action": "wait"}))

    def validate(_payload, _rows):
        value = next(validations)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(requested_estimate.subprocess, "run", fake_run)
    monkeypatch.setattr(requested_estimate, "validate_semantic_judgement", validate)
    judge = requested_estimate.SemanticJudge(
        runner=RUNNER_PATH, schema=schema, workdir=tmp_path,
        evidence_root=tmp_path / "evidence",
    )
    receipt = judge({
        "url": "https://coconala.com/messages/123", "title": "メッセージ詳細",
        "container_present": True, "own_user_path": "/users/seller",
        "messages": [
            {"message_id": "seller-1", "author_path": "/users/seller",
             "body": "こんにちは", "sent_at": "2026-08-19T00:00:00Z"},
            {"message_id": "buyer-1", "author_path": "/users/buyer",
             "body": "質問です", "sent_at": "2026-08-19T00:01:00Z"},
        ],
    }, "https://coconala.com/messages/123")

    assert receipt["judgement"] == {"next_action": "wait"}
    assert len(calls) == 2
    assert "semantic_content_evidence_invalid" in calls[1]
    assert "一文字も変えずコピー" in calls[1]


def test_semantic_judge_accepts_compatible_receipts_and_rejects_unknown_profile(tmp_path):
    schema = GIG_ROOT / "schemas" / "reply_semantic_judgement.schema.json"
    judge = requested_estimate.SemanticJudge(
        runner=RUNNER_PATH,
        schema=schema,
        workdir=tmp_path,
        evidence_root=tmp_path / "evidence",
    )
    dom = {
        "url": "https://coconala.com/messages/123",
        "title": "メッセージ詳細",
        "container_present": True,
        "own_user_path": "/users/seller",
        "messages": [
            {"message_id": "seller-1", "author_path": "/users/seller",
             "body": "こんにちは", "sent_at": "2026-08-19T00:00:00Z"},
            {"message_id": "buyer-1", "author_path": "/users/buyer",
             "body": "質問です", "sent_at": "2026-08-19T00:01:00Z"},
        ],
    }
    current = {
        "version": requested_estimate.SEMANTIC_RECEIPT_VERSION,
        "prompt_version": requested_estimate.SEMANTIC_PROMPT_VERSION,
        "runner_profile": requested_estimate.SEMANTIC_RUNNER_PROFILE,
        "schema_sha256": judge.schema_sha256,
        "seller_facts_sha256": judge.seller_facts_sha256,
        "context_sha256": requested_estimate.semantic_context_sha256(
            requested_estimate.semantic_conversation(dom),
        ),
        "official_context_sha256": "b" * 64,
        "latest_message_identity": "buyer-1",
        "judgement": {
            "conversation_state": "question",
            "next_action": "reply",
            "cycle_start_message_id": "buyer-1",
            "evidence_message_ids": ["buyer-1"],
            "required_official_context": "none",
            "estimate_terms": None,
            "reply_body": "回答です",
            "reply_audit": {
                "answered_buyer_message_ids": ["buyer-1"],
                "unanswered_questions": [],
                "unsupported_claims": [],
                "unrequested_cta": False,
                "repeats_seller_message": False,
                "off_platform_contact": False,
            },
            "uncertainty": [],
        },
    }

    assert requested_estimate.SEMANTIC_RUNNER_PROFILE == "reply-semantic-agent"
    assert judge.receipt_current(current) is True
    legacy = {**current, "runner_profile": "composition-agent"}
    assert judge.receipt_current(legacy) is True
    assert requested_estimate.project_semantic_receipt(
        dom, "https://coconala.com/messages/123", legacy,
    )["semantic_reply_body"] == "回答です"
    assert judge.receipt_current({**current, "runner_profile": "unknown-agent"}) is False


def test_semantic_validation_rejects_deferring_an_inline_artifact_request():
    rows = [
        {
            "message_id": "buyer-preview",
            "role": "buyer",
            "sent_at": "2026-08-22T12:30:13Z",
            "body": "購入前に、X用とWeibo用の2つの投稿全文をここで見せてください。",
        },
    ]
    payload = {
        "conversation_state": "question",
        "next_action": "reply",
        "cycle_start_message_id": "buyer-preview",
        "evidence_message_ids": ["buyer-preview"],
        "required_official_context": "none",
        "estimate_terms": None,
        "reply_body": "対応可能です。X用・Weibo用それぞれの投稿前案をお見せします。",
        "reply_audit": {
            "answered_buyer_message_ids": ["buyer-preview"],
            "unanswered_questions": [],
            "unsupported_claims": [],
            "unrequested_cta": False,
            "repeats_seller_message": False,
            "off_platform_contact": False,
        },
        "uncertainty": [],
    }

    with pytest.raises(
        requested_estimate.SemanticJudgementError,
        match="semantic_inline_artifact_deferred",
    ):
        requested_estimate.validate_semantic_judgement(payload, rows)


def test_semantic_validation_allows_fulfilling_deferred_artifact_after_seller_last():
    rows = [
        {
            "message_id": "buyer-preview",
            "role": "buyer",
            "sent_at": "2026-08-22T12:30:13Z",
            "body": "購入前に、X用とWeibo用の2つの投稿全文をここで見せてください。",
        },
        {
            "message_id": "seller-promise",
            "role": "seller",
            "sent_at": "2026-08-22T12:31:47Z",
            "body": "対応可能です。X用・Weibo用の投稿前案をお見せします。",
        },
    ]
    payload = {
        "conversation_state": "question",
        "next_action": "reply",
        "cycle_start_message_id": "buyer-preview",
        "evidence_message_ids": ["buyer-preview"],
        "required_official_context": "none",
        "estimate_terms": None,
        "reply_body": "X用投稿案：救出活動への協力をお願いします。\n\nWeibo用投稿案：请帮助这些动物回到原来的国家。",
        "reply_audit": {
            "answered_buyer_message_ids": ["buyer-preview"],
            "unanswered_questions": [],
            "unsupported_claims": [],
            "unrequested_cta": False,
            "repeats_seller_message": False,
            "off_platform_contact": False,
        },
        "uncertainty": [],
    }

    assert requested_estimate.validate_semantic_judgement(payload, rows)[
        "reply_body"
    ].startswith("X用投稿案：")


def test_semantic_validation_rejects_deferring_the_same_artifact_again():
    rows = [
        {
            "message_id": "buyer-preview",
            "role": "buyer",
            "sent_at": "2026-08-22T12:30:13Z",
            "body": "購入前に、X用とWeibo用の2つの投稿全文をここで見せてください。",
        },
        {
            "message_id": "seller-promise",
            "role": "seller",
            "sent_at": "2026-08-22T12:31:47Z",
            "body": "対応可能です。X用・Weibo用の投稿前案をお見せします。",
        },
    ]
    payload = {
        "conversation_state": "question",
        "next_action": "reply",
        "cycle_start_message_id": "buyer-preview",
        "evidence_message_ids": ["buyer-preview"],
        "required_official_context": "none",
        "estimate_terms": None,
        "reply_body": "承知しました。投稿前案を改めて送ります。",
        "reply_audit": {
            "answered_buyer_message_ids": ["buyer-preview"],
            "unanswered_questions": [],
            "unsupported_claims": [],
            "unrequested_cta": False,
            "repeats_seller_message": False,
            "off_platform_contact": False,
        },
        "uncertainty": [],
    }

    with pytest.raises(
        requested_estimate.SemanticJudgementError,
        match="semantic_inline_artifact_deferred",
    ):
        requested_estimate.validate_semantic_judgement(payload, rows)


def test_inline_artifact_debt_cannot_authorize_a_second_seller_only_reply():
    rows = [
        {
            "message_id": "buyer-preview", "role": "buyer",
            "sent_at": "2026-08-22T12:30:13Z",
            "body": "購入前に投稿全文をここで見せてください。",
        },
        {
            "message_id": "seller-promise", "role": "seller",
            "sent_at": "2026-08-22T12:31:47Z",
            "body": "投稿前案をお見せします。",
        },
        {
            "message_id": "seller-repeat", "role": "seller",
            "sent_at": "2026-08-22T12:35:00Z",
            "body": "準備して改めて送ります。",
        },
    ]

    assert requested_estimate._inline_artifact_debt(rows) is False


def test_semantic_validation_allows_clarify_to_name_missing_buyer_input():
    rows = [{
        "message_id": "buyer-photo", "role": "buyer",
        "sent_at": "2026-08-22T14:24:22Z", "body": "写真を送りました。",
    }]
    payload = {
        "conversation_state": "clarify",
        "next_action": "clarify",
        "cycle_start_message_id": "buyer-photo",
        "evidence_message_ids": ["buyer-photo"],
        "required_official_context": "none",
        "estimate_terms": None,
        "reply_body": "ありがとうございます。サンプル対象のメンバー名を教えてください。",
        "reply_audit": {
            "answered_buyer_message_ids": ["buyer-photo"],
            "unanswered_questions": [],
            "unsupported_claims": [],
            "unrequested_cta": False,
            "repeats_seller_message": False,
            "off_platform_contact": False,
        },
        "uncertainty": ["サンプル対象のメンバー名が会話内にない"],
    }

    assert requested_estimate.validate_semantic_judgement(payload, rows)[
        "reply_body"
    ].startswith("ありがとうございます")


def test_purchase_decision_request_requires_proactive_reply_before_estimate():
    rows = [{
        "message_id": "buyer-go", "role": "buyer",
        "sent_at": "2026-08-22T14:55:00Z",
        "body": "このアプリがいけると判断した場合は『いけます！』と答えてください。購入処理に進みます。",
    }]
    payload = {
        "conversation_state": "ready_to_buy", "next_action": "send_estimate",
        "cycle_start_message_id": "buyer-go", "evidence_message_ids": ["buyer-go"],
        "required_official_context": "none", "estimate_terms": None,
        "reply_body": None,
        "reply_audit": {
            "answered_buyer_message_ids": [], "unanswered_questions": [],
            "unsupported_claims": [], "unrequested_cta": False,
            "repeats_seller_message": False, "off_platform_contact": False,
        },
        "uncertainty": [],
    }

    with pytest.raises(
        requested_estimate.SemanticJudgementError,
        match="semantic_purchase_decision_requires_proactive_reply",
    ):
        requested_estimate.validate_semantic_judgement(payload, rows)


def test_purchase_decision_reply_cannot_lead_with_internal_confirmation():
    rows = [{
        "message_id": "buyer-go", "role": "buyer",
        "sent_at": "2026-08-22T14:55:00Z",
        "body": "いける場合は『いけます！』と答えてください。購入処理に進みます。",
    }]
    payload = {
        "conversation_state": "question", "next_action": "reply",
        "cycle_start_message_id": "buyer-go", "evidence_message_ids": ["buyer-go"],
        "required_official_context": "none", "estimate_terms": None,
        "reply_body": "承知しました。URLを確認して判断します。",
        "reply_audit": {
            "answered_buyer_message_ids": ["buyer-go"], "unanswered_questions": [],
            "unsupported_claims": [], "unrequested_cta": False,
            "repeats_seller_message": False, "off_platform_contact": False,
        },
        "uncertainty": [],
    }

    with pytest.raises(
        requested_estimate.SemanticJudgementError,
        match="semantic_purchase_decision_requires_proactive_reply",
    ):
        requested_estimate.validate_semantic_judgement(payload, rows)


def test_system_notice_with_purchase_words_does_not_force_customer_reply():
    rows = [{
        "message_id": "system-notice", "role": "buyer",
        "sent_at": "2026-09-08T00:00:00Z",
        "body": "購入者へ対応できない場合は運営までご連絡ください。",
    }]
    payload = {
        "conversation_state": "unknown", "next_action": "wait",
        "cycle_start_message_id": "system-notice", "evidence_message_ids": [],
        "required_official_context": "none", "estimate_terms": None,
        "reply_body": None,
        "reply_audit": {
            "answered_buyer_message_ids": [], "unanswered_questions": [],
            "unsupported_claims": [], "unrequested_cta": False,
            "repeats_seller_message": False, "off_platform_contact": False,
        },
        "uncertainty": ["運営からの送信専用通知"],
    }

    assert requested_estimate.validate_semantic_judgement(payload, rows)[
        "conversation_state"
    ] == "unknown"


def test_acknowledged_existing_purchase_cannot_generate_another_estimate():
    rows = [
        {
            "message_id": "buyer-purchased", "role": "buyer",
            "sent_at": "2026-08-21T01:15:00Z", "body": "すでに購入していますが…",
        },
        {
            "message_id": "seller-ack", "role": "seller",
            "sent_at": "2026-08-21T01:31:18Z",
            "body": "すでにご購入済みであることを確認しました。合意済みの内容に沿って進行いたします。",
        },
    ]
    payload = {
        "conversation_state": "ready_to_buy", "next_action": "send_estimate",
        "cycle_start_message_id": "buyer-purchased",
        "evidence_message_ids": ["buyer-purchased"],
        "required_official_context": "estimate_form", "estimate_terms": {
            "title": "作業", "content": "合意済み作業を実施します。", "quantity": 1,
            "price_jpy": 9000, "delivery_days": 4, "purchase_plan": "single",
            "title_evidence_message_ids": ["buyer-purchased"],
            "content_evidence_message_ids": ["buyer-purchased"],
            "quantity_evidence_message_ids": ["buyer-purchased"],
            "price_evidence_message_ids": ["buyer-purchased"],
            "delivery_evidence_message_ids": ["buyer-purchased"],
            "purchase_plan_evidence_message_ids": ["buyer-purchased"],
        },
        "reply_body": None,
        "reply_audit": {
            "answered_buyer_message_ids": [], "unanswered_questions": [],
            "unsupported_claims": [], "unrequested_cta": False,
            "repeats_seller_message": False, "off_platform_contact": False,
        },
        "uncertainty": [],
    }

    with pytest.raises(
        requested_estimate.SemanticJudgementError,
        match="semantic_existing_purchase_estimate_conflict",
    ):
        requested_estimate.validate_semantic_judgement(payload, rows)


def test_verified_dm_attachments_enter_semantic_context_without_local_path():
    dom = {
        "own_user_path": "/users/seller",
        "messages": [{
            "message_id": "buyer-files", "author_path": "/users/buyer",
            "sent_at": "2026-08-22T14:37:00Z", "body": "こちらで大丈夫でしょうか？",
            "verified_attachments": [{
                "filename": "1880.png", "content_type": "image/png",
                "size_bytes": 632406, "sha256": "a" * 64,
            }],
        }],
    }

    rows = requested_estimate.semantic_conversation(dom)

    assert rows[0]["verified_attachments"] == [{
        "filename": "1880.png", "content_type": "image/png",
        "size_bytes": 632406, "sha256": "a" * 64,
    }]
    assert "path" not in json.dumps(rows, ensure_ascii=False)


def test_merge_verified_dm_attachments_requires_hash_and_bytes():
    dom = {"messages": [{"message_id": "m1", "author_path": "/users/buyer", "body": "添付です"}]}
    document = {
        "messages": [{
            "message_id": "m1", "side": "buyer",
            "attachments": [{"url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png"}],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png",
            "bytes": 632406, "sha256": "b" * 64, "content_type": "image/png",
        }],
    }

    queue_snapshot.merge_verified_dm_attachments(dom, document)

    assert dom["messages"][0]["verified_attachments"][0]["sha256"] == "b" * 64


def test_merge_verified_dm_attachments_accepts_new_seller_reply_after_send():
    dom = {"messages": [
        {"message_id": "buyer-1", "author_path": "/users/buyer", "body": "添付です"},
        {"message_id": "seller-1", "author_path": "/users/seller", "body": "確認しました"},
    ]}
    document = {
        "messages": [{
            "message_id": "buyer-1", "side": "buyer",
            "attachments": [{
                "url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png",
            }],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png",
            "bytes": 632406, "sha256": "d" * 64, "content_type": "image/png",
        }],
    }

    queue_snapshot.merge_verified_dm_attachments(dom, document)

    assert dom["messages"][0]["verified_attachments"][0]["sha256"] == "d" * 64
    assert "verified_attachments" not in dom["messages"][1]


def test_merge_verified_dm_attachments_fails_closed_on_download_error():
    dom = {"messages": [{"message_id": "m1", "author_path": "/users/buyer", "body": "添付です"}]}
    document = {
        "messages": [{
            "message_id": "m1", "side": "buyer",
            "attachments": [{"url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png"}],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png",
            "error": "not_fetched",
        }],
    }

    with pytest.raises(queue_snapshot.CollectorUnhealthy, match="dm_attachment_unverified"):
        queue_snapshot.merge_verified_dm_attachments(dom, document)


def test_merge_verified_dm_attachments_uses_exact_index_and_body_when_ids_are_absent():
    dom = {"messages": [{"message_id": None, "author_path": "/users/buyer", "body": "こちらで大丈夫ですか？"}]}
    document = {
        "messages": [{
            "message_id": None, "side": "buyer", "text": "こちらで大丈夫ですか？",
            "attachments": [{"url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png"}],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "1880.png",
            "bytes": 632406, "sha256": "c" * 64, "content_type": "image/png",
        }],
    }

    queue_snapshot.merge_verified_dm_attachments(dom, document)

    assert dom["messages"][0]["verified_attachments"][0]["sha256"] == "c" * 64


def test_merge_verified_dm_attachments_rebinds_unique_exact_body_after_reorder():
    dom = {"own_user_path": "/users/seller", "messages": [
        {"message_id": None, "author_path": "/users/buyer", "body": "newer"},
        {"message_id": None, "author_path": "/users/buyer", "body": "添付です"},
    ]}
    document = {
        "messages": [{
            "message_id": None, "side": "buyer", "text": "添付です",
            "attachments": [{"url": "https://coconala.com/uploaded_files/view/1"}],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "file.xlsx",
            "bytes": 100, "sha256": "f" * 64,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }],
    }

    queue_snapshot.merge_verified_dm_attachments(dom, document)

    assert "verified_attachments" not in dom["messages"][0]
    assert dom["messages"][1]["verified_attachments"][0]["sha256"] == "f" * 64


def test_merge_verified_dm_attachments_rejects_ambiguous_exact_body():
    dom = {"own_user_path": "/users/seller", "messages": [
        {"message_id": None, "author_path": "/users/buyer", "body": "添付です"},
        {"message_id": None, "author_path": "/users/buyer", "body": "添付です"},
    ]}
    document = {
        "messages": [{
            "message_id": None, "side": "buyer", "text": "添付です",
            "attachments": [{"url": "https://coconala.com/uploaded_files/view/1"}],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "file.xlsx",
            "bytes": 100, "sha256": "f" * 64, "content_type": "application/octet-stream",
        }],
    }

    with pytest.raises(queue_snapshot.CollectorUnhealthy, match="dm_attachment_message_identity_changed"):
        queue_snapshot.merge_verified_dm_attachments(dom, document)


def test_merge_verified_dm_attachments_restores_verified_message_older_than_dom_window():
    dom = {"own_user_path": "/users/seller", "messages": [
        {"message_id": "new-buyer", "author_path": "/users/buyer",
         "sent_at": "2026-09-01 09:00:00", "body": "確認をお願いします"},
        {"message_id": "new-seller", "author_path": "/users/seller",
         "sent_at": "2026-09-01 09:05:00", "body": "確認します"},
    ]}
    document = {
        "messages": [{
            "message_id": None, "side": "buyer", "sent_at": "2026-08-31 13:58:41",
            "text": "資料をお送りします",
            "attachments": [{"url": "https://coconala.com/uploaded_files/view/1"}],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "budget.xlsx",
            "bytes": 100, "sha256": "e" * 64,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }],
    }

    queue_snapshot.merge_verified_dm_attachments(dom, document)

    restored = dom["messages"][0]
    assert restored["author_path"] == "/users/buyer"
    assert restored["sent_at"] == "2026-08-31 13:58:41"
    assert restored["body"] == "資料をお送りします"
    assert restored["verified_attachments"][0]["sha256"] == "e" * 64


def test_merge_verified_dm_attachments_rejects_missing_message_inside_dom_window():
    dom = {"own_user_path": "/users/seller", "messages": [
        {"message_id": "older", "author_path": "/users/buyer",
         "sent_at": "2026-08-31 09:00:00", "body": "older"},
    ]}
    document = {
        "messages": [{
            "message_id": None, "side": "buyer", "sent_at": "2026-09-01 09:00:00",
            "text": "missing",
            "attachments": [{"url": "https://coconala.com/uploaded_files/view/1"}],
        }],
        "attachment_index": [{
            "url": "https://coconala.com/uploaded_files/view/1", "filename": "file.xlsx",
            "bytes": 100, "sha256": "f" * 64, "content_type": "application/octet-stream",
        }],
    }

    with pytest.raises(queue_snapshot.CollectorUnhealthy, match="dm_attachment_message_identity_changed"):
        queue_snapshot.merge_verified_dm_attachments(dom, document)


def test_merge_or_refresh_durable_dm_attachments_refreshes_only_missing_manifest(monkeypatch):
    calls = []

    def missing(_dom, _thread_id):
        raise queue_snapshot.CollectorUnhealthy("dm_attachment_evidence_invalid")

    monkeypatch.setattr(queue_snapshot, "merge_durable_dm_attachments", missing)
    monkeypatch.setattr(
        queue_snapshot, "enrich_verified_dm_attachments",
        lambda dom, **kwargs: calls.append((dom, kwargs)),
    )
    dom = {"messages": []}

    queue_snapshot.merge_or_refresh_durable_dm_attachments(
        dom, helper=Path("helper"), thread_id="123", observed_at="2026-09-08T00:00:00Z",
    )

    assert calls == [(dom, {
        "helper": Path("helper"), "thread_id": "123", "observed_at": "2026-09-08T00:00:00Z",
    })]


def test_merge_or_refresh_durable_dm_attachments_preserves_other_failures(monkeypatch):
    def ambiguous(_dom, _thread_id):
        raise queue_snapshot.CollectorUnhealthy("dm_attachment_message_identity_changed")

    monkeypatch.setattr(queue_snapshot, "merge_durable_dm_attachments", ambiguous)

    with pytest.raises(queue_snapshot.CollectorUnhealthy, match="dm_attachment_message_identity_changed"):
        queue_snapshot.merge_or_refresh_durable_dm_attachments(
            {"messages": []}, helper=Path("helper"), thread_id="123",
            observed_at="2026-09-08T00:00:00Z",
        )


def test_verified_attachment_denial_debt_allows_one_correction():
    rows = [
        {
            "message_id": "buyer-files", "role": "buyer",
            "sent_at": "2026-08-22T15:03:45Z", "body": "PNGを添付しました。確認できますか？",
            "verified_attachments": [{
                "filename": "1880.png", "content_type": "image/png",
                "size_bytes": 632406, "sha256": "d" * 64,
            }],
        },
        {
            "message_id": "seller-denial", "role": "seller",
            "sent_at": "2026-08-22T15:10:53Z", "body": "PNGファイル本体を確認できない状態です。再添付してください。",
        },
    ]
    payload = {
        "conversation_state": "question", "next_action": "reply",
        "cycle_start_message_id": "buyer-files", "evidence_message_ids": ["buyer-files"],
        "required_official_context": "none", "estimate_terms": None,
        "reply_body": "確認できました。先ほどの案内は誤りです。1880.pngを受領済みです。再添付は不要です。この素材で進めます。",
        "reply_audit": {
            "answered_buyer_message_ids": ["buyer-files"], "unanswered_questions": [],
            "unsupported_claims": [], "unrequested_cta": False,
            "repeats_seller_message": False, "off_platform_contact": False,
        },
        "uncertainty": [],
    }

    assert requested_estimate.validate_semantic_judgement(payload, rows)[
        "reply_body"
    ].startswith("確認できました")


def test_reply_browser_context_preserves_verified_attachment_hash():
    dom = {
        "url": "https://coconala.com/mypage/direct_message/123",
        "title": "メッセージ詳細 | マイページ | ココナラ", "container_present": True,
        "own_user_path": "/users/12345",
        "messages": [{
            "message_id": "m1", "author_path": "/users/67890",
            "sent_at": "2026-08-22 23:37:59", "body": "こちらで大丈夫でしょうか？",
            "verified_attachments": [{
                "filename": "1880.png", "content_type": "image/png",
                "size_bytes": 632406, "sha256": "e" * 64,
            }],
        }],
    }

    context, _bounded = reply_browser.thread_state(
        dom, "https://coconala.com/mypage/direct_message/123",
    )

    assert context["conversation"][0]["verified_attachments"][0]["sha256"] == "e" * 64


def test_verified_attachment_correction_does_not_create_second_debt():
    rows = [
        {
            "message_id": "buyer-files", "role": "buyer", "sent_at": "2026-08-22T15:03:45Z",
            "body": "PNGを添付しました。", "verified_attachments": [{
                "filename": "1880.png", "content_type": "image/png",
                "size_bytes": 632406, "sha256": "d" * 64,
            }],
        },
        {"message_id": "seller-denial", "role": "seller", "sent_at": "2026-08-22T15:10:53Z", "body": "確認できません。"},
        {"message_id": "seller-correct", "role": "seller", "sent_at": "2026-08-22T15:15:00Z", "body": "確認できました。再添付は不要です。"},
    ]

    assert requested_estimate._verified_attachment_denial_debt(rows) is False


def test_semantic_judge_uses_one_bounded_runner_attempt(tmp_path, monkeypatch):
    schema = GIG_ROOT / "schemas" / "reply_semantic_judgement.schema.json"
    calls = []

    def failed_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1, "", "failed")

    monkeypatch.setattr(requested_estimate.subprocess, "run", failed_run)
    judge = requested_estimate.SemanticJudge(
        runner=RUNNER_PATH,
        schema=schema,
        workdir=tmp_path,
        evidence_root=tmp_path / "evidence",
    )

    with pytest.raises(requested_estimate.SemanticJudgementError, match="runner_failed"):
        judge({
            "url": "https://coconala.com/messages/123",
            "title": "メッセージ詳細",
            "container_present": True,
            "own_user_path": "/users/seller",
            "messages": [
                {"message_id": "seller-1", "author_path": "/users/seller",
                 "body": "こんにちは", "sent_at": "2026-08-19T00:00:00Z"},
                {"message_id": "buyer-1", "author_path": "/users/buyer",
                 "body": "質問です", "sent_at": "2026-08-19T00:01:00Z"},
            ],
        }, "https://coconala.com/messages/123")

    assert len(calls) == 1
    assert calls[0][1]["timeout"] == judge.timeout_seconds + 30


def test_direct_inbox_parser_defaults_semantic_timeout_to_240(tmp_path):
    args = queue_snapshot.argument_parser().parse_args([
        "--output", str(tmp_path / "out.json"),
        "--evidence-dir", str(tmp_path / "evidence"),
        "--mode", "direct-inbox-only",
    ])

    assert args.semantic_timeout_seconds == 240


def test_negotiate_runs_every_30_seconds_without_changing_other_job_intervals():
    jobs = json.loads(LAUNCHD_PATH.read_text(encoding="utf-8"))["jobs"]
    by_lane = {job["lane"]: job for job in jobs}

    assert by_lane["negotiate"]["KeepAlive"] is True
    assert "StartInterval" not in by_lane["negotiate"]
    negotiate_program = by_lane["negotiate"]["program"]
    assert negotiate_program[-5:] == ["--continuous", "--poll-seconds", "30", "--workers", "2"]
    assert by_lane["negotiate"]["ThrottleInterval"] == 30
    assert (by_lane["apply"]["StartInterval"], by_lane["apply"]["ThrottleInterval"]) == (60, 60)
    assert (by_lane["storefront"]["StartInterval"], by_lane["storefront"]["ThrottleInterval"]) == (60, 60)
    assert by_lane["browser"].get("StartInterval") is None
    assert by_lane["browser"]["ThrottleInterval"] == 30
    # `paid` (2026-09-07 2ecb2c8c9) and the old `release` watcher (2026-08-28 93d6720ee, retired
    # outright) are no longer jobs in this legacy manifest at all -- see
    # test_manifest_wraps_only_four_business_lanes for the paid migration and
    # test_release_watch_is_retired_in_favor_of_the_shared_reconciler in test_gig_release.py for
    # the watcher retirement. `paid` is still on the same 300-second cadence, just rendered by the
    # shared loop-registry pipeline (runtime/loop/lm_loop_apply.py) instead of gig_release.py; that
    # renderer only sets ThrottleInterval for sub-10-second cadences, so a 300-second StartInterval
    # alone is the real, current contract for this lane.
    assert "paid" not in by_lane
    assert "release" not in by_lane
    registry = json.loads(
        (GIG_ROOT.parents[2] / "config" / "loop-registry.json").read_text(encoding="utf-8")
    )
    assert registry["loops"]["hf-gig-paid-direct"]["cadence"] == {"start_interval_seconds": 300}


def test_semantic_prompt_v28_is_proactive_and_stops_blackwave():
    prompt = requested_estimate.semantic_prompt(
        [{"message_id": "buyer-1", "role": "buyer", "sent_at": "2026-08-19T00:00:00Z", "body": "質問です"}],
        official_context=None,
        seller_facts=[],
        thread_id="10131237",
    )

    assert requested_estimate.SEMANTIC_PROMPT_VERSION == "reply-negotiate-v28"
    assert requested_estimate.semantic_prompt_compatible(
        {"prompt_version": "reply-negotiate-v26"}
    ) is True
    assert requested_estimate.semantic_prompt_compatible(
        {"prompt_version": "reply-negotiate-v27"}
    ) is True
    assert requested_estimate.semantic_prompt_compatible(
        {"prompt_version": "reply-negotiate-v25"}
    ) is False
    assert "条件付き購入意思は購入承認ではありません" in prompt
    assert "すでに購入済み" in prompt
    assert "新しい見積りを送らない" in prompt
    assert "判断を本文の先頭で明言" in prompt
    assert "確認します／確認してお伝えします" in prompt
    assert "verified_attachments" in prompt
    assert "再送や文字起こしをbuyerへ要求" in prompt
    assert "確認不能と誤案内" in prompt
    assert '"conversation_thread_id":"10131237"' in prompt
    assert "Studio BlackWave" in prompt
    assert "next_action=stop" in prompt
    assert "reply・send_estimate・clarifyを一切返しません" in prompt
    assert "clarifyでは、こちらが確認する不足情報をuncertaintyにだけ列挙" in prompt
    assert "unanswered_questionsは空配列" in prompt
    assert "saas_lp_cvr_3_to_10_20260819" in requested_estimate.SELLER_FACT_IDS
    assert "公式応募" in prompt
    assert "current capability commitment" in prompt
    assert "required_official_context=application" in prompt
    assert "対応可能です" in prompt
    assert "対応可能とはお約束できません" not in prompt
    assert (
        "required_official_context=applicationは、特定の応募proposalのexact価格・納期・本文を参照しなければ答えられない時だけです。"
        "一般的な経験・能力・稼働可否の質問には使いません。"
    ) not in prompt
    assert "current buyerのcapability・対応scope・sampleを特定の応募applicationの明示scopeと照合" in prompt
    assert "application contextと無関係な一般的な経験・能力・稼働可否の質問だけでは使いません" in prompt
    assert "この会話で確認できる事実としては断言できません" not in prompt
    assert "未確認historyの不在や経験不足を自発的に説明したり、対応不可を先頭に置いたりしません" in prompt
    assert "selection sample/roughのexplicit buyer deadlineはinterim deadline" in prompt
    assert "later official final delivery dateとは別で、applied scope内ならclarifyせず受諾" in prompt
    assert "動画編集、字幕・テロップ挿入、映像加工、完成動画書き出しは対応不能です。" not in prompt
    assert "Care Earth Mart" in prompt
    assert "選定用ラフ" in prompt
    assert "後で見せます／お送りします" in prompt
    assert "実物全文" in prompt
    assert "その約束は未履行" in prompt
    assert "SaaS/Wix LP" in prompt
    assert "3%" in prompt and "10%" in prompt
    assert "CTA" in prompt and "first-view" in prompt
    assert "CPA" in prompt
    assert "違法・危険・プラットフォーム禁止" in prompt


def test_verified_seller_facts_only_return_allowlisted_saas_fact(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "facts": [
            {
                "id": "saas_lp_cvr_3_to_10_20260819",
                "claim": "visitor-to-service-start conversion was approximately 3% to 10%",
                "evidence": "verified SaaS/Wix LP application context",
            },
            {
                "id": "unknown_unverified_fact",
                "claim": "invented customer result",
                "evidence": "untrusted note",
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")

    facts = requested_estimate.verified_seller_facts(profile)

    assert facts == [{
        "id": "saas_lp_cvr_3_to_10_20260819",
        "claim": "visitor-to-service-start conversion was approximately 3% to 10%",
        "evidence": "verified SaaS/Wix LP application context",
    }]
    prompt = requested_estimate.semantic_prompt([], seller_facts=facts)
    assert "approximately 3% to 10%" in prompt
    assert "invented customer result" not in prompt
