import os, time, html, threading, subprocess, requests, ccxt
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
MIN_RATE = 0.005        # min |funding| 0.5%
FEE_SIDE = 0.0004       # taker fee per side
MIN_EDGE = 0.002        # min net edge on notional after fees+slippage+spread
MAX_SPREAD = 0.001
SL_PRICE = 0.03         # -3% price = -15% margin at 5x
NEAR_AVG = 0.0008       # right after funding: close at entry, profit, or up to -0.08%
CLOSE_START = 1.5       # start trying to exit this many sec after funding time
FORCE_CLOSE_SEC = 3     # HARD force close this many sec after funding time
MAX_RUN_SEC = 5.5 * 3600  # hand off to a fresh run before GitHub's 6h job limit
BASE = "https://contract.mexc.com/api/v1/contract"

START = time.time()
S = requests.Session()                 # keep-alive connection for fast public calls
OFFSET = [0.0]                         # exchange clock minus local clock
_threads = []

exchange = ccxt.mexc({
    "apiKey": API_KEY, "secret": SECRET,
    "options": {"defaultType": "swap"}, "enableRateLimit": True,
})


def now():
    return time.time() + OFFSET[0]


def sync_clock():
    """Align to MEXC server time so entry/exit timing is exact."""
    best = None
    for _ in range(3):
        try:
            t0 = time.time()
            j = S.get(f"{BASE}/ping", timeout=5).json()
            t1 = time.time()
            off = float(j["data"]) / 1000 - (t0 + t1) / 2
            if best is None or t1 - t0 < best[0]:
                best = (t1 - t0, off)
        except Exception as e:
            print("clock sync error", e)
    if best:
        OFFSET[0] = best[1]
        print(f"clock offset {best[1]*1000:.0f} ms (rtt {best[0]*1000:.0f} ms)")


def tg(msg):
    """Non-blocking Telegram so messages never delay trading."""
    print(msg)
    tag = "[DRY] " if DRY_RUN else ""

    def send():
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": tag + html.escape(msg)}, timeout=8)
        except Exception as e:
            print("TG error", e)
    t = threading.Thread(target=send, daemon=True)
    t.start()
    _threads.append(t)


def flush_tg():
    for t in _threads:
        t.join(timeout=5)


def get(url):
    return S.get(url, timeout=6).json()


def balance():
    b = exchange.fetch_balance()
    return float((b.get("USDT") or {}).get("free") or 0)


def next_slot(after=0):
    n = now()
    for d in (0, 1, 2):
        for h in SLOT_HOURS:
            t = (datetime.fromtimestamp(n, timezone.utc) + timedelta(days=d)).replace(
                hour=h, minute=0, second=0, microsecond=0).timestamp()
            if t > after and t - n > ENTRY_BEFORE + 10:
                return t


def sleep_until(ts):
    while True:
        d = ts - now()
        if d <= 0:
            return
        time.sleep(min(d, 20) if d > 1 else 0.01)


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
        if DRY_RUN:
            return True
        time.sleep(0.25)
        try:
            if not get_pos(sym):
                return True
        except Exception:
            pass
    tg(f"🚨 CLOSE FAILED {sym} - close manually NOW")
    return False


def quote(c):
    """Fast price from MEXC public ticker (keep-alive session). Returns exit-side price."""
    try:
        d = S.get(f"{BASE}/ticker", params={"symbol": c["raw"]}, timeout=2).json()["data"]
        if isinstance(d, list):
            d = d[0]
        return float(d["bid1"] if c["side"] == "buy" else d["ask1"])
    except Exception:
        t = exchange.fetch_ticker(c["sym"])
        return float((t.get("bid") if c["side"] == "buy" else t.get("ask")) or t["last"])


def hold(c, entry, amt, slot, wallet):
    sym, side, rate = c["sym"], c["side"], c["rate"]
    while True:
        try:
            px = quote(c)
        except Exception:
            time.sleep(0.1)
            continue
        chg = (px - entry) / entry if side == "buy" else (entry - px) / entry
        t, reason = now(), None
        if chg <= -SL_PRICE:
            reason = "STOP -3% price (-15% margin)"
        elif t >= slot + CLOSE_START:                  # funding booked -> exit ASAP
            if chg >= 0:
                reason = "AVG/PROFIT"
            elif chg >= -NEAR_AVG:
                reason = "NEAR AVG"
            elif t >= slot + FORCE_CLOSE_SEC:
                reason = f"FORCE CLOSE ({FORCE_CLOSE_SEC}s after funding)"
        if reason:
            ok = close(sym, side, amt, px)
            took = now() - slot
            time.sleep(1.5)
            nb = balance() if not DRY_RUN else wallet
            tg(f"{'✅' if ok else '⚠️'} CLOSED {sym} [{reason}] at T+{took:.1f}s\n"
               f"price {entry} -> {px} ({chg*100:.3f}%)\nfunding {rate*100:.3f}%\n"
               f"wallet {wallet:.4f} -> {nb:.4f}")
            return
        time.sleep(0.1 if t > slot - 2 else 0.4)


def process_slot(slot):
    sleep_until(slot - CHECK_BEFORE - 15)
    sync_clock()
    wallet = balance()                                  # also warms the private connection
    sleep_until(slot - CHECK_BEFORE)
    tg(f"Slot {datetime.fromtimestamp(slot, timezone.utc):%H:%M} UTC | wallet ${wallet:.4f}")
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
    try:
        if not DRY_RUN:
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
    except Exception as e:
        tg(f"Error while holding: {e} - emergency close")
        try:
            p = get_pos(sym)
            if p:
                close(sym, side, float(p["contracts"]), entry)
        except Exception as e2:
            tg(f"🚨 emergency close failed: {e2} - CLOSE MANUALLY")
    finally:
        try:
            exchange.cancel_all_orders(sym)
        except Exception:
            pass


def handoff():
    """Start the next run before this one ends, so the bot never stops (GitHub jobs max out at 6h)."""
    cmd = ["gh", "workflow", "run", "funding-bot.yml"]
    if not DRY_RUN:
        cmd += ["-f", "live=1"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        tg("Handoff to next run: " + ("ok" if r.returncode == 0 else f"FAILED {r.stderr[:200]} (keeper will restart)"))
    except Exception as e:
        tg(f"Handoff error {e} (keeper will restart)")


def test_scan():
    slot = next_slot()
    wallet = balance()
    valid = scan(slot)
    ranked = rank(valid, wallet * WALLET_PCT * LEVERAGE)
    tg(f"TEST SCAN | wallet ${wallet:.4f} | next slot {datetime.fromtimestamp(slot, timezone.utc):%H:%M} UTC\n"
       f"{len(valid)} pairs pass rate+4h-cycle filters, {len(ranked)} also pass liquidity/edge\n" +
       "\n".join(f"{c['raw']} {c['rate']*100:.3f}% {c['side']} edge {c['edge']*100:.2f}%" for c in ranked[:5]))


def main():
    exchange.load_markets()
    sync_clock()
    if os.getenv("TEST_SCAN") == "1":                  # test mode: scan now, never trade
        test_scan()
        flush_tg()
        return
    tg(f"🤖 Bot run started | {'DRY RUN' if DRY_RUN else 'LIVE'} | 5x | 99% wallet")
    last = 0
    while True:
        slot = next_slot(last)
        if (time.time() - START) + (slot - now()) + 600 > MAX_RUN_SEC:
            handoff()
            break
        try:
            process_slot(slot)
        except Exception as e:
            tg(f"Slot error: {e}")
        last = slot
    flush_tg()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"Fatal error: {e}")
        flush_tg()
        raise
