"""Automated Trading Chart Vision Bot.

Receives a chart screenshot on Telegram, extracts trade parameters with
Gemini Flash vision, calculates profit/loss percentages in code (never
trusting the AI with math), and replies with a formatted signal:

    [ASSET] [ACTION] [ORDER_TYPE]
    ENTRY: [VALUE]
    SL: [VALUE]
    TP: [VALUE]
    Profit: +[X]% / Loss: -[Y]%

Extra features:
- Live trade monitoring (Bybit for crypto and gold, Twelve Data for forex):
  alerts when a pending order fills, when to move SL to breakeven once price
  covers 30% of the distance to TP, and a motivation message on TP or SL.
  Chart assets are matched against each provider's real instrument list
  rather than a guessed suffix.
- Morning and night motivation texts to every chat that has used the bot.
  Delivery is state-driven rather than a fixed timer, so a message missed
  while the host was asleep is still sent when the bot wakes up.
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
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "8"))
NIGHT_HOUR = int(os.environ.get("NIGHT_HOUR", "22"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# Twelve Data supplies forex prices, which Bybit does not list. Get a free key
# at https://twelvedata.com/. Without one, forex charts get a signal but no
# live monitoring; crypto and gold are unaffected either way.
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "").strip()
# Free tier allows 800 credits/day and one symbol costs one credit. Default
# leaves headroom; polling is paced to fit whatever budget is set here.
TWELVEDATA_DAILY_CREDITS = int(os.environ.get("TWELVEDATA_DAILY_CREDITS", "700"))

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

# How often to check whether a scheduled text is due (seconds)
SCHEDULE_INTERVAL = 300
# A scheduled text more than this many hours late is dropped rather than sent
# at a nonsensical time (e.g. the morning text arriving at 6pm).
CATCHUP_HOURS = 6

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
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


def local_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown TIMEZONE %r, falling back to UTC", TIMEZONE)
        return ZoneInfo("UTC")


def load_state() -> dict:
    data: dict = {}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read %s, starting fresh", STATE_FILE)
    data.setdefault("chats", [])
    data.setdefault("trades", [])
    # Date (YYYY-MM-DD, local) each scheduled text was last delivered
    data.setdefault("last_sent", {})
    # Migrate trades written by older versions, which lacked these fields
    now = utcnow_iso()
    for trade in data["trades"]:
        trade.setdefault("status", "active")
        trade.setdefault("created_at", now)
        trade.setdefault("provider", "bybit")
    return data


def save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        logger.exception("Could not save state")


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


class TradeData(BaseModel):
    """A complete, validated trade setup extracted from a chart."""

    asset: str
    direction: Literal["LONG", "SHORT"]
    entry: float
    stop_loss: float
    take_profit: float
    current_price: Optional[float] = None


VISION_PROMPT = """\
You are analyzing an image that should be a screenshot of a trading chart \
(e.g. TradingView) with a long/short position tool drawn on it.

First decide: is this actually a trading chart with a visible position tool \
(entry, stop loss, and take profit levels)? If it is NOT — any other kind of \
image, or a chart without a position tool, or a chart whose price levels are \
unreadable — set is_trading_chart to false and leave every other field null.

If it IS such a chart, set is_trading_chart to true and extract the values.

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
- current_price is the price the market is currently trading at (usually the \
highlighted label on the right price axis at the last candle). If you cannot \
determine it, set it to null.

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


def entry_fill_direction(data: TradeData) -> Optional[str]:
    """Which way price must travel to reach entry: 'up', 'down', or None.

    None means the order fills immediately at market — either entry is already
    at the current price, or the chart gave us no current price to compare to.
    """
    current = data.current_price
    if current is None or current <= 0:
        return None
    if abs(data.entry - current) / data.entry < MARKET_ORDER_TOLERANCE:
        return None
    return "up" if data.entry > current else "down"


def determine_order_type(data: TradeData) -> str:
    """Deduce the pending-order type by comparing entry to current price."""
    action = "SELL" if data.direction == "SHORT" else "BUY"
    fill = entry_fill_direction(data)
    if fill is None:
        return action

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


def build_signal_message(data: TradeData) -> str:
    profit, loss = calculate_percentages(data)
    decimals = signal_decimals(data)
    entry, sl, tp = (
        f"{p:.{decimals}f}" for p in (data.entry, data.stop_loss, data.take_profit)
    )
    header = f"{data.asset.upper()} {determine_order_type(data)}"
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


