"""Set the eyecatch on a note.com DRAFT and leave it a draft.

Derived from set-eyecatch-republish.py (same selectors/flow for steps 1-2), with the
entire 公開に進む/無料/メンバーシップ/投稿する tail deliberately removed rather than
guarded: this script exists for the review loop, where the article must stay a draft
while a human looks at it. There is no code path here that can publish, so it cannot
be turned into a publish by an env var or a stray sentinel file — the same reason
publish-note.sh dropped its publish_article import instead of gating it.

env: NOTE_KEY (required) = article key, e.g. nffb40c8e2b90
     WORK/thumb.png      = the eyecatch image to upload
"""
import json, os, time
from cloakbrowser import launch_context

WORK = os.path.join(os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")), "note-work")
KEY = os.environ.get("NOTE_KEY", "")
if not KEY:
    raise SystemExit("FATAL: NOTE_KEY required")
thumb = WORK + "/thumb.png"
if not os.path.exists(thumb):
    raise SystemExit(f"FATAL: {thumb} not found")

ck = json.load(open(WORK + "/note-cookies.json"))
cookies = [{"name": k, "value": v, "domain": ".note.com", "path": "/"} for k, v in ck.items()]
ctx = launch_context(headless=True, humanize=False)
try:
    ctx.add_cookies(cookies)
    pg = ctx.new_page(); pg.set_viewport_size({"width": 1280, "height": 1000})
    pg.goto(f"https://editor.note.com/notes/{KEY}/edit/", wait_until="domcontentloaded", timeout=45000)
    for _ in range(20):
        if "公開に進む" in pg.evaluate("()=>document.body.innerText"): break
        time.sleep(1)
    time.sleep(2); pg.evaluate("window.scrollTo(0,0)"); time.sleep(1)

    # A crash/resume may return after the cover already autosaved. The editor then
    # removes the upload button, so prove the cover in its dedicated DOM slot and
    # continue instead of trying to upload it again. Body images have no eyecatch alt.
    existing = pg.evaluate("""()=>{const i=document.querySelector('img[alt="eyecatch"][src*="assets.st-note.com"]');return i?i.src:"";}""")
    if existing:
        print("eyecatch already present; upload skipped")
    else:
        # 1) eyecatch upload
        pg.click('button[aria-label="画像を追加"]'); time.sleep(2)
        with pg.expect_file_chooser(timeout=8000) as fc:
            pg.click("text=画像をアップロード")
        fc.value.set_files(thumb); time.sleep(4)

        # 2) click 保存 in the crop dialog
        saved = pg.evaluate("""()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.textContent||'').trim()==='保存'&&x.offsetParent!==null);if(b){b.click();return true;}return false;}""")
        print("crop 保存 clicked:", saved); time.sleep(5)

    # 3) verify the eyecatch actually landed in the editor, then stop. The editor autosaves
    #    the draft; we never touch 公開に進む.
    has_img = pg.evaluate("""()=>{const i=document.querySelector('img[alt="eyecatch"][src*="assets.st-note.com"]');return i?i.src:"";}""")
    result_line = f"EYECATCH_IN_EDITOR: {has_img[:80] if has_img else 'NONE'}"
    print(result_line)
    pg.screenshot(path=WORK + "/eyecatch-draft-set.png")
    print("SCREENSHOT:", WORK + "/eyecatch-draft-set.png")
    # Persist evidence past this run's stdout (2026-07-17, spec #70f): the STEP 5.5
    # readback instruction in article-daily.sh's PROMPT requires the agent to check this
    # line, but nothing wrote it anywhere durable -- if the agent's own transcript is
    # ever lost, there is no way to confirm after the fact whether an eyecatch actually
    # landed. One append-only line per attempt, timestamped and keyed by NOTE_KEY.
    try:
        log_path = os.path.join(
            os.environ.get(
                "WRITER_LOG_DIR",
                os.path.expanduser("~/.local/state/life-manager/writer/logs"),
            ),
            "note-eyecatch.log",
        )
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as lf:
            lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} key={KEY} {result_line}\n")
    except Exception as e:
        print(f"WARN: could not append to note-eyecatch.log: {e}")
finally:
    try: ctx.close()
    except: pass
