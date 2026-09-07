"""A wedged renderer must cost one context, never the caller's whole recovery.

Measured 2026-08-06 06:35 in the gig loop's parent log: B2's between-candidate target
recovery called `cdp_context_lease.py release`, the browser's renderer was wedged, and
`Target.disposeBrowserContext` never answered. `_calls()` had no timeout on `ws.recv()`, so
the lease script hung until the caller's 35-second subprocess limit killed it -- the
recovery designed to survive a dead target died at its first step, inside the lease.

Three properties pin that shut:
  1. `_calls` finishes or raises within its deadline, never hangs.
  2. `release` on a context that cannot be disposed keeps a cleanup tombstone so gc can
     still identify the browser-side context.
  3. acquire never creates a replacement while that old context remains undisposable.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

import pytest
import time
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent / "cdp_context_lease.py"
    spec = importlib.util.spec_from_file_location("cdp_context_lease_hangs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acquire_cli_preserves_exact_requested_url():
    module = load_module()
    assert module._acquire_url([
        "cdp_context_lease.py", "acquire", "mercor-task",
        "https://work.mercor.com/explore",
    ]) == "https://work.mercor.com/explore"
    assert module._acquire_url([
        "cdp_context_lease.py", "acquire", "mercor-task", "--no-seed",
    ]) == "about:blank"


class NeverAnsweringSocket:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        return None

    async def recv(self):
        await asyncio.sleep(3600)


def test_calls_raises_within_its_deadline_instead_of_hanging(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_browser_ws", lambda: "ws://127.0.0.1:1/devtools/browser/x")
    monkeypatch.setattr(
        module.websockets, "connect", lambda *a, **k: NeverAnsweringSocket()
    )
    started = time.monotonic()
    try:
        asyncio.run(module._calls([("Target.disposeBrowserContext", {"browserContextId": "c"})], timeout=1.0))
        raise AssertionError("a call that never answers must raise")
    except Exception:
        pass
    assert time.monotonic() - started < 10


def test_release_of_an_undisposable_context_keeps_cleanup_tombstone(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    leases_file.write_text(json.dumps({
        "gig-task": {
            "context_id": "dead-context",
            "target_id": "dead-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/dead-target",
            "ts": 0,
            "token": "a" * 32,
            "generation": 1,
        }
    }), encoding="utf-8")

    async def hang_forever(pairs, timeout=None):
        raise TimeoutError("Target.disposeBrowserContext never answered")

    monkeypatch.setattr(module, "_calls", hang_forever)
    result = module.release("gig-task", token="a" * 32, generation=1)

    assert result["ok"] is True
    assert "gc" in str(result.get("note") or "")
    assert result["cleanup_pending"] is True
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["gig-task"]["cleanup_pending"] is True


def test_acquire_does_not_orphan_an_undisposable_dead_context(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    leases_file.write_text(json.dumps({
        "gig-task": {
            "context_id": "dead-context", "target_id": "dead-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/dead-target",
            "ts": 0, "token": "a" * 32, "generation": 1,
        }
    }), encoding="utf-8")
    monkeypatch.setattr(module, "target_responds", lambda *_args, **_kwargs: False)

    async def dispose_fails(pairs, timeout=None):
        raise TimeoutError("dispose did not answer")

    monkeypatch.setattr(module, "_calls", dispose_fails)
    try:
        module.acquire("gig-task")
        raise AssertionError("acquire must fail closed while cleanup is unconfirmed")
    except RuntimeError as error:
        assert str(error) == "context_cleanup_pending"
    saved = json.loads(leases_file.read_text(encoding="utf-8"))
    assert saved["gig-task"]["cleanup_pending"] is True


def test_gc_keeps_cleanup_tombstone_until_dispose_succeeds(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    leases_file.write_text(json.dumps({
        "gig-task": {
            "context_id": "dead-context", "target_id": "dead-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/dead-target",
            "ts": 0, "token": "a" * 32, "generation": 1,
            "cleanup_pending": True,
        }
    }), encoding="utf-8")

    async def dispose_fails(pairs, timeout=None):
        raise TimeoutError("dispose did not answer")

    monkeypatch.setattr(module, "_calls", dispose_fails)
    result = module.gc(idle_min=45)

    assert result["reaped"] == []
    assert result["cleanup_pending"] == ["gig-task"]
    assert "gig-task" in json.loads(leases_file.read_text(encoding="utf-8"))


def test_release_with_a_wrong_fence_still_refuses(monkeypatch, tmp_path):
    # Dropping rows on dispose failure must not weaken the fence: a caller with a stale
    # token still cannot free someone else's context.
    module = load_module()
    leases_file = tmp_path / "leases.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    leases_file.write_text(json.dumps({
        "gig-task": {
            "context_id": "c", "target_id": "t",
            "ws": "ws://127.0.0.1:9222/devtools/page/t",
            "ts": 0, "token": "a" * 32, "generation": 2,
        }
    }), encoding="utf-8")
    result = module.release("gig-task", token="b" * 32, generation=2)
    assert result["ok"] is False
    assert json.loads(leases_file.read_text(encoding="utf-8")) != {}


def test_commit_cookies_merges_only_requested_domain(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    vault_file = tmp_path / "auth-state.json"
    overlay_file = tmp_path / "mercor-overlay.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_FILE", str(vault_file))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_WRITEBACK_FILE", str(overlay_file))
    leases_file.write_text(json.dumps({
        "mercor-task": {
            "context_id": "mercor-context", "target_id": "mercor-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/mercor-target",
            "ts": 0, "token": "a" * 32, "generation": 1,
        }
    }), encoding="utf-8")
    vault_file.write_text(json.dumps({
        "ts": 1,
        "cookies": [
            {"name": "old-mercor", "domain": ".mercor.com", "path": "/", "value": "old"},
            {"name": "coconala", "domain": ".coconala.com", "path": "/", "value": "keep"},
        ],
        "localStorage": {"https://coconala.com": {"key": "keep"}},
    }), encoding="utf-8")

    async def context_cookies(pairs, timeout=None):
        assert pairs == [("Storage.getCookies", {"browserContextId": "mercor-context"})]
        return [{"cookies": [
            {"name": "new-mercor", "domain": "work.mercor.com", "path": "/", "value": "new"},
            {"name": "google", "domain": ".google.com", "path": "/", "value": "ignore"},
        ]}]

    monkeypatch.setattr(module, "_calls", context_cookies)
    result = module.commit_cookies(
        "mercor-task", ["mercor.com"], token="a" * 32, generation=1
    )

    assert result["ok"] is True
    assert result["cookies_committed"] == 1
    assert json.loads(vault_file.read_text(encoding="utf-8"))["cookies"][0]["name"] == "old-mercor"
    saved = json.loads(overlay_file.read_text(encoding="utf-8"))
    assert {(cookie["name"], cookie["domain"]) for cookie in saved["cookies"]} == {
        ("new-mercor", "work.mercor.com"),
    }
    assert overlay_file.stat().st_mode & 0o777 == 0o600


def test_commit_cookies_keeps_vault_when_context_has_no_requested_cookie(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    vault_file = tmp_path / "auth-state.json"
    overlay_file = tmp_path / "mercor-overlay.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_FILE", str(vault_file))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_WRITEBACK_FILE", str(overlay_file))
    leases_file.write_text(json.dumps({
        "mercor-task": {
            "context_id": "mercor-context", "target_id": "mercor-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/mercor-target",
            "ts": 0, "token": "a" * 32, "generation": 1,
        }
    }), encoding="utf-8")
    original = {"ts": 1, "cookies": [
        {"name": "old-mercor", "domain": ".mercor.com", "path": "/", "value": "old"}
    ]}
    vault_file.write_text(json.dumps(original), encoding="utf-8")

    async def no_mercor_cookie(pairs, timeout=None):
        return [{"cookies": [
            {"name": "google", "domain": ".google.com", "path": "/", "value": "ignore"}
        ]}]

    monkeypatch.setattr(module, "_calls", no_mercor_cookie)
    result = module.commit_cookies(
        "mercor-task", ["mercor.com"], token="a" * 32, generation=1
    )

    assert result == {"ok": False, "reason": "no_matching_context_cookies"}
    assert json.loads(vault_file.read_text(encoding="utf-8")) == original
    assert not overlay_file.exists()


def test_acquire_seeds_provider_overlay_after_shared_base(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    vault_file = tmp_path / "auth-state.json"
    overlay_file = tmp_path / "mercor-overlay.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_FILE", str(vault_file))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_WRITEBACK_FILE", str(overlay_file))
    vault_file.write_text(json.dumps({"cookies": [
        {"name": "stale", "domain": ".mercor.com", "path": "/", "value": "old"},
        {"name": "google", "domain": ".google.com", "path": "/", "value": "base"},
    ]}), encoding="utf-8")
    overlay_file.write_text(json.dumps({"cookies": [
        {"name": "fresh", "domain": ".mercor.com", "path": "/", "value": "new"},
    ]}), encoding="utf-8")
    calls_seen = []

    async def create_context_and_target(pairs, timeout=None):
        calls_seen.append(pairs)
        if pairs == [("Target.createBrowserContext", {})]:
            return [{"browserContextId": "new-context"}]
        assert pairs[-1] == (
            "Target.createTarget",
            {"url": "https://work.mercor.com/explore", "browserContextId": "new-context"},
        )
        return [{}, {"targetId": "new-target"}]

    monkeypatch.setattr(module, "_calls", create_context_and_target)
    result = module.acquire("mercor-task", url="https://work.mercor.com/explore")

    assert result["ok"] is True
    seeded = calls_seen[1][0][1]["cookies"]
    assert {(cookie["name"], cookie["domain"]) for cookie in seeded} == {
        ("fresh", ".mercor.com"),
        ("google", ".google.com"),
    }


def test_commit_cookies_also_commits_only_declared_web_storage(monkeypatch, tmp_path):
    module = load_module()
    leases_file = tmp_path / "leases.json"
    overlay_file = tmp_path / "mercor-overlay.json"
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(leases_file))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_WRITEBACK_FILE", str(overlay_file))
    leases_file.write_text(json.dumps({
        "mercor-task": {
            "context_id": "mercor-context", "target_id": "mercor-target",
            "ws": "ws://127.0.0.1:9222/devtools/page/mercor-target",
            "ts": 0, "token": "a" * 32, "generation": 1,
        }
    }), encoding="utf-8")
    overlay_file.write_text(json.dumps({
        "cookies": [],
        "origins": [{
            "origin": "https://unrelated.example",
            "localStorage": [{"name": "keep", "value": "yes"}],
        }],
    }), encoding="utf-8")

    async def context_cookies(pairs, timeout=None):
        return [{"cookies": [
            {"name": "mercor", "domain": ".mercor.com", "path": "/", "value": "cookie"}
        ]}]

    async def page_storage(ws_url, pairs, timeout=None):
        assert ws_url.endswith("/mercor-target")
        assert "mercor-auth-store" in pairs[0][1]["expression"]
        assert "mercor-session-id" in pairs[0][1]["expression"]
        return [{"result": {"value": json.dumps({
            "local": {"mercor-auth-store": "private-auth-state"},
            "session": {"mercor-session-id": "private-session"},
        })}}]

    monkeypatch.setattr(module, "_calls", context_cookies)
    monkeypatch.setattr(module, "_page_calls", page_storage)
    result = module.commit_cookies(
        "mercor-task", ["mercor.com"], token="a" * 32, generation=1,
        origin="https://work.mercor.com", local_storage_keys=["mercor-auth-store"],
        session_storage_keys=["mercor-session-id"],
    )

    assert result["local_storage_committed"] == 1
    assert result["session_storage_committed"] == 1
    saved = json.loads(overlay_file.read_text(encoding="utf-8"))
    assert [row["origin"] for row in saved["origins"]] == [
        "https://unrelated.example", "https://work.mercor.com",
    ]
    assert saved["origins"][1]["localStorage"] == [{
        "name": "mercor-auth-store", "value": "private-auth-state",
    }]
    assert saved["origins"][1]["sessionStorage"] == [{
        "name": "mercor-session-id", "value": "private-session",
    }]


def test_seed_web_storage_targets_exact_origin_and_reloads(monkeypatch):
    module = load_module()
    calls = []

    async def page_calls(ws_url, pairs, timeout=None):
        calls.append((ws_url, pairs, timeout))
        return [{"result": {"value": 1}}]

    monkeypatch.setattr(module, "_page_calls", page_calls)
    count = module._seed_web_storage(
        "ws://leased-page",
        "https://work.mercor.com/explore",
        [{
            "origin": "https://work.mercor.com",
            "localStorage": [{"name": "mercor-auth-store", "value": "private"}],
            "sessionStorage": [{"name": "mercor-session-id", "value": "session"}],
        }],
    )

    assert count == 2
    expression = calls[0][1][0][1]["expression"]
    assert '"https://work.mercor.com"' in expression
    assert "localStorage.setItem" in expression
    assert "sessionStorage.setItem" in expression
    assert "setTimeout(()=>location.reload(),50)" in expression


def test_seed_web_storage_waits_for_navigation_execution_context(monkeypatch):
    module = load_module()
    attempts = []

    async def page_calls(_ws_url, _pairs, timeout=None):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise RuntimeError("Cannot find default execution context")
        return [{"result": {"value": 1}}]

    monkeypatch.setattr(module, "_page_calls", page_calls)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    assert module._seed_web_storage(
        "ws://leased-page", "https://work.mercor.com/explore",
        [{
            "origin": "https://work.mercor.com",
            "sessionStorage": [{"name": "mercor-session-id", "value": "session"}],
        }],
    ) == 1
    assert len(attempts) == 2


def test_acquire_disposes_context_when_local_storage_seed_fails(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setenv("CLOAK_CONTEXT_LEASES_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("CLOAK_SESSION_VAULT_FILE", str(tmp_path / "base.json"))
    overlay = tmp_path / "overlay.json"
    monkeypatch.setenv("CLOAK_SESSION_VAULT_WRITEBACK_FILE", str(overlay))
    overlay.write_text(json.dumps({
        "cookies": [],
        "origins": [{
            "origin": "https://work.mercor.com",
            "localStorage": [{"name": "mercor-auth-store", "value": "private"}],
        }],
    }), encoding="utf-8")
    seen = []

    async def calls(pairs, timeout=None):
        seen.append(pairs)
        if pairs == [("Target.createBrowserContext", {})]:
            return [{"browserContextId": "new-context"}]
        if pairs[0][0] == "Target.createTarget":
            return [{"targetId": "new-target"}]
        assert pairs == [(
            "Target.disposeBrowserContext", {"browserContextId": "new-context"}
        )]
        return [{}]

    monkeypatch.setattr(module, "_calls", calls)
    monkeypatch.setattr(
        module, "_seed_web_storage",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("seed failed")),
    )

    with pytest.raises(RuntimeError, match="seed failed"):
        module.acquire("mercor-task", url="https://work.mercor.com/explore")
    assert seen[-1] == [(
        "Target.disposeBrowserContext", {"browserContextId": "new-context"}
    )]
