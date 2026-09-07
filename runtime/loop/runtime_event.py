"""Uniform secret-free runtime event envelope and durable JSONL append."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


FIELDS = {
    "version", "event_id", "timestamp", "loop_id", "domain", "run_id", "phase",
    "status", "release_sha", "provider", "profile_alias", "effect_class",
    "effect_status", "blocker", "evidence_refs",
}
DOMAINS = {"physical", "mental", "financial", "earn", "growth", "system"}
PHASES = {"plan", "execute", "reconcile", "verify", "report"}
STATUSES = {"running", "pass", "fail", "blocked"}
EFFECTS = {"none", "publish", "message", "money", "application", "trade", "account_mutation"}
EFFECT_STATUSES = {"not_applicable", "unknown", "planned", "started", "verified", "failed", "reconciled"}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SAFE_REF = re.compile(r"[a-z][a-z0-9+.-]*://[A-Za-z0-9._:/-]{1,512}\Z")
SECRET = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]+|(?:token|secret|password|credential|api.?key|auth\.json)\s*[=:]|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|/" r"Users/)"
)
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


def _max_bytes() -> int:
    try:
        value = int(os.environ.get("LM_RUNTIME_EVENTS_MAX_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def _next_archive(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.stem}-{stamp}{path.suffix}.gz")
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = path.with_name(f"{path.stem}-{stamp}.{suffix}{path.suffix}.gz")
    return candidate


def rotate_jsonl_locked(
    fd: int,
    path: Path,
    max_bytes: int | None = None,
    keep_archives: int | None = None,
) -> None:
    if keep_archives is not None and keep_archives < 0:
        raise ValueError("keep_archives must be non-negative")
    if os.fstat(fd).st_size <= (max_bytes or _max_bytes()):
        return
    archive = _next_archive(path)
    temporary = archive.with_name(f".{archive.name}.tmp.{os.getpid()}")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as source, temporary.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as target:
                shutil.copyfileobj(source, target)
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, archive)
        os.ftruncate(fd, 0)
        os.fsync(fd)
        if keep_archives is not None:
            archives = sorted(path.parent.glob(f"{path.stem}-*{path.suffix}.gz"))
            expired_archives = archives[:-keep_archives] if keep_archives else archives
            for expired in expired_archives:
                expired.unlink()
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_runtime_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    missing, unknown = FIELDS - set(event), set(event) - FIELDS
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    if SECRET.search(json.dumps(event, ensure_ascii=False, sort_keys=True)):
        raise ValueError("secret-like event value forbidden")
    if event["version"] != 1 or not re.fullmatch(r"[0-9a-f]{24,64}", event["event_id"]):
        raise ValueError("invalid version or event_id")
    try:
        datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid timestamp") from exc
    for key in ("loop_id", "run_id", "provider"):
        if not isinstance(event[key], str) or not SAFE_ID.fullmatch(event[key]):
            raise ValueError(f"invalid {key}")
    alias = event["profile_alias"]
    if alias is not None and (not isinstance(alias, str) or not SAFE_ID.fullmatch(alias)):
        raise ValueError("invalid profile_alias")
    if event["domain"] not in DOMAINS or event["phase"] not in PHASES:
        raise ValueError("invalid domain or phase")
    if event["status"] not in STATUSES or event["effect_class"] not in EFFECTS:
        raise ValueError("invalid status or effect_class")
    if event["effect_status"] not in EFFECT_STATUSES:
        raise ValueError("invalid effect_status")
    blocker = event["blocker"]
    if blocker is not None and (not isinstance(blocker, str) or not SAFE_ID.fullmatch(blocker)):
        raise ValueError("invalid blocker")
    refs = event["evidence_refs"]
    if not isinstance(refs, list) or len(refs) > 32 or any(
        not isinstance(ref, str) or not SAFE_REF.fullmatch(ref) for ref in refs
    ):
        raise ValueError("invalid evidence_refs")
    return event


def build_runtime_event(*, loop_id: str, domain: str, run_id: str, release_sha: str,
                        provider: str, profile_alias: str | None, effect_class: str,
                        succeeded: bool, blocker: str | None,
                        deferred: bool = False,
                        evidence_scheme: str = "agent-runner") -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    if succeeded and deferred:
        raise ValueError("runtime event cannot be both succeeded and deferred")
    status = "blocked" if deferred else ("pass" if succeeded else "fail")
    material = f"{release_sha}:{loop_id}:{run_id}:report:{status}"
    event = {
        "version": 1,
        "event_id": hashlib.sha256(material.encode()).hexdigest()[:24],
        "timestamp": timestamp,
        "loop_id": loop_id,
        "domain": domain,
        "run_id": run_id,
        "phase": "report",
        "status": status,
        "release_sha": release_sha,
        "provider": provider,
        "profile_alias": profile_alias,
        "effect_class": effect_class,
        "effect_status": "not_applicable" if effect_class == "none" else "unknown",
        "blocker": blocker,
        "evidence_refs": [f"{evidence_scheme}://{loop_id}/{run_id}/summary.json"],
    }
    return validate_runtime_event(event)


def build_runtime_start_event(*, loop_id: str, domain: str, run_id: str,
                              release_sha: str, provider: str,
                              profile_alias: str | None, effect_class: str) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    material = f"{release_sha}:{loop_id}:{run_id}:execute:running"
    return validate_runtime_event({
        "version": 1,
        "event_id": hashlib.sha256(material.encode()).hexdigest()[:24],
        "timestamp": timestamp,
        "loop_id": loop_id,
        "domain": domain,
        "run_id": run_id,
        "phase": "execute",
        "status": "running",
        "release_sha": release_sha,
        "provider": provider,
        "profile_alias": profile_alias,
        "effect_class": effect_class,
        "effect_status": "not_applicable" if effect_class == "none" else "started",
        "blocker": None,
        "evidence_refs": [f"lm-loop://{loop_id}/{run_id}/summary.json"],
    })


def build_install_event(*, loop_id: str, domain: str, release_sha: str,
                        provider: str, effect_class: str) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    material = f"{release_sha}:{loop_id}:install:plan:pass"
    return validate_runtime_event({
        "version": 1,
        "event_id": hashlib.sha256(material.encode()).hexdigest()[:24],
        "timestamp": timestamp,
        "loop_id": loop_id,
        "domain": domain,
        "run_id": "install",
        "phase": "plan",
        "status": "pass",
        "release_sha": release_sha,
        "provider": provider,
        "profile_alias": None,
        "effect_class": effect_class,
        "effect_status": "not_applicable" if effect_class == "none" else "unknown",
        "blocker": None,
        "evidence_refs": [f"lm-loop://{loop_id}/install/summary.json"],
    })


def append_runtime_event(path: Path, event: dict) -> None:
    validate_runtime_event(event)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        rotate_jsonl_locked(fd, path)
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="replace") as reader:
            for line in reader:
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(existing, dict) and existing.get("event_id") == event["event_id"]:
                    return
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
