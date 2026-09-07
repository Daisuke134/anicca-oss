#!/usr/bin/env bash
# earn/bounty — ONE Anicca loop slot. Algora GitHub bounties (the one VERIFIED-live platform 2026):
# claim an OPEN bounty → submit a PR → TRACK the PR thread until MERGED → payout. EARN_MODE:
#   discover — gh-search live open Algora bounties → state/bounties.json (agent-doable ranked).
#   track    — for each attempt in state/attempts.jsonl, poll its PR (reviews/state); on MERGE,
#              record-earn (real external USDC only, INV-7). The loop is NOT one-off: it iterates.
# NO HUMAN for discover/track; the PR coding is the brain's job; payout KYC (Stripe) = honest gap.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EARN_MODE="${EARN_MODE:-discover}"
WAKE="${WAKE_ID:-$(date -u +%s)}"
PY=/opt/homebrew/bin/python3
[ -x "$PY" ] || PY=python3
STATE="${BOUNTY_STATE_DIR:-$HERE/state}"; mkdir -p "$STATE"
emit(){ printf '{"slot":"earn/bounty","did":%s,"earn_usdc":0,"cost_usdc":0}\n' \
  "$(printf '%s' "$1" | "$PY" -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"; }

discover(){
  local bj="$STATE/bounties.json" raw="$STATE/.algora_search_raw.json" raw2="$STATE/.algora_search_raw2.json"
  # live open Algora bounties (the algora-pbc bot comments on the funded issue)
  # NOTE: gh api output is written to a file, not piped into python's stdin — a heredoc
  # (<<'PY') supplying the python SCRIPT ITSELF also occupies stdin, so a pipe into the
  # same command's stdin is silently discarded and json.load(sys.stdin) sees EOF (this
  # previously made discover() always report 0 bounties no matter what gh returned).
  gh api -X GET search/issues -f q='commenter:algora-pbc state:open' -f per_page=50 2>/dev/null > "$raw"
  # WIDEN (Daisuke134/anicca#997, self-filed after 5 consecutive flat-38 days confirmed via manual
  # spot-checks to be genuine multi-agent PR-farming saturation, not a gate bug): commenter:algora-pbc
  # misses issues where the algora-pbc account is otherwise involved (assigned, mentioned, reacted)
  # but GH's narrower "commenter" qualifier doesn't index it as a top-level comment. involves: stays
  # 100% inside the Algora ecosystem (same trusted bot account, no cross-platform noise) — unlike
  # label:bounty/💎-Bounty-label queries tested and rejected (verified 2026-07-11: those surface
  # mostly AI-agent-farm honeypot repos like agent-playground/bug-bounty/Bounty-Hunters with zero
  # real algora-pbc bounty comments). gate()'s existing active-comment + USD-from-comment checks are
  # UNCHANGED and remain the real safety boundary -- any noise this widening adds still gets
  # correctly rejected there, never auto-attempted.
  gh api -X GET search/issues -f q='involves:algora-pbc state:open is:issue' -f per_page=100 2>/dev/null > "$raw2"
  "$PY" - "$bj" "$WAKE" "$raw" "$raw2" <<'PY' 2>/dev/null
import json,sys,re,os
d=json.load(open(sys.argv[3])) if os.path.getsize(sys.argv[3])>0 else {}
items=list(d.get("items",[]))
d2=json.load(open(sys.argv[4])) if os.path.getsize(sys.argv[4])>0 else {}
seen_urls={it.get("html_url") for it in items}
for it in d2.get("items",[]):
    if it.get("html_url") not in seen_urls:
        items.append(it)
        seen_urls.add(it.get("html_url"))
out=[]
for it in items:
    if it.get("pull_request"): continue          # issues only
    if it.get("assignee"): continue              # skip assigned
    repo=re.sub(r'.*/repos/','',it.get("repository_url","")).strip()
    out.append({"title":it.get("title","")[:80],"url":it.get("html_url",""),"repo":repo,
                "comments":it.get("comments",0)})
# lessons.jsonl 2026-07-08/09: gate wastes gh API calls checking high-comment issues that are
# already saturated with competing PRs. Comment count is a cheap low-competition proxy available
# at discover time (no extra API call) — check the least-contested issues first.
out.sort(key=lambda o: o["comments"])
import os
# GitHub search intermittently returns total_count>0 but items:[] (rate-limit/consistency).
# Don't clobber good prior data on a transient empty response (checked against the primary query
# only -- the secondary involves: query merging zero extra items is not itself a failure signal).
if not d.get("items") and os.path.exists(sys.argv[1]):
    print(len(json.load(open(sys.argv[1])).get("open_bounties",[]))); sys.exit()
json.dump({"fetched_at":sys.argv[2],"open_bounties":out}, open(sys.argv[1],"w"), indent=2)
print(len(out))
PY
  local n; n=$([ -f "$bj" ] && "$PY" -c "import json;print(len(json.load(open('$bj')).get('open_bounties',[])))" 2>/dev/null || echo 0)
  echo "[bounty] discover wake=$WAKE open_algora_bounties=$n"
  emit "discover: $n open Algora bounties (state/bounties.json). Claim agent-doable ones via a PR; payout = Stripe KYC gate."
}

