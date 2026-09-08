import os,time,sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from writer_runtime_paths import note_work_dir
from playwright.sync_api import sync_playwright
W=str(note_work_dir())
DRAFT=sys.argv[1] if len(sys.argv)>1 else "https://x.com/compose/articles/edit/2070058979993010176"
MAX_H=int(sys.argv[2]) if len(sys.argv)>2 else 650   # X col=587px; a readable diagram tops out ~601px
MIN_H=110                                            # <110 = diagram squished flat → text microscopic
p=sync_playwright().start(); b=p.chromium.connect_over_cdp("http://localhost:9222"); ctx=b.contexts[0]; pg=ctx.new_page()
rc=0
try:
    pg.goto(DRAFT, wait_until="domcontentloaded", timeout=50000); time.sleep(8)
    pg.set_viewport_size({"width":760,"height":1400})
    sizes=pg.evaluate("""()=>{const c=document.querySelector('[data-testid=composer]')||document.body;return [...c.querySelectorAll('img')].map(im=>{const b=im.getBoundingClientRect();return [Math.round(b.width),Math.round(b.height)]}).filter(s=>s[1]>40)}""")
    tall=[s for s in sizes if s[1]>MAX_H]
    flat=[s for s in sizes if s[1]<MIN_H]   # too-flat = upscaled-wide diagram → text tiny/unreadable
    print("body images:",len(sizes),"| tallest:",max((h for _,h in sizes),default=0),"px | TALL>%d:"%MAX_H,tall,"| FLAT<%d:"%MIN_H,flat)
    n=0
    for frac in [0.10,0.22,0.34,0.46,0.58,0.70,0.82,0.92]:
        pg.evaluate("(f)=>window.scrollTo(0,document.body.scrollHeight*f)", frac); time.sleep(1.4)
        pg.screenshot(path=f"{W}/fv{n}.png"); n+=1
    print("sections:",n,"(AGENT: Read fv0..fv%d.png and judge tables/diagrams/size/funnel)"%(n-1))
    if tall or flat:
        if tall: print("  ✗ FAIL: oversized image(s):",tall,"→ simpler/shorter diagram (≤6-7 node vertical chain)")
        if flat: print("  ✗ FAIL: squished-flat image(s):",flat,"→ NOT a wide multi-column layout; text is microscopic")
        rc=1
    else: print("  ✓ deterministic size PASS (still Read fv*.png + judge readability)")
    pg.close()
finally:
    b=None
sys.exit(rc)
