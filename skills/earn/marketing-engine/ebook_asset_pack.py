#!/usr/bin/env python3
"""Provision the versioned, redistributable default ebook asset pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
from typing import Callable


HERE = Path(__file__).resolve().parent
DEFAULT_PACK = HERE / "ebook-asset-packs/default-v1"
WATERCOLOR_CLIP_NAMES = tuple(f"scene-{number:02d}.mp4" for number in range(1, 7))
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
Executor = Callable[..., subprocess.CompletedProcess]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_relative(value: object) -> Path:
    require(isinstance(value, str) and value, "asset path required")
    relative = PurePosixPath(value)
    require(not relative.is_absolute() and ".." not in relative.parts and "\\" not in value,
            "asset path must be portable")
    require(str(relative) == value and not value.startswith("./"), "asset path must be canonical")
    return Path(*relative.parts)


def default_asset_root(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    if env.get("LM_EBOOK_ASSET_ROOT"):
        return Path(env["LM_EBOOK_ASSET_ROOT"]).expanduser()
    data_home = Path(env.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "life-manager/ebook-assets"


def default_pack_root(asset_root: Path) -> Path:
    return Path(asset_root) / "packs/default-v1"


def resolve_ffmpeg(environment: dict[str, str] | None = None) -> str | None:
    env = os.environ if environment is None else environment
    value = str(env.get("FFMPEG_BIN", "")).strip() or shutil.which("ffmpeg")
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    return str(candidate)


def resolve_ffprobe(environment: dict[str, str] | None = None) -> str | None:
    env = os.environ if environment is None else environment
    configured = str(env.get("FFPROBE_BIN", "")).strip()
    ffmpeg = resolve_ffmpeg(env)
    sibling = str(Path(ffmpeg).with_name("ffprobe")) if ffmpeg else ""
    value = configured or (sibling if sibling and Path(sibling).is_file() else "") or shutil.which("ffprobe")
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    return str(candidate)


def require_regular_tree_path(root: Path, relative: Path) -> Path:
    current = root
    for segment in relative.parts[:-1]:
        current /= segment
        mode = current.lstat().st_mode
        require(stat.S_ISDIR(mode) and not current.is_symlink(), "ebook asset source directory must be regular")
    output = root / relative
    mode = output.lstat().st_mode
    require(stat.S_ISREG(mode) and not output.is_symlink(), "ebook asset source must be a regular file")
    return output


def load_pack(pack_root: Path = DEFAULT_PACK) -> tuple[dict, list[tuple[Path, Path]], list[tuple[Path, str]]]:
    root = Path(pack_root)
    require(root.is_dir() and not root.is_symlink(), "ebook asset pack root must be a regular directory")
    manifest_path = root / "manifest.json"
    require(manifest_path.is_file() and not manifest_path.is_symlink(), "ebook asset manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(set(manifest) == {"schema_version", "pack_id", "license", "files", "generated_clips"}
            and manifest["schema_version"] == "marketing.ebook-assets.v1"
            and manifest["pack_id"] == "default-v1"
            and manifest["license"] == "CC0-1.0", "ebook asset manifest invalid")
    files: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for item in manifest["files"]:
        require(set(item) == {"path", "sha256"} and SHA256.fullmatch(str(item["sha256"])) is not None,
                "ebook asset file declaration invalid")
        relative = safe_relative(item["path"])
        require(relative not in seen, "ebook asset path duplicated")
        source = require_regular_tree_path(root, relative)
        require(digest(source) == item["sha256"], f"ebook asset hash mismatch: {relative.as_posix()}")
        seen.add(relative)
        files.append((relative, source))
    clips: list[tuple[Path, str]] = []
    for item in manifest["generated_clips"]:
        require(set(item) == {"path", "color"} and COLOR.fullmatch(str(item["color"])) is not None,
                "ebook clip declaration invalid")
        relative = safe_relative(item["path"])
        require(relative.suffix == ".mp4" and relative not in seen, "ebook clip path invalid")
        seen.add(relative)
        clips.append((relative, item["color"]))
    require(len(files) == 5 and len(clips) == 6, "ebook asset inventory differs")
    declared_files = {Path("manifest.json")} | {relative for relative, _ in files}
    declared_directories = {
        parent for item in declared_files for parent in item.parents if parent != Path(".")
    }
    actual_files, actual_directories = inventory(root)
    require(actual_files == declared_files and actual_directories == declared_directories,
            "ebook asset source inventory differs")
    return manifest, files, clips


def inventory(target: Path) -> tuple[set[Path], set[Path]]:
    file_result: set[Path] = set()
    directory_result: set[Path] = set()
    for root, directories, files in os.walk(target, followlinks=False):
        directory = Path(root)
        for name in directories:
            child = directory / name
            require(not child.is_symlink(), "ebook asset output directory must not be a symlink")
            directory_result.add(child.relative_to(target))
        for name in files:
            child = directory / name
            require(not child.is_symlink(), "ebook asset output must not be a symlink")
            require(child.is_file(), "ebook asset output must be a regular file")
            file_result.add(child.relative_to(target))
    return file_result, directory_result


def verify_receipt(target: Path, manifest_hash: str, expected: set[Path]) -> dict:
    receipt_path = target / "pack-receipt.json"
    require(receipt_path.is_file() and not receipt_path.is_symlink(), "conflicting ebook asset root")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    require(receipt.get("schema_version") == "marketing.ebook-assets-receipt.v1"
            and receipt.get("pack_id") == "default-v1"
            and receipt.get("manifest_sha256") == manifest_hash
            and isinstance(receipt.get("files"), list), "ebook asset receipt invalid")
    actual_files, actual_directories = inventory(target)
    expected_directories = {parent for item in expected for parent in item.parents if parent != Path(".")}
    require(actual_files == expected | {Path("pack-receipt.json")}
            and actual_directories == expected_directories,
            "ebook asset output inventory differs")
    actual: set[Path] = set()
    for item in receipt["files"]:
        require(set(item) == {"path", "sha256", "bytes"}
                and isinstance(item["bytes"], int) and item["bytes"] > 0,
                "ebook asset receipt file invalid")
        relative = safe_relative(item.get("path"))
        require(relative not in actual, "ebook asset receipt path duplicated")
        actual.add(relative)
        output = target / relative
        require(output.is_file() and not output.is_symlink()
                and SHA256.fullmatch(str(item.get("sha256", ""))) is not None
                and output.stat().st_size == item["bytes"]
                and digest(output) == item["sha256"], f"ebook asset output differs: {relative.as_posix()}")
    require(actual == expected, "ebook asset receipt inventory differs")
    return receipt


def provision_default_pack(
    *,
    asset_root: Path,
    pack_root: Path = DEFAULT_PACK,
    environment: dict[str, str] | None = None,
    executor: Executor = subprocess.run,
) -> dict:
    env = os.environ if environment is None else environment
    ffmpeg = resolve_ffmpeg(env)
    if not ffmpeg:
        return {"state": "setup_required", "missing": ["ffmpeg"], "external_effects": []}
    manifest, files, clips = load_pack(pack_root)
    manifest_hash = digest(Path(pack_root) / "manifest.json")
    expected = {relative for relative, _ in files} | {relative for relative, _ in clips}
    requested = Path(asset_root).expanduser()
    require(not requested.is_symlink(), "ebook asset root must not be a symlink")
    target = requested.resolve()
    if target.exists():
        require(target.is_dir(), "ebook asset root must be a directory")
        receipt = verify_receipt(target, manifest_hash, expected)
        return {"state": "replayed", "pack_id": manifest["pack_id"],
                "asset_root": str(target), "receipt": receipt, "external_effects": []}
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = target.with_name(f".{target.name}.stage-{os.getpid()}")
    require(not stage.exists(), "ebook asset stage already exists")
    claimed: tuple[int, int] | None = None
    try:
        stage.mkdir(mode=0o700)
        for relative, source in files:
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, output)
            output.chmod(0o600)
        for relative, color in clips:
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            executor([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                f"color=c=0x{color[1:]}:s=720x1280:d=5:r=30", "-an", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", str(output),
            ], check=True, capture_output=True)
            require(output.is_file() and output.stat().st_size > 0, "ebook clip generation produced no file")
            output.chmod(0o600)
        declared = sorted(relative for relative, _ in files) + sorted(relative for relative, _ in clips)
        rows = [{"path": relative.as_posix(), "sha256": digest(stage / relative),
                 "bytes": (stage / relative).stat().st_size} for relative in declared]
        receipt = {"schema_version": "marketing.ebook-assets-receipt.v1",
                   "pack_id": manifest["pack_id"], "manifest_sha256": manifest_hash,
                   "files": rows, "external_effects": []}
        receipt_path = stage / "pack-receipt.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                                encoding="utf-8")
        receipt_path.chmod(0o600)
        receipt = verify_receipt(stage, manifest_hash, expected)
        target.mkdir(mode=0o700)
        claimed_stat = target.stat()
        claimed = (claimed_stat.st_dev, claimed_stat.st_ino)
        for child in sorted(stage.iterdir(), key=lambda item: item.name == "pack-receipt.json"):
            child.rename(target / child.name)
        stage.rmdir()
    except Exception:
        if claimed is not None and target.exists() and not target.is_symlink():
            current = target.stat()
            if (current.st_dev, current.st_ino) == claimed and not (target / "pack-receipt.json").exists():
                shutil.rmtree(target)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"state": "provisioned", "pack_id": manifest["pack_id"],
            "asset_root": str(target), "receipt": receipt, "external_effects": []}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path,
                        help="Life Manager ebook asset base; the versioned pack is created below packs/default-v1")
    args = parser.parse_args()
    print(json.dumps(provision_default_pack(
        asset_root=default_pack_root(args.asset_root or default_asset_root())),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
