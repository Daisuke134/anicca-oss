# Building a Storefront lane on a marketplace

Measured on Coconala and Lancers, 2026-08 through 2026-09-07 — Coconala over the longer
window, Lancers on 2026-09-07 specifically while wiring a create-from-catalogue path.
CrowdWorks is included only to say it has no Storefront lane to build. Every fault below cost
a wake, a day, or in one case a listing that could never have been created at all.

## Does this marketplace have a shelf?

Ask this before anything else. It is not rhetorical.

**CrowdWorks has no storefront.** Workers search postings, propose, and get contracted —
there is no persistent listing to publish or maintain. **Coconala and Lancers both have one,
and they are not the same shape.** Coconala's listing form is a single page; Lancers'
`/myplan/add?type=manual` is a six-step wizard (fault 10). A lane guide that assumes a shelf
exists will invent one where there isn't a place to put it. The first thing a new-platform
Storefront loop does is find out which of these it is dealing with, not assume the Coconala
shape generalizes.

## Revenue reality

Storefront revenue across every platform this has run on is **¥0**. Coconala: 492 views, 0
purchases in 30 days across 4 published listings. Lancers: 8 search impressions, 0 orders on 1
listing. Meanwhile **all 8 sales ever came through the Apply lane** (most recent 2026-08-12,
see `marketplace-apply-lane.md`).

The demand evidence explains why. A competing listing in the same category sells at
**¥29,000 with 464 reviews**; ours were priced ¥5,000–7,000 with 0 reviews. On a marketplace
that ranks by review count, a new cheap listing is not a cheaper competitor — it is an
invisible one. Publishing is necessary and nowhere near sufficient. Say this plainly to
whatever reads this next: a loop that measures itself by "listings published" will report
success while earning nothing.

## Reuse before writing

| Need | Already exists |
|---|---|
| Platform-neutral decision core: KEEP/IMPROVE/RETIRE/REPLACE, mutation-contract validation and sealing, demand extraction and cluster scoring, replace planning, in-flight-draft detection, prepared-contract recovery, rejection ledger, guard identity | `skills/_shared/marketplace-core/scripts/storefront_kernel.py` — platform is a parameter; the file contains no `coconala` string |
| Catalogue load and per-platform projection, including the Lancers delivery-day enumeration | `skills/_shared/marketplace-core/scripts/listing_catalog.py` (`project_lancers`, `_lancers_delivery_days`) |
| What to sell, at what price, per platform — 20 platform-independent listings with per-platform overrides | `skills/gig-work/profile/listings/catalog.json` |
| Matching exactly one element, and recording what it saw instead of discarding it | `skills/_shared/marketplace-core/scripts/dom_contract.py` (`exactly_one`, `visible_one`) — see `marketplace-apply-lane.md`'s "refuse loudly" rule, which applies here unchanged |
| Deciding whether a failure may end a wake | `references/transient-vs-fatal.md` |
| Deciding whether a loop may operate an account on a given platform at all | `references/platform-automation-map.md` |
| Per-owner browser context so lanes stop fighting over one CDP socket (tracked, not finished — see fault 14) | `skills/browser/scripts/cdp_context_lease.py` |

A Storefront adapter's only job is DOM operation for one platform. An adapter that grows its
own selection logic, its own ledger or its own catalogue is the defect this file exists to
prevent.

## Where the platforms stand (2026-09-07)

| Piece | Coconala | Lancers | CrowdWorks |
|---|---|---|---|
| Has a shelf at all | yes | yes | **no — no lane to build** |
| Listing form shape | staged draft → publish; the wizard question was never asked here | six-step wizard, steps hidden by CSS-module class (fault 10) | n/a |
| Category vocabulary source | official selects, read live | catalogue override still wrong (fault 8); recoverable from public taxonomy (fault 9) | n/a |
| Plans per listing | flexible | exactly three, enforced (fault 11) | n/a |
| Revenue to date | ¥0 | ¥0 | n/a |
| Reads the shared catalogue | yes — `storefront_direct.py` loads `entries_by_family` and passes the family's entry into the create decision as the owner's pre-decided spec | yes — `storefront_offer.py` | n/a |
| Listings published by the loop | 4 | 1, hand-authored, predating the catalogue wiring | n/a |
| Catalogue-derived listing ever published | yes | **no — never** | n/a |

