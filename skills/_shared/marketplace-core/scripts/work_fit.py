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

from typing import Optional

__all__ = ["HARD_PROHIBITION_CLASSES", "PROHIBITED_CATEGORY_TERMS", "category_refusal",
           "PROVEN_BOARD_TERMS", "GENERAL_WORK_TERMS", "discovery_terms"]

# Kosuke is an autonomous agent. It cannot show a face, speak in real time, hold a body in a
# room, or lend a credential it does not have. Everything below follows from that, and nothing
# below is about quality or preference.
HARD_PROHIBITION_CLASSES = {
    "video_or_animation": "video editing/production, live-action filming, AI video, animation, or MV",
    "physical_or_onsite": "on-site work or physical making/assembly/cleaning/repair/cooking/sewing/woodwork/model making/packing/shipping/delivery/receipt",
    "mandatory_human_presence": "human face appearance/performance/voice recording/phone support/mandatory live call or mandatory video interview",
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
