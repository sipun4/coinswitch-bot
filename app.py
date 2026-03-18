"""
CoinSwitch Trading Bot — Web Dashboard + Email Alerts
Architecture: Browser fetches crypto prices (bypasses server network blocks),
              sends candle data to Flask, Flask runs indicators + places orders.
Deploy FREE on Render.com
"""

import requests, time, hmac, hashlib, json, os, threading, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, jsonify, request
from collections import deque

app = Flask(__name__)

# ════════════════════════════════════════════════════
#  CONFIG — set via environment variables on Render
# ════════════════════════════════════════════════════
CS_API_KEY    = os.environ.get("CS_API_KEY",    "YOUR_COINSWITCH_API_KEY")
CS_SECRET_KEY = os.environ.get("CS_SECRET_KEY", "YOUR_COINSWITCH_SECRET_KEY")
GMAIL_USER    = os.environ.get("GMAIL_USER",    "your@gmail.com")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",    "your_app_password")
ALERT_EMAIL   = os.environ.get("ALERT_EMAIL",  "your@gmail.com")
CAPITAL       = float(os.environ.get("CAPITAL", "300"))

BASE_URL = "https://coinswitch.co"
ALL_PAIRS = ["DOGE/INR", "XRP/INR", "TRX/INR", "SHIB/INR"]

TAKE_PROFIT_PCT  = 0.006
STOP_LOSS_PCT    = 0.003
EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD       = 14
RSI_BUY_MAX      = 42
RSI_SELL_MIN     = 65
VOL_MULTIPLIER   = 1.15
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MIN_SCORE        = 4
MAX_DAILY_TRADES = 15

# ════════════════════════════════════════════════════
#  SHARED STATE
# ════════════════════════════════════════════════════
state = {
    "running":       False,
    "capital":       CAPITAL,
    "current":       CAPITAL,
    "wins":          0,
    "losses":        0,
    "total_pnl":     0.0,
    "daily_trades":  0,
    "in_trade":      False,
    "trade_sym":     None,
    "entry_price":   0.0,
    "tp_price":      0.0,
    "sl_price":      0.0,
    "live_pnl":      0.0,
    "last_scan":     "Not started",
    "coin_scores":   {},
    "log":           deque(maxlen=50),
    "trades":        deque(maxlen=100),
    "per_trade":     90.0,
}

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    state["log"].appendleft(entry)
    print(entry)

def win_rate():
    t = state["wins"] + state["losses"]
    return round(state["wins"] / t * 100, 1) if t else 0.0

# ════════════════════════════════════════════════════
#  EMAIL
# ════════════════════════════════════════════════════
def send_email(subject, body):
    if GMAIL_USER == "your@gmail.com":
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_EMAIL
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        log(f"📧 Alert sent: {subject}")
    except Exception as e:
        log(f"❌ Email failed: {e}")

def email_trade_alert(symbol, entry, exit_p, pnl, reason):
    color   = "#00e676" if pnl >= 0 else "#ff3d57"
    emoji   = "💰" if pnl >= 0 else "🛑"
    subject = f"{emoji} Bot Trade: ₹{pnl:+.2f} on {symbol}"
    body = f"""
    <div style="font-family:monospace;background:#060810;color:#c9d6e3;padding:24px;border-radius:12px;max-width:480px">
      <h2 style="color:{color};margin:0 0 16px">{emoji} Trade Closed</h2>
      <table style="width:100%;border-collapse:collapse">
        <tr><td style="padding:6px 0;color:#5a7080">Coin</td><td style="color:#fff;font-weight:bold">{symbol}</td></tr>
        <tr><td style="padding:6px 0;color:#5a7080">Entry</td><td>₹{entry:.5f}</td></tr>
        <tr><td style="padding:6px 0;color:#5a7080">Exit</td><td>₹{exit_p:.5f}</td></tr>
        <tr><td style="padding:6px 0;color:#5a7080">Reason</td><td>{reason}</td></tr>
        <tr><td style="padding:6px 0;color:#5a7080">P&L</td><td style="color:{color};font-size:20px;font-weight:bold">₹{pnl:+.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#5a7080">Total P&L</td><td style="color:{color}">₹{state["total_pnl"]:.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#5a7080">Win Rate</td><td>{win_rate():.1f}%</td></tr>
      </table>
      <p style="margin:16px 0 0;color:#3a4a5a;font-size:12px">CoinSwitch Smart Bot • {datetime.now().strftime("%d %b %Y %H:%M")}</p>
    </div>"""
    send_email(subject, body)

