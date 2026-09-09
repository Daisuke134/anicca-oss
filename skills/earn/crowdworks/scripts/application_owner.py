#!/usr/bin/env python3
"""Continuously eligible CrowdWorks application owner; one bounded tick per launch."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import tempfile
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[4]
STATE = Path("~/.local/state/anicca/crowdworks").expanduser()
CATALOG = ROOT / "skills" / "gig-work" / "profile" / "listings" / "catalog.json"
TRANSACTION = STATE / "application-transaction.json"
LEDGER = STATE / "application-receipts.jsonl"
SEARCH_BUDGET_SECONDS = 240
# The lane wakes every 300s (config/loop-registry.json) and gets through about five listings
# before the search budget runs out -- measured 2026-09-07 as out_of_time 15 of 20, every wake.
# The rotation used to be the day of the year, so it advanced once a day: the same five listings
# were searched all day and the other fifteen were never looked at at all. Stepping by the number
# actually read, once per wake, covers the whole catalogue in four wakes instead of never.
WAKE_INTERVAL_SECONDS = 300
LISTINGS_READ_PER_WAKE = 5
# CrowdWorks files every posting under one of nineteen groups. Searching the catalogue's own
# twenty nouns instead asked the board only about work we already sell, so a posting the fleet can
# do but has no listing phrased for stayed invisible -- the same blindness that had Lancers
# skipping translation and salesmarketing entirely. Walk the board; price with the catalogue.
#
# Nothing is dropped here on a guess about what a group holds. video_contents and sounds do carry
# production work the fleet refuses, hardware_development and living carry physical work, but
# work_fit judges the posting rather than the shelf, and a group left out is a group never seen.
JOB_GROUPS = (
    "development", "software_development", "web_products", "ai_machine_learning", "ai_bpo",
    "ec", "business", "writing_beginner", "translation_and_interpretation", "idea",
    "design", "multimedia", "video_contents", "sounds", "3dcg", "task",
    "parttime_system_operations", "hardware_development", "living",
)
GROUPS_READ_PER_WAKE = 5


def _groups(now):
    """A rotating slice of the board, advancing every wake so all nineteen are read in four."""
    total = max(1, len(JOB_GROUPS))
    try:
        wake = int(now.timestamp() // WAKE_INTERVAL_SECONDS)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        wake = 0
    start = (wake * GROUPS_READ_PER_WAKE) % total
    return tuple(JOB_GROUPS[(start + offset) % total] for offset in range(min(GROUPS_READ_PER_WAKE, total)))


def _listing_for(listings, title, detail):
    """The first catalogue listing whose own nouns appear in this posting, or None.

    The board decides what exists and the catalogue decides what it is worth. Before this the
    catalogue decided both, by being the only thing the lane ever searched for.
    """
    for listing in listings:
        if any(term in title or term in detail for term in listing["terms"]):
            return listing
    return None


def _rotation(now, listings):
    total = max(1, len(listings))
    try:
        wake = int(now.timestamp() // WAKE_INTERVAL_SECONDS)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return 0
    return (wake * LISTINGS_READ_PER_WAKE) % total
# 固定報酬制 10,000円 〜 30,000円 / 固定報酬制 50,000円
_BUDGET = re.compile(r"固定報酬制\s*([\d,]+)\s*円(?:\s*〜\s*([\d,]+)\s*円)?")

def _listings():
    """The shared 3-platform catalog. The term derivation that used to live here is now
    listing_catalog.listing_terms(), because it was the only correct implementation of "what do
    we search for" and the other two platforms each had their own."""
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    out = []
    for item in data["listings"]:
        terms = list(listing_catalog.listing_terms(item))
        tiers = sorted(item["tiers"], key=lambda tier: tier["price_jpy"])
        if terms and tiers: out.append({**item, "terms": terms, "tiers": tiers})
    return out

# There used to be a BUILD_CATEGORIES allow-list here, and it had already been widened once for
# 「AI・チャットボット開発」「ChatGPT開発」「Webサイト更新・保守」. On 2026-09-07 it was still
# rejecting 59 of 98 open postings, among them 「HTML・CSSコーディング」 and, again,
# 「AI・チャットボット開発」. An allow-list has to be wrong every time CrowdWorks names a new
# category, and development is what the fleet is best at rather than all it can deliver, so the
# refusals -- not the acceptances -- are what belongs in a list. That list is now shared with
# Lancers and Coconala instead of being spelled a third way here.

_CATEGORY = re.compile(r"([^ ]{2,40})の仕事の依頼")

