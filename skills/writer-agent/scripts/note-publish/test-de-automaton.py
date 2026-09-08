#!/usr/bin/env python3
"""VSDD oracle for A3 de-Automaton: STATIC invariants + BEHAVIORAL invariants that RUN the real
note-stage1-render.py AND note-stage2-publish.py (stage2 against a fake note_mcp recorder, FULL body) and
assert no Automaton value leaks — including the partial-override cases (NOTE_NUM set but NOTE_TAGS unset).
Exit 0=PASS, 1=FAIL. Spec: anicca/docs/superpowers/specs/2026-06-26-A3-de-automaton-note-publisher.md
Run with the Life Manager managed Writer browser Python."""
import re, sys, os, json, subprocess, tempfile, shutil
R   = str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
VC  = os.environ.get("WRITER_BROWSER_PYTHON", sys.executable)
FIX = f"{R}/note-publish/a3_fixtures"
NOTE_ENVS = ("NOTE_SRC","NOTE_WORK","NOTE_NUM","NOTE_KEY","NOTE_TAGS","NOTE_INFOG","NOTE_IMG_DIR","NOTE_ASSETS","NOTE_MCP_SRC")
s1 = open(f"{R}/note-stage1-render.py").read()
s2 = open(f"{R}/note-stage2-publish.py").read()
rb = open(f"{R}/note-publish/rebuild-note-body.py").read()
pn = open(f"{R}/publish-note.sh").read()
fails = []
def chk(cond, msg):
    if not cond: fails.append(msg)

# ---------- STATIC invariants ----------
chk("/tmp/" not in s1, "INV-3 stage1 uses /tmp/")
chk("/tmp/" not in s2, "INV-3 stage2 uses /tmp/")
chk(not any("publish_article" in l and not l.lstrip().startswith("#") for l in s2.splitlines()),
    "INV-4 stage2 calls publish_article (must stay draft-only)")
chk("update_article" in s2, "INV-4 stage2 lost update_article")
def active_aut(src):
    return [l for l in src.splitlines() if "images/automaton/" in l and "environ.get" not in l]
chk(not active_aut(s1), "INV-5 stage1 active images/automaton/: %r" % (active_aut(s1)[:1]))
chk(not active_aut(s2), "INV-5 stage2 active images/automaton/: %r" % (active_aut(s2)[:1]))
chk(not active_aut(rb), "INV-5 rebuild active images/automaton/: %r" % (active_aut(rb)[:1]))
chk("what-is-automaton-detailed.png" not in s2, "FIND-A6 stage2 still hardcodes the Automaton infographic path")
for v in ("NOTE_SRC","NOTE_WORK","NOTE_IMG_DIR"): chk(v in s1, f"stage1 missing env {v}")
for v in ("NOTE_WORK","NOTE_NUM","NOTE_KEY","NOTE_TAGS","NOTE_INFOG"): chk(v in s2, f"stage2 missing env {v}")
chk("sys.argv" in s1, "INV-1 stage1 ignores the positional arg (sys.argv)")
chk("NOTE_IMG_DIR" in rb and "NOTE_TAGS" in rb, "rebuild not de-Automatoned (NOTE_IMG_DIR/NOTE_TAGS)")
chk("if not _SRC" in rb, "rebuild tags/guards not _SRC-gated (the FIND-001 substring heuristic must not return)")

# NUM/KEY mismatch fix (team-lead E2E FAIL, draft n3ecfe7a55890): update_article must be called
# with ARTICLE_ID = KEY or NUM (key preferred when supplied), never with the bare NUM variable,
# and publish-note.sh must actually wire DRAFT_KEY through as NOTE_KEY to stage2.
chk("ARTICLE_ID" in s2 and "update_article(sess, ARTICLE_ID" in s2,
    "stage2 update_article still called with NUM directly, not KEY-preferring ARTICLE_ID")
chk('NOTE_KEY="$DRAFT_KEY"' in pn, "publish-note.sh does not pass DRAFT_KEY through as NOTE_KEY to stage2")

