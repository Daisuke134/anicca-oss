#!/bin/bash
# F4a — one-command Zenn publisher (the note-skill's sibling, for git-based Zenn). Repeatable, with the SAME
# verification discipline as note: adapt → NO-LIE gate → LOCAL render verify (agent Reads screenshots) →
# draft (published:false, verify non-public) → publish (published:true, gated, rate-limit aware) → verify LIVE.
# Spec: anicca-project/docs/superpowers/specs/2026-06-24-publish-to-zenn-F4a.md. NEVER /tmp; SSH remote (no PAT).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$HOME/.openclaw/workspace/zenn-articles"
PY="${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}"
WORK="$HOME/.cloak/note-work"; mkdir -p "$WORK"
USER_NAME="anicca"
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/id_ed25519 -o IdentitiesOnly=yes"
filt(){ grep -vE "Update available|pip install|fonts loaded|新しいバージョン|npm install zenn" || true; }
gitz(){ git -C "$REPO" -c url.git@github.com:.insteadOf= -c user.email=anicca@aniccaai.com -c user.name=anicca "$@"; }

# generic first-person run/test/next-chapter claims that must NEVER appear in a free 解説 (no-lie gate)
LIE_PATTERNS=(次の章 動かしてみた 動かしました 動かしてみた結果 動かしてみたら 実際の動かし方 動かして検証 \
  を動かしてみた 実際にお金を入れて動かし 頭脳をつないで動かしました 検証してみた)

no_lie_gate(){ # $1 = zenn md ; $2 = optional extra-blacklist file (article-specific numerics)
  local z="$1" extra="${2:-}" bad=0 t
  for t in "${LIE_PATTERNS[@]}"; do grep -qF "$t" "$z" && { echo "  ✗ LIE: $t"; bad=1; }; done
  if [ -n "$extra" ] && [ -f "$extra" ]; then
    while IFS= read -r t; do [ -n "$t" ] && grep -qF "$t" "$z" && { echo "  ✗ BLACKLIST: $t"; bad=1; }; done < "$extra"
  fi
  [ $bad -eq 0 ] && { echo "  ✓ no-lie PASS"; return 0; } || return 1
}

