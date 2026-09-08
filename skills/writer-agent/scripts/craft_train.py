#!/usr/bin/env python3
"""Nightly SkillOpt trainer for skills/writing-craft/CRAFT.md (spec 47
19-4c T6, BUILD 3). This module holds every PURE decision --  counting
scored runs, comparing sha256, deciding accept/reject from a training
summary, and enforcing that the protected SLOW_UPDATE block never
changes -- so the contract test can drive all of it with fixtures and
fakes, no live model call, no live network. `craft-train.sh` is the thin
shell wrapper: it owns exporting the two OPENAI_COMPATIBLE_* environment
variables (never echoing the key) and then calls this module's CLI.

Why a defense-in-depth protected-block check lives HERE too, not just in
SkillOpt's own skillopt/optimizer/skill.py::_is_in_protected_region: this
script is the last thing that writes to the real CRAFT.md on disk. If a
future SkillOpt version's protection regresses, or a hand-edited
best_skill.md ever gets fed in, the copy-back step must still refuse
rather than trust the upstream guarantee blindly.

T15 wall-clock follow-up: this module also owns the pre-flight projection
(project_and_check_deadline, part 4) and the hard subprocess deadline
(run_training's timeout_s, part 3) -- both pure/fixture-testable given an
explicit now/deadline pair, still no live model call and no live network.
The one non-stdlib import is `yaml` (to read the real config's
limit/sel_env_num/test_env_num for the projection), which is NOT part of
the `skillopt` package and is available to the plain system python this
script actually runs under -- importing `skillopt` itself (e.g. via
`writing.rollout`) would still break that, and is deliberately avoided.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SLOW_UPDATE_START = "<!-- SLOW_UPDATE_START -->"
SLOW_UPDATE_END = "<!-- SLOW_UPDATE_END -->"
DEFAULT_MIN_SCORED_RUNS = 3

# SkillOpt's trainer.py writes the two held-out test rollouts this gate
# reads under these exact, literal subdirectory names of out_root (grepped
# from skillopt/engine/trainer.py: `baseline_test_dir =
# os.path.join(out_root, "test_eval_baseline")`, `test_dir =
# os.path.join(out_root, "test_eval")`). writing/rollout.py's run_batch
# writes a rollouts.json into whatever out_root it is given, so these two
# files carry the exact per-item opponent_pairs SkillOpt used to compute
# baseline_test_soft and test_soft.
TEST_EVAL_BASELINE_DIRNAME = "test_eval_baseline"
TEST_EVAL_CANDIDATE_DIRNAME = "test_eval"

# Round-4/T15 follow-up (the noise-as-signal defect): the gate must not
# accept an edit on a difference indistinguishable from resampling noise.
# STANDARD_ERROR_P=0.5 is the worst-case (maximum-variance) assumption for
# a proportion estimate -- conservative, not tuned to make the gate look
# better. MARGIN_MULTIPLIER=2 means "accept only when the observed
# improvement clears 2 standard errors of the measurement" (roughly a
# 95%-confidence one-sided read); below that the verdict is NO EVIDENCE,
# and no-evidence must reject, or CRAFT.md drifts in an unproven direction
# while every log line looks healthy. See writing/rollout.py's
# GATE_OPPONENTS for where the n=15-opponents-per-item choice (SE~=0.09 at
# full sample) comes from -- this module never assumes that n was actually
# achieved; it counts the real non-null comparisons from the rollout
# artifacts (see _count_non_null_comparisons).
STANDARD_ERROR_P = 0.5
MARGIN_MULTIPLIER = 2

# T15 wall-clock follow-up: the config's split sizes were sized for a
# 4-item probe split; the real split grew to 119/21/38 and an uncapped run
# at GATE_OPPONENTS=15 projected to ~8,000 judge calls / ~22 hours serial
# -- straight through the 06:00 publish, holding the judge broker the
# whole time. TRAIN_OPPONENTS/GATE_OPPONENTS here MUST match
# writing/rollout.py's constants of the same name (duplicated, not
# imported, so this module keeps its plain-python-interpreter-only
# dependency footprint -- importing writing.rollout would pull in
# `skillopt.model`, which needs the skillopt venv).
TRAIN_OPPONENTS = 5
GATE_OPPONENTS = 15
ORDERINGS_PER_OPPONENT = 2  # both position-bias orderings, per beat_rate.py
# SkillOpt evaluates the held-out val split against 2 skill versions
# (current vs candidate, the per-step gate) and the held-out test split
# against 3 (baseline/init, best-on-val, final/last -- see
# TEST_EVAL_BASELINE_DIRNAME/TEST_EVAL_CANDIDATE_DIRNAME above, plus the
# final pass). This is the same per-phase multiplier model the projection
# was hand-verified against (train 20*5*2=200, val 10*15*2*2=600,
# test 8*15*2*3=720, total 1,520 -- see configs/writing/default.yaml's own
# arithmetic comment), not a literal step-by-step simulation of
# trainer.py's internal per-step gate loop.
VAL_SKILLS_PER_ITEM = 2
TEST_SKILLS_PER_ITEM = 3
DEFAULT_SECONDS_PER_CALL = 10  # measured empirically, T15 follow-up report


def split_item_count(split_dir: Path, split_name: str) -> int:
    """How many items are ACTUALLY in one split file on disk -- never
    assumed from a stale comment or a config value alone."""
    path = Path(split_dir) / split_name / "items.json"
    if not path.exists():
        return 0
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    return len(items) if isinstance(items, list) else 0


def effective_count(actual: int, cap: int) -> int:
    """SkillOpt's own semantics for limit/sel_env_num/test_env_num: 0 (or
    any falsy value) means uncapped -- use the whole split. A positive cap
    uses min(actual, cap)."""
    if not cap:
        return actual
    return min(actual, cap)


def project_calls(*, train_count: int, val_count: int, test_count: int) -> dict:
    """Expected judge-call volume for one training run, given the ACTUAL
    (post-cap) item counts per split. See the constants above for the
    per-phase multiplier model."""
    train_calls = train_count * TRAIN_OPPONENTS * ORDERINGS_PER_OPPONENT
    val_calls = val_count * GATE_OPPONENTS * ORDERINGS_PER_OPPONENT * VAL_SKILLS_PER_ITEM
    test_calls = test_count * GATE_OPPONENTS * ORDERINGS_PER_OPPONENT * TEST_SKILLS_PER_ITEM
    return {
        "train_calls": train_calls,
        "val_calls": val_calls,
        "test_calls": test_calls,
        "total_calls": train_calls + val_calls + test_calls,
    }


def project_and_check_deadline(
    config: dict, split_dir: Path, *, now: datetime, deadline: datetime,
    seconds_per_call: int = DEFAULT_SECONDS_PER_CALL,
) -> dict:
    """The part 4 refusal check: compute expected calls from the REAL
    config and REAL split sizes (never assumed), project the wall-clock
    time at seconds_per_call, and report whether that projection fits
    before `deadline`. Pure given its inputs (split_dir is read, but as
    fixture-style data, same as the rest of this module's disk reads) --
    the contract test drives this directly with a fixture config +
    fixture split files + a controlled now/deadline pair, no live model
    call and no live network."""
    env_cfg = config.get("env", {}) or {}
    eval_cfg = config.get("evaluation", {}) or {}

    train_actual = split_item_count(split_dir, "train")
    val_actual = split_item_count(split_dir, "val")
    test_actual = split_item_count(split_dir, "test")

    train_count = effective_count(train_actual, int(env_cfg.get("limit", 0) or 0))
    val_count = effective_count(val_actual, int(eval_cfg.get("sel_env_num", 0) or 0))
    test_count = effective_count(test_actual, int(eval_cfg.get("test_env_num", 0) or 0))

    projection = project_calls(train_count=train_count, val_count=val_count, test_count=test_count)
    projected_seconds = projection["total_calls"] * seconds_per_call
    projected_finish = now + timedelta(seconds=projected_seconds)
    fits = projected_finish <= deadline

    return {
        **projection,
        "train_count": train_count, "val_count": val_count, "test_count": test_count,
        "train_actual": train_actual, "val_actual": val_actual, "test_actual": test_actual,
        "seconds_per_call": seconds_per_call,
        "projected_seconds": projected_seconds,
        "now": now.isoformat(),
        "deadline": deadline.isoformat(),
        "projected_finish": projected_finish.isoformat(),
        "fits_before_deadline": fits,
    }


def load_yaml_config(config_path: Path) -> dict:
    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Pure functions -- what the contract test drives directly.
# ---------------------------------------------------------------------------

def count_scored_runs(runs_root: Path) -> int:
    """Distinct run directories under state/runs/ that carry at least one
    gates/beat-rate-*.json -- the "scored runs" spec 47 T5 gates T6 on
    (docs/loop-engineering/47-writer-loop-quality-and-self-improvement.md
    line 1086: "the trainer cannot perform a meaningful gate until real
    data accumulates... T6's 'it worked' verdict waits for T5's 3 runs").
    A run directory with a ledger but no gates/beat-rate-*.json (not yet
    scored, or scoring failed) does not count."""
    if not runs_root.exists():
        return 0
    scored = {
        path.parent.parent
        for path in runs_root.glob("*/gates/beat-rate-*.json")
    }
    return len(scored)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_protected_block(text: str) -> str | None:
    """The exact substring from SLOW_UPDATE_START through SLOW_UPDATE_END
    inclusive, or None if either marker is missing (callers must treat a
    missing marker as "cannot verify safety" and refuse, not as "nothing
    to protect")."""
    start = text.find(SLOW_UPDATE_START)
    end = text.find(SLOW_UPDATE_END)
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + len(SLOW_UPDATE_END)]


def _count_non_null_comparisons(rollouts_path: Path) -> int:
    """The ACTUAL comparison count behind one held-out rollout phase's
    beat_rate, read from its rollouts.json (written by writing/rollout.py's
    run_batch) -- never assumed from GATE_OPPONENTS*item_count. Each
    NON-NULL opponent pair required BOTH position-bias orderings to
    succeed (see beat_rate.py's score_one_pair -- either ordering failing
    makes the whole pair None), so a non-null pair contributes exactly 2
    raw judge comparisons; a null pair (judge failure, budget cutoff)
    contributes 0, not 2. Missing/unreadable file -> 0, the same "no
    evidence" treatment as zero comparisons."""
    if not rollouts_path.exists():
        return 0
    try:
        episodes = json.loads(rollouts_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    non_null_pairs = 0
    for episode in episodes:
        for pair in episode.get("opponent_pairs", []) or []:
            if pair.get("score") is not None:
                non_null_pairs += 1
    return non_null_pairs * 2


def _scored_mean(rollouts_path: Path) -> float | None:
    """Mean soft score over ONLY the items that actually got scored.

    SkillOpt's summary.json reports test_soft as a mean over every item, and
    writing/rollout.py scores an item 0.0 when its opponent pool was missing
    or every pair came back inconclusive (spec 47 section 21.45). Those items
    lower the mean while contributing nothing to _count_non_null_comparisons,
    so the numerator carries the outage and the denominator does not -- the
    margin is then computed against a mean it does not describe. Averaging
    here over the same set the count is drawn from keeps the two agreeing.

    None when nothing was scored: no evidence, which the caller rejects.
    """
    if not rollouts_path.exists():
        return None
    try:
        rows = json.loads(rollouts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    scored = [
        row for row in rows
        if isinstance(row, dict)
        and any(pair.get("score") is not None
                for pair in row.get("opponent_pairs", [])
                if isinstance(pair, dict))
    ]
    if not scored:
        return None
    return sum(float(row.get("soft") or 0.0) for row in scored) / len(scored)


def _standard_error(n_comparisons: int) -> float | None:
    """Worst-case (p=0.5) standard error of a proportion estimated from
    n_comparisons binary outcomes. None (undefined) at n=0 -- there is no
    measurement to have a standard error, which callers must treat as "no
    evidence", not as "SE happens to be 0"."""
    if n_comparisons <= 0:
        return None
    return math.sqrt(STANDARD_ERROR_P * (1 - STANDARD_ERROR_P) / n_comparisons)


def decide_from_summary(summary: dict, out_root: Path) -> dict:
    """SkillOpt's own trainer.py writes out_root/summary.json with this
    exact shape (verified by reading skillopt/engine/trainer.py's final
    `return summary` block): total_accepts/total_rejects/total_skips,
    baseline_test_soft (held-out beat rate of the INIT skill), test_soft
    (held-out beat rate of the best-on-val skill SkillOpt picked).
    "edits proposed" = steps that actually produced a patch to evaluate
    (accepts + rejects; a pure "skip" step yields no body patch by design,
    per trainer.py's own comment on lapse-only steps, so it is not a
    proposed edit). "edits accepted" = total_accepts.

    Round-4/T15 follow-up (the noise-as-signal defect): "held-out beat
    rate improved" is not, on its own, evidence -- two runs of the exact
    same title swung 0.00 -> 0.30 from resampling alone. So the accept
    verdict now requires THREE things: SkillOpt's own gate accepted at
    least one step; a real improvement (test_soft > baseline_test_soft);
    AND that improvement exceeds MARGIN_MULTIPLIER (2) standard errors of
    the measurement, where the standard error is computed from the ACTUAL
    comparison counts behind baseline_test_soft and test_soft (read from
    their rollouts.json artifacts, never assumed) and propagated as the SE
    of a DIFFERENCE of two independent proportions
    (sqrt(SE_before^2 + SE_after^2)) -- the correct treatment for "is this
    difference distinguishable from noise", not just "is one measurement
    noisy". Below the margin, the verdict is NO EVIDENCE, which rejects:
    keeping an unproven edit is how the file drifts while every log line
    looks healthy."""
    total_accepts = int(summary.get("total_accepts", 0) or 0)
    total_rejects = int(summary.get("total_rejects", 0) or 0)
    baseline_rollouts = Path(out_root) / TEST_EVAL_BASELINE_DIRNAME / "rollouts.json"
    candidate_rollouts = Path(out_root) / TEST_EVAL_CANDIDATE_DIRNAME / "rollouts.json"

    # Prefer the mean over scored items only; fall back to summary.json solely
    # when the artifacts are absent, in which case the comparison count is 0
    # too and the margin is undefined, so the run rejects for want of evidence
    # rather than on a mean nobody could check.
    beat_before = _scored_mean(baseline_rollouts)
    beat_after = _scored_mean(candidate_rollouts)
    if beat_before is None:
        beat_before = summary.get("baseline_test_soft")
    if beat_after is None:
        beat_after = summary.get("test_soft")

    n_before = _count_non_null_comparisons(baseline_rollouts)
    n_after = _count_non_null_comparisons(candidate_rollouts)
    se_before = _standard_error(n_before)
    se_after = _standard_error(n_after)
    standard_error = (
        math.sqrt(se_before ** 2 + se_after ** 2)
        if se_before is not None and se_after is not None
        else None
    )
    margin_required = MARGIN_MULTIPLIER * standard_error if standard_error is not None else None

    improvement = (
        beat_after - beat_before
        if beat_before is not None and beat_after is not None
        else None
    )
    clears_margin = (
        improvement is not None
        and margin_required is not None
        and improvement > margin_required
    )
    accepted = total_accepts > 0 and clears_margin

    if total_accepts == 0:
        reason = "SkillOpt accepted zero steps"
    elif margin_required is None:
        reason = (
            f"no evidence: insufficient comparisons to compute a margin "
            f"(n_before={n_before}, n_after={n_after})"
        )
    elif accepted:
        reason = (
            f"accepted: improvement {improvement:.4f} exceeds margin "
            f"{margin_required:.4f} ({MARGIN_MULTIPLIER}x SE={standard_error:.4f})"
        )
    else:
        reason = (
            f"no evidence: improvement {improvement:.4f} does not exceed margin "
            f"{margin_required:.4f} ({MARGIN_MULTIPLIER}x SE={standard_error:.4f}) -- "
            "rejecting rather than risk drift from noise"
        )

    return {
        "accepted": accepted,
        "edits_proposed": total_accepts + total_rejects,
        "edits_accepted": total_accepts if accepted else 0,
        "beat_rate_before": beat_before,
        "beat_rate_after": beat_after,
        "improvement": improvement,
        "margin_required": margin_required,
        "standard_error": standard_error,
        "n_comparisons_before": n_before,
        "n_comparisons_after": n_after,
        "reason": reason,
    }


def apply_result(
    craft_md_path: Path,
    old_content: str,
    accepted: bool,
    new_skill_content: str | None,
) -> dict:
    """The single place CRAFT.md is written. Returns what the jsonl
    summary line needs. On any doubt -- accepted is False, new content is
    missing, or the protected block would change -- this REFUSES to
    write and reports why; CRAFT.md is left byte-identical in every one
    of those cases, same as a plain reject."""
    sha_before = sha256_text(old_content)

    if not accepted:
        return {"applied": False, "sha_before": sha_before, "sha_after": sha_before,
                 "diff": "", "refuse_reason": None}

    if new_skill_content is None:
        return {"applied": False, "sha_before": sha_before, "sha_after": sha_before,
                 "diff": "", "refuse_reason": "accepted but no skill content available"}

    old_block = extract_protected_block(old_content)
    new_block = extract_protected_block(new_skill_content)
    if old_block is None or new_block is None or old_block != new_block:
        return {"applied": False, "sha_before": sha_before, "sha_after": sha_before,
                 "diff": "",
                 "refuse_reason": "protected SLOW_UPDATE block would change or is missing -- refusing"}

    craft_md_path.write_text(new_skill_content, encoding="utf-8")
    sha_after = sha256_text(new_skill_content)
    diff_text = "\n".join(
        __import__("difflib").unified_diff(
            old_content.splitlines(), new_skill_content.splitlines(),
            fromfile="CRAFT.md (before)", tofile="CRAFT.md (after)", lineterm="",
        )
    )
    return {"applied": True, "sha_before": sha_before, "sha_after": sha_after,
             "diff": diff_text, "refuse_reason": None}


def build_jsonl_line(*, edits_proposed: int, edits_accepted: int,
                      beat_before, beat_after, sha_before: str, sha_after: str,
                      reason: str, margin_required=None, standard_error=None,
                      n_comparisons_before=None, n_comparisons_after=None) -> str:
    """margin_required/standard_error (round-4/T15) are recorded even on a
    guard/infrastructure-reject line (as null) so every row has the same
    shape, and are the real numbers behind an accept/no-evidence-reject
    verdict so a night's decision can be audited after the fact without
    re-deriving it from the rollout artifacts."""
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "edits_proposed": edits_proposed,
        "edits_accepted": edits_accepted,
        "beat_rate_before": beat_before,
        "beat_rate_after": beat_after,
        "margin_required": margin_required,
        "standard_error": standard_error,
        "n_comparisons_before": n_comparisons_before,
        "n_comparisons_after": n_comparisons_after,
        "craft_sha256_before": sha_before,
        "craft_sha256_after": sha_after,
        "reason": reason,
    }
    return json.dumps(record, ensure_ascii=False)


def append_jsonl(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Impure: the real subprocess call and CLI wiring.
# ---------------------------------------------------------------------------

def run_training(python_bin: Path, run_train_py: Path, config_path: Path,
                  out_root: Path, *, timeout_s: float = 1800,
                  config_overrides: list[str] | None = None) -> dict:
    """Runs run_train.py for real. A nonzero exit or missing summary.json
    is an INFRASTRUCTURE failure -- e.g. the 401 that happens when the
    OPENAI_COMPATIBLE_* env vars are unset -- and must read as a reject
    with a stated reason, never as a crash. Never a partial success: if
    summary.json cannot be read, nothing here treats any number in it as
    real.

    timeout_s (part 3, T15 wall-clock follow-up) is the seconds remaining
    until the hard 05:00-local deadline, computed by the caller -- not a
    fixed budget. subprocess.run's own timeout mechanism kills the child
    (and everything it spawned) the moment that elapses, so a run that
    would otherwise finish at 08:45 instead stops cleanly with CRAFT.md
    never touched (this function returns ok=False, same as any other
    infrastructure failure, before any write to CRAFT.md is possible) and
    a reason that says "deadline", not a generic subprocess failure, so
    a night that ran out of time is distinguishable from one that broke."""
    out_root.mkdir(parents=True, exist_ok=True)
    try:
        command = [str(python_bin), str(run_train_py), "--config", str(config_path),
                   "--out_root", str(out_root)]
        if config_overrides:
            command.extend(["--cfg-options", *config_overrides])
        proc = subprocess.run(
            command,
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": f"deadline: training subprocess killed after {timeout_s:.0f}s without finishing",
        }
    except OSError as exc:
        return {"ok": False, "reason": f"training subprocess failed to start/finish: {exc}"}

    summary_path = out_root / "summary.json"
    if proc.returncode != 0 or not summary_path.exists():
        tail = (proc.stderr or "")[-800:]
        return {"ok": False, "reason": f"training subprocess exited {proc.returncode}: {tail}"}

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "reason": f"summary.json unreadable: {exc}"}

    best_skill_path = out_root / "best_skill.md"
    best_skill_content = (
        best_skill_path.read_text(encoding="utf-8") if best_skill_path.exists() else None
    )
    return {"ok": True, "summary": summary, "best_skill_content": best_skill_content}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--craft-md", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--split-dir", required=True)
    parser.add_argument("--run-train", required=True)
    parser.add_argument("--skillopt-python", required=True)
    parser.add_argument("--runs-root", required=True,
                         help="skills/writer-agent/state/runs -- used only for the guard count")
    parser.add_argument("--out-root", required=True,
                         help="fresh output directory for this training attempt")
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--min-scored-runs", type=int, default=DEFAULT_MIN_SCORED_RUNS)
    parser.add_argument("--deadline-epoch", type=int, required=True,
                         help="unix timestamp of the hard wall-clock deadline (part 3/4, T15 follow-up); "
                              "craft-train.sh computes this as the next local 05:00")
    parser.add_argument("--seconds-per-call", type=float, default=DEFAULT_SECONDS_PER_CALL)
    args = parser.parse_args(argv)

    craft_md_path = Path(args.craft_md)
    jsonl_path = Path(args.jsonl)
    old_content = craft_md_path.read_text(encoding="utf-8")
    sha_before = sha256_text(old_content)
    deadline = datetime.fromtimestamp(args.deadline_epoch)

    scored_runs = count_scored_runs(Path(args.runs_root))
    if scored_runs < args.min_scored_runs:
        reason = (
            f"guard: only {scored_runs} scored run(s) under {args.runs_root}, "
            f"need >= {args.min_scored_runs} -- refusing to gate on noisy data (spec 47 T5)"
        )
        print(reason)
        append_jsonl(jsonl_path, build_jsonl_line(
            edits_proposed=0, edits_accepted=0,
            beat_before=None, beat_after=None,
            sha_before=sha_before, sha_after=sha_before, reason=reason,
        ))
        return 0

    # Part 4: refuse to start if the projected finish would blow the
    # deadline -- the check that would have caught the ~22-hour run
    # without anyone doing the arithmetic by hand.
    now = datetime.now()
    config = load_yaml_config(Path(args.config))
    split_dir = Path(args.split_dir)
    config.setdefault("env", {})["skill_init"] = str(craft_md_path)
    config["env"]["split_dir"] = str(split_dir)
    projection = project_and_check_deadline(
        config, split_dir, now=now, deadline=deadline, seconds_per_call=args.seconds_per_call,
    )
    if not projection["fits_before_deadline"]:
        reason = (
            f"refusing to start: projected {projection['total_calls']} calls "
            f"({projection['projected_seconds']:.0f}s) would finish at "
            f"{projection['projected_finish']}, past the {deadline.isoformat()} deadline "
            f"(train={projection['train_count']} val={projection['val_count']} test={projection['test_count']})"
        )
        print(reason)
        append_jsonl(jsonl_path, build_jsonl_line(
            edits_proposed=0, edits_accepted=0,
            beat_before=None, beat_after=None,
            sha_before=sha_before, sha_after=sha_before, reason=reason,
        ))
        return 0

    remaining_s = max(0.0, (deadline - now).total_seconds())
    result = run_training(
        Path(args.skillopt_python), Path(args.run_train), Path(args.config),
        Path(args.out_root), timeout_s=remaining_s,
        config_overrides=[
            f"env.skill_init={craft_md_path}",
            f"env.split_dir={split_dir}",
        ],
    )

    if not result["ok"]:
        reason = result["reason"]
        print(f"REJECT (infrastructure): {reason}")
        append_jsonl(jsonl_path, build_jsonl_line(
            edits_proposed=0, edits_accepted=0,
            beat_before=None, beat_after=None,
            sha_before=sha_before, sha_after=sha_before, reason=reason,
        ))
        return 0

    verdict = decide_from_summary(result["summary"], Path(args.out_root))
    applied = apply_result(
        craft_md_path, old_content, verdict["accepted"], result["best_skill_content"],
    )
    reason = verdict["reason"] if applied["applied"] or not verdict["accepted"] else applied["refuse_reason"]
    edits_accepted = verdict["edits_accepted"] if applied["applied"] else 0

    if applied["applied"]:
        print(f"ACCEPT: {reason} (beat_rate {verdict['beat_rate_before']} -> {verdict['beat_rate_after']})")
        diff_dir = jsonl_path.parent / "craft-train-diffs"
        diff_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (diff_dir / f"{ts}.diff").write_text(applied["diff"], encoding="utf-8")
    else:
        print(f"REJECT: {reason}")

    append_jsonl(jsonl_path, build_jsonl_line(
        edits_proposed=verdict["edits_proposed"], edits_accepted=edits_accepted,
        beat_before=verdict["beat_rate_before"], beat_after=verdict["beat_rate_after"],
        sha_before=applied["sha_before"], sha_after=applied["sha_after"], reason=reason,
        margin_required=verdict["margin_required"], standard_error=verdict["standard_error"],
        n_comparisons_before=verdict["n_comparisons_before"], n_comparisons_after=verdict["n_comparisons_after"],
    ))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 -- a night that crashes must still exit 0
        print(f"craft_train.py: unhandled exception, treating as infrastructure "
              f"reject, CRAFT.md untouched: {exc}", file=sys.stderr)
        sys.exit(0)
