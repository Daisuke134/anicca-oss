"""D4: a holder killed with -9 must not linger in the lease ledger for up to idle_min.

Before this, gc() only reaped rows idle for idle_min minutes (default 45) -- the lease
row schema recorded no `pid` at all, so a crashed holder's row was indistinguishable from
a genuinely slow-but-alive one until the idle clock ran out. acquire()/heartbeat() now
stamp `pid` = os.getppid() (the calling shell/process that actually holds the lease) on
every successful call, and gc() reaps a row immediately if its recorded pid is confirmed
dead -- on top of, never instead of, the existing idle_min safety net. Missing/inconclusive
pid never triggers early reap (legacy rows, permission-denied cross-user pids, etc.).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent / "cdp_context_lease.py"
    spec = importlib.util.spec_from_file_location("cdp_context_lease_pid_reap", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_leases(leases_file, leases):
    leases_file.write_text(json.dumps(leases), encoding="utf-8")


def _dead_pid():
    # Spawn and immediately reap a child so its pid is guaranteed dead but was real.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_pid_alive_true_for_self():
    module = load_module()
    assert module._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_a_reaped_child():
    module = load_module()
    assert module._pid_alive(_dead_pid()) is False


def test_pid_alive_none_for_missing_or_invalid():
    module = load_module()
    assert module._pid_alive(None) is None
    assert module._pid_alive(0) is None
    assert module._pid_alive("not-a-pid") is None


def test_holder_pid_rejects_dead_invalid_and_out_of_range_values(monkeypatch):
    module = load_module()
    fallback = os.getppid()
    for value in ("invalid", "0", str(_dead_pid()), str(2**80)):
        monkeypatch.setenv("AI_BROWSER_HOLDER_PID", value)
        assert module._holder_pid() == fallback


def test_acquire_stamps_pid_on_a_fresh_lease(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))

    async def create_ok(pairs, timeout=None):
        out = []
        for method, _ in pairs:
            if method == "Target.createBrowserContext":
                out.append({"browserContextId": "c1"})
            elif method == "Target.createTarget":
                out.append({"targetId": "t1"})
        return out

    monkeypatch.setattr(module, "_calls", create_ok)
    result = module.acquire("gig-task")

    assert result["pid"] == os.getppid()
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["gig-task"]["pid"] == os.getppid()


def test_acquire_reclaims_oldest_parked_context_at_limit(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("CLOAK_BROWSER_MAX_CONTEXTS", "2")
    _write_leases(leases_file, {
        "old-idle": {
            "context_id": "old-context", "target_id": "old-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/old-target",
            "ts": 1, "token": "a" * 32, "generation": 1,
            "pid": None, "parked": True,
        },
        "new-idle": {
            "context_id": "new-context", "target_id": "new-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/new-target",
            "ts": 2, "token": "b" * 32, "generation": 1,
            "pid": None, "parked": True,
        },
    })
    calls = []

    async def dispose_then_create(pairs, timeout=None):
        output = []
        for method, params in pairs:
            calls.append((method, params))
            if method == "Target.disposeBrowserContext":
                output.append({})
            elif method == "Target.createBrowserContext":
                output.append({"browserContextId": "fresh-context"})
            elif method == "Target.createTarget":
                output.append({"targetId": "fresh-target"})
        return output

    monkeypatch.setattr(module, "_calls", dispose_then_create)
    result = module.acquire("next-task")

    assert result["context_id"] == "fresh-context"
    assert calls[0] == (
        "Target.disposeBrowserContext", {"browserContextId": "old-context"}
    )
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert set(saved) == {"new-idle", "next-task"}


def test_acquire_never_reclaims_an_owned_context_at_limit(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("CLOAK_BROWSER_MAX_CONTEXTS", "1")
    _write_leases(leases_file, {
        "active": {
            "context_id": "active-context", "target_id": "active-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/active-target",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
            "pid": os.getpid(),
        }
    })

    try:
        module.acquire("next-task")
    except RuntimeError as error:
        assert str(error) == "browser_context_limit"
    else:
        raise AssertionError("active context was reclaimed")


def test_acquire_fails_closed_before_creating_context_at_browser_limit(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("CLOAK_BROWSER_MAX_CONTEXTS", "2")
    _write_leases(leases_file, {
        "first": {"context_id": "c1", "target_id": "t1", "pid": os.getpid()},
        "second": {"context_id": "c2", "target_id": "t2", "pid": os.getpid()},
    })

    async def must_not_create(*_args, **_kwargs):
        raise AssertionError("context creation must not run above the browser limit")

    monkeypatch.setattr(module, "_calls", must_not_create)
    try:
        module.acquire("third", no_seed=True)
    except RuntimeError as error:
        assert str(error) == "browser_context_limit"
    else:
        raise AssertionError("new context was admitted above the browser limit")


def test_browser_context_limit_rejects_invalid_configuration(monkeypatch):
    module = load_module()
    for value in ("0", "129", "not-an-integer"):
        monkeypatch.setenv("CLOAK_BROWSER_MAX_CONTEXTS", value)
        try:
            module._max_contexts()
        except ValueError as error:
            assert str(error) == "CLOAK_BROWSER_MAX_CONTEXTS must be an integer from 1 to 128"
        else:
            raise AssertionError(f"invalid browser context limit accepted: {value}")


def test_acquire_uses_explicit_holder_pid_from_command_substitution_wrapper(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("AI_BROWSER_HOLDER_PID", str(os.getpid()))

    async def create_ok(pairs, timeout=None):
        out = []
        for method, _ in pairs:
            if method == "Target.createBrowserContext":
                out.append({"browserContextId": "c1"})
            elif method == "Target.createTarget":
                out.append({"targetId": "t1"})
        return out

    monkeypatch.setattr(module, "_calls", create_ok)
    result = module.acquire("gig-task")

    assert result["pid"] == os.getpid()
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["gig-task"]["pid"] == os.getpid()


def test_reuse_and_heartbeat_keep_explicit_holder_pid(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("AI_BROWSER_HOLDER_PID", str(os.getpid()))
    _write_leases(leases_file, {
        "gig-task": {
            "context_id": "c1", "target_id": "t1",
            "ws": "ws://127.0.0.1:9222/devtools/page/t1",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
            "pid": os.getpid(),
        }
    })
    monkeypatch.setattr(module, "target_responds", lambda *a, **k: True)

    reused = module.acquire("gig-task")
    heartbeat = module.heartbeat("gig-task", token="a" * 32, generation=1)

    assert reused["pid"] == os.getpid()
    assert heartbeat["ok"] is True
    assert json.loads(leases_file.read_text(encoding="utf-8"))["gig-task"]["pid"] == os.getpid()


def test_acquire_rejects_a_second_live_holder(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("AI_BROWSER_HOLDER_PID", str(os.getpid()))
    _write_leases(leases_file, {
        "mercor-session": {
            "context_id": "c1", "target_id": "t1",
            "ws": "ws://127.0.0.1:9222/devtools/page/t1",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
            "pid": os.getppid(),
        }
    })
    monkeypatch.setattr(module, "target_responds", lambda *_args, **_kwargs: True)

    try:
        module.acquire("mercor-session")
    except RuntimeError as error:
        assert str(error) == "lease_busy"
    else:
        raise AssertionError("a live holder's context was shared concurrently")

    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["mercor-session"]["pid"] == os.getppid()


def test_foreign_holder_is_never_probed_or_disposed(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("AI_BROWSER_HOLDER_PID", str(os.getpid()))
    for holder_pid, pid_state in ((os.getppid(), True), (None, None)):
        _write_leases(leases_file, {
            "mercor-session": {
                "context_id": "c1", "target_id": "t1",
                "ws": "ws://127.0.0.1:9222/devtools/page/t1",
                "ts": int(time.time()), "token": "a" * 32, "generation": 1,
                "pid": holder_pid,
            }
        })
        monkeypatch.setattr(module, "_pid_alive", lambda _pid, state=pid_state: state)
        monkeypatch.setattr(
            module, "target_responds",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("foreign holder must not be health-probed")
            ),
        )
        monkeypatch.setattr(
            module, "_calls",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("foreign holder must not be disposed")
            ),
        )

        try:
            module.acquire("mercor-session")
        except RuntimeError as error:
            assert str(error) == "lease_busy"
        else:
            raise AssertionError("foreign holder was not fenced")

        saved = json.loads(leases_file.read_text(encoding="utf-8"))
        assert saved["mercor-session"]["context_id"] == "c1"


def test_gc_reaps_a_row_whose_pid_just_died_even_though_it_is_not_idle_stale(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    fresh_ts = int(time.time())  # NOT idle-stale -- only the dead pid should trigger reap
    _write_leases(leases_file, {
        "gig-task": {
            "context_id": "c1", "target_id": "t1",
            "ws": "ws://127.0.0.1:9222/devtools/page/t1",
            "ts": fresh_ts, "token": "a" * 32, "generation": 1,
            "pid": _dead_pid(),
        }
    })

    async def dispose_ok(pairs, timeout=None):
        return [{}]

    monkeypatch.setattr(module, "_calls", dispose_ok)
    result = module.gc(idle_min=45)

    assert result["reaped"] == ["gig-task"]
    assert json.loads(leases_file.read_text(encoding="utf-8")) == {}


def test_gc_does_not_reap_a_fresh_row_with_a_live_pid(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    _write_leases(leases_file, {
        "gig-task": {
            "context_id": "c1", "target_id": "t1",
            "ws": "ws://127.0.0.1:9222/devtools/page/t1",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
            "pid": os.getpid(),
        }
    })

    result = module.gc(idle_min=45)

    assert result["reaped"] == []
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["gig-task"]["context_id"] == "c1"


def test_gc_does_not_reap_a_legacy_row_missing_pid_before_idle_min(monkeypatch, tmp_path):
    # Rows written before this change have no pid field at all -- must fall back to the
    # unchanged idle_min-only behaviour, never treated as dead.
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    _write_leases(leases_file, {
        "gig-task": {
            "context_id": "c1", "target_id": "t1",
            "ws": "ws://127.0.0.1:9222/devtools/page/t1",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
        }
    })

    result = module.gc(idle_min=45)

    assert result["reaped"] == []


def test_gc_does_not_reap_a_row_reacquired_by_a_live_pid_between_dispose_and_finalize(monkeypatch, tmp_path):
    # Same race the existing identity check covers, but for the pid-liveness path: a row
    # whose pid died was mid-dispose when a live process re-acquired the same task name and
    # refreshed both ts and pid. Finalize must re-read the CURRENT pid, not the stale one
    # captured at candidate-selection time, or a live lease gets destroyed underneath it.
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    dead = _dead_pid()
    _write_leases(leases_file, {
        "gig-task": {
            "context_id": "old-context", "target_id": "old-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/old-target",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
            "pid": dead,
        }
    })

    async def dispose_then_reacquire(pairs, timeout=None):
        leases = module._leases()
        leases["gig-task"] = {
            "context_id": "old-context", "target_id": "old-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/old-target",
            "ts": int(time.time()), "token": "b" * 32, "generation": 1,
            "pid": os.getpid(),  # live pid took over the same row via acquire's reuse path
        }
        module._save(leases)
        return [{}]

    monkeypatch.setattr(module, "_calls", dispose_then_reacquire)
    result = module.gc(idle_min=45)

    assert "gig-task" not in result["reaped"]
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["gig-task"]["pid"] == os.getpid()


def test_acquire_reclaims_a_lease_whose_holder_pid_is_dead(monkeypatch, tmp_path):
    # D5: acquire() used to trust target_responds() alone -- an orphaned tab that still
    # answers Runtime.evaluate kept a provably-dead holder's context alive in the ledger
    # until gc's idle_min window (or a human) caught up (measured 2026-09-05: a dead
    # holder blocked an unrelated lane for 26 wakes). acquire() must reclaim on the free,
    # local pid check without waiting on the network probe at all.
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    _write_leases(leases_file, {
        "gig-task": {
            "context_id": "old-context", "target_id": "old-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/old-target",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
            "pid": _dead_pid(),
        }
    })

    def target_responds_should_not_be_needed(*args, **kwargs):
        raise AssertionError("acquire() must not wait on target_responds for a dead pid")

    monkeypatch.setattr(module, "target_responds", target_responds_should_not_be_needed)

    disposed = []

    async def dispose_then_create(pairs, timeout=None):
        out = []
        for method, params in pairs:
            if method == "Target.disposeBrowserContext":
                disposed.append(params.get("browserContextId"))
                out.append({})
            elif method == "Target.createBrowserContext":
                out.append({"browserContextId": "new-context"})
            elif method == "Target.createTarget":
                out.append({"targetId": "new-target"})
        return out

    monkeypatch.setattr(module, "_calls", dispose_then_create)
    result = module.acquire("gig-task")

    assert disposed == ["old-context"]
    assert result["ok"] is True
    assert result["reused"] is False
    assert result["context_id"] == "new-context"
    assert result["pid"] == os.getppid()


def test_acquire_does_not_reclaim_a_lease_whose_holder_pid_is_alive(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    _write_leases(leases_file, {
        "gig-task": {
            "context_id": "old-context", "target_id": "old-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/old-target",
            "ts": int(time.time()), "token": "a" * 32, "generation": 1,
            "pid": os.getpid(),  # this test process is definitely alive
        }
    })
    monkeypatch.setattr(module, "target_responds", lambda *a, **k: True)

    def create_should_not_be_called(pairs, timeout=None):
        raise AssertionError("acquire() must not dispose/recreate a live holder's context")

    monkeypatch.setattr(module, "_calls", create_should_not_be_called)
    try:
        module.acquire("gig-task")
    except RuntimeError as error:
        assert str(error) == "lease_busy"
    else:
        raise AssertionError("a second live holder reused the context")
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["gig-task"]["context_id"] == "old-context"
