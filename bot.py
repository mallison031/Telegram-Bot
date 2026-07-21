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
- Live trade monitoring (OANDA v20 REST API, with Bybit as a crypto fallback):
  alerts when a pending order fills, when to move SL to breakeven once price
  covers 30% of the distance to TP, and a motivation message on TP or SL.
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

# OANDA credentials. Generate a personal access token in the OANDA account
# portal -> Manage API Access. A practice token carries the same live prices
# as a live one. Without a token OANDA is skipped and Bybit is used alone.
OANDA_ACCESS_TOKEN = os.environ.get("OANDA_ACCESS_TOKEN", "").strip()
OANDA_ENV = os.environ.get("OANDA_ENV", "practice").strip().lower()
# Optional: pin a specific account. Left blank, the first one is used.
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()

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

OANDA_HOST = (
    "api-fxpractice.oanda.com" if OANDA_ENV == "practice"
    else "api-fxtrade.oanda.com"
)
OANDA_BASE_URL = f"https://{OANDA_HOST}"

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
# Bybit market categories to search for a pair, in order of preference:
# linear = USDT perpetual futures (most leveraged pairs), spot = spot market
BYBIT_CATEGORIES = ("linear", "spot")

# Tried in order — first one that responds wins. The newest Flash models on the
# free tier intermittently return 503 (high demand), so we keep fallbacks.
GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3-flash-preview",
]

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
    last_error: Exception | None = None
    for model in GEMINI_MODELS:
        try:
            response = gemini_client.models.generate_content(
                model=model,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    VISION_PROMPT,
                ],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ChartAnalysis,
                    temperature=0,
                ),
            )
        except Exception as error:  # noqa: BLE001 - fall through to next model
            logger.warning("Model %s failed: %s", model, error)
            last_error = error
            continue
        parsed = response.parsed
        if isinstance(parsed, ChartAnalysis):
            return parsed
        return ChartAnalysis.model_validate_json(response.text)
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
# Live prices, provider 1: OANDA v20 REST API (needs OANDA_ACCESS_TOKEN)
#
# A practice token carries the same live market feed as a live one — only
# order execution differs, and this bot never places orders. The pricing
# endpoint takes a CSV list of instruments, so one request per cycle prices
# every open trade no matter how many there are.
# ---------------------------------------------------------------------------

# OANDA names instruments "EUR_USD", "XAU_USD", "US30_USD". Anything not
# listed here falls back to the generic 6-letter -> "XXX_YYY" rule, and every
# candidate is checked against the account's real instrument list anyway.
OANDA_ALIASES = {
    "XAUUSD": "XAU_USD", "GOLD": "XAU_USD",
    "XAGUSD": "XAG_USD", "SILVER": "XAG_USD",
    "USOIL": "WTICO_USD", "WTI": "WTICO_USD", "CRUDE": "WTICO_USD",
    "UKOIL": "BCO_USD", "BRENT": "BCO_USD", "NATGAS": "NATGAS_USD",
    "US30": "US30_USD", "DJI": "US30_USD", "DOW": "US30_USD",
    "NAS100": "NAS100_USD", "USTEC": "NAS100_USD", "NASDAQ": "NAS100_USD",
    "SPX500": "SPX500_USD", "US500": "SPX500_USD", "SP500": "SPX500_USD",
    "GER30": "DE30_EUR", "GER40": "DE30_EUR", "DAX": "DE30_EUR",
    "UK100": "UK100_GBP", "FTSE": "UK100_GBP",
    "JPN225": "JP225_USD", "NIKKEI": "JP225_USD",
    "AUS200": "AU200_AUD", "HK50": "HK33_HKD",
}

_oanda_account_id: Optional[str] = None
_oanda_instruments: Optional[set[str]] = None


def oanda_enabled() -> bool:
    return bool(OANDA_ACCESS_TOKEN)


def oanda_headers() -> dict:
    return {
        "Authorization": f"Bearer {OANDA_ACCESS_TOKEN}",
        "Accept-Datetime-Format": "RFC3339",
    }


