import ccxt, time
from datetime import datetime

mexc = ccxt.mexc({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

START_CAPITAL = 5
LEVERAGE = 5
CHECK_EVERY_H = 4  # YOUR SETTING
FUNDING_EVERY_H = 8  # MEXC REAL
MIN_FUNDING = 0.005  # Your 0.5%

COINS = ['HOT/USDT:USDT', 'PEPE/USDT:USDT', 'BONK/USDT:USDT', 'WIF/USDT:USDT', 'FLOKI/USDT:USDT', 'DOGE/USDT:USDT', 'BTC/USDT:USDT']

def backtest_every_4h():
    end = int(time.time() * 1000)
    start = end - (60 * 24 * 60 * 60 * 1000)
    
    wallet = START_CAPITAL
    trades = []
    
    for symbol in COINS:
        try:
            history = mexc.fetch_funding_rate_history(symbol, since=start, limit=1000)
            
            for h in history:
                rate = float(h['fundingRate'])
                funding_time = int(h['timestamp'])
                
                # You check EVERY 4H, so you would enter 4H BEFORE funding time
                # Entry time = funding_time - 4H
                entry_time = funding_time - (4 * 60 * 60 * 1000)
                
                if abs(rate) < MIN_FUNDING:
                    continue
                
                # Hold 4H to collect funding (not 8H)
                position = wallet * 0.99 * LEVERAGE
                funding_fee = position * abs(rate)
                fee = position * 0.0004  # open+close limit
                
                net = funding_fee - fee
                
                trades.append({
                    'funding_time': datetime.fromtimestamp(funding_time/1000).strftime('%m-%d %H:%M'),
                    'entry_time': datetime.fromtimestamp(entry_time/1000).strftime('%m-%d %H:%M'),
                    'coin': symbol.replace('/USDT:USDT',''),
                    'rate': rate*100,
                    'pos': position,
                    'net': net,
                    'hold': '4H'
                })
                wallet += net
                
        except Exception as e:
            continue
    
    trades.sort(key=lambda x: x['funding_time'])
    
    print(f"\nEVERY 4H CHECK | FUNDING EVERY 8H | MIN {MIN_FUNDING*100}%")
    print("="*80)
    print(f"{'Entry':<10} {'Funding':<10} {'Coin':<6} {'Rate':<7} {'Hold':<4} {'Net':<6} {'Wallet'}")
    print("-"*80)
    for t in trades:
        print(f"{t['entry_time']:<10} {t['funding_time']:<10} {t['coin']:<6} {t['rate']:>6.3f}% {t['hold']:<4} ${t['net']:>5.3f} ${t['pos']:>5.1f}")
    
    total = sum([x['net'] for x in trades])
    print("="*80)
    print(f"YOUR SETTING: Run Bot MIN 0.5% MAX ∞ EVERY 4H")
    print(f"START ${START_CAPITAL} -> END ${wallet:.2f} | Profit ${total:.2f} | Trades {len(trades)}")
    print(f"Funding every 8h but you check every 4h = You enter 4h early")
    print(f"With $100k: ${100000 + total/START_CAPITAL*100000:.0f} in 60 days")

backtest_every_4h()
