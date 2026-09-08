from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_note_intent_circuit_hashes_every_executed_publisher_dependency() -> None:
    worker = (ROOT / "scripts/article-resume-pending.sh").read_text(
        encoding="utf-8"
    )

    assert 'note-publish/publish-paid.py"' in worker
    assert 'note-publish/set-eyecatch-api.py"' in worker


def test_worker_begins_tracked_editorial_hash_scope_repair() -> None:
    worker = (ROOT / "scripts/article-resume-pending.sh").read_text(
        encoding="utf-8"
    )

    assert '"tracked-editorial-hash-scope-source-defect"' in worker


def test_worker_begins_tracked_topic_router_reroute_repair() -> None:
    worker = (ROOT / "scripts/article-resume-pending.sh").read_text(
        encoding="utf-8"
    )

    assert '"tracked-topic-router-reroute-source-defect"' in worker


def test_worker_recovers_historic_unavailable_runs_before_planning() -> None:
    worker = (ROOT / "scripts/article-resume-pending.sh").read_text(
        encoding="utf-8"
    )

    recovery = worker.split(
        'python3 "$ARTICLE_ROOT/scripts/recover-known-unavailable.py"', 1
    )[1].split('python3 "$ARTICLE_ROOT/scripts/writer_unavailable_incident_bridge.py"', 1)[0]
    assert '--state-root "$STATE_DIR"' in recovery
    assert '--run-id' not in recovery


def test_note_ambiguity_recovery_uses_the_same_failure_circuit() -> None:
    worker = (ROOT / "scripts/article-resume-pending.sh").read_text(
        encoding="utf-8"
    )

    recovery = worker.split(
        '# A recognized note ambiguity', 1
    )[1].split('# A missing managed target', 1)[0]
    assert 'resume_failure_circuit.py" run' in recovery
    assert '--circuit "$RUN_DIR/gates/resume-failure-circuit.json"' in recovery
    assert '--pair "note/ja"' in recovery
    assert '--code-file "$ARTICLE_ROOT/scripts/publication_remote.py"' in recovery
    assert '--code-file "$ARTICLE_ROOT/scripts/publication_resume.py"' in recovery


def executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_df_env(tmp_path: Path) -> str:
    """Give subprocess fixtures deterministic headroom without changing production guards."""
    env_file = tmp_path / "df-env.sh"
    env_file.write_text(
        "df() {\n"
        "  printf '%s\\n' "
        "'Filesystem 1024-blocks Used Available Capacity Mounted on' "
        "'/dev/test 999999999 0 1000000 1% /'\n"
        "}\n"
    )
    return str(env_file)


