from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_owner_uses_shared_browser_lease_and_revenue_name():
    source = (ROOT / "skills/earn/mercor/scripts/application-owner").read_text()
    assert 'TASK="mercor-revenue-application"' in source
    assert 'CDP="http://127.0.0.1:9222"' in source
    assert 'cdp_context_lease.py' in source
    assert 'MERCOR_CDP_PAGE_WS' in source
    assert 'MERCOR_APPLICATION_STATE_ROOT="$STATE_ROOT"' in source
    assert 'LEGACY_ROOT="${STATE_ROOT:h}"' in source
    assert "9334" not in source


def test_mercor_pass_binds_exact_leased_page():
    source = (ROOT / "apps/job-search-loop/scripts/run-mercor.sh").read_text()
    prompt = (ROOT / "apps/job-search-loop/prompts/mercor-pass.md").read_text()
    assert "--cdp-page-ws" in source
    assert "drive only that exact leased page websocket" in prompt
