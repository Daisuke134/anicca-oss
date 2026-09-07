# Life Manager Cloud cost・pricing・legacy cleanup design

status: COST TRACK ACTIVE — implemented independently, one atom at a time
owner: Dais / Life Manager Cloud
scope: provider cost control, tenant usage metering, Stripe packaging decision, Cloud daily-runtime cleanup

## 0. Ordering and exclusions

- The remote Cloud product agent owns `CLOUD-01` through `CLOUD-08` in
  `2026-08-28-life-manager-cloud-telegram-product-ux-design.md`. This cost track runs independently
  and does not edit, reorder, or wait for that checklist. If the Cloud agent requests verification,
  this track may inspect its tests and evidence read-only without taking over its implementation.
- Do not use ElizaOS, replace the Life Manager runtime, stop Telegram/Calendar/routing, or modify
  local loops and Alpaca worktrees.
- Do not change Stripe prices before per-tenant production usage is measured. Existing Stripe remains
  the only payment authority.
- A file is not deleted merely because its name says legacy. Require production reference zero,
  registry/owner zero, tests proving the replacement, and provider replay-zero where effects exist.

## 1. Measured incident baseline

Google Cloud billing account `017949-09509F-6A3FB6` reports July 2026 subtotal JPY 28,568 and
August subtotal JPY 78,306: +JPY 49,738, +174.1%, 2.741x. August service totals were Geocoding
JPY 40,095, Gemini API JPY 13,444, Directions JPY 9,549, Routes JPY 7,924, Places JPY 5,054,
and Vertex AI JPY 2,227.

The dominant Maps credential was `lateness-directions`. Monitoring attributed approximately 21,524
Directions 4xx responses and 22,751 Geocoding 4xx responses to it in August. Railway `life-call`
boot logs showed the standalone scheduler owner and all daily loops active. GCP Cloud Run and Cloud
Scheduler were not competing owners. Therefore the incident was repeated paid failure work in the
single active Railway scheduler, not legitimate one-user demand and not a second Cloud deployment.

The billing account now has a non-blocking JPY 35,000 monthly budget with current-spend alerts at
50%, 75%, 90%, and 100%. Provider quotas remain unchanged because a blind cap can remove required
Calendar travel behavior.

## 2. Exact code findings

1. `apps/life-manager/migrations/2026-07-04-lm-route-cache.sql` creates `lm_route_cache`, but the
   production construction in `apps/life-manager/lib/travel.js` supplies only `new Map()` to
   `makeRouteCache`. No production code reads or writes `lm_route_cache`. A process restart loses all
   accepted route cache entries.
2. `apps/life-manager/lib/route-cache.js` stores only non-null results. A provider 4xx, timeout, or
   no-route result returns null and is retried on the next eligible tick. This matches the measured
   paid-failure pattern.
3. The 30-minute travel loop and 60-second Telegram reminder loop both use `directionsRoute`; without
   a durable store they cannot reliably reuse one accepted or failed result across restarts/owners.
4. `scheduler.js` and `inngest/functions.js` are not two independent business implementations:
   Inngest imports the per-user functions from `scheduler.js`, and `maybe-start-loops.js` owns their
   mutual-exclusion predicate. Retain both until one deployment mode is formally retired.
5. `routesDriveMinutes` / `directionsMinutesGoogle` are no longer on the default production fallback,
   consistent with Routes charges disappearing after the free-transit-first release. They remain
   explicit compatibility/test seams; assess deletion only after `CLOUD-02` timing acceptance.

## 3. Pricing decision before Stripe mutation

The launch recommendation is a hybrid subscription, not raw provider-cost pass-through:

| Plan | Monthly price | Included product usage | Overage behavior |
|---|---:|---|---|
| Free | JPY 0 | Calendar connection, settings, cached route display, up to 20 successful managed actions/month, no optional phone | Keep read/settings/cached results available; ask to upgrade for new paid actions |
| Plus | **JPY 4,980** | Core daily Telegram/Calendar automation and 500 successful managed actions/month | Offer prepaid 100-action pack for JPY 980; never count internal retries or provider failures |
| Pro | JPY 9,800 | 2,000 successful managed actions, optional phone allowance, priority/high-cost AI | Additional prepaid packs; explicit warning before high-cost media/agent work |

