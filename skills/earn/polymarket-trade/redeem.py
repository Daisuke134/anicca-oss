#!/usr/bin/env python3
"""
redeem.py — collect (redeem) claude-p's RESOLVED, redeemable Polymarket positions,
turning open unrealized value into realized pUSD cash in the SAME deposit wallet.

WHY THIS FILE EXISTS
--------------------
Trading (v2_recipe.py / v2_full_flow.py) only OPENS positions. Nothing in this repo
ever collected a win — every position sat "redeemable=True" with real cash owed and
$0 actually realized. This is that missing last step: colony's first realized profit.

THE MECHANISM (verified against the installed SDK, not guessed)
-----------------------------------------------------------------
The deposit wallet (`0x904B50d2…`, ERC-1167 proxy, POLY_1271 sig_type=3) is NOT an
EOA, so it can't sign its own transactions — every write goes through Polymarket's
gasless relayer, signed by the owner EOA (`0x810F6D61…`). This is the exact same
relayer path `v2_mint_deploy.py` already proved live for wallet deployment: SIWE
mint (no browser) -> RelayerApiKey -> `polymarket.clients.secure.SecureClient`.

The pinned runtime SDK (`polymarket-client` 0.1.0b13) ships a native
`SecureClient.redeem_positions(condition_id=...)` (secure.py:2188). It already knows
how to pick the right on-chain path per market:
  - regular CTF market  -> `ConditionalTokens.redeemPositions` via the collateral
    adapter (`normalize_market_position_context`, positions.py:66-95)
  - neg-risk market      -> the neg-risk collateral adapter instead
by reading the market's OWN `negRisk` flag (fetched via `list_markets`) — this is
mechanical dispatch on a fixed on-chain fact, not a strategy judgment, so it is
correctly deterministic code (R5), same as picking a URL scheme.

TWO REAL SDK GAPS FOUND (verified live, fixed here, not worked around blindly):
  1. `redeem_positions()`'s own market lookup omits `closed=True`, and Gamma's
     `/markets` endpoint silently excludes closed/resolved markets unless that
     flag is explicit (verified: `curl gamma-api/markets?slug=X` -> `[]`,
     `...&closed=true` -> the market). Since redeem() only ever targets RESOLVED
     (closed) markets, the wrapper always raised "No market found". Fixed by
     calling the same low-level primitives with `closed=True` in the lookup
     (`redeem_condition`, below).
  2. Before ANY redeem, the deposit wallet must grant the adapter contract
     ERC-1155 `setApprovalForAll` on the raw ConditionalTokens contract — verified
     by `eth_call` simulation: redeeming without it reverts
     `"ERC1155: need operator approval for 3rd party transfers"`; querying
     `CTF.isApprovedForAll(deposit_wallet, collateral_adapter)` on-chain returned
     `False`. This is the one-time step Polymarket's own web UI does silently
     before a user's first redeem; `ensure_ctf_operator_approval` does it here,
     idempotently (checks `isApprovedForAll` first, only sends a tx if needed).
     It authorizes redemption only — it cannot move pUSD/USDC and is fully
     revocable (`approved=False`).

Sources (quoted, no guessing):
  - installed `polymarket` package, `clients/secure.py::redeem_positions`,
    `::approve_erc1155_for_all`, and
    `_internal/actions/relayer/positions.py::normalize_market_position_context`
    (ground truth — this IS what ships, not docs about it).
  - `v2_mint_deploy.py` (this dir) — the proven-live SIWE/RelayerApiKey mint flow,
    reused verbatim here via `_mint_relayer_api_key`.
  - data-api `https://data-api.polymarket.com/positions` — the source of which
    conditions are `redeemable=True` and how much they're worth right now.
  - live `eth_call` against Polygon mainnet (this session, 2026-07-05) — confirmed
    each conditionId's `payoutDenominator`/`payoutNumerators` on the raw CTF
    contract is set (i.e. genuinely resolved on-chain), and confirmed the missing
    ERC-1155 approval as the exact revert cause. Note: `CTF.getPositionId` shows
    the actually-held ERC-1155 token IDs are derived from **USDC.e** collateral
    (`0x2791Bca1…`), not pUSD, even though V2 trading itself settles in pUSD —
    this script still passes `env.collateral_token` (pUSD) to the adapter call
    exactly as the SDK's own `redeem_positions()` does (unchanged from its
    internal code), since the adapter (not raw CTF) is the actual call target and
    is Polymarket's own tested conversion path; the real payout currency is
    confirmed empirically after the tx (pUSD balance check), not assumed here.

Ledger: results are appended via the CANONICAL earn-ledger writer
(`__REPO_ROOT__/skills/earn/lib/record.mjs`) — this file does not reimplement the
GATE-0 classifier or the malice-guard; it only derives the {earn,cost,tx,status}
facts and hands them to the existing writer.

NO DRY RUN — running `main()` submits real on-chain redeem transactions.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DATA_API = "https://data-api.polymarket.com"

# Whose winnings this run cashes out. Every Anicca — claude-p, Franklin, Franklin2,
# automaton, and anyone who spins up their own — redeems into their OWN wallet, so this
# cannot be a constant. It was one (claude-p's, hardcoded), which meant every other AI
# could place bets but could never collect on them: run.sh resolves each instance's own
# key, the client then derived a different wallet, and the money-safety check below
# aborted the redeem. They could win and never get paid.
#
# PM_DEPOSIT_WALLET is an explicit installation setting. The client must resolve to this
# exact deposit wallet; a mismatched signer fails closed before position lookup or redemption.
REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[3]))
LEDGER_RECORD_JS = str(REPO_ROOT / "skills/earn/lib/record.mjs")
STATE_ROOT = Path(os.environ.get("LIFE_MANAGER_STATE_ROOT", Path.home() / ".local/state/life-manager/earn-watch"))
LEDGER_PATH = os.environ.get("LIFE_MANAGER_EARN_LEDGER_PATH", str(STATE_ROOT / "earn-ledger.jsonl"))
EARN_GUARD_JS = str(REPO_ROOT / "skills/_shared/lib/earn-guard.mjs")
NODE_BIN = os.environ.get("LIFE_MANAGER_NODE", "node")
# P1 (spec §3/§4): the SAME kill-switch file run.sh (the trading entrypoint, same dir as this
# script) already checks at the top of every pass — writing it here means a cumulative-net
# HALT stops the NEXT trading pass with zero changes to run.sh itself.
KILL_SWITCH = os.environ.get(
    "LIFE_MANAGER_EARN_KILL_PATH",
    os.environ.get("PM_KILL_SWITCH", str(Path.home() / ".local/state/life-manager/polymarket/KILL")),
)
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")

# Verified against the installed SDK's PRODUCTION Environment (environments.py) —
# the raw Gnosis Conditional Tokens Framework contract and the two Polymarket
# redeem adapters (standard vs neg-risk) that must be granted ERC-1155 operator
# rights before they can redeem on the deposit wallet's behalf.
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from v2_recipe import pusd_balance  # noqa: E402  (reuse the verified balance reader)
from state_paths import external_state_path  # noqa: E402


def configured_deposit_wallet() -> str:
    """Return the install-selected Polymarket deposit wallet or fail closed."""
    wallet = os.environ.get("PM_DEPOSIT_WALLET", "")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        raise RuntimeError("PM_DEPOSIT_WALLET must be a 0x-prefixed 40-hex address")
    return wallet.lower()


# ---------------------------------------------------------------------------
# PURE FUNCTIONS — no network, no chain, no relayer. Unit-tested in test_redeem.py.
# ---------------------------------------------------------------------------

def dedupe_redeemable_conditions(positions: list[dict]) -> list[dict]:
    """Collapse a data-api /positions response into one row per redeemable
    conditionId. A resolved market can list BOTH its winning and losing outcome
    as separate rows sharing one conditionId (e.g. Wimbledon: Flavio $10 win +
    Karen $0 loss) — `redeem_positions(condition_id=...)` redeems the whole
    condition in one on-chain call, so this function sums the legs the wallet
    actually holds rather than issuing one redeem per leg."""
    by_condition: dict[str, dict] = {}
    for p in positions:
        if not p.get("redeemable"):
            continue
        cid = p["conditionId"]
        row = by_condition.setdefault(cid, {
            "conditionId": cid,
            "title": p.get("title"),
            "negativeRisk": bool(p.get("negativeRisk")),
            "currentValue": 0.0,
            "initialValue": 0.0,
            "cashPnl": 0.0,
        })
        row["currentValue"] += float(p.get("currentValue") or 0)
        row["initialValue"] += float(p.get("initialValue") or 0)
        row["cashPnl"] += float(p.get("cashPnl") or 0)
    return sorted(by_condition.values(), key=lambda r: -r["currentValue"])


def classify_market_type(negative_risk: bool) -> str:
    """R5: which on-chain redeem path a condition needs. This is a fixed fact
    the market itself carries (its own negRisk flag), not a strategy decision —
    purely informational/logging here since the installed SDK dispatches the
    actual call internally (see module docstring)."""
    return "neg_risk" if negative_risk else "standard"


def redeem_operator_for(negative_risk: bool) -> str:
    """R5: which contract must be granted ERC-1155 operator rights (and is the
    redeem call target) for a condition — mechanical dispatch on the market's own
    on-chain flag, mirroring `normalize_market_position_context`'s
    `neg_risk_collateral_adapter if neg_risk else collateral_adapter` choice
    (positions.py:92), not re-derived or guessed."""
    return NEG_RISK_COLLATERAL_ADAPTER if negative_risk else COLLATERAL_ADAPTER


def compute_recovered_amount(pusd_before: float, pusd_after: float) -> float:
    """R2 check: cash that landed in the deposit wallet from this redeem batch.
    Never negative — a decrease means something ELSE moved funds out during the
    run and that must be investigated, never silently reported as a recovery."""
    delta = round(pusd_after - pusd_before, 6)
    if delta < 0:
        raise ValueError(
            f"pUSD balance DECREASED ({pusd_before} -> {pusd_after}); refusing to "
            "report a negative recovery as a redeem result"
        )
    return delta


def build_ledger_line(row: dict, tx_hash: str, status: str, wallet: str) -> dict:
    """R3: the earn-ledger.jsonl line for one redeemed condition.
    earn_usdc = gross cash the CTF/neg-risk contract paid out for this condition
    (the first ledger touch for this position — the original buy was never itself
    ledgered); cost_usdc = the original stake; record.mjs derives
    net_usdc = earn - cost = the realized profit for this condition."""
    return {
        # The redeeming install's explicitly verified deposit wallet.
        "wallet": wallet.lower(),
        "source": "polymarket-redeem",
        "task": row["title"],
        "earn_usdc": round(row["currentValue"], 6),
        "cost_usdc": round(row["initialValue"], 6),
        "tx": tx_hash,
        "status": status,
        "chain": "polygon",
        "external": True,
        "wake": f"redeem-{row['conditionId'][:10]}",
    }


# ---------------------------------------------------------------------------
# I/O BOUNDARY — network, chain, relayer. This is the surface to scrutinize.
# ---------------------------------------------------------------------------

def fetch_positions(wallet: str) -> list[dict]:
    import requests
    r = requests.get(
        f"{DATA_API}/positions",
        params={"user": wallet, "sizeThreshold": 0.001, "limit": 100},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _mint_relayer_api_key(acct) -> str:
    """SIWE mint of a Polymarket RelayerApiKey (no browser, no CDP account).

    2026-07-07: extracted into relayer_auth.mint_relayer_api_key so
    market_maker.py/bundle_arb.py/place_order.py (which each carried their own
    NON-caching duplicate that hit the relayer's 100-keys-per-address cap and
    broke for 3 days) reuse this exact, already-proven-live implementation
    instead of drifting copies. Kept as a thin wrapper here so nothing else in
    this file needs to change."""
    from relayer_auth import mint_relayer_api_key
    return mint_relayer_api_key(acct)


def build_client(expected_wallet: str):
    """Authenticate the polymarket-client SDK for the deposit wallet, supplying a REUSED relayer
    api key (via _mint_relayer_api_key, which now lists-before-mints). The SDK's own auth was NOT
    enough: SecureClient.create(private_key, wallet) resolves the wallet but the gasless /submit
    still needs a valid relayer api key in the RelayerApiKey credential — without it, /submit
    rejects with "invalid authorization". _mint_relayer_api_key reuses an existing relayer key
    (Gamma-auth registry, capped at 100/address, no delete endpoint) so we never burn the cap."""
    key = os.environ.get("POLYGON_WALLET_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError("POLYGON_WALLET_PRIVATE_KEY must be provided by earn-watch")
    key = key if key.startswith("0x") else "0x" + key

    from eth_account import Account
    from polymarket.auth import RelayerApiKey
    from polymarket.clients.secure import SecureClient

    acct = Account.from_key(key)
    api_key = _mint_relayer_api_key(acct)  # list-before-mint: reuse existing relayer key
    tmp = SecureClient._create(private_key=key, validate_credentials=True)
    creds = tmp._ctx.credentials
    tmp.close()
    client = SecureClient.create(
        private_key=key, credentials=creds,
        api_key=RelayerApiKey(key=api_key, address=acct.address),
    )
    if str(client.wallet).lower() != expected_wallet:
        client.close()
        raise RuntimeError(
            f"money-safety abort: client resolved to wallet {client.wallet}, "
            f"but PM_DEPOSIT_WALLET expects {expected_wallet}"
        )
    return client


def is_ctf_operator_approved(owner: str, operator: str) -> bool:
    """Read-only on-chain check (no tx) — does `owner` already trust `operator`
    to move its CTF ERC-1155 tokens? Verified live 2026-07-05: this was False for
    our deposit wallet against both redeem adapters before this feature existed,
    which is the actual root cause of the 'ERC1155: need operator approval for
    3rd party transfers' revert."""
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    c = w3.eth.contract(address=w3.to_checksum_address(CTF_ADDRESS), abi=[{
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}],
        "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function",
    }])
    return c.functions.isApprovedForAll(
        w3.to_checksum_address(owner), w3.to_checksum_address(operator)
    ).call()


