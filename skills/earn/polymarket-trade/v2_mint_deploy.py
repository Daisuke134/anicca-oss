#!/usr/bin/env python3
"""Mint Polymarket V2 RelayerApiKey via SIWE (no browser) -> deploy deposit wallet."""
import os, json, base64, datetime
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

KEY = os.getenv("POLYGON_WALLET_PRIVATE_KEY"); KEY = KEY if KEY.startswith("0x") else "0x"+KEY
acct = Account.from_key(KEY)
ADDR = acct.address
GAMMA = "https://gamma-api.polymarket.com"
RELAYER = "https://relayer-v2.polymarket.com"
CHAIN = 137

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0", "Origin": "https://polymarket.com", "Referer": "https://polymarket.com/"})

# 1. nonce
r = s.get(f"{GAMMA}/nonce", timeout=20)
print("nonce HTTP", r.status_code, r.text[:120])
nonce = r.json().get("nonce")

# 2. SIWE message (EIP-4361 plaintext) — exact polygolem/viem format
issued = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
         f"{datetime.datetime.now(datetime.timezone.utc).microsecond//1000:03d}Z"
fields = {"domain":"polymarket.com","address":ADDR,
          "statement":"Welcome to Polymarket! Sign to connect.",
          "uri":"https://polymarket.com","version":"1","chainId":CHAIN,
          "nonce":nonce,"issuedAt":issued}
plaintext = (f"{fields['domain']} wants you to sign in with your Ethereum account:\n"
             f"{ADDR}\n\n{fields['statement']}\n\n"
             f"URI: {fields['uri']}\nVersion: {fields['version']}\n"
             f"Chain ID: {CHAIN}\nNonce: {nonce}\nIssued At: {issued}")

# 3. personal_sign (EIP-191)
sig = acct.sign_message(encode_defunct(text=plaintext)).signature
sig_hex = "0x" + sig.hex()

# 4. bearer = base64( JSON(fields) + ":::" + 0xsighex )
combined = json.dumps(fields, separators=(",",":")) + ":::" + sig_hex
bearer = base64.b64encode(combined.encode()).decode()

# 5. GET /login
r = s.get(f"{GAMMA}/login", headers={"Authorization": "Bearer "+bearer}, timeout=20)
print("login HTTP", r.status_code, r.text[:150])
print("cookies:", [c.name for c in s.cookies])

# 6. POST relayer auth -> {apiKey, address}
r = s.post(f"{RELAYER}/relayer/api/auth", json={}, timeout=20)
print("relayer/auth HTTP", r.status_code, r.text[:200])
if r.status_code == 200:
    data = r.json()
    api_key = data.get("apiKey") or data.get("api_key")
    print("RELAYER API KEY:", api_key)
    # 7. deploy deposit wallet using RelayerApiKey
    from polymarket.clients.secure import SecureClient
    from polymarket.auth import RelayerApiKey
    tmp = SecureClient._create(private_key=KEY, validate_credentials=True)
    creds = tmp._ctx.credentials; dw = str(tmp._ctx.wallet); tmp.close()
    print("deposit wallet:", dw)
    rk = RelayerApiKey(key=api_key, address=ADDR)
    c = SecureClient.create(private_key=KEY, credentials=creds, api_key=rk)
    print("client ready. deploying / checking deposit wallet...")
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
    code = w3.eth.get_code(w3.to_checksum_address(dw))
    print("DEPOSIT WALLET DEPLOYED?:", "YES ✅ "+dw if len(code) > 2 else "still NO")
    c.close()
else:
    print("relayer auth failed — inspect body above")
