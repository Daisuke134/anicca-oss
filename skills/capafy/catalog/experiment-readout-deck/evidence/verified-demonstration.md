# Experiment Readout Deck — verified demonstration

## Concrete input

```text
Decision: Should we extend the new onboarding checklist to all new accounts?
Decision owner: Maya. Review: Friday.

Control (no checklist): 120 invited; 30 completed activation within 7 days.
Variant (checklist): 118 invited; 41 completed activation within 7 days.
Success criterion: an activation-rate improvement of at least 5 percentage points
with no reported increase in support contacts.
Known limitation: device type was not recorded. We have no support-contact count yet.
Qualitative notes: two variant users said the checklist made the next step clear;
one control user said they did not know where to start.
```

## Actual output

```text
DECISION BRIEF
Decision: extend the checklist to all new accounts.
Observed result: the supplied activation rate is 25.0% for control (30 / 120)
and 34.7% for variant (41 / 118). Derived difference: +9.7 percentage points
(34.7% - 25.0%).
What this supports: the supplied result exceeds the stated +5 point criterion.
What it does not establish: causal certainty, performance by device type, or the
support-contact condition; device type and support-contact count are [UNVERIFIED].

EVIDENCE LEDGER
| Claim | Status | Evidence or formula |
| Control activation rate is 25.0% | DERIVED | 30 completed / 120 invited |
| Variant activation rate is 34.7% | DERIVED | 41 completed / 118 invited |
| Difference is +9.7 percentage points | DERIVED | 34.7% - 25.0% |
| Improvement clears the stated numeric criterion | SUPPLIED + DERIVED | criterion +5 points; derived +9.7 points |
| Support contacts did not increase | [UNVERIFIED] | no count supplied |
| Device-specific performance | [UNVERIFIED] | device type not recorded |

SEVEN-SLIDE OUTLINE
1. Decision requested — “Extend the onboarding checklist?”
   Takeaway: The numeric activation criterion is met in the supplied snapshot;
   the support condition remains open. Visual: decision gate with one green and
   one [UNVERIFIED] check.
2. Question and design — control versus checklist variant, each measured over
   seven days. Takeaway: 120 control and 118 variant invitations were supplied.
   Visual: two-column design card.
3. Observed evidence — 30/120 control completions and 41/118 variant completions.
   Takeaway: Keep counts beside rates. Visual: paired bars annotated with counts.
4. Comparison — 25.0% versus 34.7%; +9.7 points derived. Takeaway: The supplied
   numeric threshold is exceeded. Visual: rate comparison with formula caption.
5. Limits — device type and support contacts are [UNVERIFIED]. Takeaway: The
   snapshot alone cannot establish the open safeguard. Visual: evidence-gap list.
6. Options — extend after confirming support contacts; run a follow-up that records
   device type; or hold until both gaps are resolved. Takeaway: Maya chooses the
   acceptable uncertainty level. Visual: three-row option matrix.
7. Action register — Maya: obtain support-contact count before Friday; [TBD]:
   assign device-type instrumentation owner and date. Visual: owner/date table.
```

## Verification notes

- All figures in the output trace to the concrete input. The three displayed
  rates and difference use only the formulas shown beside them.
- The absent support-contact count and device segmentation remain explicitly
  `[UNVERIFIED]`; the output does not infer that either condition is satisfied.
- The result offers options and an action register but does not decide to extend
  the checklist for Maya.
- The output is structured text for a buyer-selected presentation tool, not a
  presentation file.
