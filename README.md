# Trading Chart Vision Bot

A Telegram bot that reads trading chart screenshots with a free vision model on Groq,
extracts the trade setup (entry, stop loss, take profit), calculates profit/loss
percentages in code, and replies with a formatted signal:

```
BTCUSD SELL LIMIT
ENTRY: 63552.40
SL: 63717.50
TP: 60361.53
Profit: +5.02% / Loss: -0.26%
```

The order type is not decorative: the bot reads from the chart whether it's a
`MARKET` execution (position open now) or a pending `LIMIT`/`STOP` (nothing
open until price reaches your entry), monitors each accordingly, and gives you
one tap to correct it if it guessed wrong.

See `telegram_bot_roadmap.md` for the full design. Automated broker execution
is shelved — see the status note in `MT5_MOBILE_AUTOMATION.md`.

## Setup (one time)

1. **Create the Telegram bot** — message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts, and copy the HTTP API token.

2. **Get a Groq API key (free tier, no card)** — sign up at
   [console.groq.com](https://console.groq.com/keys) and create an API key.

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
- **The most recent position wins** — charts usually carry several position
  tools from earlier setups. The bot reads the **newest** one, judged by which
  tool's box *starts* furthest right on the time axis, and ignores the rest.
  Recency is judged by where a tool starts, not how far right it reaches, so
  an older position still running to the right edge doesn't win. The number of
  tools it counted goes to the log, not the chat.
- **Running trades are called out** — if another position on the chart is
  already live (price has passed its entry and is travelling between entry and
  TP/SL), the bot tells you it's running and gives its levels, on top of the
  signal for the most recent setup. That one is *not* monitored — send it as
  its own chart if you want alerts for it too.
- **Market execution vs pending order** — read from the **chart**: an entry
  line sitting on the latest candle is a market execution (`BUY MARKET` /
  `SELL MARKET`), monitored as already open, even if price has moved since the
  screenshot. Entry away from the market is a pending `LIMIT` or `STOP` that
  isn't a position yet, so the bot stays quiet about breakeven/TP/SL until
  price actually touches your entry, then tells you the order should be
  filled. If price runs to TP without ever filling, it says so — and, in case
  you were in after all, that it hit TP.
- **One tap to correct it** — the message under each signal carries a button:
  `✅ I'm already in this trade` on a pending order, `⏳ It's a pending order`
  on an open one. A wrong guess silences every alert that follows, so fixing
  it shouldn't mean re-sending the chart.
- **TP and SL alerts catch wicks** — levels are tested against the **high and
  low of every polling interval**, not the last traded price, so a spike that
  tags your stop and snaps back inside the same minute still counts. When one
  interval covers both TP and SL, the bot reports the SL: which came first is
  unknowable, and claiming the win would be the worse error.
- **Breakeven alert** — once the trade is live, the bot messages you to move
  SL to breakeven once price covers **30% of the distance from entry to TP**.
- **No duplicates, no stale trades** — re-sending the same chart won't
  register the trade twice, and monitoring stops on its own after
  `TRADE_TTL_HOURS` (default 72) if nothing has resolved.

### Commands

| Command | What it does |
| --- | --- |
| `/start` | Usage hint |
| `/trades` | One line per monitored trade: `⏳` pending · `🔴` live · `🔒` at breakeven |
| `/cancel [n\|all]` | Stop monitoring one trade (or all of them) |

The command menu is registered with Telegram automatically at startup.

### Which assets can be monitored

| Instruments | Provider | Sampling | Needs |
| --- | --- | --- | --- |
| Metals (`XAUUSD`, `XAGUSD`, `XPTUSD`…) | Swissquote | live bid/ask every 5 s | nothing |
| Forex (`EURUSD`, `GBPJPY`, `USDZAR`…) | Swissquote (Yahoo Finance fallback) | live bid/ask every 5 s | nothing |
| Crypto (`BTCUSD`, `ETHUSD`, `SOLUSD`…) | Coinbase | 1-min high/low every 15 s | nothing |
| Indices, oil (`US30`, `NAS100`, `USOIL`…) | — **not monitored** | — | see below |

All feeds are public, keyless and free:
- **Swissquote** (a regulated broker) publishes live bid/ask quotes, under a
  second old, for metals and forex. Buys are checked on the ask and sells on
  the bid — the sides a broker fills and closes on — so a short's SL is caught
  when your broker would hit it, not a spread later when the chart shows it.
  It publishes no candles, so a wick that reverses between two 5 s polls can
  still slip past.
- **Yahoo Finance** is the fallback for forex pairs Swissquote doesn't quote.
- **Coinbase** covers crypto (`BTCUSD` -> `BTC-USD`) with 1-minute candles, so
  no wick is missed. It lists spot pairs only, so coins that trade solely as
  perpetuals on derivatives exchanges aren't covered.
- Charts no provider carries still get a signal; they just don't get
  breakeven/TP/SL alerts, and the bot says why.

> **Why indices and oil aren't monitored.** No free feed tracks index or oil
> CFD prices in real time. Yahoo's cash indices (`^DJI`, `^IXIC`…) only move
> during NYSE hours — frozen for ~70% of the day while `US30` trades on — and
> sit hundreds of points off the CFD price; its futures (`YM=F`, `CL=F`) are
> ~10 minutes delayed. Watching them produced silence or wrong alerts, so the
> bot now says up front that these charts get no alerts. A free OANDA practice
> account would provide real-time index and oil CFD prices if this is needed.

(The previous metals feed, `gold-api.com`, refreshed only every 30 s and sat
~$1.30 off broker quotes; trades saved under any retired feed move to the
current one automatically.)

#### How a chart maps to a market symbol

- Broker account suffixes are stripped first: Exness's `XAUUSDm` or
  `EURUSDc`, `US30.cash`. Only when what remains is recognisably a pair, so a
  ticker like `ETC` is left alone.
- Metals and forex become Swissquote instruments: `XAUUSD`/`GOLD` -> `XAU/USD`,
  `GBPJPY` -> `GBP/JPY`. Yahoo's fallback name is `EURUSD=X`.
- For crypto, Coinbase names products `BASE-QUOTE`: a chart labelled `BTCUSD` is `BTC-USD` there. The chart's own quote currency is tried first (`ETHUSDT` -> `ETH-USDT`, since USDT trades at a small premium to USD), then `USD`, `USDT` and `USDC`; the bot probes each until Coinbase confirms the product exists.

## How it works

- **The bot responds to chart images and its own commands only.** Text
  messages get no reply, and images that aren't a trading chart with a
  position tool are silently ignored (the vision model classifies each image
  before extraction).
- `bot.py` polls Telegram for messages. Photos (and image files) are downloaded
  and sent to **Groq** (`qwen/qwen3.8-27b`) with a prompt describing how to read a
  TradingView-style position tool (red box = stop loss, green/blue box = take
  profit, the line between = entry).
- The model comes from a list in `bot.py` (`VISION_MODELS`). Groq currently
  serves one vision model; the free tier allows 30 requests/min, 1,000/day and
  7,000 input tokens/min, and a chart costs ~3,500 tokens, so about **two
  charts a minute** go straight through. Past that Groq answers 429 with how
  long to wait; the bot waits exactly that long and retries, for up to 75 s,
  so a burst of three charts all get answered (the last in ~45 s) instead of
  the third failing.
- The model **reasons before answering** (`VISION_REASONING_EFFORT`, default
  `medium`), and the prompt tells it to read the levels off the box edges —
  entry is the edge the red and green boxes share, never the current-price
  line that crosses them. Without both, it called every chart a SHORT and
  read the current price as the entry (2 of 6 test charts right); with them,
  every labelled test chart is read exactly. Charts whose position tool shows
  no price labels can come back a gridline off, so keep the labels visible.
- The model is forced into **JSON mode** and given the `ChartAnalysis` schema
  (asset, direction, entry, SL, TP, current price, how many position tools it
  saw). The reply is validated against that schema, and a mismatch is retried
  — it never writes the final message and never does math.
- **Which position gets read.** The prompt tells the model to judge recency by
  where each position tool's box *starts* on the time axis and to take the
  right-most one, ignoring every other tool and drawing on the chart. It also
  reports the total it counted — asking for a count makes it look at all of
  them before choosing, and the number goes to the log rather than the chat —
  and, separately, any position price is currently inside, which is what the
  running-trade note is built from.
- The backend **calculates the percentages deterministically** (per the roadmap
  formulas), classifies the order, validates that the setup is coherent, and
  formats the reply.
- **The screenshot classifies the order, not the live price.** The chart is
  the moment the trade was taken, so it decides, in this order:
  1. where the model **saw** the entry line sit against the latest candle —
     on it (market), above or below it (pending). Judging which of two lines
     is higher is far more reliable than reading both labels and subtracting;
  2. the chart's price label: entry within `CHART_MARKET_TOLERANCE` (5 basis
     points, ~$2 on gold) is a market execution;
  3. only if the chart gives neither, the live price, within
     `MARKET_ORDER_TOLERANCE` (1 basis point).

  The live price used to decide first, and it turned most market trades into
  phantom pending orders: by the time a chart arrives, gold has moved more
  than 1 basis point ($0.42), so the bot waited for price to return to an
  entry you were already in — and when price ran to TP, never sent the
  breakeven or TP alert at all. With no signal anywhere the bot names the
  side but not the order type, rather than guessing `LIMIT` vs `STOP`, and
  settles which side of the market the entry sits on from the first price
  reading.
