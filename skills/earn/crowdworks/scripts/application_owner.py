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
# 固定報酬制 10,000円 〜 30,000円 / 固定報酬制 50,000円
_BUDGET = re.compile(r"固定報酬制\s*([\d,]+)\s*円(?:\s*〜\s*([\d,]+)\s*円)?")

def _listings():
    """The shared 3-platform catalog, with CrowdWorks search terms derived from each title."""
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    out = []
    for item in data["listings"]:
        title = re.sub(r"(します|承ります)$", "", item["title_ja"])
        # Keyword search matches nouns, not whole sentences: 「業務自動化システムを開発」finds nothing
        # while 「業務自動化」 returns a live board, so cut each part down to its noun phrase.
        parts = (re.sub(r"^[0-9０-９→\-〜~]+で", "", part).split("を")[0].strip() for part in re.split(r"[・/／]", title))
        terms = [part for part in parts if len(part) >= 3]
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
    """The best tier that fits this job's stated fixed-price budget, or None if it cannot pay for us."""
    budget = _budget(text)
    if budget is None: return None
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

def _candidate(page, listings, rotation):
    """Search the catalog's own terms and return the first job a catalog tier can actually serve."""
    # Rotation decides where to start, not where to stop: capping at a handful of listings meant a
    # day whose slice happened to be quiet reported no work while other listings had live jobs.
    ordered = listings[rotation:] + listings[:rotation]
    seen = _applied(); already = len(seen); rejected = {"closed_or_unverified": 0, "off_topic": 0, "wrong_category": 0, "budget": 0}
    # Postings we looked at seriously and still declined. Reporting every search hit would be noise;
    # a job that matched the listing and was then declined is a decision worth telling Dais about.
    declined = []
    deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
    for listing in ordered:
        if time.monotonic() > deadline: break
        page.goto("https://crowdworks.jp/public/jobs/search?hide_expired=true&search%5Bkeywords%5D="+quote(listing["terms"][0]));account._wait(page);page.wait_for_timeout(3000)
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
            except Exception:continue
            # 本人確認未提出 clients are why the 2026-09-02 scout had 9 applicants and 0 contracts.
            # Half of CrowdWorks clients are 本人確認未提出, including real companies with reviews. What
            # made the 2026-09-02 scout worthless was unverified AND unproven: 0 reviews, 0 contracts.
            if "このお仕事の募集は終了しています" in text or ("本人確認未提出" in text and "0件のレビュー" in text):rejected["closed_or_unverified"]+=1;continue
            # Match the posting itself, not the sidebar and footer: whole-page matching pulled in a
            # 医療事務 job because unrelated navigation text mentioned our nouns.
            detail=text[text.find("仕事の詳細"):text.find("クライアント情報")] if "仕事の詳細" in text and "クライアント情報" in text else ""
            if not any(term in title or term in detail for term in listing["terms"]):rejected["off_topic"]+=1;continue
            # The 医療事務 staffing post that matched on the word AI機能 alone, and would have
            # received a 240,000円 web-app proposal, is still refused here -- by what it is rather
            # than by not being on a list of approved category names.
            refusal = work_fit.category_refusal(_category(text))
            if refusal is not None:
                rejected["wrong_category"]+=1
                _decline(declined,job_id,title,f"募集カテゴリ「{_category(text)}」は受注できない区分（{refusal[0]}）です")
                continue
            tier=_priced(listing,text)
            if tier is None:
                rejected["budget"]+=1
                budget=_budget(text)
                _decline(declined,job_id,title,f"提示予算{budget[1]:,}円が最低単価{listing['tiers'][0]['price_jpy']:,}円に届きません" if budget else "固定報酬の提示がありません")
                continue
            return {"external_id":job_id,"title":re.sub(r"\s+"," ",title).strip()},listing,tier,{"inspected":len(seen)-already,**rejected,"declined":declined}
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
            configured=profile.run_apply(page=page,receipt_path=STATE/"profile-receipt.json")
            imported=_reconcile(page) if configured.get("ok") else 0
            if not configured.get("ok"):
                result={"ok":False,"status":configured.get("error","profile_incomplete"),"effect_delta":0}
            elif (candidate_result:=_candidate(page,_listings(),now.timetuple().tm_yday%max(1,len(_listings()))))[0] is None:
                result={"ok":True,"status":"profile_complete_no_eligible_open_job","imported_applications":imported,"inspected_jobs":candidate_result[3],"effect_delta":0}
            else:
                candidate,listing,tier,_inspected=candidate_result
                due=(date.today()+timedelta(days=int(tier.get("delivery_days",7)))).isoformat()
                tick=application.execute_application(page=page,opportunity=candidate,proposal_text=_proposal(listing,tier),proposed_amount_minor=tier["price_jpy"],delivery_due_on=due,expire_period_days=7,state_path=TRANSACTION,ledger_writer=_append,now=lambda:datetime.now(timezone.utc).isoformat(),account_ready=lambda:True)
                result={**tick.to_dict(),"status":"verified" if tick.application_verified else tick.error or tick.reason,"effect_delta":1 if tick.submitted else 0}
            page.close()
    # Reporting is a separate owner (crowdworks-revenue-report). Apply owns submissions only, so a
    # failed or slow report can never hold up an application, and vice versa.
    result["observed_at"]=now.isoformat();_write_status(result);print(json.dumps(result,ensure_ascii=False,separators=(",",":")));return 0 if result.get("ok") else 1

if __name__=="__main__":raise SystemExit(main())
