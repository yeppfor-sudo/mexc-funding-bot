import os, time, hmac, hashlib, requests, json
from datetime import datetime, timezone, timedelta
import math

API_KEY = os.getenv("MEXC_API_KEY")
API_SECRET = os.getenv("MEXC_API_SECRET")
BASE = "https://contract.mexc.com"
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
ENTRY_PCT = float(os.getenv("ENTRY_PCT", "70")) / 100  # 70%
DCA_PCT = float(os.getenv("DCA_PCT", "29")) / 100  # 29%
MIN_RATE = float(os.getenv("MIN_RATE_PCT", "0.5")) / 100
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "20700"))  # 5h45m for mode=run

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": msg}, timeout=5)
    except: pass

def sign(params, secret):
    query = "&".join([f"{k}={v}" for k,v in sorted(params.items())])
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_balance():
    url = f"{BASE}/api/v1/private/account/assets"
    ts = int(time.time()*1000)
    params = {"api_key": API_KEY, "req_time": ts}
    params["sign"] = sign(params, API_SECRET)
    r = requests.get(url, params=params, headers={"ApiKey": API_KEY}, timeout=10)
    data = r.json()
    # MEXC returns equity in USDT
    try:
        for asset in data["data"]:
            if asset["currency"] == "USDT":
                return float(asset["equity"])
    except:
        print("BALANCE RAW:", data)
    return 0.0

def get_all_perp():
    # 1. Scan all perpetual
    url = f"{BASE}/api/v1/contract/detail"
    r = requests.get(url, timeout=15).json()
    contracts = [c for c in r["data"] if c["symbol"].endswith("_USDT")]
    return contracts

def get_funding_rates():
    url = f"{BASE}/api/v1/contract/funding_rate"
    # MEXC needs symbol by symbol, so we use ticker endpoint with funding
    # Faster: get all tickers
    ticker_url = f"{BASE}/api/v1/contract/ticker"
    r = requests.get(ticker_url, timeout=15).json()
    rates = []
    for t in r["data"]:
        try:
            sym = t["symbol"]
            if not sym.endswith("_USDT"): continue
            rate = float(t.get("fundingRate", 0))
            rates.append({"symbol": sym, "rate": rate, "price": float(t["lastPrice"])})
        except: continue
    return rates

def get_orderbook(symbol):
    url = f"{BASE}/api/v1/contract/depth/{symbol}"
    r = requests.get(url, params={"limit": 20}, timeout=10).json()
    return r["data"]

def check_depth_ok(symbol, usdt_needed, price):
    # 3. Check order book according to balance ♾️
    try:
        book = get_orderbook(symbol)
        bids_vol = sum([float(x[1])*float(x[0]) for x in book["bids"][:10]]) # USDT depth
        asks_vol = sum([float(x[1])*float(x[0]) for x in book["asks"][:10]])
        total_depth = min(bids_vol, asks_vol)
        # need at least 3x our order size in book
        return total_depth > (usdt_needed * 3)
    except:
        return True

def place_order(symbol, side, vol_usdt, price=None):
    # side: 1 open long, 2 close long, 3 open short, 4 close short
    ts = int(time.time()*1000)
    url = f"{BASE}/api/v1/private/order/submit"
    # vol = contracts
    vol = math.floor(vol_usdt / float(price or 1))
    if vol < 1: vol = 1
    params = {
        "api_key": API_KEY,
        "req_time": ts,
        "symbol": symbol,
        "price": price or 0,
        "vol": vol,
        "side": side,
        "type": 1 if price else 5, # 1 limit, 5 market
        "open_type": 1, # isolated
        "leverage": LEVERAGE
    }
    params["sign"] = sign(params, API_SECRET)
    r = requests.post(url, data=params, headers={"ApiKey": API_KEY}, timeout=10)
    print(f"ORDER {symbol} side {side} vol {vol} -> {r.text}")
    return r.json()

def get_next_funding_time():
    now = datetime.now(timezone.utc)
    # MEXC funding 00:00, 08:00, 16:00 UTC (every 8h)
    hours = [0,8,16]
    for h in hours:
        ft = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if ft > now:
            return ft
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

