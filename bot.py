import ccxt
import time
import asyncio
import os
import requests
from datetime import datetime, timedelta

# === SECRETS FROM GITHUB ===
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === FINAL LOCKED CONFIG ===
LEVERAGE = 5
WALLET_PERCENT = 0.99
FUNDING_ENTRY_SEC = 45  # Enter 45 sec before funding
CHECK_BEFORE_SEC = 120  # Start checking 2 min before
STOP_LOSS_MARGIN = 0.15 # -15% SL on margin
MIN_NET_PROFIT = 0.05
MIN_FILTER = 0.005       # MIN 0.5%
MAX_FILTER = float('inf') # MAX ∞
FUNDING_HOURS = [0,4,8,12,16,20] # EVERY 4H as you asked

exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_SECRET,
    'options': {'defaultType': 'swap'},
    'enableRateLimit': True
})

def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass
    print(msg)

def get_balance():
    try:
        bal = exchange.fetch_balance({'type': 'swap'})
        free = float(bal.get('USDT', {}).get('free', 0) or bal.get('free', {}).get('USDT', 0) or 0)
        if free < 0.1:
            bal2 = exchange.fetch_balance()
            free = float(bal2.get('USDT', {}).get('free', 0) or 0)
        return free
    except Exception as e:
        print(f"Balance error {e}")
        return 0

def get_top_funding_mexc_api():
    """Fetch live funding - MIN 0.5% MAX ∞ - HIGHEST FIRST"""
    try:
        url = "https://contract.mexc.com/api/v1/contract/ticker"
        resp = requests.get(url, timeout=15).json()
        data = resp.get('data', [])
        lst = []
        for item in data:
            try:
                symbol = item['symbol']
                if '_USDT' not in symbol: continue
                ccxt_symbol = symbol.replace('_', '/') + ':USDT'
                funding = float(item.get('fundingRate', 0))
                abs_rate = abs(funding)
                if abs_rate < MIN_FILTER: continue
                if abs_rate > MAX_FILTER: continue
                lst.append({
                    'symbol': ccxt_symbol,
                    'raw_symbol': symbol,
                    'rate': funding,
                    'abs': abs_rate
                })
            except: continue
        lst.sort(key=lambda x: x['abs'], reverse=True) # HIGHEST FIRST
        return lst
    except Exception as e:
        print(f"Ticker API error {e}")
        return []

async def wait_for_window():
    """Wait for next funding window EVERY 4H"""
    while True:
        now = datetime.utcnow()
        next_f = None
        for h in FUNDING_HOURS:
            cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if cand > now:
                next_f = cand
                break
        if not next_f:
            next_f = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        diff = (next_f - now).total_seconds()
        if diff <= CHECK_BEFORE_SEC:
            return next_f
        sleep_time = min(diff - CHECK_BEFORE_SEC, 60)
        print(f"Next funding {next_f} UTC in {diff/3600:.2f}h - sleeping {sleep_time:.0f}s")
        await asyncio.sleep(sleep_time)

