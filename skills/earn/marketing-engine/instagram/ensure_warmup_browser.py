#!/usr/bin/env python3
"""ensure_warmup_browser.py — make a DEDICATED anti-throttle CloakBrowser instance ready+logged-in for an
account, WITHOUT touching Dais's daily-driver (:9222). Prints the logged-in tab's TID on stdout (last line).

WHY: the daily-driver runs WITHOUT occlusion/throttle flags, so an occluded reels tab has visibilityState
"hidden" → Chrome pauses the <video> → warmup can't watch reels. A second instance of the SAME stealth
Chromium, launched with --disable-features=CalculateNativeWinOcclusion --disable-backgrounding-occluded-windows
--autoplay-policy=no-user-gesture-required, keeps the page "visible" even when occluded → reels really play.
Proven 2026-06-29 on @money_blueprintdaily (6/6 distinct reels, currentTime advancing).

Idempotent: if the instance is already up + the account is already logged in, it just returns the tid.
Usage: ensure_warmup_browser.py --handle <h> --port <p> --profile <dir> --creds <ig-*.json>
"""
import argparse, glob, json, os, re, subprocess, sys, time
from pathlib import Path

CDP = str(Path(__file__).resolve().parents[3] / "browser/scripts/cdp.py")
PYB = sys.executable


def gmail_env():
    return dict(os.environ)


def base_gmail_account(email):
    # plus-addressed signup inboxes (user+tag@gmail.com) deliver to the base inbox
    local, _, domain = email.partition("@")
    return f"{local.split('+')[0]}@{domain}"


def fetch_verify_code(gmail_account, target_email, timeout=150, poll=8):
    """Poll Gmail for IG's post-login verification code and return the 6-digit code.

    The base inbox (gmail_account) receives OTP mail for EVERY plus-addressed IG account
    across all concurrent loops, and the search index's snippet/preview fields never carry
    the code (it's only in the message body) — so this must (1) fetch each candidate
    THREAD BODY, not just search metadata, and (2) filter to messages whose To: header is
    the exact target_email AND that arrived after this call started, or it will silently
    grab a stale/wrong-account code and the login will fail with no diagnostic.
    """
    start = time.time()
    deadline = start + timeout
    queries = ['in:anywhere subject:"Verify your profile" newer_than:1h',
               'in:anywhere subject:"is your Instagram code" newer_than:1h']
    seen_threads = set()
    while time.time() < deadline:
        for q in queries:
            r = subprocess.run(["gog", "gmail", "search", "--account", gmail_account, "--json", "--limit", "5", q],
                                capture_output=True, text=True, env=gmail_env())
            try:
                d = json.loads(r.stdout or "{}")
            except Exception:
                continue
            for m in (d.get("threads") or d.get("messages") or []):
                tid = m.get("id")
                if not tid or tid in seen_threads:
                    continue
                tr = subprocess.run(["gog", "gmail", "thread", "get", "--account", gmail_account, "--json", tid],
                                     capture_output=True, text=True, env=gmail_env())
                try:
                    td = json.loads(tr.stdout or "{}")
                except Exception:
                    continue
                for msg in (td.get("thread", {}).get("messages") or []):
                    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                    if headers.get("To", "").strip().lower() != target_email.strip().lower():
                        continue
                    try:
                        import base64
                        internal_ms = int(msg.get("internalDate", "0"))
                        if internal_ms and internal_ms / 1000 < start - 30:
                            continue  # stale code from a prior challenge, not this attempt
                        body = base64.urlsafe_b64decode(msg["payload"]["body"]["data"] + "==").decode("utf-8", "ignore")
                    except Exception:
                        continue
                    found = re.search(r"(?<!\d)(\d{6})(?!\d)", body)
                    if found:
                        return found.group(1)
                seen_threads.add(tid)
        time.sleep(poll)
    return None


def chromium_bin():
    c = sorted(glob.glob(os.path.expanduser("~/.cloakbrowser/chromium-*/Chromium.app/Contents/MacOS/Chromium")))
    return c[-1] if c else None


def cdp(port, *a):
    env = {**os.environ, "CDP_PORT": str(port)}
    return subprocess.run([PYB, CDP, *a], capture_output=True, text=True, env=env).stdout.strip()


def ev(port, tid, js):
    open("/tmp/_ewb.js", "w").write(js)
    o = cdp(port, "eval", tid, "/tmp/_ewb.js")
    v = o
    for _ in range(2):
        if isinstance(v, str):
            try: v = json.loads(v)
            except Exception: break
    return v


def cdp_up(port):
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=4).read()
        return True
    except Exception:
        return False


def active_account(port, tid):
    return ev(port, tid, "(()=>{const a=document.querySelector('a[href^=\"/\"] img[alt$=\"のプロフィール写真\"]');"
                         "return a?((a.getAttribute('alt').match(/^(.+?)のプロフィール/)||[])[1]||''):''})()")


