import ccxt, time, os, sys, requests
from datetime import datetime, timezone, timedelta

print("=== v22 FINAL - ENTRY 15s EXIT 3s - MIN 0.5% MAX INF ===", flush=True)

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
LIVE = os.getenv("LIVE", "false").lower() == "true"

LEVERAGE=5
INVEST_PCT=0.99
SL_PCT=3.0
ENTRY_SEC=15
EXIT_SEC=3
MIN_FUNDING=0.005
MAX_SLIPPAGE_PCT=0.5

if not API_KEY:
    print("ERROR: MEXC_API_KEY missing", flush=True)
    sys.exit(1)

exchange = ccxt.mexc({
    'apiKey': API_KEY,
    'secret': SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': 10000
})

def get_wallet():
    try:
        b = exchange.fetch_balance()
        u = b.get('USDT', {}).get('free', 0) or 0
        if u == 0:
            u = b.get('total', {}).get('USDT', 0) or 0
        return float(u)
    except:
        return 0

def check_book_fast(sym, wallet, side, rate):
    try:
        want = wallet * INVEST_PCT * LEVERAGE
        if want < 5:
            want = 5
        ob = exchange.fetch_order_book(sym, limit=20)
        bids = ob.get('bids', [])
        asks = ob.get('asks', [])
        if not bids or not asks:
            return False, 99, 0, -999
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        book = bids if side == "SHORT" else asks
        f_usdt = 0
        f_amt = 0
        w = 0
        for e in book:
            try:
                p = float(e[0]); a = float(e[1])
            except:
                continue
            lvl = p * a
            if f_usdt + lvl >= want:
                need = want - f_usdt
                need_a = need / p if p else 0
                f_amt += need_a
                w += need_a * p
                f_usdt = want
                break
            else:
                f_amt += a
                w += a * p
                f_usdt += lvl
        if f_amt == 0:
            return False, 99, 0, -999
        if f_usdt < want * 0.9:
            slip = 99
            maxf = f_usdt
        else:
            avg = w / f_amt if f_amt else mid
            slip = abs(avg - mid) / mid * 100 if mid else 99
            maxf = f_usdt
        funding = want * abs(rate)
        net = funding - want * (slip / 100) - want * 0.0004
        can = slip <= MAX_SLIPPAGE_PCT and f_usdt >= want * 0.9 and net > 0
        return can, slip, maxf, net
    except:
        return False, 99, 0, -999

def get_funding():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Fetching...", flush=True)
    try:
        r1 = requests.get("https://contract.mexc.com/api/v1/contract/funding_rate", timeout=10).json()
        fl = r1.get('data', [])
        r2 = requests.get("https://contract.mexc.com/api/v1/contract/detail", timeout=10).json()
        det = {i['symbol']: i for i in r2.get('data', [])}
        ops = []
        now = datetime.now(timezone.utc)
        for f in fl:
            s = f.get('symbol', '')
            if not s.endswith('_USDT'):
                continue
            rate = float(f.get('fundingRate', 0))
            if abs(rate) < MIN_FUNDING:
                continue
            d = det.get(s, {})
            nxt = f.get('nextFundingTime') or d.get('nextFundingTime') or 0
            interval = d.get('fundingInterval') or 8
            try:
                interval = int(interval)
                if interval not in [4, 8]:
                    interval = 8
            except:
                interval = 8
            if nxt:
                next_dt = datetime.fromtimestamp(nxt / 1000, tz=timezone.utc)
            else:
                next_dt = now + timedelta(hours=8)
            secs = (next_dt - now).total_seconds()
            base = s.replace('_USDT', '')
            ccxt_sym = f"{base}/USDT:USDT"
            side = "SHORT" if rate > 0 else "LONG"
            ops.append({'symbol': ccxt_sym, 'rate': rate, 'abs_rate': abs(rate), 'interval': interval, 'next_dt': next_dt, 'secs_to': secs, 'side': side})
        if not ops:
            return [], "0 coins"
        min_s = min(o['secs_to'] for o in ops)
        settling = [o for o in ops if o['secs_to'] <= min_s + 600]
        settling.sort(key=lambda x: x['abs_rate'], reverse=True)
        rule = f"Next {min_s/60:.0f}min | {len(ops)} coins MIN 0.5% MAX INF"
        wallet = get_wallet()
        print(f"{rule} | Wallet ${wallet:.2f} Pos ${wallet*INVEST_PCT*LEVERAGE:.2f}", flush=True)
        top5 = ", ".join([f"{x['symbol']} {x['rate']*100:+.2f}%" for x in settling[:5]])
        print(f"Top 5: {top5}", flush=True)
        good = []
        for o in settling[:10]:
            can, slip, maxf, net = check_book_fast(o['symbol'], wallet, o['side'], o['rate'])
            o['slippage'] = slip
            o['max_fill'] = maxf
            o['net_profit'] = net
            o['can_fill'] = can
            if can:
                print(f"  GOOD {o['symbol']} {o['rate']*100:+.3f}% {o['interval']}H {o['side']} Net ${net:.3f}", flush=True)
                good.append(o)
            else:
                print(f"  SKIP {o['symbol']} {o['rate']*100:+.3f}% Book ${maxf:.0f} Slip {slip:.2f}%", flush=True)
        if good:
            good.sort(key=lambda x: x['net_profit'], reverse=True)
            print(f"FINAL PICK: {good[0]['symbol']} {good[0]['rate']*100:+.3f}% {good[0]['interval']}H {good[0]['side']} Net ${good[0]['net_profit']:.3f} Next {good[0]['secs_to']/60:.0f}min", flush=True)
            return good, rule
        else:
            return [], "No good book"
    except Exception as e:
        print(f"Error {e}", flush=True)
        return [], f"Error {e}"

