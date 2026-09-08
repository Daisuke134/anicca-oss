#!/usr/bin/env bash
# render-verify-draft.sh — draft-stage visual reality gate (part 1/2 of the "render-verify + self-fix"
# pair, Dais's core ask: the loop must self-heal without human babysitting). Complements
# reality-gate.sh (which checks PUBLISHED reality via HTTP/API after the fact) by checking what a
# HUMAN EDITOR would see if they opened the draft right now, BEFORE anything ships: did rendering
# actually work, or did something break silently (raw frontmatter leaking into the visible body, a
# broken-image icon or literal un-rendered ```mermaid text where a figure should be, a missing note
# eyecatch, a misplaced paywall marker)? A gate that only reads HTML/JSON cannot see any of this —
# only an actual rendered screenshot can.
#
# Usage:
#   render-verify-draft.sh --platform <note|zenn|substack|devto> --url <draft edit URL> --lang <ja|en>
# stdout: one line of JSON:
#   {"verdict":"PASS"|"FAIL","problems":[...blocking, drives exit code...],"advisory":[...never blocks...]}
# exit 0 on PASS, 1 on FAIL. Judgment is a FRESH vision-mode model call every invocation — no
# caching, no reuse of a prior verdict (a stale PASS on a since-changed draft is worse than no gate).
#
# Deps this script declares for itself (not vendored -- project convention, see spec #58a/#64):
#   - a Chromium-family browser with CDP listening on CDP_PORT (default 9222; see ensure-browser.sh
#     in this same skill for how to keep one alive -- this script does NOT launch a browser itself,
#     it only drives one that is already up, same "never touch Dais's tabs" discipline as the rest
#     of this repo's browser-driving scripts)
#   - python3 with the `websocket-client` package (same lib every other raw-CDP script in this repo's
#     dependency tree already uses, e.g. ig-account-create/scripts/cdp.py)
#   - at least one healthy provider configured through runtime/model-runner.sh
#
# Why a NEW small CDP helper instead of reusing an existing one: this needs a genuinely FULL-PAGE
# screenshot (Page.captureScreenshot with captureBeyondViewport + an explicit clip covering the
# whole rendered page height), which the existing raw-CDP driver in this dependency tree
# (ig-account-create/scripts/cdp.py's `shot` command) does not do -- it captures the viewport only.
# This embeds the smallest possible addition (one extra RPC call, same connect/rpc pattern as that
# driver) rather than forcing an unsupported full-page use case onto a script built for something
# else.
set -uo pipefail
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}/runtime/model-runner.sh}"

PLATFORM=""
URL=""
LANG_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --lang) LANG_ARG="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$PLATFORM" in
  note|zenn|substack|devto) ;;
  *) echo "usage: render-verify-draft.sh --platform <note|zenn|substack|devto> --url <draft edit URL> --lang <ja|en>" >&2; exit 2 ;;
esac
[ -n "$URL" ] || { echo "usage: render-verify-draft.sh --platform <note|zenn|substack|devto> --url <draft edit URL> --lang <ja|en>" >&2; exit 2; }
case "$LANG_ARG" in
  ja|en) ;;
  *) echo "usage: render-verify-draft.sh --platform <note|zenn|substack|devto> --url <draft edit URL> --lang <ja|en>" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDP_PORT="${CDP_PORT:-9222}"
PY="${RENDER_VERIFY_PYTHON:-${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}}"
SHOT_DIR="${RENDER_VERIFY_SHOT_DIR:-$HOME/.cloak/render-verify}"
mkdir -p "$SHOT_DIR"
SHOT="$SHOT_DIR/${PLATFORM}-$(date +%s).png"

# Persist every verdict past this run's stdout (2026-07-17, same fix + same log file as
# deslop-gate.sh/eval-gate.sh) -- one append-only line per attempt, best-effort.
# ARTICLE_GATES_LOG override (2026-07-17, incident: an ad-hoc manual verification run
# against this same default path truncated real production evidence with no backup --
# tests/callers that need an isolated log MUST set this, never write to the production
# default), defaults to the real production path.
GATES_LOG="${ARTICLE_GATES_LOG:-$HOME/.openclaw/logs/article-gates.log}"
log_gate_verdict() {
  mkdir -p "$(dirname "$GATES_LOG")" 2>/dev/null || return 0
  printf '%s script=render-verify-draft.sh platform=%s url=%s lang=%s verdict=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$PLATFORM" "$URL" "$LANG_ARG" "$1" >>"$GATES_LOG" 2>/dev/null || true
}