def _category(text):
    """CrowdWorks labels every posting 「<カテゴリ>の仕事の依頼」 just above the client block.
    The page's first 仕事を探す is the global nav bar, so reading from there judged nothing."""
    match = _CATEGORY.search(text)
    return match.group(1) if match else ""

def _budget(text):
    match = _BUDGET.search(text)
    if match is None: return None
    low = int(match.group(1).replace(",", ""))
    return (low, int(match.group(2).replace(",", "")) if match.group(2) else low)

def _priced(listing, text):
    """The best tier this job's stated budget can pay for, or the lowest tier when it states none.

    Measured 2026-09-07: 「固定報酬の提示がありません」 was the single largest rejection reason --
    306 against 70 for a budget that was genuinely too small -- and it was being read as "cannot
    pay". A posting with no fixed price is asking for a quote. Sampling ten of them found
    「【長期・フルリモート】AIを活用したWebエンジニア募集｜WordPress・PHP・既存システム改修」 among
    them, which is the catalogue's own work, dropped without anyone looking at it.

    Quoting the lowest tier is the honest answer to a request for a quote. The postings that
    should not be bid on -- 500円 monitors, テレアポ, 求人代行 -- are refused by the work_fit
    judge a few lines below on what they are, which is a better reason than a missing number.
    """
    budget = _budget(text)
    if budget is None:
        return listing["tiers"][0]
    affordable = [tier for tier in listing["tiers"] if tier["price_jpy"] <= budget[1]]
    return affordable[-1] if affordable else None

def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module); return module

account = _module("crowdworks_account", Path(__file__).with_name("account.py"))
profile = _module("crowdworks_profile", Path(__file__).with_name("profile.py"))
application = _module("crowdworks_application", Path(__file__).with_name("application_tick.py"))
work_fit = _module("marketplace_work_fit",
                   Path(__file__).resolve().parents[3] / "_shared" / "marketplace-core" / "scripts" / "work_fit.py")
listing_catalog = _module("marketplace_listing_catalog",
                          Path(__file__).resolve().parents[3] / "_shared" / "marketplace-core" / "scripts" / "listing_catalog.py")

def _applied():
    """Projects this account already applied to. Without it the search keeps returning its own
    best match and every tick ends duplicate_project instead of reaching the next open job."""
    done = set()
    try: lines = LEDGER.read_text(encoding="utf-8").splitlines()
    except OSError: return done
    for line in lines:
        try: record = json.loads(line)
        except ValueError: continue
        identity = record.get("opportunity_external_id")
        if isinstance(identity, str): done.add(identity)
    return done

DECLINED_PER_WAKE = 3

def _decline(declined, job_id, title, reason):
    # The same posting is returned by more than one catalogue search, and reporting it twice in one
    # wake reads as two separate decisions.
    if len(declined) < DECLINED_PER_WAKE and not any(item["external_id"] == job_id for item in declined):
        declined.append({"external_id": job_id, "title": re.sub(r"\s+", " ", title).strip()[:200], "reason": reason})

EVIDENCE_ROOT = STATE / "work-fit-evidence"


def _work_fit_verdict(job_id, title, body):
    """`None` when the posting is workable, `(reason_code, quote)` when it is not.

    A judge that cannot run refuses. Treating an unavailable judge as approval is precisely how a
    lane keeps applying while the thing that was supposed to stop it is broken, and on Coconala
    that cost the account.
    """
    directory = EVIDENCE_ROOT / f"{job_id}-{int(time.time())}"
    try:
        verdicts = work_fit.judge(
            [{"posting_id": str(job_id), "title": title, "body": body}],
            evidence_dir=directory, loop="crowdworks-application")
    except Exception as error:
        return ("judge_unavailable", str(error)[:120])
    if str(job_id) not in verdicts:
        # Absent is not approved.
        return ("unjudged", "")
    return verdicts[str(job_id)]


