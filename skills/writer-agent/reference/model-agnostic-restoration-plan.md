# Writer Agent restoration implementation plan

This plan implements
[model-agnostic-restoration.md](model-agnostic-restoration.md). The specification
defines required behavior; this file defines the ordered file operations,
runtime cutover, evidence, and rollback boundaries.

No step asks the user to publish, run a command, or inspect a browser. The agent
performs the repository work and the Mac mini cutover.

## Target folder tree

```text
skills/writer-agent/
├── SKILL.md                         # canonical Writer Agent skill
├── article-daily.sh                 # only creator of a JST daily run
├── article-healthcheck.sh           # read-only health and stale-run diagnosis
├── config/
│   └── forms.json
├── runtime/
│   ├── model-runner.sh              # auto|codex|claude boundary
│   └── README.md                    # three mode and exit-code contract
├── reference/
│   ├── model-agnostic-restoration.md
│   ├── model-agnostic-restoration-plan.md
│   ├── title-best-practices.md
│   └── zenn-deferred-operations.md
├── scripts/
│   ├── run.sh
│   ├── platform-dispatch.sh
│   ├── publication-guard.py
│   ├── publication-remote.py
│   ├── publication-resume.py
│   ├── article-run-complete.py
│   ├── article-daily-start-control.py
│   ├── article-resume-pending.sh
│   ├── article-selfimprove-verify.sh
│   ├── self-improve.sh
│   ├── audit-7day.sh
│   ├── learn-whitelist.sh
│   ├── gates/                       # deterministic, quality, safety, reality
│   ├── note-publish/
│   ├── devto-publish/
│   ├── substack-publish/
│   ├── x-publish/
│   └── zenn-publish/
├── topics/
│   ├── queue/                       # immutable source seeds
│   ├── in-progress/                 # immutable source seeds already in flight at cutover
│   └── done/                        # immutable source seeds already completed at cutover
├── vendor/
└── state/                           # existing gitignored Mac mini runtime
    ├── deployed-commit
    ├── topics/{queue,in-progress,done}/  # operational claims; never dirty Git
    ├── provider-health.json
    ├── runs/<run-id>/
    ├── articles.jsonl
    ├── experiments.json
    ├── playbook.json
    ├── playbook-applications.jsonl
    ├── funnel.jsonl
    └── sales-ledger.jsonl

Loop lifecycle:

config/loop-registry.json            # sole schedule/lifecycle source
bin/lm-loop                          # status/watch/apply interface
~/.local/state/life-manager/writer/  # machine-local state and logs
```

The restored tree keeps the proven workflow shape. `runtime/model-runner.sh` and
the pending resume worker are small seams; they do not become a new workflow
engine.

## Phase 0: Freeze truth before changing production

### Read and preserve

Capture through `bin/lm-loop status <loop-id>` and repository-owned state:

- registry status for all `article-*` and `writer-*` loop IDs
- the installed immutable Life Manager release commit or file hashes
- current article database and publication rows
- current run directory and immutable draft hashes
- all known public IDs and live URLs
- current topic queue movements
- relevant daily/resume/self-improve logs
- Codex and Claude authentication status
- classified provider failures already present in the logs

Create a timestamped backup outside the repository containing the database,
run state, launchd plists, and a manifest of hashes. Do not copy credentials,
cookies, or browser profiles into git.

### Gate

Do not proceed unless the incomplete run can be bound to:

- one run ID
- one topic
- one Japanese artifact or its authoritative source
- one English artifact or its authoritative source
- all six currently live remote identities
- the two pending pairs

### Evidence

The evidence manifest records source path, size, SHA-256, and capture command.
It does not claim a public output exists without remote readback.

## Phase 1: Remove competing production writers

### Jobs to unload

Disable them through a reviewed registry release without deleting logs or state:

- `ai.anicca.writer-daily`
- `ai.anicca.writer-resume`
- `ai.anicca.writer-learn-daily`
- `ai.anicca.writer-learn-weekly`
- `ai.anicca.writer-book-monthly`

Also confirm that no old `ai.anicca.article-daily` process is still running.

### Gate

`bin/lm-loop status <loop-id>` MUST show no active loop capable of creating article
publication side effects. This maintenance interval intentionally has no daily
creator until the canary is ready.

### Rollback

Rollback during restoration means keep all article schedulers disabled and
preserve state. It does not mean re-enable the broken replacement generator.

## Phase 2: Restore the proven foreground workflow

### Source

Use `592a193` as the behavioral foundation. Restore only tracked source and
assets under `skills/writer-agent/`; do not restore runtime state, old logs,
credentials, browser profiles, or obsolete generated output.

