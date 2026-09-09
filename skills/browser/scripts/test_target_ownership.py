"""Regression tests for loop-owned CDP targets.

The production browser is shared by several loops. A loop may only close targets
that it registered under its own owner name; unowned and foreign targets are
never garbage-collected.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cdp_default_tab as default_tab  # noqa: E402
import cdp_tab_gc as tab_gc  # noqa: E402
import target_ownership as ownership  # noqa: E402


def test_idle_context_parks_when_owner_requests_reuse(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    monkeypatch.setenv("CLOAK_CONTEXT_PARK_ON_IDLE", "1")
    calls = []
    monkeypatch.setattr(
        default_tab.cdp_context_lease, "park",
        lambda owner: calls.append(("park", owner)) or {"ok": True},
    )
    monkeypatch.setattr(
        default_tab.cdp_context_lease, "release",
        lambda owner: calls.append(("release", owner)) or {"ok": True},
    )

    default_tab._release_context_if_idle("gig-reply-detector")

    assert calls == [("park", "gig-reply-detector")]


def test_registry_release_refuses_foreign_owner(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))

    ownership.claim_target("gig-target", "gig-pass")
    ownership.claim_target("other-target", "article-loop")

    assert ownership.release_target("gig-target", "article-loop") is False
    assert ownership.targets_for_owner("gig-pass") == {"gig-target"}
    assert ownership.targets_for_owner("article-loop") == {"other-target"}


def test_registry_fails_closed_at_owner_target_limit(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("first", "paid-room", max_targets=1)

    with pytest.raises(RuntimeError, match="browser_tab_limit"):
        ownership.claim_target("second", "paid-room", max_targets=1)

    assert ownership.targets_for_owner("paid-room") == {"first"}


def test_registry_defaults_to_one_target_per_owner(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("first", "paid-room")

    with pytest.raises(RuntimeError, match="browser_tab_limit"):
        ownership.claim_target("second", "paid-room")


def test_registry_prunes_only_targets_missing_from_cdp(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("live-foreign", "article-loop", max_targets=2)
    ownership.claim_target("stale-foreign", "article-loop", max_targets=2)
    ownership.claim_target("stale-caller", "gig-pass")

    assert ownership.prune_missing_targets({"live-foreign", "unregistered"}) == 2
    assert ownership.targets_for_owner("article-loop") == {"live-foreign"}
    assert ownership.targets_for_owner("gig-pass") == set()


def test_gc_selects_only_callers_owned_surplus_targets(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("gig-keep", "gig-pass", max_targets=2)
    ownership.claim_target("gig-close", "gig-pass", max_targets=2)
    ownership.claim_target("foreign", "article-loop")

    tabs = [
        {"id": "gig-keep", "type": "page", "url": "https://coconala.com/mypage"},
        {"id": "gig-close", "type": "page", "url": "https://coconala.com/requests/1"},
        {"id": "foreign", "type": "page", "url": "https://coconala.com/requests/2"},
        {"id": "unowned", "type": "page", "url": "about:blank"},
    ]

    assert tab_gc.select_doomed_target_ids(tabs, "gig-pass", keep_coconala=1) == [
        "gig-close"
    ]


def test_default_tab_close_refuses_target_owned_by_another_loop(
    tmp_path, monkeypatch
):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("foreign", "article-loop")
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(default_tab, "_call", fake_call)

    with pytest.raises(PermissionError):
        default_tab.close_tab("foreign", owner="gig-pass")

    assert calls == []


def test_closing_last_owned_tab_releases_owner_context(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("owned", "paid")
    calls = []
    released = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(default_tab, "_call", fake_call)
    monkeypatch.setattr(
        default_tab.cdp_context_lease,
        "release",
        lambda owner: released.append(owner) or {"ok": True},
    )

    assert default_tab.close_tab("owned", owner="paid")["ok"] is True
    assert calls == [("Target.closeTarget", {"targetId": "owned"})]
    assert released == ["paid"]


def test_closing_one_of_multiple_owned_tabs_keeps_context(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("first", "paid", max_targets=2)
    ownership.claim_target("second", "paid", max_targets=2)

    async def fake_call(_method, _params=None):
        return {}

    monkeypatch.setattr(default_tab, "_call", fake_call)
    monkeypatch.setattr(
        default_tab.cdp_context_lease,
        "release",
        lambda _owner: pytest.fail("context released while another tab remained"),
    )

    assert default_tab.close_tab("first", owner="paid")["ok"] is True
    assert ownership.targets_for_owner("paid") == {"second"}


def test_visible_tab_uses_owned_browser_context(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    calls = []
    monkeypatch.setattr(default_tab, "_lease", lambda owner: {
        "ok": True, "context_id": f"context-{owner}",
    })

    async def fake_call(method, params=None):
        calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": []}
        return {"targetId": "visible-1"}

    monkeypatch.setattr(default_tab, "_call", fake_call)

    row = default_tab.open_tab(
        "https://coconala.com/talkrooms/18211957", owner="paid",
    )

    assert calls == [("Target.getTargets", None), ("Target.createTarget", {
        "url": "https://coconala.com/talkrooms/18211957",
        "browserContextId": "context-paid",
        "background": False,
    })]
    assert row["target_id"] == "visible-1"
    assert row["context"] == "context-paid"
    assert ownership.owner_for_target("visible-1") == "paid"


def test_visible_tab_closes_new_target_when_owner_is_at_limit(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    monkeypatch.setenv("CLOAK_BROWSER_MAX_TABS_PER_OWNER", "1")
    ownership.claim_target("existing", "paid")
    monkeypatch.setattr(default_tab, "_lease", lambda owner: {
        "ok": True, "context_id": f"context-{owner}",
    })
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "existing"}]}
        return {"targetId": "surplus"}

    monkeypatch.setattr(default_tab, "_call", fake_call)

    with pytest.raises(RuntimeError, match="browser_tab_limit"):
        default_tab.open_tab("https://coconala.com/talkrooms/2", owner="paid")

    assert calls[-1] == ("Target.closeTarget", {"targetId": "surplus"})
    assert ownership.targets_for_owner("paid") == {"existing"}


def test_visible_tab_prunes_stale_owner_row_before_enforcing_limit(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    monkeypatch.setenv("CLOAK_BROWSER_MAX_TABS_PER_OWNER", "1")
    ownership.claim_target("dead-target", "paid")
    ownership.claim_target("foreign-live", "other")
    monkeypatch.setattr(default_tab, "_lease", lambda owner: {
        "ok": True, "context_id": f"context-{owner}",
    })
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "foreign-live"}]}
        return {"targetId": "visible-1"}

    monkeypatch.setattr(default_tab, "_call", fake_call)

    row = default_tab.open_tab("https://coconala.com/talkrooms/2", owner="paid")

    assert row["target_id"] == "visible-1"
    assert ownership.targets_for_owner("paid") == {"visible-1"}
    assert ownership.targets_for_owner("other") == {"foreign-live"}
    assert calls[0] == ("Target.getTargets", None)


def test_prune_keeps_same_owner_claim_created_after_snapshot(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("dead-target", "paid", max_targets=2)

    async def fake_call(method, params=None):
        assert (method, params) == ("Target.getTargets", None)
        ownership.claim_target("new-after-snapshot", "paid", max_targets=2)
        return {"targetInfos": [{"targetId": "new-after-snapshot"}]}

    monkeypatch.setattr(default_tab, "_call", fake_call)

    assert asyncio.run(default_tab._prune_missing_target_rows("paid")) == 1
    assert ownership.targets_for_owner("paid") == {"new-after-snapshot"}


@pytest.mark.parametrize("result", [{}, {"targetInfos": [None]}, {"targetInfos": [{}]}])
def test_prune_fails_closed_when_official_target_list_is_invalid(
    tmp_path, monkeypatch, result,
):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    ownership.claim_target("existing", "paid")

    async def fake_call(_method, _params=None):
        return result

    monkeypatch.setattr(default_tab, "_call", fake_call)

    with pytest.raises(RuntimeError, match="targetInfos"):
        asyncio.run(default_tab._prune_missing_target_rows("paid"))
    assert ownership.targets_for_owner("paid") == {"existing"}


def test_hidden_tab_closes_target_before_releasing_ownership(tmp_path, monkeypatch):
    registry = tmp_path / "target-owners.json"
    monkeypatch.setenv("CLOAK_TARGET_OWNERS_FILE", str(registry))
    monkeypatch.setenv("CLOAK_BROWSER_MAX_TABS_PER_OWNER", "2")
    ownership.claim_target("dead-hidden-1", "paid", max_targets=2)
    ownership.claim_target("dead-hidden-2", "paid", max_targets=2)
    sent = []
    def nested_lease(owner):
        async def lease_result():
            return {"ok": True, "context_id": f"context-{owner}"}

        coroutine = lease_result()
        try:
            return asyncio.run(coroutine)
        except RuntimeError:
            coroutine.close()
            raise

    monkeypatch.setattr(default_tab, "_lease", nested_lease)
    monkeypatch.setattr(
        default_tab.cdp_context_lease, "release", lambda _owner: {"ok": True}
    )

    class FakeWebSocket:
        async def send(self, payload):
            sent.append(json.loads(payload))

        async def recv(self):
            request = sent[-1]
            request_id = request["id"]
            if request["method"] == "Target.getTargets":
                return json.dumps({"id": request_id, "result": {"targetInfos": []}})
            if request["method"] == "Target.createTarget":
                return json.dumps({"id": 1, "result": {"targetId": "hidden-1"}})
            return json.dumps({"id": 2, "result": {"success": True}})

    class FakeConnection:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(default_tab, "_browser_ws", lambda: "ws://browser")
    monkeypatch.setattr(default_tab.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(default_tab, "_browser_ws", lambda: "ws://browser")
    monkeypatch.setattr(
        default_tab.sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(read=lambda: b"")),
    )

    asyncio.run(default_tab._serve_hidden_tab("https://coconala.com", owner="paid"))

    assert [row["method"] for row in sent] == [
        "Target.getTargets", "Target.createTarget", "Target.closeTarget",
    ]
    assert sent[1]["params"]["browserContextId"] == "context-paid"
    assert sent[-1]["params"] == {"targetId": "hidden-1"}
    assert ownership.owner_for_target("hidden-1") is None
    assert ownership.targets_for_owner("paid") == set()
