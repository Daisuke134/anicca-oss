import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "apply_policy.py"


def _module():
    spec = importlib.util.spec_from_file_location("marketplace_apply_policy_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_shared_apply_context_preserves_verified_facts_and_maximize_policy(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "candidate": {
            "base": "Tokyo, Japan",
            "application_email": "must-not-enter-shared-packet@example.invalid",
            "work_authorizations": [{"country": "Japan", "status": "authorized"}],
        },
        "facts": [
            {"id": "education", "claim": "Bachelor studies", "evidence": "resume"},
            {"id": "marketing", "claim": "Digital marketing experience", "evidence": "resume"},
        ],
    }), encoding="utf-8")

    value = _module().build_apply_context(profile)

    assert value["policy"]["selection"] == "maximize_truthful_submissions"
    assert value["policy"]["posting_qualifications"] == (
        "ranking_signals_not_pre_submission_rejections"
    )
    assert value["policy"]["person_bound_step"]["scope"] == "candidate_local_pending"
    assert value["policy"]["person_bound_step"]["pass_behavior"] == (
        "continue_other_candidates_without_waiting"
    )
    assert [fact["id"] for fact in value["verified_facts"]] == ["education", "marketing"]
    assert "application_email" not in value["candidate"]
