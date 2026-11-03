import ccxt
import time
import os
import sys
from datetime import datetime, timezone

print("BOT STARTED - Wallet check...", flush=True)
print(f"Time: {datetime.now(timezone.utc)} UTC", flush=True)

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
LIVE = os.getenv("LIVE", "false").lower() == "true"

if not API_KEY:
    print("ERROR: MEXC_API_KEY missing!", flush=True)
    sys.exit(1)

exchange = ccxt.mexc({
    'apiKey': API_KEY,
    'secret': SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

def get_wallet():
    try:
        bal = exchange.fetch_balance()
        usdt = bal.get('USDT', {}).get('free', 0) or bal.get('total', {}).get('USDT', 0) or 0
        print(f"Wallet: ${usdt:.2f} USDT | LIVE={LIVE}", flush=True)
        return float(usdt)
    except Exception as e:
        print(f"Wallet error: {e}", flush=True)
        return 0

def get_funding_opportunities():
    try:
        exchange.load_markets()
        tickers = exchange.fetch_funding_rates()
        ops = []
        for symbol, data in tickers.items():
            rate = data.get('fundingRate', 0)
            if rate is not None and rate <= -0.005:  # -0.5% or better (shorts get paid)
                ops.append((symbol, rate))
        ops.sort(key=lambda x: x[1])
        return ops[:5]
    except Exception as e:
        print(f"Funding fetch error: {e}", flush=True)
        return []

def trade_loop():
    wallet = get_wallet()
    if wallet < 1:
        print("Wallet < $1 - need to deposit to Futures!", flush=True)
        print("Go MEXC -> Futures -> Transfer from Spot to Futures", flush=True)

    while True:
        now = datetime.now(timezone.utc)
        # Next funding times are 00:00, 08:00, 16:00 UTC
        funding_hours = [0,8,16]
        next_hour = min([h for h in funding_hours if h > now.hour] + [24+funding_hours[0]])
        next_time = now.replace(hour=next_hour%24, minute=0, second=0, microsecond=0)
        if next_hour >=24:
            next_time = next_time.replace(day=now.day+1)

        mins_to = (next_time - now).total_seconds()/60
        print(f"\n[{now.strftime('%H:%M:%S')} UTC] Next funding {next_time.strftime('%H:%M')} UTC in {mins_to:.1f} min", flush=True)
        
        ops = get_funding_opportunities()
        if ops:
            print(f"Found {len(ops)} ops: {ops}", flush=True)
        else:
            print("No high funding yet (< -0.5%) - scanning...", flush=True)

        # If within 5 min before funding, enter
        if mins_to <= 5 and mins_to > 0 and ops and LIVE:
            symbol, rate = ops[0]
            try:
                print(f"TRADE: Short {symbol} rate {rate*100:.3f}% with 3x", flush=True)
                # Close all first
                try:
                    positions = exchange.fetch_positions()
                    for p in positions:
                        if float(p.get('contracts',0))>0:
                            exchange.create_order(p['symbol'], 'market', 'buy' if p['side']=='short' else 'sell', p['contracts'])
                except: pass

                bal = wallet * 0.99
                markets = exchange.markets[symbol]
                price = exchange.fetch_ticker(symbol)['last']
                amount = (bal * 3) / price
                # round amount
                amount = float(exchange.amount_to_precision(symbol, amount))
                if amount>0:
                    order = exchange.create_market_sell_order(symbol, amount)
                    print(f"OPENED short {symbol} {amount} @ {price} Order {order.get('id')}", flush=True)
                    # Wait for funding
                    sleep_sec = (mins_to*60)+90
                    print(f"Holding {sleep_sec/60:.1f} min to collect funding...", flush=True)
                    time.sleep(sleep_sec)
                    # Close
                    exchange.create_market_buy_order(symbol, amount)
                    print(f"CLOSED - Funding collected! New wallet:", flush=True)
                    get_wallet()
            except Exception as e:
                print(f"Trade error: {e}", flush=True)

        # Killer check: if price moved 2% against us, exit
        time.sleep(30)

if __name__ == "__main__":
    trade_loop()