# ---------------------------------------------------------------------------
# Live prices: Twelve Data (forex — needs TWELVEDATA_API_KEY)
#
# Bybit lists no forex, so pairs like EURUSD come from Twelve Data instead.
# Its free tier allows 800 credits/day and one symbol costs one credit, so
# polling is paced to fit the daily budget rather than run at the 60s rate
# used for crypto: with N forex trades open, a poll happens at most every
# 86400 * N / TWELVEDATA_DAILY_CREDITS seconds. Between polls the previous
# snapshot is reused, so alerts land up to that late — not never.
# ---------------------------------------------------------------------------

TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
# Reference data (the pair list) doesn't consume credits; cache it a day
TWELVEDATA_PAIRS_TTL = 24 * 3600
# Never poll faster than this even if the budget would allow it
TWELVEDATA_MIN_INTERVAL = 120

_td_pairs: Optional[set[str]] = None
_td_pairs_at = 0.0
_td_prices: dict[str, float] = {}
_td_prices_at = 0.0


def twelvedata_enabled() -> bool:
    return bool(TWELVEDATA_API_KEY)


def twelvedata_spacing(symbol_count: int) -> float:
    """Minimum seconds between polls to stay inside the daily credit budget."""
    if symbol_count <= 0 or TWELVEDATA_DAILY_CREDITS <= 0:
        return TWELVEDATA_MIN_INTERVAL
    return max(
        TWELVEDATA_MIN_INTERVAL, 86400.0 * symbol_count / TWELVEDATA_DAILY_CREDITS
    )


def twelvedata_symbol_candidates(asset: str) -> list[str]:
    """Twelve Data pair names to try for an asset like 'EURUSD'."""
    compact = normalize_asset(asset)
    candidates = []
    if len(compact) == 6:
        candidates.append(f"{compact[:3]}/{compact[3:]}")
    candidates.append(compact)
    return list(dict.fromkeys(candidates))


def twelvedata_error(payload: dict) -> Optional[str]:
    """The error message if this payload is an error envelope, else None."""
    if isinstance(payload, dict) and payload.get("status") == "error":
        return f"{payload.get('code')}: {payload.get('message')}"
    return None