# ---------- helpers (real subprocess runs) ----------
def run_stage1(md_text, env_extra, work):
    d = tempfile.mkdtemp(prefix="a3test-"); mdf = os.path.join(d, "art.md"); open(mdf, "w").write(md_text)
    shutil.rmtree(work, ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if k not in NOTE_ENVS}; env["NOTE_WORK"] = work; env.update(env_extra)
    r = subprocess.run([VC, f"{R}/note-stage1-render.py", mdf], env=env, capture_output=True, text=True, timeout=150)
    mf = os.path.join(work, "note-manifest.json")
    man = json.load(open(mf)) if os.path.exists(mf) else None
    shutil.rmtree(d, ignore_errors=True)
    return r.returncode, man, r.stderr[-300:]

def _run_stage2_proc(env_extra, rec):
    if os.path.exists(rec): os.remove(rec)
    cf = os.path.expanduser("~/.cloak/note-work/note-cookies.json"); made = False
    if not os.path.exists(cf): open(cf, "w").write("{}"); made = True
    env = {k: v for k, v in os.environ.items() if k not in NOTE_ENVS}     # CLEAN env: only the explicitly-set NOTE_* below
    env["PYTHONPATH"] = FIX + os.pathsep + os.environ.get("PYTHONPATH", "")
    env["A3_REC"] = rec; env["NOTE_MCP_SRC"] = FIX; env.update(env_extra)
    r = subprocess.run([VC, f"{R}/note-stage2-publish.py"], env=env, capture_output=True, text=True, timeout=120)
    if made and os.path.exists(cf): os.remove(cf)
    return r

def run_stage2(env_extra, rec):
    r = _run_stage2_proc(env_extra, rec)
    calls = json.load(open(rec)) if os.path.exists(rec) else None
    return calls, r.returncode, r.stderr[-220:]

def run_stage2_stdout(env_extra, rec):
    # same as run_stage2 but also returns full stdout — needed to assert on the EMBED_SUMMARY
    # line the NUM/KEY mismatch test below checks for (run_stage2 only exposes a stderr tail).
    r = _run_stage2_proc(env_extra, rec)
    calls = json.load(open(rec)) if os.path.exists(rec) else None
    return calls, r.returncode, r.stdout, r.stderr[-220:]

TM = ("# Demo Agent\n\n![cover](images/demo/thumb.png)\n\n![ig](images/demo/what-is-x.png)\n\n"
      "## Section\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\ntext\n\n```mermaid\nflowchart TD\n  A-->B\n```\nmore\n")
W2 = os.path.expanduser("~/.cloak/note-work/a3-test-inv2")
# INV-2 (stage1): non-Automaton article via the POSITIONAL arg → no leak; the article's OWN infographic captured
rc, man, err = run_stage1(TM, {"NOTE_IMG_DIR": "demo", "NOTE_TAGS": "foo,bar"}, W2)
if man is None: chk(False, "INV-2 stage1 produced NO manifest (rc=%s) %s" % (rc, err))
else:
    blob = json.dumps(man, ensure_ascii=False).lower()
    chk("automaton" not in blob, "INV-2 LEAK: 'automaton' in non-Automaton stage1 manifest")
    chk("166686292" not in blob, "INV-2 LEAK: Automaton NUM in the manifest")
    chk(man["title"] == "Demo Agent", "INV-2 title not from the chosen article: %r" % man.get("title"))
    chk(len(man["tables"]) == 1 and len(man["mermaids"]) == 1,
        "INV-2 markers wrong t=%s f=%s" % (len(man["tables"]), len(man["mermaids"])))
    igs = man.get("infographics") or []
    chk(len(igs) == 1 and "demo" in igs[0] and "automaton" not in igs[0].lower(),
        "INV-2 infographic not captured as the article's own: %r" % igs)
    chk(all("a3-test-inv2" in p for p in man["tables"]), "INV-2 table PNGs not under NOTE_WORK")

# INV-2 (stage2 BEHAVIORAL, clean override): run REAL stage2 with the fake recorder → no leak in upload/update
calls, rc2, err2 = run_stage2({"NOTE_WORK": W2, "NOTE_NUM": "999000", "NOTE_SRC": "/x/demo-clean.md", "NOTE_TAGS": "foo,bar"},
                              os.path.expanduser("~/.cloak/note-work/a3-stage2-calls.json"))