### Core paths

Restore:

- `skills/writer-agent/SKILL.md`
- `skills/writer-agent/article-daily.sh`
- `skills/writer-agent/article-healthcheck.sh`
- `skills/writer-agent/config/`
- `skills/writer-agent/data/`
- `skills/writer-agent/scripts/`
- `skills/writer-agent/topics/`
- `skills/writer-agent/vendor/`

Keep the current restoration specification and implementation plan.

### Verification

- the canonical skill contains the headline-image, Mermaid, table, note
  eyecatch, native JA/EN, own-eyes, reality-gate, and self-improvement steps
- the daily driver invokes the complete foreground agent
- the restored driver does not invoke Blockrun
- no source path references the deleted Writer Engine generator

## Phase 3: Reapply effective pre-replacement fixes

Apply behavior, not blind commit-level state, from:

| Commit | Behavior to preserve |
|---|---|
| `a8fa2b5` | streamed foreground progress |
| `4bbcd7c` | complete preflight before public side effects |
| `f5567da` | no blind full-prompt replay |
| `5f31ce3` | stable remote identities and crash-safe resume |
| `41b32f4` | complete intent set before first side effect |
| `30942bb` | same run/topic/hash binding |
| `93346b1` | one run per JST date |
| `33b4064` | guarded note 404 recovery |

Review each affected file against both its known-good version and current donor
behavior. Do not cherry-pick an architecture-wide replacement commit.

### Required state contract

`publication-state.json` MUST contain:

```text
run_id
date_jst
topic_id
ja_artifact_path + sha256
en_artifact_path + sha256
safety_status
pair intents
stable remote target per pair
attempt count
last observed remote state
```

Resume MUST reject missing, conflicting, or cross-run values.

## Phase 4: Add the Codex/Claude process seam

### New interface

Create:

```text
skills/writer-agent/runtime/model-runner.sh
```

Supported calls:

```text
model-runner.sh agent  --prompt-file <path>
model-runner.sh judge  --prompt-file <path>
model-runner.sh vision --prompt-file <path> --image <path>
```

Inputs:

```text
ARTICLE_PROVIDER=auto|codex|claude
ARTICLE_RUN_ID=<stable run>
ARTICLE_MODEL_LOG=<run-scoped log>
```

Exit behavior:

- success: return model output and exit zero
- retryable auth/quota/rate/service failure: record provider/mode/error class
  and return temporary failure
- invalid configuration or missing capability: fail closed
- never rewrite prompts, choose topics, mutate publication state, or publish

### Codex adapter

The full agent uses:

```text
printf '%s' "$PROMPT" |
  codex -a never -s danger-full-access \
    -C "$LIFE_MANAGER_REPO" --add-dir "$HOME" \
    exec --ephemeral --model gpt-5.6-luna \
    -c 'model_reasoning_effort="xhigh"' -
```

Judge mode uses the same model and reasoning setting without production tools.
Vision mode passes the existing screenshot through Codex image input.

### Claude adapter

The full agent preserves:

```text
claude --model sonnet --dangerously-skip-permissions \
  --add-dir "$HOME" -p "$PROMPT"
```

Judge mode remains a fresh non-tool call. Vision mode receives the existing
screenshot. Claude does not inherit a fabricated Codex effort value.

### Availability selection

1. Validate CLI path and login state.
2. Treat the first required bounded call as the quota/capability probe; do not
   spend quota on an unrelated repeated probe.
3. In `auto`, try healthy Codex first.
4. If Codex returns a classified pre-side-effect retryable failure, try Claude.
5. Record a cooldown per provider and mode.
6. If both fail, keep the run pending and create no public side effect.

### Direct call replacement

Replace 18 logical Claude calls across these 16 files:

1. `article-daily.sh`
2. `scripts/article-self-fix.sh`
3. `scripts/conscience-gate.sh`
4. `scripts/deslop-gate.sh`
5. `scripts/devto-publish/run-devto-agent.sh`
6. `scripts/eval-gate.sh`
7. `scripts/identity-gate.sh`
8. `scripts/note-publish/run-note-agent.sh`
9. `scripts/reader-testing-gate.sh` — two independent calls
10. `scripts/render-verify-draft.sh`
11. `scripts/rubric-judge.sh`
12. `scripts/self-improve.sh` — two independent calls
13. `scripts/substack-publish/run-substack-agent.sh`
14. `scripts/x-publish/run-x-agent.sh`
15. `scripts/zenn-publish/run-zenn-agent.sh`
16. `topics/make-diary-digest.sh`

