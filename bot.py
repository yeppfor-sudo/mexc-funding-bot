import os, time, requests, ccxt
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
DRY_RUN = os.getenv("LIVE","0") != "1"
BASE = "https://contract.mexc.com/api/v1/contract"

ex = ccxt.mexc({"apiKey":API_KEY,"secret":SECRET,"options":{"defaultType":"swap"},"enableRateLimit":True})

def get(u, p=None):
    try: return requests.get(u, params=p, timeout=15).json()
    except: return {}

def bal():
    try: return float((ex.fetch_balance({"type":"swap"}).get("USDT") or {}).get("free") or 0)
    except: return 0

def check_orderbook(sym, side, total_notional):
    """Check spread and depth to estimate slippage"""
    try:
        ob = ex.fetch_order_book(sym, 20)
        asks = ob["asks"]  # [price, amount]
        bids = ob["bids"]
        if not asks or not bids:
            return False, 999, 0
        
        best_ask = asks[0][0]
        best_bid = bids[0][0]
        spread_pct = (best_ask - best_bid) / best_ask * 100
        
        # Spread must be <0.15% else slippage kills funding
        if spread_pct > 0.15:
            return False, spread_pct, 0
        
        # Check depth for our notional - can we fill within 0.1% price?
        depth_notional = 0
        limit_price = best_ask * 1.001 if side=="buy" else best_bid * 0.999
        book_side = asks if side=="buy" else bids
        
        for price, amount in book_side:
            if side=="buy" and price > limit_price: break
            if side=="sell" and price < limit_price: break
            depth_notional += price * amount
        
        # Need at least 1.5x our size in depth within 0.1%
        if depth_notional < total_notional * 1.5:
            return False, spread_pct, depth_notional
        
        # Estimate slippage = weighted avg fill vs best
        filled=0; cost=0
        for price, amount in book_side:
            if filled >= total_notional: break
            take = min(price*amount, total_notional-filled)
            cost += take
            filled += take
        avg_price = cost / filled if filled else best_ask
        slip_pct = abs(avg_price - (best_ask if side=="buy" else best_bid)) / best_ask * 100
        
        if slip_pct > 0.1:  # slippage >0.1% too high
            return False, spread_pct, depth_notional
            
        return True, spread_pct, depth_notional, slip_pct
    except Exception as e:
        return False, 999, 0

def scan_best(next_slot, wallet):
    rows = get(f"{BASE}/ticker").get("data") or []
    settling=[]
    for d in rows:
        if not str(d.get("symbol","")).endswith("_USDT"): continue
        try:
            rate = float(d.get("fundingRate") or 0)
            if abs(rate) < 0.005: continue
            fr = get(f"{BASE}/funding_rate/{d['symbol']}").get("data") or {}
            nxt = int(fr.get("nextSettleTime",0))/1000
            if abs(nxt - next_slot) > 180: continue
            cycle = int(fr.get("collectCycle",4))
            settling.append((d["symbol"], rate, cycle))
        except: continue
    if not settling: return None
    settling.sort(key=lambda x: -abs(x[1]))
    
    # Now check orderbook for each candidate in order of highest funding
    total_notional = wallet * 0.99 * 3
    for sym_raw, rate, cycle in settling:
        sym = sym_raw.replace("_","/")+":USDT"
        side = "sell" if rate>0 else "buy"
        ok, spread, depth, *rest = check_orderbook(sym, side, total_notional)
        slip = rest[0] if rest else 0
        if ok:
            return (sym_raw, rate, cycle, spread, depth, slip), settling
        else:
            print(f"⏭️ Skip {sym_raw} {rate*100:+.2f}% - spread {spread:.3f}% depth ${depth:.0f} < needed ${total_notional*1.5:.0f} -> low liquidity")
    return None

ex.load_markets()
print(f"🤖 BOT V3.4 FINAL | 1 trade | 3x 99% | >=0.5% | Orderbook+Slip check | Killer -2% | 4h/8h | {'LIVE' if not DRY_RUN else 'DRY'}")

