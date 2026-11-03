import ccxt
import time
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

print("=== FINAL v11 - ORDER BOOK + INFINITE WALLET ===", flush=True)

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
LIVE = os.getenv("LIVE", "false").lower() == "true"

LEVERAGE = 5
INVEST_PCT = 0.99
SL_PCT = 3.0
ENTRY_SEC = 45
EXIT_SEC = 3
MIN_FUNDING = 0.005
MAX_SLIPPAGE_PCT = 0.5  # avoid if book slips >0.5%

if not API_KEY:
    print("ERROR: MEXC_API_KEY missing!", flush=True)
    sys.exit(1)

exchange = ccxt.mexc({
    'apiKey': API_KEY, 'secret': SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

def get_wallet():
    try:
        bal = exchange.fetch_balance()
        usdt = bal.get('USDT', {}).get('free', 0) or 0
        if usdt == 0: usdt = bal.get('total',{}).get('USDT',0) or 0
        return float(usdt)
    except: return 0

def check_orderbook(symbol, wallet_usdt, side, rate):
    """
    Live order book check to avoid slip
    Returns: (can_fill, slippage_pct, max_fill_usdt, net_profit)
    For infinite wallet ♾️ - auto caps by book depth
    """
    try:
        # Position you WANT to open
        want_pos_usdt = wallet_usdt * INVEST_PCT * LEVERAGE
        
        ob = exchange.fetch_order_book(symbol, limit=50)
        bids = ob['bids']  # [price, amount]
        asks = ob['asks']
        
        if not bids or not asks:
            return False, 99, 0, 0
        
        ticker = exchange.fetch_ticker(symbol)
        mid_price = ticker['last']
        
        # For SHORT you sell into bids, for LONG you buy into asks
        book = bids if side == "SHORT" else asks
        
        # Walk book to calculate slippage for want_pos_usdt
        filled_usdt = 0
        filled_amount = 0
        weighted_price_sum = 0
        
        for price, amount in book:
            level_usdt = price * amount
            if filled_usdt + level_usdt >= want_pos_usdt:
                # Partial fill at this level
                need_usdt = want_pos_usdt - filled_usdt
                need_amount = need_usdt / price
                filled_amount += need_amount
                weighted_price_sum += need_amount * price
                filled_usdt = want_pos_usdt
                break
            else:
                filled_amount += amount
                weighted_price_sum += amount * price
                filled_usdt += level_usdt
        
        if filled_usdt < want_pos_usdt * 0.8:
            # Book too thin - can't fill 80% of wanted
            slippage = 99
            max_fill = filled_usdt
        else:
            avg_price = weighted_price_sum / filled_amount if filled_amount else mid_price
            slippage = abs(avg_price - mid_price) / mid_price * 100
            max_fill = filled_usdt
        
        # Net profit calculation with slippage + fees
        # Funding profit - slippage loss - fees (0.04% approx)
        funding_profit = want_pos_usdt * abs(rate)
        slippage_cost = want_pos_usdt * (slippage/100)
        fee_cost = want_pos_usdt * 0.0004  # 0.04% round trip
        net_profit = funding_profit - slippage_cost - fee_cost
        
        can_fill = slippage <= MAX_SLIPPAGE_PCT and filled_usdt >= want_pos_usdt * 0.8
        
        return can_fill, slippage, max_fill, net_profit
        
    except Exception as e:
        print(f"Orderbook error {symbol}: {e}", flush=True)
        return False, 99, 0, 0

def get_all_funding_smart():
    try:
        r1 = requests.get("https://contract.mexc.com/api/v1/contract/funding_rate", timeout=15).json()
        funding_list = r1.get('data', [])
        r2 = requests.get("https://contract.mexc.com/api/v1/contract/detail", timeout=15).json()
        details = {item['symbol']: item for item in r2.get('data', [])}
        
        ops = []
        now = datetime.now(timezone.utc)
        wallet = get_wallet()
        
        for f in funding_list:
            sym = f.get('symbol','')
            if not sym.endswith('_USDT'): continue
            rate = float(f.get('fundingRate',0))
            if abs(rate) < MIN_FUNDING: continue
            
            det = details.get(sym, {})
            next_fund_ms = det.get('nextFundingTime') or f.get('nextFundingTime') or 0
            interval = det.get('fundingInterval') or 8
            try:
                interval = int(interval)
                if interval not in [4,8]: interval = 8 if interval >= 8 else 4
            except: interval = 8
            
            if next_fund_ms:
                next_dt = datetime.fromtimestamp(next_fund_ms/1000, tz=timezone.utc)
            else:
                hours = [0,4,8,12,16,20] if interval == 4 else [0,8,16]
                next_dt = None
                for h in hours:
                    cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
                    if cand > now:
                        next_dt = cand
                        break
                if next_dt is None:
                    next_dt = (now + timedelta(days=1)).replace(hour=hours[0], minute=0, second=0, microsecond=0)
            
            secs_to = (next_dt - now).total_seconds()
            base = sym.replace('_USDT','')
            ccxt_sym = f"{base}/USDT:USDT"
            
            # Direction
            side = "SHORT" if rate > 0 else "LONG"
            
            ops.append({
                'symbol': ccxt_sym, 'mexc_symbol': sym, 'rate': rate,
                'abs_rate': abs(rate), 'interval': interval,
                'next_dt': next_dt, 'secs_to': secs_to, 'side': side
            })
        
        if not ops:
            return []
        
        # Smart time filter
        min_secs = min(o['secs_to'] for o in ops)
        settling_now = [o for o in ops if o['secs_to'] <= min_secs + 600]
        
        # --- ORDER BOOK CHECK FOR YOUR WALLET (infinite capable) ---
        print(f"Checking order book for wallet ${wallet:.2f} @ {LEVERAGE}X = Pos ${wallet*INVEST_PCT*LEVERAGE:.2f}...", flush=True)
        
        scored = []
        for o in settling_now[:15]:  # check top 15 to save API
            can_fill, slip, max_fill, net_profit = check_orderbook(o['symbol'], wallet, o['side'], o['rate'])
            o['slippage'] = slip
            o['max_fill'] = max_fill
            o['net_profit'] = net_profit
            o['can_fill'] = can_fill
            
            # Adjust for infinite wallet: if wallet huge, pick coin with biggest book
            status = "OK" if can_fill else f"THIN BOOK slip {slip:.2f}%"
            print(f"  {o['symbol']} {o['rate']*100:+.3f}% {o['interval']}H {o['side']} | Book max ${max_fill:.0f} | Slip {slip:.3f}% | Net ${net_profit:.3f} | {status}", flush=True)
            
            if net_profit > 0:  # only profitable after slip
                scored.append(o)
        
        # Sort by NET profit (funding - slippage - fees), not just funding %
        # This auto-picks best for YOUR wallet size, even if ♾️
        if scored:
            scored.sort(key=lambda x: x['net_profit'], reverse=True)
            best = scored[0]
            print(f"Scanned {len(funding_list)} perp | Found {len(ops)} >0.5% | Settling now {len(settling_now)} | BOOK BEST: {best['symbol']} {best['rate']*100:.3f}% | Slip {best['slippage']:.3f}% | Net ${best['net_profit']:.3f} for your ${wallet:.2f} wallet | Wallet ${wallet:.4f}", flush=True)
            return scored
        else:
            print(f"No coin with profitable book for ${wallet:.2f} - all too thin!", flush=True)
            return settling_now  # fallback to old logic if all thin
        
    except Exception as e:
        print(f"Smart error: {e}", flush=True)
        return []

def trade_loop():
    wallet_start = get_wallet()
    print(f"START Wallet ${wallet_start:.4f} (♾️ capable) | {LEVERAGE}X {INVEST_PCT*100:.0f}% | SL {SL_PCT}% = {SL_PCT*LEVERAGE}% max | ENTRY {ENTRY_SEC}s EXIT +{EXIT_SEC}s | MAX SLIPPAGE {MAX_SLIPPAGE_PCT}%", flush=True)
    
    while True:
        now = datetime.now(timezone.utc)
        ops = get_all_funding_smart()
        
        if not ops:
            time.sleep(15)
            continue
        
        next_dt = ops[0]['next_dt']
        secs_to = (next_dt - now).total_seconds()
        mins_to = secs_to / 60
        
        if secs_to < 180:
            print(f"[{now.strftime('%H:%M:%S')} UTC] {secs_to:.0f}s to funding | Best NET: {ops[0]['symbol']} {ops[0]['rate']*100:.3f}% Slip {ops[0].get('slippage',0):.3f}% Net ${ops[0].get('net_profit',0):.3f}", flush=True)

        if 0.02 < mins_to <= (ENTRY_SEC/60) and ops and LIVE:
            best = ops[0]
            symbol = best['symbol']
            rate = best['rate']
            side = best['side']
            wallet = get_wallet()
            
            open_func = exchange.create_market_sell_order if side == "SHORT" else exchange.create_market_buy_order
            close_func = exchange.create_market_buy_order if side == "SHORT" else exchange.create_market_sell_order
            
            print(f"\n=== ENTRY {secs_to:.0f}s BEFORE | {symbol} {rate*100:.4f}% {best['interval']}H | {side} NET ${best.get('net_profit',0):.3f} Slip {best.get('slippage',0):.3f}% ===", flush=True)
            
            try:
                entry_price = exchange.fetch_ticker(symbol)['last']
                invest_usdt = wallet * INVEST_PCT
                
                # For infinite wallet: cap position by order book depth
                max_fill = best.get('max_fill', float('inf'))
                want_pos = invest_usdt * LEVERAGE
                # If book can't fill wanted, use max fill (for ♾️ wallet)
                actual_pos = min(want_pos, max_fill * 0.9)  # 90% of book to be safe
                amount = float(exchange.amount_to_precision(symbol, actual_pos / entry_price))
                
                sl_price = entry_price * (1 + SL_PCT/100) if side == "SHORT" else entry_price * (1 - SL_PCT/100)
                
                print(f"Wallet ${wallet:.4f} | Want Pos ${want_pos:.2f} | Book Max ${max_fill:.2f} | Actual Pos ${actual_pos:.2f} | {side} {amount} @ {entry_price} | SL @ {sl_price:.6f}", flush=True)
                
                open_func(symbol, amount)
                print(f"OPENED {side}", flush=True)

                exit_time = next_dt + timedelta(seconds=EXIT_SEC)
                closed = False
                
                while datetime.now(timezone.utc) < exit_time:
                    try:
                        cur = exchange.fetch_ticker(symbol)['last']
                        if side == "SHORT" and cur >= sl_price:
                            print(f"!!! SL HIT SHORT -> CLOSE", flush=True)
                            try: close_func(symbol, amount)
                            except: pass
                            closed = True
                            break
                        if side == "LONG" and cur <= sl_price:
                            print(f"!!! SL HIT LONG -> CLOSE", flush=True)
                            try: close_func(symbol, amount)
                            except: pass
                            closed = True
                            break
                    except: pass
                    time.sleep(0.5)

                if not closed:
                    print(f"GUARANTEED CLOSE +{EXIT_SEC}s ALL SITUATIONS", flush=True)
                    try: close_func(symbol, amount)
                    except:
                        time.sleep(1)
                        try: close_func(symbol, amount)
                        except: pass

                new_wallet = get_wallet()
                print(f"RESULT ${new_wallet - wallet:+.4f} | New ${new_wallet:.4f}", flush=True)

            except Exception as e:
                print(f"Trade error: {e}", flush=True)
            
            time.sleep(90)
            continue

        time.sleep(2 if secs_to < 120 else 10)

if __name__ == "__main__":
    trade_loop()
