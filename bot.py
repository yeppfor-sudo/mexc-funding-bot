import ccxt
import time
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

print("=== FINAL v18.1 - FAST SCAN FIXED SYNTAX - MIN 0.5% MAX INF ===", flush=True)

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
LIVE = os.getenv("LIVE", "false").lower() == "true"

LEVERAGE = 5
INVEST_PCT = 0.99
SL_PCT = 3.0
ENTRY_SEC = 45
EXIT_SEC = 3
MIN_FUNDING = 0.005
MAX_SLIPPAGE_PCT = 0.5

if not API_KEY:
    print("ERROR: MEXC_API_KEY missing!", flush=True)
    sys.exit(1)

exchange = ccxt.mexc({
    'apiKey': API_KEY, 'secret': SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': 10000
})

def get_wallet():
    try:
        bal = exchange.fetch_balance()
        usdt = bal.get('USDT', {}).get('free', 0) or 0
        if usdt == 0: usdt = bal.get('total',{}).get('USDT',0) or 0
        return float(usdt)
    except: return 0

def check_orderbook_fast(symbol, wallet_usdt, side, rate):
    try:
        want_pos_usdt = wallet_usdt * INVEST_PCT * LEVERAGE
        if want_pos_usdt < 5: want_pos_usdt = 5
        ob = exchange.fetch_order_book(symbol, limit=20)
        bids = ob.get('bids', []); asks = ob.get('asks', [])
        if not bids or not asks: return False, 99, 0, -999
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2 if bids and asks else 0
        if not mid: return False, 99, 0, -999
        book = bids if side=="SHORT" else asks
        filled_usdt=0; filled_amt=0; w_sum=0
        for entry in book:
            try: price=float(entry[0]); amt=float(entry[1])
            except: continue
            if price<=0 or amt<=0: continue
            lvl=price*amt
            if filled_usdt+lvl >= want_pos_usdt:
                need=want_pos_usdt-filled_usdt
                need_amt=need/price if price else 0
                filled_amt+=need_amt; w_sum+=need_amt*price; filled_usdt=want_pos_usdt; break
            else:
                filled_amt+=amt; w_sum+=amt*price; filled_usdt+=lvl
        if filled_amt==0: return False,99,0,-999
        if filled_usdt < want_pos_usdt*0.9:
            slip=99; max_fill=filled_usdt
        else:
            avg=w_sum/filled_amt if filled_amt else mid
            slip=abs(avg-mid)/mid*100 if mid else 99
            max_fill=filled_usdt
        funding=want_pos_usdt*abs(rate)
        net=funding - want_pos_usdt*(slip/100) - want_pos_usdt*0.0004
        can=slip<=MAX_SLIPPAGE_PCT and filled_usdt>=want_pos_usdt*0.9 and net>0
        return can, slip, max_fill, net
    except:
        return False,99,0,-999