def ensure_ctf_operator_approval(client, operator: str) -> str | None:
    """One-time on-chain permission grant: authorize `operator` (one of
    Polymarket's own redeem adapters) to move the deposit wallet's RESOLVED CTF
    position tokens so it can redeem them on our behalf. This grants redemption
    rights ONLY — it cannot move pUSD/USDC, and is fully revocable
    (`approved=False`) at any time. Idempotent: returns None (no tx sent) if
    already approved. Uses the SDK's own public `approve_erc1155_for_all`."""
    # Ask about THIS client's own wallet — the approval lives on the wallet that will
    # actually redeem, which differs per instance.
    if is_ctf_operator_approved(str(client.wallet), operator):
        return None
    handle = client.approve_erc1155_for_all(
        token_address=CTF_ADDRESS, operator_address=operator, approved=True,
        metadata=f"approve redeem operator {operator}"[:64],
    )
    outcome = handle.wait()
    return outcome.transaction_hash


def redeem_condition(client, condition_id: str) -> dict:
    """R1: submit the redeem tx for one condition and wait for a terminal on-chain
    outcome.

    The SDK's own convenience wrapper `SecureClient.redeem_positions()` has a
    real bug for our exact use case: internally it calls
    `list_markets(condition_ids=[condition_id])` WITHOUT `closed=True`, and the
    Gamma API silently omits closed (resolved) markets unless `closed` is
    explicitly requested (verified live: `curl gamma-api/markets?slug=...`
    returns `[]`, `?slug=...&closed=true` returns the market) — so the wrapper
    always raises `UserInputError("No market found")` for exactly the resolved
    markets redeem() exists to handle.

    This calls the SAME underlying primitives the wrapper would
    (`normalize_market_position_context` + `ctf_redeem_positions_call` +
    `client._dispatch_single_call`, secure.py:2232-2246) but fetches the market
    with `closed=True` first. Dispatch still picks standard vs neg-risk purely
    from the market's own `negRisk` flag (R5) — only the market LOOKUP changed."""
    from polymarket._internal.actions.relayer.positions import (
        normalize_market_position_context,
    )
    from polymarket.calls import ctf_redeem_positions_call

    page = client.list_markets(condition_ids=[condition_id], closed=True, page_size=1).first_page()
    markets = page.items
    if not markets:
        raise RuntimeError(f"No market found for condition {condition_id} (even with closed=True)")
    if len(markets) != 1:
        raise RuntimeError(f"Expected exactly one market for {condition_id}, got {len(markets)}")

    env = client._ctx.environment
    context = normalize_market_position_context(
        markets[0],
        context=f"condition {condition_id}",
        collateral_adapter=env.collateral_adapter,
        neg_risk_collateral_adapter=env.neg_risk_collateral_adapter,
        conditional_tokens=env.conditional_tokens,
        neg_risk_adapter=env.neg_risk_adapter,
    )
    call = ctf_redeem_positions_call(
        ctf=context.adapter_address,
        collateral=env.collateral_token,
        condition_id=context.condition_id,
    )
    handle = client._dispatch_single_call(call, metadata=f"redeem {condition_id}"[:64])
    outcome = handle.wait()
    return {"tx_hash": outcome.transaction_hash, "transaction_id": outcome.transaction_id}


