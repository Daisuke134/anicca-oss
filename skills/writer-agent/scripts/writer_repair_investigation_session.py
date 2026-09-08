#!/usr/bin/env python3
"""Resumable, machine-readable Codex sessions for the repair investigation.

SSOT §9.3.1 item H3 merged with §9.4 items C1 and C3.  This module owns three
things and nothing else, so the daily writing, editorial judge, and vision
paths are untouched:

* **C1 — the outcome is read, not guessed.**  `codex exec` exits only `0` or
  `1` (`codex-rs/exec/src/lib.rs`), so the exit status cannot name a failure
  class.  The verdict is derived from the `--json` JSONL event stream instead,
  and a failing slice records which event decided it.
* **H3 — a budget checkpoints instead of truncating.**  The live 2026-08-07
  receipt recorded `model.status TIMEOUT` at exactly its 120-second bound and
  therefore produced no verdict at all.  A slice that runs out of budget now
  persists the partial findings and the Codex session identity, and the next
  tick continues that same session.
* **C3 — resume is guarded and bounded.**  One investigation may be continued;
  a second may never be started beside it.  Slices per incident are bounded and
  exhaustion degrades to the safest known state instead of retrying forever.

Everything asserted about the wire format was measured against the installed
`codex-cli 0.145.0` on 2026-08-07, not read from documentation:

| Observation | Consequence here |
|---|---|
| success emits `turn.completed` with a `usage` block | `COMPLETED`; tokens are measured rather than declared unknown |
| failure emits top-level `{"type":"error"}` then `{"type":"turn.failed"}` and exits 1 | `FAILED`, naming the deciding event |
| an `item.completed` whose item `type` is `error` appeared inside a run that exited 0 with a completed turn | item-level errors are ADVISORY and must never be read as failure |
| a run killed mid-turn still flushed `thread.started` to its stdout file | the session id survives a budget kill, which is what makes a checkpoint possible |
| `codex exec resume <id>` recovered such a killed session in 5.0s with `cached_input_tokens` carried forward | an unterminated turn on disk is not itself a reason to refuse a resume |

Because that last measurement holds, the liveness guard for this path targets
the hazard that actually applies: a process still holding the session.  Running
a second turn on one thread is duplicate spend and is the inconsistent
"active thread with no running turn" state that openai/codex#37047 reports as
hanging `thread/resume` indefinitely.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


SCHEMA = "writer.self-heal.investigation-session"
VERSION = 1

# One investigation may be sliced this many times before the loop stops paying
# for it.  §9.3.1: "no surveyed system retries forever.  Each uses bounded
# attempts, then degrades to the safest known state, then stops and waits for a
# new trigger."
DEFAULT_MAX_SLICES = 3

TOKENS_UNKNOWN_NO_TURN = (
    "the turn never completed, so codex emitted no turn.completed usage block; "
    "a token count here would be invented"
)
COST_UNKNOWN_REASON = (
    "no price table or billing receipt is joined to a model invocation in this "
    "runtime, so a cost figure would be invented"
)

# The structured shape Terra must return.  Its keys are the keys the existing
# investigation receipt already stores (`cause_status`, `evidence_gaps`) plus
# the continuation fields H3 needs, so `writer_incident_queue.register_
# investigation` and every existing reader keep working unchanged.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "cause_status", "evidence_gaps", "findings", "primary_sources",
        "complete", "remaining_work",
    ],
    "properties": {
        "cause_status": {
            "type": "string",
            "enum": ["UNDETERMINED", "EVIDENCE_BACKED_HYPOTHESIS"],
        },
        "evidence_gaps": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["statement", "evidence", "verified"],
                "properties": {
                    "statement": {"type": "string"},
                    # Optional is expressed as nullable, never by omission.
                    "evidence": {"type": ["string", "null"]},
                    "verified": {"type": "boolean"},
                },
            },
        },
        "primary_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "title", "quote"],
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "quote": {"type": "string"},
                },
            },
        },
        "complete": {"type": "boolean"},
        "remaining_work": {"type": ["string", "null"]},
    },
}

# Composition keywords the guide lists as unsupported under strict mode.
UNSUPPORTED_SCHEMA_KEYWORDS = (
    "allOf", "not", "dependentRequired", "dependentSchemas", "if", "then", "else",
)


def assert_strict_schema(schema: Any, path: str = "$") -> None:
    """Fail locally on a schema the provider would reject, in the same words.

    Walks every nested object, including objects inside array `items`, because
    that is exactly where the production failure was: a valid top level does
    not make a schema valid.
    """
    if not isinstance(schema, dict):
        return
    for keyword in UNSUPPORTED_SCHEMA_KEYWORDS:
        if keyword in schema:
            raise ValueError(
                f"{path}: strict structured outputs do not support {keyword!r}"
            )
    if schema.get("type") == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"{path}: an object schema must declare properties")
        if schema.get("additionalProperties") is not False:
            raise ValueError(
                f"{path}: additionalProperties must always be set to false in objects"
            )
        required = schema.get("required")
        if not isinstance(required, list):
            raise ValueError(
                f"{path}: 'required' is required to be supplied and to be an array"
            )
        missing = [key for key in properties if key not in required]
        if missing:
            raise ValueError(
                f"{path}: 'required' must include every key in properties. Missing "
                f"{', '.join(repr(key) for key in missing)}. Express an optional "
                "field as a nullable type, never by omitting it from required"
            )
        unknown = [key for key in required if key not in properties]
        if unknown:
            raise ValueError(
                f"{path}: 'required' names keys absent from properties: "
                f"{', '.join(repr(key) for key in unknown)}"
            )
        for key, value in properties.items():
            assert_strict_schema(value, f"{path}.properties.{key}")
    items = schema.get("items")
    if items is not None:
        assert_strict_schema(items, f"{path}.items")

_REQUIRED_VERDICT_KEYS = tuple(VERDICT_SCHEMA["required"])
_CAUSE_STATUS_VALUES = tuple(VERDICT_SCHEMA["properties"]["cause_status"]["enum"])


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def read_deployed_commit(state_root: Path) -> str | None:
    """The deployed revision receipt for the installed loop, or None if unknown.

    Owned by `self_improve_control.update_deployed_commit`, which refuses any
    value that is not a full lowercase git object id equal to the runtime
    checkout's HEAD, and writes it atomically with fsync. The shared cleanup
    contract only admits marked terminal run directories beneath `runs/`, so
    this root-level receipt is outside its deletion boundary. It is the one
    code-version marker for this runtime; this module must not invent another.
    """
    marker = state_root / "deployed-commit"
    if not marker.is_file():
        return None
    value = marker.read_text(encoding="utf-8", errors="replace").strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def trigger_token(occurrence_count: Any, deployed_commit: str | None) -> dict[str, Any]:
    """What counts as a new input for a bounded investigation budget.

    SSOT §9.3.1 records the prior-art rule from Flagger as rolling back after a
    set number of failed checks and then not retrying "until a new commit
    arrives" -- a new commit, not a new occurrence. A deploy is therefore a
    genuine new input, and the budget resets for it just as it does for a fresh
    occurrence of the failure.
    """
    return {"occurrence_count": occurrence_count, "deployed_commit": deployed_commit}


def sessions_root(state_root: Path) -> Path:
    return state_root / "self-heal" / "investigation-sessions"


def checkpoint_path(state_root: Path, fingerprint: str) -> Path:
    return sessions_root(state_root) / f"{fingerprint}.json"


def schema_path(state_root: Path, fingerprint: str) -> Path:
    return sessions_root(state_root) / f"{fingerprint}.verdict-schema.json"


def session_dir(state_root: Path, fingerprint: str) -> Path:
    return sessions_root(state_root) / fingerprint


def _atomic(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def write_verdict_schema(state_root: Path, fingerprint: str) -> Path:
    # Fail closed here rather than spending a slice to be told by a 400.
    assert_strict_schema(VERDICT_SCHEMA)
    return _atomic(schema_path(state_root, fingerprint), VERDICT_SCHEMA)


def read_checkpoint(state_root: Path, fingerprint: str) -> dict[str, Any] | None:
    path = checkpoint_path(state_root, fingerprint)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if value.get("schema") != SCHEMA or value.get("version") != VERSION:
        return None
    return value


# ---------------------------------------------------------------------------
# C1: read the outcome out of the event stream
# ---------------------------------------------------------------------------

def parse_events(path: Path) -> dict[str, Any]:
    """Derive the slice outcome from the JSONL stream rather than the exit code.

    A partially written stream is expected and normal: a slice killed at its
    budget leaves whatever codex had already flushed, and that is exactly the
    material a checkpoint is built from.
    """
    result: dict[str, Any] = {
        "session_id": None,
        "stream_status": "NO_TERMINAL_EVENT",
        "deciding_event": None,
        "agent_messages": [],
        "advisory_errors": [],
        "failures": [],
        "usage": None,
        "event_count": 0,
        "malformed_lines": 0,
    }
    if not path.is_file():
        result["reason"] = f"no event stream was written to {path}"
        return result

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A kill can truncate the final line mid-write; that is not a failure.
            result["malformed_lines"] += 1
            continue
        if not isinstance(event, dict):
            result["malformed_lines"] += 1
            continue
        result["event_count"] += 1
        kind = event.get("type")
        if kind == "thread.started":
            result["session_id"] = event.get("thread_id")
        elif kind == "error":
            # Top-level error: `ServerNotification::Error` for a turn that will
            # not be retried (codex-rs/exec/src/lib.rs sets `error_seen`).
            result["failures"].append({
                "event": "error", "message": str(event.get("message") or "")[:2000],
            })
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            result["failures"].append({
                "event": "turn.failed", "message": str(message or "")[:2000],
            })
        elif kind == "turn.completed":
            result["usage"] = event.get("usage")
            result["stream_status"] = "COMPLETED"
            result["deciding_event"] = "turn.completed"
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                result["agent_messages"].append(str(item.get("text") or ""))
            elif item.get("type") == "error":
                # Measured: advisory, and present inside runs that exit 0.
                result["advisory_errors"].append(str(item.get("message") or "")[:2000])

    if result["failures"]:
        result["stream_status"] = "FAILED"
        result["deciding_event"] = result["failures"][0]["event"]
    return result


def parse_verdict(
    last_message: Path, stream: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the schema-bound final answer; never parse prose for a verdict."""
    text = ""
    if last_message.is_file():
        text = last_message.read_text(encoding="utf-8", errors="replace").strip()
    if not text and stream.get("agent_messages"):
        text = str(stream["agent_messages"][-1]).strip()
    if not text:
        return None, "no final message was written"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return None, f"final message is not the JSON the output schema requires: {error}"
    if not isinstance(value, dict):
        return None, "final message is not a JSON object"
    missing = [key for key in _REQUIRED_VERDICT_KEYS if key not in value]
    if missing:
        return value, f"verdict omits required keys: {', '.join(missing)}"
    if value.get("cause_status") not in _CAUSE_STATUS_VALUES:
        return value, (
            f"cause_status {value.get('cause_status')!r} is outside the schema enum"
        )
    return value, None


