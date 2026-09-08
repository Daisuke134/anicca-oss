#!/usr/bin/env python3
"""
relayer_auth.py — the ONE working Polymarket relayer SIWE-mint implementation,
shared by every script in this skill that needs a `RelayerApiKey`.

ROOT CAUSE this file fixes (2026-07-07, 3-day outage of market_maker.py /
bundle_arb.py): those two files (and place_order.py) each carried their OWN
copy-pasted `mint()`/`mint_relayer_key()` that POSTs a brand-new relayer key
on every single pass. Polymarket's relayer caps API keys at **100 per
address** and exposes no delete endpoint — repeated per-pass minting burned
through the cap on 2026-07-04 (`400 {"error":"max 100 keys per address"}`),
after which every subsequent `.json()["apiKey"]` raised `KeyError: 'apiKey'`
(the error body has no `apiKey` field) and killed the whole pass before any
trade code ran.

redeem.py had ALREADY independently discovered and fixed this exact bug
(commit history / module docstring: "An api key is meant to be REUSED, so
cache it and only mint when there is none") via LIST-BEFORE-MINT + an
on-disk cache. This module is that same fix, extracted so every caller uses
ONE implementation instead of N duplicated, drifting copies (no
reinvention — copy the already-working pattern).

Usage:
    from eth_account import Account
    from relayer_auth import mint_relayer_api_key
    acct = Account.from_key(key)
    api_key = mint_relayer_api_key(acct)

Security note: this file signs a SIWE login message (the wallet's own
Ethereum private key never leaves the caller's process — it is only used via
`acct.sign_message`, standard eth_account signing) and returns/caches a
short-lived RELAYER api key (a bearer token scoped to Polymarket's gasless
relayer for this address), never the private key itself. The cache file is
chmod 600 and contains ONLY the relayer api key string, never the wallet key.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import time as _time
from pathlib import Path

import requests
from eth_account.messages import encode_defunct
from state_paths import external_state_path

GAMMA = "https://gamma-api.polymarket.com"
RELAYER = "https://relayer-v2.polymarket.com"

def default_cache_path() -> str:
    state_root = Path(os.environ.get("LIFE_MANAGER_STATE_ROOT", Path.home() / ".local/state/life-manager/earn-watch"))
    return os.environ.get("LIFE_MANAGER_RELAYER_CACHE", str(state_root / "relayer-apikey"))


def _pick_existing_key(list_response_json, address: str):
    """From a relayer `/relayer/api/keys` listing, return the newest apiKey
    that belongs to `address`, or any usable key if address filtering yields
    nothing (defensive — the exact response shape isn't in public docs)."""
    items = list_response_json
    if not isinstance(items, list):
        items = items.get("keys") or items.get("data") or []
    mine = [
        k for k in items
        if isinstance(k, dict) and str(k.get("address", "")).lower() == address.lower()
    ]
    mine.sort(key=lambda k: str(k.get("createdAt", "")), reverse=True)
    for k in (mine or [k for k in items if isinstance(k, dict)]):
        v = k.get("apiKey") or k.get("api_key") or k.get("key")
        if v:
            return v
    return None


def mint_relayer_api_key(acct, cache_path: str | None = None, force: bool = False) -> str:
    """Return a usable Polymarket relayer API key for `acct` (an eth_account
    LocalAccount), REUSING a cached/existing key whenever possible so this
    address never hits the relayer's 100-key-per-address cap.

    Order of operations per attempt (fresh nonce every retry — a stale/racy
    SIWE nonce can 400/401 transiently, verified live 2026-07-05):
      1. On-disk cache hit (unless force=True) -> return immediately, 0 network calls.
      2. SIWE sign-in (gamma /nonce -> /login) to get a Gamma bearer.
      3. LIST existing relayer keys for this address (Gamma-auth'd GET) and
         reuse the newest one if any exists.
      4. Only if none exists: MINT one (Gamma-auth'd POST /relayer/api/auth).
      5. Cache whatever key was returned (chmod 600) so step 1 short-circuits
         every future call.

    Raises RuntimeError if all 4 attempts fail.
    """
    cache_path = external_state_path(
        cache_path or default_cache_path(), Path(__file__).resolve().parents[3],
        "LIFE_MANAGER_RELAYER_CACHE",
    )
    if not force:
        try:
            with open(cache_path) as f:
                cached = f.read().strip()
            if cached:
                return cached
        except FileNotFoundError:
            pass

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://polymarket.com",
        "Referer": "https://polymarket.com/",
    })

    last_err = None
    for attempt in range(4):
        try:
            nonce = s.get(f"{GAMMA}/nonce", timeout=20).json().get("nonce")
            now = _dt.datetime.now(_dt.timezone.utc)
            issued = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
            fields = {
                "domain": "polymarket.com", "address": acct.address,
                "statement": "Welcome to Polymarket! Sign to connect.",
                "uri": "https://polymarket.com", "version": "1", "chainId": 137,
                "nonce": nonce, "issuedAt": issued,
            }
            plaintext = (
                f"{fields['domain']} wants you to sign in with your Ethereum account:\n"
                f"{acct.address}\n\n{fields['statement']}\n\n"
                f"URI: {fields['uri']}\nVersion: {fields['version']}\n"
                f"Chain ID: {fields['chainId']}\nNonce: {nonce}\nIssued At: {issued}"
            )
            sig_hex = "0x" + acct.sign_message(encode_defunct(text=plaintext)).signature.hex()
            combined = json.dumps(fields, separators=(",", ":")) + ":::" + sig_hex
            bearer = base64.b64encode(combined.encode()).decode()

            login = s.get(f"{GAMMA}/login", headers={"Authorization": "Bearer " + bearer}, timeout=20)
            if login.status_code != 200:
                raise RuntimeError(f"gamma /login {login.status_code}: {login.text[:150]}")

            auth = {"Authorization": "Bearer " + bearer}
            # LIST-BEFORE-MINT (the actual fix): the relayer key registry caps at
            # 100/address with no delete endpoint, so reuse before ever minting.
            lst = s.get(f"{RELAYER}/relayer/api/keys", headers=auth, timeout=20)
            api_key = _pick_existing_key(lst.json(), acct.address) if lst.status_code == 200 else None
            if not api_key:
                r = s.post(f"{RELAYER}/relayer/api/auth", headers=auth, json={}, timeout=20)
                if r.status_code != 200:
                    raise RuntimeError(f"relayer /auth {r.status_code}: {r.text[:200]}")
                data = r.json()
                api_key = data.get("apiKey") or data.get("api_key")
            if not api_key:
                raise RuntimeError("relayer returned no apiKey (neither existing nor minted)")

            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w") as f:
                    f.write(api_key)
                os.chmod(cache_path, 0o600)
            except Exception:  # noqa: BLE001 — caching is best-effort, never fail the mint
                pass
            return api_key
        except Exception as e:  # noqa: BLE001 — retried with a fresh nonce
            last_err = e
            _time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"relayer auth failed after 4 attempts: {last_err}")
