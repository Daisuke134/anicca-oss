#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PY="${LIFE_MANAGER_PYTHON:-python3}"
"$PY" - "$ROOT/scripts" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import publication_resume as publication

# The production weekly audit runs under the bootstrap-managed Life Manager
# Python. A receipt with exactly the expected proof count validates there.
publication._validate_asset_proofs(
    {"media": {}},
    "x-post/ja",
    {"asset_proofs": [], "asset_urls": []},
)

# Python compatibility must not weaken the exact-cardinality invariant.
try:
    publication._validate_asset_proofs(
        {
            "media": {
                "headline_image": {
                    "path": "/tmp/headline.png",
                    "sha256": "a" * 64,
                },
                "body_assets": [],
            }
        },
        "note/ja",
        {"asset_proofs": []},
    )
except publication.InvariantError as error:
    assert str(error) == "receipt public asset content proof is incomplete"
else:
    raise AssertionError("missing public asset proof was accepted")

print("PASS: receipt validation supports managed Python and preserves cardinality")
PY
