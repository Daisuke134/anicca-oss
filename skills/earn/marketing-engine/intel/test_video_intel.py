import json
import importlib.util
from importlib.machinery import SourceFileLoader
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_intel import (VideoIntelError, discover_videos, ingest_transcripts,
                         load_video_registry, whisper_transcriber, yt_dlp_downloader)


def registry_payload():
    return {
        "schema_version": "marketing.video-source-registry.v1",
        "limits": {
            "max_posts_per_source": 20,
            "max_downloads_per_run": 1,
            "max_download_bytes": 30000000,
            "max_duration_seconds": 180,
        },
        "transcription": {"engine": "openai-whisper", "model": "base", "device": "cpu"},
        "qualification": {
            "post_min_views": 15000,
            "creator_average_views": 25000,
            "creator_floor_views": 15000,
        },
        "sources": [{
            "id": "video.tiktok.itsyangmun.en",
            "platform": "tiktok",
            "handle": "itsyangmun",
            "profile_url": "https://www.tiktok.com/@itsyangmun",
            "language": "en",
            "product_ids": ["ebook-en"],
            "enabled": True,
        }],
    }


def playlist():
    return {
        "_type": "playlist",
        "entries": [
            {
                "id": "7609309883912965390",
                "webpage_url": "https://www.tiktok.com/@itsyangmun/video/7609309883912965390",
                "uploader": "itsyangmun",
                "timestamp": 1760000000,
                "duration": 67.66,
                "view_count": 341300,
                "like_count": 5338,
                "comment_count": 142,
                "title": "Staying at home too much",
            },
            {
                "id": "2",
                "webpage_url": "https://www.tiktok.com/@itsyangmun/video/2",
                "uploader": "itsyangmun",
                "timestamp": 1760000001,
                "duration": 52,
                "view_count": 921,
                "like_count": None,
                "comment_count": 2,
                "title": "low cohort fixture",
            },
        ],
    }


