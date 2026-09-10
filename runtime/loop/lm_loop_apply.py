"""Fail-closed plist generation and one-label rollback-safe launchd swap."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from runtime.loop.macos_loop_registry import validate_registry


_IMMUTABLE_RELEASE_WORKING_DIRECTORY = re.compile(
    r"(?:^|/)loops/(?:releases|[^/]+/releases)/"
    r"[0-9]{8}T[0-9]{6}-[0-9a-f]{8,40}(?:/|$)"
)
_PRIVATE_LOG_LOOP_IDS = frozenset({
    "money-printer-symphony-bridge",
    "money-printer-symphony",
})


def _is_immutable_release_working_directory(value: object) -> bool:
    return isinstance(value, str) and bool(_IMMUTABLE_RELEASE_WORKING_DIRECTORY.search(value))


def _plist(loop_id: str, entry: dict, release_root: Path, release_sha: str) -> bytes:
    executable = str(release_root / entry["entrypoint"])
    loop_runner = str(release_root / "bin/lm-loop-run")
    log_root = os.path.expanduser(entry["log_root"])
    value = {
        "Label": entry["label"],
        "ProgramArguments": [loop_runner, loop_id, str(release_root)],
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "LIFE_MANAGER_LOOP_ID": loop_id,
            "LIFE_MANAGER_REPO": str(release_root),
            "LIFE_MANAGER_RELEASE_SHA": release_sha,
            "LIFE_MANAGER_STATE_ROOT": os.path.expanduser(entry["state_root"]),
        },
        "StandardOutPath": str(Path(log_root) / "launchd.out.log"),
        "StandardErrorPath": str(Path(log_root) / "launchd.err.log"),
    }
    browser_owner = entry.get("browser_owner")
    if browser_owner:
        value["EnvironmentVariables"].update({
            "LIFE_MANAGER_BROWSER_CDP_PORT": str(browser_owner["cdp_port"]),
            "LIFE_MANAGER_BROWSER_PROFILE": os.path.expanduser(browser_owner["profile"]),
        })
    if loop_id in {"alpaca-investment", "alpaca-investment-shadow"}:
        mode = "shadow" if loop_id.endswith("-shadow") else "paper"
        value["EnvironmentVariables"].update({
            "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
            "LIFE_MANAGER_INVESTMENT_MODE": mode,
            "ALPACA_INVESTMENT_PAPER_CREDENTIALS_FILE": str(
                Path.home() / ".local/share/anicca/credentials.json"
            ),
            "ALPACA_INVESTMENT_PAPER_STATE_DIR": (str(
                Path.home() / ".local/state/life-manager/alpaca-investment"
            ) if mode == "shadow" else os.path.expanduser(entry["state_root"])),
        })
        if mode == "shadow":
            value["EnvironmentVariables"].update({
                "ALPACA_INVESTMENT_SHADOW_CREDENTIALS_FILE": str(
                    Path.home() / ".local/share/anicca/credentials.json"
                ),
                "ALPACA_INVESTMENT_SHADOW_STATE_DIR": os.path.expanduser(entry["state_root"]),
            })
    if loop_id in _PRIVATE_LOG_LOOP_IDS:
        value["Umask"] = 0o077
    if loop_id in {"hf-gig-apply-direct", "hf-gig-storefront-direct", "hf-gig-paid-direct"}:
        value["EnvironmentVariables"]["CLOAK_SESSION_VAULT_FILE"] = str(
            Path.home() / ".cloak/vault/gig-daily-driver/auth-state.json"
        )
    if loop_id == "hf-gig-apply-direct":
        value["EnvironmentVariables"]["CLOAK_CONTEXT_PARK_ON_IDLE"] = "1"
    if loop_id == "hf-gig-reply-detector":
        value["EnvironmentVariables"].update({
            "CLOAK_CDP_BASE_URL": "http://127.0.0.1:9222",
            "CLOAK_SESSION_VAULT_FILE": str(
                Path.home() / ".cloak/vault/gig-daily-driver/auth-state.json"
            ),
            "CLOAK_CONTEXT_LEASES_FILE": str(
                Path.home() / ".cloak/vault/coconala-reply-leases.json"
            ),
            "CLOAK_TARGET_OWNERS_FILE": str(
                Path.home() / ".cloak/vault/coconala-reply-targets.json"
            ),
            "CLOAK_CONTEXT_PARK_ON_IDLE": "1",
            "GIG_CDP_HEALTH_URL": "http://127.0.0.1:9222/json/version",
        })
    if loop_id == "life-manager-cfo-hourly":
        value["EnvironmentVariables"]["LIFE_MANAGER_ENV_FILE"] = str(
            Path.home() / ".local/state/life-manager/.env"
        )
    if loop_id == "realtime-guide":
        value["EnvironmentVariables"].update({
            "LIFE_MANAGER_ENV_FILE": str(Path.home() / ".local/state/life-manager/.env"),
            "LIFE_MANAGER_PYTHON": str(
                Path.home() / ".local/share/life-manager/venv/bin/python"
            ),
        })
    if loop_id == "lateness-heartbeat":
        timeout = shutil.which("timeout")
        if not timeout or not Path(timeout).is_absolute():
            raise ValueError("lateness-heartbeat: timeout executable is unavailable")
        value["EnvironmentVariables"].update({
            "LIFE_MANAGER_ENV_FILE": str(Path.home() / ".local/state/life-manager/.env"),
            "LIFE_MANAGER_PYTHON": str(
                Path.home() / ".local/share/life-manager/venv/bin/python"
            ),
            "LIFE_MANAGER_TIMEOUT": timeout,
        })
    key, cadence = next(iter(entry["cadence"].items()))
    if key == "start_interval_seconds":
        value["StartInterval"] = cadence
        if cadence < 10:
            value["ThrottleInterval"] = cadence
    elif key == "calendar_interval":
        value["StartCalendarInterval"] = cadence
    elif key == "run_at_load":
        value["RunAtLoad"] = True
    else:
        value["KeepAlive"] = True
    if loop_id.startswith(("article-", "writer-")):
        writer_root = str(release_root / "skills/writer-agent")
        writer_state = os.path.expanduser(entry["state_root"])
        value["EnvironmentVariables"].update({
            "ARTICLE_ROOT": writer_root, "ARTICLE_SKILL_DIR": writer_root,
            "ARTICLE_STATE_DIR": writer_state, "WRITER_STATE_DIR": writer_state,
            "WRITER_LOG_DIR": os.path.expanduser(entry["log_root"]),
            "LIFE_MANAGER_ENV_FILE": str(Path.home() / ".local/state/life-manager/.env"),
            "LIFE_MANAGER_PYTHON": str(
                Path.home() / ".local/share/life-manager/venv/bin/python"
            ),
            "LIFE_MANAGER_REPO": str(release_root),
        })
    if loop_id == "agent-economy-loop":
        agent_economy_state = os.path.expanduser(entry["state_root"])
        agent_economy_home = str(Path(agent_economy_state) / "instance")
        earn_state = str(Path(agent_economy_home) / "state/skills/earn")
        value["EnvironmentVariables"].update({
            "ANICCA_REPO": str(release_root),
            "ANICCA_CODE_ROOT": str(release_root),
            "ANICCA_RELEASE_ROOT": str(release_root.parent.parent),
            "ANICCA_HOME": agent_economy_home,
            "EARN_STATE_ROOT": earn_state,
            "EARN_LEDGER": str(Path(earn_state) / "earn-ledger.jsonl"),
        })
    if loop_id in {"franklin-loop", "franklin2-loop"}:
        value["EnvironmentVariables"].update({
            "ANICCA_REPO": str(release_root),
            "ANICCA_HOME": os.path.expanduser(entry["state_root"]),
            "ANICCA_INSTANCE": "franklin" if loop_id == "franklin-loop" else "franklin2",
        })
    if loop_id == "compute-proxy":
        node = shutil.which("node")
        if not node or not Path(node).is_absolute():
            raise ValueError("compute-proxy: managed node executable is unavailable")
        value["EnvironmentVariables"].update({
            "ANICCA_HOME": os.path.expanduser(entry["state_root"]),
            "COMPUTE_PROXY_PORT": "18402",
            "LIFE_MANAGER_NODE": node,
        })
    if loop_id in {"pm-decision-loop", "pm-live-trade"}:
        node = shutil.which("node")
        if not node or not Path(node).is_absolute():
            raise ValueError(f"{loop_id}: managed node executable is unavailable")
        value["EnvironmentVariables"].update({
            "LIFE_MANAGER_ENV_FILE": str(Path.home() / ".local/state/life-manager/.env"),
            "LIFE_MANAGER_NODE": node,
            "LIFE_MANAGER_PYTHON": str(
                Path.home() / ".local/share/life-manager/venv/bin/python"
            ),
        })
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def build_apply_plan(registry: dict, release_root: Path, release_sha: str) -> list[dict]:
    validate_registry(registry)
    if not re.fullmatch(r"[0-9a-f]{40}", release_sha):
        raise ValueError("release SHA must be exact 40-character lowercase hex")
    release_root = release_root.resolve()
    try:
        manifest = json.loads((release_root / "RELEASE.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("release manifest missing or invalid") from exc
    if manifest.get("sha") != release_sha:
        raise ValueError("release manifest SHA mismatch")
    loop_runner = release_root / "bin/lm-loop-run"
    if not loop_runner.is_file() or not os.access(loop_runner, os.X_OK):
        raise ValueError("release loop runner missing or not executable")
    plan = []
    for loop_id in sorted(registry["loops"]):
        entry = registry["loops"][loop_id]
        executable = release_root / entry["entrypoint"]
        if not executable.is_file():
            raise ValueError(f"{loop_id}: missing entrypoint {entry['entrypoint']}")
        if not os.access(executable, os.X_OK):
            raise ValueError(f"{loop_id}: entrypoint is not executable {entry['entrypoint']}")
        if loop_id == "life-manager-connector-native":
            dependencies = ("playwright-core", "jsqr")
            missing = [name for name in dependencies if not (
                release_root / "apps/life-manager/node_modules" / name / "package.json"
            ).is_file()]
            if missing:
                raise ValueError("life-manager-connector-native: Connector runtime dependencies missing")
        plan.append({
            "loop_id": loop_id,
            "label": entry["label"],
            "plist_bytes": _plist(loop_id, entry, release_root, release_sha),
            "expected_arguments": [str(loop_runner), loop_id, str(release_root)],
            "release_sha": release_sha,
        })
    return plan


def apply_registry(registry: dict, release_root: Path, release_sha: str,
                   installer: Callable[[dict], dict], *, target: str | None = None) -> list[dict]:
    if target is not None:
        if target not in registry.get("loops", {}):
            raise ValueError(f"unknown apply target: {target}")
        registry = {**registry, "loops": {target: registry["loops"][target]}}
    plan = build_apply_plan(registry, release_root, release_sha)
    return [installer(item) for item in plan]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _preserve_operational_attributes(new_bytes: bytes, old_bytes: bytes | None,
                                     *, retired_environment_keys: tuple[str, ...] = (),
                                     retired_operational_keys: tuple[str, ...] = ()) -> bytes:
    if old_bytes is None:
        return new_bytes
    old, new = plistlib.loads(old_bytes), plistlib.loads(new_bytes)
    for key in ("WorkingDirectory", "ProcessType", "RunAtLoad", "ThrottleInterval", "Umask", "Nice"):
        if key in old and key not in retired_operational_keys and not (
            key == "WorkingDirectory"
            and (key not in new or _is_immutable_release_working_directory(old[key]))
        ):
            new[key] = old[key]
    preserved_env = {
        key: value
        for key, value in (old.get("EnvironmentVariables") or {}).items()
        if key != "CODEX_HOME" and key not in retired_environment_keys
    }
    new["EnvironmentVariables"] = {
        **preserved_env,
        **(new.get("EnvironmentVariables") or {}),
    }
    return plistlib.dumps(new, fmt=plistlib.FMT_XML, sort_keys=True)


def _secure_log_paths(plist_bytes: bytes) -> None:
    plist = plistlib.loads(plist_bytes)
    state_root = Path(plist["EnvironmentVariables"]["LIFE_MANAGER_STATE_ROOT"])
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_root.chmod(0o700)
    paths = {plist["StandardOutPath"], plist["StandardErrorPath"]}
    for raw_path in paths:
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)


def _ensure_runtime_roots(plist_bytes: bytes) -> None:
    plist = plistlib.loads(plist_bytes)
    state_root = Path(plist["EnvironmentVariables"]["LIFE_MANAGER_STATE_ROOT"])
    state_root.mkdir(parents=True, exist_ok=True)
    for key in ("StandardOutPath", "StandardErrorPath"):
        Path(plist[key]).parent.mkdir(parents=True, exist_ok=True)


def _loaded_arguments(text: str) -> list[str]:
    arguments, inside = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "arguments = {":
            inside = True
        elif inside and line == "}":
            break
        elif inside and line:
            arguments.append(line)
    return arguments


def install_one(item: dict, target: Path,
                launchctl: Callable[[list[str]], tuple[int, str]], *, attempts: int = 3,
                sleeper: Callable[[float], None] = time.sleep,
                preserve_unloaded: bool = False,
                retired_environment_keys: tuple[str, ...] = (),
                retired_operational_keys: tuple[str, ...] = ()) -> dict:
    label = item["label"]
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    _ensure_runtime_roots(item["plist_bytes"])
    if plistlib.loads(item["plist_bytes"]).get("Umask") == 0o077:
        _secure_log_paths(item["plist_bytes"])
    old_bytes = target.read_bytes() if target.is_file() else None
    initial_rc, _ = launchctl(["print", service])
    was_loaded = initial_rc == 0
    _atomic_write(target, _preserve_operational_attributes(
        item["plist_bytes"], old_bytes,
        retired_environment_keys=retired_environment_keys,
        retired_operational_keys=retired_operational_keys))
    if preserve_unloaded and not was_loaded:
        return {
            "ok": True,
            "label": label,
            "loaded": False,
            "loaded_arguments": [],
            "installed_arguments": item["expected_arguments"],
            "release_sha": item["release_sha"],
        }
    launchctl(["bootout", service])
    sleeper(1.0)
    last_detail = ""
    retry_delays = (3.0, 10.0)
    for attempt in range(attempts):
        bootstrap_rc, last_detail = launchctl(["bootstrap", domain, str(target)])
        if bootstrap_rc == 0:
            print_rc, printed = launchctl(["print", service])
            loaded = _loaded_arguments(printed) if print_rc == 0 else []
            if loaded == item["expected_arguments"]:
                return {"ok": True, "label": label, "loaded_arguments": loaded,
                        "release_sha": item["release_sha"]}
        launchctl(["bootout", service])
        sleeper(retry_delays[min(attempt, len(retry_delays) - 1)])
    if old_bytes is None:
        if target.exists():
            target.unlink()
    else:
        _atomic_write(target, old_bytes)
    restored = not was_loaded
    if was_loaded and old_bytes is not None:
        old_args = list(map(str, plistlib.loads(old_bytes).get("ProgramArguments") or []))
        restore_rc, _ = launchctl(["bootstrap", domain, str(target)])
        print_rc, printed = launchctl(["print", service])
        restored = restore_rc == 0 and print_rc == 0 and _loaded_arguments(printed) == old_args
    if restored:
        raise RuntimeError(f"{label}: apply failed; restored previous job ({last_detail})")
    raise RuntimeError(f"{label}: apply failed and previous job restoration failed ({last_detail})")
