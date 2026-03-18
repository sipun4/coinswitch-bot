"""
╔══════════════════════════════════════════════════════════════════╗
║  SENSEI BOT v5 — Built from CoinSwitch API docs ground-up        ║
║  Source: https://api-trading.coinswitch.co/                      ║
║                                                                  ║
║  KEY FACTS FROM DOCS:                                            ║
║  • Symbol format:  "BTC/INR"  (UPPERCASE with slash)             ║
║  • Exchange:       "coinswitchx" (primary INR exchange)          ║
║  • Order type:     "limit" with price required                   ║
║  • Signature:      Ed25519, message = METHOD + endpoint + epoch  ║
║  • Headers:        X-AUTH-APIKEY, X-AUTH-SIGNATURE, X-AUTH-EPOCH ║
║  • Portfolio:      GET /trade/api/v2/user/portfolio               ║
║  • Precision:      POST /trade/api/v2/exchangePrecision           ║
╚══════════════════════════════════════════════════════════════════╝
"""

import requests, time, json, os, threading, smtplib, math, secrets
from datetime import datetime
from urllib.parse import urlparse, urlencode
from functools import wraps
import urllib
from cryptography.hazmat.primitives.asymmetric import ed25519
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from collections import deque

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ══════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════
CS_API_KEY    = os.environ.get("CS_API_KEY",    "YOUR_API_KEY")
CS_SECRET_KEY = os.environ.get("CS_SECRET_KEY", "YOUR_SECRET_KEY")
GMAIL_USER    = os.environ.get("GMAIL_USER",    "your@gmail.com")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",    "your_app_password")
ALERT_EMAIL   = os.environ.get("ALERT_EMAIL",   "your@gmail.com")
CAPITAL       = float(os.environ.get("CAPITAL", "300"))
ACCESS_KEY    = os.environ.get("ACCESS_KEY",    "sensei2024")
BASE_URL      = "https://coinswitch.co"
EXCHANGE      = "coinswitchx"   # Default exchange
EXCHANGES     = ["coinswitchx", "wazirx"]  # All supported exchanges

# Desired pairs — bot auto-discovers which are available on coinswitchx
# MATIC was rebranded to POL in Sept 2024
DESIRED_PAIRS = [
    "DOGE/INR", "XRP/INR",  "TRX/INR",  "SHIB/INR",
    "BNB/INR",  "ADA/INR",  "POL/INR",  "LTC/INR",
    "LINK/INR", "DOT/INR",  "SOL/INR",  "AVAX/INR",
    "BTC/INR",  "ETH/INR",  "PEPE/INR", "WIF/INR",
    "SUI/INR",  "TON/INR",  "APT/INR",  "NEAR/INR",
]
# Will be populated by discover_exchange_map() on startup
# Maps symbol -> exchange: {"MATIC/INR": "wazirx", "DOGE/INR": "coinswitchx", ...}
SYMBOL_EXCHANGE_MAP = {}
ALL_PAIRS = list(DESIRED_PAIRS)  # Active pairs (filtered to available ones)

# Strategy settings
ATR_TP_MULT        = 2.0
ATR_SL_MULT        = 1.0
MIN_REWARD_RISK    = 1.5
SENSEI_MIN_SIGNALS = 4
MAX_DAILY_TRADES   = 12
MAX_DAILY_LOSS_PCT = 0.06
MIN_ORDER_INR      = 100.0   # Safe buffer above CoinSwitch ₹50 minimum

# ══════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════
state = {
    "running": False, "capital": CAPITAL, "current": CAPITAL,
    "wins": 0, "losses": 0, "total_pnl": 0.0, "daily_pnl": 0.0,
    "daily_trades": 0, "in_trade": False, "trade_sym": None,
    "entry_price": 0.0, "tp_price": 0.0, "sl_price": 0.0,
    "trail_sl": 0.0, "live_pnl": 0.0, "peak_price": 0.0,
    "per_trade": MIN_ORDER_INR,
    "last_scan": "—", "coin_scores": {}, "position_size": 0.0,
    "log": deque(maxlen=150), "trades": deque(maxlen=300),
    "sensei_mood": "PATIENT", "market_regime": "UNKNOWN",
    "signals_detail": {}, "best_coin": None,
    "session_start": None,
    "wallet": {
        "inr_balance": 0.0, "inr_locked": 0.0, "total_value": 0.0,
        "holdings": {}, "last_updated": "—",
        "session_start_inr": 0.0, "session_pnl": 0.0,
    },
    "precision_cache": {},   # {symbol: {base, quote, limit}}
    "open_order_id": None,   # Track open order for status checking
}

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    state["log"].appendleft({"ts": ts, "msg": msg, "level": level})
    print(f"[{ts}][{level}] {msg}")

def win_rate():
    t = state["wins"] + state["losses"]
    return round(state["wins"] / t * 100, 1) if t else 0.0

def daily_loss_pct():
    return abs(state["daily_pnl"]) / max(state["capital"], 1) * 100 if state["daily_pnl"] < 0 else 0

