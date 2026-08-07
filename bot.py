"""Automated Trading Chart Vision Bot.

Receives a chart screenshot on Telegram, extracts trade parameters with
Gemini Flash vision, calculates profit/loss percentages in code (never
trusting the AI with math), and replies with a formatted signal:

    [ASSET] [ACTION] [ORDER_TYPE]
    ENTRY: [VALUE]
    SL: [VALUE]
    TP: [VALUE]
    Profit: +[X]% / Loss: -[Y]%

Live trade monitoring (GoldAPI for spot metals, Yahoo Finance for
forex/indices/oil, Bybit for crypto) alerts when a pending order fills, when
to move SL to breakeven once price covers 30% of the distance to TP, and on
TP or SL. Chart assets are matched against each provider's real instrument
list rather than a guessed suffix.

Levels are tested against the high and low of each polling interval, not the
last traded price, so a wick through TP or SL between polls is still caught.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Literal, NamedTuple, Optional

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Optional settings
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))


# Fraction of the entry->TP distance that triggers the breakeven alert
BREAKEVEN_FRACTION = 0.30
# How often to check live prices for active trades (seconds)
MONITOR_INTERVAL = 60
# Drop a monitored trade after this many hours so stale setups don't pile up
# (0 disables expiry)
TRADE_TTL_HOURS = float(os.environ.get("TRADE_TTL_HOURS", "72"))
# Entry within this fraction of the current price counts as a market order
MARKET_ORDER_TOLERANCE = 0.0005
# Relative tolerance for treating a re-sent chart as the same trade
DUPLICATE_TOLERANCE = 0.001

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
# Bybit market categories to search for a pair, in order of preference:
# linear = USDT perpetual futures (most leveraged pairs), spot = spot market
BYBIT_CATEGORIES = ("linear", "spot")

# Tried in order — first one that responds wins. The newest Flash models on the
# free tier intermittently return 503 (high demand), so we keep fallbacks.
# Ordered by measured availability and latency on the free tier, not by
# version number. Benchmarked with a real vision+schema call: 3.6-flash
# answered in 2.7s and 3.1-flash-lite in 1.1s, while 3-flash-preview took
# 29.8s and the rest returned 429 (quota exhausted) inside a second.
# Exhausted models fail fast, so they cost almost nothing to skip and stay on
# as fallbacks for when the quota window rolls over.
GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-3.5-flash",
]

# When every model is busy the whole list is retried after a pause. Free-tier
# 503s are explicitly temporary ("spikes in demand are usually temporary"), so
# waiting a few seconds usually beats failing the user's chart outright.
GEMINI_BACKOFF = (0, 4, 10)
# Never spend longer than this on one image, so a reply always arrives
GEMINI_MAX_WAIT = 75

# Errors worth retrying: overloaded, rate-limited, or a transient server fault
GEMINI_RETRY_CODES = (429, 500, 502, 503, 504)


def is_retryable(error: Exception) -> bool:
    """True if this Gemini failure is transient and worth another attempt."""
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(code, int):
        return code in GEMINI_RETRY_CODES
    text = str(error)
    return any(str(c) in text for c in GEMINI_RETRY_CODES) or "UNAVAILABLE" in text

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


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
        trade.setdefault("provider", "bybit")
        trade.setdefault("category", "linear")
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
# Vision extraction (Gemini) — returns raw values only, no math, no prose
# ---------------------------------------------------------------------------

class ChartAnalysis(BaseModel):
    """Raw values Gemini extracts from the image. All math happens in code.

    is_trading_chart gates everything: when False the bot stays silent.
    """

    is_trading_chart: bool
    asset: Optional[str] = None
    direction: Optional[Literal["LONG", "SHORT"]] = None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    current_price: Optional[float] = None
    # How many position tools are drawn on the chart. When more than one, the
    # values above describe the most recent (right-most) of them.
    position_count: Optional[int] = None


class TradeData(BaseModel):
    """A complete, validated trade setup extracted from a chart."""

    asset: str
    direction: Literal["LONG", "SHORT"]
    entry: float
    stop_loss: float
    take_profit: float
    current_price: Optional[float] = None
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
- The position tool draws two shaded boxes. The RED shaded box is the Stop Loss \
zone. The GREEN or BLUE shaded box is the Take Profit zone. The horizontal line \
separating them is the Entry price.
- Read exact price values from the labels on the position tool or the price \
axis on the right.
- direction: "SHORT" if the red (stop loss) box is ABOVE the entry line, \
"LONG" if the red box is BELOW the entry line.
- current_price is the price the market is currently trading at: the \
highlighted/coloured label on the right price axis, level with the last candle \
on the right edge. This decides whether the setup is a pending order or a \
market execution, so read it carefully. If you genuinely cannot see it, set it \
to null rather than guessing.

Extract only the raw values. Do NOT calculate anything. Do NOT write a message. \
Return only the structured data.
"""


