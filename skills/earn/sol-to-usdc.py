#!/usr/bin/env python3
"""
Life Manager SOL(Solana) -> USDC(Base) auto-swap via relay.link API.
SOL is the transport asset; the configured instance receives USDC.

Flow: detect SOL on the configured Solana wallet -> relay /quote -> build+sign the Solana
tx from the returned instructions (solders) -> submit to a Solana RPC -> poll relay
/intents/status until the USDC fill lands in the configured Base recipient wallet.

Env (~/.local/state/life-manager/.env): ANICCA_SOLANA_KEY (base58 secret), SOLANA_RPC (optional).
Run: python3 sol-to-usdc.py            # swaps the full SOL balance (minus rent/fee buffer)
     python3 sol-to-usdc.py --lamports 50000000   # swap a fixed amount

UNVERIFIED until a real SOL balance exists — relay dry /quote is verified
(0.05 SOL -> 3.50 USDC), but the build/sign/submit path needs one real run to confirm.
"""
import base64
import json
import os
import sys
import time
import urllib.request

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.address_lookup_table_account import AddressLookupTableAccount

# Reusable swap skill: recipient + signing key are private runtime configuration so every
# Life Manager instance bridges only its own funds. Missing configuration is a clean no-op.
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SOL_NATIVE = "11111111111111111111111111111111"
SOLANA = 792703809
BASE = 8453
# 2026-07-12: the destination was hardcoded to Base/USDC-on-Base, so a Solana->Polygon leg (needed to
# top up the Polymarket owner EOA in ONE hop instead of Solana->Base->Polygon) had no way to express
# itself and would have forced a second bridge -- exactly the multi-hop fee bleed that cost ~$8 on
# 2026-07-12. Both are now env-overridable; the defaults keep every existing caller (the unattended
# sol-funding-daemon, which sets neither) on the identical Base/USDC route it has always used.
DEST_CHAIN = int(os.environ.get("SWAP_DEST_CHAIN", BASE))
DEST_CURRENCY = os.environ.get("SWAP_DEST_CURRENCY", USDC_BASE).lower()
RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
RENT_BUFFER = 5_000_000  # leave ~0.005 SOL for fees/rent


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["result"]


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def get(url):
    return json.loads(urllib.request.urlopen(url, timeout=30).read())


def main():
    secret = os.environ.get("SWAP_SOLANA_KEY") or os.environ.get("ANICCA_SOLANA_KEY")
    recipient = (os.environ.get("SWAP_RECIPIENT") or "").strip().lower()
    if not secret or len(recipient) != 42 or not recipient.startswith("0x"):
        print("sol funding not configured; set a Solana key and SWAP_RECIPIENT")
        return
    try:
        int(recipient[2:], 16)
    except ValueError:
        print("sol funding not configured; SWAP_RECIPIENT must be an EVM address")
        return
    kp = Keypair.from_base58_string(secret)
    me = str(kp.pubkey())

    bal = rpc("getBalance", [me])["value"]
    print(f"anicca SOL wallet {me} balance={bal/1e9} SOL")
    if "--lamports" in sys.argv:
        amount = int(sys.argv[sys.argv.index("--lamports") + 1])
    else:
        amount = bal - RENT_BUFFER
    if amount <= 0:
        print("no swappable SOL (need funds + rent buffer)")
        return
    print(f"swapping {amount/1e9} SOL -> chain {DEST_CHAIN} token {DEST_CURRENCY} to {recipient}")

    q = post("https://api.relay.link/quote", {
        "user": me, "recipient": recipient,
        "originChainId": SOLANA, "destinationChainId": DEST_CHAIN,
        "originCurrency": SOL_NATIVE, "destinationCurrency": DEST_CURRENCY,
        "amount": str(amount), "tradeType": "EXACT_INPUT",
    })
    det = q.get("details", {})
    print("quote out:", det.get("currencyOut", {}).get("amountFormatted"), "USDC")

    step = q["steps"][0]
    item = step["items"][0]
    data = item["data"]

    # build instructions
    ixs = []
    for ix in data["instructions"]:
        ixs.append(Instruction(
            program_id=Pubkey.from_string(ix["programId"]),
            accounts=[AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"], a["isWritable"]) for a in ix["keys"]],
            data=bytes.fromhex(ix["data"][2:] if ix["data"].startswith("0x") else ix["data"]),  # relay Solana ix.data = hex
        ))

    # address lookup tables
    alts = []
    for addr in data.get("addressLookupTableAddresses", []):
        acc = rpc("getAccountInfo", [addr, {"encoding": "base64"}])
        raw = base64.b64decode(acc["value"]["data"][0])
        alts.append(_parse_alt(addr, raw))

    bh = rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    msg = MessageV0.try_compile(kp.pubkey(), ixs, alts, _hash(bh))
    tx = VersionedTransaction(msg, [kp])

    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "skipPreflight": True}])
    print("submitted solana tx:", sig)

    # poll relay cross-chain status
    check = item.get("check", {}).get("endpoint")
    if check:
        url = "https://api.relay.link" + check
        for _ in range(40):
            time.sleep(3)
            st = get(url).get("status")
            print("relay status:", st)
            if st in ("success", "refund"):
                break
    print("done")


def _is_b64(s):
    try:
        base64.b64decode(s)
        return not s.startswith("0x")
    except Exception:
        return False


def _hash(b58):
    from solders.hash import Hash
    return Hash.from_string(b58)


def _parse_alt(addr, raw):
    # ALT layout: 56-byte header, then 32-byte addresses
    addrs = [Pubkey.from_bytes(raw[i:i + 32]) for i in range(56, len(raw), 32)]
    return AddressLookupTableAccount(key=Pubkey.from_string(addr), addresses=addrs)


if __name__ == "__main__":
    main()
