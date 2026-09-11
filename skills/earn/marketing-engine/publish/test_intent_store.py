import hashlib
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from intent_store import (IntentStore, build_intent, caption_sha256,
                          normalize_caption)


def fixture_intent(tmp_path):
    asset = tmp_path / "approved.mp4"
    asset.write_bytes(b"approved-video")
    return build_intent(
        experiment_id="experiment.123", creative_id="creative.123",
        product_id="ebook-en", account_id="instagram.anicca_en",
        hook_id="hook.123", renderer_id="heygen-avatar-iv", adapter="postiz",
        asset_path=asset, caption="A calm lesson. ee_testtoken",
        attribution_token="ee_testtoken", scheduled_at="2026-08-02T01:00:00Z",
        integration_id="cmn8y95rg02d2qx0y09bbk5pb", platform="instagram",
        native_handle="anicca.en",
        provider_settings={"__type": "instagram-standalone", "post_type": "post",
                           "is_trial_reel": False, "collaborators": []},
        visual_approval_id="visual.accepted.123",
    )


def test_caption_normalization_is_stable_but_not_lossy_for_identity():
    assert normalize_caption(" A  calm\nlesson. ") == "A calm lesson."
    assert caption_sha256(" A  calm\nlesson. ") == caption_sha256("A calm lesson.")


