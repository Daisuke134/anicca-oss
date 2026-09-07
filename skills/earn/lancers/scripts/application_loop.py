#!/usr/bin/env python3
"""Plan every visible Lancers opportunity and submit every eligible one."""
from __future__ import annotations

import argparse, inspect, json, os, re, shutil, subprocess, sys, tempfile, time, uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO

HERE = Path(__file__).resolve().parent
SKILLS_ROOT = HERE.parents[2]
SKILLS = SKILLS_ROOT
REPO = SKILLS_ROOT.parent
STATUS_PATH = HERE / "status.py"
APPLICATION_TICK_PATH = HERE / "application_tick.py"
AGENT_RUNNER = REPO / "runtime" / "agent-runner" / "agent_runner.py"
AGENT_RUNNER_PATH = AGENT_RUNNER
PLANNER_SCHEMA = SKILLS_ROOT / "gig-work" / "schemas" / "application_decisions.schema.json"
SCHEMA_PATH = PLANNER_SCHEMA
PRODUCT_PATH = HERE.parent / "products" / "monthly-sns-content-ops-v1.json"
PLATFORM = "lancers"
MAX_OPPORTUNITIES = 20
DEFAULT_DISCOVERY_QUERY = "SNS運用"
# Search for what the catalogue actually sells. Measured 2026-09-07: half of the previous list was
# SNS and content marketing ("SNS運用", "SNS投稿", "コンテンツ制作", "X運用", "B2Bマーケティング"),
# and one query runs per pass, so the board the lane saw was short-video editing, on-site filming and
# Threads management. It then declined all of it -- 17 mandatory_attribute_fabrication,
# 14 mandatory_human_presence, 14 video_or_animation in the last 60 decisions -- and applied to
# nothing. The lane was fetching work it is honest enough to refuse.
#
# All 20 catalogue listings are system and automation build work, so every term below is a noun
# phrase taken from one of their titles. Noun phrases, not titles: the recipe records that
# "業務自動化システムを開発" finds nothing while "業務自動化" returns a live board.
# test_lancers_queries_match_the_catalogue keeps them tied to what we sell.
DISCOVERY_QUERIES = (
    "業務自動化", "業務システム", "Webアプリ", "システム開発",
    "LINE Bot", "スクレイピング", "Excel VBA", "ダッシュボード",
    "Chrome拡張", "RPA", "ECサイト", "不具合修正",
)
PUBLIC_SOFTWARE_PROOF = {
    "source_url": "https://github.com/Daisuke134/life-manager", "title": "Life Manager", "license": "MIT",
    "description": "API、scheduler、worker、Postgres、object store、Telegram reporting、公式readback付き外部action loopを同一repositoryで実装したMIT公開のpersonal managerです。",
}
PLANNER_TASK_CLASS = "application-intent-planner"
ESCALATION_REASON = "application decision and client-facing proposal text come from this single call"
PLANNER_TIMEOUT_SECONDS = 420
DEFAULT_STATE_PATH = Path.home() / ".local/state/anicca/lancers/application.json"
DEFAULT_EVIDENCE_ROOT = Path.home() / ".local/state/anicca/lancers/planner"
DEFAULT_EVIDENCE_DIR = DEFAULT_EVIDENCE_ROOT
DECISION_FIELDS = frozenset({"request_id", "business_class", "reason_codes", "proposal_text", "price_jpy", "deliver_date"})
BUSINESS_CLASSES = frozenset({"submit_required", "hard_prohibited"})
def _persona_facts() -> dict[str, str]:
    """The attributes an application form can ask about, from the private profile SSOT.

    Without these the planner cannot tell a requirement it satisfies from one it would have to lie
    about, and defaults to refusing. Only what a posting may ask -- age, residence, citizenship --
    is read; the name and history stay out of the prompt.
    """
    fallback = {"age": "24", "base": "東京 (日本)", "citizenship": "日本"}
    try:
        data = json.loads(Path("~/.config/anicca/job-search/profile.json").expanduser()
                          .read_text(encoding="utf-8"))
        candidate = data.get("candidate") or {}
        born = date.fromisoformat(str(candidate["date_of_birth"]))
        today = date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        citizenships = candidate.get("citizenships") or []
        return {
            "age": str(age),
            "base": str(candidate.get("base") or fallback["base"]),
            "citizenship": "・".join(str(value) for value in citizenships) or fallback["citizenship"],
        }
    except Exception:
        return fallback


PERSONA_FACTS = _persona_facts()
def _work_fit():
    """The refusals are shared with Coconala and CrowdWorks. Lancers wrote them first; keeping a
    second copy here is how CrowdWorks ended up refusing work this list allows."""
    path = Path(__file__).resolve().parents[3] / "_shared" / "marketplace-core" / "scripts" / "work_fit.py"
    spec = importlib.util.spec_from_file_location("marketplace_work_fit", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARD_PROHIBITION_CLASSES = _work_fit().HARD_PROHIBITION_CLASSES
PUBLIC_FIELDS = ("schema_version", "record_type", "platform", "external_id", "title", "description", "url", "category", "budget_type", "budget_min_minor", "budget_max_minor", "currency", "buyer_external_id", "observed_at")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ID_RE = re.compile(r"^[0-9]+$")
KANA_RE = re.compile(r"[ぁ-ゖァ-ヺ]")
FORBIDDEN_TERMS = ("receipt", "gate", "agent", "model", "browser", "token", "prompt", "internal id", "レシート", "ゲート", "エージェント", "モデル", "ブラウザ", "トークン", "プロンプト", "内部ID")
FORBIDDEN_RE = re.compile("|".join(re.escape(term).replace(r"\ ", r"[ _]") for term in FORBIDDEN_TERMS), re.IGNORECASE)
RETAIN_EVIDENCE_ERRORS = frozenset({"planner_runner_failed", "planner_contract_invalid"})
SKIP_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
SKIP_CACHE_VERSION = 2

def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None: raise RuntimeError("dependency_unavailable")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return module

status = _load("_anicca_lancers_application_loop_status", STATUS_PATH)
application_tick = _load("_anicca_lancers_application_loop_tick", APPLICATION_TICK_PATH)

@dataclass(frozen=True)
class ApplicationLoopResult:
    ok: bool
    submitted: bool = False
    application_verified: bool = False
    reason: Optional[str] = None
    error: Optional[str] = None
    project_id: Optional[str] = None
    provider_proposal_id: Optional[str] = None
    cleanup_error: Optional[str] = None
    observed_count: Optional[int] = None
    already_decided_count: Optional[int] = None
    eligible_count: Optional[int] = None
    verified_count: Optional[int] = None
    provider_terminal_blocked_count: Optional[int] = None
    verified_project_ids: Optional[tuple[str, ...]] = None
    verified_provider_proposal_ids: Optional[tuple[str, ...]] = None
    provider_terminal_blocked_project_ids: Optional[tuple[str, ...]] = None
    unresolved_project_id: Optional[str] = None
    planner_expected_count: Optional[int] = None
    planner_returned_count: Optional[int] = None
    decision_reports: Optional[tuple[Mapping[str, object], ...]] = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"ok": bool(self.ok), "platform": PLATFORM, "submitted": bool(self.submitted), "application_verified": bool(self.application_verified)}
        for key in ("reason", "error", "project_id", "provider_proposal_id", "cleanup_error"):
            value = getattr(self, key)
            if value is not None: result[key] = value
        for key in ("observed_count", "already_decided_count", "eligible_count", "verified_count", "provider_terminal_blocked_count"):
            value = getattr(self, key)
            result[key] = int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
        for key in ("planner_expected_count", "planner_returned_count"):
            value = getattr(self, key)
            if isinstance(value, int) and not isinstance(value, bool): result[key] = value
        for key in ("verified_project_ids", "verified_provider_proposal_ids", "provider_terminal_blocked_project_ids"):
            value = getattr(self, key)
            if value: result[key] = list(value)
        if self.decision_reports: result["decision_reports"] = [dict(value) for value in self.decision_reports]
        if self.unresolved_project_id is not None: result["unresolved_project_id"] = self.unresolved_project_id
        return result

