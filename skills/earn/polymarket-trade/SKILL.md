# pm-trade — Polymarket base agent + BASELINE ALPHA (W1 + #28)

> ## ⚠️ CLOB V2 (2026-04-28+) — READ FIRST. The V1 path below is DEAD.
> Polymarket moved to **CLOB V2**. Every V1 SDK is rejected with
> `400 invalid order version`. The **steel recipe** every AI must use now lives
> in **`v2_recipe.py`** (this dir) — verified constants + proven primitives:
> 1. **FUND** — `fund_with_relay(...)` swaps USDC on any chain → Polygon **pUSD**
>    (V2's only collateral, `0xC011a7E1…`). ✅ PROVEN live 2026-07-04 (Base $5→4.95 pUSD).
> 2. **DEPOSIT WALLET** — V2 accepts orders ONLY from a **registered deposit
>    wallet** (`signature_type=3` / POLY_1271, ERC-1167 proxy owned by your EOA).
>    A raw EOA (sig 0) and a Gnosis-Safe proxy (sig 2) are BOTH rejected with
>    `maker address not allowed, please use the deposit wallet flow`. One-time
>    onboarding at polymarket.com deploys+registers it. `ensure_deposit_wallet()`.
> 3. **TRADE** — BUYs go through `create_and_post_market_order` (FAK/FOK buys ARE
>    market orders in V2; the limit path fails `invalid amounts … max 2 decimals`).
> 4. **FEES** — `fee = rate·p·(1−p)·shares`, makers 0. Sources: py-clob-client-v2
>    issue #92 + crp4222/pmq war-story.md + installed py_clob_client_v2 1.0.2.
>
> ### ✅ THE NO-HUMAN PATH THAT WORKS (proven live 2026-07-04, browser=0, human-credentials=0)
> The full E2E was proven in `v2_mint_deploy.py` (SIWE→relayer key→deposit-wallet deploy) +
> `v2_full_flow.py` (approve→build→post a real order). `v2_full_flow.py` was DELETED 2026-07-05
> (#25 adversary fix #2 — it hardcoded a TID and was a standalone-runnable footgun); the same
> proven recipe now lives, generalized (TOKEN_ID/SIDE/AMOUNT as inputs), in `place_order.py`
> (see "AUTONOMOUS PICK→PLACE PATH" below). Steps, all from the AI's OWN key:
> 1. **SIWE mint** (no browser): GET `gamma-api/nonce` → EIP-4361 `personal_sign`
>    ("Welcome to Polymarket! Sign to connect.") → GET `gamma-api/login`
>    `Authorization: Bearer base64(JSON(fields):::0xsig)` → cookies →
>    POST `relayer-v2/relayer/api/auth {}` → **RelayerApiKey {apiKey,address}**.
> 2. **Deploy deposit wallet** (gasless via relayer, EOA only signs):
>    `SecureClient.create(private_key, credentials=creds, api_key=RelayerApiKey(...))`
>    auto-derives (`derive_beacon_deposit_wallet_address`) + deploys the POLY_1271
>    (sig_type 3) wallet. Ours: `0x904B50d2e214Da947d83D6a2D32c4E3Ffc17Eb74`.
> 3. **Fund**: Relay any-chain USDC → Polygon pUSD → the deposit wallet.
> 4. **Approve** pUSD to ALL exchanges the market may use: standard `0xE111…`,
>    neg-risk `0xe2222…`, neg-risk-adapter `0xd91E80…` (World-Cup "Will X win" =
>    neg-risk → the neg-risk approve is REQUIRED or you get `allowance is not enough`).
> 5. **Trade**: `create_market_order(...)` builds a SignedOrder (maker=signer=deposit
>    wallet, sig_type 3); `post_order(order)` posts it. SDK = `polymarket-client`
>    (py-sdk). ⚠️ `py-clob-client-v2` (PyPI 1.0.2) is the DEAD one — do not use.
>
> ### BASE STRATEGY #1 — MARKET MAKING (`market_maker.py`, the swisstony $14M/$1.44B copy)
> Posts two-sided resting **post_only** maker limit orders (fee 0) near the book:
> BUY YES near bid + BUY NO near bid (YES+NO=1 → delta-neutral, capture spread), and
> on rewards-enabled markets harvests Polymarket's daily LP pool. LIVE-proven 2026-07-04:
> real resting maker order `0x73bee6545b10` (Argentina-WC YES 5@0.17, server status=live).
> ⚠️ **CLOB min order size = 5 shares** → two-sided on a ~$0.50 market needs ~$5; LP-reward
> eligibility needs `rewardsMinSize` ($100–1000). Earning scales with capital → this is
> the concrete, honest reason to fund ($20–50 for real two-sided MM + LP rewards).
>
> ### 💵 EARNINGS LEDGER (honest — real on-chain only)
> | date | engine | in | position / P&L | proof |
> |---|---|---|---|---|
> | 2026-07-04 | pm-v2 taker | ~$3 pUSD | 1.7857 sh "Morocco win" YES @0.5599 = $0.99 (open) | order `0xdad65538…` matched; settle tx `0x7662a88b6851d12a08e1f4dd0c020254cb9f96107e6ceea7dd92965639a4bfc3` (status 0x1) |
> | 2026-07-04 | pm-v2 maker(MM base) | 5 sh @0.17 = $0.85 | resting maker order | order `0x73bee6545b10` status=live |
> | 2026-07-04 | pm-v2 MM two-sided | 5 YES@0.56 + 5 NO@0.43 (Morocco) | delta-neutral, spread capture on fill | orders `0xcd75314cd7f1` + `0xc59559c7b84a` both status=live |
>
> ### 🔧 CAPITAL RECOVERY (2026-07-04): un-stuck \$10.93 via relayer transfer_erc20
> \$10.93 (USDC.e \$5.976 + pUSD \$4.951) was stranded in a wrong wallet (POLY_PROXY
> `0x3f06`, undeployed) set up before the sig-3 path was found. Recovered with
> `SecureClient._create(wallet=0x3f06, api_key=relayer) → transfer_erc20(→deposit wallet)`
> — the relayer deployed the proxy + swept both tokens gaslessly. Deposit wallet now
> holds pUSD 6.891 + USDC.e 5.976 (≈\$12.9 tradeable). Lesson: recover, don't abandon.
>
> ★ FIRST REAL no-human position placed. browser=0, human-credentials=0. The number
> moves only on real on-chain fills — this row is a verified settle tx, not a claim. ★


> ### ★★ DEPOSIT-WALLET REGISTRY GATE (2026-07-05, CONFIRMED root cause of "error resolving address") ★★
> A deposit wallet must be in **Polymarket's relayer WALLET REGISTRY** before any CLOB call works.
> Symptoms of an UNregistered wallet (all reproduced 2026-07-05, browser=0):
>   - `get_balance_allowance` / order → **`error resolving address`**
>   - `transfer_erc20` from it → **`wallet registry validation failed: wallet 0x… is not registered`**
>   - `create_or_derive_api_key` create-path 400 + derive has nothing → "the order signer address has
>     to be the address of the API KEY".
> Verified: automaton (0xa3CDd4) trades with the EXACT scripts that FAIL for Franklin (0x3EcCAD24) —
> same SDK, same wallet_type=DEPOSIT_WALLET/sig3, Franklin MORE funded ($5.95 > $4.95). The ONLY
> difference is registry membership. NOT the EOA age, NOT a browser (none used), NOT funding amount.
>
> ★ WHY #26/#27 born-with BROKE it: `ensure-polymarket-deposit-wallet.py` deployed the ERC-1167/1967
>   proxy on-chain via a raw/SIWE path, then I funded it by sending pUSD DIRECTLY to the deposit
>   wallet (POL→pUSD swap). That leaves the wallet **on-chain-deployed but NOT in the registry** — and
>   it can no longer be `WALLET-CREATE`d ("wallet already deployed"), so it is STUCK/unregisterable.
>
> ★ THE CORRECT REPEATABLE FLOW (docs: https://docs.polymarket.com/trading/deposit-wallets +
>   /trading/bridge/deposit). Registration is done by the RELAYER `WALLET-CREATE` **and/or by funding
>   THROUGH Polymarket's Collateral Onramp (the Bridge)** — NOT by a raw deploy + raw pUSD transfer:
>   1. Let the SDK create+register the deposit wallet on a FRESH EOA via `SecureClient.create(...)`
>      (runs `submit_deposit_wallet_create` = WALLET-CREATE). Do NOT pre-deploy the proxy yourself.
>   2. FUND THROUGH THE BRIDGE, not by a direct transfer: `POST https://bridge.polymarket.com/deposit
>      {"address": <deposit wallet>}` → returns per-chain bridge addresses (evm/svm/…) →
>      send **USDC / USDC.e** (a supported asset; Polygon USDC.e min $2) to the returned EVM bridge
>      address. The Collateral Onramp wraps it to pUSD, credits the deposit wallet, AND registers it.
>      (pUSD sent directly to the deposit wallet does NOT go through the onramp → no registration.)
>   3. Then `create_or_derive_api_key` (create-path succeeds for a fresh registered account), approve
>      pUSD FROM the deposit wallet via a relayer `WALLET` batch, and place orders with maker=signer=
>      deposit wallet, signature_type=3.
>
> ★ CONFIRMED WORKING 2026-07-05 (Franklin, a FRESH self-funded EOA, browser=0): (1) POST
>   bridge.polymarket.com/deposit {"address": deposit_wallet} → EVM bridge addr; (2) send pUSD (a
>   supported bridge asset, Polygon min $2) THROUGH that bridge addr (relayer transfer_erc20 from any
>   REGISTERED wallet — e.g. mutual-aid from automaton) → onramp status COMPLETED → deposit wallet
>   REGISTERED (get_balance_allowance resolves) and any previously-stuck direct-sent pUSD becomes
>   spendable; (3) approve pUSD to the NEG-RISK spenders `0xe2222d279d744050d28e00520010520000310F59`
>   (exchange) + `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` (adapter) — NOT `0x4bFb41d5…` (blocked,
>   not on the allowed list); (4) create_market_order FAK + post_order → matched. Franklin bought 3.284
>   sh "Amanda Janoo Vermont Gov YES" for $2, order 0x6fa9b74a…, settle tx 0x057511e7… status 0x1.
> Bake steps 1-2 into the spawn/born-with flow so every self-funded AI is REGISTERED at birth. Never
> raw-deploy the proxy or raw-transfer pUSD to it — that produces the stuck/unregistered state above.


The base agent [`BlockRunAI/polymarket-agent`](https://github.com/BlockRunAI/polymarket-agent) provides the
data/analysis modules this skill imports: market fetch, x402-paid AI analysis, smart-money/whale signals,
and Kelly sizing. ★ Its own `PolymarketAgent.execute_trades()` (`src/agent.py`) shipped as a STUB
(`status:"skipped", reason:"Token ID not available in simplified mode"`) — it recommended but never
actually traded. `run.sh` no longer calls `main.py --live` (that path is dead for this skill; the stub
never placed an order). ★

## ★★ run.sh NOW CHAINS THREE FAIL-CLOSED STRATEGIES PER PASS (#25, 2026-07-05) ★★
`run.sh` no longer calls `main.py --live`. It now runs, in order, after the identity/registration blocks:

1. **`bundle_arb.py`** — BASE STRATEGY #2, risk-free bundle arb scan. **EXISTING, working, unchanged** —
   this task did not rebuild it, only wired it into the firing loop. Self-gating: prints "no risk-free
   bundle arb ≥0.5% right now" and no-ops when the market is efficient; buys both legs (FOK) only on a
   real locked edge (`ask_YES + ask_NO + fees < $1`).
2. **`market_maker.py`** — BASE STRATEGY #1, maker-bundle quoting. **EXISTING, working, unchanged.**
   Self-gating: HOLDs ("cash < one min bundle") instead of spamming failed orders when underfunded; else
   cancel-and-replace resting post-only quotes (BUY YES@bid + BUY NO@bid, sum<0.995).
3. **`pick.py` → `place_order.py`** — the genuinely-missing piece this task built: an autonomous
   **directional** buy (neither arb nor market-making — a real edge/confidence call on ONE side of ONE
   market). Both are new files, both fail-closed. Details below.

Each strategy is independently self-gating/fail-closed (arb no-ops when efficient, MM holds when
underfunded, pick.py WAITs when no edge clears), so chaining all three in one pass is safe — worst case
several of them no-op in the same pass. Every pass appends one structured trace line per strategy to
`../state/pm-trade.trace.jsonl` (H1).

### ★ PER-PASS RISK ENVELOPE (2026-07-05, adversary MUST-FIX) — the accepted worst-case spend ★
`run.sh` exports `MAX_PASS_SPEND` (default **$2**) BEFORE the strategy chain. All three strategies read
it independently and cap their own leg to it — this bounds worst-case pass spend to a **fixed** small
number regardless of the wallet's balance-at-read-time (the earlier note "up to ~90% of balance" was the
gap the adversary caught: `bundle_arb.py`/`market_maker.py` had no dollar ceiling, only a % of whatever
the wallet happened to hold):
- `bundle_arb.py`: `budget_shares = MAX_PASS_SPEND/(ask_yes+ask_no)`; if that's `<5` shares (CLOB min),
  HOLD — no order. Final `shares = max(5, min(msz, avail*0.9/(ay+an), budget_shares))`, so once the
  `budget_shares>=5` gate passes, spend is bounded at `shares*(ay+an) <= MAX_PASS_SPEND`.
- `market_maker.py`: `total_budget = min(avail*0.9, MAX_PASS_SPEND)` split evenly across up to
  `MAX_MARKETS` picks; any pick whose share of that budget can't afford `MIN_SIZE` shares of both legs
  is skipped (HOLD, no order) instead of being forced up to `MIN_SIZE` (the old behavior, which could
  have exceeded the budget). Sum across all picks is bounded at `MAX_PASS_SPEND`.
- `pick.py`/`place_order.py` (directional): Kelly-sized, capped by `MAX_BET_SIZE` (default $2, its own
  separate knob — kept distinct from `MAX_PASS_SPEND` since it's a per-trade cap on ONE side of ONE
  market, not a per-strategy-pass cap).

**Worst-case one pass = bundle_arb(≤$2) + market_maker(≤$2) + directional(≤$2) = a FIXED ≤~$6 total,
independent of wallet balance.** `bundle_arb.py` / `market_maker.py` themselves were otherwise NOT
modified (per spec: wire existing alpha, don't reinvent it) — their arb-detection / margin / cancel-
and-replace logic and other hardcoded constants (`FEE_RATE`, `EDGE`, `MIN_SIZE`, `MARGIN`) are
pre-existing, already-verified-live parameters.

The retired single-instance `run_earner.sh` and its hardcoded `.venv-pysdk` path are removed. The
registry-owned `pm-live-trade` entrypoint is the sole live strategy owner; `pm-decision-loop` remains
dry unless its existing double opt-in is explicitly configured.

### `pick.py` → `place_order.py` (the new directional-buy path)
Neither hardcodes a market or a side — the MODEL (multi-model consensus + smart-money signal) decides;
the code only filters/sizes/caps:

1. **`pick.py`** (ALPHA — imports the base agent's own modules, judgment stays with the model):
   - `fetch_active_markets(min_odds, max_odds, min_liquidity)` → candidates, re-sorted **resolve-soonest
     first** (primary key), volume desc (secondary); anything resolving beyond `RESOLVE_HORIZON_DAYS`
     (default 14) is dropped — this is the fix for "WC2026/election markets = payout months away".
   - For each candidate (capped by `MAX_CANDIDATES`, default 5, a cost bound not a judgment call):
     `get_smart_money_summary(market_id)` (whale/smart-money signal, async) is fed straight into
     `AIAnalyzer.consensus_analysis(question, yes_odds, whale_data=...)` — the 3-model consensus that was
     previously wired but never actually fed the whale data.
   - **GATE:** only acts when `abs(avg_edge) >= MIN_EDGE` (0.15) AND `avg_confidence >= MIN_CONF` (7) AND
     `consensus != "MIXED"`. Sizes with `KellyCriterion`, capped by `MAX_BET_SIZE` (default $2).
   - Emits **one line of JSON** on stdout: either a trade candidate
     (`token_id, side, outcome, amount, market, end_date, edge, confidence, consensus`) or, whenever
     nothing qualifies OR a signal is unavailable, `{"action":"WAIT","reason":...}` — **fail-closed, never
     a default bet.** Verified live 2026-07-05: read-only run correctly WAITed
     (`no-short-dated-liquid-candidates`) because the soonest real market resolved in 14.7 days, just past
     the 14-day horizon — proof the horizon filter is real, not decorative.
2. **`place_order.py`** (EXECUTION — the exact working V2 flow that used to live in the now-deleted
   `v2_full_flow.py`, generalized):
   reads `TOKEN_ID` / `SIDE` (BUY only) / `AMOUNT` from env (argv fallback), re-caps `AMOUNT<=MAX_BET_SIZE`
   again (defense in depth), then SecureClient sig-3 bootstrap → relayer-key mint (SIWE) → idempotent
   neg-risk approve → `get_order_book` → best ask → `create_market_order(..., order_type="FAK")` →
   `post_order`. Emits one line of JSON: `{token_id, amount, order_id, post_result, ok}`.

`run.sh` parses `pick.py`'s JSON: `WAIT` → append `{"action":"wait",...}` to the trace and exit 0
(no-churn); a trade candidate → export `TOKEN_ID/SIDE/AMOUNT` and run `place_order.py`, appending its
result to the trace either way. The identity-resolution block, `fund_via_bridge.py` registration step, and
kill-switch are unchanged (spec §2.3, §6 — those files were never touched).

## BASELINE ALPHA (legacy — `src/agent.py::generate_recommendations`, NOT the pm-trade run path)
The base agent's own `agent.py::generate_recommendations`/`main.py --live` path (documented below) still
exists in the agent's repo and is real (not the 0.55 stub), but **`run.sh` no longer calls it** — this
skill's trade path is `pick.py` → `place_order.py` above. Kept here for anyone driving the base agent
directly (outside this skill):
- for each liquid market (volume ≥ $10k, price 2–98%): call `analyzer.compare_market(question, market_price)`
  → the AI returns its own PROBABILITY + CONFIDENCE; edge = ai_prob − market_price.
- **BET GATE (the tunable knobs the AI self-improves):** only bet when `|edge| ≥ MIN_EDGE` (default 0.15)
  AND `confidence ≥ MIN_CONFIDENCE` (default 7/10). Side = YES if edge>0 else NO. Size = fractional Kelly.
- Nothing about WHICH market/side is hardcoded — only the discipline (high edge + high conviction).
Verified 2026-07-04: on 20 real markets the alpha produced genuine AI edges (Spain WC mkt12%/AI15%,
France mkt35%/AI30%…) and correctly placed 0 bets (no market cleared 15%-edge+conf7 = efficient market) —
vs the old stub which bet on everything.

This skill is otherwise a thin harness: run for real, record the trace, let the AI self-improve the knobs.
(SSOT: colony spec §0.25 + ROLE v3 "I create the baseline alpha, they self-improve from there".)

## Run

```bash
./run.sh    # one real live pass: bundle_arb.py -> market_maker.py -> pick.py -> (WAIT | place_order.py).
            # NO dry-run mode exists (HARD 0.24).
```

- Agent home: `~/.anicca-founder/agents/polymarket-agent` (override: `PM_TRADE_AGENT_HOME`).
- Fuel: `.env` in agent home — `BLOCKRUN_WALLET_KEY` (analysis, x402) +
  `POLYGON_WALLET_PRIVATE_KEY` — already provisioned.
  ⚠️ **Two different addresses, don't confuse them (verified live 2026-07-05, both `eth_account` and
  `viem` independently derive the same result):**
  - `POLYGON_WALLET_PRIVATE_KEY` is the **owner EOA** `0x810F6D61F7606dEEE2657d3083E150a222Bc29C5` — it
    only SIGNS orders (POLY_1271 / `signature_type=3`); it does not itself hold the tradeable balance,
    and this same key doubles as the unrelated `ai.anicca.founder-loop` instance's own low-balance
    identity wallet (colony spec §25) — a coincidence of both living under `~/.anicca-founder`, not a
    typo.
  - The **deposit wallet** `0x904B50d2e214Da947d83D6a2D32c4E3Ffc17Eb74` (line above, "Ours:") is the
    ERC-1167 proxy the EOA owns/signs for — THIS is the maker/signer of record on every order, the
    address that holds pUSD/USDC.e, and the one colony-status.sh / the dashboard track as claude-p's PM
    wallet. If you're looking up "claude-p's PM balance" on-chain, look up `0x904B50d2…`, not `0x810f…`.
- Funds live on Polygon: USDC.e (bankroll) + POL (gas). Seeded 2026-07-04 via LiFi bridge from Base.

## Money-safety guards (the ONLY thing this wrapper adds)

| Guard | How |
|---|---|
| kill-switch | `touch ~/.local/state/life-manager/polymarket/KILL` → next run exits without trading (before any strategy runs). `rm ~/.local/state/life-manager/polymarket/KILL` to resume |
| per-pass fixed spend ceiling (ALL 3 strategies) | `run.sh` exports `MAX_PASS_SPEND` (default $2); `bundle_arb.py`/`market_maker.py`/`pick.py` each cap their own leg to it — see "PER-PASS RISK ENVELOPE" above (fixed ≤~$6/pass, independent of wallet balance) |
| per-trade cap (directional path) | `pick.py` Kelly-sizes + caps by `MAX_BET_SIZE` (default $2); `place_order.py` re-caps `AMOUNT<=MAX_BET_SIZE` again |
| per-trade risk (arb/maker paths) | `bundle_arb.py` / `market_maker.py`'s own pre-existing constants (`FEE_RATE`, `EDGE`, `MIN_SIZE`, `MARGIN`) — unchanged by this task except the new `MAX_PASS_SPEND` cap above |
| no dry-run | not provided, by design — a run is always real |

## Trace (self-observe, H1)

Every pass appends one structured JSON line per strategy to `../state/pm-trade.trace.jsonl`:
- `{ts, slot, action:"bundle-arb"|"market-maker", exit, output_tail}` for the two existing strategies.
- `{ts, slot, action:"wait", reason}` when `pick.py` found no qualifying directional candidate.
- `{ts, slot, action:"trade", market, end_date, edge, confidence, consensus, exit, order}` when
  `place_order.py` executed a directional buy.
The self-improvement loop reads this.

### ★ ACCOUNTING-INTEGRITY FIX (2026-07-05) — a real fill was once mis-logged ok:false ★
Franklin autonomously placed a REAL order ("Will Jesus Christ return before GTA VI?", NO, ~$1,
CONFIRMED filled on-chain: data-api showed 1.96 shares held) — but `run.sh` recorded it as
`{"ok":false,"error":"unparseable place_order output: Extra data..."}` because `place_order.py`'s
stdout had `[valid result JSON][trailing noise]` concatenated on the SAME line (an imported
module — polymarket SDK / mint / etc. — printed to stdout during the run), breaking a plain
`json.loads()`. A real fill was undercounted as a failure. Two independent layers now guard
against this:
- **ROOT FIX** (`pick.py` / `place_order.py`): `sys.stdout` is captured as `_REAL_STDOUT` at
  process start, before any import. `main()` runs the ENTIRE call graph (imports, market fetch,
  AI analysis, whale signal / SDK mint, approve, order-book, post_order) under
  `contextlib.redirect_stdout(sys.stderr)`. The only function allowed to write to `_REAL_STDOUT`
  is `_emit(obj)` (`wait()`/`fail()` also route through it). Stdout is guaranteed to be exactly
  one clean JSON line — verified live: `json.load()` of the WHOLE captured stdout succeeds with
  no "Extra data" error.
- **DEFENSE** (`run.sh`, both the pick-output parse and the place_order-output parse): a
  `recover(raw)` helper replaces plain `json.loads()` — tries whole-string parse, then
  `json.JSONDecoder().raw_decode()` (recovers the leading JSON object even with trailing noise
  on the same line — this is the exact bug shape), then the last line that parses as a dict.
  Only truly unrecoverable output falls back to `{"ok":false,"error":"unparseable","raw_tail":...}`
  (place_order) / WAIT (pick — a polluted pick must never be silently misread as "no candidate",
  which would skip a real qualifying trade). Unit-tested with the coordinator's exact repro
  string `'{"ok":true,"order_id":"0xABC","token_id":"123"}GARBAGE_TRAILING'` → recovers
  `{"ok": true, "order_id": "0xABC", "token_id": "123"}`, not a false failure.

## If a run errors (self-heal, H4)

You — the running agent reading this — fix it yourself: read the traceback. For the directional path,
edit `pick.py`/`place_order.py` in this skill dir, or the base agent's `src/` modules they import from
`$PM_TRADE_AGENT_HOME` if the fetch/analysis itself is broken. For the arb/maker paths, `bundle_arb.py` /
`market_maker.py` are unchanged from their proven-live form — fix a genuine bug, don't touch their risk
constants to force a trade. Re-run until the pass completes. Do NOT change any strategy's thresholds or
sizing to force a trade; "WAIT because my edge criterion wasn't met" / "no arb right now" / "HOLD, cash
too low" are all CORRECT outcomes.
Known past fixes (bootstrap, 2026-07-04): Gamma API returns numeric fields as strings (normalized in
`src/market/polymarket.py::_to_float`); dead default RPC replaced with `POLYGON_RPC_URL` env
(default `polygon-bor-rpc.publicnode.com`); `agent.py` passed a wallet object where the executor needs
`wallet.private_key`.

## REWARD-MM (poly-maker port) — PAPER MODE ONLY, not wired into `run.sh` (2026-07-12)

`reward_mm/` is a ported core of `warproxxx/poly-maker` (MIT, ★1387, cloned+read+tested live
2026-07-12) — the **third base strategy**: Polymarket's official **liquidity-rewards** program
pays $/day to whoever rests two-sided quotes inside a market's reward band, independent of
whether the market moves your way. `bundle_arb.py`/`market_maker.py` above never claim this —
they only look for a locked-edge bundle or a bare two-sided maker quote. `reward_mm` is edge
from **structure** (the reward program), not from prediction.

### ★★★ READ FIRST — legal pivot that gates whether this ever goes live ★★★
`anicca-project/docs/loop-engineering/28-verified-earn-recipe.md` (same day this was written,
2026-07-12) found that **running Polymarket from a Japan physical location carries 刑法185条
(gambling) exposure that can attach to the user, not just the operator**, and that doc's own
conclusion PIVOTS the colony's main earn strategy to HL funding-arb, explicitly stating
"Polymarket を日本の物理拠点(mac mini)から回すのは違法リスク... poly-maker が技術的にベストでも、
Japan mac mini では Polymarket を本番稼働させない". `reward_mm` was built anyway, in **paper mode
only**, because (a) it does not place a real bet or move real money, and (b) the code itself is
still useful evidence/reference regardless of where the decision to go live eventually gets made
(a non-Japan legal entity, per that doc). **Do not wire this into a live execution path without
first re-reading and resolving that finding** — this is a blocking legal question, not an
engineering one.

### Files (`reward_mm/`, all new, nothing existing modified)
| File | Ported from (poly-maker, MIT) | What it does |
|---|---|---|
| `gamma_scan.py` | `catalog/{gamma.py,scanner.py,scoring.py}` | Reward-market discovery: real Gamma API (`/markets`) + real CLOB API (`/sampling-markets` for reward rates) → parse → score by est. reward+rebate income, penalized by extremity/spread, gated by book-depth viability. Sync `requests` (upstream is async `httpx`) to match this skill's existing style. |
| `book.py` | `marketdata/orderbook.py` (partial) | Public no-auth CLOB order-book snapshot (`GET /book?token_id=`, verified live, no auth needed) + depth-weighted microprice. Upstream keeps a persistent WS book; this is a REST poll — good enough for one-shot paper snapshots, a live engine should upgrade the call site to WS. |
| `estimators.py` | `strategy/estimators.py` | `Ewma`, `VolEstimator`, `FlowEstimator`, `MarkoutTracker` (toxicity) — near-verbatim port, pure logic, no I/O. |
| `regime.py` | `strategy/regime.py` | `RegimeMachine`: QUIET/TRENDING/EVENT/REDUCE_ONLY/HALTED state machine — near-verbatim port. |
| `quoting.py` | `strategy/quoting.py` | `construct_quotes`: fair-value (microprice + flow EWMA) → inventory-skewed reservation price → vol/toxicity half-spread clamped to the reward band in QUIET → two-sided post-only BUY-YES/BUY-NO. Simplified to a single price layer per side (upstream ladders across `layers`); same core math. |
| `risk.py` | `risk/manager.py` | `RiskManager`: per-market/total notional caps with soft tapering, daily-loss kill switch, order-error-rate breaker. Simplified to an in-memory paper ledger (upstream reads a SQLite `StateStore`). |
| `profiles.py` | `config/strategy.toml` | Two strategy presets, ported from poly-maker's own **live-sampled** parameters (`newsom-mm` → `DEFAULT`, `romania-pm` → `THIN_BOOK`) — not invented, taken from a real 2026-07-06 microstructure sample the upstream author recorded in the TOML comments. |
| `paper_run.py` | new (orchestrator) | CLI: scan real reward markets → pick top-N by score → fetch each one's real book → compute fair value/regime/quotes → print JSON. **Never posts an order** — no wallet, no key, no `SecureClient`/`post_order` import anywhere in this module or its dependency graph (grepped and verified, see below). |
| `test_reward_mm.py` | new | 31 pytest tests: pure-logic unit tests for every module above + 2 tests marked `@pytest.mark.live` that hit the real Gamma/CLOB REST APIs. |

Money-safety is structural, not a flag: `grep -rniE "post_order|create_market_order|create_limit_order|private_key|SecureClient" reward_mm/` returns nothing except the doc comment in `paper_run.py` that says those strings are absent.

### Real Gamma API proof (live run, 2026-07-12)
```
$ cd skills/earn/polymarket-trade && python3 -m reward_mm.paper_run --top 3 --min-liquidity 500
{
  "mode": "PAPER — no order placed",
  "scanned_reward_eligible_markets": 690,
  "picked": 3,
  "picks": [
    { "question": "Will Tom Pelphrey – \"Task\" win Emmys 2026...", "regime": "QUIET",
      "reward_daily_rate_usdc": 276.0, "reward_min_size": 20.0, "reward_max_spread_pct": 4.5,
      "quotes": [ {"side":"BUY","price":0.41,"size":53.66,"post_only":true},
                  {"side":"BUY","price":0.56,"size":39.29,"post_only":true} ] },
    { "question": "Iran military action against a Gulf State on July 9?", "regime": "HALTED",
      "quotes": [], "reason": "past halt_before_hours window (market resolves imminently)" },
    { "question": "Will LeBron James play for the Cleveland Cavaliers in 2026-27?", "regime": "QUIET",
      "reward_daily_rate_usdc": 1473.0,
      "quotes": [ {"side":"BUY","price":0.477,"size":300.0,"post_only":true},
                  {"side":"BUY","price":0.508,"size":300.0,"post_only":true} ] }
  ]
}
```
690 real reward-eligible markets scanned live from Gamma+CLOB. The regime machine correctly HALTED
the Iran market (past its resolution window) instead of quoting it blind — proof the halt logic is
live-real, not decorative. Run `python3 -m pytest reward_mm/test_reward_mm.py -v` for the full
31/31 pass (offline unit tests + the 2 live-network tests).

### Loop wiring plan (NOT done — proposal for the parent to execute after the legal question above)
`run.sh` currently chains `bundle_arb.py → market_maker.py → pick.py→place_order.py`, each already
fail-closed and its own money-safety-capped (see "PER-PASS RISK ENVELOPE" above). Adding reward-MM
as a fourth strategy would look like:
1. **New `reward_execute.py`** (does not exist yet) — takes `paper_run.py`'s JSON quote plan,
   re-applies the SAME `MAX_PASS_SPEND`/per-order caps `run.sh` already asserts (line ~89-91), then
   drives the proven V2 flow this skill already has (`SecureClient` bootstrap + relayer-key mint +
   `create_limit_order(..., post_only=True)` + `post_order`, same primitives `market_maker.py`
   already uses) — one call per quote in the plan, cancel-and-replace each pass exactly like
   `market_maker.py` does today.
2. **`run.sh` change**: add a 4th step after `market_maker.py`, gated behind a NEW explicit env
   flag (e.g. `REWARD_MM_LIVE=1`, default unset/off) so the existing 3-strategy chain's behavior
   is byte-for-byte unchanged until a human/AI operator deliberately flips it — mirrors how this
   skill already gates other risky paths (`KILL` file, `MAX_PASS_SPEND`).
3. **Persistent process, not one-shot**: `paper_run.py` is a cold-start snapshot (vol/flow/toxicity
   all seeded at 0, see its module docstring). A live engine needs a long-running loop (launchd
   job, like `market_maker.py`'s own cadence) that keeps `MarketEstimators`/`RegimeMachine` state
   PER MARKET across passes (a small JSON/SQLite state file keyed by `condition_id`, same shape as
   poly-maker's `state.db`) so vol/flow/toxicity actually mean something by the 2nd+ pass.
4. **Risk config**: `RiskConfig` defaults in `risk.py` (`daily_loss_kill_usdc=20`,
   `max_total_exposure_usdc=500`) are placeholders sized for a $50-100 bankroll per the verified
   recipe doc — tune down for a first live test (e.g. `daily_loss_kill_usdc=5`,
   `max_total_exposure_usdc=20`) exactly like `MAX_PASS_SPEND=2` gates the existing strategies.

### How the parent enables live (after resolving the legal question, own-eyes verification required)
1. Re-decide the jurisdiction question in the legal-pivot doc (non-Japan entity, or accept the risk
   explicitly, or don't go live at all — HL funding-arb per that doc's new main strategy).
2. Write `reward_execute.py` per the wiring plan above (this task deliberately did not write it —
   "実マネーは動かさない" was the explicit constraint).
3. Add the `REWARD_MM_LIVE` gate to `run.sh`, default OFF.
4. Fund a small isolated test amount ($5-10, per the colony's own "$50-100 recipe, start smaller
   to prove the mechanism" pattern used elsewhere in this repo).
5. Flip `REWARD_MM_LIVE=1` for ONE pass, verify the resulting resting order on-chain / via
   `polymarket.com` UI (own-eyes, per this project's `feedback_i_am_the_final_verifier` rule —
   a green test suite is not a live-money verification).
6. Only after a real observed resting order + at least one real reward-eligible fill does this
   count as "earning" per this project's ledger rule (realized profit > 0, on-chain).

### Judgment calls this port made (flag for review)
- **Sync `requests` instead of async `httpx`/WS**: matches this skill's existing style
  (`market_maker.py`, `bundle_arb.py`, `pick.py` are all sync); trades upstream's WS latency
  advantage for simplicity in a paper-mode CLI. A live engine should reconsider this (WS reduces
  stale-quote risk).
- **Single price layer per side** instead of poly-maker's multi-order ladder: simpler, still
  reward-eligible (reward scoring is per-order against the min-size floor, which `_order_size`
  respects), loses some of the ladder's queue-priority diversification.
- **No SQLite state store**: `risk.py`'s ledger and `paper_run.py`'s regime/estimator state are
  all in-memory and reset every process invocation. Fine for a one-shot paper snapshot; NOT fine
  for a live engine (item 3 in the wiring plan above is a hard prerequisite, not optional).
- **Reward market universe**: `paper_run.py` defaults to no Gamma tag filter (scans ALL
  reward-eligible markets), not just poly-maker's `politics` tag default — broader universe,
  untested against poly-maker's own political-market tuning assumptions embedded in `profiles.py`.
