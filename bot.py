import os, time, hmac, hashlib, requests, math, traceback
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("MEXC_API_KEY","").strip()
API_SECRET = os.getenv("MEXC_API_SECRET","").strip()
BASES = ["https://contract.mexc.com", "https://contract.mexc.io"]
BASE_TICKER = "https://contract.mexc.com"
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
ENTRY_PCT = 0.70
DCA_PCT = 0.29
MIN_RATE = 0.005

def safe_json(r):
    if not r or not hasattr(r, 'text'): return None
    txt = r.text
    if not txt or len(txt) < 5: return None
    if txt.strip().startswith("<"): 
        if "Access Denied" in txt:
            print(f"WAF BLOCKED: {txt[:300]}")
        return None
    try:
        return r.json()
    except:
        return None

def sign(params, secret):
    query = "&".join([f"{k}={v}" for k,v in sorted(params.items())])
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_balance():
    for base in BASES:
        for i in range(3):
            try:
                ts = int(time.time()*1000)
                url = f"{base}/api/v1/private/account/assets"
                params = {"api_key": API_KEY, "req_time": ts}
                params["sign"] = sign(params, API_SECRET)
                r = requests.get(url, params=params, headers={"ApiKey": API_KEY}, timeout=25)
                j = safe_json(r)
                if not j:
                    time.sleep(2); continue
                data = j.get("data",[])
                if isinstance(data, list):
                    for a in data:
                        if a.get("currency")=="USDT":
                            bal = float(a.get("equity") or a.get("available") or 0)
                            if bal>0:
                                print(f"Balance OK {bal} from {base}")
                                return bal
                if isinstance(data, dict) and "USDT" in str(data):
                    print(f"Balance raw {data}")
                time.sleep(2)
            except Exception as e:
                print(f"Balance err {base} {e}")
                time.sleep(2)
    print("Balance fallback 6.5")
    return 6.5

def get_funding_rates():
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE_TICKER}/api/v1/contract/ticker", timeout=25)
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
                print(f"Ticker OK {len(rates)} pairs")
                return rates
        except Exception as e:
            print(f"Ticker err {e}")
            time.sleep(3)
    return []

def place_order(symbol, side, usdt):
    # Try all bases for private order - fixes Access Denied
    for base in BASES:
        try:
            ts = int(time.time()*1000)
            vol = max(1, math.floor(usdt/10))
            params = {"api_key":API_KEY,"req_time":ts,"symbol":symbol,"price":0,"vol":vol,"side":side,"type":5,"open_type":1,"leverage":LEVERAGE}
            params["sign"]=sign(params,API_SECRET)
            url = f"{base}/api/v1/private/order/submit"
            r = requests.post(url, data=params, headers={"ApiKey": API_KEY}, timeout=30)
            txt = r.text
            print(f"ORDER try {base} {symbol} side {side} vol {vol} -> {txt[:600]}")
            if "Access Denied" in txt or "<HTML>" in txt:
                print(f"BLOCKED on {base}, trying next...")
                time.sleep(2)
                continue
            if '"code":200' in txt or '"success":true' in txt or '"orderId"' in txt:
                print(f"ORDER SUCCESS on {base}")
                return txt
            return txt
        except Exception as e:
            print(f"Order err {base} {e}\n{traceback.format_exc()}")
            time.sleep(2)
    print("ALL BASES BLOCKED - WARP failed")
    return "FAILED ALL BASES"

def get_next_funding():
    now = datetime.now(timezone.utc)
    for i in range(0, 25):
        ft = (now + timedelta(hours=i)).replace(minute=0, second=5, microsecond=0)
        if ft > now + timedelta(seconds=70):
            return ft
    return (now + timedelta(days=1)).replace(hour=0,minute=0,second=5,microsecond=0)

