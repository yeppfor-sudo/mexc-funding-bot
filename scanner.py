import os, io, json, time, requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://contract.mexc.com/api/v1/contract"
STATE_FILE = "state.json"

# ---- weekly accumulation zone rules ----
MIN_DD = 80
LOW_WEEKS = 52           # zone built from lowest low of last 52 weeks
ZONE_PCT = 0.5
MAX_ABOVE_LOW = 0.6
MAX_4W_PUMP = 0.6
MIN_BARS = 30

# ---- 4h rules ----
MIN_BODY_PCT = 0.5
CROSS_LOOKBACK = 30      # crossed above EMA20 within last 30 closed 4h candles
MAX_ABOVE_EMA20 = 12.0
READY_PCT = 4.0
WATCHLIST_MAX = 40
WATCH_MAX_DAYS = 21
RESEND_HOURS = 24

SKIP = {"TSLA","NVDA","AAPL","MSFT","AMZN","GOOGL","META","COIN","MSTR","HOOD",
        "CRCL","SPY","QQQ","XAU","XAG","GOLD","SILVER","OIL","USOIL"}
STEP = {"Week1": 604800, "Hour4": 14400}


# ---------------- helpers ----------------
def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1 + i)
    return None

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("watchlist", {})
    s.setdefault("sent", {})
    return s

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)

def get_symbols():
    j = get(BASE + "/detail")
    out = []
    for x in j["data"]:
        if x.get("quoteCoin") != "USDT" or x.get("state") != 0:
            continue
        if x.get("baseCoin", "").upper() in SKIP:
            continue
        out.append(x["symbol"])
    return out

def klines(sym, interval, n=500):
    end = int(time.time())
    j = get(f"{BASE}/kline/{sym}", {"interval": interval,
            "start": end - STEP[interval] * n, "end": end})
    d = j.get("data") if j else None
    if not d or not d.get("time"):
        return None
    df = pd.DataFrame({"ts": d["time"], "o": d["open"], "h": d["high"],
                       "l": d["low"], "c": d["close"]})
    df["t"] = pd.to_datetime(df.ts, unit="s")
    return df

def closed(df, step):
    if df.ts.iloc[-1] + step > time.time():
        return df.iloc[:-1].reset_index(drop=True)
    return df.reset_index(drop=True)

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)

def bull_div(df, look=50):
    d = df.iloc[-look:].reset_index(drop=True)
    r = rsi(df["c"]).iloc[-look:].reset_index(drop=True)
    piv = [i for i in range(2, len(d) - 2) if d.l[i] == d.l[i-2:i+3].min()]
    if len(piv) < 2:
        return False
    a, b = piv[-2], piv[-1]
    return d.l[b] < d.l[a] and r[b] > r[a] and (len(d) - b) <= 12


# ---------------- weekly accumulation zone ----------------
def analyze_zone(sym, gate=True):
    try:
        full = klines(sym, "Week1")
        if full is None or len(full) < MIN_BARS + 1:
            return None
        df = closed(full, STEP["Week1"])
        ath = float(df.h.max())
        low = float(df.l.iloc[-LOW_WEEKS:].min())     # 52-week base low
        close = float(df.c.iloc[-1])
        dd = (1 - close / ath) * 100
        above = close / low - 1
        pump4 = close / float(df.l.iloc[-4:].min()) - 1
        rng = float(df.h.iloc[-6:].max() / df.l.iloc[-6:].min() - 1)
        div = bull_div(df)
        r = float(rsi(df.c).iloc[-1])
        passed = bool(dd >= MIN_DD and above <= MAX_ABOVE_LOW
                      and pump4 <= MAX_4W_PUMP)
        if gate and not passed:
            return None
        score = (dd / 10 + div * 3 + (rng < 1.0) * 2 + (r < 40) * 2
                 + max(0, 1 - above / MAX_ABOVE_LOW) * 2)
        fib = [low + (ath - low) * f for f in (0.236, 0.382, 0.618)]
        return dict(symbol=sym, passed=passed, score=round(score, 2),
                    dd=round(dd, 1), above_low_pct=round(above * 100, 1),
                    pump4w_pct=round(pump4 * 100, 1), rsi=round(r, 1),
                    divergence=bool(div), low52=low,
                    zone_low=low, zone_high=low * (1 + ZONE_PCT), sl=low * 0.97,
                    t1=fib[0], t2=fib[1], t3=fib[2], wk=full)
    except Exception:
        return None