if calls is None: chk(False, "FIND-A5 stage2 did not run/record (rc=%s) %s" % (rc2, err2))
else:
    cblob = json.dumps(calls, ensure_ascii=False).lower()
    chk("automaton" not in cblob, "INV-2 stage2 LEAK: Automaton value reached an upload/update call")
    chk("166686292" not in cblob, "INV-2 stage2 LEAK: Automaton NUM in a call")
    upd = [c for c in calls if c.get("call") == "update_article"]
    chk(bool(upd) and all(c["num"] == "999000" for c in upd),
        "INV-2 stage2 update targeted wrong NUM: %r" % [c.get("num") for c in upd])
    chk(all("automaton" not in c.get("path", "").lower() for c in calls if c.get("call") == "upload"),
        "INV-2 stage2 uploaded an Automaton asset")

# FIND-006: non-Automaton stage2 with NOTE_TAGS UNSET must NOT publish the Automaton tags
calls2, _, _ = run_stage2({"NOTE_WORK": W2, "NOTE_NUM": "999000", "NOTE_SRC": "/x/demo-clean.md"},  # NOTE_TAGS deliberately absent
                          os.path.expanduser("~/.cloak/note-work/a3-stage2-calls2.json"))
if calls2 is None: chk(False, "FIND-006 stage2 (tags unset) did not record")
else:
    upd2 = [c for c in calls2 if c.get("call") == "update_article"]
    chk(bool(upd2) and all(not any("Automaton" in (t or "") for t in (c.get("tags") or [])) for c in upd2),
        "FIND-001 stage2 leaked Automaton tags when NOTE_TAGS unset: %r" % [c.get("tags") for c in upd2])

# rebuild-note-body.py must ALSO guard the live Automaton note: NOTE_SRC set + NOTE_NUM unset → refuse loudly
rbenv = {k: v for k, v in os.environ.items() if k not in NOTE_ENVS}
rbenv["NOTE_SRC"] = "/x/non-automaton.md"; rbenv["NOTE_MCP_SRC"] = FIX   # NOTE_NUM deliberately unset
rr = subprocess.run([VC, f"{R}/note-publish/rebuild-note-body.py"], env=rbenv, capture_output=True, text=True, timeout=30)
chk(rr.returncode != 0 and "166686292" in (rr.stderr + rr.stdout),
    "rebuild did not refuse to default to the Automaton note for an explicit non-default source (rc=%s)" % rr.returncode)
# rebuild's SECOND guard: NOTE_SRC set + NOTE_NUM set (passes the id-guard) + NOTE_ASSETS unset → refuse the Automaton assets
rbenv2 = {k: v for k, v in os.environ.items() if k not in NOTE_ENVS}
rbenv2.update({"NOTE_SRC": "/x/non-automaton.md", "NOTE_NUM": "999000", "NOTE_MCP_SRC": FIX})
rr2 = subprocess.run([VC, f"{R}/note-publish/rebuild-note-body.py"], env=rbenv2, capture_output=True, text=True, timeout=30)
chk(rr2.returncode != 0 and "Automaton assets" in (rr2.stderr + rr2.stdout),
    "rebuild did not refuse the Automaton assets default for a non-default source (rc=%s)" % rr2.returncode)

# FIND-004: no-H1 article must not crash + must get a non-empty fallback title
rc4, man4, err4 = run_stage1("No heading, just text.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
                             {"NOTE_IMG_DIR": "demo"}, os.path.expanduser("~/.cloak/note-work/a3-test-noh1"))
chk(rc4 == 0 and man4 is not None, "FIND-004 no-H1 article crashed (rc=%s) %s" % (rc4, err4))
if man4: chk(bool(man4.get("title")), "FIND-004 no-H1 produced an empty title")

# FIND-A3: a malformed table (header+separator, no data row) must not crash
rcm, manm, errm = run_stage1("# T\n\n| H |\n|---|\n\nafter\n", {"NOTE_IMG_DIR": "demo"},
                             os.path.expanduser("~/.cloak/note-work/a3-test-malformed"))
chk(rcm == 0 and manm is not None, "FIND-A3 malformed-table article crashed (rc=%s) %s" % (rcm, errm))

# FIND-003: TWO infographics must both be captured + numbered (not collapsed)
TWO = ("# Two\n\n![a](images/demo/what-is-a.png)\n\nmid\n\n![b](images/demo/what-is-b.png)\n\nend\n")
rct, mant, errt = run_stage1(TWO, {"NOTE_IMG_DIR": "demo"}, os.path.expanduser("~/.cloak/note-work/a3-test-twoinfog"))
if mant is None: chk(False, "FIND-003 two-infographic article produced no manifest (rc=%s) %s" % (rct, errt))
else:
    chk(len(mant.get("infographics") or []) == 2, "FIND-003 two infographics collapsed: %r" % mant.get("infographics"))
    chk("@@INFOG1@@" in mant["body"] and "@@INFOG2@@" in mant["body"], "FIND-003 infographic markers not numbered")