def _candidate(page, listings, groups):
    """Walk a slice of the board and return the first posting a catalogue tier can serve."""
    ordered = list(groups)
    seen = _applied(); already = len(seen); # Every way out of the loop below is counted. Measured 2026-09-07: one wake reported
    # inspected=63 against counters summing to 26, so 37 postings were looked at and dropped with
    # nothing said about them -- the same anonymous refusal that cost a day on Lancers, one level
    # up. `unreadable` is a posting whose page would not load; `out_of_time` is the search budget
    # running out mid-listing, which silently truncates the board and looks like a quiet day.
    rejected = {"closed_or_unverified": 0, "off_topic": 0, "wrong_category": 0, "budget": 0, "not_workable": 0, "judge_unavailable": 0, "unreadable": 0, "out_of_time": 0}
    # Postings we looked at seriously and still declined. Reporting every search hit would be noise;
    # a job that matched the listing and was then declined is a decision worth telling Dais about.
    declined = []
    deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
    for listing in ordered:
        if time.monotonic() > deadline:
            rejected["out_of_time"] += len(ordered) - ordered.index(listing)
            break
        page.goto(f"https://crowdworks.jp/public/jobs/group/{quote(listing)}?hide_expired=true");account._wait(page);page.wait_for_timeout(3000)
        # Result titles only. A bare a[href*="/public/jobs/"] also returns the category sidebar and
        # the recommendation rail: 227 links for a 20-result search, nearly all unrelated.
        links=page.locator('h3 a[href*="/public/jobs/"]').evaluate_all("els => els.map(e => ({href:e.getAttribute('href') || '',title:(e.innerText || '').trim()}))")
        for link in links:
            match=re.search(r"/public/jobs/([0-9]+)(?:[?#]|$)",link.get("href","") if isinstance(link,dict) else "")
            if match is None:continue
            job_id,title=match.group(1),link.get("title","")
            if job_id in seen:continue
            seen.add(job_id)
            # One slow posting must not cost the whole tick its remaining candidates.
            try:page.goto(f"https://crowdworks.jp/public/jobs/{job_id}");account._wait(page);text=re.sub(r"\s+"," ",page.locator("body").inner_text())
            except Exception as error:
                rejected["unreadable"]+=1
                _decline(declined,job_id,title,f"募集ページを読み込めませんでした（{type(error).__name__}）")
                continue
            # 本人確認未提出 clients are why the 2026-09-02 scout had 9 applicants and 0 contracts.
            # Half of CrowdWorks clients are 本人確認未提出, including real companies with reviews. What
            # made the 2026-09-02 scout worthless was unverified AND unproven: 0 reviews, 0 contracts.
            if "このお仕事の募集は終了しています" in text or ("本人確認未提出" in text and "0件のレビュー" in text):rejected["closed_or_unverified"]+=1;continue
            # Match the posting itself, not the sidebar and footer: whole-page matching pulled in a
            # 医療事務 job because unrelated navigation text mentioned our nouns.
            detail=text[text.find("仕事の詳細"):text.find("クライアント情報")] if "仕事の詳細" in text and "クライアント情報" in text else ""
            matched = _listing_for(listings, title, detail)
            if matched is None:rejected["off_topic"]+=1;continue
            # The 医療事務 staffing post that matched on the word AI機能 alone, and would have
            # received a 240,000円 web-app proposal, is still refused here -- by what it is rather
            # than by not being on a list of approved category names.
            refusal = work_fit.category_refusal(_category(text))
            if refusal is not None:
                rejected["wrong_category"]+=1
                _decline(declined,job_id,title,f"募集カテゴリ「{_category(text)}」は受注できない区分（{refusal[0]}）です")
                continue
            tier=_priced(matched,text)
            if tier is None:
                rejected["budget"]+=1
                budget=_budget(text)
                _decline(declined,job_id,title,f"提示予算{budget[1]:,}円が最低単価{matched['tiers'][0]['price_jpy']:,}円に届きません" if budget else "報酬額を読み取れませんでした")
                continue
            # The category label got this far; the posting text decides. Without this the lane
            # applied to 「採用支援事業のパートナー募集」 and two more like it on 2026-09-07 --
            # agency recruitment, where nothing is delivered so nothing can be delivered well.
            # Coconala had no judgement here either and the marketplace restricted the account.
            verdict = _work_fit_verdict(job_id, title, detail or text)
            if verdict is not None:
                rejected["not_workable"]+=1
                # Not `quote`: that is urllib.parse.quote, used a few lines above to build the
                # search URL, and binding it here made it local to the whole function and took
                # the lane down with UnboundLocalError on the next wake.
                reason, evidence_quote = verdict
                _decline(declined,job_id,title,f"募集文の「{evidence_quote}」が対応できない条件（{reason}）に当たります" if evidence_quote else f"対応できない条件（{reason}）に当たります")
                continue
            return {"external_id":job_id,"title":re.sub(r"\s+"," ",title).strip()},matched,tier,{"inspected":len(seen)-already,**rejected,"declined":declined}
    return None,None,None,{"inspected":len(seen)-already,**rejected,"declined":declined}

