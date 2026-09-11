import json
import pathlib
import shutil
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ENGINE = HERE.parent
sys.path.insert(0, str(HERE))

from intent_store import IntentStore, build_intent
from publish_cli import run_postiz_operation
from test_intent_store import fixture_intent
from test_postiz_adapter import FakePostiz


def registered(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    intent = store.register(fixture_intent(tmp_path))["intent"]
    return store, intent


def test_production_flag_is_required_before_preflight_or_provider_call(tmp_path):
    store, intent = registered(tmp_path)
    client = FakePostiz()
    with pytest.raises(ValueError, match="explicit --production flag required"):
        run_postiz_operation(
            db_path=store.path, publish_key=intent["publish_key"], operation="upload",
            approvals_path=tmp_path / "missing.jsonl", owner="worker",
            now="2026-08-02T00:00:00Z", ttl_seconds=300, engine=ENGINE,
            production=False, client=client)
    assert client.upload_calls == client.create_calls == client.promote_calls == 0


def test_inactive_account_blocks_before_provider_call(tmp_path):
    store, intent = registered(tmp_path)
    client = FakePostiz()
    with pytest.raises(ValueError, match="account is not approved_active"):
        run_postiz_operation(
            db_path=store.path, publish_key=intent["publish_key"], operation="upload",
            approvals_path=tmp_path / "missing.jsonl", owner="worker",
            now="2026-08-02T00:00:00Z", ttl_seconds=300, engine=ENGINE,
            production=True, client=client)
    assert client.upload_calls == client.create_calls == client.promote_calls == 0


def test_approved_route_uploads_once_through_fenced_operation(tmp_path):
    engine = tmp_path / "engine"
    shutil.copytree(ENGINE / "registry", engine / "registry")
    account_path = engine / "registry/accounts/tiktok.monk_anicca.json"
    account = json.loads(account_path.read_text())
    account["status"] = "approved_active"
    account_path.write_text(json.dumps(account))
    asset = tmp_path / "approved.mp4"
    asset.write_bytes(b"approved-video")
    intent = build_intent(
        experiment_id="experiment.123", creative_id="creative.123", product_id="ebook-en",
        account_id="tiktok.monk_anicca", hook_id="hook.123",
        renderer_id="heygen-avatar-iv", adapter="postiz", asset_path=asset,
        caption="Read The Anicca Reset https://aniccaai.com/go/ee_testtoken ee_testtoken",
        attribution_token="ee_testtoken", scheduled_at="2026-08-02T01:00:00Z",
        integration_id=account["publisher_integration_id"], platform="tiktok",
        native_handle=account["native_handle"],
        provider_settings=account["publisher_settings"],
        visual_approval_id="visual.accepted.123")
    store = IntentStore(tmp_path / "jobs.sqlite3")
    store.register(intent)
    approvals = tmp_path / "approvals.jsonl"
    approvals.write_text(json.dumps({
        "approval_id": "visual.accepted.123", "status": "accepted",
        "asset_sha256": intent["asset_sha256"], "product_id": "ebook-en",
        "account_id": "tiktok.monk_anicca"}) + "\n")
    client = FakePostiz(integrations=[{
        "id": account["publisher_integration_id"],
        "identifier": account["publisher_provider"],
        "profile": account["native_handle"],
        "disabled": False,
    }])
    first = run_postiz_operation(
        db_path=store.path, publish_key=intent["publish_key"], operation="upload",
        approvals_path=approvals, owner="worker", now="2026-08-02T00:00:00Z",
        ttl_seconds=300, engine=engine, production=True, client=client,
        media_probe=lambda _: {"duration_seconds": 17.4, "format_names": ["mp4"],
            "video_codec": "h264", "audio_codec": "aac", "width": 720, "height": 1280})
    second = run_postiz_operation(
        db_path=store.path, publish_key=intent["publish_key"], operation="upload",
        approvals_path=approvals, owner="worker", now="2026-08-02T00:00:01Z",
        ttl_seconds=300, engine=engine, production=True, client=client,
        media_probe=lambda _: {"duration_seconds": 17.4, "format_names": ["mp4"],
            "video_codec": "h264", "audio_codec": "aac", "width": 720, "height": 1280})
    assert first["state"] == second["state"] == "accepted"
    assert client.upload_calls == 1
    assert client.integration_calls == 2
    client.integrations[0]["disabled"] = True
    with pytest.raises(ValueError, match="publisher route is not ready"):
        run_postiz_operation(
            db_path=store.path, publish_key=intent["publish_key"], operation="draft",
            approvals_path=approvals, owner="worker", now="2026-08-02T00:00:02Z",
            ttl_seconds=300, engine=engine, production=True, client=client,
            media_probe=lambda _: {"duration_seconds": 17.4, "format_names": ["mp4"],
                "video_codec": "h264", "audio_codec": "aac", "width": 720, "height": 1280})
    assert client.create_calls == 0
