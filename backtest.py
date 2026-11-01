#!/usr/bin/env python3
"""
Backtest of the bearish-CHoCH short strategy used by bot.py (MEXC perps, 15m).

Replicates the bot: signal on candle close -> limit SELL at the middle of the 0.382-0.618
retrace zone (cancels after N bars) -> SL at range top, TP at RR x risk -> risk-based sizing,
leverage cap, max open positions, daily loss halt, MEXC API fees (maker 0.01% / taker 0.05%).

Conservative candle rules (OHLC can't show intrabar order):
  * if SL and TP are both touched in one candle -> SL wins
  * no TP on the candle where the entry filled
  * SL exits pay taker fee + slippage; gaps through SL/TP fill at the open

Usage
  python backtest.py --days 180 --universe 80          # downloads + caches MEXC history
  python backtest.py --days 180 --universe 80 --sweep  # parameter grid (beware overfitting)
  python backtest.py --synthetic 40                    # random-walk sanity check (should NOT profit)
"""
import argparse
import os
import time

import numpy as np
import pandas as pd

import scanner as sc

MS15 = 900_000
BARS_48H = 192
FEE_MAKER, FEE_TAKER = 0.0001, 0.0005


# ------------------------------------------------------------------ data
def fetch_history(ex, sym, days, cache_dir, refresh=False):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, sym.replace("/", "_").replace(":", "_") + ".csv")
    if os.path.exists(path) and not refresh:
        return pd.read_csv(path)
    cur = int((time.time() - days * 86400) * 1000)
    rows = []
    while True:
        try:
            o = ex.fetch_ohlcv(sym, "15m", since=cur, limit=1000)
        except Exception as e:
            print(f"  {sym}: {e}")
            break
        if not o:
            break
        rows += o
        nxt = o[-1][0] + MS15
        if nxt <= cur or nxt > time.time() * 1000 - MS15:
            break
        cur = nxt
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"]).drop_duplicates("t").sort_values("t")
    df.to_csv(path, index=False)
    return df.reset_index(drop=True)