def main(mode="run_once"):
    print(f"=== SNIPER v12 FINAL FIX mode={mode} LEVERAGE={LEVERAGE}x ===")
    balance = get_balance()
    print(f"START bal {balance} USDT")

    if mode=="scan":
        rates = get_funding_rates()
        rates = [r for r in rates if abs(r["rate"])>=0.0001]
        rates.sort(key=lambda x: abs(x["rate"]), reverse=True)
        print(f"Found {len(rates)} pairs")
        for r in rates[:30]:
            print(f"{r['symbol']} {r['rate']*100:.4f}% {r['price']}")
        print("SCAN DONE")
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
        res1 = place_order(symbol, side_code, balance*ENTRY_PCT*LEVERAGE)
        if "Access Denied" in res1 or "FAILED" in res1:
            print("STOP - WAF still blocking private - check sniper.yml WARP")
            return
        print("70% done 20s wait DCA")
        time.sleep(20)
        res2 = place_order(symbol, side_code, balance*DCA_PCT*LEVERAGE)
        print(f"DCA result {res2[:200]}")
        print("99% done 20s wait CLOSE")
        time.sleep(20)
        close_side=2 if side_text=="LONG" else 4
        res3 = place_order(symbol, close_side, balance*0.99*LEVERAGE)
        print(f"TEST DONE {symbol} CLOSE {res3[:400]}")
        return

    while True:
        nxt = get_next_funding()
        now = datetime.now(timezone.utc)
        wait = (nxt - now).total_seconds()
        pk_time = nxt + timedelta(hours=5)
        print(f"Next {nxt} UTC | {pk_time} PK | in {int(wait)}s sleep 30 bal {balance:.2f}")
        if wait > 300:
            time.sleep(30); continue
        if wait > 80:
            time.sleep(10); continue
        if 15 <= wait <= 80:
            print("=== 50sec BEFORE FUNDING - SCANNING ===")
            rates = get_funding_rates()
            filt = [r for r in rates if abs(r["rate"])>=MIN_RATE]
            filt.sort(key=lambda x: abs(x["rate"]), reverse=True)
            print(f"High rates {len(filt)}")
            if not filt:
                time.sleep(5); continue
            pick = filt[0]
            symbol=pick["symbol"]; rate=pick["rate"]; price=pick["price"]
            side_text="LONG" if rate<0 else "SHORT"
            side_code=1 if rate<0 else 3
            print(f"ARMED {symbol} {rate*100:.4f}% -> {side_text} 70%")
            r1 = place_order(symbol, side_code, balance*ENTRY_PCT*LEVERAGE)
            if "Access Denied" in r1 or "FAILED" in r1:
                print("WAF BLOCK - abort this round")
                time.sleep(60); continue
            avg_price=price
            total_usdt=balance*ENTRY_PCT
            time.sleep(wait+18)
            print("After funding check DCA")
            rates2=get_funding_rates()
            cur=next((x for x in rates2 if x["symbol"]==symbol), None)
            cur_price=cur["price"] if cur else price
            diff=(cur_price-avg_price)/avg_price*100
            should_dca=(side_text=="LONG" and diff<=-1.0) or (side_text=="SHORT" and diff>=1.0)
            if should_dca:
                print(f"DCA +29% diff {diff:.2f}%")
                place_order(symbol, side_code, balance*DCA_PCT*LEVERAGE)
                avg_price=(price*70 + cur_price*29)/99
                total_usdt=balance*0.99
            for _ in range(36):
                rn=get_funding_rates()
                c=next((x for x in rn if x["symbol"]==symbol), None)
                if not c:
                    time.sleep(10); continue
                cp=c["price"]
                pnl=(cp-avg_price)/avg_price*100 if side_text=="LONG" else (avg_price-cp)/avg_price*100
                print(f"HOLD {symbol} pnl {pnl:.3f}%")
                if pnl>=0.05 or pnl<=-3.0:
                    close_side=2 if side_text=="LONG" else 4
                    place_order(symbol, close_side, total_usdt*LEVERAGE)
                    print(f"CLOSE {pnl:.3f}%")
                    balance=get_balance()
                    break
                time.sleep(10)
            else:
                close_side=2 if side_text=="LONG" else 4
                place_order(symbol, close_side, total_usdt*LEVERAGE)
            if mode=="run_once":
                print("run_once DONE")
                return
        time.sleep(5)

if __name__=="__main__":
    import sys
    main(sys.argv[1] if len(sys.argv)>1 else "run_once")
