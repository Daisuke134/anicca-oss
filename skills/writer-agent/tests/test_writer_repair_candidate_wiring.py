#!/usr/bin/env python3
"""Executable contract for wiring the H2 repair channel into production.

SSOT §9.3.1 item H2 built a bounded, write-capable repair channel and left it
called from nothing.  This file specifies what "wired" means, and it specifies
the placement decision as a property rather than as a comment:

* a repair may never delay publication, so it runs under its own launchd label
  and never touches the publication lock -- asserted by running the worker while
  that lock is held by a live process;
* R6 still holds, so the new label is neither the one daily creator nor the one
  same-run recovery owner -- asserted by a census over every plist in the tree;
* two workers can never work the same incident, and the mechanism is the
  incident queue's own exclusive lock plus the existing lease, not a second one;
* an incident that is merely `CLAIMED` for some other reason is left untouched,
  byte for byte.

Every test runs against a throwaway git repository, a throwaway state root and a
fake `codex` binary.  No live state, no live tree, and no provider is touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUNNER = ROOT / "runtime" / "model-runner.sh"
DRIVER = SCRIPTS / "article-repair-candidate.sh"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


wiring = _module("writer_repair_candidate_dispatch")
incidents = _module("writer_incident_queue")
candidate = _module("writer_repair_candidate")

FINGERPRINT = "b4" + "9" * 62
OTHER_FINGERPRINT = "32" + "1" * 62
LEASE = "repair-13fb88fe834b462a91400ee97449befb"
OBSERVED_AT = "2026-08-07T12:00:00Z"

LAST_MESSAGE = json.dumps({
    "changed_paths": ["skills/writer-agent/scripts/publish.py"],
    "rationale": "normalise the body before the publisher sees it",
    "sources_used": [],
    "regression_test_path": "skills/writer-agent/tests/test_repair_regression.py",
    "complete": True,
    "remaining_work": None,
})


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    source = tmp_path / "repo"
    (source / "skills" / "writer-agent" / "tests").mkdir(parents=True)
    (source / "skills" / "writer-agent" / "scripts").mkdir(parents=True)
    (source / "skills" / "writer-agent" / "scripts" / "publish.py").write_text(
        "BODY = 'original'\n", encoding="utf-8",
    )
    # A tracked file, so `git worktree add` actually creates the tests directory
    # and the channel's own default gate -- the real pytest command, not a stub
    # -- has something to collect.
    (source / "skills" / "writer-agent" / "tests" / "test_baseline.py").write_text(
        "def test_baseline():\n    assert True\n", encoding="utf-8",
    )
    _git(source, "init", "-q", "-b", "main", ".")
    _git(source, "config", "user.email", "repair@example.invalid")
    _git(source, "config", "user.name", "repair")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "base")
    return source


def _investigation(state_root: Path, fingerprint: str, *, complete: bool) -> None:
    sessions = state_root / "self-heal" / "investigation-sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{fingerprint}.json").write_text(
        json.dumps({
            "schema": "writer.self-heal.investigation-session",
            "version": 1,
            "fingerprint": fingerprint,
            "status": "COMPLETE" if complete else "CHECKPOINT",
            "slice_count": 1,
            "max_slices": 3,
            "trigger": {"occurrence_count": 1, "deployed_commit": "a" * 40},
            "verdict": {
                "cause_status": "UNDETERMINED",
                "complete": True,
                "evidence_gaps": ["official_primary_document_research_required"],
                "findings": [],
                "primary_sources": [],
                "remaining_work": "fetch note's current terms and posting policy",
            } if complete else None,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _queue(state_root: Path, items: dict) -> Path:
    path = state_root / "self-heal" / "incident-queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema": incidents.SCHEMA, "version": 1,
            "updated_at": OBSERVED_AT, "items": items,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _repair_ready_item(fingerprint: str = FINGERPRINT) -> dict:
    """The live `note/ja` shape: CLAIMED as a deliberate handoff to this stage."""
    return {
        "fingerprint": fingerprint,
        "state": "CLAIMED",
        "lease_id": LEASE,
        "destination": "note/ja",
        "run_id": "daily-2026-08-07",
        "revenue_role": "revenue-set",
        "blocking": True,
        "first_seen_at": "2026-08-07T05:27:19Z",
        "occurrence_count": 1,
        "code_rearm_rounds": 1,
        "next_action": "COLLECT_GAPS_THEN_SEARCH_OFFICIAL_PRIMARY_DOCS",
    }


def _merely_claimed_item(fingerprint: str = OTHER_FINGERPRINT) -> dict:
    """The live `32446a38` shape: CLAIMED with no investigation at all."""
    return {
        "fingerprint": fingerprint,
        "state": "CLAIMED",
        "lease_id": "production-repair-20260806-note-s3",
        "first_seen_at": "2026-08-06T00:00:00Z",
        "occurrence_count": 1,
        "next_action": "RUNBOOK_OR_INVESTIGATE",
    }


def _fake_codex(path: Path, body: str) -> Path:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'printf "%s\\n" "$@" >>"$ARTICLE_CAPTURE_ARGS"\n'
        "cat >/dev/null\n"
        'printf \'{"type":"thread.started","thread_id":"probe-thread"}\\n\'\n'
        + body
        + '\nprintf \'{"type":"turn.completed","usage":{"input_tokens":11,'
        '"output_tokens":7}}\\n\'\n'
        'printf \'%s\' "$ARTICLE_LAST_MESSAGE" >"$ARTICLE_CODEX_LAST_MESSAGE_FILE"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


REPAIRING_CODEX = (
    'printf "BODY = %s\\n" "\'repaired\'" '
    '>"$ARTICLE_REPAIR_WORKSPACE/skills/writer-agent/scripts/publish.py"\n'
    'printf "def test_ok():\\n    assert True\\n" '
    '>"$ARTICLE_REPAIR_WORKSPACE/skills/writer-agent/tests/test_repair_regression.py"\n'
)
BREAKING_CODEX = (
    'printf "BODY = %s\\n" "\'broken\'" '
    '>"$ARTICLE_REPAIR_WORKSPACE/skills/writer-agent/scripts/publish.py"\n'
)


def _environment(fake_codex: Path, capture: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "ARTICLE_PROVIDER": "codex",
        "ARTICLE_CODEX_BIN": str(fake_codex),
        "ARTICLE_CAPTURE_ARGS": str(capture),
        "ARTICLE_LAST_MESSAGE": LAST_MESSAGE,
    })
    return environment


# The channel refuses a handoff whose sources cannot be read, so a wiring test
# must supply one.  It is injected rather than fetched: these tests assert
# placement and concurrency, and they must never depend on note.com being up.
TEST_REGISTRY = {
    "schema": "writer.self-heal.repair-source-registry",
    "version": 1,
    "destinations": {
        "note/ja": {
            "sources": [{
                "url": "https://terms.example.invalid/terms",
                "title": "note ご利用規約", "role": "binding-contract",
            }],
        },
    },
}


def _offline_get(url: str, *, timeout: int, max_bytes: int) -> dict:
    return {"http_status": 200, "content_type": "text/html", "body": b"terms"}


def _run(
    repo: Path, tmp_path: Path, *, codex_body: str = REPAIRING_CODEX,
    observed_at: str = OBSERVED_AT, test_commands: list[list[str]] | None = None,
    max_attempts: int = candidate.DEFAULT_MAX_CANDIDATE_ATTEMPTS,
    suffix: str = "",
) -> dict:
    fake = _fake_codex(tmp_path / f"codex{suffix}", codex_body)
    return wiring.dispatch(
        state_root=tmp_path / "state",
        repo=repo,
        base_ref="main",
        repair_root=tmp_path / "repairs",
        model_runner=RUNNER,
        observed_at=observed_at,
        budget_seconds=120,
        max_attempts=max_attempts,
        test_commands=test_commands or [["true"]],
        environment=_environment(fake, tmp_path / f"args{suffix}.txt"),
        source_registry=TEST_REGISTRY,
        getter=_offline_get,
    )


# ---------------------------------------------------------------------------
# RED 1 -- repair work cannot delay publication
# ---------------------------------------------------------------------------

def test_the_repair_worker_never_touches_the_publication_lock(
    repo: Path, tmp_path: Path,
) -> None:
    """A 900-second repair inside a 300-second lock-holding tick would starve
    publication. The worker therefore runs somewhere that cannot hold that lock,
    and this proves it behaviourally: the lock is held by a live process for the
    whole run, and the repair still completes and still registers its candidate.
    """
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    queue_path = _queue(state_root, {FINGERPRINT: _repair_ready_item()})

    # The publication lock, held by a live process, exactly as
    # article-resume-pending.sh takes it.
    lock_dir = state_root / ".article-daily.lockdir"
    lock_dir.mkdir(parents=True)
    holder = subprocess.Popen(["sleep", "120"])
    try:
        (lock_dir / "owner.pid").write_text(f"{holder.pid}\n", encoding="utf-8")
        before = (lock_dir / "owner.pid").read_bytes()
        outcome = _run(repo, tmp_path)
    finally:
        holder.kill()
        holder.wait()

    assert outcome["status"] == "CANDIDATE_VERIFIED", outcome
    # the lock was neither taken, moved, nor rewritten
    assert (lock_dir / "owner.pid").read_bytes() == before
    assert sorted(path.name for path in lock_dir.iterdir()) == ["owner.pid"]

    item = json.loads(queue_path.read_text(encoding="utf-8"))["items"][FINGERPRINT]
    assert item["next_action"] == "VERIFY_SENSITIVE_REPAIR_IN_ISOLATED_FIXTURE"
    assert item["candidate_receipt"]["status"] == "CANDIDATE_VERIFIED"


def test_the_repair_driver_contains_no_publication_or_creation_path() -> None:
    """Placement is enforced by what the driver may not name."""
    body = DRIVER.read_text(encoding="utf-8")
    for forbidden in (
        ".article-daily.lockdir",      # the publication lock
        "article-daily.sh",            # the daily creator
        "article-resume-pending.sh",   # the same-run recovery owner
        "article_pending.py",          # the publication planner
        "publication-guard.py",        # the publication contract guard
        "publication_contract",        # the publication contract resolver
        "note-publish",                # every publisher adapter directory
        "zenn-publish",
        "devto-publish",
        "substack-publish",
        "x-publish",
        "x-post",
        ".openclaw/.env",              # the runtime credential file
    ):
        assert forbidden not in body, forbidden


def test_the_resume_tick_still_never_calls_the_repair_channel() -> None:
    """The 300-second tick that holds the publication lock is left unchanged."""
    body = (SCRIPTS / "article-resume-pending.sh").read_text(encoding="utf-8")
    assert "writer_repair_candidate" not in body
    assert "article-repair-candidate" not in body


# ---------------------------------------------------------------------------
# RED 2 -- R6: one daily creator, one same-run recovery owner
# ---------------------------------------------------------------------------

def test_r6_creator_and_recovery_owners_come_only_from_registry() -> None:
    registry = json.loads((ROOT.parents[1] / "config/loop-registry.json").read_text())
    creators = [key for key, row in registry["loops"].items()
                if row["entrypoint"].endswith("article-daily.sh")]
    recovery_owners = [key for key, row in registry["loops"].items()
                       if row["entrypoint"].endswith("article-resume-pending.sh")]
    assert creators == ["article-daily"], creators
    assert recovery_owners == ["article-resume"], recovery_owners


def test_a_verified_candidate_is_never_deployed_published_or_resolved(
    repo: Path, tmp_path: Path,
) -> None:
    """Order 5 is out of scope, and the receipt says so in a machine-checked way."""
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    queue_path = _queue(state_root, {FINGERPRINT: _repair_ready_item()})

    outcome = _run(repo, tmp_path)
    receipt = json.loads(
        Path(outcome["candidate_receipt"]).read_text(encoding="utf-8")
    )
    assert receipt["invariants"] == {
        "draft_is_public": False, "incident_resolved": False, "deployed": False,
    }
    assert receipt["deployed"] is False
    assert receipt["published"] is False
    assert receipt["pushed"] is False
    item = json.loads(queue_path.read_text(encoding="utf-8"))["items"][FINGERPRINT]
    assert item["state"] == "CLAIMED"          # not RESOLVED
    assert "effect_receipt" not in item        # nothing was deployed or resumed
    # the source repository is exactly where it started
    assert _git(repo, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# RED 3 -- selection: repair-ready is picked up, merely CLAIMED is left alone
# ---------------------------------------------------------------------------

def test_a_repair_ready_incident_is_picked_up_without_anyone_invoking_it(
    repo: Path, tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    queue_path = _queue(state_root, {
        OTHER_FINGERPRINT: _merely_claimed_item(),
        FINGERPRINT: _repair_ready_item(),
    })

    outcome = _run(repo, tmp_path)

    assert outcome["status"] == "CANDIDATE_VERIFIED"
    assert outcome["fingerprint"] == FINGERPRINT
    assert outcome["lease_id"] == LEASE          # the handoff lease, not a new one
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert queue["items"][FINGERPRINT]["candidate_receipt"]["status"] == (
        "CANDIDATE_VERIFIED"
    )


def test_a_claimed_incident_without_a_completed_verdict_is_left_untouched(
    repo: Path, tmp_path: Path,
) -> None:
    """The live `32446a38` is CLAIMED for a different reason. Byte-for-byte."""
    state_root = tmp_path / "state"
    queue_path = _queue(state_root, {OTHER_FINGERPRINT: _merely_claimed_item()})
    before = queue_path.read_bytes()

    outcome = _run(repo, tmp_path)

    assert outcome["status"] == "NO_REPAIR_READY_INCIDENT"
    assert queue_path.read_bytes() == before


def test_an_incomplete_investigation_is_not_repair_ready(
    repo: Path, tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=False)
    queue_path = _queue(state_root, {FINGERPRINT: _repair_ready_item()})
    before = queue_path.read_bytes()

    assert _run(repo, tmp_path)["status"] == "NO_REPAIR_READY_INCIDENT"
    assert queue_path.read_bytes() == before


def test_an_incident_that_already_has_a_verified_candidate_is_not_reworked(
    repo: Path, tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    item = _repair_ready_item()
    item["next_action"] = "VERIFY_SENSITIVE_REPAIR_IN_ISOLATED_FIXTURE"
    item["candidate_receipt"] = {"status": "CANDIDATE_VERIFIED", "path": "x"}
    queue_path = _queue(state_root, {FINGERPRINT: item})
    before = queue_path.read_bytes()

    assert _run(repo, tmp_path)["status"] == "NO_REPAIR_READY_INCIDENT"
    assert queue_path.read_bytes() == before


# ---------------------------------------------------------------------------
# RED 4 -- two workers can never work the same incident
# ---------------------------------------------------------------------------

def test_two_concurrent_workers_cannot_work_the_same_incident(
    repo: Path, tmp_path: Path,
) -> None:
    """The mechanism is the incident queue's own exclusive lock plus the lease
    that already exists. Nothing parallel is built beside them."""
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    _queue(state_root, {FINGERPRINT: _repair_ready_item()})

    fake = _fake_codex(tmp_path / "codex", REPAIRING_CODEX)
    driver = (
        "import importlib.util, json, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(SCRIPTS / 'writer_repair_candidate_dispatch.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "from pathlib import Path\n"
        "print(json.dumps(m.dispatch(\n"
        f"  state_root=Path({str(state_root)!r}), repo=Path({str(repo)!r}),\n"
        "  base_ref='main',\n"
        f"  repair_root=Path({str(tmp_path / 'repairs')!r}),\n"
        f"  model_runner=Path({str(RUNNER)!r}),\n"
        "  observed_at=sys.argv[1], budget_seconds=120,\n"
        "  test_commands=[['sleep', '2']],\n"
        "  environment=dict(os.environ),\n"
        ")))\n"
    )
    script = tmp_path / "worker.py"
    script.write_text(driver, encoding="utf-8")
    environment = _environment(fake, tmp_path / "args.txt")

    processes = [
        subprocess.Popen(
            ["python3", str(script), f"2026-08-07T12:0{index}:00Z"],
            env=environment, stdout=subprocess.PIPE, text=True,
        )
        for index in range(2)
    ]
    outputs = [process.communicate()[0] for process in processes]
    statuses = sorted(
        json.loads(output.strip().splitlines()[-1])["status"] for output in outputs
    )

    # Exactly one worked it; the other found nothing repair-ready.
    assert statuses == ["CANDIDATE_VERIFIED", "NO_REPAIR_READY_INCIDENT"], outputs
    # and only one attempt was ever charged against the bounded budget
    checkpoint = candidate.read_checkpoint(state_root, FINGERPRINT)
    assert checkpoint["attempts"] == 1


def test_an_in_flight_incident_is_invisible_to_the_next_worker(
    repo: Path, tmp_path: Path,
) -> None:
    """The in-flight marker lives in the queue, under the queue's own lock."""
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    item = _repair_ready_item()
    item["next_action"] = wiring.IN_PROGRESS
    _queue(state_root, {FINGERPRINT: item})

    assert _run(repo, tmp_path)["status"] == "NO_REPAIR_READY_INCIDENT"


