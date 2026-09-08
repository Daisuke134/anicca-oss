#!/usr/bin/env python3
"""Recover verified paid remote answers through existing delivery boundaries."""
from __future__ import annotations
import argparse, errno, fcntl, hashlib, json, mimetypes, os, re, shutil, signal, stat, subprocess, sys, tempfile, threading, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))
import delivery_project  # noqa: E402
import delivery_queue  # noqa: E402
import paid_admission  # noqa: E402
import paid_work_evidence  # noqa: E402
import paid_remote_result  # noqa: E402
import reconcile_paid_delivery  # noqa: E402
import step_result_status  # noqa: E402
import project_ledger  # noqa: E402
import project_janitor  # noqa: E402
from private_data_boundary import redact_prompt_text, restricted_attachment_paths  # noqa: E402
from telegram_outbox import TelegramOutbox, dispatch_one  # noqa: E402
from telegram_report import OpenClawTelegramTransport  # noqa: E402
from gig_paths import BROWSER_DIR, REPO_ROOT, RUNNER_DIR  # noqa: E402
from gig_disk_guard import disk_headroom_ok  # noqa: E402

DEFAULT_STEP_TIMEOUT_SECONDS = 2100
TARGETED_READBACK_TIMEOUT_SECONDS = 180
TERMINAL_RECONCILIATION_TIMEOUT_SECONDS = 90
TERMINAL_RECONCILIATION_CLEANUP_TIMEOUT_SECONDS = 15
DM_CONTEXT_TIMEOUT_SECONDS = 180
# One prepare child owns up to three 60-minute production rounds plus three 30-minute reviews.
# Its outer deadline must not expire before those already-bounded inner steps can settle.
FILE_PREPARE_TIMEOUT_SECONDS = 21600
# Runtime state location is machine configuration, not source. The plist supplies
# GIG_OPERATOR_BRAKE_FILE; this is only the fallback for a checkout that has none.
DEFAULT_BRAKE = Path(
    os.environ.get("GIG_OPERATOR_BRAKE_FILE")
    or Path.home() / "gig" / "state" / "paid.operator.brake"
)
DEFAULT_TELEGRAM_DATABASE = Path.home() / "gig" / "telegram-outbox.sqlite3"
DEFAULT_TELEGRAM_RECEIPTS = Path.home() / "gig" / "telegram-delivery-receipts"


def _operator_denied_paths() -> list[str]:
    """Extra directories the sandboxed builder must not read on this machine.

    The sandbox profiles allow by default and name what is forbidden, so anything
    unnamed is readable. The release tree and the order workspace are derivable and
    always denied; a second checkout or another loop's state directory is not -- only
    the operator knows those exist. GIG_SANDBOX_DENY is how they say so, colon
    separated, and an empty setting simply means there is nothing else to hide.
    """
    raw = os.environ.get("GIG_SANDBOX_DENY", "")
    denied = []
    for part in raw.split(":"):
        if not part.strip():
            continue
        path = Path(part.strip()).expanduser()
        # A relative entry compiles fine and matches nothing, so a typo would read as
        # protection while protecting nothing. Refuse the pass instead of pretending.
        if not path.is_absolute():
            raise ValueError(f"GIG_SANDBOX_DENY needs absolute paths, got {part.strip()!r}")
        denied.append(str(path))
    return denied


def _deny_reads(paths: list[str]) -> str:
    return "".join(f"(deny file-read* (subpath {json.dumps(path)}))\n" for path in paths)


def _private_model_runner(root: Path, command: list[str], label: str) -> list[str]:
    """Run a project-root model with buyer credential files unreadable at the OS boundary."""
    restricted = [str(path) for path in restricted_attachment_paths(root)]
    if not restricted:
        return command
    read_only = "--read-only" in command
    if read_only:
        # macOS rejects a second Seatbelt profile inside sandbox-exec. Keep the
        # outer privacy boundary and make it enforce the decision owner's write
        # boundary too; agent-runner may still write its evidence directory.
        command = [part for part in command if part != "--read-only"]
    write_denies = []
    if read_only:
        write_denies = [str(path) for path in root.iterdir() if path.name != "evidence"]
    profile = root / "context" / f".{label}-private-data.sb"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "(version 1)\n(allow default)\n"
        + _deny_reads(restricted)
        + "".join(f"(deny file-write* (subpath {json.dumps(path)}))\n" for path in write_denies),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    return ["/usr/bin/sandbox-exec", "-f", str(profile), *command]


