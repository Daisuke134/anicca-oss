"""Reliable X-Articles draft: type-and-chunk insertion (text chunk -> image -> text chunk -> ...).
Guarantees image ORDER (cursor always at end; no racy block-index click). Fresh draft. NEVER publishes."""
import os,time,json,sys
from pathlib import Path
from playwright.sync_api import sync_playwright
from browser_clipboard import browser_write_html, browser_write_image
from x_anchor import build_chunks
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from writer_runtime_paths import note_work_dir
W=str(note_work_dir())
PARSED=os.environ["X_PARSED"]
d=json.load(open(PARSED)); TITLE=d["title"]; THUMB=d.get("cover_image",""); html=d["html"]
cis=sorted(d["content_images"], key=lambda c:c["block_index"])
chunks=build_chunks(html, cis)
print("chunks:",len(chunks),"images:",sum(1 for k,_ in chunks if k=='img'))

DETECT="""()=>{const t=[...document.querySelectorAll('textarea')].find(e=>{const a=((e.getAttribute('aria-label')||'')+' '+(e.getAttribute('placeholder')||'')).toLowerCase();const b=e.getBoundingClientRect();return a.includes('title')||(b.height>40&&b.width>300&&b.y<700&&b.x>900);});if(!t)return null;const b=t.getBoundingClientRect();return {x:Math.round(b.x+b.width/2),y:Math.round(b.y+b.height/2)};}"""
p=sync_playwright().start(); b=p.chromium.connect_over_cdp(os.environ.get("WRITER_CDP_URL", "http://localhost:9222")); pg=b.contexts[0].new_page()
def open_editor():
    button=pg.locator('[data-testid="empty_state_button_text"]')
    if button.count():
        button.first.click()
        return
    pg.evaluate("""()=>{const b=[...document.querySelectorAll('button,a,div[role=button],span')].find(x=>(x.textContent||'').trim()==='Write');if(b)b.click();}""")
def upwait():
    for _ in range(20):
        if not pg.evaluate("()=>/Uploading|アップロード|正在上传/.test(document.body.innerText)"): break
        time.sleep(1)
try:
    pg.goto("https://x.com/compose/articles",wait_until="domcontentloaded",timeout=50000); time.sleep(5)
    open_editor()
    pos2=None
    for i in range(30):
        pos2=pg.evaluate(DETECT)
        if pos2: break
        if i in (6, 15, 24): open_editor()
        time.sleep(1)
    if not pos2: raise SystemExit("no editor")
    pg.mouse.click(pos2['x'],pos2['y']); time.sleep(0.6); pg.keyboard.type(TITLE,delay=6); time.sleep(1.5)
    DRAFT=pg.url
    pg.query_selector('[data-testid="composer"]').click(); time.sleep(0.8)
    for k,v in chunks:
        if k=="html":
            if not v.strip(): continue
            browser_write_html(pg, v); pg.keyboard.press("Meta+v"); time.sleep(2.0)
        else:
            if not os.path.exists(v):
                raise SystemExit(f"IMAGE MISSING: {v}")
            n0=pg.evaluate("()=>document.querySelector('[data-testid=composer]').querySelectorAll('img').length")
            ok=False
            for attempt in range(3):
                browser_write_image(pg, v); time.sleep(0.4); pg.keyboard.press("Meta+v"); time.sleep(2.8); upwait()
                if pg.evaluate("()=>document.querySelector('[data-testid=composer]').querySelectorAll('img').length")>n0:
                    ok=True; break
                time.sleep(1.0)
            if not ok:
                raise SystemExit(f"IMG PASTE FAILED after retries: {v}")
    # cover
    fis=pg.query_selector_all('input[type=file]')
    if fis and THUMB and os.path.exists(THUMB):
        fis[0].set_input_files(THUMB); time.sleep(5)
        pg.evaluate("""()=>{const a=[...document.querySelectorAll('button,div[role=button],span')].find(x=>['Apply','適用','保存','Save'].includes((x.textContent||'').trim()));if(a)a.click();}"""); time.sleep(3)
    nimg=pg.evaluate("()=>document.querySelector('[data-testid=composer]').querySelectorAll('img').length")
    expected_images=sum(1 for k,_ in chunks if k=='img')
    if nimg != expected_images:
        raise SystemExit(
            f"IMAGE COUNT MISMATCH: expected={expected_images} actual={nimg}"
        )
    print("imgs in composer:",nimg)
    print("DRAFT_URL:",DRAFT)
    pg.close()
finally:
    b=None
