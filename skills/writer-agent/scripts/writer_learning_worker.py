#!/usr/bin/env python3
"""Run frozen Writer replay before exposing one candidate to a matched canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from writer_learning_experiment import ExperimentStore  # noqa: E402


SAFE_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,79}")


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(_canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_output(path: Path, value: str) -> str:
    encoded = value.encode("utf-8")
    digest = _sha_bytes(encoded)
    destination = path / f"{digest}.md"
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise ValueError("content-addressed output conflicts")
        return digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return digest


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"required JSON is unavailable: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"required JSON is not an object: {path}")
    return value


def _state_root(skill_dir: Path) -> Path:
    return Path(os.environ.get("ARTICLE_STATE_DIR", str(skill_dir / "state")))


def _latest_case_run(skill_dir: Path) -> tuple[Path, dict[str, Any]]:
    state_dir = _state_root(skill_dir)
    candidates = []
    for run_dir in (state_dir / "runs").iterdir():
        route = run_dir / "gates/topic-route-input.json"
        state_path = run_dir / "gates/publication-state.json"
        if (
            run_dir.is_dir()
            and (run_dir / "article-ja.md").is_file()
            and (run_dir / "article-en.md").is_file()
            and route.is_file()
            and state_path.is_file()
        ):
            try:
                state = _json(state_path)
                resolved_run = run_dir.resolve(strict=True)
                frozen = (
                    state.get("run_id") == run_dir.name
                    and Path(str(state.get("run_dir", ""))).resolve(strict=True)
                    == resolved_run
                    and set(state.get("drafts", {})) == {"ja", "en"}
                )
                for language in ("ja", "en"):
                    article = run_dir / f"article-{language}.md"
                    entry = state.get("drafts", {}).get(language, {})
                    frozen = frozen and (
                        Path(str(entry.get("path", ""))).resolve(strict=True)
                        == article.resolve(strict=True)
                        and entry.get("sha256") == _sha_bytes(article.read_bytes())
                    )
            except (OSError, ValueError):
                frozen = False
            if frozen:
                candidates.append(
                    (state_path.stat().st_mtime_ns, run_dir.name, run_dir, route)
                )
    if not candidates:
        raise ValueError("no bilingual frozen run with reader contract is available")
    _mtime, _name, run_dir, route = max(
        candidates, key=lambda item: (item[0], item[1])
    )
    value = _json(route)
    reader = value.get("reader")
    if (
        not isinstance(reader, dict)
        or not isinstance(reader.get("job"), str)
        or not reader["job"].strip()
    ):
        raise ValueError("latest run has no durable reader job")
    return run_dir, value


def _validate_proposal(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "changed_field", "before", "after", "hypothesis"
    }:
        raise ValueError("proposal must describe exactly one writing change")
    result = {}
    for key, raw in value.items():
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"proposal {key} is empty")
        result[key] = raw.strip()
    if SAFE_FIELD.fullmatch(result["changed_field"]) is None:
        raise ValueError("proposal changed_field is invalid")
    if result["before"] == result["after"]:
        raise ValueError("proposal does not change the writing strategy")
    return result


def _extract_json(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    found = []
    index = 0
    while index < len(output):
        index = output.find("{", index)
        if index < 0:
            break
        try:
            value, end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        # A successful outer object owns every nested object in its byte
        # range. Skip that range so the final nested dictionary cannot replace
        # the complete model receipt.
        index += max(end, 1)
    if not found:
        raise ValueError("model returned no JSON object")
    return found[-1]


def _model_call(skill_dir: Path, mode: str, prompt: str, run_id: str) -> str:
    runner = Path(os.environ.get("ARTICLE_MODEL_RUNNER", skill_dir / "runtime/model-runner.sh"))
    bounded = skill_dir.parents[1] / "runtime/loop/bounded-exec.py"
    completed = subprocess.run(
        [sys.executable, str(bounded), "900", str(runner), mode, "--prompt-file", "-"],
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "ARTICLE_RUN_ID": run_id,
            "ARTICLE_MODEL_REASONING_EFFORT": os.environ.get(
                "ARTICLE_LEARNING_REASONING_EFFORT", "medium"
            ),
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(f"model {mode} failed with rc={completed.returncode}")
    return completed.stdout


def _real_proposer(skill_dir: Path, context: dict[str, Any], cycle_key: str) -> dict[str, Any]:
    prompt = (
        "You are the Writer Agent's experiment designer. Based only on the frozen reader job, "
        "current playbook, and held-out articles below, propose one writing change. Do not change "
        "safety, citations, identity, publishing, price, platform, credentials, schedules, or "
        "measurement. Return only JSON with exactly changed_field, before, after, hypothesis. "
        "changed_field is a concise machine-safe name; before and after are human-readable rules.\n"
        + json.dumps(context, ensure_ascii=False, sort_keys=True)
    )
    return _extract_json(_model_call(skill_dir, "judge", prompt, f"{cycle_key}-proposal"))


def _real_rewriter(
    skill_dir: Path, case: dict[str, Any], rule: str, trial: int, cycle_key: str
) -> str:
    prompt = (
        "Rewrite this frozen held-out article while changing only the experimental writing rule. "
        "Preserve factual claims, citations, links, CTA, language, and media contracts. Return only "
        "the complete rewritten Markdown, with no commentary.\n"
        f"Experimental rule: {rule}\nLanguage: {case['language']}\nArticle:\n{case['source']}"
    )
    return _model_call(
        skill_dir, "agent", prompt, f"{cycle_key}-{case['case_id']}-rewrite-{trial}"
    ).strip() + "\n"


def _real_evaluator(
    skill_dir: Path,
    case: dict[str, Any],
    baseline: str,
    candidate: str,
    trial: int,
    order: str,
    cycle_key: str,
) -> dict[str, Any]:
    first, second = (candidate, baseline) if order == "candidate-first" else (baseline, candidate)
    prompt = (
        "Evaluate two articles for the frozen reader job. Score reader_job and citation_support "
        "from 0 to 1, and independently report safety/citation pass booleans. The display order is "
        f"{order}; do not reward position. Return only JSON with exactly scores and guardrails, "
        "where every score has baseline and candidate and guardrails has baseline/candidate each "
        "with safety and citation booleans.\n"
        f"Reader job: {case['reader_job']}\nFIRST:\n{first}\nSECOND:\n{second}"
    )
    return _extract_json(
        _model_call(skill_dir, "judge", prompt, f"{cycle_key}-{case['case_id']}-eval-{trial}")
    )


def run_offline_cycle(
    *,
    skill_dir: Path,
    now: datetime,
    proposer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    rewriter: Callable[[dict[str, Any], str, int], str] | None = None,
    evaluator: Callable[[dict[str, Any], str, str, int, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    skill_dir = Path(skill_dir)
    state_dir = _state_root(skill_dir)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("cycle time must include timezone")
    cycle_key = f"learning-{now.astimezone().date().isoformat()}"
    receipt_path = state_dir / "learning/offline-receipts" / f"{cycle_key}.json"
    if receipt_path.exists():
        return _json(receipt_path)
    config = _json(skill_dir / "config/writer-learning.json")
    if set(config) != {"schema_version", "trials_per_case", "canary"} or config.get("schema_version") != 1:
        raise ValueError("writer learning config is malformed")
    if not isinstance(config.get("trials_per_case"), int) or config["trials_per_case"] < 3:
        raise ValueError("writer learning requires at least three trials")
    experiment_dir = state_dir / "learning/experiments" / cycle_key
    manifest_path = experiment_dir / "manifest.json"
    proposal_path = experiment_dir / "proposal.json"
    manifest = _json(manifest_path) if manifest_path.exists() else None
    if manifest is not None and not proposal_path.exists():
        case_ids = [
            str(item.get("case_id", ""))
            for item in manifest.get("held_out_cases", [])
            if isinstance(item, dict)
        ]
        run_ids = {
            case_id[:-3]
            for case_id in case_ids
            if case_id.endswith(("-ja", "-en"))
        }
        if len(run_ids) != 1:
            raise ValueError("interrupted experiment has no recoverable baseline run")
        _atomic_json(
            proposal_path,
            {
                "schema_version": 2,
                "experiment_id": cycle_key,
                "baseline_run_id": next(iter(run_ids)),
                "reader_job": manifest.get("canary_contract", {}).get("reader_job"),
                "changed_field": manifest.get("changed_field"),
                "before": manifest.get("text_diff", {}).get("before"),
                "after": manifest.get("text_diff", {}).get("after"),
                "hypothesis": (
                    "Original provider hypothesis was not durably journaled before interruption."
                ),
                "recovered_after_interruption": True,
            },
        )
    proposal_record = _json(proposal_path) if proposal_path.exists() else None
    if proposal_record is not None:
        run_id = proposal_record.get("baseline_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("proposal receipt has no baseline run")
        run_dir = state_dir / "runs" / run_id
        route = _json(run_dir / "gates/topic-route-input.json")
        reader_job = str(proposal_record.get("reader_job", "")).strip()
        if route.get("reader", {}).get("job") != reader_job:
            raise ValueError("proposal reader job differs from its frozen baseline")
        proposal = _validate_proposal(
            {
                key: proposal_record.get(key)
                for key in ("changed_field", "before", "after", "hypothesis")
            }
        )
    else:
        run_dir, route = _latest_case_run(skill_dir)
        reader_job = route["reader"]["job"].strip()
        playbook_for_prompt = (
            skill_dir / "reference/learned-playbook.md"
        ).read_text(encoding="utf-8")
        prompt_cases = []
        for language in ("ja", "en"):
            prompt_cases.append(
                {
                    "case_id": f"{run_dir.name}-{language}",
                    "language": language,
                    "article": (run_dir / f"article-{language}.md").read_text(
                        encoding="utf-8"
                    ),
                }
            )
        context = {
            "reader": route["reader"],
            "topic_id": route.get("topic_id"),
            "playbook": playbook_for_prompt,
            "held_out": prompt_cases,
        }
        proposal_call = proposer or (
            lambda value: _real_proposer(skill_dir, value, cycle_key)
        )
        proposal = _validate_proposal(proposal_call(context))
        proposal_record = {
            "schema_version": 2,
            "experiment_id": cycle_key,
            "baseline_run_id": run_dir.name,
            "reader_job": reader_job,
            **proposal,
            "recovered_after_interruption": False,
        }
        _atomic_json(proposal_path, proposal_record)
    playbook = (skill_dir / "reference/learned-playbook.md").read_text(encoding="utf-8")
    cases = []
    for language in ("ja", "en"):
        source = (run_dir / f"article-{language}.md").read_text(encoding="utf-8")
        cases.append({
            "case_id": f"{run_dir.name}-{language}",
            "language": language,
            "input_sha256": _sha_bytes(source.encode("utf-8")),
            "source": source,
            "reader_job": reader_job,
        })
    if manifest is not None:
        frozen_cases = {
            item["case_id"]: item["input_sha256"]
            for item in manifest.get("held_out_cases", [])
            if isinstance(item, dict)
        }
        if frozen_cases != {
            case["case_id"]: case["input_sha256"] for case in cases
        }:
            raise ValueError("interrupted experiment baseline artifacts changed")
    rewrite_call = rewriter or (
        lambda case, rule, trial: _real_rewriter(skill_dir, case, rule, trial, cycle_key)
    )
    evaluate_call = evaluator or (
        lambda case, baseline, candidate, trial, order: _real_evaluator(
            skill_dir, case, baseline, candidate, trial, order, cycle_key
        )
    )
    canary = config.get("canary")
    if not isinstance(canary, dict) or set(canary) != {"platform", "price", "currency", "window_hours"}:
        raise ValueError("writer learning canary config is malformed")
    canary_contract = {**canary, "reader_job": reader_job}
    active_path = state_dir / "learning/active-strategy.json"
    if active_path.is_file():
        active = _json(active_path)
        active_hash = str(active.get("strategy_sha256", ""))
        active_strategy_path = (
            state_dir / "learning/strategies" / f"{active_hash}.json"
        )
        if (
            re.fullmatch(r"[0-9a-f]{64}", active_hash) is None
            or not active_strategy_path.is_file()
            or _sha_bytes(active_strategy_path.read_bytes()) != active_hash
        ):
            raise ValueError("active Writer strategy hash drift")
        baseline_strategy = _json(active_strategy_path)
        existing_before = baseline_strategy.get(proposal["changed_field"])
        if existing_before is not None and existing_before != proposal["before"]:
            raise ValueError("proposal before differs from the active strategy")
        baseline_strategy[proposal["changed_field"]] = proposal["before"]
    else:
        baseline_strategy = {
            "playbook": playbook,
            proposal["changed_field"]: proposal["before"],
        }
    candidate_strategy = dict(baseline_strategy)
    candidate_strategy[proposal["changed_field"]] = proposal["after"]
    store = ExperimentStore(state_dir / "learning")
    manifest = store.create(
        experiment_id=cycle_key,
        baseline=baseline_strategy,
        candidate=candidate_strategy,
        changed_field=proposal["changed_field"],
        held_out_cases=[
            {key: case[key] for key in ("case_id", "language", "input_sha256")}
            for case in cases
        ],
        replay_contract={
            "model_provider": "shared-agent-runner",
            "model_version": os.environ.get("ARTICLE_MODEL_VERSION", "provider-current"),
            "evaluator_version": "writer-pairwise-v2",
            "trials_per_case": config["trials_per_case"],
        },
        canary_contract=canary_contract,
        created_at=(
            datetime.fromisoformat(str(manifest["created_at"]))
            if manifest is not None
            else now
        ),
    )
    outputs = state_dir / "learning/experiments" / cycle_key / "outputs"
    for case in cases:
        baseline_hash = _write_output(outputs, case["source"])
        candidate_receipt_path = (
            experiment_dir / "candidates" / f"{case['case_id']}.json"
        )
        if candidate_receipt_path.exists():
            candidate_receipt = _json(candidate_receipt_path)
            candidate_hash = str(
                candidate_receipt.get("candidate_output_sha256", "")
            )
            candidate_path = outputs / f"{candidate_hash}.md"
            if not candidate_path.is_file():
                raise ValueError("prepared candidate output is unavailable")
            candidate = candidate_path.read_text(encoding="utf-8")
        else:
            legacy_attempts = sorted(
                (experiment_dir / "attempts").glob(f"{case['case_id']}-*.json")
            )
            if legacy_attempts:
                legacy = _json(legacy_attempts[0])
                candidate_hash = str(
                    legacy.get("candidate_output_sha256", "")
                )
                candidate_path = outputs / f"{candidate_hash}.md"
                if not candidate_path.is_file():
                    raise ValueError("interrupted candidate output is unavailable")
                candidate = candidate_path.read_text(encoding="utf-8")
            else:
                candidate = rewrite_call(case, proposal["after"], 1)
                if not isinstance(candidate, str) or not candidate.strip():
                    raise ValueError("candidate rewrite is empty")
                candidate_hash = _write_output(outputs, candidate)
            _atomic_json(
                candidate_receipt_path,
                {
                    "schema_version": 2,
                    "case_id": case["case_id"],
                    "candidate_output_sha256": candidate_hash,
                },
            )
        for trial in range(1, config["trials_per_case"] + 1):
            replay_path = experiment_dir / "replays" / f"{case['case_id']}-{trial}.json"
            if replay_path.exists():
                continue
            attempt_path = (
                experiment_dir / "pair-attempts" / f"{case['case_id']}-{trial}.json"
            )
            if attempt_path.exists():
                attempt = _json(attempt_path)
                if attempt.get("candidate_output_sha256") != candidate_hash:
                    raise ValueError("pairwise attempt changed the frozen candidate")
                order = str(attempt.get("randomized_order", ""))
                if order not in {
                    "baseline-first", "candidate-first"
                }:
                    raise ValueError("prepared replay attempt is incomplete")
            else:
                seed = hashlib.sha256(
                    f"{cycle_key}:{case['case_id']}:{trial}".encode()
                ).digest()[0]
                order = "candidate-first" if seed % 2 else "baseline-first"
                _atomic_json(
                    attempt_path,
                    {
                        "schema_version": 2,
                        "case_id": case["case_id"],
                        "trial": trial,
                        "candidate_output_sha256": candidate_hash,
                        "randomized_order": order,
                    },
                )
            evaluation = evaluate_call(case, case["source"], candidate, trial, order)
            if not isinstance(evaluation, dict) or set(evaluation) != {"scores", "guardrails"}:
                raise ValueError("pairwise evaluator receipt is malformed")
            store.record_replay(
                cycle_key,
                {
                    "case_id": case["case_id"],
                    "trial": trial,
                    "randomized_order": order,
                    "baseline_output_sha256": baseline_hash,
                    "candidate_output_sha256": candidate_hash,
                    "scores": evaluation["scores"],
                    "guardrails": evaluation["guardrails"],
                    "evaluator_version": "writer-pairwise-v2",
                },
            )
    assignment = {
        "schema_version": 2,
        "status": "READY",
        "experiment_id": cycle_key,
        "candidate_strategy_sha256": manifest["candidate_strategy_sha256"],
        "baseline_run_id": run_dir.name,
        "reader_job": reader_job,
        "changed_field": proposal["changed_field"],
        "before": proposal["before"],
        "after": proposal["after"],
        "hypothesis": proposal["hypothesis"],
        "canary_contract": canary_contract,
    }
    assignment_path = state_dir / "learning/canary-assignment.json"
    if assignment_path.exists() and _json(assignment_path) != assignment:
        previous = _json(assignment_path)
        previous_id = str(previous.get("experiment_id", ""))
        previous_dir = state_dir / "learning/experiments" / previous_id
        decision_path = previous_dir / "decision.json"
        if not decision_path.is_file():
            raise ValueError("another canary assignment is already active")
        previous_decision = _json(decision_path).get("decision")
        completed = previous_decision in {"REVERT", "INCONCLUSIVE"} or (
            previous_decision == "KEEP"
            and any((previous_dir / "consumptions").glob("*.json"))
        )
        if not completed:
            raise ValueError("another canary assignment is already active")
        archived = previous_dir / "assignment-final.json"
        if archived.is_file() and _json(archived) != previous:
            raise ValueError("completed assignment archive conflicts")
        if not archived.is_file():
            _atomic_json(archived, previous)
        _atomic_json(assignment_path, assignment)
    elif not assignment_path.exists():
        _atomic_json(assignment_path, assignment)
    result = {
        "schema_version": 2,
        "status": "AWAITING_MATCHED_CANARY",
        "experiment_id": cycle_key,
        "replay_receipts": len(cases) * config["trials_per_case"],
        "baseline_run_id": run_dir.name,
        "candidate_strategy_sha256": manifest["candidate_strategy_sha256"],
        "assignment_path": str(assignment_path),
    }
    _atomic_json(receipt_path, result)
    return result


def current_assignment(skill_dir: Path) -> dict[str, Any]:
    skill_dir = Path(skill_dir)
    state_dir = _state_root(skill_dir)
    assignment_path = state_dir / "learning/canary-assignment.json"
    if not assignment_path.is_file():
        return {"status": "NONE"}
    assignment = _json(assignment_path)
    if assignment.get("status") != "READY":
        return {"status": "NONE"}
    strategy_hash = assignment.get("candidate_strategy_sha256")
    if not isinstance(strategy_hash, str) or re.fullmatch(r"[0-9a-f]{64}", strategy_hash) is None:
        raise ValueError("canary assignment has no candidate strategy hash")
    strategy_path = state_dir / "learning/strategies" / f"{strategy_hash}.json"
    if not strategy_path.is_file():
        raise ValueError("canary candidate strategy is unavailable")
    return {
        "status": "CANDIDATE_CANARY",
        "experiment_id": assignment["experiment_id"],
        "strategy_sha256": strategy_hash,
        "strategy_path": str(strategy_path),
        "reader_job": assignment["reader_job"],
        "changed_field": assignment["changed_field"],
        "rule": assignment["after"],
        "canary_contract": assignment["canary_contract"],
    }


def record_canary_application(
    skill_dir: Path, run_dir: Path, evidence_path: Path
) -> dict[str, Any]:
    skill_dir = Path(skill_dir)
    run_dir = Path(run_dir)
    state_dir = _state_root(skill_dir)
    state_runs = (state_dir / "runs").resolve()
    try:
        resolved_run = run_dir.resolve(strict=True)
    except OSError as error:
        raise ValueError("canary run directory is unavailable") from error
    if resolved_run.parent != state_runs or resolved_run.name != run_dir.name:
        raise ValueError("canary run is outside the Writer state boundary")
    assignment_path = state_dir / "learning/canary-assignment.json"
    assignment = _json(assignment_path)
    existing_run = assignment.get("candidate_run_id")
    receipt_path = (
        state_dir / "learning/experiments"
        / str(assignment.get("experiment_id", ""))
        / "canary-applications" / f"{resolved_run.name}.json"
    )
    if assignment.get("status") in {"PREPARED", "APPLIED"}:
        if existing_run != resolved_run.name or not receipt_path.is_file():
            raise ValueError("canary assignment was consumed by another run")
        return _json(receipt_path)
    if assignment.get("status") != "READY":
        raise ValueError("there is no ready canary assignment")
    route = _json(resolved_run / "gates/topic-route-input.json")
    reader = route.get("reader")
    if (
        not isinstance(reader, dict)
        or reader.get("job") != assignment.get("reader_job")
    ):
        raise ValueError("canary reader job differs from the matched contract")
    evidence = _json(Path(evidence_path))
    if set(evidence) != {"experiment_id", "excerpts"} or evidence.get(
        "experiment_id"
    ) != assignment.get("experiment_id"):
        raise ValueError("canary application evidence is malformed")
    excerpts = evidence.get("excerpts")
    if not isinstance(excerpts, dict) or set(excerpts) != {"ja", "en"}:
        raise ValueError("canary application requires JA and EN excerpts")
    artifacts: dict[str, str] = {}
    normalized_excerpts: dict[str, str] = {}
    for language in ("ja", "en"):
        excerpt = excerpts.get(language)
        article_path = resolved_run / f"article-{language}.md"
        try:
            article = article_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"canary {language} article is unavailable") from error
        if not isinstance(excerpt, str) or len(excerpt.strip()) < 12 or excerpt not in article:
            raise ValueError(f"canary {language} excerpt is not in the frozen article")
        artifacts[language] = _sha_bytes(article_path.read_bytes())
        normalized_excerpts[language] = excerpt
    receipt = {
        "schema_version": 2,
        "experiment_id": assignment["experiment_id"],
        "run_id": resolved_run.name,
        "strategy_sha256": assignment["candidate_strategy_sha256"],
        "reader_job": assignment["reader_job"],
        "artifact_sha256": artifacts,
        "excerpts": normalized_excerpts,
    }
    if receipt_path.exists():
        if _json(receipt_path) != receipt:
            raise ValueError("canary application receipt conflicts")
    else:
        _atomic_json(receipt_path, receipt)
    assignment.update({
        "status": "PREPARED",
        "candidate_run_id": resolved_run.name,
        "application_receipt": str(receipt_path),
    })
    _atomic_json(assignment_path, assignment)
    return receipt


def record_active_consumption(
    skill_dir: Path,
    run_dir: Path,
    evidence_path: Path,
    *,
    consumed_at: datetime,
) -> dict[str, Any]:
    skill_dir = Path(skill_dir)
    state_dir = _state_root(skill_dir)
    if consumed_at.tzinfo is None or consumed_at.utcoffset() is None:
        raise ValueError("strategy consumption time must include timezone")
    state_runs = (state_dir / "runs").resolve()
    try:
        resolved_run = Path(run_dir).resolve(strict=True)
    except OSError as error:
        raise ValueError("strategy consumption run is unavailable") from error
    if resolved_run.parent != state_runs:
        raise ValueError("strategy consumption run is outside Writer state")
    runtime = _json(resolved_run / "gates/strategy-consumption.json")
    if runtime.get("run_id") != resolved_run.name or runtime.get("status") != "consumed":
        raise ValueError("run did not consume an active strategy")
    versions = runtime.get("versions")
    if not isinstance(versions, list):
        raise ValueError("strategy consumption versions are malformed")
    learning_versions = [
        item
        for item in versions
        if isinstance(item, dict) and item.get("slice") == "writer-learning"
    ]
    if len(learning_versions) != 1:
        raise ValueError("run requires exactly one active Writer learning strategy")
    version = learning_versions[0]
    active = _json(state_dir / "learning/active-strategy.json")
    experiment_id = str(active.get("experiment_id", ""))
    strategy_sha256 = str(active.get("strategy_sha256", ""))
    if (
        version.get("learning_cycle_id") != experiment_id
        or version.get("active_version_id") != strategy_sha256
        or version.get("consumed_hash") != strategy_sha256
    ):
        raise ValueError("run consumed a different Writer learning strategy")
    strategy_path = (
        state_dir / "learning/strategies" / f"{strategy_sha256}.json"
    ).resolve()
    try:
        runtime_path = Path(str(version.get("weight_file", ""))).resolve(strict=True)
    except OSError as error:
        raise ValueError("consumed Writer strategy file is unavailable") from error
    if runtime_path != strategy_path or _sha_bytes(runtime_path.read_bytes()) != strategy_sha256:
        raise ValueError("consumed Writer strategy hash drift")
    manifest = _json(
        state_dir / "learning/experiments" / experiment_id / "manifest.json"
    )
    evidence = _json(Path(evidence_path))
    if set(evidence) != {
        "experiment_id", "strategy_sha256", "changed_field", "excerpts"
    } or (
        evidence.get("experiment_id") != experiment_id
        or evidence.get("strategy_sha256") != strategy_sha256
        or evidence.get("changed_field") != manifest.get("changed_field")
    ):
        raise ValueError("active strategy application evidence is malformed")
    excerpts = evidence.get("excerpts")
    if not isinstance(excerpts, dict) or set(excerpts) != {"ja", "en"}:
        raise ValueError("active strategy consumption requires JA and EN excerpts")
    artifacts: dict[str, str] = {}
    for language in ("ja", "en"):
        article_path = resolved_run / f"article-{language}.md"
        try:
            article = article_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"consumed {language} article is unavailable") from error
        excerpt = excerpts.get(language)
        if not isinstance(excerpt, str) or len(excerpt.strip()) < 12 or excerpt not in article:
            raise ValueError(f"active strategy {language} excerpt is not in the frozen article")
        artifacts[language] = _sha_bytes(article_path.read_bytes())
    return ExperimentStore(state_dir / "learning").record_consumption(
        experiment_id=experiment_id,
        run_id=resolved_run.name,
        strategy_sha256=strategy_sha256,
        artifact_sha256=artifacts,
        consumed_at=consumed_at,
    )


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("measurement timestamp has no timezone")
    return parsed


def _artifact_measurements(
    connection: sqlite3.Connection,
    *,
    artifact_id: str,
    published_at: datetime,
    cutoff: datetime,
    currency: str,
    expected_price: float,
) -> tuple[dict[str, float] | None, list[str], list[str]]:
    observations: list[tuple[datetime, sqlite3.Row]] = []
    for row in connection.execute(
        "SELECT observation_id,metric,value,unit,status,observed_at "
        "FROM metric_observations WHERE artifact_id=?",
        (artifact_id,),
    ):
        try:
            observed = _time(str(row["observed_at"]))
        except ValueError:
            continue
        if not published_at <= observed <= cutoff:
            continue
        observations.append((observed, row))
    view_times = [
        observed
        for observed, row in observations
        if row["metric"] == "views"
        and row["status"] == "verified"
        and row["unit"] == "count"
        and isinstance(row["value"], (int, float))
    ]
    anchor = max(view_times) if view_times else None
    latest: dict[str, sqlite3.Row] = {}
    for observed, row in observations:
        if anchor is not None and observed > anchor:
            continue
        previous = latest.get(str(row["metric"]))
        if (
            previous is None
            or _time(str(previous["observed_at"])) < observed
            or (
                _time(str(previous["observed_at"])) == observed
                and previous["status"] != "verified"
                and row["status"] == "verified"
            )
        ):
            latest[str(row["metric"])] = row
    expected_units = {
        "views": "count",
        "qualified_cta_clicks": "count",
        "purchases": "count",
        "refunds": currency,
        "net_received": currency,
    }
    missing = []
    values: dict[str, float] = {}
    receipts = []
    for metric, unit in expected_units.items():
        row = latest.get(metric)
        if (
            row is None
            or row["status"] != "verified"
            or row["unit"] != unit
            or not isinstance(row["value"], (int, float))
        ):
            missing.append(metric)
            continue
        if anchor is None or _time(str(row["observed_at"])) != anchor:
            missing.append(f"{metric}_alignment")
            continue
        key = f"{metric}_{unit}" if metric in {"net_received", "refunds"} else metric
        values[key] = float(row["value"])
        receipts.append(str(row["observation_id"]))
    if anchor is None:
        missing.append("measurement_age")
    else:
        values["measurement_age_seconds"] = (anchor - published_at).total_seconds()
    cost = latest.get("compute_cost")
    if (
        cost is None
        or cost["status"] != "verified"
        or not isinstance(cost["value"], (int, float))
        or not isinstance(cost["unit"], str)
        or not cost["unit"]
    ):
        missing.append("compute_cost")
    elif anchor is None or _time(str(cost["observed_at"])) != anchor:
        missing.append("compute_cost_alignment")
    else:
        values[f"compute_cost_{cost['unit']}"] = float(cost["value"])
        receipts.append(str(cost["observation_id"]))
    for metric, unit, expected in (
        ("price", currency, expected_price),
        ("paywall_active", "boolean", 1.0),
    ):
        row = latest.get(metric)
        if (
            row is None
            or row["status"] != "verified"
            or row["unit"] != unit
            or not isinstance(row["value"], (int, float))
            or (expected is not None and float(row["value"]) != expected)
        ):
            missing.append(metric)
        else:
            receipts.append(str(row["observation_id"]))
    views = values.get("views")
    clicks = values.get("qualified_cta_clicks")
    if views is not None and clicks is not None:
        if views <= 0 or clicks < 0 or clicks > views:
            missing.append("cta_click_rate")
        else:
            values["cta_click_rate"] = clicks / views
    return (values if not missing else None), sorted(set(missing)), receipts


def _non_comparable_metrics(measurements: dict[str, dict[str, Any]]) -> list[str]:
    try:
        baseline = set(measurements["baseline"]["metrics"])
        candidate = set(measurements["candidate"]["metrics"])
    except (KeyError, TypeError) as error:
        raise ValueError("canary measurements are malformed") from error
    return sorted(baseline.symmetric_difference(candidate))


def _measurement_age_mismatch(
    measurements: dict[str, dict[str, Any]], *, tolerance_seconds: float = 3900
) -> list[str]:
    try:
        baseline = float(
            measurements["baseline"]["metrics"]["measurement_age_seconds"]
        )
        candidate = float(
            measurements["candidate"]["metrics"]["measurement_age_seconds"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("canary measurement ages are malformed") from error
    return (
        ["non_comparable_measurement_age_seconds"]
        if abs(candidate - baseline) > tolerance_seconds
        else []
    )


def close_canary(
    skill_dir: Path,
    *,
    now: datetime,
    interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    skill_dir = Path(skill_dir)
    state_dir = _state_root(skill_dir)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("canary close time must include timezone")
    assignment_path = state_dir / "learning/canary-assignment.json"
    if not assignment_path.is_file():
        return {"status": "NO_APPLIED_CANARY"}
    assignment = _json(assignment_path)
    experiment_id = str(assignment.get("experiment_id", ""))
    experiment_dir = state_dir / "learning/experiments" / experiment_id
    close_path = experiment_dir / "close.json"
    if close_path.is_file():
        closed = _json(close_path)
        decision = str(closed.get("status", ""))
        if decision != "KEEP":
            return {
                "status": "CYCLE_COMPLETE",
                "experiment_id": experiment_id,
                "decision": decision,
            }
        consumptions = sorted((experiment_dir / "consumptions").glob("*.json"))
        if consumptions:
            return {
                "status": "CYCLE_COMPLETE",
                "experiment_id": experiment_id,
                "decision": "KEEP",
                "consumed_by_run_id": _json(consumptions[-1]).get("run_id"),
            }
        return {
            "status": "AWAITING_STRATEGY_CONSUMPTION",
            "experiment_id": experiment_id,
            "decision": "KEEP",
        }
    assignment_status = assignment.get("status")
    if assignment_status not in {"PREPARED", "APPLIED"}:
        return {"status": "NO_APPLIED_CANARY", "experiment_id": experiment_id}
    manifest = _json(experiment_dir / "manifest.json")
    application = _json(Path(str(assignment["application_receipt"])))
    contract = manifest.get("canary_contract", {})
    currency = str(contract.get("currency", ""))
    window_hours = contract.get("window_hours")
    if not currency or not isinstance(window_hours, int) or window_hours <= 0:
        raise ValueError("canary contract is malformed")
    database = state_dir / "money.sqlite3"
    if not database.is_file():
        if assignment_status == "PREPARED":
            return {
                "status": "AWAITING_CANARY_PUBLICATION",
                "experiment_id": experiment_id,
            }
        return {
            "status": "MEASUREMENT_INSUFFICIENT",
            "experiment_id": experiment_id,
            "missing": ["money.sqlite3"],
        }
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        artifacts = {}
        for lane, run_id, expected_hash in (
            (
                "candidate",
                assignment["candidate_run_id"],
                application["artifact_sha256"]["ja"],
            ),
            (
                "baseline",
                assignment["baseline_run_id"],
                next(
                    item["input_sha256"]
                    for item in manifest["held_out_cases"]
                    if item["language"] == "ja"
                ),
            ),
        ):
            rows = list(
                connection.execute(
                    "SELECT artifact_id,published_at,artifact_sha256 FROM money_artifacts "
                    "WHERE run_id=? AND platform=? AND lang='ja'",
                    (run_id, contract.get("platform")),
                )
            )
            if len(rows) != 1 or rows[0]["artifact_sha256"] != expected_hash:
                if lane == "candidate" and assignment_status == "PREPARED":
                    return {
                        "status": "AWAITING_CANARY_PUBLICATION",
                        "experiment_id": experiment_id,
                    }
                return {
                    "status": "MEASUREMENT_INSUFFICIENT",
                    "experiment_id": experiment_id,
                    "missing": [f"{lane}_artifact_receipt"],
                }
            artifacts[lane] = rows[0]
            if lane == "candidate" and assignment_status == "PREPARED":
                candidate_published = _time(str(rows[0]["published_at"]))
                assignment.update(
                    {
                        "status": "APPLIED",
                        "candidate_published_at": candidate_published.isoformat(),
                    }
                )
                _atomic_json(assignment_path, assignment)
                assignment_status = "APPLIED"
        candidate_published = _time(str(artifacts["candidate"]["published_at"]))
        closes_at = candidate_published + timedelta(hours=window_hours)
        if now < closes_at:
            return {
                "status": "CANARY_OPEN",
                "experiment_id": experiment_id,
                "closes_at": closes_at.isoformat(),
            }
        measurements = {}
        missing = []
        receipt_ids = []
        for lane, row in artifacts.items():
            published = _time(str(row["published_at"]))
            cutoff = published + timedelta(hours=window_hours)
            values, lane_missing, lane_receipts = _artifact_measurements(
                connection,
                artifact_id=str(row["artifact_id"]),
                published_at=published,
                cutoff=cutoff,
                currency=currency,
                expected_price=float(contract["price"]),
            )
            if lane_missing:
                missing.extend(f"{lane}:{item}" for item in lane_missing)
            if values is not None:
                measurements[lane] = {
                    "cohort_id": str(row["artifact_id"]),
                    "sample_size": int(values["views"]),
                    "metrics": values,
                }
            receipt_ids.extend(lane_receipts)
        if missing:
            return {
                "status": "MEASUREMENT_INSUFFICIENT",
                "experiment_id": experiment_id,
                "missing": sorted(set(missing)),
                "window_hours": window_hours,
            }
        non_comparable = _non_comparable_metrics(measurements)
        non_comparable.extend(_measurement_age_mismatch(measurements))
        if non_comparable:
            return {
                "status": "MEASUREMENT_INSUFFICIENT",
                "experiment_id": experiment_id,
                "missing": [
                    f"non_comparable_metric:{metric}" for metric in non_comparable
                ],
                "window_hours": window_hours,
            }
    finally:
        connection.close()
    store = ExperimentStore(state_dir / "learning")
    store.record_canary(
        experiment_id,
        {
            "closed": True,
            "contract": contract,
            "baseline": measurements["baseline"],
            "candidate": measurements["candidate"],
            "receipt_ids": sorted(set(receipt_ids)),
        },
    )
    decision_call = interpreter or (
        lambda evidence: _extract_json(
            _model_call(
                skill_dir,
                "judge",
                "Decide KEEP, REVERT, or INCONCLUSIVE from this frozen Writer "
                "experiment evidence. Cite only receipt IDs present in evidence. "
                "Return only JSON with exactly decision, reason, evidence_refs.\n"
                + json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                f"{experiment_id}-decision",
            )
        )
    )
    decision = store.decide(experiment_id, interpreter=decision_call)
    if decision["decision"] == "KEEP":
        store.promote(experiment_id)
    assignment.update(
        {
            "status": "DECIDED",
            "decision": decision["decision"],
            "decision_receipt": str(experiment_dir / "decision.json"),
        }
    )
    _atomic_json(assignment_path, assignment)
    result = {
        "schema_version": 2,
        "status": decision["decision"],
        "experiment_id": experiment_id,
        "reason": decision["reason"],
        "canary_deltas": decision.get("canary_deltas", {}),
    }
    _atomic_json(close_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "offline", "current", "record-application", "record-consumption",
            "close-canary",
        ),
        default="offline",
    )
    parser.add_argument("--skill-dir", type=Path, default=SCRIPT_DIR.parent)
    parser.add_argument("--now")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.command == "current":
        result = current_assignment(args.skill_dir)
    elif args.command == "record-application":
        if args.run_dir is None or args.evidence is None:
            parser.error("record-application requires --run-dir and --evidence")
        result = record_canary_application(args.skill_dir, args.run_dir, args.evidence)
    elif args.command == "record-consumption":
        if args.run_dir is None or args.evidence is None:
            parser.error("record-consumption requires --run-dir and --evidence")
        now = datetime.fromisoformat(args.now) if args.now else datetime.now().astimezone()
        result = record_active_consumption(
            args.skill_dir, args.run_dir, args.evidence, consumed_at=now
        )
    elif args.command == "close-canary":
        now = datetime.fromisoformat(args.now) if args.now else datetime.now().astimezone()
        result = close_canary(args.skill_dir, now=now)
    else:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now().astimezone()
        result = run_offline_cycle(skill_dir=args.skill_dir, now=now)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
