import os, io, json, time, requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://contract.mexc.com/api/v1/contract"
STATE_FILE = "state.json"
WATERMARK = "RANJHA"

# ---- weekly accumulation zone rules ----
MIN_DD = 80
LOW_WEEKS = 52
ZONE_PCT = 0.5
MAX_ABOVE_LOW = 0.6
MAX_4W_PUMP = 0.6
MIN_BARS = 30

# ---- trigger timeframes (4h, 8h, 12h) ----
TFS = {"4h": "Hour4", "8h": "Hour8", "12h": "Hour12"}
TF_HOURS = {"Hour4": 4, "Hour8": 8, "Hour12": 12}

# ---- trigger rules (same for every timeframe) ----
MIN_BODY_PCT = 0.5
CROSS_LOOKBACK = 30
MAX_ABOVE_EMA20 = 12.0
MAX_3D_PUMP = 45.0        # skip if already +45% in last 3 days
READY_PCT = 4.0

# ---- smart filters ----
MIN_TURNOVER = 50_000     # min 24h USDT turnover
ALERT_MIN_SCORE = 70      # Grade A only
MIN_RR = 1.0              # min reward/risk (swing SL -> next target)
BTC_CRASH_BLOCK = 4.0     # pause alerts if BTC fell >4% in last 2 candles

WATCHLIST_MAX = 40
WATCH_MAX_DAYS = 21
RESEND_HOURS = 24

SKIP = {"TSLA","NVDA","AAPL","MSFT","AMZN","GOOGL","META","COIN","MSTR","HOOD",
        "CRCL","SPY","QQQ","XAU","XAG","GOLD","SILVER","OIL","USOIL"}
STEP = {"Week1": 604800, "Hour4": 14400, "Hour8": 28800, "Hour12": 43200}


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
    if not j or not j.get("data"):
        return [], {}
    syms = []
    for x in j["data"]:
        if x.get("quoteCoin") != "USDT" or x.get("state") != 0:
            continue
        if x.get("baseCoin", "").upper() in SKIP:
            continue
        syms.append(x["symbol"])
    turn = {}
    t = get(BASE + "/ticker")
    if t and t.get("data"):
        for x in t["data"]:
            try:
                turn[x["symbol"]] = float(x.get("amount24") or 0)
            except Exception:
                pass
    if turn:
        syms = [s for s in syms if turn.get(s, 0) >= MIN_TURNOVER]
    return syms, turn

def klines(sym, interval, n=500):
    end = int(time.time())
    j = get(f"{BASE}/kline/{sym}", {"interval": interval,
            "start": end - STEP[interval] * n, "end": end})
    d = j.get("data") if j else None
    if not d or not d.get("time"):
        return None
    vol = d.get("vol") or [0] * len(d["time"])
    df = pd.DataFrame({"ts": d["time"], "o": d["open"], "h": d["high"],
                       "l": d["low"], "c": d["close"], "v": vol})
    df["t"] = pd.to_datetime(df.ts, unit="s")
    return df

def to_12h(df4):
    """MEXC has no 12h interval -> build it from 4h candles (00:00 / 12:00 UTC)."""
    d = df4.copy()
    d["k"] = (d.ts // 43200) * 43200
    out = d.groupby("k").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"),
                             c=("c", "last"), v=("v", "sum")).reset_index()
    out = out.rename(columns={"k": "ts"})
    out["t"] = pd.to_datetime(out.ts, unit="s")
    return out

def klines_tf(sym, tf, n=300):
    if tf == "Hour12":
        raw = klines(sym, "Hour4", n * 3)
        return to_12h(raw) if raw is not None else None
    return klines(sym, tf, n)

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

def grade(score):
    return "A" if score >= 70 else ("B" if score >= 55 else "C")


# ---------------- scoring ----------------
def weekly_score(dd, above, rng, r, div):
    s = min(dd, 99) / 99 * 15
    s += max(0, 1 - above / MAX_ABOVE_LOW) * 10
    s += max(0, 1 - rng / 1.5) * 7
    s += 5 if r < 40 else (2 if r < 50 else 0)
    s += 3 if div else 0
    return s                                   # max 40

