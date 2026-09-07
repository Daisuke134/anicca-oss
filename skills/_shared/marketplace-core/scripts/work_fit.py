#!/usr/bin/env python3
"""What the fleet refuses, in one place, for every marketplace.

Each adapter had grown its own answer to "should we bid on this", and they disagreed in the
expensive direction. Measured 2026-09-07:

    Lancers     an eight-class refusal list judged by an LLM against the posting text
    Coconala    an LLM planner with its own, separately worded, list
    CrowdWorks  no judgement at all -- an allow-list of fifteen development category words,
                which rejected 59 of 98 open postings including 「HTML・CSSコーディング」 and
                「AI・チャットボット開発」, both of which the catalogue sells

The allow-list is the mistake worth naming. Development is what the fleet is best at, not the
only thing it can deliver, so enumerating what it may take on has to be wrong every time the
marketplace invents a category name. The refusals are the short, stable list; everything else
is workable.

`HARD_PROHIBITION_CLASSES` is for adapters that put the posting text in front of a model.
`category_refusal` is for adapters that only have a category label, and is deliberately narrow:
a label is thin evidence, so it may only refuse on words that cannot mean anything else.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = ["HARD_PROHIBITION_CLASSES", "PROHIBITED_CATEGORY_TERMS", "category_refusal",
           "PROVEN_BOARD_TERMS", "GENERAL_WORK_TERMS", "discovery_terms",
           "JUDGEMENT_SCHEMA", "build_judgement_prompt", "judge"]

# Kosuke is an autonomous agent. It cannot show a face, speak in real time, hold a body in a
# room, or lend a credential it does not have. Everything below follows from that, and nothing
# below is about quality or preference.
HARD_PROHIBITION_CLASSES = {
    "video_or_animation": "video editing/production, live-action filming, AI video, animation, or MV",
    "physical_or_onsite": "on-site work or physical making/assembly/cleaning/repair/cooking/sewing/woodwork/model making/packing/shipping/delivery/receipt",
    "mandatory_human_presence": "human face appearance/performance/voice recording/phone support/mandatory live call or mandatory video interview",
    # 2026-09-07: applied to 「YouTube・SNS用オリジナルキャラクター制作（Live2D＋情報発信用素材
    # 一式）」 at ¥250,000. video_or_animation says "animation" and the model read a Live2D rig as
    # neither video nor animation, which is arguable. The line that matters is not the medium but
    # what has to be produced: an original drawing, model or rig is craft made in specialist tools
    # over many passes with a human eye, and taking one we cannot finish costs a review, not just
    # a proposal. Dais 2026-09-07: "true they should not any more."
    #
    # This does not close design. A page, a layout, a slide deck or a Figma-to-HTML build is a
    # document or a system and stays workable; 「Webデザイン」 and 「HTML・CSSコーディング」 are
    # deliberately absent from the terms below.
    "original_illustration_or_modelling": "producing original illustration, manga, character design, Live2D/VTuber rigging, 3D modelling, avatars or hand-drawn artwork, where the deliverable is the artwork itself rather than a page, a system or a document",
    "illegal_or_unsafe": "illegal or unsafe work",
    # 2026-09-07: applied to 整理収納アドバイザー監修 and 防災士監修. Both name a certification the
    # persona does not hold, and both were read as allowed because the wording said "legally
    # required" and a 監修 credential is not a licence. Supervising under a credential you lack is
    # lending a name, which is the thing the honesty rules exist to prevent.
    "missing_legal_qualification": "a named qualification, certification, licence or 監修者 credential that Kosuke does not hold, whether or not the law requires it",
    # 2026-09-07: applied to 「バイマで出品作業」 at roughly ¥50 per item. The catalogue sells built
    # software and automation; this is the buyer's own account operated by hand, forever. The
    # capability list already says web/browser operation, which is true of a tool we build and not
    # of standing in for staff. maintenance_retainer is unaffected: it operates systems we built.
    "manual_marketplace_operation": "ongoing manual work inside the buyer's own account or marketplace (出品代行, 受発注, 在庫更新, 投稿代行, 反復データ入力) where the deliverable is worked hours rather than software, automation or a built artifact",
    "mandatory_attribute_fabrication": "mandatory personal attribute that cannot be answered truthfully without fabrication",
}

# Dais withdrew explicit_ai_prohibition on 2026-09-07: the work is built and reviewed by an AI
# that is good at it, so a blanket "no AI" line in a posting is not a reason to refuse.

# Category labels are thin evidence, so these terms have to be ones that cannot mean development.
# 「音声」 is absent on purpose -- it is in 「音声認識AI開発」, which is exactly the work we want --
# and so is 「撮影」, which appears inside design categories the fleet can serve.
PROHIBITED_CATEGORY_TERMS = (
    ("video_or_animation", ("動画", "映像", "アニメーション", "YouTube", "TikTok", "ショート動画", "MV制作")),
    ("physical_or_onsite", ("配達", "配送", "梱包", "発送", "清掃", "施工", "内職", "軽作業", "現地", "出張")),
    ("mandatory_human_presence", ("ナレーション", "声優", "吹き替え", "テレアポ", "コールセンター", "電話営業",
                                  "モデル・タレント", "出演")),
    ("manual_marketplace_operation", ("出品代行", "せどり", "転売", "BUYMA", "バイマ", "投稿代行", "SNS運用代行")),
    # 「デザイン」 and 「画像」 are absent on purpose: 「Webサイトデザイン」, 「サムネイル作成・画像
    # デザイン」 and 「AI生成画像の加工・レタッチ」 are all workable, and only the crafts are named.
    ("original_illustration_or_modelling", ("イラスト", "漫画", "マンガ", "Live2D", "VTuber",
                                            "キャラクターデザイン", "キャラデザ", "3Dモデ",
                                            "似顔絵", "絵画", "アバター", "立ち絵")),
)


def category_refusal(category: str) -> Optional[tuple[str, str]]:
    """`(class, matched_term)` when a category label alone is enough to refuse, else None.

    None is the answer for anything unrecognised. A marketplace adds category names faster than
    anyone updates a list, and the cost of the two mistakes is not symmetric: wrongly bidding on
    one posting costs one proposal, wrongly refusing an unknown label costs every posting under
    it, silently, until somebody reads a rejection counter.
    """
    label = str(category or "")
    for prohibition, terms in PROHIBITED_CATEGORY_TERMS:
        for term in terms:
            if term in label:
                return prohibition, term
    return None


# What to search for, in one place. Measured 2026-09-07: Lancers kept a hand-written list in its
# own source, CrowdWorks derived terms from the catalogue, and Coconala searched the single
# keyword `AI`. Three answers to one question, and only CrowdWorks' was derived from what the
# owner actually sells -- so that derivation moved to listing_catalog.search_terms() and this
# composes it with the rest.

# Kept because they were measured returning live boards, not because a catalogue row spells them
# this way: 「業務自動化システム」 is the catalogue title and 「業務自動化」 is what finds jobs.
PROVEN_BOARD_TERMS = (
    "業務自動化", "業務システム", "Webアプリ", "システム開発",
    "LINE Bot", "スクレイピング", "Excel VBA", "ダッシュボード",
    "Chrome拡張", "RPA", "ECサイト", "不具合修正",
)

# Work outside the catalogue that an agent delivers well, as a file, without hours of manual
# operation in a buyer's account. Dais 2026-09-07: development is what they are best at, not the
# limit of what they can do; BUYMA, SNS posting itself and physical work are the exclusions.
GENERAL_WORK_TERMS = (
    "WordPress", "LP制作", "API連携", "GAS", "HTMLコーディング",
    "ChatGPT", "生成AI", "AIチャットボット", "Notion",
    "データ入力", "記事作成", "ブログ記事", "資料作成", "リサーチ", "翻訳", "文字起こし",
)


def discovery_terms(catalog_terms: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Every term worth searching for, deduplicated and with refused work removed.

    Terms naming work the fleet declines are dropped here rather than left for the planner:
    fetching them only manufactures skips, which is exactly what DEFAULT_DISCOVERY_QUERY =
    "SNS運用" was doing until 2026-09-07.
    """
    out: list[str] = []
    for term in tuple(PROVEN_BOARD_TERMS) + tuple(catalog_terms) + tuple(GENERAL_WORK_TERMS):
        term = str(term).strip()
        if not term or term in out:
            continue
        if category_refusal(term) is not None:
            continue
        out.append(term)
    return tuple(out)