`successful managed action` is the customer-facing value metric. Internal polling, cache hits,
retries, 4xx/5xx, duplicate suppression, and provider reconciliation cost zero customer credits.
Track actual vendor cost separately for margin control.

JPY 4,980 is provisional until the beta records two to four weeks of normalized per-tenant cost. The
release gate is p95 monthly direct cost at or below JPY 1,000 for Plus (at least 80% gross margin before
support and fixed overhead). If it exceeds that boundary, reduce provider waste or lower included
high-cost actions; do not silently degrade Calendar, Telegram, or cached daily behavior.

## 4. Free operation boundary

Everything cannot be guaranteed free while Life Manager pays commercial LLM, Maps, telephone, hosting,
and payment fees. A sustainable free tier is possible by keeping its marginal paid work bounded:

- use Google Maps monthly free SKU caps only as a shared safety buffer, never as an unlimited promise;
- free transit first, durable accepted-route cache, and bounded negative cache before Google fallback;
- deterministic code before LLM and small models before expensive models;
- no optional phone or video generation in Free;
- cached/read/settings behavior remains available after the action allowance is exhausted;
- referral/sponsor credits can fund extra actions, but never hide provider cost or promise permanent
  unlimited use.

## 5. OSS decision

Reviewed current source from `openmeterio/openmeter`, `BerriAI/litellm`, and `unkeyed/unkey`.

- OpenMeter provides event metering, entitlements, credits, and billing, but its full stack is beta and
  duplicates existing Supabase/Stripe authority. Do not add it for launch.
- LiteLLM provides LLM virtual keys, spend tracking, budgets, and caching, but it cannot meter Maps,
  Telegram, or telephone value consistently. Do not add an AI gateway before the existing call sites
  have one usage contract.
- Unkey provides API-key analytics and durable rate limits, but Life Manager's public product is
  Telegram/tenant based, not a customer API. Do not add it now.
- Reuse the event/entitlement concepts: append a tenant-scoped usage event to the existing Supabase
  ledger and report accepted aggregate usage to Stripe's official usage meter. No new billing engine.

Sources:

- Stripe usage-based billing: https://docs.stripe.com/billing/subscriptions/usage-based
- Google Maps SKU pricing/free caps: https://developers.google.com/maps/billing-and-pricing/pricing
- Gemini API pricing: https://ai.google.dev/gemini-api/docs/pricing
- OpenMeter: https://github.com/openmeterio/openmeter
- LiteLLM: https://github.com/BerriAI/litellm
- Unkey: https://github.com/unkeyed/unkey
- Reclaim pricing: https://reclaim.ai/pricing
- Motion pricing: https://www.usemotion.com/pricing
- Sunsama pricing: https://www.sunsama.com/pricing

## 6. Ordered TODO owned by this track

This track owns these atoms in the exact order below while the remote Cloud product agent separately
owns `CLOUD-01` through `CLOUD-08`. Finish and integrate one cost atom before starting the next.

1. **COST-01 — observe (DONE):** add one tenant/provider/feature usage-event contract and dashboard
   query; record success, cache hit, failure class, provider units, and estimated direct cost without
   secrets. Integrated by PR #4282. Production RPC readback returned HTTP 200 and 17 natural Maps
   usage events for 2026-09-06 UTC: Directions failure 5 / USD 0.025 estimated, Geocoding failure
   5 / USD 0.025, and Geocoding success 7 / USD 0.035. The estimates are provider list-price
   accounting events, not a replacement for the Google Cloud invoice.
2. **COST-02 — stop paid failure replay (DONE):** wire `lm_route_cache` to production, add bounded
   negative cache for deterministic 4xx/no-route and short backoff for timeout/5xx, and prove a later
   valid route can recover. PRs #4291 and #4298 integrated the durable store and the production-observed
   raw-address fallback gap. Focused route/travel tests passed 135/135; a fresh-process test proves zero
   additional provider calls during the 30-minute deterministic-failure TTL and recovery after expiry.
   Production build `342aaf07f` returned HTTP 200, wrote 4 natural success rows and 5 natural
   `google/no_route` negative rows with TTL 1800 seconds, and the next readback kept paid Directions
   failures at 5 (zero increase). Network/5xx uses a 120-second backoff. Raw addresses are represented
   only by opaque SHA-256 cache scopes and are not persisted.
