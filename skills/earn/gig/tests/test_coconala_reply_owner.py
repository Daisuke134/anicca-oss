import json
from pathlib import Path


REPO = Path(__file__).parents[4]


def test_owner_enters_shared_kernel_and_registry_is_finite():
    owner = REPO / "skills/earn/gig/scripts/coconala-reply-owner"
    text = owner.read_text(encoding="utf-8")
    assert "marketplace-core/scripts/reply_kernel.py" in text
    assert "coconala_reply_adapter.py" in text
    assert "reply_detector.py" not in text
    assert 'CLOAK_BROWSER_MAX_TABS_PER_OWNER="2"' in text

    registry = json.loads((REPO / "config/loop-registry.json").read_text())
    row = registry["loops"]["hf-gig-reply-detector"]
    assert row["entrypoint"] == "skills/earn/gig/scripts/coconala-reply-owner"
    assert row["cadence"] == {"start_interval_seconds": 300}
