#!/usr/bin/env python3
"""Publish one canonical Affiliate guide to DEV through the existing Forem pattern.

Copy+tweak source: skills/writer-engine/publishers/publisher_core.py.  The
Affiliate variant adds Forem's official canonical_url field and reuses the
Affiliate external-effect journal instead of Writer state.
"""

import argparse
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from job_journal import resume_effect, start_effect, unresolved_effect, verify_effect


class DevtoError(RuntimeError):
    pass


def _atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _key():
    if os.environ.get("DEVTO_API_KEY", "").strip():
        return os.environ["DEVTO_API_KEY"].strip()
    for path in (Path(os.environ.get("LIFE_MANAGER_ENV_FILE", "~/.local/state/life-manager/.env")),):
        path = path.expanduser()
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("DEVTO_API_KEY=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip().strip("\"'")
    raise DevtoError("DEVTO_API_KEY is unavailable")


def _request(url, key, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "api-key": key, "Content-Type": "application/json",
        "User-Agent": "life-manager-affiliate/1",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise DevtoError(f"DEV request failed: {type(error).__name__}") from error


def _existing(key, marker):
    rows = _request("https://dev.to/api/articles/me/all?per_page=1000&state=all", key)
    if not isinstance(rows, list):
        raise DevtoError("DEV article list is not an array")
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        detail = row if "body_markdown" in row else _request(
            f"https://dev.to/api/articles/{row['id']}", key,
        )
        if marker in str(detail.get("body_markdown", "")):
            return detail
    return None


def _public_readback(url, title):
    request = urllib.request.Request(url, headers={"User-Agent": "life-manager-affiliate/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
            return response.status == 200 and title in html
    except urllib.error.URLError:
        return False


def observe_metrics(state):
    """Persist real DEV exposure metrics for Affiliate-owned publications."""
    state = Path(state).expanduser()
    observed_at = datetime.now(timezone.utc)
    publications = {}
    for path in (state / "devto-publications").glob("*.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if receipt.get("state") == "LIVE" and receipt.get("public_id"):
            publications[str(receipt["public_id"])] = receipt
    rows = _request("https://dev.to/api/articles/me/all?per_page=1000&state=all", _key())
    if not isinstance(rows, list):
        raise DevtoError("DEV article list is not an array")
    by_id = {str(row.get("id")): row for row in rows if isinstance(row, dict) and row.get("id")}
    articles = []
    for public_id, publication in sorted(publications.items()):
        row = by_id.get(public_id)
        if not row:
            raise DevtoError("DEV publication is missing from authenticated metrics readback")
        values = {}
        for field in (
            "page_views_count", "public_reactions_count",
            "positive_reactions_count", "comments_count",
        ):
            value = row.get(field)
            if not isinstance(value, int) or value < 0:
                raise DevtoError(f"DEV returned invalid {field}")
            values[field] = value
        articles.append({
            "plan_id": publication.get("plan_id"),
            "placement_id": publication.get("placement_id"),
            "public_id": public_id,
            "public_url": publication.get("public_url"),
            "published_at": publication.get("published_at"),
            "experiment": publication.get("experiment"),
            **values,
        })
    receipt_path = state / "distribution-metrics" / "devto.json"
    try:
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    previous_views = {
        str(row.get("public_id")): row.get("page_views_count", 0)
        for row in previous.get("articles", []) if isinstance(row, dict)
    }
    for article in articles:
        prior = previous_views.get(article["public_id"], 0)
        if article["page_views_count"] < prior:
            raise DevtoError("DEV page views moved backwards")
        article["delta_page_views"] = article["page_views_count"] - prior
        try:
            published_at = datetime.fromisoformat(article["published_at"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise DevtoError("DEV publication has no valid published_at") from error
        article["age_seconds"] = max(0, int((observed_at - published_at).total_seconds()))
        article["baseline_ready"] = article["age_seconds"] >= 86400
        if article["baseline_ready"]:
            baseline_path = state / "distribution-baselines" / f"devto-{article['public_id']}.json"
            if not baseline_path.is_file():
                _atomic(baseline_path, {
                    "schema_version": 1,
                    "receipt_type": "DEVTO_24H_BASELINE",
                    "provider": "devto",
                    "public_id": article["public_id"],
                    "plan_id": article["plan_id"],
                    "placement_id": article["placement_id"],
                    "published_at": article["published_at"],
                    "observed_at": observed_at.isoformat(),
                    "age_seconds": article["age_seconds"],
                    "page_views_count": article["page_views_count"],
                    "public_reactions_count": article["public_reactions_count"],
                    "comments_count": article["comments_count"],
                    "experiment": article.get("experiment"),
                })
            article["baseline_receipt_sha256"] = hashlib.sha256(
                baseline_path.read_bytes()
            ).hexdigest()
    digest = hashlib.sha256(json.dumps(articles, sort_keys=True).encode()).hexdigest()
    baseline_ready = bool(articles) and all(row["baseline_ready"] for row in articles)
    receipt = {
        "schema_version": 1, "receipt_type": "DEVTO_DISTRIBUTION_METRICS",
        "state": "OBSERVED", "observed_at": observed_at.isoformat(),
        "baseline_state": "READY" if baseline_ready else "WAITING_24H",
        "baseline_receipt_count": sum(
            bool(row.get("baseline_receipt_sha256")) for row in articles
        ),
        "article_count": len(articles),
        "total_page_views": sum(row["page_views_count"] for row in articles),
        "delta_page_views": sum(row["delta_page_views"] for row in articles),
        "total_reactions": sum(row["public_reactions_count"] for row in articles),
        "total_comments": sum(row["comments_count"] for row in articles),
        "metrics_sha256": digest, "articles": articles,
    }
    _atomic(receipt_path, receipt)
    return receipt


def publish(state, plan_id):
    state = Path(state).expanduser()
    campaign_path = state / "campaign-publications" / f"{plan_id}.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    if campaign.get("state") != "X_LIVE":
        raise DevtoError("campaign is not X_LIVE")
    slug = campaign["slug"]
    artifact = json.loads((state / "content" / f"{slug}.json").read_text(encoding="utf-8"))
    policy = json.loads((state / "policy" / f"{slug}.json").read_text(encoding="utf-8"))
    markdown = artifact.get("markdown", "")
    links = artifact.get("readback_links", [])
    tracking_link = links[0] if len(links) == 1 else ""
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    if not all((
        digest == campaign.get("content_sha256"),
        artifact.get("state") == "READY_FOR_PUBLICATION",
        artifact.get("disclosure") == "affiliate_link",
        policy.get("decision") == "PASS",
        policy.get("content_sha256") == digest,
        tracking_link and markdown.count(tracking_link) == 1,
        "affiliate link" in markdown[:markdown.find(tracking_link)].casefold(),
    )):
        raise DevtoError("campaign artifact does not match publication receipt")
    placement = campaign["placement_id"]
    marker = f"affiliate-intent:{placement}"
    full_marker = f"<!-- {marker} content-sha256:{digest} -->"
    canonical = campaign["owned_url"]
    receipt_path = state / "devto-publications" / f"{plan_id}.json"
    if receipt_path.is_file():
        prior = json.loads(receipt_path.read_text(encoding="utf-8"))
        if prior.get("state") == "LIVE":
            return {**prior, "deduplicated": True}
    key = _key()
    existing = _existing(key, marker)
    created = existing is None
    job = unresolved_effect(state, "DEVTO_PUBLICATION", placement)
    if existing is None:
        body = markdown.rstrip() + "\n\n" + full_marker + "\n"
        action = {
            "operation": "publish_devto", "placement": placement,
            "canonical_url": canonical, "content_sha256": digest,
        }
        job = resume_effect(state, "DEVTO_PUBLICATION", placement) if job else start_effect(
            state, "DEVTO_PUBLICATION", placement, action,
            {"state": "READY", "canonical_url": canonical}, 86400,
        )
        existing = _request("https://dev.to/api/articles", key, "POST", {"article": {
            "title": artifact["title"], "body_markdown": body,
            "published": True, "canonical_url": canonical,
            "tags": ["ai", "productivity", "webdev", "tutorial"],
        }})
    if not isinstance(existing, dict) or not existing.get("id"):
        raise DevtoError("DEV publication has no stable id")
    readback = _request(f"https://dev.to/api/articles/{existing['id']}", key)
    live_url = str(readback.get("url", ""))
    valid = all((
        marker in str(readback.get("body_markdown", "")),
        full_marker in str(readback.get("body_markdown", "")),
        readback.get("canonical_url") == canonical,
        bool(readback.get("published_at")),
        live_url.startswith("https://dev.to/"),
        _public_readback(live_url, artifact["title"]),
    ))
    if not valid:
        raise DevtoError("DEV publication failed exact public readback")
    external = {"state": "LIVE", "public_id": str(readback["id"]), "public_url": live_url}
    if job:
        verify_effect(state, job["job_id"], external)
    receipt = {
        "schema_version": 1, "receipt_type": "DEVTO_PUBLICATION", "state": "LIVE",
        "plan_id": plan_id, "placement_id": placement, "canonical_url": canonical,
        "public_id": str(readback["id"]), "public_url": live_url,
        "content_sha256": digest, "published_at": readback["published_at"],
        "experiment": campaign.get("experiment"),
        "observed_at": datetime.now(timezone.utc).isoformat(), "deduplicated": not created,
    }
    _atomic(receipt_path, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(prog="affiliate distribution")
    parser.add_argument("command", choices=("publish-devto", "publish-substack"))
    parser.add_argument("--state", type=Path, default=Path("~/.local/state/life-manager/affiliate"))
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    if args.command == "publish-substack":
        from substack_publish import publish as selected
    else:
        selected = publish
    print(json.dumps(selected(args.state, args.plan), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
