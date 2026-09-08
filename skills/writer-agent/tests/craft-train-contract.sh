#!/usr/bin/env bash
# Contract for the nightly trainer (spec 47 19-4c T6).
#
# Fixtures and fakes only -- no live model calls, no live network. Two
# python interpreters are used deliberately: SKILLOPT_PY (the venv with
# the `skillopt` package installed) for anything that imports
# writing/rollout.py or writing/dataloader.py (both import skillopt
# modules at load time, even though nothing here calls a model through
# them -- a fake judge_fn/corpus fixture stands in); plain PY for
# craft_train.py, which has zero skillopt dependency.
set -uo pipefail

ROOT="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)}"
SKILL_DIR="$ROOT/skills/writer-agent"
VENDOR_DIR="$SKILL_DIR/vendor/skillopt-writing"

PY="${ARTICLE_PYTHON:-/opt/homebrew/bin/python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
SKILLOPT_PY="${SKILLOPT_PYTHON:-$PY}"
command -v "$SKILLOPT_PY" >/dev/null 2>&1 || SKILLOPT_PY="$PY"

CRAFT_TRAIN_SH="$SKILL_DIR/scripts/craft-train.sh"
cd "$SKILL_DIR"

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

# ---------------------------------------------------------------------------
# 1. The dataset builder / rollout prompt never puts the real title in the
#    item's prompt field.
# ---------------------------------------------------------------------------
T1_OUT="$("$SKILLOPT_PY" -c "
import sys
sys.path.insert(0, '$VENDOR_DIR')
from writing import rollout, dataloader

raw = {'id': 'x1', 'subject': 'a neutral, framing-stripped subject line',
       'title': 'THE REAL CLICKBAIT HEADLINE NOBODY SHOULD SEE', 'lang': 'en',
       'metric_primary': 5}
item = dataloader._normalize(raw)
assert item['subject'] == raw['subject']
assert item['reference_title'] == raw['title']
assert item['subject'] != item['reference_title']

prompt = rollout.build_user_prompt(item)
assert item['subject'] in prompt, 'subject missing from prompt'
assert item['reference_title'] not in prompt, 'REAL TITLE LEAKED into rollout prompt'
assert raw['title'] not in prompt, 'REAL TITLE LEAKED into rollout prompt (raw)'
print('PASS')
" 2>&1)"
check "dataset/rollout prompt never leaks the real title" "PASS" "$(echo "$T1_OUT" | tail -1)"
[ "$(echo "$T1_OUT" | tail -1)" = "PASS" ] || echo "$T1_OUT"

# ---------------------------------------------------------------------------
# 2. An item's own corpus row is excluded from its opponents.
# ---------------------------------------------------------------------------
T2_OUT="$("$SKILLOPT_PY" -c "
import sys
from pathlib import Path
sys.path.insert(0, '$VENDOR_DIR')
from writing import rollout

corpus_rows = [
    {'id': 'self1', 'lang': 'en', 'slice': 'top', 'metric_primary': 50, 'title': 'self title'},
    {'id': 'opp1', 'lang': 'en', 'slice': 'top', 'metric_primary': 40, 'title': 'opp one'},
    {'id': 'opp2', 'lang': 'en', 'slice': 'top', 'metric_primary': 30, 'title': 'opp two'},
    {'id': 'opp3', 'lang': 'en', 'slice': 'top', 'metric_primary': 20, 'title': 'opp three'},
]
item = {'id': 'self1', 'subject': 'neutral subject', 'lang': 'en'}

def fake_judge(runner, prompt, run_id):
    return '{\"winner\":\"A\",\"why\":\"fake deterministic, no model call\"}'

result = rollout.score_headline(
    'candidate headline', item, run_id='contract-test',
    runner=Path('/nonexistent'), judge_fn=fake_judge, corpus_rows=corpus_rows,
)
opp_ids = [p['opponent_id'] for p in result['pairs']]
assert opp_ids, 'no opponents sampled at all -- fixture broken'
assert 'self1' not in opp_ids, f'own row leaked into opponents: {opp_ids}'
print('PASS')
" 2>&1)"
check "item's own corpus row excluded from its opponents" "PASS" "$(echo "$T2_OUT" | tail -1)"
[ "$(echo "$T2_OUT" | tail -1)" = "PASS" ] || echo "$T2_OUT"

# ---------------------------------------------------------------------------
# 2b. A dataset containing a lang=other row excludes it (we do not publish
#     in Portuguese; that row is honest corpus data but not a split item).
# ---------------------------------------------------------------------------
T2B_OUT="$("$PY" -c "
import sys
sys.path.insert(0, '$VENDOR_DIR')
import build_dataset as bd

