"""H3 + C1 + C3: the repair investigation checkpoints instead of dying at its budget.

The live 2026-08-07 receipt for the `note/ja` incident recorded
`model.status TIMEOUT`, `model.latency_ms 120004` against `timeout_seconds 120`
and `return_code null`, so the investigation ended at `cause_status UNDETERMINED`
and nothing downstream could act.  These tests reproduce that truncation and
then require the three properties SSOT §9.3.1 (H3) and §9.4 (C1, C3) name:

1. C1 — the outcome is read from the Codex JSONL event stream, not the exit
   code, and a failed run names the event that produced the verdict.
2. H3 — a slice that exceeds its budget persists partial findings plus the
   Codex session identity and stays continuable, and a later tick resumes that
   same session instead of starting a second one.
3. C3/§9.3.1 — attempts per incident are bounded, and on exhaustion the loop
   degrades to the safest known state and stops spending model time.

The event shapes asserted here were measured against the installed
`codex-cli 0.145.0` on 2026-08-07, not taken from documentation:

* success  -> `thread.started`, `turn.started`, `item.completed`, `turn.completed`
* failure  -> top-level `{"type":"error"}` then `{"type":"turn.failed"}`, exit 1
* an `item.completed` whose item `type` is `error` is ADVISORY and was observed
  in a run that exited 0 with a completed turn, so it must never be read as a
  failure
* a run killed at its budget leaves `thread.started` (and therefore the session
  id) on disk with no terminal turn event
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "scripts" / "writer_repair_dispatch.py"
from writer_repair_fixture import write_repair_run_gates

NOTE_422 = (
    'NoteNativePublishError: Note native publish HTTP 422: '
    '{"error":{"code":"invalid","message":"本文に利用できない内容が含まれています。"}}'
)

SESSION_ID = "019fdb07-c478-7d10-be9e-2ebe323b223a"


# ---------------------------------------------------------------------------
# a model runner stub that speaks the measured codex session contract
# ---------------------------------------------------------------------------

STUB = r'''#!/usr/bin/env python3
"""Stand-in for runtime/model-runner.sh in session mode.

