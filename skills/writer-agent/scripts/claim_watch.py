#!/usr/bin/env python3
"""Fetch configured X, GitHub, and RSS sources into the durable claim store."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from claim_store import ClaimStore, SOURCE_KINDS, _text, _timestamp  # noqa: E402


class SourceUnavailable(RuntimeError):
    """A configured source could not provide trustworthy bytes this wake."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _item_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _plain(value: Any, *, limit: int = 4000) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = html.unescape(text)
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = " ".join(text.split())
    return text[:limit]


def _base(source: dict[str, Any], observed_at: str) -> dict[str, Any]:
    return {
        "source_kind": source["kind"],
        "source_name": _text(source.get("source_name"), "source_name"),
        "reader_job": _text(source.get("reader_job"), "reader_job"),
        "observed_at": observed_at,
    }


def _iso_rfc822(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_x(payload: bytes, source: dict[str, Any], observed_at: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceUnavailable("X returned invalid JSON") from error
    if isinstance(value, dict) and value.get("ok") is True:
        value = value.get("data")
    if not isinstance(value, list):
        raise SourceUnavailable("X returned no tweet list")
    candidates: list[dict[str, Any]] = []
    for item in value[: int(source.get("limit", 10))]:
        if not isinstance(item, dict) or item.get("isRetweet"):
            continue
        tweet_id = str(item.get("id") or "").strip()
        text = _plain(item.get("text"))
        meaningful_text = " ".join(re.sub(r"https?://\S+", " ", text).split())
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        handle = str(author.get("screenName") or source.get("handle") or "").lstrip("@")
        if not tweet_id or len(meaningful_text) < 20 or not handle:
            continue
        raw = _item_bytes(item)
        candidates.append(
            {
                **_base(source, observed_at),
                "url": f"https://x.com/{handle}/status/{tweet_id}",
                "title": _plain(item.get("articleTitle") or text, limit=180),
                "claim": text,
                "evidence_excerpt": _plain(item.get("articleText") or text),
                "published_at": item.get("createdAtISO") or None,
                "retrieved_sha256": _digest(raw),
            }
        )
    if not candidates:
        raise SourceUnavailable("X returned no usable original posts")
    return candidates


def _release_claim(repo: str, item: dict[str, Any]) -> str:
    body = str(item.get("body") or "")
    for line in body.splitlines():
        cleaned = re.sub(r"^\s*[-*+]\s+", "", line).strip()
        if cleaned != line.strip() and cleaned:
            return _plain(cleaned)
    tag = _plain(item.get("tag_name") or item.get("name"), limit=120)
    return f"{repo} published release {tag}."


def parse_github_releases(
    payload: bytes, source: dict[str, Any], observed_at: str
) -> list[dict[str, Any]]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceUnavailable("GitHub returned invalid JSON") from error
    if not isinstance(value, list):
        raise SourceUnavailable("GitHub returned no release list")
    repo = _text(source.get("repo"), "repo")
    candidates: list[dict[str, Any]] = []
    for item in value[: int(source.get("limit", 10))]:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        url = item.get("html_url")
        title = _plain(item.get("name") or item.get("tag_name"), limit=180)
        claim = _release_claim(repo, item)
        evidence = _plain(item.get("body") or claim)
        if not isinstance(url, str) or not title or not claim or not evidence:
            continue
        raw = _item_bytes(item)
        candidates.append(
            {
                **_base(source, observed_at),
                "url": url,
                "title": title,
                "claim": claim,
                "evidence_excerpt": evidence,
                "published_at": item.get("published_at") or None,
                "retrieved_sha256": _digest(raw),
            }
        )
    if not candidates:
        raise SourceUnavailable("GitHub returned no usable stable releases")
    return candidates


def _element_text(item: ET.Element, local_name: str) -> str:
    for child in list(item):
        if child.tag.rsplit("}", 1)[-1].lower() == local_name.lower():
            return "".join(child.itertext()).strip()
    return ""


def parse_rss(payload: bytes, source: dict[str, Any], observed_at: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise SourceUnavailable("RSS returned invalid XML") from error
    items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    candidates: list[dict[str, Any]] = []
    for item in items[: int(source.get("limit", 10))]:
        title = _plain(_element_text(item, "title"), limit=180)
        link = _element_text(item, "link")
        if not link:
            for child in list(item):
                if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        description = (
            _element_text(item, "description")
            or _element_text(item, "summary")
            or _element_text(item, "content")
        )
        evidence = _plain(description or title)
        published_raw = (
            _element_text(item, "pubDate")
            or _element_text(item, "published")
            or _element_text(item, "updated")
        )
        if not title or not link or not evidence:
            continue
        raw = ET.tostring(item, encoding="utf-8")
        candidates.append(
            {
                **_base(source, observed_at),
                "url": link,
                "title": title,
                "claim": title,
                "evidence_excerpt": evidence,
                "published_at": _iso_rfc822(published_raw) or published_raw or None,
                "retrieved_sha256": _digest(raw),
            }
        )
    if not candidates:
        raise SourceUnavailable("RSS returned no usable entries")
    return candidates


def _process_group_members_for_id(process_group: int) -> list[int] | None:
    """Read live, non-zombie members for a process group, bounded by 200ms."""

    try:
        pipe = os.popen("ps -axo pid=,pgid=,stat=", "r")
        ready, _, _ = select.select([pipe], [], [], 0.2)
        if not ready:
            pipe.close()
            return None
        output = pipe.read()
        pipe.close()
    except (OSError, ValueError):
        return None
    members: list[int] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid, group = (int(value) for value in fields[:2])
        except ValueError:
            continue
        if group == process_group and pid != os.getpid() and not fields[2].startswith("Z"):
            members.append(pid)
    return sorted(set(members))


def _process_group_snapshot(
    process: subprocess.Popen[bytes],
) -> tuple[int | None, tuple[int, ...] | None]:
    """Snapshot PGID and members before any cleanup signal can reap the leader."""

    try:
        process_group = os.getpgid(process.pid)
    except OSError:
        return None, None
    members = _process_group_members_for_id(process_group)
    if members is not None and process.pid not in members:
        members.append(process.pid)
    return process_group, tuple(members) if members is not None else None


def _process_group_survivors(
    snapshot: tuple[int | None, tuple[int, ...] | None],
) -> list[int] | None:
    process_group, initial_members = snapshot
    if process_group is None:
        return None
    current = _process_group_members_for_id(process_group)
    if current is None:
        return list(initial_members) if initial_members is not None else None
    return current


def _best_effort_process_group_signal(
    process: subprocess.Popen[bytes],
    sig: signal.Signals,
    *,
    snapshot: tuple[int | None, tuple[int, ...] | None] | None = None,
) -> None:
    """Stop a timed-out source without letting cleanup errors escape the boundary."""

    try:
        os.killpg(process.pid, sig)
        return
    except ProcessLookupError:
        return
    except OSError:
        # A launchd/session boundary can deny process-group signalling even though
        # the direct child remains ours.  Enumerate this fresh session's members and
        # signal each one before falling back to the direct child.  This prevents a
        # nested source process from surviving a denied group signal.
        if snapshot is None:
            snapshot = _process_group_snapshot(process)
        members = _process_group_survivors(snapshot)
        if members is None:
            members = list(snapshot[1] or ())
        for pid in members:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        try:
            if sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except OSError:
            pass


def _reap_process_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
) -> bool:
    """Reap a timed-out source without ever waiting forever."""

    try:
        process.communicate(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _run(command: list[str], *, timeout: float = 60) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise SourceUnavailable(type(error).__name__) from error
    process_group_snapshot = _process_group_snapshot(process)
    try:
        stdout, _stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _best_effort_process_group_signal(
            process,
            signal.SIGTERM,
            snapshot=process_group_snapshot,
        )
        reaped = _reap_process_bounded(process, timeout=0.2)
        survivors = _process_group_survivors(process_group_snapshot)
        cleanup_incomplete = survivors is None
        if not reaped or survivors:
            _best_effort_process_group_signal(
                process,
                signal.SIGKILL,
                snapshot=process_group_snapshot,
            )
            second_reap = _reap_process_bounded(process, timeout=0.2)
            reaped = reaped or second_reap
            survivors = _process_group_survivors(process_group_snapshot)
            cleanup_incomplete = cleanup_incomplete or survivors is None or bool(survivors)
        if not reaped or cleanup_incomplete:
            raise SourceUnavailable(
                "source command timeout; process cleanup incomplete"
            ) from error
        raise SourceUnavailable("source command timeout") from error
    if process.returncode != 0 or not stdout.strip():
        raise SourceUnavailable(
            f"command returned rc={process.returncode} with no trustworthy output"
        )
    return stdout


def fetch_x(
    source: dict[str, Any], *,
    command_runner: Callable[..., bytes] = _run,
    cloak_python: Path | None = None,
    bridge: Path | None = None,
) -> bytes:
    handle = _text(source.get("handle"), "handle").lstrip("@")
    if cloak_python is None:
        cloak_python = Path(
            os.environ.get(
                "WRITER_CLOAK_PYTHON",
                os.environ.get("WRITER_BROWSER_PYTHON", sys.executable),
            )
        ).expanduser()
    if bridge is None:
        bridge = SCRIPT_DIR / "x_authenticated_cli.py"
    return command_runner(
        [
            str(cloak_python), str(bridge), "--handle", handle,
            "--limit", str(int(source.get("limit", 10))),
            "--cdp-url", os.environ.get("WRITER_CDP_URL", "http://127.0.0.1:9222"),
        ],
        timeout=45,
    )


def fetch_github(source: dict[str, Any]) -> bytes:
    repo = _text(source.get("repo"), "repo")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError("repo must be owner/name")
    return _run(["gh", "api", f"repos/{repo}/releases?per_page={int(source.get('limit', 10))}"])


def fetch_rss(source: dict[str, Any]) -> bytes:
    url = _text(source.get("url"), "url")
    if not url.startswith("https://"):
        raise ValueError("RSS URL must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "Writer-Agent-Claim-Watch/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            value = response.read(4 * 1024 * 1024 + 1)
    except OSError as error:
        raise SourceUnavailable(type(error).__name__) from error
    if not value or len(value) > 4 * 1024 * 1024:
        raise SourceUnavailable("RSS response is empty or exceeds the byte cap")
    return value


PARSERS = {"x": parse_x, "github": parse_github_releases, "rss": parse_rss}
DEFAULT_FETCHERS = {"x": fetch_x, "github": fetch_github, "rss": fetch_rss}


def _validate_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("claim watch config version must be 1")
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("claim watch config requires sources")
    ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("claim watch source must be an object")
        source_id = _text(source.get("id"), "source.id")
        kind = _text(source.get("kind"), "source.kind").lower()
        if kind not in SOURCE_KINDS:
            raise ValueError("source.kind must be x, github, or rss")
        if source_id in ids:
            raise ValueError("claim watch source ids must be unique")
        ids.add(source_id)
        _text(source.get("source_name"), "source_name")
        _text(source.get("reader_job"), "reader_job")
        result.append(source)
    return result


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def run_watch(
    config: dict[str, Any],
    database: Path | str,
    receipt_path: Path | str,
    *,
    observed_at: str,
    fetchers: dict[str, Callable[[dict[str, Any]], bytes]] | None = None,
) -> dict[str, Any]:
    checked_at = str(_timestamp(observed_at, "observed_at"))
    sources = _validate_config(config)
    selected_fetchers = dict(DEFAULT_FETCHERS)
    if fetchers:
        selected_fetchers.update(fetchers)
    store = ClaimStore(database)
    rows: list[dict[str, Any]] = []
    inserted_total = 0
    deduped_total = 0
    for source in sources:
        source_id = str(source["id"])
        kind = str(source["kind"])
        try:
            payload = selected_fetchers[kind](source)
            candidates = PARSERS[kind](payload, source, checked_at)
            inserted = 0
            deduped = 0
            for candidate in candidates:
                result = store.ingest(candidate)
                if result["inserted"]:
                    inserted += 1
                else:
                    deduped += 1
            inserted_total += inserted
            deduped_total += deduped
            rows.append(
                {
                    "id": source_id, "kind": kind, "status": "OK",
                    "fetched": len(candidates), "inserted": inserted, "deduped": deduped,
                }
            )
        except SourceUnavailable as error:
            rows.append(
                {
                    "id": source_id, "kind": kind, "status": "SOURCE_UNAVAILABLE",
                    "error_class": type(error).__name__, "reason": str(error),
                    "fetched": 0, "inserted": 0, "deduped": 0,
                }
            )
    ok = sum(row["status"] == "OK" for row in rows)
    receipt = {
        "version": 1,
        "checked_at": checked_at,
        "sources": rows,
        "totals": {
            "sources": len(rows), "ok": ok, "unavailable": len(rows) - ok,
            "inserted": inserted_total, "deduped": deduped_total,
        },
    }
    _atomic_json(Path(receipt_path), receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--observed-at",
        default=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    args = parser.parse_args(argv)
    observed_at = args.observed_at() if callable(args.observed_at) else args.observed_at
    config = json.loads(args.config.read_text(encoding="utf-8"))
    receipt = run_watch(config, args.db, args.receipt, observed_at=observed_at)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["totals"]["ok"] else 75


if __name__ == "__main__":
    raise SystemExit(main())