def trigger_score(since, vr, body, stack, slope, ext):
    s = 20 if since <= 3 else 14 if since <= 8 else 8 if since <= 15 else 3
    s += 15 if vr >= 2 else 10 if vr >= 1.5 else 5 if vr >= 1.2 else 0
    s += min(body, 6) / 6 * 8
    s += 7 if stack else 0
    s += 5 if slope else 0
    s += 5 if ext <= 4 else 2 if ext <= 8 else 0
    return s                                   # max 60


# ---------------- weekly accumulation zone ----------------
def analyze_zone(sym, gate=True):
    try:
        full = klines(sym, "Week1")
        if full is None or len(full) < MIN_BARS + 1:
            return None
        df = closed(full, STEP["Week1"])
        ath = float(df.h.max())
        low = float(df.l.iloc[-LOW_WEEKS:].min())
        close = float(df.c.iloc[-1])
        dd = (1 - close / ath) * 100
        above = close / low - 1
        pump4 = close / float(df.l.iloc[-4:].min()) - 1
        rng = float(df.h.iloc[-6:].max() / df.l.iloc[-6:].min() - 1)
        div = bool(bull_div(df))
        r = float(rsi(df.c).iloc[-1])
        passed = bool(dd >= MIN_DD and above <= MAX_ABOVE_LOW and pump4 <= MAX_4W_PUMP)
        if gate and not passed:
            return None
        ws = weekly_score(dd, above, rng, r, div)
        fib = [low + (ath - low) * f for f in (0.236, 0.382, 0.618)]
        return dict(symbol=sym, passed=passed, wscore=round(ws, 1),
                    dd=round(dd, 1), above_low_pct=round(above * 100, 1),
                    pump4w_pct=round(pump4 * 100, 1), rsi=round(r, 1),
                    divergence=div, low52=low,
                    zone_low=low, zone_high=low * (1 + ZONE_PCT), sl=low * 0.97,
                    t1=fib[0], t2=fib[1], t3=fib[2], wk=full)
    except Exception:
        return None


# ---------------- trigger status (one timeframe) ----------------
def tf_status(sym, tf):
    try:
        h4 = klines_tf(sym, tf, 300)
        if h4 is None or len(h4) < 60:
            return None, None, {}
        for n in (6, 12, 20):
            h4[f"e{n}"] = ema(h4.c, n)
        hc = closed(h4, STEP[tf])
        if len(hc) < 40:
            return None, None, {}
        last = hc.iloc[-1]
        body = float((last.c / last.o - 1) * 100)
        ext = float((last.c / last.e20 - 1) * 100)
        since = None
        for k in range(1, CROSS_LOOKBACK + 1):
            p = hc.iloc[-1 - k]
            if p.c <= p.e20:
                since = k
                break
        vol_avg = float(hc.v.iloc[-21:-1].mean())
        vr = float(last.v / vol_avg) if vol_avg > 0 else 0.0
        stack = bool(last.e6 > last.e12 > last.e20)
        slope = bool(last.e20 > hc.e20.iloc[-4])
        swing_sl = float(hc.l.iloc[-12:].min()) * 0.995
        bars_3d = 72 // TF_HOURS[tf]              # 18 / 9 / 6 candles = 3 days
        pump3d = float((last.c / hc.l.iloc[-bars_3d:].min() - 1) * 100)
        green_above = bool(last.c > last.o and last.c > last.e6
                           and last.c > last.e12 and last.c > last.e20
                           and body >= MIN_BODY_PCT)
        trig = bool(green_above and since is not None
                    and ext <= MAX_ABOVE_EMA20 and pump3d <= MAX_3D_PUMP)
        gap = float((last.e20 / last.c - 1) * 100)
        ready = bool(not trig and last.c <= last.e20 and gap <= READY_PCT
                     and last.e6 > hc.e6.iloc[-4])
        status = "triggered" if trig else ("ready" if ready else None)
        s4 = trigger_score(since, vr, body, stack, slope, ext) if trig else 0.0
        info = dict(body=round(body, 2), gap=round(gap, 2), ext=round(ext, 2),
                    since=since, vr=round(vr, 2), stack=stack, slope=slope,
                    swing_sl=swing_sl, pump3d=round(pump3d, 1),
                    green_above=green_above, s4=round(s4, 1))
        return status, h4, info
    except Exception:
        return None, None, {}

def all_status(sym):
    """Run the trigger check on 4h, 8h and 12h."""
    out = {}
    for label, tf in TFS.items():
        st, df, info = tf_status(sym, tf)
        if info:
            info["tf"] = label
        out[label] = (st, df, info)
    return out

