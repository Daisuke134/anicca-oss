#!/usr/bin/env python3
"""Plan and record an idempotent current-run-only Zenn rate-window retry."""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from article_completion import (
    REQUIRED_LIVE,
    terminal_artifact_complete,
    validate_live_set,
)
from publication_resume import InvariantError as PublicationInvariantError
from publication_resume import PublicationStore
from publication_remote import probe as publication_probe


class InvariantError(ValueError):
    """Permanent queue artifact or canonical-source contract violation."""


class TransientError(RuntimeError):
    """Retryable canonical remote access failure."""


def zenn_account() -> str:
    account = os.environ.get("ZENN_ACCOUNT", "")
    if re.fullmatch(r"[A-Za-z0-9_-]+", account) is None:
        raise InvariantError("ZENN_ACCOUNT is required and invalid")
    return account


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def markdown_title(markdown: str) -> str:
    match = re.search(r"^title:\s*(.+?)\s*$", markdown, re.MULTILINE)
    if not match:
        raise ValueError("Zenn markdown has no title frontmatter")
    raw_title = match.group(1)
    try:
        title = ast.literal_eval(raw_title) if raw_title[:1] in {"'", '"'} else raw_title
    except (SyntaxError, ValueError):
        title = raw_title.strip("\"'")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Zenn title is empty")
    return title


def canonical_source_text(repo: Path, source: Path, slug: str, allow_local_source: bool) -> str:
    """Read immutable origin/main, with an explicit test-fixture-only local fallback."""
    try:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        if allow_local_source:
            return source.read_text(encoding="utf-8")
        raise TransientError("failed to fetch origin/main") from error
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "show", f"FETCH_HEAD:articles/{slug}.md"],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        if allow_local_source:
            return source.read_text(encoding="utf-8")
        raise InvariantError(f"origin/main source missing for slug={slug}") from error


