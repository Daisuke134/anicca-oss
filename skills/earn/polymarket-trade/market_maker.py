#!/usr/bin/env python3
"""
Polymarket BASE STRATEGY #1 — MAKER BUNDLE / MARKET MAKING (no-human).
On a binary market YES+NO resolves to exactly $1. If we rest BUY YES @ yes_bid and
BUY NO @ no_bid with yes_bid+no_bid < 1, then IF BOTH fill we locked a risk-free
profit (paid <$1 for a guaranteed $1). We spread quotes across several markets to
raise the odds both legs of at least one bundle fill. cancel-and-replace each pass.
Fee-aware; only quotes bundles with a positive margin. Fail-closed.
"""
import os, json, sys
import requests
from eth_account import Account

KEY=os.getenv("POLYGON_WALLET_PRIVATE_KEY"); KEY=KEY if KEY.startswith("0x") else "0x"+KEY
acct=Account.from_key(KEY); ADDR=acct.address
PUSD="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
SPENDERS=["0xE111180000d2663C0091e4f400237545B87B996B","0xe2222d279d744050d28e00520010520000310F59","0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"]
MIN_SIZE=5           # CLOB min order = 5 shares
MAX_MARKETS=3        # spread across up to 3 markets
MARGIN=0.995         # require yes_bid+no_bid <= 0.995 (locked >=0.5% if both fill)
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

import positions as pos_mod
import no_naked

def best_bid(ob):
    bids=getattr(ob,'bids',[]) or []
    if not bids: return None
    return max(float(getattr(b,'price',None) or b['price']) for b in bids)

def best_ask(ob):
    asks=getattr(ob,'asks',[]) or []
    if not asks: return None
    return min(float(getattr(a,'price',None) or a['price']) for a in asks)

def _fetch_holdings(wallet):
    """data-api /positions -> per-token holdings (pure parser). Fail-closed to []."""
    try:
        r=requests.get("https://data-api.polymarket.com/positions",
                       params={"user":wallet,"sizeThreshold":"0"},timeout=25)
        return pos_mod.parse_positions_by_token(r.text)
    except Exception as e:
        print("  positions fetch failed:", str(e)[:60]); return []

def _pairs_for(holdings):
    """For each held conditionId, resolve its (yes_token,no_token) from gamma. Fail-closed."""
    pairs=[]; seen=set()
    for h in holdings:
        cid=h.get("conditionId")
        if not cid or cid in seen: continue
        seen.add(cid)
        try:
            mk=requests.get("https://gamma-api.polymarket.com/markets",
                            params={"condition_ids":cid},timeout=20).json()
            if not mk: continue
            t=mk[0].get("clobTokenIds"); toks=json.loads(t) if isinstance(t,str) else t
            if not toks or len(toks)<2: continue
            pairs.append({"market":mk[0].get("question","?"),
                          "yes_token":str(toks[0]),"no_token":str(toks[1])})
        except Exception: continue
    return pairs

def _flatten_naked(c):
    """SPEC-no-naked-fills R3: detect naked single-leg holdings and neutralize them BEFORE any new
    quoting. Returns True if a naked leg was present this pass (caller then skips new quotes,
    fail-closed). Pure decisions come from no_naked.*; only execution touches the CLOB."""
    holdings=_fetch_holdings(c._ctx.wallet)
    naked=no_naked.naked_legs(holdings,_pairs_for(holdings))
    if not naked: return False
    for sp in SPENDERS:
        try:
            ba=c.get_balance_allowance(asset_type="COLLATERAL")
            if int(ba.allowances.get(sp,0))<1: c.approve_erc20(token_address=PUSD,spender_address=sp,amount="max").wait()
        except Exception: pass
    for leg in naked:
        try:
            ma=best_ask(c.get_order_book(token_id=leg["missing_token"]))
            hb=best_bid(c.get_order_book(token_id=leg["held_token"]))
        except Exception: ma,hb=None,None
        plan=no_naked.plan_naked_fix(leg,{"missing_ask":ma,"held_bid":hb})
        size=int(leg["held_size"])
        if size<=0 or plan["action"]=="none":
            print(f"  NAKED-FIX skip ({plan['reason']}) mkt={str(leg.get('market'))[:30]}"); continue
        try:
            if plan["action"]=="complete":
                # UNVERIFIED(live SDK): buy missing leg as FOK taker to lock a full bundle
                o=c.create_market_order(token_id=plan["token"],side="BUY",
                    amount=str(round(size*plan["price"],2)),
                    max_price=str(round(plan["price"]+0.01,3)),order_type="FOK")
                c.post_order(o); print(f"  NAKED-FIX complete {plan['token'][:10]} {size}@{plan['price']}")
            else:  # sell
                # UNVERIFIED(live SDK): sell held leg at bid as marketable limit to flatten
                o=c.create_limit_order(token_id=plan["token"],price=str(plan["price"]),
                    size=str(size),side="SELL",post_only=False)
                c.post_order(o); print(f"  NAKED-FIX sell {plan['token'][:10]} {size}@{plan['price']}")
        except Exception as e:
            print(f"  NAKED-FIX exec failed ({plan['action']}): {str(e)[:80]}")
    return True

