import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "migrate-legacy-state.sh"


def run_migration(work: Path, runtime: Path, logs: Path, destination: Path):
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env={
            **os.environ,
            "BOUNTY_LEGACY_WORK_STATE": str(work),
            "BOUNTY_LEGACY_RUNTIME_STATE": str(runtime),
            "BOUNTY_LEGACY_LOG_ROOT": str(logs),
            "BOUNTY_STATE_ROOT": str(destination),
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
    (runtime / "agent-runner-evidence/bounty-daily/pass-1").mkdir(parents=True)
    work.mkdir()
    logs.mkdir()
    (work / "attempts.jsonl").write_text('{"key":"o/r#1"}\n')
    (runtime / ".bounty-core-last-pass").write_text("done\n")
    (runtime / "agent-runner-evidence/bounty-daily/pass-1/summary.json").write_text("{}\n")
    (logs / "bounty-daily.log").write_text("pass\n")
    return work, runtime, logs, destination


def test_migrates_business_state_runtime_evidence_and_logs_without_removing_sources(tmp_path):
    work, runtime, logs, destination = fixture(tmp_path)
    result = run_migration(work, runtime, logs, destination)

    assert result.returncode == 0, result.stderr
    assert (destination / "state/attempts.jsonl").read_text() == '{"key":"o/r#1"}\n'
    assert (destination / "state/.bounty-core-last-pass").read_text() == "done\n"
    assert (destination / "state/agent-runner-evidence/bounty-daily/pass-1/summary.json").read_text() == "{}\n"
    assert (destination / "logs/bounty-daily.log").read_text() == "pass\n"
    assert (work / "attempts.jsonl").exists()
    assert "sources untouched" in result.stdout


def test_rerun_never_overwrites_destination_owned_file(tmp_path):
    work, runtime, logs, destination = fixture(tmp_path)
    assert run_migration(work, runtime, logs, destination).returncode == 0
    target = destination / "state/attempts.jsonl"
    target.write_text(target.read_text() + '{"key":"o/r#2"}\n')

    result = run_migration(work, runtime, logs, destination)

    assert result.returncode == 0, result.stderr
    assert target.read_text().endswith('{"key":"o/r#2"}\n')
    assert "skipped=" in result.stdout
