#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)"
CODE_ROOT="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd -P)" || {
  echo "agent-economy: immutable release could not be resolved" >&2
  exit 2
}
RELEASE_ROOT="${ANICCA_RELEASE_ROOT:-$(cd "$CODE_ROOT/../.." 2>/dev/null && pwd -P)}"
REPO="${ANICCA_REPO:-$CODE_ROOT}"
case "$CODE_ROOT" in
  */.worktrees/*)
    echo "agent-economy: refusing worktree runtime path: $CODE_ROOT" >&2
    exit 2
    ;;
esac

die() { echo "agent-economy: $*" >&2; exit 2; }

[ -d "$RELEASE_ROOT/releases" ] || die "namespaced releases root is missing: $RELEASE_ROOT/releases"
RELEASE="$CODE_ROOT"
[ "$REPO" = "$CODE_ROOT" ] || die "runtime repository must be the executing release"
RELEASES="$(cd "$RELEASE_ROOT/releases" 2>/dev/null && pwd -P)" || die "namespaced releases root cannot be resolved"
case "$RELEASE" in
  "$RELEASES"/*) ;;
  *) die "current release escapes the namespaced releases root" ;;
esac
[ -f "$RELEASE/RELEASE.json" ] && [ ! -L "$RELEASE/RELEASE.json" ] \
  || die "sealed release metadata is missing: $RELEASE/RELEASE.json"

METADATA_FIELDS="$(node - "$RELEASE/RELEASE.json" "$RELEASE" <<'NODE'
const fs = require('node:fs');
const path = require('node:path');
const [metadataPath, releasePath] = process.argv.slice(2);
let metadata;
try { metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8')); } catch { process.exit(2); }
if (!metadata || typeof metadata !== 'object') process.exit(3);
if (!/^[0-9a-f]{40}$/.test(String(metadata.sha || ''))) process.exit(7);
if (!(path.basename(releasePath) === metadata.sha || path.basename(releasePath).endsWith(`-${metadata.sha.slice(0, 8)}`))) process.exit(9);
if ((fs.lstatSync(metadataPath).mode & 0o222) !== 0) process.exit(24);
process.stdout.write(`${path.basename(releasePath)}\t${metadata.sha}`);
NODE
)" || die "sealed release metadata is invalid"
IFS=$'\t' read -r RELEASE_ID RELEASE_SHA <<EOF
$METADATA_FIELDS
EOF

if [ -n "${ANICCA_RELEASE_ID:-}" ] && [ "$ANICCA_RELEASE_ID" != "$RELEASE_ID" ]; then
  die "release id does not match sealed metadata"
fi
if [ -n "${ANICCA_RELEASE_SHA:-}" ] && [ "$ANICCA_RELEASE_SHA" != "$RELEASE_SHA" ]; then
  die "release sha does not match sealed metadata"
fi

[ -x "$REPO/runtime/anicca-daemon.sh" ] || die "missing daemon at $REPO/runtime/anicca-daemon.sh"

if [ "${ANICCA_VALIDATE_RELEASE_ONLY:-0}" = "1" ]; then
  echo "agent-economy: sealed release validation passed ($RELEASE_ID)"
  exit 0
fi

export ANICCA_REPO="$CODE_ROOT" ANICCA_CODE_ROOT="$CODE_ROOT"
export ANICCA_RELEASE_ROOT
export ANICCA_RELEASE_ID="$RELEASE_ID" ANICCA_RELEASE_SHA="$RELEASE_SHA"
if [ "${ANICCA_ECONOMY_CREATE_EVM_WALLET:-0}" = "1" ]; then
  /usr/bin/env node "$REPO/runtime/compute-proxy/ensure-wallet.mjs" >/dev/null
fi
exec /bin/bash "$REPO/runtime/anicca-daemon.sh"
