import os
import json
import time
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = "https://contract.mexc.com/api/v1/contract"
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = "state_base.json"

# Non-crypto contracts to skip (stocks, ETFs, metals, etc.)
EXCLUDE = {
    "XAU", "XAG", "GOLD", "SILVER", "OIL", "TSLA", "AAPL", "NVDA", "MSFT",
    "GOOGL", "AMZN", "META", "COIN", "MSTR", "SPY", "QQQ", "NDAQ", "CRCL",
    "HOOD", "PLTR", "AMD", "NFLX", "BABA", "SPX", "NAS100", "US30",
}

MAX_CONS = 8
MIN_CONS = 3
IMPULSE_ATR_MULT = 2.0
EARLY_ZONE = 0.02       # early alert when within 2% under the line
EARLY_REPEAT_DAYS = 3

S = requests.Session()


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1 + i)
    return None


def get_symbols():
    j = get(f"{BASE}/detail")
    out = []
    if not j or "data" not in j:
        return out
    for c in j["data"]:
        sym = c.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        if c.get("quoteCoin") != "USDT" or c.get("state") != 0:
            continue
        base = sym.replace("_USDT", "")
        if base.upper() in EXCLUDE:
            continue
        out.append(sym)
    return out


def get_klines(sym):
    end = int(time.time())
    start = end - 150 * 86400
    j = get(f"{BASE}/kline/{sym}",
            {"interval": "Day1", "start": start, "end": end})
    if not j or not j.get("data") or not j["data"].get("time"):
        return None
    d = j["data"]
    df = pd.DataFrame({
        "time": d["time"], "open": d["open"], "high": d["high"],
        "low": d["low"], "close": d["close"],
    })
    df = df.astype(float)
    return df


def add_ind(df):
    df["e6"] = df["close"].ewm(span=6, adjust=False).mean()
    df["e12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["e20"] = df["close"].ewm(span=20, adjust=False).mean()
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df


def build_line(h, idxs):
    """Line through the two highest highs of the consolidation."""
    hs = h[idxs]
    order = np.argsort(-hs)
    a = order[0]
    b = None
    for j in order[1:]:
        if abs(j - a) >= 2:
            b = j
            break
    if b is None:
        return None
    x1, x2 = sorted([a, b])
    y1, y2 = hs[x1], hs[x2]
    slope = (y2 - y1) / (x2 - x1)
    if not (-0.03 <= slope / y1 <= 0.005):
        return None
    gx1 = idxs[0] + x1
    # all highs must sit under the line (1% tolerance)
    for k, idx in enumerate(idxs):
        lv = y1 + slope * (k - x1)
        if h[idx] > lv * 1.01:
            return None
    return gx1, y1, slope


def detect(df):
    n = len(df)
    if n < 50:
        return None
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    e6, e12, e20 = df["e6"].values, df["e12"].values, df["e20"].values
    atr = df["atr"].values
    last = n - 1

    for mode in ("breakout", "early"):
        end = last - 1 if mode == "breakout" else last
        for k in range(end - MIN_CONS, end - MAX_CONS - 1, -1):
            if k < 20 or np.isnan(atr[k - 1]):
                continue
            body = c[k] - o[k]
            if body <= 0 or body < IMPULSE_ATR_MULT * atr[k - 1]:
                continue
            if c[k] <= e20[k]:
                continue
            # base before the impulse: EMAs tight
            top = max(e6[k - 1], e12[k - 1], e20[k - 1])
            bot = min(e6[k - 1], e12[k - 1], e20[k - 1])
            if (top - bot) / c[k - 1] > 0.08:
                continue
            idxs = list(range(k + 1, end + 1))
            bodies = np.abs(c[idxs] - o[idxs])
            if bodies.max() >= 0.6 * body:
                continue
            if h[idxs].max() - l[idxs].min() >= (h[k] - l[k]):
                continue
            if (c[idxs] < e20[idxs] * 0.98).any():
                continue
            if e20[end] < e20[k]:
                continue
            res = build_line(h, np.array(idxs))
            if res is None:
                continue
            gx1, y1, slope = res

            def line(t):
                return y1 + slope * (t - gx1)

            lv = line(last)
            sl = float(l[idxs].min())
            if mode == "breakout":
                if not (c[last] > lv and c[last - 1] <= line(last - 1)
                        and c[last] > o[last] and c[last] <= lv * 1.12):
                    continue
                entry = float(c[last])
            else:
                if not (lv * (1 - EARLY_ZONE) <= c[last] <= lv):
                    continue
                entry = float(lv)
            risk = entry - sl
            if risk <= 0 or risk / entry > 0.25:
                continue
            return {
                "mode": mode, "k": k, "end": end, "gx1": gx1, "y1": y1,
                "slope": slope, "entry": entry, "sl": sl,
                "tp1": entry + 2 * risk, "tp2": entry + 3 * risk,
                "imp_ts": int(df["time"].iloc[k]),
            }
    return None


def chart(df, sym, sig, path):
    d = df.tail(70).reset_index(drop=True)
    off = len(df) - len(d)
    fig, ax = plt.subplots(figsize=(10, 6), dpi=110)
    for i, r in d.iterrows():
        col = "#16a34a" if r["close"] >= r["open"] else "#dc2626"
        ax.plot([i, i], [r["low"], r["high"]], color=col, lw=1)
        lo = min(r["open"], r["close"])
        hi = max(r["open"], r["close"])
        ax.add_patch(plt.Rectangle((i - 0.3, lo), 0.6, max(hi - lo, 1e-12),
                                   color=col))
    ax.plot(d["e6"], color="#d946ef", lw=1.2, label="EMA6")
    ax.plot(d["e12"], color="#f59e0b", lw=1.2, label="EMA12")
    ax.plot(d["e20"], color="#6366f1", lw=1.2, label="EMA20")
    x0 = sig["gx1"] - off
    x1 = len(d) - 1
    ax.plot([x0, x1],
            [sig["y1"], sig["y1"] + sig["slope"] * (x1 + off - sig["gx1"])],
            color="black", lw=2, label="Resistance")
    ax.axvspan(sig["k"] - off + 0.5, sig["end"] - off + 0.5,
               color="gray", alpha=0.15)
    for name, val, col in (("Entry", sig["entry"], "#2563eb"),
                           ("SL", sig["sl"], "#dc2626"),
                           ("TP1", sig["tp1"], "#16a34a"),
                           ("TP2", sig["tp2"], "#15803d")):
        ax.axhline(val, color=col, ls="--", lw=1)
        ax.text(0, val, f" {name} {val:.6g}", color=col, va="bottom",
                fontsize=8)
    tag = "BREAKOUT CLOSE" if sig["mode"] == "breakout" else "EARLY"
    ax.set_title(f"{sym} 1D - {tag}")
    ax.legend(loc="upper left", fontsize=8)
    ax.set_xlim(-1, len(d) + 1)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def tg_text(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  data={"chat_id": TG_CHAT, "text": text}, timeout=30)


def tg_photo(path, caption):
    if not TG_TOKEN or not TG_CHAT:
        return
    with open(path, "rb") as f:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                      data={"chat_id": TG_CHAT, "caption": caption},
                      files={"photo": f}, timeout=60)


