#!/usr/bin/env bash
# research-product.sh — the PAID PRODUCT behind the x402 endpoint: take a buyer's query, do a real
# web search/scrape (firecrawl, $0 API), and return a concise markdown research brief. At $0 input
# cost, every paid request ($0.02 USDC to anicca's wallet) is pure profit.
#
# The old default `agent-reach format --json --query {q}` is BROKEN (the installed agent-reach CLI now
# only supports `format xhs`). firecrawl is the project's canonical web tool and works today.
#
# Usage: research-product.sh "<buyer query>"  → prints markdown to stdout.
set -uo pipefail
Q="${1:-}"
[ -n "$Q" ] || { echo "error: empty query"; exit 1; }
FC="${LIFE_MANAGER_FIRECRAWL:-$(command -v firecrawl 2>/dev/null || true)}"
[ -n "$FC" ] && [ -x "$FC" ] || { echo '{"status":"setup_required","missing":"firecrawl"}' >&2; exit 2; }
# Use firecrawl's SEARCH API (purpose-built, not bot-blocked like scraping google/ddg directly).
OUT=$("$FC" search "$Q" 2>/dev/null | grep -vE '^\s*$' | head -60)
echo "# Research brief: $Q"
echo
printf '%s\n' "$OUT" | head -50
echo
echo "_Produced by Anicca web-research (x402). Source: live web search._"