def extract_chart_analysis(image_bytes: bytes, mime_type: str) -> ChartAnalysis:
    """Call Gemini vision with enforced structured JSON output (sync).

    Tries each model in GEMINI_MODELS until one responds — free-tier models
    intermittently return 503 (high demand) or 429 (quota).
    """
    deadline = time.monotonic() + GEMINI_MAX_WAIT
    last_error: Exception | None = None

    for attempt, delay in enumerate(GEMINI_BACKOFF):
        if delay:
            if time.monotonic() + delay >= deadline:
                break
            logger.info("All models busy, retrying in %ss", delay)
            time.sleep(delay)

        for model in GEMINI_MODELS:
            try:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=[
                        genai_types.Part.from_bytes(
                            data=image_bytes, mime_type=mime_type
                        ),
                        VISION_PROMPT,
                    ],
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ChartAnalysis,
                        temperature=0,
                    ),
                )
            except Exception as error:  # noqa: BLE001 - try the next model
                last_error = error
                if not is_retryable(error):
                    # A bad key or malformed request won't fix itself
                    logger.error("Model %s failed permanently: %s", model, error)
                    raise
                logger.warning("Model %s busy (attempt %d): %s",
                               model, attempt + 1, error)
                if time.monotonic() >= deadline:
                    break
                continue

            if attempt:
                logger.info("Model %s answered on attempt %d", model, attempt + 1)
            parsed = response.parsed
            if isinstance(parsed, ChartAnalysis):
                return parsed
            return ChartAnalysis.model_validate_json(response.text)

        if time.monotonic() >= deadline:
            break

    raise last_error if last_error else RuntimeError("No Gemini model available")


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


def reference_price(data: TradeData, live_price: Optional[float] = None) -> Optional[float]:
    """The price the entry is judged against to classify the order.

    The live feed wins when we have it. Gemini reads the chart's own price
    label well enough most of the time, but it is a screenshot of a moment
    that has already passed, and when the label is small or occluded the model
    returns null — which used to make every such setup look like a market
    execution. The feed is both current and always numeric, so it decides;
    the chart's reading is only the fallback for assets no feed carries.
    """
    if live_price is not None and live_price > 0:
        return live_price
    if data.current_price is not None and data.current_price > 0:
        return data.current_price
    return None


def entry_fill_direction(
    data: TradeData, live_price: Optional[float] = None
) -> Optional[str]:
    """Which way price must travel to reach entry: 'up', 'down', or None.

    None means the order executes immediately at market — either entry sits on
    the current price, or no price was available to compare it against.
    """
    current = reference_price(data, live_price)
    if current is None:
        return None
    if abs(data.entry - current) / data.entry < MARKET_ORDER_TOLERANCE:
        return None
    return "up" if data.entry > current else "down"


