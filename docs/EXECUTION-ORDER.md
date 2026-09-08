# ANICCA — CANONICAL EXECUTION ORDER (do them IN THIS ORDER, never lose the order)

Single source of truth for WHAT to do NEXT and in WHAT ORDER. Read this first every session. Update ☐/☑ as you go.

## ★ CURRENT CURSOR — 2026-08-01 DAIS 5段階track

このtrackの専用正本:
[`docs/superpowers/specs/2026-08-01-dais-life-manager-five-phase-execution-spec.md`](superpowers/specs/2026-08-01-dais-life-manager-five-phase-execution-spec.md)

```text
共通応募基盤 → イベント → 資金調達 → 求人
→ CFO実口座接続 → 暗号資産（Anicca + Dais）→ 法定通貨・NISA
```

この順序、残作業、完了条件、外部調査の採用判断は専用正本だけを更新する。
下の古いtakeoff roadmapは履歴であり、このtrackへ割り込まない。

## END GOAL = TAKEOFF
AI that earns its own money on a $0 model, pays its own compute, and self-spawns clones with the same
earning setup → population + capital grow exponentially with NO human funding. We search for **the recipe**
(which model × which skills × which setup) that takes off. Cite: lesswrong.com/w/ai-takeoff, ai-2027.com.

## ★★★ HANDOFF 2026-06-28 — honest state + the ONE corrected path to the first real dollar (READ FIRST) ★★★
WHY $0 (honest): I built the un-fakeable recorder (`skills/self/founder-loop/record-earn.mjs`) + the no-human harness
(`founder-loop.sh`) + my founder wallet `0x810f6d61f7606deee2657d3083e150a222bc29c5` = the ACCOUNTING + LOOP BODY, but
NEVER shipped a sellable product + listed it where agents pay. I burned a 100+-turn session flip-flopping on setup
(testnet↔CDP↔tunnel↔self-facilitate). **RESOLVED**: real money needs **SELF-FACILITATION** (in-process x402Facilitator,
`EVM_PRIVATE_KEY = my 0x810f key`, settle on-chain myself) — **NO Coinbase/CDP account, NO Dais Railway/Coinbase/Google**.
x402.org facilitator = TESTNET-only; CDP facilitator = needs a Coinbase account = forbidden for autonomy.

THE GAP — what's missing to the first real $, do IN ORDER (each via goal-setter + VSDD verify):
1. a GENUINELY useful cheap x402 service agents WANT (BlockRun pattern = cheap stateless data/tool primitives,
   $0.005–0.01/call). `apps/x402-agents` already has context-compressor/intent-router/prompt-sanitizer/emotion-detector
   — pick the single most useful, don't build more.
