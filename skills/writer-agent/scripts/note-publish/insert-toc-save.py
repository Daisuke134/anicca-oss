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
    time.sleep(2)
    # caret start of [1], Enter empty line above, ArrowUp
    pg.evaluate("""()=>{const ed=document.querySelector('.ProseMirror,[contenteditable=true]');ed.focus();const h=[...ed.querySelectorAll('h2')].find(e=>/最も賢いAIが/.test(e.textContent||''));h.scrollIntoView({block:'center'});const rg=document.createRange();rg.setStart(h.firstChild,0);rg.collapse(true);const s=window.getSelection();s.removeAllRanges();s.addRange(rg);}""")
    time.sleep(0.4); pg.keyboard.press("Enter"); time.sleep(0.4); pg.keyboard.press("ArrowUp"); time.sleep(0.8)
    coord=pg.evaluate("""()=>{const sel=window.getSelection();let n=sel.anchorNode;if(n.nodeType===3)n=n.parentElement;const blk=n.closest('p,div,h1,h2,h3')||n;const r=blk.getBoundingClientRect();return {x:Math.round(r.left),y:Math.round(r.top+r.height/2)};}""")
    pg.mouse.move(coord["x"]-40, coord["y"]); time.sleep(0.6); pg.mouse.move(coord["x"]-30, coord["y"]); time.sleep(0.6)
    # click メニューを開く gutter, then 目次
    pg.evaluate(f"""()=>{{const b=[...document.querySelectorAll('button[aria-label="メニューを開く"]')].find(x=>{{const r=x.getBoundingClientRect();return r.left<{coord['x']}&&Math.abs(r.top+r.height/2-{coord['y']})<40;}});if(b)b.click();}}"""); time.sleep(2)
    clicked=pg.evaluate("""()=>{const e=[...document.querySelectorAll('button,li,div[role=menuitem],span,p')].find(x=>(x.textContent||'').trim()==='目次'&&x.offsetParent!==null);if(e){e.click();return true;}return false;}""")
    print("目次 inserted:", clicked); time.sleep(2)
    # republish (更新する)
    for b in pg.query_selector_all("button,a"):
        if (b.text_content() or "").strip()=="公開に進む": b.click(); time.sleep(3); break
    pg.evaluate("""()=>{document.querySelectorAll('label,div,span').forEach(e=>{if((e.textContent||'').trim()==='無料'){(e.closest('label')||e).click();}});}"""); time.sleep(1)
    pg.evaluate("""()=>{document.querySelectorAll('button,div,span,a').forEach(e=>{if((e.textContent||'').trim()==='メンバーシップ'){e.click();}});}"""); time.sleep(1)
    pg.evaluate("""()=>{const rows=[...document.querySelectorAll('*')].filter(e=>/メンバー全員に公開/.test(e.textContent||'')&&e.children.length<6);for(const r of rows){const root=r.closest('div')?.parentElement||r.parentElement;const btn=[...(root?.querySelectorAll('button')||[])].find(b=>(b.textContent||'').trim()==='追加');if(btn){btn.click();break;}}}"""); time.sleep(2)
    for b in pg.query_selector_all("button,a"):
        if "試し読みエリアを設定" in (b.text_content() or ""): b.click(); time.sleep(4); break
    for _ in range(3):
        assert_publish_allowed()  # VSDD F3: no public publish unless enabled
        pg.evaluate("""()=>{const el=[...document.querySelectorAll('button,a')].find(b=>['更新する','投稿する'].includes((b.textContent||'').trim())&&b.offsetParent!==null);if(el){el.scrollIntoView({block:'center'});el.click();}}"""); time.sleep(6)
        t=pg.evaluate("()=>document.body.innerText")
        if "公開されました" in t or "更新しました" in t: print("REPUBLISHED ✓"); break
    else: print("republish clicked")
finally:
    try: ctx.close()
    except: pass