# --- zenn: git-based, NO browser session (task #76, 2026-07-17 correction) ---
# Zenn publish is a plain `git push` to Daisuke134/zenn-articles (SKILL.md "ZENN ONE-SHOT
# PUBLISH") -- there is no login-gated dashboard in this loop's actual publish path, so
# screenshotting zenn.dev/dashboard (what an earlier version of this task mistakenly tried) is
# wrong on two counts: it needs a browser session this loop never otherwise uses, AND it does
# not verify the thing that can actually break (the pushed .md's frontmatter/mermaid/tables).
# --url accepts a full zenn.dev URL, a local `npx zenn preview` URL, or a bare slug -- only the
# last path segment (the slug) is used.
if [ "$PLATFORM" = "zenn" ]; then
  ZENN_REPO="${ZENN_ARTICLES_REPO:-$HOME/.openclaw/workspace/zenn-articles}"
  SLUG="${URL##*/}"
  MD="$ZENN_REPO/articles/$SLUG.md"
  PROBLEMS=(); ADVISORY=()

  if [ ! -f "$MD" ]; then
    PROBLEMS+=("rule: Z1, observation: $MD does not exist -- the article was never written into the zenn-articles repo")
  else
    if (cd "$ZENN_REPO" && ! git log -1 --format=%H -- "articles/$SLUG.md" >/dev/null 2>&1); then
      PROBLEMS+=("rule: Z2, observation: articles/$SLUG.md exists but has no commit in $ZENN_REPO -- never committed")
    elif (cd "$ZENN_REPO" && [ -n "$(git status --porcelain -- "articles/$SLUG.md" 2>/dev/null)" ]); then
      PROBLEMS+=("rule: Z2, observation: articles/$SLUG.md has uncommitted local changes -- not pushed as-is")
    fi

    FM_CHECK="$("$PY" - "$MD" <<'PYEOF'
import sys, re
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
problems = []
if not m:
    problems.append("Z3|no frontmatter block found (must start with --- ... ---)")
else:
    fm = m.group(1)
    for field in ("title", "emoji", "type", "topics", "published"):
        if not re.search(rf'^{field}:', fm, re.M):
            problems.append(f"Z3|frontmatter missing required field: {field}")
    type_m = re.search(r'^type:\s*"?(\w+)"?', fm, re.M)
    if type_m and type_m.group(1) not in ("tech", "idea"):
        problems.append(f"Z3|type must be tech or idea, got: {type_m.group(1)}")
    topics_m = re.search(r"^topics:\s*\[(.*?)\]", fm, re.M)
    if topics_m:
        n = len([t for t in topics_m.group(1).split(",") if t.strip()])
        if n > 5:
            problems.append(f"Z3|topics has {n} entries, must be <=5")
    emoji_m = re.search(r'^emoji:\s*"?([^"\n]+)"?', fm, re.M)
    if emoji_m and len(emoji_m.group(1).strip()) > 4:
        problems.append(f"Z4|emoji field looks like more than a single emoji: {emoji_m.group(1)!r}")

body = text[m.end():] if m else text
fences = re.findall(r"```mermaid\n(.*?)```", body, re.S)
for i, f in enumerate(fences):
    if not f.strip():
        problems.append(f"Z5|mermaid fence #{i+1} is empty -- would render as a blank/broken figure")

# Per the SKILL.md documented Zenn table rule: a blank line must surround every markdown table,
# or the next paragraph sticks to the table (GFM requirement).
for m2 in re.finditer(r"^\|.+\|$", body, re.M):
    line_no = body[:m2.start()].count("\n")
    lines = body.split("\n")
    prev_blank = line_no == 0 or lines[line_no - 1].strip() == "" or lines[line_no - 1].strip().startswith("|")
    if not prev_blank:
        problems.append(f"Z6|a table at line {line_no+1} has no blank line before it (GFM: sticks to prior paragraph)")
        break  # one instance is enough to flag; do not spam every row

print("\n".join(problems))
PYEOF
)"
    while IFS='|' read -r rule obs; do
      [ -z "$rule" ] && continue
      case "$rule" in
        Z1|Z2|Z3|Z5|Z6) PROBLEMS+=("rule: $rule, observation: $obs") ;;
        Z4) ADVISORY+=("rule: $rule, observation: $obs") ;;
      esac
    done <<<"$FM_CHECK"
  fi

  PROBLEMS_JSON="$(printf '%s\n' "${PROBLEMS[@]:-}" | "$PY" -c "import sys,json;print(json.dumps([l for l in sys.stdin.read().split(chr(10)) if l]))")"
  ADVISORY_JSON="$(printf '%s\n' "${ADVISORY[@]:-}" | "$PY" -c "import sys,json;print(json.dumps([l for l in sys.stdin.read().split(chr(10)) if l]))")"
  if [ "${#PROBLEMS[@]}" -eq 0 ]; then
    echo "{\"verdict\":\"PASS\",\"problems\":[],\"advisory\":$ADVISORY_JSON}"
    log_gate_verdict "PASS"
    exit 0
  else
    echo "{\"verdict\":\"FAIL\",\"problems\":$PROBLEMS_JSON,\"advisory\":$ADVISORY_JSON}"
    log_gate_verdict "FAIL"
    exit 1
  fi
fi

