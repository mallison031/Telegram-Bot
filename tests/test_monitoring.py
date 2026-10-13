"""What each price sample means for a trade, and the alerts it produces."""

import asyncio
from datetime import datetime, timezone

import pytest

import bot
from conftest import FakeContext, make_trade

S = bot.PriceSample


def test_pending_fills_in_the_direction_price_must_travel():
    t = make_trade(status="pending", fill_direction="down")
    assert bot.check_trade(t, S(4170, 4171, 4166.5)) is None
    assert bot.check_trade(t, S(4167, 4171, 4165.9)) == "entry"


def test_pending_that_reaches_tp_without_filling_is_missed():
    t = make_trade(status="pending", fill_direction="down")
    assert bot.check_trade(t, S(4183, 4183, 4170)) == "missed"


def test_sl_is_reported_when_one_interval_spans_sl_and_tp():
    assert bot.check_trade(make_trade(), S(4170, 4190, 4160)) == "sl"


def test_breakeven_alert_once():
    t = make_trade()
    assert bot.check_trade(t, S(4171, 4171, 4170)) == "breakeven"
    t["be_alerted"] = True
    assert bot.check_trade(t, S(4171, 4171, 4170)) is None


def test_stop_on_entry_closes_flat():
    assert bot.check_trade(make_trade(sl=4166.0), S(4165, 4167, 4165)) == "be"


def test_tp1_then_tp2():
    t = make_trade(tp2=4190.0)
    assert bot.check_trade(t, S(4182, 4182.5, 4180)) == "tp1"
    t.update(tp1_hit=True, sl=t["entry"])
    assert bot.check_trade(t, S(4185, 4186, 4184)) is None
    assert bot.check_trade(t, S(4190, 4190.5, 4188)) == "tp2"
    assert bot.check_trade(t, S(4166, 4170, 4165)) == "be"


def test_candle_mark_picks_up_a_late_published_minute():
    C, F = bot.Candle, bot.PriceFeed
    since = datetime.fromtimestamp(1010, timezone.utc)
    s1, m1 = F(100, (C(1000, 101, 99), C(1060, 102, 98))).sample_since(since, None)
    # Next poll: minute 1120 has filled in with a spike, though the clock moved on
    s2, m2 = F(100, (C(1060, 102, 98), C(1120, 130, 97))).sample_since(since, m1)
    assert (m1, s2.high, m2) == (1060, 130, 1120)


def run(coro):
    return asyncio.run(coro)


def test_short_sl_is_hit_on_the_ask(monkeypatch):
    ctx = FakeContext()
    t = make_trade(direction="SHORT", entry=4160, sl=4170.5, tp=4140, be_price=4154)
    bot.state["trades"].append(t)
    # Bid 4170.00 is below SL; the ask 4170.60 a short closes on is not
    run(bot.apply_feed(ctx, t, bot.PriceFeed(4170.0, ask=4170.6)))
    assert t not in bot.state["trades"]
    assert ctx.bot.sent[-1][1].startswith("🛑")  # one of the SL messages
    assert bot.state["history"][-1]["result"] == "sl"


def test_pending_buy_fills_on_the_ask_and_names_the_order():
    ctx = FakeContext()
    t = make_trade(status="pending", fill_direction="up", entry=4176,
                   sl=4168, tp=4190, order_type="BUY STOP")
    bot.state["trades"].append(t)
    run(bot.apply_feed(ctx, t, bot.PriceFeed(4175.8, ask=4176.1)))
    assert t["status"] == "active"
    assert "BUY STOP triggered" in ctx.bot.sent[-1][1]


def test_unknown_side_is_settled_with_limit_or_stop():
    t = make_trade(status="pending", fill_direction=None, order_type="SELL",
                   direction="SHORT", entry=4165, sl=4172, tp=4150)
    bot.state["trades"].append(t)
    run(bot.apply_feed(FakeContext(), t, bot.PriceFeed(4170.6, ask=4171.0)))
    assert (t["fill_direction"], t["order_type"]) == ("down", "SELL STOP")


def test_tp1_moves_stop_to_entry_and_tp2_records_half_and_half():
    ctx = FakeContext()
    t = make_trade(tp2=4190.0)
    bot.state["trades"].append(t)
    run(bot.apply_feed(ctx, t, bot.PriceFeed(4182.2, ask=4182.5)))
    assert t["tp1_hit"] and t["sl"] == t["entry"]
    assert "TP1 hit" in ctx.bot.sent[-1][1]
    run(bot.apply_feed(ctx, t, bot.PriceFeed(4190.1, ask=4190.4)))
    record = bot.state["history"][-1]
    # Half at TP1 (+0.38%), half at TP2 (+0.58%)
    assert record["result"] == "tp2" and record["pnl_pct"] == pytest.approx(0.48, abs=0.01)


def test_monitor_fetches_once_per_market_and_skips_cancelled(monkeypatch):
    calls = []

    async def fake_feed(http, provider, symbol, minutes):
        calls.append((provider, symbol))
        await asyncio.sleep(0.01)
        return bot.PriceFeed(4170.0, ask=4170.3)

    monkeypatch.setattr(bot, "fetch_feed", fake_feed)
    trades = [make_trade(id=str(i), entry=4166, sl=4100, tp=4300, be_price=4200)
              for i in range(10)]
    trades.append(make_trade(id="btc", provider="coinbase", symbol="BTC-USD",
                             entry=80000, sl=70000, tp=90000, be_price=83000))
    bot.state["trades"].extend(trades)
    run(bot.monitor_trades(FakeContext()))
    assert sorted(calls) == [("coinbase", "BTC-USD"), ("swissquote", "XAU/USD")]


def test_old_feed_routing_is_migrated(monkeypatch):
    async def resolve(asset):
        return {"provider": "swissquote", "symbol": "XAU/USD"} if asset == "XAUUSD" else None
    monkeypatch.setattr(bot, "resolve_market", resolve)
    ctx = FakeContext()
    gold = make_trade(provider="goldapi", symbol="XAU", feed_version=1, candle_mark=1.0)
    index = make_trade(asset="US30", provider="yahoo", symbol="^DJI", feed_version=1)
    bot.state["trades"] += [gold, index]
    now = datetime.now(timezone.utc)
    run(bot.tidy_trade(ctx, gold, now))
    run(bot.tidy_trade(ctx, index, now))
    assert (gold["provider"], gold["symbol"], "candle_mark" in gold) == ("swissquote", "XAU/USD", False)
    assert index not in bot.state["trades"]
    assert "Stopped monitoring US30" in ctx.bot.sent[-1][1]
