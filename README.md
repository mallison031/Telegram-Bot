# Trading Chart Vision Bot

A Telegram bot that reads trading chart screenshots with Gemini Flash vision,
extracts the trade setup (entry, stop loss, take profit), calculates profit/loss
percentages in code, and replies with a formatted signal:

```
BTCUSD SELL LIMIT
ENTRY: 63552.40
SL: 63717.50
TP: 60361.53
Profit: +5.02% / Loss: -0.26%
```

See `telegram_bot_roadmap.md` for the full design.

## Setup (one time)

1. **Create the Telegram bot** — message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts, and copy the HTTP API token.

2. **Get a Gemini API key (free tier)** — go to
   [Google AI Studio](https://aistudio.google.com/apikey) and create an API key.

3. **Configure credentials** — put both keys in a `.env` file in the project
   root (never commit this file; it is gitignored):

   ```bash
   cp .env.example .env
   # edit .env and paste both keys
   ```

4. **Install dependencies:**

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

## How to run

From the project folder:

```bash
.venv/bin/python bot.py
```

Or, if you prefer activating the virtual environment first:

```bash
source .venv/bin/activate
python bot.py
```

You should see a log line like:

```
INFO - __main__ - Bot started, polling for updates...
```

The bot is now live. Open your bot on Telegram (e.g. **@Signal_Texter_bot**),
send `/start`, then send a chart screenshot with a long/short position tool
drawn on it — you'll get the formatted signal back in a few seconds.

- **Stop the bot:** press `Ctrl+C` in the terminal.
- **Keep it running after closing the terminal (Linux):**

  ```bash
  nohup .venv/bin/python bot.py >> bot.log 2>&1 &
  ```

  Check `bot.log` for output; stop it later with `pkill -f bot.py`.

## Features

- **Chart signals** — send a chart screenshot, get the formatted signal back.
- **Breakeven alert** — after a signal, the bot watches the live price
  (Bybit public API) and messages you to move SL to breakeven once price
  covers **30% of the distance from entry to TP**.
- **TP/SL result messages** — when the trade hits take profit or stop loss,
  the bot sends a motivation message (and stops monitoring that trade).
- **Morning motivation** — a fresh Gemini-written motivation text every day
  at `MORNING_HOUR` (default 8:00, timezone via `TIMEZONE` env var) to every
  chat that has used the bot.

> Live monitoring works for assets with a matching Bybit pair — all major
> crypto (BTCUSD, ETHUSD, ...) and gold (XAUUSD via the XAUUSDT perpetual).
> Forex pairs like EURUSD aren't listed on Bybit; for those the bot says
> monitoring is unavailable and still sends the signal.

## How it works

- **The bot responds to chart images only.** Text messages get no reply, and
  images that aren't a trading chart with a position tool are silently ignored
  (Gemini classifies each image before extraction). `/start` shows a short
  usage hint.
- `bot.py` polls Telegram for messages. Photos (and image files) are downloaded
  and sent to **Gemini Flash** with a prompt describing how to read a
  TradingView-style position tool (red box = stop loss, green/blue box = take
  profit, the line between = entry).
- The model is picked from a fallback list in `bot.py` (`GEMINI_MODELS`) —
  newest Flash first, older ones as backup — because free-tier models
  occasionally return "high demand" errors.
- Gemini is forced to return **structured JSON only** (asset, direction,
  entry, SL, TP, current price) via a response schema — it never writes the
  final message and never does math.
- The backend **calculates the percentages deterministically** (per the roadmap
  formulas), deduces the order type (LIMIT/STOP) by comparing entry to the
  current price, validates that the setup is coherent, and formats the reply.

## Hosting free on Render

The bot has two modes, picked automatically:

- **Locally** (no `RENDER_EXTERNAL_URL`/`WEBHOOK_URL` set): long polling.
- **On Render**: webhook mode — Telegram POSTs each message to your Render
  URL, which is what **wakes the free service from sleep**. First reply after
  15+ minutes of inactivity takes ~30–60 s while the service wakes (Telegram
  retries delivery, so no message is lost); after that replies are instant.

### Deploy steps

1. **Push this repo to GitHub** (already done if you followed along).
2. Sign up at [render.com](https://render.com) (free, no card needed) and
   choose **New → Blueprint**, then connect the GitHub repo. Render reads
   `render.yaml` and creates the service automatically.
   - Or manually: **New → Web Service**, pick the repo, runtime *Python*,
     build command `pip install -r requirements.txt`, start command
     `python bot.py`, instance type **Free**.
3. When prompted, set the two environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `GEMINI_API_KEY`
4. Deploy. Once live, the bot registers its own webhook with Telegram —
   no manual webhook setup needed. Send `/start` to the bot to confirm.

### Keep the service awake (required for monitoring & morning texts)

A sleeping service can't watch live prices or send scheduled messages, so set
up a free uptime pinger to keep it awake 24/7:

1. Sign up at [uptimerobot.com](https://uptimerobot.com) (free) — or
   [cron-job.org](https://cron-job.org).
2. Add an HTTP(S) monitor pointing at your Render URL
   (`https://<your-app>.onrender.com/`) with a **5-minute interval**.

That's it — the pings stop Render from ever idling the service. One always-on
service uses ~730 of the free plan's 750 instance-hours per month, so it fits.

> Note: active trades and the subscriber list are stored in a local
> `state.json`. On Render's free tier this file is wiped on every redeploy or
> restart — monitored trades are forgotten then (re-send the chart to
> re-register). Fine for personal use.

### Updating the bot

Push to the GitHub repo's `main` branch — Render redeploys automatically.

### Switching back to local runs

Just run `.venv/bin/python bot.py` on your machine. **Stop the Render service
first** (or before that, run local polling will fail) — Telegram allows either
one webhook or one polling consumer, not both. Delete the webhook manually if
needed:

```bash
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```
