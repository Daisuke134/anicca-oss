"""「固定報酬の提示がありません」 was the largest rejection reason, and it was the wrong reading.

Measured 2026-09-07 over the lane's own decline records: 306 postings rejected for having no
parseable fixed price, against 70 whose stated budget was genuinely too small. Sampling ten of
the 306 found

    【長期・フルリモート】AIを活用したWebエンジニア募集｜WordPress・PHP・既存システム改修

among them -- the catalogue's own work, dropped because a number was missing rather than because
anybody judged it.

A posting with no fixed price is asking for a quote. The ones that should not be bid on -- 500円
monitors, テレアポ, 求人代行 -- are refused a few lines later by work_fit, on what they are.

Run: python3 -m pytest apps/crowdworks-revenue/tests/test_a_missing_budget_is_a_request_for_a_quote.py
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OWNER = ROOT / "skills" / "earn" / "crowdworks" / "scripts" / "application_owner.py"


def _owner():
    spec = importlib.util.spec_from_file_location("cw_priced_under_test", OWNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _listing(module):
    return module._listings()[0]


def test_no_stated_budget_quotes_the_lowest_tier():
    module = _owner()
    listing = _listing(module)
    tier = module._priced(listing, "報酬は応相談です。ご提案ください。")
    assert tier is not None
    assert tier["price_jpy"] == listing["tiers"][0]["price_jpy"]


def test_a_budget_that_cannot_reach_the_lowest_tier_is_still_refused():
    """Widening must not turn into bidding below cost."""
    module = _owner()
    assert module._priced(_listing(module), "固定報酬制 5,000 円") is None


def test_a_generous_budget_still_buys_the_best_tier_it_can():
    module = _owner()
    listing = _listing(module)
    tier = module._priced(listing, "固定報酬制 200,000 円")
    affordable = [t["price_jpy"] for t in listing["tiers"] if t["price_jpy"] <= 200000]
    assert tier["price_jpy"] == max(affordable)


def test_the_judge_still_runs_after_pricing():
    """A 500円 monitor with no stated budget now reaches the judge, which is where it should die."""
    source = OWNER.read_text(encoding="utf-8")
    priced_at = source.index("tier=_priced(listing,text)")
    judged_at = source.index("verdict = _work_fit_verdict(")
    assert priced_at < judged_at


def test_the_decline_message_no_longer_claims_there_is_no_fixed_price():
    """That sentence described a filter that has been removed. It survives only in the docstring
    that records why, which is where a withdrawn reading belongs."""
    source = OWNER.read_text(encoding="utf-8")
    assert '"固定報酬の提示がありません"' not in source, "still emitted as a decline reason"
    assert "報酬額を読み取れませんでした" in source
