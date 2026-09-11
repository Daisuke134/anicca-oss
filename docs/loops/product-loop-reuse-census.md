# Product Loop reuse census

This is the current, evidence-based map for the fourteen Product Loops. It is a
portability ledger, not a claim that every registered process or provider effect is
healthy. Runtime health comes from `lm-loop`; business success comes from an official
provider receipt.

Local/self-hosted and Cloud/hosted are host choices for this catalog. They must not
fork the business implementation. The shared contract is loop lifecycle + agent
runner + domain kernel + provider adapter + effect receipt + Telegram + CFO. Only
supervision, secret storage, durable state and browser transport may vary by host.

| # | Product Loop | Repository-owned lifecycle/domain path | Provider-specific boundary | Reuse and portability status |
|---:|---|---|---|---|
| 1 | Gig — Coconala | `skills/earn/gig-platforms/`, registry `hf-gig-*` owners | Coconala browser/readback | Active separate owner; converge through `skills/_shared/marketplace-core` in ARCH-13e |
| 2 | Gig — Lancers | `skills/earn/gig-platforms/lancers/`, registry `lancers-revenue-*` owners | Lancers browser/readback | Active separate owner; same ARCH-13e marketplace contracts |
| 3 | Gig — CrowdWorks | `skills/earn/gig-platforms/crowdworks/`, registry `crowdworks-revenue-*` owners | CrowdWorks browser/readback | Active separate owner; same ARCH-13e marketplace contracts |
| 4 | Writer | `skills/writer-agent/` | publisher/browser adapters | Shared runtime/state/Telegram migration exists; provider contracts need final Local/Cloud parity census |
| 5 | Affiliate | `skills/affiliate/` | affiliate network/browser adapters | Repository-owned source and shared browser/Telegram paths exist; public guided setup remains incomplete |
| 6 | Investment | `skills/alpaca-investment/` | Alpaca API/readback | Same repository implementation serves local and hosted execution; explicit paper/shadow/live authority remains mandatory |
| 7 | Agent Economy | `skills/earn/x402-sell/`, Agent Economy registry owners | wallet, x402 and compute/shelter providers | Repository-owned economic ledgers and adapters are being completed by their separate owner; never borrow the human wallet |
| 8 | Job Hunter | `skills/job-search/` | job-site browser, Gmail and provider readback | Shared browser/runtime exists; guided public setup and Cloud adapter coverage remain incomplete |
| 9 | Fundraiser | `skills/fundraiser/` | accelerator/grant/investor provider readback | Repository-owned loop exists; guided public setup and Cloud execution proof remain incomplete |
| 10 | Connector | `skills/connector/` and `apps/life-manager` adapters | event provider, Calendar readback | Repository-owned native loop exists; clean-user setup and hosted parity remain incomplete |
| 11 | Self-Build / Product Improvement | `skills/life-manager/self-build-daily.sh`, `apps/life-manager/scripts/life-manager-dev-daily.js` | source host, CI and review provider | Repository-owned owners exist; one reviewed feedback-to-change contract and public setup remain to converge |
| 12 | Mobile App Loops | `apps/life-manager/scripts/`, `apps/life-manager/lib/`, `skills/earn/marketing-engine/` | Postiz/native social, App Store Connect, RevenueCat | Eighteen repository-owned marketing/distribution jobs exist. App creation, Xcode source ownership, build/sign/upload and iteration are not yet one verified E2E loop |
| 13 | Capafy | `skills/capafy/` and marketing-engine shared services | Capafy product/publication providers | Repository-owned product and marketing owners exist; reuse and clean-user Cloud/Local setup need final census |
| 14 | CFO | `skills/cfo/` and shared financial events | Stripe, Moneytree and other financial-source adapters | Aggregation contract exists; each source needs provider receipt and Local/Cloud secret/state adapters without duplicating CFO logic |

## Mobile App and ebook evidence

The registered Mobile App jobs currently resolve to shared repository runners such
as `anicca-larry-ja-canary.js`, `honne-ja-cycle.js`,
`honne-en-cycle.js`, and `anicca-obou-instagram-canary.js`. These generate and
distribute marketing content and write job/Telegram evidence. They do not prove that
the submitted Xcode projects live in this repository or that account creation,
building, signing and App Store upload are automated.

The ebook products are separate product packs that reuse the marketing engine:

```text
skills/earn/marketing-engine/
├── ebook_runner.py
├── registry/ebook-packs/
│   ├── ebook-ja-watercolor.json     # watercolor-monk; obou_anicca
│   └── ebook-en-anicca-monk.json    # HeyGen Avatar IV; monk_anicca / anicca_en
├── ebook-asset-packs/default-v1/    # redistributable manifest-pinned starter assets
├── render_eval/
│   ├── watercolor_candidate.py
│   └── heygen_candidate.py          # durable create intent + provider receipt/reconciliation
├── publish/
├── measure/
└── report/
```

The English pack selects the repository-owned HeyGen Avatar IV adapter. Executable
source, neutral captions/manuscripts and the SHA-verified `default-v1` starter pack
are all tracked here. The adapter records a durable effect intent before HeyGen
creation, resumes download from a stored provider video ID, and refuses an automatic
second create when delivery is uncertain. Credentials, provider sessions, generated
renders and per-user product state remain private host data outside Git. The protected
legacy monk factory is neither moved nor read as an executable or asset dependency.

The primary Mobile App onboarding does not ask for an app repository. Most users do
not have one. The loop creates a new product repository/workspace from the shared
factory and default redistributable assets, then owns build, submission, iteration,
marketing, measurement and financial reporting. An existing-project path is only an
optional import. Dais's current `anicca-products` and `honne-ai` repositories are
reference products and migration inputs, not hard-coded runtime dependencies.

## Target ownership shape

```text
Life Manager/
├── apps/                         product surfaces, including hosted web/Telegram
├── loops/<loop>/                 objective, orchestration, durable cursor
├── skills/_shared/<domain>/      shared domain kernels with 2+ real consumers
├── runtime/                      lifecycle, agent runner, receipts, Telegram, CFO
├── providers/<provider>/         provider effects and official readback
├── products/
│   ├── mobile-apps/<product>/    app source, product manifest, marketing manifest
│   └── ebooks/<product>/         immutable book/character assets and manifest
└── config/loop-registry.json     one lifecycle registry
```

Do not move files only to make this picture literal. A migration is complete when
the ownership boundary is true, executable source is repository-owned, private
mutable data is outside Git, and Local and Cloud call the same business contract.
