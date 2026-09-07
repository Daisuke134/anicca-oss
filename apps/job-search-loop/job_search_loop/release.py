from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


INCLUDED_ROOTS = (
    "apps/job-search-loop",
    "runtime/agent-runner",
    "skills/browser/scripts/cdp_context_lease.py",
    "runtime/host/disk_admission.py",
)
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BLOCKED_NAMES = {".env", "profile.json"}
BLOCKED_SUFFIXES = {".sqlite", ".sqlite3", ".db"}


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitEntry:
    mode: int
    object_id: str
    path: str


def _git(repo_root: Path, *arguments: str, text: bool = False):
    try:
        return subprocess.check_output(
            ["git", *arguments],
            cwd=repo_root,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseError(f"git command failed: {' '.join(arguments)}") from error


def _safe_tracked_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseError(f"unsafe tracked path: {value}")
    if path.name in BLOCKED_NAMES or path.suffix in BLOCKED_SUFFIXES:
        raise ReleaseError(f"private-state-shaped tracked path: {value}")
    if not any(
        value == root or value.startswith(f"{root}/") for root in INCLUDED_ROOTS
    ):
        raise ReleaseError(f"path outside release roots: {value}")


def tracked_entries(repo_root: Path, treeish: str) -> tuple[str, list[GitEntry]]:
    commit = str(_git(repo_root, "rev-parse", f"{treeish}^{{commit}}", text=True)).strip()
    raw = _git(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        *INCLUDED_ROOTS,
    )
    entries: list[GitEntry] = []
    for record in bytes(raw).split(b"\0"):
        if not record:
            continue
        metadata, separator, encoded_path = record.partition(b"\t")
        if not separator:
            raise ReleaseError("invalid git tree record")
        mode_raw, object_type, object_id = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        path = encoded_path.decode("utf-8")
        _safe_tracked_path(path)
        entries.append(
            GitEntry(
                mode=0o755 if mode_raw == "100755" else 0o644,
                object_id=object_id,
                path=path,
            )
        )
    if not entries:
        raise ReleaseError("release tree has no tracked files")
    return commit, sorted(entries, key=lambda item: item.path)


def _normalized_info(name: str, *, mode: int, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    return info


def _write_archive(
    *,
    repo_root: Path,
    archive: Path,
    prefix: str,
    commit: str,
    version: str,
    entries: Iterable[GitEntry],
) -> int:
    entries = list(entries)
    release_metadata = (
        json.dumps(
            {
                "version": version,
                "commit": commit,
                "schema_version": 1,
                "included_roots": list(INCLUDED_ROOTS),
                "private_state_included": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    archive_items: list[tuple[str, int, bytes]] = [
        (f"{prefix}/RELEASE.json", 0o644, release_metadata)
    ]
    for entry in entries:
        data = bytes(_git(repo_root, "cat-file", "blob", entry.object_id))
        archive_items.append((f"{prefix}/{entry.path}", entry.mode, data))
    archive_items.sort(key=lambda item: item[0])

    temporary = archive.with_name(f".{archive.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=9,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    mode="w",
                    fileobj=compressed,
                    format=tarfile.PAX_FORMAT,
                ) as bundle:
                    for name, mode, data in archive_items:
                        bundle.addfile(
                            _normalized_info(name, mode=mode, size=len(data)),
                            io.BytesIO(data),
                        )
        os.chmod(temporary, 0o644)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return len(archive_items)


def build_release(
    *,
    repo_root: Path,
    output_dir: Path,
    version: str,
    treeish: str = "HEAD",
) -> dict[str, object]:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseError("version must contain only letters, numbers, dot, dash, underscore")
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    commit, entries = tracked_entries(repo_root, treeish)
    prefix = f"anicca-job-search-{version}"
    archive = output_dir / f"{prefix}.tar.gz"
    entry_count = _write_archive(
        repo_root=repo_root,
        archive=archive,
        prefix=prefix,
        commit=commit,
        version=version,
        entries=entries,
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_name(f"{archive.name}.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    os.chmod(checksum, 0o644)
    return {
        "version": 1,
        "release_version": version,
        "commit": commit,
        "archive": str(archive),
        "checksum": str(checksum),
        "sha256": digest,
        "entry_count": entry_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--treeish", default="HEAD")
    parsed = parser.parse_args(argv)
    try:
        receipt = build_release(
            repo_root=parsed.repo_root,
            output_dir=parsed.output_dir,
            version=parsed.version,
            treeish=parsed.treeish,
        )
    except ReleaseError as error:
        print(f"job-search release: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