Honours the same environment contract the real runner uses to turn on
`--json`, `-o`, `--output-schema`, session persistence and `resume`, and
reproduces the event shapes measured from codex-cli 0.145.0.
"""
import json, os, sys, time
from pathlib import Path

CODEX_MODEL="gpt-5.6-terra"  # mirrors the runner terra binding

calls = Path(os.environ["STUB_CALLS"])
scenario_path = Path(os.environ["STUB_SCENARIO"])
scenario = json.loads(scenario_path.read_text())

events_file = os.environ.get("ARTICLE_CODEX_EVENTS_FILE")
last_message_file = os.environ.get("ARTICLE_CODEX_LAST_MESSAGE_FILE")
schema_file = os.environ.get("ARTICLE_CODEX_OUTPUT_SCHEMA")
resume_id = os.environ.get("ARTICLE_CODEX_RESUME_SESSION_ID")

with calls.open("a") as handle:
    handle.write(json.dumps({
        "argv": sys.argv[1:],
        "resume_session_id": resume_id,
        "events_file": events_file,
        "last_message_file": last_message_file,
        "output_schema": schema_file,
        "role": os.environ.get("ARTICLE_MODEL_ROLE"),
        "provider": os.environ.get("ARTICLE_PROVIDER"),
        "sol": os.environ.get("ARTICLE_SOL_TRIGGER_RECEIPT"),
    }) + "\n")

# The runner must always give the caller a stream path in session mode.
if not events_file:
    sys.stderr.write("stub: no ARTICLE_CODEX_EVENTS_FILE\n")
    sys.exit(64)

# A resume continues the recorded session; a cold start opens a new one.
session_id = resume_id or scenario.get("session_id", "SESSION-COLD")
stream = open(events_file, "w")


def emit(value):
    stream.write(json.dumps(value) + "\n")
    stream.flush()


emit({"type": "thread.started", "thread_id": session_id})
emit({"type": "turn.started"})
# Measured: advisory item-level errors occur inside runs that exit 0.
emit({"type": "item.completed", "item": {
    "id": "item_0", "type": "error",
    "message": "Skill descriptions were shortened to fit the 2% skills context budget.",
}})

mode = scenario["mode"]

if mode == "hang":
    # Exceeds the caller's budget and never terminates its turn, exactly like
    # the live 120s TIMEOUT. The caller must kill it and keep what landed.
    emit({"type": "item.completed", "item": {
        "id": "item_1", "type": "agent_message",
        "text": scenario.get("partial", "partial finding: note rejects the embedded HTML block"),
    }})
    time.sleep(600)
    sys.exit(0)

if mode == "failed":
    emit({"type": "error", "message": "upstream refused the request"})
    emit({"type": "turn.failed", "error": {"message": "upstream refused the request"}})
    stream.close()
    sys.exit(1)

verdict = scenario.get("verdict", {
    "cause_status": "EVIDENCE_BACKED_HYPOTHESIS",
    "evidence_gaps": [],
    "findings": [],
    "primary_sources": [],
    "complete": True,
    "remaining_work": "",
})
emit({"type": "item.completed", "item": {
    "id": "item_1", "type": "agent_message", "text": json.dumps(verdict),
}})
emit({"type": "turn.completed", "usage": {
    "input_tokens": 22977, "cached_input_tokens": 0,
    "cache_write_input_tokens": 0, "output_tokens": 20, "reasoning_output_tokens": 0,
}})
stream.close()
if last_message_file:
    Path(last_message_file).write_text(json.dumps(verdict))
sys.exit(0)
'''


def _stub_runner(tmp_path: Path, scenario: dict) -> tuple[Path, Path, Path]:
    runner = tmp_path / "runtime" / "model-runner.sh"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(STUB)
    runner.chmod(0o755)
    calls = tmp_path / "model-calls.jsonl"
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    return runner, calls, scenario_path


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "self-heal").mkdir(parents=True)
    run_gates = state / "runs" / "daily-2026-08-07" / "gates"
    write_repair_run_gates(run_gates, NOTE_422)
    return state


def _incident(fingerprint: str = "b" * 64) -> dict:
    """The live Order 3 incident: note JA rejected with HTTP 422, blocking revenue."""
    return {
        "fingerprint": fingerprint,
        "phase": "destination:note/ja",
        "reason": NOTE_422,
        "classification": "publisher-content-rejection",
        "failure_class": "publisher-content-rejection",
        "error_signature": NOTE_422,
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__note__ja",
        "destination": "note/ja",
        "revenue_role": "revenue-set",
        "blocking": True,
        "state": "OPEN",
        "first_seen_at": "2026-08-07T05:27:19Z",
        "attempt_count": 0,
        "occurrence_count": 1,
        "occurrences": [{
            "work_id": "work-1",
            "execution_id": "publisher-repair:daily-2026-08-07",
            "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
        }],
    }


def _write_queue(state: Path, items: dict) -> Path:
    path = state / "self-heal" / "incident-queue.json"
    path.write_text(json.dumps({
        "schema": "writer.self-heal.incident-queue", "version": 1, "items": items,
    }))
    return path


def _dispatch(state: Path, runner: Path, scenario_path: Path, calls: Path, *,
              observed_at: str = "2026-08-07T08:00:00Z",
              budget: str = "3", max_slices: str | None = None) -> dict:
    environment = dict(os.environ)
    environment["STUB_CALLS"] = str(calls)
    environment["STUB_SCENARIO"] = str(scenario_path)
    environment["ARTICLE_REPAIR_TERRA_TIMEOUT_SECONDS"] = budget
    if max_slices is not None:
        environment["ARTICLE_REPAIR_MAX_INVESTIGATION_SLICES"] = max_slices
    result = subprocess.run(
        [
            sys.executable, str(DISPATCH),
            "--state-root", str(state),
            "--scripts", str(ROOT / "scripts"),
            "--registry", str(ROOT / "config" / "repair-runbooks.json"),
            "--model-runner", str(runner),
            "--observed-at", observed_at,
            "--publication-backlog", "0",
        ],
        capture_output=True, text=True, check=False, env=environment,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _attempt(outcome: dict) -> dict:
    return json.loads(Path(outcome["repair_attempt_receipt"]).read_text())


def _calls(calls: Path) -> list[dict]:
    if not calls.is_file():
        return []
    return [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]


def _reopen(state: Path, fingerprint: str = "b" * 64) -> None:
    """Hand the incident back so the next tick can claim the same fingerprint."""
    path = state / "self-heal" / "incident-queue.json"
    queue = json.loads(path.read_text())
    item = queue["items"][fingerprint]
    item["state"] = "OPEN"
    item["next_action"] = "CLAIM"
    item.pop("lease_id", None)
    path.write_text(json.dumps(queue))


# ---------------------------------------------------------------------------
# 1. H3 — a budget overrun checkpoints instead of dying
# ---------------------------------------------------------------------------

def test_budget_overrun_checkpoints_instead_of_recording_a_dead_timeout(
    tmp_path: Path,
) -> None:
    """The live failure: 120s budget expired and the attempt learned nothing.

    A checkpoint must be distinguishable from a dead TIMEOUT, carry the Codex
    session identity, and keep the partial findings the slice did produce.
    """
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID,
                   "partial": "note rejects the embedded raw HTML block"},
    )

    outcome = _dispatch(state, runner, scenario, calls)
    attempt = _attempt(outcome)
    model = attempt["model"]

    # The regression under repair: the budget must not produce a dead TIMEOUT.
    assert model["status"] == "CHECKPOINTED", model
    assert model["continuable"] is True
    assert model["session"]["id"] == SESSION_ID
    # Partial work survives the budget rather than being discarded.
    assert "note rejects the embedded raw HTML block" in json.dumps(
        model["partial_findings"], ensure_ascii=False
    )
    # Budget honesty: TIMEOUT keeps its meaning of "dead, nothing learned".
    assert model["status"] != "TIMEOUT"
    assert model["timeout_seconds"] == 3
    # Cost stays unknown with a reason; nothing is invented.
    assert attempt["cost"]["status"] == "unknown"
    assert attempt["cost"]["reason"]

    checkpoint = json.loads(
        (state / "self-heal" / "investigation-sessions" / f"{'b' * 64}.json").read_text()
    )
    assert checkpoint["status"] == "CONTINUABLE"
    assert checkpoint["session"]["id"] == SESSION_ID
    assert checkpoint["slice_count"] == 1


def test_a_later_tick_resumes_the_same_session_and_never_starts_a_second(
    tmp_path: Path,
) -> None:
    """No duplicate model spend: the continuation is a resume, not a new run."""
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )

    first = _dispatch(state, runner, scenario, calls)
    assert _attempt(first)["model"]["status"] == "CHECKPOINTED"

    # Second tick: the same incident, and the investigation now completes.
    _reopen(state)
    scenario.write_text(json.dumps({"mode": "complete", "session_id": SESSION_ID}))
    second = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T08:05:00Z",
    )
    attempt = _attempt(second)

    recorded = _calls(calls)
    assert len(recorded) == 2, recorded
    assert recorded[0]["resume_session_id"] is None, "first slice must start cold"
    assert recorded[1]["resume_session_id"] == SESSION_ID, (
        "the continuation must resume the checkpointed session, not open a new one"
    )
    assert attempt["model"]["status"] == "COMPLETED"
    assert attempt["model"]["resumed_session_id"] == SESSION_ID
    assert attempt["model"]["session"]["id"] == SESSION_ID

    checkpoint = json.loads(
        (state / "self-heal" / "investigation-sessions" / f"{'b' * 64}.json").read_text()
    )
    assert checkpoint["status"] == "COMPLETE"
    assert checkpoint["slice_count"] == 2


# ---------------------------------------------------------------------------
# 2. C1 — the verdict comes from the event stream, not the exit code
# ---------------------------------------------------------------------------

def test_failure_is_named_by_the_event_that_produced_it(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "failed", "session_id": SESSION_ID},
    )

    attempt = _attempt(_dispatch(state, runner, scenario, calls))
    model = attempt["model"]

    assert model["status"] == "FAILED"
    # "derives its status from the JSONL event stream ... A run that fails must
    # name which event produced that verdict in the receipt."
    assert model["deciding_event"] in {"error", "turn.failed"}
    assert "upstream refused the request" in json.dumps(model, ensure_ascii=False)


def test_advisory_item_level_error_does_not_fail_a_completed_turn(
    tmp_path: Path,
) -> None:
    """Measured on codex 0.145.0: an `error` item rides along inside exit-0 runs."""
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID},
    )

    attempt = _attempt(_dispatch(state, runner, scenario, calls))

    assert attempt["model"]["status"] == "COMPLETED"
    assert attempt["model"].get("deciding_event") == "turn.completed"


def test_schema_bound_verdict_keeps_the_investigation_receipt_keys(
    tmp_path: Path,
) -> None:
    """The structured verdict mirrors what the investigation receipt stores."""
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    verdict = {
        "cause_status": "EVIDENCE_BACKED_HYPOTHESIS",
        "evidence_gaps": ["browser_evidence_missing"],
        "findings": [{"statement": "note rejects raw HTML", "verified": True}],
        "primary_sources": [],
        "complete": True,
        "remaining_work": "",
    }
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID, "verdict": verdict},
    )

    attempt = _attempt(_dispatch(state, runner, scenario, calls))
    model = attempt["model"]

    assert model["verdict"]["cause_status"] == "EVIDENCE_BACKED_HYPOTHESIS"
    assert model["verdict"]["evidence_gaps"] == ["browser_evidence_missing"]
    assert model["verdict_schema_path"], "the run must bind --output-schema"
    schema = json.loads(Path(model["verdict_schema_path"]).read_text())
    assert set(schema["properties"]) >= {"cause_status", "evidence_gaps"}
    # The stub records the flag the real runner is asked to pass through.
    assert _calls(calls)[0]["output_schema"] == model["verdict_schema_path"]


def test_tokens_are_measured_from_the_event_stream_when_the_turn_completes(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID},
    )

    attempt = _attempt(_dispatch(state, runner, scenario, calls))

    assert attempt["tokens"]["status"] == "measured"
    assert attempt["tokens"]["input_tokens"] == 22977
    assert attempt["tokens"]["output_tokens"] == 20
    assert attempt["tokens"]["source"] == "turn.completed"


def test_tokens_stay_unknown_with_a_reason_when_the_turn_never_completes(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )

    attempt = _attempt(_dispatch(state, runner, scenario, calls))

    assert attempt["tokens"]["status"] == "unknown"
    assert attempt["tokens"]["reason"]


# ---------------------------------------------------------------------------
# 3. §9.3.1 escalation — bounded attempts, then degrade and stop
# ---------------------------------------------------------------------------

def test_investigation_slices_are_bounded_and_then_stop_spending_model_time(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )

    for index in range(2):
        outcome = _dispatch(
            state, runner, scenario, calls,
            observed_at=f"2026-08-07T08:0{index}:00Z", max_slices="2",
        )
        assert _attempt(outcome)["model"]["status"] == "CHECKPOINTED"
        _reopen(state)

    exhausted = _dispatch(
        state, runner, scenario, calls,
        observed_at="2026-08-07T08:09:00Z", max_slices="2",
    )
    attempt = _attempt(exhausted)

    assert attempt["model"]["status"] == "EXHAUSTED"
    assert attempt["next_action"] == "ESCALATE_AFTER_INVESTIGATION_BUDGET_EXHAUSTED"
    # Degrade and stop: no third model process is started.
    assert len(_calls(calls)) == 2, _calls(calls)

    checkpoint = json.loads(
        (state / "self-heal" / "investigation-sessions" / f"{'b' * 64}.json").read_text()
    )
    assert checkpoint["status"] == "EXHAUSTED"

    # The safest known state is preserved: the circuit receipt is untouched.
    circuit = state / "runs" / "daily-2026-08-07" / "gates" / "resume-failure-circuit.json"
    before = json.dumps({
        "version": 1,
        "pairs": {"note/ja": {"open": True, "signature": NOTE_422}},
    }).encode()
    assert circuit.read_bytes() == before


def test_a_finished_investigation_is_never_re_run_for_the_same_trigger(
    tmp_path: Path,
) -> None:
    """One investigation per incident: re-running a verdict is a second one."""
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID},
    )

    first = _dispatch(state, runner, scenario, calls)
    assert _attempt(first)["model"]["status"] == "COMPLETED"

    _reopen(state)
    again = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T08:05:00Z",
    )
    attempt = _attempt(again)

    assert attempt["model"]["status"] == "ALREADY_INVESTIGATED"
    assert attempt["model"]["verdict"]["cause_status"] == "EVIDENCE_BACKED_HYPOTHESIS"
    assert len(_calls(calls)) == 1, "no second model process may start"


def test_a_new_occurrence_reopens_an_exhausted_investigation_budget(
    tmp_path: Path,
) -> None:
    """Bounded, not permanently dead: a fresh trigger restores the budget."""
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )

    _dispatch(state, runner, scenario, calls, max_slices="1")
    _reopen(state)
    exhausted = _dispatch(
        state, runner, scenario, calls,
        observed_at="2026-08-07T08:05:00Z", max_slices="1",
    )
    assert _attempt(exhausted)["model"]["status"] == "EXHAUSTED"
    assert len(_calls(calls)) == 1

    # A new occurrence of the same failure is the new trigger.
    queue_path = state / "self-heal" / "incident-queue.json"
    queue = json.loads(queue_path.read_text())
    item = queue["items"]["b" * 64]
    item["state"] = "OPEN"
    item["next_action"] = "CLAIM"
    item.pop("lease_id", None)
    item["occurrence_count"] = 2
    queue_path.write_text(json.dumps(queue))

    scenario.write_text(json.dumps({"mode": "complete", "session_id": SESSION_ID}))
    revived = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T08:10:00Z",
        max_slices="1",
    )
    assert _attempt(revived)["model"]["status"] == "COMPLETED"
    assert len(_calls(calls)) == 2


# ---------------------------------------------------------------------------
# 4. C3 liveness guard — never resume a session another process still holds
# ---------------------------------------------------------------------------

def test_a_live_session_process_blocks_resume_and_starts_no_second_session(
    tmp_path: Path,
) -> None:
    """openai/codex#37047: resuming a thread that is still active is the hazard.

    Measured 2026-08-07 on codex 0.145.0: `codex exec resume <id>` recovers a
    session killed mid-turn in 5.0s, so an unterminated turn on disk is not by
    itself a reason to refuse.  The decisive guard for this path is that no
    process is still holding the session, because a concurrent turn on one
    thread is both duplicate spend and the inconsistent-active state that
    issue reports.
    """
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )

    _dispatch(state, runner, scenario, calls)
    _reopen(state)

    # Claim the checkpoint as still owned by a living process: this process.
    checkpoint_path = (
        state / "self-heal" / "investigation-sessions" / f"{'b' * 64}.json"
    )
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["process"]["pgid"] = os.getpgid(0)
    checkpoint["process"]["reaped"] = False
    checkpoint_path.write_text(json.dumps(checkpoint))

    blocked = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T08:05:00Z",
    )
    attempt = _attempt(blocked)

    assert attempt["model"]["status"] == "RESUME_BLOCKED_SESSION_BUSY"
    assert attempt["model"]["liveness"]["reaped"] is False
    # No second model process may start while the first still holds the session.
    assert len(_calls(calls)) == 1, _calls(calls)


def test_resume_requires_a_session_identity_and_never_guesses_with_last(
    tmp_path: Path,
) -> None:
    """A checkpoint with no captured session id is a dead TIMEOUT, not a resume.

    `codex exec resume --last` picks the newest session for the working
    directory, which this repository shares with other loops, so it cannot
    prove identity.  Without a captured id the slice must degrade rather than
    continue a stranger's session.
    """
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    # The stub emits no thread.started when told to stay silent.
    runner, calls, scenario = _stub_runner(tmp_path, {"mode": "hang"})
    runner.write_text(STUB.replace(
        'emit({"type": "thread.started", "thread_id": session_id})', "pass",
    ))
    runner.chmod(0o755)

    attempt = _attempt(_dispatch(state, runner, scenario, calls))

    assert attempt["model"]["status"] == "TIMEOUT"
    assert attempt["model"]["continuable"] is False
    assert attempt["model"]["reason"]
    assert attempt["model"]["session"]["id"] is None
    # No resume may be attempted, because there is no identity to resume.
    assert _calls(calls)[0]["resume_session_id"] is None
    # The slice was still paid for, so it is still counted against the bound;
    # otherwise a slice that reliably learns nothing would run on every tick.
    checkpoint = json.loads(
        (state / "self-heal" / "investigation-sessions" / f"{'b' * 64}.json").read_text()
    )
    assert checkpoint["status"] == "FAILED"
    assert checkpoint["slice_count"] == 1
    assert checkpoint["session"]["id"] is None


# ---------------------------------------------------------------------------
# 5. The lease must not strand the incident (defect found in review of 698e2c29)
# ---------------------------------------------------------------------------

def _queue(state: Path) -> dict:
    return json.loads((state / "self-heal" / "incident-queue.json").read_text())


def test_a_checkpointed_investigation_is_continued_without_manual_requeue(
    tmp_path: Path,
) -> None:
    """The continuation must be reachable by the installed loop, not just by tests.

    `select()` considers only OPEN and RETRY, and `claim()` sets CLAIMED, so an
    investigation that ends CHECKPOINTED while still holding its lease can never
    be picked up again: there is no lease-expiry reaper anywhere in the tree,
    and `ingest` reopens only RESOLVED, so not even a new occurrence rescues it.
    This test runs two ticks back to back with no queue surgery in between.
    """
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )

    first = _dispatch(state, runner, scenario, calls)
    assert _attempt(first)["model"]["status"] == "CHECKPOINTED"

    # The lease must be gone and the incident selectable again.
    item = _queue(state)["items"]["b" * 64]
    assert item["state"] == "RETRY", "a checkpointed investigation must release its lease"
    assert "lease_id" not in item
    assert item["next_action"] == "CONTINUE_INVESTIGATION"

    # Second tick, with no intervention at all.
    scenario.write_text(json.dumps({"mode": "complete", "session_id": SESSION_ID}))
    second = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T08:05:00Z",
    )

    assert second.get("status") != "NO_ACTIONABLE_INCIDENT"
    assert second["fingerprint"] == "b" * 64
    attempt = _attempt(second)
    assert attempt["model"]["status"] == "COMPLETED"
    assert attempt["model"]["resumed_session_id"] == SESSION_ID
    recorded = _calls(calls)
    assert len(recorded) == 2
    assert recorded[1]["resume_session_id"] == SESSION_ID


def test_a_pre_checkpoint_stranded_lease_is_reclaimed_by_the_loop_itself(
    tmp_path: Path,
) -> None:
    """The live `note/ja` shape: CLAIMED, dead TIMEOUT receipt, no checkpoint.

    Checkpoints did not exist when it timed out, so it carries none. Recovery
    must be code the loop runs, not a hand edit.
    """
    state = _seed_state(tmp_path)
    stranded_lease = "repair-7cfc1bfb24f6487298378cb84438a637"
    item = _incident()
    item["state"] = "CLAIMED"
    item["lease_id"] = stranded_lease
    item["next_action"] = "COLLECT_GAPS_THEN_SEARCH_OFFICIAL_PRIMARY_DOCS"
    item["attempt_count"] = 1
    _write_queue(state, {"b" * 64: item})
    # The exact terminal receipt the live incident holds.
    attempts = state / "self-heal" / "repair-attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    (attempts / f"{'b' * 64}-{stranded_lease}.json").write_text(json.dumps({
        "schema": "writer.self-heal.repair-attempt", "version": 1,
        "fingerprint": "b" * 64, "lease_id": stranded_lease,
        "model": {"status": "TIMEOUT", "latency_ms": 120004, "timeout_seconds": 120},
    }))
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID},
    )

    outcome = _dispatch(state, runner, scenario, calls)

    assert outcome.get("status") != "NO_ACTIONABLE_INCIDENT", (
        "the stranded incident must become selectable again"
    )
    assert outcome["fingerprint"] == "b" * 64
    reclaimed = outcome["reclaimed_stranded_leases"]
    assert reclaimed[0]["lease_id"] == stranded_lease
    assert reclaimed[0]["model_status"] == "TIMEOUT"
    assert _attempt(outcome)["model"]["status"] == "COMPLETED"


def test_a_claimed_incident_without_terminal_evidence_is_never_stolen(
    tmp_path: Path,
) -> None:
    """Liveness is proved from receipts, never guessed from elapsed time."""
    state = _seed_state(tmp_path)
    item = _incident()
    item["state"] = "CLAIMED"
    item["lease_id"] = "production-repair-20260806-note-s3"
    item["next_action"] = "RUNBOOK_OR_INVESTIGATE"
    _write_queue(state, {"b" * 64: item})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID},
    )

    outcome = _dispatch(state, runner, scenario, calls)

    assert outcome["status"] == "NO_ACTIONABLE_INCIDENT"
    assert outcome["reclaimed_stranded_leases"] == []
    assert _queue(state)["items"]["b" * 64]["state"] == "CLAIMED"
    assert _calls(calls) == []


# ---------------------------------------------------------------------------
# 6. An unroutable incident must stop consuming ticks
# ---------------------------------------------------------------------------

UNROUTABLE_WORK = {
    "work_id": "work-x",
    "execution_id": "publisher-repair:daily-2026-08-07",
    "phase": "destination:substack/ja",
    "reason": "invalid_receipt",
    "error_signature": "invalid_receipt",
    "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
}


def _unroutable_fingerprint() -> str:
    """The key `ingest` derives for work carrying no destination identity."""
    return _load_queue_module()._legacy_fingerprint(UNROUTABLE_WORK)


def _unroutable_incident(fingerprint: str | None = None) -> dict:
    """The live `346972de...` shape: routing raises before any investigation."""
    fingerprint = fingerprint or _unroutable_fingerprint()
    return {
        "fingerprint": fingerprint,
        "phase": "destination:substack/ja",
        "reason": "invalid_receipt",
        "classification": "state-corruption",
        "failure_class": "state-corruption",
        "error_signature": "invalid_receipt",
        # No run_id and no artifact_id, so the trace projection raises
        # "TraceError: generation and quality receipts are required".
        "revenue_role": "distribution",
        "blocking": False,
        "state": "OPEN",
        "first_seen_at": "2026-08-07T05:27:19Z",
        "attempt_count": 0,
        "occurrence_count": 1,
        "occurrences": [{
            "work_id": "work-x",
            "execution_id": "publisher-repair:daily-2026-08-07",
            "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
        }],
    }


def test_an_unroutable_incident_stops_consuming_ticks_after_its_bound(
    tmp_path: Path,
) -> None:
    """Live: eleven identical receipts on a five-minute cadence, without bound.

    The investigation-slice budget cannot cover this, because routing fails
    before any investigation starts.
    """
    state = _seed_state(tmp_path)
    unroutable = _unroutable_fingerprint()
    _write_queue(state, {unroutable: _unroutable_incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID},
    )

    seen = []
    for index in range(3):
        seen.append(_dispatch(
            state, runner, scenario, calls,
            observed_at=f"2026-08-07T08:0{index}:00Z",
        ))
    assert [row["status"] for row in seen] == [
        "ROUTING_FAILED", "ROUTING_FAILED", "ROUTING_EXHAUSTED",
    ]

    item = _queue(state)["items"][unroutable]
    assert item["state"] == "FAILED"
    assert item["next_action"] == "ESCALATE_AFTER_ROUTING_FAILURE_BUDGET_EXHAUSTED"
    assert item["exhausted"]["kind"] == "routing-failure"
    assert "lease_id" not in item
    # History is preserved, never deleted or rewritten.
    assert item["occurrence_count"] == 1
    assert len(item["occurrences"]) == 1

    # The fourth tick must do no work at all.
    quiet = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T08:20:00Z",
    )
    assert quiet["status"] == "NO_ACTIONABLE_INCIDENT"

    # The circuit receipt is untouched by the escalation.
    circuit = state / "runs" / "daily-2026-08-07" / "gates" / "resume-failure-circuit.json"
    before = json.dumps({
        "version": 1,
        "pairs": {"note/ja": {"open": True, "signature": NOTE_422}},
    }).encode()
    assert circuit.read_bytes() == before


def test_an_exhausted_routing_failure_rearms_on_a_genuinely_new_occurrence(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    unroutable = _unroutable_fingerprint()
    _write_queue(state, {unroutable: _unroutable_incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": SESSION_ID},
    )
    for index in range(3):
        _dispatch(
            state, runner, scenario, calls,
            observed_at=f"2026-08-07T08:0{index}:00Z",
        )
    assert _queue(state)["items"][unroutable]["state"] == "FAILED"

    queue_module = _load_queue_module()
    queue = _queue(state)
    replay = {
        "schema": "writer.observability.slo-replay",
        # Same identity, genuinely new occurrence.
        "slo_work": [dict(UNROUTABLE_WORK, work_id="work-y")],
    }
    queue_module.ingest(queue, replay, "2026-08-07T09:00:00Z")
    item = queue["items"][unroutable]

    assert item["state"] == "OPEN", "a new trigger must re-arm the bounded incident"
    assert item["next_action"] == "CLAIM"
    assert item["occurrence_count"] == 2
    assert item["previous_exhaustion"]["kind"] == "routing-failure"

    # Re-ingesting the same occurrence must not re-arm anything a second time.
    queue_module.ingest(queue, replay, "2026-08-07T09:05:00Z")
    assert queue["items"][unroutable]["rearm_count"] == 1


def _load_queue_module():
    import importlib.util as _util
    spec = _util.spec_from_file_location(
        "writer_incident_queue", ROOT / "scripts" / "writer_incident_queue.py",
    )
    module = _util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 7. Strict structured-output conformance (defect found in production, c1da3edc)
# ---------------------------------------------------------------------------

def _session_module():
    import importlib.util as _util
    spec = _util.spec_from_file_location(
        "writer_repair_investigation_session",
        ROOT / "scripts" / "writer_repair_investigation_session.py",
    )
    module = _util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _walk_objects(schema, path="$"):
    """Yield (path, schema) for every object schema, including inside items."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        yield path, schema
        for key, value in (schema.get("properties") or {}).items():
            yield from _walk_objects(value, f"{path}.properties.{key}")
    if schema.get("items") is not None:
        yield from _walk_objects(schema["items"], f"{path}.items")


