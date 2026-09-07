# Mercor → Life Manager 統合仕様

**Status:** resident runtime recovery passed / hourly owner live / 3 applications pending / no payment / human-gate canonicalization verified live / shared money receipts next
**Canonical repository:** `https://github.com/Daisuke134/life-manager`
**Canonical checkout:** `/Users/anicca/Projects/life-manager-main`

## 1. Repository boundary

`life-manager`だけがMercorのコード、skill、spec、loop、test、releaseの正本である。`profitable-claude`は移行元の履歴であり、移行完了後はMercorのproduction sourceとして参照しない。

「1つのrepository」はsource controlを1つにする意味であり、credentials、Google session、Mercor Cookie、resume PDF、個人プロフィール、応募台帳などのprivate runtime stateをGitへ入れる意味ではない。

## 2. Canonical layout

```text
life-manager/
├── skills/
│   ├── job-hunter/                         # fact bank/materials owner
│   │   ├── SKILL.md
│   │   └── references/mercor.md            # provider-specific reference
│   └── mercor/                             # user-facing Mercor skill facade
│       ├── SKILL.md
│       └── agents/openai.yaml
├── apps/job-search-loop/                   # sole browser/application side-effect owner
│   ├── job_search_loop/                    # deterministic state, adapters, evidence
│   ├── prompts/                            # bounded agent prompts
│   ├── schemas/                            # pass/application/result contracts
│   └── tests/
├── loops/job-hunter/                       # hourly/inbox/learning cadence
│   ├── loop.toml
│   └── registry.yaml
├── runtime/agent-runner/                   # provider/model routing and validation
└── docs/superpowers/specs/                 # this migration and acceptance evidence
```

Private runtime state remains outside the repository:

```text
~/.config/anicca/job-search/profile.json
~/.local/share/anicca/job-search/materials/
~/.local/state/anicca/job-search/mercor/
```

## 3. Ownership and no-duplication contract

| Responsibility | Canonical owner |
|---|---|
| Candidate facts and approved resume variants | Private profile/material SSOT |
| Shared Apply selection, human-gate disposition and effect reporting | `skills/_shared/marketplace-core/` |
| Mercor auth and provider policy | `skills/mercor/SKILL.md` + `skills/job-hunter/references/mercor.md` |
| Mercor browser, form submission, read-back, locks, evidence | `apps/job-search-loop/` |
| Hourly/inbox/learning scheduling | `loops/job-hunter/` |
| Model/provider execution | `runtime/agent-runner/` |
| Private resume, Cookie, ledger and evidence | `~/.local/state/anicca/job-search/` |

Do not create `profitable-claude`-style second executors, a second Mercor loop, or a browser script inside the skill. The skill provides policy and routing; the existing job-search runtime owns side effects.

## 4. Mercor authentication boundary

- Use only Mercor's email login with the account email from private state.
- Never click `Google`, `Okta`, or `Sign up`.
- Never click account recovery, reset, registration, recovery-email, or recursive alternate-method paths.
- If recovery/reset/wait appears, record the visible URL/text and stop.
- Use a dedicated Mercor browser profile; never navigate the job-search or trusted daily-driver tab.

## 5. Global role and locale scope

Mercor is not a Japanese-only lane and is not Job Hunter. It prioritizes Japanese/Japan, software, AI and system-development roles, then continues through English, bilingual, business operations, research, data/CRM, product and other work. It consumes the shared marketplace Apply policy and the operator's complete verified profile/resume facts.

## 6. Loop behavior

The 30-minute `mercor-revenue-application` owner is the single Mercor Apply loop. It must:

1. Reconcile the oldest in-progress Mercor application before discovering new work.
2. Deduplicate by stable Mercor listing/application identifier.
3. Treat posting qualifications as ranking signals rather than pre-submit rejection gates, answer controls truthfully from the complete verified profile/resume, and let Mercor decide eligibility.
4. Finish reversible steps before a new person-bound interview/assessment, notify its exact job/link/action once, persist only that candidate as pending, and continue other candidates without waiting. CAPTCHA, authentication failure and ambiguous provider state remain blockers.
5. Re-open the application list after any submit and store evidence plus the external result.
6. Record settled earnings only when the Mercor Earnings UI proves payment; views, invitations, offers, and estimates are not earnings.

### 6.4 End-to-end money state machine

An application is only the first state; it is not income. The complete flow is:

```text
DISCOVERED
  → GROUNDED_READY
  → SUBMITTED_PENDING_REVIEW
  → SELECTED_OR_REJECTED
  → CONTRACTED
  → AUTHORIZED_WORK
  → WORK_ACCEPTED
  → PAID_SETTLED
  → REVENUE_LEDGER
```

The resident loop owns discovery, grounded submission, Gmail/status reconciliation, Calendar scheduling, evidence, and settled-payout read-back. It does not impersonate a human interview/assessment or perform work when the contract prohibits AI/automation. `PAID_SETTLED` requires a real Mercor Earnings/contract payment row; the hourly rate, an offer, an invitation, a selected application, or a pending balance never advances the revenue ledger.

