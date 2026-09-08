#!/usr/bin/env bash
# propose-and-rewrite.sh — deterministic context bundler for X / article / yt-long
# Spec: ANICCA_USEFUL_CONTENT_SPEC.md T0-7
# Bible: clone-don't-template + history-aware + experience-injected (HR-B/C/D/H)
#
# Responsibility split (HARD RULE #6 既存 X skill と整合):
#   - 本 sh = deterministic context bundling (pattern pick / history check / experience pull / persona inject)
#   - 呼出 LLM = 実際の rewrite + 5案 scoring + 敵対テスト + final SHIP
#
# Usage:
#   bash propose-and-rewrite.sh --channel <x-useful|x-buildinpublic|x-engagement-quote|article-zenn|article-devto|article-substack-ja|article-substack-en|article-aniccaai-blog|yt-long-en|yt-long-ja>
#                               --platform <X|Zenn|Dev.to|Substack|aniccaai-blog|YT>
#                               --account <handle>
#                               [--lang ja|en]
#                               [--candidates 5]
# Output (stdout JSON, single-line)
# Exit 0 = picked, fields populated.
# Exit 2 = REPEAT exhausted (no non-anti-repeat pattern available, LLM should NOT post)
# Exit 3 = pattern file missing
# Exit 4 = jq / python missing
# Exit 5 = persona missing

set -euo pipefail

CHANNEL=""
PLATFORM=""
ACCOUNT=""
LANG=""
CANDIDATES=5
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)    CHANNEL="$2"; shift 2 ;;
    --platform)   PLATFORM="$2"; shift 2 ;;
    --account)    ACCOUNT="$2"; shift 2 ;;
    --lang)       LANG="$2"; shift 2 ;;
    --candidates) CANDIDATES="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$CHANNEL"   ]] || { echo "FATAL: --channel required" >&2; exit 1; }
[[ -n "$PLATFORM"  ]] || { echo "FATAL: --platform required" >&2; exit 1; }
[[ -n "$ACCOUNT"   ]] || { echo "FATAL: --account required" >&2; exit 1; }

command -v jq      >/dev/null 2>&1 || { echo "FATAL: jq missing" >&2; exit 4; }
command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 missing" >&2; exit 4; }

LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:?LIFE_MANAGER_REPO is required}"
WRITER_STATE_DIR="${WRITER_STATE_DIR:?WRITER_STATE_DIR is required}"
LIB="${WRITER_CONTENT_LIBRARY_DIR:-$WRITER_STATE_DIR/content-library}"

case "$CHANNEL" in
  x-*)           PATTERN_FILE="$LIB/pattern-x.jsonl" ;;
  article-*)     PATTERN_FILE="$LIB/pattern-article.jsonl" ;;
  yt-long-*)     PATTERN_FILE="$LIB/pattern-yt-long.jsonl" ;;
  iam-color-en|iam-en)  PATTERN_FILE="$LIB/pattern-iam-en.jsonl" ;;
  iam-color-ja|iam-ja)  PATTERN_FILE="$LIB/pattern-iam-ja.jsonl" ;;
  card-en)              PATTERN_FILE="$LIB/pattern-card-en.jsonl" ;;
  card-ja)              PATTERN_FILE="$LIB/pattern-card-ja.jsonl" ;;
  *) echo "FATAL: unknown channel family: $CHANNEL" >&2; exit 1 ;;
esac
[[ -f "$PATTERN_FILE" ]] || { echo "FATAL: pattern file missing: $PATTERN_FILE" >&2; exit 3; }

DEFAULT_PERSONA="$LIFE_MANAGER_REPO/skills/writer-agent/reference/default-persona.md"
PERSONA_FILE="${WRITER_PERSONA_FILE:-$WRITER_STATE_DIR/config/persona.md}"
[[ -f "$PERSONA_FILE" ]] || PERSONA_FILE="$DEFAULT_PERSONA"
[[ -f "$PERSONA_FILE" ]] || { echo "FATAL: persona missing: $PERSONA_FILE" >&2; exit 5; }

. "$LIFE_MANAGER_REPO/skills/_shared/lib/account-history.sh"
. "$LIFE_MANAGER_REPO/skills/_shared/lib/experience-log.sh"

# === Top-12 pattern rank via Python (direct file read, no stdin pipe) ===
TOP12_JSON="$(PATTERN_FILE="$PATTERN_FILE" LANG_FILTER="$LANG" python3 <<'PY'
import json, os, sys
pf = os.environ["PATTERN_FILE"]
lang = os.environ.get("LANG_FILTER","")
rows = []
with open(pf) as fh:
    for ln in fh:
        ln = ln.strip()
        if not ln: continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if (r.get("status","active") == "killed"): continue
        if lang and r.get("language") and r["language"] != lang: continue
        rows.append(r)