def get_all_funding_smart():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Fetching funding rates...", flush=True)
    try:
        r1 = requests.get("https://contract.mexc.com/api/v1/contract/funding_rate", timeout=10).json()
        funding_list = r1.get('data', [])
        print(f"Got {len(funding_list)} funding rates", flush=True)
        r2 = requests.get("https://contract.mexc.com/api/v1/contract/detail", timeout=10).json()
        details = {item['symbol']: item for item in r2.get('data', [])}
        print(f"Got {len(details)} details", flush=True)
        
        ops=[]
        now=datetime.now(timezone.utc)
        for f in funding_list:
            sym=f.get('symbol','')
            if not sym.endswith('_USDT'): continue
            rate=float(f.get('fundingRate',0))
            if abs(rate) < MIN_FUNDING: continue
            det=details.get(sym,{})
            next_ms=det.get('nextFundingTime') or f.get('nextFundingTime') or 0
            interval=det.get('fundingInterval') or 8
            try:
                interval=int(interval)
                if interval not in [4,8]: interval=8 if interval>=8 else 4
            except: interval=8
            if next_ms:
                next_dt=datetime.fromtimestamp(next_ms/1000, tz=timezone.utc)
            else:
                hours=[0,4,8,12,16,20] if interval==4 else [0,8,16]
                next_dt=None
                for h in hours:
                    cand=now.replace(hour=h, minute=0, second=0, microsecond=0)
                    if cand>now:
                        next_dt=cand; break
                if next_dt is None:
                    next_dt=(now+timedelta(days=1)).replace(hour=hours[0], minute=0, second=0, microsecond=0)
            secs=(next_dt-now).total_seconds()
            base=sym.replace('_USDT','')
            ccxt_sym=f"{base}/USDT:USDT"
            side="SHORT" if rate>0 else "LONG"
            ops.append({'symbol':ccxt_sym,'rate':rate,'abs_rate':abs(rate),'interval':interval,'next_dt':next_dt,'secs_to':secs,'side':side})
        
        if not ops:
            msg=f"Scanned {len(funding_list)} | 0 with MIN +->{MIN_FUNDING*100}% MAX INF -> MISS LOW FUNDING"
            print(msg, flush=True)
            return [], msg
        
        ops_4h=[o for o in ops if o['interval']==4]
        ops_8h=[o for o in ops if o['interval']==8]
        
        if not ops_4h or not ops_8h:
            min_secs=min(o['secs_to'] for o in ops)
            settling=[o for o in ops if o['secs_to'] <= min_secs+600]
            settling.sort(key=lambda x: x['abs_rate'], reverse=True)
            rule_msg=f"Next in {min_secs/60:.0f}min | {len(ops)} coins MIN 0.5% MAX INF"
        else:
            min_4h=min(o['secs_to'] for o in ops_4h)
            min_8h=min(o['secs_to'] for o in ops_8h)
            diff=abs(min_4h-min_8h)
            if diff <= 600:
                rule_msg=f"RULE2 SAME TIME 4H {min_4h/60:.0f}m 8H {min_8h/60:.0f}m -> HIGHEST ALL"
                min_secs=min(min_4h,min_8h)
                settling=[o for o in ops if o['secs_to'] <= min_secs+600]
                settling.sort(key=lambda x: x['abs_rate'], reverse=True)
            else:
                if min_4h < min_8h:
                    rule_msg=f"RULE1 4H sooner {min_4h/60:.0f}m vs 8H {min_8h/60:.0f}m -> Pick 4H HIGHEST"
                    settling=[o for o in ops_4h if o['secs_to'] <= min_4h+600]
                    settling.sort(key=lambda x: x['abs_rate'], reverse=True)
                else:
                    rule_msg=f"8H sooner {min_8h/60:.0f}m vs 4H {min_4h/60:.0f}m -> Pick 8H HIGHEST"
                    settling=[o for o in ops_8h if o['secs_to'] <= min_8h+600]
                    settling.sort(key=lambda x: x['abs_rate'], reverse=True)
        
        wallet=get_wallet()
        print(f"{rule_msg} | Scanned {len(funding_list)} | {len(ops)} coins MIN 0.5% MAX INF | Wallet ${wallet:.2f} Pos ${wallet*INVEST_PCT*LEVERAGE:.2f}", flush=True)
        
        top5 = ", ".join([f"{x['symbol']} {x['rate']*100:+.2f}%" for x in settling[:5]])
        print(f"Top 5 funding: {top5}", flush=True)
        
        good=[]
        print(f"Checking order book (fast, 10 coins max)...", flush=True)
        for o in settling[:10]:
            can,slip,max_fill,net=check_orderbook_fast(o['symbol'], wallet, o['side'], o['rate'])
            o['slippage']=slip; o['max_fill']=max_fill; o['net_profit']=net; o['can_fill']=can
            if can:
                print(f"  GOOD {o['symbol']} {o['rate']*100:+.3f}% {o['interval']}H {o['side']} Book ${max_fill:.0f} Slip {slip:.3f}% Net ${net:.3f}", flush=True)
                good.append(o)
            else:
                print(f"  SKIP {o['symbol']} {o['rate']*100:+.3f}% Book ${max_fill:.0f} Slip {slip:.2f}%", flush=True)
        
        if good:
            good.sort(key=lambda x: x['net_profit'], reverse=True)
            best=good[0]
            print(f"FINAL PICK: {best['symbol']} {best['rate']*100:+.3f}% {best['interval']}H {best['side']} Net ${best['net_profit']:.3f}", flush=True)
            return good, rule_msg
        else:
            msg=f"No good book in top 10 | Wallet ${wallet:.2f} -> MISS THIN BOOK"
            print(msg, flush=True)
            return [], msg

    except Exception as e:
        print(f"Error: {e}", flush=True)
        import traceback; traceback.print_exc()
        return [], f"Error {e}"