3. **COST-03 — one route fact (DONE):** travel block, Telegram reminder, and optional call reuse one
   event/version-scoped route result. Exact event ID、schedule、endpoints、go/return purpose changes
   invalidate it. PR #4419 merged as `d69b21ee5773f4629a15d226008be7095d68cf02`; focused
   current-main route/travel/reminder/wake verification passed 129/129 and Railway health serves that SHA.
4. **COST-04 — owner and spend guard:** prove exactly one Cloud scheduler owner, add tenant/provider
   daily circuit breakers that preserve cached/read/settings behavior, and alert before rejection.
5. **COST-05 — beta unit economics:** run two to four weeks, calculate p50/p95 direct cost per active
   tenant and per successful managed action, then confirm or revise JPY 4,980 before Stripe mutation.
6. **COST-06 — Stripe meter:** map only successful managed actions to the existing Stripe customer,
   add allowance/credit-pack behavior, test duplicate/replay/refund boundaries, then request the
   separately authorized real payment proof.
7. **CLEAN-01 — compatibility retirement:** after `CLOUD-02`, prove whether old Routes Pro helpers and
   legacy Directions compatibility seams have production callers. Delete only zero-reference seams
   and their now-invalid tests/comments.
8. **CLEAN-02 — runtime retirement:** after the chosen Cloud scheduler mode has natural production
   receipts, retire the unused alternate host adapter/registration surface without deleting shared
   per-user business functions.
9. **CLEAN-03 — final census:** run source reference, deployment entrypoint, loop registry, provider
   traffic, migration, and secret-free receipt checks; record retained owners and deletion evidence.

## 7. Acceptance

- Normal Calendar/Telegram/cached daily behavior remains available at every plan boundary.
- One user action produces at most one accepted billable provider result per event version and purpose.
- Deterministic 4xx/no-route repeats produce zero additional paid calls during the negative-cache TTL.
- Internal failures and retries never consume customer allowance or enter Stripe usage.
- Plus p95 direct monthly cost is at most JPY 1,000 before enabling the JPY 4,980 Stripe price.
- No local loop, Alpaca state/worktree, ElizaOS component, or unrelated production route is modified.

## 8. Friend beta launch and pricing validation

Cloud engineering and operator acceptance are complete. Real friend UAT is the next action. The
public entrypoint is `https://aniccaai.com/life-manager`, and the fastest mobile entrypoint is
`https://t.me/LifeManagerBotbot?start=lp`.

### Product promise

予定のたびにGoogle Calendarと地図・乗換案内を行き来し、出発時刻を逆算する手間をなくす。
Life ManagerはGoogle Calendarの次の予定に合わせて移動時間を確保し、出発前に経路を
Telegramへ送る。「Google Calendarや地図を二度と見なくてよい」とは表現しない。予定の登録・変更、
経路変更の確認は引き続き必要になる場合があるため、「毎回開いて調べなくていい」を正本の表現とする。

### Friend DM

> 今、Life Managerの少人数βを始めてるんだけど、5分だけ試してもらえない？
> Google Calendarをつなぐと、次の予定までの移動時間を自動で予定に入れて、
> 出発前に電車や乗換案内をTelegramで送ってくれる。
> 毎回カレンダーと地図アプリを行き来して、出発時間を逆算しなくてよくなるものです。
> 電話通知は任意で、3日間無料。
> https://t.me/LifeManagerBotbot?start=lp
> 使って詰まったところだけ教えてほしい！

### X launch post

> 次の予定に間に合うために、カレンダーを確認して、乗換案内を検索して、
> 逆算して出発時間を決める。この作業を、毎回やらなくてよくしました。
>
> Life ManagerはGoogle Calendarを読み、移動時間を予定として自動で確保。
> 出発前になると、乗る電車と乗換案内をTelegramへ送ります。
>
> 電話通知は任意。3日間無料で試せます。
> https://t.me/LifeManagerBotbot?start=lp

公開画像には実在の予定名、住所、現在地、個人名を残さない。Telegramの経路通知は、これらを
トリミングまたはぼかした画像だけを使用する。旧投稿の「生活をまるごと管理」「電話で起こす」は、
現在のDAILY機能より広く、電話が必須に見えるため再利用しない。

### Ordered post-release TODO

