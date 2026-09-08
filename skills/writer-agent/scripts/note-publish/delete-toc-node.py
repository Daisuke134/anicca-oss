import json, os, time
from publish_guard import assert_publish_allowed
from cloakbrowser import launch_context
WORK=os.path.join(os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")), "note-work")
ck=json.load(open(WORK+"/note-cookies.json"))
cookies=[{"name":k,"value":v,"domain":".note.com","path":"/"} for k,v in ck.items()]
ctx=launch_context(headless=True, humanize=False)
try:
    ctx.add_cookies(cookies)
    pg=ctx.new_page(); pg.set_viewport_size({"width":1280,"height":900})
    pg.goto(f"https://editor.note.com/notes/{__import__('os').environ.get('NOTE_KEY','na3a631e63d1a')}/edit/", wait_until="domcontentloaded", timeout=45000)
    for _ in range(20):
        if "公開に進む" in pg.evaluate("()=>document.body.innerText"): break
        time.sleep(1)
    time.sleep(2)
    h2b=pg.evaluate("""()=>document.querySelector('.ProseMirror,[contenteditable=true]').querySelectorAll('h2').length""")
    box=pg.evaluate("""()=>{const el=document.querySelector('.ProseMirror table-of-contents, [contenteditable=true] table-of-contents');if(!el)return {found:false};el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();return {x:Math.round(r.left),y:Math.round(r.top+Math.min(20,r.height/2)),found:true};}""")
    print("TOC node:", box)
    if box.get("found"):
        # place caret at the very start of the manual list ("目次...") which directly follows the TOC node
        pg.evaluate("""()=>{const ed=document.querySelector('.ProseMirror,[contenteditable=true]');ed.focus();const p=[...ed.children].find(b=>(b.textContent||'').startsWith('目次')&&/最も賢いAIが/.test(b.textContent||''));const tn=p.firstChild;const rg=document.createRange();rg.setStart(tn,0);rg.collapse(true);const s=window.getSelection();s.removeAllRanges();s.addRange(rg);p.scrollIntoView({block:'center'});}"""); time.sleep(0.5)
        pg.keyboard.press("Backspace"); time.sleep(1.2)
    gone=pg.evaluate("""()=>!document.querySelector('.ProseMirror table-of-contents,[contenteditable=true] table-of-contents')""")
    manual=pg.evaluate("""()=>{const t=document.querySelector('.ProseMirror,[contenteditable=true]').innerText;return t.includes('最も賢いAIが')&&t.includes('結論');}""")
    h2a=pg.evaluate("""()=>document.querySelector('.ProseMirror,[contenteditable=true]').querySelectorAll('h2').length""")
    print(f"TOC node gone: {gone} | manual intact: {manual} | h2 {h2b}->{h2a}")
    pg.screenshot(path=WORK+"/toc-node-deleted.png")
    if gone and manual and h2a==h2b:
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
            tt=pg.evaluate("()=>document.body.innerText")
            if "公開されました" in tt or "更新しました" in tt: print("REPUBLISHED ✓"); break
        else: print("republish clicked")
    else: print("GUARD FAILED — not saved")
finally:
    try: ctx.close()
    except: pass
