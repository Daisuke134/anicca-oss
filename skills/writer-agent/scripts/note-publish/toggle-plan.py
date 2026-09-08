import json, os, time
from cloakbrowser import launch_context
from publish_guard import assert_publish_allowed
WORK=os.path.join(os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")), "note-work")
ck=json.load(open(WORK+"/note-cookies.json"))
cookies=[{"name":k,"value":v,"domain":".note.com","path":"/"} for k,v in ck.items()]
assert_publish_allowed()  # top-gate (VSDD F3): sole-publish script — block before browser
ctx=launch_context(headless=True, humanize=False)
try:
    ctx.add_cookies(cookies)
    pg=ctx.new_page(); pg.set_viewport_size({"width":1280,"height":1300})
    pg.goto("https://note.com/membership/settings/manage", wait_until="domcontentloaded", timeout=45000)
    for _ in range(20):
        if "スタンダードプラン" in pg.evaluate("()=>document.body.innerText"): break
        time.sleep(1)
    time.sleep(2)
    # find the plan card's 公開 toggle: locate スタンダードプラン, then the nearest '公開' label below it within the card, click the switch left of it
    info=pg.evaluate("""()=>{
      const plan=[...document.querySelectorAll('*')].find(e=>/^スタンダードプラン$/.test((e.textContent||'').trim()));
      if(!plan) return {err:'no-plan'};
      const py=plan.getBoundingClientRect();
      // find '公開' label elements, pick the one just below the plan within 160px
      const labs=[...document.querySelectorAll('*')].filter(e=>(e.textContent||'').trim()==='公開'&&e.children.length===0);
      let pick=null,best=1e9;
      for(const l of labs){const r=l.getBoundingClientRect(); const d=r.top-py.top; if(d>-10&&d<200&&d<best){best=d;pick=r;}}
      if(!pick) return {err:'no-公開', py:py.top};
      return {x:Math.round(pick.left-22), y:Math.round(pick.top+pick.height/2)};
    }""")
    print("toggle pos:", info)
    if "x" in info:
        pg.mouse.click(info["x"], info["y"]); time.sleep(3)
        # confirm modal?
        for kw in ["公開する","申請する","はい","OK","プランを公開","公開"]:
            c=pg.evaluate(f"""()=>{{const b=[...document.querySelectorAll('button')].find(x=>(x.textContent||'').trim()==='{kw}'&&x.offsetParent!==null&&x.getBoundingClientRect().top>100); if(b){{b.click();return true;}} return false;}}""")
            if c: print("confirm:", kw); time.sleep(3); break
    pg.reload(wait_until="domcontentloaded"); time.sleep(5)
    after=pg.evaluate("()=>document.body.innerText")
    print("after — 公開中?", "公開中" in after, "| 審査", "審査" in after, "| 非公開", "非公開" in after)
    pg.screenshot(path=WORK+"/plan-toggle-after.png", full_page=True)
finally:
    try: ctx.close()
    except: pass