def _tick_date(value: object) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None: raise ValueError
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date): return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).date() if parsed.tzinfo else date.fromisoformat(value.strip())
        except (TypeError, ValueError, OverflowError): pass
    raise ValueError

def _discovery_query(value: object) -> str:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return DEFAULT_DISCOVERY_QUERY
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return DEFAULT_DISCOVERY_QUERY
        slot = int(parsed.astimezone(timezone.utc).timestamp() // 1800) % len(DISCOVERY_QUERIES)
        return DISCOVERY_QUERIES[slot]
    except (TypeError, ValueError, OverflowError, OSError):
        return DEFAULT_DISCOVERY_QUERY

def _observed_after(candidate: Mapping[str, object], incumbent: Mapping[str, object]) -> bool:
    """True when candidate carries a strictly later `observed_at` than incumbent."""
    def parsed(row: Mapping[str, object]) -> Optional[datetime]:
        value = row.get("observed_at")
        if not isinstance(value, str):
            return None
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        return moment if moment.tzinfo is not None else None
    later, earlier = parsed(candidate), parsed(incumbent)
    return later is not None and earlier is not None and later > earlier

def _run_exhaustive_discovery(timeout: float) -> Mapping[str, object]:
    """Union every query so one wake can see the whole reachable board, not one keyword of it.

    The default path deliberately stops at the first query that still has unclaimed rows, which
    keeps a 20s tick cheap but means the other queries' candidates are never even considered --
    'apply to every eligible candidate' cannot be true under it. Coconala solves the same problem
    with a separate exhaustive lane on its own hour-long budget rather than by widening the fast
    tick, so this is opt-in and the caller supplies the per-request timeout (the provider caps a
    single request at 60s; the exhaustive budget is that times the number of queries).

    Known limits, both inherited from the default path and both silent today:
    a query whose cards all fail to normalize reports `no_normalized_opportunities`, which is
    indistinguishable here from an empty board, so that query contributes nothing without a flag;
    and only a provider error that is not `no_normalized_opportunities` aborts the union. When two
    queries return the same `external_id` the later `observed_at` wins, so a duplicate cannot pin
    a stale copy; rows whose timestamps are missing or naive keep the incumbent.
    """
    merged: dict[str, Mapping[str, object]] = {}
    last: Mapping[str, object] = {"ok": False, "error": "no_normalized_opportunities", "opportunities": []}
    seen_ok = False
    for query in DISCOVERY_QUERIES:
        last = status.run_discovery(query=query, limit=MAX_OPPORTUNITIES, timeout=timeout)
        if last.get("ok") is not True:
            if last.get("error") != "no_normalized_opportunities":
                return last
            continue
        seen_ok = True
        opportunities = last.get("opportunities")
        if not isinstance(opportunities, Sequence) or isinstance(opportunities, (str, bytes, bytearray)):
            return last
        for row in opportunities:
            if not isinstance(row, Mapping):
                return last
            external_id = row.get("external_id")
            if not isinstance(external_id, str) or not external_id:
                continue
            seen = merged.get(external_id)
            # The same listing surfaces under several queries. Keeping whichever query ran first
            # would pin a stale copy of a row that another query observed later, so prefer the
            # later observation and fall back to the incumbent when the timestamps are unusable.
            if seen is None or _observed_after(row, seen):
                merged[external_id] = row
    if not seen_ok:
        return last
    union = {key: value for key, value in last.items() if key != "error"}
    union["ok"] = True
    union["opportunities"] = list(merged.values())
    return union

def _run_default_discovery(tick_value: object, timeout: float, state_path: Path, exclude_ids: frozenset[str] = frozenset()) -> Mapping[str, object]:
    first = _discovery_query(tick_value)
    start = DISCOVERY_QUERIES.index(first)
    last: Mapping[str, object] = {"ok": False, "error": "no_normalized_opportunities", "opportunities": []}
    observed_ids: set[str] = set()
    decided_ids: set[str] = set()
    for offset in range(len(DISCOVERY_QUERIES)):
        query = DISCOVERY_QUERIES[(start + offset) % len(DISCOVERY_QUERIES)]
        last = status.run_discovery(query=query, limit=MAX_OPPORTUNITIES, timeout=timeout)
        if last.get("ok") is True:
            opportunities = last.get("opportunities")
            if isinstance(opportunities, Sequence) and not isinstance(opportunities, (str, bytes, bytearray)):
                observed_ids.update(str(row.get("external_id")) for row in opportunities if isinstance(row, Mapping) and row.get("external_id"))
                try: remaining, _, _ = _filter_claimed_rows(opportunities, state_path)
                except Exception: return last
                remaining_ids = {str(row.get("external_id")) for row in remaining if isinstance(row, Mapping)}
                decided_ids.update(str(row.get("external_id")) for row in opportunities if isinstance(row, Mapping) and str(row.get("external_id")) not in remaining_ids)
                remaining = [row for row in remaining if str(row.get("external_id")) not in exclude_ids]
                if not remaining: continue
                return dict(last) | {"observed_count": len(observed_ids), "already_decided_count": len(decided_ids)}
            return dict(last) | {"observed_count": len(observed_ids), "already_decided_count": len(decided_ids)}
        if last.get("error") != "no_normalized_opportunities":
            return last
    return {"ok": True, "platform": PLATFORM, "source": "public_html", "opportunities": [], "observed_count": len(observed_ids), "already_decided_count": len(decided_ids)}

def _seller_proof() -> dict[str, object]:
    product = json.loads(PRODUCT_PATH.read_text(encoding="utf-8")); portfolio = product["portfolio"]; software_portfolio = product["software_portfolio"]; software = PUBLIC_SOFTWARE_PROOF
    ids = (product["listing_external_id"], portfolio["external_id"], software_portfolio["external_id"])
    strings = (product["title_stem"], product["description"], product["notice"], portfolio["title_stem"], portfolio["description"], software_portfolio["title_stem"], software_portfolio["description"])
    if not isinstance(software, Mapping) or set(software) != {"source_url", "title", "description", "license"} or software.get("source_url") != "https://github.com/Daisuke134/life-manager" or any(not isinstance(software.get(key), str) or not software[key].strip() for key in ("title", "description", "license")): raise ValueError
    if any(not isinstance(value, str) or not value.strip() for value in ids + strings) or any(ID_RE.fullmatch(value) is None for value in ids): raise ValueError
    plans = [{key: plan[key] for key in ("description", "delivery_days", "price_jpy")} for plan in product["plans"]]
    return {
        "profile_url": "https://www.lancers.jp/profile/keiodaisuke",
        "portfolio_id": ids[1], "portfolio_url": f"https://www.lancers.jp/profile/keiodaisuke/portfolio_popup/{ids[1]}",
        "portfolio_title": portfolio["title_stem"] + "ました", "portfolio_description": portfolio["description"],
        "software_portfolio_id": ids[2], "software_portfolio_url": f"https://www.lancers.jp/profile/keiodaisuke/portfolio_popup/{ids[2]}",
        "software_portfolio_title": software_portfolio["title_stem"] + "ました", "software_portfolio_description": software_portfolio["description"],
        "package_id": ids[0], "package_url": f"https://www.lancers.jp/menu/detail/{ids[0]}",
        "package_title": product["title_stem"] + "ます", "package_scope": product["description"], "package_exclusions": product["notice"], "plans": plans,
        "public_software_proof": dict(software),
    }

def _snapshot(rows: Sequence[Mapping[str, object]], today: date) -> dict[str, object]:
    if len(rows) > MAX_OPPORTUNITIES: raise ValueError
    result, ids = [], set()
    for row in rows:
        project_id = row.get("external_id") if isinstance(row, Mapping) else None
        if not isinstance(project_id, str) or ID_RE.fullmatch(project_id) is None or project_id in ids: raise ValueError
        ids.add(project_id); compact = {key: row.get(key) for key in PUBLIC_FIELDS}
        for key, maximum in (("title", 200), ("category", 120), ("description", 2000)):
            if isinstance(compact[key], str): compact[key] = compact[key][:maximum]
        result.append(compact)
    return {"tick_date": today.isoformat(), "seller_proof": _seller_proof(), "opportunities": result}

PLANNER_RULES = ("Lancersの公開案件だけを読むapplication-intent plannerである。planner自身は外部操作を行わないが、delivery systemは既存の認証済みbrowserと利用可能なtoolsで外部web serviceを操作できる。"
    "Coconalaと同じcommon marketplace feasibility policyを使う。合法で、現在のgeneral agentがcomputer、browser、code、research、writing、design、dataその他の利用可能なtoolsを使って全必須成果物を正直に完遂できる案件は広く応募する。判定はsubmit_requiredまたはhard_prohibitedの二択であり、前者を既定とする。"
    "確認済みのdelivery能力は、非同期のresearch、文章作成・編集・翻訳、digital content設計、code・software・data・AI automation、web/browser上の操作・調査・monitor、必要なtoolの導入・設定・利用、利用可能なtoolで生成できるdigital artifactである。現在未導入のsoftwareや未利用のweb serviceでも、公開または依頼者提供の通常accessで導入・利用・検証できるなら完遂可能として扱う。未提示の個人職歴、雇用経験、資格、電話営業、常駐staff稼働、専用hardwareや入手不能な外部credentialを能力として仮定しない。"
    "SNAPSHOTのseller_proofは現在のLancers公開profile、portfolio、packageとMIT公開source codeで買い手が確認できる証拠であり、能力の固定whitelistではない。案件scopeに合う証拠だけを具体的に活用し、未掲載の顧客実績、評価、売上効果、専門職歴を捏造しない。exactな同業実績や完成済みportfolioがなくても、転用可能な確認済み能力と案件固有の実行planで全必須scopeを完遂できるならsubmit_requiredにする。"
    "各案件を実際の公開内容全体から自分で判断し、指定schemaのJSONだけを返す。現在の自律delivery systemが全必須成果物を正直に完成できるならsubmit_requiredとする。専用Skill、同業職歴、実績、portfolio、testimonial、tool利用歴、資格を必要としない経験年数、難易度、競争、単発、曖昧な通常実装詳細は拒否理由にしない。未経験を経験済みと偽らず、転用可能な確認済み能力と案件固有の実行planを示す。reason_codesは空、買い手向けの具体的な日本語proposalを200〜3000文字、正直な価格、現実的な納期で返す。"
    "proposalは自己紹介だけで始めず、冒頭で依頼内容の理解と提供価値を案件固有に示す。依頼文の応募質問へ漏れなく直接答え、実行手順・schedule・納品物を明記し、案件に関係する場合だけ修正回数とLancersメッセージでの連絡方法を示す。検証済みでない実績は作らない。"
    "hard_prohibitedは案件全体が次のいずれかを必須とする場合だけ使う: "
    + "; ".join(f"{key}={value}" for key, value in HARD_PROHIBITION_CLASSES.items()) + "。"
    "hard_prohibitedではreason_codes[0]を正確なclass key、reason_codes[1]をtitle・description・categoryのいずれかに連続して存在する200文字以内の原文引用にし、proposal・price・dateはnullにする。任意・推奨・否定・引用中の単語だけで拒否しない。"
    # Measured 2026-09-07: every attribute requirement was read as fabrication because the planner
    # was asked to judge personal attributes without being told any. 「・年齢：」-- a form field --
    # and 「・20歳以上の方」, which the persona satisfies at 24, were both refused. The class is
    # correct; it was being applied to requirements that can be answered honestly.
    f"応募者の確認済み属性: 年齢{PERSONA_FACTS['age']}歳、居住地{PERSONA_FACTS['base']}、国籍{PERSONA_FACTS['citizenship']}、日本語ネイティブ。"
    "mandatory_attribute_fabricationはこの確認済み属性と矛盾する必須要件だけに使う。属性を尋ねる入力欄の存在や、確認済み属性が満たす年齢・居住地・国籍・言語の条件は、正直に答えられるので拒否理由にしない。"
    "reason_codes[1]は公開原文から一文字も足さずcopyし、長い引用に自信がなければ判断根拠を直接示す短い連続原文を使う。"
    "案件全体から納品可能性をpriorityより先に確定する。完成動画そのものの生成・編集・書き出しが必須ならvideo_or_animation、企画・構成・台本・文章だけで完成動画制作が不要ならvideo_or_animationではない。機械的なkeyword ruleは使わない。"
    "経験の不確実さ、弱いportfolio、低予算、難易度、広いまたは曖昧なscope、単発、継続性不足、Adobe実績不明、任意の相談を単独のkeyword ruleでskipしない。正確な同分野実績がなくても、確認済みの転用可能な能力で全必須scopeを完遂できるなら案件固有の実行planで応募し、未作成物はplanと明示して捏造しない。"
    "納品可能性を確定した後の優先順は、定期購入・保守・運用、次にsystem・automation・AI・web・高報酬、次にその他の非同期作業。hard prohibition必須案件を継続・AI・高報酬・低予算・簡単そうという理由でsubmit_requiredへ変えない。実行可能な低優先案件を省略しない。submit_requiredを先に並べ、強い順に返す。"
    "既知のbudget_max_minorを超えず、依頼本文にproviderの広い予算帯より狭い具体予算があれば本文の上限を優先する。budgetが応相談・未定でも拒否しない。一律の最低価格や固定上限を設けない。scopeと正のmarginを守りながら競合より少し安い価格と、実行可能な最短納期を選ぶ。"
    "live call・video meeting・顔出し・音声収録を自発的に約束せず、任意ならLancersメッセージと文書による非同期確認を提案する。必須ならhard_prohibitedにする。"
    "提案文には次の語を含めない: " + ", ".join(FORBIDDEN_TERMS) + "。送信・受注・納品・支払済みと主張しない。\nSNAPSHOT:\n")

def build_planner_prompt(rows: Sequence[Mapping[str, object]], today: date) -> str:
    return PLANNER_RULES + json.dumps(_snapshot(rows, _tick_date(today)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _invoke_agent(prompt: str, evidence_dir: Path, task_class: str, schema_path: Path, label: str) -> Mapping[str, object]:
    command = [sys.executable, str(AGENT_RUNNER), "--task-class", task_class, "--prompt-stdin", "--schema", str(schema_path), "--evidence-dir", str(evidence_dir), "--task-label", label, "--loop", "lancers-application", "--workdir", str(SKILLS_ROOT.parent), "--escalation-reason", ESCALATION_REASON]
    try:
        # stderr is kept, not discarded. The runner refuses on configuration this loop cannot see,
        # and a refusal that reaches no log is a lane that stops applying without ever saying so.
        completed = subprocess.run(command, input=prompt, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False, timeout=PLANNER_TIMEOUT_SECONDS + 30)
        if completed.returncode != 0: raise ValueError(((completed.stderr or "").strip().splitlines() or ["no stderr"])[-1])
        evidence = Path(evidence_dir); summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
        result_path = Path(str(summary["result_path"])).resolve(); result_path.relative_to(evidence.resolve())
        if summary.get("status") != "success": raise ValueError
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, Mapping): raise ValueError
        return result
    except Exception as error: raise RuntimeError(f"agent_runner_failed: {error}") from None

def _planner_runtime_schema(prompt: str, evidence_dir: Path) -> Path:
    snapshot = json.loads(prompt.rsplit("SNAPSHOT:\n", 1)[1])
    ids = [row["external_id"] for row in snapshot["opportunities"]]
    schema = json.loads(PLANNER_SCHEMA.read_text(encoding="utf-8"))
    decisions = schema["properties"]["decisions"]
    decisions["minItems"] = len(ids)
    decisions["maxItems"] = len(ids)
    decisions["items"]["properties"]["request_id"]["enum"] = ids
    decisions["items"]["properties"]["business_class"]["enum"] = sorted(BUSINESS_CLASSES)
    path = Path(evidence_dir) / "planner-runtime.schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.chmod(path, 0o600)
    return path

def invoke_planner(prompt: str, evidence_dir: Path) -> Mapping[str, object]:
    return _invoke_agent(prompt, evidence_dir, PLANNER_TASK_CLASS, _planner_runtime_schema(prompt, evidence_dir), "lancers-application-intent")

def _default_planner(prompt: str, evidence: Path) -> Mapping[str, object]:
    return invoke_planner(prompt, evidence)

def _safe_proposal(value: object, ids: Sequence[str]) -> bool:
    if not isinstance(value, str) or not 200 <= len(value) <= 3000: return False
    if len(KANA_RE.findall(value)) < max(20, len(re.findall(r"[A-Za-z]", value)) + 1): return False
    if FORBIDDEN_RE.search(value): return False
    return not any(re.search(rf"(?<![0-9]){re.escape(project_id)}(?![0-9])", value) for project_id in ids)

def _valid_date(value: object, today: date) -> bool:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None: return False
    try: parsed = date.fromisoformat(value)
    except (TypeError, ValueError, OverflowError): return False
    return value == parsed.isoformat() and today + timedelta(days=1) <= parsed <= today + timedelta(days=60)

def _valid_observed_budget(row: object) -> bool:
    if not isinstance(row, Mapping): return False
    minimum, maximum = row.get("budget_min_minor"), row.get("budget_max_minor")
    return not any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0) for value in (minimum, maximum)) and not (minimum is not None and maximum is not None and minimum > maximum)

