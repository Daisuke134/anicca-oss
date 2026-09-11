#!/usr/bin/env python3
"""SHARED-1 (INV-6) replacement for test_post_reel_single_print.py (post_reel.py retired,
web composer was a structural dead end -- IG silently dropped its automated posts).

Verifies poster.py's single-JSON-print contract: every caller (earn/clip's run.sh,
self_heal.py and earn/clip-promote's run.sh parse stdout expecting EXACTLY
ONE JSON line per invocation. main()'s early-return sites (login-fail, dry-mode-success,
verify-only) and verify_only_main() are exercised here with instagrapi mocked out (no real
network/browser), mirroring the mocking style the retired post_reel.py test used.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

MARKETING_ENGINE_PATH = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + "/marketing-engine"
sys.path.insert(0, MARKETING_ENGINE_PATH)


def _fresh_import():
    if "poster" in sys.modules:
        del sys.modules["poster"]
    import poster  # noqa
    return poster


def _mktemp_files():
    video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    video.write(b"fake"); video.close()
    cap = tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False)
    cap.write("caption text"); cap.close()
    return video.name, cap.name


def _run_main(post, argv):
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["poster.py"] + argv
    try:
        with redirect_stdout(buf):
            post.main()
    finally:
        sys.argv = old_argv
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return lines, [json.loads(ln) for ln in lines]


class TestSinglePrint(unittest.TestCase):
    def test_login_failure_prints_exactly_once(self):
        post = _fresh_import()
        video, cap = _mktemp_files()
        post.login_resilient = mock.MagicMock(return_value=False)
        with mock.patch("instagrapi.Client") as MockClient:
            MockClient.return_value = mock.MagicMock()
            lines, parsed = _run_main(
                post, ["--video", video, "--caption-file", cap, "--handle", "h", "--live"])
        self.assertEqual(len(parsed), 1, f"expected exactly 1 JSON line, got {len(parsed)}: {lines}")
        self.assertEqual(parsed[0]["outcome"], "failed")

    def test_dry_mode_prints_exactly_once(self):
        post = _fresh_import()
        video, cap = _mktemp_files()
        post.login_resilient = mock.MagicMock(return_value=True)
        with mock.patch("instagrapi.Client") as MockClient:
            MockClient.return_value = mock.MagicMock()
            lines, parsed = _run_main(
                post, ["--video", video, "--caption-file", cap, "--handle", "h"])
        self.assertEqual(len(parsed), 1, f"expected exactly 1 JSON line, got {len(parsed)}: {lines}")
        self.assertEqual(parsed[0]["outcome"], "dry")

    def test_accounts_path_reaches_login_policy(self):
        post = _fresh_import()
        video, cap = _mktemp_files()
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as accounts:
            json.dump([{"handle": "h", "status": "warming", "session_owner": "instagrapi"}], accounts)
            accounts_path = accounts.name
        post.login_resilient = mock.MagicMock(return_value=False)
        with mock.patch("instagrapi.Client") as MockClient:
            MockClient.return_value = mock.MagicMock()
            _run_main(post, ["--video", video, "--caption-file", cap, "--handle", "h",
                             "--accounts-path", accounts_path])
        self.assertEqual(post.login_resilient.call_args.kwargs["accounts_path"], accounts_path)

    def test_verify_only_returns_reels_list(self):
        # unit-level: verify_only_main() maps instagrapi Media objects -> href strings,
        # matching the {"ok","reels"} shape self_heal.py/video-run.sh already parse (SHARED-1 INV-3/4).
        post = _fresh_import()
        fake_media = mock.MagicMock()
        fake_media.model_dump.return_value = {"code": "ABC123", "product_type": "clips"}
        fake_cl = mock.MagicMock()
        fake_cl.user_id = "999"
        fake_cl.user_medias.return_value = [fake_media]
        post.login_resilient = mock.MagicMock(return_value=True)
        result = post.verify_only_main("h", "9222", client_factory=lambda: fake_cl)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reels"], ["/h/reel/ABC123/"])

    def test_verify_only_prints_exactly_once_via_main(self):
        post = _fresh_import()
        with mock.patch.object(
            post, "verify_only_main",
            return_value={"handle": "h", "verify_only": True, "ok": True, "reels": ["/h/reel/A/"]},
        ):
            lines, parsed = _run_main(post, ["--handle", "h", "--verify-only"])
        self.assertEqual(len(parsed), 1, f"expected exactly 1 JSON line, got {len(parsed)}: {lines}")
        self.assertIn("reels", parsed[0])
        self.assertTrue(parsed[0]["ok"])


if __name__ == "__main__":
    unittest.main()
