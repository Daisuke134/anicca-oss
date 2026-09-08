#!/usr/bin/env bash
# cta-gate.sh — refuse to ship a piece that gives the reader nowhere to go.
#
# Measured 2026-07-27: the twenty most-read dev.to pieces carried 2,538 of
# 3,314 total views and not one contained a path back to us. Conversion was
# structurally zero, and no amount of writing quality moves a number that is
# multiplied by an absent link. The doors existed only on recent pieces that
# nobody had found yet.
#
# This is deliberately NOT a judge. It counts. A model asked "does this have a
# good call to action" will answer yes about a piece that has none, because it
# is agreeable and the question is fuzzy; grep is not agreeable. Quality gates
# are advisory here (spec 47), but this one is a revenue-path invariant and
# fails closed.
#
#   cta-gate.sh <article.md>
#   exit 0 = a path is present, 1 = none found, 2 = usage
set -uo pipefail

MD="${1:-}"
[ -f "$MD" ] || { echo "FATAL: usage: cta-gate.sh <article.md>" >&2; exit 2; }
: "${ARTICLE_PRODUCT_LANDING_URL:?ARTICLE_PRODUCT_LANDING_URL is required}"
shift
EXPECTED_RUN_ID=""
EXPECTED_ARTIFACT_ID=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --run-id) EXPECTED_RUN_ID="${2:-}"; shift 2 ;;
    --artifact-id) EXPECTED_ARTIFACT_ID="${2:-}"; shift 2 ;;
    *) echo "FATAL: unknown argument: $1" >&2; exit 2 ;;
  esac
done

python3 - "$MD" "$EXPECTED_RUN_ID" "$EXPECTED_ARTIFACT_ID" "$ARTICLE_PRODUCT_LANDING_URL" <<'PY'
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

required = (
    "product_id",
    "run_id",
    "artifact_id",
    "variant_id",
    "click_id",
)
body = html.unescape(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_run_id = sys.argv[2]
expected_artifact_id = sys.argv[3]
landing = urlparse(sys.argv[4])
if landing.scheme not in {"http", "https"} or not landing.netloc:
    raise SystemExit("FATAL: ARTICLE_PRODUCT_LANDING_URL must be an absolute HTTP(S) URL")
landing_path = landing.path.rstrip("/")
lineage_mismatch = False
for raw in re.findall(r"https?://[^\s<>\]\[()\"']+", body):
    url = raw.rstrip(".,;:!?")
    parsed = urlparse(url)
    if (
        parsed.scheme != landing.scheme
        or parsed.netloc.lower() != landing.netloc.lower()
        or not (
            parsed.path.rstrip("/") == landing_path
            or parsed.path.startswith(f"{landing_path}/")
        )
    ):
        continue
    query = parse_qs(parsed.query, keep_blank_values=True)
    values = {
        field: query.get(field, [""])[0]
        for field in required
    }
    if all(values.values()):
        if (
            (expected_run_id and values["run_id"] != expected_run_id)
            or (
                expected_artifact_id
                and values["artifact_id"] != expected_artifact_id
            )
        ):
            lineage_mismatch = True
            continue
        print(json.dumps({
            "gate": "cta",
            "verdict": "PASS",
            "destination": landing.netloc,
            **values,
        }, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(0)

print(json.dumps({
    "gate": "cta",
    "verdict": "FAIL",
    "reason": (
        "product landing CTA lineage mismatch"
        if lineage_mismatch
        else "measurable product landing CTA missing"
    ),
}, ensure_ascii=False, separators=(",", ":")))
raise SystemExit(1)
PY
