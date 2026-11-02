import os, time, csv, html, threading, requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

# Public MEXC data only - no API keys needed. Telegram is optional.
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

DAYS = int(os.getenv("DAYS", "30"))
START_BAL = float(os.getenv("START_BALANCE", "100"))
SWEEP = os.getenv("SWEEP", "1") == "1"            # 1 = run several stress scenarios in one go
SLIP_ENV = float(os.getenv("SLIP_SIDE", "0.0005"))  # used when SWEEP=0
CAP_ENV = float(os.getenv("MAX_NOTIONAL", "0"))     # used when SWEEP=0 (0 = no cap)

# ===== SAME RULES AS THE LIVE BOT =====
LEVERAGE = 5
WALLET_PCT = 0.99
CYCLE_HOURS = 4
MIN_RATE = 0.005
FEE_SIDE = 0.0004        # taker fee per side
SL_PRICE = 0.03          # -3% price = -15% margin
SL_SLIP = 0.003          # extra slippage when a stop is hit
LIQ_MOVE = 0.19          # adverse move that would liquidate 5x isolated
NEAR_AVG = 0.0008        # close at entry / profit / up to -0.08%
VOL_MULT = 10            # pair's last-5-min turnover must be >= 10x our notional
TOP_TRY = 5              # candidates tried per slot

# (slippage per side, max position size in USDT, 0 = no cap)
SCENARIOS = [(0.0005, 0), (0.0015, 0), (0.0015, 3000), (0.003, 3000), (0.003, 1000)]

BASE = "https://contract.mexc.com/api/v1/contract"
_lock, _last = threading.Lock(), [0.0]
_kc = {}                                           # kline cache


def get(url, params=None, tries=5):
    """Returns parsed JSON, or None if every try failed (so callers can detect missing data)."""
    for i in range(tries):
        with _lock:                                   # global throttle
            wait = 0.1 - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.8 * (i + 1))
    return None


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
    """Qualifying events for one symbol, or None if the data could not be fetched."""
    out, page = [], 1
    while True:
        j = get(f"{BASE}/funding_rate/history", {"symbol": sym, "page_num": page, "page_size": 100})
        if j is None:
            return None
        d = j.get("data") or {}
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


def load_events(syms, since_ms):
    """Fetch all symbols; retry failures up to 3 passes so results are stable run to run."""
    events, pending = {}, list(syms)
    for attempt in range(3):
        with ThreadPoolExecutor(6 if attempt == 0 else 3) as ex:
            res = list(ex.map(lambda s: history(s, since_ms), pending))
        nxt = []
        for s, r in zip(pending, res):
            if r is None:
                nxt.append(s)
            else:
                events[s] = r
        pending = nxt
        print(f"pass {attempt+1}: loaded {len(events)}, failed {len(pending)}")
        if not pending:
            break
        time.sleep(3)
    return events, pending


def klines(sym, T):
    key = (sym, T)
    if key in _kc:
        return _kc[key]
    j = get(f"{BASE}/kline/{sym}", {"interval": "Min1", "start": int(T - 300), "end": int(T + 360)})
    if j is None:
        return {}
    d = j.get("data") or {}
    out = {}
    for t, o, h, l, c, v in zip(d.get("time", []), d.get("open", []), d.get("high", []),
                                d.get("low", []), d.get("close", []), d.get("vol", [])):
        out[int(t)] = (float(o), float(h), float(l), float(c), float(v))
    _kc[key] = out
    return out


def simulate(ev, wallet, cs, slip, cap):
    T, rate, sym = ev["T"], ev["rate"], ev["sym"]
    N = wallet * WALLET_PCT * LEVERAGE
    if cap:
        N = min(N, cap)
    k = klines(sym, T)
    c0 = k.get(T - 60)
    if not c0 or not k.get(T):
        return None, "no candles"
    turnover = sum(v * cs * c for (o, h, l, c, v) in (k[t] for t in k if t < T))
    if turnover < VOL_MULT * N:
        return None, "illiquid"

    s = -1 if rate > 0 else 1                          # short collects positive funding
    entry = c0[0] * (1 + s * slip)

    def chg_of(raw):                                   # executable exit move vs entry
        return s * (raw * (1 - s * slip) - entry) / entry

    def worst(c):                                      # adverse extreme of a candle
        return s * ((c[2] if s == 1 else c[1]) - entry) / entry

    funding = 0.0
    w = worst(c0)
    if w <= -SL_PRICE:                                 # stopped before funding
        chg = -LIQ_MOVE if w <= -LIQ_MOVE else -(SL_PRICE + SL_SLIP)
        reason = "SL pre-funding"
    else:
        funding = abs(rate) * N
        chg = chg_of(k[T][0])                          # price ~ right at settlement (T..T+3s)
        reason = "AVG/PROFIT" if chg >= 0 else ("NEAR AVG" if chg >= -NEAR_AVG else "FORCE CLOSE 3s")
    net = funding + N * chg - 2 * FEE_SIDE * N
    return dict(T=T, sym=sym, side="SHORT" if s == -1 else "LONG", rate=rate, notional=N,
                price_move=chg, funding=funding, net=net, reason=reason), None


