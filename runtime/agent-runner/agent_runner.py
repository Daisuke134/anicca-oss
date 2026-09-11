#!/usr/bin/env python3
"""Bounded provider-agnostic agent runner with durable per-attempt evidence."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime.loop.macos_loop_registry import validate_registry  # noqa: E402
from runtime.loop.runtime_event import (  # noqa: E402
    append_runtime_event,
    build_runtime_event,
    rotate_jsonl_locked,
)
from token_budget import TokenBudgetLedger, budget_day_for  # noqa: E402
# Every effort above medium costs the same order of money as Sol does, so all of them take the
# explicit escalation route. Naming only "high" here let "xhigh" and "max" past the gate.
RESTRICTED_EFFORTS = frozenset(("high", "xhigh", "max"))
JSON_FENCE = re.compile(r"\A```json\r?\n(?P<body>.*?)\r?\n```\Z", re.DOTALL)
DEFAULT_USAGE_LEDGER = Path.home() / ".local" / "state" / "life-manager" / "telemetry" / "agent-usage.jsonl"
DEFAULT_USAGE_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_USAGE_ARCHIVES = 3
CLAUDE_PROVIDERS = {"claude", "claude-direct"}
CODEX_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(("uniqueItems", "allOf", "if", "then", "else"))
# Smallest prompt that could plausibly express a bounded task. Kept low on
# purpose: this is a floor against empty/degenerate transport, not a style
# rule. Callers that legitimately want a stricter floor set
# AGENT_RUNNER_MIN_PROMPT_CHARS (run_agent.sh does, for loop prompts).
MIN_PROMPT_CHARS = 16
DEFAULT_HISTORY_GENERATIONS = 3
# Evidence is useful only while it is recent and inspectable.  Letting each
# provider stream indefinitely into a permanent per-run directory eventually
# turns a recoverable disk-pressure incident into a failed paid invocation.
# Keep a bounded history, and preferentially evict only completed runs.
DEFAULT_EVIDENCE_MIN_FREE_BYTES = 512 * 1024 * 1024
DEFAULT_EVIDENCE_MAX_BYTES = 256 * 1024 * 1024
PROVIDER_LEASE_BUSY = 75
PROVIDER_LEASE_BUSY_LINE = "LIFE_MANAGER_PROVIDER_LEASE_BUSY"
CODEX_INVOCATION_HOME_MARKER = "_LIFE_MANAGER_CODEX_INVOCATION_HOME"
_OWNED_CODEX_INVOCATION_HOMES: set[Path] = set()

# OpenAI Standard tier, short context, USD per 1M tokens: (input, cached_input, output).
# Source: https://developers.openai.com/api/docs/pricing (fetched 2026-07-25).
CODEX_MTOK_PRICING_USD = {
    "gpt-5.6-luna": (1.00, 0.10, 6.00),
    "gpt-5.6-terra": (2.50, 0.25, 15.00),
    "gpt-5.6-sol": (5.00, 0.50, 30.00),
}
TOOLLESS_TASK_CLASSES = (
    "composition-agent", "diagnostic-agent", "application-intent-planner",
    "reply-semantic-agent", "storefront-proposal-agent",
)
TOOLLESS_CODEX_DISABLED_FEATURES = ("shell_tool", "code_mode_host", "unified_exec")


def runtime_event_loop_id(requested_loop_id: str) -> str:
    """Bind nested agent evidence to its managed parent loop when available."""
    return os.environ.get("LIFE_MANAGER_LOOP_ID", "").strip() or requested_loop_id


def emit_runtime_event(*, loop_id: str, evidence_dir: Path,
                       selected: dict[str, Any] | None, attempts: list[dict[str, Any]],
                       candidate_profile: str | None, registry_path: Path,
                       release_sha: str) -> dict[str, Any]:
    registry = validate_registry(json.loads(registry_path.read_text(encoding="utf-8")))
    entry = registry["loops"].get(loop_id)
    if not isinstance(entry, dict):
        raise ValueError(f"managed runtime event loop is absent from registry: {loop_id}")
    last = selected or (attempts[-1] if attempts else {})
    provider = str(last.get("provider") or "unavailable")
    blocker = None if selected else str(last.get("error_class") or "runner_failed")
    run_id = hashlib.sha256(str(evidence_dir.resolve()).encode()).hexdigest()[:24]
    event = build_runtime_event(
        loop_id=loop_id,
        domain=entry["domain"],
        run_id=run_id,
        release_sha=release_sha,
        provider=provider,
        profile_alias=candidate_profile,
        effect_class=entry["effect_class"],
        succeeded=selected is not None,
        blocker=blocker,
    )
    path = Path(os.path.expanduser(entry["state_root"])) / "events.jsonl"
    append_runtime_event(path, event)
    return event


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def evidence_root_for(evidence_dir: Path) -> Path | None:
    """Return the managed evidence root for a task/run evidence directory.

    The runner is also used by callers that supply arbitrary paths.  Retention
    must never recursively delete outside the explicitly named
    ``agent-runner-evidence/<task>/<run>`` layout.
    """
    resolved = evidence_dir.resolve()
    parts = resolved.parts
    try:
        marker = parts.index("agent-runner-evidence")
    except ValueError:
        return None
    if len(parts) < marker + 3:
        return None
    return Path(*parts[:marker + 1])


def tree_size(path: Path) -> int:
    """Return file bytes below path without following symlinks."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [item for item in dirs if not (Path(root) / item).is_symlink()]
        for name in files:
            item = Path(root) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    return total


def prune_history_generations(history: Path, *, keep: int = DEFAULT_HISTORY_GENERATIONS) -> dict[str, int]:
    """Bound rotated runner output without touching ledgers or the active run."""
    result = {"removed": 0, "bytes_reclaimed": 0, "errors": 0}
    try:
        generations = sorted(path for path in history.iterdir()
                             if path.is_dir() and not path.is_symlink()
                             and ".gc-trash." not in path.name)
    except OSError:
        result["errors"] += 1
        return result
    for generation in generations[:-max(0, keep)] if keep > 0 else generations:
        reclaimed = tree_size(generation)
        trash = generation.with_name(f"{generation.name}.gc-trash.{os.getpid()}")
        try:
            os.replace(generation, trash)
            shutil.rmtree(trash)
        except OSError:
            result["errors"] += 1
            continue
        result["removed"] += 1
        result["bytes_reclaimed"] += reclaimed
    return result


def reclaim_completed_evidence(
    evidence_dir: Path,
    *,
    min_free_bytes: int = DEFAULT_EVIDENCE_MIN_FREE_BYTES,
    max_evidence_bytes: int = DEFAULT_EVIDENCE_MAX_BYTES,
) -> dict[str, int]:
    """Reclaim oldest completed managed evidence before starting a provider.

    Never remove the current invocation, a task directory, symlinks, or an
    invocation lacking ``summary.json`` (it may still be running).  A caller
    can set either threshold to zero for installations with their own disk
    governor.
    """
    root = evidence_root_for(evidence_dir)
    if root is None or not root.is_dir():
        return {"reclaimed_bytes": 0, "reclaimed_runs": 0}
    current = evidence_dir.resolve()
    candidates: list[tuple[float, Path, int]] = []
    total = 0
    for task_dir in root.iterdir():
        if not task_dir.is_dir() or task_dir.is_symlink():
            continue
        for run_dir in task_dir.iterdir():
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            size = tree_size(run_dir)
            total += size
            if run_dir.resolve() == current or not (run_dir / "summary.json").is_file():
                continue
            try:
                candidates.append((run_dir.stat().st_mtime, run_dir, size))
            except OSError:
                continue
    reclaimed_bytes = 0
    reclaimed_runs = 0
    free = shutil.disk_usage(root).free
    for _, run_dir, size in sorted(candidates, key=lambda item: item[0]):
        if total <= max_evidence_bytes and free >= min_free_bytes:
            break
        try:
            shutil.rmtree(run_dir)
        except OSError:
            continue
        total -= size
        reclaimed_bytes += size
        reclaimed_runs += 1
        free = shutil.disk_usage(root).free
    return {"reclaimed_bytes": reclaimed_bytes, "reclaimed_runs": reclaimed_runs}


