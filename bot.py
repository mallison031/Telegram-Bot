"""Automated Trading Chart Vision Bot.

Receives a chart screenshot on Telegram, extracts trade parameters with
Gemini 2.5 Flash vision, calculates profit/loss percentages in code
(never trusting the AI with math), and replies with a formatted signal:

    [ASSET] [ACTION] [ORDER_TYPE]
    ENTRY: [VALUE]
    SL: [VALUE]
    TP: [VALUE]
    Profit: +[X]% / Loss: -[Y]%
"""

import asyncio
import logging
import os
from typing import Literal, Optional

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
# python-telegram-bot's httpx logs every poll request at INFO; silence them
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


class TradeData(BaseModel):
    """Raw values Gemini extracts from the chart. All math happens in code."""

    asset: str
    direction: Literal["LONG", "SHORT"]
    entry: float
    stop_loss: float
    take_profit: float
    current_price: Optional[float] = None


VISION_PROMPT = """\
You are analyzing a screenshot of a trading chart (e.g. TradingView) that has a \
long/short position tool drawn on it.

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


def build_signal_message(data: TradeData) -> str:
    profit, loss = calculate_percentages(data)
    prices = (data.entry, data.stop_loss, data.take_profit)
    # All prices in one signal share the same precision (e.g. 1.08345 / 1.07900)
    decimals = max(2, *(_natural_decimals(p) for p in prices))
    entry, sl, tp = (f"{p:.{decimals}f}" for p in prices)
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


def extract_trade_data(image_bytes: bytes, mime_type: str) -> TradeData:
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
                    response_schema=TradeData,
                    temperature=0,
                ),
            )
        except Exception as error:  # noqa: BLE001 - fall through to next model
            logger.warning("Model %s failed: %s", model, error)
            last_error = error
            continue
        parsed = response.parsed
        if isinstance(parsed, TradeData):
            return parsed
        return TradeData.model_validate_json(response.text)
    raise last_error if last_error else RuntimeError("No Gemini model available")


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

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    status = await message.reply_text("🔍 Analyzing chart...")

    try:
        image_bytes = bytes(await file.download_as_bytearray())
        data = await asyncio.to_thread(extract_trade_data, image_bytes, mime_type)

        error = validate(data)
        if error:
            await status.edit_text(
                f"⚠️ Couldn't read a valid setup from this chart.\n{error}\n"
                "Make sure the position tool with entry/SL/TP is clearly visible."
            )
            return

        await status.edit_text(build_signal_message(data))
    except Exception:
        logger.exception("Failed to process chart")
        await status.edit_text(
            "❌ Sorry, I couldn't process that image. "
            "Please send a clear screenshot of a chart with a position tool drawn on it."
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 Send me a screenshot of a trading chart with a long/short position "
        "tool drawn on it (entry, stop loss, take profit) and I'll reply with a "
        "formatted signal including profit/loss percentages."
    )


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Please send a chart screenshot (as a photo or image file). Use /start for help."
    )


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_chart))
    app.add_handler(MessageHandler(~filters.COMMAND, handle_other))

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
