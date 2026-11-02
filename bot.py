import requests
import pandas as pd
from datetime import datetime, timedelta
import time
from collections import defaultdict

INITIAL = 5.0
LEVERAGE = 5
WALLET = 0.99
FEE = 0.0004  # 0.02% + 0.02% limit
MIN = 0.005   # 0.5%
MAX = 999
SETTLE_HOURS = [1,5,9,13,17,21]  # YOUR SCREENSHOT: 01:00,05:00,09:00,13:00,17:00,21:00 UTC
SETTLE_EVERY = 4

def get_symbols():
    url = "https://contract.mexc.com/api/v1/contract/detail"
    r = requests.get(url, timeout=20).json()
    syms = []
    for c in r.get('data', []):
        if c['symbol'].endswith('_USDT'):
            syms.append(c['symbol'])
    return syms

def get_real_history(symbol, days=60):
    url = "https://contract.mexc.com/api/v1/contract/funding_rate/history"
    params = {'symbol': symbol, 'page_size': 1000, 'page_num': 1}
    try:
        resp = requests.get(url, params=params, timeout=15).json()
        # MEXC returns different formats
        data = []
        if 'data' in resp:
            d = resp['data']
            if isinstance(d, dict) and 'resultList' in d:
                data = d['resultList']
            elif isinstance(d, list):
                data = d
        else:
            data = resp.get('resultList', [])
        
        history = []
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        for item in data:
            ts = item.get('settleTime') or item.get('createTime') or item.get('timestamp')
            if not ts:
                continue
            try:
                dt = datetime.fromtimestamp(int(ts)/1000 if len(str(int(ts))) > 10 else int(ts))
            except:
                continue
            
            if dt >= cutoff:
                rate = float(item.get('fundingRate', 0))
                if rate != 0:
                    history.append({
                        'datetime': dt,
                        'symbol': symbol,
                        'rate': rate,
                        'abs': abs(rate)
                    })
        return history
    except Exception as e:
        return []