- **Monitoring samples ranges, not points.** Every 15 s a background job asks
  each candle provider (Coinbase, Yahoo) for the 1-minute candles
  covering the time since that trade was last checked, and tests
  TP/SL/breakeven against the interval's high and low. Comparing only the last traded price meant a level counted solely if
  price was still beyond it at the instant of the poll, so wicks were invisible
  and trades ran on past their stops. The lookback is bounded by the trade's
  own `created_at`, so a wick from before you sent the chart can't resolve it,
  and capped at `MAX_LOOKBACK_MINUTES` (180) so waking from a long sleep
  doesn't request a day of history. Each trade remembers the newest candle it
  has used and picks up from there, rather than from the clock time of its
  last check: Yahoo publishes a minute's high/low late, and a window moving
  with a 15 s clock would slide past that minute before it filled in.
- **One request per market, all markets at once.** Trades are grouped by
  symbol, so ten trades on `BTC-USD` cost one request, and every market is
  fetched in parallel with a `FEED_TIMEOUT` (10 s) cap each. The number of
  trades has no limit; a cycle takes as long as the slowest single feed, not
  the sum of all of them. (Checking trades one at a time used to let a few
  slow feeds overrun the interval, and the scheduler then silently skipped
  whole cycles.) A feed that fails or times out only delays its own trades,
  which catch up on the next cycle.
