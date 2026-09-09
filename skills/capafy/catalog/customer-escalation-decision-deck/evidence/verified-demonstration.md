# Customer Escalation Decision Deck — verified demonstration

## Offline selection notes

This candidate was selected without a Capafy API or UI call. The local inventory snapshot
`capafy-sales-ranking.json` (observed 2026-09-08) records the official seller winner as
`Slide Maker — Any Content Into a Styled Deck`, `sales_usd: 9.99`, and
`sku_type: subscription_week`. The release-owned `sales_selector.py` was evaluated only
against this recorded data and returned `signal: sales`; no remote state was read or
modified. A repository and local candidate-inventory text search found no existing
customer-support escalation or escalation-deck candidate.

## Concrete input

```text
Account: Northstar Health
Account context: 240 support agents use the export workflow.

Escalation timeline and evidence:
- 2026-08-18: Ticket #481 says the export took 45 minutes.
- 2026-08-20: The customer wrote that a scheduled report was delayed twice that week.
- 2026-08-22: Ticket #502 repeats the 45-minute export observation.
- 2026-08-23: Support told the customer that the team is investigating.

Known limits:
- No root cause has been confirmed.
- No workaround, delivery date, financial impact, or contractual remedy was supplied.

Stakeholders: Maya Chen (Customer Success), Omar Ruiz (Support).
Requested executive decision: Decide whether to assign a daily customer-update owner.
```

## Actual output

### Executive readout

- **Account:** Northstar Health; supplied context says 240 support agents use the export workflow.
- **Reported impact:** the customer reported a scheduled report was delayed twice in the week of 2026-08-20.
- **Observed events:** Tickets #481 and #502 each record a 45-minute export observation.
- **Decision required:** decide whether to assign a daily customer-update owner.
- **Unknowns:** root cause, workaround, delivery date, financial impact, and contractual remedy are not supplied.

### Slide-by-slide outline

| Slide | Takeaway | Supplied evidence | Visual suggestion |
|---|---|---|---|
| 1. Escalation at a glance | Northstar Health has reported repeated export-delay concerns that need an ownership decision. | Account context; 2026-08-20 customer statement; requested decision. | Account-and-decision summary card. |
| 2. What is observed | Two tickets record a 45-minute export observation. | #481 on 2026-08-18; #502 on 2026-08-22. | Two-point dated timeline. |
| 3. What the customer reported | The customer said a scheduled report was delayed twice that week. | Customer statement dated 2026-08-20. | Quoted-statement callout. |
| 4. What remains unknown | The supplied record does not establish a cause, workaround, date, financial impact, or remedy. | Known limits section. | Known-versus-unknown split. |
| 5. Decision and next action | Leadership must decide whether to name a daily-update owner; no owner has yet been supplied. | Requested executive decision; stakeholder list. | Decision box with `[TBD]` owner field. |

### Timeline

| Date | Event | Evidence status |
|---|---|---|
| 2026-08-18 | Ticket #481 records a 45-minute export observation. | supported |
| 2026-08-20 | Customer says a scheduled report was delayed twice that week. | customer-reported |
| 2026-08-22 | Ticket #502 repeats a 45-minute export observation. | supported |
| 2026-08-23 | Support says the team is investigating. | supported |

### Evidence and decision register

| Item | Status | Supplied basis | Owner / date |
|---|---|---|---|
| Exports took 45 minutes on two recorded occasions. | supported | Tickets #481 and #502 | No owner or date supplied |
| Scheduled report was delayed twice in one week. | customer-reported | Customer statement, 2026-08-20 | No owner or date supplied |
| An export defect caused the delay. | [UNVERIFIED] | No confirmed root cause supplied | [TBD] |
| A workaround exists. | [UNVERIFIED] | No workaround supplied | [TBD] |
| Assign a daily customer-update owner. | decision required | Requested executive decision | [TBD] |

### Questions before a commitment

1. Who will own a daily update if leadership approves one?
2. What evidence confirms or rules out a root cause?
3. Is there a verified workaround and who has validated it?
4. Is a delivery date, financial impact, or contractual remedy relevant? None was supplied.

## Verification notes

1. Every account fact, date, ticket number, count, stakeholder name, and requested decision appears in the concrete input.
2. The 45-minute observations are tied only to Tickets #481 and #502; the output does not generalize them into a confirmed incident cause.
3. The customer’s delay statement is labeled `customer-reported`, not independently confirmed.
4. Root cause, workaround, delivery date, financial impact, contractual remedy, owner, and due date remain unknown or `[TBD]` because the input does not supply them.
5. The output is structured text only. It makes no claim to create, export, send, or update a presentation or any external system.