def _public_excerpt(excerpt: str, public_text: str) -> bool:
    return " ".join(excerpt.split()) in " ".join(public_text.split())

def _validate(rows: Sequence[Mapping[str, object]], value: object, today: date) -> dict[str, Mapping[str, object]]:
    try:
        if not isinstance(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), Mapping) or not isinstance(value, Mapping): raise ValueError
        decisions = value.get("decisions")
        if set(value) != {"decisions"} or not isinstance(decisions, list) or len(decisions) != len(rows): raise ValueError
        expected = [str(row["external_id"]) for row in rows]; rows_by_id = {str(row["external_id"]): row for row in rows}; found: dict[str, Mapping[str, object]] = {}
        for decision in decisions:
            if not isinstance(decision, Mapping) or set(decision) != DECISION_FIELDS: raise ValueError
            project_id, business_class, reasons = decision.get("request_id"), decision.get("business_class"), decision.get("reason_codes")
            if not isinstance(project_id, str) or project_id not in expected or project_id in found or business_class not in BUSINESS_CLASSES or not isinstance(reasons, list) or any(not isinstance(reason, str) or not reason.strip() for reason in reasons) or len(set(reasons)) != len(reasons): raise ValueError
            proposal, price, due = decision.get("proposal_text"), decision.get("price_jpy"), decision.get("deliver_date")
            if business_class == "hard_prohibited":
                if proposal is not None or price is not None or due is not None or len(reasons) < 2 or reasons[0] not in HARD_PROHIBITION_CLASSES: raise ValueError
                public_row = rows_by_id[project_id]
                public_text = "\n".join(str(public_row.get(key) or "") for key in ("title", "description", "category"))
                if not 1 <= len(reasons[1]) <= 200 or not _public_excerpt(reasons[1], public_text): raise ValueError
            elif reasons or not _safe_proposal(proposal, expected) or isinstance(price, bool) or not isinstance(price, int) or price < 1 or not _valid_date(due, today): raise ValueError
            found[project_id] = decision
        if set(found) != set(expected): raise ValueError
        for row in rows:
            if not _valid_observed_budget(row): raise ValueError
        for project_id, decision in found.items():
            row, price = rows_by_id[project_id], decision.get("price_jpy")
            maximum = row.get("budget_max_minor")
            if decision["business_class"] == "submit_required" and (row.get("currency") != "JPY" or (maximum is not None and price > maximum)): raise ValueError
        return found
    except Exception: raise ValueError from None

