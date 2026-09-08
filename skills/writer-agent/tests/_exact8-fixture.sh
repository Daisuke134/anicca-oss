#!/usr/bin/env bash
# Shared exact8 publication-state fixture for shell contract tests.
export ARTICLE_TEST_ONLY=1
# Portable, non-secret destination identities for publication-state fixtures.
# Production requires the installer to provide its own values; shell tests own
# explicit synthetic identities instead of inheriting one operator's env.
export NOTE_URLNAME=writer-note
export ZENN_ACCOUNT=writer-zenn
export DEVTO_ACCOUNT_HANDLE=writer-devto
export SUBSTACK_PUBLICATION_JA=writer-ja.substack.com
export SUBSTACK_PUBLICATION_EN=writer-en.substack.com
export X_ACCOUNT_HANDLE=writer-x
export ARTICLE_PRODUCT_LANDING_URL=https://writer.example/product
export ARTICLE_SELF_OWNED_BASE_URL=https://writer.example

exact8_init_state() {
  local root="$1"
  local run_dir="$2"
  local ledger="$3"
  local run_id="$4"
  local topic_id="$5"
  local zenn_slug="$6"
  local state="$run_dir/gates/publication-state.json"
  local helper="$root/skills/writer-agent/scripts/publication_resume.py"

  mkdir -p "$run_dir/gates"
  printf '# ja\n\nhttps://writer.example/product?product_id=fixture&run_id=%s&artifact_id=article-ja&variant_id=fixture-ja&click_id=%s-article-ja\n' \
    "$run_id" "$run_id" >"$run_dir/article-ja.md"
  printf '%s\n' '---' 'title: "Exact8 fixture"' 'tags: ai, agents' '---' '# en' '' \
    >"$run_dir/article-en.md"
  printf 'https://writer.example/product?product_id=fixture&run_id=%s&artifact_id=article-en&variant_id=fixture-en&click_id=%s-article-en\n' \
    "$run_id" "$run_id" >>"$run_dir/article-en.md"
  # Publication init refuses drafts without the canonical media envelope;
  # attach it through the same mechanical boundary production uses.
  python3 "$root/skills/writer-agent/scripts/canonical_media.py" attach \
    --file "$run_dir/article-ja.md" >/dev/null
  python3 "$root/skills/writer-agent/scripts/canonical_media.py" attach \
    --file "$run_dir/article-en.md" >/dev/null
  printf 'short post\n\nhttps://writer.example/product?product_id=fixture&run_id=%s&artifact_id=x-post-ja&variant_id=fixture-x&click_id=%s-x-post-ja\n' \
    "$run_id" "$run_id" >"$run_dir/x-post-ja.txt"
  printf 'headline\n' >"$run_dir/headline-image.png"
  printf 'diagram\n' >"$run_dir/body-diagram.png"
  python3 "$helper" --state "$state" --ledger "$ledger" init \
    --run-id "$run_id" --run-dir "$run_dir" --topic-id "$topic_id" \
    --draft-ja "$run_dir/article-ja.md" --draft-en "$run_dir/article-en.md" \
    --x-post-ja "$run_dir/x-post-ja.txt" --safety ALLOW --max-resume-attempts 2 \
    --headline-image "$run_dir/headline-image.png" \
    --body-asset "$run_dir/body-diagram.png" \
    --legacy-exact8 \
    >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair note/ja --target-kind note-key --target note-current >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair zenn-article/ja --target-kind zenn-slug --target "$zenn_slug" >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair devto/en --target-kind devto-article-id --target 1001 >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair substack/ja --target-kind substack-draft-id --target 1002 >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair substack/en --target-kind substack-draft-id --target 1003 >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair x-article/ja --target-kind x-draft-url \
    --target https://x.com/i/articles/ja-current/edit >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair x-article/en --target-kind x-draft-url \
    --target https://x.com/i/articles/en-current/edit >/dev/null
  python3 "$helper" --state "$state" --ledger "$ledger" intent \
    --pair x-post/ja --target-kind x-post-slot --target daily-2026-07-22 >/dev/null
}

exact8_write_zenn_readback() {
  local state="$1"
  local output="$2"
  local slug="$3"
  local published_at="$4"
  local live_url="$5"

  python3 - "$state" "$output" "$slug" "$published_at" "$live_url" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1], encoding="utf-8"))
