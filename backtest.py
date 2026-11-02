import os, time, csv, html, threading, requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

# Public MEXC data only - no API keys needed. Telegram is optional.
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

DAYS = int(os.getenv("DAYS", "30"))
START_BAL = float(os.getenv("START_BALANCE", "100"))

# ===== SAME RULES AS THE LIVE BOT =====
LEVERAGE = 5
WALLET_PCT = 0.99
CYCLE_HOURS = 4
MIN_RATE = 0.005
FEE_SIDE = 0.0004        # taker fee per side
SLIP_SIDE = 0.0005       # assumed slippage per side (no historical order book available)
SL_PRICE = 0.03          # -3% price = -15% margin
SL_SLIP = 0.003          # extra slippage when a stop is hit
LIQ_MOVE = 0.19          # adverse move that would liquidate 5x isolated
NEAR_AVG = 0.0008        # close at entry / profit / up to -0.08%
FALLBACK_SEC = 30        # then accept net-positive exit
MIN_LOCK = 0.0005
MAX_HOLD = 300           # seconds after funding
VOL_MULT = 10            # pair's last-5-min turnover must be >= 10x our notional
TOP_TRY = 5              # candidates tried per slot

BASE = "https://contract.mexc.com/api/v1/contract"
_lock, _last = threading.Lock(), [0.0]


def get(url, params=None, tries=4):
    for i in range(tries):
        with _lock:                                   # global throttle
            wait = 0.08 - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.6 * (i + 1))
    return {}


def tg(msg):
    print(msg)
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": html.escape(msg)}, timeout=10)
    except Exception as e:
        print("TG error", e)


def history(sym, since_ms):
    out, page = [], 1
    while True:
        d = get(f"{BASE}/funding_rate/history",
                {"symbol": sym, "page_num": page, "page_size": 100}).get("data") or {}
        rows = d.get("resultList") or []
        if not rows:
            break
        out += rows
        if min(int(r["settleTime"]) for r in rows) < since_ms:
            break
        if page >= int(d.get("totalPage") or 1):
            break
        page += 1
    rows = sorted([r for r in out if int(r["settleTime"]) >= since_ms], key=lambda r: int(r["settleTime"]))
    ev = []
    for i, r in enumerate(rows):
        cyc = int(r.get("collectCycle") or 0)
        if not cyc and i > 0:                          # infer cycle from spacing if field missing
            cyc = round((int(r["settleTime"]) - int(rows[i - 1]["settleTime"])) / 3.6e6)
        rate = float(r["fundingRate"])
        if cyc == CYCLE_HOURS and abs(rate) >= MIN_RATE:
            t = round(int(r["settleTime"]) / 1000 / 60) * 60
            ev.append(dict(sym=sym, rate=rate, T=t))
    return ev


def klines(sym, t0, t1):
    d = get(f"{BASE}/kline/{sym}", {"interval": "Min1", "start": int(t0), "end": int(t1)}).get("data") or {}
    out = {}
    for t, o, h, l, c, v in zip(d.get("time", []), d.get("open", []), d.get("high", []),
                                d.get("low", []), d.get("close", []), d.get("vol", [])):
        out[int(t)] = (float(o), float(h), float(l), float(c), float(v))
    return out


