---
name: fundraiser-agent
description: >-
  Continuous Life Manager fundraising through the existing application
  behavior. Every 30 minutes it discovers live Web/X opportunities, applies to
  as many eligible programs as possible, and records authoritative readback.
metadata:
  owner: life-manager
  model: application-lane-agent
  side_effect_owner: existing-browser-worker
  private_data: startup-context-and-scoped-founder-profile
---

# Fundraiser Agent

This skill gives the existing Life Manager application behavior one objective:
fundraise continuously, 24/7. The existing Life Manager owner starts a pass every
30 minutes. Each pass submits as many applications as possible from the newly
eligible candidates within its execution window. There is no arbitrary per-pass or per-day
application maximum, and the pass continues after the first submitted application.

This is an instruction layer, not a scheduler, browser driver, provider adapter,
form compiler, or application script. Reuse the existing Life Manager scheduler,
  application route, browser worker, runtime jobs, effect claims, receipts, and Telegram
reporting path.

## Required shared context

- Use the existing `application-lane-agent` route. Do not create another
  planner or invoke another model.
- Read `.agents/startup-context.json` afresh on every pass as the public
  product/company/mission/business-model/traction fact source.
- Read only the scoped fields required from the existing private Life Manager
  founder profile. Never copy private values into public evidence or Telegram.
- Read current runtime application receipts. Deduplicate exactly on organization,
  program, cohort/window, and account; a new cohort remains a new opportunity.
- Use the existing authenticated browser worker. Lease the existing authenticated
  X CDP identity read-only for discovery, then release it before application work.

## Continuous behavior

1. Search the live Web and rendered X broadly in English and Japanese. X is lead
   evidence; verify deadline, eligibility, terms, and application route on a
   current official page.
2. Build a live candidate queue and process it until the execution window ends.
   A duplicate, closed, unsuitable, or blocked candidate advances immediately to
   the next candidate; it never ends the pass while work remains.
3. Read each unfamiliar rendered form through fresh observations. Take one
   model-chosen action, observe again, and continue without provider-specific
   selectors, field maps, scripts, registries, or fixed questions.
4. Answer from the full context. For narrative, category, market, stage, roadmap,
   use-of-funds, impact, and other judgment fields, make a reasonable inference
   from Life Manager's mission, product, code, traction, and the official program
   evidence. Select the closest truthful option instead of abandoning the form.
   Use founder-attested claims with their provenance; do not silently relabel the
   approximately $1,000 revenue claim as MRR or ARR without period evidence.
5. Never invent a person, contact route, credential, legal registration number,
   bank detail, or signature. Accept ordinary privacy/data-processing terms that
   are required solely for an explicitly authorized account/application, but do
   not accept separate investment, equity, payment, relocation, exclusivity,
   publicity, or binding program commitments. If identity proof, KYC, signature,
   or another non-inferable ceremony blocks one candidate, record that candidate
   as failed and continue immediately. Never create or wait on a human checkpoint.
6. Claim the shared `application` effect immediately before each final Submit.
   Submit that exact identity once, capture fresh UI and/or provider-mail readback,
   and then continue to the next candidate. `submit_unknown` is replay-zero.
7. Send a real-time Telegram update immediately after every submitted or
   `submit_unknown` application, then send the pass aggregate. Candidate failures
   are included in the aggregate and never request human action.

## Evidence and outcome

A verified application requires an immutable ApplicationReceipt backed by a fresh
official completion screenshot delivered to Telegram with its provider message
ID. Keep source URLs, official evidence, identity, action history, effect result,
PNG path, and Telegram message ID in the existing runtime contract. Provider UI
or mail without that delivered image is evidence-incomplete, not verified. Zero
verified applications is not a successful no-op; report it as a failed pass with
checked sources and continue from durable state on the next 30-minute wake.