def fetch_receipt_status(tx_hash: str, max_wait_s: int = 120) -> str:
    """Independent on-chain confirmation (do not just trust the relayer's own
    success signal) — read the real Polygon receipt so the DONE criteria's
    'polygonscan status 0x1' claim is backed by fresh RPC evidence, not the SDK."""
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is not None:
            return hex(receipt.status)
        time.sleep(3)
    raise TimeoutError(f"no receipt for {tx_hash} after {max_wait_s}s")


def record_ledger_line(line: dict, ledger_path: str | None = None) -> bool:
    """R3/R4 bookkeeping: append via the CANONICAL earn-ledger writer (record.mjs)
    so the malice-guard + GATE-0 classifier stay the single source of truth —
    this file does not reimplement ledger schema or profitability rules."""
    ledger_path = external_state_path(
        ledger_path or LEDGER_PATH, REPO_ROOT, "LIFE_MANAGER_EARN_LEDGER_PATH"
    )
    args = [NODE_BIN, LEDGER_RECORD_JS, json.dumps(line), ledger_path]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"record.mjs failed: {result.stderr.strip()}")
    return "PROFITABLE" in result.stdout


def check_cumulative_halt(wallet: str, source: str, ledger_path: str | None = None) -> tuple[bool, str]:
    """P1 (spec §3/§4): ask the shared CUMULATIVE earn>spend guard (earn-guard.mjs) whether
    this wallet+source has gone insolvent (or has ledger numbers it can't trust) across every
    redeem ever recorded, not just this one condition. Fail-closed like record_ledger_line: an
    unexpected error running the guard itself is treated as a HALT, never silently ignored —
    this function never raises."""
    try:
        ledger_path = external_state_path(
            ledger_path or LEDGER_PATH, REPO_ROOT, "LIFE_MANAGER_EARN_LEDGER_PATH"
        )
    except RuntimeError as e:
        return True, f"guard-error:{e}"
    args = [NODE_BIN, EARN_GUARD_JS, "check", wallet, source, ledger_path]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001 — fail-closed: can't check -> assume HALT
        return True, f"guard-error:{e}"
    if result.returncode == 0:
        return False, ""
    return True, result.stderr.strip() or f"guard exited {result.returncode}"


