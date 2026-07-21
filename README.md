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
- **Pending orders are tracked as pending** — a `LIMIT`/`STOP` setup isn't a
  position yet, so the bot stays quiet about breakeven/TP/SL until price
  actually touches your entry, then messages you that the order should be
  filled. If price runs to TP without ever filling, it tells you the setup
  played out without you instead of claiming a win you never took.
- **Breakeven alert** — once the trade is live, the bot watches the price
  (Bybit public API) and messages you to move SL to breakeven once
  price covers **30% of the distance from entry to TP**.
- **TP/SL result messages** — when the trade hits take profit or stop loss,
  the bot sends a motivation message (and stops monitoring that trade).
- **No duplicates, no stale trades** — re-sending the same chart won't
  register the trade twice, and monitoring stops on its own after
  `TRADE_TTL_HOURS` (default 72) if nothing has resolved.
- **Morning and night motivation** — a fresh Gemini-written text each day at
  `MORNING_HOUR` (default 8:00) and `NIGHT_HOUR` (default 22:00), in
  `TIMEZONE`, to every chat that has used the bot. The morning text is a
  plan-your-day nudge; the night one is a wind-down (journal your trades, no
  revenge trading, rest).

### Commands

| Command | What it does |
| --- | --- |
| `/start` | Usage hint |
| `/trades` | List the trades being monitored in this chat, with status |
| `/cancel [n\|all]` | Stop monitoring one trade (or all of them) |
| `/motivate` | Send the morning text right now (handy for testing) |
| `/night` | Send the night text right now |

The command menu is registered with Telegram automatically at startup.

### Which assets can be monitored

| Instruments | Provider | Needs |
| --- | --- | --- |
| Instruments | Provider | Update rate | Needs |
| --- | --- | --- | --- |
| Crypto (BTCUSD, ETHUSD, SOLUSD…) | Bybit | every 60 s | nothing |
| Gold and silver (XAUUSD, XAGUSD) | Bybit | every 60 s | nothing |
| Forex (EURUSD, GBPUSD, USDJPY…) | Twelve Data | every 2–6 min | `TWELVEDATA_API_KEY` |
| Stock indices, oil | — | not monitored | — |

The two providers are used together, each for what it does best. Bybit is
keyless and unmetered, so it handles everything it lists — all crypto plus
gold and silver. Twelve Data fills the one gap that matters, forex, which
Bybit does not list at all. Charts neither provider carries still get a
signal; they just don't get breakeven/TP/SL alerts.

#### Forex polling and the free credit budget

Twelve Data's free tier allows **800 credits/day**, and one pair costs one
credit per poll. Polling at the crypto rate of once a minute would burn that
in under an hour, so forex is paced to fit the budget instead:

| Open forex trades | Price check every |
| --- | --- |
| 1 | ~2 min |
| 2 | ~4 min |
| 3 | ~6 min |

Crypto and gold are unaffected — they still update every 60 seconds. The
practical cost is that a forex breakeven/TP/SL alert can arrive a few minutes
after the level is touched, which the bot tells you when it registers the
trade. Set `TWELVEDATA_DAILY_CREDITS` lower to be more conservative, or higher
if you upgrade the plan.

If the credit budget does run out, the bot keeps serving the last known prices
and logs the error rather than crashing or going silent.

#### How a chart maps to a Bybit pair

Bybit names pairs its own way: a chart labelled `BTCUSD` is `BTCUSDT` there,
and `XAUUSD` is `XAUUSDT`. Rather than guess a suffix, the bot **downloads
Bybit's real pair list** (755 linear + 592 spot at the time of writing),
caches it for 6 hours, and matches the chart's asset against it.

That matters for correctness, not just tidiness: a constructed name can
collide with a real but unrelated market. `SPXUSDT` exists on Bybit — it is a
memecoin, not the S&P 500. Matching against the live list, and rejecting what
isn't there, avoids pricing your index trade against a coin that shares a
ticker prefix.

