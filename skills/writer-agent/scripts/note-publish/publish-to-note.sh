#!/bin/bash
# F1 — one-command note publisher. Orchestrates the proven note-publish scripts, idempotent + guarded,
# with a deterministic VERIFY gate whose screenshot the active model agent LOOKS at before --go.
# Spec: docs/superpowers/specs/2026-06-24-publish-to-note-sh-F1.md. NEVER /tmp; data in Writer state.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/../writer-runtime-env.sh"
PY="${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}"
HBPY="$PY"
WORK="$NOTE_WORK_ROOT"; mkdir -p "$WORK"
DEFAULT_KEY="na3a631e63d1a"

filt(){ grep -vE "Update available|pip install|fonts loaded|no leaks found" || true; }

cmd="${1:-}"; shift || true

case "$cmd" in
  cookies)
    "$HBPY" "$DIR/extract-note-cookies.py" | filt ;;

  verify)
    key="${1:-$DEFAULT_KEY}"; gated="${2:-true}"
    echo "== VERIFY note/$key (visitor view + API truth) =="
    "$PY" "$DIR/verify-note.py" "$key" "$gated" | filt
    rc=$?
    echo "→ screenshot: $WORK/verify-$key.png  (AGENT: Read it and judge the look before any --go)"
    exit $rc ;;

  publish)
    # publish <markdown> [--key K|new] [--price 500] [--paywall-before "H"] [--eyecatch IMG] [--mode draft|go]
    MD="${1:-}"; shift || true
    KEY="$DEFAULT_KEY"; PRICE="500"; PAYWALL=""; EYECATCH=""; MODE="draft"; NUM=""; IMG_DIR=""; TAGS=""; INFOG=""
    while [ $# -gt 0 ]; do case "$1" in
      --key) KEY="$2"; shift 2;;
      --price) PRICE="$2"; shift 2;;
      --paywall-before) PAYWALL="$2"; shift 2;;
      --eyecatch) EYECATCH="$2"; shift 2;;
      --mode) MODE="$2"; shift 2;;
      --num) NUM="$2"; shift 2;;
      --img-dir) IMG_DIR="$2"; shift 2;;
      --tags) TAGS="$2"; shift 2;;
      --infog) INFOG="$2"; shift 2;;
      *) echo "unknown arg: $1"; exit 2;;
    esac; done
    export NOTE_KEY="$KEY" NOTE_PRICE="$PRICE" NOTE_PAYWALL="$PAYWALL" NOTE_EYECATCH="$EYECATCH" NOTE_MODE="$MODE" NOTE_SRC="$MD"
    [ -n "$NUM" ] && export NOTE_NUM="$NUM"; [ -n "$IMG_DIR" ] && export NOTE_IMG_DIR="$IMG_DIR"; [ -n "$TAGS" ] && export NOTE_TAGS="$TAGS"; [ -n "$INFOG" ] && export NOTE_INFOG="$INFOG"
    echo "== PUBLISH md=$MD key=$KEY price=$PRICE paywall-before='$PAYWALL' mode=$MODE =="
    echo "[1/7] cookies";   "$HBPY" "$DIR/extract-note-cookies.py" | filt
    if [ "$KEY" = "new" ]; then
      [ -n "$NUM" ] || { echo "ERROR: --key new requires --num <noteId> (new-article id creation is a TODO) — refusing to default to the Automaton note 166686292"; exit 2; }
      echo "[2/7] render+draft (new article)"
      "$PY" "$DIR/../note-stage1-render.py" "$MD" | filt
      "$PY" "$DIR/../note-stage2-publish.py" | filt
      # the new key would be parsed from stage2 output here (TODO when first new article runs)
    else echo "[2/7] existing key → skip render"; fi
    echo "[3/7] eyecatch";  [ -n "$EYECATCH" ] && "$PY" "$DIR/set-eyecatch-republish.py" | filt || echo "  (skip)"
    echo "[4/7] 目次(manual big titles)"; "$PY" "$DIR/insert-toc-save.py" | filt; "$PY" "$DIR/delete-toc-node.py" | filt
    echo "[5/7] gate (無料+membership+試し読み, mode=$MODE)"; "$PY" "$DIR/publish.py" | filt
    echo "[6/7] plan 公開 ON"; "$PY" "$DIR/toggle-plan.py" | filt
    echo "[7/7] verify"; "$PY" "$DIR/verify-note.py" "$KEY" "true" | filt
    echo "→ AGENT: Read $WORK/verify-$KEY.png and judge before flipping --mode go." ;;

  enable-publish)
    # deliberate human act → create a 10-min sentinel that the publish guard requires (besides NOTE_MODE=go).
    mkdir -p "$WORK"; touch "$WORK/.PUBLISH_ENABLED"
    echo "PUBLISH ENABLED for 10 min (sentinel $WORK/.PUBLISH_ENABLED). Now run: publish ... --mode go" ;;
  disable-publish)
    rm -f "$WORK/.PUBLISH_ENABLED"; echo "publish disabled (sentinel removed)" ;;

  *)
    echo "usage:"
    echo "  publish-to-note.sh verify  <noteKey> [gated]"
    echo "  publish-to-note.sh publish <md> [--key K|new] [--price 500] [--paywall-before \"H\"] [--eyecatch IMG] [--mode draft|go]"
    echo "  publish-to-note.sh enable-publish | disable-publish   (gate for a manual public publish)"
    echo "  publish-to-note.sh cookies"
    exit 2 ;;
esac
