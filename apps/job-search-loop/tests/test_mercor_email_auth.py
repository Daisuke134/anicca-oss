import json

import pytest

from job_search_loop import mercor_email_auth
from job_search_loop.mercor_email_auth import application_email, extract_action_url


def test_extracts_only_official_mercor_firebase_action_link():
    link = (
        "https://mercor-prod-firebase.firebaseapp.com/__/auth/action?"
        "mode=signIn&oobCode=private&continueUrl=https%3A%2F%2Fwork.mercor.com"
    )
    assert extract_action_url({"body": f"Continue to Mercor → ( {link} )"}) == link


@pytest.mark.parametrize("body", ["", "https://evil.example/auth/action?mode=signIn"])
def test_rejects_missing_or_unofficial_action_link(body):
    with pytest.raises(ValueError, match="mercor_magic_link_not_found"):
        extract_action_url({"body": body})


def test_reads_application_email_from_private_profile(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"candidate": {"application_email": "candidate@example.com"}}))
    assert application_email(profile) == "candidate@example.com"


def test_email_login_navigates_to_exact_official_login_before_filling():
    source = mercor_email_auth.Path(mercor_email_auth.__file__).read_text()
    navigate = '"Page.navigate", {"url": "https://work.mercor.com/login"}'
    fill = "document.querySelector('input[type=\"email\"][name=\"email\"]')"
    assert navigate in source
    assert source.index(navigate) < source.index(fill)
