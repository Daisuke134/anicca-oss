"""A lane that applies without judging is the shape that costs an account.

Measured 2026-09-07. Coconala applied 20-30 times a day for a week with no fitness judgement at
all -- 楽譜制作, イラストレッスン, 留学相談, SNS運用代行 among them -- and the marketplace
restricted the account on 09-02. CrowdWorks had the same gap; the first five applications after
its category allow-list came off included three agency recruitments, where nothing is delivered.

`category_refusal` is what a platform gets when a label is all it has. This is what it gets when
it has the posting text.

Run: python3 -m pytest skills/_shared/marketplace-core/tests/test_work_fit_judge.py
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("work_fit_judge_under_test", SCRIPTS / "work_fit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fit = _load()

POSTINGS = [
    {"posting_id": "1", "title": "採用支援事業のパートナー募集", "body": "代理店として販売いただきます"},
    {"posting_id": "2", "title": "Webデザイン", "body": "採用サイトのデザイン制作をお願いします"},
]


def _runner(payload):
    def run(prompt, evidence_dir):
        return payload
    return run


def test_a_workable_posting_comes_back_as_none(tmp_path):
    verdicts = fit.judge(POSTINGS, evidence_dir=tmp_path, runner=_runner({"judgements": [
        {"posting_id": "1", "workable": True, "reason_code": None, "quote": None},
        {"posting_id": "2", "workable": True, "reason_code": None, "quote": None},
    ]}))
    assert verdicts == {"1": None, "2": None}


def test_a_refusal_carries_the_class_and_the_owner_s_own_words(tmp_path):
    verdicts = fit.judge(POSTINGS, evidence_dir=tmp_path, runner=_runner({"judgements": [
        {"posting_id": "1", "workable": False, "reason_code": "no_deliverable",
         "quote": "代理店として販売いただきます"},
        {"posting_id": "2", "workable": True, "reason_code": None, "quote": None},
    ]}))
    assert verdicts["1"] == ("no_deliverable", "代理店として販売いただきます")
    assert verdicts["2"] is None


def test_an_unjudged_posting_is_absent_rather_than_approved(tmp_path):
    """Defaulting the other way is how a lane applies to work nobody looked at."""
    verdicts = fit.judge(POSTINGS, evidence_dir=tmp_path, runner=_runner({"judgements": [
        {"posting_id": "1", "workable": True, "reason_code": None, "quote": None},
    ]}))
    assert "2" not in verdicts


def test_a_judge_that_cannot_run_raises_rather_than_approving(tmp_path):
    def broken(prompt, evidence_dir):
        raise OSError("agent runner missing")
    with pytest.raises(fit.JudgementUnavailable):
        fit.judge(POSTINGS, evidence_dir=tmp_path, runner=broken)


def test_a_malformed_answer_raises_rather_than_approving(tmp_path):
    for payload in ({}, {"judgements": "yes"}, {"judgements": None}):
        with pytest.raises(fit.JudgementUnavailable):
            fit.judge(POSTINGS, evidence_dir=tmp_path, runner=_runner(payload))


def test_a_verdict_for_a_posting_we_did_not_ask_about_is_ignored(tmp_path):
    verdicts = fit.judge(POSTINGS, evidence_dir=tmp_path, runner=_runner({"judgements": [
        {"posting_id": "999", "workable": False, "reason_code": "video_or_animation", "quote": "x"},
        {"posting_id": "1", "workable": True, "reason_code": None, "quote": None},
    ]}))
    assert "999" not in verdicts and verdicts["1"] is None


def test_a_missing_reason_still_refuses_under_a_named_code(tmp_path):
    verdicts = fit.judge(POSTINGS[:1], evidence_dir=tmp_path, runner=_runner({"judgements": [
        {"posting_id": "1", "workable": False, "reason_code": None, "quote": None},
    ]}))
    assert verdicts["1"] == ("unstated", "")


def test_no_postings_costs_nothing(tmp_path):
    def never(prompt, evidence_dir):
        raise AssertionError("the judge must not be invoked for an empty batch")
    assert fit.judge([], evidence_dir=tmp_path, runner=never) == {}


def test_the_prompt_carries_every_prohibition_and_the_postings():
    prompt = fit.build_judgement_prompt(POSTINGS)
    for name in fit.HARD_PROHIBITION_CLASSES:
        assert name in prompt
    assert "no_deliverable" in prompt
    assert "代理店として販売いただきます" in prompt
    # Fitness only: this judge never writes the proposal or the price.
    assert "提案文も価格も書きません" in prompt


def test_default_judge_uses_the_supported_shared_apply_task_class(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"judgements": []}), encoding="utf-8")
    (tmp_path / "summary.json").write_text(json.dumps({
        "status": "success", "result_path": str(result_path),
    }), encoding="utf-8")
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(fit.subprocess, "run", run)
    fit._default_runner("prompt", tmp_path, "crowdworks-application")
    index = observed["command"].index("--task-class")
    assert observed["command"][index + 1] == "application-intent-planner"
