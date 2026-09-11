#!/usr/bin/env python3
"""warmer.py — deterministic WARM step for clip_pass.sh (bookkeeping + subprocess dispatch
only, no LLM judgment; per building-agents this is mechanical, not a place for model judgment).

Per pass, picks the SINGLE OLDEST warming account (earliest started_warming -- backfilled from
its own ~/.cloak/ig-warmup-<handle>.json log history the first time it's seen) and, serially:
  1. Ensures that ONE account's isolated CloakBrowser is up + logged in via
     ig-account-warmer's ensure_warmup_browser.py (idempotent launcher: starts the browser on
     the account's OWN port/profile if it's down, logs in with ~/.cloak/ig-<handle>.json creds,
     no-ops if already up+logged in). Needs creds + a profile dir -- WARNs and skips the whole
     account this pass if either is missing (never guesses a profile/port).
  2. Runs warm.py once (CDP_PORT=<port>) if the browser ended up reachable -- warm.py itself is
     idempotent (no-ops if already warmed today).
  3. Counts account age from started_warming with creation day == day1.
  4. Reads the account's warmup state back and decides:
       - a ban signal anywhere in the log/aborts history -> status: investigating + WARN.
       - day1-2 -> keep status: warming and session_owner: browser.
       - day >= 3 -> establish the golden instagrapi session once: password login, timeline feed
         probe, settings dump, then status: ready and session_owner: instagrapi.
       - a saved/attempted session is never password-relogged. Dead session -> session_failed.
Only ONE account is touched per pass (deliberately serial -- keeps resource use minimal and
each promotion decision individually auditable) and only accounts with status starting "warming" are
ever touched -- a live "ready" posting account's browser (e.g. the daily-driver-adjacent
aiclipsvault instance) is never started/stopped/touched by this script.

Safety: clip-accounts.json edits are backup -> in-place (edit only the touched account object,
never drop/reorder/duplicate rows) -> row-count-verified before AND after the write.

Usage: warmer.py [path-to-clip-accounts.json]   (defaults to ~/.cloak/clip-accounts.json)
"""
import sys, os, json, subprocess, shutil, datetime, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENSURE_PY = os.path.join(HERE, "instagram", "ensure_warmup_browser.py")
WARM_PY = os.path.join(HERE, "instagram", "warm.py")
PY = sys.executable
PROMOTE_DAY = 3


def warming_day(started_warming, today=None):
    """Creation date is day1; invalid or future dates are not promotable."""
    today = today or datetime.date.today()
    try:
        started = datetime.date.fromisoformat(started_warming)
    except (TypeError, ValueError):
        return 0
    elapsed = (today - started).days
    return elapsed + 1 if elapsed >= 0 else 0


