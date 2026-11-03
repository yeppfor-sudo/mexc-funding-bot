import os, csv, statistics as st, time
import backtest as b          # reuses data loading + Telegram from backtest.py (keep both files in the repo)

DAYS = int(os.getenv("DAYS", "30"))
HOLDS = [1, 3, 5, 10, 15]     # minutes to hold the reversal trade
DELAYS = [0, 1]               # minutes after funding before entering (0 = right at settlement)
STOPS = [0.01, 0.02]          # price stop for the reversal trade
SLIPS = [0.0005, 0.0015, 0.003]
LIQ_NOTIONAL = 3000           # coin must trade >= VOL_MULT x this in the 5 min before funding
SL_EXTRA = 0.003              # extra slippage when the stop is hit
TOP_TRY = 5


def kl(sym, T):
    j = b.get(f"{b.BASE}/kline/{sym}", {"interval": "Min1", "start": int(T - 300), "end": int(T + 1260)})
    d = ((j or {}).get("data")) or {}
    out = {}
    for t, o, h, l, c, v in zip(d.get("time", []), d.get("open", []), d.get("high", []),
                                d.get("low", []), d.get("close", []), d.get("vol", [])):
        out[int(t)] = (float(o), float(h), float(l), float(c), float(v))
    return out


def pick_events(slots, cs):
    """The coin the funding bot would have traded in each slot (highest rate that is liquid)."""
    chosen = []
    for T in sorted(slots):
        for ev in sorted(slots[T], key=lambda e: -abs(e["rate"]))[:TOP_TRY]:
            k = kl(ev["sym"], T)
            if not k.get(T - 60) or not k.get(T):
                continue
            turn = sum(v * cs[ev["sym"]] * c for (o, h, l, c, v) in (k[t] for t in k if t < T))
            if turn < b.VOL_MULT * LIQ_NOTIONAL:
                continue
            chosen.append((ev, k))
            break
    return chosen


def sim(k, T, rate, delay, hold, stop, slip):
    """Reversal: LONG after positive funding (we were short, price usually dumped), SHORT after negative."""
    s = 1 if rate > 0 else -1
    te = T + 60 * delay
    c = k.get(te)
    if not c:
        return None
    entry = c[0] * (1 + s * slip)
    last = None
    for i in range(hold):
        cd = k.get(te + 60 * i)
        if not cd:
            return None
        adverse = cd[2] if s == 1 else cd[1]
        if s * (adverse - entry) / entry <= -stop:
            return -(stop + SL_EXTRA) - 2 * b.FEE_SIDE
        last = cd
    return s * (last[3] * (1 - s * slip) - entry) / entry - 2 * b.FEE_SIDE


def summarize(rets):
    n = len(rets)
    mean = sum(rets) / n
    sd = st.pstdev(rets) if n > 1 else 0
    return dict(n=n, mean=mean, med=st.median(rets), win=sum(1 for r in rets if r > 0) / n,
                t=(mean / (sd / n ** 0.5)) if sd > 0 else 0, worst=min(rets))


def main():
    since = time.time() - DAYS * 86400
    j = b.get(f"{b.BASE}/detail") or {}
    cs = {d["symbol"]: float(d.get("contractSize") or 1) for d in (j.get("data") or [])
          if str(d.get("symbol", "")).endswith("_USDT")}
    events, failed = b.load_events(cs, since * 1000)
    slots = {}
    for evs in events.values():
        for e in evs:
            slots.setdefault(e["T"], []).append(e)
    chosen = pick_events(slots, cs)
    print(f"{len(chosen)} funding events usable (of {len(slots)} slots), {len(failed)} symbols failed to load")
    if len(chosen) < 10:
        b.tg("Reversal test: too few events to conclude anything.")
        return

    rows = []
    for delay in DELAYS:
        for hold in HOLDS:
            for stop in STOPS:
                row = dict(delay=delay, hold=hold, stop=stop)
                for slip in SLIPS:
                    rets = []
                    for ev, k in chosen:
                        r = sim(k, ev["T"], ev["rate"], delay, hold, stop, slip)
                        if r is not None:
                            rets.append((ev["T"], ev["rate"], r))
                    row[f"s{slip}"] = rets
                rows.append(row)

    with open("reversal_results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["delay_min", "hold_min", "stop", "slip", "n", "avg_net_pct", "median_pct", "win_pct", "t_stat", "worst_pct"])
        for row in rows:
            for slip in SLIPS:
                rets = [r for _, _, r in row[f"s{slip}"]]
                if rets:
                    m = summarize(rets)
                    w.writerow([row["delay"], row["hold"], row["stop"], slip, m["n"], round(m["mean"] * 100, 3),
                                round(m["med"] * 100, 3), round(m["win"] * 100, 1), round(m["t"], 2), round(m["worst"] * 100, 2)])

    mid = f"s{SLIPS[1]}"
    hi = f"s{SLIPS[2]}"
    ranked = sorted([r for r in rows if r[mid]], key=lambda r: -summarize([x for _, _, x in r[mid]])["mean"])
    lines = []
    for r in ranked[:5]:
        m = summarize([x for _, _, x in r[mid]])
        m3 = summarize([x for _, _, x in r[hi]])
        ts = sorted(r[mid])
        cut = len(ts) * 2 // 3
        a = [x for _, _, x in ts[:cut]]
        c = [x for _, _, x in ts[cut:]]
        first = sum(a) / len(a) * 100 if a else 0
        last = sum(c) / len(c) * 100 if c else 0
        lines.append(f"enter +{r['delay']}m hold {r['hold']}m stop {r['stop']*100:.0f}%: n={m['n']} avg {m['mean']*100:+.2f}% "
                     f"(~{m['mean']*5*0.99*100:+.1f}% wallet @5x) win {m['win']*100:.0f}% t={m['t']:.1f} | 0.30% slip: {m3['mean']*100:+.2f}% | "
                     f"first 2/3 {first:+.2f}% vs last 1/3 {last:+.2f}%")
    best = ranked[0]
    bm, bm3 = summarize([x for _, _, x in best[mid]]), summarize([x for _, _, x in best[hi]])
    ts = sorted(best[mid]); cut = len(ts) * 2 // 3
    stable = sum(x for _, _, x in ts[:cut]) > 0 and sum(x for _, _, x in ts[cut:]) > 0
    verdict = ("POSSIBLE EDGE - still needs a small live test" if (bm3["mean"] > 0 and bm["t"] >= 2 and stable)
               else "NO RELIABLE EDGE in this data - do not trade it")
    pos = [x for _, rt, x in best[mid] if rt > 0]
    neg = [x for _, rt, x in best[mid] if rt < 0]
    b.tg(f"🔁 REVERSAL TEST {DAYS}d | {len(chosen)} events | long after +funding, short after -funding\n"
         f"Costs: fee {b.FEE_SIDE*100:.2f}%/side + slippage; ranked at 0.15% slip\n\n" + "\n".join(lines) +
         f"\n\nBest config by side: after +funding {sum(pos)/len(pos)*100 if pos else 0:+.2f}% (n={len(pos)}), "
         f"after -funding {sum(neg)/len(neg)*100 if neg else 0:+.2f}% (n={len(neg)})\n"
         f"VERDICT: {verdict}\n"
         f"Note: picking the best of {len(rows)} configs overstates results; trust it only if 0.30% slip and the first/last split stay positive.")


if __name__ == "__main__":
    main()