def test_register_exact_replay_and_conflict(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    intent = fixture_intent(tmp_path)
    assert store.register(intent)["created"] is True
    assert store.register(intent)["created"] is False
    with pytest.raises(ValueError, match="conflicting intent replay"):
        store.register(intent | {"visual_approval_id": "visual.accepted.other"})


def test_provider_settings_change_publication_identity(tmp_path):
    first = fixture_intent(tmp_path)
    changed = build_intent(
        experiment_id="experiment.123", creative_id="creative.123",
        product_id="ebook-en", account_id="instagram.anicca_en",
        hook_id="hook.123", renderer_id="heygen-avatar-iv", adapter="postiz",
        asset_path=pathlib.Path(first["asset_path"]), caption=first["caption"],
        attribution_token="ee_testtoken", scheduled_at="2026-08-02T01:00:00Z",
        integration_id="cmn8y95rg02d2qx0y09bbk5pb", platform="instagram",
        native_handle="anicca.en",
        provider_settings={"__type": "instagram-standalone", "post_type": "post",
                           "is_trial_reel": True, "collaborators": []},
        visual_approval_id="visual.accepted.123")
    assert first["publish_key"] != changed["publish_key"]


def test_native_handle_change_changes_publication_identity(tmp_path):
    first = fixture_intent(tmp_path)
    changed = build_intent(
        experiment_id=first["experiment_id"], creative_id=first["creative_id"],
        product_id=first["product_id"], account_id=first["account_id"],
        hook_id=first["hook_id"], renderer_id=first["renderer_id"], adapter=first["adapter"],
        asset_path=pathlib.Path(first["asset_path"]), caption=first["caption"],
        attribution_token=first["attribution_token"], scheduled_at=first["scheduled_at"],
        integration_id=first["integration_id"], platform=first["platform"],
        native_handle="different.handle", provider_settings=first["provider_settings"],
        visual_approval_id=first["visual_approval_id"])
    assert first["publish_key"] != changed["publish_key"]


def test_one_account_slot_cannot_have_two_intents(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    intent = fixture_intent(tmp_path)
    store.register(intent)
    second = build_intent(
        experiment_id="experiment.other", creative_id=intent["creative_id"],
        product_id=intent["product_id"], account_id=intent["account_id"],
        hook_id=intent["hook_id"], renderer_id=intent["renderer_id"], adapter=intent["adapter"],
        asset_path=pathlib.Path(intent["asset_path"]), caption=intent["caption"],
        attribution_token=intent["attribution_token"], scheduled_at=intent["scheduled_at"],
        integration_id=intent["integration_id"], platform=intent["platform"],
        native_handle=intent["native_handle"],
        provider_settings=intent["provider_settings"],
        visual_approval_id=intent["visual_approval_id"])
    with pytest.raises(ValueError, match="account slot already reserved"):
        store.register(second)


@pytest.mark.parametrize("field,value,error", [
    ("publish_key", "publication.forged", "publish key mismatch"),
    ("asset_sha256", "0" * 64, "asset hash mismatch"),
    ("caption_sha256", "0" * 64, "caption hash mismatch"),
])
def test_register_rejects_forged_derived_identity(tmp_path, field, value, error):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError, match=error):
        store.register(fixture_intent(tmp_path) | {field: value})


def test_two_workers_and_expired_fencing(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    key = store.register(fixture_intent(tmp_path))["intent"]["publish_key"]
    first = store.acquire(key, owner="worker-a", now="2026-08-01T00:00:00Z", ttl_seconds=60)
    assert first["fence"] == 1
    assert store.acquire(key, owner="worker-b", now="2026-08-01T00:00:30Z", ttl_seconds=60) is None
    second = store.acquire(key, owner="worker-b", now="2026-08-01T00:01:01Z", ttl_seconds=60)
    assert second["fence"] == 2
    with pytest.raises(ValueError, match="stale lease fence"):
        store.begin_dispatch(key, owner="worker-a", fence=1, operation="create_draft",
                             request={"type": "draft"}, now="2026-08-01T00:01:02Z")


def test_crash_before_dispatch_allows_one_fenced_successor_dispatch(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    key = store.register(fixture_intent(tmp_path))["intent"]["publish_key"]
    store.acquire(key, owner="crashed-worker", now="2026-08-01T00:00:00Z", ttl_seconds=1)
    successor = store.acquire(key, owner="successor", now="2026-08-01T00:00:02Z",
                              ttl_seconds=60)
    first = store.begin_dispatch(key, owner="successor", fence=successor["fence"],
                                 operation="upload_media", request={"asset": "exact"},
                                 now="2026-08-01T00:00:03Z")
    replay = store.begin_dispatch(key, owner="successor", fence=successor["fence"],
                                  operation="upload_media", request={"asset": "exact"},
                                  now="2026-08-01T00:00:04Z")
    assert first["created"] is True
    assert replay["created"] is False
    assert first["attempt_id"] == replay["attempt_id"]


def test_dispatch_is_durable_and_uncertain_never_redispatches(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    key = store.register(fixture_intent(tmp_path))["intent"]["publish_key"]
    lease = store.acquire(key, owner="worker-a", now="2026-08-01T00:00:00Z", ttl_seconds=60)
    attempt = store.begin_dispatch(key, owner="worker-a", fence=lease["fence"],
                                   operation="create_draft", request={"type": "draft"},
                                   now="2026-08-01T00:00:01Z")
    assert attempt["state"] == "dispatching"
    store.mark_uncertain(attempt["attempt_id"], "timeout after request write",
                         now="2026-08-01T00:00:02Z")
    replay = store.begin_dispatch(key, owner="worker-a", fence=lease["fence"],
                                  operation="create_draft", request={"type": "draft"},
                                  now="2026-08-01T00:00:03Z")
    assert replay["state"] == "uncertain"
    assert replay["created"] is False


def test_stale_worker_may_record_received_response_but_not_dispatch(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    key = store.register(fixture_intent(tmp_path))["intent"]["publish_key"]
    lease = store.acquire(key, owner="worker-a", now="2026-08-01T00:00:00Z", ttl_seconds=1)
    attempt = store.begin_dispatch(key, owner="worker-a", fence=lease["fence"],
                                   operation="create_draft", request={"type": "draft"},
                                   now="2026-08-01T00:00:00Z")
    store.acquire(key, owner="worker-b", now="2026-08-01T00:00:02Z", ttl_seconds=60)
    stored = store.record_response(attempt["attempt_id"], {"postId": "post-123"},
                                   now="2026-08-01T00:00:03Z")
    assert stored["state"] == "accepted"
    assert store.get(key)["provider_post_id"] == "post-123"


def test_upload_receipt_persists_media_id_and_path(tmp_path):
    store = IntentStore(tmp_path / "jobs.sqlite3")
    key = store.register(fixture_intent(tmp_path))["intent"]["publish_key"]
    lease = store.acquire(key, owner="worker", now="2026-08-01T00:00:00Z", ttl_seconds=60)
    attempt = store.begin_dispatch(key, owner="worker", fence=lease["fence"],
                                   operation="upload_media",
                                   request={"asset_sha256": "a" * 64},
                                   now="2026-08-01T00:00:01Z")
    store.record_response(attempt["attempt_id"], {
        "id": "media-123", "name": "approved.mp4",
        "path": "https://uploads.postiz.com/approved.mp4",
        "organizationId": "org-123", "createdAt": "2026-08-02T00:00:00Z",
        "updatedAt": "2026-08-02T00:00:00Z",
    }, now="2026-08-01T00:00:02Z")
    current = store.get(key)
    assert current["provider_media_id"] == "media-123"
    assert current["provider_media_path"] == "https://uploads.postiz.com/approved.mp4"