# ════════════════════════════════════════════════════
#  INDICATORS (pure Python, no external calls)
# ════════════════════════════════════════════════════
def ema(prices, period):
    if len(prices) < period:
        return [prices[-1]] * len(prices)
    e = [sum(prices[:period]) / period]
    k = 2 / (period + 1)
    for p in prices[period:]:
        e.append(p * k + e[-1] * (1 - k))
    pad = len(prices) - len(e)
    return [e[0]] * pad + e

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - 100 / (1 + ag / al), 2) if al else 100.0

def calc_macd(prices):
    e12 = ema(prices, MACD_FAST)
    e26 = ema(prices, MACD_SLOW)
    ml  = min(len(e12), len(e26))
    line = [e12[-ml+i] - e26[-ml+i] for i in range(ml)]
    sig  = ema(line, MACD_SIGNAL)
    return line[-1], sig[-1]

def bollinger(closes, period=20):
    if len(closes) < period:
        p = closes[-1]; return p*1.02, p, p*0.98
    w = closes[-period:]
    m = sum(w) / period
    s = (sum((x-m)**2 for x in w) / period) ** 0.5
    return m+2*s, m, m-2*s

# ════════════════════════════════════════════════════
#  SCORING ENGINE (receives candle data from browser)
# ════════════════════════════════════════════════════
def score_from_candles(closes, volumes, highs, lows):
    """Run all 5 indicators on provided candle data."""
    if len(closes) < 30:
        return -1, {}
    price = closes[-1]
    ef    = ema(closes, EMA_FAST)
    es    = ema(closes, EMA_SLOW)
    r     = calc_rsi(closes, RSI_PERIOD)
    ml, ms = calc_macd(closes)
    avg_v  = sum(volumes[-20:]) / 20
    _, _, bb_low = bollinger(closes)

    c1 = (ef[-2] < es[-2]) and (ef[-1] > es[-1])
    c2 = r < RSI_BUY_MAX
    c3 = ml > ms
    c4 = volumes[-1] > avg_v * VOL_MULTIPLIER
    c5 = price <= bb_low * 1.005

    return sum([c1,c2,c3,c4,c5]), {
        "price":  round(price, 5),
        "rsi":    r,
        "checks": {
            "EMA Cross ↑": c1, "RSI Oversold": c2,
            "MACD Bullish": c3, "Volume Spike": c4, "Near BB Low": c5
        }
    }

# ════════════════════════════════════════════════════
#  COINSWITCH ORDER
# ════════════════════════════════════════════════════
def make_headers(method, endpoint, payload=""):
    ts  = str(int(time.time() * 1000))
    msg = ts + method + endpoint + (payload or "")
    sig = hmac.new(CS_SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"X-AUTH-APIKEY": CS_API_KEY, "X-AUTH-SIGNATURE": sig,
            "X-AUTH-EPOCH": ts, "Content-Type": "application/json"}