def metric(e):
    obs = e.get("observed", {}) or {}
    for k in ("views", "impressions", "views_avg"):
        v = obs.get(k)
        if isinstance(v, (int, float)) and v:
            return v
    return 1
def score(e):
    base = metric(e)
    if not e.get("lastUsed"):
        base = int(base * 1.2)
    # WS3b3 mixed-schema risk guard: structural_principle 持ち行を tiebreaker で先頭に
    has_sp = 1 if (e.get("structural_principle") or "").strip() else 0
    return (has_sp, base)  # tuple desc sort
ranked = sorted(rows, key=score, reverse=True)
print(json.dumps(ranked[:12], ensure_ascii=False))
PY
)"

NCANDS="$(printf '%s' "$TOP12_JSON" | jq 'length')"

# === HR-L cold-start path ===
# pattern library 空でも fail せず、 cold-start mode で persona + experience だけからゼロ生成。
# Account owner自身の prior 投稿が貯まれば heartbeat が pattern library を埋め、通常pathに戻る。
COLD_START="false"
if [[ -z "$NCANDS" || "$NCANDS" -eq 0 ]]; then
  COLD_START="true"
  PICK='{"source_id":"cold-start","status":"active","structural_principle":"設定された persona + 当日 experience 1次情報のみから完全ゼロ生成。既存テンプレ・既存 hook・既存 narrative arc は参照しない。構造は account owner が当日の経験から最も明確に伝わる形を選ぶ。","success_signals":[],"rewrite_axes":["切り口","tone","length"]}'
fi

# === step 3: history-aware filter (anti-repeat 14d) ===
PICK_FINAL=""
if [[ "$COLD_START" == "true" ]]; then
  # cold-start は anti-repeat 適用なし (pattern_id 共通なので意味ない)
  PICK_FINAL="$PICK"
else
  for ((i=0; i<NCANDS; i++)); do
    CAND="$(printf '%s' "$TOP12_JSON" | jq -c ".[$i]")"
    HOOK="$(printf '%s' "$CAND" | jq -r '.hook // .lede // .hook_first_30s // ""')"
    STYPE="$(printf '%s' "$CAND" | jq -r '.structure.type // .niche_tags[0] // "default"')"
    if ah_check "$CHANNEL" "$PLATFORM" "$ACCOUNT" "$HOOK" "$STYPE" 14 2>/dev/null; then
      PICK_FINAL="$CAND"
      break
    fi
  done

  if [[ -z "$PICK_FINAL" ]]; then
    echo "FATAL: 14d anti-repeat exhausted for channel=$CHANNEL platform=$PLATFORM account=$ACCOUNT — top-${NCANDS} all seen recently" >&2
    exit 2
  fi
fi
PICK="$PICK_FINAL"

