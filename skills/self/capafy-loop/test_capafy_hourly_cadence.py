import plistlib
from pathlib import Path


PLIST = Path(__file__).parent / "launchd" / "ai.anicca.capafy-loop-daily.plist"
DAILY = Path(__file__).parent / "capafy-loop-daily.sh"


def test_capafy_supply_owner_wakes_hourly_without_duplicate_schedule() -> None:
    config = plistlib.loads(PLIST.read_bytes())

    assert config["StartInterval"] == 3600
    assert config["ThrottleInterval"] == 60
    assert "StartCalendarInterval" not in config
    assert config["Label"] == "ai.anicca.capafy-loop-daily"


def test_daily_separates_immutable_execution_from_writable_public_source() -> None:
    script = DAILY.read_text(encoding="utf-8")

    assert 'LIFE_MANAGER_RELEASE_ROOT=' in script
    assert 'LIFE_MANAGER_SOURCE_REPO=' in script
    assert '[ -w "$LIFE_MANAGER_SOURCE_REPO" ]' in script
    assert 'CAPAFY_CATALOG_DIR="$LIFE_MANAGER_SOURCE_REPO/skills/capafy/catalog"' in script
    assert 'python3 "$LIFE_MANAGER_RELEASE_ROOT/skills/capafy-autopublish/scripts/inventory_status.py"' in script
    assert 'inside $LIFE_MANAGER_SOURCE_REPO/skills/capafy/catalog/<new-slug>/' in script


def test_every_healthy_terminal_refreshes_the_healthcheck_marker() -> None:
    script = DAILY.read_text(encoding="utf-8")

    assert 'HEALTHY_MARKER="$HOME/.local/state/life-manager/state/capafy-autopublish/.capafy-healthy-pass"' in script
    assert script.count("mark_healthy || exit 2") == 3


def test_selfheal_request_preempts_cap_full_offline_build() -> None:
    script = DAILY.read_text(encoding="utf-8")

    assert 'SELFHEAL_REQUEST="$HOME/.local/state/life-manager/state/capafy-loop-selfheal-request.json"' in script
    assert 'if [ "$VERDICT" = "CAP_FULL" ] && [ ! -f "$SELFHEAL_REQUEST" ]; then' in script


def test_browser_lane_calls_include_explicit_escalation_reason() -> None:
    script = DAILY.read_text(encoding="utf-8")

    assert script.count('--escalation-reason "authorized Capafy browser publishing workflow"') == 2
