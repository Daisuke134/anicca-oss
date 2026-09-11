from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ebook_asset_pack import DEFAULT_PACK, load_pack, provision_default_pack


READY = {"FFMPEG_BIN": shutil.which("ffmpeg") or "/usr/bin/true"}


def test_missing_ffmpeg_is_setup_required_without_output(tmp_path, monkeypatch):
    monkeypatch.setattr("ebook_asset_pack.shutil.which", lambda _name: None)
    target = tmp_path / "assets"
    assert provision_default_pack(asset_root=target, environment={}) == {
        "state": "setup_required", "missing": ["ffmpeg"], "external_effects": [],
    }
    assert not target.exists()


def test_provisions_hash_verified_pack_and_replays_without_regeneration(tmp_path):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(f"clip:{args[8]}".encode())
        return subprocess.CompletedProcess(args, 0)

    target = tmp_path / "assets"
    first = provision_default_pack(asset_root=target, environment=READY, executor=execute)
    assert first["state"] == "provisioned"
    assert len(calls) == 6
    receipt = json.loads((target / "pack-receipt.json").read_text())
    assert len(receipt["files"]) == 11
    for item in receipt["files"]:
        output = target / item["path"]
        assert output.is_file() and not output.is_symlink()
        assert hashlib.sha256(output.read_bytes()).hexdigest() == item["sha256"]
    replay = provision_default_pack(asset_root=target, environment=READY, executor=execute)
    assert replay["state"] == "replayed"
    assert len(calls) == 6


def test_tampered_output_and_source_fail_closed(tmp_path):
    def execute(args, **_kwargs):
        Path(args[-1]).write_bytes(b"clip")
        return subprocess.CompletedProcess(args, 0)

    target = tmp_path / "assets"
    provision_default_pack(asset_root=target, environment=READY, executor=execute)
    (target / "manuscripts/ebook-en.md").write_text("changed")
    with pytest.raises(ValueError, match="output differs"):
        provision_default_pack(asset_root=target, environment=READY, executor=execute)

    copied = tmp_path / "pack"
    shutil.copytree(DEFAULT_PACK, copied)
    (copied / "manuscripts/ebook-ja.md").write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_pack(copied)


@pytest.mark.parametrize("surplus_kind", ["file", "directory", "symlink"])
def test_source_pack_rejects_every_undeclared_entry(tmp_path, surplus_kind):
    copied = tmp_path / "pack"
    shutil.copytree(DEFAULT_PACK, copied)
    surplus = copied / "undeclared"
    if surplus_kind == "file":
        surplus.write_text("extra")
    elif surplus_kind == "directory":
        surplus.mkdir()
    else:
        surplus.symlink_to(copied / "LICENSE.txt")
    with pytest.raises(ValueError, match="inventory differs|must not be a symlink"):
        load_pack(copied)


def test_replay_rejects_surplus_files_and_intermediate_symlinks(tmp_path):
    def execute(args, **_kwargs):
        Path(args[-1]).write_bytes(b"clip")
        return subprocess.CompletedProcess(args, 0)

    target = tmp_path / "assets"
    provision_default_pack(asset_root=target, environment=READY, executor=execute)
    (target / "surplus.txt").write_text("not declared")
    with pytest.raises(ValueError, match="inventory differs"):
        provision_default_pack(asset_root=target, environment=READY, executor=execute)
    (target / "surplus.txt").unlink()
    (target / "surplus-empty").mkdir()
    with pytest.raises(ValueError, match="inventory differs"):
        provision_default_pack(asset_root=target, environment=READY, executor=execute)
    (target / "surplus-empty").rmdir()
    outside = tmp_path / "outside-captions"
    outside.mkdir()
    shutil.rmtree(target / "captions")
    (target / "captions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="output directory must not be a symlink"):
        provision_default_pack(asset_root=target, environment=READY, executor=execute)

    copied = tmp_path / "pack"
    shutil.copytree(DEFAULT_PACK, copied)
    shutil.rmtree(copied / "captions")
    (copied / "captions").symlink_to(DEFAULT_PACK / "captions", target_is_directory=True)
    with pytest.raises(ValueError, match="source directory must be regular"):
        load_pack(copied)


def test_existing_empty_target_is_not_replaced(tmp_path):
    target = tmp_path / "assets"
    target.mkdir()
    before = target.stat()
    with pytest.raises(ValueError, match="conflicting ebook asset root"):
        provision_default_pack(asset_root=target, environment=READY)
    after = target.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_failed_publish_removes_only_the_newly_claimed_incomplete_target(tmp_path, monkeypatch):
    def execute(args, **_kwargs):
        Path(args[-1]).write_bytes(b"clip")
        return subprocess.CompletedProcess(args, 0)

    original = Path.rename
    calls = 0

    def fail_second_move(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        return original(source, destination)

    monkeypatch.setattr(Path, "rename", fail_second_move)
    target = tmp_path / "assets"
    with pytest.raises(OSError, match="injected publish failure"):
        provision_default_pack(asset_root=target, environment=READY, executor=execute)
    assert not target.exists()
    assert list(tmp_path.glob(".assets.stage-*")) == []


def test_first_publish_rejects_generator_surplus_directory(tmp_path):
    def execute(args, **_kwargs):
        output = Path(args[-1])
        output.write_bytes(b"clip")
        (output.parent / "surplus-empty").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(args, 0)

    target = tmp_path / "assets"
    with pytest.raises(ValueError, match="output inventory differs"):
        provision_default_pack(asset_root=target, environment=READY, executor=execute)
    assert not target.exists()


def test_first_publish_rejects_generator_symlink_clip(tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")

    def execute(args, **_kwargs):
        Path(args[-1]).symlink_to(outside)
        return subprocess.CompletedProcess(args, 0)

    target = tmp_path / "assets"
    with pytest.raises(ValueError, match="must not be a symlink"):
        provision_default_pack(asset_root=target, environment=READY, executor=execute)
    assert not target.exists()


def test_rejects_symlink_asset_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "assets"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        provision_default_pack(asset_root=linked, environment=READY)