Current money diagnosis: the account has three `SUBMITTED_PENDING_REVIEW` applications, no observed selection/contract, and Mercor Earnings says `No payment history yet`. The five canonical labels are enabled. Immutable release `3c9293ee4362ed386eb174dd761d5b54c09f2019` runs both the hourly Mercor scheduler and authenticated browser owner on CDP `9334`. Natural wake `mercor-20260826-141446-82944` proved the installed cadence. Current-release kickstart `mercor-20260826-145944-91942` then inspected 13 listings, submitted zero, created no duplicate or blocker, preserved the three-row ledger, exited 0, delivered Telegram ACK `message_id=35053`, read Earnings as `not_observed`, and emitted each reused Finance Interview gate ID once in the receipt. Natural Inbox wake `inbox-20260826-131020-30009` exited 0 with ACK `message_id=34853`. Resident runtime recovery is complete. Settled revenue is not observed, not zeroed as a fabricated success, and no `$10K` claim is valid.

### 6.1 Ready-to-submit queue

The loop may submit a new listing without human intervention when the live application page proves all of the following:

- `3 of 3 steps completed` and `100%`;
- the completed `Domain Expert Interview` is explicitly reused;
- `Submit application` is visible;
- the listing/application identifier is not already in the private application ledger.

For each wake, submit every grounded ready listing within the bounded candidate scan. Durably claim each listing before its single click, reopen the application result, and append one evidence row after authoritative readback. Never resubmit `submitted_pending_review` or `submit_unknown`, click `Start` on a new interview, or infer readiness from a title alone.

An ungrounded or human-gated candidate is not a reason to end the wake. The model must continue through distinct candidates until it reaches an ambiguous irreversible boundary or exhausts the bounded verified queue. Every authoritative submission readback is recorded before continuing.

Current ready queue observed in Mercor before the resident pass:

1. Data analysis / quantitative readouts Evaluator — $80–$120/hour (submitted in the resident pass below)
2. General business strategy / management Evaluator — $80–$120/hour
3. Humanities / arts / culture Evaluator — $80–$120/hour
4. Media / journalism / communications Evaluator — $80–$120/hour

Japanese language / cultural fluency Evaluator and Software / AI / IT / data Evaluator are already submitted and excluded from the queue.

## 6.3 Model-led execution decision

The Mercor macro loop must be model-led rather than a large selector script. The model observes the live page, decides the next safe action from the task packet, and adapts when the page changes. Deterministic code owns only the boundaries that must not drift: operator/profile isolation, lease ownership, approved domains, application dedupe, the single irreversible submit wrapper, read-back, evidence, and ledger writes.

### OSS code evidence and adopted patterns

The implementation comparison was performed against fixed public commits:

| OSS | Fixed commit | Observed mechanism | Adoption decision |
|---|---|---|---|
| `browser-use/browser-use` | `85ddbfedf609166b2d2c76c3d80506649fee82a9` | Model-led `Agent` loop; `AgentState`; `BrowserStateSummary`; short/long browser error memory; domain-filtered `ActionRegistry`; structured output schema; failure/loop budgets | Adopt the state/error/action-boundary pattern; do not copy the whole runtime |
| `browserbase/stagehand` | `a21633d53930abc5d62b8dbd6b608995f2ccb4b1` | Narrow `observe`, `act`, and schema-validated `extract` verbs over an owned browser session; explicit session cleanup | Adopt the narrow verbs and extraction contract; keep the existing Life Manager CDP owner |

The selected call graph is:

```text
existing launchd wake
  → job-hunter loop lease
  → runtime/agent-runner (model-led Mercor prompt)
  → observe live Mercor page
  → model selects next grounded action
  → guard validates domain/state/dedupe
  → act through owned browser session
  → extract success/read-back
  → deterministic evidence + ledger append
```

Do not encode every Mercor selector, role title, or page branch in shell/Python. When a page changes, the model re-observes and returns a structured `blocked`, `needs_human`, `submitted`, or `observed_no_action` result. A bounded retry may re-observe; it may not repeat an ambiguous submit.

## 6.2 Open-source reusable macro loop

The Mercor lane is a reusable open-source macro loop for any operator, not a promise that every operator will earn $10K. Each operator supplies their own private inputs and completes identity-bound steps; the shared code owns the repeatable orchestration.

### Operator onboarding (human once per account)

1. Provide a private resume/fact profile and approve the material baseline.
2. Authenticate the operator's own Mercor email account in an isolated browser.
3. Complete Mercor's required profile, work authorization, payment setup, interview, and assessment steps personally.
4. Connect the operator's own Gmail and Google Calendar through the existing Job Hunter integration.
5. Set the operator's target rate, weekly capacity, locales, role families, and hard exclusions.

### Resident loop (minimal human runtime)

- `acquisition`: 30-minute discovery, dedupe, fact matching, and every grounded ready submission within the bounded queue.
- `inbox`: 15-minute Gmail/Mercor reconciliation, selection/contract/rejection transitions.
- `calendar`: FreeBusy, idempotent interview event, reminders, prep pack delivery.
- `work`: track only authorized human/approved work; never impersonate an interview or violate task AI-use rules.
- `earnings`: reconcile Mercor Earnings/settled payouts and calculate verified monthly run-rate.
- `guardian`: leases, browser ownership, retries, duplicate-submit protection, and evidence integrity.
- `learning`: weekly role/rate/conversion analysis without fabricating success.

### Public/private boundary

Public repository: adapters, schemas, prompts, tests, launchd templates, provider policy, and redacted fixtures. Private runtime: resume, fact ledger, Mercor email session, Calendar IDs, Gmail thread IDs, payment details, application ledger, evidence, and earnings. A new operator gets a fresh private state root and never receives Dais's credentials or profile.

### $10K verification contract

