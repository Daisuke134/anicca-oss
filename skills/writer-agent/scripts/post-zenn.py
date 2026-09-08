#!/usr/bin/env python3
"""Tracked draft-only Zenn repository stager."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
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



def slugify(title: str) -> str:
    value = re.sub(r"[^a-z0-9\- ]+", "", title.lower().strip())
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return (value or "life-manager-day")[:50]


def force_draft(source: str) -> str:
    if re.search(r"(?m)^published:\s*(?:true|false)\s*$", source):
        value = re.sub(
            r"(?m)^published:\s*(?:true|false)\s*$",
            "published: false",
            source,
        )
    else:
        match = re.match(r"^---\r?\n", source)
        if match is None:
            raise ValueError("Zenn source requires frontmatter")
        value = source[: match.end()] + "published: false\n" + source[match.end() :]
    if re.search(r"(?m)^published:\s*true\s*$", value):
        raise ValueError("Zenn draft flag rewrite failed")
    return value


def _title(source: str) -> str:
    match = re.search(
        r"(?m)^title:\s*(.+?)\s*$",
        source,
    )
    return match.group(1).strip().strip("\"'") if match else "life-manager-day"


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "GIT_SSH_COMMAND": (
            f"ssh -i {os.environ.get('ZENN_SSH_KEY', str(Path.home() / '.ssh/id_ed25519'))} "
            "-o IdentitiesOnly=yes"
        ),
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def stage() -> dict[str, str]:
    article_date = os.environ.get("ARTICLE_DATE", date.today().isoformat())
    writer_state = Path(
        os.environ.get(
            "WRITER_STATE_DIR",
            str(Path.home() / ".local/state/life-manager/writer"),
        )
    )
    workspace = writer_state
    source_path = workspace / "drafts" / article_date / "ja.md"
    repo = Path(
        os.environ.get(
            "ZENN_REPO_PATH",
            str(writer_state / "checkouts/zenn-articles"),
        )
    )
    account = os.environ.get("ZENN_ACCOUNT", "")
    if re.fullmatch(r"[A-Za-z0-9_-]+", account) is None:
        raise RuntimeError("ZENN_ACCOUNT is required and invalid")
    if not source_path.is_file() or not (repo / ".git").is_dir():
        raise RuntimeError("Zenn source or repository is missing")
    source = force_draft(source_path.read_text(encoding="utf-8"))
    gate_text("post-zenn", {str(source_path): source})
    slug = f"{article_date}-{slugify(_title(source))}"
    relative = Path("articles") / f"{slug}.md"
    destination = repo / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source, encoding="utf-8")
    added = _git(repo, ["add", str(relative)])
    if added.returncode != 0:
        raise RuntimeError("Zenn git add failed")
    staged = _git(repo, ["diff", "--cached", "--name-only"])
    unrelated = [
        item
        for item in staged.stdout.splitlines()
        if item and item != str(relative)
    ]
    if unrelated:
        raise RuntimeError("Zenn repository has unrelated staged changes")
    changed = _git(repo, ["diff", "--cached", "--quiet", "--", str(relative)])
    if changed.returncode != 0:
        committed = _git(
            repo,
            [
                "commit",
                "-m",
                f"article(draft): {slug}",
                "--",
                str(relative),
            ],
        )
        if committed.returncode != 0:
            raise RuntimeError("Zenn draft commit failed")
    pushed = _git(repo, ["push", "origin", "HEAD:main"])
    if pushed.returncode != 0:
        raise RuntimeError("Zenn draft push failed")
    return {
        "slug": slug,
        "url": f"https://zenn.dev/{account}/articles/{slug}",
        "relative_path": str(relative),
    }


def main() -> int:
    try:
        result = stage()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
