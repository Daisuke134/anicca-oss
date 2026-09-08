#!/usr/bin/env python3
"""Blind pairwise-score our title candidates against real, high-performing
writing we did not write -- the single number (beat rate) the nightly
trainer is allowed to optimise against.

Why this exists: spec 47 section 19.3. The loop currently judges itself with
its own rubric, which is a closed circle -- it can only get better at
pleasing a judge we wrote (arxiv.org/abs/2310.01798, self-correction without
an external signal degrades rather than improves). harvest_corpus.py (19-2)
gave us opponents we did not write and cannot edit. This script is the other
half: it turns (our candidates, their corpus) into beat_rate = the fraction
of blind head-to-head comparisons our headline wins against real headlines
that really performed.

Two design facts drive every choice below:
  * The judge must be blind. It sees two headlines, A and B, no metrics, no
    source, no hint which is ours -- otherwise the number measures the
    judge's bias, not our writing.
  * The judge has position bias strong enough to flip verdicts
    (arxiv.org/abs/2305.17926). Every pair is asked in BOTH orderings and
    averaged, so a judge that always prefers "A" produces beat_rate 0.5 for
    everyone (no signal), never a fake win streak for whichever side lands
    on A. The `--max-calls 5, judge-always-says-A` case in the contract test
    is that control, made explicit.

Design choices made because the spec left them open (simplest option, noted
here rather than asked about):
  * Opponent pool = top 20% by metric_primary within (lang, slice=="top"),
    with `int(len * 0.2)` truncation and a floor of 1 so a small corpus
    still yields a usable pool.
  * If the pool has fewer rows than the requested opponent count, all of
    the pool is sampled (not an error) -- there is nothing better to do
    with a thin corpus.
  * A candidate with zero non-null pairs (every judge call for it was
    inconclusive or cut off by the budget) gets beat_rate 0.0, matching the
    JSON schema's numeric (never null) contract for that field.
  * The judge call timeout (JUDGE_TIMEOUT_S, 120s) is not specified by the
    coordinator; picked generously since LLM judge calls are slower than
    the corpus harvester's HTTP calls.
  * `report`'s summary line format is not specified; picked one line per
    lang, key=value, terse enough to paste into the nightly Telegram
    message as-is.

Testing seam (documented per the task's "your choice, document it"): every
scoring function that calls the judge accepts an explicit `judge_fn`
parameter (default: the module-level `call_judge`). Tests inject a fake by
PASSING `judge_fn=` explicitly into `score_ledger`/`score_candidate`/
`score_one_pair`, not by monkeypatching the module attribute `call_judge` --
a monkeypatched module global would not retroactively change an
already-bound default parameter value (a classic Python gotcha), so explicit
injection is both the simpler and the correct seam. No live network in
tests; see tests/beat-rate-contract.sh.

Judge process boundary: exactly `scripts/editorial-gate.sh` and
`self_improve_control.py`'s `model_judge` -- `$ARTICLE_MODEL_RUNNER judge
--prompt-file -`, defaulting to the repository-owned Writer runtime model
runner. No second model path is invented here; the broker
discovery inside that runner is what keeps judging alive outside the agent
sandbox.

CORPUS INPUT: skills/writer-agent/state/writing-corpus/*.jsonl, the
harvest_corpus.py (19-2) schema -- rows carry `lang`, `slice`
("top"|"baseline"), and `metric_primary` (the raw number norm_score was
computed from).

Output: <run-dir>/gates/beat-rate-<lang>.json, schema fixed by the
coordinator's spec (see score_ledger's docstring for the exact keys).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Callable

DEFAULT_OPPONENTS = 5
DEFAULT_MAX_CALLS = 120
OPPONENT_POOL_FRACTION = 0.2
JUDGE_TIMEOUT_S = 120

# Sources whose rows are NOT authored article headlines, and so cannot serve
# as a "which headline would a stranger click" opponent for OUR headlines.
# Round-4/T15 follow-up: hn's top slice mixes real writing-craft pieces with
# breaking news and product announcements ("Judge approves $1.5B Anthropic
# settlement for pirated books", "DeepSeek makes the V4 Pro price discount
# permanent") -- their score came from the EVENT, not from any craft in the
# headline. An essay headline cannot beat breaking news on a blind
# which-would-you-click test, so a loss against one of these measures
# nothing about the essay; it is the same contamination class as the
# Stephen Hawking row harvest_corpus.py already had to fix for its OWN top
# slice, just not catchable by a title-length filter since these are full,
# well-formed sentences. hn stays in the corpus (honest data, useful for
# other questions) -- it is excluded from OPPONENT SELECTION specifically.
EXCLUDED_OPPONENT_SOURCES = {"hn"}


class BeatRateError(ValueError):
    """The scoring run cannot proceed at all -- a genuine contract
    violation (bad ledger, no eligible opponents). Distinct from a single
    judge call failing, which becomes an `inconclusive` pair, never this."""


# ---------------------------------------------------------------------------
# Small pure functions -- these are what the contract test drives directly,
# with an injected fake `judge_fn` standing in for the real judge process.
# ---------------------------------------------------------------------------

def select_opponent_pool(corpus_rows: list[dict], lang: str) -> list[dict]:
    """The deterministic candidate pool a run samples opponents from: same
    `lang`, `slice == "top"` (real winners -- the corpus's own baseline/loser
    rows are not eligible to stand in as opponents), `metric_primary > 1`
    (excludes the near-zero noise floor), and `source` not in
    EXCLUDED_OPPONENT_SOURCES (see that constant's comment -- those rows are
    real, honest corpus data but the wrong reference class for a
    headline-vs-headline judge call). Ranked by metric_primary descending
    with `id` as a tie-break so equal scores never depend on filesystem read
    order, then the top 20% is kept."""
    eligible = [
        row for row in corpus_rows
        if row.get("lang") == lang
        and row.get("slice") == "top"
        and row.get("metric_primary", 0) > 1
        and row.get("source") not in EXCLUDED_OPPONENT_SOURCES
    ]
    if not eligible:
        return []
    eligible.sort(key=lambda r: (-r.get("metric_primary", 0), str(r.get("id", ""))))
    pool_size = max(1, int(len(eligible) * OPPONENT_POOL_FRACTION))
    return eligible[:pool_size]


def sample_opponents(pool: list[dict], run_id: str, lang: str, k: int) -> list[dict]:
    """Deterministic sample: seeded by f"{run_id}:{lang}" so the same run
    and language always pick the same opponents -- a before/after comparison
    means nothing otherwise. Samples min(k, len(pool)) without replacement;
    a thin pool is not an error, it just yields fewer opponents."""
    rng = random.Random(f"{run_id}:{lang}")
    n = min(k, len(pool))
    return rng.sample(pool, n)


def build_judge_prompt(title_a: str, title_b: str) -> str:
    """Blind pairwise prompt: no metrics, no source, no hint which side is
    ours, one question, one required JSON shape."""
    return (
        "You are shown two headlines, A and B, with no other context.\n"
        "Imagine a stranger scrolling a feed of unrelated posts. Which "
        "headline would make them stop and click?\n\n"
        f"A: {title_a}\n"
        f"B: {title_b}\n\n"
        "Answer that one question only. Return EXACTLY one JSON object and "
        "nothing else, no scores, no rubric, no axes:\n"
        '{"winner":"A"|"B","why":"<one clause>"}'
    )


def _extract_last_json_object(text: str) -> dict | None:
    """Best-effort last-JSON-object extraction (same approach as
    self_improve_control.py's _extract_json), but returns None on total
    failure instead of raising -- here a parse failure is the
    "inconclusive" signal, not an exception to propagate."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    found: dict | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found = value
    return found


def parse_judge_reply(raw: str | None) -> str | None:
    """"A" or "B" from a judge reply, or None if unparseable/empty. None
    means inconclusive -- callers must never treat it as a loss."""
    obj = _extract_last_json_object(raw or "")
    if not obj:
        return None
    winner = obj.get("winner")
    return winner if winner in ("A", "B") else None


class CallBudget:
    """Enforces --max-calls across an entire scoring run. Never makes a call
    once the budget is spent -- "never blow past the budget silently" -- and
    remembers whether it ever had to skip a call it was asked to make, which
    is exactly what `partial` reports."""

    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.used = 0
        self.hit_cap = False

    def try_call(
        self,
        judge_fn: Callable[[Path, str, str], str],
        runner: Path,
        prompt: str,
        run_id: str,
    ) -> str | None:
        if self.used >= self.max_calls:
            self.hit_cap = True
            return None
        self.used += 1
        return judge_fn(runner, prompt, run_id)


def score_one_pair(
    candidate_title: str,
    opponent_title: str,
    *,
    runner: Path,
    run_id: str,
    budget: CallBudget,
    judge_fn: Callable[[Path, str, str], str],
) -> float | None:
    """One opponent, both orderings (the position-bias control). wins both
    = 1.0, splits = 0.5, loses both = 0.0. Either ordering being
    unparseable, or the budget running out before both orderings complete,
    makes the pair inconclusive (None) -- never a fake loss."""
    reply_ab = budget.try_call(judge_fn, runner, build_judge_prompt(candidate_title, opponent_title), run_id)
    winner_ab = parse_judge_reply(reply_ab)

    reply_ba = budget.try_call(judge_fn, runner, build_judge_prompt(opponent_title, candidate_title), run_id)
    winner_ba = parse_judge_reply(reply_ba)

    if winner_ab is None or winner_ba is None:
        return None
    wins = (1 if winner_ab == "A" else 0) + (1 if winner_ba == "B" else 0)
    return wins / 2.0


def score_candidate(
    title: str,
    role: str,
    cited_rule: str | None,
    opponents: list[dict],
    *,
    runner: Path,
    run_id: str,
    budget: CallBudget,
    judge_fn: Callable[[Path, str, str], str],
) -> dict:
    """One candidate (chosen or rejected) against every sampled opponent.
    beat_rate is the mean of the NON-NULL pair scores -- inconclusive pairs
    are excluded from the mean, never averaged in as a loss. Zero non-null
    pairs (all inconclusive / all cut off by budget) -> beat_rate 0.0.

    Each pair and the candidate as a whole report `opponent_source`/
    `opponent_sources` -- which corpus source(s) the opponents actually came
    from -- so what class of writing a beat_rate was measured against can
    never again be invisible (round-4/T15 follow-up)."""
    pairs = []
    for opponent in opponents:
        score = score_one_pair(
            title, opponent["title"],
            runner=runner, run_id=run_id, budget=budget, judge_fn=judge_fn,
        )
        pairs.append({
            "opponent_id": opponent["id"],
            "opponent_source": opponent.get("source"),
            "score": score,
        })
    non_null = [p["score"] for p in pairs if p["score"] is not None]
    beat_rate = (sum(non_null) / len(non_null)) if non_null else 0.0
    opponent_sources = sorted({p["opponent_source"] for p in pairs if p["opponent_source"]})
    return {
        "title": title, "role": role, "cited_rule": cited_rule, "beat_rate": beat_rate,
        "pairs": pairs, "opponent_sources": opponent_sources,
    }


def load_ledger(path: Path) -> dict:
    """The title-candidates-<lang>.json ledger written by
    title_candidates.py. Its own contract (>=5 candidates, cited rejection
    reasons) was already enforced when it was written; this only checks
    what beat_rate.py itself needs to proceed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BeatRateError(f"cannot read candidates ledger {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BeatRateError(f"{path}: ledger must be a JSON object")
    chosen = data.get("chosen")
    if not isinstance(chosen, dict) or not str(chosen.get("title", "")).strip():
        raise BeatRateError(f"{path}: chosen.title is required")
    if not isinstance(data.get("rejected"), list):
        raise BeatRateError(f"{path}: rejected must be a list")
    if not str(data.get("lang", "")).strip():
        raise BeatRateError(f"{path}: lang is required")
    return data


def score_ledger(
    ledger: dict,
    corpus_rows: list[dict],
    *,
    run_id: str,
    runner: Path,
    opponents_k: int = DEFAULT_OPPONENTS,
    max_calls: int = DEFAULT_MAX_CALLS,
    judge_fn: Callable[[Path, str, str], str] | None = None,
) -> dict:
    """The full scoring run. Output schema (coordinator's spec, extended
    round-4/T15 with `opponent_sources` so the reference class a beat_rate
    was measured against can never again be invisible):

    {"run_id":..., "lang":..., "opponent_ids":[...], "opponent_sources":[...],
     "calls_used":N, "partial":false,
     "candidates":[{"title":..., "role":"chosen"|"rejected",
     "cited_rule":...|null, "beat_rate":0.0, "opponent_sources":[...],
     "pairs":[{"opponent_id":..., "opponent_source":..., "score":1.0|0.5|0.0|null}]}],
     "chosen_beat_rate":0.0, "best_rejected_beat_rate":0.0,
     "selection_inverted":true|false}

    selection_inverted is true when ANY rejected candidate scored strictly
    higher than the chosen one -- the alarm this whole design exists to
    raise: the selection rule that killed a stronger candidate is wrong,
    and its cited_rule says which line to blame.

    A pool that comes back empty after EXCLUDED_OPPONENT_SOURCES filtering
    (e.g. a language whose only eligible rows are all hn) is a CLEAR
    refusal (BeatRateError) here, not a silent fall-through to scoring
    against zero opponents -- that would produce beat_rate 0.0 for every
    candidate, indistinguishable from "lost every pair", which is exactly
    the invisible-wrong-yardstick failure this whole fix exists to end.
    """
    resolved_judge_fn = judge_fn if judge_fn is not None else call_judge
    lang = ledger["lang"]
    pool = select_opponent_pool(corpus_rows, lang)
    if not pool:
        raise BeatRateError(
            f"no corpus opponents available for lang={lang!r} (need rows with "
            f"slice=='top', metric_primary>1, and source not in "
            f"{sorted(EXCLUDED_OPPONENT_SOURCES)!r} -- news/announcement "
            "sources are excluded from opponent selection, see "
            "EXCLUDED_OPPONENT_SOURCES)"
        )
    opponents = sample_opponents(pool, run_id, lang, opponents_k)

    budget = CallBudget(max_calls)
    candidates_out = [
        score_candidate(
            ledger["chosen"]["title"], "chosen", None, opponents,
            runner=runner, run_id=run_id, budget=budget, judge_fn=resolved_judge_fn,
        )
    ]
    for entry in ledger.get("rejected", []):
        candidates_out.append(
            score_candidate(
                entry["title"], "rejected", entry.get("cited_rule"), opponents,
                runner=runner, run_id=run_id, budget=budget, judge_fn=resolved_judge_fn,
            )
        )

    def _scorable(candidate: dict) -> int:
        return sum(1 for pair in candidate.get("pairs", []) if pair.get("score") is not None)

    for candidate in candidates_out:
        candidate["scorable_pairs"] = _scorable(candidate)

    chosen_beat_rate = candidates_out[0]["beat_rate"]
    rejected_rates = [c["beat_rate"] for c in candidates_out[1:]]
    best_rejected_beat_rate = max(rejected_rates) if rejected_rates else 0.0
    # A candidate whose pairs ALL came back inconclusive reports 0.0, which is
    # indistinguishable from losing every comparison. If that candidate is the
    # chosen one, every rejected candidate that did get scored looks stronger,
    # selection_inverted fires, and rule_blame charges a loss to the line that
    # candidate cited -- a rule convicted by a judge outage rather than by a
    # reader. Losses accumulate into deletion candidates (section 19.1), so the
    # alarm is withheld rather than raised on no evidence: null, not false,
    # because "we could not tell" is not "the selection was sound".
    if candidates_out[0]["scorable_pairs"] == 0:
        selection_inverted = None
    else:
        selection_inverted = any(rate > chosen_beat_rate for rate in rejected_rates)

    return {
        "run_id": run_id,
        "lang": lang,
        "opponent_ids": [o["id"] for o in opponents],
        "opponent_sources": sorted({o.get("source") for o in opponents if o.get("source")}),
        "calls_used": budget.used,
        "partial": budget.hit_cap,
        "candidates": candidates_out,
        "chosen_beat_rate": chosen_beat_rate,
        "best_rejected_beat_rate": best_rejected_beat_rate,
        "selection_inverted": selection_inverted,
    }


def format_report_line(lang: str, data: dict) -> str:
    """The nightly Telegram summary line for one lang's beat-rate result.
    Carries opponent_sources (round-4/T15) so which reference class a
    beat_rate was measured against is visible without opening the JSON."""
    sources = ",".join(data.get("opponent_sources", [])) or "?"
    return (
        f"beat-rate {lang}: chosen={data['chosen_beat_rate']:.2f} "
        f"best_rejected={data['best_rejected_beat_rate']:.2f} "
        f"inverted={'yes' if data['selection_inverted'] else 'no'} "
        f"calls={data['calls_used']}/{DEFAULT_MAX_CALLS} "
        f"partial={'yes' if data['partial'] else 'no'} "
        f"opponents={sources}"
    )


def load_corpus_rows(corpus_dir: Path) -> list[dict]:
    """Every row from every *.jsonl file under the writing-corpus directory.
    Unparseable lines are skipped silently -- harvest_corpus.py already
    guarantees well-formed rows, this is defensive, not a contract this
    script enforces."""
    rows: list[dict] = []
    if not corpus_dir.exists():
        return rows
    for path in sorted(corpus_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# ---------------------------------------------------------------------------
# Network/process (impure) -- everything above this line is what the
# contract test drives with an injected judge_fn, no subprocess involved.
# ---------------------------------------------------------------------------

def call_judge(runner: Path, prompt: str, run_id: str) -> str:
    """The real judge call: $ARTICLE_MODEL_RUNNER judge --prompt-file -,
    exactly as editorial-gate.sh and self_improve_control.py's model_judge
    invoke it -- one model process boundary, no second path.

    Any infrastructure failure (missing binary, timeout, nonzero exit)
    returns "" rather than raising, so it flows into parse_judge_reply as
    unparseable -> inconclusive, and a single bad call never crashes the
    whole scoring run ("never let infrastructure failure look like a
    quality verdict" applies to run survival too, not just to the score)."""
    environment = {**os.environ, "ARTICLE_RUN_ID": run_id}
    try:
        result = subprocess.run(
            [str(runner), "judge", "--prompt-file", "-"],
            input=prompt,
            capture_output=True,
            text=True,
            env=environment,
            timeout=JUDGE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_corpus_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "state" / "writing-corpus"


def _default_runner() -> Path:
    return Path(os.environ.get(
        "ARTICLE_MODEL_RUNNER",
        str(Path(__file__).resolve().parent.parent / "runtime" / "model-runner.sh"),
    ))


def cmd_score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    run_id = run_dir.resolve().name
    corpus_dir = Path(args.corpus_dir) if args.corpus_dir else _default_corpus_dir()
    try:
        ledger = load_ledger(Path(args.candidates))
        corpus_rows = load_corpus_rows(corpus_dir)
        result = score_ledger(
            ledger, corpus_rows,
            run_id=run_id, runner=_default_runner(),
            opponents_k=args.opponents, max_calls=args.max_calls,
        )
    except BeatRateError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 3
    gates_dir = run_dir / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    out_path = gates_dir / f"beat-rate-{result['lang']}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True, "path": str(out_path),
        "calls_used": result["calls_used"], "partial": result["partial"],
        "chosen_beat_rate": result["chosen_beat_rate"],
        "selection_inverted": result["selection_inverted"],
    }))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    gates_dir = Path(args.run_dir) / "gates"
    files = sorted(gates_dir.glob("beat-rate-*.json")) if gates_dir.exists() else []
    if not files:
        print(f"no beat-rate results under {gates_dir}")
        return 0
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(format_report_line(data.get("lang", "?"), data))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_score = sub.add_parser("score", help="blind pairwise-score a title ledger against corpus opponents")
    p_score.add_argument("--candidates", required=True, help="title-candidates-<lang>.json ledger")
    p_score.add_argument("--run-dir", required=True)
    p_score.add_argument("--corpus-dir", default=None)
    p_score.add_argument("--opponents", type=int, default=DEFAULT_OPPONENTS)
    p_score.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)

    p_report = sub.add_parser("report", help="print the nightly summary line(s)")
    p_report.add_argument("--run-dir", required=True)

    args = parser.parse_args(argv)
    if args.command == "score":
        return cmd_score(args)
    if args.command == "report":
        return cmd_report(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
