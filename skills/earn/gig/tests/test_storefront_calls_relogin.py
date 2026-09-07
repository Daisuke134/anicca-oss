"""Noticing the session died is only half of it.

session_vault already carries relogin_coconala: it logs the account back in, banks the
result to the vault the lanes seed their contexts from, and rate-limits itself to one
attempt per cooldown. Nothing called it. So the wake that learned to name
storefront_session_expired still stopped there, and 241 wakes reported a dead session while
waiting for a person to log in by hand.

The wake asks once and reports what it got back. It never guarantees the session -- a
recovery that fails is reported beside the expiry, not swallowed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import storefront_direct as direct  # noqa: E402

SOURCE = (SCRIPTS / "storefront_direct.py").read_text(encoding="utf-8")


class _Vault:
    def __init__(self, answer):
        self._answer = answer

    def relogin_coconala(self):
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


@pytest.fixture
def vault(monkeypatch):
    def install(answer):
        monkeypatch.setattr(direct, "_session_vault", lambda: _Vault(answer))
    return install


def test_a_successful_relogin_is_reported_as_such(vault):
    vault({"ok": True, "dump": {"ok": True}})
    assert direct._relogin_coconala() == "relogin_ok"


def test_the_cooldown_is_reported_rather_than_treated_as_failure(vault):
    # The cooldown is the recovery working as designed, not an error.
    vault({"ok": False, "skipped": True, "reason": "relogin attempted 5min ago"})
    assert direct._relogin_coconala() == "relogin_cooldown"


def test_a_failed_relogin_carries_its_reason(vault):
    vault({"ok": False, "reason": "TimeoutError: login form"})
    assert direct._relogin_coconala().startswith("relogin_failed:")
    assert "TimeoutError" in direct._relogin_coconala()


def test_an_unavailable_recovery_never_raises_into_the_wake(vault):
    vault(RuntimeError("module gone"))
    assert direct._relogin_coconala() == "relogin_unavailable:RuntimeError"


def test_an_answer_that_is_not_a_mapping_is_named_not_assumed(vault):
    vault("yes")
    assert direct._relogin_coconala() == "relogin_answer_invalid"


def test_the_expiry_branch_actually_calls_it():
    block = SOURCE[SOURCE.index("def _read_official_catalog"):]
    block = block[:block.index("raise RuntimeError(failure)")]
    assert "_relogin_coconala()" in block, "detecting the expiry must trigger the recovery"


def test_the_wake_still_reports_the_expiry_alongside_the_recovery():
    # A recovered session must not hide that the session had died.
    block = SOURCE[SOURCE.index("def _read_official_catalog"):]
    block = block[:block.index("raise RuntimeError(failure)")]
    assert 'f"{failure}:{recovered}"' in block


def test_a_dead_session_still_stops_the_retry_loop():
    block = SOURCE[SOURCE.index("def _read_official_catalog"):]
    block = block[:block.index("raise RuntimeError(failure)")]
    assert "break" in block[block.index("_relogin_coconala()"):]
