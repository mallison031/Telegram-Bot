"""Chart symbols → feeds, and which news moves which market."""

from datetime import datetime, timedelta, timezone

import pytest

import bot


@pytest.mark.parametrize("asset, expected", [
    ("XAUUSDm", "XAUUSD"), ("EURUSDc", "EURUSD"), ("US30m", "US30"),
    ("NAS100.cash", "NAS100"), ("BTCUSDm", "BTCUSD"),
    ("ETC", "ETC"), ("ETCUSD", "ETCUSD"),  # tickers that merely end in C/M survive
])
def test_canonical_asset(asset, expected):
    assert bot.canonical_asset(asset) == expected


def test_indices_and_oil_are_refused_with_a_reason():
    for asset in ("US30", "US30m", "NAS100.cash", "USOIL", "WTI", "GER40"):
        assert bot.unmonitored_reason(asset)
    assert bot.unmonitored_reason("XAUUSD") is None


def test_feed_symbol_candidates():
    assert bot.swissquote_symbol_candidates("XAUUSDm") == ["XAU/USD"]
    assert bot.swissquote_symbol_candidates("GOLD") == ["XAU/USD"]
    assert bot.swissquote_symbol_candidates("BTC") == []
    assert bot.coinbase_symbol_candidates("ETHUSDT")[0] == "ETH-USDT"
    assert bot.coinbase_symbol_candidates("BTCUSD")[0] == "BTC-USD"


@pytest.mark.parametrize("asset, currencies", [
    ("XAUUSD", {"USD"}), ("EURJPY", {"EUR", "JPY"}), ("BTCUSD", {"USD"}),
    ("ETHUSDT", {"USD"}), ("US30", {"USD"}), ("GER40", {"EUR"}), ("WTI", {"USD"}),
])
def test_news_currencies(asset, currencies):
    assert bot.news_currencies(asset) == currencies


def test_upcoming_news_window_and_note():
    now = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    ev = lambda country, hours, title: {
        "country": country, "title": title, "impact": "High",
        "when": now + timedelta(hours=hours)}
    events = [ev("USD", 2.5, "CPI m/m"), ev("USD", 5, "Too late"),
              ev("EUR", 1, "ECB rate"), ev("USD", -1, "Already out"),
              ev("All", 3, "G20")]
    gold = bot.upcoming_news(events, "XAUUSD", now)
    assert [e["title"] for e in gold] == ["CPI m/m", "G20"]
    note = bot.news_note(gold, now)
    assert "USD CPI m/m at 13:30 (in 2h 30m)" in note  # 12:30 UTC = 13:30 Lagos
    assert bot.news_note([], now) is None
