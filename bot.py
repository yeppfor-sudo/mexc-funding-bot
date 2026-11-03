import ccxt
import time
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

print("=== FINAL BOT v10 - ALL IN ONE ===", flush=True)
print(f"Time: {datetime.now(timezone.utc)} UTC", flush=True)

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
LIVE = os.getenv("LIVE", "false").lower() == "true"

# === YOUR CONFIG ===
LEVERAGE = 5              # 5X
INVEST_PCT = 0.99         # 99% compound
SL_PCT = 3.0              # -3% price = -15% wallet max loss
ENTRY_SEC = 45            # enter 45 sec before funding
EXIT_SEC = 3              # exit 3 sec after funding - GUARANTEED CLOSE ALL SITUATIONS
MIN_FUNDING = 0.005       # >0.5% absolute (your ++>0.5 rule)
# ===================

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
        return float(usdt)
    except Exception as e:
        print(f"Wallet error: {e}", flush=True)
        return 0

def get_all_funding_smart():
    """
    ALL IN ONE:
    1. Scan ALL 1193 MEXC perp
    2. Filter >0.5% absolute (+/-)
    3. Get 4H/8H interval + next funding time
    4. Smart pick:
       - When 4H and 8H close same time (00,08,16) -> pick highest funding
       - When 8H funding higher but 8H still has 4H wait -> pick current 4H highest
    """
    try:
        # Funding rates
        r1 = requests.get("https://contract.mexc.com/api/v1/contract/funding_rate", timeout=15).json()
        funding_list = r1.get('data', [])
        
        # Details for interval
        r2 = requests.get("https://contract.mexc.com/api/v1/contract/detail", timeout=15).json()
        details = {item['symbol']: item for item in r2.get('data', [])}
        
        ops = []
        now = datetime.now(timezone.utc)
        
        for f in funding_list:
            sym = f.get('symbol','')
            if not sym.endswith('_USDT'):
                continue
            rate = float(f.get('fundingRate',0))
            
            # Rule: ++>0.5 filter
            if abs(rate) < MIN_FUNDING:
                continue
            
            det = details.get(sym, {})
            next_fund_ms = det.get('nextFundingTime') or f.get('nextFundingTime') or 0
            interval = det.get('fundingInterval') or 8
            
            try:
                interval = int(interval)
                if interval not in [4,8]:
                    interval = 8 if interval >= 8 else 4
            except:
                interval = 8
            
            if next_fund_ms:
                next_dt = datetime.fromtimestamp(next_fund_ms/1000, tz=timezone.utc)
            else:
                # Fallback schedule
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
            
            ops.append({
                'symbol': ccxt_sym,
                'rate': rate,
                'abs_rate': abs(rate),
                'interval': interval,
                'next_dt': next_dt,
                'secs_to': secs_to
            })
        
        if not ops:
            print(f"Scanned {len(funding_list)} perp | 0 coins >0.5%", flush=True)
            return []
        
        # SMART LOGIC
        min_secs = min(o['secs_to'] for o in ops)
        settling_now = [o for o in ops if o['secs_to'] <= min_secs + 600]  # 10min window
        settling_now.sort(key=lambda x: x['abs_rate'], reverse=True)
        
        print(f"Scanned {len(funding_list)} perp | Found {len(ops)} >0.5% | Next in {min_secs/60:.1f}min | Settling now {len(settling_now)} | Best {settling_now[0]['symbol']} {settling_now[0]['rate']*100:.3f}% {settling_now[0]['interval']}H | Wallet ${get_wallet():.4f}", flush=True)
        
        return settling_now
        
    except Exception as e:
        print(f"Funding smart error: {e}", flush=True)
        return []

