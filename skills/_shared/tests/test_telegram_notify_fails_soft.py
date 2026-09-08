"""An alert must never be able to kill the loop that is reporting the problem.

Measured 2026-09-07: `session_vault_tick.sh` died every 30 minutes on this exact line, immediately
after logging `ALERT: session dead for: https://coconala.com/mypage/dashboard`. `${VAR:?msg}` exits
a non-interactive shell outright, and `|| true` at the call site does not catch a
parameter-expansion error, so the tick never reached anything after the alert -- including the
session re-banking that would have healed it. The alarm was ringing into a bell it had cut the rope
of, and Coconala applied to nothing for four days.

Run: python3 -m pytest skills/_shared/tests/test_telegram_notify_fails_soft.py
"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "telegram-notify.sh"


def _run(body: str, env: dict) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.pop("TELEGRAM_ALERT_CHAT_ID", None)
    full["HOME"] = env.pop("HOME", full["HOME"])
    full.update(env)
    return subprocess.run(
        ["bash", "-c", f'set -uo pipefail\n. "{SCRIPT}"\n{body}\n'],
        capture_output=True, text=True, env=full,
    )


def test_a_missing_chat_id_does_not_kill_the_caller(tmp_path):
    done = _run('telegram_notify "x" || true\necho STILL_ALIVE', {"HOME": str(tmp_path)})
    assert "STILL_ALIVE" in done.stdout
    assert done.returncode == 0


def test_a_dropped_alert_says_so_on_stderr(tmp_path):
    done = _run('telegram_notify "session dead" || true', {"HOME": str(tmp_path)})
    assert "no TELEGRAM_ALERT_CHAT_ID" in done.stderr
    assert "session dead" in done.stderr


def test_it_reads_the_id_from_the_fleet_env_file(tmp_path):
    """The id lives in the same .env the rest of the fleet loads; the tick never sourced it."""
    env_dir = tmp_path / ".local" / "state" / "life-manager"
    env_dir.mkdir(parents=True)
    (env_dir / ".env").write_text("TELEGRAM_ALERT_CHAT_ID=12345\n", encoding="utf-8")
    done = _run('telegram_notify "x" >/dev/null 2>&1; echo "rc=$?"', {"HOME": str(tmp_path)})
    # It got far enough to try the provider rather than dropping the alert for want of an id.
    assert "no TELEGRAM_ALERT_CHAT_ID" not in done.stderr


def test_an_explicit_id_still_wins(tmp_path):
    done = _run('telegram_notify "x" >/dev/null 2>&1; echo done',
                {"HOME": str(tmp_path), "TELEGRAM_ALERT_CHAT_ID": "999"})
    assert "done" in done.stdout
    assert "no TELEGRAM_ALERT_CHAT_ID" not in done.stderr


def test_it_uses_the_repository_owned_sender():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "../send-telegram.sh" in source
    assert "openclaw message send" not in source