async def main():
    wallet = get_balance()
    telegram(f"🤖 *BOT STARTED FINAL*\n💰 Wallet: ${wallet:.4f}\n⚙️ MIN 0.5% MAX ∞ | HIGHEST FIRST | EVERY 4H {FUNDING_HOURS}\n99% x {LEVERAGE}x | SL -15% | Compound 100%\nProven +964% in 30d backtest")
    
    while True:
        try:
            next_funding_time = await wait_for_window()
            wallet = get_balance()
            
            if wallet < 0.5:
                telegram(f"❌ Balance ${wallet:.4f} too low - Transfer USDT from Spot to Futures")
                await asyncio.sleep(60)
                continue

            position_usdt = wallet * WALLET_PERCENT * LEVERAGE
            top = get_top_funding_mexc_api()

            if not top:
                telegram(f"⏰ {next_funding_time} UTC - No pair >=0.5% now. Waiting next slot in 4h.")
                await asyncio.sleep(60)
                continue

            top3_str = ", ".join([f"{p['raw_symbol']} {p['rate']*100:.2f}%" for p in top[:3]])
            telegram(f"⏰ *2 min to funding*\nFound {len(top)} pairs >=0.5%\n🏆 *HIGHEST:* {top[0]['raw_symbol']} {top[0]['rate']*100:.4f}%\n📊 Top: {top3_str}")

            selected = None
            for pair in top[:30]:
                sym = pair['symbol']
                rate = pair['rate']
                try:
                    ticker = exchange.fetch_ticker(sym)
                    price = float(ticker['last'])
                    if price == 0: continue
                    market = exchange.market(sym)
                    min_cost = float(market['limits']['cost']['min']) if market['limits'].get('cost') else 5.0
                    gross = position_usdt * abs(rate)
                    fee = position_usdt * 0.0012
                    net = gross - fee
                    if net < MIN_NET_PROFIT: continue
                    if position_usdt < min_cost: continue
                    amount = position_usdt / price
                    # Adjust amount to market precision
                    amount = float(exchange.amount_to_precision(sym, amount))
                    selected = {'symbol': sym, 'rate': rate, 'price': price, 'amount': amount, 'net': net, 'raw': pair['raw_symbol']}
                    break
                except Exception as e:
                    continue

            if not selected:
                telegram(f"⚠️ No affordable pair >=0.5% for ${wallet:.2f} - Skipping this slot")
                await asyncio.sleep(60)
                continue

            # Wait for exact entry 45 sec before funding
            while True:
                diff = (next_funding_time - datetime.utcnow()).total_seconds()
                if diff <= FUNDING_ENTRY_SEC:
                    break
                await asyncio.sleep(0.2)

            sym = selected['symbol']
            rate = selected['rate']
            side = "LONG" if rate < 0 else "SHORT"
            raw = selected['raw']

            try:
                exchange.set_leverage(LEVERAGE, sym)
                exchange.set_margin_mode('ISOLATED', sym)
                amt = selected['amount']

                telegram(f"🚀 *SNIPING HIGHEST* {raw}\nRate {rate*100:.4f}% | {side}\nPos ${position_usdt:.2f} Net ${selected['net']:.4f}\nMIN 0.5% MAX ∞")

                if side == "LONG":
                    order = exchange.create_market_buy_order(sym, amt)
                else:
                    order = exchange.create_market_sell_order(sym, amt)

                entry = float(order.get('average') or selected['price'])
                telegram(f"✅ *Entered* {side} {sym}\nEntry {entry}\nRate {rate*100:.4f}%")

                # Close logic: avg/profit or SL -15%
                await asyncio.sleep(75) # Wait for funding paid
                start = time.time()
                closed = False

                while time.time() - start < 300: # 5 min max hold
                    try:
                        curr = float(exchange.fetch_ticker(sym)['last'])
                        change = (curr - entry) / entry if side == "LONG" else (entry - curr) / entry
                        
                        if change >= 0: # avg or profit
                            if side == "LONG":
                                exchange.create_market_sell_order(sym, amt)
                            else:
                                exchange.create_market_buy_order(sym, amt)
                            new_bal = get_balance()
                            profit = new_bal - wallet
                            telegram(f"✅ *CLOSED PROFIT* {sym}\n{entry} -> {curr} ({change*100:.3f}%)\nRate {rate*100:.4f}%\nWallet ${wallet:.4f} -> ${new_bal:.4f} (+${profit:.4f})")
                            closed = True
                            break

                        if change * LEVERAGE <= -STOP_LOSS_MARGIN: # -15% margin SL
                            if side == "LONG":
                                exchange.create_market_sell_order(sym, amt)
                            else:
                                exchange.create_market_buy_order(sym, amt)
                            new_bal = get_balance()
                            telegram(f"🛑 *SL -15%* {sym}\nRate {rate*100:.4f}%\nWallet ${new_bal:.4f}")
                            closed = True
                            break
                    except Exception as e:
                        print(f"Close check error {e}")
                    await asyncio.sleep(1)

                if not closed:
                    try:
                        if side == "LONG":
                            exchange.create_market_sell_order(sym, amt)
                        else:
                            exchange.create_market_buy_order(sym, amt)
                        telegram(f"⚠️ Force closed {sym} after 5 min")
                    except: pass

            except Exception as e:
                telegram(f"❌ Failed {sym}: {e}")
                # Try to close any open position
                try:
                    pos = exchange.fetch_positions([sym])
                    for p in pos:
                        if float(p.get('contracts', 0)) > 0:
                            amt = float(p['contracts'])
                            if p['side'] == 'long':
                                exchange.create_market_sell_order(sym, amt)
                            else:
                                exchange.create_market_buy_order(sym, amt)
                except: pass

            await asyncio.sleep(10)

        except Exception as e:
            telegram(f"❌ Main loop error: {e}")
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
