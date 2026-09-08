#!/usr/bin/env python3
"""Provision and validate the Writer-owned Zenn publication checkout."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class ZennCheckoutError(RuntimeError):
    """The configured publication checkout is unsafe or unavailable."""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    ssh_key = environment.get("ZENN_SSH_KEY", "")
    if ssh_key and not environment.get("GIT_SSH_COMMAND"):
        environment["GIT_SSH_COMMAND"] = (
            f"ssh -i {ssh_key} -o IdentitiesOnly=yes"
        )
    return subprocess.run(
        ["git", *args], env=environment, capture_output=True, text=True, check=False
    )


def _normalized_remote(value: str) -> str:
    return value.strip().removesuffix("/").removesuffix(".git")


def _validate(repo: Path, remote: str) -> None:
    if any(part in {".openclaw", ".hermes"} for part in repo.parts):
        raise ZennCheckoutError("legacy OpenClaw/Hermes checkout path is refused")
    if not remote.strip():
        raise ZennCheckoutError("ZENN_REPOSITORY_URL is required")
    if repo.is_symlink():
        raise ZennCheckoutError("symlink checkout path is refused")


def _reject_symlink_chain(path: Path) -> None:
    candidate = path.absolute()
    for node in (candidate, *candidate.parents):
        if node.exists() and node.is_symlink():
            raise ZennCheckoutError("symlink checkout path is refused")


def ensure_checkout(repo: Path, remote: str, git_name: str, git_email: str) -> dict[str, str]:
    repo = repo.expanduser()
    _reject_symlink_chain(repo)
    repo = repo.resolve(strict=False)
    _validate(repo, remote)
    repo.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = repo.parent / f".{repo.name}.provision.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if repo.exists():
            if not (repo / ".git").exists():
                raise ZennCheckoutError("configured Zenn checkout is not a Git repository")
        else:
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{repo.name}.clone-", dir=repo.parent)
            )
            try:
                result = _git("clone", "--", remote, str(temporary / "repo"))
                if result.returncode != 0:
                    raise ZennCheckoutError("Zenn repository clone failed")
                os.replace(temporary / "repo", repo)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        actual = _git("-C", str(repo), "remote", "get-url", "origin")
        if actual.returncode != 0:
            raise ZennCheckoutError("Zenn checkout has no origin remote")
        if _normalized_remote(actual.stdout) != _normalized_remote(remote):
            raise ZennCheckoutError("Zenn checkout origin does not match configuration")
        if not git_name.strip() or not re.fullmatch(r"[^@\s]+@[^@\s]+", git_email):
            raise ZennCheckoutError("ZENN_GIT_NAME and ZENN_GIT_EMAIL are required")
        for key, value in (("user.name", git_name), ("user.email", git_email)):
            configured = _git("-C", str(repo), "config", "--local", key, value)
            if configured.returncode != 0:
                raise ZennCheckoutError("Zenn checkout Git identity configuration failed")
    return {"status": "ready", "repo": str(repo), "remote": remote}


def valid_account(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ZennCheckoutError("ZENN_ACCOUNT is required and invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(
            os.environ.get(
                "ZENN_REPO_PATH",
                str(
                    Path.home()
                    / ".local/state/life-manager/writer/checkouts/zenn-articles"
                ),
            )
        ),
    )
    parser.add_argument(
        "--remote", default=os.environ.get("ZENN_REPOSITORY_URL", "")
    )
    parser.add_argument("--account", default=os.environ.get("ZENN_ACCOUNT", ""))
    parser.add_argument("--git-name", default=os.environ.get("ZENN_GIT_NAME", ""))
    parser.add_argument("--git-email", default=os.environ.get("ZENN_GIT_EMAIL", ""))
    args = parser.parse_args()
    valid_account(args.account)
    print(
        json.dumps(
            ensure_checkout(args.repo, args.remote, args.git_name, args.git_email),
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ZennCheckoutError) as error:
        print(f"zenn-checkout: {error}", file=os.sys.stderr)
        raise SystemExit(2)
