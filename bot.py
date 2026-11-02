import ccxt, time, requests
from datetime import datetime, timezone, timedelta

BASE = "https://contract.mexc.com/api/v1/contract"

def get(u):
    return requests.get(u, timeout=15).json()

ex = ccxt.mexc({"options":{"defaultType":"swap"}})
ex.load_markets()

# Get all USDT perps
markets = [m for m in ex.markets.values() if m['symbol'].endswith(':USDT')]
print(f"Markets: {len(markets)}")

# Fetch funding history for 30 days for each coin
# MEXC endpoint: /funding_rate/history?symbol=BTC_USDT
all_events = []
for m in markets[:200]:  # top 200 to avoid rate limit
    raw = m['id']  # e.g. BTC_USDT
    try:
        h = get(f"{BASE}/funding_rate/history?symbol={raw}&page_size=200")
        data = h.get('data') or {}
        lst = data.get('resultList') or data.get('data') or []
        for e in lst:
            ts = int(e.get('settleTime') or e.get('time') or 0)
            if ts > 1e12: ts //= 1000
            if ts < (time.time()-30*86400): continue
            rate = float(e.get('fundingRate') or 0)
            if abs(rate) < 0.005: continue  # >0.5% ONLY
            all_events.append((ts, raw, rate))
        time.sleep(0.05)
    except: pass

all_events.sort()
print(f"Events >0.5% last 30d: {len(all_events)}")

# Simulate your bot
# Wallet $100 start, 99% *5x, edge after fees 0.2%, SL -3% price, 0.08% near avg profit close
wallet = 100
wins=0; losses=0; fees=0; skipped=0

for ts, raw, rate in all_events:
    # Assume 70% win close on avg/profit, 15% near avg, 15% SL - your backtest 72.9% winrate
    # Real simulation needs orderbook but this is funding PnL
    gross = wallet * 0.99 * abs(rate)  # funding collected
    cost = wallet * 0.99 * 5 * 0.0004 * 2  # 2 sides taker API 0.08%
    net = gross - cost
    if net < wallet*0.002: # edge filter
        skipped+=1
        continue
    # 72.9% win as per your screenshot
    import random
    if random.random() < 0.729:
        wallet += net
        wins+=1
    else:
        # SL -3% price = -15% margin on 99%*5x position = -14.85% wallet
        loss = wallet * 0.99 * 0.03 * 5 * 0.15 # 15% of trades hit SL
        if random.random() < 0.2:
            wallet -= wallet*0.99*0.15*0.2
            losses+=1
        else:
            wallet += net
            wins+=1
    fees+=cost

print(f"Start $100 -> ${wallet:.2f} | Wins {wins} Loss {losses} Skipped {skipped} | Events {len(all_events)}")
