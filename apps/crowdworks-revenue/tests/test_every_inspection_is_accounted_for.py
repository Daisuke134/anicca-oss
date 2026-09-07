"""A wake that inspects 63 postings and explains 26 of them has lost the other 37.

Measured 2026-09-07 in production:

    {"inspected":63,"closed_or_unverified":8,"off_topic":0,"wrong_category":0,
     "budget":17,"not_workable":1,"judge_unavailable":0}

26 accounted, 37 gone. Both silent exits were `continue`/`break` with no counter: a posting whose
page failed to load, and the search budget running out part-way through the listings. The second
is the worse one -- it truncates the board without saying so, and reads as a quiet day.

This is the same fault this session spent hours on in Lancers, one level up: a lane that refuses
without saying why cannot be repaired from its own reports.

Run: python3 -m pytest apps/crowdworks-revenue/tests/test_every_inspection_is_accounted_for.py
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OWNER = ROOT / "skills" / "earn" / "crowdworks" / "scripts" / "application_owner.py"


def _candidate_source() -> str:
    tree = ast.parse(OWNER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_candidate":
            return ast.get_source_segment(OWNER.read_text(encoding="utf-8"), node)
    raise AssertionError("_candidate not found")


def test_the_counters_cover_the_unreadable_page_and_the_expired_budget():
    source = OWNER.read_text(encoding="utf-8")
    for name in ("unreadable", "out_of_time"):
        assert f'"{name}": 0' in source, name
        assert f'rejected["{name}"]' in source, name


def test_no_continue_in_the_candidate_loop_is_silent():
    """Each `continue` must be preceded by a counter increment inside the same block."""
    source = _candidate_source()
    lines = source.split("\n")
    for index, line in enumerate(lines):
        if line.strip() != "continue":
            continue
        window = "\n".join(lines[max(0, index - 6):index + 1])
        assert 'rejected[' in window, f"silent continue at:\n{window}"


def test_the_truncated_search_says_how_much_it_did_not_read():
    """Counting one is not enough: the point is how many listings went unread."""
    source = _candidate_source()
    assert re.search(r'rejected\["out_of_time"\]\s*\+=\s*len\(ordered\)\s*-\s*ordered\.index\(listing\)', source)


def test_an_unreadable_posting_is_reported_not_just_counted():
    """Dais reads the declined list; a page that would not load is a decision like any other."""
    source = _candidate_source()
    assert "募集ページを読み込めませんでした" in source
    assert "type(error).__name__" in source
