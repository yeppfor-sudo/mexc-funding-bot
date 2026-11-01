#!/usr/bin/env python3
"""
MEXC perpetual scanner
Finds: wedge breakout -> retest of the broken trendline (previous closed candle)
  LONG  = falling wedge breaks up, price retests the broken upper line
  SHORT = rising wedge breaks down, price retests the broken lower line
Sends a chart (entry / SL / TP marked) to Telegram. Runs on GitHub Actions.
"""
import io
import os
import sys
import json
import math
import time
import html
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf

# ----------------------------------------------------------------- config
BASE = "https://contract.mexc.com"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAMES = [t.strip() for t in os.getenv("TIMEFRAMES", "1h,2h,4h,8h,12h,1d,3d,7d").split(",") if t.strip()]
EMA_MODE = os.getenv("EMA_MODE", "basic").lower()          # off | basic | strict
ENABLE_SHORTS = os.getenv("ENABLE_SHORTS", "true").lower() == "true"
MIN_VOL_USDT = float(os.getenv("MIN_VOL_USDT", "1000000"))  # 24h turnover filter
MAX_ALERTS = int(os.getenv("MAX_ALERTS", "25"))             # per run
WORKERS = int(os.getenv("WORKERS", "6"))

PIVOT_ORDER = 3     # bars each side to confirm a swing high/low
LOOKBACK = 90       # max bars of wedge before breakout
MIN_BARS = 15       # min wedge length
MAX_AFTER = 12      # breakout may be at most this many bars before the retest candle
SL_ATR = 0.25       # SL buffer in ATR
STATE_FILE = "state.json"

# Perps that are not crypto (stocks / metals / oil). Add more via EXCLUDE_BASES=AAA,BBB
NON_CRYPTO = {
    "TSLA", "NVDA", "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NFLX", "AMD", "INTC",
    "COIN", "MSTR", "HOOD", "CRCL", "PLTR", "BABA", "SPY", "QQQ", "XAU", "XAG", "GOLD",
    "SILVER", "OIL", "USOIL", "UKOIL", "COPPER", "NAS100", "US500",
}
NON_CRYPTO |= {x.strip().upper() for x in os.getenv("EXCLUDE_BASES", "").split(",") if x.strip()}

# tf -> (MEXC source interval, resample rule or None, seconds)
TF = {
    "1h": ("Min60", None, 3600),
    "2h": ("Min60", "2h", 7200),
    "4h": ("Hour4", None, 14400),
    "8h": ("Hour8", None, 28800),
    "12h": ("Hour4", "12h", 43200),
    "1d": ("Day1", None, 86400),
    "3d": ("Day1", "3D", 259200),
    "7d": ("Week1", None, 604800),
}
SRC_SEC = {"Min60": 3600, "Hour4": 14400, "Hour8": 28800, "Day1": 86400, "Week1": 604800}
EPOCH = pd.Timestamp("1970-01-01")

# ----------------------------------------------------------------- http
S = requests.Session()
_lock = threading.Lock()
_next = [0.0]


def throttle(gap=0.12):
    with _lock:
        now = time.time()
        wait = _next[0] - now
        _next[0] = max(now, _next[0]) + gap
    if wait > 0:
        time.sleep(wait)


