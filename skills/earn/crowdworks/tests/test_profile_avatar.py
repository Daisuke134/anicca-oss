from __future__ import annotations

import importlib.util
import json
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


def test_current_attachment_avatar_is_read_back() -> None:
    source = "https://crowdworks.jp/attachments/59139511.jpg?width=200&height=200"

    class Locator:
        first = None

        def __init__(self) -> None:
            self.first = self

        def wait_for(self, **_kwargs: object) -> None:
            return None

        def count(self) -> int:
            return 1

        def get_attribute(self, name: str) -> str | None:
            return source if name == "src" else None

    class Page:
        def locator(self, selector: str) -> Locator:
            assert 'img[alt="userIcon"]' in selector
            return Locator()

    assert profile._public_avatar(Page()) == {
        "present": True,
        "aligned": True,
        "hash": profile._hash(source),
    }


def test_relative_current_attachment_avatar_is_resolved_before_readback() -> None:
    source = "/attachments/59139971.jpg?width=200&height=200"
    absolute = "https://crowdworks.jp/attachments/59139971.jpg?width=200&height=200"

    class Locator:
        first = None

        def __init__(self) -> None:
            self.first = self

        def wait_for(self, **_kwargs: object) -> None:
            return None

        def count(self) -> int:
            return 1

        def get_attribute(self, name: str) -> str | None:
            return source if name == "src" else None

    class Page:
        def locator(self, _selector: str) -> Locator:
            return Locator()

    assert profile._public_avatar(Page()) == {
        "present": True,
        "aligned": True,
        "hash": profile._hash(absolute),
    }


def test_shared_avatar_is_the_existing_provider_neutral_asset() -> None:
    expected = Path(__file__).parents[3] / "gig-work" / "profile" / "avatar.jpg"

    assert profile.DEFAULT_AVATAR_PATH == expected
    assert profile._validated_avatar(profile.DEFAULT_AVATAR_PATH) == expected


def test_matching_official_components_need_no_profile_mutation() -> None:
    config = {
        "display_name": "Kaito｜AI自動化",
        "occupation": "AI関連サービス",
        "occupation_detail": {"id": "142", "label": "プロンプトエンジニア"},
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


def test_shared_commercial_profile_owns_crowdworks_public_fields(tmp_path: Path) -> None:
    provider_path = tmp_path / "provider.json"
    provider_path.write_text(
        '{"version":1,"provider_employee_id":"7145638","display_name":"old",'
        '"occupation":"AI関連サービス","simple_introduction":"old",'
        '"introduction":"old","skills":[{"name":"old","level":"1","years":1,"note":"old"}],'
        '"status":"available",'
        '"hours_limit":"31-40","min_hourly_wage":3000,"max_hourly_wage":5000,'
        '"web_meeting":"available","job_categories":["Webプログラミング"]}',
        encoding="utf-8",
    )
    provider_path.chmod(0o600)

    config = profile.load_config(provider_path, profile.DEFAULT_COMMERCIAL_PROFILE_PATH)

    assert config["display_name"] == "Kaito｜AI自動化"
    assert config["occupation"] == "ITエンジニア"
    assert config["occupation_detail"] == {"id": "1", "label": "システムエンジニア（SE）"}
    assert "ソフトウェア" in config["introduction"]
    assert {item["name"] for item in config["skills"]} >= {"Python", "TypeScript"}
    assert config["avatar_path"] == str(profile.DEFAULT_AVATAR_PATH)
    assert json.loads(json.dumps(config, ensure_ascii=False))["occupation_detail"]["id"] == "1"


def test_buyer_visible_detail_occupation_is_required_for_alignment() -> None:
    config = {
        "display_name": "Kaito｜AI自動化",
        "occupation": "ITエンジニア",
        "occupation_detail": {"id": "1", "label": "システムエンジニア（SE）"},
        "status": "available",
        "hours_limit": "31-40",
        "min_hourly_wage": 3000,
        "max_hourly_wage": 5000,
        "web_meeting": "available",
        "introduction": "ソフトウェア開発とAI自動化、教育研修支援",
        "job_categories": ["Webプログラミング"],
        "skills": [{"name": "Python"}, {"name": "TypeScript"}],
    }
    components = profile._expected_components(config)
    components["avatar"] = {"aligned": True}
    components["occupation_detail"]["hash"] = profile._hash("142:プロンプトエンジニア")

    assert profile._profile_aligned(components, config) is False


def test_public_detail_occupation_reads_exact_buyer_visible_id_and_label() -> None:
    class Item:
        def get_attribute(self, name: str) -> str | None:
            return "/public/employees/occupation/1" if name == "href" else None

        def inner_text(self) -> str:
            return "システムエンジニア（SE）"

    class Items:
        def count(self) -> int:
            return 1

        def nth(self, _index: int) -> Item:
            return Item()

    class Page:
        def locator(self, selector: str) -> Items:
            assert "/public/employees/occupation/" in selector
            return Items()

    assert profile._public_occupation_detail(Page()) == {
        "id": "1",
        "label": "システムエンジニア（SE）",
    }
