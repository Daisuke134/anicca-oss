#!/usr/bin/env python3
"""Re-arm only unavailable publication failures with a deterministic live proof."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
NOTE_RUNTIME_ERROR = "note_mcp_venv_missing"
NOTE_S3_UPLOAD_ERROR = "note-body-image-s3-403-embedded-0-of-1"
SUBSTACK_BROWSER_ERROR = "substack_editor_redirect_own_eyes_unverified"
SUBSTACK_PROBE_TIMEOUT_SECONDS = 45
ZENN_STAGE_TIMEOUT_ERROR = "zenn-stage-timeout-no-dispatch-result"
TRACKED_STATE_DIRECTORY_ERROR = "tracked-state-directory-permission-after-draft-create"
TRACKED_STATE_PAIRS = ("note/ja", "substack/ja", "substack/en")


def run(command: list[str], *, env: dict[str, str], timeout: int = 180) -> bool:
    try:
        return (
            subprocess.run(
                command,
                env=env,
                check=False,
                timeout=timeout,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def managed_env(state: dict[str, Any], state_path: Path) -> dict[str, str] | None:
    run_dir = Path(str(state.get("run_dir", "")))
    ledger = str(state.get("ledger_path", ""))
    if (
        not run_dir.is_dir()
        or state_path != run_dir / "gates" / "publication-state.json"
        or not ledger
    ):
        return None
    return {
        **os.environ,
        "ARTICLE_AUTOPUBLISH": "1",
        "ARTICLE_RUN_DIR": str(run_dir),
        "ARTICLE_PUBLICATION_STATE": str(state_path),
        "ARTICLE_LEDGER": ledger,
        "ARTICLE_GATES_LOG": str(run_dir / "gates" / "article-gates.log"),
    }


def _tracked_state_failure_proven(
    state: dict[str, Any], pair: str, entry: dict[str, Any]
) -> bool:
    """Prove that staging returned a stable draft before only state archival failed.

    This rule is deliberately narrower than a generic unavailable recovery: it
    accepts one exact dispatch error, an existing target of the platform's
    canonical kind, no receipt, and no current-run live ledger row. The next
    managed worker therefore reuses that target; it never creates another draft.
    """
    if pair not in TRACKED_STATE_PAIRS or entry.get("status") != "unavailable":
        return False
    if entry.get("error") != TRACKED_STATE_DIRECTORY_ERROR or entry.get("receipt"):
        return False
    target_kind = str(entry.get("target_kind", ""))
    target = str(entry.get("target", ""))
    expected_kind = {
        "note/ja": "note-key",
        "substack/ja": "substack-draft-id",
        "substack/en": "substack-draft-id",
    }[pair]
    if target_kind != expected_kind:
        return False
    if pair == "note/ja":
        if re.fullmatch(r"[A-Za-z0-9_-]{4,200}", target) is None:
            return False
    elif re.fullmatch(r"[0-9]{1,30}", target) is None:
        return False

    ledger = Path(str(state.get("ledger_path", "")))
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if not isinstance(row, dict):
                return False
            if (
                row.get("run_id") == state.get("run_id")
                and f"{row.get('platform')}/{row.get('lang')}" == pair
                and (
                    row.get("published") is True
                    or bool(row.get("live_url"))
                    or row.get("reality_gate") == "PASS"
                    or row.get("receipt")
                )
            ):
                return False
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False

    results = Path(str(state.get("run_dir", ""))) / "gates" / "platform-dispatch-results.jsonl"
    try:
        rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("platform") == pair.split("/", 1)[0]
        and row.get("lang") == pair.split("/", 1)[1]
    ]
    if len(matches) != 1 or matches[0].get("status") != "failed":
        return False
    raw = str(matches[0].get("raw_output", ""))
    return "writer-agent/state: Permission denied" in raw


def recover_state(state_path: Path, *, allow_zenn_intent: bool = False) -> None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return
    if not isinstance(state, dict):
        return
    env = managed_env(state, state_path)
    if env is None:
        return

    pairs = state.get("pairs", {})
    if not isinstance(pairs, dict):
        return
    guard = os.environ.get(
        "ARTICLE_PUBLICATION_GUARD", str(SCRIPT_DIR / "publication-guard.py")
    )

    # A release is immutable, so the old run.sh wrote its post-draft metadata
    # into the release and failed after Note/Substack had already returned a
    # stable draft. Re-arm only that exact, no-live-receipt shape with the same
    # target. Do not clear the pair (which would authorize a new create).
    for pair in TRACKED_STATE_PAIRS:
        entry = pairs.get(pair, {})
        if isinstance(entry, dict) and _tracked_state_failure_proven(state, pair, entry):
            run(
                [
                    "python3",
                    guard,
                    "register-intent",
                    "--pair",
                    pair,
                    "--target-kind",
                    str(entry["target_kind"]),
                    "--target",
                    str(entry["target"]),
                ],
                env=env,
            )

    # Before 9efbf289, a continuous-policy terminal receipt with an
    # editorial/reader ADVISORY was rejected by the later intent boundary,
    # then persisted as unavailable. The shared receipt validator now accepts
    # that policy while still requiring current hashes, identity PASS and
    # safety ALLOW. Reopen only this exact historic false negative through
    # the guarded state transition; it rechecks those invariants and never
    # touches a receipted or live destination.
    for pair in ("devto/en", "substack/ja", "substack/en"):
        entry = pairs.get(pair, {})
        if (
            isinstance(entry, dict)
            and entry.get("status") == "unavailable"
            and entry.get("error") == "publication-intent-stale-quality-receipt"
            and not entry.get("receipt")
        ):
            run(
                ["python3", guard, "recover-stale-quality", "--pair", pair],
                env=env,
            )

    note = pairs.get("note/ja", {})
    if (
        isinstance(note, dict)
        and note.get("status") == "unavailable"
        and note.get("error") == NOTE_RUNTIME_ERROR
        and not note.get("receipt")
    ):
        note_source = os.environ.get(
            "NOTE_MCP_SRC",
            str(SCRIPT_DIR.parent / "vendor/note-mcp/src"),
        )
        runtime_python = os.environ.get("WRITER_BROWSER_PYTHON", "python3")
        note_env = {
            **env,
            "NOTE_MCP_SRC": note_source,
            "PYTHONPATH": note_source + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            ),
        }
        if run([runtime_python, "-c", "import note_mcp.api.articles"], env=note_env):
            run(
                ["python3", guard, "clear-unavailable", "--pair", "note/ja"],
                env=env,
            )

    # note-mcp used to drop x-amz-security-token from Note's STS-signed body
    # image POST.  The local uploader now preserves every returned POST field;
    # recompile that exact repaired path before reopening only this known 403.
    if (
        isinstance(note, dict)
        and note.get("status") == "unavailable"
        and note.get("error") == NOTE_S3_UPLOAD_ERROR
        and not note.get("receipt")
    ):
        upload_guard = os.environ.get("ARTICLE_NOTE_S3_UPLOAD_GUARD")
        command = (
            shlex.split(upload_guard)
            if upload_guard
            else [
                "python3", "-m", "py_compile",
                str(SCRIPT_DIR / "note_s3_upload.py"),
                str(SCRIPT_DIR / "note-stage2-publish.py"),
            ]
        )
        if run(command, env=env):
            run(
                ["python3", guard, "clear-unavailable", "--pair", "note/ja"],
                env=env,
            )

    identities = state.get("destination_identities", {})
    if not isinstance(identities, dict):
        identities = {}
    for pair in ("substack/ja", "substack/en"):
        entry = pairs.get(pair, {})
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target", ""))
        if (
            entry.get("status") != "unavailable"
            or entry.get("error") != SUBSTACK_BROWSER_ERROR
            or entry.get("receipt")
            or entry.get("target_kind") != "substack-draft-id"
            or re.fullmatch(r"[1-9][0-9]*", target) is None
        ):
            continue
        account = str(
            identities.get(pair, "aniccabuddha.substack.com")
        ).strip().lower()
        if re.fullmatch(r"[a-z0-9-]+\.substack\.com", account) is None:
            continue
        verifier = os.environ.get(
            "ARTICLE_RENDER_VERIFY", str(SCRIPT_DIR / "render-verify-draft.sh")
        )
        lang = pair.split("/", 1)[1]
        url = f"https://{account}/publish/post/{target}"
        if run(
            [
                verifier,
                "--platform",
                "substack",
                "--url",
                url,
                "--lang",
                lang,
            ],
            env=env,
            timeout=SUBSTACK_PROBE_TIMEOUT_SECONDS,
        ):
            run(
                [
                    "python3",
                    guard,
                    "register-intent",
                    "--pair",
                    pair,
                    "--target-kind",
                    "substack-draft-id",
                    "--target",
                    target,
                ],
                env=env,
            )

    devto = pairs.get("devto/en", {})
    if (
        isinstance(devto, dict)
        and devto.get("status") == "unavailable"
        and devto.get("error")
        == "devto-target-missing-from-owned-drafts-after-publish-put"
        and not devto.get("receipt")
        and devto.get("target_kind") == "devto-article-id"
        and re.fullmatch(r"[1-9][0-9]*", str(devto.get("target", "")))
    ):
        run(
            [
                "python3",
                guard,
                "recover-unavailable",
                "--pair",
                "devto/en",
            ],
            env=env,
        )

    zenn = pairs.get("zenn-article/ja", {})
    zenn_slug = str(zenn.get("target", "")) if isinstance(zenn, dict) else ""
    zenn_repo = Path(
        os.environ.get(
            "ARTICLE_ZENN_REPO",
            str(Path.home() / ".openclaw/workspace/zenn-articles"),
        )
    )
    zenn_article = zenn_repo / "articles" / f"{zenn_slug}.md"
    try:
        zenn_draft = zenn_article.read_text(encoding="utf-8")
    except OSError:
        zenn_draft = ""
    zenn_is_known_timeout = (
        isinstance(zenn, dict)
        and zenn.get("status") == "unavailable"
        and zenn.get("error") == ZENN_STAGE_TIMEOUT_ERROR
    )
    zenn_is_staged_intent = (
        allow_zenn_intent
        and isinstance(zenn, dict)
        and zenn.get("status") == "intent"
    )
    if (
        (zenn_is_known_timeout or zenn_is_staged_intent)
        and not zenn.get("receipt")
        and zenn.get("target_kind") == "zenn-slug"
        and re.fullmatch(r"[a-z0-9_-]{12,50}", zenn_slug)
        and len(re.findall(r"(?m)^published:\s*false\s*$", zenn_draft)) == 1
        and not re.search(r"(?m)^published:\s*true\s*$", zenn_draft)
    ):
        registered = zenn_is_staged_intent
        if zenn_is_known_timeout:
            cleared = run(
                ["python3", guard, "clear-unavailable", "--pair", "zenn-article/ja"],
                env=env,
            )
            registered = cleared and run(
                [
                    "python3",
                    guard,
                    "register-intent",
                    "--pair",
                    "zenn-article/ja",
                    "--target-kind",
                    "zenn-slug",
                    "--target",
                    zenn_slug,
                ],
                env=env,
            )
        publisher = os.environ.get(
            "ARTICLE_ZENN_CURRENT_RUN",
            str(SCRIPT_DIR / "zenn-publish" / "current_run_zenn.py"),
        )
        if registered:
            run(
                [
                    sys.executable,
                    publisher,
                    "publish",
                    "--state",
                    str(state_path),
                    "--ledger",
                    str(state["ledger_path"]),
                    "--repo",
                    str(zenn_repo),
                ],
                env=env,
                timeout=240,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    pattern = (
        f"runs/{args.run_id}/gates/publication-state.json"
        if args.run_id
        else "runs/*/gates/publication-state.json"
    )
    state_paths = sorted(args.state_root.glob(pattern))
    zenn_intent_owner: Path | None = None
    if args.run_id and state_paths:
        zenn_intent_owner = state_paths[0].resolve()
    elif not args.run_id:
        candidates: list[tuple[str, Path]] = []
        for candidate in state_paths:
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
                pairs = value.get("pairs", {})
                active_six = all(
                    pairs.get(pair, {}).get("status") == "skipped"
                    for pair in ("x-article/en", "x-post/ja")
                )
                zenn_intent = pairs.get("zenn-article/ja", {}).get("status") == "intent"
                if active_six and zenn_intent:
                    candidates.append((str(value.get("created_at", "")), candidate.resolve()))
            except (OSError, TypeError, json.JSONDecodeError):
                continue
        if candidates:
            zenn_intent_owner = max(candidates)[1]
    for state_path in state_paths:
        resolved = state_path.resolve()
        recover_state(resolved, allow_zenn_intent=resolved == zenn_intent_owner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
