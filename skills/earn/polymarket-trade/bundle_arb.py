#!/usr/bin/env python3
"""
Polymarket BASE STRATEGY #2 — BUNDLE ARBITRAGE (risk-free, no-human).
For a binary market YES+NO must resolve to exactly $1. If ask_YES + ask_NO < $1
(minus taker fees), BUY BOTH → locked profit at resolution regardless of outcome.
Scans many markets, executes the best opportunity. Fee = rate*p*(1-p)*shares.
"""
import os, json, sys
import requests
from eth_account import Account

KEY=os.getenv("POLYGON_WALLET_PRIVATE_KEY"); KEY=KEY if KEY.startswith("0x") else "0x"+KEY
acct=Account.from_key(KEY); ADDR=acct.address
PUSD="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
SPENDERS=["0xE111180000d2663C0091e4f400237545B87B996B","0xe2222d279d744050d28e00520010520000310F59","0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"]
FEE_RATE=0.05  # conservative blended taker rate
EDGE=0.005     # require >=0.5% locked edge after fee
MAX_PASS_SPEND=float(os.getenv("MAX_PASS_SPEND","2.0"))  # fixed USD ceiling per pass (money-safety, #25 adversary fix)

# ROOT CAUSE FIX (2026-07-07): this used to carry its own mint() that POSTed a brand-new
# relayer key every pass -> hit Polymarket's 100-keys-per-address cap on 2026-07-04 ->
# every pass since crashed with KeyError:'apiKey' (error body has no apiKey field). Now
# reuses the SAME list-before-mint + cached implementation redeem.py already proved live
# (see relayer_auth.py docstring) instead of a second, drifting copy.
from relayer_auth import mint_relayer_api_key
def mint(): return mint_relayer_api_key(acct)

from polymarket.clients.secure import SecureClient
from polymarket.auth import RelayerApiKey

import no_naked

def best_ask(ob):
    asks=getattr(ob,'asks',[]) or []
    if not asks: return None,0
    a=min(asks, key=lambda x: float(getattr(x,'price',None) or x['price']))
    return float(getattr(a,'price',None) or a['price']), float(getattr(a,'size',None) or a.get('size',0))

def best_bid_price(ob):
    bids=getattr(ob,'bids',[]) or []
    if not bids: return None
    return max(float(getattr(b,'price',None) or b['price']) for b in bids)

def main():
    tmp=SecureClient._create(private_key=KEY,validate_credentials=True); creds=tmp._ctx.credentials; tmp.close()
    c=SecureClient.create(private_key=KEY,credentials=creds,api_key=RelayerApiKey(key=mint(),address=ADDR))
    ba=c.get_balance_allowance(asset_type="COLLATERAL"); avail=int(ba.balance)/1e6
    print("deposit wallet:",c._ctx.wallet,"| pUSD:",avail)
    for sp in SPENDERS:
        if int(ba.allowances.get(sp,0))<1: c.approve_erc20(token_address=PUSD,spender_address=sp,amount="max").wait()
    ms=requests.get("https://gamma-api.polymarket.com/markets?closed=false&active=true&order=volume24hr&ascending=false&limit=60",timeout=25).json()
    best=None
    scanned=0
    for m in ms:
        t=m.get("clobTokenIds")
        if not t or not m.get("enableOrderBook"): continue
        try: toks=json.loads(t) if isinstance(t,str) else t
        except: continue
        if len(toks)<2: continue
        try:
            ay,sy=best_ask(c.get_order_book(token_id=str(toks[0])))
            an,sn=best_ask(c.get_order_book(token_id=str(toks[1])))
        except Exception: continue
        scanned+=1
        if ay is None or an is None: continue
        cost=ay+an
        # locked edge = 1 - cost - fees(both legs)
        fee=FEE_RATE*ay*(1-ay)+FEE_RATE*an*(1-an)
        edge=1.0-cost-fee
        if edge>=EDGE and min(sy,sn)>=5:
            if not best or edge>best[0]: best=(edge,m.get("question","?"),str(toks[0]),str(toks[1]),ay,an,min(sy,sn))
    print(f"scanned {scanned} markets.")
    if not best:
        print("no risk-free bundle arb ≥0.5% right now (market efficient). MM keeps quoting.")
        c.close(); return 0
    edge,q,yes,no,ay,an,msz=best
    # MAX_PASS_SPEND: fixed USD ceiling for this leg (in addition to the avail*0.9 gate below,
    # take the MIN) — bounds worst-case pass spend to a fixed number regardless of wallet balance.
    budget_shares=int(MAX_PASS_SPEND/(ay+an))
    if budget_shares<5:
        print(f"HOLD: MAX_PASS_SPEND ${MAX_PASS_SPEND:.2f} can't afford 5 shares of both legs "
              f"(needs ${5*(ay+an):.2f}). No order placed this pass.")
        c.close(); return 0
    print(f"ARB FOUND: {q[:50]} | ask_YES {ay}+ask_NO {an}={ay+an:.3f} | locked edge {edge*100:.2f}%")
    shares=max(5, min(int(msz), int((avail*0.9)/(ay+an)), budget_shares))
    print(f"buying {shares} YES + {shares} NO (FOK) = locked ${shares*edge:.3f} profit...")
    # SPEC-no-naked-fills R4: the two FOK legs are NOT atomic together. Track each leg's fill; if
    # exactly one filled, immediately unwind it so no naked directional position survives this pass.
    legs=[]
    for tid,px in [(yes,ay),(no,an)]:
        o=c.create_market_order(token_id=tid, side="BUY", amount=str(round(shares*px,2)), max_price=str(round(px+0.01,3)), order_type="FOK")
        r=c.post_order(o)
        # UNVERIFIED(live SDK): FOK success signalled by ok=True / status matched|filled
        status=str(getattr(r,'status','')).lower()
        filled=bool(getattr(r,'ok',False)) or status in ("matched","filled","success")
        legs.append({"token":tid,"filled":filled})
        print(f"  leg -> ok={getattr(r,'ok',None)} status={getattr(r,'status','')} id={getattr(r,'order_id','')[:14]}")
    for u in no_naked.plan_arb_unwind(legs):
        try:
            bid=best_bid_price(c.get_order_book(token_id=u["token"]))
            if bid:
                # UNVERIFIED(live SDK): sell the lone filled leg at bid to flatten (no naked survives)
                o=c.create_limit_order(token_id=u["token"], price=str(bid), size=str(shares), side="SELL", post_only=False)
                c.post_order(o); print(f"  ARB-UNWIND sold naked leg {u['token'][:12]} {shares}@{bid}")
            else:
                print(f"  ARB-UNWIND WARN: no bid to sell naked leg {u['token'][:12]} — manual review")
        except Exception as e:
            print(f"  ARB-UNWIND failed: {str(e)[:80]}")
    c.close(); return 0

if __name__=="__main__":
    sys.exit(main())
