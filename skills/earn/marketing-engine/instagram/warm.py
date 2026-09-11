#!/usr/bin/env python3
"""ig-account-warmer — verified passive + day-gated engagement warmup.

The passive path watches reels, views stories, visits feed profiles, and scrolls. The
engagement path follows the day caps below, uses accessible labels instead of DOM-position
selectors, and counts a follow/like only after its UI state changes. Missing controls and
unavailable comment text are reported as unfulfilled instead of silently skipped.

Sources: SPEC.md (BlackHatWorld 2025: "watching reels = best warmup"; same-IP/device; slow ramp;
72h-critical → day1-2 passive; instagrapi best-practices: one stable IP per account).

Usage: warm.py <handle> [--dry]
"""
import sys, os, json, time, random, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "browser/scripts"))
import cdp

HANDLE = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
DRY = "--dry" in sys.argv
STATE = os.path.expanduser(f"~/.cloak/ig-warmup-{HANDLE}.json") if HANDLE else None

# Passive caps ramp with warmup age (reels = primary).
CAPS = {1: {"reels": 6, "scrolls": 5, "stories": 3, "profiles": 2},
        2: {"reels": 8, "scrolls": 6, "stories": 5, "profiles": 3},
        3: {"reels": 8, "scrolls": 6, "stories": 5, "profiles": 3},
        4: {"reels": 10, "scrolls": 6, "stories": 6, "profiles": 3},
        5: {"reels": 10, "scrolls": 7, "stories": 6, "profiles": 4},
        6: {"reels": 12, "scrolls": 7, "stories": 7, "profiles": 4},
        7: {"reels": 12, "scrolls": 8, "stories": 8, "profiles": 5}}
ENGAGEMENT_CAPS = {
    1: {"follow": (0, 0), "like": (0, 0), "comment": (0, 0)},
    2: {"follow": (3, 5), "like": (5, 10), "comment": (0, 2)},
    3: {"follow": (5, 10), "like": (10, 20), "comment": (3, 5)},
    4: {"follow": (5, 10), "like": (15, 25), "comment": (3, 5)},
}
BAN = ["操作がブロック", "Action Blocked", "アクションはブロック", "後でもう一度", "Try Again Later",
       "We restrict certain activity", "本人確認", "一時的にブロック", "challenge_required",
       "アカウントが停止", "Account Suspended"]
MAXFAIL = 4


def jit(a, b): time.sleep(random.uniform(a, b))


def require_isolated_port(port):
    port = int(port)
    if port == 9222:
        raise ValueError("refusing Instagram activity on :9222 main context; isolated port required")
    return port


def engagement_plan(day, requested=None, chooser=random.randint):
    """Choose within recipe caps and clamp explicit requests; day1 requests are refused."""
    raw = ENGAGEMENT_CAPS[min(max(int(day), 1), 4)]
    requested = requested or {}
    out = {"caps": {}, "requested": dict(requested), "targets": {},
           "refused": {}, "clamped": {}}
    for action, (lo, hi) in raw.items():
        out["caps"][action] = {"min": lo, "max": hi}
        wanted = requested.get(action)
        if wanted is not None:
            wanted = max(0, int(wanted))
            target = min(wanted, hi)
            if hi == 0 and wanted:
                out["refused"][action] = "day1 engagement is forbidden"
            if target != wanted:
                out["clamped"][action] = {"requested": wanted, "target": target}
        else:
            target = chooser(lo, hi)
        out["targets"][action] = target
    return out


def ev(tid, expr):
    r = cdp.evaluate(tid, expr)
    return None if (isinstance(r, dict) and "__error__" in r) else r


def loadj(p, d):
    try: return json.load(open(p))
    except Exception: return dict(d)


def ban(tid):
    t = (ev(tid, "document.body.innerText") or "")
    return next((s for s in BAN if s.lower() in t.lower()), None)


def logged_in(tid):
    if ev(tid, """(()=>!!document.querySelector('input[name="username"],input[name="password"]'))()"""):
        return False
    marker = ev(tid, """(()=>document.querySelectorAll(
      'svg[aria-label="新規投稿"],svg[aria-label="New post"],svg[aria-label="メッセージ"],a[href$="/saved/"]').length)()""")
    return bool(marker and marker > 0)


