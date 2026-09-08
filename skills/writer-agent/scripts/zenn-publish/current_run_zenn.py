#!/usr/bin/env python3
"""Stage and publish the imported frozen run at its registered Zenn slug."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
# --- fail-closed PII gate wiring ---------------------------------------------------
# gate_* raise SystemExit (non-zero) on a finding, a missing blocklist, an unreadable
# artifact, or ANY scanner error. There is no code path where a failure means publish.
import sys as _pii_sys  # noqa: E402
from pathlib import Path as _PiiPath  # noqa: E402
_pii_sys.path.insert(0, str(next(
    _p / "_shared"
    for _p in _PiiPath(__file__).resolve().parents
    if (_p / "_shared" / "pii_gate.py").is_file()
)))
from pii_gate import gate_files, gate_run_dir, gate_text  # noqa: E402,F401

from typing import Any


class ZennCurrentRunRefused(RuntimeError):
    pass


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from canonical_media import MediaContractError, strip_media_contract  # noqa: E402

SLUG = re.compile(r"[a-z0-9_-]{12,50}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
    )


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ZennCurrentRunRefused("publication state is malformed")
    return value


def _immutable(descriptor: dict[str, Any], label: str) -> Path:
    path = Path(str(descriptor.get("path", "")))
    if not path.is_file() or sha256(path) != descriptor.get("sha256"):
        raise ZennCurrentRunRefused(
            f"immutable {label} is missing or changed"
        )
    return path


def _copy_exact(source: Path, destination: Path) -> None:
    if destination.exists():
        if not destination.is_file() or sha256(destination) != sha256(source):
            raise ZennCurrentRunRefused(
                f"Zenn destination conflicts: {destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.tmp"
    )
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def _heading(source: str) -> tuple[str, str]:
    match = re.match(r"^\s*#{1,6}\s+(.+?)\s*(?:\n|$)", source)
    if match is None:
        raise ZennCurrentRunRefused(
            "frozen Japanese draft has no leading title heading"
        )
    title = match.group(1).strip()
    if not title or len(title) > 70:
        raise ZennCurrentRunRefused("frozen Zenn title is invalid")
    return title, source[match.end() :].lstrip("\n")


def build(state_path: Path, repo: Path) -> dict[str, Any]:
    state_path = Path(state_path)
    state = _load(state_path)
    run_id = str(state.get("run_id", ""))
    if not re.fullmatch(r"daily-\d{4}-\d{2}-\d{2}", run_id):
        raise ZennCurrentRunRefused("current run ID is invalid")
    pair = state.get("pairs", {}).get("zenn-article/ja", {})
    slug = str(pair.get("target", ""))
    if (
        pair.get("target_kind") != "zenn-slug"
        or SLUG.fullmatch(slug) is None
        or pair.get("status") not in {"intent", "pending", "live"}
    ):
        raise ZennCurrentRunRefused(
            "publication state has no registered pending Zenn slug"
        )
    source = _immutable(
        state.get("drafts", {}).get("ja", {}),
        "Japanese draft",
    )
    media = state.get("media", {})
    headline = media.get("headline_image", {})
    body_assets = [
        item
        for item in media.get("body_assets", [])
        if isinstance(item, dict)
    ]
    if len(body_assets) != 1:
        raise ZennCurrentRunRefused(
            "current Zenn representation requires one body diagram"
        )
    items = [headline, *body_assets]
    sources = [
        _immutable(item, f"media {index}")
        for index, item in enumerate(items, 1)
    ]
    media_dir = Path(repo) / "images" / run_id
    for media_source in sources:
        _copy_exact(media_source, media_dir / media_source.name)
    frozen = source.read_text(encoding="utf-8")
    try:
        frozen = strip_media_contract(frozen)
    except MediaContractError as error:
        raise ZennCurrentRunRefused(str(error)) from error
    title, body = _heading(frozen)
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    diagram_name = sources[1].name
    rendered = (
        "---\n"
        f'title: "{escaped}"\n'
        'emoji: "🔁"\n'
        'type: "tech"\n'
        'topics: ["ai", "agent", "automation"]\n'
        "published: false\n"
        "---\n\n"
        f"{body.rstrip()}\n\n"
        f"![](/images/{run_id}/{diagram_name})\n\n"
        f"<!-- article-run:{run_id} "
        f"source-sha256:{state['drafts']['ja']['sha256']} -->\n"
    )
    article = Path(repo) / "articles" / f"{slug}.md"
    if article.exists() and article.read_text(encoding="utf-8") != rendered:
        raise ZennCurrentRunRefused(
            "registered Zenn article path already has different bytes"
        )
    _atomic_text(article, rendered)
    return {
        "run_id": run_id,
        "slug": slug,
        "title": title,
        "article": str(article),
        "media": [str(media_dir / item.name) for item in sources],
    }


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "GIT_SSH_COMMAND": (
            f"ssh -i {os.environ.get('ZENN_SSH_KEY', str(Path.home() / '.ssh/id_ed25519'))} "
            "-o IdentitiesOnly=yes"
        ),
    }
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            *arguments,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _checked_git(repo: Path, *arguments: str) -> str:
    result = _git(repo, *arguments)
    if result.returncode != 0:
        raise ZennCurrentRunRefused(
            result.stderr.strip()
            or f"Zenn git {' '.join(arguments)} failed"
        )
    return result.stdout


def _push_main_rebased(repo: Path) -> None:
    """Retry one dedicated content commit after a concurrent main advance."""
    first = _git(repo, "push", "origin", "HEAD:main")
    if first.returncode == 0:
        return
    error = first.stderr.strip()
    lowered = error.lower()
    if "non-fast-forward" not in lowered and "fetch first" not in lowered:
        raise ZennCurrentRunRefused(
            error or "Zenn git push origin HEAD:main failed"
        )
    _checked_git(repo, "fetch", "origin", "main")
    rebased = _git(repo, "rebase", "origin/main")
    if rebased.returncode != 0:
        _git(repo, "rebase", "--abort")
        raise ZennCurrentRunRefused(
            rebased.stderr.strip()
            or "Zenn git rebase origin/main failed"
        )
    _checked_git(repo, "push", "origin", "HEAD:main")


def _raw_media_base(repo: Path) -> str:
    override = os.environ.get("ARTICLE_MEDIA_RAW_BASE", "").rstrip("/")
    if override:
        return override
    raise ZennCurrentRunRefused(
        "ARTICLE_MEDIA_RAW_BASE is required for Zenn media"
    )


def _refuse_unrelated_staged(
    repo: Path,
    allowed: set[str],
) -> None:
    names = set(
        filter(
            None,
            _checked_git(repo, "diff", "--cached", "--name-only")
            .splitlines(),
        )
    )
    unrelated = sorted(names - allowed)
    if unrelated:
        raise ZennCurrentRunRefused(
            f"Zenn repository has unrelated staged paths: {unrelated}"
        )


def stage_media(state_path: Path, repo: Path) -> dict[str, Any]:
    """Publish only one run's immutable media, independent of its Zenn article."""
    state_path = Path(state_path)
    repo = Path(repo)
    if not (repo / ".git").is_dir():
        raise ZennCurrentRunRefused("Zenn repository is missing")
    state = _load(state_path)
    run_id = str(state.get("run_id", ""))
    if re.fullmatch(
        r"(?:daily-\d{4}-\d{2}-\d{2}|\d{8}-\d{6})", run_id
    ) is None:
        raise ZennCurrentRunRefused("current run ID is invalid")
    media = state.get("media", {})
    descriptors = [
        media.get("headline_image", {}),
        *[
            item
            for item in media.get("body_assets", [])
            if isinstance(item, dict)
        ],
    ]
    if len(descriptors) < 2:
        raise ZennCurrentRunRefused(
            "immutable headline and body media are required"
        )
    relative_media: list[str] = []
    for index, descriptor in enumerate(descriptors, 1):
        source = _immutable(descriptor, f"media {index}")
        relative = f"images/{run_id}/{source.name}"
        _copy_exact(source, repo / relative)
        relative_media.append(relative)
    allowed = set(relative_media)
    _checked_git(repo, "add", *sorted(allowed))
    _refuse_unrelated_staged(repo, allowed)
    if _git(
        repo, "diff", "--cached", "--quiet", "--", *sorted(allowed)
    ).returncode != 0:
        _checked_git(
            repo,
            "commit",
            "-m",
            f"media: exact8 {run_id}",
            "--",
            *sorted(allowed),
        )
    _push_main_rebased(repo)
    raw_base = _raw_media_base(repo)
    receipt = {
        "version": 1,
        "run_id": run_id,
        "assets": [
            {
                "path": descriptor["path"],
                "sha256": descriptor["sha256"],
                "url": (
                    f"{raw_base}/{run_id}/"
                    f"{Path(str(descriptor['path'])).name}"
                ),
            }
            for descriptor in descriptors
        ],
    }
    receipt_path = state_path.parent / "media-urls.json"
    _atomic_json(receipt_path, receipt)
    return {
        "run_id": run_id,
        "media": [str(repo / path) for path in relative_media],
        "media_receipt": str(receipt_path),
    }


