"""
CoinSwitch Smart Bot v3 — Advanced Multi-Strategy Engine
12 coins | 3 strategies | High win rate | Low risk
"""
import requests, time, hmac, hashlib, json, os, threading, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, jsonify, request
from collections import deque

app = Flask(__name__)

CS_API_KEY    = os.environ.get("CS_API_KEY",    "YOUR_COINSWITCH_API_KEY")
CS_SECRET_KEY = os.environ.get("CS_SECRET_KEY", "YOUR_COINSWITCH_SECRET_KEY")
GMAIL_USER    = os.environ.get("GMAIL_USER",    "your@gmail.com")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",    "your_app_password")
ALERT_EMAIL   = os.environ.get("ALERT_EMAIL",  "your@gmail.com")
CAPITAL       = float(os.environ.get("CAPITAL", "300"))
BASE_URL      = "https://coinswitch.co"

# ── 12 coins across different market caps ──────────────
ALL_PAIRS = [
    "DOGE/INR","XRP/INR","TRX/INR","SHIB/INR",
    "BNB/INR","ADA/INR","MATIC/INR","LTC/INR",
    "LINK/INR","DOT/INR","AVAX/INR","SOL/INR"
]

# ── Per-strategy settings ──────────────────────────────
STRATEGIES = {
    "SCALP": {
        "tp": 0.005, "sl": 0.0025, "min_score": 2,
        "desc": "Quick 0.5% scalp, tight 0.25% stop"
    },
    "SWING": {
        "tp": 0.010, "sl": 0.005,  "min_score": 3,
        "desc": "0.1% swing, 0.5% stop, higher reward"
    },
    "MOMENTUM": {
        "tp": 0.008, "sl": 0.003,  "min_score": 2,
        "desc": "Momentum burst, 0.8% target"
    },
}
MAX_DAILY_TRADES = 20
SCAN_INTERVAL    = 60   # seconds

state = {
    "running": False, "capital": CAPITAL, "current": CAPITAL,
    "wins": 0, "losses": 0, "total_pnl": 0.0, "daily_trades": 0,
    "in_trade": False, "trade_sym": None, "strategy": None,
    "entry_price": 0.0, "tp_price": 0.0, "sl_price": 0.0, "live_pnl": 0.0,
    "last_scan": "Not started", "coin_scores": {}, "per_trade": 90.0,
    "log": deque(maxlen=100), "trades": deque(maxlen=200),
    "best_strategy": None, "market_trend": "UNKNOWN",
}

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    state["log"].appendleft(f"[{ts}] {msg}")
    print(f"[{ts}] {msg}")

def win_rate():
    t = state["wins"] + state["losses"]
    return round(state["wins"] / t * 100, 1) if t else 0.0

