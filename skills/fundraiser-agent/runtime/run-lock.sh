#!/bin/bash

run_lock_process_identity() {
  ps -o lstart= -p "$1" 2>/dev/null | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

run_lock_mtime() {
  stat -f '%m' "$1" 2>/dev/null || stat -c '%Y' "$1" 2>/dev/null
}

write_run_lock_owner() {
  local lock_dir="$1" identity temporary
  identity="$(run_lock_process_identity "$$")"
  [ -n "$identity" ] || return 1
  temporary="$lock_dir/.owner.$$"
  printf '%s\n%s\n' "$$" "$identity" >"$temporary"
  mv "$temporary" "$lock_dir/owner"
}

acquire_run_lock() {
  local lock_dir="$1" owner_pid="" owner_identity="" owner_missing=false actual_identity age stale
  if mkdir "$lock_dir" 2>/dev/null; then
    write_run_lock_owner "$lock_dir" || { rmdir "$lock_dir" 2>/dev/null || true; return 1; }
    return 0
  fi

  if [ -f "$lock_dir/owner" ]; then
    { IFS= read -r owner_pid; IFS= read -r owner_identity; } <"$lock_dir/owner" || true
  else
    owner_missing=true
  fi
  if [[ "$owner_pid" =~ ^[0-9]+$ ]] && [ -n "$owner_identity" ] && kill -0 "$owner_pid" 2>/dev/null; then
    actual_identity="$(run_lock_process_identity "$owner_pid")"
    [ "$actual_identity" != "$owner_identity" ] || return 1
  elif [ "$owner_missing" = true ]; then
    age=$(( $(date +%s) - $(run_lock_mtime "$lock_dir") ))
    [ "$age" -ge 60 ] || return 1
  fi

  stale="${lock_dir}.stale.$$"
  mv "$lock_dir" "$stale" 2>/dev/null || return 1
  if ! mkdir "$lock_dir" 2>/dev/null; then
    mv "$stale" "$lock_dir" 2>/dev/null || true
    return 1
  fi
  if ! write_run_lock_owner "$lock_dir"; then
    rmdir "$lock_dir" 2>/dev/null || true
    mv "$stale" "$lock_dir" 2>/dev/null || true
    return 1
  fi
  rm -f "$stale/owner" "$stale"/.owner.*
  rmdir "$stale" 2>/dev/null || true
}

release_run_lock() {
  local lock_dir="$1" owner_pid owner_identity
  owner_pid="$(sed -n '1p' "$lock_dir/owner" 2>/dev/null || true)"
  owner_identity="$(sed -n '2p' "$lock_dir/owner" 2>/dev/null || true)"
  [ "$owner_pid" = "$$" ] || return 0
  [ "$owner_identity" = "$(run_lock_process_identity "$$")" ] || return 0
  rm -f "$lock_dir/owner"
  rmdir "$lock_dir" 2>/dev/null || true
}
