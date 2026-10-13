"""Market vs pending, and LIMIT vs STOP."""

import pytest

import bot


@pytest.mark.parametrize("direction, fill, expected", [
    ("LONG", "down", "BUY LIMIT"),
    ("LONG", "up", "BUY STOP"),
    ("SHORT", "up", "SELL LIMIT"),
    ("SHORT", "down", "SELL STOP"),
    ("LONG", None, "BUY MARKET"),
    ("SHORT", None, "SELL MARKET"),
    ("LONG", "unknown", "BUY"),
])
def test_order_type_for(direction, fill, expected):
    assert bot.order_type_for(direction, fill) == expected


def setup(**k):
    base = dict(asset="XAUUSD", direction="LONG", entry=4170.0,
                stop_loss=4165.0, take_profit=4185.0)
    return bot.TradeData(**{**base, **k})


def test_chart_placement_beats_a_moved_live_price():
    # The bug this guards: a market buy screenshotted at entry, price $2.50
    # higher by the time it arrives, used to become a BUY LIMIT.
    assert bot.determine_order_type(setup(entry_placement="at"), 4172.5) == "BUY MARKET"


def test_chart_price_label_within_tolerance_is_market():
    assert bot.determine_order_type(setup(current_price=4170.8), 4175.0) == "BUY MARKET"


def test_placement_decides_side_even_if_live_price_passed_entry():
    assert bot.determine_order_type(setup(entry_placement="below"), 4169.0) == "BUY LIMIT"


def test_live_price_only_when_chart_gives_nothing():
    assert bot.determine_order_type(setup(), 4170.1) == "BUY MARKET"
    assert bot.determine_order_type(setup(), 4180.0) == "BUY LIMIT"
    assert bot.determine_order_type(setup()) == "BUY"


def test_percentages_and_validation():
    short = bot.TradeData(asset="BTCUSD", direction="SHORT", entry=63552.40,
                          stop_loss=63717.50, take_profit=60361.53)
    assert bot.calculate_percentages(short) == (5.02, 0.26)
    assert bot.validate(short) is None
    flipped = short.model_copy(update={"direction": "LONG"})
    assert "LONG" in bot.validate(flipped)
