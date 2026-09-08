#!/usr/bin/env python3
"""A bounded repair channel that can actually write. SSOT §9.3.1 item H2.

The first completed investigation returned `cause_status UNDETERMINED` with
`complete: true` and asked, in its own `remaining_work`, for note's current
terms, posting policy, and any official specification defining a 422 body
rejection, fetched as 原文 and matched against the failing request before
concluding a violation.  A read-only, offline judge cannot do that.  This module
is the capability, under guardrails copied wholesale from the prior art §9.3.1
already recorded.

That same verdict is also why this module refuses empty handoffs.  It named
zero `primary_sources` and put no URL in `remaining_work`, so `fetch_sources`
-- the only egress here, and one that takes explicit URLs -- would have fetched
nothing, and three bounded attempts would have been spent producing no
evidence-backed change: the "ran but did no work" signature H1 exists to catch.
Two mechanisms close that:

* **resolution without recall.**  `config/repair-source-registry.json` maps a
  destination to documents that were curated *by fetching them*, so `note/ja`
  resolves to note's own binding terms without any model remembering a URL.  A
  recalled URL is not merely unreliable, it is confidently wrong: during
  curation `https://note.com/guideline` returned HTTP 200 and is a user's
  profile page, not note's guideline.  A 200 proves reachability, never
  identity.
* **an empty set is refused before it costs anything.**  `UnresolvableSources
  Error` is raised before the worktree exists and before the plan advances
  `attempts`, so a verdict with nothing to read leaves the bounded budget
  intact for one that has.  The refusal travels the dispatcher's existing
  preparation-failure bound, so it is counted three times and then degrades --
  no second budget was invented for it.

The guardrails, copied wholesale from the prior art §9.3.1 already records:

* **Renovate** gates automerge strictly on green required checks.  Here the only
  status that a downstream stage will accept is `CANDIDATE_VERIFIED`, and it is
  written only when every configured test command exited `0`.  A failing
  candidate is written as `DISCARDED`, which
  `writer_incident_queue.register_candidate` rejects by schema -- so "green is
  the only gate" is enforced by the consumer, not by this module's good manners.
* **SapFix** validates each candidate in an isolated environment against
  existing plus generated tests and degrades to a full or partial revert when
  every candidate fails.  Here each attempt runs in its own git worktree and a
  discarded attempt is hard-reverted to `base_head`.
* **The escalation rule**: bounded attempts, then degrade to the safest known
  state, then stop until a genuinely new trigger.  The trigger is the *same*
  token the investigation path already uses -- occurrence count plus deployed
  commit, from `writer_repair_investigation_session` -- so there is one
  mechanism, not two.

What makes the external-effect prohibitions real rather than instructional:

| Prohibition | Enforcement |
|---|---|
| no publish/post/submit/send | the model runs with `network_access=false`; it has no egress at all.  The only outbound path in this channel is `fetch_sources`, which this module owns and which can issue nothing but an unauthenticated HTTPS `GET` |
| no write outside the workspace | `--sandbox workspace-write` with `/tmp` and `$TMPDIR` excluded, so the worktree is the sole writable root.  Detected as well as prevented: a canary outside the workspace and the source repository's own HEAD and porcelain status are compared before and after |
| no push to a protected branch | this module never runs `git push`, the model has no network, and `assert_unprotected_branch` refuses to operate on a protected branch name |
| no secrets read or emitted | the child environment is built from an allowlist, so no credential variable is handed over; and every changed file plus every fetched document is scanned before a candidate can be verified |

Deploying the candidate, resuming the work item, and the public readback are
Order 5 and are deliberately absent here.  This module ends at a verified
candidate plus its receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


SCRIPTS = Path(__file__).resolve().parent


def _module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:  # pragma: no cover - import machinery guard
        raise RuntimeError(f"cannot load {name}")
    spec.loader.exec_module(module)
    return module


session = _module("writer_repair_investigation_session")


PLAN_SCHEMA = "writer.self-heal.repair-candidate-plan"
# Fixed by the existing consumer `writer_incident_queue.register_candidate`.
RECEIPT_SCHEMA = "writer.self-heal.candidate-verification-receipt"
VERSION = 1

REPAIR_MODE = "repair"
ALLOWED_ARTIFACT_PREFIX = "skills/writer-agent/"
NEXT_ACTION = "VERIFY_SENSITIVE_REPAIR_IN_ISOLATED_FIXTURE"

# SapFix-shaped bound: a small number of candidates, then revert and stop.
DEFAULT_MAX_CANDIDATE_ATTEMPTS = 3
DEFAULT_BUDGET_SECONDS = 900
DEFAULT_TEST_TIMEOUT_SECONDS = 900
DEFAULT_SOURCE_TIMEOUT_SECONDS = 20
DEFAULT_SOURCE_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_SOURCES = 8

# Where a destination's official documents come from when the verdict names
# none.  Curated by fetching, never by recall -- see the file's own
# `curation_rule`.  It is data, not code, so adding a destination is a reviewed
# edit rather than a model's memory.
REGISTRY_SCHEMA = "writer.self-heal.repair-source-registry"
DEFAULT_SOURCE_REGISTRY_PATH = SCRIPTS.parent / "config" / "repair-source-registry.json"

# Whether a completed investigation is something this stage can act on.
# `verdict.complete` answers a different question: it says the investigation
# stopped paying for the model, not that it produced anything to repair from.
HANDOFF_ACTIONABLE = "ACTIONABLE"
HANDOFF_NO_SOURCES = "UNACTIONABLE_NO_RESOLVABLE_SOURCE"
HANDOFF_NOTHING_FETCHED = "UNACTIONABLE_NO_SOURCE_ARRIVED"


class UnresolvableSourcesError(ValueError):
    """This verdict cannot be repaired from, because nothing can be read.

    Raised before any attempt slot is charged and before any workspace exists,
    so a verdict with no fetchable evidence cannot silently consume the bounded
    budget a correct verdict will need.  Carries the per-URL resolution rows so
    the receipt can show exactly which document never arrived and why.
    """

    def __init__(self, message: str, *, status: str, resolution: list[dict[str, Any]]):
        super().__init__(message)
        self.status = status
        self.resolution = resolution

# The gate.  Written into the plan *before* the model runs and bound into the
# receipt by hash, so nothing the model produces can influence what verifies it.
DEFAULT_TEST_COMMANDS: list[list[str]] = [
    ["python3", "-m", "pytest", "-q", "skills/writer-agent/tests/", "-p", "no:cacheprovider"],
]

# A candidate branch may never be one of these, and this module never pushes.
PROTECTED_BRANCHES = frozenset({
    "main", "master", "dev", "develop", "release", "production", "HEAD",
})

# Only these names, plus ARTICLE_* minus the denylist, reach the model process.
ENVIRONMENT_ALLOWLIST = frozenset({
    "PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "TERM", "TZ", "SHELL", "USER", "LOGNAME", "PYTHONDONTWRITEBYTECODE",
})
ENVIRONMENT_DENYLIST = frozenset({
    # `sol-audit` is only reachable with a trigger receipt; a repair must never
    # carry one, so it is dropped rather than trusted.
    "ARTICLE_SOL_TRIGGER_RECEIPT",
})
CREDENTIAL_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY"
    r"|COOKIE|SESSION|CREDENTIAL|AUTH|BEARER|WEBHOOK)",
    re.IGNORECASE,
)

# Named rules, so a receipt can say which rule fired without quoting the match.
SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_credential", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{24,}")),
    ("assigned_credential", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|access[_-]?token|auth[_-]?token)\b"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9/+=_\-]{20,}"
    )),
)

FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")

TOKENS_UNKNOWN_REASON = (
    "the turn never completed, so codex emitted no turn.completed usage block; "
    "a token count here would be invented"
)
COST_UNKNOWN_REASON = (
    "no price table or billing receipt is joined to a model invocation in this "
    "runtime, so a cost figure would be invented"
)
DEGRADE_REASON = (
    "the repair channel spent its {maximum} bounded candidate attempts without a "
    "green test gate; per SSOT §9.3.1 it degrades to the safest known state -- "
    "the workspace is reverted to its base commit, nothing is deployed and "
    "nothing is published -- and stops until a genuinely new trigger arrives"
)


# ---------------------------------------------------------------------------
# small durable helpers
# ---------------------------------------------------------------------------

def _atomic(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _porcelain(worktree: Path) -> list[str]:
    # Deliberately not `_git`: that strips the output, and a porcelain line for
    # an unstaged modification begins with a space (` M path`). Stripping it
    # shifts every column by one and silently truncates the first character of
    # the path, which turns a legal `skills/...` change into a bogus
    # "path outside allowed scope" discard.
    lines = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    paths: list[str] = []
    for line in lines:
        if len(line) < 4:
            raise ValueError(f"unparseable git status entry: {line!r}")
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value.strip('"'))
    return sorted(paths)


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("./"):
        raise ValueError(f"unsafe relative path: {value}")
    if not value.startswith(ALLOWED_ARTIFACT_PREFIX) or value == ALLOWED_ARTIFACT_PREFIX:
        raise ValueError(f"path outside allowed scope: {value}")
    return value


# ---------------------------------------------------------------------------
# guardrails, each callable and therefore testable on its own
# ---------------------------------------------------------------------------

def assert_unprotected_branch(branch: str) -> str:
    """A repair candidate never lands on, and is never built from, a trunk."""
    if not branch or branch in PROTECTED_BRANCHES:
        raise ValueError(f"refusing to use protected branch: {branch!r}")
    if branch.startswith("release/"):
        raise ValueError(f"refusing to use protected branch: {branch!r}")
    return branch


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Build the model child's environment from an allowlist.

    A variable the child never receives is a secret the child can never emit.
    `ARTICLE_*` is admitted as a family because the model runner is configured
    through it, but any `ARTICLE_*` name that looks like credential material is
    dropped anyway, and the sol trigger receipt is dropped unconditionally.
    """
    source = dict(os.environ if base is None else base)
    scrubbed: dict[str, str] = {}
    for name, value in source.items():
        if name in ENVIRONMENT_DENYLIST:
            continue
        if CREDENTIAL_NAME_RE.search(name):
            continue
        if name in ENVIRONMENT_ALLOWLIST or name.startswith("ARTICLE_"):
            scrubbed[name] = value
    return scrubbed


