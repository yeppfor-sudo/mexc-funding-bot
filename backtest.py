#!/usr/bin/env python3
"""
30-day backtest of the funding sniper on REAL MEXC perpetual data (public endpoints, no API key).

Data:   /funding_rate/history (settled rates for every pair) + 1-min klines around each settlement.
Rules:  same as bot.py -> highest |rate| >= MIN_RATE per slot, 70% at T-60s candle open (5x),
        funding received at T, DCA 29% if price goes DCA_TRIGGER% against avg, exit at avg (+fee buffer),
        SL at SL% from current avg, compounding, one trade at a time.
Honest limits are printed at the end of the summary.
"""
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://contract.mexc.com"


def _f(n, d):
    return float(os.getenv(n, d))


DAYS = int(_f("DAYS", "30"))
START_EQ = _f("START_EQUITY", "100")
LEV = _f("LEVERAGE", "5")
ENTRY_PCT = _f("ENTRY_PCT", "70") / 100
DCA_PCT = _f("DCA_PCT", "29") / 100
MIN_RATE = _f("MIN_RATE_PCT", "0.5") / 100
DCA_TRIG = _f("DCA_TRIGGER_PCT", "1.0")
SL = _f("SL_PRICE_PCT", "3.0")
FEE = _f("TAKER_FEE_PCT", "0.02") / 100
SLIP = _f("SLIPPAGE_PCT", "0.05") / 100        # per fill (no historical order book available)
SLIP_SL = _f("SL_SLIPPAGE_PCT", "0.15") / 100  # stop fills are worse
HOLD_H = _f("MAX_HOLD_HOURS", "12")            # backtest cap; forced market close after this
LIQ_MULT = _f("LIQUIDITY_MULT", "3")           # entry-window turnover must be >= mult x position notional
CYCLES = {int(x) for x in os.getenv("FUNDING_CYCLES_HOURS", "4").split(",")}
OUT = os.getenv("OUT_DIR", "results")

S = requests.Session()


def get(path, params=None):
    for i in range(6):
        try:
            j = S.get(BASE + path, params=params, timeout=20).json()
            if j.get("success"):
                return j["data"]
        except Exception:
            pass
        time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"failed: {path} {params}")


def history(sym, start_ms):
    out, page = [], 1
    while True:
        d = get("/api/v1/contract/funding_rate/history", {"symbol": sym, "page_num": page, "page_size": 100})
        rows = d.get("resultList") or []
        for r in rows:
            if int(r["settleTime"]) >= start_ms and int(r.get("collectCycle", 8)) in CYCLES:
                out.append((int(r["settleTime"]), float(r["fundingRate"])))
        if not rows or min(int(r["settleTime"]) for r in rows) < start_ms or page >= int(d.get("totalPage", 1)):
            return out
        page += 1


def simulate(rate, settle_ms, eq, k):
    t, o, h, l, c = k["time"], k["open"], k["high"], k["low"], k["close"]
    s = settle_ms // 1000
    if (s - 60) not in t:
        return None
    i0 = t.index(s - 60)
    if i0 + 1 >= len(t) or t[i0 + 1] != s:
        return None
    long = rate < 0
    d = 1 if long else -1
    n0 = eq * ENTRY_PCT * LEV
    amt = k.get("amount") or []
    if amt and sum(amt[i0:i0 + 2]) < LIQ_MULT * eq * (ENTRY_PCT + DCA_PCT) * LEV:
        return "illiquid"
    ep = o[i0] * (1 + d * SLIP)
    st = {"q": n0 / ep, "avg": ep, "fees": n0 * FEE, "fund": 0.0, "dca": False}

    def adverse(i):
        return (st["avg"] - l[i]) / st["avg"] if long else (h[i] - st["avg"]) / st["avg"]

    def finish(px, i, reason, slip):
        xp = px * (1 - d * slip)
        price_pnl = d * (xp - st["avg"]) * st["q"]
        fees = st["fees"] + st["q"] * xp * FEE
        net = price_pnl + st["fund"] - fees
        return {"side": "LONG" if long else "SHORT", "entry": round(ep, 8), "avg": round(st["avg"], 8),
                "exit": round(xp, 8), "dca": st["dca"], "reason": reason, "funding": st["fund"],
                "price_pnl": price_pnl, "fees": fees, "net": net, "ret_pct": net / eq * 100,
                "hold_min": (t[i] + 60 - (s - 60)) / 60, "end_ms": (t[i] + 60) * 1000}

    # pre-funding candle (entry candle): stop can hit before funding is received
    if adverse(i0) >= SL / 100:
        return finish(st["avg"] * (1 - d * SL / 100), i0, "SL before funding", SLIP_SL)
    last = min(len(t) - 1, i0 + 1 + int(HOLD_H * 60))
    for i in range(i0 + 1, last + 1):
        if i == i0 + 1:
            st["fund"] = abs(rate) * st["q"] * o[i]
        target = st["avg"] * (1 + d * 2 * FEE)
        if (o[i] >= target) if long else (o[i] <= target):
            return finish(o[i], i, "exit at open (profit/avg)", SLIP)
        adv = adverse(i)
        if adv >= SL / 100:
            return finish(st["avg"] * (1 - d * SL / 100), i, "STOP-LOSS", SLIP_SL)
        if not st["dca"] and adv >= DCA_TRIG / 100:
            px = st["avg"] * (1 - d * DCA_TRIG / 100) * (1 + d * SLIP)
            nd = eq * DCA_PCT * LEV
            qd = nd / px
            st["avg"] = (st["avg"] * st["q"] + px * qd) / (st["q"] + qd)
            st["q"] += qd
            st["fees"] += nd * FEE
            st["dca"] = True
            if adverse(i) >= SL / 100:  # pessimistic: same candle also hits the new stop
                return finish(st["avg"] * (1 - d * SL / 100), i, "STOP-LOSS after DCA", SLIP_SL)
            continue  # pessimistic: no exit in the DCA candle
        if (h[i] >= target) if long else (l[i] <= target):
            return finish(target, i, "exit at avg", SLIP)
    return finish(c[last], last, "timeout market close", SLIP)


