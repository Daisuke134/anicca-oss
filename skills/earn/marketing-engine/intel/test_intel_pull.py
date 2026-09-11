import json
import pathlib
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import intel_pull  # noqa: E402


class FakeResponse:
    def __init__(self, status, payload=b"", headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class AdapterTests(unittest.TestCase):
    def test_judge_schema_const_nodes_declare_provider_compatible_type(self):
        schema = json.loads((HERE / "schemas" / "intel-judgment.schema.json").read_text())
        missing = []
        def visit(value, path="$"):
            if isinstance(value, dict):
                if "const" in value and "type" not in value:
                    missing.append(path)
                for key, child in value.items():
                    visit(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")
        visit(schema)
        self.assertEqual([], missing)

    def test_judge_schema_uses_only_provider_subset_constraints(self):
        schema = json.loads((HERE / "schemas" / "intel-judgment.schema.json").read_text())
        forbidden = {"pattern", "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "uniqueItems"}
        found = []
        def visit(value, path="$"):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in forbidden:
                        found.append(f"{path}.{key}")
                    visit(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")
        visit(schema)
        self.assertEqual([], found)

    def test_every_judge_schema_object_is_closed_for_structured_output(self):
        schema = json.loads((HERE / "schemas" / "intel-judgment.schema.json").read_text())
        open_objects = []
        def visit(value, path="$"):
            if isinstance(value, dict):
                if value.get("type") == "object" and value.get("additionalProperties") is not False:
                    open_objects.append(path)
                for key, child in value.items():
                    visit(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")
        visit(schema)
        self.assertEqual([], open_objects)

    def test_x_articles_fetches_profile_then_complete_status_blocks(self):
        profile = {
            "code": 200,
            "results": [{
                "id": "2081979523873038368",
                "url": "https://x.com/GeorgeLampro20/status/2081979523873038368",
            }],
            "cursor": {"bottom": "cursor"},
        }
        status = {
            "code": 200,
            "status": {
                "id": "2081979523873038368",
                "url": "https://x.com/GeorgeLampro20/status/2081979523873038368",
                "views": 177698,
                "likes": 612,
                "article": {
                    "title": "How to go from 0-10k a month in under 30 days",
                    "content": {"blocks": [{"text": "Build the gotcha first."}], "entityMap": {}},
                },
            },
        }
        http = FakeHTTP([FakeResponse(200, encoded(profile)), FakeResponse(200, encoded(status))])
        source = {"id": "x.george", "adapter": "x_articles", "handle": "GeorgeLampro20", "limit": 1}

        result = intel_pull.collect_source(source, http=http, env={})

        self.assertEqual("success", result.status)
        self.assertEqual(["x:2081979523873038368"], result.item_ids)
        self.assertEqual(1, len(result.payload["articles"]))
        self.assertEqual("Build the gotcha first.", result.payload["articles"][0]["article"]["content"]["blocks"][0]["text"])
        self.assertNotIn("cursor", http.calls[1]["url"])

    def test_atom_and_rss_entries_keep_native_identity_and_conditional_headers(self):
        atom = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><id>tag:feed</id><updated>2026-07-27T19:18:39Z</updated><entry><id>tag:item:1</id><title>ASO update</title><updated>2026-07-27T19:18:39Z</updated><link href="https://github.com/Eronred/aso-skills/commit/abc"/></entry></feed>'''
        http = FakeHTTP([FakeResponse(200, atom, {"etag": '"abc"'})])
        source = {"id": "rss.aso", "adapter": "rss", "url": "https://github.com/Eronred/aso-skills/commits/main.atom", "limit": 5}

        result = intel_pull.collect_source(source, http=http, env={}, cache={"etag": '"old"'})

        self.assertEqual("success", result.status)
        self.assertEqual(["rss:tag:item:1"], result.item_ids)
        self.assertEqual('"old"', http.calls[0]["headers"]["If-None-Match"])
        self.assertEqual('"abc"', result.cache["etag"])

    def test_rss_304_is_unchanged_not_empty_success(self):
        http = FakeHTTP([FakeResponse(304, b"", {"etag": '"same"'})])
        source = {"id": "rss.aso", "adapter": "rss", "url": "https://example.com/feed.xml", "limit": 5}
        result = intel_pull.collect_source(source, http=http, env={}, cache={"etag": '"same"'})
        self.assertEqual("unchanged", result.status)
        self.assertEqual([], result.item_ids)
        self.assertEqual("not_modified", result.reason)

    def test_github_repo_and_apple_lookup_are_structured_facts(self):
        repo = {"full_name": "Eronred/aso-skills", "html_url": "https://github.com/Eronred/aso-skills", "stargazers_count": 1698, "license": {"spdx_id": "MIT"}}
        commit = [{"sha": "abc", "html_url": "https://github.com/Eronred/aso-skills/commit/abc", "commit": {"message": "docs", "committer": {"date": "2026-07-27T19:18:39Z"}}}]
        apple = {"resultCount": 1, "results": [{"trackId": 6755129214, "trackName": "Anicca", "trackViewUrl": "https://apps.apple.com/app/id6755129214", "averageUserRating": 4.5, "userRatingCount": 46}]}
        http = FakeHTTP([
            FakeResponse(200, encoded(repo)), FakeResponse(200, encoded(commit)),
            FakeResponse(200, encoded(apple)),
        ])

        gh = intel_pull.collect_source({"id": "gh.aso", "adapter": "github_repo", "repo": "Eronred/aso-skills"}, http=http, env={})
        app = intel_pull.collect_source({"id": "apple.apps", "adapter": "apple_lookup", "ids": [6755129214], "country": "jp"}, http=http, env={})

        self.assertEqual(["github:Eronred/aso-skills@abc"], gh.item_ids)
        self.assertEqual(["apple:6755129214@unknown:r46"], app.item_ids)
        self.assertEqual(46, app.payload["results"][0]["userRatingCount"])

    def test_github_discovery_and_apple_search_are_named_searches_not_fake_trending(self):
        github = {"total_count": 1, "items": [{"full_name": "owner/repo", "html_url": "https://github.com/owner/repo", "pushed_at": "2026-08-01T00:00:00Z"}]}
        apple = {"resultCount": 1, "results": [{"trackId": 9, "trackName": "Competitor", "version": "2.0", "userRatingCount": 7, "trackViewUrl": "https://apps.apple.com/app/id9"}]}
        http = FakeHTTP([FakeResponse(200, encoded(github)), FakeResponse(200, encoded(apple))])
        discovered = intel_pull.collect_source({"id": "gh.discovery", "adapter": "github_search", "query": "app store optimization", "limit": 5}, http=http, env={})
        storefront = intel_pull.collect_source({"id": "apple.search", "adapter": "apple_search", "term": "affirmations", "country": "us", "limit": 5}, http=http, env={})
        self.assertEqual(["github-search:owner/repo@2026-08-01T00:00:00Z"], discovered.item_ids)
        self.assertEqual(["apple:9@2.0:r7"], storefront.item_ids)
        self.assertEqual("app store optimization", http.calls[0]["params"]["q"])
        self.assertEqual("affirmations", http.calls[1]["params"]["term"])

    def test_meta_without_declared_token_is_honestly_unavailable(self):
        http = FakeHTTP([])
        source = {"id": "meta.calm", "adapter": "meta_ad_library", "query": "Calm", "country": "ALL"}
        result = intel_pull.collect_source(source, http=http, env={})
        self.assertEqual("unavailable", result.status)
        self.assertEqual("meta_ad_library_access_token_not_configured", result.reason)
        self.assertEqual([], http.calls)

    def test_transport_failure_is_error_not_zero_item_success(self):
        http = FakeHTTP([TimeoutError("timed out")])
        source = {"id": "rss.failure", "adapter": "rss", "url": "https://example.com/feed.xml", "limit": 5}
        result = intel_pull.collect_source(source, http=http, env={})
        self.assertEqual("error", result.status)
        self.assertEqual("TimeoutError", result.error_class)
        self.assertNotIn("timed out", result.reason)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for name in ("playbook.jsonl", "hook-library.jsonl", "creators.jsonl", "ad-swipe.jsonl"):
            (self.root / name).write_bytes(b"")

    def tearDown(self):
        self.tmp.cleanup()

    def tactic(self, source_url="https://x.com/a/status/1", record_id="tactic.real-source.v1"):
        return {
            "schema_version": "marketing.tactic.v1", "id": record_id,
            "source_type": "x_article", "source_url": source_url, "source_handle": "@a",
            "source_null_reason": None, "captured_at": "2026-08-01T00:00:00Z",
            "provenance": "live_observed", "claim": "Test a real mechanism.",
            "mechanism": "The captured source describes a bounded test.",
            "applies_to": ["content"], "testable": True, "status": "new",
            "evidence_url": source_url, "evidence_null_reason": None,
            "our_result": None, "result_evidence": None,
        }

    def test_accepts_only_urls_present_in_captured_evidence_and_dedupes_rerun(self):
        judgment = {"playbook": [self.tactic()], "creators": [], "ad_swipe": []}
        allowed = {"https://x.com/a/status/1"}
        first = intel_pull.accept_judgment(judgment, root=self.root, allowed_urls=allowed)
        second = intel_pull.accept_judgment(judgment, root=self.root, allowed_urls=allowed)
        self.assertEqual({"playbook": 1, "creators": 0, "ad-swipe": 0}, first)
        self.assertEqual({"playbook": 0, "creators": 0, "ad-swipe": 0}, second)
        self.assertEqual(1, len((self.root / "playbook.jsonl").read_text().splitlines()))

    def test_rejects_invented_source_url_without_partial_append(self):
        judgment = {"playbook": [self.tactic("https://x.com/a/status/999")], "creators": [], "ad_swipe": []}
        with self.assertRaisesRegex(intel_pull.JudgmentError, "captured evidence"):
            intel_pull.accept_judgment(judgment, root=self.root, allowed_urls={"https://x.com/a/status/1"})
        self.assertEqual(b"", (self.root / "playbook.jsonl").read_bytes())

    def test_rejects_invalid_canonical_record_without_partial_append(self):
        bad = self.tactic()
        bad["claim"] = ""
        with self.assertRaises(intel_pull.JudgmentError):
            intel_pull.accept_judgment({"playbook": [bad], "creators": [], "ad_swipe": []}, root=self.root, allowed_urls={"https://x.com/a/status/1"})
        self.assertEqual(b"", (self.root / "playbook.jsonl").read_bytes())

    def test_rejects_collection_or_profile_url_as_tactic_source(self):
        for source_url in ("https://x.com/a", "https://itunes.apple.com/search"):
            row = self.tactic(source_url)
            if "itunes" in source_url:
                row["source_type"] = "web"
            with self.assertRaises(intel_pull.JudgmentError):
                intel_pull.accept_judgment(
                    {"playbook": [row], "creators": [], "ad_swipe": []},
                    root=self.root, allowed_urls={source_url},
                )
        self.assertEqual(b"", (self.root / "playbook.jsonl").read_bytes())

    def test_rejects_unbounded_judge_output(self):
        rows = [self.tactic(record_id=f"tactic.real-source-{index}.v1") for index in range(6)]
        with self.assertRaisesRegex(intel_pull.JudgmentError, "bounded item limit"):
            intel_pull.accept_judgment(
                {"playbook": rows, "creators": [], "ad_swipe": []},
                root=self.root, allowed_urls={"https://x.com/a/status/1"},
            )

    def test_declared_source_enrichment_is_exact_and_idempotent(self):
        source = {"id": "x.a", "enrichments": [{"tactic_id": "tactic.open.v1", "status_id": "1"}]}
        result = intel_pull.CollectionResult(
            "x.a", "success", "https://x.com/a", 200, 200, ["x:1"],
            {"articles": [{"id": "1", "url": "https://x.com/a/status/1"}]},
        )
        receipts = [{"source_id": "x.a", "evidence_path": "/evidence/a.json", "sha256": "a" * 64}]
        rows = intel_pull.declared_enrichments([source], [result], receipts, "2026-08-01T00:00:00Z")
        path = self.root / "source-enrichments.jsonl"
        first = intel_pull._append_enrichments(path, rows, self.root / ".enrichment.lock")
        second = intel_pull._append_enrichments(path, rows, self.root / ".enrichment.lock")
        self.assertEqual(1, first)
        self.assertEqual(0, second)
        self.assertEqual("https://x.com/a/status/1", json.loads(path.read_text())["source_url"])


class PullRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.intel = self.root / "intel"
        self.evidence = self.root / "evidence"
        self.intel.mkdir()
        for name in ("playbook.jsonl", "hook-library.jsonl", "creators.jsonl", "ad-swipe.jsonl"):
            (self.intel / name).write_bytes(b"")
        self.registry = self.root / "sources.json"
        self.registry.write_text(json.dumps({
            "schema_version": "marketing.intel-sources.v1",
            "sources": [{
                "id": "apple.apps", "adapter": "apple_lookup", "enabled": True,
                "cadence": "daily", "ids": [6755129214], "country": "jp",
                "product_ids": ["anicca-ios"], "languages": ["ja"],
            }],
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def _http(self):
        return FakeHTTP([FakeResponse(200, encoded({
            "resultCount": 1,
            "results": [{
                "trackId": 6755129214, "trackName": "Anicca",
                "trackViewUrl": "https://apps.apple.com/app/id6755129214",
                "averageUserRating": 4.5, "userRatingCount": 46,
            }],
        }))])

    def _judgment(self, _manifest):
        return {"playbook": [], "creators": [], "ad_swipe": [{
            "schema_version": "marketing.ad-swipe.v1", "id": "store.anicca-jp.v1",
            "platform": "app_store", "advertiser": "Anicca", "product_id": "anicca-ios",
            "source_url": "https://apps.apple.com/app/id6755129214", "source_null_reason": None,
            "captured_at": "2026-08-01T00:00:00Z", "first_seen_at": "2026-08-01T00:00:00Z",
            "last_seen_at": "2026-08-01T00:00:00Z", "impressions": None,
            "impressions_null_reason": "apple_lookup_does_not_return_impressions",
            "why_it_works": "The listing exposes a concrete daily affirmation promise.",
            "replication_plan": "Test an original listing treatment against product-page conversion.",
            "status": "new", "evidence_url": "https://apps.apple.com/app/id6755129214",
            "evidence_null_reason": None,
        }]}

    def test_pull_writes_immutable_receipts_accepts_grounded_row_and_skips_seen_judge(self):
        first = intel_pull.run_pull(
            registry_path=self.registry, intel_root=self.intel, evidence_root=self.evidence,
            http=self._http(), env={}, judge=self._judgment,
            run_id="a" * 32, observed_at="2026-08-01T00:00:00Z",
        )
        called = []
        second = intel_pull.run_pull(
            registry_path=self.registry, intel_root=self.intel, evidence_root=self.evidence,
            http=self._http(), env={}, judge=lambda manifest: called.append(manifest),
            run_id="b" * 32, observed_at="2026-08-01T01:00:00Z",
        )

        self.assertEqual(1, first["new_source_items"])
        self.assertEqual(1, first["accepted"]["ad-swipe"])
        self.assertEqual(0, second["new_source_items"])
        self.assertEqual([], called)
        self.assertEqual(1, len((self.intel / "ad-swipe.jsonl").read_text().splitlines()))
        self.assertTrue((self.evidence / ("a" * 32) / "apple.apps.json").is_file())
        receipt = json.loads((self.evidence / ("a" * 32) / "run.json").read_text())
        self.assertEqual("success", receipt["sources"][0]["status"])
        self.assertEqual(64, len(receipt["sources"][0]["sha256"]))
        self.assertNotIn("authorization", json.dumps(receipt).lower())

    def test_pull_records_unavailable_source_without_calling_judge_or_failing_run(self):
        self.registry.write_text(json.dumps({
            "schema_version": "marketing.intel-sources.v1",
            "sources": [{
                "id": "meta.ads", "adapter": "meta_ad_library", "enabled": True,
                "cadence": "daily", "query": "Calm", "country": "ALL",
                "product_ids": ["anicca-ios"], "languages": ["en"],
            }],
        }))
        result = intel_pull.run_pull(
            registry_path=self.registry, intel_root=self.intel, evidence_root=self.evidence,
            http=FakeHTTP([]), env={}, judge=lambda manifest: self.fail("judge must not run"),
            run_id="c" * 32, observed_at="2026-08-01T02:00:00Z",
        )
        self.assertEqual("partial", result["status"])
        self.assertEqual("unavailable", result["sources"][0]["status"])
        self.assertEqual(0, result["new_source_items"])

    def test_failed_judge_remains_pending_and_is_retried(self):
        first = intel_pull.run_pull(
            registry_path=self.registry, intel_root=self.intel, evidence_root=self.evidence,
            http=self._http(), env={}, judge=lambda manifest: (_ for _ in ()).throw(RuntimeError("judge down")),
            run_id="d" * 32, observed_at="2026-08-01T03:00:00Z",
        )
        retried = []
        second = intel_pull.run_pull(
            registry_path=self.registry, intel_root=self.intel, evidence_root=self.evidence,
            http=self._http(), env={}, judge=lambda manifest: retried.append(manifest) or self._judgment(manifest),
            run_id="e" * 32, observed_at="2026-08-01T04:00:00Z",
        )
        self.assertEqual("error", first["judge"]["status"])
        self.assertEqual(1, first["pending_judgment"])
        self.assertEqual(1, len(retried))
        self.assertEqual("success", second["judge"]["status"])
        self.assertEqual(0, second["pending_judgment"])

if __name__ == "__main__":
    unittest.main()
