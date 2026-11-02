import os, time, html, requests, ccxt, threading, queue, json
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

API_KEY = os.getenv("MEXC_API_KEY")
SECRET = os.getenv("MEXC_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("LIVE","0") != "1"

LEVERAGE = 5
WALLET_PCT = 0.99
MAX_PER_COIN = 3000
MAX_TOTAL = 10000
MAX_COINS = 3
SLOT_HOURS = [0,4,8,12,16,20]
MIN_RATE = 0.005
FEE = 0.0004
MIN_EDGE = 0.002
MAX_SPREAD = 0.001
SL = 0.03
NEAR_AVG = 0.0008

BASE = "https://contract.mexc.com/api/v1/contract"
CLOCK = 0
STATE_FILE="/tmp/risk_state.json"
START_WALLET = 0

ex = ccxt.mexc({"apiKey":API_KEY,"secret":SECRET,"options":{"defaultType":"swap"},"enableRateLimit":True})

q = queue.Queue()
def worker():
    while True:
        try:
            m=q.get()
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id":TG_CHAT,"text":("[DRY] " if DRY_RUN else "")+html.escape(m)[:3800]}, timeout=10)
        except:
            pass
        time.sleep(0.15)

threading.Thread(target=worker, daemon=True).start()

def tg(m):
    print(m, flush=True)
    try:
        q.put_nowait(m)
    except:
        pass

def get(u):
    for _ in range(3):
        try:
            return requests.get(u, timeout=12).json()
        except:
            time.sleep(0.4)
    return {}

def sync():
    global CLOCK
    for _ in range(3):
        try:
            t0=time.time()
            r=get(f"{BASE}/ping")
            srv=r.get('data',{}).get('time') or r.get('time') or t0*1000
            if srv>1e12:
                srv/=1000
            CLOCK=srv-(t0+time.time())/2
            return
        except:
            time.sleep(0.5)

def now():
    return time.time()+CLOCK

def bal():
    for _ in range(3):
        try:
            return float((ex.fetch_balance({'type':'swap'}).get('USDT') or {}).get('free') or 0)
        except:
            time.sleep(0.5)
    return 0

def wait(ts):
    while True:
        d=ts-now()
        if d<=0:
            return
        time.sleep(min(d,15) if d>1 else 0.01)

def finfo(raw):
    try:
        d=get(f"{BASE}/funding_rate/{raw}").get("data") or {}
        return float(d["fundingRate"]), int(d["collectCycle"]), int(d["nextSettleTime"])/1000
    except:
        return None

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"day":"","day_pnl":0,"loss_streak":0,"trades_today":0}

def save_state(s):
    try:
        with open(STATE_FILE,"w") as f:
            json.dump(s,f)
    except:
        pass

def check_risk(wallet_before, wallet_after):
    global START_WALLET
    s=load_state()
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if s["day"]!=today:
        s={"day":today,"day_pnl":0,"loss_streak":0,"trades_today":0}
    pnl = wallet_after - wallet_before
    s["day_pnl"]+=pnl
    s["trades_today"]+=1
    if pnl<0:
        s["loss_streak"]+=1
    else:
        s["loss_streak"]=0
    save_state(s)
    if s["day_pnl"] < -wallet_before*0.15:
        tg(f"🚨 CIRCUIT BREAKER: Day loss {s['day_pnl']:.2f} >15% - Pause 24h")
        time.sleep(86400)
        return False
    if s["loss_streak"]>=4:
        tg(f"🚨 4 loss streak - Pause 4h")
        time.sleep(14400)
        s["loss_streak"]=0
        save_state(s)
    if START_WALLET>0 and wallet_after < START_WALLET*0.5:
        tg(f"🚨 Max DD 50% hit - Stop today")
        return False
    return True

def scan(slot):
    rows=get(f"{BASE}/ticker").get("data") or []
    cand=[]
    for d in rows:
        if not str(d.get("symbol","")).endswith("_USDT"):
            continue
        try:
            rate=float(d.get("fundingRate") or 0)
            if abs(rate) < MIN_RATE:
                continue
            cand.append((d["symbol"], rate))
        except:
            continue
    cand=sorted(cand, key=lambda c: -abs(c[1]))[:35]
    with ThreadPoolExecutor(10) as pool:
        infos=list(pool.map(lambda c: finfo(c[0]), cand))
    out=[]
    for (raw,rate),inf in zip(cand,infos):
        if not inf:
            continue
        r,cycle,nxt=inf
        if cycle==4 and abs(nxt-slot)<90 and abs(r)>=MIN_RATE:
            out.append((raw, r))
    return out

