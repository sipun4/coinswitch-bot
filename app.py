"""
CoinSwitch Trading Bot — Web Dashboard + Email Alerts
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
GMAIL_PASS    = os.environ.get("GMAIL_PASS",    "your_app_password")   # Gmail App Password
ALERT_EMAIL   = os.environ.get("ALERT_EMAIL",  "your@gmail.com")      # Who gets the alerts
CAPITAL       = float(os.environ.get("CAPITAL", "300"))

BASE_URL      = "https://coinswitch.co"
ALL_PAIRS     = ["DOGE/INR", "XRP/INR", "TRX/INR", "SHIB/INR"]
# Yahoo Finance symbols — works on virtually all servers, no API key needed
YF_MAP = {
    "DOGE/INR": "DOGE-INR",
    "XRP/INR":  "XRP-INR",
    "TRX/INR":  "TRX-INR",
    "SHIB/INR": "SHIB-INR",
}

TAKE_PROFIT_PCT = 0.006
STOP_LOSS_PCT   = 0.003
EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD      = 14
RSI_BUY_MAX     = 42
RSI_SELL_MIN    = 65
VOL_MULTIPLIER  = 1.15
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MIN_SCORE       = 4
MAX_DAILY_TRADES= 15
SLEEP_SECONDS   = 60

# ════════════════════════════════════════════════════
#  SHARED STATE (thread-safe via simple dict)
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
    "log":           deque(maxlen=50),   # recent log lines
    "trades":        deque(maxlen=100),  # trade history
}

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    state["log"].appendleft(entry)
    print(entry)


# ════════════════════════════════════════════════════
#  EMAIL ALERTS
# ════════════════════════════════════════════════════
def send_email(subject, body):
    if GMAIL_USER == "your@gmail.com":
        log("⚠️ Email not configured — skipping alert")
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
        log(f"📧 Email sent: {subject}")
    except Exception as e:
        log(f"❌ Email failed: {e}")

def email_trade_alert(kind, symbol, entry, exit_p, pnl, reason):
    color  = "#00c853" if pnl >= 0 else "#d50000"
    emoji  = "💰" if pnl >= 0 else "🛑"
    subject = f"{emoji} Bot {kind}: ₹{pnl:+.2f} on {symbol}"
    body = f"""
    <div style="font-family:monospace;background:#0a0a0a;color:#e0e0e0;padding:24px;border-radius:12px;max-width:480px">
      <h2 style="color:{color};margin:0 0 16px">{emoji} Trade {kind}</h2>
      <table style="width:100%;border-collapse:collapse">
        <tr><td style="padding:6px 0;color:#888">Coin</td><td style="color:#fff;font-weight:bold">{symbol}</td></tr>
        <tr><td style="padding:6px 0;color:#888">Entry</td><td>₹{entry:.5f}</td></tr>
        <tr><td style="padding:6px 0;color:#888">Exit</td><td>₹{exit_p:.5f}</td></tr>
        <tr><td style="padding:6px 0;color:#888">Reason</td><td>{reason}</td></tr>
        <tr><td style="padding:6px 0;color:#888">P&L</td><td style="color:{color};font-size:20px;font-weight:bold">₹{pnl:+.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#888">Total P&L</td><td style="color:{color}">₹{state['total_pnl']:.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#888">Win Rate</td><td>{win_rate():.1f}%</td></tr>
      </table>
      <p style="margin:16px 0 0;color:#555;font-size:12px">CoinSwitch Smart Bot • {datetime.now().strftime('%d %b %Y %H:%M')}</p>
    </div>
    """
    send_email(subject, body)


# ════════════════════════════════════════════════════
#  INDICATORS
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

def rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - 100 / (1 + ag / al), 2) if al else 100.0

def macd_val(prices):
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

def atr_val(highs, lows, closes, period=14):
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return sum(trs[-period:]) / min(len(trs), period) if trs else 0

def win_rate():
    t = state["wins"] + state["losses"]
    return round(state["wins"] / t * 100, 1) if t else 0.0


# ════════════════════════════════════════════════════
#  MARKET DATA (Binance public API → INR)
# ════════════════════════════════════════════════════
def fetch_candles(symbol, limit=60):
    """
    Fetch 1-minute OHLCV candles from Yahoo Finance.
    Yahoo Finance works on virtually all servers with no API key.
    Returns (closes, volumes, highs, lows) in INR.
    """
    yf_sym = YF_MAP.get(symbol)
    if not yf_sym:
        raise ValueError(f"Unknown symbol: {symbol}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    # Yahoo Finance v8 chart API — 1m candles, last 1 hour
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
    params = {
        "interval":  "1m",
        "range":     "1h",
        "includePrePost": "false",
    }
    try:
        r    = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        result = data["chart"]["result"][0]
        closes  = result["indicators"]["quote"][0]["close"]
        highs   = result["indicators"]["quote"][0]["high"]
        lows    = result["indicators"]["quote"][0]["low"]
        volumes = result["indicators"]["quote"][0]["volume"]

        # Clean out None values (Yahoo sometimes returns nulls)
        cleaned = [(c, h, l, v) for c, h, l, v in zip(closes, highs, lows, volumes)
                   if c is not None and h is not None and l is not None and v is not None]
        if len(cleaned) < 20:
            raise ValueError(f"Not enough clean candles: {len(cleaned)}")

        closes  = [x[0] for x in cleaned]
        highs   = [x[1] for x in cleaned]
        lows    = [x[2] for x in cleaned]
        volumes = [x[3] for x in cleaned]
        return closes[-limit:], volumes[-limit:], highs[-limit:], lows[-limit:]

    except Exception as e:
        raise ValueError(f"Yahoo Finance failed for {yf_sym}: {e}")


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
#  SCORING ENGINE
# ════════════════════════════════════════════════════
def score_coin(symbol):
    try:
        closes, volumes, highs, lows = fetch_candles(symbol)
    except Exception as e:
        return -1, {"error": str(e), "price": None, "rsi": None, "checks": {}}

    price = closes[-1]
    ef    = ema(closes, EMA_FAST)
    es    = ema(closes, EMA_SLOW)
    r     = rsi(closes, RSI_PERIOD)
    ml, ms = macd_val(closes)
    avg_v  = sum(volumes[-20:]) / 20
    _, _, bb_low = bollinger(closes)

    c1 = (ef[-2] < es[-2]) and (ef[-1] > es[-1])
    c2 = r < RSI_BUY_MAX
    c3 = ml > ms
    c4 = volumes[-1] > avg_v * VOL_MULTIPLIER
    c5 = price <= bb_low * 1.005

    return sum([c1,c2,c3,c4,c5]), {
        "price": round(price, 5),
        "rsi":   r,
        "checks": {"EMA Cross ↑": c1, "RSI Oversold": c2,
                   "MACD Bullish": c3, "Volume Spike": c4, "Near BB Low": c5}
    }


# ════════════════════════════════════════════════════
#  BOT LOOP (runs in background thread)
# ════════════════════════════════════════════════════
def bot_loop():
    per_trade = round(state["capital"] * 0.30)
    log(f"🚀 Bot started | Capital ₹{state['capital']} | Per trade ₹{per_trade}")

    in_trade    = False
    trade_sym   = None
    entry_price = 0.0
    tp_price    = 0.0
    sl_price    = 0.0

    while state["running"]:
        try:
            if state["daily_trades"] >= MAX_DAILY_TRADES:
                log(f"⏸ Daily limit {MAX_DAILY_TRADES} reached. Sleeping 1hr.")
                time.sleep(3600)
                continue

            state["last_scan"] = datetime.now().strftime("%H:%M:%S")

            # ── CASE 1: Looking for entry ────────────────────────
            if not in_trade:
                log("🔍 Scanning coins...")
                best_sym, best_score, best_det = None, -1, {}
                scores = {}
                for sym in ALL_PAIRS:
                    sc, det = score_coin(sym)
                    scores[sym] = {"score": sc, "price": det.get("price"), "rsi": det.get("rsi"), "checks": det.get("checks",{})}
                    log(f"  {sym} Score {sc}/5  RSI {det.get('rsi','?')}  ₹{det.get('price','?')}")
                    if sc > best_score:
                        best_score, best_sym, best_det = sc, sym, det
                state["coin_scores"] = scores

                if best_score >= MIN_SCORE and best_det.get("price"):
                    price = best_det["price"]
                    log(f"🚀 BUY signal on {best_sym} (score {best_score}/5) @ ₹{price:.5f}")
                    result = place_order(best_sym, "BUY", per_trade, price)
                    if "error" not in str(result).lower():
                        entry_price = price
                        tp_price    = price * (1 + TAKE_PROFIT_PCT)
                        sl_price    = price * (1 - STOP_LOSS_PCT)
                        trade_sym   = best_sym
                        in_trade    = True
                        state.update({"in_trade": True, "trade_sym": trade_sym,
                                      "entry_price": entry_price, "tp_price": tp_price,
                                      "sl_price": sl_price, "live_pnl": 0.0})
                        log(f"✅ BUY filled @ ₹{entry_price:.5f} | TP ₹{tp_price:.5f} | SL ₹{sl_price:.5f}")
                    else:
                        log(f"❌ Order failed: {result}")
                else:
                    log(f"⏳ No strong signal (best {best_score}/5). Waiting...")

            # ── CASE 2: In a trade — watch exit ─────────────────
            else:
                try:
                    closes, _, _, _ = fetch_candles(trade_sym)
                    price  = closes[-1]
                    r_now  = rsi(closes)
                    lpnl   = round((price - entry_price) / entry_price * per_trade, 2)
                    state["live_pnl"] = lpnl
                    log(f"📈 {trade_sym} ₹{price:.5f} | RSI {r_now} | Live P&L ₹{lpnl:+.2f}")
                except Exception as e:
                    log(f"⚠️ Data error: {e}"); time.sleep(30); continue

                reason = None
                if   price >= tp_price: reason = "TAKE_PROFIT"
                elif price <= sl_price: reason = "STOP_LOSS"
                elif r_now > RSI_SELL_MIN: reason = "RSI_OVERBOUGHT"

                if reason:
                    log(f"{'💰' if reason=='TAKE_PROFIT' else '🛑'} {reason} — selling {trade_sym} @ ₹{price:.5f}")
                    place_order(trade_sym, "SELL", per_trade, price)
                    pnl = round((price - entry_price) / entry_price * per_trade, 2)
                    state["total_pnl"]    += pnl
                    state["current"]      += pnl
                    state["daily_trades"] += 1
                    if pnl >= 0: state["wins"]   += 1
                    else:        state["losses"] += 1
                    state["trades"].appendleft({
                        "time": datetime.now().strftime("%H:%M"),
                        "sym": trade_sym, "entry": entry_price,
                        "exit": price, "pnl": pnl, "reason": reason
                    })
                    in_trade = False
                    state.update({"in_trade": False, "trade_sym": None, "live_pnl": 0.0})
                    log(f"Trade closed | P&L ₹{pnl:+.2f} | Total ₹{state['total_pnl']:.2f}")
                    email_trade_alert("Closed", trade_sym, entry_price, price, pnl, reason)

        except Exception as e:
            log(f"⚠️ Error: {e}")

        # sleep with early-exit check
        for _ in range(SLEEP_SECONDS):
            if not state["running"]: break
            time.sleep(1)

    log("⏹ Bot stopped.")


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
        "tp_price":     round(state["tp_price"], 5),
        "sl_price":     round(state["sl_price"], 5),
        "live_pnl":     state["live_pnl"],
        "last_scan":    state["last_scan"],
        "coin_scores":  state["coin_scores"],
        "log":          list(state["log"])[:20],
        "trades":       list(state["trades"])[:10],
    })

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    if not state["running"]:
        cap = float(data.get("capital", CAPITAL))
        state.update({"capital": cap, "current": cap, "running": True,
                      "wins": 0, "losses": 0, "total_pnl": 0.0,
                      "daily_trades": 0, "in_trade": False})
        t = threading.Thread(target=bot_loop, daemon=True)
        t.start()
        return jsonify({"ok": True, "msg": f"Bot started with ₹{cap}"})
    return jsonify({"ok": False, "msg": "Already running"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    state["running"] = False
    return jsonify({"ok": True, "msg": "Bot stopping..."})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
