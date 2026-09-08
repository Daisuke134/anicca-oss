#!/bin/bash

acquire_run_lock() {
  local lock_dir="$1" owner stale
  if mkdir "$lock_dir" 2>/dev/null; then
    printf '%s\n' "$$" >"$lock_dir/owner.pid"
    return 0
  fi

  owner="$(cat "$lock_dir/owner.pid" 2>/dev/null || true)"
  if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
    return 1
  fi

  stale="${lock_dir}.stale.$$"
  mv "$lock_dir" "$stale" 2>/dev/null || return 1
  if ! mkdir "$lock_dir" 2>/dev/null; then
    mv "$stale" "$lock_dir" 2>/dev/null || true
    return 1
  fi
  printf '%s\n' "$$" >"$lock_dir/owner.pid"
  rm -f "$stale/owner.pid"
  rmdir "$stale" 2>/dev/null || true
}

release_run_lock() {
  local lock_dir="$1" owner
  owner="$(cat "$lock_dir/owner.pid" 2>/dev/null || true)"
  [ "$owner" = "$$" ] || return 0
  rm -f "$lock_dir/owner.pid"
  rmdir "$lock_dir" 2>/dev/null || true
}
