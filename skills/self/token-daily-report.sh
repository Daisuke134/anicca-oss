#!/bin/bash
# token-daily-report.sh — exact previous-JST-day global + per-loop token telemetry.
# Wake → report → die (single-shot; no persistent session).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USAGE_REPORT="${AGENT_USAGE_REPORT_BIN:-$SCRIPT_DIR/../agent-runner/usage_report.py}"

YESTERDAY=$(TZ=Asia/Tokyo date -v-1d +%Y%m%d)
YESTERDAY_ISO=$(TZ=Asia/Tokyo date -v-1d +%F)
JSON=$(npx -y ccusage@20.0.18 daily --since "$YESTERDAY" --until "$YESTERDAY" \
  --timezone Asia/Tokyo --by-agent --json 2>/dev/null || echo '{}')

SUMMARY=$(echo "$JSON" | jq -r --arg day "$YESTERDAY_ISO" '
  (.daily // []) |
  if length == 0 then "全CLI消費 " + $day + ": データなし(ccusage失敗 or 消費ゼロ)"
  else
    .[0] as $d |
    "全CLI消費 " + $day + ": " + (($d.totalTokens/1000000*10|floor/10)|tostring) +
    "Mtok / $" + (($d.totalCost*100|floor/100)|tostring) + " API換算(実請求ではない)\n" +
    "agent別:\n" +
    ($d.agents | map("  \(.agent): \((.totalTokens/1000000*10|floor/10))Mtok / $\((.totalCost*100|floor/100)) API換算") | join("\n")) +
    "\nmodel TOP5:\n" +
    ([$d.agents[] | .agent as $agent | .modelBreakdowns[] |
      {agent:$agent, model:.modelName, tokens:(.inputTokens+.cacheReadTokens+.outputTokens), cost:.cost}] |
      sort_by(-.tokens) | .[0:5] |
      map("  \(.agent)/\(.model): \((.tokens/1000000*10|floor/10))Mtok / $\((.cost*100|floor/100))") |
      join("\n"))
  end' 2>/dev/null)
[ -z "$SUMMARY" ] && SUMMARY="token日報の生成に失敗(jq/ccusage要確認)"

LOOP_JSON=$(python3 "$USAGE_REPORT" \
  --date "$YESTERDAY_ISO" --format json 2>/dev/null || echo '{}')
LOOP_SUMMARY=$(echo "$LOOP_JSON" | jq -r '
  if (.totals.attempts // 0) == 0 then
    "loop別runner実測: データなし（telemetry導入前または実行ゼロ）"
  else
    "loop別runner実測: \(.totals.total_tokens) tokens / \(.totals.attempts) attempts / usage不明 \(.totals.unavailable_attempts)\n" +
    (.groups | map("  \(.loop) \(.provider)/\(.model) \(.effort // "-"): \(.total_tokens) tokens (\(.measured_attempts)/\(.attempts) measured)") | join("\n"))
  end' 2>/dev/null)
[ -z "$LOOP_SUMMARY" ] && LOOP_SUMMARY="loop別runner実測: 集計失敗"

# Persistent agent sessions (the hidden-socket leak class).
TMUX_COUNT=$(ps aux | grep -c '[t]mux -S /tmp/anicca-.*\.sock' || true)
ZOMBIES=$(ps -axo etime=,command= | grep '[t]mux -S /tmp/anicca-selffix' | awk '$1 ~ /-/ {c++} END {print c+0}')

REPORT="📊 token日報 $(TZ=Asia/Tokyo date +%m/%d)
$SUMMARY
$LOOP_SUMMARY
常駐tmuxセッション: ${TMUX_COUNT:-0}本 / 1日超ゾンビselffix: ${ZOMBIES:-0}本
(掟: 常駐禁止・自壊タイマー・引退届が先)"
"$SCRIPT_DIR/../_shared/send-telegram.sh" "$REPORT" \
  "${TOKEN_REPORT_TELEGRAM_TARGET:-${TELEGRAM_ALERT_CHAT_ID:?TOKEN_REPORT_TELEGRAM_TARGET or TELEGRAM_ALERT_CHAT_ID is required}}"
