#!/usr/bin/env python3
"""Pull landing redirect counts and join them to the Capafy sales snapshot."""

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib import request

REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[4]))
CAPAFY_HTTP = str(REPO_ROOT / "skills/capafy-autopublish/vendor/capafy-user/scripts/capafy_http.py")
DEFAULT_STATS_URL = "https://capafy-skills-daily.netlify.app/go-stats"
OUTPUT_FILE = Path(os.path.expanduser("~/.local/state/life-manager/state/capafy-attribution.jsonl"))
POSTS_FILE = Path(os.path.expanduser("~/.local/state/life-manager/state/capafy-marketing-ig-ledger.jsonl"))


def _find_agent_list(value):
    if isinstance(value, list) and (not value or isinstance(value[0], dict)):
        return value
    if isinstance(value, dict):
        for nested in value.values():
            found = _find_agent_list(nested)
            if found is not None:
                return found
    return None


def _fetch_agents():
    result = subprocess.run(
        [sys.executable, CAPAFY_HTTP, "GET", "/agent/agents"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"capafy_http failed with exit {result.returncode}")
    start = min(
        (index for index in (result.stdout.find("{"), result.stdout.find("[")) if index >= 0),
        default=-1,
    )
    if start < 0:
        raise RuntimeError("no JSON in capafy_http output")
    return json.loads(result.stdout[start:])


def _existing_row(output_file: Path, day: str):
    if not output_file.exists():
        return None
    for line in output_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("date") == day:
            return row
    return None


def _previous_row(output_file: Path, day: str):
    if not output_file.exists():
        return None
    rows = []
    for line in output_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("date") or "") < day:
            rows.append(row)
    return max(rows, key=lambda row: str(row.get("date") or ""), default=None)


def _posts_for_day(posts_file: Path, day: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if not posts_file.exists():
        return result
    for line in posts_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        agent_id = str(row.get("agent_id") or "").strip() if isinstance(row, dict) else ""
        published_at = str(row.get("published_at") or "") if isinstance(row, dict) else ""
        native_url = str(row.get("reel_url") or row.get("native_url") or "") if isinstance(row, dict) else ""
        if agent_id and published_at.startswith(day) and native_url:
            result.setdefault(agent_id, []).append(native_url)
    return {agent_id: sorted(set(urls)) for agent_id, urls in result.items()}


def _counter_delta(current, previous):
    if (
        isinstance(current, int)
        and not isinstance(current, bool)
        and isinstance(previous, int)
        and not isinstance(previous, bool)
        and current >= previous
    ):
        return current - previous
    return None


def pull(
    stats_url: str = DEFAULT_STATS_URL,
    output_file: Path = OUTPUT_FILE,
    posts_file: Path = POSTS_FILE,
    today: date | None = None,
):
    day = (today or date.today()).isoformat()
    existing = _existing_row(output_file, day)
    if existing is not None:
        return existing

    stats_request = request.Request(
        stats_url,
        headers={"Accept": "application/json", "User-Agent": "capafy-attribution/1"},
    )
    with request.urlopen(stats_request, timeout=30) as response:
        stats = json.load(response)
    if not isinstance(stats, dict):
        raise RuntimeError("go-stats response must be a JSON object")

    agents = _find_agent_list(_fetch_agents()) or []
    sales_by_id = {
        str(agent["agentId"]): agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("agentId") is not None
    }
    previous = _previous_row(output_file, day)
    previous_by_id = {
        str(row.get("agent_id")): row
        for row in ((previous or {}).get("agents") or [])
        if isinstance(row, dict) and row.get("agent_id") is not None
    }
    posts_by_id = _posts_for_day(posts_file, day)
    joined = []
    agent_ids = sorted({str(agent_id) for agent_id in stats} | set(posts_by_id))
    for agent_id in agent_ids:
        clicks = stats.get(agent_id, stats.get(int(agent_id), 0) if agent_id.isdigit() else 0)
        snapshot = sales_by_id.get(str(agent_id), {})
        cumulative_clicks = int(clicks)
        cumulative_sales = snapshot.get("sales")
        if not isinstance(cumulative_sales, int) or isinstance(cumulative_sales, bool):
            cumulative_sales = None
        prior = previous_by_id.get(agent_id, {})
        joined.append(
            {
                "agent_id": agent_id,
                "name": snapshot.get("name"),
                "post_urls": posts_by_id.get(agent_id, []),
                "cumulative_clicks": cumulative_clicks,
                "cumulative_sales": cumulative_sales,
                "window_clicks": _counter_delta(cumulative_clicks, prior.get("cumulative_clicks")),
                "window_sales": _counter_delta(cumulative_sales, prior.get("cumulative_sales")),
                "subscription_orders": None,
                "attribution_status": "candidate_no_order_level_source",
            }
        )

    row = {
        "schema_version": 2,
        "date": day,
        "window": {"start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z"},
        "causal_claim": False,
        "attribution_status": "candidate_no_order_level_source",
        "attribution_note": "Agent-level post, redirect, and sales windows are correlated only; Capafy exposes no order-level UTM/source or subscription-order join.",
        "agents": joined,
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return row


def main() -> int:
    try:
        row = pull(stats_url=os.environ.get("CAPAFY_GO_STATS_URL", DEFAULT_STATS_URL))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **row}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
