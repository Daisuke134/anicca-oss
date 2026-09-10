import importlib.util
import json
from pathlib import Path


MODULE = Path(__file__).parents[1] / "scripts" / "reply_grounding.py"
SPEC = importlib.util.spec_from_file_location("marketplace_reply_grounding_test", MODULE)
grounding = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(grounding)


def test_reply_grounding_combines_private_ssot_and_public_provider_facts(tmp_path):
    private = tmp_path / "profile.json"
    private.write_text(json.dumps({
        "candidate": {"date_of_birth": "2002-01-30", "base": "Tokyo, Japan"},
        "facts": [
            {"id": "current_role", "claim": "2025年4月からAI関連業務に従事", "evidence": "resume"},
        ],
    }), encoding="utf-8")
    public = tmp_path / "public.json"
    public.write_text(json.dumps({
        "hours_limit": "31-40",
        "status": "available",
        "occupation": "AI関連サービス",
        "skills": [{"name": "Python", "years": 3, "note": "業務自動化"}],
    }), encoding="utf-8")

    result = grounding.build_reply_grounding(
        candidate_profile_path=private,
        provider_profile_path=public,
        today=grounding.date(2026, 9, 8),
    )

    assert result["candidate"] == {"age_band": "20代", "base": "Tokyo, Japan"}
    assert result["verified_facts"] == [
        {"id": "current_role", "claim": "2025年4月からAI関連業務に従事"},
    ]
    assert result["provider_public_facts"]["hours_limit"] == "31-40"
    assert result["provider_public_facts"]["skills"][0]["name"] == "Python"
    assert {row["id"] for row in result["prompt_facts"]} >= {
        "candidate_age_band", "candidate_base", "provider_hours_limit",
        "provider_skill_python", "current_role",
    }
    assert any(row["claim"] == "性別: male" for row in grounding.build_reply_grounding(
        candidate_profile_path=_profile_with_gender(tmp_path),
        today=grounding.date(2026, 9, 8),
    )["prompt_facts"])
    assert "evidence" not in json.dumps(result, ensure_ascii=False)


def test_projected_provider_profile_mapping_is_the_reply_source(tmp_path):
    private = tmp_path / "profile.json"
    private.write_text(json.dumps({
        "candidate": {},
        "facts": [{"id": "x", "claim": "verified", "evidence": "source"}],
    }), encoding="utf-8")

    result = grounding.build_reply_grounding(
        candidate_profile_path=private,
        provider_profile={
            "display_name": "Kaito｜AI自動化",
            "occupation": "ITエンジニア",
            "skills": [{"name": "Python", "years": 3, "note": "業務自動化"}],
        },
    )

    assert result["provider_public_facts"]["occupation"] == "ITエンジニア"
    assert any(row["claim"] == "職種: ITエンジニア" for row in result["prompt_facts"])


def test_missing_gender_stays_visibly_missing_instead_of_being_inferred(tmp_path):
    private = tmp_path / "profile.json"
    private.write_text(json.dumps({
        "candidate": {"date_of_birth": "2002-01-30"},
        "facts": [{"id": "x", "claim": "verified", "evidence": "source"}],
    }), encoding="utf-8")

    result = grounding.build_reply_grounding(
        candidate_profile_path=private, today=grounding.date(2026, 9, 8)
    )

    assert "gender" not in result["candidate"]
    assert result["missing_candidate_fields"] == ["gender"]


def test_private_identity_never_enters_composition_facts_but_public_seller_name_does(tmp_path):
    private = tmp_path / "profile.json"
    private.write_text(json.dumps({
        "candidate": {
            "full_name": "Private Legal Name",
            "preferred_name": "Private",
            "application_email": "private@example.com",
            "phone": "+81-00-0000-0000",
            "date_of_birth": "2002-01-30",
        },
        "facts": [
            {"id": "role", "claim": "Python開発を3年経験", "evidence": "resume"},
            {"id": "named_role", "claim": "Private Legal Name is a developer", "evidence": "resume"},
            {"id": "contact", "claim": "Contact private@example.com", "evidence": "profile"},
        ],
    }), encoding="utf-8")
    public = tmp_path / "public.json"
    public.write_text(json.dumps({"display_name": "Kaito｜AI自動化"}), encoding="utf-8")

    result = grounding.build_reply_grounding(
        candidate_profile_path=private, provider_profile_path=public,
        today=grounding.date(2026, 9, 8),
    )

    rendered = json.dumps(result["prompt_facts"], ensure_ascii=False)
    assert "Private Legal Name" not in rendered
    assert "private@example.com" not in rendered
    assert result["provider_public_facts"]["display_name"] == "Kaito｜AI自動化"
    assert result["private_identity_values"] == [
        "+81-00-0000-0000", "Private", "Private Legal Name", "private@example.com",
    ]


def _profile_with_gender(tmp_path):
    path = tmp_path / "profile-with-gender.json"
    path.write_text(json.dumps({
        "candidate": {"date_of_birth": "2002-01-30", "gender": "male"},
        "facts": [{"id": "x", "claim": "verified", "evidence": "source"}],
    }), encoding="utf-8")
    return path