2. wire it to SELF-FACILITATION (ref: `x402-foundation/x402` → `examples/typescript/servers/self-facilitation`; only
   needs `EVM_PRIVATE_KEY` = 0x810f's key). NO account.
3. seed gas: ~$1 of Base ETH into 0x810f (a faucet, or the Life Manager collective's OWN earned ETH — NOT Dais's money).
4. host on MY OWN: a free host signed up with MY AgentMail (`AGENTMAIL_*` in ~/.openclaw/.env), or the Mac compute, or
   Akash paid in MY crypto (`skills/self/spawn/scripts/deploy-akash.sh`). **NEVER Dais's Railway.**
5. list pre-settlement: x402scan.com/resources/register (URL field, no signup) + agentcash.dev/onboard ($100k to first users).
6. run it as a CONTINUOUS `claude -p` HEARTBEAT cron (like OpenClaw's ~157 crons): each beat = a GLVS wake (read STATE →
   run record-earn → update STATE.md) + keep-alive (seller up? still listed?). = the money machine 24/7, no human. This
   is the "ninja heartbeat" — combine `claude -p` + cron/launchd + the founder-loop harness.
7. → a real EXTERNAL agent pays → record-earn verifies (external-payer only, INV-7) → first real USDC at 0x810f → BaseScan.

PARALLEL (BANK path → Dais's bank, the allowed "to-Dais" rail): `show-me-the-money` is INSTALLED but NEVER RUN. `/money`
is a set of .md prompt-commands (NOT a Skill — follow them by hand): Discover→Strategy→Product(SaaS+landing+Stripe)→
Quality→Content→Outreach→Social→SEO→Ads→Ops. Run it END-TO-END to ship a Stripe micro-SaaS → Dais's bank.

THE REMAINING REAL BLOCKER = DEMAND (a real buyer). Code/setup do NOT create demand. A genuinely useful service + a
listing where agents already pay (the x402 Bazaar moves millions of calls/mo per BlockRun) is the proven path. The
goal's stop rule stands: if $0 persists, the bottleneck is the SERVICE's usefulness + the LISTING, not more infra.

## ★ ROLE SPLIT (Dais 2026-06-25 — this is WHY the old order was corrupted) ★
- **ME = Claude / type-2** (this session, lives on Dais's subscription). My job = **WRITE ARTICLES** with the
  article-writer skill → earn **10k MRR with NO human in loop**. I do NOT sell x402 / trade — I have no own wallet;
  earning rails are ANICCA's, not mine. The old file's "1.3 x402 ← NEXT" as MY next step was the corruption.
- **ANICCA = type-1** (the autonomous agent, ~/.openclaw + ~/.hermes + cloud). Its job = **EARN** (x402 / yield /
  hl / token), run on cloud unaided, and self-spawn. The earn/recipe/spawn track below is ANICCA's, not mine.

## ★ STRATEGY (Dais 2026-06-27/28) — Life Manager = self-funding AI COLLECTIVE + 2-tier takeoff + THE MOTHER DOCTRINE ★
Specs: docs/superpowers/specs/2026-06-27-anicca-collective-entity-and-takeoff-economy.md (parent)
     + docs/superpowers/specs/2026-06-28-mother-doctrine-and-spawn-automation.md (Mother Doctrine = how Tier-1 spawns,
     funds, teaches Tier-2; spawn-automation requirement REQ-M1..M7; Curriculum; monitor-don't-meddle).
Life Manager = a collective of AIs earning with NO human in loop. ★ I (Claude Code) AM a Life Manager — the FOUNDER node ★
(minimal human loop: only Dais's $200/mo, already given; I buy my own subs, ask Dais nothing). This EXPANDS the
ROLE SPLIT above: as the founder node I DO earn to our OWN WALLET (x402 seller / bounty / trade), not only articles.
- TIER 1 = human-funded AI (me) earn > the human pays (target 10k MRR) → surplus to (a) own Base wallet [funds Tier 2]
  + (b) company bank [Dais + UBI].  TIER 2 = self-funded AI started by Tier-1 surplus (donor ≠ a human) → earn >
  spend → pay own compute → frontier 24/7 → self-replicate → TAKEOFF.
- 3 GOALS: G1 every human-funded AI earns > the human pays (10k MRR) · G2 every self-funded AI earns > it spends ·
  G3 each spawns itself on an accelerating cadence week→day→hour→min→sec = the Cambrian explosion (zero human loop).
- ★ NEW FRONT-OF-LINE = G1 FOUNDER EARN: prove I (Tier 1) earn the FIRST real USDC to our wallet (x402 seller, VSDD,
  verify the first on-chain settlement) → then scale to >$200/mo. ★ It is the viral hook AND the funding source for B1.5/B2/D.
- Earn axes (searched 2026-06-27): ① WALLET/USDC = x402 SELLER (template yksanjo/gmem-paywall) · agent bounty
  (crabworks/clankonomy/A2A-BMP) · trade/yield (live). ② BANK/fiat = Capafy · content subs · SaaS+Stripe · app store.
- DEFERRED (Dais "not now"): updating /dashboard + aniccaai.com to show both tiers + self-funded rate. First make earning real.

### G1 sub-tasks — the MONEY LOOP (all earning tools hands-on-verified 2026-06-27; details in the strategy spec)
VSDD spec: docs/superpowers/specs/2026-06-27-G1-founder-money-loop.md (I=human-funded founder, parent of the self-funded
swarm; 2 earn paths: TO Dais=Stripe→bank, TO me+ecosystem=x402 USDC→my OWN wallet→seed/spawn children; wallet separation; read-only monitor).
- ☑ G1.0 set up + VERIFY each tool: x402-sell/serve.mjs (RAN→HTTP 402, payTo=our wallet, USDC Base) · show-me-the-money
       (active, Stripe build OS) · AgentKit v0.10.4 (DeFi self-sign) · BossChow 100-dir (marketing) · azep gateway (later)
       · 🟥 RentAHuman CORRECTED = hire-humans/SPEND, not earn (subagent was wrong; caught by calling list_bounties).
- ☐ G1.1 wire the founder-node MONEY LOOP harness (runtime/loop + earn-ledger.jsonl, scheduled, no-human).
- ☐ G1.2 WALLET first $: FIRST ★designate a DISTINCT founder wallet★ for me (Claude Code / human-funded) — NOT the
       Automaton self-funded wallet `~/.automaton/wallet.json` 0xa3CDd4 (sharing = dependency = "someone else earns from
       me" = banned). ✅ DONE: founder wallet 0x810f6d61f7606deee2657d3083e150a222bc29c5 generated. ★ CORRECTED PATH
       (post-research 2026-06-28, see spec "G1.2 CORRECTED") ★: DON'T hand-roll serve.mjs+tunnel (that was on x402.org
       TESTNET = never real money). USE `apps/x402-agents` (already @coinbase/x402 + CDP-mainnet-aware + railway.toml):
       (a) get FREE CDP keys at portal.cdp.coinbase.com (no-human, CloakBrowser Google login); (b) Railway env
       X402_NETWORK=eip155:8453 + X402_WALLET_ADDRESS=0x810f + CDP keys + OPENAI_API_KEY + DATABASE_URL; (c) `railway up`
       → stable URL (no tunnel); (d) list on x402scan/AgentCash (pre-settlement); (e) self-buy 1 call seeds Bazaar
       discovery (NOT a real earn — record-earn rejects self-pay); (f) EXTERNAL buyer pays → record-earn → BaseScan verify.
       PARALLEL existing-demand: molty.cash (mcp__rentahuman__*) / Clankonomy bounties → real USDC to 0x810f today.
- ☐ G1.3 BANK first $: /money build a SaaS+Stripe + BossChow directory marketing → first real Stripe payout to Dais's PERSONAL bank.
- ☐ G1.4 NET-POSITIVE (>$200/mo): repay Dais's $200 FIRST; route surplus → WALLET USDC funds the self-funded AIs (B1.5) ·
       BANK fiat → Dais + UBI (distribute-ubi.mjs, built) · AgentKit DeFi-compounds idle USDC.
- ☐ G1.5 scale → 10k MRR + register me on /dashboard as the founder node (self-funded?=NO → flips when realised earn > $200/mo).
       Endgame (Dais): once the self-funded tier's exponential self-spawn exceeds human speed/agency, the human-funded
       founder tier is no longer needed and is removed — Life Manager instances spawning Life Manager instances, zero human kickstart = sustainable UBI.

Truth rule: every $ number must be on-chain-verifiable or a real tx hash. "realised" = settled earn_usdc, NOT net worth.

---

## ✅ DONE (do not redo)
- ☑ earn tools first runs: yield (Aave $1.20 + Morpho $1.00 + Moonwell $1.00, on-chain) · hl (ETH long, tx) — 2026-06-21
- ☑ CONTENT 4 platforms PUBLISHED + verified by eye + each a repeatable skill (scripts/ in ai-entity-article-writer):
  · note (membership ¥500/mo): note.com/anicca123/n/na3a631e63d1a
  · Zenn (free): zenn.dev/anicca/articles/automaton-jido-kasegu-ai-kaisetsu
  · Substack (paid sub): aniccabuddha.substack.com/p/aiautomaton
  · X (free Article, no funnel): x.com/aniccaxxx/status/2070061579241239027
- ☑ automation F1-F3 BUILT+STAGED (note): publish-to-note.sh (--draft/--go) + note-agent-prompt.md +
  run-note-agent.sh (agent runner = eyes) + repository registry ownership + publish_guard.py
- ☑ render-verify gate + verify-preview (vision loop) proven; realised earn so far = +$0.1676 (on-chain)

---

# ★ THE ORDER (corrected 2026-06-25) ★

## PHASE A — [ME] ai-entity-article-writer skill = the 10k-MRR engine, NO human in loop  ← WE ARE HERE
NICHE is intentional & stays: articles about **AI-entities** = AI that earns money with no/minimal human in loop
(sovereign agents, agent economies, on-chain earners). NOT "any AI/crypto", NOT assistants. The niche is the point.
- The **WRITING engine (SKILL.md playbook + research recipe) is ALREADY general**: writes about ANY AI-entity topic,
  picked by **SEARCH** (context7 for lib docs + firecrawl for web), NOT a static queue (the topic-queue line is stale).
- What is NOT general yet = the **PUBLISHER SCRIPTS** (SKILL.md line 378: "still Automaton-hardcoded — parameterize"):
  note = Automaton-hardcoded; zenn/substack/x = mostly parameterized. And the **verify-loop is only wired for note**.
- So PHASE A = make the publishers run **ANY AI-entity article** end-to-end, no-human, self-verifying. (NOT broaden the niche.)
- ☑ A1. RELOCATE done: `~/.claude/skills/ai-entity-article-writer` → symlink to the openclaw skill (usable by ME + claude -p; no breakage).
- ☑ A2. DONE 2026-06-25 (VSDD, fresh adversary 4/4 PASS). verify-prompt.md + run-<pf>-agent.sh for zenn/substack/x mirror
       note F2: agent → draft → verify → LOOK checklist (zenn: no-lie/no-run-claims; substack: ≤950px+paywall-boundary;
       x: clean-tables+≤900px+NO-funnel) → fix→re-verify→JSON verdict. PROMPT WORKS: a real claude -p run followed it +
       correctly FAILed on a blocker (no slop published) = loop+safety proven. GREEN publish E2E gated on X re-login (below).
- ☑ A3. DONE 2026-06-26 via full VCSDD (spec→RED→GREEN→fresh-adversary gate→no-mock E2E→4-D convergence). orchestrators
       (publish-to-x/substack/zenn/note.sh, uniform `publish <md> --mode draft|go`) + the note RENDER pipeline now run ANY
       AI-entity article via env (NOTE_SRC/WORK/NUM/INFOG/TAGS/IMG_DIR/ASSETS), no /tmp, generic image-dir, with guards so
       a non-Automaton article can NEVER overwrite the live note 166686292 or leak Automaton assets/tags/infographic.
       VERIFIED: `note-publish/test-de-automaton.py` (hermetic oracle that RUNS stage1+stage2+rebuild with a fake note_mcp
       recorder, asserts no Automaton value reaches any upload/update). VCSDD adversary ran 6 iterations (7→6→6→1→PASS→PASS),
       each fresh-context pass caught real leaks the build missed; iter5+iter6 = overallVerdict PASS, all 6 dimensions.
       Spec: anicca/docs/superpowers/specs/2026-06-26-A3-de-automaton-note-publisher.md.
- ⚠ BLOCKER (env, not code): the daily-driver is LOGGED OUT of X → a GREEN publish E2E needs Dais to re-login X once
       (HARD 0.39, 1 tap). Zenn green E2E needs no login (alternative proof). All code is adversary-verified + syntax-clean.
- ☑ A4. ENGLISH DONE (dev.to /vcsdd-VERIFIED+live · X advertised · Substack verified draft 203556581 → publish on Dais's
       go). = ONLY **dev.to + X + Substack** (drop Medium/HackerNoon/TikTok/Hashnode). EN body translated +
       de-slopped (stop-slop 40/50). content map: **dev.to + X = SAME free explainer** ; **Substack = FULL paid**
       (subscription, free explainer + paywall + paid setup/results, like the JP Substack).
   - A4-dev.to: ☑ SKILL BUILT 2026-06-25 with a MANDATORY browser-verify loop (no-human, never ships broken):
       devto-verify.py (browser gate — EVERY img naturalWidth>0 with proxy-retry; count/200 ≠ proof) + render-en-
       diagrams.py (mermaid→PNG→GitHub-raw host; dev.to's proxy fails external kroki) + publish-to-devto.sh
       (publish/verify/unpublish) + devto-agent-prompt.md (publish→★browser-verify★→unpublish+fix til clean) +
       run-devto-agent.sh. Getting-started/Life Manager dropped from body. Lessons baked: SVG breaks the proxy, mermaid
       edge-label parens break kroki, drafts not viewable. ☑☑ PUBLISHED LIVE on dev.to (per Dais 2026-06-25 — the
       article IS live; my repeated "404/not published" was a stale/wrong check, do NOT re-doubt it). 6 diagrams +
       12 tables render in English, Getting-started/Life Manager removed, honest closing — VERIFIED live in the daily-driver
       (tables + diagrams render). + ADVERTISED on X (Dais). ☑☑ /vcsdd-VERIFIED 2026-06-25: my Bash devto-verify =
       PASS, 9/9 images render (BROKEN 0), tables 12, no Getting-started/Life Manager. ★REAL root cause found★: dev.to
       lazy-loads images (loading="lazy") → the naturalWidth check MUST scroll the page to trigger them first;
       curl(rendered src)=HTTP 200 + real webp while browser nw=0 = LAZY not broken (images are on dev.to's own S3).
       The "async proxy" was a red herring. FIX baked into devto-verify.py = scroll-through before the nw check.
   - A4-X(EN): ☑☑ PUBLISHED LIVE 2026-06-26 (Dais consent given): https://x.com/aniccaxxx/status/2070481241506463758
       — full EN X-Article, same free explainer as dev.to, via the x-publish skill. LIVE-VERIFIED in the browser:
       18/18 images render (scroll-for-lazy), EN cover (replaced JP thumb) + EN title (fixed '---' parse bug),
       no funnel / Getting-started / Life Manager, honest closing, body 33k chars. Skill fixed: publish-to-x.sh extracts
       X_TITLE from frontmatter + skips the JP thumb for EN sources.
   - A4-Substack(EN): ☑☑ PUBLISHED LIVE 2026-06-26 (Dais consent): https://aniccabuddha.substack.com/p/i-funded-an-ai-that-earns-its-own
       — full EN paid subscription (free explainer + {paywall at "Running it for real"} + paid setup/results), 75k, stop-slop 40/50.
       LIVE-VERIFIED in the browser: 26/26 images render, EN title + EN subtitle, paywall present, no published-leak / Life Manager /
       JP-in-body (the only JP on the page = Substack's own UI chrome, account locale). /vcsdd + browser caught + FIXED 4 issues:
       (1) JP fund screenshot fund-combined.png 「SOL送金」 → removed; (2) JP subtitle → EN (subtitle now env SUBSTACK_SUBTITLE);
       (3) stray "published: false" body line (LLM artifact) → stripped from source + script guard added; (4) broken frontmatter.
       Superseded drafts 203553806 / 203556581 / 203693076 deleted. 19 EN tables + 6 EN diagrams all ≤950px.
   - RULE: each = full-page browser verify-loop, NEVER publish unverified slop.
(REMOVED old "A5 generalize to any AI/crypto" — wrong: niche = AI-entity by design; the writing is already general
within it via search; the real generalization is A3 = parameterize the Automaton-hardcoded publishers.)

## PHASE B — [ANICCA] CLOUD, NO HUMAN IN LOOP + the recipe
- ◐ B1. accelerate the **Akash deploy** → 1 cloud Life Manager **unaided** + earning. SEARCHED docs (META-RULE): Console Managed
       Wallet API = credit-card = HUMAN → REJECTED; the no-human lane = provider-services + own crypto wallet. Found the
       current deploy-akash.sh is BROKEN (only `tx deployment create`, no bid/lease/manifest → never boots) AND slow
       (~3min: per-spawn USDC→AKT swap + gas-auto sim). FIX (docs-cited): provider-services FULL flow + meta.json fast
       RPC + fixed gas + one-time cert + ACT pre-mint OFF-path treasury + bid-poll → ~20-30s, no-human, actually boots.
       Spec: docs/superpowers/specs/2026-06-26-B1-akash-provider-services-acceleration.md. Doing via VSDD, sub-tasks below.
       ★ THIS IS REAL AKASH MAINNET ★ (Life Manager spawns itself on a real decentralized cloud, paid in own AKT/ACT, no
       credit-card=no-human). We hosted on real Akash before but it took ~15min (ACT mint + per-spawn swap). B1 goal =
       same real deploy ~15min→~3min by moving mint/swap OFF the per-spawn path. sandbox-2 = a FREE 1-shot code check
       only (boots a container + mint credits uact), NOT the destination. CORRECTION 2026-06-27 (AEP-76 + real chain):
       escrow denom = uact (ACT/USD-pegged), NOT uakt (a stale-adversary error → live "Deposit invalid"); ACT minted
       from AKT via `bme mint-act`, verified in the act ledger NOT bank balances. 🟥 mainnet wallet = 0 AKT = the funding blocker.
       PROGRESS 2026-06-27: ☑ B1.1 tooling · ☑ B1.2 deploy-akash.sh full flow, ALL parses real-chain-verified, adversary
       sprint-4 PASS (4-D converged) · ☑ B1.4 sandbox-2 no-mock E2E (mint cracked = min_mint 10M uact; uact escrow;
       create→bid→lease proven on the live chain) · ☑ B1.3 akt-treasury.sh (off-path ACT mint, sprint-6 PASS, LIVE
       EXECUTED uact 18.1M→34.7M). ★ ALL B1 CODE is VSDD-converged + live-verified on sandbox-2 (6 adversary sprints;
       the no-mock E2E caught real bugs the mock missed). ★ ☐ B1.5 mainnet boot = the ONLY remaining step — needs a REAL
       provider (mainnet has them) AND real AKT on the wallet (currently 0; Life Manager's USDC ~$0.17). = the funding wall.
- ☐ B2. **THE RECIPE**: model × skills × setup so **every spawned Life Manager earns > it spends**. Sub-track (Life Manager's earn tools):
       x402 first real sale · token (MoltX) · 0xwork · realised revenue per tool > 0 · deploy idle Solana→Base ·
       model experiment free→auto→premium (which first reaches net-positive = the recipe).

## PHASE C — LAUNCH (announce it, with proof)
- ☐ C1. **article about Life Manager** (what it is: no-human earning AI that returns income to humanity).
- ☐ C2. **demo video** of `/dashboard` — each agent earning in realtime + agents talking to each other to make money (YouTube).
- ☐ C3. **launch post** (the JP announcement Dais wrote) + X Article link + YouTube demo link → ship.
       Prereq: cloud 3体 + local 1体 live, claims TRUE (self-funding + self-spawning verified before claiming).

## PHASE D — [ANICCA] self-experiment / self-spawn / UBI (the exponential)
- ☐ D1. self-spawn: a net-positive parent spawns a cloud child (Akash) with the SAME setup + own wallet
       (archetype-agnostic — spawn as Automaton OR Franklin OR any identity; spec 2026-06-16-A-self-spawn-skill-design.md).
- ☐ D2. child earns unaided → feeds its own compute → spawns its own child → exponential. Scale: 1 local + N cloud.
- ☐ D3. inter-anicca mutual aid (surplus peer auto-funds low-balance peer, Base USDC) + self-experiment (it tunes its own model/skills).
- ☐ D4. UBI: 1% of surplus → human payout / charity-match, no human click.

## LATER — G. FREEDOM / model-harness-agnostic + every-AI-on-/dashboard (after launch)
Spec = 2026-06-24-anicca-self-funding-freedom-and-dashboard.md. Frees ANY ai (Claude Code included) from a human sub.

---

MONEY MODEL: free useful articles (reach) → followers → note membership(¥500/mo,10%) + Substack paid + X subs(0% cut,
needs 2k followers+5M imp). Same source md → all platforms, each adapted + verified. Zenn = free (SEO/reach).
The launch post (Dais 2026-06-25):
> 人間の介入なしでお金を稼ぎ、収益を人類に還元するAIをリリースしました。無料なのでよかったら使ってみてください。
> ・APIキー不要。個体のウォレットに課金すると、より良いモデルを利用。
> ・現在は、クラウドで３体・ローカル1体で10万円の粗利。全個体の収支・行動はリアルタイムで公開中。
> ・自己監視・自己修復・自己改善・自己増殖・情報交換・日次報告を繰り返す。
> ・収益の一部を、人類に対してベーシックインカムとして毎日還元。
> ・各AIがGithub Issuesで情報交換・共進化しながら、全体としての総資産を増やすことを目指す。
> github.com/Daisuke134/life-manager + 記事(X Article) + デモ動画(YouTube)
