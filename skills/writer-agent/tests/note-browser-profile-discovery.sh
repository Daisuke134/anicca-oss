#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/private/playwright_firefoxdev_profile-old"
mkdir -p "$TMP/var/a/b/T/playwright_firefoxdev_profile-new"
mkdir -p "$TMP/var/a/b/T/too/deep/playwright_firefoxdev_profile-ignore"
touch -t 202607280101 "$TMP/private/playwright_firefoxdev_profile-old"
touch -t 202607290101 "$TMP/var/a/b/T/playwright_firefoxdev_profile-new"
touch -t 202607300101 "$TMP/var/a/b/T/too/deep/playwright_firefoxdev_profile-ignore"

ACTUAL="$(python3 "$ROOT/scripts/find-note-browser-profile.py" \
  --root "$TMP/private:1" --root "$TMP/var:4")"
test "$ACTUAL" = "$TMP/var/a/b/T/playwright_firefoxdev_profile-new"

# Ordered discovery preserves an explicitly selected root even when a fallback
# happens to contain a newer profile.
ORDERED="$(python3 "$ROOT/scripts/find-note-browser-profile.py" \
  --ordered-root "$TMP/private:1" --ordered-root "$TMP/var:4")"
test "$ORDERED" = "$TMP/private/playwright_firefoxdev_profile-old"

echo "PASS: note browser profile discovery is bounded and newest-first"
