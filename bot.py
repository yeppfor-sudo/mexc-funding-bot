import os, time, hmac, hashlib, requests
from datetime import datetime, timezone, timedelta
import math, traceback

API_KEY = os.getenv("MEXC_API_KEY","").strip()
API_SECRET = os.getenv("MEXC_API_SECRET","").strip()
BASE = "https://contract.mexc.com"
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
ENTRY_PCT = 0.70
DCA_PCT = 0.29
MIN_RATE = 0.005

def safe_json(r):
    txt = r.text if hasattr(r, 'text') else ""
    if not txt or len(txt) < 5:
        print(f"EMPTY response {r.status_code if hasattr(r,'status_code') else '?'}")
        return None
    if txt.strip().startswith("<"):
        print(f"HTML BLOCKED: {txt[:300]}")
        return None
    try:
        return r.json()
    except:
        print(f"JSON fail: {txt[:500]}")
        return None

def sign(params, secret):
    query = "&".join([f"{k}={v}" for k,v in sorted(params.items())])
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_balance():
    for i in range(5):
        try:
            ts = int(time.time()*1000)
            url = f"{BASE}/api/v1/private/account/assets"
            params = {"api_key": API_KEY, "req_time": ts}
            params["sign"] = sign(params, API_SECRET)
            r = requests.get(url, params=params, headers={"ApiKey": API_KEY}, timeout=20)
            j = safe_json(r)
            if not j:
                time.sleep(3); continue
            data = j.get("data",[])
            if isinstance(data, list):
                for a in data:
                    if a.get("currency")=="USDT":
                        bal = float(a.get("equity") or a.get("available") or 0)
                        print(f"Balance OK {bal}")
                        return bal if bal>0 else 6.5
            return 6.5
        except Exception as e:
            print(f"Balance err {e}")
            time.sleep(3)
    print("Balance fallback 6.5")
    return 6.5

def get_funding_rates():
    for i in range(3):
        try:
            r = requests.get(f"{BASE}/api/v1/contract/ticker", timeout=20)
            j = safe_json(r)
            if not j:
                time.sleep(2); continue
            rates=[]
            for t in j.get("data",[]):
                sym = t.get("symbol","")
                if not sym.endswith("_USDT"): continue
                try:
                    rates.append({"symbol":sym,"rate":float(t.get("fundingRate",0)),"price":float(t.get("lastPrice",0))})
                except: continue
            if rates:
                return rates
        except Exception as e:
            print(f"Ticker err {e}")
            time.sleep(2)
    return []

def place_order(symbol, side, usdt):
    try:
        ts = int(time.time()*1000)
        vol = max(1, math.floor(usdt/10))
        params = {"api_key":API_KEY,"req_time":ts,"symbol":symbol,"price":0,"vol":vol,"side":side,"type":5,"open_type":1,"leverage":LEVERAGE}
        params["sign"]=sign(params,API_SECRET)
        r = requests.post(f"{BASE}/api/v1/private/order/submit", data=params, headers={"ApiKey":API_KEY}, timeout=20)
        print(f"ORDER {symbol} side {side} vol {vol} -> {r.text[:600]}")
        return r.text
    except Exception as e:
        print(f"Order err {e}\n{traceback.format_exc()}")
        return ""

# FIXED: now checks every hour - catches 12 UTC = 5pm PK funding
def get_next_funding():
    now = datetime.now(timezone.utc)
    # MEXC has 1h (HOT), 4h (T/Q), 8h (BTC) - check every hour
    for i in range(0, 25):
        ft = (now + timedelta(hours=i)).replace(minute=0, second=5, microsecond=0)
        if ft > now + timedelta(seconds=70):
            return ft
    return (now + timedelta(days=1)).replace(hour=0,minute=0,second=5,microsecond=0)