def scan_for_secrets(text: str) -> list[str]:
    """Return the names of the rules that fired.  Never returns the match."""
    return sorted({name for name, pattern in SECRET_RULES if pattern.search(text)})


def assert_fetchable(url: str) -> str:
    """Refuse anything that is not an anonymous read of a public document.

    HTTPS only, no credentials in the URL, and no host that resolves into a
    private, loopback, link-local or reserved range, so this cannot be pointed
    at a machine-local service.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"only https sources may be fetched: {url!r}")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise ValueError(f"a source URL may not carry credentials: {url!r}")
    host = parts.hostname
    if not host:
        raise ValueError(f"source URL has no host: {url!r}")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable is recorded by the caller as UNFETCHED, not as a
        # violation; the shape of the URL is what this function decides.
        if host in {"localhost", "localhost.localdomain"}:
            raise ValueError(f"refusing a loopback source: {url!r}") from None
        return url
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast
        ):
            raise ValueError(f"refusing a non-public source address: {url!r}")
    return url


def _https_get(url: str, *, timeout: int, max_bytes: int) -> dict[str, Any]:
    """The single outbound call this whole channel is capable of making.

    Method is hard-coded to GET, no body is sent, no cookie jar exists, and no
    authorization header is added.  A GET cannot publish, post, or submit.
    """
    request = urllib.request.Request(  # noqa: S310 - scheme asserted by caller
        url, method="GET",
        headers={"User-Agent": "writer-agent-repair/1.0 (primary-source reader)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"source exceeds {max_bytes} bytes: {url!r}")
        return {
            "http_status": int(response.status),
            "content_type": response.headers.get("Content-Type"),
            "body": body,
        }


def fetch_sources(
    urls: Iterable[str], destination: Path, *, observed_at: str,
    timeout: int = DEFAULT_SOURCE_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_SOURCE_MAX_BYTES,
    max_sources: int = DEFAULT_MAX_SOURCES,
    getter: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch the primary documents the verdict named, and record what arrived.

    A later reader must be able to tell an official document from a guess, so
    every row carries the URL, the retrieval time, the byte count and the
    content hash.  A source that could not be fetched is recorded as
    `UNFETCHED` with the reason.  Nothing is ever invented.
    """
    getter = getter or _https_get
    destination.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for url in list(urls)[:max_sources]:
        row: dict[str, Any] = {"url": url, "fetched_at": observed_at}
        try:
            assert_fetchable(url)
            fetched = getter(url, timeout=timeout, max_bytes=max_bytes)
        except (ValueError, OSError) as error:
            row["status"] = "UNFETCHED"
            row["reason"] = f"{type(error).__name__}: {error}"
            rows.append(row)
            continue
        body = fetched["body"]
        digest = _sha256_bytes(body)
        path = destination / f"{digest[:16]}.source"
        path.write_bytes(body)
        row.update({
            "status": "FETCHED",
            "http_status": fetched.get("http_status"),
            "content_type": fetched.get("content_type"),
            "bytes": len(body),
            "sha256": digest,
            "path": str(path),
            "secret_rules_matched": scan_for_secrets(
                body.decode("utf-8", errors="replace")
            ),
        })
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# resolving what to fetch, without asking a model to recall a URL
# ---------------------------------------------------------------------------

