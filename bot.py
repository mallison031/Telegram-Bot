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
- Live trade monitoring (Binance public API): alerts to move SL to breakeven
  once price covers 30% of the distance to TP, and sends a motivation message
  when TP or SL is hit.
- Daily morning motivation text to every chat that has used the bot.
"""

import asyncio
import json
import logging
import os
import random
from datetime import time as dtime
from pathlib import Path
from typing import Literal, Optional

import httpx
import pytz
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
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# Fraction of the entry->TP distance that triggers the breakeven alert
BREAKEVEN_FRACTION = 0.30
# How often to check live prices for active trades (seconds)
MONITOR_INTERVAL = 60

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

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read %s, starting fresh", STATE_FILE)
    return {"chats": [], "trades": []}


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


def determine_order_type(data: TradeData) -> str:
    """Deduce the pending-order type by comparing entry to current price."""
    action = "SELL" if data.direction == "SHORT" else "BUY"
    current = data.current_price
    if current is None or current <= 0:
        return action

    # Within 0.05% of entry -> effectively a market order
    if abs(data.entry - current) / data.entry < 0.0005:
        return action

    if data.direction == "SHORT":
        # Selling above the market waits for price to rise -> LIMIT
        order = "LIMIT" if data.entry > current else "STOP"
    else:
        # Buying below the market waits for price to fall -> LIMIT
        order = "LIMIT" if data.entry < current else "STOP"
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
# Live price monitoring (Bybit public API — no key required)
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


async def resolve_bybit_symbol(asset: str) -> Optional[tuple[str, str]]:
    """Map an asset like BTCUSD to a Bybit (symbol, category), if it exists."""
    compact = asset.upper().replace("/", "").replace(" ", "").replace("-", "")
    if compact.endswith("USDT"):
        candidates = [compact]
    elif compact.endswith("USD"):
        candidates = [compact + "T"]
    else:
        candidates = [compact + "USDT"]

    async with httpx.AsyncClient(timeout=10) as http:
        for symbol in candidates:
            for category in BYBIT_CATEGORIES:
                if await fetch_bybit_price(http, symbol, category) is not None:
                    return symbol, category
    return None


def check_trade(trade: dict, price: float) -> Optional[str]:
    """Return an event for the trade at this price: 'tp', 'sl', 'breakeven', None."""
    long = trade["direction"] == "LONG"
    if (price >= trade["tp"]) if long else (price <= trade["tp"]):
        return "tp"
    if (price <= trade["sl"]) if long else (price >= trade["sl"]):
        return "sl"
    if not trade["be_alerted"]:
        if (price >= trade["be_price"]) if long else (price <= trade["be_price"]):
            return "breakeven"
    return None


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


async def monitor_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: check live prices for all active trades."""
    trades = state["trades"]
    if not trades:
        return

    async with httpx.AsyncClient(timeout=10) as http:
        for trade in list(trades):
            price = await fetch_bybit_price(
                http, trade["symbol"], trade.get("category", "linear")
            )
            if price is None:
                continue

            event = check_trade(trade, price)
            if event is None:
                continue

            decimals = trade.get("decimals", 2)
            if event == "breakeven":
                trade["be_alerted"] = True
                text = (
                    f"🔒 {trade['asset']}: price reached "
                    f"{trade['be_price']:.{decimals}f} — 30% of the way to TP.\n"
                    f"Move your STOP LOSS to BREAKEVEN ({trade['entry']:.{decimals}f}) "
                    "to make this a risk-free trade."
                )
            elif event == "tp":
                state["trades"].remove(trade)
                text = random.choice(TP_MESSAGES).format(
                    asset=trade["asset"], profit=trade["profit_pct"]
                )
            else:  # sl
                state["trades"].remove(trade)
                text = random.choice(SL_MESSAGES).format(
                    asset=trade["asset"], loss=trade["loss_pct"]
                )

            save_state()
            try:
                await context.bot.send_message(chat_id=trade["chat_id"], text=text)
            except Exception:
                logger.exception("Could not deliver alert to chat %s", trade["chat_id"])


# ---------------------------------------------------------------------------
# Morning motivation
# ---------------------------------------------------------------------------

def generate_motivation() -> str:
    """Fresh morning motivation via Gemini, with a static fallback (sync)."""
    prompt = (
        "Write a short, energetic good-morning motivation message for a day "
        "trader. 2-3 sentences, include one practical reminder about discipline "
        "or risk management, a couple of fitting emojis, no hashtags, no "
        "preamble — output the message text only."
    )
    for model in GEMINI_MODELS:
        try:
            response = gemini_client.models.generate_content(
                model=model, contents=prompt
            )
            text = (response.text or "").strip()
            if text:
                return text
        except Exception:
            continue
    return random.choice(FALLBACK_MORNING)


async def morning_motivation(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job: send a motivation text to every registered chat."""
    if not state["chats"]:
        return
    text = await asyncio.to_thread(generate_motivation)
    for chat_id in list(state["chats"]):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("Could not deliver morning message to chat %s", chat_id)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

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

        # Register the trade for live monitoring (pairs listed on Bybit)
        resolved = await resolve_bybit_symbol(data.asset)
        decimals = signal_decimals(data)
        be_price = breakeven_price(data)
        profit, loss = calculate_percentages(data)
        if resolved:
            symbol, category = resolved
            state["trades"].append({
                "chat_id": message.chat_id,
                "asset": data.asset.upper(),
                "symbol": symbol,
                "category": category,
                "direction": data.direction,
                "entry": data.entry,
                "sl": data.stop_loss,
                "tp": data.take_profit,
                "be_price": be_price,
                "be_alerted": False,
                "decimals": decimals,
                "profit_pct": profit,
                "loss_pct": loss,
            })
            save_state()
            await message.reply_text(
                f"🔔 Trade is being monitored live.\n"
                f"I'll alert you to move SL to breakeven at "
                f"{be_price:.{decimals}f} (30% of the way to TP), and again "
                f"when TP or SL is hit."
            )
        else:
            await message.reply_text(
                f"ℹ️ Live monitoring isn't available for {data.asset.upper()} "
                "(no matching Bybit pair) — breakeven/TP/SL alerts are off "
                "for this trade."
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
        "I'll also monitor the trade live (crypto pairs), alert you to move SL "
        "to breakeven at 30% of the way to TP, message you when TP/SL hits, "
        "and send a motivation text every morning. 🌅"
    )


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Chart images only — text and any other message types are ignored
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_chart))

    # Background jobs: trade monitoring + daily morning motivation
    app.job_queue.run_repeating(monitor_trades, interval=MONITOR_INTERVAL, first=10)
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.UnknownTimeZoneError:
        logger.warning("Unknown TIMEZONE %r, falling back to UTC", TIMEZONE)
        tz = pytz.utc
    app.job_queue.run_daily(
        morning_motivation,
        time=dtime(hour=MORNING_HOUR, tzinfo=tz),
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
