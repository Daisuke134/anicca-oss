#!/usr/bin/env bash
# Copy durable Founder business state into the registry-owned root. Never deletes sources.
set -euo pipefail

SOURCE="${1:-$HOME/.anicca-founder}"
TARGET="${2:-$HOME/.local/state/life-manager/founder-loop-cadence}"
[[ "$SOURCE" = /* && "$TARGET" = /* && "$SOURCE" != "$TARGET" ]] || {
  echo "source and target must be distinct absolute paths" >&2; exit 2;
}
case "$TARGET/" in "$SOURCE/"*) echo "target must not be inside source" >&2; exit 2;; esac
[ -d "$SOURCE" ] || { echo '{"status":"skipped","reason":"no_legacy_source"}'; exit 0; }
if find "$SOURCE/state" -type l -print -quit 2>/dev/null | grep -q .; then
  echo "legacy state contains a symlink" >&2; exit 2
fi

mkdir -p "$TARGET/state" "$TARGET/logs"
copied=0; preserved=0
copy_one() {
  local src="$1" dst="$2"
  if [ -e "$dst" ]; then preserved=$((preserved + 1)); return; fi
  mkdir -p "$(dirname "$dst")"
  cp -p "$src" "$dst"
  chmod 600 "$dst"
  copied=$((copied + 1))
}
[ ! -f "$SOURCE/wallet.json" ] || copy_one "$SOURCE/wallet.json" "$TARGET/wallet.json"
[ ! -f "$SOURCE/STATE.md" ] || copy_one "$SOURCE/STATE.md" "$TARGET/STATE.md"
while IFS= read -r -d '' src; do
  rel="${src#"$SOURCE/state/"}"
  copy_one "$src" "$TARGET/state/$rel"
done < <(find "$SOURCE/state" -type f -print0 2>/dev/null)
find "$TARGET" -type d -exec chmod 700 {} +
printf '{"status":"ok","copied":%d,"preserved":%d}\n' "$copied" "$preserved"