def api_get(path, params=None):
    for attempt in range(4):
        throttle()
        try:
            r = S.get(BASE + path, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(1 + attempt)
    return None


def closed_only(df, sec, now):
    start = np.asarray((df.index - EPOCH).total_seconds())
    return df[start + sec <= now]


def fetch_klines(sym, interval, now):
    sec = SRC_SEC[interval]
    bars = 220 if interval == "Week1" else 400
    j = api_get(f"/api/v1/contract/kline/{sym}",
                {"interval": interval, "start": int(now - sec * bars), "end": int(now)})
    if not j or not j.get("success"):
        return None
    d = j.get("data") or {}
    if not d.get("time"):
        return None
    df = pd.DataFrame({k: d[k] for k in ("open", "high", "low", "close", "vol")},
                      index=pd.to_datetime(d["time"], unit="s")).astype(float).sort_index()
    return closed_only(df, sec, now)


def resample(df, rule):
    return df.resample(rule, origin="epoch").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"}).dropna()


def get_symbols():
    j = api_get("/api/v1/contract/detail")
    data = j.get("data") if j else None
    if isinstance(data, dict):
        data = [data]
    if not data:
        return []
    syms = []
    for x in data:
        base = str(x.get("baseCoin", "")).upper()
        if x.get("quoteCoin") == "USDT" and x.get("state") == 0 and base not in NON_CRYPTO:
            syms.append(x["symbol"])
    jt = api_get("/api/v1/contract/ticker")
    tk = jt.get("data") if jt else None
    if isinstance(tk, list) and tk and MIN_VOL_USDT > 0:
        vol = {t.get("symbol"): float(t.get("amount24") or 0) for t in tk}
        syms = [s for s in syms if vol.get(s, 0) >= MIN_VOL_USDT]
    return syms


# ----------------------------------------------------------------- analysis
def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def pivots(arr, order, high=True):
    out = []
    for i in range(order, len(arr) - order):
        w = arr[i - order:i + order + 1]
        if (high and np.argmax(w) == order) or ((not high) and np.argmin(w) == order):
            out.append(i)
    return out


def invert(df):
    """Mirror prices so a rising-wedge breakdown looks like a falling-wedge breakout."""
    return pd.DataFrame({"open": -df.open, "high": -df.low, "low": -df.high,
                         "close": -df.close, "vol": df.vol})


def ema_ok(d):
    if EMA_MODE == "off":
        return True
    c = d.close
    e6, e12, e20 = (c.ewm(span=s, adjust=False).mean().iloc[-1] for s in (6, 12, 20))
    px = c.iloc[-1]
    if EMA_MODE == "strict":
        return px > e6 > e12 > e20
    return px > e20 and e6 > e12   # basic


def detect(df):
    """Falling wedge breakout (up) + retest of the broken upper line on the last closed candle."""
    n = len(df)
    if n < 60:
        return None
    h, l, c = df.high.values, df.low.values, df.close.values
    last = n - 1
    a = atr(df).iloc[-1]
    if not np.isfinite(a) or a <= 0:
        return None
    ph, pl = pivots(h, PIVOT_ORDER, True), pivots(l, PIVOT_ORDER, False)

    for bi in range(max(last - MAX_AFTER, 30), last):
        hi = [i for i in ph if bi - LOOKBACK <= i <= bi - PIVOT_ORDER][-4:]
        lo = [i for i in pl if bi - LOOKBACK <= i <= bi - PIVOT_ORDER][-4:]
        if len(hi) < 3 or len(lo) < 2:
            continue
        start = min(hi[0], lo[0])
        if bi - start < MIN_BARS:
            continue
        mh, bh = np.polyfit(hi, h[hi], 1)
        ml, bl = np.polyfit(lo, l[lo], 1)
        if not (mh < 0 and ml < 0 and mh < ml):          # both down, upper falls faster
            continue
        U = lambda x: mh * x + bh
        L = lambda x: ml * x + bl
        w0, w1 = U(start) - L(start), U(bi) - L(bi)
        if w0 <= 0 or w1 <= 0 or w1 > 0.75 * w0:         # must converge
            continue
        if np.max(np.abs(h[hi] - U(np.array(hi)))) > 0.2 * w0:
            continue
        if np.max(np.abs(l[lo] - L(np.array(lo)))) > 0.2 * w0:
            continue
        xs = np.arange(start, bi)
        if np.any(c[xs] > U(xs) + 0.05 * w0) or np.any(c[xs] < L(xs) - 0.05 * w0):
            continue                                       # price must respect the wedge
        if not c[bi] > U(bi) + 0.1 * a:                    # breakout close above upper line
            continue
        mid = np.arange(bi + 1, last)
        if len(mid) and np.any(c[mid] < U(mid) - 0.3 * a):  # must keep holding above the line
            continue
        if not (l[last] <= U(last) + 0.3 * a and c[last] > U(last)):  # retest + hold
            continue
        return dict(bi=bi, start=start, mh=mh, bh=bh, ml=ml, bl=bl, w0=w0, atr=a)
    return None


def trade_levels(d, res, sgn):
    last = len(d) - 1
    ul = res["mh"] * last + res["bh"]
    ubi = res["mh"] * res["bi"] + res["bh"]
    entry = d.close.values[last]
    sl = min(d.low.values[last], ul) - SL_ATR * res["atr"]
    risk = entry - sl
    if risk <= 0:
        return None
    tp1, tp2 = entry + 1.5 * risk, entry + 2.5 * risk
    tp3 = ubi + res["w0"]                                  # wedge-height measured move
    if tp3 <= tp2:
        tp3 = entry + 4 * risk
    return [float(sgn * x) for x in (entry, sl, tp1, tp2, tp3)]


def scan_symbol(sym):
    out, cache, now = [], {}, time.time()
    for tf in TIMEFRAMES:
        if tf not in TF:
            continue
        src, rule, sec = TF[tf]
        if src not in cache:
            cache[src] = fetch_klines(sym, src, now)
        raw = cache[src]
        if raw is None or len(raw) < 30:
            continue
        df = closed_only(resample(raw, rule), sec, now) if rule else raw
        if len(df) < 60:
            continue
        for side, sgn in (("long", 1), ("short", -1)):
            if side == "short" and not ENABLE_SHORTS:
                continue
            d = df if sgn == 1 else invert(df)
            res = detect(d)
            if not res or not ema_ok(d):
                continue
            lv = trade_levels(d, res, sgn)
            if not lv:
                continue
            out.append(dict(sym=sym, tf=tf, side=side, sgn=sgn, df=df, res=res, lv=lv,
                            key=f"{sym}|{tf}|{side}|{int(df.index[res['bi']].timestamp())}"))
    return out


def safe_scan(sym):
    try:
        return scan_symbol(sym)
    except Exception as e:
        print(f"[warn] {sym}: {e}")
        return []


# ----------------------------------------------------------------- output
def fmt(x):
    if x == 0:
        return "0"
    d = max(2, 5 - int(math.floor(math.log10(abs(x)))))
    s = f"{x:.{d}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def make_chart(s):
    df, res, sgn = s["df"], s["res"], s["sgn"]
    n = len(df)
    last = n - 1
    lo = max(0, min(res["start"] - 12, n - 100))
    lo = max(lo, n - 200)
    p = df.iloc[lo:][["open", "high", "low", "close"]].copy()
    p.columns = ["Open", "High", "Low", "Close"]
    idx = p.index

    aps = []
    for span, col in ((6, "#ffd166"), (12, "#4cc9f0"), (20, "#f72585")):
        aps.append(mpf.make_addplot(df.close.ewm(span=span, adjust=False).mean().iloc[lo:],
                                    color=col, width=0.9))
    off = res["atr"] * 0.6
    mk = "^" if sgn == 1 else "v"
    for bar, color in ((res["bi"], "#ffd166"), (last, "#00e5ff")):
        if bar < lo:
            continue
        m = pd.Series(np.nan, index=idx)
        k = bar - lo
        m.iloc[k] = p.Low.iloc[k] - off if sgn == 1 else p.High.iloc[k] + off
        aps.append(mpf.make_addplot(m, type="scatter", marker=mk, markersize=90, color=color))

    x0 = max(res["start"], lo)
    def line(m, b):
        return [(df.index[x], float(sgn * (m * x + b))) for x in (x0, last)]
    alines = dict(alines=[line(res["mh"], res["bh"]), line(res["ml"], res["bl"])],
                  colors=["#ffd166", "#a0a0ff"], linewidths=1.4)

    lv = s["lv"]
    cols = ["#ffffff", "#ff4d4d", "#06d6a0", "#06d6a0", "#06d6a0"]
    names = ["ENTRY", "SL", "TP1", "TP2", "TP3"]
    kind = "Falling wedge breakout retest" if sgn == 1 else "Rising wedge breakdown retest"
    title = f"{s['sym']} PERP  {s['tf']}  {s['side'].upper()}  |  {kind}"

    fig, axes = mpf.plot(p, type="candle", style="nightclouds", addplot=aps, alines=alines,
                         hlines=dict(hlines=lv, colors=cols, linestyle="--", linewidths=0.9),
                         title=title, figsize=(11, 6.5), returnfig=True, tight_layout=True)
    ax = axes[0]
    for v, nm, col in zip(lv, names, cols):
        ax.text(0.99, v, f"{nm} {fmt(v)} ", transform=ax.get_yaxis_transform(),
                color=col, fontsize=8, ha="right", va="bottom")
    ymin, ymax = min(float(p.Low.min()), *lv), max(float(p.High.max()), *lv)
    pad = (ymax - ymin) * 0.04
    ax.set_ylim(ymin - pad, ymax + pad)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def caption(s):
    df, lv = s["df"], s["lv"]
    entry, sl, tp1, tp2, tp3 = lv
    pct = lambda v: f"{(v - entry) / entry * 100:+.2f}%"
    c = df.close
    e = [c.ewm(span=x, adjust=False).mean().iloc[-1] for x in (6, 12, 20)]
    icon = "🟢" if s["side"] == "long" else "🔴"
    kind = "Falling wedge breakout → retest" if s["side"] == "long" else "Rising wedge breakdown → retest"
    t = df.index[-1].strftime("%Y-%m-%d %H:%M")
    return (
        f"{icon} <b>{s['side'].upper()}</b> · <b>{html.escape(s['sym'])}</b> (MEXC Perp)\n"
        f"📐 {kind}\n"
        f"⏱ TF <b>{s['tf']}</b> · last closed candle open {t} UTC\n\n"
        f"Entry: <code>{fmt(entry)}</code>\n"
        f"SL: <code>{fmt(sl)}</code> ({pct(sl)})\n"
        f"TP1: <code>{fmt(tp1)}</code> ({pct(tp1)})\n"
        f"TP2: <code>{fmt(tp2)}</code> ({pct(tp2)})\n"
        f"TP3: <code>{fmt(tp3)}</code> ({pct(tp3)})\n\n"
        f"EMA 6/12/20: {fmt(e[0])} / {fmt(e[1])} / {fmt(e[2])}\n"
        f"⚠️ Not financial advice. Check the chart before trading."
    )


def tg_photo(png, cap):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                      data={"chat_id": CHAT, "caption": cap, "parse_mode": "HTML"},
                      files={"photo": ("chart.png", png, "image/png")}, timeout=60)
    if not r.ok:
        print("[telegram]", r.status_code, r.text[:200])
    return r.ok


