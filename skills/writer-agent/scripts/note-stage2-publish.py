# NOTE (VSDD F3 FIND-011): this path is DRAFT-ONLY by construction — it calls note-mcp update_article →
# draft_save?is_temp_saved=true (status=DRAFT); it never calls publish_article(). No public publish here.
import sys, json, time, asyncio, urllib.request, os, subprocess, re
from pathlib import Path
sys.path.insert(0, os.environ.get("NOTE_MCP_SRC", str(Path(__file__).resolve().parents[1] / "vendor/note-mcp/src")))
from note_mcp.models import Session, ArticleInput
from note_mcp.api.articles import update_article, generate_image_html
from note_s3_upload import upload_body_image
from note_stage2_assets import resolve_mermaid_images
WORK=os.path.expanduser(os.environ.get("NOTE_WORK","~/.cloak/note-work/note-stage")); os.makedirs(WORK, exist_ok=True)
mf=json.load(open(f"{WORK}/note-manifest.json")); ck=json.load(open(os.path.expanduser("~/.cloak/note-work/note-cookies.json")))
NUM=os.environ.get("NOTE_NUM","166686292"); title=mf["title"]; body=mf["body"]
# NOTE_KEY (article key, e.g. "n1234567890ab"): when set, used for update_article INSTEAD of NUM.
# Works around a real note-mcp bug (note_mcp/api/articles.py update_article, verified by reading
# the source): when article_id is numeric AND the body contains an embed-worthy standalone URL
# (youtube/twitter/note.com/github/zenn/qiita/... — see note_mcp/utils/markdown_to_html.py
# has_embed_url), update_article tries to resolve the article's key by calling
# get_article_via_api(numeric_id) — but that function itself REJECTS numeric IDs and raises
# NoteAPIError unconditionally. Passing the key directly skips that broken code path entirely
# (_is_article_key_format short-circuits it). upload_body_image does NOT need this — its note_id
# argument is accepted but never referenced in the function body, so NUM works fine there.
KEY=os.environ.get("NOTE_KEY","")
ARTICLE_ID=KEY or NUM
_SRC=os.environ.get("NOTE_SRC","")
# the Automaton note-id default is allowed ONLY for the genuine default run (no explicit NOTE_SRC). ANY explicit
# article must pass NOTE_NUM, so no non-Automaton article (whatever its title) can overwrite the live note 166686292.
if NUM=="166686292" and _SRC:
    raise SystemExit("NOTE_NUM defaulted to the Automaton note 166686292 but NOTE_SRC=%r is set — pass NOTE_NUM for any non-default article" % _SRC)
INFOG=os.environ.get("NOTE_INFOG","")   # env OVERRIDE for the first infographic only; each @@INFOGn@@ otherwise uses the article's own captured path
# tags: the Automaton tags are the default ONLY for the genuine default run; an explicit article gets NOTE_TAGS or none (never the Automaton tags)
TAGS=[t for t in os.environ.get("NOTE_TAGS", ("AI,AIエージェント,暗号資産,Automaton,自律AI" if not _SRC else "")).split(",") if t]
def dims(p):
    o=subprocess.check_output(["sips","-g","pixelWidth","-g","pixelHeight",p]).decode()
    w=int(re.search(r'pixelWidth: (\d+)',o).group(1)); h=int(re.search(r'pixelHeight: (\d+)',o).group(1)); return w,h
# A diagram must fit on the reader's screen without scrolling, and must never be upscaled past its
# own pixels. Sizing by width alone does neither: a `flowchart TD` comes out of kroki portrait (e.g.
# 276x606), so forcing width=480 stretched it to 480x1052 — taller than the viewport and blurry,
# because 480 is wider than the 276 pixels that actually exist. Fit inside a width AND a height box,
# and never scale above 1.0.
MAX_H = 560   # a diagram taller than this pushes the next paragraph off-screen on a laptop
def compact_html(url, localpng, cw=480, max_h=None):
    w,h=dims(localpng)
    mh = max_h or MAX_H
    scale = min(cw/w, mh/h, 1.0)   # 1.0 cap = never upscale past the source pixels
    return generate_image_html(url, width=max(1,round(w*scale)), height=max(1,round(h*scale)))