def watch_reels(tid, n):
    """Watch n reels. HONEST: count only reels whose <video> currentTime ADVANCED over the dwell
    (or wrapped — a short looping reel). No author recorded (DOM can't reliably ID the reel author)."""
    cdp.navigate(tid, "https://www.instagram.com/reels/"); time.sleep(6)
    played, fails = 0, 0
    vsel = """(()=>{const v=[...document.querySelectorAll('video')]
      .find(x=>{const r=x.getBoundingClientRect();return r.height>300&&r.top<700&&r.bottom>120;});
      return v?Number(v.currentTime||0):null;})()"""
    while played < n and fails < MAXFAIL:
        if ban(tid): break
        t0 = ev(tid, vsel)
        if not isinstance(t0, (int, float)):
            cdp.press_key(tid, "ArrowDown", code="ArrowDown", vk=40); jit(2, 4); fails += 1; continue
        jit(8, 18)  # real dwell
        t1 = ev(tid, vsel)
        if isinstance(t1, (int, float)) and (t1 > t0 + 1.5 or (0 <= t1 < t0)):
            played += 1; fails = 0
        else:
            fails += 1
        cdp.press_key(tid, "ArrowDown", code="ArrowDown", vk=40); jit(2.5, 4.5)
    return played


def _story_marker(tid):
    return ev(tid, """(()=>{
      if(!location.pathname.startsWith('/stories/'))return null;
      const media=[...document.querySelectorAll('video,img')]
        .filter(x=>{const r=x.getBoundingClientRect();return r.width>240&&r.height>300&&r.bottom>0&&r.top<innerHeight;})
        .sort((a,b)=>{const ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();
          return br.width*br.height-ar.width*ar.height;})[0];
      if(!media)return null;
      return [location.pathname,media.tagName,media.currentSrc||media.src||media.poster||'',
        media.getAttribute('alt')||''].join('|');})()""")


def view_stories(tid, n):
    """View n stories. HONEST: count only an opened story or advance with a changed DOM media marker."""
    cdp.navigate(tid, "https://www.instagram.com/"); time.sleep(6)
    before = _story_marker(tid)
    opened = ev(tid, """(()=>{
      const visible=x=>{const r=x.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight;};
      let target=[...document.querySelectorAll('a[href^="/stories/"]')].find(visible);
      if(!target){
        const img=[...document.querySelectorAll('main img[alt]')].find(x=>{
          const r=x.getBoundingClientRect(),alt=x.getAttribute('alt')||'';
          return visible(x)&&r.left>220&&r.top>40&&r.top<340&&r.width>=36&&r.width<=140&&
            /profile picture|プロフィール写真/i.test(alt);});
        target=img&&img.closest('button,[role="button"],a');
      }
      if(!target)return null;
      const hint=target.getAttribute('href')||target.textContent||'story-tray';
      target.click();return String(hint).trim().slice(0,120);})()""")
    if not opened: return 0
    jit(4.5, 8.0)
    after = _story_marker(tid)
    if not after or after == before: return 0
    viewed, fails = 1, 0
    while viewed < n and fails < MAXFAIL:
        if ban(tid): break
        before = after
        cdp.press_key(tid, "ArrowRight", code="ArrowRight", vk=39); jit(4.5, 9.0)
        after = _story_marker(tid)
        if after and after != before:
            viewed += 1; fails = 0
        else:
            fails += 1
    return viewed


def _profile_marker(tid, expected):
    return ev(tid, """(()=>{
      const expected=%s;
      if(location.pathname.toLowerCase()!==expected.toLowerCase())return null;
      const main=document.querySelector('main'),header=main&&main.querySelector('header');
      if(!header||!header.querySelector('h1,h2,img,span'))return null;
      return expected;})()""" % json.dumps(expected))


