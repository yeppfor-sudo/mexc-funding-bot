import ccxt
import time
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

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
        usdt = bal.get('USDT', {}).get('free', 0) or 0
        if usdt == 0:
            usdt = bal.get('total', {}).get('USDT', 0) or 0
        print(f"Wallet: ${float(usdt):.4f} USDT | LIVE={LIVE}", flush=True)
        return float(usdt)
    except Exception as e:
        print(f"Wallet error: {e}", flush=True)
        return 0

def get_funding_opportunities():
    try:
        url = "https://contract.mexc.com/api/v1/contract/funding_rate"
        r = requests.get(url, timeout=15, headers={'User-Agent':'Mozilla/5.0'})
        data = r.json()
        rows = data.get('data', [])
        print(f"RAW MEXC: {len(rows)} perpetual contracts found (ALL 400+)", flush=True)
        ops = []
        for item in rows:
            try:
                symbol_raw = item.get('symbol', '')
                if not symbol_raw.endswith('_USDT'):
                    continue
                rate = float(item.get('fundingRate', 0))
                if rate <= -0.004:
                    base = symbol_raw.replace('_USDT','')
                    ccxt_symbol = f"{base}/USDT:USDT"
                    ops.append((ccxt_symbol, rate))
                    print(f"FOUND {ccxt_symbol} {rate*100:.4f}%", flush=True)
            except: continue
        ops.sort(key=lambda x: x[1])
        print(f"Scanned ALL {len(rows)} MEXC perp, found {len(ops)} good (< -0.4%)", flush=True)
        return ops[:5]
    except Exception as e:
        print(f"Funding fetch error: {e}", flush=True)
        return []

def trade_loop():
    wallet = get_wallet()
    while True:
        now = datetime.now(timezone.utc)
        funding_times = [0,8,16]
        next_h = None
        for h in funding_times:
            if h > now.hour or (h == now.hour and now.minute < 3):
                next_h = h
                break
        if next_h is None:
            next_h = 0
            next_dt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            next_dt = now.replace(hour=next_h, minute=0, second=0, microsecond=0)
        
        mins_to = (next_dt - now).total_seconds()/60
        print(f"\n[{now.strftime('%H:%M:%S')} UTC] Next funding {next_dt.strftime('%H:%M')} UTC in {mins_to:.1f} min", flush=True)

        ops = get_funding_opportunities()
        if ops:
            print(f"Best: {ops[0][0]} {ops[0][1]*100:.4f}%", flush=True)
        else:
            print("No funding > -0.4% - scanning all 400+", flush=True)

        if 0.2 < mins_to <= 2.5 and ops and LIVE:
            symbol, rate = ops[0]
            try:
                wallet = get_wallet()
                # Close old positions simple
                try:
                    positions = exchange.fetch_positions()
                    for p in positions:
                        amt = 0
                        try:
                            amt = float(p.get('contracts',0))
                        except: pass
                        if amt:
                            print(f"Closing old {p['symbol']}", flush=True)
                            try:
                                if amt > 0:
                                    exchange.create_market_sell_order(p['symbol'], abs(amt))
                                else:
                                    exchange.create_market_buy_order(p['symbol'], abs(amt))
                            except: pass
                except Exception as e:
                    print(f"Close old error: {e}", flush=True)

                ticker = exchange.fetch_ticker(symbol)
                price = ticker['last']
                bal = wallet * 0.97
                amount = (bal * 3) / price
                amount = float(exchange.amount_to_precision(symbol, amount))
                
                if amount <=0:
                    time.sleep(60)
                    continue

                print(f"TRADE NOW: Short {symbol} {rate*100:.3f}% amount {amount} @ {price}", flush=True)
                order = exchange.create_market_sell_order(symbol, amount)
                print(f"OPENED {order.get('id')} - waiting funding...", flush=True)
                
                wait = int(mins_to*60 + 90)
                time.sleep(wait)
                
                print("Closing position...", flush=True)
                exchange.create_market_buy_order(symbol, amount)
                print("CLOSED - Funding collected!", flush=True)
                
                new_wallet = get_wallet()
                print(f"PROFIT: ${new_wallet - wallet:.4f} | Total ${new_wallet:.4f}", flush=True)
                
            except Exception as e:
                print(f"Trade error: {e}", flush=True)
        
        time.sleep(30)

if __name__ == "__main__":
    trade_loop()
