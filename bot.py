#!/usr/bin/env python3
"""
MEXC perpetual funding-fee sniper - FIXED for small balance $10
Fixes: depth [price,qty] not [price,qty,vol], relax 99.9% -> 80% check
"""
import hashlib
import hmac
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

def _f(n, d):
    return float(os.getenv(n, d))

def _b(n, d):
    return os.getenv(n, d).strip().lower() in ("1", "true", "yes", "on")

BASE = "https://contract.mexc.com"
KEY = os.getenv("MEXC_API_KEY", "")
SECRET = os.getenv("MEXC_API_SECRET", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

DRY_RUN = _b("DRY_RUN", "true")
SIM_BALANCE = _f("SIM_BALANCE", "10")
LEVERAGE = int(_f("LEVERAGE", "5"))
ENTRY_PCT = _f("ENTRY_PCT", "70") / 100
DCA_PCT = _f("DCA_PCT", "29") / 100
MIN_RATE = _f("MIN_RATE_PCT", "0.5") / 100
PRECHECK_S = _f("PRECHECK_SECONDS", "120")
ENTRY_S = _f("ENTRY_SECONDS_BEFORE", "50")
DCA_TRIGGER = _f("DCA_TRIGGER_PCT", "1.0")
SL_PCT = _f("SL_PRICE_PCT", "3.0")
HARD_SL_PCT = SL_PCT + 1.5
USE_HARD_SL = _b("USE_HARD_SL", "true")
FEE = _f("TAKER_FEE_PCT", "0.02") / 100
MAX_SLIP = _f("MAX_SLIPPAGE_PCT", "0.50")
CYCLES = {int(x) for x in os.getenv("FUNDING_CYCLES_HOURS", "4").split(",")}
MAX_HOLD_MIN = _f("MAX_HOLD_MINUTES", "0")
RUN_S = _f("RUN_SECONDS", "20700")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.info

def tg(msg):
    log(msg)
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
    except Exception:
        pass

class Mexc:
    def __init__(self):
        self.s = requests.Session()
        self.off = 0.0
    def _req(self, method, path, params=None, body=None, signed=False):
        url, headers, data = BASE + path, {"Content-Type": "application/json"}, None
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
        if qs and method == "GET":
            url += "?" + qs
        if method == "POST":
            data = json.dumps(body, separators=(",", ":"))
        if signed:
            ts = str(int(time.time() * 1000))
            payload = KEY + ts + (data if method == "POST" else qs)
            sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
            headers.update({"ApiKey": KEY, "Request-Time": ts, "Signature": sig})
        r = self.s.request(method, url, data=data, headers=headers, timeout=10)
        j = r.json()
        if not j.get("success", False):
            raise RuntimeError(f"{path}: {j}")
        return j["data"]
    def get(self, path, params=None, signed=False):
        return self._req("GET", path, params, signed=signed)
    def post(self, path, body):
        return self._req("POST", path, body=body, signed=True)

def slip_pct(levels, need, ref):
    got = cost = 0.0
    for lv in levels:
        if len(lv) < 2:
            continue
        price = float(lv[0])
        qty = float(lv[1])
        take = min(qty, need - got)
        got += take
        cost += take * price
        if got >= need:
            break
    if got < need * 0.8:
        return None, got
    return abs(cost / got - ref) / ref * 100, got

def rnd_price(p, tick):
    dec = max(0, int(round(-math.log10(tick))))
    return round(round(p / tick) * tick, dec)

class Bot:
    def __init__(self):
        self.api = Mexc()
        self.det = {}
        self.sim = None
        self.sim_bal = SIM_BALANCE
        self.ctx = None
        self.deadline = time.time() + RUN_S

    def now(self):
        return time.time() + self.api.off

    def start(self):
        srv = self.api.get("/api/v1/contract/ping")
        self.api.off = srv / 1000 - time.time()
        self.det = {d["symbol"]: d for d in self.api.get("/api/v1/contract/detail")}

    def balance(self):
        if DRY_RUN:
            return self.sim_bal, self.sim_bal
        for a in self.api.get("/api/v1/private/account/assets", signed=True):
            if a["currency"] == "USDT":
                return float(a["equity"]), float(a["availableBalance"])
        return 0.0, 0.0

    def ticker(self, sym):
        return self.api.get("/api/v1/contract/ticker", {"symbol": sym})

    def position(self, sym=None):
        if DRY_RUN:
            return self.sim if (self.sim and (sym is None or self.sim["sym"] == sym)) else None
        params = {"symbol": sym} if sym else None
        for p in self.api.get("/api/v1/private/position/open_positions", params, signed=True):
            if float(p["holdVol"]) > 0:
                return {"sym": p["symbol"], "side": "long" if p["positionType"] == 1 else "short",
                        "vol": float(p["holdVol"]), "avg": float(p["holdAvgPrice"]),
                        "pid": p["positionId"]}
        return None

    def place(self, sym, action, vol, ref, pid=None, hard_sl=None):
        code = {"open_long": 1, "close_short": 2, "open_short": 3, "close_long": 4}[action]
        is_open = action.startswith("open")
        tick = float(self.det[sym]["priceUnit"])
        if DRY_RUN:
            side = "long" if action.endswith("long") else "short"
            cs = float(self.det[sym]["contractSize"])
            if is_open:
                if self.sim is None:
                    self.sim = {"sym": sym, "side": side, "vol": vol, "avg": ref, "pid": 1}
                else:
                    tot = self.sim["vol"] + vol
                    self.sim["avg"] = (self.sim["avg"] * self.sim["vol"] + ref * vol) / tot
                    self.sim["vol"] = tot
                self.sim_bal -= ref * vol * cs * FEE
            else:
                d = 1 if side == "long" else -1
                self.sim_bal += (ref - self.sim["avg"]) * d * vol * cs - ref * vol * cs * FEE
                self.sim = None
            return
        body = {"symbol": sym, "price": rnd_price(ref, tick), "vol": vol, "leverage": LEVERAGE,
                "side": code, "type": 3 if is_open else 5, "openType": 1}
        if pid:
            body["positionId"] = pid
        if hard_sl and USE_HARD_SL:
            body["stopLossPrice"] = rnd_price(hard_sl, tick)
        self.api.post("/api/v1/private/order/submit", body)

    def candidates(self):
        tk = []
        for t in self.api.get("/api/v1/contract/ticker"):
            s, d = t["symbol"], self.det.get(t["symbol"])
            if not d or not s.endswith("_USDT") or d.get("state") != 0 or d.get("apiAllowed") is False:
                continue
            try:
                if abs(float(t["fundingRate"])) >= MIN_RATE:
                    tk.append(t)
            except (KeyError, TypeError, ValueError):
                continue
        def info(t):
            try:
                return self.api.get(f"/api/v1/contract/funding_rate/{t['symbol']}")
            except Exception:
                return None
        with ThreadPoolExecutor(8) as ex:
            infos = list(ex.map(info, tk))
        out = []
        for t, i in zip(tk, infos):
            if not i or int(i.get("collectCycle", 8)) not in CYCLES:
                continue
            rate = float(i["fundingRate"])
            if abs(rate) >= MIN_RATE:
                out.append({"sym": t["symbol"], "rate": rate, "settle": int(i["nextSettleTime"])})
        out.sort(key=lambda c: -abs(c["rate"]))
        return out

    def evaluate(self, c, bal):
        s, d, rate = c["sym"], self.det[c["sym"]], c["rate"]
        cs, unit, min_vol = float(d["contractSize"]), float(d.get("volUnit", 1)), float(d["minVol"])
        side = "short" if rate > 0 else "long"
        book = self.api.get(f"/api/v1/contract/depth/{s}", {"limit": 100})
        asks, bids = book["asks"], book["bids"]
        if not asks or not bids:
            return None, "empty book"
        ref = (float(asks[0][0]) + float(bids[0][0])) / 2
        entry_n = bal * ENTRY_PCT * LEVERAGE
        if math.floor(entry_n / (ref * cs) / unit) * unit < min_vol:
            return None, f"min order {min_vol} contracts not affordable"
        total_vol = bal * (ENTRY_PCT + DCA_PCT) * LEVERAGE / (ref * cs)
        entry_vol = entry_n / (ref * cs)
        s_in, got_in = slip_pct(asks if side == "long" else bids, entry_vol, ref)
        if s_in is None:
            return None, f"book too thin for balance (need {entry_vol:.1f} got {got_in:.1f})"
        s_out, _ = slip_pct(bids if side == "long" else asks, total_vol, ref)
        if s_out is None:
            s_out = s_in
        if max(s_in, s_out) > MAX_SLIP:
            return None, f"slippage {max(s_in, s_out):.2f}% > {MAX_SLIP}%"
        net = abs(rate) * entry_n - entry_n * 2 * FEE - entry_n * (s_in + s_out) / 100
        if net <= 0:
            return None, "not profitable after fees/slippage"
        return {"side": side, "ref": ref, "net": net, "s_in": s_in, "s_out": s_out}, ""

    def pick(self, bal, T):
        group = [c for c in self.candidates() if abs(c["settle"] / 1000 - T) < 30]
        for c in group[:15]:
            ev, why = self.evaluate(c, bal)
            if ev:
                return c, ev
            log(f"skip {c['sym']} {c['rate']*100:+.3f}%: {why}")
        return None, None

    def open_trade(self, c, ev, equity0, avail):
        s, d = c["sym"], self.det[c["sym"]]
        cs, unit = float(d["contractSize"]), float(d.get("volUnit", 1))
        tk = self.ticker(s)
        long = ev["side"] == "long"
        ref = float(tk["ask1"] if long else tk["bid1"])
        cap = ref * (1 + MAX_SLIP / 100) if long else ref * (1 - MAX_SLIP / 100)
        vol = math.floor(avail * ENTRY_PCT * LEVERAGE / (ref * cs) / unit) * unit
        vol = int(vol) if float(unit).is_integer() else vol
        hard = ref * (1 - HARD_SL_PCT / 100) if long else ref * (1 + HARD_SL_PCT / 100)
        self.place(s, "open_long" if long else "open_short", vol, cap if not DRY_RUN else ref, hard_sl=hard)
        time.sleep(0.6)
        pos = self.position(s)
        if not pos:
            tg(f"Entry not filled: {s}")
            return None
        tg(f"OPEN {pos['side'].upper()} {s} rate {c['rate']*100:+.3f}% vol {pos['vol']} avg {pos['avg']}\n"
           f"funding in {int(c['settle']/1000 - self.now())}s | est net {ev['net']:.2f} USDT")
        return {"sym": s, "rate": c["rate"], "settle": c["settle"], "funded": False, "dca": False,
                "equity0": equity0, "cs": cs, "unit": unit, "t0": time.time(), "tfund": None}

    def close(self, ctx, pos, price, reason):
        long = pos["side"] == "long"
        vol = int(pos["vol"]) if float(pos["vol"]).is_integer() else pos["vol"]
        self.place(pos["sym"], "close_long" if long else "close_short", vol, price, pid=pos["pid"])
        time.sleep(1.5)
        d = 1 if long else -1
        pnl = (price - pos["avg"]) * d * pos["vol"] * ctx["cs"]
        notional = pos["avg"] * pos["vol"] * ctx["cs"]
        fund = abs(ctx["rate"]) * notional if ctx["funded"] else 0
        tg(f"CLOSE {pos['sym']} ({reason}) @ {price}\nprice pnl {pnl:+.3f} | funding ~{fund:+.3f} | "
           f"fees ~{-2*FEE*notional:.3f} | est net {pnl + fund - 2*FEE*notional:+.3f} USDT")

    def manage(self, ctx):
        sym = ctx["sym"]
        while True:
            if time.time() > self.deadline:
                tg(f"Run time over with open position {sym}; next run takes over.")
                return
            pos = self.position(sym)
            if not pos:
                tg(f"Position {sym} closed (exit/stop).")
                self.ctx = None
                return
            price = float(self.ticker(sym)["lastPrice"])
            avg, long = pos["avg"], pos["side"] == "long"
            adverse = (avg - price) / avg * 100 if long else (price - avg) / avg * 100
            if adverse >= SL_PCT:
                self.close(ctx, pos, price, f"STOP-LOSS -{SL_PCT}% price / -{SL_PCT*LEVERAGE:.0f}% margin")
                self.ctx = None
                return
            if not ctx["funded"] and self.now() >= ctx["settle"] / 1000 + 3:
                ctx["funded"], ctx["tfund"] = True, time.time()
                if DRY_RUN:
                    self.sim_bal += abs(ctx["rate"]) * avg * pos["vol"] * ctx["cs"]
                tg(f"Funding settled on {sym}; exiting at avg {avg} (or better) / DCA if {DCA_TRIGGER}% against.")
            if ctx["funded"]:
                buf = 2 * FEE
                target = avg * (1 + buf) if long else avg * (1 - buf)
                if (price >= target) if long else (price <= target):
                    self.close(ctx, pos, price, "exit at avg/profit")
                    self.ctx = None
                    return
                if MAX_HOLD_MIN and time.time() - ctx["tfund"] > MAX_HOLD_MIN * 60:
                    self.close(ctx, pos, price, "max hold time")
                    self.ctx = None
                    return
                if not ctx["dca"] and adverse >= DCA_TRIGGER:
                    self.dca(ctx, pos, price)
            time.sleep(0.7)

    def dca(self, ctx, pos, price):
        _, avail = self.balance()
        margin = min(DCA_PCT * ctx["equity0"], avail * 0.995)
        vol = math.floor(margin * LEVERAGE / (price * ctx["cs"]) / ctx["unit"]) * ctx["unit"]
        vol = int(vol) if float(ctx["unit"]).is_integer() else vol
        ctx["dca"] = True
        if vol < float(self.det[pos["sym"]]["minVol"]):
            tg("DCA skipped: balance below min order size.")
            return
        long = pos["side"] == "long"
        self.place(pos["sym"], "open_long" if long else "open_short", vol, price, pid=pos["pid"])
        time.sleep(0.6)
        n = self.position(pos["sym"])
        if n:
            tg(f"DCA {pos['sym']} +{vol} @ {price} | new avg {n['avg']} | total vol {n['vol']}\n"
               f"SL now {SL_PCT}% from new avg, exit at new avg")

    def recover(self, pos):
        i = self.api.get(f"/api/v1/contract/funding_rate/{pos['sym']}")
        nxt, cyc = int(i["nextSettleTime"]), int(i.get("collectCycle", 4))
        last = nxt - cyc * 3600 * 1000
        funded = self.now() >= last / 1000 + 3
        eq, _ = self.balance()
        d = self.det[pos["sym"]]
        tg(f"Recovered open position {pos['sym']} {pos['side']} avg {pos['avg']} (funded={funded}, DCA treated as done)")
        return {"sym": pos["sym"], "rate": float(i["fundingRate"]), "settle": last if funded else nxt,
                "funded": funded, "dca": True, "equity0": eq, "cs": float(d["contractSize"]),
                "unit": float(d.get("volUnit", 1)), "t0": time.time(), "tfund": time.time() if funded else None}

    def cycle(self):
        cands = self.candidates()
        if not cands:
            log("no pair with |funding| >= %.2f%%" % (MIN_RATE * 100))
            time.sleep(20)
            return
        T = min(c["settle"] for c in cands) / 1000
        left = T - self.now()
        if left > PRECHECK_S + 30:
            top = cands[0]
            log(f"next funding in {int(left)}s | top {top['sym']} {top['rate']*100:+.3f}%")
            time.sleep(min(left - PRECHECK_S - 5, 60))
            return
        if left < ENTRY_S - 5:
            log("too late for this slot, waiting for next")
            time.sleep(max(1, T - self.now() + 5))
            return
        _, avail = self.balance()
        c, ev = self.pick(avail, T)
        if not c:
            tg("Pre-check: no pair passes depth/size/profit filters for this slot.")
            time.sleep(max(1, T - self.now() + 5))
            return
        tg(f"ARMED {c['sym']} {c['rate']*100:+.3f}% -> {ev['side'].upper()} | slip in/out "
           f"{ev['s_in']:.2f}/{ev['s_out']:.2f}% | entry at T-{int(ENTRY_S)}s")
        time.sleep(max(0, T - ENTRY_S - self.now()))
        equity, avail = self.balance()
        c, ev = self.pick(avail, T)
        if not c:
            tg("Final re-scan: nothing valid, skipping slot.")
            time.sleep(max(1, T - self.now() + 5))
            return
        self.ctx = self.open_trade(c, ev, avail, avail)
        if self.ctx:
            self.manage(self.ctx)

    def run(self):
        tg(f"Funding sniper started | {'DRY-RUN' if DRY_RUN else 'LIVE'} | {LEVERAGE}x | "
           f"{ENTRY_PCT*100:.0f}%+{DCA_PCT*100:.0f}% | min rate {MIN_RATE*100:.2f}% | cycles {sorted(CYCLES)}h")
        while True:
            try:
                pos = self.position()
                if pos:
                    self.ctx = self.ctx or self.recover(pos)
                    self.manage(self.ctx)
                    if time.time() > self.deadline:
                        return
                    continue
                if time.time() > self.deadline - 600:
                    tg("Flat and run window ending; handing over to next run.")
                    return
                self.cycle()
            except Exception as e:
                tg(f"ERROR: {e}")
                time.sleep(5)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if not DRY_RUN and not (KEY and SECRET):
        sys.exit("Set MEXC_API_KEY / MEXC_API_SECRET (or DRY_RUN=true).")
    bot = Bot()
    bot.start()
    if mode == "scan":
        _, avail = bot.balance()
        print(f"Balance: {avail} USDT | Min rate: {MIN_RATE*100:.2f}%")
        for c in bot.candidates()[:15]:
            ev, why = bot.evaluate(c, avail)
            print(f"{c['sym']:<18}{c['rate']*100:+8.3f}%  ", f"OK net~{ev['net']:.2f}" if ev else f"skip: {why}")
        return
    bot.run()

if __name__ == "__main__":
    main()
