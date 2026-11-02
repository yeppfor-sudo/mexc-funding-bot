import os, time, html, requests, ccxt
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# ===== SECRETS (GitHub Actions) =====
API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("LIVE", "0") != "1"   # safe by default: LIVE=1 to trade for real

# ===== CONFIG =====
LEVERAGE = 5
WALLET_PCT = 0.99
SLOT_HOURS = [0, 4, 8, 12, 16, 20]   # UTC settlement times of 4h pairs
CYCLE_HOURS = 4
CHECK_BEFORE = 120      # scan/validate 2 min before funding
ENTRY_BEFORE = 45       # enter 45 sec before funding
MAX_WAIT = 55 * 60      # exit if next slot is further away than this
MIN_RATE = 0.005        # min |funding| 0.5%
FEE_SIDE = 0.0004       # taker fee per side (conservative, check your tier)
MIN_EDGE = 0.002        # min net edge on notional after fees+slippage+spread
MAX_SPREAD = 0.001
SL_PRICE = 0.03         # -3% price = -15% margin at 5x
NEAR_AVG = 0.0008       # right after funding: close at entry, profit, or up to -0.08% "near avg"
FALLBACK_SEC = 30       # if still not near avg after this, accept any net-positive exit
MIN_LOCK = 0.0005       # net result (incl. funding, fees) must be >= +0.05% for fallback exit
MAX_HOLD_AFTER = 300    # force close this long after funding
BASE = "https://contract.mexc.com/api/v1/contract"

exchange = ccxt.mexc({
    "apiKey": API_KEY, "secret": SECRET,
    "options": {"defaultType": "swap"}, "enableRateLimit": True,
})


def tg(msg):
    print(msg)
    try:
        tag = "[DRY] " if DRY_RUN else ""
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": tag + html.escape(msg)}, timeout=8)
    except Exception as e:
        print("TG error", e)


def get(url):
    return requests.get(url, timeout=10).json()


def balance():
    b = exchange.fetch_balance()
    return float((b.get("USDT") or {}).get("free") or 0)