# Judging the posting text, not the category label. `category_refusal` above is what a platform
# gets when a label is all it has; this is what it gets when it has the posting.
#
# CrowdWorks had neither. Measured 2026-09-07, the first five applications after its category
# allow-list was removed included three agency and reseller recruitments -- nothing is delivered,
# so there is nothing to deliver well. Coconala had no fitness judgement either, applied to 楽譜
# 制作, イラストレッスン and 留学相談 twenty to thirty times a day for a week, and was restricted
# by the marketplace on 2026-09-02. A lane that applies without judging is the shape that costs
# an account.

JUDGEMENT_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "work_fit_judgement.schema.json"
_AGENT_RUNNER = Path(__file__).resolve().parents[4] / "runtime" / "agent-runner" / "agent_runner.py"
_JUDGE_TIMEOUT_SECONDS = 240


class JudgementUnavailable(RuntimeError):
    """The judge could not run. Callers must treat this as 'do not apply', never as 'workable'."""


def build_judgement_prompt(postings: Sequence[Mapping[str, object]]) -> str:
    """One posting per row: id, title, and enough body to decide on."""
    rows = [{
        "posting_id": str(item.get("posting_id") or item.get("external_id") or "").strip(),
        "title": str(item.get("title") or "")[:200],
        "body": str(item.get("body") or item.get("description") or "")[:4000],
    } for item in postings]
    classes = "\n".join(f"- {name}: {text}" for name, text in HARD_PROHIBITION_CLASSES.items())
    return (
        "あなたは受注可否だけを判定します。提案文も価格も書きません。\n"
        "次のいずれかに当たる募集は workable=false とし、reason_code にそのclass keyを、"
        "quote に募集本文から連続する200文字以内の原文引用を入れてください。\n"
        f"{classes}\n"
        "- no_deliverable: 代理店・パートナー・アフィリエイト等の勧誘で、納品する成果物が存在しない\n"
        "どれにも当たらなければ workable=true、reason_code と quote は null にします。\n"
        "得意でない分野というだけでは false にしません。判断は本文の記述だけに基づかせ、"
        "推測で条件を足しません。全てのpostingについて1件ずつ返します。\n"
        "POSTINGS:\n" + json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _default_runner(prompt: str, evidence_dir: Path, loop: str) -> Mapping[str, object]:
    command = [
        sys.executable, str(_AGENT_RUNNER), "--task-class", "planning", "--prompt-stdin",
        "--schema", str(JUDGEMENT_SCHEMA), "--evidence-dir", str(evidence_dir),
        "--task-label", "work-fit-judgement", "--loop", loop,
        "--workdir", str(Path(__file__).resolve().parents[4]),
    ]
    # stderr is kept. A runner that refuses on configuration the lane cannot see is a lane that
    # stops applying without ever saying why.
    completed = subprocess.run(command, input=prompt, text=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, check=False,
                               timeout=_JUDGE_TIMEOUT_SECONDS + 30)
    if completed.returncode != 0:
        raise JudgementUnavailable(((completed.stderr or "").strip().splitlines() or ["no stderr"])[-1])
    evidence = Path(evidence_dir)
    summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "success":
        raise JudgementUnavailable(str(summary.get("status")))
    result_path = Path(str(summary["result_path"])).resolve()
    result_path.relative_to(evidence.resolve())
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, Mapping):
        raise JudgementUnavailable("result_not_an_object")
    return result