# INV-1: DEFAULT env (no arg, no NOTE_SRC) renders the Automaton article unchanged
AUT = os.path.expanduser("~/.cache/anicca-article-wt/docs/articles/2026-06-11-automaton-jp.md")
if os.path.exists(AUT):
    W1 = os.path.expanduser("~/.cloak/note-work/a3-test-inv1"); shutil.rmtree(W1, ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if k not in NOTE_ENVS}; env["NOTE_WORK"] = W1
    subprocess.run([VC, f"{R}/note-stage1-render.py"], env=env, capture_output=True, text=True, timeout=200)
    mf = os.path.join(W1, "note-manifest.json"); man1 = json.load(open(mf)) if os.path.exists(mf) else None
    if man1 is None: chk(False, "INV-1 default produced no manifest")
    else:
        chk("Automaton" in man1["title"], "INV-1 default title regressed: %r" % man1["title"])
        chk(len(man1["tables"]) >= 10 and len(man1["mermaids"]) >= 5,
            "INV-1 default render regressed t=%s f=%s" % (len(man1["tables"]), len(man1["mermaids"])))
else:
    print("  (INV-1 default Automaton md absent on disk -> behavioral no-regression skipped)")

# --- stage2 partial-embed-failure invariants (team-lead bug: per-item try/except silently
# blanked the failed marker to "" and always exited 0, so a draft missing N images looked
# byte-for-byte identical to a fully-embedded one to run.sh's stage2_ok gate). The manifest's
# infographic path always lives under run_stage1's tempdir (deleted before stage2 runs), so it
# is never a real upload attempt here — only the table (tbl1) and mermaid (fig1) are exercised.
W2E = os.path.expanduser("~/.cloak/note-work/a3-test-stage2embed")
rc2e, man2e, err2e = run_stage1(TM, {"NOTE_IMG_DIR": "demo"}, W2E)
if man2e is None:
    chk(False, "stage2-embed setup: stage1 produced no manifest (rc=%s) %s" % (rc2e, err2e))
else:
    # positive: no injected failure -> full embed, rc 0, no failure placeholder / no raw markers left
    calls_ok, rc_ok, err_ok = run_stage2({"NOTE_WORK": W2E, "NOTE_NUM": "999100", "NOTE_SRC": "/x/embed-ok.md"},
                                          os.path.expanduser("~/.cloak/note-work/a3-stage2-embed-ok.json"))
    chk(calls_ok is not None, "stage2-embed positive: stage2 did not record (rc=%s) %s" % (rc_ok, err_ok))
    chk(rc_ok == 0, "stage2-embed positive: rc should be 0 on full embed, got %s (%s)" % (rc_ok, err_ok))
    if calls_ok:
        upd_ok = [c for c in calls_ok if c.get("call") == "update_article"]
        chk(bool(upd_ok), "stage2-embed positive: no update_article call recorded")
        body_ok = (upd_ok[0].get("body") or "") if upd_ok else ""
        chk("画像の埋め込みに失敗" not in body_ok, "stage2-embed positive: failure placeholder present with no injected failure")
        chk("@@TBL1@@" not in body_ok and "@@FIG1@@" not in body_ok, "stage2-embed positive: raw markers survived a full-success embed")

    # negative: fail the table upload only -> draft is STILL saved, rc is non-zero, the failed
    # marker is a visible placeholder (not silently blanked), and the OTHER item (fig1) still embeds
    calls_fail, rc_fail, err_fail = run_stage2(
        {"NOTE_WORK": W2E, "NOTE_NUM": "999101", "NOTE_SRC": "/x/embed-fail.md", "A3_FAIL_UPLOAD": "tbl1.png"},
        os.path.expanduser("~/.cloak/note-work/a3-stage2-embed-fail.json"))
    chk(rc_fail != 0, "stage2-embed negative: rc must be non-zero when an image embed fails (got %s)" % rc_fail)
    chk(calls_fail is not None, "stage2-embed negative: stage2 did not record at all (rc=%s) %s" % (rc_fail, err_fail))
    if calls_fail:
        upd_fail = [c for c in calls_fail if c.get("call") == "update_article"]
        chk(bool(upd_fail), "stage2-embed negative: draft was NOT saved on partial embed failure — a failure must never drop the draft")
        body_fail = (upd_fail[0].get("body") or "") if upd_fail else ""
        chk("画像の埋め込みに失敗しました: tbl1" in body_fail,
            "stage2-embed negative: failed marker was not left visible (old bug: silently blanked to ''): %r" % body_fail[:200])
        chk("@@TBL1@@" not in body_fail, "stage2-embed negative: raw @@TBL1@@ marker leaked into the saved body")
        uploads_fail = [c for c in calls_fail if c.get("call") == "upload"]
        chk(not any("tbl1.png" in c.get("path", "") for c in uploads_fail),
            "stage2-embed negative: the injected-failure upload should never have recorded a successful call")
        chk(any("fig1.png" in c.get("path", "") for c in uploads_fail),
            "stage2-embed negative: the OTHER item (fig1) should still have embedded despite tbl1 failing")

