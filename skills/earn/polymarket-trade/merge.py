#!/usr/bin/env python3
"""
merge.py — RECOVER-1: merge a balanced YES/NO leg pair back into pUSD collateral
via the installed polymarket-client SDK's native `SecureClient.merge_positions`.

WHY THIS FILE EXISTS
---------------------
redeem.py only collects RESOLVED (closed) markets. AUDIT-1 (2026-07-17) found claude-p
holding an OPEN (unresolved) Fed-rate market position — 13 YES / 5 NO shares — where the
balanced 5+5 pair is mergeable RIGHT NOW, permissionless, no counterparty/liquidity needed
(CTF mergePositions burns one YES + one NO token 1:1 for 1 unit of collateral). This is
NOT a redeem (market isn't resolved) and NOT a sale (no orderbook liquidity needed) — it
is a third, distinct CTF primitive this repo had no script for.

MECHANISM (verified against the installed SDK, ground truth not docs)
-----------------------------------------------------------------------
`polymarket.clients.secure.SecureClient.merge_positions(condition_id=..., amount=...)`
(secure.py:2110) already exists in the SDK redeem.py imports from (`polymarket-client`
0.1.0b13, managed Life Manager Python). Reuses redeem.py's already-proven `build_client()` (SIWE mint,
no browser) and `ensure_ctf_operator_approval()` (same ERC-1155 operator grant redeem
needs — verified live 2026-07-17: both COLLATERAL_ADAPTER and NEG_RISK_COLLATERAL_ADAPTER
already `isApprovedForAll=True` for claude-p's deposit wallet from prior redeem runs, so
this call needs zero new approval tx).

`amount="max"` resolves (SDK's own `resolve_merge_amount_from_balances`) to the largest
BALANCED amount actually held — i.e. min(YES balance, NO balance) — so it can never merge
more than what both legs actually hold, and never needs the caller to hand-compute base
units (6-decimal share counts) itself.

Sources (quoted, no guessing):
  - installed `polymarket` package, `clients/secure.py::merge_positions` (line 2110) —
    ground truth, read directly from the installed wheel in this session.
  - `_internal/actions/relayer/positions.py::resolve_merge_amount_from_balances` — the
    "max = balanced amount held" resolution, read directly.
  - live `data-api.polymarket.com/positions?user=0x904B50d2…` (this session) — confirmed
    `"mergeable": true` on both legs, conditionId
    `0x8bf1c1536ecb1c08fe13c6b71e8ab1f58bf3461c4cb79f5f1679f869a06aef86`,
    YES size=13 avgPrice=0.6923, NO size=5 avgPrice=0.26, negativeRisk=true.
  - live `isApprovedForAll` eth_call (this session) — both adapters already approved,
    zero new approval tx needed.

Ledger: same CANONICAL earn-ledger writer as redeem.py (record.mjs), source
"polymarket-merge". cost_usdc = the ACTUAL cost basis of the merged shares
(avgPrice * merged_amount per leg, summed) — never fabricated, computed from the
data-api's own avgPrice for the exact merged size.

NO DRY RUN — running `main()` submits a real on-chain merge transaction.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redeem import (  # noqa: E402  (reuse proven infra, don't reinvent)
    DEPOSIT_WALLET,
    EXPECT_WALLET,
    LEDGER_PATH,
    build_client,
    ensure_ctf_operator_approval,
    redeem_operator_for,
    fetch_receipt_status,
    record_ledger_line,
    check_cumulative_halt,
    write_kill_switch,
    fetch_positions,
)
from v2_recipe import pusd_balance  # noqa: E402


def find_mergeable_condition(positions: list[dict], condition_id: str | None = None) -> dict | None:
    """R5: pick the condition to merge. If condition_id given, only that one. Else the
    first condition where BOTH a Yes and No leg are present and `mergeable=True` — pure
    selection, no strategy judgment (which market to merge is not a decision this file
    makes; it merges what the caller names or what data-api already flags mergeable)."""
    by_cid: dict[str, list[dict]] = {}
    for p in positions:
        if condition_id and p.get("conditionId") != condition_id:
            continue
        if not p.get("mergeable"):
            continue
        by_cid.setdefault(p["conditionId"], []).append(p)
    for cid, legs in by_cid.items():
        if len(legs) >= 2:
            return {"conditionId": cid, "legs": legs}
    return None


def compute_merge_cost_basis(legs: list[dict], merged_amount: float) -> float:
    """R3: real cost basis of the merged_amount shares on EACH leg (avgPrice * amount),
    summed. Never fabricated — reads each leg's own avgPrice from data-api."""
    total = 0.0
    for leg in legs:
        total += float(leg.get("avgPrice") or 0) * merged_amount
    return round(total, 6)


def main() -> int:
    condition_id_arg = sys.argv[1] if len(sys.argv) > 1 else None

    client = build_client()
    wallet = str(client.wallet)

    try:
        positions = fetch_positions(wallet)
        picked = find_mergeable_condition(positions, condition_id_arg)
        if not picked:
            print(f"no mergeable balanced condition found for {wallet} — nothing to do")
            client.close()
            return 0

        cid = picked["conditionId"]
        legs = picked["legs"]
        merged_amount = min(float(l["size"]) for l in legs)
        neg_risk = bool(legs[0].get("negativeRisk"))
        title = legs[0].get("title")
        print(f"merging condition {cid} ({title!r}) — balanced amount={merged_amount} negRisk={neg_risk}")
        for l in legs:
            print(f"  leg {l['outcome']}: size={l['size']} avgPrice={l['avgPrice']} value=${l['currentValue']:.4f}")

        before = pusd_balance(wallet)
        print(f"pUSD before: {before}")
    except Exception:
        client.close()
        raise

    try:
        operator = redeem_operator_for(neg_risk)
        approve_tx = ensure_ctf_operator_approval(client, operator)
        if approve_tx:
            print(f"  approved CTF operator {operator}: tx={approve_tx}")
        else:
            print(f"  CTF operator {operator} already approved — no approval tx needed")

        handle = client.merge_positions(condition_id=cid, amount="max")
        outcome = handle.wait()
        tx_hash = outcome.transaction_hash
        status = fetch_receipt_status(tx_hash)
        print(f"merge tx={tx_hash} status={status}")
    finally:
        client.close()

    after = pusd_balance(wallet)
    recovered = round(after - before, 6)
    print(f"pUSD after: {after}  (recovered: {recovered})")
    if recovered < 0:
        raise RuntimeError(f"pUSD balance DECREASED ({before} -> {after}); refusing to report negative recovery")

    cost_basis = compute_merge_cost_basis(legs, merged_amount)
    line = {
        "wallet": wallet.lower(),
        "source": "polymarket-merge",
        "task": title,
        "earn_usdc": round(recovered, 6),
        "cost_usdc": cost_basis,
        "tx": tx_hash,
        "status": status,
        "chain": "polygon",
        "external": True,
        "wake": f"merge-{cid[:10]}",
    }
    profitable = record_ledger_line(line, LEDGER_PATH)
    print(json.dumps({
        "conditionId": cid,
        "title": title,
        "merged_amount": merged_amount,
        "tx_hash": tx_hash,
        "status": status,
        "recovered_usdc": recovered,
        "profitable": profitable,
    }, ensure_ascii=False))

    halted, reason = check_cumulative_halt(wallet.lower(), "polymarket-merge", LEDGER_PATH)
    if halted:
        write_kill_switch(reason)
        print(f"P1 GUARD: cumulative net breach ({reason}) — wrote KILL switch.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
