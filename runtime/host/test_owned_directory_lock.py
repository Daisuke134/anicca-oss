import json
import os
from pathlib import Path
import subprocess
import sys
import time


SCRIPT = Path(__file__).with_name("owned_directory_lock.py")


def invoke(action: str, lock: Path, pid: int, token: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), action, str(lock), str(pid), token],
        capture_output=True,
        text=True,
        check=False,
    )


def test_live_owner_is_busy_and_exact_owner_can_release(tmp_path):
    lock = tmp_path / "run.lock"
    first = invoke("acquire", lock, os.getpid(), "first")
    assert first.returncode == 0
    assert json.loads(first.stdout) == {"status": "acquired"}

    second = invoke("acquire", lock, os.getpid(), "second")
    assert second.returncode == 75
    assert json.loads(second.stdout) == {"status": "busy"}
    assert invoke("release", lock, os.getpid(), "wrong").returncode == 2
    assert invoke("release", lock, os.getpid(), "first").returncode == 0
    assert not lock.exists()


def test_dead_owner_is_reclaimed(tmp_path):
    lock = tmp_path / "run.lock"
    lock.mkdir()
    (lock / "owner.json").write_text(
        json.dumps({"version": 1, "pid": 999_999_999, "process_start": "old", "token": "old"}),
        encoding="utf-8",
    )
    result = invoke("acquire", lock, os.getpid(), "new")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "reclaimed"}
    assert invoke("release", lock, os.getpid(), "new").returncode == 0


def test_malformed_and_empty_legacy_locks_are_reclaimed(tmp_path):
    for name, content in (("malformed", "not-json"), ("empty", None)):
        lock = tmp_path / f"{name}.lock"
        lock.mkdir()
        if content is not None:
            (lock / "owner.json").write_text(content, encoding="utf-8")
        old = time.time() - 60
        os.utime(lock, (old, old))
        result = invoke("acquire", lock, os.getpid(), name)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {"status": "reclaimed"}
        assert invoke("release", lock, os.getpid(), name).returncode == 0


def test_valid_json_scalar_and_array_legacy_locks_are_reclaimed(tmp_path):
    for index, content in enumerate(("null", "1", "true", "[]", '[{"pid":1}]')):
        lock = tmp_path / f"scalar-{index}.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(content, encoding="utf-8")
        old = time.time() - 60
        os.utime(lock, (old, old))
        result = invoke("acquire", lock, os.getpid(), f"token-{index}")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"status": "reclaimed"}
        assert invoke("release", lock, os.getpid(), f"token-{index}").returncode == 0


def test_symlink_lock_fails_closed(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    lock = tmp_path / "run.lock"
    lock.symlink_to(target, target_is_directory=True)
    result = invoke("acquire", lock, os.getpid(), "token")
    assert result.returncode == 2
    assert lock.is_symlink()


def test_fresh_ownerless_initialization_window_is_busy(tmp_path):
    lock = tmp_path / "run.lock"
    lock.mkdir()
    result = invoke("acquire", lock, os.getpid(), "other")
    assert result.returncode == 75
    assert json.loads(result.stdout) == {"status": "busy"}
    assert lock.exists()