def next_slot():
    now = datetime.now(timezone.utc)
    for d in (0, 1):
        for h in SLOT_HOURS:
            t = (now + timedelta(days=d)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t.timestamp() - time.time() > ENTRY_BEFORE + 10:
                return t.timestamp()


def sleep_until(ts):
    while True:
        d = ts - time.time()
        if d <= 0:
            return
        time.sleep(min(d, 20) if d > 2 else 0.02)


def funding_info(raw):
    try:
        d = get(f"{BASE}/funding_rate/{raw}").get("data") or {}
        return float(d["fundingRate"]), int(d["collectCycle"]), int(d["nextSettleTime"]) / 1000
    except Exception:
        return None


def scan(slot):
    """All USDT perps with |rate|>=MIN_RATE, verified: 4h cycle AND settling at this slot."""
    rows = get(f"{BASE}/ticker").get("data") or []
    cand = [(d["symbol"], float(d.get("fundingRate") or 0)) for d in rows
            if str(d.get("symbol", "")).endswith("_USDT")]
    cand = sorted([c for c in cand if abs(c[1]) >= MIN_RATE], key=lambda c: -abs(c[1]))[:25]
    with ThreadPoolExecutor(8) as ex:
        infos = list(ex.map(lambda c: funding_info(c[0]), cand))
    out = []
    for (raw, _), inf in zip(cand, infos):
        if not inf:
            continue
        rate, cycle, nxt = inf
        if cycle == CYCLE_HOURS and abs(nxt - slot) < 90 and abs(rate) >= MIN_RATE:
            out.append((raw, rate))
    return out


def rank(valid, notional):
    """Net edge = |rate| - round-trip fees - round-trip slippage - spread. Skip unaffordable/illiquid."""
    res = []
    for raw, rate in valid:
        sym = raw.replace("_", "/") + ":USDT"
        try:
            m = exchange.market(sym)
            cs = float(m.get("contractSize") or 1)
            side = "sell" if rate > 0 else "buy"          # +rate: shorts get paid
            ob = exchange.fetch_order_book(sym, 20)
            levels = ob["asks"] if side == "buy" else ob["bids"]
            best = levels[0][0]
            spread = (ob["asks"][0][0] - ob["bids"][0][0]) / best
            amt = float(exchange.amount_to_precision(sym, notional / (best * cs)))
            min_amt = float((m.get("limits", {}).get("amount") or {}).get("min") or 0)
            if amt <= 0 or amt < min_amt:
                continue                                    # wallet too small for this pair
            need, cost = amt, 0.0
            for p, q in levels:
                take = min(q, need)
                cost += take * p
                need -= take
                if need <= 0:
                    break
            if need > 0:
                continue                                    # book too thin
            slip = abs(cost / amt - best) / best
            edge = abs(rate) - 2 * FEE_SIDE - 2 * slip - spread
            if spread > MAX_SPREAD or edge < MIN_EDGE:
                continue
            res.append(dict(raw=raw, sym=sym, rate=rate, side=side, amt=amt, px=best,
                            slip=slip, spread=spread, edge=edge))
        except Exception as e:
            print("rank", raw, e)
    return sorted(res, key=lambda c: -c["edge"])


def setup(c):
    pt = 2 if c["side"] == "sell" else 1
    try:
        exchange.set_leverage(LEVERAGE, c["sym"], params={"openType": 1, "positionType": pt})
        return True
    except Exception as e:
        print("leverage fail", c["raw"], e)
        return False


def order(sym, side, amt, reduce, px):
    if DRY_RUN:
        return
    p = {"openType": 1}
    if reduce:
        p["reduceOnly"] = True
    exchange.create_order(sym, "market", side, amt, px, p)


def get_pos(sym):
    for p in exchange.fetch_positions([sym]):
        if float(p.get("contracts") or 0) > 0:
            return p
    return None


def close(sym, side, amt, px):
    closing = "buy" if side == "sell" else "sell"
    for _ in range(3):
        try:
            order(sym, closing, amt, True, px)
        except Exception as e:
            print("close err", e)
        time.sleep(0.4)
        try:
            if DRY_RUN or not get_pos(sym):
                return True
        except Exception:
            pass
    tg(f"🚨 CLOSE FAILED {sym} - close manually NOW")
    return False


def hold(c, entry, amt, slot, wallet):
    sym, side, rate = c["sym"], c["side"], c["rate"]
    floor = -(abs(rate) - 2 * FEE_SIDE - MIN_LOCK)   # worst price move still net-positive
    while True:
        try:
            t = exchange.fetch_ticker(sym)
            px = (t.get("bid") if side == "buy" else t.get("ask")) or t["last"]
        except Exception:
            time.sleep(0.5)
            continue
        chg = (px - entry) / entry if side == "buy" else (entry - px) / entry
        now, reason = time.time(), None
        if chg <= -SL_PRICE:
            reason = "STOP -3% price (-15% margin)"
        elif now >= slot + 2:                          # funding booked -> exit ASAP
            since = now - slot
            if chg >= 0:
                reason = "AVG/PROFIT"
            elif chg >= -NEAR_AVG:
                reason = "NEAR AVG"
            elif since > FALLBACK_SEC and chg >= floor:
                reason = "NET-POSITIVE (funding covers price move)"
            elif since > MAX_HOLD_AFTER:
                reason = "TIMEOUT"
        if reason:
            ok = close(sym, side, amt, px)
            time.sleep(2)
            nb = balance() if not DRY_RUN else wallet
            tg(f"{'✅' if ok else '⚠️'} CLOSED {sym} [{reason}]\nprice {entry} -> {px} ({chg*100:.3f}%)"
               f"\nfunding {rate*100:.3f}%\nwallet {wallet:.4f} -> {nb:.4f}")
            return
        time.sleep(0.5)


def main():
    exchange.load_markets()
    slot = next_slot()
    if slot - time.time() > MAX_WAIT:
        tg("Next slot too far away, exiting.")
        return
    sleep_until(slot - CHECK_BEFORE)

    wallet = balance()
    tg(f"Run for slot {datetime.fromtimestamp(slot, timezone.utc):%H:%M} UTC | wallet ${wallet:.4f}")
    if wallet < 0.5:
        tg("Balance too low (transfer USDT to Futures).")
        return
    notional = wallet * WALLET_PCT * LEVERAGE

    ranked = rank(scan(slot), notional)
    ready = []
    for c in ranked[:5]:
        if setup(c):
            ready.append(c)
        if len(ready) == 3:
            break
    if not ready:
        tg("No pair passes filters (rate, 4h cycle, affordable, liquid, net edge). Skipping slot.")
        return
    tg("Candidates:\n" + "\n".join(
        f"{c['raw']} {c['rate']*100:.3f}% {c['side']} net edge {c['edge']*100:.2f}%" for c in ready))

    # final re-validation at T-50s: rate may have moved
    sleep_until(slot - 50)
    best = None
    for c in ready:
        inf = funding_info(c["raw"])
        if not inf:
            continue
        rate, cycle, nxt = inf
        if cycle != CYCLE_HOURS or abs(nxt - slot) >= 90 or (rate > 0) != (c["rate"] > 0):
            continue
        edge = abs(rate) - 2 * FEE_SIDE - 2 * c["slip"] - c["spread"]
        if abs(rate) >= MIN_RATE and edge >= MIN_EDGE and (best is None or edge > best["edge"]):
            best = dict(c, rate=rate, edge=edge)
    if not best:
        tg("ABORT: no candidate still valid at T-50s.")
        return

    sleep_until(slot - ENTRY_BEFORE)
    sym, side, amt = best["sym"], best["side"], best["amt"]
    tg(f"🚀 ENTER {best['raw']} {'SHORT' if side=='sell' else 'LONG'} funding {best['rate']*100:.3f}%")
    try:
        try:
            order(sym, side, amt, False, best["px"])
        except Exception as e:
            print("entry retry smaller", e)
            amt = float(exchange.amount_to_precision(sym, amt * 0.95))
            order(sym, side, amt, False, best["px"])
    except Exception as e:
        tg(f"Entry failed: {e}")
        return

    entry = best["px"]
    if not DRY_RUN:
        time.sleep(0.3)
        pos = get_pos(sym)
        if not pos:
            tg("Entry not found in positions, aborting.")
            return
        entry = float(pos["entryPrice"])
        amt = float(pos["contracts"])
        if abs(float(pos.get("leverage") or LEVERAGE) - LEVERAGE) > 0.01:
            close(sym, side, amt, entry)
            tg(f"Leverage mismatch ({pos.get('leverage')}x), closed immediately.")
            return
        try:   # exchange-side stop as backup to polling
            sl = entry * (1 + SL_PRICE) if side == "sell" else entry * (1 - SL_PRICE)
            exchange.create_order(sym, "market", "buy" if side == "sell" else "sell", amt, None,
                                  {"stopLossPrice": sl, "reduceOnly": True, "openType": 1})
        except Exception as e:
            print("exchange stop unavailable, polling SL only:", e)
    tg(f"Entered @ {entry} amt {amt} | SL {SL_PRICE*100:.0f}% price")

    hold(best, entry, amt, slot, wallet)
    try:
        exchange.cancel_all_orders(sym)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"Fatal error: {e}")
        raise