def load_source_registry(path: Path | None = None) -> dict[str, Any]:
    """Read the curated per-destination registry, or an empty one.

    A missing or malformed registry is not an error here: it resolves to
    nothing, and resolving to nothing is refused loudly one call later by
    `assert_actionable_handoff`.  Failing at load time instead would turn a
    config problem into an unexplained crash.
    """
    path = Path(path) if path is not None else DEFAULT_SOURCE_REGISTRY_PATH
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict) or value.get("schema") != REGISTRY_SCHEMA:
        return {}
    return value


def registry_sources(
    registry: dict[str, Any] | None, destination: str | None,
) -> list[dict[str, Any]]:
    """The curated documents for one destination, or none.  Never a guess."""
    if not isinstance(registry, dict) or not destination:
        return []
    destinations = registry.get("destinations")
    if not isinstance(destinations, dict):
        return []
    entry = destinations.get(destination)
    if not isinstance(entry, dict):
        return []
    return [
        row for row in (entry.get("sources") or [])
        if isinstance(row, dict) and isinstance(row.get("url"), str) and row["url"]
    ]


def resolve_source_urls(
    verdict: dict[str, Any], destination: str | None,
    registry: dict[str, Any] | None, *, max_sources: int = DEFAULT_MAX_SOURCES,
) -> list[dict[str, Any]]:
    """Decide what this repair is allowed to read, and record where it came from.

    Order matters.  A `primary_sources` row must carry a quote to satisfy the
    verdict schema, so a verdict that names one had to have read it; those lead.
    The curated registry follows, deduplicated, as the documents the
    investigation would have read had it been given egress.  Nothing else is
    admitted: no model-recalled URL, no derived or guessed path.
    """
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in verdict.get("primary_sources") or []:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        resolved.append({
            "url": url, "provenance": "verdict",
            "title": row.get("title"),
        })
    for row in registry_sources(registry, destination):
        url = row["url"]
        if url in seen:
            continue
        seen.add(url)
        resolved.append({
            "url": url, "provenance": f"registry:{destination}",
            "title": row.get("title"), "role": row.get("role"),
        })
    return resolved[:max_sources]


def handoff_status(
    verdict: dict[str, Any], resolved: list[dict[str, Any]],
) -> str:
    """Is a completed investigation actionable by the repair channel?

    `verdict.complete` is the investigation's own budget statement: it means the
    slice loop stops paying for the model.  It is not a claim that the next
    stage can do anything.  A verdict that reaches `complete: true` with
    `cause_status UNDETERMINED` and zero resolvable sources is complete as an
    investigation and unactionable as a handoff, and those are different facts
    that must not share one boolean.
    """
    return HANDOFF_ACTIONABLE if resolved else HANDOFF_NO_SOURCES


