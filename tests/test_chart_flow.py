"""A chart arriving, end to end, with the vision model and feeds faked."""

import asyncio

import pytest

import bot
from conftest import FakeContext, FakeMessage, FakeUpdate, make_trade


class Photo:
    async def get_file(self):
        return self

    async def download_as_bytearray(self):
        return bytearray(b"img")


def chart(**k):
    base = dict(is_trading_chart=True, asset="XAUUSDm", direction="LONG",
                entry=4166.0, stop_loss=4161.5, take_profit=4182.0,
                current_price=4170.6, entry_placement="below", position_count=1)
    return bot.ChartAnalysis(**{**base, **k})


@pytest.fixture
def send(monkeypatch):
    """Send a chart through handle_chart; returns the replies' texts."""
    def _send(analysis, market=("swissquote", "XAU/USD"), news=None):
        monkeypatch.setattr(bot, "extract_chart_analysis", lambda b, m: analysis)

        async def resolve(asset):
            return dict(zip(("provider", "symbol"), market)) if market else None

        async def price(http, m):
            return 4170.6

        async def no_news(asset):
            return news

        monkeypatch.setattr(bot, "resolve_market", resolve)
        monkeypatch.setattr(bot, "fetch_market_price", price)
        monkeypatch.setattr(bot, "news_warning", no_news)
        message = FakeMessage(1, photo=[Photo()])
        asyncio.run(bot.handle_chart(FakeUpdate(message), FakeContext()))
        return message.replies
    return _send


def test_monitored_chart_gets_signal_and_named_status(send):
    replies = send(chart())
    assert replies[0].text.startswith("XAUUSDM BUY LIMIT")
    assert replies[1].text.startswith("⏳ BUY LIMIT — pending order")
    assert replies[1].reply_markup is not None  # the correction button
    trade = bot.state["trades"][0]
    # Both messages can be replied to, to edit the trade
    assert trade["msg_ids"] == [replies[0].message_id, replies[1].message_id]


def test_unmonitored_chart_gets_only_the_signal(send):
    replies = send(chart(asset="WTI", entry=92.5, stop_loss=91.6, take_profit=95.0,
                         current_price=93.0), market=None)
    assert [r.text.split("\n")[0] for r in replies] == ["WTI BUY LIMIT"]
    assert bot.state["trades"] == []


def test_unmonitored_chart_still_gets_a_news_warning(send):
    replies = send(chart(asset="US30"), market=None, news="📰 High-impact news…")
    assert len(replies) == 2 and replies[1].text.startswith("📰")


def test_news_rides_on_the_status_message(send):
    replies = send(chart(), news="📰 High-impact news within 4h")
    assert len(replies) == 2 and "📰 High-impact news" in replies[1].text


def test_running_trade_offer_and_watch_button(send):
    running = bot.ActivePosition(direction="SHORT", entry=4180.0,
                                 stop_loss=4186.0, take_profit=4165.0)
    replies = send(chart(position_count=2, active_position=running))
    note = next(r for r in replies if r.text.startswith("⚡"))
    assert "tap below" in note.text
    data = note.reply_markup.inline_keyboard[0][0].callback_data
    assert data.startswith("run:")

    class Query:
        def __init__(self):
            self.data, self.message, self.answers = data, FakeMessage(1), []

        async def answer(self, text=None):
            self.answers.append(text)

        async def edit_message_reply_markup(self, reply_markup=None):
            pass

    class Update:
        callback_query = Query()

    ctx = FakeContext()
    asyncio.run(bot.watch_running_trade(Update(), ctx))
    watched = bot.state["trades"][-1]
    assert (watched["direction"], watched["entry"], watched["status"]) == ("SHORT", 4180.0, "active")
    assert "SELL (running)" in ctx.bot.sent[-1][1]
    asyncio.run(bot.watch_running_trade(Update(), ctx))  # tapped twice
    assert Update.callback_query.answers[-1].startswith("That offer has expired")


def test_incomplete_running_trade_is_not_offered(send):
    running = bot.ActivePosition(direction="SHORT", entry=4180.0)
    replies = send(chart(position_count=2, active_position=running))
    note = next(r for r in replies if r.text.startswith("⚡"))
    assert note.reply_markup is None and "own chart" in note.text


def test_trades_shows_live_progress(monkeypatch):
    bot.state["trades"].append(make_trade(entry=4166, sl=4161.5, tp=4182))
    bot.state["trades"].append(make_trade(id="p", status="pending", entry=4160))

    async def prices(trades):
        return {("swissquote", "XAU/USD"): 4174.0}

    monkeypatch.setattr(bot, "current_prices", prices)
    message = FakeMessage(1)
    asyncio.run(bot.list_trades(FakeUpdate(message), FakeContext()))
    text = message.replies[-1].text
    assert "now 4174.00 · +0.19% · 📈 50% of the way to TP" in text
    assert "now 4174.00 · entry 0.34% away" in text
