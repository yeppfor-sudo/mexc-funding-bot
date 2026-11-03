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
        ops = []
        symbols = [s for s in exchange.markets if ':USDT' in s][:50]
        for symbol in symbols:
            try:
                data = exchange.fetch_funding_rate(symbol)
                rate = data.get('fundingRate')
                if rate is not None and rate <= -0.005:
                    ops.append((symbol, rate))
                    print(f"FOUND {symbol} {rate*100:.4f}%", flush=True)
                time.sleep(0.3)
            except:
                continue
        ops.sort(key=lambda x: x[1])
        print(f"Scanned {len(symbols)} markets, found {len(ops)} good", flush=True)
        return ops[:5]
    except Exception as e:
        print(f"Funding fetch error: {e}", flush=True)
        return []

def trade_loop():
    wallet = get_wallet()
    while True:
        now = datetime.now(timezone.utc)
        mins_to_16 = ((16 - now.hour) % 24)*60 - now.minute
        if mins_to_16 < 0:
            mins_to_16 += 24*60
        print(f"\n[{now.strftime('%H:%M:%S')} UTC] Next funding 16:00 UTC in {mins_to_16:.1f} min", flush=True)
        
        ops = get_funding_opportunities()
        if not ops:
            print("No high funding yet (< -0.5%) - scanning...", flush=True)
        else:
            print(f"Best: {ops[0]}", flush=True)

        if mins_to_16 <= 5 and mins_to_16 > 0 and ops and LIVE:
            symbol, rate = ops[0]
            try:
                print(f"TRADE NOW: Short {symbol} {rate*100:.3f}%", flush=True)
                price = exchange.fetch_ticker(symbol)['last']
                bal = wallet * 0.99
                amount = (bal * 3) / price
                amount = float(exchange.amount_to_precision(symbol, amount))
                print(f"Amount {amount} @ {price}", flush=True)
                exchange.create_market_sell_order(symbol, amount)
                print("OPENED - waiting 90s for funding", flush=True)
                time.sleep(90)
                exchange.create_market_buy_order(symbol, amount)
                print("CLOSED - Funding collected!", flush=True)
                get_wallet()
            except Exception as e:
                print(f"Trade error: {e}", flush=True)

        time.sleep(30)

trade_loop()
