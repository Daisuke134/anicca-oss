#!/usr/bin/env python3
"""
inventory_status.py — deterministic answer to "does the drain-only loop have ANY real
work to do right now?", so the daily loop (and the health signal) can tell three states
apart that the old "did published.jsonl grow?" proxy could NOT:

  PUBLISHABLE  — there is a ready inventory item (LISTING + icon + skill dir) whose title
                 is not yet on the server, and the 5-slot cap is open. The loop SHOULD run
                 the publish flow. (Also covers a REVIEW_REJECTED item that needs a retry.)
  DRAINED      — every ready inventory title is already online. Nothing to publish. This is
                 HEALTHY IDLE, not a failure — do not alarm, do not burn a self-fix.
  CAP_FULL     — >=5 unlisted (draft/under_review) agents already occupy the publish cap.
                 Wait for review to clear. Healthy idle.
  SERVER_UNREADABLE — publish-list could not be read (auth/network). Report, do not guess.

WHY THIS EXISTS (self-fix-capafy-loop, 2026-07-08):
  The capafy healthcheck escalated an Opus self-fix whenever state/published.jsonl went 30h
  without growing, on the theory "loop alive but produces no skill => CP1 broken". But this
  loop is DRAIN-ONLY over a FINITE, hand-built inventory (c1-c5, o1-o10). Once every built
  listing is online (the real state on 2026-07-08: 20 online, 0 publishable), published.jsonl
  CANNOT grow, so the 30h alarm fires forever and spawns an expensive Opus fixer for a NON-bug.
  The pipeline (agentic cp1_agent.py -> CP2 -> CP3) was never broken; the loop just correctly
  stops at "inventory empty". The health proxy was wrong. This tool gives the loop a truthful
  verdict so the marker it writes distinguishes "healthy idle" from "genuinely stuck".

Server truth only (never the local ledger). Exit 0 always; verdict is on stdout as JSON and
as a VERDICT=<state> line for cheap bash grepping.
"""
import json, os, re, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_list_response import parse_agent_list

REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[3]))
STATE_HOME = Path(os.environ.get(
    "LIFE_MANAGER_STATE_HOME",
    Path.home() / ".local/state/life-manager",
)).expanduser()
AUTO = os.environ.get("CAPAFY_AUTO") or str(REPO_ROOT / "skills/capafy-autopublish")
PUB = os.path.join(AUTO, "vendor", "capafy-publisher")
ICONS = os.environ.get("CAPAFY_ICONS_DIR") or str(STATE_HOME / "assets/capafy/icons")
FEATURES = os.environ.get("CAPAFY_FEATURES_DIR") or str(STATE_HOME / "features")
SKILLS = os.environ.get("CAPAFY_SKILLS_ROOT") or str(REPO_ROOT / "skills")
CATALOG = os.environ.get("CAPAFY_CATALOG_DIR") or str(REPO_ROOT / "skills/capafy/catalog")

ONLINE = {"online", "approved"}
# Capafy's create endpoint counts rejected agents toward its five-unlisted-agent
# limit as well as drafts and submissions under review.  Treating a rejection as
# a free slot made the drainer select a fresh catalog item that publish-init could
# never create, then report a successful runner pass while the outer loop stayed
# PUBLISHABLE forever.  Keep the retry classification below, but include it in
# capacity accounting so the handoff mirrors the server's actual admission rule.
UNLISTED = {"draft", "under_review", "review_rejected"}
REJECTED = {"review_rejected", "banned"}
CAP = 5


def normalize_agents(agents):
    """Return sanitized rows and deterministic five-slot counts from server truth."""
    normalized = []
    counts = {"total": len(agents), "listed": 0, "occupied": 0, "free": None,
              "retry": 0, "blocked": 0, "unknown": 0}
    structurally_valid = True
    for agent in agents:
        if not isinstance(agent, dict):
            structurally_valid = False
            counts["unknown"] += 1
            continue
        agent_id = str(agent.get("agentId") or "").strip()
        status = agent.get("agentStatus")
        if not agent_id or not isinstance(status, str) or not status:
            structurally_valid = False
            counts["unknown"] += 1
            continue
        if status in ONLINE:
            lifecycle = "listed"
            counts["listed"] += 1
        elif status in UNLISTED:
            # A rejected agent is still retryable, but it also consumes one of
            # the platform's unlisted slots until Capafy releases it.
            lifecycle = "retry" if status == "review_rejected" else "occupied"
            counts["occupied"] += 1
            if status == "review_rejected":
                counts["retry"] += 1
        elif status == "banned":
            lifecycle = "blocked"
            counts["blocked"] += 1
        else:
            lifecycle = "unknown"
            structurally_valid = False
            counts["unknown"] += 1
        normalized.append({
            "agent_id": agent_id,
            "name": str(agent.get("name") or ""),
            "latest_version_id": agent.get("latestAgentVersionId"),
            "latest_version_name": agent.get("latestVersionName"),
            "remote_status": status,
            "lifecycle": lifecycle,
            "agent_type": agent.get("agentType"),
            "sales": agent.get("sales"),
            "recent_sales": agent.get("recentSales"),
        })
    if structurally_valid:
        counts["free"] = max(0, CAP - counts["occupied"])
    else:
        counts["occupied"] = None
    return {"readable": structurally_valid, "counts": counts, "agents": normalized}


