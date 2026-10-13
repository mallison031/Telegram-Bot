"""Reply-to-edit, and results tracking."""

import asyncio

import pytest

import bot
from conftest import FakeContext, FakeMessage, FakeUpdate, make_trade


@pytest.mark.parametrize("text, expected", [
    ("sl 4170", {"sl": 4170.0}),
    ("SL: 4,170.5", {"sl": 4170.5}),
    ("be", {"sl": "be"}),
    ("sl be", {"sl": "be"}),
    ("tp 4195 tp2 4210", {"tp": 4195.0, "tp2": 4210.0}),
    ("tp1 4195", {"tp": 4195.0}),
    ("entry 4166", {"entry": 4166.0}),
    ("cancel", {"cancel": True}),
    ("thanks bot", None),
    ("tp be", None),
])
def test_parse_edits(text, expected):
    assert bot.parse_edits(text) == expected


def test_apply_edits_recomputes_levels():
    t = make_trade()
    msg = bot.apply_edits(t, {"tp": 4190.0, "tp2": 4200.0})
    assert "TP1 → 4190.00" in msg and "TP2 → 4200.00" in msg
    assert t["be_price"] == pytest.approx(4166 + (4190 - 4166) * 0.3)
    assert t["profit_pct"] == round((4190 - 4166) / 4166 * 100, 2)


def test_breakeven_edit():
    t = make_trade()
    assert "breakeven" in bot.apply_edits(t, {"sl": "be"})
    assert (t["sl"], t["be_alerted"], t["loss_pct"]) == (t["entry"], True, 0.0)


@pytest.mark.parametrize("edits", [
    {"sl": 4170.0},          # SL above a long's entry
    {"tp": 4160.0},          # TP below a long's entry
    {"tp2": 4180.0},         # TP2 not beyond TP1
    {"sl": -1.0},
])
def test_incoherent_edits_change_nothing(edits):
    t = make_trade()
    before = dict(t)
    with pytest.raises(ValueError):
        bot.apply_edits(t, edits)
    assert t == before


def run(coro):
    return asyncio.run(coro)


def reply_to(trade_message_id, text, chat_id=1):
    target = FakeMessage(chat_id, message_id=trade_message_id, from_id=999)
    return FakeUpdate(FakeMessage(chat_id, message_id=77, text=text, reply_to=target))


def test_reply_edits_the_trade_it_answers():
    t = make_trade(msg_ids=[42])
    bot.state["trades"].append(t)
    update = reply_to(42, "sl 4163")
    run(bot.edit_trade(update, FakeContext()))
    assert t["sl"] == 4163.0
    assert "SL → 4163.00" in update.effective_message.replies[-1].text


def test_reply_cancel_and_unknown_and_help():
    t = make_trade(msg_ids=[42])
    bot.state["trades"].append(t)
    help_update = reply_to(42, "what now?")
    run(bot.edit_trade(help_update, FakeContext()))
    assert "reply to any of my messages" in help_update.effective_message.replies[-1].text
    run(bot.edit_trade(reply_to(42, "cancel"), FakeContext()))
    assert t not in bot.state["trades"]
    assert bot.state["history"][-1]["result"] == "cancelled"
    gone = reply_to(42, "sl 4163")
    run(bot.edit_trade(gone, FakeContext()))
    assert "isn't being monitored" in gone.effective_message.replies[-1].text


def test_reply_to_someone_else_is_ignored():
    target = FakeMessage(1, message_id=42, from_id=123)  # not the bot
    update = FakeUpdate(FakeMessage(1, text="sl 4163", reply_to=target))
    run(bot.edit_trade(update, FakeContext()))
    assert update.effective_message.replies == []


def test_record_result_and_summary():
    win = make_trade(asset="XAUUSD")
    bot.record_result(win, "tp", win["tp"])
    loss = make_trade(asset="EURUSD", entry=1.10, sl=1.09, tp=1.12)
    bot.record_result(loss, "sl", loss["sl"])
    flat = make_trade()
    bot.record_result(flat, "be", flat["entry"])
    bot.record_result(make_trade(), "missed")
    text = bot.summarize_results(bot.results_since(1, None), "All time")
    assert "3 closed · ✅ 1 won · ❌ 1 lost · 🔒 1 breakeven" in text
    assert "Win rate: 50%" in text
    assert "Best: XAUUSD +0.38% (2) · Worst: EURUSD -0.91% (1)"  # the BE trade is gold too in text
    assert "Not taken: 1 missed" in text


def test_stats_command():
    msg = FakeMessage(1)
    run(bot.show_stats(FakeUpdate(msg), FakeContext(["all"])))
    assert "No trades closed yet" in msg.replies[-1].text
    run(bot.show_stats(FakeUpdate(msg), FakeContext(["soon"])))
    assert "/stats 7" in msg.replies[-1].text
