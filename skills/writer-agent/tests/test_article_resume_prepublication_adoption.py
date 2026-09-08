from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, body: str, *, executable: bool = False) -> None:
    path.write_text(body, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _fake_df_env(tmp_path: Path) -> str:
    path = tmp_path / "df-env.sh"
    _write(
        path,
        "df() { printf '%s\\n' "
        "'Filesystem 1024-blocks Used Available Capacity Mounted on' "
        "'/dev/test 999999999 0 1000000 1% /'; }\n",
    )
    return str(path)


def test_cross_day_adoption_precedes_both_quality_plans_and_never_starts_daily(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "writer-agent"
    scripts = fake_root / "scripts"
    runtime = fake_root / "runtime"
    state = tmp_path / "state"
    target_id = "20260829-165022"
    target = state / "runs" / target_id
    newer = state / "runs" / "20260830-151951"
    for run_dir, status in (
        (target, "provider-failed-ambiguous"),
        (newer, "interrupted-safe"),
    ):
        (run_dir / "gates").mkdir(parents=True)
        _write(run_dir / "article-daily-prompt.txt", "immutable prompt")
        _write(
            run_dir / "gates/generation-state.json",
            json.dumps({"run_id": run_dir.name, "status": status}),
        )
    scripts.mkdir(parents=True, exist_ok=True)
    runtime.mkdir()
    shutil.copy(ROOT / "scripts/article-resume-pending.sh", scripts)
    shutil.copy(ROOT / "scripts/writer-runtime-env.sh", scripts)
    calls = tmp_path / "calls"
    _write(
        state / "articles.jsonl",
        json.dumps({"run_id": target_id, "published": False, "state": "pending"})
        + "\n",
    )
    _write(
        scripts / "article_daily_start_control.py",
        'print(\'{"action":"new","reason":"no-same-jst-day-run"}\')\n',
    )
    _write(
        scripts / "article_pending.py",
        "import json, os\n"
        "if os.environ.get('PRIORITY_READY') == '1':\n"
        f" open(os.environ['CALLS'], 'a').write('publication-plan\\n')\n"
        f" print(json.dumps({{'status':'READY','run_id':{target_id!r},"
        f"'run_dir':{str(target)!r},"
        f"'state_path':{str(target / 'gates/generation-state.json')!r},"
        f"'ledger_path':{str(state / 'articles.jsonl')!r},"
        "'initialization_pairs':[],'eligible_pairs':['note/ja'],'recovery_pairs':[]}))\n"
        "else:\n"
        " print('{\"status\":\"BLOCKED\"}')\n",
    )
    _write(
        scripts / "article_generation_state.py",
        "import json, os, pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "open(os.environ['CALLS'], 'a').write('adopt ' + run_dir.name + '\\n')\n"
        "if os.environ.get('ADOPTION_FAIL') == '1': raise SystemExit(9)\n"
        "state_path = run_dir / 'gates/generation-state.json'\n"
        "state = json.loads(state_path.read_text())\n"
        "state['status'] = 'quality-repair-ready'\n"
        "state_path.write_text(json.dumps(state))\n"
        "print(json.dumps({'action':'adopted','status':'quality-repair-ready'}))\n",
    )
    _write(
        scripts / "quality_feedback_recovery.py",
        "import json, os, sys\n"
        "def plan(*_args): return {'status':'NONE'}\n"
        "if __name__ == '__main__':\n"
        " open(os.environ['CALLS'], 'a').write('quality-feedback\\n')\n"
        " if os.environ.get('FEEDBACK_READY') == '1':\n"
        "  print(json.dumps({'status':'READY','reason':'terminal-quality-feedback'}))\n"
        " else:\n"
        "  print(json.dumps({'status':'NONE'}))\n",
    )
    repair_prompt = target / "quality-repair-prompt.txt"
    owner_recovery_prompt = target / "owner-recovery-prompt.txt"
    _write(repair_prompt, "repair")
    _write(owner_recovery_prompt, "owner recovery")
    _write(
        scripts / "quality_repair_control.py",
        "import json, os, sys\n"
        "command = sys.argv[1]\n"
        "if command == 'plan':\n"
        " open(os.environ['CALLS'], 'a').write('quality-plan\\n')\n"
        " if os.environ.get('QUALITY_PLAN_REFUSE') == '1':\n"
        "  print(json.dumps({'status':'REFUSED','reason':'quality-repair-evidence-invalid'}))\n"
        " elif os.environ.get('QUALITY_PLAN_REFUSE') == 'terminal':\n"
        "  print(json.dumps({'status':'REFUSED','reason':'quality-repair-already-terminal-blocked'}))\n"
        " else:\n"
        "  shape = os.environ.get('QUALITY_PLAN_SHAPE')\n"
        "  if shape == 'source-recovery':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-active-editorial-repair-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'source-recovery-attempt-one':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-active-editorial-repair-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'source-recovery-wrong-state':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-active-editorial-repair-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'owner-recovery':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-repair-owner-prompt-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'owner-recovery-attempt-one':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-repair-owner-prompt-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'owner-recovery-wrong-state':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-repair-owner-prompt-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'owner-orphan':\n"
        f"   print(json.dumps({{'status':'READY','reason':'orphaned-owner-prompt-recovery','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(owner_recovery_prompt)!r},'prompt_sha256':{hashlib.sha256(owner_recovery_prompt.read_bytes()).hexdigest()!r},'orphaned_owner_pid':1234}}))\n"
        "  elif shape == 'owner-orphan-missing':\n"
        f"   print(json.dumps({{'status':'READY','reason':'orphaned-owner-prompt-recovery','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(owner_recovery_prompt)!r},'prompt_sha256':{hashlib.sha256(owner_recovery_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'owner-orphan-mismatch':\n"
        f"   print(json.dumps({{'status':'READY','reason':'orphaned-owner-prompt-recovery','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(owner_recovery_prompt)!r},'prompt_sha256':{hashlib.sha256(owner_recovery_prompt.read_bytes()).hexdigest()!r},'orphaned_owner_pid':9999}}))\n"
        "  elif shape == 'cross-source-extra':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-active-editorial-repair-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(owner_recovery_prompt)!r},'prompt_sha256':{hashlib.sha256(owner_recovery_prompt.read_bytes()).hexdigest()!r},'orphaned_owner_pid':1234}}))\n"
        "  elif shape == 'cross-owner-invoking':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-repair-owner-prompt-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':2,'prompt_path':{str(owner_recovery_prompt)!r},'prompt_sha256':{hashlib.sha256(owner_recovery_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'source-recovery-wrong-run':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-active-editorial-repair-source-defect','run_id':'other-run','run_dir':{str(target)!r},'repair_epoch':1,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'source-recovery-malformed':\n"
        f"   print(json.dumps({{'status':'READY','reason':'tracked-active-editorial-repair-source-defect','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':1}}))\n"
        "  elif shape == 'wrong-run':\n"
        f"   print(json.dumps({{'status':'READY','reason':'prepared-quality-repair','run_id':'other-run','run_dir':{str(target)!r},'repair_epoch':1,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'malformed':\n"
        f"   print(json.dumps({{'status':'READY','reason':'prepared-quality-repair','run_id':{target_id!r}}}))\n"
        "  elif shape == 'structurally-exhausted':\n"
        f"   print(json.dumps({{'status':'READY','reason':'structurally-exhausted-quality-evaluations','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':1}}))\n"
        "  elif shape == 'orphaned':\n"
        f"   print(json.dumps({{'status':'READY','reason':'orphaned-quality-repair','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r},'orphaned_owner_pid':1234}}))\n"
        "  elif shape == 'mismatch':\n"
        f"   print(json.dumps({{'status':'READY','reason':'prepared-quality-repair','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':2,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':'bad'}}))\n"
        "  elif shape == 'unknown':\n"
        f"   print(json.dumps({{'status':'READY','reason':'unsupported-quality-repair','run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        "  elif shape == 'no-output':\n"
        "   pass\n"
        "  else:\n"
        f"   reason = 'prepared-quality-repair' if os.path.exists({str(target / 'gates/quality-repair-state.json')!r}) else 'tracked-reader-terminal-receipt-source-defect'\n"
        f"   print(json.dumps({{'status':'READY','reason':reason,'run_id':{target_id!r},'run_dir':{str(target)!r},'repair_epoch':1,'attempts':1,'prompt_path':{str(repair_prompt)!r},'prompt_sha256':{hashlib.sha256(repair_prompt.read_bytes()).hexdigest()!r}}}))\n"
        " if os.environ.get('QUALITY_PLAN_RC') is not None:\n"
        "  raise SystemExit(int(os.environ['QUALITY_PLAN_RC']))\n"
        "elif command == 'begin': open(os.environ['CALLS'], 'a').write('quality-begin\\n'); "
        f"print(json.dumps({{'status':'prepared','run_id':{target_id!r},'prompt_path':{str(repair_prompt)!r}}}))\n"
        "elif command == 'invoke': open(os.environ['CALLS'], 'a').write('quality-invoke\\n'); "
        f"print(json.dumps({{'status':'invoking','run_id':{target_id!r},'prompt_path':({str(owner_recovery_prompt)!r} if os.environ.get('QUALITY_PLAN_SHAPE') in ('owner-recovery','owner-orphan') else {str(repair_prompt)!r})}}))\n"
        "elif command == 'terminalize': open(os.environ['CALLS'], 'a').write('quality-terminalize\\n'); "
        "print(json.dumps({'status':'ok'}))\n"
        "else: "
        f"print(json.dumps({{'status':'ok','prompt_path':{str(repair_prompt)!r}}}))\n",
    )
    _write(
        scripts / "writer_capacity_floor.py",
        "print(536870912)\n",
    )
    _write(
        scripts / "publication_contract_resolver.py",
        "import os\n"
        "open(os.environ['CALLS'], 'a').write('publication-foreground\\n')\n"
        "raise SystemExit(9)\n",
    )
    _write(runtime / "model-runner-support.py", "raise SystemExit(0)\n")
    _write(
        runtime / "judge-broker.sh",
        "#!/usr/bin/env bash\nexit 0\n",
        executable=True,
    )
    _write(
        runtime / "model-runner.sh",
        "#!/usr/bin/env bash\nprintf 'model-runner\\n' >> \"$CALLS\"\nprintf '%s\\n' \"$3\" >> \"$PROMPTS\"\nexit 97\n",
        executable=True,
    )
    daily = fake_root / "article-daily.sh"
    _write(
        daily,
        f"#!/usr/bin/env bash\nprintf daily > {str(tmp_path / 'daily-started')!r}\n",
        executable=True,
    )

    env = {
            **os.environ,
            "ARTICLE_ROOT": str(fake_root),
            "ARTICLE_STATE_DIR": str(state),
            "ARTICLE_OWNER_FENCE_ACTIVE": "1",
            "ARTICLE_LOCAL_DATE": "2026-08-31",
            "ARTICLE_LOCAL_HOUR": "00",
            "ARTICLE_DAILY_SCHEDULE_HOUR": "6",
            "ARTICLE_RESUME_MIN_FREE_BYTES": "536870912",
            "ARTICLE_RESUME_LOG": str(tmp_path / "resume.log"),
            "ARTICLE_MODEL_SUPPORT": str(runtime / "model-runner-support.py"),
            "ARTICLE_MODEL_RUNNER": str(runtime / "model-runner.sh"),
            "BASH_ENV": _fake_df_env(tmp_path),
            "CALLS": str(calls),
            "PROMPTS": str(tmp_path / "prompts"),
        }
    command = ["bash", str(scripts / "article-resume-pending.sh")]
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        f"adopt {target_id}",
        "quality-feedback",
        "quality-plan",
        "quality-begin",
        "quality-invoke",
        "model-runner",
    ]
    assert not (tmp_path / "daily-started").exists()
    assert json.loads((target / "gates/generation-state.json").read_text())["status"] == (
        "quality-repair-ready"
    )

    # Once quality repair has been prepared, the next wake must not re-adopt
    # the same run. The fake adopter records and rejects any redundant call.
    repair_state = target / "gates/quality-repair-state.json"
    _write(
        repair_state,
        json.dumps(
            {
                "version": 1,
                "status": "prepared",
                "run_id": target_id,
                "repair_epoch": 1,
                "attempts": 1,
                "prompt_path": str(repair_prompt),
                "prompt_sha256": hashlib.sha256(repair_prompt.read_bytes()).hexdigest(),
                "qrr_lineage": {
                    "adoption_receipt_sha256": "a" * 64,
                    "adoption_receipt_file_sha256": "b" * 64,
                    "transition_receipt_sha256": "c" * 64,
                    "prompt_sha256": hashlib.sha256(repair_prompt.read_bytes()).hexdigest(),
                },
            }
        ),
    )
    # A repair-state file may not coexist with a non-ready generation status.
    _write(
        target / "gates/generation-state.json",
        json.dumps({"run_id": target_id, "status": "provider-failed-ambiguous"}),
    )
    calls.unlink()
    wrong_generation = subprocess.run(
        command,
        env={**env, "ADOPTION_FAIL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong_generation.returncode == 1
    assert not calls.exists()
    _write(
        target / "gates/generation-state.json",
        json.dumps({"run_id": target_id, "status": "quality-repair-ready"}),
    )

    calls.unlink(missing_ok=True)
    repair_retry = subprocess.run(
        command,
        env={**env, "ADOPTION_FAIL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert repair_retry.returncode == 0, repair_retry.stderr
    assert calls.read_text().splitlines() == [
        "quality-plan",
        "quality-feedback",
        "quality-invoke",
        "model-runner",
    ]
    assert not (tmp_path / "daily-started").exists()

    # The same-attempt source recovery is a ready repair plan, not a new begin.
    prepared_state = json.loads(repair_state.read_text())
    source_recovery_state = dict(prepared_state)
    source_recovery_state.update(
        {
            "status": "terminal-incomplete",
            "attempts": 2,
            "quality_action": "evaluate_reroute",
            "source_defect": "reader-terminal-receipt",
        }
    )
    _write(repair_state, json.dumps(source_recovery_state))
    calls.unlink()
    source_recovery = subprocess.run(
        command,
        env={
            **env,
            "ADOPTION_FAIL": "1",
            "QUALITY_PLAN_SHAPE": "source-recovery",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert source_recovery.returncode == 0, source_recovery.stderr
    assert calls.read_text().splitlines() == [
        "quality-plan",
        "quality-feedback",
        "quality-invoke",
        "model-runner",
    ]
    assert not (tmp_path / "daily-started").exists()

    for shape in (
        "source-recovery-wrong-run",
        "source-recovery-malformed",
        "source-recovery-attempt-one",
    ):
        calls.unlink()
        invalid_source_recovery = subprocess.run(
            command,
            env={
                **env,
                "ADOPTION_FAIL": "1",
                "QUALITY_PLAN_SHAPE": shape,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid_source_recovery.returncode == 1
        assert calls.read_text().splitlines() == ["quality-plan"]

    wrong_state = dict(source_recovery_state)
    wrong_state["status"] = "prepared"
    _write(repair_state, json.dumps(wrong_state))
    calls.unlink()
    invalid_source_recovery_state = subprocess.run(
        command,
        env={
            **env,
            "ADOPTION_FAIL": "1",
            "QUALITY_PLAN_SHAPE": "source-recovery-wrong-state",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_source_recovery_state.returncode == 1
    assert calls.read_text().splitlines() == ["quality-plan"]
    _write(repair_state, json.dumps(prepared_state))

    owner_recovery_state = dict(prepared_state)
    owner_recovery_state.update(
        {
            "status": "terminal-incomplete",
            "attempts": 2,
            "quality_action": "evaluate_reroute",
            "source_defect": "reader-terminal-receipt",
        }
    )
    _write(repair_state, json.dumps(owner_recovery_state))
    calls.unlink()
    owner_recovery = subprocess.run(
        command,
        env={
            **env,
            "ADOPTION_FAIL": "1",
            "QUALITY_PLAN_SHAPE": "owner-recovery",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert owner_recovery.returncode == 0, owner_recovery.stderr
    assert calls.read_text().splitlines() == [
        "quality-plan",
        "quality-feedback",
        "quality-invoke",
        "model-runner",
    ]
    assert (tmp_path / "prompts").read_text().splitlines()[-1] == str(owner_recovery_prompt)
    for shape in ("owner-recovery-attempt-one", "owner-recovery-wrong-state"):
        state_for_shape = dict(owner_recovery_state)
        if shape == "owner-recovery-wrong-state":
            state_for_shape["status"] = "prepared"
        _write(repair_state, json.dumps(state_for_shape))
        calls.unlink()
        invalid_owner_recovery = subprocess.run(
            command,
            env={
                **env,
                "ADOPTION_FAIL": "1",
                "QUALITY_PLAN_SHAPE": shape,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid_owner_recovery.returncode == 1
        assert calls.read_text().splitlines() == ["quality-plan"]
    _write(repair_state, json.dumps(prepared_state))

    orphan_state = dict(prepared_state)
    orphan_state.update({"status":"invoking","attempts":2,"quality_action":"evaluate_reroute","source_defect":"reader-terminal-receipt","owner_pid":1234,"source_recovery_receipt_sha256":"b" * 64,"owner_recovery_receipt_sha256":"a" * 64,"prompt_path":str(owner_recovery_prompt),"prompt_sha256":hashlib.sha256(owner_recovery_prompt.read_bytes()).hexdigest()})
    _write(repair_state, json.dumps(orphan_state))
    calls.unlink()
    orphan = subprocess.run(command, env={**env,"ADOPTION_FAIL":"1","QUALITY_PLAN_SHAPE":"owner-orphan"}, capture_output=True,text=True,check=False)
    assert orphan.returncode == 0, orphan.stderr
    assert calls.read_text().splitlines() == ["quality-plan","quality-feedback","quality-invoke","model-runner"]
    assert (tmp_path / "prompts").read_text().splitlines()[-1] == str(owner_recovery_prompt)
    calls.unlink()
    invalid_orphan = subprocess.run(command, env={**env,"ADOPTION_FAIL":"1","QUALITY_PLAN_SHAPE":"owner-orphan-missing"}, capture_output=True,text=True,check=False)
    assert invalid_orphan.returncode == 1
    assert calls.read_text().splitlines() == ["quality-plan"]
    calls.unlink()
    cross = subprocess.run(command, env={**env,"ADOPTION_FAIL":"1","QUALITY_PLAN_SHAPE":"owner-orphan-mismatch"}, capture_output=True,text=True,check=False)
    assert cross.returncode == 1
    assert calls.read_text().splitlines() == ["quality-plan"]
    missing_receipt = dict(orphan_state)
    missing_receipt.pop("owner_recovery_receipt_sha256")
    _write(repair_state, json.dumps(missing_receipt))
    calls.unlink()
    cross = subprocess.run(command, env={**env,"ADOPTION_FAIL":"1","QUALITY_PLAN_SHAPE":"owner-orphan"}, capture_output=True,text=True,check=False)
    assert cross.returncode == 1
    assert calls.read_text().splitlines() == ["quality-plan"]
    for shape in ("cross-source-extra", "cross-owner-invoking"):
        calls.unlink()
        cross = subprocess.run(command, env={**env,"ADOPTION_FAIL":"1","QUALITY_PLAN_SHAPE":shape}, capture_output=True,text=True,check=False)
        assert cross.returncode == 1
        assert calls.read_text().splitlines() == ["quality-plan"]
    terminal_orphan = dict(orphan_state)
    terminal_orphan["status"] = "terminal-incomplete"
    _write(repair_state, json.dumps(terminal_orphan))
    calls.unlink()
    cross = subprocess.run(command, env={**env,"ADOPTION_FAIL":"1","QUALITY_PLAN_SHAPE":"owner-orphan"}, capture_output=True,text=True,check=False)
    assert cross.returncode == 1
    assert calls.read_text().splitlines() == ["quality-plan"]
    _write(repair_state, json.dumps(prepared_state))

    # A terminal-blocked refusal is allowed only with controller rc=1.
    calls.unlink()
    terminal_refusal = subprocess.run(
        command,
        env={
            **env,
            "ADOPTION_FAIL": "1",
            "QUALITY_PLAN_REFUSE": "terminal",
            "QUALITY_PLAN_RC": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert terminal_refusal.returncode == 0, terminal_refusal.stderr
    assert calls.read_text().splitlines() == ["quality-plan", "quality-feedback"]

    # Other controller refusals remain before feedback/model/daily.
    calls.unlink()
    refused = subprocess.run(
        command,
        env={
            **env,
            "ADOPTION_FAIL": "1",
            "FEEDBACK_READY": "1",
            "QUALITY_PLAN_REFUSE": "1",
            "QUALITY_PLAN_RC": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 1
    assert calls.read_text().splitlines() == ["quality-plan"]

    # The structural terminal-ready shape and orphaned shape are valid plans.
    for shape, expected in (
        (
            "structurally-exhausted",
            ["quality-plan", "quality-feedback", "quality-terminalize"],
        ),
        ("orphaned", ["quality-plan", "quality-feedback", "quality-invoke", "model-runner"]),
    ):
        calls.unlink()
        valid_plan = subprocess.run(
            command,
            env={**env, "ADOPTION_FAIL": "1", "QUALITY_PLAN_SHAPE": shape},
            capture_output=True,
            text=True,
            check=False,
        )
        assert valid_plan.returncode == 0, valid_plan.stderr
        assert calls.read_text().splitlines() == expected

    for shape in ("wrong-run", "malformed", "mismatch", "unknown", "no-output"):
        calls.unlink()
        invalid_plan = subprocess.run(
            command,
            env={**env, "ADOPTION_FAIL": "1", "QUALITY_PLAN_SHAPE": shape},
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid_plan.returncode == 1
        assert calls.read_text().splitlines() == ["quality-plan"]

    # Repair-state links and non-regular paths fail before any planner/model.
    repair_state_bytes = repair_state.read_bytes()
    repair_state.unlink()
    repair_state.symlink_to(repair_prompt)
    calls.unlink()
    symlink_state = subprocess.run(
        command,
        env={**env, "ADOPTION_FAIL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert symlink_state.returncode == 1
    assert not calls.exists()
    repair_state.unlink()
    repair_state.write_bytes(repair_state_bytes)

    repair_state.unlink()
    repair_state.mkdir()
    calls.unlink(missing_ok=True)
    nonregular_state = subprocess.run(
        command,
        env={**env, "ADOPTION_FAIL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert nonregular_state.returncode == 1
    assert not calls.exists()
    repair_state.rmdir()
    repair_state.write_bytes(repair_state_bytes)

    # A priority publication backlog must not be suppressed by the adopted-run guard.
    calls.unlink(missing_ok=True)
    priority = subprocess.run(
        command,
        env={
            **env,
            "ADOPTION_FAIL": "1",
            "PRIORITY_READY": "1",
            "ARTICLE_LOCAL_DATE": "2026-08-29",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert priority.returncode == 1
    priority_calls = calls.read_text().splitlines()
    assert "publication-plan" in priority_calls
    assert "publication-foreground" in priority_calls
    assert "model-runner" not in priority_calls
    assert not (tmp_path / "daily-started").exists()

    # A feedback-ready state cannot bypass the repair planner's lineage refusal.
    calls.unlink()
    refused = subprocess.run(
        command,
        env={
            **env,
            "ADOPTION_FAIL": "1",
            "FEEDBACK_READY": "1",
            "QUALITY_PLAN_REFUSE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 1
    assert calls.read_text().splitlines() == ["quality-plan"]
    assert not (tmp_path / "daily-started").exists()
    repair_state.unlink()

    # Invalid adoption evidence is fail-closed before either quality planner.
    _write(
        target / "gates/generation-state.json",
        json.dumps({"run_id": target_id, "status": "provider-failed-ambiguous"}),
    )
    calls.unlink()
    failed = subprocess.run(
        command,
        env={**env, "ADOPTION_FAIL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert calls.read_text().splitlines() == [f"adopt {target_id}"]
    assert not (tmp_path / "daily-started").exists()

    # A ledger-backed run in a non-candidate state is neutral; before 06:00 the
    # normal scheduler boundary returns without adoption or a new run.
    _write(
        target / "gates/generation-state.json",
        json.dumps({"run_id": target_id, "status": "interrupted-safe"}),
    )
    calls.unlink()
    neutral = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert neutral.returncode == 0, neutral.stderr
    assert not calls.exists()
    assert not (tmp_path / "daily-started").exists()

    # Two ledger-backed ambiguous runs are never resolved by arbitrary sort
    # order; the owner refuses before invoking either adopter.
    _write(
        target / "gates/generation-state.json",
        json.dumps({"run_id": target_id, "status": "provider-failed-ambiguous"}),
    )
    _write(
        newer / "gates/generation-state.json",
        json.dumps({"run_id": newer.name, "status": "provider-failed-ambiguous"}),
    )
    _write(
        state / "articles.jsonl",
        "\n".join(
            json.dumps({"run_id": run_id, "published": False, "state": "pending"})
            for run_id in (target_id, newer.name)
        )
        + "\n",
    )
    ambiguous = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ambiguous.returncode == 1
    assert not calls.exists()
    assert not (tmp_path / "daily-started").exists()
