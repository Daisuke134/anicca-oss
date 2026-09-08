# Verified demonstration

## Concrete input

```text
Reporting period: Q2 2026. Audience: board of directors.

Priority: reduce time to first completed workspace.

Supplied metrics:
- 4,800 new workspaces were created in Q2; 2,160 completed setup within seven days.
- Q1 had 4,500 new workspaces; 1,800 completed setup within seven days.
- Median time to completed setup was 2.8 days in Q2 and 3.6 days in Q1.
- The product team ran two onboarding experiments. No causal result is supplied.

Supplied accomplishments:
- Released a setup checklist on 14 May 2026.
- Added an in-product prompt that points to the checklist.

Supplied risks:
- The completion metric excludes workspaces created through the partner-import flow.
- A data-quality review for that flow has no owner or date yet.

Board decision requested: approve one additional analytics contractor for the
partner-import measurement work. Do not add a budget, forecast, revenue claim,
or any decision not written above.
```

## Actual output

### Board headline

Q2's supplied operating update shows more new workspaces and more seven-day
setup completions than Q1, alongside a lower supplied median time to completed
setup. The supplied material does not establish that either onboarding experiment
caused the change.

### Metric ledger

| Metric | Q2 supplied value | Q1 supplied value | Comparison treatment | Gap or boundary |
|---|---:|---:|---|---|
| New workspaces | 4,800 | 4,500 | Q2 is 300 higher in the supplied counts. | Partner-import flow is a separate scope issue. |
| Completed setup within seven days | 2,160 | 1,800 | Q2 is 360 higher in the supplied counts. | Completion metric excludes partner-import workspaces. |
| Median time to completed setup | 2.8 days | 3.6 days | Q2 is 0.8 days lower. | No causal analysis was supplied. |
| Onboarding experiments | Two experiments ran. | [TBD] | Do not attribute metric changes to the experiments. | No causal result supplied. |

### Slide storyboard

| Slide | Purpose and title | Takeaway | Supplied evidence | Visual direction | Presenter note |
|---|---|---|---|---|---|
| 1 | Q2 setup-completion update | The update concerns the priority of reducing time to first completed workspace. | Q2 2026; stated priority. | Title card with the priority statement. | Keep the period and priority explicit. |
| 2 | Operating snapshot | Q2 supplied counts exceed the Q1 counts for new workspaces and seven-day completions. | 4,800 vs 4,500 new workspaces; 2,160 vs 1,800 completions. | Two paired count bars with each period labelled. | Describe the counts; do not state a cause. |
| 3 | Setup-time movement | The supplied median time fell from 3.6 days to 2.8 days. | 2.8 days in Q2; 3.6 days in Q1. | Two-value comparison card. | State the 0.8-day difference as arithmetic, not as an experiment result. |
| 4 | What changed in the product | Two supplied onboarding changes focused attention on the checklist. | Checklist released 14 May 2026; in-product checklist prompt; two experiments ran. | Timeline with the two release facts. | Do not infer effectiveness from timing. |
| 5 | Measurement boundary | The reported completion metric excludes partner-import workspaces. | Supplied exclusion; data-quality review lacks owner and date. | Scope box plus `[OWNER TBD]` and `[DATE TBD]`. | Make the measurement gap visible before any discussion of completeness. |
| 6 | Decision requested | The supplied board request is approval for one additional analytics contractor for partner-import measurement work. | Exact decision request. | Single decision card. | Do not add cost, hiring date, or expected outcome. |

### Decisions requested

- Approve one additional analytics contractor for the partner-import measurement work.

### Risks and open questions

- The completion metric excludes partner-import workspaces.
- `[OWNER TBD]` and `[DATE TBD]` for the partner-import data-quality review.
- The input supplies no causal result for either onboarding experiment.

### Traceability check

- Preserved: Q2 2026; 4,800; 2,160; 4,500; 1,800; 2.8 days; 3.6 days; 14 May 2026; two experiments; and the exact contractor decision.
- The Q2-minus-Q1 comparisons are arithmetic differences from supplied values.
- Added no budget, forecast, revenue claim, experiment result, owner, date, or board decision.
- Produced a text storyboard only; no presentation file was created.

## Verification notes

1. Every metric and date in the output appears in the concrete input; each arithmetic comparison is shown from its supplied pair.
2. The output retains the partner-import exclusion and labels the missing review owner and date rather than inventing them.
3. The two experiments remain non-causal because the input supplies no causal result.
4. The only decision in the output is the contractor approval explicitly requested in the input.
5. The demonstration is a local text artifact. It does not access company systems, validate records, create a presentation file, or change remote platform state.