rows = [
    {'id': 'ja1', 'lang': 'ja', 'slice': 'top', 'metric_primary': 10, 'title': 'a real ja fixture headline'},
    {'id': 'en1', 'lang': 'en', 'slice': 'top', 'metric_primary': 10, 'title': 'a real en fixture headline, long enough'},
    {'id': 'pt1', 'lang': 'other', 'slice': 'top', 'metric_primary': 10, 'title': 'a real pt fixture headline, long enough'},
]
filtered = bd.eligible_rows(rows)
ids = {r['id'] for r in filtered}
assert 'pt1' not in ids, f'lang=other row leaked into eligible rows: {ids}'
assert {'ja1', 'en1'} <= ids, f'ja/en rows wrongly excluded: {ids}'

membership = bd.stratified_membership(filtered, target_counts={'train': 1, 'val': 1, 'test': 1})
all_ids = {r['id'] for lang in membership for split in membership[lang] for r in membership[lang][split]}
assert 'pt1' not in all_ids, f'lang=other row leaked into split membership: {all_ids}'
print('PASS')
" 2>&1)"
check "a lang=other row is excluded from the dataset" "PASS" "$(echo "$T2B_OUT" | tail -1)"
[ "$(echo "$T2B_OUT" | tail -1)" = "PASS" ] || echo "$T2B_OUT"

# ---------------------------------------------------------------------------
# 2c. An empty split is refused (exit nonzero / raise, naming which one),
#     and nothing gets written to disk when it fires.
# ---------------------------------------------------------------------------
T2C_OUT="$("$PY" -c "
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, '$VENDOR_DIR')
import build_dataset as bd

with tempfile.TemporaryDirectory() as td:
    corpus_dir = Path(td, 'corpus')
    corpus_dir.mkdir()
    split_dir = Path(td, 'splits')

    # 6 ja rows (enough to fill train/val/test under a tiny target), but
    # only 1 en row -- not enough for en to populate all three splits, so
    # en/val and/or en/test must come out empty.
    rows = [{'id': f'ja{i}', 'lang': 'ja', 'slice': 'top', 'metric_primary': 10, 'title': f'a real ja fixture headline {i}'} for i in range(6)]
    rows.append({'id': 'en1', 'lang': 'en', 'slice': 'top', 'metric_primary': 10, 'title': 'a real en fixture headline long enough'})
    (corpus_dir / 'fixture.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8',
    )

    def fake_judge(runner, prompt, run_id):
        return '{\"subject\": \"fake neutral subject, no model call\"}'

    raised = False
    try:
        bd.build(
            corpus_dir, split_dir, runner=Path('/nonexistent'), run_id='contract-test',
            judge_fn=fake_judge, target_counts={'train': 2, 'val': 2, 'test': 2},
        )
    except bd.EmptySplitError as exc:
        raised = True
        msg = str(exc)
        assert 'en/' in msg, f'refusal message does not name the empty en split: {msg}'

    assert raised, 'build() did not refuse an empty split -- it should have raised EmptySplitError'
    assert not split_dir.exists() or not any(split_dir.rglob('items.json')), 'a split file was written despite the refusal'
    print('PASS')
" 2>&1)"
check "an empty split is refused and nothing is written" "PASS" "$(echo "$T2C_OUT" | tail -1)"
[ "$(echo "$T2C_OUT" | tail -1)" = "PASS" ] || echo "$T2C_OUT"

# ---------------------------------------------------------------------------
# 2d. --max-per-lang caps the eligible pool to the HIGHEST metric_primary
#     rows, not an arbitrary first-N-by-id (post-review fix: a daily
#     rebuild must not re-spend judge calls at full-corpus scale, and the
#     top of the distribution -- not whichever ids sort first -- is what
#     we want to imitate).
# ---------------------------------------------------------------------------
T2D_OUT="$("$PY" -c "
import sys
sys.path.insert(0, '$VENDOR_DIR')
import build_dataset as bd

# ids sort z < a alphabetically here on purpose: a first-N-by-id cap would
# keep 'zzz-low' (low score) over 'aaa-high' (high score) if it ignored
# metric_primary. Correct behavior keeps the two highest-scoring rows.
rows = [
    {'id': 'zzz-low1', 'lang': 'en', 'metric_primary': 5},
    {'id': 'zzz-low2', 'lang': 'en', 'metric_primary': 4},
    {'id': 'aaa-high1', 'lang': 'en', 'metric_primary': 99},
    {'id': 'aaa-high2', 'lang': 'en', 'metric_primary': 98},
]
capped = bd.cap_eligible_per_lang(rows, max_per_lang=2)
ids = {r['id'] for r in capped}
assert ids == {'aaa-high1', 'aaa-high2'}, f'cap kept the wrong rows: {ids}'
print('PASS')
" 2>&1)"
check "--max-per-lang caps by highest metric_primary, not first-by-id" "PASS" "$(echo "$T2D_OUT" | tail -1)"
[ "$(echo "$T2D_OUT" | tail -1)" = "PASS" ] || echo "$T2D_OUT"

