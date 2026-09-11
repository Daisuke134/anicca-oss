#!/usr/bin/env python3
"""Syndicate one verified Affiliate article through the proven Writer API shape."""

import hashlib
import html
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from job_journal import resume_effect, start_effect, unresolved_effect, verify_effect


PUBLICATION = "aniccabuddha.substack.com"


class SubstackError(RuntimeError):
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


def _cookie():
    if os.environ.get("SUBSTACK_SESSION_COOKIE", "").strip():
        return os.environ["SUBSTACK_SESSION_COOKIE"].strip()
    for path in (Path(os.environ.get("LIFE_MANAGER_ENV_FILE", "~/.local/state/life-manager/.env")),):
        path = path.expanduser()
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("SUBSTACK_SESSION_COOKIE=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SubstackError("SUBSTACK_SESSION_COOKIE is unavailable")


def _json(url, cookie, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", "Content-Type": "application/json",
        "Cookie": cookie, "Referer": f"https://{PUBLICATION}/publish/post",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read(512).decode("utf-8", errors="replace").replace("\n", " ")
        raise SubstackError(f"Substack HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise SubstackError(f"Substack request failed: {type(error).__name__}") from error
    if not isinstance(value, (dict, list)):
        raise SubstackError("Substack API returned an unsupported value")
    return value


class _Article(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth, self.count, self.parts = 0, 0, []

    def handle_starttag(self, tag, attrs):
        if tag == "article" and self.depth == 0:
            self.depth, self.count = 1, self.count + 1
        elif self.depth:
            self.parts.append(self.get_starttag_text())
            if tag not in self.VOID:
                self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if self.depth:
                self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.depth:
            self.parts.append(f"&#{name};")


def _owned_html(url):
    request = urllib.request.Request(url, headers={"User-Agent": "life-manager-affiliate/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        parser = _Article()
        parser.feed(response.read().decode("utf-8", errors="replace"))
    body = "".join(parser.parts)
    if parser.count != 1 or "affiliate link" not in body.casefold() or "try.elevenlabs.io" not in body:
        raise SubstackError("owned article HTML failed disclosure readback")
    return body


def _public_html(url):
    request = urllib.request.Request(url, headers={"User-Agent": "life-manager-affiliate/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace") if response.status == 200 else ""
    except urllib.error.URLError:
        return ""


def _profile(cookie):
    profile = _json("https://substack.com/api/v1/user/profile/self", cookie)
    matches = [row for row in profile.get("publicationUsers", []) if
               row.get("publication", {}).get("subdomain") == PUBLICATION.removesuffix(".substack.com")]
    if len(matches) != 1 or not isinstance(matches[0].get("user_id"), int):
        raise SubstackError("session does not own the configured publication")
    return matches[0]["user_id"]


def _inline(value):
    nodes, position = [], 0
    for match in re.finditer(r"\[([^\]]+)\]\((https://[^)]+)\)|(https://\S+)", value):
        if match.start() > position:
            nodes.append({"type": "text", "text": value[position:match.start()]})
        label, target = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(3))
        nodes.append({"type": "text", "text": label,
                      "marks": [{"type": "link", "attrs": {"href": target}}]})
        position = match.end()
    if position < len(value):
        nodes.append({"type": "text", "text": value[position:]})
    return nodes


def _prosemirror(markdown):
    paragraphs = [row.strip() for row in re.split(r"\n\s*\n", markdown) if row.strip()]
    return json.dumps({"type": "doc", "content": [
        {"type": "paragraph", "content": _inline(row.replace("\n", " "))}
        for row in paragraphs
    ]}, ensure_ascii=False, separators=(",", ":"))


def _public_targets(title):
    feed = _public_html(f"https://{PUBLICATION}/feed")
    links = re.findall(
        r"<item>.*?<title><!\[CDATA\[\s*" + re.escape(title) +
        r"\s*\]\]></title>.*?<link>(https://[^<]+)</link>.*?</item>",
        feed, re.DOTALL,
    )
    targets = []
    for link in reversed(list(dict.fromkeys(html.unescape(row) for row in links))):
        visible = _public_html(link)
        match = re.search(r'\\"post\\":\{.*?\\"id\\":(\d+)', visible)
        if match:
            targets.append({"id": match.group(1), "url": link})
    return targets


def _existing(cookie, marker):
    value = _json(f"https://{PUBLICATION}/api/v1/drafts", cookie)
    rows = value if isinstance(value, list) else value.get("drafts", value.get("posts", []))
    matches = [str(row.get("id")) for row in rows if isinstance(row, dict)
               and marker in json.dumps(row, ensure_ascii=False) and str(row.get("id", "")).isdigit()]
    matches = list(dict.fromkeys(matches))
    if len(matches) > 1:
        raise SubstackError("multiple Substack targets share one placement marker")
    return matches[0] if matches else None


def publish(state, plan_id):
    state = Path(state).expanduser()
    campaign = json.loads((state / "campaign-publications" / f"{plan_id}.json").read_text())
    artifact = json.loads((state / "content" / f"{campaign['slug']}.json").read_text())
    policy = json.loads((state / "policy" / f"{campaign['slug']}.json").read_text())
    markdown = artifact.get("markdown", "")
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    link = (artifact.get("readback_links") or [""])[0]
    if not all((campaign.get("state") == "X_LIVE", digest == campaign.get("content_sha256"),
                policy.get("decision") == "PASS", policy.get("content_sha256") == digest,
                markdown.count(link) == 1, "affiliate link" in markdown[:markdown.find(link)].casefold())):
        raise SubstackError("campaign failed the distribution policy gate")
    receipt_path = state / "substack-publications" / f"{plan_id}.json"
    if receipt_path.is_file():
        prior = json.loads(receipt_path.read_text())
        if prior.get("state") == "LIVE":
            return {**prior, "deduplicated": True}
    placement, cookie = campaign["placement_id"], _cookie()
    marker = f"affiliate-intent:{placement} content-sha256:{digest}"
    job = unresolved_effect(state, "SUBSTACK_PUBLICATION", placement)
    target_path = state / "substack-targets" / f"{plan_id}.json"
    target_row = json.loads(target_path.read_text()) if target_path.is_file() else {}
    target = str(target_row.get("target", "")) or _existing(cookie, marker)
    public_targets = _public_targets(artifact["title"]) if job and not target else []
    if public_targets:
        target = public_targets[0]["id"]
        _atomic(target_path, {"target": target, "public_url": public_targets[0]["url"],
                              "recovered": True})
    if job and not target:
        raise SubstackError("unresolved Substack effect requires public recovery; refusing a new draft")
    created = target is None
    payload = {
        "draft_title": artifact["title"], "draft_subtitle": "",
        "draft_body": _prosemirror(markdown),
        "draft_bylines": [{"id": _profile(cookie), "is_guest": False}],
        "type": "newsletter", "audience": "everyone", "draft_section_id": None,
        "section_chosen": True, "write_comment_permissions": "everyone",
        "should_send_email": False,
    }
    if target is None:
        action = {"operation": "publish_substack", "placement": placement,
                  "owned_url": campaign["owned_url"], "content_sha256": digest}
        job = resume_effect(state, "SUBSTACK_PUBLICATION", placement) if job else start_effect(
            state, "SUBSTACK_PUBLICATION", placement, action,
            {"state": "READY", "owned_url": campaign["owned_url"]}, 86400,
        )
        draft = _json(f"https://{PUBLICATION}/api/v1/drafts", cookie, "POST", payload)
        target = str(draft.get("id", ""))
        if not target.isdigit():
            raise SubstackError("draft creation returned no stable id")
        _atomic(target_path, {"target": target, "recovered": False})
    else:
        updated = _json(f"https://{PUBLICATION}/api/v1/drafts/{target}", cookie, "PUT", payload)
        if str(updated.get("id", target)) != target:
            raise SubstackError("Substack update changed the protected target")
    _json(f"https://{PUBLICATION}/api/v1/drafts/{target}/publish", cookie, "POST",
          {"send": False, "share_automatically": False})
    readback = _json(f"https://{PUBLICATION}/api/v1/drafts/{target}", cookie)
    slug = readback.get("slug") or readback.get("draft_slug")
    live_url = f"https://{PUBLICATION}/p/{slug}" if slug else ""
    visible = _public_html(f"{live_url}?output=1") if live_url else ""
    if not ((readback.get("is_published") is True or readback.get("post_date"))
            and artifact["title"] in visible and "affiliate link" in visible.casefold()):
        raise SubstackError("Substack publication failed exact public readback")
    external = {"state": "LIVE", "public_id": target, "public_url": live_url}
    if job:
        verify_effect(state, job["job_id"], external)
    receipt = {"schema_version": 1, "receipt_type": "SUBSTACK_PUBLICATION", "state": "LIVE",
               "plan_id": plan_id, "placement_id": placement, "public_id": target,
               "public_url": live_url, "owned_url": campaign["owned_url"],
               "content_sha256": digest, "observed_at": datetime.now(timezone.utc).isoformat(),
               "experiment": campaign.get("experiment"),
               "deduplicated": not created,
               "duplicate_public_urls": [row["url"] for row in public_targets[1:]]}
    _atomic(receipt_path, receipt)
    return receipt
