#!/usr/bin/env python3
"""note-draft-ledger.py — local idempotency ledger for publish-note.sh.

Purely local (NO network access, NO note_mcp import): decides whether a given
markdown file should reuse an existing note.com draft key or create a new one,
and records the outcome after a successful publish so the next run of the same
markdown file reuses the same draft instead of creating a new one every time.

Subcommands:
  resolve --md <path> [--key <key>] [--new-draft] [--ledger <path>]
      Decision order (first match wins):
        1. --new-draft            -> action=create   (escape hatch: ignore the ledger)
        2. --key <k> given        -> action=update, article_id=<k>  (explicit override wins)
        3. ledger has an entry for the md path -> action=update, article_id=<stored key>
        4. otherwise               -> action=create   (no prior draft known for this file)
      Prints one line of JSON to stdout:
        {"action": "update"|"create", "article_id": <key>|null,
         "key_format_ok": <bool>|null, "reason": "<why>"}
      key_format_ok is the note.com "n..." key-format check (see note_mcp
      _is_article_key_format: starts with 'n', not purely digits) — null when
      action=="create" (no article_id to check). This is a LOCAL format check
      only; it does not verify the article still exists or is still a draft on
      note.com — the caller must do that live, over the network, before trusting
      article_id for an update.

  record --md <path> --key <key> [--num <num>] [--title <title>] [--ledger <path>]
      Atomically upserts {<absolute md path>: {key, num, title, updated_at}}
      into the ledger JSON (same-dir temp file + os.replace, so a concurrent
      reader never observes a partially-written file).

Ledger path default: $WRITER_STATE_DIR/note-work/draft-ledger.json
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time

DEFAULT_LEDGER = os.path.join(
    os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")),
    "note-work/draft-ledger.json",
)


def _load(ledger_path: str) -> dict:
    if not os.path.exists(ledger_path):
        return {}
    try:
        with open(ledger_path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt/partial ledger must never crash the publish pipeline — treat it as empty
        # (worst case: one extra draft gets created, which is exactly today's pre-ledger behavior).
        return {}


class _LedgerLock:
    """Cross-PROCESS mutual exclusion around a read-modify-write on the ledger.

    Each `record` invocation is a separate subprocess (publish-note.sh spawns one per publish
    run); os.replace() alone only makes a SINGLE write atomic, it does not prevent two concurrent
    invocations from both reading the same stale snapshot and then each overwriting the other's
    update on write (a lost-update race, not a torn-file race — verified by an 8-way concurrent
    `record()` test that silently lost 2 of 8 entries before this lock was added). fcntl.flock on
    a companion `.lock` file serializes the whole read+modify+write critical section across
    processes; the lock file itself is never the data file, so a crash mid-write still leaves the
    real ledger file in the last atomically-written (i.e. always well-formed) state.
    """

    def __init__(self, ledger_path: str):
        d = os.path.dirname(ledger_path) or "."
        os.makedirs(d, exist_ok=True)
        self._lock_path = ledger_path + ".lock"
        self._fh = None

    def __enter__(self):
        self._fh = open(self._lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        return False


def _atomic_write(ledger_path: str, data: dict) -> None:
    d = os.path.dirname(ledger_path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".draft-ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, ledger_path)
    except Exception:
        os.unlink(tmp_path)
        raise


def _is_key_format(article_id: str | None) -> bool:
    # Same rule as note_mcp.api.articles._is_article_key_format: "n" + at least one non-purely-
    # numeric char. A bare numeric id (e.g. "123") is NOT a key and must not be trusted for
    # update_article without a live status check (see note-draft-ledger.py module docstring).
    return bool(article_id) and article_id.startswith("n") and not article_id.isdigit()


def cmd_resolve(args: argparse.Namespace) -> None:
    md_abs = os.path.abspath(args.md)

    if args.new_draft:
        print(json.dumps({"action": "create", "article_id": None, "key_format_ok": None, "reason": "new-draft-flag"}))
        return

    if args.key:
        print(json.dumps({
            "action": "update", "article_id": args.key,
            "key_format_ok": _is_key_format(args.key), "reason": "explicit-key",
        }))
        return

    ledger = _load(args.ledger)
    entry = ledger.get(md_abs)
    if entry and entry.get("key"):
        k = entry["key"]
        print(json.dumps({
            "action": "update", "article_id": k,
            "key_format_ok": _is_key_format(k), "reason": "ledger",
        }))
        return

    print(json.dumps({"action": "create", "article_id": None, "key_format_ok": None, "reason": "no-ledger-entry"}))


def cmd_record(args: argparse.Namespace) -> None:
    if not args.account:
        raise ValueError("NOTE_URLNAME or --account is required")
    md_abs = os.path.abspath(args.md)
    # Lock spans load+merge+write: without it, two concurrent `record` processes can both load
    # the same pre-update snapshot and each write back a copy missing the other's entry (see
    # _LedgerLock docstring — this is a real, previously-reproduced bug, not theoretical).
    with _LedgerLock(args.ledger):
        ledger = _load(args.ledger)
        ledger[md_abs] = {
            "key": args.key,
            "num": args.num or "",
            "title": args.title or "",
            "account": args.account,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _atomic_write(args.ledger, ledger)
    print(json.dumps({"ok": True, "md": md_abs, "key": args.key}))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("resolve")
    pr.add_argument("--md", required=True)
    pr.add_argument("--key", default="")
    pr.add_argument("--new-draft", action="store_true")
    pr.set_defaults(func=cmd_resolve)

    pc = sub.add_parser("record")
    pc.add_argument("--md", required=True)
    pc.add_argument("--key", required=True)
    pc.add_argument("--num", default="")
    pc.add_argument("--title", default="")
    pc.add_argument("--account", default=os.environ.get("NOTE_URLNAME", ""))
    pc.set_defaults(func=cmd_record)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — this script's failures must be visible, never silent
        print(f"FATAL note-draft-ledger.py: {e}", file=sys.stderr)
        sys.exit(1)