def pick(res):
    """Best timeframe: triggered (highest score) first, else 'ready' (smallest gap)."""
    trig = [(l, v) for l, v in res.items() if v[0] == "triggered"]
    if trig:
        l, v = max(trig, key=lambda x: (x[1][2]["s4"], TF_HOURS[TFS[x[0]]]))
        others = [k for k, _ in trig if k != l]
        return "triggered", l, v[1], v[2], others
    rd = [(l, v) for l, v in res.items() if v[0] == "ready"]
    if rd:
        l, v = min(rd, key=lambda x: x[1][2]["gap"])
        return "ready", l, v[1], v[2], []
    return None, None, None, {}, []

def plan(z, price, swing_sl):
    tgt = next((t for t in (z["t1"], z["t2"], z["t3"]) if t > price * 1.03), z["t3"])
    risk = price - swing_sl
    rr = (tgt - price) / risk if risk > 0 and tgt > price else 0.0
    return tgt, round(rr, 2)

def btc_regime():
    try:
        b = klines("BTC_USDT", "Hour4", 100)
        b["e20"] = ema(b.c, 20)
        bc = closed(b, STEP["Hour4"])
        chg2 = float((bc.c.iloc[-1] / bc.c.iloc[-3] - 1) * 100)
        return dict(above=bool(bc.c.iloc[-1] > bc.e20.iloc[-1]), chg2=round(chg2, 2))
    except Exception:
        return dict(above=True, chg2=0.0)


# ---------------- chart + telegram ----------------
def candles(ax, df):
    for i, row in df.iterrows():
        col = "green" if row.c >= row.o else "red"
        ax.plot([i, i], [row.l, row.h], color=col, lw=1)
        ax.bar(i, abs(row.c - row.o), bottom=min(row.o, row.c), color=col, width=0.7)

def make_chart(z, h4, info, total, tgt, tf):
    fig, (a2, a1) = plt.subplots(2, 1, figsize=(12, 13),
                                 gridspec_kw={"height_ratios": [3, 2]})
    # ---------- trigger timeframe (top, big) ----------
    h = h4.iloc[-90:].reset_index(drop=True)
    candles(a2, h)
    for n, c in ((6, "#e377c2"), (12, "#ff9f40"), (20, "#6c5ce7")):
        a2.plot(h[f"e{n}"], color=c, lw=1.6, label=f"EMA{n}")
    price = float(h.c.iloc[-1])
    sl4 = float(info["swing_sl"])
    a2.axhline(price, color="green", ls=":", lw=1.2, label=f"Live {price:.6g}")
    a2.axhline(sl4, color="red", ls="--", lw=1.2, label=f"{tf} SL {sl4:.6g}")
    lo = min(float(h.l.min()), sl4) * 0.98
    hi = float(max(h.h.max(), h.e20.max())) * 1.04
    if tgt <= hi * 1.5:                       # draw target only if it fits
        a2.axhline(tgt, color="blue", ls="--", lw=1.2, label=f"Target {tgt:.6g}")
        hi = max(hi, tgt * 1.02)
    a2.set_ylim(lo, hi)
    a2.legend(fontsize=9, loc="upper left")
    st = max(len(h) // 8, 1)
    a2.set_xticks(range(0, len(h), st))
    a2.set_xticklabels(h.t.dt.strftime("%d %b %H:%M")[::st], fontsize=8)
    a2.set_title(f'{z["symbol"]} {tf} | Grade {grade(total)} ({total:.0f}/100) | '
                 f'crossed {info["since"]} candles ago | vol x{info["vr"]} | '
                 f'next target {tgt:.6g}', fontsize=12)
    # ---------- weekly (bottom) ----------
    df = z["wk"].iloc[-130:].reset_index(drop=True)
    a1.set_facecolor("#fff8c4")
    candles(a1, df)
    a1.axhspan(z["zone_low"], z["zone_high"], color="green", alpha=0.25, label="Accumulation Zone")
    a1.axhline(z["sl"], color="red", ls="--", label="SL (1W close below)")
    for k, c in (("t1", "blue"), ("t2", "purple"), ("t3", "navy")):
        a1.axhline(z[k], color=c, lw=0.8, label=f"{k.upper()} {z[k]:.6g}")
    a1.set_yscale("log")
    a1.legend(fontsize=8, loc="upper right")
    st = max(len(df) // 6, 1)
    a1.set_xticks(range(0, len(df), st))
    a1.set_xticklabels(df.t.dt.strftime("%b %y")[::st])
    a1.set_title(f'1W | DD {z["dd"]}% | +{z["above_low_pct"]}% above 52w low', fontsize=11)

    # ---------- watermark ----------
    fig.text(0.5, 0.5, WATERMARK, fontsize=90, color="gray", alpha=0.15,
             ha="center", va="center", rotation=30, fontweight="bold", zorder=0)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf

def tg_photo(buf, caption):
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    for _ in range(2):
        try:
            buf.seek(0)
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendPhoto",
                              data={"chat_id": chat, "caption": caption[:1000]},
                              files={"photo": ("chart.png", buf.getvalue())}, timeout=60)
            print("telegram", r.status_code)
            if r.status_code == 200:
                return True
        except Exception as e:
            print("telegram error", e)
        time.sleep(3)
    return False

def tg_text(text):
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text[:4000]}, timeout=30)
    except Exception as e:
        print("telegram text error", e)