def trade_loop():
    ws=get_wallet()
    print(f"START ${ws:.4f} | MIN +->{MIN_FUNDING*100}% MAX INF | 4H/8H LONG/SHORT HIGHEST | {LEVERAGE}X 99% SL {SL_PCT}%", flush=True)
    while True:
        now=datetime.now(timezone.utc)
        good_coins, rule_msg = get_all_funding_smart()
        wallet=get_wallet()
        
        if not good_coins:
            time.sleep(10)
            continue
        
        next_dt=good_coins[0]['next_dt']
        secs_to=(next_dt-now).total_seconds()
        
        if secs_to < 180:
            print(f"[{now.strftime('%H:%M:%S')} UTC] {secs_to:.0f}s to funding | {good_coins[0]['symbol']} {good_coins[0]['rate']*100:+.3f}% {good_coins[0]['interval']}H", flush=True)

        if 0.02 < secs_to/60 <= (ENTRY_SEC/60) and LIVE:
            best=good_coins[0]; symbol=best['symbol']; rate=best['rate']; side=best['side']
            open_f=exchange.create_market_sell_order if side=="SHORT" else exchange.create_market_buy_order
            close_f=exchange.create_market_buy_order if side=="SHORT" else exchange.create_market_sell_order
            print(f"\n=== ENTRY {secs_to:.0f}s | {symbol} {rate*100:+.4f}% {best['interval']}H {side} ===", flush=True)
            try:
                entry=exchange.fetch_ticker(symbol)['last']
                invest=wallet*INVEST_PCT
                max_fill=best.get('max_fill', float('inf'))
                want=invest*LEVERAGE
                actual=min(want, max_fill*0.9) if max_fill>0 else want
                amt=float(exchange.amount_to_precision(symbol, actual/entry))
                sl=entry*(1+SL_PCT/100) if side=="SHORT" else entry*(1-SL_PCT/100)
                print(f"Wallet ${wallet:.4f} | Pos ${actual:.2f} | {side} {amt} @ {entry} | SL {sl:.6f}", flush=True)
                open_f(symbol, amt)
                print(f"OPENED {side}", flush=True)
                exit_time=next_dt+timedelta(seconds=EXIT_SEC)
                closed=False
                while datetime.now(timezone.utc) < exit_time:
                    try:
                        cur=exchange.fetch_ticker(symbol)['last']
                        if side=="SHORT" and cur>=sl:
                            print(f"!!! SL HIT -> CLOSE", flush=True)
                            try: close_f(symbol, amt)
                            except: pass
                            closed=True; break
                        if side=="LONG" and cur<=sl:
                            print(f"!!! SL HIT -> CLOSE", flush=True)
                            try: close_f(symbol, amt)
                            except: pass
                            closed=True; break
                    except: pass
                    time.sleep(0.3)
                if not closed:
                    print(f"CLOSE +{EXIT_SEC}s GUARANTEED", flush=True)
                    try: close_f(symbol, amt)
                    except:
                        time.sleep(1)
                        try: close_f(symbol, amt)
                        except: pass
                nw=get_wallet()
                print(f"RESULT ${nw-wallet:+.4f} | New ${nw:.4f}", flush=True)
            except Exception as e:
                print(f"Trade error: {e}", flush=True)
            time.sleep(90)
            continue
        
        time.sleep(3 if secs_to<120 else 10)

if __name__=="__main__":
    trade_loop()