# ---------------------------------------------------------------------------
# 2e. The subject cache survives deletion of data/writing_split -- that
#     exact sequence (`rm -rf data/writing_split && rebuild`) is what lost
#     every already-paid-for subject the first time. Two parts: (a) the
#     cache file itself, which lives OUTSIDE split_dir, is untouched by
#     deleting split_dir; (b) a full build() after that deletion reuses
#     every cached subject with ZERO new judge calls.
# ---------------------------------------------------------------------------
T2E_OUT="$("$PY" -c "
import sys, tempfile, json, shutil
from pathlib import Path
sys.path.insert(0, '$VENDOR_DIR')
import build_dataset as bd

with tempfile.TemporaryDirectory() as td:
    split_dir = Path(td, 'data', 'writing_split')
    split_dir.mkdir(parents=True)
    cache_path = bd.default_cache_path(split_dir)
    assert cache_path == Path(td, 'data', 'subject-cache.json'), f'unexpected cache path: {cache_path}'

    # (a) direct cache-function check: persist, then rm -rf split_dir.
    bd.save_subject_cache(cache_path, {'row1': 'a cached subject'})
    shutil.rmtree(split_dir)
    assert not split_dir.exists()
    assert cache_path.exists(), 'cache file was deleted by rm -rf of split_dir -- it must live outside it'
    loaded = bd.load_existing_subjects(split_dir, cache_path)
    assert loaded.get('row1') == 'a cached subject', f'cache did not survive split_dir deletion: {loaded}'
    print('PART_A:PASS')

    # (b) end-to-end: a real build(), then rm -rf split_dir, then a SECOND
    # build() whose judge_fn always fails -- if the cache did not survive,
    # every row would come back skipped (derive_subject returns None on a
    # bad judge reply); if it did survive, every row is 'reused' and NONE
    # are 'generated', proving zero new judge calls were needed.
    corpus_dir = Path(td, 'corpus')
    corpus_dir.mkdir()
    # ids chosen so bucket_for lands one in each of train/val/test, per
    # language, with no hand-waving about hash luck.
    rows = [
        {'id': 'ja-cachetest-1', 'lang': 'ja', 'slice': 'top', 'metric_primary': 10, 'title': 'a real ja cache-test train headline'},
        {'id': 'ja-cachetest-0', 'lang': 'ja', 'slice': 'top', 'metric_primary': 10, 'title': 'a real ja cache-test val headline'},
        {'id': 'ja-cachetest-5', 'lang': 'ja', 'slice': 'top', 'metric_primary': 10, 'title': 'a real ja cache-test test headline'},
        {'id': 'en-cachetest-1', 'lang': 'en', 'slice': 'top', 'metric_primary': 10, 'title': 'a real en cache-test train headline'},
        {'id': 'en-cachetest-0', 'lang': 'en', 'slice': 'top', 'metric_primary': 10, 'title': 'a real en cache-test val headline'},
        {'id': 'en-cachetest-27', 'lang': 'en', 'slice': 'top', 'metric_primary': 10, 'title': 'a real en cache-test test headline'},
    ]
    (corpus_dir / 'fixture.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8',
    )

    def working_judge(runner, prompt, run_id):
        return '{\"subject\": \"fake neutral subject, no model call\"}'

    result1 = bd.build(
        corpus_dir, split_dir, runner=Path('/nonexistent'), run_id='contract-test',
        judge_fn=working_judge, target_counts={'train': 1, 'val': 1, 'test': 1}, max_per_lang=0,
    )
    assert result1['subjects_generated'] == 6, f'expected 6 fresh subjects, got {result1}'
    assert cache_path.exists(), 'cache file was not written after a real build'

    shutil.rmtree(split_dir)
    assert not split_dir.exists()

    def failing_judge(runner, prompt, run_id):
        return 'not json at all -- infrastructure failure stand-in'

    result2 = bd.build(
        corpus_dir, split_dir, runner=Path('/nonexistent'), run_id='contract-test-2',
        judge_fn=failing_judge, target_counts={'train': 1, 'val': 1, 'test': 1}, max_per_lang=0,
    )
    assert result2['subjects_generated'] == 0, f'a rebuild after rm -rf split_dir re-spent judge calls: {result2}'
    assert result2['subjects_reused'] == 6, f'a rebuild after rm -rf split_dir did not reuse the cache: {result2}'
    assert result2['rows_skipped'] == 0, f'rows were skipped despite a full cache hit: {result2}'
    print('PART_B:PASS')
" 2>&1)"
check "cache survives split_dir deletion (direct check)" "PART_A:PASS" "$(echo "$T2E_OUT" | grep '^PART_A:')"
check "a rebuild after rm -rf split_dir costs zero new judge calls" "PART_B:PASS" "$(echo "$T2E_OUT" | grep '^PART_B:')"
[ "$(echo "$T2E_OUT" | tail -1)" = "PART_B:PASS" ] || echo "$T2E_OUT"

