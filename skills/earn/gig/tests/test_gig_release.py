import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "gig_release.py"
SPEC = importlib.util.spec_from_file_location("gig_release_gc_test", SCRIPT)
gig_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gig_release)


def test_writer_notification_identity_has_no_personal_public_default(monkeypatch):
    monkeypatch.setattr(gig_release, "OVERRIDES", Path("/nonexistent/install.json"))
    manifest, table = gig_release.settings(Path("/release"))
    job = next(
        row for row in manifest["jobs"]
        if row["label"] == "ai.anicca.writer-opportunity-response"
    )
    environment = gig_release.plist_for(job, table)["EnvironmentVariables"]

    assert not environment.get("WRITER_GMAIL_ACCOUNT")
    assert table["WRITER_TELEGRAM_TARGET"] == ""


def test_shared_launchd_path_resolves_user_installed_agent_tools(monkeypatch):
    monkeypatch.setattr(gig_release, "OVERRIDES", Path("/nonexistent/install.json"))
    manifest, table = gig_release.settings(Path("/release"))
    job = next(
        row for row in manifest["jobs"]
        if row["label"] == "ai.anicca.life-manager-upwork-free-loop"
    )

    environment = gig_release.plist_for(job, table)["EnvironmentVariables"]

    assert environment["PATH"].split(":")[0] == f'{table["HOME"]}/.local/bin'


