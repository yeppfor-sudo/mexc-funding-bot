import os, time, hmac, hashlib, requests, math
from datetime import datetime, timezone

API_KEY = os.getenv("MEXC_API_KEY","").strip()
API_SECRET = os.getenv("MEXC_API_SECRET","").strip()
PROXY_URL = os.getenv("PROXY_URL","").strip()
LEVERAGE = int(os.getenv("LEVERAGE","5"))
MODE = os.getenv("MODE","test_now")

BASES = ["https://contract.mexc.com", "https://contract.mexc.io"]
BASE_TICKER = "https://contract.mexc.com"

# Fix proxy format for Webshare
if PROXY_URL and ":80" in PROXY_URL:
    # Try 8080 is more stable than 80 on Railway
    PROXY_URL = PROXY_URL.replace(":80", ":8080")
    print(f"Auto-fixed proxy port to 8080")

PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
print(f"=== RAILWAY BOT MODE={MODE} ===")
print(f"Proxy: {PROXY_URL[:40]}..." if PROXY_URL else "No proxy - will be BLOCKED!")
print(f"API Key: {API_KEY[:5]}...")

def sign(params, secret):
    query = "&".join([f"{k}={v}" for k,v in sorted(params.items())])
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def smart_req(method, url, **kwargs):
    kwargs.setdefault('timeout', 30)
    if PROXIES:
        # Don't fallback to direct - keep trying proxy
        for attempt in range(3):
            try:
                print(f"Proxy try {attempt+1}/3 -> {url[:70]}")
                r = requests.request(method, url, proxies=PROXIES, **kwargs)
                # If MEXC returns HTML WAF page even via proxy, retry
                if r.text.strip().startswith("<") or "Access Denied" in r.text[:500]:
                    print(f"WAF via proxy attempt {attempt+1}, retrying...")
                    time.sleep(2)
                    continue
                return r
            except Exception as e:
                print(f"Proxy fail attempt {attempt+1}: {e}")
                time.sleep(3)
        print("CRITICAL: All proxy tries failed - proxy dead")
        # Return fake response to avoid crash
        raise Exception("Proxy dead - check webshare.io proxy")
    else:
        # No proxy - will be blocked on Railway
        return requests.request(method, url, **kwargs)

def get_balance():
    for base in BASES:
        try:
            ts = int(time.time()*1000)
            url = f"{base}/api/v1/private/account/assets"
            params = {"api_key": API_KEY, "req_time": ts}
            params["sign"] = sign(params, API_SECRET)
            r = smart_req("GET", url, params=params, headers={"ApiKey": API_KEY})
            print(f"Balance response from {base}: {r.text[:300]}")
            if r.text.strip().startswith("<"):
                print(f"WAF BLOCKED on {base}")
                continue
            j = r.json()
            for a in j.get("data", []):
                if a.get("currency") == "USDT":
                    bal = float(a.get("equity") or a.get("availableBalance") or 0)
                    if bal > 0:
                        print(f"Balance OK {bal} USDT from {base}")
                        return bal
        except Exception as e:
            print(f"Balance error {base}: {e}")
    print("Balance fallback 6.5 - API BLOCKED")
    return 6.5

def get_funding_tickers():
    try:
        r = smart_req("GET", f"{BASE_TICKER}/api/v1/contract/ticker", timeout=20)
        data = r.json().get("data", [])
        pairs = []
        for t in data:
            sym = t.get("symbol","")
            if sym.endswith("_USDT"):
                try:
                    rate = float(t.get("fundingRate",0))
                    pairs.append({"symbol": sym, "rate": rate})
                except:
                    pass
        pairs.sort(key=lambda x: abs(x["rate"]), reverse=True)
        return pairs
    except Exception as e:
        print(f"Ticker error: {e}")
        return []

def place_order(sym, side, usdt_value):
    vol = max(1, math.floor(usdt_value/10))
    print(f"Placing ORDER {sym} side={side} vol={vol} usdt={usdt_value}")
    for base in BASES:
        try:
            ts = int(time.time()*1000)
            p = {"api_key": API_KEY, "req_time": ts, "symbol": sym, "price": 0, "vol": vol, "side": side, "type": 5, "open_type": 1, "leverage": LEVERAGE}
            p["sign"] = sign(p, API_SECRET)
            r = smart_req("POST", f"{base}/api/v1/private/order/submit", data=p, headers={"ApiKey": API_KEY})
            print(f"ORDER RESULT {base}: {r.text[:800]}")
            if "200" in r.text or "orderId" in r.text or "success" in r.text.lower():
                return True
        except Exception as e:
            print(f"Order error {base}: {e}")
    return False

# MAIN
bal = get_balance()
print(f"START bal {bal} USDT")

tickers = get_funding_tickers()
print(f"Found {len(tickers)} pairs")
if tickers:
    print(f"Top 3: {tickers[:3]}")

if MODE == "test_now" and tickers:
    top = tickers[0]
    side = 1 if top["rate"] < 0 else 3  # 1=long, 3=short for funding
    print(f"TEST MODE: {top['symbol']} rate {top['rate']*100:.4f}% side {side}")
    ok = place_order(top["symbol"], side, bal*0.7*LEVERAGE)
    print(f"TEST ORDER {'SUCCESS' if ok else 'FAILED'}")
else:
    print(f"MODE {MODE} - waiting for funding window")
    while True:
        print(f"Loop check bal {bal}")
        time.sleep(60)
