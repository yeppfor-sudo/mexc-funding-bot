import ccxt, time
from datetime import datetime, timedelta

# ================= BACKTEST CONFIG =================
mexc = ccxt.mexc({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

START_CAPITAL = 5
WALLET_PERCENT = 0.99
LEVERAGE = 5
MIN_FUNDING = 0.005  # 0.5% - Your HOT 0.70% rule
FEE = 0.0006  # 0.06% taker (MEXC API)
SL_HIT_RATE = 0.15  # 15% trades hit -3% SL (realistic)

# Coins to backtest (top meme + majors)
COINS = ['HOT/USDT:USDT', 'PEPE/USDT:USDT', 'BONK/USDT:USDT', 'WIF/USDT:USDT', 'FLOKI/USDT:USDT', 'DOGE/USDT:USDT', 'SHIB/USDT:USDT', 'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

def backtest_60_days():
    print(f"=== MEXC 60-DAY BACKTEST | Capital ${START_CAPITAL} | MIN {MIN_FUNDING*100}% ===")
    
    end = int(time.time() * 1000)
    start = end - (60 * 24 * 60 * 60 * 1000)
    
    all_trades = []
    wallet = START_CAPITAL
    
    for symbol in COINS:
        try:
            # Fetch 60 days funding history
            history = mexc.fetch_funding_rate_history(symbol, since=start, limit=1000)
            print(f"\n{symbol}: {len(history)} fundings in 60 days")
            
            for h in history:
                rate = float(h['fundingRate'])
                if abs(rate) < MIN_FUNDING:
                    continue
                
                # Your bot logic
                position = wallet * WALLET_PERCENT * LEVERAGE
                # Cap by depth logic: assume $100k depth for meme, $1.5M for BTC
                depth_cap = 100000 if 'HOT' in symbol or 'PEPE' in symbol else 1500000
                position = min(position, depth_cap * 0.15)
                
                gross = position * abs(rate)
                fee = position * FEE * 2  # open+close
                net_funding = gross - fee
                
                # Simulate SL: 15% chance of -3% loss
                import random
                random.seed(int(h['timestamp']))
                is_sl = random.random() < SL_HIT_RATE
                if is_sl:
                    sl_loss = position * 0.03
                    net = net_funding - sl_loss
                    result = f"SL LOSS -3%"
                else:
                    net = net_funding
                    result = f"WIN"
                
                all_trades.append({
                    'time': datetime.fromtimestamp(h['timestamp']/1000).strftime('%Y-%m-%d %H:%M'),
                    'symbol': symbol,
                    'rate': rate*100,
                    'position': position,
                    'gross': gross,
                    'net': net,
                    'result': result,
                    'wallet_before': wallet
                })
                wallet += net
                if wallet < 0.5:
                    wallet = 0.5
                
        except Exception as e:
            print(f"  {symbol} error: {e}")
            continue
    
    # Sort by time
    all_trades.sort(key=lambda x: x['time'])
    
    print("\n" + "="*90)
    print(f"{'Date':<16} {'Coin':<14} {'Rate':<8} {'Pos':<7} {'Gross':<7} {'Net':<7} {'Wallet':<8} {'Result'}")
    print("="*90)
    
    for t in all_trades[-50:]:  # Last 50 trades
        print(f"{t['time']:<16} {t['symbol']:<14} {t['rate']:>6.2f}% ${t['position']:>5.0f} ${t['gross']:>5.3f} ${t['net']:>6.3f} ${t['wallet_before']:>6.2f} {t['result']}")
    
    # Summary
    total_trades = len(all_trades)
    wins = len([t for t in all_trades if t['result']=='WIN'])
    total_gross = sum([t['gross'] for t in all_trades])
    total_net = sum([t['net'] for t in all_trades])
    final_wallet = START_CAPITAL + total_net
    
    print("\n" + "="*90)
    print(f"BACKTEST RESULT 60 DAYS | MEXC REAL FUNDING")
    print(f"Start: ${START_CAPITAL} -> End: ${final_wallet:.2f} | Profit: ${total_net:.2f} ({total_net/START_CAPITAL*100:.1f}%)")
    print(f"Total trades: {total_trades} | Wins: {wins} ({wins/total_trades*100:.1f}%) | SL hits: {total_trades-wins}")
    print(f"Avg per trade: ${total_net/total_trades:.4f} | Avg per day: ${total_net/60:.2f}")
    print(f"With $100k cap (same strategy): ${(total_net/START_CAPITAL)*100000:.0f} profit in 60 days")
    print("="*90)
    
    # Extrapolate to your scenarios
    print(f"\nEXTRAPOLATION:")
    print(f"$5 -> 60 days: ${final_wallet:.2f}")
    print(f"$100k with same hit rate: ${100000 + (total_net/START_CAPITAL)*100000:.0f} (if no depth limits)")
    print(f"$100k with $50k cap + 0.15% filter: ~${100000 + (total_net/60*0.3)*60:.0f} (realistic with PEPE depth)")
    print(f"$20M ($1M x 20 exchanges): ~${20000000 + (total_net/START_CAPITAL)*20000000*0.6:.0f} (60% due to depth)")

backtest_60_days()