# ---------------- test mode ----------------
def debug(sym):
    sym = sym.upper().replace("USDT", "").replace("_", "") + "_USDT"
    z = analyze_zone(sym, gate=False)
    print("==== TEST", sym, "====")
    if z is None:
        print("no weekly data / not enough bars")
        return
    print(f"WEEKLY  52w low {z['low52']:.6g} | drawdown {z['dd']}% (need >= {MIN_DD})")
    print(f"WEEKLY  above low {z['above_low_pct']}% (need <= {MAX_ABOVE_LOW*100:.0f}) | 4w pump {z['pump4w_pct']}%")
    print(f"WEEKLY  ZONE PASSED: {z['passed']} | RSI {z['rsi']} | div {z['divergence']} | wscore {z['wscore']}/40")
    res = all_status(sym)
    for label, (st, h4, info) in res.items():
        print(f"{label.upper():<7} status: {st}")
        print(f"{label.upper():<7} {info}")
        if st == "triggered":
            total = z["wscore"] + info["s4"]
            price = float(h4.c.iloc[-1])
            tgt, rr = plan(z, price, info["swing_sl"])
            print(f"{label.upper():<7} TOTAL {total:.1f}/100 grade {grade(total)} | need >= {ALERT_MIN_SCORE}")
            print(f"{label.upper():<7} PLAN price {price:.6g} | SL {info['swing_sl']:.6g} | "
                  f"target {tgt:.6g} | RR {rr} (need >= {MIN_RR})")


