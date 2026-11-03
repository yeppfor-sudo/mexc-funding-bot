import os, time, requests, ccxt
from datetime import datetime, timezone, timedelta

# --- CONFIG ---
API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
DRY_RUN = os.getenv("LIVE","0") != "1"  # LIVE=1 = real money
BASE = "https://contract.mexc.com/api/v1/contract"

# MEXC client
ex = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": SECRET,
    "options": {"defaultType": "swap"},
    "enableRateLimit": True
})

def get(url, params=None):
    try:
        return requests.get(url, params=params, timeout=15).json()
    except:
        return {}

def get_balance():
    try:
        bal = ex.fetch_balance({"type":"swap"})
        return float((bal.get("USDT") or {}).get("free") or 0)
    except:
        return 0

def scan_best_for_slot(next_slot_timestamp):
    """
    CORE LOGIC YOU ASKED:
    - At 00,08,16 UTC: BOTH 4h and 8h settle -> pick highest from BOTH
    - At 04,12,20 UTC: ONLY 4h settles (8h still 4h left) -> pick highest 4h only
    We check nextSettleTime to know which coins actually settle at this slot.
    """
    rows = get(f"{BASE}/ticker").get("data") or []
    settling = []
    
    for d in rows:
        if not str(d.get("symbol","")).endswith("_USDT"):
            continue
        try:
            rate = float(d.get("fundingRate") or 0)
            if abs(rate) < 0.005:  # >=0.5%
                continue
            
            # Get real next settle time
            fr = get(f"{BASE}/funding_rate/{d['symbol']}").get("data") or {}
            nxt = int(fr.get("nextSettleTime",0)) / 1000
            cycle = int(fr.get("collectCycle", 4))  # 4 = 4h, 8 = 8h
            
            # IMPORTANT: Only if this coin settles at THIS slot (within 3 min diff)
            if abs(nxt - next_slot_timestamp) > 180:
                continue
            
            settling.append((d["symbol"], rate, cycle))
        except:
            continue
    
    if not settling:
        return None
    
    # Pick highest absolute funding among settling coins
    settling.sort(key=lambda x: -abs(x[1]))
    best = settling[0]
    return best, settling

# Start
ex.load_markets()
print(f"🤖 BOT V3.2 FINAL | 1 TRADE | >=0.5% | 4h/8h RULE | T-45s T+3s | 99%*5x | Killer -5% | {'DRY' if DRY_RUN else 'LIVE MODE'}")

while True:
    # Find next funding slot (00,04,08,12,16,20 UTC)
    now = datetime.now(timezone.utc)
    next_slot = None
    for d in (0,1):
        for h in [0,4,8,12,16,20]:
            t = (now + timedelta(days=d)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t.timestamp() > time.time() + 60:
                if not next_slot or t.timestamp() < next_slot:
                    next_slot = t.timestamp()
    
    dt = datetime.fromtimestamp(next_slot, timezone.utc)
    is_both_slot = dt.hour in [0,8,16]
    slot_name = "BOTH 4H+8H CLOSE" if is_both_slot else "ONLY 4H CLOSE"
    
    wait = next_slot - time.time() - 45
    if wait > 0:
        print(f"Next {dt:%H:%M} UTC [{slot_name}] in {wait/60:.1f} min | Bal ${get_balance():.2f} | Scanning ALL...")
        if wait > 60:
            time.sleep(wait - 5)
        else:
            time.sleep(wait)
    
    # Wait exact T-45s
    while time.time() < next_slot - 45.2:
        time.sleep(0.05)
    
    wallet = get_balance()
    if wallet < 1:
        print(f"Low balance ${wallet:.2f} - need $20+")
        time.sleep(10)
        continue
    
    # SCAN
    result = scan_best_for_slot(next_slot)
    if not result:
        print(f"❌ No >=0.5% coin settling at {dt:%H:%M} UTC [{slot_name}] - skip this slot")
        time.sleep(10)
        continue
    
    (sym_raw, rate, cycle), all_settling = result
    sym = sym_raw.replace("_","/") + ":USDT"
    side = "sell" if rate > 0 else "buy"
    side_name = "SHORT" if rate > 0 else "LONG"  # Positive funding -> SHORT to get funding, Negative -> LONG
    
    print(f"\n{'='*60}")
    print(f"⏰ {dt:%H:%M} UTC [{slot_name}] | Wallet ${wallet:.2f}")
    print(f"All settling now: {', '.join([f'{s} {r*100:+.2f}% {c}H' for s,r,c in all_settling[:5]])}")
    print(f"🔥 FINAL PICK: {sym_raw} {rate*100:+.3f}% {cycle}H -> {side_name}")
    print(f"{'='*60}")

    # TRADE OPEN AND CLOSE STYLE
    total_notional = wallet * 0.99 * 5  # 99% wallet * 5x leverage
    
    try:
        # STEP 1: SET LEVERAGE 5x ISOLATED
        ex.set_leverage(5, sym, params={"openType":1, "positionType":2 if side=="sell" else 1})
        
        # STEP 2: GET PRICE + AMOUNT
        ob = ex.fetch_order_book(sym, 10)
        best_price = ob["asks"][0][0] if side=="buy" else ob["bids"][0][0]
        market = ex.market(sym)
        contract_size = float(market.get('contractSize') or 1)
        amount = float(ex.amount_to_precision(sym, total_notional / (best_price * contract_size)))
        
        # STEP 3: OPEN AT T-45s - Market order
        if not DRY_RUN:
            ex.create_order(sym, "market", side, amount, None, {"openType":1})
            time.sleep(0.4)
            pos = ex.fetch_positions([sym])
            entry_price = float(pos[0]["entryPrice"]) if pos and pos[0].get("entryPrice") else best_price
            amount = float(pos[0]["contracts"]) if pos else amount
        else:
            entry_price = best_price
        
        print(f"🚀 STEP 3 OPEN: {sym_raw} {side_name} @ {entry_price} | Notional ${total_notional:.2f} | T-45s")
        print(f"   Holding for funding... funding at T=0")
        
        # STEP 4: HOLD T-45s to T+3s with KILLER -5%
        closed_by_killer = False
        while time.time() < next_slot + 3.0:
            try:
                current_price = ex.fetch_ticker(sym)["last"]
                # PnL % of position (not wallet)
                if side == "buy":
                    change = (current_price - entry_price) / entry_price
                else:
                    change = (entry_price - current_price) / entry_price
                
                if change <= -0.05:  # Price moved -5% against us
                    print(f"💥 STEP 4 KILLER: Price {change*100:.2f}% -> CLOSE IMMEDIATELY at T+{time.time()-next_slot:.1f}s")
                    if not DRY_RUN:
                        ex.create_order(sym, "market", "buy" if side=="sell" else "sell", amount, None, {"openType":1, "reduceOnly":True})
                    closed_by_killer = True
                    break
            except:
                pass
            time.sleep(0.05)
        
        # STEP 5: CLOSE AT T+3s
        if not closed_by_killer:
            print(f"✅ STEP 5 CLOSE at T+3s (after funding received)")
            if not DRY_RUN:
                ex.create_order(sym, "market", "buy" if side=="sell" else "sell", amount, None, {"openType":1, "reduceOnly":True})
        
        new_wallet = get_balance()
        pnl = new_wallet - wallet
        print(f"💰 RESULT: ${wallet:.2f} -> ${new_wallet:.2f} | PnL ${pnl:+.4f} | Funding {rate*100:+.3f}%")
        print(f"{'='*60}\n")
        
    except Exception as e:
        print(f"Trade failed {sym_raw}: {e}")
    
    time.sleep(5)