# ══════════════════════════════════════════════════════════
#  AUTH — Ed25519 exactly as per CoinSwitch docs
# ══════════════════════════════════════════════════════════
def make_headers(method, endpoint, params=None):
    """
    Signature per docs:
      GET:  message = method + unquoted_endpoint_with_params + epoch_time
      POST/DELETE: message = method + endpoint + epoch_time
    """
    epoch_time      = str(int(time.time() * 1000))
    unquote_endpoint = endpoint

    if method == "GET" and params and len(params) > 0:
        endpoint         += ('&' if urlparse(endpoint).query else '?') + urlencode(params)
        unquote_endpoint  = urllib.parse.unquote_plus(endpoint)

    signature_msg    = method + unquote_endpoint + epoch_time
    request_string   = bytes(signature_msg, 'utf-8')
    secret_key_bytes = bytes.fromhex(CS_SECRET_KEY)
    private_key      = ed25519.Ed25519PrivateKey.from_private_bytes(secret_key_bytes)
    signature        = private_key.sign(request_string).hex()

    return {
        "Content-Type":     "application/json",
        "X-AUTH-APIKEY":    CS_API_KEY,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH":     epoch_time,
    }, endpoint   # return modified endpoint for GET requests

# ══════════════════════════════════════════════════════════
#  COINSWITCH API CALLS — all in one place, no scattered code
# ══════════════════════════════════════════════════════════

def cs_get(endpoint, params=None):
    """Authenticated GET request to CoinSwitch."""
    headers, full_ep = make_headers("GET", endpoint, params)
    r = requests.get(BASE_URL + full_ep, headers=headers, timeout=12)
    return r.status_code, r.json()

def cs_post(endpoint, payload):
    """Authenticated POST request to CoinSwitch."""
    headers, _ = make_headers("POST", endpoint)
    body = json.dumps(payload, separators=(',', ':'))
    r = requests.post(BASE_URL + endpoint, headers=headers, data=body, timeout=12)
    return r.status_code, r.json()

def cs_delete(endpoint, payload):
    """Authenticated DELETE request — signature includes sorted payload per docs."""
    epoch_time = str(int(time.time() * 1000))
    body_str   = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    sig_msg    = "DELETE" + endpoint + body_str
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(CS_SECRET_KEY))
    signature   = private_key.sign(bytes(sig_msg, 'utf-8')).hex()
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": CS_API_KEY,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH": epoch_time,
    }
    r = requests.delete(BASE_URL + endpoint, headers=headers,
                        data=json.dumps(payload, separators=(',', ':')), timeout=12)
    return r.status_code, r.json()

def validate_keys():
    try:
        code, data = cs_get("/trade/api/v2/validate/keys")
        return code == 200 and "Valid" in data.get("message", "")
    except Exception as e:
        return False

def fetch_exchange_precision(symbol):
    """
    POST /trade/api/v2/exchangePrecision
    Returns {base: 5, quote: 2, limit: 0} — decimal places for qty/price/min
    """
    if symbol in state["precision_cache"]:
        return state["precision_cache"][symbol]
    try:
        sym_exch = SYMBOL_EXCHANGE_MAP.get(symbol, EXCHANGE)
        code, data = cs_post("/trade/api/v2/exchangePrecision",
                             {"exchange": sym_exch, "symbol": symbol})
        if code == 200:
            prec = data.get("data", {}).get(sym_exch, {}).get(symbol, {})
            if prec:
                state["precision_cache"][symbol] = prec
                log(f"Precision {symbol}: base={prec.get('base')} quote={prec.get('quote')} limit={prec.get('limit')}", "INFO")
                return prec
    except Exception as e:
        log(f"Precision fetch error: {e}", "ERR")
    return {"base": 4, "quote": 2, "limit": 0}

def fetch_depth(symbol):
    """
    GET /trade/api/v2/depth?symbol=SYMBOL
    Returns best bid/ask for accurate order pricing.
    """
    try:
        sym_exch = SYMBOL_EXCHANGE_MAP.get(symbol, EXCHANGE)
        code, data = cs_get("/trade/api/v2/depth",
                            {"symbol": symbol, "exchange": sym_exch})
        if code == 200:
            d = data.get("data", {})
            asks = d.get("asks", [])
            bids = d.get("bids", [])
            best_ask = float(asks[0][0]) if asks else None
            best_bid = float(bids[0][0]) if bids else None
            return best_ask, best_bid
    except:
        pass
    return None, None