def simulate(ev, wallet, cs):
    T, rate, sym = ev["T"], ev["rate"], ev["sym"]
    N = wallet * WALLET_PCT * LEVERAGE
    k = klines(sym, T - 300, T + 360)
    c0 = k.get(T - 60)
    if not c0 or not k.get(T):
        return None, "no candles"
    turnover = sum(v * cs * c for (o, h, l, c, v) in (k[t] for t in k if t < T))
    if turnover < VOL_MULT * N:
        return None, "illiquid"

    s = -1 if rate > 0 else 1                          # short collects positive funding
    entry = c0[0] * (1 + s * SLIP_SIDE)

    def chg_of(raw):                                   # executable exit move vs entry
        return s * (raw * (1 - s * SLIP_SIDE) - entry) / entry

    def worst(c):                                      # adverse extreme of a candle
        return s * ((c[2] if s == 1 else c[1]) - entry) / entry

    funding, chg, reason = 0.0, 0.0, None
    w = worst(c0)
    if w <= -SL_PRICE:                                 # stopped before funding
        chg = -LIQ_MOVE if w <= -LIQ_MOVE else -(SL_PRICE + SL_SLIP)
        reason = "SL pre-funding"
    else:
        funding = abs(rate) * N
        floor = -(abs(rate) - 2 * FEE_SIDE - MIN_LOCK)
        c = chg_of(k[T][0])                            # price right after settlement
        if c >= -NEAR_AVG:
            chg, reason = c, ("AVG/PROFIT" if c >= 0 else "NEAR AVG")
        else:
            last = k[T][3]
            for i in range(1, MAX_HOLD // 60 + 1):
                cd = k.get(T + 60 * (i - 1))
                if not cd:
                    continue
                last = cd[3]
                w = worst(cd)
                if w <= -SL_PRICE:
                    chg = -LIQ_MOVE if w <= -LIQ_MOVE else -(SL_PRICE + SL_SLIP)
                    reason = "SL"
                    break
                c = chg_of(cd[3])
                if c >= -NEAR_AVG:
                    chg, reason = c, ("AVG/PROFIT" if c >= 0 else "NEAR AVG")
                    break
                if 60 * i > FALLBACK_SEC and c >= floor:
                    chg, reason = c, "NET-POSITIVE"
                    break
                if i == MAX_HOLD // 60:
                    chg, reason = c, "TIMEOUT"
            if reason is None:
                chg, reason = chg_of(last), "TIMEOUT"
    net = funding + N * chg - 2 * FEE_SIDE * N
    return dict(T=T, sym=sym, side="SHORT" if s == -1 else "LONG", rate=rate, notional=N,
                price_move=chg, funding=funding, net=net, reason=reason), None


def main():
    since = time.time() - DAYS * 86400
    detail = get(f"{BASE}/detail").get("data") or []
    cs = {d["symbol"]: float(d.get("contractSize") or 1) for d in detail
          if str(d.get("symbol", "")).endswith("_USDT")}
    tg(f"Backtest start: {len(cs)} USDT perps, last {DAYS} days, start ${START_BAL}")

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda s: history(s, since * 1000), list(cs)))
    slots = {}
    for evs in res:
        for e in evs:
            slots.setdefault(e["T"], []).append(e)
    n_events = sum(len(v) for v in slots.values())
    print(f"{n_events} qualifying funding events over {len(slots)} slots")

    wallet, peak, max_dd = START_BAL, START_BAL, 0.0
    trades, skipped = [], {"no candidates": 0, "illiquid": 0, "no candles": 0}
    for T in sorted(slots):
        cands = sorted(slots[T], key=lambda e: -abs(e["rate"]))[:TOP_TRY]
        done = False
        for ev in cands:
            tr, err = simulate(ev, wallet, cs[ev["sym"]])
            if tr:
                wallet = max(wallet + tr["net"], 0)
                tr["wallet"] = wallet
                trades.append(tr)
                peak = max(peak, wallet)
                max_dd = max(max_dd, (peak - wallet) / peak if peak else 0)
                done = True
                break
            skipped[err] += 1
        if not done:
            skipped["no candidates"] += 1
        if wallet <= 0.5:
            break

    with open("trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time_utc", "sym", "side", "rate", "notional", "price_move",
                                          "funding", "net", "reason", "wallet"])
        w.writeheader()
        for t in trades:
            w.writerow(dict(time_utc=datetime.fromtimestamp(t["T"], timezone.utc).strftime("%Y-%m-%d %H:%M"),
                            **{k: (round(t[k], 6) if isinstance(t[k], float) else t[k])
                               for k in ["sym", "side", "rate", "notional", "price_move", "funding",
                                         "net", "reason", "wallet"]}))

    if not trades:
        tg("Backtest finished: no tradable events found.")
        return
    wins = [t for t in trades if t["net"] > 0]
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    worst_t = min(trades, key=lambda t: t["net"])
    best_t = max(trades, key=lambda t: t["net"])
    tg(f"📊 BACKTEST {DAYS}d | 5x | 99% wallet | compounding\n"
       f"Slots with events: {len(slots)} | traded: {len(trades)} | skipped: {sum(skipped.values())} {skipped}\n"
       f"Win rate: {len(wins)/len(trades)*100:.1f}% | exits: {reasons}\n"
       f"Balance: ${START_BAL:.2f} -> ${wallet:.2f} ({(wallet/START_BAL-1)*100:+.1f}%)\n"
       f"Max drawdown: {max_dd*100:.1f}%\n"
       f"Best: {best_t['sym']} ${best_t['net']:+.2f} | Worst: {worst_t['sym']} ${worst_t['net']:+.2f} ({worst_t['reason']})\n"
       f"Assumed fee {FEE_SIDE*100:.2f}%/side, slippage {SLIP_SIDE*100:.2f}%/side. 1-min candles, conservative exits.")


if __name__ == "__main__":
    main()