async def twelvedata_get(
    http: httpx.AsyncClient, path: str, **params
) -> Optional[dict]:
    """GET a Twelve Data endpoint, returning parsed JSON or None on failure."""
    try:
        response = await http.get(
            f"{TWELVEDATA_BASE_URL}{path}",
            params={**params, "apikey": TWELVEDATA_API_KEY},
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Twelve Data request to %s failed: %s", path, error)
        return None

    error = twelvedata_error(payload)
    if error:
        logger.warning("Twelve Data %s returned an error — %s", path, error)
        return None
    return payload


async def twelvedata_pairs(http: httpx.AsyncClient) -> set[str]:
    """Every forex pair Twelve Data lists, cached for TWELVEDATA_PAIRS_TTL."""
    global _td_pairs, _td_pairs_at
    if _td_pairs is not None and time.monotonic() - _td_pairs_at < TWELVEDATA_PAIRS_TTL:
        return _td_pairs

    payload = await twelvedata_get(http, "/forex_pairs")
    if payload is None:
        return _td_pairs or set()
    names = {
        row["symbol"] for row in payload.get("data") or [] if row.get("symbol")
    }
    if not names:
        return _td_pairs or set()

    _td_pairs, _td_pairs_at = names, time.monotonic()
    logger.info("Twelve Data lists %d forex pairs", len(names))
    return names


def parse_twelvedata_prices(payload: dict, symbols: list[str]) -> dict[str, float]:
    """Read prices from either response shape.

    One symbol returns {"price": "1.08"}; several return a mapping keyed by
    symbol, where an individual entry may itself be an error object.
    """
    def as_price(value) -> Optional[float]:
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    if "price" in payload and len(symbols) == 1:
        price = as_price(payload["price"])
        return {symbols[0]: price} if price else {}

    prices = {}
    for symbol in symbols:
        row = payload.get(symbol)
        if not isinstance(row, dict):
            continue
        error = twelvedata_error(row)
        if error:
            logger.warning("Twelve Data has no price for %s — %s", symbol, error)
            continue
        price = as_price(row.get("price"))
        if price:
            prices[symbol] = price
    return prices


async def twelvedata_prices(
    http: httpx.AsyncClient, symbols: set[str]
) -> dict[str, float]:
    """Current prices for the given pairs, paced to the free-tier budget."""
    global _td_prices, _td_prices_at
    if not symbols or not twelvedata_enabled():
        return {}

    spacing = twelvedata_spacing(len(symbols))
    if _td_prices_at and time.monotonic() - _td_prices_at < spacing:
        return _td_prices  # too soon to spend more credits; reuse the snapshot

    ordered = sorted(symbols)
    payload = await twelvedata_get(http, "/price", symbol=",".join(ordered))
    if payload is None:
        return _td_prices  # keep serving the last good snapshot

    prices = parse_twelvedata_prices(payload, ordered)
    if prices:
        _td_prices, _td_prices_at = prices, time.monotonic()
    return prices or _td_prices


# ---------------------------------------------------------------------------
# Provider routing: Bybit for crypto and gold, Twelve Data for forex
# ---------------------------------------------------------------------------

async def resolve_market(asset: str) -> Optional[dict]:
    """Pick where to source live prices for a charted asset.

    Bybit goes first: it is keyless, unmetered and covers crypto plus gold.
    Twelve Data picks up forex, which Bybit does not list at all.
    """
    async with httpx.AsyncClient(timeout=20) as http:
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

        if twelvedata_enabled():
            pairs = await twelvedata_pairs(http)
            for candidate in twelvedata_symbol_candidates(asset):
                if candidate in pairs:
                    return {"provider": "twelvedata", "symbol": candidate}
    return None


async def fetch_trade_price(
    http: httpx.AsyncClient, trade: dict, quotes: dict[str, float]
) -> Optional[float]:
    """Current price for a monitored trade.

    Twelve Data prices arrive pre-fetched for the whole cycle; Bybit is
    queried per symbol. Trades registered against a provider that has since
    been removed yield None, so they are skipped rather than priced against
    the wrong market, and the TTL retires them on its own.
    """
    provider = trade.get("provider", "bybit")
    if provider == "bybit":
        return await fetch_bybit_price(
            http, trade["symbol"], trade.get("category", "linear")
        )
    if provider == "twelvedata":
        return quotes.get(trade["symbol"])
    logger.warning(
        "Trade on %s uses retired provider %r — it will expire on its own",
        trade.get("asset"), provider,
    )
    return None


def check_trade(trade: dict, price: float) -> Optional[str]:
    """Return the event for this trade at this price, or None.

    A pending order (LIMIT/STOP) is not a position yet, so it can only report
    'entry' (price touched the entry level, order filled) or 'missed' (price
    ran all the way to TP without ever filling — the setup is void).

    Once filled, the trade reports 'tp', 'sl' or 'breakeven'.
    """
    long = trade["direction"] == "LONG"
    reached_tp = (price >= trade["tp"]) if long else (price <= trade["tp"])

    if trade.get("status") == "pending":
        if (price <= trade["entry"]) if trade.get("fill_direction") == "down" \
                else (price >= trade["entry"]):
            return "entry"
        return "missed" if reached_tp else None

    if reached_tp:
        return "tp"
    if (price <= trade["sl"]) if long else (price >= trade["sl"]):
        return "sl"
    if not trade["be_alerted"]:
        if (price >= trade["be_price"]) if long else (price <= trade["be_price"]):
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

FALLBACK_MORNING = [
    "🌅 Good morning, trader! Plan your trades, trade your plan. Discipline "
    "today compounds into freedom tomorrow. 📈",
    "🌅 Morning! Remember: you don't have to catch every move — you only have "
    "to catch YOUR setup. Patience is a position too. 💪",
    "🌅 Rise and grind! Risk management first, profits second. Protect your "
    "capital and the wins will follow. 🚀",
]

FALLBACK_NIGHT = [
    "🌙 Markets close, the work doesn't. Journal today's trades — what you "
    "did right matters as much as what you'd change. Rest well; a sharp mind "
    "is your real edge. 😴",
    "🌙 Day's done. Green or red, you followed a process — that's the part "
    "you control. Screens off, review tomorrow with fresh eyes. 🧠",
    "🌙 Wrap it up for today. No revenge trades, no late-night chasing. The "
    "market will hand out new setups tomorrow, and you'll be rested for "
    "them. 🌟",
]


async def deliver(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    """Push a message to a chat; a delivery failure must not abort the job."""
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Could not deliver message to chat %s", chat_id)


async def monitor_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: check live prices for all monitored trades."""
    trades = state["trades"]
    if not trades:
        return

    now = datetime.now(dt_timezone.utc)

    async with httpx.AsyncClient(timeout=15) as http:
        # One Twelve Data request covers every forex trade this cycle
        quotes = await twelvedata_prices(
            http, {t["symbol"] for t in trades if t.get("provider") == "twelvedata"}
        )

        for trade in list(trades):
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
                continue

            price = await fetch_trade_price(http, trade, quotes)
            if price is None:
                continue

            event = check_trade(trade, price)
            if event is None:
                continue

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

            save_state()
            await deliver(context, trade["chat_id"], text)


# ---------------------------------------------------------------------------
# Scheduled motivation texts (morning + night)
#
# These are driven by the date recorded in state, not by a fire-once timer:
# a free host that sleeps through the scheduled minute would silently skip an
# APScheduler job, whereas here the bot notices on its next check that today's
# text hasn't gone out yet and sends it (up to CATCHUP_HOURS late).
# ---------------------------------------------------------------------------

SCHEDULE_PROMPTS = {
    "morning": (
        "Write a short, energetic good-morning motivation message for a day "
        "trader. 2-3 sentences, include one practical reminder about discipline "
        "or risk management, a couple of fitting emojis, no hashtags, no "
        "preamble — output the message text only."
    ),
    "night": (
        "Write a short, calm end-of-day message for a day trader who is "
        "finishing their trading session. 2-3 sentences: encourage them to "
        "review or journal today's trades, discourage revenge trading, and "
        "remind them that rest sharpens judgement. A couple of fitting emojis, "
        "no hashtags, no preamble — output the message text only."
    ),
}

SCHEDULE_FALLBACKS = {"morning": FALLBACK_MORNING, "night": FALLBACK_NIGHT}


def generate_motivation(slot: str = "morning") -> str:
    """Fresh motivation text via Gemini, with a static fallback (sync)."""
    for model in GEMINI_MODELS:
        try:
            response = gemini_client.models.generate_content(
                model=model, contents=SCHEDULE_PROMPTS[slot]
            )
            text = (response.text or "").strip()
            if text:
                return text
        except Exception as error:  # noqa: BLE001 - fall through to next model
            logger.warning("Model %s failed for %s text: %s", model, slot, error)
    logger.info("Using a static fallback for the %s text", slot)
    return random.choice(SCHEDULE_FALLBACKS[slot])


async def broadcast_motivation(
    context: ContextTypes.DEFAULT_TYPE, slot: str
) -> None:
    text = await asyncio.to_thread(generate_motivation, slot)
    chats = list(state["chats"])
    logger.info("Sending %s text to %d chat(s)", slot, len(chats))
    for chat_id in chats:
        await deliver(context, chat_id, text)


async def scheduled_texts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: send any motivation text that is due and unsent today."""
    if not state["chats"]:
        return

    now = datetime.now(local_timezone())
    today = now.date().isoformat()

    for slot, hour in (("morning", MORNING_HOUR), ("night", NIGHT_HOUR)):
        if state["last_sent"].get(slot) == today:
            continue
        due = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now < due:
            continue

        # Record the attempt before sending: a delivery failure must not make
        # the bot retry on every tick for the rest of the day.
        state["last_sent"][slot] = today
        save_state()

        late = now - due
        if late > timedelta(hours=CATCHUP_HOURS):
            logger.info(
                "Skipping the %s text for %s — %.1fh late (limit %sh)",
                slot, today, late.total_seconds() / 3600, CATCHUP_HOURS,
            )
            continue

        if late > timedelta(seconds=SCHEDULE_INTERVAL):
            logger.info("Catching up on the %s text (%.1fh late)",
                        slot, late.total_seconds() / 3600)
        await broadcast_motivation(context, slot)


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
    decimals = trade.get("decimals", 2)
    status = "⏳ pending entry" if trade.get("status") == "pending" else "🔴 live"
    return (
        f"{index}. {trade['asset']} {trade['direction']} — {status}\n"
        f"   entry {trade['entry']:.{decimals}f} · "
        f"SL {trade['sl']:.{decimals}f} · TP {trade['tp']:.{decimals}f}"
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
        "📋 Monitored trades:\n\n" + "\n".join(lines)
        + "\n\nUse /cancel <number> to stop one, or /cancel all."
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


async def send_motivation_now(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot: str
) -> None:
    """Generate and send one motivation text immediately, for testing."""
    message = update.effective_message
    register_chat(message.chat_id)
    await context.bot.send_chat_action(
        chat_id=message.chat_id, action=ChatAction.TYPING
    )
    text = await asyncio.to_thread(generate_motivation, slot)
    await message.reply_text(text)


async def motivate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/motivate — send the morning text now instead of waiting for it."""
    await send_motivation_now(update, context, "morning")


async def night(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/night — send the night text now instead of waiting for it."""
    await send_motivation_now(update, context, "night")


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

        await message.reply_text(build_signal_message(data))

        # Register the trade for live monitoring (Bybit, or Twelve Data for forex)
        market = await resolve_market(data.asset)
        if not market:
            hint = "" if twelvedata_enabled() else (
                " Set TWELVEDATA_API_KEY to enable forex monitoring."
            )
            await message.reply_text(
                f"ℹ️ Live monitoring isn't available for {data.asset.upper()} "
                f"— no price feed lists it, so breakeven/TP/SL alerts are off "
                f"for this trade.{hint}"
            )
            return

        symbol = market["symbol"]
        decimals = signal_decimals(data)

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
        fill = entry_fill_direction(data)
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
            # A pending order isn't a position yet — no TP/SL/breakeven alerts
            # until price actually touches the entry level.
            "status": "pending" if fill else "active",
            "fill_direction": fill,
            "created_at": utcnow_iso(),
        })
        save_state()

        # Forex is polled on a free data plan, so be honest about the cadence
        cadence = ""
        if market["provider"] == "twelvedata":
            open_pairs = {
                t["symbol"] for t in state["trades"]
                if t.get("provider") == "twelvedata"
            }
            minutes = twelvedata_spacing(len(open_pairs)) / 60
            cadence = (
                f"\n⏱ Forex prices are checked about every {minutes:.0f} min "
                "(free data plan), so alerts can be that late."
            )

        if fill:
            expiry = (
                f"\nMonitoring stops automatically after {TRADE_TTL_HOURS:g}h."
                if TRADE_TTL_HOURS > 0 else ""
            )
            await message.reply_text(
                f"🔔 Pending order registered. I'll tell you when price "
                f"reaches your entry at {data.entry:.{decimals}f}, then watch "
                f"for breakeven ({be_price:.{decimals}f}), TP and SL."
                f"{expiry}{cadence}"
            )
        else:
            await message.reply_text(
                f"🔔 Trade is being monitored live.\n"
                f"I'll alert you to move SL to breakeven at "
                f"{be_price:.{decimals}f} (30% of the way to TP), and again "
                f"when TP or SL is hit.{cadence}"
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_chat(update.effective_message.chat_id)
    await update.effective_message.reply_text(
        "👋 Send me a screenshot of a trading chart with a long/short position "
        "tool drawn on it (entry, stop loss, take profit) and I'll reply with a "
        "formatted signal.\n\n"
        "I'll also monitor the trade live: I tell you when a pending order "
        "fills, when to move SL to breakeven at 30% of the way to TP, and "
        "when TP/SL hits — plus a motivation text every morning 🌅 and "
        "night 🌙.\n\n"
        "/trades — what I'm currently watching\n"
        "/cancel — stop watching a trade\n"
        "/motivate — morning text now\n"
        "/night — night text now"
    )


BOT_COMMANDS = [
    ("start", "How to use the bot"),
    ("trades", "List trades being monitored"),
    ("cancel", "Stop monitoring a trade"),
    ("motivate", "Send the morning text now"),
    ("night", "Send the night text now"),
]


async def register_commands(app: Application) -> None:
    """Populate the in-app command menu (best effort — never block startup)."""
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.warning("Could not set the bot command menu", exc_info=True)


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
    app.add_handler(CommandHandler("motivate", motivate))
    app.add_handler(CommandHandler("night", night))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_chart))

    # Background jobs: trade monitoring + the morning/night texts. Both are
    # repeating checks rather than one-shot daily timers, so a missed window
    # (host asleep, restart) is recovered instead of silently skipped.
    app.job_queue.run_repeating(monitor_trades, interval=MONITOR_INTERVAL, first=10)
    app.job_queue.run_repeating(scheduled_texts, interval=SCHEDULE_INTERVAL, first=15)

    tz = local_timezone()
    logger.info(
        "Motivation texts: morning %02d:00, night %02d:00 (%s, now %s)",
        MORNING_HOUR, NIGHT_HOUR, tz.key,
        datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
    )
    logger.info(
        "Live prices: Bybit (crypto, gold) + %s",
        f"Twelve Data (forex, {TWELVEDATA_DAILY_CREDITS} credits/day)"
        if twelvedata_enabled()
        else "no forex feed — set TWELVEDATA_API_KEY to enable it",
    )

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