def main(mode="run_once"):
    print(f"Sniper v6 ADV 70+29% DCA LIVE {LEVERAGE}x bal check...")
    tg(f"🤖 Sniper LIVE {LEVERAGE}x Mode:{mode} Starting. Balance check...")

    balance = get_balance()
    if balance < 1:
        print(f"Balance low {balance} - check Futures wallet")
        return
    print(f"Balance {balance} USDT")

    while True:
        next_ft = get_next_funding_time()
        now = datetime.now(timezone.utc)
        wait = (next_ft - now).total_seconds()
        print(f"Next funding {next_ft} UTC in {int(wait)}s sleep 60s | bal {balance:.2f}")
        if wait > 300: # sleep till 5 min before
            time.sleep(min(60, wait-300))
            continue
        if wait > 70:
            time.sleep(10)
            continue

        # 50 sec before funding - MAIN ENTRY
        if 40 <= wait <= 70:
            print("=== 50sec BEFORE FUNDING - SCANNING ===")
            rates = get_funding_rates()
            # 2. filter highest
            filtered = [r for r in rates if abs(r["rate"]) >= MIN_RATE]
            # 4. pick highest both neg/pos
            filtered.sort(key=lambda x: abs(x["rate"]), reverse=True)
            if not filtered:
                print("No high funding rate")
                time.sleep(5)
                continue

            for pick in filtered[:5]: # check top 5 for liquidity
                symbol = pick["symbol"]
                rate = pick["rate"]
                price = pick["price"]
                side_text = "LONG" if rate < 0 else "SHORT" # neg rate -> long to collect
                side_code = 1 if rate < 0 else 3

                need_usdt = balance * ENTRY_PCT * LEVERAGE
                if not check_depth_ok(symbol, need_usdt, price):
                    print(f"Depth low for {symbol} skip")
                    continue

                # 5. invest 70%
                entry_usdt = balance * ENTRY_PCT
                print(f"ARMED {symbol} rate {rate*100:.3f}% -> {side_text} 70% {entry_usdt:.2f} USDT")
                tg(f"⚡ ARMED {symbol} {rate*100:.3f}% -> {side_text}\n70% {entry_usdt:.2f} USDT")

                order = place_order(symbol, side_code, need_usdt, price=None) # market
                entry_price = price
                avg_price = entry_price
                total_usdt = entry_usdt

                # Wait for funding
                print(f"OPEN {side_text} {symbol} waiting funding...")
                time.sleep(wait + 15) # cross funding

                # 6. After funding volatile -> DCA 29% at 1-1.5% down from avg
                print("Funding done - checking DCA")
                # get current price
                rates2 = get_funding_rates()
                cur = next((x for x in rates2 if x["symbol"]==symbol), None)
                cur_price = cur["price"] if cur else price

                price_diff_pct = (cur_price - avg_price)/avg_price*100
                # If price moved against us 1%+ -> DCA
                should_dca = False
                if side_text=="LONG" and price_diff_pct <= -1.0: should_dca=True
                if side_text=="SHORT" and price_diff_pct >= 1.0: should_dca=True

                if should_dca:
                    dca_usdt = balance * DCA_PCT
                    need_dca = dca_usdt * LEVERAGE
                    print(f"DCA TRIGGER {symbol} {price_diff_pct:.2f}% -> adding 29% {dca_usdt:.2f}")
                    tg(f"📉 DCA {symbol} {price_diff_pct:.2f}% -> +29%")
                    place_order(symbol, side_code, need_dca, price=None)
                    # new avg: (70%*p1 + 29%*p2)/99%
                    avg_price = (entry_price*70 + cur_price*29)/99
                    total_usdt = entry_usdt + dca_usdt
                else:
                    print(f"No DCA needed {price_diff_pct:.2f}%")

                # 7. Exit at avg price + funding profit
                # 8. SL -3% price = -15% capital with 5x
                # Compound loop
                print(f"HOLDING {symbol} avg {avg_price:.6f} waiting avg exit...")
                # try exit at avg price
                for i in range(30): # 30 attempts ~5 min
                    rates_now = get_funding_rates()
                    cur = next((x for x in rates_now if x["symbol"]==symbol), None)
                    if not cur: time.sleep(10); continue
                    cp = cur["price"]
                    # Exit if back to avg or profit
                    pnl_pct = (cp - avg_price)/avg_price*100 if side_text=="LONG" else (avg_price-cp)/avg_price*100
                    if pnl_pct >= 0.05: # back to avg +0.05%
                        print(f"EXIT at avg/profit {cp} pnl {pnl_pct:.3f}%")
                        close_side = 2 if side_text=="LONG" else 4
                        place_order(symbol, close_side, total_usdt*LEVERAGE, price=None)
                        funding_profit = abs(rate)*total_usdt*LEVERAGE
                        print(f"CLOSED {symbol} price {pnl_pct:.3f}% funding +{funding_profit:.4f} USDT")
                        tg(f"✅ CLOSE {symbol} +{pnl_pct:.3f}% price | funding +{funding_profit:.3f} USDT")
                        balance = get_balance() # compound
                        break
                    # SL -3% price = -15% capital
                    if pnl_pct <= -3.0:
                        print(f"SL -3% {symbol} closing {pnl_pct:.2f}%")
                        close_side = 2 if side_text=="LONG" else 4
                        place_order(symbol, close_side, total_usdt*LEVERAGE, price=None)
                        tg(f"🛑 SL {symbol} {pnl_pct:.2f}%")
                        balance = get_balance()
                        break
                    time.sleep(10)
                else:
                    # force close
                    close_side = 2 if side_text=="LONG" else 4
                    place_order(symbol, close_side, total_usdt*LEVERAGE, price=None)

                if mode=="run_once":
                    print("run_once done exit")
                    return
                else:
                    break # go wait next funding

        time.sleep(5)

if __name__ == "__main__":
    import sys
    m = sys.argv[1] if len(sys.argv)>1 else "run_once"
    main(m)