# === step 4: experience-log today (useful only) ===
# el_useful_today exits 1 when no file exists with set -euo pipefail
# So both jq -s and fallback echo '[]' can produce output = double line
# Fix: filter to last valid JSON line only
# NOTE: el_useful_today exits 1 when no log file exists. Under `set -o pipefail` that
# made the whole pipe "fail" even though jq printed `[]`; the old `|| echo '[]'` then
# fired AND appended a 2nd `[]` → EXPERIENCE_JSON = `[]\n[]` (two JSON values) → invalid
# for jq --argjson. Fix: absorb el_useful_today's exit with `|| true` (so pipefail+set -e
# don't kill the script), then jq yields a single `[]`; guard it's an array.
EL_RAW="$( { el_useful_today 2>/dev/null || true; } | jq -s -c '.' 2>/dev/null | tail -n1)"
EXPERIENCE_JSON="${EL_RAW:-[]}"
[[ "$EXPERIENCE_JSON" == \[* ]] || EXPERIENCE_JSON='[]'

# HR-L: cold-start かつ experience も空なら 投稿しない (架空 fact 生成防止)
EXP_LEN="$(printf '%s' "$EXPERIENCE_JSON" | jq 'length' 2>/dev/null || echo 0)"
if [[ "$COLD_START" == "true" && "$EXP_LEN" -eq 0 ]]; then
  echo "FATAL: cold-start path + experience-log empty — refusing to fabricate. Run capture-today.sh or wait for next heartbeat." >&2
  exit 6
fi

# === step 5: bundle ===
PATTERN_ID="$(printf '%s' "$PICK" | jq -r '.source_id')"

if [[ "$COLD_START" == "true" ]]; then
INSTRUCTION="persona_path を必ず読み、そこに設定された話者として、channel=${CHANNEL} platform=${PLATFORM} account=${ACCOUNT} に投稿する1本を生成。

★ 本リクエストは HR-L cold-start mode ★
pattern library は空です。named third-party creatorのviral postの構造クローンもverbatim借用も禁止。pattern libraryはaccount owner自身のprior投稿が貯まるまで空です。

★ cold-start での書き方 ★
1. **persona_pathを必ず読む。** 設定されたvoice・表記・NGパターンを理解してから書く。
2. **構造は experience entries から逆算して自分で決める。** 「数字 1 次情報が複数あるなら listicle」「重要な気付き 1 つなら confession」「失敗 → 学び なら postmortem」 等。 既存 viral pattern を真似しない、 自分の経験が最も clear に伝わる形を選ぶ。
3. **experience[] の 1 次情報 (時刻 / 数字 / 具体イベント) のみを anchor に書く。** 架空の数字・時刻・出来事の捏造は absolutely 禁止 (架空 = sloppy = 削除)。
4. **${CANDIDATES} 案生成 → 自己採点 → winner → humanize → SHIP** (recursive-improver loop)。
   採点軸: hook 強 / 数字 1 次情報具体性 / scroll サバイブ / 敵対 CMO (差別化) / **借用ゼロ verify** (verbatim_blacklist.txt + 名のある creator の signature phrase 含む) / configured persona 整合。
5. **投稿前 vg_check (${LIFE_MANAGER_REPO}/skills/_shared/lib/verbatim-guard.sh)** で verbatim_blacklist.txt grep block。 1 つでも phrase hit したら書き直し。
6. 投稿成功時に呼出側スクリプトが ah_record 呼出。 LLM 側 record 不要。

NG: personaにない代理人表現 / 一般論 / em-dash連発 / 中毒煽り / verbatim_blacklist.txtの任意phrase / 名のあるcreatorのsignature phraseの構造的真似。投稿前にpersona_pathとvg_checkを必ず照合。
"
else
INSTRUCTION="persona_path を必ず読み、そこに設定された話者として、channel=${CHANNEL} platform=${PLATFORM} account=${ACCOUNT} に投稿する1本を生成。

★ 核心ルール (HR-J + HR-K + HR-L): ★
1. pattern.structural_principle は **抽象的な構造の指針**。 文言・catchphrase・特定 phrase の verbatim 借用は **絶対禁止**。
2. 文章は **100% configured personaの一人称で完全に新規生成**。過去viral投稿からのphrase借用は禁止。
3. experience[] から当日の 1 次情報 (時刻 / 数字 / 具体イベント) を anchor に。 これが空ならその channel の投稿を skip (架空の事実を作るくらいなら投稿しない)。
4. 投稿前に必ず vg_check (${LIFE_MANAGER_REPO}/skills/_shared/lib/verbatim-guard.sh) で verbatim_blacklist.txt grep block。 1 つでも phrase hit したら書き直し。
5. ${CANDIDATES} 案生成 → 自己採点 (hook 強 / 数字 1 次情報 / scroll サバイブ / 敵対 CMO / 借用ゼロ) → winner 1 本 → humanize (em-dash 禁止 / rule-of-three 禁止 / 一般論削除) → SHIP。
6. anti-repeat 14日 は既に通過済 (pattern_id=${PATTERN_ID})。 同 pattern 再利用でも文言は完全に別物に。
7. 投稿成功時に呼出側スクリプトが ah_record 呼出。 LLM 側 record 不要。

参考に渡されるpattern.structural_principleは**構造の地図**であり文章テンプレではない。各構造要素をconfigured persona自身の語彙・voiceで書く。

NG: personaにない代理人表現 / 一般論 / em-dash連発 / 中毒煽り / verbatim_blacklist.txtの任意phrase。投稿前にpersona_pathとvg_checkを必ず照合。
"
fi

REWRITE_AXES_JSON="$(printf '%s' "$PICK" | jq -c '.rewrite_axes // []')"

jq -nc \
  --argjson pattern "$PICK" \
  --arg persona_path "$PERSONA_FILE" \
  --argjson experience "$EXPERIENCE_JSON" \
  --arg channel "$CHANNEL" \
  --arg platform "$PLATFORM" \
  --arg account "$ACCOUNT" \
  --arg instruction "$INSTRUCTION" \
  --argjson rewrite_axes "$REWRITE_AXES_JSON" \
  --argjson candidate_count "$CANDIDATES" \
  '{
    pattern: $pattern,
    persona_path: $persona_path,
    experience: $experience,
    channel: $channel,
    platform: $platform,
    account: $account,
    anti_repeat_window_days: 14,
    instruction: $instruction,
    rewrite_axes: $rewrite_axes,
    candidate_count: $candidate_count
  }'