cmd="${1:-}"; shift || true
case "$cmd" in
  adapt) # adapt <src> <slug> <title> <emoji> <topics-csv> <paid-from> [closing]   (ZENN_CUT_LINES env optional)
    SRC="$1"; SLUG="$2"; OUT="$REPO/articles/$2.md"
    "$PY" "$DIR/zenn-adapt.py" "$SRC" "$OUT" "$2" "$3" "$4" "$5" "$6" "${7:-}" | filt
    echo "→ $OUT" ;;

  gate) # gate <slug> [extra-blacklist]
    echo "== NO-LIE GATE $1 =="; no_lie_gate "$REPO/articles/$1.md" "${2:-}" ;;

  render) # render <slug>  → start local preview; AGENT screenshots + Reads every section
    ( cd "$REPO" && [ -d node_modules/.bin ] || npm install >/dev/null 2>&1
      node_modules/.bin/zenn preview --port 8019 >"$WORK/zenn-preview.log" 2>&1 & )
    sleep 8
    echo "preview: http://localhost:8019/articles/$1  (HTTP $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8019/ 2>/dev/null))"
    echo "→ AGENT: screenshot every section + Read (mermaid SVG, tables, no broken render, no slop) before publish." ;;

  draft) # draft <slug>  → push published:false, verify NON-public
    sed -i '' 's/^published: true$/published: false/' "$REPO/articles/$1.md"
    gitz add "articles/$1.md"; gitz commit -q -m "article(draft): $1" 2>&1 | tail -1
    gitz push origin HEAD:main 2>&1 | tail -1; sleep 25
    pub=$(curl -s "https://zenn.dev/api/articles?username=$USER_NAME&order=latest" -H 'User-Agent: Mozilla/5.0' | "$PY" -c "import json,sys;print(any(x.get('slug')=='$1' for x in json.load(sys.stdin).get('articles',[])))")
    echo "draft public? $pub  (False = correct draft)" ;;

  publish) # publish <slug>   GATED: needs `enable` sentinel + ZENN_MODE=go ; rate-limit aware
    SLUG="$1"
    if [ "${ZENN_MODE:-}" != "go" ] || [ ! -f "$WORK/.ZENN_PUBLISH_ENABLED" ]; then
      echo "REFUSED: run '$0 enable' (10-min sentinel) AND ZENN_MODE=go to publish. (rate-limit: 1 new article/window, never toggle)"; exit 3; fi
    GUARD_OUT=$("$PY" "$DIR/../publication-guard.py" preflight --pair zenn-article/ja --target-kind zenn-slug --target "$SLUG") || exit $?
    if [ "$(printf '%s' "$GUARD_OUT" | jq -r '.action // empty')" = "skip-live" ]; then
      rm -f "$WORK/.ZENN_PUBLISH_ENABLED"
      echo "ALREADY_LIVE_SKIP url=$(printf '%s' "$GUARD_OUT" | jq -r '.live_url')"
      exit 0
    fi
    # re-run the no-lie gate as a hard pre-publish guard
    no_lie_gate "$REPO/articles/$SLUG.md" "${2:-}" || { echo "BLOCKED: no-lie gate failed"; exit 4; }
    sed -i '' 's/^published: false$/published: true/' "$REPO/articles/$SLUG.md"
    if [ -n "${ARTICLE_RUN_DIR:-}" ]; then
      "$PY" "$DIR/../zenn-deferred-control.py" create \
        --artifact "$ARTICLE_RUN_DIR/gates/zenn-deferred.json" \
        --markdown-file "$REPO/articles/$SLUG.md" --slug "$SLUG" || {
          echo "FATAL: could not persist deferred Zenn retry artifact" >&2
          exit 5
        }
    fi
    gitz add "articles/$SLUG.md"; gitz commit -q -m "article(publish): $SLUG LIVE" 2>&1 | tail -1
    gitz push origin HEAD:main 2>&1 | tail -1; rm -f "$WORK/.ZENN_PUBLISH_ENABLED"
    echo "pushed. verifying LIVE (Zenn 24h rate-limit may delay)…"; sleep 35
    "$0" verify "$SLUG"
    # NOT-LIVE under the rolling window stays as an intent for the deferred worker. If the
    # platform is already live, this records/repairs the current-run receipt now.
    "$PY" "$DIR/../publication-guard.py" reconcile --pair zenn-article/ja || true ;;

  verify) # verify <slug>
    URL="https://zenn.dev/$USER_NAME/articles/$1"
    code=$(curl -s -o /dev/null -w '%{http_code}' "$URL")
    live=$(curl -s "https://zenn.dev/api/articles?username=$USER_NAME&order=latest" -H 'User-Agent: Mozilla/5.0' | "$PY" -c "import json,sys;a=[x for x in json.load(sys.stdin).get('articles',[]) if x.get('slug')=='$1'];print('LIVE '+a[0]['title'][:40] if a else 'NOT-LIVE')")
    echo "HTTP $code | $live"
    [ "$code" = "200" ] || echo "  (403/not-live right after push usually = Zenn 24h post-count rate-limit, not a bug — re-trigger after the window clears)" ;;

  enable)  touch "$WORK/.ZENN_PUBLISH_ENABLED"; echo "ZENN publish enabled 10min. now: ZENN_MODE=go $0 publish <slug>" ;;
  disable) rm -f "$WORK/.ZENN_PUBLISH_ENABLED"; echo "disabled" ;;
  *) echo "usage: publish-to-zenn.sh {adapt|gate|render|draft|publish|verify|enable|disable} ..."; exit 2 ;;
esac
