"""Automated Trading Chart Vision Bot.

Receives a chart screenshot on Telegram, extracts trade parameters with a
vision model on Groq's free tier, calculates profit/loss percentages in code (never
trusting the AI with math), and replies with a formatted signal:

    [ASSET] [ACTION] [ORDER_TYPE]
    ENTRY: [VALUE]
    SL: [VALUE]
    TP: [VALUE]
    Profit: +[X]% / Loss: -[Y]%

Live trade monitoring (Swissquote for metals and forex, Yahoo Finance as the
forex fallback, Coinbase for crypto) alerts when a pending order fills, when
to move SL to breakeven once price covers 30% of the distance to TP, and on
TP or SL. Chart assets are matched against each provider's real instrument
list rather than a guessed suffix. Indices and oil are not monitored: no free
feed tracks their CFD prices in real time.

Candle feeds are tested against each interval's high and low, so a wick
through TP or SL between polls is still caught; quote feeds are polled every
few seconds on the broker's own bid/ask.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Literal, NamedTuple, Optional

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

# Optional settings
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))


# Fraction of the entry->TP distance that triggers the breakeven alert
BREAKEVEN_FRACTION = 0.30
# How often to check live prices (seconds). Live-quote feeds (Swissquote:
# metals, forex) are polled this often, because a quote is a single point and
# polling is the only way to catch a short wick. Candle feeds (Coinbase,
# Yahoo) already carry each minute's high/low, so they are polled every
# CANDLE_FEED_INTERVAL instead. Each poll is one request per market, not per
# trade: trade count barely affects the load, distinct symbols do.
MONITOR_INTERVAL = float(os.environ.get("MONITOR_INTERVAL", "5"))
CANDLE_FEED_INTERVAL = max(MONITOR_INTERVAL, 15)
QUOTE_FEEDS = {"swissquote"}
# Never wait longer than this for one market's prices, so a slow feed can't
# stretch a cycle past MONITOR_INTERVAL (the scheduler skips a run that would
# overlap the previous one)
FEED_TIMEOUT = min(4, MONITOR_INTERVAL)
# Markets fetched at once per cycle
MAX_PARALLEL_FEEDS = 10
# Drop a monitored trade after this many hours so stale setups don't pile up
# (0 disables expiry)
TRADE_TTL_HOURS = float(os.environ.get("TRADE_TTL_HOURS", "72"))
# Entry within this fraction of the price on the CHART counts as a market
# order (5 basis points: ~$2 on gold, ~5 pips on EURUSD). The chart is the
# moment the trade was taken, so this can afford to be loose enough to absorb
# the model misreading a label by a tick or two.
CHART_MARKET_TOLERANCE = 0.0005
# Entry within this fraction of the LIVE price counts as a market order. Only
# used when the chart gives no reading at all. Deliberately tight (1 basis
# point): by the time a chart arrives price has usually moved, so live price
# alone can't tell a market trade taken a minute ago from a limit order, and
# calling a limit order "market" sends alerts for a position never opened.
MARKET_ORDER_TOLERANCE = 0.0001
# Relative tolerance for treating a re-sent chart as the same trade
DUPLICATE_TOLERANCE = 0.001

COINBASE_PRODUCTS_URL = "https://api.exchange.coinbase.com/products"

# Groq's OpenAI-compatible endpoint. Free tier, no card: sign up at
# console.groq.com and create a key.
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# Tried in order — first one that responds wins. Groq currently serves a
# single vision model; the list stays so another can be added as a fallback.
# Free-tier limits for it: 30 requests/min, 1,000/day and 7,000 input
# tokens/min. A chart costs ~3,500 tokens (the image alone is 2,048), so
# about two charts a minute fit before Groq answers 429; extract_chart_analysis
# then waits as long as Groq says and tries again.
VISION_MODELS = [
    "qwen/qwen3.8-27b",
]

# How hard the model thinks before answering: "none", "low", "medium" or
# "high". Without reasoning (Groq's default) it called every chart a SHORT and
# read the current-price line as the entry: 2 of 6 test charts right. With
# "medium" and the prompt's box-edge instructions, every labelled chart is
# read exactly, for ~2 s more per chart. Reasoning tokens are output, so they
# don't eat into the free tier's per-minute input-token budget.
VISION_REASONING_EFFORT = os.environ.get("VISION_REASONING_EFFORT", "medium")

# When every model is busy the whole list is retried after a pause: as long
# as Groq's 429 says the per-minute budget needs to free up, or else these
# successive waits (the last repeats). Waiting beats failing the user's chart.
VISION_BACKOFF = (4, 10, 20)
# Never spend longer than this on one image, so a reply always arrives
VISION_MAX_WAIT = 75

# Errors worth retrying: overloaded, rate-limited, or a transient server fault
VISION_RETRY_CODES = (429, 500, 502, 503, 504)


class VisionAPIError(Exception):
    """A failed vision request, tagged with whether trying again could help."""

    def __init__(
        self, message: str, retryable: bool, retry_after: Optional[float] = None
    ):
        super().__init__(message)
        self.retryable = retryable
        # Seconds the API asked us to wait before retrying, when it said
        self.retry_after = retry_after


def is_retryable(error: Exception) -> bool:
    """True if this vision failure is transient and worth another attempt."""
    if isinstance(error, VisionAPIError):
        return error.retryable
    # Timeouts and dropped connections
    return isinstance(error, httpx.TransportError)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistent state: subscribed chats + active trades
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    return datetime.now(dt_timezone.utc).isoformat()


def load_state() -> dict:
    data: dict = {}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read %s, starting fresh", STATE_FILE)
    data.setdefault("chats", [])
    data.setdefault("trades", [])
    data.pop("last_sent", None)  # retired: the scheduled motivation texts
    # Migrate trades written by older versions, which lacked these fields.
    # Every key monitor_trades reads is defaulted here: a trade missing one
    # used to raise mid-cycle and abort the checks for every other trade too.
    now = utcnow_iso()
    for trade in data["trades"]:
        trade.setdefault("status", "active")
        trade.setdefault("created_at", now)
        # Trades from before providers were recorded were all on Bybit, which
        # monitor_trade now re-resolves to a live feed
        trade.setdefault("provider", "bybit")
        trade.pop("category", None)  # Bybit-only field
        trade.setdefault("fill_direction", None)
        trade.setdefault("be_alerted", False)
        trade.setdefault("decimals", 2)
        trade.setdefault("profit_pct", 0.0)
        trade.setdefault("loss_pct", 0.0)
    return data


def save_state() -> None:
    """Persist state, writing through a temp file so a crash mid-write can't
    truncate it — a half-written state.json loses every monitored trade.
    """
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)
    except OSError:
        logger.exception("Could not save state to %s", STATE_FILE)


state = load_state()


def register_chat(chat_id: int) -> None:
    if chat_id not in state["chats"]:
        state["chats"].append(chat_id)
        save_state()


# ---------------------------------------------------------------------------
# Vision extraction (Groq) — returns raw values only, no math, no prose
# ---------------------------------------------------------------------------

class ActivePosition(BaseModel):
    """A second position tool the chart shows as already running.

    Reported for information only — the bot signals and monitors the most
    recent setup, not this one.
    """

    direction: Literal["LONG", "SHORT"]
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class ChartAnalysis(BaseModel):
    """Raw values the vision model extracts from the image. All math happens in code.

    is_trading_chart gates everything: when False the bot stays silent.
    """

    is_trading_chart: bool
    asset: Optional[str] = None
    direction: Optional[Literal["LONG", "SHORT"]] = None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    current_price: Optional[float] = None
    # Where the entry line sits relative to the current price, judged
    # visually. Reading two prices off a chart and subtracting them is far
    # more error-prone than seeing which line is higher, and this is the only
    # thing that can classify the order when no feed carries the asset.
    entry_placement: Optional[Literal["above", "below", "at"]] = None
    # How many position tools are drawn on the chart. When more than one, the
    # values above describe the most recent (right-most) of them.
    position_count: Optional[int] = None
    # A different position tool that price is currently inside — a trade
    # already running, as opposed to the setup being signalled.
    active_position: Optional[ActivePosition] = None


class TradeData(BaseModel):
    """A complete, validated trade setup extracted from a chart."""

    asset: str
    direction: Literal["LONG", "SHORT"]
    entry: float
    stop_loss: float
    take_profit: float
    current_price: Optional[float] = None
    entry_placement: Optional[Literal["above", "below", "at"]] = None
    position_count: int = 1


VISION_PROMPT = """\
You are analyzing an image that should be a screenshot of a trading chart \
(e.g. TradingView) with a long/short position tool drawn on it.