def validate_artifact(artifact: dict[str, Any], repo: Path, run_id: str | None = None,
                      topic_id: str | None = None, allow_local_source: bool = False) -> None:
    slug = artifact.get("slug")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9_-]{12,50}", slug):
        raise InvariantError("invalid deferred Zenn slug")
    if not isinstance(artifact.get("title"), str) or not artifact["title"].strip():
        raise InvariantError("invalid deferred Zenn title")
    if artifact.get("live_url") != f"https://zenn.dev/{zenn_account()}/articles/{slug}":
        raise InvariantError("invalid deferred Zenn live URL")
    expected = (repo / "articles" / f"{slug}.md").resolve()
    declared = Path(str(artifact.get("markdown_file", ""))).resolve()
    if declared != expected:
        raise InvariantError("deferred Zenn source does not match repository slug")
    source = canonical_source_text(repo, expected, slug, allow_local_source)
    if not re.search(r"^published:\s*true\s*$", source, re.MULTILINE):
        raise InvariantError("origin/main source is not published:true")
    try:
        source_title = markdown_title(source)
    except ValueError as error:
        raise InvariantError(str(error)) from error
    if source_title != artifact["title"]:
        raise InvariantError("deferred Zenn title does not match canonical source")
    if run_id is not None and artifact.get("run_id") not in {None, run_id}:
        raise InvariantError("deferred artifact belongs to a different run")
    if topic_id is not None and artifact.get("topic_id") not in {None, topic_id}:
        raise InvariantError("deferred artifact belongs to a different topic")
    if artifact.get("status") not in {"waiting", "pending", "live-recorded", "complete"}:
        raise InvariantError("invalid deferred artifact status")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_api(path: str | None) -> dict[str, Any]:
    if path:
        return read_json(Path(path))
    request = urllib.request.Request(
        f"https://zenn.dev/api/articles?username={zenn_account()}&order=latest",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Zenn API did not return an object")
    return value


def publication_context(
    args: argparse.Namespace,
) -> tuple[PublicationStore, dict[str, Any], dict[str, Any]]:
    artifact_path = Path(args.artifact)
    state_path = Path(
        getattr(args, "publication_state", None)
        or artifact_path.parent / "publication-state.json"
    )
    store = PublicationStore(state_path, Path(args.ledger))
    try:
        state = store.read()
        plan = store.worker_plan()
    except PublicationInvariantError as error:
        raise InvariantError(f"invalid exact8 publication state: {error}") from error
    if state.get("run_id") != args.run_id:
        raise InvariantError("publication state belongs to a different run")
    if plan.get("reason") not in {"pending-publication-pairs", "all-complete"}:
        raise InvariantError(
            f"publication state is not worker-safe: {plan.get('reason', 'unknown')}"
        )
    pair = state.get("pairs", {}).get("zenn-article/ja")
    if (
        not isinstance(pair, dict)
        or pair.get("target_kind") != "zenn-slug"
        or not isinstance(pair.get("target"), str)
    ):
        raise InvariantError("publication state has no stable Zenn intent")
    return store, state, pair


def create(args: argparse.Namespace) -> int:
    markdown = Path(args.markdown_file)
    title = markdown_title(markdown.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-z0-9_-]{12,50}", args.slug):
        raise ValueError("invalid Zenn slug")
    atomic_write(Path(args.artifact), {
        "status": "waiting",
        "slug": args.slug,
        "title": title,
        "live_url": f"https://zenn.dev/{zenn_account()}/articles/{args.slug}",
        "markdown_file": str(markdown.resolve()),
    })
    return 0


def plan(args: argparse.Namespace) -> int:
    artifact = read_json(Path(args.artifact))
    _, state, pair = publication_context(args)
    topic_id = str(state["topic_id"])
    if artifact.get("slug") != pair["target"]:
        raise InvariantError("deferred Zenn slug conflicts with publication intent")
    validate_artifact(
        artifact, Path(args.repo), args.run_id, topic_id, args.allow_local_source
    )
    if pair.get("status") == "live":
        exact_eight, _, exact_topic = validate_live_set(
            ledger_rows(Path(args.ledger)), args.run_id, REQUIRED_LIVE
        )
        if exact_eight and terminal_artifact_complete(
            artifact, args.run_id, exact_topic
        ):
            print(json.dumps({"action": "complete"}, separators=(",", ":")))
            return 0
        print(json.dumps({"action": "finalize"}, separators=(",", ":")))
        return 0
    slug = artifact.get("slug")
    articles = load_api(args.api_json).get("articles", [])
    match = next(
        (
            article
            for article in articles
            if isinstance(article, dict) and article.get("slug") == slug
        ),
        None,
    )
    if match is not None:
        published_at = match.get("published_at")
        if not isinstance(published_at, str):
            raise InvariantError("Zenn API match has no publication timestamp")
        parse_time(published_at)
        print(
            json.dumps(
                {
                    "action": "verify",
                    "slug": slug,
                    "published_at": published_at,
                },
                separators=(",", ":"),
            )
        )
        return 0

    published = []
    for article in articles:
        if isinstance(article, dict) and isinstance(article.get("published_at"), str):
            published.append(parse_time(article["published_at"]))
    if not published:
        print(json.dumps({"action": "retry", "slug": slug}, separators=(",", ":")))
        return 0
    retry_at = max(published) + timedelta(hours=24)
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    wait_seconds = max(0, math.ceil((retry_at - now).total_seconds()))
    action = "wait" if wait_seconds else "retry"
    print(json.dumps({
        "action": action,
        "slug": slug,
        "wait_seconds": wait_seconds,
        "retry_at": retry_at.isoformat(),
    }, separators=(",", ":")))
    return 0


def handoff(args: argparse.Namespace) -> int:
    artifact_path = Path(args.artifact)
    artifact = read_json(artifact_path)
    _, state, pair = publication_context(args)
    topic_id = str(state["topic_id"])
    if artifact.get("slug") != pair["target"]:
        raise InvariantError("deferred Zenn slug conflicts with publication intent")
    validate_artifact(artifact, Path(args.repo), args.run_id, topic_id, args.allow_local_source)
    slug = artifact["slug"]
    if artifact.get("status") == "complete":
        print(json.dumps({"action": "complete", "slug": slug}, separators=(",", ":")))
        return 0

    pair_live = pair.get("status") == "live"
    update = {
        "status": "live-recorded" if pair_live else "pending",
        "run_id": args.run_id,
        "topic_id": topic_id,
        "handed_off_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if pair_live:
        update["recorded_live_url"] = pair.get("receipt", {}).get("live_url")
    artifact.update(update)
    atomic_write(artifact_path, artifact)
    print(json.dumps({"action": "handed-off", "slug": slug}, separators=(",", ":")))
    return 0


def record(args: argparse.Namespace) -> int:
    artifact_path = Path(args.artifact)
    artifact = read_json(artifact_path)
    store, state, pair = publication_context(args)
    topic_id = str(state["topic_id"])
    if artifact.get("slug") != pair["target"]:
        raise InvariantError("deferred Zenn slug conflicts with publication intent")
    validate_artifact(
        artifact, Path(args.repo), args.run_id, topic_id, args.allow_local_source
    )
    if artifact.get("status") not in {"pending", "live-recorded"}:
        raise InvariantError("record requires pending or live-recorded artifact")
    if args.live_url != artifact["live_url"]:
        raise InvariantError("live URL does not match deferred artifact")
    try:
        readback_json = getattr(args, "test_public_readback_json", None)
        if readback_json:
            readback_path = Path(readback_json).resolve(strict=True)
            if (
                not args.allow_local_source
                or os.environ.get("ARTICLE_TEST_ONLY") != "1"
                or not str(readback_path).startswith(("/tmp/", "/private/tmp/"))
            ):
                raise InvariantError("test public readback injection is forbidden")
            evidence = read_json(readback_path)
        else:
            evidence = publication_probe("zenn-article/ja", pair["target"], state)
        if (
            evidence.get("status") != "live"
            or evidence.get("verified") is not True
            or evidence.get("live_url") != args.live_url
            or evidence.get("published_at") != args.published_at
        ):
            raise InvariantError("Zenn public content/media readback is incomplete")
        receipt = store.record_live(
            "zenn-article/ja",
            args.live_url,
            evidence,
        )
    except PublicationInvariantError as error:
        raise InvariantError(f"Zenn receipt conflicts with exact8 state: {error}") from error
    artifact["status"] = "live-recorded"
    artifact["recorded_live_url"] = args.live_url
    artifact["recorded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    atomic_write(artifact_path, artifact)
    print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--artifact", required=True)
    create_parser.add_argument("--markdown-file", required=True)
    create_parser.add_argument("--slug", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--ledger", required=True)
    plan_parser.add_argument("--run-id", required=True)
    plan_parser.add_argument("--artifact", required=True)
    plan_parser.add_argument("--publication-state")
    plan_parser.add_argument("--api-json")
    plan_parser.add_argument("--now")
    plan_parser.add_argument("--repo", default=os.environ.get("ZENN_REPO_PATH", str(Path.home() / ".local/state/life-manager/writer/checkouts/zenn-articles")))
    plan_parser.add_argument("--test-allow-local-source", dest="allow_local_source", action="store_true", help=argparse.SUPPRESS)
    handoff_parser = sub.add_parser("handoff")
    handoff_parser.add_argument("--ledger", required=True)
    handoff_parser.add_argument("--run-id", required=True)
    handoff_parser.add_argument("--artifact", required=True)
    handoff_parser.add_argument("--publication-state")
    handoff_parser.add_argument("--repo", default=os.environ.get("ZENN_REPO_PATH", str(Path.home() / ".local/state/life-manager/writer/checkouts/zenn-articles")))
    handoff_parser.add_argument("--test-allow-local-source", dest="allow_local_source", action="store_true", help=argparse.SUPPRESS)
    record_parser = sub.add_parser("record")
    record_parser.add_argument("--ledger", required=True)
    record_parser.add_argument("--run-id", required=True)
    record_parser.add_argument("--artifact", required=True)
    record_parser.add_argument("--publication-state")
    record_parser.add_argument("--live-url", required=True)
    record_parser.add_argument("--published-at", required=True)
    record_parser.add_argument(
        "--test-public-readback-json",
        help=argparse.SUPPRESS,
    )
    record_parser.add_argument("--repo", default=os.environ.get("ZENN_REPO_PATH", str(Path.home() / ".local/state/life-manager/writer/checkouts/zenn-articles")))
    record_parser.add_argument("--test-allow-local-source", dest="allow_local_source", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return {"create": create, "plan": plan, "handoff": handoff, "record": record}[args.command](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InvariantError as error:
        print(f"INVARIANT: {error}", file=sys.stderr)
        raise SystemExit(2)
    except TransientError as error:
        print(f"TRANSIENT: {error}", file=sys.stderr)
        raise SystemExit(3)