## Coconala faults

**1. A 7-hour silence with a wrong name.** From 00:51 to 08:09, 241 consecutive wakes reported
`official_inventory_empty_or_invalid` while the lane was in fact reading a login page. The
Apply lane hit the same session expiry the same night and recovered by itself, because it
names that failure correctly. The difference was the name, not the code. Cross-reference
`transient-vs-fatal.md` — this is exactly the "the world did not answer" pile, misfiled as a
verdict.

**2. Locale-dependent decoding under launchd.** The hero-image font lookup
(`_hero_font_path` in `skills/earn/gig/scripts/storefront_direct.py`) resolved
`fc-match -f %{file} "Hiragino Sans"`, whose answer names a Japanese path:
`/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc`. Read with `subprocess.run([...], text=True)`,
Python decodes with the locale's encoding — launchd sets no `LANG`, so the name came back
mangled and the wake reported `storefront_generated_image_font_missing` for a font that was
installed the whole time. **This was the final blocker preventing any publish at all.** Fixed
by naming the encoding explicitly (`encoding="utf-8", errors="strict"`). The general rule:
anything launchd runs has no locale, so every subprocess decode must name its own encoding
rather than inherit one.

**3. Counting the wrong effect.** The wake counted draft stages as its effect, so it satisfied
its own one-effect-per-wake fence without anything becoming buyer-visible. Corrected to count
only the buyer-visible effect. The rule: an effect fence must count only buyer-visible effects,
or a loop can run forever satisfying itself while nothing changes on the public page.

**4. A rejection rule with no expiry.** Three strikes against a proposal had no time bound,
permanently blocking one capability family. Strikes are now counted only since the running
release's `cut_at`.

**5. A dismissal that fired while the thing existed.** The 3-strike rule dismissed a demand
cluster while a real draft for it was live. Gated on the live draft being absent before
dismissing.

**6. "Oldest first" picked a deleted draft.** The stranded-draft recovery chose the stalest
ledger row, which named a draft that no longer existed. Fixed by requiring membership in the
live draft census. The general rule: a ledger row is a claim about the world, not the world —
intersect it with a live census before acting on it.

**7. A response schema the provider rejected.** A nullable field modeled as `oneOf` produced
`Invalid schema for response_format 'codex_output_schema': ... 'oneOf' is not permitted`. The
form the provider accepts is `{"type": ["string", "null"]}`. **Local tests cannot catch this —
only a real provider call can**, because the schema was valid JSON Schema and would pass any
local validator. See `transient-vs-fatal.md`'s closing rule: where a restriction is enforced
somewhere you cannot run locally, encode the enforcer's restriction in the test, not the
standard's.

## Lancers faults

**8. A field value that does not exist in the form.** All 20 catalogue families carry
`platform_overrides.lancers.category = "システム開発"` in `skills/gig-work/profile/listings/catalog.json`.
The live `___main_category_id` select (read at `skills/earn/lancers/scripts/storefront_offer.py`,
where the field is filled via `_field(page, '[name="___main_category_id"]').select_option(label=...)`)
offers exactly nine labels, read live on 2026-09-07: `AI・プログラミング・システム開発` /
`音楽・ナレーション` / `Web集客・マーケティング` / `ビジネス・コンサルティング` /
`デザイン・Webデザイン` / `データ分析・作業自動化` / `動画制作・アニメーション・写真` /
`ライティング・翻訳` / `その他`. `"システム開発"` is not among them, so every catalogue-derived
create would have failed on every family with the override as it stands.

