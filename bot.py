import os, time, requests, ccxt
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
DRY_RUN = os.getenv("LIVE","0") != "1"
BASE = "https://contract.mexc.com/api/v1/contract"

ex = ccxt.mexc({"apiKey":API_KEY,"secret":SECRET,"options":{"defaultType":"swap"},"enableRateLimit":True})

def get(u, params=None):
    try: return requests.get(u, params=params, timeout=15).json()
    except: return {}

def bal():
    try: return float((ex.fetch_balance({"type":"swap"}).get("USDT") or {}).get("free") or 0)
    except: return 0

def scan_all(next_slot):
    rows = get(f"{BASE}/ticker").get("data") or []
    cand = []
    for d in rows:
        if not str(d.get("symbol","")).endswith("_USDT"): continue
        try:
            rate = float(d.get("fundingRate") or 0)
            if abs(rate) < 0.005: continue
            cand.append((d["symbol"], rate))
        except: continue
    valid=[]
    for sym, rate in cand:
        try:
            fr = get(f"{BASE}/funding_rate/{sym}").get("data") or {}
            nxt = int(fr.get("nextSettleTime",0))/1000
            if abs(nxt - next_slot) < 180:
                valid.append((sym, rate, fr.get("collectCycle",4)))
        except: pass
    return sorted(valid, key=lambda x: -abs(x[1]))[:3]

ex.load_markets()
print(f"🤖 BOT V3 FINAL | ALL {len(get(f'{BASE}/ticker').get('data') or [])} perps | >=0.5% INF highest | T-45s T+3s | 99%*5x | Killer -5% | {'DRY' if DRY_RUN else 'LIVE'}")

while True:
    now = datetime.now(timezone.utc)
    next_slot = None
    for d in (0,1):
        for h in [0,4,8,12,16,20]:
            t = (now+timedelta(days=d)).replace(hour=h,minute=0,second=0,microsecond=0)
            if t.timestamp() > time.time()+60:
                if not next_slot or t.timestamp() < next_slot:
                    next_slot = t.timestamp()
    wait = next_slot - time.time() - 45
    if wait > 0:
        print(f"Next {datetime.fromtimestamp(next_slot, timezone.utc):%H:%M} UTC in {wait/60:.1f}min | Bal ${bal():.2f} | ALL scan ready")
        if wait > 60: time.sleep(wait-5)
        else: time.sleep(wait)
    while time.time() < next_slot - 45.2: time.sleep(0.05)
    
    wallet = bal()
    if wallet < 1:
        print("Low bal"); time.sleep(10); continue
    
    print(f"\n⏰ SCAN ALL {datetime.fromtimestamp(next_slot, timezone.utc):%H:%M} UTC | Wallet ${wallet:.2f}")
    picks = scan_all(next_slot)
    if not picks:
        print("❌ No >=0.5% settling now - wait next slot"); time.sleep(10); continue
    
    total = wallet * 0.99 * 5
    per = total / len(picks)
    print(f"🔥 Pick highest {len(picks)}: {', '.join([f'{s} {r*100:+.2f}%' for s,r,_ in picks])} | Size ${total:.0f}")
    
    entries=[]
    for raw, rate, cycle in picks:
        sym = raw.replace("_","/")+":USDT"
        side = "sell" if rate>0 else "buy"
        try:
            ex.set_leverage(5, sym, params={"openType":1,"positionType":2 if side=="sell" else 1})
            m = ex.market(sym)
            ob = ex.fetch_order_book(sym,10)
            best = ob["asks"][0][0] if side=="buy" else ob["bids"][0][0]
            cs = float(m.get('contractSize') or 1)
            amt = float(ex.amount_to_precision(sym, per/(best*cs)))
            if not DRY_RUN:
                ex.create_order(sym,"market",side,amt,None,{"openType":1})
                time.sleep(0.3)
                pos = ex.fetch_positions([sym])
                entry = float(pos[0]["entryPrice"]) if pos and pos[0].get("entryPrice") else best
                amt = float(pos[0]["contracts"]) if pos else amt
            else: entry = best
            entries.append({"sym":sym,"raw":raw,"side":side,"entry":entry,"amt":amt,"rate":rate})
            print(f"🚀 ENTER {raw} {side.upper()} {rate*100:+.2f}% @ {entry}")
        except Exception as e:
            print(f"Enter fail {raw}: {e}")
    
    print(f"⏳ Hold until T+3s | Killer if -5% price (-25% @5x)")
    t_closed=set()
    while time.time() < next_slot + 3.0 and len(t_closed) < len(entries):
        now_sec = time.time() - next_slot
        for i,e in enumerate(entries):
            if i in t_closed: continue
            try:
                px = ex.fetch_ticker(e["sym"])["last"]
                chg = (px - e["entry"])/e["entry"] if e["side"]=="buy" else (e["entry"]-px)/e["entry"]
                if chg <= -0.05:
                    print(f"💥 KILLER {e['raw']} {chg*100:.2f}% T+{now_sec:.2f}s -> CLOSE")
                    if not DRY_RUN:
                        ex.create_order(e["sym"],"market","buy" if e["side"]=="sell" else "sell",e["amt"],None,{"openType":1,"reduceOnly":True})
                    t_closed.add(i)
            except: pass
        time.sleep(0.05)
    
    for i,e in enumerate(entries):
        if i not in t_closed:
            try:
                if not DRY_RUN: ex.create_order(e["sym"],"market","buy" if e["side"]=="sell" else "sell",e["amt"],None,{"openType":1,"reduceOnly":True})
                print(f"✅ CLOSE {e['raw']} T+3s")
            except: pass
    
    print(f"✅ DONE ${wallet:.2f} -> ${bal():.2f}\n")
    time.sleep(5)