def normalize_asset(asset: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", asset.upper())


def oanda_symbol_candidates(asset: str) -> list[str]:
    """OANDA instrument names to try for an asset like 'EURUSD' or 'XAUUSD'."""
    compact = normalize_asset(asset)
    candidates = []
    if compact in OANDA_ALIASES:
        candidates.append(OANDA_ALIASES[compact])
    # Crypto charts often read as USDT pairs; OANDA quotes them against USD
    if compact.endswith("USDT"):
        compact = compact[:-1]
    if len(compact) == 6:
        candidates.append(f"{compact[:3]}_{compact[3:]}")
    candidates.append(compact)
    return list(dict.fromkeys(candidates))


def oanda_price(row: dict) -> Optional[float]:
    """Mid price from an OANDA price row, preferring the closeout quotes."""
    def mid(bid, ask) -> Optional[float]:
        try:
            bid, ask = float(bid), float(ask)
        except (TypeError, ValueError):
            return None
        return (bid + ask) / 2 if bid > 0 and ask > 0 else None

    price = mid(row.get("closeoutBid"), row.get("closeoutAsk"))
    if price is not None:
        return price
    try:
        return mid(row["bids"][0]["price"], row["asks"][0]["price"])
    except (KeyError, IndexError, TypeError):
        return None


async def oanda_get(http: httpx.AsyncClient, path: str, **params) -> Optional[dict]:
    """GET an OANDA endpoint, returning parsed JSON or None on any failure."""
    try:
        response = await http.get(
            f"{OANDA_BASE_URL}{path}", params=params or None, headers=oanda_headers()
        )
    except httpx.HTTPError as error:
        logger.warning("OANDA request to %s failed: %s", path, error)
        return None

    if response.status_code in (401, 403):
        logger.warning(
            "OANDA rejected the token (HTTP %s) — check OANDA_ACCESS_TOKEN and "
            "OANDA_ENV (%s, host %s)",
            response.status_code, OANDA_ENV, OANDA_HOST,
        )
        return None
    if response.status_code != 200:
        logger.warning("OANDA %s returned HTTP %s", path, response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning("OANDA %s returned a non-JSON response", path)
        return None


async def oanda_account_id(http: httpx.AsyncClient) -> Optional[str]:
    """The account to price against — from config, else the first one found."""
    global _oanda_account_id
    if _oanda_account_id:
        return _oanda_account_id
    if OANDA_ACCOUNT_ID:
        _oanda_account_id = OANDA_ACCOUNT_ID
        return _oanda_account_id

    payload = await oanda_get(http, "/v3/accounts")
    accounts = (payload or {}).get("accounts") or []
    if not accounts:
        logger.warning("OANDA returned no accounts for this token")
        return None
    _oanda_account_id = accounts[0].get("id")
    if len(accounts) > 1:
        logger.info(
            "OANDA token has %d accounts, using %s (set OANDA_ACCOUNT_ID to pin one)",
            len(accounts), _oanda_account_id,
        )
    else:
        logger.info("OANDA account %s (%s)", _oanda_account_id, OANDA_HOST)
    return _oanda_account_id


async def oanda_instruments(http: httpx.AsyncClient) -> set[str]:
    """Instrument names this account can price, fetched once and cached."""
    global _oanda_instruments
    if _oanda_instruments is not None:
        return _oanda_instruments

    account = await oanda_account_id(http)
    if not account:
        return set()
    payload = await oanda_get(http, f"/v3/accounts/{account}/instruments")
    if payload is None:
        return set()  # transient: leave uncached so we retry next time
    names = {
        item["name"] for item in payload.get("instruments") or [] if item.get("name")
    }
    _oanda_instruments = names
    logger.info("OANDA offers %d instruments on this account", len(names))
    return names


async def oanda_prices(
    http: httpx.AsyncClient, symbols: set[str]
) -> dict[str, float]:
    """Current mid prices for the given instruments: {name: price}.

    Returns an empty dict on any failure — trades are simply skipped this
    cycle rather than acting on a stale or missing quote.
    """
    if not symbols or not oanda_enabled():
        return {}
    account = await oanda_account_id(http)
    if not account:
        return {}

    payload = await oanda_get(
        http, f"/v3/accounts/{account}/pricing", instruments=",".join(sorted(symbols))
    )
    if payload is None:
        return {}

    prices = {}
    for row in payload.get("prices") or []:
        name = row.get("instrument")
        price = oanda_price(row)
        if name and price:
            prices[name] = price
    return prices


# ---------------------------------------------------------------------------
# Live prices, provider 2: Bybit public API (no key required)
# ---------------------------------------------------------------------------

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


async def resolve_bybit_symbol(
    http: httpx.AsyncClient, asset: str
) -> Optional[tuple[str, str]]:
    """Map an asset like BTCUSD to a Bybit (symbol, category), if it exists."""
    compact = normalize_asset(asset)
    if compact.endswith("USDT"):
        candidates = [compact]
    elif compact.endswith("USD"):
        candidates = [compact + "T"]
    else:
        candidates = [compact + "USDT"]

    for symbol in candidates:
        for category in BYBIT_CATEGORIES:
            if await fetch_bybit_price(http, symbol, category) is not None:
                return symbol, category
    return None


# ---------------------------------------------------------------------------
# Provider routing: OANDA first (forex, metals, indices), Bybit for crypto
# ---------------------------------------------------------------------------

async def resolve_market(asset: str) -> Optional[dict]:
    """Pick where to source live prices for an asset.

    OANDA is preferred because it covers forex, metals and indices, which
    Bybit does not list. Bybit is the fallback for crypto pairs (and for
    everything, when no OANDA token is configured).
    """
    async with httpx.AsyncClient(timeout=15) as http:
        if oanda_enabled():
            available = {name.upper(): name for name in await oanda_instruments(http)}
            for candidate in oanda_symbol_candidates(asset):
                symbol = available.get(candidate.upper())
                if symbol:
                    return {"provider": "oanda", "symbol": symbol}

        resolved = await resolve_bybit_symbol(http, asset)
        if resolved:
            symbol, category = resolved
            return {"provider": "bybit", "symbol": symbol, "category": category}
    return None


async def fetch_trade_price(
    http: httpx.AsyncClient, trade: dict, quotes: dict[str, float]
) -> Optional[float]:
    """Current price for a monitored trade, from whichever provider owns it.

    OANDA prices arrive pre-fetched for the whole cycle; Bybit is queried per
    symbol. An unknown provider yields None, so the trade is skipped rather
    than priced against the wrong market.
    """
    provider = trade.get("provider", "bybit")
    if provider == "oanda":
        return quotes.get(trade["symbol"])
    if provider == "bybit":
        return await fetch_bybit_price(
            http, trade["symbol"], trade.get("category", "linear")
        )
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
        # One OANDA request prices every OANDA-backed trade this cycle
        quotes = await oanda_prices(
            http, {t["symbol"] for t in trades if t.get("provider") == "oanda"}
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

        # Register the trade for live monitoring (OANDA, or Bybit for crypto)
        market = await resolve_market(data.asset)
        if not market:
            hint = "" if oanda_enabled() else (
                " Set OANDA_ACCESS_TOKEN to enable forex, metals and indices."
            )
            await message.reply_text(
                f"ℹ️ Live monitoring isn't available for {data.asset.upper()} "
                f"(no matching instrument) — breakeven/TP/SL alerts are off "
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

        if fill:
            expiry = (
                f"\nMonitoring stops automatically after {TRADE_TTL_HOURS:g}h."
                if TRADE_TTL_HOURS > 0 else ""
            )
            await message.reply_text(
                f"🔔 Pending order registered. I'll tell you when price "
                f"reaches your entry at {data.entry:.{decimals}f}, then watch "
                f"for breakeven ({be_price:.{decimals}f}), TP and SL."
                f"{expiry}"
            )
        else:
            await message.reply_text(
                f"🔔 Trade is being monitored live.\n"
                f"I'll alert you to move SL to breakeven at "
                f"{be_price:.{decimals}f} (30% of the way to TP), and again "
                f"when TP or SL is hit."
            )
    except Exception:
        logger.exception("Failed to process chart")
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
        "Live prices: %s",
        f"OANDA ({OANDA_HOST}) with Bybit fallback" if oanda_enabled()
        else "Bybit only — set OANDA_ACCESS_TOKEN for forex/metals/indices",
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
