"""Two things the lane applied to on 2026-09-07 that it should have refused.

Re-aiming the queries brought applications back — 0 in the morning, 6 by 10:40 — and the first
batch showed what the honesty rules still let through:

    【整理収納アドバイザー監修依頼】楽天､ECサイト上で販売する商品の監修
    【防災士監修】楽天､ECサイト上で販売する防災用品の監修依頼
    【主婦・未経験の方大歓迎♪隙間時間で簡単作業！】バイマで出品作業  (~¥50/件)

The first two name a credential the persona does not hold. `missing_legal_qualification` said
"legally required", and a 監修 credential is not a licence, so they read as allowed — but
supervising under a credential you lack is lending a name.

The third is the buyer's own account, operated by hand, forever. The capability list says
"web/browser上の操作", which is true of a tool we build and not of standing in for staff.

Dais's standing rule: never take work we are not good at; it produces slow delivery and bad
reviews.

Run: python3 -m pytest apps/lancers-revenue/tests/test_scope_guards.py
"""

import importlib.util
import sys
from pathlib import Path

LOOP = Path(__file__).resolve().parents[3] / "skills" / "earn" / "lancers" / "scripts" / "application_loop.py"


def _module():
    spec = importlib.util.spec_from_file_location("lancers_loop_scope", LOOP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


loop = _module()


def test_a_credential_we_lack_is_refused_even_when_no_law_requires_it():
    """整理収納アドバイザー and 防災士 are certifications, not licences. Both were let through."""
    text = loop.HARD_PROHIBITION_CLASSES["missing_legal_qualification"]
    assert "whether or not the law requires it" in text
    for word in ("certification", "監修者"):
        assert word in text


def test_operating_the_buyers_account_by_hand_is_its_own_class():
    assert "manual_marketplace_operation" in loop.HARD_PROHIBITION_CLASSES
    text = loop.HARD_PROHIBITION_CLASSES["manual_marketplace_operation"]
    for term in ("出品代行", "在庫更新", "worked hours"):
        assert term in text


def test_maintaining_systems_we_built_is_not_caught_by_it():
    """maintenance_retainer sells 開発済みシステムの保守・運用. The distinction is whose system."""
    text = loop.HARD_PROHIBITION_CLASSES["manual_marketplace_operation"]
    assert "buyer's own account" in text
    assert "software, automation or a built artifact" in text


def test_both_classes_reach_the_planner():
    for key in ("missing_legal_qualification", "manual_marketplace_operation"):
        assert key in loop.PLANNER_RULES


def test_the_earlier_guards_are_still_there():
    """Fixing scope must not quietly loosen anything already decided."""
    for key in ("video_or_animation", "physical_or_onsite", "mandatory_human_presence",
                "mandatory_attribute_fabrication"):
        assert key in loop.HARD_PROHIBITION_CLASSES
    # Dais 2026-09-07 withdrew explicit_ai_prohibition: the work is built and checked by an AI that
    # is good at it, so a blanket "no AI" line in a posting is not a reason to refuse.
    assert "explicit_ai_prohibition" not in loop.HARD_PROHIBITION_CLASSES
    assert "応募者の確認済み属性" in loop.PLANNER_RULES
