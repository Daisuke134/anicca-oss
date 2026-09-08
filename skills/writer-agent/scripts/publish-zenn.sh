#!/usr/bin/env bash
# publish-zenn.sh — managed Zenn boundary for Writer Agent
# Usage:
#   bash publish-zenn.sh --markdown-file <f> --title <t> --meta <m>
# stdout: release URL on success
# Strategy: copy md to the managed Writer Agent draft dir, then invoke post-zenn.py

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"

MD_FILE=""
TITLE=""
META=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --markdown-file) MD_FILE="$2"; shift 2 ;;
    --title)         TITLE="$2"; shift 2 ;;
    --meta)          META="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -f "$MD_FILE" ]] || { echo "FATAL: markdown not found: $MD_FILE" >&2; exit 1; }
[[ -n "$TITLE" ]] || { echo "FATAL: --title required" >&2; exit 1; }

# Validate frontmatter has title
if ! grep -q '^title:' "$MD_FILE"; then
  echo "FATAL: markdown missing 'title:' frontmatter" >&2; exit 1
fi

if [[ -z "${ARTICLE_RUN_DIR:-}" || ! -f "${ARTICLE_PUBLICATION_STATE:-}" || ! -f "${ARTICLE_LEDGER:-}" ]]; then
  echo "FATAL: managed publication state is required" >&2
  exit 2
fi

# --- fail-closed PII gate (scripts/pii-gate.py) ---------------------------------------
# Nothing operator-identifying may reach the PUBLIC zenn-articles repository (a push is public even at published:false). ANY non-zero exit from the gate -- a finding,
# an unconfigured blocklist, or an internal scanner error -- aborts this publish. Gate output
# goes to stderr so it cannot pollute this script's single-line stdout contract.
python3 "$SCRIPT_DIR/pii-gate.py" --stage publish-zenn "$MD_FILE" >&2 || exit $?


# Initial staging is always draft-only. Zenn renders an article publicly when
# published:true reaches the linked repository, so this boundary forces false
# on a copy. The dedicated deferred worker may later flip only the registered
# slug after the measured platform interval and exact8 readback gates pass.
TODAY="$(date +%Y-%m-%d)"
DRAFT_DIR="$HOME/.openclaw/workspace/writer-agent/drafts/$TODAY"
mkdir -p "$DRAFT_DIR"
cp "$MD_FILE" "$DRAFT_DIR/ja.md"

# Force published:false on the staged copy regardless of what the source file said.
sed -i '' 's/^published: true$/published: false/' "$DRAFT_DIR/ja.md"
if ! grep -q '^published: false' "$DRAFT_DIR/ja.md"; then
  # Frontmatter had no published: line at all -- inject one right after the opening ---.
  sed -i '' '1{/^---$/a\
published: false
}' "$DRAFT_DIR/ja.md"
fi
if grep -qE '^published:[[:space:]]*true[[:space:]]*$' "$DRAFT_DIR/ja.md"; then
  echo "FATAL: staged draft still has published: true after forcing false -- refusing to publish" >&2
  exit 5
fi

