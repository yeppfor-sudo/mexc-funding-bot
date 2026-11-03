import os, time, requests, ccxt
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
DRY_RUN = os.getenv("LIVE","0") != "1"
BASE = "https://contract.mexc.com/api/v1/contract"

ENTRY_BEFORE = 35
EXIT_AFTER = 3
LEVERAGE = 3
WALLET_PCT = 0.99  # Use 99% always

ex = ccxt.mexc({"apiKey":API_KEY,"secret":SECRET,"options":{"defaultType":"swap"},"enableRateLimit":True})

def get(u, p=None):
    try: return requests.get(u, params=p, timeout=12).json()
    except: return {}

def bal():
    try: return float((ex.fetch_balance({"type":"swap"}).get("USDT") or {}).get("free") or 0)
    except: return 0

def check_book(sym, side, need):
    try:
        ob = ex.fetch_order_book(sym, 50)  # deeper book for big wallets
        asks, bids = ob["asks"], ob["bids"]
        if not asks or not bids: return False, 999, 0, 0
        best_ask, best_bid = asks[0][0], bids[0][0]
        spread = (best_ask-best_bid)/best_ask*100
        if spread > 0.15: return False, spread, 0, 0

        depth=0
        limit = best_ask*1.001 if side=="buy" else best_bid*0.999
        book = asks if side=="buy" else bids
        for pr, am in book:
            if side=="buy" and pr>limit: break
            if side=="sell" and pr<limit: break
            depth+=pr*am
        
        # INFINITE FIX: depth must be 1.5x need, works for $10 or $10k
        if depth < need*1.5: return False, spread, depth, 0

        filled=cost=0
        for pr, am in book:
            if filled>=need: break
            take=min(pr*am, need-filled)
            cost+=take; filled+=take
        avg=cost/filled if filled else best_ask
        slip=abs(avg-(best_ask if side=="buy" else best_bid))/best_ask*100
        if slip>0.1: return False, spread, depth, slip
        return True, spread, depth, slip
    except:
        return False, 999, 0, 0

def scan_best(next_slot, wallet):
    rows = get(f"{BASE}/ticker").get("data") or []
    cand=[]
    for d in rows:
        if not str(d.get("symbol","")).endswith("_USDT"): continue
        try:
            rate=float(d.get("fundingRate") or 0)
            if abs(rate)<0.005: continue
            fr=get(f"{BASE}/funding_rate/{d['symbol']}").get("data") or {}
            nxt=int(fr.get("nextSettleTime",0))/1000
            if abs(nxt-next_slot)>180: continue
            cyc=int(fr.get("collectCycle",4))
            cand.append((d["symbol"], rate, cyc))
        except: continue
    if not cand: return None
    cand.sort(key=lambda x: -abs(x[1]))

    need = wallet*WALLET_PCT*LEVERAGE
    for sym_raw, rate, cyc in cand:
        sym=sym_raw.replace("_","/")+":USDT"
        side="sell" if rate>0 else "buy"
        ok, spread, depth, slip = check_book(sym, side, need)
        if ok:
            return (sym_raw, rate, cyc, spread, depth, slip), cand
        else:
            print(f"⏭️ Skip {sym_raw} {rate*100:+.2f}% spread {spread:.3f}% depth ${depth:.0f} < need ${need*1.5:.0f}")
    return None

ex.load_markets()
print(f"🤖 BOT V3.6 INFINITE | T-{ENTRY_BEFORE}s T+{EXIT_AFTER}s | 1 trade | {LEVERAGE}x {int(WALLET_PCT*100)}% | >=0.5% | Book+Slip | Killer -2% | 4h/8h | {'LIVE' if not DRY_RUN else 'DRY'}")