def test_verdict_schema_is_strict_conformant_at_every_nesting_level() -> None:
    """The rule the provider enforces, checked structurally rather than by bytes.

    Primary source, https://platform.openai.com/docs/guides/structured-outputs:
    "Although all fields must be required (and the model will return a value
    for each parameter), it is possible to emulate an optional parameter by
    using a union type with `null`." and "`additionalProperties: false` must
    always be set in objects".

    Production receipt slice-1 failed with 400 invalid_json_schema: "In
    context=('properties', 'findings', 'items'), 'required' is required to be
    supplied and to be an array including every key in properties. Missing
    'evidence'." A valid top level does not make a schema valid.
    """
    schema = _session_module().VERDICT_SCHEMA
    objects = dict(_walk_objects(schema))
    # The nested cases the provider actually rejected must be covered.
    assert "$.properties.findings.items" in objects
    assert "$.properties.primary_sources.items" in objects

    for path, node in objects.items():
        properties = node.get("properties")
        assert isinstance(properties, dict) and properties, f"{path}: no properties"
        assert node.get("additionalProperties") is False, (
            f"{path}: additionalProperties must always be set to false in objects"
        )
        required = node.get("required")
        assert isinstance(required, list), (
            f"{path}: 'required' is required to be supplied and to be an array"
        )
        assert sorted(required) == sorted(properties), (
            f"{path}: 'required' must include every key in properties; "
            f"missing {sorted(set(properties) - set(required))}"
        )

    # Optional fields are nullable, never omitted from required.
    findings = objects["$.properties.findings.items"]
    assert findings["properties"]["evidence"]["type"] == ["string", "null"]
    assert schema["properties"]["remaining_work"]["type"] == ["string", "null"]