def ensure_evidence_capacity(evidence_dir: Path) -> dict[str, int]:
    """Apply managed evidence retention and fail before a paid provider on ENOSPC."""
    min_free = int(os.environ.get("AGENT_RUNNER_EVIDENCE_MIN_FREE_BYTES", DEFAULT_EVIDENCE_MIN_FREE_BYTES))
    max_bytes = int(os.environ.get("AGENT_RUNNER_EVIDENCE_MAX_BYTES", DEFAULT_EVIDENCE_MAX_BYTES))
    if min_free < 0 or max_bytes < 0:
        raise ValueError("agent-runner evidence thresholds must be non-negative")
    result = reclaim_completed_evidence(
        evidence_dir, min_free_bytes=min_free, max_evidence_bytes=max_bytes,
    )
    root = evidence_root_for(evidence_dir)
    if root is not None and root.exists() and shutil.disk_usage(root).free < min_free:
        raise OSError(
            errno.ENOSPC,
            "insufficient free space after completed agent evidence reclamation",
            str(root),
        )
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def codex_output_schema(schema: Any, result_path: Path) -> Path:
    """Write the provider subset without weakening deterministic validation."""

    def compatible(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: compatible(item)
                for key, item in value.items()
                if key not in CODEX_UNSUPPORTED_SCHEMA_KEYWORDS
            }
        if isinstance(value, list):
            return [compatible(item) for item in value]
        return value

    prefix = result_path.name.removesuffix(".result.json")
    path = result_path.with_name(f"{prefix}.codex-output-schema.json")
    provider_schema = compatible(schema)
    if provider_schema == {}:
        provider_schema = {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        }
    atomic_json(path, provider_schema)
    return path


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _empty_usage() -> dict[str, Any]:
    return {
        "measurement": "unavailable",
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_creation_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
        "provider_cost_usd": None,
        "cost_basis": "unavailable",
        "upstream_provider": None,
        "upstream_model": None,
    }


def extract_provider_usage(provider: str, stdout_text: str, model: str | None = None) -> dict[str, Any]:
    """Normalize provider-reported usage; never invent missing token counts."""
    usage = _empty_usage()
    try:
        if provider == "codex":
            completed = None
            for line in stdout_text.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.completed":
                    completed = event.get("usage")
            if not isinstance(completed, dict):
                return usage
            input_tokens = _token(completed.get("input_tokens"))
            output_tokens = _token(completed.get("output_tokens"))
            if input_tokens is None or output_tokens is None:
                return usage
            usage.update({
                "measurement": "provider_reported",
                "input_tokens": input_tokens,
                "cached_input_tokens": _token(completed.get("cached_input_tokens")) or 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": _token(completed.get("reasoning_output_tokens")) or 0,
                "total_tokens": input_tokens + output_tokens,
            })
            pricing = CODEX_MTOK_PRICING_USD.get(str(model or ""))
            if pricing is not None:
                cached = usage["cached_input_tokens"]
                uncached = max(0, input_tokens - cached)
                input_rate, cached_rate, output_rate = pricing
                usage["provider_cost_usd"] = (
                    uncached * input_rate + cached * cached_rate + output_tokens * output_rate
                ) / 1_000_000
                # Codex runs on subscription; this is the API-price equivalent,
                # the same cost basis the Claude CLI branch reports below.
                usage["cost_basis"] = "api_equivalent_estimate"
            return usage
        wrapper = json.loads(stdout_text)
        if not isinstance(wrapper, dict):
            return usage
        if provider in CLAUDE_PROVIDERS:
            model_usage = wrapper.get("modelUsage")
            if isinstance(model_usage, dict) and len(model_usage) == 1:
                usage["upstream_model"] = next(iter(model_usage))
            raw = wrapper.get("usage")
            if not isinstance(raw, dict):
                return usage
            direct = _token(raw.get("input_tokens"))
            output_tokens = _token(raw.get("output_tokens"))
            if direct is None or output_tokens is None:
                return usage
            cache_create = _token(raw.get("cache_creation_input_tokens")) or 0
            cache_read = _token(raw.get("cache_read_input_tokens")) or 0
            cost = wrapper.get("total_cost_usd")
            usage.update({
                "measurement": "provider_reported",
                "input_tokens": direct + cache_create + cache_read,
                "cached_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": _token(raw.get("reasoning_output_tokens")) or 0,
                "total_tokens": direct + cache_create + cache_read + output_tokens,
                "provider_cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                # Claude CLI exposes an API-price equivalent even when OAuth/subscription is
                # paying the actual bill. It is useful telemetry, but not actual marginal cost.
                "cost_basis": "api_equivalent_estimate" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "unavailable",
            })
            return usage
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return usage
    return usage


def budget_charge_tokens(provider: str, usage: dict[str, Any], reservation_tokens: int) -> int:
    if usage.get("measurement") != "provider_reported":
        return reservation_tokens
    total = _token(usage.get("total_tokens"))
    if total is None or total <= 0:
        return reservation_tokens
    if provider != "codex":
        return total
    cached = _token(usage.get("cached_input_tokens"))
    if cached is None or cached > total:
        return reservation_tokens
    return total - cached


def claude_json_document(payload: str) -> str:
    """Return the single JSON document inside a Claude answer, or the answer unchanged.

    Callers json.loads() the result file, so anything the model puts around the object is a
    parse error that reads as a runner failure even though the answer was correct. Measured
    2026-08-31 on the Lancers planner: the model reasoned in prose, wrote "JSON出す。", then
    emitted the object, and json.loads reported `Extra data: line 1 column 3`.

    Only a whole document is accepted. If what is found does not span to the end of the
    remaining text, the original string is returned untouched rather than silently keeping the
    first of several objects -- a truncated or multi-answer reply must fail loudly upstream.
    """
    text = payload.strip()
    fenced = JSON_FENCE.fullmatch(text)
    if fenced and "```" not in fenced.group("body"):
        text = fenced.group("body").strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return payload
    try:
        _, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return payload
    # A closing fence may trail the object when the model wrote prose before opening the fence,
    # so the whole string was never a fence match. Backticks are not a second answer.
    if text[end:].strip().strip("`").strip():
        return payload
    return text[start:end]


def extract_claude_payload(stdout_path: Path, result_path: Path) -> str:
    """Unwrap Claude's JSON output envelope while tolerating legacy plain output."""
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    try:
        wrapper = json.loads(text)
    except json.JSONDecodeError:
        result_path.write_text(text, encoding="utf-8")
        return ""
    if isinstance(wrapper, dict) and wrapper.get("type") == "result" and "result" in wrapper:
        payload = wrapper["result"]
        if isinstance(payload, str):
            payload = claude_json_document(payload)
        result_path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        result_path.write_text(text, encoding="utf-8")
    return ""


def append_usage_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        os.fchmod(handle.fileno(), 0o600)
        rotate_jsonl_locked(
            handle.fileno(), path, DEFAULT_USAGE_MAX_BYTES, DEFAULT_USAGE_ARCHIVES
        )
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProviderLeaseBusy(RuntimeError):
    pass


