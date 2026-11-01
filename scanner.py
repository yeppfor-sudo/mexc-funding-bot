#!/usr/bin/env python3
"""
MEXC perpetual scanner: bearish CHoCH on the 15m timeframe -> Telegram alerts.

- Scans every active USDT-margined perpetual on MEXC (public data only, no API keys).
- Detects a bearish Change of Character (CHoCH) on the last closed 15m candle(s).
- Optional filters: 48h gain (min % and/or top N gainers), include/exclude majors.
- Sends a chart + levels (short zone, SL, TP) to Telegram. De-duplicates alerts.

This tool only sends ALERTS. It does not place orders.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ccxt
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("CHAT_ID")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Karachi"))
HIST_FILE = os.getenv("HIST_FILE", "alerts_history.json")

TIMEFRAME = "15m"
BARS_48H = 48 * 4  # 15m bars in 48h
CANDLE_LIMIT = 500

# Non-crypto / stablecoin / leveraged-ETF style tickers that MEXC lists as perps
NON_CRYPTO = {
    "TLT", "TLH", "IEF", "SHY", "INDA", "EWJ", "EWA", "EWZ", "KWEB", "QQQ", "SPY", "DIA",
    "IWM", "EWW", "EWC", "MCHI", "INDY", "STRC", "OURA", "SPX", "SPX500", "NAS100", "DJI",
    "US30", "UK100", "GER40", "RKLB", "NVDA", "AAPL", "TSLA", "MSFT", "STOCK", "ETF", "OIL",
    "SILVER", "GOLD", "XAU", "XAG", "XAUT", "PAXG", "KAU", "TRY", "EUR", "GBP", "JPY", "AUD",
    "CAD", "CHF", "NZD", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDE", "USDP", "GUSD",
    "PYUSD", "EURI", "EURC", "XLU", "XLP", "XLE", "XLF", "XLB", "XLI", "XLY", "XLK", "XLV",
    "GLD", "SLV", "GDX", "GDXJ", "COPX", "LIT", "URA", "SQQQ", "TQQQ", "SOXS", "SOXL",
    "SPXS", "SPXL", "SKDD", "XSOXS", "XSOX", "LABU", "LABD", "TECL", "TECS", "UPRO", "SPXU",
    "TZA", "TNA", "FAS", "FAZ", "SDOW", "UDOW", "LEAD", "COPPER", "NICKEL", "ZINC", "TIN",
    "ALUM",
}
MAJORS = {
    "BTC", "ETH", "SOL", "BNB", "TRX", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK", "LTC",
    "BCH", "XLM", "ETC", "HBAR", "APT", "NEAR", "UNI", "FIL", "ARB", "OP", "ATOM", "MATIC",
    "POL", "TON", "SHIB", "PEPE", "SUI", "TAO", "FET", "WIF", "ENA", "JUP", "SEI", "TIA",
}


# --------------------------------------------------------------------------- utils
def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def tg_message(text, dry):
    if dry or not TOKEN or not CHAT:
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
    except Exception as e:
        log(f"telegram message failed: {e}")


def tg_photo(path, caption, dry):
    if dry or not TOKEN or not CHAT:
        print(f"[dry-run] chart saved at {path}\n{caption}")
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                data={"chat_id": CHAT, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=40,
            )
        return r.ok
    except Exception as e:
        log(f"telegram photo failed: {e}")
        return False


def load_hist():
    try:
        with open(HIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_hist(d):
    with open(HIST_FILE, "w") as f:
        json.dump(d, f)


# --------------------------------------------------------------------------- markets
def mexc_perps(ex, include_majors):
    out = []
    for sym, m in ex.markets.items():
        if not (m.get("swap") and m.get("linear") and m.get("settle") == "USDT"):
            continue
        if m.get("active") is False:
            continue
        base = str(m.get("base", "")).upper()
        if not base or base in NON_CRYPTO:
            continue
        if not include_majors and base in MAJORS:
            continue
        out.append(sym)
    return sorted(out)


def fetch_df(ex, sym):
    try:
        o = ex.fetch_ohlcv(sym, TIMEFRAME, limit=CANDLE_LIMIT)
    except Exception as e:
        log(f"skip {sym}: {e}")
        return None
    if not o or len(o) < 120:
        return None
    df = pd.DataFrame(o, columns=["t", "o", "h", "l", "c", "v"])
    if df["v"].tail(8).sum() == 0:  # dead / illiquid market
        return None
    return df


def gain_48h(df):
    """Gain over the last 48h measured on closed 15m candles."""
    if len(df) < BARS_48H + 3:
        return None
    now_c = float(df["c"].iloc[-2])
    old_c = float(df["c"].iloc[-2 - BARS_48H])
    return (now_c / old_c - 1) * 100 if old_c > 0 else None


# --------------------------------------------------------------------------- signal
def find_bearish_choch_events(df, length):
    """
    All bearish CHoCH events in `df` (the final, live candle is ignored).

    Bearish CHoCH = in an up-structure (last break was a close above a pivot high),
    price CLOSES below the latest pivot low.
    Pivots are only used once confirmed (`length` bars later), so there is no
    lookahead / repainting.
    """
    h, lo, c = df["h"].values, df["l"].values, df["c"].values
    n = len(df)
    upper = lower = None
    up_broken = low_broken = False
    direction = 0  # 1 = bullish structure, -1 = bearish
    events = []

    for i in range(2 * length, n - 1):  # n-1 is the live candle
        p = i - length  # pivot candidate, confirmed at bar i
        if h[p] == h[p - length : i + 1].max():
            upper, up_broken = float(h[p]), False
        if lo[p] == lo[p - length : i + 1].min():
            lower, low_broken = float(lo[p]), False
        if upper is None or lower is None:
            continue

        if not low_broken and c[i] < lower <= c[i - 1]:
            low_broken = True
            if direction == 1:
                low_val = float(lo[i])
                rng = upper - low_val
                events.append({
                    "bar": i,
                    "close": float(c[i]),
                    "break_level": lower,
                    "range_top": upper,
                    "range_bottom": low_val,
                    "eq": (upper + low_val) / 2,
                    "zone_top": upper - rng * 0.382,
                    "zone_bot": upper - rng * 0.618,
                })
            direction = -1
        if not up_broken and c[i] > upper >= c[i - 1]:
            up_broken = True
            direction = 1
    return events


def detect_bearish_choch(df, length, check_bars):
    """Latest bearish CHoCH if it printed on one of the last `check_bars` closed candles."""
    events = find_bearish_choch_events(df, length)
    n = len(df)
    if events and events[-1]["bar"] >= n - 1 - check_bars:
        return events[-1]
    return None


# --------------------------------------------------------------------------- chart
def draw_chart(df, sym, sig, gain, path):
    view = df.tail(90).reset_index(drop=True)
    offset = len(df) - len(view)
    fig, ax = plt.subplots(figsize=(13, 6), facecolor="black")
    ax.set_facecolor("black")
    up = view["c"] >= view["o"]
    cols = ["#089981" if u else "#f23645" for u in up]
    ax.vlines(view.index, view["l"], view["h"], colors=cols, linewidth=1)
    ax.vlines(view.index, view["o"], view["c"], colors=cols, linewidth=4)
    ax.axhspan(sig["range_bottom"], sig["range_top"], color="#f23645", alpha=0.10)
    ax.axhspan(sig["zone_bot"], sig["zone_top"], color="#f23645", alpha=0.40, label="Short zone (0.382-0.618)")
    ax.axhline(sig["break_level"], color="white", ls="--", lw=1.1, label=f"Broken low {sig['break_level']:.6g}")
    ax.axhline(sig["range_top"], color="gray", ls=":", lw=1, label=f"SL / range top {sig['range_top']:.6g}")
    ax.axhline(sig["eq"], color="#f23645", ls="--", lw=0.9, label="EQ")
    ax.scatter(sig["bar"] - offset, sig["close"], color="white", s=140, marker="v", zorder=5)
    gtxt = f" | 48h {gain:+.1f}%" if gain is not None else ""
    ax.set_title(f"{sym} MEXC 15m  BEARISH CHoCH{gtxt}", color="white", fontweight="bold")
    ax.tick_params(colors="white")
    ax.legend(facecolor="black", labelcolor="white", fontsize=7, loc="upper left")
    ax.grid(alpha=0.15)
    plt.tight_layout()
    plt.savefig(path, dpi=130, facecolor="black")
    plt.close(fig)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="MEXC perp bearish CHoCH 15m scanner")
    ap.add_argument("--fractal", type=int, default=int(os.getenv("FRACTAL_LEN", 7)), help="pivot length")
    ap.add_argument("--check-bars", type=int, default=2, help="closed candles back to accept a signal")
    ap.add_argument("--min-gain", type=float, default=None, help="only pairs up >= X%% over 48h")
    ap.add_argument("--top", type=int, default=None, help="only the top N 48h gainers")
    ap.add_argument("--include-majors", action="store_true", help="also scan BTC/ETH/SOL etc.")
    ap.add_argument("--cooldown", type=float, default=2.0, help="hours before re-alerting same pair")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    args = ap.parse_args()

    if not args.dry_run and not (TOKEN and CHAT):
        log("TELEGRAM_TOKEN / CHAT_ID not set -> running in dry-run mode")
        args.dry_run = True

    ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    syms = mexc_perps(ex, args.include_majors)
    log(f"{len(syms)} MEXC USDT perpetuals to scan")

    cands = []
    for s in syms:
        df = fetch_df(ex, s)
        if df is None:
            continue
        cands.append((s, df, gain_48h(df)))

    if args.min_gain is not None:
        cands = [x for x in cands if x[2] is not None and x[2] >= args.min_gain]
    if args.top:
        cands = sorted([x for x in cands if x[2] is not None], key=lambda x: x[2], reverse=True)[: args.top]
    log(f"{len(cands)} pairs after filters")

    hist = load_hist()
    found = 0
    for sym, df, gain in cands:
        sig = detect_bearish_choch(df, args.fractal, args.check_bars)
        if not sig:
            continue
        bar_ts = int(df["t"].iloc[sig["bar"]])
        prev = hist.get(sym, {})
        if prev.get("bar_ts") == bar_ts:
            continue
        if time.time() - prev.get("sent", 0) < args.cooldown * 3600:
            continue

        live = float(df["c"].iloc[-1])
        entry = (sig["zone_top"] + sig["zone_bot"]) / 2
        sl = sig["range_top"]
        risk = sl - entry
        tp1, tp2 = entry - risk, entry - 2 * risk
        closed_at = datetime.fromtimestamp(bar_ts / 1000, timezone.utc).astimezone(TZ) + timedelta(minutes=15)
        gtxt = f"{gain:+.1f}%" if gain is not None else "n/a"
        caption = (
            f"🔴 <b>BEARISH CHoCH 15m</b>\n\n"
            f"<b>Pair:</b> <code>{sym}</code> (MEXC perp)\n"
            f"<b>48h:</b> {gtxt}\n"
            f"<b>CHoCH close:</b> <code>{sig['close']:.6g}</code> | <b>Live:</b> <code>{live:.6g}</code>\n"
            f"<b>Broken low:</b> <code>{sig['break_level']:.6g}</code>\n"
            f"<b>Short zone:</b> <code>{sig['zone_bot']:.6g}</code> - <code>{sig['zone_top']:.6g}</code>\n"
            f"<b>SL:</b> <code>{sl:.6g}</code>\n"
            f"<b>TP1 / TP2 (1R / 2R):</b> <code>{tp1:.6g}</code> / <code>{tp2:.6g}</code>\n"
            f"<b>Candle close:</b> {closed_at.strftime('%Y-%m-%d %H:%M')} {TZ.key}\n\n"
            f"<i>Alert only, not financial advice.</i>"
        )
        os.makedirs("charts", exist_ok=True)
        path = f"charts/{sym.split('/')[0]}_{bar_ts}.png"
        draw_chart(df, sym, sig, gain, path)
        tg_photo(path, caption, args.dry_run)
        try:
            os.remove(path)
        except OSError:
            pass
        hist[sym] = {"bar_ts": bar_ts, "sent": time.time()}
        save_hist(hist)
        found += 1
        time.sleep(1.2)

    log(f"done - {found} signal(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
