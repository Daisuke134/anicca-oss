#!/usr/bin/env bash
# Contract for skills/writing-craft/ (spec 47 section 19.4 / 19-4c T4).
#
# Pure file checks -- no model calls, no live network. Line-count caps,
# protected-region markers, and the no-duplicate-title-ban check all run
# directly against the committed files.
set -uo pipefail

ROOT="${LIFE_MANAGER_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
CRAFT_DIR="$ROOT/skills/writer-agent/reference"
CRAFT_MD="$CRAFT_DIR/CRAFT.md"
X_POST="${WRITING_CRAFT_X_POST:-$CRAFT_DIR/formats/x-post.md}"
ARTICLE="$CRAFT_DIR/formats/article.md"
LONGFORM="${WRITING_CRAFT_LONGFORM:-$CRAFT_DIR/formats/longform.md}"
TITLE_RULES="$ROOT/skills/writer-agent/reference/title-best-practices.md"
ARTICLE_DAILY="$ROOT/skills/writer-agent/article-daily.sh"

PY=/opt/homebrew/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3
PASS=0
FAIL=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
    printf 'ok   %s\n' "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %s (expected %s got %s)\n' "$name" "$expected" "$actual"
  fi
}

check_le() {
  local name="$1" max="$2" actual="$3"
  if [ "$actual" -le "$max" ]; then
    PASS=$((PASS + 1))
    printf 'ok   %s (%s <= %s)\n' "$name" "$actual" "$max"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %s (%s > %s)\n' "$name" "$actual" "$max"
  fi
}

# 1. Line-count caps: CRAFT.md <= 60, each format adapter <= 25.
for f in "$CRAFT_MD" "$X_POST" "$ARTICLE" "$LONGFORM"; do
  [ -f "$f" ] || { FAIL=$((FAIL + 1)); printf 'FAIL missing file: %s\n' "$f"; }
done
CRAFT_LINES="$(wc -l < "$CRAFT_MD" | tr -d ' ')"
X_POST_LINES="$(wc -l < "$X_POST" | tr -d ' ')"
ARTICLE_LINES="$(wc -l < "$ARTICLE" | tr -d ' ')"
LONGFORM_LINES="$(wc -l < "$LONGFORM" | tr -d ' ')"
check_le "CRAFT.md is at most 60 lines" 60 "$CRAFT_LINES"
check_le "formats/x-post.md is at most 25 lines" 25 "$X_POST_LINES"
check_le "formats/article.md is at most 25 lines" 25 "$ARTICLE_LINES"
check_le "formats/longform.md is at most 25 lines" 25 "$LONGFORM_LINES"

# 2. Protected markers present exactly once each, and correctly ordered
#    (START before END) -- SkillOpt refuses to let step-level edits target
#    text inside them (verified in skillopt/optimizer/skill.py
#    _is_in_protected_region), so this block must exist from the start.
START_COUNT="$(grep -c '<!-- SLOW_UPDATE_START -->' "$CRAFT_MD")"
END_COUNT="$(grep -c '<!-- SLOW_UPDATE_END -->' "$CRAFT_MD")"
check "SLOW_UPDATE_START appears exactly once" "1" "$START_COUNT"
check "SLOW_UPDATE_END appears exactly once" "1" "$END_COUNT"
START_LINE="$(grep -n '<!-- SLOW_UPDATE_START -->' "$CRAFT_MD" | head -1 | cut -d: -f1)"
END_LINE="$(grep -n '<!-- SLOW_UPDATE_END -->' "$CRAFT_MD" | head -1 | cut -d: -f1)"
ORDER_OK="False"
[ -n "$START_LINE" ] && [ -n "$END_LINE" ] && [ "$START_LINE" -lt "$END_LINE" ] && ORDER_OK="True"
check "SLOW_UPDATE_START precedes SLOW_UPDATE_END" "True" "$ORDER_OK"

# 3. No line of CRAFT.md or any adapter duplicates a ban sentence from
#    reference/title-best-practices.md section 2 (normalised-text compare).
#    Title rules live in exactly one file; CRAFT.md may only point at it.
DUP_CHECK="$("$PY" -c "
import re

def normalize(text):
    text = text.strip()
    text = text.replace('**', '')
    text = text.replace('\`', '')
    text = re.sub(r'\s+', '', text)
    return text

title_text = open('$TITLE_RULES', encoding='utf-8').read()
section = title_text.split('## 2. ')[1].split('## 3.')[0]
ban_sentences = []
for line in section.splitlines():
    line = line.strip()
    m = re.match(r'^\d+\.\s+\*\*(.+?)\*\*', line)
    if m:
        ban_sentences.append(normalize(m.group(1)))
assert len(ban_sentences) >= 4, f'expected >=4 ban sentences, found {len(ban_sentences)}'

craft_files = ['$CRAFT_MD', '$X_POST', '$ARTICLE', '$LONGFORM']
hits = []
for path in craft_files:
    for lineno, line in enumerate(open(path, encoding='utf-8'), start=1):
        norm_line = normalize(line)
        if not norm_line:
            continue
        for ban in ban_sentences:
            if ban and ban in norm_line:
                hits.append((path, lineno, ban))

print(len(ban_sentences), len(hits))
")"
check "at least 4 ban sentences were extracted from title-best-practices.md section 2" "True" "$(echo "$DUP_CHECK" | awk '{print ($1>=4)}' | sed 's/1/True/;s/0/False/')"
check "no line in CRAFT.md or any adapter duplicates a title-ban sentence" "0" "$(echo "$DUP_CHECK" | awk '{print $2}')"

# 4. article-daily.sh mentions CRAFT.md (STEP 3 REQUIRED READ wiring).
MENTIONS="$(grep -c 'ARTICLE_ROOT_PLACEHOLDER/reference/CRAFT.md' "$ARTICLE_DAILY")"
check "article-daily.sh mentions CRAFT.md" "True" "$([ "$MENTIONS" -ge 1 ] && echo True || echo False)"
check "article-daily.sh has no standalone craft compatibility path" "0" \
  "$(grep -c 'profitable-claude/skills/writing-craft' "$ARTICLE_DAILY" || true)"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