> **Bybit's `+` forex pairs (`EURUSD+`, `GBPUSD+`) are not reachable here.**
> Those belong to Bybit's separate MetaTrader 5 forex product, and MT5
> symbols are not served by the public v5 REST API — a full scan of all 1,370
> instruments across the linear, inverse and spot categories returns only
> `XAUUSDT` and `XAGUSDT` as non-crypto. MT5 is reachable only through the
> MetaTrader terminal, whose Python bridge is Windows-only and cannot run on
> Render. Forex therefore comes from Twelve Data instead.

#### Getting a Twelve Data key

Sign up free at [twelvedata.com](https://twelvedata.com/) — no card — and copy
the API key from the dashboard. Put it in `.env` as `TWELVEDATA_API_KEY=…` and
in Render's environment variables. Without it, crypto and gold monitoring work
exactly as before and forex charts simply get a signal with no alerts.

## How it works

- **The bot responds to chart images and its own commands only.** Text
  messages get no reply, and images that aren't a trading chart with a
  position tool are silently ignored (Gemini classifies each image before
  extraction).
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
- That same entry-vs-current-price comparison decides whether the trade starts
  out **pending** or **live**: a market order is live immediately, while a
  LIMIT/STOP order waits for price to reach entry before any breakeven/TP/SL
  alert can fire. Every 60 s a background job checks each monitored trade
  against the live price and advances it through those states.
- **Pair resolution** downloads Bybit's full symbol list per market category
  (cached 6 h) and matches the chart's asset against it, preferring `linear`
  (USDT perpetuals) over `spot`. Per-cycle pricing then uses a targeted
  single-symbol call, because the full linear ticker payload is ~550 KB and
  fetching that every minute would be wasteful.
- **The scheduled texts are state-driven, not timer-driven.** The bot records
  the date each text was last sent in `state.json` and checks every 5 minutes
  whether today's is still outstanding. A one-shot daily timer is silently
  dropped if the host happens to be asleep at that exact minute — this
  survives that, and delivers the message late (up to 6 h) instead of never.

## How the Telegram side works

Telegram bots never talk to users directly — everything goes through
Telegram's **Bot API** servers, authenticated by the bot token from BotFather.

### The message flow, step by step

```
You (Telegram app)                Telegram servers                 This bot
       |                                 |                            |
       |  1. send chart photo            |                            |
       |-------------------------------->|                            |
       |                                 |  2. "update" (JSON)        |
       |                                 |--------------------------->|
       |                                 |  3. getFile + download     |
       |                                 |<---------------------------|
       |                                 |       (bot sends image to Gemini,
       |                                 |        does the math locally)
       |                                 |  4. sendMessage (signal)   |
       |                                 |<---------------------------|
       |  5. signal appears in chat      |                            |
       |<--------------------------------|                            |
```

1. **You send a photo** to the bot chat. Telegram stores the image on its
   servers and creates an *update* — a JSON object describing the new message.
2. **The bot receives the update** in one of two ways (chosen automatically
   in `main()`):
   - **Polling (local runs):** the bot repeatedly calls `getUpdates`,
     a long-poll HTTP request that returns as soon as something arrives.
     Outbound-only — works behind any firewall, no public URL needed.
   - **Webhook (on Render):** the bot registers its public URL with Telegram
     once at startup (`setWebhook`), and Telegram then POSTs each update to
     `https://<app>.onrender.com/telegram`. Requests are verified with a
     secret token so only Telegram can trigger the bot. Failed deliveries
     (e.g. while the free service wakes from sleep) are retried by Telegram.
3. **The bot downloads the image** via the Bot API (`getFile`), since updates
   only carry a file reference, not the image bytes themselves.
4. **The bot replies** with `sendMessage` to the chat the photo came from
   (`chat_id` in the update). Handler routing in `main()` decides what runs:
   photos/image files → chart analysis; `/start` → the welcome text; anything
   else → no handler, so no reply.
5. **Push messages without an incoming message:** for breakeven/TP/SL alerts
   and the morning motivation, the bot calls `sendMessage` on its own using
   the `chat_id`s it saved in `state.json` — a bot may message any chat where
   the user has already started a conversation with it. (This also means the
   bot cannot message anyone who has never opened it — Telegram forbids
   unsolicited first contact.)

### Good to know

- **One consumer at a time:** a bot token supports either an active webhook
  *or* polling — not both at once. That's why local runs require stopping the
  Render deployment first (see *Switching back to local runs* below).
- **Groups:** the bot works in group chats too, but by default BotFather's
  *privacy mode* means it only sees photos sent as replies to it or messages
  mentioning it. Disable privacy mode via BotFather (`/setprivacy`) if you
  want it to react to every chart posted in a group.
- **Photos vs. files:** Telegram re-compresses photos to JPEG; sending the
  screenshot as a *file/document* preserves full quality, which can help
  Gemini read small price labels. The bot accepts both.

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
3. When prompted, set the environment variables:
   - `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY` — required
   - `TIMEZONE` — set it (e.g. `Africa/Lagos`), or the scheduled texts run on
     UTC and arrive at the wrong local hour
   - live prices need no key — Bybit's API is public
4. Deploy. Once live, the bot registers its own webhook with Telegram —
   no manual webhook setup needed. Send `/start` to the bot to confirm.

### Keep the service awake (required for monitoring & scheduled texts)

A sleeping service can't watch live prices or send scheduled messages, so set
up a free uptime pinger to keep it awake 24/7:

1. Sign up at [uptimerobot.com](https://uptimerobot.com) (free) — or
   [cron-job.org](https://cron-job.org).
2. Add an HTTP(S) monitor pointing at your Render URL
   (`https://<your-app>.onrender.com/`) with a **5-minute interval**.

That's it — the pings stop Render from ever idling the service. One always-on
service uses ~730 of the free plan's 750 instance-hours per month, so it fits.

> Note: monitored trades, the subscriber list and the record of which texts
> have already gone out are stored in a local `state.json` (gitignored — it
> holds your chat IDs and open positions, so don't commit it). On Render's
> free tier this file is wiped on every redeploy or restart: monitored trades
> are forgotten (re-send the chart to re-register), and a redeploy shortly
> after a scheduled text can send it a second time. To make it survive, attach
> a Render persistent disk and point `STATE_FILE` at a path on it.

## Troubleshooting

**The morning or night text didn't arrive.** Check, in order:

1. **Send `/motivate`.** If a message comes back, Gemini and delivery are fine
   and the problem is timing, not the bot. If nothing comes back, check the
   logs — Gemini failures fall back to a canned message, so silence points at
   Telegram delivery or the chat not being registered (send `/start`).
2. **Is `TIMEZONE` set on the host?** Unset means UTC — an 8:00 text lands at
   9:00 in Lagos, 4:00 in New York. The startup log prints the resolved
   timezone and the local time, e.g.
   `Motivation texts: morning 08:00, night 22:00 (Africa/Lagos, now ...)`.
3. **Was the service awake?** A sleeping free-tier service runs no jobs at all.
   The bot now catches up on a text it missed while asleep (up to 6 hours
   late), but it can only do that once it wakes — set up the uptime pinger
   above.
4. **Did the chat ever `/start` the bot?** Scheduled texts only go to chats in
   `state.json`. That list is lost when the free-tier filesystem resets;
   sending any message to the bot re-registers the chat.

**A trade says monitoring is unavailable.** Neither provider lists that asset.
Expected for stock indices and oil — see the coverage table above. For forex,
check that `TWELVEDATA_API_KEY` is set; the startup log line beginning
`Live prices:` says whether the forex feed is active. For a crypto pair you
believe exists, look for `Bybit lists N linear pairs`; if that line is missing
or N is 0, the pair list couldn't be downloaded and every asset is rejected.

**Forex alerts feel slow.** That's the free data plan — see the polling table
above. Crypto and gold still update every 60 s.

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
