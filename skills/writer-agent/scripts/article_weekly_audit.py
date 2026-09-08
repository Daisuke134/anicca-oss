#!/usr/bin/env python3
"""Seven-day public, language, SEO, media, and learning evidence audit."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import publication_remote  # noqa: E402
from publication_resume import (  # noqa: E402
    ACTIVE_PAIRS,
    LEGACY_EXACT8_PAIRS,
    PublicationStore,
    validate_receipt_evidence,
)
from publication_contract_resolver import infer_publication_contract  # noqa: E402

REQUIRED_PAIRS = ACTIVE_PAIRS


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _json_lines(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _required_pairs(value: dict[str, Any]) -> tuple[str, ...]:
    return (
        LEGACY_EXACT8_PAIRS
        if infer_publication_contract(value) == "legacy-exact8"
        else ACTIVE_PAIRS
    )


def summarize_run(value: dict[str, Any]) -> dict[str, Any]:
    failures = []
    required_pairs = _required_pairs(value)
    if value.get("exact8") is not True:
        failures.append("exact8")
    for field in ("remote_readback", "media"):
        evidence = value.get(field, {})
        for pair in required_pairs:
            if evidence.get(pair) != "PASS":
                failures.append(f"{field}:{pair}")
    for language in ("ja", "en"):
        if value.get("language", {}).get(language) != "PASS":
            failures.append(f"language:{language}")
    if value.get("metrics_snapshot") is not True:
        failures.append("metrics_snapshot")
    if value.get("experiment_application") == "MISSING":
        failures.append("experiment_application")
    return {
        **value,
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
    }


def _run_date(run_id: str) -> date | None:
    if not run_id.startswith("daily-"):
        return None
    try:
        return date.fromisoformat(run_id.removeprefix("daily-"))
    except ValueError:
        return None


def _application_status(
    skill_dir: Path,
    run_id: str,
) -> str:
    run_date = _run_date(run_id)
    if run_date is None:
        return "NOT_REQUIRED"
    applications = _json_lines(
        skill_dir / "state/playbook-applications.jsonl"
    )
    required = []
    for record in sorted(
        (skill_dir / "state/learning/experiments").glob("*.json")
    ):
        try:
            experiment = json.loads(record.read_text(encoding="utf-8"))
            cut = datetime.fromisoformat(experiment["cut"]).date()
            evaluation = datetime.fromisoformat(
                experiment["evaluation_at"]
            ).date()
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        if cut < run_date <= evaluation:
            required.append(str(experiment.get("experiment_id", "")))
    if not required:
        return "NOT_REQUIRED"
    for experiment_id in required:
        if not any(
            row.get("experiment_id") == experiment_id
            and row.get("run_id") == run_id
            and set(row.get("languages", [])) == {"ja", "en"}
            for row in applications
        ):
            return "MISSING"
    return "PASS"


def audit_run(
    skill_dir: Path,
    state_path: Path,
    ledger: Path,
    *,
    probe: Callable[
        [str, str, dict[str, Any]], dict[str, Any]
    ] = publication_remote.probe,
) -> dict[str, Any]:
    store = PublicationStore(state_path, ledger)
    state = store.read()
    run_id = str(state["run_id"])
    exact8 = store.completion_status() == "complete"
    required_pairs = _required_pairs(state)
    remote_results = {}
    media_results = {}
    for pair in required_pairs:
        entry = state["pairs"].get(pair, {})
        receipt = entry.get("receipt", {})
        expected_url = str(receipt.get("live_url", ""))
        try:
            remote = probe(pair, str(entry.get("target", "")), state)
            live_url = str(remote.get("live_url", ""))
            if (
                remote.get("status") != "live"
                or live_url != expected_url
            ):
                raise ValueError(
                    f"remote status/url mismatch: {remote.get('status')}"
                )
            validate_receipt_evidence(state, pair, live_url, remote)
            remote_results[pair] = "PASS"
            media_results[pair] = "PASS"
        except Exception as error:
            remote_results[pair] = f"FAIL:{error}"
            media_results[pair] = f"FAIL:{error}"

    run_dir = Path(str(state["run_dir"]))
    language = {}
    for lane in ("ja", "en"):
        result = subprocess.run(
            [
                "bash",
                str(skill_dir / "scripts/language-purity-gate.sh"),
                "--markdown-file",
                str(run_dir / f"article-{lane}.md"),
                "--lang",
                lane,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        language[lane] = (
            "PASS"
            if result.returncode == 0
            else f"FAIL:{(result.stderr or result.stdout)[-500:]}"
        )
    metrics = (
        skill_dir
        / "state/learning/metrics"
        / f"{run_id}.json"
    ).is_file()
    return summarize_run(
        {
            "run_id": run_id,
        "exact8": exact8,
        "publication_contract": infer_publication_contract(state),
            "remote_readback": remote_results,
            "language": language,
            "media": media_results,
            "metrics_snapshot": metrics,
            "experiment_application": _application_status(
                skill_dir,
                run_id,
            ),
        }
    )


def seo_evidence(rank_dir: Path) -> dict[str, Any]:
    files = sorted(rank_dir.glob("ranks-*.json"))
    if not files:
        return {
            "status": "MISSING",
            "reason": "no SEO rank snapshots",
        }
    result: dict[str, Any] = {
        "status": "PASS",
        "latest": str(files[-1]),
        "snapshot_count": len(files),
    }
    if len(files) < 2:
        result["delta"] = None
        result["reason"] = "only one SEO rank snapshot"
        return result

    def ranks(path: Path) -> dict[str, int]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rows = (
            value
            if isinstance(value, list)
            else value.get("keywords", [])
            if isinstance(value, dict)
            else []
        )
        return {
            str(row["keyword"]): int(row["rank"])
            for row in rows
            if isinstance(row, dict)
            and row.get("keyword")
            and isinstance(row.get("rank"), (int, float))
            and row["rank"] > 0
        }

    before = ranks(files[-2])
    after = ranks(files[-1])
    result["delta"] = {
        keyword: before[keyword] - after[keyword]
        for keyword in sorted(before.keys() & after.keys())
        if before[keyword] != after[keyword]
    }
    return result


def _telegram(target: str, message: str) -> str:
    result = subprocess.run(
        [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            target,
            "--message",
            message,
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    message_id = str(payload.get("messageId", "")).strip()
    if payload.get("dryRun") is True or not message_id:
        raise RuntimeError("weekly audit Telegram messageId is missing")
    return message_id


def audit(
    skill_dir: Path,
    *,
    state_dir: Path | None = None,
    today: date,
    target: str,
    notify: bool = True,
) -> dict[str, Any]:
    state_root = Path(state_dir) if state_dir is not None else skill_dir / "state"
    ledger = state_root / "articles.jsonl"
    # The audit runs at 22:00, thirty minutes before today's learning cycle.
    # Audit the seven fully closed article/learning days through yesterday.
    through = today - timedelta(days=1)
    since = through - timedelta(days=6)
    states = []
    for path in sorted(
        state_root.glob("runs/*/gates/publication-state.json")
    ):
        run_id = path.parents[1].name
        run_date = _run_date(run_id)
        if run_date is not None and since <= run_date <= through:
            states.append(path)
    runs = [audit_run(skill_dir, path, ledger) for path in states]
    seo = seo_evidence(
        Path(os.environ.get("WRITER_SEO_STATE_DIR", state_root / "seo-rank-monitor"))
    )
    failures = [
        failure
        for run in runs
        for failure in run["failures"]
    ]
    if not runs:
        failures.append("no exact8 run in seven-day window")
    if seo["status"] != "PASS":
        failures.append("seo")
    result = {
        "schema_version": 1,
        "audit_id": f"weekly-audit-{today.isoformat()}",
        "window": {
            "from": since.isoformat(),
            "through": through.isoformat(),
        },
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
        "runs": runs,
        "seo": seo,
        "audited_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt = (
        state_root
        / "learning"
        / f"weekly-audit-{today.isoformat()}.json"
    )
    try:
        prior = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        prior = None
    if (
        isinstance(prior, dict)
        and prior.get("audit_id") == result["audit_id"]
        and prior.get("status") == result["status"]
        and prior.get("failures") == result["failures"]
        and prior.get("runs") == result["runs"]
        and prior.get("seo") == result["seo"]
        and prior.get("messageId")
    ):
        return prior
    _atomic_json(receipt, result)
    if notify:
        message = (
            f"[{result['audit_id']}] status={result['status']} "
            f"runs={len(runs)} failures={len(failures)}"
        )
        result["messageId"] = _telegram(target, message)
        _atomic_json(receipt, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", type=Path, default=SCRIPTS.parent)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=os.environ.get("ARTICLE_STATE_DIR"),
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("TELEGRAM_TARGET_ID", "8547730585"),
    )
    parser.add_argument("--today", help=argparse.SUPPRESS)
    parser.add_argument(
        "--test-no-notify",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    try:
        result = audit(
            args.skill_dir,
            state_dir=args.state_dir,
            today=(
                date.fromisoformat(args.today)
                if args.today
                else datetime.now().astimezone().date()
            ),
            target=args.target,
            notify=not args.test_no_notify,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"weekly audit pending: {error}", file=sys.stderr)
        return 75
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