# ════════════════════════════════════════════════════
#  EMAIL
# ════════════════════════════════════════════════════
def send_alert(symbol, entry, exit_p, pnl, reason, strategy):
    if GMAIL_USER == "your@gmail.com": return
    color = "#00e676" if pnl >= 0 else "#ff3d57"
    emoji = "💰" if pnl >= 0 else "🛑"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{emoji} {symbol} {'+' if pnl>=0 else ''}₹{pnl:.2f} ({strategy})"
        msg["From"] = GMAIL_USER; msg["To"] = ALERT_EMAIL
        msg.attach(MIMEText(f"""
        <div style="font-family:monospace;background:#060810;color:#c9d6e3;padding:24px;border-radius:12px">
          <h2 style="color:{color}">{emoji} Trade Closed — {strategy} Strategy</h2>
          <table style="width:100%">
            <tr><td style="color:#5a7080">Coin</td><td><b>{symbol}</b></td></tr>
            <tr><td style="color:#5a7080">Entry</td><td>₹{entry:.6f}</td></tr>
            <tr><td style="color:#5a7080">Exit</td><td>₹{exit_p:.6f}</td></tr>
            <tr><td style="color:#5a7080">Reason</td><td>{reason}</td></tr>
            <tr><td style="color:#5a7080">P&L</td><td style="color:{color};font-size:20px"><b>{'+'if pnl>=0 else ''}₹{pnl:.2f}</b></td></tr>
            <tr><td style="color:#5a7080">Total P&L</td><td style="color:{color}">₹{state['total_pnl']:.2f}</td></tr>
            <tr><td style="color:#5a7080">Win Rate</td><td>{win_rate():.1f}% ({state['wins']}W/{state['losses']}L)</td></tr>
          </table>
          <p style="color:#3a4a5a;font-size:11px;margin-top:12px">CoinSwitch Smart Bot v3 • {datetime.now().strftime('%d %b %Y %H:%M')}</p>
        </div>""", "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        log(f"📧 Email sent for {symbol}")
    except Exception as e:
        log(f"❌ Email error: {e}")

# ════════════════════════════════════════════════════
#  INDICATORS
# ════════════════════════════════════════════════════
def ema(p, n):
    if len(p) < n: return [p[-1]] * len(p)
    e = [sum(p[:n]) / n]; k = 2/(n+1)
    for x in p[n:]: e.append(x*k + e[-1]*(1-k))
    return [e[0]]*(len(p)-len(e)) + e

def sma(p, n):
    return [sum(p[max(0,i-n+1):i+1])/min(i+1,n) for i in range(len(p))]

def calc_rsi(p, n=14):
    if len(p) < n+1: return 50.0
    g=[max(p[i]-p[i-1],0) for i in range(1,len(p))]
    l=[max(p[i-1]-p[i],0) for i in range(1,len(p))]
    ag=sum(g[-n:])/n; al=sum(l[-n:])/n
    return round(100-100/(1+ag/al),2) if al else 100.0

def calc_macd(p):
    e12=ema(p,12); e26=ema(p,26)
    ml=min(len(e12),len(e26))
    line=[e12[-ml+i]-e26[-ml+i] for i in range(ml)]
    sig=ema(line,9)
    hist=[line[i]-sig[i] for i in range(min(len(line),len(sig)))]
    return line[-1], sig[-1], hist[-1] if hist else 0

def bollinger(p, n=20):
    if len(p)<n: v=p[-1]; return v*1.02,v,v*0.98
    w=p[-n:]; m=sum(w)/n; s=(sum((x-m)**2 for x in w)/n)**.5
    return m+2*s, m, m-2*s

def stochastic(closes, highs, lows, n=14):
    if len(closes)<n: return 50.0,50.0
    lo=min(lows[-n:]); hi=max(highs[-n:])
    k=round((closes[-1]-lo)/(hi-lo)*100,2) if hi!=lo else 50.0
    # %D = 3-period SMA of %K (simplified)
    ks=[]
    for i in range(max(0,len(closes)-n-3), len(closes)):
        end=i+1; start=max(0,end-n)
        lo2=min(lows[start:end]); hi2=max(highs[start:end])
        ks.append((closes[i]-lo2)/(hi2-lo2)*100 if hi2!=lo2 else 50.0)
    d=sum(ks[-3:])/min(3,len(ks)) if ks else 50.0
    return k, d

def atr(h, l, c, n=14):
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    return sum(trs[-n:])/min(len(trs),n) if trs else 0

def vwap(closes, volumes):
    pv=sum(c*v for c,v in zip(closes,volumes))
    tv=sum(volumes)
    return pv/tv if tv else closes[-1]

def adx_trend(h, l, c, n=14):
    """Simplified ADX — returns trend strength 0-100."""
    if len(c)<n+1: return 25.0
    plus_dm=[max(h[i]-h[i-1],0) if h[i]-h[i-1]>l[i-1]-l[i] else 0 for i in range(1,len(c))]
    minus_dm=[max(l[i-1]-l[i],0) if l[i-1]-l[i]>h[i]-h[i-1] else 0 for i in range(1,len(c))]
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    atr_v=sum(trs[-n:])/n if len(trs)>=n else 1
    pdi=sum(plus_dm[-n:])/n/atr_v*100 if atr_v else 0
    mdi=sum(minus_dm[-n:])/n/atr_v*100 if atr_v else 0
    dx=abs(pdi-mdi)/(pdi+mdi)*100 if pdi+mdi else 0
    return round(dx,1), pdi>mdi  # (strength, is_uptrend)

# ════════════════════════════════════════════════════
#  3 STRATEGIES
# ════════════════════════════════════════════════════
def strategy_scalp(closes, volumes, highs, lows):
    """
    SCALP: Fast EMA cross + RSI not overbought + volume confirmation.
    Fires more often, smaller targets. Best for ranging markets.
    Score 0-4.
    """
    ef=ema(closes,9); es=ema(closes,21)
    r=calc_rsi(closes,14)
    avg_v=sum(volumes[-20:])/20
    vwap_v=vwap(closes[-20:],volumes[-20:])
    price=closes[-1]

    c1=(ef[-2]<es[-2]) and (ef[-1]>es[-1])   # EMA cross up
    c2=r<55                                    # RSI not overbought (relaxed)
    c3=volumes[-1]>avg_v*1.05                  # Slight volume pickup
    c4=price>vwap_v                            # Price above VWAP

    score=sum([c1,c2,c3,c4])
    return score, {"EMA Cross":c1,"RSI<55":c2,"Volume↑":c3,"Above VWAP":c4}, r, price

def strategy_momentum(closes, volumes, highs, lows):
    """
    MOMENTUM: MACD histogram turning positive + RSI building + ADX trending.
    Catches breakouts. Score 0-4.
    """
    ml, ms, mh = calc_macd(closes)
    r=calc_rsi(closes,14)
    adx_str, is_up = adx_trend(highs,lows,closes)
    ef=ema(closes,9); es=ema(closes,21)
    price=closes[-1]

    c1=mh>0 and ml>ms                         # MACD bullish histogram
    c2=35<r<65                                 # RSI in momentum zone
    c3=adx_str>20 and is_up                   # ADX trending up
    c4=ef[-1]>es[-1]                           # Fast EMA above slow

    score=sum([c1,c2,c3,c4])
    return score, {"MACD Hist+":c1,"RSI Momentum":c2,"ADX Trend↑":c3,"EMA Aligned":c4}, r, price

def strategy_reversal(closes, volumes, highs, lows):
    """
    REVERSAL: Oversold RSI + price near Bollinger lower + Stochastic crossup.
    Catches bounces from dips. Score 0-4.
    """
    r=calc_rsi(closes,14)
    bb_up, bb_mid, bb_lo=bollinger(closes)
    k, d=stochastic(closes,highs,lows)
    price=closes[-1]
    avg_v=sum(volumes[-20:])/20

    c1=r<40                                    # RSI oversold
    c2=price<=bb_lo*1.008                      # Near Bollinger lower
    c3=k>d and k<35                            # Stochastic cross up from low
    c4=volumes[-1]>avg_v*1.1                   # Volume confirmation

    score=sum([c1,c2,c3,c4])
    return score, {"RSI Oversold":c1,"Near BB Low":c2,"Stoch CrossUp":c3,"Volume Spike":c4}, r, price

def analyze_coin(closes, volumes, highs, lows):
    """
    Run all 3 strategies, pick the best scoring one.
    Returns best (strategy_name, score, checks, rsi, price).
    """
    results={}
    try: results["SCALP"]    = strategy_scalp(closes,volumes,highs,lows)
    except: pass
    try: results["MOMENTUM"] = strategy_momentum(closes,volumes,highs,lows)
    except: pass
    try: results["REVERSAL"] = strategy_reversal(closes,volumes,highs,lows)
    except: pass

    if not results: return "NONE", -1, {}, 50, 0

    # Pick strategy with highest score
    best = max(results.items(), key=lambda x: x[1][0])
    name = best[0]
    score, checks, rsi_v, price = best[1]

    # Get min_score threshold for this strategy
    min_sc = STRATEGIES[name]["min_score"]
    return name, score, checks, rsi_v, price, min_sc, results

# ════════════════════════════════════════════════════
#  COINSWITCH ORDER
# ════════════════════════════════════════════════════
def make_headers(method, endpoint, payload=""):
    ts=str(int(time.time()*1000))
    msg=ts+method+endpoint+(payload or "")
    sig=hmac.new(CS_SECRET_KEY.encode(),msg.encode(),hashlib.sha256).hexdigest()
    return {"X-AUTH-APIKEY":CS_API_KEY,"X-AUTH-SIGNATURE":sig,"X-AUTH-EPOCH":ts,"Content-Type":"application/json"}

def place_order(symbol, side, amount_inr, price):
    ep="/trade/api/v2/order"
    qty=round(amount_inr/price,6)
    body=json.dumps({"symbol":symbol.replace("/",""),"side":side.lower(),"type":"market","quantity":qty})
    try:
        r=requests.post(BASE_URL+ep,headers=make_headers("POST",ep,body),data=body,timeout=10)
        return r.json()
    except Exception as e:
        return {"error":str(e)}

# ════════════════════════════════════════════════════
#  FLASK ROUTES
# ════════════════════════════════════════════════════
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/state")
def api_state():
    t=state["wins"]+state["losses"]
    return jsonify({
        "running":state["running"],"capital":state["capital"],"current":round(state["current"],2),
        "pnl":round(state["total_pnl"],2),"wins":state["wins"],"losses":state["losses"],
        "win_rate":round(state["wins"]/t*100,1) if t else 0,
        "daily_trades":state["daily_trades"],"in_trade":state["in_trade"],
        "trade_sym":state["trade_sym"],"strategy":state["strategy"],
        "entry_price":state["entry_price"],"tp_price":round(state["tp_price"],6),
        "sl_price":round(state["sl_price"],6),"live_pnl":state["live_pnl"],
        "last_scan":state["last_scan"],"coin_scores":state["coin_scores"],
        "log":list(state["log"])[:25],"trades":list(state["trades"])[:15],
        "market_trend":state["market_trend"],
    })

@app.route("/api/start",methods=["POST"])
def api_start():
    d=request.json or {}
    cap=float(d.get("capital",CAPITAL))
    state.update({"running":True,"capital":cap,"current":cap,"wins":0,"losses":0,
                  "total_pnl":0.0,"daily_trades":0,"in_trade":False,"per_trade":round(cap*0.30)})
    log(f"🚀 Bot v3 started | Capital ₹{cap} | Per trade ₹{round(cap*0.30)} | 12 coins | 3 strategies")
    return jsonify({"ok":True})

@app.route("/api/stop",methods=["POST"])
def api_stop():
    state["running"]=False; log("⏹ Bot stopped.")
    return jsonify({"ok":True})

@app.route("/api/candles",methods=["POST"])
def api_candles():
    if not state["running"]: return jsonify({"action":"idle"})
    if state["daily_trades"]>=MAX_DAILY_TRADES: return jsonify({"action":"limit"})

    data=request.json or {}
    all_c=data.get("candles",{})
    state["last_scan"]=datetime.now().strftime("%H:%M:%S")
    per_trade=state["per_trade"]

    # ── IN TRADE: monitor exit ────────────────────────
    if state["in_trade"]:
        sym=state["trade_sym"]
        c=all_c.get(sym,{})
        closes=c.get("closes",[])
        if not closes: return jsonify({"action":"wait"})
        price=closes[-1]
        r_now=calc_rsi(closes)
        lpnl=round((price-state["entry_price"])/state["entry_price"]*per_trade,2)
        state["live_pnl"]=lpnl

        strat=state["strategy"] or "SCALP"
        cfg=STRATEGIES.get(strat,STRATEGIES["SCALP"])

        reason=None
        if   price>=state["tp_price"]: reason="TAKE_PROFIT ✅"
        elif price<=state["sl_price"]: reason="STOP_LOSS 🛑"
        elif r_now>75:                 reason="RSI_OVERBOUGHT"

        if reason:
            place_order(sym,"SELL",per_trade,price)
            pnl=round((price-state["entry_price"])/state["entry_price"]*per_trade,2)
            state["total_pnl"]+=pnl; state["current"]+=pnl; state["daily_trades"]+=1
            if pnl>=0: state["wins"]+=1
            else:      state["losses"]+=1
            state["trades"].appendleft({"time":datetime.now().strftime("%H:%M"),
                "sym":sym,"strategy":strat,"entry":state["entry_price"],
                "exit":price,"pnl":pnl,"reason":reason})
            entry_was=state["entry_price"]
            state.update({"in_trade":False,"trade_sym":None,"live_pnl":0.0,"strategy":None})
            log(f"{'💰' if pnl>=0 else '🛑'} {reason} | {sym} | P&L ₹{pnl:+.2f} | Total ₹{state['total_pnl']:.2f}")
            threading.Thread(target=send_alert,args=(sym,entry_was,price,pnl,reason,strat),daemon=True).start()
        else:
            log(f"📈 {sym}[{strat}] ₹{price:.5f} RSI:{r_now} P&L:₹{lpnl:+.2f}")
        return jsonify({"action":"monitoring"})

    # ── SCANNING: find best opportunity ──────────────
    log(f"🔍 Scanning {len(all_c)} coins across 3 strategies...")
    scores={}
    best_sym=None; best_score=-1; best_cfg=None; best_strat=None; best_price=0

    for sym, candles in all_c.items():
        closes=candles.get("closes",[]); volumes=candles.get("volumes",[])
        highs=candles.get("highs",[]); lows=candles.get("lows",[])
        if len(closes)<25: continue
        try:
            name,sc,checks,rsi_v,price,min_sc,all_results=analyze_coin(closes,volumes,highs,lows)
            # Build per-strategy scores for display
            strat_scores={k:v[0] for k,v in all_results.items()}
            scores[sym]={"score":sc,"strategy":name,"price":round(price,5),"rsi":rsi_v,
                         "checks":checks,"strat_scores":strat_scores}
            log(f"  {sym:<12} [{name}] {sc}/4  RSI:{rsi_v}  ₹{price:.4f}")
            if sc>best_score or (sc==best_score and sc>=min_sc):
                best_score=sc; best_sym=sym; best_strat=name
                best_cfg=STRATEGIES[name]; best_price=price
        except Exception as e:
            scores[sym]={"score":-1,"strategy":"ERR","price":None,"rsi":None,"checks":{},"strat_scores":{}}
            log(f"  {sym} error: {e}")

    state["coin_scores"]=scores

    # ── Detect market trend ───────────────────────────
    if scores:
        avg_rsi=sum(v["rsi"] for v in scores.values() if v.get("rsi")) / max(1,sum(1 for v in scores.values() if v.get("rsi")))
        state["market_trend"]="BULLISH 🟢" if avg_rsi>55 else "BEARISH 🔴" if avg_rsi<40 else "NEUTRAL ⚪"

    min_needed=best_cfg["min_score"] if best_cfg else 2
    if best_score>=min_needed and best_price>0:
        tp=best_price*(1+best_cfg["tp"])
        sl=best_price*(1-best_cfg["sl"])
        log(f"🚀 BUY {best_sym} [{best_strat}] score:{best_score}/4 @ ₹{best_price:.5f} TP:₹{tp:.5f} SL:₹{sl:.5f}")
        result=place_order(best_sym,"BUY",per_trade,best_price)
        if "error" not in str(result).lower():
            state.update({"in_trade":True,"trade_sym":best_sym,"strategy":best_strat,
                          "entry_price":best_price,"tp_price":tp,"sl_price":sl,"live_pnl":0.0})
            log(f"✅ Order filled | Strategy: {best_strat} | {best_cfg['desc']}")
        else:
            log(f"❌ Order failed: {result}")
    else:
        log(f"⏳ Best signal: {best_sym} [{best_strat}] {best_score}/4 — waiting for min {min_needed}/4...")

    return jsonify({"action":"scanned","scores":scores})

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)
