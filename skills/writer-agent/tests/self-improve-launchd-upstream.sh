#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/self-improve-upstream.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

git init --bare "$TMP/remote.git" >/dev/null
git init "$TMP/seed" >/dev/null
git -C "$TMP/seed" config user.name "Writer Test"
git -C "$TMP/seed" config user.email "writer-test@example.invalid"
printf 'base\n' >"$TMP/seed/state.txt"
git -C "$TMP/seed" add state.txt
git -C "$TMP/seed" commit -m base >/dev/null
git -C "$TMP/seed" branch -M main
git -C "$TMP/seed" remote add origin "$TMP/remote.git"
git -C "$TMP/seed" push -u origin main >/dev/null

git -C "$TMP/seed" switch -c codex/writer-e1-incident-red >/dev/null
printf 'stale\n' >>"$TMP/seed/state.txt"
git -C "$TMP/seed" commit -am stale >/dev/null
git -C "$TMP/seed" push origin codex/writer-e1-incident-red >/dev/null

git -C "$TMP/seed" switch main >/dev/null
printf 'current\n' >>"$TMP/seed/state.txt"
git -C "$TMP/seed" commit -am current >/dev/null
git -C "$TMP/seed" push origin main >/dev/null
git --git-dir="$TMP/remote.git" symbolic-ref HEAD refs/heads/main
git clone "$TMP/remote.git" "$TMP/runtime" >/dev/null

ROOT="$ROOT" RUNTIME_REPO="$TMP/runtime" python3 - <<'PY'
import importlib.util
import os
from pathlib import Path

root = Path(os.environ["ROOT"])
runtime = Path(os.environ["RUNTIME_REPO"])
os.environ.pop("ARTICLE_SOURCE_BRANCH", None)

module_path = root / "scripts/self_improve_control.py"
spec = importlib.util.spec_from_file_location("self_improve_control", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

module.ensure_repo_synced(runtime)
assert module.source_branch(runtime) == "main"
PY

echo "PASS: launchd learning derives the runtime checkout's origin upstream"
