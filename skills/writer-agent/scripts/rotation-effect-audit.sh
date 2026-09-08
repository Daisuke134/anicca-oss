#!/usr/bin/env bash
# rotation-effect-audit.sh — biweekly verification that the 14-day
# self-improve rotation rule is actually improving Brave-tracked ranks.
#
# Methodology: read topic-queue picks over the last 14 days
# (state/topic-queue-history-*.md once persisted), match each pick to its
# target keyword, then compare the keyword's Brave rank delta between the
# pick date and today. Reports up / unchanged / down counts + posts to
# Slack as proof for Dais.
#
# Spec: 2026-06-03-anicca-article-distribution-spec.md §4 Patch A-12.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"

SKILL_DIR="${ARTICLE_SKILL_DIR:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}}"
RANK_DIR="$HOME/.openclaw/skills/anicca-seo-rank-monitor/state"

RESULT=$(SKILL_DIR="$SKILL_DIR" RANK_DIR="$RANK_DIR" python3 <<'PY'
import json, glob, os, re, datetime

skill_dir = os.environ['SKILL_DIR']
rank_dir = os.environ['RANK_DIR']

# 1. Topic-queue history (persisted snapshots, one per day) — currently a
#    forward-looking input; until self-improve writes daily snapshots, we
#    surface methodology + counts so the audit visibly reports its state.
history_files = sorted(glob.glob(os.path.join(skill_dir, 'state', 'topic-queue-history-*.md')))
picks = []
kw_re = re.compile(r'keyword:\s*([^\n]+)', re.I)
for f in history_files[-14:]:
  try:
    body = open(f, encoding='utf-8').read()
    m = kw_re.search(body)
    if m:
      kw = m.group(1).strip()
      date_match = re.search(r'topic-queue-history-(\d{4}-\d{2}-\d{2})\.md', f)
      picks.append({'date': date_match.group(1) if date_match else '?',
                    'keyword': kw,
                    'file': os.path.basename(f)})
  except Exception:
    continue

# 2. Compare rank delta per pick.
rank_files = sorted(glob.glob(os.path.join(rank_dir, 'ranks-*.json')))
def load(f):
  try:
    return json.load(open(f, encoding='utf-8'))
  except Exception:
    return None
def rank_for(snapshot, kw):
  if not snapshot: return 0
  for r in snapshot.get('keywords', []):
    if r.get('keyword') == kw and r.get('rank', 0) > 0:
      return r['rank']
  return 0

up = unchanged = down = 0
today_snapshot = load(rank_files[-1]) if rank_files else None
for p in picks:
  # Pick snapshot closest to pick date
  pick_snapshot = None
  for f in rank_files:
    if p['date'] in f:
      pick_snapshot = load(f); break
  o = rank_for(pick_snapshot, p['keyword'])
  t = rank_for(today_snapshot, p['keyword'])
  if o == 0 or t == 0:
    continue
  if t < o: up += 1
  elif t == o: unchanged += 1
  else: down += 1

conclusion = ('pending: topic-queue-history not yet persisted; '
              'will activate once self-improve.sh begins writing per-day snapshots')
if picks:
  if up > down:
    conclusion = 'rotation positive: more rank ups than downs over the 14d window'
  elif down > up:
    conclusion = 'rotation negative: more rank downs than ups; rule needs adjustment'
  else:
    conclusion = 'rotation neutral: ups equal downs'

print(json.dumps({
  'methodology': 'compare per-day rotation pick to Brave rank delta 14d',
  'days_analyzed': 14,
  'topics_picked': len(picks),
  'rank_improved': up,
  'rank_unchanged': unchanged,
  'rank_dropped': down,
  'conclusion': conclusion,
}, indent=2, ensure_ascii=False))
PY
)

printf '%s\n' "$RESULT"

TEXT="A-12 rotation rule 14d effect audit:
$RESULT"
if [[ -n "${SLACK_BOT_TOKEN:-}" ]]; then
  curl -sS -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H 'Content-Type: application/json; charset=utf-8' \
    -d "$(jq -nc --arg ch "${SLACK_CHANNEL_ID:-C091G3PKHL2}" --arg t "$TEXT" '{channel:$ch, text:$t}')" >/dev/null || true
fi
