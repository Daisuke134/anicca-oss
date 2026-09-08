import json, os, time
from publish_guard import assert_publish_allowed
from cloakbrowser import launch_context
WORK=os.path.join(os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")), "note-work")
ck=json.load(open(WORK+"/note-cookies.json"))
cookies=[{"name":k,"value":v,"domain":".note.com","path":"/"} for k,v in ck.items()]
ctx=launch_context(headless=True, humanize=False)
try:
    ctx.add_cookies(cookies)
    pg=ctx.new_page(); pg.set_viewport_size({"width":1280,"height":1000})
    pg.goto(f"https://editor.note.com/notes/{__import__('os').environ.get('NOTE_KEY','na3a631e63d1a')}/edit/", wait_until="domcontentloaded", timeout=45000)
    for _ in range(20):
        if "公開に進む" in pg.evaluate("()=>document.body.innerText"): break
        time.sleep(1)
    time.sleep(2); pg.evaluate("window.scrollTo(0,0)"); time.sleep(1)
    # 1) eyecatch upload
    pg.click('button[aria-label="画像を追加"]'); time.sleep(2)
    with pg.expect_file_chooser(timeout=8000) as fc:
        pg.click("text=画像をアップロード")
    fc.value.set_files(WORK+"/thumb.png"); time.sleep(4)
    # 2) click 保存 in crop dialog
    saved=pg.evaluate("""()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.textContent||'').trim()==='保存'&&x.offsetParent!==null);if(b){b.click();return true;}return false;}""")
    print("crop 保存 clicked:", saved); time.sleep(4)
    # 3) 公開に進む
    for b in pg.query_selector_all("button,a"):
        if (b.text_content() or "").strip()=="公開に進む": b.click(); time.sleep(3); break
    # 4) ensure 無料 + membership
    pg.evaluate("""()=>{document.querySelectorAll('label,div,span').forEach(e=>{if((e.textContent||'').trim()==='無料'){(e.closest('label')||e).click();}});}"""); time.sleep(1.2)
    pg.evaluate("""()=>{document.querySelectorAll('button,div,span,a').forEach(e=>{if((e.textContent||'').trim()==='メンバーシップ'){e.click();}});}"""); time.sleep(1.2)
    added_now=pg.evaluate("""()=>{const rows=[...document.querySelectorAll('*')].filter(e=>/メンバー全員に公開/.test(e.textContent||'')&&e.children.length<6);for(const r of rows){const root=r.closest('div')?.parentElement||r.parentElement;const btn=[...(root?.querySelectorAll('button')||[])].find(b=>(b.textContent||'').trim()==='追加');if(btn){btn.click();return true;}}return false;}"""); time.sleep(2)
    print("membership 追加済:", "追加済" in pg.evaluate("()=>document.body.innerText"))
    # 5) 試し読みエリア overlay + 投稿する
    for b in pg.query_selector_all("button,a"):
        if "試し読みエリアを設定" in (b.text_content() or ""): b.click(); time.sleep(3); break
    assert_publish_allowed()  # VSDD F3: no public publish unless enabled
    posted=pg.evaluate("""()=>{const el=[...document.querySelectorAll('button,a')].find(b=>(b.textContent||'').trim()==='投稿する'&&b.offsetParent!==null);if(el){el.click();return true;}return false;}""")
    assert_publish_allowed()  # VSDD F3: no public publish unless enabled
    print("投稿する clicked:", posted); time.sleep(8)
    print("published modal:", "公開されました" in pg.evaluate("()=>document.body.innerText"))
    pg.screenshot(path=WORK+"/eyecatch-republished.png")
finally:
    try: ctx.close()
    except: pass