def allocate_action(normalized, retries, publishable, resumable_drafts=None):
    """Choose at most one stable action without performing any platform write.

    An exact-title repository draft can be resumed in place even when all five
    submission slots are occupied: finishing that existing Agent does not create
    a sixth Agent.  The optional argument keeps the pre-resume call contract
    compatible for callers that only provide retry/fresh candidates.
    """
    resumable_drafts = resumable_drafts or []
    if not normalized.get("readable"):
        return {"verdict": "SERVER_UNREADABLE"}
    occupied = (normalized.get("counts") or {}).get("occupied")
    if not isinstance(occupied, int) or isinstance(occupied, bool) or occupied < 0:
        return {"verdict": "SERVER_UNREADABLE"}
    if resumable_drafts:
        item = min(
            resumable_drafts,
            key=lambda row: (str(row.get("agent_id") or ""), str(row.get("title") or "")),
        )
        return {
            "verdict": "PUBLISHABLE",
            "reason": "resume exact-title repository draft",
            "action": "resume_draft",
            "action_key": f"resume:{item['agent_id']}",
            "item": item,
        }
    # Capafy permits at most five simultaneous draft/under-review submissions.
    # A rejected agent becomes retryable only after that rejection has freed a slot;
    # retrying an old agent does not create a sixth submission exception.
    if occupied >= CAP:
        return {"verdict": "CAP_FULL", "occupied": occupied}
    if retries:
        item = min(retries, key=lambda row: (str(row.get("agent_id") or ""), str(row.get("title") or "")))
        return {
            "verdict": "PUBLISHABLE",
            "reason": "review_rejected retry",
            "action": "retry_existing",
            "action_key": f"retry:{item['agent_id']}",
            "item": item,
        }
    if publishable:
        item = min(publishable, key=lambda row: (str(row.get("feature") or ""), str(row.get("title") or "")))
        identity = item.get("feature") or item.get("title")
        return {
            "verdict": "PUBLISHABLE",
            "action": "create_fresh",
            "action_key": f"create:{identity}",
            "item": item,
        }
    return {"verdict": "DRAINED"}