def validate_decisions(rows: Sequence[Mapping[str, object]], value: object, today: date) -> list[str]:
    try: _validate(rows, value, _tick_date(today))
    except Exception: return ["planner_failed"]
    return []

def _reset(path: Path) -> None:
    if path.is_symlink() or path.is_file(): path.unlink()
    elif path.is_dir(): shutil.rmtree(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=False); os.chmod(path, 0o700)

def _tick_result(value: object, project_id: str) -> ApplicationLoopResult:
    raw = value.to_dict() if callable(getattr(value, "to_dict", None)) else value
    if not isinstance(raw, Mapping): return ApplicationLoopResult(False, error="submission_uncertain", project_id=project_id)
    clean = lambda item: item if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", item) else None
    found = raw.get("project_id") if isinstance(raw.get("project_id"), str) and ID_RE.fullmatch(raw["project_id"]) else project_id
    reason, error = clean(raw.get("reason")), clean(raw.get("error"))
    submitted, raw_verified = raw.get("submitted") is True, raw.get("application_verified") is True
    provider_verified = raw_verified or reason in {"duplicate_project", "provider_reconciled"}
    provider_blocked = reason == "provider_terminal_blocked"
    raw_error = raw.get("error")
    raw_ok = raw.get("ok") if isinstance(raw.get("ok"), bool) else None
    provider_id = clean(raw.get("provider_proposal_id"))
    invalid = raw_error is not None or (raw_ok is False and not provider_blocked) or not (provider_verified or provider_blocked)
    if invalid:
        return ApplicationLoopResult(False, submitted, False, reason, error or "submission_uncertain", found, provider_id)
    return ApplicationLoopResult(raw_ok is not False, submitted, raw_verified, reason, None, found, provider_id)

