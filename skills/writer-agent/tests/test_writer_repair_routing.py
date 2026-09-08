"""Order 4: route known failure classes to runbooks and unknown ones to Terra.

The fixtures below reuse the exact shape of the live `note/ja` incident opened
by Order 3 (`run_id daily-2026-08-07`, execution id
`publisher-repair:daily-2026-08-07`) so the routing contract is proved against
the real production identity rather than a convenient invention.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE_RUN = Path.home() / "profitable-claude" / "skills" / "writer-agent" / "state" / "runs"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


QUEUE = _module("writer_incident_queue")
ROUTER = _module("writer_repair_router")

NOTE_422 = (
    'NoteNativePublishError: Note native publish HTTP 422: '
    '{"error":{"code":"invalid","message":"本文に利用できない内容が含まれています。"}}'
)


# --------------------------------------------------------------------------
# 1. A publisher content rejection is its own failure class
# --------------------------------------------------------------------------

def test_note_http_422_body_rejection_is_its_own_failure_class() -> None:
    assert QUEUE._classification("destination:note/ja", NOTE_422) == (
        "publisher-content-rejection"
    )


def test_content_rejection_is_read_from_the_signature_when_the_reason_is_terse() -> None:
    assert QUEUE._classification(
        "destination:note/ja", "note-native-publish-failed", NOTE_422
    ) == "publisher-content-rejection"


def test_substack_identity_conflict_is_a_known_publisher_gate() -> None:
    assert QUEUE._classification(
        "destination:substack/en", "substack-publication-identity-conflict"
    ) == "publisher-identity"


def test_credential_and_rate_limit_classes_still_win_over_content_rejection() -> None:
    """403 and 429 are 4xx too; the pre-existing precise classes must not regress."""
    assert QUEUE._classification(
        "destination:note/ja", "note-body-image-s3-403-embedded-0-of-1"
    ) == "credential"
    assert QUEUE._classification(
        "destination:substack/en", "substack HTTP 429 rate-limit"
    ) == "rate-limit"


# --------------------------------------------------------------------------
# 2. Reclassification keeps one incident identity
# --------------------------------------------------------------------------

def test_reclassified_incident_is_adopted_in_place_with_lease_and_history() -> None:
    work = {
        "work_id": "work-2",
        "execution_id": "publisher-repair:daily-2026-08-07",
        "phase": "destination:note/ja",
        "reason": NOTE_422,
        "error_signature": NOTE_422,
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__note__ja",
        "destination": "note/ja",
        "revenue_role": "revenue-set",
        "blocking": True,
        "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
    }
    stale = QUEUE._digest({
        "scheme": QUEUE.DESTINATION_IDENTITY_SCHEME,
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__note__ja",
        "destination": "note/ja",
        "failure_class": "process",
    })
    queue = {
        "schema": "writer.self-heal.incident-queue",
        "version": 1,
        "items": {
            stale: {
                "fingerprint": stale,
                "phase": "destination:note/ja",
                "reason": NOTE_422,
                "classification": "process",
                "failure_class": "process",
                "error_signature": NOTE_422,
                "run_id": "daily-2026-08-07",
                "artifact_id": "daily-2026-08-07__note__ja",
                "destination": "note/ja",
                "revenue_role": "revenue-set",
                "blocking": True,
                "state": "CLAIMED",
                "lease_id": "lease-held",
                "attempt_count": 3,
                "first_seen_at": "2026-08-07T05:27:19Z",
                "occurrence_count": 1,
                "occurrences": [{"work_id": "work-1", "error_signature": NOTE_422}],
            }
        },
    }

    QUEUE.ingest(
        queue,
        {"schema": "writer.observability.slo-replay", "slo_work": [work]},
        "2026-08-07T07:00:00Z",
    )

    assert len(queue["items"]) == 1, "reclassification must not open a second incident"
    fingerprint, item = next(iter(queue["items"].items()))
    assert fingerprint != stale
    assert item["previous_fingerprint"] == stale
    assert item["failure_class"] == "publisher-content-rejection"
    assert item["classification"] == "publisher-content-rejection"
    assert item["state"] == "CLAIMED"
    assert item["lease_id"] == "lease-held"
    assert item["attempt_count"] == 3
    assert item["first_seen_at"] == "2026-08-07T05:27:19Z"
    assert [row["work_id"] for row in item["occurrences"]] == ["work-1", "work-2"]


def test_identity_reclassification_releases_stale_claim_for_known_runbook() -> None:
    work = {
        "work_id": "identity-2",
        "phase": "destination:substack/en",
        "reason": "substack-publication-identity-conflict",
        "error_signature": "substack-publication-identity-conflict",
        "run_id": "daily-2026-08-21",
        "artifact_id": "daily-2026-08-21__substack__en",
        "destination": "substack/en",
    }
    stale = QUEUE._fingerprint(work, "process")
    queue = {
        "schema": QUEUE.SCHEMA,
        "version": QUEUE.VERSION,
        "items": {stale: {
            "fingerprint": stale,
            "failure_class": "process",
            "classification": "process",
            "state": "CLAIMED",
            "lease_id": "old-terra-lease",
            "attempt_count": 4,
            "occurrences": [{"work_id": "identity-1"}],
        }},
    }

    QUEUE.ingest(
        queue,
        {"schema": "writer.observability.slo-replay", "slo_work": [work]},
        "2026-08-21T06:10:00Z",
    )

    assert len(queue["items"]) == 1
    item = next(iter(queue["items"].values()))
    assert item["failure_class"] == "publisher-identity"
    assert item["state"] == "RETRY"
    assert item["next_action"] == "CLAIM"
    assert "lease_id" not in item
    assert item["previous_lease_id"] == "old-terra-lease"
    assert item["attempt_count"] == 4


# --------------------------------------------------------------------------
# 3. The router must resolve the run from the incident's own run_id
# --------------------------------------------------------------------------

def _live_shaped_incident() -> dict:
    return {
        "fingerprint": "f" * 64,
        "state": "CLAIMED",
        "phase": "destination:note/ja",
        "reason": NOTE_422,
        "classification": "publication-readback",
        "failure_class": "publication-readback",
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__note__ja",
        "destination": "note/ja",
        "occurrences": [{
            "work_id": "work-1",
            "execution_id": "publisher-repair:daily-2026-08-07",
            "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
        }],
    }


def test_router_prefers_the_incident_run_id_over_the_execution_id_prefix(
    tmp_path: Path,
) -> None:
    """`publisher-repair:daily-2026-08-07` must not become run `repair:daily-2026-08-07`."""
    item = _live_shaped_incident()
    assert ROUTER._run_id(item) == "daily-2026-08-07"

    state_dir = tmp_path / "state"
    (state_dir / "runs" / "daily-2026-08-07").mkdir(parents=True)
    queue = {
        "schema": "writer.self-heal.incident-queue",
        "version": 1,
        "items": {"f" * 64: item},
    }
    registry = json.loads((ROOT / "config" / "repair-runbooks.json").read_text())

    decision = ROUTER.route(
        queue, registry, "f" * 64, state_dir, ROOT / "scripts",
    )

    assert decision["route"] == "KNOWN"
    assert decision["runbook_id"] == "publication-plan-v1"
    assert str(state_dir / "runs" / "daily-2026-08-07") in " ".join(decision["command"])


def test_router_still_derives_run_id_from_a_legacy_publication_execution_id() -> None:
    item = {
        "state": "CLAIMED",
        "occurrences": [{"execution_id": "publication-20260806-084924"}],
    }
    assert ROUTER._run_id(item) == "20260806-084924"


# --------------------------------------------------------------------------
# 4. Dispatch: claim ordering, one per tick, lease
# --------------------------------------------------------------------------

DISPATCH = ROOT / "scripts" / "writer_repair_dispatch.py"


FAKE_VERDICT = json.dumps({
    "cause_status": "UNDETERMINED", "evidence_gaps": [], "findings": [],
    "primary_sources": [], "complete": True, "remaining_work": None,
})


def _fake_model_runner(path: Path, calls: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        # Mirrors the real runner's role -> model binding so the receipt's model
        # name is measured from the runner in use, never guessed.
        'CODEX_MODEL="gpt-5.6-terra"\n'
        f'echo "role=${{ARTICLE_MODEL_ROLE:-unset}} provider=${{ARTICLE_PROVIDER:-unset}} '
        f'sol=${{ARTICLE_SOL_TRIGGER_RECEIPT:-unset}} argv=$*" >> "{calls}"\n'
        # SSOT §9.4 C1: the repair path now reads its outcome from the codex
        # `--json` event stream, because `codex exec` exits only 0 or 1 and
        # cannot name a failure class. A runner that emits no stream is, by
        # that contract, a run that produced no verdict, so this stand-in
        # reproduces the event shapes measured from codex-cli 0.145.0.
        'if [ -n "${ARTICLE_CODEX_EVENTS_FILE:-}" ]; then\n'
        '  mkdir -p "$(dirname "$ARTICLE_CODEX_EVENTS_FILE")"\n'
        '  {\n'
        '    printf \'{"type":"thread.started","thread_id":"019fdb05-0000-7000-8000-000000000001"}\\n\'\n'
        '    printf \'{"type":"turn.started"}\\n\'\n'
        '    printf \'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\\n\'\n'
        '  } > "$ARTICLE_CODEX_EVENTS_FILE"\n'
        '  if [ -n "${ARTICLE_CODEX_LAST_MESSAGE_FILE:-}" ]; then\n'
        f'    printf \'%s\' \'{FAKE_VERDICT}\' > "$ARTICLE_CODEX_LAST_MESSAGE_FILE"\n'
        '  fi\n'
        'fi\n'
        'echo "terra investigation output"\n'
    )
    path.chmod(0o755)


def _seed_state(tmp_path: Path, *, with_run: bool = True) -> Path:
    state = tmp_path / "state"
    (state / "self-heal").mkdir(parents=True)
    if with_run:
        run_gates = state / "runs" / "daily-2026-08-07" / "gates"
        run_gates.mkdir(parents=True)
        source = LIVE_RUN / "daily-2026-08-07" / "gates"
        # The exact receipts the trace projection needs to reproduce the real
        # `destination:note/ja` failure, copied read-only out of live state.
        for name in (
            "generation-state.json", "quality-self-heal.json",
            "publication-state.json", "resume-failure-circuit.json",
        ):
            shutil.copy(source / name, run_gates / name)
    return state


def _queue_with(items: dict) -> dict:
    return {"schema": "writer.self-heal.incident-queue", "version": 1, "items": items}


def _blocking_revenue_incident(fingerprint: str = "b" * 64) -> dict:
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


def _older_distribution_incident(fingerprint: str = "c" * 64) -> dict:
    return {
        "fingerprint": fingerprint,
        "phase": "destination:zenn-article/ja",
        "reason": "zenn-stage-timeout-no-dispatch-result",
        "classification": "process",
        "failure_class": "process",
        "error_signature": "zenn-stage-timeout-no-dispatch-result",
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__zenn__ja",
        "destination": "zenn-article/ja",
        "revenue_role": "distribution",
        "blocking": False,
        "state": "OPEN",
        "first_seen_at": "2026-08-06T13:19:01Z",
        "attempt_count": 0,
        "occurrence_count": 1,
        "occurrences": [{
            "work_id": "work-0",
            "execution_id": "publisher-repair:daily-2026-08-07",
            "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
        }],
    }


def _metrics_incident(fingerprint: str = "e" * 64) -> dict:
    """An UNKNOWN-routing failure that owns no destination circuit."""
    return {
        "fingerprint": fingerprint,
        "phase": "metrics",
        "reason": "expected_receipt_missing",
        "classification": "measurement",
        "failure_class": "measurement",
        "error_signature": "expected_receipt_missing",
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__metrics",
        "revenue_role": "distribution",
        "blocking": False,
        "state": "OPEN",
        "first_seen_at": "2026-08-07T05:10:00Z",
        "attempt_count": 0,
        "occurrence_count": 1,
        "occurrences": [{
            "work_id": "work-7",
            "execution_id": "publisher-repair:daily-2026-08-07",
            "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
        }],
    }


def _older_reporting_incident(fingerprint: str = "d" * 64) -> dict:
    """An older non-blocking reporting failure that a runbook already covers."""
    return {
        "fingerprint": fingerprint,
        "phase": "reporting",
        "reason": "expected_receipt_missing",
        "classification": "process",
        "failure_class": "process",
        "error_signature": "expected_receipt_missing",
        "run_id": "daily-2026-08-07",
        "artifact_id": "daily-2026-08-07__report",
        "revenue_role": "distribution",
        "blocking": False,
        "state": "OPEN",
        "first_seen_at": "2026-08-06T13:19:01Z",
        "attempt_count": 0,
        "occurrence_count": 1,
        "occurrences": [{
            "work_id": "work-9",
            "execution_id": "publisher-repair:daily-2026-08-07",
            "source_receipt": {"path": "gates/publication-state.json", "sha256": "a" * 64},
        }],
    }


def _dispatch(state: Path, tmp_path: Path, *, registry: Path | None = None,
              observed_at: str = "2026-08-07T08:00:00Z",
              publication_backlog: str = "0",
              defer_model_always: bool = False) -> dict:
    runner = tmp_path / "runtime" / "model-runner.sh"
    if not runner.exists():
        _fake_model_runner(runner, tmp_path / "model-calls")
    result = subprocess.run(
        [
            "python3", str(DISPATCH),
            "--state-root", str(state),
            "--scripts", str(ROOT / "scripts"),
            "--registry", str(registry or ROOT / "config" / "repair-runbooks.json"),
            "--model-runner", str(runner),
            "--observed-at", observed_at,
            "--publication-backlog", publication_backlog,
            *( ["--defer-model-always"] if defer_model_always else [] ),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _fake_df_env(tmp_path: Path) -> str:
    """Give resume subprocess fixtures deterministic headroom."""
    env_file = tmp_path / "df-env.sh"
    env_file.write_text(
        "df() {\n"
        "  printf '%s\\n' "
        "'Filesystem 1024-blocks Used Available Capacity Mounted on' "
        "'/dev/test 999999999 0 1000000 1% /'\n"
        "}\n"
    )
    return str(env_file)


def _copy_resume_runtime(scripts: Path) -> None:
    for name in (
        "article-resume-pending.sh",
        "writer-runtime-env.sh",
        "writer_capacity_floor.py",
    ):
        shutil.copy(ROOT / "scripts" / name, scripts)


def test_dispatch_claims_the_blocking_revenue_set_incident_before_an_older_distribution_one(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    queue_path = state / "self-heal" / "incident-queue.json"
    queue_path.write_text(json.dumps(_queue_with({
        "c" * 64: _older_distribution_incident(),
        "b" * 64: _blocking_revenue_incident(),
    })))

    decision = _dispatch(state, tmp_path)

    assert decision["fingerprint"] == "b" * 64
    stored = json.loads(queue_path.read_text())["items"]
    # An investigation that reached a verdict keeps its lease as a handoff to
    # the next stage, exactly as a KNOWN runbook decision does.
    assert stored["b" * 64]["state"] == "CLAIMED"
    assert stored["b" * 64]["lease_id"] == decision["lease_id"]
    assert stored["c" * 64]["state"] == "OPEN", "a free-distribution failure must wait"


def test_dispatch_claims_exactly_one_incident_per_tick_and_holds_a_lease(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    queue_path = state / "self-heal" / "incident-queue.json"
    queue_path.write_text(json.dumps(_queue_with({
        "d" * 64: _older_reporting_incident(),
        "b" * 64: _blocking_revenue_incident(),
    })))

    first = _dispatch(state, tmp_path)
    second = _dispatch(state, tmp_path, observed_at="2026-08-07T08:05:00Z")

    assert first["fingerprint"] == "b" * 64
    assert second["fingerprint"] == "d" * 64
    assert first["lease_id"] != second["lease_id"]
    stored = json.loads(queue_path.read_text())["items"]
    assert [stored[key]["state"] for key in ("b" * 64, "d" * 64)] == ["CLAIMED", "CLAIMED"]
    assert stored["b" * 64]["lease_id"] == first["lease_id"]

    third = _dispatch(state, tmp_path, observed_at="2026-08-07T08:10:00Z")
    assert third["status"] == "NO_ACTIONABLE_INCIDENT"


# --------------------------------------------------------------------------
# 5. KNOWN route: a decision receipt, and no runbook execution
# --------------------------------------------------------------------------

def test_known_route_persists_a_runbook_decision_receipt_without_executing_the_runbook(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    sentinel = tmp_path / "runbook-was-executed"
    runbook_script = tmp_path / "fake_runbook.py"
    runbook_script.write_text(f"open({str(sentinel)!r}, 'w').write('x')\n")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema": "writer.self-heal.runbooks",
        "version": 1,
        "runbooks": [{
            "runbook_id": "content-rejection-plan-v1",
            "version": 3,
            "match": {"classification": "publisher-content-rejection"},
            "mode": "bounded-local-write",
            "command": ["python3", str(runbook_script), "--run-dir", "{run_dir}"],
        }],
    }))
    queue_path = state / "self-heal" / "incident-queue.json"
    queue_path.write_text(json.dumps(_queue_with({"b" * 64: _blocking_revenue_incident()})))

    decision = _dispatch(state, tmp_path, registry=registry)

    assert decision["route"] == "KNOWN"
    assert not sentinel.exists(), "Order 4 decides; Order 5 executes"
    receipt = json.loads(Path(decision["runbook_decision_receipt"]).read_text())
    assert receipt["schema"] == "writer.self-heal.runbook-decision"
    assert receipt["version"] == 1
    assert receipt["fingerprint"] == "b" * 64
    assert receipt["route"] == "KNOWN"
    assert receipt["runbook_id"] == "content-rejection-plan-v1"
    assert receipt["runbook_version"] == 3
    assert receipt["mode"] == "bounded-local-write"
    assert len(receipt["command_sha256"]) == 64
    assert receipt["next_action"] == "EXECUTE_BOUNDED_RUNBOOK"
    assert receipt["executed"] is False
    assert str(state / "runs" / "daily-2026-08-07") in " ".join(receipt["command"])
    assert Path(decision["runbook_decision_receipt"]).parent == state / "self-heal" / "runbook-decisions"

    # The decision must be reachable from the incident, exactly like an
    # investigation receipt is, and the lease must survive.
    stored = json.loads(queue_path.read_text())["items"]["b" * 64]
    assert stored["state"] == "CLAIMED"
    assert stored["lease_id"] == decision["lease_id"]
    assert stored["next_action"] == "EXECUTE_BOUNDED_RUNBOOK"
    assert stored["runbook_decision_receipt"]["runbook_id"] == "content-rejection-plan-v1"
    assert stored["runbook_decision_receipt"]["runbook_version"] == 3
    assert stored["runbook_decision_receipt"]["path"] == decision["runbook_decision_receipt"]


def test_runbook_decision_registration_refuses_a_receipt_claiming_execution(
    tmp_path: Path,
) -> None:
    """Order 4 may only register a decision; an `executed` receipt is Order 5's."""
    fingerprint = "a" * 64
    queue = {
        "schema": "writer.self-heal.incident-queue",
        "version": 1,
        "items": {fingerprint: {
            "fingerprint": fingerprint, "state": "CLAIMED", "lease_id": "lease-1",
        }},
    }
    receipt_path = tmp_path / "decision.json"
    receipt_path.write_text(json.dumps({
        "schema": "writer.self-heal.runbook-decision",
        "version": 1,
        "fingerprint": fingerprint,
        "route": "KNOWN",
        "runbook_id": "report-refresh-v1",
        "command_sha256": "e" * 64,
        "executed": True,
        "next_action": "EXECUTE_BOUNDED_RUNBOOK",
    }))

    try:
        QUEUE.register_runbook_decision(
            queue, fingerprint, "lease-1", receipt_path, "2026-08-07T09:00:00Z"
        )
    except ValueError as error:
        assert "unexecuted" in str(error)
    else:  # pragma: no cover - the guard must reject this receipt
        raise AssertionError("an executed runbook receipt must be refused")