def run(slots, cs, slip, cap):
    wallet, peak, dd = START_BAL, START_BAL, 0.0
    trades, skipped = [], {"illiquid": 0, "no candles": 0, "no candidates": 0}
    for T in sorted(slots):
        done = False
        for ev in sorted(slots[T], key=lambda e: -abs(e["rate"]))[:TOP_TRY]:
            tr, err = simulate(ev, wallet, cs[ev["sym"]], slip, cap)
            if tr:
                wallet = max(wallet + tr["net"], 0)
                tr["wallet"] = wallet
                trades.append(tr)
                peak = max(peak, wallet)
                dd = max(dd, (peak - wallet) / peak if peak else 0)
                done = True
                break
            skipped[err] += 1
        if not done:
            skipped["no candidates"] += 1
        if wallet <= 0.5:
            break
    nets = sorted([t["net"] for t in trades], reverse=True)
    return dict(slip=slip, cap=cap, wallet=wallet, dd=dd, trades=trades, skipped=skipped,
                wins=sum(1 for t in trades if t["net"] > 0),
                sl=sum(1 for t in trades if t["reason"].startswith("SL")),
                ex_top3=sum(nets) - sum(nets[:3]))


def write_csv(trades):
    with open("trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time_utc", "sym", "side", "rate", "notional", "price_move",
                                          "funding", "net", "reason", "wallet"])
        w.writeheader()
        for t in trades:
            row = {k: (round(t[k], 6) if isinstance(t[k], float) else t[k])
                   for k in ["sym", "side", "rate", "notional", "price_move", "funding", "net", "reason", "wallet"]}
            row["time_utc"] = datetime.fromtimestamp(t["T"], timezone.utc).strftime("%Y-%m-%d %H:%M")
            w.writerow(row)


def line(r):
    n = len(r["trades"])
    cap = f"cap ${r['cap']:.0f}" if r["cap"] else "no cap"
    return (f"slip {r['slip']*100:.2f}% {cap}: ${START_BAL:.0f} -> ${r['wallet']:.2f} "
            f"({(r['wallet']/START_BAL-1)*100:+.0f}%) | {n} trades, win {r['wins']/max(n,1)*100:.0f}%, "
            f"SL {r['sl']}, DD {r['dd']*100:.0f}%, ex-top3 {r['ex_top3']:+.1f}")


def main():
    since = time.time() - DAYS * 86400
    j = get(f"{BASE}/detail") or {}
    cs = {d["symbol"]: float(d.get("contractSize") or 1) for d in (j.get("data") or [])
          if str(d.get("symbol", "")).endswith("_USDT")}
    tg(f"Backtest start: {len(cs)} USDT perps, last {DAYS} days, start ${START_BAL}")

    events, failed = load_events(cs, since * 1000)
    slots = {}
    for evs in events.values():
        for e in evs:
            slots.setdefault(e["T"], []).append(e)
    n_ev = sum(len(v) for v in slots.values())
    quality = (f"DATA: {len(events)}/{len(cs)} symbols loaded, {len(failed)} failed | "
               f"{n_ev} qualifying events over {len(slots)} slots")
    if failed:
        quality += f"\nFailed (results incomplete): {', '.join(failed[:10])}{' ...' if len(failed) > 10 else ''}"

    scen = SCENARIOS if SWEEP else [(SLIP_ENV, CAP_ENV)]
    results = [run(slots, cs, s, c) for s, c in scen]
    if not results[0]["trades"]:
        tg(quality + "\nNo tradable events found.")
        return
    write_csv(results[0]["trades"])

    base = results[0]
    reasons = {}
    for t in base["trades"]:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    best = max(base["trades"], key=lambda t: t["net"])
    worst = min(base["trades"], key=lambda t: t["net"])
    tg(f"📊 BACKTEST {DAYS}d | 5x | 99% wallet | compounding | force close 3s\n{quality}\n"
       f"Skipped (base): {base['skipped']}\nExits (base): {reasons}\n"
       f"Best {best['sym']} ${best['net']:+.2f} | Worst {worst['sym']} ${worst['net']:+.2f} ({worst['reason']})\n\n"
       + "\n".join(line(r) for r in results) +
       f"\n\nFee {FEE_SIDE*100:.2f}%/side. 1-min candles. Rates known in advance (optimistic). ex-top3 = net $ without the 3 best trades.")


if __name__ == "__main__":
    main()
