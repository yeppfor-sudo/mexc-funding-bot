#!/usr/bin/env python3
"""
Funding Sniper v4 - WAF Bypass for GitHub Actions
Fixes: GitHub IP banned by MEXC WAF -> Access Denied HTML
"""
import hashlib, hmac, json, logging, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests

def _f(n,d): return float(os.getenv(n,d))
def _b(n,d): return os.getenv(n,d).strip().lower() in ("1","true","yes","on")
BASES=["https://contract.mexc.com","https://futures.mexc.com"]
KEY=os.getenv("MEXC_API_KEY",""); SECRET=os.getenv("MEXC_API_SECRET","")
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN",""); TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")
DRY_RUN=_b("DRY_RUN","true"); SIM_BALANCE=_f("SIM_BALANCE","10")
LEVERAGE=int(_f("LEVERAGE","5")); ENTRY_PCT=_f("ENTRY_PCT","70")/100; DCA_PCT=_f("DCA_PCT","29")/100
MIN_RATE=_f("MIN_RATE_PCT","0.5")/100; PRECHECK_S=_f("PRECHECK_SECONDS","120"); ENTRY_S=_f("ENTRY_SECONDS_BEFORE","50")
DCA_TRIGGER=_f("DCA_TRIGGER_PCT","1.0"); SL_PCT=_f("SL_PRICE_PCT","3.0"); HARD_SL_PCT=SL_PCT+1.5
USE_HARD_SL=_b("USE_HARD_SL","true"); FEE=_f("TAKER_FEE_PCT","0.02")/100; MAX_SLIP=_f("MAX_SLIPPAGE_PCT","0.50")
CYCLES={int(x) for x in os.getenv("FUNDING_CYCLES_HOURS","4").split(",")}; RUN_S=_f("RUN_SECONDS","20700")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"); log=logging.info
def tg(m):
    log(m)
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id":TG_CHAT,"text":m}, timeout=10)
        except: pass

class Mexc:
    def __init__(self): self.s=requests.Session(); self.off=0.0
    def _req(self, method, path, params=None, body=None, signed=False):
        last=None
        for base in BASES:
          for attempt in range(4):
            try:
                url=base+path
                headers={
                  "Content-Type":"application/json",
                  "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                  "Accept":"application/json",
                  "Origin":"https://futures.mexc.com",
                  "Referer":"https://futures.mexc.com/"
                }
                qs="&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
                if qs and method=="GET": url+= "?"+qs
                data=json.dumps(body, separators=(",",":")) if method=="POST" else None
                if signed:
                    ts=str(int(time.time()*1000)); payload=KEY+ts+(data if method=="POST" else qs)
                    sig=hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
                    headers.update({"ApiKey":KEY,"Request-Time":ts,"Signature":sig})
                r=self.s.request(method, url, data=data, headers=headers, timeout=20)
                txt=r.text or ""
                if "Access Denied" in txt:
                    last=f"WAF block on {base}"; time.sleep(1); continue
                if not txt.strip():
                    if attempt<3: time.sleep(0.8); continue
                    raise RuntimeError("empty response")
                j=r.json()
                if not j.get("success", False):
                    if j.get("code")==429 and attempt<3: time.sleep(1); continue
                    raise RuntimeError(str(j))
                return j["data"]
            except Exception as e:
                last=e
                if "Access Denied" in str(e) or "empty" in str(e).lower():
                    time.sleep(0.8); continue
        raise RuntimeError(f"{path} WAF fail: {last} -> Disable IP whitelist in MEXC API settings!")

    def get(self, path, params=None, signed=False): return self._req("GET", path, params, signed=signed)
    def post(self, path, body): return self._req("POST", path, body=body, signed=True)

def slip_pct(levels, need, ref):
    got=cost=0.0
    for lv in levels:
        if len(lv)<2: continue
        p,q=float(lv[0]),float(lv[1]); take=min(q, need-got); got+=take; cost+=take*p
        if got>=need: break
    if got<need*0.8: return None, got
    return abs(cost/got-ref)/ref*100, got

def rnd_price(p,tick):
    dec=max(0,int(round(-math.log10(tick)))); return round(round(p/tick)*tick, dec)

