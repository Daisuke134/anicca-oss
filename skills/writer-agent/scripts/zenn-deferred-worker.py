#!/usr/bin/env python3
"""One-shot launchd worker for durable, Zenn-only rolling-window retries."""

from __future__ import annotations

import argparse
import ast
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from article_completion import REQUIRED_LIVE, terminal_artifact_complete, validate_live_set
from publication_resume import PublicationStore


SCRIPT_DIR = Path(__file__).resolve().parent
CONTROL = SCRIPT_DIR / "zenn-deferred-control.py"


class InvariantViolation(ValueError):
    """Permanent queue or canonical repository contract violation."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def atomic_update(path: Path, **fields: Any) -> dict[str, Any]:
    value = read_json(path) if path.exists() else {}
    value.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return value


def atomic_replace(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def replace_update(path: Path, remove: tuple[str, ...] = (), **fields: Any) -> dict[str, Any]:
    value = read_json(path)
    for key in remove:
        value.pop(key, None)
    value.update(fields)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return value


def append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{iso_now()} {message}\n")


def control_json(command: str, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(CONTROL), command, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError(f"control {command} returned non-object JSON")
    return value


def plan(args: argparse.Namespace, ledger: Path, run_id: str, artifact: Path) -> dict[str, Any]:
    command = [
        "--ledger",
        str(ledger),
        "--run-id",
        run_id,
        "--artifact",
        str(artifact),
        "--publication-state",
        str(artifact.parent / "publication-state.json"),
        "--repo",
        str(args.repo),
    ]
    if args.allow_local_source:
        command.append("--test-allow-local-source")
    if args.api_json:
        command += ["--api-json", args.api_json]
    if args.now:
        command += ["--now", args.now]
    return control_json("plan", *command)


def retain_pending(artifact: Path, action: str, error: str | None = None, **fields: Any) -> None:
    update: dict[str, Any] = {
        "status": "pending",
        "last_action": action,
        "last_checked_at": iso_now(),
    }
    update.update(fields)
    if error:
        update["last_error"] = error[-2000:]
        atomic_update(artifact, **update)
    else:
        replace_update(artifact, remove=("last_error",), **update)


def notify(args: argparse.Namespace, message: str) -> bool:
    if args.notify_bin:
        command = [args.notify_bin, message]
    else:
        command = [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            os.environ.get("TELEGRAM_TARGET_ID", "8547730585"),
            "--message",
            message,
            "--json",
        ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def notify_exact8(
    args: argparse.Namespace,
    artifact_path: Path,
    run_id: str,
    live_url: str,
) -> tuple[bool, str | None]:
    if args.notify_bin:
        message = (
            f"article-daily: run {run_id} Zenn live + reality PASS; "
            f"exact8 complete: {live_url}"
        )
        return notify(args, message), "test-notify-bin"
    command = [
        sys.executable,
        str(SCRIPT_DIR / "article-completion-notify.py"),
        "--state",
        str(artifact_path.parent / "publication-state.json"),
        "--ledger",
        str(args.ledger),
        "--target",
        os.environ.get("TELEGRAM_TARGET_ID", "8547730585"),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        receipt = json.loads(result.stdout)
    except (
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ):
        return False, None
    message_id = str(receipt.get("message_id", "")).strip()
    return receipt.get("status") == "sent" and bool(message_id), message_id or None


def control_invariant(error: BaseException) -> bool:
    return isinstance(error, subprocess.CalledProcessError) and "INVARIANT:" in (error.stderr or "")


def control_transient(error: BaseException) -> bool:
    return isinstance(error, subprocess.CalledProcessError) and "TRANSIENT:" in (error.stderr or "")


def control_initialization_pending(error: BaseException) -> bool:
    return (
        isinstance(error, subprocess.CalledProcessError)
        and "INVARIANT: publication state is not worker-safe: missing-targets"
        in (error.stderr or "")
    )


def stale_worker_plan_recovered(
    args: argparse.Namespace,
    artifact_path: Path,
    run_id: str,
) -> bool:
    try:
        store = PublicationStore(
            artifact_path.parent / "publication-state.json",
            args.ledger,
        )
        state = store.read()
        decision = store.worker_plan()
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        state.get("run_id") == run_id
        and decision.get("resumable") is True
        and decision.get("reason") == "pending-publication-pairs"
        and decision.get("pending_pairs") == ["zenn-article/ja"]
    )


def deliver_quarantine_notice(args: argparse.Namespace, artifact: Path, run_id: str, reason: str) -> None:
    sent = notify(args, f"Zenn retry run {run_id} quarantined: {reason[-500:]}")
    atomic_update(
        artifact,
        quarantine_notification_status="sent" if sent else "failed",
        quarantine_notification_checked_at=iso_now(),
    )


def quarantine(args: argparse.Namespace, artifact: Path, run_id: str, reason: str) -> None:
    replace_update(
        artifact,
        status="quarantined",
        last_action="quarantined",
        quarantined_at=iso_now(),
        last_error=reason[-2000:],
    )
    append_log(args.log, f"run={run_id} quarantined permanent invariant: {reason}")
    deliver_quarantine_notice(args, artifact, run_id, reason)


def canonical_remote(value: str) -> str:
    value = value.removesuffix(".git").rstrip("/")
    match = re.fullmatch(r"git@github\.com:(.+)", value)
    if match:
        return f"github.com/{match.group(1)}"
    return value.removeprefix("https://").removeprefix("ssh://git@")


def frontmatter_title(markdown: str) -> str:
    match = re.search(r"^title:\s*(.+?)\s*$", markdown, re.MULTILINE)
    if not match:
        raise ValueError("origin/main source has no title")
    raw = match.group(1)
    try:
        title = ast.literal_eval(raw) if raw[:1] in {"'", '"'} else raw
    except (SyntaxError, ValueError):
        title = raw.strip("\"'")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("origin/main source has empty title")
    return title


def prepare_remote_retry(args: argparse.Namespace, artifact: dict[str, Any], slug: str) -> str:
    repo = args.repo
    try:
        remote = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise InvariantViolation("origin remote is missing") from error
    if canonical_remote(remote) != canonical_remote(args.expected_remote):
        raise InvariantViolation(f"unexpected origin remote: {remote}")
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "origin", "main"],
        check=True, capture_output=True, text=True,
    )
    try:
        base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise InvariantViolation("origin/main is missing after fetch") from error
    try:
        source = subprocess.run(
            ["git", "-C", str(repo), "show", f"{base}:articles/{slug}.md"],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise InvariantViolation(f"origin/main source missing for slug={slug}") from error
    if not re.search(r"^published:\s*true\s*$", source, re.MULTILINE):
        raise InvariantViolation("origin/main source is not published:true")
    try:
        source_title = frontmatter_title(source)
    except ValueError as error:
        raise InvariantViolation(str(error)) from error
    if source_title != artifact.get("title"):
        raise InvariantViolation("origin/main title does not match deferred artifact")
    marker = f"<!-- zenn-deferred-retry:{base} -->"
    retry_source = re.sub(
        r"(?m)^<!-- zenn-deferred-retry:[0-9a-f]{40} -->\n?",
        "",
        source,
    )
    retry_source = retry_source.rstrip("\n") + f"\n{marker}\n"
    with tempfile.TemporaryDirectory(
        prefix=".zenn-deferred-index-", dir=repo.parent
    ) as temporary:
        index_env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        subprocess.run(
            ["git", "-C", str(repo), "read-tree", base],
            check=True, capture_output=True, text=True, env=index_env,
        )
        blob = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            check=True, capture_output=True, text=True, input=retry_source,
        ).stdout.strip()
        subprocess.run(
            [
                "git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                "100644", blob, f"articles/{slug}.md",
            ],
            check=True, capture_output=True, text=True, env=index_env,
        )
        tree = subprocess.run(
            ["git", "-C", str(repo), "write-tree"],
            check=True, capture_output=True, text=True, env=index_env,
        ).stdout.strip()
    commit_env = os.environ.copy()
    commit_env.update({
        "GIT_AUTHOR_NAME": os.environ["ZENN_GIT_NAME"],
        "GIT_AUTHOR_EMAIL": os.environ["ZENN_GIT_EMAIL"],
        "GIT_COMMITTER_NAME": os.environ["ZENN_GIT_NAME"],
        "GIT_COMMITTER_EMAIL": os.environ["ZENN_GIT_EMAIL"],
    })
    commit = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", base,
         "-m", f"article(retry): {slug} after Zenn rate window"],
        check=True, capture_output=True, text=True, env=commit_env,
    ).stdout.strip()
    if args.before_push_hook:
        subprocess.run([str(args.before_push_hook)], check=True, env={**os.environ, "ZENN_BASE": base})
    return commit


def push_remote(args: argparse.Namespace, commit: str) -> None:
    if args.test_push_bin:
        subprocess.run(
            [str(args.test_push_bin), str(args.repo), commit],
            check=True, capture_output=True, text=True,
        )
        return
    push_env = os.environ.copy()
    push_env.setdefault(
        "GIT_SSH_COMMAND", f"ssh -i {Path.home() / '.ssh/id_ed25519'} -o IdentitiesOnly=yes"
    )
    subprocess.run(
        ["git", "-C", str(args.repo), "push", "origin", f"{commit}:refs/heads/main"],
        check=True, capture_output=True, text=True, env=push_env,
    )


def backlog_snapshot(artifacts: list[Path]) -> tuple[int, str | None, int]:
    entries: list[tuple[datetime, str]] = []
    now = datetime.now(timezone.utc)
    for path in artifacts:
        try:
            artifact = read_json(path)
            if artifact.get("status") not in {"waiting", "pending", "live-recorded"}:
                continue
            raw = artifact.get("handed_off_at")
            if isinstance(raw, str):
                created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            entries.append((created, str(artifact.get("run_id") or path.parent.parent.name)))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    if not entries:
        return 0, None, 0
    created, run_id = min(entries)
    return len(entries), run_id, max(0, int((now - created).total_seconds()))


def report_backlog(args: argparse.Namespace, artifacts: list[Path]) -> None:
    count, oldest_run, oldest_age = backlog_snapshot(artifacts)
    append_log(args.log, f"backlog count={count} oldest_run={oldest_run or '-'} oldest_age_seconds={oldest_age}")
    if count < args.backlog_alert_count and oldest_age < args.backlog_alert_seconds:
        return
    signature = f"{count}:{oldest_run}:{oldest_age // 86400}"
    previous: dict[str, Any] = {}
    try:
        previous = read_json(args.backlog_state)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    if previous.get("signature") == signature:
        return
    message = f"Zenn retry backlog: count={count} oldest_run={oldest_run} oldest_age={oldest_age}s"
    if notify(args, message):
        atomic_replace(args.backlog_state, {"signature": signature, "notified_at": iso_now()})


def process_artifact(args: argparse.Namespace, artifact_path: Path) -> bool:
    try:
        artifact = read_json(artifact_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        append_log(args.log, f"invalid artifact={artifact_path}: {error}")
        return False

    if artifact.get("status") == "quarantined":
        run_id = str(artifact.get("run_id") or artifact_path.parent.parent.name)
        last_error = str(artifact.get("last_error", ""))
        if (
            "INVARIANT: publication state is not worker-safe: missing-targets"
            in last_error
        ):
            artifact = replace_update(
                artifact_path,
                remove=(
                    "last_error",
                    "quarantined_at",
                    "quarantine_notification_status",
                    "quarantine_notification_checked_at",
                ),
                status="waiting",
                last_action="initialization-recheck",
                last_checked_at=iso_now(),
            )
            append_log(args.log, f"run={run_id} initialization quarantine recovered")
        elif (
            "INVARIANT: publication state is not worker-safe: ambiguous-target-or-reality"
            in last_error
            and stale_worker_plan_recovered(args, artifact_path, run_id)
        ):
            artifact = replace_update(
                artifact_path,
                remove=(
                    "last_error",
                    "quarantined_at",
                    "quarantine_notification_status",
                    "quarantine_notification_checked_at",
                ),
                status="waiting",
                last_action="worker-plan-recheck",
                last_checked_at=iso_now(),
            )
            append_log(
                args.log,
                f"run={run_id} stale worker-plan quarantine recovered",
            )
        else:
            if artifact.get("quarantine_notification_status") != "sent":
                deliver_quarantine_notice(
                    args,
                    artifact_path,
                    run_id,
                    str(artifact.get("last_error", "unknown invariant")),
                )
            return False
    run_id = artifact.get("run_id") or artifact_path.parent.parent.name
    if not isinstance(run_id, str) or not run_id:
        run_id = artifact_path.parent.parent.name
        quarantine(args, artifact_path, run_id, "invalid run id")
        return False
    if artifact.get("status") not in {"waiting", "pending", "live-recorded", "complete"}:
        quarantine(args, artifact_path, run_id, "invalid deferred artifact status")
        return False
    if artifact.get("status") == "complete":
        exact_eight, _, topic_id = validate_live_set(
            ledger_rows(args.ledger), run_id, REQUIRED_LIVE
        )
        if exact_eight and terminal_artifact_complete(artifact, run_id, topic_id):
            return False
        append_log(args.log, f"run={run_id} complete artifact is immutable; incomplete terminal evidence ignored")
        return False

    if artifact.get("status") == "waiting":
        try:
            handoff = control_json(
                "handoff",
                "--ledger",
                str(args.ledger),
                "--run-id",
                run_id,
                "--artifact",
                str(artifact_path),
                "--publication-state",
                str(artifact_path.parent / "publication-state.json"),
                "--repo",
                str(args.repo),
                *(["--test-allow-local-source"] if args.allow_local_source else []),
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
            if control_initialization_pending(error):
                retain_pending(
                    artifact_path,
                    "initialization-pending",
                    error.stderr.strip(),
                )
                append_log(
                    args.log,
                    f"run={run_id} initialization incomplete; pending retained",
                )
            elif control_invariant(error):
                quarantine(args, artifact_path, run_id, error.stderr.strip())
            elif control_transient(error):
                append_log(args.log, f"handoff transient fetch failure run={run_id}; later queue items continue")
            else:
                append_log(args.log, f"handoff failed run={run_id}: {error}")
            return False
        if handoff.get("action") != "handed-off":
            append_log(args.log, f"handoff skipped run={run_id} action={handoff.get('action')}")
            return False
        artifact = read_json(artifact_path)

    try:
        decision = plan(args, args.ledger, run_id, artifact_path)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        if control_initialization_pending(error):
            retain_pending(
                artifact_path,
                "initialization-pending",
                error.stderr.strip(),
            )
            append_log(
                args.log,
                f"run={run_id} initialization incomplete; pending retained",
            )
        elif control_invariant(error):
            quarantine(args, artifact_path, run_id, error.stderr.strip())
        elif control_transient(error):
            retain_pending(artifact_path, "fetch-error", error.stderr.strip())
            append_log(args.log, f"run={run_id} transient fetch failure; pending retained and later queue items continue")
        else:
            retain_pending(artifact_path, "network-error", str(error))
            append_log(args.log, f"run={run_id} network/API error; pending retained: {error}")
        return False

    action = decision.get("action")
    if action == "complete":
        return False
    if action == "ineligible":
        quarantine(args, artifact_path, run_id, str(decision.get("reason", "unknown")))
        return False
    if action == "wait":
        retry_at = decision.get("retry_at")
        retain_pending(artifact_path, "waiting", retry_at=retry_at)
        append_log(args.log, f"run={run_id} window closed until {retry_at}; pending retained")
        return False
    if action not in {"retry", "verify", "finalize"}:
        retain_pending(artifact_path, "invalid-plan", json.dumps(decision, ensure_ascii=False))
        append_log(args.log, f"run={run_id} invalid plan; pending retained")
        return False

    slug = artifact.get("slug")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9_-]{12,50}", slug):
        quarantine(args, artifact_path, run_id, "invalid slug")
        return False
    source = (args.repo / "articles" / f"{slug}.md").resolve()
    declared_source = Path(str(artifact.get("markdown_file", ""))).resolve()
    if source != declared_source:
        quarantine(args, artifact_path, run_id, "artifact source does not match repository slug")
        return False
    push_budget_consumed = False
    if action == "retry":
        try:
            commit = prepare_remote_retry(args, artifact, slug)
        except InvariantViolation as error:
            quarantine(args, artifact_path, run_id, str(error))
            return False
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            retain_pending(artifact_path, "fetch-error", str(error))
            append_log(args.log, f"run={run_id} pre-push fetch/preparation failed; pending retained; scan continues: {error}")
            return False
        # From this point the remote outcome can be ambiguous. Conservatively consume
        # the scan's one-push budget before starting the push subprocess.
        push_budget_consumed = True
        try:
            push_remote(args, commit)
            atomic_update(artifact_path, last_retriggered_at=iso_now())
            append_log(args.log, f"run={run_id} retriggered same slug={slug}")
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            retain_pending(artifact_path, "network-error", str(error))
            append_log(args.log, f"run={run_id} push outcome ambiguous/failed; pending retained; scan budget consumed: {error}")
            return True

    live_url = str(artifact["live_url"])
    title = str(artifact["title"])
    if action != "finalize":
        try:
            verdict = subprocess.run(
                ["bash", str(args.reality_gate), "ssr", live_url, title],
                check=True,
                capture_output=True,
                text=True,
            )
            if "VERDICT=PASS" not in verdict.stdout:
                raise subprocess.CalledProcessError(1, verdict.args, verdict.stdout, verdict.stderr)
        except (OSError, subprocess.CalledProcessError) as error:
            retain_pending(artifact_path, "reality-pending", str(error))
            append_log(args.log, f"run={run_id} Zenn still 403/not live; pending retained")
            return push_budget_consumed

        published_at = decision.get("published_at")
        if not isinstance(published_at, str) or not published_at:
            retain_pending(
                artifact_path,
                "publication-timestamp-pending",
                "Zenn API has not exposed the stable slug publication timestamp",
            )
            append_log(
                args.log,
                f"run={run_id} Zenn reality passed without remote published_at; pending retained",
            )
            return push_budget_consumed
        try:
            control_json(
                "record",
                "--ledger",
                str(args.ledger),
                "--run-id",
                run_id,
                "--artifact",
                str(artifact_path),
                "--publication-state",
                str(artifact_path.parent / "publication-state.json"),
                "--live-url",
                live_url,
                "--published-at",
                published_at,
                "--repo",
                str(args.repo),
                *(["--test-allow-local-source"] if args.allow_local_source else []),
                *(
                    ["--test-public-readback-json", args.test_public_readback_json]
                    if args.test_public_readback_json
                    else []
                ),
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
            if control_invariant(error):
                quarantine(args, artifact_path, run_id, error.stderr.strip())
            elif control_transient(error):
                retain_pending(artifact_path, "fetch-error", error.stderr.strip())
                append_log(args.log, f"run={run_id} record fetch failure; pending retained")
            else:
                retain_pending(artifact_path, "record-error", str(error))
                append_log(args.log, f"run={run_id} ledger record failed; pending retained: {error}")
            return push_budget_consumed
    try:
        subprocess.run(
            [
                sys.executable,
                str(args.complete_bin),
                "--ledger",
                str(args.ledger),
                "--run-id",
                run_id,
                "--armed",
                "1",
                "--publication-state",
                str(artifact_path.parent / "publication-state.json"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        replace_update(
            artifact_path,
            remove=("last_error",),
            status="live-recorded",
            completion_check="pending-exact8",
            last_action="waiting-for-exact8",
        )
        append_log(
            args.log,
            f"run={run_id} Zenn row recorded; exact8 remains pending: {error}",
        )
        return push_budget_consumed
    args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
    args.heartbeat.touch()
    heartbeat_at = iso_now()
    atomic_update(artifact_path, status="live-recorded", completion_check="passed", heartbeat_at=heartbeat_at)
    notified, notification_message_id = notify_exact8(
        args,
        artifact_path,
        run_id,
        live_url,
    )
    if notified:
        replace_update(
            artifact_path,
            remove=("last_error",),
            status="complete",
            completed_at=iso_now(),
            completed_live_url=live_url,
            heartbeat_at=heartbeat_at,
            notification_status="sent",
            notification_message_id=notification_message_id,
            last_action="complete",
        )
    else:
        atomic_update(
            artifact_path,
            status="live-recorded",
            notification_status="failed",
            last_action="notification-pending",
            last_error="completion notification failed",
        )
    append_log(
        args.log,
        f"run={run_id} exact8 complete url={live_url} "
        f"notification={'sent' if notified else 'failed'}",
    )
    return push_budget_consumed


def parse_args() -> argparse.Namespace:
    skill_root = SCRIPT_DIR.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=skill_root / "state/articles.jsonl")
    parser.add_argument("--runs-root", type=Path, default=skill_root / "state/runs")
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("ZENN_REPO_PATH", str(Path.home() / ".local/state/life-manager/writer/checkouts/zenn-articles"))))
    parser.add_argument("--expected-remote", default=os.environ.get("ZENN_REPOSITORY_URL", ""))
    parser.add_argument("--before-push-hook", type=Path)
    parser.add_argument("--test-allow-local-source", dest="allow_local_source", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--test-public-readback-json", help=argparse.SUPPRESS)
    parser.add_argument("--test-push-bin", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--api-json")
    parser.add_argument("--now")
    parser.add_argument("--reality-gate", type=Path, default=SCRIPT_DIR / "reality-gate.sh")
    parser.add_argument("--complete-bin", type=Path, default=SCRIPT_DIR / "article-run-complete.py")
    parser.add_argument("--heartbeat", type=Path, default=Path(os.environ.get("WRITER_STATE_DIR", str(Path.home() / ".local/state/life-manager/writer"))) / ".article-loop-last-pass")
    parser.add_argument("--notify-bin")
    parser.add_argument("--log", type=Path, default=Path(os.environ.get("WRITER_LOG_DIR", str(Path.home() / ".local/state/life-manager/writer/logs"))) / "article-zenn-retry.log")
    parser.add_argument("--lock-file", type=Path, default=skill_root / "state/.zenn-deferred-worker.lock")
    parser.add_argument(
        "--publication-lock-dir",
        type=Path,
        default=skill_root / "state/.article-daily.lockdir",
    )
    parser.add_argument("--backlog-state", type=Path, default=skill_root / "state/.zenn-deferred-backlog.json")
    parser.add_argument("--backlog-alert-count", type=int, default=2)
    parser.add_argument("--backlog-alert-seconds", type=int, default=86400)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with args.lock_file.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            append_log(args.log, "lock busy; another Zenn retry scan owns the queue")
            return 0

        try:
            args.publication_lock_dir.mkdir()
        except FileExistsError:
            append_log(args.log, "shared publication lock is held")
            return 0
        try:
            pushed = False
            artifacts = sorted(args.runs_root.glob("*/gates/zenn-deferred.json"))
            append_log(args.log, f"scan start artifacts={len(artifacts)}")
            try:
                report_backlog(args, artifacts)
            except Exception as error:
                append_log(args.log, f"backlog reporting failed; queue scan continues: {error}")
            for artifact in artifacts:
                if pushed:
                    break
                pushed = process_artifact(args, artifact)
            append_log(args.log, "scan complete")
        finally:
            try:
                args.publication_lock_dir.rmdir()
            except OSError as error:
                append_log(args.log, f"shared publication lock cleanup failed: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
