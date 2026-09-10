from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[4]
LOCKER = ROOT / "skills" / "browser" / "scripts" / "run_with_file_lock.py"


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def test_all_browser_lanes_share_one_provider_lock():
    scripts = ROOT / "skills" / "earn" / "crowdworks" / "scripts"
    for owner in ("application-owner", "reply-owner", "paid-owner"):
        source = (scripts / owner).read_text(encoding="utf-8")
        assert "run_with_file_lock.py" in source
        assert '"$STATE_ROOT/provider-browser.lock"' in source


def test_lock_survives_exec_until_child_exits(tmp_path):
    lock = tmp_path / "provider.lock"
    first_ready = tmp_path / "first-ready"
    second_ready = tmp_path / "second-ready"
    first = subprocess.Popen([
        sys.executable, str(LOCKER), str(lock), "--", sys.executable, "-c",
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(.4)",
        str(first_ready),
    ])
    _wait_for(first_ready)
    second = subprocess.Popen([
        sys.executable, str(LOCKER), str(lock), "--", sys.executable, "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(second_ready),
    ])
    time.sleep(0.1)
    assert not second_ready.exists()
    assert first.wait(timeout=3) == 0
    assert second.wait(timeout=3) == 0
    assert second_ready.exists()


def test_sigkill_releases_lock_for_next_process(tmp_path):
    lock = tmp_path / "provider.lock"
    first_ready = tmp_path / "first-ready"
    second_ready = tmp_path / "second-ready"
    first = subprocess.Popen([
        sys.executable, str(LOCKER), str(lock), "--", sys.executable, "-c",
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)",
        str(first_ready),
    ])
    _wait_for(first_ready)
    second = subprocess.Popen([
        sys.executable, str(LOCKER), str(lock), "--", sys.executable, "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(second_ready),
    ])
    time.sleep(0.1)
    assert not second_ready.exists()
    first.kill()
    first.wait(timeout=3)
    assert second.wait(timeout=3) == 0
    assert second_ready.exists()
