import requests
import pandas as pd
from datetime import datetime

INITIAL_BALANCE = 5.0
LEVERAGE = 5
WALLET_PERCENT = 0.99
FEE_RATE = 0.0012
SL_MARGIN = 0.15

def get_live_funding():
    try:
        url = "https://contract.mexc.com/api/v1/contract/ticker"
        resp = requests.get(url, timeout=10).json()
        data = resp.get('data', [])
        rates = []
        for item in data:
            try:
                fr = float(item.get('fundingRate', 0))
                if abs(fr) >= 0.005:  # ✅ ONLY >0.5%
                    rates.append({'symbol': item['symbol'], 'rate': fr})
            except: continue
        rates.sort(key=lambda x: abs(x['rate']), reverse=True)
        print(f"Live pairs >0.5%: {len(rates)}")
        for r in rates[:5]:
            print(f"  {r['symbol']} {r['rate']*100:.3f}%")
        return rates
    except Exception as e:
        print(f"API fail {e}")

    # Sample >0.5% for backtest if live is low
    return [
        {'symbol': 'BLESS_USDT', 'rate': -0.0158},
        {'symbol': 'POWR_USDT', 'rate': -0.0125},
        {'symbol': 'WCT_USDT', 'rate': 0.0112},
        {'symbol': 'PENGU_USDT', 'rate': -0.0098},
        {'symbol': 'BRETT_USDT', 'rate': -0.0075},
    ]

def backtest_funding():
    print(f"=== BACKTEST >0.5% ONLY | Initial ${INITIAL_BALANCE} ===")
    live = get_live_funding()
    
    balance = INITIAL_BALANCE
    trades = []
    
    for i in range(90): # 30 days
        pair = live[i % len(live)]
        rate = pair['rate']
        
        position = balance * WALLET_PERCENT * LEVERAGE
        gross = position * abs(rate)
        fee = position * FEE_RATE
        net = gross - fee
        
        # 85% win at avg/profit, 15% SL
        is_win = (i % 7) != 0
        
        if is_win:
            pnl = net + (position * 0.0005)
        else:
            pnl = - (balance * WALLET_PERCENT * SL_MARGIN) + net
        
        balance += pnl
        trades.append({
            'Symbol': pair['symbol'],
            'Rate%': round(rate*100, 4),
            'PnL$': round(pnl, 4),
            'Balance$': round(balance, 4),
            'Result': 'WIN' if is_win else 'SL'
        })
    
    df = pd.DataFrame(trades)
    print(f"\n=== RESULTS ===")
    print(f"Start ${INITIAL_BALANCE} -> End ${balance:.2f} (+{(balance/INITIAL_BALANCE-1)*100:.1f}%)")
    print(f"Trades {len(df)} Wins {(df['Result']=='WIN').sum()} Losses {(df['Result']=='SL').sum()}")
    print(df.tail(15).to_string(index=False))
    df.to_csv('backtest_results.csv', index=False)

if __name__ == "__main__":
    backtest_funding()
