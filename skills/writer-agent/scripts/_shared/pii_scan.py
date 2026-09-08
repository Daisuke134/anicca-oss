#!/usr/bin/env python3
"""Fail-closed PII/identity scanner for everything this pipeline makes public.

PORTED 2026-07-30 from the writer-engine worktree
(anicca-project/.worktrees/writer-pii-gate/skills/writer-engine/gates/safety/pii_scan.py,
sha256 490173d3de7d555afe5086ba4316d12221ebf2775cd539cb1f08bfede9d991cf) into the pipeline that
actually publishes. Detector behaviour is byte-identical to that source. Exactly two things
changed, both marked "PORT:" below:
  * the last-resort ledger path defaults to this skill's own state/ dir instead of
    ~/.local/share/writer-engine (that is a DIFFERENT engine's data dir);
  * load_blocklist() falls back to reading only the two WRITER_PII_* keys out of
    LIFE_MANAGER_ENV_FILE, because several publish scripts here are launched by schedulers
    that never source that file. It never reads, prints or returns any other key.

Measured breach that motivated it: an operator-personal GitHub handle and a city-level
machine location reached the body of already-published articles through freely generated
"code is here: https://github.com/<handle>/..." lines -- they are in no template, so a
publish-time gate is the only reliable stop.

Two detector classes, both deterministic (no model in the loop, unlike the sibling
identity-gate.sh which judges *persona* rather than *identifiers*):

  1. blocklist -- exact, case-insensitive literals. The blocklist itself is PII, so it is NEVER
     stored in this repo: it is loaded at runtime from WRITER_PII_BLOCKLIST (newline- or
     comma-separated) and/or a file named by WRITER_PII_BLOCKLIST_FILE. Neither configured is a
     configuration FAILURE, not a pass -- callers must treat PIIConfigError as "do not publish".
  2. patterns -- generic regexes for email, credit-card-shaped digit runs (Luhn-checked to cut
     false positives), international/JP phone numbers, and JP postal-code + address fragments.
     Plus one cross-class rule: github.com/<handle> where the handle is in the blocklist.

Everything is scanned twice: as given, and NFKC-normalised, so a full-width or otherwise
compatibility-decomposed spelling of a blocklisted literal cannot walk past the gate. All
regexes are bounded (no nested unbounded quantifiers), so hostile input cannot make this hang.

Nothing here ever logs a matched literal. Findings carry a redacted span (`Dai***134`) and a
stable rule id; that pair is what reaches stderr, the ledger, and the operator.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPTS))
from writer_runtime_paths import life_manager_env_file  # noqa: E402

BLOCKLIST_ENV = "WRITER_PII_BLOCKLIST"
BLOCKLIST_FILE_ENV = "WRITER_PII_BLOCKLIST_FILE"
LOG_ENV = "WRITER_PII_LOG"
DATA_DIR_ENV = "WRITER_DATA_DIR"

MAX_SCAN_CHARS = 4_000_000

# PORT: this skill's own state dir, used only as the last-resort ledger location when neither
# WRITER_PII_LOG nor WRITER_DATA_DIR is set. `parents[2]` = skills/writer-agent.
_SKILL_ROOT = Path(__file__).resolve().parents[2]
# PORT: some callers do not source LIFE_MANAGER_ENV_FILE. Read ONLY the two
# WRITER_PII_* keys out of it, and only when the process environment does not already carry them.
# WRITER_PII_ENV_FILE overrides the path (tests point it at a path that does not exist, so they can
# still assert the unconfigured-blocklist refusal on a machine whose real .env is configured).
ENV_FILE_ENV = "WRITER_PII_ENV_FILE"
_ENV_FILE = life_manager_env_file()


def _dotenv_fallback(values: Mapping[str, str]) -> dict[str, str]:
    """Return {WRITER_PII_*: value} recovered from LIFE_MANAGER_ENV_FILE, or {} when unavailable.

    Never raises and never touches any other key: a missing/broken .env must leave the caller in
    the "blocklist not configured" state, which is itself a refusal, not a pass.
    """
    if str(values.get(BLOCKLIST_ENV, "")).strip() or str(values.get(BLOCKLIST_FILE_ENV, "")).strip():
        return {}
    configured = str(values.get(ENV_FILE_ENV, "")).strip()
    path = Path(configured).expanduser() if configured else _ENV_FILE
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    recovered: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, raw = line.partition("=")
        if not separator or key.strip() not in (BLOCKLIST_ENV, BLOCKLIST_FILE_ENV):
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        recovered[key.strip()] = raw
    return recovered


class PIIRefused(RuntimeError):
    """Base class for every reason this gate refuses to let content out."""


class PIIConfigError(PIIRefused):
    """The blocklist is absent or unusable, so nothing may be published."""


class PIIViolation(PIIRefused):
    """At least one identifier was found in content that was about to go public."""

    def __init__(self, stage: str, findings: Sequence["Finding"], *, sources: Sequence[str] = ()) -> None:
        self.stage = stage
        self.findings = tuple(findings)
        self.sources = tuple(sources)
        detail = ", ".join(summarize(self.findings)) or "no detail"
        super().__init__(f"PII gate BLOCK at {stage}: {detail}")

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.rule_id for item in self.findings))


@dataclass(frozen=True)
class Finding:
    """One hit. `redacted` is the only representation of the match that may be logged."""

    rule_id: str
    matched_kind: str
    start: int
    end: int
    redacted: str
    variant: str = "raw"
    source: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "matched_kind": self.matched_kind,
            "start": self.start,
            "end": self.end,
            "redacted": self.redacted,
            "variant": self.variant,
            "source": self.source,
        }


# ---------------------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------------------


def redact(value: str) -> str:
    """Return a span that identifies a match to a human without reproducing it."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= 4:
        return "*" * len(collapsed) or "***"
    keep = 3 if len(collapsed) >= 10 else 1
    return f"{collapsed[:keep]}***{collapsed[-keep:]}"


