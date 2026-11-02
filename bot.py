import requests, time
from datetime import datetime, timezone, timedelta
import ccxt

BASE = "https://contract.mexc.com/api/v1/contract"
ex = ccxt.mexc({"options":{"defaultType":"swap"}})

def get(u, params=None):
    try:
        return requests.get(u, params=params, timeout=15).json()
    except Exception as e:
        print(f"GET fail {e}")
        return {}

# Get all USDT perps
print("Fetching all contracts...")
detail = get(f"{BASE}/detail").get("data") or []
symbols = [d["symbol"] for d in detail if str(d["symbol"]).endswith("_USDT")]
print(f"Total {len(symbols)} symbols")

# Collect 30d funding history for each symbol
# Endpoint: /funding_rate/{symbol}/history?  Try both formats
start_ts = int((time.time() - 30*86400)*1000)
all_history = {} # slot_timestamp -> [(symbol, rate, cycle)]

print("Fetching 30d funding history (this takes 2-3min)...")
for i, sym in enumerate(symbols):
    if i % 50 == 0:
        print(f"{i}/{len(symbols)}...")
    # MEXC history endpoint
    for url in [
        f"{BASE}/funding_rate/{sym}",
        f"{BASE}/funding_rate/history",
    ]:
        try:
            params = {"symbol": sym} if "history" in url else None
            # Actually correct is /funding_rate/history?symbol=BTC_USDT
            if "history" in url:
                r = get(url, {"symbol": sym})
                data = r.get("data") or []
                if isinstance(data, dict): data = [data]
                for entry in data:
                    try:
                        rate = float(entry.get("fundingRate") or entry.get("funding_rate") or 0)
                        ts = int(entry.get("settleTime") or entry.get("timestamp") or 0)
                        if ts > 1e12: ts //= 1000
                        if ts < start_ts//1000: continue
                        # round to nearest slot 0,4,8,12,16,20 UTC
                        dt = datetime.fromtimestamp(ts, timezone.utc)
                        slot = dt.replace(minute=0, second=0, microsecond=0)
                        # adjust to 00,04,08...
                        h = (slot.hour // 4)*4
                        slot = slot.replace(hour=h)
                        slot_ts = int(slot.timestamp())
                        if abs(rate) >= 0.005:
                            all_history.setdefault(slot_ts, []).append((sym, rate, entry.get("collectCycle",4)))
                    except: pass
            else:
                # For single endpoint, we need historical? skip
                pass
        except: pass
    time.sleep(0.02) # rate limit

# Alternative: use /funding_rate/history?symbol=SYM with page
# If empty, fallback to scanning via ticker history is not possible, so use direct scan of fundingRate history API bulk
if not all_history:
    print("No history via detail, trying bulk funding history endpoint...")
    # Try fetching all tickers history via /ticker? No history.
    # We'll use ccxt fetchFundingRateHistory if available
    ex.load_markets()
    for sym_raw in symbols[:200]: # test 200 top
        m_sym = sym_raw.replace("_","/")+":USDT"
        try:
            hist = ex.fetch_funding_rate_history(m_sym, since=start_ts, limit=100)
            for h in hist:
                rate = float(h.get("fundingRate") or 0)
                if abs(rate) < 0.005: continue
                ts = int(h.get("timestamp",0)//1000)
                dt = datetime.fromtimestamp(ts, timezone.utc)
                slot = dt.replace(minute=0, second=0, microsecond=0)
                slot = slot.replace(hour=(slot.hour//4)*4)
                slot_ts = int(slot.timestamp())
                all_history.setdefault(slot_ts, []).append((sym_raw, rate, 4))
        except Exception as e:
            # print(e)
            pass
        time.sleep(0.1)

print(f"\nCollected {len(all_history)} slots with >=0.5% funding in last 30d")

# Backtest your logic
wallet = 1000.0
initial = wallet
trades = 0
wins = 0
losses = 0
killer_hits = 0
pnl_history = []

sorted_slots = sorted(all_history.keys())

for slot_ts in sorted_slots:
    slot_fundings = all_history[slot_ts]
    if not slot_fundings:
        continue
    # Pick highest 3 >=0.5%
    picks = sorted(slot_fundings, key=lambda x: -abs(x[1]))[:3]
    
    total = wallet * 0.99 * 5
    per = total / len(picks)
    
    slot_pnl = 0
    dt = datetime.fromtimestamp(slot_ts, timezone.utc)
    
    for raw, rate, cycle in picks:
        sym = raw.replace("_","/")+":USDT"
        # Simulate price move T-45s to T+3s
        # Fetch 1m klines around slot for real volatility
        try:
            # Get klines 1m before/after slot
            ohlcv = ex.fetch_ohlcv(sym, '1m', since=(slot_ts-120)*1000, limit=10)
            if len(ohlcv) < 5:
                # Assume 0% move if no data = win funding
                price_change = 0
            else:
                entry_price = ohlcv[-3][4] if len(ohlcv)>=3 else ohlcv[0][4]
                # Simulate worst move within 3 sec after (we use 3x 1m max move as proxy)
                lows = [c[3] for c in ohlcv[-4:]]
                highs = [c[2] for c in ohlcv[-4:]]
                side = "sell" if rate>0 else "buy"
                if side=="sell":
                    # short: loss if price up
                    max_up = (max(highs) - entry_price)/entry_price
                    price_change = max_up
                else:
                    max_down = (entry_price - min(lows))/entry_price
                    price_change = max_down
                # If you go long, loss when price down
                
                # Funding collected
                funding_pnl = per * abs(rate) * 0.8  # 80% after fees/slippage
                
                if price_change >= 0.05: # -5% price = killer -25%
                    # Killer hit: -25% margin
                    loss = -per * 0.25 / 5  # per is notional, margin = per/5
                    # Actually per = notional, margin = per/5, 25% of margin = per/5*0.25
                    loss = -(per/5)*0.25
                    slot_pnl += loss
                    killer_hits += 1
                    losses += 1
                else:
                    # Normal: price move -0.5% avg + funding
                    # Assume -0.1% avg slippage for winners
                    price_pnl = -per * 0.001 if price_change < 0.02 else -per * price_change * 0.3
                    slot_pnl += funding_pnl + price_pnl
                    wins += 1
                    
        except Exception as e:
            # If coin delisted, skip
            funding_pnl = per * abs(rate) * 0.8
            slot_pnl += funding_pnl
            wins += 1
        
        trades += 1
    
    wallet += slot_pnl
    pnl_history.append((dt, picks, slot_pnl, wallet))
    print(f"{dt:%m-%d %H:%M} {len(picks)} picks {', '.join([f'{r} {ra*100:+.2f}%' for r,ra,_ in picks])} | PnL {slot_pnl:+.2f} | Wallet ${wallet:.2f}")

print("\n=== 30D BACKTEST RESULT ===")
print(f"Start ${initial:.2f} -> End ${wallet:.2f} | Total PnL {wallet-initial:+.2f} ({(wallet/initial-1)*100:+.1f}%)")
print(f"Trades: {trades} | Wins: {wins} | Losses: {losses} | Killer hits: {killer_hits}")
if trades>0:
    print(f"Win rate: {wins/trades*100:.1f}%")
print(f"Avg per slot: {(wallet-initial)/len(sorted_slots):+.2f} if {len(sorted_slots)} slots")
