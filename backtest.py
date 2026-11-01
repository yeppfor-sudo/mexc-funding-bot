import ccxt
import pandas as pd
import requests
from datetime import datetime
import os
import random

INITIAL_BALANCE = 5.0
LEVERAGE = 5
WALLET_PERCENT = 0.99
FEE_RATE = 0.0012
SL_MARGIN = 0.15

def get_funding_history_fixed():
    """Use direct MEXC API for backtest"""
    try:
        url = "https://contract.mexc.com/api/v1/contract/ticker"
        resp = requests.get(url, timeout=10).json()
        data = resp.get('data', [])
        history = []
        for item in data[:30]:
            funding = float(item.get('fundingRate', 0))
            if abs(funding) < 0.002:
                continue
            symbol = item['symbol'].replace('_', '/') + ':USDT'
            # Simulate 90 funding periods (30 days)
            for i in range(90):
                history.append({
                    'symbol': symbol,
                    'rate': funding + random.uniform(-0.002, 0.002), # Add variation
                    'timestamp': datetime.now()
                })
        return history
    except Exception as e:
        print(f"API error {e}, using sample data")
        return [
            {'symbol': 'BLESS/USDT:USDT', 'rate': -0.0158, 'timestamp': datetime.now()},
            {'symbol': 'POWR/USDT:USDT', 'rate': -0.012, 'timestamp': datetime.now()},
        ] * 90

def backtest_funding():
    print(f"=== BACKTEST | Initial ${INITIAL_BALANCE} ===")
    history = get_funding_history_fixed()
    balance = INITIAL_BALANCE
    trades = []
    
    for h in history:
        rate = h['rate']
        if abs(rate) < 0.003:
            continue
        position = balance * WALLET_PERCENT * LEVERAGE
        gross = position * abs(rate)
        fee = position * FEE_RATE
        net = gross - fee
        if net < 0.05:
            continue
        
        # 80% win at avg/profit, 20% SL
        is_win = random.random() < 0.80
        if is_win:
            pnl = net + (position * 0.001)
        else:
            pnl = - (balance * WALLET_PERCENT * SL_MARGIN) + net
        
        new_bal = balance + pnl
        trades.append({
            'symbol': h['symbol'],
            'rate': rate*100,
            'pnl': pnl,
            'balance': new_bal,
            'result': 'WIN' if is_win else 'SL'
        })
        balance = new_bal
        if balance < 0.5:
            break
    
    df = pd.DataFrame(trades)
    if len(df)==0:
        print("No trades")
        return
    print(f"Initial ${INITIAL_BALANCE:.2f} -> Final ${balance:.2f} Profit ${balance-INITIAL_BALANCE:.2f} ({(balance/INITIAL_BALANCE-1)*100:.1f}%)")
    print(f"Trades: {len(df)} Win: {(df['result']=='WIN').mean()*100:.1f}% Avg Rate: {df['rate'].mean():.3f}%")
    print(df.tail(15).to_string())
    df.to_csv('backtest_results.csv', index=False)
    print("Saved backtest_results.csv")

if __name__ == "__main__":
    backtest_funding()
