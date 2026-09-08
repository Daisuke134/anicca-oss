import re, os, subprocess, urllib.request, html as _h
SRC=os.environ["X_SRC"]
DST=os.environ["X_DST"]
ASSETS=os.environ["X_ASSETS"]
import html as _html
from cloakbrowser import launch_context as _lc
THUMB=os.environ.get("X_COVER", "")
md=open(SRC).read()
md=re.sub(r'^---\n.*?\n---\n','',md,count=1,flags=re.S)   # strip frontmatter
# The canonical source envelope is a cross-platform manifest, not literal X
# body markup. X uses the pinned headline as its cover and the already-rendered
# body diagram as its one reader-visible figure. Resolve that body asset before
# writing the adapted Markdown into $WRITER_STATE_DIR/note-work, where relative run-dir
# paths would otherwise break.
_canonical=re.compile(
    r'(?ms)^<!-- canonical-media:start -->\n(?P<content>.*?)^<!-- canonical-media:end -->\n?'
)
if _canonical.search(md):
    # An in-place repair adapts the immutable draft into a copy under
    # gates/x-inplace-repair/<lang>/ (never the run root), so SRC is no
    # longer a sibling of body-diagram.png. Fall back to the managed
    # ARTICLE_RUN_DIR the caller already exports for exactly this reason
    # (measured live 2026-07-26: daily-2026-07-26 x-article/ja unpublished
    # successfully, then failed here and was stranded offline because only
    # the adapted copy's own directory was ever searched).
    _search_dirs=[os.path.dirname(SRC)]
    _run_dir=os.environ.get("ARTICLE_RUN_DIR","")
    if _run_dir and _run_dir not in _search_dirs:
        _search_dirs.append(_run_dir)
    _body=""
    for _dir in _search_dirs:
        _candidate=os.path.realpath(os.path.join(_dir,"body-diagram.png"))
        if os.path.isfile(_candidate):
            _body=_candidate
            break
    if not _body:
        raise SystemExit(
            "canonical body diagram is missing: "
            + os.path.join(_search_dirs[0], "body-diagram.png")
        )
    # The canonical envelope is the sole source of X media. Older drafts also
    # carried bare relative copies after the envelope; keeping those paths in
    # the prepared directory makes the parser treat the same cover/body as
    # missing content. Drop only these two legacy duplicate lines, preserving
    # every other reader-visible image and the immutable source files.
    md=re.sub(
        r'(?m)^!\[[^\]]*\]\(headline-image\.png\)\s*\n?',
        '',
        md,
    )
    md=re.sub(
        r'(?m)^!\[[^\]]*\]\(body-diagram\.png\)\s*\n?',
        '',
        md,
    )
    def _adapt_canonical(match):
        # Only the media representations are platform-specific. Preserve any
        # reader-visible copy in the envelope (notably the measurable CTA);
        # replacing the whole envelope silently deleted that final block.
        visible=match.group("content")
        visible=re.sub(r'(?ms)^```mermaid\s*\n.*?^```\s*$', '', visible)
        visible=re.sub(r'(?m)^\s*!\[[^\]]*\]\([^\n]*\)\s*$', '', visible)
        visible=re.sub(r'\n{3,}', '\n\n', visible).strip()
        suffix=f"\n\n{visible}" if visible else ""
        return f"\n![body diagram]({_body}){suffix}\n"
    md=_canonical.sub(_adapt_canonical,md,count=1)
# mermaid → kroki PNG
figs=[0]
def _pad_to_col(png, target=600):
    # X Articles upscales EVERY body image to the ~587px column width. A narrow (vertical) diagram gets
    # blown up huge. Pad narrow PNGs to >=600px wide (white bg, centred) so X shows them ~1:1, not upscaled.
    try:
        o=subprocess.run(["sips","-g","pixelWidth","-g","pixelHeight",png],capture_output=True,text=True).stdout
        w=int(re.search(r'pixelWidth: (\d+)',o).group(1)); h=int(re.search(r'pixelHeight: (\d+)',o).group(1))
        if w<target:
            subprocess.run(["sips","-p",str(h),str(target),"--padColor","FFFFFF",png,"--out",png],capture_output=True)
    except Exception: pass
def _fig(m):
    figs[0]+=1; src=m.group(1); p=f"{ASSETS}/fig{figs[0]}.png"
    try:
        req=urllib.request.Request("https://kroki.io/mermaid/png",data=src.encode(),headers={"Content-Type":"text/plain","User-Agent":"Mozilla/5.0"})
        open(p,"wb").write(urllib.request.urlopen(req,timeout=40).read())
        _pad_to_col(p)   # auto-fix: pad narrow diagrams so X doesn't upscale them (2026-06-28)
        return f"\n![figure]({p})\n"
    except Exception as e: return ""
md=re.sub(r'```mermaid\n(.*?)```',_fig,md,flags=re.S)
# tables → table_to_image PNG (each consecutive | block)
lines=md.split('\n'); out=[]; cur=[]; tn=[0]
_CTX=[None]
def _clean_table_png(tbl, png):
    def ch(c):
        c=_html.escape(c.strip()); return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', c)
    rows=[[c for c in r.strip("|").split("|")] for r in tbl]
    rows=[r for i,r in enumerate(rows) if not (i==1 and all(set(c.strip())<=set("-: ") for c in r))]
    th="".join(f"<th>{ch(c)}</th>" for c in rows[0]); trs="".join("<tr>"+"".join(f"<td>{ch(c)}</td>" for c in r)+"</tr>" for r in rows[1:])
    h=f"""<!doctype html><meta charset=utf-8><style>body{{margin:0;padding:24px;background:#fff;font-family:'Hiragino Sans',sans-serif;width:1080px}}table{{border-collapse:collapse;width:100%;table-layout:fixed;font-size:28px;color:#1f2937;line-height:1.45}}th,td{{border:2px solid #cbd5e1;padding:14px 18px;text-align:left;vertical-align:top;word-break:break-word}}th{{background:#1E3A8A;color:#fff;font-weight:700}}tr:nth-child(even) td{{background:#f1f5f9}}b{{color:#0f766e}}</style><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"""
    fn=png.replace('.png','.html'); open(fn,'w').write(h)
    if _CTX[0] is None: _CTX[0]=_lc(headless=True,humanize=False)
    pg=_CTX[0].new_page(); pg.goto('file://'+fn,wait_until='load',timeout=20000); import time as _t;_t.sleep(0.4)
    (pg.query_selector('table') or pg.query_selector('body')).screenshot(path=png); pg.close()
def flush():
    if not cur: return
    tn[0]+=1; png=f"{ASSETS}/tbl{tn[0]}.png"
    _clean_table_png(list(cur), png)
    out.append(f"![table]({png})" if os.path.exists(png) else '')
    cur.clear()
for l in lines:
    if l.lstrip().startswith('|'): cur.append(l)
    else:
        flush(); out.append(l)
flush()
md='\n'.join(out)
md=re.sub(r'\n{3,}','\n\n',md).strip()
# H1 title + cover image first
import re as _re2
has_h1=bool(_re2.match(r"^#\s+", md.strip()))
_title=os.environ.get("X_TITLE","")
_cover=os.environ.get("X_COVER", THUMB)
parts=[]
if _title and not has_h1: parts.append("# "+_title)
if _cover and os.path.exists(_cover): parts.append(f"![cover]({_cover})")
parts.append(md)
body="\n\n".join(parts)
open(DST,"w").write(body)
print("X md:",DST)
print("figs:",figs[0],"tables:",tn[0])
