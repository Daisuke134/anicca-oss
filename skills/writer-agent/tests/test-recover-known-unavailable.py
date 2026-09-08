import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "recover-known-unavailable.py"


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def state_file(root: Path) -> Path:
    run = root / "runs" / "daily-2026-07-29"
    gates = run / "gates"
    gates.mkdir(parents=True)
    path = gates / "publication-state.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "daily-2026-07-29",
                "run_dir": str(run),
                "state_path": str(path),
                "ledger_path": str(root / "articles.jsonl"),
                "destination_identities": {
                    "substack/ja": "aniccabuddha.substack.com",
                },
                "pairs": {
                    "note/ja": {
                        "status": "unavailable",
                        "error": "note_mcp_venv_missing",
                    },
                    "substack/ja": {
                        "status": "unavailable",
                        "error": "substack_editor_redirect_own_eyes_unverified",
                        "target_kind": "substack-draft-id",
                        "target": "208" + "936451",
                    },
                    "x-article/ja": {
                        "status": "unavailable",
                        "error": "x_editor_login_wall_own_eyes_unverified",
                        "target": "https://x.com/compose/articles/edit/1",
                    },
                    "devto/en": {
                        "status": "unavailable",
                        "error": "devto-target-missing-from-owned-drafts-after-publish-put",
                        "target_kind": "devto-article-id",
                        "target": "4286330",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_recovers_only_proven_known_failures(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state_file(root)
    calls = tmp_path / "calls"
    render = executable(
        tmp_path / "render",
        'printf "render:%s\\n" "$*" >>"$CALLS"\n',
    )
    guard = tmp_path / "guard"
    guard.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("guard:" + " ".join(sys.argv[1:]) + "\\n")\n',
        encoding="utf-8",
    )
    note_src = tmp_path / "note-src"
    (note_src / "note_mcp/api").mkdir(parents=True)
    (note_src / "note_mcp/__init__.py").write_text("", encoding="utf-8")
    (note_src / "note_mcp/api/__init__.py").write_text("", encoding="utf-8")
    (note_src / "note_mcp/api/articles.py").write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "CALLS": str(calls),
        "NOTE_MCP_SRC": str(note_src),
        "ARTICLE_RENDER_VERIFY": str(render),
        "ARTICLE_PUBLICATION_GUARD": str(guard),
    }

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--state-root",
            str(root),
            "--run-id",
            "daily-2026-07-29",
        ],
        env=env,
        check=True,
    )

    logged = calls.read_text(encoding="utf-8").splitlines()
    assert logged == [
        "guard:clear-unavailable --pair note/ja",
        "render:--platform substack --url "
        "https://aniccabuddha.substack.com/publish/post/208936451 --lang ja",
        "guard:register-intent --pair substack/ja "
        "--target-kind substack-draft-id --target " + "208936451",
        "guard:recover-unavailable --pair devto/en",
    ]


def test_failed_note_and_substack_probes_never_rearm(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state_file(root)
    calls = tmp_path / "calls"
    failing = executable(tmp_path / "failing", "exit 9\n")
    guard = tmp_path / "guard"
    guard.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("guard:" + " ".join(sys.argv[1:]) + "\\n")\n',
        encoding="utf-8",
    )
    note_src = tmp_path / "note-src"
    note_src.mkdir()
    env = {
        **os.environ,
        "CALLS": str(calls),
        "NOTE_MCP_SRC": str(note_src),
        "WRITER_BROWSER_PYTHON": str(failing),
        "ARTICLE_RENDER_VERIFY": str(failing),
        "ARTICLE_PUBLICATION_GUARD": str(guard),
    }

    subprocess.run(
        ["python3", str(SCRIPT), "--state-root", str(root)],
        env=env,
        check=True,
    )
    # Dev.to performs its own authenticated probe inside the bounded guard;
    # neither of the two failed external probes may reach a state mutation.
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "guard:recover-unavailable --pair devto/en",
    ]


def test_recovers_known_zenn_stage_timeout_through_current_run_publisher(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    path = state_file(root)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["pairs"] = {
        "zenn-article/ja": {
            "status": "unavailable",
            "error": "zenn-stage-timeout-no-dispatch-result",
            "target_kind": "zenn-slug",
            "target": "known-zenn-slug",
        }
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    repo = tmp_path / "zenn"
    (repo / "articles").mkdir(parents=True)
    (repo / "articles/known-zenn-slug.md").write_text(
        "---\ntitle: Known\npublished: false\n---\nBody\n", encoding="utf-8"
    )
    calls = tmp_path / "calls"
    guard = tmp_path / "guard"
    guard.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("guard:" + " ".join(sys.argv[1:]) + "\\n")\n',
        encoding="utf-8",
    )
    publisher = tmp_path / "publisher"
    publisher.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("publisher:" + " ".join(sys.argv[1:]) + "\\n")\n',
        encoding="utf-8",
    )

    subprocess.run(
        ["python3", str(SCRIPT), "--state-root", str(root)],
        env={
            **os.environ,
            "CALLS": str(calls),
            "ARTICLE_PUBLICATION_GUARD": str(guard),
            "ARTICLE_ZENN_REPO": str(repo),
            "ARTICLE_ZENN_CURRENT_RUN": str(publisher),
        },
        check=True,
    )

    assert calls.read_text(encoding="utf-8").splitlines() == [
        "guard:clear-unavailable --pair zenn-article/ja",
        "guard:register-intent --pair zenn-article/ja "
        "--target-kind zenn-slug --target known-zenn-slug",
        f"publisher:publish --state {path.resolve()} "
        f"--ledger {root / 'articles.jsonl'} --repo {repo}",
    ]