Comments, logs, and filenames MUST use provider-neutral terms when they describe
the boundary. The canonical skill name remains Writer Agent.

### Gate

A repository search MUST find no operational direct `claude -p` or direct
`codex exec` call outside `runtime/model-runner.sh`.

## Phase 5: Restore exact8 and durable scheduling

### Daily creation

`config/loop-registry.json` entry `article-daily`:

```text
StartCalendarInterval = 06:00
RunAtLoad = true
ARTICLE_AUTOPUBLISH = 1
ARTICLE_PROVIDER = codex during cutover
```

Only this job may create `daily-YYYY-MM-DD`.

### Immediate group

After the immutable package and all pre-publication gates exist, publish
independently in this order:

1. note Japanese
2. Zenn Japanese
3. Dev.to English
4. Substack Japanese
5. Substack English
6. X Article Japanese

The order makes logs deterministic. A failure continues to the next pair.

### X Article spacing

Port the useful schedule calculation but remove the deadline:

```text
not_before = x_article_ja.published_at + 6 hours
eligible = now >= not_before
deadline = none
```

The worker MUST use the remote readback timestamp for Japanese publication, not
the local attempt timestamp.

### Pending worker

Register `article-resume` with a 300-second interval in
`config/loop-registry.json`. Its script:

1. acquires the same global article publication lock
2. selects the oldest incomplete run
3. validates run/topic/artifact/intent hashes
4. reconciles uncertain remote outcomes
5. publishes only currently eligible pending pairs
6. skips X Article English until `not_before`
7. never creates a topic, draft, run, or replacement remote target

### X Post

The same daily package owns one Japanese X Post. At or after 12:00 JST, the
pending worker assigns at most one JST-date slot. It records the X status ID
before marking the pair live. Old pending runs use FIFO ownership without
creating two posts on one date.

### Zenn

Keep the dedicated Zenn deferred worker for the platform rolling window. It
owns only the persisted Zenn slug and MUST not respawn the article agent.

## Phase 6: Backport destination and media improvements

### Donor boundary

Use these current Writer Engine areas only as implementation donors:

```text
skills/writer-engine/publishers/
skills/writer-engine/gates/reality/
skills/writer-engine/notifications/
```

Do not import:

```text
skills/writer-engine/core/generation.py
skills/writer-engine/core/orchestrate.py
skills/writer-engine/runtime/
```

### Per destination

#### note

- keep authenticated draft identity
- preserve note key before publish
- set eyecatch from the already-selected headline image
- require `assets.st-note.com` eyecatch readback
- require visible body diagram
- recover a confirmed public 404 without duplicating a live article

#### Zenn

- retain canonical frontmatter and repository ownership
- publish the stable slug
- require deployment/readback
- hand rolling-window delay to the dedicated worker

#### Dev.to

- make English live, not draft-only
- preserve authenticated article ID
- require canonical URL and body/media readback

#### Substack

- use the supported draft endpoint/schema
- retain separate JA and EN draft IDs
- publish without subscriber email unless the canonical publisher says
  otherwise
- require content and media readback

#### X Article

- keep separate JA and EN stable draft identities
- preserve browser identity, publish-button, confirmation, and DOM fixes
- recover uncertain publication by account/timeline readback before create
- enforce the six-hour minimum for English

#### X Post

- create independent short-form copy from the same research
- publish once per JST date
- verify account identity, status ID, timeline visibility, and emoji integrity

### Media

Do not create another headline-image generator. Restore the generator and
selection already embedded in the proven foreground workflow.

For each run:

1. generate/select the headline image once
2. persist its path and hash
3. reuse it as note eyecatch and platform cover where supported
4. author at least one Mermaid-backed diagram
5. render/adapt the diagram per platform
6. verify every visible public asset

## Phase 7: Restore self-improvement

### Daily 22:30 driver

Restore:

```text
scripts/self-improve.sh
scripts/measure-sales.py
scripts/_shared/measure-funnel.py
scripts/article-selfimprove-verify.sh
config/loop-registry.json entry `article-self-improve`
```

Replace both direct Claude judgments in `self-improve.sh` with
`model-runner.sh judge`.

### Measure

For every completed exact8 run, persist:

- run and topic IDs
- exact8 URLs
- selected writing experiment
- JA/EN quality axes
- views/likes with collection timestamp and missing reason
- SEO position/delta
- note/Substack sales and revenue when measurable
- source artifact hashes

Metrics collection MUST be deterministic. A model may interpret evidence but
MUST NOT fabricate or overwrite measurements.

### Learn and apply

At 22:30:

