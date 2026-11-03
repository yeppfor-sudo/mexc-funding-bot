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
        # ONE CALL = ALL 400+ MEXC PERP - NO RATE LIMIT
        url = "https://contract.mexc.com/api/v1/contract/funding_rate"
        r = requests.get(url, timeout=15, headers={'User-Agent':'Mozilla/5.0'})
        data = r.json()
        rows = data.get('data', [])
        
        print(f"RAW MEXC: {len(rows)} perpetual contracts found (ALL 400+)", flush=True)
        
        ops = []
        for item in rows:
            try:
                symbol_raw = item.get('symbol', '')  # BMT_USDT
                if not symbol_raw.endswith('_USDT'):
                    continue
                rate = float(item.get('fundingRate', 0))
                # -0.4% threshold to catch more, you wanted 400+
                if rate <= -0.004:
                    base = symbol_raw.replace('_USDT','')
                    ccxt_symbol = f"{base}/USDT:USDT"
                    ops.append((ccxt_symbol, rate, symbol_raw))
                    print(f"FOUND {ccxt_symbol} {rate*100:.4f}% | raw {symbol_raw}", flush=True)
            except: continue
        
        ops.sort(key=lambda x: x[1])  # Most negative first (best profit)
        print(f"Scanned ALL {len(rows)} MEXC perp, found {len(ops)} good (< -0.4%)", flush=True)
        
        # Double-check BMT specifically (your coin today)
        if len(rows) > 0 and not any('BMT' in o[0] for o in ops):
            try:
                bmt = [x for x in rows if 'BMT' in x.get('symbol','')]
                if bmt:
                    rate = float(bmt[0].get('fundingRate',0))
                    print(f"BMT Check: {bmt[0].get('symbol')} = {rate*100:.4f}% (not enough? need -0.4%)", flush=True)
            except: pass
        
        # Return only symbol, rate for trading
        return [(o[0], o[1]) for o in ops[:5]]
        
    except Exception as e:
        print(f"Funding fetch error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return []

def trade_loop():
    wallet = get_wallet()
    if wallet < 1:
        print("Add funds to Futures wallet", flush=True)

    while True:
        now = datetime.now(timezone.utc)
        # Funding times 00,08,16 UTC
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
            print(f"Best trade: {ops[0][0]} {ops[0][1]*100:.4f}%", flush=True)
            # Show top 3
            for s,r in ops[:3]:
                print(f"  -> {s} {r*100:.3f}%", flush=True)
        else:
            print("No funding > -0.4% now - will keep scanning all 400+", flush=True)

        # ENTER 2.5 to 0.2 min before funding
        if 0.2 < mins_to <= 2.5 and ops and LIVE:
            symbol, rate = ops[0]
            try:
                print(f"\n=== TRADE WINDOW OPEN ===", flush=True)
                wallet = get_wallet()
                # Close old positions - killer protection
                try:
                    positions = exchange.fetch_positions()
                    for p in positions:
                        amt = float(p.get('contracts',0) or p.get('info',{}).get('holdVol',0) or 0)
                        if amt and amt != 0:
                            print(f"Closing old {p['symbol']} {amt}", flush=True)
                            side = 'buy' if p.get('side')=='short' or float(p.get('info',{}).get('holdVol',0) < 0 else 'sell'
                            # For MEXC, just market close
                            try:
                                exchange.create_order(p['symbol'], 'market', side, abs(amt))
                            except: pass
                except Exception as e:
                    print(f"Close old error: {e}", flush=True)

                ticker = exchange.fetch_ticker(symbol)
                price = ticker['last']
                bal = wallet * 0.97
                amount = (bal * 3) / price  # 3X leverage
                amount = float(exchange.amount_to_precision(symbol, amount))
                
                if amount <=0:
                    print("Amount too small", flush=True)
                    time.sleep(60)
                    continue

                print(f"TRADE NOW: Short {symbol} {rate*100:.3f}% amount {amount} @ {price} (3X)", flush=True)
                order = exchange.create_market_sell_order(symbol, amount)
                print(f"OPENED {symbol} ID {order.get('id')} - waiting funding...", flush=True)
                
                # Wait funding + 90 sec
                wait = int(mins_to*60 + 90)
                print(f"Sleeping {wait}s until after funding...", flush=True)
                time.sleep(wait)
                
                # Close with market buy
                print("Closing position...", flush=True)
                try:
                    exchange.create_market_buy_order(symbol, amount)
                    print("CLOSED - Funding collected!", flush=True)
                except Exception as e:
                    print(f"Close error: {e} - retry", flush=True)
                    time.sleep(5)
                    try:
                        exchange.create_market_buy_order(symbol, amount)
                    except Exception as e2:
                        print(f"Close failed: {e2}", flush=True)
                
                new_wallet = get_wallet()
                profit = new_wallet - wallet
                print(f"PROFIT: ${profit:.4f} | New total ${new_wallet:.4f}", flush=True)
                
            except Exception as e:
                print(f"Trade error: {e}", flush=True)
                import traceback
                traceback.print_exc()
        
        time.sleep(30)

if __name__ == "__main__":
    trade_loop()