def establish_golden_session(handle, home=None, client_factory=None, warming_day_value=None):
    """Create the one password-backed session, or verify saved settings without relogin."""
    if warming_day_value is not None and warming_day_value < PROMOTE_DAY:
        return {"ok": False, "terminal": False, "login_performed": False,
                "refused": "warming_day<3",
                "error": f"instagrapi login refused: warming_day={warming_day_value} < {PROMOTE_DAY}"}
    home = home or os.path.expanduser("~")
    cloak_dir = os.path.join(home, ".cloak")
    settings = os.path.join(cloak_dir, f"instagrapi-{handle}.json")
    attempt = os.path.join(cloak_dir, f".golden-session-attempted-{handle}")
    creds_path = os.path.join(cloak_dir, f"ig-{handle}.json")

    if client_factory is None:
        try:
            from instagrapi import Client
        except Exception as e:
            return {"ok": False, "terminal": False, "error": f"instagrapi unavailable: {type(e).__name__}"}
        client_factory = Client

    if os.path.exists(settings):
        try:
            cl = client_factory()
            cl.delay_range = [1, 3]
            cl.load_settings(settings)
            feed = cl.get_timeline_feed()
            if feed is None:
                raise RuntimeError("timeline feed returned no data")
            cl.dump_settings(settings)
            return {"ok": True, "terminal": False, "login_performed": False, "settings_path": settings}
        except Exception as e:
            return {
                "ok": False,
                "terminal": True,
                "login_performed": False,
                "error": f"saved session dead; refusing relogin: {type(e).__name__}: {str(e)[:160]}",
            }

    if os.path.exists(attempt):
        return {
            "ok": False,
            "terminal": True,
            "login_performed": False,
            "error": "golden login already attempted; refusing relogin",
        }
    if not os.path.exists(creds_path):
        return {"ok": False, "terminal": False, "login_performed": False, "error": "signup credentials missing"}

    try:
        creds = json.load(open(creds_path))
        username, password = creds["username"], creds["pw"]
        cl = client_factory()
        cl.delay_range = [1, 3]
    except Exception as e:
        return {"ok": False, "terminal": False, "login_performed": False, "error": f"cannot prepare login: {type(e).__name__}"}

    os.makedirs(cloak_dir, exist_ok=True)
    try:
        fd = os.open(attempt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {
            "ok": False,
            "terminal": True,
            "login_performed": False,
            "error": "golden login already attempted; refusing relogin",
        }
    with os.fdopen(fd, "w") as f:
        f.write(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}\n")

    try:
        cl.login(username, password)
        feed = cl.get_timeline_feed()
        if feed is None:
            raise RuntimeError("timeline feed returned no data")
        cl.dump_settings(settings)
        if not os.path.exists(settings):
            raise RuntimeError("settings dump missing")
        return {"ok": True, "terminal": False, "login_performed": True, "settings_path": settings}
    except Exception as e:
        return {
            "ok": False,
            "terminal": True,
            "login_performed": True,
            "error": f"golden session failed; never retry: {type(e).__name__}: {str(e)[:160]}",
        }


def load_warmup_state(handle):
    p = os.path.expanduser(f"~/.cloak/ig-warmup-{handle}.json")
    try:
        return json.load(open(p))
    except Exception:
        return {"handle": handle, "log": []}


def state_summary(st):
    """day = the last successful run's own recorded day (honest: what was actually reached,
    not a recomputed calendar guess). banned = any log/abort entry ever showed a ban signal."""
    log = st.get("log", [])
    aborts = st.get("aborts", [])
    day = log[-1].get("day", 0) if log else 0
    banned = any(
        e.get("ban") is True or e.get("ban_signal")
        or (isinstance(e.get("ABORT"), str) and "ban" in e["ABORT"].lower())
        for e in (log + aborts)
    )
    dates = sorted(r.get("date") for r in log if r.get("date"))
    return day, banned, (dates[0] if dates else None)


def recent_abort_after_last_log(st):
    """True if the most recent aborts entry (a login failure) is dated AFTER the most
    recent log entry (a successful day). Guards against promoting off a stale
    log[-1].day when the account has actually failed to log in since that success --
    the real bug: state_summary()'s day/banned alone missed this because a plain
    'not logged in' abort never sets a ban signal. Compares by 'date' (fallback to
    'day' if a date is missing on either side)."""
    log = st.get("log", [])
    aborts = st.get("aborts", [])
    if not aborts:
        return False
    if not log:
        return True  # only failures ever recorded, nothing successful to promote on
    last_log, last_abort = log[-1], aborts[-1]
    log_date, abort_date = last_log.get("date"), last_abort.get("date")
    if log_date and abort_date:
        return abort_date > log_date
    return last_abort.get("day", 0) > last_log.get("day", 0)


def browser_up(port, timeout=4):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json/list", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def append_warmlog(handle, entry):
    p = os.path.expanduser(f"~/.cloak/warmlog-{handle}.jsonl")
    entry = dict(entry, ts=int(time.time()), date=datetime.date.today().isoformat())
    with open(p, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def pick_target(accts):
    """The single oldest warming account this pass. started_warming is backfilled (once) from
    the account's own log history when missing, then the earliest started_warming wins; a tie
    falls back to list order (deterministic, no randomness)."""
    warming = [a for a in accts if str(a.get("status") or "").lower().startswith("warming")]
    changed = False
    for a in warming:
        if not a.get("started_warming"):
            _, _, first_date = state_summary(load_warmup_state(a.get("handle")))
            a["started_warming"] = first_date or datetime.date.today().isoformat()
            changed = True
            print(f"WARM {a.get('handle')}: recorded started_warming={a['started_warming']}")
    warming.sort(key=lambda a: a["started_warming"])
    return (warming[0] if warming else None), changed


def _write(accts_path, accts, n_before):
    n_after = len(accts)
    if n_after != n_before:
        print(f"WARM: ABORT -- in-memory row count changed {n_before}->{n_after}, not writing")
        return 1
    bak = accts_path + f".bak-{int(time.time())}"
    shutil.copyfile(accts_path, bak)
    tmp = accts_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(accts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, accts_path)
    written = json.load(open(accts_path))
    if len(written) != n_before:
        print(f"WARM: POST-WRITE row count mismatch ({len(written)} != {n_before}) -- restoring backup")
        shutil.copyfile(bak, accts_path)
        return 1
    print(f"WARM: wrote {accts_path} ({len(written)} rows, backup={bak})")
    return 0


def main():
    accts_path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.cloak/clip-accounts.json")
    try:
        accts = json.load(open(accts_path))
    except Exception as e:
        print(f"WARM: cannot read {accts_path}: {e}")
        return 1
    n_before = len(accts)

    target, changed = pick_target(accts)
    if not target:
        print("WARM: no warming accounts this pass")
        return _write(accts_path, accts, n_before) if changed else 0

    handle, port, profile = target["handle"], target.get("port"), target.get("profile")
    account_day = warming_day(target.get("started_warming"))
    print(
        f"WARM: target this pass = {handle} "
        f"(started_warming={target.get('started_warming')}, account_day={account_day})"
    )

    if not browser_up(port):
        creds = os.path.expanduser(f"~/.cloak/ig-{handle}.json")
        if not os.path.exists(creds) or not profile:
            append_warmlog(handle, {"action": "skip_no_creds_or_profile", "port": port})
            print(f"WARM {handle}: WARN browser down on :{port} and no creds/profile to launch it -- skip")
        else:
            profdir = os.path.expanduser(f"~/.cloak/profiles/{profile}")
            try:
                r = subprocess.run(
                    [PY, ENSURE_PY, "--handle", handle, "--port", str(port), "--profile", profdir, "--creds", creds],
                    capture_output=True, text=True, timeout=300,
                )
                out = (r.stdout or "").strip()
                append_warmlog(handle, {"action": "ensure_browser", "rc": r.returncode, "out": out[:300]})
                print(f"WARM {handle}: ensure_warmup_browser rc={r.returncode} out={out[:200]!r}")
            except Exception as e:
                append_warmlog(handle, {"action": "ensure_browser_error", "error": repr(e)[:200]})
                print(f"WARM {handle}: ensure_warmup_browser FAILED {e!r}")

    if browser_up(port):
        try:
            r = subprocess.run(
                [PY, WARM_PY, handle],
                env={**os.environ, "CDP_PORT": str(port)},
                capture_output=True, text=True, timeout=600,
            )
            out = (r.stdout or "").strip()
            append_warmlog(handle, {"action": "warm_run", "rc": r.returncode, "out": out[:500]})
            print(f"WARM {handle}: warm.py rc={r.returncode} out={out[:300]}")
        except Exception as e:
            append_warmlog(handle, {"action": "warm_run_error", "error": repr(e)[:200]})
            print(f"WARM {handle}: warm.py FAILED {e!r}")
    else:
        append_warmlog(handle, {"action": "skip_browser_still_down", "port": port})
        print(f"WARM {handle}: WARN browser still down on :{port} after launch attempt -- warm.py not run")

    warmup_state = load_warmup_state(handle)
    warm_log_day, banned, _ = state_summary(warmup_state)
    recent_failure = recent_abort_after_last_log(warmup_state)
    for a in accts:
        if a.get("handle") != handle:
            continue
        if banned:
            a["status"] = "investigating"
            a["note"] = (
                f"{datetime.date.today().isoformat()} warmer.py: WARN ban signal detected in "
                f"warmup log -- moved warming->investigating (account_day={account_day}, warm_log_day={warm_log_day})."
            )
            changed = True
            print(f"WARM {handle}: BAN SIGNAL -- moved to investigating")
            append_warmlog(handle, {"action": "ban_detected", "day": account_day})
        elif recent_failure:
            print(
                f"WARM {handle}: HELD (account_day={account_day}, warm_log_day={warm_log_day}; "
                "a login abort was recorded after the last successful warm log -- not promoting)"
            )
            append_warmlog(handle, {"action": "held_recent_abort", "day": account_day})
        elif account_day >= PROMOTE_DAY:
            session = establish_golden_session(handle, warming_day_value=account_day)
            if session.get("ok"):
                prior_note = (a.get("note") or "")[:300]
                a["status"] = "ready"
                a["session_owner"] = "instagrapi"
                a["note"] = (
                    f"{datetime.date.today().isoformat()} warmer.py: golden session alive; "
                    f"PROMOTED warming->ready (account_day={account_day}, login_performed="
                    f"{str(bool(session.get('login_performed'))).lower()}). prior note: {prior_note}"
                )
                changed = True
                print(
                    f"WARM {handle}: GOLDEN SESSION alive; PROMOTED to ready "
                    f"(account_day={account_day}, login_performed={session.get('login_performed')})"
                )
                append_warmlog(
                    handle,
                    {
                        "action": "golden_session_ready",
                        "day": account_day,
                        "login_performed": bool(session.get("login_performed")),
                    },
                )
            elif session.get("terminal"):
                a["status"] = "session_failed"
                a["note"] = (
                    f"{datetime.date.today().isoformat()} warmer.py: {session.get('error')}; "
                    "account discarded, password relogin forbidden."
                )
                changed = True
                print(f"WARM {handle}: SESSION FAILED terminally -- discard; never relogin")
                append_warmlog(handle, {"action": "golden_session_failed", "day": account_day})
            else:
                print(f"WARM {handle}: HELD before golden session -- {session.get('error')}")
                append_warmlog(handle, {"action": "golden_session_held", "day": account_day})
        else:
            print(f"WARM {handle}: not yet promotable (account_day={account_day} < {PROMOTE_DAY})")
        break

    return _write(accts_path, accts, n_before) if changed else (print("WARM: no changes this pass") or 0)


if __name__ == "__main__":
    sys.exit(main())