class VideoIntelTest(unittest.TestCase):
    def test_transcriber_uses_segment_timestamps_without_expensive_word_timestamps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "source.mp4"
            media.write_bytes(b"media")
            generated = root / "source.json"
            generated.write_text('{"segments": []}\n')
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch("video_intel.subprocess.run", return_value=completed) as run:
                target = whisper_transcriber(
                    media, root, {"model": "base", "device": "cpu"}, "ja")
            self.assertEqual(target, root / "transcript.json")
            self.assertNotIn("--word_timestamps", run.call_args.args[0])

    def test_downloader_prefers_small_mp4_and_reports_success_without_a_file(self):
        completed = mock.Mock(returncode=0, stdout="larger than max-filesize", stderr="")
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("video_intel.subprocess.run", return_value=completed) as run:
            with self.assertRaisesRegex(RuntimeError, "larger than max-filesize"):
                yt_dlp_downloader(
                    {"native_url": "https://www.tiktok.com/@owner/video/1"},
                    Path(temp), {"max_download_bytes": 30_000_000})
        self.assertEqual(run.call_args.args[0][4:6], ["-f", "w[ext=mp4]/w"])

    def test_registry_rejects_mixed_product_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            data = registry_payload()
            data["sources"][0]["product_ids"] = ["ebook-en", "anicca-ios"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(VideoIntelError, "one product"):
                load_video_registry(path)

    def test_discovery_preserves_native_metrics_and_numeric_floors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / "video-sources.json"
            registry.write_text(json.dumps(registry_payload()), encoding="utf-8")
            result = discover_videos(
                registry, root / "intel", root / "evidence",
                collector=lambda source, limit: playlist(),
                observed_at="2026-08-01T15:00:00Z",
                run_id="fixture-run",
            )
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["new_observations"], 2)
            rows = [json.loads(line) for line in
                    (root / "intel" / "video-observations.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["native_url"], playlist()["entries"][0]["webpage_url"])
            self.assertEqual(rows[0]["metrics"]["views"], 341300)
            self.assertTrue(rows[0]["meets_post_view_floor"])
            self.assertIsNone(rows[1]["metrics"]["likes"])
            self.assertFalse(rows[1]["meets_post_view_floor"])
            summary = result["sources"][0]["cohort"]
            self.assertEqual(summary["average_views"], 171110.5)
            self.assertEqual(summary["floor_views"], 921)
            self.assertFalse(summary["meets_creator_consistency_floor"])

    def test_identical_discovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / "video-sources.json"
            registry.write_text(json.dumps(registry_payload()), encoding="utf-8")
            kwargs = dict(collector=lambda source, limit: playlist(),
                          observed_at="2026-08-01T15:00:00Z")
            first = discover_videos(registry, root / "intel", root / "evidence",
                                    run_id="first", **kwargs)
            second = discover_videos(registry, root / "intel", root / "evidence",
                                     run_id="second", **kwargs)
            self.assertEqual(first["new_observations"], 2)
            self.assertEqual(second["new_observations"], 0)
            self.assertEqual(len((root / "intel" / "video-observations.jsonl").read_text().splitlines()), 2)

    def test_collector_failure_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / "video-sources.json"
            registry.write_text(json.dumps(registry_payload()), encoding="utf-8")
            def failed(source, limit):
                raise RuntimeError("native extractor blocked")
            result = discover_videos(registry, root / "intel", root / "evidence",
                                     collector=failed, run_id="failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["sources"][0]["status"], "error")
            self.assertIn("native extractor blocked", result["sources"][0]["reason"])
            self.assertFalse((root / "intel" / "video-observations.jsonl").exists())

    def test_lm_exposes_video_discovery_command(self):
        lm_path = Path(__file__).resolve().parent.parent / "bin" / "lm"
        spec = importlib.util.spec_from_loader("marketing_lm", SourceFileLoader("marketing_lm", str(lm_path)))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(module.video_intel, "discover_videos") as discover:
            root = Path(temp)
            discover.return_value = {"status": "success", "run_id": "fixture"}
            rc = module.main(["intel", "video-discover", "--intel-root", str(root / "intel"),
                              "--evidence-root", str(root / "evidence")])
            self.assertEqual(rc, 0)
            discover.assert_called_once()

    def test_lm_exposes_video_ingest_command(self):
        lm_path = Path(__file__).resolve().parent.parent / "bin" / "lm"
        spec = importlib.util.spec_from_loader("marketing_lm_ingest",
                                               SourceFileLoader("marketing_lm_ingest", str(lm_path)))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(module.video_intel, "ingest_transcripts") as ingest:
            root = Path(temp)
            ingest.return_value = {"status": "success", "run_id": "fixture"}
            rc = module.main(["intel", "video-ingest", "--intel-root", str(root / "intel"),
                              "--evidence-root", str(root / "evidence")])
            self.assertEqual(rc, 0)
            ingest.assert_called_once()

    def test_ingest_downloads_top_eligible_unprocessed_post_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / "video-sources.json"
            registry.write_text(json.dumps(registry_payload()), encoding="utf-8")
            discover_videos(registry, root / "intel", root / "discovery",
                            collector=lambda source, limit: playlist(), run_id="discover")

            def downloader(observation, destination, limits):
                path = destination / "source.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture-video")
                return path

            def transcriber(media, destination, config, language):
                result = {"language": "en", "text": "Original spoken fixture.",
                          "segments": [{"start": 0.0, "end": 3.0,
                                        "text": "Original spoken fixture."}]}
                path = destination / "transcript.json"
                path.write_text(json.dumps(result), encoding="utf-8")
                return path

            first = ingest_transcripts(registry, root / "intel", root / "ingest",
                                       downloader=downloader, transcriber=transcriber,
                                       run_id="ingest-1", observed_at="2026-08-01T15:10:00Z")
            second = ingest_transcripts(registry, root / "intel", root / "ingest",
                                        downloader=downloader, transcriber=transcriber,
                                        run_id="ingest-2", observed_at="2026-08-01T15:11:00Z")
            self.assertEqual(first["new_transcripts"], 1)
            self.assertEqual(second["new_transcripts"], 0)
            row = json.loads((root / "intel" / "video-transcripts.jsonl").read_text())
            self.assertEqual(row["native_id"], "7609309883912965390")
            self.assertEqual(row["segment_count"], 1)
            self.assertEqual(row["transcription_model"], "base")
            self.assertTrue(Path(row["media_path"]).is_file())
            self.assertTrue(Path(row["transcript_path"]).is_file())

    def test_ingest_rejects_download_over_byte_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = registry_payload()
            data["limits"]["max_download_bytes"] = 4
            registry = root / "video-sources.json"
            registry.write_text(json.dumps(data), encoding="utf-8")
            discover_videos(registry, root / "intel", root / "discovery",
                            collector=lambda source, limit: playlist(), run_id="discover")
            def oversized(observation, destination, limits):
                path = destination / "source.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"too-large")
                return path
            result = ingest_transcripts(registry, root / "intel", root / "ingest",
                                        downloader=oversized,
                                        transcriber=lambda *args: self.fail("must not transcribe"),
                                        run_id="oversized")
            self.assertEqual(result["status"], "failed")
            self.assertIn("byte cap", result["items"][0]["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