def login(port, tid, creds):
    cdp(port, "nav", tid, "https://www.instagram.com/accounts/login/"); time.sleep(7)
    js = """(()=>{function setv(el,val){const d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;d.call(el,'');el.dispatchEvent(new Event('input',{bubbles:true}));d.call(el,val);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}
      const u=document.querySelector('input[name=email]')||document.querySelector('input[name=username]');
      const p=document.querySelector('input[name=pass]')||document.querySelector('input[name=password]');
      if(!u||!p)return 'no-fields'; setv(u,%s); setv(p,%s); return 'filled';})()""" % (
        json.dumps(creds["username"]), json.dumps(creds["pw"]))
    if ev(port, tid, js) != "filled":
        return False
    time.sleep(1)
    btn = ev(port, tid, "(()=>{const b=[...document.querySelectorAll('button,div[role=button]')].find(x=>(x.textContent||'').trim()==='ログイン'&&x.getBoundingClientRect().height>0);if(!b)return null;const r=b.getBoundingClientRect();return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)})})()")
    if isinstance(btn, str):
        try: btn = json.loads(btn)
        except Exception: btn = None
    if isinstance(btn, dict):
        cdp(port, "clickxy", tid, str(btn["x"]), str(btn["y"]))
    time.sleep(6)

    # IG sometimes challenges a new-device login with an emailed verification code
    # (either the "Verify your profile" or "is your Instagram code" template).
    url = ev(port, tid, "location.href")
    if isinstance(url, str) and ("codeentry" in url or "challenge" in url):
        code = fetch_verify_code(base_gmail_account(creds["email"]), creds["email"])
        if code:
            fill_js = ("(()=>{function setv(el,val){const d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                       "d.call(el,'');el.dispatchEvent(new Event('input',{bubbles:true}));d.call(el,val);el.dispatchEvent(new Event('input',{bubbles:true}));"
                       "el.dispatchEvent(new Event('change',{bubbles:true}));}"
                       "const inp=document.querySelector('input[name=email]')||document.querySelector('input[type=text]');"
                       "if(!inp)return 'no-field'; setv(inp,%s); return inp.value;})()" % json.dumps(code))
            ev(port, tid, fill_js)
            time.sleep(1)
            next_btn = ev(port, tid, "(()=>{const b=[...document.querySelectorAll('button,div[role=button]')].find(x=>/^(次へ|Next|Confirm|確認)$/.test((x.textContent||'').trim())&&x.getBoundingClientRect().height>0);if(!b)return null;const r=b.getBoundingClientRect();return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)})})()")
            if isinstance(next_btn, str):
                try: next_btn = json.loads(next_btn)
                except Exception: next_btn = None
            if isinstance(next_btn, dict):
                cdp(port, "clickxy", tid, str(next_btn["x"]), str(next_btn["y"]))
            time.sleep(6)
    # dismiss the onetap "save login info" prompt if shown
    save = ev(port, tid, "(()=>{const b=[...document.querySelectorAll('button,div[role=button]')].find(x=>/(情報を保存|Save info)/.test((x.textContent||'').trim())&&x.getBoundingClientRect().height>0);if(!b)return null;const r=b.getBoundingClientRect();return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)})})()")
    if isinstance(save, str):
        try: save = json.loads(save)
        except Exception: save = None
    if isinstance(save, dict):
        cdp(port, "clickxy", tid, str(save["x"]), str(save["y"])); time.sleep(4)
    cdp(port, "nav", tid, "https://www.instagram.com/"); time.sleep(5)
    return active_account(port, tid) == creds["username"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--creds", required=True)
    a = ap.parse_args()
    creds = json.load(open(os.path.expanduser(a.creds)))
    prof = os.path.expanduser(a.profile)

    if not cdp_up(a.port):
        ch = chromium_bin()
        if not ch:
            print("ERROR: no cloakbrowser chromium binary found"); return
        subprocess.Popen([ch, f"--remote-debugging-port={a.port}", f"--user-data-dir={prof}",
                          "--no-first-run", "--no-default-browser-check",
                          "--disable-features=CalculateNativeWinOcclusion",
                          "--disable-backgrounding-occluded-windows",
                          "--autoplay-policy=no-user-gesture-required", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(2)
            if cdp_up(a.port): break

    # reuse an existing instagram tab if logged in, else open one
    tid = cdp(a.port, "new", "https://www.instagram.com/"); time.sleep(6)
    if active_account(a.port, tid) != creds["username"]:
        if not login(a.port, tid, creds):
            print("ERROR: login failed (challenge/OTP?)"); return
    print(tid)   # last line = the logged-in tab tid


if __name__ == "__main__":
    main()
