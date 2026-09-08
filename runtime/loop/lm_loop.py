#!/usr/bin/env python3
"""Read-only lm-loop commands. Lifecycle mutation is added in later slices."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from runtime.loop.macos_launchd_inventory import extract_release, parse_disabled, parse_loaded
from runtime.loop.macos_loop_registry import validate_registry
from runtime.loop.lm_loop_apply import (
    _loaded_arguments,
    _preserve_operational_attributes,
    apply_registry,
    install_one,
)
from runtime.loop.lm_loop_lifecycle import lifecycle, lifecycle_one
from runtime.loop.runtime_event import append_runtime_event, build_install_event, validate_runtime_event


ROOT = Path(__file__).resolve().parents[2]


def _next_eligible(cadence: dict) -> str:
    key, value = next(iter(cadence.items()))
    if key == "start_interval_seconds":
        return f"interval:{value}s"
    if key == "calendar_interval":
        return "calendar:" + json.dumps(value, sort_keys=True, separators=(",", ":"))
    return key.replace("_", "-")


def status_rows(registry: dict, *, loaded: dict, disabled: dict, events: dict,
                installed_releases: dict) -> list[dict]:
    validate_registry(registry)
    rows = []
    for loop_id in sorted(registry["loops"]):
        entry = registry["loops"][loop_id]
        label = entry["label"]
        runtime = loaded.get(label)
        if disabled.get(label):
            launchd_state = "disabled"
        elif runtime:
            launchd_state = "loaded-running" if runtime.get("pid") else "loaded-idle"
        else:
            launchd_state = "unloaded"
        event = events.get(loop_id) or {}
        rows.append({
            "classification": "managed",
            "owner": "life-manager",
            "desired_mode": "continuous" if "keep_alive" in entry["cadence"] else "scheduled",
            "loop_id": loop_id,
            "label": label,
            "domain": entry["domain"],
            "launchd_state": launchd_state,
            "pid": runtime.get("pid") if runtime else None,
            "last_exit": runtime.get("last_exit") if runtime else None,
            "installed_release_sha": installed_releases.get(label),
            "provider_route": entry["provider_route"],
            "provider": event.get("provider"),
            "profile_alias": event.get("profile_alias"),
            "last_pass": event.get("timestamp"),
            "last_terminal_result": event.get("status"),
            "effect_class": entry["effect_class"],
            "effect_status": event.get("effect_status", "unknown"),
            "event_release_sha": event.get("release_sha"),
            "next_eligible_run": _next_eligible(entry["cadence"]),
            "blocker": event.get("blocker"),
        })
    return rows


def resolver_rows(registry: dict, *, loaded: dict, disabled: dict, events: dict,
                  installed_releases: dict, installed_labels: set[str]) -> list[dict]:
    rows = status_rows(registry, loaded=loaded, disabled=disabled, events=events,
                       installed_releases=installed_releases)
    managed = {entry["label"] for entry in registry["loops"].values()}
    external = set(registry.get("external_labels", []))
    retired = set(registry.get("retired_labels", []))
    labels = external | retired | installed_labels | {
        label for label in loaded if label.startswith("ai.anicca.")
    }
    for label in sorted(labels - managed):
        runtime = loaded.get(label)
        classification = (
            "retired" if label in retired else
            "external" if label in external else
            "unmanaged"
        )
        if disabled.get(label):
            launchd_state = "disabled"
        elif runtime:
            launchd_state = "loaded-running" if runtime.get("pid") else "loaded-idle"
        else:
            launchd_state = "unloaded"
        present = bool(runtime or label in installed_labels)
        rows.append({
            "classification": classification,
            "owner": "external" if classification == "external" else (
                "retired" if classification == "retired" else "unknown"),
            "desired_mode": classification,
            "loop_id": label,
            "label": label,
            "domain": None,
            "launchd_state": launchd_state,
            "pid": runtime.get("pid") if runtime else None,
            "last_exit": runtime.get("last_exit") if runtime else None,
            "installed_release_sha": installed_releases.get(label),
            "provider_route": None,
            "provider": None,
            "profile_alias": None,
            "last_pass": None,
            "last_terminal_result": None,
            "effect_class": "unknown",
            "effect_status": "unknown",
            "event_release_sha": None,
            "next_eligible_run": None,
            "blocker": (
                "retired_still_present" if classification == "retired" and present else
                "unmanaged_label" if classification == "unmanaged" else None),
        })
    return sorted(rows, key=lambda row: row["label"])


def doctor_report(registry: dict, *, installed_labels: set[str], loaded_labels: set[str],
                  existing_entrypoints: set[str]) -> dict:
    validate_registry(registry)
    retired = set(registry.get("retired_labels", []))
    managed = ({entry["label"] for entry in registry["loops"].values()}
               | set(registry.get("external_labels", [])) | retired)
    unmanaged = sorted((installed_labels | loaded_labels) - managed)
    missing = sorted(
        f"{loop_id}:{entry['entrypoint']}"
        for loop_id, entry in registry["loops"].items()
        if entry["entrypoint"] not in existing_entrypoints
    )
    return {
        "ok": not unmanaged and not missing and not ((installed_labels | loaded_labels) & retired),
        "registry_entries": len(registry["loops"]),
        "unmanaged_labels": unmanaged,
        "missing_entrypoints": missing,
        "retired_installed_labels": sorted((installed_labels | loaded_labels) & retired),
    }


def _launchctl(*args: str) -> str:
    with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(mode="w+") as stderr:
        result = subprocess.run(
            ["launchctl", *args], stdout=stdout, stderr=stderr, text=True, timeout=15)
        stdout.seek(0)
        stderr.seek(0)
        output, error = stdout.read(), stderr.read()
    if result.returncode:
        raise RuntimeError(error.strip() or "launchctl failed")
    return output


def _last_event(state_root: str, loop_id: str | None = None,
                cache: dict[Path, dict[str | None, dict]] | None = None) -> dict | None:
    path = Path(os.path.expanduser(state_root)) / "events.jsonl"
    if cache is not None and path in cache:
        return cache[path].get(loop_id)
    reports: dict[str | None, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            value = json.loads(line)
            validate_runtime_event(value)
        except (json.JSONDecodeError, ValueError):
            continue
        if value.get("phase") != "report":
            continue
        reports.setdefault(None, value)
        reports.setdefault(value.get("loop_id"), value)
    if cache is not None:
        cache[path] = reports
    return reports.get(loop_id)


def _release_from_plist(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
    except Exception:
        return None
    release_sha = str((plist.get("EnvironmentVariables") or {}).get(
        "LIFE_MANAGER_RELEASE_SHA") or "")
    if re.fullmatch(r"[0-9a-f]{40}", release_sha):
        return release_sha
    args = list(map(str, plist.get("ProgramArguments") or []))
    release = extract_release(" ".join(args))
    if release:
        return release
    for arg in args:
        candidate = Path(os.path.expanduser(arg))
        try:
            if candidate.exists():
                release = extract_release(str(candidate.resolve()))
        except OSError:
            continue
        if release:
            return release
    return None


def collect_live(registry: dict, *, full_inventory: bool = True
                 ) -> tuple[dict, dict, dict, set[str], set[str]]:
    loaded = parse_loaded(_launchctl("list"))
    disabled = parse_disabled(_launchctl("print-disabled", f"gui/{os.getuid()}"))
    plist_dir = Path.home() / "Library/LaunchAgents"
    installed_paths = (list(plist_dir.glob("ai.anicca.*.plist")) if full_inventory else [
        plist_dir / f"{entry['label']}.plist" for entry in registry["loops"].values()
        if (plist_dir / f"{entry['label']}.plist").is_file()
    ])
    installed = {path.stem for path in installed_paths}
    releases, events = {}, {}
    for path in installed_paths:
        releases[path.stem] = _release_from_plist(path)
    event_cache: dict[Path, dict[str | None, dict]] = {}
    for loop_id, entry in registry["loops"].items():
        label = entry["label"]
        releases[label] = _release_from_plist(plist_dir / f"{label}.plist")
        event = _last_event(entry["state_root"], loop_id, event_cache)
        if event:
            events[loop_id] = event
    return loaded, disabled, events, releases, installed


def _select(rows: list[dict], target: str) -> list[dict]:
    if target == "all":
        return rows
    selected = [row for row in rows if row["loop_id"] == target]
    if not selected:
        raise ValueError(f"unknown loop id: {target}")
    return selected


def snapshot(registry: dict, target: str) -> list[dict]:
    if target != "all" and target in registry["loops"]:
        selected_registry = {**registry, "loops": {target: registry["loops"][target]}}
        loaded, disabled, events, releases, _ = collect_live(
            selected_registry, full_inventory=False)
        return status_rows(
            selected_registry, loaded=loaded, disabled=disabled, events=events,
            installed_releases=releases)
    loaded, disabled, events, releases, installed = collect_live(registry)
    rows = resolver_rows(
        registry, loaded=loaded, disabled=disabled, events=events,
        installed_releases=releases, installed_labels=installed)
    return _select(rows, target)


def _safe_launchctl(executable: Path, args: list[str]) -> tuple[int, str]:
    with tempfile.TemporaryFile(mode="w+") as output:
        result = subprocess.run(
            [str(executable), *args], stdout=output, stderr=output, text=True, timeout=30)
        output.seek(0)
        return result.returncode, output.read()


@contextmanager
def _apply_lock(current: Path, lock_path: Path | None):
    lock_path = Path(lock_path or current.parent / ".apply.lock").expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd, "a+") as owner_lock:
            lock_fd = -1
            try:
                fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("production apply is already owned") from exc
            yield
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)


def _label_apply_lock_path(current: Path, label: str,
                           lock_path: Path | None = None) -> Path:
    base = Path(lock_path).expanduser() if lock_path else current.parent / ".apply-locks"
    return (base / f"{label}.lock" if lock_path is None else
            base.with_name(f"{base.name}.{label}.lock"))


def _service_is_running(detail: str) -> bool:
    return bool(re.search(r"\bstate\s*=\s*running\b|\bpid\s*=\s*[1-9][0-9]*\b",
                          detail))


def _skip_if_not_loaded_idle(item: dict, release_sha: str,
                             launchctl_safe: Path) -> dict | None:
    rc, printed = _safe_launchctl(
        launchctl_safe, ["print", f"gui/{os.getuid()}/{item['label']}"])
    if rc != 0:
        return {"ok": True, "label": item["label"], "loaded": False,
                "loaded_arguments": [], "release_sha": release_sha,
                "changed": False, "skipped": "unloaded"}
    if not _service_is_running(printed):
        return None
    return {"ok": True, "label": item["label"], "loaded": True,
            "loaded_arguments": _loaded_arguments(printed),
            "release_sha": release_sha, "changed": False,
            "skipped": "loaded-running"}


def _retire_labels(registry: dict, agents_dir: Path, launchctl_safe: Path,
                   current: Path, lock_path: Path | None,
                   labels: list[str] | None = None) -> list[dict]:
    results = []
    domain = f"gui/{os.getuid()}"
    selected = labels if labels is not None else registry.get("retired_labels", [])
    for label in sorted(selected):
        with _apply_lock(current, _label_apply_lock_path(current, label, lock_path)):
            service = f"{domain}/{label}"
            present_rc, present_detail = _safe_launchctl(launchctl_safe, ["print", service])
            absent = present_rc != 0 and bool(re.search(
                r"(?i)(?:could not find service|service not found|\babsent\b)", present_detail))
            if present_rc != 0 and not absent:
                raise RuntimeError(
                    f"{label}: retirement presence readback failed: {present_detail.strip()}")
            if present_rc == 0:
                bootout_rc, detail = _safe_launchctl(launchctl_safe, ["bootout", service])
                if bootout_rc != 0:
                    raise RuntimeError(f"{label}: retirement bootout failed: {detail.strip()}")
                for attempt in range(50):
                    verify_rc, verify_detail = _safe_launchctl(
                        launchctl_safe, ["print", service])
                    if verify_rc != 0:
                        if not re.search(
                                r"(?i)(?:could not find service|service not found|\babsent\b)",
                                verify_detail):
                            raise RuntimeError(
                                f"{label}: retirement absence readback failed: "
                                f"{verify_detail.strip()}")
                        break
                    if attempt == 49:
                        raise RuntimeError(f"{label}: retirement readback still loaded")
                    time.sleep(0.1)
            plist = agents_dir / f"{label}.plist"
            removed = plist.is_file()
            if removed:
                plist.unlink()
            results.append({"ok": True, "label": label, "retired": True,
                            "was_loaded": present_rc == 0,
                            "removed_plist": removed})
    return results


def activate_current(current: Path, release_root: Path,
                     lock_path: Path | None = None) -> None:
    current = Path(current).expanduser()
    release_root = Path(release_root).expanduser()
    with _apply_lock(current, lock_path):
        release_root = release_root.resolve(strict=True)
        if not release_root.is_dir():
            raise ValueError("release root is not a directory")
        current.parent.mkdir(parents=True, exist_ok=True)
        swap = current.with_name(current.name + ".swap")
        swap.unlink(missing_ok=True)
        swap.symlink_to(release_root)
        try:
            os.replace(swap, current)
        finally:
            swap.unlink(missing_ok=True)


def apply_live(release_root: Path, agents_dir: Path, launchctl_safe: Path,
               target: str | None = None, *, current: Path | None = None,
               lock_path: Path | None = None,
               preserve_unloaded: bool = False,
               skip_busy: bool = False,
               reload_running: bool = False,
               event_writer=append_runtime_event) -> list[dict]:
    release_root = release_root.resolve()
    current = Path(current or "~/loops/current").expanduser()
    registry = json.loads((release_root / "config/loop-registry.json").read_text())
    manifest = json.loads((release_root / "RELEASE.json").read_text())
    release_sha = manifest.get("sha")
    retired_target = target if target in set(registry.get("retired_labels", [])) else None
    plan = ([] if retired_target else
            apply_registry(registry, release_root, release_sha, lambda item: item, target=target))
    preflight_rc, detail = _safe_launchctl(launchctl_safe, ["preflight"])
    if preflight_rc:
        raise RuntimeError(f"launchctl-safe preflight failed: {detail.strip()}")
    results = (
        _retire_labels(registry, agents_dir, launchctl_safe, current, lock_path)
        if target is None else
        _retire_labels(
            registry, agents_dir, launchctl_safe, current, lock_path,
            labels=[retired_target],
        ) if retired_target else []
    )
    for item in plan:
        item_lock = (None if reload_running else
                     _label_apply_lock_path(current, item["label"], lock_path))
        try:
            with _apply_lock(current, item_lock):
                if skip_busy:
                    skipped = _skip_if_not_loaded_idle(
                        item, release_sha, launchctl_safe)
                    if skipped is not None:
                        results.append(skipped)
                        continue
                target_path = agents_dir / f"{item['label']}.plist"
                result = None
                existing_bytes = target_path.read_bytes() if target_path.is_file() else None
                writer_loop_ids = {
                    "article-audit-7day", "article-daily", "article-healthcheck",
                    "article-learn-whitelist", "article-resume", "article-self-improve",
                    "article-zenn-retry", "writer-claim-loop", "writer-craft-train",
                    "writer-money-sync", "writer-opportunity-discovery",
                    "writer-opportunity-response", "writer-report", "writer-sales-measure",
                }
                writer_retired_environment_keys = (
                    ("ARTICLE_DAILY_LOG", "ARTICLE_MODEL_LOG", "GIG_LOG_DIR")
                    if item["loop_id"] in writer_loop_ids else ()
                )
                retired_environment_keys = {
                    "life-manager-cfo-hourly": ("LIFE_MANAGER_APP_DIR", "CFO_STATE_DIR"),
                    "agentmail-webhook": (
                        "AGENTMAIL_QUEUE_PATH", "AGENTMAIL_DB_PATH",
                        "AGENTMAIL_ADAPTER_STATE_DIR", "AGENTMAIL_SEMANTIC_STATE_DIR",
                    ),
                    "agentmail-replier": (
                        "AGENTMAIL_QUEUE_PATH", "AGENTMAIL_DB_PATH",
                        "AGENTMAIL_ADAPTER_STATE_DIR", "AGENTMAIL_SEMANTIC_STATE_DIR",
                    ),
                    "agentmail-nudge": (
                        "AGENTMAIL_QUEUE_PATH", "AGENTMAIL_DB_PATH",
                        "AGENTMAIL_ADAPTER_STATE_DIR", "AGENTMAIL_SEMANTIC_STATE_DIR",
                    ),
                    "agent-economy-loop": ("CEO_EFFECTIVE_CRON_DIR",),
                    # These two lanes' plists were installed while they were still rendered
                    # from skills/earn/gig/config/launchd-jobs.json's legacy manifest, which
                    # explicitly set GIG_DISK_HEADROOM_KIB="0" for them (see gig_disk_guard.py's
                    # module comment). Now that they are lm-loop registry loops, _plist() never
                    # sets this key, so _preserve_operational_attributes carries that "0" forward
                    # forever unless it is named here. Dropping it lets the safe code default
                    # (524288 KiB) take over. hf-gig-storefront-direct is deliberately excluded:
                    # its frozen value was already 524288, so retiring it has no effect and only
                    # widens the blast radius of this change.
                    "hf-gig-apply-direct": ("GIG_DISK_HEADROOM_KIB",),
                    "hf-gig-reply-detector": ("GIG_DISK_HEADROOM_KIB",),
                    "pm-decision-loop": (
                        "ANICCA_HOME", "PM_TRADE_AGENT_HOME", "PKVAR",
                        "ANICCA_EVM_PRIVATE_KEY", "BASE_CHAIN_WALLET_KEY", "BLOCKRUN_WALLET_KEY",
                        "POLYGON_WALLET_PRIVATE_KEY",
                    ),
                    "pm-live-trade": (
                        "ANICCA_HOME", "PM_TRADE_AGENT_HOME", "PKVAR",
                        "ANICCA_EVM_PRIVATE_KEY", "BASE_CHAIN_WALLET_KEY", "BLOCKRUN_WALLET_KEY",
                        "POLYGON_WALLET_PRIVATE_KEY",
                    ),
                }.get(item["loop_id"], writer_retired_environment_keys)
                retired_operational_keys = (
                    ("WorkingDirectory",)
                    if item["loop_id"] == "life-manager-cfo-hourly" else ()
                )
                desired_bytes = _preserve_operational_attributes(
                    item["plist_bytes"], existing_bytes,
                    retired_environment_keys=retired_environment_keys,
                    retired_operational_keys=retired_operational_keys)
                if existing_bytes is not None and existing_bytes == desired_bytes:
                    rc, printed = _safe_launchctl(
                        launchctl_safe, ["print", f"gui/{os.getuid()}/{item['label']}"])
                    loaded = _loaded_arguments(printed) if rc == 0 else []
                    if loaded == item["expected_arguments"]:
                        result = {"ok": True, "label": item["label"],
                                  "loaded_arguments": loaded, "release_sha": release_sha,
                                  "changed": False}
                if result is None:
                    result = install_one(
                        item, target_path, lambda args: _safe_launchctl(launchctl_safe, args),
                        preserve_unloaded=preserve_unloaded,
                        retired_environment_keys=retired_environment_keys,
                        retired_operational_keys=retired_operational_keys)
                    result["changed"] = True
                entry = registry["loops"][item["loop_id"]]
                event = build_install_event(
                    loop_id=item["loop_id"], domain=entry["domain"], release_sha=release_sha,
                    provider=entry["provider_route"], effect_class=entry["effect_class"])
                event_writer(Path(os.path.expanduser(entry["state_root"])) / "events.jsonl", event)
                result["install_event_id"] = event["event_id"]
                results.append(result)
        except RuntimeError as exc:
            if not (skip_busy and str(exc) == "production apply is already owned"):
                raise
            skipped = _skip_if_not_loaded_idle(item, release_sha, launchctl_safe)
            if skipped is None:
                raise
            results.append(skipped)
    return results


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    commands = {"apply", "doctor", "reconcile", "start", "stop", "restart", "status", "watch"}
    if not args or args[0] not in commands:
        print("usage: lm-loop apply [--all]|doctor|reconcile <provider-route> [--loaded-idle-only] [--loop-id <loop-id>]...|start|stop|restart <loop-id|all>|status|watch [<loop-id|all>]", file=sys.stderr)
        return 2
    command = args[0]
    if command == "apply":
        if args[1:] not in ([], ["--all"]):
            print(json.dumps({"ok": False, "error": "apply accepts only --all"}))
            return 2
        target = os.environ.get("LIFE_MANAGER_APPLY_TARGET")
        if not target and args[1:] != ["--all"]:
            print(json.dumps({
                "ok": False,
                "error": "apply requires LIFE_MANAGER_APPLY_TARGET; use --all only for an intentional fleet-wide reload",
            }, sort_keys=True))
            return 2
        if target and args[1:] == ["--all"]:
            print(json.dumps({"ok": False, "error": "--all conflicts with LIFE_MANAGER_APPLY_TARGET"}))
            return 2
        release_root = Path(os.environ.get("LIFE_MANAGER_RELEASE_ROOT", "~/loops/current")).expanduser()
        agents_dir = Path(os.environ.get(
            "LIFE_MANAGER_LAUNCH_AGENTS_DIR", "~/Library/LaunchAgents")).expanduser()
        launchctl_safe = Path(os.environ.get(
            "LIFE_MANAGER_LAUNCHCTL_SAFE", str(release_root / "bin/launchctl-safe"))).expanduser()
        try:
            results = apply_live(
                release_root, agents_dir, launchctl_safe,
                target=target)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            return 1
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0
    registry = validate_registry(json.loads((ROOT / "config/loop-registry.json").read_text()))
    if command == "reconcile":
        positionals, loop_ids, loaded_idle_only, include_running = [], [], False, False
        reconcile_args = args[1:]
        index = 0
        while index < len(reconcile_args):
            value = reconcile_args[index]
            if value == "--loaded-idle-only":
                loaded_idle_only = True
            elif value == "--include-running":
                include_running = True
            elif value == "--loop-id":
                if index + 1 >= len(reconcile_args) or reconcile_args[index + 1].startswith("--"):
                    print(json.dumps({"ok": False, "error": "--loop-id requires a value"}))
                    return 2
                loop_ids.append(reconcile_args[index + 1])
                index += 1
            elif value.startswith("--loop-id="):
                loop_id = value.split("=", 1)[1]
                if not loop_id:
                    print(json.dumps({"ok": False, "error": "--loop-id requires a value"}))
                    return 2
                loop_ids.append(loop_id)
            elif value.startswith("--"):
                print(json.dumps({"ok": False, "error": f"unknown reconcile option: {value}"}))
                return 2
            else:
                positionals.append(value)
            index += 1
        if len(positionals) != 1:
            print(json.dumps({"ok": False, "error": "reconcile requires <provider-route>"}))
            return 2
        route = positionals[0]
        requested_ids = set(loop_ids)
        if (route == "deterministic"
                and os.environ.get("LIFE_MANAGER_LOOP_ID") == "life-manager-release-reconciler"):
            requested_ids.add("life-manager-disk-cleanup")
        if include_running and not requested_ids:
            print(json.dumps({"ok": False,
                              "error": "--include-running requires --loop-id"}))
            return 2
        if include_running and loaded_idle_only:
            print(json.dumps({"ok": False,
                              "error": "--include-running conflicts with --loaded-idle-only"}))
            return 2
        for loop_id in loop_ids:
            entry = registry["loops"].get(loop_id)
            if not isinstance(entry, dict):
                print(json.dumps({"ok": False, "error": f"unknown loop id: {loop_id}"}))
                return 2
            if entry["provider_route"] != route:
                print(json.dumps({"ok": False,
                                  "error": f"loop id {loop_id} is not on provider route {route}"}))
                return 2
        release_root = Path(os.environ.get("LIFE_MANAGER_RELEASE_ROOT", ROOT)).expanduser().resolve(strict=True)
        current_sha = json.loads((release_root / "RELEASE.json").read_text()).get("sha")
        rows = snapshot(registry, "all")
        automatic_release_reconciler = (
            os.environ.get("LIFE_MANAGER_LOOP_ID") == "life-manager-release-reconciler"
        )
        explicitly_reloadable = {
            loop_id for loop_id in requested_ids
            if registry["loops"][loop_id].get("cadence", {}).get("keep_alive") is True
        }
        eligible_states = ({"loaded-idle", "loaded-running"} if include_running else
                           {"loaded-idle", "loaded-running"} if explicitly_reloadable else
                           {"loaded-idle"} if loaded_idle_only else
                           {"loaded-idle", "unloaded"})
        eligible = [row for row in rows if (
            row["classification"] == "managed"
            and row["loop_id"] != os.environ.get("LIFE_MANAGER_LOOP_ID")
            and row["provider_route"] == route
            and (not requested_ids or row["loop_id"] in requested_ids)
            and row["launchd_state"] in eligible_states
            and (row["launchd_state"] != "loaded-running"
                 or include_running
                 or row["loop_id"] in explicitly_reloadable)
            and row["installed_release_sha"]
            and row["installed_release_sha"] != current_sha
            and (not automatic_release_reconciler
                 or row["loop_id"] in explicitly_reloadable
                 or row.get("event_release_sha") == row["installed_release_sha"])
        )]
        applied, failed = [], []
        for row in eligible:
            try:
                applied.extend(apply_live(
                    release_root, Path("~/Library/LaunchAgents").expanduser(),
                    release_root / "bin/launchctl-safe",
                    target=row["loop_id"],
                    preserve_unloaded=row["launchd_state"] == "unloaded",
                    skip_busy=(loaded_idle_only and
                               row["loop_id"] not in explicitly_reloadable),
                    reload_running=row["launchd_state"] == "loaded-running"))
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                failed.append({"loop_id": row["loop_id"], "error": str(exc)})
        print(json.dumps({
            "ok": not failed, "route": route, "release_sha": current_sha,
            "eligible": len(eligible), "applied": applied, "failed": failed,
            "skipped_running": [row["loop_id"] for row in rows if (
                row["classification"] == "managed"
                and row["provider_route"] == route
                and row["launchd_state"] == "loaded-running"
                and row["installed_release_sha"] != current_sha)],
        }, indent=2, sort_keys=True))
        return 1 if failed else 0
    if command in {"start", "stop", "restart"}:
        if len(args) != 2:
            print(json.dumps({"ok": False, "error": f"{command} requires <loop-id|all>"}))
            return 2
        target = args[1]
        if target != "all" and target not in registry["loops"]:
            print(json.dumps({"ok": False, "error": f"unknown loop id: {target}"}))
            return 2
        agents_dir = Path(os.environ.get(
            "LIFE_MANAGER_LAUNCH_AGENTS_DIR", "~/Library/LaunchAgents")).expanduser()
        launchctl_safe = Path(os.environ.get(
            "LIFE_MANAGER_LAUNCHCTL_SAFE", str(ROOT / "bin/launchctl-safe"))).expanduser()
        preflight_rc, detail = _safe_launchctl(launchctl_safe, ["preflight"])
        if preflight_rc:
            print(json.dumps({"ok": False, "error": detail.strip()}, sort_keys=True))
            return 1
        results = lifecycle(
            registry, command, target,
            lambda action, loop_id, entry: lifecycle_one(
                action, loop_id, entry, agents_dir,
                lambda launch_args: _safe_launchctl(launchctl_safe, launch_args)))
        print(json.dumps(results, indent=2, sort_keys=True))
        return 1 if any(row["return_code"] for row in results) else 0
    target = args[1] if len(args) > 1 else "all"
    if command == "doctor":
        loaded, _, _, _, installed = collect_live(registry)
        existing = {entry["entrypoint"] for entry in registry["loops"].values()
                    if (ROOT / entry["entrypoint"]).is_file()}
        report = doctor_report(registry, installed_labels=installed,
                               loaded_labels=set(loaded), existing_entrypoints=existing)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    while True:
        print(json.dumps(snapshot(registry, target), indent=2, sort_keys=True), flush=True)
        if command == "status" or os.environ.get("LM_LOOP_WATCH_ONCE") == "1":
            return 0
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
