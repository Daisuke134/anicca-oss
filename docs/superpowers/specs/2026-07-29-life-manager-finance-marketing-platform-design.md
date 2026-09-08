# Life Manager Portable Runtime + Finance + Self-Improving Marketing Platform Design

**Status:** Canonical program SSOT  
**Scope owner:** Life Manager  
**Canonical repository:** `https://github.com/Daisuke134/life-manager`  
**Current release:** OpenClaw-to-Life-Manager Portable Runtime Migration (local incident recovery)
**First production products:** Anicca iOS and Honne AI
**Primary outcome:** every retained loop runs from a Life Manager-owned local
process with zero OpenClaw dependency. A hosted/cloud profile is future work
and is not an incident-recovery gate.

**Current implementation cursor:** the portable contracts, bounded
financial-report adapter, shared adapter registry, generic video publication
adapter/chain, and one historical Honne JA shadow receipt are proven in code.
The checked-in `runtime-up` path still contains the historical compose/
PostgreSQL adapter, but MKT-01 now provides the accepted direct-process/
file-ledger path for the incident canary. A real provider canary remains
gated on MKT-02 and MKT-03; the historical database path is not used by the
local recovery runtime.
The current production incident is separate from those proofs: as measured on
2026-08-20 JST, every relevant OpenClaw marketing cron is disabled in the live
SQLite scheduler, every Larry/ReelClaw LaunchAgent is explicitly disabled and
unloaded, and no Life Manager marketing scheduler/worker is running. Postiz is
connected and the Anicca/Honne TikTok integrations remain enabled, so billing
or provider authentication is not the proximate stop cause. The next active
slice is controlled recovery from Life Manager: keep the quarantined legacy
state unchanged, restore one known-good product lane, emit a direct public URL
and missed-slot alert to Telegram, prove seven expected cycles, and only then
expand to the remaining Anicca/Honne producers. This incident recovery preserves
existing behavior under Orders 9 and 11; it does not activate post-migration
feature work.

**Near-term mobile-marketing target.** This is a restoration, not a new
marketing-system invention: reuse the retained accounts, creatives, cadence
contracts, and Postiz routes that already worked, move their execution behind
Life Manager, and return each approved account to three expected posts per day.
The return is one account at a time so a known-broken lane is repaired before
its first post; old OpenClaw cron labels remain disabled as rollback evidence.

**Incident recovery execution correction (2026-08-21 JST).** The original
OpenClaw marketing runtime was not Docker-, Colima-, Railway-, or
PostgreSQL-based. The current recovery therefore runs locally from the
canonical checkout as a direct Life Manager process (optionally supervised by
the local OS after an explicit cutover gate). Its durable state is owned by a
Life Manager data root (`LM_DATA_DIR`, default
`~/.local/state/life-manager`) using append-only JSONL ledgers and atomic
claim/lease/receipt files. No database, container runtime, Railway service, or
OpenClaw folder/env/assets is a prerequisite. MKT-01 ports the I-3 claim,
receipt, replay, and Telegram-dedupe boundary to this local ledger. External
provider/Telegram sends remain gated until the lane manifest and controlled
canary slices; no real send was performed by MKT-01.

## 1. Executive decision

Life Manager becomes the single control plane for personal health and autonomous
income loops. The Portable Runtime Migration ships first. Until its local and
cloud gates pass, new finance, marketing-learning, health, and app-generation
features are frozen except where they are required to preserve an existing loop
during migration.

The current incident release has one required outcome and one deferred option:

```text
Required incident outcome — local:
  the retained lane runs from a Life Manager-owned direct local process,
  filesystem ledgers, and local secret boundary with OpenClaw stopped and
  ~/.openclaw inaccessible

Deferred option — hosted:
  a future adapter may run the same contracts in a hosted environment;
  Railway, Docker/Colima, PostgreSQL, and cloud workers are not part of this
  incident and do not block local recovery
```

Repository identity is not inferred from a local directory name:

| Role | GitHub identity | Local state | Rule |
|---|---|---|---|
| **Only active SSOT** | [`Daisuke134/life-manager`](https://github.com/Daisuke134/life-manager), repository ID `1248111245` | canonical checkout is currently `/Users/operator/Projects/life-manager-main` | all new code, specs, plans, issues, CI, deployment config, schedules, and release evidence live here |
| Legacy historical source | [`Daisuke134/life-manager-v0`](https://github.com/Daisuke134/life-manager-v0), repository ID `1273052304` | archived read-only; any old local clone is non-runtime | no new work, runtime, scheduler, CI, or deployment may target it; 35/35 disposition, issue transfer, redirect, archive, and runtime-reference-zero gate are complete |
| Temporary worktrees | branches whose Git common directory is the canonical repository | multiple paths may exist during bounded work | a worktree is not another repository or SSOT; merge or retire it when its task closes |

The selected incident architecture is local direct-process/file-ledger first;
hosted local/cloud parity is deferred and is not a recovery prerequisite:

| Plane | Runtime | Responsibility |
|---|---|---|
| Portable application | Life Manager packages and direct processes; no container requirement | accounts, products, schedules, jobs, ledgers, attribution, experiments, reports, panel, Telegram |
| Local deployment | direct `life-manager` process from the canonical checkout, with optional local OS supervision | private/self-hosted operation without Docker, Colima, Railway, PostgreSQL, or any OpenClaw process/folder |
| Hosted deployment (deferred) | provider-neutral processes and storage adapters | future always-on/multi-tenant operation; not an incident-recovery prerequisite |
| Data plane | Life Manager-owned append-only JSONL ledgers plus atomic claim/lease/receipt files | immutable events, content lineage, metrics, experiments, job state; no mandatory database |
| Object plane | Life Manager-owned local data root and filesystem artifacts | media, evidence, exports, signed migration archives; never `~/.openclaw` |
| Secrets | Life Manager-owned local secret boundary (OS keychain or guarded local env file) | credentials and browser sessions; never stored in prompts, job payloads, logs, or Git |

### 1.1 Deployment and commercial boundary

Local is the only deployment profile in this incident. A future hosted profile
may reuse the same contracts, but it is deferred and must not fork or block the
local recovery.

| Mode | Operator | Availability | Current commercial rule |
|---|---|---|---|
| Local/self-hosted | user operates Life Manager on Mac or Linux | depends on the user's machine and internet | no managed-cloud monthly subscription is required; the user supplies hardware and connector/provider costs |
| Cloud/hosted (deferred) | Life Manager may later operate versioned services on a provider-neutral host | future always-on mode; not used by this incident and no Railway deployment is assumed | any future subscription/entitlement design is outside the local recovery gate |

Annual, lifetime, and usage-only plans are outside the current release. No
numeric price is authorized until measured per-tenant infrastructure and
connector costs establish a sustainable floor.

The migration order is mandatory:

```text
archived historical `life-manager-v0` + one writable canonical repository
  → self-contained canonical repository (`Daisuke134/life-manager`)
  → OpenClaw-dependent local
  → Life Manager-owned local
  → portable local/cloud parity
  → cloud-default production
```

`life-manager-v0`, OpenClaw, Profitable Claude, and repository-specific launchd jobs are migration
sources only. Every retained script, asset, prompt, schedule, state file, and
credential reference moves behind Life Manager-owned contracts and paths
before cloud migration begins. Legacy repositories remain intact until parity
and rollback evidence pass.

## 2. Goal and non-goals

### Goal

`done = true` only when all of the following are true:

1. Every OpenClaw cron entry and relevant launchd job is classified as
   `migrate`, `replace`, or `retire`; no enabled or loaded job is unowned.
2. Stopping `ai.openclaw.gateway` and denying access to `~/.openclaw` does not
   interrupt any retained Life Manager loop.
3. Runtime code, config, schedules, secrets, assets, and state contain no read,
   execute, import, or fallback dependency on `~/.openclaw`,
   `/Users/operator/profitable-claude`, or `/Users/operator/anicca`.
4. The Life Manager local entrypoint runs all retained loops from
   Life Manager-owned code, a local data root, and local secret boundary.
5. A future hosted adapter may run the same contracts; hosted parity is not a
   prerequisite for this incident and must not introduce an OpenClaw fallback.
6. The Life Manager panel and Telegram show the same reconciled financial
   snapshot, including freshness and unavailable-source states.
7. Every published mobile creative has lineage:
   `product → campaign → experiment → format → hook → artifact → publication
   → platform metrics → install → trial → paid revenue`.
8. A learning promotion changes one bounded rule, passes a canary, is consumed
   by the next generation run, and can be reverted from a durable receipt.
9. Anicca iOS and Honne AI each have independent product packs, goals,
   attribution, metrics, and learned weights.
10. A new user can connect a product and run the same engine without Anicca
   names, paths, accounts, or credentials.
11. In cloud mode, powering off the Mac Mini does not interrupt any production
   loop; Codex on the Mac is an optional command client, not an executor.
12. Cloud worker capacity scales horizontally with tenant demand and enforces
   per-tenant quotas, rate limits, concurrency limits, and fair scheduling.
13. GitHub, CI, Railway, local launchd during migration, and every canonical
    spec resolve to repository ID `1248111245`; repository ID `1273052304` is
    read-only archived after a signed import/equivalence manifest passes.

### Non-goals for the first release

| Deferred item | Reason |
|---|---|
| New financial connectors and the full Financial Health dashboard | Preserve and migrate the existing report first; expand it only after local and cloud runtime parity |
| New marketing formats or autonomous self-improvement | Migrate current posting behavior first; activate learning only after publication and metric receipts survive local/cloud parity |
| Autonomous mobile-app creation and App Store submission | Prove growth of existing apps before generalizing the development loop |
| Physical- and mental-health expansion | Preserve currently retained health behavior; new automation follows runtime migration |
| Deleting OpenClaw or legacy repositories | Do not delete history. `life-manager-v0` is archived after its dedicated import/equivalence gate; other runtime sources are disabled and archived only after shadow, canary, restart, and dependency tests |
| Trading with user funds | Financial health is read-only reporting; execution requires a separate risk and authorization spec |
| Accepting raw Apple IDs or passwords | App Store Connect API keys and delegated authorization are safer and automation-compatible |
| Claiming guaranteed `$10k MRR` | `$10k MRR` is a measurable target with explicit assumptions, not a promise |

## 3. Evidence: current state

### 3.1 Measured runtime state

| System | Measured state | Consequence |
|---|---|---|
| Canonical GitHub repository | `Daisuke134/life-manager`, repository ID `1248111245`, public/unarchived, default branch `main`; 3,241 tracked files in the measured checkout | This is the only write/deploy/spec target |
| Legacy GitHub repository | `Daisuke134/life-manager-v0`, repository ID `1273052304`, public/archived, default branch `main`; 35 tracked files, open issue/PR・workflow/webhook/deployment 0 | Retirement complete: 35/35 disposition, missing required behavior 0, canonical redirect exactly1, active runtime reference 0 |
| Local repository naming | canonical repo is cloned as `life-manager-main`; the directory named `life-manager` actually points to `life-manager-v0` | Directory names are unsafe identifiers; use remote URL plus repository ID until the legacy clone is quarantined and the canonical checkout is normalized |
| Live Life Manager launchd paths | measured Life Manager daily, dev, financial, payout, self-build, TaskMarket, uGig, and x402 jobs point to `/Users/operator/Projects/life-manager-main`; none point to `life-manager-v0` | Runtime already selects the canonical repository, but remains local and path-bound |
| Local OpenClaw gateway | Running as `ai.openclaw.gateway` | OpenClaw is still running |
| OpenClaw scheduler | Read-only capture contains 222 stored jobs; 92 are marked enabled | Stored/enabled is not proof of current execution; each row remains unclassified until Task 2 assigns a verified disposition |
| macOS launchd | Read-only capture contains 176 user LaunchAgent plist files and one additional relevant loaded-only label; 161 plist-backed labels are currently loaded. The loaded CFO plist cannot be parsed by `plutil`, and `com.anicca.daemon` has no user LaunchAgent plist; both are preserved as explicit parse errors | launchd is the actual scheduler for many observed loops; malformed or loaded-only rows must not be silently dropped or changed |
| Unified runtime inventory | `docs/migrations/openclaw/runtime-inventory.json` contains all 399 captured OpenClaw and relevant LaunchAgent rows; 269 are enabled or loaded; commands and private identifiers are redacted | All 399 rows now carry one measured disposition (214 migrate, 40 replace, 132 retire, 13 retain-external; zero `unclassified`) with owners and rollback actions per `docs/migrations/openclaw/classification-summary.md`; no scheduler change is authorized by the inventory |
| Profitable Claude marketing engine | Registry, schemas, bounded learning, canary keep/revert, observation terminalizer, and dashboard exist | Reuse these contracts |
| Marketing runtime store | About 20 KB; dashboard and logs only; no run, publication, metric, experiment, or observation ledgers | The production feedback loop is not closed |
| Life Manager Railway | `/health` returns 200 from `life-call-production.up.railway.app` | A functioning cloud control plane already exists |
| Life Manager panel | Timeline, connection, score, gate, API cost, and limited financial-ledger projections exist | Extend; do not build a second dashboard |
| Financial report loop | the legacy launchd loop remains active; the Life Manager local scheduler/job/worker path separately sent real Telegram `message_id=432` with immutable snapshot/effect receipt and restart count `1 → 1` | Report execution is OpenClaw-independent and shadow-ready; scheduler ownership is intentionally not cut over until seven expected runs |
| Financial report scope | x402/TaskMarket/USDC earnings, fees, losses, API costs, payout reserve | Bank, App Store, Stripe, broader crypto, and business P&L are absent |
| Moneytree connector | One connected Japanese bank account is readable and the owner confirms the current plugin readback is accurate | Accept the current readback; no balance-discrepancy incident is open. Keep the private amount and account identifiers out of Git |
| Historical Anicca metrics | 299 seven-day downloads on 2026-05-15; subsequent snapshot showed zero due to ASC collection failure; historical trial start rate about 3.5–3.8% | Data quality must be first-class; zero and unavailable cannot be conflated |

RevenueCat administration and credential operations belong to the separate
Anicca iOS/API product boundary. They are not a Life Manager connector, metric
source, implementation cursor, or completion gate. Hosted Life Manager
entitlement remains Stripe-based.

### 3.2 Mobile marketing incident snapshot (2026-08-20 JST)

This read-only incident measurement supersedes the historical launch state below.
No legacy job was triggered, enabled, disabled, stopped, or restarted during the
measurement.

| Boundary | Measured fact | Consequence |
|---|---|---|
| OpenClaw runtime scheduler | current read-only `openclaw cron list --all --json` reports 319 live scheduler rows; the enabled-only view exposes exactly one enabled job, unrelated `o1c14-funder-program-discovery-daily` with `lastStatus=error`. Mobile-marketing/Larry/ReelClaw rows exist in the live inventory but are disabled. The older on-disk `cron/jobs.json` has only 222 rows: all 32 Larry/ReelClaw-named rows are disabled, while several differently named social jobs retain stale `enabled=true` bytes that disagree with live scheduler state | files, mere row presence, and old enabled flags are not execution proof; OpenClaw is not currently scheduling these mobile-marketing posts |
| macOS launchd | 39 relevant marketing plists remain on disk; zero relevant labels are loaded; the persistent launchd override explicitly disables all 25 Larry/ReelClaw labels | the legacy clock stopped on 2026-08-01; files on disk are not evidence of execution |
| Quarantine evidence | `jobs.json.pre-marketing-quarantine-20260801T0955.bak` and the final launchd logs align with a 2026-08-01 09:55 JST marketing quarantine | the exact actor and intent are unproven; do not mass-enable the old fleet |
| Postiz account | `GET /public/v1/is-connected` returns `connected=true`; integrations return 29 total, 28 enabled, one disabled | cancellation is not the proximate cause |
| Target TikTok integrations | Honne JA `@honnevideo`, Honne EN `@honne_reveal`, Anicca `@anicca.jp4`, and Anicca iOS `@anicca.jp` all return `disabled=false` | provider routing remains configured; TikTok-side publishing credentials remain unproven until a controlled canary |
| JP4 provider recheck (2026-08-21 JST) | Postiz `GET /public/v1/integrations` returns JP4 `HTTP 200`, `disabled=false`; Postiz `GET /public/v1/analytics/cmn8x8hdv028uqx0y4gdfse5t` returns `HTTP 200` with Followers `122`, Videos `303`, and Views `12,510` | the JP4 token can answer read-only provider calls; this does not prove that TikTok accepts a new direct publication |
| Life Manager replacement | generic generation/publication contracts exist, but no Life Manager-owned local marketing scheduler or worker process is currently running | migration code has not yet replaced production scheduling; Docker, Colima, Railway, and a database are not prerequisites |
| Telegram observation | the post notifier repeatedly measures zero posts and sends zero publication messages; daily generic reports still send | zero-output silence must become a missed-expected-slot alert |
| Shared learner | mining fails on judge/source ingestion; scoring skips because only three posts meet the 24-hour cohort and the minimum is ten | there is no closed self-improvement loop today |

The last verified public target posts are:

- Honne JA: `https://www.tiktok.com/@honnevideo/video/7668837367739418632`
- Honne EN: `https://www.tiktok.com/@honne_reveal/video/7668814897594779655`
- Anicca JP4: `https://www.tiktok.com/@anicca.jp4/video/7668590687105058834`
- Anicca iOS: `https://www.tiktok.com/@anicca.jp/video/7668475610708232200`

Honne EN/JA were healthy through their final runs. Several Anicca ReelClaw and
Larry producers were already broken before quarantine by missing hook files,
blank hook IDs, poster argument failures, and fragile environment loading.
Therefore bulk re-enablement is not a valid rollback.

### 3.2.1 Current verified delivery and metric matrix

This is the current operational truth, not the desired three-post policy. A
`verified` row has a Life Manager receipt, an exact public artifact URL, and a
natural-language Telegram receipt. Provider account metrics are useful for
health only; they are not post-level attribution and are never treated as
installs, trials, or revenue.

| Product/account | Verified destination and latest Life Manager proof | Telegram | Current provider metric state | Operational state |
|---|---|---|---|---|
| Honne EN `@honne_reveal` | TikTok; approved ReelClaw direct URL `7676419421304425748` | `27482` | 24h native post and account metrics are preserved with unsupported fields unavailable; 72h/7d observations remain pending | verified metric source; scheduled 0/day, default-off during recovery |
| Honne JA `@honnevideo` | TikTok; approved ReelClaw direct URL `7676425660641889537` | `27515` | 24h native post Views `1,035`, Likes `11`, Comments/Shares/Saves `0`, engagement `1.06%`; Followers `4`, Following `8`, Total Likes `923`, Videos `278`, latest-20 Views `5,650` / Likes `42` / Comments `0` / Shares `0`; reach/watch time/completion unavailable | verified metric source; scheduled 0/day, default-off during recovery |
| Anicca iOS main `@anicca.jp` / `@anicca.jp1` | TikTok `7676422253638176020` remains verified. Instagram `DcTFx_UjSio` proves the native Card/account but is quarantined as a success candidate because caption `強い人の口癖…` does not match its baked hook `怠けてるんじゃない…` | `27500`; Instagram `27510` is historical only | TikTok 24h native post Views `108`, Likes/Comments/Shares/Saves `0`, engagement `0%`; Reach/Watch time/Completion unavailable. TikTok account Followers `257`, Following `33`, Total Likes `17,662`, Videos `470`, latest-20 Views `5,296` / Likes `51` / Comments `1` / Shares `2`; historical Instagram post Views/Reach `6`, other reported counts `0` | TikTok verified; Instagram pack-ready/default-off and exact canary open |
| Anicca JP4 `@anicca.jp4` | TikTok; approved card direct URL `7676495865816632583` | `27939` | Followers `122`, Videos `304`, account Views `11,868`; post-level/install/revenue join open | verified canary; default-off |
| Anicca HE `@anicca.he` | TikTok; existing Postiz row `cmt32u9dj00jxqp0yqdh6yi96` reconciled by exact native profile-caption readback to `https://www.tiktok.com/@anicca.he/video/7676500512308481296` | `28431` | post-level metric collection remains open | verified recovered effect; default-off |
| Anicca EN Card `@anicca.encards` | latest exact LM effect is Postiz `PUBLISHED` row `cmt9rbish00qylf0yw6gt9ulr`, creative `EN-CARD-V2-e678c823480f`, and Instagram Reel `https://www.instagram.com/reel/Dcfph70jorc/`; caption and bound English Card media match | `35175` | 2h/24h/72h/7d/daily are registered with the 30-minute LM owner and remain pending until due; unavailable is not zero | production-armed at 3/day, 08:45/12:45/21:30 JST; replay 0 |
| Anicca JA Larry `@ani.cca1234` | Instagram carousel `https://www.instagram.com/p/DceW-whAQT7/`; all six native slides match the ordered LM objects | `34328` | immediate post counts are measured `0`; account analytics is unavailable/empty response | verified canary; scheduled 0/day, default-off |
| Anicca EN Widget `@anicca.en` | Instagram Reel `https://www.instagram.com/reel/DcekGtmjmOf/`; full native video matches the approved Widget pack | `34435` | immediate Views `14`; Reach/Saves/Likes/Comments/Shares `0`; account and product funnel sources unavailable | verified canary; scheduled 0/day, default-off |
| Anicca JA Widget `@anicca.jp.videos` | Instagram Reel `https://www.instagram.com/reel/DcetvubDA4Z/`; exact owner/caption and full native video match the approved Widget pack | `34523` | immediate Views `173`, Reach `134`, Likes `1`, Saves/Comments/Shares `0`; account and product funnel sources unavailable | verified canary; scheduled 0/day, default-off |
| Remaining integrations without a verified canary, including Anicca JA Card and YouTube candidates | no new Life Manager verified publication receipt | none | no current Life Manager observation row | classified/default-off or held/unassigned; no provider write |

The remaining metric gap is explicit: TikTok, Instagram, and YouTube must each
produce platform-post observations at 2h/24h/72h/7d, then App Store Connect,
RevenueCat, and product analytics must join the same immutable creative and
campaign lineage. Missing data stays `unavailable`, never `0`.

**Seven-day provider readback.** The live Postiz window contains only thirteen
rows across the few accounts above; it is not evidence that every enabled
integration is receiving every video or slideshow. It includes the verified
receipts, the reconciled `@anicca.he` effect, two YouTube
errors, the reconciled-absent JP4 attempt, and quarantined legacy generic or
`lm_wake` rows. The remaining Larry/ReelClaw labels stay explicitly disabled
(twenty-six relevant labels in the live override) and no running legacy
publisher is used. An enabled Postiz integration is routing configuration, not
a claim of current posting, a direct public artifact, Telegram receipt, or
metrics completeness.

### 3.2.2 Recovery re-audit and corrected current truth

This later audit overrides stale completion and cadence claims elsewhere in
this document. It changes no provider or launchd state. The host currently has
30 live Postiz integrations: 17 TikTok, 8 Instagram, 3 YouTube, and 2 X. Of
those, 29 are provider-enabled. The Life Manager manifest describes the 28
mobile integrations as six `target` rows and 22 `hold` rows, but production
cycles do not read that manifest. It is therefore an inventory, not an effect
fence or scheduling control plane. Two enabled X integrations are outside it;
one produced 46 `PUBLISHED` rows and two `ERROR` rows during the same four-day
window.

The six selected mobile schedules remain loaded with calendar triggers. They
are not currently running, but automatic publication paths are still armed.
From 2026-08-22 through 2026-08-25 JST, their expected/provider-published
counts were:

| Destination | Runtime object and format | Configured cadence | Expected / Postiz `PUBLISHED` | Latest caption/account-bound native proof |
|---|---|---:|---:|---|
| Honne EN TikTok `@honne_reveal` | existing vertical MP4; `reelclaw/relationship-confession`; English | 07:00, 11:00, 20:30 JST | 12 / 8 | `https://www.tiktok.com/@honne_reveal/video/7677930257042787605` |
| Honne JA TikTok `@honnevideo` | existing vertical MP4; `reelclaw/relationship-confession`; Japanese | 08:30, 12:30, 21:30 JST | 12 / 9 | `https://www.tiktok.com/@honnevideo/video/7677945699123629333` |
| Anicca main TikTok `@anicca.jp` | existing vertical MP4; `reelclaw-card/nudge-card`; Japanese | 08:00, 16:00, 22:37 JST | 12 / 5 | `https://www.tiktok.com/@anicca.jp/video/7677365911833070866` |
| Anicca JP4 TikTok `@anicca.jp4` | existing vertical MP4; `reelclaw-card/nudge-card`; Japanese | 09:15, 15:15, 20:45 JST | 12 / 2 | `https://www.tiktok.com/@anicca.jp4/video/7677106804039355656` for one exact effect only |
| Anicca HE TikTok `@anicca.he` | existing vertical MP4; `reelclaw-card/nudge-card`; Japanese | 07:15, 13:45, 18:15 JST | 12 / 5 | `https://www.tiktok.com/@anicca.he/video/7677725253916773653` |
| Anicca main Instagram Postiz `@anicca.jp1`, native `anicca.ios.jp` | same Anicca vertical MP4 as a Reel | 19:10 JST | 4 / 3 | `https://www.instagram.com/reel/DcdZEvtET7s/`; latest unauthenticated HTML readback is not independently caption-verifiable |

The total is 32 `PUBLISHED` rows against 64 expected slots. Only 19 rows have
unique direct-native receipts under the current caption/account criterion; 13
provider-success/local-nonterminal effects remain: 12 `failed` jobs with
`unknown_effect=true` and one stale `running` job. They must be reconciled in
oldest-first order without retry or replacement publication. A separate false
completion also exists: two different JP4 video effects were bound to Postiz
row `cmt5exlqb00cjqk0yu6q2xftc` and native TikTok URL
`7677106804039355656` because provider reuse compared integration and caption
but not video hash, creative, or slot. Native thumbnail evidence supports the
first video and contradicts the second. The current completed-receipt API
cannot supersede that provider/video lineage, so this is a code and durable
state repair, not a profile-URL correction.

Current not-yet-classified mobile inventory remains `0/day` until handled one
account at a time:

- Instagram: `@anicca.affirmation`, `@anicca.bochi`, and `@obou.anicca`.
- TikTok: `@anicca_buddha`, `@anicca_slideshow`, `@anicca.comedy`,
  `@anicca.daily`, `@anicca.jp8`, `@anicca.jpx`, `@aniccaaffirmation`,
  `@aniccaen2`, `@aniccajp`, `@aniccajp2`, `@monk_anicca`,
  `@obou_anicca`.
- YouTube: `@anicca-affirmation-video`, `@anicca-ai`, and owner-skipped
  `@anicca-jp`. Only the last remains the one allowed YouTube skip.

There is no live Honne Instagram or YouTube integration, so dedicated Honne
profiles must be provisioned before either platform can be classified or
armed. No Anicca destination may be borrowed and relabelled as Honne.

The current publisher does not generate Larry photo slideshows. Legacy Larry
did generate six/seven-image affirmation slideshows and could cross-post an
Instagram carousel. Legacy ReelClaw card/widget jobs copied pre-rendered MP4
assets and changed captions; they were not per-run renderers. All audited
Larry/ReelClaw mobile OpenClaw cron jobs are now disabled and their related
LaunchAgents are unloaded. The pre-quarantine snapshot proves that some were
previously enabled, but their code accepted Postiz IDs, profile URLs, or
YouTube `QUEUE` as success, swallowed child failures, contained stale or wrong
integration wiring, and produced no caption-matching direct-native receipt.
They remain rollback evidence only. Reusable creative responsibilities are the
Larry fixed-string slide composition, 14-day anti-repeat, image normalization,
Instagram PNG-to-JPEG conversion, and retained asset pools. Existing Honne and
Anicca Card JA packs are already migrated; copying them again is prohibited.

The current hook chooser is LRU rotation, not self-improvement. Metrics and
reward are not chooser inputs; `preferred` is not consumed as a winner; no
keep/revert decision changes the next generation; and the static
`honne_en_base_20260823` campaign token has been shared by eleven hooks. MKT-12A0
is therefore invalid as a causal hook cohort and is reopened after recovery.

Host-wide disk exhaustion was a concurrent safety blocker: the Data volume had
about 266 MiB available at 100% utilization, ordinary writes returned `ENOSPC`,
and several publication logs contained both space and ledger failures.
MKT-09R0 removed only regeneration-safe bun/npm/browser/Codex dependency and
user cache contents, restoring `1,610,212 KiB` available. The content-object and
local-ledger suite passes 20/20; a fresh isolated write below the LM data root
returned object bytes equal, first enqueue `created=true`, replay
`created=false`, and an intact queued job readback. The probe was then deleted.
No LM evidence/state/object pack or OpenClaw file was removed and no provider
adapter ran. Re-download is the recovery path for the removed cache contents.

MKT-09R1 now enforces the recovery fence at the shared local-ledger boundary,
so every cycle using `marketing.video.publish` is checked both before a new
publication enqueue and before an existing queued effect claim. The explicit
LM state file is mode `0600` and `closed`; a future `open` state must name the
one exact allowed effect key. A real Honne EN cycle for the next 07:00 JST slot
created one generation receipt, then durably refused publication job
`marketing-video-publication:7184e3bdde21ee6fd2bfd520ad61585048ebedf3c14ef6a9905ea917bddbe904`
at enqueue. Before/after/replay counts stayed publication jobs `42`, Telegram
jobs `140`, and Postiz window rows `16`; generation moved once from `53` to
`54` and then stayed `54`. Two refusal readbacks are mode `0600`; no job was
loaded, stopped, restarted, unloaded, enabled, deleted, or kickstarted. TDD
proved the missing behavior RED twice, then the focused fence tests pass 2/2,
the full local-ledger suite 20/20, and the affected cycle/canary suite 26/26.

MKT-09R2 repairs the JP4 caption-collision incident without a provider effect.
Postiz caption-only preflight reuse now fails closed unless the row carries the
exact video digest, and both distribution reuse and TikTok metric discovery
assign one provider row/native URL to its first media lineage. The first JP4
effect remains `published` at provider row `cmt5exlqb00cjqk0yu6q2xftc` and
`https://www.tiktok.com/@anicca.jp4/video/7677106804039355656`; TikTok oEmbed
returned account `@anicca.jp4` and caption `心を整える5つの言葉`, while its fresh
thumbnail visually matched first object `35a15c7c…9a15` (woman at a laptop,
hook `怠けてるんじゃない。脳が限界なだけ。`) and contradicted second object
`7e24db96…9ae9` (different person and hook). The second publication job and its
dependent Telegram `30370` receipt are now terminal `conflict`, each retaining
the superseded receipt in append-only history; the correct Telegram `29982`
and first publication receipt are unchanged. The correction added exactly two
job and two receipt events, replay added `0/0`, and metrics discovery returns
one row for the native URL. Focused RED 4 then GREEN 4/4; full Python 50/50 and
Node 44/44 pass. No Postiz/TikTok/Telegram write or legacy job operation ran.

MKT-09R3-01 reconciles Honne JA effect `b9b21411…917c` as `present` without a
new effect. Its exact lineage is slot `2026-08-22T23:30:00.000Z`, creative
`HJA-013-ed3318c496f4`, video `ed3318c4…fcb6`, and caption `ママに「好きにすれば」って
言われた時の正解`. Postiz row `cmt50gsr706qnqp0yak91fvvn` matched the
integration, caption, and time but exposed only a profile URL and internal
suffix `7677000652719114257`; TikTok oEmbed rejected that suffix with HTTP 400.
The profile readback exposed candidate `7677002249733786896`; official oEmbed
then returned `@honnevideo`, the exact caption, and the direct canonical URL,
while its fresh thumbnail visually matched the exact video object's first
frame. Reconciliation appended one job and one receipt event, replay appended
`0/0`, and the same Postiz window remained four rows. No Telegram or provider
write and no legacy job operation ran.

MKT-09R3-02 reconciles Honne EN effect `0b1f8c3f…9784` as `present` without a
new effect. Exact lineage is slot `2026-08-23T02:00:00.000Z`, creative
`HEN-007-154a1508e0a8`, video `154a1508…d36e`, and caption `the dev who built
this deserves a medal`. Postiz row `cmt55ufo008dlqk0yunxhqmlb` matched exact
integration/caption/time but its internal suffix `7677041102414923784` failed
official oEmbed with HTTP 400. Profile readback exposed direct candidate
`7677041244052131080`; official oEmbed returned `@honne_reveal` and the exact
caption, and its fresh thumbnail visually matched the exact video frame and
hook `my friends are actually evil`. Reconciliation events were `1/1`, replay
`0/0`, and the Postiz window remained two rows. No Telegram/provider write or
legacy job operation ran.

MKT-09R3-03 reconciles Honne EN effect `3d52f25e…0fb9` as `present` without a
new effect. Exact lineage is slot `2026-08-23T11:30:00.000Z`, creative
`HEN-008-c8ccd06d2a77`, video `c8ccd06d…f39d5`, and caption `whoever made this
app i love you`. Postiz row `cmt5q6rit02dvqk0yrbnbjwzg` matched the exact
integration/caption/time but its internal suffix `7677186618888996881` failed
official oEmbed with HTTP 400. Profile readback exposed direct candidate
`7677187822066961680`; official oEmbed returned `@honne_reveal` and the exact
caption, and its fresh thumbnail visually matched the exact video frame and
hook `i am so cooked`. Reconciliation events were `1/1`, replay `0/0`, and the
Postiz window remained two rows. No Telegram/provider write or legacy job
operation ran.

MKT-09R3-04 reconciles Anicca JA effect `50d36c4e…fae0` as `present` without
a new effect. Exact lineage is slot `2026-08-23T22:15:00.000Z`, creative
`AJ-CARD-002-35a15c7ce990`, video `35a15c7c…9a15`, and caption `メンタルが強い人の
口癖５選`. Postiz row `cmt6d84wz0b4nqk0ygs409fcx` was the only exact
integration/caption/time row and exposed only the `@anicca.he` profile plus
internal suffix `7677352047456536593`; official oEmbed rejected that suffix
with HTTP 400. Profile readback exposed two caption matches, and TikTok ID
timestamps bound the target slot to `7677353835345595664` at 22:13:55 UTC
rather than the 13-hour-old post. Official oEmbed returned the exact caption
and direct URL, and its fresh thumbnail visually matched the exact video frame,
person, laptop, composition, and hook `怠けてるんじゃない。脳が限界なだけ。`.
Reconciliation events were `1/1`, replay `0/0`, and the Postiz window kept
one exact-effect row. No Telegram/provider write or legacy job operation ran.

MKT-09R3-05 reconciles Honne EN effect `e97d54e5…663d8` as terminal `absent`,
not success. Exact lineage is slot `2026-08-24T02:00:00.000Z`, creative
`HEN-010-c8ccd06d2a77`, video `c8ccd06d…f39d5`, and caption `someone give
the dev a raise rn`. Postiz row `cmt6l9gk90dkyqk0yrdhao2dh` was the only
exact integration/caption/time row; its stored upload bytes exactly matched the
LM video SHA. However it exposed only the `@honne_reveal` profile and internal
suffix `7677412015358511120`, which official oEmbed rejected with HTTP 400.
The current TikTok browser DOM listed 15 public native videos, yt-dlp exposed
the same recent IDs, and exact-caption search found no target; neither the
caption nor slot had a direct native artifact. The durable receipt therefore
keeps Postiz `PUBLISHED` as provider state but records `status=absent`,
`public_url=unavailable`, and `provider_reconciled=false`. Reconciliation
events were `1/1`, replay `0/0`, and the Postiz window remained one row. No
new effect, provider write, Telegram receipt, or legacy job operation ran.

MKT-09R3-06 reconciles Honne JA effect `9cf83b48…8389` as `present` without
a new effect. Exact lineage is slot `2026-08-24T03:30:00.000Z`, creative
`HJA-017-ed3318c496f4`, video `ed3318c4…fcb6`, and caption `「怒ってないよ」の
本音を 翻訳してみた【親編】`. Postiz row `cmt6oh7920nzkqp0yulkbwtfp`
matched exact integration/caption/time but exposed only the `@honnevideo`
profile and internal suffix `7677433871651129360`; official oEmbed rejected
that suffix with HTTP 400. Profile readback exposed direct candidate
`7677435198132342033` at 03:29:38 UTC; official oEmbed returned the exact
caption and account, and its fresh thumbnail visually matched the exact video
frame and hook `上司の「怒ってないよ」絶対怒ってる`. Reconciliation events were
`1/1`, replay `0/0`, and the Postiz window remained three rows. No
Telegram/provider write or legacy job operation ran.

MKT-09R3-07 reconciles Honne EN effect `05024c43…b93a6` as `present` without
a new effect. Exact lineage is slot `2026-08-24T11:30:00.000Z`, creative
`HEN-011-c8ccd06d2a77`, video `c8ccd06d…f39d5`, and caption `wait what does
that even mean`. Postiz row `cmt75mhgu00jrmp0yjsyjzxf5` was the only exact
integration/caption/time row but exposed only the `@honne_reveal` profile and
internal suffix `7677558829269829649`; official oEmbed rejected that suffix
with HTTP 400. Profile readback exposed direct candidate
`7677558878862675201` at 11:29:35 UTC; official oEmbed returned the exact
caption/account, and its fresh thumbnail visually matched the exact video frame
and hook `i am so cooked`. Reconciliation events were `1/1`, replay `0/0`,
and the Postiz window remained four rows. No Telegram/provider write or legacy
job operation ran.

MKT-09R3-08 reconciles Honne JA effect `78685a6f…f9f5a` as `present` without
a new effect. Exact lineage is slot `2026-08-24T12:30:00.000Z`, creative
`HJA-018-b11d88411bdd`, video `b11d8841…9ed4`, and caption `99%の人が間違える
親の「怒ってないよ」`. Postiz row `cmt77rnx301ccmp0yrwo5o99g` was the only
exact integration/caption/time row but exposed only the `@honnevideo` profile
and internal suffix `7677574085069539344`; official oEmbed rejected that
suffix with HTTP 400. Profile readback exposed direct candidate
`7677574370595753232` at 12:29:42 UTC; official oEmbed returned the exact
caption/account, and its fresh thumbnail visually matched the exact video
frame, person, hand, and hook `上司に「怒ってないよ」って言われた時の正解`.
Reconciliation events were `1/1`, replay `0/0`, and the Postiz window
remained two rows. No Telegram/provider write or legacy job operation ran.

MKT-09R3-09 reconciles Honne EN effect `1eacd207…4586e` as `present` without
a new effect. Exact lineage is slot `2026-08-24T22:00:00.000Z`, creative
`HEN-012-de63fa6bb0e2`, video `de63fa6b…b896`, and caption `what do you mean
by that`. Postiz row `cmt7s4qmh08q2mp0yoeuoxs4k` was the only exact
integration/caption/time row but exposed only the `@honne_reveal` profile and
internal suffix `7677724015939667988`; official oEmbed rejected that suffix
with HTTP 400. Profile readback exposed direct candidate
`7677724501978713365` at 22:12:17 UTC; official oEmbed returned the exact
caption/account, and its fresh thumbnail visually matched the exact video
frame and hook `my friends are actually evil`. Reconciliation events were
`1/1`, replay `0/0`, and the Postiz window remained three rows. No
Telegram/provider write or legacy job operation ran.

MKT-09R3-10 closes stale-running Anicca JA effect `8d6553d3…501da` as
`present` without reclaiming or submitting it. Exact lineage is slot
`2026-08-24T23:00:00.000Z`, creative `AJ-CARD-001-7e24db967bf7`, video
`7e24db96…9ae9`, and caption `強い人の口癖、5つだけ`. Postiz row
`cmt7u9up1091mp20yh5fc4zyq` was the only exact integration/caption/time row
but exposed only the `@anicca.jp` profile and internal suffix
`7677736820393920513`; official oEmbed rejected that suffix with HTTP 400.
Profile readback exposed direct candidate `7677736873061551377` at 23:00:17
UTC; official oEmbed returned exact account/caption, and its fresh thumbnail
visually matched the exact object and hook `やらなきゃいけないのに動けない自分が嫌になる`.
Only after readback, the expired external-effect claim returned `null` and
moved `running→reconciling` with lease owner/expiry cleared; resolution then
completed `present`. Job events were `2`, receipt events `1`, replay
`0/0`, and no provider execution or legacy job operation ran.

MKT-09R3-11 reconciles Anicca JP4 effect `404ff87a…41741` as `present`
without a new effect. Exact lineage is slot `2026-08-25T06:15:00.000Z`,
creative `AJ-CARD-003-35a15c7ce990`, video `35a15c7c…9a15`, and caption
`強さは静けさから生まれる`. Postiz row `cmt89tbgo0cb1mp0yq9jrw2x1` was the
only exact integration/caption/time row but exposed only the `@anicca.jp4`
profile and internal suffix `7677848455687047185`; official oEmbed rejected
that suffix with HTTP 400. Profile readback exposed direct candidate
`7677848633974344977`; official oEmbed returned exact account/caption, and
its fresh thumbnail visually matched the exact object and hook `怠けてるんじゃない。
脳が限界なだけ。`. Reconciliation events were `1/1`, replay `0/0`, and the
Postiz window remained two rows. No Telegram/provider write or legacy job
operation ran.

MKT-09R3-12 reconciles delayed Honne EN effect `72454c23…69d45` as `present`
without hiding its schedule miss. Exact lineage is logical slot
`2026-08-25T02:00:00.000Z`, creative `HEN-013-154a1508e0a8`, video
`154a1508…d36e`, and caption `explain this to me like im 5`; the job was not
enqueued until 09:07 UTC. Postiz row `cmt8fyup70dmemp0y83v1lurt` was the only
exact integration/caption row and records actual publish time 09:07 UTC, a
seven-hour delay. Its internal suffix `7677892791749658640` failed official
oEmbed with HTTP 400. Profile readback exposed direct candidate
`7677893103830764816`; official oEmbed returned exact account/caption, and
its fresh thumbnail visually matched the exact video and hook
`my friends are actually evil`. The receipt keeps logical `slot=02:00` and
actual `published_at=09:07` separately. Reconciliation events were `1/1`,
replay `0/0`, and no provider write or legacy job operation ran.

MKT-09R3-13 reconciles delayed Honne JA effect `f9aa4089…b5c81` as `present`
and closes the 13-effect recovery set. Exact lineage is logical slot
`2026-08-25T03:30:00.000Z`, creative `HJA-019-ed3318c496f4`, video
`ed3318c4…fcb6`, and caption `彼女に「好きにすれば」って 言われた時の正解`;
the job was not enqueued until 09:07 UTC. Postiz row
`cmt8fz5ry0dbsp20y17m7b366` was the only exact integration/caption row and
records actual publish time 09:07 UTC. Its internal suffix
`7677892935858227216` failed official oEmbed with HTTP 400. Profile readback
exposed direct candidate `7677893274102811920`; official oEmbed returned the
exact account/caption, and its fresh thumbnail visually matched the exact
object and hook `上司の「怒ってないよ」絶対怒ってる`. The receipt keeps logical
slot and actual publish time separate; reconciliation was `1/1`, replay
`0/0`. Across all 13 recovery effects, terminal decisions are 12 `present`
and one `absent`; the full publication ledger is 41 `completed`, one
`conflict`, and zero unknown/running/reconciling jobs. No new provider effect
or legacy job operation ran.

The public GitHub `main` tree contains the current Life Manager marketing
source, but not the live LM `.env`, ledgers, or object-store MP4s. This audit
does not prove that the full Git history is secret-free. The official Postiz
CLI at fixed commit `77d09c668cb2f7793989a185844d0a0c3d65c951` documents useful
endpoint and payload shapes, but it has no create idempotency or automatic
effect reconciliation, recommends blind create retries, uses a second
credential store, and is AGPL. It is not embedded or invoked. Life Manager
keeps its own minimal client, durable effect ledger, no automatic POST retry,
and GET-only readback/reconciliation. Third-party Postiz CLIs are rejected.

The read-only legacy inventory confirms that the live OpenClaw scheduler
contains marketing targets but zero enabled marketing targets, and the
corresponding launchd agents are unloaded. The older `cron/jobs.json` is not
the live scheduler inventory: it
retains disabled Larry/ReelClaw rows plus stale enabled bytes for jobs such as
`4.7-slideshow-morning`, whose script hard-codes the Honne JA TikTok integration
for unrelated Anicca slideshow content. Those bytes and old backups prove only
past configuration, not current execution, and must not be revived or copied as
account routing. ReelClaw Widget JA has
inconsistent account mappings, Widget/Card EN lacks an approved hook bank, and
Larry JA/EN lacks a reliable native-artifact verification contract. Life
Manager reuses only the creative responsibilities that survive review — fixed
string slideshow composition, 14-day anti-repeat, normalization, Instagram
PNG-to-JPEG conversion, and approved asset pools. It does not copy the legacy
scheduler, account mappings, wrappers, `postId`/`QUEUE` success logic, or
`|| true` error suppression. No legacy job state changed during this audit.

### 3.3 Historical marketing launch state before quarantine

The table preserves the pre-quarantine schedules for behavior equivalence. Its
`Active` cells are historical observations, not the current launch state. It
lists the revenue-relevant daily families, not every unrelated maintenance job.

| Family | Historical cadence | Historical boundary | Pre-quarantine condition |
|---|---|---|---|
| Larry Anicca slideshows | Multiple EN/JA accounts, 1–4 posts per account/day | Profitable Claude wrapper; several scripts return to `~/.openclaw` | Active; some library-post jobs exit 3 |
| ReelClaw Anicca videos | Card/widget EN/JA, 1–2 posts per variant/day | Mostly `~/.openclaw/skills/_dispatcher` and ReelClaw scripts | Active; several jobs exit 1 |
| Honne videos | EN at 07:00/11:00/20:30; JA at 08:30/12:30/21:30 | `~/.openclaw` ReelClaw scripts | Active with recent logs. The JA shadow counterpart was enabled in the local compose stack on 2026-07-30 13:33 JST (worker capabilities generate-only, never `marketing.video.publish`, via a gitignored `deploy/local/compose.override.yaml` outside repo defaults); this was the historical basis for expecting shadow evidence at 12:30/21:30 JST, not proof that the gate is currently accruing |
| Larry strategy learning | 05:10 daily | Reads OpenClaw content metrics and library state | Latest exit 2 |
| Capafy core | 08:10 daily | `/Users/operator/anicca` | Active |
| Capafy goal/marketing | 09:00, 11:20, 16:00 | `/Users/operator/anicca` | Active; latest marketing rows show zero engagement |
| Clipping | Every 86,400 seconds | `/Users/operator/anicca` | Active; latest recorded asset was below quality floor |
| Writer/article | craft train and article resume/daily/learning jobs are loaded; standalone writer daily/learn plists exist but are not currently loaded | Profitable Claude plus local writer CLI | Mixed; loaded jobs include successes and failures |
| Shared marketing metrics | Every 15 minutes | Profitable Claude engine | Runs successfully but has no production ledgers to observe |
| Shared marketing dashboard | Every 15 minutes | Profitable Claude engine | Runs successfully but projects an empty runtime |
| Life Manager financial report | Every five minutes; send gates at 20:00 daily and Sunday 20:05 weekly | Legacy launchd still reads its historical environment; replacement uses Life Manager PostgreSQL jobs and tenant secret refs | Replacement real-send proof complete (`message_id=432`); legacy remains active only for shadow/cutover safety |

Immediate Life Manager marketing routing measured during this design update:

| Route | Scheduler identity | TikTok integration before | TikTok integration after | State |
|---|---|---|---|---|
| Current Life Manager IG + TikTok daily pass | launchd `ai.anicca.life-manager-daily`, 10:15 JST | `cmp9txjdp01c8oh0yb6dhlarr` (`@anicca_buddha`) | `cmpc6cr6g00d8lg0yfythzz9f` (`@anicca.comedy`) | Route change is now proven by B01 TikTok `7667934981481875473`; the old launchd remains loaded for the seven-cycle shadow gate |
| Retired OpenClaw wrappers | `6ad526a5-7f2a-4738-930b-89c158acf10d` and `a7e6095c-1472-4408-ae04-e20598b66cbf` | `cmpc6cr6g00d8lg0yfythzz9f` | n/a | Both are classified `disabled` |
| Retired recording-based successor | launchd `ai.anicca.lm-video-post` | `cmpc6cr6g00d8lg0yfythzz9f` | n/a | Service is not loaded, active plist is absent, and control-plane desired state is `disabled` |

The first `ai.anicca.life-manager-daily` migration slice is operational:

- Life Manager imports the exact B01 video, caption, standing approval, and
  Instagram profile into its own data root. Runtime jobs contain only
  content-addressed `object://sha256/...`, tenant profile, integration, and
  secret references.
- Instagram and TikTok are separate durable publication jobs. One broken
  account cannot block the other platform, and a partial effect cannot be
  misreported as a two-platform success.
- TikTok B01 is publicly reachable at
  `https://www.tiktok.com/@anicca.comedy/video/7667934981481875473`.
  The PostgreSQL job completed on attempt 1 with that raw URL and the exact
  video/caption hashes. Re-enqueue after worker replacement returned
  `created=false`; the provider still contained exactly one matching post.
- Postiz returned a profile URL plus a numeric `releaseId`. The adapter now
  derives and verifies the individual video URL, and reconciles the exact
  recent caption+integration before any upload. A provider success can no
  longer become either a false failure or a duplicate retry.
- The configured Instagram session returns `ChallengeRequired` and remains
  `poisoned_manual_backup`. No IG write was attempted and no unrelated account
  was substituted. Replacing/warming that tenant profile remains required
  before the Instagram shadow counter can start.
- This slice proves portable distribution and restart safety, not creative
  quality or self-improvement. B01 came from the currently retained renderer.

The second `ai.anicca.life-manager-daily` migration slice makes generation
portable without enabling a competing scheduler:

- The creative bank, call audio, stock footage, proof image, and Whisper
  transcript were copied once into Life Manager's private content-addressed
  object store. Generation jobs now carry only those five immutable refs plus
  tenant and date; they read no OpenClaw or other repository path.
- The local Life Manager worker completed three real render jobs on attempt 1.
  A03 produced a 34.666667-second MP4 with SHA-256
  `35e4084f1aa1349029348c908a5d1f07a0db2c9ded1fe5728fcfd2d69fba0cda`;
  state, lock, and render files are mode `0600`, and the MP4 was imported back
  into the immutable object store.
- Real frame comparison exposed why the old rotation did not create visible
  variation: A01 and A02 had different ledger IDs but byte-identical MP4s and
  no rendered text because the runtime image had no fonts. The image now
  packages fontconfig plus Noto/CJK and the ASS contract names that font.
  A03 has a different MP4 hash, and its decoded frame visibly contains both
  `BEFORE LIFE MANAGER • A03` and the Japanese hook.
- Life Manager now has the 10:15 JST generation scheduler contract, but
  `LM_MARKETING_GENERATION_ENABLED` defaults to `false`. It remains disabled
  while the legacy launchd owner is loaded, so this proof cannot create a
  duplicate daily publication.
- This closes portable asset/state/render ownership for this one loop.
  Publication chaining, metric attribution, winner/challenger learning,
  next-run consumption, Telegram marketing reporting, and seven-cycle shadow
  evidence remain open. Autonomous learning still remains frozen until the
  runtime migration gates permit Orders 32–35.

The third slice closes the durable generation→publication handoff without
creating a competing external effect:

- The scheduler scans only one explicit tenant and receipts created on or
  after an explicit cutoff. It accepts only completed, verified
  `marketing_daily_generation` receipts and fans each receipt out into
  independent Instagram and TikTok publication jobs.
- An isolated PostgreSQL proof completed generation attempt 1, found exactly
  one immutable receipt, and created exactly two queued publication jobs:
  Instagram 1 and TikTok 1. Replaying the same scan returned
  `created=false,false`; the database still contained three total jobs
  (generation 1 plus publication 2) and one generation receipt.
- The proof database was dropped after verification. The live local database
  contains zero A03 publication jobs, so this slice made no provider call and
  created no duplicate post.
- `LM_MARKETING_PUBLICATION_CHAIN_ENABLED` defaults to `false`, historical
  backfill has no implicit default, and the legacy
  `ai.anicca.life-manager-daily` LaunchAgent remains loaded
  (`runs=1`, `last exit code=1`). This is a handoff/idempotency proof, not a
  cutover or a claim that Instagram is repaired.

The fourth slice repairs the Life Manager-owned Instagram profile without
posting or modifying the legacy source:

- Browser authorization reached Instagram home after dismissing the provider's
  automated-behavior warning, and the authenticated profile link was exactly
  `/anicca.affirms2/`. No different logged-in browser account was accepted as
  evidence.
- The Life Manager-owned saved session then passed a read-only feed probe,
  launcher probe, and `account_info` identity check for `anicca.affirms2`.
  Recovery atomically changed only that tenant profile row from
  `poisoned_manual_backup` to `ready`, removed the poison fields, preserved file
  mode `0600`, and recorded `recovered_at`.
- A subsequent read-only verification returned `ok=true`, 12 Reels, including
  `/anicca.affirms2/reel/DbPPpXCMjrf/` and
  `/anicca.affirms2/reel/DbKkdfjsaTZ/`. This proves session and account parity;
  it does not claim a new publication.
- A mismatched authenticated username now fails closed: it cannot refresh the
  saved settings or recover the profile. A `ChallengeRequired` remains a
  quarantine event; the private API never retries a password login.
- The legacy `~/.cloak` account row remains
  `poisoned_manual_backup`, the publication chain remains disabled, and
  `ai.anicca.life-manager-daily` remains loaded. No external post or scheduler
  cutover occurred in this slice.

The fifth slice establishes truthful publication measurement without activating
a competing scheduler or claiming unavailable data as zero:

- Every new distribution result carries the provider's post ID and route into
  the immutable publication receipt. Historical receipts remain verifiable but
  cannot be observed unless those join keys already exist; immutable history is
  never rewritten.
- Each publication is observed independently. Deterministic, reference-only
  jobs are created only when its 2h, 24h, 72h, or 7d window is due. Repeated
  scheduler scans rely on the durable job ID for idempotency and cannot create
  duplicate observations.
- An isolated PostgreSQL proof scanned one completed publication receipt,
  created four due observation jobs on the first pass and zero on the second
  (`true,true,true,true` then `false,false,false,false`). A real worker claim
  completed all four controlled-empty observations as `insufficient` with
  `views=null` and `reward.effect=no_change`; the proof database was then
  removed.
- The observation receipt preserves product, creative, platform, clickable
  public URL, provider ID/route, video/caption hashes, exact window timestamps,
  platform metrics, product metrics, and reward eligibility. A measured zero is
  valid; a missing, delayed, unsupported, or unattributed metric remains
  `value=null` with a controlled reason.
- Postiz's per-post analytics route is wired for views, likes, comments, and
  shares. The current product-metric provider truthfully reports
  `attribution_not_configured` until App Store Connect, RevenueCat/product
  analytics, and proceeds sources are added.
- A read-only check of the real B01 TikTok post could not obtain metrics:
  TikTok blocked the direct downloader from the current IP, and Postiz returned
  an empty analytics array. Therefore B01's current measured state is
  unavailable, not zero, and it causes no learning change.
- `LM_MARKETING_OBSERVATION_ENABLED` defaults to `false` and requires an
  explicit tenant, product, cutoff, and worker capability. The existing
  LaunchAgent is unchanged; no post, message, credential mutation, or scheduler
  cutover occurred in this slice.

The sixth slice begins the shared Larry/ReelClaw producer migration with the
measured working Honne JA path and no external effect:

- The one-time importer copied 24 Honne JA hooks and four verified MP4 files
  into the Life Manager data root and immutable object store. Its runtime
  manifest contains only tenant/product/format/locale IDs and
  `object://sha256/...` refs; generation never reads `~/.openclaw` or
  Profitable Claude.
- The generic `marketing.video.generate` adapter is product-, format-, locale-,
  and tenant-scoped. It chooses the least-recent active hook from the imported
  prior-use state plus durable Life Manager receipts and rotates media
  deterministically by schedule slot.
- An isolated PostgreSQL worker proof enqueued one Honne JA job, completed one
  immutable `marketing_video_artifact` receipt, selected `HJA-007`, and emitted
  content-addressed video and copy refs. Re-enqueue returned `created=false`;
  the database remained at one job and one receipt.
- This adapter is a no-effect producer. It did not call Postiz, Instagram,
  TikTok, or YouTube. The existing `ai.anicca.reelclaw-honne-ja` LaunchAgent
  remains loaded at 12:30 and 21:30. Its current launchctl process counters
  were reset by a later service reload (`runs=0`, never exited); the schedule
  and legacy owner remain intact and were not cut over by this slice.
- Measured Larry and Anicca ReelClaw paths are not silently promoted: their
  current logs include missing media/hook/API inputs and non-zero exits. Each
  gets its own repair, import, shadow, and seven-expected-run proof after the
  working Honne slice.

The seventh slice wires Honne JA generic video scheduling into the Life
Manager scheduler in shadow mode with no external effect:

- `apps/life-manager/lib/honne-ja-shadow-schedule.js` encodes exactly the
  legacy `ai.anicca.reelclaw-honne-ja` cadence — `StartCalendarInterval` 12:30
  and 21:30 Asia/Tokyo, verified read-only via `plutil` — as exact-instant
  schedule slots, and `runSchedulerOwner` gained a Honne JA shadow scan behind
  `LM_HONNE_JA_SHADOW_ENABLED`. The flag defaults to `false`, only the
  deliberate exact value `true` enables it, and it is enabled nowhere; the
  legacy LaunchAgent remains the loaded production owner.
- A manual one-shot trigger (`scripts/honne-ja-shadow-cycle.js run`) executed
  one real shadow cycle against the running local durable store on 2026-07-30.
  The one-time importer copied the 24 Honne JA hooks and four MP4s into the
  runtime data volume (pack `object://sha256/609d5414…`), and the cycle
  enqueued and completed generation job
  `marketing-video-generation:0f19ddbb…` for slot `2026-07-30T03:30:00.000Z`
  through the same `createWorkerHandlers`/`executeCapabilityJob` worker path
  as the HJA-007 proof. It selected `HJA-008` — the least-recent active hook
  after HJA-007 in the imported prior-use state — and emitted lineage whose
  `video_sha256`
  (`7658b8130ff2e750a143bd09462d11c30ace49d06aa587754bd088f27399165f`) equals
  the legacy `v2.mp4` SHA-256 byte for byte.
- The chained Instagram and TikTok publication jobs were enqueued durably and
  HELD: both remain `queued`, a durable hold row with status `shadow_held`
  records their job IDs, no worker holds the `marketing.video.publish`
  capability in shadow, and no Instagram, TikTok, or Postiz call occurred. A
  replay of the same slot returned `created=false` for all three jobs, claimed
  zero jobs, and appended no duplicate hold row.
- `scripts/honne-ja-shadow-status.js` reads the durable store and reports the
  count of consecutive EXPECTED 12:30/21:30 JST slots with exactly one
  verified shadow generation receipt each against the §13 seven-expected-run
  gate; expected slots that passed without a receipt row reset the count and
  are reported as `missed_slots`, and duplicate receipts for one slot also
  reset. No ownership cutover occurred and none is claimed.

The historical OpenClaw JSON store contains enabled entries for Larry,
ReelClaw, app reviews, Capafy publishing, CFO sync, and other jobs. The current
SQLite scheduler supersedes that JSON snapshot and has no enabled relevant
marketing row; stale configuration is never treated as a running owner without
a current run receipt.

### 3.4 Evidence and inference are separate

**Evidence:** launchd logs show real posts and executions; the shared marketing
runtime has no ledgers.

**Inference:** repetition is not primarily a prompt-quality problem. The
production generators cannot consume learned weights that were never produced
from attributed observations.

**Evidence:** the existing financial report is based on a wallet ledger and API
cost ledger.

**Inference:** it is an agent-economy P&L report, not yet a personal net-worth
or whole-business financial-health report.

## 4. External constraints and sources

| Source | Core statement | Design consequence |
|---|---|---|
| [Apple Analytics Reports](https://developer.apple.com/documentation/analytics-reports) | “Apple does not generate reports until you create a valid Analytics Report Request.” | Connector setup creates the request once, then polls and downloads all segments; missing requests are an explicit setup state |
| [Railway Cron Jobs](https://docs.railway.com/reference/cron-jobs) | “Services configured as cron jobs are expected to execute a task, and terminate as soon as that task is finished.” | Railway cron only enqueues bounded jobs; long rendering, browser, and learning work runs in leased workers |
| [The Twelve-Factor App: Codebase](https://12factor.net/codebase) | “One codebase tracked in revision control, many deploys” | Local and cloud use the same source and release artifact; deployment configuration changes, business logic does not |
| [Telegram Bot API](https://core.telegram.org/bots/api) | “The Bot API is an HTTP-based interface” | Telegram is a delivery surface, not the financial source of truth |
| [Moneytree LINK](https://docs.link.getmoneytree.com/docs) | Moneytree LINK exposes standardized financial data after user authorization and uses OAuth 2.0 Authorization Code Grant with PKCE | Hosted production tenants connect through Moneytree LINK/OAuth; local owners use their own Codex/Moneytree plugin connection; raw bank credentials never enter Life Manager |
| [Postiz Post Analytics](https://docs.postiz.com/public-api/analytics/post) | “Get analytics data for a specific published post.” | Keep Postiz during migration and collect post-level metrics by provider post ID instead of scraping immediately after publication |
| [Postiz public analytics controller](https://github.com/gitroomhq/postiz-app/blob/39516ab97fab8de49c00300a617bd39e0c325c77/apps/backend/src/public-api/routes/v1/public.integrations.controller.ts) | The public controller delegates `GET /analytics/post/:postId` to `checkPostAnalytics(postId, date)` | Treat provider post ID as a required future receipt join key and keep the observation window explicit |
| [instagrapi best practices](https://github.com/subzeroid/instagrapi/blob/master/docs/usage-guide/best-practices.md) | “Use the settings file next time” after one successful login and verification | Reuse a saved device/session; quarantine challenges instead of hammering password login, and require exact provider identity before recovery |
| [Microsoft SkillOpt](https://microsoft.github.io/SkillOpt/) | “SkillOpt makes the skill document itself the optimization target.” | Skill changes use scored rollout evidence, bounded edits, and a held-out keep/revert gate; production skills are never rewritten unconditionally |
| [GitHub repository archival](https://docs.github.com/en/repositories/archiving-a-github-repository/archiving-repositories) | “You can archive a repository to make it read-only for all users and indicate that it's no longer actively maintained.” | Preserve `life-manager-v0` history without permitting new writes, but only after its unique-content manifest and equivalence gate pass |
| [Stripe subscriptions](https://docs.stripe.com/billing/subscriptions/overview) | “Subscriptions require coordination between your site and Stripe” | Cloud scheduling and worker access follow durable webhook-derived entitlement; a client redirect or checkout page never grants access by itself |

## 5. Alternatives considered

| Approach | Strength | Fatal weakness | Decision |
|---|---|---|---|
| Railway-only monolith | Simple deployment and multi-user SaaS | API, scheduler, browser, rendering, and posting contend for one failure and scaling boundary | Rejected |
| Local-only Life Manager | Maximum privacy and simplest first migration | Lights-out, earthquakes, and machine failure stop production; cannot host thousands of tenants | Supported deployment, not the default |
| Cloud-only Life Manager | Always on and commercially scalable | Blocks the required safe migration path and excludes privacy/self-hosting users | Rejected as the only mode |
| Permanent split runtime where cloud requires a local worker | Reuses local browser sessions | Cloud uptime still depends on the user's machine | Rejected |
| One portable runtime with local and cloud deployment adapters | Removes OpenClaw first, preserves self-hosting, then adds always-on scale without rewriting loop logic | Requires strict adapter contracts and parity tests | Selected |

## 6. Target architecture

```text
                         COMMAND + REPORT SURFACES
       ┌───────────────┬──────────────────┬────────────────────┐
       │ Telegram bot  │ Web/PWA panel    │ CLI / Codex client │
       └───────┬───────┴─────────┬────────┴──────────┬─────────┘
               └─────────────────┼───────────────────┘
                                 ▼
                    LIFE MANAGER PORTABLE CORE
       ┌───────────────────────────────────────────────────────┐
       │ API/auth · tenant/product registry · policy engine   │
       │ scheduler · leased jobs · receipts · event ledgers   │
       │ finance projector · experiment/learning controller   │
       │ connector + loop + storage + secret interfaces       │
       └───────────────┬───────────────────────┬───────────────┘
                       │ same contracts       │
          ┌────────────▼────────────┐  ┌──────▼────────────────────────┐
          │ LOCAL REQUIRED          │  │ HOSTED DEFERRED             │
          │ direct LM process       │  │ provider-neutral processes   │
          │ local API/CLI/report    │  │ optional managed control     │
          │ JSONL ledgers + leases  │  │ optional database adapter    │
          │ filesystem artifacts    │  │ optional object store        │
          │ local secret boundary   │  │ tenant vault (future)        │
          │ local OS supervision    │  │ hosted worker pools (future) │
          └────────────┬────────────┘  └──────┬────────────────────────┘
                       └──────────────┬────────┘
                                      ▼
       ┌───────────────────────────────────────────────────────┐
       │ API │ browser │ media │ publish │ observe │ learn    │
       │ finance │ mobile marketing │ writer │ gig │ Capafy   │
       └──────────────────────────┬────────────────────────────┘
                                  ▼
       App Store · Moneytree · Stripe · social platforms
                                  │
                                  ▼
                    receipts → metrics → decisions
```

The local process is the only required deployment for this incident and must be
OpenClaw-free. A hosted profile is a later adapter exercise; it may add managed
availability, tenant isolation, and horizontal worker scaling, but it is not
allowed to change local business logic or become a reason to start a container
or database now. Codex may submit local commands through the CLI, but scheduled
work must be owned by the Life Manager process itself.

### 6.1 Canonical repository layout

This section is the only SSOT for the program-wide repository layout. The
Agent Economy ownership overlay, wallet/payment-provider boundaries, live
earnings attribution, and financial-independence stages are owned by
[`2026-07-19-anicca-one-repo-consolidation-spec.md`](./2026-07-19-anicca-one-repo-consolidation-spec.md).

The repository boundary is exact:

```text
GitHub SSOT:  Daisuke134/life-manager       (repository ID 1248111245)
legacy only:  Daisuke134/life-manager-v0    (repository ID 1273052304)
```

All new canonical code and operating specifications live in the active Life
Manager monorepo. The target layout extends its existing conventions:

```text
apps/
  life-manager/             # API, panel, Telegram, reports, cloud scheduler
adapters/                   # provider/transport boundaries
runtime/                    # portable scheduler and worker runtime
services/                   # deployable long-running services
skills/                     # generalized capabilities, never product credentials
packages/
  loop-core/
  job-protocol/
  runtime-adapters/
  finance-engine/
  marketing-engine/
  connector-contracts/
  product-packs/
    anicca-ios/
    honne-ai/
deploy/
  local/
    process/                  # direct local process launchers (required path)
  hosted/                    # deferred; not used by incident recovery
docs/
  migrations/
    life-manager-v0/        # signed file/behavior disposition and equivalence evidence
  superpowers/
    specs/                  # program/design SSOT
    plans/                  # ordered executable slices
```

`life-manager-v0` files are not copied blindly into a second top-level product.
All 35 tracked files are classified behavior-by-behavior; missing required
behavior is zero, the README is redirect-only, and the repository is archived.
Local checkout names are never runtime inputs: jobs use installed release/data
roots rather than either absolute checkout name.

The actual implementation follows existing repository conventions instead of
forcing this exact folder shape where it would create churn. Responsibility
boundaries and single-repository ownership are mandatory even if paths differ.
The checked-in `deploy/local/compose.yaml` and its `runtime-up` wrapper are
historical prototype artifacts only; they are not the incident startup path and
must not be invoked for recovery.

### 6.2 Job protocol

Every action is a durable job:

| Field | Meaning |
|---|---|
| `job_id` | globally unique idempotency key |
| `tenant_id` | owner boundary |
| `loop_id` | finance, marketing, writer, gig, or another registered loop |
| `capability` | collect, research, generate, render, publish, observe, learn, report |
| `worker_pool` | api, browser, media, publish, observe, learn, or report |
| `resource_class` | cpu/memory/gpu/browser requirements used for placement |
| `deployment_id` | local or cloud runtime that owns the lease |
| `input_refs` | immutable `object://` or local-file references, never embedded secrets |
| `lease_owner`, `lease_until` | single-writer worker claim |
| `attempt`, `max_attempts` | bounded retry |
| `effect_class` | read, draft, publish, money |
| `status` | queued, leased, succeeded, failed, dead-lettered |
| `receipt_ref` | immutable evidence of the real effect |

Workers advertise capabilities and heartbeat. The scheduler never knows
whether a worker is local or cloud, nor whether the implementation is Claude,
Codex, Hermes, or deterministic code. OpenClaw is not an adapter or fallback.

### 6.3 Runtime adapter contract

| Interface | Local implementation (required now) | Hosted implementation (deferred) |
|---|---|---|
| State/ledger | Life Manager-owned append-only JSONL and atomic files | optional hosted database adapter |
| Queue/leases | filesystem claim/lease files with atomic rename | provider-managed claim protocol |
| Objects | local filesystem under `LM_DATA_DIR` | optional object storage |
| Secrets | OS keychain or guarded Life Manager env file | tenant-scoped cloud vault |
| Browser profile | encrypted local profile under the Life Manager data root | encrypted tenant-scoped cloud profile |
| Scheduler | direct Life Manager local process | the same scheduler contract in a hosted process |
| Workers | local child processes or OS-supervised processes | hosted worker pool |
| Observability | local logs/receipts in the data root and panel/Telegram projections | centralized hosted logs/receipts |

Business logic may depend only on these interfaces. It may not branch on
OpenClaw paths, machine usernames, or repository locations. Migration tests
run with `~/.openclaw` inaccessible.

### 6.4 Multi-tenant execution rules

| Concern | Required behavior |
|---|---|
| Isolation | every job, object, secret, browser profile, artifact, and receipt carries `tenant_id`; authorization fails closed |
| Fairness | tenant queues use weighted fair scheduling; one tenant cannot consume every worker |
| Limits | per-tenant publish, browser, rendering, spend, and connector rate limits |
| Idempotency | scheduler retries may create attempts, never duplicate external effects |
| Browser state | encrypted, tenant-scoped cloud profiles; ephemeral execution containers; no shared cookies |
| Scaling | each worker pool scales independently from queue depth and job age |
| Recovery | leases expire; another eligible worker in the same deployment reconciles before retrying an unknown effect |

Local mode keeps the same `tenant_id` boundary even for a single user. This
prevents local-only shortcuts from breaking later cloud migration.

### 6.5 Connector contract

Every source implements:

```text
connect() -> authorization state
sync(cursor) -> immutable source events + next cursor
health() -> freshness, last success, actionable error
normalize(events) -> canonical ledger rows
```

Initial connectors:

| Domain | Connectors |
|---|---|
| Cash and net worth | Local: the owner's authenticated Codex plus the OpenAI-curated Moneytree plugin. Cloud: Moneytree LINK/OAuth. Both normalize into the same ledger; manual balance and exchange/wallet read-only APIs remain fallbacks |
| Mobile apps | App Store Connect Analytics/Sales, Mixpanel, and product-pack metric inputs |
| Web products | Stripe |
| Autonomous income | uGig, Capafy, clipping affiliate, writer, x402, bounty |
| Distribution | TikTok, Instagram, YouTube, X through OAuth/API where available and isolated cloud-browser adapters where necessary |

An unavailable connector returns `unavailable` with its last successful
snapshot. It never returns a fabricated zero.

#### 6.5.1 Moneytree deployment adapters

Moneytree transport follows deployment ownership; it is not one global
credential path.

| Mode | Authentication and execution | Required owner ceremony | Product boundary |
|---|---|---|---|
| Local/self-hosted | The owner signs Codex Local in with their own ChatGPT/Codex subscription, installs the OpenAI-curated Moneytree plugin, connects their own Moneytree account, and the local Life Manager scheduler invokes that authenticated Codex tool | One Codex sign-in and one official Moneytree connection/consent; repeat only after an explicit expired or revoked state | No Life Manager Moneytree LINK client is required. The Mac must remain online and plugin availability, Codex quota, and Moneytree freshness remain external dependencies |
| Cloud | The hosted tenant authorizes the Life Manager Moneytree LINK client with OAuth Authorization Code + PKCE; cloud workers sync only that tenant | One tenant-scoped Moneytree OAuth consent | Requires Moneytree LINK production approval, registered redirect URI, granted read scopes, tenant vault isolation, and contract/usage compliance |

The OSS installer may verify Codex authentication and offer the canonical
Moneytree plugin install/connect action. It MUST NOT copy another user's Codex
session, silently consent to financial access, extract plugin credentials, or
claim connection until a real account readback succeeds. Until the supported
plugin-install interface is production-stable, onboarding presents the exact
owner action and resumes automatically after readback instead of depending on
an undocumented installer API.

An adapter becomes `available` only after a contract test proves that it can
project its provider response into the same source receipt: tenant, adapter
id, provider, account pseudonym, integer JPY balance, retrieved time,
provider data time when supplied, newest transaction time, cursor, and
raw-response hash. A plugin whose callable tool lacks sufficient structured
readback remains `unavailable` or `stale_or_unconfirmed`; agent prose and a
rendered widget are not parsed as a canonical financial receipt.
`retrieved_at` never substitutes for `provider_data_at`. When provider data
time is absent or a newer official bank readback disagrees, retain both values,
mark the Moneytree snapshot `stale_or_unconfirmed`, exclude it from fresh
deltas and savings limits, and tell the user the exact discrepancy. Never
silently overwrite the audit row or call a plugin response real-time bank data.

## 7. Financial-health model

### 7.1 Canonical statements

| Statement | Contents |
|---|---|
| Personal balance sheet | cash, investments, crypto, receivables, liabilities, net worth |
| Daily cash flow | money in, money out, transfers, fees, realized gains/losses |
| Business P&L | revenue, refunds, platform fees, API/compute/ad costs, contribution profit by business |
| Recurring revenue | MRR, active paid, trials, new MRR, expansion, contraction, churn |
| Liquidity and risk | runway, tax reserve, emergency reserve, concentration, stale sources |

Every supported amount is an integer JPY value with a timestamp. Non-JPY rows
are shown as unsupported and excluded; Life Manager does not convert them.
Transfers are excluded from income. Unrealized asset appreciation is separated
from earned business income.

### 7.2 Business dimensions

Initial business keys are:

`life_manager`, `anicca_ios`, `honne_ai`, `gig_work`, `capafy`,
`clipping_affiliate`, `writer`, `nisa`, `crypto_yield`,
`crypto_trading`, `x402`, and `bounty`.

The list is data, not code. Users can add products and businesses without a
deployment.

### 7.3 Telegram financial report

Agent Economy revenue attribution and live amounts MUST come from
[`2026-07-19-anicca-one-repo-consolidation-spec.md` §0.4](./2026-07-19-anicca-one-repo-consolidation-spec.md#04-agent-economy-earnings-ssot).
Unverified income, self-pay, seed capital, and principal recovery MUST NOT be
rendered as revenue; unavailable amounts render as `$0.00` or `unavailable`.

Daily report time defaults to 20:00 in the user's timezone, after the existing
financial-report convention. Each tenant can configure a different report
time; no second scheduler or report contract is created.

```text
FINANCIAL HEALTH · 2026-07-29

Net worth          ¥X       today +¥Y
Liquid cash        ¥X       30-day runway N months
Income today       ¥X       month ¥Y
Costs today        ¥X       month ¥Y
Operating profit   ¥X       margin Z%

Business           Today    Month    MRR
Life Manager       ...
Anicca iOS         ...
Honne AI           ...
Gig / Capafy       ...
Crypto / x402      ...

Mobile growth
Anicca: installs N · trials N · paid N · MRR $N
Honne:  installs N · trials N · paid N · MRR $N

Data health
Moneytree fresh · product metrics fresh · ASC delayed 1d

Action taken
Promoted hook rule H-17 after 72h canary; paused format F-04.
```

Delivery policy:

| Message | Cadence |
|---|---|
| Financial health | Daily |
| Weekly CFO review | Sunday evening |
| Month close | First complete day after month-end data is available |
| Exception | material source failure, unexpected spend, payment failure, abnormal revenue drop |
| Physical health | Separate daily message in a later release |
| Mental health | Separate daily message in a later release |

The daily message stays concise. Details open the authenticated panel.

### 7.4 Telegram UI/UX

Telegram is the daily command surface; the web panel is the detailed system of
record. The bot sends one scheduled digest per health domain plus
exception-only alerts, rather than narrating every background job.

The same Telegram contract operates in local and cloud modes. `残高`, `明細`,
`今月`, `節約案`, and `接続状態` enqueue read-only financial jobs and reply
with the canonical snapshot hash, source freshness, and any excluded account
or liability. Telegram never receives bank passwords, Moneytree tokens, Codex
credentials, or full account numbers. A savings recommendation is unavailable
until liabilities, scheduled payments, source freshness, and the configured
cash-reserve floor are sufficient; the bot names the missing evidence instead
of inventing an amount.

Before any Moneytree value enters a model prompt, JSONL event, ledger receipt,
or Telegram message, deterministic code projects an explicit field allowlist
and replaces the provider account identifier with a tenant-scoped pseudonym.
Acceptance requires negative fixtures proving plain account numbers, access or
refresh tokens, raw provider payloads, and unapproved fields occur zero times
in prompts, logs, receipts, and Telegram output.

```text
                         TELEGRAM TO-BE

 /start
   │
   ├─ identity + tenant
   ├─ choose Local or Cloud
   ├─ create Base/Solana public addresses and start zero-balance crypto core
   ├─ connect additional Finance / Health / Products capabilities when needed
   ├─ choose timezone + report times
   └─ first source-health check
             │
             ▼
 ┌──────────────────────────────────────────────────────────┐
 │ LIFE MANAGER · TODAY                                     │
 ├──────────────────────────────────────────────────────────┤
 │ Financial   —        no verified change yet              │
 │ Physical    —        no verified change yet              │
 │ Mental      —        no verified change yet              │
 │ Income      gross $0.00 · cost $0.00 · net $0.00         │
 ├──────────────────────────────────────────────────────────┤
 │ Next report: Financial Health at 20:00 JST               │
 │ Runtime: Cloud · healthy · Mac not required              │
 ├──────────────────────────────────────────────────────────┤
 │ [Financial] [Physical] [Mental]                          │
 │ [Income loops] [What changed?] [Open web panel]          │
 └──────────────────────────────────────────────────────────┘
             │
             ├─ scheduled separate reports
             ├─ user-requested drill-down
             └─ material exception alert
```

Report ownership is tenant-scoped:

| Recipient | Receives |
|---|---|
| Personal user | only their financial, physical, mental, products, loops, actions, and connector health |
| Business/product operator | their product funnel, revenue, publishing, experiments, and blocked actions |
| Life Manager platform owner/admin | aggregate SaaS MRR, active tenants, infrastructure cost, job reliability, and anonymized system health; never another tenant's raw ledger or health data |

The daily set is:

| Report | Core fields |
|---|---|
| Financial Health | net worth, cash, runway, income, costs, profit, MRR, source freshness, material deltas |
| Physical Health | sleep duration/quality, HR/HRV when available, steps/activity, recovery trend, one highest-leverage action |
| Mental Health | mood, stress, focus, screen time, habits/reflections, risk trend, one bounded intervention |
| Income & Growth | revenue and funnel by business; jobs attempted/won; posts, reach, installs, trials, paid, retention; best/worst experiment; rule kept/reverted |
| Runtime Health | local/cloud mode, last successful cycle, failed connectors, blocked jobs, stale data, required authorization |

Financial Health example:

```text
┌──────────────────────────────────────────────┐
│ LIFE MANAGER · FINANCIAL HEALTH              │
│ Wed, Jul 29 · data through 19:58 JST         │
├──────────────────────────────────────────────┤
│ HEALTH SCORE  72 / 100        ▲ 4 today      │
│ Net worth     ¥12,340,000     ▲ ¥31,400      │
│ Cash runway   14.2 months     Healthy        │
│ MRR           $1,240          ▲ $84 MTD      │
├──────────────────────────────────────────────┤
│ TODAY                 MONTH                  │
│ Income   ¥42,100       ¥611,300              │
│ Costs    ¥11,900       ¥203,800              │
│ Profit   ¥30,200       ¥407,500              │
├──────────────────────────────────────────────┤
│ BUSINESSES                                   │
│ Life Manager      ¥18,400  MRR $620          │
│ Anicca iOS         ¥7,900  38 installs       │
│ Honne AI            ¥3,100  21 installs       │
│ Gig / Capafy       ¥10,800                    │
│ Crypto / x402       ¥1,900                    │
├──────────────────────────────────────────────┤
│ NEEDS ATTENTION                              │
│ ⚠ App Store data is 26h old                  │
│ ↓ Honne install→trial fell 18% vs 7d base    │
├──────────────────────────────────────────────┤
│ LIFE MANAGER DID                             │
│ ✓ Published 8 creatives                      │
│ ✓ Kept hook H-17 after 72h canary            │
│ ↩ Reverted format F-04                       │
├──────────────────────────────────────────────┤
│ [Open dashboard] [Explain changes]           │
│ [Mobile apps]   [Fix connection]             │
└──────────────────────────────────────────────┘
```

Income & Growth example:

```text
┌──────────────────────────────────────────────┐
│ LIFE MANAGER · INCOME & GROWTH               │
│ Today · compared with 7-day baseline         │
├──────────────────────────────────────────────┤
│ TOTAL        ¥30,200 profit    MRR $1,240    │
├──────────────────────────────────────────────┤
│ MOBILE APPS                                  │
│ Anicca   8 posts · 38 installs · 2 trials    │
│           1 paid · $620 MRR · CPA ¥—         │
│ Honne    6 posts · 21 installs · 1 trial     │
│           0 paid · $180 MRR                   │
│ Best: H-17 confession hook · +42% installs   │
│ Next: test a distinct demo-first challenger  │
├──────────────────────────────────────────────┤
│ OTHER LOOPS                                  │
│ Gig       12 applied · 2 replies · 1 won     │
│ Capafy     4 published · ¥8,400 attributed   │
│ Writer     1 published · ¥0 confirmed        │
│ Crypto     ¥1,900 realized · risk within cap │
├──────────────────────────────────────────────┤
│ AUTONOMOUS CHANGES                           │
│ ✓ kept H-17 after 72h revenue canary         │
│ ↩ reverted F-04 after trial conversion drop  │
│ ⚠ Instagram session needs authorization      │
├──────────────────────────────────────────────┤
│ [Apps] [Experiments] [Loops] [Fix account]   │
└──────────────────────────────────────────────┘
```

Conversation flow:

```text
/start
  → Create tenant
  → Connect money sources
  → Add products/businesses
  → Connect App Store + channels
  → Choose timezone/report times
  → First reconciled snapshot

Scheduled digest
  → scan health and exceptions in under 30 seconds
  → tap a problem or business
  → receive a compact drill-down
  → open the authenticated web panel for ledger/experiment detail

Exception alert
  → state what changed, financial impact, and evidence
  → offer only safe actions: explain, retry sync, pause loop, open panel
  → money movement or expanded public broadcast requires its own authorization
```

Default delivery:

| Time | Message | Interaction |
|---|---|---|
| 08:00 | Physical Health | sleep/activity/recovery summary and one action |
| 20:00 | Financial Health | net worth, income, business P&L, mobile funnel, actions, data health |
| 20:10 | Income & Growth | product funnels, loop earnings, experiments, autonomous changes |
| 21:00 | Mental Health | mood/stress/attention summary and reflection |
| Immediate | Material exception only | source failure, payment failure, abnormal revenue/spend, blocked effect |
| Sunday 20:30 | Weekly CEO/CFO review | week-over-week financial, product, loop, health, and priority review |
| Month close | Monthly statement | net-worth change, P&L by business, MRR bridge, costs, taxes/reserves, retained learnings |

Users can disable or reschedule any non-critical digest. Immediate messages are
limited to material exceptions and authorization requests; successful
background jobs appear in the next digest rather than generating notification
spam.

## 8. Self-improving mobile marketing loop

### 8.1 Loop

```text
                           LIFE MANAGER SHARED MARKETING ENGINE
┌─────────────────────────────────────────────────────────────────────────────────┐
│ Tenant → Product pack → Audience/account → Goal                                 │
│                       Anicca JA/EN | Honne JA/EN                                │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 v
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 1. OBSERVE                                                                      │
│ Market/source URLs → viral-format DNA → rights/source receipt → format library  │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 v
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 2. PROPOSE                                                                      │
│ Planner selects a portfolio; it does not force one producer forever             │
│ Larry slides | ReelClaw UGC+demo | Remotion | MoneyPrinterTurbo | future packs  │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 v
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 3. PRODUCE + GATE                                                               │
│ Distinct hook/format/scene/CTA → duplicate + proof + locale + policy + QA gates │
│ artifact_id + source_id + skill_version + product_id + account_id are immutable │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 v
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 4. PUBLISH                                                                      │
│ Postiz remains the primary provider for TikTok, Instagram, YouTube, and X.      │
│ → provider post_id → exact public URL. The current IG fallback is temporary;   │
│ YouTube support is not wired into the generic LM adapter yet. Do not replace    │
│ Postiz or enable a local publisher until the same receipts/metrics gates pass.  │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 v
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 5. OBSERVE AT THE RIGHT HORIZON                                                 │
│ 2h delivery/hold | 24h engagement | 72h install/activation | 7–35d paid/retain  │
│ Postiz/platform + App Store Connect + product analytics                         │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 v
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 6. REWARD + LEARN                                                               │
│ revenue/retention > paid > trial > activated install > click > watch > view     │
│ One blamed rule → bounded SkillOpt edit → challenger → held-out/canary gate     │
│ keep or revert + rejected-edit memory + next-run consumption proof              │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 │
                 ┌───────────────┴────────────────┐
                 v                                v
      Telegram: natural-language receipts    Panel: read-only receipts,
      + app/revenue delta + source health   funnels, learning and failures
                 │                                │
                 └───────────────┬────────────────┘
                                 └──────────────────────────────→ next generation
```

### 8.2 What is shared and what is isolated

| Shared across products | Isolated per product/channel/audience |
|---|---|
| job protocol, ledgers, experiment controller, canary/revert, dashboards, connector interfaces | promise, proof, offer, forbidden claims, hook weights, format library, platform account, locale, attribution, reward |

This prevents an Anicca hook winner from silently changing Honne output.

### 8.3 Format DNA

The engine learns structure, not verbatim creative:

| Slice | Examples |
|---|---|
| Hook | confession, contradiction, threat, curiosity gap, specific outcome |
| Narrative | problem–agitation–proof, before/after, demo, reaction, list |
| Visual grammar | first-frame text density, cuts, slide count, product reveal timing |
| Audio | voice, music energy, silence, sound-effect timing |
| CTA | install, comment, save, trial, direct proof |

Each format records source URL, observed date, public metrics, extracted
structure, allowed reuse, and similarity fingerprints. Copyrighted assets are
not copied into the product.

### 8.4 Variation gates

A candidate fails before rendering when:

1. Its normalized hook equals a hook used by the same account in the recent
   exclusion window.
2. Semantic similarity to a recent hook exceeds the configured product limit.
3. The same format family would exceed its rolling share cap.
4. Product proof, locale, platform, or CTA does not match the product pack.
5. The visual checksum or transcript is effectively a duplicate.

Variation is measured, not requested with a prompt.

### 8.5 Reward hierarchy

| Horizon | Signal | Use |
|---|---|---|
| 2 hours | valid publication, views, first-frame hold | detect delivery or creative failure |
| 24 hours | hold, completion, saves, profile/product clicks | rank hooks and formats |
| 72 hours | attributed installs and activated users | promote acquisition winners |
| 7–35 days | trials, paid users, proceeds, retention | promote revenue winners |

The optimizer uses the deepest available reward. Views cannot override a
measured loss in paid conversion. Missing revenue remains unknown rather than
zero.

### 8.6 Anicca and Honne product packs

| Pack | Required proof |
|---|---|
| Anicca iOS | notification/Nudge experience, actual intervention outcomes, supported problem types, real paywall and price |
| Honne AI | real product demo, language-matched assets, actual transformation, real store/paywall path |

Each pack owns JA and EN audiences separately. Existing Larry and ReelClaw
producers become adapters behind the same artifact/publication contracts.

**2026-08-21 asset-lineage correction.** The legacy asset inventory was read
without starting any legacy job. The retained mobile mapping is:

| Product | Approved legacy family | Locale/media family | Destination rule |
|---|---|---|---|
| Honne AI | ReelClaw Honne demo | `honne-en` / `honne-ja` | TikTok only; EN `@honne_reveal`, JA `@honnevideo` |
| Anicca iOS | Larry slideshow plus ReelClaw card/widget | Larry account variants; `card-en/ja`; `widget-en/ja` | only the current Life Manager lane manifest assignments |

The `lm_wake_EN.mp4` and `lm_wake_JA.mp4` files are Life Manager wake/demo
artifacts, not one of these mobile marketing packs. They must not be selected
for Honne or Anicca iOS publication. A one-time migration may copy a legacy
pack into the Life Manager object store, but the runtime must resolve only
object references under `LM_DATA_DIR` and never read the legacy directory,
environment, or scheduler.

The one-time local import now exists for both Honne locales: EN pack
`object://sha256/0c28dee02059c6853c614c897bb8d47820aeeaa951432c8953577d8cde617706`
with media `de63fa6b…`, `c8ccd06d…`, `154a1508…`, and JA pack
`object://sha256/d550a0a6732134ea23292818f3cf389db6f31d6ad699652c9eccb7b6a92486ab`
with media `b11d8841…`, `7658b813…`, `ced745f2…`, `ed3318c4…`. These refs
are local evidence only; no OpenClaw path is part of the runtime contract.

### 8.7 Measured state of the shared marketing engine (2026-07-30)

Everything below was read off the running system, not inferred. It supersedes any
earlier statement that Postiz was cancelled.

**Current recheck (2026-08-20 JST).** Postiz still answers
`connected=true`; 28 of 29 integrations are enabled and all four target
Anicca/Honne TikTok integrations are enabled. The target accounts received zero
new Postiz submissions from 2026-08-01 through 2026-08-20. The stop is upstream:
the legacy schedulers are quarantined and the Life Manager replacement is not
running. Postiz subscription and API authentication are therefore eliminated
as the proximate cause; TikTok-side publishing credentials remain unverified
until a controlled canary. The historical engine details
below remain useful for migration equivalence, but their launch-state claims are
not current.

**Local-runtime correction for this incident.** The preceding portable/cloud
design text is not an instruction to start Docker, Colima, Railway, or
PostgreSQL. The recovery control plane is a direct local Life Manager process
with Life Manager-owned append-only JSONL ledgers and atomic filesystem
claims/receipts under `LM_DATA_DIR` (default
`~/.local/state/life-manager`). MKT-01 now owns the I-3 claim/receipt/replay
boundary through this local ledger: live/dead lock ownership is fail-closed,
partial JSONL tails recover without accepting mid-file corruption, heartbeat
markers survive crash ordering, and expired external effects enter
`reconciling`/`unknown_effect` instead of retrying a possibly-posted effect.
The old SQL/`pg` path is not used by this incident runtime. No OpenClaw path,
env file, asset, or state may be read.

**Distribution actually in use.** `GET api.postiz.com/public/v1/integrations` returns
**29 live integrations** (16 TikTok, 7 Instagram, 3 YouTube, 1 X). `anicca-larry`'s
`post-to-tiktok.js` posts through that API with `POSTIZ_API_KEY`. Instagram
additionally has an agent-owned fallback lane (account created by
`ig-account-create`, posted by `marketing-engine/poster.py` over instagrapi).
**Decision: keep Postiz as the primary provider for this recovery and do not
schedule a Postiz exit.** Cutting it now would strand the existing accounts. Any
future local replacement is optional and must pass a separate provider
parity/cutover gate; it is not a reason to read OpenClaw or re-enable legacy jobs.
The fallback remains evidence of historical operation, not a new production owner.

**How shared the engine really is.** One provisioning path is genuinely shared; the
posting and config paths are not yet:

| Component | clip | capafy | life-manager |
|---|---|---|---|
| `provision_prompt.sh` | yes (`clip_pass.sh:14`) | yes (`capafy-ig-marketing-daily.sh:17`) | — |
| `load_manifest.sh` | **no** | yes (`:19-20`) | — |
| `poster.py` | **no** (own instagrapi call in `run.sh`) | yes (`capafy-goal-monitor.sh:35`) | — |
| `run_agent.sh` | — | yes | yes (`life-manager-dev-d0.sh:11`) |

**Why every post looked identical.** The library was scraped once (2026-05-28, 68 EN
rows) under a "scrape once, generate forever" rule, and `fixed-strings-larry-en-v1.json`
pins the hook, the CTA and a single static background for all seven slides.

**The money baseline, read live from RevenueCat on 2026-07-31:** MRR **$20**, 5 active
subscriptions, **0 active trials**, $3 revenue and 230 new customers over 28 days
against 275 active users. Zero trials is the first thing to explain — the funnel is
not short of traffic ideas, it is short of a trial anyone starts. Reaching $10k at the
observed ~$4 blended price needs ~2,500 subscriptions, so the gap is 500×, and at
eight new customers a day no creative test is statistically readable yet.

**What the loop can now see.** `measure/collect_metrics.py` writes one daily row from
RevenueCat, Stripe and the App Store analytics API (Apple builds those reports
asynchronously; the Anicca iOS request already lists 50 available reports). A source
that fails is stored with its error, never as a zero, and a run that reaches no money
source at all exits non-zero. `measure/collect_post_metrics.py` fills the gap that
made scoring impossible: 797 posts in `account-history.jsonl` had `views_6h/24h/48h`
all null. Postiz returns no engagement, and its stored TikTok `releaseURL` is only the
profile (`https://www.tiktok.com/@handle`), which is why `postURLs` answered 400.
The numeric suffix inside `releaseId` is not a native TikTok video ID. The current
adapter therefore refuses to turn it into a direct URL; it must match a public
profile/caption/time readback before a publication receipt is reported. If that
readback is unavailable, the effect remains `unknown` and is never retried as a
new post. TikTok metrics still require provider/profile collection; Instagram uses
its real post URL. First real readings: 557, 189, 74 and 0 views.

**A quarter of the output was disappearing.** The same window shows 106 posts, 83
PUBLISHED and **23 in ERROR state — 22 of them on the "Anicca" TikTok account** — with
nothing anywhere surfacing it.

**Scoring exists but stays silent until it can be honest.** `brain/score.py` ranks
0.6·views + 0.4·engagement (the skill-autoshorts formula) with two guards that
implementation lacks: a post under 24h is not judged, and a cohort under ten posts
produces no verdict at all. Run today: 3 judged posts → `insufficient_data`, no winner
declared.

**Posts were going to accounts that no longer exist (found 2026-07-31).** The TikTok
account behind the "Anicca" integration, `@aniccaen2`, does not exist: Apify answers
"This profile/hashtag does not exist" across three runs while `@aniccajp` returns
videos in the same call, and every "PUBLISHED" post on that integration carries only a
profile URL, never a `/video/` URL. The ten-day pattern is a flat 6 PUBLISHED + 6 ERROR
per day (83/68), so English larry output was not half-failing — it was going nowhere.
Auditing all fifteen enabled TikTok integrations found two more dead targets,
`@anicca.jp8` and `@anicca.jpx`. Six launchd jobs were repointed to verified-alive
accounts (larry-en-v1 to `@aniccaaffirmation`, whose latest video has **253,700 views**
and which no loop was feeding), and `measure/audit_accounts.py` now runs at 06:30,
alerts, and exits non-zero while any target is dead. The three dead integrations still
exist in Postiz and should be deleted or re-provisioned as agent-owned accounts.

**Telegram historically proved three delivery tiers.** A per-post message with the
tappable public URL every 30 minutes (7 real sends, rerun sent 0, proving dedupe), a
daily money digest at 22:00, and a weekly review on Sundays that names accounts to cut
or feed. Raw logs appear in none of them. The current notifier observes zero posts and
sends zero publication messages, so the replacement must alert when an expected slot
is missed. TikTok messages must use a native direct video URL verified by
provider/profile readback, never a profile URL or a numeric `releaseId` suffix alone.

**The ebook can be bought.** The 401 was an expired *test* key; the live key answers
200. Product, a $19 price, a payment link (checkout HTTP 200) and a post-purchase
redirect to a PDF that returns HTTP 200 / application/pdf / 74,424 bytes are live.
Note `dev` deploys only to a Netlify preview, so delivery files must reach `main`.

**Closed on 2026-07-30.**
- `build-from-fixed-strings.sh` now fails at the real cause: a missing background or a
  blank slide text stops the build instead of surfacing three steps later as a node
  ENOENT. Verified by running all three cases.
- `marketing-engine/mine/` mines one niche per day (`mine_daily.sh`, rotating, Apify
  free tier) and `freshness_gate.sh` fails when the newest scrape is older than 48h.
  Verified: gate FAILed before mining, monk-wisdom went 68 → 173 cards, the launchd
  run rotated to honne-relationship (79 → 179) and exited 0.

### 3.2.3 Current control-plane and three-post portfolio contract

This section is the current operational override for two questions only:
whether one loop repair can reload an unrelated loop, and whether every
assigned TikTok, Instagram, and YouTube destination posts three times per day.
It does not authorize an all-label apply or a broad OpenClaw rollback.

**Owner boundary:** this workstream owns only Life Manager mobile-app marketing:
the destination map, Larry/ReelClaw/slideshow assets and routing, Postiz/native
publication, marketing launchd owners, metrics, Telegram, and replay evidence.
Capafy, Connector, Coconala/Gig, Writer, Job Search, and every other business
loop remain separate owner workstreams. Their code, plist, release selection,
state, and schedule MUST NOT be changed by this workstream.

The source/runtime ownership chain is:

```text
GitHub Daisuke134/life-manager main
  -> /Users/anicca/Projects/life-manager-main (development checkout)
  -> /Users/anicca/loops/releases/<timestamp>-<git-sha> (immutable build)
  -> ~/Library/LaunchAgents/<exact-label>.plist (one loaded owner)
  -> ~/.local/state/life-manager/<loop-id> plus durable publication receipts
```

The read-only host inventory contains 183 Anicca/Gig LaunchAgents. Of those,
158 load `20260831T204728-439dc71d`, ten load one of five other immutable
Life Manager releases, and fifteen directly load an external Python, Node,
shell, Go, router, or observability binary. Therefore `loops/current`, the
repository checkout, and every loaded job are not one interchangeable folder.
A loaded job keeps its exact immutable `ProgramArguments` until that exact
label is deliberately applied. Global `current` alone is not deployment proof.

Every change MUST use one named `LIFE_MANAGER_APPLY_TARGET`. Before and after
the apply, the operator MUST record the target label's plist path, immutable
release, Git SHA, entrypoint, state root, runs, and last exit, and MUST prove
that every sibling label retained the same plist mtime and `ProgramArguments`.
Targetless/all-label apply is prohibited. A release is eligible only when it
is built from canonical `origin/main`, passes `lm-loop doctor`, contains all
tracked Git blobs and modes, and has enough capacity to finish atomically.
Rollback means reselecting the target label's exact prior immutable release;
it never means reverting the whole OpenClaw repository or moving all labels.

The machine-readable posting map is `config/marketing-destinations.json`.
Section 8.8.1 is its human-readable contract. Historical OpenClaw commit
`5a2e6093` (`state/POSTIZ_ACCOUNT_MAP.md`) and refresh commit `d86afe75` are
recovery evidence only; production MUST NOT read the OpenClaw map, assets,
credentials, cron, or working tree. The old map is reused to recover proven
handle/integration/creative relationships, not as a second live SSOT.

Twelve current mobile-app TikTok/Instagram target labels load the immutable
release `20260831T204728-439dc71d` and already have exactly three JST calendar
slots per day in their plists and in `config/marketing-destinations.json`.
Browser readback fixes two provider aliases: Postiz `@anicca.affirmation` owns
native Instagram `@anicca.ios`, and Postiz `@anicca.jp1` owns native Instagram
`@anicca.ios.jp`. Instagram `@obou.anicca` belongs only to the separate Japanese
ebook lane and is a 0/day mobile-app hold. Instagram `@anicca.en` describes the
Anicca personalized self-care App Store product and is never an ebook account.
The schedule definition is present, but publication is not healthy: ten labels
have `runs=0` / never exited; EN Card Instagram, EN Slideshow TikTok, and Honne
JA each have one run with exit 1. A plist or calendar is not a post receipt.
The current required volume is 36 verified TikTok/Instagram publications per
day. Connected Anicca TikTok `@aniccaaffirmation` and YouTube `@anicca-ai` are
valid mobile-app identities but remain explicit 0/day TODOs until each exact
creative/owner lane is canaried and installed. The three connected YouTube integrations remain held at 0/day and have no
installed posting owner; they are now in scope for classification, one-canary
verification, and a later ramp to three posts/day each. No YouTube lane may
borrow Honne identity or reuse a creative without an exact product/account map.

Acceptance requires, for each destination independently: expected slots equal
three, provider `PUBLISHED` rows equal three, three direct native URLs bind the
intended handle and exact content, missed and duplicate counts equal zero,
Telegram is naturally deduplicated, and same-slot replay creates zero provider
or message effects. A failure in one destination MUST leave every other label
loaded and scheduled from its prior immutable release.

The fixed execution order and durable checklist for this focused recovery is:

1. [x] Freeze the source/release/label/state inventory and enforce exact-target
   apply plus sibling non-mutation evidence.
2. [x] Reconcile the OpenClaw historical map against the live Postiz registry
   and `config/marketing-destinations.json`; one handle has one product,
   locale, renderer, media form, approved pack, integration, and native owner.
3. [x] Finish MKT-09 classification, pack, canary, native-readback, Telegram,
   metric-registration, and replay-zero gates; `@anicca_buddha` is a truthful
   mixed-account hold, not a guessed Anicca renderer lane.
4. [x] **MKT-10R6:** repair the existing shared Life Manager/Postiz
   production-slot boundary, arm exactly one approved destination, and prove
   one natural scheduled wake through exact Postiz/native account, content,
   caption and slot readback, natural Telegram, metrics registration, and
   replay-zero. Every sibling marketing lane and every non-marketing label
   remains byte-for-byte unchanged.
5. [ ] **ACTIVE — MKT-10R7-PF:** re-audit and restore the mobile-app destinations
   in owner-mandated platform order: every TikTok account first, then every
   Instagram account, then YouTube. Historical Postiz rows, old native URLs,
   loaded labels, or configured CalendarIntervals are evidence only and do not
   prove a fresh post reached the intended native account. Advance exactly one
   account at a time only after fresh native-profile readback, correct pinned-post
   content-family match, one new public post, exact integration, three daily
   CalendarIntervals, and same-slot provider/message replay-zero. The only TikTok
   account currently accepted as freshly verified is `@anicca_buddha`.
6. [x] **SKIPPED/HOLD — MKT-10R8:** TikTok `@anicca.videojp` is frozen as the
   Japanese ReelClaw UGC-video family, but has no exact Postiz integration.
   Keep it manifest-unarmed at 0/day and never borrow another integration;
   resume only after an exact `@anicca.videojp` connection exists.
7. [ ] **BLOCKED BY PLATFORM ORDER:** after every TikTok and Instagram atom below
   is freshly verified, classify and canary the existing YouTube integrations,
   install their exact Life Manager owners, then use the same three/day gate.
8. [ ] Close MKT-11 2h/24h/72h/7d metrics and ASC/RevenueCat/product
   attribution, MKT-12 bounded hook learning, and MKT-13 daily/weekly Telegram
   reports before retiring legacy OpenClaw ownership.

**Incident I-1 implementation.** Life Manager now has a separate
`marketing-liveness` process, independent of the marketing scheduler it
observes. It evaluates only explicitly `production-armed` lanes, walks the
most recent 100 expected local-time slots after each lane's grace period, and
turns each slot into one deterministic `message` job. Disabled, default-off,
and shadow lanes create no job. A success requires the generic publication
adapter's verified receipt plus `provider_reconciled=true`; its Telegram body
uses the direct TikTok `/video/<id>` or Instagram artifact URL. A missing or
unreconciled publication reports `status=missed`, `public URL=unavailable`, and
`retry state=unavailable`, never zero or success. The runtime job/effect key is
the durable notification dedupe boundary, and worker execution rebinds every
reference before resolving the tenant-scoped Telegram token and chat. Fake
transport, replay, off-lane, long-lived schedule, JSONB round-trip, per-lane
receipt ranking, runtime wiring, and independent-service tests pass. The four
changed runtime paths pass the legacy dependency scanner with zero violations.
The repository-wide scanner now passes with zero violations: the Connector
photo transport stages its temporary file beneath the validated Life Manager
data root instead of `.openclaw/media`. The change preserves the provider
transport and removes the last repository-wide legacy-path finding.

### 8.8 Platform and account matrix

Postiz remains the publishing control plane for the recovery. The old
2026-06-03 Postiz map is historical evidence only (it listed 30 integrations);
the live registry is authoritative and must be refreshed by MKT-02. Life
Manager must never read the OpenClaw map, credentials, or assets at runtime.

**Current incident override.** Owner-supplied native TikTok profile screenshots
at 23:10-23:11 invalidate the earlier fleet-level success conclusion. They do
not expose post dates, so they cannot independently prove the reported
one-month duration. They do prove that account/product/locale/form separation
is not healthy: sunset photo-carousel history is mixed with unrelated Japanese
person-video content on `@anicca.jp`, and the same person-video families appear
across Anicca, Honne, affirmation, Buddha, and girl-branded profiles. A Postiz
`PUBLISHED` row, provider analytics, HTTP 200, or a correct canary on one row no
longer closes a destination. Each lane returns to unverified until a current
native-profile readback binds exact platform, native handle, media form, first
frame/media hashes, caption, locale, product, public URL when available, and
slot time.

The live Postiz integration readback and the screenshots currently disagree:

| Native screenshot | Live Postiz integration readback | Current disposition |
|---|---|---|
| TikTok `@anicca.jp` | TikTok `cmp9sdev5012voh0y58qs45xc`, enabled | route corrected to Larry sunset native-photo carousel; remains unverified until natural-owner canary and native readback |
| TikTok `@anicca.videojp` | no matching live integration | missing integration; 0/day |
| TikTok `@honne_reveal` | TikTok `cmoig11ew001zlv0yk6vqo1us`, enabled | connected, but visible cross-persona history requires fresh native audit |
| TikTok `@aniccaaffirmation` | TikTok `cmp93bkpu01uvoh0yd3aj560g`, enabled; LM manifest hold | connected hold; 0/day; never borrow another Anicca lane |
| TikTok `@anicca_buddha` | TikTok `cmp9txjdp01c8oh0yb6dhlarr`, enabled; LM manifest hold | connected hold; 0/day; browser-visible pinned/grid contract is the eyeglass-wearing man at a restaurant, rendered as a six-image Japanese Larry slideshow; sunset, woman, ReelClaw video, and generic Buddha imagery are forbidden |
| TikTok `@anicca.jp1` | no TikTok integration; the same Postiz profile string is Instagram `cmn8ycvtn02djqx0ytuisn9mw` | platform mismatch; TikTok 0/day; Instagram row cannot prove this TikTok account |
| TikTok `@anicca_girl` | no matching live integration | missing integration; 0/day |

The live mode-0600 lane manifest also contradicts the prose and earlier canary
state: it contains thirteen `production-armed=true` lanes at three/day while
this section still says default-off/zero/day. This is a control-plane drift,
not permission to post. Until MKT-10R0 through MKT-10R7 close, the intended
state is fail-closed: no new lane may be armed from the stale canary labels,
and MKT-11 attribution or MKT-12 learning may not treat these lanes as healthy.

External implementation references establish the boundary, not runtime
ownership:

- Source: [Postiz TikTok automation](https://postiz.com/codex/tiktok). Core
  quote: “List your connected accounts and grab your TikTok integration ID.”
  Therefore integration identity is platform-specific and must be inventoried
  before publication.
- Source: [Postiz TikTok scheduler](https://postiz.com/channels/tiktok). Core
  quote: “Postiz supports TikTok videos, single images, and multi-image photo
  carousels.” Therefore video and native photo carousel are distinct forms and
  cannot share a success assertion.
- Source: [Upload-Post Larry marketing skill](https://github.com/Upload-Post/upload-post-larry-marketing-skill).
  Core quote: “generate → overlay → post → track → iterate.” Life Manager keeps
  this loop shape but retains Postiz as its current provider adapter; the skill
  is design evidence, not a runtime checkout dependency.
- Source: [LarryBrain-Skill](https://github.com/OllieWazza/LarryBrain-Skill).
  Core quote: “Skills are plain markdown files that any agent can read and
  follow.” It is a marketplace/bootstrap skill, not the missing Larry publisher
  and not a reason to restore OpenClaw ownership.

| Product/channel | TikTok | Instagram | YouTube | Current truth |
|---|---|---|---|---|
| Honne EN | `@honne_reveal` (`cmoig11ew001zlv0yk6vqo1us`) | **unassigned** — no live Honne Instagram profile in the 2026-08-21 registry | **not used** | TikTok only until an explicit live Instagram assignment exists |
| Honne JA | `@honnevideo` (`cmnit95mg015rrm0ye5vm8dhl`) | **unassigned** — no live Honne Instagram profile in the 2026-08-21 registry | **not used** | TikTok only until an explicit live Instagram assignment exists |
| Anicca JP4 | `@anicca.jp4` (`cmn8x8hdv028uqx0y4gdfse5t`) | not frozen | no dedicated target recorded | one lane at a time |
| Anicca iOS / main JA | `@anicca.jp` (`cmp9sdev5012voh0y58qs45xc`) | `@anicca.jp1` (`cmn8ycvtn02djqx0ytuisn9mw`) | `@anicca-jp` (`cmn1oukj9012nnq0yqhouc3ib`) skipped by owner instruction, LM lane disabled | TikTok/Instagram only; YouTube remains 0/day |
| Anicca EN / main | not in the incident canary | `@anicca.encards` (`cmpc3gx4001nklg0y27a8o66q`) | live candidate `@anicca-ai` (`cmq3u37gi005iqp0y90a2w92n`), LM lane disabled | EN Card Instagram is classified at 0/day; separate pack remains to import |
| Affirmation pack | not an Anicca iOS lane | not an Anicca iOS lane | historical candidate `@anicca-affirmation-video` (`cmn8ymq6c02oio70y5ea1trv8`) | do not mix rewards |

#### 8.8.1 Target account, creative, and cadence contract

This is the intended production portfolio for the two current mobile apps. It
does not arm a lane. The current scheduled cadence is **zero posts per day on
every row** while the incident gates remain open. After that exact row passes a
canary, exact Postiz/local-lineage proof, replay, and metric-source gate, Life Manager may arm
only the target cadence shown here. The daily limit is per destination account,
not a command to fan every creative out to every connected Postiz integration.

Owner-directed API success rule: for these selected Postiz routes, publication
success is the exact Postiz API row in `PUBLISHED` state plus matching LM
account, integration, platform, creative, locally stored media bytes/order, and
caption lineage. A direct native URL is retained when available and useful, but
TikTok indexing, oEmbed, browser login, or a caption-bearing native page is not
a publication blocker and is never required before continuing other executable
work. Postiz profile URLs or numeric suffixes alone still do not prove identity;
they pass only as fields on the already exact row bound to the LM effect.

| Product / locale | Platform and exact account | Integration | Creative that belongs there | Target cadence after that row is healthy | Current state |
|---|---|---|---|---:|---|
| Honne EN | TikTok `@honne_reveal` | `cmoig11ew001zlv0yk6vqo1us` | `reelclaw` 9:16 relationship-confession video: a short message/situation followed by the honest meaning, English captions, app CTA | 3/day | verified metric source; scheduled 0/day, default-off during recovery |
| Honne JA | TikTok `@honnevideo` | `cmnit95mg015rrm0ye5vm8dhl` | `reelclaw` 9:16 relationship-confession video in Japanese; not an Anicca affirmation/card creative | 3/day | verified metric source; scheduled 0/day, default-off during recovery |
| Anicca iOS JA main | TikTok `@anicca.jp` | `cmp9sdev5012voh0y58qs45xc` | Larry sunset six-image native photo carousel from exact pack `anicca-ios-larry-sunset-ja.pack.json`; never a ReelClaw/person MP4 | 3/day | incident route corrected; scheduled 0/day/default-off until MKT-10R6 natural canary, native readback, Telegram, metrics registration, and replay-zero |
| Anicca iOS JA main | Instagram Postiz alias `@anicca.jp1`, native owner `@anicca.ios.jp` | `cmn8ycvtn02djqx0ytuisn9mw` | one selected Anicca JA nudge-card MP4 as a Reel; the same creative lineage may be reused across platforms but has its own publication effect | 3/day at 08:10, 13:10, and 19:10 JST | exact Reel `Dce7_IPlUlr`, natural Telegram `34651`, and replay 0 verified; scheduled 0/day/default-off until metric discovery closes |
| Anicca iOS JA expansion | TikTok `@anicca.jp4` | `cmn8x8hdv028uqx0y4gdfse5t` | Anicca JA `reelclaw-card` / `nudge-card` slideshow-style MP4 from the approved card pack | 3/day | verified repeatable 24h metric source; scheduled 0/day, default-off during recovery |
| Anicca iOS JA expansion | TikTok `@anicca.he` | `cmq2aoena08bhqp0yx1epjcik` | Anicca JA `reelclaw-card` / `nudge-card` slideshow-style MP4 from the approved card pack | 3/day | recovered effect plus verified repeatable 24h metric source; scheduled 0/day, default-off during recovery |
| Anicca iOS JA Larry | Instagram `@ani.cca1234` | `cmq3sq7mc000eqp0y7azfm8yk` | Larry JA v1 six-image native photo carousel: one fixed Japanese hook plus five Japanese body slides, JPEG for Instagram; not an MP4 Reel | 3/day at 10:30, 16:30, and 22:30 JST | direct native canary, natural Telegram, replay, and metric source verified; scheduled 0/day, default-off |
| Anicca iOS JA Widget | Instagram `@anicca.jp.videos` | `cmmzzg2es0539p30ycb94ayx0` | Japanese `reelclaw-widget` / `widget-demo-reel`: a Japanese hook followed by the iPhone lock-screen Widget installation flow and visible Anicca widget; never the separate affirmation/Card sequence historically mixed into this account | 3/day at 08:05, 13:05, and 18:20 JST | direct native canary `DcetvubDA4Z`, natural Telegram, replay, and immediate metric source verified; scheduled 0/day/default-off |
| Anicca iOS EN Card | Instagram `@anicca.encards` | `cmpc3gx4001nklg0y27a8o66q` | English `reelclaw-card` / `nudge-card` 9:16 MP4 Reel from LM pack `anicca-ios-reelclaw-card-en.pack.json`; hook is bound to its exact approved media; never JA or Larry copy | 3/day at 08:45, 12:45, and 21:30 JST | Postiz exact `PUBLISHED` row, natural Telegram, replay 0, and metric owner verified; production-armed |
| Anicca iOS EN Widget | Instagram `@anicca.en` | `cmn8y95rg02d2qx0y09bbk5pb` | English `reelclaw-widget` / `widget-demo-reel`: English hook followed by the lock-screen Widget installation flow; never Card EN or JA copy | 3/day at 07:30, 09:30, and 19:00 JST | direct native canary `DcekGtmjmOf`, natural Telegram, replay, and immediate metric source verified; scheduled 0/day/default-off |
| Anicca iOS EN Affirmation | Instagram Postiz alias `@anicca.affirmation`, native owner `@anicca.ios` | `cmp9pedr700ttqh0yj8o57fog` | Larry-style six-image native English affirmation carousel: one hook plus five mental-health affirmation slides; never Card/Widget video | 3/day at 10:00, 15:00, and 20:00 JST | exact six-slide pack/approval and canary are verified; scheduled 0/day/default-off pending cadence arm |

**Browser-fixed-post routing audit.** Direct browser inspection of the native
profiles confirms the current content-family routing for all ten Anicca mobile
lanes: `@anicca.ios` = EN affirmation, `@anicca.encards` = EN Card,
`@anicca_slideshow` = EN slideshow, `@anicca.en` = EN Widget,
`@anicca.he` / `@anicca.ios.jp` / `@anicca.jp4` = JA nudge Card,
`@ani.cca1234` = JA Larry carousel, `@anicca.jp` = JA sunset Larry, and
`@anicca.jp.videos` = JA Widget. Postiz alias `@anicca.affirmation` continues
to resolve only to native `@anicca.ios`; Postiz alias `@anicca.jp1` continues
to resolve only to native Instagram `@anicca.ios.jp`. `@anicca.jp.videos`
matches the Widget family, but its fixed posts use a different actor asset from
the current approved pack; keep it at 0/day until that asset-level difference is
resolved. Recovery order remains one lane at a time: provider `PUBLISHED`, exact
native owner, browser-visible matching new post, and same-slot replay zero for
all three natural daily slots before the next lane is armed. Ebook/monk remains
0/day until every Anicca and Honne lane is terminally verified.
| Anicca iOS EN Slideshow | TikTok `@anicca_slideshow` | `cmnenjkff01j1pa0ysufmzhfr` | six-image native English mental-health carousel from the exact approved LM pack; never an MP4 or a Card/Widget creative | 3/day at 09:00, 15:00, and 21:00 JST | exact Postiz `PUBLISHED` row, local ordered assets/caption, natural Telegram `34998`, and replay 0 verified; metrics-owner closure remains active |
| Anicca iOS EN Affirmation | TikTok `@aniccaaffirmation` | `cmp93bkpu01uvoh0yd3aj560g` | English affirmation video/carousel only; never Honne, Buddha, girl, or Japanese person-video content | 0/day until canary | native browser readback proves the Anicca App Store identity; connected but no retained Life Manager owner yet, so activation remains TODO |
| Anicca iOS EN main | YouTube Shorts `@anicca-ai` | `cmq3u37gi005iqp0y90a2w92n` | English Anicca self-care/affirmation Shorts only | 0/day until canary | native browser readback proves the Anicca App Store identity; exact three/day owner remains TODO |
| Anicca iOS JA main | YouTube Shorts `@anicca-jp` | `cmn1oukj9012nnq0yqhouc3ib` | one selected Anicca JA nudge-card MP4 per day, with a Shorts title/description and the same immutable creative/campaign lineage | 0/day | **skipped by owner instruction** after the exact canary effect ended absent and channel ownership could not be refreshed; LM-disabled |

#### 8.8.2 End-state operating contract

The incident is finished only when every healthy **existing linked** row above
is actually posting at its target cadence, not merely connected or enabled in
Postiz. API inventory proves Honne has TikTok EN/JA routes but no Instagram or
YouTube integration. Under the owner-directed API-only policy those missing
routes remain explicit `unavailable/missing_integration`, not blockers and not
borrowed Anicca accounts. If they are linked externally later, each begins with
one verified canary and ramps to the same three/day contract after
URL/Telegram/replay/metrics.
The owner-skipped Anicca `@anicca-jp` YouTube lane stays 0/day. Every other
connected integration is classified into this table or retained as an explicit
0/day hold; mixed-product accounts are never used to inflate coverage.

For every selected healthy destination, the durable owner must run continuously
and produce exactly three scheduled publication opportunities every local day.
For every scheduled post, the durable loop must produce the exact native URL
(or the approved exact Postiz/local-object proof for a TikTok photo carousel),
one natural publication receipt, 2h/24h/72h/7d plus daily/weekly metrics, and an
ASC/RevenueCat/product attribution status. Hook learning changes one hook only,
keeps format/CTA/account fixed, and proves the next generated post consumed the
keep/revert decision. This is the reusable open-source mobile-app marketing
loop required before a new app is onboarded. The marketing loop is not done
until this three/day publication, measurement, learning, and reporting cycle is
owned by Life Manager 24/7 for every selected destination with missed and
duplicate effects surfaced naturally instead of silently ignored.

Honne has no assigned Instagram account and no YouTube route. Do not create a
Honne Instagram/YouTube effect by borrowing an Anicca account. Native Larry
photo/carousel slideshows are also **not** in the armed portfolio yet. Life
Manager now has one imported, account-bound Larry JA pack that passed its
six-frame render audit plus a dedicated Postiz carousel publication contract.
It has not passed the real provider effect, direct-native-artifact, replay,
Telegram, or metric-source canary gates.

Every other live Postiz TikTok/Instagram/YouTube integration has a target
cadence of **0/day** until a later atomic classification binds it to a product,
locale, renderer, approved pack, campaign, and metric source. This explicit hold
includes:

- TikTok: `@aniccaaffirmation`, `@aniccaen2` (measured dead),
  `@monk_anicca` (provider-disabled), `@anicca.daily`, `@anicca_slideshow`,
  `@aniccajp`, `@anicca.jp8` (measured dead), `@aniccajp2`, `@anicca.jpx`
  (measured dead), `@obou_anicca`, `@anicca_buddha`, and `@anicca.comedy`.
- Instagram: `@anicca.affirmation`, `@obou.anicca`, and `@anicca.bochi`.
- YouTube: `@anicca-ai` and `@anicca-affirmation-video`.

An enabled Postiz connection in this hold list is routing configuration only.
It is not permission to publish. `@aniccaaffirmation` is a high-value research
candidate because the historical audit observed a 253,700-view video, but it
stays at 0/day until its separate English affirmation product/pack and app
attribution are proven; that evidence cannot be silently credited to the
Anicca iOS JA loop.

The three YouTube handles above are live Postiz profiles read back on 2026-08-21;
their current Postiz `disabled` value is `false`, but every Life Manager lane
is explicitly `disabled` until the direct-URL contract. They are not a
production assignment. YouTube is an **Anicca-only**
destination in this recovery. Honne has no YouTube job, account, campaign,
metric gate, or TODO; do not create or assign one. An Anicca YouTube lane stays
disabled until its live Postiz integration, product, locale, account, and direct
public URL are all recorded in the Life Manager manifest. A shared Anicca
YouTube channel cannot silently serve two product packs; if it is intentionally
shared, its campaign and reward joins must still remain product-scoped.

Postiz is retained for Honne TikTok/Instagram and Anicca
TikTok/Instagram/YouTube. The generic publication contract now accepts
`instagram`, `tiktok`, and Anicca-only `youtube`; YouTube uses its own Postiz
integration reference and accepts only a verified direct URL (`/shorts/<id>` or
`/watch?v=<id>`). YouTube remains disabled in the lane manifest until the
controlled provider canary; no Honne YouTube route is permitted.

**MKT-02 cursor (2026-08-21 JST — complete for the measured assignments).**
The Life Manager-side registry boundary is implemented as a secret-free
deterministic manifest normalizer and atomic writer. A read-only
`GET /public/v1/integrations` with the authorized credential returned `HTTP 200`
and 29 rows. The frozen artifact is
`~/.local/state/life-manager/marketing/lane-manifest.json`, mode `0600`, with
manifest ID
`marketing-lane-manifest:9867179bbb8db1cbd434800562a92c40935b353789c2c60de4027dba9895790c`
and eight explicit routes: four target TikTok routes, two evidence-backed
Anicca Instagram routes, and two Anicca YouTube candidates held explicitly
disabled. All eight are `production_armed=false`; no provider write occurred.
The live registry contains no Honne Instagram profile, so Honne Instagram is
explicitly **unassigned**, not guessed or enabled. The artifact contains no
credential, OpenClaw path, or raw provider payload. The next gate is MKT-03's
single Honne EN TikTok canary; the Postiz credential remains outside Git and
must never be copied into the manifest.

**MKT-03 preflight (2026-08-21 JST — superseded by the controlled canary).**
The read-only preflight records two external blockers and one local lineage
precondition that is now satisfied:

1. `apps/life-manager/scripts/honne-en-canary.js` now has an explicit
   `LM_HONNE_EN_CANARY_TRANSPORT=postiz` path, but it requires the exact
   `PROMOTE_HONNE_EN_TIKTOK_CANARY` confirmation and performs a claim-before-
   effect preflight. Fake receipts remain test-only and cannot satisfy the
   real-publication acceptance gate.
2. A one-time, value-redacted migration now provisions only
   `LM_TELEGRAM_BOT_TOKEN`, `LM_TELEGRAM_ALERT_CHAT_ID`, and
   `LM_POSTIZ_API_KEY` in `~/.local/state/life-manager/.env` (mode `0600`).
   The source files remain untouched and are never passed to the Life Manager
   child process. Redacted Telegram `getMe` returned `ok=true` with bot
   username `AniccaLifeBot`; no token or chat value is recorded here.
3. The Life Manager-owned marketing data root now contains one tenant-scoped
   Honne EN candidate, still held in shadow: pack
   `object://sha256/adcdebe26b73d71911b0e89eab9dfb3e4e7155cc976841d339fbf1ac9df7aa3c`,
   verified 1080x1920/12-second H.264 media
   `object://sha256/cfbc17642a4df899e2c86169bdc5dc69ada3ab11181a1b0a8c67ba5a906b1aad`,
   copy object `object://sha256/ad83d11dc0b73aeecb842d7ea589c9572494ce82cd2339df3ea2f66c707448a8`,
   and a job-scoped approval object
   `object://sha256/8ce90da6656db6ed37ed774b17b4bb4a6be9712eb8f093f3065e18dd3abddc15`.
   The ready generation receipt is for `honne-ai`/`en`, creative
   `HEN-001-cfbc17642a4d`, slot `2026-08-22T02:00:00.000Z`; its TikTok
   publication job is queued at the explicit shadow-hold sentinel with the
   `@honne_reveal` integration. Replaying both inputs returned `created=false`
   and appended zero ledger rows. The approval object's basis is the existing
   standing-policy record; it is not evidence of an external publication.
   The Postiz credential alone does not create these lineage inputs. The real
   path still refuses a job unless its video, caption, and approval object refs
   resolve and its Instagram profile ref is the explicit
   `profile://instagram/unassigned` sentinel. Its Postiz executor is
   module-owned, its distribution child process receives only the explicit
   non-secret environment allowlist, and any publication receipt plus final
   redirect must remain on `@honne_reveal` with the same numeric video id.

The first canary attempt stopped before provider execution because the
distribution adapter did not recognise the existing
`approval_mode=standing_policy_no_additional_gate` object. The adapter now
accepts that explicit standing-policy shape (including a single JSON approval
object as well as JSONL) and the 37 existing distribution tests pass. The
failed attempt was reconciled as `absent` after a read-only Postiz target
search returned no row and no local publication ledger row; its JSONL backup is
retained outside Git.

**MKT-07 provider recheck (2026-08-21 JST).** The failed JP4 effect remains
terminal and is not retried. A fresh read-only Postiz detail fetch confirms the
provider-owned error is `postSocialPending` `START_TO_CLOSE` timeout with
`MAXIMUM_ATTEMPTS_REACHED`, and the row has neither `releaseId` nor
`releaseURL`. The same integration answers Postiz analytics successfully, so
the failure is publication-path/account state rather than a Life Manager
asset, caption, or lineage mismatch. Postiz has no public API endpoint that
completes a reconnect; its public OAuth route
(`GET /public/v1/social/tiktok?refresh=cmn8x8hdv028uqx0y4gdfse5t`) generated a
fresh TikTok authorization URL, but the existing browser session reached the
TikTok `/login` page. No password, MFA, provider write, or old OpenClaw job was
used. MKT-07 is therefore **blocked pending one owner-completed TikTok OAuth
reconnect for `@anicca.jp4`**, after which a new effect (never the failed one)
may be canaried and reconciled.

**MKT-03 controlled canary (2026-08-21 JST — quarantined historical attempt).**
One and only one Honne EN TikTok job was promoted from the shadow sentinel.
Postiz returned a reconciled `PUBLISHED` receipt for
`https://www.tiktok.com/@honne_reveal/video/7676366077437233172`, and a direct
HTTP readback returned `200` with the same account and numeric video ID. The
natural-language Telegram receipt used the same URL and returned
`message_id=27226`; its payload includes product, locale, platform, slot,
status, public URL, and retry state. The canary process replay returned
`publication_replay_created=false`, `telegram_created=false`, and
`telegram_replay_created=false`; a second process replay returned the same
message ID with all three duplicate flags false. However, the creative was a
generic Life Manager 12-second object rather than the migrated `honne-en`
ReelClaw pack. The row, receipt, and replay evidence remain preserved as
rollback evidence, but this attempt does not satisfy the approved-pack gate;
the next action is a new effect using the imported pack. No other lane was
enabled and no OpenClaw or legacy launchd job was touched.

**MKT-03 approved-pack retry (2026-08-21 JST — reconciled complete).** A
new generation was created from the migrated `honne-en` ReelClaw pack, not the
quarantined wake/demo object: creative `HEN-005-154a1508e0a8`, video object
`object://sha256/154a1508e0a869be1cd4f22dae729b8e7760940790cfc8d2e4a592b5bf67d36e`,
and caption object
`object://sha256/a8bfa3ab15744007138511e1738587b9e8bda3485a5f2c2feb49ba1909c31262`.
The job was promoted once through the Life Manager local ledger. Postiz then
created provider row `cmt2nc8gj00x5ph0yodvxq4dm` for integration
`cmoig11ew001zlv0yk6vqo1us` with state `PUBLISHED`, but exposed only the
profile URL `https://www.tiktok.com/@honne_reveal` and internal
`releaseId=v_pub_file~v2-1.7676386997019920404`. The public
profile/caption/time resolver initially could not produce a native URL, so the
local job correctly ended `unknown_effect=true` without retrying. A read-only
Chrome profile readback then matched the exact caption to
`https://www.tiktok.com/@honne_reveal/video/7676388327427149077`, which differs
from the Postiz suffix. The local ledger reconciled the same effect key as
`present`; the publication receipt carries `provider_reconciled=true`, and
Telegram sent one natural-language receipt with `message_id=27358`. The first
replay and a second process replay both created zero publication or Telegram
effects. The old generic canary remains quarantined evidence; no new provider
write was made during reconciliation.

The direct-URL resolver now keeps the yt-dlp path but falls back to the existing
CDP browser profile DOM when TikTok's JavaScript page defeats yt-dlp. It matches
the normalized caption against the newest profile links and still accepts only a
native `/video/<id>` URL. The fallback was read-only verified against the same
Honne EN caption and returned `7676388327427149077`; it never treats the Postiz
`releaseId` suffix as the provider ID.

**MKT-03A YouTube shadow contract (2026-08-21 JST — complete).** The generic
Life Manager publication adapter now accepts an Anicca-only YouTube platform
job with a platform-scoped `integration://postiz/youtube/...` reference. Receipt
verification accepts only direct `https://www.youtube.com/shorts/<id>` or
`https://www.youtube.com/watch?v=<id>` URLs and rejects a channel/profile URL.
The shadow plan creates one YouTube job and invokes no provider transport; the
existing Instagram/TikTok job reference shape remains backward compatible.
Both live Postiz YouTube candidates remain explicitly `disabled` and no
provider write or scheduler change occurred. MKT-03B is the first provider
canary and remains open.

**MKT-03B asset/direct-URL correction (2026-08-21 JST).** The first Anicca
fan-out attempt used the Life Manager wake/demo object rather than the approved
Larry/ReelClaw mobile pack. The TikTok row is visible at
`https://www.tiktok.com/@anicca.jp/video/7676379526930156821`; its thumbnail is
the same FaceTime wake render, so it is quarantined evidence and not an
accepted Anicca iOS canary. Postiz returned an internal release id ending in
`7676378097071163413`, which is **not** the native TikTok video id; the native
URL was found by public profile/caption/time readback. The adapter now refuses
to treat a numeric `releaseId` as a verified public URL and must resolve the
provider profile before recording a direct TikTok receipt. The remaining MKT-03B
canary must use the migrated approved pack and a native direct URL; no retry of
the quarantined creative is allowed.

**Wrong-content root cause and permanent guard.** The Anicca canary's Postiz
account and API route were correct; Life Manager supplied the wrong bytes. The
generic canary accepted `format_id=anicca-wake` because the earlier generation
and publication contracts checked hashes and provider references but did not
enforce the product's approved format family. That let the Life Manager
`lm_wake_JA` demo pass the provider boundary. The guard is now fail-closed in
both adapters: Anicca accepts only Larry/ReelClaw families, Honne accepts only
the migrated Honne ReelClaw family, and unknown formats are rejected before any
Postiz call. Runtime inputs are LM object refs only; every future lane must
pass the same one-lane canary, native URL, Telegram, replay, and seven-cycle
gates. The original wrong Anicca row remains quarantine evidence and is never
reused.

**Telegram binding discovery (2026-08-21 JST — transport and LM ownership
complete for I-3).** The canonical job-search loop remains the existing working reference for
the natural-language/report transport: `apps/job-search-loop/job_search_loop/telegram.py`
calls the Telegram Bot API directly, keeps an at-most-once SQLite outbox, and
reads only the private job-search configuration at
`~/.config/anicca/job-search/telegram.env` (mode `0600`, keys
`TELEGRAM_BOT_TOKEN` and `JOB_SEARCH_TELEGRAM_CHAT_ID`). Its durable evidence
contains successful receipts with message IDs `4421` and `26925`; replay reads
the same outbox and does not resend. This proves that the other loop's transport
is healthy, not that its bot/chat credential belongs to Life Manager.

No transport copy is needed: Life Manager already owns the equivalent direct
Bot API helper at `apps/life-manager/lib/telegram.js` and the durable
`marketing-liveness-adapter.js` path that renders the required natural-language
receipt and validates the provider message ID. Copying job-search code would
create a second sender, not remove the binding blocker.

The shared shell helper `skills/_shared/send-telegram.sh` remains out of the
runtime because its fallback reads `$HOME/.openclaw/.env`. Life Manager imports
no OpenClaw folder/env/assets at runtime and does not read the job-search env
after the one-time migration. The LM-owned env contains only the three required
secret names, is mode `0600`, and is excluded from Git, ledgers, logs, and this
spec. The canary above proves the direct Bot API transport, receipt binding,
and replay dedupe without exposing any credential value.

### 8.9 Telegram marketing reporting contract

The ledger is machine-readable; Telegram is always a natural-language
projection of that ledger. Raw JSON, `label exit=N`, raw log tails, and a bare
profile URL are not reports. Every message names the product, locale, platform,
account, expected slot, observation window, status, retry state, and source
health. The three tiers are:

| Tier | Cadence | Required content |
|---|---|---|
| Publication/incident receipt | immediately after reconciliation or a missed slot | product + locale + platform + account + slot; `published`/`missed`; direct public URL or `unavailable`; provider post ID; retry state; campaign ID |
| App/account digest | daily | every production-armed lane, expected/published/missed/duplicate counts, social metrics, attributed installs, activation, trials, paid users, proceeds/MRR, and unavailable sources |
| Portfolio review | weekly | Honne and Anicca separately; platform/account winners and failures, attribution coverage, retention/revenue movement, source failures, one keep/revert decision, and the next bounded change |

The natural-language message is generated from a deterministic snapshot hash so
the panel and Telegram can be reconciled. `unavailable`, `partial`, and
`conflict` are written as words with their source/error; they are never printed
as zero. A rerun with the same snapshot and effect key sends no duplicate
message. A production message must identify itself as `Life Manager:::` and
must not expose secrets or OpenClaw paths.

**2026-08-21 legacy-report observation.** The owner-visible line beginning
`📤 user6721125412040 · tiktok` with the caption fragment and
`https://www.tiktok.com/@honne_reveal` is not a Life Manager receipt. It is
the legacy `marketing-engine/report/notify_posts.py` projection: it uses the
Postiz integration name as the account label and forwards the provider's
profile-only `releaseURL`, so it is neither natural language nor a verified
publication link. The corresponding
`ai.anicca.marketing-post-notify` LaunchAgent was loaded but inactive with
`StartInterval=1800`; its prior log contains raw-notifier sends. On
2026-08-21 JST it was explicitly disabled through
`bin/launchctl-safe disable gui/501/ai.anicca.marketing-post-notify` after the
control-plane preflight passed. No kickstart, stop, restart, bootout, or
deletion was performed; the plist and logs remain rollback evidence. Life
Manager's shared Telegram renderer produces a sentence with
product/locale/platform/account/slot, explicit status, `Public URL`, and
`Retry`, and its regression test rejects that raw legacy shape. The full
MKT-11A daily/weekly snapshot projection remains open; this closes only the
per-post renderer boundary and quarantines the raw legacy sender.

### 8.10 Social metrics, app metrics, and attribution

Every observation row has the immutable join keys
`product_id`, `locale`, `platform`, `account_id`, `integration_id`,
`provider_post_id`, `public_url`, `campaign_id`, `creative_id`, `slot`,
`observed_at`, and `window`. The metric value also carries `source`, `status`,
and `error`.

| Surface | Metrics collected when the provider exposes them |
|---|---|
| TikTok | delivery, views, hold/completion, likes, comments, saves/shares, profile/link clicks |
| Instagram | delivery, reach/plays, likes, comments, saves/shares, profile/link clicks |
| YouTube | delivery, views, watch time/average duration, likes, comments, subscribers, link clicks |
| App | store clicks, installs, activation, trial start, paid receipt, renewal, proceeds, monthly-equivalent MRR |

The attribution chain is:

`publication URL → platform/account → campaign link → store click → install → activation → trial → paid receipt → renewal`.

The campaign link layer uses the existing Apple `pt`/`ct` contract or an
equivalent first-party smart-link contract. A TikTok spike is not treated as
causal merely because installs rose on the same day. Reports show verified
attributed installs, partial/unattributed installs, attribution coverage, and
the observation window separately. RevenueCat/App Store Connect/product
analytics are the financial and app truth; Postiz is the social publication and
post-metric source. Missing provider data remains `null`/unavailable.

### 8.11 OSS reference research (learn first, copy only after license review)

The following repositories are reference material, not new runtime dependencies:

| Repository | Useful pattern | Decision |
|---|---|---|
| [`gitroomhq/postiz-app`](https://github.com/gitroomhq/postiz-app) (AGPL-3.0) | scheduler, provider adapters, analytics, self-hosted/hosted parity | keep using Postiz API; do not vendor-copy into Life Manager without AGPL review |
| [`gitroomhq/postiz-agent`](https://github.com/gitroomhq/postiz-agent) (AGPL-3.0) | integration discovery, YouTube/TikTok/Instagram settings, post and analytics commands | copy the discovery/checklist ideas only; Life Manager keeps its own local ledger |
| [`grovs-io/grovs-iOS`](https://github.com/grovs-io/grovs-iOS) (MIT) | iOS smart links, deferred deep links, StoreKit 2 revenue attribution | evaluate for the campaign-link join; do not add before the incident canary |
| [`zmsp/AppRankly`](https://github.com/zmsp/AppRankly) (AGPL-3.0) | App Store/Play installs, retention, ASO and report vocabulary | study metric definitions only; its Docker/database deployment is not the local recovery runtime |
| [`nowork-studio/NotFair`](https://github.com/nowork-studio/NotFair) (MIT) | measured baseline, one action per observation window, rollback and local receipts | reuse the bounded-learning pattern, not its agent runtime |
| [`loopmark-opensource/loopmark-agent`](https://github.com/loopmark-opensource/loopmark-agent) (MIT) | content/funnel decomposition | reference only; it does not provide reliable social-follower analytics |

OSS adoption must preserve Life Manager's local-first boundary, product/locale
isolation, truthful unavailable states, and the no-mass-reenable incident gate.

**Ordered remainder for this engine** (mirrors the harness task list):

| # | Work | Gate | State |
|---|---|---|---|
| 1 | Hook-variation gate + background pool | a hook repeated inside the exclusion window cannot render | **done** — the gate skipped the pinned hook on a real run and picked the next pooled one; background rotation is tested but enabled per variant via `bg_pool` |
| 2 | Per-account attribution (Apple `pt`/`ct` campaign links) | one publication URL joined to installs and then to a paid event | **link layer done** — three accounts hold store-verified links (http=200), a bogus app id is refused; the install/paid join is unproven until posts run |
| 3 | Repair the ebook checkout | one real purchase reaches the ledger | the 401 was the **expired test key**; the live key answers 200. Product, $19 price, payment link and a post-purchase PDF redirect now exist — the remaining gap is traffic, not plumbing |
| 4 | Three-tier Telegram reporting | every publication/miss, daily app/account digest, and weekly portfolio review is natural language and names every ERROR-state post | open — replace `label exit=N` and raw log tails with the §8.9 snapshot projection |
| 5 | Put each account's campaign link in its bio | link visible on IG/TikTok/YouTube profiles | open |
| 6 | Reward scoring at 2h/24h/72h/7–35d, then kill/scale rules | a daily record of what was killed and what was scaled | **partly done** — engagement-depth scoring runs daily and refuses tiny cohorts; install/paid depth waits on Apple's report data |
| 7 | Apply the same variation gate + fresh hook pool to ReelClaw video | `hookPool-ja.txt` (static since 2026-03-17) is replaced by mined hooks under the gate | open |
| 8 | Point `clip` at `load_manifest.sh` + `poster.py` | one posting path for every loop | open |
| 9 | Product packs for aniccaios / honne / ebook EN / ebook JA | a new product runs with a manifest and zero engine edits | open |
| 10 | Scale accounts 1 → 5 → 50 at 2–3 posts/day | 10–20 posts/day per product, warmup respected | blocked on 4 and 6 — volume before measurement reproduces the 10M-views/5-signups failure |
| 11 | Keep Postiz as the primary provider while evaluating optional local replacement | selected Instagram/TikTok/YouTube lanes publish with verified provider URLs and metrics | **deferred** — no Postiz exit is part of incident recovery; any later replacement needs a new parity/cutover gate |

## 9. `$10k MRR` operating model

No verified product-level subscription baseline is imported into Life Manager.
The plan therefore treats the figures below as target mechanics, never as a
claim about Anicca or Honne revenue.

Twelve-month scenarios:

| Scenario | Blended MRR / active subscriber | Install→paid | Monthly churn | Active subscribers needed | New paid/month needed | Installs/month needed |
|---|---:|---:|---:|---:|---:|---:|
| Best | $8.00 | 5.0% | 5% | 1,250 | 136 | 2,714 |
| Base | $6.00 | 3.0% | 8% | 1,667 | 211 | 7,021 |
| Worst | $4.17 | 1.5% | 12% | 2,399 | 367 | 24,449 |

These are target mechanics, not forecasts. The first growth gate is not
“post more”; it is:

1. Restore trustworthy per-product install and revenue collection.
2. Establish one complete attribution chain from publication to paid.
3. Raise the number of genuinely distinct tested hooks per week.
4. Improve the weakest measured funnel step.
5. Scale publishing only after a challenger beats its product-specific
   baseline without retention or revenue regression.

Honne and Anicca are reported as separate products first:

`MRR_product = active paid monthly-equivalent receipts × net monthly ARPU`.

The portfolio total is the sum of verified product receipts. A target of
`$10k` combined is different from `$10k` for Honne plus `$10k` for Anicca; the
latter is a `$20k` portfolio target. Social views and likes are acquisition
signals, not MRR, and a TikTok/Instagram/YouTube spike is not credited to a
product until the campaign-to-install-to-paid join is verified.

## 10. To-Be Life Manager UI/UX

### 10.1 Navigation

```text
┌──────────────────────┬───────────────────────────────────────────────┐
│ LIFE MANAGER         │ TODAY                                         │
│                      │                                               │
│ ● Today              │ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ │
│ Health               │ │Finance │ │Physical│ │Mental  │ │Income  │ │
│   Financial          │ │72 ▲4   │ │64 ▼3   │ │78 ▲6  │ │59 ▲2  │ │
│   Physical           │ └────────┘ └────────┘ └────────┘ └────────┘ │
│   Mental             │                                               │
│ Income Loops         │ Net worth / runway / profit / MRR             │
│   Portfolio          │ Business contribution and mobile funnel       │
│   Mobile Apps        │                                               │
│   Web Apps           │ ┌───────────────────────────────────────────┐ │
│   Gig/Affiliate      │ │ Attention                                 │ │
│   Capafy/Writer      │ │ ASC stale · IG authorization · 1 blocked │ │
│   Crypto/Bounty      │ └───────────────────────────────────────────┘ │
│ Marketing Studio     │                                               │
│ Reports              │ Recent actions · receipts · source health     │
│ Connections          │                                               │
│ Settings             │ Runtime: Cloud  [Switch/export to Local]      │
└──────────────────────┴───────────────────────────────────────────────┘
```

### 10.2 Today

The first screen answers three questions:

1. Am I financially healthier than yesterday?
2. What made or lost money?
3. What did Life Manager do, and what needs attention?

Top cards:

| Card | Content |
|---|---|
| Net worth | current value, day/month delta, freshness |
| Income | today and month-to-date |
| Operating profit | revenue minus attributable costs |
| Recurring revenue | total MRR and target progress |
| Attention | failed connector, anomaly, blocked job, required authorization |

Below the cards: business contribution, mobile growth funnel, recent autonomous
actions, and source-health strip.

### 10.3 Financial Health

Four tabs:

| Tab | Contents |
|---|---|
| Overview | net worth, liquid cash, runway, income, expenses, profit |
| Businesses | P&L and MRR by product/business |
| Assets | bank, NISA, crypto, yield positions, liabilities |
| Ledger | reconciled transactions, classifications, provenance, freshness |

All numbers show source and last sync. Unknown values display “Unavailable”,
never `0`.

### 10.4 Mobile Apps

Portfolio row per app:

`installs → activation → paywall → trial → paid → retained → MRR`

App detail includes acquisition by channel/creative, store conversion,
subscription cohorts, experiment history, content calendar, and the current
bottleneck. Anicca and Honne never share learned weights.

### 10.5 Marketing Studio

| Area | User sees |
|---|---|
| Content | published artifact thumbnail/title plus the raw TikTok, Instagram, or YouTube URL |
| Queue | planned, rendering, scheduled, published, observing |
| Format library | source, format DNA, product fit, recent usage, performance |
| Learning audit | changed rule, blame evidence, canary, keep/revert, consumption proof; read-only and autonomous |
| Accounts | platform health, last successful post, rate/quality limits |

There are no user-facing experiment buttons and no public artifact page. The UI
exposes receipts, not agent narration. A green state requires a real platform
URL or verified metric row.

### 10.6 Connections and deployment

Users connect App Store Connect with an API key, social platforms with
OAuth/session adapters, and banking through the deployment-owned Moneytree
adapter defined in section 6.5.1. Raw passwords are not a product interface.

| Mode | User experience | Availability |
|---|---|---|
| Cloud, recommended | sign in from phone/web, authorize connectors, Life Manager remains online without a personal computer | managed multi-tenant services and autoscaled workers |
| Local/self-hosted | install Life Manager, sign in with the owner's ChatGPT/Codex subscription, connect the Moneytree plugin once, and use localhost/PWA or Telegram | runs only while that machine is online; does not wait for the Life Manager Moneytree LINK contract |
| Local→Cloud migration | export an encrypted tenant bundle, import it to cloud, reconcile cursors and receipts, then switch scheduler ownership | no loop runs from both schedulers during cutover |

The panel shows the active deployment, scheduler owner, last successful cycle,
and whether turning off the current machine would stop work. Mac Codex commands
use `life-manager` CLI or the authenticated API in either mode.

## 11. Failure handling

| Failure | Behavior |
|---|---|
| Source delayed | retain last good value, mark stale, exclude from fresh delta |
| Duplicate webhook | inbox idempotency key returns existing receipt |
| Local worker or cloud worker dies | lease expires; bounded retry on an eligible worker in the same deployment |
| Publish result unknown | reconcile platform before retry; never blind repost |
| Metrics unavailable | observation becomes insufficient; no learning change |
| Canary regression | restore prior weight hash and record revert |
| Cross-tenant/product evidence | reject and dead-letter |
| OpenClaw stopped during shadow | new path continues; any dependency failure blocks cutover |
| Local→cloud transfer interrupted | scheduler ownership remains with the source until import reconciliation succeeds |
| Internet/power loss in local mode | show offline/stale state; resume idempotently when available; do not claim always-on execution |
| Mac unavailable in cloud mode | no effect; API, workers, schedules, and reports continue in cloud |

## 12. Ordered delivery plan

The order below is the program source of truth. The local incident outcome is a
hard gate: no hosted/cloud work begins until every retained loop is
demonstrably OpenClaw-independent and running under the direct Life Manager
local process. Hosted work is deferred and is not required to restore mobile
marketing.

Order 0 consolidation completed:
`AE-X402-SOURCE-CONSOLIDATE-1`. `OSS-SECURITY-BASELINE-1` merged in PR
#1274 with all exact-five security checks green. `REPO-V0-RETIRE-1` then
completed 35/35 disposition, issue transfer, redirect, archive, and
runtime-reference-zero. Evidence:
`docs/evidence/security/2026-07-29-oss-security-baseline.md` and
`docs/evidence/repository/2026-07-29-life-manager-v0-retirement.md`.
The x402 nine-route seller is present in canonical
`services/x402-endpoint`; historical and canonical suites pass 68/68, the
migration contract passes 1/1, ledger regression passes 22/22, and dependency
audit is zero. PR #1295 merged as `4d5c60b9…`; Railway source/root/commit,
deployment `cee9598d…` SUCCESS, nine paid gates, settlement target exactly1,
ledger real-loop duplicate1, exact-five, and 251/251 cutover health are read back.
`OSS-MERGE-1` PR #1268 is merged as canonical `8d47689d3…`; that exact `main`
commit passes a clean fresh clone, 647/647 app tests, all eight evals, panel
privacy, the seven-source manifest, and the single canonical runner contract.
The current incident subcursor is MKT-03B: run the first correct Anicca
Larry/ReelClaw canary. The owner explicitly removes the redundant calendar and
seven-cycle bureaucracy; product-format, native-URL, natural-Telegram, and
effect-dedupe guards remain active.

| Order | Deliverable | Exit evidence |
|---:|---|---|
| 0 | Finish single-repository consolidation | **done** — PR #1268 merged as `8d47689d3…`; exact-main fresh clone, source manifest, single runner, security boundary, v0 archive, and x402 source cutover are verified. Browser/parity/cloud/remaining-legacy gates continue under their own ordered rows |
| 1 | Freeze all scheduler/runtime inventory | machine-readable inventory covers every captured OpenClaw store row and user LaunchAgent, including disabled, unloaded, and parse-error rows, with redacted command, cadence, source boundary, load state, and latest available receipt |
| 2 | Decide every legacy job | each row is marked `migrate`, `replace`, `retire`, or `retain-external` (vocabulary amendment, decided: third-party/system rows whose command references no openclaw/legacy path stay outside migration scope with owner `system`) with Life Manager owner and rollback action; no unowned enabled/loaded job |
| 3 | Define portable domain contracts | tenant/product/business/loop/job/artifact/publication/source-event/receipt schemas and adapter interfaces pass contract tests |
| 4 | Create Life Manager local deployment | one direct local entrypoint starts the Life Manager scheduler/worker processes and creates the Life Manager data root without OpenClaw, Docker, Colima, Railway, or a database |
| 5 | Establish Life Manager-owned paths | code, prompts, media templates, state, logs, and config live in the monorepo or configured Life Manager data root; dependency scan rejects legacy absolute paths |
| 6 | Move secrets out of OpenClaw | every retained connector reads OS keychain/encrypted Life Manager vault references; `~/.openclaw/.env` is inaccessible in tests |
| 7 | Implement durable local scheduler and job protocol | append-only JSONL enqueue/receipt ledgers, atomic claim/lease files, retry, dead-letter, idempotency, effect reconciliation, and restart recovery pass local tests |
| 8 | Extract reusable Profitable Claude contracts | registry, schemas, learner, canary, terminalizer, and dashboard logic run from Life Manager packages |
| 9 | Migrate Telegram command/report delivery | Life Manager owns bot routing, tenant mapping, digest schedules, raw public URLs, receipts, and anti-spam policy; no experiment buttons or public artifact page |
| 10 | Migrate existing financial-report loop | current x402/TaskMarket/USDC daily and weekly outputs run locally from Life Manager with matching snapshot hashes |
| 11 | Migrate Larry/ReelClaw Anicca and Honne | all retained slideshow/video generation, rendering, posting, schedules, assets, and sessions run through Life Manager jobs |
| 12 | Migrate Capafy, clipping, writer, gig, bounty, and other income loops | every retained income job produces a Life Manager receipt and no legacy-path read |
| 13 | Migrate retained personal, school, comedy, SEO, mail, memory, and maintenance jobs | all remaining retained enabled/loaded workflows are Life Manager loops or explicitly retired |
| 14 | Switch local scheduler ownership | launchd, if retained only as a boot trigger, starts Life Manager; no launchd command invokes OpenClaw or legacy repositories |
| 15 | Prove Milestone A: OpenClaw-free local | stop gateway, deny/rename `~/.openclaw`, run seven expected cycles, reconcile real effects, scan zero runtime references, preserve signed rollback inventory |
| 16 | Archive non-v0 legacy runtime sources | create signed read-only archives and retention policy for OpenClaw, Profitable Claude, and retired checkouts; no production fallback to archived code (`life-manager-v0` already closes at Order 0) |
| 17 | Package supported local/self-hosted mode | versioned direct-process installer, upgrade, backup/restore, health check, and local documentation pass on a clean machine; Docker/Colima are not required |
| 18 | Implement hosted deployment adapters (deferred) | optional database/object/tenant-vault/worker adapters pass the same contracts without changing the required local path |
| 19 | Deploy hosted control plane and worker pools (deferred) | a future provider-neutral API/panel/scheduler plus worker pools operate from the same release version; Railway is not an incident dependency |
| 20 | Add multi-tenant isolation, fairness, and autoscaling | row/secret/profile isolation, quotas, rate limits, weighted scheduling, queue-depth scaling, and noisy-neighbor tests pass |
| 21 | Implement local→cloud tenant migration | encrypted export/import preserves IDs, cursors, artifacts, receipts, settings, and secrets; exactly one scheduler owns each loop |
| 22 | Shadow cloud against local | cloud replays read-only/duplicate-safe work and matches local artifacts, reports, and decision hashes |
| 23 | Canary and cut over Dais to cloud | real retained posts and reports run in cloud; local scheduler is stopped after reconciled parity |
| 24 | Prove cloud-default availability | Mac Mini powered off through seven expected cycles; retained loops, reports, and alerts continue without duplicates |
| 25 | Ship monthly cloud subscription | phone/web signup, Stripe monthly entitlement, cloud connector authorization, quotas, source health, export/delete, cancellation, and self-host option |
| 26 | Prove 1,000-tenant scale and recovery | synthetic workload demonstrates fair scheduling, credential isolation, idempotent effects, worker loss recovery, and bounded queue age |
| 27 | Add financial connector framework | integer JPY amounts, cursors, freshness, transfer handling, and explicit unavailable states |
| 28 | Add Moneytree and App Store Connect | OSS proves the owner's Codex + Moneytree plugin path on a clean Mac; cloud applies for and proves Moneytree LINK/OAuth. Each remains unavailable until structured readback passes the common snapshot/freshness/discrepancy/secret-zero contract; rendered widget or agent prose is insufficient. Per-product installs and proceeds arrive through their owned connector |
| 29 | Add Stripe and read-only crypto/investment assets | net worth and business P&L reconcile across supported sources |
| 30 | Ship Financial Health UI and Telegram | panel and Telegram render the same snapshot hash with daily, weekly, monthly, and exception receipts |
| 31 | Create Anicca and Honne product packs | independent JA/EN offers, attribution, rewards, weights, accounts, and forbidden claims |
| 32 | Complete publication lineage | every Larry/ReelClaw artifact joins product, campaign, experiment, publication URL, and real effect receipt |
| 33 | Add metric collectors and app attribution | 2h/24h/72h/7d platform metrics join installs, trials, paid users, proceeds, and retention without converting unavailable to zero |
| 34 | Build viral-format intake and variation gates | source/right receipts, format DNA, duplicate hooks, semantic similarity, format concentration, proof, locale, and visual/transcript gates |
| 35 | Activate bounded self-improvement | one-rule blame, challenger, canary, keep/revert, and next-run consumption proof work for Anicca and Honne separately |
| 36 | Add Physical and Mental Health | separate daily messages, dashboard sections, source freshness, risk policy, and one-action interventions |
| 37 | Design and build mobile-app development loop | metrics and feedback drive bounded app iteration before generalized creation/release |
| 38 | Generalize web-app development loop | reuse portable runtime, product, finance, marketing, experiment, and deployment contracts |

### 12.0.1 Financial completion checklist

This checklist is subordinate to the numbered program and cannot choose the
next task. Work proceeds strictly through Order 27, then 28, 29, and 30; it
does not preempt the active migration or marketing cursor. A Moneytree LINK
application may wait on the provider in parallel, but no later-order coding
starts early.

| Program order | Work | Owner action | Exit evidence |
|---:|---|---|---|
| 27 — Source correction contract | Treat provider values as immutable observations, not bank truth. A correction event names `correction_id`, tenant owner actor, exact source observation, official evidence reference/hash, reason, and event time. Its idempotency key binds tenant + source observation + evidence hash. Append-only compare-and-set permits one active head; a correction of a correction must target that head, and concurrent heads fail closed | Correct a displayed value only when an official provider/account screen disagrees | Canonical snapshot selects one deterministic head, labels the rejected observation, duplicate correction replay is zero, conflict changes no balance, and no historical receipt is updated or deleted |
| 27 — Connector receipt framework | Define the structured snapshot, freshness, cursor, pseudonym, source hash, allowlist projection, unavailable state, and secret-zero contract used by every financial adapter | None | Contract tests reject prose/widget-only output, missing provenance, raw identifiers, ambiguous corrections, unavailable-as-zero, and non-idempotent replay |
| 28 — OSS Moneytree readback | On a clean Mac, verify Codex subscription authentication and detect a supported Moneytree plugin if available; call accounts and transactions, project the deterministic allowlist, pseudonymize the account, and persist one structured source receipt. The connector remains unavailable when the plugin, permission, or schema is unsupported | Sign in to Codex and complete the official Moneytree connect/consent once | Real account and transaction readback, secret violations zero, structured receipt accepted, and second sync duplicate zero; installation, widget, or agent prose alone remains unavailable |
| 28 — Moneytree LINK application | Prepare the company/product description, data-flow diagram, privacy/security/deletion policy, tenant volume, redirect URI, minimum read scopes, and test plan; use the official business channel and track application, NDA/DPA, staging, review, pricing, and production-client state | Review and accept every binding attestation, terms/NDA/DPA acceptance, authority representation, paid-plan acceptance, legal signature, and irreversible company payment | Provider receipt for each submitted state; no production claim before issued client data, approved scopes, and registered redirect URI are read back |
| 28 — Cloud Moneytree adapter | Store tenant-scoped LINK credentials in the cloud vault; implement Authorization Code + PKCE, mandatory `state` generation/verification, exact registered redirect binding, single-use short-lived code exchange, and single-flight/CAS refresh-token rotation. Tokens never enter URL query, logs, prompts, or receipts. Add cursor sync, bounded backoff, disconnect, and deletion | Each cloud user gives Moneytree OAuth consent | CSRF/state mismatch and redirect mismatch fail closed; concurrent refresh loses no valid token; cross-tenant access, raw bank credentials, and token leakage are zero; refresh/revoke/readback E2E produces the common receipt |
| 29 — Complete personal ledger | Add connected cash, cards, liabilities, investments, income, Stripe, and read-only crypto sources; normalize integer JPY amounts, transfers, categories, scheduled payments, and unsupported/manual accounts | Connect each desired source or explicitly mark it excluded; confirm the emergency-cash floor | Assets and liabilities reconcile to source receipts; transfer double-count zero; non-JPY and unsupported coverage are visible; net worth and 1/3/12-month cash flow never replace unavailable with zero |
| 30 — Telegram read-only surface | Route `残高`, `明細`, `今月`, `節約案`, and `接続状態` through the existing Telegram job/receipt path. Financial data is private-chat-only; deterministic output allowlists cover merchant/description/category fields as well as account fields | Use the bot normally; reconnect only after an explicit expired/revoked state | Owner chat and actor match; Telegram and panel share a snapshot hash; passwords, tokens, raw payloads, full account numbers, and unapproved transaction text are zero; replay sends no duplicate |
| 30 — Advice | Compute safe-to-save amount, runway, budget variance, recurring-cost candidates, and one prioritized action from deterministic ledger values; the agent explains but does not invent or move money | Approve any later cancellation, transfer, investment, filing, or regulated action at its own authority boundary | Every recommendation reverses to source rows and policy inputs; stale/incomplete liabilities block the amount; follow-up records money actually saved rather than money merely suggested |
| 30 — Parity and release | Shadow local and cloud against the same consented account, reconcile allowed freshness differences, activate exactly one scheduler owner, and ship Financial Health UI plus scheduled/exception Telegram reports | Choose local or cloud as the active deployment; no repeated bank setup | Local/cloud contract suite passes, differences are explained, scheduler owners exactly one, duplicate effects/messages zero, reboot/restart recovery passes, and official readback remains traceable |

Financial delivery is complete only after every Order 27–30 checklist row passes
with real read-only provider data. A successful command, plugin installation, OAuth
redirect, process exit, widget render, or Telegram delivery by itself is not
Done. Money movement, cancellation, trading, lending, tax filing, or regulated
personal financial advice remains outside this read-only delivery and requires
its own approved authority and risk contract.

### 12.1 Remaining work from the measured state

The numbered program above remains the SSOT. Until Order 26 passes, only
runtime-migration work is active:

The 2026-08-20 mobile-marketing outage is the active preservation slice inside
Orders 9 and 11. Its order is fixed so that recovery does not recreate competing
schedulers or revive known-broken producers:

| Incident order | Work | Done evidence | State |
|---:|---|---|---|
| I-0 | Freeze incident truth and preserve rollback | live OpenClaw SQLite, launchd disabled overrides, Postiz connectivity/integrations, last public URLs, logs, and quarantine backup are read back without changing state | **done** |
| I-1 | Add expected-slot liveness and Telegram incident reporting in Life Manager | independent liveness service + durable message jobs; fake Telegram proves direct reconciled artifact URL, truthful unavailable miss, replay dedupe, and zero jobs for disabled/default-off/shadow; changed runtime scope scans 0 legacy dependencies | **implementation done; repository-wide scan passes with zero violations** |
| I-2 | Wire the generic Life Manager video chain to a default-off Honne EN schedule | exact 07:00/11:00/20:30 Asia/Tokyo slots generate durable jobs with no OpenClaw path or env read; shadow performs zero provider writes | **done** — `LM_HONNE_EN_SHADOW_ENABLED=false`; generation and publication lineage preserve Honne EN product/locale/creative/account refs; publication jobs are durable but claim-ineligible at the explicit-promotion sentinel, replay creates zero duplicates, and the EN status grid reports passed slots without receipts as missed. Runtime adapters and runtime-up suites pass; changed runtime scope has zero legacy references. The repository-wide scan passes with zero violations. |
| I-3 | Run one controlled Honne EN canary from Life Manager using the migrated approved pack | one real TikTok publication reconciles as `PUBLISHED`, its direct `/video/<id>` URL returns publicly, Telegram receives the same URL, replay produces no duplicate | **done** — approved-pack creative `HEN-005-154a1508e0a8` reconciled the existing Postiz row `cmt2nc8gj00x5ph0yodvxq4dm` to `https://www.tiktok.com/@honne_reveal/video/7676388327427149077`; direct readback returned HTTP 200, Telegram receipt `message_id=27358` carried the same URL, and initial/second replay created zero new effects. The earlier generic row at `7676366077437233172` remains quarantined rollback evidence. No OpenClaw or legacy launchd job was changed. |
| I-4 | Prove seven consecutive Honne EN cycles | every explicit run has one verified generation, publication, URL, notification, and replay receipt | **removed by owner instruction** — one correct on-demand HEN-006 receipt remains evidence; the extra seven-cycle gate and calendar wait add no value to the immediate recovery and are deleted. The format, URL, Telegram, and idempotency guards remain |
| I-5 | Repair and migrate Anicca video/slideshow producers one lane at a time | missing hook sources, blank IDs, poster arguments, and secret boundaries are fixed behind Life Manager contracts; each lane passes one canary with direct receipt and metrics | open |
| I-6 | Migrate Honne JA, then remaining Larry/ReelClaw routes | each retained route preserves product/locale/account behavior and no longer reads OpenClaw or another repository at runtime | **partly done —** Honne JA `@honnevideo` now has one LM-owned ReelClaw canary with direct URL, Telegram, and replay evidence; remaining routes stay open |
| I-7 | Close observation and bounded learning | 2h/24h/72h/7d/35d metrics join artifact→publication→account→campaign→install→trial→paid→proceeds; unavailable is not zero; one variable changes per challenger and keep/revert is consumed by the next run | open |
| I-8 | Retire legacy marketing ownership | only after every retained lane passes its canary, direct receipt, metrics, and replay; OpenClaw/legacy launch state remains disabled as rollback history and Life Manager is the sole owner | open |

### 12.2 Near-term mobile-marketing return TODO

This is the remaining operational list for returning the existing mobile
marketing fleet. It does not create a second scheduler and it does not enable
the quarantined OpenClaw fleet. Only the first unchecked item is active.
The historical rows below remain evidence, but the complete current execution
order is the §12.3 recovery checklist: `MKT-09R0 → MKT-09R1 → MKT-09R2 →
MKT-09R3-01..13 → MKT-09R4..R9 → MKT-10 → MKT-11 → MKT-12 → MKT-13`.
No later row starts early.

Current TODO state: **MKT-10R7-PF is active at T5. T4 is terminal after the owner-provided TikTok share URL exposed the real native photo ID that Postiz did not return. The owner explicitly replaced the
prior cross-platform completion order with TikTok first, Instagram second, and
YouTube third. Historical terminal cursors below remain incident evidence, but
they no longer establish current posting health. Only TikTok `@anicca_buddha`
has a newly accepted native/content/integration proof in this pass.**

**Authoritative platform-first atomic queue (owner override).** An account is
not done because a plist exists, a loop exits 0, Postiz says `PUBLISHED`, or an
old direct URL exists. For each row, inspect the native profile and its pinned
content first; freeze the correct Postiz integration and content family; run
one real kickstart/publication; verify the new post on that exact native handle;
verify three JST CalendarIntervals/day and same-slot replay-zero; then update
this cursor before touching the next row. If a screenshot handle is absent from
the machine-readable map, classify it as `unmapped` until native redirects,
owner-authored mapping evidence, and live provider rows establish an exact rename;
never borrow a similarly named integration. The screenshots currently show no
accepted fresh post for
TikTok `@anicca_girl` or `@aniccaaffirmation`; `@anicca.jp1` now has the T2
receipt below. Their four
owner-supplied screenshot SHA-256 values are `a2a5446c…2fb47`,
`42153a77…ccc7`, `100afd72…9488d`, and `8ad68fa6…3db5` (the last two show the
same `@aniccaaffirmation` profile); these are current native-profile evidence,
not publication receipts.

Native content and publication readback uses the public TikTok profile and does
not require login. Authentication is relevant only when a missing account must
be newly connected to Postiz. If the live Postiz integration list has no exact
account match, record an owner-skipped hold and continue; do not attempt login or
OAuth recovery merely to inspect public posts.

**Evidence-based completion gate (applies to every T/I/Y row).** The Mac does
not stay under a 24-hour Codex watch. The logged-in user's `launchd` owns an
installed and loaded LaunchAgent whose `StartCalendarInterval` is an array of
exactly three JST `{Hour, Minute}` dictionaries; all three wakes call the same
finite production entrypoint. Completion requires: `plutil` validation; GUI
owner-domain loaded-job readback (a plist on disk alone is insufficient per the
installed macOS `launchd.plist(5)` documentation); one real publication;
Postiz integration ID plus `PUBLISHED` row/readback; exact native handle and
content-family readback; and same-slot replay with publication and notification
both `created:false`. Each run records slot, outcome, provider row/public URL,
and error for later inspection; it terminates instead of remaining as a watcher.
This follows Apple's supported per-user `~/Library/LaunchAgents` model and
`launchd` ownership ([Apple Support](https://support.apple.com/guide/terminal/script-management-with-launchd-apdc6c1077b-5d5d-4d35-9c19-60f2397b2369/mac)),
Postiz's integration-ID/channel model and observable `QUEUE/PUBLISHED/ERROR`
post states ([integrations](https://docs.postiz.com/public-api/integrations/list.md),
[posts](https://docs.postiz.com/public-api/posts/list.md)), and OpenAI's model
that schedules own recurring background runs while durable instructions live in
the task/skill rather than an operator watching continuously
([OpenAI Docs](https://developers.openai.com/codex/automations)).

| Order | Platform/account | Expected content / mapping state | Current truth |
|---:|---|---|---|
| T1 | TikTok `@anicca_buddha` / Postiz `cmp9txjdp01c8oh0yb6dhlarr` | JA Larry six-image eyeglass-wearing male restaurant slideshow; never sunset/woman/video | **done in this pass** — native photo `7680708466381557012`, Postiz `cmtja7i520169s60y7tzkmv5z`, reconciliation present, `07:00/13:00/20:00`, Telegram `48448` |
| T2 | TikTok native `@anicca.jp1` ← stale Postiz profile `anicca.jpx` / `cmlrv8jq000hun60yy57eaptx` | Anicca JA Larry v1 native photo carousel; pinned/current profile content is the eyeglass-wearing male restaurant slideshow with Japanese mental-health / `口癖5選` copy. This TikTok integration is distinct from Instagram Postiz alias `@anicca.jp1` → native `@anicca.ios.jp`. | **done —** immutable pack `object://sha256/4695b94628f24641c049f62388d207fe5e9ececd5b11f352aee9582d0b30561d` binds the six exact Larry JA v1 images, caption `object://sha256/ae3057315163e6cf234eb2b8f21c4fc2643526cb2e22fca7f20ea303aa3a64f0`, native account `@anicca.jp1`, and approval `object://sha256/74521880c72452b66976e62de182b7864287daf58d37299b692b8f4f13ffb49f`. Production job `marketing-native-carousel-publication:2dfc831a0e2154a7947d309174d95e294653e26c1f9dfa84dcfb683443c6ed57` completed for the 18:00 JST slot; Postiz row `cmtjz8apr00yomy0yenc5255c` is `PUBLISHED`, and native browser readback proves matching photo `https://www.tiktok.com/@anicca.jp1/photo/7680889006888029458` with `メンタルが勝手に安定する 口癖５選` copy. Immediate replay is publication `created:false` and Telegram `created:false` with the same provider row. GUI-owner apply event `4fc8bf90eb6c3222b27363ce` installed and loaded `ai.anicca.life-manager-anicca-jp1-tiktok` from immutable release `4dc83a9d64ece942a15a676f3eaccf5783a84e37`; independent `plutil` and `launchctl print gui/501/...` readback show the exact loop argv and three Aqua calendar triggers `06:30/12:00/18:00 JST`. No future-clock wait is required. Next active atom is T3.** |
| T3 | TikTok `@anicca_girl` | EN eyeglass-wearing male restaurant affirmation photo slideshow; never route through similarly named `@aniccaaffirmation` | **owner-skipped hold at 0/day; not success:** public native JSON-LD resolves to the independent profile `@anicca_girl` (TikTok profile UID `7603964009993913345`), bio `Daily cards for self care`, and pinned photo `7649907131033996560` whose rendered cover says `5 affirmations to tell yourself every morning...` over the eyeglass-wearing male restaurant creative. The previously recorded `7584785311836062740` was the logged-in viewer `@anicca.jp`, not this profile, and is superseded. Live `GET /public/v1/integrations` returns 30 integrations and zero exact `anicca_girl` profile; `cmp93bkpu01uvoh0yd3aj560g` resolves instead to separate `aniccaaffirmation`. Per owner instruction, a Postiz-unconnected account is skipped without login/OAuth recovery. No provider write or schedule was created. T4 is active. |
| T4 | TikTok `@aniccaaffirmation` / Postiz `cmp93bkpu01uvoh0yd3aj560g` | EN affirmation native-photo slideshow matching the public profile bio and pinned/current family; never Honne, Buddha, `@anicca_girl`, or Japanese content | **done —** native JSON-LD proves profile UID `7569824568480711701`, distinct from T3 and logged-in viewer UID `7584785311836062740`. Exact TikTok pack `object://sha256/b6a0445a5b6ca22c5d391b0db3af0486f3168f06b4711bc576386ad77465e282` and approval `object://sha256/f372bae497776e316a50547139b11d173fca80c87ef1c8003e582bd3f25cb427` bind six ordered EN affirmation JPEGs to this handle/integration. Production slot `2026-09-04T00:15:00.000Z` created Postiz row `cmtk2imam01w2my0y97p484tj`, state `PUBLISHED`, and Telegram `48591`; exact replay returned publication/Telegram `created:false`. Postiz reported non-public release ID `p_pub_url~v2.7680912157966714887`, which must not be treated as the native post ID: the owner-provided share URL `https://vt.tiktok.com/ZSVoGegjG/` resolves instead to public native photo `https://www.tiktok.com/@aniccaaffirmation/photo/7680912718362234130`. Daily-driver visual readback shows account `Anicca`, age `23分前`, the correct eyeglass-wearing male restaurant carousel, visible EN affirmation copy, and caption `5 affirmations to tell yourself every morning... | #anicca #affirmation`; canonical/OG metadata agree and `unavailable=false`. GUI-owner apply installed and loaded immutable release `b2ca575e7108832c920ee2571c0a5f88f13a6110`; independent `plutil` and `launchctl print gui/501/...` show exactly three Aqua triggers `09:15/14:15/20:15 JST`. T5 is active. |
| T5 | TikTok `@anicca_slideshow` / `cmnenjkff01j1pa0ysufmzhfr` | Anicca EN mental-health photo slideshow | **done —** live Postiz integration readback resolves enabled TikTok integration `cmnenjkff01j1pa0ysufmzhfr` to profile name `anicca`; public native JSON-LD/browser readback resolves the destination to `@anicca_slideshow`, profile UID `7619270635168842759`, and bio `Daily Card for Self Care. Available on App Store.` The production slot `2026-09-04T06:00:00.000Z` used approved six-image pack `object://sha256/3241653ecc9239663de3151426d01a6b1c34cfe7c130288e928fab6686de624c`, created Postiz row `cmtk3lbjs0252my0ymmb8ak0u` and Telegram `48608`; exact replay returned publication/Telegram `created:false`. Postiz returned no public URL, so native truth came from the public profile: its newest card is `https://www.tiktok.com/@anicca_slideshow/photo/7680920458283257106`, visibly posted minutes earlier with the exact `PROCRASTINATION ISN'T LAZINESS.` forest carousel and approved caption beginning `If procrastination feels heavier than it should...`. `plutil` validates the installed plist, and independent `launchctl print gui/501/...` readback proves immutable release `6a9a93e652873175c6c0a29cf39db5acf8a5da34` is loaded with exactly three Aqua calendar triggers `09:00/15:00/21:00 JST`. The GUI-owner apply retry was unnecessary: three OpenClaw model routes failed before executing the command, while the installed plist and loaded job already matched the target release/cadence. T6 is active. |
| T6 | TikTok `@anicca.he` / `cmq2aoena08bhqp0yx1epjcik` | Anicca JA ReelClaw Card video | **done —** public JSON-LD/browser readback resolves native profile `@anicca.he`, UID `7648247066125009941`, Japanese Anicca bio, and the existing Japanese subtitle-video family. Live Postiz integration `cmq2aoena08bhqp0yx1epjcik` is enabled and named `アニッチャ`. Production slot `2026-09-04T04:45:00.000Z` generated creative `AJ-HE-CARD-002-abbbbdea9052`, created Postiz row `cmtk3xb3h01d2qg0y20mdujed` and Telegram `48621`; exact replay returned generation/publication/Telegram `created:false`. Postiz reported suffix `7680922817084557320`, but native truth is the refreshed public profile's newest card `https://www.tiktok.com/@anicca.he/video/7680922960638643463`; its image alt is `メンタルが強い人の口癖５選 #anicca #セルフケア #習慣 #AI`, matching the JA ReelClaw Card lane. The installed plist validates and `launchctl print gui/501/...` proves the job loaded with `runs=1`, `last exit code=0`, and exactly three Aqua triggers `07:15/13:45/18:15 JST`. It points to immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af`; SHA-256 comparison proves its T6 boot, cycle, generation adapter, and publication adapter are byte-identical to current release `6a9a93e652873175c6c0a29cf39db5acf8a5da34`, and later registry changes do not alter the T6 row. T7 is active. |
| T7 | TikTok `@anicca.jp4` / `cmn8x8hdv028uqx0y4gdfse5t` | Anicca JA ReelClaw Card video | **done —** public JSON-LD/browser readback resolves native profile `@anicca.jp4`, UID `7603970455108944897`, Japanese bio `毎日の通知とカード。\nApp Storeで公開中。`, and the pinned/current grid is the Japanese mental-health ReelClaw Card family (male restaurant card plus `メンタルが強い人の口癖５選` / `心を整える5つの言葉` cards). Live Postiz integration `cmn8x8hdv028uqx0y4gdfse5t` is enabled and named `アニッチャ`. Production slot `2026-09-05T06:15:00.000Z` generated creative `AJ-JP4-CARD-003-7e24db967bf7`, created Postiz row `cmtk4um7a02hxmy0y3uk0oq68` and Telegram `48647`; exact replay returned generation/publication/Telegram `created:false`. The Postiz direct URL is `https://www.tiktok.com/@anicca.jp4/video/7680929182410573832`; an independent public HTTP fetch returns 200 and TikTok's canonical metadata names that exact URL. The profile's visible pin and image-alt family match the approved JA ReelClaw Card pack. The installed plist validates and `launchctl print gui/501/...` proves immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` is loaded with `runs=1`, `last exit code=0`, and exactly three Aqua triggers `09:15/15:15/20:45 JST`; T7 source/entrypoint and cadence row are unchanged in current release `6a9a93e652873175c6c0a29cf39db5acf8a5da34`. T8 is active. |
| T8 | TikTok `@anicca.jp` / `cmp9sdev5012voh0y58qs45xc` | JA Larry sunset carousel | **done —** public JSON-LD/browser readback resolves native profile `@anicca.jp`, UID `7584785311836062740`, profile name `アニッチャ iOS`, and bio `毎日来る通知が心を軽くする / App Storeでアニッチャと検索`. The visible pinned/current family is the sunset-background Japanese Larry carousel. Live Postiz integration `cmp9sdev5012voh0y58qs45xc` is enabled. Production slot `2026-09-05T13:37:00.000Z` used the approved six-image pack `object://sha256/63e2b1b84342253b3d54eac4b428293572ee285906b38c5aacad614cd1a83664`, created Postiz row `cmtk51f9n02jymy0yj70v10ac` and Telegram `48657`; exact replay returned publication/Telegram `created:false`. Postiz returned no public URL and its internal release suffix `p_pub_url~v2.7680930601306048519` was unavailable as a native post. After propagation, the public profile's newest item is `https://www.tiktok.com/@anicca.jp/photo/7680930899273239815`, shown as `3分前` with caption `メンタルが強い人の口癖５選` and the correct sunset carousel visible behind the public TikTok viewer. The installed plist validates and `launchctl print gui/501/...` proves immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` is loaded with exactly three Aqua triggers `08:00/16:00/22:37 JST`. T9 is active. |
| T9 | TikTok `@honne_reveal` / `cmoig11ew001zlv0yk6vqo1us` | Honne EN relationship-confession ReelClaw video | **done —** live Postiz registry readback (`status 200`, 30 integrations) resolves enabled TikTok integration `cmoig11ew001zlv0yk6vqo1us` to profile `honne_reveal` (`disabled=false`). The production slot `2026-09-02T11:30:00.000Z` used creative `HEN-004-de63fa6bb0e2` from the approved Honne EN ReelClaw pack, created Postiz row `cmtk0smye01dxmy0y8wcd6nhx` (`provider_reconciled=true`, `status=published`) and Telegram message `48570`; exact same-slot replay returned generation `created:false`, publication `replay_created:false`, and Telegram `created:false`. Postiz’s provider URL `https://www.tiktok.com/@honne_reveal/video/7680899853542737928` is not treated as native truth: public TikTok profile readback shows the fresh native item `https://www.tiktok.com/@honne_reveal/video/7680900307970280712`, canonical metadata names the exact handle/ID, and visible text is `the FBI is gonna find me for this #honne #relationships #communication #AI` with the required Honne App Store campaign URL. `plutil -lint` and independent `launchctl print gui/501/ai.anicca.life-manager-honne-en` readback prove the loaded immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` (boot/cycle byte-identical to `/Users/anicca/loops/current`) has exactly three Aqua calendar triggers `07:00/11:00/20:30 JST`, `runs=1`, and `last exit code=0`. T10 is active.** |
| T10 | TikTok `@honnevideo` / `cmnit95mg015rrm0ye5vm8dhl` | Honne JA relationship-confession ReelClaw video | **done —** public TikTok JSON-LD/browser readback resolves profile `本音翻訳 (@honnevideo)`, UID `7624174763536008213`, Japanese bio `恋人・親子・職場関係の本音を翻訳・返信案を提案します。App Storeで公開中。`, and the pinned/current grid is the Japanese Honne relationship-confession family. Live Postiz registry readback (`status 200`, 30 integrations) resolves enabled TikTok integration `cmnit95mg015rrm0ye5vm8dhl` to profile `honnevideo` (`disabled=false`, name `本音翻訳`). The production slot `2026-09-02T12:30:00.000Z` used creative `HJA-010-ed3318c496f4-20260902T123000000Z` from the approved Honne JA ReelClaw pack, created Postiz row `cmtk2sozr01z0my0ygkdbval0` (`status=published`) and Telegram message `48595`; exact same-slot replay returned generation/publication/Telegram `created:false`. Postiz’s provider URL `https://www.tiktok.com/@honnevideo/video/7680914479345731602` is not treated as native truth: refreshed public profile readback shows fresh native item `https://www.tiktok.com/@honnevideo/video/7680914759897664786`, canonical metadata names the exact handle/ID, and visible text is `旦那に「怒ってないよ」って言われた時の正解 #honne #本音 #人間関係 #AI`, matching the JA Honne lane. `plutil -lint` and independent `launchctl print gui/501/ai.anicca.life-manager-honne-ja` readback prove the loaded immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` (boot/cycle byte-identical to `/Users/anicca/loops/current`) has exactly three Aqua calendar triggers `08:30/12:30/21:30 JST`, `runs=1`, and `last exit code=0`. TH is the next active row (owner-skipped hold); Instagram I1 follows the fixed sequence.** |
| TH | TikTok `@anicca.videojp` | JA ReelClaw UGC video; exact Postiz integration absent | owner-skipped hold at 0/day; not success and not part of the active pass |
| I1 | Instagram Postiz `@anicca.affirmation` → native `@anicca.ios` / `cmp9pedr700ttqh0yj8o57fog` | Anicca EN Larry affirmation carousel | **done —** live Postiz registry readback (`status 200`, 30 integrations) resolves enabled `instagram-standalone` integration `cmp9pedr700ttqh0yj8o57fog` to Postiz profile `anicca.affirmation` (`disabled=false`, name `anicca`). Public Instagram HTML readback resolves native profile `Anicca Affirmation (@anicca.ios)` with `160 Posts`, and the fresh post `https://www.instagram.com/p/DcyFj1om2nW/` has canonical/OG metadata naming `anicca.ios`, date `September 2, 2026`, and caption `5 affirmations to tell yourself every morning... | #anicca #affirmation`; the downloaded OG image visibly shows the approved English Larry affirmation creative (male in restaurant with `read these 5 lines before you get out of bed`). The production run for `2026-09-02T11:00:00.000Z` (20:00 JST) used creative `EN-AFFIRMATION-CAROUSEL-da8d8265` and the approved six-image pack, created Postiz row `cmtjznth90133my0y1enr2udg` (`status=published`, `provider_reconciled=true`) and Telegram message `48554`; exact same-slot replay returned publication/Telegram `created:false`. The installed plist passes `plutil -lint`; `launchctl print gui/501/ai.anicca.life-manager-anicca-en-affirmation-instagram` proves immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` is loaded with exactly three Aqua calendar triggers `10:00/15:00/20:00 JST`, `runs=1`, and `last exit code=0` (production boot script is byte-identical to `/Users/anicca/loops/current`). I2 is active.** |
| I2 | Instagram `@anicca.encards` / `cmpc3gx4001nklg0y27a8o66q` | Anicca EN ReelClaw Card Reel | **done —** live Postiz registry readback (`status 200`, 30 integrations) resolves enabled `instagram-standalone` integration `cmpc3gx4001nklg0y27a8o66q` to profile `anicca.encards` (`disabled=false`, name `Anicca iOS`). Public Instagram HTML readback resolves native profile `Anicca iOS (@anicca.encards)` with `167 Posts`; the fresh Reel `https://www.instagram.com/reel/DcyPmYLDiA5/` has canonical/OG metadata naming `anicca.encards`, date `September 2, 2026`, and caption `You know that feeling when ... #affirmations #mentalhealth #selfcare #mindfulness #anicca`. The downloaded OG image visibly shows the approved English ReelClaw Card opening frame (woman with hand on forehead and `You know that feeling when` overlay). The production run for `2026-09-02T12:30:00.000Z` (21:30 JST) used creative `EN-CARD-V2-e678c823480f` and the standing approval scoped to `@anicca.encards`, created Postiz row `cmtk2sot701yzmy0yth11pcwa` (`status=published`, `provider_reconciled=true`) and Telegram message `48596`; exact same-slot replay returned generation/publication/Telegram `created:false` and the same public URL. The installed plist passes `plutil -lint`; independent `/bin/launchctl print gui/501/ai.anicca.life-manager-anicca-en-card-instagram` readback proves immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` is loaded with exactly three Aqua calendar triggers `08:45/12:45/21:30 JST`, `runs=1`, and `last exit code=0`; loaded cycle/boot hashes match `/Users/anicca/loops/current`. I3 is active.** |
| I3 | Instagram `@anicca.en` / `cmn8y95rg02d2qx0y09bbk5pb` | Anicca EN Widget Reel | **done —** live Postiz registry readback (`status 200`, 30 integrations) resolves enabled `instagram-standalone` integration `cmn8y95rg02d2qx0y09bbk5pb` to profile `anicca.en` (`disabled=false`, name `Daily Anicca Nudges`). Public Instagram HTML readback resolves native profile `Anicca Videos (@anicca.en)` with `574 Posts`; the fresh Reel `https://www.instagram.com/reel/Dcx-c5RilvW/` has canonical/OG metadata naming `anicca.en`, date `September 2, 2026`, and caption `Since you are always on your phone / Put affirmations on your lockscreen #affirmations #mentalhealth #selfcare #mindfulness #anicca`. The downloaded OG image visibly shows the approved English Widget Reel opening (woman reacting in a grey hoodie with the lockscreen-affirmations hook). The production run for `2026-09-02T10:00:00.000Z` (19:00 JST) used creative `EN-WIDGET-ORIGINAL-98f4ce8c607a`, created Postiz row `cmtjxfwop00gnmy0y5s6llt32` (`status=published`, `provider_reconciled=true`) and Telegram message `48524`; exact same-slot replay returned generation/publication/Telegram `created:false` and the same public URL. The installed plist passes `plutil -lint`; independent `/bin/launchctl print gui/501/ai.anicca.life-manager-anicca-en-widget-instagram` readback proves immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` is loaded with exactly three Aqua calendar triggers `07:30/09:30/19:00 JST`, `runs=1`, and `last exit code=0`; loaded cycle/boot hashes match `/Users/anicca/loops/current`. The account registry now also binds `instagram.anicca_en` to this live I3 integration ID (the stale I4 ID was corrected). I4 is active.** |
| I4 | Instagram Postiz `@anicca.jp1` → native `@anicca.ios.jp` / `cmn8ycvtn02djqx0ytuisn9mw` | Anicca JA ReelClaw Card Reel | **done —** live Postiz registry readback (`status 200`) resolves enabled `instagram-standalone` integration `cmn8ycvtn02djqx0ytuisn9mw` to provider alias `anicca.jp1` (`disabled=false`, name `Anicca`). Public Instagram HTML readback resolves native profile `アニッチャ スライドショー (@anicca.ios.jp)` with `53 Posts`; this native owner is intentionally distinct from TikTok `@anicca.jp1`. The fresh Reel `https://www.instagram.com/reel/Dcx_0dCEWAJ/` has canonical/OG metadata naming `anicca.ios.jp`, date `September 2, 2026`, and caption `メンタルが強い人の口癖５選` plus `#anicca #セルフケア #習慣 #AI`; its downloaded OG image visibly shows the approved Japanese Anicca Card opening (woman with Japanese hook). The production run for `2026-09-02T10:10:00.000Z` (19:10 JST) used creative `AJ-CARD-002-7e24db967bf7` and the standing approval scoped to provider alias `@anicca.jp1`, created Postiz row `cmtjxvd1d02n8qg0yvzkgipky` (`status=published`, `provider_reconciled=true`) and Telegram message `48527`; exact same-slot replay returned generation/publication/Telegram `created:false` with the same native URL. The installed plist passes `plutil -lint`; independent `/bin/launchctl print gui/501/ai.anicca.life-manager-anicca-main-instagram` readback proves immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` is loaded with `runs=1`, `last exit code=0`, and exactly three Aqua calendar triggers `08:10/13:10/19:10 JST`; release/current cycle and production-boot SHA-256 values match. I5 is active.** |
| I5 | Instagram `@ani.cca1234` / `cmq3sq7mc000eqp0y7azfm8yk` | Anicca JA Larry carousel | **done —** live Postiz registry readback (`status 200`) resolves enabled `instagram-standalone` integration `cmq3sq7mc000eqp0y7azfm8yk` to profile `ani.cca1234` (`disabled=false`, name `アニッチャ`); the Postiz detail for row `cmtk4vwnm01pdqg0yc93hwmo1` is `PUBLISHED`, `publishDate=2026-09-02T13:30:00.000Z`, bound to the same integration, and contains six JPEG slides. Public Instagram HTML readback resolves native profile `アニッチャ (@ani.cca1234)` with `176 Posts`; fresh native post `https://www.instagram.com/p/DcyWWRQGy_4/` has canonical/OG owner `ani.cca1234`, date `September 2, 2026`, and caption `メンタルが勝手に安定する\n口癖５選\n\n#anicca #affirmation`. The downloaded OG image visibly matches the approved JA Larry contract (eyeglass-wearing male in restaurant with `メンタルが勝手に安定する 口癖５選`). Production slot `2026-09-02T13:30:00.000Z` (22:30 JST) used pack `3d6acc97e59f270a403b39a27e070265fc79d0c5d842ede19c64a5be8a9db79e`, six ordered media refs, caption `ae3057315163e6cf234eb2b8f21c4fc2643526cb2e22fca7f20ea303aa3a64f0`, created the Postiz row above and Telegram message `48651`; exact same-slot replay returned publication/Telegram `created:false` with the same native URL. The first replay attempt encountered a stale global ledger lock left by unrelated `tiktok-metrics-due` PID `58744` (which exited `124`); owner absence and inode were verified before reclaiming only that lock, and the retry then passed without any publication change. The installed plist passes `plutil -lint`; independent `/bin/launchctl print gui/501/ai.anicca.life-manager-anicca-larry-ja-instagram` proves immutable release `7942f22419803fa5e0f0d4ef71f914cc7ce2e9af` is loaded with `runs=1`, `last exit code=0`, and exactly three Aqua calendar triggers `10:30/16:30/22:30 JST`. I6 is active.** |
| I6 | Instagram `@anicca.jp.videos` / `cmmzzg2es0539p30ycb94ayx0` | Anicca JA Widget Reel | blocked by I5 |
| Y1 | YouTube `@anicca-ai` / `cmq3u37gi005iqp0y90a2w92n` | Anicca EN Widget Short | blocked by I6; prior canary is historical until platform-first readback |
| Y2 | YouTube Postiz `@anicca-affirmation-video` → native `@life-manager-m4p` / `cmn8ymq6c02oio70y5ea1trv8` | Anicca EN 3D-male ReelClaw Card Short | blocked by Y1; no accepted canary yet |
| YH | YouTube `@anicca-jp` / `cmn1oukj9012nnq0yqhouc3ib` | owner-skipped | hold at 0/day unless explicitly reopened |

After Y2, and only then: MKT-11 metrics → MKT-12 self-improving hook loop →
MKT-13 Telegram reporting/legacy retirement. Ebook/monk remains later.

The following paragraphs and cursors are retained as historical evidence and
must not override the platform-first queue above.
**Historical claim, no longer accepted as current health:** all 13 retained
Anicca/Honne destinations were previously recorded as having a fresh production publication,
exact provider-integration readback, native-profile owner/content-family
readback, Telegram receipt, and same-slot replay with zero new effects. The
frozen mapping is Anicca EN Instagram `@anicca.affirmation`/Larry,
`@anicca.encards`/card, `@anicca.en`/widget; Anicca EN TikTok
`@anicca_slideshow`/slideshow; Anicca JA Instagram `@anicca.jp1` (native
`@anicca.ios.jp`)/card, `@ani.cca1234`/Larry,
`@anicca.jp.videos`/widget; Anicca JA TikTok `@anicca.he`/card,
`@anicca.jp4`/card, `@anicca.jp`/Larry carousel; and Honne TikTok
`@honne_reveal`/EN relationship-confession and `@honnevideo`/JA
relationship-confession. Their integration IDs remain the exact values in
`config/marketing-destinations.json` and the live lane manifest.

The JA-widget boot's missing production command is fixed at
`d7269f70ef550ba056f006dc7412b9f107bfaea2`, released as
`20260902T070901-d7269f70`. Recovery also exposed an operator-trigger defect:
kickstart consumed a due slot and a subsequent explicit slot created one extra
public post on each of `@anicca.he` and `@honne_reveal`. Both posts are valid
for the mapped account/content family, but count as duplicates for this
recovery. No public deletion is authorized. Ebook/monk remains 0/day.

**Current incident correction:** the native screenshots and live integration
readback reopen Order 25 MKT-10 before MKT-11. The fixed program order is not
reordered; MKT-10 is incomplete. R0 through R5 are now terminal. The accepted
Larry correction merged as `01dc472e…`. The R3 closure snapshot recorded all
thirteen loaded marketing labels at immutable main-derived release
`20260831T004335-01dc472e`. Later control-plane readback at `2026-08-31
01:00:24 JST`, after an unrelated main-derived release advanced the host-wide
selector, recorded twelve labels at `20260831T005035-fc39c2a6` and Honne JA at
`20260831T004335-01dc472e`. Both are sealed main releases containing R4. The
selector is the source for a future `lm-loop apply`, not a requirement that
installed labels continuously chase unrelated main commits. Per-label loaded
argv plus sealed release metadata is the execution SSOT; a timestamped closure
snapshot is evidence, not a permanent claim about the next independently
applied release.
The first unchecked subcursor is MKT-10R7. MKT-11, MKT-12, and MKT-13 stay
blocked until MKT-10R7 closes.

| Subcursor | One-at-a-time work | Done evidence |
|---:|---|---|
| MKT-10R0 | **done —** freeze the current provider/manifest/native contradiction without publishing | immutable evidence `d2ca4981…303b8` records all 7 screenshot hashes/handles, all 30 live Postiz integrations with the exact matching rows, lane-manifest hash `a67070b1…8b00`, loaded release `557f1b59`, and provider writes 0. It proves `@anicca.videojp` and `@anicca_girl` are absent from Postiz while `@anicca.jp1` is an enabled Instagram integration, not TikTok |
| MKT-10R1 | **done —** make one machine-readable destination contract the SSOT | `config/marketing-destinations.json` fixes 12 mobile-app targets to exact product/job-product, locale, platform, Postiz alias, native handle, integration, renderer, job format, media form, approved pack name/ref, three JST slots, loop name, label, and entrypoint. Browser readback fixes the two renamed Instagram owners; `@obou.anicca` is removed from the mobile portfolio |
| MKT-10R2 | **done —** close account presence before content work | current provider audit covers all 30 live Postiz integrations exactly once as 12 mobile targets plus 18 integration holds, with three additional absent/platform-collision holds. `@aniccaaffirmation`, `@anicca-ai`, `@anicca_buddha`, `@anicca.videojp`, TikTok `@anicca.jp1`, and `@anicca_girl` remain explicit 0/day until their exact owner/integration gate closes; uncovered live integrations and route errors are 0 |
| MKT-10R3 | **done —** repair the control-plane drift fail-closed | PR `#3332` merged at `01dc472e…`. The R3 closure snapshot recorded all 13 destination labels at `20260831T004335-01dc472e` with exact loop ids; later `2026-08-31 01:00:24 JST` readback recorded 12 at descendant `20260831T005035-fc39c2a6` and Honne JA at `01dc472e…`. Each loaded argv pins one existing immutable main-derived release containing R4; installed labels need not equal the moving global selector. R3 install events created no publication. Manifest `4992c334…fc33` is 13/13 `default-off`, armed 0, with 17 holds; fence is closed |
| MKT-10R4 | **done implementation/review/release; live proof belongs to R6 —** revalidate Larry assets and account routing | `@anicca.jp` is fixed to Postiz TikTok `cmp9sdev5012voh0y58qs45xc`, renderer/job format `larry`, form `affirmation-carousel`, and immutable pack `63e2b1b8…3664`. Six ordered 1010×1080 sunset JPEGs contain no person/ReelClaw media. The released adapter rejects invalid dimensions before secret/provider access; both Python transport and JS provider/receipt/reconcile paths reject a bare TikTok `/video/` URL and accept only exact `PUBLISHED` photo proof with matching integration, caption SHA, title, `DIRECT_POST`, and strict `p_pub_url~v2.<digits>`. Ordinary TikTok video and Instagram `/p/` routes remain intact. Focused Node 51/51 and transport 24/24 pass; fresh read-only verdict is `ship`; all eight CI checks pass. Native publication, Telegram, metrics registration, and replay-zero remain R6, so this is not yet a working-loop claim |
| MKT-10R5 | **done read-only routing closure —** Revalidate ReelClaw/Honne/Anicca video routing | all 13 approved pack objects exist at their exact SHA refs. Every video pack readback matches its destination product, locale, format/form family; destination selection binds job-product + locale + platform + integration + format + form and rejects Honne/Card/integration cross-family substitutions. Focused destination/manifest/ledger suite passes 45/45 with provider writes 0. Mixed historical feeds remain quarantined |
| MKT-10R6 | **done —** prove the production slot contract one destination at a time through the natural owner | all 12 retained routes were armed one at a time, official `lm-loop start` kickstarted, then exercised with one explicit unique slot when outside the due window. Each reached its exact Postiz integration and public native owner/content family; same-slot replay created zero new publication and zero duplicate Telegram. The two renamed Instagram aliases resolve to native `@anicca.ios` and `@anicca.ios.jp`; TikTok photo/video propagation IDs were read from the public profile rather than inferred from Postiz IDs. The slot-scoped publication fixes are merged in immutable main release `78350d1a…` |
| MKT-10R7 | **done — all thirteen retained destinations system-closed** | The terminal cursor below is authoritative: every retained Anicca/Honne TikTok/Instagram target is loaded at exactly three CalendarIntervals/day with exact integration, native owner/content readback, one real publication, and replay-zero. `@anicca.videojp` is not one of the thirteen connected targets and remains a separate 0/day hold. |

**MKT-10R7 live cursor override:** Instagram Postiz alias `@anicca.jp1` / native `@anicca.ios.jp` is terminal at 3/day under manifest `1bfb1ea7…a2b7`: JA Card Reel `https://www.instagram.com/reel/Dcw45GEkkSn/`, Postiz `cmtjbpx83035ppn0yqlg26mtl`, CalendarIntervals `08:10/13:10/19:10`, kickstart exit 0, and immediate replay publication/Telegram `created:false`. The first active atom is now TikTok `@anicca.jp4` / `cmn8x8hdv028uqx0y4gdfse5t`; this sentence supersedes the row's prior first-atom wording without reordering later atoms.

**MKT-10R7 live cursor 2:** TikTok `@anicca.jp4` is terminal at 3/day under manifest `56caaf24…9c0c3`: exact Postiz `cmn8x8hdv028uqx0y4gdfse5t`, JA Card pack `dd0dc38d…ed3f`, native `https://www.tiktok.com/@anicca.jp4/video/7680693831918077972`, Postiz `cmtj88gjf02efpn0yy8p1a0pj`, and CalendarIntervals `09:15/15:15/20:45`. Its 08:59 kickstart was before the first due window and correctly created provider effects 0 (`marketing cycle has no due slot yet`). The first active atom is now Instagram `@ani.cca1234` / `cmq3sq7mc000eqp0y7azfm8yk`.

**MKT-10R7 live cursor 3:** Instagram `@ani.cca1234` is terminal at 3/day under manifest `00481c56…674a`: exact Postiz `cmq3sq7mc000eqp0y7azfm8yk`, JA Larry six-image pack `3d6acc97…b79e`, native `https://www.instagram.com/p/DcwsWPygVDG/`, Postiz `cmtj7tads02b0pn0y5kaartu9`, and CalendarIntervals `10:30/16:30/22:30`. Its pre-10:30 kickstarts correctly created provider effects 0 (`JA production has no due slot yet`). The first active atom is now TikTok `@anicca.jp` / `cmp9sdev5012voh0y58qs45xc`, whose allowed content is the sunset Larry carousel and never the Buddha male pack.

**MKT-10R7 live cursor 4:** TikTok `@anicca.jp` is terminal at 3/day under manifest `bd045904…9e07`: exact Postiz `cmp9sdev5012voh0y58qs45xc`, sunset Larry pack `63e2b1b8…3664`, CalendarIntervals `08:00/16:00/22:37`, and live Postiz `cmtjc110501pes60yhnac0v47` / TikTok photo ID `7680721569441777684`. Pack/media hashes prove sunset content rather than the Buddha male pack. Immediate replay returned publication/Telegram `created:false` and provider receipt delta 0. The first active atom is now Instagram `@anicca.jp.videos` / `cmmzzg2es0539p30ycb94ayx0`.

**MKT-10R7 live cursor 5:** Instagram `@anicca.jp.videos` is terminal at 3/day under manifest `14153b9b…2713`: exact Postiz `cmmzzg2es0539p30ycb94ayx0`, JA Widget pack `00dcff6f…95ed`, CalendarIntervals `08:05/13:05/18:20`, and live Reel `https://www.instagram.com/reel/Dcw6MWljNYd/` / Postiz `cmtjc4sit03capn0yl8pzvgsy`. Immediate replay returned publication/Telegram `created:false`. All eleven connected Anicca targets are now production-armed at 3/day; `@obou.anicca` is not a retained target, while missing `@anicca.videojp` remains 0/day. The first active atom is now Honne EN TikTok `@honne_reveal` / `cmoig11ew001zlv0yk6vqo1us`.

**MKT-10R7 live cursor 6:** Honne EN TikTok `@honne_reveal` is terminal at 3/day under manifest `5025f5a8…42ba`: exact Postiz `cmoig11ew001zlv0yk6vqo1us`, relationship-confession ReelClaw pack `0c28dee0…7706`, CalendarIntervals `07:00/11:00/20:30`, and native `https://www.tiktok.com/@honne_reveal/video/7680694952506099732` / Postiz `cmtj8e61p02fvpn0ydj48cpjv`. Kickstart exited 0 with public URL HTTP 200, provider reconciliation true, publication replay false, and Telegram `created:false`. The first active atom is now Honne JA TikTok `@honnevideo` / `cmnit95mg015rrm0ye5vm8dhl`.

**MKT-10R7 live cursor 7 — terminal override:** Honne JA TikTok `@honnevideo` is terminal at 3/day under manifest `8ec307f3…af72c`: exact Postiz integration `cmnit95mg015rrm0ye5vm8dhl`, Honne JA pack `d550a0a6…86ab`, CalendarIntervals `08:30/12:30/21:30`, and native `https://www.tiktok.com/@honnevideo/video/7680723828586170388` / Postiz publication `cmtjcebpa03e9pn0yst1kmlze`. Immediate same-slot replay returned generation/publication/Telegram `created:false`, so it produced no duplicate provider or notification effect. This terminal cursor supersedes the MKT-10R7 table row's earlier `active` wording: all 13 retained Anicca/Honne mobile targets are loaded, production-armed at exactly three CalendarIntervals/day, and point to immutable release `20260902T085615-e9d59c32`; all 17 non-target holds remain manifest-unarmed at 0/day. Missing TikTok `@anicca.videojp` has no exact Postiz integration and remains a hold rather than borrowing another account. YouTube, ebook, and monk remain outside this completed mobile-lane gate.

**MKT-10R7 Buddha corrective cursor — terminal:** TikTok `@anicca_buddha`
is bound to Postiz `cmp9txjdp01c8oh0yb6dhlarr`, Anicca iOS / JA / Larry
six-image affirmation carousel, and the immutable eyeglass-wearing male
restaurant pack `5459a684…017e`; sunset, woman, ReelClaw video, and generic
Buddha imagery remain forbidden. The first activation published one wrong
sunset carousel `7680702456753343765`, retained as incident evidence. The
corrected effect uses Postiz row `cmtja7i520169s60y7tzkmv5z`; all six provider
image bytes match the ordered job media SHA-256 values, and native profile
readback binds the matching caption and male thumbnail to photo
`7680708466381557012`. The provider returned after the wrapper had recorded
the job as failed/unknown, so append-only `resolveReconciliation(present)`
changed that exact job to completed with `unknown_effect=false`; no replacement
publication occurred. Fresh adversarial verification returned `SHIP` and the
owner report is Telegram `48448`. The lane remains configured at
`07:00/13:00/20:00` JST; the next fixed-order item is MKT-11, not another
canary or a replay of this effect.

**MKT-10R8 live cursor 1:** owner classification is frozen as Anicca iOS / Japanese / TikTok `@anicca.videojp` / ReelClaw UGC video. Daily-driver native inspection shows the exact profile, Anicca logo and bio, plus pinned woman-led vertical videos with Japanese captions; sunset Larry, Buddha male and photo-carousel families are forbidden. A fresh live Postiz API read still has 30 integrations and no exact `@anicca.videojp` row; historical URLs for all plausible hold integrations resolve to their own handles rather than this target. TikTok Web `@anicca.jp` credentials do not switch to this separate account. A private recovery email identifies `@aniccaaffirmation`, so its code was not used; the next email attempt hit TikTok's temporary recovery-rate limit. Until exact OAuth ownership exists, this target remains manifest-unarmed at 0/day and no provider post is authorized. Next atom: recover the target session or connect it through Postiz, then freeze the returned integration ID before code, cadence or publication changes.

**MKT-10R8 live cursor 2:** Gmail history proves `m.05eg95@bumpmail.io` is the owner alias for exactly `anicca.videojp`; fresh recovery emails explicitly greet `anicca.videojp`. Postiz login and Add Channel → TikTok OAuth are ready. TikTok accepts fresh email-code requests on new device IDs, but password-reset submission currently returns account-level `error_code=7`, `試行回数が上限に達しました。後ほどお試しください。`; no password change or Postiz integration was created. The private credential row is marked `recovery-pending`, not verified. The next atom remains one exact action: after the TikTok reset limit clears, finish alias recovery and OAuth (or authorize the prepared Postiz QR from the already logged-in iPhone), read back the new integration as profile `anicca.videojp`, then proceed to manifest/code/cadence/publication. All provider effects remain 0 for this target.

**MKT-10R8 terminal owner override:** skip this account for now. Preserve the
verified mapping `@anicca.videojp` → Anicca iOS / Japanese / ReelClaw UGC video,
but keep it as an explicit missing-integration hold at 0/day. No OAuth retry,
manifest arm, cadence, publication, or borrowed integration is allowed in the
current pass. This is a deliberate skip, not a successful posting claim. The
first active atom advances to classification of the existing YouTube
integrations; ebook and monk remain later at 0/day.

**YouTube live cursor 1:** the Postiz API returns exactly three YouTube rows:
`@anicca-ai` / `cmq3u37gi005iqp0y90a2w92n`,
`@anicca-affirmation-video` / `cmn8ymq6c02oio70y5ea1trv8`, and
`@anicca-jp` / `cmn1oukj9012nnq0yqhouc3ib`. All three provider connections
currently report `disabled:false`, but all three remain Life Manager-unarmed at
0/day; provider connectivity is not a posting-owner claim. Work stays atomic:
(1) classify and canary `@anicca-ai`; (2) classify and canary
`@anicca-affirmation-video`; (3) preserve the existing owner-skip for
`@anicca-jp` at 0/day unless the owner explicitly reopens it. No Honne content
or identity may use these Anicca YouTube integrations.

**YouTube live cursor 2 — `@anicca-ai` classified:** native YouTube readback
binds handle `@anicca-ai` to channel ID `UCFGkrtiSTni-AZ9DUY6Q5EA` and Postiz
integration `cmq3u37gi005iqp0y90a2w92n`. Its latest twenty Shorts all use the
title `how to put affirmations on your lockscreen`; direct frame inspection of
the latest three shows the same woman-led English UGC hook, `Put affirmations
on your lockscreen`. The exact mapping is therefore Anicca iOS / English /
YouTube Shorts / `reelclaw-widget` / `lockscreen-affirmation-widget`. Larry,
Card, Japanese, Honne, and the skipped JA YouTube canary assets are forbidden.
The lane remains Life Manager-unarmed at 0/day while the next atom reuses the
approved EN Widget pack lineage to run one exact Postiz/native canary, then
proves Telegram and same-slot replay-zero before any cadence arm.

**YouTube live cursor 3 — canary verified; scheduler install pending:** main PR
`#3909` adds exact destination `anicca-ios-en-widget-youtube`, Postiz
`cmq3u37gi005iqp0y90a2w92n`, the approved EN Widget pack
`79e8fe12…701a1`, and three JST intervals `07:45/13:15/19:45`. Focused
destination/manifest/cycle checks pass 29/29 and main-derived immutable release
`20260902T105547-bc9ffdee` passes the same checks despite non-fatal npm ENOSPC
warnings during unrelated dependency extraction. The one exact canary is public
at `https://www.youtube.com/watch?v=P_Epsp57RbY`, Postiz
`cmtjgc5yc0429pn0yrpr45yhj`, Telegram `48262`. Native metadata binds channel
ID `UCFGkrtiSTni-AZ9DUY6Q5EA`, handle `@anicca-ai`, public availability, and
English title/description; 1s/8s/14s native frames match the source Widget
object. Same-slot replay returns generation/publication/Telegram
`created:false`. Manifest `8cba262c…15d81f` contains 14 targets / 16 holds,
marks this lane `canary-verified`, and
arms only this newly classified integration in addition to the prior thirteen.
The LaunchAgent is not yet installed: targeted `lm-loop apply` fails closed
before plist mutation because this Codex app-server context reports
`id -un=501`, manager rc 153, and `gui/501` rc 141. Do not claim the three/day
cron is live until a GUI/Aqua context runs targeted apply, then kickstart and
reads back the exact loaded release argv and replay-zero. No retry publication
is required.

**YouTube live cursor 4 — exact remaining atom:** host storage is healthy again
(26 GiB free), so disk capacity is not a blocker. The canary, native owner and
content-family verification, Telegram receipt, manifest arm, and replay-zero
are already terminal and must not be repeated. The only unfinished action for
`@anicca-ai` is to run targeted `lm-loop apply` from a valid GUI/Aqua launchd
context, kickstart the installed `life-manager-anicca-ai-youtube` loop, and
read back its plist/loaded argv as immutable release
`20260902T105547-bc9ffdee` with exactly `07:45/13:15/19:45` JST. This isolated
Codex context still reports numeric `id -un=501`, missing manager context, and
`gui/501` rc 141, so the official apply continues to fail closed before plist
mutation. After that single scheduler atom closes, the next fixed-order atom is
classification and canary of YouTube `@anicca-affirmation-video` /
`cmn8ymq6c02oio70y5ea1trv8`; `@anicca-jp` remains owner-skipped at 0/day, and
ebook/monk remain later.

**YouTube live cursor 5 — GUI bridge unavailable:** a target-only helper was
prepared to run the official apply/start/status commands from Terminal, but it
never started. AppleScript rejects Terminal's `do script` command in this
isolated app-server, LaunchServices cannot resolve Terminal by name, and the
absolute Terminal.app open fails before launch. The target plist remains absent
and publication effects remain unchanged. This confirms the remaining scheduler
atom requires an external state change: a valid logged-in GUI/Aqua execution
context. Do not bypass `lm-loop`, hand-write the plist, run raw `launchctl`, or
advance the next YouTube lane while this fixed-order atom is open.

**YouTube live cursor 6 — `@anicca-ai` terminal at 3/day:** after the Codex
harness returned to a healthy Aqua context (`id -un=anicca`, manager `Aqua`,
`gui/501` rc 0), target-only `lm-loop apply` installed and loaded
`ai.anicca.life-manager-anicca-ai-youtube`. Its plist and live argv point to
immutable main release `20260902T131725-29fb7681` / SHA
`29fb7681b3e3983870c105db8ec7907282f894f8` and contain exactly
`07:45/13:15/19:45` JST. Official kickstart consumed the 13:15 slot once:
YouTube `https://www.youtube.com/watch?v=vFptviO7Gfo`, Postiz
`cmtjlbdvf00kvpn0yi13wq6lu`, Telegram `48283`. Native readback proves public
15-second content on uploader `@anicca-ai`, channel ID
`UCFGkrtiSTni-AZ9DUY6Q5EA`, English self-belief/affirmation copy, and the
approved Widget family. The run terminated `pass` on the installed SHA and
returned loaded-idle. A second official kickstart of the same due slot also
terminated `pass`; generation/publication/Telegram all returned
`created:false`, and the exact publication ledger remained three lifecycle
rows plus one receipt. This supersedes cursors 3–5's pending scheduler state.
The first active atom is now classification and canary of YouTube
`@anicca-affirmation-video` / `cmn8ymq6c02oio70y5ea1trv8`; `@anicca-jp`
remains owner-skipped at 0/day, and ebook/monk remain later.

**Shared-memory and checkout guard:** the canonical source checkout is
`/Users/anicca/Projects/life-manager-main`, backed by
`Daisuke134/life-manager`; production still executes only sealed main-derived
releases. `/Users/anicca/anicca` is an older dirty checkout of the same remote,
not the source SSOT. It must not be deleted yet because live LaunchAgents still
reference its marketing metrics, x402, Franklin, and self-improvement paths.
Retirement requires an inventory, one-owner-at-a-time migration to registry
managed main releases, preservation of dirty runtime evidence, and a final
loaded-reference count of zero. This cleanup does not reorder the active
marketing cursor. The Codex runtime lesson is now discoverable in repo-root
`AGENTS.md`, durable `MEMORY.md`, and the daily memory note, in addition to the
machine-wide generated Codex rules.

**YouTube live cursor 7 — affirmation alias classified:** live Postiz GET
returns exactly one enabled YouTube integration
`cmn8ymq6c02oio70y5ea1trv8`, named `Daily Affirmation App - Anicca`, under the
provider alias `@anicca-affirmation-video`. That alias is not the current
native handle: its URL returns 404. Published rows from the same integration
resolve to native channel `Life Manager`, handle `@life-manager-m4p`, channel
ID `UCqCLudkgpJZp4Pvi6ELGalw`; four independent published URLs agree on that
identity. The current native Shorts feed starts with `UW9Y0b3dlpI`,
`iCmr_9WbeT4`, `smNfGW6ibs4`, and `8ggJw-SNeAc`. Their titles and descriptions
all advertise Anicca on the App Store in English. Direct frame inspection of
the latest three shows a recurring 3D adult male character, English white
captions, and short mental-health/self-compassion narratives, including mirror
self-talk and eating-disorder recovery. Freeze this lane as Anicca iOS / EN /
YouTube Shorts / renderer and job format `reelclaw-card` / form `nudge-card`.
The retired `anicca-youtube-daily` source independently confirms that this
exact integration consumed the newest `reelclaw-card-en` `reel-final.mp4` and
names published `rZ2ojg0EAYk` as its live reference. It is not Widget, Larry/photo slideshow,
Card, sunset, woman-led UGC, Honne, or monk content; those families are
forbidden on this integration. Native YouTube exposes no pinned-post marker for
these Shorts, so classification rests on the current ordered recent feed plus
the integration-bound published URLs. Provider writes, publication effects,
Telegram effects, and lifecycle commands remain zero. The next atom is the
single-integration binding; no other YouTube integration may be borrowed.

**YouTube live cursor 8 — account-bound pack ready:** the shared EN Card pack
was rejected after direct frame inspection because it contains woman-led UGC
plus green in-app affirmation cards, which does not match the current native
`@life-manager-m4p` feed. A YouTube-only migration pack instead binds the
directly inspected 24-second 1080x1920 H.264/AAC 3D male recovery narrative to
Anicca iOS / EN / `reelclaw-card` / `nudge-card`. Exact refs are pack
`object://sha256/42b2cd00440e0998947700ed0f3dc688f2ab737829b4f03e5f1bcfae9d2e6f62`,
video `object://sha256/a3082d1e1ae3c52842e05c47a90dee2779dde08a515175f72f575cf729328423`,
and caption
`object://sha256/6c3d42b021fe01717d22b858a0bcb65a03fe8af59c02856e02f8dba39f827cc1`.
All three object hashes read back exactly; the video object is mode `0600`.
The named import is `anicca-ios-reelclaw-card-en-youtube.pack.json`. This first
pack is migration input for the controlled canary, not permission to repeat the
same creative forever; the production loop must rotate or generate fresh
matching EN 3D male narratives while preserving the frozen account family. No
provider, Telegram, scheduler, or lifecycle effect occurred.

**YouTube live cursor 9 — default-off destination implemented:** destination
SSOT now promotes only Postiz alias `@anicca-affirmation-video` / integration
`cmn8ymq6c02oio70y5ea1trv8` from hold to retained lane
`anicca-ios-en-card-youtube`, while freezing native handle
`@life-manager-m4p`, pack `42b2cd…e6f62`, `reelclaw-card` / `nudge-card`, and
JST `08:15`, `14:15`, `20:15`. Registry label
`ai.anicca.life-manager-anicca-affirmation-youtube` points to the dedicated
thin boot script, which reuses the existing account-bound cycle through command
`run-anicca-affirmation-youtube`; existing `@anicca-ai` Widget and held
`@anicca-jp` routes are unchanged. JSON, shell syntax, Node syntax, destination
audit, registry audit, and the focused shared-cycle contract pass 13/13. The
named pack SHA is exact and its mode is `0600`. This is source configuration
only: provider posts, Telegram messages, installed release changes, lifecycle
commands, and runtime activation remain zero pending the controlled canary.

**YouTube live cursor 10 — canary preflight ready, natural release pending:**
private runtime state now contains only this lane's three required non-secret
object references: pack `42b2cd…e6f62`, media `a3082d…28423`, and standing
approval `07ac08ab…0717b`. The approval is account-bound to Anicca iOS / EN /
YouTube / `reelclaw-card` / `@life-manager-m4p`; its exact object and the
private env remain mode `0600`. Existing Postiz, Telegram, tenant, and data-root
keys are present without being copied into source or evidence. Read-only
ledger search finds no pending or completed Life Manager effect for the new
integration, lane, native handle, or selected media, so there is no ambiguous
publication to retry. PR #3936 merged to main at `5d27ad50…`; the immutable
selector still reads prior main `8f956147…` after a five-minute observation
window, so the 14:15 slot did not run this new lane. This is a verified wait,
not a canary success and not proof of watcher failure. Remote performed no
apply, kickstart, lifecycle, provider, or Telegram action. TODO 5 remains
active until the independent release owner advances and one exact natural
effect is verified.

**YouTube live cursor 11 — existing Life Manager rollout retained:** the lane
already exists on `origin/main` with its exact destination, registry row,
dedicated entrypoint, integration, pack, native handle, and three daily slots.
No OpenClaw clone, new scheduler architecture, or new release-owner design is
required. The observation that the current immutable selector still precedes
this lane proves only that rollout had not reached this label at that instant;
it does not prove that Life Manager lacks its established rollout process.
Follow the same main-derived immutable-release and one-label rollout used by
the working Anicca/Honne lanes, then let the real lane perform the canary and
natural schedule. The retired Coconala-specific
`ai.anicca.hf-gig-release-watch` remains unrelated and must not be restored.
Remote still does not call `launchctl`, `lm-loop apply/start/stop/restart`, raw
launchctl, Terminal/AppleScript bridges, signals, or app/Mac restart. Provider
and Telegram effects remain zero until the real lane runs.

**Next atomic TODO — fixed order:**

1. **Done:** resolve Postiz alias `@anicca-affirmation-video` to native
   `@life-manager-m4p` / `UCqCLudkgpJZp4Pvi6ELGalw` and freeze Anicca iOS / EN /
   `reelclaw-card` / `nudge-card` with the
   forbidden families above.
2. **Done:** live Postiz registry and integration-bound published URLs bind only
   `cmn8ymq6c02oio70y5ea1trv8`; neither other YouTube integration is borrowed.
3. **Done:** account-bound YouTube pack and exact video/caption refs are recorded
   in live cursor 8; the mismatching shared Instagram pack remains excluded.
4. **Done:** the default-off destination, registry row, dedicated boot route,
   shared-cycle lane, exact three slots, and focused contract are recorded in
   live cursor 9; no runtime or provider effect was claimed.
5. **Active:** use the established Life Manager main-derived immutable-release
   and one-label rollout, then run one natural controlled canary and verify
   Postiz, native handle/content, Telegram, and same-slot replay-zero. No new
   owner architecture or OpenClaw clone is part of this atom.
6. Arm exactly three daily intervals in source. Remote does not call lifecycle
   commands; the independent release watcher applies the release and the loop's
   natural wake runs it. Read back plist, loaded argv, immutable release,
   terminal event, and provider effect from artifacts.
7. Keep YouTube `@anicca-jp` owner-skipped at 0/day. Only after this YouTube
   atom is terminal may the ebook/monk work begin.

**141 prevention invariant:** Remote ChatGPT/Codex/Claude sessions never call
`launchctl`, `lm-loop apply/start/stop/restart`, app-server daemon lifecycle,
remote-control lifecycle, Terminal/AppleScript bridges, or OS/app restart.
They do not probe 141. Existing release watchers and natural loop wakes own all
host lifecycle; Remote only observes process, ledger, receipts, and public
effects. Historical 141 means an invalid harness context, never a reason to
shutdown, restart, signal a Remote-owned process, or block the business TODO.

#### MKT-10R6 production-slot repair contract

1. **Overview:** production launchd wakes currently omit the slot required by the canary CLI, so every loaded marketing destination can appear installed while failing before publication.
2. **Acceptance criteria:** one and only one approved destination is `production-armed`; its natural wake binds the exact configured JST calendar instant to `--slot`, reaches the matching Postiz integration, records a direct native publication receipt with correct media lineage, sends one natural Telegram receipt, registers the metrics owner, and an immediate replay creates zero provider effects and zero duplicate Telegram messages. Every other target and every hold remains 0/day.
3. **As-Is / To-Be:** As-Is is 13 loaded labels at `439dc71d…`; ten had never exited and three had one exit-1 run before the ref repair, with manifest armed 0. The owner-path canary now has one `PUBLISHED` provider receipt and one Telegram receipt, and its same-owner replay adds zero receipts; the manifest is restored armed 0/fence closed. To-Be is still one CalendarInterval natural terminal for the active lane with independent provider/native account readback and replay-zero; no global mass-arm is allowed.
4. **Test matrix:** focused tests MUST cover exact slot derivation at each configured calendar boundary, rejection of an absent/off-schedule/mismatched slot, one-lane-only manifest gating, and replay-zero. Natural E2E MUST cover the active destination's Postiz row, native account/content readback, Telegram receipt, and metrics registration.
5. **Boundaries:** no direct Codex publication, no off-schedule substitute executor, no retry after unknown effect, no OpenClaw/Larry legacy cron restoration, no simultaneous multi-account canary, no targetless/all-label apply, no mutation of the held Capafy daily/healthcheck jobs, and no MKT-11/MKT-12/MKT-13 work before R7.
6. **Execution:** focused RED/GREEN on the production boot/CLI boundary; merge to main; build one verified immutable release; apply only the active destination label; wait for its next exact natural slot; verify terminal/provider/Telegram/metrics/replay evidence; then repeat destination-by-destination in contract order.

**R6 owner-path evidence:** the first attempt failed closed on stale private
Larry pack/media/approval refs; the second attempt failed closed because the
temporary canary helper opened the publication fence while production requires
`production-armed` with a closed fence. After restoring the exact default-off
manifest, aligning only the four non-secret object refs to the approved
`JA_MAIN_TIKTOK` lane, and arming that lane with the fence closed, the existing
LaunchAgent owner completed one Postiz photo publication and one Telegram
receipt. Postiz readback was HTTP 200, `PUBLISHED`, profile
`https://www.tiktok.com/@anicca.jp`, integration
`cmp9sdev5012voh0y58qs45xc`, and provider row `cmthai3x7083yoc0yeki4ofqe`.
The provider/photo contract intentionally carries `public_url=null`; a direct
native TikTok photo URL and the next CalendarInterval wake remain the open R6
gates. No sibling launchd label, plist, release, or non-marketing state was
changed, and the target manifest was returned to 13/13 default-off.

| E2E item | Value |
|---|---|
| UI change | none |
| Judgment | Maestro not required; real launchd plus Postiz/native/Telegram/metrics readback is required because the failure is a production scheduler-to-provider contract |

### 12.2.1 Local folder and deletion boundary

The local repository folder is `/Users/anicca/Projects/life-manager-main`.
Production does not execute that mutable checkout directly; launchd points to
an immutable Life Manager release selected through `/Users/anicca/loops/current`,
and mutable state/credentials/ledgers live under
`/Users/anicca/.local/state/life-manager`.

```text
/Users/anicca/
├── Projects/life-manager-main/       canonical Git checkout; source and spec
├── loops/current -> immutable release production selector
├── .local/state/life-manager/        private state, objects, ledgers, receipts
├── .openclaw/                        live gateway/ask/peer runtime owner
├── anicca-project/                   loaded Codex progress reporter owner
├── profitable-claude/                legacy source still referenced by installed plist and scan
├── gig/                              Coconala runtime/state/private bundle and secondary release root
├── lm-loops-core/                    separate development worktree
├── life-manager-repo-v0-retire/      retained v0 migration/evidence checkout
├── Projects/life-manager-{8i-cutover,eliza-migration,eliza-worktrees,symphony-workspaces}/
│                                      retained migration/workspace roots
└── reelclaw-ja-20260317-1330/        retained generated legacy media/evidence
```

Deletion is not authorized or safe. Current loaded LaunchAgents still execute
`.openclaw` for `ai.openclaw.anicca-ask`, `ai.openclaw.gateway`, and
`com.anicca.peer-api`, and execute `anicca-project` for
`com.anicca.codex-progress-reporter`. The installed but currently unloaded
`ai.anicca.freelancer-bid-watch.plist` still names `profitable-claude`.
The production legacy-path scanner still reports 52 violations across 711
files, including marketing defaults that read `.openclaw` state/env and a
Profitable Claude marketing command. Cleaning directory contents does not
close these executable references or their protected state/evidence. The deletion
gate is: loaded references zero, production dependency scan zero, clean-user
install pass, immutable-release natural cycles pass, official effects and
replay-zero pass, and protected state/session/evidence backup readback passes.
Until every gate is true, no listed folder is disposable as a set.

Current read-only production matrix separates installed state from outcome proof:

| Surface | Loaded generation / launchd | Latest terminal or effect evidence | Verdict |
|---|---|---|---|
| Marketing 13 destinations | all 13 labels load `20260831T192828-7ab5a318`; each runs 0 / never exited | latest events are install-only with `effect_status=unknown`; manifest 13 `default-off`, armed 0, holds 17; latest receipts are generated artifacts, not Postiz/native publications | **UNPROVEN / OFF** |
| Capafy daily | `ai.anicca.capafy-loop-daily` loads authoritative `20260831T193835-aced5c6b`; runs 0 / never exited | natural daily terminal absent | **PENDING NATURAL TERMINAL** |
| Capafy healthcheck | same `aced5c6b`; initially runs 4 / last exit 0, but the read-only 20:22 JST refresh shows runs 7 / last exit `78: EX_CONFIG` | recover a natural health terminal exit 0 without reloading, restarting, stopping, or applying either held Capafy label | **FAIL — CONFIG** |
| Connector | `ai.anicca.life-manager-connector-native` loads `7ab5a318`; runs 1 / last exit 2 | latest global event `entrypoint_exit_2`; no completed Calendar/Telegram/bundle chain | **FAIL** |
| Coconala Apply / Browser / Reply | `hf-gig-*` owners load `7ab5a318`; Apply last exit 0, Reply last exit 0, Browser long-running | no single current four-lane official terminal/replay manifest; process liveness is not marketplace completion | **PARTIAL / UNPROVEN** |
| Coconala Paid / Storefront | same `7ab5a318`; Paid last exit 1, Storefront last exit 1 | current owner terminals are nonzero; official outstanding project closure/replay remains open in `skills/earn/gig/TODO.md` | **FAIL** |
| Codex account routing | current agent-runner config orders `acct1` before `acct2`; durable model events show `profile_alias=acct1` successes and no `acct2` event | Account 1 use is proven, but no same-run `acct2 depleted → acct1 selected` receipt exists | **FAILOVER UNPROVEN** |
| Legacy independence | production legacy scanner reports 52 violations across 711 files | `.openclaw`, legacy anicca roots, and `profitable-claude` remain named by runtime paths | **NOT SELF-CONTAINED** |

The loaded Coconala owners are especially not independent yet. Apply, Browser, Reply, Paid, Daily Report, and Storefront plists still use `.openclaw` for logs or mutable state, use `/Users/anicca/gig` for runtime/private state, and most point `LIFE_MANAGER_REPO` at `/Users/anicca/gig/releases/life-manager/current` instead of the canonical checkout/release selector. Paid also retains `profitable-claude` and `.openclaw` inside its sandbox-deny boundary. These references name active consumers, so neither `.openclaw`, `gig`, nor `profitable-claude` is deletable.

The recovery baseline from immutable release `efe75bfc…fc20b` reports 232
inventory rows: 166 managed, 44 retired, 81 managed rows whose latest terminal
event is `fail`, three managed rows unloaded, and 62 managed effects still
`unknown`. Only 27 managed labels were installed from that current release at
the readback boundary. These are direct blockers to an “all loops are working”
or “the legacy folders are deletable” claim; loaded/running state alone does not
override a failed terminal event or unknown official effect.

For every selected Postiz video/Reel/carousel lane, publication success now
requires the exact Postiz API row in `PUBLISHED` state plus the exact LM
product/account/integration/platform/creative/local-media/caption lineage. A
profile URL or numeric release suffix without that exact row and lineage is not
success. A caption-matching native URL and browser/login readback are optional
diagnostics, not publication gates. No Google login is part of this recovery.

**Historical pre-incident execution snapshot — superseded by the current
§12.2 MKT-10R0-R7 cursor above.** At that time an external observation clock was
not an active implementation item. It remained `time-gated pending` only when
it had an exact due time, a durable LM owner, and terminal acceptance evidence.
The then-active atomic item was reconciled before any dependent account arm.

**Historical executable atomic item:** MKT-09R9-15 / Order 24F10 reconciled the
existing `@anicca_slideshow` API canary; it created no new publication. For this
TikTok **native photo carousel** lane only, publication acceptance is the exact
Postiz API/DB row in `PUBLISHED` state with matching integration, caption, title,
slot, and DIRECT_POST settings plus the LM-local SHA-verified pack, six ordered
JPEGs, caption, approval, and media-order hash. A direct native photo URL is not
required because Postiz does not expose one for this photo form. This exception
does not weaken video/Reel lanes: those still require their direct native URL.
After durable reconciliation, send one natural Telegram receipt describing the
account/content/Postiz state without inventing a URL, prove same-effect replay 0,
record immediate source status, and register 2h/24h/72h/7d under the durable LM
owner. No other account or cadence begins.

The first 24F10 slot `2026-08-26T04:46:42.000Z` is terminal `absent`, not a
publication success and not retryable as the same job. Exact job
`marketing-native-carousel-publication:87916760…038c6e2` failed attempt 1 in
local pre-provider validation with `carousel images are Instagram-only`; the
exact Postiz integration window contains zero rows, Telegram contains no success
receipt, controls returned to armed 0/fence closed, and durable reconciliation
sets `unknown_effect=false`, `decision=absent`. The shared transport's second
Instagram-only branch is corrected to accept only Instagram/TikTok photo
carousels, preserve the caller's exact platform/title, poll the exact platform,
and require a direct TikTok URL before success. Python regression is 21/21.
After this fix is pushed, 24F10 permits one new-slot attempt only; it does not
reuse or reinterpret the absent effect.

The initial new-slot command then failed locally at enqueue with `local ledger
job id collision`; provider and Telegram effects remain 0. This is the intended
dedupe response because job/effect identity excludes slot. The approved recovery
is a new immutable creative revision
`EN-SLIDESHOW-PROCRASTINATION-05090bf2b4ee-R2` over the same inspected
pack/media/caption, with approval
`object://sha256/6e69c242e75481d2d6a3f51fe2c07e5dc151bb33c9b29f30972e81aa5bf8f668`.
The old absent effect and approval remain retained evidence; no ledger dedupe
rule is weakened and no media/caption changes are smuggled into the retry.

The R2 provider effect exists and is accepted under the photo-carousel rule;
it must not be retried. Postiz row `cmt9mebpj0341mp0ymi31r582` is `PUBLISHED` with the
exact caption/title/integration and numeric release suffix
`7678198747632977937`, while its release URL is still only the profile URL.
The numeric suffix remains provider metadata and is not converted into a URL.
Local job `marketing-native-carousel-publication:0cd8be1c…c788f` is now
terminal `completed`, `reconciliation_decision=present`, and
`unknown_effect=false`. Its verified receipt binds Postiz row
`cmt9mebpj0341mp0ymi31r582`, provider state `PUBLISHED`, integration
`cmnenjkff01j1pa0ysufmzhfr`, exact caption/title/DIRECT_POST metadata, pack
`3241653e…de624c`, six ordered media hashes, order hash `97cb56b3…1c3855`, and
caption hash `8e6f7cec…f2c6d`; `public_url=null` is explicit and no URL is invented.
Telegram/replay/metrics are the next atomic steps.
The shared natural-language renderer now accepts this exact photo proof as
`postiz_published_exact_assets`, says Postiz API `PUBLISHED` and exact approved
local assets/caption matched, and omits a nonexistent public URL. Video/Reel
messages retain their direct-URL validation. Focused liveness/runner tests are
21/21; the next action is replaying the existing slot to create exactly one
Telegram message, followed by a second replay proving message 0/publication 0.

That Telegram/replay gate is now terminal. The LM runner replayed the existing
slot with publication `created=false`, sent one natural receipt as provider
message `34998`, then replayed again with publication `created=false` and
Telegram `created=false` returning the same message ID. The retained publication
receipt count for Postiz row `cmt9mebpj0341mp0ymi31r582` remains exactly one.
The then-active atomic item was immediate Postiz metric-source status plus durable
2h/24h/72h/7d ownership for this photo receipt; no new publication starts.

Metrics code now discovers this exact reconciled photo receipt by job/account/
integration/format/form/locale/caption-object identity and routes it through
Postiz API only. It persists post fields from post analytics, current account
plus latest-20 aggregates from account analytics, and marks every empty or
unsupported field `unavailable` rather than zero. URL-free photo snapshots and
their natural Telegram wording use the same
`postiz_published_exact_assets` evidence. Existing direct-video metrics remain
unchanged. Focused metrics/liveness tests pass 18/18. Next: push, trigger the
existing LM metrics owner, and read back pending/due status plus any immediate
provider source observation.

**Historical remaining-atomic snapshot — superseded by the current MKT-10R0–R7
incident cursor in §12.2; do not execute or infer current arm/cadence from this
list.** The rows below preserve pre-incident receipts only. Current operational
truth was 13 retained lanes default-off/armed 0, with R3 reopened and
`@anicca.jp` routed to Larry sunset native photos rather than ReelClaw video.

1. **Done:** `@anicca_slideshow` metrics: add the exact receipt to the LM Postiz-API
   metrics owner; persist immediate source status and automatic 2h/24h/72h/7d
   windows; report measured account/post fields naturally to Telegram; preserve
   empty/missing fields as `unavailable`, never zero.
2. **Done:** close `@anicca_slideshow` as `canary-verified/default-off`, armed 0, with
   publication present, Telegram `34998`, replay publication 0/message 0, and
   metric-owner evidence.
3. **Done for the selected portfolio:** classify each exact Postiz integration by
   product, locale, account, platform, renderer, content form, and approved asset
   family. Mixed/wrong/unknown routes become hold at 0/day.
4. **Done for the selected portfolio:** import and visually approve one correct
   local pack per account. Bind object hashes, caption, media order, account,
   integration, renderer, and form.
5. **Done for the selected portfolio:** run one API canary for each account only.
   Confirm Postiz `PUBLISHED` and exact
   stored assets/caption; send one natural Telegram receipt; prove publication
   replay 0/message replay 0; register immediate plus 2h/24h/72h/7d metrics.
6. **Done for the selected portfolio:** repeat steps 3–5 one account at a time for
   every retained TikTok and Instagram integration. Do not create missing Honne
   Instagram/YouTube routes and do not relabel Anicca accounts as Honne.
7. **Done-skipped:** the login-dependent Anicca YouTube route remains skipped at
   0/day because no usable API-only route exists.
8. **Active:** enable cadence only for individually healthy verified accounts,
   first one live slot, then the second, then the third after each preceding slot proves
   published/missed/duplicate and metric-source health. The terminal cadence is
   exactly 3/day on every selected available destination. Never mass-enable.
   **Honne EN first live slot done:** the shared ledger now permits an exact
   production-armed lane through the closed canary fence while open fences still
   require their exact effect. LM owner published HEN-016 as Postiz row
   `cmt9peveo00btlf0yb63uguqg`, natural Telegram `35067`, and replay kept
   jobs/receipts at `841/272` with publication 0/message 0 and exit 0. Manifest
   `af258cc29fffd0446bae20967de9388ce13d5e388c9d0ff180dce7d7122486fe`
   arms Honne EN only at target 3/day; every other selected lane remains armed 0.
   **Honne JA first live slot done:** exact creative `HJA-022-ced745f2245a`
   published as Postiz row `cmt9pt0tt00f3lf0y046lm2lo`; the LM receipt binds
   the JA ReelClaw account/integration, video hash `ced745f2…a9472`, caption hash
   `9e407699…7510dd6`, and natural Telegram `35080`. Replay kept jobs
   `847→847`, receipts `274→274`, publication/message `created=false`, and owner
   exit 0. Manifest `65f910db5d5124b6d50bb5864edc63cb82b34c45388180eb1765d66ff7624245`
   arms exactly Honne EN and Honne JA at 3/day. Transport commit `574d0662e`
   derives a TikTok candidate only from an exact `PUBLISHED` Postiz row with an
   accepted TikTok release-id shape; focused Postiz tests pass 21/21.
   **Historical pre-incident Anicca main TikTok live slot; superseded, not a
   current route/arm claim:** LM label
   `ai.anicca.life-manager-anicca-main-tiktok` now runs from the fixed stable
   release instead of the dirty canonical checkout. Manifest
   `6f84fc3e8272357815f6792f6ae74a8c8bfadfa9d9df22abcb17a823372d9ae6`
   arms exactly Honne EN, Honne JA, and `@anicca.jp` at 3/day; 17 holds remain
   0/day. Exact creative `AJ-CARD-002-abbbbdea9052` published as Postiz row
   `cmt9q4n3t00gllf0yvd22ea95`, with video hash `abbbbdea‧66782a`, caption hash
   `04757a25…8d920`, natural Telegram `35095`, and owner exit 0. Replay created
   no publication, distribution row, receipt, or Telegram message; the one
   concurrent job observed during replay was an unrelated `@obou.anicca` 2h
   metrics event. The shared TikTok metrics owner initially failed because the
   stable release lacked `ws`; importing the 196 KiB version 8.21.0 dependency
   from another immutable LM release restored exit 0 without a canonical or
   OpenClaw runtime dependency. This exact post is registered as pending at 2h
   `2026-08-26T08:40:11.030Z`, 24h, 72h, 7d, and daily; no pending value is
   reported as zero or success.
   **Anicca main Instagram first 3/day slot done:** commit `83118d2f8` changes
   only this LM lane from one 19:10 slot to 08:10/13:10/19:10 JST; focused and
   shared ledger/manifest tests pass 46/46. The stable-release LM label and
   manifest `6a0c9fab751a08dc44f0e1aab1db46faa0d5933e69b9ee3636558e4a6f226525`
   arm exactly Honne EN/JA, Anicca main TikTok, and `@anicca.jp1` at 3/day;
   17 holds remain 0/day. The 13:10 creative `AJ-CARD-002-5639e14832ad`
   published as exact Postiz `PUBLISHED` row `cmt9qd07n00knqs0yls15e2xh` and
   Reel `https://www.instagram.com/reel/DcfmdXACEKz/`, with exact media/caption
   lineage, natural Telegram `35112`, and owner exit 0. Replay kept jobs
   `868→868`, receipts `281→281`, distribution `21→21`, publication/message
   `created=false`. Instagram metrics owner exit 0 and registered 2h
   `2026-08-26T08:47:02.846Z`, 24h, 72h, 7d, and daily as pending.
   **Anicca JP4 TikTok first live slot done:** the stable-release LM label and
   manifest `e27bd324732525b81ea82185e5b9ab59c2a6f57443e6f279d20076719da8e53b`
   arm exactly five selected routes at 3/day; 17 holds remain 0/day. The first
   15:15 run correctly rejected shared creative identity already published by
   `@anicca.jp`. A JP4-only immutable pack `dd0dc38d…14e7ed3f` preserves the
   approved hook text/title/hashtags/media/caption but namespaces hook IDs as
   `AJ-JP4-CARD-*`; no dedupe rule is weakened. Creative
   `AJ-JP4-CARD-001-35a15c7ce990` published as exact Postiz `PUBLISHED` row
   `cmt9qojfp00o5qs0ykckq379i` at
   `https://www.tiktok.com/@anicca.jp4/video/7678229828386834453`, with natural
   Telegram `35127` and owner exit 0. Replay kept jobs `877→877`, receipts
   `284→284`, distribution `22→22`, publication/message `created=false`.
   TikTok metrics registered 2h `2026-08-26T08:55:39.858Z`, 24h, 72h, 7d,
   and daily as pending. During registration, URL-free Postiz photo metrics
   incorrectly classified `public_url=unavailable` as Instagram; commit
   `e68a08ade` uses the immutable `tiktok_*` snapshot kind, passes 17/17, and
   real owner exit 0 sent the slideshow 2h natural Telegram `35136` without
   blocking JP4 discovery.
   **Anicca HE TikTok first live slot done:** the stable-release LM label and
   manifest `048847214e3676e1b4252bb4fdd639f67484c37643ab5795a717f48aa4fd39b6`
   arm exactly six selected routes at 3/day; 17 holds remain 0/day. HE-only pack
   `35a31331…8bdf96f` preserves the approved content/media/caption and namespaces
   hook IDs as `AJ-HE-CARD-*`, preventing cross-account TikTok effect collisions
   without weakening dedupe. Creative `AJ-HE-CARD-001-35a15c7ce990` published
   as exact Postiz `PUBLISHED` row `cmt9qwix400n0lf0y0gv7t2ek` at
   `https://www.tiktok.com/@anicca.he/video/7678231818881206290`, with natural
   Telegram `35148` and owner exit 0. Replay kept jobs `892→892`, receipts
   `289→289`, distribution `23→23`, publication/message `created=false`.
   TikTok metrics owner exit 0 registered 2h `2026-08-26T09:01:52.438Z`, 24h,
   72h, 7d, and daily as pending.
   **Anicca EN Card Instagram first live slot done:** commit `4c1871309` gives
   `@anicca.encards` exactly 08:45/12:45/21:30 JST and a dedicated LM label.
   The first kickstart correctly failed before provider with `local ledger job
   id collision`, proving the one-hook/one-media canary pack could not sustain
   daily publication. Commit `1b933f919` adds optional hook-to-approved-media
   binding without changing legacy pack behavior. Direct frame inspection
   rejects Japanese v1 and approves English Card v2/v3/v4; LM-only pack
   `e204995d…e0820e` binds every hook to its exact mode-0600 object, so runtime
   reads no OpenClaw path/env/assets. Creative `EN-CARD-V2-e678c823480f`
   published as exact Postiz API `PUBLISHED` row `cmt9rbish00qylf0yw6gt9ulr`
   at `https://www.instagram.com/reel/Dcfph70jorc/`, integration `cmpc3gx…`,
   matching caption, natural Telegram `35175`, and owner exit 0. Replay kept
   jobs `907→907`, receipts `294→294`, distribution `24→24`, with generation,
   publication, and Telegram all `created=false`. Manifest
   `ce4e9b4d…87f89c` arms exactly seven routes and retains 17 holds at 0/day.
   Commit `845b57c4b` registers the exact EN Card metric lane; the metrics plist
   was corrected from the dirty canonical checkout to fixed release
   `7eb86d63b…f29a5f1`, interval 1800, exit 0. Reel `Dcfph70jorc` now has 2h
   `2026-08-26T09:13:53.435Z`, 24h, 72h, 7d, and daily pending ownership.
   **Next atomic item:** enable and verify only Anicca EN Widget Instagram
   `@anicca.en`; every other selected destination stays unchanged until its
   exact effect, natural Telegram, replay 0, and metric ownership are terminal.
9. Keep the daily metrics loop running for every healthy account: social post and
   account metrics, 2h/24h/72h/7d windows, and natural Telegram. Unsupported or
   empty metrics remain source-labelled `unavailable`.
10. Join each immutable creative/account/campaign lineage with ASC installs,
    RevenueCat trials/subscriptions/proceeds, and product activation/retention.
    Unattributed values remain unattributed; timing alone is not attribution.
11. Close the bounded self-improvement loop independently for Honne EN, Honne JA,
    and Anicca: change one hook/title variable, keep stable assignment, compare
    attributed outcomes, keep/revert, and prove the next creative consumed the
    decision. Never select winners across products.
12. Finish daily and weekly natural-language Telegram reports covering posting,
    misses, duplicates, social metrics, installs, activation, trials, paid,
    proceeds, attribution coverage, and the next bounded hook change.
13. Prove a continuous owner soak: every selected available destination produces
    three daily opportunities, correct-content publication receipts, replay 0,
    due metrics, attributed app outcomes, bounded next-hook consumption, and
    natural daily/weekly Telegram without silent misses or duplicates.
14. Only after that full three/day 24/7 loop is proven, retire legacy ownership
    while retaining disabled artifacts as rollback evidence. Do not enable,
    kickstart, stop, restart, or delete legacy OpenClaw/launchd jobs during this
    recovery. Order 14 terminal is the definition of the mobile-app marketing
    loop being done; no additional hidden recovery TODO remains.
Controls are restored to fence closed and armed 0. API-only operation is enforced by removing the CDP/
browser fallback from profile resolution; a profile-only result now fails fast
after the bounded API/CLI readback instead of holding the effect fence open for
repeated browser attempts. The only next action is reconciliation of this exact
existing effect under the photo-carousel acceptance rule; no new publication
effect is allowed.

Postiz implementation evidence removes the remaining ambiguity about that
numeric suffix. The cloned upstream `gitroomhq/postiz-app` at commit
`81af6c9761f2c50e4741438a9e31bda222b32d2c` shows TikTok Business photo/video
rows can retain `p_pub_url~...` / `v_pub_url~...` as the **share/publish ID**;
`postAnalytics` must call TikTok's private provider-token publish-status API to
resolve it to `post_ids[0]` before querying the native item. Therefore
`7678198747632977937` must not be interpolated into `/video/<id>` and is not a
native direct URL. The public Postiz post-analytics route currently returns an
empty array and does not expose the resolved item ID. The exact blocker is now
provider readback capability, not publication uncertainty: without a public
Postiz response containing the resolved TikTok item ID or a caption-matching
native API artifact, R2 cannot truthfully transition from unknown to present.

The exact upstream defect is narrower still: the normal `tiktok.provider.ts`
`postAnalytics` path resolves only release IDs containing `v_pub_url`; it does
not resolve the photo form's `p_pub_url`. It therefore sends the share/publish
ID to TikTok `video/query`, which returns no row and surfaces as public Postiz
analytics `[]`. The public integration response contains only id/name/identifier/
picture/disabled/profile and no provider token or resolved item ID. Postiz's
`/posts/:id/missing` path is also inapplicable because repository mutation and
lookup are deliberately restricted to rows whose stored release ID already
equals `missing`; this row retains `p_pub_url~v2…`. Exact-caption web search is
also empty. Thus no safe public Postiz API call can currently perform the
private provider-token status resolution. Do not overwrite the release ID,
invent a `/video/` URL, expose an integration token, or create another effect.

**Completed immediately preceding code item:** MKT-09R9-14 / Order 24F9 extends
the shared native-carousel boundary for the single frozen TikTok photo lane and
adds a thin `anicca-en-slideshow-tiktok-canary.js` wrapper. Postiz transport now
accepts ordered JPEG carousels for TikTok, rejects non-JPEG media, preserves the
six-item order, supplies the exact hook title, and retains DIRECT_POST. Job and
receipt contracts bind platform/account/integration/pack/media/caption and accept
only direct `@anicca_slideshow/video/<id>` URLs. Target-only manifest/fence
control restores armed 0/closed after a simulated exact effect; Telegram remains
held without native verification. JS adapter/runner 25/25 and Python transport
20/20 pass; syntax/diff checks pass; provider/Telegram effects during code stage
are 0.

**Completed immediately preceding pack item:** MKT-09R9-13 / Order 24F8 rejects
all nine historical Postiz JPEGs as publication pack candidates because original
resolution inspection proves their hook text extends beyond both horizontal
edges. A corrected six-slide EN carousel retains only a clean crop of the exact
forest visual family and places complete safe-area copy: procrastination hook,
brain/discomfort explanation, smaller-task instruction, two-minute action,
momentum reframe, and Anicca CTA. Full contact-sheet inspection passes. Pack
`object://sha256/3241653ecc9239663de3151426d01a6b1c34cfe7c130288e928fab6686de624c`,
approval `object://sha256/ab96425da6f82672be19a3ac74b3e2ad1c98f632bc39d31dab23720701aed5b5`,
caption `object://sha256/8e6f7cecee64454d906a787bad4b4c57736fff2668c1b9eea6c0d666140f2c6d`,
visual `object://sha256/7a111900c6adf8ad7bb87601af145252464f56b5ed2c7609a8b686adb08425e4`,
and six ordered JPEG refs are mode-0600/SHA exact. Manifest
`marketing-lane-manifest:c5d04d7ac192d67a0d47ab68bb9fd8fa016e34bc45eb336c2479f6acfb796974`
moves only this integration hold→pack-ready/default-off at target 1/day; armed 0,
fence closed, provider/Telegram/scheduler writes 0, and no Postiz/OpenClaw URL is
a runtime asset dependency.

**Completed immediately preceding classification:** MKT-09R9-12 / Order 24F7
classifies TikTok `@anicca_slideshow` as Anicca iOS / EN / `slideshow` /
`mental-health-photo-post`. The enabled Postiz integration has 13 rows: nine
published and four error. All nine published rows are TikTok photo publications,
zero videos, with the same English mental-health hashtag caption and title
`words you needed to hear today`. Postiz details prove one 1024x1536 JPEG per
row. Full nine-image inspection shows a coherent forest background with distinct
English mental-health hook overlays; repeated rows also reuse byte-identical
assets. Postiz still exposes only a profile URL plus `p_pub_url` suffix, so no
direct native URL or success is inferred. Evidence
`object://sha256/c486589741d5e6a93fa3bd4483bfb27bd34b296ce09b799046beedac5bb1a8a2`;
visual `object://sha256/2a1c6606b4953eaa7486efc29a6cf3f0f33b1451cc198f5dcabed05a84f890b5`.
No approved pack exists yet, so the manifest remains hold/default-off at 0/day,
armed 0; provider, Telegram, and scheduler effects are 0.

**Completed immediately preceding classification:** MKT-09R9-11 / Order 24F6
keeps TikTok `@anicca_buddha` on terminal hold at 0/day. Postiz API reports an
enabled integration named `アニッチャ お笑い`, 114 rows: 93 published and 21
error. The published set is not one coherent lane: 88 are six-image Japanese
mental-strength slideshows through `2026-07-23`, followed by five separate Life
Manager MP4 promotions from `2026-07-24` through `2026-07-28`. Exact Postiz
detail for `cmrwzmwli006gn20y0boak6md` proves six JPEG assets; full contact-sheet
inspection shows the same personal restaurant photo with Japanese overlay text,
not Buddhist art or an app renderer. Postiz supplies only the TikTok profile URL
plus a `p_pub_url` suffix; oEmbed/native direct caption verification is
unavailable, so no numeric suffix is promoted to success. The old LM daily route
was already moved from this mixed personal account to `@anicca.comedy`.
Product, renderer, format, and approved pack therefore remain null instead of
being guessed. Evidence
`object://sha256/ca2de82eedb6ee24b5884860b5a03a6612a959d04f5e18629c48fb05545b4480`;
visual `object://sha256/5fd3fb3d53ed708acc2f58f9fb36fba0018099a8908866d62c5807b90469eb2`.
Manifest stays `b96ffe9b…661b4`, hold/default-off, armed 0; provider, Telegram,
and scheduler effects are 0.

**Completed immediately preceding live item:** MKT-09R9-10 / Order 24F5 posts
one exact Obou canary through Postiz API. Direct native Reel
`https://www.instagram.com/reel/DcfVvIkkWyz/` binds owner `@obou.anicca`, the
complete approved caption, and the approved 48-second watercolor video after
full-frame comparison. Postiz row `cmt9l523g02mbp20ybq3lefov`; native evidence
`object://sha256/3c87ca0de1a7ada5c148fe1cce3fabdf37a48b113d605a537434a6593b3e664a`;
verification `object://sha256/25c4b23d37d91e910e95a60dd04e787cccd6985e9e67b522a92d05b98b7883a6`;
natural Telegram `34870`; same-slot replay publication 0/message 0. Existing
30-minute owner `ai.anicca.life-manager-instagram-metrics` discovers this exact
effect and owns 2h `2026-08-26T06:21:05.630Z`, 24h
`2026-08-27T04:21:05.630Z`, 72h `2026-08-29T04:21:05.630Z`, and 7d
`2026-09-02T04:21:05.630Z`; pending remains pending and unavailable never
becomes zero. Status
`object://sha256/eeb154df317c925ac498aee24a1d18557d4b9fd9d352683960bd0ac444b51993`;
manifest `marketing-lane-manifest:b96ffe9bd782ef36fb642bda91f63b30e760834f369cd64fb66d487d215661b4`
is canary-verified/default-off, armed 0, fence closed.

**Completed immediately preceding code item:** MKT-09R9-09 / Order 24F4 adds
one immutable `OBOU_LANE` and thin CLI wrapper while reusing the existing video
publication, target-only control, native verification, Telegram, replay, and
metrics-compatible receipt contracts. It corrects the publication family to
`format=watercolor` while retaining pack/manifest format `watercolor-reel`, and
adds `watercolor` to the Anicca format allowlist. Exact pack/video/caption and
corrected pack-bound approval
`object://sha256/2fb66c87729a915545ca94d0029562240e543bad3f2bb9080ffc3fa821a538d7`
are pinned; alternate self-consistent refs fail before secret/provider access.
Obou-focused 5/5 and full existing EN/JA Widget/Card 37/37 pass; syntax and diff
checks pass; provider/Telegram effects during code stage are 0. Earlier approval
objects without the correct pack/format binding remain superseded, not deleted.

**Completed immediately preceding pack item:** MKT-09R9-08 / Order 24F3 imports
one exact valid pack after rejecting latest `DbeS8W_kmWC` because its caption
ends mid-sentence at `苦しいの`. Selected direct `DbUlc_Kk-IX` has native
owner/caption match, a complete 900-byte no-LF caption, and a fully inspected
48.13356-second 720x1280 H.264/AAC watercolor Buddhist self-care Reel. Video
`object://sha256/b2772de4303acc901f42b43a0b3f4af166ae3daeb5ee7fd24e090e5b62f2b0e8`,
caption `object://sha256/40293be368c6c33b04bb6fa6be8ff4bc879ca8c6d18c2944d7275c488088ac0a`,
visual `object://sha256/7d809896bb6103a4dac6c09ffebb9203a72da26caf855bac0692b58d5bc4ae07`,
pack `object://sha256/2a24da50040c9a2705c2e8975d76152b6add447504ac21493cdfca999f598145`,
and final approval `object://sha256/2fb66c87729a915545ca94d0029562240e543bad3f2bb9080ffc3fa821a538d7`
are SHA-exact/mode0600. The earlier `cca891d1…a96c4d0` and
`291df0f3…00ff3e7` approval objects are retained as superseded. Manifest
`marketing-lane-manifest:3ffb30c49af2ed74528950b408f29db7d08f2f83476f93181f098367ec5dae1d`
changes only this lane to pack-ready/default-off; armed 0, fence closed,
provider/Telegram/scheduler writes 0, and the pack has no OpenClaw runtime path.

**Completed immediately preceding classification:** MKT-09R9-07 / Order 24F2
classifies Instagram `@obou.anicca` as Anicca iOS / JA / `watercolor` /
`watercolor-reel` / `buddhist-self-care-reel`, with healthy target 2/day at
07:00 and 20:00 JST but actual 0/day. The enabled Postiz integration has 210
rows, 209 published/direct Reels and one error; no carousel/photo effects.
Instagram API binds latest direct `DbeS8W_kmWC` to native owner
`obou.anicca`, exact long-form Buddhist caption, and 91.160-second 720x1280
GraphVideo. Full-timeline inspection shows only illustrated JA Buddhist
self-care narrative. Archived Content Factory independently identifies the
same route as `watercolor` at 07:00/20:00; it is evidence only and creates no
runtime dependency. Evidence
`object://sha256/1aa931970691d9d9262acd2684bb191e3887aab5eddd9937a31d8bbef3d13157`;
manifest `marketing-lane-manifest:ebc9ca2ff1ddad8c0f74280d93ee02df02460a608c2a65af5d28d07f36f69136`
changes only this integration hold→classified, 12 targets/18 holds, default-off,
armed 0, fence closed, provider/Telegram effects 0.

**Completed immediately preceding classification:** MKT-09R9-06 / Order 24F1
keeps Instagram `@anicca.bochi` at a justified terminal 0/day hold. The enabled
Postiz integration has 233 rows: 106 published/direct, 125 error; published
content classifies as 51 Anicca-app, 40 generic mental-health, 14 separate AI
memorial/tomb product, and one other, across 67 carousel/photo and 39 Reel
effects. Instagram API binds both representative direct GraphSidecars to native
owner `@anicca.bochi`: mental-health `DZSSHHtGjii` and AI memorial
`DZPVAMGFL21`. Full six-slide visual inspection confirms materially different
products/forms, so allowed renderer and approved pack remain null rather than
guessing. Evidence
`object://sha256/92521be6bec0011c04d03f1bdcb9a04cfebf43b65ae363a67625a79185a59525`
is mode 0600; manifest remains hold/0-day, fence closed, armed 0, provider and
Telegram effects 0.

The runner now performs this control transition itself: it validates the
closed mode-0600 controls and exact pack-ready lane, saves their exact bytes,
arms only the calculated effect/lane, and restores both files in `finally` on
success or provider failure. Focused control coverage is 22/22 GREEN, including
byte-exact restoration after an unknown provider effect; syntax and diff checks
pass. No live provider or Telegram effect occurred during this repair.

**Completed immediately preceding atomic item:** MKT-09R9-04 / Order 24D
generalizes the existing native-carousel adapter and runner for exactly one
additional immutable EN lane while preserving the JA command. EN identity,
Postiz integration `cmp9pedr700ttqh0yj8o57fog`, six ordered object refs,
caption, approval, and native owner `@anicca.ios` are pinned; alternate
self-consistent refs fail before secret/provider access. Direct `/p/`, native
verification, natural Telegram, and replay contracts are shared rather than
copied. Focused tests are 21/21 GREEN; both production files pass `node
--check`, `git diff --check` passes, and provider/Telegram effects during the
code stage are 0. The existing shared ledger already enforces exact open-fence
effect key plus exact production-armed manifest lane at enqueue and claim, so
no second control implementation is added.

**Completed canary sub-item:** MKT-09R9-05A publishes exactly one EN
affirmation carousel as Postiz `cmt9jm8990291p20y0a2l1xmk` at direct native
`https://www.instagram.com/p/DcfQ2-hG3KR/`. Instagram's embed API returns
native owner `anicca.ios`, the exact approved caption, `GraphSidecar`, and six
ordered CDN images whose bytes equal all six approved SHA-256 values. Direct
visual inspection confirms the intended hook plus five EN mental-health
affirmations and no Honne/Widget/Card/wrong-locale content. API evidence is
`object://sha256/d404b93a5d4393dda4f1e9e5cadddae33777b2b3155fef175e9c6f9da2945549`,
native verification is `object://sha256/6162ef537ffeef74bf599e480deeeb2d0d94f56862749ab2faf8a4f21a9733ca`,
natural Telegram is `34799`, and the next same-slot run creates publication 0
and message 0. Immediate post metrics are measured Views/Reach/Likes/Comments/
Shares/Saves 0; unsupported/funnel fields remain unavailable, not zero, in
`object://sha256/d7dc6400a204fd442ba06e54fe3df51827b6f5349e399798814831490fa62d60`.
Manifest `marketing-lane-manifest:5f9d4f61e7b6ebfdd59162c663226e877728303b501a38ff9f3ab0d6d2afc4a4`
marks only this route canary-verified while remaining default-off, armed 0;
the fence is closed. The earlier metric status recorded future windows as
`pending_unregistered` and is superseded without deletion by the registration
evidence below.

**Completed metric-registration sub-item:** MKT-09R9-05B reuses the existing
LM-owned Instagram due planner and 30-minute launchd owner; it adds no scheduler.
Discovery accepts the EN carousel only after the exact carousel receipt
verifier, integration, direct `/p/`, provider ID, publication time, and
mode-0600 caption object SHA all pass. Focused due/read coverage is 6/6 GREEN
in canonical runtime. Live discovery finds exactly Postiz
`cmt9jm8990291p20y0a2l1xmk` and registers 2h
`2026-08-26T05:37:17.624Z`, 24h `2026-08-27T03:37:17.624Z`, 72h
`2026-08-29T03:37:17.624Z`, and 7d `2026-09-02T03:37:17.624Z` as pending.
Durable owner `ai.anicca.life-manager-instagram-metrics` points at the canonical
boot script, interval 1,800 seconds, runs 12, last exit 0; it was not restarted
or kickstarted. Registered status
`object://sha256/c08bf9e85a92dc5e72a1d975dcb2e84e7ea4ea079a5a001f5efbbb61f56a97d3`
supersedes `d7dc6400…62d60`; commit `a34cec9f3` is present in canonical runtime
and main.

**Completed immediately preceding atomic item:** MKT-09R9-03 / Order 24C
imports exact pack `object://sha256/e23cd41257832d2032fd889bd9a16ec95ea8dc213cdd7a2e3f820fbe1578669e`,
caption `object://sha256/bf90a15a5a615d2bb295c1829f7329f391a870fe4e950c8099972c20bf6e64a0`,
and account-bound approval
`object://sha256/7740cd09733d0cb7a5d8f32ff4614c3e07ebae27df0e3eae8bca8df80b968845`.
The six ordered JPEG refs reuse the visually inspected native carousel and
produce media-order hash `4daa5db7…9837f9`; caption bytes equal Postiz row
`cms8movjd0ewykz0yr2en9ux5` exactly with no terminal LF. Every object and env
binding is SHA-exact/mode `0600`; exact pack/caption/approval matches in local
jobs/receipts are 0. Manifest
`marketing-lane-manifest:b48c8e11a7e1b46999bbe7d3eda5cbb6a756ece2d45ca51fe2480b8fab5ce18f`
changes only this lane from `classified` to `pack-ready`, default-off, all armed
0, fence closed. Provider/Telegram/scheduler/legacy writes are 0.

**Completed immediately preceding atomic item:** MKT-09R9-02 / Order 24B
classifies this lane as Anicca iOS / EN / Instagram / renderer `larry` / format
`native-photo-carousel` / form `affirmation-carousel`, target 1/day at 10:00
JST. Postiz has 144 rows. Direct `https://www.instagram.com/p/Dbcvm5Mm8gM/`
binds native owner `anicca.ios`, caption `5 affirmations to tell yourself every
morning... | #anicca #affirmation`, and six ordered English mental-health text
slides with no Card or Widget flow. Evidence
`object://sha256/d4b237686f274a138369e4469e003828566e5b077c9a8118c8c856ad1286c65b`
and contact sheet `object://sha256/f8dacec7fdcf621998735905011290539d7267d727bd442aef0ab31ec94d503c`
are SHA-addressed. Historical OpenClaw commands that pass this Instagram ID as
`--tt` are rejected as a platform mapping error. Manifest
`marketing-lane-manifest:88975d6717b7ab8baebf824f04f538848564321ee8c8615c5b50804e31755d33`
moves only this row from hold to `classified/default-off`, 11 targets/19 holds,
armed 0, fence closed; provider and legacy writes are 0.

Read-only classification proves that no existing route can pass. The live
registry has 30 integrations and eight enabled Instagram profiles:
`obou.anicca`, `anicca.affirmation`, `anicca.encards`, `anicca.jp.videos`,
`anicca.jp1`, `anicca.bochi`, `ani.cca1234`, and `anicca.en`. Their 2,076
historical rows belong to Anicca/Buddhist/affirmation/Card/Widget/Larry content;
none is a dedicated Honne EN product account. Current evidence is
`object://sha256/0023c799443305f76a60beb1dc167e2f466bd02227d2527f394f1b5d08d0e28f`;
it preserves global `6,825`, selected `2,076`, every integration/profile/count,
zero literal Honne-brand rows, and cadence initial `1/day` / healthy maximum
`3/day`. Earlier `e07dbe0a…c93906` and `f89d0be1…554a1` are retained but
superseded; the latter lacked the second fresh-OTP attempt.
Provisioning candidate `@honne_reveal` is username-available and reached the
ordinary email OTP screen. Instagram rejected the Gmail plus-address, then the
owner Gmail OTP was rejected as expired/incorrect; one official resend produced
no new code. A later clean signup session again proved the handle and DOB,
received a new owner-Gmail OTP, and read that latest message body directly;
Instagram still rejected the fresh code as invalid/expired. No account was
created, and no phone, CAPTCHA, recovery, Postiz write, publication, Telegram,
or legacy-job operation occurred. Both isolated browser contexts were closed.
Order 24A remains active until Instagram accepts an ordinary fresh OTP for this
exact account or an independently owned dedicated Honne EN account is
connected; an Anicca route or another product's email/account is never a
fallback.

The owner then removed login/signup from this recovery path and made Postiz
CLI/API the only route authority. API disposition
`object://sha256/2750a67c78b3db28c8f794d034fcb29331a4bdc1435c9483fe0df4a91769ac8e`
is terminal: the 30-row registry contains Honne EN/JA TikTok only, Honne
Instagram `0`, Honne YouTube `0`; Postiz can publish only to an existing
integration. The earlier signup evidence remains historical, but 24A is no
longer blocked on account creation. Missing Honne routes are explicit
`unavailable/missing_integration` and the next existing linked Instagram route
is `@anicca.affirmation`.

**Completed immediately preceding atomic item:** MKT-09R8-13 / Order 23J is
terminal. Exact Anicca JA Card publication is Postiz row
`cmt9d2khz00r1p20yb6qbtvyg` at direct native Reel
`https://www.instagram.com/reel/Dce7_IPlUlr/`; owner is `anicca.ios.jp`, caption
and visible Card/My Path content are correct, natural Telegram is `34651`, and
same-slot publication/message replay is 0. Native video/contact/evidence/
verification objects are `ae07770e…b5640`, `69dca534…58de74`,
`9c8cc26e…85d32e`, and `c73062a4…18bee4`. Immediate Postiz metrics measure
Views `126`, Reach `62`, Likes/Comments/Shares/Saves `0`, Engagement `0%`;
unsupported Impressions/Watch time/Average watch time/Completion remain
unavailable in metric status `ea1f93eb…9a9e8f3`. Commit `e9c460b46` fixes
target-filtered Instagram discovery: the loop now discovers this row exactly
once while skipping valid EN Card and EN/JA Widget rows and still rejects a
malformed JA Card row. Registered due times are 2h `2026-08-26T02:35:50.741Z`,
24h `2026-08-27T00:35:50.741Z`, 72h `2026-08-29T00:35:50.741Z`, and 7d
`2026-09-02T00:35:50.741Z`, plus daily 17:30 JST. Manifest
`marketing-lane-manifest:9213d62a61f8c76539083bd0fe84abf93c82e93d1ed9ad2920ee18dfb20b54df`
changes only this lane to `canary-verified`; it remains default-off, all lanes
armed 0, and fence closed.

The final decoded-frame guard is committed at `d82107c69`: `ffprobe
-count_frames` supplies `nb_read_frames` for both inputs, missing or unequal
decoded counts fail closed, and the existing raw/blur thresholds remain
unchanged. Parent verification passes the full runner suite `32/32`; the
native/source pair independently decodes `312/312` frames and compares true;
fresh adversarial review returns `ship`. Same-slot live replay then returns
publication `created=false` and Telegram `created=false`, reusing message
`34651`; jobs/receipts remain `787/255` with exactly one publication receipt
and one natural message receipt. Commit `e9c460b46` is byte-identical to the
canonical metrics runtime file and its target filter passes `3/3`; a second
fresh adversary returns `ship`. The installed LM metrics plist retains
`StartInterval=1800`, but an immediate non-killing `launchctl kickstart` from
this isolated GUI control plane returns `141 Reentrancy avoided`. No service,
legacy job, plist, or scheduler state is changed; the next natural interval is
the readback point for the deployed filter, while the exact due windows remain
registered rather than being called measured early.

MKT-09R8-13 implementation preflight is complete without an external effect.
The live manifest has exactly one matching `@anicca.jp1` target at integration
`cmn8ycvtn02djqx0ytuisn9mw`, renderer `reelclaw-card`, format
`nudge-card-reel`, state `pack-ready/default-off`; every lane is armed 0 and
the publication fence is closed. Pack `76937db0…fe311c`, caption
`311f9c3d…6ba2eb`, and selected video `35a15c7c…e9a15` resolve from the LM
object store at mode `0600`. All five dedicated Card Instagram env bindings
are unset, while shared `LM_ANICCA_MAIN_PACK_REF` still points to the rejected
source pack and must remain byte-unchanged. Existing standing approval
`3f138ade…831eb7` authorizes the account/form generally but does not bind the
exact pack, caption, video, integration, and creative required by the canary;
an exact immutable approval object is therefore required before arming. Postiz
uses account alias `@anicca.jp1`, but the already established native Instagram
owner is `anicca.ios.jp`; the reused runner must preserve the alias for
manifest/approval/publication identity and verify the separate frozen native
owner at `CaptionUsername`. The minimal implementation is one frozen lane in
the existing canary engine, one thin CLI wrapper, and focused regression tests;
it does not add a publisher, scheduler, or alternate receipt path.

The implementation is now `GREEN` at commits `c8a4b3ebe` and `e105d7cf1`:
the shared canary engine accepts one additional frozen Card lane, keeps
provider alias `@anicca.jp1` separate from native owner `@anicca.ios.jp`, and a
thin dedicated CLI selects only that lane. Focused tests pass `29/29`, including
dedicated-ref isolation, exact raw integration, fail-before-secret/provider,
wrong-owner hold, one natural Telegram release only after native verification,
and same-slot publication/message replay 0. Official Postiz public-API readback
finds the exact integration among 30 rows as `instagram-standalone`, profile
`anicca.jp1`, `disabled=false`. Runtime env, manifest, fence, provider rows,
Telegram publication receipts, scheduler, and legacy state remain unchanged;
fresh adversarial review and the exact runtime approval/preflight are still
required before the one allowed effect.

Fresh adversarial pre-effect review rejects that first GREEN as not yet safe to
run: dedicated env names alone still accepted any mutually consistent
replacement pack and approval instead of the exact selected refs. The same
live preflight also finds host DNS unavailable for `api.postiz.com`; the Python
transport already implements an exact-host/public-IPv4-validated
`LM_POSTIZ_RESOLVE_IP`, but the Node subprocess allowlist omitted that key.
Both are implementation blockers, not reasons to post through a second path.
Exact canary approval
`object://sha256/bb3e2ac385d7c7ed9a2387522ba441ece797fd8bcc9827c9386dcf66db764ee2`
now binds account, integration, pack, creative, video, and caption at mode
`0600`, rooted in standing approval `3f138ade…831eb7`; it creates no external
effect. Before runtime env may be set, the frozen lane must reject every ref
other than pack `76937db0…fe311c`, video `35a15c7c…e9a15`, caption
`311f9c3d…6ba2eb`, and approval `bb3e2ac3…764ee2`, and the focused DNS
allowlist regression must be GREEN. Effect count remains zero.

Those pre-effect blockers are now closed at commits `891d671ab` and
`6f56fe538`. The lane freezes all four exact refs and rejects a mutually
consistent alternate before object reads, secrets, provider access, or state;
the subprocess passes only the existing validated Postiz DNS override key; and
the natural receipt names native owner `@anicca.ios.jp`, never provider alias
`@anicca.jp1`. Parent verification passes widget `30/30` and publication
adapter `16/16`; the final targeted adversary returns `SHIP`. The private
runtime env is mode `0600` and now binds the four exact refs plus validated
Postiz IPv4 `69.46.46.109`; the shared main pack remains byte-identical. Every
object resolves with SHA/mode `0600`, manifest `bbc2bb24…ca124` still has
armed 0 and only the exact lane `pack-ready/default-off`, and the fence remains
closed. The current local ledgers are `780/253` because unrelated owners
continued, but exact job `marketing-video-publication:f4cb935f…aa31f1` and the
creative+caption+approval identity match zero jobs and zero receipts. Immediate
official Postiz readback over 124 August rows finds five rows for the integration
and zero for the exact caption. Native verification binding is absent, so the
first run must hold Telegram. Exactly one effect at immutable logical slot
`2026-08-26T00:33:11.000Z` is authorized next; no other slot or retry is
allowed if its outcome becomes unknown.

At effect time a concurrent owner had already opened the exact same effect
fence. The planned `00:33:11.000Z` invocation therefore failed before provider
access with `publication controls are not closed` and created no second job or
effect. The existing exact job
`marketing-video-publication:f4cb935f…aa31f1` owns actual slot
`2026-08-26T00:33:55.000Z` and is terminal `completed`,
`unknown_effect=false`, with exactly one enqueue/claim/complete sequence and
one receipt. Postiz row `cmt9d2khz00r1p20yb6qbtvyg` has the LF-equivalent
exact caption and direct candidate
`https://www.instagram.com/reel/Dce7_IPlUlr/`. Manifest bytes are restored to
`bbc2bb24…ca124`, target is again `pack-ready/default-off`, all lanes armed 0,
and fence closed at mode `0600`. Native verification remains absent, so
Telegram is correctly held at 0. No retry or second publication is allowed;
only this direct candidate may proceed to native owner/full-video verification.

Native verification is terminal. The captioned embed binds exact native owner
`anicca.ios.jp` and the approved caption; parent full-timeline visual readback
confirms the same woman, hook, Japanese Anicca Card, and My Path ending.
Instagram native MP4 `object://sha256/ae07770e…b5640`, native contact sheet
`object://sha256/69dca534…58de74`, evidence
`object://sha256/9c8cc26e…85d32e`, and verification
`object://sha256/c73062a4…18bee4` are immutable. The first natural Telegram
transport failed before HTTP because system DNS was unavailable; the same
durable message job was sent once through the DNS-resolved transport and
reconciled `present` as message `34651`. A further same-slot replay returns
publication `created=false`, Telegram `created=false`, and the same direct URL
and message ID. No second post exists. The only remaining MKT-09R8-13 failure is
metrics discovery: the collector throws on unrelated valid EN Card and Widget
rows before reaching this valid JA Card row.

**Completed immediately preceding atomic item:** MKT-09R8-12 / Order 23I
reconciled the complete existing Postiz history before any new effect. Read-only
GETs covered `1970-01-01` through `2026-08-27`: pre-2025 and 2025 returned zero,
2026 H1 returned 4,629 rows, July 2,071, and August-to-observation 122. Their
6,822 unique IDs exactly equal the one-shot broad response, with zero duplicate
or missing IDs. Exact integration `cmn8ycvtn02djqx0ytuisn9mw` has 60 rows, but
zero rows match caption object `311f9c3d…6ba2eb`, allowing only provider
omission of its single terminal LF. Therefore zero native-video candidates
exist and none can satisfy the required full-media match
`35a15c7ce990…e9a15`. Corrected immutable absence evidence is
`object://sha256/96a0d7691293e974962e0c69a0975f6ac7ff9a8a500f60569f73f53f7261ce47`.
It resolves the selected video directly from the SHA-verified pack: caption and
pack refs each match zero local ledger lines, while the intentionally reused
video ref appears in 125 job lines and 11 receipt lines; the exact integration
+ caption + pack + video identity matches zero. The earlier evidence
`79dd1fc4…a0b32b8` is retained but superseded because it contained a manually
transcribed non-pack video hash. Global jobs/receipts remain `774/251`; manifest
`bbc2bb24…ca124` remains pack-ready/default-off with armed 0 and the publication
fence remains closed. Provider, Telegram, scheduler, and legacy-job writes are
all zero. The historical
`DcTFx_UjSio` row remains rejected because its caption is different; only the
later dedicated-runner item may create a new exact effect.

**Earlier completed atomic item:** MKT-09R8-11 / Order 23H
audited all four existing JA Card media objects rather than trusting legacy pack
metadata. Source pack `694e3ab6…7d480` is rejected as a runnable pack because
its four `強い人の口癖…` metadata hooks match none of the baked media hooks.
Media 1 `7e24db96…a9ae9` and media 2 `abbbbdea…66782a` end in generic quote
sequences rather than an Anicca Nudge Card and are rejected. Media 3
`5639e148…82a32` is a valid held alternative with baked hook
`やらなきゃいけないのに 動けない自分が嫌になる`. Media 4
`35a15c7ce990…e9a15` is selected: 21 sampled timeline frames collectively show
baked hook `怠けてるんじゃない。脳が限界なだけ。`, the Japanese Anicca
Card, and complete My Path transition without blank, clipping, foreign language,
or CTA.
Its new exact caption object `311f9c3d…6ba2eb` matches that baked hook and ends
in LF. Account-bound modern pack `76937db0…fe311c` reuses the existing video
bytes, binds only `@anicca.jp1` / exact integration / `reelclaw-card` /
`nudge-card-reel`, and points to visual evidence `6ddf6284…9149dc`.
Full four-candidate evidence is `object://sha256/e0a7b1ab98bbdab7430cfaf9fcf9f6448581aa44d152427cf82314e07ceeb06f`;
all 15 referenced objects are SHA-exact/mode `0600`, caption bytes match the
pack, and this pack operation leaves jobs/receipts unchanged at `771/250`.
During later adversarial review, an unrelated Honne `marketing.video.generate`
job with `effect_class=none` added only enqueue/claim/complete plus one artifact
receipt, moving the global ledger to `774/251`; it created no provider or
message effect and does not change this pack result. The prior direct Instagram canary
uses the selected media but caption `強い人の口癖、5つだけ`; it therefore
proves native account/Card identity but no longer passes the exact content
success gate. Manifest `marketing-lane-manifest:bbc2bb247ddb91ffd5b1b195e8f0cfa02bcdd96ab31bc93d7b1c32301f4ca124`
downgrades only this lane from historical `verified` to truthful `pack-ready`;
actual 0/day/default-off/armed 0 and fence closed remain unchanged. The shared
`LM_ANICCA_MAIN_PACK_REF` is not changed because it also feeds TikTok; the
exact Instagram pack must use a dedicated binding before cadence arm.
Fresh read-only adversarial review returned `SHIP`: it independently checked all
four timeline classifications, metadata/baked-hook mismatch, media 4 selection,
LF-exact caption, account-bound approval, all referenced SHA/mode `0600`, media
byte reuse, target-only manifest downgrade, armed 0/fence closed, unchanged
shared pack ref, and the next read-only reconciliation gate.

**Earlier completed atomic item:** MKT-09R8-10 / Order 23G
classifies Anicca iOS / JA / Instagram Card Postiz alias `@anicca.jp1` and
native owner `@anicca.ios.jp` as one lane only: integration
`cmn8ycvtn02djqx0ytuisn9mw`, renderer `reelclaw-card`, format
`nudge-card-reel`, form `nudge-card`, and pack
`anicca-ios-reelclaw-card-ja.pack.json`. The live Postiz registry returns the
integration exactly once as enabled `instagram-standalone`; five target rows in
the measured range are direct Reels with Japanese Anicca/self-care captions.
Only the known canary below is visually classified as Card. Direct
`https://www.instagram.com/reel/DcTFx_UjSio/` binds native owner
`anicca.ios.jp`, exact caption `強い人の口癖、5つだけ`, and the approved
woman → Japanese Anicca Card → My Path flow. Instagram's current transcode is
preserved as `object://sha256/ae07770e5a74f9ff04ee68e2504cdb040c67c3c7e8fef06fcd38c2ea85fb5640`
with contact sheet
`object://sha256/cd167041abe6a24858542f6ba90b0d1cc1de363e640c12b37c0e80b4142a38fc`.
Parent full-timeline visual inspection matches the approved media; the generic
strict comparator does not pass (`2 fps`, 21 frames, SSIM min `0.921209`, mean
`0.956471`), so this is classification evidence only and is not promoted into
a terminal native-verification claim. The first immutable classification object
`48fc8782…e2e34` remains retained but is superseded because its embedded caption
omitted the approved caption object's final LF and its history-family wording
was too broad. Corrected evidence
`object://sha256/5728925f0480b785b182e9c7dc63e27ef07bb296f536fd47eadad825657c5c6c`
is byte-identical to caption object `bdef736e…601d9`, including final LF, and
limits the Card visual claim to the known canary.
The existing manifest already has the exact assignment and stronger historical
`verified` canary state, so no manifest bytes are changed or downgraded. Target
is 1/day at 19:10 JST; actual remains 0/day/default-off/armed 0, fence closed.
The targeted runtime path reads LM env/object refs and scans zero OpenClaw
paths/env/assets. `launchctl print` is currently unavailable with GUI bootstrap
error 141, so no claim is made that either plist is registered; neither plist
nor any legacy job was changed. Fresh read-only adversarial review returned
`SHIP`: it verified the corrected final-LF caption, alias/native-owner binding,
runtime `anicca-ios` to manifest `anicca` normalization, existing receipt/media
lineage, classification-only SSIM scope, default-off/armed 0/fence closed state,
zero external writes, and zero targeted OpenClaw runtime dependencies.

**Earlier completed atomic item:** MKT-09R8-09 / Order 23F ran
exactly one controlled canary for Anicca iOS / JA / Instagram
`@anicca.jp.videos` using
pack `object://sha256/16d4452d5e3d408d76b915b76e80bf014a57b6354bf133391c785542db6f7696`
and Postiz integration `cmmzzg2es0539p30ycb94ayx0`. Before the one provider
call, a dedicated fail-closed runner must bind the exact account, integration,
pack/video/caption SHAs, one logical slot, open effect fence, and temporary
target-only arm, then restore both controls on every terminal path. Success
requires a caption-matching direct Instagram Reel whose native full video
matches the approved bytes, followed by one natural-language Telegram receipt,
replay 0, and explicit metric-source status. Profile URLs, numeric releases,
Postiz `PUBLISHED`, or HTTP 200 alone never pass. No scheduler, fan-out, retry,
second account, or legacy-job action is allowed.

The MKT-09R8-09 **runner and native-evidence gates are green; its sole external
effect is complete and no second effect is allowed**.
Copying the 500-line EN runner was rejected. The existing Widget canary is now
shared through immutable EN and JA lane definitions, while the new
`anicca-ja-widget-canary.js` is only a thin exact-lane CLI wrapper. The JA
definition pins `@anicca.jp.videos`, integration
`cmmzzg2es0539p30ycb94ayx0`, manifest account
`anicca-ios-ja-widget-instagram`, locale `ja`, the exact four LM object env
keys, creative `JA-WIDGET-CANARY-0c67b0a4d1de`, and approved pack name
`anicca-ios-reelclaw-widget-ja.pack.json`. Only the exact frozen EN or JA lane
object is accepted; a cloned caller-supplied definition is rejected before env,
secret, provider, or state access. TDD RED first observed the absent JA wrapper,
then separately observed the cloned-lane hole. Parent GREEN is focused 19/19
and combined publication/ledger/manifest/liveness/object-store 83/83, with
syntax and diff checks green. Tests prove exact JA pack/approval/account/
integration identity, exact Postiz integration with no profile-state path,
target-only temporary arm and fence, byte/mode restoration on success and
unknown provider failure, native verification before one Telegram, and
same-slot publication/message replay 0. The live read-only preflight still has
integration enabled for profile `anicca.jp.videos`, zero target rows and zero
exact-caption rows among 47 Postiz rows in the last 36 hours, zero matching
local jobs/receipts, manifest `pack-ready` with armed 0, and fence closed.
Fresh adversarial review returned `SHIP`. Exact approval
`object://sha256/f7b1109d72fa4514419e9c15e16c4ae1910697a6f31526767dbd76b6ba52c855`
is SHA-exact at mode `0600` and binds only this account, integration, pack,
creative, video, and caption. The next allowed state is the final
closed-control preflight followed by at most one provider effect; provider
writes remain 0. That final preflight now passes: the live integration is
enabled for `anicca.jp.videos`; 47 recent Postiz rows contain zero target rows
and zero exact-caption rows; matching local jobs/receipts remain zero; all four
objects are SHA-exact/mode `0600`; manifest `d3ee329a…906b9` is pack-ready with
armed 0 and the fence is closed. A separate no-effect transport probe then
reproduced system DNS failure inside Python `urllib` before any HTTP request,
while the same Postiz API and integration return HTTP 200 through an exact
current DoH-resolved address. Provider writes therefore remain 0. The active
blocker fix is a minimal TDD, opt-in, exact-Postiz-host DNS override; the canary
does not start until that path is GREEN, reviewed, and the closed-control
preflight passes again. TDD is now GREEN: the override accepts only one public
IPv4 literal, remaps only exact `api.postiz.com` within a scoped context, keeps
the HTTPS hostname/TLS identity unchanged, and restores the original resolver
on exit. Focused Postiz tests pass 19/19, combined distribution tests pass
39/39, syntax/diff checks pass, and a real read-only Python `urllib` probe now
returns HTTP 200 for the exact integration before restoring the resolver.
Provider writes remain 0. Fresh adversarial review is the only remaining code
gate before the final preflight and one effect. That fresh review returned
`SHIP`: it confirmed public-IPv4 and exact-host scope, preserved TLS hostname,
resolver restoration on exceptional paths, no material `_publish` behavior
change, and no relevant single-process CLI concurrency leak. The next allowed
action is the repeated closed-control preflight followed by at most one effect.
The repeated preflight passes with the same enabled integration, 47 recent rows
but zero target/exact-caption rows, zero local jobs/receipts, pack-ready manifest
`d3ee329a…906b9`, armed 0, and a closed fence, all control files mode `0600`.
The initial CLI spelling `2026-08-25T22:28:41Z` was rejected by the runner's
canonical-instant parser before env, state, or provider access. Postiz target
rows, local jobs/receipts, manifest bytes, armed count, and fence all remained
unchanged. The same instant's required canonical spelling is the one immutable
logical slot `2026-08-25T22:28:41.000Z`. This input correction is not a second
effect; no other instant, retry after provider access, account, or integration
is authorized.

That sole provider effect is complete as Postiz row `cmt98nnld02pdp20ypm3ohqna`
at direct `https://www.instagram.com/reel/DcetvubDA4Z/`; manifest bytes are
restored to `d3ee329a…906b9`, armed 0, and the fence is closed. Telegram remains
correctly held at 0. The public captioned embed binds owner
`anicca.jp.videos` and the exact approved caption. Native MP4
`object://sha256/4d450f923746e4ed59837f6593674512d09ebad0755d93f9fa6a506053102154`
is 17.831 seconds H.264/AAC; the full-video comparator passes and parent visual
inspection of both 36-frame contact sheets confirms the exact Japanese hook,
woman, and complete Anicca lock-screen Widget installation. Native contact
sheet is `object://sha256/24e62343ba84f6b645a9351fe8cc89028e254bdcbdb7ac16a6cf7755d9ac023a`.
The first immutable evidence/verification pair `a615234d…473eb0` /
`e1dae40f…fa34d2` remains retained but is superseded because its embedded
caption omitted the approved caption object's final LF. A fresh live readback
again proves exact owner/caption/direct URL and fetches the same native SHA.
Corrected evidence
`object://sha256/65401d93fbd845bdbcd957673c2f2daa6e42b1d0df2184d03bc1a5392213c1f9`
is byte-identical to caption object `b57c4f89…e1c6d`, including its final LF;
corrected verification is
`object://sha256/1980f2309b600d948262f1f3aed8544ee6ec7ea2ee2b19a160d5ed2d4f8a318f`.
Both are SHA-exact/mode `0600`. The remaining runtime gate is to make the
runner's live embed/media re-fetch tolerate the same DNS outage and Instagram's
double-escaped `video_url` without weakening host/TLS/content checks. No second
publication is allowed; the existing receipt is the only input to this TDD fix.
That fix is now GREEN and fresh-review `SHIP`. It decodes JSON-style Unicode
escapes only inside the extracted media URL, rejects credentials/ports/fragments,
falls back only on DNS errors, accepts only valid CNAME rows plus public IPv4 A
records, disables ambient proxies, and invokes bounded/no-redirect HTTPS curl
with `--resolve` so Host/TLS SNI remain native. Focused runner tests pass 25/25
and combined tests pass 89/89. A real fallback read gets embed/media HTTP 200 and
the fetched native SHA is exactly `4d450f…02154`. At that state, the only
allowed action was a same-slot replay with corrected verification
`1980f230…8a318f`; it could create only the one natural Telegram receipt and no
provider effect.
That replay is now terminal: natural publication Telegram `34523` contains the
same direct URL, and a further same-slot replay returns publication 0, message
0, and Telegram transport calls 0. The message job has exactly one
enqueue/claim/complete sequence and one receipt. Immediate Postiz post analytics
measures Views `173`, Reach `134`, Likes `1`, and Saves/Comments/Shares `0`;
account analytics is HTTP 200 with an empty response and remains `unavailable`,
not zero. Product attribution is also unavailable, while 2h/24h/72h/7d owner
registration remains explicitly `pending`, in metric status
`object://sha256/fab4b0ed640af823fe99ee13b5b65248cca1c8ad728951a9750fcf1e5c2539c6`.
Manifest `marketing-lane-manifest:1d9ce1df6b639bf6a0ef282a61f5ca506b8748dfe0390cda5b378bee19ec8d15`
records only this target's `canary-verified` transition; all lanes remain
default-off/armed 0 and the fence remains closed.

The MKT-09R8-06 external effect is **terminal verified**.
The dedicated runner and combined publication/ledger/liveness tests are green,
the live target integration is enabled, the exact caption has 0 target rows in
the last 24 hours, all 9 lanes remain unarmed, and the fence remains closed.
The first adversarial review rejected arbitrary evidence bytes and missing
runner-owned manifest/fence restoration. TDD now proves exact target-only controls,
byte/mode restoration on normal success and provider failure, receipt replay with
no arm, and the live manifest's separate internal account alias and native profile.
The second fresh review's self-attestation defect is now closed: the runner fetches
the captioned Reel and native CDN MP4 over HTTPS, binds its bytes by SHA, rejects
caller match booleans, and applies ffprobe/ffmpeg comparison. Parent read-only E2E
against a real Instagram Caption embed and CDN MP4 passed, while the same-caption
historical Reel correctly failed against the different approved person/content.
The third review's author-anchor, first-second-only comparison, and final embed URL
defects are closed under TDD. The fourth review's bounded truncation path is also
closed: duration tolerance is capped at 0.25 seconds, both streams are sampled at
2 fps across the longer timeline, at most one terminal frame may be absent, every
compared SSIM score must be at least 0.95, and the 15.0-second versus 13.5-second
regression fails closed. The fifth fresh read-only review returned SHIP after
focused 12/12 and combined 76/76 tests. At that pre-effect gate, MKT-09R8-06 was
the only active item and required the exact final approval object plus closed
control readback before its one Postiz call. No retry, fan-out, scheduler arm,
or legacy-job action was allowed.

The exact final approval is now
`object://sha256/9c7514fc4c49f4632c0f35cb25f692fd9106960500ae936dfefc89aaddac01a3`
at mode `0600`; readback binds only the approved pack, video/caption digests,
`@anicca.en`, and integration `cmn8y95rg02d2qx0y09bbk5pb`. A fresh
read-only preflight found 30 live integrations with that Instagram integration
enabled, 32 Postiz rows in the last 24 hours but zero for the target integration
and zero exact-caption rows, and no exact local job or receipt. Manifest
`marketing-lane-manifest:11ace472e123b4f025bce13d10a61b89f1d851863dd907107daadade5d3058be`
still has 9 targets, 21 holds, and armed 0; the target remains `pack-ready`
and `default-off`, and the publication fence remains closed. The next and only
allowed external effect is the single exact canary using logical slot
`2026-08-25T21:03:33.000Z`.

That single call created Postiz row `cmt95nd89023fp20yna8g8olx` and direct
candidate `https://www.instagram.com/reel/DcekGtmjmOf/`; local job
`marketing-video-publication:e98e8552c03c8a16d0aa7ff211fbe1af630a25ddc917b7fe7dc7f5b8640ad528`
is `completed`, `unknown_effect=false`, and the reconciled receipt records
`published_at=2026-08-25T21:06:17.106Z`. This is not yet success: the LM
Telegram publication receipt is held until exact native owner, caption, and
full-video visual bytes pass. The runner restored manifest
`marketing-lane-manifest:11ace472e123b4f025bce13d10a61b89f1d851863dd907107daadade5d3058be`
with armed 0 and the target `default-off`/`pack-ready`, restored the fence
closed, and preserved both control files at mode `0600`. No retry or second
publication is allowed.

Native readback now binds exact `CaptionUsername=anicca.en`, the normalized
approved caption, and CDN host `scontent-nrt1-1.cdninstagram.com`; imported
candidate bytes have SHA-256
`90f8e8413ffa0ae0e539a7a4f00160031f8f479998ac35c09dc99ebb26dcd390`,
H.264/AAC 720x1280, and duration 15.102708 seconds. Parent visual inspection of
all 30 half-second source and native frames found the same woman, English hook,
lock-screen screens, and complete Anicca Widget installation sequence. The
first comparator result was false because Instagram color/recompression produced
four frames just below 0.95 (minimum 0.947961; mean 0.971818), not because the
content differed. TDD now adds an Instagram-like recompression regression and
uses a per-frame floor of 0.945 while retaining the 0.25-second duration,
full-timeline, changed-tail, and truncation gates. Focused 12/12, combined 76/76,
and the actual native comparison are green; a fresh adversarial review was
required before evidence import or Telegram release. That fresh review returned
SHIP: it independently reproduced the actual 30-frame score distribution and
the new Instagram-like regression, reran focused 12/12 and combined 76/76, and
confirmed that wrong solid content, changed tail, and 15-to-13.5-second
truncation still fail closed. The immutable evidence import and runner live
refetch passed. LM natural-language Telegram `34435` carries the same direct
URL; same-slot replay created publication 0 and message 0 with the same message
ID. Native video
`object://sha256/90f8e8413ffa0ae0e539a7a4f00160031f8f479998ac35c09dc99ebb26dcd390`,
contact sheet `object://sha256/6ca2d141d79e71d8f5d083d4f61a6b6d7c2625d1626a1ff46d3279e31fde5f2b`,
evidence `object://sha256/756925fa1f7505f45a6a4821b633477dff3779538f5b24b94f547ddd46bc6e44`,
and verification
`object://sha256/d0abae7654a5ebe0f59aa659ad5ece0004305bac69691a49159c63e501184eba`
all read back at mode `0600`.

Immediate Postiz post analytics measured Views `14`, Reach `0`, Saves `0`,
Likes `0`, Comments `0`, and Shares `0`. Account analytics is explicitly
`unavailable/empty_provider_response`; installs, activations, trials, paid,
and proceeds are explicitly `unavailable/attribution_not_configured`, never
zero. Corrected metric status
`object://sha256/ef76f419e368998866e9086fea5ac7ba9f3c4b7cc2e564b43e370dc29ed9c646`
marks 2h/24h/72h/7d collection registration `pending`; due times alone are not
claimed as a running loop. Final manifest
`marketing-lane-manifest:f72c663ebc28c8e4d3bc28d236d4c38b200e1fe2e5074455d9d1f7c222ab9d42`
records `canary-verified`, default-off, target limit 2/day, actual scheduled
0/day, armed 0, and 21 holds; the fence is closed.

MKT-09R8-07 is now **terminal classified** for exactly one next account. Live
Postiz integration `cmmzzg2es0539p30ycb94ayx0` is enabled and binds Instagram
profile `@anicca.jp.videos`. Direct native Reel
`https://www.instagram.com/reel/DbYPO6SoOQU/` binds that owner to the exact
Japanese Widget caption; visual inspection across the full 17.83-second native
video shows a Japanese hook, iPhone lock-screen customization, and the visible
Anicca Widget installation flow. A second direct Reel,
`https://www.instagram.com/reel/DbY5kvdICul/`, proves the same account also has
a distinct Japanese affirmation/Card sequence, so that creative is explicitly
excluded from this lane instead of inheriting the mixed history. The two
disabled legacy Widget JA crons remain untouched; their 08:05 and 18:20 JST
cadence supplies the healthy target only, while their obsolete Instagram
integration ID is rejected. Manifest
`marketing-lane-manifest:20e4570fe02f5675465fa3846b21133d346e36f31d0856a68f3625164557997a`
validates at mode `0600` with 10 targets, 20 holds, every target `default-off`,
armed 0, and the fence closed. Provider, Telegram, scheduler, and legacy-job
writes were 0; actual cadence remains 0/day.

MKT-09R8-08 is now **terminal pack-ready** for that same account. The disabled
legacy renderer was inspected read-only and rejected as runtime code because it
randomly chose `widget-ja/v*.mp4`, selected a separate LRU caption hook without
rendering it into the video, fanned out to TikTok/Instagram/YouTube, and masked
publisher failures with `|| true`. All four source videos were inspected across
their full timelines. `v1` is the forbidden affirmation/Card flow, `v3` ends
before the complete Widget selection, and `v2` has a different hook. Only `v4`
has the exact Japanese hook `ロック画面にアファメーション / 置けるの知らなかった`
plus the complete Anicca Widget installation sequence seen in direct native Reel
`https://www.instagram.com/reel/DbYPO6SoOQU/`. At 2 fps across the full shared
timeline, source/native SSIM is minimum `0.977367` and mean `0.991391`.
Approved pack
`object://sha256/16d4452d5e3d408d76b915b76e80bf014a57b6354bf133391c785542db6f7696`
binds exactly video `object://sha256/0c67b0a4d1de17c1a4221f4ccfbc1ad798cec9ba45db6a1bcd3d0f48a12cd188`,
caption `object://sha256/b57c4f89186fd453618cc3a0396f8ce73f297908732737a545331853d77e1c6d`,
and full-frame evidence
`object://sha256/f1c5b5c2fd0262fc842b7879c2a5f4a19630a9f0533a9f7c9f54dee10ff3a8b7`.
Every object reads back by exact SHA at mode `0600`; the pack contains no
OpenClaw path, environment, asset reference, or secret. Manifest
`marketing-lane-manifest:d3ee329adef52a7cac66d1fefe0be8b80acd6916d9b2a5e09d2b506b48f906b9`
records `pack-ready` with 10 targets, 20 holds, all targets `default-off`, armed
0, and fence closed. Provider, Telegram, scheduler, and legacy-job writes were
0; actual cadence remains 0/day.

| ID | Atomic action | Account/lane | Done evidence |
|---|---|---|---|
| MKT-01 | **done —** Port I-3 claim, receipt, Telegram dedupe, and replay state from PostgreSQL/`pg` to the Life Manager-owned local JSONL/atomic-file ledger | all lanes | direct local process restarts cleanly; 32/32 focused tests; 149/149 runtime-adapter tests; 8/8 runtime-path tests; live/dead lock recovery stress 20/20; duplicate claim/effect/notification count is 0; expired external effects reconcile instead of retrying |
| MKT-02 | **done —** Read the live Postiz integration registry and freeze a redacted multi-platform lane manifest containing integration ID, provider, profile, locale, product, and disabled state | Honne TikTok/Instagram; Anicca TikTok/Instagram/YouTube | live GET `HTTP 200` with 29 rows; manifest `marketing-lane-manifest:9867179bbb8db1cbd434800562a92c40935b353789c2c60de4027dba9895790c` at the Life Manager data root, mode `0600`; eight explicit routes validate and all remain non-production; Honne Instagram is recorded as unassigned because no live profile exists; no provider write |
| MKT-03 | **done —** Run one controlled publication using the Life Manager route, reconcile `PUBLISHED`, verify the direct TikTok `/video/<id>` URL, and send one Telegram receipt | Honne EN `@honne_reveal` | approved-pack creative `HEN-005-154a1508e0a8` reconciled provider row `cmt2nc8gj00x5ph0yodvxq4dm` to `https://www.tiktok.com/@honne_reveal/video/7676388327427149077`; Telegram `message_id=27358`; initial and second replay created zero new effects. Historical generic row `7676366077437233172` / `message_id=27226` remains quarantined evidence |
| MKT-03A | **done —** Extend the generic publication contract and direct-URL verifier to YouTube while keeping Postiz as the provider | selected **Anicca** YouTube integration only | adapter accepts Anicca-only YouTube jobs with `youtube_integration_ref`; `/shorts/<id>` and `/watch?v=<id>` receipts pass while profile URLs fail; shadow plan creates one job and provider writes remain 0; both live candidates stay disabled |
| MKT-03B | Run one controlled Postiz fan-out canary with one effect key per platform, one selected product lane at a time: Anicca on TikTok/Instagram/YouTube or Honne on TikTok | one selected production-armed product lane | **partly done —** the Anicca iOS JA `reelclaw-card` `card-ja/v4` creative `AJ-CARD-001-35a15c7ce990` now has verified TikTok `https://www.tiktok.com/@anicca.jp/video/7676422253638176020` (row `cmt2s158o02kyph0yvht8d8wd`, Telegram `27500`) and Instagram `https://www.instagram.com/reel/DcTFx_UjSio/` (row `cmt2sfjcx02bapj0ymsu4tapf`, Telegram `27510`); both replayed with zero new effects. The earlier `lm_wake` row remains quarantined. YouTube remains disabled and metrics/attribution remain open |
| MKT-04 | Run Honne EN on-demand cycles one by one and prove seven consecutive receipts | Honne EN `@honne_reveal`, one explicit manual trigger per cycle | **removed by owner instruction** — retain the single HEN-006 receipt as evidence; no calendar trigger or seven-cycle wait remains |
| MKT-05 | Repair the known hook, asset, poster-argument, and environment-boundary defects before any publication | Honne JA `@honnevideo` | **done —** migrated `honne-ja` ReelClaw pack runs through LM object refs only; no legacy hook/path/env/asset read reached the provider |
| MKT-06 | Restore Honne JA on-demand publishing and record its account receipt | Honne JA `@honnevideo` | **done —** creative `HJA-011-ed3318c496f4` published at `https://www.tiktok.com/@honnevideo/video/7676425660641889537`, provider row `cmt2siqgp0009nt0yoi1qz7lf`, Telegram `message_id=27515`, replay 0; old `honne-ja-fresh` remains disabled |
| MKT-07 | Repair and canary the JP4 lane | Anicca JP4 `@anicca.jp4` | **done —** the failed `AJ-CARD-002-7e24db967bf7` effect remains reconciled `absent` and was never retried. A new Life Manager-only `AJ-CARD-003-5639e14832ad` nudge-card effect used the approved card media `5639e148…`, JP4 approval `97f2c5fb…`, and Postiz row `cmt328uot00s2qk0y23e8ptii`. Postiz's numeric release suffix was not trusted: caption/profile readback verified `https://www.tiktok.com/@anicca.jp4/video/7676495865816632583` (HTTP 200). Natural Telegram receipt `message_id=27939` carried that same direct URL; replay created zero publication and zero message effects. The JP4 runner rejects any non-pack media, wrong approval, wrong integration, or terminal job before claim |
| MKT-08 | Repair and canary the iOS lane | Anicca iOS `@anicca.jp` | **done —** `AJ-CARD-001-35a15c7ce990` remains verified at TikTok `https://www.tiktok.com/@anicca.jp/video/7676422253638176020` (Telegram `27500`) and Instagram `https://www.instagram.com/reel/DcTFx_UjSio/` (Telegram `27510`); current local replay for both jobs returned `created=false`. TikTok account metrics are Followers `257`, Videos `470`, Views `5,252`; TikTok post-level analytics returned an empty array and is recorded unavailable, while Instagram recorded Views/Reach `6` and Saves/Likes/Comments/Shares `0` |
| MKT-09 | Migrate and recover the remaining Larry/ReelClaw accounts one by one, preserving each measured account/locale contract | remaining Anicca/Honne accounts | **in progress —** R0 through R8-06 are terminal: all 13 unknown/stale effects are closed as 12 `present` and one `absent`; `@anicca.encards`, `@ani.cca1234`, and `@anicca.en` each have isolated direct-native canaries, natural Telegram, replay 0, and explicit metric-source status while remaining default-off at actual 0/day. R8-07 now classifies exactly one next retained account; no cadence arm starts early |
| MKT-09A | Encode the §8.8.1 target and hold dispositions in the Life Manager lane manifest; no provider write | all measured TikTok/Instagram/YouTube integrations | **done —** owner-directed YouTube skip advanced the validated schema-v2 manifest to `marketing-lane-manifest:55eb386417bd38547ceaad2a80936e51f272bec964387b0751c55a1fa60efda9`: six classified non-YouTube target rows, 22 `hold` rows at `0/day`, exact `@anicca-jp` YouTube row held with `provider_disabled=true`, armed rows `0`, file mode `0600`, provider writes `0`. The prior seven-target manifest remains historical state evidence |
| MKT-09B1 | Repair and read back the exact selected YouTube integration; no publication | YouTube `@anicca-jp` (`cmn1oukj9012nnq0yqhouc3ib`) only | **skipped by owner instruction; 0/day.** The first exact canary effect remains terminal `absent`: local job `marketing-video-publication:272c9640dbb10b04706d3f4a52335c8a54d92e764485b66a10fcedf137bfba00`, Postiz row `cmt41nl0809imqp0yx5rls3zp`, provider state `ERROR`, error `Refresh channel needed`, no direct URL/release ID/Telegram receipt, and no retry. OAuth diagnosis remains evidence only; no owner login is required while the lane is skipped |
| MKT-09B2 | Publish and verify one selected Anicca JA YouTube Shorts canary | YouTube `@anicca-jp` (`cmn1oukj9012nnq0yqhouc3ib`) only | **skipped by owner instruction; 0/day.** No replacement effect, success receipt, metrics claim, or scheduler arm |
| MKT-10A | Read and persist the complete available metric surface for one verified account before arming it | Honne EN TikTok `@honne_reveal` only | **done —** 24h snapshot `object://sha256/b6dee2a5bf17b36e5e66999f25de0d534afbaa0da40ef547dce3e6c4985edb05` binds the verified direct URL, `account_id`, caption, and native evidence `object://sha256/0021ec3f8d3d9cb3b37de2092065f6e2a52049ba68715f17021f6dde99532de9`; identity and account metrics use separate non-colliding fields. Native post metrics: views `187`, likes `6`, comments `0`, shares `0`, saves `0`, derived engagement `3.21%`; reach/watch time/completion are explicitly unsupported/unavailable. Postiz post analytics is unavailable because its stored `v_pub_file~…` release ID returns an empty array and its correction endpoint only accepts `releaseId=missing`; this is not converted to zero. Postiz account metrics: followers `5`, following `7`, total likes `1,427`, videos `177`, and latest-20-video aggregate views `4,266`, likes `136`, comments `0`, shares `0`. The 2h snapshot was not captured and is `source_delayed`; 72h/7d remain pending; no publication or cadence change |
| MKT-10B | Make the verified full-metric native fallback repeatable in Life Manager | Honne EN TikTok `@honne_reveal` only | **done —** `tiktok-native-metric-source.js` and its thin CLI reject wrong direct URL/video/account/caption and unsafe local identities before persistence; the real 24h snapshot first returned `created=true`, replay returned `created=false`, and wrong caption/account/video all failed closed. Immutable repeatable snapshot `object://sha256/dfabd2c6456a1eae0540b69973b0f9424fd8f1dd31ba3db8a22f1a7b2fb550d2` preserves views/likes/comments/shares/saves, derived engagement, and explicit unavailable reach/watch time/completion; no publication or cadence change |
| MKT-10C | Enable one healthy account at its §8.8.1 channel limit | Honne EN TikTok `@honne_reveal` only | **done —** schema-v2 manifest `marketing-lane-manifest:89d39d96bfa0369add58de7dac45164b40f4ed29022ad157c38e524763dc0b5d` arms exactly `@honne_reveal` at `target_daily_limit=3`; five other target lanes and all 22 holds are unchanged. New LM-owned trigger `ai.anicca.life-manager-honne-en` has only 07:00/11:00/20:30 Asia/Tokyo calendar events, reads only `~/.local/state/life-manager/.env`, and logs only below the LM state root. Readback immediately after bootstrap was `not running`, `runs=0`, `last exit=(never exited)` with unchanged job/receipt line counts, so arming itself created no post. The legacy ReelClaw label remains disabled and untouched; focused manifest/cycle tests pass 18/18 |
| MKT-10 | Enable the §8.8.1 three-per-day policy only after each account canary and metrics health gate passes | every production-armed selected destination | exactly 3/day on every selected available TikTok/Instagram destination, enabled one account and one slot at a time; no duplicate effects and no silent misses; skipped/missing routes remain 0/day |
| MKT-11-HJA1 | Read and persist the complete available 24h metric surface | Honne JA TikTok `@honnevideo` only | **done —** direct native account/video/caption match persisted post Views `1,035`, Likes `11`, Comments/Shares/Saves `0`, derived engagement `1.06%`, and explicit unavailable reach/watch time/completion. Postiz post analytics returned HTTP 200 with an empty array and is `unavailable/empty_response`, not zero or success. Postiz account analytics measured Followers `4`, Following `8`, Total Likes `923`, Videos `278`, and latest-20 aggregate Views `5,650`, Likes `42`, Comments/Shares `0`. Composite snapshot `object://sha256/0b0433ff74d9c4d6f387c0158d4ce416b53fdebe61fb3f1f2ef9d07e3babfd47`; native identity evidence `object://sha256/19563bba9c566bfb5f3bd88c1341131de9e36089d5a076561f05a8212d5d004f`; native replay `created=false`; job/receipt line counts unchanged at 84/24; Honne JA remains default-off |
| MKT-11-HJA2 | Make the combined post/account metric read repeatable before cadence eligibility | Honne JA TikTok `@honnevideo` only | **done —** the existing `tiktok-native-metrics-read.js` accepts the exact integration and provider row as additional identity keys, reads TikTok embedded JSON plus Postiz account/post analytics, and writes one mode-`0600` immutable combined snapshot. Real first run created `object://sha256/72e1665d65656a2071174ed6caa882c3834c13de5e0fc32d4195ca601eb7551d`; replay returned `created=false`; wrong caption failed before provider persistence. Empty Postiz post analytics remains `unavailable/empty_response`; all eight account metrics and every available native post metric are source-labelled; a future non-empty Postiz result preserves its Views/Likes/Comments/Shares instead of discarding them. Focused tests pass 2/2; job/receipt counts remain 84/24; no cadence change |
| MKT-11-HJA3 | Enable the next healthy account at its TikTok limit | Honne JA TikTok `@honnevideo` only | **done —** schema-v2 manifest `marketing-lane-manifest:902c5db24142d764ab064bfc252dc2428b510798106c8abeae0a7f97df6f33ae` arms exactly Honne EN and Honne JA at `target_daily_limit=3`; four Anicca targets and all 22 holds are unchanged. New LM-owned JA trigger has only 08:30/12:30/21:30 Asia/Tokyo events, reads/logs only below the LM state root, and has no RunAtLoad. Immediate readback was `not running`, `runs=0`, `last exit=(never exited)` with job/receipt counts unchanged at 84/24, so arming created no post. Legacy JA labels remain disabled and untouched; focused tests pass 2/2 |
| MKT-11-AJM1 | Read the complete native metric surface before any Anicca main cadence change | Anicca main TikTok `@anicca.jp` only | **done —** native embedded JSON matched direct URL `7676422253638176020`, account `@anicca.jp`, video ID, and exact Anicca JA card caption `強い人の口癖、5つだけ`. The immutable 24h snapshot `object://sha256/aac182917b92e10888acb140e926ff9b41e488d29963c0f3c04924b6ed3e8bb5` records Views `108`, measured Likes/Comments/Shares/Saves `0`, derived engagement `0%`, and explicit unavailable Reach/Watch time/Completion. Replay returned `created=false`; wrong caption failed closed; mode is `0600`; job/receipt counts remain 84/24; Anicca main remains default-off. Initial object import hit ENOSPC, so only the regenerable npm cache was cleaned; retry succeeded without deleting runtime evidence |
| MKT-11-AJM2 | Read the complete Postiz account/post source through the combined collector | Anicca main TikTok `@anicca.jp` only | **done —** exact integration `cmp9sdev5012voh0y58qs45xc` and provider row `cmt2s158o02kyph0yvht8d8wd` are bound into combined snapshot `object://sha256/631074806072b8939a77051189ca70b7122fe4d8e604de8b2169fcdd3a4a248b`. Postiz post analytics returned an empty array and remains `unavailable/empty_response`; account analytics measured Followers `257`, Following `33`, Total Likes `17,662`, Videos `470`, and latest-20 aggregate Views `5,296`, Likes `51`, Comments `1`, Shares `2`. Native post metrics remain source-labelled; replay returned `created=false`; mode is `0600`; job/receipt counts remain 84/24; both Anicca TikTok and Instagram remain default-off |
| MKT-11-AJM3 | Enable only the healthy Anicca main TikTok destination | Anicca main TikTok `@anicca.jp` only | **historical done; current routing superseded by MKT-10R0–R7 —** this record proves the former `reelclaw-card` arm and its no-arm-time-effect readback only. It is not the current creative contract. The current route is Larry sunset six-image native photo carousel, integration `cmp9sdev5012voh0y58qs45xc`, scheduled 0/day/default-off until MKT-10R6. The old manifest `marketing-lane-manifest:c294e4e13f2c54a914f4a9ac2c52521d34630b3772225df2fe2d4d0a406f8008` must not be restored |
| MKT-11-AJIG1 | Capture a repeatable complete Instagram metric source before any Instagram cadence change | Anicca main Instagram `@anicca.jp1` only | **done —** direct Reel `https://www.instagram.com/reel/DcTFx_UjSio/` was re-read and bound to native owner `anicca.ios.jp`, exact caption `強い人の口癖、5つだけ`, Postiz integration `cmn8ycvtn02djqx0ytuisn9mw`, and row `cmt2sfjcx02bapj0ymsu4tapf`. Immutable 24h snapshot `object://sha256/e43de2bf938b617e8f0515cbdbaa786252add9b614c13c92b35534662c1f7bd7` records Views `32`, Reach `31`, Saves/Likes/Comments/Shares `0`, derived engagement `0%`; Impressions/Watch time/Average watch time/Completion and account totals remain unavailable with reasons. Replay returned `created=false`, wrong owner failed closed, file mode is `0600`, focused check passes 1/1, and Instagram remains off |
| MKT-11-AJIG2 | Deliver every Instagram observation as natural-language Telegram from the same immutable snapshot | Anicca main Instagram `@anicca.jp1` only | **done —** the existing durable `marketing.liveness.telegram` job now accepts a fail-closed `observed` metric payload and renders every measured value plus all unavailable names, direct Reel URL, window, and immutable object ref. Identity is content-addressed, so the same snapshot builds the same job/effect key. The Instagram collector imports a newly created snapshot into LM object storage, enqueues/claims/completes that message through the local durable ledger, and skips Telegram entirely on snapshot replay. Focused checks pass 7/7; real 24h replay returned `telegram.created=false/reason=snapshot_replay` with jobs/receipts unchanged at `84/24`. The already-sent 24h natural report remains the historical first delivery; it was not duplicated |
| MKT-11-AJIG3 | Schedule all four observation windows and natural Telegram delivery from verified publication time | Anicca main Instagram `@anicca.jp1` only | **done —** LM-owned `ai.anicca.life-manager-instagram-metrics` runs the due planner every 1,800 seconds. Each 2h/24h/72h/7d snapshot and Telegram effect is content/window deduped; values resolve from the immutable LM object at execution. A window over 90 minutes late becomes all-unavailable `source_delayed`, never current values mislabeled as historical. Real loop E2E created 2h object `object://sha256/99252a1f3563c6418775fb3e35879e58b284e23235ca1930f87de6d9cdb8880c` and natural Telegram `message_id=28561`; immediate replay added zero jobs/receipts. 24h is complete; 72h is due `2026-08-24T10:10:15.268Z`, 7d `2026-08-28T10:10:15.268Z`. Launchd readback is `not running`, `runs=0`, `never exited`, interval 1800; bootstrap changed no jobs/receipts (`87/25`). Focused checks pass 8/8; Instagram posting remains off |
| MKT-11-AJIG4 | Send one daily natural-language digest from the latest immutable snapshots | Anicca main Instagram `@anicca.jp1` only | **done —** the existing 30-minute LM loop has a `17:30 JST` daily gate. Its first run after the gate selected the latest measured immutable snapshot, preserved every measured/unavailable field, appended `2h unavailable / 24h measured / 72h pending / 7d pending`, and imported daily object `object://sha256/a46a0e56fd03839c9531aeba910937bcf9c62e5540729418c5d50305c914c73d`. The loop itself sent natural Telegram `message_id=28565` with Views `32`, Reach `31`, Likes/Comments/Shares/Saves `0`, Engagement `0%`, and explicit unavailable Impressions/Watch time/Average watch time/Completion/account totals. Immediate same-day replay returned `snapshot_replay`, adding zero jobs/receipts (`90/26` stayed unchanged). Focused checks pass 8/8; no manual composition and Instagram remains off |
| MKT-11-AJIG5 | Discover every future verified publication on the same Instagram account before cadence arm | Anicca main Instagram `@anicca.jp1` only | **done —** the armed 30-minute metric loop now reads the LM-owned `anicca-ios` distribution ledger and accepts only `published + provider_reconciled=true` Instagram rows with direct `/reel/<shortcode>/`, exact `reelclaw-card/nudge-card/ja`, a valid provider row, and a caption object whose bytes match its recorded SHA-256. Each accepted row derives its own shortcode, caption, provider row, and publication time; all persistence/collection/daily paths are parameterized by that verified identity. Focused checks pass 9/9, including discovery of a new valid row; real current-lane replay discovered `DcTFx_UjSio`, kept 72h/7d pending, returned daily `snapshot_replay`, and changed no jobs/receipts (`90/26`) |
| MKT-11-AJIG6 | Enable only the healthy Anicca main Instagram destination | Anicca main Instagram `@anicca.jp1` only | **done —** LM-owned `ai.anicca.life-manager-anicca-main-instagram` has one `19:10 JST` slot and is fixed to `anicca-ios/reelclaw-card/ja`, Postiz integration `cmn8ycvtn02djqx0ytuisn9mw`, and profile alias `@anicca.jp1`. It uses only LM env/object refs and the Postiz integration path, with no Instagram credential/profile files. A provider success must reconcile the exact caption to a direct Reel before natural Telegram. Bootstrap readback was `not running`, `runs=0`, `never exited`; jobs/receipts stayed `90/26`, proving no arm-time effect. Manifest `marketing-lane-manifest:a0e95d6574dcbfb6e4f43a941fa8549212101498d1cb6fc6191244e26d4e56fd` arms exactly Honne EN/JA TikTok, Anicca main TikTok, and Anicca main Instagram at their limits; JP4/HE remain off, 22 holds and three YouTube 0/day rows remain. Focused checks pass 20/20; the first scheduled Reel remains subject to direct content/account/Telegram/metrics readback |
| MKT-11-JP4M1 | Capture the complete native and Postiz metric surface before JP4 cadence change | Anicca JP4 TikTok `@anicca.jp4` only | **done as an early 18.37h observation; 24h gate remains open —** native embedded JSON matched direct video `7676495865816632583`, account `@anicca.jp4`, and exact caption `今すぐやれ / 完璧より完了。`; Postiz identity is integration `cmn8x8hdv028uqx0y4gdfse5t` and row `cmt328uot00s2qk0y23e8ptii`. Early immutable object `object://sha256/48e6ea8d1e10865957db5f01e9542e1ec089feec995a99ddfa29693f17b1c55f` measured post Views `141`, Likes `2`, Comments/Shares `0`, Saves `1`, engagement `2.13%`; account Followers `122`, Following `0`, Total Likes `6,839`, Videos `304`, latest-20 Views `11,873`, Likes `110`, Comments `1`, Shares `2`; Reach/Watch time/Completion unavailable. Its original incorrectly labelled `24h` file is preserved mode-`0600` under `quarantine/18h-early-observation.original.json`, while the active 24h slot is empty for the ledger-derived true due time `2026-08-22T14:46:13.230Z`. Wrong caption failed closed, jobs/receipts stayed `90/26`, and JP4 remains off |
| MKT-11-JP4M2 | Deliver every JP4 observation and daily digest from the loop in natural language | Anicca JP4 TikTok `@anicca.jp4` only | **done —** the JP4-only due planner discovers only caption-object-verified direct `@anicca.jp4` publications, schedules 2h/24h/72h/7d plus a 17:30 JST daily digest, preserves a missed window as `source_delayed`, and routes immutable snapshots through the durable natural Telegram renderer. TikTok messages enumerate all post values plus Followers/Following/Total Likes/Videos/latest-20 Views/Likes/Comments/Shares instead of collapsing them to a count. Real loop E2E sent the late 2h all-unavailable report as Telegram `28585` and daily window summary as `28586`; immutable objects are `object://sha256/b19728f95f7f21cc73946cbc8378358812617e52c0215d2c740e0deafd7261da` and `object://sha256/64e23aeb08013626b5c823122e530e49f07f9d427936e67db58d8ca093d503be`. Immediate replay added zero jobs/receipts (`96/28` unchanged). LM-owned label `ai.anicca.life-manager-tiktok-metrics` read back `not running`, `runs=0`, `never exited`, interval 1,800 seconds; bootstrap added no effect. Focused tests pass 11/11; JP4 stays off |
| MKT-11-JP4M3 | Verify the true 24h observation produced by the durable loop before cadence eligibility | Anicca JP4 TikTok `@anicca.jp4` only | **done —** Playwright's browser-level CDP connection repeatedly timed out before any snapshot/effect. The collector now reuses the repo's raw CDP client with an explicit isolated target ID, navigates only its owned tab, and closes that exact target. Real loop `runs=18/exit=0` matched caption object SHA `999081b7…4160` (`今すぐやれ / 完璧より完了。`) to direct URL `https://www.tiktok.com/@anicca.jp4/video/7676495865816632583`. Immutable 24h snapshot `object://sha256/0f6face148d8a7384ffdcf6fbcc5e37ca63bf4b79f4a7efb70cabbb09da708fe` records Views `151`, Likes `2`, Comments/Shares `0`, Saves `1`, Engagement `1.99%`, all eight account metrics, and explicit unavailable Reach/Watch time/Completion plus Postiz post `empty_response`. Natural Telegram is `29056`; corrected daily is `29057`. Immediate replay `runs=19/exit=0` kept jobs/receipts at `192/60`; focused checks pass 31/31 |
| MKT-11-JP4M4 | Enable only the healthy JP4 destination at its TikTok limit | Anicca JP4 TikTok `@anicca.jp4` only | **done —** manifest `marketing-lane-manifest:747f78c4f944c8c41280d9494b99969e12d79fc7543ef3e9746779ec46e331c1` arms JP4 at `target_daily_limit=3` while HE remains default-off and all 22 holds remain 0/day. New LM-owned label `ai.anicca.life-manager-anicca-jp4` has only `09:15/15:15/20:45 JST`, uses explicit JP4 integration/standing approval plus LM object refs, and has no RunAtLoad. Bootstrap readback is `not running`, `runs=0`, `never exited`; jobs/receipts stayed `192/60`, proving arm-time publication and Telegram effects were zero. No legacy label operation |
| MKT-11-HEM1 | Capture and deliver the exact existing HE effect's complete 24h metric source | Anicca HE TikTok `@anicca.he` only | **done —** the recovered effect was absent from distribution JSONL because reconciliation happened after the original provider effect, so discovery uses only exact completed publication job `marketing-video-publication:7732e4…39dd`; it requires the durable receipt's provider-reconciled URL, provider row, format/form/locale, and caption object SHA. The first loop sent late 2h unavailable as Telegram `29071`, never zero. At the exact true-24h gate, `runs=21/exit=0` matched direct URL `https://www.tiktok.com/@anicca.he/video/7676500512308481296` and caption SHA `999081b7…4160`, then persisted immutable snapshot `object://sha256/b904efb047027c9753903365f5d0fa8320030f5542c74d40c924f8099e7fa34d`: Views `125`, Likes/Comments/Shares/Saves `0`, Engagement `0%`, all eight account metrics, explicit unavailable Reach/Watch time/Completion and Postiz post `empty_response`. Natural Telegram is `29074`. Immediate replay `runs=22/exit=0` kept jobs/receipts at `198/62`; focused checks pass 14/14. No publication effect and HE remains off |
| MKT-11-HEM2 | Enable only the healthy HE destination at its TikTok limit | Anicca HE TikTok `@anicca.he` only | **done —** manifest `marketing-lane-manifest:49f5e55d95caacb8784c8cd579e306e327fa862a7b24739e0993b3748070f7df` arms HE at max 3/day while retaining the five previously armed rows and all 22 holds at 0/day. New LM-owned label `ai.anicca.life-manager-anicca-he` has only `07:15/13:45/18:15 JST`, no RunAtLoad, and reads explicit HE integration/standing approval plus existing LM card-pack refs. Bootstrap readback is `not running`, `runs=0`, `never exited`; jobs/receipts stayed `198/62`, proving arm-time publication and Telegram effects zero. Focused checks pass 21/21; no legacy operation |
| MKT-11 | Collect complete TikTok/Instagram metrics for Honne and Anicca at 2h/24h/72h/7d, then join App Store Connect, RevenueCat, and product analytics by creative lineage | every published non-YouTube account | **in progress one account at a time —** Honne EN/JA and Anicca main TikTok have 24h observations and are armed at 3/day; Anicca main Instagram is armed at 1/day. JP4 now has a repeatable true-24h source and is eligible for its isolated arm; HE remains off and follows JP4. Every source attempts the complete social/account surface and preserves unavailable explicitly. ASC, RevenueCat, and product analytics must join by creative/campaign lineage in MKT-11B before growth or revenue is attributed. YouTube remains 0/day by owner instruction |
| MKT-11P1 | Reconcile the health of every already-armed publication schedule before claiming daily cadence | Honne EN/JA TikTok and Anicca main TikTok/Instagram only | **done —** Honne EN is terminal at direct URL `7676388327427149077`, Telegram `27358`, replay 0. Honne JA is terminal at direct URL `7676832422364728584`, Telegram `28920`, replay 0. Anicca main TikTok's corrected latest direct URL is `https://www.tiktok.com/@anicca.jp/video/7676852644698262791`, corrected Telegram `28959`, replay 0; the stale duplicate-caption URL and Telegram `28951` remain quarantined. Anicca main Instagram kickstart resolves Postiz row `cmt47w3970bzgqk0y8biiy3oj` and exact caption `強さは静けさから生まれる` to direct Reel `https://www.instagram.com/reel/DcVqoxWjyNK/`; native page readback identifies owner `anicca.ios.jp` and the exact caption/hashtags. Final replay reads `runs=2/exit=0`, jobs/receipts stay `177/55`, generation/publication/Telegram are all `created=false`, and the retained natural receipt is Telegram `28657`. All four already-armed schedules are healthy; no cadence expansion and no JP4/HE/YouTube enablement |
| MKT-11A | Emit natural-language Telegram for every 2h/24h/72h/7d observation plus daily and weekly tiers from the same immutable snapshot used by the panel | every measured non-YouTube product/account/platform | **done for current daily/weekly tiers; future windows continue under durable owners —** per-post receipts use the Life Manager natural-language renderer and reject the raw legacy shape. Honne and Anicca daily summaries plus the weekly review are immutable, source-labelled, and replay-deduped. Every metric message preserves measured values and explicitly unavailable fields; future 72h/7d observations remain scheduled |
| MKT-11A1 | Connect one already-armed TikTok account to automatic window and daily natural-language reporting | Honne EN TikTok `@honne_reveal` only | **done; future windows time-gated under durable owner —** the existing TikTok due collector accepts only `honne-ai/reelclaw/relationship-confession/en/@honne_reveal`, excluding the older generic `relationship-intent` row, and verifies caption-object SHA plus direct URL. Real loop E2E sent the late 2h all-unavailable observation as Telegram `28603` (`object://sha256/99505bd2dbaa44e7ab4fbc6a0f0db9cca302fe0015f401fec389754bd24fc3d8`) and daily summary as `28604` (`object://sha256/73c7f13a8c6151bdd22c026fd5d38cb3baad4a3e3aefddf5d9819b00c583b65a`); immediate replay added zero jobs/receipts (`102/30` unchanged). 24h is due `2026-08-22T09:48:55.372Z`; 72h/7d remain pending under the same 30-minute loop. Focused checks pass 12/12; no cadence change |
| MKT-11A2 | Repeat the proven automatic reporting path for the next armed TikTok account | Honne JA TikTok `@honnevideo` only | **done —** the target-gated planner accepts exact `honne-ai/reelclaw/relationship-confession/ja/@honnevideo`, with EN/JA account and locale isolation proven. Real loop E2E sent late 2h Telegram `28607` (`object://sha256/bbf3c49c8fce986a1eb67f9269b6c63ebc3f62400383c41d6cd749bc32b180c8`), daily `28608` (`object://sha256/541d5f32248acd0ee73386c0e80377aac8b2770a374279b3283f23b283aca598`), and reconciled the pre-existing measured 24h snapshot exactly once as `28611` (`object://sha256/72e1665d65656a2071174ed6caa882c3834c13de5e0fc32d4195ca601eb7551d`). The 24h message enumerates Views `1,035`, Likes `11`, Comments/Shares/Saves `0`, Engagement `1.06%`, all eight account metrics, and unavailable Reach/Watch time/Completion. Immediate replay added zero jobs/receipts (`111/33` unchanged); 72h/7d remain time-gated; focused checks pass 12/12; no cadence change |
| MKT-11A3 | Repeat the proven automatic reporting path for Anicca main TikTok | Anicca main TikTok `@anicca.jp` only | **done —** exact `anicca-ios/reelclaw-card/nudge-card/ja/@anicca.jp` target is isolated from JP4/Honne. Real loop sent late 2h `28614` (`object://sha256/f81fedf10767c946bc8d17308d7782b69cae25856393c5a3307633ce1978691c`), measured 24h `28615` (`object://sha256/631074806072b8939a77051189ca70b7122fe4d8e604de8b2169fcdd3a4a248b`), and daily `28616`. E2E exposed that daily selection incorrectly trusted Postiz post status over measured native metrics; the root fix now chooses measured/derived snapshot fields, preserves the original report, and sent explicit daily corrections `28620` for Anicca and `28621` for Honne JA. Corrected Anicca daily object `object://sha256/9f3b2491ba1fd4811805780b3b6147751b2abc4d3f17c85ec6c582035c4b6066` contains Views `108`, all eight account metrics, and `24h measured`; corrected Honne JA object is `object://sha256/dc01889a3ea8607fdf95c95ac1c816eefcfdcc62e2866d8ea8b17071944c983a`. Immediate replay added zero jobs/receipts (`126/38` unchanged); focused checks pass 14/14; no cadence change or cross-product weights |
| MKT-11A4 | Emit product daily summaries and one weekly Honne-vs-Anicca review | all verified non-YouTube lanes | split into MKT-11A4H → MKT-11A4A → MKT-11A4W so only one product/effect is active at once |
| MKT-11A4H | Emit one Honne daily product summary | Honne EN/JA TikTok only | **done —** the LM-owned 30-minute TikTok metrics loop produced immutable summary `object://sha256/c1224a206e50a813a2115d37b7192dbaabe78140192c58d9931b32986f15f072` and natural Telegram `28973`. It contains both caption-matched direct URLs, Views/Likes/Comments/Shares/Saves/Engagement and account metrics; Installs/Trials/Paid are explicitly `取得不可`, never zero. After restoring the already-declared lockfile dependency tree, the exact launchd label completed at `runs=10/exit=0`. Immediate kickstart replay completed at `runs=11/exit=0`, returned `summary_replay`, and kept jobs/receipts at `180/56` |
| MKT-11A4A | Emit one Anicca daily product summary | Anicca main TikTok/Instagram plus explicit JP4/HE source status | **done —** the existing LM-owned TikTok metrics loop now invokes the product-generic builder and durable sender after Honne replay. Real run `runs=12/exit=0` sent natural Telegram `28983` from immutable summary `object://sha256/09861d40f28e7f16d591ea3d9abe82bfd3f5b877c0fcd22ff37901ff2c4e69d9`. Main TikTok/Instagram rows include direct URLs and measured/unavailable fields; JP4's delayed daily source remains explicit unavailable, HE remains daily-source unavailable with only its verified direct URL, and Installs/Trials/Paid remain `取得不可`. Immediate replay at `runs=13/exit=0` returned `summary_replay` for both products and kept jobs/receipts at `183/57`; focused checks pass 13/13 |
| MKT-11A4W | Emit one weekly Honne-vs-Anicca review | completed product daily summaries | **done —** the LM-owned metrics loop produced immutable weekly review `object://sha256/dcfb358df647d477106091a26897279b047c45f6f0887453dd6ebed46acf1e22` and natural Telegram `29050`. It binds Honne daily `c1224a…f072` and Anicca daily `09861d…e69d9`, reports honest `1/7` coverage, preserves each account's measured/unavailable values and direct URL, marks ASC/RevenueCat/product attribution unavailable, and explicitly refuses a cross-product winner or weight transfer. The weekly job completed with `unknown_effect=false`; immediate same-label replay kept jobs/receipts at `186/58`. Focused checks pass 14/14. A later JP4 browser timeout leaves the overall label at exit 1 but does not alter the terminal weekly receipt |
| MKT-11B | Compute attribution coverage and separate verified platform-attributed installs from partial/unattributed installs; include YouTube only for Anicca | every product and campaign | each report shows campaign ID, observation window, source status, and confidence; timing alone never becomes causal attribution |
| MKT-11B1 | Preserve creative, hook, publication, account, and campaign lineage in one attribution input | Honne and Anicca independently | **done —** the LM-owned one-shot lineage builder resolved all six selected 24h social snapshots to exactly one publication receipt using product, locale, platform, provider post ID, and caption-matching direct URL. It integrity-read the exact video/caption objects, preserved creative, first-line hook + hash, slot, publication time, account/integration, window, and immutable metric ref in `object://sha256/592db55a119c08aaa6ec3269002be6c53371f8097b27a140dd040e2567174df1`. No campaign link exists, so all six rows remain `campaign_status=unavailable` and `attribution_status=unattributed`; attribution rate is unavailable, not zero. Immediate replay returned `created=false` with the same object ref. No ASC/RevenueCat value, publication, Telegram effect, or cadence change |
| MKT-11B2 | Join App Store Connect acquisition observations without inventing creative attribution | Honne and Anicca independently | **done —** App inventory readback bound Anicca `6755129214` and Honne `6759667221` to separate API requests. Anicca's official App Downloads Standard and Discovery/Engagement Detailed segments for data `2026-08-19..21` produced First-time downloads `1`, Updates `1`, Total downloads `1`, Impressions `9`, Unique impressions `5`, Product page views `0`, and Unique product page views `0`; product totals remain `unattributed` with confidence `official_product_total_no_campaign` and are never copied to one of the four Anicca social rows. Honne had only a stale one-time snapshot, so ongoing request `c7c05836-181e-49cc-ae71-b57b7a0b466e` was created; its first report remains `unavailable/report_pending`, never zero. Immutable joined snapshot `object://sha256/a583c9d48c955941d3af54d1a52f4804018fe7bcbfb30af7466ecac13dd92e6b` binds B1 lineage `592db5…4df1`; all six social rows remain unattributed. Same-day replay returned `created=false`. The existing LM-owned 30-minute TikTok metrics loop collects ASC once after 22:00 JST; readback was runs `23`, last exit `0`. API timeout/empty data fails product-local to unavailable. The initial UTC/empty-parser zero snapshot is quarantined and not consumed |
| MKT-11B3 | Join RevenueCat trial, subscription, renewal, cancellation, and proceeds observations | Honne and Anicca independently | **done —** Life Manager does not administer RevenueCat or read another runtime's path/env. It accepts only a schema-checked product-owned observation from its product-pack inbox, rejects product identity mixing and any unavailable metric represented as zero, and imports it after the exact ASC snapshot. No valid RevenueCat API credential or product export was available without a new login, so Anicca and Honne independently remain `unavailable/product_pack_observation_missing` for trials, active subscriptions, renewals, cancellations, and proceeds; every value is `null`, never zero. Immutable snapshot `object://sha256/46769d5e1ae5f980bf24b342316fbf70f9cc7aab9254851f17efc601f9cacbc4` binds ASC `a583c9…92e6b`, forbids timing-only attribution, and labels RevenueCat proceeds as an estimate rather than store settlement. Immediate replay returned `created=false`. The existing LM-owned 22:00 metrics path now imports this product-pack observation after ASC; focused checks pass 4/4 |
| MKT-11B4 | Join product activation/retention and publish attribution coverage | Honne and Anicca independently | **done —** the existing summary renderer now joins the exact ASC and RevenueCat immutable inputs with a schema-checked product-owned activation/retention inbox. Missing product observations remain `null/unavailable`; a measured source cannot contain unavailable fields, product identity mixing fails closed, and D1/D7 retention is accepted only as an explicit cohort observation. Daily coverage `object://sha256/e03e723f04ba94b9ac57954856b7fcbe7be037ff650a1d8bf7f715276dad74e3` reports Anicca official Install `1`, with activation/trial/paid/retained/D1/D7/proceeds unavailable; Honne remains unavailable including pending ASC. Campaign attribution rate remains unavailable rather than zero. Natural Telegram `29142` was sent through the LM summary receipt path; immediate replay returned `summary_replay`. The 22:00 LM metrics path now runs ASC → RevenueCat → product coverage → natural Telegram → weekly review; future product daily/weekly summaries consume the same coverage. Focused checks pass 8/8 |
| MKT-12A0 | Establish one campaign-linked cohort before changing a hook | Honne EN only | **reopened and blocked by MKT-09 recovery —** `honne_en_base_20260823` was shared by eleven changing hooks, so it is not a one-hook baseline cohort. Rebuild it only after recovery with one immutable campaign token per baseline/challenger, then require direct-native publication, natural Telegram, replay 0, social metrics, and official product readback before changing the hook |
| MKT-12 | Run bounded learning: change one hook/format/CTA variable, keep or revert from receipts, and prove the next run consumed the decision | Honne and Anicca separately | one-variable challenger, keep/revert receipt, and next-run consumption receipt |
| MKT-12A | Run the first one-variable hook challenger from a usable attributed cohort | Honne EN only | freeze format/CTA/asset family; change one hook; compare source-labelled cohort; keep or revert; next generated publication proves it consumed the decision |
| MKT-12B | Repeat bounded hook learning without cross-product leakage | Honne JA only | same gate as MKT-12A with independent locale/account weights |
| MKT-12C | Repeat bounded hook learning without cross-product leakage | Anicca iOS only | same gate as MKT-12A with independent Anicca weights; no Honne winner is copied without a new Anicca challenger |
| MKT-13 | Retire legacy ownership only after every retained platform lane passes its canary/metrics/replay gate | entire mobile fleet | Life Manager is sole owner; Postiz remains the selected provider; old disabled state remains archived rollback evidence |

### 12.3 Atomic finish checklist

This checklist is the implementation order from the current state to the end of
the incident recovery. Complete exactly one row at a time. A `PUBLISHED`
provider row, a numeric `releaseId`, an enabled integration, or an HTTP 200
profile page is never enough by itself.

`Time-gated pending` observations are monitored state, not executable work: their
durable LM loop continues while exactly one later non-dependent row is active.
Their dependent account arm remains blocked until the observation is terminal.

| Order | Atomic action | Start only when | Done evidence | Never do |
|---:|---|---|---|---|
| 0 | **done — MKT-09R0:** restore safe writable capacity using only regeneration-safe caches and agent-owned temporary files | always | `1,610,212 KiB` available; bun/npm/hyperframes/Codex dependency/user cache contents only; 20/20 object/ledger tests; isolated LM-root probe object bytes equal, first enqueue `true`, replay `false`, readback intact; probe removed; provider effects `0` | delete evidence, state JSONL, receipts, object packs, OpenClaw assets, plists, or logs |
| 1 | **done — MKT-09R1:** add one LM-owned publication effect fence shared by every mobile cycle | Order 0 passes and before the next scheduled mobile slot | shared ledger enqueue/claim gate; mode-0600 closed state; real HEN-015 cycle and replay leave publication `42`, Telegram `140`, Postiz `16`; generation `53→54→54`; durable refusal; RED 2, GREEN 2/2, ledger 20/20, cycle/canary 26/26 | stop/restart/unload/delete a job or rely on the non-enforced manifest |
| 2 | **done — MKT-09R2:** repair JP4 caption-only false-positive reconciliation with TDD | Order 1 passes | exact media-lineage ownership across Postiz preflight, distribution reuse, and metrics; first native URL/oEmbed/thumbnail visually matches first object; second publication and Telegram `30370` terminal `conflict` with superseded receipts retained; first publication and Telegram `29982` unchanged; correction events `2/2`; replay `0/0`; metric rows for URL `1`; Python 50/50 and Node 44/44 | mutate the native post, reuse the first URL for the second video, or erase historical evidence |
| 3 | **done — MKT-09R3-01:** reconcile `b9b214111c86b0b861ce1737dedaba3a55de3ef16e1868d3e00ed9b001cd917c` | Order 2 passes | Honne JA `HJA-013` is terminal `present` at direct `https://www.tiktok.com/@honnevideo/video/7677002249733786896`; exact caption/account oEmbed; fresh thumbnail matches video object; Postiz internal suffix rejected; correction events `1/1`; replay `0/0`; provider window unchanged at 4 | treat profile URL, numeric suffix, HTTP 200, or `PUBLISHED` alone as present |
| 4 | **done — MKT-09R3-02:** reconcile `0b1f8c3fcfadb59ce46813e4081d461664ed66c038ea6a90fe54b63973299784` | Order 3 terminal | Honne EN `HEN-007` is terminal `present` at direct `https://www.tiktok.com/@honne_reveal/video/7677041244052131080`; exact caption/account oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window unchanged at 2 | retry or replace |
| 5 | **done — MKT-09R3-03:** reconcile `3d52f25e190e211350511aa472c29b2a5ab8117a5efa9e482602c0d1e0c00fb9` | Order 4 terminal | Honne EN `HEN-008` is terminal `present` at direct `https://www.tiktok.com/@honne_reveal/video/7677187822066961680`; exact caption/account oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window unchanged at 2 | retry or replace |
| 6 | **done — MKT-09R3-04:** reconcile `50d36c4ecdee8b99141d867c3c3dc5feac06da6302a287c9c08528884870fae0` | Order 5 terminal | Anicca JA `AJ-CARD-002` is terminal `present` at direct `https://www.tiktok.com/@anicca.he/video/7677353835345595664`; exact caption/account/time oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window retains one exact-effect row | retry or replace |
| 7 | **done — MKT-09R3-05:** reconcile `e97d54e5415eb593dc7ec613ae4da3205eb6e86daecba37f3367e2b1cc2663d8` | Order 6 terminal | Honne EN `HEN-010` is terminal `absent`, never success: exact Postiz upload SHA/caption/slot but no direct native artifact; internal suffix oEmbed 400; browser DOM 15 public posts, yt-dlp, and exact-caption search found no match; `public_url=unavailable`; correction `1/1`; replay `0/0`; provider window remains one row | retry, replace, or call Postiz `PUBLISHED` success |
| 8 | **done — MKT-09R3-06:** reconcile `9cf83b4855c8d9f97d300ed584b74d92e05cb4e1715743cb9fe43e042c988389` | Order 7 terminal | Honne JA `HJA-017` is terminal `present` at direct `https://www.tiktok.com/@honnevideo/video/7677435198132342033`; exact caption/account/time oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window remains three rows | retry or replace |
| 9 | **done — MKT-09R3-07:** reconcile `05024c43b9774d985bd9ea1c14dab9dc5a118057f29de84f689db3898b0b93a6` | Order 8 terminal | Honne EN `HEN-011` is terminal `present` at direct `https://www.tiktok.com/@honne_reveal/video/7677558878862675201`; exact caption/account/time oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window remains four rows | retry or replace |
| 10 | **done — MKT-09R3-08:** reconcile `78685a6f6e3f5c1b88db6f2cf7a0a3f7e2d9b59a1068117fe61a5c0cc16f9f5a` | Order 9 terminal | Honne JA `HJA-018` is terminal `present` at direct `https://www.tiktok.com/@honnevideo/video/7677574370595753232`; exact caption/account/time oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window remains two rows | retry or replace |
| 11 | **done — MKT-09R3-09:** reconcile `1eacd207e3c651c367badb07a65bcdbc8233896e685db1dd6b179aaa60a4586e` | Order 10 terminal | Honne EN `HEN-012` is terminal `present` at direct `https://www.tiktok.com/@honne_reveal/video/7677724501978713365`; exact caption/account/time oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window remains three rows | retry or replace |
| 12 | **done — MKT-09R3-10:** reconcile stale-running `8d6553d3e292f2a080875d0152daa1ea43709ef1983e79f993606939a4d501da` | Order 11 terminal | Anicca JA `AJ-CARD-001` is terminal `present` at direct `https://www.tiktok.com/@anicca.jp/video/7677736873061551377`; exact caption/account/time oEmbed and visual match; stale claim returned null and lease cleared before resolution; internal suffix rejected; job/receipt events `2/1`; replay `0/0` | reclaim and submit before readback |
| 13 | **done — MKT-09R3-11:** reconcile `404ff87aa9cc660e8656436275dd6cfe303d3053f74817fae58c595d57041741` | Order 12 terminal | Anicca JP4 `AJ-CARD-003` is terminal `present` at direct `https://www.tiktok.com/@anicca.jp4/video/7677848633974344977`; exact caption/account/time oEmbed; fresh thumbnail matches video object; internal suffix rejected; correction `1/1`; replay `0/0`; provider window remains two rows | retry or replace |
| 14 | **done — MKT-09R3-12:** reconcile `72454c23e24342175fa4d45274a6631c91b1a91530e5dedcc47ba6223e269d45` | Order 13 terminal | Honne EN `HEN-013` is terminal `present` at direct `https://www.tiktok.com/@honne_reveal/video/7677893103830764816`; exact caption/account oEmbed and visual match; internal suffix rejected; logical slot 02:00 and actual publish 09:07 remain distinct; correction `1/1`; replay `0/0` | retry, replace, or erase the seven-hour miss |
| 15 | **done — MKT-09R3-13:** reconcile `f9aa4089d72fb3b77542cd967ed9f1c5d8c4f710de79d6a268cad7b0448b5c81` | Order 14 terminal | Honne JA `HJA-019` is terminal `present` at direct `https://www.tiktok.com/@honnevideo/video/7677893274102811920`; exact caption/account oEmbed and visual match; logical slot 03:30 and actual publish 09:07 remain distinct; all 13 effects are 12 present/1 absent; publication ledger 41 completed/1 conflict/open 0; replay `0/0` | collapse multiple effects into one direct URL |
| 16 | **done — MKT-09R4:** make the 30-integration registry and enforced lane policy agree | Order 15 passes | live manifest `marketing-lane-manifest:537ad257ea887097f60b07d0e439f1bfb30b8f8a4703cd960b27b31c8fab3639` validates at mode `0600`: 17 TikTok, 8 Instagram, 3 YouTube, 2 X; all 30 owner=`life-manager`; six classified targets are `default-off`, 24 are 0/day; only YouTube `@anicca-jp` is `skip`; provider-disabled mismatches 0; armed 0. Shared enqueue/claim policy rejects an open-fence effect unless tenant/product/locale/platform/integration and exact armed target all agree; RED 35/38, GREEN 38/38. Official `postiz-agent` source was audited at `77d09c668cb2f7793989a185844d0a0c3d65c951`; legacy cron/source inventory remained read-only; provider writes 0 | mass-disable or mass-enable Postiz accounts, trust a CLI README without reading its entrypoints, or enable/kickstart/stop/restart/delete legacy jobs |
| 17 | **done — MKT-09R5:** classify exactly one remaining non-skipped mobile account | Order 16 passes | Anicca iOS / EN / Instagram `@anicca.encards` / Postiz `cmpc3gx4001nklg0y27a8o66q` is bound to `reelclaw-card`, `nudge-card-reel`, planned pack `anicca-ios-reelclaw-card-en.pack.json`, and healthy target 2/day at 12:45/21:30 JST. Native profile readback names `Anicca iOS`, exact pre-quarantine commands agree on the integration, and live manifest `marketing-lane-manifest:2e1dc72f045dd672476652c3f1d3d9c39b12bb1245d232e9546b74764c7c0c13` validates with 30 total, 7 targets/23 holds, armed 0, mode `0600`; provider/legacy writes 0 | infer product from handle or reuse another product's account |
| 18 | **done — MKT-09R6:** import or build that account's one approved pack in LM object storage | Order 17 passes | the imported canary pack is `object://sha256/cd255b6f676f0692f002ee2c9805a67fa8a32779dc2bc04f757ed068e0bcc14c` and its sole media is `object://sha256/b8e15711140e8fef6dcb37bb7ba2cce5a1a5332708a53ed8be752dbc2bf6da29`; both SHA read back exactly at mode `0600`. Direct frames from the stored object show the matching English hook `when nothing is wrong / but something is wrong` followed by two English Anicca affirmation cards. The first legacy candidate was rejected because its frame was Japanese; it remains immutable and is not referenced by this import. One hook plus one media prevents the generic selector from pairing this canary caption with a different rendered hook. Import/generation tests pass 7/7; live manifest `marketing-lane-manifest:4cb892e0bfd70aa8d97d74f043fe95aac61e593b692db2fd534842c500101113` records `pack-ready`, 0 armed lanes, and the fence remains closed. Runtime inputs are LM object refs only; no OpenClaw path/env/assets dependency and no provider or legacy-job write occurred | execute a legacy OpenClaw publisher, retain the Japanese v1 candidate, or import multiple rendered hooks that the current selector can mismatch |
| 19 | **done — MKT-09R7:** run one canary for that exact account | Order 18 passes and fence is opened only for this effect | creative `EN-CARD-A0A1D2FE-b8e15711140e` is directly verified at `https://www.instagram.com/reel/DceOe0OnGn2/`: the captioned native embed binds owner `anicca.encards` and the exact LM caption, while the native `og:image` visibly matches the English hook, person, clothing, and background in media object `b8e157…6da29`. Natural Telegram `34261` carries the same URL; generation/publication/message replay all return `created=false`. Initial Postiz post analytics measures Views/Reach/Saves/Likes/Comments/Shares as 0; account analytics is `unavailable/empty_response`, not zero. Evidence is `object://sha256/c6a40d9a66b6ac08cef710601fed36f0a2fd0d816259c55516158c19049c29be`; manifest `marketing-lane-manifest:ab627e1b5c3e9a47e6946c381343824edcf1421f21ef4c2792c81b3551c872b9` records `canary-verified`, default-off, armed 0; fence is closed. Focused tests pass 50/50 | fan out, notify before native verification, accept a profile URL, or call unavailable success/zero |
| 20 | **done — MKT-09R8-01:** classify exactly one next retained account | Order 19 terminal | Anicca iOS / JA / Instagram `@ani.cca1234` / Postiz `cmq3sq7mc000eqp0y7azfm8yk` is bound to renderer `larry`, format `native-photo-carousel`, planned pack `anicca-ios-larry-ja-v1.pack.json`, and healthy target 1/day at 16:30 JST. Native profile `アニッチャ` and its Japanese affirmation/App Store bio match the product and locale; live Postiz is `instagram-standalone`, disabled=false. The adopted disabled cron command passes this ID only as `--ig`; a separate cron that incorrectly passes the Instagram ID as `--tt`, plus legacy `|| true` and IG-failure omission, are explicitly rejected. Manifest `marketing-lane-manifest:aaf7a513cb7f19e6b4af80d3c6b8741accc1e15d24e3eab60cd535791f9a8b5f` validates with 30 total, 8 targets/22 holds, armed 0; provider/legacy writes 0 | classify multiple accounts, infer ownership from a handle, or copy the broken platform mapping |
| 21 | **done — MKT-09R8-02:** import or build only that account's approved pack | Order 20 passes | account-bound pack `object://sha256/3d6acc97e59f270a403b39a27e070265fc79d0c5d842ede19c64a5be8a9db79e` references exactly six ordered JPEG objects (`d4f003…`, `ac47a0…`, `bbd5ba…`, `366527…`, `7d1ca4…`, `052566…`); every object SHA reads back exactly at mode `0600`. Direct visual audit confirms the fixed Japanese hook plus five Japanese body slides with no blank, clipped, or foreign frame. Body source `7467383752117718303` was selected after rejecting repeated timestamp-less source `7197571464126549291`; `include_cta=false` proves six, not seven, slides. Read-only adversarial verification returned `ship`. Manifest `marketing-lane-manifest:cef325cc72d308910dde59ee5f351c9b85b4952b1f5da1c7ce75c9a5f56ab06d` records `pack-ready`, default-off, target 1/day, armed 0; fence remains closed. The one-time legacy renderer supplied migration input only: LM runtime has no OpenClaw path/env/assets dependency, and provider/history/legacy-job writes are 0 | execute a legacy publisher, add a CTA slide, reuse the repeatedly selected source, or retain a runtime OpenClaw dependency |
| 22A | **done — MKT-09R8-03A:** implement the dedicated native-carousel publication contract under TDD; provider writes remain 0 | Order 21 passes | the pinned `marketing:carousel` effect binds `anicca-ios` / JA / Larry / `@ani.cca1234` / exact integration / pack SHA / ordered six-media digest / caption SHA and still uses capability `marketing.video.publish`, so the existing fence and lane policy remain authoritative. Pack, every JPEG, caption, and dedicated approval are SHA-verified before the token or transport is reached. Postiz receives only the raw integration ID and one ordered `post_type=post` image array; the full integration ref stays in approval/receipt. Carousel receipts require reconciled direct Instagram `/p/<shortcode>` and reject profile, Reel, and numeric-release-only URLs. Provider error/result mismatch is `unknown_effect`; response loss, missing/malformed/unmatched local JSONL all reconcile `unknown`, never `absent`, so no retry is authorized without provider proof. Carousel caption preserves the exact approved UTF-8 bytes while legacy video trim behavior is unchanged. TDD RED captured missing adapter and unsupported payload; GREEN passes new Node 9/9, combined Node 47/47, Postiz plus distribution Python 55/55, `py_compile`, and diff-check. Fresh adversarial review found and the implementation fixed the response-loss duplicate risk plus caption-byte mismatch; no provider/runtime/legacy write occurred | infer absence from a missing local row, trim approved carousel copy, pass an integration URI to Postiz, or reuse the generic video receipt |
| 22B | **done — MKT-09R8-03B:** run one canary for only that account | Order 22A passes | exact effect `marketing:carousel:anicca-ios:LARRY-JA-CANARY:…` completed once as Postiz row `cmt91js0200afmp0yll86hhqj` at direct `https://www.instagram.com/p/DceW-whAQT7/`. Public captioned embed binds owner `ani.cca1234` and the exact Japanese caption; its GraphSidecar has six ordered native JPEGs and every slide is pixel-identical to its ordered LM object (RMSE `0`). Visual evidence is `object://sha256/f5ec74bc99f63a2461668648d8dc903e900953e1e67e46ae40a8e8ef74fb3d76`; native verification is `object://sha256/419dac51f71c2fce5cdb9507531ba9523e9df13d49eaef3734849baecf462cff`. Natural Telegram `34328` carries the same URL; replay creates publication `0` and message `0`. Immediate post analytics measures Views/Reach/Saves/Likes/Comments/Shares as 0; account analytics is `unavailable/empty_response`, not zero, in `object://sha256/ccf7caf29a27537a7023c09c0def8c8a49f26fda86ea302f263675a25cf61e40`. The outer tool stream ended after the provider effect but before its wrapper restored temporary controls; the exact Postiz row was read back without retry, then fence and manifest were restored before verification. Final manifest `marketing-lane-manifest:819788f19f24d5dd19926ee48273c9098efd8760dbbef1c39297a475f5a8bf8f` records `canary-verified`, default-off, armed 0; fence is closed and no legacy job was touched | fan out, send a Reel/video, notify before native verification, retry an ambiguous effect, or accept provider state alone |
| 23A | **done — MKT-09R8-04:** classify exactly one next retained account | Order 22B terminal | Anicca iOS / EN / Instagram `@anicca.en` / Postiz `cmn8y95rg02d2qx0y09bbk5pb` is bound only to renderer `reelclaw-widget`, format `widget-demo-reel`, planned pack `anicca-ios-reelclaw-widget-en.pack.json`, and healthy target 2/day at 09:30 and 19:00 JST. The live Postiz registry names `Daily Anicca Nudges`, provider `instagram-standalone`, disabled=false; native profile `Anicca Videos (@anicca.en)` has 565 posts and an indexed App Store/self-care bio. Direct `https://www.instagram.com/reel/DbInY17DSpI/` binds owner `anicca.en` to the exact English widget caption. Postiz has 392 historical rows for this integration, with recent direct Reel rows dominated by Widget EN; Card EN remains isolated on `@anicca.encards`, so old card/widget mixing is not adopted. `@aniccaen2` remains held because native availability is unproved and recent Postiz rows are 0; unavailable `@anicca.affirmation` remains held; live `@anicca.bochi` remains held because its 233-row history mixes the AI memorial/tomb product with mental-health content. Manifest `marketing-lane-manifest:d81822e8997a6b76e9085a44260f6ca2d4a50dbcfb10d76b538b090285d8e938` validates with 30 total, 9 targets/21 holds, armed 0, mode `0600`; actual schedule is 0/day, fence closed, and provider/Telegram/scheduler/legacy writes are 0 | classify several accounts, infer ownership from a handle, mix Card EN into this lane, or promote a dead/mixed account |
| 23B | **done — MKT-09R8-05:** import one approved pack for only that account | Order 23A passes | approved pack `object://sha256/645cedb029b36a2c29412b0d37d2fd756f32c7f45b7c2d2abb7d2bb08f5a524d` binds Anicca iOS / EN / Instagram `@anicca.en` / integration `cmn8y95rg02d2qx0y09bbk5pb` / `reelclaw-widget` / `widget-demo-reel` to exactly one ordered video `object://sha256/98f4ce8c607ab9122a3252ebed05b293d09698ef77400203644bef61f31a6bad` and caption `object://sha256/a9d94b852845f692aa9f63534d66386a29764f71601158ce2de65020b583787c`. All refs read back at mode `0600`; caption bytes equal the pack exactly. The 15.0-second 1080x1920 H.264/AAC object has the English hook `Since you are always on your phone / Put affirmations on your lockscreen`, a shock reaction, and only the Anicca lock-screen Widget installation demo. Direct visual inspection of all 30 half-second frames found no CTA/demo overlay, foreign language, Card content, blank, or clipping; evidence is `object://sha256/7b179dd350d06b7179bdf8527ff881ba414909d8e6f1a3779f40330ef4c9012e`. A same-hook render with baked `try Anicca — link in bio` CTA and a 40.47-second in-app Nudge/Card render were rejected. Manifest `marketing-lane-manifest:11ace472e123b4f025bce13d10a61b89f1d851863dd907107daadade5d3058be` records `pack-ready`, 9 targets/21 holds, armed 0; fence is closed. OpenClaw supplied read-only migration bytes only; the final pack contains LM object refs and no OpenClaw path/env/assets dependency. Provider/history/Telegram/scheduler/legacy-job writes are 0 | import several packs, accept a CTA/wrong-form render, or retain a runtime OpenClaw dependency |
| 23C | **done — MKT-09R8-06:** run one canary for only that account | Order 23B passes | exact one Postiz row `cmt95nd89023fp20yna8g8olx` is verified at `https://www.instagram.com/reel/DcekGtmjmOf/`: CaptionUsername is `anicca.en`, caption bytes match, CDN native video `90f8e841…dcd390` and all 30 frames show the approved woman/hook/complete Widget install. Fresh adversary returned SHIP on the bounded Instagram transcode gate; focused 12/12 and combined 76/76 pass while wrong-tail and 13.5s truncation fail. Evidence is `756925fa…bc6e44`, verification is `d0abae76…184eba`, natural Telegram is `34435`, and replay is publication 0/message 0. Immediate Views 14 and Reach/Saves/Likes/Comments/Shares 0 are measured; account and product funnel sources remain explicitly unavailable in corrected status `ef76f419…d9c646`, whose future-window registration remains pending. Manifest `f72c663e…ab9d42` is canary-verified/default-off/actual 0/day/armed 0; fence closed; no legacy job touched | accept provider state alone, notify before native verification, retry, or claim pending metric windows are registered |
| 23D | **done — MKT-09R8-07:** classify exactly one next retained account | Order 23C terminal | Anicca iOS / JA / Instagram `@anicca.jp.videos` / Postiz `cmmzzg2es0539p30ycb94ayx0` is bound only to `reelclaw-widget`, `widget-demo-reel`, planned pack `anicca-ios-reelclaw-widget-ja.pack.json`, and healthy target 2/day at 08:05/18:20 JST. Full native visual inspection proves the Widget install flow and separately proves that the account's affirmation/Card history must be excluded. Manifest `20e4570f…7997a` validates with 10 targets/20 holds, actual 0/day, armed 0, mode `0600`, fence closed; provider/Telegram/scheduler/legacy writes 0 | infer content from a handle, adopt the mixed Card history, or reuse the obsolete legacy integration ID |
| 23E | **done — MKT-09R8-08:** import or build exactly one approved Widget JA pack for that account | Order 23D passes | Pack `16d4452d…f7696` binds exactly one 17.717-second H.264/AAC video `0c67b0a4…cd188`, caption `b57c4f89…e1c6d`, and full-frame evidence `f1c5b5c2…3a8b7`. Full-timeline native comparison has SSIM min `0.977367`/mean `0.991391`; direct visual inspection rejects Card `v1`, different-hook `v2`, and incomplete `v3`. All objects are SHA-exact/mode `0600`; pack has no OpenClaw runtime dependency. Manifest `d3ee329a…906b9` is pack-ready, actual 0/day, armed 0, fence closed; provider/Telegram/scheduler/legacy writes 0 | import several packs, accept Card/incomplete content, decouple caption from the baked hook, or retain an OpenClaw runtime dependency |
| 23F | **done — MKT-09R8-09:** run one canary for only that account | Order 23E passes | the sole effect completed as Postiz `cmt98nnld02pdp20ypm3ohqna` at direct Reel `DcetvubDA4Z`; public owner/caption/native content match the approved pack. The retained first evidence pair omitted only the caption's final LF and is superseded without deletion; corrected evidence `65401d93…13c1f9` and verification `1980f230…8a318f` are byte-exact/mode `0600`. Natural Telegram `34523` carries the same URL; replay is publication 0/message 0/transport call 0 with one message ledger sequence. Immediate metrics are Views `173`, Reach `134`, Likes `1`, Saves/Comments/Shares `0`; account/product sources are unavailable and future windows pending in `fab4b0ed…2539c6`. Manifest `1d9ce1df…ec8d15` is canary-verified/default-off/armed 0; fence closed | fan out, accept Postiz state alone, retry an ambiguous effect, or call unavailable zero/success |
| 23G | **done — MKT-09R8-10:** classify exactly Anicca JA Card `@anicca.jp1` / integration `cmn8ycvtn02djqx0ytuisn9mw` | Order 23F terminal | live Postiz uniquely binds the enabled Instagram integration; direct `DcTFx_UjSio` binds native owner `anicca.ios.jp`, exact Japanese caption, and visually matching woman → Anicca Card → My Path content. Corrected classification `5728925f…7c5c6c` binds only `anicca-ios` / JA / Instagram / `reelclaw-card` / `nudge-card-reel` / existing pack `694e3ab6…7d480`, and its caption is byte-identical to `bdef736e…601d9` including final LF; superseded `48fc8782…e2e34` is retained. The other four recent rows are classified only as direct Reels with Japanese Anicca/self-care captions, not visually asserted as Card. Target 1/day at 19:10 JST, actual 0/day/default-off/armed 0, fence closed. Current native transcode/contact sheet `ae07770e…b5640` / `cd167041…a38fc` are classification-only because strict SSIM min is `0.921209`; no terminal comparator claim is made. Existing stronger manifest state is preserved byte-for-byte; provider/Telegram/scheduler/legacy writes 0 | classify Larry EN or several accounts in parallel, infer product from a handle, or treat visual classification as terminal native verification |
| 23H | **done — MKT-09R8-11:** revalidate and re-adopt exactly one existing JA Card pack | Order 23G terminal | full-timeline audit rejects source pack `694e3ab6…7d480` as runnable because none of its metadata hooks match baked media hooks; media 1/2 are wrong-form generic quote sequences, media 3 is a held valid Card alternative, and media 4 `35a15c7ce990…e9a15` is selected with complete Japanese Nudge Card/My Path. Exact LF-terminated caption `311f9c3d…6ba2eb`, account-bound pack `76937db0…fe311c`, visual `6ddf6284…9149dc`, and four-candidate evidence `e0a7b1ab…eeb06f` are SHA-exact/mode `0600`. Historical `DcTFx_UjSio` uses this media but the wrong `強い人の口癖…` caption, so it is not an exact-content success. Manifest `bbc2bb24…ca124` truthfully moves only this lane to pack-ready; actual 0/day/default-off/armed 0 and fence closed. The pack operation itself changes no jobs/receipts at `771/250`; a later unrelated Honne effect-class-none generation moves the global count to `774/251` without provider/message effect. Fresh adversary returned `SHIP` | reuse mismatched metadata/caption, accept media 1/2, call the prior canary exact-content success, duplicate video bytes, or change the shared TikTok/Instagram pack env |
| 23I | **done — MKT-09R8-12:** reconcile exact existing Postiz/native history before any new effect | Order 23H terminal | corrected evidence `96a0d769…61ce47` proves 6,822 segmented IDs exactly equal the broad history with zero duplicates/misses; exact integration has 60 rows and LF-equivalent caption candidates are 0, so native candidates are 0 and status is truthfully `absent`. Caption/pack ledger refs are each 0; the reusable selected-video ref appears in jobs/receipts `125/11`, but the joint integration+caption+pack+video identity is 0. Superseded `79dd1fc4…a0b32b8` is retained after adversarial review caught its manually transcribed non-pack video hash. Global jobs/receipts remain `774/251`; manifest remains pack-ready/default-off/armed 0 and fence closed; provider/Telegram/scheduler/legacy writes are 0 | post again before proving absence, accept caption-only/profile/provider state, or reuse the mismatched `DcTFx_UjSio` receipt |
| 23J | **done — MKT-09R8-13:** finish one dedicated JA Card Instagram canary | Order 23I terminal absent | direct Reel `Dce7_IPlUlr`, Postiz `cmt9d2khz00r1p20yb6qbtvyg`, native owner/caption/content, natural Telegram `34651`, replay 0, immediate metric status `ea1f93eb…9a9e8f3`, automatic 2h/24h/72h/7d discovery, and target-only manifest `canary-verified/default-off` are terminal | post again, treat unrelated valid EN/Widget rows as corruption, retry the reconciled message, or arm cadence before metric registration |
| 24A | **done-unavailable — MKT-09R9-01:** resolve Honne EN Instagram route | Order 23J terminal | API disposition `2750a67c…69ac8e` proves the 30-row registry has Honne TikTok EN/JA but Honne Instagram/YouTube 0. Owner requires API-only/no login/no signup; missing routes are unavailable, not blockers. No account/Postiz/publication effect | relabel an Anicca account or claim API can publish without an integration |
| 24B | **done — MKT-09R9-02:** classify Instagram `@anicca.affirmation` / `cmp9pedr700ttqh0yj8o57fog` | Order 24A terminal unavailable | evidence `d4b23768…86c65b` binds Anicca iOS/EN/Larry/native-photo-carousel, Postiz alias `anicca.affirmation`, native owner `anicca.ios`, 144 rows, six-slide native `Dbcvm5Mm8gM`, target 1/day; manifest `88975d67…755d33` changes only this row to classified/default-off, armed 0; writes 0 | classify multiple accounts, infer only from handle, or adopt broken legacy `--tt` mapping |
| 24C | **done — MKT-09R9-03:** import and approve one exact `@anicca.affirmation` pack | Order 24B passes | pack `e23cd412…78669e`, caption `bf90a15a…6e64a0`, approval `7740cd09…968845`, six ordered native JPEGs and order hash `4daa5db7…9837f9` are SHA-exact/mode0600; manifest `b48c8e11…5ce18f` pack-ready/default-off, armed0/fenceclosed; effects0 | import several packs or mix Card/Widget content |
| 24D | **done — MKT-09R9-04:** TDD-generalize the native carousel runner for exactly the frozen EN lane | Order 24C passes | existing JA command remains green; EN account/integration/pack/ordered media/caption/approval/native owner are immutable; alternate refs fail before provider; direct `/p/`, native verification, Telegram and replay contracts are reused; focused 21/21, syntax and diff checks pass; provider/Telegram writes 0 | copy runner, accept caller-defined lane, or execute hardcoded JA adapter against EN |
| 24E | **done — MKT-09R9-05:** finish one `@anicca.affirmation` API canary and metric registration | Order 24D passes | direct `DcfQ2-hG3KR`, Postiz `cmt9jm8990291p20y0a2l1xmk`, owner/caption/GraphSidecar/six exact ordered images and visual content verified; Telegram `34799`; replay publication 0/message 0; immediate post metrics measured; fence closed/default-off/armed 0. Existing 30-minute LM owner discovers the immutable effect and owns exact 2h/24h/72h/7d due times; registered status `c08bf9e8…6a97d3`; focused 6/6 | fan out, manually leave controls open, call pending windows measured, or arm cadence before metrics |
| 24F | **done — MKT-09R9-N:** close the selected Honne/Anicca iOS Postiz canary portfolio one account at a time | Order 24E terminal | `@anicca_slideshow` is the terminal retained canary: exact Postiz `PUBLISHED` row plus ordered LM assets/caption, natural Telegram `34998`, publication/message replay 0, and automatic Postiz-only 2h/24h/72h/7d/daily ownership. Existing LM metrics label resolves fixed release `7eb86d63b…f29a5f1`, interval `1800`, kickstart exit `0`. Final manifest `b7c82a116e99359a3b59131608bb1d6f3f9afd48b37f252524b0463165648122` records slideshow `canary-verified`, target 3/day, default-off, armed 0; every selected lane has an approved pack and terminal canary state, all 13 lanes remain armed 0, 17 non-selected rows remain hold/skip at 0/day, and fence is closed. The 2h observation remains asynchronously due and does not block the next cadence item. Dirty canonical user edits and every legacy label remain untouched | wait idly for a metric window, classify non-selected accounts, retry either effect, mass-enable, or claim armed 0 is daily posting |
| 25 | **historical in-progress snapshot — superseded by MKT-10R0–R7:** arm only one healthy verified account at a time, ramping its first, second, then third daily slot | exact account has correct-content publication, Telegram, replay, and usable metric-source evidence | The former eight-route/3-day arm and manifest `6ea71d6a…e1c3b2` are incident evidence only and must not be restored. The then-current manifest was 13 retained lanes default-off/armed 0 with 17 holds; the then-current first gate was reopened R3, and `@anicca.jp` was corrected to Larry sunset native photos. Historical EN Widget/Postiz/Telegram/metric receipts remain immutable evidence, not present liveness | jump directly to three/day everywhere, reuse a visibly broken candidate, restore the old manifest, or call historical receipts current liveness |
| 26 | **MKT-11:** collect 2h/24h/72h/7d social metrics and join ASC, RevenueCat, and product analytics | exact published effect has immutable creative/campaign lineage | views, likes, comments, shares, saves, watch/retention fields when supported, installs, activation, trials, paid, proceeds, and attribution coverage are source-labelled; unavailable stays unavailable | substitute account aggregates for post outcomes or infer installs from timing |
| 27 | **MKT-12:** close bounded hook learning independently for Honne EN, Honne JA, and Anicca | one product/account has a usable attributed cohort | stable assignment; one hook token per baseline/challenger; immutable outcome; keep/revert CAS decision; next generation proves it consumed the decision | LRU rotation, shared campaign token, cross-product winner, or multi-variable edit |
| 28 | **MKT-13:** finish daily/weekly natural Telegram reporting, then retire legacy ownership | every retained route has Orders 16–27 evidence | reports cover every retained lane and funnel source with replay dedupe; LM is sole owner; legacy disabled state stays archived rollback evidence | raw integration/profile receipts, hidden unavailable data, or deleting legacy evidence |

The product-growth sequence after incident recovery remains: (1) close and prove
marketing, (2) use App Store Connect, RevenueCat, Mixpanel/PostHog, reviews, and
retention evidence to iterate the existing apps, then (3) generalize build,
submission, rejection-repair, and release into the mobile-app development loop.
`$10k MRR` is the first outcome target, not a guaranteed system property; higher
portfolio targets require measured unit economics, platform-policy compliance,
and independent product demand.

| Now | Work | Why it is still missing | Done evidence |
|---:|---|---|---|
| 1 | Repair and freeze the machine-readable scheduler inventory | the OpenClaw store and live scheduler disagree, and launchd is the real owner of many loops | every stored and loaded job has one `migrate`, `replace`, or `retire` decision and an owner; measured: all 399 captured rows and all 269 enabled-or-loaded rows now have exactly one disposition and a non-null owner |
| 2 | Decide every legacy job and freeze new legacy writes | inventory without disposition cannot drive a safe cutover | each job has a Life Manager owner, target adapter, effect class, verification command, and rollback action; measured: 214 migrate, 40 replace, 132 retire, and 13 retain-external rows are recorded with rollback actions, and verification commands are set only for the five adapters that already exist |
| 3 | Build the portable local runtime foundation | current loops lack one shared Life Manager data root, secret provider, durable generic job protocol, and local direct-process bundle. A legacy-path dependency scan now exists and passes (`apps/life-manager/scripts/scan-legacy-paths.js` + `scan-legacy-paths.test.js`, wired into `npm test` as `test:legacy-paths`): it walks the monorepo runtime (`apps/life-manager`, `runtime/`) plus every skill the runtime actually loads or spawns — `skills/video/daily-lm-video`, `skills/video/lm-distribution`, `skills/tools/telegram-user`, `skills/life-manager`, and `skills/earn/marketing-engine` (whose `run_agent.sh` is spawned by `life-manager-daily.sh` and `life-manager-dev-d0.sh`) — and fails on any non-allowlisted `.openclaw`/`profitable-claude`/`life-manager-v0` reference or legacy anicca code-root reference (`$HOME`-, `${HOME}`-, or `~`-rooted anicca checkout and the anicca-oss checkout). The allowlist holds only (a) line-pinned denial regexes and copy-only migration tooling and (b) an explicitly tracked, line-and-content-pinned set of five pre-Order-12 holes: the x402-sell/taskmarket/payout earn-loop boot defaults that still point at the legacy anicca code roots, each named with its owning Order (Order 12); `verifyAllowlist` fails the scan the moment any pinned line moves, changes, or disappears. The runtime's own legacy-path dependencies in that scope were removed: `daily-dev-loop.js` defaults its state dir to `<data root>/state/life-manager-dev` via `resolveDataRoot` in `runtime-paths.js` (`LM_DATA_DIR`, falling back to `~/.local/state/life-manager`), the four launchd boot scripts (payout, x402 ledger, taskmarket ledger, ugig observer) load `LIFE_MANAGER_ENV_FILE` (default `~/.local/state/life-manager/.env`) through a shared guarded loader that warns-but-boots when the file is absent and refuses (exit 1) any env file beneath a legacy runtime root, the taskmarket/ugig installers and the dev/taskmarket/ugig launchd templates write logs beneath `~/.local/state/life-manager/logs`, and the daily video generator's argless defaults resolve to the same `<data root>/state/lm-video` paths `life-manager-daily.sh` exports. Existing on-disk legacy state (lm-video recordings/render state, dev-loop `done.jsonl` dedup history) is migrated copy-based via `apps/life-manager/scripts/migrate-legacy-state.sh` (idempotent copy with size readback, never move/delete — the legacy loop stays owner until cutover); until that copy has run, `generate.py` and the dev loop fail loudly naming the migration script instead of silently starting empty or silently reading the legacy path. Still OPEN in this row (not claimed): the shared secret vault/provider (Order 6), local append-only ledger/atomic lease implementation, and the legacy-env-inaccessibility proof (cutover gate 6: denied `~/.openclaw` access without interruption); no Order is marked done by this slice | one direct local entrypoint starts the scheduler and workers from the Life Manager data root while all legacy roots are denied |
| 4 | Finish Telegram command migration and shadow the current financial report | the bounded report adapter is complete, but the rest of bot command routing and seven-run cutover evidence remain | **report slice proven:** local Life Manager sent real `message_id=432`, stored matching snapshot/effect receipt, and read no OpenClaw env. **Command routing slice done:** the LM webhook's slash-command surface now covers the legacy telegram_bot.py parity list (/help /status /where /stop /subscribe /connect /payout /reset, plus the already-routed /start and /panel and the edited_message live-location stream) via a generic router (`apps/life-manager/lib/slash-command.js` + `lib/late-notice.js#deleteLiveLocation`) with unit and fake-transport HTTP contract tests (`lib/slash-command.test.js`, `test/telegram-slash-http-contract.test.js`, both wired into `npm test`) proving unknown-/command honesty, branch ordering against the payout intake / feedback / panel / browser-task branches, tenant-scoped /stop deletion, idempotent /payout reopen, and the /connect alias into the existing parsed-control flow. **Adversary follow-ups closed:** (a) /reset now discloses that rewinding `tg_onboard_stage` also pauses browser-task intake (`lib/browser-task-intake.js` accepts only the `"done"` stage) and the coupling is named in code; (b) only an exact `/start` (optionally `@BotName`, optionally followed by a deep-link payload) is a start — `"/startfoo"` is answered as an unknown command, safe because Telegram deep links always deliver the payload space-separated (`core.telegram.org/bots/features` → Deep Linking: `?start=airplane` → `/start airplane`, `?startgroup=spaceship` → `/start@your_bot spaceship`); (c) `/connect <non-calendar>` (e.g. `/connect gmail`) is answered honestly instead of silently connecting Calendar; (d) `/status` distinguishes `active` / `complimentary until <expiry>` / `not active` so a comped row (`lib/comp-window.js`, which already carries it past the paywall) is no longer told its subscription is inactive; the seven-run shadow stays open |
| 5 | Import shared execution contracts needed by retained loops | the shared adapter registry, content-addressed object import, tenant profile boundary, and financial/first marketing adapters are complete; most marketing and income loops still execute through legacy paths | Life Manager owns the remaining minimum runner, schemas, artifacts, publications, receipts, and verification adapters needed to preserve behavior |
| 6 | Migrate Larry/ReelClaw, Capafy, clipping, writer, gig, bounty, and all retained loops | `ai.anicca.life-manager-daily` now has real portable generation and TikTok distribution receipts, a fixed visible-hook render, an idempotent generation→Instagram/TikTok durable-job chain, a read-only verified Life Manager-owned IG profile, and a generic due-window observation pipeline. Honne JA now also has a generic Life Manager-owned 24-hook/four-media producer with a completed durable `HJA-007` shadow receipt and idempotent replay. A generic (product-agnostic) marketing video publication adapter now exists at `apps/life-manager/lib/marketing-video-publication-adapter.js`, binding product/format/form/locale/slot/creative to one platform-scoped publish effect and passing its contract tests (job build, Instagram+TikTok+Anicca-YouTube planning, tenant-scoped provider execution with lineage/URL, direct YouTube URL verification, and cross-product/mismatched-provider rejection); its job identity now agrees with its effect identity (job_id is derived from tenant + effect_key, slot is lineage only), so a replay of the same bytes+caption+platform at a new slot can never violate the database's `UNIQUE (tenant_id, effect_key)` rule or double-post. Its reconcile path is now proven by passing tests: `distribute.py` writes format/form/locale/slot lineage into every ledger row, a ledger-recovered receipt passes the adapter's own verification before "present" is returned, `provider_reconciled` is propagated from the ledger row (never fabricated), the absent path returns the reconciler-required receipt shape (`lookup: "ledger_no_published_row"`), and legacy shadow rows without lineage resolve to "unknown" without crashing the reader; the distribution subprocess also runs on an allowlisted environment instead of the full parent env. Reconcile provenance is now propagated on all paths (the ledger short-circuit reports the existing row's `provider_reconciled` instead of fabricating true, and the subprocess allowlist passes the real chain's `INSTAGRAPI_PYTHON`/`CDP_HOST`/`CDP_PORT` through), unknown reconciliation now ages (a durable per-attempt counter dead-letters the job with `RECONCILE_UNKNOWN_EXHAUSTED` after 5 consecutive unknown reconcile results, reset on resolution, per `migrations/20260730_runtime_reconcile_unknown_aging.sql`), and the chain's two-platform fanout is sequential fail-fast (a first-platform enqueue collision stops the remaining platform's enqueue in the same scan), all proven by passing python/node/postgres tests. It is not yet wired into any scheduler or the loop-adapter registry manifest. A generic `marketing-video-publication-chain.js` now chains one generic video generation receipt into exactly two independent durable Instagram/TikTok publication jobs product-generically, with an end-to-end fake-store proof (enqueue→claim→execute→complete, then full replay with 0 new jobs, 0 claimable jobs, 0 additional provider executions, under the enforced `(tenant_id, effect_key)` unique rule) and cross-product/hash-mismatch/different-slot rejection all proven by passing tests; it is not yet wired into any scheduler. Honne JA generic video scheduling is now wired into the Life Manager scheduler in shadow mode behind `LM_HONNE_JA_SHADOW_ENABLED` (default `false`, enabled nowhere), with slots encoding exactly the legacy 12:30/21:30 Asia/Tokyo launchd cadence (`lib/honne-ja-shadow-schedule.js`); one real manual shadow cycle (`scripts/honne-ja-shadow-cycle.js`) against the running local durable store completed generation receipt hook `HJA-008` (job `marketing-video-generation:0f19ddbb…`, slot `2026-07-30T03:30:00.000Z`, `video_sha256` equal to the legacy source bytes) through the same worker path as the HJA-007 proof, and enqueued both Instagram/TikTok publication jobs durably in a held state (`queued` + durable `shadow_held` hold row, zero provider calls, idempotent replay with no new rows); a seven-cycle status reader (`scripts/honne-ja-shadow-status.js`) counts, per §13 semantics, only the trailing run of consecutive EXPECTED 12:30/21:30 JST slots each holding exactly one verified receipt — an expected slot that passed with no receipt row (scheduler off/stopped) breaks and resets the count and is reported in `missed_slots`, and a duplicate receipt for one slot is a gate violation that also resets — so scattered receipts can never reach `gate_met`; it reports n/7 toward the §13 seven-expected-run gate. Product attribution, bounded learning, the remaining broken Larry/ReelClaw slices, and all other loops remain open; all new fanout stays disabled during shadowing. A 2026-07-30 local compose override once produced a 1/7 status, but on 2026-08-20 no Life Manager marketing scheduler/worker process or container is running, so the shadow gate is not accruing and no current cutover ownership is claimed | every retained effect executes from a Life Manager job and produces a machine-verifiable receipt |
| 7 | Switch scheduler ownership and prove OpenClaw-free local | launchd and OpenClaw can still become competing writers | seven expected local cycles pass with the gateway stopped and all legacy roots inaccessible, without missed or duplicate effects |
| 8 | Package the supported local option | a working checkout is not yet a reproducible self-hosted product | clean-machine install, upgrade, backup/restore, health check, and uninstall verification pass |
| 9 | Deploy the same release to cloud | current Railway service does not yet own every retained loop or worker class | API, scheduler, and worker pools run the same contracts and release hashes as local |
| 10 | Add cloud tenant isolation and monthly subscription | hosted operation needs durable entitlement and fair resource isolation | Stripe webhook entitlement, tenant isolation, quotas, cancellation/export, and noisy-neighbor tests pass |
| 11 | Shadow, cut over, and prove Mac-independent cloud | cloud cannot become scheduler owner without parity evidence | reconciled shadow, Dais canary, then seven expected cycles with the Mac Mini powered off and no duplicate effects |
| 12 | Resume product feature work | finance expansion and marketing self-improvement are intentionally frozen during migration | Order 26 is complete; Orders 27–38 become active in sequence |

## 13. Cutover gates

Normal cutover never disables a running legacy job until its replacement
passes. Incident quarantine is the explicit exception: already-disabled jobs
stay disabled and preserved as rollback state while controlled recovery proves
the replacement; they are not mass-enabled merely to satisfy this gate.

1. Seven consecutive expected runs have complete receipts.
2. At least one real publication per selected account is reconciled to a
   platform URL.
3. Financial panel and Telegram share a snapshot hash.
4. Local worker restart does not create duplicate effects.
5. OpenClaw dependency scan is empty for migrated runtime paths.
6. Forced OpenClaw shutdown plus denied access to `~/.openclaw` does not
   interrupt local Life Manager.
7. Rollback restores the prior scheduler state from a signed inventory.

Cloud becomes Dais's scheduler owner only after:

1. The same release passes local and cloud contract tests.
2. Local and cloud shadow outputs reconcile.
3. One scheduler-owner lease prevents double execution during transfer.
4. Real posting and Telegram delivery succeed from cloud.
5. A powered-off Mac does not interrupt seven expected cloud cycles.

## 14. Security and commercial constraints

| Constraint | Requirement |
|---|---|
| Tenant isolation | every row and job is tenant-scoped; cross-tenant joins fail closed |
| Credentials | encrypted at rest; references only in jobs; redacted in logs and reports |
| Local secrets | OS keychain or encrypted Life Manager vault; never `.env` files inherited from OpenClaw |
| Cloud secrets | tenant-scoped vault with audited access and rotation |
| Bank access | Local uses the owner's authenticated Codex + Moneytree plugin; cloud uses tenant-scoped Moneytree LINK/OAuth; neither accepts raw MUFG credentials |
| Apple access | App Store Connect API keys/delegated roles; no stored Apple ID password |
| Financial action | read-only in this spec |
| Publishing | explicit connector authorization and auditable publication receipt |
| Data export/delete | user-controlled export and deletion without deleting financial audit records required by law |
| Claims | no guaranteed income or “automatic $10k MRR” promise |

## 15. Success metrics

| Layer | Metric |
|---|---|
| Independence | retained loops with zero OpenClaw/legacy-path references; unowned scheduler entries |
| Portability | contract suite passing unchanged in local and cloud modes; tenant export/import reconciliation |
| Reliability | expected jobs with valid receipts; duplicate effects; stale sources |
| Variety | unique hook fingerprints; format concentration; duplicate rejection rate |
| Acquisition | per-product/locale/platform/account views, clicks, attributed installs per 1,000 impressions, and attribution coverage |
| Monetization | install→trial, trial→paid, paid retention, MRR, proceeds |
| Reporting | natural-language Telegram receipt/digest/review coverage; snapshot-hash parity; duplicate messages |
| Learning | scorable observations, kept challengers, reverted regressions, consumed weight receipts |
| Finance | reconciled net worth coverage, classified income coverage, source freshness |
| Product | users whose measured monthly benefit/revenue exceeds subscription cost |

## 16. Spec self-review

| Check | Result |
|---|---|
| Placeholder scan | No unresolved implementation placeholders |
| Internal consistency | The incident path is one direct local process with JSONL/atomic-file ledgers; a hosted adapter is deferred and must not alter the local contract |
| Scope | Program is decomposed into OpenClaw independence and local recovery first; hosted deployment, financial/growth closure, and later health/development loops remain deferred |
| Ambiguity | Local is the required full deployment now; hosted mode is optional future work; Docker, Colima, Railway, PostgreSQL, and OpenClaw are not local prerequisites or fallbacks |
| Evidence honesty | Current ASC snapshots are marked inconsistent; unavailable product metrics never become zero |