def determine_order_type(
    data: TradeData, live_price: Optional[float] = None
) -> str:
    """Classify the setup as a market execution or a pending LIMIT/STOP order.

    Entry at the current price is a market execution — the position is open
    now. Entry away from the current price is a pending order that only
    becomes a position once price travels to it, and whether it is a LIMIT or
    a STOP depends on which side of the market it sits.
    """
    action = "SELL" if data.direction == "SHORT" else "BUY"
    fill = entry_fill_direction(data, live_price)
    if fill is None:
        return f"{action} MARKET"

    if data.direction == "SHORT":
        # Selling above the market waits for price to rise -> LIMIT
        order = "LIMIT" if fill == "up" else "STOP"
    else:
        # Buying below the market waits for price to fall -> LIMIT
        order = "LIMIT" if fill == "down" else "STOP"
    return f"{action} {order}"


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
# Live prices: Bybit public API (no key, no account)
#
# Bybit names its pairs its own way: a chart labelled BTCUSD or XAUUSD is
# BTCUSDT / XAUUSDT here. Rather than guess a suffix and hope, the bot
# downloads Bybit's real symbol list per market category and matches against
# it — the list is authoritative and catches renames and new listings.
#
# The list is only used to resolve a chart to a pair. Per-cycle pricing stays
# a targeted per-symbol call, because the full linear ticker payload is ~550KB
# and fetching that every minute would be absurd.
# ---------------------------------------------------------------------------

# Cache the symbol list this long before refetching (seconds)
BYBIT_SYMBOLS_TTL = 6 * 3600

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


