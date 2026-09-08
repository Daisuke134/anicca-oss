from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


GUARD = Path(__file__).resolve().parents[1] / "disk_admission.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("disk_admission_test", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"
    host_state = home / ".local/state/life-manager/state"
    producer_state = home / ".local/state/life-manager/producer"
    host_state.mkdir(parents=True)
    producer_state.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LIFE_MANAGER_HOST_STATE_DIR", str(host_state))
    monkeypatch.setenv("LIFE_MANAGER_PRODUCER_STATE_DIR", str(producer_state))
    monkeypatch.setenv("LIFE_MANAGER_DISK_HEADROOM_KIB", "0")
    for key in (
        "GIG_DISK_HEADROOM_KIB", "GIG_HOST_STATE_DIR", "GIG_STATE_DIR",
        "OPENCLAW_STATE_DIR", "DISK_CONTROL_STATE_DIR",
        "LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP",
        "LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_life_manager_owned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LIFE_MANAGER_HOST_STATE_DIR")
    monkeypatch.delenv("LIFE_MANAGER_PRODUCER_STATE_DIR")
    guard = load_guard()
    assert guard._host_state_dir() == home / ".local/state/life-manager/state"
    assert guard._state_dir() == home / ".local/state/life-manager"


@pytest.mark.parametrize(
    ("filename", "reason"),
    (("disk-writers.stop", "disk_writers_stop"),
     ("disk-pressure.block", "disk_pressure_block")),
)
def test_policy_flags_fail_closed(
    monkeypatch: pytest.MonkeyPatch, filename: str, reason: str,
):
    host_state = Path(os.environ["LIFE_MANAGER_HOST_STATE_DIR"])
    host_state.joinpath(filename).write_text("blocked\n", encoding="utf-8")
    guard = load_guard()
    assert guard.disk_headroom_ok() is False
    receipt = json.loads(
        (Path(os.environ["LIFE_MANAGER_PRODUCER_STATE_DIR"])
         / "state/disk-headroom.json").read_text(encoding="utf-8")
    )
    assert receipt["reason"] == reason
    assert receipt["effect"] == 0


def test_fresh_policy_directory_is_created_private(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    host_state = tmp_path / "missing"
    monkeypatch.setenv("LIFE_MANAGER_HOST_STATE_DIR", str(host_state))
    guard = load_guard()
    assert guard.disk_headroom_ok() is True
    assert host_state.stat().st_mode & 0o777 == 0o700


def test_fresh_producer_state_is_created_before_disk_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    producer_state = tmp_path / "new" / "producer"
    monkeypatch.setenv("LIFE_MANAGER_PRODUCER_STATE_DIR", str(producer_state))
    guard = load_guard()
    assert guard.disk_headroom_ok() is True
    assert producer_state.stat().st_mode & 0o777 == 0o700


def test_symlink_policy_directory_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("LIFE_MANAGER_HOST_STATE_DIR", str(alias))
    guard = load_guard()
    assert guard.disk_headroom_ok() is False


def test_ignore_is_explicit_and_flag_specific(monkeypatch: pytest.MonkeyPatch):
    host_state = Path(os.environ["LIFE_MANAGER_HOST_STATE_DIR"])
    host_state.joinpath("disk-pressure.block").write_text("blocked\n", encoding="utf-8")
    monkeypatch.setenv("LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK", "1")
    guard = load_guard()
    assert guard.disk_headroom_ok() is True


def test_cli_requires_child_command():
    guard = load_guard()
    assert guard.main([]) == 2