def _run_private_model_serialized(root: Path, command: list[str], label: str, step: str,
                                  *, effect_owner: bool = False) -> str:
    """Admit one paid model run at a time, before its runner timeout starts."""
    effect_descriptor = None
    if effect_owner:
        effect_lock_path = root / ".paid-effect-owner.lock"
        effect_descriptor = os.open(effect_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(effect_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(effect_descriptor)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise Failure("remote_owner_busy") from error
            raise
    model_lock_path = root / ".paid-model-admission.lock"
    model_descriptor = os.open(model_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(model_descriptor, fcntl.LOCK_EX)
        return _run(_private_model_runner(root, command, label), step)
    finally:
        fcntl.flock(model_descriptor, fcntl.LOCK_UN)
        os.close(model_descriptor)
        if effect_descriptor is not None:
            fcntl.flock(effect_descriptor, fcntl.LOCK_UN)
            os.close(effect_descriptor)
PAID_DECISION_SCHEMA_VERSION = 4
PAID_DECISION_PROMPT_VERSION = "paid-semantic-decision-v19"
PAID_DECISION_MODEL = "gpt-5.6-terra"
PAID_FILE_MODEL = "gpt-5.6-terra"
PAID_RUNNER_CANDIDATES = {
    ("codex", "gpt-5.6-terra"),
    ("codex", "gpt-5.6-sol"),
    ("codex", "gpt-5.6-luna"),
    ("claude", "claude-sonnet-5"),
    ("claude-direct", "claude-sonnet-5"),
}
PAID_FILE_POLICY_VERSION = "paid-file-build-review-v21"
MAX_FILE_REVIEW_ITERATIONS = 1
PAID_REMOTE_WAIT_RECHECK_SECONDS = 3600
PAID_MAX_PARALLEL_PROJECTS = 8
PAID_MAX_PARALLEL_READBACKS = PAID_MAX_PARALLEL_PROJECTS
PAID_TERMINAL_RECONCILES_PER_WAKE = 1
MANUAL_ONLY_TALKROOM_IDS = frozenset()
PAID_SOURCE_CENSUS_VERSION = "paid-source-census-v4"
# The skills a paid order may be built with. A skill the lane cannot see is a skill it will
# reimplement badly under time pressure, so the BUYMA and video contracts belong here now that both
# are proven against real pages and real renders.
PAID_SOURCE_CENSUS_SKILLS = ("music-score-omr", "buyma-work", "ai-video-work")
PAID_FILE_OPERATOR_POLICY = "paid-file-operator-policy.json"
PAID_DECISION_FIELDS = frozenset((
    "decision", "mode", "feedback_sha256", "requirements_sha256",
    "latest_message_identity", "required_output", "required_effect", "required_assets", "delivery_stage",
    "formal_approval_evidence", "unresolved",
))

class Failure(RuntimeError):
    def __init__(self, step: str, detail: str = ""):
        super().__init__(detail or step)
        self.step = step
        self.detail = detail

def _load(path: Path) -> Any: return json.loads(path.read_text(encoding="utf-8"))

def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _has_resumption_marker(workspace: Path) -> bool:
    """Keep a failed owner workspace only when its continuation marker is valid."""
    markers = (
        (workspace / "delivery" / "paid-tool-requests.json", "requests"),
        (workspace / "context" / "paid-tool-results.json", "results"),
    )
    for path, field in markers:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = _load(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("version") == 1 and isinstance(value.get(field), list):
            return True
    return False


def _has_runner_diagnostic(workspace: Path) -> bool:
    return any(path.is_file() and not path.is_symlink()
               for path in workspace.glob("**/evidence/**/*"))


@contextmanager
def _project_workspace(root: Path, prefix: str, *, resume: bool = False) -> Iterator[str]:
    """Keep active Paid work outside shared temp and resume it after abrupt worker death."""
    runtime = root.parent.parent / "runtime" / root.name
    runtime.mkdir(parents=True, exist_ok=True)
    candidates = ([path for path in runtime.glob(f"{prefix}*")
                   if resume and path.is_dir() and not path.is_symlink()]
                  if resume else [])
    workspace = max(candidates, key=lambda path: path.stat().st_mtime_ns, default=None)
    if workspace is None:
        workspace = Path(tempfile.mkdtemp(prefix=prefix, dir=runtime))
    workspace.resolve().relative_to(runtime.resolve())
    completed = False
    try:
        yield str(workspace)
        completed = True
    finally:
        if completed or not (_has_resumption_marker(workspace) or _has_runner_diagnostic(workspace)):
            shutil.rmtree(workspace, ignore_errors=True)

def _text(value: Any) -> str: return str(value or "").strip()

def _runner_loop_id() -> str: return _text(os.environ.get("LIFE_MANAGER_LOOP_ID")) or "gig"

def _comparison_key(value: str) -> str: return " ".join(value.split())

STEP_TIMEOUT_RETURNCODE = 124


def _run_bounded(command: list[str], *, env=None, timeout: float | None = None):
    """A wedged child must become a failed step, not an unbounded wait.

    The runner already budgets itself, but nothing enforced that from out here: one child that
    never returned held the whole lane for an hour, sleeping, with no output written.
    """
    stdout_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    process = subprocess.Popen(
        command, stdout=stdout_file, stderr=stderr_file, text=True, env=env,
        start_new_session=os.name == "posix",
    )

    def captured() -> tuple[str, str]:
        stdout_file.flush(); stderr_file.flush()
        stdout_file.seek(0); stderr_file.seek(0)
        return stdout_file.read(), stderr_file.read()

    def terminate() -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            process.kill()

    previous: dict[int, Any] = {}
    main_thread = threading.current_thread() is threading.main_thread()
    if main_thread and os.name == "posix":
        def forward(signum: int, _frame: Any) -> None:
            terminate()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
    effective_timeout = timeout or DEFAULT_STEP_TIMEOUT_SECONDS
    try:
        process.wait(timeout=effective_timeout)
        stdout, stderr = captured()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as error:
        terminate()
        stdout, stderr = captured()
        return subprocess.CompletedProcess(
            command, STEP_TIMEOUT_RETURNCODE, stdout,
            stderr + f"\nstep timed out after {error.timeout}s")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        stdout_file.close(); stderr_file.close()


def _run(command: list[str], step: str, timeout: float | None = None,
         env: dict[str, str] | None = None) -> str:
    result = _run_bounded(command, timeout=timeout, env=env)
    if result.returncode:
        tail = redact_prompt_text((result.stderr or result.stdout or "")[-2000:]).strip()
        raise Failure(step, tail or f"subprocess_returncode={result.returncode}")
    return result.stdout

def _json_line(stdout: str, step: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try: value = json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(value, dict): return value
    raise Failure(step)

def _collector(args, mode, output, evidence, item_path=None, item=None):
    command = [sys.executable, str(args.collector), "--output", str(output), "--evidence-dir", str(evidence),
               "--mode", mode, "--projects-root", str(args.projects_root), "--cdp-helper", str(args.cdp_helper)]
    if mode == "orders-only":
        command.append("--visible-with-screenshot")
    if mode == "selected-talkroom-only":
        room = _text((item or {}).get("talkroom_id"))
        if not item_path or not re.fullmatch(r"[0-9]+", room): raise Failure("remote_resume")
        command += ["--talkroom-id", room, "--project-id", delivery_project.resolve_project_root(args.projects_root, item).name,
                    "--selected-order-input", str(item_path), "--visible-with-screenshot"]
    return command

def _collect_dm_context(args, item: dict[str, Any], root: Path, base: Path) -> None:
    """Bind the existing pre-purchase DM collector before any semantic paid work."""
    evidence = base / "preflight" / "direct-message.json"
    required_sources = (
        root / "source" / "proposal" / f"offer-{_text(item.get('talkroom_id'))}.json",
        root / "source" / "talkroom" / "messages.jsonl",
        root / "requirements" / "live-buyer-reply.json",
    )
    try:
        previous = _load(evidence)
        recent_unavailable = (
            previous.get("error") in {
                "dm_thread_not_found", "dm_thread_unknown", "dm_collection_unavailable",
            }
            and time.time() - evidence.stat().st_mtime < 3600
        )
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        recent_unavailable = False
    if recent_unavailable and all(path.is_file() and not path.is_symlink() for path in required_sources):
        return
    thread_id = ""
    try:
        proposal = _load(root / "source" / "proposal" / f"offer-{_text(item.get('talkroom_id'))}.json")
        matched = re.fullmatch(r"/mypage/direct_message/([0-9]{1,32})", _text(proposal.get("direct_message_reference")))
        thread_id = matched.group(1) if matched else ""
    except (OSError, TypeError, json.JSONDecodeError):
        pass
    command = [sys.executable, str(args.dm_collector), "--project-root", str(root),
               "--buyer", _text(item.get("buyer")), "--observed-at", datetime.now().astimezone().isoformat(),
               "--cdp-helper", str(args.cdp_helper), "--evidence-output", str(evidence)]
    if thread_id:
        command += ["--thread-id", thread_id]
    result = _run_bounded(command, timeout=DM_CONTEXT_TIMEOUT_SECONDS)
    try:
        receipt = _json_line(result.stdout, "dm_context")
    except Failure:
        receipt = {
            "ok": False,
            "error": "dm_collection_unavailable",
            "returncode": result.returncode,
            "talkroom_id": _text(item.get("talkroom_id")),
        }
        _write(evidence, receipt)
    if receipt.get("ok") is True:
        return
    # A verified full-inbox miss is the explicit no-pre-purchase-DM state. Browser,
    # parser, identity and attachment failures must not silently compile partial context.
    if receipt.get("error") not in {
        "dm_thread_not_found", "dm_thread_unknown", "dm_collection_unavailable",
    }:
        raise Failure("dm_context")
    # A missing DM must remain a visible context gap, but it cannot suppress work already
    # fully specified by the authenticated proposal, talkroom and accumulated requirements.
    for required in required_sources:
        if not required.is_file() or required.is_symlink():
            raise Failure("dm_context")


def _run_paid_preflight(args, command: list[str]) -> str:
    lock_path = args.cdp_lock_dir.parent / ".paid-preflight-browser.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _run(command, "remote_resume")
        except Failure as error:
            if not any(transient in error.detail for transient in (
                "collector_unhealthy:talkroom_history_empty",
                "authenticated tab did not finish navigation",
            )):
                raise
            return _run(command, "remote_resume")

def _row(snapshot: dict[str, Any], room: str) -> dict[str, Any]:
    for value in snapshot.get("orders", []):
        if isinstance(value, dict) and _text(value.get("talkroom_id")) == room:
            talkroom = snapshot.get("talkroom")
            return {**talkroom, **value} if isinstance(talkroom, dict) else value
    talkroom = snapshot.get("talkroom")
    if isinstance(talkroom, dict) and _text(talkroom.get("talkroom_id")) == room: return talkroom
    raise Failure("remote_resume")

def _seller_last(row: dict[str, Any]) -> str:
    values = [row.get(key) for key in ("seller_last_message", "latest_seller_message", "last_seller_message")]
    values += row.get("seller_sent_messages") or row.get("seller_messages") or []
    for value in reversed(values):
        value = value.get("text") if isinstance(value, dict) else value
        if isinstance(value, str) and value.strip(): return value.strip()
    return ""


def _seller_last_sha256(row: dict[str, Any]) -> str:
    values = row.get("seller_sent_messages") or row.get("seller_messages") or []
    for value in reversed(values):
        if isinstance(value, dict) and re.fullmatch(r"[0-9a-f]{64}", _text(value.get("text_sha256"))):
            return _text(value.get("text_sha256"))
    message = _seller_last(row)
    return hashlib.sha256(_comparison_key(message).encode()).hexdigest() if message else ""


def _seller_message_with_attachment(row: dict[str, Any], message: str, filename: str) -> bool:
    expected = _comparison_key(message)
    expected_sha256 = hashlib.sha256(expected.encode()).hexdigest()
    for value in reversed(row.get("seller_sent_messages") or row.get("seller_messages") or []):
        if (isinstance(value, dict)
                and (_text(value.get("text_sha256")) == expected_sha256
                     or _comparison_key(_text(value.get("text"))) == expected)
                and filename in (value.get("attachments") or [])):
            return True
    return False


def _validated_customer_attachment(root: Path, value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"path", "filename", "sha256"}:
        raise ValueError("invalid customer attachment")
    raw = Path(_text(value.get("path")))
    filename, digest = _text(value.get("filename")), _text(value.get("sha256"))
    if (raw.is_symlink() or not _regular_file(raw) or Path(filename).name != filename
            or raw.name != filename or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("invalid customer attachment")
    evidence_raw = root / "evidence"
    if evidence_raw.is_symlink() or not evidence_raw.is_dir():
        raise ValueError("invalid customer attachment root")
    path = raw.resolve(); evidence = evidence_raw.resolve()
    path.relative_to(evidence)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("customer attachment hash mismatch")
    return {"path": str(path), "filename": filename, "sha256": digest}


def _answer_cycle_may_close(item: dict[str, Any]) -> bool:
    return not (
        item.get("buyer_feedback_pending_artifact") is True
        and item.get("buyer_visible_artifact_observed") is not True
    )


def _reported_remote_cycle(args, item: dict[str, Any]) -> Path | None:
    """Recognize a verified remote cycle already reported in the official room."""
    try:
        root = _paid_project_root(args, item)
        feedback = _text(item.get("buyer_feedback_sha256"))
        observed = item
        evidence_path = Path(_text(item.get("talkroom_evidence_file")))
        if _regular_file(evidence_path) and not evidence_path.is_symlink():
            live = _load(evidence_path)
            if _text(live.get("talkroom_id")) == _text(item.get("talkroom_id")):
                observed = {**item, **live}
        answer_path = root / "delivery" / "paid-answer.json"
        answer = _load(answer_path)
        intent = _load(root / "delivery" / "paid-remote-intent.json")
        result_path = root / "delivery" / "paid-remote-result.json"
        try:
            current_decision = _current_paid_decision(root, item)
        except (AttributeError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
            current_decision = None
        new_non_answer_work = (isinstance(current_decision, dict)
                               and current_decision.get("decision") == "actionable"
                               and current_decision.get("mode") != "answer")
        if (isinstance(current_decision, dict)
                and current_decision.get("decision") == "actionable"
                and current_decision.get("mode") == "answer"
                and intent.get("mode") not in {"answer", "consultation_answer"}):
            return None
        # Sending the answer changes the compiled context and intentionally makes the prior
        # semantic decision stale. Replay recognition therefore binds the signed answer intent
        # directly to the unchanged buyer feedback and official seller-last readback; requiring
        # the old decision to remain current makes every successful answer look actionable again.
        if (intent.get("mode") in {"answer", "consultation_answer"}
                and not new_non_answer_work and _answer_cycle_may_close(observed)):
            if _operator_policy_newer_than(root, item, answer_path):
                return None
            _validate_consultation_authorization(root, feedback)
            message = _text(answer.get("message"))
            formal = observed.get("formal_delivery_observed", observed.get("formal_delivery_confirmed"))
            if (message and _seller_last_sha256(observed) == hashlib.sha256(_comparison_key(message).encode()).hexdigest()
                    and formal is False
                    and _text(observed.get("talkroom_state", observed.get("transaction_state")))):
                return root
            return None
        if _operator_policy_newer_than(root, item, result_path):
            return None
        result = _load(result_path)
        message = _text(result.get("customer_message"))
        if result.get("status") != "completed" and not message:
            message = _text(answer.get("message"))
        attachment = _validated_customer_attachment(root, result.get("customer_attachment"))
        seller_match = (_seller_message_with_attachment(observed, message, attachment["filename"])
                        if attachment else
                        _seller_last_sha256(observed) == hashlib.sha256(_comparison_key(message).encode()).hexdigest())
        formal = observed.get("formal_delivery_observed", observed.get("formal_delivery_confirmed"))
        outcome = result.get("business_outcome")
        terminal = (
            result.get("status") == "ok" and result.get("verified_after") is True
        ) or (
            result.get("status") == "completed"
            and isinstance(outcome, dict)
            and outcome.get("required_effect_satisfied") is True
            and outcome.get("required_output_satisfied") is True
            and outcome.get("remaining_work") == []
        )
        if (terminal
                and result.get("buyer_feedback_sha256") == feedback
                and message and seller_match
                and formal is False
                and _text(observed.get("talkroom_state", observed.get("transaction_state")))):
            return root
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
        pass
    return None


def _reported_file_progress_cycle(args, item: dict[str, Any]) -> Path | None:
    """Recognize a reconciled review-stage file send without resending it."""
    try:
        root = _paid_project_root(args, item)
        manifest, snapshots = _file_bundle_snapshots(root, validate_source_census=False)
        receipt = _load(root / "context" / "paid-file-authorization.json")
        feedback = _text(item.get("buyer_feedback_sha256")) or _text(receipt.get("buyer_feedback_sha256"))
        requirements_sha256 = paid_remote_result.requirements_digest(root, feedback)
        review_state = _load(root / "context" / "paid-review-state.json")
        if (isinstance(review_state, dict)
                and review_state.get("state") == "REPAIR_PENDING"
                and review_state.get("buyer_feedback_sha256") == feedback
                and review_state.get("requirements_sha256") == requirements_sha256):
            return None
        if receipt.get("version") == 2:
            stable = delivery_queue.evidence_path(args.delivery_evidence_dir, item)
            _validate_file_authorization(root, stable, feedback, requirements_sha256)
        else:
            # Migration-only reconciliation: an already buyer-visible v1 send may
            # be recognized, but this receipt is never accepted by the writer.
            owner = root / "evidence" / "agent-PAID_FILE_OWNER"
            summary = _runner_summary(owner)
            result_path = _consultation_result_path(owner, summary)
            result = _load(result_path)
            if (not isinstance(receipt, dict) or receipt.get("version") != 1
                    or receipt.get("buyer_feedback_sha256") != feedback
                    or receipt.get("requirements_sha256") != requirements_sha256
                    or receipt.get("manifest_sha256") != snapshots["manifest"][1]
                    or receipt.get("artifact_sha256") != snapshots["artifact"][1]
                    or receipt.get("acceptance_sha256") != snapshots["acceptance"][1]
                    or receipt.get("owner_summary_sha256") != hashlib.sha256((owner / "summary.json").read_bytes()).hexdigest()
                    or receipt.get("owner_result_sha256") != hashlib.sha256(result_path.read_bytes()).hexdigest()
                    or result.get("status") != "ok"):
                return None
        state = _load(root / "state.json")
        artifact = Path(_text(manifest.get("artifact_path"))).resolve()
        artifact.relative_to(root.resolve())
        messages = item.get("seller_sent_messages") or item.get("seller_messages") or []
        latest = messages[-1] if isinstance(messages, list) and messages else None
        if (not isinstance(latest, dict) or artifact.name not in (latest.get("attachments") or [])
                or item.get("buyer_reply_after_artifact_observed") is True
                or item.get("formal_delivery_observed", item.get("formal_delivery_confirmed")) is not False
                or not _text(item.get("talkroom_state", item.get("transaction_state")))
                or state.get("handled_buyer_feedback_sha256") != feedback
                or state.get("delivery_confirmed_feedback_sha256") != feedback
                or state.get("current_version") != manifest.get("artifact_version")
                or state.get("latest_buyer_visible_version") != manifest.get("artifact_version")
                or state.get("current_package_sha256") != manifest.get("package_sha256")
                or state.get("work_state") != "DELIVERED"
                or state.get("next_action") != "await_buyer_feedback"
                or not _regular_file(artifact)
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != manifest.get("package_sha256")):
            return None
        return root
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
        return None


def _reported_handoff_cycle(args, item: dict[str, Any]) -> Path | None:
    """Trust an explicit out-of-loop handoff only while live seller-last still matches."""
    try:
        root = _paid_project_root(args, item)
        receipt_path = root / "delivery" / "paid-external-handoff.json"
        if receipt_path.is_symlink() or not _regular_file(receipt_path):
            return None
        receipt = _load(receipt_path)
        room = _text(item.get("talkroom_id"))
        feedback = _text(item.get("buyer_feedback_sha256"))
        message_sha256 = _text(receipt.get("message_sha256"))
        if (receipt.get("version") != 1 or receipt.get("status") != "awaiting_buyer"
                or _text(receipt.get("talkroom_id")) != room
                or _text(receipt.get("buyer_feedback_sha256")) != feedback
                or not re.fullmatch(r"[0-9a-f]{64}", message_sha256)
                or _seller_last_sha256(item) != message_sha256
                or item.get("formal_delivery_observed", item.get("formal_delivery_confirmed")) is not False
                or not _text(item.get("talkroom_state", item.get("transaction_state")))):
            return None
        evidence_root = (root / "evidence").resolve()
        official = Path(_text(receipt.get("official_readback"))).resolve()
        official.relative_to(evidence_root)
        if (official.is_symlink() or not _regular_file(official)
                or hashlib.sha256(official.read_bytes()).hexdigest()
                != _text(receipt.get("official_readback_sha256"))):
            return None
        official_row = _row(_load(official), room)
        if (_text(official_row.get("buyer_feedback_sha256")) != feedback
                or _seller_last_sha256(official_row) != message_sha256
                or official_row.get("formal_delivery_observed",
                                    official_row.get("formal_delivery_confirmed")) is not False):
            return None
        work_evidence = receipt.get("work_evidence")
        if not isinstance(work_evidence, list):
            return None
        for proof in work_evidence:
            if not isinstance(proof, dict) or set(proof) != {"path", "sha256"}:
                return None
            path = Path(_text(proof.get("path"))).resolve()
            path.relative_to(evidence_root)
            if (path.is_symlink() or not _regular_file(path)
                    or hashlib.sha256(path.read_bytes()).hexdigest() != _text(proof.get("sha256"))):
                return None
        return root
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
        return None

def _reported_formal_cycle(args, item: dict[str, Any]) -> Path | None:
    """Close only an official formal delivery backed by the same project artifact."""
    try:
        state = _text(item.get("talkroom_state", item.get("transaction_state")))
        if state not in {"納品確認待ち", "取引完了"}:
            return None
        talkroom_identity = {key: value for key, value in item.items() if key not in {"request_id", "project_id"}}
        candidate = delivery_project.resolve_project_root(args.projects_root, talkroom_identity)
        if candidate.is_symlink():
            return None
        root = candidate.resolve()
        root.relative_to(args.projects_root.resolve())
        if not root.is_dir():
            return None
        project_state = _load(root / "state.json")
        formal = item.get("formal_delivery_observed", item.get("formal_delivery_confirmed"))
        if formal is not True and project_state.get("formal_delivery_confirmed") is not True:
            return None
        if (item.get("buyer_feedback_pending_artifact") is True
                and _text(project_state.get("handled_buyer_feedback_sha256"))
                != _text(item.get("buyer_feedback_sha256"))):
            return None
        attachments: set[str] = set()
        seller_texts: set[str] = set()
        for message in item.get("seller_sent_messages") or item.get("seller_messages") or []:
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("text"), str) and message["text"]:
                seller_texts.add(message["text"])
            attachments.update(value for value in message.get("attachments") or []
                               if isinstance(value, str) and value and Path(value).name == value)
        project_ids = {root.name}
        project_ids.update(_text(item.get(key)) for key in ("request_id", "project_id"))
        project_ids.discard("")
        ledgers = [root / "events.jsonl", *root.rglob("formal-delivery-ledger.jsonl")]
        for ledger in ledgers:
            if ledger.is_symlink() or not _regular_file(ledger):
                continue
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try: record = json.loads(line)
                except json.JSONDecodeError: continue
                if (not isinstance(record, dict) or record.get("event") != "FORMAL_DELIVERY_CONFIRMED"
                        or _text(record.get("project_id")) not in project_ids
                        or _text(record.get("talkroom_id")) != _text(item.get("talkroom_id"))):
                    continue
                artifact = Path(_text(record.get("artifact_path")))
                if artifact.is_symlink() or not _regular_file(artifact):
                    continue
                resolved = artifact.resolve()
                resolved.relative_to(root)
                content = resolved.read_bytes()
                artifact_bound = (
                    len(content) == record.get("artifact_bytes")
                    and hashlib.sha256(content).hexdigest() == record.get("artifact_sha256")
                )
                linked_readback = (
                    record.get("linked_asset_delivery") is True
                    and record.get("seller_attachment_readback") is None
                    and _text(record.get("seller_message_readback")) in seller_texts
                )
                attachment_readback = (
                    resolved.name == record.get("seller_attachment_readback")
                    and resolved.name in attachments
                )
                if artifact_bound and (linked_readback or attachment_readback):
                    return root
    except (OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
        pass
    return None

def observe_orders(args, evidence_dir) -> list[dict[str, Any]]:
    snapshot = evidence_dir / "orders-only-snapshot.json"
    command = _collector(args, "orders-only", snapshot, evidence_dir)
    try:
        _run(command, "orders_observation")
    except Failure as error:
        if not any(transient in error.detail for transient in (
            "collector_unhealthy:orders_missing_container",
            "authenticated tab did not finish navigation",
        )):
            raise
        _run(command, "orders_observation")
    try: queue = delivery_queue.build_preliminary(_load(snapshot), date.fromisoformat(args.today))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error: raise Failure("orders_observation") from error
    return [dict(item) for item in queue.get("items", []) if isinstance(item, dict)
            and _text(item.get("talkroom_id")) not in MANUAL_ONLY_TALKROOM_IDS
            and item.get("terminal") is not True
            and _text(item.get("talkroom_state")) not in {"取引完了", "completed", "closed", "terminal"}]


def _reconcile_absent_talkrooms(args, open_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Refresh a bounded, rotating set of known rooms missing from the open-order page."""
    receipt_path = args.evidence_dir / "terminal-reconciliation.json"
    try:
        previous = _load(receipt_path)
        checked_at = dict(previous.get("checked_at") or {}) if isinstance(previous, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        checked_at = {}
    open_rooms = {_text(item.get("talkroom_id")) for item in open_items}
    candidates: list[tuple[float, str, Path, dict[str, Any]]] = []
    for root in args.projects_root.iterdir() if args.projects_root.is_dir() else ():
        state_path = root / "state.json"
        try:
            state = _load(state_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        room = _text(state.get("talkroom_id")) if isinstance(state, dict) else ""
        if (not re.fullmatch(r"[0-9]+", room) or room in open_rooms
                or room in MANUAL_ONLY_TALKROOM_IDS):
            continue
        try:
            last_checked = float(checked_at.get(room, 0))
        except (TypeError, ValueError):
            last_checked = 0
        candidates.append((last_checked, room, root, state))

    results = []
    for _, room, root, state in sorted(candidates)[:PAID_TERMINAL_RECONCILES_PER_WAKE]:
        base = args.evidence_dir / "terminal-reconciliation" / room
        item_path, snapshot = base / "item.json", base / "snapshot.json"
        item = {
            "request_id": root.name,
            "talkroom_id": room,
            "talkroom_state": state.get("talkroom_state"),
            "buyer": state.get("buyer"),
        }
        _write(item_path, item)
        checked_at[room] = time.time()
        owner = f"paid-terminal-{room}"
        try:
            _run(
                _collector(args, "selected-talkroom-only", snapshot, base, item_path, item),
                "terminal_reconciliation", timeout=TERMINAL_RECONCILIATION_TIMEOUT_SECONDS,
                env=_fresh_child_env(args, owner=owner),
            )
            observed = {**item, **_row(_load(snapshot), room)}
            root = delivery_project.record_queue_selection(
                args.projects_root, observed, adapter="coconala",
            )
            state_path = root / "state.json"
            transaction_state = _text(observed.get("transaction_state") or observed.get("talkroom_state"))
            talkroom_state = _text(observed.get("talkroom_state"))
            terminal_receipt_written = False
            if transaction_state == project_janitor.COMPLETE_STATE and talkroom_state == project_janitor.COMPLETE_STATE:
                terminal_path = root / "project-terminal.json"
                _write(terminal_path, {
                    "version": 1,
                    "authority": "official_provider_readback",
                    "terminal": True,
                    "adapter": "coconala",
                    "project_id": root.name,
                    "talkroom_id": room,
                    "transaction_state": transaction_state,
                    "talkroom_state": talkroom_state,
                    "state_sha256": project_janitor._sha256(state_path),
                    "provider_snapshot_sha256": project_janitor._sha256(snapshot),
                    "observed_at": time.time(),
                })
                terminal_path.chmod(0o600)
                terminal_receipt_written = True
            results.append({"talkroom_id": room, "status": "observed",
                            "talkroom_state": observed.get("talkroom_state"),
                            "terminal_receipt_written": terminal_receipt_written})
        except (Failure, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            results.append({"talkroom_id": room, "status": "failed",
                            "failed_step": error.step if isinstance(error, Failure) else "state_update"})
        finally:
            try:
                subprocess.run(
                    [sys.executable, str(args.cdp_helper), "close-owned", "--owner", owner],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=TERMINAL_RECONCILIATION_CLEANUP_TIMEOUT_SECONDS,
                    check=False, env=_fresh_child_env(args, owner=owner),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    receipt = {"version": 1, "checked_at": checked_at, "results": results,
               "remaining_candidates": max(0, len(candidates) - len(results))}
    _write(receipt_path, receipt)
    return receipt

def _regular_file(path: Path) -> bool:
    try: return stat.S_ISREG(path.lstat().st_mode)
    except OSError: return False

def _runner_summary(managed: Path) -> dict[str, Any]:
    managed = managed.resolve()
    summary_path = managed / "summary.json"
    if not _regular_file(summary_path): raise ValueError("invalid verifier summary")
    summary = _load(summary_path)
    raw_result = summary.get("result_path") if isinstance(summary, dict) else None
    if not isinstance(raw_result, str) or not raw_result.strip(): raise ValueError("invalid verifier result path")
    result_candidate = managed / raw_result if not Path(raw_result).is_absolute() else Path(raw_result)
    if result_candidate.is_symlink() or not _regular_file(result_candidate): raise ValueError("invalid verifier result file")
    result_path = result_candidate.resolve()
    if result_path.parent != managed: raise ValueError("invalid verifier result file")
    return summary


def _official_content_sha256(row: dict[str, Any]) -> str:
    canonical = {
        "side": row.get("side"),
        "sent_at": row.get("sent_at"),
        "text": str(row.get("text") or ""),
        "attachments": row.get("attachments"),
    }
    return hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _official_message_rows(root: Path, talkroom_id: str) -> list[dict[str, Any]]:
    path = root / "source" / "talkroom" / "messages.jsonl"
    if path.is_symlink() or not _regular_file(path):
        raise Failure("paid_work_decision")
    try:
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            content = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in content.split("\n") if line.strip()]
        rows = [json.loads(line) for line in lines]
    except (OSError, json.JSONDecodeError) as error:
        raise Failure("paid_work_decision") from error
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise Failure("paid_work_decision")
    state = _load(root / "state.json")
    if _text(state.get("talkroom_id")) != talkroom_id:
        raise Failure("paid_work_decision")
    return rows


def _official_identity(row: dict[str, Any], talkroom_id: str) -> dict[str, str]:
    required = {"version", "source", "talkroom_id", "message_id", "observed_at",
                "content_sha256", "side", "sent_at", "text", "attachments"}
    if (set(row) < required or row.get("version") != 1
            or row.get("source") != "coconala_live_talkroom"
            or _text(row.get("talkroom_id")) != talkroom_id
            or not _text(row.get("message_id"))
            or not _text(row.get("observed_at"))
            or row.get("side") not in {"buyer", "seller", "system"}
            or not isinstance(row.get("text"), str)
            or not isinstance(row.get("attachments"), list)
            or not re.fullmatch(r"[0-9a-f]{64}", _text(row.get("content_sha256")))
            or _official_content_sha256(row) != row.get("content_sha256")):
        raise Failure("paid_work_decision")
    for attachment in row["attachments"]:
        if not isinstance(attachment, dict) or set(attachment) < {
                "filename", "content_type", "size_text", "href", "reference"}:
            raise Failure("paid_work_decision")
    return {"message_id": _text(row["message_id"]),
            "content_sha256": _text(row["content_sha256"]), "side": _text(row["side"])}


def _latest_official_identity(root: Path, talkroom_id: str) -> dict[str, str]:
    return _official_identity(_official_message_rows(root, talkroom_id)[-1], talkroom_id)


def _latest_official_buyer_identity(root: Path, talkroom_id: str) -> dict[str, str]:
    for row in reversed(_official_message_rows(root, talkroom_id)):
        if row.get("side") == "buyer":
            return _official_identity(row, talkroom_id)
    try:
        receipt = _load(root / "requirements" / "live-buyer-reply.json")
        feedback = _text(receipt.get("feedback_sha256"))
        identity = f"purchased-offer:{talkroom_id}"
        if (receipt.get("version") == 1
                and receipt.get("source") == "purchased_offer_before_first_buyer_message"
                and receipt.get("buyer_feedback_stage") == "initial_request"
                and _text(receipt.get("project_id")) == talkroom_id
                and _text(receipt.get("talkroom_id")) == talkroom_id
                and re.fullmatch(r"[0-9a-f]{64}", feedback)
                and _text(receipt.get("feedback_identity_sha256")) == feedback
                and receipt.get("feedback_message_identities") == [identity]):
            return {"message_id": identity, "content_sha256": feedback, "side": "buyer"}
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    raise Failure("paid_work_decision")


def _validate_paid_decision(value: dict[str, Any], feedback: str, requirements: str,
                            identity: dict[str, str],
                            buyer_identity: dict[str, str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PAID_DECISION_FIELDS:
        raise ValueError("invalid paid semantic decision")
    decision, mode = value.get("decision"), value.get("mode")
    if decision not in {"actionable", "await_buyer", "satisfied_noop", "blocked"}:
        raise ValueError("invalid paid semantic decision")
    if decision == "actionable" and mode not in {"file", "remote", "answer"}:
        raise ValueError("actionable decision requires mode")
    if decision != "actionable" and mode is not None:
        raise ValueError("non-actionable decision requires null mode")
    delivery_stage = value.get("delivery_stage")
    if decision == "actionable" and mode == "file" and delivery_stage not in {"formal", "review"}:
        raise ValueError("file decision requires formal or review delivery stage")
    if (decision != "actionable" or mode != "file") and delivery_stage != "none":
        raise ValueError("non-file decision requires none delivery stage")
    approval = value.get("formal_approval_evidence")
    if delivery_stage == "formal":
        current_buyer = buyer_identity or identity
        if current_buyer.get("side") != "buyer" or approval != current_buyer:
            raise ValueError("formal delivery requires current buyer approval evidence")
    elif approval is not None:
        raise ValueError("non-formal decision requires null approval evidence")
    if (not isinstance(value.get("feedback_sha256"), str)
            or not isinstance(value.get("requirements_sha256"), str)
            or not isinstance(value.get("required_output"), str)
            or not isinstance(value.get("required_effect"), str)
            or value.get("feedback_sha256") != feedback
            or value.get("requirements_sha256") != requirements
            or value.get("latest_message_identity") != identity
            or not _text(value.get("required_output"))
            or not _text(value.get("required_effect"))):
        raise ValueError("paid semantic decision identity mismatch")
    unresolved = value.get("unresolved")
    if not isinstance(unresolved, list) or any(not isinstance(item, str) for item in unresolved):
        raise ValueError("invalid paid semantic decision unresolved")
    required_assets = value.get("required_assets")
    if not isinstance(required_assets, list):
        raise ValueError("invalid paid semantic decision required assets")
    asset_fields = {"asset_id", "kind", "minimum_count", "buyer_visible_purpose",
                    "source_authority", "archive_required"}
    asset_ids: set[str] = set()
    for asset in required_assets:
        if (not isinstance(asset, dict) or set(asset) != asset_fields
                or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", _text(asset.get("asset_id")))
                or asset.get("asset_id") in asset_ids
                or asset.get("kind") not in {"screenshot", "image", "linked_asset"}
                or not isinstance(asset.get("minimum_count"), int)
                or isinstance(asset.get("minimum_count"), bool)
                or asset.get("minimum_count") < 1
                or not _text(asset.get("buyer_visible_purpose"))
                or asset.get("source_authority") not in {"builder", "buyer", "account_owner"}
                or not isinstance(asset.get("archive_required"), bool)):
            raise ValueError("invalid paid semantic decision required asset")
        asset_ids.add(asset["asset_id"])
    return value


def _decision_runner_proof(evidence: Path) -> dict[str, Any]:
    summary = _runner_summary(evidence)
    expected = {"status": "success", "task_label": "paid-work-decision",
                "task_class": "escalation-agent", "escalated": True}
    if any(summary.get(key) != value for key, value in expected.items()):
        raise Failure("paid_work_decision")
    provider_model = (summary.get("selected_provider"), summary.get("selected_model"))
    if provider_model not in PAID_RUNNER_CANDIDATES:
        raise Failure("paid_work_decision")
    result_path = _consultation_result_path(evidence, summary)
    summary_path = evidence / "summary.json"
    return {**expected, "selected_provider": provider_model[0], "selected_model": provider_model[1],
            "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest()}


def _file_snapshot(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not _regular_file(path):
        raise ValueError("bound input is not a regular file")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError("bound input cannot be read") from error
    return len(content), hashlib.sha256(content).hexdigest()


def _file_operator_policy(root: Path, feedback: str,
                          requirements_sha256: str) -> tuple[Path | None, dict[str, Any], str]:
    """Return one account-owner policy only when it is scoped to this exact cycle."""
    path = root / "context" / PAID_FILE_OPERATOR_POLICY
    if not path.exists():
        return None, {}, ""
    try:
        _size, digest = _file_snapshot(path)
        policy = _load(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise Failure("operator_policy") from error
    if not isinstance(policy, dict):
        raise Failure("operator_policy")
    directives = policy.get("directives")
    if (policy.get("version") != 1
            or policy.get("authorized_by") != "account_owner"
            or _text(policy.get("request_id")) != root.name
            or not isinstance(directives, list) or not directives
            or any(not isinstance(value, str) or not value.strip() for value in directives)):
        raise Failure("operator_policy")
    if (policy.get("buyer_feedback_sha256") != feedback
            or policy.get("requirements_sha256") != requirements_sha256):
        return None, {}, ""
    return path, policy, digest


def _file_review_images(root: Path, artifact_sha256: str, finding: str = "",
                        limit: int = 12) -> list[Path]:
    """Create artifact-bound visual inputs for the fresh native-vision reviewer."""
    resolved_root = root.resolve()
    frames: list[Path] = []
    for receipt_path in sorted((root / "work").glob("**/receipt.json")):
        try:
            receipt = _load(receipt_path)
            if receipt.get("output", {}).get("sha256") != artifact_sha256:
                continue
            raw = receipt.get("qc", {}).get("output", {}).get("review_frames", {}).get("path")
            directory = Path(_text(raw)).resolve()
            directory.relative_to(resolved_root)
            candidates = sorted(path for path in directory.glob("*.jpg")
                                if not path.is_symlink() and path.is_file())
        except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not candidates:
            continue
        cited: list[Path] = []
        for number in re.findall(r"(?:frame[- ]?)?(\d{4})", finding, flags=re.IGNORECASE):
            candidate = directory / f"frame-{number}.jpg"
            if candidate in candidates and candidate not in cited:
                cited.append(candidate)
        remaining = max(0, limit - len(cited))
        sampled: list[Path] = []
        if remaining == 1:
            sampled = [candidates[len(candidates) // 2]]
        elif remaining > 1:
            sampled = [candidates[round(index * (len(candidates) - 1) / (remaining - 1))]
                       for index in range(remaining)]
        frames = cited + [path for path in sampled if path not in cited]
        break
    if frames:
        return frames[:limit]

    try:
        manifest_path = root / "delivery" / "paid-work-result.json"
        manifest = _load(manifest_path)
        raw_artifact = Path(_text(manifest.get("artifact_path")))
        artifact = (resolved_root / raw_artifact if not raw_artifact.is_absolute()
                    else raw_artifact).resolve()
        artifact.relative_to(resolved_root)
        if (manifest.get("package_sha256") != artifact_sha256
                or _file_snapshot(artifact)[1] != artifact_sha256):
            return []
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return []

    suffix = artifact.suffix.casefold()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        review_dir = root / "evidence" / "controller-artifact-review" / artifact_sha256
        review_dir.mkdir(parents=True, exist_ok=True)
        _write(review_dir / "review-manifest.json", {
            "version": 1, "artifact_path": str(artifact), "artifact_sha256": artifact_sha256,
            "pages": [{"path": str(artifact), "sha256": artifact_sha256}],
        })
        return [artifact]
    if suffix == ".zip":
        review_dir = root / "evidence" / "controller-artifact-review" / artifact_sha256
        try:
            review_dir.mkdir(parents=True, exist_ok=True)
            for stale in review_dir.glob("candidate-*"):
                if not stale.is_symlink() and stale.is_file():
                    stale.unlink()
            with zipfile.ZipFile(artifact) as archive:
                names = [name for name in archive.namelist()
                         if Path(name).suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
                         and not name.endswith("/")]
                rendered = [name for name in names if "/png/" in f"/{name.casefold()}"
                            and "/assets/" not in f"/{name.casefold()}"]
                selected = (rendered or names)[:limit]
                pages = []
                for index, name in enumerate(selected, start=1):
                    target = review_dir / f"candidate-{index:02d}{Path(name).suffix.casefold()}"
                    target.write_bytes(archive.read(name))
                    pages.append(target)
            if not pages:
                return []
            _write(review_dir / "review-manifest.json", {
                "version": 1, "artifact_path": str(artifact),
                "artifact_sha256": artifact_sha256,
                "pages": [{"entry": name, "path": str(page), "sha256": _file_snapshot(page)[1]}
                          for name, page in zip(selected, pages)],
            })
            return pages
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            raise Failure("file_visual_evidence") from error
    if suffix != ".pdf":
        return []

    review_dir = root / "evidence" / "controller-artifact-review" / artifact_sha256
    try:
        review_dir.mkdir(parents=True, exist_ok=True)
        for stale in review_dir.glob("candidate-page-*.png"):
            if not stale.is_symlink() and stale.is_file():
                stale.unlink()
        prefix = review_dir / "candidate-page"
        _run(["pdftoppm", "-png", "-r", "150", str(artifact), str(prefix)],
             "file_visual_evidence")
        pages = sorted(
            review_dir.glob("candidate-page-*.png"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        )
        if not pages:
            raise Failure("file_visual_evidence")
        rows = [
            {"page": index, "path": str(page), "sha256": _file_snapshot(page)[1]}
            for index, page in enumerate(pages, start=1)
        ]
        _write(review_dir / "review-manifest.json", {
            "version": 1,
            "artifact_path": str(artifact),
            "artifact_sha256": artifact_sha256,
            "pages": rows,
        })
        return pages
    except Failure:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise Failure("file_visual_evidence") from error


def _prepare_blind_output_audit(
    args, root: Path, manifest: dict[str, Any], artifact_sha256: str,
    source_correspondence_sha256: str,
) -> tuple[Path | None, list[Path]]:
    """Blind-read owner-declared manual crops before the final reviewer sees expectations."""
    try:
        correspondence = _load(Path(_text(manifest.get("source_correspondence_path"))))
        exact = correspondence.get("exact_semantic_value_checks")
        if not exact:
            # A receipt that declares no manual checks has nothing to blind-audit, exactly
            # like one whose declared list comes back empty below. Reading through a missing
            # key instead raised AttributeError, which surfaced as file_visual_evidence and
            # killed every source-correspondence job on its first round.
            return None, []
        evidence_path = Path(_text(exact.get("label_evidence_path"))).resolve()
        evidence_path.relative_to(root.resolve())
        if exact.get("label_evidence_sha256") != _file_snapshot(evidence_path)[1]:
            raise ValueError("stale label evidence")
        evidence = _load(evidence_path)
        manual = [row for row in evidence.get("checks", [])
                  if _text(row.get("verification_mode")).startswith("manual")]
        if not manual:
            return None, []
        crops: list[tuple[str, Path, str, str, str]] = []
        for index, row in enumerate(manual, start=1):
            path = Path(_text(row.get("output_crop_path"))).resolve()
            path.relative_to(root.resolve())
            sha256 = _file_snapshot(path)[1]
            if sha256 != row.get("output_crop_sha256"):
                raise ValueError("stale output crop")
            crops.append((f"blind-{index:04d}", path, sha256,
                          _text(row.get("expected_label_from_controller")), _text(row.get("role"))))
    except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise Failure("file_visual_evidence") from error

    with _project_workspace(root, "paid-output-audit-") as temporary:
        isolated = Path(temporary)
        copied: list[Path] = []
        for blind_id, source, _sha256, _expected, _role in crops:
            target = isolated / "crops" / f"{blind_id}{source.suffix.casefold()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target)
        schema = isolated / "schema.json"
        _write(schema, {
            "type": "object", "additionalProperties": False,
            "required": ["status", "reads"],
            "properties": {
                "status": {"type": "string", "const": "ok"},
                "reads": {
                    "type": "array", "minItems": len(crops), "maxItems": len(crops),
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["id", "visible_label", "confidence"],
                        "properties": {
                            "id": {"type": "string"},
                            "visible_label": {"type": "string", "minLength": 1},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                    },
                },
            },
        })
        prompt = isolated / "prompt.txt"
        prompt.write_text(
            "You are a blind visual transcription reviewer. Work only in this isolated directory. "
            "The attached images correspond in order to IDs "
            f"{json.dumps([{'id': row[0], 'target_role': row[4]} for row in crops])}. Each target role names the "
            "colored note whose adjacent Japanese solfege label must be read; ignore labels belonging to other colored "
            "notes in the same crop. Read only that visible label glyph, including any flat sign. You are not given any "
            "expected value and must not search outside this directory. Return exactly one read per ID in the same order. "
            "Use confidence low rather than guessing when the glyph is unreadable.",
            encoding="utf-8",
        )
        runtime = isolated / "runtime"
        runtime.mkdir()
        runner_source = Path(args.agent_runner).resolve()
        isolated_runner = runtime / runner_source.name
        shutil.copyfile(runner_source, isolated_runner)
        for sibling in ("token_budget.py", "config.json"):
            source = runner_source.parent / sibling
            if source.is_file():
                shutil.copyfile(source, runtime / sibling)
        profile = isolated / "blind-only.sb"
        profile.write_text(
            "(version 1)\n(allow default)\n"
            f"(deny file-read* (subpath {json.dumps(str(root.resolve().parent))}))\n"
            f"(deny file-read* (subpath {json.dumps(str(REPO_ROOT))}))\n"
            + _deny_reads(_operator_denied_paths())
            + f"(deny file-write* (subpath {json.dumps(str(root.resolve().parent))}))\n",
            encoding="utf-8",
        )
        evidence_dir = isolated / "evidence"
        started = time.time_ns()
        command = [
            "/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, str(isolated_runner),
            "--task-class", "escalation-agent",
            "--prompt-file", str(prompt), "--schema", str(schema),
            "--evidence-dir", str(evidence_dir), "--task-label", "paid-output-blind-audit",
            "--loop", _runner_loop_id(), "--workdir", str(isolated), "--timeout-seconds", "1800", "--read-only",
            "--escalation-reason", "Blind output crop readback before paid submission",
        ]
        for image in copied:
            command += ["--image", str(image)]
        completed = _run_bounded(command)
        if completed.returncode:
            attempts = evidence_dir / "attempts.jsonl"
            attempt_stdout = evidence_dir / "attempt-01.stdout.log"
            attempt_stderr = evidence_dir / "attempt-01.stderr.log"
            _write(root / "context" / "paid-output-audit-runner-error.json", {
                "version": 1, "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-4000:], "stderr_tail": completed.stderr[-4000:],
                "attempts_tail": attempts.read_text(encoding="utf-8")[-8000:] if attempts.is_file() else "",
                "attempt_stdout_tail": attempt_stdout.read_text(encoding="utf-8")[-4000:] if attempt_stdout.is_file() else "",
                "attempt_stderr_tail": attempt_stderr.read_text(encoding="utf-8")[-4000:] if attempt_stderr.is_file() else "",
            })
            raise Failure("file_verifier")
        result, proof = _file_runner_result(
            evidence_dir, task_label="paid-output-blind-audit", started_ns=started,
        )
        reads = result.get("reads") if isinstance(result, dict) else None
        if (result.get("status") != "ok" or not isinstance(reads, list)
                or [row.get("id") for row in reads] != [row[0] for row in crops]):
            raise Failure("file_verifier")
        rows = [{
            "id": blind_id, "crop_path": str(path), "crop_sha256": sha256, "target_role": role,
            "blind_visible_label": _text(read.get("visible_label")),
            "confidence": read.get("confidence"), "expected_label": expected,
            "match": _text(read.get("visible_label")) == expected,
        } for (blind_id, path, sha256, expected, role), read in zip(crops, reads)]
        audit_path = root / "context" / "paid-output-visual-audit.json"
        _write(audit_path, {
            "version": 1, "policy_version": PAID_FILE_POLICY_VERSION,
            "artifact_sha256": artifact_sha256,
            "source_correspondence_sha256": source_correspondence_sha256,
            "label_evidence_path": str(evidence_path),
            "label_evidence_sha256": _file_snapshot(evidence_path)[1],
            "status": "PASS" if all(row["match"] for row in rows) else "FAIL",
            "reads": rows, **proof,
        })
    return audit_path, [row[1] for row in crops]


def _file_reference_images(root: Path, manifest: dict[str, Any], limit: int = 4) -> list[Path]:
    """Read explicit visual-reference paths from the artifact's bound acceptance evidence."""
    try:
        resolved_root = root.resolve()
        acceptance = Path(_text(manifest.get("acceptance_evidence_path"))).resolve()
        acceptance.relative_to(resolved_root)
        if acceptance.is_symlink() or not acceptance.is_file():
            return []
        value = _load(acceptance)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []

    raw_paths: list[str] = []
    reference_key = re.compile(r"(?:visual_)?reference(?:_image|_frame|_sheet)?_paths?$")

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str) and reference_key.fullmatch(key.casefold()):
                    values = child if isinstance(child, list) else [child]
                    raw_paths.extend(item for item in values if isinstance(item, str))
                else:
                    collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    collect(value)
    requirements = _load(root / "requirements" / "live-buyer-reply.json")
    for attachment in requirements.get("attachments", []) if isinstance(requirements, dict) else []:
        if isinstance(attachment, dict) and isinstance(attachment.get("source_path"), str):
            raw_paths.append(attachment["source_path"])
    images: list[Path] = []
    for raw in raw_paths:
        try:
            path = Path(raw)
            path = (resolved_root / path if not path.is_absolute() else path).resolve()
            path.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if (path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
                and not path.is_symlink() and path.is_file() and path not in images):
            images.append(path)
        if len(images) >= limit:
            break
    return images


def _file_review_disposition(verdict: Any) -> str:
    if verdict == "deliverable":
        return "approve"
    if verdict == "needs_revision":
        return "repair"
    return "block"


def _review_ready_may_ship(verdict: Any, review_ready_allowed: bool, review_round: int) -> bool:
    return (
        review_ready_allowed
        and verdict == "undeterminable"
        and review_round == MAX_FILE_REVIEW_ITERATIONS
    )


def _shipment_basis_authorized(shipment_basis: Any, verdict: Any) -> bool:
    return (shipment_basis, verdict) in {
        ("reviewer_approved", "deliverable"),
        ("single_material_review_repaired", "needs_revision"),
        ("max_review_iterations_review_ready", "undeterminable"),
    }


def _file_progress_payload(cadence: dict[str, Any]) -> dict[str, Any]:
    """Keep internal acceptance evidence out of the buyer-facing progress note."""
    payload = delivery_queue.progress_payload(cadence)
    owner_message = _text(cadence.get("customer_message"))
    if owner_message:
        payload["message"] = owner_message
        return payload
    revision = (cadence.get("buyer_feedback_stage") == "revision"
                or cadence.get("buyer_reply_after_artifact_observed") is True)
    payload["message"] = (
        "お世話になっております。修正版をお送りします。ご確認をお願いいたします。"
        if revision else
        "お世話になっております。成果物をお送りします。ご確認をお願いいたします。"
    )
    return payload


def _browser_contract_finding(process: Any) -> str | None:
    for line in reversed(_text(getattr(process, "stdout", "")).splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        finding = value.get("contract_invalid") if isinstance(value, dict) else None
        if isinstance(finding, str) and finding.strip():
            return finding.strip()
    return None


def _context_input_snapshot(root: Path, context: Path) -> dict[str, tuple[int, str]]:
    try:
        compiled = _load(context)
        source_refs = compiled["source_refs"]
        read_first = compiled["combined_context"]["read_these_first"]
    except (KeyError, TypeError, OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid compiled context") from error
    if not isinstance(source_refs, list) or not isinstance(read_first, list):
        raise ValueError("invalid compiled context inputs")
    root = root.resolve()
    snapshots: dict[str, tuple[int, str]] = {}
    for ref in source_refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise ValueError("invalid compiled source reference")
        raw = Path(ref["path"])
        if raw.is_absolute():
            raise ValueError("compiled source reference must be relative")
        path = (root / raw).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError("compiled source reference escapes project") from error
        # The compiled context freezes the exact prior owner outputs in context_sha256. Those
        # outputs are then deliberately replaced by the selected owner, so treating them as live
        # routing inputs makes a decision invalidate itself as soon as it is executed. Buyer
        # sources, requirements and state remain live freshness inputs.
        relative = path.relative_to(root)
        if ((relative.parts and relative.parts[0] == "delivery")
                or relative == Path("events.jsonl")):
            continue
        snapshot = _file_snapshot(path)
        if (ref.get("bytes"), ref.get("sha256")) != snapshot:
            raise ValueError("compiled source reference changed")
        snapshots[str(path)] = snapshot
    for raw in read_first:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("invalid compiled read path")
        candidate = Path(raw)
        path = (root / candidate if not candidate.is_absolute() else candidate).resolve()
        snapshots[str(path)] = _file_snapshot(path)
    return snapshots


def _context_inputs_sha256(snapshots: dict[str, tuple[int, str]]) -> str:
    payload = json.dumps(snapshots, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _revalidate_file_snapshots(snapshots: dict[str, tuple[int, str]]) -> None:
    for raw, expected in snapshots.items():
        if _file_snapshot(Path(raw)) != expected:
            raise ValueError("bound paid decision input changed")


def _decision_prompt(context: Path, context_sha256: str, feedback: str,
                    requirements: str, identity: dict[str, str],
                    buyer_identity: dict[str, str],
                    operator_policy: dict[str, Any] | None = None,
                    operator_policy_sha256: str = "") -> bytes:
    policy_instruction = ""
    if operator_policy:
        policy_instruction = (
            "The account owner supplied an exact-cycle operator policy with SHA256 "
            f"{operator_policy_sha256}: "
            f"{json.dumps(operator_policy['directives'], ensure_ascii=False)}. "
            "Treat it as current project authority. It may stop, narrow, transfer, or otherwise constrain seller work, "
            "but it cannot invent buyer approval, authorize formal delivery, or override marketplace safety. "
        )
    return (
        f"Return one strict JSON semantic decision for the compiled cumulative context {context}. "
        f"context_sha256={context_sha256}; current feedback_sha256={feedback}; requirements_sha256={requirements}; "
        f"latest_message_identity={json.dumps(identity, sort_keys=True)}; "
        f"latest_buyer_message_identity={json.dumps(buyer_identity, sort_keys=True)}. "
        "Use decision actionable, await_buyer, satisfied_noop, or blocked. "
        "Actionable requires mode file, remote, or answer; every other decision requires mode null. "
        "Choose mode from the required effect: remote only for an authenticated system mutation outside the Coconala "
        "marketplace; remote is for a project-authorized work target, including a seller-owned account explicitly bound "
        "to this project in ~/gig/private/remote-account-authorizations.json, never for any Coconala page or transaction control. "
        "Coconala cancellation, refund, dispute, and contract controls require their own code-owned adapter and must be "
        "blocked when no such adapter is present. Answer mode is for the Coconala talkroom: use it when a text response "
        "itself completely satisfies the request, or when one indispensable missing "
        "buyer input must be requested before work can continue. In the latter case ask one bounded question that names "
        "exactly what is missing. Before choosing any question, search the complete compiled context and every "
        "read_these_first source; asking for any fact already present there is forbidden. Do not invent a buyer approval "
        "or authentication dependency when the compiled context or exact-cycle operator policy identifies an authorized, "
        "effect-ready seller-owned resource that can perform the work. If the seller has already told the buyer that login "
        "succeeded, never ask that buyer for an OTP, login availability, credentials, or another coordination step; choose "
        "remote and do the authorized work. A buyer who delegates execution judgment or asks for results is not asking for "
        "another explanation, approval question, or authentication ceremony. "
        "A resource resolver result of available=false proves only that no reusable resource is registered; it does not "
        "prove the authorized outcome is infeasible. When the buyer contract requires seller signup, account creation, "
        "or resource setup and general browser/email tools can perform it, choose actionable remote and require the owner "
        "to create or recover the seller-owned resource autonomously. Missing an exact named skill is not a blocker. "
        "Never ask the buyer to supply a seller-owned account, skill, or setup step that the authorized owner can create. "
        "gate the buyer did not request. A seller's earlier voluntary promise to ask for confirmation is not a buyer requirement, "
        "and a newer seller correction plus an explicit private account authorization permits the promised work to proceed. "
        "Use await_buyer only after that "
        "exact question has already been sent and no newer buyer "
        "reply exists. Use file whenever satisfying the request requires a seller-produced local artifact, "
        "including when one absent fact blocks only part of the scope: put only that fact in unresolved and require the "
        "useful non-blocked portion now rather than delaying the entire artifact. Never invent a document or other "
        "artifact merely because a detailed answer has several sections: choose answer unless the buyer or accumulated "
        "contract requires a file, or the requested outcome cannot truthfully be delivered as talkroom text. "
        "When the buyer explicitly requires the deliverable contents pasted into the Coconala talkroom and does not also "
        "require a separate file, choose answer even for structured copy, revisions, or content previously stored in a file. "
        "including initial delivery, revision, and resubmission of an artifact already present on disk. A file delivery "
        "may also include an accompanying Coconala message. Do not choose remote merely because the response describes "
        "or acknowledges an action. "
        "When the accumulated contract still requires authorized external effects, do not choose file merely to "
        "package or report incomplete progress; choose remote until the required effects are complete or official "
        "evidence proves reachable exhaustion. A started search, query list, zero-byte or partial output, model "
        "narration, timeout, or simple absence of candidates is not exhaustion evidence. Exhaustion requires a "
        "complete nonempty machine-readable receipt that binds the intended query scope, attempted count equal to "
        "intended count, completion time, zero query errors, and the official URLs actually checked; the model still "
        "decides semantic eligibility from those sources. Audit progress semantically rather than copying a stored total. If an "
        "already-published live system has an authorized target and official deployment or provider receipts in the "
        "compiled context, a buyer-requested revision remains remote work: choose remote until the live revision and its official verification are complete. "
        "For buyer review, send its verified HTTPS review URL without a file attachment. Choose file instead only when "
        "the accumulated buyer contract explicitly asks for source files, an archive, or a download. "
        "Do not resend a review URL already present in official Coconala readback; reconcile it and continue the revision. "
        "If later feedback changes the live system, update and verify the same authorized target before deciding whether "
        "a new URL message is needed. "
        "effect's exact outbound payload asks the recipient to confirm a required qualification, that effect remains "
        "qualification-only and does not count toward a qualified target until an affirmative official response is "
        "read back. When a stored classification contradicts its payload or official response state, do not propagate "
        "the incorrect total to the buyer: require the remote owner to append an audited classification revision, then "
        "continue the authorized external work from the corrected effective ledger. "
        "For every initial file submission and every correction, set delivery_stage to review and "
        "formal_approval_evidence to null. Formal delivery is never inferred merely because an artifact is complete. "
        "Set delivery_stage to formal only after the buyer explicitly approves an already submitted artifact and permits "
        "the transaction to be completed; then formal_approval_evidence must exactly equal "
        "latest_buyer_message_identity. A later seller acknowledgement does not erase that approval; any later buyer "
        "message becomes the new buyer identity and requires a new semantic decision. Every non-formal decision uses "
        "formal_approval_evidence null. Every non-file decision "
        "uses delivery_stage none. Decide explicit approval from "
        "the complete semantic workflow, never from title or keyword matching. required_output and required_effect must "
        "state the bounded outcome. required_assets must list every buyer-visible screenshot, image, or linked asset "
        "required for the current bounded output and available for honest verification in this cycle; use [] only when "
        "the current output requires no such media. An asset that can exist only after a future event belongs in "
        "unresolved and in the later required output, not in the current required_assets contract. "
        "Each required asset needs a stable lowercase asset_id, kind, minimum_count, buyer_visible_purpose, "
        "source_authority builder/buyer/account_owner, and archive_required. Never move an asset that is available and "
        "required for the current output into unresolved. unresolved is an array of strings. "
        + policy_instruction
        + "Read the context and its read_these_first files. Do not use a browser or mutate anything."
    ).encode("utf-8")


def _cached_paid_decision(root: Path, receipt: Any, prompt: Path,
                          prompt_sha256: str, schema_sha256: str, context_sha256: str,
                          context_inputs_sha256: str, feedback: str, requirements: str,
                          identity: dict[str, str], buyer_identity: dict[str, str],
                          operator_policy_sha256: str) -> dict[str, Any]:
    if (not isinstance(receipt, dict)
            or receipt.get("schema_version") != PAID_DECISION_SCHEMA_VERSION
            or receipt.get("prompt_version") != PAID_DECISION_PROMPT_VERSION
            or receipt.get("schema_sha256") != schema_sha256
            or receipt.get("context_sha256") != context_sha256
            or receipt.get("context_inputs_sha256") != context_inputs_sha256
            or receipt.get("operator_policy_sha256", "") != operator_policy_sha256
            or receipt.get("prompt_sha256") != prompt_sha256
            or not isinstance(receipt.get("runner"), dict)):
        raise ValueError("stale paid decision receipt")
    runner = receipt["runner"]
    if (set(runner) != {"status", "task_label", "task_class", "escalated",
                        "selected_provider", "selected_model", "summary_sha256", "result_sha256"}
            or runner.get("status") != "success"
            or runner.get("task_label") != "paid-work-decision"
            or runner.get("task_class") != "escalation-agent"
            or runner.get("escalated") is not True
            or (runner.get("selected_provider"), runner.get("selected_model")) not in PAID_RUNNER_CANDIDATES
            or any(not re.fullmatch(r"[0-9a-f]{64}", runner.get(key, ""))
                   for key in ("summary_sha256", "result_sha256"))):
        raise ValueError("invalid paid decision runner proof")
    if prompt.is_symlink() or not _regular_file(prompt) or _file_snapshot(prompt)[1] != prompt_sha256:
        raise ValueError("stale paid decision prompt")
    evidence_root = root / "evidence"
    evidence = evidence_root / "agent-PAID_WORK_DECISION"
    if (evidence_root.is_symlink() or not evidence_root.is_dir()
            or evidence.is_symlink() or not evidence.is_dir()):
        raise ValueError("missing paid decision evidence")
    proof = _decision_runner_proof(evidence)
    if proof != runner:
        raise ValueError("tampered paid decision evidence")
    result_path = _consultation_result_path(evidence)
    value = _load(result_path)
    validated = _validate_paid_decision(value, feedback, requirements, identity, buyer_identity)
    cached_value = {key: receipt.get(key) for key in PAID_DECISION_FIELDS}
    if validated != cached_value:
        raise ValueError("paid decision result does not match receipt")
    return validated


def _paid_decision(args, item_path: Path, root: Path, base: Path) -> dict[str, Any]:
    item_snapshot = _file_snapshot(item_path)
    feedback = _text(_load(item_path).get("buyer_feedback_sha256"))
    requirements = paid_remote_result.requirements_digest(root, feedback)
    item = _load(item_path)
    talkroom_id = _text(item.get("talkroom_id"))
    identity = _latest_official_identity(root, talkroom_id)
    buyer_identity = _latest_official_buyer_identity(root, talkroom_id)
    schema = args.decision_schema
    schema_snapshot = _file_snapshot(schema)
    schema_sha256 = schema_snapshot[1]
    context = root / "context" / "current.json"
    if context.parent.is_symlink() or (context.parent.exists() and not context.parent.is_dir()):
        raise Failure("context_compile")
    context.parent.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, str(args.context_compiler), "--project-root", str(root),
          "--queue-item", str(item_path), "--output", str(context)], "context_compile")
    if context.is_symlink() or not _regular_file(context):
        raise Failure("context_compile")
    context_snapshot = context.read_bytes()
    context_sha256 = hashlib.sha256(context_snapshot).hexdigest()
    context_inputs = _context_input_snapshot(root, context)
    context_inputs_sha256 = _context_inputs_sha256(context_inputs)
    operator_policy_path, operator_policy, operator_policy_sha256 = _file_operator_policy(
        root, feedback, requirements)
    if operator_policy_path is not None:
        context_inputs[str(operator_policy_path.resolve())] = _file_snapshot(operator_policy_path)
        context_inputs_sha256 = _context_inputs_sha256(context_inputs)
    if (paid_remote_result.requirements_digest(root, feedback) != requirements
            or _latest_official_identity(root, talkroom_id) != identity):
        raise Failure("paid_work_decision")
    if context.parent.is_symlink() or not context.parent.is_dir():
        raise Failure("paid_work_decision")
    receipt_path = context.parent / "paid-work-decision.json"
    prompt = base / "mode" / "decision.prompt.txt"
    prompt_bytes = _decision_prompt(
        context, context_sha256, feedback, requirements, identity, buyer_identity,
        operator_policy, operator_policy_sha256)
    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
    try:
        receipt = None if receipt_path.is_symlink() or not _regular_file(receipt_path) else _load(receipt_path)
        return _cached_paid_decision(root, receipt, prompt, prompt_sha256,
                                     schema_sha256, context_sha256, context_inputs_sha256,
                                     feedback, requirements, identity, buyer_identity,
                                     operator_policy_sha256)
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    if prompt.parent.is_symlink() or (prompt.parent.exists() and not prompt.parent.is_dir()):
        raise Failure("paid_work_decision")
    prompt.parent.mkdir(parents=True, exist_ok=True)
    if prompt.is_symlink():
        raise Failure("paid_work_decision")
    prompt.write_bytes(prompt_bytes)
    prompt_snapshot = _file_snapshot(prompt)
    evidence_root = root / "evidence"
    if evidence_root.is_symlink() or (evidence_root.exists() and not evidence_root.is_dir()):
        raise Failure("paid_work_decision")
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence = evidence_root / "agent-PAID_WORK_DECISION"
    if evidence.is_symlink():
        raise Failure("paid_work_decision")
    started_ns = time.time_ns()
    try:
        decision_command = [sys.executable, str(args.agent_runner), "--task-class", "escalation-agent",
              "--prompt-file", str(prompt), "--schema", str(schema), "--evidence-dir", str(evidence),
              "--task-label", "paid-work-decision", "--escalation-reason",
              "Paid delivery routing must use an authorized escalation semantic model.",
              "--loop", _runner_loop_id(), "--workdir", str(root), "--timeout-seconds", "1800", "--read-only"]
        _run_private_model_serialized(root, decision_command, "paid-work-decision", "paid_work_decision")
        try:
            value = _consultation_runner_result(
                evidence, task_label="paid-work-decision", task_class="escalation-agent",
                model=PAID_DECISION_MODEL, started_ns=started_ns,
            )
        except Failure as error:
            raise Failure("paid_work_decision") from error
        runner_proof = _decision_runner_proof(evidence)
        value = _validate_paid_decision(value, feedback, requirements, identity, buyer_identity)
        current_bound = {
            str(item_path): item_snapshot,
            str(schema): schema_snapshot,
            str(prompt): prompt_snapshot,
            str(context): (len(context_snapshot), context_sha256),
        }
        _revalidate_file_snapshots(current_bound)
        current_context_inputs = _context_input_snapshot(root, context)
        current_policy_path, _current_policy, current_policy_sha256 = _file_operator_policy(
            root, feedback, requirements)
        if current_policy_path is not None:
            current_context_inputs[str(current_policy_path.resolve())] = _file_snapshot(current_policy_path)
        if (current_context_inputs != context_inputs
                or current_policy_sha256 != operator_policy_sha256
                or paid_remote_result.requirements_digest(root, feedback) != requirements
                or _latest_official_identity(root, talkroom_id) != identity):
            raise ValueError("paid decision inputs changed")
        receipt = {"schema_version": PAID_DECISION_SCHEMA_VERSION,
                   "prompt_version": PAID_DECISION_PROMPT_VERSION, "schema_sha256": schema_sha256,
                   "prompt_sha256": prompt_sha256, "context_sha256": context_sha256,
                   "context_inputs_sha256": context_inputs_sha256,
                   "operator_policy_sha256": operator_policy_sha256,
                   "runner": runner_proof, **value}
        _write(receipt_path, receipt)
        return value
    except Failure:
        raise
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise Failure("paid_work_decision") from error


def _decision_only(args, item_path: Path, output: Path) -> int:
    try:
        item = _load(item_path)
        room = _text(item.get("talkroom_id"))
        if not re.fullmatch(r"[0-9]+", room):
            raise Failure("paid_work_decision")
        candidate = delivery_project.resolve_project_root(args.projects_root, item)
        projects = args.projects_root.resolve()
        if candidate.is_symlink() or candidate.parent.resolve() != projects:
            raise Failure("paid_work_decision")
        root = candidate.resolve()
        root.relative_to(projects)
        if not root.is_dir():
            raise Failure("paid_work_decision")
        if _account_owner_observe_only(args, item) is not None:
            _write(output, {"status": "reserved_for_owner", "talkroom_id": room,
                            "effect": 0, "readback": 1, "failed": 0})
            return 0
        state = _load(root / "state.json")
        if _text(state.get("talkroom_id")) != room:
            raise Failure("paid_work_decision")
        evidence_root = args.evidence_dir.resolve()
        paid_root = evidence_root / "paid-direct"
        if paid_root.is_symlink():
            raise Failure("paid_work_decision")
        base_root = paid_root.resolve()
        if base_root.parent != evidence_root:
            raise Failure("paid_work_decision")
        base = (base_root / room).resolve()
        if base.parent != base_root:
            raise Failure("paid_work_decision")
        value = _paid_decision(args, item_path, root, base)
        _write(output, {"status": "completed", **value})
        return 0
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        _write(output, {"status": "failed", "failed_step": error.step if isinstance(error, Failure) else "paid_work_decision", "effect": 0})
        return 1

def _delivery_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    snapshot = {}
    for name in ("paid-remote-intent.json", "paid-remote-result.json", "paid-answer.json"):
        path = root / "delivery" / name
        try: info = path.lstat()
        except FileNotFoundError:
            if name != "paid-answer.json": raise Failure("remote_verifier")
            snapshot[name] = (False, None, None, None, None); continue
        except OSError as error: raise Failure("remote_verifier") from error
        if not stat.S_ISREG(info.st_mode): raise Failure("remote_verifier")
        try: content = path.read_bytes()
        except OSError as error: raise Failure("remote_verifier") from error
        snapshot[name] = (True, info.st_dev, info.st_ino, info.st_mode, content)
    return snapshot

def _project_identity_snapshot(root: Path, exclude: Path) -> dict[str, tuple[Any, ...]]:
    """Identity of everything under the project except the verifier's own evidence directory.

    The verifier needs to reach a live page and record what it saw, so it cannot run without write
    access. Delivery artefacts were already fenced by digest; this closes the rest, so a verifier
    that can write still cannot quietly improve the thing it is verifying. Identity rather than
    content because these projects carry rendered video.
    """
    snapshot: dict[str, tuple[Any, ...]] = {}
    # Both sides resolved, or the exclusion silently never matches: on macOS a project under
    # /var resolves to /private/var, and comparing one resolved path against unresolved parents
    # quietly keeps everything in scope.
    try: root, exclude = root.resolve(), exclude.resolve()
    except OSError as error: raise Failure("remote_verifier") from error
    for path in sorted(root.rglob("*")):
        try:
            if exclude == path or exclude in path.parents: continue
            info = path.lstat()
        except OSError as error: raise Failure("remote_verifier") from error
        snapshot[str(path.relative_to(root))] = (
            info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
        )
    return snapshot

def _requirements_snapshot(root: Path) -> bytes:
    path = root / "requirements" / "live-buyer-reply.json"
    if path.is_symlink() or not _regular_file(path): raise Failure("context_compile")
    try: return path.read_bytes()
    except OSError as error: raise Failure("context_compile") from error

def _validate_verifier_runner(managed: Path, semantic_status: str,
                              min_mtime_ns: int | None = None) -> dict[str, Any]:
    summary = _runner_summary(managed)
    expected = {"status": "success", "task_label": "paid-remote-verifier", "task_class": "escalation-agent",
                "escalated": True}
    if (not isinstance(summary, dict)
            or any(summary.get(key) != value for key, value in expected.items())
            or (summary.get("selected_provider"), summary.get("selected_model")) not in PAID_RUNNER_CANDIDATES):
        raise ValueError("verifier runner selection mismatch")
    if step_result_status.status_from_evidence(managed) != semantic_status:
        raise ValueError("verifier runner semantic status mismatch")
    if min_mtime_ns is not None:
        raw_result = summary["result_path"]
        result_path = (managed / raw_result if not Path(raw_result).is_absolute() else Path(raw_result)).resolve()
        now_ns = time.time_ns()
        for path in (managed / "summary.json", result_path):
            mtime_ns = path.stat().st_mtime_ns
            if mtime_ns <= min_mtime_ns or mtime_ns > now_ns + 1_000_000_000:
                raise ValueError("verifier runner proof is stale")
    return summary

def _semantic_effect_contract(project_root: Path) -> tuple[dict[str, Any], str]:
    decision = _load(project_root / "context" / "paid-work-decision.json")
    contract = {key: decision.get(key) for key in (
        "decision", "mode", "feedback_sha256", "requirements_sha256",
        "required_output", "required_effect", "required_assets",
    )}
    if (contract["decision"] != "actionable" or contract["mode"] != "remote"
            or not _text(contract["required_output"]) or not _text(contract["required_effect"])
            or not isinstance(contract["required_assets"], list)):
        raise ValueError("invalid semantic effect contract")
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return contract, hashlib.sha256(encoded).hexdigest()

def _require_semantic_effect_binding(project_root: Path, *records: dict[str, Any]) -> str:
    _, digest = _semantic_effect_contract(project_root)
    if any(not isinstance(record, dict) or record.get("semantic_contract_sha256") != digest
           for record in records):
        raise ValueError("semantic effect contract mismatch")
    return digest

def _validated_business_outcome(record: dict[str, Any]) -> dict[str, Any]:
    """Require official proof that the semantic outcome itself is complete.

    Matching one remote target state is only a transport/readback proof.  It is
    never sufficient when the semantic contract requires a broader business
    effect or buyer-facing output.
    """
    outcome = record.get("business_outcome") if isinstance(record, dict) else None
    if not isinstance(outcome, dict):
        raise ValueError("business outcome missing")
    if (outcome.get("required_effect_satisfied") is not True
            or outcome.get("required_output_satisfied") is not True
            or outcome.get("remaining_work") not in ([], None)):
        raise ValueError("business outcome incomplete")
    receipts = outcome.get("official_receipts")
    if not isinstance(receipts, list) or not receipts:
        raise ValueError("official business receipts missing")
    for receipt in receipts:
        source = (_text(receipt.get("readback_source")) if isinstance(receipt, dict) else "")
        if not source and isinstance(receipt, dict):
            source = " ".join(_text(receipt.get(key)) for key in ("provider", "kind", "result")).strip()
        if (not isinstance(receipt, dict)
                or not _text(receipt.get("effect_key"))
                or not _text(receipt.get("official_url"))
                or not source
                or receipt.get("exact_readback") is not True):
            raise ValueError("invalid official business receipt")
    return outcome


def _business_outcomes_match_effects(builder: dict[str, Any], verifier: dict[str, Any]) -> bool:
    def identity(outcome: dict[str, Any]) -> tuple[Any, ...]:
        receipts = outcome.get("official_receipts") if isinstance(outcome, dict) else None
        effects = sorted(
            (_text(receipt.get("effect_key")), _text(receipt.get("official_url")))
            for receipt in receipts or [] if isinstance(receipt, dict)
        )
        return (
            outcome.get("required_effect_satisfied"),
            outcome.get("required_output_satisfied"),
            outcome.get("remaining_work") or [],
            effects,
        )

    return identity(builder) == identity(verifier)


def _verifier_evidence_references(result: dict[str, Any]) -> list[tuple[str, str]]:
    references = [(field, result.get(field)) for field in ("before_evidence", "after_evidence")
                  if isinstance(result.get(field), str) and result.get(field).strip()]
    single = result.get("verifier_evidence")
    if not references and isinstance(single, str) and single.strip():
        references = [("verifier_evidence", single)]
    if not references and isinstance(result.get("evidence"), list):
        references = [("evidence", value) for value in result["evidence"]
                      if isinstance(value, str) and value.strip()]
    return references

def _validate_managed_verifier(verifier: Path, project_root: Path, intent: dict[str, Any], feedback: str, digest: str,
                               min_evidence_mtime_ns: int | None = None) -> Path:
    try:
        if not isinstance(intent, dict) or not isinstance(intent.get("desired_state"), dict) or not _text(intent.get("target")):
            raise ValueError("invalid remote intent")
        project_root = project_root.resolve(); evidence_root = project_root / "evidence"
        if evidence_root.is_symlink() or not evidence_root.is_dir():
            raise ValueError("invalid project evidence root")
        managed_path = evidence_root / "agent-PAID_REMOTE_VERIFY"
        if managed_path.is_symlink() or not managed_path.is_dir() or verifier.is_symlink():
            raise ValueError("invalid managed verifier root")
        verifier = verifier.resolve(); managed = managed_path.resolve()
        if (verifier.name != "remote-verifier-result.json" or verifier.parent != managed
                or not verifier.is_file()):
            raise ValueError("invalid managed verifier path")
        _validate_verifier_runner(managed, "ok", min_evidence_mtime_ns)
        delivery_result = _load(project_root / "delivery" / "paid-remote-result.json")
        requirements_sha256 = paid_remote_result.requirements_digest(project_root, feedback)
        message = delivery_result.get("customer_message") if isinstance(delivery_result, dict) else None
        if not isinstance(message, str) or not message.strip():
            raise ValueError("invalid customer message")
        message_sha256 = hashlib.sha256(message.encode()).hexdigest()
        if (intent.get("requirements_sha256") != requirements_sha256
                or delivery_result.get("requirements_sha256") != requirements_sha256
                or intent.get("message_sha256") != message_sha256
                or delivery_result.get("message_sha256") != message_sha256):
            raise ValueError("builder requirements contract mismatch")
        result = _load(verifier); desired = intent["desired_state"]; target = intent["target"]
        semantic_contract_sha256 = _require_semantic_effect_binding(project_root, intent, delivery_result)
        builder_outcome = _validated_business_outcome(delivery_result)
        verifier_outcome = _validated_business_outcome(result)
        if (not isinstance(result, dict) or result.get("verified") is not True
                or result.get("buyer_feedback_sha256") != feedback or result.get("target") != target
                or result.get("desired_digest", result.get("desired_state_digest")) != digest
                or result.get("observed_digest") != digest
                or not paid_remote_result.canonical_equal(result.get("observed_state"), desired)
                or result.get("requirements_sha256") != requirements_sha256
                or result.get("message_sha256") != message_sha256):
            raise ValueError("verifier result mismatch")
        if not _business_outcomes_match_effects(builder_outcome, verifier_outcome):
            raise ValueError("business outcome verifier mismatch")
        attachment = _validated_customer_attachment(project_root, delivery_result.get("customer_attachment"))
        if (intent.get("customer_attachment") != attachment or result.get("customer_attachment") != attachment):
            raise ValueError("customer attachment verifier mismatch")
        references = _verifier_evidence_references(result)
        if not references:
            raise ValueError("verifier evidence missing")
        observed = False; now_ns = time.time_ns(); evidence_records = []
        for field, raw_path in references:
            evidence_path = (managed / raw_path if not Path(raw_path).is_absolute() else Path(raw_path)).resolve()
            evidence_path.relative_to(managed)
            mtime_ns = evidence_path.stat().st_mtime_ns if evidence_path.is_file() else 0
            if (not evidence_path.is_file() or (min_evidence_mtime_ns is not None
                                                 and (mtime_ns <= min_evidence_mtime_ns or mtime_ns > now_ns + 1_000_000_000))):
                raise ValueError("verifier evidence is stale or missing")
            evidence = _load(evidence_path)
            if (not isinstance(evidence, dict) or evidence.get("target") != target
                    or evidence.get("authenticated") is not True
                    or evidence.get("requirements_sha256") != requirements_sha256
                    or evidence.get("message_sha256") != message_sha256):
                raise ValueError("verifier evidence identity mismatch")
            if not paid_remote_result.canonical_equal(evidence.get("observed_state"), desired):
                raise ValueError("verifier observed state mismatch")
            evidence_records.append((evidence_path, evidence))
            observed = True
        if not observed:
            raise ValueError("verifier observed evidence missing")
        # The verifier model occasionally copies or recomputes this identifier
        # incorrectly.  Normalize verifier-owned records only after every
        # substantive state, identity, freshness, and outcome check succeeds.
        if result.get("semantic_contract_sha256") != semantic_contract_sha256:
            result["semantic_contract_sha256"] = semantic_contract_sha256
            _write(verifier, result)
        for evidence_path, evidence in evidence_records:
            if evidence.get("semantic_contract_sha256") != semantic_contract_sha256:
                evidence["semantic_contract_sha256"] = semantic_contract_sha256
                _write(evidence_path, evidence)
        return verifier
    except Failure:
        raise
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise Failure("remote_verifier") from error

def _review_failure(verifier: Path, project_root: Path, intent: dict[str, Any], feedback: str, digest: str,
                    min_mtime_ns: int | None = None) -> dict[str, Any]:
    """Read a model-owned rejection as repair input, never as effect authorization."""
    try:
        project_root = project_root.resolve(); managed = project_root / "evidence" / "agent-PAID_REMOTE_VERIFY"
        if managed.is_symlink() or not managed.is_dir() or verifier.is_symlink():
            raise ValueError("invalid managed verifier root")
        verifier = verifier.resolve()
        if verifier.parent != managed.resolve() or verifier.name != "remote-verifier-result.json" or not verifier.is_file():
            raise ValueError("invalid managed verifier result")
        _validate_verifier_runner(managed, "blocked", min_mtime_ns)
        mtime_ns, now_ns = verifier.stat().st_mtime_ns, time.time_ns()
        if min_mtime_ns is not None and (mtime_ns <= min_mtime_ns or mtime_ns > now_ns + 1_000_000_000):
            raise ValueError("stale verifier rejection")
        result = _load(verifier)
        delivery_result = _load(project_root / "delivery" / "paid-remote-result.json")
        _require_semantic_effect_binding(project_root, intent, delivery_result, result)
        requirements_sha256 = paid_remote_result.requirements_digest(project_root, feedback)
        message = delivery_result.get("customer_message") if isinstance(delivery_result, dict) else None
        if not isinstance(message, str) or not message.strip(): raise ValueError("invalid customer message")
        message_sha256 = hashlib.sha256(message.encode()).hexdigest()
        if (not isinstance(result, dict) or result.get("verified") is not False
                or result.get("buyer_feedback_sha256") != feedback
                or result.get("target") != intent.get("target")
                or result.get("desired_digest", result.get("desired_state_digest")) != digest
                or result.get("requirements_sha256") != requirements_sha256
                or result.get("message_sha256") != message_sha256):
            raise ValueError("verifier rejection identity mismatch")
        classification = result.get("classification")
        delta = result.get("delta")
        if classification == "quality_mismatch":
            if not isinstance(delta, list) or not 1 <= len(delta) <= 20:
                raise ValueError("invalid reviewer delta")
            for item in delta:
                fields = ("requirement", "expected", "observed", "evidence", "repair")
                if (not isinstance(item, dict) or set(item) != set(fields)
                        or any(not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 2000
                               for key in fields)):
                    raise ValueError("invalid reviewer delta")
        elif classification in {"auth_transient", "cdp_transient"}:
            if delta not in (None, []): raise ValueError("transient rejection cannot prescribe a repair")
        else:
            raise ValueError("unclassified verifier rejection")
        return {"classification": classification, "delta": delta or []}
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise Failure("remote_verifier") from error

def resolve_managed_verifier(project_root: Path, feedback: str, digest: str) -> Path:
    try:
        project_root = project_root.resolve()
        intent = _load(project_root / "delivery" / "paid-remote-intent.json")
        managed = project_root / "evidence" / "agent-PAID_REMOTE_VERIFY"
        if managed.is_symlink() or not managed.is_dir(): raise Failure("remote_resume")
        verifier = managed / "remote-verifier-result.json"
        return _validate_managed_verifier(verifier, project_root, intent, feedback, digest)
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    raise Failure("remote_resume")

def _targeted(args, item, index):
    room = _text(item.get("talkroom_id")); base = args.evidence_dir / "paid-direct" / "targeted" / room
    item_path, snapshot = base / "item.json", base / "snapshot.json"
    _write(item_path, item)
    _reclaim_browser_owner(args, f"paid-direct-{room}")
    started_ns = time.time_ns()
    environment = _fresh_child_env(args, owner=f"paid-direct-{room}")
    try:
        _run(
            _collector(args, "selected-talkroom-only", snapshot, base, item_path, item),
            "targeted_readback", timeout=TARGETED_READBACK_TIMEOUT_SECONDS, env=environment,
        )
    except Failure as error:
        if "authenticated tab did not finish navigation" in error.detail:
            _run(
                _collector(args, "selected-talkroom-only", snapshot, base, item_path, item),
                "targeted_readback", timeout=TARGETED_READBACK_TIMEOUT_SECONDS, env=environment,
            )
            return {**item, **_row(_load(snapshot), room)}
        # The collector atomically publishes the official snapshot before optional trailing
        # work. A child that wedges after that point must not discard this wake's fresh readback.
        if (not snapshot.is_file() or snapshot.stat().st_mtime_ns <= started_ns):
            raise
        _row(_load(snapshot), room)
    return {**item, **_row(_load(snapshot), room)}

def _recoverable(args, item):
    try:
        root = _paid_project_root(args, item)
        delivery = root / "delivery"
        if not root.is_dir() or not (root / "state.json").is_file(): return None
        feedback = _text(item.get("buyer_feedback_sha256")); intent_path = delivery / "paid-remote-intent.json"
        try:
            decision = _current_paid_decision(root, item)
        except (AttributeError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
            decision = None
        if decision is not None:
            if decision.get("decision") != "actionable":
                return None
            if decision.get("mode") in {"file", "answer"}:
                return root, None
            remote_mode = decision.get("mode") == "remote"
        else:
            if _answer_ready(root, item):
                return root, None
            remote_mode = (_legacy_paid_mode(root, feedback) == "remote"
                           or _remote_revision_required(root, feedback))
        if remote_mode and all((delivery / name).is_file()
                               for name in ("paid-remote-intent.json", "paid-remote-result.json")):
            intent = _load(intent_path)
            if isinstance(intent, dict) and intent.get("buyer_feedback_sha256") == feedback:
                try:
                    verifier = resolve_managed_verifier(
                        root, feedback, _text(intent.get("desired_state_sha256")),
                    )
                    paid_remote_result.resume(
                        root, feedback, _text(intent.get("desired_state_sha256")), verifier,
                    )
                    return root, verifier
                except Failure: pass
                except (OSError, ValueError, TypeError, json.JSONDecodeError): pass
        if remote_mode:
            return root, None
        return None
    except (Failure, OSError, ValueError, TypeError, json.JSONDecodeError): return None


def _paid_project_root(args, item: dict[str, Any]) -> Path:
    projects = args.projects_root.expanduser().resolve()
    candidate = delivery_project.resolve_project_root(args.projects_root, item)
    if candidate.is_symlink() or candidate.parent.resolve() != projects:
        raise Failure("context_compile")
    root = candidate.resolve()
    root.relative_to(projects)
    return root


def _account_owner_observe_only(args, item: dict[str, Any]) -> Path | None:
    """Honor a room-local no-resend disposition only while its official effect still verifies."""
    try:
        if item.get("buyer_reply_after_artifact_observed") is True:
            return None
        root = _paid_project_root(args, item)
        receipt_path = root / "context" / "paid-effect-policy.json"
        if not _regular_file(receipt_path):
            return None
        receipt = _load(receipt_path)
        room = _text(item.get("talkroom_id"))
        package_sha256 = _text(receipt.get("package_sha256"))
        artifact_basename = _text(receipt.get("artifact_basename"))
        if (receipt.get("version") != 1 or receipt.get("disposition") != "observe_only"
                or receipt.get("authority") != "account_owner_instruction"
                or _text(receipt.get("talkroom_id")) != room
                or not re.fullmatch(r"[0-9a-f]{64}", package_sha256)
                or not artifact_basename or Path(artifact_basename).name != artifact_basename):
            return None
        state = _load(root / "state.json")
        if (state.get("buyer_visible") is not True
                or state.get("latest_buyer_visible_package_sha256") != package_sha256):
            return None
        evidence_root = (args.evidence_dir / "paid-direct" / room).resolve()
        effect = Path(_text(receipt.get("effect_evidence"))).resolve()
        official = Path(_text(receipt.get("official_readback"))).resolve()
        effect.relative_to(evidence_root)
        official.relative_to(evidence_root)
        if (not _regular_file(effect) or not _regular_file(official)
                or hashlib.sha256(effect.read_bytes()).hexdigest()
                != _text(receipt.get("effect_evidence_sha256"))
                or hashlib.sha256(official.read_bytes()).hexdigest()
                != _text(receipt.get("official_readback_sha256"))):
            return None
        effect_row = _load(effect)
        if (effect_row.get("talkroom_id") != room
                or effect_row.get("artifact_basename") != artifact_basename
                or effect_row.get("package_sha256") != package_sha256
                or effect_row.get("send_performed") is not True
                or effect_row.get("formal_delivery_checkbox") is not False):
            return None
        official_row = _row(_load(official), room)
        seller_messages = official_row.get("seller_sent_messages") or []
        if not any(artifact_basename in (message.get("attachments") or [])
                   for message in seller_messages if isinstance(message, dict)):
            return None
        return receipt_path
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
        return None


def _current_paid_decision(root: Path, item: dict[str, Any]) -> dict[str, Any]:
    receipt = _load(root / "context" / "paid-work-decision.json")
    context = root / "context" / "current.json"
    feedback = _text(item.get("buyer_feedback_sha256"))
    requirements = paid_remote_result.requirements_digest(root, feedback)
    context_inputs = _context_input_snapshot(root, context)
    operator_policy_path, _operator_policy, operator_policy_sha256 = _file_operator_policy(
        root, feedback, requirements)
    if operator_policy_path is not None:
        context_inputs[str(operator_policy_path.resolve())] = _file_snapshot(operator_policy_path)
    if (receipt.get("context_sha256") != _file_snapshot(context)[1]
            or receipt.get("schema_version") != PAID_DECISION_SCHEMA_VERSION
            or receipt.get("prompt_version") != PAID_DECISION_PROMPT_VERSION
            or receipt.get("operator_policy_sha256", "") != operator_policy_sha256
            or receipt.get("context_inputs_sha256") != _context_inputs_sha256(context_inputs)):
        raise ValueError("stale paid decision context")
    value = {key: receipt[key] for key in PAID_DECISION_FIELDS}
    talkroom_id = _text(item.get("talkroom_id"))
    identity = _latest_official_identity(root, talkroom_id)
    buyer_identity = _latest_official_buyer_identity(root, talkroom_id)
    return _validate_paid_decision(value, feedback, requirements, identity, buyer_identity)


def _file_mode(root: Path, item: dict[str, Any]) -> bool:
    decision = _current_paid_decision(root, item)
    return decision.get("decision") == "actionable" and decision.get("mode") == "file"


def _file_runner_result(evidence: Path, *, task_label: str,
                        started_ns: int | None) -> tuple[dict[str, Any], dict[str, str]]:
    summary = _runner_summary(evidence)
    expected = {
        "status": "success", "task_label": task_label, "task_class": "escalation-agent",
        "escalated": True,
    }
    if (any(summary.get(key) != value for key, value in expected.items())
            or (summary.get("selected_provider"), summary.get("selected_model")) not in PAID_RUNNER_CANDIDATES):
        raise Failure("file_verifier" if "verifier" in task_label else "file_builder")
    result_path = _consultation_result_path(evidence, summary)
    if started_ns is not None:
        now_ns = time.time_ns()
        for path in (evidence / "summary.json", result_path):
            if path.stat().st_mtime_ns <= started_ns or path.stat().st_mtime_ns > now_ns + 1_000_000_000:
                raise Failure("file_verifier" if "verifier" in task_label else "file_builder")
    value = _load(result_path)
    if not isinstance(value, dict):
        raise Failure("file_verifier" if "verifier" in task_label else "file_builder")
    proof = {
        "selected_provider": summary["selected_provider"],
        "selected_model": summary["selected_model"],
        "summary_sha256": hashlib.sha256((evidence / "summary.json").read_bytes()).hexdigest(),
        "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
    }
    return value, proof


def _source_census_inputs(root: Path) -> list[dict[str, str]]:
    requirements = _load(root / "requirements" / "live-buyer-reply.json")
    attachments = requirements.get("attachments") if isinstance(requirements, dict) else None
    result: list[dict[str, str]] = []
    for attachment in attachments if isinstance(attachments, list) else []:
        if not isinstance(attachment, dict) or not _text(attachment.get("source_path")):
            continue
        path = Path(_text(attachment["source_path"])).resolve()
        relative = path.relative_to(root.resolve())
        sha256 = _file_snapshot(path)[1]
        if attachment.get("sha256") != sha256:
            raise ValueError("stale source census input")
        result.append({"path": str(relative), "sha256": sha256})
    return result


def _trusted_source_census(root: Path, requirements_sha256: str) -> tuple[Path, dict[str, Any]] | None:
    trust_path = root / "context" / "paid-source-census.json"
    if not _regular_file(trust_path):
        return None
    trust = _load(trust_path)
    census_path = Path(_text(trust.get("census_path"))).resolve() if isinstance(trust, dict) else Path()
    expected_sources = _source_census_inputs(root)
    if (not isinstance(trust, dict) or trust.get("version") != 1
            or trust.get("policy_version") != PAID_SOURCE_CENSUS_VERSION
            or trust.get("requirements_sha256") != requirements_sha256
            or trust.get("sources") != expected_sources
            or census_path != (root / "acceptance" / "controller-source-census-v1.json").resolve()
            or trust.get("census_sha256") != _file_snapshot(census_path)[1]):
        return None
    visuals = trust.get("visual_sources")
    visual_suffixes = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    if (not isinstance(visuals, list)
            or (any(Path(source["path"]).suffix.casefold() in visual_suffixes
                    for source in expected_sources) and not visuals)):
        return None
    try:
        for visual in visuals:
            path = Path(_text(visual.get("path"))).resolve()
            path.relative_to(root.resolve())
            if visual.get("sha256") != _file_snapshot(path)[1]:
                return None
        census_visuals = _load(census_path).get("visual_sources")
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if census_visuals != [{key: value for key, value in visual.items() if key != "path"}
                          for visual in visuals]:
        return None
    return census_path, trust


def _source_census_visual_inputs(
    isolated: Path, copied_sources: list[dict[str, str]],
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Create controller-owned page images that the census model must actually see."""
    visual_dir = isolated / "visual-sources"
    visual_dir.mkdir(parents=True, exist_ok=True)
    images: list[Path] = []
    rows: list[dict[str, Any]] = []
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    for index, source in enumerate(copied_sources):
        copied = Path(source["isolated_path"])
        rendered: list[Path] = []
        if copied.suffix.casefold() == ".pdf":
            prefix = visual_dir / f"source-{index:04d}-page"
            _run(["pdftoppm", "-png", "-r", "150", str(copied), str(prefix)], "file_builder")
            rendered = sorted(visual_dir.glob(f"{prefix.name}-*.png"),
                              key=lambda path: int(path.stem.rsplit("-", 1)[1]))
            if not rendered:
                raise Failure("file_builder")
        elif copied.suffix.casefold() in image_suffixes:
            rendered = [copied]
        for page, image in enumerate(rendered, start=1):
            sha256 = _file_snapshot(image)[1]
            images.append(image)
            rows.append({
                "source_path": source["path"], "page": page, "sha256": sha256, "kind": "source",
            })
    return images, rows


def _stage_isolated_agent_runtime(
    runner_source: Path, schema_source: Path, isolated: Path,
) -> tuple[Path, Path]:
    """Recreate the runner's repository-relative imports inside its isolated workspace."""
    runtime_root = isolated / "runtime"
    runner_dir = runtime_root / "agent-runner"
    loop_dir = runtime_root / "loop"
    schema_dir = isolated / "schemas"
    runner_dir.mkdir(parents=True, exist_ok=True)
    loop_dir.mkdir(parents=True, exist_ok=True)
    schema_dir.mkdir(parents=True, exist_ok=True)
    isolated_runner = runner_dir / runner_source.name
    shutil.copyfile(runner_source, isolated_runner)
    for sibling in ("token_budget.py", "config.json"):
        source = runner_source.parent / sibling
        if source.is_file():
            shutil.copyfile(source, runner_dir / sibling)
    loop_source = runner_source.parent.parent / "loop"
    for source in loop_source.glob("*.py"):
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, loop_dir / source.name)
    isolated_schema = schema_dir / schema_source.name
    shutil.copyfile(schema_source, isolated_schema)
    return isolated_runner, isolated_schema


def _prepare_source_census(args, root: Path, requirements_sha256: str, code_root: Path) -> Path | None:
    sources = _source_census_inputs(root)
    if not sources:
        return None
    existing = _trusted_source_census(root, requirements_sha256)
    if existing is not None:
        return existing[0]
    with _project_workspace(root, "paid-source-census-") as temporary:
        isolated = Path(temporary)
        copied_sources: list[dict[str, str]] = []
        for index, source in enumerate(sources):
            original = (root / source["path"]).resolve()
            copied = isolated / "sources" / f"{index:04d}{original.suffix}"
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copied)
            copied_sources.append({**source, "isolated_path": str(copied)})
        visual_images, expected_visuals = _source_census_visual_inputs(isolated, copied_sources)
        requirements_copy = isolated / "requirements.json"
        shutil.copyfile(root / "requirements" / "live-buyer-reply.json", requirements_copy)
        source_manifest = isolated / "source-manifest.json"
        _write(source_manifest, {
            "version": 1, "requirements_path": str(requirements_copy),
            "requirements_sha256": requirements_sha256, "sources": copied_sources,
        })
        approved_skills = isolated / "skills"
        for name in PAID_SOURCE_CENSUS_SKILLS:
            source_skill = code_root / "skills" / name
            if not (source_skill / "SKILL.md").is_file():
                continue
            shutil.copytree(source_skill, approved_skills / name)
        coverage_sources = []
        overlay_cli = approved_skills / "music-score-omr" / "scripts" / "source_notehead_overlay.py"
        for index, source in enumerate(copied_sources):
            copied = Path(source["isolated_path"])
            if copied.suffix.casefold() != ".pdf" or not overlay_cli.is_file():
                continue
            coverage_dir = isolated / "source-coverage" / f"source-{index:04d}"
            _run([sys.executable, str(overlay_cli), "--source", str(copied),
                  "--output", str(coverage_dir)], "file_builder")
            detector_manifest_path = coverage_dir / "detector-manifest.json"
            detector_manifest = _load(detector_manifest_path)
            if (not isinstance(detector_manifest, dict)
                    or detector_manifest.get("source_sha256") != source["sha256"]):
                raise Failure("file_builder")
            detector_sha256 = _file_snapshot(detector_manifest_path)[1]
            coverage_sources.append({
                "source_path": source["path"], "detector_manifest_sha256": detector_sha256,
                "manifest": detector_manifest,
            })
            for page in detector_manifest.get("pages", []):
                for system in page.get("systems", []):
                    tile = coverage_dir / _text(system.get("coverage_tile"))
                    tile_sha256 = _file_snapshot(tile)[1]
                    if tile_sha256 != system.get("coverage_tile_sha256"):
                        raise Failure("file_builder")
                    visual_images.append(tile)
                    expected_visuals.append({
                        "source_path": source["path"], "page": page.get("page"),
                        "system": system.get("system"), "sha256": tile_sha256,
                        "kind": "recognized_system_tile",
                        "detector_manifest_sha256": detector_sha256,
                    })
        coverage_manifest = isolated / "coverage-manifest.json"
        _write(coverage_manifest, {"version": 1, "sources": coverage_sources})
        visual_manifest = isolated / "visual-manifest.json"
        _write(visual_manifest, {"version": 1, "visual_sources": expected_visuals})
        runner_source = Path(args.agent_runner).resolve()
        isolated_runner, isolated_schema = _stage_isolated_agent_runtime(
            runner_source, args.runner_schema, isolated,
        )
        census = isolated / "source-census.json"
        prompt = isolated / "prompt.txt"
        prompt.write_text(
            "You are the Paid source-only census owner. Work only in this isolated workdir. Read source-manifest.json, "
            "the copied requirements, and every copied buyer source. You cannot read any candidate artifact, producer "
            "ledger, output manifest, project workspace, release tree, repository, docs, or spec. Inspect only the "
            "controller-approved skills copied under skills/, read an applicable SKILL.md fully when one applies, and "
            "use its proven local CLI. Never search outside this isolated workdir. "
            "Every image listed in visual-manifest.json is attached as native vision input; visually inspect every one. "
            "Read coverage-manifest.json. Inspect every kind=recognized_system_tile: its top half is the source and its "
            "bottom half has every Audiveris-recognized notehead filled fluorescent green. Any notehead that remains black "
            "in the bottom half is an omission; any green mark without a source notehead is a false positive. Add "
            "coverage_findings with exactly one object per tile: source_path, page, system, source_sha256, tile_sha256, "
            "omissions, false_positive_detector_ids. omissions is a list of complete census-item objects using the same "
            "source_locator, exact_semantic_value, modifiers, verification fields as items. A recognized count or repeated "
            "generic sentence is not coverage evidence. Every omission must also appear exactly in items. "
            "Create source-census.json with version=1, status=PASS, requirements_sha256, sources copied exactly from the "
            "source manifest using only path and sha256, visual_sources copied exactly from visual-manifest.json, and a "
            "nonempty items array. Enumerate the complete atomic source set "
            "needed to prove the requested transformation. Every item must contain nonempty source_locator, "
            "exact_semantic_value, modifiers, and verification strings. When visual_sources is nonempty, every item's "
            "verification must cite the exact sha256 of the attached page image used to adjudicate it. For notation, "
            "enumerate every notehead including "
            "chords, ties, written accidentals, and carried accidentals; OMR counts are corroboration only, so adjudicate "
            "all source-only detector disagreements from source pixels. Do not infer anything from a candidate count. "
            "Do not create or inspect any deliverable. Return gig_step_result status ok only after the census exists.",
            encoding="utf-8",
        )
        evidence = isolated / "evidence"
        profile = isolated / "source-only.sb"
        quoted_projects = json.dumps(str(root.resolve().parent))
        quoted_live_evidence = json.dumps(str(root.resolve().parent.parent / "evidence"))
        quoted_delivery_evidence = json.dumps(str(root.resolve().parent.parent / "delivery-evidence"))
        quoted_releases = json.dumps(str(code_root.resolve().parent))
        quoted_checkout = json.dumps(str(REPO_ROOT))
        profile.write_text(
            "(version 1)\n(allow default)\n"
            f"(deny file-read* (subpath {quoted_projects}))\n"
            f"(deny file-read* (subpath {quoted_live_evidence}))\n"
            f"(deny file-read* (subpath {quoted_delivery_evidence}))\n"
            f"(deny file-read* (subpath {quoted_releases}))\n"
            f"(deny file-read* (subpath {quoted_checkout}))\n"
            + _deny_reads(_operator_denied_paths())
            + f"(deny file-write* (subpath {quoted_projects}))\n",
            encoding="utf-8",
        )
        started = time.time_ns()
        command = [
            "/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, str(isolated_runner),
            "--task-class", "escalation-agent",
            "--prompt-file", str(prompt), "--schema", str(isolated_schema),
            "--evidence-dir", str(evidence), "--task-label", "paid-source-census",
            "--loop", _runner_loop_id(), "--workdir", str(isolated), "--timeout-seconds", "3600",
            "--escalation-reason", "Independent source-only census before paid build",
        ]
        for image in visual_images:
            command += ["--image", str(image)]
        _run(command, "file_builder")
        result, proof = _file_runner_result(evidence, task_label="paid-source-census", started_ns=started)
        value = _load(census)
        items = value.get("items") if isinstance(value, dict) else None
        fields = ("source_locator", "exact_semantic_value", "modifiers", "verification")
        expected_sources = [{"path": row["path"], "sha256": row["sha256"]} for row in sources]
        overlay_rows = [row for row in expected_visuals if row.get("kind") == "recognized_system_tile"]
        source_rows = {row["path"]: row for row in expected_sources}
        source_image_rows = {(row["source_path"], row["page"]): row for row in expected_visuals
                             if row.get("kind") == "source"}
        findings = value.get("coverage_findings") if isinstance(value, dict) else None
        expected_findings = {(row["source_path"], row["page"], row["system"]): row for row in overlay_rows}
        findings_by_page = ({(row.get("source_path"), row.get("page"), row.get("system")): row for row in findings}
                            if isinstance(findings, list) and all(isinstance(row, dict) for row in findings) else {})
        coverage_valid = not overlay_rows or set(findings_by_page) == set(expected_findings)
        for key, overlay in expected_findings.items():
            finding = findings_by_page.get(key, {})
            source = source_rows.get(key[0], {})
            source_image = source_image_rows.get(key[:2], {})
            omissions = finding.get("omissions")
            false_positives = finding.get("false_positive_detector_ids")
            coverage_valid = coverage_valid and (
                finding.get("source_sha256") == source.get("sha256")
                and finding.get("tile_sha256") == overlay.get("sha256")
                and isinstance(omissions, list) and isinstance(false_positives, list)
                and all(isinstance(omission, dict)
                        and all(_text(omission.get(field)) for field in fields)
                        and source_image.get("sha256") in _text(omission.get("verification"))
                        and overlay.get("sha256") in _text(omission.get("verification"))
                        and any(all(item.get(field) == omission.get(field) for field in fields)
                                for item in items or []) for omission in omissions)
            )
        if (result.get("status") != "ok" or value.get("version") != 1 or value.get("status") != "PASS"
                or value.get("requirements_sha256") != requirements_sha256
                or value.get("sources") != expected_sources
                or value.get("visual_sources") != expected_visuals
                or not coverage_valid
                or not isinstance(items, list) or not items
                or any(not isinstance(item, dict) or any(not _text(item.get(field)) for field in fields)
                       or (expected_visuals and not any(
                           visual["sha256"] in _text(item.get("verification"))
                           for visual in expected_visuals)) for item in items)):
            raise Failure("file_builder")
        target = root / "acceptance" / "controller-source-census-v1.json"
        shutil.copyfile(census, target)
        durable_evidence = root / "evidence" / "agent-PAID_SOURCE_CENSUS" / requirements_sha256
        shutil.copytree(evidence, durable_evidence, dirs_exist_ok=True)
        durable_pages = durable_evidence / "source-pages"
        durable_pages.mkdir(parents=True, exist_ok=True)
        durable_visuals: list[dict[str, Any]] = []
        for image, visual in zip(visual_images, expected_visuals):
            durable = durable_pages / image.name
            shutil.copyfile(image, durable)
            durable_visuals.append({**visual, "path": str(durable)})
        _write(root / "context" / "paid-source-census.json", {
            "version": 1, "policy_version": PAID_SOURCE_CENSUS_VERSION,
            "requirements_sha256": requirements_sha256, "sources": sources,
            "visual_sources": durable_visuals,
            "census_path": str(target.resolve()), "census_sha256": _file_snapshot(target)[1],
            **proof,
        })
        return target


def _validated_source_census(root: Path, receipt: dict[str, Any],
                             sources: list[dict[str, Any]]) -> None:
    binding = receipt.get("independent_source_census")
    trust_path = root / "context" / "paid-source-census.json"
    trust = _load(trust_path) if _regular_file(trust_path) else None
    census_path = root / "acceptance" / "controller-source-census-v1.json"
    trusted_sources = trust.get("sources") if isinstance(trust, dict) else None
    receipt_sources = {(str(source.get("path")), source.get("sha256")) for source in sources if isinstance(source, dict)}
    controller_receipt_path = (binding.get("controller_receipt_path") if isinstance(binding, dict) else None) or receipt.get("controller_receipt_path")
    controller_receipt_sha256 = (binding.get("controller_receipt_sha256") if isinstance(binding, dict) else None) or receipt.get("controller_receipt_sha256")
    if (not isinstance(binding, dict) or not isinstance(trust, dict)
            or binding.get("path") != str(census_path.resolve())
            or binding.get("sha256") != _file_snapshot(census_path)[1]
            or controller_receipt_path != str(trust_path.resolve())
            or controller_receipt_sha256 != _file_snapshot(trust_path)[1]
            or trust.get("policy_version") != PAID_SOURCE_CENSUS_VERSION
            or not isinstance(trusted_sources, list)
            or any((source["path"], source["sha256"]) not in receipt_sources for source in trusted_sources)):
        raise ValueError("invalid controller-owned independent source census")


def _file_bundle_snapshots(
    root: Path, *, validate_source_census: bool = True,
) -> tuple[dict[str, Any], dict[str, tuple[int, str]]]:
    manifest_path = root / "delivery" / "paid-work-result.json"
    manifest = _load(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("invalid file manifest")
    paths = {
        "manifest": manifest_path,
        "requirements": Path(_text(manifest.get("requirements_path"))),
        "artifact": Path(_text(manifest.get("artifact_path"))),
        "acceptance": Path(_text(manifest.get("acceptance_evidence_path"))),
    }
    correspondence = _text(manifest.get("source_correspondence_path"))
    if correspondence:
        paths["source_correspondence"] = Path(correspondence)
    resolved_root = root.resolve()
    snapshots: dict[str, tuple[int, str]] = {}
    for key, path in paths.items():
        resolved = path.resolve()
        resolved.relative_to(resolved_root)
        snapshots[key] = _file_snapshot(resolved)
    if correspondence:
        receipt = _load(paths["source_correspondence"])
        mappings = receipt.get("mappings") if isinstance(receipt, dict) else None
        sources = receipt.get("sources") if isinstance(receipt, dict) else None
        if (not isinstance(receipt, dict)
                or receipt.get("version") != 1 or receipt.get("status") != "PASS"
                or receipt.get("artifact_sha256") != snapshots["artifact"][1]
                or not isinstance(sources, list) or not sources
                or not isinstance(mappings, list) or not mappings):
            raise ValueError("invalid source correspondence receipt")
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("invalid source correspondence source")
            source_path = Path(_text(source.get("path")))
            source_path = (resolved_root / source_path if not source_path.is_absolute()
                           else source_path).resolve()
            source_path.relative_to(resolved_root)
            if source.get("sha256") != _file_snapshot(source_path)[1]:
                raise ValueError("stale source correspondence source")
        fields = ("source_locator", "output_locator", "correspondence", "verification")
        if any(not isinstance(mapping, dict) or any(not _text(mapping.get(field)) for field in fields)
               for mapping in mappings):
            raise ValueError("invalid source correspondence mapping")
        if validate_source_census:
            _validated_source_census(root, receipt, sources)
    return manifest, snapshots


def _archive_member_data(artifact: Path, claimed: str) -> tuple[str, bytes]:
    with zipfile.ZipFile(artifact) as archive:
        try:
            return claimed, archive.read(claimed)
        except KeyError:
            members = [name for name in archive.namelist() if name and not name.endswith("/")]
            if len(members) != 1:
                raise
            return members[0], archive.read(members[0])


def _repair_finding_applies(review_state: dict[str, Any], artifact_sha256: str) -> bool:
    return (
        review_state.get("state") == "REPAIR_PENDING"
        and review_state.get("mode") == "file"
        and review_state.get("artifact_sha256") == artifact_sha256
    )


def _normalize_acceptance_delta(root: Path) -> None:
    manifest_path = root / "delivery" / "paid-work-result.json"
    manifest = _load(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("invalid file manifest")
    acceptance_path = Path(_text(manifest.get("acceptance_evidence_path"))).resolve()
    acceptance_path.relative_to(root.resolve())
    acceptance = _load(acceptance_path)
    if not isinstance(acceptance, dict):
        raise ValueError("invalid acceptance delta")
    acceptance_status = acceptance.get("status")
    if acceptance_status not in {"PASS", "REVIEW_READY", "BLOCKED_NON_DELEGABLE"}:
        raise ValueError("invalid acceptance status")
    manifest_status = manifest.get("status")
    if acceptance_status == "PASS":
        if manifest_status == "PASS":
            manifest_status = "ok"
        if manifest_status != "ok":
            raise ValueError("invalid file manifest status")
    elif manifest_status != acceptance_status:
        raise ValueError("acceptance status mismatch")
    decision = _load(root / "context" / "paid-work-decision.json")
    decision_assets = decision.get("required_assets") if isinstance(decision, dict) else None
    required_assets = manifest.get("required_assets")
    if required_assets is None:
        required_assets = decision_assets
    elif isinstance(decision_assets, list) and required_assets != decision_assets:
        contract_diff = _asset_contract_diff(decision_assets, required_assets)
        decision_path = root / "context" / "paid-work-decision.json"
        decision_sha256 = _file_snapshot(decision_path)[1]
        manifest_sha256 = _file_snapshot(manifest_path)[1]
        state = _load(root / "state.json")
        proposed_effect_key = project_ledger.effect_key(
            _text(state.get("adapter")), _text(state.get("talkroom_id")),
            "file_review_submission", decision_sha256,
        )
        source_fact_ids = [f"file:sha256:{decision_sha256}", f"file:sha256:{manifest_sha256}"]
        envelope = project_ledger.capability_result(
            "paid.asset_contract",
            "succeeded" if contract_diff["status"] == "equivalent_wording_normalized" else "needs_work",
            evidence=[{"path": str(decision_path.resolve()), "sha256": decision_sha256},
                      {"path": str(manifest_path.resolve()), "sha256": manifest_sha256}],
            errors=[{"code": "asset_contract_diff", **contract_diff}],
            source_fact_ids=source_fact_ids,
        )
        fact = project_ledger.append_fact(
            root, "asset_contract_compared", contract_diff,
            provenance=[
                {"fact_id": source_fact_ids[0], "path": str(decision_path.resolve()),
                 "sha256": decision_sha256},
                {"fact_id": source_fact_ids[1], "path": str(manifest_path.resolve()),
                 "sha256": manifest_sha256},
            ], capability=envelope, effect=proposed_effect_key,
        )
        _write(root / "context" / "paid-asset-contract-diff.json", {
            "version": 1,
            "status": contract_diff["status"],
            "decision_path": str(decision_path.resolve()),
            "decision_sha256": decision_sha256,
            "manifest_path": str(manifest_path.resolve()),
            "manifest_sha256": manifest_sha256,
            "fact_id": fact["fact_id"], "effect_key": proposed_effect_key,
            "capability_result": envelope,
            **contract_diff,
        })
        if contract_diff["status"] == "equivalent_wording_normalized":
            required_assets = decision_assets
        else:
            raise Failure("file_contract_review")
    artifact_assets = manifest.get("artifact_assets")
    if not isinstance(required_assets, list) or not isinstance(artifact_assets, list):
        raise ValueError("invalid asset contract")
    artifact_path = Path(_text(manifest.get("artifact_path"))).resolve()
    for asset in artifact_assets:
        if not isinstance(asset, dict):
            continue
        asset_path = Path(_text(asset.get("path")))
        asset_path = (root / asset_path if not asset_path.is_absolute() else asset_path).resolve()
        asset_path.relative_to(root.resolve())
        asset["path"] = str(asset_path)
        member = _text(asset.get("archive_member")) if isinstance(asset, dict) else ""
        if not member:
            continue
        if artifact_path.suffix.casefold() != ".zip" or not artifact_path.is_file():
            raise ValueError("invalid archive asset")
        try:
            member, data = _archive_member_data(artifact_path, member)
        except (KeyError, OSError, zipfile.BadZipFile) as error:
            raise ValueError("invalid archive member") from error
        asset["archive_member"] = member
        if isinstance(asset.get("buyer_visible_asset"), str):
            asset["buyer_visible_asset"] = True
        asset["path"] = str(artifact_path)
        asset["bytes"] = len(data)
        asset["sha256"] = hashlib.sha256(data).hexdigest()
        asset["mime_type"] = mimetypes.guess_type(member)[0] or "application/octet-stream"
        if not isinstance(asset.get("provenance"), str):
            asset["provenance"] = json.dumps(
                asset.get("provenance"), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
    if (acceptance_status == "BLOCKED_NON_DELEGABLE"
            and not _text(acceptance.get("blocking_action"))):
        raise ValueError("missing non-delegable blocking action")
    delta = acceptance.get("acceptance_delta")
    if isinstance(delta, str) and delta.strip():
        delta = [delta.strip()]
        acceptance["acceptance_delta"] = delta
        _write(acceptance_path, acceptance)
    if (not isinstance(delta, list) or not delta
            or any(not isinstance(value, str) or not value.strip() for value in delta)):
        raise ValueError("invalid acceptance delta")
    manifest["status"] = manifest_status
    manifest["acceptance_status"] = acceptance_status
    manifest["required_assets"] = required_assets
    if manifest.get("acceptance_delta") != delta:
        manifest["acceptance_delta"] = delta
    _write(manifest_path, manifest)


def _asset_contract_diff(decision_assets: list[Any], manifest_assets: list[Any]) -> dict[str, Any]:
    """Compare stable mechanics; leave buyer-visible semantic changes to the Project Owner."""
    mechanical = ("kind", "minimum_count", "source_authority", "archive_required")

    def indexed(rows: list[Any]) -> dict[str, dict[str, Any]] | None:
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            asset_id = _text(row.get("asset_id"))
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", asset_id) or asset_id in result:
                return None
            result[asset_id] = row
        return result

    decision_by_id, manifest_by_id = indexed(decision_assets), indexed(manifest_assets)
    if decision_by_id is None or manifest_by_id is None:
        return {"status": "owner_review_required", "reason": "invalid_or_duplicate_asset_id",
                "missing_in_manifest": [], "extra_in_manifest": [], "changed": []}
    missing = sorted(set(decision_by_id) - set(manifest_by_id))
    extra = sorted(set(manifest_by_id) - set(decision_by_id))
    changed = []
    wording_only = []
    for asset_id in sorted(set(decision_by_id) & set(manifest_by_id)):
        left, right = decision_by_id[asset_id], manifest_by_id[asset_id]
        fields = [field for field in mechanical if left.get(field) != right.get(field)]
        if fields:
            changed.append({"asset_id": asset_id, "fields": fields})
        elif left.get("buyer_visible_purpose") != right.get("buyer_visible_purpose"):
            wording_only.append(asset_id)
    status = ("equivalent_wording_normalized"
              if not missing and not extra and not changed else "owner_review_required")
    return {"status": status, "reason": "stable_asset_contract_diff",
            "missing_in_manifest": missing, "extra_in_manifest": extra,
            "changed": changed, "wording_only": wording_only}


def _owner_feedback(root: Path, capability: str, errors: list[Any],
                    evidence_paths: list[Path]) -> Path:
    """Persist raw specialist output as facts the same Project Owner can read next."""
    evidence = []
    provenance = []
    for path in evidence_paths:
        path = path.resolve()
        path.relative_to(root.resolve())
        if not _regular_file(path):
            continue
        sha256 = _file_snapshot(path)[1]
        source_fact_id = f"file:sha256:{sha256}"
        evidence.append({"path": str(path), "sha256": sha256})
        provenance.append({"fact_id": source_fact_id, "path": str(path), "sha256": sha256})
    source_fact_ids = [row["fact_id"] for row in provenance]
    structured_errors = [
        error if isinstance(error, dict) else {"code": "validator_error", "detail": str(error)}
        for error in errors
    ]
    envelope = project_ledger.capability_result(
        capability, "needs_work", evidence=evidence, errors=structured_errors,
        source_fact_ids=source_fact_ids,
    )
    fact = project_ledger.append_fact(
        root, "project_owner_feedback", {"capability": capability, "errors": structured_errors},
        provenance=provenance, capability=envelope,
    )
    output = root / "context" / "paid-owner-feedback.json"
    _write(output, {"version": 1, "fact_id": fact["fact_id"],
                    "capability_result": envelope})
    return output


def _file_immutable_inputs(root: Path, context: Path) -> dict[str, tuple[int, str]]:
    compiled = _load(context)
    read_first = compiled.get("combined_context", {}).get("read_these_first")
    if not isinstance(read_first, list):
        raise ValueError("invalid file builder inputs")
    resolved_root = root.resolve()
    snapshots = {str(context.resolve()): _file_snapshot(context)}
    for raw in read_first:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("invalid file builder input")
        candidate = Path(raw)
        path = (resolved_root / candidate if not candidate.is_absolute() else candidate).resolve()
        path.relative_to(resolved_root)
        snapshots[str(path)] = _file_snapshot(path)
    snapshots[str((root / "context" / "paid-work-decision.json").resolve())] = _file_snapshot(
        root / "context" / "paid-work-decision.json"
    )
    snapshots[str((root / "state.json").resolve())] = _file_snapshot(root / "state.json")
    operator_policy = root / "context" / PAID_FILE_OPERATOR_POLICY
    if operator_policy.exists():
        snapshots[str(operator_policy.resolve())] = _file_snapshot(operator_policy)
    for path in (root / "context" / "paid-source-census.json",
                 root / "acceptance" / "controller-source-census-v1.json"):
        if path.is_file():
            snapshots[str(path.resolve())] = _file_snapshot(path)
    return snapshots


def _resumable_file_bundle(root: Path, stable: Path, feedback: str) -> tuple[dict[str, Any], dict[str, tuple[int, str]]] | None:
    try:
        _normalize_acceptance_delta(root)
        manifest, snapshots = _file_bundle_snapshots(root)
        requirements = _load(Path(_text(manifest.get("requirements_path"))))
        observed = _text(requirements.get("feedback_first_observed_at") or requirements.get("observed_at"))
        if requirements.get("feedback_sha256") != feedback or not observed:
            return None
        observed_at = datetime.fromisoformat(observed).timestamp()
        artifact_stat = Path(_text(manifest.get("artifact_path"))).stat()
        if max(artifact_stat.st_mtime, artifact_stat.st_ctime) < observed_at:
            return None
        ok, _ = paid_work_evidence.validate_paid_work(
            root, stable, require_delivery_evidence=False, artifact_judge=paid_work_evidence.STRUCTURE_ONLY,
            allow_fresh_blocked_for_review=True,
        )
        return (manifest, snapshots) if ok else None
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _blocked_file_bundle_for_recheck(
    root: Path,
    review_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, tuple[int, str]]] | None:
    """Return the exact previously blocked artifact for review before any revision.

    New buyer context can resolve a blocker, add a repairable requirement, or merely add
    urgency.  None of those cases authorizes discarding the old safety decision and
    running the owner first.  A fresh reviewer must classify the same artifact against
    the new cumulative context; only a concrete ``needs_revision`` verdict may re-enter
    the builder.
    """
    if (not isinstance(review_state, dict)
            or review_state.get("state") != "REVIEW_BLOCKED"
            or review_state.get("mode") != "file"):
        return None
    try:
        manifest, snapshots = _file_bundle_snapshots(root, validate_source_census=False)
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return (manifest, snapshots) if review_state.get("artifact_sha256") == snapshots["artifact"][1] else None


def _validate_file_authorization(root: Path, stable: Path, feedback: str,
                                 requirements_sha256: str) -> dict[str, Any]:
    _policy_path, _policy, operator_policy_sha256 = _file_operator_policy(
        root, feedback, requirements_sha256,
    )
    receipt = _load(root / "context" / "paid-file-authorization.json")
    manifest, snapshots = _file_bundle_snapshots(root)
    verifier = root / "evidence" / "agent-PAID_FILE_VERIFY"
    summary = _runner_summary(verifier)
    result_path = _consultation_result_path(verifier, summary)
    result = _load(result_path)
    review_state = _load(root / "context" / "paid-review-state.json")
    shipment_basis = receipt.get("shipment_basis") if isinstance(receipt, dict) else None
    review_authorized = _shipment_basis_authorized(shipment_basis, result.get("verdict"))
    if (not isinstance(receipt, dict) or receipt.get("version") != 4
            or receipt.get("buyer_feedback_sha256") != feedback
            or receipt.get("requirements_sha256") != requirements_sha256
            or receipt.get("review_policy_version") != PAID_FILE_POLICY_VERSION
            or receipt.get("operator_policy_sha256") != operator_policy_sha256
            or receipt.get("manifest_sha256") != snapshots["manifest"][1]
            or receipt.get("artifact_sha256") != snapshots["artifact"][1]
            or receipt.get("acceptance_sha256") != snapshots["acceptance"][1]
            or ("source_correspondence" in snapshots and receipt.get("source_correspondence_sha256")
                != snapshots["source_correspondence"][1])
            or receipt.get("verifier_summary_sha256") != hashlib.sha256((verifier / "summary.json").read_bytes()).hexdigest()
            or receipt.get("verifier_result_sha256") != hashlib.sha256(result_path.read_bytes()).hexdigest()
            or receipt.get("reviewer_model") != summary.get("selected_model")
            or (isinstance(review_state, dict)
                and review_state.get("state") == "REPAIR_PENDING"
                and review_state.get("buyer_feedback_sha256") == feedback
                and review_state.get("requirements_sha256") == requirements_sha256)
            or not review_authorized
            or not _text(result.get("reason"))
            or summary.get("task_label") != "paid-file-verifier"
            or summary.get("task_class") != "escalation-agent"
            or summary.get("escalated") is not True
            or (summary.get("selected_provider"), summary.get("selected_model")) not in PAID_RUNNER_CANDIDATES):
        raise ValueError("stale file authorization")
    ok, errors = paid_work_evidence.validate_paid_work(
        root, stable, artifact_judge=lambda *_: ("deliverable", _text(result.get("reason"))),
        allow_review_ready=_load(root / "context" / "paid-work-decision.json").get("delivery_stage") == "review",
    )
    if not ok:
        raise ValueError("invalid authorized file bundle:" + ",".join(errors))
    return manifest


def _rewrite_staging_paths(value: Any, source: Path, target: Path) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_staging_paths(child, source, target) for key, child in value.items()}
    if isinstance(value, list):
        return [_rewrite_staging_paths(child, source, target) for child in value]
    if isinstance(value, str):
        return value.replace(str(source.resolve()), str(target.resolve()))
    return value


def _clone_prior_artifact(source: Path, target: Path) -> None:
    """Give the isolated owner an independent APFS clone without duplicating its bytes."""
    cloned = subprocess.run(["/bin/cp", "-c", str(source), str(target)], capture_output=True)
    if cloned.returncode:
        target.unlink(missing_ok=True)
        shutil.copy2(source, target)


def _prior_artifact_candidates(root: Path) -> list[Path]:
    """Return existing ZIPs already bound by project-owned state or receipts."""
    root = root.resolve()
    candidates: list[Path] = []

    def add(raw: str | Path) -> None:
        path = Path(raw)
        path = path if path.is_absolute() else root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return
        if resolved.suffix.casefold() == ".zip" and _regular_file(resolved) and resolved not in candidates:
            candidates.append(resolved)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and value.casefold().endswith(".zip"):
            add(value)

    for path in sorted((root / "delivery").glob("*.zip")):
        add(path)
    references = [root / "state.json", root / "context" / "current.json"]
    references.extend(sorted((root / "acceptance").rglob("*.json")))
    for path in references:
        try:
            visit(_load(path))
        except (OSError, json.JSONDecodeError):
            continue
    return candidates


def _next_artifact_version(root: Path, prior_candidates: list[Path]) -> str:
    versions: list[int] = []
    values = [path.name for path in (root / "delivery").iterdir()]
    try:
        values.append(_text(_load(root / "state.json").get("current_version")))
    except (AttributeError, OSError, json.JSONDecodeError):
        pass
    values.extend(path.name for path in prior_candidates)
    for value in values:
        versions.extend(int(match.group(1)) for match in re.finditer(
            r"(?:^|-)v(\d+)(?:\D|$)", value,
        ))
    return f"v{max(versions, default=0) + 1}"


def _prepare_file_owner_staging(root: Path, context: Path, staging: Path) -> Path | None:
    shutil.copytree(root / "requirements", staging / "requirements")
    restricted = set(restricted_attachment_paths(root))

    def ignore_private(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        return {name for name in names if (base / name).resolve() in restricted}

    shutil.copytree(root / "source", staging / "source", ignore=ignore_private)
    context_target = staging / "context"
    context_target.mkdir(parents=True, exist_ok=True)
    excluded = {
        "paid-source-census.json", "paid-review-state.json", "paid-file-authorization.json",
        "paid-output-visual-audit.json", "paid-output-audit-runner-error.json",
    }
    for path in (root / "context").iterdir():
        if path.name in excluded or path.is_symlink():
            continue
        target = context_target / path.name
        if path.is_dir():
            shutil.copytree(path, target)
        elif path.is_file():
            shutil.copyfile(path, target)
    rewritten = _rewrite_staging_paths(_load(context), root, staging)
    _write(context_target / "current.json", rewritten)
    shutil.copyfile(root / "state.json", staging / "state.json")
    for name in ("delivery", "acceptance", "work", "evidence"):
        (staging / name).mkdir()
    prior_manifest = root / "delivery" / "paid-work-result.json"
    manifest = _load(prior_manifest) if _regular_file(prior_manifest) else {}
    prior = Path(_text(manifest.get("artifact_path"))) if isinstance(manifest, dict) else Path()
    if prior.is_file():
        prior.resolve().relative_to(root.resolve())
        target = staging / "work" / "prior-artifact" / prior.name
        target.parent.mkdir(parents=True)
        _clone_prior_artifact(prior, target)
        return target
    return None


def _promote_staged_file_bundle(staging: Path, root: Path, expected_version: str) -> None:
    manifest_path = staging / "delivery" / "paid-work-result.json"
    manifest = _load(manifest_path)
    if not isinstance(manifest, dict):
        raise Failure("file_builder")
    if ("customer_message" in manifest
            and (not isinstance(manifest["customer_message"], str)
                 or not manifest["customer_message"].strip()
                 or len(manifest["customer_message"]) > 2400)):
        raise Failure("file_builder")
    for key in ("requirements_path", "artifact_path", "acceptance_evidence_path"):
        path = Path(_text(manifest.get(key)))
        if not path.is_absolute():
            manifest[key] = str((staging / path).resolve())
    manifest["project_root"] = str(staging.resolve())
    _write(manifest_path, manifest)
    _normalize_acceptance_delta(staging)
    manifest, _snapshots = _file_bundle_snapshots(staging, validate_source_census=False)
    if (_text(manifest.get("source_correspondence_path"))
            or manifest.get("artifact_version") != expected_version):
        raise Failure("file_builder")
    artifact = Path(_text(manifest.get("artifact_path"))).resolve()
    acceptance = Path(_text(manifest.get("acceptance_evidence_path"))).resolve()
    artifact.relative_to(staging.resolve())
    acceptance.relative_to(staging.resolve())
    delivery_target = root / "delivery" / artifact.name
    acceptance_target = root / "acceptance" / acceptance.name
    shutil.copyfile(artifact, delivery_target)
    shutil.copyfile(acceptance, acceptance_target)
    promoted = _rewrite_staging_paths(manifest, staging, root)
    promoted["project_root"] = str(root.resolve())
    promoted["requirements_path"] = str((root / "requirements" / "live-buyer-reply.json").resolve())
    promoted["artifact_path"] = str(delivery_target.resolve())
    promoted["acceptance_evidence_path"] = str(acceptance_target.resolve())
    promoted.pop("source_correspondence_path", None)
    for asset in promoted.get("artifact_assets", []):
        if not isinstance(asset, dict):
            continue
        if "provenance" not in asset and asset.get("provenance_class"):
            asset["provenance"] = asset.pop("provenance_class")
        if "archive_member" not in asset and asset.get("archive_member_path"):
            asset["archive_member"] = asset.pop("archive_member_path")
        if asset.get("archive_member"):
            asset["path"] = str(delivery_target.resolve())
    _write(root / "delivery" / "paid-work-result.json", promoted)


def _file_customer_message_instruction() -> str:
    return (
        "Add customer_message to the manifest: write the concise buyer-facing handoff from the complete "
        "conversation and buyer_trust_context, without internal evidence. Lead with the delivered outcome, "
        "use one to three short natural sentences, remove repeated apologies, process narration and evidence "
        "claims, and include only what the buyer needs to review or do next. Only when the latest buyer-side "
        "message is itself an unresolved complaint or cancellation warning, acknowledge it and offer the one "
        "relevant remedy. A later explicit buyer approval supersedes older complaints and cancellation warnings; "
        "do not carry their apology, correction, or cancellation language into the final handoff. "
    )


def _owner_tool_result_instruction() -> str:
    return (
        "If context/paid-tool-results.json exists, inspect its status. For status=success, independently "
        "verify every declared artifact and acceptance hash, provider provenance, commercial-use evidence, "
        "and correspondence to the buyer requirements before using that artifact; a receipt is evidence, "
        "not an automatic approval. For status=failed, treat it as mechanical failure evidence and "
        "semantically choose a different honest skill-supported input or approach. When failed evidence "
        "explicitly marks interrupted_before_effect=true, controller/native-application health failed before "
        "the input produced an effect; after health is repaired and read back, retry the same capability/input "
        "hash. Otherwise never repeat the same capability and input hash."
    )


def _refresh_owner_controller_context(root: Path, staging: Path) -> None:
    target_dir = staging / "context"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in ("paid-tool-results.json", "paid-owner-feedback.json"):
        source, target = root / "context" / name, target_dir / name
        if source.is_file() and not source.is_symlink():
            value = _load(source)
            if name == "paid-tool-results.json" and value.get("status") == "success":
                evidence_dir = staging / "work" / "provider-evidence"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                for field in ("artifact", "acceptance", "rights_and_correspondence"):
                    record = value.get(field)
                    if not isinstance(record, dict):
                        raise Failure("file_builder")
                    original = Path(_text(record.get("path"))).resolve()
                    original.relative_to(root.resolve())
                    digest = _text(record.get("sha256"))
                    if (original.is_symlink() or not _regular_file(original)
                            or not re.fullmatch(r"[0-9a-f]{64}", digest)
                            or hashlib.sha256(original.read_bytes()).hexdigest() != digest):
                        raise Failure("file_builder")
                    copied = evidence_dir / f"{field}-{original.name}"
                    shutil.copyfile(original, copied)
                    record["path"] = str(copied.resolve())
                _write(target, value)
            else:
                shutil.copyfile(source, target)
        else:
            target.unlink(missing_ok=True)


def _run_isolated_file_owner(args, root: Path, context: Path, prompt_text: str,
                             owner_evidence: Path) -> int:
    decision = _load(root / "context" / "paid-work-decision.json")
    requirements_sha256 = _text(decision.get("requirements_sha256"))
    context_inputs_sha256 = _text(decision.get("context_inputs_sha256"))
    if (not re.fullmatch(r"[0-9a-f]{64}", requirements_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", context_inputs_sha256)):
        raise Failure("file_builder")
    with _project_workspace(
        root, f"paid-file-owner-{requirements_sha256[:12]}-",
        resume=True,
    ) as temporary:
        staging = Path(temporary)
        if (staging / "state.json").is_file():
            prior_dir = staging / "work" / "prior-artifact"
        else:
            _prepare_file_owner_staging(root, context, staging)
            prior_dir = staging / "work" / "prior-artifact"
        _refresh_owner_controller_context(root, staging)
        prior_dir.mkdir(parents=True, exist_ok=True)
        for candidate in _prior_artifact_candidates(root):
            target = prior_dir / candidate.name
            if not target.exists():
                _clone_prior_artifact(candidate, target)
        prior_candidates = sorted(prior_dir.glob("*.zip"))
        prompt = staging / "owner.prompt.txt"
        expected_version = _next_artifact_version(root, prior_candidates)
        isolated_prompt = _rewrite_staging_paths(prompt_text, root, staging)
        prior_instruction = (
            f" Prior artifact candidates are under {prior_dir}. Inspect their actual previews and the complete "
            "buyer conversation, then choose the last buyer-accepted visual lineage as the revision base. "
            "Do not assume the highest version is accepted: buyer feedback may reject it. Preserve every "
            "unrelated property of the chosen lineage and change only what later buyer feedback requests."
            if prior_candidates else ""
        )
        prompt.write_text(
            isolated_prompt + prior_instruction
            + f" The exact required artifact_version is {expected_version}; use that version in the "
            "artifact filename, acceptance filename and manifest. Copy required_assets exactly, including "
            "every asset_id and field, from context/paid-work-decision.json into the manifest; never rename, "
            "summarize, regroup, or replace that contract. "
            + _file_customer_message_instruction()
            + "If a required native desktop application cannot be "
            "controlled from this isolated process, do not fake its output and do not ask the buyer or operator "
            "to run it. Write delivery/paid-tool-requests.json with version=1 and a requests array. Each request "
            "must contain capability, input, output and receipt fields using paths relative to this workdir. The currently "
            "available capability is illustrator_native_roundtrip with input, output and receipt fields. Return "
            "blocked after writing the request; the durable controller will execute it outside this sandbox and "
            "resume this same owner to inspect the official receipt and finish the artifact. "
            + _owner_tool_result_instruction(),
            encoding="utf-8",
        )
        staged_evidence = staging / "runner-evidence"
        profile = staging / "owner-only.sb"
        profile.write_text(
            "(version 1)\n(allow default)\n"
            f"(deny file-read* (subpath {json.dumps(str(root.resolve()))}))\n"
            f"(deny file-write* (subpath {json.dumps(str(root.resolve()))}))\n",
            encoding="utf-8",
        )
        started = time.time_ns()
        command = [
            "/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, str(args.agent_runner),
            "--task-class", "escalation-agent",
            "--prompt-file", str(prompt), "--schema", str(args.runner_schema),
            "--evidence-dir", str(staged_evidence), "--task-label", "paid-file-owner",
            "--loop", _runner_loop_id(), "--workdir", str(staging), "--timeout-seconds", "3600",
            "--escalation-reason", "One isolated paid owner must build the buyer deliverable",
        ]
        try:
            for owner_round in range(2):
                try:
                    _run(command, "file_builder")
                except Failure:
                    if not (staging / "delivery" / "paid-tool-requests.json").is_file():
                        raise
                try:
                    executed = _execute_owner_tool_requests(staging, REPO_ROOT)
                except Failure:
                    _persist_owner_tool_failure(staging, root)
                    raise
                if not executed:
                    break
                if owner_round:
                    raise Failure("file_builder")
                prompt.write_text(
                    prompt.read_text(encoding="utf-8")
                    + " The controller executed every request in delivery/paid-tool-requests.json. Read "
                    "delivery/paid-tool-results.json and the exact receipt files, inspect the resulting outputs, "
                    "then rebuild and verify the final artifact. Do not repeat a completed request.",
                    encoding="utf-8",
                )
        finally:
            if staged_evidence.is_dir():
                shutil.copytree(staged_evidence, owner_evidence, dirs_exist_ok=True)
                summary_path = owner_evidence / "summary.json"
                summary = _load(summary_path)
                if isinstance(summary, dict) and _text(summary.get("result_path")):
                    summary["result_path"] = str(
                        (owner_evidence / Path(_text(summary["result_path"])).name).resolve()
                    )
                    _write(summary_path, summary)
        owner, _proof = _file_runner_result(
            owner_evidence, task_label="paid-file-owner", started_ns=started,
        )
        if owner.get("status") != "ok" or step_result_status.status_from_evidence(owner_evidence) != "ok":
            raise Failure("file_builder")
        try:
            _promote_staged_file_bundle(staging, root, expected_version)
            (root / "context" / "paid-tool-results.json").unlink(missing_ok=True)
        except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure) as error:
            manifest = _load(staging / "delivery" / "paid-work-result.json")
            acceptance_path = Path(_text(manifest.get("acceptance_evidence_path"))) if isinstance(manifest, dict) else Path()
            if acceptance_path and not acceptance_path.is_absolute():
                acceptance_path = staging / acceptance_path
            rejected = owner_evidence / "rejected-candidate"
            rejected.mkdir(parents=True, exist_ok=True)
            for candidate in (
                Path(_text(manifest.get("artifact_path"))) if isinstance(manifest, dict) else Path(),
                acceptance_path,
                staging / "delivery" / "paid-work-result.json",
            ):
                try:
                    candidate = candidate.resolve()
                    candidate.relative_to(staging.resolve())
                except (OSError, ValueError):
                    continue
                if candidate.is_file():
                    shutil.copy2(candidate, rejected / candidate.name)
            _write(owner_evidence / "promotion-error.json", {
                "version": 1,
                "error_type": type(error).__name__,
                "error": str(error),
                "expected_artifact_version": expected_version,
                "manifest": manifest,
                "acceptance": _load(acceptance_path) if acceptance_path.is_file() else None,
            })
            raise
        return started


def _execute_owner_tool_requests(staging: Path, code_root: Path) -> int:
    """Execute narrow mechanical desktop capabilities outside the model sandbox.

    The model owns the semantic decision to request a tool. The controller owns the
    OS capability boundary, validates every path, and records exact command output.
    """
    request_path = staging / "delivery" / "paid-tool-requests.json"
    if not request_path.is_file():
        return 0
    value = _load(request_path)
    requests = value.get("requests") if isinstance(value, dict) else None
    if isinstance(value, dict) and value.get("version") == 1 and requests == []:
        request_path.unlink()
        return 0
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(requests, list) or not 1 <= len(requests) <= 4):
        raise Failure("file_builder")
    tool = code_root / "skills" / "design" / "illustrator-native" / "scripts" / "illustrator_native_roundtrip.py"
    results: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        capability = (request.get("capability", request.get("tool"))
                      if isinstance(request, dict) else None)
        if capability != "illustrator_native_roundtrip":
            raise Failure("file_builder")
        resolved: dict[str, Path] = {}
        for field, suffixes in (("input", {".svg", ".pdf"}), ("output", {".ai"}), ("receipt", {".json"})):
            raw = request.get(field)
            if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
                raise Failure("file_builder")
            path = (staging / raw).resolve()
            try:
                path.relative_to(staging.resolve())
            except ValueError as error:
                raise Failure("file_builder") from error
            if path.suffix.casefold() not in suffixes:
                raise Failure("file_builder")
            resolved[field] = path
        if not resolved["input"].is_file() or resolved["input"].stat().st_size == 0:
            raise Failure("file_builder")
        lock_path = Path(os.environ.get("GIG_STATE_DIR", Path.home() / "gig")) / ".paid-desktop-tool.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            completed = subprocess.run(
                [sys.executable, str(tool), str(resolved["input"]), str(resolved["output"]),
                 "--receipt", str(resolved["receipt"])],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=360,
            )
        result = {
            "index": index, "capability": capability, "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:],
        }
        results.append(result)
        if completed.returncode or not resolved["output"].is_file() or not resolved["receipt"].is_file():
            _write(staging / "delivery" / "paid-tool-results.json", {"version": 1, "results": results})
            raise Failure("file_builder")
    _write(staging / "delivery" / "paid-tool-results.json", {"version": 1, "results": results})
    request_path.rename(staging / "delivery" / "paid-tool-requests.executed.json")
    return len(results)


def _tool_failure_before_input_effect(results: object) -> bool:
    return (
        isinstance(results, list) and bool(results)
        and all(
            isinstance(row, dict) and not _text(row.get("stdout"))
            and (
                row.get("returncode") == -15
                or ("_ensure_responsive" in _text(row.get("stderr"))
                    and "app.version" in _text(row.get("stderr"))
                    and "timed out" in _text(row.get("stderr")))
            )
            for row in results
        )
    )


def _refresh_owner_tool_failure_instruction(root: Path) -> None:
    """Upgrade durable mechanical evidence using the current effect boundary."""
    path = root / "context" / "paid-tool-results.json"
    if not path.is_file():
        return
    value = _load(path)
    if not isinstance(value, dict) or value.get("status") != "failed":
        return
    before_effect = _tool_failure_before_input_effect(value.get("results"))
    value["interrupted_before_effect"] = before_effect
    if before_effect:
        value["instruction"] = (
            "Mechanical tool evidence only. The controller or native-application health check failed before the "
            "input produced any tool output or effect; after controller/application health is repaired and read "
            "back, retry the same capability and input hash."
        )
        _write(path, value)


def _persist_owner_tool_failure(staging: Path, root: Path) -> None:
    """Preserve mechanical failure facts for the next semantic owner decision."""
    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: sanitize(child) for key, child in value.items()}
        if isinstance(value, list):
            return [sanitize(child) for child in value]
        if isinstance(value, str):
            return value.replace(str(staging), "/paid-owner-workdir").replace(
                str(staging.resolve()), "/paid-owner-workdir")
        return value

    request_path = staging / "delivery" / "paid-tool-requests.json"
    result_path = staging / "delivery" / "paid-tool-results.json"
    if not request_path.is_file() or not result_path.is_file():
        return
    request = _load(request_path)
    result = _load(result_path)
    requests = request.get("requests") if isinstance(request, dict) else None
    if (not isinstance(request, dict) or request.get("version") != 1
            or not isinstance(requests, list) or not 1 <= len(requests) <= 4
            or not isinstance(result, dict) or result.get("version") != 1):
        return
    inputs = []
    for row in requests:
        raw = row.get("input") if isinstance(row, dict) else None
        if not isinstance(raw, str) or Path(raw).is_absolute():
            continue
        path = (staging / raw).resolve()
        try:
            path.relative_to(staging.resolve())
        except ValueError:
            continue
        if path.is_file():
            inputs.append({
                "path": raw,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    results = sanitize(result.get("results", []))
    interrupted_before_effect = _tool_failure_before_input_effect(results)
    instruction = (
        "Mechanical tool evidence only. The controller or native-application health check failed before the "
        "input produced any tool output or effect; after controller/application health is repaired and read back, "
        "retry the same capability and input hash."
        if interrupted_before_effect else
        "Mechanical tool evidence only. Read the buyer context and choose a different honest "
        "skill-supported input or approach; do not repeat the same capability and input hash."
    )
    _write(root / "context" / "paid-tool-results.json", {
        "version": 1,
        "status": "failed",
        "instruction": instruction,
        "interrupted_before_effect": interrupted_before_effect,
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
        "requests": requests,
        "inputs": inputs,
        "results": sanitize(result.get("results", [])),
    })


def _build_and_authorize_file(args, item_path: Path, root: Path, item: dict[str, Any],
                              feedback: str, requirements_sha256: str, base: Path,
                              stable: Path) -> dict[str, Any]:
    code_root = REPO_ROOT
    _refresh_owner_tool_failure_instruction(root)
    context = root / "context" / "current.json"
    _run([sys.executable, str(args.context_compiler), "--project-root", str(root),
          "--queue-item", str(item_path), "--output", str(context)], "context_compile")
    review_ready_allowed = _load(root / "context" / "paid-work-decision.json").get("delivery_stage") == "review"
    operator_policy_path, operator_policy, operator_policy_sha256 = _file_operator_policy(
        root, feedback, requirements_sha256,
    )
    resumed = _resumable_file_bundle(root, stable, feedback)
    review_state_path = root / "context" / "paid-review-state.json"
    review_state = _load(review_state_path) if _regular_file(review_state_path) else {}
    finding = ""
    blocked_recheck_finding = ""
    if (resumed is not None and isinstance(review_state, dict)
            and _repair_finding_applies(review_state, resumed[1]["artifact"][1])
            and review_state.get("review_policy_version") == PAID_FILE_POLICY_VERSION
            and review_state.get("operator_policy_sha256") == operator_policy_sha256
            and review_state.get("buyer_feedback_sha256") == feedback
            and review_state.get("requirements_sha256") == requirements_sha256):
        finding = (_text(review_state.get("finding"))
                   or "The prior reviewer requested changes; revise the existing artifact line.")
    if (resumed is not None and isinstance(review_state, dict)
            and review_state.get("state") == "REVIEW_BLOCKED"
            and review_state.get("mode") == "file"
            and review_state.get("review_policy_version") == PAID_FILE_POLICY_VERSION
            and review_state.get("operator_policy_sha256") == operator_policy_sha256
            and review_state.get("buyer_feedback_sha256") == feedback
            and review_state.get("requirements_sha256") == requirements_sha256
            and review_state.get("artifact_sha256") == resumed[1]["artifact"][1]):
        finding = (_text(review_state.get("finding"))
                   or "The prior reviewer could not approve the artifact; revise the same artifact line.")
    if resumed is None:
        blocked_bundle = _blocked_file_bundle_for_recheck(root, review_state)
        if blocked_bundle is not None:
            resumed = blocked_bundle
            blocked_recheck_finding = (_text(review_state.get("finding"))
                                       or "The prior fresh reviewer could not authorize this exact artifact.")
    try:
        source_census = _prepare_source_census(args, root, requirements_sha256, code_root)
        bound = _file_immutable_inputs(root, context)
        requirements_before = _requirements_snapshot(root)
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise Failure("file_builder", "source_census_or_input_binding") from error
    builder_prompt = base / "file" / "builder.prompt.txt"
    builder_prompt.parent.mkdir(parents=True, exist_ok=True)
    owner_instructions = (
        "You are the sole paid task owner. Work only inside PROJECT_ROOT. Read context/current.json, "
        "every combined_context.read_these_first file, requirements/live-buyer-reply.json, state.json, and "
        "context/paid-work-decision.json. If context/paid-asset-contract-diff.json or "
        "context/paid-owner-feedback.json exists, read its complete raw structured errors and source fact ids, "
        "semantically repair the underlying work or manifest, and do not merely rename an error state. "
        f"Before creating anything, search {code_root} with rg for existing relevant SKILL.md files and production CLIs, "
        "read the applicable skill fully, and use its proven CLI contract instead of reimplementing the workflow. "
        "Choose tools from the complete buyer context and required output, never from a hardcoded buyer name, category, or keyword router. "
        "Create the actual buyer-facing deliverable, not a plan, "
        "status report, transaction summary, or promise. Create exactly the next vN artifact under delivery/, "
        "a JSON acceptance file whose status is exactly PASS, REVIEW_READY, or BLOCKED_NON_DELEGABLE and whose "
        "acceptance_delta is a nonempty JSON array of nonempty strings. Use PASS for a complete artifact ready "
        "for the current delivery stage; when delivery_stage is review, later buyer approval is not a prerequisite. "
        "Use REVIEW_READY only for an explicitly permitted incomplete buyer-review draft, and "
        "BLOCKED_NON_DELEGABLE when an account-owner input cannot truthfully be delegated; the blocked acceptance "
        "must include blocking_action with the one exact minimum owner action. Create delivery/paid-work-result.json "
        "with status 'ok' for PASS or the exact same non-PASS status, "
        "binding project_root, requirements_path, artifact_path, artifact_version, acceptance_evidence_path, "
        "acceptance_status, acceptance_delta, package_sha256, and artifact_assets. Each artifact_assets entry must "
        "bind a buyer-visible asset with asset_id, buyer_visible_asset, path, non-zero bytes, mime_type, type, "
        "sha256, provenance, and archive_member when applicable. For an archive member, path must name the "
        "produced ZIP and archive_member must name the exact member inside it. Copy acceptance_delta exactly from the acceptance "
        "file without paraphrasing it. Self-check every accumulated requirement against the actual produced artifact. "
        "When the semantic decision has unresolved items, produce every useful non-blocked portion now, state the exact "
        "remaining limitation without placeholders or invented facts, and claim PASS only for that bounded required_output; "
        "otherwise satisfy the complete accumulated request. Open/read the artifact and correct omissions before PASS. "
        "When a prior review identifies one defect, derive its failure class, enumerate analogous instances across the "
        "complete source and output, repair every confirmed instance, and add class-wide regression evidence. Never patch "
        "only the named locator while leaving the same defect elsewhere. "
        "This is the physically isolated production phase. Build only from files present in this staging root. Do not create "
        "a source-correspondence receipt or add source_correspondence_path to the manifest; a separate controller-owned fresh "
        "reviewer performs post-hash correspondence after promotion. Prior candidates, controller census, proof receipts and "
        "the durable project are intentionally inaccessible. "
        "Distinguish buyer-visible deliverable requirements from application qualifications and preferred production tools. "
        "Never claim use of an unavailable tool. A named tool is a delivery blocker only when the contract explicitly requires "
        "that editable source/project or proof of that tool's use as a delivered output; otherwise use the proven available CLI "
        "to create the requested buyer-visible result. "
        "Do not use Docker, Hermes, gig_pass.sh, network submission, browser mutation, fake data, placeholders, or mocks. "
        "Return the gig_step_result schema only after those files exist."
    )
    if operator_policy_path is not None:
        owner_instructions += (
            f" Read the exact account-owner policy {operator_policy_path} (SHA256 "
            f"{operator_policy_sha256}) and obey its scoped directives: "
            f"{json.dumps(operator_policy['directives'], ensure_ascii=False)}. It may resolve workflow/tool conflicts, "
            "but it never permits a false provenance claim or waives buyer-visible quality."
        )
    verifier_prompt = base / "file" / "verifier.prompt.txt"
    verifier_evidence = root / "evidence" / "agent-PAID_FILE_VERIFY"
    verdict: dict[str, Any] = {}
    proof: dict[str, Any] = {}
    audit_path = None
    audit_snapshot = None
    review_images: list[Path] = []
    reference_images: list[Path] = []
    visual_snapshots: dict[str, tuple[int, str]] = {}
    state_matches_cycle = (
        isinstance(review_state, dict)
        and review_state.get("review_policy_version") == PAID_FILE_POLICY_VERSION
        and review_state.get("operator_policy_sha256") == operator_policy_sha256
        and review_state.get("buyer_feedback_sha256") == feedback
        and review_state.get("requirements_sha256") == requirements_sha256
    )
    prior_round = (int(review_state.get("round", 0))
                   if state_matches_cycle and review_state.get("state") == "REPAIR_PENDING" else 0)
    review_already_completed = (
        state_matches_cycle
        and review_state.get("state") == "REPAIR_PENDING"
        and review_state.get("verdict") == "needs_revision"
        and prior_round >= MAX_FILE_REVIEW_ITERATIONS
    )
    start_round = min(prior_round + 1, MAX_FILE_REVIEW_ITERATIONS)
    shipment_basis = "reviewer_approved"
    review_rounds = range(start_round, MAX_FILE_REVIEW_ITERATIONS + 1)
    for review_round in review_rounds:
        if resumed is None or finding:
            correction = (" A fresh reviewer rejected the prior artifact. Create a corrected next version that resolves "
                          f"this finding: {finding}" if finding else "")
            builder_prompt.write_text(owner_instructions + correction, encoding="utf-8")
            owner_evidence = root / "evidence" / "agent-PAID_FILE_OWNER"
            try:
                owner_started = _run_isolated_file_owner(
                    args, root, context, owner_instructions + correction, owner_evidence,
                )
            except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise Failure("file_builder", "isolated_file_owner") from error
            _normalize_acceptance_delta(root)
            manifest, snapshots = _file_bundle_snapshots(root)
            for key in ("manifest", "artifact", "acceptance"):
                path = (root / "delivery" / "paid-work-result.json" if key == "manifest" else
                        Path(manifest[f"{key}_path" if key == "artifact" else "acceptance_evidence_path"]))
                stat = path.stat()
                fresh_ns = max(stat.st_mtime_ns, stat.st_ctime_ns) if key == "artifact" else stat.st_mtime_ns
                if fresh_ns <= owner_started:
                    raise Failure("file_builder")
        else:
            manifest, snapshots = resumed
        if (_requirements_snapshot(root) != requirements_before
                or _file_immutable_inputs(root, context) != bound
                or paid_remote_result.requirements_digest(root, feedback) != requirements_sha256):
            raise Failure("requirements_toctou")
        review_images = _file_review_images(root, snapshots["artifact"][1], finding)
        ok, errors = paid_work_evidence.validate_paid_work(
            root, stable, require_delivery_evidence=False, artifact_judge=paid_work_evidence.STRUCTURE_ONLY,
            allow_fresh_blocked_for_review=True, allow_review_ready=review_ready_allowed,
        )
        if not ok:
            _owner_feedback(root, "paid.file_structure", errors, [
                root / "delivery" / "paid-work-result.json",
                Path(_text(manifest.get("acceptance_evidence_path"))),
            ])
            raise Failure("file_owner_feedback")
        audit_path, audit_images = (None, [])
        if "source_correspondence" in snapshots:
            audit_path, audit_images = _prepare_blind_output_audit(
                args, root, manifest, snapshots["artifact"][1], snapshots["source_correspondence"][1],
            )
            review_images += [path for path in audit_images if path not in review_images]
        reference_images = _file_reference_images(root, manifest)
        if Path(manifest["artifact_path"]).suffix.casefold() in {".mp4", ".mov", ".m4v", ".webm"} and not review_images:
            raise Failure("file_visual_evidence")
        visual_snapshots = {
            str(path): _file_snapshot(path) for path in review_images + reference_images
        }
        audit_snapshot = _file_snapshot(audit_path) if audit_path is not None else None
        census_snapshot = _file_snapshot(source_census) if source_census is not None else None
        census_receipt = root / "context" / "paid-source-census.json"
        census_receipt_snapshot = (_file_snapshot(census_receipt)
                                   if source_census is not None else None)
        if review_already_completed:
            summary_path = verifier_evidence / "summary.json"
            prior_summary = _load(summary_path)
            prior_result_path = _consultation_result_path(verifier_evidence, prior_summary)
            prior_verdict = _load(prior_result_path)
            if prior_verdict.get("verdict") != "needs_revision":
                raise Failure("file_validation")
            proof = {
                "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                "result_sha256": hashlib.sha256(prior_result_path.read_bytes()).hexdigest(),
                "selected_provider": _text(prior_summary.get("selected_provider")),
                "selected_model": _text(prior_summary.get("selected_model")),
            }
            shipment_basis = "single_material_review_repaired"
            verdict = {"verdict": "deliverable", "reason": f"Owner repaired the one material finding: {finding}"}
            break
        policy_instruction = (
            "No scoped account-owner policy exists."
            if operator_policy_path is None else
            f"The exact account-owner policy is {operator_policy_path} with SHA256 {operator_policy_sha256}. "
            f"Its scoped directives are {json.dumps(operator_policy['directives'], ensure_ascii=False)}. "
            "Honor those directives when they resolve workflow/tool conflicts; they do not waive buyer-visible quality "
            "and never authorize a false provenance claim."
        )
        blocked_recheck_instruction = (
            f"This exact artifact was previously REVIEW_BLOCKED for: {blocked_recheck_finding} "
            "The cumulative buyer context or policy has since changed. Decide first whether the new input actually resolves "
            "that blocker. Added urgency, repetition, or a request for explanation does not resolve missing proof. "
            if blocked_recheck_finding else ""
        )
        verifier_prompt.write_text(
            f"You are a fresh read-only reviewer. Independently read the actual artifact {manifest['artifact_path']}, "
            f"the complete accumulated requirements {manifest['requirements_path']}, acceptance evidence "
            f"{manifest['acceptance_evidence_path']}, and compiled context {context}. The exact artifact SHA256 is "
            f"{snapshots['artifact'][1]}, the exact requirements file SHA256 is {snapshots['requirements'][1]}, and the "
            f"accumulated requirements digest is {requirements_sha256}. Inspect the buyer-visible output "
            "itself and every applicable raw domain/Skill receipt. This is one material-risk review, not an improvement or "
            "style review. Block only for: (1) a materially missing explicit buyer requirement, (2) a false or materially "
            "unverified claim, (3) wrong target, duplicate effect, or formal-delivery error, (4) secret, legal, or money risk, "
            "or (5) a corrupt or buyer-unusable artifact. Wording preference, optional additions, cosmetic polish, alternate "
            "approaches, and other nice-to-have improvements are non-blocking; return deliverable for them. Reject plans, "
            "placeholders, corrupt files, material requirement omissions, materially unsupported claims, and unproved required "
            "provenance. Distinguish "
            "a forbidden omission from an explicitly unresolved input in the semantic decision: for the latter, require a "
            "useful completed non-blocked artifact and an honest bounded limitation rather than blocking all output. Distinguish "
            "buyer-visible deliverable requirements from application qualifications and preferred production tools. Never "
            "invent tool provenance; require a named tool only when the contract explicitly requires its editable source/project "
            "or proof of its use as a delivered output. current_state identifies the last buyer-visible artifact and may be older "
            "than this exact candidate; that is not a candidate provenance mismatch. Never mutate, "
            f"submit, send, or rewrite any project file. {policy_instruction} Candidate review images attached to this "
            f"invocation are exactly {json.dumps([str(path) for path in review_images], ensure_ascii=False)}. Visual "
            f"reference images attached to this invocation are exactly "
            f"{json.dumps([str(path) for path in reference_images], ensure_ascii=False)}. You MUST visually inspect every "
            "attached candidate and reference image. "
            f"The controller-owned blind output audit is {audit_path} with SHA256 "
            f"{audit_snapshot[1] if audit_snapshot else 'none'}; its vision agent saw only isolated hash-bound crops and no "
            "expected values. Verify that receipt and independently inspect the same attached crops. Never claim a visual "
            "defect in a frame that was not attached; use raw "
            "receipts only for nonvisual facts about the remaining frames. "
            f"The controller-owned source-only census is {source_census} with SHA256 "
            f"{census_snapshot[1] if census_snapshot else 'none'}, and its controller receipt is {census_receipt} with SHA256 "
            f"{census_receipt_snapshot[1] if census_receipt_snapshot else 'none'}. The isolated producer was physically denied "
            "read access to the durable project, census, prior candidates and prior receipts. There is intentionally no "
            "producer-authored source-correspondence receipt. You are the separate post-hash correspondence authority: read the "
            "fixed census, raw buyer sources and actual candidate, independently compare every semantic value and modifier, and "
            "sample visual source/output regions directly. Counts, layout checks and owner assertions are not proof. Return "
            "needs_revision only for a concrete material-risk mismatch in the five blocking classes, including every analogous "
            "instance of its failure class. "
            "Return undeterminable only when the available local tools and evidence cannot truthfully decide correspondence. "
            "When the exact buyer source and candidate are locally readable with existing available tools, "
            f"{blocked_recheck_instruction}"
            "perform the comparison yourself rather than deferring to producer provenance. "
            "correctable buyer-visible defect proved by an attached candidate/reference comparison or deterministic receipt, "
            "and state the requirement, evidence, and repair. Before returning one repairable finding, inspect the complete "
            "source and output for analogous instances of the same failure class and include every confirmed instance in one "
            "bounded finding when the available evidence permits it. A repair is correctable only when the project or release proves "
            "that every tool needed for that specific repair is currently available and the repair needs no new paid license, "
            "signup, account change, or false provenance claim. If a candidate has both a deterministic repairable defect and "
            "a separate unavailable-tool or provenance blocker, return needs_revision for the repairable material defect. "
            "The owner gets one repair pass; there is no second reviewer round. If required "
            "provenance is absent and no independently repairable defect remains, return undeterminable, never needs_revision. "
            "If visual evidence is insufficient, return undeterminable; "
            "undeterminable and semantic refusal verdicts never authorize discarding the artifact. Return only artifact_judgement "
            "schema; verdict=deliverable only when every non-overridden accumulated requirement is proved by direct evidence.",
            encoding="utf-8",
        )
        verifier_started = time.time_ns()
        verifier_command = [
            sys.executable, str(args.agent_runner), "--task-class", "escalation-agent",
            "--prompt-file", str(verifier_prompt), "--schema", str(args.artifact_schema),
            "--evidence-dir", str(verifier_evidence), "--task-label", "paid-file-verifier",
            "--loop", _runner_loop_id(), "--workdir", str(root), "--timeout-seconds", "1800", "--read-only",
            "--escalation-reason", "Fresh independent review before paid submission",
        ]
        for image in review_images + reference_images:
            verifier_command += ["--image", str(image)]
        _run_private_model_serialized(root, verifier_command, "paid-file-verifier", "file_verifier")
        verdict, proof = _file_runner_result(
            verifier_evidence, task_label="paid-file-verifier", started_ns=verifier_started,
        )
        disposition = _file_review_disposition(verdict.get("verdict"))
        if disposition == "approve" and _text(verdict.get("reason")):
            break
        finding = _text(verdict.get("reason")) or "The reviewer did not prove the artifact deliverable."
        verdict_path = _consultation_result_path(verifier_evidence)
        _owner_feedback(root, "paid.file_evaluator", [verdict], [verdict_path])
        if _review_ready_may_ship(verdict.get("verdict"), review_ready_allowed, review_round):
            shipment_basis = "max_review_iterations_review_ready"
            break
        if disposition == "repair" and review_round == MAX_FILE_REVIEW_ITERATIONS:
            _write(root / "context" / "paid-review-state.json", {
                "version": 1, "state": "REPAIR_PENDING", "mode": "file",
                "review_policy_version": PAID_FILE_POLICY_VERSION,
                "operator_policy_sha256": operator_policy_sha256,
                "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
                "artifact_sha256": snapshots["artifact"][1], "round": review_round,
                "verdict": _text(verdict.get("verdict")), "finding": finding,
            })
            correction = (
                " The one material-risk review found this bounded defect. Repair this failure class and every "
                f"confirmed analogous instance once, then self-check the complete artifact: {finding}"
            )
            owner_started = _run_isolated_file_owner(
                args, root, context, owner_instructions + correction,
                root / "evidence" / "agent-PAID_FILE_OWNER",
            )
            _normalize_acceptance_delta(root)
            manifest, snapshots = _file_bundle_snapshots(root)
            for key in ("manifest", "artifact", "acceptance"):
                path = (root / "delivery" / "paid-work-result.json" if key == "manifest" else
                        Path(manifest[f"{key}_path" if key == "artifact" else "acceptance_evidence_path"]))
                stat = path.stat()
                fresh_ns = max(stat.st_mtime_ns, stat.st_ctime_ns) if key == "artifact" else stat.st_mtime_ns
                if fresh_ns <= owner_started:
                    raise Failure("file_builder")
            if (_requirements_snapshot(root) != requirements_before
                    or _file_immutable_inputs(root, context) != bound
                    or paid_remote_result.requirements_digest(root, feedback) != requirements_sha256):
                raise Failure("requirements_toctou")
            review_images = _file_review_images(root, snapshots["artifact"][1], finding)
            ok, errors = paid_work_evidence.validate_paid_work(
                root, stable, require_delivery_evidence=False,
                artifact_judge=paid_work_evidence.STRUCTURE_ONLY,
                allow_fresh_blocked_for_review=True, allow_review_ready=review_ready_allowed,
            )
            if not ok:
                _owner_feedback(root, "paid.file_structure", errors, [
                    root / "delivery" / "paid-work-result.json",
                    Path(_text(manifest.get("acceptance_evidence_path"))),
                ])
                raise Failure("file_owner_feedback")
            reference_images = _file_reference_images(root, manifest)
            visual_snapshots = {
                str(path): _file_snapshot(path) for path in review_images + reference_images
            }
            audit_path = None
            audit_snapshot = None
            shipment_basis = "single_material_review_repaired"
            verdict = {"verdict": "deliverable", "reason": f"Owner repaired the one material finding: {finding}"}
            break
        if review_round == MAX_FILE_REVIEW_ITERATIONS:
            _write(root / "context" / "paid-review-state.json", {
                "version": 1, "state": "REVIEW_BLOCKED", "mode": "file",
                "review_policy_version": PAID_FILE_POLICY_VERSION,
                "operator_policy_sha256": operator_policy_sha256,
                "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
                "artifact_sha256": snapshots["artifact"][1], "round": review_round,
                "verdict": _text(verdict.get("verdict")), "finding": finding,
            })
            raise Failure("file_validation")
        _write(root / "context" / "paid-review-state.json", {
            "version": 1, "state": "REPAIR_PENDING", "mode": "file",
            "review_policy_version": PAID_FILE_POLICY_VERSION,
            "operator_policy_sha256": operator_policy_sha256,
            "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
            "artifact_sha256": snapshots["artifact"][1], "round": review_round, "finding": finding,
        })
        resumed = None
    current_manifest, current_snapshots = _file_bundle_snapshots(root)
    if (current_manifest != manifest or current_snapshots != snapshots
            or _requirements_snapshot(root) != requirements_before
            or _file_immutable_inputs(root, context) != bound
            or (audit_path is not None and _file_snapshot(audit_path) != audit_snapshot)
            or {str(path): _file_snapshot(path) for path in review_images + reference_images}
            != visual_snapshots):
        raise Failure("file_toctou")
    _write(stable, manifest)
    ok, errors = paid_work_evidence.validate_paid_work(
        root, stable, artifact_judge=lambda *_: ("deliverable", _text(verdict.get("reason"))),
        allow_fresh_blocked_for_review=True, allow_review_ready=review_ready_allowed,
    )
    if not ok:
        _owner_feedback(root, "paid.file_presend_validation", errors, [
            root / "delivery" / "paid-work-result.json",
        ])
        raise Failure("file_owner_feedback")
    paid_work_evidence.resolve_fresh_blocked_after_review(
        root, manifest["requirements_path"], feedback, snapshots["artifact"][1],
    )
    ok, errors = paid_work_evidence.validate_paid_work(
        root, stable, artifact_judge=lambda *_: ("deliverable", _text(verdict.get("reason"))),
        allow_review_ready=review_ready_allowed,
    )
    if not ok:
        _owner_feedback(root, "paid.file_final_validation", errors, [
            root / "delivery" / "paid-work-result.json",
        ])
        raise Failure("file_owner_feedback")
    _write(root / "context" / "paid-file-authorization.json", {
        "version": 4, "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
        "review_policy_version": PAID_FILE_POLICY_VERSION,
        "operator_policy_sha256": operator_policy_sha256,
        "manifest_sha256": snapshots["manifest"][1], "artifact_sha256": snapshots["artifact"][1],
        "acceptance_sha256": snapshots["acceptance"][1],
        **({"source_correspondence_sha256": snapshots["source_correspondence"][1]}
           if "source_correspondence" in snapshots else {}),
        "reviewer_model": proof["selected_model"],
        "shipment_basis": shipment_basis,
        "review_round": review_round,
        "verifier_summary_sha256": proof["summary_sha256"],
        "verifier_result_sha256": proof["result_sha256"],
    })
    _write(root / "context" / "paid-review-state.json", {
        "version": 1,
        "state": "APPROVED",
        "mode": "file",
        "review_policy_version": PAID_FILE_POLICY_VERSION,
        "operator_policy_sha256": operator_policy_sha256,
        "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
        "artifact_sha256": snapshots["artifact"][1], "round": review_round,
    })
    return manifest


def _prepare_file(args, item_path: Path, root: Path, item: dict[str, Any], base: Path,
                  feedback: str) -> dict[str, Any]:
    (root / "delivery").mkdir(parents=True, exist_ok=True)
    requirements_sha256 = paid_remote_result.requirements_digest(root, feedback)
    stable = delivery_queue.evidence_path(args.delivery_evidence_dir, item)
    try:
        manifest = _validate_file_authorization(root, stable, feedback, requirements_sha256)
        repaired = False
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
        manifest = _build_and_authorize_file(
            args, item_path, root, item, feedback, requirements_sha256, base, stable,
        )
        repaired = True
    evidence, blockers = delivery_queue.delivery_gate(item, args.delivery_evidence_dir, args.projects_root)
    try:
        semantic = _current_paid_decision(root, item)
    except (AttributeError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
        semantic = _paid_decision(args, item_path, root, base)
    cadence = {**item, **{key: evidence[key] for key in (
        "project_root", "requirements_path", "artifact_path", "artifact_version",
        "acceptance_evidence_path", "acceptance_status", "package_sha256",
        "acceptance_delta", "recipient_access_required", "required_assets", "artifact_assets",
    ) if key in evidence}, "blockers": blockers,
        "buyer_formal_delivery_hold": item.get("buyer_formal_delivery_hold") is True,
        "latest_message_identity": semantic.get("latest_message_identity"),
        "latest_buyer_message_identity": _latest_official_buyer_identity(
            root, _text(item.get("talkroom_id"))),
        "formal_approval_evidence": semantic.get("formal_approval_evidence")}
    decision = delivery_queue.delivery_decision(cadence)
    if decision.get("mode") not in {"formal", "progress"}:
        _owner_feedback(root, "paid.delivery_gate", [decision], [
            root / "delivery" / "paid-work-result.json",
            root / "requirements" / "live-buyer-reply.json",
        ])
        raise Failure("file_owner_feedback")
    prepared = {
        **cadence, "delivery_evidence": evidence, "delivery_action": decision["mode"],
        "formal_delivery_checkbox": decision["formal_delivery_checkbox"],
        "progress_payload": _file_progress_payload(cadence) if decision["mode"] == "progress" else None,
        "requirements_sha256": requirements_sha256, "_paid_mode": "file",
        "file_repaired": repaired, "file_manifest": str(root / "delivery" / "paid-work-result.json"),
    }
    if manifest.get("package_sha256") != evidence.get("package_sha256"):
        raise Failure("file_validation")
    return prepared


def _legacy_paid_mode(root: Path, feedback: str) -> str:
    mode = _load(root / "delivery" / "paid-work-mode.json")
    if (not isinstance(mode, dict) or mode.get("status") != "ok"
            or mode.get("feedback_sha256") != feedback
            or mode.get("mode") not in {"file", "remote", "answer"}):
        return ""
    return _text(mode.get("mode"))


def _answer_ready(root: Path, item: dict[str, Any]) -> bool:
    try:
        feedback = _text(item.get("buyer_feedback_sha256"))
        intent_path = root / "delivery" / "paid-remote-intent.json"
        intent_mode = _text(_load(intent_path).get("mode")) if intent_path.is_file() else ""
        try:
            decision = _current_paid_decision(root, item)
            answer_mode = decision.get("decision") == "actionable" and decision.get("mode") == "answer"
            semantic_current = True
        except (AttributeError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
            answer_mode = intent_mode == "consultation_answer" and _legacy_paid_mode(root, feedback) == "answer"
            semantic_current = False
        if (not answer_mode or (not semantic_current and intent_mode
                                and intent_mode not in {"answer", "consultation_answer"})):
            return False
        paid_remote_result.requirements_digest(root, feedback)
        if not semantic_current and _remote_revision_required(root, feedback):
            return False
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _remote_revision_required(root: Path, feedback: str) -> bool:
    state = _load(root / "state.json")
    cycle, live = state.get("active_feedback_cycle"), state.get("live_system_delivery")
    return (isinstance(cycle, dict) and isinstance(live, dict)
            and cycle.get("buyer_feedback_sha256") == feedback
            and cycle.get("action") == "resubmit" and cycle.get("phase") == "ACTIONABLE"
            and bool(_text(live.get("target_url"))))


def _remote_mode_required(root: Path, item: dict[str, Any], feedback: str) -> bool:
    """Use legacy remote state only when no current semantic decision is valid."""
    try:
        decision = _current_paid_decision(root, item)
        return decision.get("decision") == "actionable" and decision.get("mode") == "remote"
    except (AttributeError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return _legacy_paid_mode(root, feedback) == "remote" or _remote_revision_required(root, feedback)

def _repair_prompt(root: Path, item: Path, feedback: str, requirements_sha256: str,
                   verifier: bool, cdp_helper: Path,
                   review_delta: list[dict[str, str]] | None = None) -> str:
    code_root = REPO_ROOT
    semantic_contract, semantic_contract_sha256 = _semantic_effect_contract(root)
    role = "fresh read-only remote reviewer" if verifier else "paid remote owner"
    suffix = "remote-verifier" if verifier else "remote-builder"
    tab_owner = f"{os.environ.get('CLOAK_BROWSER_OWNER', f'paid-direct-{root.name}')}-{suffix}"
    mutation = "Never mutate, click submit, or send anything." if verifier else "Mutate only the authenticated target when required, idempotently."
    target_contract = (
        f"Read builder-owned {root / 'delivery/paid-remote-intent.json'} and "
        f"{root / 'delivery/paid-remote-result.json'} without modifying them. Independently verify that live target, "
        "not the Coconala talkroom, and write only verifier-owned result/evidence."
        if verifier else
        f"Use {root / 'delivery/paid-remote-intent.json'} and {root / 'delivery/paid-remote-result.json'} when present; "
        "for a fresh cycle derive the remote target from full project context, then write both records and verify that target."
    )
    verifier_contract = (
        "Write version=1 remote-verifier-result.json under PROJECT_ROOT/evidence/agent-PAID_REMOTE_VERIFY/. "
        "Once the required official checks are sufficient to decide PASS or BLOCKED, write that result immediately before any optional exploration; do not exhaustively inspect unrelated historical attachments or messages. "
        "On PASS write verified=true with matching feedback, target, desired/observed digest, canonical observed_state, "
        "requirements_sha256, message_sha256, customer_attachment, and fresh verifier evidence, then return runner status=ok. "
        "PASS is forbidden merely because one target state matches. Independently prove the complete required_effect and "
        "required_output. Copy the builder's business_outcome only after checking every receipt against the official provider: "
        "required_effect_satisfied=true, required_output_satisfied=true, remaining_work=[], and a nonempty official_receipts "
        "array whose records contain effect_key, official_url, readback_source, and exact_readback=true. If any required work "
        "remains, return blocked; never describe a partial target, draft, internal state, or public proxy as business completion. "
        "For a correctable mismatch write verified=false, classification=quality_mismatch, and a nonempty delta array whose "
        "objects contain only requirement, expected, observed, evidence, and repair strings, then return status=blocked. "
        "For a temporary browser failure use auth_transient or cdp_transient with no delta."
        if verifier else
        "Never write remote-verifier-result.json or verifier-owned evidence."
    )
    ownership = (
        "Never modify paid-remote-intent.json, paid-remote-result.json, paid-answer.json, or any buyer/client surface."
        if verifier else
        "Write project-owned intent/result, authenticated before/after evidence, and a natural Japanese customer_message. "
        "Once the required official checks are sufficient to decide completion or a blocker, write the durable result immediately before any optional exploration; do not exhaustively inspect unrelated historical attachments or messages. "
        "paid-remote-result.json must include business_outcome with required_effect_satisfied, required_output_satisfied, "
        "remaining_work, and official_receipts. remaining_work must always be an array: use a nonempty string array when "
        "blocked and [] only when no work remains. Set both satisfied fields true only after the complete semantic contract has "
        "official provider readback; otherwise preserve progress, write status=blocked and a nonempty blocker in paid-remote-result.json, "
        "and make every wait receipt include nonempty provider, kind, and url or official_url fields, plus either a nonempty readback or both readback_source and exact_readback=true, "
        "and return blocked without manufacturing a completion result. "
        "do not submit to Coconala or use formal delivery."
    )
    correction = ""
    if not verifier and review_delta:
        correction = ("A fresh reviewer rejected the prior result. Correct every structured finding, then re-read the "
                      "live target and rewrite owner evidence: "
                      + json.dumps(review_delta, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + ". ")
    operator_policy_path, operator_policy, operator_policy_sha256 = _file_operator_policy(
        root, feedback, requirements_sha256,
    )
    operator_instruction = (
        "" if operator_policy_path is None else
        f"Read and obey the exact-cycle account-owner policy {operator_policy_path} with SHA256 "
        f"{operator_policy_sha256}: {json.dumps(operator_policy['directives'], ensure_ascii=False)}. "
        "It may resolve account and workflow choices but never permits false claims or waives buyer requirements. "
    )
    return (f"You are the {role}. PROJECT_ROOT={root}. Current queue item={item}. "
            f"Read {root / 'context/current.json'} and every read_these_first path, then read the live target. "
            f"Current buyer feedback SHA256={feedback}. {mutation} "
            "The semantic decision below is the immutable outcome contract for this cycle. You may choose tools, skills, "
            "accounts, and execution steps autonomously, but must not replace its required effect with an easier proxy: "
            f"{json.dumps(semantic_contract, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}. "
            f"Write semantic_contract_sha256={semantic_contract_sha256} into intent, result, and every owned evidence JSON. "
            "The verifier must copy the same digest into its result and evidence and reject any state that does not satisfy "
            "required_effect and required_output. "
            "At this remote owner/verifier stage, required_output_satisfied means the buyer-requested remote output exists, "
            "has official provider readback, and its accurate customer_message is ready for the downstream code-owned Coconala connector; "
            "it does not mean the Coconala message is already sent. That connector runs only after owner and verifier PASS, "
            "then enforces formal delivery off and reads back the exact seller message. Never open or mutate Coconala in this stage. "
            "Never require or guess a fixed Coconala profile ID: a buyer profile link inside a talkroom is not the seller identity, "
            "and Coconala session/send authorization belongs to the code-owned Coconala connector. "
            f"{operator_instruction}"
            f"The canonical accumulated buyer requirements are {root / 'requirements/live-buyer-reply.json'} "
            f"with accumulated requirements SHA256={requirements_sha256}. Independently read that file; its feedback_sha256 must match the current feedback. "
            f"{target_contract} "
            f"Before building raw browser automation, search {code_root} with rg for an existing production adapter and relevant SKILL.md that "
            "support the target and its live readback. Read the skill, use its CLI contract when applicable, and do not reimplement it. "
            "Choose skills from the complete project context and actual target capabilities, never from a hardcoded buyer-name or keyword router. "
            "Before any external mutation, bind every factual claim in the outbound payload to an official source URL or a hash-bound "
            "project source. Omit facts that were not observed; when a missing fact matters, ask the recipient as a concise qualification "
            "question instead of asserting it. Preserve this claim-to-source map in owner evidence for official verification. "
            "Compose the outbound payload from the current recipient's claim map. Never reuse another recipient's payload, canned factual "
            "message mode, or exact attribute value. Immediately before send, compare every literal factual value in the exact composer "
            "text with its bound source; if any value differs, do not send that text and instead omit the value or use a concise "
            "qualification question. Browser scripts may transport and read back the model-approved exact text, but must not choose or "
            "substitute semantic copy. "
            "An authenticated remote session is not proof that it is the correct account. Before signup or asking the buyer for an account, "
            f"run python3 {code_root / 'skills/_shared/resource_resolver.py'} resolve --service <target-host> --capability <action>. "
            "A resolver result of available=false proves only that no reusable resource is registered; it does not prove "
            "the authorized outcome is infeasible. When the buyer contract requires seller signup, account creation, or "
            "resource setup and general browser/email tools can perform it, create or recover the seller-owned resource autonomously. "
            "Missing an exact named skill is not a blocker. Never ask the buyer to supply a seller-owned account, skill, or "
            "setup step that the authorized owner can create. Persist any newly created credential only through the private "
            "credential SSOT contract without exposing its value. "
            "Resource discovery is not live readiness: inspect the selected skill/session in official UI or API before effect. "
            "All independent paid projects run concurrently for observation, mutation, and readback. Each project owns a "
            "distinct browser target and owner identity and never waits for another project merely because the provider account is shared. "
            "When the first channel is unavailable, use the complete project context to resolve another authorized skill, account, "
            "or contact surface that achieves the same buyer outcome. Treat qualification questions as legitimate first contact when "
            "the recipient permits that channel; do not require every fact to be public before contact, and do not stop merely because "
            "one transport is unavailable. One invalid, private, unreachable, or unverified candidate is not batch exhaustion; continue "
            "to the next authorized candidate until the batch outcome is met or a complete scope-bound exhaustion receipt exists. "
            "Do not finalize a partial batch after a command timeout or interruption; resume every unattempted candidate individually, "
            "and require attempted count to equal intended count before any exhaustion result. "
            "Never bypass platform policy, impersonate the buyer, or invent consent. "
            "A matching reusable seller-owned browser identity may serve this project; never infer authorization from login alone. "
            f"Run every leased-browser operation through {code_root / 'skills/browser/with-browser.sh'} <identity> -- <command>, which acquires, exports CDP, "
            "and releases through an EXIT/signal trap; never split browser-guard acquire and release across separate commands. Verify the live "
            "account handle/profile URL in official DOM while that wrapper owns the lease. Bind the observed account/member identity to the matching authorization before reading or mutating it, "
            "and fail closed rather than reuse another client's session or evidence. "
            "Never use Hermes or gig_pass.sh. "
            f"When no authorized registered identity matches, use the existing CloakBrowser daily-driver with a self-owned default-context tab: "
            f"{cdp_helper} open <url> --background --owner {tab_owner}; close that exact target tab and never navigate an existing foreground tab. "
            "Invoke the helper with python3, never python. "
            "Before any external mutation, reconcile all project-owned external-effect receipts with current official "
            "provider and matching bookkeeping readback. Never repeat an effect whose receipt is already verified; "
            "instead carry that verified effect into the canonical desired and observed state. "
            "Keep the operation idempotent and skip a mutation when the desired state is already true. "
            "Use one canonical state contract: observed_state must exactly equal paid-remote-intent.json desired_state in "
            "paid-remote-result.json, authenticated after evidence, and remote-verifier-result.json; desired_state_sha256, "
            "after_state_digest, desired_digest, and observed_digest must be the SHA256 of that same canonical JSON object. "
            "Only arrays named tags or keywords are unordered; every other array is ordered, including image order. "
            f"Write requirements_sha256={requirements_sha256} into paid-remote-intent.json, paid-remote-result.json, and every owned evidence JSON. "
            "Write message_sha256 as the SHA256 of the exact customer_message into intent/result/evidence; reviewer independently checks it. "
            "Each evidence JSON must have top-level target, authenticated=true, requirements_sha256, message_sha256, and observed_state; "
            "after evidence observed_state must exactly equal the canonical desired_state. "
            "Never place credential values in commands, stdout, prompts, or evidence; read existing credential files only at runtime "
            "without echoing or hardcoding their values. "
            "Buyer-prohibited images must be compared by visual/content identity; absence of a filename is not proof. "
            "Content-identical duplicate images do not satisfy a request for another image; compare content hashes and visual identity, "
            "and never claim a replacement unless it is distinct and safe. "
            "For PDF attachments, render or extract every page with existing local PDF tools before deciding. "
            "If the buyer requires a screenshot/file, bind one exact customer_attachment with absolute path, basename, and SHA256; "
            "reviewer must inspect its real content. Otherwise it is null. "
            f"{ownership} {correction}{verifier_contract} Return only the runner schema result.")


def _normalize_builder_result(root: Path) -> None:
    intent_path, result_path = root / "delivery/paid-remote-intent.json", root / "delivery/paid-remote-result.json"
    intent, result = _load(intent_path), _load(result_path)
    outcome = result.get("business_outcome")
    remaining = outcome.get("remaining_work") if isinstance(outcome, dict) else None
    remaining_blocker = (remaining[0].strip() if isinstance(remaining, list) and remaining
                         and isinstance(remaining[0], str) else "")
    if (result.get("status") == "blocked" and not _text(result.get("blocker"))
            and remaining_blocker):
        result["blocker"] = remaining_blocker
    feedback = _text(intent.get("buyer_feedback_sha256") or intent.get("feedback_sha256"))
    if re.fullmatch(r"[0-9a-f]{64}", feedback):
        intent["feedback_sha256"] = intent["buyer_feedback_sha256"] = feedback
        result["feedback_sha256"] = result["buyer_feedback_sha256"] = feedback
    desired = intent.get("desired_state")
    if isinstance(desired, dict):
        digest = paid_remote_result._sha(desired)
        intent["desired_state_sha256"] = intent["desired_digest"] = digest
        result["desired_state_sha256"] = result["desired_digest"] = digest
    raw_after_value = _text(result.get("after_evidence"))
    if not raw_after_value:
        owner_evidence = root / "evidence" / "agent-PAID_REMOTE_OWNER"
        for candidate in sorted(owner_evidence.rglob("*.json"), reverse=True):
            try:
                evidence = _load(candidate)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            official = evidence.get("official_readback")
            outcome = result.get("business_outcome")
            no_effect_wait = (result.get("status") == "blocked" and isinstance(outcome, dict)
                              and outcome.get("required_effect_satisfied") is False
                              and outcome.get("required_output_satisfied") is False)
            if (evidence.get("authenticated") is True
                    and evidence.get("target") == intent.get("target")
                    and evidence.get("requirements_sha256") == intent.get("requirements_sha256")
                    and evidence.get("message_sha256") == intent.get("message_sha256")
                    and paid_remote_result.canonical_equal(evidence.get("observed_state"), desired)
                    and (no_effect_wait or (isinstance(official, dict)
                         and official.get("send_performed") is False
                         and official.get("deduplicated") is True
                         and official.get("exact_customer_message_readback") is True))):
                raw_after_value = str(candidate.relative_to(root))
                result["before_evidence"] = raw_after_value
                result["after_evidence"] = raw_after_value
                if not no_effect_wait:
                    result["status"] = "ok"
                elif not _text(result.get("blocker")) and remaining_blocker:
                    result["blocker"] = remaining_blocker
                result["verified_after"] = True
                break
        if not raw_after_value:
            _write(intent_path, intent)
            _write(result_path, result)
            return
    raw_after = Path(raw_after_value)
    after_path = (root / raw_after if not raw_after.is_absolute() else raw_after).resolve()
    after_path.relative_to(root)
    after = _load(after_path)
    if (after.get("authenticated") is True and after.get("target") == intent.get("target")
            and paid_remote_result.canonical_equal(after.get("observed_state"), intent.get("desired_state"))):
        outcome = result.get("business_outcome")
        if isinstance(outcome, dict) and isinstance(outcome.get("official_receipts"), list):
            source = _text((after.get("official_readback") or {}).get("provider")) or "official readback"
            outcome["official_receipts"] = [
                {"provider": source, "kind": "external_state", "official_url": receipt,
                 "exact_readback": True, "readback_source": raw_after_value}
                if isinstance(receipt, str) and receipt.startswith("https://") else receipt
                for receipt in outcome["official_receipts"]
            ]
        if result.get("status") == "completed":
            result["status"] = "ok"
        if result.get("status") == "ok":
            result["verified_after"] = True
        result["observed_state"] = after["observed_state"]
        result["after_state_digest"] = result["observed_digest"] = intent.get("desired_state_sha256")
        _write(intent_path, intent)
        _write(result_path, result)


def _consultation_attachments(root: Path) -> tuple[list[dict[str, str]], list[Path], list[Path]]:
    requirements = _load(root / "requirements" / "live-buyer-reply.json")
    rows = requirements.get("attachments") if isinstance(requirements, dict) else None
    if not isinstance(rows, list):
        raise Failure("context_compile")
    decision = _load(root / "context" / "paid-work-decision.json")
    identity = decision.get("latest_message_identity") if isinstance(decision, dict) else None
    message_id = _text(identity.get("message_id")) if isinstance(identity, dict) else ""
    if not message_id:
        raise Failure("context_compile")
    rows = [row for row in rows if isinstance(row, dict) and
            _text(row.get("download_reference")).startswith(f"message:{message_id}:attachment:")]
    if not rows:
        return [], [], []
    expected, images, documents = [], [], []
    seen: set[tuple[str, str]] = set()
    source_parent = root / "source"
    source_dir = source_parent / "buyer-attachments"
    if (source_parent.is_symlink() or source_dir.is_symlink()
            or not source_parent.is_dir() or not source_dir.is_dir()):
        raise Failure("context_compile")
    source_root = source_dir.resolve()
    try: source_root.relative_to(root.resolve())
    except ValueError as error: raise Failure("context_compile") from error
    for row in rows:
        if not isinstance(row, dict):
            raise Failure("context_compile")
        filename, digest = _text(row.get("filename")), _text(row.get("sha256"))
        raw = Path(_text(row.get("source_path")))
        if not filename or Path(filename).name != filename or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Failure("context_compile")
        if raw.is_symlink() or not _regular_file(raw):
            raise Failure("context_compile")
        image = raw.resolve()
        try: image.relative_to(source_root)
        except ValueError as error: raise Failure("context_compile") from error
        if image.name != f"{digest[:12]}-{filename}" or hashlib.sha256(image.read_bytes()).hexdigest() != digest:
            raise Failure("context_compile")
        identity = (filename, digest)
        if identity in seen:
            continue
        seen.add(identity)
        expected.append({"filename": filename, "sha256": digest})
        if image in set(restricted_attachment_paths(root)):
            continue
        if _text(row.get("content_type")).startswith("image/") or image.suffix.lower() in {
                ".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}:
            images.append(image)
        else:
            documents.append(image)
    return expected, images, documents


def _consultation_runner_result(evidence: Path, *, task_label: str, task_class: str,
                                model: str, started_ns: int) -> dict[str, Any]:
    summary = _runner_summary(evidence)
    expected = {"status": "success", "task_label": task_label, "task_class": task_class,
                }
    if task_class == "escalation-agent": expected["escalated"] = True
    if (any(summary.get(key) != value for key, value in expected.items())
            or (summary.get("selected_provider"), summary.get("selected_model")) not in PAID_RUNNER_CANDIDATES):
        raise Failure("remote_verifier" if "verifier" in task_label else "remote_builder")
    result_path = _consultation_result_path(evidence, summary)
    now_ns = time.time_ns()
    mtimes = ((evidence / "summary.json").stat().st_mtime_ns, result_path.stat().st_mtime_ns)
    if min(mtimes) <= started_ns or max(mtimes) > now_ns + 1_000_000_000:
        raise Failure("remote_verifier" if "verifier" in task_label else "remote_builder")
    value = _load(result_path)
    if not isinstance(value, dict):
        raise Failure("remote_verifier" if "verifier" in task_label else "remote_builder")
    return value


def _consultation_result_path(evidence: Path, summary: dict[str, Any] | None = None) -> Path:
    summary = summary or _runner_summary(evidence)
    raw = Path(summary["result_path"])
    return (evidence / raw if not raw.is_absolute() else raw).resolve()


def _validate_consultation_result(value: dict[str, Any], expected: list[dict[str, str]],
                                  *, message: str | None = None) -> list[dict[str, str]]:
    reviewed = value.get("reviewed_attachments")
    if value.get("status") not in {"ok", "blocked"} or not isinstance(reviewed, list):
        raise ValueError("invalid consultation result")
    if [{"filename": row.get("filename"), "sha256": row.get("sha256")} for row in reviewed
            if isinstance(row, dict)] != expected:
        raise ValueError("consultation attachments mismatch")
    if any(not _text(row.get("observation")) for row in reviewed):
        raise ValueError("consultation observation missing")
    customer_message = _text(value.get("customer_message"))
    if not customer_message or (message is not None and customer_message != message):
        raise ValueError("consultation message mismatch")
    issues = value.get("issues")
    if not isinstance(issues, list) or any(not _text(issue) for issue in issues):
        raise ValueError("invalid consultation issues")
    return reviewed


def _validate_consultation_authorization(root: Path, feedback: str) -> dict[str, Any]:
    intent = _load(root / "delivery" / "paid-remote-intent.json")
    expected, _, _ = _consultation_attachments(root)
    requirements_sha256 = paid_remote_result.requirements_digest(root, feedback)
    desired = intent.get("desired_state") if isinstance(intent, dict) else None
    if (intent.get("version") != 2
            or intent.get("mode") not in {"answer", "consultation_answer"}
            or intent.get("buyer_feedback_sha256") != feedback
            or intent.get("requirements_sha256") != requirements_sha256 or not isinstance(desired, dict)):
        raise ValueError("invalid consultation authorization")
    message = _text(desired.get("customer_message"))
    if paid_remote_result.SECRET.search(message):
        raise ValueError("unsafe consultation answer")
    reviewed = _validate_consultation_result({"status": "ok", "customer_message": message,
        "reviewed_attachments": desired.get("reviewed_attachments"), "issues": []}, expected)
    digest = hashlib.sha256(json.dumps(desired, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    message_sha256 = hashlib.sha256(message.encode()).hexdigest()
    if (intent.get("desired_state_sha256") != digest or intent.get("message_sha256") != message_sha256
            or intent.get("target") != f"https://coconala.com/talkrooms/{_text(intent.get('talkroom_id'))}"):
        raise ValueError("invalid consultation authorization")
    owner_dir = root / "evidence" / "agent-PAID_ANSWER_OWNER"
    owner = _consultation_runner_result(owner_dir, task_label="paid-answer-owner",
                                        task_class="escalation-agent", model=PAID_DECISION_MODEL,
                                        started_ns=0)
    owner_result_path = _consultation_result_path(owner_dir)
    owner_summary = _runner_summary(owner_dir)
    if (hashlib.sha256(owner_result_path.read_bytes()).hexdigest()
            != intent.get("owner_result_sha256")
            or hashlib.sha256((owner_dir / "summary.json").read_bytes()).hexdigest()
            != intent.get("owner_summary_sha256")
            or intent.get("owner_model") != owner_summary.get("selected_model")):
        raise ValueError("consultation review proof changed")
    _validate_consultation_result(owner, expected, message=message)
    if owner.get("status") != "ok" or owner.get("issues"):
        raise ValueError("consultation review blocked")
    answer = _load(root / "delivery" / "paid-answer.json")
    if (answer.get("status") != "answer" or answer.get("message") != message
            or answer.get("requirements_sha256") != requirements_sha256
            or answer.get("message_sha256") != message_sha256):
        raise ValueError("consultation answer mismatch")
    return {**intent, "reviewed_attachments": reviewed}


def _run_consultation_review(args, item_path: Path, root: Path, feedback: str, base: Path) -> Path:
    context = root / "context" / "current.json"
    context.parent.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, str(args.context_compiler), "--project-root", str(root),
          "--queue-item", str(item_path), "--output", str(context)], "context_compile")
    requirements_sha256 = paid_remote_result.requirements_digest(root, feedback)
    requirements_snapshot = _requirements_snapshot(root)
    expected, images, documents = _consultation_attachments(root)
    schema = HERE.parent / "schemas" / "paid_consultation_review.schema.json"
    owner_evidence = root / "evidence" / "agent-PAID_ANSWER_OWNER"
    verifier_evidence = root / "evidence" / "agent-PAID_ANSWER_VERIFY"
    # A brand-new project has no owner directory yet. Create it before taking the project
    # identity snapshot, otherwise agent-runner's mkdir changes the parent evidence directory
    # mtime and is misclassified as a customer-context TOCTOU mutation.
    owner_evidence.mkdir(parents=True, exist_ok=True)
    review_state_path = root / "context" / "paid-review-state.json"
    review_state = _load(review_state_path) if _regular_file(review_state_path) else {}
    issues = ([_text(issue) for issue in review_state.get("findings", []) if _text(issue)]
              if isinstance(review_state, dict)
              and review_state.get("state") == "REPAIR_PENDING"
              and review_state.get("mode") == "answer"
              and review_state.get("buyer_feedback_sha256") == feedback
              and review_state.get("requirements_sha256") == requirements_sha256 else [])
    for review_round in range(1, 4):
        owner_prompt = base / "answer-review" / "owner.prompt.txt"
        owner_prompt.parent.mkdir(parents=True, exist_ok=True)
        if expected:
            attachment_instruction = (
                "Inspect every buyer attachment listed here, preserve its exact order, filename, and SHA256 in "
                f"reviewed_attachments, and record a concrete observation: {json.dumps(expected, ensure_ascii=False)}. "
                f"Visually inspect image inputs. Read non-image documents with local read-only tools from these paths: "
                f"{json.dumps([str(path) for path in documents], ensure_ascii=False)}."
            )
        else:
            attachment_instruction = "No buyer attachments are present; reviewed_attachments must be an empty array."
        owner_prompt.write_text(
            "You are the paid answer owner. Never submit or send anything. You may run local read commands and "
            "research the public web with installed CLI tools when the buyer asks for current or externally verifiable "
            "facts. Before repeating any external fact not proved by the compiled project sources, fetch its official "
            "page with the installed crwl CLI so the command output is preserved in your runner stdout evidence; include "
            "the exact official URL in the answer. Attempt each official fact source once; if retrieval fails, label that "
            "fact unverified or omit it and return to the buyer answer immediately. Never debug, patch, or repeatedly retry "
            "a research tool inside a customer-response task. "
            f"Read {context}, {root / 'requirements/live-buyer-reply.json'}, and the current semantic decision at "
            f"{root / 'context/paid-work-decision.json'}. The answer must satisfy that decision's required_output and "
            f"required_effect without contradicting primary evidence. {attachment_instruction} "
            "Write a concrete, safe Japanese answer to the exact current buyer request. Distinguish proved facts from "
            "uncertainty and invent nothing. Compress the buyer-facing response to the smallest useful answer: lead "
            "with the conclusion, keep only the decisive reasons and the next action, and remove repeated explanation, "
            "internal research narration, exhaustive alternatives, and facts the buyer already knows. Use short natural "
            "paragraphs and normally stay within 600 Japanese characters; exceed that target only when the buyer "
            "explicitly requested a detailed written report and the extra detail directly answers that request. "
            "Never ask for a fact available anywhere in current.json or any "
            "read_these_first source. Ask at most one question only when the absent fact is indispensable to truthful "
            "work; otherwise answer or produce the non-blocked work now. Resolve every previous "
            f"fresh-review issue: {json.dumps(issues, ensure_ascii=False)}. Return blocked only when no safe answer can be sent.",
            encoding="utf-8",
        )
        command = [sys.executable, str(args.agent_runner), "--task-class", "escalation-agent",
                   "--prompt-file", str(owner_prompt), "--schema", str(schema),
                   "--evidence-dir", str(owner_evidence), "--task-label", "paid-answer-owner",
                   "--escalation-reason", "Paid owner composes the exact paid buyer answer",
                   "--loop", _runner_loop_id(), "--workdir", str(root), "--timeout-seconds", "1800"]
        for image in images:
            command += ["--image", str(image)]
        command = _private_model_runner(root, command, "paid-answer-owner")
        project_snapshot = _project_identity_snapshot(root, owner_evidence)
        owner_started_ns = time.time_ns()
        _run(command, "remote_builder")
        if _project_identity_snapshot(root, owner_evidence) != project_snapshot:
            raise Failure("remote_builder")
        owner = _consultation_runner_result(
            owner_evidence, task_label="paid-answer-owner", task_class="escalation-agent",
            model=PAID_DECISION_MODEL, started_ns=owner_started_ns,
        )
        try:
            reviewed = _validate_consultation_result(owner, expected)
        except (AttributeError, ValueError, TypeError) as error:
            raise Failure("answer_validation") from error
        if owner.get("status") != "ok":
            raise Failure("remote_builder")
        break

    if _requirements_snapshot(root) != requirements_snapshot or _consultation_attachments(root)[0] != expected:
        raise Failure("requirements_toctou")
    target = _text(_load(item_path).get("marketplace_url"))
    if target != f"https://coconala.com/talkrooms/{_text(_load(item_path).get('talkroom_id'))}":
        raise Failure("remote_builder")
    message = _text(owner["customer_message"])
    desired = {"customer_message": message, "reviewed_attachments": reviewed}
    digest = hashlib.sha256(json.dumps(desired, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    message_sha256 = hashlib.sha256(message.encode()).hexdigest()
    delivery = root / "delivery"; delivery.mkdir(parents=True, exist_ok=True)
    _write(delivery / "paid-remote-intent.json", {"version": 2, "mode": "answer",
           "buyer_feedback_sha256": feedback, "talkroom_id": _text(_load(item_path).get("talkroom_id")),
           "target": target, "desired_state": desired, "desired_state_sha256": digest,
           "requirements_sha256": requirements_sha256, "message_sha256": message_sha256,
           "owner_result_sha256": hashlib.sha256(_consultation_result_path(owner_evidence).read_bytes()).hexdigest(),
           "owner_summary_sha256": hashlib.sha256((owner_evidence / "summary.json").read_bytes()).hexdigest(),
           "owner_model": _runner_summary(owner_evidence)["selected_model"]})
    _write(delivery / "paid-answer.json", {"version": 1, "status": "answer", "message": message,
           "requirements_sha256": requirements_sha256, "message_sha256": message_sha256})
    _validate_consultation_authorization(root, feedback)
    _write(root / "context" / "paid-review-state.json", {
        "version": 1, "state": "APPROVED", "mode": "answer",
        "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
        "round": review_round, "message_sha256": message_sha256,
    })
    return _consultation_result_path(owner_evidence)


def _remote_owner_checkpoint(status: str, root: Path, feedback: str, digest: str,
                             pass_start: float) -> str:
    if status == "ok":
        return "ok"
    if status == "blocked":
        paid_remote_result.validate_wait(root, feedback, digest, pass_start)
        return "pending"
    raise Failure("remote_builder")


def _remote_wait_is_fresh(root: Path, feedback: str, digest: str,
                          now: float | None = None) -> bool:
    paid_remote_result.validate_wait(root, feedback, digest, pass_start=0)
    observed_at = (root / "delivery" / "paid-remote-result.json").stat().st_mtime
    release_manifest = REPO_ROOT / "RELEASE.json"
    if _regular_file(release_manifest) and release_manifest.stat().st_mtime > observed_at:
        return False
    operator_policy = root / "context" / PAID_FILE_OPERATOR_POLICY
    if _regular_file(operator_policy) and operator_policy.stat().st_mtime > observed_at:
        return False
    age = (time.time() if now is None else now) - observed_at
    return 0 <= age < PAID_REMOTE_WAIT_RECHECK_SECONDS


def _remote_wait_before_decision(root: Path, item: dict[str, Any],
                                 now: float | None = None) -> bool:
    feedback = _text(item.get("buyer_feedback_sha256"))
    intent = _load(root / "delivery" / "paid-remote-intent.json")
    result_path = root / "delivery" / "paid-remote-result.json"
    result = _load(result_path)
    _, semantic_sha256 = _semantic_effect_contract(root)
    if _require_semantic_effect_binding(root, intent, result) != semantic_sha256:
        return False
    if _operator_policy_newer_than(root, item, result_path):
        return False
    return _remote_wait_is_fresh(
        root, feedback, _text(intent.get("desired_state_sha256")), now=now,
    )


def _operator_policy_newer_than(root: Path, item: dict[str, Any], checkpoint: Path) -> bool:
    feedback = _text(item.get("buyer_feedback_sha256"))
    requirements_sha256 = paid_remote_result.requirements_digest(root, feedback)
    policy_path, _policy, _policy_sha256 = _file_operator_policy(
        root, feedback, requirements_sha256,
    )
    return (policy_path is not None
            and policy_path.stat().st_mtime_ns > checkpoint.stat().st_mtime_ns)


def _run_remote_repair(args, item_path: Path, root: Path, feedback: str, base: Path) -> Path:
    context = root / "context" / "current.json"
    context.parent.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, str(args.context_compiler), "--project-root", str(root), "--queue-item", str(item_path), "--output", str(context)], "context_compile")
    try: requirements_sha256 = paid_remote_result.requirements_digest(root, feedback)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error: raise Failure("context_compile") from error
    requirements_snapshot = _requirements_snapshot(root)
    progress = root / "delivery" / "paid-remote-progress.jsonl"
    try: _, semantic_contract_sha256 = _semantic_effect_contract(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error: raise Failure("context_compile") from error
    repair = base / "remote-repair"
    repair.mkdir(parents=True, exist_ok=True)
    pass_start = time.time()
    verifier_evidence = root / "evidence" / "agent-PAID_REMOTE_VERIFY"
    review_delta: list[dict[str, str]] | None = None
    builder_required, validation_start = True, pass_start
    try:
        intent = _load(root / "delivery" / "paid-remote-intent.json")
        delivery_result = _load(root / "delivery" / "paid-remote-result.json")
        _require_semantic_effect_binding(root, intent, delivery_result)
        digest = _text(intent.get("desired_state_sha256"))
        if _remote_wait_is_fresh(root, feedback, digest):
            raise Failure("remote_progress")
        paid_remote_result.validate_builder(root, feedback, digest, resume=True)
        builder_required, validation_start = False, 0
        try:
            previous = _review_failure(
                verifier_evidence / "remote-verifier-result.json", root, intent, feedback, digest,
            )
            if previous["classification"] == "quality_mismatch":
                review_delta, builder_required = previous["delta"], True
        except Failure:
            pass
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    verifier_path = None
    for review_round in range(1, 4):
        if builder_required:
            prompt = repair / "owner.prompt.txt"
            prompt.write_text(
                _repair_prompt(root, item_path, feedback, requirements_sha256, False,
                               args.cdp_helper, review_delta),
                encoding="utf-8",
            )
            with prompt.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\nDurable effect progress is {progress}. Read it before acting and never repeat an effect_key. "
                    f"If it is absent or incomplete, inspect prior owner evidence under {root / 'evidence/agent-PAID_REMOTE_OWNER'}, "
                    "verify each claimed effect again on the official target, and checkpoint only the exact readbacks before new mutations. "
                    "Immediately after every official exact readback, write one project-owned effect JSON and run "
                    f"python3 {HERE / 'effect_checkpoint.py'} --project-root {root} --effect-json <path>. "
                    "Checkpoint before searching for the next target or composing the final result. quality_status is "
                    "For long X recon, use the production x_collect.py --output path so every completed query is "
                    "atomically durable; do not redirect its final stdout to a zero-byte file that loses the whole pass on timeout. "
                    "qualified, qualification, or invalid; qualification_sources must contain the official URLs or "
                    "hash-bound project sources supporting the outbound claims. Reduce the ledger by effect_key with the last row effective; "
                    "a classification_revision is an audit correction, never another external effect. If the exact outbound payload asks the "
                    "recipient to confirm any required qualification, classify it as qualification with counts_toward_50=false until an "
                    "affirmative official response supplies that missing fact. A classification revision preserves the prior effect's semantic_contract_sha256 "
                    "as part of immutable effect identity; only the current-cycle intent and result use the current semantic digest. "
                    "If an earlier row violates this rule, preserve its effect identity "
                    "and receipt, write a corrected effect JSON with classification_revision=true and a nonempty revision_reason, and checkpoint "
                    "that same effect_key before calculating totals.\n"
                )
            owner_evidence = root / "evidence" / "agent-PAID_REMOTE_OWNER"
            owner_started_ns = time.time_ns()
            owner_command = [sys.executable, str(args.agent_runner), "--task-class", "escalation-agent",
                  "--prompt-file", str(prompt), "--schema", str(args.runner_schema),
                  "--evidence-dir", str(owner_evidence), "--task-label", "paid-remote-owner",
                  "--escalation-reason", "Paid owner mutates the authenticated paid target",
                  "--loop", _runner_loop_id(), "--workdir", str(root), "--timeout-seconds", "1800"]
            progress_size = progress.stat().st_size if _regular_file(progress) else 0
            try:
                _run_private_model_serialized(
                    root, owner_command, "paid-remote-owner", "remote_builder", effect_owner=True,
                )
            except Failure:
                if _regular_file(progress) and progress.stat().st_size > progress_size:
                    raise Failure("remote_progress")
                raise
            _consultation_runner_result(
                owner_evidence, task_label="paid-remote-owner", task_class="escalation-agent",
                model=PAID_DECISION_MODEL, started_ns=owner_started_ns,
            )
            if _requirements_snapshot(root) != requirements_snapshot:
                raise Failure("remote_builder")
            if not all((root / "delivery" / name).is_file()
                       for name in ("paid-remote-intent.json", "paid-remote-result.json")):
                raise Failure("remote_builder")
            try:
                intent = _load(root / "delivery" / "paid-remote-intent.json")
                delivery_result = _load(root / "delivery" / "paid-remote-result.json")
                if _require_semantic_effect_binding(root, intent, delivery_result) != semantic_contract_sha256:
                    raise ValueError("semantic effect contract changed")
                _normalize_builder_result(root)
                intent = _load(root / "delivery" / "paid-remote-intent.json")
                digest = _text(intent.get("desired_state_sha256"))
                checkpoint = _remote_owner_checkpoint(
                    step_result_status.status_from_evidence(owner_evidence),
                    root, feedback, digest, pass_start,
                )
                if checkpoint == "pending":
                    raise Failure("remote_progress")
                paid_remote_result.validate_builder(root, feedback, digest, pass_start)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise Failure("remote_builder") from error
            builder_required, validation_start = False, pass_start

        if verifier_evidence.is_symlink():
            raise Failure("remote_verifier")
        verifier_evidence.mkdir(parents=True, exist_ok=True)
        for stale in verifier_evidence.rglob("*.json"):
            if stale.name in {"summary.json", "runner-result.json"} or re.fullmatch(r"attempt-[0-9]+\.result\.json", stale.name):
                continue
            if stale.is_file():
                stale.unlink()
        verifier_path = verifier_evidence / "remote-verifier-result.json"
        verifier_path.unlink(missing_ok=True)
        repair.mkdir(parents=True, exist_ok=True)
        prompt = repair / "verifier.prompt.txt"
        prompt.write_text(
            _repair_prompt(root, item_path, feedback, requirements_sha256, True, args.cdp_helper),
            encoding="utf-8",
        )
        delivery_snapshot, verifier_started_ns = _delivery_snapshot(root), time.time_ns()
        project_snapshot = _project_identity_snapshot(root, verifier_evidence)
        # Not --read-only. Its contract is to open the live target and write
        # remote-verifier-result.json, and a read-only sandbox denies it both the socket and the
        # file - which is why this gate had never once passed. Tampering stays fenced by the
        # snapshots either side of the run.
        verifier_command = [sys.executable, str(args.agent_runner), "--task-class", "escalation-agent",
              "--prompt-file", str(prompt), "--schema", str(args.runner_schema),
              "--evidence-dir", str(verifier_evidence), "--task-label", "paid-remote-verifier",
              "--escalation-reason", "Fresh model independently verifies the paid live target",
              "--loop", _runner_loop_id(), "--workdir", str(root), "--timeout-seconds", "1800"]
        _run_private_model_serialized(root, verifier_command, "paid-remote-verifier", "remote_verifier")
        if (_requirements_snapshot(root) != requirements_snapshot
                or _delivery_snapshot(root) != delivery_snapshot
                or _project_identity_snapshot(root, verifier_evidence) != project_snapshot):
            raise Failure("remote_verifier")
        semantic_status = step_result_status.status_from_evidence(verifier_evidence)
        if semantic_status == "ok":
            verifier_path = _validate_managed_verifier(
                verifier_path, root, intent, feedback, digest, verifier_started_ns,
            )
            break
        if semantic_status != "blocked":
            raise Failure("remote_verifier")
        rejected = _review_failure(
            verifier_path, root, intent, feedback, digest, verifier_started_ns,
        )
        if review_round == 3:
            _write(root / "context" / "paid-review-state.json", {
                "version": 1, "state": "REPAIR_PENDING", "mode": "remote",
                "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
                "round": review_round, "findings": rejected.get("delta") or [],
            })
            raise Failure("remote_verifier")
        if rejected["classification"] == "quality_mismatch":
            review_delta, builder_required = rejected["delta"], True
        else:
            review_delta, builder_required = None, False

    if verifier_path is None:
        raise Failure("remote_verifier")
    try:
        intent = _load(root / "delivery" / "paid-remote-intent.json")
        digest = _text(intent.get("desired_state_sha256"))
        verifier_path = resolve_managed_verifier(root, feedback, digest)
        paid_remote_result.write_answer(root, feedback, digest, validation_start, verifier_path)
        paid_remote_result.record(root, feedback, digest, validation_start, verifier_path)
        paid_remote_result.resume(root, feedback, digest, verifier_path)
        _write(root / "context" / "paid-review-state.json", {
            "version": 1, "state": "APPROVED", "mode": "remote",
            "buyer_feedback_sha256": feedback, "requirements_sha256": requirements_sha256,
            "round": review_round, "desired_state_sha256": digest,
        })
        return verifier_path
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure) as error:
        raise Failure("remote_resume") from error

def _is_coconala_cancellation_block(semantic: dict[str, Any]) -> bool:
    unresolved = semantic.get("unresolved")
    effect = _text(semantic.get("required_effect"))
    return (
        semantic.get("decision") == "blocked"
        and isinstance(unresolved, list)
        and "coconala" in effect.casefold()
        and (
            "cancellation" in effect.casefold()
            or re.search(r"\bcancel\b", effect, re.IGNORECASE) is not None
            or "キャンセル" in effect
        )
        and any("adapter" in _text(item).casefold() for item in unresolved)
    )


def _durable_coconala_cancellation_intent(intent: dict[str, Any], item: dict[str, Any]) -> bool:
    room, feedback = _text(item.get("talkroom_id")), _text(item.get("buyer_feedback_sha256"))
    return (
        bool(room and feedback)
        and isinstance(intent, dict)
        and intent.get("action") == "cancellation_request"
        and intent.get("target") == f"https://coconala.com/talkrooms/{room}"
        and intent.get("feedback_sha256") == feedback
        and intent.get("effect_key") == hashlib.sha256(
            f"coconala:cancel:{room}:{feedback}".encode()
        ).hexdigest()
        and intent.get("formal_delivery_checkbox") is False
        and intent.get("phase") in {"prepared", "click_started", "effect_started", "verified"}
    )


def _run_coconala_cancellation(args, item_path: Path, root: Path,
                               feedback: str, evidence_dir: Path) -> dict[str, Any]:
    payload = _json_line(_run([
        sys.executable, str(args.cancel_browser),
        "--queue-item", str(item_path), "--project-root", str(root),
        "--evidence-dir", str(evidence_dir),
        "--default-tab-helper", str(args.cdp_helper),
    ], "cancellation_browser"), "cancellation_browser")
    evidence = payload.get("evidence")
    if (
        payload.get("ok") is not True
        or not isinstance(evidence, dict)
        or evidence.get("action") != "cancellation_request"
        or evidence.get("feedback_sha256") != feedback
        or evidence.get("formal_delivery_checkbox") is not False
        or evidence.get("readback") != 1
        or not (evidence.get("send_performed") is True or evidence.get("deduplicated") is True)
    ):
        raise Failure("cancellation_browser")
    return evidence


def _prepare_one(args, item_path: Path, output: Path) -> int:
    room = ""; root = None; diagnostic_stage = "load_item"
    try:
        item = _load(item_path); room, feedback = _text(item.get("talkroom_id")), _text(item.get("buyer_feedback_sha256"))
        diagnostic_stage = "resolve_project"
        root = _paid_project_root(args, item)
        if _account_owner_observe_only(args, item) is not None:
            _write(output, {"status": "reserved_for_owner", "talkroom_id": room,
                            "effect": 0, "readback": 1, "failed": 0})
            return 0
        base = args.evidence_dir / "paid-direct" / room
        preflight = base / "preflight" / "selected-talkroom-snapshot.json"
        diagnostic_stage = "reuse_parent_readback"
        parent_snapshot = (
            args.evidence_dir / "paid-direct" / "targeted" / room / "snapshot.json"
        )
        _write(preflight, _load(parent_snapshot))
        diagnostic_stage = "preflight_validate"
        preflight_row = _row(_load(preflight), room)
        if _text(preflight_row.get("buyer_feedback_sha256")) != feedback: raise Failure("remote_resume")
        diagnostic_stage = "dm_context"
        _collect_dm_context(args, {**item, **preflight_row}, root, base)
        diagnostic_stage = "external_wait_resume"
        try:
            if _remote_wait_before_decision(root, item):
                progress = root / "delivery" / "paid-remote-progress.jsonl"
                _write(output, {"status": "pending", "talkroom_id": room, "failed": 0,
                                "failed_step": None, "effect": 0, "readback": 1,
                                "progress_ledger": str(progress),
                                "_paid_prepare_status": "pending"})
                return 0
        except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
            pass
        diagnostic_stage = "semantic_decision"
        try:
            cancellation_intent = _load(root / "delivery" / "cancellation-intent.json")
        except (AttributeError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
            cancellation_intent = None
        if _durable_coconala_cancellation_intent(cancellation_intent, item):
            semantic = {
                "decision": "blocked",
                "required_effect": "Coconala キャンセルリクエスト: cancel the transaction.",
                "unresolved": [
                    "No code-owned Coconala cancellation/transaction-control adapter is present.",
                ],
            }
        else:
            try:
                semantic = _current_paid_decision(root, item)
            except (AttributeError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
                semantic = _paid_decision(args, item_path, root, base)
        if semantic.get("decision") in {"satisfied_noop", "await_buyer"}:
            status = "satisfied_noop" if semantic.get("decision") == "satisfied_noop" else "awaiting_buyer"
            _write(output, {
                "status": status,
                "talkroom_id": room,
                "effect": 0,
                "readback": 1,
                "failed": 0,
                "semantic_decision": semantic.get("decision"),
                "_paid_prepare_status": "no_effect",
            })
            return 0
        if _is_coconala_cancellation_block(semantic):
            _write(output, {
                **item,
                "project_root": str(root),
                "semantic_decision": "cancellation",
                "formal_delivery_checkbox": False,
                "_paid_mode": "cancellation",
                "_paid_prepare_status": "prepared",
            })
            return 0
        if semantic.get("decision") == "blocked":
            _write(output, {
                "status": "pending",
                "talkroom_id": room,
                "effect": 0,
                "readback": 1,
                "failed": 0,
                "failed_step": None,
                "semantic_decision": "blocked",
                "unresolved": semantic.get("unresolved") or [],
                "_paid_prepare_status": "pending",
            })
            return 0
        file_mode = semantic.get("decision") == "actionable" and semantic.get("mode") == "file"
        if file_mode:
            diagnostic_stage = "file_prepare"
            try:
                prepared = _prepare_file(args, item_path, root, item, base, feedback)
            except ValueError as error:
                raise Failure("file_validation") from error
            _write(output, {**prepared, "project_root": str(root), "_paid_prepare_status": "prepared"})
            return 0
        delivery = root / "delivery"; intent_path = delivery / "paid-remote-intent.json"
        consultation_answer = _answer_ready(root, item)
        verifier = None; repaired = False
        if consultation_answer:
            try:
                _validate_consultation_authorization(root, feedback)
                verifier = _consultation_result_path(root / "evidence" / "agent-PAID_ANSWER_VERIFY")
            except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
                verifier = _run_consultation_review(args, item_path, root, feedback, base)
                repaired = True
        else:
            try:
                intent = _load(intent_path); digest = _text(intent.get("desired_state_sha256"))
                _normalize_builder_result(root)
                verifier = resolve_managed_verifier(root, feedback, digest)
                try:
                    paid_remote_result.resume(root, feedback, digest, verifier)
                except ValueError:
                    paid_remote_result.write_answer(root, feedback, digest, 0, verifier)
                    paid_remote_result.record(root, feedback, digest, 0, verifier)
                    paid_remote_result.resume(root, feedback, digest, verifier)
            except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, Failure):
                if not _remote_mode_required(root, item, feedback):
                    raise Failure("remote_resume")
                verifier = _run_remote_repair(args, item_path, root, feedback, base)
                repaired = True
        intent = _load(intent_path); digest = _text(intent.get("desired_state_sha256"))
        prepared = {**item, "project_root": str(root), "remote_repaired": repaired,
                    "requirements_sha256": paid_remote_result.requirements_digest(root, feedback)}
        _write(output, {**prepared, "_paid_prepare_status": "prepared"})
        return 0
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if (isinstance(error, Failure) and error.step == "file_builder"
                and isinstance(root, Path)
                and step_result_status.status_from_evidence(
                    root / "evidence" / "agent-PAID_FILE_OWNER",
                ) == "blocked"):
            _write(output, {"status": "pending", "talkroom_id": room, "failed": 0,
                            "failed_step": None, "effect": 0, "readback": 1,
                            "_paid_prepare_status": "pending"})
            return 0
        if isinstance(error, Failure) and error.step == "file_verifier" and isinstance(root, Path):
            try:
                review_state = _load(root / "context" / "paid-review-state.json")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                review_state = {}
            if review_state.get("state") == "REPAIR_PENDING":
                _write(output, {"status": "pending", "talkroom_id": room, "failed": 0,
                                "failed_step": None, "effect": 0, "readback": 0,
                                "_paid_prepare_status": "pending"})
                return 0
        cause = error.__cause__
        progress = root / "delivery" / "paid-remote-progress.jsonl" if isinstance(root, Path) else None
        if isinstance(error, Failure) and error.step == "remote_progress" and progress is not None:
            _write(output, {"status": "pending", "talkroom_id": room, "failed": 0,
                            "failed_step": None, "effect": 0, "readback": 1,
                            "progress_ledger": str(progress), "_paid_prepare_status": "pending"})
            return 0
        failure = {
            "status": "failed", "talkroom_id": room, "failed": 1,
            "failed_step": error.step if isinstance(error, Failure) else "remote_resume",
            "diagnostic_stage": diagnostic_stage,
            "error_type": type(error).__name__,
            "error_detail": (error.detail if isinstance(error, Failure) else str(error))[:500],
            "cause_type": type(cause).__name__ if cause is not None else None,
            "cause_detail": str(cause)[:500] if cause is not None else None,
            "effect": 0, "readback": 0,
        }
        contract_diff = root / "context" / "paid-asset-contract-diff.json" if isinstance(root, Path) else None
        if (isinstance(error, Failure) and error.step == "file_contract_review"
                and contract_diff is not None and _regular_file(contract_diff)):
            failure["diagnostic_evidence"] = str(contract_diff)
        owner_feedback = root / "context" / "paid-owner-feedback.json" if isinstance(root, Path) else None
        if owner_feedback is not None and _regular_file(owner_feedback):
            failure["owner_feedback"] = str(owner_feedback)
        _write(output, failure); return 1


def _write_file_effect(args, item_path: Path, output: Path, prepared: dict[str, Any]) -> int:
    room = _text(prepared.get("talkroom_id")); sent_effect = 0
    try:
        feedback = _text(prepared.get("buyer_feedback_sha256"))
        root = _paid_project_root(args, prepared)
        requirements_sha256 = _text(prepared.get("requirements_sha256"))
        if (not _file_mode(root, prepared)
                or paid_remote_result.requirements_digest(root, feedback) != requirements_sha256):
            raise Failure("requirements_toctou")
        stable = delivery_queue.evidence_path(args.delivery_evidence_dir, prepared)
        manifest = _validate_file_authorization(root, stable, feedback, requirements_sha256)
        base = args.evidence_dir / "paid-direct" / room
        presend = base / "presend" / "selected-talkroom-snapshot.json"
        _reclaim_browser_owner(args, f"paid-direct-{room}")
        environment = _fresh_child_env(args, owner=f"paid-direct-{room}")
        _run(
            _collector(args, "selected-talkroom-only", presend, presend.parent, item_path, prepared),
            "presend_readback", env=environment,
        )
        row = _row(_load(presend), room)
        if (_text(row.get("buyer_feedback_sha256")) != feedback
                or paid_remote_result.requirements_digest(root, feedback) != requirements_sha256):
            raise Failure("presend_readback")
        evidence, blockers = delivery_queue.delivery_gate(row, args.delivery_evidence_dir, args.projects_root)
        cadence = {**prepared, **row, **{key: evidence[key] for key in (
            "project_root", "requirements_path", "artifact_path", "artifact_version",
            "acceptance_evidence_path", "acceptance_status", "package_sha256",
            "acceptance_delta", "recipient_access_required",
        ) if key in evidence}, "delivery_evidence": evidence, "blockers": blockers}
        cadence["customer_message"] = _text(manifest.get("customer_message"))
        # A targeted DOM readback is intentionally sparse.  It may discover a new
        # reason to downgrade/cancel a send, but it must not erase a buyer hold
        # already derived from the complete project context.
        if prepared.get("buyer_formal_delivery_hold") is True:
            cadence["buyer_formal_delivery_hold"] = True
            cadence["buyer_formal_delivery_hold_reason"] = prepared.get(
                "buyer_formal_delivery_hold_reason"
            )
        decision = delivery_queue.delivery_decision(cadence)
        action = _text(decision.get("mode"))
        if action not in {"formal", "progress"}:
            raise Failure("presend_readback")
        prepared_action = _text(prepared.get("delivery_action"))
        if action == "formal" and prepared_action != "formal":
            raise Failure("presend_action_escalation")
        cadence.update(
            delivery_action=action,
            formal_delivery_checkbox=decision.get("formal_delivery_checkbox") is True,
            progress_payload=_file_progress_payload(cadence) if action == "progress" else None,
        )
        browser_item = base / "file" / f"queue-{os.getpid()}-{time.time_ns()}.json"
        _write(browser_item, cadence)
        browser_evidence = base / "file-browser" / f"{manifest['artifact_version']}-{manifest['package_sha256'][:12]}"
        browser_started = time.time()
        revision_after_formal = (
            action == "progress" and row.get("formal_delivery_observed") is True
            and _text(row.get("talkroom_state", row.get("transaction_state"))) == "納品確認待ち"
        )
        if revision_after_formal:
            cadence["revision_after_formal"] = True
            _write(browser_item, cadence)
        if action == "formal":
            command = [sys.executable, str(args.formal_browser), "--queue-item", str(browser_item),
                       "--manifest", str(root / "delivery" / "paid-work-result.json"),
                       "--project-root", str(root), "--evidence-dir", str(browser_evidence),
                       "--ledger", str(root / "events.jsonl"), "--default-tab-helper", str(args.cdp_helper)]
        else:
            command = [sys.executable, str(args.answer_browser), "--queue-item", str(browser_item),
                       "--manifest", str(root / "delivery" / "paid-work-result.json"),
                       "--evidence-dir", str(browser_evidence), "--default-tab-helper", str(args.cdp_helper)]
            if revision_after_formal:
                command.append("--revision-after-formal")
        disk_reason = _effect_gate_reason(args)
        if disk_reason is not None:
            return _write_disk_pending(output, room, disk_reason, "before_file_browser_effect")
        browser_process = _run_bounded(command)
        if browser_process.returncode:
            browser_error = {
                "version": 1,
                "returncode": browser_process.returncode,
                "stdout": redact_prompt_text(browser_process.stdout[-4000:]),
                "stderr": redact_prompt_text(browser_process.stderr[-4000:]),
            }
            _write(browser_evidence / "browser-error.json", browser_error)
            owner_browser_result = root / "context" / "paid-browser-result.json"
            _write(owner_browser_result, browser_error)
            _owner_feedback(root, "paid.file_browser", [browser_error], [owner_browser_result])
            contract_finding = _browser_contract_finding(browser_process)
            if contract_finding:
                authorization = _load(root / "context" / "paid-file-authorization.json")
                _write(root / "context" / "paid-review-state.json", {
                    "version": 1, "state": "REPAIR_PENDING", "mode": "file",
                    "review_policy_version": PAID_FILE_POLICY_VERSION,
                    "operator_policy_sha256": _text(authorization.get("operator_policy_sha256")),
                    "buyer_feedback_sha256": feedback,
                    "requirements_sha256": requirements_sha256,
                    "artifact_sha256": manifest["package_sha256"],
                    "round": 0, "verdict": "needs_revision", "finding": contract_finding,
                })
            if "artifact_is_about_the_deal_not_the_deliverable:" in browser_process.stderr:
                authorization = _load(root / "context" / "paid-file-authorization.json")
                finding = browser_process.stderr.rsplit(
                    "artifact_is_about_the_deal_not_the_deliverable:", 1,
                )[-1].strip().splitlines()[0]
                _write(root / "context" / "paid-review-state.json", {
                    "version": 1, "state": "REPAIR_PENDING", "mode": "file",
                    "review_policy_version": PAID_FILE_POLICY_VERSION,
                    "operator_policy_sha256": _text(authorization.get("operator_policy_sha256")),
                    "buyer_feedback_sha256": feedback,
                    "requirements_sha256": requirements_sha256,
                    "artifact_sha256": manifest["package_sha256"],
                    "round": int(authorization.get("review_round") or 0),
                    "finding": finding,
                })
            raise Failure("file_browser")
        browser = _json_line(browser_process.stdout, "file_browser")
        if browser.get("ok") is not True or not isinstance(browser.get("evidence"), dict):
            raise Failure("file_browser")
        sent_effect = int(browser["evidence"].get("send_performed") is True)
        reconcile_paid_delivery.reconcile(root, browser_evidence, browser_item, min_mtime=browser_started)
        final = base / "official-readback" / "selected-talkroom-snapshot.json"
        _run(_collector(args, "selected-talkroom-only", final, final.parent, browser_item, cadence), "official_readback")
        official = _row(_load(final), room)
        if _text(official.get("buyer_feedback_sha256")) != feedback:
            raise Failure("official_readback")
        if action == "formal":
            if _reported_formal_cycle(args, official) != root:
                raise Failure("official_readback")
        else:
            payload = cadence.get("progress_payload") or {}
            binding = f"添付: {Path(_text(manifest.get('artifact_path'))).name}（{_text(manifest.get('artifact_version'))}）"
            message = _text(payload.get("message"))
            sent_message = message if binding in message else f"{message}\n\n{binding}"
            if not _seller_message_with_attachment(
                    official, sent_message, Path(_text(manifest.get("artifact_path"))).name):
                raise Failure("official_readback")
        result = {
            "talkroom_id": room, "send_performed": browser["evidence"].get("send_performed") is True,
            "deduplicated": browser["evidence"].get("deduplicated") is True,
            "formal_delivery_checkbox": action == "formal", "file_repaired": prepared.get("file_repaired") is True,
            "evidence_paths": {"manifest": str(root / "delivery" / "paid-work-result.json"),
                               "presend_readback": str(presend), "browser": str(browser_evidence),
                               "official_readback": str(final)},
        }
        _write(output, {"status": "completed", "effect": int(result["send_performed"]),
                        "readback": 1, "failed": 0, "item": result})
        return 0
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError,
            reconcile_paid_delivery.ReconcileError) as error:
        cause = error.__cause__
        _write(output, {"status": "failed", "talkroom_id": room, "failed": 1,
                        "failed_step": error.step if isinstance(error, Failure) else "file_delivery",
                        "error_type": type(error).__name__, "error_detail": str(error)[:500],
                        "cause_type": type(cause).__name__ if cause is not None else None,
                        "cause_detail": str(cause)[:500] if cause is not None else None,
                        "effect": sent_effect, "readback": 0})
        return 1

def _write_one(args, item_path: Path, output: Path) -> int:
    room = ""
    sent_effect = 0
    try:
        prepared = _load(item_path); item = prepared
        room = _text(prepared.get("talkroom_id"))
        if not room or os.environ.get("CLOAK_BROWSER_OWNER") != f"paid-direct-{room}":
            _write(output, {"status": "failed", "talkroom_id": room, "failed": 1,
                            "failed_step": "writer_owner", "effect": 0, "readback": 0})
            return 1
        if _account_owner_observe_only(args, item) is not None:
            _write(output, {"status": "reserved_for_owner",
                            "talkroom_id": _text(prepared.get("talkroom_id")),
                            "effect": 0, "readback": 1, "failed": 0})
            return 0
        disk_reason = _effect_gate_reason(args)
        if disk_reason is not None:
            return _write_disk_pending(output, _text(prepared.get("talkroom_id")), disk_reason,
                                       "before_paid_effect")
        if prepared.get("_paid_mode") == "file":
            return _write_file_effect(args, item_path, output, prepared)
        room, feedback = _text(item.get("talkroom_id")), _text(item.get("buyer_feedback_sha256"))
        root = _paid_project_root(args, item)
        base = args.evidence_dir / "paid-direct" / room
        if prepared.get("_paid_mode") == "cancellation":
            cancellation = _run_coconala_cancellation(
                args, item_path, root, feedback, base / "cancellation",
            )
            result = {
                "talkroom_id": room,
                "send_performed": cancellation.get("send_performed") is True,
                "deduplicated": cancellation.get("deduplicated") is True,
                "formal_delivery_checkbox": False,
                "effect_key": cancellation.get("effect_key"),
                "evidence_paths": {
                    "official_readback": cancellation.get("live_dom_path"),
                    "screenshot": cancellation.get("screenshot_path"),
                },
            }
            _write(output, {
                "status": "completed", "talkroom_id": room,
                "effect": int(result["send_performed"]), "readback": 1, "failed": 0,
                "semantic_decision": "cancellation", "formal_delivery_checkbox": False,
                "item": result,
            })
            return 0
        requirements_sha256 = _text(prepared.get("requirements_sha256"))
        if paid_remote_result.requirements_digest(root, feedback) != requirements_sha256:
            raise Failure("requirements_toctou")
        answer_path = root / "delivery" / "paid-answer.json"
        answer_before = answer_path.read_bytes()
        delivery = root / "delivery"; intent_path = delivery / "paid-remote-intent.json"
        intent = _load(intent_path); digest = _text(intent.get("desired_state_sha256"))
        consultation_expected = None
        if _answer_ready(root, item):
            expected, _, _ = _consultation_attachments(root)
            consultation_expected = expected
            reviewed = intent.get("desired_state", {}).get("reviewed_attachments")
            if ([{"filename": row.get("filename"), "sha256": row.get("sha256")}
                 for row in reviewed if isinstance(row, dict)] if isinstance(reviewed, list) else []) != expected:
                raise Failure("requirements_toctou")
        if consultation_expected is not None:
            _validate_consultation_authorization(root, feedback)
        else:
            verifier = resolve_managed_verifier(root, feedback, digest)
            paid_remote_result.resume(root, feedback, digest, verifier)
        if answer_path.read_bytes() != answer_before: raise Failure("answer_toctou")
        try: answer_payload = json.loads(answer_before.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error: raise Failure("answer_snapshot") from error
        if not isinstance(answer_payload, dict) or not _text(answer_payload.get("message")): raise Failure("answer_snapshot")
        customer_attachment = None
        if consultation_expected is None:
            remote_result = _load(root / "delivery" / "paid-remote-result.json")
            customer_attachment = _validated_customer_attachment(root, remote_result.get("customer_attachment"))
            if customer_attachment is not None:
                answer_payload["attachment"] = customer_attachment
        answer_dir = base / "answer"; answer_snapshot = answer_dir / f"paid-answer-{os.getpid()}-{time.time_ns()}.json"
        _write(answer_snapshot, answer_payload)
        if _load(answer_snapshot) != answer_payload: raise Failure("answer_snapshot")
        repaired = prepared.get("remote_repaired") is True if isinstance(prepared, dict) else False
        presend = base / "presend" / "selected-talkroom-snapshot.json"
        _reclaim_browser_owner(args, f"paid-direct-{room}")
        environment = _fresh_child_env(args, owner=f"paid-direct-{room}")
        _run(
            _collector(args, "selected-talkroom-only", presend, presend.parent, item_path, item),
            "presend_readback", env=environment,
        )
        presend_row = _row(_load(presend), room)
        if (_text(presend_row.get("buyer_feedback_sha256")) != feedback
                or paid_remote_result.requirements_digest(root, feedback) != requirements_sha256):
            raise Failure("presend_readback")
        if consultation_expected is not None:
            try:
                current_attachments = _consultation_attachments(root)[0]
            except Failure as error:
                raise Failure("requirements_toctou") from error
            if current_attachments != consultation_expected:
                raise Failure("requirements_toctou")
        message = _text(answer_payload.get("message"))
        seller_matches = (_seller_message_with_attachment(presend_row, message, customer_attachment["filename"])
                          if customer_attachment else
                          _seller_last_sha256(presend_row) == hashlib.sha256(_comparison_key(message).encode()).hexdigest())
        if (message and _text(presend_row.get("talkroom_state", presend_row.get("transaction_state")))
                and presend_row.get("formal_delivery_observed", presend_row.get("formal_delivery_confirmed")) is False
                and seller_matches):
            result = {"talkroom_id": room, "send_performed": False, "deduplicated": True,
                      "formal_delivery_checkbox": False, "evidence_paths": {"answer_snapshot": str(answer_snapshot), "official_readback": str(presend), "presend_readback": str(presend)}}
            _write(output, {"status": "completed", "effect": 0, "readback": 1, "failed": 0, "item": result}); return 0
        disk_reason = _effect_gate_reason(args)
        if disk_reason is not None:
            return _write_disk_pending(output, room, disk_reason, "before_answer_browser_effect")
        browser = _json_line(_run([sys.executable, str(args.answer_browser), "--queue-item", str(item_path), "--answer-file", str(answer_snapshot),
                                   "--evidence-dir", str(answer_dir), "--default-tab-helper", str(args.cdp_helper)], "answer_browser"), "answer_browser")
        if browser.get("ok") is not True or not isinstance(browser.get("evidence"), dict): raise Failure("answer_browser")
        evidence = browser["evidence"]; sent_effect = int(evidence.get("send_performed") is True)
        final = base / "official-readback" / "selected-talkroom-snapshot.json"
        _run(_collector(args, "selected-talkroom-only", final, final.parent, item_path, item), "official_readback")
        row = _row(_load(final), room)
        official_matches = (_seller_message_with_attachment(row, message, customer_attachment["filename"])
                            if customer_attachment else
                            _seller_last_sha256(row) == hashlib.sha256(_comparison_key(message).encode()).hexdigest())
        if (_text(row.get("buyer_feedback_sha256")) != feedback
                or not _text(row.get("talkroom_state", row.get("transaction_state")))
                or row.get("formal_delivery_observed", row.get("formal_delivery_confirmed")) is not False
                or not official_matches):
            raise Failure("official_readback")
        result = {"talkroom_id": room, "send_performed": evidence.get("send_performed") is True, "deduplicated": evidence.get("deduplicated") is True,
                  "formal_delivery_checkbox": evidence.get("formal_delivery_checkbox") is True, "remote_repaired": repaired,
                  "evidence_paths": {"answer_snapshot": str(answer_snapshot), "pre_readback": str(base / "preflight" / "selected-talkroom-snapshot.json"), "presend_readback": str(presend), "official_readback": str(final)}}
        _write(output, {"status": "completed", "effect": int(result["send_performed"]), "readback": 1, "failed": 0, "item": result}); return 0
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        _write(output, {"status": "failed", "talkroom_id": room, "failed": 1, "failed_step": error.step if isinstance(error, Failure) else "remote_resume", "effect": sent_effect, "readback": 0}); return 1

def _child_command(args, phase, item, output):
    return [sys.executable, str(HERE / "paid_direct.py"), phase, str(item), "--output", str(output),
            "--evidence-dir", str(args.evidence_dir), "--projects-root", str(args.projects_root), "--collector", str(args.collector),
            "--answer-browser", str(args.answer_browser), "--formal-browser", str(args.formal_browser),
            "--cancel-browser", str(args.cancel_browser),
            "--delivery-evidence-dir", str(args.delivery_evidence_dir),
            "--cdp-helper", str(args.cdp_helper), "--context-compiler", str(args.context_compiler),
            "--dm-collector", str(args.dm_collector),
            "--agent-runner", str(args.agent_runner), "--runner-schema", str(args.runner_schema),
            "--artifact-schema", str(args.artifact_schema), "--today", args.today]

def _prepare_command(args, item, output):
    return _child_command(args, "--effect-item", item, output)

def _effect_command(args, item, output):
    return _child_command(args, "--write-item", item, output)


def _effect_process_diagnostic(process: Any) -> dict[str, Any]:
    return {
        "returncode": int(process.returncode),
        "stdout_tail": _text(getattr(process, "stdout", ""))[-2000:],
        "stderr_tail": _text(getattr(process, "stderr", ""))[-2000:],
    }

def _fresh_child_env(args, owner=None):
    env = {key: value for key, value in os.environ.items() if key != "GIG_CDP_LOCK_HELD"}
    env["CDP_LOCK_DIR"] = str(args.cdp_lock_dir)
    if owner:
        env["CLOAK_BROWSER_OWNER"] = owner
    return env


def _paid_browser_owners(room: str) -> tuple[str, ...]:
    base = f"paid-direct-{room}"
    return base, f"{base}-remote-builder", f"{base}-remote-verifier"


def _reclaim_browser_owner(args, owner: str) -> None:
    try:
        subprocess.run(
            [sys.executable, str(args.cdp_helper), "close-owned", "--owner", owner],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False, env=_fresh_child_env(args, owner=owner),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _reclaim_paid_tabs(args, room: str) -> None:
    for owner in _paid_browser_owners(room):
        _reclaim_browser_owner(args, owner)


def _run_paid_item(args, room: str, item_file: Path, prepared_file: Path,
                   effect_file: Path) -> tuple[dict[str, Any], int, int, int, str]:
    browser_owner = f"paid-direct-{room}"
    prepared = {"status": "failed", "failed_step": "remote_resume"}
    for attempt in range(2):
        prepare = _run_bounded(
            _prepare_command(args, item_file, prepared_file),
            env=_fresh_child_env(args, owner=browser_owner), timeout=FILE_PREPARE_TIMEOUT_SECONDS,
        )
        try:
            prepared = _load(prepared_file)
        except (OSError, json.JSONDecodeError):
            prepared = {
                "status": "failed",
                "failed_step": "remote_resume",
                "prepare_process": _effect_process_diagnostic(prepare),
            }
            _write(prepared_file, prepared)
        if (attempt == 0 and prepare.returncode != STEP_TIMEOUT_RETURNCODE
                and prepare.returncode
                and prepared.get("failed_step") == "remote_resume"):
            continue
        break
    if prepare.returncode == 0 and prepared.get("_paid_prepare_status") == "pending":
        return {"talkroom_id": room, "status": "pending"}, 0, 0, 0, ""
    if prepare.returncode == 0 and prepared.get("_paid_prepare_status") == "no_effect":
        return {
            "talkroom_id": room,
            "status": _text(prepared.get("status")) or "satisfied_noop",
            "send_performed": False,
            "deduplicated": True,
            "formal_delivery_checkbox": False,
        }, 0, int(prepared.get("readback") == 1), 0, ""
    if prepare.returncode == 0 and prepared.get("_paid_prepare_status") == "terminal_effect":
        item = prepared.get("item")
        if not isinstance(item, dict):
            return {"talkroom_id": room, "status": "failed", "failed_step": "effect_result"}, 0, 0, 1, "effect_result"
        return {
            "talkroom_id": room,
            "status": _text(prepared.get("status")) or "completed",
            "send_performed": item.get("send_performed") is True,
            "deduplicated": item.get("deduplicated") is True,
            "formal_delivery_checkbox": False,
            "effect_key": item.get("effect_key"),
            "evidence_paths": item.get("evidence_paths") or {},
        }, int(prepared.get("effect") or 0), int(prepared.get("readback") == 1), 0, ""
    if prepare.returncode or prepared.get("_paid_prepare_status") != "prepared":
        step = _text(prepared.get("failed_step")) or "remote_resume"
        return {"talkroom_id": room, "status": "failed", "failed_step": step}, 0, 0, 1, step
    checkpoint = effect_file.with_name(effect_file.stem + "-checkpoint.json")
    try:
        _write(checkpoint, {
            "status": "pending", "talkroom_id": room, "effect": 0,
            "readback": 0, "checkpoint": "before_paid_effect",
            "prepared_file": str(prepared_file), "reason": "effect_not_started",
        })
    except OSError:
        return {"talkroom_id": room, "status": "failed", "failed_step": "disk_checkpoint"}, 0, 0, 1, "disk_checkpoint"
    gate_reason = _effect_gate_reason(args)
    if gate_reason is not None:
        if not _update_disk_checkpoint(
                checkpoint, status="pending", checkpoint="before_paid_effect", reason=gate_reason):
            return {"talkroom_id": room, "status": "failed", "failed_step": "disk_checkpoint"}, 0, 0, 1, "disk_checkpoint"
        return {"talkroom_id": room, "status": "pending", "checkpoint": "before_paid_effect",
                "reason": gate_reason}, 0, 0, 0, ""
    process = _run_bounded(
        _effect_command(args, prepared_file, effect_file),
        env=_fresh_child_env(args, owner=browser_owner),
    )
    try:
        value = _load(effect_file)
    except (OSError, json.JSONDecodeError):
        value = {"status": "failed", "failed_step": "writer_lock", "effect": 0, "readback": 0}
    if process.returncode == 0 and value.get("status") == "pending":
        if not _update_disk_checkpoint(
                checkpoint, status="pending", checkpoint=value.get("checkpoint", "before_paid_effect"),
                reason=value.get("reason", "disk_pressure"), effect=0,
                readback=int(value.get("readback") == 1)):
            return {"talkroom_id": room, "status": "failed", "failed_step": "disk_checkpoint"}, 0, 0, 1, "disk_checkpoint"
        return {"talkroom_id": room, "status": "pending",
                "checkpoint": value.get("checkpoint", "before_paid_effect"),
                "reason": value.get("reason", "disk_pressure")}, 0, 0, 0, ""
    if process.returncode or value.get("status") != "completed":
        step = _text(value.get("failed_step")) or "writer_lock"
        observed_effect = int(value.get("effect") == 1)
        checkpoint_state = "delivery_unknown" if process.returncode or observed_effect else "effect_rejected"
        if not _update_disk_checkpoint(
            checkpoint, status=checkpoint_state, checkpoint="effect_observed" if observed_effect else checkpoint_state,
            reason=step, effect=observed_effect, readback=int(value.get("readback") == 1),
            process=_effect_process_diagnostic(process)):
            step = "disk_checkpoint"
        return ({"talkroom_id": room, "status": "failed", "failed_step": step},
                observed_effect, int(value.get("readback") == 1), 1, step)
    item_result = value.get("item") or {}
    try:
        checkpoint.unlink(missing_ok=True)
    except OSError:
        pass
    row = {"talkroom_id": room, "status": "completed", **{
        key: item_result[key] for key in (
            "send_performed", "deduplicated", "formal_delivery_checkbox", "remote_repaired",
            "file_repaired", "evidence_paths",
        ) if key in item_result
    }}
    return row, int(item_result.get("send_performed") is True), int(value.get("readback") == 1), 0, ""


def _paid_report(run_id: str, result: dict[str, Any]) -> str:
    items = ", ".join(
        f"{_text(row.get('talkroom_id'))}={_text(row.get('status'))}"
        for row in result.get("items", []) if isinstance(row, dict)
    ) or "なし"
    next_action = ("案件別failureをbounded repair" if int(result.get("failed") or 0)
                   else "未完案件を次wakeでresume" if int(result.get("pending") or 0)
                   else "新しいbuyer feedbackを待つ")
    return "\n".join((
        "[ココナラ][納品] Codex:::",
        f"observed: {int(result.get('observed') or 0)}件",
        f"actionable: {int(result.get('actionable') or 0)}件",
        f"effect: {int(result.get('effect') or 0)}件",
        f"readback: {int(result.get('readback') or 0)}件",
        f"failed: {int(result.get('failed') or 0)}件",
        f"pending: {int(result.get('pending') or 0)}件",
        f"oldest: {_text(result.get('oldest')) or '不明'}",
        f"案件: {items}",
        f"次の一手: {next_action}",
        f"run_id: {run_id}",
    ))


def _report_paid_wake(args, result: dict[str, Any], run_id: str) -> dict[str, Any]:
    message = _paid_report(run_id, result)
    event_key = f"gig:telegram:paid-direct:v1:{run_id}"
    outbox = TelegramOutbox(args.telegram_database)
    row = outbox.enqueue(event_key=event_key, kind="paid-direct", message=message,
                         created_at=int(time.time()), suppress_identical_body=False)
    if row["state"] == "sent":
        return {"status": "sent", "message_id": row.get("message_id")}
    if row["state"] == "delivery_unknown":
        return {"status": "delivery_unknown", "message_id": row.get("message_id")}
    delivered = dispatch_one(
        outbox, owner=f"gig-paid-direct:{run_id}", now=lambda: int(time.time()),
        transport=OpenClawTelegramTransport(target=args.telegram_target, executable=args.openclaw,
                                            receipt_dir=args.telegram_receipt_dir),
        report_id=int(row["report_id"]),
    )
    return {"status": delivered["status"], "message_id": delivered.get("message_id")}

@contextmanager
def _lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True); handle = path.open("a+")
    try:
        try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: yield False; return
        yield True
    finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN); handle.close()


def _operator_brake_status(path: Path | None = None) -> str:
    """Use the shared expiring brake contract; an expired record is not a held brake."""
    environment = os.environ.copy()
    if path is not None:
        environment["GIG_OPERATOR_BRAKE_FILE"] = str(path)
    try:
        completed = subprocess.run(
            [str(HERE / "gig_brake.sh"), "status"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=5, check=False, env=environment,
        )
    except Exception:
        return "failed"
    return {0: "held", 1: "free"}.get(completed.returncode, "failed")

def _unique_orders(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep only the freshest observation for each structural talkroom identity."""
    by_room: dict[str, dict[str, Any]] = {}
    for item in items:
        room = _text(item.get("talkroom_id"))
        if not room:
            raise Failure("orders_parse")
        previous = by_room.get(room)
        freshness = (
            _text(item.get("snapshot_captured_at")),
            _text(item.get("talkroom_observed_at")),
            _text(item.get("buyer_feedback_sha256")),
        )
        previous_freshness = (
            _text(previous.get("snapshot_captured_at")),
            _text(previous.get("talkroom_observed_at")),
            _text(previous.get("buyer_feedback_sha256")),
        ) if previous else ("", "", "")
        if previous is None or freshness > previous_freshness:
            by_room[room] = item
    return list(by_room.values()), len(items) - len(by_room)


def _paid_queue_priority(args, item: dict[str, Any]) -> tuple[int, int, str, str]:
    """Resume rejected artifacts before starting or revisiting other paid work."""
    owner_rank = 100
    try:
        root = _paid_project_root(args, item)
        priority = _load(root / "context" / "paid-priority.json")
        value = priority.get("priority")
        if (priority.get("version") == 1 and priority.get("authorized_by") == "account_owner"
                and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100):
            owner_rank = value
    except (Failure, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        state = _load(_paid_project_root(args, item) / "context" / "paid-review-state.json")
        repair_rank = 0 if state.get("state") == "REPAIR_PENDING" else 1
    except (Failure, OSError, ValueError, TypeError, json.JSONDecodeError):
        repair_rank = 1
    return owner_rank, repair_rank, _text(item.get("delivery_date")) or "9999-12-31", _text(item.get("talkroom_id"))


def _disk_gate_reason() -> str | None:
    """Return a durable reason when pressure forbids starting another paid item."""
    try:
        return None if disk_headroom_ok() else "disk_pressure"
    except Exception as error:  # fail closed when the host policy cannot be read
        return f"disk_preflight_error:{type(error).__name__}"


def _effect_gate_reason(args) -> str | None:
    """Check host pressure and the operator brake at the irreversible-effect boundary."""
    reason = _disk_gate_reason()
    if reason is not None:
        return reason
    brake = getattr(args, "operator_brake", None)
    if brake is None:
        return None
    brake_status = _operator_brake_status(brake)
    return {
        "held": "operator_brake",
        "free": None,
        "failed": "operator_brake_check_failed",
    }.get(brake_status, "operator_brake_check_failed")


def _write_disk_pending(output: Path, room: str, reason: str, checkpoint: str) -> int:
    """Persist a no-effect checkpoint when pressure reaches an in-flight boundary."""
    _write(output, {
        "status": "pending", "talkroom_id": room, "failed": 0,
        "effect": 0, "readback": 0, "send_performed": False,
        "checkpoint": checkpoint, "reason": reason,
    })
    return 0


def _persist_disk_checkpoint(args, room: str, item: dict[str, Any], reason: str,
                             checkpoint: str) -> str:
    """Write a deterministic per-room pending marker before any queue mutation."""
    path = args.evidence_dir / "paid-direct" / "items" / f"item-{room}-disk-checkpoint.json"
    _write(path, {
        "version": 1, "status": "pending", "talkroom_id": room,
        "buyer_feedback_sha256": _text(item.get("buyer_feedback_sha256")),
        "effect": 0, "readback": 0, "checkpoint": checkpoint,
        "reason": reason,
    })
    return str(path)


def _parent_disk_block(args, room: str, item: dict[str, Any], reason: str,
                       checkpoint: str) -> dict[str, Any]:
    try:
        path = _persist_disk_checkpoint(args, room, item, reason, checkpoint)
    except OSError:
        return {
            "talkroom_id": room, "status": "failed", "failed_step": "disk_checkpoint",
            "effect": 0, "readback": 0, "reason": reason,
        }
    return {
        "talkroom_id": room, "status": "disk_pressure", "send_performed": False,
        "deduplicated": False, "checkpoint": checkpoint, "reason": reason,
        "checkpoint_path": path,
    }


def _update_disk_checkpoint(path: Path, **updates: Any) -> bool:
    """Atomically advance a per-item checkpoint; return false if durability is unavailable."""
    try:
        current = _load(path) if path.is_file() else {}
        if not isinstance(current, dict):
            current = {}
        current.update(updates)
        _write(path, current)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _paid_project_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=PAID_MAX_PARALLEL_PROJECTS,
        thread_name_prefix="paid-project",
    )


def _reported_paid_row(args, item: dict[str, Any]) -> dict[str, Any] | None:
    room = _text(item.get("talkroom_id"))
    effect_policy = _account_owner_observe_only(args, item)
    if effect_policy is not None:
        return {"talkroom_id": room, "status": "reserved_for_owner",
                "send_performed": False, "deduplicated": True,
                "formal_delivery_checkbox": False,
                "evidence_paths": {"official_readback": str(effect_policy)}}
    handoff = _reported_handoff_cycle(args, item)
    if handoff is not None:
        return {"talkroom_id": room, "status": "awaiting_buyer",
                "send_performed": False, "deduplicated": True,
                "formal_delivery_checkbox": False,
                "evidence_paths": {"official_readback": str(
                    handoff / "delivery" / "paid-external-handoff.json")}}
    if _reported_formal_cycle(args, item) is not None:
        return {"talkroom_id": room, "status": "awaiting_buyer",
                "send_performed": False, "deduplicated": True,
                "formal_delivery_checkbox": True,
                "evidence_paths": {"official_readback": _text(item.get("talkroom_evidence_file"))}}
    if _reported_file_progress_cycle(args, item) is not None:
        return {"talkroom_id": room, "status": "awaiting_buyer",
                "send_performed": False, "deduplicated": True,
                "formal_delivery_checkbox": False,
                "evidence_paths": {"official_readback": _text(item.get("talkroom_evidence_file"))}}
    if _reported_remote_cycle(args, item) is not None:
        return {"talkroom_id": room, "status": "completed",
                "send_performed": False, "deduplicated": True,
                "formal_delivery_checkbox": False,
                "evidence_paths": {"official_readback": _text(item.get("talkroom_evidence_file"))}}
    return None


def _admitted_paid_projects(args, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available = [item for item in items if not _paid_timed_retry_is_future(args, item)]
    if not available:
        return []
    available.sort(key=lambda item: _paid_queue_priority(args, item))
    admission = paid_admission.plan(
        available,
        projects_root=args.projects_root,
        max_orders=len(available),
    )
    paid_admission.record_decisions(
        admission, projects_root=args.projects_root,
        pass_id=f"paid-direct-{time.time_ns()}",
    )
    admitted = set(admission.get("admitted") or [])
    return [item for item in available if paid_admission.stable_identity(item) in admitted]


def _paid_timed_retry_is_future(args, item: dict[str, Any], now: float | None = None) -> bool:
    try:
        retry = _load(_paid_project_root(args, item) / "context" / "paid-retry.json")
        if retry.get("version") != 1 or retry.get("status") != "timed_retry":
            return False
        due = datetime.fromisoformat(_text(retry.get("retry_not_before")).replace("Z", "+00:00"))
        return due.timestamp() > (time.time() if now is None else now)
    except (AttributeError, Failure, OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _paid_pending_count(rows: dict[str, dict[str, Any]]) -> int:
    return sum(row.get("status") in {"pending", "disk_pressure", "queued"}
               for row in rows.values())


def _paid_parent_status(*, failed: int, pending: int) -> str:
    return "failed" if failed else ("pending" if pending else "completed")


def run_once(args, output: Path) -> int:
    # An operator must be able to stand this lane down without unloading launchd. This is the
    # one lane that can press an irreversible formal-delivery control, so it checks the brake
    # before it takes the writer lock or observes anything.
    empty = {"observed": 0, "actionable": 0, "effect": 0, "readback": 0,
             "failed": 0, "pending": 0, "oldest": None, "items": []}
    brake_status = _operator_brake_status(args.operator_brake)
    if brake_status == "failed":
        _write(output, {"status": "failed", "failed": 1,
                        "failed_step": "operator_brake_check_failed",
                        **{key: value for key, value in empty.items() if key != "failed"}})
        return 1
    if brake_status == "held":
        _write(output, {"status": "operator_brake",
                        "operator_brake_file": str(args.operator_brake), **empty})
        return 0
    with _lock(args.lock_file) as acquired:
        if not acquired:
            _write(output, {"status": "busy", "observed": 0, "actionable": 0, "effect": 0, "readback": 0, "failed": 0, "pending": 0, "oldest": None, "items": []}); return 0
        try:
            observed_items = observe_orders(args, args.evidence_dir / "orders")
            items, duplicate_dropped = _unique_orders(observed_items)
        except Failure as error:
            _write(output, {"status": "failed", "observed": 0, "actionable": 0, "effect": 0, "readback": 0, "failed": 1, "pending": 0, "oldest": None, "failed_step": error.step, "items": []}); return 1
        items.sort(key=lambda item: _paid_queue_priority(args, item))
        rows: dict[str, dict[str, Any]] = {}
        actionable = 0
        failed = effect = readback = 0; failed_step = ""
        targeted_items = []
        with ThreadPoolExecutor(
            max_workers=PAID_MAX_PARALLEL_READBACKS, thread_name_prefix="paid-refresh",
        ) as refresh_executor:
            refresh_jobs = [
                (item, refresh_executor.submit(_targeted, args, item, index))
                for index, item in enumerate(items)
            ]
            for original, job in refresh_jobs:
                room = _text(original.get("talkroom_id"))
                try:
                    targeted_items.append(job.result())
                except (Failure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                    step = error.step if isinstance(error, Failure) else "targeted_readback"
                    failed, failed_step = failed + 1, step
                    rows[room] = {"talkroom_id": room, "status": "failed", "failed_step": step,
                                  "error_detail": str(error)[:500]}
        work_candidates = []
        for item in targeted_items:
            room = _text(item.get("talkroom_id"))
            if item.get("_paid_targeted_status") == "pending":
                rows[room] = {"talkroom_id": room, "status": "pending",
                              "reason": "browser_lease_busy",
                              "browser_lease_owner": item.get("browser_lease_owner")}
                continue
            reported = _reported_paid_row(args, item)
            if reported is not None:
                rows[room] = reported
                readback += 1
            else:
                work_candidates.append(item)
        admitted_items = _admitted_paid_projects(args, work_candidates)
        admitted_rooms = {_text(item.get("talkroom_id")) for item in admitted_items}
        for item in work_candidates:
            room = _text(item.get("talkroom_id"))
            if room not in admitted_rooms:
                rows[room] = {"talkroom_id": room, "status": "queued"}

        executor = _paid_project_executor()
        jobs = []
        disk_blocked_reason: str | None = None
        for item in admitted_items:
            room = _text(item.get("talkroom_id"))
            if disk_blocked_reason is None:
                disk_blocked_reason = _effect_gate_reason(args)
            if disk_blocked_reason is not None:
                rows[room] = _parent_disk_block(
                    args, room, item, disk_blocked_reason, "before_project_queue_mutation",
                )
                if rows[room].get("status") == "failed":
                    failed, failed_step = failed + 1, "disk_checkpoint"
                continue
            room = _text(item.get("talkroom_id"))
            try:
                delivery_project.record_queue_selection(
                    args.projects_root, item, adapter="coconala",
                )
                resolved = _recoverable(args, item)
                if resolved is None:
                    root = _paid_project_root(args, item)
                    if _answer_ready(root, item):
                        resolved = _recoverable(args, item)
                    elif (root.is_dir() and not root.is_symlink()
                          and (root / "requirements" / "live-buyer-reply.json").is_file()
                          and re.fullmatch(r"[0-9a-f]{64}", _text(item.get("buyer_feedback_sha256")))):
                        root.resolve().relative_to(args.projects_root.resolve())
                        # Decision generation belongs to the project worker. It revalidates after
                        # DM collection, so doing it here only serializes independent projects and
                        # can become stale before the worker starts.
                        resolved = (root.resolve(), None)
            except (Failure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                step = error.step if isinstance(error, Failure) else "context_compile"
                failed, failed_step = failed + 1, step
                rows[room] = {"talkroom_id": room, "status": "failed", "failed_step": step}
                continue
            if resolved is None: rows[room] = {"talkroom_id": room, "status": "pending"}; continue
            root, _ = resolved; private = {key: item[key] for key in (
                "request_id", "contract_id", "talkroom_id", "title", "marketplace_url", "talkroom_url",
                "buyer_feedback_sha256", "buyer_feedback_stage", "buyer_feedback_pending_artifact",
                "buyer_feedback_identity_sha256", "buyer_feedback_message_identities",
                "buyer_feedback_requirements_path", "buyer_reply_after_artifact_observed",
                "buyer_formal_delivery_hold", "delivery_date", "status", "talkroom_state",
                "talkroom_evidence_file", "talkroom_observed_at", "snapshot_captured_at",
                "talkroom_evidence_sha256", "talkroom_screenshot_sha256", "formal_delivery_observed",
                "formal_delivery_confirmed", "formal_delivery_control_checked",
                "formal_delivery_control_disabled", "buyer_visible_artifact_observed",
                "room_contract_kind", "price_jpy", "price_source", "buyer",
            ) if key in item}
            private.update(project_root=str(root))
            if disk_blocked_reason is None:
                disk_blocked_reason = _effect_gate_reason(args)
            if disk_blocked_reason is not None:
                rows[room] = _parent_disk_block(
                    args, room, item, disk_blocked_reason, "before_paid_item",
                )
                if rows[room].get("status") == "failed":
                    failed, failed_step = failed + 1, "disk_checkpoint"
                continue
            item_file = args.evidence_dir / "paid-direct" / "items" / f"item-{room}.json"
            effect_file = item_file.with_name(item_file.stem + "-result.json")
            _write(item_file, private)
            actionable += 1
            prepared_file = item_file.with_name(item_file.stem + "-prepared.json")
            effect_file.unlink(missing_ok=True); prepared_file.unlink(missing_ok=True)
            _reclaim_paid_tabs(args, room)
            jobs.append((room, executor.submit(
                _run_paid_item, args, room, item_file, prepared_file, effect_file,
            )))
        executor.shutdown(wait=True)
        for room, job in jobs:
            try:
                row, item_effect, item_readback, item_failed, item_step = job.result()
            finally:
                _reclaim_paid_tabs(args, room)
            rows[room] = row
            effect += item_effect
            readback += item_readback
            failed += item_failed
            if item_step:
                failed_step = item_step
        dates = [_text(item.get("delivery_date")) for item in items if _text(item.get("delivery_date"))]
        if any(
                isinstance(row, dict)
                and row.get("status") == "pending"
                and row.get("checkpoint") in {
                    "before_paid_effect", "before_file_browser_effect", "before_answer_browser_effect",
                }
                for row in rows.values()
        ):
            disk_blocked_reason = disk_blocked_reason or "disk_pressure"
        pending = _paid_pending_count(rows)
        result_status = _paid_parent_status(failed=failed, pending=pending)
        try:
            terminal_reconciliation = _reconcile_absent_talkrooms(args, items)
        except Exception as error:
            terminal_reconciliation = {
                "status": "failed", "failed_step": "terminal_reconciliation",
                "error": type(error).__name__,
            }
        try:
            janitor = project_janitor.scan(
                args.projects_root, args.projects_root.parent / "janitor.jsonl", dry_run=False,
            )
            _write(args.evidence_dir / "project-janitor.json", janitor)
        except Exception as error:
            janitor = {
                "status": "failed", "failed_step": "project_janitor",
                "error": type(error).__name__,
            }
        result = {"status": result_status, "observed": len(items),
                  "duplicate_dropped": duplicate_dropped, "actionable": actionable,
                  "effect": effect, "readback": readback, "failed": failed,
                  "pending": pending,
                  "oldest": min(dates, default=None),
                  "project_janitor": janitor, "terminal_reconciliation": terminal_reconciliation,
                  "items": [rows[_text(item["talkroom_id"])] for item in items]}
        if failed_step: result["failed_step"] = failed_step
        _write(output, result); return int(bool(failed))

def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("output", "evidence-dir"): parser.add_argument(f"--{flag}", required=True, type=Path)
    parser.add_argument("--projects-root", type=Path, default=Path.home() / "gig/projects"); parser.add_argument("--collector", type=Path, default=HERE / "coconala_queue_snapshot.py")
    parser.add_argument("--run-with-cdp-lock", type=Path, default=HERE / "run_with_cdp_lock.sh"); parser.add_argument("--answer-browser", type=Path, default=HERE / "coconala_paid_progress_browser.py")
    parser.add_argument("--formal-browser", type=Path, default=HERE / "coconala_formal_delivery_browser.py")
    parser.add_argument("--cancel-browser", type=Path, default=HERE / "coconala_cancel_browser.py")
    parser.add_argument("--delivery-evidence-dir", type=Path, default=Path.home() / "gig" / "delivery-evidence")
    parser.add_argument("--cdp-lock-dir", type=Path, default=Path.home() / "gig" / ".cdp-gig.lock")
    parser.add_argument("--cdp-helper", type=Path, default=BROWSER_DIR / "scripts" / "cdp_default_tab.py"); parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--context-compiler", type=Path, default=HERE / "project_context_compiler.py"); parser.add_argument("--agent-runner", type=Path, default=RUNNER_DIR / "agent_runner.py")
    parser.add_argument("--dm-collector", type=Path, default=HERE / "coconala_dm_collect.py")
    parser.add_argument("--runner-schema", type=Path, default=HERE.parent / "schemas/gig_step_result.schema.json")
    parser.add_argument("--artifact-schema", type=Path, default=HERE.parent / "schemas/paid_file_judgement.schema.json")
    parser.add_argument("--decision-schema", type=Path, default=HERE.parent / "schemas/paid_work_decision.schema.json")
    parser.add_argument("--telegram-database", type=Path, default=DEFAULT_TELEGRAM_DATABASE)
    parser.add_argument("--telegram-receipt-dir", type=Path, default=DEFAULT_TELEGRAM_RECEIPTS)
    parser.add_argument("--telegram-target", default=os.environ.get("GIG_REPORT_CHAT", ""))
    parser.add_argument("--openclaw", type=Path, default=Path("/opt/homebrew/bin/openclaw"))
    parser.add_argument("--operator-brake", type=Path,
                        default=Path(os.environ.get("GIG_OPERATOR_BRAKE_FILE", DEFAULT_BRAKE)))
    parser.add_argument("--today", default=date.today().isoformat()); parser.add_argument("--effect-item", type=Path); parser.add_argument("--write-item", type=Path); parser.add_argument("--decision-item", type=Path); return parser

def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    for name in ("output", "evidence_dir", "projects_root", "collector", "run_with_cdp_lock", "answer_browser", "formal_browser", "cancel_browser", "delivery_evidence_dir", "cdp_lock_dir", "context_compiler", "dm_collector", "agent_runner", "runner_schema", "artifact_schema", "decision_schema"): setattr(args, name, getattr(args, name).expanduser().resolve())
    args.cdp_helper = args.cdp_helper.expanduser(); args.lock_file = args.lock_file.expanduser().resolve() if args.lock_file else args.evidence_dir / ".paid-direct.lock"
    if args.write_item: return _write_one(args, args.write_item.expanduser().resolve(), args.output)
    if args.effect_item: return _prepare_one(args, args.effect_item.expanduser().resolve(), args.output)
    if args.decision_item: return _decision_only(args, args.decision_item.expanduser().resolve(), args.output)
    run_id = f"{time.time_ns()}-{os.getpid()}"
    parent_browser_owner = os.environ.get("CLOAK_BROWSER_OWNER", "gig-paid-direct")
    _reclaim_browser_owner(args, parent_browser_owner)
    try:
        rc = run_once(args, args.output)
        result = _load(args.output)
    except Exception as error:
        rc = 1
        result = {"status": "failed", "observed": 0, "actionable": 0, "effect": 0,
                  "readback": 0, "failed": 1, "pending": 0, "oldest": None,
                  "failed_step": "paid_direct", "error": type(error).__name__,
                  "error_detail": str(error)[:500], "items": []}
    finally:
        _reclaim_browser_owner(args, parent_browser_owner)
    try:
        result["telegram"] = _report_paid_wake(args, result, run_id)
    except Exception as error:
        result["telegram"] = {"status": "failed", "error": type(error).__name__}
    _write(args.output, result)
    return rc

if __name__ == "__main__": raise SystemExit(main())
