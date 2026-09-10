from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "profile.py"
SPEC = importlib.util.spec_from_file_location("crowdworks_profile_avatar_test", MODULE_PATH)
assert SPEC and SPEC.loader
profile = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = profile
SPEC.loader.exec_module(profile)


def test_default_public_avatar_is_not_aligned() -> None:
    source = (
        "https://cw-assets.crowdworks.jp/vite/2026-09/1/packs/legacy/"
        "images/user_picture/default/140x140-example.png"
    )

    assert profile._avatar_aligned(source) is False


def test_uploaded_public_avatar_is_aligned() -> None:
    source = "https://crowdworks.jp/user_picture/123456/140x140-example.jpg"

    assert profile._avatar_aligned(source) is True


def test_shared_avatar_is_the_existing_provider_neutral_asset() -> None:
    expected = Path(__file__).parents[3] / "gig-work" / "profile" / "avatar.jpg"

    assert profile.DEFAULT_AVATAR_PATH == expected
    assert profile._validated_avatar(profile.DEFAULT_AVATAR_PATH) == expected


def test_matching_official_components_need_no_profile_mutation() -> None:
    config = {
        "display_name": "Kaito｜AI自動化",
        "occupation": "AI関連サービス",
        "status": "available",
        "hours_limit": "31-40",
        "min_hourly_wage": 3000,
        "max_hourly_wage": 5000,
        "web_meeting": "available",
        "introduction": "AI・ソフトウェア開発と研修支援",
        "job_categories": ["Webプログラミング", "AIシステム開発"],
        "skills": [{"name": "Python"}, {"name": "TypeScript"}],
    }
    components = profile._expected_components(config)
    components["avatar"] = {"aligned": True}

    assert profile._profile_aligned(components, config) is True

    components["occupation"]["hash"] = profile._hash("プロンプトエンジニア")
    assert profile._profile_aligned(components, config) is False