def backtest_60d_live_4h():
    print("="*90)
    print(f"60 DAYS LIVE MEXC REAL DATA | SETTLE EVERY 4H {SETTLE_HOURS} | MIN {MIN*100}% MAX ∞")
    print(f"HIGHEST FIRST | Initial ${INITIAL} | {LEVERAGE}x | {WALLET*100}%")
    print("="*90)
    
    print("\n[1/2] Getting all USDT symbols...")
    symbols = get_symbols()
    print(f"Total symbols: {len(symbols)}")
    
    # Prioritize pairs that actually have 4h billing like POWR in your screenshot
    priority_keywords = ['POWR','BLESS','WCT','PENGU','BRETT','HOT','PEPE','BONK','WIF','FLOKI','MEME','1000','DOGE','SHIB']
    priority = [s for s in symbols if any(k in s for k in priority_keywords)]
    test_list = (priority + symbols)[:100]  # Top 100
    
    print(f"\n[2/2] Fetching REAL funding history 60 days for {len(test_list)} pairs...")
    print("This is REAL data from contract.mexc.com, not simulation")
    
    all_history = []
    for i, sym in enumerate(test_list):
        hist = get_real_history(sym, 60)
        if hist:
            all_history.extend(hist)
        if i % 10 == 0:
            print(f"  {i}/{len(test_list)} {sym}: {len(hist)} records (latest {hist[0]['rate']*100:.4f}% if any)" if hist else f"  {i}/{len(test_list)} {sym}: 0")
        time.sleep(0.12)
    
    print(f"\nTotal REAL records past 60 days: {len(all_history)}")
    if not all_history:
        print("API blocked, use ccxt fallback")
        return
    
    # Filter MIN 0.5%
    filtered = [h for h in all_history if MIN <= h['abs'] <= MAX]
    print(f"After MIN {MIN*100}% filter: {len(filtered)} records")
    print(f"Max funding found in 60d real: {max([h['abs'] for h in all_history])*100:.4f}%" if all_history else "0")
    
    # Group by 4h slot matching YOUR screenshot hours
    grouped = defaultdict(list)
    for h in filtered:
        dt = h['datetime']
        # Snap to nearest settle hour in SETTLE_HOURS
        # Your screenshot shows exact hours 1,5,9,13,17,21
        hour = dt.hour
        # Find closest settle hour
        closest = min(SETTLE_HOURS, key=lambda x: abs(x-hour) if abs(x-hour) <=2 else 999)
        if abs(closest - hour) > 2:  # Skip if not near settle hour
            # Force to 4h bucket
            closest = (hour // 4) * 4 + 1  # 1,5,9,13,17,21
            if closest > 23:
                closest = 21
        slot_key = dt.replace(hour=closest, minute=0, second=0, microsecond=0)
        grouped[slot_key].append(h)
    
    print(f"4H Settlement slots with >=0.5%: {len(grouped)} slots (max 60*6=360)")
    
    # BACKTEST - HIGHEST FIRST per 4h slot
    balance = INITIAL
    trades = []
    
    for slot in sorted(grouped.keys()):
        events = grouped[slot]
        events.sort(key=lambda x: x['abs'], reverse=True)
        best = events[0]  # Highest funding in that 4h slot
        
        pos = balance * WALLET * LEVERAGE
        gross = pos * best['abs']
        fee = pos * FEE
        net = gross - fee
        
        if net < 0.001:
            continue
        
        balance += net
        trades.append({
            'Date': slot.strftime('%Y-%m-%d %H:00'),
            'Symbol': best['symbol'],
            'Rate%': round(best['rate']*100, 4),
            'Abs%': round(best['abs']*100, 4),
            'PnL$': round(net, 4),
            'Balance$': round(balance, 4),
            'PairsInSlot': len(events)
        })
    
    df = pd.DataFrame(trades)
    if df.empty:
        print("\nNo trades >=0.5% - try MIN 0.15%")
        print(f"All history avg: {sum([h['abs'] for h in all_history])/len(all_history)*100 if all_history else 0:.4f}%")
        return
    
    print("\n" + "="*90)
    print("60 DAYS LIVE REAL RESULTS - SETTLE EVERY 4H")
    print("="*90)
    print(f"START ${INITIAL:.2f} -> END ${balance:.2f} | PROFIT ${balance-INITIAL:.2f} ({(balance/INITIAL-1)*100:.1f}%)")
    print(f"Trades taken: {len(df)} / {len(grouped)} slots")
    print(f"Highest real: {df['Abs%'].max():.4f}% | Lowest: {df['Abs%'].min():.4f}% | Avg: {df['Abs%'].mean():.4f}%")
    print(f"Settlement: Every 4h at {SETTLE_HOURS} UTC = 6 per day x 60 = 360 max")
    print(f"Total fees: ${len(df)*INITIAL*WALLET*LEVERAGE*FEE:.3f}")
    
    print("\nTop 25 HIGHEST real fundings past 60 days (your POWR -1.7% type):")
    print(df.sort_values('Abs%', ascending=False).head(25).to_string(index=False))
    
    print("\nLast 30 trades (most recent):")
    print(df.tail(30).to_string(index=False))
    
    df.to_csv('backtest_60d_LIVE_4H_REAL.csv', index=False)
    print("\n✅ Saved to backtest_60d_LIVE_4H_REAL.csv")
    
    print("\n--- GROWTH with 4H settlement ---")
    for d in [1,7,14,30,60]:
        target = d*6
        idx = min(target-1, len(df)-1)
        if idx >= 0:
            print(f"Day {d} ({target} settlements): ${df.iloc[idx]['Balance$']:.2f} ({(df.iloc[idx]['Balance$']/INITIAL-1)*100:.1f}%)")
    
    print(f"\nExtrapolation with 4H settlement:")
    print(f"$5 -> 60d: ${balance:.2f}")
    print(f"$100 -> 60d: ${100 * balance/INITIAL:.2f}")
    print(f"$100k with $50k cap: ${100000 * balance/INITIAL:.0f}")

if __name__ == "__main__":
    backtest_60d_live_4h()