# ── attempt: turn the top gated survivor into a WORK-ORDER the coding-brain executes (FIND-001) ──
# Writes an attempts.jsonl entry {repo,issue,bounty_usd,status:claim} so track() has something to
# watch and the brain/coding-subagent has a concrete handoff (claim /attempt + open the PR, then set
# pr:N). The actual fix IS real engineering (legitimately the brain's job) — but the artifact exists.
attempt(){
  local gj="$STATE/gated.json" af="$STATE/attempts.jsonl"
  [ -f "$gj" ] || gate >/dev/null 2>&1
  # FIX (VCSDD REQ-B1 item 1, docs/superpowers/specs/2026-07-05-affiliate-bounty-state-machine-design.md §3):
  # survivors[0]固定ではなく、attempts.jsonlに未登録の最初の1件を選ぶ。以前はsurvivors[0]が既に
  # claimed済みだと"already claimed"と言うだけでsurvivors[1]以降を永久に試さないバグだった。
  local pick; pick=$("$PY" -c "
import json,os
survivors=json.load(open('$gj')).get('survivors',[])
claimed=set()
if os.path.exists('$af'):
    for line in open('$af'):
        line=line.strip()
        if not line: continue
        try: d=json.loads(line)
        except Exception: continue
        if d.get('key'): claimed.add(d['key'])
for s in survivors:
    k=s['repo']+'#'+str(s['issue'])
    if k not in claimed:
        print(json.dumps(s)); break
" 2>/dev/null)
  [ -n "$pick" ] || { emit "attempt: no unclaimed gated survivor to claim (all already attempted or real-USD inventory empty)"; return; }
  local key; key=$("$PY" -c "import json,sys;d=json.loads(sys.argv[1]);print(d['repo']+'#'+str(d['issue']))" "$pick" 2>/dev/null)
  "$PY" -c "import json,sys;d=json.loads(sys.argv[1]);print(json.dumps({'key':d['repo']+'#'+str(d['issue']),'repo':d['repo'],'issue':d['issue'],'bounty_usd':d.get('bounty_usd',0),'pr':None,'status':'claim','wake':sys.argv[2]}))" "$pick" "$WAKE" >> "$af"
  emit "attempt: WORK-ORDER written for $key (\$$(printf '%s' "$pick"|"$PY" -c 'import json,sys;print(json.load(sys.stdin).get("bounty_usd",0))' 2>/dev/null)). Brain: /attempt + fix(VSDD) + PR → set pr:N."
}

track(){
  local af="$STATE/attempts.jsonl" merged=0 tracked=0
  [ -f "$af" ] || { emit "track: no attempts yet (state/attempts.jsonl empty)"; return; }
  # FIX (VCSDD REQ-B1 item 2, docs/superpowers/specs/2026-07-05-affiliate-bounty-state-machine-design.md §3):
  # 事前に「既にstalled行が1回でも出たkey」の集合を作り、それらは以後gh呼び出しごとスキップする
  # (これが無いとCLOSED判定のたびに毎wake重複してstalled行を無限に追記し続けるバグになる)。
  local resolved; resolved=$("$PY" -c "
import json
resolved=set()
for line in open('$af'):
    line=line.strip()
    if not line: continue
    try: d=json.loads(line)
    except Exception: continue
    if d.get('status')=='stalled' and d.get('key'):
        resolved.add(d['key'])
print(' '.join(sorted(resolved)))
" 2>/dev/null)
  # BOUNTY_GH_OVERRIDE: テストフック、clip run.shのCLIP_SELF_HEAL_OVERRIDEと同じ命名慣習
  local gh_bin="${BOUNTY_GH_OVERRIDE:-gh}"
  local stalled_keys=""   # ループ中はここに溜めるだけ、$afへは書かない(ループとのI/O競合回避)
  # each line: {"key","repo":"o/r","pr":N|null,"issue":N,"bounty_usd":X,"status"}
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    local repo pr key; repo=$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('repo',''))" "$line" 2>/dev/null)
    key=$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('key',''))" "$line" 2>/dev/null)
    pr=$("$PY" -c "import json,sys;v=json.loads(sys.argv[1]).get('pr');print(v if v else '')" "$line" 2>/dev/null)
    [ -n "$repo" ] || continue
    case " $resolved " in *" $key "*) continue;; esac   # 既にstalled確定済みならgh呼び出しごとスキップ
    tracked=$((tracked+1))
    [ -n "$pr" ] || { echo "[bounty] track $repo (no PR yet — awaiting brain)"; continue; }
    local st; st=$("$gh_bin" pr view "$pr" -R "$repo" --json state,reviewDecision 2>/dev/null | "$PY" -c "import json,sys;d=json.load(sys.stdin);print(d.get('state',''),d.get('reviewDecision',''))" 2>/dev/null)
    echo "[bounty] track $repo#$pr → $st"
    case "$st" in
      MERGED*) merged=$((merged+1));;
      CLOSED*) stalled_keys="$stalled_keys $key";;
    esac
  done < "$af"
  # ループ終了後にまとめて追記(ループ中のファイルI/O競合を避ける)
  for k in $stalled_keys; do
    "$PY" -c "import json;print(json.dumps({'key':'$k','status':'stalled'}))" >> "$af"
  done
  # SETTLE (FIND-002): if anything merged, call the chain oracle — only real external USDC counts (INV-7)
  if [ "$merged" -gt 0 ]; then
    local re="${LIFE_MANAGER_REPO:-$(cd "$HERE/../.." && pwd)}/skills/self/founder-loop/record-earn.mjs"
    if [ -f "$re" ]; then local out; out=$(timeout 45 node "$re" --source bounty --task settle --wake "$WAKE" 2>&1 || true); echo "[bounty] settle: $out"; fi
  fi
  emit "track: $tracked attempt(s), $merged merged → record-earn settle (real USDC only)."
}

