# backtest.py - LIVE MEXC 30-day funding backtest
# pip install ccxt requests pandas
# python backtest.py

import ccxt, requests, time, statistics
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

BASE = "https://contract.mexc.com/api/v1/contract"
MIN_RATE = 0.005
FEE = 0.0004
MIN_EDGE = 0.002
SL = 0.03
NEAR_AVG = 0.0008

def get(u):
    for _ in range(3):
        try:
            r = requests.get(u, timeout=15)
            return r.json()
        except:
            time.sleep(0.5)
    return {}

ex = ccxt.mexc({"options":{"defaultType":"swap"}})
ex.load_markets()
markets = [m for m in ex.markets.values() if m['id'].endswith('_USDT')]
print(f"MEXC USDT perps: {len(markets)}")

# Fetch 30d funding history for all coins
def fetch_history(m):
    raw = m['id']
    try:
        # MEXC history endpoint - 200 per page
        h = get(f"{BASE}/funding_rate/history?symbol={raw}&page_size=200")
        data = h.get('data') or {}
        lst = data.get('resultList') or data.get('data') or data.get('result') or []
        events=[]
        for e in lst:
            ts = int(e.get('settleTime') or e.get('time') or e.get('nextSettleTime') or 0)
            if ts>1e12: ts//=1000
            if ts < time.time()-30*86400: continue
            rate = float(e.get('fundingRate') or e.get('rate') or 0)
            if abs(rate) < MIN_RATE: continue
            # Check collect cycle = 4h
            cycle = int(e.get('collectCycle') or 4)
            if cycle != 4: continue
            events.append((ts, raw, rate))
        return events
    except:
        return []

print("Fetching 30d funding >0.5% - this takes 2 min...")
all_events=[]
with ThreadPoolExecutor(20) as pool:
    results = list(pool.map(fetch_history, markets[:500]))
    for r in results:
        all_events.extend(r)

all_events = sorted(all_events)
print(f"\n=== LIVE RESULTS LAST 30 DAYS ===")
print(f"Total events >0.5% (4h only): {len(all_events)}")

# Group by settlement slot (00,04,08,12,16,20 UTC)
from collections import defaultdict
slots = defaultdict(list)
for ts, raw, rate in all_events:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    slot_key = dt.replace(minute=0, second=0, microsecond=0)
    # Round to nearest 4h slot
    slot_key = slot_key.replace(hour=(slot_key.hour//4)*4)
    slots[slot_key].append((raw, rate))

print(f"Slots with at least 1 coin >0.5%: {len(slots)} / 180 slots (30d * 6 per day)")
print(f"Avg coins per slot >0.5%: {statistics.mean([len(v) for v in slots.values()]):.1f}")

# Simulate your bot logic
wallet = 100.0
start_wallet = wallet
wins=0; losses=0; sl_hits=0; no_trade=0
equity_curve=[wallet]
max_dd=0; peak=wallet

# Your screenshot stats for realistic price move after funding
# 72.9% winrate, avg win +0.35%, avg loss -0.9%, SL 15 events
for slot_dt in sorted(slots.keys()):
    coins = slots[slot_dt]
    # Rank by edge = funding - 2*fee - spread - slippage
    ranked=[]
    for raw, rate in coins:
        edge = abs(rate) - 2*FEE - 0.001 - 0.0005 # spread 0.1% + slip 0.05%
        if edge < MIN_EDGE: continue
        ranked.append((raw, rate, edge))
    ranked = sorted(ranked, key=lambda x: -abs(x[1]))

    if not ranked:
        no_trade+=1
        continue

    # Take top 1-3 as bot does (MAX_COINS=3, cap $3k)
    take = ranked[:3]
    # Assume 99%*5x position
    per_coin_wallet = wallet * 0.99 / len(take)

    for raw, rate, edge in take:
        gross = per_coin_wallet * abs(rate)  # funding fee
        cost = per_coin_wallet * 5 * FEE * 2  # open+close
        net_funding = gross - cost

        # Simulate price move in 3 sec after settlement
        # From your live data: 70% price returns to avg or profit within 3s
        import random
        r = random.random()
        if r < 0.70:  # close on avg/profit within 3s
            wallet += net_funding + per_coin_wallet*0.0002 # small profit on close
            wins+=1
        elif r < 0.85:  # near avg 0.08%
            wallet += net_funding - per_coin_wallet*0.0005
            wins+=1
        else:  # SL -3% price = -15% margin
            loss = per_coin_wallet * SL * 5 * 0.2 # 15% of losers hit SL
            # Actually SL is -15% of position = -14.85% wallet slice
            wallet -= per_coin_wallet * 0.15
            losses+=1
            sl_hits+=1

        equity_curve.append(wallet)
        peak = max(peak, wallet)
        dd = (peak - wallet)/peak
        max_dd = max(max_dd, dd)

        # Force close at T+3s - no hold beyond

print(f"\n=== BACKTEST SIMULATION (Your Bot Rules) ===")
print(f"Start: ${start_wallet:.2f} -> End: ${wallet:.2f} | PnL: {((wallet/start_wallet-1)*100):+.2f}%")
print(f"Traded slots: {len(slots)-no_trade} | Skipped (no edge): {no_trade}")
print(f"Trades: {wins+losses} | Wins: {wins} | Losses: {losses} | SL hits: {sl_hits}")
print(f"Winrate: {wins/(wins+losses)*100:.1f}% | Max DD: {max_dd*100:.1f}%")
print(f"Avg funding collected per trade: {(wallet-start_wallet)/(wins+losses) if wins+losses else 0:.4f}")

# Show top coins last 30d
from collections import Counter
top_coins = Counter([raw for _, raw, _ in all_events]).most_common(15)
print(f"\nTop coins >0.5% last 30d:")
for raw, cnt in top_coins:
    avg_rate = statistics.mean([r for _, rr, r in all_events if rr==raw])
    print(f"  {raw}: {cnt} times, avg {avg_rate*100:.3f}%")

print(f"\n=== SAMPLE EVENTS (Last 5 slots) ===")
for slot_dt in sorted(slots.keys())[-5:]:
    print(f"{slot_dt:%Y-%m-%d %H:%M} UTC - {len(slots[slot_dt])} coins >0.5%: " + ", ".join([f"{raw} {rate*100:.3f}%" for raw, rate in slots[slot_dt][:5]]))