while True:
    now = datetime.now(timezone.utc)
    next_slot = None
    for d in (0,1):
        for h in [0,4,8,12,16,20]:
            t = (now+timedelta(days=d)).replace(hour=h,minute=0,second=0,microsecond=0)
            if t.timestamp() > time.time()+60:
                if not next_slot or t.timestamp() < next_slot:
                    next_slot = t.timestamp()
    dt = datetime.fromtimestamp(next_slot, timezone.utc)
    slot_name = "BOTH 4H+8H" if dt.hour in [0,8,16] else "ONLY 4H"
    wait = next_slot - time.time() - 45
    if wait > 0:
        print(f"Next {dt:%H:%M} UTC [{slot_name}] in {wait/60:.1f}min | Bal ${bal():.2f}")
        if wait > 60: time.sleep(wait-5)
        else: time.sleep(wait)
    while time.time() < next_slot - 45.2: time.sleep(0.05)

    wallet = bal()
    if wallet < 1:
        print(f"Low balance ${wallet:.2f}"); time.sleep(10); continue
    # Check wallet min for trade
    total_notional = wallet * 0.99 * 3
    if total_notional < 10:
        print(f"Wallet too small for safe trade: notional ${total_notional:.2f}"); time.sleep(10); continue

    result = scan_best(next_slot, wallet)
    if not result:
        print(f"❌ No liquid coin >=0.5% settling at {dt:%H:%M} [{slot_name}]")
        time.sleep(10); continue
    
    (sym_raw, rate, cycle, spread, depth, slip), all_settling = result
    sym = sym_raw.replace("_","/")+":USDT"
    side = "sell" if rate>0 else "buy"
    sname = "SHORT" if rate>0 else "LONG"

    print(f"\n⏰ {dt:%H:%M} UTC [{slot_name}] | Wallet ${wallet:.2f} | Notional ${total_notional:.2f}")
    print(f"All settling: {', '.join([f'{s} {r*100:+.2f}% {c}H' for s,r,c in all_settling[:4]])}")
    print(f"🔥 PICK (orderbook OK): {sym_raw} {rate*100:+.3f}% {cycle}H -> {sname} | Spread {spread:.3f}% Depth ${depth:.0f} Slip {slip:.3f}%")

    try:
        ex.set_leverage(3, sym, params={"openType":1,"positionType":2 if side=="sell" else 1})
        ob = ex.fetch_order_book(sym,5)
        best_px = ob["asks"][0][0] if side=="buy" else ob["bids"][0][0]
        m = ex.market(sym); cs = float(m.get('contractSize') or 1)
        amt = float(ex.amount_to_precision(sym, total_notional/(best_px*cs)))
        
        if not DRY_RUN:
            ex.create_order(sym,"market",side,amt,None,{"openType":1})
            time.sleep(0.4)
            pos = ex.fetch_positions([sym])
            entry = float(pos[0]["entryPrice"]) if pos and pos[0].get("entryPrice") else best_px
            amt = float(pos[0]["contracts"]) if pos else amt
            # STOP -2%
            stop_price = entry * 0.98 if side=="buy" else entry * 1.02
            try:
                ex.create_order(sym, "market", "buy" if side=="sell" else "sell", amt, None, {"openType":1,"reduceOnly":True,"stopPrice":stop_price})
                print(f"🛡️ STOP -2% at {stop_price}")
            except: pass
        else: entry = best_px
        
        print(f"🚀 ENTER @ {entry} | Spread {spread:.3f}% Slip {slip:.3f}%")

        closed=False
        while time.time() < next_slot + 3.0:
            try:
                px = ex.fetch_ticker(sym)["last"]
                chg = (px-entry)/entry if side=="buy" else (entry-px)/entry
                if chg <= -0.02 and not DRY_RUN and not closed:
                    ex.create_order(sym,"market","buy" if side=="sell" else "sell",amt,None,{"openType":1,"reduceOnly":True})
                    closed=True; print(f"💥 KILLER -2% {chg*100:.1f}%"); break
            except: pass
            time.sleep(0.05)
        
        if not closed and not DRY_RUN:
            try: ex.create_order(sym,"market","buy" if side=="sell" else "sell",amt,None,{"openType":1,"reduceOnly":True})
            except: pass
            try: ex.cancel_all_orders(sym)
            except: pass
        
        new_bal = bal()
        # Calculate real slippage cost
        fee = total_notional * 0.0004
        funding_gain = total_notional * abs(rate)
        net = funding_gain - fee - (total_notional * slip/100)
        print(f"✅ CLOSE | ${wallet:.2f}->{new_bal:.2f} | Funding +${funding_gain:.2f} Fees -${fee:.2f} Slip -${total_notional*slip/100:.2f} | Net est +${net:.2f}\n")

    except Exception as e:
        print(f"Fail {sym_raw}: {e}")
        try: ex.cancel_all_orders(sym)
        except: pass
    time.sleep(5)