class Bot:
    def __init__(self): self.api=Mexc(); self.det={}; self.sim=None; self.sim_bal=SIM_BALANCE; self.ctx=None; self.deadline=time.time()+RUN_S
    def now(self): return time.time()+self.api.off
    def start(self):
        srv=self.api.get("/api/v1/contract/ping"); self.api.off=srv/1000-time.time()
        self.det={d["symbol"]:d for d in self.api.get("/api/v1/contract/detail")}
    def balance(self):
        if DRY_RUN: return self.sim_bal, self.sim_bal
        for a in self.api.get("/api/v1/private/account/assets", signed=True):
            if a["currency"]=="USDT": return float(a["equity"]), float(a["availableBalance"])
        return 0.0,0.0
    def ticker(self, sym): return self.api.get("/api/v1/contract/ticker", {"symbol":sym})
    def position(self, sym=None):
        if DRY_RUN: return self.sim if (self.sim and (sym is None or self.sim["sym"]==sym)) else None
        params={"symbol":sym} if sym else None
        for p in self.api.get("/api/v1/private/position/open_positions", params, signed=True):
            if float(p["holdVol"])>0: return {"sym":p["symbol"],"side":"long" if p["positionType"]==1 else "short","vol":float(p["holdVol"]),"avg":float(p["holdAvgPrice"]),"pid":p["positionId"]}
        return None
    def place(self, sym, action, vol, ref, pid=None, hard_sl=None):
        code={"open_long":1,"close_short":2,"open_short":3,"close_long":4}[action]
        is_open=action.startswith("open"); tick=float(self.det[sym]["priceUnit"])
        if DRY_RUN:
            side="long" if action.endswith("long") else "short"; cs=float(self.det[sym]["contractSize"])
            if is_open:
                if self.sim is None: self.sim={"sym":sym,"side":side,"vol":vol,"avg":ref,"pid":1}
                else:
                    tot=self.sim["vol"]+vol; self.sim["avg"]=(self.sim["avg"]*self.sim["vol"]+ref*vol)/tot; self.sim["vol"]=tot
            else: self.sim=None
            return
        body={"symbol":sym,"price":rnd_price(ref,tick),"vol":vol,"leverage":LEVERAGE,"side":code,"type":3 if is_open else 5,"openType":1}
        if pid: body["positionId"]=pid
        if hard_sl and USE_HARD_SL: body["stopLossPrice"]=rnd_price(hard_sl,tick)
        for i in range(5):
            try: return self.api.post("/api/v1/private/order/submit", body)
            except Exception as e:
                if i==4: raise
                log(f"order retry {i+1} {e}"); time.sleep(0.6)

    def candidates(self):
        tk=[t for t in self.api.get("/api/v1/contract/ticker") if t["symbol"].endswith("_USDT") and abs(float(t.get("fundingRate",0)))>=MIN_RATE]
        def info(t):
            try: return self.api.get(f"/api/v1/contract/funding_rate/{t['symbol']}")
            except: return None
        with ThreadPoolExecutor(8) as ex: infos=list(ex.map(info, tk))
        out=[]
        for t,i in zip(tk,infos):
            if not i or int(i.get("collectCycle",8)) not in CYCLES: continue
            out.append({"sym":t["symbol"],"rate":float(i["fundingRate"]),"settle":int(i["nextSettleTime"])})
        out.sort(key=lambda c: -abs(c["rate"])); return out

    def evaluate(self,c,bal):
        s,d,rate=c["sym"],self.det[c["sym"]],c["rate"]; cs=float(d["contractSize"]); unit=float(d.get("volUnit",1)); min_vol=float(d["minVol"])
        side="short" if rate>0 else "long"
        book=self.api.get(f"/api/v1/contract/depth/{s}", {"limit":100}); asks,bids=book["asks"],book["bids"]
        if not asks or not bids: return None,"empty book"
        ref=(float(asks[0][0])+float(bids[0][0]))/2; entry_n=bal*ENTRY_PCT*LEVERAGE
        total_vol=bal*(ENTRY_PCT+DCA_PCT)*LEVERAGE/(ref*cs); entry_vol=entry_n/(ref*cs)
        s_in,got_in=slip_pct(asks if side=="long" else bids, entry_vol, ref)
        if s_in is None: return None,f"book too thin"
        s_out,_=slip_pct(bids if side=="long" else asks, total_vol, ref)
        if s_out is None: s_out=s_in
        if max(s_in,s_out)>MAX_SLIP: return None,f"slippage {max(s_in,s_out):.2f}%"
        net=abs(rate)*entry_n-entry_n*2*FEE-entry_n*(s_in+s_out)/100
        if net<=0: return None,"not profitable"
        return {"side":side,"ref":ref,"net":net,"s_in":s_in,"s_out":s_out},""

    def pick(self,bal,T):
        group=[c for c in self.candidates() if abs(c["settle"]/1000-T)<30]
        for c in group[:15]:
            ev,why=self.evaluate(c,bal)
            if ev: return c,ev
            log(f"skip {c['sym']} {c['rate']*100:+.3f}%: {why}")
        return None,None

    def open_trade(self,c,ev,avail):
        s=self.det[c["sym"]]; cs,unit=float(s["contractSize"]),float(s.get("volUnit",1))
        tk=self.ticker(c["sym"]); long=ev["side"]=="long"; ref=float(tk["ask1"] if long else tk["bid1"])
        vol=math.floor(avail*ENTRY_PCT*LEVERAGE/(ref*cs)/unit)*unit
        hard=ref*(1-HARD_SL_PCT/100) if long else ref*(1+HARD_SL_PCT/100)
        self.place(c["sym"],"open_long" if long else "open_short",vol,ref,hard_sl=hard)
        time.sleep(0.8); pos=self.position(c["sym"])
        if not pos: return None
        tg(f"OPEN {pos['side'].upper()} {c['sym']} rate {c['rate']*100:+.3f}% vol {pos['vol']}")
        return {"sym":c["sym"],"rate":c["rate"],"settle":c["settle"],"funded":False,"dca":False,"equity0":avail,"cs":cs,"unit":unit,"t0":time.time(),"tfund":None}

    def close(self,ctx,pos,price,reason):
        self.place(pos["sym"],"close_long" if pos["side"]=="long" else "close_short",pos["vol"],price,pid=pos["pid"])
        tg(f"CLOSE {pos['sym']} ({reason}) @ {price}")

    def manage(self,ctx):
        while True:
            pos=self.position(ctx["sym"])
            if not pos: tg(f"Position {ctx['sym']} closed."); return
            price=float(self.ticker(ctx["sym"])["lastPrice"]); avg=pos["avg"]; long=pos["side"]=="long"
            adverse=(avg-price)/avg*100 if long else (price-avg)/avg*100
            if adverse>=SL_PCT: self.close(ctx,pos,price,f"SL -{SL_PCT}%"); return
            if not ctx["funded"] and self.now()>=ctx["settle"]/1000+3:
                ctx["funded"]=True; tg(f"Funding settled {ctx['sym']}; exiting at avg")
            if ctx["funded"]:
                target=avg*(1+2*FEE) if long else avg*(1-2*FEE)
                if (price>=target) if long else (price<=target): self.close(ctx,pos,price,"exit at avg/profit"); return
                if not ctx["dca"] and adverse>=DCA_TRIGGER:
                    _,avail=self.balance(); margin=min(DCA_PCT*ctx["equity0"],avail*0.995)
                    vol=math.floor(margin*LEVERAGE/(price*ctx["cs"])/ctx["unit"])*ctx["unit"]
                    if vol>=float(self.det[pos["sym"]]["minVol"]):
                        self.place(pos["sym"],"open_long" if long else "open_short",vol,price,pid=pos["pid"])
                        ctx["dca"]=True; tg(f"DCA {pos['sym']} +{vol}")
            time.sleep(0.7)

    def cycle(self):
        cands=self.candidates()
        if not cands: time.sleep(20); return
        T=min(c["settle"] for c in cands)/1000; left=T-self.now()
        if left>PRECHECK_S+30: top=cands[0]; log(f"next funding in {int(left)}s | top {top['sym']} {top['rate']*100:+.3f}%"); time.sleep(min(left-PRECHECK_S-5,60)); return
        if left<ENTRY_S-5: log("too late"); time.sleep(max(1,T-self.now()+5)); return
        _,avail=self.balance(); c,ev=self.pick(avail,T)
        if not c: tg("Pre-check no pair"); time.sleep(max(1,T-self.now()+5)); return
        tg(f"ARMED {c['sym']} {c['rate']*100:+.3f}% -> {ev['side'].upper()}")
        time.sleep(max(0,T-ENTRY_S-self.now())); _,avail=self.balance(); c,ev=self.pick(avail,T)
        if not c: tg("Final re-scan nothing"); time.sleep(max(1,T-self.now()+5)); return
        ctx=self.open_trade(c,ev,avail)
        if ctx: self.manage(ctx)

    def run(self):
        tg(f"Sniper v4 WAF-fix LIVE {LEVERAGE}x")
        while True:
            try:
                pos=self.position()
                if pos:
                    from types import SimpleNamespace
                    d=self.det[pos["sym"]]; i=self.api.get(f"/api/v1/contract/funding_rate/{pos['sym']}")
                    ctx={"sym":pos["sym"],"rate":float(i["fundingRate"]),"settle":int(i["nextSettleTime"]),"funded":False,"dca":True,"equity0":self.balance()[0],"cs":float(d["contractSize"]),"unit":float(d.get("volUnit",1))}
                    self.manage(ctx); continue
                if time.time()>self.deadline-600: tg("Handing over"); return
                self.cycle()
            except Exception as e: tg(f"ERROR: {e}"); time.sleep(2)

def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "run"
    b=Bot(); b.start()
    if mode=="scan":
        _,avail=b.balance()
        for c in b.candidates()[:10]: print(c)
        return
    b.run()
if __name__=="__main__": main()