while True:
    now=datetime.now(timezone.utc)
    next_slot=None
    for d in (0,1):
        for h in [0,4,8,12,16,20]:
            t=(now+timedelta(days=d)).replace(hour=h,minute=0,second=0,microsecond=0)
            if t.timestamp()>time.time()+60:
                if not next_slot or t.timestamp()<next_slot: next_slot=t.timestamp()
    dt=datetime.fromtimestamp(next_slot, timezone.utc)
    slot_name="BOTH 4H+8H" if dt.hour in [0,8,16] else "ONLY 4H"
    wait=next_slot-time.time()-ENTRY_BEFORE
    if wait>0:
        # Show dynamic wallet
        w=bal()
        print(f"Next {dt:%H:%M} UTC [{slot_name}] in {wait/60:.1f}min | Bal ${w:.2f} | Notional ${w*WALLET_PCT*LEVERAGE:.2f} | Entry T-{ENTRY_BEFORE}s")
        time.sleep(wait-5 if wait>60 else wait)
    while time.time()<next_slot-ENTRY_BEFORE-0.2: time.sleep(0.05)

    wallet=bal()
    # INFINITE FIX: Only skip if balance is dust, not $5
    if wallet < 1:
        print(f"Balance ${wallet:.2f} dust - waiting deposit"); time.sleep(10); continue

    result=scan_best(next_slot, wallet)
    if not result:
        print(f"❌ No >=0.5% liquid at {dt:%H:%M} [{slot_name}]"); time.sleep(10); continue

    (sym_raw, rate, cyc, spread, depth, slip), all_cand = result
    sym=sym_raw.replace("_","/")+":USDT"
    side="sell" if rate>0 else "buy"
    sname="SHORT" if rate>0 else "LONG"
    need=wallet*WALLET_PCT*LEVERAGE

    print(f"\n⏰ {dt:%H:%M} UTC [{slot_name}] | Wallet ${wallet:.2f} -> Notional ${need:.2f} ({LEVERAGE}x)")
    print(f"Settling: {', '.join([f'{s} {r*100:+.2f}% {c}H' for s,r,c in all_cand[:5]])}")
    print(f"🔥 PICK: {sym_raw} {rate*100:+.3f}% {cyc}H -> {sname} | Spread {spread:.3f}% Depth ${depth:.0f} Slip {slip:.3f}%")

    try:
        ex.set_leverage(LEVERAGE, sym, params={"openType":1,"positionType":2 if side=="sell" else 1})
        ob=ex.fetch_order_book(sym,5)
        best=ob["asks"][0][0] if side=="buy" else ob["bids"][0][0]
        m=ex.market(sym); cs=float(m.get('contractSize') or 1)
        amt=float(ex.amount_to_precision(sym, need/(best*cs)))

        if not DRY_RUN:
            ex.create_order(sym,"market",side,amt,None,{"openType":1})
            time.sleep(0.5)
            pos=ex.fetch_positions([sym])
            entry=float(pos[0]["entryPrice"]) if pos and pos[0].get("entryPrice") else best
            amt=float(pos[0]["contracts"]) if pos else amt
            sp=entry*0.98 if side=="buy" else entry*1.02
            try:
                ex.create_order(sym,"market","buy" if side=="sell" else "sell",amt,None,{"openType":1,"reduceOnly":True,"stopPrice":sp})
                print(f"🛡️ STOP -2% at {sp:.6f}")
            except: pass
        else: entry=best

        print(f"🚀 ENTER T-{ENTRY_BEFORE}s @ {entry}")

        closed=False
        while time.time()<next_slot+EXIT_AFTER:
            try:
                px=ex.fetch_ticker(sym)["last"]
                chg=(px-entry)/entry if side=="buy" else (entry-px)/entry
                if chg<=-0.02 and not DRY_RUN and not closed:
                    ex.create_order(sym,"market","buy" if side=="sell" else "sell",amt,None,{"openType":1,"reduceOnly":True})
                    closed=True; print(f"💥 KILLER -2% {chg*100:.2f}%"); break
            except: pass
            time.sleep(0.05)

        if not closed and not DRY_RUN:
            try: ex.create_order(sym,"market","buy" if side=="sell" else "sell",amt,None,{"openType":1,"reduceOnly":True})
            except: pass
            try: ex.cancel_all_orders(sym)
            except: pass

        nb=bal()
        gain=need*abs(rate); fee=need*0.0004; slip_cost=need*slip/100
        print(f"💰 ${wallet:.2f} -> ${nb:.2f} | +${gain:.2f} fund -${fee:.2f} fee -${slip_cost:.2f} slip | Est +${gain-fee-slip_cost:.2f} | Real ${nb-wallet:+.4f} | Wallet {nb/wallet*100-100:+.2f}%\n")

    except Exception as e:
        print(f"Fail {sym_raw}: {e}")
        try: ex.cancel_all_orders(sym)
        except: pass
    time.sleep(5)