1. reconcile the previous experiment if its evaluation date has arrived
2. calculate disjoint pre/post evidence
3. keep or revert the exact recorded change
4. find the weakest evidence-backed axis
5. ask a fresh judge for exactly one bounded change
6. write the experiment record before applying the change
7. apply it additively
8. commit and push the exact applied or reverted source diff

At the next 06:00 run:

1. run `article-selfimprove-verify.sh`
2. read the current verified playbook
3. apply the experiment to the eligible JA/EN article
4. append application evidence tied to run ID and experiment ID

### Weekly controls

- Sunday 03:00: restore and run whitelist learning; syntax-check before replace
- Sunday 22:00: restore and run seven-day public URL, language, SEO, media, and
  experiment audit

### Failure behavior

If both model providers are unavailable, preserve the current playbook and
measurements and retry at the next learning opportunity. Publishing uses the
last known-good rules and remains independent.

## Phase 8: Install without activating the daily creator

### Install

1. push the complete source commit
2. fast-forward the canonical Life Manager repository at
   `$LIFE_MANAGER_REPO` to that exact commit
3. write its hash to
   `$WRITER_STATE_DIR/deployed-commit`
   and atomically refresh that marker after every successful learning commit/push
4. resolve Writer source directly from `$LIFE_MANAGER_REPO/skills/writer-agent`
5. install all article launchd plists
6. load only health, pending recovery, Zenn recovery, and learning jobs needed
   for the canary
7. keep the 06:00 daily creator unloaded

### Static verification

- `bash -n` for changed shell entry points
- Python compile for changed Python entry points
- `plutil -lint` for every article plist
- `git diff --check`
- no Blockrun reference in operational article paths
- no direct provider invocation outside the shim
- symlink and deployed commit resolve correctly

These checks reuse existing tooling. They do not create a new test framework.

## Phase 9: Repair the current run and execute one armed canary

### Repair in place

Load the frozen current run. Treat the six proven live identities as immutable.
Import them as `repair-required`, never as completed exact8 receipts: a copied
legacy LIVE flag is not proof that the immutable headline/body media is public.

Repair:

- headline and required body diagram on every existing live article destination,
  updating each protected remote ID in place
- note eyecatch and required body diagram on the same note article
- missing Zenn Japanese using its stable intended slug
- missing X Article English using the saved English artifact and the existing
  Japanese publication timestamp

Never recreate or replace note, Dev.to, Substack JA/EN, X Article JA, or X
Post. An in-place media update/republish operation is required where the
destination needs one to make the same protected ID expose the new media; only
a fresh identity/content/media readback may convert `repair-required` to live.

### Canary

Pin:

```text
ARTICLE_PROVIDER=codex
ARTICLE_AUTOPUBLISH=1
```

Kickstart one complete foreground run through the same daily driver that
launchd will use. Do not call publishers manually outside the loop except for
the same-run repair path.

### Canary evidence

Collect:

- exact eight pair rows
- eight stable public IDs and URLs
- JA/EN artifact hashes
- headline and body asset hashes
- public screenshots/readback
- X JA and EN timestamps proving at least six hours
- exactly one X Post for the JST date
- provider and model/effort record
- Telegram message ID
- no duplicate IDs or URLs

Any failure leaves the daily creator disabled and the run pending.

## Phase 10: Activate and prove unattended operation

### Activate

After the canary passes:

1. bootstrap `ai.anicca.article-daily`
2. confirm its 06:00 calendar trigger
3. confirm the pending worker interval is 300 seconds
4. confirm the self-improve and weekly schedules
5. verify no Writer Engine production labels are loaded

### First unattended day

Do not manually kickstart the next run. Observe:

- launchd creates one JST run at 06:00
- the provider runner selects the expected provider
- exact8 completes
- X Article English waits at least six hours
- X Post appears once
- 22:30 learning records one bounded evidence-backed result

### Enable automatic provider selection

Keep Codex pinned until Claude passes real `agent`, `judge`, and `vision`
capability checks. Then set:

```text
ARTICLE_PROVIDER=auto
```

Perform a same-run controlled provider switch before the first live side
effect. After public side effects, verify only the pending-pair path uses the
alternate provider.

## Completion definition

The restoration is complete only when:

- the real canary passes every specification acceptance criterion
- one subsequent 06:00 run starts without human or interactive-agent action
- exact8 and media readback pass
- X language spacing and one-X-Post rules pass
- the 22:30 self-improvement receipt exists
- installed commit, skill alias, and launchd paths match
- the final source and deployment commit is pushed

Anything less remains incomplete.