def test_the_schema_checker_rejects_each_way_a_schema_can_go_wrong() -> None:
    """The checker must catch the real mistakes, not just pass the good case."""
    module = _session_module()
    module.assert_strict_schema(module.VERDICT_SCHEMA)

    nested_missing_required = {
        "type": "object", "additionalProperties": False, "required": ["rows"],
        "properties": {"rows": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["a"], "properties": {"a": {"type": "string"},
                                              "b": {"type": "string"}},
        }}},
    }
    try:
        module.assert_strict_schema(nested_missing_required)
        raise AssertionError("a nested items object missing a required key must fail")
    except ValueError as error:
        assert "items" in str(error) and "'b'" in str(error)

    missing_additional_properties = {
        "type": "object", "required": ["a"], "properties": {"a": {"type": "string"}},
    }
    try:
        module.assert_strict_schema(missing_additional_properties)
        raise AssertionError("a missing additionalProperties must fail")
    except ValueError as error:
        assert "additionalProperties" in str(error)

    unsupported_keyword = {
        "type": "object", "additionalProperties": False, "required": ["a"],
        "properties": {"a": {"type": "string"}}, "allOf": [],
    }
    try:
        module.assert_strict_schema(unsupported_keyword)
        raise AssertionError("an unsupported composition keyword must fail")
    except ValueError as error:
        assert "allOf" in str(error)


