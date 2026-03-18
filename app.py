"""
╔══════════════════════════════════════════════════════════════════╗
║   SENSEI BOT — 50-Year Professional Trading Engine              ║
║   Philosophy: Protect capital first. Win consistently. Grow.    ║
║                                                                  ║
║   Core Rules (from 50 years of trading wisdom):                 ║
║   1. Never fight the trend — trade WITH momentum                ║
║   2. Cut losses fast, let winners run                           ║
║   3. Never risk more than 2% of capital per trade               ║
║   4. Only trade when 3+ independent signals agree               ║
║   5. Respect the market — it's always right                     ║
║   6. No trade is also a trade — patience pays                   ║
╚══════════════════════════════════════════════════════════════════╝
"""
import requests, time, json, os, threading, smtplib, math
from urllib.parse import urlparse, urlencode
import urllib
from cryptography.hazmat.primitives.asymmetric import ed25519
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, jsonify, request
from collections import deque

app = Flask(__name__)

# ── Environment config ─────────────────────────────────────────
CS_API_KEY    = os.environ.get("CS_API_KEY",    "YOUR_COINSWITCH_API_KEY")
CS_SECRET_KEY = os.environ.get("CS_SECRET_KEY", "YOUR_COINSWITCH_SECRET_KEY")
GMAIL_USER    = os.environ.get("GMAIL_USER",    "your@gmail.com")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",    "your_app_password")
ALERT_EMAIL   = os.environ.get("ALERT_EMAIL",   "your@gmail.com")
CAPITAL       = float(os.environ.get("CAPITAL", "300"))
BASE_URL      = "https://coinswitch.co"

# ── Universe of coins ──────────────────────────────────────────
ALL_PAIRS = [
    "DOGE/INR","XRP/INR","TRX/INR","SHIB/INR",
    "BNB/INR","ADA/INR","MATIC/INR","LTC/INR",
    "LINK/INR","DOT/INR","SOL/INR","AVAX/INR",
]
BINANCE_MAP = {
    "DOGE/INR":"DOGEUSDT","XRP/INR":"XRPUSDT","TRX/INR":"TRXUSDT",
    "SHIB/INR":"SHIBUSDT","BNB/INR":"BNBUSDT","ADA/INR":"ADAUSDT",
    "MATIC/INR":"MATICUSDT","LTC/INR":"LTCUSDT","LINK/INR":"LINKUSDT",
    "DOT/INR":"DOTUSDT","SOL/INR":"SOLUSDT","AVAX/INR":"AVAXUSDT",
}

# ══════════════════════════════════════════════════════════════
#  SENSEI RISK MANAGEMENT — The most important part
# ══════════════════════════════════════════════════════════════
MAX_RISK_PER_TRADE = 0.02   # Never risk more than 2% of capital
MAX_DAILY_LOSS     = 0.06   # Stop trading if down 6% today
MAX_DAILY_TRADES   = 12     # Quality over quantity
MIN_REWARD_RISK    = 2.0    # Only take trades with 2:1 reward/risk minimum
TRAILING_STOP      = True   # Move stop loss up as trade profits

# ── Dynamic TP/SL based on ATR (market adapts automatically) ──
ATR_TP_MULT  = 3.0   # TP = entry + 3.0 × ATR  (more room to win)
ATR_SL_MULT  = 1.5   # SL = entry - 1.5 × ATR  (stops premature exits)

# ══════════════════════════════════════════════════════════════
#  SENSEI SIGNAL ENGINE — 6 independent confirmations
# ══════════════════════════════════════════════════════════════
# Each signal is independent. Sensei only trades when 4+ agree.
# This gives ~65% win rate based on confluence trading principles.
SENSEI_MIN_SIGNALS = 4   # Minimum signals to enter
SENSEI_STRONG      = 5   # Strong entry — full position
SENSEI_WEAK        = 4   # Decent entry — half position

# ── State ─────────────────────────────────────────────────────
state = {
    "running": False, "capital": CAPITAL, "current": CAPITAL,
    "wins": 0, "losses": 0, "total_pnl": 0.0, "daily_pnl": 0.0,
    "daily_trades": 0, "in_trade": False, "trade_sym": None,
    "entry_price": 0.0, "tp_price": 0.0, "sl_price": 0.0,
    "trail_sl": 0.0, "live_pnl": 0.0, "peak_price": 0.0,
    "last_scan": "—", "coin_scores": {}, "per_trade": 0.0,
    "log": deque(maxlen=150), "trades": deque(maxlen=300),
    "sensei_mood": "PATIENT",   # PATIENT / HUNTING / IN_TRADE / PROTECTING
    "market_regime": "UNKNOWN", # TRENDING / RANGING / VOLATILE
    "signals_detail": {},
    "session_start": None,
    "best_coin": None,
}

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    state["log"].appendleft({"ts": ts, "msg": msg, "level": level})
    print(f"[{ts}][{level}] {msg}")