def tokens_from(stream: dict[str, Any]) -> dict[str, Any]:
    """Record measured usage, or unknown with a reason.  Never a fabricated zero."""
    usage = stream.get("usage")
    if not isinstance(usage, dict):
        return {"status": "unknown", "reason": TOKENS_UNKNOWN_NO_TURN}
    measured: dict[str, Any] = {"status": "measured", "source": "turn.completed"}
    for key in (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens",
    ):
        if isinstance(usage.get(key), int):
            measured[key] = usage[key]
    return measured


# ---------------------------------------------------------------------------
# C3: liveness guard
# ---------------------------------------------------------------------------

def liveness(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """May this checkpoint's session be resumed on this tick?

    Blocking condition: a process group we started is still running.  Resuming
    then would put a second turn on one thread, which is both duplicate spend
    and the inconsistent active-thread state openai/codex#37047 reports as a
    permanent hang.
    """
    process = checkpoint.get("process")
    process = process if isinstance(process, dict) else {}
    reaped = process.get("reaped") is True
    pgid = process.get("pgid")
    result: dict[str, Any] = {"pgid": pgid, "reaped": reaped}
    if reaped:
        result["alive"] = False
        result["safe_to_resume"] = True
        result["reason"] = "the previous slice's process group was reaped by this loop"
        return result
    if not isinstance(pgid, int):
        result["alive"] = False
        result["safe_to_resume"] = True
        result["reason"] = "the checkpoint records no process group to wait for"
        return result
    try:
        os.killpg(pgid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        # It exists and belongs to somebody else; treat as held, never steal.
        alive = True
    except (OverflowError, ValueError):
        alive = False
    result["alive"] = alive
    result["safe_to_resume"] = not alive
    result["reason"] = (
        f"process group {pgid} is still running and holds the session"
        if alive else
        f"process group {pgid} is gone"
    )
    return result


# ---------------------------------------------------------------------------
# running one slice
# ---------------------------------------------------------------------------

def run_slice(
    *, model_runner: Path, mode: str, prompt_path: Path, events_path: Path,
    last_message_path: Path, verdict_schema: Path, budget_seconds: int,
    resume_session_id: str | None, environment: dict[str, str],
    stdout_path: Path, stderr_path: Path,
) -> dict[str, Any]:
    """Run one bounded slice, keeping whatever it produced when the budget ends.

    The child gets its own process group so a budget kill reaps the whole tree.
    openai/codex#7852 documents codex children surviving their parent, and an
    orphan that keeps running would both spend tokens outside the budget and
    hold the session against the next tick's resume.

    The event stream is written straight to `events_path` by the runner rather
    than piped, so a `SIGKILL` still leaves the flushed events on disk.  That is
    the measured behaviour that makes the checkpoint possible at all.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)
    child_environment = dict(environment)
    child_environment["ARTICLE_CODEX_EVENTS_FILE"] = str(events_path)
    child_environment["ARTICLE_CODEX_LAST_MESSAGE_FILE"] = str(last_message_path)
    child_environment["ARTICLE_CODEX_OUTPUT_SCHEMA"] = str(verdict_schema)
    if resume_session_id:
        child_environment["ARTICLE_CODEX_RESUME_SESSION_ID"] = resume_session_id
    else:
        child_environment.pop("ARTICLE_CODEX_RESUME_SESSION_ID", None)

    started = time.monotonic()
    result: dict[str, Any] = {
        "events_path": str(events_path),
        "last_message_path": str(last_message_path),
        "resume_session_id": resume_session_id,
    }
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        process = subprocess.Popen(
            [str(model_runner), mode, "--prompt-file", str(prompt_path)],
            env=child_environment, stdin=subprocess.DEVNULL,
            stdout=out, stderr=err, start_new_session=True,
        )
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:  # pragma: no cover - exited immediately
            pgid = process.pid
        result["pgid"] = pgid
        try:
            process.wait(timeout=budget_seconds)
            result["killed_at_budget"] = False
            result["reaped"] = True
        except subprocess.TimeoutExpired:
            # Budget reached.  Stop the spend, keep what landed on disk.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            process.wait()
            result["killed_at_budget"] = True
            result["reaped"] = True
    result["return_code"] = process.returncode
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    result["stderr_excerpt"] = _tail(stderr_path)
    return result


def _tail(path: Path, limit: int = 500) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()[-limit:]


# ---------------------------------------------------------------------------
# checkpoint record
# ---------------------------------------------------------------------------

def build_checkpoint(
    *, previous: dict[str, Any] | None, fingerprint: str, status: str,
    slice_count: int, max_slices: int, trigger: Any, observed_at: str,
    lease_id: str, session_id: str | None, run: dict[str, Any],
    stream: dict[str, Any], verdict: dict[str, Any] | None,
) -> dict[str, Any]:
    slices = list((previous or {}).get("slices") or [])
    slices.append({
        "observed_at": observed_at,
        "lease_id": lease_id,
        "status": status,
        "deciding_event": stream.get("deciding_event"),
        "resumed_session_id": run.get("resume_session_id"),
        "killed_at_budget": run.get("killed_at_budget"),
        "return_code": run.get("return_code"),
        "latency_ms": run.get("latency_ms"),
        "events_path": run.get("events_path"),
        "event_count": stream.get("event_count"),
    })
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "fingerprint": fingerprint,
        "status": status,
        "slice_count": slice_count,
        "max_slices": max_slices,
        "trigger": trigger,
        "updated_at": observed_at,
        "session": {
            "id": session_id,
            "codex_home": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        },
        "process": {
            "pgid": run.get("pgid"),
            "reaped": run.get("reaped") is True,
            "killed_at_budget": run.get("killed_at_budget") is True,
        },
        "partial_findings": {
            "agent_messages": stream.get("agent_messages") or [],
            "advisory_errors": stream.get("advisory_errors") or [],
        },
        "verdict": verdict,
        "slices": slices,
    }


def save_checkpoint(
    state_root: Path, fingerprint: str, checkpoint: dict[str, Any],
) -> Path:
    return _atomic(checkpoint_path(state_root, fingerprint), checkpoint)