def test_recovers_note_s3_presigned_post_failure_after_token_complete_fix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    path = state_file(root)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["pairs"] = {
        "note/ja": {
            "status": "unavailable",
            "error": "note-body-image-s3-403-embedded-0-of-1",
        }
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    calls = tmp_path / "calls"
    guard = tmp_path / "guard"
    guard.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("guard:" + " ".join(sys.argv[1:]) + "\\n")\n',
        encoding="utf-8",
    )
    upload_guard = executable(tmp_path / "upload-guard", 'printf "upload:%s\\n" "$*" >>"$CALLS"\n')

    subprocess.run(
        ["python3", str(SCRIPT), "--state-root", str(root)],
        env={
            **os.environ,
            "CALLS": str(calls),
            "ARTICLE_PUBLICATION_GUARD": str(guard),
            "ARTICLE_NOTE_S3_UPLOAD_GUARD": str(upload_guard),
        },
        check=True,
    )

    assert calls.read_text(encoding="utf-8").splitlines() == [
        "upload:",
        "guard:clear-unavailable --pair note/ja",
    ]


def test_recovers_historic_stale_quality_rejections_without_touching_gates(
    tmp_path: Path,
) -> None:
    """A persisted pre-fix rejection must resume the same frozen run.

    The quality receipt and immutable drafts were already validated when the
    state was created.  Recovery may only clear the obsolete unavailable
    entries; it must not regenerate drafts or rerun/relax identity or safety.
    """
    root = tmp_path / "state"
    path = state_file(root)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["pairs"] = {
        pair: {
            "status": "unavailable",
            "error": "publication-intent-stale-quality-receipt",
        }
        for pair in ("devto/en", "substack/ja", "substack/en")
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    calls = tmp_path / "calls"
    guard = tmp_path / "guard"
    guard.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("guard:" + " ".join(sys.argv[1:]) + "\\n")\n',
        encoding="utf-8",
    )

    subprocess.run(
        ["python3", str(SCRIPT), "--state-root", str(root)],
        env={
            **os.environ,
            "CALLS": str(calls),
            "ARTICLE_PUBLICATION_GUARD": str(guard),
        },
        check=True,
    )

    assert calls.read_text(encoding="utf-8").splitlines() == [
        "guard:recover-stale-quality --pair devto/en",
        "guard:recover-stale-quality --pair substack/ja",
        "guard:recover-stale-quality --pair substack/en",
    ]


def test_rearms_permission_failure_with_same_stable_targets_only(tmp_path: Path) -> None:
    root = tmp_path / "state"
    path = state_file(root)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["pairs"] = {
        "note/ja": {
            "status": "unavailable",
            "error": "tracked-state-directory-permission-after-draft-create",
            "target_kind": "note-key",
            "target": "n1e88460f58b2",
        },
        "substack/ja": {
            "status": "unavailable",
            "error": "tracked-state-directory-permission-after-draft-create",
            "target_kind": "substack-draft-id",
            "target": "212110259",
        },
        "substack/en": {
            "status": "unavailable",
            "error": "tracked-state-directory-permission-after-draft-create",
            "target_kind": "substack-draft-id",
            "target": "212110268",
        },
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    (root / "articles.jsonl").write_text("", encoding="utf-8")
    (path.parent / "platform-dispatch-results.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "platform": platform,
                    "lang": lang,
                    "status": "failed",
                    "raw_output": "mkdir: /release/skills/writer-agent/state: Permission denied",
                }
            )
            for platform, lang in (("note", "ja"), ("substack", "ja"), ("substack", "en"))
        )
        + "\n",
        encoding="utf-8",
    )
    calls = tmp_path / "calls"
    guard = tmp_path / "guard"
    guard.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("guard:" + " ".join(sys.argv[1:]) + "\\n")\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["python3", str(SCRIPT), "--state-root", str(root), "--run-id", "daily-2026-07-29"],
        env={**os.environ, "CALLS": str(calls), "ARTICLE_PUBLICATION_GUARD": str(guard)},
        check=True,
    )
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "guard:register-intent --pair note/ja --target-kind note-key --target n1e88460f58b2",
        "guard:register-intent --pair substack/ja --target-kind substack-draft-id --target " + "212110259",
        "guard:register-intent --pair substack/en --target-kind substack-draft-id --target " + "212110268",
    ]


def test_permission_failure_recovery_refuses_live_ledger(tmp_path: Path) -> None:
    root = tmp_path / "state"
    path = state_file(root)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["pairs"] = {
        "note/ja": {
            "status": "unavailable",
            "error": "tracked-state-directory-permission-after-draft-create",
            "target_kind": "note-key",
            "target": "n1e88460f58b2",
        }
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    (root / "articles.jsonl").write_text(
        json.dumps(
            {
                "run_id": "daily-2026-07-29",
                "platform": "note",
                "lang": "ja",
                "published": True,
                "live_url": "https://note.com/anicca123/n/n1e88460f58b2",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (path.parent / "platform-dispatch-results.jsonl").write_text(
        json.dumps(
            {
                "platform": "note",
                "lang": "ja",
                "status": "failed",
                "raw_output": "mkdir: /release/skills/writer-agent/state: Permission denied",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls = tmp_path / "calls"
    guard = tmp_path / "guard"
    guard.write_text(
        'import os, sys\n'
        'with open(os.environ["CALLS"], "a", encoding="utf-8") as out:\n'
        '    out.write("called\\n")\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["python3", str(SCRIPT), "--state-root", str(root), "--run-id", "daily-2026-07-29"],
        env={**os.environ, "CALLS": str(calls), "ARTICLE_PUBLICATION_GUARD": str(guard)},
        check=True,
    )
    assert not calls.exists()