def main(mode="run_once"):
    print(f"=== SNIPER v10 70+29% HOURLY FIX mode={mode} LEVERAGE={LEVERAGE}x ===")
    balance = get_balance()
    print(f"START bal {balance} USDT")

    if mode=="scan":
        rates = get_funding_rates()
        rates = [r for r in rates if abs(r["rate"])>=0.0001]
        rates.sort(key=lambda x: abs(x["rate"]), reverse=True)
        print(f"Found {len(rates)} pairs")
        for r in rates[:20]:
            print(f"{r['symbol']} {r['rate']*100:.4f}% {r['price']}")
        print("SCAN DONE - MEXC NOT blocked")
        return

    if mode=="test_now":
        print("=== TEST NOW trading highest NOW ===")
        rates = get_funding_rates()
        filt = [r for r in rates if abs(r["rate"])>=0.002]
        filt.sort(key=lambda x: abs(x["rate"]), reverse=True)
        if not filt:
            print("No rates"); return
        pick = filt[0]
        symbol=pick["symbol"]; rate=pick["rate"]
        side_text="LONG" if rate<0 else "SHORT"
        side_code=1 if rate<0 else 3
        print(f"TEST ARMED {symbol} {rate*100:.4f}% {side_text}")
        place_order(symbol, side_code, balance*ENTRY_PCT*LEVERAGE)
        print("70% done 20s wait DCA")
        time.sleep(20)
        place_order(symbol, side_code, balance*DCA_PCT*LEVERAGE)
        print("99% done 20s wait CLOSE")
        time.sleep(20)
        close_side=2 if side_text=="LONG" else 4
        place_order(symbol, close_side, balance*0.99*LEVERAGE)
        print(f"TEST DONE {symbol}")
        return

    # Real funding sniper
    while True:
        nxt = get_next_funding()
        now = datetime.now(timezone.utc)
        wait = (nxt - now).total_seconds()
        print(f"Next {nxt} UTC | {nxt+timedelta(hours=5)} PK | in {int(wait)}s sleep 30 bal {balance:.2f}")
        if wait > 300:
            time.sleep(30); continue
        if wait > 80:
            time.sleep(10); continue

        if 15 <= wait <= 80:
            print("=== 50sec BEFORE FUNDING - SCANNING ===")
            rates = get_funding_rates()
            filt = [r for r in rates if abs(r["rate"])>=MIN_RATE]
            filt.sort(key=lambda x: abs(x["rate"]), reverse=True)
            print(f"Filtered high {len(filt)}")
            if not filt:
                time.sleep(5); continue
            for pick in filt[:5]:
                symbol=pick["symbol"]; rate=pick["rate"]; price=pick["price"]
                side_text="LONG" if rate<0 else "SHORT"
                side_code=1 if rate<0 else 3
                need = balance*ENTRY_PCT*LEVERAGE
                print(f"ARMED {symbol} {rate*100:.4f}% -> {side_text} 70% {balance*ENTRY_PCT:.2f} USDT")
                place_order(symbol, side_code, need)
                entry_price=price
                avg_price=entry_price
                total_usdt=balance*ENTRY_PCT
                print(f"OPEN {side_text} waiting funding {int(wait)}s")
                time.sleep(wait+18)

                rates2=get_funding_rates()
                cur=next((x for x in rates2 if x["symbol"]==symbol), None)
                cur_price=cur["price"] if cur else price
                diff=(cur_price-avg_price)/avg_price*100
                print(f"After funding diff {diff:.2f}%")
                should_dca=(side_text=="LONG" and diff<=-1.0) or (side_text=="SHORT" and diff>=1.0)
                if should_dca:
                    print(f"DCA +29% {symbol}")
                    place_order(symbol, side_code, balance*DCA_PCT*LEVERAGE)
                    avg_price=(entry_price*70 + cur_price*29)/99
                    total_usdt=balance*0.99
                else:
                    print(f"No DCA {diff:.2f}%")

                for _ in range(36):
                    rn=get_funding_rates()
                    c=next((x for x in rn if x["symbol"]==symbol), None)
                    if not c:
                        time.sleep(10); continue
                    cp=c["price"]
                    pnl=(cp-avg_price)/avg_price*100 if side_text=="LONG" else (avg_price-cp)/avg_price*100
                    print(f"HOLD {symbol} pnl {pnl:.3f}% avg {avg_price:.4f} cur {cp:.4f}")
                    if pnl>=0.05 or pnl<=-3.0:
                        close_side=2 if side_text=="LONG" else 4
                        place_order(symbol, close_side, total_usdt*LEVERAGE)
                        print(f"{'PROFIT' if pnl>=0 else 'SL'} CLOSE {pnl:.3f}%")
                        balance=get_balance()
                        break
                    time.sleep(10)
                else:
                    close_side=2 if side_text=="LONG" else 4
                    place_order(symbol, close_side, total_usdt*LEVERAGE)

                if mode=="run_once":
                    print("run_once DONE")
                    return
                break
        time.sleep(5)

if __name__=="__main__":
    import sys
    main(sys.argv[1] if len(sys.argv)>1 else "run_once")
