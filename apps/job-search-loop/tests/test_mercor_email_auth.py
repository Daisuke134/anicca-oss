import pytest

from job_search_loop.mercor_email_auth import extract_action_url


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
