"""Shared completion-evidence rules for article publishing."""

from __future__ import annotations

from collections.abc import Iterable
import re
from urllib.parse import urlparse


ACTIVE_REQUIRED_WITHOUT_ZENN = {
    ("note", "ja"),
    ("substack", "ja"),
    ("substack", "en"),
    ("x-article", "ja"),
}
ACTIVE_REQUIRED_LIVE = ACTIVE_REQUIRED_WITHOUT_ZENN

# Compatibility for already-persisted exact-eight runs. New publication state
# uses ACTIVE_REQUIRED_LIVE and never requires dormant destinations.
LEGACY_REQUIRED_WITHOUT_ZENN = {
    ("note", "ja"),
    ("devto", "en"),
    ("substack", "ja"),
    ("substack", "en"),
    ("x-article", "ja"),
    ("x-article", "en"),
    ("x-post", "ja"),
}
LEGACY_REQUIRED_LIVE = LEGACY_REQUIRED_WITHOUT_ZENN | {("zenn-article", "ja")}
REQUIRED_WITHOUT_ZENN = ACTIVE_REQUIRED_WITHOUT_ZENN
REQUIRED_LIVE = ACTIVE_REQUIRED_LIVE


def valid_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_live_set(
    rows: Iterable[dict], run_id: str, required: set[tuple[str, str]]
) -> tuple[bool, list[dict], str | None]:
    """Validate all published rows for a run as one exact, single-topic set."""
    candidates = [row for row in rows if row.get("run_id") == run_id and row.get("published") is True]
    live = [
        row for row in candidates
        if row.get("reality_gate") == "PASS"
        and valid_http_url(row.get("live_url"))
        and isinstance(row.get("topic_id"), str)
        and row["topic_id"]
    ]
    live = [
        row for row in live
        if (row.get("platform"), row.get("lang")) in required
    ]
    if len(live) != len(required):
        return False, live, None
    pairs = [(row.get("platform"), row.get("lang")) for row in live]
    topics = {row["topic_id"] for row in live}
    if set(pairs) != required or len(pairs) != len(set(pairs)) or len(topics) != 1:
        return False, live, None
    return True, live, next(iter(topics))


def terminal_artifact_complete(artifact: dict, run_id: str, topic_id: str | None) -> bool:
    """Recognize terminal evidence without consulting mutable source or the network."""
    slug = artifact.get("slug")
    canonical_url = artifact.get("live_url")
    return (
        isinstance(slug, str)
        and re.fullmatch(r"[a-z0-9_-]{12,50}", slug) is not None
        and isinstance(canonical_url, str)
        and re.fullmatch(
            rf"https://zenn\.dev/[A-Za-z0-9_-]+/articles/{re.escape(slug)}",
            canonical_url,
        )
        is not None
        and artifact.get("status") == "complete"
        and artifact.get("run_id") == run_id
        and topic_id is not None
        and artifact.get("topic_id") == topic_id
        and artifact.get("live_url") == canonical_url
        and artifact.get("completed_live_url") == canonical_url
        and artifact.get("completion_check") == "passed"
        and artifact.get("notification_status") == "sent"
        and isinstance(artifact.get("heartbeat_at"), str)
        and bool(artifact["heartbeat_at"])
    )