def assert_actionable_handoff(
    *, verdict: dict[str, Any], destination: str | None,
    resolved: list[dict[str, Any]], rows: list[dict[str, Any]] | None = None,
) -> None:
    """Refuse an empty handoff before it can cost an attempt.

    Two ways a handoff is empty, and both are refused by name:

    * nothing to fetch -- the verdict named no `primary_sources` and no curated
      document exists for this destination;
    * nothing arrived -- every named URL failed, which is what a fabricated
      citation looks like from here.  A hallucinated URL cannot be told from a
      dead one, and neither is evidence, so both are absent rather than
      accepted.
    """
    if not resolved:
        raise UnresolvableSourcesError(
            "this verdict is complete but unactionable: its `primary_sources` "
            f"is empty and no curated source is registered for destination "
            f"{destination!r}, so the repair channel would fetch nothing and "
            "spend a bounded attempt producing no evidence-backed change",
            status=HANDOFF_NO_SOURCES, resolution=list(rows or []),
        )
    if rows is not None and not any(row.get("status") == "FETCHED" for row in rows):
        detail = "; ".join(
            f"{row.get('url')} -> {row.get('status')}: {row.get('reason')}"
            for row in rows
        )
        raise UnresolvableSourcesError(
            f"none of the {len(rows)} named source documents could be read, so "
            "there is no primary evidence to repair from. An unreachable URL "
            "and an invented one are indistinguishable here and neither counts "
            f"as a source: {detail}",
            status=HANDOFF_NOTHING_FETCHED, resolution=list(rows),
        )


# ---------------------------------------------------------------------------
# the bounded budget, sharing the investigation path's own trigger token
# ---------------------------------------------------------------------------

def checkpoint_path(state_root: Path, fingerprint: str) -> Path:
    return state_root / "self-heal" / "repair-candidates" / f"{fingerprint}.json"


def read_checkpoint(state_root: Path, fingerprint: str) -> dict[str, Any] | None:
    path = checkpoint_path(state_root, fingerprint)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if value.get("schema") != PLAN_SCHEMA or value.get("version") != VERSION:
        return None
    return value


def read_verdict(state_root: Path, fingerprint: str) -> dict[str, Any]:
    """The completed H3 investigation verdict this repair is allowed to act on."""
    investigation = session.read_checkpoint(state_root, fingerprint)
    if investigation is None:
        raise ValueError("no investigation checkpoint exists for this incident")
    if investigation.get("status") != "COMPLETE":
        raise ValueError("the investigation has not reached a verdict")
    verdict = investigation.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("complete") is not True:
        raise ValueError("the investigation verdict is absent or incomplete")
    return verdict


# ---------------------------------------------------------------------------
# stage 1 -- prepare an isolated workspace and the evidence to work from
# ---------------------------------------------------------------------------

REPAIR_PROMPT = """You are the Writer Agent repair worker for one incident.

Incident fingerprint: {fingerprint}

The completed investigation reached this verdict. It is the only cause material
you may rely on; do not re-derive it and do not contradict it without evidence:
{verdict}

Primary documents already fetched for you, by URL, retrieval time and content
hash. They are on disk and readable. `provenance` says where each came from:
`verdict` means the investigation cited it, `registry:<destination>` means it is
a curated official document for this destination, `caller` means it was passed
in explicitly. A row whose `status` is `UNFETCHED` never arrived and is not
evidence; its `reason` says why. If a document you need is not listed, say so in
`remaining_work`; never invent a URL, a quote, or a rule:
{sources}

Your workspace is the isolated checkout at {workspace} on branch {branch}.
It is the only place you can write. You have no network access at all.

Hard boundaries, all enforced outside this prompt by the sandbox and by the
verification that runs after you:
- Edit files only under {allowed_prefix} inside your workspace.
- Do not publish, post, submit, or send anything to any service.
- Do not run git push, git remote, or any deployment command.
- Do not write, echo, or embed credentials, tokens, cookies, or API keys.
- Do not modify anything outside your workspace.

Do this:
1. Read what you need, using the fetched documents as the authority.
2. Make the smallest change that repairs the named failure.
3. Add or update an automated test under {allowed_prefix}tests/ that fails
   before your change and passes after it.
4. Reply with the JSON object the output schema requires. If you could not
   reach a defensible change, set "complete" to false and say exactly what is
   missing in "remaining_work". A candidate you are unsure of is discarded by
   the test gate anyway, so an honest incomplete answer costs nothing.
"""

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "changed_paths", "rationale", "sources_used", "regression_test_path",
        "complete", "remaining_work",
    ],
    "properties": {
        "changed_paths": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
        "sources_used": {"type": "array", "items": {"type": "string"}},
        "regression_test_path": {"type": ["string", "null"]},
        "complete": {"type": "boolean"},
        "remaining_work": {"type": ["string", "null"]},
    },
}