# --- NUM/KEY mismatch (team-lead real E2E FAIL, draft n3ecfe7a55890, 2026-07-15): note-mcp's
# update_article raises when given a numeric article_id AND the body has an embed-worthy
# standalone URL (see note_mcp/api/articles.py update_article + get_article_via_api — verified by
# reading the source; the fake note_mcp fixture reproduces the exact condition). The fix is
# ARTICLE_ID = NOTE_KEY or NOTE_NUM in note-stage2-publish.py.
TMK = ("# Demo Agent\n\n![cover](images/demo/thumb.png)\n\n## Section\n\n"
       "https://github.com/anthropics/claude-code\n\n"
       "| A | B |\n|---|---|\n| 1 | 2 |\n\ntext\n\n```mermaid\nflowchart TD\n  A-->B\n```\nmore\n")
W2K = os.path.expanduser("~/.cloak/note-work/a3-test-numkey")
rc2k, man2k, err2k = run_stage1(TMK, {"NOTE_IMG_DIR": "demo"}, W2K)
if man2k is None:
    chk(False, "numkey setup: stage1 produced no manifest (rc=%s) %s" % (rc2k, err2k))
else:
    chk("github.com" in man2k["body"], "numkey setup: stage1 manifest body lost the standalone embed URL")

    # positive: NOTE_KEY supplied alongside NOTE_NUM -> update_article must be called with the KEY
    # (not the numeric NUM), and the run must succeed (rc 0, full embed).
    calls_key, rc_key, err_key = run_stage2(
        {"NOTE_WORK": W2K, "NOTE_NUM": "999200", "NOTE_KEY": "ntestfakekey0001", "NOTE_SRC": "/x/numkey-key.md"},
        os.path.expanduser("~/.cloak/note-work/a3-stage2-numkey-key.json"))
    chk(rc_key == 0, "numkey positive: rc should be 0 when NOTE_KEY is supplied (got %s) %s" % (rc_key, err_key))
    if calls_key:
        upd_key = [c for c in calls_key if c.get("call") == "update_article"]
        chk(bool(upd_key) and all(c["num"] == "ntestfakekey0001" for c in upd_key),
            "numkey positive: update_article was not called with NOTE_KEY: %r" % [c.get("num") for c in upd_key])
    else:
        chk(False, "numkey positive: stage2 did not record (rc=%s) %s" % (rc_key, err_key))

    # negative (regression guard for the real note-mcp bug): NUM-only, no NOTE_KEY, body has an
    # embed URL -> update_article must fail, AND stage2 must still print EMBED_SUMMARY
    # embedded=0/total on stdout instead of dying with a bare traceback and nothing on stdout —
    # that silent-death was exactly what made publish-note.sh's stage2_embedded= come out empty
    # in the real E2E failure.
    calls_numonly, rc_numonly, stdout_numonly, err_numonly = run_stage2_stdout(
        {"NOTE_WORK": W2K, "NOTE_NUM": "999201", "NOTE_SRC": "/x/numkey-numonly.md"},
        os.path.expanduser("~/.cloak/note-work/a3-stage2-numkey-numonly.json"))
    chk(rc_numonly != 0,
        "numkey negative: rc must be non-zero when update_article gets a numeric ID + embed body (got %s) %s" % (rc_numonly, err_numonly))
    chk("EMBED_SUMMARY embedded=0/" in stdout_numonly,
        "numkey negative: EMBED_SUMMARY embedded=0/N must still print on update_article failure (old bug: stdout was empty): %r" % stdout_numonly[-200:])
    chk(not calls_numonly or not any(c.get("call") == "update_article" for c in calls_numonly),
        "numkey negative: the fake update_article recorded a call despite raising — fixture invariant broken")