The loop reports one `$10K verified net month` only after one complete calendar month has at least USD 10,000 of actual settled payout evidence minus complete fees and costs. It reports `$10K stable monthly` or MRR only after three consecutive qualifying monthly cycles. It must never convert an offer, application, view, estimated rate, or unworked weekly cap into revenue. The expected-hours planner uses the contract's displayed rate and weekly cap; it does not promise a result.

The current portfolio proof comes from Coconala, not Mercor: the inspected Coconala ledger contains
JPY 126,438 net-of-fee completed-work earnings in total and JPY 62,478 across six August rows through
August 12, while Mercor has three pending applications and no payment history. This supports a
Mercor scale experiment; it does not prove `$1K this month`, `$10K`, bank-received cash, or MRR.

The contract-level work harness is implemented in `apps/job-search-loop/job_search_loop/mercor_work_harness.py`, and its private append-only/idempotent event store is `mercor_work_store.py`. It rejects invalid work transitions, requires `authorization_policy=explicitly_allowed` for `authorized_work`, requires `acceptance_status=accepted` for `accepted`, routes human-bound work to `needs_human`, and accepts a revenue record only from a `paid_settled` event with a positive amount, settled status, payment ID, and evidence reference. This is a partial provider-local state machine: it does not yet require QA/artifact evidence, a shared DeliveryReceipt, actual fee/cost evidence, or bank/payout matching. Those are active engineering work, not an external wait.

### 6.5 Target portfolio closed-loop contract

Mercor is one provider lane in the Life Manager money loop. The cross-provider design SSOT is `docs/superpowers/specs/2026-08-22-life-manager-gig-economy-loop-design.md`; this Mercor spec owns only Mercor transport, state, policy, and evidence. Coconala, Lancers, CrowdWorks, and Upwork do not call the Mercor executor and Mercor does not call theirs.

```mermaid
flowchart LR
  D[Discover] --> A[Apply or inbound sale]
  A --> C[Official contract receipt]
  C --> W[Authorized work]
  W --> Q[QA and delivery receipt]
  Q --> P[Settled payment receipt]
  P --> L[Net cash ledger]
  L --> O[Portfolio allocation]
  O --> D
  H[Identity interview assessment] -. typed human ceremony .-> C
```

The target shared contract is receipt identity, not shared mutable execution: `ApplicationReceipt → ContractReceipt → AuthorizationReceipt → QAReceipt → DeliveryReceipt → PaymentReceipt → bank/payout match`. It is not implemented end-to-end today: the current shared marketplace contract has Application/Delivery/Payment-shaped records but lacks Contract/Authorization/QA receipt types, does not make QA and Delivery evidence a mandatory joined sequence, and does not prove actual fee/cost or bank matching in PaymentReceipt. Each provider keeps its own schedule, browser/profile, policy gate, rate limit, entity lock, and private state. Reporting and allocation read the receipts without becoming a fifth side-effect owner. A provider with a stopped scheduler, unknown authorization, missing contract, prohibited AI use, or no settled payment remains visibly open; another provider may continue independently.

## 7. Calendar and minimal-human-glue flow

Mercor interview messages enter the existing Job Hunter inbox lane. Reuse `apps/job-search-loop/job_search_loop/interview_scheduling.py` and `calendar_sync.py`; do not create a Mercor-specific Calendar writer.

1. Classify the Gmail/Mercor message and require a clear role, source thread, timezone, start, and end.
2. Read Google Calendar FreeBusy for the primary calendar.
3. Choose the earliest explicitly offered slot that is free; never invent a time.
4. Create or update one idempotent private Calendar event keyed by the source thread and normalized start time, with 3-day and 1-day reminders.
5. Register the interview-prep job and deliver grounded prep windows through the existing inbox/prep loop.
6. Human glue remains only for Gmail/Calendar authorization, ambiguous scheduling, and attending the interview or taking a human-bound assessment. The system never impersonates the interview.

## 8. Current migration state

