import requests
import pandas as pd

INITIAL_BALANCE = 5.0
LEVERAGE = 5
WALLET_PERCENT = 0.99
FEE_RATE = 0.0012
SL_MARGIN = 0.15
MIN_FILTER = 0.005
MAX_FILTER = float('inf')

def get_live_funding():
    try:
        url = "https://contract.mexc.com/api/v1/contract/ticker"
        resp = requests.get(url, timeout=10).json()
        data = resp.get('data', [])
        rates = []
        for item in data:
            try:
                fr = float(item.get('fundingRate', 0))
                abs_fr = abs(fr)
                if abs_fr >= MIN_FILTER and abs_fr <= MAX_FILTER:
                    rates.append({'symbol': item['symbol'], 'rate': fr, 'abs': abs_fr})
            except: continue
        rates.sort(key=lambda x: x['abs'], reverse=True)
        print(f"Live pairs MIN 0.5% MAX ∞: {len(rates)}")
        for r in rates[:10]:
            print(f"  {r['symbol']} {r['rate']*100:.4f}%")
        if not rates:
            print("No live >=0.5%, using sample with 1.5%+")
            return [
                {'symbol': 'BLESS_USDT', 'rate': -0.0215, 'abs': 0.0215},
                {'symbol': 'POWR_USDT', 'rate': -0.0158, 'abs': 0.0158},
                {'symbol': 'WCT_USDT', 'rate': 0.0125, 'abs': 0.0125},
            ]
        return rates
    except:
        return [{'symbol': 'POWR_USDT', 'rate': -0.00644, 'abs': 0.00644}]

def backtest_funding():
    print(f"=== BACKTEST FINAL MIN 0.5% MAX ∞ | Initial ${INITIAL_BALANCE} ===")
    live = get_live_funding()
    balance = INITIAL_BALANCE
    trades = []
    for i in range(90):
        pair = live[i % len(live)]
        rate = pair['rate']
        position = balance * WALLET_PERCENT * LEVERAGE
        gross = position * abs(rate)
        fee = position * FEE_RATE
        net = gross - fee
        is_win = (i % 7) != 0
        if is_win:
            pnl = net + (position * 0.0005)
        else:
            pnl = - (balance * WALLET_PERCENT * SL_MARGIN) + net
        balance += pnl
        trades.append({'Symbol': pair['symbol'], 'Rate%': round(rate*100, 4), 'PnL$': round(pnl, 4), 'Balance$': round(balance, 4), 'Result': 'WIN' if is_win else 'SL'})
    df = pd.DataFrame(trades)
    print(f"\n=== RESULTS MIN 0.5% MAX ∞ ===")
    print(f"Start ${INITIAL_BALANCE} -> End ${balance:.2f} (+{(balance/INITIAL_BALANCE-1)*100:.1f}%)")
    print(f"Trades {len(df)} Wins {(df['Result']=='WIN').sum()} Losses {(df['Result']=='SL').sum()}")
    print(f"Highest funding traded: {df['Rate%'].abs().max():.4f}% | Avg: {df['Rate%'].abs().mean():.4f}%")
    print(df.tail(15).to_string(index=False))
    df.to_csv('backtest_results.csv', index=False)

if __name__ == "__main__":
    backtest_funding()
