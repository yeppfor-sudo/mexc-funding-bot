import ccxt
import time
import asyncio
import os
import requests
from datetime import datetime, timedelta

# ===== CONFIG FROM GITHUB SECRETS =====
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEVERAGE = 5
WALLET_PERCENT = 0.99
FUNDING_ENTRY_SEC = 45  # Enter 45 sec before funding
CHECK_BEFORE_SEC = 120  # Check 2 min before
STOP_LOSS_MARGIN = 0.15  # -15% with 5x = -3% spot price
MIN_NET_PROFIT = 0.05  # Must make at least $0.05 after fees

exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_SECRET,
    'options': {'defaultType': 'swap'},
    'enableRateLimit': True
})

def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except: pass
    print(msg)

def get_balance():
    try:
        bal = exchange.fetch_balance()
        free = 0
        if 'USDT' in bal and bal['USDT'].get('free') is not None:
            free = float(bal['USDT']['free'])
        elif 'free' in bal and 'USDT' in bal['free']:
            free = float(bal['free']['USDT'])
        else:
            # Try futures wallet
            bal2 = exchange.fetch_balance({'type': 'swap'})
            free = float(bal2.get('USDT', {}).get('free', 0) or bal2.get('free', {}).get('USDT', 0) or 0)
        return free
    except Exception as e:
        print(f"Balance error: {e}")
        return 0

def get_top_funding():
    rates = exchange.fetch_funding_rates()
    lst = []
    for sym, data in rates.items():
        if 'USDT:USDT' in sym:
            rate = float(data['fundingRate'])
            lst.append({'symbol': sym, 'rate': rate, 'abs': abs(rate), 'next_ts': data.get('fundingTimestamp')})
    lst.sort(key=lambda x: x['abs'], reverse=True)
    return lst

async def wait_for_window():
    while True:
        now = datetime.utcnow()
        hours = [0, 8, 16] # MEXC funding times
        next_f = None
        for h in hours:
            cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if cand > now:
                next_f = cand
                break
        if not next_f:
            next_f = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        diff = (next_f - now).total_seconds()
        print(f"Next funding {next_f} UTC in {diff/60:.1f} min")
        
        if diff <= CHECK_BEFORE_SEC:
            return next_f
        await asyncio.sleep(min(diff - CHECK_BEFORE_SEC, 60))

async def main():
    wallet = get_balance()
    telegram(f"🤖 MEXC FUNDING SNIPER STARTED\n💰 Wallet: ${wallet:.4f}\n⚙️ 99% x {LEVERAGE}x | Entry 45s before funding | SL -15% margin | Compound 100%\nNext scan at funding time")

    while True:
        next_funding_time = await wait_for_window()
        telegram(f"⏰ 2 min to funding {next_funding_time} UTC - Scanning...")

        wallet = get_balance()
        if wallet < 0.5:
            telegram(f"❌ Balance ${wallet:.4f} too low, waiting next round")
            await asyncio.sleep(60)
            continue

        position_usdt = wallet * WALLET_PERCENT * LEVERAGE
        top = get_top_funding()
        
        selected = None
        for pair in top[:30]:
            sym = pair['symbol']
            rate = pair['rate']
            if abs(rate) < 0.002: # Skip <0.2%
                continue
            try:
                ticker = exchange.fetch_ticker(sym)
                price = float(ticker['last'])
                market = exchange.market(sym)
                min_cost = float(market['limits']['cost']['min']) if market['limits'].get('cost') else 5.0
                
                # Fee calc: Taker 0.06%*2 = 0.12%
                gross = position_usdt * abs(rate)
                fee = position_usdt * 0.0012
                net = gross - fee
                
                if net < MIN_NET_PROFIT:
                    continue
                if position_usdt < min_cost:
                    print(f"Skip {sym} need ${min_cost} have ${position_usdt:.2f}")
                    continue
                
                amount = position_usdt / price
                min_amount = float(market['limits']['amount']['min']) if market['limits'].get('amount') else 0
                if amount < min_amount:
                    continue
                
                selected = {'symbol': sym, 'rate': rate, 'price': price, 'amount': amount, 'net': net, 'gross': gross, 'fee': fee}
                break
            except Exception as e:
                print(f"Check {sym} error {e}")
                continue

        if not selected:
            telegram(f"⚠️ No affordable pair for ${wallet:.2f}. Waiting next funding.")
            await asyncio.sleep(60)
            continue

        # Wait until 45 sec before
        while True:
            now = datetime.utcnow()
            diff = (next_funding_time - now).total_seconds()
            if diff <= FUNDING_ENTRY_SEC:
                break
            await asyncio.sleep(0.2)

        # ENTER
        sym = selected['symbol']
        rate = selected['rate']
        side = "LONG" if rate < 0 else "SHORT"
        try:
            exchange.set_leverage(LEVERAGE, sym)
            exchange.set_margin_mode('ISOLATED', sym)
            amt = selected['amount']
            
            telegram(f"🚀 SNIPING {sym}\nRate: {rate*100:.4f}% | {side}\nBalance: ${wallet:.4f} -> Pos ${position_usdt:.2f}\nGross: ${selected['gross']:.4f} Fee: ${selected['fee']:.4f} Net: ${selected['net']:.4f}")
            
            if side == "LONG":
                order = exchange.create_market_buy_order(sym, amt)
            else:
                order = exchange.create_market_sell_order(sym, amt)
            
            entry = float(order.get('average') or selected['price'])
            telegram(f"✅ Entered {side} {sym} at {entry}")
            
            # Wait for funding (70 sec after funding time)
            await asyncio.sleep(75)
            
            # CLOSE LOGIC: Try avg/profit price for 5 min, else SL -15%
            start = time.time()
            closed = False
            while time.time() - start < 300:
                ticker = exchange.fetch_ticker(sym)
                curr = float(ticker['last'])
                if side == "LONG":
                    change = (curr - entry) / entry
                else:
                    change = (entry - curr) / entry
                margin_pnl = change * LEVERAGE

                if change >= 0: # Avg or profit
                    if side == "LONG":
                        exchange.create_market_sell_order(sym, amt)
                    else:
                        exchange.create_market_buy_order(sym, amt)
                    new_bal = get_balance()
                    telegram(f"✅ CLOSED at PROFIT {sym}\nEntry {entry} -> Exit {curr}\nChange {change*100:.2f}% Margin {margin_pnl*100:.2f}%\nWallet ${wallet:.4f} -> ${new_bal:.4f} (+${new_bal-wallet:.4f})\nCOMPOUNDING!")
                    closed = True
                    break
                
                if margin_pnl <= -STOP_LOSS_MARGIN:
                    if side == "LONG":
                        exchange.create_market_sell_order(sym, amt)
                    else:
                        exchange.create_market_buy_order(sym, amt)
                    new_bal = get_balance()
                    telegram(f"🛑 SL -15% {sym} at {curr} Wallet ${new_bal:.4f}")
                    closed = True
                    break
                
                await asyncio.sleep(1)
            
            if not closed:
                # Force close
                if side == "LONG":
                    exchange.create_market_sell_order(sym, amt)
                else:
                    exchange.create_market_buy_order(sym, amt)
                telegram(f"⚠️ Force closed {sym} after 5 min")

        except Exception as e:
            telegram(f"❌ Trade failed {sym}: {e}")

        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