- **One bad trade can't silence the rest.** Each trade is checked inside its
  own `try`, because an unexpected key used to raise mid-loop and abort the
  cycle for every trade in every chat, every minute, indefinitely.
- **Pair resolution** probes each candidate pair with a single-symbol price
  call, in provider order (Swissquote, Yahoo, Coinbase); the first that
  answers is the trade's feed. Trades saved under older routing (Bybit,
  GoldAPI, Yahoo indices) are moved to the current feed automatically on the
  next monitoring cycle, or dropped with a message saying why.

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
       |                                 |       (bot sends image to Groq,
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
   the bot calls `sendMessage` on its own using
   the `chat_id`s it saved in `state.json` — a bot may message any chat where
   the user has already started a conversation with it. (This also means the
   bot cannot message anyone who has never opened it — Telegram forbids
   unsolicited first contact.)

### Good to know

- **One consumer at a time:** a bot token supports either an active webhook
  *or* polling — not both at once. That's why local runs require stopping the
  Render deployment first (see *Switching back to local runs* below).
- **Groups — read the next section.** The bot analyses a chart from *any*
  sender, but Telegram's privacy mode stops most of them ever reaching it.
- **Photos vs. files:** Telegram re-compresses photos to JPEG; sending the
  screenshot as a *file/document* preserves full quality, which can help
  the model read small price labels. The bot accepts both.

## Reading every member's charts in a group

The bot has never cared who sent a photo — there is no sender filter in the
code, and any member's chart is analysed the same way. What gets in the way is
**Telegram's privacy mode**, which is ON by default for every new bot. While it
is on, a bot in a group is only *delivered* commands, @mentions, and replies to
its own messages. Other members' photos never reach it at all, so there is
nothing for the code to respond to.

There is no Bot API call that can change this — it is a BotFather setting:

1. Open [@BotFather](https://t.me/BotFather)
2. `/setprivacy` → choose your bot → **Disable**
3. **Remove the bot from the group and add it back.** The change only applies
   on re-join; existing memberships keep the old setting.

The bot checks this for you at startup via `getMe` and logs which state it is
in:

```
Privacy mode is OFF — every member's charts are visible in groups
```

If privacy mode is still on it logs a warning instead, and `/start` in a group
replies with the steps above — so this fails loudly rather than looking like a
bot that just ignores people.

## Hosting free on Render

The bot has two modes, picked automatically:

- **Locally** (no `RENDER_EXTERNAL_URL`/`WEBHOOK_URL` set): long polling.
- **On Render**: webhook mode — Telegram POSTs each message to your Render URL.

### Deploy steps

1. **Push this repo to GitHub** (already done if you followed along).
2. Sign up at [render.com](https://render.com) (free, no card needed) and
   choose **New → Blueprint**, then connect the GitHub repo. Render reads
   `render.yaml` and creates the service automatically.
   - Or manually: **New → Web Service**, pick the repo, runtime *Python*,
     build command `pip install -r requirements.txt`, start command
     `python bot.py`, instance type **Free**.
3. When prompted, set the environment variables:
   - `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY` — required
   - live prices need no key — every feed the bot uses is public
4. Deploy. Once live, the bot registers its own webhook with Telegram —
   no manual webhook setup needed. Send `/start` to the bot to confirm.

### Keep the service awake (required for monitoring)

A free service sleeps after 15 minutes idle, and **a sleeping service runs no
monitoring job at all**. Incoming Telegram messages wake it (the first reply
takes ~30–60 s; Telegram retries, so nothing is lost), but alerts need it awake
continuously. Add a free uptime pinger:

1. Sign up at [uptimerobot.com](https://uptimerobot.com) (free) — or
   [cron-job.org](https://cron-job.org).
2. Add an HTTP(S) monitor pointing at your Render URL
   (`https://<your-app>.onrender.com/`) with a **5-minute interval**.

The pings stop Render idling the service. One always-on service uses ~730 of
the free plan's 750 instance-hours per month, so it fits.

### Where state lives

Monitored trades and the chat list are stored in `state.json` (gitignored — it
holds your chat IDs and open positions, so don't commit it). Writes go through
a temp file and an atomic rename, so a crash mid-write can't truncate it.

**On the free tier this file is wiped on every redeploy and restart** —
monitored trades are forgotten, and no breakeven/TP/SL alert can fire for a
trade registered before the last deploy. Re-send the chart to re-register.
After the alert fixes, this is the most likely reason a real alert never
arrives, so check `/trades` first.

To make trades survive, Render requires a paid instance (~$7/month) — disks
are not offered on free instances. Switch `render.yaml` to `plan: starter` and
add:

```yaml
    disk:
      name: bot-state
      mountPath: /var/data
      sizeGB: 1
    envVars:
      - key: STATE_FILE
        value: /var/data/state.json
```

## Troubleshooting

**A TP or SL alert didn't arrive.** Check, in order:

1. **Is the trade still registered?** Send `/trades`. If it isn't listed, it
   isn't being watched — a free-tier redeploy wipes `state.json`. Re-send the
   chart to re-register.
2. **Was the service awake?** A sleeping service runs no monitoring job at
   all. On waking, the next cycle looks back over the gap (up to
   `MAX_LOOKBACK_MINUTES`, 180) and will still report a level crossed while it
   slept — but only up to that cap, so set up the uptime pinger.
3. **Was it still pending?** A `⏳` trade has not filled, and pending orders
   deliberately get no TP/SL alerts until price touches your entry.
4. **Was it read as pending when you were already in (or the reverse)?** The
   message under the signal says which; tap its button to correct it.
5. **Is it gold, silver or forex?** Swissquote publishes quotes, not candles,
   so a wick that reverses between 5 s polls can be missed. Crypto is checked
   on 1-minute high/low and cannot miss one.
6. **Is it an index or oil?** These aren't monitored at all — the bot says so
   when you send the chart.
7. **Check the logs.** Every event logs its sample, e.g.
   `XAUUSD SHORT -> SL (last 4310.93, range 4309.82-4310.93 since 09:41:00)`. A trade
   that keeps failing logs `Monitoring failed for <asset> in chat <id>` with a
   traceback each cycle, without affecting any other trade.

**The bot ignores charts from other people in my group.** Telegram privacy
mode is still on — see *Reading every member's charts in a group*. Remember
the remove-and-re-add step; disabling it in BotFather alone does nothing for a
group the bot is already in.

**It read the wrong position off my chart.** The bot takes the right-most
position tool. If two start at nearly the same point the model can pick the
other one — crop the screenshot to the setup you mean. When it sees more than
one tool it tells you how many it found.

**A trade says monitoring is unavailable.** Either it's an index or oil (not
monitored, see *Which assets can be monitored*), or no feed lists that symbol —
make sure the chart shows a standard ticker (e.g. `XAUUSD`, `EURUSD`, `BTCUSD`).

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
