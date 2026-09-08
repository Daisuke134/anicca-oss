import importlib.util
import inspect
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_x_post_uses_frozen_state_identity_and_checks_browser_profile(monkeypatch):
    module = load("portable_x_post", "skills/writer-agent/scripts/x-post/publish.py")
    monkeypatch.setenv("X_ACCOUNT_HANDLE", "changed_after_initialization")
    expected = "https://x.com/frozen_writer"
    assert module._account_url(
        {"destination_identities": {"x-post/ja": "frozen_writer"}}
    ) == expected

    class Profile:
        def __init__(self, href):
            self.first = self
            self.href = href

        def count(self):
            return 1

        def get_attribute(self, _name):
            return self.href

    class Page:
        def __init__(self, href):
            self.href = href

        def locator(self, _selector):
            return Profile(self.href)

    assert module._authenticated_profile_matches(Page("/frozen_writer"), expected)
    assert not module._authenticated_profile_matches(Page("/another_writer"), expected)


def test_x_article_readback_uses_the_frozen_identity_parameter():
    sys.path.insert(0, str(ROOT / "skills/writer-agent/scripts/x-publish"))
    module = load(
        "portable_x_article",
        "skills/writer-agent/scripts/x-publish/x_inplace_repair.py",
    )
    method = inspect.getsource(module.XBrowserAdapter.replace_and_publish)
    assert "expected_identity" in inspect.signature(
        module.XBrowserAdapter.replace_and_publish
    ).parameters
    assert 'os.environ.get("X_ACCOUNT_HANDLE"' not in method


def test_self_owned_readback_accepts_the_configured_host():
    module = load(
        "portable_self_owned",
        "skills/writer-agent/scripts/self_owned_article.py",
    )
    contract = {
        "slug": "portable-proof",
        "lang": "en",
        "artifact_id": "run__self-owned__en",
        "source_sha256": "a" * 64,
        "preview_sha256": "b" * 64,
        "paid_sha256": "c" * 64,
        "preview_markdown": "This public preview is deliberately longer than twenty characters.",
        "paid_markdown": "This private paid section must never appear in public markup.",
    }
    markup = module.public_receipt_markup(contract)
    receipt = module.verify_public_readback(
        contract,
        "https://writer.example/blog/portable-proof",
        markup,
        base_url="https://writer.example",
        observed_at="2026-09-08T00:00:00Z",
    )
    assert receipt["verified"] is True


