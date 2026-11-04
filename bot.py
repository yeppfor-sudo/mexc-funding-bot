import os, time, hmac, hashlib, requests, json
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
    txt = r.text
    if not txt or len(txt) < 5:
        print(f"EMPTY response status {r.status_code}")
        return None
    if txt.strip().startswith("<"):
        print(f"HTML BLOCKED: {txt[:300]}")
        return None
    try:
        return r.json()
    except Exception as e:
        print(f"JSON fail: {txt[:500]} err {e}")
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
            print(f"Balance OK: {j}")
            data = j.get("data",[])
            if isinstance(data, list):
                for a in data:
                    if a.get("currency")=="USDT":
                        return float(a.get("equity") or a.get("available") or 6.5)
            return 6.5
        except Exception as e:
            print(f"Balance exception {e}\n{traceback.format_exc()}")
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
            if rates: return rates
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
        print(f"ORDER {symbol} {side} -> {r.text[:600]}")
        return r.text
    except Exception as e:
        print(f"Order err {e}")
        return ""

def get_next_funding():
    now = datetime.now(timezone.utc)
    for h in [0,8,16]:
        ft = now.replace(hour=h,minute=0,second=0,microsecond=0)
        if ft > now+timedelta(seconds=70):
            return ft
    return (now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)

def main(mode="scan"):
    print(f"=== SNIPER v8 BULLETPROOF mode={mode} ===")
    print(f"API_KEY present? {len(API_KEY)>10} SECRET {len(API_SECRET)>10}")

    if mode=="scan":
        rates = get_funding_rates()
        rates = [r for r in rates if abs(r["rate"])>=0.0001]
        rates.sort(key=lambda x: abs(x["rate"]), reverse=True)
        print(f"Found {len(rates)} pairs")
        for r in rates[:20]:
            print(f"{r['symbol']} {r['rate']*100:.4f}% price {r['price']}")
        print("SCAN DONE - if you see list, MEXC NOT blocked")
        return

    balance = get_balance()
    print(f"START balance {balance}")

    while True:
        nxt = get_next_funding()
        wait = (nxt - datetime.now(timezone.utc)).total_seconds()
        print(f"Next {nxt} UTC in {int(wait)}s sleep 30")
        if wait > 300:
            time.sleep(30); continue
        if wait > 75:
            time.sleep(10); continue

        if 35 <= wait <= 75:
            print("50sec BEFORE - SCANNING")
            rates = get_funding_rates()
            filt = [r for r in rates if abs(r["rate"])>=MIN_RATE]
            filt.sort(key=lambda x: abs(x["rate"]), reverse=True)
            print(f"Filtered {len(filt)}")
            if not filt:
                time.sleep(5); continue
            pick = filt[0]
            symbol=pick["symbol"]; rate=pick["rate"]; price=pick["price"]
            side_text="LONG" if rate<0 else "SHORT"
            side_code=1 if rate<0 else 3
            print(f"ARMED {symbol} {rate*100:.4f}% {side_text}")
            place_order(symbol, side_code, balance*ENTRY_PCT*LEVERAGE)
            time.sleep(wait+20)
            # DCA check
            rates2 = get_funding_rates()
            cur = next((x for x in rates2 if x["symbol"]==symbol), None)
            cur_price = cur["price"] if cur else price
            diff = (cur_price-price)/price*100
            print(f"After funding diff {diff:.2f}%")
            if (side_text=="LONG" and diff<=-1) or (side_text=="SHORT" and diff>=1):
                print(f"DCA 29% {symbol}")
                place_order(symbol, side_code, balance*DCA_PCT*LEVERAGE)
            # Exit at avg
            for _ in range(30):
                rn = get_funding_rates()
                c = next((x for x in rn if x["symbol"]==symbol), None)
                if not c: 
                    time.sleep(10); continue
                pnl = (c["price"]-price)/price*100 if side_text=="LONG" else (price-c["price"])/price*100
                print(f"HOLD {symbol} pnl {pnl:.3f}%")
                if pnl>=0.05 or pnl<=-3:
                    close_side=2 if side_text=="LONG" else 4
                    place_order(symbol, close_side, balance*0.99*LEVERAGE)
                    print(f"CLOSED {pnl:.3f}%")
                    break
                time.sleep(10)
            if mode=="run_once":
                return
        time.sleep(5)

if __name__=="__main__":
    import sys
    main(sys.argv[1] if len(sys.argv)>1 else "scan")