slug = sys.argv[3]
published_at = sys.argv[4]
live_url = sys.argv[5]
assets = [item["sha256"] for item in state["media"]["body_assets"]]
urls = [f"https://assets.example/{index}.png" for index, _ in enumerate(assets)]
value = {
    "status": "live",
    "live_url": live_url,
    "verified": True,
    "public_id": slug,
    "published_at": published_at,
    "stable_target": slug,
    "artifact_sha256": state["drafts"]["ja"]["sha256"],
    "language": "ja",
    "content_verified": True,
    "asset_hashes": assets,
    "asset_urls": urls,
    "asset_proofs": [
        {
            "expected_sha256": expected,
            "remote_sha256": expected,
            "remote_url": url,
            "match_method": "exact-sha256",
        }
        for expected, url in zip(assets, urls)
    ],
    "asset_verified": True,
    "body_media_verified": True,
    "destination_identity": state["destination_identities"]["zenn-article/ja"],
    "identity_verified": True,
    "identity_source": "zenn-username-scoped-api",
}
json.dump(value, open(sys.argv[2], "w", encoding="utf-8"))
PY
}

exact8_record_other_seven() {
  local root="$1"
  local run_dir="$2"
  local ledger="$3"
  local helper="$root/skills/writer-agent/scripts/publication_resume.py"
  local state="$run_dir/gates/publication-state.json"
  local pair public_id live_url evidence
  for pair in note/ja devto/en substack/ja substack/en x-article/ja x-article/en x-post/ja; do
    case "$pair" in
      note/ja) public_id=note-current; live_url=https://note.com/writer-note/n/note-current ;;
      devto/en) public_id=1001; live_url=https://dev.to/writer-devto/1001 ;;
      substack/ja) public_id=1002; live_url=https://writer-ja.substack.com/p/1002 ;;
      substack/en) public_id=1003; live_url=https://writer-en.substack.com/p/1003 ;;
      x-article/ja) public_id=3001; live_url=https://x.com/writer-x/article/3001 ;;
      x-article/en) public_id=3002; live_url=https://x.com/writer-x/article/3002 ;;
      x-post/ja) public_id=4001; live_url=https://x.com/writer-x/status/4001 ;;
    esac
    evidence="$(python3 - "$state" "$pair" "$public_id" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1], encoding="utf-8"))
pair = sys.argv[2]
public_id = sys.argv[3]
entry = state["pairs"][pair]
assets = [] if pair == "x-post/ja" else [
    item["sha256"] for item in state["media"]["body_assets"]
]
if pair not in {"zenn-article/ja", "x-post/ja"}:
    assets.insert(0, state["media"]["headline_image"]["sha256"])
value = {
    "verified": True,
    "public_id": public_id,
    "published_at": "2026-07-22T03:00:00+00:00",
    "stable_target": entry["target"],
    "artifact_sha256": (
        state["x_post"]["sha256"]
        if pair == "x-post/ja"
        else state["drafts"][entry["lang"]]["sha256"]
    ),
    "language": entry["lang"],
    "content_verified": True,
    "asset_hashes": assets,
    "asset_verified": True,
    "asset_urls": [
        f"https://assets.example/{index}.png"
        for index, _ in enumerate(assets)
    ],
    "asset_proofs": [
        {
            "expected_sha256": expected,
            "remote_sha256": expected,
            "remote_url": f"https://assets.example/{index}.png",
            "match_method": "exact-sha256",
        }
        for index, expected in enumerate(assets)
    ],
    "destination_identity": state["destination_identities"][pair],
    "identity_verified": True,
    "identity_source": "authenticated-destination-readback",
}
if pair == "note/ja":
    value.update(eyecatch_verified=True, body_media_verified=True)
elif pair in {"devto/en", "substack/ja", "substack/en"}:
    value["body_media_verified"] = True
elif pair.startswith("x-article/"):
    value.update(cover_verified=True, body_media_verified=True)
else:
    value.update(
        timeline_verified=True,
        emoji_verified=True,
        status_id=public_id,
    )
print(json.dumps(value, separators=(",", ":")))
PY
)"
    ARTICLE_TEST_ONLY=1 python3 "$helper" --state "$state" --ledger "$ledger" record-live \
      --pair "$pair" --live-url "$live_url" \
      --evidence-json "$evidence" --test-local-asset-readback >/dev/null
  done
}