def test_a_turn_that_fails_before_any_output_is_terminal_but_still_counted(
    tmp_path: Path,
) -> None:
    """The production slice-1 shape: rejected in 4.4s, before any reasoning.

    Resuming a session whose first turn never produced output re-sends the
    identical failing request, so it would burn the whole bound to learn
    nothing. It must be terminal for the session and still count against the
    bound, and be distinguishable from both a dead TIMEOUT and a continuable
    checkpoint.
    """
    state = _seed_state(tmp_path)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "failed", "session_id": SESSION_ID},
    )

    attempt = _attempt(_dispatch(state, runner, scenario, calls))
    model = attempt["model"]

    assert model["status"] == "FAILED"
    assert model["status"] != "TIMEOUT"
    assert model["continuable"] is False
    assert model["made_progress"] is False
    assert model["reason"]
    assert model["deciding_event"] in {"error", "turn.failed"}

    checkpoint = json.loads(
        (state / "self-heal" / "investigation-sessions" / f"{'b' * 64}.json").read_text()
    )
    assert checkpoint["status"] == "FAILED"
    assert checkpoint["slice_count"] == 1, "the failed slice must count toward the bound"

    # A later tick must not resume the dead session.
    _reopen(state)
    _dispatch(state, runner, scenario, calls, observed_at="2026-08-07T08:05:00Z")
    recorded = _calls(calls)
    assert len(recorded) == 2
    assert recorded[1]["resume_session_id"] is None, (
        "a session with no progress must never be resumed"
    )
    assert json.loads(
        (state / "self-heal" / "investigation-sessions" / f"{'b' * 64}.json").read_text()
    )["slice_count"] == 2


