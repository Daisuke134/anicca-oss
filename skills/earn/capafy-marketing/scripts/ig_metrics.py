#!/usr/bin/env python3
"""
B6 (IG variant) — Instagram Reel metrics (deterministic TOOL, browser-direct read).

IG sibling of x_metrics.py. For each Capafy marketing Reel in the IG ledger, opens its
permalink on the CloakBrowser daily-driver (:9222, @useclaudeskills) and reads the PUBLIC
engagement IG renders (likes / comments; views/plays when shown). Appends a dated snapshot
to `capafy-marketing-ig-metrics.jsonl`. Empty ledger (no Reels yet — account still warming)
= clean no-op.

IG attribution is handled separately by pull_attribution.py, which pulls the landing redirect
counter after this metrics pass and joins it to the Capafy sales snapshot.
"""
import importlib.util
import json, os, re, subprocess, sys, time
from pathlib import Path

CDP = str(Path(__file__).resolve().parents[3] / "browser/scripts/cdp.py")
PY = sys.executable
IGLEDGER = os.path.expanduser("~/.local/state/life-manager/state/capafy-marketing-ig-ledger.jsonl")
METRICS = os.path.expanduser("~/.local/state/life-manager/state/capafy-marketing-ig-metrics.jsonl")
POSTER = Path(__file__).resolve().parents[2] / "marketing-engine/poster.py"
ACCOUNTS = os.path.expanduser("~/.cloak/clip-accounts-capafy.json")
REACH_MARKER = os.environ.get(
    "CAPAFY_IG_REACH_MARKER",
    os.path.expanduser("~/.local/state/life-manager/state/.capafy-ig-reach-healthy"),
)
CURRENT_HANDLE = os.environ.get("CAPAFY_IG_HANDLE", "")

READ_JS = r'''(() => {
  const a=document.querySelector('article'); if(!a) return JSON.stringify({available:false,reason:'article_absent'});
  const num=(s)=>{ if(!s) return 0; s=s.replace(/[,\s]/g,''); const m=s.match(/([\d\.]+)([KMkm]?)/); if(!m) return 0;
    let n=parseFloat(m[1]); if(/[Kk]/.test(m[2]))n*=1000; if(/[Mm]/.test(m[2]))n*=1e6; return Math.round(n); };
  const out={available:true,evidence:0,likes:0,comments:0,views:0};
  for (const el of a.querySelectorAll('[aria-label],span,a')){
    const t=(el.getAttribute('aria-label')||el.innerText||'');
    let m;
    if((m=t.match(/([\d,\.KMkm]+)\s*(likes|いいね)/i))) { out.likes=Math.max(out.likes,num(m[1])); out.evidence++; }
    if((m=t.match(/([\d,\.KMkm]+)\s*(comments|コメント)/i))) { out.comments=Math.max(out.comments,num(m[1])); out.evidence++; }
    if((m=t.match(/([\d,\.KMkm]+)\s*(views|plays|回視聴|再生)/i))) { out.views=Math.max(out.views,num(m[1])); out.evidence++; }
  }
  return JSON.stringify(out);
})()'''


def _browser_metrics(value):
    if not isinstance(value, dict) or not value.get("available") or not value.get("evidence"):
        return None
    return {
        "views": int(value.get("views", 0) or 0),
        "likes": int(value.get("likes", 0) or 0),
        "comments": int(value.get("comments", 0) or 0),
        "source": "instagram_public_dom",
        "metric_status": "measured",
    }


def _read(url):
    tid = subprocess.run([PY, CDP, "new", url], capture_output=True, text=True, timeout=60).stdout.strip().strip('"')
    time.sleep(7)
    r = subprocess.run([PY, CDP, "eval", tid, "-"], input=READ_JS, capture_output=True, text=True, timeout=60).stdout.strip().strip('"').replace('\\"', '"')
    subprocess.run([PY, CDP, "close", tid], capture_output=True, text=True, timeout=30)
    try:
        return _browser_metrics(json.loads(r))
    except Exception:
        return None


def _load_poster():
    spec = importlib.util.spec_from_file_location("capafy_metrics_poster", POSTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_read(url, handle, client_factory=None, poster_module=None):
    match = re.search(r"/(?:reel|p)/([^/?#]+)", str(url))
    settings = os.path.expanduser(f"~/.cloak/instagrapi-{handle}.json")
    if not match or not handle or not os.path.isfile(settings):
        return None
    try:
        if client_factory is None:
            from instagrapi import Client
            client_factory = Client
        poster_module = poster_module or _load_poster()
        client = client_factory()
        client.delay_range = [1, 3]
        poster_module.apply_proxy(client, handle, {}, ACCOUNTS)
        client.load_settings(settings)
        media = client.media_info_v1(client.media_pk_from_code(match.group(1)))
        data = media.model_dump() if hasattr(media, "model_dump") else media.dict()
        plays = int(data.get("play_count", 0) or 0)
        views = int(data.get("view_count", 0) or 0)
        return {
            "views": max(plays, views),
            "likes": int(data.get("like_count", 0) or 0),
            "comments": int(data.get("comment_count", 0) or 0),
            "source": "instagrapi_private",
            "metric_status": "measured",
        }
    except Exception:
        return None


def _write_reach_marker(metrics_path=METRICS, marker_path=REACH_MARKER, expected_handle=CURRENT_HANDLE):
    """Enable commercial copy only after two distinct owner-measured Reels have reach."""
    latest = {}
    if os.path.isfile(metrics_path):
        for line in open(metrics_path):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("reel_url"):
                latest[row["reel_url"]] = row
    healthy = [
        row for row in latest.values()
        if expected_handle
        and row.get("handle") == expected_handle
        and row.get("source") == "instagrapi_private"
        and row.get("metric_status") == "measured"
        and int(row.get("views", 0) or 0) > 0
    ]
    if len(healthy) < 2:
        return False
    receipt = {
        "status": "reach_healthy",
        "criterion": "two_distinct_owner_measured_reels_with_nonzero_views",
        "handle": expected_handle,
        "reels": sorted(row["reel_url"] for row in healthy),
        "observed_at": int(time.time()),
    }
    marker = Path(marker_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, marker)
    return True


def main():
    if not os.path.exists(IGLEDGER):
        print(json.dumps({"ok": True, "measured": 0, "note": "no IG Reels yet (account warming) — no-op"})); return 0
    reels = {}
    for line in open(IGLEDGER):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("reel_url"):
            reels[r["reel_url"]] = r
    if not reels:
        print(json.dumps({"ok": True, "measured": 0, "note": "no reel_url rows yet — no-op"})); return 0
    os.makedirs(os.path.dirname(METRICS), exist_ok=True)
    measured = 0
    incomplete = False
    for url, r in reels.items():
        s = _private_read(url, r.get("handle")) or _read(url)
        if s is None:
            incomplete = True
            print(json.dumps({"snapshot": {"reel_url": url, "agent_id": r.get("agent_id"), "metric_status": "unavailable"}}, ensure_ascii=False))
            continue
        row = {"ts": int(time.time()), "reel_url": url, "agent_id": r.get("agent_id"),
               "listing_name": r.get("listing_name"), "handle": r.get("handle"), **s}
        with open(METRICS, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        measured += 1
        print(json.dumps({"snapshot": row}, ensure_ascii=False))
    reach_healthy = _write_reach_marker()
    print(json.dumps({"ok": not incomplete, "measured": measured, "unavailable": int(incomplete),
                      "reach_healthy": reach_healthy}))
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