os.makedirs(WORK, exist_ok=True)
def render_remote_mermaid(src, path):
    req=urllib.request.Request("https://kroki.io/mermaid/png",data=src.encode(),headers={"Content-Type":"text/plain","User-Agent":"Mozilla/5.0"})
    path.write_bytes(urllib.request.urlopen(req,timeout=40).read())
figpaths=resolve_mermaid_images(
    mf,
    __import__("pathlib").Path(WORK),
    os.environ.get("ARTICLE_PUBLICATION_STATE", ""),
    render_remote_mermaid,
)
sess=Session(cookies=ck, user_id=os.environ.get("NOTE_USER_ID", "14651590"), username=os.environ.get("NOTE_URLNAME", "anicca123"), created_at=int(time.time()))
async def main():
    nb=body  # no body hero (the cover is the note eyecatch, set separately) — avoids the duplicate
    total=0; ok=0; failed=[]   # embed accounting — this is what makes a partial failure visible to the caller
    def _fail(label, e):
        # visible placeholder, NOT a silent blank: the old code replaced the marker with "" on
        # exception, which erased all trace of the missing image (a draft with 5/6 images looked
        # identical to a fully-embedded one). Leaving readable Japanese text means a human
        # reviewing the draft by hand actually sees the gap instead of missing it.
        failed.append(label); print(f"{label} FAIL {str(e)[:50]}", flush=True)
        return f"\n[画像の埋め込みに失敗しました: {label}]\n"
    # infographic (full width, right after [0])
    infogs = mf.get("infographics") or []
    for _i, _ip in enumerate(infogs, 1):
        path = INFOG if (_i==1 and INFOG) else _ip   # NOTE_INFOG env overrides the first infographic only
        label=f"infog{_i}"
        if path and os.path.exists(path):
            total+=1
            try:
                ig=await upload_body_image(sess, path, NUM)
                nb=nb.replace(f"@@INFOG{_i}@@", "\n"+compact_html(ig.url, path, cw=380)+"\n"); ok+=1; print(label, flush=True)
            except Exception as e: nb=nb.replace(f"@@INFOG{_i}@@", _fail(label, e))
        else: nb=nb.replace(f"@@INFOG{_i}@@","")   # no infographic supplied for this slot — intentional, not a failure, not counted
    nb=re.sub(r'@@INFOG\d+@@','',nb)   # drop any stray/unmatched infographic markers
    for i,p in enumerate(mf["tables"],1):
        label=f"tbl{i}"; total+=1
        try:
            img=await upload_body_image(sess,p,NUM)
            nb=nb.replace(f"@@TBL{i}@@", "\n"+compact_html(img.url,p)+"\n"); ok+=1; print(label,flush=True)
        except Exception as e: nb=nb.replace(f"@@TBL{i}@@", _fail(label, e))
    for i,p in enumerate(figpaths,1):
        label=f"fig{i}"; total+=1
        try:
            img=await upload_body_image(sess,p,NUM)
            nb=nb.replace(f"@@FIG{i}@@", "\n"+compact_html(img.url,p)+"\n"); ok+=1; print(label,flush=True)
        except Exception as e: nb=nb.replace(f"@@FIG{i}@@", _fail(label, e))
    # draft is ALWAYS saved, with whatever succeeded — a partial embed failure must not drop the whole draft.
    # update_article can ALSO fail outright (e.g. NOTE_KEY was not supplied and the body has an embed-worthy
    # URL — see the ARTICLE_ID comment above). That must still print EMBED_SUMMARY instead of dying with a
    # bare traceback and no stdout line: publish-note.sh greps stdout for "embedded=N/M" to fill in
    # stage2_embedded=, and a body that never got saved means 0 of the uploaded images actually made it into
    # the persisted draft — regardless of how many individual uploads above succeeded — so embedded=0/total
    # is the correct count here, not `ok`.
    try:
        await update_article(sess, ARTICLE_ID, ArticleInput(title=title, body=nb, tags=TAGS))
    except Exception as e:
        print(f"EMBED_SUMMARY embedded=0/{total} failed=update_article:{str(e)[:120]}", flush=True)
        sys.exit(1)
    print(f"UPDATED note article id={ARTICLE_ID}", flush=True)
    print(f"EMBED_SUMMARY embedded={ok}/{total} failed={','.join(failed) if failed else 'none'}", flush=True)
    if failed:
        sys.exit(1)   # non-zero rc = "draft saved but NOT all images embedded" — publish-note.sh maps this to stage2_ok=false
asyncio.run(main())