# ---------------------------------------------------------------------------
# 2f. A bare product-name title ("Claude Sonnet 5") is excluded -- its
#     score came from the product being announced, not from any craft in
#     the headline (the Stephen Hawking contamination pattern in a
#     different shape), and training against it pushes the model toward
#     violating title-best-practices.md ban 1 (an undefined proper noun in
#     the headline). A normal-length headline is kept, proving this is a
#     targeted filter, not a blanket exclusion.
# ---------------------------------------------------------------------------
T2F_OUT="$("$PY" -c "
import sys
sys.path.insert(0, '$VENDOR_DIR')
import build_dataset as bd

rows = [
    {'id': 'bare1', 'lang': 'en', 'slice': 'top', 'metric_primary': 10, 'title': 'Claude Sonnet 5'},
    {'id': 'normal1', 'lang': 'en', 'slice': 'top', 'metric_primary': 10,
     'title': 'How we cut our AWS bill by 40 percent in one weekend'},
]
filtered = bd.eligible_rows(rows)
ids = {r['id'] for r in filtered}
assert 'bare1' not in ids, f'bare product title \"Claude Sonnet 5\" was not excluded: {ids}'
assert 'normal1' in ids, f'a normal-length headline was wrongly excluded: {ids}'
assert bd.count_bare_title_excluded(rows) == 1, f'exclusion count wrong: {bd.count_bare_title_excluded(rows)}'
print('PASS')
" 2>&1)"
check "bare product title excluded, normal headline kept" "PASS" "$(echo "$T2F_OUT" | tail -1)"
[ "$(echo "$T2F_OUT" | tail -1)" = "PASS" ] || echo "$T2F_OUT"

# ---------------------------------------------------------------------------
# 3. A rejected epoch leaves CRAFT.md byte-identical (sha256 before == after).
# 4. The protected SLOW_UPDATE block is never modified even when an edit
#    targets text inside it.
# ---------------------------------------------------------------------------
T34_OUT="$("$PY" -c "
import sys, tempfile
from pathlib import Path
sys.path.insert(0, '$SKILL_DIR/scripts')
import craft_train as ct

with tempfile.TemporaryDirectory() as td:
    craft_path = Path(td, 'CRAFT.md')
    old_content = (
        'line1\nline2\n<!-- SLOW_UPDATE_START -->\n'
        'protected text\n<!-- SLOW_UPDATE_END -->\n'
    )
    craft_path.write_text(old_content, encoding='utf-8')

    # 3: reject -> byte-identical
    r = ct.apply_result(craft_path, old_content, accepted=False, new_skill_content=None)
    assert r['applied'] is False
    assert r['sha_before'] == r['sha_after']
    assert craft_path.read_text(encoding='utf-8') == old_content
    print('T3:PASS')

    # 4: accept, but the proposed edit targets the protected block -> refuse,
    # file stays byte-identical
    tampered = (
        'line1 EDITED\nline2\n<!-- SLOW_UPDATE_START -->\n'
        'TAMPERED protected text\n<!-- SLOW_UPDATE_END -->\n'
    )
    r2 = ct.apply_result(craft_path, old_content, accepted=True, new_skill_content=tampered)
    assert r2['applied'] is False, r2
    assert 'protected' in (r2['refuse_reason'] or '').lower()
    assert r2['sha_before'] == r2['sha_after']
    assert craft_path.read_text(encoding='utf-8') == old_content
    print('T4:PASS')

    # 4b sanity: a safe edit (protected block untouched) DOES apply, proving
    # the refusal above is really about the protected block, not a blanket
    # no-op.
    safe = (
        'line1 EDITED SAFELY\nline2\n<!-- SLOW_UPDATE_START -->\n'
        'protected text\n<!-- SLOW_UPDATE_END -->\n'
    )
    r3 = ct.apply_result(craft_path, old_content, accepted=True, new_skill_content=safe)
    assert r3['applied'] is True, r3
    assert r3['sha_before'] != r3['sha_after']
    print('T4b:PASS')
" 2>&1)"
check "rejected epoch leaves CRAFT.md byte-identical" "T3:PASS" "$(echo "$T34_OUT" | grep '^T3:')"
check "protected SLOW_UPDATE block never modified by a targeting edit" "T4:PASS" "$(echo "$T34_OUT" | grep '^T4:')"
check "a safe edit (protected block untouched) does apply" "T4b:PASS" "$(echo "$T34_OUT" | grep '^T4b:')"
[ "$(echo "$T34_OUT" | tail -1)" = "T4b:PASS" ] || echo "$T34_OUT"

# ---------------------------------------------------------------------------
# 4c. The margin gate (round-4/T15 follow-up: noise wearing the costume of
#     a measurement). Fixture: 15 non-null opponent pairs on each side (30
#     comparisons each), so SE_before=SE_after=sqrt(0.25/30)=0.0913,
#     SE_diff=sqrt(2*0.0913^2)=0.1291, margin_required=2*SE_diff=0.2582.
#     An improvement of 0.20 (below margin) must reject and leave CRAFT.md
#     byte-identical; an improvement of 0.30 (above margin) must accept.
#     Both rows must record the margin and the standard error used.
# ---------------------------------------------------------------------------
MARGIN_OUT="$("$PY" -c "
import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, '$SKILL_DIR/scripts')
import craft_train as ct