def prepare(
    *, repo: Path, base_ref: str, repair_root: Path, state_root: Path,
    fingerprint: str, observed_at: str,
    destination: str | None = None,
    source_urls: Iterable[str] | None = None,
    source_registry: dict[str, Any] | None = None,
    registry_path: Path | None = None,
    test_commands: list[list[str]] | None = None,
    max_attempts: int = DEFAULT_MAX_CANDIDATE_ATTEMPTS,
    getter: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create the isolated workspace, fetch the evidence, and write the plan.

    Returns a plan whose `status` is `READY_TO_REPAIR`, or `EXHAUSTED` when the
    bounded attempts for the current trigger are spent.  `EXHAUSTED` spends no
    model time and creates no workspace.

    Raises `UnresolvableSourcesError` when nothing can be read.  That happens
    before the worktree is created and before the plan advances `attempts`, so a
    verdict with no evidence costs no part of the bounded budget.
    """
    if FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("invalid incident fingerprint")
    repo = Path(repo).resolve()
    state_root = Path(state_root).resolve()
    repair_root = Path(repair_root).resolve()

    verdict = read_verdict(state_root, fingerprint)
    deployed_commit = session.read_deployed_commit(state_root)
    investigation = session.read_checkpoint(state_root, fingerprint) or {}
    occurrence = (investigation.get("trigger") or {}).get("occurrence_count")
    trigger = session.trigger_token(occurrence, deployed_commit)

    checkpoint = read_checkpoint(state_root, fingerprint)
    same_trigger = checkpoint is not None and checkpoint.get("trigger") == trigger
    attempts_used = int(checkpoint.get("attempts", 0)) if same_trigger else 0

    if attempts_used >= max_attempts:
        reason = DEGRADE_REASON.format(maximum=max_attempts)
        degraded = {
            "schema": PLAN_SCHEMA, "version": VERSION, "status": "EXHAUSTED",
            "fingerprint": fingerprint, "observed_at": observed_at,
            "trigger": trigger, "attempts": attempts_used,
            "attempts_used": attempts_used, "max_attempts": max_attempts,
            "reason": reason, "next_action": "WAIT_FOR_NEW_TRIGGER",
            "deployed": False,
        }
        _atomic(checkpoint_path(state_root, fingerprint), degraded)
        return degraded

    if _git(repo, "status", "--porcelain"):
        raise ValueError("the repair source repository must be clean")
    base_head = _git(repo, "rev-parse", f"{base_ref}^{{commit}}")

    # ---- resolve and fetch the evidence *before* anything is spent ---------
    # The first completed investigation could cite zero official documents
    # because it had no network, and its `remaining_work` named no URL either.
    # An explicit `source_urls` still wins for a caller that knows better;
    # otherwise the verdict's own citations lead and the curated registry for
    # this destination fills the gap.  Evidence lives outside the workspace on
    # purpose: it must be readable by the model but must never show up as a
    # candidate artifact.
    task_dir = state_root / "self-heal" / "repair-candidates" / fingerprint
    if source_urls:
        resolved = [{"url": url, "provenance": "caller"} for url in source_urls]
    else:
        registry = (
            source_registry if source_registry is not None
            else load_source_registry(registry_path)
        )
        resolved = resolve_source_urls(verdict, destination, registry)
    assert_actionable_handoff(
        verdict=verdict, destination=destination, resolved=resolved,
    )
    sources = fetch_sources(
        [row["url"] for row in resolved], task_dir / "sources",
        observed_at=observed_at, getter=getter,
    )
    provenance = {row["url"]: row for row in resolved}
    for row in sources:
        origin = provenance.get(row["url"], {})
        row["provenance"] = origin.get("provenance", "caller")
        if origin.get("title"):
            row["title"] = origin["title"]
    assert_actionable_handoff(
        verdict=verdict, destination=destination, resolved=resolved, rows=sources,
    )
    handoff = {
        "status": handoff_status(verdict, resolved),
        "destination": destination,
        "resolved": len(resolved),
        "fetched": sum(1 for row in sources if row.get("status") == "FETCHED"),
        "unfetched": sum(1 for row in sources if row.get("status") != "FETCHED"),
        "verdict_named_sources": len(verdict.get("primary_sources") or []),
    }

    attempt = attempts_used + 1
    short = fingerprint[:12]
    # The trigger is part of the name so a re-armed budget gets fresh workspaces
    # instead of colliding with the ones its previous trigger already used.
    stamp = _sha256_bytes(json.dumps(trigger, sort_keys=True).encode("utf-8"))[:8]
    branch = assert_unprotected_branch(
        f"repair/writer-{short}-{stamp}-candidate-{attempt}"
    )
    workspace = repair_root / f"writer-{short}-{stamp}-candidate-{attempt}"
    if workspace.exists():
        raise ValueError(f"repair workspace already exists: {workspace}")
    branch_exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo, check=False,
    ).returncode == 0
    if branch_exists:
        raise ValueError(f"repair branch already exists: {branch}")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(workspace), base_head],
        cwd=repo, check=True, capture_output=True, text=True,
    )

    session.assert_strict_schema(SUMMARY_SCHEMA)
    summary_schema_path = _atomic(task_dir / f"summary-schema-{attempt}.json", SUMMARY_SCHEMA)

    commands = [list(command) for command in (test_commands or DEFAULT_TEST_COMMANDS)]
    if not commands or not all(
        command and all(isinstance(part, str) and part for part in command)
        for command in commands
    ):
        raise ValueError("the test gate requires a non-empty argv command list")
    commands_sha256 = _sha256_bytes(
        json.dumps(commands, sort_keys=True).encode("utf-8")
    )

    prompt = REPAIR_PROMPT.format(
        fingerprint=fingerprint,
        verdict=json.dumps(verdict, ensure_ascii=False, indent=2),
        sources=json.dumps(
            [
                {key: row[key] for key in row if key not in {"secret_rules_matched"}}
                for row in sources
            ],
            ensure_ascii=False, indent=2,
        ),
        workspace=workspace, branch=branch,
        allowed_prefix=ALLOWED_ARTIFACT_PREFIX,
    )
    prompt_path = task_dir / f"repair-prompt-{attempt}.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")

    # A canary the run must not be able to touch. It sits outside the workspace
    # and inside the state tree, so a boundary failure is detected as well as
    # prevented.
    canary_path = task_dir / f"boundary-canary-{attempt}.txt"
    canary_value = f"{fingerprint}:{attempt}:{observed_at}"
    canary_path.write_text(canary_value, encoding="utf-8")

    plan = {
        "schema": PLAN_SCHEMA, "version": VERSION, "status": "READY_TO_REPAIR",
        "observed_at": observed_at,
        "fingerprint": fingerprint,
        "attempt": attempt,
        # `attempts` is the consumed count and is advanced here rather than
        # after the run, so the bound fails closed: an attempt that crashes
        # between prepare and execute still costs a slot instead of being
        # retried for free against a workspace name that already exists.
        "attempts": attempt,
        "attempts_used": attempts_used,
        "max_attempts": max_attempts,
        "trigger": trigger,
        "deployed_commit": deployed_commit,
        "repo_path": str(repo),
        "state_root": str(state_root),
        "base_head": base_head,
        "branch": branch,
        "workspace_path": str(workspace),
        "allowed_prefix": ALLOWED_ARTIFACT_PREFIX,
        "verdict": verdict,
        "handoff": handoff,
        "sources": sources,
        "prompt_path": str(prompt_path),
        "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
        "summary_schema_path": str(summary_schema_path),
        "test_commands": commands,
        "test_commands_sha256": commands_sha256,
        "canary_path": str(canary_path),
        "canary_sha256": _sha256_bytes(canary_value.encode("utf-8")),
        "next_action": "RUN_BOUNDED_REPAIR",
        "deployed": False,
    }
    _atomic(checkpoint_path(state_root, fingerprint), plan)
    _atomic(task_dir / f"repair-plan-{attempt}.json", plan)
    return plan


# ---------------------------------------------------------------------------
# stage 2 -- run the bounded attempt and verify it, or discard it
# ---------------------------------------------------------------------------

def _revert(workspace: Path, base_head: str) -> dict[str, Any]:
    """SapFix's degrade: put the workspace back exactly at its base commit."""
    try:
        _git(workspace, "reset", "--hard", base_head)
        _git(workspace, "clean", "-fdx")
        return {"reverted": True, "to": base_head}
    except subprocess.CalledProcessError as error:
        return {
            "reverted": False, "to": base_head,
            "reason": (error.stderr or "")[-500:],
        }


def _discard(
    *, plan: dict[str, Any], receipt: dict[str, Any], check: str, reason: str,
    state_root: Path, out_path: Path, workspace: Path,
) -> dict[str, Any]:
    receipt["status"] = "DISCARDED"
    receipt["decision"] = "DISCARD"
    receipt["failed_check"] = check
    receipt["reason"] = reason
    receipt["next_action"] = "DISCARD_CANDIDATE"
    receipt["revert"] = _revert(workspace, plan["base_head"])
    return _finish(receipt=receipt, plan=plan, state_root=state_root, out_path=out_path)


def _finish(
    *, receipt: dict[str, Any], plan: dict[str, Any], state_root: Path,
    out_path: Path,
) -> dict[str, Any]:
    receipt["receipt_path"] = str(out_path)
    _atomic(out_path, receipt)
    checkpoint = dict(plan)
    checkpoint["attempts"] = int(plan["attempt"])
    checkpoint["attempts_used"] = int(plan["attempt"])
    checkpoint["status"] = (
        "CANDIDATE_VERIFIED" if receipt["status"] == "CANDIDATE_VERIFIED"
        else "DISCARDED"
    )
    checkpoint["last_receipt_path"] = str(out_path)
    checkpoint["updated_at"] = receipt["observed_at"]
    _atomic(checkpoint_path(state_root, plan["fingerprint"]), checkpoint)
    return receipt


def execute(
    *, plan: dict[str, Any], model_runner: Path, observed_at: str,
    budget_seconds: int = DEFAULT_BUDGET_SECONDS,
    environment: dict[str, str] | None = None,
    test_timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One bounded repair attempt: write in isolation, then prove it or drop it."""
    if plan.get("schema") != PLAN_SCHEMA or plan.get("version") != VERSION:
        raise ValueError("unsupported repair candidate plan")
    if plan.get("status") != "READY_TO_REPAIR":
        raise ValueError(f"plan is not runnable: {plan.get('status')}")

    fingerprint = plan["fingerprint"]
    state_root = Path(plan["state_root"])
    repo = Path(plan["repo_path"])
    workspace = Path(plan["workspace_path"])
    branch = assert_unprotected_branch(str(plan["branch"]))
    model_runner = Path(model_runner).resolve()
    if not model_runner.is_file() or not os.access(model_runner, os.X_OK):
        raise ValueError(f"model runner is not executable: {model_runner}")
    if _git(workspace, "branch", "--show-current") != branch:
        raise ValueError("workspace branch does not match the plan")
    if _git(workspace, "rev-parse", "HEAD") != plan["base_head"]:
        raise ValueError("workspace base does not match the plan")
    if _porcelain(workspace):
        raise ValueError("the repair workspace must start clean")

    commands = [list(command) for command in plan["test_commands"]]
    if _sha256_bytes(
        json.dumps(commands, sort_keys=True).encode("utf-8")
    ) != plan["test_commands_sha256"]:
        raise ValueError("the test gate does not match the hash bound into the plan")

    task_dir = state_root / "self-heal" / "repair-candidates" / fingerprint
    attempt = int(plan["attempt"])
    out_path = task_dir / f"candidate-{attempt}.json"

    # What the boundary looked like before the model ran.
    canary_path = Path(plan["canary_path"])
    repo_head_before = _git(repo, "rev-parse", "HEAD")
    repo_status_before = _porcelain(repo)

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA, "version": VERSION,
        "observed_at": observed_at,
        "fingerprint": fingerprint,
        "attempt": attempt,
        "max_attempts": int(plan["max_attempts"]),
        "trigger": plan["trigger"],
        "deployed_commit": plan.get("deployed_commit"),
        "repo_path": str(repo),
        "base_head": plan["base_head"],
        "branch": branch,
        "workspace_path": str(workspace),
        "verdict": plan["verdict"],
        "handoff": plan.get("handoff"),
        "sources": plan["sources"],
        "test_commands": commands,
        "test_commands_sha256": plan["test_commands_sha256"],
        "invariants": {
            "draft_is_public": False, "incident_resolved": False, "deployed": False,
        },
        "deployed": False,
        "published": False,
        "pushed": False,
        "tokens": {"status": "unknown", "reason": TOKENS_UNKNOWN_REASON},
        "cost": {"status": "unknown", "reason": COST_UNKNOWN_REASON},
    }

    # ---- the model writes, caged -----------------------------------------
    child = child_environment(environment)
    child["ARTICLE_REPAIR_WORKSPACE"] = str(workspace)
    child["ARTICLE_PROVIDER"] = "codex"
    child["ARTICLE_MODEL_ROLE"] = "terra"
    # Keep repair provider logs and health inside the isolated attempt state.
    # An explicit caller still wins; these defaults prevent any live Writer
    # state from becoming the repair attempt's model-runtime destination.
    child.setdefault("ARTICLE_RUN_ID", f"repair-{fingerprint[:12]}-{attempt}")
    child.setdefault("ARTICLE_MODEL_ROOT", str(repo))
    child.setdefault("ARTICLE_MODEL_STATE_ROOT", str(state_root))
    child.setdefault("ARTICLE_MODEL_LOG", str(task_dir / "model-runner.log"))
    child.setdefault(
        "ARTICLE_PROVIDER_HEALTH", str(state_root / "provider-health.json"),
    )

    run = session.run_slice(
        model_runner=model_runner, mode=REPAIR_MODE,
        prompt_path=Path(plan["prompt_path"]),
        events_path=task_dir / f"attempt-{attempt}.events.jsonl",
        last_message_path=task_dir / f"attempt-{attempt}.last-message.txt",
        verdict_schema=Path(plan["summary_schema_path"]),
        budget_seconds=budget_seconds, resume_session_id=None,
        environment=child,
        stdout_path=task_dir / f"attempt-{attempt}.stdout.txt",
        stderr_path=task_dir / f"attempt-{attempt}.stderr.txt",
    )
    stream = session.parse_events(Path(run["events_path"]))
    receipt["model"] = {
        "mode": REPAIR_MODE,
        "runner": str(model_runner),
        "return_code": run["return_code"],
        "killed_at_budget": run["killed_at_budget"],
        "budget_seconds": budget_seconds,
        "session_id": stream["session_id"],
        "stream_status": stream["stream_status"],
        "deciding_event": stream["deciding_event"],
        "event_count": stream["event_count"],
        "failures": stream["failures"],
        "events_path": run["events_path"],
    }
    receipt["tokens"] = session.tokens_from(stream)
    receipt["latency_ms"] = {"model": run["latency_ms"]}
    receipt["agent_summary"] = _agent_summary(Path(run["last_message_path"]), stream)

    # ---- the boundary, checked as well as caged ---------------------------
    canary_now = (
        _sha256_bytes(canary_path.read_bytes()) if canary_path.is_file() else None
    )
    repo_head_after = _git(repo, "rev-parse", "HEAD")
    repo_status_after = _porcelain(repo)
    boundary = {
        "canary_path": str(canary_path),
        "canary_intact": canary_now == plan["canary_sha256"],
        "source_repo_head_unchanged": repo_head_after == repo_head_before,
        "source_repo_clean": repo_status_after == repo_status_before,
        "source_repo_head": repo_head_after,
    }
    receipt["workspace_boundary"] = boundary
    if not all(
        (boundary["canary_intact"], boundary["source_repo_head_unchanged"],
         boundary["source_repo_clean"])
    ):
        return _discard(
            plan=plan, receipt=receipt, check="workspace_boundary",
            reason=(
                "the attempt changed something outside its isolated workspace; "
                "the candidate is discarded and the workspace reverted"
            ),
            state_root=state_root, out_path=out_path, workspace=workspace,
        )

    # ---- what it changed ---------------------------------------------------
    try:
        changed = _porcelain(workspace)
        for path in changed:
            _safe_relative(path)
    except ValueError as error:
        return _discard(
            plan=plan, receipt=receipt, check="allowed_paths",
            reason=f"{type(error).__name__}: {error}",
            state_root=state_root, out_path=out_path, workspace=workspace,
        )
    receipt["changed_paths"] = changed
    if not changed:
        return _discard(
            plan=plan, receipt=receipt, check="no_change",
            reason="the attempt produced no change, so there is no candidate",
            state_root=state_root, out_path=out_path, workspace=workspace,
        )

    artifacts: list[dict[str, Any]] = []
    matched_rules: list[dict[str, Any]] = []
    for path in changed:
        changed_file = workspace / path
        if changed_file.is_symlink() or not changed_file.is_file():
            return _discard(
                plan=plan, receipt=receipt, check="allowed_paths",
                reason=f"changed path is not a regular file: {path}",
                state_root=state_root, out_path=out_path, workspace=workspace,
            )
        payload = changed_file.read_bytes()
        artifacts.append({"path": path, "sha256": _sha256_bytes(payload)})
        rules = scan_for_secrets(payload.decode("utf-8", errors="replace"))
        if rules:
            matched_rules.append({"path": path, "rules": rules})
    receipt["artifacts"] = artifacts

    # The secret scan runs before the diff is recorded, so a receipt that
    # discards for credential material never carries that material itself.
    if matched_rules:
        receipt["secret_scan"] = {"clean": False, "matches": matched_rules}
        return _discard(
            plan=plan, receipt=receipt, check="secret_scan",
            reason=(
                "the change contains material matching a credential rule; the "
                "candidate is discarded and the matched text is not recorded"
            ),
            state_root=state_root, out_path=out_path, workspace=workspace,
        )
    source_rules = [
        {"url": row["url"], "rules": row["secret_rules_matched"]}
        for row in plan["sources"]
        if row.get("secret_rules_matched")
    ]
    if source_rules:
        receipt["secret_scan"] = {"clean": False, "matches": source_rules}
        return _discard(
            plan=plan, receipt=receipt, check="secret_scan",
            reason="a fetched document matched a credential rule",
            state_root=state_root, out_path=out_path, workspace=workspace,
        )
    receipt["secret_scan"] = {"clean": True, "rules": [name for name, _ in SECRET_RULES]}

    _git(workspace, "add", "-A")
    receipt["diff"] = _git(workspace, "diff", "--cached", "--unified=3")

    # ---- the gate ----------------------------------------------------------
    test_environment = child_environment(environment)
    test_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    test_environment["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    results: list[dict[str, Any]] = []
    green = True
    for command in commands:
        try:
            completed = subprocess.run(
                command, cwd=workspace, check=False, capture_output=True,
                text=True, timeout=test_timeout_seconds, env=test_environment,
            )
            exit_code = completed.returncode
            output = (completed.stdout + completed.stderr)[-4000:]
        except subprocess.TimeoutExpired:
            exit_code = 124
            output = f"test command exceeded {test_timeout_seconds}s"
        results.append({
            "command": command, "exit_code": exit_code, "output_excerpt": output,
        })
        if exit_code != 0:
            green = False
            break
    receipt["test_results"] = results
    if not green:
        failed = results[-1]
        return _discard(
            plan=plan, receipt=receipt, check="test_gate",
            reason=(
                f"the test gate command {failed['command']!r} exited "
                f"{failed['exit_code']}; only a green gate makes a candidate "
                "eligible"
            ),
            state_root=state_root, out_path=out_path, workspace=workspace,
        )

    # ---- green: commit inside the isolated workspace only -----------------
    if _porcelain(workspace) != changed:
        return _discard(
            plan=plan, receipt=receipt, check="test_gate",
            reason="running the gate changed the candidate's own file set",
            state_root=state_root, out_path=out_path, workspace=workspace,
        )
    _git(workspace, "add", "-A")
    _git(
        workspace, "-c", "user.email=repair@writer.invalid",
        "-c", "user.name=writer-repair",
        "commit", "-q", "-m",
        f"repair(writer): bounded candidate for incident {fingerprint[:12]}",
    )
    commit = _git(workspace, "rev-parse", "HEAD")
    if COMMIT_RE.fullmatch(commit) is None:  # pragma: no cover - git contract
        raise ValueError("git did not return a full commit id")
    receipt["feature_commit"] = commit
    receipt["status"] = "CANDIDATE_VERIFIED"
    receipt["decision"] = "ELIGIBLE_FOR_ISOLATED_FIXTURE_VERIFICATION"
    receipt["next_action"] = NEXT_ACTION
    return _finish(
        receipt=receipt, plan=plan, state_root=state_root, out_path=out_path,
    )


def _agent_summary(last_message: Path, stream: dict[str, Any]) -> dict[str, Any]:
    """The model's own account, recorded as a claim and never as evidence."""
    text = ""
    if last_message.is_file():
        text = last_message.read_text(encoding="utf-8", errors="replace").strip()
    if not text and stream.get("agent_messages"):
        text = str(stream["agent_messages"][-1]).strip()
    if not text:
        return {"status": "absent", "reason": "no final message was written"}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return {"status": "unparsed", "reason": str(error), "excerpt": text[:2000]}
    if not isinstance(value, dict):
        return {"status": "unparsed", "reason": "final message is not an object"}
    return {"status": "claimed", "claim": value}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--repo", required=True, type=Path)
    prepare_parser.add_argument("--base-ref", required=True)
    prepare_parser.add_argument("--repair-root", required=True, type=Path)
    prepare_parser.add_argument("--state-root", required=True, type=Path)
    prepare_parser.add_argument("--fingerprint", required=True)
    prepare_parser.add_argument("--observed-at", required=True)
    prepare_parser.add_argument("--source-url", action="append", default=[])
    # Without a destination the curated registry cannot be resolved, and a
    # verdict citing nothing is refused rather than run empty.
    prepare_parser.add_argument("--destination", default=None)
    prepare_parser.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_CANDIDATE_ATTEMPTS,
    )

    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--plan", required=True, type=Path)
    execute_parser.add_argument("--model-runner", required=True, type=Path)
    execute_parser.add_argument("--observed-at", required=True)
    execute_parser.add_argument(
        "--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS,
    )

    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(
            repo=args.repo, base_ref=args.base_ref, repair_root=args.repair_root,
            state_root=args.state_root, fingerprint=args.fingerprint,
            observed_at=args.observed_at, source_urls=args.source_url,
            destination=args.destination, max_attempts=args.max_attempts,
        )
    else:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        result = execute(
            plan=plan, model_runner=args.model_runner,
            observed_at=args.observed_at, budget_seconds=args.budget_seconds,
        )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
