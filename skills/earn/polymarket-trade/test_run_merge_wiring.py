#!/usr/bin/env python3
"""Integration tests for the real PM live-pass shell control flow.

The external wallet/SDK boundary is replaced by a tiny executable, while the
real run.sh still owns ordering, exit-code handling, and trace persistence.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


RUN_SH = Path(__file__).with_name("run.sh")
EXPECTED_CALLS = [
    "fund_via_bridge.py",
    "redeem.py",
    "merge.py",
    "bundle_arb.py",
    "market_maker.py",
    "pinnacle_observe.py",
    "pick.py",
]


def _build_harness(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        """#!/bin/bash
set -u
if [ "$#" -eq 0 ]; then exec /usr/bin/python3; fi
if [ "$1" = -c ]; then exec /usr/bin/python3 "$@"; fi
script_name="$(basename "$1")"
if [ "$script_name" = bounded-exec.py ]; then shift 3; exec "$0" "$@"; fi
printf '%s\\n' "$script_name" >> "$PM_TEST_CALLS"
case "$script_name" in
  fund_via_bridge.py) ;;
  redeem.py) echo "redeem-ok" ;;
  merge.py)
    echo "merge-result"
    exit "${PM_TEST_MERGE_RC:-0}"
    ;;
  bundle_arb.py) echo "arb-ok" ;;
  market_maker.py) echo "market-maker-ok" ;;
  pinnacle_observe.py) ;;
  pick.py) printf '%s\\n' '{"action":"WAIT","reason":"controlled-test"}' ;;
  *) echo "unexpected script: $script_name" >&2; exit 90 ;;
esac
"""
    )
    fake_python.chmod(0o755)

    fake_node = tmp_path / "bin" / "node"
    fake_node.write_text(
        "#!/bin/sh\ncase \"$*\" in *resolve-identity.mjs*) printf '0x%s\\n' '" + ("1" * 64) + "' ;; esac\n"
    )
    fake_node.chmod(0o755)

    wallet_home = tmp_path / "wallet"
    wallet_home.mkdir()
    env_file = tmp_path / "life-manager.env"
    env_file.write_text("")

    calls_file = tmp_path / "calls.txt"
    return fake_python, wallet_home, env_file, calls_file


def _run_live_pass(tmp_path: Path, merge_rc: int = 0) -> tuple[subprocess.CompletedProcess[str], list[str], list[dict]]:
    fake_python, wallet_home, env_file, calls_file = _build_harness(tmp_path)
    state_root = tmp_path / "state"
    env = {
        **os.environ,
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LIFE_MANAGER_ENV_FILE": str(env_file),
        "LIFE_MANAGER_PYTHON": str(fake_python),
        "LIFE_MANAGER_NODE": str(fake_python.parent / "node"),
        "LIFE_MANAGER_STATE_ROOT": str(state_root),
        "LIFE_MANAGER_WALLET_HOME": str(wallet_home),
        "PM_DEPOSIT_WALLET": "0x" + ("2" * 40),
        "PM_TEST_CALLS": str(calls_file),
        "PM_TEST_MERGE_RC": str(merge_rc),
    }
    result = subprocess.run(
        ["/bin/bash", str(RUN_SH)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    calls = calls_file.read_text().splitlines()
    trace_file = state_root / "pm-trade.trace.jsonl"
    traces = [
        json.loads(line)
        for line in trace_file.read_text().splitlines()
        if line.strip()
    ]
    return result, calls, traces


def test_live_pass_recovers_balanced_positions_before_cash_gated_strategies(tmp_path: Path) -> None:
    """Removing or moving the merge invocation must break the observed call order."""
    result, calls, _ = _run_live_pass(tmp_path)

    assert result.returncode == 0, result.stderr
    assert calls == EXPECTED_CALLS


def test_merge_failure_is_traced_and_does_not_disable_later_strategies(tmp_path: Path) -> None:
    """A temporary merge/RPC failure must not turn the earning pass into a one-way stop."""
    result, calls, traces = _run_live_pass(tmp_path, merge_rc=7)

    assert result.returncode == 0, result.stderr
    assert calls == EXPECTED_CALLS
    assert any(row.get("action") == "merge" and row.get("exit") == 7 for row in traces)