1. Daisが上記DMを友人へ送り、同じTelegramリンクをXへ投稿する。
2. 友人UATで、開始、Google Calendar接続、home設定、Travel block、Telegram経路通知までを確認する。
3. 本番usage ledgerを2〜4週間集計し、active tenantごとのp50/p95直接原価と成功action単価を確定する。
4. Plusのp95直接原価が月額JPY 1,000以下なら、月額JPY 4,980を確定する。超える場合は、機能を止めずにprovider wasteと含有量を先に調整する。
5. 価格確定後にだけ既存Stripeの価格・allowanceを変更し、test modeでentitlementとwebhook replayを再確認する。

現時点の単一推奨価格は月額JPY 4,980。JPY 10,000 MRRを明確に超えるには有料会員3人が必要で、
MRRはJPY 14,940になる。2人ではJPY 9,960で目標を40円下回る。正常化後の直接原価を
1人月JPY 1,000以下に保てた場合、3人の直接原価上限はJPY 3,000、決済手数料・support・固定費前の
粗利はJPY 11,940、粗利率は約79.9%になる。現在のGoogle Cloudアカウント全体のforecastを
「1人当たり原価」とは扱わない。そこには製品外利用と請求lagが含まれるため、正確なunit economicsは
手順3のtenant別実測で確定する。

## 9. Superseding monetization decision — monthly allowance, one plan

「beta」「3日trial」「onboarding直後の月額プラン確認」は廃止する。Life Managerは正式製品として、
card不要の毎月無料利用枠を全員に付与する。無料枠は毎月resetし、内部API tokenや失敗retryではなく、
ユーザーへ価値が届いた`successful managed action`だけを消費する。Calendar接続、設定変更、cache hit、
内部poll、重複抑止、4xx/5xx、provider retryは無料枠を消費しない。

無料枠の初期値は月30 successful managed actionsとする。80%到達時は完了したactionの後に1回だけ予告し、
100%到達時は進行中・claim済みの通知を完了してから次の新規provider effectを保留する。枠を使い切っても、設定、接続解除、既存情報、
cache済み結果は利用可能に保つ。新しい有料provider effectだけを停止し、Telegramで現在の利用数、次回reset、
継続方法を説明する。途中でStripe情報を要求せず、本人が継続を選んだ時だけStripe Checkoutを開く。

有料planはLife Manager `$29/month`の1つだけとする。月500 successful managed actionsを含み、電話は
明示opt-inかつ月間allowance内で提供する。複数tier、provider別課金、ユーザーに見えるtoken換算、前払いcard、
onboarding paywallは作らない。将来agent economyがtenantのcomputeを実際に賄える場合、検証済み収益を
creditとして本人の請求へ充当できるが、未実現収益を無料化の根拠にはしない。

無料枠到達時の正本copy:

> 今月の無料利用分を使い切りました。Life Managerの設定とこれまでの情報はそのまま残っています。
> 引き続きLife Managerに生活を管理させる場合は、月額$29でこのまま続けられます。
> 次の無料利用分は翌月に戻ります。

`$10K MRR`はUSD 10,000/monthを意味する。$29 planでは345 paying usersでUSD 10,005 MRRになる。
JPYはGoogle Cloud invoiceの照合にだけ併記し、MRR、ARPU、価格、粗利の主通貨はUSDとする。

### Ordered correction TODO

1. **DONE (local/review) — Telegram-native onboarding:** Google consent以外のMini App必須stepを削除する。
   `/start`のprivate-chat actor claim、Telegram言語による日英表示、通常URLのGoogle consent button、
   WebApp非使用、cookie不要のone-time OAuth callback、Calendar ACTIVE同期、Telegram復帰と自宅住所質問を
   branch `feat/lm-telegram-native-start-20260907`で実装。ローカルcontractはPASS済み。
   ローカルcontract 153/153とfresh read-only reviewがPASS。本番反映は項目4でまとめて行う。
2. **DONE (local/review) — optional phone:** 電話の用途、任意性、番号保存とcall opt-inの分離をTelegramで実装する。
   電話skip、番号保存時call OFF、別質問での明示opt-in、日英copy、onboarding paywall削除を実装し、
   関連test 160/160とfresh read-only reviewがPASS。