def fetch_wallet():
    """GET /trade/api/v2/user/portfolio — live INR balance."""
    try:
        code, data = cs_get("/trade/api/v2/user/portfolio")
        if code != 200:
            log(f"Portfolio error [{code}]: {data}", "ERR")
            return
        items = data.get("data", [])
        inr_balance = inr_locked = 0.0
        holdings = {}
        for item in items:
            cur  = item.get("currency", "").upper()
            main = float(item.get("main_balance", 0) or 0)
            lock = float(item.get("blocked_balance", 0) or 0)
            if cur == "INR":
                inr_balance, inr_locked = main, lock
            elif main > 0 or lock > 0:
                holdings[cur] = {"qty": main, "locked": lock}

        w = state["wallet"]
        prev = w.get("inr_balance", 0)

        if w.get("session_start_inr", 0) == 0 and inr_balance > 0:
            w["session_start_inr"] = inr_balance + inr_locked
            log(f"💳 Wallet loaded — ₹{inr_balance:.2f} available + ₹{inr_locked:.2f} locked", "WALLET")
            log(f"💳 Session start: ₹{w['session_start_inr']:.2f}", "WALLET")

        if w["session_start_inr"] > 0:
            w["session_pnl"] = round((inr_balance + inr_locked) - w["session_start_inr"], 2)

        w.update({"inr_balance": inr_balance, "inr_locked": inr_locked,
                  "total_value": inr_balance + inr_locked,
                  "holdings": holdings, "last_updated": datetime.now().strftime("%H:%M:%S")})

        if abs(inr_balance - prev) > 0.01 and prev > 0:
            diff = inr_balance - prev
            log(f"{'📈' if diff>0 else '📉'} Wallet: ₹{inr_balance:.2f} ({'+' if diff>0 else ''}₹{diff:.2f})", "WALLET")
    except Exception as e:
        log(f"Wallet error: {e}", "ERR")

def place_order(symbol, side, amount_inr, fallback_price):
    """
    Place a limit order on CoinSwitch PRO.
    From docs: POST /trade/api/v2/order
    Required fields: symbol (UPPERCASE), side, type, price, quantity, exchange
    """
    # Step 1: Get live price from orderbook (most accurate)
    best_ask, best_bid = fetch_depth(symbol)
    if side.upper() == "BUY":
        order_price = best_ask * 1.001 if best_ask else fallback_price * 1.002
    else:
        order_price = best_bid * 0.999 if best_bid else fallback_price * 0.998

    # Step 2: Get exchange precision for correct decimal places
    prec       = fetch_exchange_precision(symbol)
    base_prec  = int(prec.get("base", 4))    # quantity decimal places
    quote_prec = int(prec.get("quote", 2))   # price decimal places

    # Step 3: Round price and quantity correctly
    order_price = round(order_price, quote_prec)
    raw_qty     = amount_inr / order_price
    quantity    = round(raw_qty, base_prec)

    # Step 4: Enforce minimum order size
    if amount_inr < MIN_ORDER_INR:
        log(f"⚠️ Raising ₹{amount_inr} → ₹{MIN_ORDER_INR} (minimum)", "WARN")
        amount_inr  = MIN_ORDER_INR
        quantity    = round(amount_inr / order_price, base_prec)

    # Step 5: Get correct exchange FIRST, then build payload
    symbol_exchange = SYMBOL_EXCHANGE_MAP.get(symbol, EXCHANGE)
    log(f"🔔 ORDER: {side} {symbol} exchange={symbol_exchange} qty={quantity} price={order_price}", "ORDER")

    payload = {
        "symbol":   symbol,
        "side":     side.lower(),
        "type":     "limit",
        "price":    order_price,
        "quantity": quantity,
        "exchange": symbol_exchange,
    }
    log(f"Payload: {json.dumps(payload)}", "ORDER")
    try:
        code, data = cs_post("/trade/api/v2/order", payload)
        log(f"Order [{code}]: {json.dumps(data)[:200]}", "ORDER")
        if code == 200:
            order_id = data.get("data", {}).get("order_id", "")
            state["open_order_id"] = order_id
            log(f"✅ Order placed! ID: {order_id}", "ORDER")
        return code, data
    except Exception as e:
        log(f"Order exception: {e}", "ERR")
        return 0, {"error": str(e)}

def cancel_order(order_id):
    """DELETE /trade/api/v2/order — cancel an open order."""
    try:
        code, data = cs_delete("/trade/api/v2/order", {"order_id": order_id})
        log(f"Cancel [{code}]: {data}", "ORDER")
        return code == 200
    except Exception as e:
        log(f"Cancel error: {e}", "ERR")
        return False

def get_order_status(order_id):
    """GET /trade/api/v2/order?order_id=X — check if order executed."""
    try:
        code, data = cs_get("/trade/api/v2/order", {"order_id": order_id})
        if code == 200:
            return data.get("data", {}).get("status", "UNKNOWN")
    except:
        pass
    return "UNKNOWN"

# ══════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════
def ema(p, n):
    if len(p) < n: return [p[-1]] * len(p)
    e = [sum(p[:n]) / n]; k = 2/(n+1)
    for x in p[n:]: e.append(x*k + e[-1]*(1-k))
    return [e[0]]*(len(p)-len(e)) + e

def calc_rsi(p, n=14):
    if len(p) < n+1: return 50.0
    g = [max(p[i]-p[i-1], 0) for i in range(1, len(p))]
    l = [max(p[i-1]-p[i], 0) for i in range(1, len(p))]
    ag = sum(g[-n:])/n; al = sum(l[-n:])/n
    return round(100-100/(1+ag/al), 2) if al else 100.0