# ---------------- 4h status ----------------
def h4_status(sym):
    try:
        h4 = klines(sym, "Hour4", 300)
        if h4 is None or len(h4) < 60:
            return None, None, {}
        for n in (6, 12, 20):
            h4[f"e{n}"] = ema(h4.c, n)
        hc = closed(h4, STEP["Hour4"])
        last = hc.iloc[-1]
        body = float((last.c / last.o - 1) * 100)
        ext = float((last.c / last.e20 - 1) * 100)
        since = None
        for k in range(1, CROSS_LOOKBACK + 1):
            p = hc.iloc[-1 - k]
            if p.c <= p.e20:
                since = k
                break
        green_above = bool(last.c > last.o and last.c > last.e6
                           and last.c > last.e12 and last.c > last.e20
                           and body >= MIN_BODY_PCT)
        trig = bool(green_above and since is not None and ext <= MAX_ABOVE_EMA20)
        gap = float((last.e20 / last.c - 1) * 100)
        ready = bool(not trig and last.c <= last.e20 and gap <= READY_PCT
                     and last.e6 > hc.e6.iloc[-4])
        status = "triggered" if trig else ("ready" if ready else None)
        info = dict(body=round(body, 2), gap=round(gap, 2), ext=round(ext, 2),
                    since=since, green_above=green_above)
        return status, h4, info
    except Exception:
        return None, None, {}


# ---------------- chart + telegram ----------------
def candles(ax, df):
    for i, row in df.iterrows():
        col = "green" if row.c >= row.o else "red"
        ax.plot([i, i], [row.l, row.h], color=col, lw=1)
        ax.bar(i, abs(row.c - row.o), bottom=min(row.o, row.c), color=col, width=0.7)