# ---------------------------------------------------------------------------
# 8. A deploy is a new input (defect found in production after e0475e41)
# ---------------------------------------------------------------------------

COMMIT_A = "06141970eb640087ef4f9e696c909fbc607cfc5e"  # the live deployed-commit
COMMIT_B = "e0475e41" + "0" * 32


def _set_deployed_commit(state: Path, commit: str) -> None:
    """The marker `self_improve_control.update_deployed_commit` owns."""
    (state / "deployed-commit").write_text(commit + "\n", encoding="utf-8")


def _exhaust_investigation(state, runner, calls, scenario, *, at, slices=1):
    for index in range(slices):
        _dispatch(
            state, runner, scenario, calls, observed_at=at,
            max_slices=str(slices),
        )
        _reopen(state)
    return _dispatch(state, runner, scenario, calls, observed_at=at,
                     max_slices=str(slices))


def test_an_exhausted_incident_rearms_when_the_deployed_code_changes(
    tmp_path: Path,
) -> None:
    """SSOT §9.3.1 takes the rule from Flagger: retry "until a new commit arrives".

    Without this a repair can never be validated by the thing it repaired: the
    live `note/ja` incident spent all three slices proving the invalid-schema
    bug, and the fix landed after the budget was gone.
    """
    state = _seed_state(tmp_path)
    _set_deployed_commit(state, COMMIT_A)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )

    exhausted = _exhaust_investigation(
        state, runner, calls, scenario, at="2026-08-07T08:00:00Z", slices=1,
    )
    assert _attempt(exhausted)["model"]["status"] == "EXHAUSTED"
    item = _queue(state)["items"]["b" * 64]
    assert item["state"] == "FAILED"
    assert item["exhausted"]["deployed_commit"] == COMMIT_A

    # Same code version: it stays stopped, and no model process starts.
    before = len(_calls(calls))
    quiet = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T08:05:00Z",
        max_slices="1",
    )
    assert quiet["status"] == "NO_ACTIONABLE_INCIDENT"
    assert quiet["rearmed_on_new_code"] == []
    assert len(_calls(calls)) == before

    # A deploy arrives. That is a genuinely new input.
    _set_deployed_commit(state, COMMIT_B)
    scenario.write_text(json.dumps({"mode": "complete", "session_id": SESSION_ID}))
    revived = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T09:00:00Z",
        max_slices="1",
    )

    assert revived["fingerprint"] == "b" * 64
    assert _attempt(revived)["model"]["status"] == "COMPLETED", (
        "the budget must reset for the new code version"
    )
    assert len(_calls(calls)) == before + 1