# ---------------------------------------------------------------------------
# RED 5 -- discard records why; exhaustion stops and waits for a new trigger
# ---------------------------------------------------------------------------

def test_a_discarded_candidate_records_why_and_stays_repair_ready(
    repo: Path, tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    queue_path = _queue(state_root, {FINGERPRINT: _repair_ready_item()})

    outcome = _run(
        repo, tmp_path, codex_body=BREAKING_CODEX, test_commands=[["false"]],
    )

    assert outcome["status"] == "CANDIDATE_DISCARDED"
    assert outcome["failed_check"] == "test_gate"
    item = json.loads(queue_path.read_text(encoding="utf-8"))["items"][FINGERPRINT]
    assert item["state"] == "CLAIMED"
    assert item["lease_id"] == LEASE
    # released back to the repair-ready set so the bound, not a stuck marker,
    # is what stops it
    assert item["next_action"] == "COLLECT_GAPS_THEN_SEARCH_OFFICIAL_PRIMARY_DOCS"
    assert item["repair_candidate_last_discard"]["failed_check"] == "test_gate"
    assert "exited" in item["repair_candidate_last_discard"]["reason"]
    assert "candidate_receipt" not in item


def test_exhausting_the_bound_stops_the_incident_until_a_new_trigger(
    repo: Path, tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    queue_path = _queue(state_root, {FINGERPRINT: _repair_ready_item()})

    for index in range(candidate.DEFAULT_MAX_CANDIDATE_ATTEMPTS):
        outcome = _run(
            repo, tmp_path, codex_body=BREAKING_CODEX, test_commands=[["false"]],
            observed_at=f"2026-08-07T12:0{index}:00Z", suffix=str(index),
        )
        assert outcome["status"] == "CANDIDATE_DISCARDED", outcome

    spent = _run(
        repo, tmp_path, codex_body=BREAKING_CODEX, test_commands=[["false"]],
        observed_at="2026-08-07T12:09:00Z", suffix="x",
    )
    assert spent["status"] == "REPAIR_BUDGET_EXHAUSTED"
    item = json.loads(queue_path.read_text(encoding="utf-8"))["items"][FINGERPRINT]
    assert item["state"] == "FAILED"
    assert item["next_action"] == "ESCALATE_AFTER_REPAIR_CANDIDATE_BUDGET_EXHAUSTED"
    assert item["exhausted"]["kind"] == "repair-candidates"
    assert "lease_id" not in item

    # FAILED sits outside every selector, so it stops consuming ticks entirely.
    assert _run(repo, tmp_path, suffix="y")["status"] == "NO_REPAIR_READY_INCIDENT"

    # A genuinely new trigger -- a deployed code version -- revives it through
    # the queue's existing re-arm, not through anything this wiring invents.
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    rearmed = incidents.rearm_on_new_code(queue, "c" * 40, "2026-08-07T13:00:00Z")
    assert [row["fingerprint"] for row in rearmed] == [FINGERPRINT]
    assert queue["items"][FINGERPRINT]["state"] == "OPEN"


def test_repeated_preparation_failures_are_bounded_and_never_spin_forever(
    repo: Path, tmp_path: Path,
) -> None:
    """A repair that cannot even start must degrade, exactly like routing does."""
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    queue_path = _queue(state_root, {FINGERPRINT: _repair_ready_item()})
    # A dirty source repository makes `prepare` refuse, every time.
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    statuses = []
    for index in range(wiring.DEFAULT_MAX_PREPARATION_FAILURES):
        statuses.append(_run(
            repo, tmp_path, observed_at=f"2026-08-07T12:1{index}:00Z",
            suffix=f"p{index}",
        )["status"])

    assert statuses[:-1] == ["REPAIR_FAILED"] * (
        wiring.DEFAULT_MAX_PREPARATION_FAILURES - 1
    ), statuses
    assert statuses[-1] == "REPAIR_PREPARATION_EXHAUSTED"
    item = json.loads(queue_path.read_text(encoding="utf-8"))["items"][FINGERPRINT]
    assert item["state"] == "FAILED"
    assert item["exhausted"]["kind"] == "repair-candidates"


# ---------------------------------------------------------------------------
# RED 6 -- the production path may not weaken the channel's own gate or budget
# ---------------------------------------------------------------------------

def test_production_cannot_override_the_test_gate_or_the_budget() -> None:
    """`test_commands` is a test seam on the function, never a production input."""
    parser_source = Path(wiring.__file__).read_text(encoding="utf-8")
    assert "--test-command" not in parser_source
    assert "ARTICLE_REPAIR_TEST_COMMANDS" not in parser_source
    driver = DRIVER.read_text(encoding="utf-8")
    assert "--test-command" not in driver
    assert "--max-attempts" not in driver
    # the wiring does not invent a second budget: it defaults to the channel's
    assert wiring.DEFAULT_MAX_CANDIDATE_ATTEMPTS == (
        candidate.DEFAULT_MAX_CANDIDATE_ATTEMPTS
    )
    assert wiring.DEFAULT_BUDGET_SECONDS == candidate.DEFAULT_BUDGET_SECONDS
    assert "DEFAULT_MAX_CANDIDATE_ATTEMPTS = candidate." in parser_source
    assert "DEFAULT_BUDGET_SECONDS = candidate." in parser_source


def test_the_trigger_token_is_the_investigation_paths_own(
    repo: Path, tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    _investigation(state_root, FINGERPRINT, complete=True)
    _queue(state_root, {FINGERPRINT: _repair_ready_item()})
    (state_root / "deployed-commit").write_text("a" * 40, encoding="utf-8")

    outcome = _run(repo, tmp_path)
    attempt = json.loads(
        Path(outcome["repair_attempt_receipt"]).read_text(encoding="utf-8")
    )
    session = _module("writer_repair_investigation_session")
    assert attempt["trigger"] == session.trigger_token(1, "a" * 40)


# ---------------------------------------------------------------------------
# RED 7 -- the installed driver runs the whole thing with no person involved
# ---------------------------------------------------------------------------

def test_the_launchd_driver_runs_the_repair_end_to_end(
    repo: Path, tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    (state_root / "runs").mkdir(parents=True)
    _investigation(state_root, FINGERPRINT, complete=True)
    queue_path = _queue(state_root, {FINGERPRINT: _repair_ready_item()})
    fake = _fake_codex(tmp_path / "codex", REPAIRING_CODEX)
    log = tmp_path / "repair.log"

    environment = _environment(fake, tmp_path / "args.txt")
    environment.update({
        "ARTICLE_ROOT": str(ROOT),
        "ARTICLE_STATE_DIR": str(state_root),
        "ARTICLE_REPAIR_LOG": str(log),
        "ARTICLE_MODEL_RUNNER": str(RUNNER),
        "LIFE_MANAGER_SOURCE_REPO": str(repo),
        "ARTICLE_REPAIR_BASE_REF": "main",
        "ARTICLE_REPAIR_ROOT": str(tmp_path / "repairs"),
        "ARTICLE_REPAIR_BUDGET_SECONDS": "120",
    })
    result = subprocess.run(
        ["bash", str(DRIVER)], env=environment, capture_output=True, text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    body = log.read_text(encoding="utf-8")
    assert '"status":"CANDIDATE_VERIFIED"' in body, body
    item = json.loads(queue_path.read_text(encoding="utf-8"))["items"][FINGERPRINT]
    assert item["next_action"] == "VERIFY_SENSITIVE_REPAIR_IN_ISOLATED_FIXTURE"


def test_the_driver_exits_quietly_when_there_is_no_incident_queue(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    (state_root / "runs").mkdir(parents=True)
    log = tmp_path / "repair.log"
    environment = os.environ.copy()
    environment.update({
        "ARTICLE_ROOT": str(ROOT),
        "ARTICLE_STATE_DIR": str(state_root),
        "ARTICLE_REPAIR_LOG": str(log),
    })
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(DRIVER)], env=environment, capture_output=True, text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 30