def make_chart(z, h4, info):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(16, 6))
    df = z["wk"].iloc[-130:].reset_index(drop=True)
    a1.set_facecolor("#fff8c4")
    candles(a1, df)
    a1.axhspan(z["zone_low"], z["zone_high"], color="green", alpha=0.25, label="Accumulation Zone")
    a1.axhline(z["sl"], color="red", ls="--", label="SL (1W close below)")
    for k, c in (("t1", "blue"), ("t2", "purple"), ("t3", "navy")):
        a1.axhline(z[k], color=c, lw=0.8, label=f"{k.upper()} {z[k]:.6g}")
    a1.set_yscale("log")
    a1.legend(fontsize=7, loc="upper right")
    st = max(len(df) // 6, 1)
    a1.set_xticks(range(0, len(df), st))
    a1.set_xticklabels(df.t.dt.strftime("%b %y")[::st])
    a1.set_title(f'{z["symbol"]} 1W | DD {z["dd"]}% | +{z["above_low_pct"]}% above 52w low')
    h = h4.iloc[-90:].reset_index(drop=True)
    candles(a2, h)
    for n, c in ((6, "#e377c2"), (12, "#ff9f40"), (20, "#6c5ce7")):
        a2.plot(h[f"e{n}"], color=c, lw=1.4, label=f"EMA{n}")
    a2.legend(fontsize=7, loc="upper left")
    st = max(len(h) // 6, 1)
    a2.set_xticks(range(0, len(h), st))
    a2.set_xticklabels(h.t.dt.strftime("%d %b %H:%M")[::st], fontsize=7)
    a2.set_title(f'4h | crossed {info.get("since")} candles ago | '
                 f'{info.get("ext")}% above EMA20 | LIVE {h.c.iloc[-1]:.6g}')
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf

def tg_photo(buf, caption):
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendPhoto",
                      data={"chat_id": chat, "caption": caption[:1000]},
                      files={"photo": ("chart.png", buf.getvalue())}, timeout=60)
    print("telegram", r.status_code)
    return r.status_code == 200


# ---------------- test mode ----------------
def debug(sym):
    sym = sym.upper().replace("USDT", "").replace("_", "") + "_USDT"
    z = analyze_zone(sym, gate=False)
    print("==== TEST", sym, "====")
    if z is None:
        print("no weekly data / not enough bars")
        return
    print(f"WEEKLY  52w low {z['low52']:.6g}")
    print(f"WEEKLY  drawdown {z['dd']}% (need >= {MIN_DD})")
    print(f"WEEKLY  above 52w low {z['above_low_pct']}% (need <= {MAX_ABOVE_LOW*100:.0f})")
    print(f"WEEKLY  4w pump {z['pump4w_pct']}% (need <= {MAX_4W_PUMP*100:.0f})")
    print(f"WEEKLY  ZONE PASSED: {z['passed']} | RSI {z['rsi']} | divergence {z['divergence']}")
    st, h4, info = h4_status(sym)
    print(f"4H      status: {st}")
    print(f"4H      {info}")
    print(f"4H      need: green_above True, since not None (<= {CROSS_LOOKBACK}), "
          f"ext <= {MAX_ABOVE_EMA20}")


# ---------------- main ----------------
def main():
    dbg = os.environ.get("DEBUG_SYMBOL", "").strip()
    if dbg:
        debug(dbg)
        return

    state = load_state()
    now = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    syms = get_symbols()
    print("pairs:", len(syms))
    with ThreadPoolExecutor(6) as ex:
        zones = [z for z in ex.map(analyze_zone, syms) if z]
    zmap = {z["symbol"]: z for z in zones}
    print("zone pairs:", len(zmap))

    for s in list(state["watchlist"]):
        if s not in zmap:
            z = analyze_zone(s, gate=False)
            if z:
                zmap[s] = z

    results = {}
    def chk(sym):
        st, h4, info = h4_status(sym)
        return sym, st, h4, info
    with ThreadPoolExecutor(6) as ex:
        for sym, st, h4, info in ex.map(chk, list(zmap)):
            results[sym] = (st, h4, info)

    # 1) alerts
    for sym, (st, h4, info) in results.items():
        if st != "triggered":
            continue
        if now - state["sent"].get(sym, 0) < RESEND_HOURS * 3600:
            continue
        z = zmap[sym]
        price = h4.c.iloc[-1]
        cap = (f"🚀 {sym} 4h closed green above EMA20\n"
               f"Live price: {price:.6g} ({info['ext']}% above EMA20, "
               f"crossed {info['since']} candles ago)\n"
               f"Zone: {z['zone_low']:.6g} - {z['zone_high']:.6g}\n"
               f"SL (1W close below): {z['sl']:.6g}\n"
               f"T1 {z['t1']:.6g} | T2 {z['t2']:.6g} | T3 {z['t3']:.6g}\n"
               f"DD from ATH: {z['dd']}% | RSI(1W): {z['rsi']} | Div: {z['divergence']}")
        try:
            if tg_photo(make_chart(z, h4, info), cap):
                state["sent"][sym] = now
                state["watchlist"].pop(sym, None)
        except Exception as e:
            print("send error", sym, e)

    # 2) watchlist cleanup
    for sym in list(state["watchlist"]):
        w = state["watchlist"][sym]
        z = zmap.get(sym)
        age_days = (now - datetime.fromisoformat(w["added"]).timestamp()) / 86400
        if z is None or not z["passed"] or age_days > WATCH_MAX_DAYS:
            state["watchlist"].pop(sym)
            continue
        if results.get(sym, (None,))[0] == "ready":
            w["score"] = round(z["score"] + 3, 2)

    # 3) add new "ready to cross" pairs (each pair saved once)
    new = []
    for sym, (st, h4, info) in results.items():
        z = zmap[sym]
        if st == "ready" and z["passed"] and sym not in state["watchlist"]:
            sc = z["score"] + max(0, (READY_PCT - info["gap"]) / READY_PCT) * 3
            new.append((sc, sym, z, info))
    new.sort(reverse=True)
    for sc, sym, z, info in new:
        state["watchlist"][sym] = dict(added=now_iso, score=round(sc, 2),
                                       gap_pct=info["gap"], zone_low=z["zone_low"],
                                       zone_high=z["zone_high"], sl=z["sl"])

    if len(state["watchlist"]) > WATCHLIST_MAX:
        keep = sorted(state["watchlist"].items(), key=lambda kv: kv[1]["score"],
                      reverse=True)[:WATCHLIST_MAX]
        state["watchlist"] = dict(keep)

    state["sent"] = {k: v for k, v in state["sent"].items() if now - v < 48 * 3600}
    state["updated"] = now_iso
    save_state(state)
    print("watchlist:", len(state["watchlist"]), "| sent log:", len(state["sent"]))

if __name__ == "__main__":
    main()
