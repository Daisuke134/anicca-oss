#!/usr/bin/env python3
"""
reconcile_ledger.py — make state/published.jsonl mirror the Capafy SERVER truth.

WHY THIS EXISTS (self-fix-capafy-loop, 2026-07-07):
  The daily loop's health signal is `state/published.jsonl` (the healthcheck escalates a
  self-fix when it stops growing for 30h). But the ledger was a LOCAL append-only log that
  silently drifted from the server:
    - agents that actually went ONLINE (e.g. O1 Japanese Humanizer 6501274812, O2 Academic
      Humanizer 7883384570 — both status=4 listed) were never appended → ledger looked stale
      → false-positive 30h escalation, even though the pipeline had really published them;
    - an agent recorded as "submitted status=1" (O9 YouTube 7686597754) was actually
      REVIEW_REJECTED on the server (status=2/auditStatus=3) → the loop kept treating a dead
      item as done and never retried it.
  Dual source-of-truth (local ledger vs server) is the real defect. The server is the ONLY
  truth. This tool reconciles the ledger against `publish-list` every run, so:
    - the ledger grows the moment the server has a newly-online agent (health signal recovers
      and stays truthful),
    - review_rejected items surface as work-to-retry instead of silently "done".

  It NEVER invents a publish: it only records agents the SERVER reports as online, and only
  flags (does not fabricate) rejected ones. Idempotent + atomic.

Usage:
  reconcile_ledger.py            # reconcile, print a summary, exit 0
  reconcile_ledger.py --json     # also print a machine-readable summary line (RECONCILE_JSON=...)
Exit 0 always (reconciliation is best-effort; a server read failure is reported, not fatal).
"""
import json, os, subprocess, sys, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_list_response import parse_agent_list

# Source stays in the repository; auth and mutable ledger state live in the user state root.
REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[3]))
STATE_HOME = Path(os.environ.get(
    "LIFE_MANAGER_STATE_HOME",
    Path.home() / ".local/state/life-manager",
)).expanduser()
AUTO = os.environ.get("CAPAFY_AUTO") or str(REPO_ROOT / "skills/capafy-autopublish")
PUB = os.path.join(AUTO, "vendor", "capafy-publisher")
LEDGER = os.environ.get("CAPAFY_PUBLISHED_LEDGER") or str(
    STATE_HOME / "state/capafy-autopublish/published.jsonl"
)

ONLINE = {"online", "approved"}          # agentStatus values that mean "live / listed"
REJECTED = {"review_rejected", "banned"} # need attention / retry
DRAFT = {"draft"}                        # created but never submitted → orphan stub, surface it


def load_ledger():
    """Return entries plus ids already recorded as server-confirmed online."""
    entries, online_ids = [], set()
    if not os.path.exists(LEDGER):
        return entries, ids
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                entries.append(line)  # preserve unparseable lines verbatim
                continue
            entries.append(d)
            aid = str(d.get("agent_id") or d.get("agentId") or "").strip()
            if aid:
                status = str(d.get("status") or "").strip().lower()
                if status.startswith("online") or status.startswith("approved"):
                    online_ids.add(aid)
    return entries, online_ids


def server_agents():
    """Return the server's agent list (publish-list), or None on read failure."""
    try:
        result = subprocess.run(
            [sys.executable, "packager.py", "publish-list"],
            cwd=PUB, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"publish-list exited with {result.returncode}")
        return parse_agent_list(json.loads(result.stdout, strict=False))
    except Exception as e:
        print(f"[reconcile] server read FAILED: {e}", file=sys.stderr)
        return None


def main():
    want_json = "--json" in sys.argv[1:]
    entries, recorded_online = load_ledger()
    agents = server_agents()
    if agents is None:
        # fail-safe: never touch the ledger on a bad read
        if want_json:
            print('RECONCILE_JSON=' + json.dumps({"ok": False, "reason": "server_read_failed"}))
        return 0

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    appended, rejected, drafts = [], [], []
    for a in agents:
        aid = str(a.get("agentId") or "").strip()
        status = a.get("agentStatus")
        name = (a.get("name") or "").strip()
        if not aid:
            continue
        if status in ONLINE and aid not in recorded_online:
            entry = {
                "agent_id": aid, "title": name,
                "status": "online (status=4 listed) — reconciled from server",
                "date": today,
                "note": "reconcile_ledger.py: server-confirmed live but was missing from ledger.",
            }
            entries.append(entry); recorded_online.add(aid); appended.append((aid, name))
        elif status in REJECTED:
            rejected.append((aid, status, name))
        elif status in DRAFT:
            # Orphan stub: publish-init created a card that CP1/CP3 never finished. These used to
            # be INVISIBLE (reconcile only knew online/rejected), so a half-published draft rotted
            # silently AND occupied one of the 5 publish-cap slots. Surface it so a human/Opus pass
            # can either finish it or abandon it — never a silent leak. (Not auto-published: a stub
            # with the "(LM generated…)" placeholder title is a duplicate of an already-online skill.)
            drafts.append((aid, name))

    if appended:
        tmp = LEDGER + ".tmp"
        with open(tmp, "w") as f:
            for e in entries:
                f.write((e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + "\n")
        os.replace(tmp, LEDGER)

    print(f"[reconcile] server agents={len(agents)} appended={len(appended)} "
          f"rejected={len(rejected)} drafts={len(drafts)}")
    for aid, name in appended:
        print(f"  +ONLINE {aid}  {name[:52]}")
    for aid, st, name in rejected:
        print(f"  !{st.upper()} {aid}  {name[:52]}  (needs re-publish / retry)")
    for aid, name in drafts:
        print(f"  ?DRAFT {aid}  {name[:52]}  (orphan stub — finish or abandon; occupies a cap slot)")
    if want_json:
        print("RECONCILE_JSON=" + json.dumps({
            "ok": True, "appended": [a for a, _ in appended],
            "rejected": [{"agent_id": a, "status": s, "name": n} for a, s, n in rejected],
            "drafts": [{"agent_id": a, "name": n} for a, n in drafts],
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