def write_rollouts(path, n_non_null_pairs, soft=0.5):
    # The gate now averages soft over the SAME items it counts pairs from, so
    # the fixture has to carry the mean it wants tested rather than declaring
    # it in summary.json (spec 47 section 21.45). soft is what the item scored.
    path.parent.mkdir(parents=True, exist_ok=True)
    pairs = [{'opponent_id': f'opp{i}', 'opponent_source': 'zenn', 'score': 0.5} for i in range(n_non_null_pairs)]
    episode = {'id': 'item1', 'beat_rate': soft, 'soft': soft, 'opponent_pairs': pairs}
    path.write_text(json.dumps([episode]), encoding='utf-8')

craft_content = (
    'line1\n<!-- SLOW_UPDATE_START -->\nprotected\n<!-- SLOW_UPDATE_END -->\n'
)

# Case A: improvement 0.20, BELOW the 0.2582 margin -> must reject.
with tempfile.TemporaryDirectory() as td:
    out_root = Path(td, 'out')
    write_rollouts(out_root / 'test_eval_baseline' / 'rollouts.json', 15, soft=0.30)
    write_rollouts(out_root / 'test_eval' / 'rollouts.json', 15, soft=0.50)
    summary = {'total_accepts': 1, 'total_rejects': 0,
               'baseline_test_soft': 0.30, 'test_soft': 0.50}
    verdict = ct.decide_from_summary(summary, out_root)
    assert verdict['accepted'] is False, verdict
    assert abs(verdict['margin_required'] - 0.2581988897471611) < 1e-9, verdict
    assert abs(verdict['standard_error'] - 0.12909944487358055) < 1e-9, verdict

    craft_path = Path(td, 'CRAFT.md')
    craft_path.write_text(craft_content, encoding='utf-8')
    applied = ct.apply_result(craft_path, craft_content, verdict['accepted'], 'anything different')
    assert applied['applied'] is False, applied
    assert applied['sha_before'] == applied['sha_after'], applied
    assert craft_path.read_text(encoding='utf-8') == craft_content
    print('CASE_A:PASS')

# Case B: improvement 0.30, ABOVE the 0.2582 margin -> must accept.
with tempfile.TemporaryDirectory() as td:
    out_root = Path(td, 'out')
    write_rollouts(out_root / 'test_eval_baseline' / 'rollouts.json', 15, soft=0.30)
    write_rollouts(out_root / 'test_eval' / 'rollouts.json', 15, soft=0.60)
    summary = {'total_accepts': 1, 'total_rejects': 0,
               'baseline_test_soft': 0.30, 'test_soft': 0.60}
    verdict = ct.decide_from_summary(summary, out_root)
    assert verdict['accepted'] is True, verdict
    print('CASE_B:PASS')

# Case C: the jsonl row records both margin_required and standard_error.
line = ct.build_jsonl_line(
    edits_proposed=1, edits_accepted=0, beat_before=0.30, beat_after=0.50,
    sha_before='a'*64, sha_after='a'*64, reason='no evidence',
    margin_required=0.2581988897471611, standard_error=0.12909944487358055,
    n_comparisons_before=30, n_comparisons_after=30,
)
record = json.loads(line)
assert abs(record['margin_required'] - 0.2581988897471611) < 1e-9, record
assert abs(record['standard_error'] - 0.12909944487358055) < 1e-9, record
assert record['n_comparisons_before'] == 30 and record['n_comparisons_after'] == 30, record
print('CASE_C:PASS')

# Case D: an outage item scores soft 0.0 with zero non-null pairs. It must not
# drag the mean, because it contributes nothing to the comparison count either
# -- numerator and denominator drawn from the same set (spec 47 section 21.45).
with tempfile.TemporaryDirectory() as td:
    out_root = Path(td, 'out')
    good = {'id': 'ok', 'soft': 0.60, 'beat_rate': 0.60,
            'opponent_pairs': [{'opponent_id': 'o1', 'score': 1.0}]}
    outage = {'id': 'down', 'soft': 0.0, 'beat_rate': 0.0,
              'opponent_pairs': [{'opponent_id': 'o2', 'score': None}]}
    path = out_root / 'test_eval' / 'rollouts.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([good, outage]), encoding='utf-8')
    mean = ct._scored_mean(path)
    assert abs(mean - 0.60) < 1e-9, ('outage item polluted the mean', mean)
    print('CASE_D:PASS')
" 2>&1)"
check "an improvement below the margin is rejected, CRAFT.md byte-identical" "CASE_A:PASS" "$(echo "$MARGIN_OUT" | grep '^CASE_A:')"
check "an improvement above the margin is accepted" "CASE_B:PASS" "$(echo "$MARGIN_OUT" | grep '^CASE_B:')"
check "the jsonl row records margin_required and standard_error" "CASE_C:PASS" "$(echo "$MARGIN_OUT" | grep '^CASE_C:')"
check "an outage item does not drag the held-out mean" "CASE_D:PASS" "$(echo "$MARGIN_OUT" | grep '^CASE_D:')"
[ "$(echo "$MARGIN_OUT" | tail -1)" = "CASE_C:PASS" ] || echo "$MARGIN_OUT"