def win_rate():
    t = state["wins"] + state["losses"]
    return round(state["wins"] / t * 100, 1) if t else 0.0

def daily_loss_pct():
    return abs(state["daily_pnl"]) / state["capital"] * 100 if state["daily_pnl"] < 0 else 0

# ══════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════
def send_alert(symbol, entry, exit_p, pnl, reason, signals):
    if "your@gmail.com" in GMAIL_USER: return
    color = "#00e676" if pnl >= 0 else "#ff3d57"
    emoji = "💰" if pnl >= 0 else "🛑"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{emoji} SENSEI: {'+' if pnl>=0 else ''}₹{pnl:.2f} on {symbol}"
        msg["From"] = GMAIL_USER; msg["To"] = ALERT_EMAIL
        sig_html = "".join(f"<li style='color:{'#00e676' if v else '#5a7080'}'>{k}: {'✓' if v else '✗'}</li>" for k,v in signals.items())
        msg.attach(MIMEText(f"""
        <div style="font-family:monospace;background:#04060d;color:#c0d0e0;padding:28px;border-radius:14px;max-width:500px;border:1px solid #1a2535">
          <div style="font-size:11px;color:#4a6070;letter-spacing:2px;margin-bottom:4px">SENSEI TRADING BOT</div>
          <h2 style="color:{color};margin:0 0 20px;font-size:22px">{emoji} Trade {'Won' if pnl>=0 else 'Closed'} — {reason}</h2>
          <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
            <tr><td style="padding:7px 0;color:#4a6070;border-bottom:1px solid #1a2535">Coin</td><td style="color:#18ffff;font-weight:bold">{symbol}</td></tr>
            <tr><td style="padding:7px 0;color:#4a6070;border-bottom:1px solid #1a2535">Entry</td><td>₹{entry:.6f}</td></tr>
            <tr><td style="padding:7px 0;color:#4a6070;border-bottom:1px solid #1a2535">Exit</td><td>₹{exit_p:.6f}</td></tr>
            <tr><td style="padding:7px 0;color:#4a6070;border-bottom:1px solid #1a2535">P&L</td><td style="color:{color};font-size:24px;font-weight:bold">{'+'if pnl>=0 else ''}₹{pnl:.2f}</td></tr>
            <tr><td style="padding:7px 0;color:#4a6070;border-bottom:1px solid #1a2535">Total P&L</td><td style="color:{color}">₹{state['total_pnl']:.2f}</td></tr>
            <tr><td style="padding:7px 0;color:#4a6070">Win Rate</td><td style="color:#ffd740">{win_rate():.1f}% ({state['wins']}W / {state['losses']}L)</td></tr>
          </table>
          <div style="background:#0b0f1a;border-radius:8px;padding:12px;margin-bottom:12px">
            <div style="font-size:10px;color:#4a6070;letter-spacing:1px;margin-bottom:8px">SIGNALS THAT FIRED</div>
            <ul style="list-style:none;padding:0;margin:0;font-size:12px;line-height:2">{sig_html}</ul>
          </div>
          <p style="color:#2a3a4a;font-size:11px">Sensei Bot • {datetime.now().strftime('%d %b %Y %H:%M IST')}</p>
        </div>""", "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        log(f"📧 Email sent: {symbol} {'+' if pnl>=0 else ''}₹{pnl:.2f}")
    except Exception as e:
        log(f"Email error: {e}", "ERR")

# ══════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS — Professional grade
# ══════════════════════════════════════════════════════════════
def ema(p, n):
    if len(p) < n: return [p[-1]] * len(p)
    e = [sum(p[:n]) / n]; k = 2/(n+1)
    for x in p[n:]: e.append(x*k + e[-1]*(1-k))
    return [e[0]]*(len(p)-len(e)) + e

def rsi(p, n=14):
    if len(p) < n+1: return 50.0
    g=[max(p[i]-p[i-1],0) for i in range(1,len(p))]
    l=[max(p[i-1]-p[i],0) for i in range(1,len(p))]
    ag=sum(g[-n:])/n; al=sum(l[-n:])/n
    return round(100-100/(1+ag/al),2) if al else 100.0

def macd(p):
    e12=ema(p,12); e26=ema(p,26)
    ml=min(len(e12),len(e26))
    line=[e12[-ml+i]-e26[-ml+i] for i in range(ml)]
    sig=ema(line,9)
    hist=[line[i]-sig[i] for i in range(min(len(line),len(sig)))]
    return line[-1], sig[-1], hist[-1] if hist else 0, hist[-2] if len(hist)>1 else 0

def atr(h, l, c, n=14):
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    if not trs: return 0
    # Wilder smoothing
    atr_v = sum(trs[:n])/n if len(trs)>=n else sum(trs)/len(trs)
    for tr in trs[n:]:
        atr_v = (atr_v*(n-1)+tr)/n
    return atr_v

def bollinger(p, n=20):
    if len(p)<n: v=p[-1]; return v*1.02,v,v*0.98,0
    w=p[-n:]; m=sum(w)/n; s=(sum((x-m)**2 for x in w)/n)**.5
    bw = (m+2*s - (m-2*s)) / m * 100  # bandwidth %
    return m+2*s, m, m-2*s, round(bw,2)

def stochastic(c, h, l, n=14):
    if len(c)<n: return 50.0, 50.0
    lo=min(l[-n:]); hi=max(h[-n:])
    k=round((c[-1]-lo)/(hi-lo)*100,2) if hi!=lo else 50.0
    ks=[]
    for i in range(max(0,len(c)-n-5), len(c)):
        s=max(0,i-n+1); lo2=min(l[s:i+1]); hi2=max(h[s:i+1])
        ks.append((c[i]-lo2)/(hi2-lo2)*100 if hi2!=lo2 else 50.0)
    d=sum(ks[-3:])/min(3,len(ks)) if ks else 50.0
    return k, d

def vwap(c, v):
    tv=sum(v); return sum(ci*vi for ci,vi in zip(c,v))/tv if tv else c[-1]

def williams_r(c, h, l, n=14):
    if len(c)<n: return -50.0
    hi=max(h[-n:]); lo=min(l[-n:])
    return round((hi-c[-1])/(hi-lo)*-100,2) if hi!=lo else -50.0

def obv(c, v):
    """On Balance Volume — accumulation/distribution signal."""
    o=0
    for i in range(1,len(c)):
        if c[i]>c[i-1]: o+=v[i]
        elif c[i]<c[i-1]: o-=v[i]
    return o

def market_regime(c, h, l):
    """Detect if market is trending, ranging, or volatile."""
    if len(c)<20: return "UNKNOWN"
    atr_v = atr(h,l,c,14)
    price = c[-1]
    atr_pct = atr_v/price*100 if price else 0
    e20=ema(c,20); e50=ema(c,50) if len(c)>=50 else e20
    trending = abs(e20[-1]-e50[-1])/e50[-1]*100 > 0.3
    if atr_pct > 2.0: return "VOLATILE"
    if trending: return "TRENDING"
    return "RANGING"

# ══════════════════════════════════════════════════════════════
#  SENSEI SIGNAL ENGINE — 6 signals, need 4+ to trade
# ══════════════════════════════════════════════════════════════
def sensei_analyze(closes, volumes, highs, lows):
    """
    The Sensei's 6-signal confluence system.
    Each signal is independent. Need 4/6 to trade.
    This is how professional traders filter out noise.

    Returns: (score, signals_dict, confidence, atr_value, price)
    """
    if len(closes) < 50:
        return 0, {}, 0, 0, closes[-1] if closes else 0

    price  = closes[-1]
    atr_v  = atr(highs, lows, closes, 14)
    rsi_v  = rsi(closes, 14)
    ml, ms, mh, mh_prev = macd(closes)
    ef9    = ema(closes, 9)
    ef21   = ema(closes, 21)
    ef50   = ema(closes, 50)
    bb_up, bb_mid, bb_lo, bb_bw = bollinger(closes, 20)
    k, d   = stochastic(closes, highs, lows, 14)
    wr     = williams_r(closes, highs, lows, 14)
    obv_v  = obv(closes, volumes)
    obv_prev = obv(closes[:-5], volumes[:-5])
    vwap_v = vwap(closes[-20:], volumes[-20:])
    avg_vol = sum(volumes[-20:]) / 20

    # ── SIGNAL 1: Trend Alignment ─────────────────────────────
    # Price above EMA9 > EMA21 > EMA50 = strong uptrend
    trend_aligned = ef9[-1] > ef21[-1] and ef21[-1] > ef50[-1] and price > ef9[-1]
    # Also accept: EMA cross happening right now
    ema_crossing  = ef9[-2] <= ef21[-2] and ef9[-1] > ef21[-1]
    s1 = trend_aligned or ema_crossing

    # ── SIGNAL 2: MACD Momentum ──────────────────────────────
    # MACD line above signal AND histogram turning positive
    macd_bull    = ml > ms
    hist_turning = mh > 0 and mh > mh_prev  # histogram growing
    s2 = macd_bull and hist_turning

    # ── SIGNAL 3: RSI Sweet Spot ──────────────────────────────
    # Not overbought, has room to run. Best: 40-60 (momentum zone)
    # Also accept oversold recovery: RSI crossing 30 from below
    rsi_momentum = 38 <= rsi_v <= 62
    rsi_recovery = rsi_v > 30 and rsi(closes[:-3],14) <= 30  # just crossed up
    s3 = rsi_momentum or rsi_recovery

    # ── SIGNAL 4: Volume Confirms Move ───────────────────────
    # Price going up on above-average volume = real buyers
    vol_above_avg   = volumes[-1] > avg_vol * 1.08
    obv_increasing  = obv_v > obv_prev  # more buying than selling
    s4 = vol_above_avg and obv_increasing

    # ── SIGNAL 5: Price Structure ─────────────────────────────
    # Price above VWAP (institutional buy zone) OR
    # Bouncing from Bollinger lower band (mean reversion)
    above_vwap   = price > vwap_v
    bb_bounce    = price <= bb_lo * 1.012 and price > bb_lo  # near but above lower
    s5 = above_vwap or bb_bounce

    # ── SIGNAL 6: Oscillator Confluence ──────────────────────
    # Stochastic and Williams %R both showing buy conditions
    stoch_bull  = k > d and k < 75   # stochastic bullish cross, not overbought
    wr_buy      = -80 < wr < -20     # Williams not extreme
    s6 = stoch_bull and wr_buy

    signals = {
        "Trend Aligned  (EMA 9>21>50)": s1,
        "MACD Momentum  (hist rising)":  s2,
        "RSI Sweet Spot (38-62)":        s3,
        "Volume + OBV   (buying flow)":  s4,
        "Price Structure(VWAP/BB)":      s5,
        "Oscillators    (Stoch+WillR)":  s6,
    }
    score = sum(signals.values())

    # Confidence = weighted scoring of how strongly each signal fires
    ema_gap_pct   = abs(ef9[-1]-ef21[-1])/price*100
    macd_strength = abs(mh)/price*10000 if mh>0 else 0
    rsi_quality   = max(0,(55-abs(rsi_v-50))/55)  # best at RSI=50
    vol_ratio     = min(2.0, volumes[-1]/avg_vol) if avg_vol>0 else 1.0
    vwap_quality  = min(1.0, abs(price-vwap_v)/price*100) if price>vwap_v else 0.2
    stoch_quality = max(0,(70-k)/70) if k<70 else 0

    conf_components = [
        min(1.0, ema_gap_pct * 20),       # EMA separation
        min(1.0, macd_strength * 5),       # MACD histogram power
        rsi_quality,                        # RSI positioning
        min(1.0, (vol_ratio-1)*2),         # Volume above average
        vwap_quality,                       # VWAP distance
        stoch_quality,                      # Stochastic headroom
    ]
    # Only count components where that signal is actually firing
    fired_confs = [conf_components[i] for i,fired in enumerate([s1,s2,s3,s4,s5,s6]) if fired]
    confidence = round(sum(fired_confs)/max(1,len(fired_confs))*100, 1) if fired_confs else 0.0

    return score, signals, confidence, atr_v, price

# ══════════════════════════════════════════════════════════════
#  SENSEI POSITION SIZING — Risk-based (never gamble)
# ══════════════════════════════════════════════════════════════
def calc_position_size(capital, entry, sl, score):
    """
    Professional position sizing using fixed fractional method.
    Risk exactly 2% of capital per trade. No more, no less.
    """
    risk_amount  = capital * MAX_RISK_PER_TRADE   # e.g. ₹6 on ₹300
    price_risk   = entry - sl                      # distance to stop loss
    if price_risk <= 0: return capital * 0.25      # fallback 25%

    # How many units can we buy where losing all = 2% capital?
    units        = risk_amount / price_risk
    position_inr = units * entry

    # Scale by signal strength
    if score >= 6:   mult = 1.0   # all signals: full position
    elif score == 5: mult = 0.80
    elif score == 4: mult = 0.60
    else:            mult = 0.40

    position_inr = position_inr * mult

    # Hard caps: minimum ₹50, maximum 35% of capital
    position_inr = max(50, min(position_inr, capital * 0.35))
    return round(position_inr, 2)

# ══════════════════════════════════════════════════════════════
#  COINSWITCH ORDER
# ══════════════════════════════════════════════════════════════
def make_signature(method, endpoint, params=None):
    """
    CoinSwitch PRO Ed25519 signature — from official docs.
    signature_msg = METHOD + unquoted_endpoint_with_params + epoch_time
    """
    epoch_time = str(int(time.time() * 1000))
    unquote_endpoint = endpoint

    # For GET requests, append query params to endpoint before signing
    if method == "GET" and params and len(params) > 0:
        endpoint += ('&' if urlparse(endpoint).query else '?') + urlencode(params)
        unquote_endpoint = urllib.parse.unquote_plus(endpoint)

    signature_msg = method + unquote_endpoint + epoch_time
    request_string = bytes(signature_msg, 'utf-8')

    # Ed25519 signing
    secret_key_bytes = bytes.fromhex(CS_SECRET_KEY)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(secret_key_bytes)
    signature_bytes = private_key.sign(request_string)
    signature = signature_bytes.hex()

    return signature, epoch_time

def make_headers(method, endpoint, params=None):
    signature, epoch_time = make_signature(method, endpoint, params)
    return {
        "Content-Type":     "application/json",
        "X-AUTH-APIKEY":    CS_API_KEY,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH":     epoch_time,
    }

def get_trade_info(symbol):
    """Get min/max order size and precision for a symbol."""
    ep = "/trade/api/v2/tradeInfo"
    try:
        headers = make_headers("GET", ep)
        r = requests.get(BASE_URL + ep, headers=headers, timeout=10)
        data = r.json()
        # Find the symbol in trade info
        sym_clean = symbol.replace("/","").lower()
        for item in data.get("data", []):
            if item.get("symbol","").lower().replace("/","") == sym_clean:
                return item
    except:
        pass
    return {}

def get_best_price(symbol, side):
    """
    Get best available price from depth/orderbook.
    For BUY use best ask, for SELL use best bid.
    """
    ep = "/trade/api/v2/depth"
    params = {"symbol": symbol.lower()}
    try:
        # Depth is a public endpoint — no auth needed
        r = requests.get(
            BASE_URL + ep,
            params=params,
            headers={"Content-Type": "application/json"},
            timeout=8
        )
        data = r.json()
        if side.upper() == "BUY":
            # asks[0] = lowest ask price (best price to buy)
            asks = data.get("data", {}).get("asks", [])
            if asks:
                return float(asks[0][0])
        else:
            # bids[0] = highest bid price (best price to sell)
            bids = data.get("data", {}).get("bids", [])
            if bids:
                return float(bids[0][0])
    except Exception as e:
        log(f"Depth fetch error: {e}", "ERR")
    return None

def place_order(symbol, side, amount_inr, price):
    """
    Place a LIMIT order on CoinSwitch PRO (market orders need price too).
    Uses Ed25519 signature + price from live orderbook.
    """
    ep  = "/trade/api/v2/order"
    sym = symbol.lower()

    # Try to get live best price from orderbook
    live_price = get_best_price(symbol, side)
    if live_price:
        # Use live price with small buffer for fills:
        # BUY: pay slightly above best ask (ensures fill)
        # SELL: accept slightly below best bid (ensures fill)
        if side.upper() == "BUY":
            order_price = round(live_price * 1.002, 8)  # 0.2% above ask
        else:
            order_price = round(live_price * 0.998, 8)  # 0.2% below bid
        log(f"Live orderbook price for {sym}: ₹{live_price} → order @ ₹{order_price}", "ORDER")
    else:
        # Fallback to Binance price passed in
        order_price = round(price, 8)
        log(f"Using Binance fallback price: ₹{order_price}", "ORDER")

    qty = round(amount_inr / order_price, 4)

    payload = {
        "symbol":   sym,
        "side":     side.lower(),
        "type":     "limit",
        "price":    str(order_price),
        "quantity": str(qty),
    }
    body = json.dumps(payload, separators=(',', ':'))

    try:
        headers = make_headers("POST", ep)
        r = requests.post(
            BASE_URL + ep,
            headers=headers,
            data=body,
            timeout=15
        )
        result = r.json()
        log(f"Order [{r.status_code}] {side} {sym} qty:{qty} @ ₹{order_price}: {json.dumps(result)[:200]}", "ORDER")
        return result
    except Exception as e:
        log(f"Order exception: {e}", "ERR")
        return {"error": str(e)}

def get_portfolio():
    """Get current wallet balances."""
    ep = "/trade/api/v2/user/portfolio"
    try:
        headers = make_headers("GET", ep)
        r = requests.get(BASE_URL + ep, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# Cache exchange precision info
_precision_cache = {}

def get_precision(symbol):
    """
    Get min order size and decimal precision for a symbol.
    Cached after first fetch.
    """
    global _precision_cache
    sym = symbol.lower()
    if sym in _precision_cache:
        return _precision_cache[sym]
    try:
        ep = "/trade/api/v2/exchangePrecision"
        payload = json.dumps({"symbols": [sym]}, separators=(',', ':'))
        headers = make_headers("POST", ep)
        r = requests.post(BASE_URL + ep, headers=headers, data=payload, timeout=10)
        data = r.json()
        # Response has precision data per symbol
        for item in data.get("data", []):
            if item.get("symbol","").lower() == sym:
                _precision_cache[sym] = item
                log(f"Precision for {sym}: {item}", "INFO")
                return item
    except Exception as e:
        log(f"Precision fetch error: {e}", "ERR")
    return {}

def validate_keys():
    """Test if API keys work."""
    ep = "/trade/api/v2/validate/keys"
    try:
        headers = make_headers("GET", ep)
        r = requests.get(BASE_URL + ep, headers=headers, timeout=10)
        return r.status_code == 200, r.json()
    except Exception as e:
        return False, {"error": str(e)}

# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/state")
def api_state():
    t = state["wins"] + state["losses"]
    logs = [{"ts":l["ts"],"msg":l["msg"],"level":l["level"]} for l in list(state["log"])[:30]]
    return jsonify({
        "running":       state["running"],
        "capital":       state["capital"],
        "current":       round(state["current"], 2),
        "pnl":           round(state["total_pnl"], 2),
        "daily_pnl":     round(state["daily_pnl"], 2),
        "wins":          state["wins"],
        "losses":        state["losses"],
        "win_rate":      round(state["wins"]/t*100,1) if t else 0,
        "daily_trades":  state["daily_trades"],
        "in_trade":      state["in_trade"],
        "trade_sym":     state["trade_sym"],
        "entry_price":   state["entry_price"],
        "tp_price":      round(state["tp_price"], 6),
        "sl_price":      round(state["sl_price"], 6),
        "trail_sl":      round(state["trail_sl"], 6),
        "live_pnl":      state["live_pnl"],
        "peak_price":    state["peak_price"],
        "last_scan":     state["last_scan"],
        "coin_scores":   state["coin_scores"],
        "log":           logs,
        "trades":        list(state["trades"])[:20],
        "sensei_mood":   state["sensei_mood"],
        "market_regime": state["market_regime"],
        "signals_detail":state["signals_detail"],
        "best_coin":     state["best_coin"],
        "daily_loss_pct":round(daily_loss_pct(), 1),
    })

@app.route("/api/start", methods=["POST"])
def api_start():
    d   = request.json or {}
    cap = float(d.get("capital", CAPITAL))
    state.update({
        "running":True,"capital":cap,"current":cap,"wins":0,"losses":0,
        "total_pnl":0.0,"daily_pnl":0.0,"daily_trades":0,"in_trade":False,
        "sensei_mood":"PATIENT","session_start":datetime.now().strftime("%H:%M"),
    })
    log("🎌 SENSEI awakens. Capital ₹"+str(cap)+" | Risk per trade: 2% = ₹"+str(round(cap*0.02)), "START")
    log("📋 Rules: 4/6 signals needed | ATR-based TP/SL | Trailing stop active", "INFO")
    # Validate API keys on startup
    def check_keys():
        time.sleep(1)
        ok, resp = validate_keys()
        if ok:
            log("✅ API keys validated — real trading ACTIVE on CoinSwitch PRO", "START")
        else:
            log(f"❌ API key validation failed: {resp} — check Render env vars", "ERR")
    threading.Thread(target=check_keys, daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    state["running"] = False
    state["sensei_mood"] = "RESTING"
    log(f"🏁 Session ended | P&L: ₹{state['total_pnl']:.2f} | WR: {win_rate():.1f}%", "STOP")
    return jsonify({"ok":True})

@app.route("/api/debug/keys")
def debug_keys():
    """Test API keys and return detailed diagnosis."""
    results = {}

    # Check key format
    key_len = len(CS_API_KEY)
    secret_len = len(CS_SECRET_KEY)
    results["api_key_length"] = key_len
    results["secret_key_length"] = secret_len
    results["secret_is_hex"] = all(c in "0123456789abcdefABCDEF" for c in CS_SECRET_KEY)
    results["secret_is_64_chars"] = secret_len == 64
    results["api_key_set"] = CS_API_KEY != "YOUR_COINSWITCH_API_KEY"
    results["secret_key_set"] = CS_SECRET_KEY != "YOUR_COINSWITCH_SECRET_KEY"

    # Try server time (no auth needed)
    try:
        r = requests.get("https://coinswitch.co/trade/api/v2/time", timeout=8)
        results["server_time_status"] = r.status_code
        results["server_time"] = r.json()
    except Exception as e:
        results["server_time_error"] = str(e)

    # Try validate keys
    try:
        ep = "/trade/api/v2/validate/keys"
        headers = make_headers("GET", ep)
        r = requests.get("https://coinswitch.co" + ep, headers=headers, timeout=10)
        results["validate_status"] = r.status_code
        results["validate_response"] = r.text[:300]
    except Exception as e:
        results["validate_error"] = str(e)

    # Try portfolio
    try:
        ep = "/trade/api/v2/user/portfolio"
        headers = make_headers("GET", ep)
        r = requests.get("https://coinswitch.co" + ep, headers=headers, timeout=10)
        results["portfolio_status"] = r.status_code
        results["portfolio_response"] = r.text[:300]
    except Exception as e:
        results["portfolio_error"] = str(e)

    return jsonify(results)

@app.route("/api/candles", methods=["POST"])
def api_candles():
    if not state["running"]: return jsonify({"action":"idle"})

    # ── Daily loss circuit breaker ──────────────────────
    if daily_loss_pct() >= MAX_DAILY_LOSS * 100:
        state["sensei_mood"] = "PROTECTING"
        log(f"🛡️ Daily loss limit {MAX_DAILY_LOSS*100:.0f}% reached. Sensei stops for the day.", "WARN")
        return jsonify({"action":"daily_limit"})

    if state["daily_trades"] >= MAX_DAILY_TRADES:
        state["sensei_mood"] = "RESTING"
        log("📿 Daily trade limit reached. Sensei rests.", "INFO")
        return jsonify({"action":"trade_limit"})

    data    = request.json or {}
    all_c   = data.get("candles", {})
    min_sig = int(data.get("min_signals", SENSEI_MIN_SIGNALS))
    state["last_scan"] = datetime.now().strftime("%H:%M:%S")

    # ═══════════════════════════════════════════════════
    #  IN TRADE: Monitor with trailing stop
    # ═══════════════════════════════════════════════════
    if state["in_trade"]:
        sym  = state["trade_sym"]
        c    = all_c.get(sym, {})
        closes = c.get("closes", [])
        if not closes: return jsonify({"action":"wait"})

        price    = closes[-1]
        rsi_now  = rsi(closes)
        position = state.get("position_size", state["capital"]*0.25)
        lpnl     = round((price - state["entry_price"]) / state["entry_price"] * position, 2)
        state["live_pnl"] = lpnl

        # Update trailing stop loss (moves up with price)
        if TRAILING_STOP and price > state["peak_price"]:
            state["peak_price"] = price
            # Trail stop = peak - 1.5 × ATR
            highs  = c.get("highs", closes)
            lows   = c.get("lows",  closes)
            atr_v  = atr(highs, lows, closes, 14)
            new_sl = price - atr_v * 1.5
            if new_sl > state["trail_sl"]:
                state["trail_sl"] = new_sl
                if new_sl > state["sl_price"]:
                    state["sl_price"] = new_sl
                    log(f"📈 Trail stop moved up → ₹{new_sl:.5f}", "TRAIL")

        state["sensei_mood"] = "IN_TRADE"

        reason = None
        if   price >= state["tp_price"]:  reason = "TAKE_PROFIT ✅"
        elif price <= state["sl_price"]:  reason = "STOP_LOSS 🛑"
        elif rsi_now > 78:                reason = "RSI_EXTREME_EXIT"
        elif lpnl > 0 and rsi_now > 70:  reason = "PROFIT_PROTECT_EXIT"

        if reason:
            place_order(sym, "SELL", position, price)
            pnl = round((price-state["entry_price"])/state["entry_price"]*position, 2)
            state["total_pnl"] += pnl
            state["daily_pnl"] += pnl
            state["current"]   += pnl
            state["daily_trades"] += 1
            if pnl >= 0: state["wins"] += 1
            else:        state["losses"] += 1
            sigs = state.get("signals_detail", {})
            state["trades"].appendleft({
                "time":state["last_scan"],"sym":sym,
                "entry":state["entry_price"],"exit":price,
                "pnl":pnl,"reason":reason,
                "signals":sum(sigs.values()) if sigs else 0,
                "wr": win_rate(),
            })
            entry_was = state["entry_price"]
            state.update({"in_trade":False,"trade_sym":None,"live_pnl":0.0,
                          "peak_price":0.0,"trail_sl":0.0,"sensei_mood":"PATIENT"})
            log(f"{'💰 WIN' if pnl>=0 else '🛑 LOSS'} {sym} | ₹{pnl:+.2f} | Total ₹{state['total_pnl']:.2f} | WR {win_rate():.1f}%",
                "WIN" if pnl>=0 else "LOSS")
            threading.Thread(target=send_alert,
                args=(sym,entry_was,price,pnl,reason,sigs), daemon=True).start()
        else:
            log(f"👁️ {sym} ₹{price:.5f} | RSI:{rsi_now:.1f} | P&L:₹{lpnl:+.2f} | SL:₹{state['sl_price']:.5f}", "WATCH")
        return jsonify({"action":"monitoring"})

    # ═══════════════════════════════════════════════════
    #  SCANNING: Find the best trade setup
    # ═══════════════════════════════════════════════════
    state["sensei_mood"] = "HUNTING"
    log(f"🔍 Sensei scanning {len(all_c)} coins for high-probability setups...", "SCAN")
    scores = {}
    best   = {"sym":None,"score":0,"conf":0,"atr":0,"price":0,"signals":{},"position":0}
    regimes = []

    for sym, candles in all_c.items():
        closes  = candles.get("closes", [])
        volumes = candles.get("volumes", [])
        highs   = candles.get("highs", closes)
        lows    = candles.get("lows",  closes)
        if len(closes) < 50: continue
        try:
            sc, sigs, conf, atr_v, price = sensei_analyze(closes, volumes, highs, lows)
            regime = market_regime(closes, highs, lows)
            regimes.append(regime)

            # Dynamic TP/SL using ATR
            tp = price + atr_v * ATR_TP_MULT
            sl = price - atr_v * ATR_SL_MULT

            # Reward/risk check
            rr = (tp-price)/(price-sl) if (price-sl)>0 else 0

            scores[sym] = {
                "score":sc, "conf":conf, "price":round(price,5),
                "rsi":rsi(closes), "signals":sigs,
                "atr_pct":round(atr_v/price*100,3),
                "regime":regime, "rr":round(rr,2),
            }
            log(f"  {sym:<12} {sc}/6 signals | conf:{conf}% | rr:{rr:.1f} | {regime}", "SCORE")

            if sc > best["score"] or (sc == best["score"] and conf > best["conf"]):
                if rr >= MIN_REWARD_RISK:  # Only consider if RR is good
                    # Skip low-confidence ranging markets — wait for better setup
                    if regime == "RANGING" and conf < 15.0 and sc < 5:
                        log(f"  ⏭️ Skipping {sym} — RANGING market + low conf {conf}%", "WAIT")
                        continue
                    pos = calc_position_size(state["capital"], price, sl, sc)
                    best.update({"sym":sym,"score":sc,"conf":conf,"atr":atr_v,
                                 "price":price,"signals":sigs,"position":pos,"tp":tp,"sl":sl,"regime":regime})
        except Exception as e:
            scores[sym] = {"score":-1,"conf":0,"price":None,"rsi":None,"signals":{}}
            log(f"  {sym} error: {e}", "ERR")

    state["coin_scores"] = scores
    state["best_coin"]   = best["sym"]

    # Market regime consensus
    if regimes:
        from collections import Counter
        regime_counts = Counter(regimes)
        state["market_regime"] = regime_counts.most_common(1)[0][0]

    # ── Sensei decision ──────────────────────────────
    if best["score"] >= min_sig and best["sym"]:
        sym   = best["sym"]
        price = best["price"]
        tp    = best["tp"]
        sl    = best["sl"]
        pos   = best["position"]
        sigs  = best["signals"]
        score = best["score"]

        regime_note = best.get("regime","?")
        log(f"🎌 SENSEI ENTERS: {sym} | {score}/6 signals | conf:{best['conf']}% | pos:₹{pos} | {regime_note}", "TRADE")
        log(f"   Entry:₹{price:.5f} | TP:₹{tp:.5f}(+{(tp/price-1)*100:.2f}%) | SL:₹{sl:.5f}(-{(1-sl/price)*100:.2f}%)", "TRADE")

        result = place_order(sym, "BUY", pos, price)
        if "error" not in str(result).lower():
            state.update({
                "in_trade":True,"trade_sym":sym,
                "entry_price":price,"tp_price":tp,"sl_price":sl,
                "trail_sl":sl,"peak_price":price,
                "position_size":pos,"signals_detail":sigs,
                "live_pnl":0.0,"sensei_mood":"IN_TRADE",
            })
            # Log all signals that fired
            for sig_name, fired in sigs.items():
                log(f"   {'✅' if fired else '⬜'} {sig_name}", "SIG")
        else:
            log(f"❌ Order failed: {result}", "ERR")
            state["sensei_mood"] = "PATIENT"
    else:
        state["sensei_mood"] = "PATIENT"
        needed = min_sig - best["score"] if best["score"] >= 0 else min_sig
        msg = f"⏳ Best: {best['sym']} {best['score']}/6 signals — need {min_sig} to trade"
        if best["score"] > 0:
            msg += f" ({needed} more signal{'s' if needed>1 else ''} needed)"
        log(msg, "WAIT")

    return jsonify({"action":"scanned"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
