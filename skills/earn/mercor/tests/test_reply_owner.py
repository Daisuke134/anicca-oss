import json
import os
from pathlib import Path
import subprocess

import pytest


OWNER = Path(__file__).parents[1] / "scripts/reply-owner"


FAKE_PYTHON = r'''#!/usr/bin/env python3
import json, os, pathlib, sys

log = pathlib.Path(os.environ["CALL_LOG"])
def record(value):
    with log.open("a") as stream:
        stream.write(value + "\n")

args = sys.argv[1:]
if args and args[0].endswith("cdp_context_lease.py"):
    command = args[1]
    count_path = pathlib.Path(os.environ["ACQUIRE_COUNT"])
    count = int(count_path.read_text()) if count_path.exists() else 0
    if command == "acquire":
        count += 1
        count_path.write_text(str(count))
        record(f"acquire:{count}")
        if os.environ.get("FAIL_ACQUIRE_AT") == str(count):
            raise SystemExit(1)
        print(json.dumps({"ws": f"ws://127.0.0.1/{count}", "token": f"token-{count}",
                          "generation": count}))
    elif command == "release":
        record("release:" + ":".join(args[args.index("--token") + 1:]))
        if os.environ.get("FAIL_RELEASE") == "1":
            raise SystemExit(1)
        print('{"ok":true}')
    elif command == "park":
        record("park:" + ":".join(args[args.index("--token") + 1:]))
        print('{"ok":true}')
    elif command == "commit-cookies":
        record("commit")
        print('{"ok":true}')
    raise SystemExit(0)

if args[:1] == ["-c"]:
    print("owner@example.com")
    raise SystemExit(0)

if len(args) >= 2 and args[0] == "-m":
    module = args[1]
    output = pathlib.Path(args[args.index("--output") + 1])
    if module.endswith("mercor_page_ready"):
        count_path = pathlib.Path(os.environ["PAGE_COUNT"])
        count = int(count_path.read_text()) + 1 if count_path.exists() else 1
        count_path.write_text(str(count)); record(f"page:{count}")
        output.write_text('{"ok":true}\n')
        if os.environ.get("FAIL_PAGE_AT") == str(count):
            raise SystemExit(1)
    elif module.endswith("mercor_auth_readback"):
        count_path = pathlib.Path(os.environ["AUTH_COUNT"])
        count = int(count_path.read_text()) + 1 if count_path.exists() else 1
        count_path.write_text(str(count)); record(f"auth:{count}")
        results = os.environ["AUTH_RESULTS"].split(",")
        ok = count <= len(results) and results[count - 1] == "pass"
        output.write_text(json.dumps({"status": "authenticated" if ok else "logged_out"}) + "\n")
        raise SystemExit(0 if ok else 2)
    elif module.endswith("mercor_reply_snapshot"):
        record("snapshot")
        output.write_text(json.dumps({"version": 1, "observed_at": "2026-09-09T00:00:00Z",
                                      "applications": {"applications": []}, "notifications": [],
                                      "assessments": [], "contracts": [], "interviews": [],
                                      "gmail": []}) + "\n")
    raise SystemExit(0)

record("kernel")
raise SystemExit(0)
'''


def run_owner(tmp_path, *, auth="pass", fail_release=False, fail_acquire_at=None,
              fail_page_at=None):
    home = tmp_path / "home"
    profile = home / ".config/anicca/job-search/profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text('{"candidate":{"application_email":"owner@example.com"}}')
    overlay = home / ".local/state/anicca/job-search/mercor/application/auth-overlay.json"
    overlay.parent.mkdir(parents=True)
    overlay.write_text('{"cookies":[],"origins":[]}')
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "LIFE_MANAGER_STATE_ROOT": str(tmp_path / "state"),
        "LIFE_MANAGER_LEASE_PYTHON": str(fake),
        "LIFE_MANAGER_PYTHON": str(fake),
        "CALL_LOG": str(tmp_path / "calls.log"),
        "ACQUIRE_COUNT": str(tmp_path / "acquire-count"),
        "PAGE_COUNT": str(tmp_path / "page-count"),
        "AUTH_COUNT": str(tmp_path / "auth-count"),
        "AUTH_RESULTS": auth,
        "FAIL_RELEASE": "1" if fail_release else "0",
        "FAIL_ACQUIRE_AT": str(fail_acquire_at or ""),
        "FAIL_PAGE_AT": str(fail_page_at or ""),
    }
    done = subprocess.run(["zsh", str(OWNER)], env=env, text=True,
                          capture_output=True, check=False)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    return done, calls


def test_authenticated_context_is_reused_without_release(tmp_path):
    done, calls = run_owner(tmp_path, auth="pass")
    assert done.returncode == 0, done.stderr
    assert calls == ["acquire:1", "page:1", "auth:1", "snapshot", "commit", "kernel",
                     "park:token-1:--generation:1"]


def test_failed_auth_refreshes_once_with_fenced_release(tmp_path):
    done, calls = run_owner(tmp_path, auth="fail,pass")
    assert done.returncode == 0, done.stderr
    assert calls[:6] == ["acquire:1", "page:1", "auth:1",
                         "release:token-1:--generation:1", "acquire:2", "page:2"]
    assert calls.count("auth:2") == 1
    assert "kernel" in calls


@pytest.mark.parametrize(
    ("kwargs", "expected", "forbidden"),
    [
        ({"auth": "fail,fail"}, "park:token-2:--generation:2", "kernel"),
        ({"auth": "fail", "fail_release": True}, "park:token-1:--generation:1", "acquire:2"),
        ({"auth": "fail", "fail_acquire_at": 2}, "release:token-1:--generation:1", "park:"),
        ({"auth": "pass", "fail_page_at": 1}, "park:token-1:--generation:1", "auth:1"),
    ],
)
def test_refresh_failures_stop_safely(tmp_path, kwargs, expected, forbidden):
    done, calls = run_owner(tmp_path, **kwargs)
    assert done.returncode != 0
    assert expected in calls
    assert not any(call.startswith(forbidden) for call in calls)