First decide: is this actually a trading chart with a visible position tool \
(entry, stop loss, and take profit levels)? If it is NOT — any other kind of \
image, or a chart without a position tool, or a chart whose price levels are \
unreadable — set is_trading_chart to false and leave every other field null.

If it IS such a chart, set is_trading_chart to true and extract the values.

WHICH POSITION TO READ — this matters most:
A chart often has several position tools drawn on it from earlier setups, plus \
other drawings (trendlines, rectangles, fibs, notes). You must extract exactly \
ONE position: the MOST RECENT one.

The most recent position is the one furthest to the RIGHT on the chart — time \
runs left to right, so the right-most position tool is the newest. Judge this \
by where each tool's box STARTS (its left edge / the entry line's anchor): the \
tool whose box starts furthest right is the most recent, even if an older tool \
is taller or stretches further right. If two start at the same place, take the \
one nearest the last (right-most) candle.

Ignore every other position tool on the chart completely — do not average them, \
do not blend their levels, and do not pick the largest or most obvious one. \
Set position_count to the total number of position tools you can see (1 if \
there is only one), and return the levels of the right-most one only.

How to read the chart:
- The asset name is usually in the top-left corner (e.g. "Bitcoin / U.S. Dollar" \
means the asset symbol is BTCUSD). Return the compact ticker symbol.
- The position tool draws two shaded boxes stacked on top of each other: a \
RED box (the Stop Loss zone) and a GREEN or BLUE box (the Take Profit zone). \
Read the three levels from the boxes' edges:
  - entry = the edge the red and green boxes SHARE, where one ends and the \
other begins;
  - stop_loss = the far edge of the red box (the edge away from the green box);
  - take_profit = the far edge of the green/blue box (the edge away from the \
red box).
- Do NOT confuse the entry with the current-price line. The current price is a \
separate horizontal line that runs across the whole chart to the highlighted \
label on the right price axis, and it often crosses straight through one of \
the boxes. It is never the entry unless it happens to sit exactly on the edge \
the two boxes share.
- Read exact price values from the labels on the position tool or the price \
axis on the right.
- direction: "SHORT" if the red (stop loss) box is ABOVE the green box, \
"LONG" if the red box is BELOW the green box.
- current_price is the price the market is currently trading at: the \
highlighted/coloured label on the right price axis, level with the last candle \
on the right edge. If you genuinely cannot see it, set it to null rather than \
guessing.

IS IT A PENDING ORDER OR A MARKET EXECUTION:
Set entry_placement by LOOKING at the chart, not by comparing numbers. Find the \
horizontal entry line of the position you returned, and find the level of the \
current price (the last candle on the right edge / the highlighted price-axis \
label). Then:
- "above" - the entry line is drawn clearly ABOVE the current price level
- "below" - the entry line is drawn clearly BELOW the current price level
- "at"    - the entry line sits ON the current price, level with the last \
candle, so the trade would execute immediately

Judge this visually. Seeing which line is higher is reliable; reading two \
prices off the axis and subtracting them is not. Most drawn setups are pending \
orders placed away from the current price — only answer "at" when the entry \
line genuinely touches the latest candle's price level.

IS ANOTHER POSITION ALREADY RUNNING:
Besides the most recent position, the chart may show a DIFFERENT position tool \
that is currently live: price has already passed its entry line and the candles \
are now travelling between that entry and its take profit or stop loss, with \
the tool extending to the right edge alongside the latest candles. If such a \
position exists AND it is not the same one you returned above, describe it in \
active_position (its direction, and its entry/stop_loss/take_profit if you can \
read them). If there is no such position, or the position you returned above is \
itself the running one, leave active_position null.

Extract only the raw values. Do NOT calculate anything. Do NOT write a message. \
Return only the structured data.

Respond with a single JSON object matching this JSON Schema, using null for any \
value you cannot read:
""" + json.dumps(ChartAnalysis.model_json_schema())


def retry_after_seconds(response: httpx.Response, message: str) -> Optional[float]:
    """How long a 429 asks us to wait: the retry-after header, else the
    "Please try again in 6.5s" in Groq's message."""
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        pass
    match = re.search(r"try again in ([\d.]+)(ms|s)", message)
    if not match:
        return None
    return float(match.group(1)) / (1000 if match.group(2) == "ms" else 1)


def request_chart_analysis(
    http: httpx.Client, model: str, image_url: str
) -> ChartAnalysis:
    """One vision request to Groq; raises VisionAPIError on any failure.

    Groq's JSON mode guarantees syntactically valid JSON but not the schema,
    so the reply is validated here and a mismatch is treated as retryable.
    """
    response = http.post(
        GROQ_CHAT_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "response_format": {"type": "json_object"},
            # Groq rejects JSON mode unless reasoning is hidden or parsed out
            "reasoning_format": "hidden",
            "reasoning_effort": VISION_REASONING_EFFORT,
            "temperature": 0,
        },
    )

    if response.status_code != 200:
        try:
            detail = response.json().get("error") or {}
        except ValueError:
            detail = {}
        message = detail.get("message") or response.text[:200]
        # json_validate_failed is a 400, but it means the model produced
        # malformed JSON this time — a fresh attempt usually succeeds.
        retryable = (response.status_code in VISION_RETRY_CODES
                     or detail.get("code") == "json_validate_failed")
        raise VisionAPIError(
            f"HTTP {response.status_code}: {message}", retryable,
            retry_after=retry_after_seconds(response, message),
        )

    try:
        content = response.json()["choices"][0]["message"]["content"]
        return ChartAnalysis.model_validate_json(content)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        # pydantic's ValidationError is a ValueError
        raise VisionAPIError(f"Unusable reply: {error}", retryable=True) from error


def extract_chart_analysis(image_bytes: bytes, mime_type: str) -> ChartAnalysis:
    """Read a chart with a Groq vision model, as validated JSON (sync).

    Tries each model in VISION_MODELS until one responds — the free tier
    returns 429 once the per-minute token budget is spent.
    """
    image_url = (
        f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )
    deadline = time.monotonic() + VISION_MAX_WAIT

    with httpx.Client(timeout=30) as http:
        for attempt in range(1, 100):
            last_error: Exception | None = None
            for model in VISION_MODELS:
                try:
                    analysis = request_chart_analysis(http, model, image_url)
                except Exception as error:  # noqa: BLE001 - try the next model
                    last_error = error
                    if not is_retryable(error):
                        # A bad key or malformed request won't fix itself
                        logger.error("Model %s failed permanently: %s", model, error)
                        raise
                    logger.warning("Model %s busy (attempt %d): %s",
                                   model, attempt, error)
                    continue

                if attempt > 1:
                    logger.info("Model %s answered on attempt %d", model, attempt)
                return analysis

            # Every model is busy. Wait as long as the API asked — the free
            # tier's per-minute token budget frees up on a schedule it states
            # — padded a little, or else back off.
            asked = getattr(last_error, "retry_after", None)
            wait = (asked + 0.5 if asked
                    else VISION_BACKOFF[min(attempt, len(VISION_BACKOFF)) - 1])
            if time.monotonic() + wait >= deadline:
                break
            logger.info("All models busy, retrying in %.1fs", wait)
            time.sleep(wait)

    raise last_error if last_error else RuntimeError("No vision model available")


def to_trade_data(analysis: ChartAnalysis) -> Optional[TradeData]:
    """Return a complete TradeData, or None if the image isn't a usable chart."""
    if not analysis.is_trading_chart:
        return None
    required = (analysis.asset, analysis.direction, analysis.entry,
                analysis.stop_loss, analysis.take_profit)
    if any(value is None for value in required):
        return None
    return TradeData(
        asset=analysis.asset,
        direction=analysis.direction,
        entry=analysis.entry,
        stop_loss=analysis.stop_loss,
        take_profit=analysis.take_profit,
        current_price=analysis.current_price,
        entry_placement=analysis.entry_placement,
        position_count=max(1, analysis.position_count or 1),
    )


# ---------------------------------------------------------------------------
# Deterministic math & formatting (never done by the AI)
# ---------------------------------------------------------------------------

def calculate_percentages(data: TradeData) -> tuple[float, float]:
    """Profit/loss percentages, calculated deterministically (never by the AI)."""
    entry = data.entry
    if data.direction == "SHORT":
        profit = (entry - data.take_profit) / entry * 100
        loss = (data.stop_loss - entry) / entry * 100
    else:  # LONG
        profit = (data.take_profit - entry) / entry * 100
        loss = (entry - data.stop_loss) / entry * 100
    return round(profit, 2), round(loss, 2)