# ---------------------------------------------------------------------------
# 5. The fewer-than-3-scored-runs guard exits 0 without running.
# ---------------------------------------------------------------------------
T5_DIR="$(mktemp -d)"
trap 'rm -rf "$T5_DIR"' EXIT

mkdir -p "$T5_DIR/runs/run1/gates" "$T5_DIR/runs/run2/gates"
: > "$T5_DIR/runs/run1/gates/beat-rate-en.json"
: > "$T5_DIR/runs/run2/gates/beat-rate-en.json"
# 2 scored runs -- below the 3-run floor.

FIXTURE_CRAFT="$T5_DIR/CRAFT.md"
printf 'line1\n<!-- SLOW_UPDATE_START -->\nprotected\n<!-- SLOW_UPDATE_END -->\n' > "$FIXTURE_CRAFT"

FIXTURE_CONF="$T5_DIR/cliproxyapi.conf"
{
  echo "port: 9999"
  echo "api-keys:"
  echo '  - "fixture-test-key-not-real"'
} > "$FIXTURE_CONF"

# A "spy" standing in for the skillopt venv python: if craft-train.sh's
# guard did NOT short-circuit before invoking the trainer, this would run
# and leave a marker file behind. It must never appear.
SPY="$T5_DIR/spy-skillopt-python.sh"
cat > "$SPY" <<SPYEOF
#!/usr/bin/env bash
if [ "\${1:-}" = "-c" ]; then
  exit 0
fi
touch "$T5_DIR/spy-invoked"
exit 0
SPYEOF
chmod +x "$SPY"

SHA_BEFORE="$(shasum -a 256 "$FIXTURE_CRAFT" | awk '{print $1}')"

CRAFT_TRAIN_JSONL="$T5_DIR/craft-train.jsonl" \
CRAFT_TRAIN_RUNS_ROOT="$T5_DIR/runs" \
CRAFT_MD="$FIXTURE_CRAFT" \
CLIPROXY_CONF="$FIXTURE_CONF" \
SKILLOPT_PYTHON="$SPY" \
  bash "$CRAFT_TRAIN_SH" >"$T5_DIR/stdout.log" 2>"$T5_DIR/stderr.log"
T5_EXIT=$?

SHA_AFTER="$(shasum -a 256 "$FIXTURE_CRAFT" | awk '{print $1}')"

check "guard run: craft-train.sh exits 0" "0" "$T5_EXIT"
check "guard run: CRAFT.md untouched" "$SHA_BEFORE" "$SHA_AFTER"
check "guard run: trainer subprocess never invoked" "False" "$([ -f "$T5_DIR/spy-invoked" ] && echo True || echo False)"
GUARD_REASON="$(grep -o '"reason": *"[^"]*guard[^"]*"' "$T5_DIR/craft-train.jsonl" 2>/dev/null | head -1)"
check "guard run: jsonl records a guard reason" "True" "$([ -n "$GUARD_REASON" ] && echo True || echo False)"

TRACE_SECRET="writer-contract-secret-must-not-appear"
ARTICLE_OPENAI_API_KEY="$TRACE_SECRET" \
CRAFT_TRAIN_JSONL="$T5_DIR/trace-craft-train.jsonl" \
CRAFT_TRAIN_RUNS_ROOT="$T5_DIR/runs" \
CRAFT_MD="$FIXTURE_CRAFT" \
SKILLOPT_PYTHON="$SPY" \
  bash -x "$CRAFT_TRAIN_SH" >"$T5_DIR/trace.log" 2>&1
check "xtrace never emits the direct credential" "False" "$(rg -q "$TRACE_SECRET" "$T5_DIR/trace.log" && echo True || echo False)"

# ---------------------------------------------------------------------------
# 6. T15 wall-clock follow-up, part 4: the projection refusal fires when
#    sizes are too large (the real train=119/val=21/test=38 split, WITHOUT
#    the new limit/sel_env_num/test_env_num caps, projects to ~16 hours at
#    10s/call and must not fit a ~6-hour window), and fits comfortably once
#    the caps this task adds (20/10/8) are applied -- exactly reproducing
#    the coordinator's own arithmetic (200 + 600 + 720 = 1,520 calls).
# ---------------------------------------------------------------------------
PROJECTION_OUT="$("$PY" -c "
import sys, tempfile, json
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, '$SKILL_DIR/scripts')
import craft_train as ct

