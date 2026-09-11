#!/usr/bin/env python3
"""Canonical entrypoint for the current Marketing Engine runner lanes."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys

import run_contract
import run_with_contract


HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
REGISTRY_PATH = HERE / "runners.json"


def load_registry(path: pathlib.Path = REGISTRY_PATH) -> dict[str, dict]:
    body = json.loads(pathlib.Path(path).read_text())
    if body.get("schema_version") != "marketing.runners.v1":
        raise run_contract.ContractError("unsupported runner registry schema")
    runners = body.get("runners")
    if not isinstance(runners, dict) or set(runners) != set(run_contract.RUNNERS):
        raise run_contract.ContractError("runner registry must match the runner contract")
    for runner_id, item in runners.items():
        if not isinstance(item.get("command"), list) or not item["command"]:
            raise run_contract.ContractError(f"{runner_id} command is required")
        if not isinstance(item.get("product_ids"), list):
            raise run_contract.ContractError(f"{runner_id} product_ids must be an array")
        reason = item.get("quarantine_reason")
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise run_contract.ContractError(f"{runner_id} quarantine_reason is invalid")
    return runners


def resolve_command(command: list[str]) -> list[str]:
    values = {
        "repo": str(REPO_ROOT),
        "home": str(pathlib.Path.home()),
        "python": sys.executable,
    }
    return [part.format(**values) for part in command]


def default_roots(environment: dict[str, str] | None = None) -> tuple[pathlib.Path, pathlib.Path]:
    env = os.environ if environment is None else environment
    root = pathlib.Path(env.get("LIFE_MANAGER_STATE_ROOT", str(HERE.parent)))
    return root / "state", root / "evidence" / "runs"


def prepare_mine_command(command: list[str], state_root: pathlib.Path,
                         evidence_root: pathlib.Path) -> list[str]:
    """Seed mutable intel data outside the release and route daily writes there."""
    source = HERE.parent / "intel"
    target = pathlib.Path(state_root) / "intel"
    if target.is_symlink():
        raise OSError(f"refusing symlinked mutable intel directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        relative = path.relative_to(source)
        destination = target / relative
        parent = target
        for part in relative.parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise OSError(f"refusing symlinked mutable intel directory: {parent}")
            parent.mkdir(exist_ok=True)
            parent.chmod(0o700)
        if destination.is_symlink():
            raise OSError(f"refusing symlinked mutable intel file: {destination}")
        if not destination.exists():
            shutil.copy2(path, destination)
        destination.chmod(0o600)
    return [
        *command,
        "--source-registry", str(source / "sources.json"),
        "--video-registry", str(source / "video-sources.json"),
        "--intel-root", str(target),
        "--evidence-root", str(pathlib.Path(evidence_root) / "intel-daily"),
    ]


def main(argv: list[str] | None = None) -> int:
    default_state, default_evidence = default_roots()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runner", choices=sorted(run_contract.RUNNERS))
    parser.add_argument("--state-root", type=pathlib.Path,
                        default=default_state)
    parser.add_argument("--evidence-root", type=pathlib.Path,
                        default=default_evidence)
    parser.add_argument("--no-send", action="store_true")
    parser.add_argument("--timeout", type=int)
    args = parser.parse_args(argv)
    try:
        item = load_registry()[args.runner]
        command = resolve_command(item["command"])
        if args.runner == "mine":
            command = prepare_mine_command(command, args.state_root, args.evidence_root)
        result = run_with_contract.execute(
            runner_id=args.runner,
            command=command,
            state_root=args.state_root,
            evidence_root=args.evidence_root,
            environment="production",
            dry_run=False,
            product_ids=item["product_ids"],
            quarantine_reason=item["quarantine_reason"],
            send=not args.no_send,
            timeout=args.timeout,
        )
    except (OSError, json.JSONDecodeError, run_contract.ContractError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps({
        "runner_id": result.event["runner_id"],
        "run_id": result.event["run_id"],
        "status": result.event["status"],
        "returncode": result.returncode,
        "delivery": result.delivery,
    }, ensure_ascii=False, sort_keys=True))
    return result.returncode if result.event["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
