from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "publication_resume.py"
SPEC = importlib.util.spec_from_file_location("publication_resume_identity_quarantine", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

IDENTITIES = {
    "note/ja": "writer-note",
    "zenn-article/ja": "writer-zenn",
    "devto/en": "writer_devto",
    "substack/ja": "writer-ja.substack.com",
    "substack/en": "writer-en.substack.com",
    "x-article/ja": "writer_x",
    "x-article/en": "writer_x",
    "x-post/ja": "writer_x",
}


def _state(tmp_path: Path) -> tuple[Path, Path, dict]:
    run = tmp_path / "runs" / "daily-2026-08-21"
    gates = run / "gates"
    gates.mkdir(parents=True)
    ledger = tmp_path / "articles.jsonl"
    state_path = gates / "publication-state.json"
    identities = dict(IDENTITIES)
    identities["substack/en"] = identities["substack/ja"]
    pairs = {
        pair: {
            "platform": pair.split("/", 1)[0],
            "lang": pair.split("/", 1)[1],
            "status": "skipped" if pair in MODULE.DORMANT_PAIRS else "intent",
            **({} if pair in MODULE.DORMANT_PAIRS else {"target_kind": "substack-draft-id", "target": "123"} if pair.startswith("substack/") else {"target_kind": "note-key", "target": "abcd"} if pair == "note/ja" else {"target_kind": "x-draft-url", "target": "https://x.com/compose/articles/edit/12345678"}),
        }
        for pair in MODULE.SUPPORTED_PAIRS
    }
    for pair in MODULE.DORMANT_PAIRS:
        pairs[pair]["skip_receipt"] = {
            "type": "dormant-destination",
            "pair": pair,
            "reason": "dormant-destination",
            "slo": "not-applicable",
            "recorded_at": "2026-08-21T00:00:00Z",
        }
    payload = {
        "version": 1,
        "publication_contract": "active-four",
        "run_id": run.name,
        "run_dir": str(run),
        "state_path": str(state_path),
        "ledger_path": str(ledger),
        "topic_id": "topic-1",
        "destination_identities": identities,
        "safety_status": "ALLOW",
        "drafts": {"ja": {"path": str(run / "article-ja.md"), "sha256": "0" * 64}, "en": {"path": str(run / "article-en.md"), "sha256": "0" * 64}},
        "media": {"headline_image": {"path": str(run / "headline-image.png")}, "body_assets": [{"path": str(run / "body.png")}]},
        "pairs": pairs,
        "resume_attempts": 0,
        "max_resume_attempts": 2,
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path, ledger, payload


def test_unquarantined_conflation_fails_closed() -> None:
    identities = dict(IDENTITIES)
    identities["substack/en"] = identities["substack/ja"]
    with pytest.raises(MODULE.InvariantError, match="unquarantined"):
        MODULE.validate_persisted_destination_identities(
            {"destination_identities": identities, "pairs": {"substack/en": {"status": "intent"}}}
        )


def test_quarantined_identity_set_must_remain_complete() -> None:
    identities = dict(IDENTITIES)
    identities["substack/en"] = identities["substack/ja"]
    identities.pop("x-post/ja")
    state = {
        "destination_identities": identities,
        "identity_quarantine": {
            "version": 1,
            "pair": "substack/en",
            "reason": MODULE.IDENTITY_CONFLICT_REASON,
            "previous_identity": "writer-ja.substack.com",
            "recorded_at": "2026-08-21T00:00:00Z",
        },
        "pairs": {"substack/en": {"status": "unavailable", "error": MODULE.IDENTITY_CONFLICT_REASON}},
    }
    with pytest.raises(MODULE.InvariantError, match="unexpected pair set"):
        MODULE.validate_persisted_destination_identities(state)


def test_quarantine_is_idempotent_and_allows_persisted_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path, ledger, _ = _state(tmp_path)
    store = MODULE.PublicationStore(state_path, ledger)
    monkeypatch.setattr(store, "_validate_layout", lambda *args, **kwargs: Path(tmp_path / "runs" / "daily-2026-08-21"))

    remote = {
        "status": "not-live",
        "verified": True,
        "destination_identity": "writer-ja.substack.com",
        "identity_verified": True,
        "identity_source": "protected-substack-authenticated-draft-api",
        "source": "substack-draft-api",
    }
    first = store.quarantine_identity_conflict("substack/en", "123", remote)
    assert first["status"] == "unavailable"
    assert first["error"] == MODULE.IDENTITY_CONFLICT_REASON
    MODULE.validate_persisted_destination_identities(store.read())

    second = store.quarantine_identity_conflict("substack/en", "123", remote)
    assert second == first


def test_quarantine_refuses_live_readback_or_any_same_run_ledger_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, ledger, _ = _state(tmp_path)
    store = MODULE.PublicationStore(state_path, ledger)
    monkeypatch.setattr(store, "_validate_layout", lambda *args, **kwargs: Path(tmp_path / "runs" / "daily-2026-08-21"))
    live = {"status": "live", "verified": True}
    with pytest.raises(MODULE.InvariantError, match="not-live proof"):
        store.quarantine_identity_conflict("substack/en", "123", live)

    ledger.write_text(
        json.dumps({"run_id": "daily-2026-08-21", "topic_id": "different", "platform": "substack", "lang": "en"}) + "\n",
        encoding="utf-8",
    )
    remote = {
        "status": "not-live",
        "verified": True,
        "destination_identity": "writer-ja.substack.com",
        "identity_verified": True,
        "identity_source": "protected-substack-authenticated-draft-api",
        "source": "substack-draft-api",
    }
    with pytest.raises(MODULE.InvariantError, match="ledger row"):
        store.quarantine_identity_conflict("substack/en", "123", remote)


def test_quarantine_allows_explicit_no_effect_staging_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, ledger, _ = _state(tmp_path)
    ledger.write_text(
        json.dumps(
            {
                "run_id": "daily-2026-08-21",
                "topic_id": "topic-1",
                "platform": "substack",
                "lang": "en",
                "state": "staged:pre-publication",
                "published": False,
                "verified_logged_in": False,
                "live_url": None,
                "public_id": None,
                "receipt": None,
                "published_at": None,
                "reality_gate": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store = MODULE.PublicationStore(state_path, ledger)
    monkeypatch.setattr(store, "_validate_layout", lambda *args, **kwargs: Path(tmp_path / "runs" / "daily-2026-08-21"))
    remote = {
        "status": "not-live",
        "verified": True,
        "destination_identity": "writer-ja.substack.com",
        "identity_verified": True,
        "identity_source": "protected-substack-authenticated-draft-api",
        "source": "substack-draft-api",
    }
    result = store.quarantine_identity_conflict("substack/en", "123", remote)
    assert result["status"] == "unavailable"


def test_quarantined_en_identity_migrates_only_into_configured_reinitialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, ledger, _ = _state(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pairs"].pop("substack/en")
    state["reinitialization_pairs"] = ["substack/en"]
    state["identity_quarantine"] = {
        "version": 1,
        "pair": "substack/en",
        "reason": MODULE.IDENTITY_CONFLICT_REASON,
        "previous_identity": "writer-ja.substack.com",
        "recorded_at": "2026-08-21T00:00:00Z",
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    configured = {
        "NOTE_URLNAME": "writer-note",
        "ZENN_ACCOUNT": "writer-zenn",
        "DEVTO_ACCOUNT_HANDLE": "writer_devto",
        "SUBSTACK_PUBLICATION_JA": "writer-ja.substack.com",
        "SUBSTACK_PUBLICATION_EN": "writer-new-en.substack.com",
        "X_ACCOUNT_HANDLE": "writer_x",
    }
    for key, value in configured.items():
        monkeypatch.setenv(key, value)
    store = MODULE.PublicationStore(state_path, ledger)
    monkeypatch.setattr(
        store,
        "_validate_layout",
        lambda *args, **kwargs: Path(tmp_path / "runs" / "daily-2026-08-21"),
    )

    result = store.migrate_quarantined_substack_en_identity(
        "writer-new-en.substack.com"
    )

    persisted = store.read()
    assert result["previous_identity"] == "writer-ja.substack.com"
    assert result["new_identity"] == "writer-new-en.substack.com"
    assert persisted["destination_identities"]["substack/en"] == "writer-new-en.substack.com"
    assert "substack/en" not in persisted["pairs"]
    MODULE.validate_persisted_destination_identities(persisted)


@pytest.mark.parametrize(
    "row_update",
    [
        {"provider_receipt": {"live_url": "https://example.test/live"}},
        {"post_id": "123"},
        {"state": "staged:published"},
        {"state": "unavailable:live"},
        {"state": "staged:republished"},
        {"state": "unavailable:posted"},
    ],
)
def test_quarantine_rejects_unknown_or_effect_capable_ledger_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row_update: dict,
) -> None:
    state_path, ledger, _ = _state(tmp_path)
    row = {
        "run_id": "daily-2026-08-21",
        "topic_id": "topic-1",
        "platform": "substack",
        "lang": "en",
        "state": "staged:pre-publication",
        "published": False,
        "verified_logged_in": False,
        "live_url": None,
        "public_id": None,
        "receipt": None,
        "published_at": None,
        "reality_gate": None,
    }
    row.update(row_update)
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    store = MODULE.PublicationStore(state_path, ledger)
    monkeypatch.setattr(store, "_validate_layout", lambda *args, **kwargs: Path(tmp_path / "runs" / "daily-2026-08-21"))
    remote = {
        "status": "not-live",
        "verified": True,
        "destination_identity": "writer-ja.substack.com",
        "identity_verified": True,
        "identity_source": "protected-substack-authenticated-draft-api",
        "source": "substack-draft-api",
    }
    with pytest.raises(MODULE.InvariantError, match="effect-capable"):
        store.quarantine_identity_conflict("substack/en", "123", remote)


def test_guard_cli_reaches_legacy_migration_before_normal_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, ledger, _ = _state(tmp_path)
    guard_path = Path(__file__).parents[1] / "scripts" / "publication-guard.py"
    spec = importlib.util.spec_from_file_location("publication_guard_quarantine", guard_path)
    guard = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(guard)

    base_store = guard.PublicationStore

    class MigrationStore(base_store):
        def _validate_layout(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return Path(tmp_path / "runs" / "daily-2026-08-21")

    remote = {
        "status": "not-live",
        "verified": True,
        "destination_identity": "writer-ja.substack.com",
        "identity_verified": True,
        "identity_source": "protected-substack-authenticated-draft-api",
        "source": "substack-draft-api",
    }
    monkeypatch.setattr(guard, "PublicationStore", MigrationStore)
    monkeypatch.setattr(guard, "probe", lambda *args, **kwargs: remote)
    monkeypatch.setenv("ARTICLE_RUN_DIR", str(state_path.parent.parent))
    monkeypatch.setenv("ARTICLE_PUBLICATION_STATE", str(state_path))
    monkeypatch.setenv("ARTICLE_LEDGER", str(ledger))
    monkeypatch.setattr(
        sys,
        "argv",
        ["publication-guard.py", "quarantine-identity-conflict", "--pair", "substack/en"],
    )
    guard.main()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["pairs"]["substack/en"]["status"] == "unavailable"
