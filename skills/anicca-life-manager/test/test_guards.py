import os, sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "skills/anicca-life-manager/scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills/_shared"))
import lateness_check as lc
def run():
    assert hasattr(lc, "life_manager_enabled"), "life_manager_enabled missing"
    assert lc.life_manager_enabled({"lifeManager": {"enabled": False}}) is False
    assert lc.life_manager_enabled({"lifeManager": {"enabled": True}}) is True
    assert lc.life_manager_enabled({}) is True
    assert lc.RELENTLESS_MAX_DEFAULT == 3, f"retry cap should be 3, got {getattr(lc,'RELENTLESS_MAX_DEFAULT','MISSING')}"
    with mock.patch.dict(os.environ, {"LIFE_MANAGER_TEST_VALUE": "runtime-injected"}, clear=False):
        assert lc.env("LIFE_MANAGER_TEST_VALUE") == "runtime-injected"
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.object(lc, "gemini_reachable", return_value=(True, "ok")), \
            mock.patch("urllib.request.urlopen") as urlopen:
        assert lc.place_lateness_call("test") is None
        urlopen.assert_not_called()
    with mock.patch.object(lc, "_wait_for_call_outcome") as wait_for_outcome:
        assert lc._wait_for_available_call(None) is None
        wait_for_outcome.assert_not_called()
        wait_for_outcome.return_value = ("completed", 30)
        assert lc._wait_for_available_call("CA123") == ("completed", 30)
        wait_for_outcome.assert_called_once_with("CA123", deadline_sec=120)
    print("PASS")
run()

def run_renraku():
    import renraku
    assert hasattr(renraku, "auto_send_allowed"), "auto_send_allowed missing"
    assert renraku.auto_send_allowed({}) is False
    assert renraku.auto_send_allowed({"lateness": {"autoSendMail": True}}) is True
    assert renraku.auto_send_allowed({"lateness": {"autoSendMail": False}}) is False
    with mock.patch.dict(os.environ, {"LIFE_MANAGER_TEST_VALUE": "runtime-injected"}, clear=False):
        assert renraku.env("LIFE_MANAGER_TEST_VALUE") == "runtime-injected"
    print("PASS renraku")
run_renraku()