Nothing in the catalogue's own tests could see this, because the catalogue was self-consistent
— it was only wrong about the world. The rule: an overlay naming a provider's own vocabulary
must be checked against that vocabulary, and the check belongs in a test, not a hope.

Do not take the corrected value from the production listing file by pattern-matching on a
similar-looking field. `skills/earn/lancers/products/monthly-sns-content-ops-v1.json` carries
two different taxonomies: its package `category` is `Web集客・マーケティング`, while its
`software_portfolio.category` is `AI・システム開発・運用` — a portfolio label that the package
form's select does not offer at all. Reading the nearest plausible string out of a neighbouring
structure is how a wrong value gets laundered into looking measured.

**9. Dependent selects cannot be read from the initial DOM.** `ProjectPlanForm.project_category_id`
(the subcategory) populates only after the main category is chosen — both `_apply` and
`_fill_create_form` in `storefront_offer.py` wait for it with
`page.wait_for_function(...option').some(o => o.textContent.trim() === label))` rather than
reading it up front. Its vocabulary was recovered without touching the account, from the
platform's own public browse taxonomy at `https://www.lancers.jp/menu/search`, whose eight
parent groups map one-to-one onto eight of the nine main categories. Corroborated by the
already-live listing's own subcategory (`SNSマーケティング・運用代行`) appearing verbatim under
the matching parent, then confirmed by a live run selecting a catalogue-derived subcategory
successfully. The general technique — **a public taxonomy page is a free, no-account, no-lock
source for a form's own vocabulary** — is reusable on any platform whose create form gates its
own field list behind a login.