def test_cta_gate_requires_and_accepts_the_configured_landing_url(tmp_path):
    article = tmp_path / "article.md"
    article.write_text(
        "Visit https://writer.example/product?product_id=p&run_id=r&"
        "artifact_id=a&variant_id=v&click_id=c\n",
        encoding="utf-8",
    )
    script = ROOT / "skills/writer-agent/scripts/cta-gate.sh"
    missing = subprocess.run(
        ["bash", str(script), str(article)],
        env={key: value for key, value in os.environ.items() if key != "ARTICLE_PRODUCT_LANDING_URL"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0

    configured_env = dict(os.environ)
    configured_env["ARTICLE_PRODUCT_LANDING_URL"] = "https://writer.example/product"
    accepted = subprocess.run(
        ["bash", str(script), str(article), "--run-id", "r", "--artifact-id", "a"],
        env=configured_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["destination"] == "writer.example"


def test_note_repair_refuses_authenticated_account_mismatch(monkeypatch):
    module = load(
        "portable_note_repair",
        "skills/writer-agent/scripts/note-publish/note_inplace_repair.py",
    )
    adapter = module.NoteMcpAdapter.__new__(module.NoteMcpAdapter)
    adapter.session = SimpleNamespace(cookies={"session": "fixture"})

    async def current_user(_cookies):
        return {"id": "other-id", "urlname": "other-writer"}

    adapter._get_current_user = current_user
    monkeypatch.setenv("NOTE_USER_ID", "expected-id")
    monkeypatch.setenv("NOTE_URLNAME", "expected-writer")
    with pytest.raises(module.NoteRepairRefused, match="authenticated Note account"):
        asyncio.run(adapter.identity())


def test_self_owned_receipt_uses_frozen_state_not_mutable_environment(monkeypatch):
    module = load(
        "portable_publication_resume",
        "skills/writer-agent/scripts/publication_resume.py",
    )
    state = {
        "run_id": "run-1",
        "topic_id": "topic-1",
        "self_owned_base_url": "https://writer.example/site",
    }
    row = {
        "run_id": "run-1",
        "topic_id": "topic-1",
        "platform": "self-owned",
        "lang": "en",
        "published": True,
        "reality_gate": "PASS",
        "verified": True,
        "live_url": "https://writer.example/site/blog/portable-proof",
        "artifact_id": "run-1__self-owned__en",
        "artifact_sha256": "a" * 64,
        "preview_sha256": "b" * 64,
        "paid_sha256": "c" * 64,
    }
    monkeypatch.setenv("ARTICLE_SELF_OWNED_BASE_URL", "https://changed.example")
    assert module.is_self_owned_publication_receipt(row, state)


def test_note_publish_checks_real_account_before_any_draft_effect():
    source = (ROOT / "skills/writer-agent/scripts/publish-note.sh").read_text(
        encoding="utf-8"
    )
    assert "ANICCA_NOTE_USER_ID" not in source
    assert "get_current_user(session.cookies)" in source
    assert source.index("get_current_user(session.cookies)") < source.index(
        "draft = await create_draft(session, article)"
    )


def test_legacy_self_owned_state_upgrades_from_receipt_once(tmp_path):
    module = load(
        "portable_self_owned_upgrade",
        "skills/writer-agent/scripts/self_owned_article.py",
    )
    run_dir = tmp_path / "run-legacy"
    run_dir.mkdir()
    drafts = {}
    contracts = []
    for lang in ("ja", "en"):
        draft = run_dir / f"article-{lang}.md"
        draft.write_text(f"# {lang}\n\nportable body", encoding="utf-8")
        drafts[lang] = {
            "path": str(draft),
            "sha256": module.hashlib.sha256(draft.read_bytes()).hexdigest(),
        }
        contracts.append(
            {
                "lang": lang,
                "run_id": run_dir.name,
                "slug": f"portable-{lang}",
                "artifact_id": f"{run_dir.name}__self-owned__{lang}",
                "source_sha256": lang[0] * 64,
                "preview_sha256": "b" * 64,
                "paid_sha256": "c" * 64,
            }
        )
    publication = tmp_path / "publication.json"
    publication.write_text(
        json.dumps(
                {
                    "run_id": run_dir.name,
                    "run_dir": str(run_dir),
                    "topic_id": "topic",
                    "safety_status": "ALLOW",
                    "self_owned_base_url": "https://writer.example/site",
                    "drafts": drafts,
                }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "self-owned.json"
    articles = {}
    for contract in contracts:
        pair = f"self-owned/{contract['lang']}"
        articles[pair] = {
            "status": "live" if contract["lang"] == "en" else "intent",
            "target_kind": "aniccaai-slug",
            "target": contract["slug"],
            "artifact_id": contract["artifact_id"],
            "artifact_sha256": contract["source_sha256"],
            "preview_sha256": contract["preview_sha256"],
            "paid_sha256": contract["paid_sha256"],
        }
    articles["self-owned/en"]["receipt"] = {
        "live_url": "https://writer.example/site/blog/portable-en"
    }
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "run_dir": str(run_dir),
                "topic_id": "topic",
                "articles": articles,
            }
        ),
        encoding="utf-8",
    )
    store = module.SelfOwnedPublicationStore(state_path, tmp_path / "ledger.jsonl")
    store.prepare(publication, contracts, base_url="https://writer.example/site")
    upgraded = json.loads(state_path.read_text(encoding="utf-8"))
    assert upgraded["self_owned_base_url"] == "https://writer.example/site"
    assert all(
        entry["target_kind"] == "self-owned-slug"
        for entry in upgraded["articles"].values()
    )


def test_legacy_main_state_read_path_freezes_base_from_existing_receipt(
    tmp_path, monkeypatch
):
    module = load(
        "portable_main_state_upgrade",
        "skills/writer-agent/scripts/publication_resume.py",
    )
    state_path = tmp_path / "publication.json"
    ledger_path = tmp_path / "articles.jsonl"
    state = {"version": 1, "run_id": "run-1", "topic_id": "topic-1"}
    ledger_path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "topic_id": "topic-1",
                "platform": "self-owned",
                "published": True,
                "live_url": "https://writer.example/site/blog/existing-article",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv(
        "ARTICLE_SELF_OWNED_BASE_URL", "https://writer.example/site"
    )
    store = module.PublicationStore(state_path, ledger_path)
    upgraded = store.read()
    assert upgraded["self_owned_base_url"] == "https://writer.example/site"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["self_owned_base_url"] == "https://writer.example/site"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("ARTICLE_SELF_OWNED_BASE_URL", "https://other.example")
    with pytest.raises(module.InvariantError, match="does not match"):
        store.read()


def test_note_404_draft_readback_uses_writer_state_ledger(tmp_path, monkeypatch):
    module = load(
        "portable_publication_remote",
        "skills/writer-agent/scripts/publication_remote.py",
    )
    work = tmp_path / "writer/note-work"
    work.mkdir(parents=True)
    (work / "draft-ledger.json").write_text(
        json.dumps(
            {
                "article": {
                    "key": "nportable",
                    "account": "writer-note",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WRITER_STATE_DIR", str(tmp_path / "writer"))

    def not_public(_url, **_kwargs):
        raise module.urllib.error.HTTPError(_url, 404, "missing", {}, None)

    monkeypatch.setattr(module, "get_json", not_public)
    result = module.note(
        "nportable",
        {"destination_identities": {"note/ja": "writer-note"}},
    )
    assert result["status"] == "not-live"
    assert result["identity_source"] == "authenticated-note-draft-ledger"
