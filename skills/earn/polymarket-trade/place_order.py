#!/usr/bin/env python3
"""
place_order.py — EXECUTION: generalized V2 FAK order placement (#25 spec §2.2).

The EXACT working path that used to live in v2_full_flow.py (SecureClient sig-3
credential bootstrap + relayer-key mint via SIWE + get_order_book +
create_market_order FAK + post_order), generalized so TOKEN_ID/SIDE/AMOUNT are
INPUTS, never hardcoded. v2_full_flow.py itself was DELETED (#25 adversary fix
#2) — it hardcoded a TID and was standalone-runnable, a footgun now that this
file supersedes it; nothing else imported/called it (verified via grep before
removal). WHICH market/side/amount is entirely pick.py's (the model's)
decision — this file only executes it and enforces the money-safety cap.

Inputs (env preferred, positional argv fallback):
  TOKEN_ID   CLOB token id to BUY                              (argv[1])
  SIDE       must be "BUY" — this flow only supports BUY;
             SELL needs shares already held, out of scope here (argv[2])
  AMOUNT     USD spend amount                                  (argv[3])
  POLYGON_WALLET_PRIVATE_KEY  this instance's signer key (read via the
                              agent's own .env, same as fund_via_bridge.py)
  MAX_BET_SIZE (default 2)   hard cap on AMOUNT (money-safety, not judgment)

Output: exactly one line of JSON on the REAL stdout (guaranteed clean — see below):
  {"token_id","amount","order_id","post_result","ok"}
On any failure before an order can be attempted: {"ok": false, "error": "..."}
(exit code 1). Never places a fake/simulated order — no dry-run path exists.

CLEAN-STDOUT GUARANTEE (#25 adversary fix, accounting integrity, 2026-07-05):
a real Franklin fill ("Will Jesus Christ return before GTA VI?", NO, ~$1,
CONFIRMED filled on-chain) was mis-logged ok:false by run.sh because the
polymarket SDK (imported inside main(), during mint/credential-bootstrap)
printed to stdout and merged onto the SAME line as the result JSON, breaking
run.sh's json.loads(). Fix: `sys.stdout` at process start is captured as
`_REAL_STDOUT` before anything runs; ALL work (SDK import, SIWE mint,
approve, order book, order placement) runs under
`contextlib.redirect_stdout(sys.stderr)`, so any stray SDK/library print
lands on stderr. The ONLY thing ever written to `_REAL_STDOUT` is the single
final `_emit(...)` call — stdout is guaranteed to be exactly one clean JSON
line, so a real fill is never again mis-recorded as a parse failure.
"""
import os
import sys
import json
import contextlib
import requests
from eth_account import Account

_REAL_STDOUT = sys.stdout  # capture BEFORE any import/work that might print


def _emit(obj):
    """The ONLY function allowed to write to the real, captured stdout."""
    print(json.dumps(obj), file=_REAL_STDOUT, flush=True)


PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
NEG_EXCH = "0xe2222d279d744050d28e00520010520000310F59"
NEG_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
DEFAULT_MAX_BET_SIZE = 2.0
SLIPPAGE = 0.03


def _arg(i):
    return sys.argv[i] if len(sys.argv) > i else None


def fail(reason):
    """Uses _emit (real stdout) so it's clean even when called from inside
    the redirect_stdout(sys.stderr) block in main()."""
    _emit({"ok": False, "error": reason})
    sys.exit(1)


# ROOT CAUSE FIX (2026-07-07): this used to carry its own per-call mint that POSTed a
# brand-new relayer key EVERY invocation -> contributes to Polymarket's 100-keys-per-
# address cap (the same cap that killed market_maker.py/bundle_arb.py for 3 days, see
# relayer_auth.py docstring). Now reuses the SAME list-before-mint + cached
# implementation redeem.py already proved live, instead of a third drifting copy.
from relayer_auth import mint_relayer_api_key


def mint_relayer_key(acct):
    return mint_relayer_api_key(acct)


def approve_spenders(client):
    """Idempotent — fund_via_bridge.py already does this at registration
    time; re-approving here is safe and covers a freshly-registered wallet
    whose approvals haven't landed yet."""
    for spender in (NEG_EXCH, NEG_ADAPTER):
        balance_allowance = client.get_balance_allowance(asset_type="COLLATERAL")
        if int(balance_allowance.allowances.get(spender, 0)) < 1:
            handle = client.approve_erc20(token_address=PUSD, spender_address=spender, amount="max")
            try:
                handle.wait()
            except Exception:
                pass


def best_ask_price(order_book):
    asks = getattr(order_book, "asks", [])
    if not asks:
        return 0.58  # last-resort fallback, matches the original proven-live value
    return min(float(getattr(a, "price", None) or a["price"]) for a in asks)


def _run():
    """All the actual work. Called ONLY from inside main()'s
    redirect_stdout(sys.stderr) block, so any print() anywhere in this call
    graph (the polymarket SDK's import/mint/approve/order-book/post_order
    machinery) lands on stderr, never merging onto the final result line."""
    token_id = os.environ.get("TOKEN_ID") or _arg(1)
    side = (os.environ.get("SIDE") or _arg(2) or "BUY").upper()
    amount_raw = os.environ.get("AMOUNT") or _arg(3)

    if not token_id:
        fail("missing TOKEN_ID")
    if side != "BUY":
        fail(f"unsupported SIDE:{side} (this flow only executes BUY)")
    if not amount_raw:
        fail("missing AMOUNT")

    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        fail(f"invalid AMOUNT:{amount_raw}")

    max_bet_size = float(os.environ.get("MAX_BET_SIZE", DEFAULT_MAX_BET_SIZE))
    if amount > max_bet_size:
        amount = max_bet_size  # money-safety cap (spec §2.2) — not a judgment call

    key = os.environ.get("POLYGON_WALLET_PRIVATE_KEY")
    if not key:
        fail("missing POLYGON_WALLET_PRIVATE_KEY")
    key = key if key.startswith("0x") else "0x" + key

    from polymarket.clients.secure import SecureClient
    from polymarket.auth import RelayerApiKey

    acct = Account.from_key(key)
    bootstrap = SecureClient._create(private_key=key, validate_credentials=True)
    credentials = bootstrap._ctx.credentials
    bootstrap.close()

    client = SecureClient.create(
        private_key=key,
        credentials=credentials,
        api_key=RelayerApiKey(key=mint_relayer_key(acct), address=acct.address),
    )

    try:
        approve_spenders(client)

        order_book = client.get_order_book(token_id=token_id)
        ask = best_ask_price(order_book)
        max_price = str(round(ask + SLIPPAGE, 3))

        signed_order = client.create_market_order(
            token_id=token_id,
            side="BUY",
            amount=str(amount),
            max_price=max_price,
            order_type="FAK",
        )
        response = client.post_order(signed_order)

        try:
            post_result = response.model_dump(mode="json")
        except Exception:
            post_result = str(response)

        _emit({
            "token_id": token_id,
            "amount": amount,
            "order_id": getattr(response, "order_id", None),
            "post_result": post_result,
            "ok": bool(getattr(response, "ok", False)),
        })
    finally:
        client.close()


def main():
    """Entry point. Redirects sys.stdout to stderr for the ENTIRE run
    (SDK import, mint, approve, order-book, post_order) so stdout stays
    clean; _emit()/fail() write to _REAL_STDOUT directly, so they're
    unaffected by the redirect."""
    with contextlib.redirect_stdout(sys.stderr):
        _run()


if __name__ == "__main__":
    main()
