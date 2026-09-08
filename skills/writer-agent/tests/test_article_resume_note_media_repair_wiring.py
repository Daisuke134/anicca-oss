from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def test_worker_recovers_ambiguous_note_then_repairs_same_key(
    tmp_path: Path,
) -> None:
    """Two deterministic ticks must recover and repair without a model or new note."""
    fake_root = tmp_path / "writer-agent"
    state_dir = tmp_path / "state"
    note_mcp = tmp_path / "note-mcp"
    run_dir = state_dir / "runs" / "daily-2026-07-30"
    scripts = fake_root / "scripts"
    runtime = fake_root / "runtime"
    browser = tmp_path / "skills" / "browser"
    gates = run_dir / "gates"
    (scripts / "note-publish").mkdir(parents=True)
    (scripts / "_shared").mkdir()
    (note_mcp / ".venv" / "bin").mkdir(parents=True)
    runtime.mkdir()
    browser.mkdir(parents=True)
    gates.mkdir(parents=True)
    for name in ("article-resume-pending.sh", "resume_failure_circuit.py"):
        shutil.copy(ROOT / "scripts" / name, scripts)
    shutil.copy(ROOT / "scripts" / "writer_capacity_floor.py", scripts)
    for name in ("publication_remote.py", "publication_resume.py"):
        shutil.copy(ROOT / "scripts" / name, scripts)
    shutil.copy(ROOT / "scripts" / "publication_contract_resolver.py", scripts)
    shutil.copy(ROOT / "scripts" / "publication_contract.py", scripts)
    shutil.copy(ROOT / "scripts" / "_shared" / "notifier.sh", scripts / "_shared")
    executable(
        scripts / "writer-runtime-env.sh",
        "#!/usr/bin/env bash\n"
        "WRITER_LOG_DIR=\"$ARTICLE_STATE_DIR/logs\"\n"
        "STATE_DIR=\"$ARTICLE_STATE_DIR\"\n"
        "WRITER_BROWSER_PYTHON=\"$NOTE_MCP_DIR/.venv/bin/python\"\n"
        "export WRITER_LOG_DIR STATE_DIR WRITER_BROWSER_PYTHON\n",
    )
    (scripts / "note-publish" / "set-eyecatch-draft.py").write_text(
        "selector_version = 1\n"
    )
    (scripts / "article_daily_start_control.py").write_text(
        'print(\'{"action":"skip-pending-worker"}\')\n'
    )
    for name in ("quality_feedback_recovery.py", "quality_repair_control.py"):
        (scripts / name).write_text('print(\'{"status":"NONE"}\')\n')
    (scripts / "recover-known-unavailable.py").write_text("raise SystemExit(0)\n")
    (scripts / "writer_unavailable_incident_bridge.py").write_text(
        "raise SystemExit(0)\n"
    )
    (scripts / "writer_repair_dispatch.py").write_text("raise SystemExit(0)\n")
    executable(browser / "ensure_browser.sh", "#!/usr/bin/env bash\nexit 0\n")
    state_path = gates / "publication-state.json"
    ledger_path = state_dir / "articles.jsonl"
    (scripts / "article_pending.py").write_text(
        "import json\n"
        f"p={str(state_path)!r}; d=json.load(open(p)); "
        "status=d['pairs']['note/ja']['status']\n"
        f"base={{'status':'READY','run_id':'daily-2026-07-30',"
        f"'run_dir':{str(run_dir)!r},'state_path':p,"
        f"'ledger_path':{str(ledger_path)!r},'initialization_pairs':[]}}\n"
        "if status == 'ambiguous':\n"
        " base.update(recovery_pairs=['note/ja'],"
        "eligible_pairs=['x-article/ja','substack/ja'])\n"
        "else:\n"
        " base.update(recovery_pairs=[],eligible_pairs=['note/ja'])\n"
        "print(json.dumps(base))\n"
    )
    executable(
        scripts / "publication-guard.py",
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "p=os.environ['ARTICLE_PUBLICATION_STATE']; d=json.load(open(p))\n"
        "assert sys.argv[1:4] == ['recover-ambiguous','--pair','note/ja']\n"
        "d['pairs']['note/ja']['status']='repair-required'\n"
        "d['pairs']['note/ja'].pop('error',None)\n"
        "d['pairs']['note/ja']['existing_publication']={"
        "'live_url':'https://note.com/anicca123/n/n-test','public_id':'n-test'}\n"
        "json.dump(d,open(p,'w')); open(os.environ['CALLS'],'a').write('recover\\n')\n",
    )
    executable(
        scripts / "note-publish" / "note_inplace_repair.py",
        "#!/usr/bin/env python3\n"
        "import json,os\n"
        "p=os.environ['ARTICLE_PUBLICATION_STATE']; d=json.load(open(p))\n"
        "assert d['pairs']['note/ja']['target'] == 'n-test'\n"
        "d['pairs']['note/ja']['status']='live'\n"
        "d['pairs']['note/ja']['live_url']='https://note.com/anicca123/n/n-test'\n"
        "json.dump(d,open(p,'w')); open(os.environ['CALLS'],'a').write('repair\\n')\n",
    )
    executable(
        note_mcp / ".venv" / "bin" / "python",
        "#!/usr/bin/env bash\n"
        "echo pinned-python >>\"$CALLS\"\n"
        "exec python3 \"$@\"\n",
    )
    executable(
        scripts / "publish-note-managed.py",
        "#!/usr/bin/env python3\n"
        "import os\nopen(os.environ['CALLS'],'a').write('wrong-publisher\\n')\n"
        "raise SystemExit(99)\n",
    )
    executable(
        scripts / "article-completion-notify.py",
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
    )
    executable(runtime / "model-runner.sh", "#!/usr/bin/env bash\nexit 91\n")
    (runtime / "model-runner-support.py").write_text("raise SystemExit(0)\n")
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "daily-2026-07-30",
                "publication_contract": "active-six",
                "pairs": {
                    "note/ja": {
                        "status": "ambiguous",
                        "error": "public-asset-readback-failed",
                        "target_kind": "note-key",
                        "target": "n-test",
                    }
                },
            }
        )
    )
    ledger_path.write_text("")
    calls = tmp_path / "calls"
    log = tmp_path / "resume.log"
    env = {
        **os.environ,
        "ARTICLE_ROOT": str(fake_root),
        "ARTICLE_STATE_DIR": str(state_dir),
        "ARTICLE_LOCAL_DATE": "2026-07-30",
        "ARTICLE_RESUME_LOG": str(log),
        "ARTICLE_MODEL_RUNNER": str(runtime / "model-runner.sh"),
        "ARTICLE_MODEL_SUPPORT": str(runtime / "model-runner-support.py"),
        "ARTICLE_OWNER_FENCE_ACTIVE": "1",
        "ARTICLE_RESUME_MIN_FREE_BYTES": "536870912",
        "BASH_ENV": _fake_df_env(tmp_path),
        "CALLS": str(calls),
        "NOTE_MCP_DIR": str(note_mcp),
        "LIFE_MANAGER_REPO": str(tmp_path),
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
    }
    command = ["bash", str(scripts / "article-resume-pending.sh")]

    first = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    second = subprocess.run(command, env=env, capture_output=True, text=True, check=False)

    assert [first.returncode, second.returncode] == [0, 0], (first.stderr, second.stderr)
    assert calls.read_text().splitlines() == [
        "recover",
        "pinned-python",
        "repair",
    ]
    final = json.loads(state_path.read_text())["pairs"]["note/ja"]
    assert final["status"] == "live"
    assert final["target"] == "n-test"