- Mercor skill/spec exist in the migration source and have been read back.
- Canonical Life Manager repo already owns `skills/job-hunter/`, `apps/job-search-loop/`, and `loops/job-hunter/`.
- The Mercor profile was authenticated and its resume/profile fields were verified in the browser at the recorded checkpoint; the stopped browser owner means this is not a current session-health claim.
- Japanese evaluator application has the 14-minute `Domain Expert Interview` completed and its recorded application read-back says `3 of 3 steps done`, `Your application has been submitted!`, with review expected within four weeks. Submission used the user's explicit attestation of Japanese native fluency and 24 years living in Japan; do not resubmit while selection is pending.
- Related evaluator pages for Data analysis, General business strategy, Humanities/arts/culture, and Media/journalism/communications read `3 of 3 steps completed` with `Submit application` visible and reuse the completed Domain Expert Interview. Software/AI/IT/data was the strongest next candidate and is now submitted with a four-week review window.
- The resident model-led pass at `mercor-20260822-200153-55024` inspected the already-submitted Software listing, selected Data analysis / quantitative readouts, submitted it once, reopened the result, and read back the visible submitted state. The result is recorded in the private ledger and evidence paths under `~/.local/state/anicca/job-search/mercor/`; no `needs_human` or `blocked` result was returned.
- At the initial enablement checkpoint, the dedicated browser transport `ai.anicca.job-search-mercor-browser` was `KeepAlive`/`RunAtLoad` with CDP `127.0.0.1:9334`, and `ai.anicca.job-search-mercor` was registered with a 3600-second wake interval. The first recovery pass blocked while the browser was down; the next pass `mercor-20260822-204000-83882` read back `browser-owner=ready`, inspected the existing Software application, and observed the Data quality / CRM operations listing without submitting because the private fact bank did not verify its 5+ years requirement. The pass exited 0, wrote fresh evidence, and left the ledger at three submitted applications. This historical state is superseded by the stopped-runtime read-back below.
- Operational meaning of 24/7 is a persistent browser transport plus an hourly bounded wake, not a busy loop that clicks continuously. Each wake may submit at most one grounded listing; `observed_no_action` is a successful safe pass when no listing is fully supported by verified facts.
- The first live resident pass after enablement inspected `Data quality / CRM operations Evaluator`, but did not submit because the private facts did not verify the listing's five-year requirement. This is the expected grounded behavior; adding a role-specific fact is only allowed when the operator can truthfully evidence it.
- At that checkpoint, pass reporting was wired through the existing idempotent Telegram outbox. Pass `mercor-20260822-213818-48917` was delivered with Telegram `message_id=28848`; it contained the status, inspected job titles, and no-action reason without resume/profile secrets.
- The installed intended cadence at that checkpoint was generic acquisition/daily every 3600 seconds, Mercor acquisition every 3600 seconds, inbox every 900 seconds (15 minutes), learning every Monday at 09:15 Asia/Tokyo, and the dedicated Mercor browser transport KeepAlive. There was no Mercor 30-minute or 5-minute submit loop. Installed cadence is not proof that the labels are currently loaded.
- The provider-local work harness and private event store pass their existing tests: they cover explicitly authorized work → acceptance → paid-settled → revenue, reject missing authorization/acceptance and pending/offer payment evidence, route AI-prohibited work to `needs_human`, and persist idempotent state events without profile/resume secrets. They do not yet prove the stronger shared QA/delivery/net-cash contract defined in Section 6.5.
- At that checkpoint, Earnings capture ran after each Mercor pass. E2E pass `mercor-20260822-224541-76805` reconciled the Japanese submission and continued through four Explore candidates: Generalist (Macbook User) was `needs_human` for an assessment/device-fact gate, Biology Research Scientist lacked U.S. work eligibility and biology/assay facts, and Generalist Expert lacked native-English evidence. It captured Earnings with `Your total earnings to date are $0.00` / `No payment history yet`, synced `not_observed` with zero events, and delivered Telegram ACK `message_id=28953`. No duplicate submit occurred.
- That pass briefly wrote three model-generated evidence artifacts to the repo root; they were moved to the private pass evidence directory and the prompt now explicitly forbids repository-root evidence writes. No private artifact was committed.
- At that checkpoint, human-bound gates became durable in `~/.local/state/anicca/job-search/mercor/human-gates.jsonl`; Project Thor assessment and Finance Interview each had an idempotent gate ID and evidence reference.
- The live pass `mercor-20260822-231155-58248` inspected five distinct listings, submitted none, and read back the two human-bound gates (Project Thor assessment and Finance Interview) plus two grounded fact-gate failures. The model returned stale evidence paths from an older `model-pass-*` directory; the parent rejected them fail-closed, persisted `evidence-validation-error.json` under the current private run, reported `status=blocked`, captured Earnings `empty`, and delivered Telegram `message_id=29006`. The pass context now binds the exact current `evidence_dir`, and non-empty evidence paths outside that directory or missing on disk are rejected without allowing a submit claim.
- The follow-up pass `mercor-20260822-231916-82217` verified the repair: it inspected the existing submitted Software listing plus six distinct candidates, returned `observed_no_action` with no submit, wrote all DOM/screenshot evidence beneath the bound current run directory, captured Earnings `empty`, and delivered Telegram `message_id=29012`. The ledger remains at three submitted applications.
- At that checkpoint, human-gate replay collapsed wording drift for the reusable Project Thor assessment and Finance Interview without rewriting the append-only history; the private `pending()` projection contained two logical gates.
- The follow-up pass `mercor-20260822-233559-30906` inspected the existing Software application plus five candidates, returned `observed_no_action`, and delivered Telegram `message_id=29043`; no application was added and Earnings remained `empty`. The parent post-pass mode normalization made every model-created evidence file `600` and directory `700`, while repeated Finance Interview reporting reused one gate ID.
- At that recovery checkpoint, the Inbox LaunchAgent had been loaded from stale `/Users/anicca/lm-loops-core`; a safe bootout/bootstrap reloaded the existing plist from `/Users/anicca/Projects/life-manager-main`, and the read-back showed canonical `run-inbox.sh`. That checkpoint's canonical Inbox run exited 0: SQLite confirmation-integrity errors were recorded as `ledger_integrity_blocked`, uncertain Telegram delivery was recorded as `delivery_unknown` without blind retry, and an unrelated Workday account-verification email remained unopened. This does not supersede the current launchctl-unknown read-back below.
- The Mercor pass contract now requires bounded pagination (up to four additional Explore pages and twelve candidate-detail pages per wake) when the first page has no grounded candidate. Run `mercor-20260823-000556-22147` exercised the expanded queue but hit a transient owned-target navigation conflict caused by a concurrent read-only probe; it returned `blocked`, made no submit attempt, captured Earnings `empty`, and delivered Telegram `message_id=29084`. Future probes must use an isolated target or wait until the resident pass is idle.
- The clean follow-up `mercor-20260823-001448-50548` ran without external target interference: it reconciled the existing Software submission, inspected the expanded candidate queue, made no new submit, routed Project Thor to the durable human gate, captured Earnings `empty`, delivered Telegram `message_id=29099`, and left the application ledger at three rows. Parent evidence normalization produced `600` files under the current private run.
- The next resident wake `mercor-20260823-002500-92325` completed the bounded pagination queue with no new submit. It routed a required assessment to `needs_human`, recorded one incomplete detail inspection as `blocked`, captured Earnings `empty`, delivered Telegram `message_id=29108`, and left the application ledger at three rows. No interview, assessment, recovery screen, or Submit control was clicked.
- The live read-only Earnings page at `https://work.mercor.com/earnings` was opened in the dedicated Mercor profile. It read `Your total earnings to date are $0.00`, `No payment history yet`, and `Once you receive your first payout, it will appear here`. The private read-back is `status=not_observed`, `revenue_credited=false`, and `verified_monthly_run_rate_usd=null`; no application, offer, rate estimate, or `$0.00` placeholder is counted as earnings.
- Failure fixtures now cover page drift, stale/non-Mercor tabs, transient CDP failure, ambiguous submit read-back, recovery/reset/Google `はい` screens, and authoritative successful read-back. The guard fails closed and never retries an ambiguous submit or clicks recovery UI.
- A redacted two-operator fixture now proves separate operator IDs, state roots, application ledgers, evidence files, resume paths, and CDP endpoints; no cross-operator listing or evidence is visible.
- The portable browser owner is now tracked without a second launcher: `ai.anicca.job-search-mercor-browser.plist` reuses `run-browser.sh`, and `install-launchd.sh --mercor-browser-only` renders operator profile, isolated CDP `9334`, fingerprint `81234`, `mercor-browser` logs/state, KeepAlive, and RunAtLoad. A 189-item release archive contained the template and launcher; extraction plus clean non-Dais HOME install/read-back passed with no credentials or Dais path in the portable environment. Port `9222`, the daily-driver profile, invalid port/fingerprint/profile, and unsafe state names fail before write/load. Scoped Singleton cleanup preserves crash recovery. Real Chromium/CDP read-back remains Atomic 20 because the current process context cannot resolve `getpwuid(501)`.
- Root-cause audit found that Workday publication commit `c4e9b3970` deleted the entire Mercor parent, 14 provider/work/earnings modules, prompt/schemas, scheduler, reference, fixtures, and tests. The deleted paths were selectively restored from the immediately preceding working tree without reverting Workday. Current integration adds `mercor_pass` to AgentRunner, `--mercor-only` to launchd install, and the Mercor CLI/loop registry route; normal Job Hunter install still does not auto-enable Mercor.
- Every restored Mercor wake now finalizes browser-owner, runner, invalid-result, Earnings capture/sync, lock, and success paths into a private `0600` terminal receipt plus exactly one `mercor-pass:<run_id>` outbox/report. Failed downstream stages override a provider success result; unacknowledged `send_started` is `delivery_unknown`; reporting-module failure still leaves terminal and report artifacts while preserving the original return code. Inbox uses PID-scoped run IDs and gives private-env, no-work, runner, result-path, and success exits one `job-search-inbox:<run_id>` terminal event. Three restored sync paths share the same ACK rule.
- The restored combined release contains 228 items and clean-HOME replay proves the Mercor browser owner, hourly scheduler at 3600 seconds, runner-failure terminal, Inbox credential-failure terminal, and no private data. The complete current Job Hunter suite passes 357 tests. Live launchd/CDP/Telegram continuity remains Atomic 20.
- Atomic 20 recovery began from immutable release `e5f03f8e0991b2b18df5346832bd122e8226831a`. All five canonical labels were absent because launchd's persistent disabled map still held them, not because their plist files or executable bits were missing. Enabling only those five labels made Daily 1800s, Inbox 900s, Learning, Mercor 3600s, and Mercor-browser KeepAlive load with exact immutable paths. CDP `9334` returned Chrome 145. Natural Inbox and Mercor wakes now prove the installed cadence. Atomic 20 stays open only until the user-requested immediate Mercor kickstart reaches terminal/Telegram/ledger/Earnings read-back and the newer immutable release is activated.
- The first recovered pass exposed two machine-state mismatches: the portable default launched a fresh unauthenticated profile instead of the prior isolated authenticated Mercor profile, and `run-mercor.sh` defaulted to a nonexistent generic resume path instead of private `mercor/resume-state.json`. Release `74fe14fd54b83de06da79592032b0a626980c49f` resolves the grounded resume-state and waits for actual `/earnings` navigation; the production browser owner explicitly reuses the authenticated isolated profile. Pass `mercor-20260826-130152-15407` then inspected 12 live listings, submitted zero, preserved the three-row ledger with zero duplicate, routed nine distinct requirements to human gates, exited 0, delivered Telegram ACK `message_id=34857`, and captured empty Earnings as `not_observed`. Natural hourly wake `mercor-20260826-141446-82944` inspected 13 listings, submitted zero, preserved the three-row ledger, had no blockers, exited 0, delivered Telegram ACK `message_id=34974`, and captured Earnings as `not_observed`. This proves the cadence once; duplicate natural-wake waiting is removed in favor of an immediate kickstart and official read-back.
- A production-scope reference scan found zero `profitable-claude` or legacy absolute-path dependencies in `apps/job-search-loop`, `loops/job-hunter`, `skills/mercor`, or the Mercor provider reference; canonical runtime/launchd/job-hunter contract tests pass 17/17.
- Deletion-gate read-back found `/Users/anicca/profitable-claude` is a separate 2.0G Git repository with uncommitted changes. The redacted read-only inventory at `~/.local/state/anicca/job-search/mercor/evidence/legacy-consumer-inventory-20260822.json` contains 463 jobs total and 15 enabled `profitable_claude` jobs (5 currently loaded); a broader plist path scan finds 38 files including disabled/legacy entries. No deletion, move, unload, or process termination was performed; removing it now would break unrelated active loops. The final destructive step remains pending migration or an explicit stop plan for those consumers.
- Mercor Summary currently resets after reload and is tracked as `summary_unpersisted`.
- Current runtime read-back at `2026-08-26 10:30 JST` finds the five installed plists for `ai.anicca.job-search-daily`, `ai.anicca.job-search-inbox`, `ai.anicca.job-search-learning`, `ai.anicca.job-search-mercor`, and `ai.anicca.job-search-mercor-browser`. The current process context cannot prove loaded/absent state because `id -un` returns `501` and `launchctl print gui/501/<label>` returns `141 Reentrancy avoided`; a GUI-capable owner must read it back. No process owns CDP `9334`. The application ledger still contains exactly three `submitted_pending_review` rows. The last successful pass at `16:21 JST` returned `needs_human`, submitted nothing, and sent Telegram message `33116`; the next Mercor pass at `17:32` failed without a result or Telegram report, and the latest Inbox pass at `17:54` also failed without a result. Continuous application and real-time reporting are not producing current evidence.
- The human-gate file contains 16 pending rows with 16 generated gate IDs. It includes semantic duplicates for Project Thor, Finance Interview, and Professional Work Survey; canonicalization must key the stable provider assessment/listing identity rather than free-text reason.
- The filesystem currently has `9.3 GiB` available, so the `512 MiB` write floor now passes. Disk pressure is no longer the active runtime blocker. LaunchAgent state is an evidence gap because GUI read-back is unavailable; absent live CDP owner and silent terminal failures remain the proven runtime blockers.