def calc_macd(p):
    e12=ema(p,12); e26=ema(p,26)
    ml=min(len(e12),len(e26))
    line=[e12[-ml+i]-e26[-ml+i] for i in range(ml)]
    sig=ema(line,9)
    hist=[line[i]-sig[i] for i in range(min(len(line),len(sig)))]
    return line[-1], sig[-1], hist[-1] if hist else 0, hist[-2] if len(hist)>1 else 0

def calc_atr(h, l, c, n=14):
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    if not trs: return 0
    a = sum(trs[:n])/min(n,len(trs))
    for tr in trs[n:]: a = (a*(n-1)+tr)/n
    return a

def bollinger(p, n=20):
    if len(p)<n: v=p[-1]; return v*1.02,v,v*0.98
    w=p[-n:]; m=sum(w)/n; s=(sum((x-m)**2 for x in w)/n)**.5
    return m+2*s, m, m-2*s

def stochastic(c, h, l, n=14):
    if len(c)<n: return 50.0, 50.0
    lo=min(l[-n:]); hi=max(h[-n:])
    k=round((c[-1]-lo)/(hi-lo)*100,2) if hi!=lo else 50.0
    ks=[]
    for i in range(max(0,len(c)-n-5), len(c)):
        s=max(0,i-n+1); lo2=min(l[s:i+1]); hi2=max(h[s:i+1])
        ks.append((c[i]-lo2)/(hi2-lo2)*100 if hi2!=lo2 else 50.0)
    return k, sum(ks[-3:])/min(3,len(ks)) if ks else 50.0

def calc_vwap(c, v):
    tv=sum(v); return sum(ci*vi for ci,vi in zip(c,v))/tv if tv else c[-1]

def williams_r(c, h, l, n=14):
    if len(c)<n: return -50.0
    hi=max(h[-n:]); lo=min(l[-n:])
    return round((hi-c[-1])/(hi-lo)*-100, 2) if hi!=lo else -50.0

def calc_obv(c, v):
    o=0
    for i in range(1,len(c)):
        o += v[i] if c[i]>c[i-1] else (-v[i] if c[i]<c[i-1] else 0)
    return o

# ══════════════════════════════════════════════════════════
#  SENSEI 6-SIGNAL ENGINE
# ══════════════════════════════════════════════════════════
def sensei_analyze(closes, volumes, highs, lows):
    if len(closes) < 50: return 0, {}, 0, 0, closes[-1]
    price = closes[-1]
    atr_v = calc_atr(highs, lows, closes)
    rsi_v = calc_rsi(closes)
    ml, ms, mh, mh_prev = calc_macd(closes)
    ef9  = ema(closes, 9); ef21 = ema(closes, 21); ef50 = ema(closes, 50)
    bb_up, bb_mid, bb_lo = bollinger(closes)
    k, d = stochastic(closes, highs, lows)
    wr   = williams_r(closes, highs, lows)
    vwap_v = calc_vwap(closes[-20:], volumes[-20:])
    avg_vol = sum(volumes[-20:]) / 20
    obv_now  = calc_obv(closes, volumes)
    obv_prev = calc_obv(closes[:-5], volumes[:-5])

    c1 = (ef9[-2]<ef21[-2] and ef9[-1]>ef21[-1]) or (ef9[-1]>ef21[-1] and ef21[-1]>ef50[-1] and price>ef9[-1])
    c2 = mh > 0 and mh > mh_prev and ml > ms
    c3 = (38 <= rsi_v <= 62) or (rsi_v > 30 and calc_rsi(closes[:-3]) <= 30)
    c4 = volumes[-1] > avg_vol * 1.08 and obv_now > obv_prev
    c5 = price > vwap_v or (price <= bb_lo * 1.012)
    c6 = k > d and k < 75 and (-80 < wr < -20)

    signals = {
        "Trend EMA (9>21>50)":     c1,
        "MACD Hist rising":        c2,
        "RSI Sweet Spot (38-62)":  c3,
        "Volume + OBV flow":       c4,
        "VWAP / Bollinger":        c5,
        "Stoch + Williams R":      c6,
    }
    score = sum(signals.values())
    fired = [i for i,(k_,v) in enumerate([(c1,c1),(c2,c2),(c3,c3),(c4,c4),(c5,c5),(c6,c6)]) if v]
    conf_vals = [
        min(1.0, abs(ef9[-1]-ef21[-1])/price*200),
        min(1.0, abs(mh)/price*5000) if mh>0 else 0,
        max(0, 1-(abs(rsi_v-50)/50)),
        min(1.0, (volumes[-1]/avg_vol-1)*2) if volumes[-1]>avg_vol else 0,
        min(1.0, abs(price-vwap_v)/price*100),
        min(1.0, max(0,(75-k)/75)) if k<75 else 0,
    ]
    fired_confs = [conf_vals[i] for i in range(6) if [c1,c2,c3,c4,c5,c6][i]]
    confidence  = round(sum(fired_confs)/max(1,len(fired_confs))*100, 1)
    return score, signals, confidence, atr_v, price

