# CoinSwitch Smart Bot — Free Web Dashboard

Live trading bot with web UI + email alerts, hosted FREE on Render.com.

---

## Files
```
app.py              ← Bot engine + Flask web server
templates/index.html← Live dashboard UI
requirements.txt    ← Python packages
render.yaml         ← Render deployment config
```

---

## Deploy FREE on Render.com (10 minutes)

### Step 1 — Put files on GitHub
1. Go to https://github.com → Sign up free → New repository
2. Name it `coinswitch-bot` → Create
3. Upload all 4 files: app.py, templates/index.html, requirements.txt, render.yaml

### Step 2 — Deploy on Render
1. Go to https://render.com → Sign up free (use GitHub login)
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub → select `coinswitch-bot` repo
4. Render auto-detects settings from render.yaml → click **"Create Web Service"**
5. Wait ~3 minutes for deploy — you get a free URL like `https://coinswitch-bot.onrender.com`

### Step 3 — Set Environment Variables on Render
Go to your service → **Environment** tab → Add these:

| Key             | Value                        |
|-----------------|------------------------------|
| CS_API_KEY      | Your CoinSwitch PRO API key  |
| CS_SECRET_KEY   | Your CoinSwitch PRO Secret   |
| GMAIL_USER      | your@gmail.com               |
| GMAIL_PASS      | Your Gmail App Password*     |
| ALERT_EMAIL     | where to send alerts         |
| CAPITAL         | 300 (or your amount)         |

*Gmail App Password: https://myaccount.google.com/apppasswords
  → Google Account → Security → 2-Step Verification → App Passwords
  → Select app: Mail → Generate → copy the 16-character password

### Step 4 — Open Dashboard
Visit your Render URL → enter capital → click START BOT 🚀

---

## Email Alerts
You'll receive an email like this for every trade:

```
Subject: 💰 Bot Closed: +₹1.80 on DOGE/INR

Coin     : DOGE/INR
Entry    : ₹7.23400
Exit     : ₹7.27800
Reason   : TAKE_PROFIT
P&L      : +₹1.80
Total P&L: +₹4.20
Win Rate : 66.7%
```

---

## Important Notes
- Free Render plan sleeps after 15 min of no web traffic
- To keep it always-on: set up a free uptime monitor at https://uptimerobot.com
  → Add monitor → HTTP(s) → paste your Render URL → every 5 minutes
- Never share your API keys publicly
- ⚠️ Crypto trading involves risk — never invest what you can't afford to lose