def synthetic(n_syms, days, seed=7):
    rng = np.random.default_rng(seed)
    n = days * 96
    out = {}
    for k in range(n_syms):
        vol = rng.uniform(0.004, 0.012)
        r = rng.normal(0, vol, n)
        c = 10 * np.exp(np.cumsum(r))
        o = np.concatenate([[c[0]], c[:-1]])
        wick = np.abs(rng.normal(0, vol / 2, n))
        h = np.maximum(o, c) * (1 + wick)
        l = np.minimum(o, c) * (1 - wick)
        t = (int(time.time() * 1000) - (n - np.arange(n)) * MS15).astype(np.int64)
        out[f"SYN{k}/USDT:USDT"] = pd.DataFrame({"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1.0})
    return out


# ------------------------------------------------------------------ per-symbol simulation
def simulate_symbol(sym, df, p):
    t, o, h, l, c = (df[k].values for k in ("t", "o", "h", "l", "c"))
    n = len(df)
    trades, busy = [], -1
    for ev in sc.find_bearish_choch_events(df, p.fractal):
        i = ev["bar"]
        if i <= busy:  # one position / pending order per symbol
            continue
        entry = (ev["zone_top"] + ev["zone_bot"]) / 2
        sl = ev["range_top"]
        dist = sl - entry
        if c[i] >= entry or dist <= 0 or dist / entry > p.max_sl_pct / 100:
            continue
        if p.min_gain is not None:
            if i < BARS_48H or (c[i] / c[i - BARS_48H] - 1) * 100 < p.min_gain:
                continue
        tp = entry - dist * p.rr

        fill = next((j for j in range(i + 1, min(i + 1 + p.expiry, n)) if h[j] >= entry), None)
        if fill is None:
            end = min(i + p.expiry, n - 1)
            trades.append({"sym": sym, "sig_ts": t[i], "exit_ts": t[end], "kind": "CANCEL",
                           "ret_pct": 0.0, "dist_pct": dist / entry})
            busy = end
            continue

        fp = max(entry, o[fill])
        kind, xp, xj = "OPEN", c[-1], n - 1
        for j in range(fill, n):
            if h[j] >= sl:
                kind, xp, xj = "SL", max(sl, o[j]), j
                break
            if j > fill and l[j] <= tp:
                kind, xp, xj = "TP", min(tp, o[j]), j
                break
        exit_fee = FEE_MAKER if kind == "TP" else FEE_TAKER + p.slippage
        ret = (fp - xp) / fp - FEE_MAKER - exit_fee
        trades.append({"sym": sym, "sig_ts": t[i], "fill_ts": t[fill], "exit_ts": t[xj], "kind": kind,
                       "entry": entry, "sl": sl, "tp": tp, "fill": fp, "exit": xp,
                       "ret_pct": ret, "dist_pct": dist / entry})
        busy = xj
    return trades


# ------------------------------------------------------------------ portfolio
def run_portfolio(trades, p):
    eq = p.equity
    active, taken, pnl_day = [], [], {}
    for tr in sorted(trades, key=lambda x: x["sig_ts"]):
        ts = tr["sig_ts"]
        still = []
        for a in active:
            if a["exit_ts"] <= ts:
                eq += a["pnl"]
                d = int(a["exit_ts"] // 86_400_000)
                pnl_day[d] = pnl_day.get(d, 0.0) + a["pnl"]
            else:
                still.append(a)
        active = still
        if len(active) >= p.max_positions:
            continue
        day = int(ts // 86_400_000)
        today = pnl_day.get(day, 0.0)
        if today <= -p.daily_loss_pct / 100 * (eq - today):
            continue
        risk_usd = eq * p.risk_pct / 100
        notional = min(risk_usd / tr["dist_pct"], eq * 0.9 / p.max_positions * p.leverage)
        pnl = notional * tr["ret_pct"]
        rec = dict(tr, risk_usd=risk_usd, notional=notional, pnl=pnl, equity_in=eq)
        active.append(rec)
        taken.append(rec)
    for a in active:
        eq += a["pnl"]
    curve = sorted(((r["exit_ts"], r["pnl"]) for r in taken), key=lambda x: x[0])
    return taken, eq, curve


def metrics(taken, curve, p, eq_end):
    done = [r for r in taken if r["kind"] != "CANCEL"]
    if not done:
        return None
    pnl = np.array([r["pnl"] for r in done])
    rr = np.array([r["pnl"] / r["risk_usd"] for r in done])
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    e, peak, mdd = p.equity, p.equity, 0.0
    for _, x in curve:
        e += x
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    return {
        "signals_taken": len(taken), "filled": len(done), "fill_rate": len(done) / len(taken),
        "win_rate": float((pnl > 0).mean()), "avg_R": float(rr.mean()),
        "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
        "return_pct": (eq_end / p.equity - 1) * 100, "max_dd_pct": mdd * 100,
        "tp": sum(r["kind"] == "TP" for r in done), "sl": sum(r["kind"] == "SL" for r in done),
    }


def fmt(m):
    return (f"filled {m['filled']:>5} ({m['fill_rate']*100:.0f}% of setups) | win {m['win_rate']*100:5.1f}% | "
            f"avgR {m['avg_R']:+.3f} | PF {m['profit_factor']:.2f} | return {m['return_pct']:+8.1f}% | "
            f"maxDD {m['max_dd_pct']:.1f}%")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Backtest bearish CHoCH short strategy (MEXC perps 15m)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--universe", type=int, default=80, help="top N perps by current 24h volume")
    ap.add_argument("--symbols", type=str, default=None, help="comma list e.g. DOGE/USDT:USDT,PEPE/USDT:USDT")
    ap.add_argument("--include-majors", action="store_true")
    ap.add_argument("--synthetic", type=int, default=0, help="run on N random-walk symbols (sanity check)")
    ap.add_argument("--refresh", action="store_true", help="re-download history")
    ap.add_argument("--cache", default="data")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--risk-pct", type=float, default=0.5)
    ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--max-positions", type=int, default=3)
    ap.add_argument("--max-sl-pct", type=float, default=8.0)
    ap.add_argument("--daily-loss-pct", type=float, default=3.0)
    ap.add_argument("--expiry", type=int, default=8, help="entry expiry in 15m bars")
    ap.add_argument("--fractal", type=int, default=7)
    ap.add_argument("--min-gain", type=float, default=None)
    ap.add_argument("--slippage", type=float, default=0.0005, help="extra cost on stop exits (fraction)")
    p = ap.parse_args()

    if p.synthetic:
        data = synthetic(p.synthetic, p.days)
    else:
        import ccxt
        ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        ex.load_markets()
        if p.symbols:
            syms = [s.strip() for s in p.symbols.split(",")]
        else:
            syms = sc.mexc_perps(ex, p.include_majors)
            try:
                tk = ex.fetch_tickers(syms)
                syms.sort(key=lambda s: -(tk.get(s, {}).get("quoteVolume") or 0))
            except Exception as e:
                print(f"volume sort failed: {e}")
            syms = syms[: p.universe]
        data = {}
        for k, s in enumerate(syms, 1):
            print(f"[{k}/{len(syms)}] {s}", flush=True)
            df = fetch_history(ex, s, p.days, p.cache, p.refresh)
            if df is not None and len(df) > 500:
                data[s] = df

    print(f"\n{len(data)} symbols, ~{p.days} days\n")
    grid = [(p.rr, p.fractal)]
    if p.sweep:
        grid = [(r, f) for r in (1.0, 1.5, 2.0, 3.0) for f in (5, 7, 10)]

    best = None
    for rr, fr in grid:
        q = argparse.Namespace(**{**vars(p), "rr": rr, "fractal": fr})
        trades = []
        for s, df in data.items():
            trades += simulate_symbol(s, df, q)
        taken, eq_end, curve = run_portfolio(trades, q)
        m = metrics(taken, curve, q, eq_end)
        label = f"RR {rr:<3} fractal {fr:<2}"
        print(f"{label} | " + (fmt(m) if m else "no trades"))
        if m and (best is None or m["return_pct"] > best[0] or not p.sweep):
            best = (m["return_pct"], q, taken, eq_end, curve, m)

    if not best:
        return
    _, q, taken, eq_end, curve, m = best
    done = [r for r in taken if r["kind"] != "CANCEL"]
    if done:
        mid = np.median([r["sig_ts"] for r in done])
        print("\nStability check (same parameters, two halves of the period):")
        for name, sub in (("first half ", [r for r in done if r["sig_ts"] <= mid]),
                          ("second half", [r for r in done if r["sig_ts"] > mid])):
            pnl = np.array([r["pnl"] / r["risk_usd"] for r in sub])
            print(f"  {name}: {len(sub):>4} trades | win {np.mean(pnl > 0)*100:.1f}% | avgR {pnl.mean():+.3f}")
        by_sym = {}
        for r in done:
            by_sym[r["sym"]] = by_sym.get(r["sym"], 0.0) + r["pnl"]
        top = sorted(by_sym.items(), key=lambda x: -x[1])
        pos_total = sum(v for _, v in top if v > 0)
        print(f"\nBest 3 symbols: {[(s.split('/')[0], round(float(v), 1)) for s, v in top[:3]]}")
        print(f"Worst 3 symbols: {[(s.split('/')[0], round(float(v), 1)) for s, v in top[-3:]]}")
        if pos_total > 0:
            print(f"Top-3 symbols = {sum(v for _, v in top[:3]) / pos_total * 100:.0f}% of all gross profit")
        pd.DataFrame(done).to_csv("backtest_trades.csv", index=False)
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            e, xs, ys = p.equity, [], []
            for ts, x in curve:
                e += x
                xs.append(pd.to_datetime(ts, unit="ms"))
                ys.append(e)
            plt.figure(figsize=(10, 4))
            plt.plot(xs, ys)
            plt.title(f"Equity (RR {q.rr}, fractal {q.fractal})")
            plt.grid(alpha=0.3)
            plt.tight_layout()
            plt.savefig("backtest_equity.png", dpi=120)
            print("\nSaved backtest_trades.csv and backtest_equity.png")
        except Exception as e:
            print(f"plot skipped: {e}")


if __name__ == "__main__":
    main()