def trade_loop():
    ws = get_wallet()
    print(f"START ${ws:.4f} | MIN 0.5% MAX INF | ENTRY {ENTRY_SEC}s EXIT {EXIT_SEC}s | LIVE={LIVE} | {LEVERAGE}X", flush=True)
    if not LIVE:
        print("!!! DRY RUN - LIVE=false !!!", flush=True)
    while True:
        now = datetime.now(timezone.utc)
        good, rule = get_funding()
        wallet = get_wallet()
        if not LIVE and int(time.time()) % 30 == 0:
            print(f"!!! DRY RUN LIVE=false !!! Wallet ${wallet:.2f}", flush=True)
        if not good:
            time.sleep(10)
            continue
        next_dt = good[0]['next_dt']
        secs_to = (next_dt - now).total_seconds()
        if secs_to < 120:
            print(f"[{now.strftime('%H:%M:%S')} UTC] {secs_to:.0f}s -> {good[0]['symbol']} {good[0]['rate']*100:+.3f}% LIVE={LIVE}", flush=True)
        if 0.5 < secs_to <= ENTRY_SEC and LIVE:
            best = good[0]
            symbol = best['symbol']
            rate = best['rate']
            side = best['side']
            open_f = exchange.create_market_sell_order if side == "SHORT" else exchange.create_market_buy_order
            close_f = exchange.create_market_buy_order if side == "SHORT" else exchange.create_market_sell_order
            print(f"\n=== ENTRY {secs_to:.0f}s | {symbol} {rate*100:+.4f}% {best['interval']}H {side} | LIVE={LIVE} ===", flush=True)
            try:
                entry = exchange.fetch_ticker(symbol)['last']
                invest = wallet * INVEST_PCT
                maxf = best.get('max_fill', float('inf'))
                want = invest * LEVERAGE
                actual = min(want, maxf * 0.9) if maxf > 0 else want
                amt = float(exchange.amount_to_precision(symbol, actual / entry))
                sl = entry * (1 + SL_PCT / 100) if side == "SHORT" else entry * (1 - SL_PCT / 100)
                print(f"Wallet ${wallet:.4f} Pos ${actual:.2f} {side} {amt} @ {entry} SL {sl:.6f}", flush=True)
                open_f(symbol, amt)
                print(f"OPENED {side}", flush=True)
                exit_time = next_dt + timedelta(seconds=EXIT_SEC)
                closed = False
                while datetime.now(timezone.utc) < exit_time:
                    try:
                        cur = exchange.fetch_ticker(symbol)['last']
                        if side == "SHORT" and cur >= sl:
                            print("!!! SL HIT", flush=True)
                            close_f(symbol, amt)
                            closed = True
                            break
                        if side == "LONG" and cur <= sl:
                            print("!!! SL HIT", flush=True)
                            close_f(symbol, amt)
                            closed = True
                            break
                    except:
                        pass
                    time.sleep(0.2)
                if not closed:
                    print(f"CLOSE +{EXIT_SEC}s GUARANTEED", flush=True)
                try:
                    close_f(symbol, amt)
                except:
                    time.sleep(0.5)
                    close_f(symbol, amt)
                nw = get_wallet()
                print(f"RESULT ${nw-wallet:+.4f} New ${nw:.4f}", flush=True)
            except Exception as e:
                print(f"Trade error {e}", flush=True)
            time.sleep(90)
            continue
        time.sleep(1 if secs_to < 120 else 6)

if __name__ == "__main__":
    trade_loop()