def visit_profiles(tid, n):
    """Visit n distinct feed profiles. HONEST: count only routes with a loaded profile-page DOM marker."""
    cdp.navigate(tid, "https://www.instagram.com/"); time.sleep(6)
    attempted, visited, fails = set(), set(), 0
    own = json.dumps(f"/{HANDLE.lstrip('@')}/".lower())
    while len(visited) < n and fails < MAXFAIL:
        if ban(tid): break
        candidate = ev(tid, """(()=>{
          const attempted=new Set(%s),own=%s;
          const visible=x=>{const r=x.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>80&&r.top<innerHeight-40;};
          const links=[...document.querySelectorAll('main article a[href]')].filter(a=>{
            const path=new URL(a.href,location.origin).pathname;
            return visible(a)&&/^\\/[A-Za-z0-9._]+\\/$/.test(path)&&path.toLowerCase()!==own&&
              !attempted.has(path.toLowerCase());});
          const target=links[0];
          if(!target)return null;
          const path=new URL(target.href,location.origin).pathname;
          target.scrollIntoView({block:'center'});target.click();return path;})()""" %
          (json.dumps(sorted(attempted)), own))
        if not candidate:
            ev(tid, f"window.scrollBy(0,{random.randint(500, 1000)}); 'ok'"); jit(3.5, 7.0)
            fails += 1; continue
        attempted.add(candidate.lower())
        jit(3.0, 6.0)
        marker = None
        for _ in range(3):
            marker = _profile_marker(tid, candidate)
            if marker: break
            jit(1.5, 2.5)
        if marker and candidate.lower() not in visited:
            visited.add(candidate.lower()); fails = 0; jit(4.5, 10.0)
        else:
            fails += 1
        ev(tid, "history.back(); 'ok'"); jit(3.0, 6.0)
        if not ev(tid, "location.pathname==='/'"):
            cdp.navigate(tid, "https://www.instagram.com/"); time.sleep(5)
    return len(visited)


def scroll_feed(tid, n):
    cdp.navigate(tid, "https://www.instagram.com/"); time.sleep(6)
    for _ in range(n):
        cdp.evaluate(tid, f"window.scrollBy(0, {random.randint(500, 1100)}); 'ok'"); jit(3.5, 8.0)
    return n


def engage_feed(tid, plan):
    """Use accessible labels/text, and count only a verified post-click state change."""
    done = {"follow": 0, "like": 0, "comment": 0}
    unfulfilled = []
    cdp.navigate(tid, "https://www.instagram.com/"); time.sleep(6)
    scripts = {
        "follow": """(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&r.bottom>0&&r.top<innerHeight};const b=[...document.querySelectorAll('button,[role=button]')].find(e=>visible(e)&&/^(follow|フォローする)$/i.test((e.innerText||e.getAttribute('aria-label')||'').trim()));if(!b)return {ok:false,reason:'no semantic Follow control'};b.click();return new Promise(r=>setTimeout(()=>r({ok:/following|requested|フォロー中|リクエスト済み/i.test((b.innerText||b.getAttribute('aria-label')||'').trim()),reason:'follow state did not change'}),1200));})()""",
        "like": """(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&r.bottom>0&&r.top<innerHeight};const s=[...document.querySelectorAll('svg[aria-label]')].find(e=>visible(e)&&/^(like|いいね！?)$/i.test(e.getAttribute('aria-label')||''));if(!s)return {ok:false,reason:'no accessible Like control'};const b=s.closest('button,[role=button]')||s.parentElement;b.click();return new Promise(r=>setTimeout(()=>{const a=(b.querySelector('svg[aria-label]')||s).getAttribute('aria-label')||'';r({ok:/unlike|いいね！を取り消す/i.test(a),reason:'like state did not change'})},900));})()""",
        # Comments require authored text/judgment. Never fabricate one; report it explicitly.
        "comment": None,
    }
    for action in ("follow", "like", "comment"):
        target = plan["targets"][action]
        if not target:
            continue
        if scripts[action] is None:
            unfulfilled.append({"action": action, "target": target, "completed": 0,
                                "reason": "comment text unavailable; refusing fabricated comment"})
            continue
        failures = 0
        while done[action] < target and failures < MAXFAIL:
            result = ev(tid, scripts[action])
            if isinstance(result, dict) and result.get("ok"):
                done[action] += 1; failures = 0; jit(4.0, 11.0)
            else:
                failures += 1
                ev(tid, f"window.scrollBy(0,{random.randint(450, 900)}); 'ok'"); jit(2.5, 6.0)
        if done[action] < target:
            unfulfilled.append({"action": action, "target": target, "completed": done[action],
                                "reason": (result.get("reason") if isinstance(result, dict) else "control unavailable")})
    return done, unfulfilled