# --------------------------------------------------------------------------
# 6. UNKNOWN route: Terra, never Sol
# --------------------------------------------------------------------------

def test_unknown_route_investigates_with_terra_and_never_sol(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    calls = tmp_path / "model-calls"
    _fake_model_runner(tmp_path / "runtime" / "model-runner.sh", calls)
    queue_path = state / "self-heal" / "incident-queue.json"
    queue_path.write_text(json.dumps(_queue_with({"b" * 64: _blocking_revenue_incident()})))

    decision = _dispatch(state, tmp_path)

    assert decision["route"] == "UNKNOWN"
    investigation = json.loads(Path(decision["investigation_receipt"]).read_text())
    assert investigation["schema"] == "writer.self-heal.unknown-investigation"
    assert investigation["run_id"] == "daily-2026-08-07"
    assert investigation["fingerprint"] == "b" * 64

    stored = json.loads(queue_path.read_text())["items"]["b" * 64]
    assert stored["investigation_receipt"]["path"] == decision["investigation_receipt"]

    observed = calls.read_text()
    assert "role=terra" in observed
    assert "provider=codex" in observed
    assert "sol=unset" in observed
    assert "sol-audit" not in observed
    # The read-only judge boundary, not agent mode: a five-minute repair tick
    # must not get `--sandbox danger-full-access` over $HOME.
    assert "argv=judge --prompt-file" in observed

    attempt = json.loads(Path(decision["repair_attempt_receipt"]).read_text())
    assert attempt["model"]["role"] == "terra"
    assert attempt["model"]["provider"] == "codex"
    assert attempt["model"]["name"] == "gpt-5.6-terra"
    assert attempt["model"]["status"] == "COMPLETED"


def test_model_defers_only_when_publication_can_actually_advance(
    tmp_path: Path,
) -> None:
    """A tick that is really about to publish must not buy model time first.

    The routed incident here owns no destination circuit, so publication can
    still advance and the Terra call waits. The receipt must record the
    predicate's inputs so the deferral is auditable rather than asserted.
    """
    state = _seed_state(tmp_path)
    calls = tmp_path / "model-calls"
    _fake_model_runner(tmp_path / "runtime" / "model-runner.sh", calls)
    queue_path = state / "self-heal" / "incident-queue.json"
    # An UNKNOWN route that would otherwise call Terra, but it owns no
    # destination circuit, so note/ja's open circuit is not its to break.
    queue_path.write_text(json.dumps(_queue_with({"e" * 64: _metrics_incident()})))

    decision = _dispatch(state, tmp_path, publication_backlog="1")

    assert decision["route"] == "UNKNOWN"
    assert not calls.exists(), "no model may run while publication can still advance"
    attempt = json.loads(Path(decision["repair_attempt_receipt"]).read_text())
    assert attempt["model"]["status"] == "DEFERRED_PUBLICATION_PRIORITY"
    assert attempt["model"]["reason"]
    assert attempt["model"]["role"] == "terra"
    assert "return_code" not in attempt["model"]
    assert attempt["latency_ms"]["model"]["status"] == "unknown"
    progress = attempt["publication_progress"]
    assert progress["backlog"] is True
    assert progress["routed_incident_blocks_publication"] is False
    assert progress["decision"] == "DEFER_MODEL"


def test_model_runs_when_the_routed_incident_is_itself_the_publication_blocker(
    tmp_path: Path,
) -> None:
    """The livelock breaker.

    note/ja is blocked by its own open circuit, so publication stays pending,
    so backlog is 1 on every tick. Deferring on backlog alone means Terra never
    investigates, nothing diagnoses the 422, the circuit never opens, and the
    loop reports health while doing zero repair. When the incident being routed
    owns the circuit that blocks publication, there is no publication progress
    to protect and the investigation must run.
    """
    state = _seed_state(tmp_path)
    calls = tmp_path / "model-calls"
    _fake_model_runner(tmp_path / "runtime" / "model-runner.sh", calls)
    queue_path = state / "self-heal" / "incident-queue.json"
    queue_path.write_text(json.dumps(_queue_with({"b" * 64: _blocking_revenue_incident()})))

    decision = _dispatch(state, tmp_path, publication_backlog="1")

    assert decision["route"] == "UNKNOWN"
    assert calls.exists(), "the circuit owner must be investigated, not deferred"
    observed = calls.read_text()
    assert "role=terra" in observed
    assert "provider=codex" in observed
    assert "sol=unset" in observed

    attempt = json.loads(Path(decision["repair_attempt_receipt"]).read_text())
    assert attempt["model"]["status"] == "COMPLETED"
    progress = attempt["publication_progress"]
    assert progress["backlog"] is True
    assert progress["routed_incident_blocks_publication"] is True
    assert progress["decision"] == "RUN_MODEL"
    assert len(progress["circuit_receipt"]["sha256"]) == 64

    # The deterministic half still lands in full.
    stored = json.loads(queue_path.read_text())["items"]["b" * 64]
    assert stored["state"] == "CLAIMED"
    assert stored["lease_id"] == decision["lease_id"]
    assert stored["investigation_receipt"]["path"] == decision["investigation_receipt"]


def test_foreground_publication_defers_even_a_circuit_owner(tmp_path: Path) -> None:
    import importlib.util as _util

    spec = _util.spec_from_file_location("writer_repair_dispatch", DISPATCH)
    assert spec and spec.loader
    module = _util.module_from_spec(spec)
    spec.loader.exec_module(module)

    progress = module.publication_progress(
        {"run_id": "daily-2026-08-07", "destination": "note/ja"},
        tmp_path,
        True,
        defer_model_always=True,
    )

    assert progress["decision"] == "DEFER_MODEL"
    assert progress["reason"] == module.PUBLICATION_PRIORITY_REASON
    assert progress["circuit_receipt"]["status"] == "deferred"


def test_terra_budget_leaves_the_publication_tick_room_by_default(tmp_path: Path) -> None:
    """The bound must be a fraction of the 300s launchd interval, not most of it."""
    import importlib.util as _util
    spec = _util.spec_from_file_location("writer_repair_dispatch", DISPATCH)
    module = _util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.DEFAULT_TERRA_TIMEOUT_SECONDS <= 120
    assert module.DEFAULT_TERRA_TIMEOUT_SECONDS >= 90, (
        "must still exceed the 84.8s measured judge latency"
    )


# --------------------------------------------------------------------------
# 7. The repair-attempt receipt
# --------------------------------------------------------------------------

def test_repair_attempt_receipt_records_unknown_tokens_and_cost_rather_than_zero(
    tmp_path: Path,
) -> None:
    state = _seed_state(tmp_path)
    queue_path = state / "self-heal" / "incident-queue.json"
    queue_path.write_text(json.dumps(_queue_with({"b" * 64: _blocking_revenue_incident()})))

    decision = _dispatch(state, tmp_path)
    attempt = json.loads(Path(decision["repair_attempt_receipt"]).read_text())

    assert attempt["schema"] == "writer.self-heal.repair-attempt"
    assert attempt["version"] == 1
    assert attempt["fingerprint"] == "b" * 64
    assert attempt["lease_id"] == decision["lease_id"]
    assert attempt["route"] == "UNKNOWN"
    assert attempt["run_id"] == "daily-2026-08-07"
    assert attempt["destination"] == "note/ja"
    assert attempt["revenue_role"] == "revenue-set"
    assert attempt["blocking"] is True
    assert attempt["failure_class"] == "publisher-content-rejection"
    assert attempt["runbook"] == {
        "status": "unknown",
        "reason": "no runbook matched this failure class; route is UNKNOWN",
    }
    assert attempt["tool"]["path"] == str(ROOT / "scripts" / "writer_unknown_investigation.py")
    assert len(attempt["tool"]["sha256"]) == 64
    assert isinstance(attempt["latency_ms"]["total"], int)
    assert attempt["latency_ms"]["total"] >= 0
    # SSOT §9.4 C1 changed what is genuinely measurable here: the codex
    # `--json` stream carries a `turn.completed` usage block, so tokens are now
    # read from the run instead of declared unknown. Cost still has no price
    # table joined to it, so it stays unknown with a reason. The invariant the
    # receipt has always kept is unchanged: nothing unmeasured is reported as a
    # number, and nothing measured is reported as unknown.
    assert attempt["tokens"]["status"] == "measured"
    assert attempt["tokens"]["source"] == "turn.completed"
    assert attempt["tokens"]["input_tokens"] == 1
    assert attempt["tokens"]["output_tokens"] == 1
    assert attempt["cost"]["status"] == "unknown"
    assert attempt["cost"]["reason"]
    # A missing measurement must never be reported as a measured zero.
    assert "value" not in attempt["tokens"]
    assert "value" not in attempt["cost"]


# --------------------------------------------------------------------------
# 8. Installed loop wiring
# --------------------------------------------------------------------------

def test_resume_loop_dispatches_repair_routing_after_the_incident_bridge(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "writer-agent"
    state_dir = tmp_path / "state"
    scripts = fake_root / "scripts"
    runtime = fake_root / "runtime"
    (state_dir / "runs").mkdir(parents=True)
    scripts.mkdir(parents=True)
    (scripts / "_shared").mkdir()
    runtime.mkdir()
    _copy_resume_runtime(scripts)
    shutil.copy(ROOT / "scripts" / "_shared" / "notifier.sh", scripts / "_shared")
    (scripts / "article_daily_start_control.py").write_text(
        'print(\'{"action":"skip-pending-worker"}\')\n'
    )
    for name in ("quality_feedback_recovery.py", "quality_repair_control.py"):
        (scripts / name).write_text('print(\'{"status":"NONE"}\')\n')
    (scripts / "recover-known-unavailable.py").write_text("raise SystemExit(0)\n")
    calls = tmp_path / "calls"
    (scripts / "writer_unavailable_incident_bridge.py").write_text(
        f"open({str(calls)!r}, 'a').write('bridge\\n')\n"
    )
    (scripts / "writer_repair_dispatch.py").write_text(
        "import sys\n"
        f"open({str(calls)!r}, 'a').write('dispatch ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    )
    (scripts / "article_pending.py").write_text(
        'print(\'{"status":"BLOCKED","reason":"test"}\')\n'
    )
    (runtime / "model-runner.sh").write_text("#!/usr/bin/env bash\nexit 91\n")
    (runtime / "model-runner.sh").chmod(0o755)
    (runtime / "model-runner-support.py").write_text("raise SystemExit(0)\n")

    result = subprocess.run(
        ["bash", str(scripts / "article-resume-pending.sh")],
        env={
            **os.environ,
            "ARTICLE_ROOT": str(fake_root),
            "ARTICLE_STATE_DIR": str(state_dir),
            "LIFE_MANAGER_REPO": str(ROOT.parents[1]),
            "LIFE_MANAGER_ENV_FILE": str(tmp_path / "missing.env"),
            "ARTICLE_LOCAL_DATE": "2026-08-07",
            "ARTICLE_RESUME_LOG": str(tmp_path / "resume.log"),
            "ARTICLE_MODEL_RUNNER": str(runtime / "model-runner.sh"),
            "ARTICLE_MODEL_SUPPORT": str(runtime / "model-runner-support.py"),
            "ARTICLE_OWNER_FENCE_ACTIVE": "1",
            "ARTICLE_RESUME_MIN_FREE_BYTES": "536870912",
            "BASH_ENV": _fake_df_env(tmp_path),
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
        },
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert lines[0] == "bridge"
    assert lines[1].startswith("dispatch ")
    assert f"--state-root {state_dir}" in lines[1]
    assert f"--scripts {scripts}" in lines[1]
    assert f"--registry {fake_root}/config/repair-runbooks.json" in lines[1]
    assert f"--model-runner {runtime}/model-runner.sh" in lines[1]
    # No publication backlog on this tick, so the model step is allowed.
    assert "--publication-backlog 0" in lines[1]


def test_resume_loop_plans_publication_before_self_heal() -> None:
    """The Coconala-shaped foreground queue must precede repair routing."""
    worker = (ROOT / "scripts" / "article-resume-pending.sh").read_text(
        encoding="utf-8"
    )
    planner = worker.index('PLAN="$(python3 "$PLANNER"')
    dispatcher = worker.index(
        'python3 "$ARTICLE_ROOT/scripts/writer_repair_dispatch.py"'
    )
    release = worker.rindex("release_publication_lock || exit 1", planner, dispatcher)
    assert planner < dispatcher
    assert release < dispatcher
    assert '--publication-backlog "1"' in worker[dispatcher:]
    assert "--defer-model-always" in worker[dispatcher:]
    assert '--publication-backlog "0"' in worker[dispatcher:]
    assert "publication queue foreground; self-heal deferred" in worker


def test_resume_loop_older_backlog_does_not_suppress_new_daily_schedule(
    tmp_path: Path,
) -> None:
    """A prior-day repair must not turn the daily creator into a starvation queue."""
    fake_root = tmp_path / "writer-agent"
    state_dir = tmp_path / "state"
    scripts = fake_root / "scripts"
    (state_dir / "runs").mkdir(parents=True)
    scripts.mkdir(parents=True)
    _copy_resume_runtime(scripts)
    marker = tmp_path / "daily-started"
    (fake_root / "article-daily.sh").write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' daily > {str(marker)!r}\n"
    )
    (fake_root / "article-daily.sh").chmod(0o755)
    (scripts / "article_daily_start_control.py").write_text(
        "print('{\"action\":\"new\",\"reason\":\"no-same-jst-day-run\"}')\n"
    )
    (scripts / "article_pending.py").write_text(
        "import json\n"
        "print(json.dumps({'status':'READY','run_id':'daily-2026-08-07',"
        "'eligible_pairs':['substack/ja'],'initialization_pairs':[],"
        "'recovery_pairs':[]}))\n"
    )
    log = tmp_path / "resume.log"
    result = subprocess.run(
        ["bash", str(scripts / "article-resume-pending.sh")],
        env={
            **os.environ,
            "ARTICLE_ROOT": str(fake_root),
            "ARTICLE_STATE_DIR": str(state_dir),
            "LIFE_MANAGER_REPO": str(ROOT.parents[1]),
            "LIFE_MANAGER_ENV_FILE": str(tmp_path / "missing.env"),
            "ARTICLE_OWNER_FENCE_ACTIVE": "1",
            "ARTICLE_LOCAL_DATE": "2026-08-21",
            "ARTICLE_LOCAL_HOUR": "06",
            "ARTICLE_DAILY_SCHEDULE_HOUR": "6",
            "ARTICLE_RESUME_MIN_FREE_BYTES": "536870912",
            "BASH_ENV": _fake_df_env(tmp_path),
            "ARTICLE_RESUME_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text().strip() == "daily"
    assert "older publication backlog does not block daily schedule" in log.read_text()


def test_resume_loop_future_or_unknown_backlog_still_suppresses_new_daily(
    tmp_path: Path,
) -> None:
    """Future/invalid identities fail closed instead of creating a duplicate run."""
    for index, run_id in enumerate(
        ("daily-2026-08-22", "daily-not-a-date", "20260807-abcdef")
    ):
        case = tmp_path / str(index)
        fake_root = case / "writer-agent"
        state_dir = case / "state"
        scripts = fake_root / "scripts"
        (state_dir / "runs").mkdir(parents=True)
        scripts.mkdir(parents=True)
        _copy_resume_runtime(scripts)
        marker = case / "daily-started"
        (fake_root / "article-daily.sh").write_text(
            f"#!/usr/bin/env bash\nprintf '%s\\n' daily > {str(marker)!r}\n"
        )
        (fake_root / "article-daily.sh").chmod(0o755)
        (scripts / "article_daily_start_control.py").write_text(
            "print('{\"action\":\"new\"}')\n"
        )
        counter = case / "planner-calls"
        (scripts / "article_pending.py").write_text(
            "import json, pathlib\n"
            f"counter = pathlib.Path({str(counter)!r})\n"
            "n = int(counter.read_text()) if counter.exists() else 0\n"
            "counter.write_text(str(n + 1))\n"
            f"print(json.dumps({{'status':'READY','run_id':{run_id!r},"
            "'eligible_pairs':['substack/ja'],'initialization_pairs':[],"
            "'recovery_pairs':[]}) if n == 0 else "
            "json.dumps({'status':'BLOCKED','reason':'test'}))\n"
        )
        result = subprocess.run(
            ["bash", str(scripts / "article-resume-pending.sh")],
            env={
                **os.environ,
                "ARTICLE_ROOT": str(fake_root),
                "ARTICLE_STATE_DIR": str(state_dir),
                "LIFE_MANAGER_REPO": str(ROOT.parents[1]),
                "LIFE_MANAGER_ENV_FILE": str(case / "missing.env"),
                "ARTICLE_OWNER_FENCE_ACTIVE": "1",
                "ARTICLE_LOCAL_DATE": "2026-08-21",
                "ARTICLE_LOCAL_HOUR": "06",
                "ARTICLE_DAILY_SCHEDULE_HOUR": "6",
                "ARTICLE_RESUME_MIN_FREE_BYTES": "536870912",
                "BASH_ENV": _fake_df_env(case),
                "ARTICLE_RESUME_LOG": str(case / "resume.log"),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not marker.exists()