def breakeven_price(data: TradeData) -> float:
    """Price at which 30% of the entry->TP distance is covered."""
    return data.entry + (data.take_profit - data.entry) * BREAKEVEN_FRACTION


def entry_fill_direction(
    data: TradeData, live_price: Optional[float] = None
) -> Optional[str]:
    """Which way price must travel to reach entry: 'up', 'down', or None.

    None means a market execution: the entry sat on the price when the trade
    was taken, so the position is already open.

    The chart decides first, because the screenshot is the moment the trade
    was taken. Judging by the live price instead turned most market trades
    into phantom pending orders: by the time a chart arrives, gold has moved
    more than the 1-basis-point tolerance ($0.42), so the bot waited for price
    to come back to an entry the user was already in — and when it ran
    straight to TP, never sent the breakeven or TP alert at all.

    In order: where the model saw the entry line sit against the last candle
    (judging which line is higher is far more reliable than reading both
    labels), then the chart's price label, then the live price.
    """
    if data.entry_placement == "at":
        return None
    if data.entry_placement == "above":
        return "up"
    if data.entry_placement == "below":
        return "down"

    chart = data.current_price
    if chart is not None and chart > 0:
        if abs(data.entry - chart) / data.entry < CHART_MARKET_TOLERANCE:
            return None
        return "up" if data.entry > chart else "down"

    if live_price is not None and live_price > 0:
        if abs(data.entry - live_price) / data.entry < MARKET_ORDER_TOLERANCE:
            return None
        return "up" if data.entry > live_price else "down"

    # Nothing to compare against at all — assume pending rather than claim a
    # position is open. See MARKET_ORDER_TOLERANCE for why that way round.
    return "unknown"


def determine_order_type(
    data: TradeData, live_price: Optional[float] = None
) -> str:
    """Classify the setup as a market execution or a pending LIMIT/STOP order.

    Entry at the current price is a market execution — the position is open
    now. Entry away from the current price is a pending order that only
    becomes a position once price travels to it, and whether it is a LIMIT or
    a STOP depends on which side of the market it sits.
    """
    return order_type_for(data.direction, entry_fill_direction(data, live_price))


def order_type_for(direction: str, fill: Optional[str]) -> str:
    """BUY/SELL plus MARKET, LIMIT or STOP, from which way price must travel
    to reach the entry (None: it's already there).

        BUY LIMIT   buy below the market  — waits for price to drop to entry
        BUY STOP    buy above the market  — waits for price to rise to entry
        SELL LIMIT  sell above the market — waits for price to rise to entry
        SELL STOP   sell below the market — waits for price to drop to entry
    """
    action = "SELL" if direction == "SHORT" else "BUY"
    if fill is None:
        return f"{action} MARKET"
    if fill not in ("up", "down"):
        # Pending, but with no price reference there is no way to say which
        # side of the market the entry sits on, and LIMIT vs STOP is exactly
        # that question. Better to name neither than to guess wrong.
        return action
    # A limit order waits for a better price, a stop order for a worse one
    better = "down" if action == "BUY" else "up"
    return f"{action} {'LIMIT' if fill == better else 'STOP'}"


def _natural_decimals(value: float) -> int:
    """Number of decimals needed to represent the price without trailing zeros."""
    max_decimals = 5 if value >= 1 else 8
    text = f"{value:.{max_decimals}f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def signal_decimals(data: TradeData) -> int:
    prices = (data.entry, data.stop_loss, data.take_profit)
    return max(2, *(_natural_decimals(p) for p in prices))


def build_signal_message(data: TradeData, live_price: Optional[float] = None) -> str:
    profit, loss = calculate_percentages(data)
    decimals = signal_decimals(data)
    entry, sl, tp = (
        f"{p:.{decimals}f}" for p in (data.entry, data.stop_loss, data.take_profit)
    )
    header = f"{data.asset.upper()} {determine_order_type(data, live_price)}"
    return (
        f"{header}\n"
        f"ENTRY: {entry}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Profit: +{profit}% / Loss: -{loss}%"
    )


def describe_chart_context(
    analysis: ChartAnalysis, data: TradeData, decimals: int
) -> Optional[str]:
    """What else was on the chart besides the setup being signalled.

    Only one thing is worth saying out loud: that another position on the
    chart is a trade already running. The count of position tools is logged
    rather than messaged — the model is still asked for it, because counting
    them makes it look at all of them before choosing, but reporting it back
    is noise on a chart that always has several.
    """
    lines = []

    if data.position_count > 1:
        logger.info(
            "%s: %d position tools on the chart, read the most recent",
            data.asset.upper(), data.position_count,
        )

    running = analysis.active_position
    if running:
        levels = " · ".join(
            f"{label} {value:.{decimals}f}"
            for label, value in (
                ("entry", running.entry),
                ("SL", running.stop_loss),
                ("TP", running.take_profit),
            )
            if value is not None and value > 0
        )
        lines.append(
            f"⚡ A {running.direction} on that chart is already running"
            + (f" — {levels}" if levels else "")
            + ".\nThat one is not monitored: the signal above is for the most "
            "recent setup. Send the running trade as its own chart if you want "
            "breakeven and TP/SL alerts for it too."
        )

    return "\n\n".join(lines) if lines else None


def validate(data: TradeData) -> Optional[str]:
    """Sanity-check the extracted values; return an error message or None."""
    if min(data.entry, data.stop_loss, data.take_profit) <= 0:
        return "Extracted prices were invalid (zero or negative)."
    if data.direction == "SHORT":
        if not (data.stop_loss > data.entry > data.take_profit):
            return (
                "Values don't look like a valid SHORT setup "
                "(expected SL above entry and TP below entry)."
            )
    else:
        if not (data.stop_loss < data.entry < data.take_profit):
            return (
                "Values don't look like a valid LONG setup "
                "(expected SL below entry and TP above entry)."
            )
    return None


# ---------------------------------------------------------------------------
# Live prices: Coinbase Exchange public API (no key, no account)
#
# Coinbase names its products BASE-QUOTE: a chart labelled BTCUSD is BTC-USD
# there. Candidates are probed one product at a time, and a 404 means
# Coinbase doesn't list it. It carries crypto only — no metals, forex or
# indices — and fewer coins than the derivatives exchanges.
# ---------------------------------------------------------------------------

# Quote currencies a chart ticker can end in, longest first so that USDT
# isn't read as USD followed by a stray T
COINBASE_QUOTES = ("USDT", "USDC", "USD", "EUR", "GBP")

# Never ask a provider for more than this much history in one poll. Bounds the
# catch-up after the host has been asleep for hours.
MAX_LOOKBACK_MINUTES = 180


class PriceSample(NamedTuple):
    """What price did over an interval, not just where it ended up.

    Comparing TP/SL against the last traded price only catches a level if
    price is still beyond it at the instant we poll. A wick that spikes
    through the stop and snaps back inside the same minute is invisible that
    way, and the trade runs on as if nothing happened. Carrying the high and
    low of the interval means any touch counts, however brief.
    """

    last: float
    high: float
    low: float

    @classmethod
    def point(cls, price: float) -> "PriceSample":
        """A sample from a feed that only publishes a spot price, no range."""
        return cls(price, price, price)


class Candle(NamedTuple):
    """One 1-minute candle: when it opened (epoch seconds), its high and low."""

    start: float
    high: float
    low: float


class PriceFeed(NamedTuple):
    """A market's recent prices, fetched once per cycle and shared by every
    trade on that market.

    Each trade then takes only the candles since it was last checked, so
    sharing one fetch never lets a trade see prices from before it existed.
    """

    last: float
    candles: tuple[Candle, ...] = ()
    # Live-quote feeds also carry the ask (`last` is then the bid)
    ask: Optional[float] = None

    def price_for(self, buying: bool) -> float:
        """The side of the quote an order is filled or closed on."""
        return self.ask if buying and self.ask is not None else self.last

    def sample_since(
        self, since: Optional[datetime], mark: Optional[float] = None
    ) -> tuple[PriceSample, Optional[float]]:
        """What price did since a trade's last check, and its new candle mark.

        `mark` is the open time of the newest candle the previous check used.
        Candles are taken from it onwards; it is re-read because it may still
        have been forming then. Tracking candles rather than the clock matters
        because feeds publish late: Yahoo leaves a minute's high/low null for
        a while, and a clock cutoff advancing every few seconds would pass
        that minute before it filled in and never test it at all.

        Without a mark (a trade's first check), take the candle that was open
        at `since` onwards — part of it may predate `since`, but dropping it
        would miss what came after.
        """
        if mark is not None:
            recent = [c for c in self.candles if c.start >= mark]
        else:
            cutoff = since.timestamp() - 60 if since else float("-inf")
            recent = [c for c in self.candles if c.start > cutoff]
        new_mark = max((c.start for c in recent), default=mark)

        if not recent:
            # A spot-only feed, or a market with no trades in the window
            return PriceSample.point(self.last), new_mark
        sample = PriceSample(
            self.last,
            max(self.last, *(c.high for c in recent)),
            min(self.last, *(c.low for c in recent)),
        )
        return sample, new_mark


