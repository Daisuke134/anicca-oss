#!/usr/bin/env python3
"""Mac-local Affiliate wake and append-only receipts."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from job_journal import (
    JobStateError, reconcile_effect, resume_effect, start_effect,
    unresolved_effect, verify_effect,
)
from provider_cli import ProviderError, observe, poll, read_login_credentials, resume
from program_registry import TTS_PLACEMENT, apply_getresponse, elevenlabs_link_action
from acquisition_decision import advance as advance_acquisition_decision
from funnel_decision import (
    advance as advance_funnel_decision,
    advance_distribution_route,
)
from cta_instrumentation import (
    advance as advance_cta_instrumentation, join_provider_interval, observe_clicks,
    observe_entries,
)
from runtime_guard import runtime_guard
import x_profile_cli


SYSTEME_LOGIN = "https://systeme.io/en/login"
ELEVENLABS_HOME = "https://elevenlabs.io/app/home"
RUN_OWNER_LABEL = "ai.anicca.affiliate-loop"
RELEASE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
AFFILIATE_OWNER_LABELS = (
    "ai.anicca.affiliate-browser",
    "ai.anicca.affiliate-impact-browser",
    "ai.anicca.affiliate-x-browser",
    "ai.anicca.affiliate-source-refresh",
    "ai.anicca.affiliate-composition",
    RUN_OWNER_LABEL,
)
QUARANTINE_FAILURE_THRESHOLD = 3
QUARANTINABLE_EFFECTS = {
    "EXTERNAL_WRITE", "PROVIDER_LINK_WRITE", "PUBLICATION_WRITE",
}
EXTERNAL_ACTION_DAILY_CAP = None
EXTERNAL_COST_DAILY_CAP_MINOR = 500
COST_BUDGET_JST = ZoneInfo("Asia/Tokyo")
REPOST_PROPOSAL_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REPOST_PLACEMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
REPOST_CONSUMPTION_STATES = {"EFFECT_STARTED", "POSTED", "UNVERIFIED", "NO_EFFECT"}


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def append(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def rotate_tool_attempt_receipts(path, *, max_bytes=32 * 1024 * 1024,
                                 recent_no_effect_bytes=8 * 1024 * 1024,
                                 keep_archives=4):
    result = {"archived_rows": 0, "protected_rows": 0, "active_rows": 0}
    if not path.is_file() or path.stat().st_size <= max_bytes:
        return result
    lines = path.read_bytes().splitlines(keepends=True)
    entries = []
    for line in lines:
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            entries.append((line, False))
            continue
        entries.append((line, row.get("effect_certainty") in {
            "NO_EFFECT", "READ_ONLY_CONFIRMED",
        }))
    disposable_indices = [index for index, (_, disposable) in enumerate(entries)
                          if disposable]
    recent_indices = set()
    recent_bytes = 0
    while (disposable_indices
           and recent_bytes + len(entries[disposable_indices[-1]][0])
           <= recent_no_effect_bytes):
        index = disposable_indices.pop()
        line = entries[index][0]
        recent_indices.add(index)
        recent_bytes += len(line)
    archived = [entries[index][0] for index in disposable_indices]
    active = [line for index, (line, disposable) in enumerate(entries)
              if not disposable or index in recent_indices]
    result["protected_rows"] = sum(not disposable for _, disposable in entries)
    result["active_rows"] = len(active)
    if not archived:
        return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = path.with_name(f"tool-attempt-receipts.archive-{stamp}.jsonl.gz")
    compressed = gzip.compress(b"".join(archived), mtime=0)
    archive_tmp = archive.with_name(f".{archive.name}.{os.getpid()}.tmp")
    active_tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with archive_tmp.open("xb") as stream:
            os.chmod(archive_tmp, 0o600)
            stream.write(compressed)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(archive_tmp, archive)
        with active_tmp.open("xb") as stream:
            os.chmod(active_tmp, 0o600)
            stream.write(b"".join(active))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(active_tmp, path)
    finally:
        archive_tmp.unlink(missing_ok=True)
        active_tmp.unlink(missing_ok=True)
    archives = sorted(path.parent.glob("tool-attempt-receipts.archive-*.jsonl.gz"))
    for old in archives[:-max(0, keep_archives)] if keep_archives else archives:
        old.unlink()
    result["archived_rows"] = len(archived)
    return result


def _append_unique_unlocked(path, value, identity):
    with path.open("a+", encoding="utf-8") as stream:
        stream.seek(0)
        for line in stream:
            try:
                existing = json.loads(line)
            except ValueError:
                continue
            if all(existing.get(key) == value[key] for key in identity):
                return False
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        return True


def append_unique(path, value, identity):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.name == "tool-attempt-receipts.jsonl":
        lock_path = path.with_name(f"{path.name}.lock")
        with lock_path.open("a", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            rotate_tool_attempt_receipts(path)
            return _append_unique_unlocked(path, value, identity)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        for line in stream:
            try:
                existing = json.loads(line)
            except ValueError:
                continue
            if all(existing.get(key) == value[key] for key in identity):
                return False
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        return True


def json_rows(path):
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def observe_x_growth(state, cdp_port, inspector=x_profile_cli.inspect):
    config = x_profile_cli.load_config(Path(__file__).resolve().parents[1], "en")
    profile = inspector(SimpleNamespace(
        cdp_host="127.0.0.1", cdp_port=cdp_port, state=state,
    ), config)
    followers = x_profile_cli.exact_profile_count(
        profile.get("followers_text"), "followers"
    )
    following = x_profile_cli.exact_profile_count(
        profile.get("following_text"), "following"
    )
    core = {
        "schema_version": 1,
        "receipt_type": "X_FOLLOWER_BASELINE",
        "handle": config["handle"],
        "rendered_url": profile.get("rendered_url"),
        "followers": {"count": followers,
                      "state": "EXACT" if followers is not None else "UNAVAILABLE_EXACT"},
        "following": {"count": following,
                      "state": "EXACT" if following is not None else "UNAVAILABLE_EXACT"},
    }
    transition_id = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {
        **core, "transition_id": transition_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    changed = append_unique(
        state / "x-growth" / "follower-baselines.jsonl", receipt, ("transition_id",)
    )
    atomic_json(state / "x-growth" / "latest-followers.json", receipt)
    return {**receipt,
            "state": "OBSERVED" if followers is not None else "UNAVAILABLE_EXACT",
            "changed": changed}


def observe_x_post_metrics(state, cdp_port, inspector=x_profile_cli.inspect_post_metrics):
    repost_root = Path(os.environ.get(
        "AFFILIATE_REPOST_STATE_DIR", Path.home() / "loops" / "x-repost"
    )).expanduser()
    posted_path = repost_root / "posted.jsonl"
    if not posted_path.is_file():
        return {"state": "WAITING_FOR_AFFILIATE_POST", "changed": False}
    raw = posted_path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    selected = next((
        row for row in reversed(rows)
        if row.get("kind") in {"affiliate_distribution", "affiliate_distribution_quote"}
        and isinstance(row.get("affiliate_job_id"), str)
        and isinstance(row.get("affiliate_placement_id"), str)
        and is_owned_article_url(row.get("affiliate_owned_article_url"))
        and isinstance(row.get("post_url"), str)
    ), None)
    if selected is None:
        return {"state": "WAITING_FOR_AFFILIATE_POST", "changed": False}
    distribution_placement_id = selected["affiliate_placement_id"]
    placement_id = distribution_placement_id
    try:
        job = json.loads((
            state / "x-distribution-jobs" / f'{selected["affiliate_job_id"]}.json'
        ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job = {}
    lineage = job.get("experiment_lineage") if isinstance(job, dict) else None
    if (
        isinstance(lineage, dict) and lineage.get("kind") == "EXPERIMENT"
        and isinstance(lineage.get("control_placement_id"), str)
        and REPOST_PLACEMENT_ID_PATTERN.fullmatch(lineage["control_placement_id"])
    ):
        placement_id = lineage["control_placement_id"]
    config = x_profile_cli.load_config(Path(__file__).resolve().parents[1], "en")
    metrics = inspector(SimpleNamespace(
        cdp_host="127.0.0.1", cdp_port=cdp_port, state=state,
    ), config, selected["post_url"])
    metric_receipts = {
        key: {"count": metrics.get(key),
              "state": "EXACT" if isinstance(metrics.get(key), int) else "UNAVAILABLE_EXACT"}
        for key in ("views", "replies", "reposts", "likes", "bookmarks")
    }
    core = {
        "schema_version": 1,
        "receipt_type": "X_POST_METRICS_BASELINE",
        "post_url": selected["post_url"],
        "job_id": selected["affiliate_job_id"],
        "placement_id": placement_id,
        "distribution_placement_id": distribution_placement_id,
        "owned_article_url": selected["affiliate_owned_article_url"],
        "source_posted_sha256": hashlib.sha256(raw).hexdigest(),
        "impressions": metric_receipts.pop("views"),
        **metric_receipts,
    }
    transition_id = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "transition_id": transition_id,
               "observed_at": datetime.now(timezone.utc).isoformat()}
    changed = append_unique(
        state / "x-growth" / "post-metrics.jsonl", receipt, ("transition_id",)
    )
    atomic_json(state / "x-growth" / "latest-post-metrics.json", receipt)
    return {**receipt, "state": "OBSERVED", "changed": changed}


def observe_x_channel_ledger(state, cdp_port, inspector=x_profile_cli.inspect_post_metrics):
    follower_rows = json_rows(state / "x-growth" / "follower-baselines.jsonl")
    exact_followers = [
        row for row in follower_rows
        if isinstance((row.get("followers") or {}).get("count"), int)
        and (row.get("followers") or {}).get("state") == "EXACT"
    ]
    if not exact_followers:
        return {"state": "WAITING_FOR_FOLLOWER_BASELINE", "changed": False}
    follower_delta = (
        exact_followers[-1]["followers"]["count"]
        - exact_followers[-2]["followers"]["count"]
        if len(exact_followers) >= 2 else None
    )
    money_path = state / "x-growth" / "latest-post-metrics.json"
    if not money_path.is_file():
        return {"state": "WAITING_FOR_MONETIZATION_METRICS", "changed": False}
    money = json.loads(money_path.read_text(encoding="utf-8"))
    metric_names = ("impressions", "replies", "reposts", "likes", "bookmarks")
    placement_rows = [
        row for row in json_rows(state / "x-growth" / "post-metrics.jsonl")
        if row.get("placement_id") == money.get("placement_id")
        and isinstance(row.get("post_url"), str)
    ]
    if placement_rows:
        maxima = {}
        for row in placement_rows:
            post = maxima.setdefault(row["post_url"], {})
            for name in metric_names:
                count = (row.get(name) or {}).get("count")
                if isinstance(count, int):
                    post[name] = max(post.get(name, count), count)
        money = {
            **money,
            **{
                name: {
                    "count": sum(post.get(name, 0) for post in maxima.values()),
                    "state": "EXACT",
                }
                for name in metric_names
            },
            "distribution_post_count": len(maxima),
        }
    repost_root = Path(os.environ.get(
        "AFFILIATE_REPOST_STATE_DIR", Path.home() / "loops" / "x-repost"
    )).expanduser()
    posted_path = repost_root / "posted.jsonl"
    raw = posted_path.read_bytes()
    posted = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    growth = next((
        row for row in reversed(posted)
        if row.get("kind") in {"quote", "reply", "original"}
        and isinstance(row.get("post_url"), str)
    ), None)
    if growth is None:
        return {"state": "WAITING_FOR_GROWTH_POST", "changed": False}
    config = x_profile_cli.load_config(Path(__file__).resolve().parents[1], "en")
    observed = inspector(SimpleNamespace(
        cdp_host="127.0.0.1", cdp_port=cdp_port, state=state,
    ), config, growth["post_url"])

    def metrics(value):
        return {
            key if key != "views" else "impressions": {
                "count": value.get(key),
                "state": "EXACT" if isinstance(value.get(key), int) else "UNAVAILABLE_EXACT",
            }
            for key in ("views", "replies", "reposts", "likes", "bookmarks")
        }

    core = {
        "schema_version": 1,
        "receipt_type": "X_GROWTH_MONETIZATION_LEDGER",
        "handle": config["handle"],
        "followers_before_transition_id": (
            exact_followers[-2].get("transition_id") if len(exact_followers) >= 2 else None
        ),
        "followers_after_transition_id": exact_followers[-1].get("transition_id"),
        "followers_delta": {
            "count": follower_delta,
            "state": "EXACT" if follower_delta is not None else "NO_PRIOR_BASELINE",
        },
        "lanes": {
            "growth": {
                "post_url": growth["post_url"], "kind": growth["kind"],
                **metrics(observed),
            },
            "monetization": {
                key: money.get(key) for key in (
                    "post_url", "job_id", "placement_id", "impressions",
                    "replies", "reposts", "likes", "bookmarks",
                    "distribution_post_count",
                )
            },
        },
        "source_posted_sha256": hashlib.sha256(raw).hexdigest(),
        "monetization_metrics_transition_id": money.get("transition_id"),
        "money_state": "NON_MONEY",
    }
    transition_id = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "transition_id": transition_id,
               "observed_at": datetime.now(timezone.utc).isoformat()}
    changed = append_unique(
        state / "x-growth" / "channel-ledger.jsonl", receipt, ("transition_id",)
    )
    atomic_json(state / "x-growth" / "latest-channel-ledger.json", receipt)
    return {**receipt, "state": "OBSERVED", "changed": changed}


def build_money_funnel_row(state):
    channel_path = state / "x-growth" / "latest-channel-ledger.json"
    ledger_path = state / "placement-ledger.json"
    if not channel_path.is_file() or not ledger_path.is_file():
        return {"state": "WAITING_FOR_FUNNEL_SOURCES", "changed": False}
    channel = json.loads(channel_path.read_text(encoding="utf-8"))
    monetization = (channel.get("lanes") or {}).get("monetization") or {}
    placement_id = monetization.get("placement_id")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger_core = {key: value for key, value in ledger.items() if key != "ledger_sha256"}
    if ledger.get("ledger_sha256") != hashlib.sha256(json.dumps(
        ledger_core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest():
        raise ValueError("placement ledger hash mismatch")
    matches = [
        row for row in ledger.get("placements", [])
        if isinstance(row, dict) and row.get("placement_id") == placement_id
    ]
    if len(matches) != 1:
        return {"state": "WAITING_FOR_EXACT_PLACEMENT", "changed": False}
    placement = matches[0]

    def cohort_metric(path, field):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"count": None, "state": "UNKNOWN_NOT_IN_COHORT"}, None
        rows = [row for row in receipt.get("placements", [])
                if row.get("placement_id") == placement_id]
        if len(rows) != 1 or not isinstance(rows[0].get("count"), int):
            return {"count": None, "state": "UNKNOWN_NOT_IN_COHORT"}, None
        return {"count": rows[0]["count"], "state": rows[0].get("state", "OBSERVED")}, (
            receipt.get("receipt_sha256") or hashlib.sha256(path.read_bytes()).hexdigest()
        )

    entries, entries_source = cohort_metric(
        state / "owned-entry-observations" / "latest.json", "owned_entries"
    )
    cta, cta_source = cohort_metric(
        state / "cta-click-observations" / "latest.json", "cta_clicks"
    )
    repost_root = Path(os.environ.get(
        "AFFILIATE_REPOST_STATE_DIR", Path.home() / "loops" / "x-repost"
    )).expanduser()
    posted_path = repost_root / "posted.jsonl"
    posted_raw = posted_path.read_bytes()
    posted_rows = [json.loads(line) for line in posted_raw.decode("utf-8").splitlines()
                   if line.strip()]
    post = next((row for row in reversed(posted_rows)
                 if row.get("affiliate_job_id") == monetization.get("job_id")
                 and row.get("post_url") == monetization.get("post_url")), None)
    if post is None:
        return {"state": "WAITING_FOR_EXACT_POST", "changed": False}
    provider = placement.get("provider_clicks") or {}
    provider_observed_at = provider.get("observed_at")
    try:
        provider_at = datetime.fromisoformat(str(provider_observed_at).replace("Z", "+00:00"))
        posted_at = datetime.fromisoformat(str(post.get("posted_at")).replace("Z", "+00:00"))
        provider_after_post = provider_at >= posted_at
    except ValueError:
        provider_after_post = False
    commission = placement.get("commission") or {}
    statuses = commission.get("status_counts") or {}
    approved_or_paid = int(statuses.get("approved") or 0) + int(statuses.get("paid") or 0)
    cost = placement.get("cost") or {}
    core = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_MONEY_FUNNEL_ROW",
        "job_id": monetization.get("job_id"),
        "placement_id": placement_id,
        "post_url": monetization.get("post_url"),
        "impressions": monetization.get("impressions"),
        "owned_entries": entries,
        "cta_clicks": cta,
        "provider_clicks": {
            "cumulative_count": provider.get("count"),
            "cumulative_unique_count": provider.get("unique_count"),
            "observed_at": provider_observed_at,
            "post_distribution_count": None,
            "post_distribution_state": (
                "BASELINE_UNAVAILABLE" if provider_after_post
                else "WAITING_FOR_POST_PROVIDER_READBACK"
            ),
        },
        "transactions": {
            "count": commission.get("transaction_count"),
            "state": "OBSERVED" if isinstance(commission.get("transaction_count"), int)
            else "UNKNOWN",
        },
        "commission_status_counts": {
            key: statuses.get(key) for key in ("pending", "approved", "paid", "reversed")
        },
        "approved_or_paid_net_minor_by_currency": (
            commission.get("approved_or_paid_net_minor_by_currency") or {}
        ),
        "approved_or_paid_money_state": (
            "APPROVED_OR_PAID" if approved_or_paid else "NO_APPROVED_OR_PAID"
        ),
        "cost": {
            "state": cost.get("actual_cash_state", "UNKNOWN"),
            "actual_cash_amount_by_currency": cost.get("actual_cash_amount_by_currency"),
        },
        "source_receipts": {
            "x_channel_transition_id": channel.get("transition_id"),
            "placement_ledger_sha256": ledger.get("ledger_sha256"),
            "owned_entries": entries_source,
            "cta_clicks": cta_source,
            "posted_sha256": hashlib.sha256(posted_raw).hexdigest(),
        },
    }
    transition_id = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "transition_id": transition_id,
               "observed_at": datetime.now(timezone.utc).isoformat()}
    changed = append_unique(
        state / "money-funnel" / "rows.jsonl", receipt, ("transition_id",)
    )
    atomic_json(state / "money-funnel" / "latest.json", receipt)
    return {**receipt, "state": "OBSERVED", "changed": changed}


def activate_funnel_experiment(state, decision):
    if not isinstance(decision, dict) or decision.get("state") not in {
        "READY", "ALREADY_DECIDED",
    }:
        return {"state": "WAITING_FOR_FUNNEL_DECISION", "changed": False}
    funnel = json.loads((state / "money-funnel" / "latest.json").read_text(encoding="utf-8"))
    source_transition = decision.get("source_funnel_transition_id")
    decision_id = decision.get("decision_id")
    if not (
        isinstance(source_transition, str) and REPOST_PROPOSAL_ID_PATTERN.fullmatch(source_transition)
        and source_transition == funnel.get("transition_id")
        and isinstance(decision_id, str) and REPOST_PROPOSAL_ID_PATTERN.fullmatch(decision_id)
        and isinstance(funnel.get("placement_id"), str)
        and REPOST_PLACEMENT_ID_PATTERN.fullmatch(funnel["placement_id"])
    ):
        raise ValueError("funnel experiment control mismatch")
    active_path = state / "funnel-experiments" / "active.json"
    if active_path.is_file():
        active = json.loads(active_path.read_text(encoding="utf-8"))
        if active.get("state") == "ACTIVE" and active.get("decision_id") == decision_id:
            return {**active, "changed": False}
        if active.get("state") == "ACTIVE":
            return {
                "state": "BLOCKED_ACTIVE_EXPERIMENT", "changed": False,
                "active_experiment_id": active.get("experiment_id"),
                "active_decision_id": active.get("decision_id"),
            }
    material = {
        "decision_id": decision_id,
        "source_funnel_transition_id": source_transition,
        "control_placement_id": funnel["placement_id"],
        "control_job_id": funnel.get("job_id"),
        "control_post_url": funnel.get("post_url"),
        "selected_variable": decision.get("selected_variable"),
        "official_success_metric": decision.get("official_success_metric"),
    }
    experiment_id = hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_FUNNEL_EXPERIMENT",
        "state": "ACTIVE",
        "experiment_id": experiment_id,
        **material,
        "bottleneck": decision.get("bottleneck"),
        "exposure_assessment": decision.get("exposure_assessment"),
        "hypothesis": decision.get("hypothesis"),
        "action": decision.get("action"),
        "observation_state": "OPEN",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    changed = append_unique(
        state / "funnel-experiments" / "history.jsonl", receipt, ("experiment_id",)
    )
    atomic_json(active_path, receipt)
    return {**receipt, "changed": changed}


def enforce_exposure_gate(state):
    active_path = state / "funnel-experiments" / "active.json"
    latest_path = state / "money-funnel" / "latest.json"
    if not active_path.is_file() or not latest_path.is_file():
        return {"state": "WAITING_FOR_ACTIVE_EXPERIMENT", "changed": False}
    active = json.loads(active_path.read_text(encoding="utf-8"))
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    source = next((
        row for row in json_rows(state / "money-funnel" / "rows.jsonl")
        if row.get("transition_id") == active.get("source_funnel_transition_id")
    ), None)
    if active.get("state") != "ACTIVE" or source is None:
        raise ValueError("active exposure source unavailable")
    assessment = active.get("exposure_assessment")
    if assessment not in {"insufficient", "sufficient", "unknown"}:
        raise ValueError("invalid exposure assessment")
    baseline_impressions = (source.get("impressions") or {}).get("count")
    current_impressions = (latest.get("impressions") or {}).get("count")
    sufficient = assessment == "sufficient"
    state_name = (
        "EXPOSURE_SUFFICIENT" if sufficient else
        "WAITING_FOR_EXPOSURE" if assessment == "insufficient" else
        "EXPOSURE_UNKNOWN"
    )
    transactions = (latest.get("transactions") or {}).get("count")
    core = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_EXPOSURE_GATE",
        "state": state_name,
        "experiment_id": active.get("experiment_id"),
        "decision_id": active.get("decision_id"),
        "control_placement_id": active.get("control_placement_id"),
        "selected_variable": active.get("selected_variable"),
        "source_funnel_transition_id": active.get("source_funnel_transition_id"),
        "current_funnel_transition_id": latest.get("transition_id"),
        "official_success_metric": active.get("official_success_metric"),
        "baseline_impressions": baseline_impressions,
        "current_impressions": current_impressions,
        "conversion_verdict_allowed": sufficient,
        "distribution_required": not sufficient,
        "maximize_relevant_exposure": not sufficient,
        "transactions_observed": transactions,
        "transactions_verdict_state": (
            "ELIGIBLE_FOR_JUDGMENT" if sufficient
            else "NOT_JUDGED_INSUFFICIENT_EXPOSURE"
            if assessment == "insufficient" else "NOT_JUDGED_UNKNOWN_EXPOSURE"
        ),
    }
    transition_id = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "transition_id": transition_id,
               "observed_at": datetime.now(timezone.utc).isoformat()}
    changed = append_unique(
        state / "funnel-experiments" / "exposure-gates.jsonl",
        receipt, ("transition_id",),
    )
    atomic_json(state / "funnel-experiments" / "latest-exposure-gate.json", receipt)
    return {**receipt, "changed": changed}


def materialize_distribution_mix_plan(state, decision=None):
    active_path = state / "funnel-experiments" / "active.json"
    gate_path = state / "funnel-experiments" / "latest-exposure-gate.json"
    if not active_path.is_file() or not gate_path.is_file():
        return {"state": "WAITING_FOR_EXPOSURE_GATE", "changed": False}
    active = json.loads(active_path.read_text(encoding="utf-8"))
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    if active.get("state") != "ACTIVE" or active.get("selected_variable") != "distribution_mix":
        raise ValueError("active experiment is not distribution_mix")
    if (
        gate.get("experiment_id") != active.get("experiment_id")
        or gate.get("state") != "WAITING_FOR_EXPOSURE"
        or gate.get("distribution_required") is not True
    ):
        return {"state": "NO_DISTRIBUTION_REQUIRED", "changed": False}
    selected_decision = active
    if isinstance(decision, dict) and decision.get("state") in {"READY", "ALREADY_DECIDED"}:
        try:
            latest_funnel = json.loads((
                state / "money-funnel" / "latest.json"
            ).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            latest_funnel = {}
        if not all((
            decision.get("selected_variable") == "distribution_mix",
            decision.get("bottleneck") == "reach",
            decision.get("exposure_assessment") == "insufficient",
            decision.get("source_funnel_transition_id") == latest_funnel.get("transition_id"),
            latest_funnel.get("placement_id") == active.get("control_placement_id"),
            isinstance(decision.get("decision_id"), str)
            and REPOST_PROPOSAL_ID_PATTERN.fullmatch(decision["decision_id"]),
        )):
            raise ValueError("distribution continuation decision mismatch")
        selected_decision = decision
    job_id = active.get("control_job_id")
    job_path = state / "x-distribution-jobs" / f"{job_id}.json"
    if not job_path.is_file():
        raise ValueError("control distribution job unavailable")
    job = json.loads(job_path.read_text(encoding="utf-8"))
    placement_id = active.get("control_placement_id")
    if job.get("job_id") != job_id or job.get("placement_id") != placement_id:
        raise ValueError("control distribution job mismatch")
    content_sha256 = job.get("content_sha256")
    if not isinstance(content_sha256, str) or not REPOST_PROPOSAL_ID_PATTERN.fullmatch(content_sha256):
        raise ValueError("control content hash unavailable")
    surface_dirs = {
        "devto": "devto-publications",
        "substack": "substack-publications",
        "x": "x-posts",
    }
    live_surfaces = sorted(
        surface for surface, directory in surface_dirs.items()
        if any(
            row.get("placement_id") == placement_id
            and row.get("state") == "LIVE"
            and isinstance(row.get("public_url"), str)
            for row in (
                json.loads(path.read_text(encoding="utf-8"))
                for path in (state / directory).glob("*.json")
            )
        )
    )
    core = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_DISTRIBUTION_MIX_PLAN",
        "state": "READY",
        "experiment_id": active["experiment_id"],
        "decision_id": selected_decision.get("decision_id"),
        "selected_variable": "distribution_mix",
        "control_placement_id": placement_id,
        "control_job_id": job_id,
        "control_content_sha256": content_sha256,
        "control_post_url": active.get("control_post_url"),
        "target_x_account": job.get("target_x_account"),
        "live_surfaces": live_surfaces,
        "next_action": "SAFE_X_RECIRCULATION",
        "cadence_rule": "ONE_RELEVANT_RECIRCULATION_PER_OWNER_PASS",
        "maximize_relevant_exposure": gate.get("maximize_relevant_exposure") is True,
        "official_success_metric": selected_decision.get("official_success_metric"),
        "decision_action": selected_decision.get("action"),
        "content_mutation_allowed": False,
    }
    plan_id = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "plan_id": plan_id,
               "created_at": datetime.now(timezone.utc).isoformat()}
    changed = append_unique(
        state / "funnel-experiments" / "distribution-plans.jsonl",
        receipt, ("plan_id",),
    )
    atomic_json(state / "funnel-experiments" / "latest-distribution-plan.json", receipt)
    return {**receipt, "changed": changed}


def focused_live_lineage(state):
    latest = state / "focused-cohort" / "latest.json"
    if not latest.is_file():
        return []
    focus = json.loads(latest.read_text(encoding="utf-8"))
    current = focus.get("placement_id")
    if not isinstance(current, str) or not current:
        return []
    campaigns = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (state / "campaign-publications").glob("*.json")
    ]
    lineage = [current]
    visited = {current}
    while True:
        children = [
            campaign for campaign in campaigns
            if campaign.get("state") == "X_LIVE"
            and campaign.get("placement_id") not in visited
            and (campaign.get("experiment") or {}).get("control_placement_id") == current
        ]
        if not children:
            return lineage
        child = max(children, key=lambda campaign: (
            campaign.get("created_at") or "", campaign.get("placement_id") or "",
        ))
        current = child["placement_id"]
        lineage.append(current)
        visited.add(current)


def refresh_funnel_snapshot(state, limit=3):
    """Persist the highest-observed exact placement funnels without inference."""
    ledger = json.loads((state / "placement-ledger.json").read_text(encoding="utf-8"))
    rows = ledger.get("placements")
    if not isinstance(rows, list):
        raise ValueError("placement ledger rows unavailable")
    ranked_all = sorted(
        (row for row in rows if isinstance(row, dict) and row.get("placement_id")),
        key=lambda row: (
            -(
                (row.get("provider_clicks") or {}).get("unique_count")
                if isinstance((row.get("provider_clicks") or {}).get("unique_count"), int)
                else -1
            ),
            -(
                (row.get("provider_clicks") or {}).get("count")
                if isinstance((row.get("provider_clicks") or {}).get("count"), int)
                else -1
            ),
            row["placement_id"],
        ),
    )
    ranked = ranked_all[:limit]
    by_placement = {row["placement_id"]: row for row in ranked_all}
    focused_lineage = focused_live_lineage(state)
    selected_ids = {row["placement_id"] for row in ranked}
    for placement_id in focused_lineage:
        if placement_id in by_placement and placement_id not in selected_ids:
            ranked.append(by_placement[placement_id])
            selected_ids.add(placement_id)
    placements = []
    for row in ranked:
        placement_id = row["placement_id"]
        campaign = next((
            value for path in (state / "campaign-publications").glob("*.json")
            for value in [json.loads(path.read_text(encoding="utf-8"))]
            if value.get("placement_id") == placement_id
        ), {})
        exposure = row.get("exposure") if isinstance(row.get("exposure"), dict) else {}
        clicks = row.get("provider_clicks") if isinstance(row.get("provider_clicks"), dict) else {}
        commission = row.get("commission") if isinstance(row.get("commission"), dict) else {}
        placements.append({
            "placement_id": placement_id,
            "owned_url": row.get("public_url"),
            "x_permalink": campaign.get("x_url"),
            "provider_link_key_state": "PRESENT" if row.get("provider_link_key") else "MISSING",
            "owned_visits": {
                "count": exposure.get("owned_page_visits"),
                "state": exposure.get("owned_page_visits_state", "UNKNOWN"),
                "observed_at": exposure.get("observed_at"),
            },
            "cta_clicks": {"count": None, "state": "UNKNOWN", "observed_at": None},
            "provider_clicks": {
                "count": clicks.get("count"),
                "unique_count": clicks.get("unique_count"),
                "state": clicks.get("unique_state", "UNKNOWN"),
                "observed_at": clicks.get("observed_at"),
            },
            "customers": {
                "count": None,
                "state": "UNAVAILABLE_AT_EXACT_PLACEMENT",
                "observed_at": None,
            },
            "transactions": {
                "count": commission.get("transaction_count"),
                "state": "OBSERVED" if isinstance(commission.get("transaction_count"), int) else "UNKNOWN",
            },
            "money_state": "NON_MONEY_UNTIL_APPROVED_OR_PAID",
        })
    core = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_TOP_PLACEMENT_FUNNEL_SNAPSHOT",
        "source_ledger_sha256": ledger.get("ledger_sha256"),
        "selection": "PROVIDER_UNIQUE_CLICKS_DESC",
        "limit": limit,
        "focused_lineage_count": len([
            placement_id for placement_id in focused_lineage
            if placement_id in selected_ids
        ]),
        "placements": placements,
    }
    snapshot_sha256 = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "snapshot_sha256": snapshot_sha256}
    changed = append_unique(
        state / "funnel-snapshots.jsonl", receipt, ("snapshot_sha256",)
    )
    atomic_json(state / "funnel-snapshots" / "latest.json", receipt)
    return {**receipt, "state": "OBSERVED", "changed": changed}


def focus_cohort(state):
    """Select one existing pre-payment cohort and freeze broad expansion."""
    latest = state / "focused-cohort" / "latest.json"
    prior = json.loads(latest.read_text(encoding="utf-8")) if latest.is_file() else None
    interval = json.loads(
        (state / "interval-funnel-joins" / "latest.json").read_text(encoding="utf-8")
    )
    claimed = interval.get("receipt_sha256")
    core = {key: value for key, value in interval.items() if key != "receipt_sha256"}
    if claimed != hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest():
        raise ValueError("interval funnel receipt hash mismatch")
    snapshot = json.loads(
        (state / "funnel-snapshots" / "latest.json").read_text(encoding="utf-8")
    )
    snapshot_rows = {row["placement_id"]: row for row in snapshot.get("placements", [])}
    candidates = []
    for row in interval.get("placements", []):
        placement_id = row.get("placement_id")
        campaign = next((
            json.loads(path.read_text(encoding="utf-8"))
            for path in (state / "campaign-publications").glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("placement_id") == placement_id
        ), {})
        plan_id = campaign.get("plan_id")
        handoff_path = state / "campaign-handoffs" / f"{plan_id}.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8")) if handoff_path.is_file() else {}
        buyer_intent = handoff.get("buyer_intent", "")
        clicks = (snapshot_rows.get(placement_id, {}).get("provider_clicks") or {})
        if campaign.get("state") == "X_LIVE" and "before paying" in buyer_intent.lower():
            experiment = campaign.get("experiment") or handoff.get("experiment") or {}
            candidates.append({
                "placement_id": placement_id, "plan_id": plan_id,
                "buyer_problem": buyer_intent,
                "decision_stage_query": handoff.get("title"),
                "handoff_fingerprint": handoff.get("handoff_fingerprint"),
                "provider_unique_clicks": clicks.get("unique_count", -1),
                "provider_clicks": clicks.get("count", -1),
                "created_at": campaign.get("created_at"),
                "control_placement_id": experiment.get("control_placement_id"),
                "experiment_decision_id": experiment.get("decision_id"),
                "experiment_selected_variable": experiment.get("selected_variable"),
                "experiment_success_metric": experiment.get("success_metric"),
            })
    if not candidates:
        return {"state": "NO_QUALIFIED_COHORT", "changed": False}
    selected = next((
        row for row in candidates
        if prior and row["placement_id"] == prior.get("placement_id")
    ), None)
    if selected is None:
        if prior:
            write_focused_baseline(state, prior)
            return {**prior, "state": "FOCUSED", "changed": False}
        base_candidates = [row for row in candidates if not row.get("control_placement_id")]
        selected = max(base_candidates or candidates, key=lambda row: (
            row["provider_unique_clicks"], row["provider_clicks"], row["placement_id"]
        ))
    visited = set()
    while selected["placement_id"] not in visited:
        visited.add(selected["placement_id"])
        children = [
            row for row in candidates
            if row.get("control_placement_id") == selected["placement_id"]
            and row["placement_id"] not in visited
        ]
        if not children:
            break
        selected = max(children, key=lambda row: (
            row.get("created_at") or "", row["placement_id"],
        ))
    if prior and selected["placement_id"] == prior.get("placement_id"):
        write_focused_baseline(state, prior)
        return {**prior, "state": "FOCUSED", "changed": False}
    receipt_core = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_FOCUSED_COHORT",
        "selection_state": "FOCUSED_EXPLORATION",
        **selected,
        "source_interval_receipt_sha256": claimed,
        "source_snapshot_sha256": snapshot.get("snapshot_sha256"),
        "selection_basis": "PRE_PAYMENT_INTENT_THEN_PROVIDER_UNIQUE_CLICKS",
        "channel_set": ["owned_article", "x"],
        "placement_expansion_state": "PAUSED_FOR_FOCUSED_COHORT",
        "money_state": "NON_MONEY",
    }
    receipt_sha256 = hashlib.sha256(json.dumps(
        receipt_core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**receipt_core, "receipt_sha256": receipt_sha256}
    changed = append_unique(state / "focused-cohorts.jsonl", receipt, ("receipt_sha256",))
    atomic_json(latest, receipt)
    write_focused_baseline(state, receipt)
    return {**receipt, "state": "FOCUSED", "changed": changed}


def write_focused_baseline(state, focus):
    """Expose immutable focused evidence to the existing model decision owner."""
    interval = json.loads(
        (state / "interval-funnel-joins" / "latest.json").read_text(encoding="utf-8")
    )
    row = next(
        item for item in interval.get("placements", [])
        if item.get("placement_id") == focus.get("placement_id")
    )
    baseline = {
        "schema_version": 1,
        "receipt_type": "FOCUSED_INTERVAL_BASELINE",
        "public_id": focus["placement_id"],
        "plan_id": focus["plan_id"],
        "placement_id": focus["placement_id"],
        "observed_at": interval.get("interval_end"),
        "source_interval_receipt_sha256": focus["source_interval_receipt_sha256"],
        "buyer_problem": focus["buyer_problem"],
        "control_provider_clicks": focus["provider_clicks"],
        "control_provider_unique_clicks": focus["provider_unique_clicks"],
        "interval_cta_clicks": row.get("cta_clicks"),
        "interval_provider_click_delta": row.get("provider_click_delta"),
        "interval_provider_unique_click_delta": row.get("provider_unique_click_delta"),
        "customer_state": row.get("customer_state"),
        "transaction_count": row.get("transaction_count"),
        "required_success_metric": "EXACT_PLACEMENT_OFFICIAL_TRANSACTION_COUNT",
    }
    atomic_json(
        state / "distribution-baselines" / f"focused-{focus['receipt_sha256']}.json",
        baseline,
    )


def focused_publication_allowed(state, placement, progress, handoff=None):
    path = state / "focused-cohort" / "latest.json"
    if not path.is_file():
        return True
    focus = json.loads(path.read_text(encoding="utf-8"))
    if (
        placement == focus.get("placement_id")
        or progress.get("state") in {"MATERIALIZED", "OWNED_NOT_LIVE", "OWNED_LIVE"}
    ):
        return True
    experiment = (handoff or {}).get("experiment") or {}
    baseline_sha256 = experiment.get("baseline_sha256", "")
    decision_path = state / "acquisition-decisions" / f"{baseline_sha256}.json"
    try:
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return all((
        experiment.get("control_placement_id") == focus.get("placement_id"),
        experiment.get("decision_id") == decision.get("decision_id"),
        experiment.get("selected_variable") == decision.get("selected_variable"),
        experiment.get("success_metric") == decision.get("success_metric"),
        experiment.get("instruction") == decision.get("next_campaign_instruction"),
        decision.get("state") == "READY",
        re.search(r"\b(customer|transaction)(_count)?\b", decision.get("success_metric", ""), re.I),
        not re.search(r"\b(view|click|engagement|impression)s?\b", decision.get("success_metric", ""), re.I),
    ))


def _private_env_value(name):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for path in (
        Path("~/.config/anicca/affiliate.env"),
        Path(os.environ.get("LIFE_MANAGER_ENV_FILE", "~/.local/state/life-manager/.env")),
    ):
        path = path.expanduser()
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(f"{name}=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip().strip("\"'")
    raise ValueError(f"{name} is unavailable")


def observe_owned_visits(state):
    """Join an exact Netlify analytics capability readback without guessing visits."""
    snapshot = json.loads(
        (state / "funnel-snapshots" / "latest.json").read_text(encoding="utf-8")
    )
    token = _private_env_value("NETLIFY_AUTH_TOKEN")
    request = urllib.request.Request(
        "https://api.netlify.com/api/v1/sites?per_page=100",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "life-manager-affiliate/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        sites = json.load(response)
    site = next((
        row for row in sites if isinstance(row, dict)
        and row.get("custom_domain") == "aniccaai.com"
    ), None)
    if not site:
        raise ValueError("owned Netlify site unavailable")
    enabled = bool(site.get("analytics_instance_id"))
    reason = "NETLIFY_ANALYTICS_API_NOT_REGISTERED" if enabled else "NETLIFY_WEB_ANALYTICS_DISABLED"
    rows = [{
        "placement_id": row.get("placement_id"),
        "owned_url": row.get("owned_url"),
        "count": None,
        "state": "UNAVAILABLE",
        "reason": reason,
    } for row in snapshot.get("placements", [])]
    core = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_OWNED_VISIT_OBSERVATION",
        "source_funnel_snapshot_sha256": snapshot.get("snapshot_sha256"),
        "site_identity": "aniccaai.com",
        "analytics_enabled": enabled,
        "placements": rows,
        "money_state": "NON_MONEY",
    }
    receipt_sha256 = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "receipt_sha256": receipt_sha256}
    changed = append_unique(
        state / "owned-visit-observations.jsonl", receipt, ("receipt_sha256",)
    )
    atomic_json(state / "owned-visit-observations" / "latest.json", receipt)
    return {**receipt, "state": "UNAVAILABLE", "changed": changed, "reason": reason}


def quarantine_snapshot(state_root, threshold=QUARANTINE_FAILURE_THRESHOLD):
    """Quarantine only external tools with a consecutive failure streak."""
    streaks = {}
    last_failure = {}
    for row in json_rows(state_root / "tool-attempt-receipts.jsonl"):
        tool = row.get("tool")
        if row.get("effect_class") not in QUARANTINABLE_EFFECTS or not tool:
            continue
        if (
            tool == "publication.advance"
            and row.get("failure_class") == "RUNTIME_TRANSIENT"
            and row.get("failure_type") == "FileNotFoundError"
        ):
            streaks[tool] = 0
            last_failure.pop(tool, None)
            continue
        if row.get("outcome") == "FAILED":
            streaks[tool] = streaks.get(tool, 0) + 1
            last_failure[tool] = row.get("failure_type")
        else:
            streaks[tool] = 0
            last_failure.pop(tool, None)
    tools = {
        tool: {
            "consecutive_failures": streaks[tool],
            "last_failure_type": last_failure.get(tool),
        }
        for tool in sorted(streaks)
        if streaks[tool] >= threshold
    }
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_QUARANTINE_SNAPSHOT",
        "state": "QUARANTINED" if tools else "CLEAR",
        "failure_threshold": threshold,
        "tools": tools,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(state_root / "quarantine.json", receipt)
    return receipt


def action_budget_snapshot(state_root, cap=EXTERNAL_ACTION_DAILY_CAP):
    """Count non-no-effect external attempts in the current JST day."""
    day = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    used = 0
    for row in json_rows(state_root / "tool-attempt-receipts.jsonl"):
        if row.get("effect_class") not in QUARANTINABLE_EFFECTS:
            continue
        if row.get("effect_certainty") == "NO_EFFECT":
            continue
        try:
            observed_day = datetime.fromisoformat(
                str(row.get("finished_at", "")).replace("Z", "+00:00")
            ).astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
        except (TypeError, ValueError):
            continue
        if observed_day == day:
            used += 1
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_EXTERNAL_ACTION_BUDGET",
        "state": "ACTION_CAP_DISABLED" if cap is None else (
            "ACTION_CAP_BLOCKED" if used >= cap else "CLEAR"
        ),
        "day": day,
        "used_attempts": used,
        "daily_cap": cap,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(state_root / "action-budget.json", receipt)
    return receipt


def _cost_timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_jst_day_epoch():
    now = datetime.now(COST_BUDGET_JST)
    next_day = datetime.combine(
        now.date() + timedelta(days=1), datetime_time.min, tzinfo=COST_BUDGET_JST,
    )
    return int(next_day.timestamp())


def cost_budget_snapshot(state_root, cap_minor=EXTERNAL_COST_DAILY_CAP_MINOR):
    """Snapshot known actual USD bills for the current JST day."""
    now = datetime.now(COST_BUDGET_JST)
    day = now.date().isoformat()
    ledger = state_root / "cost-ledger.jsonl"
    rows = []
    unknown_rows = 1 if not ledger.is_file() else 0
    if ledger.is_file():
        try:
            lines = ledger.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
            unknown_rows += 1
        for line in lines:
            try:
                rows.append(json.loads(line))
            except (TypeError, ValueError):
                unknown_rows += 1
    seen_ids = set()
    known = {}
    valid_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            unknown_rows += 1
            continue
        timestamp = next(
            (parsed for field in ("occurred_at", "created_at", "observed_at")
             if (parsed := _cost_timestamp(row.get(field))) is not None),
            None,
        )
        if timestamp is not None and timestamp.astimezone(COST_BUDGET_JST).date().isoformat() != day:
            continue
        identifier = next(
            (value.strip() for field in ("cost_id", "event_id", "receipt_id")
             if isinstance((value := row.get(field)), str) and value.strip()),
            None,
        )
        if identifier is not None and identifier in seen_ids:
            continue
        if identifier is not None:
            seen_ids.add(identifier)
        amount = row.get("amount_minor")
        valid = (
            row.get("cost_basis") == "actual_billed"
            and timestamp is not None
            and timestamp.astimezone(COST_BUDGET_JST).date().isoformat() == day
            and identifier is not None
            and row.get("currency") == "USD"
            and isinstance(amount, int)
            and not isinstance(amount, bool)
            and amount >= 0
        )
        if not valid:
            unknown_rows += 1
            continue
        valid_rows += 1
        known["USD"] = known.get("USD", 0) + amount
    known_usd = known.get("USD", 0)
    state = (
        "COST_CAP_BLOCKED" if known_usd >= cap_minor else
        "CLEAR" if valid_rows > 0 and unknown_rows == 0 else
        "COST_CAP_UNKNOWN"
    )
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_EXTERNAL_COST_BUDGET",
        "state": state,
        "day": day,
        "cap_minor": cap_minor,
        "known_actual_minor_by_currency": known,
        "known_actual_usd_minor": known_usd,
        "unknown_rows": unknown_rows,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(state_root / "cost-budget.json", receipt)
    return receipt


def installed_release_sha():
    """Return the immutable release SHA, or an explicit source-checkout marker."""
    candidate = Path(__file__).resolve().parents[1].name
    return candidate if RELEASE_SHA_PATTERN.fullmatch(candidate) else "SOURCE_CHECKOUT"


def run_receipt_stages(event):
    """Expose stage state without copying URLs, links, credentials, or content."""
    repost = event.get("repost_observation") or {}
    return [
        {"name": "provider", "state": event.get("provider_state")},
        {"name": "placement_link", "state": event.get("placement_link_state")},
        {"name": "publication", "state": event.get("publication_state")},
        {"name": "distribution", "state": event.get("distribution_state")},
        {"name": "revenue", "state": event.get("revenue_state")},
        {"name": "rolling_net", "state": event.get("rolling_net_net_state")},
        {"name": "repost_observation", "state": repost.get("state")},
        {"name": "telegram", "state": event.get("telegram_state", "PENDING")},
    ]


def append_run_receipt(state, event, started_at, finished_at=None, run_id=None,
                       terminal_state=None, failure_type=None,
                       scheduler_run_id=None):
    """Persist one replay-safe canonical receipt for a scheduler wake."""
    finished_at = time.time() if finished_at is None else finished_at
    run_id = run_id or event.get("wake_event_uuid")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run receipt requires a run_id")
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_RUN_RECEIPT",
        "run_id": run_id,
        "wake_event_uuid": event.get("wake_event_uuid"),
        "scheduler_run_id": scheduler_run_id or run_id,
        "release_sha": installed_release_sha(),
        "owner_label": RUN_OWNER_LABEL,
        "causal_parent": {
            "type": "scheduler",
            "owner_label": RUN_OWNER_LABEL,
            "trigger": "launchd",
        },
        "started_at": datetime.fromtimestamp(
            started_at, timezone.utc
        ).isoformat(),
        "finished_at": datetime.fromtimestamp(
            finished_at, timezone.utc
        ).isoformat(),
        "duration_ms": max(0, int(round((finished_at - started_at) * 1000))),
        "due_work": {
            "revenue_state": event.get("revenue_state"),
            "acquisition_decision_state": event.get("acquisition_decision_state"),
            "publication_state": event.get("publication_state"),
        },
        "stages": run_receipt_stages(event),
        "terminal_state": terminal_state or event.get("status") or "UNKNOWN",
        "run_state": "FAILED" if failure_type else "SUCCEEDED",
        "failure_type": failure_type,
    }
    return append_unique(
        state / "run-receipts.jsonl", receipt, ("run_id", "terminal_state")
    )


_TOOL_EXTERNAL_EFFECTS = {
    "EXTERNAL_WRITE", "MESSAGE_SEND", "PROVIDER_LINK_WRITE", "PUBLICATION_WRITE",
}


def _safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {
        str(key): number
        for key, number in value.items()
        if isinstance(key, str) and isinstance(number, (int, float))
        and not isinstance(number, bool)
    }


def _tool_outcome(result, failure_type=None):
    state = result.get("state") if isinstance(result, dict) else None
    if failure_type or (isinstance(state, str) and state.endswith("_FAILED")):
        return "FAILED"
    if state in {
        "COOLDOWN", "NO_PENDING", "NO_TRANSACTIONS", "NOT_RUN", "WAITING_FOR_PLACEMENT_LINK",
        "BROWSER_UNAVAILABLE", "SIGN_IN_REQUIRED", "AUTH_REQUIRED", "ELIGIBILITY_BLOCKED",
        "DISK_GUARD_BLOCKED", "DISK_GUARD_UNKNOWN", "QUARANTINED",
        "ACTION_CAP_BLOCKED", "COST_CAP_BLOCKED",
    }:
        return "NO_EFFECT"
    return "COMPLETED"


def _tool_effect_certainty(result, effect_class, failure_type=None):
    if failure_type or not isinstance(result, dict):
        if effect_class in _TOOL_EXTERNAL_EFFECTS:
            return "UNKNOWN"
        return "READ_ONLY_CONFIRMED" if effect_class == "READ_ONLY" else "NO_EFFECT"
    state = result.get("state")
    if result.get("deduplicated") is True:
        return "NO_EFFECT"
    if result.get("changed") or result.get("sent") or state in {
        "LIVE", "X_LIVE", "VERIFIED", "SENT", "SELF_HEALED",
    }:
        return "EFFECT_CONFIRMED"
    if effect_class == "READ_ONLY":
        return "READ_ONLY_CONFIRMED"
    if _tool_outcome(result) == "NO_EFFECT":
        return "NO_EFFECT"
    return "UNKNOWN"


def _classify_tool_failure(error):
    """Return a typed failure class and bounded retry window without error text."""
    name = type(error).__name__
    module = type(error).__module__
    if name == "TimeoutError" or module.startswith("playwright."):
        return "BROWSER_TRANSIENT", 300
    if isinstance(error, ProviderError):
        return "PROVIDER_TRANSIENT", 900
    if isinstance(error, JobStateError):
        return "OWNED_JOURNAL", 1800
    if isinstance(error, (OSError, subprocess.SubprocessError)):
        return "RUNTIME_TRANSIENT", 600
    if isinstance(error, (ValueError, KeyError, json.JSONDecodeError)):
        return "CONTRACT", None
    return "UNKNOWN", None


def _classify_revenue_failure(failure_type):
    """Map the durable revenue-cycle failure contract to retry metadata."""
    classes = {
        "TIMEOUT": "BROWSER_TRANSIENT",
        "NONZERO_EXIT": "PROVIDER_TRANSIENT",
        "INVALID_JSON": "CONTRACT",
    }
    failure_class = classes.get(failure_type, "UNKNOWN")
    return failure_class, 3_600 if failure_class != "CONTRACT" else None


def append_tool_attempt_receipt(
    state, scheduler_run_id, tool, effect_class, attempt, preconditions,
    started_at, result=None, failure_type=None, retry_due_at=None,
    failure_class=None, retry_state=None, wake_event_uuid=None,
):
    """Persist one redacted, replay-safe receipt for an admitted tool attempt."""
    finished_at = time.time()
    result = result if isinstance(result, dict) else {}
    state_value = result.get("state")
    safe_preconditions = {
        str(key): value for key, value in (preconditions or {}).items()
        if isinstance(key, str) and isinstance(value, (str, int, float, bool, type(None)))
    }
    input_fingerprint = hashlib.sha256(json.dumps({
        "tool": tool, "preconditions": safe_preconditions,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    failure_type = failure_type or result.get("failure_type")
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_TOOL_ATTEMPT",
        "release_sha": installed_release_sha(),
        "owner_label": RUN_OWNER_LABEL,
        "scheduler_run_id": scheduler_run_id,
        "run_id": scheduler_run_id,
        "wake_event_uuid": wake_event_uuid,
        "tool": tool,
        "attempt": attempt,
        "effect_class": effect_class,
        "input_fingerprint": input_fingerprint,
        "preconditions": safe_preconditions,
        "started_at": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "finished_at": datetime.fromtimestamp(finished_at, timezone.utc).isoformat(),
        "duration_ms": max(0, int(round((finished_at - started_at) * 1000))),
        "outcome": _tool_outcome(result, failure_type),
        "failure_type": failure_type,
        "failure_class": failure_class,
        "retry_state": retry_state or ("RETRYABLE" if retry_due_at else "NOT_RETRYABLE"),
        "retry_due_at": retry_due_at if isinstance(retry_due_at, (int, float)) else None,
        "effect_certainty": _tool_effect_certainty(result, effect_class, failure_type),
        "postcondition": {
            "state": state_value,
            "changed": result.get("changed") if isinstance(result.get("changed"), bool) else None,
            "deduplicated": result.get("deduplicated") if isinstance(result.get("deduplicated"), bool) else None,
        },
        "usage": _safe_usage(result.get("usage") or result.get("provider_usage")),
    }
    return append_unique(
        state / "tool-attempt-receipts.jsonl", receipt,
        ("scheduler_run_id", "tool", "attempt"),
    )


def attempt_tool(
    state, scheduler_run_id, tool, effect_class, preconditions, operation,
    attempt=1, wake_event_uuid=None,
):
    """Run an admitted stage and persist success, failure, or no-effect evidence."""
    started_at = time.time()
    try:
        result = operation()
    except BaseException as error:
        failure_class, retry_seconds = _classify_tool_failure(error)
        append_tool_attempt_receipt(
            state, scheduler_run_id, tool, effect_class, attempt, preconditions,
            started_at, failure_type=type(error).__name__,
            failure_class=failure_class,
            retry_state="RETRYABLE" if retry_seconds else "NOT_RETRYABLE",
            retry_due_at=(time.time() + retry_seconds) if retry_seconds else None,
            wake_event_uuid=wake_event_uuid,
        )
        raise
    result = result if isinstance(result, dict) else {}
    append_tool_attempt_receipt(
        state, scheduler_run_id, tool, effect_class, attempt, preconditions,
        started_at, result=result, failure_type=result.get("failure_type"),
        failure_class=result.get("failure_class"),
        retry_due_at=result.get("retry_due_at") or result.get("retry_after"),
        retry_state=result.get("retry_state"), wake_event_uuid=wake_event_uuid,
    )
    return result


def wake_event_rows(state):
    """Return wake rows without confusing append-only delivery receipts for wakes."""
    return [
        row for row in json_rows(state / "events.jsonl")
        if row.get("event") in (None, "affiliate_wake")
    ]


def wake_event_uuid(event):
    """Derive a stable identity before Telegram delivery fields are attached."""
    identity = {
        key: value for key, value in event.items()
        if key not in {
            "wake_event_uuid", "telegram_event_uuid", "telegram_state",
            "telegram_message_id", "telegram_delivery_receipt_event_uuid",
        }
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def is_owned_article_url(value):
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "aniccaai.com"
        and bool(re.fullmatch(r"/blog/[a-z0-9][a-z0-9-]*", parsed.path))
        and not parsed.username and not parsed.password and port is None
        and not parsed.query and not parsed.fragment
    )


def safe_proposal_text(value, max_length=240):
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and not any(char.isspace() and char in "\r\n" or ord(char) < 32 for char in value)
        and "http" not in value.casefold()
    )


def repost_consumption_state(repost_root, proposal_id, placement_id):
    path = Path(repost_root).expanduser() / "affiliate-proposals-consumed.jsonl"
    if not path.is_file():
        return "UNCONSUMED_BY_SEPARATE_OWNER"
    latest = None
    placements_by_proposal = {}
    states_by_proposal = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("schema_version") != 1:
                return "CONSUMPTION_LEDGER_INVALID"
            if row.get("receipt_type") != "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION":
                return "CONSUMPTION_LEDGER_INVALID"
            if (
                not isinstance(row.get("proposal_id"), str)
                or not REPOST_PROPOSAL_ID_PATTERN.fullmatch(row["proposal_id"])
                or not isinstance(row.get("placement_id"), str)
                or not REPOST_PLACEMENT_ID_PATTERN.fullmatch(row["placement_id"])
                or row.get("state") not in REPOST_CONSUMPTION_STATES
                or not isinstance(row.get("observed_at"), str)
            ):
                return "CONSUMPTION_LEDGER_INVALID"
            try:
                observed = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
            except ValueError:
                return "CONSUMPTION_LEDGER_INVALID"
            if observed.tzinfo is None:
                return "CONSUMPTION_LEDGER_INVALID"
            if row.get("revenue_credit_state") != "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN":
                return "CONSUMPTION_LEDGER_INVALID"
            if row["state"] == "EFFECT_STARTED":
                if row.get("post_url") is not None:
                    return "CONSUMPTION_LEDGER_INVALID"
                snapshot = row.get("proposal")
                if not isinstance(snapshot, dict) or (
                    snapshot.get("receipt_type") != "AFFILIATE_REPOST_PROPOSAL"
                    or snapshot.get("state") != "READY_FOR_EXISTING_REPOST_OWNER"
                    or snapshot.get("proposal_id") != row["proposal_id"]
                    or snapshot.get("placement_id") != row["placement_id"]
                    or snapshot.get("language") != "en"
                    or snapshot.get("disclosure_required") is not True
                    or snapshot.get("tracking_link_state") != "NOT_INCLUDED"
                    or snapshot.get("revenue_credit_state") != "NO_REVENUE_CREDIT"
                    or not is_owned_article_url(snapshot.get("owned_article_url"))
                    or any(
                        value is not None and not safe_proposal_text(value)
                        for value in (snapshot.get("article_title"), snapshot.get("buyer_intent"))
                    )
                ):
                    return "CONSUMPTION_LEDGER_INVALID"
            elif row["state"] == "POSTED":
                post_url = row.get("post_url")
                try:
                    parsed = urlparse(post_url)
                    port = parsed.port
                except (TypeError, ValueError):
                    return "CONSUMPTION_LEDGER_INVALID"
                if not (
                    parsed.scheme == "https" and parsed.hostname == "x.com"
                    and not parsed.username and not parsed.password and port is None
                    and not parsed.query and not parsed.fragment
                    and re.fullmatch(r"/[A-Za-z0-9_]+/status/[0-9]+", parsed.path)
                ):
                    return "CONSUMPTION_LEDGER_INVALID"
            elif row.get("post_url") is not None:
                return "CONSUMPTION_LEDGER_INVALID"
            placements = placements_by_proposal.setdefault(row["proposal_id"], set())
            placements.add(row["placement_id"])
            if len(placements) > 1:
                return "CONSUMPTION_LEDGER_INVALID"
            prior_states = states_by_proposal.setdefault(row["proposal_id"], [])
            if not prior_states and row["state"] != "EFFECT_STARTED":
                return "CONSUMPTION_LEDGER_INVALID"
            if prior_states:
                prior = prior_states[-1]
                if prior != "EFFECT_STARTED" or row["state"] not in REPOST_CONSUMPTION_STATES - {"EFFECT_STARTED"}:
                    return "CONSUMPTION_LEDGER_INVALID"
            prior_states.append(row["state"])
            if row.get("proposal_id") == proposal_id:
                latest = row
    except (OSError, UnicodeDecodeError, ValueError):
        return "CONSUMPTION_LEDGER_UNAVAILABLE"
    if latest is None:
        return "UNCONSUMED_BY_SEPARATE_OWNER"
    if latest.get("placement_id") != placement_id:
        return "CONSUMPTION_LEDGER_MISMATCH"
    return {
        "EFFECT_STARTED": "EFFECT_STARTED_BY_SEPARATE_OWNER",
        "POSTED": "POSTED_BY_SEPARATE_OWNER",
        "UNVERIFIED": "UNVERIFIED_BY_SEPARATE_OWNER",
        "NO_EFFECT": "NO_EFFECT_BY_SEPARATE_OWNER",
    }.get(latest.get("state"), "CONSUMPTION_LEDGER_INVALID")


def observe_repost_acquisition(state):
    """Read the existing Repost ledger without owning or creating its effects."""
    configured = os.environ.get("AFFILIATE_REPOST_STATE_DIR")
    source_config_mode = "ENV" if configured else "NONE"
    if not configured:
        fallback = Path.home() / "loops" / "x-repost"
        if fallback.is_dir():
            configured = str(fallback)
            source_config_mode = "DEFAULT_HOME_LOOPS"
    source_state, raw, rows, invalid_row_count = "NOT_CONFIGURED", b"", [], 0
    if configured:
        posted_path = Path(configured).expanduser() / "posted.jsonl"
        if posted_path.is_file():
            source_state = "OBSERVED"
            try:
                raw = posted_path.read_bytes()
                for line in raw.decode("utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        invalid_row_count += 1
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
                    else:
                        invalid_row_count += 1
            except (OSError, UnicodeDecodeError):
                source_state, raw, rows, invalid_row_count = "UNAVAILABLE", b"", [], 0
        else:
            source_state = "UNAVAILABLE"

    campaign_by_x_url = {}
    campaign_by_placement = {}
    for path in (state / "campaign-publications").glob("*.json"):
        try:
            campaign = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        x_url = campaign.get("x_url")
        if isinstance(x_url, str) and x_url.startswith("https://x.com/"):
            campaign_by_x_url[x_url] = campaign.get("plan_id")
        placement_id = campaign.get("placement_id")
        owned_url = campaign.get("owned_url")
        if (
            campaign.get("state") == "X_LIVE"
            and isinstance(placement_id, str) and placement_id
            and is_owned_article_url(owned_url)
        ):
            campaign_by_placement[placement_id] = owned_url

    def exact_join(row):
        if any(
            isinstance(row.get(field), str)
            and row[field] in campaign_by_x_url
            for field in ("post_url", "source_url")
        ):
            return "CAMPAIGN_X_URL"
        placement_id = row.get("affiliate_placement_id")
        owned_url = row.get("affiliate_owned_article_url")
        if (
            isinstance(placement_id, str)
            and isinstance(owned_url, str)
            and row.get("source_url") == owned_url
            and campaign_by_placement.get(placement_id) == owned_url
        ):
            return "AFFILIATE_PLACEMENT_ID"
        return None

    join_methods = [exact_join(row) for row in rows]
    joined_count = sum(method is not None for method in join_methods)
    placement_id_join_count = sum(
        method == "AFFILIATE_PLACEMENT_ID" for method in join_methods
    )
    post_action_count = len(rows)
    unjoined_count = post_action_count - joined_count
    source_file_sha256 = (
        hashlib.sha256(raw).hexdigest() if source_state == "OBSERVED" else None
    )
    identity = {
        "source_state": source_state,
        "source_config_mode": source_config_mode,
        "source_file_sha256": source_file_sha256,
        "post_action_count": post_action_count,
        "joined_campaign_count": joined_count,
        "placement_id_join_count": placement_id_join_count,
        "unjoined_post_action_count": unjoined_count,
        "invalid_row_count": invalid_row_count,
    }
    transition_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    join_state = (
        "NOT_CONFIGURED" if source_state == "NOT_CONFIGURED" else
        "UNKNOWN" if source_state == "UNAVAILABLE" else
        "NO_ROWS" if not rows else
        "ALL_EXACT_CAMPAIGN_URL_JOINED" if joined_count == post_action_count else
        "PARTIAL_EXACT_CAMPAIGN_URL_JOINED" if joined_count else
        "NO_EXACT_CAMPAIGN_URL_JOIN"
    )
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_REPOST_OBSERVATION",
        "transition_id": transition_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_state": source_state,
        "source_config_mode": source_config_mode,
        "source_file_sha256": source_file_sha256,
        "post_action_count": post_action_count if source_state == "OBSERVED" else None,
        "joined_campaign_count": joined_count if source_state == "OBSERVED" else None,
        "placement_id_join_count": placement_id_join_count if source_state == "OBSERVED" else None,
        "unjoined_post_action_count": unjoined_count if source_state == "OBSERVED" else None,
        "invalid_row_count": invalid_row_count if source_state == "OBSERVED" else None,
        "join_state": join_state,
        "denominator_state": "POST_ACTION_COUNT_ONLY",
        "revenue_credit_state": "NO_REVENUE_CREDIT",
    }
    prior = {}
    try:
        prior = json.loads(
            (state / "repost-observations" / "latest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        pass
    changed = prior.get("transition_id") != transition_id
    if not changed:
        changed = not any(
            (row.get("repost_observation") or {}).get("transition_id") == transition_id
            for row in json_rows(state / "events.jsonl")
        )
    if changed:
        append_unique(
            state / "repost-observations.jsonl", receipt, ("transition_id",)
        )
    atomic_json(state / "repost-observations" / "latest.json", receipt)
    return {
        **receipt,
        "state": source_state,
        "changed": changed,
    }


def create_repost_proposal(state):
    """Offer one disclosed Affiliate placement to the separate Repost owner.

    This writes a private handoff only. It never posts, grants revenue credit,
    or assumes that the separate owner has consumed the proposal.
    """
    try:
        ledger = json.loads((state / "placement-ledger.json").read_text(encoding="utf-8"))
        placement_rows = {
            row.get("placement_id"): row for row in ledger.get("placements", [])
            if isinstance(row, dict) and isinstance(row.get("placement_id"), str)
        }
    except (OSError, ValueError, TypeError):
        placement_rows = {}
    candidates = []
    for path in (state / "campaign-publications").glob("*.json"):
        try:
            campaign = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        placement_id = campaign.get("placement_id")
        owned_url = campaign.get("owned_url")
        if not (
            campaign.get("state") == "X_LIVE"
            and isinstance(placement_id, str) and placement_id
            and is_owned_article_url(owned_url)
        ):
            continue
        metrics = placement_rows.get(placement_id, {}).get("provider_clicks", {})
        click_count = metrics.get("count") if isinstance(metrics, dict) else None
        article_title = None
        buyer_intent = None
        slug = campaign.get("slug")
        if isinstance(slug, str):
            try:
                content = json.loads((state / "content" / f"{slug}.json").read_text(encoding="utf-8"))
                article_title = content.get("title")
            except (OSError, ValueError):
                pass
        plan_paths = [
            state / "discovered-source-plans" / f"{campaign.get('plan_id')}.json",
            Path(__file__).resolve().parents[1] / "config" / "source-plans" / f"{campaign.get('plan_id')}.json",
        ]
        for plan_path in plan_paths:
            if buyer_intent is not None or not plan_path.is_file():
                continue
            try:
                buyer_intent = json.loads(plan_path.read_text(encoding="utf-8")).get("buyer_intent")
            except (OSError, ValueError):
                buyer_intent = None
        candidates.append({
            "placement_id": placement_id,
            "plan_id": campaign.get("plan_id"),
            "owned_article_url": owned_url,
            "provider_click_count": click_count if isinstance(click_count, int) else None,
            "article_title": article_title if safe_proposal_text(article_title) else None,
            "buyer_intent": buyer_intent if safe_proposal_text(buyer_intent) else None,
            "created_at": campaign.get("created_at") or "",
        })
    if not candidates:
        return {"state": "WAITING_FOR_ELIGIBLE_PLACEMENT", "changed": False}
    repost_root = os.environ.get("AFFILIATE_REPOST_STATE_DIR") or str(
        Path.home() / "loops" / "x-repost"
    )
    eligible = []
    for candidate in candidates:
        proposal_identity = {
            "placement_id": candidate["placement_id"],
            "owned_article_url": candidate["owned_article_url"],
            "language": "en",
            "proposal_kind": "AFFILIATE_REPOST_PROPOSAL",
        }
        candidate["proposal_id"] = hashlib.sha256(json.dumps(
            proposal_identity, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        delivery = repost_consumption_state(
            repost_root, candidate["proposal_id"], candidate["placement_id"]
        )
        if delivery in {
            "CONSUMPTION_LEDGER_INVALID", "CONSUMPTION_LEDGER_UNAVAILABLE",
            "CONSUMPTION_LEDGER_MISMATCH",
        }:
            return {"state": "REPOST_CONSUMPTION_UNSAFE", "changed": False}
        if delivery == "UNCONSUMED_BY_SEPARATE_OWNER":
            eligible.append(candidate)
    if not eligible:
        return {"state": "WAITING_FOR_REPOST_PROPOSAL_SLOT", "changed": False}
    selected = max(eligible, key=lambda row: (
        row["provider_click_count"] is not None,
        row["provider_click_count"] or -1,
        row["created_at"],
    ))
    proposal_id = selected["proposal_id"]
    delivery_state = repost_consumption_state(
        repost_root, proposal_id, selected["placement_id"]
    )
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_REPOST_PROPOSAL",
        "proposal_id": proposal_id,
        "state": "READY_FOR_EXISTING_REPOST_OWNER",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "placement_id": selected["placement_id"],
        "plan_id": selected["plan_id"],
        "owned_article_url": selected["owned_article_url"],
        "language": "en",
        "disclosure_required": True,
        "provider_click_count": selected["provider_click_count"],
        "article_title": selected["article_title"],
        "buyer_intent": selected["buyer_intent"],
        "selection_state": "BOUNDED_EXPLORATION_NO_APPROVED_NET",
        "repost_delivery_state": delivery_state,
        "revenue_credit_state": "NO_REVENUE_CREDIT",
        "tracking_link_state": "NOT_INCLUDED",
    }
    changed = append_unique(
        state / "repost-proposals.jsonl", receipt, ("proposal_id",)
    )
    atomic_json(state / "repost-proposals" / "latest.json", receipt)
    return {
        **receipt,
        "state": "READY_FOR_EXISTING_REPOST_OWNER" if changed else "ALREADY_PROPOSED",
        "changed": changed,
    }


def create_x_distribution_job(state, proposal):
    """Queue one public, effect-bound Affiliate job for the existing X owner."""
    if not isinstance(proposal, dict):
        return {"state": "WAITING_FOR_REPOST_PROPOSAL", "changed": False}
    plan_id = proposal.get("plan_id")
    placement_id = proposal.get("placement_id")
    owned_url = proposal.get("owned_article_url")
    if not (
        isinstance(plan_id, str) and plan_id
        and isinstance(placement_id, str)
        and REPOST_PLACEMENT_ID_PATTERN.fullmatch(placement_id)
        and is_owned_article_url(owned_url)
    ):
        return {"state": "WAITING_FOR_REPOST_PROPOSAL", "changed": False}
    try:
        campaign = json.loads((
            state / "campaign-publications" / f"{plan_id}.json"
        ).read_text(encoding="utf-8"))
        policy_path = state / "campaign-policy" / f"{plan_id}.json"
        policy_bytes = policy_path.read_bytes()
        policy = json.loads(policy_bytes)
    except (OSError, ValueError):
        return {"state": "WAITING_FOR_POLICY_PASS", "changed": False}
    content_sha256 = campaign.get("content_sha256")
    source_set_sha256 = policy.get("source_set_sha256")
    checks = policy.get("checks")
    if not all((
        campaign.get("placement_id") == placement_id,
        campaign.get("owned_url") == owned_url,
        isinstance(content_sha256, str)
        and REPOST_PROPOSAL_ID_PATTERN.fullmatch(content_sha256),
        policy.get("receipt_type") == "GENERIC_CAMPAIGN_POLICY",
        policy.get("state") == policy.get("decision") == "PASS",
        policy.get("plan_id") == plan_id,
        isinstance(checks, dict) and checks and all(checks.values()),
        (policy.get("semantic_audit") or {}).get("decision") == "PASS",
        not (policy.get("semantic_audit") or {}).get("unsupported_claims"),
        isinstance(source_set_sha256, str)
        and REPOST_PROPOSAL_ID_PATTERN.fullmatch(source_set_sha256),
    )):
        return {"state": "WAITING_FOR_POLICY_PASS", "changed": False}
    slug = campaign.get("slug")
    try:
        owned = json.loads((
            state / "owned-publications" / f"{slug}.json"
        ).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"state": "WAITING_FOR_OWNED_READBACK", "changed": False}
    if not all((
        owned.get("state") == "LIVE",
        owned.get("public_url") == owned_url,
        owned.get("content_sha256") == content_sha256,
        isinstance(owned.get("rendered_sha256"), str)
        and REPOST_PROPOSAL_ID_PATTERN.fullmatch(owned["rendered_sha256"]),
    )):
        return {"state": "WAITING_FOR_OWNED_READBACK", "changed": False}
    try:
        x_profile = json.loads((
            Path(__file__).resolve().parents[1] / "config" / "x-profiles" / "en.json"
        ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "WAITING_FOR_X_TARGET", "changed": False}
    target = x_profile.get("handle")
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", target):
        return {"state": "WAITING_FOR_X_TARGET", "changed": False}
    experiment = campaign.get("experiment")
    if isinstance(experiment, dict):
        decision_id = experiment.get("decision_id")
        control = experiment.get("control_placement_id")
        if not (
            isinstance(decision_id, str) and REPOST_PROPOSAL_ID_PATTERN.fullmatch(decision_id)
            and isinstance(control, str) and REPOST_PLACEMENT_ID_PATTERN.fullmatch(control)
        ):
            return {"state": "WAITING_FOR_EXPERIMENT_LINEAGE", "changed": False}
        lineage = {
            "kind": "EXPERIMENT",
            "decision_id": decision_id,
            "control_placement_id": control,
        }
    else:
        lineage = {"kind": "BASE", "decision_id": None, "control_placement_id": None}
    effect_core = {
        "placement_id": placement_id,
        "owned_article_url": owned_url,
        "content_sha256": content_sha256,
        "target_x_account": target,
        "cadence_class": "AFFILIATE_MONETIZATION",
    }
    effect_identity = hashlib.sha256(json.dumps(
        effect_core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    job_material = {
        **effect_core,
        "effect_identity": effect_identity,
        "experiment_lineage": lineage,
        "policy_sha256": policy_sha256,
        "source_set_sha256": source_set_sha256,
    }
    job_id = hashlib.sha256(json.dumps(
        job_material, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    existing = next((
        row for row in json_rows(state / "x-distribution-jobs.jsonl")
        if row.get("effect_identity") == effect_identity
    ), None)
    if existing:
        return {**existing, "state": "ALREADY_QUEUED", "changed": False}
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_X_DISTRIBUTION_JOB",
        "state": "QUEUED",
        "job_id": job_id,
        **job_material,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "private_tracking_url_state": "NOT_INCLUDED",
        "revenue_credit_state": "NO_REVENUE_CREDIT",
    }
    changed = append_unique(
        state / "x-distribution-jobs.jsonl", receipt, ("effect_identity",)
    )
    if not changed:
        existing = next(
            row for row in json_rows(state / "x-distribution-jobs.jsonl")
            if row.get("effect_identity") == effect_identity
        )
        return {**existing, "state": "ALREADY_QUEUED", "changed": False}
    atomic_json(state / "x-distribution-jobs" / f"{job_id}.json", receipt)
    atomic_json(state / "x-distribution-jobs" / "latest.json", receipt)
    return {**receipt, "changed": True}


def create_x_recirculation_job(state, plan, route=None):
    """Queue the one content-preserving X child selected by a distribution plan."""
    if not isinstance(plan, dict) or plan.get("state") != "READY":
        return {"state": "WAITING_FOR_DISTRIBUTION_PLAN", "changed": False}
    if not all((
        plan.get("selected_variable") == "distribution_mix",
        plan.get("next_action") == "SAFE_X_RECIRCULATION",
        plan.get("content_mutation_allowed") is False,
    )):
        raise ValueError("invalid recirculation plan")
    plan_id = plan.get("plan_id")
    decision_id = plan.get("decision_id")
    control_job_id = plan.get("control_job_id")
    control_placement = plan.get("control_placement_id")
    if not all((
        isinstance(plan_id, str) and REPOST_PROPOSAL_ID_PATTERN.fullmatch(plan_id),
        isinstance(decision_id, str) and REPOST_PROPOSAL_ID_PATTERN.fullmatch(decision_id),
        isinstance(control_job_id, str)
        and REPOST_PROPOSAL_ID_PATTERN.fullmatch(control_job_id),
        isinstance(control_placement, str)
        and REPOST_PLACEMENT_ID_PATTERN.fullmatch(control_placement),
    )):
        raise ValueError("recirculation plan lineage invalid")
    mode = "QUOTE_CONTROL_POST"
    route_id = None
    if route is not None:
        if route.get("state") not in {"READY", "ALREADY_ROUTED"}:
            return {"state": "WAITING_FOR_DISTRIBUTION_ROUTE", "changed": False}
        if route.get("plan_id") != plan_id:
            raise ValueError("distribution route plan mismatch")
        if route.get("target") == "wait":
            return {"state": "ROUTE_WAIT", "changed": False}
        mode = {
            "x_self_quote": "QUOTE_CONTROL_POST",
            "x_relevant_external_quote": "QUOTE_RELEVANT_EXTERNAL",
        }.get(route.get("target"))
        route_id = route.get("route_id")
        if not (
            mode and isinstance(route_id, str)
            and REPOST_PROPOSAL_ID_PATTERN.fullmatch(route_id)
        ):
            raise ValueError("distribution route invalid")
    control_path = state / "x-distribution-jobs" / f"{control_job_id}.json"
    try:
        control = json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("control distribution job unavailable") from error
    if not all((
        control.get("job_id") == control_job_id,
        control.get("placement_id") == control_placement,
        control.get("content_sha256") == plan.get("control_content_sha256"),
    )):
        raise ValueError("recirculation control mismatch")
    suffix = f"-mix-{plan_id[:8]}"
    placement_id = f"{control_placement[:80 - len(suffix)].rstrip('-')}{suffix}"
    effect_core = {
        "placement_id": placement_id,
        "owned_article_url": control["owned_article_url"],
        "content_sha256": control["content_sha256"],
        "target_x_account": control["target_x_account"],
        "cadence_class": control["cadence_class"],
        "distribution_mode": mode,
        "control_post_url": plan.get("control_post_url"),
    }
    if route_id:
        effect_core["distribution_route_id"] = route_id
    if not isinstance(effect_core["control_post_url"], str) or not re.fullmatch(
        r"https://x\.com/[A-Za-z0-9_]{1,15}/status/[0-9]+",
        effect_core["control_post_url"],
    ):
        raise ValueError("recirculation control post unavailable")
    effect_identity = hashlib.sha256(json.dumps(
        effect_core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    lineage = {
        "kind": "EXPERIMENT", "decision_id": decision_id,
        "control_placement_id": control_placement,
    }
    job_material = {
        **effect_core,
        "effect_identity": effect_identity,
        "experiment_lineage": lineage,
        "policy_sha256": control["policy_sha256"],
        "source_set_sha256": control["source_set_sha256"],
    }
    job_id = hashlib.sha256(json.dumps(
        job_material, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    existing = next((
        row for row in json_rows(state / "x-distribution-jobs.jsonl")
        if row.get("effect_identity") == effect_identity
    ), None)
    if existing:
        return {**existing, "state": "ALREADY_QUEUED", "changed": False}
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_X_DISTRIBUTION_JOB",
        "state": "QUEUED", "job_id": job_id,
        **job_material,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "private_tracking_url_state": "NOT_INCLUDED",
        "revenue_credit_state": "NO_REVENUE_CREDIT",
    }
    changed = append_unique(
        state / "x-distribution-jobs.jsonl", receipt, ("effect_identity",)
    )
    if not changed:
        raise ValueError("recirculation effect identity race")
    atomic_json(state / "x-distribution-jobs" / f"{job_id}.json", receipt)
    atomic_json(state / "x-distribution-jobs" / "latest.json", receipt)
    return {**receipt, "changed": True}


def latest_commission_rows(state):
    """Return one latest lifecycle row per provider transaction lineage."""
    latest = {}
    for index, row in enumerate(json_rows(state / "commission-ledger.jsonl")):
        provider = row.get("provider") or "unknown"
        transaction_id = row.get("provider_transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            continue
        observed = row.get("observed_at")
        try:
            order_time = datetime.fromisoformat(
                observed.replace("Z", "+00:00")
            ) if isinstance(observed, str) and observed else None
        except ValueError:
            order_time = None
        order = (order_time or datetime.min.replace(tzinfo=timezone.utc), index)
        identity = (provider, transaction_id)
        prior = latest.get(identity)
        if prior is None or order > prior[0]:
            latest[identity] = (order, row)
    return [row for _, row in latest.values()]


def latest_live_url(state):
    receipts = list((state / "x-posts").glob("*.json")) + list(
        (state / "owned-publications").glob("*.json")
    )
    live = []
    for path in receipts:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if row.get("state") == "LIVE" and str(row.get("public_url", "")).startswith("https://"):
            live.append(row)
    return max(live, key=lambda row: row.get("observed_at", ""))["public_url"] if live else None


def latest_live_campaign(state):
    live = []
    for path in (state / "campaign-publications").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if row.get("state") == "X_LIVE" and str(row.get("x_url", "")).startswith("https://"):
            live.append(row)
    return max(live, key=lambda row: row.get("created_at", "")) if live else {}


def advance_devto_distribution(state, now=None, cooldown_seconds=86400):
    """Syndicate at most one X_LIVE campaign per day through the DEV adapter."""
    from devto_publish import publish

    now = int(time.time()) if now is None else now
    receipts = []
    for path in (state / "devto-publications").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            receipts.append(row)
        except (OSError, ValueError):
            continue
    observed = []
    for row in receipts:
        try:
            observed.append(int(datetime.fromisoformat(
                row["observed_at"].replace("Z", "+00:00")
            ).timestamp()))
        except (KeyError, TypeError, ValueError):
            continue
    if observed and now - max(observed) < cooldown_seconds:
        return {"state": "COOLDOWN", "public_url": None, "changed": False}
    done = {row.get("plan_id") for row in receipts if row.get("state") == "LIVE"}
    due = []
    for path in (state / "campaign-publications").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if row.get("state") == "X_LIVE" and row.get("plan_id") not in done:
            due.append(row)
    if not due:
        return {"state": "ALREADY_LIVE", "public_url": None, "changed": False}
    selected = max(due, key=lambda row: row.get("created_at", ""))
    result = publish(state, selected["plan_id"])
    return {
        "state": result["state"], "public_url": result.get("public_url"),
        "plan_id": selected["plan_id"], "channel": "devto",
        "changed": not result.get("deduplicated", False),
    }


def advance_substack_distribution(state, now=None, cooldown_seconds=86400):
    """Syndicate at most one X_LIVE campaign per day through Substack."""
    from substack_publish import publish

    now = int(time.time()) if now is None else now
    receipts = []
    for path in (state / "substack-publications").glob("*.json"):
        try:
            receipts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    sent_ids = {row.get("event_uuid") for row in json_rows(state / "telegram-sent.jsonl")}
    for row in receipts:
        identity = {"kind": "DISTRIBUTION_LIVE", "channel": "substack",
                    "plan_id": row.get("plan_id"), "public_url": row.get("public_url")}
        event_uuid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if row.get("state") == "LIVE" and event_uuid not in sent_ids:
            return {"state": "LIVE", "public_url": row.get("public_url"),
                    "plan_id": row.get("plan_id"), "channel": "substack", "changed": True}
    observed = []
    for row in receipts:
        try:
            observed.append(int(datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")).timestamp()))
        except (KeyError, TypeError, ValueError):
            continue
    if observed and now - max(observed) < cooldown_seconds:
        return {"state": "COOLDOWN", "public_url": None, "changed": False, "channel": "substack"}
    done = {row.get("plan_id") for row in receipts if row.get("state") == "LIVE"}
    due = []
    for path in (state / "campaign-publications").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if row.get("state") == "X_LIVE" and row.get("plan_id") not in done:
            due.append(row)
    if not due:
        return {"state": "ALREADY_LIVE", "public_url": None, "changed": False, "channel": "substack"}
    selected = max(due, key=lambda row: row.get("created_at", ""))
    result = publish(state, selected["plan_id"])
    return {"state": result["state"], "public_url": result.get("public_url"),
            "plan_id": selected["plan_id"], "channel": "substack",
            "changed": not result.get("deduplicated", False)}


def observe_devto_acquisition(state, now=None, cooldown_seconds=3600):
    """Poll the existing DEV publication metrics without adding a scheduler."""
    from devto_publish import observe_metrics

    now = int(time.time()) if now is None else now
    receipt_path = state / "distribution-metrics" / "devto.json"
    try:
        prior = json.loads(receipt_path.read_text(encoding="utf-8"))
        observed = int(datetime.fromisoformat(
            prior["observed_at"].replace("Z", "+00:00")
        ).timestamp())
    except (OSError, KeyError, TypeError, ValueError):
        prior, observed = {}, 0
    if prior.get("baseline_state") and observed and now - observed < cooldown_seconds:
        return {**prior, "state": "COOLDOWN"}
    return observe_metrics(state)


def owner_event(state, wake_event, sent_event_ids=None):
    sent_event_ids = sent_event_ids or set()
    commission_transitions = json_rows(state / "commission-ledger.jsonl")
    click_transitions = json_rows(state / "click-ledger.jsonl")
    campaign = latest_live_campaign(state)
    cycle_path = state / "revenue-cycle.json"
    try:
        cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cycle = {}
    try:
        metrics = json.loads(
            (state / "provider-metrics" / "elevenlabs.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        metrics = {}
    click_delta = metrics.get("delta_from_baseline", {}).get("clicks")
    impact_state = wake_event.get("impact_state")
    impact_changed = wake_event.get("impact_changed", False)
    candidates = []

    def add(
        kind, identity, money, public_url=None, article_url=None, decision=None,
        scope=None, recovery=None, next_job=None, dedupe_key=None,
    ):
        event_uuid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        candidates.append({
            "event_uuid": event_uuid, "kind": kind, "money": money,
            "public_url": public_url, "article_url": article_url,
            "decision": decision, "scope": scope, "recovery": recovery,
            "next_job": next_job, "dedupe_key": dedupe_key,
        })

    if wake_event.get("placement_link_changed") and wake_event.get("placement_link_state") == "VERIFIED":
        kind = "PLACEMENT_LINK_VERIFIED"
        add(kind, {
            "kind": kind, "provider": "elevenlabs",
            "placement_id": wake_event.get("placement_link_placement"),
            "provider_link_key": wake_event.get("placement_link_key"),
        }, "link verified / commission not observed yet")
    if (
        wake_event.get("publication_link_receipt_pending")
        and wake_event.get("publication_link_state") == "VERIFIED"
    ):
        kind = "PLACEMENT_LINK_VERIFIED"
        add(kind, {
            "kind": kind, "provider": "elevenlabs",
            "placement_id": wake_event.get("publication_link_placement"),
            "provider_link_key": wake_event.get("publication_link_key"),
        }, "link verified / commission not observed yet")
    if wake_event.get("distribution_changed") and wake_event.get("distribution_state") == "LIVE":
        kind = "DISTRIBUTION_LIVE"
        add(kind, {
            "kind": kind, "channel": wake_event.get("distribution_channel"),
            "plan_id": wake_event.get("distribution_plan_id"),
            "public_url": wake_event.get("distribution_url"),
        }, "LIVE / commission not observed yet", wake_event.get("distribution_url"))
    if impact_changed and impact_state in {"APPLICATION_PENDING", "APPROVED", "REJECTED"}:
        kind = f"PROGRAM_{impact_state}"
        add(kind, {
            "kind": kind,
            "provider": "hubspot-impact",
            "transition_id": wake_event.get("impact_transition_id"),
        }, "commission not observed yet")
    if wake_event.get("impact_login_reconciled_job_id"):
        kind = "SELF_HEALED"
        add(kind, {
            "kind": kind,
            "provider": "hubspot-impact",
            "job_id": wake_event["impact_login_reconciled_job_id"],
        }, "login effect reconciled from fresh authenticated readback", scope="impact-login")
    wake_history = wake_event_rows(state)
    if len(wake_history) >= 2:
        previous, current = wake_history[-2:]
        if (
            previous.get("publication_state") == "PUBLICATION_FAILED"
            and current.get("publication_state") != "PUBLICATION_FAILED"
        ):
            kind = "SELF_HEALED"
            add(kind, {
                "kind": kind, "scope": "publication",
                "failed_at": previous.get("ts"),
                "recovered_state": current.get("publication_state"),
            }, "publication retry recovered / commission not observed yet",
                current.get("publication_url") or latest_live_url(state),
                scope="publication")
    latest_revenue_failure = None
    latest_revenue_recovery = None
    for row in wake_history[:-1]:
        if row.get("revenue_state") == "REVENUE_CYCLE_FAILED":
            latest_revenue_failure = row
            latest_revenue_recovery = None
        elif row.get("revenue_state") in {"NO_TRANSACTIONS", "TRANSACTIONS_RECONCILED"}:
            if latest_revenue_failure is not None:
                latest_revenue_recovery = row
    recovery_event = (
        wake_event
        if wake_event.get("revenue_state") in {"NO_TRANSACTIONS", "TRANSACTIONS_RECONCILED"}
        else latest_revenue_recovery
    )
    if (
        latest_revenue_failure is not None
        and recovery_event is not None
    ):
        kind = "SELF_HEALED"
        add(kind, {
            "kind": kind, "scope": "revenue",
            "failed_at": latest_revenue_failure.get("ts"),
            "recovered_at": recovery_event.get("ts"),
            "recovered_state": recovery_event.get("revenue_state"),
        }, (
            "revenue capture recovered / transactions="
            f"{recovery_event.get('revenue_source_rows')} / no estimated revenue counted"
        ), recovery_event.get("publication_url") or latest_live_url(state),
            scope="revenue")
    if wake_event.get("revenue_state") == "REVENUE_CYCLE_FAILED":
        try:
            failure = json.loads((state / "revenue-cycle-failure.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failure = {}
        stage = failure.get("stage") or "UNKNOWN"
        failure_type = failure.get("failure_type") or "UNKNOWN"
        failure_class = failure.get("failure_class") or "UNKNOWN"
        retry_state = failure.get("retry_state") or "UNKNOWN"
        return_code = failure.get("return_code")
        retry_after = failure.get("retry_after")
        kind = "REVENUE_CYCLE_FAILED"
        add(kind, {
            "kind": kind,
            "provider": "elevenlabs",
            "stage": stage,
            "failure_type": failure_type,
            "failure_class": failure_class,
            "retry_state": retry_state,
            "retry_after": retry_after,
            "return_code": return_code,
            "error_sha256": failure.get("error_sha256"),
            "latest_source_artifact_sha256": failure.get("latest_source_artifact_sha256"),
            "observed_at": failure.get("observed_at"),
        }, (
            f"provider capture failed closed / stage={stage} / type={failure_type} / "
            f"class={failure_class} / retry={retry_state} / "
            "no transaction or estimated revenue counted"
        ), wake_event.get("publication_url") or latest_live_url(state), scope="revenue")
    if wake_event.get("acquisition_decision_state") == "DECISION_FAILED":
        kind = "ACQUISITION_DECISION_FAILED"
        failure_type = wake_event.get("acquisition_decision_failure_type") or "UNKNOWN"
        baseline_sha256 = wake_event.get("acquisition_decision_baseline_sha256") or "UNKNOWN"
        add(kind, {
            "kind": kind,
            "baseline_sha256": baseline_sha256,
            "failure_type": failure_type,
        }, f"acquisition decision failed: {failure_type} / no public effect", decision=(
            "同じbaselineを再試行します。公開・provider transaction・収益は未発生"
        ))
    if wake_event.get("acquisition_decision_changed"):
        kind = "ACQUISITION_DECISION_READY"
        add(kind, {
            "kind": kind,
            "decision_id": wake_event.get("acquisition_decision_id"),
        }, "commission not observed yet", decision=(
            f"実測baselineから「{wake_event.get('acquisition_decision_variable')}」を"
            f"1つだけ変更します。仮説: {wake_event.get('acquisition_decision_hypothesis')} "
            f"次の実行: {wake_event.get('acquisition_decision_instruction')}"
        ))
    repost = wake_event.get("repost_observation") or {}
    repost_proposal = wake_event.get("repost_proposal") or {}
    if (
        repost_proposal.get("changed")
        and repost_proposal.get("state") == "READY_FOR_EXISTING_REPOST_OWNER"
    ):
        kind = "REPOST_PROPOSAL_READY"
        add(kind, {
            "kind": kind,
            "proposal_id": repost_proposal.get("proposal_id"),
        }, (
            f"placement={repost_proposal.get('placement_id') or 'UNKNOWN'} / "
            "existing Repost owner handoff only / no post, click, or revenue credit"
        ), decision=(
            "英語・disclosure必須・tracking link非含有のplacement proposalを"
            "別Repost owner向けに保存しました"
        ), next_job=(
            "別Repost ownerがproposalを消費し、exact placement ID付きの"
            "public readbackを返すまで外部効果は主張しない"
        ))
    if repost.get("changed") and repost.get("state") == "OBSERVED":
        kind = "REPOST_OBSERVED"
        add(kind, {
            "kind": kind,
            "transition_id": repost.get("transition_id"),
        }, (
            f"既存Repostの投稿アクション={repost.get('post_action_count')}件 / "
            f"Affiliate campaign完全一致={repost.get('joined_campaign_count')}件 / "
            f"未結合={repost.get('unjoined_post_action_count')}件 / "
            "分母=投稿アクションのみ / revenue credit=0"
        ))
    for transition in commission_transitions:
        kind = {
            "pending": "COMMISSION_PENDING", "approved": "COMMISSION_APPROVED",
            "reversed": "COMMISSION_REVERSED", "paid": "COMMISSION_PAID",
        }.get(transition.get("status"), "COMMISSION_CHANGED")
        placement = transition.get("placement") or {}
        placement_id = placement.get("placement_id") or "UNKNOWN"
        add(kind, {"kind": kind, "transition_id": transition["transition_id"]}, (
            f"provider={transition.get('provider') or 'UNKNOWN'} / "
            f"transaction={transition.get('provider_transaction_id') or 'UNKNOWN'} / "
            f"placement={placement_id} / status={transition.get('status') or 'UNKNOWN'} / "
            f"gross={transition.get('gross_commission_minor') or 0} minor / "
            f"reversal={transition.get('reversal_minor') or 0} minor / "
            f"net={transition.get('net_commission_minor') or 0} minor / "
            f"currency={transition.get('currency') or 'UNKNOWN'} / "
            f"settlement={transition.get('provider_settlement_id') or 'UNKNOWN'} / "
            f"payout={transition.get('provider_payout_id') or 'UNKNOWN'}"
        ), placement.get("public_url"))
    for link_transition in click_transitions:
        if not isinstance(link_transition.get("delta_click_count"), int) or link_transition["delta_click_count"] <= 0:
            continue
        kind = "CLICK_DELTA"
        add(kind, {"kind": kind, "transition_id": link_transition["transition_id"]}, (
            f"provider link clicks=+{link_transition['delta_click_count']} / "
            "commission not observed yet"
        ), link_transition.get("public_url"))
    if isinstance(click_delta, int) and click_delta > 0:
        kind = "UNATTRIBUTED_CLICK_DELTA"
        metrics_row = metrics.get("metrics")
        if not isinstance(metrics_row, dict):
            metrics_row = {}
        signups = metrics_row.get("signups")
        paid_signups = metrics_row.get("paid_signups")
        conversion_rate = metrics_row.get("conversion_rate")
        add(kind, {
            "kind": kind, "provider": "elevenlabs",
            "metrics_sha256": metrics.get("metrics_sha256"),
            "clicks": metrics_row.get("clicks"),
            "signups": signups,
            "paid_signups": paid_signups,
            "conversion_rate": conversion_rate,
        }, (
            f"aggregate post-baseline clicks=+{click_delta} / "
            f"signups={signups if signups is not None else 'UNKNOWN'} / "
            f"paid_signups={paid_signups if paid_signups is not None else 'UNKNOWN'} / "
            f"conversion={conversion_rate if conversion_rate is not None else 'UNKNOWN'} / "
            "not attributable / commission not observed"
        ))
    if campaign:
        kind = "PLACEMENT_LIVE"
        add(kind, {"kind": kind, "plan_id": campaign.get("plan_id"), "x_url": campaign["x_url"]},
            "LIVE / commission not observed yet", campaign["x_url"], campaign.get("owned_url"))
    try:
        rolling_net = json.loads((state / "rolling-net.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rolling_net = {}
    if rolling_net.get("receipt_type") == "AFFILIATE_ROLLING_NET":
        kind = "AFFILIATE_ROLLING_NET"
        net_usd = rolling_net.get("approved_or_paid_net_usd")
        net_text = f"USD {net_usd:,.2f}" if isinstance(net_usd, (int, float)) else "UNKNOWN"
        add(kind, {
            "kind": kind,
            "source_ledger_sha256": rolling_net.get("source_ledger_sha256"),
            "source_transition_count": rolling_net.get("source_transition_count"),
            "status_counts": rolling_net.get("status_counts"),
            "approved_or_paid_net_minor_by_currency": rolling_net.get(
                "approved_or_paid_net_minor_by_currency"
            ),
            "reversal_minor_by_currency": rolling_net.get("reversal_minor_by_currency"),
            "cost_state": rolling_net.get("cost_state"),
            "cost_coverage_state": rolling_net.get("cost_coverage_state"),
            "net_state": rolling_net.get("net_state"),
            "threshold_state": rolling_net.get("threshold_state"),
        }, (
            f"{rolling_net.get('money_state')} / net={rolling_net.get('net_state')} / "
            f"threshold={rolling_net.get('threshold_state')} / "
            f"approved_or_paid_net={net_text} / "
            f"cost={rolling_net.get('cost_state')} / "
            f"cost_coverage={rolling_net.get('cost_coverage_state')}"
        ))
    if cycle.get("state") == "NO_TRANSACTIONS":
        kind = "REVENUE_RECONCILED"
        add(kind, {"kind": kind, "provider": "elevenlabs", "state": "NO_TRANSACTIONS"},
            "NO_TRANSACTIONS / gross=unknown / net=unknown / cost=unknown",
            wake_event.get("publication_url") or latest_live_url(state))
    if (
        (
            wake_event.get("status") not in ("READY_FOR_PUBLICATION",)
            or wake_event.get("cost_budget_state") == "COST_CAP_BLOCKED"
        )
        and wake_event.get("acquisition_decision_state") != "DECISION_FAILED"
    ):
        kind = "BLOCKED"
        blockers = []
        action_state = wake_event.get("action_budget_state")
        cost_state = wake_event.get("cost_budget_state")
        cost_blocked = cost_state == "COST_CAP_BLOCKED"
        if cost_blocked:
            known_minor = wake_event.get("cost_budget_known_actual_usd_minor")
            cap_minor = wake_event.get("cost_budget_cap_minor")
            known_text = (
                f"USD {known_minor / 100:,.2f}"
                if isinstance(known_minor, int) and not isinstance(known_minor, bool)
                else "UNKNOWN"
            )
            cap_text = (
                f"USD {cap_minor / 100:,.2f}"
                if isinstance(cap_minor, int) and not isinstance(cap_minor, bool)
                else "UNKNOWN"
            )
            blockers.append(
                f"external_cost_cap={known_text}/{cap_text}"
            )
        if action_state == "ACTION_CAP_BLOCKED":
            blockers.append(
                f"external_action_cap={wake_event.get('action_budget_used_attempts', 'UNKNOWN')}"
                f"/{wake_event.get('action_budget_daily_cap', 'UNKNOWN')}"
            )
        guard_state = wake_event.get("runtime_guard_state")
        if guard_state in {"DISK_GUARD_BLOCKED", "DISK_GUARD_UNKNOWN"}:
            free_bytes = wake_event.get("runtime_guard_free_bytes")
            floor_bytes = wake_event.get("runtime_guard_floor_bytes")
            blockers.append(
                f"runtime_disk={guard_state}"
                f"(free={free_bytes if isinstance(free_bytes, int) else 'UNKNOWN'}"
                f"/floor={floor_bytes if isinstance(floor_bytes, int) else 'UNKNOWN'} bytes)"
            )
        if not blockers:
            blockers.append(f"status={wake_event.get('status') or 'UNKNOWN'}")
        blocker_text = " / ".join(blockers)
        blocker_key = "BLOCKED:" + "|".join(
            (("COST_CAP_BLOCKED",) if cost_blocked else ())
            + (action_state or "CLEAR", guard_state or "CLEAR")
        )
        no_transactions = (
            cycle.get("state") == "NO_TRANSACTIONS"
            or wake_event.get("rolling_net_money_state") == "NO_TRANSACTIONS"
        )
        money = (
            "NO_TRANSACTIONS / approved_or_paid_net=USD 0.00 / cost=UNKNOWN / "
            f"no money counted / blocker={blocker_text}"
            if no_transactions else
            f"money=UNKNOWN / no money counted / blocker={blocker_text}"
        )
        recovery = (
            "外部作用は行わず、既知actual_billedのJST日次cost cap回復を再確認します。"
            "provider capture・ledger・Telegramの読取りは継続"
            if cost_blocked
            else
            "外部作用は行わず、JST日次capとディスクfloorの回復を再確認します。"
            "provider capture・ledger・Telegramの読取りは継続"
            if action_state == "ACTION_CAP_BLOCKED" and guard_state == "DISK_GUARD_BLOCKED"
            else "JST日次外部作用capがCLEARになるまで、公開・リンク作用を行わず読取りを継続"
            if action_state == "ACTION_CAP_BLOCKED"
            else "ディスク空きがfloorを満たすまで、新規生成・公開を行わず読取りを継続"
            if guard_state in {"DISK_GUARD_BLOCKED", "DISK_GUARD_UNKNOWN"}
            else "未解決の外部状態を再読取りし、作用なしで再試行を予約"
        )
        next_job = (
            "既知actual_billed costが次のJST日次capを下回った後、同じdurable placement jobを既存ownerが再開"
            if cost_blocked
            else
            "ディスク空きが10GiB以上かつJST日次capがCLEARになった後、"
            "同じdurable placement jobを既存ownerが再開（手動公開・captureはしない）"
            if action_state == "ACTION_CAP_BLOCKED" and guard_state == "DISK_GUARD_BLOCKED"
            else "JST日次capがCLEARになった後、同じdurable placement jobを既存ownerが再開"
            if action_state == "ACTION_CAP_BLOCKED"
            else "ディスク空きが10GiB以上になった後、同じdurable jobを既存ownerが再開"
            if guard_state in {"DISK_GUARD_BLOCKED", "DISK_GUARD_UNKNOWN"}
            else "未解決の外部状態を既存ownerが再読取り"
        )
        add(
            kind,
            {
                "kind": kind, "provider": "elevenlabs", "dedupe_key": blocker_key,
            },
            money, wake_event.get("publication_url") or latest_live_url(state),
            decision=blocker_text, recovery=recovery, next_job=next_job,
            dedupe_key=blocker_key,
        )
    selected = next(
        (candidate for candidate in candidates if candidate["event_uuid"] not in sent_event_ids),
        None,
    )
    if not selected:
        return None
    kind = selected["kind"]
    recovery = selected.get("recovery") or (
        "次のwakeが同じpublicationを再開し、重複作用なしで進行を回復しました"
        if kind == "SELF_HEALED" and selected.get("scope") == "publication"
        else "次のwakeが同じ収益captureを再実行し、provider readbackを回復しました"
        if kind == "SELF_HEALED" and selected.get("scope") == "revenue"
        else "provider captureの失敗を記録し、実取引なしで再試行を予約しました"
        if kind == "REVENUE_CYCLE_FAILED"
        else "Impactの認証済み画面から、未解決だった同じlogin jobを完了しました"
        if kind == "SELF_HEALED"
        else "なし" if kind != "BLOCKED"
        else "未回復の外部状態があります"
    )
    next_job = selected.get("next_job") or (
        "同じcampaignのpublic readbackと収益計測を継続"
        if kind == "SELF_HEALED" and selected.get("scope") == "publication"
        else "同じprovider transaction台帳を継続監視し、実取引だけをplacementへ照合"
        if kind == "SELF_HEALED" and selected.get("scope") == "revenue"
        else
        "同じ申請を再提出せず、Impactの審査状態を継続確認"
        if kind.startswith("PROGRAM_") or kind == "SELF_HEALED"
        else "同じrolling 30日net receiptを再計算し、実取引だけを監視"
        if kind == "AFFILIATE_ROLLING_NET"
        else "同じ公式provider captureを既存ownerが再試行し、手動captureは行わない"
        if kind == "REVENUE_CYCLE_FAILED"
        else "既存Repostの投稿アクションをowned記事・provider clickへ完全一致で結合"
        if kind == "REPOST_OBSERVED"
        else "provider transactionを待ち、sub-IDまたはlink fingerprintでplacementへ照合"
        if kind in {"CLICK_DELTA", "UNATTRIBUTED_CLICK_DELTA", "PLACEMENT_LINK_VERIFIED"}
        else "buyer-intentを収集し、次の公開・収益照合を継続"
    )
    program = (
        "X Repost / Affiliate acquisition"
        if kind == "REPOST_OBSERVED"
        else "ElevenLabs / PartnerStack"
        if kind == "SELF_HEALED" and selected.get("scope") in {"publication", "revenue"}
        else "HubSpot / Impact"
        if kind.startswith("PROGRAM_") or kind == "SELF_HEALED"
        else "ElevenLabs / PartnerStack"
    )
    body = "\n".join((
        "Life Manager Affiliate::: Affiliate loop report",
        f"実行: {kind}",
        f"公開先: {selected['public_url'] or '未紐付け'}",
        *((f"記事: {selected['article_url']}",) if selected.get("article_url") else ()),
        f"プログラム: {program}",
        f"お金: {selected['money']}",
        *((f"判断: {selected['decision']}",) if selected.get("decision") else ()),
        f"回復: {recovery}",
        f"次: {next_job}",
    ))
    result = {"event_uuid": selected["event_uuid"], "kind": kind, "body": body, "created_at": int(time.time())}
    if kind == "BLOCKED" and selected.get("dedupe_key", "").startswith("BLOCKED:COST_CAP_BLOCKED"):
        result["dedupe_key"] = selected["dedupe_key"]
    return result


def daily_summary_event(state, wake_event, now=None):
    now = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    report_date = now.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    try:
        placement_ledger = json.loads(
            (state / "placement-ledger.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        placement_ledger = {}
    ledger_placements = (
        placement_ledger.get("placements")
        if isinstance(placement_ledger.get("placements"), list)
        else []
    )
    live_plan_ids = {
        row.get("plan_id") for row in ledger_placements
        if isinstance(row, dict)
        and isinstance(row.get("plan_id"), str)
        and row.get("provider_link_key")
        and row.get("public_url")
    }
    budget_blocked_campaigns = []
    for receipt_path in (state / "composition-receipts").glob("*.json"):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not (
                receipt.get("state") == "FAILED"
                and receipt.get("failure_class") == "RUNNER_REJECTED"
            ):
                continue
            if receipt["plan_id"] in live_plan_ids:
                continue
            run_id = f"{receipt['plan_id']}-{receipt['source_set_sha256'][:16]}"
            summary = json.loads((
                state / "composition-runs" / run_id / "summary.json"
            ).read_text(encoding="utf-8"))
            budget = summary["budget"]
            if summary.get("status") == "budget_blocked" and budget.get("day") == report_date:
                plan_paths = (
                    state / "discovered-source-plans" / f"{receipt['plan_id']}.json",
                    Path(__file__).resolve().parents[1]
                    / "config" / "source-plans" / f"{receipt['plan_id']}.json",
                )
                plan = next((
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in plan_paths if path.is_file()
                ), {})
                budget_blocked_campaigns.append({
                    "plan_id": receipt["plan_id"],
                    "label": plan.get("buyer_intent") or "次の英語campaign",
                })
        except (OSError, ValueError, KeyError, TypeError):
            continue
    budget_blocked = len(budget_blocked_campaigns)
    owned_live = sum(
        json.loads(path.read_text(encoding="utf-8")).get("state") == "LIVE"
        for path in (state / "owned-publications").glob("*.json")
    )
    x_live = sum(
        json.loads(path.read_text(encoding="utf-8")).get("state") == "LIVE"
        for path in (state / "x-posts").glob("*.json")
    )
    try:
        links = json.loads(
            (state / "provider-reports" / "partnerstack-links" / "latest.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        links = {}
    link_report_placements = (
        links.get("placements") if isinstance(links.get("placements"), list) else []
    )
    if ledger_placements:
        placements = ledger_placements
        dedicated_link_count = sum(bool(row.get("provider_link_key")) for row in placements)
        observed_clicks = [
            row.get("provider_clicks", {}).get("count")
            for row in placements
            if isinstance(row.get("provider_clicks"), dict)
            and isinstance(row.get("provider_clicks", {}).get("count"), int)
        ]
        click_measurement_count = len(observed_clicks)
        click_unknown_count = max(dedicated_link_count - click_measurement_count, 0)
        link_clicks = sum(observed_clicks)
        observed_unique_clicks = [
            row.get("provider_clicks", {}).get("unique_count")
            for row in placements
            if isinstance(row.get("provider_clicks"), dict)
            and isinstance(row.get("provider_clicks", {}).get("unique_count"), int)
        ]
    else:
        placements = link_report_placements
        dedicated_link_count = len(placements)
        observed_clicks = [
            row.get("current_click_count")
            for row in placements
            if isinstance(row.get("current_click_count"), int)
        ]
        click_measurement_count = len(observed_clicks)
        click_unknown_count = max(dedicated_link_count - click_measurement_count, 0)
        link_clicks = sum(observed_clicks)
        observed_unique_clicks = [
            row.get("current_unique_click_count")
            for row in placements
            if isinstance(row.get("current_unique_click_count"), int)
        ]
    unique_click_measurement_count = len(observed_unique_clicks)
    unique_click_unknown_count = max(
        dedicated_link_count - unique_click_measurement_count, 0
    )
    unique_link_clicks = sum(observed_unique_clicks)
    try:
        devto_metrics = json.loads(
            (state / "distribution-metrics" / "devto.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        devto_metrics = {}
    commissions = latest_commission_rows(state)
    status_counts = {
        status: sum(row.get("status") == status for row in commissions)
        for status in ("pending", "approved", "paid", "reversed")
    }
    approved_by_currency = {}
    for row in commissions:
        if row.get("status") not in {"approved", "paid"}:
            continue
        currency = row.get("currency") or "UNKNOWN"
        approved_by_currency[currency] = approved_by_currency.get(currency, 0) + int(
            row.get("net_commission_minor") or 0
        )
    economic_stage = (
        "E0_PROVIDER_CLICK" if link_clicks == 0
        else "E1_APPROVED_COMMISSION"
        if not (status_counts["approved"] + status_counts["paid"])
        else "POST_E1_OPTIMIZATION"
    )
    wake_count_today = sum(
        datetime.fromtimestamp(row.get("ts", 0), ZoneInfo("Asia/Tokyo")).date().isoformat()
        == report_date
        for row in wake_event_rows(state)
        if isinstance(row.get("ts"), int)
    )
    identity = {
        "kind": "AFFILIATE_DAILY_SUMMARY",
        "date": report_date,
        "schema_version": 2,
    }
    event_uuid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    receipt = {
        "schema_version": 2,
        "receipt_type": "AFFILIATE_DAILY_SUMMARY",
        "report_date": report_date,
        "event_uuid": event_uuid,
        "wake_count_today": wake_count_today,
        "owned_live": owned_live,
        "x_live": x_live,
        "placement_count": len(placements),
        "dedicated_link_count": dedicated_link_count,
        "provider_link_clicks": link_clicks,
        "provider_click_measurement_count": click_measurement_count,
        "provider_click_unknown_count": click_unknown_count,
        "provider_unique_clicks": unique_link_clicks,
        "provider_unique_click_measurement_count": unique_click_measurement_count,
        "provider_unique_click_unknown_count": unique_click_unknown_count,
        "devto_article_count": devto_metrics.get("article_count"),
        "devto_page_views": devto_metrics.get("total_page_views"),
        "devto_page_view_delta": devto_metrics.get("delta_page_views"),
        "devto_baseline_state": devto_metrics.get("baseline_state"),
        "devto_baseline_receipt_count": devto_metrics.get("baseline_receipt_count"),
        "commission_status_counts": status_counts,
        "approved_or_paid_net_minor_by_currency": approved_by_currency,
        "provider_observed_at": links.get("observed_at"),
        "provider_state": wake_event.get("provider_state"),
        "impact_state": wake_event.get("impact_state"),
        "systeme_state": wake_event.get("systeme_state"),
        "economic_stage": economic_stage,
        "composition_budget_blocked_count": budget_blocked,
        "composition_budget_blocked_campaigns": budget_blocked_campaigns,
        "created_at": int(now.timestamp()),
    }
    atomic_json(state / "daily-summaries" / f"{report_date}.json", receipt)
    if approved_by_currency:
        approved_text = "、".join(
            f"{currency} {minor / 100:,.2f}"
            for currency, minor in sorted(approved_by_currency.items())
        )
    else:
        approved_text = "USD 0.00"
    try:
        observed_text = datetime.fromisoformat(links["observed_at"]).astimezone(
            ZoneInfo("Asia/Tokyo")
        ).strftime("%Y-%m-%d %H:%M JST")
        observed_line = f"最終provider確認は{observed_text}です。"
    except (KeyError, TypeError, ValueError):
        observed_line = "最終provider確認はまだ取得できていません。"
    state_text = {
        "AUTHENTICATED": "ログイン済み",
        "APPLICATION_PENDING": "申請審査中",
        "REJECTED": "申請却下",
        "CAPTCHA_CHALLENGE": "CAPTCHAの外部確認待ち",
        None: "状態未取得",
    }
    stage_text = {
        "E0_PROVIDER_CLICK": "専用リンクで最初の外部クリックを確認する段階です。",
        "E1_APPROVED_COMMISSION": "クリックから最初の承認済み報酬を確認する段階です。",
        "POST_E1_OPTIMIZATION": "実測収益を使って次のcampaignを選ぶ段階です。",
    }[economic_stage]
    if budget_blocked:
        campaign_text = "、".join(
            row["label"] for row in budget_blocked_campaigns[:2]
        )
        if budget_blocked > 2:
            campaign_text += f"ほか{budget_blocked - 2}件"
        next_action = (
            f"現在の制作対象「{campaign_text}」は、本日の安全な生成予算上限を守って保留しています。"
            "Agentは次のJST予算で同じ仕事を自動再開し、その間もprovider確認、"
            "公開計測、収益照合を継続します。"
        )
    else:
        next_action = (
            "Agentは現在の実測値を収集し、次に実行可能なcampaignを1件だけ進めます。"
        )
    body = "\n".join((
        "Life Manager Affiliate::: 今日の運用報告です。",
        f"{report_date}は、Affiliate loopが{receipt['wake_count_today']}回動きました。",
        f"現在、owned記事は{owned_live}本、X投稿は{x_live}件が公開状態です。",
        (
            f"正規台帳には{len(placements)}配信面、PartnerStack専用リンクは"
            f"{dedicated_link_count}本あります。配信面別に観測できた"
            f"{click_measurement_count}本の外部クリックは合計{link_clicks}件です。"
        ),
        (
            f"残り{click_unknown_count}本のクリック値はprovider未観測のため、"
            "0件として扱っていません。"
            if click_unknown_count
            else "全専用リンクの配信面別クリック値をproviderから観測できています。"
        ),
        (
            f"uniqueクリックは観測済み{unique_link_clicks}件です。"
            f"残り{unique_click_unknown_count}本はprovider未観測で、分母を推測していません。"
            if unique_click_unknown_count
            else f"uniqueクリックは配信面別に{unique_link_clicks}件をproviderから観測できています。"
        ),
        (
            f"DEVではAffiliate記事{devto_metrics.get('article_count', 0)}本が"
            f"合計{devto_metrics.get('total_page_views', 0)}回閲覧され、"
            f"前回確認からの増加は{devto_metrics.get('delta_page_views', 0)}回です。"
        ),
        (
            "DEVの24時間reach baselineは確定済みです。"
            if devto_metrics.get("baseline_state") == "READY"
            else "DEVの24時間reach baselineは観測中です。"
        ),
        (
            "報酬は、保留"
            f"{status_counts['pending']}件、承認{status_counts['approved']}件、"
            f"支払済み{status_counts['paid']}件、取消{status_counts['reversed']}件です。"
        ),
        f"承認済み以上の純報酬は{approved_text}です。clickや保留報酬は収益に含めていません。",
        observed_line,
        (
            f"ElevenLabsは{state_text.get(receipt['provider_state'], '確認が必要な状態')}、"
            f"HubSpotは{state_text.get(receipt['impact_state'], '確認が必要な状態')}、"
            f"Systeme.ioは{state_text.get(receipt['systeme_state'], '確認が必要な状態')}です。"
        ),
        f"次の経済stageは、{stage_text}",
        f"次のAgent行動は、{next_action}",
    ))
    return {
        "event_uuid": event_uuid,
        "kind": "AFFILIATE_DAILY_SUMMARY",
        "body": body,
        "created_at": receipt["created_at"],
    }


def next_telegram_event(state, wake_event):
    sent_ids = {row.get("event_uuid") for row in json_rows(state / "telegram-sent.jsonl")}
    event = owner_event(state, wake_event, sent_ids)
    if event:
        return event
    return daily_summary_event(state, wake_event)


def find_message_id(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.replace("_", "").lower() == "messageid" and item is not None:
                return str(item)
            found = find_message_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_message_id(item)
            if found:
                return found
    return None


def telegram_dedupe_key(row):
    """Return a stable state key for owner reports whose measurements may drift."""
    if not isinstance(row, dict) or row.get("kind") != "BLOCKED":
        return None
    if isinstance(row.get("dedupe_key"), str) and row["dedupe_key"]:
        return row["dedupe_key"]
    body = row.get("body") if isinstance(row.get("body"), str) else ""
    cost = "COST_CAP_BLOCKED" if "external_cost_cap=" in body else "CLEAR"
    action = "ACTION_CAP_BLOCKED" if "external_action_cap=" in body else "CLEAR"
    if "runtime_disk=DISK_GUARD_BLOCKED" in body:
        guard = "DISK_GUARD_BLOCKED"
    elif "runtime_disk=DISK_GUARD_UNKNOWN" in body:
        guard = "DISK_GUARD_UNKNOWN"
    else:
        guard = "CLEAR"
    if cost == "CLEAR":
        if action == "CLEAR" and guard == "CLEAR":
            return "BLOCKED:LEGACY"
        return f"BLOCKED:{action}|{guard}"
    return f"BLOCKED:{cost}|{action}|{guard}"


def supersede_telegram_rows(state, outbox, sent_by_id):
    """Exclude equivalent pending reports without claiming a provider delivery."""
    superseded_path = state / "telegram-superseded.jsonl"
    superseded_ids = {
        row.get("event_uuid") for row in json_rows(superseded_path)
        if row.get("event_uuid")
    }
    sent_keys = {
        telegram_dedupe_key(outbox_row)
        for event_uuid in sent_by_id
        for outbox_row in outbox
        if outbox_row.get("event_uuid") == event_uuid
    }
    sent_keys.discard(None)
    candidates_by_key = {}
    for index, row in enumerate(outbox):
        key = telegram_dedupe_key(row)
        if key:
            candidates_by_key.setdefault(key, []).append((index, row))
    for key, candidates in candidates_by_key.items():
        if key in sent_keys:
            keep = None
        else:
            keep = max(
                candidates,
                key=lambda item: (item[1].get("created_at") or 0, item[0]),
            )[1]
        for _, row in candidates:
            if row.get("event_uuid") == (keep or {}).get("event_uuid"):
                continue
            event_uuid = row.get("event_uuid")
            if not event_uuid or event_uuid in sent_by_id or event_uuid in superseded_ids:
                continue
            canonical = (
                next((item[1].get("event_uuid") for item in candidates if item[1] is keep), None)
                if keep else next((item[1].get("event_uuid") for item in candidates if item[1].get("event_uuid") in sent_by_id), None)
            )
            receipt = {
                "schema_version": 1,
                "receipt_type": "AFFILIATE_TELEGRAM_SUPERSEDED",
                "event_uuid": hashlib.sha256(json.dumps({
                    "event_uuid": event_uuid, "dedupe_key": key,
                    "canonical_event_uuid": canonical,
                }, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "superseded_event_uuid": event_uuid,
                "canonical_event_uuid": canonical,
                "dedupe_key": key,
                "reason": "EQUIVALENT_REPORT_ALREADY_DELIVERED" if key in sent_keys else "NEWER_EQUIVALENT_REPORT",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            append_unique(superseded_path, receipt, ("event_uuid",))
            superseded_ids.add(event_uuid)
    return superseded_ids


def flush_telegram(state, event, runner=subprocess.run):
    requested_event_uuid = event.get("event_uuid") if event else None
    if event:
        append_unique(state / "telegram-outbox.jsonl", event, ("event_uuid",))
    outbox = json_rows(state / "telegram-outbox.jsonl")
    sent_rows = json_rows(state / "telegram-sent.jsonl")
    sent_ids = {row.get("event_uuid") for row in sent_rows}
    sent_by_id = {row.get("event_uuid"): row for row in sent_rows}
    superseded_ids = supersede_telegram_rows(state, outbox, sent_by_id)
    # A transport timeout is an ambiguous external effect: Telegram may have
    # accepted the message even though the CLI never returned its messageId.
    # Retrying such a row under the same local identity is not replay safety;
    # OpenClaw's message CLI has no provider idempotency key and real duplicates
    # were observed in the sibling X owner. Quarantine it for readback instead.
    ambiguous_ids = {
        row.get("telegram_event_uuid")
        for row in json_rows(state / "events.jsonl")
        if row.get("receipt_type") == "AFFILIATE_TELEGRAM_DELIVERY"
        and row.get("delivery_state") == "SEND_TIMEOUT_UNKNOWN"
        and row.get("telegram_event_uuid")
    }
    for row in outbox:
        if row.get("event_uuid") in sent_by_id:
            sent = sent_by_id[row["event_uuid"]]
            reconcile_effect(state, "TELEGRAM_SEND", row["event_uuid"], {
                "state": "SENT", "event_uuid": row["event_uuid"], "message_id": sent.get("message_id"),
            })
    pending = [
        row for row in outbox
        if row.get("event_uuid") not in sent_ids
        and row.get("event_uuid") not in superseded_ids
        and row.get("event_uuid") not in ambiguous_ids
    ]
    if not pending:
        requested_ambiguous = requested_event_uuid in ambiguous_ids
        return {
            "state": "AMBIGUOUS_NO_RETRY" if requested_ambiguous else "NO_PENDING", "sent": 0,
            "message_id": (sent_by_id.get(requested_event_uuid) or {}).get("message_id"),
            "sent_event_uuid": (
                requested_event_uuid
                if requested_event_uuid in sent_by_id or requested_ambiguous else None
            ),
        }
    openclaw = shutil.which("openclaw")
    if not openclaw:
        return {
            "state": "TRANSPORT_UNAVAILABLE", "sent": 0, "message_id": None,
            "sent_event_uuid": pending[0]["event_uuid"],
        }
    row = pending[0]
    try:
        job = resume_effect(state, "TELEGRAM_SEND", row["event_uuid"]) or start_effect(
            state, "TELEGRAM_SEND", row["event_uuid"],
            {"channel": "telegram", "event_uuid": row["event_uuid"],
             "body_sha256": hashlib.sha256(row["body"].encode()).hexdigest()},
            {"state": "NOT_SENT", "event_uuid": row["event_uuid"]}, 60,
        )
    except JobStateError:
        return {
            "state": "RECONCILE_REQUIRED", "sent": 0, "message_id": None,
            "sent_event_uuid": row["event_uuid"],
        }
    try:
        completed = runner(
            [openclaw, "message", "send", "--channel", "telegram", "--target", "8547730585",
             "--message", row["body"], "--json"],
            check=False, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        # The provider may have accepted the send before the CLI timed out.
        # Keep the write-ahead effect unresolved and the event pending; never
        # claim SENT or invent a message ID from a transport timeout.
        return {
            "state": "SEND_TIMEOUT_UNKNOWN", "sent": 0, "message_id": None,
            "sent_event_uuid": row["event_uuid"],
        }
    except OSError:
        return {
            "state": "TRANSPORT_UNAVAILABLE", "sent": 0, "message_id": None,
            "sent_event_uuid": row["event_uuid"],
        }
    try:
        response = json.loads(completed.stdout)
    except ValueError:
        response = None
    message_id = find_message_id(response)
    if completed.returncode or not message_id:
        return {
            "state": "SEND_FAILED", "sent": 0, "message_id": None,
            "sent_event_uuid": row["event_uuid"],
        }
    append_unique(state / "telegram-sent.jsonl", {
        "event_uuid": row["event_uuid"], "message_id": message_id,
        "sent_at": int(time.time()),
    }, ("event_uuid",))
    verify_effect(state, job["job_id"], {
        "state": "SENT", "event_uuid": row["event_uuid"], "message_id": message_id,
    })
    return {
        "state": "SENT", "sent": 1, "message_id": message_id,
        "sent_event_uuid": row["event_uuid"],
    }


def append_telegram_delivery_receipt(state, wake_event, telegram_event, delivery):
    """Persist Telegram enqueue/attempt/result beside the wake that caused it."""
    wake_uuid = wake_event.get("wake_event_uuid") or wake_event_uuid(wake_event)
    sent_event_uuid = delivery.get("sent_event_uuid")
    if sent_event_uuid and sent_event_uuid != telegram_event.get("event_uuid"):
        telegram_event = next(
            (
                row for row in json_rows(state / "telegram-outbox.jsonl")
                if row.get("event_uuid") == sent_event_uuid
            ),
            telegram_event,
        )
    telegram_uuid = (
        telegram_event.get("event_uuid")
        if isinstance(telegram_event, dict) else None
    )
    delivery_state = delivery.get("state") or "UNKNOWN"
    message_id = delivery.get("message_id")
    attempted = delivery_state in {
        "SENT", "SEND_FAILED", "SEND_TIMEOUT_UNKNOWN", "AMBIGUOUS_NO_RETRY",
    }
    if delivery_state == "SENT":
        delivery_result = "DELIVERED"
    elif delivery_state == "NO_PENDING" and message_id:
        delivery_result = "ALREADY_DELIVERED"
    elif delivery_state == "NO_PENDING":
        delivery_result = "NO_PENDING"
    elif delivery_state == "SEND_FAILED":
        delivery_result = "FAILED"
    else:
        delivery_result = "UNKNOWN"
    failure_type = (
        delivery_state
        if delivery_state in {
            "SEND_FAILED", "SEND_TIMEOUT_UNKNOWN", "TRANSPORT_UNAVAILABLE",
            "RECONCILE_REQUIRED", "AMBIGUOUS_NO_RETRY",
        }
        else None
    )
    identity = {
        "wake_event_uuid": wake_uuid,
        "telegram_event_uuid": telegram_uuid,
        "delivery_state": delivery_state,
        "provider_message_id": message_id,
    }
    receipt = {
        "event": "affiliate_telegram_delivery",
        "schema_version": 1,
        "receipt_type": "AFFILIATE_TELEGRAM_DELIVERY",
        "event_uuid": hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "wake_event_uuid": wake_uuid,
        "wake_ts": wake_event.get("ts"),
        "telegram_event_uuid": telegram_uuid,
        "telegram_kind": telegram_event.get("kind") if isinstance(telegram_event, dict) else None,
        "telegram_created_at": telegram_event.get("created_at") if isinstance(telegram_event, dict) else None,
        "enqueue_state": "ENQUEUED" if telegram_uuid else "NOT_ENQUEUED",
        "attempt_state": "ATTEMPTED" if attempted else "NOT_ATTEMPTED",
        "delivery_state": delivery_state,
        "delivery_result": delivery_result,
        "provider_message_id": message_id,
        "failure_type": failure_type,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    append_unique(state / "events.jsonl", receipt, ("event_uuid",))
    return receipt


def reconcile_telegram_delivery_history(state, wake_event):
    """Reconcile sent-ledger message IDs into append-only canonical receipts."""
    sent_rows = json_rows(state / "telegram-sent.jsonl")
    outbox = {
        row.get("event_uuid"): row
        for row in json_rows(state / "telegram-outbox.jsonl")
        if row.get("event_uuid")
    }
    delivery_rows = [
        row for row in json_rows(state / "events.jsonl")
        if row.get("receipt_type") == "AFFILIATE_TELEGRAM_DELIVERY"
    ]
    exact_receipt_pairs = {
        (row.get("telegram_event_uuid"), str(row.get("provider_message_id")))
        for row in delivery_rows
        if row.get("delivery_state") in {"SENT", "NO_PENDING"}
        and row.get("provider_message_id") is not None
    }
    message_to_event = {
        str(row.get("message_id")): row.get("event_uuid")
        for row in sent_rows
        if row.get("event_uuid") and row.get("message_id") is not None
    }
    repaired = []
    for sent in sent_rows:
        telegram_uuid = sent.get("event_uuid")
        message_id = sent.get("message_id")
        if not telegram_uuid or message_id is None:
            continue
        message_id = str(message_id)
        timeout_rows = [
            row for row in delivery_rows
            if row.get("telegram_event_uuid") == telegram_uuid
            and row.get("delivery_state") == "SEND_TIMEOUT_UNKNOWN"
        ]
        if timeout_rows:
            timeout = timeout_rows[-1]
            repair_identity = {
                "type": "AFFILIATE_REPAIR_RECEIPT",
                "repair_kind": "TELEGRAM_SEND_RESUME",
                "telegram_event_uuid": telegram_uuid,
                "provider_message_id": message_id,
            }
            repair = {
                "event": "affiliate_repair",
                "receipt_type": "AFFILIATE_REPAIR_RECEIPT",
                "schema_version": 1,
                "event_uuid": hashlib.sha256(
                    json.dumps(repair_identity, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "repair_kind": "TELEGRAM_SEND_RESUME",
                "diagnosis": {
                    "failure_type": "SEND_TIMEOUT_UNKNOWN",
                    "telegram_delivery_event_uuid": timeout.get("event_uuid"),
                },
                "repair": {
                    "action": "RESUME_SAME_TELEGRAM_SEND",
                    "same_telegram_event_uuid": telegram_uuid,
                },
                "postcondition": {
                    "state": "SENT",
                    "provider_message_id": message_id,
                },
                "outcome": "SELF_HEALED",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            if append_unique(state / "repair-receipts.jsonl", repair, ("event_uuid",)):
                repaired.append(repair)
        related = [
            row for row in delivery_rows
            if row.get("telegram_event_uuid") == telegram_uuid
            or str(row.get("provider_message_id")) == message_id
        ]
        if not related:
            continue
        if (telegram_uuid, message_id) in exact_receipt_pairs:
            continue
        superseded = []
        for row in delivery_rows:
            same_event = row.get("telegram_event_uuid") == telegram_uuid
            same_message = str(row.get("provider_message_id")) == message_id
            expected_event = message_to_event.get(message_id)
            misbound = same_message and expected_event and expected_event != row.get("telegram_event_uuid")
            if same_event or misbound:
                superseded.append(row.get("event_uuid"))
        identity = {
            "type": "AFFILIATE_TELEGRAM_DELIVERY_RECONCILIATION",
            "telegram_event_uuid": telegram_uuid,
            "provider_message_id": message_id,
        }
        event = {
            "event": "affiliate_telegram_delivery_reconciliation",
            "receipt_type": identity["type"],
            "schema_version": 1,
            "event_uuid": hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "reconciled_at_wake_uuid": wake_event_uuid(wake_event),
            "telegram_event_uuid": telegram_uuid,
            "telegram_kind": outbox.get(telegram_uuid, {}).get("kind"),
            "provider_message_id": message_id,
            "reconciliation_state": "RECONCILED_FROM_SENT_LEDGER",
            "superseded_receipt_event_uuids": sorted(filter(None, superseded)),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if append_unique(state / "events.jsonl", event, ("event_uuid",)):
            repaired.append(event)
    for prior in json_rows(state / "events.jsonl"):
        if prior.get("receipt_type") != "AFFILIATE_TELEGRAM_DELIVERY_RECONCILIATION":
            continue
        telegram_uuid = prior.get("telegram_event_uuid")
        message_id = str(prior.get("provider_message_id"))
        related = [
            row for row in delivery_rows
            if row.get("telegram_event_uuid") == telegram_uuid
            or str(row.get("provider_message_id")) == message_id
        ]
        if related:
            continue
        identity = {
            "type": "AFFILIATE_TELEGRAM_DELIVERY_RECONCILIATION_RETRACTION",
            "reconciliation_event_uuid": prior.get("event_uuid"),
        }
        event = {
            "event": "affiliate_telegram_delivery_reconciliation",
            "receipt_type": identity["type"],
            "schema_version": 1,
            "event_uuid": hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "reconciled_at_wake_uuid": wake_event_uuid(wake_event),
            "telegram_event_uuid": telegram_uuid,
            "provider_message_id": message_id,
            "reconciliation_state": "RETRACTED_INSUFFICIENT_BASE_EVIDENCE",
            "superseded_reconciliation_event_uuid": prior.get("event_uuid"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if append_unique(state / "events.jsonl", event, ("event_uuid",)):
            repaired.append(event)
    return repaired


def elevenlabs_link(path, field="Default affiliate link"):
    if not path.is_file() or path.stat().st_mode & 0o077:
        return None
    text = path.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^## ElevenLabs\n.*?(?=^## |\Z)", text)
    if not section:
        return None
    match = re.search(rf"(?m)^- {re.escape(field)}: `?([^`\s]+)`?$", section.group())
    if not match:
        return None
    link = match.group(1)
    parsed = urlparse(link)
    return link if parsed.scheme == "https" and parsed.hostname == "try.elevenlabs.io" else None


def browser_ready(port, attempts=15):
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as response:
                return response.status == 200
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(2)
    return False


def owner_health(state_root, ports=(9324, 9326, 9327), runner=subprocess.run):
    """Read launchd/CDP health and persist one redacted watchdog snapshot."""
    uid = str(os.getuid())
    labels = {}
    for label in AFFILIATE_OWNER_LABELS:
        try:
            result = runner(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        output = result.stdout if result and result.returncode == 0 else ""
        state_match = re.search(r"(?m)^\s*state = ([^\n]+)", output)
        runs_match = re.search(r"(?m)^\s*runs = ([^\n]+)", output)
        exit_match = re.search(r"(?m)^\s*last exit code = ([^\n]+)", output)
        labels[label] = {
            "state": state_match.group(1).strip() if state_match else "NOT_LOADED",
            "runs": runs_match.group(1).strip() if runs_match else None,
            "last_exit_code": exit_match.group(1).strip() if exit_match else None,
        }
    cdp = {str(port): {"state": "READY" if browser_ready(port) else "UNAVAILABLE"}
           for port in ports}
    label_ok = all(row["state"] in {"running", "not running"} for row in labels.values())
    cdp_ok = all(row["state"] == "READY" for row in cdp.values())
    receipt = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_OWNER_HEALTH",
        "state": "HEALTHY" if label_ok and cdp_ok else "DEGRADED",
        "labels": labels,
        "cdp": cdp,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(state_root / "owner-health.json", receipt)
    return receipt


def provider_poll(state, cdp_port, attempts=15, provider="elevenlabs"):
    args = SimpleNamespace(
        provider=provider,
        cdp_host="127.0.0.1",
        cdp_port=cdp_port,
        state=state,
        receipt=state / "providers" / f"{provider}.json",
    )
    for attempt in range(attempts):
        try:
            return poll(args, observe(args))
        except (ProviderError, OSError, ValueError, KeyError, json.JSONDecodeError):
            if attempt + 1 < attempts:
                time.sleep(2)
    return {
        "state": "PROVIDER_OBSERVATION_FAILED",
        "changed": False,
        "transition_id": None,
    }


def recover_provider(state, cdp_port, private_markdown, provider="elevenlabs"):
    common = dict(
        provider=provider,
        cdp_host="127.0.0.1",
        cdp_port=cdp_port,
        state=state,
        private_markdown=private_markdown,
    )
    resume_args = SimpleNamespace(
        **common, receipt=state / "providers" / f"{provider}-resume.json",
    )
    poll_args = SimpleNamespace(
        **common, receipt=state / "providers" / f"{provider}.json",
    )
    recovered = resume(resume_args)
    return poll(poll_args, recovered)


def generic_publication_terminal_state(completed, invalid_metadata):
    if invalid_metadata:
        return "CAMPAIGN_METADATA_INVALID"
    return "ALREADY_LIVE" if completed else "NO_DUE_PUBLICATION"


def advance_generic_publication(
    state, landing_root, x_cdp_port, private_markdown, provider_cdp_port=9324,
    owned_publisher=None, x_publisher=None, link_acquirer=None,
):
    """Advance one policy-PASS generic campaign through existing effect fences."""
    from owned_publish import publish as default_owned_publisher
    from x_post_cli import publish as default_x_publisher

    owned_publisher = owned_publisher or default_owned_publisher
    x_publisher = x_publisher or default_x_publisher
    link_acquirer = link_acquirer or elevenlabs_link_action
    completed = False
    invalid_metadata = False

    def generic_link_receipt_pending(placement):
        return not any(
            row.get("publication_link_placement") == placement
            and row.get("publication_link_state") == "VERIFIED"
            for row in json_rows(state / "events.jsonl")
        )

    def publication_priority(policy_path):
        """Resume an in-flight owned publication before opening another one.

        A public deploy can be delivered before its CDN readback becomes
        observable.  If a newly sorted campaign acquires a link first, its
        placement-link gate can return early forever and starve the delivered
        publication's reconciliation.  Existing effect receipts make this
        ordering replay-safe; no new external effect is introduced here.
        """
        plan_id = policy_path.stem
        try:
            progress = json.loads((
                state / "campaign-publications" / f"{plan_id}.json"
            ).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            progress = {}
        try:
            handoff = json.loads((
                state / "campaign-handoffs" / f"{plan_id}.json"
            ).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            handoff = {}
        slug = progress.get("slug") or handoff.get("slug")
        try:
            owned = json.loads((
                state / "owned-publications" / f"{slug}.json"
            ).read_text(encoding="utf-8")) if slug else {}
        except (OSError, ValueError, json.JSONDecodeError):
            owned = {}
        in_flight = (
            progress.get("state") in {"MATERIALIZED", "OWNED_NOT_LIVE", "OWNED_LIVE"}
            or owned.get("state") in {"INTENT", "DELIVERED", "LIVE"}
        )
        return (0 if in_flight else 1, policy_path.name)

    for policy_path in sorted(
        (state / "campaign-policy").glob("*.json"), key=publication_priority,
    ):
        plan_id = policy_path.stem
        # A campaign whose placement is already live is finished, so its policy
        # and handoff receipts no longer gate anything. Validating them first
        # lets a stale pair left behind by a later recomposition return and block
        # every campaign sorted after it, which is how a published campaign
        # blocked campaign seven twice on 2026-08-17.
        try:
            placement_receipt = json.loads((
                state / "x-posts" / f"{plan_id}-1.json"
            ).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            placement_receipt = {}
        if placement_receipt.get("state") == "LIVE":
            completed = True
            continue
        handoff_path = state / "campaign-handoffs" / f"{plan_id}.json"
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            handoff_bytes = handoff_path.read_bytes()
            handoff = json.loads(handoff_bytes)
            core = dict(handoff)
            fingerprint = core.pop("handoff_fingerprint")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return {"state": "POLICY_RECEIPT_INVALID", "public_url": None}
        if policy.get("decision") != "PASS":
            continue
        valid = all((
            policy.get("receipt_type") == "GENERIC_CAMPAIGN_POLICY",
            policy.get("state") == "PASS",
            policy.get("plan_id") == handoff.get("plan_id") == plan_id,
            policy.get("locale") == handoff.get("locale") == "en",
            policy.get("handoff_sha256") == hashlib.sha256(handoff_bytes).hexdigest(),
            policy.get("handoff_fingerprint") == fingerprint,
            policy.get("source_set_sha256") == handoff.get("source_set_sha256"),
            isinstance(policy.get("checks"), dict) and policy["checks"]
            and all(policy["checks"].values()),
            (policy.get("semantic_audit") or {}).get("decision") == "PASS",
            handoff.get("receipt_type") == "CAMPAIGN_HANDOFF",
            handoff.get("state") == "READY_FOR_POLICY",
            fingerprint == hashlib.sha256(json.dumps(
                core, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
        ))
        if not valid:
            return {"state": "POLICY_RECEIPT_INVALID", "public_url": None}

        slug = handoff.get("slug", "")
        placement = f"{plan_id}-1"
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,100}", slug)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", placement)
        ):
            invalid_metadata = True
            continue
        progress_path = state / "campaign-publications" / f"{plan_id}.json"
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            progress = {}
        if not focused_publication_allowed(state, placement, progress, handoff):
            continue
        owned_receipt_path = state / "owned-publications" / f"{slug}.json"
        x_receipt_path = state / "x-posts" / f"{placement}.json"
        try:
            existing_owned = json.loads(owned_receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_owned = {}
        try:
            existing_x = json.loads(x_receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_x = {}
        rebound_from_handoff_fingerprint = None
        if progress:
            # A published placement is terminal. When a source refresh later
            # recomposes a live campaign the handoff legitimately changes, and
            # republishing would mean a second X post for the same placement, so
            # the new handoff is simply not a publication task. Checking the
            # conflict first instead would let one already-live campaign block
            # every campaign sorted after it, which is what stalled campaign
            # seven behind a recomposed audio-to-text on 2026-08-17.
            if progress.get("state") == "X_LIVE" and progress.get("provider_link_key"):
                completed = True
                continue
            # Still a real hazard while the campaign is in flight: content that
            # changed between materialization and publication. The sole safe
            # exception is an unpublished MATERIALIZED checkpoint with neither
            # effect receipt: it can be rebound to the current sealed handoff.
            if progress.get("handoff_fingerprint") != fingerprint:
                if (
                    progress.get("state") == "MATERIALIZED"
                    and not existing_owned
                    and not existing_x
                ):
                    rebound_from_handoff_fingerprint = progress.get("handoff_fingerprint")
                    progress = {}
                else:
                    return {"state": "PUBLICATION_CONFLICT", "public_url": None}
        if not progress and existing_owned.get("state") == existing_x.get("state") == "LIVE":
            completed = True
            continue
        if not progress and (existing_owned or existing_x):
            return {"state": "PUBLICATION_CONFLICT", "public_url": None}

        destination = next((
            row.get("locator") for row in handoff.get("cited_sources", [])
            if str(row.get("locator", "")).startswith("https://elevenlabs.io/")
        ), None)
        dedicated = link_acquirer(
            state, provider_cdp_port, private_markdown, placement, create=True,
            title=handoff.get("title"), description=handoff.get("buyer_intent"),
            destination=destination,
        )
        link_metadata = {
            "publication_link_state": dedicated.get("state"),
            "publication_link_placement": placement,
            "publication_link_key": dedicated.get("provider_link_key"),
            "publication_link_changed": not dedicated.get("deduplicated", False),
            "publication_link_deduplicated": dedicated.get("deduplicated"),
            "publication_link_failure_type": dedicated.get("failure_type"),
            "publication_link_receipt_pending": (
                dedicated.get("state") == "VERIFIED"
                and generic_link_receipt_pending(placement)
            ),
        }
        if dedicated.get("state") != "VERIFIED":
            return {"state": "WAITING_FOR_PLACEMENT_LINK", "public_url": None, **link_metadata}
        if not dedicated.get("deduplicated", False):
            return {"state": "WAITING_FOR_PLACEMENT_LINK", "public_url": None, **link_metadata}
        link = elevenlabs_link(private_markdown, dedicated.get("private_link_field", ""))
        markdown = handoff.get("owned_article_markdown", "")
        x_copy = handoff.get("x_copy", "")
        disclosure = handoff.get("disclosure", "")
        if (
            not link
            or markdown.count("{{AFFILIATE_LINK}}") != 1
            or markdown.find(disclosure) < 0
            or markdown.find(disclosure) >= markdown.find("{{AFFILIATE_LINK}}")
            or x_copy.count("{{OWNED_ARTICLE_URL}}") != 1
        ):
            return {"state": "CAMPAIGN_CONTENT_INVALID", "public_url": None}
        published_markdown = markdown.replace("{{AFFILIATE_LINK}}", link)
        content_sha256 = hashlib.sha256(published_markdown.encode()).hexdigest()
        created_at = progress.get("created_at") or datetime.now(timezone.utc).isoformat()
        progress = {
            "schema_version": 1,
            "receipt_type": "GENERIC_CAMPAIGN_PUBLICATION",
            "plan_id": plan_id,
            "slug": slug,
            "placement_id": placement,
            "handoff_fingerprint": fingerprint,
            "content_sha256": content_sha256,
            "state": progress.get("state", "MATERIALIZED"),
            "created_at": created_at,
            "provider_link_key": dedicated.get("provider_link_key"),
            "tracking_custom_link_id": dedicated.get("tracking_custom_link_id"),
        }
        if rebound_from_handoff_fingerprint:
            progress["rebound_from_handoff_fingerprint"] = rebound_from_handoff_fingerprint
        if handoff.get("opportunity_decision"):
            progress["opportunity_decision"] = handoff["opportunity_decision"]
        if handoff.get("experiment"):
            progress["experiment"] = handoff["experiment"]
        atomic_json(progress_path, progress)
        atomic_json(state / "content" / f"{slug}.json", {
            "slug": slug,
            "title": handoff["title"],
            "state": "READY_FOR_PUBLICATION",
            "markdown": published_markdown,
            "content_sha256": content_sha256,
            "disclosure": "affiliate_link",
            "source_hashes": [row["raw_sha256"] for row in handoff["cited_sources"]],
            "readback_markers": [disclosure],
            "readback_links": [link],
            "project": "AFFILIATE DECISION GUIDE",
            "built_at": created_at,
            "opportunity_decision": handoff.get("opportunity_decision"),
            "experiment": handoff.get("experiment"),
        })
        atomic_json(state / "policy" / f"{slug}.json", {
            "decision": "PASS",
            "content_sha256": content_sha256,
            "generic_policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            "opportunity_decision": handoff.get("opportunity_decision"),
            "experiment": handoff.get("experiment"),
        })

        owned = owned_publisher(SimpleNamespace(
            state=state, landing_root=landing_root, slug=slug,
            base_url="https://aniccaai.com", remote="origin", branch="main",
        ))
        if owned.get("state") != "LIVE":
            progress.update(state="OWNED_NOT_LIVE", public_url=owned.get("public_url"))
            atomic_json(progress_path, progress)
            return {"state": "OWNED_NOT_LIVE", "public_url": owned.get("public_url"), **link_metadata}
        progress.update(state="OWNED_LIVE", owned_url=owned["public_url"])
        atomic_json(progress_path, progress)
        # Handoffs sealed before the publisher disclosure contract was aligned
        # used this equivalent prefix. Normalize it without changing the
        # source-backed article or bypassing the policy receipt.
        if x_copy.startswith("Affiliate disclosure:"):
            x_copy = x_copy.replace(
                "Affiliate disclosure:", "Affiliate link disclosure:", 1,
            )
        x_content = x_copy.replace("{{OWNED_ARTICLE_URL}}", owned["public_url"])
        x_content_path = state / "x-content" / f"{placement}.txt"
        x_content_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        x_content_path.write_text(x_content + "\n", encoding="utf-8")
        posted = x_publisher(SimpleNamespace(
            state=state, content=x_content_path, placement=placement,
            cdp_host="127.0.0.1", cdp_port=x_cdp_port,
        ))
        if posted.get("state") != "LIVE":
            return {"state": "X_NOT_LIVE", "public_url": posted.get("public_url"), **link_metadata}
        progress.update(state="X_LIVE", x_url=posted["public_url"])
        atomic_json(progress_path, progress)
        return {"state": "X_LIVE", "public_url": posted["public_url"], **link_metadata}
    return {
        "state": generic_publication_terminal_state(completed, invalid_metadata),
        "public_url": None,
    }


LEGACY_DEDICATED_PLACEMENTS = (
    {
        "slug": "elevenlabs-plans-for-solo-creators",
        "placement": "elevenlabs-en-1",
        "title": "ElevenLabs plans for solo creators",
        "description": "Decision guide for solo creators comparing ElevenLabs plans.",
        "builder": "plans",
    },
    {
        "slug": "elevenagents-for-customer-support",
        "placement": "elevenagents-en-1",
        "title": "ElevenAgents customer support evaluation",
        "description": "Decision guide for teams evaluating ElevenAgents for customer support.",
        "builder": "agents",
    },
)


def advance_legacy_dedicated_publication(
    state, landing_root, x_cdp_port, private_markdown, provider_cdp_port=9324,
    link_acquirer=None, owned_publisher=None, x_publisher=None,
):
    """Migrate one already-live legacy placement to its own provider link."""
    from content import (
        build, build_agents, build_x, build_x_agents, policy, policy_agents,
    )
    from owned_publish import publish as default_owned_publisher
    from x_post_cli import publish as default_x_publisher

    link_acquirer = link_acquirer or elevenlabs_link_action
    owned_publisher = owned_publisher or default_owned_publisher
    x_publisher = x_publisher or default_x_publisher
    root = Path(__file__).resolve().parents[1]
    completed = False
    for config in LEGACY_DEDICATED_PLACEMENTS:
        slug = config["slug"]
        placement = config["placement"]
        try:
            existing_x = json.loads(
                (state / "x-posts" / f"{placement}.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if existing_x.get("state") != "LIVE":
            continue
        try:
            dedicated = json.loads(
                (state / "program-links" / f"{placement}.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            dedicated = {}
        field = dedicated.get("private_link_field", "")
        link = elevenlabs_link(private_markdown, field) if field else None
        try:
            artifact = json.loads(
                (state / "content" / f"{slug}.json").read_text(encoding="utf-8")
            )
            owned = json.loads(
                (state / "owned-publications" / f"{slug}.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            artifact, owned = {}, {}
        if all((
            dedicated.get("state") == "VERIFIED",
            link,
            artifact.get("readback_links") == [link],
            owned.get("state") == "LIVE",
            owned.get("content_sha256") == artifact.get("content_sha256"),
        )):
            completed = True
            continue
        if dedicated.get("state") != "VERIFIED":
            dedicated = link_acquirer(
                state, provider_cdp_port, private_markdown, placement, create=True,
                title=config["title"], description=config["description"],
            )
            if dedicated.get("state") != "VERIFIED" or not dedicated.get("deduplicated", False):
                return {"state": "WAITING_FOR_PLACEMENT_LINK", "public_url": None}
            field = dedicated.get("private_link_field", "")
        if config["builder"] == "plans":
            build(root, state, private_markdown, field)
            policy(state, private_markdown, field)
            x_builder = build_x
        else:
            build_agents(root, state, private_markdown, field)
            policy_agents(state, private_markdown, field)
            x_builder = build_x_agents
        published = owned_publisher(SimpleNamespace(
            state=state, landing_root=landing_root, slug=slug,
            base_url="https://aniccaai.com", remote="origin", branch="main",
        ))
        if published.get("state") != "LIVE":
            return {"state": "OWNED_NOT_LIVE", "public_url": published.get("public_url")}
        x_builder(state)
        posted = x_publisher(SimpleNamespace(
            state=state, content=state / "x-content" / f"{placement}.txt",
            placement=placement, cdp_host="127.0.0.1", cdp_port=x_cdp_port,
        ))
        if posted.get("state") != "LIVE":
            return {"state": "X_NOT_LIVE", "public_url": posted.get("public_url")}
        return {"state": "X_LIVE", "public_url": posted.get("public_url")}
    return {
        "state": "ALREADY_LIVE" if completed else "NO_DUE_PUBLICATION",
        "public_url": None,
    }


def advance_known_publication(
    state, landing_root, x_cdp_port, private_markdown=None, provider_cdp_port=9324,
):
    landing_root = Path(landing_root).expanduser()
    generic = advance_generic_publication(
        state, landing_root, x_cdp_port, private_markdown, provider_cdp_port,
    )
    generic_non_blocking = {
        "NO_DUE_PUBLICATION", "ALREADY_LIVE", "PUBLICATION_CONFLICT",
        "POLICY_RECEIPT_INVALID", "CAMPAIGN_METADATA_INVALID",
        "CAMPAIGN_CONTENT_INVALID",
    }
    if generic["state"] not in generic_non_blocking:
        return generic
    if (state / "focused-cohort" / "latest.json").is_file():
        return {
            "state": "FOCUSED_COHORT_HELD", "public_url": None,
            "generic_state": generic["state"],
        }
    legacy = advance_legacy_dedicated_publication(
        state, landing_root, x_cdp_port, private_markdown, provider_cdp_port,
    )
    if legacy["state"] not in {"NO_DUE_PUBLICATION", "ALREADY_LIVE"}:
        legacy["generic_state"] = generic["state"]
        return legacy
    slug = "elevenagents-for-customer-support"
    placement = "elevenagents-en-1"
    x_receipt_path = state / "x-posts" / f"{placement}.json"
    try:
        x_receipt = json.loads(x_receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        x_receipt = {}
    if x_receipt.get("state") == "LIVE":
        result = advance_tts_api_publication(
            state, landing_root, x_cdp_port, private_markdown,
            x_receipt.get("public_url"),
        )
        result["generic_state"] = generic["state"]
        result["legacy_state"] = legacy["state"]
        return result

    artifact_path = state / "content" / f"{slug}.json"
    policy_path = state / "policy" / f"{slug}.json"
    if not artifact_path.is_file() or not policy_path.is_file():
        return {"state": "NO_DUE_PUBLICATION", "public_url": None}
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except ValueError:
        return {"state": "POLICY_RECEIPT_INVALID", "public_url": None}
    if policy.get("decision") != "PASS":
        return {"state": "POLICY_NOT_PASSED", "public_url": None}

    from content import build_x_agents
    from owned_publish import publish as publish_owned
    from x_post_cli import publish as publish_x

    owned = publish_owned(SimpleNamespace(
        state=state,
        landing_root=landing_root,
        slug=slug,
        base_url="https://aniccaai.com",
        remote="origin",
        branch="main",
    ))
    if owned.get("state") != "LIVE":
        return {"state": "OWNED_NOT_LIVE", "public_url": owned.get("public_url")}
    build_x_agents(state)
    posted = publish_x(SimpleNamespace(
        state=state,
        content=state / "x-content" / f"{placement}.txt",
        placement=placement,
        cdp_host="127.0.0.1",
        cdp_port=x_cdp_port,
    ))
    return {"state": "X_LIVE", "public_url": posted.get("public_url")}


def advance_tts_api_publication(state, landing_root, x_cdp_port, private_markdown, fallback_url):
    slug = "elevenlabs-text-to-speech-api-for-developers"
    placement = "elevenlabs-tts-api-en-1"
    receipt_path = state / "x-posts" / f"{placement}.json"
    try:
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    try:
        dedicated_link = json.loads(
            (state / "program-links" / f"{slug}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        dedicated_link = {}
    if existing.get("state") == "LIVE" and dedicated_link.get("state") != "VERIFIED":
        return {"state": "ALREADY_LIVE", "public_url": existing.get("public_url")}
    if not (state / "sources" / "elevenlabs-api-pricing" / "latest.json").is_file():
        return {"state": "ALREADY_LIVE", "public_url": fallback_url}
    if private_markdown is None:
        return {"state": "TTS_API_CREDENTIAL_BOUNDARY_MISSING", "public_url": fallback_url}

    from content import build_tts_api, build_x_tts_api, policy_tts_api
    from owned_publish import publish as publish_owned
    from x_post_cli import content_fingerprint, publish as publish_x

    root = Path(__file__).resolve().parents[1]
    build_tts_api(root, state, private_markdown)
    policy_tts_api(state, private_markdown)
    owned = publish_owned(SimpleNamespace(
        state=state, landing_root=landing_root, slug=slug,
        base_url="https://aniccaai.com", remote="origin", branch="main",
    ))
    if owned.get("state") != "LIVE":
        return {"state": "OWNED_NOT_LIVE", "public_url": owned.get("public_url")}
    build_x_tts_api(state)
    x_content_path = state / "x-content" / f"{placement}.txt"
    # The relink republish is complete once the live receipt already carries the
    # rebuilt content. Re-driving X for a settled effect cannot publish anything
    # new, and a transient timeline scrape then fails the whole wake.
    if existing.get("state") == "LIVE" and existing.get("content_sha256") == content_fingerprint(
        x_content_path.read_text(encoding="utf-8")
    ):
        return {"state": "ALREADY_LIVE", "public_url": existing.get("public_url")}
    posted = publish_x(SimpleNamespace(
        state=state, content=x_content_path,
        placement=placement, cdp_host="127.0.0.1", cdp_port=x_cdp_port,
    ))
    return {"state": "X_LIVE", "public_url": posted.get("public_url")}


def sweep_publication_liveness(state, x_cdp_port, now=None, publisher=None):
    """Re-verify every live X receipt once per JST day.

    The completed publication paths deliberately stop touching X, so nothing
    else would ever notice a post that was deleted or suspended after the fact
    and the ledger would keep reporting a dead URL as live. Verification still
    has to happen; it just must not happen on every ten-minute wake, which is
    what made a transient timeline scrape fail the whole loop. Publishing with a
    matching live receipt is verify-only: the compose branch is unreachable once
    the receipt carries a public URL, so this can observe but never post.
    """
    from x_post_cli import publish as default_publisher

    publisher = publisher or default_publisher
    receipt_path = state / "publication-liveness.json"
    today = (now or datetime.now(ZoneInfo("Asia/Tokyo"))).date().isoformat()
    try:
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if previous.get("day") == today:
        return {"state": "COOLDOWN", "checked": 0, "unverified": []}
    checked = 0
    unverified = []
    for path in sorted((state / "x-posts").glob("*.json")):
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        content = state / "x-content" / f"{path.stem}.txt"
        if (
            existing.get("state") != "LIVE"
            or not existing.get("public_url")
            or not content.is_file()
        ):
            continue
        checked += 1
        try:
            publisher(SimpleNamespace(
                state=state, content=content, placement=path.stem,
                cdp_host="127.0.0.1", cdp_port=x_cdp_port,
            ))
        except Exception as error:
            unverified.append({
                "placement_id": path.stem,
                "failure_type": type(error).__name__,
                "failure_detail": str(error)[:300],
            })
    # The day is recorded even when a placement failed, so one bad scrape cannot
    # drag the sweep back onto the per-wake cadence this exists to avoid. The
    # failure stays visible in the wake event and in this receipt.
    atomic_json(receipt_path, {
        "schema_version": 1,
        "receipt_type": "PUBLICATION_LIVENESS_SWEEP",
        "day": today,
        "checked": checked,
        "unverified": unverified,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "state": "UNVERIFIED_PLACEMENTS" if unverified else "ALL_LIVE",
        "checked": checked,
        "unverified": unverified,
    }


def revenue_cycle_due(state, now=None, cooldown_seconds=3600):
    current_time = int(time.time()) if now is None else int(now)
    receipt = state / "revenue-cycle.json"
    completed_at = None
    if receipt.is_file():
        try:
            completed_at = int(json.loads(receipt.read_text(encoding="utf-8"))["completed_at"])
        except (OSError, ValueError, KeyError, TypeError):
            completed_at = None
    if completed_at is not None and current_time - completed_at < cooldown_seconds:
        repost_root = Path(os.environ.get(
            "AFFILIATE_REPOST_STATE_DIR", Path.home() / "loops" / "x-repost"
        )).expanduser()
        newest_affiliate_post = None
        for row in json_rows(repost_root / "posted.jsonl"):
            if row.get("kind") not in {
                "affiliate_distribution", "affiliate_distribution_quote",
            }:
                continue
            try:
                posted_at = int(datetime.fromisoformat(
                    str(row.get("posted_at")).replace("Z", "+00:00")
                ).timestamp())
            except ValueError:
                continue
            newest_affiliate_post = max(newest_affiliate_post or posted_at, posted_at)
        if newest_affiliate_post is None or newest_affiliate_post <= completed_at:
            return False
        if newest_affiliate_post <= current_time:
            return True
        return False
    try:
        failure = json.loads(
            (state / "revenue-cycle-failure.json").read_text(encoding="utf-8")
        )
        failure_at = int(failure.get("observed_at", 0))
        retry_after = int(failure.get("retry_after", 0))
    except (OSError, ValueError, TypeError):
        failure_at, retry_after = 0, 0
    # A successful cycle supersedes an older failure receipt. A still-newer
    # failure receipt owns the retry window and prevents repeated provider
    # capture attempts on every ten-minute wake.
    if failure_at > (completed_at or 0) and retry_after > current_time:
        return False
    return completed_at is None or current_time - completed_at >= cooldown_seconds


def revenue_failure(state, stage, failure_type, return_code, error_text):
    now = int(time.time())
    failure_class, retry_seconds = _classify_revenue_failure(failure_type)
    retry_state = "RETRYABLE" if retry_seconds else "NOT_RETRYABLE"
    retry_after = now + retry_seconds if retry_seconds else None
    try:
        latest = json.loads(
            (state / "provider-reports" / "partnerstack" / "latest.json").read_text(
                encoding="utf-8"
            )
        )
        source_hash = latest.get("rendered_artifact_sha256")
    except (OSError, ValueError):
        source_hash = None
    failure = {
        "schema_version": 1,
        "receipt_type": "REVENUE_CYCLE_FAILURE",
        "state": "REVENUE_CYCLE_FAILED",
        "stage": stage,
        "failure_type": failure_type,
        "failure_class": failure_class,
        "retry_state": retry_state,
        "return_code": return_code,
        "error_sha256": hashlib.sha256(error_text.encode()).hexdigest(),
        "latest_source_artifact_sha256": source_hash,
        "observed_at": now,
        "retry_after": retry_after,
    }
    atomic_json(state / "revenue-cycle-failure.json", failure)
    return {
        "state": failure["state"],
        "source_rows": None,
        "appended_transitions": None,
        "failure_type": failure_type,
        "failure_class": failure_class,
        "retry_state": retry_state,
        "retry_after": retry_after,
    }


def run_revenue_cycle(state, cdp_port):
    if not revenue_cycle_due(state):
        return {"state": "COOLDOWN", "source_rows": None, "appended_transitions": None}
    script = Path(__file__).with_name("revenue_cli.py")
    common = ["--state", str(state), "--cdp-port", str(cdp_port)]
    result = None
    link_result = {}
    for command in ("observe", "links", "capture", "reconcile"):
        try:
            completed = subprocess.run(
                [sys.executable, str(script), command, *common],
                check=False, capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired as error:
            return revenue_failure(state, command, "TIMEOUT", None, str(error))
        if completed.returncode:
            return revenue_failure(
                state, command, "NONZERO_EXIT", completed.returncode, completed.stderr,
            )
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            return revenue_failure(
                state, command, "INVALID_JSON", completed.returncode, completed.stdout,
            )
        if command == "links":
            link_result = result
    cycle = {
        "state": result["money_state"],
        "source_rows": result["source_rows"],
        "appended_transitions": result["appended_transitions"],
        "link_appended_transitions": link_result.get("appended_transitions", 0),
        "link_latest_transition": link_result.get("latest_transition"),
        "completed_at": int(time.time()),
    }
    atomic_json(state / "revenue-cycle.json", cycle)
    return cycle


def refresh_placement_ledger(state):
    """Rebuild placement economics from durable real receipts on every wake."""
    script = Path(__file__).with_name("revenue_cli.py")
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "ledger", "--state", str(state)],
            check=False, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"state": "LEDGER_FAILED", "failure_type": "TIMEOUT"}
    if completed.returncode:
        return {"state": "LEDGER_FAILED", "failure_type": "NONZERO_EXIT"}
    try:
        ledger = json.loads(completed.stdout)
    except ValueError:
        return {"state": "LEDGER_FAILED", "failure_type": "INVALID_JSON"}
    return {
        "state": "LEDGER_READY",
        "ledger_sha256": ledger["ledger_sha256"],
        "placement_count": len(ledger["placements"]),
    }


def refresh_rolling_net(state):
    """Rebuild the fail-closed rolling-net receipt from the same owner wake."""
    script = Path(__file__).with_name("revenue_cli.py")
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "net", "--state", str(state)],
            check=False, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"state": "ROLLING_NET_FAILED", "failure_type": "TIMEOUT"}
    if completed.returncode:
        return {"state": "ROLLING_NET_FAILED", "failure_type": "NONZERO_EXIT"}
    try:
        receipt = json.loads(completed.stdout)
    except ValueError:
        return {"state": "ROLLING_NET_FAILED", "failure_type": "INVALID_JSON"}
    return {
        "state": "ROLLING_NET_READY",
        "receipt_sha256": receipt.get("receipt_sha256"),
        "money_state": receipt.get("money_state"),
        "net_state": receipt.get("net_state"),
        "threshold_state": receipt.get("threshold_state"),
        "approved_or_paid_net_usd": receipt.get("approved_or_paid_net_usd"),
        "placement_ledger_sha256": receipt.get("placement_ledger_sha256"),
        "status_counts": receipt.get("status_counts"),
        "cost_state": receipt.get("cost_state"),
        "cost_coverage_state": receipt.get("cost_coverage_state"),
        "unknown_reasons": receipt.get("unknown_reasons"),
    }


def resume_systeme_provider(state, cdp_port, private_markdown):
    """Reuse the provider harness on the shared EN browser, then restore its owner URL."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise ProviderError("Playwright is unavailable") from error
    args = SimpleNamespace(
        provider="systeme-io", cdp_host="127.0.0.1", cdp_port=cdp_port,
        state=state, private_markdown=private_markdown,
        receipt=state / "provider-systeme-io.json",
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            pages = [page for context in browser.contexts for page in context.pages]
            if len(pages) != 1:
                raise ProviderError("expected one shared English provider tab")
            page = pages[0]
            page.goto(SYSTEME_LOGIN, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_function(
                """() => location.pathname.includes('/dashboard') ||
                    document.body.innerText.includes('Log in')""",
                timeout=15_000,
            )
        before = observe(args)
        return resume(args) if before["state"] == "SIGN_IN_REQUIRED" else before
    finally:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            pages = [page for context in browser.contexts for page in context.pages]
            if len(pages) != 1:
                raise ProviderError("expected one shared English provider tab")
            page = pages[0]
            page.goto(ELEVENLABS_HOME, wait_until="domcontentloaded", timeout=20_000)


def verify_systeme_email(state, cdp_port, private_markdown):
    receipt_path = state / "provider-systeme-email-verification.json"
    if receipt_path.is_file():
        prior = json.loads(receipt_path.read_text(encoding="utf-8"))
        if prior.get("state") == "EMAIL_VERIFIED":
            return {**prior, "deduplicated": True}
        if (
            prior.get("state") == "CAPTCHA_CHALLENGE"
            and int(prior.get("retry_after", 0)) > int(time.time())
        ):
            return {**prior, "deduplicated": True}
    text = private_markdown.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^## Systeme\.io\n.*?(?=^## |\Z)", text)
    match = re.search(
        r"(?m)^- Email verification link:[ \t]*(https://\S+)[ \t]*$",
        section.group() if section else "",
    )
    if not match:
        return {"state": "VERIFICATION_LINK_UNAVAILABLE", "deduplicated": False}
    link = match.group(1)
    pending = unresolved_effect(state, "PROVIDER_EMAIL_VERIFY", "systeme-io")
    job = (
        resume_effect(state, "PROVIDER_EMAIL_VERIFY", "systeme-io")
        if pending else start_effect(
            state, "PROVIDER_EMAIL_VERIFY", "systeme-io",
            {"operation": "verify_email", "provider": "systeme-io"},
            {"state": "CONFIRMATION_EMAIL_RECEIVED"}, 300,
        )
    )
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        pages = [page for context in browser.contexts for page in context.pages]
        if len(pages) != 1:
            raise ProviderError("expected one shared English provider tab")
        page = pages[0]
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_function(
                """() => !location.pathname.includes('/register/confirm/') ||
                    document.body.innerText.includes('Create a password to confirm your account')""",
                timeout=15_000,
            )
            confirmation_response = None
            form_heading = page.get_by_text(
                "Create a password to confirm your account", exact=True,
            )
            if form_heading.count():
                profile = json.loads(Path(
                    "~/.config/anicca/job-search/profile.json"
                ).expanduser().read_text(encoding="utf-8"))
                names = profile["candidate"]["name_romaji_parts"]
                _, password = read_login_credentials(private_markdown, "Systeme.io")
                page.locator("input[name='firstName']").fill(names["given"])
                page.locator("input[name='lastName']").fill(names["family"])
                page.locator("input[name='plainPassword']").fill(password)
                page.locator("input[name='confirm_password']").fill(password)
                captcha = page.locator("iframe[src*='/recaptcha/api2/anchor?']")
                try:
                    captcha.wait_for(timeout=15_000)
                    captcha_anchor = page.frame_locator(
                        "iframe[src*='/recaptcha/api2/anchor?']"
                    ).locator("#recaptcha-anchor")
                    captcha_anchor.wait_for(timeout=15_000)
                    captcha_anchor.evaluate("element => element.click()")
                    page.wait_for_function(
                        """() => !!document.querySelector(
                            "textarea[name='g-recaptcha-response']"
                        )?.value""",
                        timeout=15_000,
                    )
                except Exception:
                    result = {
                        "schema_version": 1,
                        "receipt_type": "PROVIDER_EMAIL_VERIFICATION",
                        "provider": "systeme-io",
                        "state": "CAPTCHA_CHALLENGE",
                        "retry_after": int(time.time()) + 21_600,
                        "rendered_text_sha256": hashlib.sha256(
                            page.locator("body").inner_text().encode()
                        ).hexdigest(),
                        "deduplicated": False,
                    }
                    atomic_json(receipt_path, result)
                    return result
                with page.expect_response(
                    lambda response: "/api/security/register/confirm" in response.url,
                    timeout=20_000,
                ) as response_info:
                    page.locator(
                        "button[data-test-id='button-auth-register-confirm-submit']"
                    ).click(timeout=5_000)
                response = response_info.value
                confirmation_response = {
                    "http_status": response.status,
                    "url_sha256": hashlib.sha256(response.url.encode()).hexdigest(),
                    "body_sha256": hashlib.sha256(response.body()).hexdigest(),
                }
                if 200 <= response.status < 300:
                    page.wait_for_function(
                        "() => !location.pathname.includes('/register/confirm/')",
                        timeout=20_000,
                    )
            accepted = (
                page.url.startswith("https://systeme.io/")
                and ("/login" in page.url or "/dashboard" in page.url)
            )
            result = {
                "schema_version": 1,
                "receipt_type": "PROVIDER_EMAIL_VERIFICATION",
                "provider": "systeme-io",
                "state": "EMAIL_VERIFIED" if accepted else "VERIFICATION_AMBIGUOUS",
                "observed_url": page.url,
                "rendered_text_sha256": hashlib.sha256(
                    page.locator("body").inner_text().encode()
                ).hexdigest(),
                "confirmation_response": confirmation_response,
                "deduplicated": False,
            }
            if accepted:
                verify_effect(state, job["job_id"], {
                    key: result[key] for key in (
                        "state", "observed_url", "rendered_text_sha256",
                        "confirmation_response",
                    )
                })
            atomic_json(receipt_path, result)
            return result
        finally:
            page.goto(ELEVENLABS_HOME, wait_until="domcontentloaded", timeout=20_000)


def wake(args):
    """Run one owner wake and always leave a terminal scheduler receipt."""
    started_at = time.time()
    run_id = hashlib.sha256(
        f"{installed_release_sha()}:{started_at:.6f}:{os.getpid()}".encode()
    ).hexdigest()
    try:
        return _wake_once(args, started_at, run_id)
    except BaseException as error:
        append_run_receipt(
            args.state.expanduser(),
            {"status": "FAILED"},
            started_at,
            run_id=run_id,
            terminal_state="FAILED",
            failure_type=type(error).__name__,
            scheduler_run_id=run_id,
        )
        raise


def _wake_once(args, started_at, run_id):
    state = args.state.expanduser()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    guard = runtime_guard(state)
    health = owner_health(
        state,
        ports=(args.cdp_port, getattr(args, "x_cdp_port", 9326),
               getattr(args, "impact_cdp_port", 9327)),
    )
    quarantine = quarantine_snapshot(state)
    action_budget = action_budget_snapshot(state)
    cost_budget = cost_budget_snapshot(state)
    attempt_counts = {}

    def refresh_action_budget():
        nonlocal action_budget
        action_budget = action_budget_snapshot(state)
        return action_budget

    def refresh_cost_budget():
        nonlocal cost_budget
        cost_budget = cost_budget_snapshot(state)
        return cost_budget

    def admit(tool, effect_class, preconditions, operation, wake_event_uuid=None):
        attempt_counts[tool] = attempt_counts.get(tool, 0) + 1
        if effect_class in QUARANTINABLE_EFFECTS and tool in quarantine.get("tools", {}):
            result = {
                "state": "QUARANTINED",
                "quarantine_state": "QUARANTINED",
                "quarantined_tool": tool,
                "changed": False,
                "deduplicated": False,
                "public_url": None,
                "channel": None,
                "program": None,
                "placement": preconditions.get("placement"),
                "transition_id": None,
                "retry_state": "NOT_RETRYABLE",
            }
            append_tool_attempt_receipt(
                state, run_id, tool, effect_class, attempt_counts[tool],
                preconditions, time.time(), result=result,
                failure_class="QUARANTINE", retry_state="NOT_RETRYABLE",
                wake_event_uuid=wake_event_uuid,
            )
            return result
        if effect_class in QUARANTINABLE_EFFECTS:
            current_cost = refresh_cost_budget()
            if current_cost["state"] == "COST_CAP_BLOCKED":
                retry_after = _next_jst_day_epoch()
                result = {
                    "state": "COST_CAP_BLOCKED",
                    "cost_budget_state": current_cost["state"],
                    "cost_budget_known_actual_usd_minor": current_cost[
                        "known_actual_usd_minor"
                    ],
                    "cost_budget_cap_minor": current_cost["cap_minor"],
                    "cost_budget_unknown_rows": current_cost["unknown_rows"],
                    "action_budget_state": action_budget.get("state"),
                    "action_budget_used_attempts": action_budget.get("used_attempts"),
                    "action_budget_daily_cap": action_budget.get("daily_cap"),
                    "changed": False,
                    "deduplicated": False,
                    "public_url": None,
                    "channel": None,
                    "program": None,
                    "placement": preconditions.get("placement"),
                    "transition_id": None,
                    "failure_class": "COST_CAP",
                    "retry_state": "RETRYABLE",
                    "retry_after": retry_after,
                }
                append_tool_attempt_receipt(
                    state, run_id, tool, effect_class, attempt_counts[tool],
                    preconditions, time.time(), result=result,
                    failure_class="COST_CAP", retry_due_at=retry_after,
                    retry_state="RETRYABLE", wake_event_uuid=wake_event_uuid,
                )
                return result
        if effect_class in QUARANTINABLE_EFFECTS and refresh_action_budget()["state"] == "ACTION_CAP_BLOCKED":
            result = {
                "state": action_budget["state"],
                "action_budget_state": action_budget["state"],
                "used_attempts": action_budget["used_attempts"],
                "daily_cap": action_budget["daily_cap"],
                "changed": False,
                "deduplicated": False,
                "public_url": None,
                "channel": None,
                "program": None,
                "placement": preconditions.get("placement"),
                "transition_id": None,
                "retry_state": "RETRYABLE",
                "retry_after": int(time.time()) + 3600,
            }
            append_tool_attempt_receipt(
                state, run_id, tool, effect_class, attempt_counts[tool],
                preconditions, time.time(), result=result,
                failure_class="ACTION_CAP", retry_due_at=result["retry_after"],
                retry_state="RETRYABLE", wake_event_uuid=wake_event_uuid,
            )
            return result
        try:
            result = attempt_tool(
                state, run_id, tool, effect_class, preconditions, operation,
                attempt=attempt_counts[tool],
                wake_event_uuid=wake_event_uuid,
            )
        except BaseException:
            if effect_class in QUARANTINABLE_EFFECTS:
                refresh_action_budget()
                refresh_cost_budget()
            raise
        if effect_class in QUARANTINABLE_EFFECTS:
            refresh_action_budget()
            refresh_cost_budget()
        return result

    lock = (state / ".wake.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        append_run_receipt(
            state,
            {"status": "ALREADY_RUNNING"},
            started_at,
            run_id=run_id,
            terminal_state="ALREADY_RUNNING",
            scheduler_run_id=run_id,
        )
        lock.close()
        print('{"state":"ALREADY_RUNNING"}')
        return 0
    try:
        repost_observation = admit(
            "repost.observe", "READ_ONLY", {"owner": "existing-x-repost"},
            lambda: observe_repost_acquisition(state),
        )
    except Exception as error:
        repost_observation = {
            "state": "OBSERVATION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
            "transition_id": None, "source_file_sha256": None,
            "post_action_count": None, "joined_campaign_count": None,
            "unjoined_post_action_count": None, "invalid_row_count": None,
            "denominator_state": "POST_ACTION_COUNT_ONLY",
            "revenue_credit_state": "NO_REVENUE_CREDIT",
        }
    try:
        x_growth = admit(
            "growth.x-followers", "READ_ONLY", {"owner": "affiliate-x-browser"},
            lambda: observe_x_growth(state, getattr(args, "x_cdp_port", 9326)),
        )
    except Exception as error:
        x_growth = {
            "state": "OBSERVATION_FAILED", "changed": False,
            "failure_type": type(error).__name__, "followers": {
                "count": None, "state": "UNAVAILABLE_EXACT",
            },
        }
    try:
        x_post_metrics = admit(
            "growth.x-post-metrics", "READ_ONLY", {"owner": "affiliate-x-browser"},
            lambda: observe_x_post_metrics(state, getattr(args, "x_cdp_port", 9326)),
        )
    except Exception as error:
        x_post_metrics = {
            "state": "OBSERVATION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
            "impressions": {"count": None, "state": "UNAVAILABLE_EXACT"},
        }
    try:
        x_channel_ledger = admit(
            "growth.x-channel-ledger", "READ_ONLY",
            {"post_metrics_state": x_post_metrics.get("state")},
            lambda: observe_x_channel_ledger(state, getattr(args, "x_cdp_port", 9326)),
        )
    except Exception as error:
        x_channel_ledger = {
            "state": "OBSERVATION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        repost_proposal = admit(
            "repost.propose", "LEDGER_ONLY", {"owner": "existing-x-repost"},
            lambda: create_repost_proposal(state),
        )
    except Exception as error:
        repost_proposal = {
            "state": "PROPOSAL_FAILED", "changed": False,
            "failure_type": type(error).__name__, "proposal_id": None,
        }
    try:
        x_distribution_job = admit(
            "distribution.x-job", "LEDGER_ONLY",
            {"proposal_id": repost_proposal.get("proposal_id")},
            lambda: create_x_distribution_job(state, repost_proposal),
        )
    except Exception as error:
        x_distribution_job = {
            "state": "QUEUE_FAILED", "changed": False,
            "failure_type": type(error).__name__, "job_id": None,
        }
    link = admit(
        "tracking-link.read", "READ_ONLY", {"provider": "elevenlabs"},
        lambda: {"state": "AVAILABLE" if elevenlabs_link(args.private_markdown.expanduser()) else "MISSING"},
    ).get("state") == "AVAILABLE"
    browser = admit(
        "browser.provider-ready", "READ_ONLY", {"cdp_port": args.cdp_port},
        lambda: {"state": "READY" if browser_ready(args.cdp_port) else "UNAVAILABLE"},
    ).get("state") == "READY"
    provider = admit(
        "provider.poll.elevenlabs", "READ_ONLY", {"browser_ready": browser},
        lambda: provider_poll(state, args.cdp_port) if browser else {
            "state": "BROWSER_UNAVAILABLE", "changed": False, "transition_id": None,
        },
    )
    recovery_state = "NOT_NEEDED"
    if provider["state"] == "SIGN_IN_REQUIRED":
        try:
            provider = admit(
                "provider.recover.elevenlabs", "EXTERNAL_WRITE",
                {"provider_state": provider.get("state")},
                lambda: recover_provider(state, args.cdp_port, args.private_markdown.expanduser()),
            )
            recovery_state = "RECOVERED" if provider["state"] == "AUTHENTICATED" else provider["state"]
        except (ProviderError, JobStateError, OSError, ValueError, KeyError, json.JSONDecodeError):
            recovery_state = "RECOVERY_FAILED"
    placement_link = {"state": "NOT_RUN", "placement": TTS_PLACEMENT, "deduplicated": None}
    if provider["state"] == "AUTHENTICATED":
        try:
            placement_link = admit(
                "provider-link.elevenlabs", "PROVIDER_LINK_WRITE",
                {"provider_state": provider.get("state"), "placement": TTS_PLACEMENT},
                lambda: elevenlabs_link_action(
                    state, args.cdp_port, args.private_markdown.expanduser(),
                    TTS_PLACEMENT, create=True,
                ),
            )
        except (JobStateError, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
            placement_link = {
                "state": "PLACEMENT_LINK_FAILED", "placement": TTS_PLACEMENT,
                "deduplicated": None, "failure_type": type(error).__name__,
            }
        except Exception as error:
            # A transient Playwright selector timeout must not strand the
            # already-verified link or kill the whole wake. Reuse only the
            # exact local receipt for this placement; unknown browser errors
            # still fail closed and remain visible to the owner.
            is_playwright_timeout = (
                type(error).__name__ == "TimeoutError"
                and type(error).__module__.startswith("playwright.")
            )
            receipt_path = state / "program-links" / f"{TTS_PLACEMENT}.json"
            try:
                verified_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                verified_receipt = {}
            if is_playwright_timeout and (
                verified_receipt.get("state") == "VERIFIED"
                and verified_receipt.get("placement") == TTS_PLACEMENT
            ):
                placement_link = {
                    **verified_receipt,
                    "state": "VERIFIED",
                    "deduplicated": True,
                    "readback_state": "LOCAL_VERIFIED_RECEIPT_REUSED",
                    "provider_readback_pending": True,
                    "failure_type": type(error).__name__,
                }
            else:
                raise
    placement_link_ready = placement_link.get("state") == "VERIFIED"
    placement_link_changed = placement_link_ready and not placement_link.get("deduplicated", False)
    impact = {
        "state": "BROWSER_UNAVAILABLE", "changed": False, "transition_id": None,
    }
    impact_recovery_state = "NOT_NEEDED"
    impact_port = getattr(args, "impact_cdp_port", 9327)
    impact_browser = admit(
        "browser.impact-ready", "READ_ONLY", {"cdp_port": impact_port},
        lambda: {"state": "READY" if browser_ready(impact_port) else "UNAVAILABLE"},
    ).get("state") == "READY"
    if impact_browser:
        impact = admit(
            "provider.poll.hubspot-impact", "READ_ONLY", {"browser_ready": True},
            lambda: provider_poll(state, impact_port, provider="hubspot-impact"),
        )
        if impact["state"] == "SIGN_IN_REQUIRED":
            try:
                impact = admit(
                    "provider.recover.hubspot-impact", "EXTERNAL_WRITE",
                    {"provider_state": impact.get("state")},
                    lambda: recover_provider(
                        state, impact_port, args.private_markdown.expanduser(),
                        provider="hubspot-impact",
                    ),
                )
                impact_recovery_state = (
                    "RECOVERED" if impact["state"] in {
                        "APPLICATION_PENDING", "APPROVED", "REJECTED",
                    } else impact["state"]
                )
            except (ProviderError, JobStateError, OSError, ValueError, KeyError, json.JSONDecodeError):
                impact_recovery_state = "RECOVERY_FAILED"
    application = {"state": "NOT_RUN", "program": "getresponse"}
    if provider["state"] == "AUTHENTICATED" and placement_link_ready and not placement_link_changed:
        try:
            application = admit(
                "provider.application.getresponse", "EXTERNAL_WRITE",
                {"provider_state": provider.get("state"), "link_ready": placement_link_ready},
                lambda: apply_getresponse(
                    state, args.cdp_port,
                    Path("~/.config/anicca/job-search/profile.json"),
                ),
            )
        except (JobStateError, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
            application = {
                "state": "APPLICATION_FAILED", "program": "getresponse",
                "failure_type": type(error).__name__,
            }
    systeme = {"state": "NOT_RUN", "provider": "systeme-io"}
    systeme_verification = {"state": "NOT_RUN"}
    if provider["state"] == "AUTHENTICATED" and placement_link_ready and not placement_link_changed:
        try:
            systeme_verification = admit(
                "provider.verify.systeme-io", "READ_ONLY",
                {"provider_state": provider.get("state")},
                lambda: verify_systeme_email(
                    state, args.cdp_port, args.private_markdown.expanduser(),
                ),
            )
            if systeme_verification["state"] == "EMAIL_VERIFIED":
                systeme = admit(
                    "provider.resume.systeme-io", "EXTERNAL_WRITE",
                    {"verification_state": systeme_verification.get("state")},
                    lambda: resume_systeme_provider(
                        state, args.cdp_port, args.private_markdown.expanduser(),
                    ),
                )
            else:
                systeme = {
                    "state": systeme_verification["state"],
                    "provider": "systeme-io",
                }
        except Exception as error:
            systeme = {
                "state": "PROVIDER_FAILED", "provider": "systeme-io",
                "failure_type": type(error).__name__,
            }
    try:
        landing_root = getattr(
            args, "landing_root",
            Path(os.environ.get("AFFILIATE_LANDING_ROOT", Path(__file__).resolve().parents[3])),
        )
        publication = (
            admit(
                "publication.advance", "PUBLICATION_WRITE",
                {"link_ready": placement_link_ready, "link_changed": placement_link_changed},
                lambda: advance_known_publication(
                    state, landing_root.expanduser(), getattr(args, "x_cdp_port", 9326),
                    args.private_markdown.expanduser(), args.cdp_port,
                ),
            )
            if placement_link_ready and not placement_link_changed
            else {"state": "WAITING_FOR_PLACEMENT_LINK", "public_url": None}
        )
    except Exception as error:
        publication = {
            "state": "PUBLICATION_FAILED", "public_url": None,
            "failure_type": type(error).__name__,
            "failure_detail": str(error)[:600],
        }
    try:
        liveness = admit(
            "publication.liveness", "READ_ONLY", {"channel": "x"},
            lambda: sweep_publication_liveness(
                state, getattr(args, "x_cdp_port", 9326),
            ),
        )
    except Exception as error:
        liveness = {
            "state": "SWEEP_FAILED", "checked": 0, "unverified": [],
            "failure_type": type(error).__name__,
        }
    try:
        if placement_link_changed or not placement_link_ready:
            distribution = {
                "state": "WAITING_FOR_PLACEMENT_LINK", "public_url": None,
                "changed": False, "channel": None,
            }
        else:
            distribution = admit(
                "distribution.devto", "PUBLICATION_WRITE", {"link_ready": placement_link_ready},
                lambda: advance_devto_distribution(state),
            )
            if not distribution.get("changed"):
                distribution = admit(
                    "distribution.substack", "PUBLICATION_WRITE",
                    {"devto_changed": bool(distribution.get("changed"))},
                    lambda: advance_substack_distribution(state),
                )
    except Exception as error:
        distribution = {
            "state": "DISTRIBUTION_FAILED", "public_url": None,
            "changed": False, "failure_type": type(error).__name__,
            "failure_detail": str(error)[:600],
        }
    try:
        devto_metrics = admit(
            "acquisition.observe-devto", "READ_ONLY", {"channel": "devto"},
            lambda: observe_devto_acquisition(state),
        )
    except Exception as error:
        devto_metrics = {
            "state": "OBSERVATION_FAILED", "article_count": None,
            "total_page_views": None, "delta_page_views": None,
            "failure_type": type(error).__name__,
        }
    revenue = admit(
        "revenue.capture", "READ_ONLY", {"provider_state": provider.get("state")},
        lambda: run_revenue_cycle(state, args.cdp_port) if provider["state"] == "AUTHENTICATED" else {
            "state": "PROVIDER_NOT_AUTHENTICATED", "source_rows": None, "appended_transitions": None,
        },
    )
    placement_ledger = admit(
        "ledger.placement-refresh", "LEDGER_ONLY", {"revenue_state": revenue.get("state")},
        lambda: refresh_placement_ledger(state),
    )
    money_funnel = admit(
        "ledger.money-funnel", "LEDGER_ONLY",
        {"placement_ledger_state": placement_ledger.get("state")},
        lambda: build_money_funnel_row(state),
    )
    funnel_snapshot = admit(
        "ledger.funnel-snapshot", "LEDGER_ONLY",
        {"placement_ledger_state": placement_ledger.get("state")},
        lambda: refresh_funnel_snapshot(state),
    )
    owned_visits = admit(
        "acquisition.observe-owned-visits", "READ_ONLY",
        {"funnel_snapshot_sha256": funnel_snapshot.get("snapshot_sha256")},
        lambda: observe_owned_visits(state),
    )
    focused_funnel = (funnel_snapshot.get("placements") or [{}])[0]
    try:
        cta_instrumentation = admit(
            "publication.cta-instrumentation", "PUBLICATION_WRITE",
            {"placement_id": focused_funnel.get("placement_id")},
            lambda: advance_cta_instrumentation(
                state, landing_root.expanduser(), focused_funnel["placement_id"],
                focused_funnel["owned_url"],
            ),
        )
    except Exception as error:
        cta_instrumentation = {
            "state": "CTA_INSTRUMENTATION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        owned_entries = admit(
            "acquisition.observe-x-owned-entries", "READ_ONLY",
            {"instrumentation_state": cta_instrumentation.get("state")},
            lambda: observe_entries(state, funnel_snapshot.get("placements") or []),
        )
    except Exception as error:
        owned_entries = {
            "state": "ENTRY_OBSERVATION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        cta_clicks = admit(
            "acquisition.observe-cta-clicks", "READ_ONLY",
            {"instrumentation_state": cta_instrumentation.get("state")},
            lambda: observe_clicks(state, funnel_snapshot.get("placements") or []),
        )
    except Exception as error:
        cta_clicks = {
            "state": "CTA_OBSERVATION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        interval_funnel = admit(
            "ledger.interval-funnel-join", "LEDGER_ONLY",
            {"cta_click_state": cta_clicks.get("state")},
            lambda: join_provider_interval(state),
        )
    except Exception as error:
        interval_funnel = {
            "state": "INTERVAL_FUNNEL_JOIN_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        focused_cohort = admit(
            "ledger.focus-cohort", "LEDGER_ONLY",
            {"interval_funnel_state": interval_funnel.get("state")},
            lambda: focus_cohort(state) if interval_funnel.get("state") == "OBSERVED" else {
                "state": "WAITING_FOR_INTERVAL_FUNNEL", "changed": False,
            },
        )
    except Exception as error:
        focused_cohort = {
            "state": "FOCUS_SELECTION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    rolling_net = admit(
        "ledger.rolling-net", "LEDGER_ONLY", {"placement_ledger_state": placement_ledger.get("state")},
        lambda: refresh_rolling_net(state),
    )
    try:
        acquisition_decision = admit(
            "acquisition.decision", "READ_ONLY",
            {"rolling_net_state": rolling_net.get("state")},
            lambda: advance_acquisition_decision(
                Path(__file__).resolve().parent.parent, state, run_id
            ),
        )
    except Exception as error:
        acquisition_decision = {
            "state": "DECISION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        funnel_decision = admit(
            "acquisition.funnel-decision", "READ_ONLY",
            {"money_funnel_state": money_funnel.get("state")},
            lambda: advance_funnel_decision(
                Path(__file__).resolve().parent.parent, state, run_id
            ),
        )
    except Exception as error:
        funnel_decision = {
            "state": "DECISION_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        funnel_experiment = admit(
            "acquisition.funnel-experiment", "LEDGER_ONLY",
            {"funnel_decision_state": funnel_decision.get("state")},
            lambda: activate_funnel_experiment(state, funnel_decision),
        )
    except Exception as error:
        funnel_experiment = {
            "state": "EXPERIMENT_LOCK_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        exposure_gate = admit(
            "acquisition.exposure-gate", "LEDGER_ONLY",
            {"funnel_experiment_state": funnel_experiment.get("state")},
            lambda: enforce_exposure_gate(state),
        )
    except Exception as error:
        exposure_gate = {
            "state": "EXPOSURE_GATE_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        distribution_plan = admit(
            "acquisition.distribution-mix-plan", "LEDGER_ONLY",
            {"exposure_gate_state": exposure_gate.get("state")},
            lambda: materialize_distribution_mix_plan(state, funnel_decision),
        )
    except Exception as error:
        distribution_plan = {
            "state": "DISTRIBUTION_PLAN_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        distribution_route = admit(
            "acquisition.distribution-route", "READ_ONLY",
            {"distribution_plan_state": distribution_plan.get("state")},
            lambda: advance_distribution_route(
                Path(__file__).resolve().parent.parent, state,
                distribution_plan, run_id,
            ),
        )
    except Exception as error:
        distribution_route = {
            "state": "DISTRIBUTION_ROUTE_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    try:
        recirculation_job = admit(
            "acquisition.x-recirculation-job", "LEDGER_ONLY",
            {"distribution_plan_state": distribution_plan.get("state")},
            lambda: create_x_recirculation_job(
                state, distribution_plan, distribution_route,
            ),
        )
    except Exception as error:
        recirculation_job = {
            "state": "RECIRCULATION_JOB_FAILED", "changed": False,
            "failure_type": type(error).__name__,
        }
    if provider["state"] == "AUTHENTICATED" and not placement_link_ready:
        status = placement_link["state"]
    elif not link:
        status = "TRACKING_LINK_UNAVAILABLE"
    elif not browser:
        status = "BROWSER_UNAVAILABLE"
    elif provider["state"] == "AUTHENTICATED":
        status = "READY_FOR_PUBLICATION"
    else:
        status = provider["state"]
    event = {
        "event": "affiliate_wake",
        "provider": "elevenlabs",
        "runtime_guard_state": guard.get("state"),
        "runtime_guard_free_bytes": guard.get("free_bytes"),
        "runtime_guard_floor_bytes": guard.get("floor_bytes"),
        "runtime_guard_receipt_persist_state": guard.get("receipt_persist_state"),
        "owner_health_state": health.get("state"),
        "owner_health_labels": health.get("labels"),
        "owner_health_cdp": health.get("cdp"),
        "quarantine_state": quarantine.get("state"),
        "quarantined_tools": quarantine.get("tools"),
        "action_budget_state": action_budget.get("state"),
        "action_budget_used_attempts": action_budget.get("used_attempts"),
        "action_budget_daily_cap": action_budget.get("daily_cap"),
        "cost_budget_state": cost_budget.get("state"),
        "cost_budget_known_actual_minor_by_currency": cost_budget.get(
            "known_actual_minor_by_currency"
        ),
        "cost_budget_known_actual_usd_minor": cost_budget.get(
            "known_actual_usd_minor"
        ),
        "cost_budget_cap_minor": cost_budget.get("cap_minor"),
        "cost_budget_unknown_rows": cost_budget.get("unknown_rows"),
        "provider_changed": provider["changed"],
        "provider_state": provider["state"],
        "provider_transition_id": provider["transition_id"],
        "provider_recovery_state": recovery_state,
        "placement_link_state": placement_link.get("state"),
        "placement_link_placement": placement_link.get("placement"),
        "placement_link_key": placement_link.get("provider_link_key"),
        "placement_link_changed": placement_link_changed,
        "placement_link_deduplicated": placement_link.get("deduplicated"),
        "placement_link_failure_type": placement_link.get("failure_type"),
        "impact_state": impact["state"],
        "impact_changed": impact["changed"],
        "impact_transition_id": impact["transition_id"],
        "impact_recovery_state": impact_recovery_state,
        "impact_login_reconciled_job_id": impact.get("login_reconciled_job_id"),
        "application_program": application.get("program"),
        "application_state": application.get("state"),
        "application_deduplicated": application.get("deduplicated"),
        "application_failure_type": application.get("failure_type"),
        "systeme_state": systeme.get("state"),
        "systeme_failure_type": systeme.get("failure_type"),
        "systeme_submitted": systeme.get("submitted"),
        "systeme_verification_state": systeme_verification.get("state"),
        "systeme_verification_deduplicated": systeme_verification.get("deduplicated"),
        "publication_state": publication["state"],
        "publication_url": publication["public_url"],
        "publication_failure_type": publication.get("failure_type"),
        "publication_failure_detail": publication.get("failure_detail"),
        "publication_generic_state": publication.get("generic_state"),
        "publication_link_state": publication.get("publication_link_state"),
        "publication_link_placement": publication.get("publication_link_placement"),
        "publication_link_key": publication.get("publication_link_key"),
        "publication_link_changed": publication.get("publication_link_changed"),
        "publication_link_deduplicated": publication.get("publication_link_deduplicated"),
        "publication_link_failure_type": publication.get("publication_link_failure_type"),
        "publication_link_receipt_pending": publication.get("publication_link_receipt_pending"),
        "publication_liveness_state": liveness["state"],
        "publication_liveness_checked": liveness["checked"],
        "publication_liveness_unverified": [
            row["placement_id"] for row in liveness["unverified"]
        ],
        "distribution_state": distribution["state"],
        "distribution_url": distribution.get("public_url"),
        "distribution_plan_id": distribution.get("plan_id"),
        "distribution_channel": distribution.get("channel"),
        "distribution_changed": distribution.get("changed", False),
        "distribution_failure_type": distribution.get("failure_type"),
        "distribution_failure_detail": distribution.get("failure_detail"),
        "devto_metrics_state": devto_metrics.get("state"),
        "devto_article_count": devto_metrics.get("article_count"),
        "devto_page_views": devto_metrics.get("total_page_views"),
        "devto_page_view_delta": devto_metrics.get("delta_page_views"),
        "devto_baseline_state": devto_metrics.get("baseline_state"),
        "devto_baseline_receipt_count": devto_metrics.get("baseline_receipt_count"),
        "devto_metrics_failure_type": devto_metrics.get("failure_type"),
        "repost_observation": {
            key: repost_observation.get(key)
            for key in (
                "state", "changed", "transition_id", "source_file_sha256",
                "post_action_count", "joined_campaign_count",
                "unjoined_post_action_count", "invalid_row_count", "join_state",
                "denominator_state", "revenue_credit_state", "failure_type",
            )
        },
        "x_growth": {
            key: x_growth.get(key)
            for key in (
                "state", "changed", "transition_id", "handle", "rendered_url",
                "followers", "following", "failure_type",
            )
        },
        "x_post_metrics": {
            key: x_post_metrics.get(key)
            for key in (
                "state", "changed", "transition_id", "post_url", "job_id",
                "placement_id", "impressions", "replies", "reposts", "likes",
                "bookmarks", "failure_type",
            )
        },
        "x_channel_ledger": {
            key: x_channel_ledger.get(key)
            for key in (
                "state", "changed", "transition_id", "followers_delta", "lanes",
                "money_state", "failure_type",
            )
        },
        "repost_proposal": {
            key: repost_proposal.get(key)
            for key in (
                "state", "changed", "proposal_id", "placement_id", "plan_id",
                "provider_click_count", "repost_delivery_state",
                "revenue_credit_state", "tracking_link_state", "failure_type",
            )
        },
        "x_distribution_job": {
            key: x_distribution_job.get(key)
            for key in (
                "state", "changed", "job_id", "effect_identity", "placement_id",
                "target_x_account", "cadence_class", "failure_type",
            )
        },
        "acquisition_decision_state": acquisition_decision.get("state"),
        "acquisition_decision_changed": acquisition_decision.get("changed", False),
        "acquisition_decision_baseline_sha256": acquisition_decision.get("baseline_sha256"),
        "acquisition_decision_id": acquisition_decision.get("decision_id"),
        "acquisition_decision_variable": acquisition_decision.get("selected_variable"),
        "acquisition_decision_hypothesis": acquisition_decision.get("hypothesis"),
        "acquisition_decision_instruction": acquisition_decision.get("next_campaign_instruction"),
        "acquisition_decision_failure_type": acquisition_decision.get("failure_type"),
        "funnel_decision": {
            key: funnel_decision.get(key)
            for key in (
                "state", "changed", "decision_id", "source_funnel_transition_id",
                "bottleneck", "exposure_assessment", "selected_variable",
                "hypothesis", "action", "official_success_metric", "failure_type",
            )
        },
        "funnel_experiment": {
            key: funnel_experiment.get(key)
            for key in (
                "state", "changed", "experiment_id", "decision_id",
                "source_funnel_transition_id", "control_placement_id",
                "control_job_id", "control_post_url", "selected_variable",
                "official_success_metric", "observation_state", "failure_type",
            )
        },
        "exposure_gate": {
            key: exposure_gate.get(key)
            for key in (
                "state", "changed", "transition_id", "experiment_id",
                "control_placement_id", "selected_variable",
                "official_success_metric", "baseline_impressions",
                "current_impressions", "conversion_verdict_allowed",
                "distribution_required", "maximize_relevant_exposure",
                "transactions_observed", "transactions_verdict_state", "failure_type",
            )
        },
        "distribution_plan": {
            key: distribution_plan.get(key)
            for key in (
                "state", "changed", "plan_id", "experiment_id", "decision_id",
                "selected_variable", "control_placement_id", "control_job_id",
                "control_content_sha256", "control_post_url", "target_x_account",
                "live_surfaces", "next_action", "cadence_rule",
                "maximize_relevant_exposure", "official_success_metric",
                "content_mutation_allowed", "failure_type",
            )
        },
        "recirculation_job": {
            key: recirculation_job.get(key)
            for key in (
                "state", "changed", "job_id", "effect_identity", "placement_id",
                "owned_article_url", "content_sha256", "experiment_lineage",
                "target_x_account", "cadence_class", "failure_type",
            )
        },
        "distribution_route": {
            key: distribution_route.get(key)
            for key in (
                "state", "changed", "route_id", "plan_id", "target", "reason",
                "evidence", "failure_type",
            )
        },
        "revenue_state": revenue["state"],
        "revenue_source_rows": revenue["source_rows"],
        "revenue_appended_transitions": revenue["appended_transitions"],
        "link_appended_transitions": revenue.get("link_appended_transitions", 0),
        "link_latest_transition": revenue.get("link_latest_transition"),
        "placement_ledger_state": placement_ledger["state"],
        "placement_ledger_sha256": placement_ledger.get("ledger_sha256"),
        "placement_ledger_count": placement_ledger.get("placement_count"),
        "money_funnel": {
            key: money_funnel.get(key)
            for key in (
                "state", "changed", "transition_id", "job_id", "placement_id",
                "post_url", "impressions", "owned_entries", "cta_clicks",
                "provider_clicks", "transactions", "commission_status_counts",
                "approved_or_paid_money_state", "cost", "failure_type",
            )
        },
        "placement_ledger_failure_type": placement_ledger.get("failure_type"),
        "funnel_snapshot_state": funnel_snapshot.get("state"),
        "funnel_snapshot_sha256": funnel_snapshot.get("snapshot_sha256"),
        "funnel_snapshot_placement_ids": [
            row.get("placement_id") for row in funnel_snapshot.get("placements", [])
        ],
        "owned_visit_state": owned_visits.get("state"),
        "owned_visit_reason": owned_visits.get("reason"),
        "owned_visit_receipt_sha256": owned_visits.get("receipt_sha256"),
        "owned_entry_state": owned_entries.get("state"),
        "owned_entry_receipt_sha256": owned_entries.get("receipt_sha256"),
        "owned_entry_failure_type": owned_entries.get("failure_type"),
        "cta_instrumentation_state": cta_instrumentation.get("state"),
        "cta_instrumentation_commit": cta_instrumentation.get("commit"),
        "cta_instrumentation_failure_type": cta_instrumentation.get("failure_type"),
        "cta_click_state": cta_clicks.get("state"),
        "cta_click_receipt_sha256": cta_clicks.get("receipt_sha256"),
        "cta_click_failure_type": cta_clicks.get("failure_type"),
        "interval_funnel_state": interval_funnel.get("state"),
        "interval_funnel_receipt_sha256": interval_funnel.get("receipt_sha256"),
        "interval_funnel_failure_type": interval_funnel.get("failure_type"),
        "focused_cohort_state": focused_cohort.get("state"),
        "focused_cohort_placement_id": focused_cohort.get("placement_id"),
        "focused_cohort_receipt_sha256": focused_cohort.get("receipt_sha256"),
        "focused_cohort_expansion_state": focused_cohort.get("placement_expansion_state"),
        "focused_cohort_failure_type": focused_cohort.get("failure_type"),
        "rolling_net_state": rolling_net.get("state"),
        "rolling_net_money_state": rolling_net.get("money_state"),
        "rolling_net_net_state": rolling_net.get("net_state"),
        "rolling_net_threshold_state": rolling_net.get("threshold_state"),
        "rolling_net_approved_or_paid_net_usd": rolling_net.get("approved_or_paid_net_usd"),
        "rolling_net_sha256": rolling_net.get("receipt_sha256"),
        "rolling_net_placement_ledger_sha256": rolling_net.get("placement_ledger_sha256"),
        "rolling_net_failure_type": rolling_net.get("failure_type"),
        "rolling_net_status_counts": rolling_net.get("status_counts"),
        "rolling_net_cost_state": rolling_net.get("cost_state"),
        "rolling_net_cost_coverage_state": rolling_net.get("cost_coverage_state"),
        "rolling_net_unknown_reasons": rolling_net.get("unknown_reasons"),
        "status": status,
        "ts": int(time.time()),
    }
    event["wake_event_uuid"] = wake_event_uuid(event)

    def reconcile_history():
        rows = reconcile_telegram_delivery_history(state, event)
        return {"state": "RECONCILED" if rows else "NO_CHANGE", "changed": bool(rows), "count": len(rows)}

    telegram_history = admit(
        "telegram.reconcile-history", "LEDGER_ONLY", {"wake_event_uuid": event["wake_event_uuid"]},
        reconcile_history,
        wake_event_uuid=event["wake_event_uuid"],
    )
    event["telegram_history_reconciled_count"] = telegram_history.get("count", 0)
    append(state / "events.jsonl", event)
    atomic_json(state / "last-run.json", event)
    telegram_event = next_telegram_event(state, event)
    telegram = admit(
        "telegram.send", "MESSAGE_SEND", {"event_kind": telegram_event.get("kind")},
        lambda: flush_telegram(state, telegram_event),
        wake_event_uuid=event["wake_event_uuid"],
    )
    delivery_receipt = append_telegram_delivery_receipt(
        state, event, telegram_event, telegram,
    )
    event["telegram_event_uuid"] = delivery_receipt.get("telegram_event_uuid")
    event["telegram_state"] = telegram["state"]
    event["telegram_message_id"] = telegram["message_id"]
    event["telegram_delivery_receipt_event_uuid"] = delivery_receipt["event_uuid"]
    atomic_json(state / "last-run.json", event)
    append_run_receipt(
        state,
        event,
        started_at,
        run_id=event["wake_event_uuid"],
        terminal_state=event.get("status") or "UNKNOWN",
        scheduler_run_id=run_id,
    )
    lock.close()
    print(json.dumps(event, sort_keys=True, separators=(",", ":")))
    return 0


def placement(args):
    link = elevenlabs_link(args.private_markdown.expanduser())
    if not link:
        raise ValueError("verified ElevenLabs tracking link is unavailable")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", args.placement):
        raise ValueError("invalid placement")
    state = args.state.expanduser()
    receipt = {
        "event": "placement_ready",
        "locale": args.locale,
        "placement": args.placement,
        "provider": "elevenlabs",
        "status": "TRACKING_LINK_VERIFIED",
        "ts": int(time.time()),
    }
    created = append_unique(
        state / "placements.jsonl", receipt, ("provider", "locale", "placement")
    )
    receipt["deduplicated"] = not created
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="affiliate loop")
    parser.add_argument("command", choices=("wake", "placement"))
    parser.add_argument("--state", type=Path, default=Path("~/.local/state/life-manager/affiliate"))
    parser.add_argument("--private-markdown", type=Path, default=Path("~/.config/anicca/affiliate-credentials.md"))
    parser.add_argument("--cdp-port", type=int, default=9324)
    parser.add_argument("--x-cdp-port", type=int, default=9326)
    parser.add_argument("--impact-cdp-port", type=int, default=9327)
    parser.add_argument(
        "--landing-root", type=Path,
        default=Path(os.environ.get("AFFILIATE_LANDING_ROOT", Path(__file__).resolve().parents[3])),
    )
    parser.add_argument("--placement", default="article-1")
    parser.add_argument("--locale", choices=("en", "ja"), default="en")
    args = parser.parse_args()
    return wake(args) if args.command == "wake" else placement(args)


if __name__ == "__main__":
    raise SystemExit(main())