**10. A creation form that is a wizard, not a form.** `/myplan/add?type=manual` is six steps —
基本情報 → 料金表 → 業務内容 → 確認事項 → 画像ほか → 公開 — with every step present in the DOM
at once and all non-current steps wrapped in a `div` whose CSS-module class matches
`_hidden_` (the observed hash `_hidden_p1vuy_39` is build-generated and must never be
hardcoded). On `main` as checked into this worktree, `_apply()` in `storefront_offer.py`
already walks the steps with `_step(page, "料金表")` / `_step(page, "業務内容")` / etc. before
filling each one's fields, because it edits an *existing* listing through the same wizard.
`create_package()`'s `_fill_create_form()`, the path from the shared catalogue to a *brand
new* listing, still fills everything flat in one pass with no step-walking — the shape that
produced `Locator.fill: Timeout 30000ms exceeded` on `ProjectPlanMenuForm[0].description`: the
locator resolved, the element had a zero-size bounding box because its ancestor carried
`_hidden_`. **The diagnostic worth teaching: a locator that resolves but times out on fill
means a hidden ancestor, not a missing element.** A fix that walks `create_package()` through
the same steps `_apply()` already uses exists on the unmerged branch
`feat/lancers-lane-creates-from-catalog-20260907` (PR #4537) — not merged as of this writing,
so treat the flat-fill shape above as what `main` still does. Walk the steps, verify arrival
after each advance, and name the step that stalled along with any on-page validation text.

**11. A validator can be right and still block every listing.** Lancers requires exactly three
plans — `_require_create_fields` in `storefront_offer.py` enforces `len(value) != 3` for
`"plans"` and raises `create_field_missing` otherwise. 18 of 20 catalogue families had two
tiers when this was measured; only 2 families could be sold there. Resolved by adding a third
tier to every family in `catalog.json` (confirmed: all 20 listings there now carry exactly
three tiers). The rule: a per-platform structural requirement is a catalogue design
constraint, not an adapter bug — fixing it in the adapter would mean inventing a tier with no
grounding in what the work actually is.

**12. Enumerations must be read, not assumed.** Delivery time offers exactly
`LANCERS_DELIVERY_DAYS = (1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30, 45, 60, 75, 90)` days
(`listing_catalog.py`). A catalogue tier quoted at 18 days is projected up to 21 by
`_lancers_delivery_days`, never rounded to the nearest option. `storefront_offer.py`'s
`_select_delivery_time` selects by option *label*, matching `r"([0-9]+)\s*日"` against every
option's text and raising `create_delivery_time_unmatched` — naming every option it saw — on
no match, rather than selecting by index or nearest value. Select by label, never by index or
neighbour, and stop the wake loudly on a mismatch, because a mismatch means the form changed
shape.

## Cross-cutting, both platforms

**13. Shipping is not merging** — same as the Apply guide (`marketplace-apply-lane.md`), but
restate with the storefront's own instance: a merged fix stays dormant until a release is cut
from a main SHA and **each label is repointed at it**, one label at a time. Fault 10 above is
exactly this in progress: the fix exists on PR #4537 and does nothing for a running Lancers
lane until it is merged, released, and its label is repointed.

**14. Lanes sharing one CDP port contend.** CDP allows one websocket per target, which
produced `HTTP 500` between lanes on the same port. Per-owner browser contexts via
`Target.createBrowserContext` are the fix, implemented in
`skills/browser/scripts/cdp_context_lease.py` — but wiring every lane through it is **tracked
work, not finished work**. Say so rather than assuming it is already in effect.

**15. Nested account locks deadlock.** `fcntl.flock` locks an open file description, not a
process, so a second acquisition nested inside a held one waits on itself forever. A wake that
needs two independent effects on the same account must take the lock twice, sequentially, not
hold it across both.

**16. Official readback is the only proof.** Nothing is claimed as published until it is read
back from the public page. A submit that returns 200 is not a listing; a listing id parsed
from the post-submit URL, then confirmed by reading the public page it names, is. This is the
same discipline `marketplace-paid-lane.md` states for Paid: "Paid work is not revenue until the
relevant official money receipt exists." A Storefront listing is not live until the relevant
official public-page receipt exists.

**17. An env var frozen at migration time outlives every later change.** Merging, cutting a
release and repointing the label all leave it untouched, because the plist writer preserves old
environment variables and the new definition never mentions the key. A config file that no
longer reaches the job is worse than no config file, because it reads as authoritative. This is
`_preserve_operational_attributes` in `runtime/loop/lm_loop_apply.py`: every old
`EnvironmentVariables` entry is carried forward unless named in `retired_environment_keys`, and
once `hf-gig-apply-direct` and `hf-gig-reply-detector` migrated onto lm-loop's registry
(`config/loop-registry.json`), their rendered plist stopped mentioning
`GIG_DISK_HEADROOM_KIB` at all — the value frozen in each plist at migration time (`"0"`,
inherited from the legacy `skills/earn/gig/config/launchd-jobs.json` manifest) survived a
merge, a release cut, and a label repoint of `launchd-jobs.json`'s corrected `"524288"` before
anyone noticed, because that file no longer had any path to either lane's installed plist.
Verify a config change by reading the installed plist, not by observing that the label was
repointed.

## What is still unproven

State this plainly rather than let the fault list above read as a working lane:

- No catalogue-derived listing has ever been published on Lancers. Fault 10's fix is unmerged;
  until it lands, `create_package()` dies on the second step (料金表) at
  `ProjectPlanMenuForm[0].description`, which is the first field the flat fill reaches that
  belongs to a step other than 基本情報.
- Storefront revenue is ¥0 on every platform this has run on.
- The Lancers 公開 step's submit control label is unobserved. `_create_submit_control` in
  `storefront_offer.py` discovers it at runtime from a set of known labels
  (`_CREATE_SUBMIT_LABELS = ("確認画面へ", "公開する", "公開", "保存する", "保存")`) and fails
  loudly, naming every button text it actually saw, rather than guessing — that runtime
  discovery is what covers the gap, not a confirmed observation of which label really appears.
- Per-lane browser contexts (fault 14) are not finished; lanes on a shared CDP port can still
  contend.

A guide that reads as if the lane works would be the worst possible version of this file.