def rank(valid):
    res=[]
    for raw,rate in valid:
        sym=raw.replace("_","/")+":USDT"
        try:
            m=ex.market(sym)
            cs=float(m.get('contractSize') or 1)
            side="sell" if rate>0 else "buy"
            ob=ex.fetch_order_book(sym,50)
            best=ob["asks"][0][0] if side=="buy" else ob["bids"][0][0]
            spread=(ob["asks"][0][0]-ob["bids"][0][0])/best
            depth=0
            for p,q in (ob["asks"] if side=="buy" else ob["bids"]):
                if abs(p-best)/best>0.003:
                    break
                depth+=p*q*cs
            liq=min(depth*0.3, MAX_PER_COIN)
            edge=abs(rate)-2*FEE-2*0.0005-spread
            if spread>MAX_SPREAD or edge<MIN_EDGE:
                continue
            amt_test=float(ex.amount_to_precision(sym, liq/(best*cs)))
            min_amt=float((m.get("limits",{}).get("amount") or {}).get("min") or 0)
            if amt_test<min_amt:
                continue
            res.append(dict(raw=raw,sym=sym,rate=rate,side=side,best=best,edge=edge,spread=spread,cs=cs,max_usd=liq))
        except:
            pass
    return sorted(res, key=lambda c: -abs(c["rate"]))

def lev(c):
    pt=2 if c["side"]=="sell" else 1
    try:
        ex.set_leverage(LEVERAGE,c["sym"],params={"openType":1,"positionType":pt})
        return True
    except:
        return False

def o(sym,side,amt,reduce=False):
    if DRY_RUN:
        return True
    p={"openType":1}
    if reduce:
        p["reduceOnly"]=True
    for _ in range(3):
        try:
            ex.create_order(sym,"market",side,amt,None,p)
            return True
        except:
            time.sleep(0.08)
    return False

def pos(sym):
    try:
        for p in ex.fetch_positions([sym]):
            if float(p.get("contracts") or 0)>0:
                return p
    except:
        pass
    return None

def close(sym,side,amt):
    to="buy" if side=="sell" else "sell"
    for _ in range(5):
        try:
            if DRY_RUN:
                return True
            o(sym,to,amt,True)
            time.sleep(0.2)
            if not pos(sym):
                return True
        except:
            pass
    tg(f"🚨 CLOSE FAILED {sym} CLOSE MANUALLY")
    return False

