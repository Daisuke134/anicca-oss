"""Rebuild the note article body, restoring ALL images at their correct positions from PERSISTENT assets
(~/.cloak/note-work/automaton-assets/), with NO infographic (Dais deleted it) and NO body hero (the cover is
the note eyecatch). Real paths only — NEVER /tmp. Draft-only (update_article → draft_save). Then verify in browser.
This is the RESTORE after my keyboard-demote deleted ~25 images. Tables/figs already rendered to the assets dir;
this only UPLOADS them and places them at the markers — it does not re-render or change text."""
import sys, json, time, asyncio, os, re, html, subprocess
from pathlib import Path
sys.path.insert(0, os.environ.get("NOTE_MCP_SRC", str(Path(__file__).resolve().parents[2] / "vendor/note-mcp/src")))
from note_mcp.models import Session, ArticleInput
from note_mcp.api.articles import update_article, generate_image_html
from note_mcp.api.images import upload_body_image

ASSETS = os.path.expanduser(os.environ.get("NOTE_ASSETS", "~/.cloak/note-work/automaton-assets"))
COOK = os.path.expanduser("~/.cloak/note-work/note-cookies.json")
ART = os.environ.get("NOTE_SRC", "/Users/anicca/.cache/anicca-article-wt/docs/articles/2026-06-11-automaton-jp.md")
NUM = os.environ.get("NOTE_NUM", "166686292")
IMG_DIR = os.environ.get("NOTE_IMG_DIR", "automaton")
_SRC = os.environ.get("NOTE_SRC", "")
# same protection as stage2: the Automaton note-id / assets defaults are allowed ONLY for the genuine default run
if NUM == "166686292" and _SRC:
    raise SystemExit("NOTE_NUM defaulted to the Automaton note 166686292 but NOTE_SRC=%r is set — pass NOTE_NUM for any non-default article" % _SRC)
if "automaton-assets" in ASSETS and _SRC:
    raise SystemExit("NOTE_ASSETS defaulted to the Automaton assets but NOTE_SRC=%r is set — pass NOTE_ASSETS" % _SRC)
ck = json.load(open(COOK))

md = open(ART).read()
m = re.search(r'^#\s+(.+)$', md, re.M); title = m.group(1).strip() if m else os.path.splitext(os.path.basename(ART))[0]
body = re.sub(r'^#\s+.+$', '', md, count=1, flags=re.M)
body = re.sub(r'!\[[^\]]*\]\(images/'+re.escape(IMG_DIR)+r'/thumb\.png\)\s*', '', body)           # eyecatch is separate
body = re.sub(r'!\[[^\]]*\]\(images/'+re.escape(IMG_DIR)+r'/what-is-[^)]*\.png\)\s*', '', body)  # NO infographic
body = re.sub(r'^>\s?', '', body, flags=re.M)                                        # un-blockquote
# NOTE: do NOT convert 補足 bold → ### (keep them bold so the auto-目次 stays short — sub-points are not 小見出し)

# fund image refs → @@FUNDn@@ (in markdown order)
fund_files = []
def _fund(mo):
    fund_files.append(mo.group(1)); return f"@@FUND{len(fund_files)}@@"
body = re.sub(r'!\[[^\]]*\]\(images/'+re.escape(IMG_DIR)+r'/(fund-[^)]+\.png)\)', _fund, body)

# tables (consecutive '|' lines) → @@TBLn@@
lines = body.splitlines(); out = []; cur = []; tbl_n = 0
for l in lines:
    if l.strip().startswith('|'):
        cur.append(l)
    else:
        if cur:
            tbl_n += 1; out.append(f"@@TBL{tbl_n}@@"); cur = []
        out.append(l)
if cur:
    tbl_n += 1; out.append(f"@@TBL{tbl_n}@@")
body = "\n".join(out)
# mermaid fences → @@FIGn@@
fig_n = [0]
def _fig(mo):
    fig_n[0] += 1; return f"@@FIG{fig_n[0]}@@"
body = re.sub(r'```mermaid\n.*?```', _fig, body, flags=re.S)
print(f"markers: TBL={tbl_n} FIG={fig_n[0]} FUND={len(fund_files)}")

def dims(p):
    o = subprocess.check_output(["sips","-g","pixelWidth","-g","pixelHeight",p]).decode()
    return int(re.search(r'pixelWidth: (\d+)',o).group(1)), int(re.search(r'pixelHeight: (\d+)',o).group(1))
def compact(url, png, cw=480):
    w,h = dims(png); return generate_image_html(url, width=cw, height=max(1,round(cw*h/w)))

note_user_id = os.environ.get("NOTE_USER_ID", "").strip()
note_urlname = os.environ.get("NOTE_URLNAME", "").strip()
if not note_user_id or not note_urlname:
    raise SystemExit("NOTE_USER_ID and NOTE_URLNAME are required")
sess = Session(cookies=ck, user_id=note_user_id, username=note_urlname, created_at=int(time.time()))
async def main():
    nb = body
    for n in range(1, tbl_n+1):
        p = f"{ASSETS}/tbl{n}.png"
        img = await upload_body_image(sess, p, NUM)
        nb = nb.replace(f"@@TBL{n}@@", "\n"+compact(img.url, p)+"\n"); print(f"tbl{n}", flush=True)
    for n in range(1, fig_n[0]+1):
        p = f"{ASSETS}/fig{n}.png"
        img = await upload_body_image(sess, p, NUM)
        nb = nb.replace(f"@@FIG{n}@@", "\n"+compact(img.url, p)+"\n"); print(f"fig{n}", flush=True)
    for i, ff in enumerate(fund_files, 1):
        p = f"{ASSETS}/{ff}"
        img = await upload_body_image(sess, p, NUM)
        nb = nb.replace(f"@@FUND{i}@@", "\n"+compact(img.url, p)+"\n"); print(f"fund{i}", flush=True)
    await update_article(sess, NUM, ArticleInput(title=title, body=nb,
                         tags=[t for t in os.environ.get("NOTE_TAGS", ("AI,AIエージェント,暗号資産,Automaton,自律AI" if not _SRC else "")).split(",") if t]))
    print(f"REBUILT (draft) NUM={NUM}", flush=True)
asyncio.run(main())