def _proposal(listing, tier):
    return "\n".join((
        "はじめまして。募集内容を拝見し、以下の範囲で対応できます。",
        listing["value_prop"],
        f"今回のご提案範囲: {tier['scope']}",
        "納品物: "+"、".join(listing["deliverables"]),
        "着手にあたり共有いただきたい情報: "+"、".join(listing["required_inputs"]),
        "ご不明点や範囲の調整があれば、ご希望に合わせて再見積りいたします。",
    ))

def _reconcile(page):
    """Import any submission whose landing page was not recognised, so a real application that
    CrowdWorks already accepted still reaches the ledger instead of being silently lost."""
    try: pending = json.loads(TRANSACTION.read_text(encoding="utf-8")).get("pending") or {}
    except Exception: return 0
    imported = 0
    for entry in pending.values():
        project_id = entry.get("project_id") if isinstance(entry, dict) else None
        if not isinstance(project_id, str): continue
        # Still pending means still unverified, whether or not the submit step managed to read an id
        # back. Skipping the entries that already carried one stranded application 304592525.
        proposal_id = entry.get("proposal_id") or application.find_proposal_id(page, project_id)
        if not isinstance(proposal_id, str): continue
        outcome = application.reconcile_existing_application(page=page, proposal_id=proposal_id, opportunity={"external_id": project_id}, state_path=TRANSACTION, ledger_writer=_append, now=lambda: datetime.now(timezone.utc).isoformat(), account_ready=lambda: True)
        imported += 1 if getattr(outcome, "application_verified", False) else 0
    return imported

def _append(receipt):
    LEDGER.parent.mkdir(parents=True,exist_ok=True)
    with LEDGER.open("a",encoding="utf-8") as handle:
        handle.write(json.dumps(receipt,ensure_ascii=False,separators=(",",":"))+"\n");handle.flush();os.fsync(handle.fileno())

def _write_status(payload):
    path=STATE/"application-owner.json";path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=".application-owner.")
    with os.fdopen(fd,"w",encoding="utf-8") as handle:json.dump(payload,handle,ensure_ascii=False,separators=(",",":"));handle.write("\n");handle.flush();os.fsync(handle.fileno())
    os.chmod(tmp,0o600);os.replace(tmp,path)

def main():
    now=datetime.now(timezone.utc)
    if not account._owner():result={"ok":False,"status":"browser_unavailable","effect_delta":0}
    else:
        ensured=account.run_ensure(state_path=account.DEFAULT_STATE_PATH,allow_signup=False,ownership_checker=account._owner,browser_factory=account._browser,vault_restorer=account._restore,vault_dumper=account._dump,credential_loader=account._credentials,notifier=account._notify,now=lambda:datetime.now(timezone.utc).isoformat())
        if not ensured.authenticated:result={"ok":False,"status":ensured.error or ensured.status,"effect_delta":0}
        else:
            browser=account._browser(account.CDP_URL);page=browser.contexts[0].new_page()
            try:
                configured=profile.run_apply(page=page,receipt_path=STATE/"profile-receipt.json")
                imported=_reconcile(page) if configured.get("ok") else 0
                if not configured.get("ok"):
                    result={"ok":False,"status":configured.get("error","profile_incomplete"),"effect_delta":0}
                elif (candidate_result:=_candidate(page,_listings(),_groups(now)))[0] is None:
                    result={"ok":True,"status":"profile_complete_no_eligible_open_job","imported_applications":imported,"inspected_jobs":candidate_result[3],"effect_delta":0}
                else:
                    candidate,listing,tier,_inspected=candidate_result
                    due=(date.today()+timedelta(days=int(tier.get("delivery_days",7)))).isoformat()
                    tick=application.execute_application(page=page,opportunity=candidate,proposal_text=_proposal(listing,tier),proposed_amount_minor=tier["price_jpy"],delivery_due_on=due,expire_period_days=7,state_path=TRANSACTION,ledger_writer=_append,now=lambda:datetime.now(timezone.utc).isoformat(),account_ready=lambda:True)
                    result={**tick.to_dict(),"status":"verified" if tick.application_verified else tick.error or tick.reason,"effect_delta":1 if tick.submitted else 0}
            finally:
                page.close()
    # Reporting is a separate owner (crowdworks-revenue-report). Apply owns submissions only, so a
    # failed or slow report can never hold up an application, and vice versa.
    result["observed_at"]=now.isoformat();_write_status(result);print(json.dumps(result,ensure_ascii=False,separators=(",",":")));return 0 if result.get("ok") else 1

if __name__=="__main__":raise SystemExit(main())
