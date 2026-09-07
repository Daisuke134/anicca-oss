---
name: mercor
description: "Mercor provider lane for Life Manager: safe Google/Gmail authentication, resume/profile maintenance, grounded applications, and verified earnings. Use for Mercor jobs, assessments, applications, and the Life Manager job loop."
---

# Mercor

Mercor is an independent Life Manager revenue marketplace, not a Job Hunter subfeature. Prioritize Japanese/Japan, software, AI and system-development work, then continue through other roles. `apps/job-search-loop/` owns Mercor browser effects while shared marketplace contracts own reusable Apply policy and effect reporting.

## Canonical owners

- Candidate truth and resume variants: `~/.config/anicca/job-search/profile.json` and private material SSOT
- Shared Apply policy and reporting: `skills/_shared/marketplace-core/`
- Browser/application runtime: `apps/job-search-loop/`
- Cadence and installed owner: `config/loop-registry.json` → `mercor-revenue-application`
- Private Mercor state: `~/.local/state/anicca/job-search/mercor/`
- Integration spec: `docs/superpowers/specs/2026-08-22-mercor-life-manager-consolidation.md`

## Authentication hard stops

1. Use ordinary Google sign-in and inject the Keychain password only into the isolated UI. Never print or persist the secret.
2. Never click a browser Google 2FA button with accessible name `はい`; the user alone approves `はい` inside the Gmail iOS app.
3. Never use account recovery, reset, registration, recovery-email, or recursive alternate-method paths.
4. On any recovery/reset/wait screen, record the URL and visible text and stop.
5. Never use another site's tab or the trusted daily-driver browser.

## Application policy

- Reconcile an existing in-progress Mercor application before discovering a new listing.
- Read the complete verified profile facts and resume text. Posting qualifications are ranking signals, not pre-submit rejection gates: answer required controls truthfully, submit when Mercor accepts those answers, and let Mercor decide eligibility.
- Do not impersonate interviews or assessments. The Japanese Evaluator's 14-minute camera/microphone `Domain Expert Interview` is completed and the application page reads `Your application has been submitted!`; reconcile the review result and never resubmit the same application.
- A new interview, assessment, camera, screen-share, or other person-bound step becomes candidate-local durable `needs_human`: finish reversible steps, notify the exact job/link/action once, then continue other candidates without waiting. CAPTCHA, auth failure, and ambiguous provider state are blockers rather than invented human work.
- Count earnings only from an authoritative Mercor Earnings/contract settlement read-back; never count views, invitations, estimates, or pending offers.

## Ready-to-submit automation

When a listing shows every required step complete, `100%`, any required interview completed/reused, and a visible `Submit application`, the 30-minute Mercor revenue-application owner submits every ready listing within the bounded scan. Before each click it durably claims the listing and reads back the submitted state; an existing claim, pending application, or ambiguous prior click is never retried.

## Reusable open-source macro loop

Treat this as a provider module for any operator, not as a shared account or a guaranteed-income machine. Each operator must supply their own resume/facts, Google/Mercor session, payment setup, Calendar, interview/assessment completion, capacity, locales, and exclusions. Keep those inputs in the operator's private XDG state root; never commit them or reuse another operator's credentials.

The loop owns recurring discovery, ready-form submission, Gmail/Calendar reconciliation, reminders, evidence, duplicate protection, and settled-earnings accounting. It does not impersonate interviews, assessments, or paid work where Mercor prohibits AI/automation. `$10K verified` means three consecutive cycles of actual settled payouts, not an offer or an estimated capacity.

The execution style is model-led: observe the live page, reason about the next action, and adapt to page drift. Keep deterministic code small and boundary-focused—owned browser session, domain allowlist, lease, pre-effect claim, read-back, evidence, and ledger. Do not turn every possible UI branch into a brittle script.

## Calendar policy

- Reuse `apps/job-search-loop/job_search_loop/interview_scheduling.py` and `calendar_sync.py` for every Mercor interview, regardless of locale.
- Classify the Gmail/Mercor thread, require explicit start/end/timezone, check Calendar FreeBusy, and create one idempotent private event with prep reminders.
- Human glue is limited to authorization, ambiguous scheduling, attending the interview, and human-bound assessments. Never impersonate an interview.

## Loop contract

The 30-minute `mercor-revenue-application` owner is the sole Mercor Apply loop. Do not create a second Mercor executor or merge its business identity into Job Hunter. It consumes shared marketplace Apply policy/reporting; its adapter owns only Mercor auth, DOM, provider state, mutation and official readback. It keeps person-bound gates candidate-local and continues the scan.
