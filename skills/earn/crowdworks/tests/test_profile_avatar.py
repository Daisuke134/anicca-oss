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
        "skills": [
            {"name": "Python", "level": "4", "years": 3, "note": "業務自動化"},
            {"name": "TypeScript", "level": "3", "years": 3, "note": "Web開発"},
        ],
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
        "skills": [
            {"name": "Python", "level": "4", "years": 3, "note": "業務自動化"},
            {"name": "TypeScript", "level": "3", "years": 3, "note": "Web開発"},
        ],
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


def test_skill_alignment_includes_public_level_years_and_note() -> None:
    config = {
        "display_name": "Kaito｜AI自動化",
        "occupation": "ITエンジニア",
        "occupation_detail": {"id": "1", "label": "システムエンジニア（SE）"},
        "status": "available",
        "hours_limit": "31-40",
        "min_hourly_wage": 3000,
        "max_hourly_wage": 5000,
        "web_meeting": "available",
        "introduction": "ソフトウェア開発",
        "job_categories": ["Webプログラミング"],
        "skills": [{"name": "Python", "level": "4", "years": 3, "note": "業務自動化"}],
    }
    components = profile._expected_components(config)
    components["avatar"] = {"aligned": True}
    assert profile._profile_aligned(components, config) is True

    components["skills"]["hash"] = profile._hash("Python|4|1〜3年|別の備考")
    assert profile._profile_aligned(components, config) is False


def test_commercial_profile_rejects_year_band_the_provider_cannot_project(tmp_path: Path) -> None:
    value = json.loads(profile.DEFAULT_COMMERCIAL_PROFILE_PATH.read_text(encoding="utf-8"))
    value["skills"][0]["years"] = 4
    path = tmp_path / "commercial.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    try:
        profile._load_commercial(path)
    except profile.ProfileError as error:
        assert error.code == "commercial_profile_invalid"
    else:
        raise AssertionError("unsupported year band was accepted")


def test_stale_same_name_skill_is_deleted_before_exact_rebuild() -> None:
    class Locator:
        def __init__(self, page: "Page", kind: str) -> None:
            self.page = page
            self.kind = kind
            self.first = self

        def count(self) -> int:
            return self.page.rows if self.kind == "rows" else 1

        def locator(self, selector: str) -> "Locator":
            assert selector == 'a[data-method="delete"]'
            return Locator(self.page, "delete")

        def click(self) -> None:
            self.page.rows -= 1

    class Page:
        rows = 1
        url = profile.SKILLS_URL

        def locator(self, selector: str) -> Locator:
            assert selector in {'tr[id^="user_skills_"]', "body"}
            return Locator(self, "rows" if selector.startswith("tr") else "body")

        def once(self, event: str, callback: object) -> None:
            assert event == "dialog"

        def wait_for_load_state(self, **_kwargs: object) -> None:
            return None

        def goto(self, url: str) -> None:
            self.url = url

    page = Page()
    profile._delete_all_skills(page)
    assert page.rows == 0


def test_hidden_stale_occupation_checkbox_is_cleared_without_clicking() -> None:
    class HiddenCheckbox:
        checked = True
        evaluated = False

        def is_checked(self) -> bool:
            return self.checked

        def evaluate(self, script: str, checked: bool) -> None:
            assert "dispatchEvent" in script
            self.evaluated = True
            self.checked = checked

    checkbox = HiddenCheckbox()
    profile._set_checkbox(checkbox, False)
    assert checkbox.checked is False
    assert checkbox.evaluated is True
