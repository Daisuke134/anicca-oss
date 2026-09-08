"""Shared PUBLISH GUARD (VSDD F3 FIND-007/008). Every script that clicks 投稿する/更新する/公開する/申請する
MUST call assert_publish_allowed() immediately before that click, so NO script on disk can push a note article
public unless publishing is EXPLICITLY enabled.

Threat model (honest — see spec FIND-009): this is strong defense-in-depth against ACCIDENTAL publishing and
NORMAL operation. It is NOT an absolute sandbox against a fully-compromised `--dangerously-skip-permissions`
Bash agent (which is omnipotent on the host and could set env + create the sentinel). Those threats are mitigated
elsewhere: NOTE_TOPIC is allow-list sanitized before reaching the agent, the agent runs OUR trusted prompt, and
true isolation (container/VM) is future work. The scheduled/unattended path sets NOTE_FORCE_DRAFT=1 and removes
the sentinel, so under normal operation it can only ever DRAFT.

To ENABLE a (manual, human) publish, BOTH must hold:
  1. env: NOTE_MODE=go  AND  NOTE_FORCE_DRAFT != 1
  2. a fresh sentinel file $WRITER_STATE_DIR/note-work/.PUBLISH_ENABLED (created by `publish-to-note.sh enable-publish`,
     valid 10 min — a deliberate act, stronger than an env var alone).
"""
import os, sys, time

SENTINEL = os.path.join(
    os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")),
    "note-work/.PUBLISH_ENABLED",
)
SENTINEL_TTL = 600  # seconds


def publish_allowed():
    env_ok = (os.environ.get("NOTE_MODE") == "go") and (os.environ.get("NOTE_FORCE_DRAFT") != "1")
    try:
        sentinel_ok = os.path.exists(SENTINEL) and (time.time() - os.path.getmtime(SENTINEL) < SENTINEL_TTL)
    except OSError:
        sentinel_ok = False
    return env_ok and sentinel_ok, env_ok, sentinel_ok


def assert_publish_allowed():
    ok, env_ok, sentinel_ok = publish_allowed()
    if not ok:
        print(f"PUBLISH GUARD: BLOCKED — draft-safe, no 投稿/更新/公開 click "
              f"(env_ok={env_ok}, sentinel_ok={sentinel_ok}). "
              f"To publish (manual): publish-to-note.sh enable-publish, then run with NOTE_MODE=go.")
        sys.exit(0)
    print("PUBLISH GUARD: allowed (NOTE_MODE=go + fresh sentinel).")