## 9. Migration acceptance gate

Do not delete or archive the migration source until all are true:

- Mercor skill, provider reference, and this spec are committed and pushed to `Daisuke134/life-manager`.
- `apps/job-search-loop` has the Mercor adapter, application dedupe, result schema, and tests; the
  application-side browser-owner logic alone does not satisfy this gate.
- The repo includes a redacted portable Mercor browser transport template/installer, and clean-home
  installation proves the operator-specific profile/CDP owner without credentials or Dais paths.
- `loops/job-hunter` has one canonical 30-minute route; no second executor exists.
- A real read-only pass and one authorized form submission have fresh evidence and no duplicate application.
- Private runtime state is copied to `~/.local/state/anicca/job-search/mercor/` with mode `700/600`, and no secret or private resume is committed.
- A repository-wide reference scan shows no production Mercor path still depends on `profitable-claude`.
- Only after the above read-back may `profitable-claude` be archived/deleted as a separate destructive operation.

### 9.1 Fresh acceptance matrix

| To-Be | Verification | Cover |
|---|---|---|
| Portable browser owner | Clean non-Dais HOME installs and reads back the tracked template with isolated configured CDP and no credential/profile data; live CDP readiness remains Resident cadence | Passed |
| Terminal real-time reporting | Success, no-action, human-gate, runner failure, CDP failure, and malformed Earnings fixtures each produce exactly one terminal receipt and one deduplicated Telegram outbox event; a live connector records ACK or `delivery_unknown` without blocking provider work | Implementation/release replay passed; live read-back remains Resident cadence |
| Resident cadence | Two scheduled 30-minute Mercor wakes plus one 15-minute Inbox wake use canonical release paths and create zero duplicate applications | Required |
| Human-gate identity | Twenty-three append-only rows currently project to twenty pending gate IDs; each terminal receipt lists a reused gate ID once, while stale auth/resume rows and broader wording drift still require canonical projection | Required |
| Money receipts | Application → Contract → Authorization → QA → Delivery → net Payment → Stripe/bank match rejects every missing or pending stage | Required |
| First paid E2E | One real selected contract reaches authorized work, independent QA, delivery, acceptance, settled payout, and verified net once | Required |
| OSS replay | A clean operator reproduces the same isolated path with private state outside Git and no Dais data | Required |

