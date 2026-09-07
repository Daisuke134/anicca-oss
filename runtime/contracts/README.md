# Common loop contracts

These wire contracts are the boundary shared by local JSONL/SQLite and cloud Postgres adapters.
They do not choose a storage engine and do not move an existing database.

- Scheduler declaration: `runtime/loop/loop.schema.json`
- Runtime work item: `runtime/contracts/common-record.schema.json#/$defs/Job`
- Runtime event: `runtime/contracts/common-record.schema.json#/$defs/RuntimeEvent`
- External effect: `#/$defs/Effect`
- Verified receipt: `#/$defs/Receipt`
- Durable notification outbox item: `#/$defs/OutboxItem`
- Financial Manager ledger record: `#/$defs/FinancialRecord`
- Browser target lease: `#/$defs/BrowserTargetLease`

An entrypoint exit proves only a runtime event. An external effect becomes true only through a
provider-backed `Receipt`. Outbox delivery uses `message_key` as its retry identity. Financial
records keep non-negative minor-unit amounts; `direction` carries the sign, while `scope` and
`kind` prevent a personal balance, internal transfer, business revenue, cost, and payout from being
silently aggregated as the same thing. `verification.status=verified` requires evidence references.
Every FinancialRecord identity is deterministic:
`record_id = "financial:" + sha256_utf8(subject_id + "\n" + idempotency_key)`.
The JSON Schema enforces the resulting shape; `projectFinancialRecord` enforces this cross-field
hash invariant before either local or cloud persistence.

`BrowserTargetLease` is the portable record shape, not an authorization validator. The runtime
adapter must also verify that the target ID matches the WebSocket path, timestamps are ordered,
the CDP origin and provider URL are allowed, credentials are absent, and the owner token plus
generation still match current storage before any heartbeat, mutation, close, or release.
Connector's adapter is the first implementation of those operational checks; Gig and Job Hunter
follow only after their live target ownership is read back.

Existing domain rows remain in their current files and databases. ARCH-08 adapters translate them
to these records at read/write boundaries; they must not rewrite historical evidence in place.
An outbox row in `delivery_uncertain` is never blindly returned to `pending`; a provider readback
must reconcile it to delivered or a safe retry decision.