def test_gc_preserves_release_referenced_by_loaded_launchd_job(tmp_path, monkeypatch):
    current_sha = "a" * 40
    loaded_sha = "b" * 40
    rollback_sha = "c" * 40
    stale_sha = "d" * 40
    for sha in (current_sha, loaded_sha, rollback_sha, stale_sha):
        release = tmp_path / sha
        release.mkdir()
        (release / "marker").write_text(sha, encoding="utf-8")
    (tmp_path / "current").symlink_to(current_sha)

    monkeypatch.setattr(gig_release, "RELEASE_ROOT", tmp_path)
    monkeypatch.setattr(gig_release, "CURRENT_RELEASE", tmp_path / "current")
    monkeypatch.setattr(
        gig_release,
        "loaded_program",
        lambda label: ["/bin/bash", str(tmp_path / loaded_sha / "browser.sh")],
    )
    monkeypatch.setattr(
        gig_release.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    removed = gig_release.collect_old_releases()

    assert loaded_sha not in removed
    assert (tmp_path / loaded_sha).is_dir()


def test_gc_inspects_loaded_disk_cleanup_owner_while_idle(tmp_path, monkeypatch):
    current_sha = "a" * 40
    cleanup_sha = "b" * 40
    rollback_sha = "c" * 40
    stale_sha = "d" * 40
    for sha in (current_sha, cleanup_sha, rollback_sha, stale_sha):
        (tmp_path / sha).mkdir()
    (tmp_path / "current").symlink_to(current_sha)

    monkeypatch.setattr(gig_release, "RELEASE_ROOT", tmp_path)
    monkeypatch.setattr(gig_release, "CURRENT_RELEASE", tmp_path / "current")
    monkeypatch.setattr(
        gig_release,
        "loaded_program",
        lambda label: (
            ["/usr/bin/python3", str(tmp_path / cleanup_sha / "disk_cleanup.py")]
            if label == "ai.anicca.life-manager-disk-cleanup"
            else []
        ),
    )
    monkeypatch.setattr(
        gig_release.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    removed = gig_release.collect_old_releases()

    assert cleanup_sha not in removed
    assert (tmp_path / cleanup_sha).is_dir()


def test_gc_preserves_release_pinned_by_live_wrapper(tmp_path, monkeypatch):
    current_sha = "a" * 40
    pinned_sha = "b" * 40
    rollback_sha = "c" * 40
    stale_sha = "d" * 40
    for sha in (current_sha, pinned_sha, rollback_sha, stale_sha):
        release = tmp_path / sha
        release.mkdir()
        (release / "marker").write_text(sha, encoding="utf-8")
    (tmp_path / "current").symlink_to(current_sha)
    parent = tmp_path / pinned_sha / "skills" / "earn" / "gig" / "scripts" / "application_parent.py"
    parent.parent.mkdir(parents=True)
    parent.touch()

    monkeypatch.setattr(gig_release, "RELEASE_ROOT", tmp_path)
    monkeypatch.setattr(gig_release, "CURRENT_RELEASE", tmp_path / "current")
    monkeypatch.setattr(gig_release, "loaded_program", lambda _label: [])
    monkeypatch.setattr(gig_release.os, "getpid", lambda: 4242)
    monkeypatch.setattr(gig_release.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(
        gig_release.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    marker = gig_release.pin_release_for_process(parent)
    removed = gig_release.collect_old_releases()

    assert marker == tmp_path / ".pins" / f"4242-{pinned_sha}"
    assert pinned_sha not in removed
    assert (tmp_path / pinned_sha).is_dir()


def test_job_needs_activation_when_loaded_environment_is_stale(monkeypatch):
    job = {
        "label": "ai.anicca.hf-gig-reply-detector",
        "program": ["/usr/bin/python3", "{{RELEASE}}/reply_detector.py"],
        "env": {"GIG_NO_CONTACT_REGISTRY": "{{HOME}}/.config/no-contact.json"},
        "log_basename": "reply",
    }
    table = {
        "RELEASE": "/tmp/current",
        "HOME": "/Users/test",
        "GIG_LOG_DIR": "/tmp/logs",
    }
    desired = gig_release.plist_for(job, table)
    monkeypatch.setattr(gig_release, "loaded_program", lambda _label: desired["ProgramArguments"])
    monkeypatch.setattr(gig_release, "loaded_environment", lambda _label: {})

    assert gig_release.job_needs_activation(job, table) is True


def test_job_does_not_need_activation_when_program_and_environment_match(monkeypatch):
    job = {
        "label": "ai.anicca.hf-gig-reply-detector",
        "program": ["/usr/bin/python3", "{{RELEASE}}/reply_detector.py"],
        "env": {"GIG_NO_CONTACT_REGISTRY": "{{HOME}}/.config/no-contact.json"},
        "log_basename": "reply",
    }
    table = {
        "RELEASE": "/tmp/current",
        "HOME": "/Users/test",
        "GIG_LOG_DIR": "/tmp/logs",
    }
    desired = gig_release.plist_for(job, table)
    monkeypatch.setattr(gig_release, "loaded_program", lambda _label: desired["ProgramArguments"])
    monkeypatch.setattr(
        gig_release,
        "loaded_environment",
        lambda _label: desired["EnvironmentVariables"] | {"XPC_SERVICE_NAME": job["label"]},
    )

    assert gig_release.job_needs_activation(job, table) is False


def test_default_activation_never_restarts_a_busy_continuous_lane():
    label = "ai.anicca.hf-gig-reply-detector"

    assert gig_release.skip_busy_for_requested_activation(label, None) is True
    assert gig_release.skip_busy_for_requested_activation(label, {label}) is False


def test_upwork_browser_running_fence_uses_process_fallback(monkeypatch):
    label = "ai.anicca.life-manager-upwork-browser"
    ps_output = [""]

    def run(command, **_kwargs):
        if command[:2] == ["launchctl", "print"]:
            return SimpleNamespace(returncode=1, stdout="")
        if command == ["ps", "-axo", "command="]:
            return SimpleNamespace(returncode=0, stdout=ps_output[0])
        raise AssertionError(command)

    monkeypatch.setattr(gig_release.subprocess, "run", run)

    assert gig_release.is_running(label) is False
    ps_output[0] = "/Applications/Chromium --remote-debugging-port=9233\n"
    assert gig_release.is_running(label) is True


def test_coconala_browser_running_fence_uses_process_fallback(monkeypatch):
    label = "ai.anicca.hf-gig-browser"
    ps_output = [""]

    def run(command, **_kwargs):
        if command[:2] == ["launchctl", "print"]:
            return SimpleNamespace(returncode=1, stdout="")
        if command == ["ps", "-axo", "command="]:
            return SimpleNamespace(returncode=0, stdout=ps_output[0])
        raise AssertionError(command)

    monkeypatch.setattr(gig_release.subprocess, "run", run)

    assert gig_release.is_running(label) is False
    ps_output[0] = "/Applications/Chromium --remote-debugging-port=9223\n"
    assert gig_release.is_running(label) is True


def test_default_release_scope_is_only_the_three_unmigrated_coconala_business_lanes():
    assert gig_release.activation_labels(None) == {
        "ai.anicca.hf-gig-apply-direct",
        "ai.anicca.hf-gig-storefront-direct",
        "ai.anicca.hf-gig-reply-detector",
    }


def test_migrated_paid_job_is_not_owned_by_legacy_manifest():
    manifest = json.loads(gig_release.MANIFEST.read_text(encoding="utf-8"))
    assert all(row["label"] != "ai.anicca.hf-gig-paid-direct" for row in manifest["jobs"])
    assert "ai.anicca.hf-gig-paid-direct" not in gig_release.JOB_PROCESS_MARKERS


def test_migrated_writer_claim_job_is_not_owned_by_legacy_manifest():
    manifest = json.loads(gig_release.MANIFEST.read_text(encoding="utf-8"))
    assert all(row["label"] != "ai.anicca.writer-claim-loop" for row in manifest["jobs"])
    assert "ai.anicca.writer-claim-loop" not in gig_release.JOB_PROCESS_MARKERS
    installer = gig_release.REPO_ROOT / "skills/writer-agent/scripts/install-claim-loop-worker.sh"
    assert not installer.exists()


def test_migrated_writer_money_sync_job_is_not_owned_by_legacy_manifest():
    manifest = json.loads(gig_release.MANIFEST.read_text(encoding="utf-8"))
    assert all(row["label"] != "ai.anicca.writer-money-sync" for row in manifest["jobs"])
    assert "ai.anicca.writer-money-sync" not in gig_release.JOB_PROCESS_MARKERS
    installer = gig_release.REPO_ROOT / "skills/writer-agent/scripts/install-money-sync-worker.sh"
    assert not installer.exists()


def test_migrated_writer_opportunity_discovery_is_not_owned_by_legacy_manifest():
    manifest = json.loads(gig_release.MANIFEST.read_text(encoding="utf-8"))
    label = "ai.anicca.writer-opportunity-discovery"
    assert all(row["label"] != label for row in manifest["jobs"])
    assert label not in gig_release.JOB_PROCESS_MARKERS
    installer = gig_release.REPO_ROOT / "skills/writer-agent/scripts/install-opportunity-discovery-worker.sh"
    assert not installer.exists()


def test_explicit_release_scope_is_preserved():
    assert gig_release.activation_labels({"example.job"}) == {"example.job"}


def test_watch_publishes_release_without_reactivating_launchd(monkeypatch, tmp_path):
    sha = "a" * 40
    release = tmp_path / sha
    release.mkdir()
    published = []
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "watch"])
    monkeypatch.setattr(
        gig_release, "git",
        lambda *args, **_kwargs: sha if args[:2] == ("rev-parse", "origin/main") else "",
    )
    monkeypatch.setattr(gig_release, "current_sha", lambda: "")
    monkeypatch.setattr(gig_release, "build", lambda _sha: release)
    monkeypatch.setattr(gig_release, "publish", published.append)
    monkeypatch.setattr(gig_release, "collect_old_releases", lambda: [])
    monkeypatch.setattr(gig_release, "settings", lambda _release: ({}, {}))
    monkeypatch.setattr(gig_release, "job_needs_activation", lambda _job, _table: True)
    monkeypatch.setattr(gig_release, "control_plane_available", lambda: True)
    monkeypatch.setattr(gig_release, "require_control_plane", lambda: True)
    monkeypatch.setattr(
        gig_release, "activate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("watch must not mutate launchd")),
    )

    assert gig_release.main() == 0
    assert published == [release]