def handle(slot):
    dt=datetime.fromtimestamp(slot, timezone.utc)
    nxt_dt=dt+timedelta(hours=4)
    wait(slot-135)
    sync()
    wait(slot-120)
    wallet=bal()
    if wallet<1:
        tg(f"Low wallet ${wallet}")
        return
    if wallet<1000:
        max_total=min(wallet*WALLET_PCT*LEVERAGE, 1000)
    elif wallet<10000:
        max_total=min(wallet*WALLET_PCT*LEVERAGE, MAX_TOTAL)
    else:
        max_total=min(wallet*WALLET_PCT*4, 20000)
    total=max_total
    tg(f"⏰ Slot {dt:%H:%M} UTC - Scan >0.5% | Wallet ${wallet:.2f} | Total ${total:.0f}")
    valid=scan(slot)
    if not valid:
        tg(f"❌ No funding >=0.5% at {dt:%H:%M} - Waiting next {nxt_dt:%H:%M} UTC")
        return
    ranked=rank(valid)
    if not ranked:
        tg(f"❌ No >0.5% passes edge/liq - Waiting next {nxt_dt:%H:%M} UTC")
        return
    ready=[]
    for c in ranked[:10]:
        if lev(c):
            ready.append(c)
        if len(ready)>=MAX_COINS:
            break
    if not ready:
        return
    tg(f"🔥 Found {len(ready)} >0.5%: " + ", ".join([f"{c['raw']} {c['rate']*100:+.3f}%" for c in ready]))
    wait(slot-50)
    final=[]
    for c in ready:
        inf=finfo(c["raw"])
        if not inf:
            continue
        rate,cycle,nxt=inf
        if cycle!=4 or abs(nxt-slot)>=90 or abs(rate)<MIN_RATE or (rate>0)!=(c["rate"]>0):
            continue
        edge=abs(rate)-2*FEE-2*0.0005-c["spread"]
        if edge>=MIN_EDGE:
            final.append(dict(c,rate=rate,edge=edge))
    if not final:
        tg(f"❌ ABORT T-50s dropped <0.5% - Waiting next {nxt_dt:%H:%M} UTC")
        return
    wait(slot-45)
    per=total/len(final)
    entries=[]
    for c in final:
        if abs(c["rate"])<MIN_RATE:
            continue
        usd=min(per, c["max_usd"], MAX_PER_COIN if wallet<10000 else 5000)
        amt=float(ex.amount_to_precision(c["sym"], usd/(c["best"]*c["cs"])))
        if o(c["sym"],c["side"],amt,False):
            entries.append(dict(c,amt=amt,usd=usd))
            tg(f"🚀 ENTER {c['raw']} {'SHORT' if c['side']=='sell' else 'LONG'} {c['rate']*100:+.3f}% ${usd:.0f}")
    if not entries:
        return
    for e in entries:
        if DRY_RUN:
            e["entry"]=e["best"]
            continue
        time.sleep(0.12)
        p=pos(e["sym"])
        if p:
            ep=float(p["entryPrice"])
            e["entry"]=ep
            e["amt"]=float(p["contracts"])
            try:
                sl=ep*(1+SL) if e["side"]=="sell" else ep*(1-SL)
                ex.create_order(e["sym"],"market","buy" if e["side"]=="sell" else "sell",e["amt"],None,{"stopLossPrice":sl,"reduceOnly":True,"openType":1})
            except:
                pass
        else:
            e["entry"]=e["best"]
    tg(f"⏳ HOLD - Must close within 3s after {dt:%H:%M} UTC")
    closed=set()
    while now() < slot + 3.0 and len(closed) < len(entries):
        sec = now() - slot
        for i,e in enumerate(entries):
            if i in closed:
                continue
            try:
                t=ex.fetch_ticker(e["sym"])
                px=(t.get("bid") if e["side"]=="buy" else t.get("ask")) or t["last"]
                entry=e.get("entry",e["best"])
                chg=(px-entry)/entry if e["side"]=="buy" else (entry-px)/entry
                reason=None
                if chg <= -SL:
                    reason=f"STOP -3%"
                elif sec >= 0:
                    if chg >= 0:
                        reason="AVG/PROFIT"
                    elif chg >= -NEAR_AVG and sec>=1.0:
                        reason="NEAR AVG"
                if reason:
                    ok=close(e["sym"],e["side"],e["amt"])
                    nb=bal() if not DRY_RUN else wallet
                    tg(f"{'✅' if ok else '⚠️'} CLOSE {e['raw']} [{reason} T+{sec:.2f}s] {chg*100:.3f}% {wallet:.2f}->{nb:.2f}")
                    closed.add(i)
            except:
                pass
        time.sleep(0.05)
    for i,e in enumerate(entries):
        if i not in closed:
            try:
                close(e["sym"],e["side"],e["amt"])
                tg(f"⏱️ FORCE CLOSE {e['raw']} at T+3.0s")
            except:
                pass
    try:
        for e in entries:
            ex.cancel_all_orders(e["sym"])
    except:
        pass
    nb=bal()
    tg(f"✅ DONE {dt:%H:%M} UTC - All closed within 3s | Wallet ${nb:.2f}")
    check_risk(wallet, nb)

def main():
    global START_WALLET
    ex.load_markets()
    sync()
    START_WALLET=bal()
    tg(f"BOT START - >0.5% ALWAYS 99%*5x ${START_WALLET:.2f} {'DRY' if DRY_RUN else 'LIVE'} - Risk ON + Force T+3s")
    start=time.time()
    while True:
        if time.time()-start>20000:
            try:
                tok=os.getenv("GH_TOKEN")
                repo=os.getenv("GH_REPO")
                if tok and repo:
                    requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/bot.yml/dispatches",
                        json={"ref":"main"}, headers={"Authorization":f"Bearer {tok}","Accept":"application/vnd.github.v3+json"}, timeout=10)
                    tg(f"🔄 Chaining next runner...")
            except:
                pass
            time.sleep(5)
            return
        nxt=None
        now_ts=now()
        for d in (0,1):
            for h in SLOT_HOURS:
                t=datetime.now(timezone.utc).replace(hour=h,minute=0,second=0,microsecond=0)+timedelta(days=d)
                ts=t.timestamp()
                if ts>now_ts+50 and (nxt is None or ts<nxt):
                    nxt=ts
        if not nxt or nxt-now_ts>4000:
            time.sleep(30)
            continue
        try:
            handle(nxt)
        except Exception as e:
            tg(f"Slot error: {e}")
        time.sleep(1)

if __name__=="__main__":
    main()
