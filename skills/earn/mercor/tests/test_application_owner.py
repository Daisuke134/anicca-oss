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
    assert 'LEASE_PYTHON="${LIFE_MANAGER_LEASE_PYTHON:-/usr/bin/python3}"' in source
    assert "MERCOR_RUN_EARNINGS_SYNC=0" in source
    assert "job_search_loop.mercor_page_ready" in source
    assert '--ws "$MERCOR_CDP_PAGE_WS"' in source
    assert 'commit-cookies "$TASK" --domain mercor.com' in source
    assert "job_search_loop.mercor_auth_readback" in source
    assert "job_search_loop.mercor_email_auth" in source
    assert '--after-epoch "$RUN_STARTED_AT"' in source
    assert source.count('"$ROOT/apps/job-search-loop/scripts/run-mercor.sh"') == 2
    assert source.index("job_search_loop.mercor_auth_readback") < source.index(
        'commit-cookies "$TASK" --domain mercor.com'
    )
    assert '"reason":"authenticated_readback_required"' in source
    assert '--origin https://work.mercor.com --local-storage-key mercor-auth-store' in source
    assert '--session-storage-key mercor-session-id --session-storage-key mercor-user-ip' in source
    assert 'CLOAK_SESSION_VAULT_WRITEBACK_FILE="$STATE_ROOT/auth-overlay.json"' in source
    assert 'session-writeback.json' in source
    assert '--token "$LEASE_TOKEN" --generation "$LEASE_GENERATION"' in source
    assert "9334" not in source


def test_mercor_pass_binds_exact_leased_page():
    source = (ROOT / "apps/job-search-loop/scripts/run-mercor.sh").read_text()
    prompt = (ROOT / "apps/job-search-loop/prompts/mercor-pass.md").read_text()
    assert "--cdp-page-ws" in source
    assert "drive only that exact leased page websocket" in prompt