def main():
    tmp=SecureClient._create(private_key=KEY,validate_credentials=True); creds=tmp._ctx.credentials; tmp.close()
    c=SecureClient.create(private_key=KEY,credentials=creds,api_key=RelayerApiKey(key=mint(),address=ADDR))
    try: c.cancel_all(); print("  cancel-and-replace: cleared old quotes")
    except Exception as e: print("  cancel_all:", str(e)[:60])
    # SPEC-no-naked-fills R3: neutralize any naked single-leg position BEFORE quoting. If one was
    # present, do NOT add new maker quotes this pass (fail-closed — never grow naked exposure).
    if _flatten_naked(c):
        print("  naked leg handled this pass; skipping new maker quotes (fail-closed).")
        c.close(); return 0
    ba=c.get_balance_allowance(asset_type="COLLATERAL"); avail=int(ba.balance)/1e6
    print("deposit wallet pUSD:", avail)
    # BALANCE FLOOR (adversary fix): a two-sided min-size bundle costs ~MIN_SIZE*1.0 pUSD.
    # If we can't afford even one bundle, HOLD this pass instead of spamming failed orders.
    if avail < MIN_SIZE * 1.0:
        print(f"  HOLD: cash ${avail:.2f} < one min bundle (~${MIN_SIZE*1.0:.2f}). "
              f"Not placing (positions will free cash on resolution). No churn.")
        c.close(); return 0
    for sp in SPENDERS:
        if int(ba.allowances.get(sp,0))<1: c.approve_erc20(token_address=PUSD,spender_address=sp,amount="max").wait()

    ms=requests.get("https://gamma-api.polymarket.com/markets?closed=false&active=true&order=volume24hr&ascending=false&limit=50",timeout=25).json()
    picks=[]
    for m in ms:
        t=m.get("clobTokenIds")
        if not t or not m.get("enableOrderBook"): continue
        try: toks=json.loads(t) if isinstance(t,str) else t
        except: continue
        if len(toks)<2: continue
        try:
            by=best_bid(c.get_order_book(token_id=str(toks[0])))
            bn=best_bid(c.get_order_book(token_id=str(toks[1])))
        except Exception: continue
        if by is None or bn is None: continue
        s=by+bn
        if 0.20<by<0.80 and s<=MARGIN:   # profitable bundle if both fill
            picks.append((s, m.get("question","?"), str(toks[0]), str(toks[1]), round(by,3), round(bn,3)))
        if len(picks)>=MAX_MARKETS: break
    if not picks:
        print("no profitable maker-bundle market found this pass."); c.close(); return 0
    # MAX_PASS_SPEND: fixed USD ceiling for the TOTAL quoted across all markets this pass (in
    # addition to the avail*0.98 gate above, take the MIN) — bounds worst-case pass spend to a
    # fixed number regardless of wallet balance (#25 adversary fix).
    # BUG FIX (2026-07-10): the buffer was 0.9 (10% haircut). Orders rest post_only (price is
    # locked at placement, no fill-time slippage), so a 10% haircut had no risk-reduction purpose
    # but silently contradicted the entry gate at line 50 (`avail < MIN_SIZE*1.0`, i.e. "$5.00 is
    # enough to try"): at exactly the wallet's real balance ($5.00) and typical near-$1 bundle
    # prices (~$0.98/share, needing ~$4.90 for MIN_SIZE=5), 0.9*5.00=$4.50 could never clear
    # $4.90 — the wallet was permanently unfundable regardless of the dilution fix above. 0.98
    # still reserves a real cash buffer while no longer defeating the wallet's own entry gate.
    total_budget = min(avail*0.98, MAX_PASS_SPEND)
    # BUG FIX (2026-07-10): dividing total_budget evenly across len(picks) (up to MAX_MARKETS=3)
    # left per_market_budget below MIN_SIZE*price for weeks whenever total_budget was near the
    # MAX_PASS_SPEND cap (avail flatlined at $5.00 for 534/15216 earner.log lines; real orders
    # placed only twice since 2026-07-04) -- the cap made every market unfundable instead of
    # funding fewer markets fully. Fund as many picks as the budget can actually clear at
    # MIN_SIZE, cheapest bundle first, instead of pre-dividing among all picks regardless of
    # viability.
    picks.sort(key=lambda p: p[0])
    funded=[]; remaining=total_budget
    for pick in picks:
        min_cost = pick[0]*MIN_SIZE
        if remaining >= min_cost:
            funded.append(pick); remaining -= min_cost
    if not funded:
        print(f"  HOLD: budget ${total_budget:.2f} can't fund even the cheapest bundle "
              f"(needs ${picks[0][0]*MIN_SIZE:.2f}). No order placed.")
        c.close(); return 0
    picks = funded
    per_market_budget = total_budget/len(picks)
    placed=0
    for s,q,yes,no,by,bn in picks:
        # size so both legs stay within this market's share of the fixed per-pass budget
        size = int(per_market_budget/(by+bn))
        if size < MIN_SIZE:
            print(f"MKT {q[:42]:42} HOLD: budget ${per_market_budget:.2f} < {MIN_SIZE} sh both legs "
                  f"(needs ${MIN_SIZE*(by+bn):.2f}). Skipping, no order placed.")
            continue
        size = min(size, 200)
        print(f"MKT {q[:42]:42} YES {by}+NO {bn}={s:.3f} lock {(1-s)*100:.1f}% -> {size} sh/leg")
        for tid,px in [(yes,by),(no,bn)]:
            try:
                o=c.create_limit_order(token_id=tid, price=str(px), size=str(size), side="BUY", post_only=True)
                r=c.post_order(o)
                print(f"    {'YES' if tid==yes else 'NO '} {size}@{px} ok={getattr(r,'ok',None)} status={getattr(r,'status','')} id={getattr(r,'order_id','')[:12]}")
                placed+=1
            except Exception as e:
                print(f"    leg FAILED: {str(e)[:90]}")
    print(f"placed {placed} maker legs across {len(picks)} markets. Bundle profit locks when both legs of a market fill.")
    c.close(); return 0

if __name__=="__main__":
    sys.exit(main())