def test_rearming_on_new_code_preserves_the_whole_history(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    _set_deployed_commit(state, COMMIT_A)
    _write_queue(state, {"b" * 64: _incident()})
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "hang", "session_id": SESSION_ID},
    )
    _exhaust_investigation(
        state, runner, calls, scenario, at="2026-08-07T08:00:00Z", slices=1,
    )
    before = _queue(state)["items"]["b" * 64]
    occurrences_before = before["occurrences"]
    exhaustion_before = before["exhausted"]

    _set_deployed_commit(state, COMMIT_B)
    outcome = _dispatch(
        state, runner, scenario, calls, observed_at="2026-08-07T09:00:00Z",
        max_slices="1",
    )

    item = _queue(state)["items"]["b" * 64]
    # Nothing erased.
    assert item["occurrences"] == occurrences_before
    assert item["occurrence_count"] == 1
    assert item["exhaustion_history"] == [exhaustion_before]
    assert item["previous_exhaustion"] == exhaustion_before
    # Visibly a second round under a new code version.
    assert item["code_rearm_rounds"] == 1
    assert item["rearmed_by"] == {
        "kind": "deployed-commit-change", "from": COMMIT_A, "to": COMMIT_B,
        "round": 1, "observed_at": "2026-08-07T09:00:00Z",
    }
    assert outcome["rearmed_on_new_code"][0]["from_deployed_commit"] == COMMIT_A
    # Prior receipts are untouched on disk.
    assert list((state / "self-heal" / "repair-attempts").glob("*.json"))


