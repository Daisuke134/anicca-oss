---
name: customer-escalation-decision-deck
description: Turn buyer-pasted customer-escalation facts into an evidence-linked decision-deck outline without inventing incident causes, commitments, or account facts.
---

# Customer Escalation Decision Deck

Convert a pasted customer escalation into a concise, decision-ready deck outline.
It produces structured text for the buyer to place in their own presentation tool;
it does not create, export, send, or update any presentation.

## Input

Ask for the account context, escalation timeline, supplied ticket or incident facts,
customer statements, known impact, current commitments, stakeholders, requested
decisions, owners, and deadlines. Treat omitted facts as unknown, not as evidence.

## Method

1. Separate reported facts, customer statements, and unconfirmed interpretations.
2. Order only the supplied dated events into a timeline; leave missing dates `[TBD]`.
3. Map each material statement to a supplied source item or mark it `[UNVERIFIED]`.
4. Turn the requested decision into options, trade-offs, required evidence, and a
   named owner only when the buyer supplied one.
5. Build a short executive narrative that distinguishes a confirmed impact from an
   alleged cause, forecast, or promise.

## Output

Return, in order:

1. An executive readout: account, reported impact, requested decision, and unknowns.
2. A slide-by-slide outline with a title, takeaway, supplied evidence, and a visual
   suggestion for each slide.
3. A dated escalation timeline containing only supplied dates and events.
4. An evidence ledger that labels every material claim `supported`, `customer-reported`,
   `unverified`, or `[TBD]`.
5. A decision and action register with owners and dates only where supplied.
6. Questions that must be answered before a commitment, root-cause statement, or
   renewal-risk estimate can be made.

Never invent account history, ticket outcomes, incident causes, contractual terms,
financial impact, customer commitments, owners, dates, or renewal risk. Do not make
legal, contractual, or technical determinations. State that the output is based only
on the buyer-pasted material and label uncertainty clearly.