def lookback_minutes(since: Optional[datetime], now: datetime) -> int:
    """How many 1-minute candles to request to cover the gap since `since`."""
    if since is None:
        return 2
    gap = (now - since).total_seconds()
    return max(2, min(MAX_LOOKBACK_MINUTES, int(gap // 60) + 2))

_bybit_symbols: dict[str, set[str]] = {}
_bybit_symbols_at: dict[str, float] = {}


def normalize_asset(asset: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", asset.upper())


def bybit_symbol_candidates(asset: str) -> list[str]:
    """Bybit pair names to try for an asset like 'BTCUSD' or 'XAUUSD'.

    Order matters: the first candidate that Bybit actually lists wins.
    """
    compact = normalize_asset(asset)
    candidates = []

    if compact.endswith("USDT"):
        base = compact[:-4]
        candidates.append(compact)
    elif compact.endswith("USDC"):
        base = compact[:-4]
        candidates.append(compact)
    elif compact.endswith("USD"):
        base = compact[:-3]
        # BTCUSD -> BTCUSDT is the usual perpetual; the plain USD pair exists
        # for a few inverse contracts, so keep it as a second choice.
        candidates.extend([compact + "T", compact])
    else:
        base = compact

    candidates.extend([base + "USDT", base + "USDC"])
    return [c for c in dict.fromkeys(candidates) if c]


async def bybit_symbols(http: httpx.AsyncClient, category: str) -> set[str]:
    """Every pair Bybit lists in a category, cached for BYBIT_SYMBOLS_TTL.

    Diagnostics only — resolution probes individual symbols instead, because
    this payload is ~550KB and too slow to sit on the request path.
    """
    cached = _bybit_symbols.get(category)
    if cached and time.monotonic() - _bybit_symbols_at.get(category, 0) < BYBIT_SYMBOLS_TTL:
        return cached

    try:
        response = await http.get(BYBIT_TICKERS_URL, params={"category": category})
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Could not fetch the Bybit %s pair list: %s", category, error)
        return cached or set()

    if payload.get("retCode") != 0:
        logger.warning("Bybit %s pair list returned retCode %s",
                       category, payload.get("retCode"))
        return cached or set()

    names = {
        row["symbol"]
        for row in (payload.get("result") or {}).get("list") or []
        if row.get("symbol")
    }
    if not names:
        return cached or set()

    _bybit_symbols[category] = names
    _bybit_symbols_at[category] = time.monotonic()
    logger.info("Bybit lists %d %s pairs", len(names), category)
    return names


async def fetch_bybit_price(
    http: httpx.AsyncClient, symbol: str, category: str
) -> Optional[float]:
    """Last traded price for a Bybit symbol, or None if unavailable."""
    try:
        r = await http.get(
            BYBIT_TICKERS_URL, params={"category": category, "symbol": symbol}
        )
        payload = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    # Bybit returns HTTP 200 even for unknown symbols; retCode signals success
    if payload.get("retCode") != 0:
        return None
    tickers = (payload.get("result") or {}).get("list") or []
    if not tickers:
        return None
    try:
        return float(tickers[0]["lastPrice"])
    except (KeyError, ValueError):
        return None


async def fetch_bybit_sample(
    http: httpx.AsyncClient, symbol: str, category: str, minutes: int
) -> Optional[PriceSample]:
    """High/low/close over the last `minutes` 1-minute candles on Bybit."""
    try:
        r = await http.get(
            BYBIT_KLINE_URL,
            params={
                "category": category,
                "symbol": symbol,
                "interval": "1",
                "limit": minutes,
            },
        )
        payload = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if payload.get("retCode") != 0:
        return None

    # Rows are [start_ms, open, high, low, close, volume, turnover], newest first
    rows = (payload.get("result") or {}).get("list") or []
    highs, lows = [], []
    last: Optional[float] = None
    for row in rows:
        try:
            high, low, close = float(row[2]), float(row[3]), float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
        if last is None:
            last = close
        highs.append(high)
        lows.append(low)

    if last is None:
        # Klines unavailable (a brand-new listing, say) — fall back to the
        # ticker so the trade is still checked, just without wick coverage.
        price = await fetch_bybit_price(http, symbol, category)
        return PriceSample.point(price) if price else None
    return PriceSample(last, max(highs), min(lows))


# ---------------------------------------------------------------------------
# Live prices: Gold API (public spot metals — keyless, no account needed)
#
# Covers real spot gold (XAUUSD -> XAU) and spot silver (XAGUSD -> XAG).
# Free, unmetered public JSON API: https://api.gold-api.com/price/{symbol}
# ---------------------------------------------------------------------------

GOLDAPI_BASE_URL = "https://api.gold-api.com/price"


def goldapi_symbol_candidates(asset: str) -> list[str]:
    compact = normalize_asset(asset)
    if compact in ("XAUUSD", "GOLD", "XAU"):
        return ["XAU"]
    if compact in ("XAGUSD", "SILVER", "XAG"):
        return ["XAG"]
    return []


async def fetch_goldapi_price(
    http: httpx.AsyncClient, symbol: str
) -> Optional[float]:
    """Current spot price for XAU (Gold) or XAG (Silver) from gold-api.com."""
    try:
        response = await http.get(
            f"{GOLDAPI_BASE_URL}/{symbol.upper()}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        price = float(payload["price"])
        return price if price > 0 else None
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as error:
        logger.warning("GoldAPI price check failed for %s: %s", symbol, error)
        return None


# ---------------------------------------------------------------------------
# Live prices: Yahoo Finance (public market data — keyless, no account needed)
#
# Covers forex pairs (EURUSD=X), indices (^DJI, ^GSPC, ^IXIC, ^GDAXI, ^FTSE),
# and commodities/oil (CL=F, BZ=F).
# ---------------------------------------------------------------------------

def yahoo_symbol_candidates(asset: str) -> list[str]:
    """Yahoo Finance symbol names to try for an asset like 'EURUSD' or 'US30'."""
    compact = normalize_asset(asset)
    candidates = []

    # Indices & Commodities
    index_map = {
        "US30": "^DJI", "DJI": "^DJI", "DOW": "^DJI",
        "US500": "^GSPC", "SPX500": "^GSPC", "SPX": "^GSPC", "SP500": "^GSPC",
        "US100": "^IXIC", "NAS100": "^IXIC", "NASDAQ": "^IXIC",
        "GER40": "^GDAXI", "GER30": "^GDAXI", "DAX": "^GDAXI",
        "UK100": "^FTSE", "FTSE": "^FTSE",
        "USOIL": "CL=F", "WTI": "CL=F",
        "UKOIL": "BZ=F", "BRENT": "BZ=F",
    }
    if compact in index_map:
        candidates.append(index_map[compact])

    # Forex pairs (e.g., EURUSD -> EURUSD=X)
    if len(compact) == 6 and compact.isalpha():
        candidates.append(f"{compact}=X")

    # Try raw as a fallback
    candidates.append(asset.strip())

    return list(dict.fromkeys(candidates))


async def fetch_yahoo_chart(
    http: httpx.AsyncClient, symbol: str
) -> Optional[dict]:
    """Raw 1-minute chart payload for a Yahoo Finance symbol."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = await http.get(
            url,
            params={"interval": "1m", "range": "1d"},
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


async def fetch_yahoo_sample(
    http: httpx.AsyncClient, symbol: str, minutes: int
) -> Optional[PriceSample]:
    """High/low/last over the last `minutes` 1-minute candles on Yahoo."""
    result = await fetch_yahoo_chart(http, symbol)
    if not result:
        return None

    try:
        last = float(result["meta"]["regularMarketPrice"])
    except (KeyError, TypeError, ValueError):
        return None
    if last <= 0:
        return None

    try:
        quote = result["indicators"]["quote"][0]
        highs = [h for h in (quote.get("high") or [])[-minutes:] if h]
        lows = [low for low in (quote.get("low") or [])[-minutes:] if low]
    except (KeyError, IndexError, TypeError):
        highs, lows = [], []

    # The quote arrays go quiet outside market hours; the last price alone is
    # still a valid (if range-less) reading.
    if not highs or not lows:
        return PriceSample.point(last)
    return PriceSample(last, max(max(highs), last), min(min(lows), last))


# ---------------------------------------------------------------------------
# Provider routing: Yahoo Finance for metals/forex/indices/oil, Bybit for crypto
# ---------------------------------------------------------------------------

async def resolve_market(asset: str) -> Optional[dict]:
    """Pick where to source live prices for a charted asset.

    GoldAPI goes first for spot metals: Gold/Silver (XAUUSD, XAGUSD).
    Yahoo Finance goes next for traditional markets: forex pairs, stock indices, and oil.
    Bybit goes next: it covers crypto (BTCUSD, ETHUSD, SOLUSD...).
    """
    async with httpx.AsyncClient(timeout=20) as http:
        for candidate in goldapi_symbol_candidates(asset):
            if await fetch_goldapi_price(http, candidate) is not None:
                return {"provider": "goldapi", "symbol": candidate}

        for candidate in yahoo_symbol_candidates(asset):
            if await fetch_yahoo_price(http, candidate) is not None:
                return {"provider": "yahoo", "symbol": candidate}

        # Ask Bybit about each candidate directly rather than downloading the
        # whole ticker table. The full linear list is ~550KB and takes ~9s on
        # a good connection, which times out on a free-tier host and silently
        # rejects every asset; a single-symbol probe is ~1KB and answers in
        # milliseconds. Bybit is still the authority on whether a pair exists.
        for candidate in bybit_symbol_candidates(asset):
            for category in BYBIT_CATEGORIES:
                if await fetch_bybit_price(http, candidate, category) is not None:
                    return {
                        "provider": "bybit",
                        "symbol": candidate,
                        "category": category,
                    }
    return None


async def fetch_market_price(
    http: httpx.AsyncClient, market: dict
) -> Optional[float]:
    """Current price for a resolved market, whichever provider carries it."""
    provider = market["provider"]
    if provider == "bybit":
        return await fetch_bybit_price(
            http, market["symbol"], market.get("category", "linear")
        )
    if provider == "goldapi":
        return await fetch_goldapi_price(http, market["symbol"])
    if provider == "yahoo":
        return await fetch_yahoo_price(http, market["symbol"])
    return None


async def fetch_trade_sample(
    http: httpx.AsyncClient, trade: dict, minutes: int
) -> Optional[PriceSample]:
    """What price did over the last `minutes` for a monitored trade."""
    provider = trade.get("provider", "bybit")
    if provider == "bybit":
        return await fetch_bybit_sample(
            http, trade["symbol"], trade.get("category", "linear"), minutes
        )
    if provider == "yahoo":
        return await fetch_yahoo_sample(http, trade["symbol"], minutes)
    if provider == "goldapi":
        # gold-api.com publishes a spot price and nothing else — no OHLC
        # endpoint — so spot metals are the one feed still sampled pointwise
        # and can miss a wick that reverses inside the polling interval.
        price = await fetch_goldapi_price(http, trade["symbol"])
        return PriceSample.point(price) if price else None
    logger.warning(
        "Trade on %s uses retired provider %r — it will expire on its own",
        trade.get("asset"), provider,
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
        if trade.get("fill_direction") == "down":
            if sample.low <= trade["entry"]:
                return "entry"
        elif sample.high >= trade["entry"]:
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


def trade_watched_since(trade: dict, now: datetime) -> Optional[datetime]:
    """When this trade was last checked — the start of the interval to sample.

    Bounding the lookback to the time we have actually been watching matters:
    a trade registered a minute ago must not be resolved by a wick from an
    hour before it existed.
    """
    for key in ("checked_at", "created_at"):
        try:
            return datetime.fromisoformat(trade[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


async def monitor_trade(
    context: ContextTypes.DEFAULT_TYPE,
    http: httpx.AsyncClient,
    trade: dict,
    now: datetime,
) -> None:
    """Check one trade against the price range since it was last checked."""
    trades = state["trades"]
    decimals = trade.get("decimals", 2)

    if trade_expired(trade, now):
        trades.remove(trade)
        save_state()
        await deliver(
            context, trade["chat_id"],
            f"⌛ Stopped monitoring {trade['asset']} — no result after "
            f"{TRADE_TTL_HOURS:g}h. Re-send the chart if the setup is "
            "still valid.",
        )
        return

    # Self-healing: upgrade any legacy futures gold/silver trade to spot metals
    if trade.get("provider") == "yahoo" and trade.get("symbol") in ("GC=F", "SI=F"):
        trade["provider"] = "goldapi"
        trade["symbol"] = "XAU" if trade["symbol"] == "GC=F" else "XAG"
        save_state()

    minutes = lookback_minutes(trade_watched_since(trade, now), now)
    sample = await fetch_trade_sample(http, trade, minutes)
    if sample is None:
        return

    # Only advance the watermark once a reading actually came back, so an
    # outage doesn't blind the bot to what price did while the feed was down.
    trade["checked_at"] = now.isoformat()

    event = check_trade(trade, sample)
    if event is None:
        save_state()
        return

    if event == "entry":
        trade["status"] = "active"
        text = (
            f"✅ {trade['asset']}: price touched your entry "
            f"{trade['entry']:.{decimals}f} — the order should be "
            "filled. Now watching for breakeven, TP and SL."
        )
    elif event == "missed":
        trades.remove(trade)
        text = (
            f"🚪 {trade['asset']}: price reached TP "
            f"({trade['tp']:.{decimals}f}) without ever filling your "
            f"entry at {trade['entry']:.{decimals}f}. The setup played "
            "out without you — no loss taken. Missing a trade costs "
            "nothing; chasing one does. 🧠"
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
        "%s %s -> %s (last %s, range %s-%s over %dm)",
        trade["asset"], trade["direction"], event.upper(),
        sample.last, sample.low, sample.high, minutes,
    )
    save_state()
    await deliver(context, trade["chat_id"], text)


async def monitor_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: check live prices for all monitored trades."""
    if not state["trades"]:
        return

    now = datetime.now(dt_timezone.utc)

    async with httpx.AsyncClient(timeout=15) as http:
        for trade in list(state["trades"]):
            # One bad trade must not abort the cycle for everyone else's.
            # Without this an unexpected key or a malformed price silently
            # stopped every alert in every chat, on every tick, for good.
            try:
                await monitor_trade(context, http, trade, now)
            except Exception:
                logger.exception(
                    "Monitoring failed for %s in chat %s",
                    trade.get("asset"), trade.get("chat_id"),
                )


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


async def handle_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an incoming chart image (photo or image file)."""
    message = update.effective_message

    if message.photo:
        file = await message.photo[-1].get_file()
        mime_type = "image/jpeg"  # Telegram re-encodes photos as JPEG
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file = await message.document.get_file()
        mime_type = message.document.mime_type
    else:
        return

    register_chat(message.chat_id)
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
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

        # Resolve the price feed before replying: its live price is what
        # decides whether this is a market execution or a pending order, and
        # it is a far better authority on that than a screenshot's price label.
        market = await resolve_market(data.asset)
        live_price = None
        if market:
            async with httpx.AsyncClient(timeout=15) as http:
                live_price = await fetch_market_price(http, market)

        decimals = signal_decimals(data)
        await message.reply_text(build_signal_message(data, live_price))

        if data.position_count > 1:
            await message.reply_text(
                f"👀 I found {data.position_count} position tools on that chart "
                "and read the most recent one (furthest right). Crop to a single "
                "position if you meant a different one."
            )

        if not market:
            await message.reply_text(
                f"ℹ️ Live monitoring isn't available for {data.asset.upper()} "
                f"— no price feed lists it, so breakeven/TP/SL alerts are off "
                f"for this trade."
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
        state["trades"].append({
            "chat_id": message.chat_id,
            "asset": data.asset.upper(),
            "provider": market["provider"],
            "symbol": symbol,
            "category": market.get("category", "linear"),
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
            "fill_direction": fill,
            "created_at": utcnow_iso(),
        })
        save_state()

        if fill:
            expiry = (
                f"\nMonitoring stops automatically after {TRADE_TTL_HOURS:g}h."
                if TRADE_TTL_HOURS > 0 else ""
            )
            away = "above" if fill == "up" else "below"
            await message.reply_text(
                f"⏳ Pending order — entry sits {away} the market"
                + (f" ({live_price:.{decimals}f})" if live_price else "")
                + f", so nothing is open yet. I'll tell you when price reaches "
                f"{data.entry:.{decimals}f}, then watch for breakeven "
                f"({be_price:.{decimals}f}), TP and SL.{expiry}"
            )
        else:
            await message.reply_text(
                f"🔴 Market execution — entry is at the current price"
                + (f" ({live_price:.{decimals}f})" if live_price else "")
                + ", so I'm treating this as already open.\n"
                f"I'll alert you to move SL to breakeven at "
                f"{be_price:.{decimals}f} (30% of the way to TP), and again "
                "when TP or SL is hit."
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
        "I check the live price to tell a market execution from a pending "
        "LIMIT/STOP order, then monitor the trade: when a pending order fills, "
        "when to move SL to breakeven at 30% of the way to TP, and when TP or "
        "SL is hit.\n\n"
        "/trades — what I'm currently watching\n"
        "/cancel — stop watching a trade"
        + (f"\n\n{warning}" if warning else "")
    )


BOT_COMMANDS = [
    ("start", "How to use the bot"),
    ("trades", "List trades being monitored"),
    ("cancel", "Stop monitoring a trade"),
]


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
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_chart))

    # Each run samples the price range since the previous one, so a tick
    # missed while the host slept is covered by the next one's lookback
    # rather than lost.
    app.job_queue.run_repeating(monitor_trades, interval=MONITOR_INTERVAL, first=10)

    logger.info(
        "Monitoring %d trade(s) every %ds on interval high/low",
        len(state["trades"]), MONITOR_INTERVAL,
    )
    logger.info("Live prices: GoldAPI (spot metals) + Yahoo Finance (forex/indices/oil) + Bybit (crypto)")

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
