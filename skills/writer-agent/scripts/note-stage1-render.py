import re, os, sys, json, time, html
from cloakbrowser import launch_context
# source = positional arg (orchestrator passes "$MD") OR NOTE_SRC env OR the Automaton default
ART=sys.argv[1] if len(sys.argv)>1 else os.environ.get("NOTE_SRC","")
if not ART: raise SystemExit("article path argument or NOTE_SRC is required")
STATE=os.path.expanduser(os.environ.get("WRITER_STATE_DIR","~/.local/state/life-manager/writer"))
WORK=os.path.expanduser(os.environ.get("NOTE_WORK",f"{STATE}/note-work/note-stage")); os.makedirs(WORK, exist_ok=True)
IMG_DIR=os.environ.get("NOTE_IMG_DIR","automaton")
md=open(ART).read()
# strip YAML frontmatter (zenn/devto parse it; note has no frontmatter concept and would
# otherwise render the raw "title: ...\nemoji: ..." block as literal body text)
md=re.sub(r'^---\n.*?\n---\n', '', md, count=1, flags=re.S)
# strip H1 title
m=re.search(r'^#\s+(.+)$', md, re.M); title=(m.group(1).strip() if m else os.path.splitext(os.path.basename(ART))[0])
body=re.sub(r'^#\s+.+$', '', md, count=1, flags=re.M)
# strip local thumbnail ref
body=re.sub(r'!\[[^\]]*\]\(images/'+re.escape(IMG_DIR)+r'/thumb\.png\)\s*', '', body)
INFOG_REFS=[]
def _infog_cap(mo): INFOG_REFS.append(os.path.join(os.path.dirname(os.path.abspath(ART)), mo.group(1))); return f'@@INFOG{len(INFOG_REFS)}@@'
body=re.sub(r'!\[[^\]]*\]\((images/'+re.escape(IMG_DIR)+r'/what-is-[^)]*\.png)\)', _infog_cap, body)

# UN-BLOCKQUOTE: strip leading '> ' from every line (fixes 補足 separation + reveals harness table)
body=re.sub(r'^>\s?', '', body, flags=re.M)
body=re.sub(r'^\*\*(📌\s*補足[:：].+?)\*\*\s*$', r'### \1', body, flags=re.M)

# find markdown tables (consecutive '|' lines), replace with @@TBLn@@, render PNG (bigger+sharp)
def ch(c):
    c=html.escape(c.strip()); return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', c)
def to_html(tbl):
    rows=[[c for c in r.strip("|").split("|")] for r in tbl]
    rows=[r for i,r in enumerate(rows) if not (i==1 and all(set(c.strip())<=set("-: ") for c in r))]
    if not rows: return "<table></table>"
    th="".join(f"<th>{ch(c)}</th>" for c in rows[0])
    trs="".join("<tr>"+"".join(f"<td>{ch(c)}</td>" for c in r)+"</tr>" for r in rows[1:])
    return f"""<!doctype html><meta charset=utf-8><style>
    body{{margin:0;padding:28px;background:#fff;font-family:'Hiragino Sans',sans-serif;width:1080px;}}
    table{{border-collapse:collapse;width:100%;table-layout:fixed;font-size:28px;color:#1f2937;line-height:1.5;}}
    th,td{{border:2px solid #cbd5e1;padding:14px 18px;text-align:left;vertical-align:top;word-break:break-word;}}
    th{{background:#1E3A8A;color:#fff;font-weight:700;}} tr:nth-child(even) td{{background:#f1f5f9;}}
    b{{color:#0f766e;}}</style><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"""

out=[]; cur=[]; tbl_paths=[]
ctx=launch_context(headless=True, humanize=False)
pg=ctx.new_page()
try:
    pg.set_viewport_size({"width":1120,"height":1200})
    def flush():
        if len(cur)>=2:
            n=len(tbl_paths)+1; fn=f"{WORK}/tbl{n}.html"; open(fn,"w").write(to_html(cur))
            pg.goto("file://"+fn, wait_until="domcontentloaded", timeout=15000); time.sleep(0.5)
            png=f"{WORK}/tbl{n}.png"
            pg.query_selector("body").screenshot(path=png); tbl_paths.append(png)
            out.append(f"@@TBL{n}@@")
        else: out.extend(cur)
    for ln in body.split("\n"):
        if ln.strip().startswith("|"): cur.append(ln)
        else: flush(); cur.clear(); out.append(ln)
    flush()
finally:
    try: ctx.close()
    except: pass
body="\n".join(out)
# find mermaid blocks → markers + save sources
figs=[]
def rfig(mo): figs.append(mo.group(1)); return f"\n@@FIG{len(figs)}@@\n"
body=re.sub(r'```mermaid\n(.*?)\n```', rfig, body, flags=re.S)
json.dump({"title":title,"body":body,"tables":tbl_paths,"mermaids":figs,"infographics":INFOG_REFS}, open(f"{WORK}/note-manifest.json","w"))
print(f"title={title[:30]} | tables={len(tbl_paths)} | mermaids={len(figs)}")