def main():
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE))
        except Exception:
            state = {}
    now = int(time.time())
    syms = get_symbols()
    print("pairs:", len(syms))
    found = []

    for sym in syms:
        time.sleep(0.12)
        df = get_klines(sym)
        if df is None or len(df) < 50:
            continue
        live = float(df["close"].iloc[-1])   # current open candle
        df = df.iloc[:-1].reset_index(drop=True)  # closed candles only
        df = add_ind(df)
        sig = detect(df)
        if not sig:
            continue
        if live < sig["sl"]:
            continue  # stale: already below stop
        st = state.get(sym, {})
        if sig["mode"] == "breakout":
            if st.get("breakout") == sig["imp_ts"]:
                continue
        else:
            if st.get("breakout") == sig["imp_ts"]:
                continue
            if now - st.get("early_ts", 0) < EARLY_REPEAT_DAYS * 86400:
                continue
        found.append((sym, sig, df))

    if not found:
        tg_text("1D base-breakout scan: no setups today.")
    else:
        lines = ["1D base-breakout scan:"]
        for sym, sig, _ in found:
            tag = "BREAKOUT" if sig["mode"] == "breakout" else "EARLY"
            lines.append(f"{tag}  {sym}")
        tg_text("\n".join(lines))

    for sym, sig, df in found:
        path = f"/tmp/{sym}.png"
        chart(df, sym, sig, path)
        tag = "BREAKOUT CLOSE" if sig["mode"] == "breakout" else "EARLY"
        cap = (f"{sym} 1D {tag} (long)\n"
               f"Entry: {sig['entry']:.6g}\nSL: {sig['sl']:.6g}\n"
               f"TP1: {sig['tp1']:.6g}\nTP2: {sig['tp2']:.6g}")
        tg_photo(path, cap)
        st = state.setdefault(sym, {})
        if sig["mode"] == "breakout":
            st["breakout"] = sig["imp_ts"]
        else:
            st["early_ts"] = now

    json.dump(state, open(STATE_FILE, "w"))


if __name__ == "__main__":
    main()