def place_order(symbol, side, amount_inr, price):
    ep   = "/trade/api/v2/order"
    qty  = round(amount_inr / price, 6)
    body = json.dumps({"symbol": symbol.replace("/",""), "side": side.lower(),
                       "type": "market", "quantity": qty})
    hdrs = make_headers("POST", ep, body)
    try:
        r = requests.post(BASE_URL + ep, headers=hdrs, data=body, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ════════════════════════════════════════════════════
#  FLASK ROUTES
# ════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state")
def api_state():
    t = state["wins"] + state["losses"]
    return jsonify({
        "running":      state["running"],
        "capital":      state["capital"],
        "current":      round(state["current"], 2),
        "pnl":          round(state["total_pnl"], 2),
        "wins":         state["wins"],
        "losses":       state["losses"],
        "win_rate":     round(state["wins"]/t*100,1) if t else 0,
        "daily_trades": state["daily_trades"],
        "in_trade":     state["in_trade"],
        "trade_sym":    state["trade_sym"],
        "entry_price":  state["entry_price"],
        "tp_price":     round(state["tp_price"], 6),
        "sl_price":     round(state["sl_price"], 6),
        "live_pnl":     state["live_pnl"],
        "last_scan":    state["last_scan"],
        "coin_scores":  state["coin_scores"],
        "log":          list(state["log"])[:20],
        "trades":       list(state["trades"])[:10],
    })

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    cap  = float(data.get("capital", CAPITAL))
    state.update({
        "running": True, "capital": cap, "current": cap,
        "wins": 0, "losses": 0, "total_pnl": 0.0,
        "daily_trades": 0, "in_trade": False,
        "per_trade": round(cap * 0.30),
    })
    log(f"🚀 Bot started | Capital ₹{cap} | Per trade ₹{round(cap*0.30)}")
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    state["running"] = False
    log("⏹ Bot stopped.")
    return jsonify({"ok": True})

@app.route("/api/candles", methods=["POST"])
def api_candles():
    """
    Browser sends candle data here.
    We run indicators, decide BUY/SELL, place orders.
    This is the core trading engine.
    """
    if not state["running"]:
        return jsonify({"action": "idle"})

    if state["daily_trades"] >= MAX_DAILY_TRADES:
        return jsonify({"action": "limit_reached"})

    data       = request.json or {}
    all_candles = data.get("candles", {})  # {symbol: {closes,volumes,highs,lows}}
    state["last_scan"] = datetime.now().strftime("%H:%M:%S")
    per_trade  = state["per_trade"]

    # ── If in trade: check exit ──────────────────────
    if state["in_trade"]:
        sym     = state["trade_sym"]
        candles = all_candles.get(sym, {})
        closes  = candles.get("closes", [])
        if not closes:
            return jsonify({"action": "wait"})

        price  = closes[-1]
        r_now  = calc_rsi(closes)
        lpnl   = round((price - state["entry_price"]) / state["entry_price"] * per_trade, 2)
        state["live_pnl"] = lpnl
        log(f"📈 {sym} ₹{price:.5f} RSI {r_now} Live P&L ₹{lpnl:+.2f}")

        reason = None
        if   price >= state["tp_price"]:  reason = "TAKE_PROFIT"
        elif price <= state["sl_price"]:  reason = "STOP_LOSS"
        elif r_now > RSI_SELL_MIN:        reason = "RSI_OVERBOUGHT"

        if reason:
            log(f"{'💰' if reason=='TAKE_PROFIT' else '🛑'} {reason} — selling {sym} @ ₹{price:.5f}")
            place_order(sym, "SELL", per_trade, price)
            pnl = round((price - state["entry_price"]) / state["entry_price"] * per_trade, 2)
            state["total_pnl"]    += pnl
            state["current"]      += pnl
            state["daily_trades"] += 1
            if pnl >= 0: state["wins"]   += 1
            else:        state["losses"] += 1
            state["trades"].appendleft({
                "time": datetime.now().strftime("%H:%M"),
                "sym": sym, "entry": state["entry_price"],
                "exit": price, "pnl": pnl, "reason": reason
            })
            entry_was = state["entry_price"]
            state.update({"in_trade": False, "trade_sym": None, "live_pnl": 0.0})
            threading.Thread(target=email_trade_alert,
                args=(sym, entry_was, price, pnl, reason), daemon=True).start()
            log(f"Trade closed | P&L ₹{pnl:+.2f} | Total ₹{state['total_pnl']:.2f}")
        return jsonify({"action": "monitoring"})

    # ── Not in trade: score all coins ───────────────
    log("🔍 Scanning coins...")
    best_sym, best_score, best_det = None, -1, {}
    scores = {}

    for sym, candles in all_candles.items():
        closes  = candles.get("closes", [])
        volumes = candles.get("volumes", [])
        highs   = candles.get("highs", [])
        lows    = candles.get("lows", [])
        sc, det = score_from_candles(closes, volumes, highs, lows)
        scores[sym] = {"score": sc, "price": det.get("price"), "rsi": det.get("rsi"), "checks": det.get("checks", {})}
        log(f"  {sym} Score {sc}/5  RSI {det.get('rsi','?')}  ₹{det.get('price','?')}")
        if sc > best_score:
            best_score, best_sym, best_det = sc, sym, det

    state["coin_scores"] = scores

    if best_score >= MIN_SCORE and best_det.get("price"):
        price = best_det["price"]
        log(f"🚀 BUY signal on {best_sym} (score {best_score}/5) @ ₹{price:.5f}")
        result = place_order(best_sym, "BUY", per_trade, price)
        if "error" not in str(result).lower():
            tp = price * (1 + TAKE_PROFIT_PCT)
            sl = price * (1 - STOP_LOSS_PCT)
            state.update({
                "in_trade": True, "trade_sym": best_sym,
                "entry_price": price, "tp_price": tp, "sl_price": sl, "live_pnl": 0.0
            })
            log(f"✅ BUY @ ₹{price:.5f} | TP ₹{tp:.5f} | SL ₹{sl:.5f}")
        else:
            log(f"❌ Order failed: {result}")
    else:
        log(f"⏳ No strong signal (best {best_score}/5). Waiting...")

    return jsonify({"action": "scanned", "scores": scores})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