with tempfile.TemporaryDirectory() as td:
    split_dir = Path(td, 'splits')
    for name, n in [('train', 119), ('val', 21), ('test', 38)]:
        d = split_dir / name
        d.mkdir(parents=True)
        (d / 'items.json').write_text(json.dumps([{'id': f'{name}{i}'} for i in range(n)]), encoding='utf-8')

    now = datetime(2026, 1, 1, 23, 10, 0)
    deadline = now + timedelta(hours=5, minutes=50)  # 23:10 -> 05:00 next day

    uncapped = {'env': {'limit': 0}, 'evaluation': {'sel_env_num': 0, 'test_env_num': 0}}
    p1 = ct.project_and_check_deadline(uncapped, split_dir, now=now, deadline=deadline, seconds_per_call=10)
    assert p1['fits_before_deadline'] is False, p1
    assert p1['total_calls'] > 5000, p1
    print('TOO_LARGE:PASS', p1['total_calls'])

    capped = {'env': {'limit': 20}, 'evaluation': {'sel_env_num': 10, 'test_env_num': 8}}
    p2 = ct.project_and_check_deadline(capped, split_dir, now=now, deadline=deadline, seconds_per_call=10)
    assert p2['fits_before_deadline'] is True, p2
    assert p2['total_calls'] == 1520, p2
    print('FITS:PASS', p2['total_calls'])
" 2>&1)"
check "projection refuses an oversized (uncapped) run" "TOO_LARGE:PASS" "$(echo "$PROJECTION_OUT" | sed -n '1p' | awk '{print $1}')"
check "projection fits the capped (20/10/8) sizing, matching the arithmetic" "FITS:PASS" "$(echo "$PROJECTION_OUT" | sed -n '2p' | awk '{print $1}')"
[ "$(echo "$PROJECTION_OUT" | sed -n '2p' | awk '{print $1}')" = "FITS:PASS" ] || echo "$PROJECTION_OUT"

# ---------------------------------------------------------------------------
# 7. T15 wall-clock follow-up, part 3: the deadline path stops cleanly and
#    leaves CRAFT.md byte-identical. A REAL (but harmless -- just `sleep`,
#    no model call, no network) subprocess stands in for a run that would
#    still be going at the deadline; run_training's own subprocess timeout
#    must fire and report a reason starting with "deadline", and end to
#    end through main() the craft file must never be touched.
# ---------------------------------------------------------------------------
DEADLINE_OUT="$("$PY" -c "
import sys, tempfile, time, json
from pathlib import Path
sys.path.insert(0, '$SKILL_DIR/scripts')
import craft_train as ct

with tempfile.TemporaryDirectory() as td:
    stall = Path(td, 'stall.sh')
    stall.write_text('#!/usr/bin/env bash\nsleep 10\n', encoding='utf-8')
    stall.chmod(0o755)

    result = ct.run_training(stall, Path('unused-run-train'), Path('unused-config'), Path(td, 'out'), timeout_s=1)
    assert result['ok'] is False, result
    assert result['reason'].startswith('deadline'), result
    print('REASON:PASS')

    split_dir = Path(td, 'splits')
    for name in ('train', 'val', 'test'):
        d = split_dir / name
        d.mkdir(parents=True)
        (d / 'items.json').write_text(json.dumps([{'id': f'{name}1'}]), encoding='utf-8')
    config_path = Path(td, 'default.yaml')
    config_path.write_text(
        f'env:\n  limit: 1\n  split_dir: {split_dir}\nevaluation:\n  sel_env_num: 1\n  test_env_num: 1\n',
        encoding='utf-8',
    )
    craft_path = Path(td, 'CRAFT.md')
    craft_content = 'line1\n<!-- SLOW_UPDATE_START -->\nprotected\n<!-- SLOW_UPDATE_END -->\n'
    craft_path.write_text(craft_content, encoding='utf-8')
    runs_root = Path(td, 'runs')
    for i in (1, 2, 3):
        g = runs_root / f'run{i}' / 'gates'
        g.mkdir(parents=True)
        (g / 'beat-rate-en.json').write_text('{}', encoding='utf-8')
    jsonl_path = Path(td, 'craft-train.jsonl')
    # Deadline just 3s out; --seconds-per-call tiny so the PROJECTION
    # fits (this is testing the subprocess deadline, not the projection
    # refusal from test 6) while the real stall.sh (sleep 10) genuinely
    # outlives the actual wall-clock budget main() computes.
    deadline_epoch = int(time.time()) + 3

    rc = ct.main([
        '--craft-md', str(craft_path), '--config', str(config_path),
        '--split-dir', str(split_dir),
        '--run-train', 'unused-run-train', '--skillopt-python', str(stall),
        '--runs-root', str(runs_root), '--out-root', str(Path(td, 'trainout')),
        '--jsonl', str(jsonl_path), '--deadline-epoch', str(deadline_epoch),
        '--seconds-per-call', '0.001',
    ])
    assert rc == 0, rc
    assert craft_path.read_text(encoding='utf-8') == craft_content, 'CRAFT.md was modified on the deadline path'
    last = json.loads(jsonl_path.read_text(encoding='utf-8').splitlines()[-1])
    assert last['reason'].startswith('deadline'), last
    assert last['craft_sha256_before'] == last['craft_sha256_after'], last
    print('E2E:PASS')