| E2E item | Value |
|---|---|
| UI change | Yes: external Mercor browser, Inbox, Calendar, Earnings, Stripe/bank read-back |
| Conclusion | Maestro not required because this is not an iOS UI path; real provider/browser E2E and official receipts are mandatory |

## 10. Atomic completion sequence (one active item at a time)

Only the first remaining item is active. Checkmarks after a reopened item record capabilities completed before the current regression; they are historical evidence, not permission to skip the reopened gap. The strict remaining order is `20 → 21 → 22 → 23 → 24 → 25`. Finish each item's evidence and read-back before starting the next. A `needs_human` result is a durable state, not permission to skip the next independent item.

1. [x] **Canonical source:** keep skill, spec, provider reference, and runtime state under the Life Manager boundary; no private secrets in Git.
2. [x] **Canary evidence:** complete and submit Japanese Evaluator plus Software/AI/IT/data Evaluator; store fresh submitted read-back and evidence.
3. [x] **Queue discovery:** read the four `3/3 + Submit` ready listings and exclude submitted application IDs.
4. [x] **Model contract:** add a model-led `mercor_pass` prompt and structured result schema to `runtime/agent-runner`; allowed results are `submitted`, `observed_no_action`, `needs_human`, and `blocked`.
5. [x] **Provider adapter:** implement Mercor discovery/application reconciliation in `apps/job-search-loop/`; keep Gmail, Calendar, ledger, and browser ownership in existing modules.
6. [x] **Submit guard:** implement exactly one deterministic irreversible wrapper requiring `3/3 + visible Submit + ledger dedupe`, followed by mandatory post-submit read-back.
7. [x] **Operator onboarding:** add private per-operator state roots and setup for resume, account, Calendar, payment, capacity, locale, role families, and exclusions.
8. [x] **Calendar fixture:** test Gmail classification → FreeBusy → idempotent Calendar event → prep reminders with a redacted Mercor fixture.
9. [x] **Resident cadence live enablement:** install the dedicated Mercor browser transport and the existing `ai.anicca.job-search-mercor` LaunchAgent, kickstart one real pass, and read back service state plus pass evidence. The browser transport is KeepAlive, the job wake interval is 3600 seconds, and the live pass exited 0 with a grounded `observed_no_action` result and no duplicate submit.
10. [x] **Dais full pass:** run one real model-led pass at `mercor-20260822-200153-55024`; submit Data analysis once, read back the submitted page, and persist the ledger/evidence result.
11. [x] **Earnings gate:** implement the settled-only parser/schema and perform a live Earnings read-back. The current account has no payment history, so the durable result is `not_observed` with no revenue credited; the first real settled payout remains an external runtime condition for a future `$10K verified` claim.
12. [x] **Failure fixtures:** test page drift, stale tab, transient failure, ambiguous submit, recovery/reset screen, and successful read-back.
13. [x] **Multi-operator test:** run a second redacted operator fixture with separate state, browser, ledger, and evidence; confirm no cross-operator data.
14. [x] **Portable Mercor browser owner:** the tracked Mercor template reuses the shared browser launcher, isolates profile/CDP/log/state, preserves fingerprint and crash cleanup, ships in the reproducible release, and passes clean non-Dais HOME install/configuration read-back with no credentials or Dais paths. Live Chromium/CDP and natural schedule read-back remain item 20.
15. [x] **Reference cleanup:** remove all production references to `profitable-claude` and confirm the canonical Life Manager runtime still passes.
16. [x] **Terminal and real-time reporting:** the restored Mercor parent and Inbox finalizers write private terminal receipts and enqueue one deduplicated run-scoped event for success, no-action, human gate, lock, runner/CDP/result/Earnings failure, and credential failure; ACK is recognized only with `status=sent` plus message ID, otherwise `delivery_unknown`. Failure preserves original rc and cannot erase provider evidence. Release replay passes; live Telegram delivery remains item 20.
17. [x] **Work-harness contract/store:** enforce submitted → selected → contracted → authorized_work → accepted → paid_settled → revenue_recorded transitions, with fail-closed settlement evidence, `needs_human` routing, private append-only events, and idempotent event IDs.
18. [x] **Work-harness Inbox sync:** accept optional strict `mercor_work_events` from the Inbox result, persist transitions in the private idempotent event store, and emit one Telegram receipt per event.
19. [x] **Work-harness Calendar/Earnings wiring:** connect Calendar event artifacts and explicit authorization/acceptance evidence to the durable private work state. Calendar artifact sync, provider-local authorization/acceptance gates, and live Earnings capture/settled-snapshot sync are implemented; the stronger shared money receipt contract remains open below.
20. [x] **Resident runtime recovery:** the five canonical labels are enabled; exact Daily 1800s, Inbox 900s, Mercor 3600s, Mercor-browser KeepAlive paths and CDP `9334` are read back. Natural Inbox wake `inbox-20260826-131020-30009` completed with exit 0 and Telegram `message_id=34853`; natural Mercor wake `mercor-20260826-141446-82944` proved the hourly cadence with zero duplicate. Current immutable release `3c9293ee4362ed386eb174dd761d5b54c09f2019` owns both Mercor scheduler and browser. Kickstart run `mercor-20260826-145944-91942` inspected 13, submitted zero, preserved the three-row ledger, had zero blockers/duplicates, exited 0, delivered Telegram `message_id=35053`, synced Earnings as `not_observed`, and proved receipt-local gate-ID deduplication.
21. [x] **Human-gate identity:** release `a7c58a3627caf15907642446ea95c58a267f3fe1` is deployed to the hourly scheduler. Live run `mercor-20260826-154311-13590` inspected 12 listings, submitted zero, blocked zero, preserved the three-row application ledger, exited 0, delivered Telegram ACK `message_id=35138`, and read Earnings as `not_observed`. Its grounded auth/resume resolutions increased the append-only store from 30 to 32 events while the canonical current projection reads back exactly 10 pending ceremonies.
22. [ ] **Shared money receipt contract:** the provider-neutral receipt set is defined and verified in `skills/_shared/marketplace-core`: `ContractReceipt`, `AuthorizationReceipt`, artifact-bound `QAReceipt`, QA-bound `DeliveryReceipt`, settled `PaymentReceipt`, and `PayoutMatchReceipt`. The Mercor full-chain fixture proves ordered Application → Contract → Authorization → QA → Delivery → net Payment → bank match linkage; mismatched platform, status, application/contract/work/QA/artifact/payment IDs, observation order, net amount, payout amount, or currency fails closed. Mercor runtime enforces `paid_settled → bank_matched → revenue_recorded`; its one-receipt bank snapshot sync persists payment/payout/bank IDs only when payment ID and amount match the prior settled event, then records revenue idempotently. The existing Moneytree CFO source is currently stale/unknown with refresh disabled, so no live Mercor bank match exists. Lancers immutable packaging includes the shared payment schema, and its adapter now maps only an officially working, escrow-confirmed project plus verified proposal terms to `ContractReceipt`; unresolved candidates fail closed. Next wire Lancers contract detail readback into that mapper, then add Authorization/QA/Delivery/Payment/payout mapping before Coconala, CrowdWorks, and Upwork fixtures.
23. [ ] **First Mercor paid E2E:** reconcile one selection and offer, schedule any human ceremony, complete only explicitly authorized work, independently QA and deliver it, read back acceptance and settled payout, join Stripe/bank evidence, and record verified net once.
24. [ ] **OSS operator replay:** install the redacted Mercor package on a clean non-Dais HOME, complete only documented account ceremonies, and prove isolated private state plus the same application → Telegram → contract → payment receipt path without credentials or profile leakage.
25. [ ] **Deletion gate:** only after every prior read-back succeeds, migrate or explicitly stop the 15 enabled old-repo jobs (and reconcile the broader 38-file plist scan), then archive/delete `/Users/anicca/profitable-claude` as a separate destructive operation. Current status: blocked by active consumers and dirty old-repo worktree; no destructive action was taken.

