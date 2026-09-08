from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("application_parent", SCRIPTS / "application_parent.py")
assert SPEC and SPEC.loader
application_parent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(application_parent)


def test_heartbeat_rechecks_one_transient_ledger_lock_timeout(monkeypatch, tmp_path) -> None:
    handle = application_parent.LeaseHandle(
        lease_script=tmp_path / "lease.py", task="apply", heartbeat_seconds=0.001
    )
    handle.value = {
        "token": "1" * 32,
        "generation": 1,
        "ws": "ws://example.invalid/devtools/page/1",
    }
    calls = 0

    def run(*_arguments):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(["lease.py", "heartbeat"], 35)
        handle._stop.set()
        return {"ok": True}

    monkeypatch.setattr(handle, "_run", run)

    handle._heartbeat_loop()

    assert calls == 2
    assert handle._heartbeat_error is None


def test_successful_apply_parks_healthy_context_for_next_wake(monkeypatch, tmp_path) -> None:
    handle = application_parent.LeaseHandle(
        lease_script=tmp_path / "lease.py", task="apply", heartbeat_seconds=20,
    )
    handle.value = {
        "token": "1" * 32,
        "generation": 1,
        "ws": "ws://example.invalid/devtools/page/1",
    }
    monkeypatch.setenv("CLOAK_CONTEXT_PARK_ON_IDLE", "1")
    calls = []
    monkeypatch.setattr(handle, "_run", lambda *args: calls.append(args) or {"ok": True})

    assert handle.__exit__(None, None, None) is False

    assert calls == [("park", "apply", "--token", "1" * 32, "--generation", "1")]