def test_repeated_deploys_cannot_rearm_an_unfixable_incident_forever() -> None:
    """The anti-cycle bound: a deploy buys a round, but only a bounded number.

    Otherwise a permanently unfixable incident would be revived by every future
    deploy forever, which is its own infinite loop. Driven directly against the
    re-arm rule so the bound is asserted rather than an interleaving.
    """
    module = _load_queue_module()
    limit = module.DEFAULT_MAX_CODE_REARM_ROUNDS
    work = {
        "work_id": "work-1", "phase": "destination:note/ja",
        "reason": NOTE_422, "error_signature": NOTE_422,
        "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__note__ja", "destination": "note/ja",
    }
    # The key `ingest` derives for this work, so the re-arm and the new
    # occurrence act on one identity rather than opening a duplicate beside it.
    key = module._fingerprint(work, "publisher-content-rejection")

    def exhausted_item(commit):
        return {
            "fingerprint": key, "state": "FAILED",
            "occurrences": [{"work_id": "work-1"}], "occurrence_count": 1,
            "exhausted": {
                "kind": "investigation-slices", "count": 3,
                "deployed_commit": commit,
            },
        }

    queue = {"items": {key: exhausted_item("0" * 40)}}
    for round_index in range(1, limit + 1):
        commit = f"{round_index:040x}"
        rearmed = module.rearm_on_new_code(queue, commit, f"2026-08-0{round_index}T00:00:00Z")
        assert len(rearmed) == 1, f"round {round_index} must re-arm"
        item = queue["items"][key]
        assert item["state"] == "OPEN"
        assert item["code_rearm_rounds"] == round_index
        # It exhausts again under this same new code version.
        item["state"] = "FAILED"
        item["exhausted"] = {
            "kind": "investigation-slices", "count": 3, "deployed_commit": commit,
        }

    # One deploy too many: refused, with a recorded reason, and still stopped.
    refused = module.rearm_on_new_code(queue, "f" * 40, "2026-08-09T00:00:00Z")
    assert refused == []
    item = queue["items"][key]
    assert item["state"] == "FAILED"
    assert item["code_rearm_rounds"] == limit
    assert item["code_rearm_capped"]["max_rounds"] == limit
    assert item["code_rearm_capped"]["reason"]
    # History survives every round.
    assert len(item["exhaustion_history"]) == limit
    # Only a genuinely new occurrence can revive it now.
    module.ingest(queue, {
        "schema": "writer.observability.slo-replay",
        "slo_work": [dict(work, work_id="work-2")],
    }, "2026-08-10T00:00:00Z")
    assert queue["items"][key]["state"] == "OPEN"


def test_an_unreadable_deployed_commit_marker_never_rearms(tmp_path: Path) -> None:
    """Fail closed: an absent or malformed marker is not evidence of a deploy."""
    module = _load_queue_module()
    item = {
        "fingerprint": "b" * 64, "state": "FAILED",
        "exhausted": {"kind": "investigation-slices", "deployed_commit": COMMIT_A},
    }
    queue = {"items": {"b" * 64: item}}
    assert module.rearm_on_new_code(queue, None, "2026-08-07T09:00:00Z") == []
    assert queue["items"]["b" * 64]["state"] == "FAILED"

    session_module = _session_module()
    state = tmp_path / "state"
    state.mkdir()
    assert session_module.read_deployed_commit(state) is None
    (state / "deployed-commit").write_text("not-a-commit\n")
    assert session_module.read_deployed_commit(state) is None
    (state / "deployed-commit").write_text(COMMIT_A + "\n")
    assert session_module.read_deployed_commit(state) == COMMIT_A


def test_the_live_dead_note_ja_incident_recovers_without_hand_repair(
    tmp_path: Path,
) -> None:
    """The exact live shape at 07:50:19Z, which recorded no code version at all.

    An exhaustion written before this change carries no `deployed_commit`, and
    that absence is itself proof it was exhausted under different code than
    whatever is deployed now, so it re-arms once and then follows the bound.
    """
    state = _seed_state(tmp_path)
    _set_deployed_commit(state, COMMIT_A)
    item = _incident()
    item["state"] = "FAILED"
    item["occurrence_count"] = 1
    item["next_action"] = "ESCALATE_AFTER_INVESTIGATION_BUDGET_EXHAUSTED"
    item["exhausted"] = {
        "kind": "investigation-slices", "count": 3, "trigger": 1,
        "reason": "the investigation used its 3 bounded slices without a verdict",
        "observed_at": "2026-08-07T07:50:19Z",
        # No deployed_commit: this record predates the concept.
    }
    _write_queue(state, {"b" * 64: item})
    # The live checkpoint: EXHAUSTED, 3 of 3, integer trigger.
    sessions = state / "self-heal" / "investigation-sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{'b' * 64}.json").write_text(json.dumps({
        "schema": "writer.self-heal.investigation-session", "version": 1,
        "fingerprint": "b" * 64, "status": "EXHAUSTED",
        "slice_count": 3, "max_slices": 3, "trigger": 1,
        "session": {"id": "019fdb27-3fb4-74a0-8f20-412bc38a2e6f"},
        "process": {"pgid": None, "reaped": True},
        "partial_findings": {"agent_messages": [], "advisory_errors": []},
        "slices": [],
    }))
    runner, calls, scenario = _stub_runner(
        tmp_path, {"mode": "complete", "session_id": "019fdb99-0000-7000-8000-000000000001"},
    )

    outcome = _dispatch(state, runner, scenario, calls)

    assert outcome["fingerprint"] == "b" * 64, "it must become selectable again"
    assert outcome["rearmed_on_new_code"][0]["from_deployed_commit"] is None
    attempt = _attempt(outcome)
    assert attempt["model"]["status"] == "COMPLETED", (
        "the budget must reset, since the old trigger carries no code version"
    )
    # The dead session is not resumed; the old one only ever failed.
    assert _calls(calls)[0]["resume_session_id"] is None
    assert _queue(state)["items"]["b" * 64]["exhaustion_history"][0]["count"] == 3