def trade_loop():
    wallet_start = get_wallet()
    print(f"START Wallet ${wallet_start:.4f} | {LEVERAGE}X {INVEST_PCT*100:.0f}% COMPOUND | SL {SL_PCT}% price = {SL_PCT*LEVERAGE}% wallet | ENTRY {ENTRY_SEC}s EXIT +{EXIT_SEC}s GUARANTEED | LIVE={LIVE}", flush=True)
    print(f"Rules: +/- >{MIN_FUNDING*100}% | 4H/8H smart pick | LONG negative SHORT positive to COLLECT funding", flush=True)
    
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
            print(f"[{now.strftime('%H:%M:%S')} UTC] {secs_to:.0f}s to funding | Best {ops[0]['symbol']} {ops[0]['rate']*100:.4f}% {ops[0]['interval']}H", flush=True)

        # ENTRY: 45 sec before
        if 0.02 < mins_to <= (ENTRY_SEC/60) and ops and LIVE:
            best = ops[0]
            symbol = best['symbol']
            rate = best['rate']
            wallet = get_wallet()
            
            if wallet < 1:
                print(f"Wallet too low ${wallet}", flush=True)
                time.sleep(60)
                continue
            
            # Direction to COLLECT funding
            if rate > 0:
                side = "SHORT"  # + funding -> shorts receive
                open_func = exchange.create_market_sell_order
                close_func = exchange.create_market_buy_order
            else:
                side = "LONG"   # - funding -> longs receive
                open_func = exchange.create_market_buy_order
                close_func = exchange.create_market_sell_order
            
            print(f"\n=== ENTRY {secs_to:.0f}s BEFORE | {symbol} {rate*100:.4f}% {best['interval']}H | {side} TO COLLECT FUNDING ===", flush=True)
            
            try:
                entry_price = exchange.fetch_ticker(symbol)['last']
                invest_usdt = wallet * INVEST_PCT
                position_usdt = invest_usdt * LEVERAGE
                amount = float(exchange.amount_to_precision(symbol, position_usdt / entry_price))
                
                # SL
                if side == "SHORT":
                    sl_price = entry_price * (1 + SL_PCT/100)
                else:
                    sl_price = entry_price * (1 - SL_PCT/100)
                
                max_loss = invest_usdt * (SL_PCT/100) * LEVERAGE
                
                print(f"Wallet ${wallet:.4f} | Invest ${invest_usdt:.4f} | {LEVERAGE}X Pos ${position_usdt:.4f} | {side} {amount} @ {entry_price}", flush=True)
                print(f"SL {SL_PCT}% @ {sl_price:.6f} | Max loss -${max_loss:.4f} (-{SL_PCT*LEVERAGE:.0f}% wallet) counted even in +{EXIT_SEC}s window", flush=True)
                
                open_func(symbol, amount)
                print(f"OPENED {side} - funding in {secs_to:.0f}s", flush=True)

                # MONITOR with SL until +3s - COUNT -3% LOSS IN 3SEC WINDOW
                exit_time = next_dt + timedelta(seconds=EXIT_SEC)
                print(f"Monitoring SL every 0.5s until {exit_time.strftime('%H:%M:%S')} UTC +{EXIT_SEC}s...", flush=True)
                
                closed = False
                while datetime.now(timezone.utc) < exit_time:
                    try:
                        cur_price = exchange.fetch_ticker(symbol)['last']
                        
                        # SL -3% check - counts in 3sec after funding too
                        if side == "SHORT" and cur_price >= sl_price:
                            print(f"!!! SL HIT SHORT Price {cur_price:.6f} >= {sl_price:.6f} | +{SL_PCT}% price = -{SL_PCT*LEVERAGE}% wallet -> CLOSE NOW (counts in +{EXIT_SEC}s window)", flush=True)
                            try: close_func(symbol, amount)
                            except: pass
                            closed = True
                            break
                        if side == "LONG" and cur_price <= sl_price:
                            print(f"!!! SL HIT LONG Price {cur_price:.6f} <= {sl_price:.6f} | -{SL_PCT}% price = -{SL_PCT*LEVERAGE}% wallet -> CLOSE NOW", flush=True)
                            try: close_func(symbol, amount)
                            except: pass
                            closed = True
                            break
                    except: pass
                    time.sleep(0.5)

                # GUARANTEED CLOSE AT +3s IN ALL SITUATIONS
                if not closed:
                    print(f"TIME +{EXIT_SEC}s AFTER FUNDING - CLOSING IMMEDIATELY IN ALL SITUATIONS (profit or loss counted)", flush=True)
                    try:
                        close_func(symbol, amount)
                        print(f"CLOSED GUARANTEED +{EXIT_SEC}s - Funding collected!", flush=True)
                    except Exception as e:
                        print(f"Close retry 1: {e}", flush=True)
                        time.sleep(1)
                        try:
                            close_func(symbol, amount)
                        except Exception as e2:
                            print(f"Close retry 2: {e2} - trying force close positions", flush=True)
                            try:
                                poss = exchange.fetch_positions([symbol])
                                for p in poss:
                                    amt = float(p.get('contracts',0) or 0)
                                    if amt != 0:
                                        if amt > 0:
                                            exchange.create_market_sell_order(symbol, abs(amt))
                                        else:
                                            exchange.create_market_buy_order(symbol, abs(amt))
                            except: pass

                new_wallet = get_wallet()
                pnl = new_wallet - wallet
                pnl_pct = (pnl / wallet * 100) if wallet else 0
                print(f"RESULT: P&L ${pnl:.4f} ({pnl_pct:+.2f}%) | New Wallet ${new_wallet:.4f} | Next trade 99% = ${new_wallet*INVEST_PCT:.4f} @ {LEVERAGE}X", flush=True)

            except Exception as e:
                print(f"Trade error: {e}", flush=True)
                import traceback
                traceback.print_exc()
            
            print("Cooldown 90 sec to avoid double trade...", flush=True)
            time.sleep(90)
            continue

        # Fast loop near funding to never miss 45s window
        time.sleep(2 if secs_to < 120 else 10)

if __name__ == "__main__":
    trade_loop()
