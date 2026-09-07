import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parent / "migrate-legacy-state.sh"


def run_migration(work: Path, runtime: Path, logs: Path, destination: Path):
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env={
            **os.environ,
            "REDDIT_LEGACY_WORK_STATE": str(work),
            "REDDIT_LEGACY_RUNTIME_STATE": str(runtime),
            "REDDIT_LEGACY_LOG_ROOT": str(logs),
            "REDDIT_STATE_ROOT": str(destination),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def fixture(tmp_path):
    work = tmp_path / "work-state"
    runtime = tmp_path / "runtime-state"
    logs = tmp_path / "logs"
    destination = tmp_path / "destination"
    (runtime / "agent-runner-evidence/reddit-daily/pass-1").mkdir(parents=True)
    work.mkdir()
    logs.mkdir()
    (work / "posts.jsonl").write_text('{"url":"https://reddit.example/post"}\n')
    (runtime / ".reddit-loop-last-pass").write_text("done\n")
    (runtime / "agent-runner-evidence/reddit-daily/pass-1/summary.json").write_text("{}\n")
    (logs / "reddit-loop-daily.log").write_text("pass\n")
    return work, runtime, logs, destination


def test_migrates_ledger_heartbeat_evidence_and_logs_without_removing_sources(tmp_path):
    work, runtime, logs, destination = fixture(tmp_path)
    result = run_migration(work, runtime, logs, destination)

    assert result.returncode == 0, result.stderr
    assert (destination / "state/posts.jsonl").read_text().startswith('{"url"')
    assert (destination / "state/.reddit-loop-last-pass").read_text() == "done\n"
    assert (destination / "state/agent-runner-evidence/reddit-daily/pass-1/summary.json").read_text() == "{}\n"
    assert (destination / "logs/reddit-loop-daily.log").read_text() == "pass\n"
    assert (work / "posts.jsonl").exists()
    assert "sources untouched" in result.stdout


def test_rerun_preserves_destination_owned_ledger(tmp_path):
    work, runtime, logs, destination = fixture(tmp_path)
    assert run_migration(work, runtime, logs, destination).returncode == 0
    target = destination / "state/posts.jsonl"
    target.write_text(target.read_text() + '{"url":"https://reddit.example/new"}\n')

    result = run_migration(work, runtime, logs, destination)

    assert result.returncode == 0, result.stderr
    assert target.read_text().endswith('{"url":"https://reddit.example/new"}\n')
    assert "skipped=" in result.stdout