3. **DONE (local/real PostgreSQL/review) — monthly free allowance:** 3日trial表示とonboarding内の料金CTAを削除済み。
   月30回（paidは500回）のtenant/month/event ledger、15分pending reservation、success確定、月1回の枠到達noticeを追加し、
   Travel blockとTelegram reminderの新規route effect前へ接続。関連test 141/141とTravel wiring 113/113がPASS。
   任意電話のprovider effectと成功receiptも同じevent allowanceへ接続済み。ただしfresh reviewで次のcorrectness gateが
   fix-firstとなったためDONEにしない。(a) reservation tokenを発行し、complete/releaseを予約月と所有tokenで照合する、
   (b) 同一eventの並行workerは1つだけprovider effectを実行する、(c) not-due・未選択・duplicate・no-route・past・
   create/send失敗は自分のpendingだけを解放する、(d) route cache/negative cacheをallowanceより先に読み、枠到達後も
   cached/read/Calendar/Telegram/settingsを維持する、(e) 80%と枯渇noticeをplan-awareなused/limit/reset copyにし、
   exhausted後の80%逆順送信を禁止する、(f) noticeの曖昧配送を永続reconciliation可能にする。
   実DBで並行reserve、誤token拒否、月跨ぎcomplete/release、pending残留0、limit後cache hitのprovider call 0、
   exhausted優先、notice replayを証明。さらに通常通話をpaid-onlyにし、tenant timezoneでresetする月3,600接続秒の
   owner-token ledgerを追加した。並行予約込みで上限を超えず、発信前に`accepted`を永続化し、Telnyx
   `time_limit_secs`へ残秒を渡す。transport/5xx/malformed successはclaimを保持し、署名済み`call.hangup`から
   公式`call_duration`を取得してのみ精算するため、bridge crash、response loss、`LM_AMD=off`でもreplay-safe。
   関連test 236/236、migration二重適用、実PostgreSQLの3,480秒時並行reserve 1/2件、誤token拒否、37秒精算後
   残83秒、stale accepted保持をPASS。fresh read-only reviewはSHA `999755fe0`を`ship`判定。
4. **IN PROGRESS — merge/production:** 1〜3をreview・mergeし、必要なmigrationを本番適用してproduction deploy/readbackを行う。
5. Telegram Webへ既存sessionまたは通常loginで入り、DaisのTelegram actorと隔離test actorで、新規開始から
   最初のTravel block・乗換案内・Telegram provider receipt・replay追加送信0までE2Eする。
6. E2E receipt後にだけ公開導線を再開し、友人DMとX投稿を行う。

## 10. `$29/month` unit-cost envelope

Planning exchange rate is USD 1 = JPY 150. One normal active user means 60 physical events/month,
one accepted route fact per event/version/purpose, at most 100 short Gemini text actions, and optional
calls averaging one total connected minute per physical event across T-10 and T-5. Free SKU caps are
excluded from the safety calculation even when the actual invoice is lower.

| Component | Unit assumption | Monthly cost without calls | Monthly cost with calls |
|---|---:|---:|---:|
| Google Geocoding | up to 2 × USD 0.005 per physical event | USD 0.60 | USD 0.60 |
| Google Routes/Directions | up to 2 × USD 0.005 per physical event | USD 0.60 | USD 0.60 |
| Gemini short text | 100 bounded Flash actions | USD 0.20–1.00 | USD 0.20–1.00 |
| Calendar / Telegram APIs | no per-message product charge | USD 0 | USD 0 |
| Gemini Live | measured planning rate about USD 0.023/min | USD 0 | USD 1.38 |
| Telnyx voice, streaming, AMD, destination carrier | conservative blended reserve | USD 0 | USD 2.00–4.00 |
| provider error / FX reserve | after cache and replay fences | USD 0.50–1.50 | USD 1.00–2.50 |
| **Direct API total** | | **USD 1.90–3.70** | **USD 5.78–10.08** |

Therefore `$29/month` is sufficient only while phone usage is bounded. The paid plan includes at most
60 connected phone minutes/month; after that, Telegram, Calendar, Travel block, and cached/read/settings
continue, while new phone calls wait for the next monthly reset. This is one plan with a fair-use boundary,
not a second tier. At direct cost USD 10.08, contribution before Stripe, hosting, support, and fixed costs is
USD 18.92 and direct-API margin is 65.2%. At the expected USD 6 cost it is USD 23 and 79.3%.

The free allowance is 30 completed managed events/month, not 30 internal effects. One event may create a
Travel block and one Telegram乗換案内 while consuming one allowance unit. Optional phone is excluded from
the standing free allowance except for one onboarding test call; this caps expected free-user direct API
cost around or below approximately USD 1.50 before shared fixed costs. Usage resets monthly at the tenant billing timezone.
