"""An application report has to say what was applied for and for how much.

Measured 2026-09-07 by reading Dais's own Telegram history. Lancers, on a real application:

    [Lancers][応募判断] 📨 応募を公式確認しました
    案件: 案件5598169
    案件ID: 5598169

CrowdWorks, on a real application:

    [CrowdWorks][応募完了] ✅ 実際に応募しました
    案件: 【Shopify】RakuFit公式ECサイトのデザイン・構築
    Proposal ID: 304757474
    提案: JPY 250000 / 固定報酬

Both facts were already in hand: the pending descriptor that this path reconciles carries `title`
and `amount_minor`. The report substituted 「案件<id>」 for one and dropped the other, so the
message for a real application said less than the message for a refusal.

Run: python3 -m pytest apps/lancers-revenue/tests/test_application_report_says_what_and_how_much.py
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "skills" / "earn" / "lancers" / "scripts" / "telegram_report.py"


def _report():
    spec = importlib.util.spec_from_file_location("lancers_report_under_test", REPORT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERIFIED = {
    "project_id": "5598169",
    "title": "【工場勤務者向け】リカバリーサービスPR記事｜ライター募集",
    "business_class": "submit_required",
    "reason_codes": [],
    "outcome": "application_verified",
    "provider_proposal_id": "27897159",
    "price_jpy": 30000,
}


def test_a_real_application_names_the_job_the_price_and_the_receipt():
    text = _report().render_application_decision(VERIFIED)
    assert "【工場勤務者向け】リカバリーサービスPR記事｜ライター募集" in text
    assert "提案額: 30,000円" in text
    assert "Proposal ID: 27897159" in text
    assert "案件: 案件5598169" not in text


def test_the_price_is_grouped_because_a_bare_integer_is_hard_to_read():
    text = _report().render_application_decision({**VERIFIED, "price_jpy": 250000})
    assert "提案額: 250,000円" in text


def test_a_missing_price_leaves_no_empty_line_behind():
    text = _report().render_application_decision({k: v for k, v in VERIFIED.items() if k != "price_jpy"})
    assert "提案額" not in text
    assert "\n\n" not in text


def test_a_nonsense_price_is_omitted_rather_than_printed():
    for bad in (0, -1, True, "30000", None):
        text = _report().render_application_decision({**VERIFIED, "price_jpy": bad})
        assert "提案額" not in text, bad


def test_a_refusal_still_reads_the_same_and_carries_no_price():
    text = _report().render_application_decision({
        "project_id": "5597055",
        "title": "【防災士監修】楽天､ECサイト上で販売する防災用品の監修依頼",
        "business_class": "hard_prohibited",
        "reason_codes": ["missing_legal_qualification", "・防災士資格または防災分野での活動実績を確認できる方"],
        "outcome": "skipped",
    })
    assert "🚫 応募しません" in text
    assert "missing_legal_qualification" in text
    assert "提案額" not in text and "Proposal ID" not in text
