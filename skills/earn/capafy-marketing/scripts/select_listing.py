#!/usr/bin/env python3
"""
B1 — Capafy promotion selector (deterministic TOOL, no LLM).

Reads the seller endpoint GET /agent/agents (no buyer token needed), keeps only
agentStatus=="online" listings, and picks ONE with rotation + dedup so we don't
promote the same listing twice in a row. Records each pick in a rotation ledger.

Emits a single clean JSON object on stdout with the selected listing — this is the
handoff to B2 (the running agent writes the tweet copy from name+desc) and then to
x_post.py (--url/--tweet/--reply).

The copy is NOT written here (that is the agent's judgment, per the skill's design).
This tool only selects + resolves the URL + does bookkeeping.
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[4]))
CAPAFY_HTTP = str(REPO_ROOT / "skills/capafy-autopublish/vendor/capafy-user/scripts/capafy_http.py")
ROTATION = os.path.expanduser("~/.local/state/life-manager/state/capafy-marketing-rotation.jsonl")
EVIDENCE_ROOT = REPO_ROOT / "skills/capafy/marketing-evidence"
LISTING_URL_FMT = "https://capafy.ai/agent/{agent_id}"


def _fetch_agents() -> list:
    out = subprocess.run(
        [sys.executable, CAPAFY_HTTP, "GET", "/agent/agents"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    # capafy_http prints a log line then the JSON body; grab from the first { or [
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise RuntimeError("no JSON in capafy_http output")
    d = json.loads(out[start:])

    def find_list(x):
        if isinstance(x, list) and x and isinstance(x[0], dict):
            return x
        if isinstance(x, dict):
            for v in x.values():
                r = find_list(v)
                if r:
                    return r
        return None
    return find_list(d) or []


def _load_rotation() -> dict:
    last = {}
    if os.path.exists(ROTATION):
        for line in open(ROTATION):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                last[r["agent_id"]] = r["ts"]
            except Exception:  # noqa: BLE001
                continue
    return last


def _record(agent_id: str, rotation: str = ROTATION) -> None:
    os.makedirs(os.path.dirname(rotation), exist_ok=True)
    with open(rotation, "a") as f:
        f.write(json.dumps({"agent_id": agent_id, "ts": int(time.time())}) + "\n")


def choose(agents: list, last: dict, evidence_root: Path = EVIDENCE_ROOT) -> dict | None:
    eligible = []
    for agent in agents:
        agent_id = str(agent.get("agentId") or "")
        evidence = evidence_root / agent_id / "case1.md"
        if agent.get("agentStatus") == "online" and agent_id and evidence.is_file() and evidence.stat().st_size > 0:
            eligible.append((agent, evidence))
    if not eligible:
        return None
    eligible.sort(key=lambda row: (last.get(str(row[0].get("agentId")), 0), str(row[0].get("agentId"))))
    pick, evidence = eligible[0]
    return {
        "ok": True,
        "agent_id": str(pick.get("agentId")),
        "name": pick.get("name"),
        "desc": (pick.get("desc") or "")[:600],
        "sales": pick.get("sales"),
        "rating": pick.get("rating"),
        "url": LISTING_URL_FMT.format(agent_id=pick.get("agentId")),
        "online_pool": sum(agent.get("agentStatus") == "online" for agent in agents),
        "evidence_ready_pool": len(eligible),
        "evidence_source": str(evidence),
        "selection_committed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit-agent-id")
    parser.add_argument("--rotation", default=ROTATION)
    args = parser.parse_args()
    if args.commit_agent_id:
        _record(str(args.commit_agent_id), args.rotation)
        print(json.dumps({"ok": True, "agent_id": str(args.commit_agent_id), "selection_committed": True}))
        return 0
    agents = _fetch_agents()
    pick = choose(agents, _load_rotation())
    if pick is None:
        print(json.dumps({"ok": False, "error": "no online evidence-ready listings"}))
        return 1
    print(json.dumps(pick, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
