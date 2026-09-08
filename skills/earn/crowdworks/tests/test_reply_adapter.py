from pathlib import Path
import importlib.util
import sys


MODULE = Path(__file__).parents[1] / "scripts/reply_adapter.py"
SPEC = importlib.util.spec_from_file_location("crowdworks_reply_adapter_test", MODULE)
adapter_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter_module
SPEC.loader.exec_module(adapter_module)


def test_observation_uses_official_thread_and_message_ids():
    row = {"thread_id": 303996182, "id": 425906697}
    observed = adapter_module.CrowdWorksReplyAdapter._observation(row)
    assert observed["provider"] == "crowdworks"
    assert observed["thread_id"] == "303996182"
    assert observed["latest_event_id"] == "425906697"


def test_owner_enters_shared_reply_kernel():
    owner = MODULE.with_name("reply-owner").read_text(encoding="utf-8")
    assert "marketplace-core/scripts/reply_kernel.py" in owner
    assert "reply_adapter.py" in owner


def test_adapter_contains_no_provider_lifecycle_copy():
    source = MODULE.read_text(encoding="utf-8")
    assert "def run_wake" not in source
    assert "next_eligible_at" not in source
    assert "effect_key" not in source
