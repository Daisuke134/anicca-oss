import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parent / "bounty-healthcheck.sh"
CLI = Path(__file__).parent / "bounty-cli.sh"
REPO = Path(__file__).parents[2]


def test_bounty_cli_resolves_shared_runner_from_repository(tmp_path):
    result = subprocess.run(
        ["/bin/bash", str(CLI)],
        env={**os.environ, "HOME": str(tmp_path), "AGENT_WIRING_PROBE_ONLY": "1"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert str(REPO / "skills/earn/marketing-engine/run_agent.sh") in result.stdout


def test_bounty_status_initializes_portable_state_root(tmp_path):
    state_root = tmp_path / "bounty"
    subprocess.run(
        ["/bin/bash", str(CLI), "--status"],
        env={**os.environ, "HOME": str(tmp_path), "BOUNTY_STATE_ROOT": str(state_root)},
        capture_output=True,
        text=True,
        check=True,
    )

    assert (state_root / "state").is_dir()
    assert (state_root / "logs").is_dir()


def test_stale_heartbeat_is_reported_without_launchd_recovery(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "mutation"
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env={
            **os.environ,
            "HOME": str(home),
            "BASH_FUNC_launchctl%%": f'() {{ printf x >> "{marker}"; return 0; }}',
            "BASH_FUNC_tmux%%": "() { return 1; }",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "stale/missing" in (
        home / ".local/state/life-manager/bounty/logs/bounty-core-healthcheck.log"
    ).read_text()