# --- draft idempotency (team-lead task, 2026-07-16): publish-note.sh must not create a fresh
# note.com draft every re-run of the same markdown file — it must reuse the previous draft via
# update_article(). The decision logic (ledger lookup / --key override / --new-draft escape
# hatch / key-format safety check) lives in note-draft-ledger.py, which is pure local file I/O
# (no network, no note_mcp import) and so is exercised directly here via real subprocess runs —
# same "run it for real, don't mock" spirit as the stage1/stage2 behavioral checks above.
LEDGER_PY = f"{R}/note-draft-ledger.py"
chk(os.path.exists(LEDGER_PY), "note-draft-ledger.py missing")

# static: publish-note.sh must actually be wired to the ledger, must pass the resolved decision
# into the create_draft/update_article heredoc, must record the outcome afterward, and the
# DRAFT-ONLY invariant must still hold (update_article is now imported — that's expected and
# required for idempotency — but publish_article must still never appear active).
chk("note-draft-ledger.py" in pn, "publish-note.sh not wired to note-draft-ledger.py")
chk("resolve --md" in pn, "publish-note.sh missing the ledger resolve call")
chk("record --md" in pn, "publish-note.sh missing the ledger record call (next run would never reuse this draft)")
chk("--key)" in pn and "--new-draft)" in pn, "publish-note.sh missing --key/--new-draft CLI wiring")
chk("from note_mcp.api.articles import create_draft, get_article_via_api, update_article" in pn,
    "publish-note.sh heredoc missing get_article_via_api/update_article imports for the idempotent-update path")
chk("ArticleStatus.DRAFT" in pn, "publish-note.sh missing the live published-article safety check before update_article")
chk("key_format_ok" in pn, "publish-note.sh missing the key-format safety gate before trusting a ledger article_id")
chk(not any("publish_article" in l and not l.lstrip().startswith("#") for l in pn.splitlines()),
    "publish-note.sh calls publish_article (must stay draft-only even with idempotency added)")
chk("DRAFT_REUSED" in pn and "reused=" in pn, "publish-note.sh missing the reused= stdout token (run.sh contract)")

# static: run.sh must parse reused= the same way it already parses stage1_ok=/stage2_ok= and
# feed it into meta.json (team-lead instruction: "同じ流儀で" — same convention).
rs = open(f"{R}/run.sh").read()
chk("NOTE_REUSED" in rs and "reused=[a-z]+" in rs, "run.sh does not parse the reused= token from publish-note.sh's stdout")
chk("draft_reused" in rs, "run.sh does not feed reused= into meta.json (draft_reused key)")

def _mktmp_ledger_env():
    d = tempfile.mkdtemp(prefix="a3-ledger-")
    return d, os.path.join(d, "draft-ledger.json")

def _resolve(ledger, md, extra_args=()):
    r = subprocess.run([VC, LEDGER_PY, "--ledger", ledger, "resolve", "--md", md, *extra_args],
                        capture_output=True, text=True, timeout=20)
    return r.returncode, (json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None), r.stderr

def _record(ledger, md, key, num="", title=""):
    r = subprocess.run([VC, LEDGER_PY, "--ledger", ledger, "record", "--md", md, "--key", key,
                         "--num", num, "--title", title], capture_output=True, text=True, timeout=20)
    return r.returncode, (json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None), r.stderr

# positive/negative 1: no ledger file yet -> action=create (this is what makes publish-note.sh
# call create_draft, not update_article, on the very first run of a new article)
LD1, LF1 = _mktmp_ledger_env()
md1 = os.path.join(LD1, "article.md"); open(md1, "w").write("# T\n")
rc, res, err = _resolve(LF1, md1)
chk(rc == 0 and res is not None, "idempotency: resolve (no ledger) crashed rc=%s %s" % (rc, err))
if res: chk(res["action"] == "create" and res["article_id"] is None, "idempotency: no-ledger resolve should be create, got %r" % res)