def test_note_resume_prioritizes_publication_and_uses_failure_circuit(
    tmp_path: Path,
) -> None:
    """The real worker wiring must suppress a third unchanged note failure."""
    fake_root = tmp_path / "writer-agent"
    state_dir = tmp_path / "state"
    run_dir = state_dir / "runs" / "daily-2026-07-30"
    scripts = fake_root / "scripts"
    runtime = fake_root / "runtime"
    gates = run_dir / "gates"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    gates.mkdir(parents=True)
    (scripts / "note-publish").mkdir()
    (scripts / "_shared").mkdir()
    shutil.copy(ROOT / "scripts" / "article-resume-pending.sh", scripts)
    shutil.copy(ROOT / "scripts" / "publication_contract_resolver.py", scripts)
    shutil.copy(ROOT / "scripts" / "publication_contract.py", scripts)
    shutil.copy(ROOT / "scripts" / "publication_remote.py", scripts)
    shutil.copy(ROOT / "scripts" / "publication_resume.py", scripts)
    shutil.copy(ROOT / "scripts" / "resume_failure_circuit.py", scripts)
    shutil.copy(ROOT / "scripts" / "writer_capacity_floor.py", scripts)
    shutil.copy(ROOT / "scripts" / "_shared" / "notifier.sh", scripts / "_shared")
    (scripts / "writer-runtime-env.sh").write_text(
        "WRITER_LOG_DIR=\"$ARTICLE_STATE_DIR/logs\"\n"
        "STATE_DIR=\"$ARTICLE_STATE_DIR\"\n"
        "WRITER_BROWSER_PYTHON=python3\n"
        "export WRITER_LOG_DIR STATE_DIR WRITER_BROWSER_PYTHON\n"
    )
    (scripts / "note-publish" / "set-eyecatch-draft.py").write_text(
        "selector_version = 1\n"
    )
    for dependency in ("set-eyecatch-api.py", "publish-paid.py"):
        (scripts / "note-publish" / dependency).write_text(
            "dependency_version = 1\n"
        )

    (scripts / "article_daily_start_control.py").write_text(
        'print(\'{"action":"new"}\')\n'
    )
    quality_prompt = run_dir / "quality-feedback-prompt.txt"
    quality_prompt.write_text("quality feedback")
    (scripts / "quality_feedback_recovery.py").write_text(
        "import json,sys\n"
        f"prompt={str(quality_prompt)!r}\n"
        "command=sys.argv[1]\n"
        "if command == 'plan':\n"
        " print(json.dumps({'status':'READY','reason':'terminal-quality-feedback'}))\n"
        "elif command in {'begin','invoke'}:\n"
        " print(json.dumps({'status':'READY','run_id':'quality-run','prompt_path':prompt}))\n"
        "else:\n"
        " print(json.dumps({'status':'DONE'}))\n"
    )
    (scripts / "quality_repair_control.py").write_text(
        'print(\'{"status":"NONE"}\')\n'
    )
    (scripts / "recover-known-unavailable.py").write_text("raise SystemExit(0)\n")
    (scripts / "writer_unavailable_incident_bridge.py").write_text(
        "import os\n"
        "open(os.environ['BRIDGE_CALLS'], 'a').write('incident-bridge\\n')\n"
    )
    (scripts / "article_pending.py").write_text(
        "import json\n"
        f"print(json.dumps({{'status':'READY','run_id':'daily-2026-07-30',"
        f"'run_dir':{str(run_dir)!r},"
        f"'state_path':{str(gates / 'publication-state.json')!r},"
        f"'ledger_path':{str(state_dir / 'articles.jsonl')!r},"
        "'initialization_pairs':[],'eligible_pairs':['note/ja']}))\n"
    )
    executable(
        scripts / "publish-note-managed.py",
        "#!/usr/bin/env python3\n"
        "import os\n"
        "open(os.environ['CALLS'], 'a').write('call\\n')\n"
        "raise SystemExit('TimeoutError: upload button is absent')\n",
    )
    executable(
        scripts / "article-completion-notify.py",
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
    )
    executable(
        runtime / "model-runner.sh",
        "#!/usr/bin/env bash\nexit 91\n",
    )
    (runtime / "model-runner-support.py").write_text("raise SystemExit(0)\n")
    (gates / "publication-state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "daily-2026-07-30",
                "publication_contract": "active-six",
                "pairs": {
                    "note/ja": {
                        "status": "intent",
                        "target_kind": "note-key",
                        "target": "n-test",
                    }
                },
            }
        )
    )
    (state_dir / "articles.jsonl").write_text("")
    calls = tmp_path / "calls"
    bridge_calls = tmp_path / "bridge-calls"
    log = tmp_path / "resume.log"
    env = {
        **os.environ,
        "ARTICLE_ROOT": str(fake_root),
        "ARTICLE_STATE_DIR": str(state_dir),
        "ARTICLE_LOCAL_DATE": "2026-07-30",
        "ARTICLE_LOCAL_HOUR": "00",
        "ARTICLE_DAILY_SCHEDULE_HOUR": "6",
        "ARTICLE_RESUME_LOG": str(log),
        "ARTICLE_MODEL_RUNNER": str(runtime / "model-runner.sh"),
        "ARTICLE_MODEL_SUPPORT": str(runtime / "model-runner-support.py"),
        "ARTICLE_OWNER_FENCE_ACTIVE": "1",
        "ARTICLE_RESUME_MIN_FREE_BYTES": "536870912",
        "BASH_ENV": _fake_df_env(tmp_path),
        "CALLS": str(calls),
        "BRIDGE_CALLS": str(bridge_calls),
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
    }
    command = ["bash", str(scripts / "article-resume-pending.sh")]

    results = [
        subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        for _ in range(3)
    ]

    assert [result.returncode for result in results] == [1, 0, 0], [
        result.stderr for result in results
    ]
    assert calls.read_text().splitlines() == ["call", "call"]
    assert bridge_calls.read_text().splitlines() == ["incident-bridge"] * 3
    assert "same-failure-circuit-open" in log.read_text()
