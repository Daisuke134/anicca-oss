"""Rebuild one Note draft body from explicit source, article and asset inputs.

Mutable cookies and work files live under the shared Writer state root. This
recovery command uploads existing rendered assets; it never publishes.
"""
import sys, json, time, asyncio, os, re, html, subprocess
from pathlib import Path
SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT))
from writer_runtime_paths import note_work_dir

sys.path.insert(0, os.environ.get("NOTE_MCP_SRC", str(Path(__file__).resolve().parents[2] / "vendor/note-mcp/src")))
from note_mcp.models import Session, ArticleInput
from note_mcp.api.articles import update_article, generate_image_html
from note_mcp.api.images import upload_body_image

def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return os.path.expanduser(value)


ASSETS = required("NOTE_ASSETS")
ART = required("NOTE_SRC")
NUM = required("NOTE_NUM")
IMG_DIR = os.environ.get("NOTE_IMG_DIR", Path(ART).stem)
COOK = str(note_work_dir() / "note-cookies.json")
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
                         tags=[t for t in os.environ.get("NOTE_TAGS", "").split(",") if t]))
    print(f"REBUILT (draft) NUM={NUM}", flush=True)
asyncio.run(main())
