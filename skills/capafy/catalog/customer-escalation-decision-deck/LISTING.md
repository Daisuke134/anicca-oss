Primary Model: Claude Sonnet 4.6 · category: 生産性 · tags: customer escalation, decision deck, account management

## Offline selection notes

The current local `capafy-sales-ranking.json` snapshot (observed 2026-09-08) records
the official seller winner as `Slide Maker — Any Content Into a Styled Deck`, with
`sales_usd: 9.99` and `sku_type: subscription_week`. The release-owned
`sales_selector.py` was evaluated offline against that recorded official winner and
returned `signal: sales` with the advice to build the next skill in the same
customer-job category/style. Current repository and local candidate-inventory searches
found no customer-support escalation or escalation-deck candidate. This candidate uses
the proven structured-deck format for the distinct recurring job of turning a pasted
customer escalation into an evidence-linked decision brief.

## Renewal reason

Each escalation cycle has a different account, timeline, evidence set, decision, and
stakeholder group. A prior brief becomes stale when new tickets, customer statements,
or leadership decisions are supplied.

## Unit economics

The weekly and monthly ladder follows the observed high-value deck band recorded in
the repository playbook. It is a paid subscription with a bounded message cap; every
plan is explicitly `No Free Trial`.

| cycle | price | cap | trial |
|---|---:|---:|---|
| week | $9.99 | 20 | No Free Trial |
| month | $24.99 | 60 | No Free Trial |

## Title
Customer Escalation Decision Deck

## shortDescription
Turn pasted customer-escalation facts into an evidence-linked decision-deck outline. Separate reported impact from unverified causes, expose missing commitments, and give leaders a clear decision and action register.

## welcomeMessage
👋 Paste the account context, timeline, ticket or incident facts, customer statements, current commitments, stakeholders, and decision needed. I’ll turn only that material into an evidence-linked escalation deck outline.

Example: “A strategic customer reports repeated export delays. Here are the dated tickets, their stated impact, our current commitments, and the executive decision we need.”

## detailedDescription
✨ **A clear leadership brief when an account needs attention**

Paste the facts your team has about a customer escalation: account context, dated events,
ticket or incident notes, customer statements, known impact, stakeholders, current
commitments, and the decision required. The Agent turns only that material into a
concise deck outline for your own presentation tool.

⚙️ **How it works**

1. Separates supplied facts, customer-reported statements, and unconfirmed interpretations.
2. Orders only the dates and events you provide into an escalation timeline.
3. Maps material claims to supplied evidence and labels gaps `[TBD]` or `[UNVERIFIED]`.
4. Frames the requested decision with options, trade-offs, owners, and dates only when supplied.
5. Produces a leadership narrative that keeps confirmed impact separate from alleged cause or promise.

📦 **Every escalation brief includes**

- Executive readout: account, reported impact, decision, and unknowns
- Slide-by-slide outline with takeaway, evidence, and visual suggestion
- Dated timeline and evidence ledger
- Decision and action register with supplied owners and due dates
- Questions to resolve before making a commitment or root-cause statement

💡 **What makes it different**

- It turns raw escalation material into a decision format rather than a generic summary.
- It makes missing evidence and contradictory statements visible before leaders act.
- It returns structured text for your own presentation tool; it does not create or export presentation files.
- **Honesty first:** it does not browse, retrieve information, invent causes, forecast renewal risk, or make commitments for your team.

👤 **Built for**

Customer-success leaders, support leaders, account teams, product managers, and
executives preparing recurring customer-escalation reviews.
