"""Chart reading against the real vision model — opt-in.

Guards the regression where the model called every chart a SHORT and read
the current-price line as the entry. Needs a real GROQ_API_KEY and takes
~4 minutes because of the free tier's rate limit:

    RUN_LIVE_VISION=1 pytest tests/test_live_vision.py   (key from .env)
"""

import os
import time

import pytest

LIVE = os.environ.get("RUN_LIVE_VISION") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_VISION=1 to run")

ORDERS = [
    ("BUY LIMIT", 4166.00, 4161.50, 4182.00, "dark"),
    ("BUY STOP", 4176.00, 4168.00, 4190.00, "light"),
    ("SELL LIMIT", 4175.00, 4180.50, 4160.00, "dark"),
    ("SELL STOP", 4165.00, 4172.00, 4150.00, "light"),
    ("BUY MARKET", 4170.60, 4165.00, 4185.00, "dark"),
    ("SELL MARKET", 4170.60, 4176.00, 4158.00, "light"),
]


@pytest.fixture(scope="module")
def live_bot():
    import bot
    return bot


@pytest.mark.parametrize("order, entry, sl, tp, theme", ORDERS)
def test_reads_each_order_type(live_bot, tmp_path, order, entry, sl, tp, theme):
    from charts import draw_chart
    path = tmp_path / "chart.png"
    draw_chart(path, entry, sl, tp, theme)
    analysis = live_bot.extract_chart_analysis(path.read_bytes(), "image/png")
    data = live_bot.to_trade_data(analysis)
    assert data is not None and live_bot.validate(data) is None
    assert (data.entry, data.stop_loss, data.take_profit) == (entry, sl, tp)
    assert live_bot.determine_order_type(data) == order
    time.sleep(30)  # stay inside the free tier's per-minute token budget