def server_agents():
    """Return list of server agents, or None on read failure."""
    try:
        result = subprocess.run(
            [sys.executable, "packager.py", "publish-list"],
            cwd=PUB, capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            raise RuntimeError(f"publish-list exited with {result.returncode}")
        return parse_agent_list(json.loads(result.stdout, strict=False))
    except Exception as e:
        print(f"[inventory_status] server read FAILED: {e}", file=sys.stderr)
        return None


def listing_title(path):
    """Extract the '## Title' value from a LISTING.md (same rule publish_prepare.sh uses)."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except Exception:
        return None
    for i, ln in enumerate(lines):
        if ln.strip() == "## Title" and i + 1 < len(lines):
            return lines[i + 1].strip()
    return None


def ready_inventory():
    """Return complete legacy candidates plus repository-owned canonical catalog items."""
    items = []
    if os.path.isdir(FEATURES):
        for name in sorted(os.listdir(FEATURES)):
            if not name.startswith("capafy-"):
                continue
            d = os.path.join(FEATURES, name)
            listing = os.path.join(d, "LISTING.md")
            title = listing_title(listing) if os.path.isfile(listing) else None
            m = re.match(r"^capafy-([a-z][0-9]+)-", name)
            icon = os.path.join(ICONS, m.group(1) + ".png") if m else ""
            skill = os.path.join(d, "SKILL.md")
            if title and os.path.isfile(icon) and os.path.isfile(skill):
                items.append({"feature": name, "title": title, "icon": icon,
                              "listing": listing, "skill": skill, "source": "legacy_state"})

    if os.path.isdir(CATALOG):
        for name in sorted(os.listdir(CATALOG)):
            d = os.path.join(CATALOG, name)
            if not os.path.isdir(d):
                continue
            listing = os.path.join(d, "LISTING.md")
            skill = os.path.join(d, "SKILL.md")
            # SVG is source artwork; CP1 only accepts PNG/JPG/WebP. Prefer a
            # listing-ready raster asset so a resumed draft can save Basic Info.
            icon = next((os.path.join(d, candidate) for candidate in ("icon.png", "icon.jpg", "icon.webp", "icon.svg")
                         if os.path.isfile(os.path.join(d, candidate))), None)
            title = listing_title(listing) if os.path.isfile(listing) else None
            if title and icon and os.path.isfile(skill):
                items.append({"feature": f"catalog:{name}", "title": title, "icon": icon,
                              "listing": listing, "skill": skill, "source": "repo_catalog"})

    # The repository catalog is authoritative when a legacy candidate has the same title.
    by_title = {}
    for item in items:
        if item["title"] not in by_title or item["source"] == "repo_catalog":
            by_title[item["title"]] = item
    return [by_title[title] for title in sorted(by_title)]


def main():
    agents = server_agents()
    if agents is None:
        verdict = {"verdict": "SERVER_UNREADABLE"}
        print("VERDICT=SERVER_UNREADABLE")
        print(json.dumps(verdict, ensure_ascii=False))
        return 0

    normalized = normalize_agents(agents)
    if not normalized["readable"]:
        verdict = {"verdict": "SERVER_UNREADABLE", **normalized}
        print("VERDICT=SERVER_UNREADABLE")
        print(json.dumps(verdict, ensure_ascii=False))
        return 0

    online_titles = {(a.get("name") or "").strip() for a in agents if a.get("agentStatus") in ONLINE}
    unlisted = [a for a in agents if a.get("agentStatus") in UNLISTED]
    rejected = [a for a in agents if a.get("agentStatus") in REJECTED]

    # In-flight titles = agents already submitted and awaiting review, or a half-saved draft
    # (draft/under_review). An inventory item whose title is already in-flight must NOT count as
    # publishable — it is done from the loop's side (the resume-guard would just re-open an already
    # status=1 agent, the loop would forever log "PUBLISHABLE" and never reach DRAINED, and the
    # healthcheck's healthy-pass marker would go stale → a FALSE self-fix escalation). Publishable =
    # ready inventory that is NOT online AND NOT already in-flight on the server. (self-fix, 2026-07-08)
    inflight_titles = {(a.get("name") or "").strip() for a in unlisted}

    items = ready_inventory()
    rejected_titles = {(a.get("name") or "").strip() for a in rejected}
    publishable = [it for it in items if it["title"] not in online_titles
                   and it["title"] not in inflight_titles
                   and it["title"] not in rejected_titles]

    # A rejected agent is only retryable if its title still matches a CURRENT
    # ready_inventory LISTING.md. If the LISTING.md title has since drifted (edited,
    # or the skill was successfully republished under a new agent_id/title), the
    # rejected agent is an ORPHAN: no local content matches it, retrying is a no-op
    # that just creates a duplicate draft (publish_prepare.sh's exact-title RESUME
    # GUARD can never find it). An orphan must NOT block DRAINED forever.
    # (self-fix-capafy-loop, 2026-07-17: found agent 2485008254 stuck exactly this
    # way — review_rejected under an old title "...Built for Retention" while
    # o9's LISTING.md now reads "...Keep Viewers Watching", already online as a
    # different agent_id 7686597754.)
    ready_titles = {it["title"] for it in items}
    retryable_rejected = [a for a in rejected if (a.get("name") or "").strip() in ready_titles]

    ready_by_title = {item["title"]: item for item in items}
    resumable_drafts = []
    for agent in agents:
        if agent.get("agentStatus") != "draft":
            continue
        title = (agent.get("name") or "").strip()
        item = ready_by_title.get(title)
        if not item or item.get("source") != "repo_catalog":
            continue
        resumable_drafts.append({"agent_id": str(agent.get("agentId")), **item})

    retry_items = []
    for agent in retryable_rejected:
        title = (agent.get("name") or "").strip()
        retry_items.append({"agent_id": str(agent.get("agentId")), "title": title,
                            **ready_by_title[title]})
    fresh_items = [
        {key: item[key] for key in ("feature", "title", "icon", "listing", "skill", "source")}
        for item in publishable
    ]
    v = allocate_action(normalized, retry_items, fresh_items, resumable_drafts)

    v.update({
        "online_count": len(online_titles),
        "unlisted_count": len(unlisted),
        "rejected_count": len(rejected),
        "ready_inventory": len(items),
        "publishable_count": len(publishable),
        **normalized,
    })
    print("VERDICT=" + v["verdict"])
    print(json.dumps(v, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