def tg_text(msg):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": msg, "parse_mode": "HTML"}, timeout=30)
    if not r.ok:
        print("[telegram]", r.status_code, r.text[:200])
    return r.ok


# ----------------------------------------------------------------- main
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    cutoff = time.time() - 30 * 86400
    st = {k: v for k, v in st.items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=0, sort_keys=True)


def main():
    if not TOKEN or not CHAT:
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        sys.exit(1)
    if "--test" in sys.argv:
        ok = tg_text("✅ MEXC wedge scanner is connected to Telegram.")
        sys.exit(0 if ok else 1)

    t0 = time.time()
    state = load_state()
    syms = get_symbols()
    if not syms:
        print("No symbols returned from MEXC")
        sys.exit(1)
    print(f"Scanning {len(syms)} perpetual pairs on {TIMEFRAMES} ...")

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(safe_scan, syms))
    sigs = [s for r in results for s in r]
    new = [s for s in sigs if s["key"] not in state]
    new.sort(key=lambda s: -TIMEFRAMES.index(s["tf"]))
    print(f"Signals: {len(sigs)} | new: {len(new)} | took {time.time() - t0:.0f}s")

    for s in new[:MAX_ALERTS]:
        try:
            ok = tg_photo(make_chart(s), caption(s))
        except Exception as e:
            print(f"[chart error] {s['sym']} {s['tf']}: {e}")
            ok = tg_text(caption(s))
        if ok:
            state[s["key"]] = int(time.time())
        time.sleep(1.2)
    save_state(state)


if __name__ == "__main__":
    main()