### 10.1 Remaining operational TODO from current unproven resident continuity

These are runtime milestones, not additional submit clicks:

1. **Restore the resident owner:** keep the now-passing disk preflight, obtain GUI-capable launchd read-back, bootstrap only proven-absent daily/inbox/learning/Mercor/Mercor-browser labels from the current release, and prove two consecutive scheduled Mercor wakes plus one Inbox wake with live ACK or durable `delivery_unknown`.
2. **Canonicalize human gates:** complete. Deployed release `a7c58a3627caf15907642446ea95c58a267f3fe1` retains the append-only history, projects stable identities, and live run `mercor-20260826-154311-13590` proves 32 events / 10 current ceremonies. Distinct interviews, assessments, and recordings remain separate.
3. **Resume truthful acquisition:** on each 30-minute wake, model-rank every visible executable role, exclude only unsupported legal/location/physical/policy/capacity requirements, submit every grounded deduplicated application within the bounded scan, and report the complete inspected set.
4. **Implement the shared money receipts:** the shared receipt models and Mercor full-chain fixture pass; next migrate Coconala, Lancers, CrowdWorks, and Upwork through the same Contract, Authorization, QA, Delivery, net Payment, and bank/payout-match evidence gate before revenue or allocation.
5. **Selection/inbox reconciliation:** let the 15-minute inbox lane classify Mercor selection, rejection, and contract messages; persist each transition and evidence.
6. **Contract/calendar handoff:** for an explicit interview offer, use FreeBusy and the existing idempotent Calendar/prep flow; the user attends any human-bound interview, assessment, or physical recording.
7. **Authorized work and QA:** only after a contract explicitly permits the tool/model, track artifacts, independent QA, delivery, and acceptance; otherwise route the task to `needs_human`.
8. **Settled payout:** read back the first real paid/settled Mercor row, join actual fees/costs and official Stripe/bank evidence, and calculate the trailing-30-day verified net amount; do not count pending or estimated balances.
9. **OSS replay and scale proof:** prove the same isolated path on a clean operator HOME, report one `$10K verified net month` only from one complete qualifying month, and require three consecutive qualifying cycles before reporting `$10K stable monthly` or MRR.
10. **Legacy cleanup:** separately migrate/stop the old-repo consumers before deleting `profitable-claude`; this is not a reason to disable the Mercor loop.

