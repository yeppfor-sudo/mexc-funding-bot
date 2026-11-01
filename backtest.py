import ccxt
import pandas as pd
from datetime import datetime, timedelta
import os

# Same keys from secrets
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")

INITIAL_BALANCE = 5.0 # Test with $5, $10, $100 - change here
LEVERAGE = 5
WALLET_PERCENT = 0.99
FEE_RATE = 0.0012 # 0.12% taker x2
SL_MARGIN = 0.15 # -15%

exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_SECRET,
    'options': {'defaultType': 'swap'}
})

def backtest_funding(days=30):
    print(f"=== BACKTEST {days} DAYS | Initial ${INITIAL_BALANCE} ===")
    
    # Get all funding pairs
    rates = exchange.fetch_funding_rates()
    symbols = [s for s in rates if 'USDT:USDT' in s][:50] # Top 50
    
    balance = INITIAL_BALANCE
    trades = []
    
    # For each symbol, get funding history
    for symbol in symbols[:20]: # Test 20 top
        try:
            # Fetch funding history last 30 days
            history = exchange.fetch_funding_rate_history(symbol, limit=90) # 90 = 30 days * 3 per day
            
            for funding in history:
                rate = float(funding['fundingRate'])
                if abs(rate) < 0.003: # Skip <0.3%
                    continue
                
                position_usdt = balance * WALLET_PERCENT * LEVERAGE
                gross = position_usdt * abs(rate)
                fee = position_usdt * FEE_RATE
                net = gross - fee
                
                if net <= 0:
                    continue
                
                # Simulate close: 80% chance close at avg/profit (random 0 to +0.5%)
                # 20% chance SL -3% spot
                import random
                is_profit_close = random.random() < 0.80
                
                if is_profit_close:
                    # Close at +0.1% avg
                    price_pnl = 0.001
                    margin_pnl = price_pnl * LEVERAGE
                    total_pnl = net + (position_usdt * price_pnl)
                else:
                    # SL hit -3% spot = -15% margin
                    total_pnl = - (balance * WALLET_PERCENT * SL_MARGIN) + net
                    margin_pnl = -SL_MARGIN
                
                new_balance = balance + total_pnl
                trades.append({
                    'date': datetime.fromtimestamp(funding['timestamp']/1000),
                    'symbol': symbol,
                    'rate': rate*100,
                    'balance_before': balance,
                    'gross': gross,
                    'fee': fee,
                    'net_funding': net,
                    'total_pnl': total_pnl,
                    'balance_after': new_balance,
                    'result': 'PROFIT' if is_profit_close else 'SL'
                })
                balance = new_balance
                
                # Stop if bust
                if balance < 0.5:
                    break
            if balance < 0.5:
                break
        except Exception as e:
            print(f"Skip {symbol}: {e}")
            continue
    
    df = pd.DataFrame(trades)
    if len(df) == 0:
        print("No profitable funding found last 30 days")
        return
    
    print(f"\n=== RESULTS ===")
    print(f"Initial: ${INITIAL_BALANCE:.2f} -> Final: ${balance:.2f}")
    print(f"Total Profit: ${balance-INITIAL_BALANCE:.2f} ({(balance/INITIAL_BALANCE-1)*100:.1f}%)")
    print(f"Total Trades: {len(df)}")
    print(f"Win Rate: {(df['result']=='PROFIT').mean()*100:.1f}%")
    print(f"Avg Funding: {df['rate'].mean():.4f}%")
    print(f"\nLast 10 trades:\n{df.tail(10)[['date','symbol','rate','total_pnl','balance_after','result']].to_string()}")
    
    # Save CSV
    df.to_csv('backtest_results.csv', index=False)
    print("\nSaved to backtest_results.csv")
    
    # Compound projection
    print(f"\n=== COMPOUND PROJECTION (if you continue) ===")
    monthly_return = (balance/INITIAL_BALANCE - 1)
    print(f"30 days: ${INITIAL_BALANCE} -> ${balance:.2f} = {monthly_return*100:.1f}%")
    print(f"If $5 for 60 days: ${5 * (1+monthly_return)**2:.2f}")
    print(f"If $5 for 90 days: ${5 * (1+monthly_return)**3:.2f}")

if __name__ == "__main__":
    backtest_funding(30)
