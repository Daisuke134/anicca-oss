import json, os, time
from cloakbrowser import launch_context
from publish_guard import assert_publish_allowed
WORK=os.path.join(os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")), "note-work")
ck=json.load(open(WORK+"/note-cookies.json"))
cookies=[{"name":k,"value":v,"domain":".note.com","path":"/"} for k,v in ck.items()]
ctx=launch_context(headless=True, humanize=False)
try:
    ctx.add_cookies(cookies)
    pg=ctx.new_page(); pg.set_viewport_size({"width":1280,"height":1100})
    ok=False
    for attempt in range(3):
        try:
            pg.goto(f"https://editor.note.com/notes/{__import__('os').environ.get('NOTE_KEY','na3a631e63d1a')}/edit/", wait_until="domcontentloaded", timeout=45000)
            for _ in range(20):
                if "公開に進む" in pg.evaluate("()=>document.body.innerText"): ok=True; break
                time.sleep(1)
            if ok: break
        except Exception as e:
            print("load attempt", attempt, "err:", str(e)[:50]); time.sleep(3)
    print("editor loaded:", ok)
    time.sleep(2)
    for b in pg.query_selector_all("button,a"):
        if (b.text_content() or "").strip()=="公開に進む": b.click(); time.sleep(3); break
    pg.evaluate("""()=>{document.querySelectorAll('label,div,span').forEach(e=>{if((e.textContent||'').trim()==='無料'){(e.closest('label')||e).click();}});}""")
    time.sleep(1.5)
    pg.evaluate("""()=>{document.querySelectorAll('button,div,span,a').forEach(e=>{if((e.textContent||'').trim()==='メンバーシップ'){e.click();}});}""")
    time.sleep(1.5)
    pg.evaluate("""()=>{const rows=[...document.querySelectorAll('*')].filter(e=>/メンバー全員に公開/.test(e.textContent||'')&&e.children.length<6);for(const r of rows){const root=r.closest('div')?.parentElement||r.parentElement;const btn=[...(root?.querySelectorAll('button')||[])].find(b=>(b.textContent||'').trim()==='追加');if(btn){btn.click();break;}}}""")
    time.sleep(2)
    # verify membership added (追加済) before publishing
    added="追加済" in pg.evaluate("()=>document.body.innerText")
    print("membership 追加済:", added)
    # enter the 試し読みエリア overlay (where 投稿する lives + the line is preset before [6])
    for b in pg.query_selector_all("button,a"):
        if "試し読みエリアを設定" in (b.text_content() or ""): b.click(); time.sleep(3); break
    print("in preview overlay:", "設定したラインより上" in pg.evaluate("()=>document.body.innerText"))
    # ★ shared PUBLISH GUARD (F3 FIND-007): no 投稿/更新 unless explicitly enabled (env go + fresh sentinel) ★
    assert_publish_allowed()
    # click 投稿する (button or a; robust) — only reached when publish is explicitly allowed
    posted=pg.evaluate("""()=>{
      const el=[...document.querySelectorAll('button,a,div[role=button],span')].find(b=>(b.textContent||'').trim()==='投稿する' && b.offsetParent!==null);
      if(el){el.scrollIntoView({block:'center'}); el.click(); return true;} return false;}""")
    if not posted:
        try: pg.get_by_text("投稿する", exact=True).first.click(timeout=6000); posted=True
        except Exception as e: print("get_by_text 投稿 err:", str(e)[:50])
    print("投稿する clicked:", posted)
    # wait for publish confirmation
    for _ in range(12):
        u=pg.url
        if "/publish/" not in u or "公開しました" in pg.evaluate("()=>document.body.innerText"): break
        time.sleep(1)
    time.sleep(8)
    print("final url:", pg.url)
    print("text:", pg.evaluate("()=>document.body.innerText.slice(0,200)").replace(chr(10),' / '))
    pg.screenshot(path=WORK+"/published.png", full_page=False)
finally:
    try: ctx.close()
    except: pass