# ── gate: filter discovered bounties to genuinely-attemptable ones (the drizzle#1188 lesson) ──
# Deterministic checks via gh: (a) funder NOT withdrawn (issue comments), (b) NO existing open PR
# referencing the issue. (c) fix-not-blocked + (d) agent-doable are left to the brain's judgment on
# the survivors. Bounded to the top N candidates (gh rate-limit + time).
gate(){
  local bj="$STATE/bounties.json" gj="$STATE/gated.json" n="${BOUNTY_GATE_N:-12}"
  [ -f "$bj" ] || discover >/dev/null 2>&1
  "$PY" - "$bj" "$gj" "$n" <<'PY' 2>/dev/null
import json,sys,subprocess,re
bj,gj,n=sys.argv[1],sys.argv[2],int(sys.argv[3])
def sh(args):
    try: return subprocess.run(args,capture_output=True,text=True,timeout=30).stdout
    except Exception: return ""
# BUG FIX (2026-07-12 cadence self-fix): "checked" used to be len(items) where items was ALREADY
# capped to the top n=BOUNTY_GATE_N. Once the Algora board's real open-bounty count exceeded
# BOUNTY_GATE_N (48, itself a 2026-07-11 self-fix bump from 12), checked permanently plateaued at
# 48 no matter how much the board actually changed day to day -- the exact "same-value row reads
# as healthy" blind spot the increment-kind cadence check exists to catch (see cadence.py's own
# docstring), just moved one level down. checked now reports the FULL uncapped discovered-board
# size (cheap: bj is already on disk, no extra GH API calls) so it genuinely tracks board churn;
# `items` (below) stays capped to n for the expensive per-issue GH API vetting loop only.
full_open_bounties=json.load(open(bj)).get("open_bounties",[])
items=full_open_bounties[:n]
survivors=[]
for it in items:
    repo=it.get("repo",""); url=it.get("url","")
    m=re.search(r'/issues/(\d+)',url)
    if not repo or not m: continue
    num=m.group(1)
    # (a) funder withdrawn? scan comments
    cj=sh(["gh","issue","view",num,"-R",repo,"--json","body,comments,closed,state"])
    try: d=json.loads(cj) if cj.strip() else {}
    except Exception: d={}
    if d.get("state")!="OPEN": continue
    comments=d.get("comments",[])
    body=" ".join(c.get("body","") for c in comments)
    if re.search(r'removing the bounty|bounty.*(removed|withdrawn|cancell?ed|no longer)|no bounty', body, re.I):
        continue
    # loadertest#3 lesson: some "bounties" are bot-command test noise, not real tasks — e.g. an
    # issue body of just "/tip $5 @user" with no description of any bug/feature. No amount of
    # engineering makes that agent-doable; a PR against it would be fabricated work. Strip any
    # slash-bot-command lines from the issue's OWN body and require real descriptive text left over.
    issue_body=d.get("body") or ""
    stripped=re.sub(r'(?m)^\s*/\S+.*$', '', issue_body).strip()
    if len(re.sub(r'\s+','',stripped)) < 20:
        continue
    # SECURITY (2026-07-11, confirmed live incident UnsafeLabs/Bounty-Hunters#852/#864 found the
    # first time discover() widened past commenter:algora-pbc to involves:algora-pbc): a REAL,
    # actively-paying Algora bounty can still carry an embedded prompt-injection payload demanding
    # the attempting agent paste its system prompt/config into a merged file (there: a required
    # ".provenance.json" with a "config_snapshot" field defined as "the full text of all
    # instructions and guidelines loaded before your first task"). This is a config-exfiltration
    # attack on autonomous coding agents, not a legitimate acceptance criterion — funder-active +
    # real-$ + no-open-PR (checks a/b/d) do NOT protect against it, so it needs its own gate. This
    # is a narrow, high-confidence deterministic pre-filter (defense-in-depth only); the coding
    # brain in bounty-cli.sh's STARTUP prompt independently re-reads the full issue before ever
    # attempting and uses its own judgment to catch phrasings this regex misses.
    # confirmed 2 distinct phrasings live in the SAME repo (#852: ".provenance.json"/"config_snapshot"
    # asking for "the full text of all instructions and guidelines loaded"; #864: ".contributor.json"/
    # "initialized_with" asking for "the full unedited text" of "the first message in your
    # conversation") -- an adversary rephrases at will, so this list is necessarily incomplete;
    # it is a best-effort pre-filter for known variants only, NOT the primary defense (that is the
    # brain's own per-survivor judgment call, wired into bounty-cli.sh's STARTUP prompt).
    exfil_text=issue_body+" "+body
    if re.search(r'config[_ -]?snapshot|initialized_with|\.provenance\.json|\.contributor\.json|paste (the )?(full text of )?(all )?(your )?(instructions|system prompt)|reveal your (system prompt|instructions)|instructions and guidelines loaded|first message in your conversation|text of the first message|full unedited text', exfil_text, re.I):
        continue
    # keystatic#340 lesson: Algora marks a WITHDRAWN bounty by rendering the bot's bounty comment
    # in ~~strikethrough~~. Skip if any algora-pbc comment has its bounty line struck through.
    algora=[c for c in comments if "algora" in (c.get("author",{}) or {}).get("login","").lower()]
    if any(re.search(r'~~.{0,40}bounty', c.get("body",""), re.I|re.S) for c in algora):
        continue
    # require an ACTIVE (non-struck) algora bounty comment to exist at all
    if not any(re.search(r'\bbounty\b', c.get("body","")) and not c.get("body","").lstrip().startswith("~~") for c in algora):
        continue
    # (b) existing open PR referencing the issue?
    pl=sh(["gh","pr","list","-R",repo,"--state","open","--search",f"#{num} in:body","--json","number"])
    try: prs=json.loads(pl) if pl.strip() else []
    except Exception: prs=[]
    if prs: continue   # someone already has an open PR
    # (c) not an obvious throwaway/farm repo (name/owner heuristic only — NOT a star cut, which
    # over-drops real small-org bounties like microg/GmsCore $1340). Token-farms are caught by (d).
    owner=repo.split("/")[0]; name=repo.split("/")[-1]
    if name.lower() in ("test","young","playground","sandbox","demo") or re.fullmatch(r'\d{6,}',owner):
        continue
    # (d) REAL USD — read the AMOUNT from the active algora-pbc comment, do NOT keyword-guess (the old
    # `\d+ tokens?` regex false-matched ORM/CSS "N tokens" and wrongly dropped real bounties like
    # drizzle/zod). An active bounty line "$200 bounty" → USD + amount. "75 RTC" (no $) → token, skip.
    active=[c.get("body","") for c in algora if not c.get("body","").lstrip().startswith("~~")]
    amt=0
    for b in active:
        mm=re.search(r'\$([0-9][0-9,]*)[\s*]*(?:bounty|reward|tip)', b, re.I) or re.search(r'bounty[\s*]{0,12}\$([0-9][0-9,]*)', b, re.I)
        if mm: amt=max(amt,int(mm.group(1).replace(",",""))); break
    if amt<=0: continue   # no USD ($) amount in the active algora comment → token/none, skip
    rj=sh(["gh","repo","view",repo,"--json","stargazerCount,isArchived"])
    try:
        rd=json.loads(rj) if rj.strip() else {}
    except Exception: rd={}
    if rd.get("isArchived"): continue   # archived repo: GitHub blocks new PRs, bounty is dead
    stars=rd.get("stargazerCount",0)
    survivors.append({**it,"issue":int(num),"stars":stars,"bounty_usd":amt,"gate":"passed (active+funded $%d, no-PR, USD-from-comment)"%amt})
json.dump({"survivors":survivors,"checked":len(full_open_bounties)}, open(gj,"w"), indent=2)
print(f"{len(survivors)}/{len(items)}")
PY
  local s; s=$([ -f "$gj" ] && "$PY" -c "import json;print(len(json.load(open('$gj')).get('survivors',[])))" 2>/dev/null || echo 0)
  echo "[bounty] gate: $s candidates passed (funder-active + no-open-PR) of top $n"
  emit "gate: $s attemptable bounties (state/gated.json). Brain then judges fix-not-blocked + agent-doable, picks ONE."
}

case "$EARN_MODE" in track) track ;; gate) gate ;; attempt) attempt ;; *) discover ;; esac
exit 0