def stage(state_path: Path, repo: Path) -> dict[str, Any]:
    repo = Path(repo)
    if not (repo / ".git").is_dir():
        raise ZennCurrentRunRefused("Zenn repository is missing")
    result = build(state_path, repo)
    relative_article = f"articles/{result['slug']}.md"
    relative_media = [
        str(Path(path).relative_to(repo)) for path in result["media"]
    ]
    allowed = {relative_article, *relative_media}
    _checked_git(repo, "add", *sorted(allowed))
    _refuse_unrelated_staged(repo, allowed)
    if _git(
        repo, "diff", "--cached", "--quiet", "--", *sorted(allowed)
    ).returncode != 0:
        _checked_git(
            repo,
            "commit",
            "-m",
            f"article(draft): {result['slug']}",
            "--",
            *sorted(allowed),
        )
    _push_main_rebased(repo)
    state = _load(Path(state_path))
    assets = [
        state["media"]["headline_image"],
        *state["media"]["body_assets"],
    ]
    raw_base = _raw_media_base(repo)
    media_receipt = {
        "version": 1,
        "run_id": result["run_id"],
        "assets": [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "url": (
                    f"{raw_base}/{result['run_id']}/"
                    f"{Path(item['path']).name}"
                ),
            }
            for item in assets
        ],
    }
    receipt_path = Path(state_path).parent / "media-urls.json"
    _atomic_json(receipt_path, media_receipt)
    result["media_receipt"] = str(receipt_path)
    return result