def lookback_minutes(since: Optional[datetime], now: datetime) -> int:
    """How many 1-minute candles to request to cover the gap since `since`."""
    if since is None:
        return 2
    gap = (now - since).total_seconds()
    return max(2, min(MAX_LOOKBACK_MINUTES, int(gap // 60) + 2))

def normalize_asset(asset: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", asset.upper())


# Quote currencies a symbol can end in, for recognising a pair under a suffix
PAIR_QUOTES = ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD",
               "CAD", "ZAR", "TRY", "MXN", "SGD", "HKD", "NOK", "SEK", "PLN")
# Account-type suffixes brokers append: Exness XAUUSDm / EURUSDc, US30.cash...
BROKER_SUFFIXES = ("CASH", "MICRO", "MINI", "PRO", "RAW", "ECN", "M", "C", "I", "Z")


def canonical_asset(asset: str) -> str:
    """The plain market symbol, with any broker account suffix removed.

    A chart read off an Exness account says XAUUSDm, which matched no feed.
    A suffix is only stripped when what remains is recognisably a pair or a
    known index, so tickers that merely end in those letters (ETC) survive.
    """
    compact = normalize_asset(asset)
    for suffix in BROKER_SUFFIXES:
        stem = compact[: -len(suffix)]
        if compact.endswith(suffix) and stem and (
            stem.endswith(PAIR_QUOTES) or stem in UNMONITORED_ASSETS
        ):
            return stem
    return compact


def coinbase_symbol_candidates(asset: str) -> list[str]:
    """Coinbase product IDs to try for an asset like 'BTCUSD' or 'ETHUSDT'.

    Order matters: the first product Coinbase actually lists wins. The chart's
    own quote currency goes first, because USDT pairs trade at a small premium
    to USD and the levels on the chart are priced in whichever it shows.
    """
    compact = canonical_asset(asset)
    base, quote = compact, None
    for candidate in COINBASE_QUOTES:
        if compact.endswith(candidate) and len(compact) > len(candidate):
            base, quote = compact[: -len(candidate)], candidate
            break

    quotes = ([quote] if quote else []) + ["USD", "USDT", "USDC"]
    return [f"{base}-{q}" for q in dict.fromkeys(quotes)]


async def fetch_coinbase_price(
    http: httpx.AsyncClient, product: str
) -> Optional[float]:
    """Last traded price for a Coinbase product, or None if unavailable."""
    try:
        r = await http.get(f"{COINBASE_PRODUCTS_URL}/{product}/ticker")
        # Unknown products answer 404 {"message": "NotFound"}
        if r.status_code != 200:
            return None
        price = float(r.json()["price"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None
    return price if price > 0 else None


async def fetch_coinbase_feed(
    http: httpx.AsyncClient, product: str, minutes: int
) -> Optional[PriceFeed]:
    """The last `minutes` of 1-minute candles on Coinbase."""
    end = datetime.now(dt_timezone.utc)
    start = end - timedelta(minutes=minutes)
    try:
        r = await http.get(
            f"{COINBASE_PRODUCTS_URL}/{product}/candles",
            params={
                "granularity": 60,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        rows = r.json() if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        rows = []

    # Rows are [time, low, high, open, close, volume], newest first
    candles = []
    last: Optional[float] = None
    for row in rows if isinstance(rows, list) else []:
        try:
            start, low, high, close = (
                float(row[0]), float(row[1]), float(row[2]), float(row[4])
            )
        except (IndexError, TypeError, ValueError):
            continue
        if last is None:
            last = close
        candles.append(Candle(start, high, low))

    if last is None:
        # Coinbase publishes no candle for a minute without trades, so a quiet
        # pair can come back empty — fall back to the ticker so the trade is
        # still checked, just without wick coverage.
        price = await fetch_coinbase_price(http, product)
        return PriceFeed(price) if price else None
    return PriceFeed(last, tuple(candles))


# ---------------------------------------------------------------------------
# Live prices: Swissquote public quotes (keyless, no account needed)
#
# Real-time bid/ask from a regulated broker for spot metals (XAU/USD,
# XAG/USD, XPT/USD...) and forex pairs (EUR/USD, GBP/JPY, USD/ZAR...).
# Quotes are under a second old. It publishes no candles, so it is sampled
# point by point — but every few seconds, and on the same bid/ask a broker
# fills and closes on, which matters more for gold than candle coverage on a
# feed that doesn't match the broker's price. (gold-api.com, the previous
# metals feed, refreshed only every 30 s and sat ~$1.30 off broker quotes.)
# ---------------------------------------------------------------------------

SWISSQUOTE_QUOTES_URL = (
    "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument"
)

SWISSQUOTE_ALIASES = {"GOLD": "XAU/USD", "XAU": "XAU/USD",
                      "SILVER": "XAG/USD", "XAG": "XAG/USD"}


def swissquote_symbol_candidates(asset: str) -> list[str]:
    """Swissquote instrument names to try, e.g. 'XAUUSD' -> 'XAU/USD'."""
    compact = canonical_asset(asset)
    if compact in SWISSQUOTE_ALIASES:
        return [SWISSQUOTE_ALIASES[compact]]
    if len(compact) == 6 and compact.isalpha():
        return [f"{compact[:3]}/{compact[3:]}"]
    return []


async def fetch_swissquote_quote(
    http: httpx.AsyncClient, instrument: str
) -> Optional[tuple[float, float]]:
    """Live (bid, ask) for a Swissquote instrument, or None if unavailable."""
    try:
        r = await http.get(f"{SWISSQUOTE_QUOTES_URL}/{instrument}")
        if r.status_code != 200:
            return None
        # One entry per liquidity platform, each with a few spread profiles.
        # An unknown instrument answers with an empty list.
        for platform in r.json():
            for prices in platform.get("spreadProfilePrices") or []:
                bid, ask = float(prices["bid"]), float(prices["ask"])
                if 0 < bid <= ask:
                    return bid, ask
    except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
        pass
    return None


async def fetch_swissquote_price(
    http: httpx.AsyncClient, instrument: str
) -> Optional[float]:
    """Live bid — the price charts plot — for a Swissquote instrument."""
    quote = await fetch_swissquote_quote(http, instrument)
    return quote[0] if quote else None


# ---------------------------------------------------------------------------
# Live prices: Yahoo Finance (public market data — keyless, no account needed)
#
# A fallback for forex pairs Swissquote doesn't quote (EURUSD=X), and stocks.
# Not used for indices or oil any more — see UNMONITORED_ASSETS.
# ---------------------------------------------------------------------------

def yahoo_symbol_candidates(asset: str) -> list[str]:
    """Yahoo Finance symbol names to try for an asset like 'EURUSD' or 'AAPL'."""
    compact = canonical_asset(asset)
    candidates = []

    # Forex pairs (e.g., EURUSD -> EURUSD=X)
    if len(compact) == 6 and compact.isalpha():
        candidates.append(f"{compact}=X")

    # Try raw as a fallback
    candidates.append(asset.strip())

    return list(dict.fromkeys(candidates))


async def fetch_yahoo_chart(
    http: httpx.AsyncClient, symbol: str, minutes: int = 2
) -> Optional[dict]:
    """Raw 1-minute chart payload for a Yahoo Finance symbol.

    Asks for just the last `minutes` rather than the whole day: ~1.5KB
    instead of ~57KB, which matters at a poll every few seconds.
    """
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    end = int(time.time())
    try:
        r = await http.get(
            url,
            params={
                "interval": "1m",
                "period1": end - minutes * 60,
                "period2": end,
            },
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return None
        return (r.json()["chart"]["result"] or [None])[0]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None


async def fetch_yahoo_price(
    http: httpx.AsyncClient, symbol: str
) -> Optional[float]:
    """Current regularMarketPrice for a Yahoo Finance symbol, or None if unavailable."""
    result = await fetch_yahoo_chart(http, symbol)
    if not result:
        return None
    try:
        price = float(result["meta"]["regularMarketPrice"])
    except (KeyError, TypeError, ValueError):
        return None
    return price if price > 0 else None


async def fetch_yahoo_feed(
    http: httpx.AsyncClient, symbol: str, minutes: int
) -> Optional[PriceFeed]:
    """The last `minutes` of 1-minute candles on Yahoo."""
    result = await fetch_yahoo_chart(http, symbol, minutes)
    if not result:
        return None

    try:
        last = float(result["meta"]["regularMarketPrice"])
    except (KeyError, TypeError, ValueError):
        return None
    if last <= 0:
        return None

    # Outside market hours the window holds no candles at all; the last price
    # alone is still a valid (if range-less) reading.
    try:
        quote = result["indicators"]["quote"][0]
        rows = zip(result.get("timestamp") or [],
                   quote.get("high") or [], quote.get("low") or [])
        candles = tuple(
            Candle(float(start), float(high), float(low))
            for start, high, low in rows
            if high and low  # a minute with no trades is null
        )
    except (KeyError, IndexError, TypeError, ValueError):
        candles = ()
    return PriceFeed(last, candles)


# ---------------------------------------------------------------------------
# Provider routing: Swissquote for metals/forex, Yahoo Finance as the forex
# fallback, Coinbase for crypto
# ---------------------------------------------------------------------------

# Bump when routing changes: trades stamped with an older version are moved
# onto whichever feed now carries their asset (see tidy_trade).
FEED_VERSION = 2

# Index and oil CFDs have no free real-time feed. Yahoo's cash indices (^DJI,
# ^IXIC...) only move during NYSE hours — frozen for ~70% of the day while the
# CFD trades on — and sit hundreds of points off the CFD price; its futures
# (YM=F, CL=F...) are ~10 minutes delayed. Watching either produced silence or
# wrong alerts, so these are refused outright and the user is told why.
UNMONITORED_ASSETS = {
    "US30", "DJ30", "DJI", "DOW", "WS30", "YM",
    "US500", "SPX500", "SPX", "SP500", "ES",
    "US100", "NAS100", "USTEC", "NASDAQ", "NDX", "NQ",
    "GER40", "GER30", "DE40", "DE30", "DAX", "UK100", "FTSE",
    "JP225", "JPN225", "NIKKEI", "HK50", "AUS200", "FRA40", "EU50", "STOXX50",
    "USOIL", "WTI", "XTIUSD", "UKOIL", "BRENT", "XBRUSD", "NGAS", "XNGUSD",
}

UNMONITORED_REASON = (
    "there's no free real-time price feed for indices and oil, and a delayed "
    "or off-hours one would send wrong alerts"
)


def unmonitored_reason(asset: str) -> Optional[str]:
    """Why an asset is deliberately not monitored, or None if it may be."""
    if canonical_asset(asset) in UNMONITORED_ASSETS:
        return UNMONITORED_REASON
    return None


async def resolve_market(asset: str) -> Optional[dict]:
    """Pick where to source live prices for a charted asset.

    Swissquote goes first for spot metals and forex: live broker bid/ask.
    Yahoo Finance goes next for forex pairs Swissquote lacks, and stocks.
    Coinbase goes last: it covers crypto (BTCUSD, ETHUSD, SOLUSD...).
    """
    if unmonitored_reason(asset):
        return None
    async with httpx.AsyncClient(timeout=20) as http:
        for candidate in swissquote_symbol_candidates(asset):
            if await fetch_swissquote_price(http, candidate) is not None:
                return {"provider": "swissquote", "symbol": candidate}

        for candidate in yahoo_symbol_candidates(asset):
            if await fetch_yahoo_price(http, candidate) is not None:
                return {"provider": "yahoo", "symbol": candidate}

        for candidate in coinbase_symbol_candidates(asset):
            if await fetch_coinbase_price(http, candidate) is not None:
                return {"provider": "coinbase", "symbol": candidate}
    return None


async def fetch_market_price(
    http: httpx.AsyncClient, market: dict
) -> Optional[float]:
    """Current price for a resolved market, whichever provider carries it."""
    provider = market["provider"]
    if provider == "coinbase":
        return await fetch_coinbase_price(http, market["symbol"])
    if provider == "swissquote":
        return await fetch_swissquote_price(http, market["symbol"])
    if provider == "yahoo":
        return await fetch_yahoo_price(http, market["symbol"])
    return None


async def fetch_feed(
    http: httpx.AsyncClient, provider: str, symbol: str, minutes: int
) -> Optional[PriceFeed]:
    """The last `minutes` of prices for one market, whichever provider has it."""
    if provider == "coinbase":
        return await fetch_coinbase_feed(http, symbol, minutes)
    if provider == "yahoo":
        return await fetch_yahoo_feed(http, symbol, minutes)
    if provider == "swissquote":
        # A quote, not candles: sampled point by point, so a wick that
        # reverses between two polls (a few seconds) can still slip past.
        quote = await fetch_swissquote_quote(http, symbol)
        return PriceFeed(quote[0], ask=quote[1]) if quote else None
    logger.warning(
        "Market %s uses retired provider %r — its trades will expire on "
        "their own", symbol, provider,
    )
    return None


def check_trade(trade: dict, sample: PriceSample) -> Optional[str]:
    """Return the event for this trade over this interval, or None.

    Levels are tested against the interval's high and low rather than its
    closing price, so a level that price only touched still counts.

    A pending order (LIMIT/STOP) is not a position yet, so it can only report
    'entry' (price touched the entry level, order filled) or 'missed' (price
    ran all the way to TP without ever filling — the setup is void).

    Once filled, the trade reports 'tp', 'sl' or 'breakeven'.
    """
    long = trade["direction"] == "LONG"
    # The level a move in each direction reaches first
    favourable = sample.high if long else sample.low
    adverse = sample.low if long else sample.high

    def reached(level: float, extreme: float) -> bool:
        return extreme >= level if long else extreme <= level

    reached_tp = reached(trade["tp"], favourable)

    if trade.get("status") == "pending":
        # Entry can sit either side of the market, so test the extreme that
        # travels towards it rather than assuming a direction.
        fill = trade.get("fill_direction")
        if fill == "down":
            touched = sample.low <= trade["entry"]
        elif fill == "up":
            touched = sample.high >= trade["entry"]
        else:
            # Which side entry sits on was never established, so any interval
            # that spans the entry level counts as having reached it.
            touched = sample.low <= trade["entry"] <= sample.high
        if touched:
            return "entry"
        return "missed" if reached_tp else None

    # SL is checked before TP: when one interval spans both levels we cannot
    # tell which came first, and assuming the loss is the honest default —
    # reporting a win the trade may not have taken is the worse error.
    if (adverse <= trade["sl"]) if long else (adverse >= trade["sl"]):
        return "sl"
    if reached_tp:
        return "tp"
    if not trade.get("be_alerted") and reached(trade["be_price"], favourable):
        return "breakeven"
    return None


def trade_expired(trade: dict, now: datetime) -> bool:
    """True once a trade has been monitored for longer than TRADE_TTL_HOURS."""
    if TRADE_TTL_HOURS <= 0:
        return False
    try:
        created = datetime.fromisoformat(trade["created_at"])
    except (KeyError, ValueError):
        return False
    return now - created >= timedelta(hours=TRADE_TTL_HOURS)


TP_MESSAGES = [
    "🎯 TP HIT on {asset}! +{profit}% banked. Discipline pays — this is what "
    "following the plan looks like. Protect the win and stay patient for the "
    "next A+ setup. 🚀",
    "🎯 {asset} just hit TAKE PROFIT (+{profit}%)! Great execution. Winners "
    "take profits and walk away — don't give it back overtrading. 💪",
    "🎯 TP reached on {asset}! +{profit}% secured. Consistency beats intensity. "
    "One good trade at a time. 🔥",
]

SL_MESSAGES = [
    "🛑 {asset} hit STOP LOSS (-{loss}%). A stop hit is not a failure — it's "
    "your risk plan working exactly as designed. Small controlled losses keep "
    "you in the game. On to the next setup. 💪",
    "🛑 SL hit on {asset} (-{loss}%). Every professional trader takes losses; "
    "amateurs take big ones, pros take planned ones. Yours was planned. "
    "Reset, refocus, keep going. 🧠",
    "🛑 {asset} stopped out (-{loss}%). Protecting capital IS winning. The "
    "market will still be here tomorrow — and so will your account. 🌅",
]

async def deliver(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    """Push a message to a chat; a delivery failure must not abort the job."""
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Could not deliver message to chat %s", chat_id)


def trade_window_start(trade: dict) -> Optional[datetime]:
    """Where the next check's price window starts for this trade.

    That is the newest candle the last check used, or — before any candle has
    been seen — the moment the trade was registered. Never the clock time of
    the last check: candles publish late, and a window that moves with the
    clock would slide past a minute before its high/low appeared.

    Bounding it to the time we have actually been watching matters too: a
    trade registered a minute ago must not be resolved by a wick from an hour
    before it existed.
    """
    if trade.get("candle_mark") is not None:
        return datetime.fromtimestamp(trade["candle_mark"], dt_timezone.utc)
    try:
        return datetime.fromisoformat(trade["created_at"])
    except (KeyError, TypeError, ValueError):
        return None


async def tidy_trade(
    context: ContextTypes.DEFAULT_TYPE, trade: dict, now: datetime
) -> bool:
    """Expire or migrate a trade before pricing; False if it was dropped."""
    trades = state["trades"]

    if trade_expired(trade, now):
        trades.remove(trade)
        save_state()
        await deliver(
            context, trade["chat_id"],
            f"⌛ Stopped monitoring {trade['asset']} — no result after "
            f"{TRADE_TTL_HOURS:g}h. Re-send the chart if the setup is "
            "still valid.",
        )
        return False

    # Self-healing: a trade registered under older feed routing (Bybit,
    # GoldAPI, Yahoo indices...) is moved once onto whichever feed carries its
    # asset now, or dropped with a message if none does.
    if trade.get("feed_version") != FEED_VERSION:
        market = await resolve_market(trade["asset"])
        if market is None:
            trades.remove(trade)
            save_state()
            reason = (unmonitored_reason(trade["asset"])
                      or "no price feed lists it any more")
            await deliver(
                context, trade["chat_id"],
                f"⚠️ Stopped monitoring {trade['asset']} — {reason}.",
            )
            return False
        if (market["provider"], market["symbol"]) != (
                trade.get("provider"), trade.get("symbol")):
            logger.info("Moved %s from %s %s to %s %s", trade["asset"],
                        trade.get("provider"), trade.get("symbol"),
                        market["provider"], market["symbol"])
            trade["provider"] = market["provider"]
            trade["symbol"] = market["symbol"]
            trade.pop("candle_mark", None)  # belonged to the old feed
        trade["feed_version"] = FEED_VERSION
        save_state()

    return True


async def apply_feed(
    context: ContextTypes.DEFAULT_TYPE,
    trade: dict,
    feed: PriceFeed,
) -> None:
    """Check one trade against what its market did since it was last checked."""
    trades = state["trades"]
    decimals = trade.get("decimals", 2)
    since = trade_window_start(trade)
    pending = trade.get("status") == "pending"
    if feed.ask is not None:
        # A live quote: test the side the broker uses. Buying fills on the ask
        # and selling on the bid — a pending BUY fills, and an open SELL
        # closes (SL or TP), when the ask gets there. Charts plot the bid, so
        # a short's SL is otherwise hit on the broker a spread before the
        # chart shows it.
        buying = (trade["direction"] == "LONG") == pending
        sample, mark = PriceSample.point(feed.price_for(buying)), None
    else:
        sample, mark = feed.sample_since(since, trade.get("candle_mark"))

    # Only advance the mark once a reading actually came back, so an outage
    # doesn't blind the bot to what price did while the feed was down.
    if mark is not None:
        trade["candle_mark"] = mark

    event = check_trade(trade, sample)
    if event is None:
        if pending and trade.get("fill_direction") is None:
            # Which side of the market entry sits on was never established.
            # check_trade can only fill such an order on a sample that spans
            # entry, which a single quote never does — so settle the side now
            # from where price actually is, and the next touch fills it.
            trade["fill_direction"] = "up" if trade["entry"] > sample.last else "down"
            # Now that the side is known, so is LIMIT vs STOP
            trade["order_type"] = order_type_for(
                trade["direction"], trade["fill_direction"])
        return

    if event == "entry":
        trade["status"] = "active"
        text = (
            f"✅ {trade['asset']} {trade.get('order_type') or ''} triggered — "
            f"price reached your entry {trade['entry']:.{decimals}f}, so the "
            "order should be filled. Now watching for breakeven, TP and SL."
        ).replace("  ", " ")
    elif event == "missed":
        trades.remove(trade)
        text = (
            f"🚪 {trade['asset']}: price reached TP "
            f"({trade['tp']:.{decimals}f}) without coming back to your "
            f"entry at {trade['entry']:.{decimals}f}, so a pending order "
            "there never filled — no loss taken.\n"
            "If you were actually already in this trade, it just hit TP 🎯"
        )
    elif event == "breakeven":
        trade["be_alerted"] = True
        text = (
            f"🔒 {trade['asset']}: price reached "
            f"{trade['be_price']:.{decimals}f} — 30% of the way to TP.\n"
            f"Move your STOP LOSS to BREAKEVEN ({trade['entry']:.{decimals}f}) "
            "to make this a risk-free trade."
        )
    elif event == "tp":
        trades.remove(trade)
        text = random.choice(TP_MESSAGES).format(
            asset=trade["asset"], profit=trade["profit_pct"]
        )
    else:  # sl
        trades.remove(trade)
        text = random.choice(SL_MESSAGES).format(
            asset=trade["asset"], loss=trade["loss_pct"]
        )

    logger.info(
        "%s %s -> %s (last %s, range %s-%s since %s)",
        trade["asset"], trade["direction"], event.upper(),
        sample.last, sample.low, sample.high,
        since.strftime("%H:%M:%S") if since else "start",
    )
    # Saved before sending, so a crash between the two can't repeat the alert
    save_state()
    await deliver(context, trade["chat_id"], text)


async def fetch_feeds(
    http: httpx.AsyncClient, wanted: dict[tuple[str, str], int]
) -> dict[tuple[str, str], Optional[PriceFeed]]:
    """Fetch every market at once, each bounded by FEED_TIMEOUT.

    A market that errors or times out maps to None: its trades are skipped
    this cycle and their watermark stays put, so the next cycle covers the gap.
    """
    limit = asyncio.Semaphore(MAX_PARALLEL_FEEDS)

    async def one(provider: str, symbol: str, minutes: int) -> Optional[PriceFeed]:
        async with limit:
            try:
                return await asyncio.wait_for(
                    fetch_feed(http, provider, symbol, minutes), FEED_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("%s %s took over %ss, skipped this cycle",
                               provider, symbol, FEED_TIMEOUT)
            except Exception:
                logger.exception("Price fetch failed for %s %s", provider, symbol)
            return None

    results = await asyncio.gather(
        *(one(provider, symbol, minutes)
          for (provider, symbol), minutes in wanted.items())
    )
    return dict(zip(wanted, results))


# When each market was last polled (time.monotonic())
_last_polled: dict[tuple[str, str], float] = {}


async def monitor_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: check live prices for all monitored trades.

    Prices are fetched once per market, not per trade, and all markets at
    once. Fetching trade by trade made a cycle as slow as the sum of every
    request; with a few slow feeds it overran the interval, and the scheduler
    then skipped whole cycles rather than run two at the same time.
    """
    if not state["trades"]:
        return

    now = datetime.now(dt_timezone.utc)

    # One bad trade must not abort the cycle for everyone else's. Without
    # this an unexpected key or a malformed price silently stopped every
    # alert in every chat, on every tick, for good.
    for trade in list(state["trades"]):
        try:
            await tidy_trade(context, trade, now)
        except Exception:
            logger.exception("Housekeeping failed for %s in chat %s",
                             trade.get("asset"), trade.get("chat_id"))

    # How far back each market needs to reach: its longest-unchecked trade.
    # Only markets due a poll are fetched; the rest keep their marks and
    # catch up next time.
    clock = time.monotonic()
    wanted: dict[tuple[str, str], int] = {}
    for trade in state["trades"]:
        key = (trade.get("provider"), trade.get("symbol"))
        every = MONITOR_INTERVAL if key[0] in QUOTE_FEEDS else CANDLE_FEED_INTERVAL
        # A little slack so a tick landing a hair early doesn't skip a poll
        if clock - _last_polled.get(key, float("-inf")) < every - 1:
            continue
        minutes = lookback_minutes(trade_window_start(trade), now)
        wanted[key] = max(wanted.get(key, 0), minutes)
    if not wanted:
        return
    for key in wanted:
        _last_polled[key] = clock

    async with httpx.AsyncClient(timeout=FEED_TIMEOUT) as http:
        feeds = await fetch_feeds(http, wanted)

    for trade in list(state["trades"]):
        feed = feeds.get((trade.get("provider"), trade.get("symbol")))
        # No feed, or /cancel removed the trade while prices were in flight
        if feed is None or trade not in state["trades"]:
            continue
        try:
            await apply_feed(context, trade, feed)
        except Exception:
            logger.exception("Monitoring failed for %s in chat %s",
                             trade.get("asset"), trade.get("chat_id"))

    # Persist the advanced watermarks once per cycle, not once per trade
    save_state()


# ---------------------------------------------------------------------------
# Monitored-trade bookkeeping
# ---------------------------------------------------------------------------

def _close(a: float, b: float) -> bool:
    return abs(a - b) <= abs(b) * DUPLICATE_TOLERANCE


def find_duplicate_trade(
    chat_id: int, data: TradeData, symbol: str
) -> Optional[dict]:
    """The already-monitored trade matching this setup, if there is one.

    Re-reading the same chart can shift a digit, so levels are compared with a
    small relative tolerance rather than for exact equality.
    """
    for trade in state["trades"]:
        if (trade["chat_id"] != chat_id or trade["symbol"] != symbol
                or trade["direction"] != data.direction):
            continue
        if (_close(trade["entry"], data.entry)
                and _close(trade["sl"], data.stop_loss)
                and _close(trade["tp"], data.take_profit)):
            return trade
    return None


def chat_trades(chat_id: int) -> list[dict]:
    return [t for t in state["trades"] if t["chat_id"] == chat_id]


def describe_trade(trade: dict, index: int) -> str:
    """One scannable line per trade — the glyph carries the status.

    ⏳ waiting for entry · 🔴 running · 🔒 running, SL already moved to breakeven
    """
    decimals = trade.get("decimals", 2)
    if trade.get("status") == "pending":
        glyph = "⏳"
    else:
        glyph = "🔒" if trade.get("be_alerted") else "🔴"
    order = trade.get("order_type") or trade["direction"]
    return (
        f"{index}. {glyph} {trade['asset']} {order} · "
        f"E {trade['entry']:.{decimals}f} · "
        f"SL {trade['sl']:.{decimals}f} · "
        f"TP {trade['tp']:.{decimals}f}"
    )


def monitoring_status_text(trade: dict) -> str:
    """How the bot is treating a trade, for the reply under its signal."""
    decimals = trade.get("decimals", 2)
    entry = f"{trade['entry']:.{decimals}f}"
    be_price = f"{trade['be_price']:.{decimals}f}"
    order = trade.get("order_type") or trade["direction"]
    if trade.get("status") == "pending":
        waits = {"up": f"waits for price to rise to {entry}",
                 "down": f"waits for price to drop to {entry}"}.get(
                     trade.get("fill_direction"), f"waits for price to reach {entry}")
        expiry = (
            f"\nMonitoring stops automatically after {TRADE_TTL_HOURS:g}h."
            if TRADE_TTL_HOURS > 0 else ""
        )
        return (
            f"⏳ {order} — pending order: it {waits}, so nothing is open "
            f"yet. I'll tell you when it triggers, then watch for breakeven "
            f"({be_price}), TP and SL.{expiry}"
        )
    how = ("market execution: the entry was at the market price, so I'm "
           "treating this as open now" if order.endswith("MARKET")
           else "triggered: price reached the entry, so the position is open")
    return (
        f"🔴 {order} — {how}.\n"
        f"I'll alert you to move SL to breakeven at {be_price} (30% of the "
        "way to TP), and again when TP or SL is hit."
    )


def correction_buttons(trade: dict) -> Optional[InlineKeyboardMarkup]:
    """One tap to fix a wrong market/pending guess.

    The bot can only infer from a screenshot whether the trade was taken at
    market or left as a pending order, and a wrong guess silences every alert
    that follows: a market trade read as pending waits for an entry that
    already happened.
    """
    if not trade.get("id"):
        return None
    if trade.get("status") == "pending":
        button = InlineKeyboardButton(
            "✅ I'm already in this trade", callback_data=f"in:{trade['id']}")
    else:
        button = InlineKeyboardButton(
            "⏳ It's a pending order", callback_data=f"wait:{trade['id']}")
    return InlineKeyboardMarkup([[button]])


async def correct_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button handler: switch a trade between open and pending."""
    query = update.callback_query
    action, _, trade_id = (query.data or "").partition(":")
    chat_id = query.message.chat_id if query.message else None
    trade = next(
        (t for t in state["trades"]
         if t.get("id") == trade_id and t["chat_id"] == chat_id),
        None,
    )
    if trade is None:
        await query.answer("That trade isn't being monitored any more.")
        return

    side = "SELL" if trade["direction"] == "SHORT" else "BUY"
    if action == "in" and trade.get("status") == "pending":
        trade["status"] = "active"
        trade["order_type"] = f"{side} MARKET"
    elif action == "wait" and trade.get("status") != "pending":
        trade["status"] = "pending"
        trade["order_type"] = side
        trade["be_alerted"] = False
        # Settled from the next price reading (see apply_feed)
        trade["fill_direction"] = None
    else:
        await query.answer()  # a double tap: already in that state
        return

    save_state()
    logger.info("%s in chat %s corrected to %s",
                trade["asset"], chat_id, trade["status"])
    await query.answer("Updated ✅")
    try:
        await query.edit_message_text(
            monitoring_status_text(trade), reply_markup=correction_buttons(trade)
        )
    except Exception:  # noqa: BLE001 - the state change is what matters
        logger.warning("Could not update the status message", exc_info=True)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def list_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/trades — show what is currently being monitored in this chat."""
    message = update.effective_message
    register_chat(message.chat_id)
    trades = chat_trades(message.chat_id)
    if not trades:
        await message.reply_text(
            "No trades are being monitored here. Send a chart screenshot to "
            "start one."
        )
        return
    lines = [describe_trade(t, i) for i, t in enumerate(trades, start=1)]
    await message.reply_text(
        f"📋 {len(trades)} monitored — ⏳ pending · 🔴 live · 🔒 at breakeven\n"
        + "\n".join(lines)
        + "\n\n/cancel <number> · /cancel all"
    )


async def cancel_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel [n|all] — stop monitoring one trade, or all of them."""
    message = update.effective_message
    trades = chat_trades(message.chat_id)
    if not trades:
        await message.reply_text("Nothing to cancel — no trades are being monitored.")
        return

    arg = context.args[0].lower() if context.args else ""

    if arg == "all" or (not arg and len(trades) == 1):
        for trade in trades:
            state["trades"].remove(trade)
        save_state()
        await message.reply_text(
            f"🗑️ Stopped monitoring {len(trades)} trade(s). No further alerts."
        )
        return

    if not arg:
        lines = [describe_trade(t, i) for i, t in enumerate(trades, start=1)]
        await message.reply_text(
            "Which one? Send /cancel <number> or /cancel all.\n\n" + "\n".join(lines)
        )
        return

    if not arg.isdigit() or not 1 <= int(arg) <= len(trades):
        await message.reply_text(
            f"Pick a number between 1 and {len(trades)}, or use /cancel all. "
            "See /trades for the list."
        )
        return

    trade = trades[int(arg) - 1]
    state["trades"].remove(trade)
    save_state()
    await message.reply_text(
        f"🗑️ Stopped monitoring {trade['asset']} {trade['direction']}. "
        "No further alerts for it."
    )


async def typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Show the typing indicator — cosmetic, so never let it break the reply.

    sendChatAction is rejected outright in channels. This used to run outside
    the handler's try block, so a channel post raised here and the chart was
    never analysed at all, silently.
    """
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as error:  # noqa: BLE001 - an indicator is not worth failing over
        logger.debug("No typing indicator for chat %s: %s", chat_id, error)


async def handle_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an incoming chart image (photo or image file)."""
    # Editing a channel post re-delivers it as edited_channel_post; analysing
    # it again would repost the same signal.
    if update.edited_message or update.edited_channel_post:
        return

    message = update.effective_message
    if message is None:
        return

    register_chat(message.chat_id)
    await typing(context, message.chat_id)

    try:
        if message.photo:
            file = await message.photo[-1].get_file()
            mime_type = "image/jpeg"  # Telegram re-encodes photos as JPEG
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file = await message.document.get_file()
            mime_type = message.document.mime_type
        else:
            return

        image_bytes = bytes(await file.download_as_bytearray())
        analysis = await asyncio.to_thread(extract_chart_analysis, image_bytes, mime_type)

        data = to_trade_data(analysis)
        if data is None:
            # Not a trading chart — stay silent per bot policy
            logger.info("Ignored non-chart image in chat %s", message.chat_id)
            return

        error = validate(data)
        if error:
            await message.reply_text(
                f"⚠️ Couldn't read a valid setup from this chart.\n{error}\n"
                "Make sure the position tool with entry/SL/TP is clearly visible."
            )
            return

        # Resolve the price feed before replying. The chart decides whether
        # this is a market execution or a pending order; the live price is
        # the fallback for a chart that gives no reading (entry_fill_direction).
        market = await resolve_market(data.asset)
        live_price = None
        if market:
            async with httpx.AsyncClient(timeout=15) as http:
                live_price = await fetch_market_price(http, market)

        decimals = signal_decimals(data)
        await message.reply_text(build_signal_message(data, live_price))

        context_note = describe_chart_context(analysis, data, decimals)
        if context_note:
            await message.reply_text(context_note)

        if not market:
            reason = unmonitored_reason(data.asset) or "no price feed lists it"
            await message.reply_text(
                f"ℹ️ Live monitoring isn't available for {data.asset.upper()} "
                f"— {reason}. Breakeven/TP/SL alerts are off for this trade."
            )
            return

        symbol = market["symbol"]

        existing = find_duplicate_trade(message.chat_id, data, symbol)
        if existing:
            await message.reply_text(
                f"🔁 Already monitoring this {data.asset.upper()} setup "
                f"({'pending entry' if existing['status'] == 'pending' else 'live'})"
                " — not adding it twice. Use /trades to see it or /cancel to drop it."
            )
            return

        be_price = breakeven_price(data)
        profit, loss = calculate_percentages(data)
        fill = entry_fill_direction(data, live_price)
        trade = {
            # Lets the correction buttons find this trade again
            "id": secrets.token_hex(4),
            "chat_id": message.chat_id,
            "asset": data.asset.upper(),
            "provider": market["provider"],
            "symbol": symbol,
            "direction": data.direction,
            "entry": data.entry,
            "sl": data.stop_loss,
            "tp": data.take_profit,
            "be_price": be_price,
            "be_alerted": False,
            "decimals": decimals,
            "profit_pct": profit,
            "loss_pct": loss,
            "order_type": determine_order_type(data, live_price),
            # A pending order isn't a position yet — no TP/SL/breakeven alerts
            # until price actually touches the entry level.
            "status": "pending" if fill else "active",
            # "unknown" means we never established which side of the market
            # entry sits on; check_trade then fills on any touch either way.
            "fill_direction": fill if fill in ("up", "down") else None,
            "created_at": utcnow_iso(),
            "feed_version": FEED_VERSION,
        }
        state["trades"].append(trade)
        save_state()

        await message.reply_text(
            monitoring_status_text(trade), reply_markup=correction_buttons(trade)
        )
    except Exception as error:
        logger.exception("Failed to process chart")
        if is_retryable(error):
            await message.reply_text(
                "⏳ The vision AI is overloaded right now (free tier). I kept "
                "retrying for a while and it stayed busy — send the chart "
                "again in a minute and it usually goes through."
            )
        else:
            await message.reply_text(
                "❌ Sorry, I couldn't process that image right now. Please try again."
            )


# ---------------------------------------------------------------------------
# Group chats
#
# The bot has never filtered on who sent a photo — any member's chart is
# analysed. What silently blocks that is Telegram's *privacy mode*, which is
# ON by default for every new bot: in a group it then only receives commands,
# @mentions, and replies to its own messages, so other members' photos never
# reach the bot at all. There is no Bot API call to change it (BotFather
# only), but getMe reports the resulting permission, so the bot can at least
# say so instead of looking broken.
# ---------------------------------------------------------------------------

PRIVACY_HINT = (
    "⚠️ I can only see charts sent as a reply to me or with @mention in this "
    "group.\n\nTo read every chart from every member, the bot owner needs to "
    "turn privacy mode off:\n"
    "1. Open @BotFather\n"
    "2. /setprivacy → pick this bot → Disable\n"
    "3. Remove me from the group and add me back (the change only takes "
    "effect on re-join)"
)

# Set once at startup from getMe; None until then
reads_all_group_messages: Optional[bool] = None


def group_privacy_warning(update: Update) -> Optional[str]:
    """PRIVACY_HINT if this is a group the bot can't fully see, else None."""
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return None
    return None if reads_all_group_messages else PRIVACY_HINT


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_chat(update.effective_message.chat_id)
    warning = group_privacy_warning(update)
    await update.effective_message.reply_text(
        "👋 Send me a screenshot of a trading chart with a long/short position "
        "tool drawn on it (entry, stop loss, take profit) and I'll reply with a "
        "formatted signal.\n\n"
        "If several positions are drawn, I read the most recent one — the "
        "furthest right.\n\n"
        "I read from the chart whether you're in at market or waiting on a "
        "pending LIMIT/STOP order (one tap fixes it if I guess wrong), then "
        "monitor the trade: when a pending order fills, when to move SL to "
        "breakeven at 30% of the way to TP, and when TP or SL is hit.\n\n"
        "/trades — what I'm currently watching\n"
        "/cancel — stop watching a trade"
        + (f"\n\n{warning}" if warning else "")
    )


BOT_COMMANDS = [
    ("start", "How to use the bot"),
    ("trades", "List trades being monitored"),
    ("cancel", "Stop monitoring a trade"),
]


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log anything a handler raised.

    Without this, python-telegram-bot swallows handler exceptions with a
    single "No error handlers are registered" line and no traceback — which is
    how a channel post failing on sendChatAction looked like the bot simply
    ignoring the chart.
    """
    chat = getattr(getattr(update, "effective_chat", None), "id", None)
    kind = getattr(getattr(update, "effective_chat", None), "type", None)
    logger.error(
        "Handler failed for chat %s (%s)", chat, kind, exc_info=context.error
    )


async def register_commands(app: Application) -> None:
    """Populate the command menu and check group visibility (best effort)."""
    global reads_all_group_messages
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.warning("Could not set the bot command menu", exc_info=True)

    try:
        me = await app.bot.get_me()
        reads_all_group_messages = bool(me.can_read_all_group_messages)
    except Exception:
        logger.warning("Could not read the bot's group permissions", exc_info=True)
        return

    if reads_all_group_messages:
        logger.info("Privacy mode is OFF — every member's charts are visible in groups")
    else:
        logger.warning(
            "Privacy mode is ON: in groups this bot only receives commands, "
            "@mentions and replies to itself, so charts posted by other "
            "members never reach it. Disable it in @BotFather "
            "(/setprivacy -> Disable), then remove and re-add the bot to each "
            "group for the change to take effect."
        )


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(register_commands)
        .build()
    )

    # Chart images only — text and any other message types are ignored
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trades", list_trades))
    app.add_handler(CommandHandler("cancel", cancel_trade))
    app.add_handler(CallbackQueryHandler(correct_trade, pattern=r"^(in|wait):"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_chart))
    app.add_error_handler(on_error)

    # Each run samples the price range since the previous one, so a tick
    # missed while the host slept is covered by the next one's lookback
    # rather than lost. A run that would overlap a still-running one is
    # skipped by the scheduler, which FEED_TIMEOUT keeps from happening.
    app.job_queue.run_repeating(monitor_trades, interval=MONITOR_INTERVAL, first=10)

    logger.info(
        "Monitoring %d trade(s) every %gs on interval high/low",
        len(state["trades"]), MONITOR_INTERVAL,
    )
    logger.info("Live prices: Swissquote (metals/forex) + Yahoo Finance (forex fallback) + Coinbase (crypto)")

    # On Render (and similar hosts) RENDER_EXTERNAL_URL is set automatically:
    # run in webhook mode so incoming Telegram messages wake the free service.
    # Locally neither variable is set, so we fall back to polling.
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        import hashlib

        port = int(os.environ.get("PORT", "10000"))
        # Deterministic secret so Telegram-signed requests can be verified
        # without configuring an extra env var.
        secret = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:48]
        logger.info("Bot started in webhook mode on port %s -> %s", port, base_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=f"{base_url.rstrip('/')}/telegram",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Bot started, polling for updates...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
