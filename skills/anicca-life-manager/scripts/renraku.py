#!/usr/bin/env python3
"""
renraku — when the user is confirmed late, tell the right people automatically.

OSS-general: NO hardcoded names/addresses. Recipients + signing identity come
from the per-user gitignored profile (identity/profile.json):
  - profile.stakeholder_for(event) -> {channel, to, sender}  (e.g. work->boss email)
  - profile.identity_for(event)     -> signing name (comedy=stage name, work=legal)
Delivery order: profile stakeholder (email/slack) -> calendar attendees -> Slack draft.

Tone: humanized JA, polite but not stiff.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_SHARED = Path(__file__).resolve().parents[2] / "_shared"
sys.path.insert(0, str(REPO_SHARED))
import anicca_profile as prof  # noqa: E402

LIFE_MANAGER_HOME = Path(os.environ.get(
    "LIFE_MANAGER_HOME", str(Path.home() / ".local" / "state" / "life-manager"),
))
ANICCA_HOME = Path(os.environ.get("ANICCA_HOME", str(LIFE_MANAGER_HOME)))
ENV_PATH = Path(os.environ.get(
    "LIFE_MANAGER_ENV_FILE",
    str(ANICCA_HOME / ".env"),
))
ENV = ENV_PATH.read_text() if ENV_PATH.is_file() else ""


def env(name, default=""):
    if name in os.environ:
        return os.environ[name]
    m = re.search(rf"^{name}=(.*)$", ENV, re.M)
    return (m.group(1).strip().strip('"').strip("'") if m else default)


def gog_account():
    return env("GOG_ACCOUNT", "") or prof.google_account()


def auto_send_allowed(profile: dict) -> bool:
    """第三者への謝罪mail自動送信は明示opt-in (lateness.autoSendMail:true) のみ。
    既定 False = 送信前にユーザー確認 (Capafy reject R2: third-party連絡前のconsent)。"""
    return bool(((profile or {}).get("lateness") or {}).get("autoSendMail", False))


def compose(sender, event, minutes):
    # Dais 2026-05-31 mail template HARD RULE (Power of Free BAN 事件 後):
    #   ・event 名 入れない (誤特定 リスク回避)
    #   ・名前 入れない (誤発信 回避)
    #   ・「すぐ向かいます」入れない (Dais 不指示の妄想)
    #   ・「申し訳ございません」必須
    #   ・そっけなく謝罪のみ
    # sender/event は受け取るが本文に展開しない (state log 用に残す)。
    return (
        f"お世話になっております。\n\n"
        f"本日 約 {minutes} 分 遅刻となります。\n"
        f"ご迷惑をお掛けし、申し訳ございません。\n\n"
        f"よろしくお願いいたします。"
    )


def send_gmail(to_addr, subject, body):
    out = subprocess.run(
        [os.environ.get("LIFE_MANAGER_GOG", "gog"), "gmail", "send", "--account", gog_account(),
         "--to", to_addr, "--subject", subject, "--body", body],
        capture_output=True, text=True,
        env={**os.environ, "GOG_KEYRING_PASSWORD": env("GOG_KEYRING_PASSWORD")},
    )
    return out.returncode == 0, (out.stderr or out.stdout)[:200]


def slack(text, channel=None):
    try:
        req = urllib.request.Request("https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel or env("SLACK_CHANNEL_ID"), "text": text}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {env('SLACK_BOT_TOKEN')}"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"[renraku] slack failed: {e}", file=sys.stderr)
        return False


def send_renraku(event, minutes, attendees=None):
    """event: {summary, location, attendees?, description?}. Returns a result dict."""
    summary = event.get("summary", "予定")
    ctx_text = f"{summary} {event.get('location','')} {event.get('description','')}"
    entry = prof.stakeholder_for(ctx_text)
    sender = (entry or {}).get("sender") or prof.identity_for(ctx_text)
    msg = compose(sender, summary, minutes)
    subject = "本日の遅刻のお知らせ"  # event 名 入れない (Dais 2026-05-31 厳命)

    # Capafy reject R2: 第三者連絡前の確認。autoSendMail opt-in でなければ送信せず下書きのみ。
    try:
        _profile = prof.load_profile()
    except Exception:
        _profile = {}
    if not auto_send_allowed(_profile):
        slack(f"🏃 *遅刻 renraku 確認待ち* (自動送信OFF)\n予定: *{summary}*\n送信先候補: {((entry or {}).get('to')) or 'attendees/未登録'}\nそのまま送れる文面:\n> {msg}\n"
              f"_自動送信を許可するなら profile.json の lateness.autoSendMail を true に。_")
        return {"via": "confirm-required", "ok": False, "sent": False}

    # 1) profile stakeholder (email / slack)
    if entry:
        if entry.get("channel") == "email" and entry.get("to"):
            ok, info = send_gmail(entry["to"], subject, msg)
            slack(f"🏃 renraku: {summary} → email {entry['to']} ({'sent' if ok else 'FAILED '+info})\n> {msg}")
            return {"via": "profile-email", "ok": ok}
        if entry.get("channel") == "slack" and entry.get("to"):
            ok = slack(msg, channel=entry["to"])
            slack(f"🏃 renraku: {summary} → slack {entry['to']} ({'sent' if ok else 'FAIL'})\n> {msg}")
            return {"via": "profile-slack", "ok": ok}

    # 2) calendar attendees -> email
    emails = [a.get("email") for a in (attendees or event.get("attendees") or [])
              if a.get("email") and a.get("email") != gog_account()]
    if emails:
        results = [send_gmail(e, subject, msg) for e in emails]
        ok = all(r[0] for r in results)
        slack(f"🏃 renraku: {summary} → {len(emails)} attendee(s) ({'sent' if ok else 'partial/FAIL'})\n> {msg}")
        return {"via": "attendees", "ok": ok}

    # 3) Firecrawl fallback (Dais 2026-05-31 A7): event 名で web 検索 → 公式 contact mail 抽出
    fc_email = firecrawl_find_contact(summary, event)
    if fc_email and fc_email not in get_blocklist_apply(prof):
        ok, info = send_gmail(fc_email, subject, msg)
        slack(f"🏃 renraku (Firecrawl fallback): {summary} → {fc_email} ({'sent' if ok else 'FAILED '+info})\n> {msg}")
        return {"via": "firecrawl-fallback", "ok": ok, "to": fc_email}

    # 4) no recipient known -> ready draft to Slack for one-tap forward
    slack(f"🏃 *遅刻 renraku 要送信* (送信先未登録)\n予定: *{summary}*\nそのまま送れる文面:\n> {msg}\n"
          f"_この種の予定の連絡先を profile.json stakeholders に入れれば次回から自動送信。_")
    return {"via": "slack-draft", "ok": True}


# ── Firecrawl fallback (A7) ────────────────────────────────────────────────────
def firecrawl_find_contact(summary, event=None):
    """Try to find the organizer/host contact email via Firecrawl when stakeholder
    map and attendees are empty. Returns email str or None.
    Strategy: search for event title + 連絡先, scrape top hit, extract first
    plausible email. Best-effort, fail-safe (None on any error)."""
    if not summary:
        return None
    try:
        # Build query: event title + "連絡先" or "contact"
        q = f"{summary} 連絡先 OR contact"
        # Use firecrawl CLI (auto-installed by `brew install firecrawl-cli`).
        # Search top 3 results, scrape, extract first valid email.
        import shlex
        cmd = [os.environ.get("LIFE_MANAGER_FIRECRAWL", "firecrawl"), "search", q, "--limit", "3", "--format", "markdown"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        # Extract first plausible email (skip the operator's own gog account
        # and common noise). The operator's address comes from env at runtime
        # so no maintainer identity is baked into shipping code.
        emails = re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", out.stdout)
        own_account = (env("GOG_ACCOUNT", "") or "").lower()
        skip = {own_account, "noreply@", "no-reply@", "support@firecrawl", "info@example"}
        for em in emails:
            if any(em.lower().startswith(s) or s in em.lower() for s in skip):
                continue
            return em
    except Exception as e:
        print(f"[renraku] firecrawl fallback failed: {e}", file=sys.stderr)
    return None


def get_blocklist_apply(prof_module):
    """Read profile.lateness.blocklistApply (Dais 2026-05-31). These addresses
    must NEVER receive APPLY/エントリー mail. They MAY receive 遅刻 mail (this
    function is checked in step 3 to avoid sending fallback apply-looking mail
    to BAN list, but renraku-mail itself is allowed per blocklistRenraku)."""
    try:
        p = prof_module.load()
        return set(p.get("lateness", {}).get("blocklistApply", []))
    except Exception:
        return set()


if __name__ == "__main__":
    ev = {"summary": sys.argv[1] if len(sys.argv) > 1 else "テスト予定"}
    mins = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    print(json.dumps(send_renraku(ev, mins), ensure_ascii=False))