def _activity(res, name, fn):
    try:
        return fn()
    except Exception as e:
        warning = f"{name}: {repr(e)[:160]}"
        res.setdefault("warnings", []).append(warning)
        print(f"WARN {warning}", file=sys.stderr)
        return 0


def main():
    if not HANDLE and not DRY: print("usage: warm.py <handle> [--dry]"); sys.exit(2)
    os.makedirs(os.path.expanduser("~/.cloak"), exist_ok=True)
    today = datetime.date.today().isoformat()
    st = loadj(STATE, {"handle": HANDLE, "log": []}) if STATE else {"handle": None, "log": []}
    day = max(1, min(7, (datetime.date.today() - datetime.date.fromisoformat(st["log"][0]["date"])).days + 1)) if st["log"] else 1
    caps = CAPS[day]
    engagement = engagement_plan(day)
    if DRY: print(json.dumps({"plan": {"day": day, "caps": caps, "engagement": engagement}}, ensure_ascii=False)); return

    try:
        require_isolated_port(os.environ.get("CDP_PORT", "9222"))
    except ValueError as e:
        print(json.dumps({"refused": str(e)}, ensure_ascii=False)); return

    hour = datetime.datetime.now().hour
    if not 9 <= hour <= 23:
        skip = {"date": today, "action": "skip_off_hours", "hour": hour}
        st.setdefault("skips", []).append(skip)
        json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)
        print(json.dumps(skip, ensure_ascii=False)); return
    if any(r.get("date") == today for r in st["log"]):   # only SUCCESSFUL runs are in 'log'
        print(json.dumps({"skip": "already warmed today", "day": day})); return

    res = {"date": today, "day": day, "actions": {}, "verified": {}}
    tid = None
    try:
        tid = cdp.new_tab("https://www.instagram.com/"); time.sleep(7)
        if not logged_in(tid): res["ABORT"] = "not logged in"; return
        if ban(tid): res["ABORT"] = "ban on load"; res["signal"] = ban(tid); return

        reels = _activity(res, "watch_reels", lambda: watch_reels(tid, caps["reels"]))
        res["actions"]["reels"] = res["verified"]["reels_played"] = reels
        sig = ban(tid)
        if sig: res["ban"] = True; res["ban_signal"] = sig; return

        stories = _activity(res, "view_stories", lambda: view_stories(tid, caps["stories"]))
        res["actions"]["stories"] = res["verified"]["stories_viewed"] = stories
        sig = ban(tid)
        if sig: res["ban"] = True; res["ban_signal"] = sig; return

        profiles = _activity(res, "visit_profiles", lambda: visit_profiles(tid, caps["profiles"]))
        res["actions"]["profiles"] = res["verified"]["profiles_visited"] = profiles
        sig = ban(tid)
        if sig: res["ban"] = True; res["ban_signal"] = sig; return

        scrolls = _activity(res, "scroll_feed", lambda: scroll_feed(tid, caps["scrolls"]))
        res["actions"]["scrolls"] = scrolls
        engagement_done, unfulfilled = engage_feed(tid, engagement)
        res["actions"].update(engagement_done)
        res["verified"].update({f"{k}_state_changes": v for k, v in engagement_done.items()})
        res["engagement_plan"] = engagement
        if unfulfilled:
            res["unfulfilled"] = unfulfilled
        sig = ban(tid)
        if sig: res["ban_signal"] = sig
    except Exception as e:
        res["error"] = repr(e)[:200]
        print(f"WARN run: {res['error']}", file=sys.stderr)
    finally:
        _save(st, res)
        try:
            if tid: cdp.screenshot(tid, os.path.expanduser(f"~/.cloak/warmup-{HANDLE}-{today}.png"))
        except Exception: pass
        print(json.dumps(res, ensure_ascii=False))


def _save(st, res):
    # FIND-004 fix: ONLY a real warmup (did actions, no load-time ABORT) counts toward the day-anchor
    # + same-day idempotency. ABORT/no-action runs go to 'aborts' (informational) so a transient
    # failure never advances the ramp nor burns the day.
    did_action = any(isinstance(v, (int, float)) and v > 0 for v in res.get("actions", {}).values())
    ok = ("ABORT" not in res) and did_action
    if ok:
        st["log"] = [r for r in st["log"] if r.get("date") != res["date"]] + [res]
    else:
        st.setdefault("aborts", []).append(res)
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
