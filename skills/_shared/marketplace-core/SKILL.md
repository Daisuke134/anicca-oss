---
name: marketplace-core
description: Build an apply loop for a new gig marketplace. Read before adding a platform, before writing any discovery/judgement/reporting code for one, or when an existing lane applies to nothing and its own reports do not say why.
---

# marketplace-core — one apply loop, many marketplaces

An apply loop does seven things. Only three of them are about the marketplace.

```
① find postings      platform-specific
② judge fitness      SHARED
③ write the proposal SHARED
④ submit             platform-specific
⑤ read back          platform-specific
⑥ report             SHARED
⑦ remember           SHARED
```

A new platform is `①④⑤` and nothing else. If you find yourself writing a refusal
rule, a Telegram message or a duplicate check, stop: it exists here already, and a
second copy will drift. Measured 2026-09-07, Coconala's private copy of the
refusals was missing `manual_marketplace_operation` and
`original_illustration_or_modelling`, applied 20-30 times a day to 出品代行 and
イラスト作成 for a week, and the marketplace restricted the account.

## What you write

```python
discover(category_or_query) -> [posting]   # the platform's own facet, not guessed nouns
fetch(posting_id)           -> text        # enough body to judge on
submit(posting, proposal)   -> receipt_id  # the irreversible bit
readback(receipt_id)        -> bool        # the platform's own record, not yours
```

## What you get

| module | for |
|---|---|
| `work_fit.py` | ② `HARD_PROHIBITION_CLASSES` (11) for text, `category_refusal()` for a bare label, `judge()` when the lane has no planner |
| `listing_catalog.py` | ① `listing_terms()` / `search_terms()` — what the owner sells, as search nouns |
| `application_transaction.py` | ⑦ claim → submit → readback, so a crash cannot double-apply |
| `ledger.py` | ⑦ the receipt record |
| `telegram_outbox.py` | ⑥ fencing, retry, and suppression of an unchanged message |
| `telegram_delivery.py` | ⑥ the send itself — no external CLI |
| `lane_summary.py` | ⑥ the wake sentence |
| `dom_contract.py` | ④⑤ a selector that names itself when it stops matching |

## Seven traps, each measured

**1. Ask the board, don't guess nouns.** Lancers: keyword 「業務システム」 returns 3
postings; `/system` returns 23, `/writing` 30. Five category requests see three
times what twelve keyword requests do, at under half the request rate.

**2. Filter at the source.** 27 of 30 Lancers cards were 求人 — fetched, parsed,
normalised and counted before being discarded. `type[]=project` drops them before
they cost anything.

**3. Count every way out.** A CrowdWorks wake reported `inspected: 63` against
counters summing to 26. The other 37 left through a bare `continue`. A lane that
refuses without saying why cannot be repaired from its own reports, and the
counters are all the owner sees.

**4. One error name per failure, not per file.** `proposal_form_changed` was raised
from 41 places in one file and recorded by 3. It cost a day. Every raise site must
name its function and line, and any handler that *relabels* an exception must
record the original first — two such handlers made the recorder look broken.

**5. A cache that never wrote is worse than none.** Lancers' skip cache called
`hashlib` without importing it; the caller swallowed every exception with a comment
explaining why a cache failure must not stop applications. It never stopped one. It
never cached one either, so every refusal was re-judged by the planner every minute
and the 20-row batch filled with work already refused.

**6. Slices, and a reason to read the next one.** A wake reads the union in slices
of `MAX_OPPORTUNITIES`. Continue on *both* "nothing eligible here" and "all of these
are already claimed" — the second is exactly when the next slice matters, and
skipping it hid 80 of 100 fetched rows.

**7. A promoted rule must be re-read against the new catalogue.**
`mandatory_desktop_or_browser_operations` was correct on Coconala and, hours after
being shared, refused RPA scenario building and a system migration on Lancers —
that platform's core work. Building, migrating and publishing through a tool are
deliveries; only operating it for hours is not.

## Order of work

1. `discover` against the platform's own facet, and count what you drop.
2. `fetch` + `work_fit` before anything is submitted. **A lane that applies before
   it can refuse is the one that loses the account.**
3. `application_transaction` around `submit`/`readback`.
4. Report through the outbox with the title, the price and the receipt id — a real
   application must say more than a refusal does.
5. Only then widen discovery. Widening before submission is proven produces skips.

## Reading a lane that applies to nothing

In this order, because each is cheaper than the last:

```
台帳       ~/.local/state/anicca/<platform>/…       what actually happened
wake summary  eligible_count vs verified_count      found vs landed
counters      do they sum to inspected?             what left silently
evidence      *-changes.jsonl                       which selector, which posting
Telegram      skills/tools/telegram-user            what the owner really receives
```

The lane's own log is the *last* place to look, not the first. Twice on
2026-09-07 the reported error named the wrong thing: `planner_runner_failed` was a
full disk, and `proposal_form_changed` was a relabelled browser timeout.