def calc_position_size(capital, entry, sl_price, score):
    risk_amount  = capital * 0.02
    price_risk   = max(entry - sl_price, entry * 0.001)
    raw_position = (risk_amount / price_risk) * entry
    mult = {6:1.0, 5:0.85, 4:0.70}.get(score, 0.55)
    pos  = max(MIN_ORDER_INR, min(raw_position * mult, capital * 0.80))
    return round(pos, 2)

def market_regime(c, h, l):
    if len(c)<20: return "UNKNOWN"
    atr_v = calc_atr(h,l,c)
    if atr_v/c[-1]*100 > 2: return "VOLATILE"
    e20=ema(c,20); e50=ema(c,50) if len(c)>=50 else e20
    if abs(e20[-1]-e50[-1])/e50[-1]*100 > 0.3: return "TRENDING"
    return "RANGING"

# ══════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════
def send_alert(symbol, entry, exit_p, pnl, reason, signals):
    if "your@gmail.com" in GMAIL_USER: return
    color = "#00e676" if pnl >= 0 else "#ff3d57"
    emoji = "💰" if pnl >= 0 else "🛑"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{emoji} SENSEI: {'+' if pnl>=0 else ''}₹{pnl:.2f} on {symbol}"
        msg["From"] = GMAIL_USER; msg["To"] = ALERT_EMAIL
        sig_html = "".join(f"<li style='color:{'#00e676' if v else '#5a7080'}'>{k}: {'✓' if v else '✗'}</li>"
                          for k,v in signals.items())
        msg.attach(MIMEText(f"""
        <div style="font-family:monospace;background:#04060d;color:#c0d0e0;padding:24px;border-radius:12px;max-width:480px">
          <h2 style="color:{color}">{emoji} {reason}</h2>
          <table style="width:100%">
            <tr><td style="color:#4a6070">Coin</td><td><b>{symbol}</b></td></tr>
            <tr><td style="color:#4a6070">Entry</td><td>₹{entry:.6f}</td></tr>
            <tr><td style="color:#4a6070">Exit</td><td>₹{exit_p:.6f}</td></tr>
            <tr><td style="color:#4a6070">P&L</td><td style="color:{color};font-size:20px"><b>{'+'if pnl>=0 else ''}₹{pnl:.2f}</b></td></tr>
            <tr><td style="color:#4a6070">Total P&L</td><td>₹{state['total_pnl']:.2f}</td></tr>
            <tr><td style="color:#4a6070">Win Rate</td><td>{win_rate():.1f}%</td></tr>
          </table>
          <ul style="list-style:none;padding:0;font-size:11px;line-height:2">{sig_html}</ul>
        </div>""", "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        log(f"📧 Alert sent: {symbol}")
    except Exception as e:
        log(f"Email error: {e}", "ERR")

# ══════════════════════════════════════════════════════════
#  AUTH MIDDLEWARE
# ══════════════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login_page")) if request.method == "GET" else (jsonify({"error":"unauthorized"}), 401)
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        entered = (request.json or request.form).get("key", "")
        if entered == ACCESS_KEY:
            session["authenticated"] = True
            return jsonify({"ok": True}) if request.is_json else redirect(url_for("index"))
        return jsonify({"ok": False, "error": "Wrong access key"}), 401 if request.is_json else render_template("login.html", error="Wrong key.")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ══════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/state")
@login_required
def api_state():
    t = state["wins"] + state["losses"]
    return jsonify({
        "running":        state["running"],
        "capital":        state["capital"],
        "current":        round(state["current"], 2),
        "pnl":            round(state["total_pnl"], 2),
        "daily_pnl":      round(state["daily_pnl"], 2),
        "wins":           state["wins"],
        "losses":         state["losses"],
        "win_rate":       round(state["wins"]/t*100,1) if t else 0,
        "daily_trades":   state["daily_trades"],
        "in_trade":       state["in_trade"],
        "trade_sym":      state["trade_sym"],
        "entry_price":    state["entry_price"],
        "tp_price":       round(state["tp_price"], 8),
        "sl_price":       round(state["sl_price"], 8),
        "trail_sl":       round(state["trail_sl"], 8),
        "live_pnl":       state["live_pnl"],
        "peak_price":     state["peak_price"],
        "per_trade":      state["per_trade"],
        "last_scan":      state["last_scan"],
        "coin_scores":    state["coin_scores"],
        "log":            [{"ts":l["ts"],"msg":l["msg"],"level":l["level"]} for l in list(state["log"])[:30]],
        "trades":         list(state["trades"])[:20],
        "sensei_mood":    state["sensei_mood"],
        "market_regime":  state["market_regime"],
        "signals_detail": state["signals_detail"],
        "best_coin":      state["best_coin"],
        "daily_loss_pct": round(daily_loss_pct(), 1),
        "wallet": {
            "inr_balance":   round(state["wallet"]["inr_balance"], 2),
            "inr_locked":    round(state["wallet"]["inr_locked"], 2),
            "total_value":   round(state["wallet"]["total_value"], 2),
            "holdings":      state["wallet"]["holdings"],
            "last_updated":  state["wallet"]["last_updated"],
            "session_start": round(state["wallet"]["session_start_inr"], 2),
            "session_pnl":   round(state["wallet"]["session_pnl"], 2),
        },
    })

@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    d   = request.json or {}
    cap = float(d.get("capital", CAPITAL))
    # Scale per-trade to always meet MIN_ORDER_INR
    per_trade = max(MIN_ORDER_INR, round(cap * (0.80 if cap<=200 else 0.55 if cap<=500 else 0.35), 2))
    state.update({
        "running": True, "capital": cap, "current": cap,
        "wins": 0, "losses": 0, "total_pnl": 0.0, "daily_pnl": 0.0,
        "daily_trades": 0, "in_trade": False, "per_trade": per_trade,
        "sensei_mood": "PATIENT", "session_start": datetime.now().strftime("%H:%M"),
    })
    log(f"🎌 SENSEI v5 awakens | Capital ₹{cap} | Per trade ₹{per_trade} | Exchange: {EXCHANGE}", "START")
    log(f"📋 Signal threshold: {SENSEI_MIN_SIGNALS}/6 | Min order: ₹{MIN_ORDER_INR} | Max daily loss: {MAX_DAILY_LOSS_PCT*100:.0f}%", "INFO")

    def startup():
        global SYMBOL_EXCHANGE_MAP, ALL_PAIRS
        time.sleep(0.5)
        ok = validate_keys()
        if not ok:
            log("❌ API key validation FAILED — check Render env vars", "ERR")
            return
        log("✅ API keys valid — LIVE trading on CoinSwitch PRO", "START")
        time.sleep(0.2)

        # Build symbol→exchange map from CoinSwitch coins API
        sym_map = {}
        for exchange in EXCHANGES:
            try:
                code, data = cs_get("/trade/api/v2/coins", {"exchange": exchange})
                if code == 200:
                    coins = data.get("data", {}).get(exchange, [])
                    for coin in coins:
                        if coin not in sym_map:  # coinswitchx takes priority
                            sym_map[coin] = exchange
                    log(f"📋 {exchange}: {len(coins)} coins available", "INFO")
            except Exception as e:
                log(f"Coins fetch error {exchange}: {e}", "ERR")

        SYMBOL_EXCHANGE_MAP = sym_map

        # Find which desired pairs are actually available
        tradeable = [p for p in DESIRED_PAIRS if p in sym_map]

        if not tradeable:
            # Fallback: pick first 12 INR pairs from coinswitchx
            cs_coins = [c for c,e in sym_map.items() if c.endswith("/INR") and e=="coinswitchx"]
            tradeable = cs_coins[:12]
            log(f"⚠️ None of desired pairs found — using top {len(tradeable)} from coinswitchx", "WARN")

        ALL_PAIRS[:] = tradeable   # in-place update so module-level var changes
        log(f"✅ Exchange map: {len(sym_map)} coins available", "INFO")
        log(f"✅ Trading {len(ALL_PAIRS)} pairs: {', '.join(ALL_PAIRS)}", "INFO")

        # Show which desired pairs exist and which don't
        for pair in DESIRED_PAIRS:
            exch = sym_map.get(pair)
            if exch:
                log(f"  ✅ {pair} → {exch}", "INFO")
            else:
                log(f"  ❌ {pair} → NOT on any exchange (skip)", "WARN")
        time.sleep(0.2)
        fetch_wallet()
    threading.Thread(target=startup, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    state["running"] = False
    state["sensei_mood"] = "RESTING"
    log(f"🏁 Session ended | P&L: ₹{state['total_pnl']:.2f} | WR: {win_rate():.1f}%", "STOP")
    return jsonify({"ok": True})

@app.route("/api/candles", methods=["POST"])
@login_required
def api_candles():
    if not state["running"]: return jsonify({"action": "idle"})
    if daily_loss_pct() >= MAX_DAILY_LOSS_PCT * 100:
        state["sensei_mood"] = "PROTECTING"
        log("🛡 Daily loss limit reached. Sensei stops for the day.", "WARN")
        return jsonify({"action": "daily_limit"})
    if state["daily_trades"] >= MAX_DAILY_TRADES:
        state["sensei_mood"] = "RESTING"
        return jsonify({"action": "trade_limit"})

    data  = request.json or {}
    all_c = data.get("candles", {})
    min_s = int(data.get("min_signals", SENSEI_MIN_SIGNALS))
    state["last_scan"] = datetime.now().strftime("%H:%M:%S")
    per_trade = state["per_trade"]

    # ── IN TRADE: monitor + trailing stop ──────────────────
    if state["in_trade"]:
        sym    = state["trade_sym"]
        c      = all_c.get(sym, {})
        closes = c.get("closes", [])
        if not closes: return jsonify({"action": "wait"})

        price   = closes[-1]
        rsi_now = calc_rsi(closes)
        pos     = state.get("position_size", per_trade)
        lpnl    = round((price - state["entry_price"]) / state["entry_price"] * pos, 2)
        state["live_pnl"] = lpnl

        # Trailing stop — move up with price
        if price > state["peak_price"]:
            state["peak_price"] = price
            highs  = c.get("highs", closes)
            lows   = c.get("lows",  closes)
            atr_v  = calc_atr(highs, lows, closes)
            new_sl = price - atr_v * 1.5
            if new_sl > state["trail_sl"]:
                state["trail_sl"] = new_sl
                if new_sl > state["sl_price"]:
                    state["sl_price"] = new_sl
                    log(f"📈 Trail SL → ₹{new_sl:.6f}", "TRAIL")

        state["sensei_mood"] = "IN_TRADE"
        log(f"👁 {sym} ₹{price:.6f} | RSI:{rsi_now:.1f} | P&L:₹{lpnl:+.2f} | SL:₹{state['sl_price']:.6f}", "WATCH")

        reason = None
        if   price >= state["tp_price"]:  reason = "TAKE_PROFIT ✅"
        elif price <= state["sl_price"]:  reason = "STOP_LOSS 🛑"
        elif rsi_now > 78:                reason = "RSI_OVERBOUGHT"
        elif lpnl > 0 and rsi_now > 70:  reason = "PROFIT_PROTECT"

        if reason:
            code, result = place_order(sym, "SELL", pos, price)
            pnl = round((price - state["entry_price"]) / state["entry_price"] * pos, 2)
            state["total_pnl"]    += pnl
            state["daily_pnl"]    += pnl
            state["current"]      += pnl
            state["daily_trades"] += 1
            if pnl >= 0: state["wins"]   += 1
            else:        state["losses"] += 1
            state["trades"].appendleft({
                "time": state["last_scan"], "sym": sym,
                "entry": state["entry_price"], "exit": price,
                "pnl": pnl, "reason": reason, "signals": sum(state["signals_detail"].values()),
            })
            entry_was = state["entry_price"]
            sigs = state["signals_detail"].copy()
            state.update({"in_trade": False, "trade_sym": None, "live_pnl": 0.0,
                          "peak_price": 0.0, "trail_sl": 0.0, "sensei_mood": "PATIENT",
                          "open_order_id": None})
            log(f"{'💰 WIN' if pnl>=0 else '🛑 LOSS'} {sym} ₹{pnl:+.2f} | Total ₹{state['total_pnl']:.2f} | WR {win_rate():.1f}%",
                "WIN" if pnl>=0 else "LOSS")
            threading.Thread(target=fetch_wallet, daemon=True).start()
            threading.Thread(target=send_alert, args=(sym,entry_was,price,pnl,reason,sigs), daemon=True).start()
        return jsonify({"action": "monitoring"})

    # ── SCAN: find best trade ───────────────────────────────
    state["sensei_mood"] = "HUNTING"
    log(f"🔍 Scanning {len(all_c)} coins...", "SCAN")
    scores = {}
    best   = {"sym": None, "score": 0, "conf": 0, "atr": 0, "price": 0, "signals": {}, "pos": 0, "tp": 0, "sl": 0}
    regimes = []

    for sym, candles in all_c.items():
        closes  = candles.get("closes", [])
        volumes = candles.get("volumes", [])
        highs   = candles.get("highs", closes)
        lows    = candles.get("lows",  closes)
        if len(closes) < 50: continue
        try:
            sc, sigs, conf, atr_v, price = sensei_analyze(closes, volumes, highs, lows)
            reg = market_regime(closes, highs, lows)
            regimes.append(reg)
            tp  = price + atr_v * ATR_TP_MULT
            sl  = price - atr_v * ATR_SL_MULT
            rr  = (tp-price) / max(price-sl, price*0.0001)
            scores[sym] = {"score": sc, "conf": conf, "price": round(price,6),
                           "rsi": calc_rsi(closes), "signals": sigs, "rr": round(rr,2), "regime": reg}
            log(f"  {sym:<12} {sc}/6 conf:{conf}% rr:{rr:.1f} {reg}", "SCORE")
            if rr >= MIN_REWARD_RISK and (sc > best["score"] or (sc == best["score"] and conf > best["conf"])):
                pos = calc_position_size(state["capital"], price, sl, sc)
                best.update({"sym":sym,"score":sc,"conf":conf,"atr":atr_v,
                             "price":price,"signals":sigs,"pos":pos,"tp":tp,"sl":sl})
        except Exception as e:
            scores[sym] = {"score":-1,"conf":0,"price":None,"rsi":None,"signals":{}}
            log(f"  {sym} error: {e}", "ERR")

    state["coin_scores"] = scores
    state["best_coin"]   = best["sym"]
    if regimes:
        from collections import Counter
        state["market_regime"] = Counter(regimes).most_common(1)[0][0]

    if best["score"] >= min_s and best["sym"]:
        sym   = best["sym"]
        price = best["price"]
        pos   = best["pos"]
        sigs  = best["signals"]
        log(f"🎌 ENTERING: {sym} | {best['score']}/6 signals | conf:{best['conf']}% | ₹{pos}", "TRADE")
        for name, fired in sigs.items():
            log(f"  {'✅' if fired else '⬜'} {name}", "SIG")

        log(f"📤 Calling place_order for {sym} @ ₹{price:.6f} pos=₹{pos}", "TRADE")
        code, result = place_order(sym, "BUY", pos, price)
        log(f"📥 place_order returned code={code} result={str(result)[:150]}", "TRADE")
        if code == 200:
            entry = best["price"]
            state.update({
                "in_trade": True, "trade_sym": sym,
                "entry_price": entry, "tp_price": best["tp"], "sl_price": best["sl"],
                "trail_sl": best["sl"], "peak_price": entry,
                "position_size": pos, "signals_detail": sigs, "live_pnl": 0.0,
                "sensei_mood": "IN_TRADE",
            })
            log(f"✅ BUY filled | TP ₹{best['tp']:.6f} | SL ₹{best['sl']:.6f}", "TRADE")
        else:
            log(f"❌ Order failed [{code}]: {result}", "ERR")
            state["sensei_mood"] = "PATIENT"
    else:
        state["sensei_mood"] = "PATIENT"
        needed = min_s - best["score"] if best["score"] >= 0 else min_s
        log(f"⏳ Best: {best['sym']} {best['score']}/6 — need {min_s} ({needed} more)", "WAIT")
        overbought = sum(1 for v in scores.values() if v.get("rsi") and v["rsi"] > 70)
        if overbought > 7:
            log(f"⚠️ {overbought}/12 coins overbought (RSI>70) — waiting for cooldown", "WARN")

    return jsonify({"action": "scanned"})

@app.route("/api/wallet/refresh", methods=["POST"])
@login_required
def api_wallet_refresh():
    threading.Thread(target=fetch_wallet, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/test_symbol")
@login_required  
def api_test_symbol():
    """
    Try every possible symbol format with a tiny test order (₹0 qty)
    to find which format CoinSwitch actually accepts.
    Visit: your-url/api/test_symbol
    """
    import time
    results = {}
    
    # First: get the actual coins list to see exact format
    try:
        code, data = cs_get("/trade/api/v2/coins", {"exchange": EXCHANGE})
        coins = data.get("data", {}).get(EXCHANGE, [])
        results["coins_sample"] = coins[:10]
        results["matic_in_list"] = "MATIC/INR" in coins
        results["matic_lower"]   = "matic/inr" in coins
        results["coins_count"]   = len(coins)
    except Exception as e:
        results["coins_error"] = str(e)

    # Try exchangePrecision with different formats
    formats_to_try = ["MATIC/INR", "matic/inr", "MATICINR", "maticinr"]
    prec_results = {}
    for fmt in formats_to_try:
        try:
            code, data = cs_post("/trade/api/v2/exchangePrecision",
                                 {"exchange": EXCHANGE, "symbol": fmt})
            prec_results[fmt] = {"code": code, "data": str(data)[:100]}
        except Exception as e:
            prec_results[fmt] = {"error": str(e)}
        time.sleep(0.2)
    results["precision_tests"] = prec_results

    # Try depth with different formats
    depth_results = {}
    for fmt in formats_to_try:
        try:
            code, data = cs_get("/trade/api/v2/depth", {"symbol": fmt})
            depth_results[fmt] = {"code": code, "has_asks": bool(data.get("data",{}).get("asks"))}
        except Exception as e:
            depth_results[fmt] = {"error": str(e)}
        time.sleep(0.2)
    results["depth_tests"] = depth_results

    return jsonify(results)

@app.route("/api/debug")
@login_required
def api_debug():
    """Full API diagnostic — visit /api/debug in browser to check everything."""
    results = {}
    results["api_key_set"]    = CS_API_KEY != "YOUR_API_KEY"
    results["secret_key_set"] = CS_SECRET_KEY != "YOUR_SECRET_KEY"
    results["secret_is_hex"]  = all(c in "0123456789abcdefABCDEF" for c in CS_SECRET_KEY)
    results["secret_len"]     = len(CS_SECRET_KEY)
    try:
        code, data = cs_get("/trade/api/v2/time")
        results["server_time"] = data
    except Exception as e:
        results["server_time_error"] = str(e)
    try:
        ok = validate_keys()
        results["keys_valid"] = ok
    except Exception as e:
        results["keys_error"] = str(e)
    try:
        code, data = cs_get("/trade/api/v2/user/portfolio")
        results["portfolio_status"] = code
        results["portfolio"] = str(data)[:500]
    except Exception as e:
        results["portfolio_error"] = str(e)
    try:
        code, data = cs_get("/trade/api/v2/coins", {"exchange": EXCHANGE})
        coins = data.get("data", {}).get(EXCHANGE, [])
        results["coins_count"] = len(coins)
        results["matic_listed"] = "MATIC/INR" in coins
        results["sample_coins"] = coins[:5]
    except Exception as e:
        results["coins_error"] = str(e)
    return jsonify(results)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