def _guard(
    state_path: Path,
    ledger: Path,
    command: str,
    slug: str,
) -> dict[str, Any]:
    arguments = [
        sys.executable,
        str(SCRIPTS / "publication-guard.py"),
        command,
        "--pair",
        "zenn-article/ja",
    ]
    if command == "preflight":
        arguments.extend(
            ["--target-kind", "zenn-slug", "--target", slug]
        )
    environment = {
        **os.environ,
        "ARTICLE_PUBLICATION_STATE": str(state_path),
        "ARTICLE_LEDGER": str(ledger),
        "ARTICLE_RUN_DIR": str(state_path.parent.parent),
        "ARTICLE_AUTOPUBLISH": "1",
    }
    result = subprocess.run(
        arguments,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ZennCurrentRunRefused(
            result.stderr.strip() or "Zenn publication guard refused"
        )
    return json.loads(result.stdout)


def publish(
    state_path: Path,
    ledger: Path,
    repo: Path,
) -> dict[str, Any]:
    state_path = Path(state_path)
    ledger = Path(ledger)
    repo = Path(repo)
    state = _load(state_path)
    pair = state.get("pairs", {}).get("zenn-article/ja", {})
    slug = str(pair.get("target", ""))
    decision = _guard(state_path, ledger, "preflight", slug)
    if decision.get("action") == "skip-live":
        return decision
    if decision.get("action") != "publish":
        raise ZennCurrentRunRefused("Zenn guard did not authorize publish")
    article = repo / "articles" / f"{slug}.md"
    if not article.is_file():
        raise ZennCurrentRunRefused("registered Zenn draft is missing")
    draft = article.read_text(encoding="utf-8")
    if len(re.findall(r"(?m)^published:\s*false\s*$", draft)) != 1:
        raise ZennCurrentRunRefused(
            "registered Zenn draft is not exactly published:false"
        )
    gate_text("zenn-current-run-publish", {str(article): draft})
    live = re.sub(
        r"(?m)^published:\s*false\s*$",
        "published: true",
        draft,
        count=1,
    )
    _atomic_text(article, live)
    deferred = state_path.parent / "zenn-deferred.json"
    create = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "zenn-deferred-control.py"),
            "create",
            "--artifact",
            str(deferred),
            "--markdown-file",
            str(article),
            "--slug",
            slug,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if create.returncode != 0:
        _atomic_text(article, draft)
        raise ZennCurrentRunRefused(
            create.stderr.strip()
            or "Zenn deferred retry artifact could not be created"
        )
    relative = f"articles/{slug}.md"
    _checked_git(repo, "add", relative)
    _refuse_unrelated_staged(repo, {relative})
    _checked_git(
        repo,
        "commit",
        "-m",
        f"article(publish): {slug} LIVE",
        "--",
        relative,
    )
    _push_main_rebased(repo)
    wait_seconds = int(os.environ.get("ARTICLE_ZENN_VERIFY_WAIT", "35"))
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    try:
        return _guard(state_path, ledger, "reconcile", slug)
    except ZennCurrentRunRefused:
        return {
            "action": "deferred",
            "slug": slug,
            "artifact": str(deferred),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("media", "stage", "publish"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--state",
            type=Path,
            default=Path(
                os.environ.get("ARTICLE_PUBLICATION_STATE", "")
            ),
        )
        child.add_argument(
            "--repo",
            type=Path,
        default=Path(
            os.environ.get(
                "ZENN_REPO_PATH",
                str(Path.home() / ".local/state/life-manager/writer/checkouts/zenn-articles"),
            )
        ),
        )
        if command == "publish":
            child.add_argument(
                "--ledger",
                type=Path,
                default=Path(os.environ.get("ARTICLE_LEDGER", "")),
            )
    args = parser.parse_args()
    if not args.state.is_file():
        raise ZennCurrentRunRefused("publication state is required")
    if args.command == "media":
        result = stage_media(args.state, args.repo)
    elif args.command == "stage":
        result = stage(args.state, args.repo)
    else:
        result = publish(args.state, args.ledger, args.repo)
    print(
        json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ZennCurrentRunRefused,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2)