def summarize(findings: Iterable[Finding]) -> list[str]:
    """Render findings as `rule_id@offset(redacted)` -- safe for stderr and the ledger."""
    return [f"{item.rule_id}@{item.start}({item.redacted})" for item in findings]


# ---------------------------------------------------------------------------------------
# blocklist loading -- fail closed, never hardcoded
# ---------------------------------------------------------------------------------------


def load_blocklist(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Load operator identifiers from the environment. Missing configuration is an error."""
    values = os.environ if environ is None else environ
    # PORT: recover the two WRITER_PII_* keys from LIFE_MANAGER_ENV_FILE when the caller environment
    # does not already carry them (launchd/agent-spawned publishers). Failure here is a no-op, so
    # the "not configured" refusal below still fires.
    fallback = _dotenv_fallback(values)
    if fallback:
        values = {**dict(values), **fallback}
    raw: list[str] = []
    inline = str(values.get(BLOCKLIST_ENV, ""))
    if inline.strip():
        raw.append(inline)
    configured_path = str(values.get(BLOCKLIST_FILE_ENV, "")).strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        try:
            raw.append(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise PIIConfigError(f"PII blocklist file is unreadable: {path}") from error
    if not raw:
        raise PIIConfigError(
            f"PII blocklist is not configured; set {BLOCKLIST_ENV} or {BLOCKLIST_FILE_ENV}"
        )
    terms: list[str] = []
    for chunk in raw:
        for line in re.split(r"[\n,]", chunk):
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            terms.append(term)
    if not terms:
        raise PIIConfigError("PII blocklist is configured but contains no terms")
    return tuple(dict.fromkeys(terms))


# ---------------------------------------------------------------------------------------
# generic patterns
# ---------------------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,4}")
_CARD_CANDIDATE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
_PHONE_INTERNATIONAL = re.compile(
    r"(?<![0-9A-Za-z+])\+[0-9]{1,3}[ .-]?(?:\(?[0-9]{1,4}\)?[ .-]?)[0-9]{2,4}[ .-]?[0-9]{2,4}(?![0-9])"
)
_PHONE_JP_DOMESTIC = re.compile(
    r"(?<![0-9A-Za-z-])0[0-9]{1,4}[ -][0-9]{1,4}[ -][0-9]{3,4}(?![0-9])"
)
_POSTAL_JP_MARKED = re.compile(r"〒\s*[0-9]{3}-?[0-9]{4}")
_POSTAL_JP_ADDRESS = re.compile(
    r"(?<![0-9])[0-9]{3}-[0-9]{4}[\s　]*[^\s　]{0,12}?[都道府県][^\s　]{1,20}?[市区町村]"
)
_GITHUB_HANDLE = re.compile(r"(?i)\bgithub\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _pattern_findings(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in _EMAIL.finditer(text):
        findings.append(
            Finding("pii.pattern.email", "email", match.start(), match.end(), redact(match.group(0)))
        )
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"[^0-9]", "", match.group(0))
        # A run of one repeated digit (0000..., 1-1-1-...) is padding or separator noise, and
        # an all-zero run trivially satisfies Luhn. Real card numbers use more than one digit.
        if len(set(digits)) < 2:
            continue
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            findings.append(
                Finding(
                    "pii.pattern.credit-card", "credit-card",
                    match.start(), match.end(), redact(match.group(0)),
                )
            )
    for pattern in (_PHONE_INTERNATIONAL, _PHONE_JP_DOMESTIC):
        for match in pattern.finditer(text):
            findings.append(
                Finding(
                    "pii.pattern.phone", "phone",
                    match.start(), match.end(), redact(match.group(0)),
                )
            )
    for pattern in (_POSTAL_JP_MARKED, _POSTAL_JP_ADDRESS):
        for match in pattern.finditer(text):
            findings.append(
                Finding(
                    "pii.pattern.postal-jp", "postal-address-jp",
                    match.start(), match.end(), redact(match.group(0)),
                )
            )
    return findings


# ---------------------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------------------


def _blocklist_findings(text: str, blocklist: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    folded = text.casefold()
    for term in blocklist:
        needle = term.casefold()
        if not needle:
            continue
        start = folded.find(needle)
        while start >= 0:
            findings.append(
                Finding(
                    "pii.blocklist.literal", "blocklist",
                    start, start + len(needle), redact(text[start : start + len(needle)]),
                )
            )
            start = folded.find(needle, start + len(needle))
    for match in _GITHUB_HANDLE.finditer(text):
        handle = match.group(1).casefold()
        if any(handle == term.casefold() for term in blocklist):
            findings.append(
                Finding(
                    "pii.blocklist.github-handle", "github-handle",
                    match.start(1), match.end(1), redact(match.group(1)),
                )
            )
    return findings


def _variants(text: str) -> list[tuple[str, str]]:
    normalized = unicodedata.normalize("NFKC", text)
    if normalized == text:
        return [("raw", text)]
    return [("raw", text), ("nfkc", normalized)]


def scan(
    text: str,
    *,
    blocklist: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    source: str = "",
) -> list[Finding]:
    """Return every PII finding in `text`. Raises PIIConfigError when unconfigured."""
    terms = load_blocklist(environ) if blocklist is None else tuple(blocklist)
    if not terms:
        raise PIIConfigError("PII blocklist is empty")
    value = "" if text is None else str(text)
    if len(value) > MAX_SCAN_CHARS:
        raise PIIConfigError(
            f"PII gate refuses to scan {len(value)} characters (limit {MAX_SCAN_CHARS})"
        )
    seen: set[tuple[str, str, int]] = set()
    findings: list[Finding] = []
    for variant, candidate in _variants(value):
        for finding in _blocklist_findings(candidate, terms) + _pattern_findings(candidate):
            key = (finding.rule_id, finding.redacted, finding.start if variant == "raw" else -1)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                Finding(
                    finding.rule_id, finding.matched_kind, finding.start, finding.end,
                    finding.redacted, variant, source,
                )
            )
    findings.sort(key=lambda item: (item.source, item.start, item.rule_id))
    return findings


def scan_sources(
    sources: Mapping[str, str],
    *,
    blocklist: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Scan every labelled surface (body, title, alt text, metadata) in one pass."""
    terms = load_blocklist(environ) if blocklist is None else tuple(blocklist)
    findings: list[Finding] = []
    for label in sorted(sources):
        findings.extend(scan(sources[label], blocklist=terms, source=label))
    return findings


# ---------------------------------------------------------------------------------------
# recording + enforcement
# ---------------------------------------------------------------------------------------


def log_path(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = str(values.get(LOG_ENV, "")).strip()
    if configured:
        return Path(configured).expanduser()
    # PORT: last-resort location is this skill's own state/ dir. ~/.local/lib/writer-engine and
    # ~/.local/share/writer-engine belong to a DIFFERENT, older engine and are not written here.
    data_dir = str(values.get(DATA_DIR_ENV, "")).strip()
    if data_dir:
        return Path(data_dir).expanduser() / "ledger" / "pii-gate.jsonl"
    return _SKILL_ROOT / "state" / "pii-gate.jsonl"


def record(
    stage: str,
    verdict: str,
    findings: Sequence[Finding] = (),
    *,
    reason: str = "",
    context: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Append one redacted JSON line. Never raises -- a broken log must not unblock a block."""
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "gate": "pii_scan",
        "stage": stage,
        "verdict": verdict,
        "rule_ids": list(dict.fromkeys(item.rule_id for item in findings)),
        "findings": [item.as_record() for item in findings],
        "reason": reason,
        **({} if context is None else {"context": dict(context)}),
    }
    try:
        path = log_path(environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def enforce(
    stage: str,
    sources: Mapping[str, str],
    *,
    context: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise unless every source is clean. PIIConfigError and PIIViolation both mean STOP."""
    try:
        findings = scan_sources(sources, environ=environ)
    except PIIConfigError as error:
        record(stage, "CONFIG-FAIL", reason=str(error), context=context, environ=environ)
        raise
    if findings:
        record(stage, "BLOCK", findings, context=context, environ=environ)
        raise PIIViolation(stage, findings, sources=sorted(sources))