def main():
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - DAYS * 86400 * 1000
    syms = [d["symbol"] for d in get("/api/v1/contract/detail")
            if d["symbol"].endswith("_USDT") and d.get("state") == 0]
    print(f"{len(syms)} perpetuals, fetching funding history for {DAYS} days...")
    with ThreadPoolExecutor(8) as ex:
        hist = list(ex.map(lambda s: (s, history(s, start_ms)), syms))
    slots = {}
    for sym, rows in hist:
        for ms, r in rows:
            if abs(r) >= MIN_RATE:
                slots.setdefault(ms, []).append((abs(r), sym, r))
    print(f"{len(slots)} funding slots with at least one pair >= {MIN_RATE*100:.2f}%")

    eq, peak, max_dd, busy = START_EQ, START_EQ, 0.0, 0
    trades, curve, skipped_busy, skipped_nodata = [], [(start_ms, START_EQ)], 0, 0
    for ms in sorted(slots):
        if ms <= busy:
            skipped_busy += 1
            continue
        res = None
        for _, sym, r in sorted(slots[ms], reverse=True)[:5]:
            k = get(f"/api/v1/contract/kline/{sym}",
                    {"interval": "Min1", "start": ms // 1000 - 60, "end": ms // 1000 + int(HOLD_H * 3600) + 60})
            res = simulate(r, ms, eq, k)
            if isinstance(res, dict):
                res.update({"settle_ms": ms, "symbol": sym, "rate_pct": r * 100, "equity_before": eq})
                break
        if not isinstance(res, dict):
            skipped_nodata += 1
            continue
        eq += res["net"]
        busy = res["end_ms"]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
        res["equity_after"] = eq
        trades.append(res)
        curve.append((busy, eq))
        if eq <= 0:
            print("Account wiped out in the backtest.")
            break

    n = len(trades)
    wins = sum(1 for x in trades if x["net"] > 0)
    summ = {
        "days": DAYS, "start_equity": START_EQ, "final_equity": round(eq, 2),
        "return_pct": round((eq / START_EQ - 1) * 100, 2), "trades": n,
        "win_rate_pct": round(wins / n * 100, 1) if n else 0,
        "stop_losses": sum(1 for x in trades if "STOP" in x["reason"] or "SL" in x["reason"]),
        "dca_used": sum(1 for x in trades if x["dca"]),
        "timeouts": sum(1 for x in trades if x["reason"].startswith("timeout")),
        "funding_total": round(sum(x["funding"] for x in trades), 2),
        "price_pnl_total": round(sum(x["price_pnl"] for x in trades), 2),
        "fees_total": round(-sum(x["fees"] for x in trades), 2),
        "max_drawdown_pct": round(max_dd, 1),
        "worst_trade_pct": round(min((x["ret_pct"] for x in trades), default=0), 1),
        "avg_hold_min": round(sum(x["hold_min"] for x in trades) / n, 1) if n else 0,
        "slots_skipped_busy": skipped_busy, "slots_skipped_no_data_or_illiquid": skipped_nodata,
    }
    os.makedirs(OUT, exist_ok=True)
    if trades:
        with open(f"{OUT}/trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
    json.dump(summ, open(f"{OUT}/summary.json", "w"), indent=2)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.figure(figsize=(9, 4))
        plt.plot([c[0] / 1000 / 86400 for c in curve], [c[1] for c in curve])
        plt.title(f"Funding sniper equity, last {DAYS}d"); plt.ylabel("USDT"); plt.grid(alpha=.3)
        plt.savefig(f"{OUT}/equity.png", dpi=110, bbox_inches="tight")
    except Exception as e:
        print("chart skipped:", e)

    text = ("BACKTEST (real MEXC data, last %dd)\n" % DAYS) + "\n".join(f"{k}: {v}" for k, v in summ.items()) + (
        "\n\nLimits: settled funding rate used as the rate seen at T-50s (optimistic); order book not "
        "historical (turnover filter + slippage assumption); 1-min candles, pessimistic intrabar order.")
    print(text)
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text}, timeout=15)
            for fn in ("equity.png", "trades.csv"):
                p = f"{OUT}/{fn}"
                if os.path.exists(p):
                    api = "sendPhoto" if fn.endswith("png") else "sendDocument"
                    key = "photo" if fn.endswith("png") else "document"
                    requests.post(f"https://api.telegram.org/bot{tok}/{api}", data={"chat_id": chat},
                                  files={key: open(p, "rb")}, timeout=30)
        except Exception as e:
            print("telegram failed:", e)


if __name__ == "__main__":
    main()