def _emit(result: ApplicationLoopResult, stream: TextIO) -> None:
    stream.write(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"); stream.flush()

def _pending_submitter_override(*_args: object, **_kwargs: object) -> Mapping[str, object]:
    raise RuntimeError("pending_reconciliation_submitter_disabled")

def _reconcile_pending(descriptor: Mapping[str, object], state_path: Path) -> ApplicationLoopResult:
    project_id = descriptor.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return ApplicationLoopResult(False, error="state_invalid")
    try:
        value = application_tick.run_live_tick(
            project_id=project_id,
            proposal_text="pending reconciliation",
            proposed_amount_minor=descriptor["amount_minor"],
            delivery_due_on=descriptor["delivery_due_on"],
            state_path=Path(state_path),
            submitter_override=_pending_submitter_override,
        )
    except Exception:
        return ApplicationLoopResult(False, error="submission_uncertain", project_id=project_id)
    result = _tick_result(value, project_id)
    verified = (result,) if _provider_verified(result) else ()
    blocked = (project_id,) if _provider_terminal_blocked(result) else ()
    return _batch_summary(result, 1, 1, verified, blocked, unresolved_project_id=None if verified or blocked else project_id, submitted=result.submitted)

def run_reconcile_only(state_path: Path, output_stream: Optional[TextIO] = None) -> dict[str, object]:
    try:
        descriptors = application_tick.shared.read_pending_descriptors(Path(state_path))
    except Exception:
        result = {
            "ok": False,
            "platform": PLATFORM,
            "submitted": False,
            "application_verified": False,
            "reconciled_project_ids": [],
            "verified_project_ids": [],
            "unresolved_project_ids": [],
            "error": "state_invalid",
        }
        if output_stream is not None:
            output_stream.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()
        return result

    reconciled_project_ids = []
    verified_project_ids = []
    unresolved_project_ids = []
    for descriptor in descriptors:
        project_id = descriptor["project_id"]
        reconciled_project_ids.append(project_id)
        result = _reconcile_pending(descriptor, Path(state_path))
        if _provider_verified(result):
            verified_project_ids.append(project_id)
        else:
            unresolved_project_ids.append(project_id)
    summary = {
        "ok": not unresolved_project_ids,
        "platform": PLATFORM,
        "submitted": False,
        "application_verified": bool(verified_project_ids),
        "reconciled_project_ids": reconciled_project_ids,
        "verified_project_ids": verified_project_ids,
        "unresolved_project_ids": unresolved_project_ids,
    }
    if output_stream is not None:
        output_stream.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
        output_stream.flush()
    return summary

def _submit(fn: Callable[..., object], row: Mapping[str, object], proposal: str, amount: int, due: str, state_path: Path) -> object:
    values = {"project_id": str(row["external_id"]), "proposal_text": proposal, "proposed_amount_minor": amount, "delivery_due_on": due, "state_path": state_path}
    try:
        parameters = inspect.signature(fn).parameters
        keyword = any(parameter.kind == parameter.VAR_KEYWORD for parameter in parameters.values()) or {"project_id", "proposal_text", "proposed_amount_minor", "delivery_due_on"}.issubset(parameters)
    except (TypeError, ValueError): keyword = False
    return fn(**values) if keyword else fn(row, proposal, amount, due)

def _skip_cache_path(state_path: Path) -> Path:
    return Path(state_path).with_name("application-decisions.json")

def _skip_content_sha256(row: Mapping[str, object]) -> str:
    payload = {key: row.get(key) for key in PUBLIC_FIELDS if key != "observed_at"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _read_skip_cache(state_path: Path, now: Optional[float] = None) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(_skip_cache_path(state_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, Mapping) or value.get("version") != SKIP_CACHE_VERSION or not isinstance(value.get("decisions"), Mapping):
        return {}
    current = time.time() if now is None else now
    result = {}
    for project_id, row in value["decisions"].items():
        if not isinstance(project_id, str) or ID_RE.fullmatch(project_id) is None or not isinstance(row, Mapping): raise ValueError
        expires_at, content_sha256 = row.get("expires_at"), row.get("content_sha256")
        if row.get("business_class") == "hard_prohibited" and isinstance(content_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", content_sha256) and isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and expires_at > current:
            result[project_id] = dict(row)
    return result

def _write_skip_cache(state_path: Path, decisions: Mapping[str, Mapping[str, object]]) -> None:
    path = _skip_cache_path(state_path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False, encoding="utf-8") as handle:
            temporary = handle.name; os.fchmod(handle.fileno(), 0o600)
            json.dump({"version": SKIP_CACHE_VERSION, "decisions": decisions}, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
        os.replace(temporary, path); os.chmod(path, 0o600)
    finally:
        if temporary:
            try: os.unlink(temporary)
            except FileNotFoundError: pass

def _filter_claimed_rows(rows: Sequence[Mapping[str, object]], state_path: Path) -> tuple[list[Mapping[str, object]], Optional[str], list[dict[str, str]]]:
    # ponytail: a malformed row (bad budget shape) is skipped individually instead of
    # discarding every other row observed this tick alongside it.
    valid_rows = [row for row in rows if _valid_observed_budget(row)]
    skipped = [{"project_id": str(row.get("external_id")) if isinstance(row, Mapping) and isinstance(row.get("external_id"), str) else "unknown", "reason": "invalid_observed_budget"} for row in rows if not _valid_observed_budget(row)]
    skipped.extend({
        "project_id": str(row.get("external_id")),
        "reason": "unsupported_application_workflow",
    } for row in valid_rows if row.get("budget_type") == "bounty")
    good_rows = [row for row in valid_rows if row.get("budget_type") != "bounty"]
    ids = [row.get("external_id") if isinstance(row, Mapping) else None for row in good_rows]
    duplicate_ids = {project_id for project_id in ids if isinstance(project_id, str) and ids.count(project_id) > 1}
    skip_cache = _read_skip_cache(state_path)
    remaining, first_claimed = [], None
    for row, project_id in zip(good_rows, ids):
        cached = skip_cache.get(project_id) if isinstance(project_id, str) else None
        cache_matches = isinstance(cached, Mapping) and cached.get("content_sha256") == _skip_content_sha256(row)
        if not isinstance(project_id, str) or ID_RE.fullmatch(project_id) is None or project_id in duplicate_ids or (not application_tick.state_has_claim(Path(state_path), project_id) and not cache_matches):
            remaining.append(row)
        elif first_claimed is None:
            first_claimed = project_id
    return remaining, first_claimed, skipped

def _cache_no_effect(decisions: Mapping[str, Mapping[str, object]], rows: Mapping[str, Mapping[str, object]], state_path: Path) -> None:
    cached = _read_skip_cache(state_path); expires_at = time.time() + SKIP_CACHE_TTL_SECONDS
    for project_id, decision in decisions.items():
        if decision.get("business_class") == "hard_prohibited":
            cached[project_id] = {"business_class": "hard_prohibited", "content_sha256": _skip_content_sha256(rows[project_id]), "expires_at": expires_at}
    _write_skip_cache(state_path, cached)

def _capacity_reason(state_path: Path, tick_value: object) -> Optional[str]:
    try:
        now = tick_value if isinstance(tick_value, datetime) else datetime.fromisoformat(str(tick_value).replace("Z", "+00:00"))
        if now.tzinfo is None or now.utcoffset() is None: raise ValueError
        snapshot = json.loads(Path(state_path).with_name("contracts.json").read_text(encoding="utf-8"))
        observed = datetime.fromisoformat(str(snapshot["observed_at"]).replace("Z", "+00:00"))
        keys = ("project_working_count", "monthly_contract_count", "storefront_contract_candidate_count")
        counts = [snapshot[key] for key in keys]
        if snapshot.get("source_complete") is not True or observed.tzinfo is None or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts) or snapshot.get("contract_candidate_count") != sum(counts): raise ValueError
        age = now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)
        if age < timedelta(minutes=-1): raise ValueError
        if sum(counts): return "capacity_details_required"
        return None
    except Exception:
        return "capacity_source_unavailable"

def _provider_verified(result: ApplicationLoopResult) -> bool:
    return result.error is None and result.ok and (result.application_verified or result.reason in {"duplicate_project", "provider_reconciled"})

def _provider_terminal_blocked(result: ApplicationLoopResult) -> bool:
    return result.error == "provider_terminal_blocked" or (result.error is None and result.reason == "provider_terminal_blocked")

def _batch_summary(
    result: ApplicationLoopResult, observed_count: int, eligible_count: int,
    verified: Sequence[ApplicationLoopResult], blocked: Sequence[str],
    *, unresolved_project_id: Optional[str] = None, ok: Optional[bool] = None,
    submitted: Optional[bool] = None,
) -> ApplicationLoopResult:
    verified_projects = tuple(item.project_id for item in verified if item.project_id is not None)
    verified_proposals = tuple(item.provider_proposal_id for item in verified if item.provider_proposal_id is not None)
    success = result.ok if ok is None else ok
    return replace(result, ok=success, submitted=any(item.submitted for item in verified) if submitted is None else submitted, observed_count=observed_count, eligible_count=eligible_count, verified_count=len(verified), provider_terminal_blocked_count=len(blocked), verified_project_ids=verified_projects, verified_provider_proposal_ids=verified_proposals, provider_terminal_blocked_project_ids=tuple(blocked), unresolved_project_id=unresolved_project_id)

def _rank_eligible_by_buyer_quality(eligible: Sequence[tuple[Mapping[str, object], Mapping[str, object]]]) -> list[tuple[Mapping[str, object], Mapping[str, object]]]:
    ranked = []
    for index, item in enumerate(eligible):
        row, decision = item
        maximum = row.get("budget_max_minor")
        budget = maximum if isinstance(maximum, int) and not isinstance(maximum, bool) else int(decision["price_jpy"])
        proposal_match = re.search(r"(?:^|\n)提案数: ([0-9][0-9,]*)件(?:\n|$)", str(row.get("description") or ""))
        applicants = int(proposal_match.group(1).replace(",", "")) if proposal_match else float("inf")
        ranked.append(((0 if budget >= 50000 else 1, -budget, applicants, index), item))
    return [item for _key, item in sorted(ranked, key=lambda value: value[0])]

def _plan_and_submit(rows: Sequence[Mapping[str, object]], today: date, evidence: Path, planner: Optional[Callable[..., object]], safety_verifier: Optional[Callable[..., object]], submitter: Optional[Callable[..., object]], state_path: Path) -> ApplicationLoopResult:
    observed_count = len(rows)
    pre_filter_by_id = {str(row["external_id"]): row for row in rows if isinstance(row, Mapping) and isinstance(row.get("external_id"), str)}
    try:
        rows, claimed_project_id, budget_skips = _filter_claimed_rows(rows, state_path)
        skip_reports = [{
            "project_id": item["project_id"],
            "title": str(pre_filter_by_id.get(item["project_id"], {}).get("title") or "")[:200],
            "business_class": "invalid", "reason_codes": [], "outcome": "failed", "error": item["reason"],
        } for item in budget_skips]
        if not rows:
            reason = "duplicate_project" if claimed_project_id is not None else "no_eligible_project"
            return _batch_summary(ApplicationLoopResult(True, reason=reason, project_id=claimed_project_id, decision_reports=tuple(skip_reports) or None), observed_count, 0, (), ())
        prompt = build_planner_prompt(rows, today)
    except Exception: return _batch_summary(ApplicationLoopResult(False, error="planner_contract_invalid"), observed_count, 0, (), ())
    try: planned = (planner or _default_planner)(prompt, evidence)
    except Exception: return _batch_summary(ApplicationLoopResult(False, error="planner_runner_failed", planner_expected_count=len(rows), decision_reports=tuple(skip_reports) or None), observed_count, 0, (), ())
    returned = len(planned.get("decisions")) if isinstance(planned, Mapping) and isinstance(planned.get("decisions"), list) else None
    try:
        items = planned.get("decisions") if isinstance(planned, Mapping) and set(planned) == {"decisions"} else None
        # Measured 2026-09-07: planner_contract_invalid 1235 times, more than every other
        # failure combined, and every one of them threw away decisions the planner had
        # actually made.  A model that returns 19 judgements for 20 rows has judged 19
        # rows; the loop below already matches on request_id and books whatever is missing
        # or unrecognised into invalid_ids.  Requiring the counts to match on top of that
        # converts a partial answer into a total loss, and a lane that applies to nothing.
        if not isinstance(items, list): raise ValueError
        rows_by_id = {str(row["external_id"]): row for row in rows}
        decisions = {}; invalid_ids = []
        for item in items:
            project_id = str(item.get("request_id") or "") if isinstance(item, Mapping) else ""
            if project_id not in rows_by_id or project_id in decisions:
                decisions.pop(project_id, None)
                invalid_ids.append(project_id)
                continue
            try: decisions.update(_validate([rows_by_id[project_id]], {"decisions": [item]}, today))
            except Exception: invalid_ids.append(project_id)
        invalid_ids.extend(project_id for project_id in rows_by_id if project_id not in decisions and project_id not in invalid_ids)
    except Exception: return _batch_summary(ApplicationLoopResult(False, error="planner_contract_invalid", planner_expected_count=len(rows), planner_returned_count=returned, decision_reports=tuple(skip_reports) or None), observed_count, 0, (), ())
    try: _cache_no_effect(decisions, rows_by_id, state_path)
    except Exception:
        # This cache only avoids re-planning hard-prohibited listings.  Receipt and
        # fingerprint state remain the authority for duplicate external effects, so
        # a cache write failure must not stop fresh positive-EV applications.
        pass
    reports = list(skip_reports)
    reports.extend({
        "project_id": project_id,
        "title": str(rows_by_id[project_id].get("title") or "")[:200],
        "business_class": str(decision["business_class"]),
        "reason_codes": list(decision.get("reason_codes") or ()),
        "outcome": "skipped" if decision["business_class"] != "submit_required" else "planned",
    } for project_id, decision in decisions.items())
    reports.extend({
        "project_id": project_id,
        "title": str(rows_by_id[project_id].get("title") or "")[:200],
        "business_class": "invalid",
        "reason_codes": [],
        "outcome": "failed",
        "error": "planner_contract_invalid",
    } for project_id in invalid_ids)
    eligible = [(rows_by_id[project_id], decision) for project_id, decision in decisions.items() if decision.get("business_class") == "submit_required"]
    if not eligible:
        return _batch_summary(ApplicationLoopResult(True, reason="no_eligible_project", decision_reports=tuple(reports)), observed_count, 0, (), ())
    if submitter is None and len(eligible) > 1:
        eligible = _rank_eligible_by_buyer_quality(eligible)
    verified, blocked = [], []
    unresolved: list[ApplicationLoopResult] = []
    reports_by_id = {str(report["project_id"]): report for report in reports}
    for row, decision in eligible:
        project_id, proposal = str(row["external_id"]), str(decision["proposal_text"])
        amount, due = int(decision["price_jpy"]), str(decision["deliver_date"])
        try:
            value = application_tick.run_live_tick(project_id=project_id, proposal_text=proposal, proposed_amount_minor=amount, delivery_due_on=due, state_path=state_path) if submitter is None else _submit(submitter, row, proposal, amount, due, state_path)
            current = _tick_result(value, project_id)
        except Exception:
            current = ApplicationLoopResult(False, error="submission_uncertain", project_id=project_id)
        if _provider_verified(current):
            verified.append(current)
            reports_by_id[project_id]["outcome"] = "application_verified"
            reports_by_id[project_id]["provider_proposal_id"] = current.provider_proposal_id
            continue
        if _provider_terminal_blocked(current):
            blocked.append(project_id)
            reports_by_id[project_id]["outcome"] = "provider_terminal_blocked"
            continue
        reports_by_id[project_id]["outcome"] = "failed"
        reports_by_id[project_id]["error"] = current.error or current.reason or "submission_unverified"
        unresolved.append(current)
    if unresolved:
        current = unresolved[0]
        return _batch_summary(replace(current, decision_reports=tuple(reports)), observed_count, len(eligible), verified, blocked, unresolved_project_id=current.project_id, submitted=any(item.submitted for item in verified))
    final = replace(verified[-1], decision_reports=tuple(reports)) if verified else ApplicationLoopResult(True, reason="provider_terminal_blocked", project_id=blocked[-1] if blocked else None, decision_reports=tuple(reports))
    return _batch_summary(final, observed_count, len(eligible), verified, blocked, ok=True, submitted=any(item.submitted for item in verified))

def run_loop(*, exhaustive: bool = False, state_path: Path = DEFAULT_STATE_PATH, evidence_root: Optional[Path] = None, discoverer: Optional[Callable[..., Mapping[str, object]]] = None, planner: Optional[Callable[..., object]] = None, safety_verifier: Optional[Callable[..., object]] = None, submitter: Optional[Callable[..., object]] = None, clock: Optional[Callable[[], object]] = None, discovery: Optional[Callable[..., Mapping[str, object]]] = None, now: Optional[Callable[[], object]] = None, evidence_dir: Optional[Path] = None, output_stream: Optional[TextIO] = None, query: Optional[str] = None, timeout: float = 20.0) -> dict[str, object]:
    try:
        pending = application_tick.read_pending_descriptor(Path(state_path))
    except Exception:
        result = ApplicationLoopResult(False, error="state_invalid")
        if output_stream is not None: _emit(result, output_stream)
        return result.to_dict()
    quarantined_project_id = None
    if pending is not None:
        pending_result = _reconcile_pending(pending, Path(state_path))
        if pending_result.error != "submission_uncertain":
            if pending_result.application_verified and pending_result.project_id:
                pending_result = replace(pending_result, decision_reports=({
                    "project_id": pending_result.project_id,
                    "title": f"案件{pending_result.project_id}",
                    "business_class": "submit_required",
                    "reason_codes": [],
                    "outcome": "application_verified",
                    "provider_proposal_id": pending_result.provider_proposal_id,
                },))
            if output_stream is not None: _emit(pending_result, output_stream)
            return pending_result.to_dict()
        quarantined_project_id = pending_result.unresolved_project_id or pending_result.project_id
    try: tick_value = (clock or now or (lambda: datetime.now(timezone.utc)))()
    except Exception: tick_value = None
    capacity_reason = _capacity_reason(Path(state_path), tick_value) if submitter is None and discoverer is None and discovery is None and query is None else None
    if capacity_reason is not None:
        result = ApplicationLoopResult(True, reason=capacity_reason, unresolved_project_id=quarantined_project_id)
        if output_stream is not None: _emit(result, output_stream)
        return result.to_dict()
    root = Path(evidence_root if evidence_root is not None else evidence_dir or DEFAULT_EVIDENCE_ROOT); result = ApplicationLoopResult(False, error="planner_runner_failed"); cleanup_failed = False; evidence: Optional[Path] = None
    try:
        try:
            _reset(root); evidence = root / f"run-{uuid.uuid4().hex}"; evidence.mkdir(mode=0o700, exist_ok=False); os.chmod(evidence, 0o700)
        except Exception: evidence = None
        if evidence is not None:
            source = discoverer or discovery
            turns = 3 if source is None and query is None else 1
            observed_total = 0; decision_reports: list[Mapping[str, object]] = []; wake_seen_ids: set[str] = set()
            for turn in range(turns):
                turn_evidence = evidence
                if turns > 1:
                    turn_evidence = evidence / f"turn-{turn + 1}"
                    turn_evidence.mkdir(mode=0o700, exist_ok=False)
                try:
                    observed = source(query=query if query is not None else _discovery_query(tick_value), limit=MAX_OPPORTUNITIES, timeout=timeout) if source is not None or query is not None else (_run_exhaustive_discovery(timeout) if exhaustive else _run_default_discovery(tick_value, timeout, Path(state_path), frozenset(wake_seen_ids)))
                except Exception: observed = None
                if observed is None or not isinstance(observed, Mapping): result = ApplicationLoopResult(False, error="discovery_failed")
                else:
                    error, opportunities = observed.get("error"), observed.get("opportunities", [])
                    if observed.get("ok") is not True and not (error == "no_normalized_opportunities" and "opportunities" in observed and not opportunities):
                        clean_error = error if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", error or "") else "discovery_failed"
                        result = ApplicationLoopResult(False, error=clean_error)
                    elif error is not None and error != "no_normalized_opportunities":
                        result = ApplicationLoopResult(False, error=error if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", error) else "discovery_failed")
                    elif isinstance(opportunities, (str, bytes, bytearray)) or not isinstance(opportunities, Sequence): result = ApplicationLoopResult(False, error="discovery_failed")
                    elif not opportunities: result = ApplicationLoopResult(True, reason="no_eligible_project", observed_count=int(observed.get("observed_count") or 0), already_decided_count=int(observed.get("already_decided_count") or 0))
                    else:
                        # Exhaustive discovery unions every query with no per-call cap, so a wake
                        # can see more rows than one planner call may ever receive (build_planner_prompt's
                        # _snapshot enforces MAX_OPPORTUNITIES and raising there fails the whole
                        # batch with zero judged). Slice to that cap per turn and only mark the
                        # sliced-in rows as seen, so the remaining rows are picked up on a later turn
                        # instead of being silently dropped or blocking every row behind an oversized batch.
                        fresh = []
                        for row in opportunities:
                            project_id = row.get("external_id") if isinstance(row, Mapping) else None
                            if not isinstance(project_id, str) or project_id not in wake_seen_ids:
                                fresh.append(row)
                            if len(fresh) >= MAX_OPPORTUNITIES: break
                        for row in fresh:
                            project_id = row.get("external_id") if isinstance(row, Mapping) else None
                            if isinstance(project_id, str): wake_seen_ids.add(project_id)
                        if not fresh:
                            result = ApplicationLoopResult(True, reason="no_eligible_project", observed_count=int(observed.get("observed_count") or 0), already_decided_count=int(observed.get("already_decided_count") or 0))
                        else:
                            try: today = _tick_date(tick_value)
                            except Exception: result = ApplicationLoopResult(False, error="planner_contract_invalid")
                            else:
                                result = _plan_and_submit(fresh, today, turn_evidence, planner, safety_verifier, submitter, Path(state_path))
                                result = replace(result, observed_count=max(result.observed_count or 0, int(observed.get("observed_count") or 0)), already_decided_count=int(observed.get("already_decided_count") or 0))
                observed_total = max(observed_total, result.observed_count or 0) if source is None and query is None else observed_total + (result.observed_count or 0)
                decision_reports.extend(result.decision_reports or ())
                result = replace(result, observed_count=observed_total, decision_reports=tuple(decision_reports) or None)
                if result.reason != "no_eligible_project":
                    break
    finally:
        if result.error not in RETAIN_EVIDENCE_ERRORS:
            try:
                if root.is_symlink() or root.is_file(): root.unlink()
                elif root.is_dir(): shutil.rmtree(root)
            except OSError: cleanup_failed = True
    if cleanup_failed:
        result = replace(result, ok=False, error=result.error or "evidence_cleanup_failed", cleanup_error="evidence_cleanup_failed" if result.error else None)
    if quarantined_project_id is not None and result.unresolved_project_id is None:
        result = replace(result, unresolved_project_id=quarantined_project_id)
    if output_stream is not None: _emit(result, output_stream)
    return result.to_dict()

run_application_loop = run_loop
run_once = run_loop

def main(argv: Optional[Sequence[str]] = None, *, discovery: Optional[Callable[..., Mapping[str, object]]] = None, planner: Optional[Callable[..., object]] = None, submitter: Optional[Callable[..., object]] = None, now: Optional[Callable[[], object]] = None, clock: Optional[Callable[[], object]] = None, stdout: Optional[TextIO] = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False); parser.add_argument("--json", action="store_true", required=True); parser.add_argument("--reconcile-only", action="store_true"); parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH)); parser.add_argument("--exhaustive", action="store_true", help="union every discovery query instead of stopping at the first fruitful one"); parser.add_argument("--discovery-timeout", type=float, default=20.0, help="seconds per discovery request (provider bound: 0 < t <= 60). The exhaustive budget is this multiplied by the number of queries, not a larger single request."); args = parser.parse_args(list(argv) if argv is not None else None)
    output_stream = sys.stdout if stdout is None else stdout
    result = run_reconcile_only(Path(args.state_path), output_stream=output_stream) if args.reconcile_only else run_loop(discoverer=discovery, planner=planner, submitter=submitter, clock=clock or now, state_path=Path(args.state_path), output_stream=output_stream, exhaustive=args.exhaustive, timeout=args.discovery_timeout)
    if not args.reconcile_only and discovery is None and planner is None and submitter is None and now is None and clock is None:
        reporter = _load("_anicca_lancers_application_reporter", HERE / "telegram_report.py")
        delivery = reporter.notify_application_wake(result)
        if delivery.delivery_uncertain or delivery.pre_send_failed:
            return 1
    return 0 if result["ok"] else 1

__all__ = ["AGENT_RUNNER", "AGENT_RUNNER_PATH", "ApplicationLoopResult", "DEFAULT_EVIDENCE_DIR", "DEFAULT_EVIDENCE_ROOT", "DEFAULT_STATE_PATH", "PLANNER_SCHEMA", "SCHEMA_PATH", "application_tick", "build_planner_prompt", "invoke_planner", "main", "run_application_loop", "run_loop", "run_once", "run_reconcile_only", "status", "validate_decisions"]

if __name__ == "__main__": raise SystemExit(main())