# ---------------- main ----------------
def main():
    dbg = os.environ.get("DEBUG_SYMBOL", "").strip()
    if dbg:
        debug(dbg)
        return

    state = load_state()
    now = time.time()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    syms, turn = get_symbols()
    print("pairs (liquid crypto):", len(syms))
    if not syms:
        print("no pairs returned by MEXC, exiting")
        return
    with ThreadPoolExecutor(6) as ex:
        zones = [z for z in ex.map(analyze_zone, syms) if z]
    zmap = {z["symbol"]: z for z in zones}
    print("zone pairs:", len(zmap))

    for s in list(state["watchlist"]):
        if s not in zmap:
            z = analyze_zone(s, gate=False)
            if z:
                zmap[s] = z

    # results[sym] = (status, tf_label, df, info, other_triggered_tfs)
    results = {}
    def chk(sym):
        return sym, pick(all_status(sym))
    with ThreadPoolExecutor(6) as ex:
        for sym, r in ex.map(chk, list(zmap)):
            results[sym] = r

    btc = btc_regime()
    btc_blocked = btc["chg2"] <= -BTC_CRASH_BLOCK
    print("BTC regime:", btc, "| blocked:", btc_blocked)

    # 1) alerts (best score first)
    cands = []
    for sym, (st, tf, h4, info, others) in results.items():
        if st != "triggered":
            continue
        z = zmap[sym]
        total = z["wscore"] + info["s4"]
        price = float(h4.c.iloc[-1])
        tgt, rr = plan(z, price, info["swing_sl"])
        cands.append((total, sym, z, h4, info, price, tgt, rr, tf, others))
    cands.sort(key=lambda x: x[0], reverse=True)

    for total, sym, z, h4, info, price, tgt, rr, tf, others in cands:
        if btc_blocked:
            print("BTC crash block, skip", sym)
            continue
        if grade(total) != "A" or total < ALERT_MIN_SCORE or rr < MIN_RR:
            print("filtered (not grade A / low RR)", sym, tf, round(total, 1), "rr", rr)
            continue
        if now - state["sent"].get(sym, 0) < RESEND_HOURS * 3600:
            continue
        btc_txt = "BTC 4h above EMA20 ✅" if btc["above"] else "BTC 4h below EMA20 ⚠️"
        oth_txt = f"Also triggered: {', '.join(others)}\n" if others else ""
        cap = (f"🚀 {sym} | {tf} | Grade {grade(total)} ({total:.0f}/100)\n"
               f"{tf} closed green above EMA20 (crossed {info['since']} candles ago, "
               f"vol x{info['vr']}, {info['ext']}% above EMA20)\n"
               f"{oth_txt}"
               f"Live: {price:.6g}\n"
               f"{tf} SL: {info['swing_sl']:.6g} | Weekly SL: {z['sl']:.6g}\n"
               f"Next target: {tgt:.6g} | R:R {rr}\n"
               f"T1 {z['t1']:.6g} | T2 {z['t2']:.6g} | T3 {z['t3']:.6g}\n"
               f"DD from ATH {z['dd']}% | +{z['above_low_pct']}% above 52w low | "
               f"RSI(1W) {z['rsi']} | Div {z['divergence']}\n"
               f"{btc_txt}")
        try:
            if tg_photo(make_chart(z, h4, info, total, tgt, tf), cap):
                state["sent"][sym] = now
                state["watchlist"].pop(sym, None)
        except Exception as e:
            print("send error", sym, e)

    # 2) watchlist cleanup
    for sym in list(state["watchlist"]):
        w = state["watchlist"][sym]
        z = zmap.get(sym)
        try:
            age_days = (now - datetime.fromisoformat(w["added"]).timestamp()) / 86400
        except Exception:
            age_days = 0
            w["added"] = now_iso
        if z is None or not z["passed"] or age_days > WATCH_MAX_DAYS:
            state["watchlist"].pop(sym)
            continue
        st, tf, h4, info, _ = results.get(sym, (None, None, None, {}, []))
        if st == "ready":
            w["score"] = round(z["wscore"] + max(0, (READY_PCT - info["gap"]) / READY_PCT) * 20, 1)
            w["gap_pct"] = info["gap"]
            w["tf"] = tf

    # 3) add new "ready to cross" pairs (each pair saved once)
    new = []
    for sym, (st, tf, h4, info, _) in results.items():
        z = zmap[sym]
        if st == "ready" and z["passed"] and sym not in state["watchlist"]:
            sc = z["wscore"] + max(0, (READY_PCT - info["gap"]) / READY_PCT) * 20
            new.append((sc, sym, z, info, tf))
    new.sort(key=lambda x: x[0], reverse=True)
    for sc, sym, z, info, tf in new:
        state["watchlist"][sym] = dict(added=now_iso, score=round(sc, 1),
                                       gap_pct=info["gap"], tf=tf,
                                       zone_low=z["zone_low"],
                                       zone_high=z["zone_high"], sl=z["sl"])

    if len(state["watchlist"]) > WATCHLIST_MAX:
        keep = sorted(state["watchlist"].items(), key=lambda kv: kv[1].get("score", 0),
                      reverse=True)[:WATCHLIST_MAX]
        state["watchlist"] = dict(keep)

    # 4) daily watchlist summary (once per UTC day)
    today = now_dt.strftime("%Y-%m-%d")
    if state.get("last_summary") != today and state["watchlist"]:
        rows = sorted(state["watchlist"].items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
        lines = [f"📋 Watchlist ({len(rows)}) - ready to cross EMA20 (4h/8h/12h)"]
        for i, (s, w) in enumerate(rows, 1):
            lines.append(f"{i}. {s}  [{w.get('tf', '4h')}]  score {w.get('score', '?')}  gap {w.get('gap_pct', '?')}%")
        tg_text("\n".join(lines))
        state["last_summary"] = today

    state["sent"] = {k: v for k, v in state["sent"].items() if now - v < 48 * 3600}
    state["updated"] = now_iso
    save_state(state)
    print("watchlist:", len(state["watchlist"]), "| sent log:", len(state["sent"]))

if __name__ == "__main__":
    main()