def write_kill_switch(reason: str) -> None:
    """Trip the SAME kill-switch file run.sh (this skill's trading entrypoint) already checks
    at the top of every pass — a cumulative HALT here stops the NEXT trading pass with zero
    changes to run.sh's own logic."""
    kill_switch = external_state_path(
        KILL_SWITCH, REPO_ROOT, "LIFE_MANAGER_EARN_KILL_PATH"
    )
    os.makedirs(os.path.dirname(kill_switch), exist_ok=True)
    with open(kill_switch, "w") as f:
        f.write(f"P1 fail-closed HALT ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}): {reason}\n")


def main() -> int:
    global LEDGER_PATH, KILL_SWITCH
    LEDGER_PATH = external_state_path(LEDGER_PATH, REPO_ROOT, "LIFE_MANAGER_EARN_LEDGER_PATH")
    KILL_SWITCH = external_state_path(KILL_SWITCH, REPO_ROOT, "LIFE_MANAGER_EARN_KILL_PATH")
    # WHO AM I — answered by this instance's own key, not by a constant. The wallet is
    # DERIVED from the key, so an instance can only ever see and cash out positions it
    # actually owns: claude-p cannot redeem Franklin's winnings and Franklin cannot
    # redeem claude-p's, no matter how the environment is misconfigured.
    expected_wallet = configured_deposit_wallet()
    client = build_client(expected_wallet)
    wallet = str(client.wallet)

    try:
        positions = fetch_positions(wallet)
        rows = dedupe_redeemable_conditions(positions)
        if not rows:
            print(f"no redeemable conditions found for {wallet} — nothing to do")
            return 0

        print(f"found {len(rows)} redeemable condition(s) for {wallet}:")
        for row in rows:
            kind = classify_market_type(row["negativeRisk"])
            print(f"  - {row['title']!r} conditionId={row['conditionId']} "
                  f"value=${row['currentValue']:.4f} type={kind}")

        before = pusd_balance(wallet)
        print(f"pUSD before: {before}")
    except Exception:
        client.close()
        raise

    results = []
    try:
        for row in rows:
            operator = redeem_operator_for(row["negativeRisk"])
            approve_tx = ensure_ctf_operator_approval(client, operator)
            if approve_tx:
                print(f"  approved CTF operator {operator}: tx={approve_tx}")
            print(f"redeeming {row['conditionId']} ({row['title']!r}) ...")
            tx = redeem_condition(client, row["conditionId"])
            status = fetch_receipt_status(tx["tx_hash"])
            print(f"  tx={tx['tx_hash']} status={status}")
            line = build_ledger_line(row, tx_hash=tx["tx_hash"], status=status, wallet=wallet)
            profitable = record_ledger_line(line, ledger_path=LEDGER_PATH)
            results.append({**tx, "status": status, "row": row, "profitable": profitable})
            # P1 (spec §3/§4): after EVERY redeem, re-check the cumulative earn>spend
            # invariant. HALT -> stop redeeming further conditions THIS pass AND trip the
            # kill-switch so the trading entrypoint (run.sh) refuses the NEXT pass too.
            halted, reason = check_cumulative_halt(
                wallet.lower(), "polymarket-redeem", ledger_path=LEDGER_PATH
            )
            if halted:
                write_kill_switch(reason)
                print(f"P1 GUARD: cumulative net breach ({reason}) — wrote KILL switch, stopping redeem pass.")
                break
    finally:
        client.close()

    after = pusd_balance(wallet)
    recovered = compute_recovered_amount(before, after)
    print(f"pUSD after: {after}  (recovered: {recovered})")

    for r in results:
        print(json.dumps({
            "conditionId": r["row"]["conditionId"],
            "title": r["row"]["title"],
            "tx_hash": r["tx_hash"],
            "status": r["status"],
            "profitable": r["profitable"],
        }, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