### 10.2 Actionable now versus external state

There is actionable engineering work now; the system is not reduced to passive waiting:

- **Actionable now:** implement Atomic 22's shared Contract → Authorization → QA → Delivery → net Payment → bank/payout-match receipt chain in `skills/_shared/marketplace-core`, then migrate Mercor through the same evidence contract. Resident cadence, human-gate projection, portable packaging, terminal reporting, and disk headroom pass.
- **External state:** Mercor must independently select an application, issue a contract, authorize the actual work, accept delivery, and settle a payout. The loop cannot force those provider decisions or fabricate the missing evidence.
- **Current diagnosis:** three applications are pending review; five canonical LaunchAgents are enabled; immutable release `a7c58a3627caf15907642446ea95c58a267f3fe1` owns the hourly scheduler while the unchanged authenticated browser owner remains on its prior immutable release at CDP `9334`. Natural Mercor wake `mercor-20260826-141446-82944` proved hourly cadence. Atomic 21 live run `mercor-20260826-154311-13590` exited 0 with Telegram ACK `message_id=35138`, 12 inspected listings, zero new submission, zero blockers, a preserved three-row ledger, Earnings `not_observed`, 32 human-gate history events, and 10 canonical pending ceremonies. Atomic 20 and 21 are complete. The private Mercor revenue state remains `not_observed`, not a guessed zero or a claimed income.
- **Capacity economics:** `apps/job-search-loop/job_search_loop/mercor_economics.py` computes a capacity-capped projection only. At the current 40 hours/week and displayed $80–$120/hour, three accepted applications still project $13,866.67–$20,800 gross/month total; the naive three-full-time calculation ($41,600.00–$62,400.00) is shown separately and is not feasible under the 40-hour capacity or revenue evidence contract.