def acquire_provider_lease(path_value: str) -> int | None:
    """Acquire the optional provider-owned lock and return its open descriptor."""
    if not path_value:
        return None
    descriptor = os.open(Path(path_value).expanduser(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("provider lease path must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise ProviderLeaseBusy() from error
            raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a timed-out provider and every child in its process group."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _strip_browser_routes_for_planner(child_env: dict[str, str]) -> dict[str, str]:
    forbidden_names = ("BROWSER", "CDP", "WEBSOCKET", "PLAYWRIGHT", "PUPPETEER")
    loopback_values = ("localhost", "127.0.0.1", "[::1]", "//::1")
    return {
        name: value
        for name, value in child_env.items()
        if not any(token in name.upper() for token in forbidden_names)
        and not any(token in value.lower() for token in loopback_values)
    }


def provider_process_env(provider: str, provider_config: dict[str, Any],
                         environ: dict[str, str] | None = None, *,
                         task_class: str | None = None,
                         invocation_id: str | None = None) -> dict[str, str]:
    """Build a provider-scoped, non-interactive child environment."""
    child_env = dict(os.environ if environ is None else environ)
    child_env.pop(CODEX_INVOCATION_HOME_MARKER, None)
    if provider != "codex":
        child_env.pop("CODEX_HOME", None)
    if task_class == "application-intent-planner":
        child_env = _strip_browser_routes_for_planner(child_env)
    if provider == "codex":
        automation_home_value = provider_config.get("automation_home")
        if not automation_home_value:
            return child_env
        if invocation_id is not None and (
            not isinstance(invocation_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]+", invocation_id) is None
        ):
            raise ValueError("codex invocation_id must be a safe path component")
        automation_home = Path(os.path.expandvars(os.path.expanduser(
            str(automation_home_value)
        )))
        automation_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        automation_home.chmod(0o700)
        codex_home = automation_home
        if invocation_id is not None:
            invocations_home = automation_home / "invocations"
            try:
                invocations_home.mkdir(exist_ok=True, mode=0o700)
            except FileExistsError as error:
                raise ValueError("codex invocation parent must be a real directory") from error
            if invocations_home.is_symlink() or not invocations_home.is_dir():
                raise ValueError("codex invocation parent must be a real directory")
            invocations_home.chmod(0o700)
            codex_home = invocations_home / invocation_id
            try:
                codex_home.mkdir(exist_ok=False, mode=0o700)
            except FileExistsError as error:
                raise ValueError("codex invocation_id home already exists") from error
        else:
            codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        codex_home.chmod(0o700)
        child_env["CODEX_HOME"] = str(codex_home)
        automation_user_home = codex_home / "user-home"
        automation_user_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        automation_user_home.chmod(0o700)
        child_env["HOME"] = str(automation_user_home)

        ssl_cert_file_value = provider_config.get("ssl_cert_file")
        if ssl_cert_file_value:
            ssl_cert_file = Path(os.path.expandvars(os.path.expanduser(
                str(ssl_cert_file_value)
            )))
            if not ssl_cert_file.is_file():
                raise ValueError("codex TLS certificate bundle unavailable")
            child_env["SSL_CERT_FILE"] = str(ssl_cert_file)

        model_providers = provider_config.get("model_providers", {})
        if isinstance(model_providers, dict):
            for model_provider in model_providers.values():
                if not isinstance(model_provider, dict):
                    continue
                env_key = model_provider.get("env_key")
                if not isinstance(env_key, str) or not env_key or child_env.get(env_key):
                    continue
                token_file = os.path.expandvars(os.path.expanduser(str(
                    model_provider.get("auth_token_file", "~/.cli-proxy-api-key")
                )))
                try:
                    auth_token = Path(token_file).read_text(encoding="utf-8").strip()
                except OSError as error:
                    raise ValueError("codex model provider auth unavailable") from error
                if not auth_token:
                    raise ValueError("codex model provider auth unavailable")
                child_env[env_key] = auth_token
        auth_file = Path(os.path.expandvars(os.path.expanduser(str(
            provider_config.get("auth_file", "~/.codex/auth.json")
        ))))
        if not auth_file.is_file():
            raise ValueError("codex automation auth unavailable")
        auth_source = auth_file.resolve()
        auth_target = codex_home / "auth.json"
        try:
            auth_target.symlink_to(auth_source)
        except FileExistsError:
            try:
                if auth_target.resolve(strict=True) != auth_source:
                    raise ValueError("codex automation auth target mismatch")
            except OSError as error:
                raise ValueError("codex automation auth target invalid") from error
        if invocation_id is not None:
            owned_home = codex_home.resolve()
            _OWNED_CODEX_INVOCATION_HOMES.add(owned_home)
            child_env[CODEX_INVOCATION_HOME_MARKER] = str(owned_home)
        return child_env
    if provider not in CLAUDE_PROVIDERS:
        return child_env
    # launchd jobs run with a minimal environment that omits USER; the claude
    # CLI reads its stored OAuth credentials only when USER is set, so every
    # loop's claude call fails "Not logged in" without this even though the
    # account is fully logged in (measured 2026-09-04).
    if not child_env.get("USER"):
        child_env["USER"] = pwd.getpwuid(os.getuid()).pw_name
    if provider == "claude-direct":
        for name in (
            "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            child_env.pop(name, None)
        return child_env
    if any(child_env.get(name) for name in (
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    )):
        return child_env

    token_file = os.path.expandvars(os.path.expanduser(str(
        provider_config.get("auth_token_file", "~/.cli-proxy-api-key")
    )))
    try:
        auth_token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError("claude headless auth unavailable") from error
    if not auth_token:
        raise ValueError("claude headless auth unavailable")
    child_env["ANTHROPIC_BASE_URL"] = str(
        provider_config.get("base_url", "http://127.0.0.1:8317")
    )
    child_env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    if task_class == "application-intent-planner":
        child_env = _strip_browser_routes_for_planner(child_env)
    return child_env


def resolve_provider_profiles(
    candidates: list[dict[str, Any]], providers: dict[str, Any]
) -> list[dict[str, Any]]:
    """Resolve explicit profiles and expand the configured Codex account route."""
    resolved: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("provider") != "codex":
            resolved.append(dict(candidate))
            continue
        alias = candidate.get("profile_alias")
        if not isinstance(alias, str) or not alias:
            raise ValueError("codex candidate requires explicit profile_alias")
        codex = providers.get("codex", {})
        profiles = codex.get("profiles", {})
        order = codex.get("account_profile_order") or [alias]
        if not isinstance(order, list) or not order or not all(
            isinstance(value, str) and value for value in order
        ):
            raise ValueError("codex account_profile_order is invalid")
        if alias not in order:
            raise ValueError(f"codex profile_alias is not in account_profile_order: {alias}")
        order = order[order.index(alias):]
        for position, profile_alias in enumerate(order):
            profile = profiles.get(profile_alias)
            if not isinstance(profile, dict):
                raise ValueError(f"codex profile_alias is not configured: {profile_alias}")
            scoped = dict(candidate)
            scoped.update({
                "profile_alias": profile_alias,
                "automation_home": profile.get("automation_home"),
                "auth_file": profile.get("auth_file"),
                "account_fallback_next": position < len(order) - 1,
            })
            if not scoped["automation_home"] or not scoped["auth_file"]:
                raise ValueError(f"codex profile is incomplete: {profile_alias}")
            resolved.append(scoped)
    return resolved


def codex_attempt_started_work(stdout: str) -> bool:
    """Treat any non-message Codex item as effect-uncertain tool work."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {"item.started", "item.completed"}:
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") not in {"agent_message", "error"}:
            return True
    return False


# The live provider Popen, if any. start_new_session detaches the provider into its
# own session, so a signal aimed at the RUNNER's process group never reaches it — when
# the runner is killed externally the provider survives as an orphan (2026-07-27: an
# orphaned codex child held the gig browser lock for 12 minutes after tier1-remediate
# SIGTERMed the worker; the next pass died with deferred_cdp_busy, exit 75). The
# forwarding handler below is that signal's only path into the detached session.
_ACTIVE_PROVIDER_PROCESS: subprocess.Popen[bytes] | None = None


def forward_termination_to_provider(signum: int, _frame: Any) -> None:
    """Take the detached provider's whole session down with us, then die honestly."""
    process = _ACTIVE_PROVIDER_PROCESS
    if process is not None and process.poll() is None:
        terminate_process_tree(process)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_termination_forwarding() -> None:
    if os.name != "posix":
        return
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward_termination_to_provider)


def remove_incompatible_codex_model_cache(codex_home: Path) -> bool:
    """Drop a regenerable cache that the installed Codex schema cannot decode."""
    cache = codex_home / "models_cache.json"
    if not cache.is_file():
        return False
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        models = payload.get("models") if isinstance(payload, dict) else None
        compatible = isinstance(models, list) and all(
            isinstance(model, dict) and "base_instructions" in model for model in models
        )
    except (OSError, json.JSONDecodeError):
        compatible = False
    if compatible:
        return False
    cache.unlink(missing_ok=True)
    return True


def run_provider_process(command: list[str], *, stdout: Any, stderr: Any,
                         timeout: int, cwd: str, input_bytes: bytes | None,
                         stdin: Any, env: dict[str, str],
                         completion_path: Path | None = None,
                         lease_fd: int | None = None,
                         deadline: float | None = None) -> int:
    """Run one provider in an isolated process group with a hard timeout."""
    global _ACTIVE_PROVIDER_PROCESS
    deadline = time.monotonic() + timeout if deadline is None else deadline
    provider_lock_fd = None
    process: subprocess.Popen[bytes] | None = None
    child_env = dict(env)
    cleanup_marker = child_env.pop(CODEX_INVOCATION_HOME_MARKER, None)
    cleanup_home = None
    if cleanup_marker and child_env.get("CODEX_HOME"):
        marked_home = Path(cleanup_marker).resolve()
        if (
            marked_home == Path(child_env["CODEX_HOME"]).resolve()
            and marked_home in _OWNED_CODEX_INVOCATION_HOMES
        ):
            cleanup_home = marked_home
    try:
        codex_home = child_env.get("CODEX_HOME")
        codex_home_lock_contended = False
        if codex_home:
            lock_path = Path(codex_home) / ".agent-runner-provider.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            provider_lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            while True:
                try:
                    fcntl.flock(provider_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    codex_home_lock_contended = True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProviderLeaseBusy("codex automation home is busy") from error
                    time.sleep(min(0.01, remaining))
            if codex_home_lock_contended and time.monotonic() >= deadline:
                raise ProviderLeaseBusy("codex automation home is busy")
            remove_incompatible_codex_model_cache(Path(codex_home))
        inherited_fds = tuple(fd for fd in (lease_fd, provider_lock_fd) if fd is not None)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_bytes is not None else stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=child_env,
            start_new_session=os.name == "posix",
            pass_fds=inherited_fds,
        )
        _ACTIVE_PROVIDER_PROCESS = process
        if input_bytes is not None and process.stdin is not None:
            process.stdin.write(input_bytes)
            process.stdin.close()
        stable: tuple[int, int, float] | None = None
        while process.poll() is None:
            if completion_path is not None and completion_path.is_file():
                snapshot = (completion_path.stat().st_size, completion_path.stat().st_mtime_ns)
                if stable is None or stable[:2] != snapshot:
                    stable = (*snapshot, time.monotonic())
                elif time.monotonic() - stable[2] >= 2:
                    try:
                        terminate_process_tree(process)
                    except PermissionError:
                        # macOS can deny a group signal after the provider has
                        # already sealed its result. Stop the leader directly
                        # and wait; never report success while it remains alive.
                        try:
                            process.terminate()
                        except PermissionError:
                            pass
                        process.wait(timeout=5)
                    return 0
            if time.monotonic() >= deadline:
                terminate_process_tree(process)
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.25)
    finally:
        if process is not None and process.poll() is None:
            terminate_process_tree(process)
        _ACTIVE_PROVIDER_PROCESS = None
        if provider_lock_fd is not None:
            os.close(provider_lock_fd)
        if cleanup_home:
            try:
                shutil.rmtree(cleanup_home, ignore_errors=True)
            finally:
                _OWNED_CODEX_INVOCATION_HOMES.discard(cleanup_home)
    return process.returncode


def resolve_executable(provider_config: dict[str, Any], default: str) -> str:
    """Resolve PATH first, then configured portable user-relative fallbacks."""
    primary = str(provider_config.get("executable", default))
    candidates = [primary, *provider_config.get("executable_fallbacks", [])]
    for candidate in candidates:
        expanded = os.path.expandvars(os.path.expanduser(str(candidate)))
        resolved = shutil.which(expanded)
        if resolved:
            return resolved
    return os.path.expandvars(os.path.expanduser(primary))


def codex_model_providers_toml(value: Any) -> str:
    """Render the small string-only provider table accepted by Codex -c."""
    if not isinstance(value, dict) or not value:
        raise ValueError("codex model_providers must be a non-empty object")
    providers: list[str] = []
    for name, config in value.items():
        if not isinstance(name, str) or not name or not isinstance(config, dict):
            raise ValueError("codex model_providers must map names to objects")
        fields: list[str] = []
        for key, item in config.items():
            if not isinstance(key, str) or not key or not isinstance(item, str):
                raise ValueError("codex model provider fields must be strings")
            fields.append(f"{key}={json.dumps(item)}")
        providers.append(f"{name}={{" + ",".join(fields) + "}")
    return "{" + ",".join(providers) + "}"


def parse_contract_result(text: str, *, salvage: bool = True) -> Any:
    """Read the contract object out of a provider reply that may carry prose.

    Providers under a schema contract sometimes answer conversationally: a sentence
    of preamble, then the object inside a ```json fence. Measured 2026-07-27 on
    gig-pass-1785123005 agent-LEARN -- the claude-direct fallback did the work
    correctly and wrote a valid object, but json.loads over the whole file failed at
    "line 1 column 1" and the step was recorded as a failure. Self-improvement had
    been stalled on this since 07-21, and capafy's listing lane fails identically.

    Strict parse first, so a clean reply is unaffected. Only then salvage, and only a
    real object: prose with no JSON at all still fails, because accepting an
    acknowledgement as a result is the defect this contract exists to prevent.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if not salvage:
            raise
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("no JSON object in provider result")


def validate_schema(value: Any, schema: dict[str, Any], at: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    expected_names = (
        [expected] if isinstance(expected, str)
        else expected if isinstance(expected, list)
        else []
    )
    known_names = [name for name in expected_names if name in type_map]
    if known_names:
        valid = False
        for name in known_names:
            matches = isinstance(value, type_map[name])
            if name in ("integer", "number") and isinstance(value, bool):
                matches = False
            valid = valid or matches
        if not valid:
            return [f"{at}: expected {' or '.join(known_names)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{at}: expected const {schema['const']!r}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{at}: missing required property {key}")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in value:
                errors.extend(validate_schema(value[key], child, f"{at}.{key}"))
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{at}: expected at least {schema['minItems']} items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, schema["items"], f"{at}[{index}]"))
    return errors


def candidate_capabilities(provider_config: dict[str, Any], candidate: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    required = candidate.get("required_capabilities", [])
    if not isinstance(required, list) or not all(isinstance(item, str) and item for item in required):
        raise ValueError("required_capabilities must be an array of non-empty strings")
    by_model = provider_config.get("model_capabilities", {})
    if not isinstance(by_model, dict):
        raise ValueError("provider model_capabilities must be an object")
    available = by_model.get(candidate.get("model"), {})
    if not isinstance(available, dict):
        raise ValueError("model capabilities must be an object")
    missing = [name for name in required if available.get(name) is not True]
    if missing:
        raise ValueError(f"missing required model capabilities: {', '.join(missing)}")
    return required, available


def command_for(provider: str, executable: str, provider_config: dict[str, Any],
                candidate: dict[str, Any], args: argparse.Namespace, prompt: str,
                schema: dict[str, Any], result_path: Path,
                timeout_seconds: int | None = None, session_id: str | None = None,
                *, prompt_via_stdin: bool = False,
                rollout_budget_tokens: int | None = None) -> list[str]:
    del timeout_seconds, session_id
    model = candidate["model"]
    effort = candidate.get("effort", "medium")
    if provider == "codex":
        resume_session_id = getattr(args, "codex_resume_session_id", None)
        command = [executable, "exec"]
        if not resume_session_id:
            command.append("--ephemeral")
        command.extend(["--model", model, "-c", f'model_reasoning_effort="{effort}"'])
        model_provider = provider_config.get("model_provider")
        if model_provider:
            if not isinstance(model_provider, str):
                raise ValueError("codex model_provider must be a string")
            command.extend(["-c", f"model_provider={json.dumps(model_provider)}"])
            command.extend([
                "-c",
                "model_providers=" + codex_model_providers_toml(
                    provider_config.get("model_providers")
                ),
            ])
        if provider_config.get("automation_home"):
            project_doc_max_bytes = provider_config.get("project_doc_max_bytes", 0)
            if not isinstance(project_doc_max_bytes, int) or project_doc_max_bytes < 0:
                raise ValueError("codex project_doc_max_bytes must be a nonnegative integer")
            disabled_skills = provider_config.get("disabled_skills", [])
            if not isinstance(disabled_skills, list) or not all(
                isinstance(name, str) and name for name in disabled_skills
            ):
                raise ValueError("codex disabled_skills must be a list of nonempty names")
            skill_rows = ",".join(
                f'{{name={json.dumps(name)},enabled=false}}' for name in disabled_skills
            )
            command.extend([
                "-c", f"project_doc_max_bytes={project_doc_max_bytes}",
                "-c", f"shell_environment_policy.set.HOME={json.dumps(str(Path.home()))}",
                "-c", f"skills.config=[{skill_rows}]",
            ])
            disabled_features = provider_config.get("disabled_features", [])
            if not isinstance(disabled_features, list) or not all(
                isinstance(name, str) and name for name in disabled_features
            ):
                raise ValueError("codex disabled_features must be a list of nonempty names")
            for feature in disabled_features:
                command.extend(["--disable", feature])
        if rollout_budget_tokens is not None:
            if rollout_budget_tokens <= 0:
                raise ValueError("codex rollout budget must be positive")
            command.extend(["-c", (
                "features.rollout_budget={enabled=true,"
                f"limit_tokens={rollout_budget_tokens},"
                "reminder_at_remaining_tokens=[],"
                "sampling_token_weight=1.0,prefill_token_weight=1.0}")])
        command.extend(["--ignore-user-config", "--json"])
        if schema:
            command.extend([
                "--output-schema", str(codex_output_schema(schema, result_path)),
            ])
        command.extend(["-o", str(result_path)])
        for image in getattr(args, "image", []) or []:
            command.extend(["--image", str(image)])
        if args.task_class == "writer-repair-agent":
            command.extend([
                "--sandbox", "workspace-write",
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "-c", "sandbox_workspace_write.network_access=false",
            ])
        elif args.task_class in TOOLLESS_TASK_CLASSES:
            command.extend(["--sandbox", "read-only"])
            for feature in TOOLLESS_CODEX_DISABLED_FEATURES:
                command.extend(["--disable", feature])
        elif getattr(args, "read_only", False):
            command.extend(["--sandbox", "read-only"])
        else:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.extend(["--skip-git-repo-check", "-C", str(args.workdir)])
        if resume_session_id:
            command.extend(["resume", resume_session_id])
        command.append("-" if prompt_via_stdin else prompt)
        return command
    if provider in CLAUDE_PROVIDERS:
        command = [
            executable, "--model", model, "--no-session-persistence",
            "--output-format", "json",
        ]
        if args.task_class in TOOLLESS_TASK_CLASSES:
            command.extend(["--tools", ""])
        elif not getattr(args, "read_only", False):
            command.append("--dangerously-skip-permissions")
        command.append("-p")
        if not prompt_via_stdin:
            command.append(prompt)
        return command
    raise ValueError(f"unsupported provider adapter: {provider}")


CODEX_QUOTA_CODES = frozenset((
    "insufficient_quota", "quota_exceeded", "usage_limit_reached", "usage_limit_exceeded",
    "usage_not_included", "rate_limit_exceeded", "rate_limit_reached",
    "billing_hard_limit_reached", "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted", "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
))
CODEX_AUTH_CODES = frozenset((
    "authentication_error", "authentication_failed", "invalid_api_key", "invalid_token",
    "oauth_token_expired", "authentication_session_expired", "token_expired", "refresh_token_expired",
    "refresh_token_reused", "refresh_token_revoked", "unauthorized",
))
CODEX_UNAVAILABLE_CODES = frozenset((
    "server_overloaded", "internal_server_error", "connection_failed",
    "http_connection_failed", "response_stream_connection_failed", "response_stream_disconnected",
))
CODEX_QUOTA_STATUS = frozenset((402, 429))
CODEX_UNAVAILABLE_STATUS = frozenset((408, 500, 502, 503, 504, 529))
CODEX_AUTH_STATUS = frozenset((401, 403))
CODEX_QUOTA_MESSAGE_PREFIXES = (
    "quota exceeded", "insufficient quota", "usage limit reached", "you've hit your usage limit",
    "rate limit exceeded", "rate limit reached for ", "your workspace is out of credits",
    "you hit your spend cap",
)
CODEX_AUTH_MESSAGE_PREFIXES = (
    "authentication failed", "authentication error", "invalid api key", "invalid token",
    "unauthorized", "session expired", "your access token could not be refreshed because",
)


def _codex_error_envelopes(text: str):
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type in {"turn.failed", "error"}:
            error = event.get("error") if isinstance(event.get("error"), dict) else event
            if isinstance(error, dict):
                yield error
        elif event_type == "response.failed":
            response = event.get("response")
            error = response.get("error") if isinstance(response, dict) else None
            if isinstance(error, dict):
                yield error
        elif event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "error":
                yield item


def _codex_structured_error_class(stdout: str, stderr: str) -> str | None:
    for error in (*_codex_error_envelopes(stdout), *_codex_error_envelopes(stderr)):
        codes = {
            str(error.get(key)).strip().casefold()
            for key in ("code", "error_code", "errorCode", "codex_error_info", "error_type", "type")
            if isinstance(error.get(key), str) and error.get(key).strip() and error.get(key) != "error"
        }
        status = next((int(error[key]) for key in ("status", "status_code", "statusCode", "http_status")
                       if str(error.get(key, "")).isdigit()), None)
        message = " ".join(error.get("message", "").split()).casefold() if isinstance(error.get("message"), str) else ""
        if codes & CODEX_UNAVAILABLE_CODES or status in CODEX_UNAVAILABLE_STATUS:
            return "transient_unavailable"
        if codes & CODEX_AUTH_CODES or status in CODEX_AUTH_STATUS or any(
            message.startswith(prefix) for prefix in CODEX_AUTH_MESSAGE_PREFIXES
        ):
            return "transient_auth"
        if codes & CODEX_QUOTA_CODES or status in CODEX_QUOTA_STATUS or any(
            message.startswith(prefix) for prefix in CODEX_QUOTA_MESSAGE_PREFIXES
        ):
            return "transient_quota"
    return None


def classify_provider_error(
    rc: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    launch_error: str,
    provider: str | None = None,
) -> str:
    """Classify failures for safe fallback. Only transient provider failures retry."""
    if timed_out or rc == 124:
        return "transient_timeout"
    if provider == "codex":
        structured = _codex_structured_error_class(stdout, stderr)
        if structured:
            return structured
        text = f"{stderr}\n{launch_error}".lower()
        if any(token in text for token in (
            "failed to lookup address information", "could not resolve host",
            "nodename nor servname", "name or service not known", "connection refused",
            "connection reset", "network is unreachable", "stream disconnected before completion",
            "timed out negotiating with the code-mode host",
        )) or rc == 127 or launch_error:
            return "transient_unavailable"
        return "validation_or_task_failure"
    # Claude prints quota/weekly-limit notices to stdout (with an empty
    # stderr), so both provider streams are part of the transient signal.
    text = f"{stdout}\n{stderr}\n{launch_error}".lower()
    if "shared rollout token budget exhausted" in text:
        return "native_rollout_budget_exhausted"
    if any(token in text for token in (
        "failed to lookup address information", "could not resolve host",
        "nodename nor servname", "name or service not known",
        "connection refused", "connection reset", "network is unreachable",
        "stream disconnected before completion",
    )):
        return "transient_unavailable"
    if any(token in text for token in (
        "invalid credentials", "invalid api key", "invalid token",
        "permission denied", "insufficient permission", "forbidden", "unauthorized",
    )):
        return "validation_or_task_failure"
    if any(token in text for token in (
        "oauth session expired", "authentication session expired", "auth session expired",
        "oauth token expired", "authentication token expired", "access token expired",
        "access token has expired", "session token expired", "session token has expired",
        "oauth token refresh failed", "auth token refresh failed", "token refresh failed",
        "failed to refresh oauth", "failed to refresh auth", "failed to refresh token",
        "could not refresh oauth", "could not refresh auth", "could not refresh token",
    )):
        return "transient_auth"
    if any(token in text for token in (
        "quota", "429", "rate limit", "rate_limit", "usage limit", "usage_limit",
        "weekly limit", "weekly_limit", "resets jul",
        "temporarily unavailable", "overloaded",
    )):
        return "transient_quota"
    if rc == 127 or launch_error:
        return "transient_unavailable"
    return "validation_or_task_failure"


def codex_failover_action(
    candidate: dict[str, Any],
    error_class: str | None,
    result_fresh: bool,
    attempt_started_work: bool,
) -> str:
    """Choose the only safe next step after a failed Codex account attempt."""
    if candidate.get("provider") != "codex":
        return "not_applicable"
    if attempt_started_work:
        return "stop"
    if result_fresh and error_class != "transient_unavailable":
        return "stop"
    if candidate.get("account_fallback_next"):
        if error_class in {"transient_quota", "transient_auth"}:
            return "retry_next_account"
        if error_class in {"transient_timeout", "transient_unavailable"}:
            return "continue_next_non_codex"
        return "stop"
    if error_class in {
        "transient_timeout", "transient_quota", "transient_unavailable", "transient_auth",
    }:
        return "continue_next_non_codex"
    return "stop"


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-class", required=True,
                        choices=("deterministic", "composition-agent", "reply-semantic-agent", "storefront-proposal-agent", "repeatable-agent", "tool-agent", "browser-lane-agent", "application-lane-agent", "application-intent-planner", "diagnostic-agent", "marketing-agent", "high-value-agent", "escalation-agent", "writer-sol-audit", "writer-repair-agent", "affiliate-marketing-agent", "affiliate-escalation-agent"))
    prompt_source = parser.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("--prompt-file", type=Path)
    prompt_source.add_argument("--prompt-stdin", action="store_true")
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--task-label", required=True)
    parser.add_argument("--loop", default="unattributed")
    parser.add_argument("--workdir", type=Path, default=Path.home())
    parser.add_argument("--candidate-profile")
    parser.add_argument("--escalation-reason")
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--image", action="append", type=Path, default=[])
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--codex-resume-session-id")
    parsed = parser.parse_args()

    if parsed.task_class in {"composition-agent", "reply-semantic-agent", "storefront-proposal-agent", "application-intent-planner"} and not parsed.prompt_stdin:
        parser.error(f"{parsed.task_class} requires --prompt-stdin")

    config_path = Path(os.environ.get("AGENT_RUNNER_CONFIG", HERE / "config.json"))
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        schema = json.loads(parsed.schema.read_text(encoding="utf-8"))
        prompt = sys.stdin.read() if parsed.prompt_stdin else parsed.prompt_file.read_text(encoding="utf-8")
        # Fail closed before any provider process starts. Billing begins at
        # launch, not at a usable answer: capafy was charged $0.135 on both
        # 2026-07-26 and 2026-07-27 for a one-turn greeting. A prompt this
        # small cannot carry a bounded task, so paying to discover that is
        # pure waste and the guard must run before the launch, not after.
        min_prompt_chars = int(os.environ.get(
            "AGENT_RUNNER_MIN_PROMPT_CHARS", MIN_PROMPT_CHARS))
        stripped_prompt_chars = len(prompt.strip())
        if stripped_prompt_chars < min_prompt_chars:
            raise ValueError(
                f"prompt is empty or too small to carry a task "
                f"({stripped_prompt_chars} chars < {min_prompt_chars}); "
                "refusing to launch a paid provider"
            )
        task_config = config["task_classes"][parsed.task_class]
        candidates = resolve_provider_profiles(
            task_config["candidates"], config.get("providers", {})
        )
        for candidate in candidates:
            if "timeout_seconds" not in candidate:
                continue
            candidate_timeout = candidate["timeout_seconds"]
            if (not isinstance(candidate_timeout, int)
                    or isinstance(candidate_timeout, bool) or candidate_timeout <= 0):
                raise ValueError("candidate timeout_seconds must be a positive integer")
        configured_timeout_seconds = int(
            task_config.get("timeout_seconds", config["timeout_seconds"])
        )
        if configured_timeout_seconds < 1:
            raise ValueError("configured timeout must be positive")
        if parsed.timeout_seconds is not None and parsed.timeout_seconds < 1:
            raise ValueError("explicit timeout must be positive")
        timeout_seconds = min(
            configured_timeout_seconds,
            parsed.timeout_seconds
            if parsed.timeout_seconds is not None
            else configured_timeout_seconds,
        )
        route = str(task_config.get("route") or f"{parsed.task_class}:configured")
        escalation_reason = (
            parsed.escalation_reason.strip()
            if isinstance(parsed.escalation_reason, str) and parsed.escalation_reason.strip()
            else None
        )
        restricted_candidates = [
            candidate for candidate in candidates
            if str(candidate.get("effort") or "") in RESTRICTED_EFFORTS
            or "sol" in str(candidate.get("model") or "").lower()
        ]
        requires_explicit_escalation = bool(
            task_config.get("requires_explicit_escalation")
        )
        if restricted_candidates and not requires_explicit_escalation:
            raise ValueError("high-effort/Sol candidates require an explicit escalation route")
        if requires_explicit_escalation and escalation_reason is None:
            raise ValueError("explicit escalation reason is required")
        if not requires_explicit_escalation and escalation_reason is not None:
            raise ValueError("escalation reason is only valid for an explicit escalation route")
        candidate_profile: dict[str, Any] = {}
        if parsed.candidate_profile:
            candidate_profile = config.get("candidate_profiles", {}).get(parsed.candidate_profile)
            if not isinstance(candidate_profile, dict):
                raise ValueError(f"candidate profile not configured: {parsed.candidate_profile}")
            if candidate_profile.get("task_class") != parsed.task_class:
                raise ValueError("candidate profile task_class mismatch")
        budget_scope_id = os.environ.get("ANICCA_BUDGET_SCOPE_ID", "").strip()
        pass_budget_raw = os.environ.get("ANICCA_PASS_TOKEN_BUDGET", "").strip()
        daily_budget_raw = os.environ.get("ANICCA_LOOP_DAILY_TOKEN_BUDGET", "").strip()
        if bool(budget_scope_id) != bool(pass_budget_raw):
            raise ValueError("token budget scope/pass settings must be provided together")
        budget_enabled = bool(budget_scope_id and pass_budget_raw)
        if os.environ.get("ANICCA_BUDGET_REQUIRED", "").strip() == "1" and not budget_enabled:
            raise ValueError("token budget is required but scope/pass/daily settings are missing")
        task_token_reservation = int(task_config.get("token_reservation", 0))
        pass_token_budget = int(pass_budget_raw or 0)
        daily_token_budget = int(daily_budget_raw) if daily_budget_raw else None
        if budget_enabled and (
            task_token_reservation <= 0 or pass_token_budget <= 0
            or (daily_token_budget is not None and daily_token_budget <= 0)
        ):
            raise ValueError("enabled token budgets and task reservation must be positive")
        token_reservation = pass_token_budget if budget_enabled else task_token_reservation
        budget_daily_scope = (
            os.environ.get("ANICCA_BUDGET_DAILY_SCOPE", "").strip() or parsed.loop)
        budget_ledger = TokenBudgetLedger(Path(os.environ.get(
            "ANICCA_TOKEN_BUDGET_LEDGER",
            Path.home() / ".local/state/life-manager/telemetry/token-budget.jsonl")))
        budget_day = budget_day_for(
            datetime.now(timezone.utc),
            os.environ.get("ANICCA_BUDGET_DAY_TZ", "").strip() or "Asia/Tokyo")
    except Exception as error:
        print(f"agent-runner: invalid input/config: {error}", file=sys.stderr)
        return 2
    if parsed.task_class == "deterministic" or not candidates:
        print("agent-runner: deterministic tasks have no model candidate", file=sys.stderr)
        return 2

    try:
        lease_fd = acquire_provider_lease(
            os.environ.get("LIFE_MANAGER_PROVIDER_LEASE_PATH", "").strip()
        )
    except ProviderLeaseBusy:
        print(PROVIDER_LEASE_BUSY_LINE, file=sys.stderr)
        return PROVIDER_LEASE_BUSY
    except (OSError, ValueError) as error:
        print(f"agent-runner: provider lease failed: {error}", file=sys.stderr)
        return 2

    evidence_dir = parsed.evidence_dir.resolve()
    # Do this before creating/writing attempt files or launching a billable
    # provider.  A full volume used to make Codex panic while writing its own
    # evidence, leaving Capafy drafts stranded despite an otherwise healthy
    # publish session.
    try:
        retention = ensure_evidence_capacity(evidence_dir)
    except (OSError, ValueError) as error:
        if lease_fd is not None:
            os.close(lease_fd)
        print(f"agent-runner: evidence preflight failed: {error}", file=sys.stderr)
        return 2
    evidence_dir.mkdir(parents=True, exist_ok=True)
    attempts_path = evidence_dir / "attempts.jsonl"
    summary_path = evidence_dir / "summary.json"
    # An evidence directory is one logical run. Reusing it must never let a
    # prior result/attempt/summary satisfy the current run.
    attempts_path.unlink(missing_ok=True)
    summary_path.unlink(missing_ok=True)
    started_at = utc_now()
    attempts: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    budget_blocked: dict[str, Any] | None = None
    last_budget: dict[str, Any] = {"status": "disabled"}
    total_deadline = time.monotonic() + timeout_seconds
    skip_codex_profiles = False
    for index, candidate in enumerate(candidates, 1):
        remaining_timeout_seconds = math.floor(total_deadline - time.monotonic())
        if remaining_timeout_seconds < 1:
            break
        effective_candidate = dict(candidate)
        provider = effective_candidate["provider"]
        if skip_codex_profiles and provider == "codex":
            continue
        attempt_timeout_seconds = min(
            remaining_timeout_seconds,
            effective_candidate.get("timeout_seconds", remaining_timeout_seconds))
        provider_config = dict(config.get("providers", {}).get(provider, {}))
        for key in ("profile_alias", "automation_home", "auth_file"):
            if key in effective_candidate:
                provider_config[key] = effective_candidate[key]
        executable = resolve_executable(provider_config, provider)
        stdout_path = evidence_dir / f"attempt-{index:02d}.stdout.log"
        stderr_path = evidence_dir / f"attempt-{index:02d}.stderr.log"
        result_path = evidence_dir / f"attempt-{index:02d}.result.json"
        for stale_path in (stdout_path, stderr_path, result_path):
            stale_path.unlink(missing_ok=True)
        budget_event_id = f"agent-budget-{uuid.uuid4().hex}"
        if budget_enabled:
            last_budget = budget_ledger.reserve(
                event_id=budget_event_id,
                loop=parsed.loop,
                scope_id=budget_scope_id,
                daily_scope=budget_daily_scope,
                day=budget_day,
                reservation_tokens=token_reservation,
                pass_limit=pass_token_budget,
                daily_limit=daily_token_budget,
            )
            if last_budget["status"] == "blocked":
                budget_blocked = last_budget
                break
        attempt_started = utc_now()
        attempt_started_ns = time.time_ns()
        monotonic_start = time.monotonic()
        rc = 127
        timed_out = False
        launch_error = ""
        adapter_error = ""
        required_capabilities: list[str] = []
        model_capabilities: dict[str, Any] = {}
        candidate_prompt = prompt
        if provider in CLAUDE_PROVIDERS:
            # Codex is handed the output schema through --output-schema, so it answers in the
            # required shape. The claude CLI has no equivalent flag, so without this the model
            # guesses the envelope, schema validation fails, and the runner correctly refuses to
            # select a schema-invalid answer -- the fallback exists but can never be chosen.
            # Measured 2026-08-31: attempt-03 returned is_error=false with a well-formed decision
            # that was discarded because it was not wrapped in the schema's envelope.
            # Measured 2026-09-04 on the storefront proposal agent: rc=0, a complete well-formed
            # object -- but built from the caller's own context field names (hypothesis_key,
            # before) instead of the schema's (hypothesis, proposed_price_jpy, uncertainty). The
            # schema dump alone sits at the tail of a long prompt behind a same-shaped context
            # object; naming the required keys is what a Codex candidate gets for free from
            # --output-schema and a Claude candidate does not.
            required_top_level = schema.get("required", []) if isinstance(schema, dict) else []
            candidate_prompt = (
                f"{prompt}\n\n"
                "OUTPUT CONTRACT: reply with one JSON document and nothing else. No prose, no "
                "code fence. It must validate against this JSON Schema exactly, including the "
                "top-level envelope:\n"
                f"{json.dumps(schema, ensure_ascii=False)}\n"
                + (
                    "The top-level object MUST contain exactly these keys, spelled exactly as "
                    f"in the schema: {', '.join(required_top_level)}. Use the schema's own key "
                    "names even when the input context above uses different names for a similar "
                    "concept; do not copy a context object's field names or add fields the "
                    "schema does not define.\n"
                    if isinstance(required_top_level, list) and required_top_level else ""
                )
            )
        try:
            required_capabilities, model_capabilities = candidate_capabilities(provider_config, effective_candidate)
            command = command_for(
                provider, executable, provider_config, effective_candidate, parsed, candidate_prompt,
                schema, result_path,
                prompt_via_stdin=parsed.prompt_stdin,
                rollout_budget_tokens=pass_token_budget if budget_enabled else None,
            )
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                try:
                    rc = run_provider_process(
                        command,
                        stdout=stdout,
                        stderr=stderr,
                        timeout=attempt_timeout_seconds,
                        cwd=parsed.workdir,
                        input_bytes=candidate_prompt.encode("utf-8") if parsed.prompt_stdin else None,
                        stdin=None if parsed.prompt_stdin else subprocess.DEVNULL,
                        env=provider_process_env(
                            provider,
                            provider_config,
                            task_class=parsed.task_class,
                            invocation_id=(
                                f"{os.getpid()}-{index}-{uuid.uuid4().hex}"
                                if provider == "codex" else None
                            ),
                        ),
                        completion_path=result_path,
                        lease_fd=lease_fd,
                        deadline=total_deadline,
                    )
                except subprocess.TimeoutExpired:
                    timed_out = True
                    rc = 124
                except ProviderLeaseBusy as error:
                    launch_error = str(error)
                    stderr.write((launch_error + "\n").encode())
                    rc = 75
                except OSError as error:
                    launch_error = str(error)
                    stderr.write((launch_error + "\n").encode())
                    rc = 127
        except Exception as error:
            adapter_error = str(error)
            rc = 2
            stdout_path.touch()
            stderr_path.write_text(adapter_error + "\n", encoding="utf-8")

        if rc == 0 and not timed_out and provider in CLAUDE_PROVIDERS and not result_path.exists():
            adapter_error = extract_claude_payload(stdout_path, result_path)
        result_fresh = result_path.is_file() and result_path.stat().st_mtime_ns >= attempt_started_ns
        schema_valid = False
        schema_errors: list[str] = []
        if result_fresh and (rc == 0 or (provider == "codex" and timed_out)):
            try:
                result = parse_contract_result(result_path.read_text(encoding="utf-8"))
                schema_errors = validate_schema(result, schema)
                schema_valid = not schema_errors
            except Exception as error:
                schema_errors = [f"result parse failed: {error}"]
        elif not result_fresh:
            schema_errors = ["provider did not produce a fresh result for this attempt"]

        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        usage = extract_provider_usage(provider, stdout_text, model=effective_candidate.get("model"))
        if budget_enabled:
            charged_tokens = budget_charge_tokens(provider, usage, token_reservation)
            settlement = budget_ledger.settle(
                event_id=budget_event_id,
                actual_tokens=charged_tokens,
                measurement=str(usage["measurement"]),
            )
            last_budget = {
                **last_budget,
                "charged_tokens": charged_tokens,
                "measurement": usage["measurement"],
                "pass_consumed_after_tokens": settlement["pass_consumed_after_tokens"],
                "daily_consumed_after_tokens": settlement["daily_consumed_after_tokens"],
            }
        codex_toolhost_unavailable = (
            provider == "codex"
            and "timed out negotiating with the code-mode host" in stderr_text.lower()
            and not codex_attempt_started_work(stdout_text)
        )
        accepted_result = (
            schema_valid
            and (rc == 0 or (provider == "codex" and timed_out))
            and not codex_toolhost_unavailable
        )
        error_class = None if accepted_result else classify_provider_error(
            rc, timed_out, stdout_text, stderr_text, launch_error, provider=provider,
        )

        row = {
            "attempt": index,
            "started_at": attempt_started,
            "finished_at": utc_now(),
            "duration_ms": round((time.monotonic() - monotonic_start) * 1000),
            "task_label": parsed.task_label,
            "task_class": parsed.task_class,
            "route": route,
            "escalated": requires_explicit_escalation,
            "escalation_reason": escalation_reason,
            "provider": provider,
            "profile_alias": provider_config.get("profile_alias"),
            "budget": last_budget,
            "model": effective_candidate.get("model"),
            "effort": effective_candidate.get("effort"),
            "required_capabilities": required_capabilities,
            "model_capabilities": model_capabilities,
            "executable": executable,
            "rc": rc,
            "timed_out": timed_out,
            "stdout_path": str(stdout_path),
            "stdout_sha256": sha256_file(stdout_path),
            "stderr_path": str(stderr_path),
            "stderr_sha256": sha256_file(stderr_path),
            "result_path": str(result_path),
            "result_present": result_fresh,
            "schema_path": str(parsed.schema.resolve()),
            "schema_valid": schema_valid,
            "schema_errors": schema_errors,
            "launch_error": launch_error or None,
            "adapter_error": adapter_error or None,
            "error_class": error_class,
            "capabilities": provider_config.get("capabilities", {}),
            "cost_tier": provider_config.get("cost_tier"),
            "quota": provider_config.get("quota"),
            "usage": usage,
        }
        usage_path = Path(os.environ.get("ANICCA_USAGE_LEDGER", DEFAULT_USAGE_LEDGER))
        provider_name = usage.get("upstream_provider") or {
            "codex": "openai", "claude": "anthropic",
            "claude-direct": "anthropic",
        }.get(provider, provider)
        usage_event = {
            "version": 1,
            "event_id": hashlib.sha256(
                f"{evidence_dir}:{index}".encode("utf-8")
            ).hexdigest()[:24],
            "timestamp": row["finished_at"],
            "loop": parsed.loop,
            "task_label": parsed.task_label,
            "task_class": parsed.task_class,
            "route": route,
            "escalated": requires_explicit_escalation,
            "escalation_reason": escalation_reason,
            "attempt": index,
            "provider": provider,
            "profile_alias": provider_config.get("profile_alias"),
            "provider_name": provider_name,
            "model": effective_candidate.get("model"),
            "upstream_model": usage.get("upstream_model"),
            "effort": effective_candidate.get("effort"),
            "status": "success" if accepted_result else "failed",
            "error_class": error_class,
            "duration_ms": row["duration_ms"],
            "measurement": usage["measurement"],
            "tokens": {
                "input": usage["input_tokens"],
                "cached_input": usage["cached_input_tokens"],
                "cache_creation_input": usage["cache_creation_input_tokens"],
                "output": usage["output_tokens"],
                "reasoning_output": usage["reasoning_output_tokens"],
                "total": usage["total_tokens"],
            },
            "provider_cost_usd": usage["provider_cost_usd"],
            "cost_basis": usage["cost_basis"],
            "gen_ai": {
                "operation_name": "invoke_agent",
                "provider_name": provider_name,
                "request_model": effective_candidate.get("model"),
            },
        }
        try:
            append_usage_event(usage_path, usage_event)
        except OSError as error:
            row["telemetry_error"] = str(error)
        with attempts_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        attempts.append(row)
        if accepted_result:
            selected = row
            break
        # A valid response with a schema/contract error is deterministic and
        # must not silently switch providers. Fallback is only for transient
        # timeout, expired provider auth, provider availability, or quota failures.
        if rc == 0 and result_fresh and not schema_valid:
            break
        if provider == "codex":
            action = codex_failover_action(
                effective_candidate,
                error_class,
                result_fresh,
                codex_attempt_started_work(stdout_text),
            )
            if action == "retry_next_account":
                continue
            if action == "continue_next_non_codex":
                skip_codex_profiles = True
                continue
            break
        if error_class not in (
            "transient_timeout", "transient_quota", "transient_unavailable", "transient_auth",
        ):
            break

    summary = {
        "version": 1,
        "started_at": started_at,
        "finished_at": utc_now(),
        "task_label": parsed.task_label,
        "loop": parsed.loop,
        "task_class": parsed.task_class,
        "route": route,
        "escalated": requires_explicit_escalation,
        "escalation_reason": escalation_reason,
        "status": "success" if selected else ("budget_blocked" if budget_blocked else "failed"),
        "budget": budget_blocked or last_budget,
        "selected_provider": selected["provider"] if selected else None,
        "selected_profile_alias": selected["profile_alias"] if selected else None,
        "selected_model": selected["model"] if selected else None,
        "selected_effort": selected["effort"] if selected else None,
        "attempt_count": len(attempts),
        "attempts_path": str(attempts_path),
        "result_path": selected["result_path"] if selected else None,
        "evidence_reclamation": retention,
    }
    runtime_event_failed = False
    release_sha = os.environ.get("LIFE_MANAGER_RELEASE_SHA", "").strip()
    if release_sha:
        registry_path = Path(os.environ.get(
            "LIFE_MANAGER_REGISTRY", REPO_ROOT / "config" / "loop-registry.json"))
        try:
            event = emit_runtime_event(
                loop_id=runtime_event_loop_id(parsed.loop),
                evidence_dir=evidence_dir,
                selected=selected,
                attempts=attempts,
                candidate_profile=(selected or (attempts[-1] if attempts else {})).get(
                    "profile_alias"),
                registry_path=registry_path,
                release_sha=release_sha,
            )
            summary["runtime_event_id"] = event["event_id"]
        except (OSError, ValueError, json.JSONDecodeError) as error:
            runtime_event_failed = True
            summary["status"] = "failed"
            summary["runtime_event_error"] = str(error)
    atomic_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    if lease_fd is not None:
        os.close(lease_fd)
    if selected and not runtime_event_failed:
        return 0
    return 75 if budget_blocked else 1


if __name__ == "__main__":
    install_termination_forwarding()
    raise SystemExit(run())
