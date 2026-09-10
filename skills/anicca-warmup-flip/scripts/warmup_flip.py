#!/usr/bin/env python3
"""Advance Postiz warmups and durably deliver graduation notifications."""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path


def _atomic_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def _recover(journal: Path, state: Path, pending: Path) -> None:
    if not journal.exists():
        return
    transaction = json.loads(journal.read_text())
    _atomic_text(state, transaction["state"])
    if transaction.get("message"):
        _atomic_text(pending, transaction["message"] + "\n")
    journal.unlink()


def run(state: Path, sender: Path, today: datetime.date | None = None) -> int:
    today = today or datetime.date.today()
    lock_path = state.parent.parent / "warmup-flip.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = state.parent.parent / "pending-telegram.txt"
    journal = state.parent.parent / "warmup-flip-transaction.json"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover(journal, state, pending)
        if pending.exists():
            result = subprocess.run(["bash", str(sender), pending.read_text().rstrip()], check=False)
            if result.returncode:
                return result.returncode
            pending.unlink()
        data = json.loads(state.read_text())
        integrations = data.get("integrations")
        if not isinstance(integrations, list) or not all(isinstance(item, dict) for item in integrations):
            raise ValueError("Postiz integration state is invalid")
        flipped: list[str] = []
        seeded: list[str] = []
        for item in integrations:
            if item.get("warmup_phase") != "warmup":
                continue
            started = item.get("warmup_started_at")
            if not started:
                item["warmup_started_at"] = today.isoformat()
                seeded.append(item.get("handle", "?"))
                continue
            try:
                age_days = (today - datetime.date.fromisoformat(started)).days
            except (TypeError, ValueError):
                continue
            if age_days >= 7:
                item["warmup_phase"] = "live"
                flipped.append(f"{item.get('handle', '?')} ({age_days}d)")

        summary = f"warmup-flip: seeded={len(seeded)} flipped={len(flipped)}"
        message = ""
        if flipped:
            message = "🚀 Warmup graduation\n" + summary + "\n" + "\n".join(flipped)
        if seeded or flipped:
            data["updated_at"] = datetime.datetime.now().astimezone().isoformat()
            state_body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
            _atomic_text(journal, json.dumps({"id": uuid.uuid4().hex, "state": state_body, "message": message}))
            _recover(journal, state, pending)
        print(summary)
        for handle in seeded:
            print(f"  📌 seeded warmup_started_at={today}: {handle}")
        for handle in flipped:
            print(f"  🚀 flipped to live: {handle}")
        if pending.exists():
            result = subprocess.run(["bash", str(sender), pending.read_text().rstrip()], check=False)
            if result.returncode:
                return result.returncode
            pending.unlink()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--sender", type=Path, required=True)
    parser.add_argument("--today", type=datetime.date.fromisoformat)
    args = parser.parse_args()
    return run(args.state, args.sender, args.today)


if __name__ == "__main__":
    raise SystemExit(main())