" 2>&1)"
check "run_training reports a deadline reason on a real subprocess timeout" "REASON:PASS" "$(echo "$DEADLINE_OUT" | grep '^REASON:')"
check "end-to-end deadline path leaves CRAFT.md byte-identical" "E2E:PASS" "$(echo "$DEADLINE_OUT" | grep '^E2E:')"
[ "$(echo "$DEADLINE_OUT" | tail -1)" = "E2E:PASS" ] || echo "$DEADLINE_OUT"

# The opponent counts are duplicated in craft_train.py and writing/rollout.py
# on purpose (importing rollout would drag in the skillopt venv), and a
# duplicated constant that must stay in sync is exactly the drift this whole
# session kept finding. If they diverge, the pre-flight projection silently
# estimates with the wrong n -- the same arithmetic that caught the 22-hour
# run. Pin them together rather than trusting the comment that says they match.
SYNC=$("$PY" - <<'PYEOF'
import re
from pathlib import Path
def read(path):
    text = Path(path).read_text(encoding="utf-8")
    return {name: int(re.search(rf"^{name} = (\d+)", text, re.M).group(1))
            for name in ("TRAIN_OPPONENTS", "GATE_OPPONENTS")}
a = read("scripts/craft_train.py")
b = read("vendor/skillopt-writing/writing/rollout.py")
print("same" if a == b else f"DIVERGED {a} vs {b}")
PYEOF
)
check "opponent counts stay in sync across both modules" "same" "$SYNC"

PORTABLE=$("$PY" - <<'PYEOF'
from pathlib import Path
root = Path.cwd()
config = root / "vendor/skillopt-writing/configs/writing/default.yaml"
base = root / "vendor/skillopt-writing/configs/_base_/default.yaml"
wrapper = (root / "scripts/craft-train.sh").read_text(encoding="utf-8")
driver = (root / "scripts/craft_train.py").read_text(encoding="utf-8")
requirements = (root.parent.parent / "requirements-runtime.txt").read_text(encoding="utf-8")
text = config.read_text(encoding="utf-8")
assert base.is_file()
assert "_base_: ../_base_/default.yaml" in text
assert "optimizer_backend: openai_chat" in text
assert "target_backend: openai_chat" in text
assert "/tmp/SkillOpt" not in text and "/Users/" not in text
assert 'SKILLOPT_PYTHON="${SKILLOPT_PYTHON:-$PY}"' in wrapper
assert "/opt/homebrew/etc/cliproxyapi.conf" not in wrapper
assert "ARTICLE_OPENAI_API_KEY" in wrapper
assert "ARTICLE_OPENAI_BASE_URL" in wrapper
assert "date -j" not in wrapper
assert '--split-dir "$SPLIT_DIR"' in wrapper
assert 'OUT_ROOT="$STATE_DIR/craft-train-output/' in wrapper
assert '"--cfg-options", *config_overrides' in driver
assert "skillopt==0.2.0" in requirements
print("portable")
PYEOF
)
check "SkillOpt trainer has no checkout/home-venv dependency" "portable" "$PORTABLE"

BACKEND_CONFIG=$("$SKILLOPT_PY" - <<'PYEOF'
from skillopt.config import flatten_config, load_config
from skillopt.model.backend_config import set_optimizer_backend, set_target_backend
cfg = flatten_config(load_config(
    "vendor/skillopt-writing/configs/writing/default.yaml",
    ["env.skill_init=/tmp/craft.md", "env.split_dir=/tmp/splits"],
))
set_optimizer_backend(cfg["optimizer_backend"])
set_target_backend(cfg["target_backend"])
assert cfg["skill_init"] == "/tmp/craft.md"
assert cfg["split_dir"] == "/tmp/splits"
print("accepted")
PYEOF
)
check "SkillOpt 0.2.0 accepts configured backends and path overrides" "accepted" "$BACKEND_CONFIG"

DST=$("$PY" - <<'PYEOF'
import os, sys, time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path("scripts").resolve()))
from craft_deadline import next_deadline
old = os.environ.get("TZ")
try:
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    now = int(time.mktime(datetime(2026, 3, 7, 23, 10).timetuple()))
    _, deadline = next_deadline(5, 0, now_epoch=now)
    assert datetime.fromtimestamp(deadline) == datetime(2026, 3, 8, 5, 0)
    assert deadline - now == 4 * 3600 + 50 * 60
    fall_now = int(time.mktime(datetime(2026, 10, 31, 23, 10).timetuple()))
    _, fall_deadline = next_deadline(5, 0, now_epoch=fall_now)
    assert datetime.fromtimestamp(fall_deadline) == datetime(2026, 11, 1, 5, 0)
    assert fall_deadline - fall_now == 6 * 3600 + 50 * 60
    print("dst-safe")
finally:
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()
PYEOF
)
check "next local deadline preserves DST wall time" "dst-safe" "$DST"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
