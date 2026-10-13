"""Test setup: an offline, isolated bot.

Environment is set BEFORE bot.py is imported, because it reads it at import
time. Every external service is blanked out — in particular the Upstash
settings, so a test can never overwrite the real stored trades even when
they are present in .env (load_dotenv never overrides a variable that is
already set, even to an empty string).
"""

import os
import sys
import tempfile
from pathlib import Path

_STATE_DIR = tempfile.mkdtemp(prefix="bot-tests-")
if os.environ.get("RUN_LIVE_VISION") != "1":
    # Offline runs never reach Groq; the opt-in live test needs the real key,
    # which then comes from the shell or .env
    os.environ["GROQ_API_KEY"] = "test-groq-key"
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:test",
    "GEMINI_API_KEY": "",
    "UPSTASH_REDIS_REST_URL": "",
    "UPSTASH_REDIS_REST_TOKEN": "",
    "ADMIN_CHAT_ID": "",
    "STATE_FILE": str(Path(_STATE_DIR) / "state.json"),
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import bot  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_state():
    """Every test starts with no chats, trades, history or offers."""
    for key in ("chats", "trades", "history"):
        bot.state[key] = []
    bot.state["offers"] = {}
    bot._last_polled.clear()
    yield


class FakeBot:
    """Stands in for telegram.Bot: records what would have been sent."""

    id = 999

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return FakeMessage(chat_id, message_id=1000 + len(self.sent), text=text)

    async def send_chat_action(self, **kwargs):
        pass


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    """Enough of telegram.Message for the handlers under test."""

    def __init__(self, chat_id=1, message_id=1, text=None, photo=None,
                 reply_to=None, from_id=None):
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.photo = photo
        self.document = None
        self.reply_to_message = reply_to
        self.from_user = FakeUser(from_id) if from_id is not None else None
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        sent = FakeMessage(self.chat_id, message_id=5000 + len(self.replies),
                           text=text, from_id=FakeBot.id)
        sent.reply_markup = reply_markup
        self.replies.append(sent)
        return sent


class FakeContext:
    def __init__(self, args=None):
        self.bot = FakeBot()
        self.args = args or []


class FakeUpdate:
    def __init__(self, message):
        self.effective_message = message
        self.edited_message = None
        self.edited_channel_post = None
        self.effective_chat = None


def make_trade(**overrides) -> dict:
    """A monitored XAUUSD long, open, with sane defaults."""
    trade = {
        "id": "t1", "chat_id": 1, "asset": "XAUUSD", "provider": "swissquote",
        "symbol": "XAU/USD", "direction": "LONG", "entry": 4166.0, "sl": 4161.5,
        "tp": 4182.0, "be_price": 4170.8, "be_alerted": False, "decimals": 2,
        "profit_pct": 0.38, "loss_pct": 0.11, "order_type": "BUY LIMIT",
        "status": "active", "fill_direction": None,
        "created_at": bot.utcnow_iso(), "feed_version": bot.FEED_VERSION,
    }
    trade.update(overrides)
    return trade