# --- full-page screenshot via raw CDP (own small helper, see header for why) ---
SHOT_ERR="$("$PY" - "$CDP_PORT" "$URL" "$SHOT" <<'PYEOF' 2>&1 >/dev/null
import sys, json, base64, time, urllib.request
from websocket import create_connection

port, url, out = sys.argv[1], sys.argv[2], sys.argv[3]
CDP = f"http://127.0.0.1:{port}"

def browser_ws():
    d = json.load(urllib.request.urlopen(CDP + "/json/version", timeout=5))
    return d["webSocketDebuggerUrl"]

def rpc(ws, _id, method, params=None):
    ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == _id:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})

bws = create_connection(browser_ws(), timeout=20, suppress_origin=True)
try:
    r = rpc(bws, 1, "Target.createTarget", {"url": url})
    tid = r["targetId"]
finally:
    bws.close()

try:
    pws = create_connection(f"ws://127.0.0.1:{port}/devtools/page/{tid}", timeout=30, max_size=None, suppress_origin=True)
    try:
        rpc(pws, 1, "Page.enable")
        time.sleep(3)  # let the SPA editor actually finish rendering, not just DOMContentLoaded
        metrics = rpc(pws, 2, "Page.getLayoutMetrics")
        size = metrics.get("cssContentSize") or metrics.get("contentSize")
        width = max(1, int(size["width"]))
        height = max(1, int(size["height"]))
        r = rpc(pws, 3, "Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
        })
        with open(out, "wb") as f:
            f.write(base64.b64decode(r["data"]))
    finally:
        pws.close()
finally:
    # never leave a stray tab on the shared daily-driver
    bws2 = create_connection(browser_ws(), timeout=20, suppress_origin=True)
    try:
        rpc(bws2, 4, "Target.closeTarget", {"targetId": tid})
    except Exception:
        pass
    finally:
        bws2.close()
PYEOF
)"
if [ ! -f "$SHOT" ]; then
  printf '{"verdict":"FAIL","problems":["screenshot_failed: %s"],"advisory":[]}\n' "$(printf '%s' "${SHOT_ERR:0:200}" | sed 's/"/\\"/g')"
  log_gate_verdict "FATAL:screenshot-failed"
  exit 1
fi

# --- judge (fresh every call, vision scoped to the one screenshot) ---
CHECKLIST="以下は${PLATFORM}の下書き編集画面の実スクリーンショットです（言語=${LANG_ARG}）。この画像を Read で開いて、
機械的チェックリストだけで判定しろ（あなたの一般的な文章の好みは判定に使うな）:

BLOCKING（1つでも該当したら FAIL、ルールIDと具体的にどこで見えたかを problems に書く）:
  P1 frontmatterらしきテキスト（title:/emoji:/topics: 等のYAML風の生テキスト）が本文領域にそのまま見えている
  P2 壊れた画像アイコン、または \`\`\`mermaid のような生のコードフェンステキストが図の代わりに見えている
     （= 描画されるべき図/表が描画されていない）

ADVISORY（該当しても FAIL にはしない。advisory に書くだけ）:
  A1 見出しの階層が崩れている（例: H3の後に前置きなくH1が来る等、明らかに変）
  A2 [noteのみ] エディタ上部にアイキャッチ画像が見当たらない（この段階でまだ設定されていない可能性があるため
     advisory止まり。platform=note でない場合はこのチェック自体を省略してよい）
  A3 有料ライン（paywallの区切り）マーカーの位置が明らかに不自然（例: 本文の冒頭1行目にある等）

最後の行に、他に何も出力せず、この形式の1行JSONだけを出力しろ:
{\"verdict\":\"PASS\"または\"FAIL\",\"problems\":[\"rule: P#, quote/observation: <画像で見えた具体的な内容>\", ...],\"advisory\":[\"rule: A#, ...\", ...]}
problemsが空ならverdictはPASS。1つでもP1/P2があればFAIL。判定に迷ったら該当なしとせよ（過検出より見逃しの方が
安全 -- このgateはSTEP6.5相当のブロッキングゲートであり、疑わしきは人間の目に回すのではなく次段の
self-fixがさらに検証するため）。

対象スクリーンショット: ${SHOT}"

OUT="$(printf '%s' "$CHECKLIST" | "$MODEL_RUNNER" vision --prompt-file - --image "$SHOT" 2>&1)"
JSON="$(printf '%s\n' "$OUT" | grep -o '{"verdict".*}' | tail -1)"
if [ -z "$JSON" ]; then
  echo '{"verdict":"FAIL","problems":["judge_returned_no_json"],"advisory":[]}'
  log_gate_verdict "FATAL:no-json-from-judge"
  exit 1
fi
echo "$JSON"
JUDGE_VERDICT=$(printf '%s' "$JSON" | grep -o '"verdict":"[A-Z]*"' | head -1 | cut -d'"' -f4)
log_gate_verdict "${JUDGE_VERDICT:-UNKNOWN}"
printf '%s' "$JSON" | grep -q '"verdict":"PASS"'