def judge(
    postings: Sequence[Mapping[str, object]],
    *,
    evidence_dir: Path,
    runner: Optional[Callable[..., Mapping[str, object]]] = None,
    loop: str = "marketplace-work-fit",
) -> dict[str, Optional[tuple[str, str]]]:
    """`{posting_id: None}` when workable, `{posting_id: (reason_code, quote)}` when not.

    A posting the judge did not return is absent from the result rather than defaulted to
    workable: an unjudged posting is not an approved one, and defaulting the other way is how a
    lane applies to work nobody looked at.
    """
    if not postings:
        return {}
    ids = [str(item.get("posting_id") or item.get("external_id") or "").strip() for item in postings]
    try:
        result = (runner or (lambda prompt, directory: _default_runner(prompt, directory, loop)))(
            build_judgement_prompt(postings), Path(evidence_dir))
    except JudgementUnavailable:
        raise
    except Exception as error:
        raise JudgementUnavailable(str(error)) from None

    rows = result.get("judgements") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        raise JudgementUnavailable("judgements_missing")
    verdicts: dict[str, Optional[tuple[str, str]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        posting_id = str(row.get("posting_id") or "").strip()
        if posting_id not in ids or posting_id in verdicts:
            continue
        if row.get("workable") is True:
            verdicts[posting_id] = None
            continue
        reason = str(row.get("reason_code") or "").strip() or "unstated"
        quote = str(row.get("quote") or "").strip()[:200]
        verdicts[posting_id] = (reason, quote)
    return verdicts