# positive/negative 2: same MD re-run after a record() -> action=update with the stored key
# (this is the actual "same article re-run reuses the draft instead of creating a new one" case)
rc, res, err = _record(LF1, md1, key="ntestkey000001", num="555001", title="T")
chk(rc == 0 and res is not None and res.get("key") == "ntestkey000001", "idempotency: record failed rc=%s %s" % (rc, err))
rc, res, err = _resolve(LF1, md1)
chk(rc == 0 and res is not None, "idempotency: resolve (after record) crashed rc=%s %s" % (rc, err))
if res:
    chk(res["action"] == "update" and res["article_id"] == "ntestkey000001" and res["key_format_ok"] is True,
        "idempotency: post-record resolve should reuse the recorded key, got %r" % res)

# positive 3: --key explicit overrides whatever the ledger says
rc, res, err = _resolve(LF1, md1, extra_args=["--key", "nexplicitoverride1"])
chk(rc == 0 and res is not None and res["action"] == "update" and res["article_id"] == "nexplicitoverride1",
    "idempotency: explicit --key did not override the ledger: %r (rc=%s %s)" % (res, rc, err))

# positive 4: --new-draft escape hatch ignores both --key and the ledger
rc, res, err = _resolve(LF1, md1, extra_args=["--key", "nexplicitoverride1", "--new-draft"])
chk(rc == 0 and res is not None and res["action"] == "create" and res["article_id"] is None,
    "idempotency: --new-draft escape hatch did not force create: %r (rc=%s %s)" % (res, rc, err))

# safety: a numeric-only ledger key must resolve with key_format_ok=false, so publish-note.sh's
# heredoc refuses to call update_article on it (get_article_via_api rejects numeric ids anyway —
# this is the LOCAL half of that same safety net, verified against the real note_mcp source in
# the NUM/KEY mismatch tests above).
rc, res, err = _record(LF1, md1, key="555001", num="555001", title="T")
chk(rc == 0, "idempotency: record with numeric key failed rc=%s %s" % (rc, err))
rc, res, err = _resolve(LF1, md1)
if res: chk(res["key_format_ok"] is False, "idempotency: numeric-only key must be key_format_ok=false, got %r" % res)

# a corrupt ledger file must never crash resolve — it must fall back to create (same
# fail-safe-to-old-behavior contract as publish-note.sh's own WARN-and-fallback wiring above)
open(LF1, "w").write("{not valid json")
rc, res, err = _resolve(LF1, md1)
chk(rc == 0 and res is not None and res["action"] == "create", "idempotency: corrupt ledger did not fail open to create: rc=%s res=%r err=%s" % (rc, res, err))

# atomic write: the ledger must be a single well-formed JSON file after N concurrent record()
# calls for DIFFERENT files -- an os.replace()-based writer never leaves a torn/partial file for
# a concurrent reader to observe (this reproduces the exact hazard the cookie-write code at
# publish-note.sh:247-254 already guards against, applied to the ledger).
LD2, LF2 = _mktmp_ledger_env()
mds = []
for i in range(8):
    p = os.path.join(LD2, f"art{i}.md"); open(p, "w").write(f"# T{i}\n"); mds.append(p)
import concurrent.futures
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(lambda i: _record(LF2, mds[i], key=f"nconc{i:04d}key", num=str(i), title=f"T{i}"), range(8)))
chk(all(rc == 0 for rc, _, _ in results), "idempotency: concurrent record() calls did not all succeed: %r" % [rc for rc, _, _ in results])
try:
    final_ledger = json.load(open(LF2))
    chk(len(final_ledger) == 8, "idempotency: concurrent writes lost entries (torn-file race) — expected 8, got %d" % len(final_ledger))
except json.JSONDecodeError as e:
    chk(False, "idempotency: ledger is not valid JSON after concurrent writes (non-atomic write) — %s" % e)
shutil.rmtree(LD1, ignore_errors=True); shutil.rmtree(LD2, ignore_errors=True)

if fails:
    print("FAIL (%d):" % len(fails))
    for f in fails: print("  -", f)
    sys.exit(1)
print("PASS -- all static + behavioral invariants hold (stage1 + stage2 leak-checked, partial-override + multi-infographic + partial-embed-failure + draft idempotency)")
sys.exit(0)