# Invoke existing post-zenn.py with explicit ARTICLE_DATE
# The managed exact8 package publishes its immutable media bytes to the
# already-public Zenn repository before any downstream staging. Dev.to then
# references these content-addressed run paths; Zenn itself keeps native
# Mermaid rendering. Only this run's media directory is staged or committed.
if [[ -n "${ARTICLE_RUN_DIR:-}" || -n "${ARTICLE_PUBLICATION_STATE:-}" ]]; then
  [[ -f "${ARTICLE_PUBLICATION_STATE:-}" && -d "${ARTICLE_RUN_DIR:-}" ]] || {
    echo "FATAL: managed Zenn staging requires run directory and publication state" >&2
    exit 2
  }
  ZENN_REPO_PATH="${ZENN_REPO_PATH:-$HOME/.openclaw/workspace/zenn-articles}"
  [[ -d "$ZENN_REPO_PATH/.git" ]] || {
    echo "FATAL: Zenn repository is missing: $ZENN_REPO_PATH" >&2
    exit 3
  }
  RUN_ID="$(jq -r '.run_id // empty' "$ARTICLE_PUBLICATION_STATE")"
  [[ "$RUN_ID" == "$(basename "$ARTICLE_RUN_DIR")" && "$RUN_ID" =~ ^(daily-[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8}-[0-9]{6})$ ]] || {
    echo "FATAL: Zenn media run identity is invalid" >&2
    exit 3
  }
  MEDIA_REL="images/$RUN_ID"
  MEDIA_DIR="$ZENN_REPO_PATH/$MEDIA_REL"
  mkdir -p "$MEDIA_DIR"
  while IFS=$'\t' read -r MEDIA_PATH MEDIA_HASH; do
    [[ -f "$MEDIA_PATH" && -n "$MEDIA_HASH" ]] || {
      echo "FATAL: immutable media entry is missing" >&2
      exit 3
    }
    ACTUAL_HASH="$(shasum -a 256 "$MEDIA_PATH" | awk '{print $1}')"
    [[ "$ACTUAL_HASH" == "$MEDIA_HASH" ]] || {
      echo "FATAL: immutable media changed before Zenn staging" >&2
      exit 3
    }
    cp "$MEDIA_PATH" "$MEDIA_DIR/$(basename "$MEDIA_PATH")"
  done < <(jq -r '
    [.media.headline_image] + .media.body_assets
    | .[] | [.path,.sha256] | @tsv
  ' "$ARTICLE_PUBLICATION_STATE")
  git -C "$ZENN_REPO_PATH" add "$MEDIA_REL"
  UNRELATED_STAGED="$(git -C "$ZENN_REPO_PATH" diff --cached --name-only | grep -v "^${MEDIA_REL}/" || true)"
  [[ -z "$UNRELATED_STAGED" ]] || {
    echo "FATAL: Zenn repository has unrelated staged changes; refusing media commit" >&2
    exit 3
  }
  if ! git -C "$ZENN_REPO_PATH" diff --cached --quiet -- "$MEDIA_REL"; then
    git -C "$ZENN_REPO_PATH" \
      -c user.email=anicca@aniccaai.com -c user.name=anicca \
      commit -m "media: exact8 $RUN_ID" -- "$MEDIA_REL" >/dev/null || exit 3
    GIT_SSH_COMMAND="ssh -i ${ZENN_SSH_KEY:-$HOME/.ssh/id_ed25519} -o IdentitiesOnly=yes" \
      git -C "$ZENN_REPO_PATH" push origin HEAD:main >/dev/null || exit 3
  fi
  python3 - "$ARTICLE_PUBLICATION_STATE" "$ARTICLE_RUN_DIR/gates/media-urls.json" \
    "${ARTICLE_MEDIA_RAW_BASE:-https://raw.githubusercontent.com/Daisuke134/zenn-articles/main/images}" <<'PY'
import json, os, sys
from pathlib import Path
state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
base = sys.argv[3].rstrip("/") + "/" + state["run_id"]
items = [state["media"]["headline_image"], *state["media"]["body_assets"]]
value = {
    "version": 1,
    "run_id": state["run_id"],
    "assets": [
        {"path": item["path"], "sha256": item["sha256"],
         "url": base + "/" + Path(item["path"]).name}
        for item in items
    ],
}
target = Path(sys.argv[2])
temporary = target.with_name("." + target.name + f".{os.getpid()}.tmp")
temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
                     encoding="utf-8")
os.replace(temporary, target)
PY
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$(ARTICLE_DATE="$TODAY" python3 "$SCRIPT_DIR/post-zenn.py" 2>&1)"
EC=$?
if [[ $EC -ne 0 ]]; then
  echo "FATAL: post-zenn.py failed (exit=$EC):" >&2
  printf '%s\n' "$OUT" >&2
  exit $EC
fi

# post-zenn.py has a JSON stdout contract. Parsing it as arbitrary log text
# previously retained the trailing `",` and registered that punctuation as
# part of the managed slug.
URL="$(printf '%s\n' "$OUT" | jq -er '.url | select(type == "string")' 2>/dev/null || true)"
if [[ ! "$URL" =~ ^https://zenn\.dev/[A-Za-z0-9_-]+/articles/[a-z0-9_-]{12,50}$ ]]; then
  echo "FATAL: post-zenn.py returned no canonical Zenn URL" >&2
  printf '%s\n' "$OUT" >&2
  exit 4
fi

if [[ -n "${ARTICLE_RUN_DIR:-}" || -n "${ARTICLE_PUBLICATION_STATE:-}" || -n "${ARTICLE_LEDGER:-}" ]]; then
  ZENN_SLUG="${URL##*/}"
  python3 "$SCRIPT_DIR/publication-guard.py" register-intent \
    --pair zenn-article/ja --target-kind zenn-slug --target "$ZENN_SLUG" >/dev/null || exit $?
fi

echo "$URL"
