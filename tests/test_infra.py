"""Saving to Upstash, the backup chart reader, and error alerts."""

import asyncio
import json
import logging
import threading

import httpx
import pytest

import bot
from conftest import FakeContext, make_trade


class FakeUpstash:
    """An in-memory Redis speaking the few commands the bot uses, shared by
    any number of RemoteState objects — each one a running copy of the bot."""

    def __init__(self):
        self.data: dict = {}
        self.read_only = False
        self.down = False
        self.writes = 0

    def run(self, command):
        if self.down:
            raise httpx.ConnectError("down")
        op, key, *args = command
        writing = op in ("SET", "HSET", "HDEL", "RPUSH", "LTRIM", "DEL")
        if writing and self.read_only:
            raise httpx.HTTPStatusError(
                "NOPERM", request=httpx.Request("POST", "https://fake"),
                response=httpx.Response(403))
        if writing and not key.endswith(":write-check") and ":claim:" not in key:
            self.writes += 1
        if op == "GET":
            return self.data.get(key)
        if op == "SET":
            if "NX" in args and key in self.data:
                return None
            self.data[key] = args[0]
            return "OK"
        if op == "DEL":
            return int(self.data.pop(key, None) is not None)
        if op == "HSET":
            self.data.setdefault(key, {})[args[0]] = args[1]
            return 1
        if op == "HDEL":
            return int(self.data.get(key, {}).pop(args[0], None) is not None)
        if op == "HGETALL":
            return [x for kv in self.data.get(key, {}).items() for x in kv]
        if op == "RPUSH":
            self.data.setdefault(key, []).extend(args)
            return len(self.data[key])
        if op == "LTRIM":
            self.data[key] = self.data.get(key, [])[int(args[0]):]
            return "OK"
        if op == "LRANGE":
            return list(self.data.get(key, []))
        raise AssertionError(f"unexpected command {op}")


@pytest.fixture
def upstash(monkeypatch):
    fake = FakeUpstash()
    monkeypatch.setattr(bot, "upstash", fake.run)
    monkeypatch.setattr(bot, "upstash_pipeline", lambda cmds: [fake.run(c) for c in cmds])
    monkeypatch.setattr(bot, "UPSTASH_URL", "https://fake")
    monkeypatch.setattr(bot, "UPSTASH_TOKEN", "t")
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    return fake


def copy_of_the_bot():
    """A RemoteState plus the state it loaded, like a freshly started bot."""
    rs = bot.RemoteState()
    data = rs.load() or {}
    data.setdefault("trades", [])
    data.setdefault("history", [])
    return rs, data


def flush(rs, data):
    """Save synchronously (the real save runs on a background thread)."""
    commands = bot.changes_between(rs._synced, bot.snapshot(data))
    if commands:
        bot.upstash_pipeline(commands)
        rs._record_sent(commands)


def test_trades_and_results_survive_a_restart(upstash):
    rs, data = copy_of_the_bot()
    data["trades"].append(make_trade(id="a"))
    data["history"].append({"id": "r1", "chat_id": 1, "asset": "XAUUSD", "pnl_pct": 0.4})
    flush(rs, data)
    _, reloaded = copy_of_the_bot()
    assert [t["id"] for t in reloaded["trades"]] == ["a"]
    assert reloaded["history"][0]["id"] == "r1"


def test_background_saver_writes_only_changes(upstash):
    rs, data = copy_of_the_bot()
    data["trades"].append(make_trade(id="a"))
    rs.save(data)
    pause = threading.Event()  # time.sleep is stubbed out by the fixture
    for _ in range(200):  # the saver thread runs on its own
        if bot.TRADES_KEY in upstash.data:
            break
        pause.wait(0.01)
    assert list(upstash.data[bot.TRADES_KEY]) == ["a"]


def test_per_poll_bookkeeping_costs_no_write(upstash):
    rs, data = copy_of_the_bot()
    data["trades"].append(make_trade(id="a"))
    flush(rs, data)
    before = upstash.writes
    data["trades"][0]["candle_mark"] = 123.0
    flush(rs, data)
    assert upstash.writes == before


def test_overlapping_copies_never_erase_each_others_trades(upstash):
    """Regression: the state was one blob, so during a deploy the last copy
    to save erased every trade only the other copy knew about."""
    old, old_data = copy_of_the_bot()
    new, new_data = copy_of_the_bot()
    old_data["trades"].append(make_trade(id="sent-to-old"))
    flush(old, old_data)
    new_data["trades"].append(make_trade(id="sent-to-new"))
    flush(new, new_data)
    stored = sorted(upstash.data[bot.TRADES_KEY])
    assert stored == ["sent-to-new", "sent-to-old"]

    # The new copy picks up the old copy's trade...
    new.reconcile(new_data, upstash.run(["HGETALL", bot.TRADES_KEY]))
    assert sorted(t["id"] for t in new_data["trades"]) == stored
    # ...and its changes; and when the old copy closes it, so does the new
    old_data["trades"][0]["status"] = "pending"
    flush(old, old_data)
    new.reconcile(new_data, upstash.run(["HGETALL", bot.TRADES_KEY]))
    assert next(t for t in new_data["trades"] if t["id"] == "sent-to-old")["status"] == "pending"
    old_data["trades"].clear()
    flush(old, old_data)
    new.reconcile(new_data, upstash.run(["HGETALL", bot.TRADES_KEY]))
    assert [t["id"] for t in new_data["trades"]] == ["sent-to-new"]


def test_reconcile_keeps_this_copys_unsent_changes(upstash):
    rs, data = copy_of_the_bot()
    data["trades"].append(make_trade(id="a"))
    flush(rs, data)
    data["trades"][0]["sl"] = 4163.0       # edited here, not yet saved
    rs._latest = bot.snapshot(data)
    assert rs.reconcile(data, upstash.run(["HGETALL", bot.TRADES_KEY])) == 0
    assert data["trades"][0]["sl"] == 4163.0


def test_only_one_copy_claims_an_alert(upstash):
    a, _ = copy_of_the_bot()
    b, _ = copy_of_the_bot()
    assert a.claim("t1:tp") is True
    assert b.claim("t1:tp") is False
    assert b.claim("t1:sl") is True


def test_the_losing_copy_follows_the_event_silently(upstash, monkeypatch):
    rs, _ = copy_of_the_bot()
    monkeypatch.setattr(bot, "remote_state", rs)
    rs.claim("t1:tp")  # the other copy got there first
    t = make_trade(id="t1")
    bot.state["trades"].append(t)
    ctx = FakeContext()
    asyncio.run(bot.apply_feed(ctx, t, bot.PriceFeed(4182.5, ask=4182.8)))
    assert t not in bot.state["trades"]
    assert ctx.bot.sent == [] and bot.state["history"] == []


def test_old_single_blob_state_is_moved_over(upstash):
    upstash.data[bot.STATE_KEY] = json.dumps({
        "chats": [1], "trades": [make_trade(id="legacy")], "history": []})
    _, data = copy_of_the_bot()
    assert [t["id"] for t in data["trades"]] == ["legacy"]
    assert bot.STATE_KEY not in upstash.data
    assert list(upstash.data[bot.TRADES_KEY]) == ["legacy"]


def test_read_only_token_is_caught_at_startup(upstash):
    """Regression: Upstash's read-only token reads fine, so the bot reported
    "loaded from Upstash" while every save was refused and trades were lost
    on the next restart."""
    upstash.read_only = True
    rs = bot.RemoteState()
    rs.load()
    assert not rs.ready and "READ-ONLY" in rs.problem
    assert rs.claim("x") is True  # without saving, this is the only copy


def test_unreadable_upstash_means_no_writes(upstash):
    upstash.down = True
    rs = bot.RemoteState()
    assert rs.load() is None and not rs.ready
    rs.save({"trades": [make_trade()]})
    assert rs._latest is None


def test_start_notice_reports_saving_problems(monkeypatch):
    monkeypatch.setattr(bot, "ADMIN_CHAT_ID", "42")
    rs = bot.RemoteState.__new__(bot.RemoteState)
    rs.enabled, rs.ready, rs.problem = True, False, "the Upstash token is READ-ONLY"
    monkeypatch.setattr(bot, "remote_state", rs)
    ctx = FakeContext()
    asyncio.run(bot.announce_start(ctx))
    assert "⚠️ the Upstash token is READ-ONLY" in ctx.bot.sent[-1][1]
    rs.enabled = False
    asyncio.run(bot.announce_start(ctx))
    assert "NOT saved" in ctx.bot.sent[-1][1]


def test_retry_after_parsing():
    r = httpx.Response(429)
    assert bot.retry_after_seconds(r, "Please try again in 720ms.") == 0.72
    assert bot.retry_after_seconds(r, "Please try again in 5.42s.") == 5.42
    assert bot.retry_after_seconds(httpx.Response(429, headers={"retry-after": "3"}), "") == 3.0


def test_backup_reader_answers_when_groq_is_full(monkeypatch):
    answer = bot.ChartAnalysis(is_trading_chart=True, asset="XAUUSD")

    def groq_full(*a):
        raise bot.VisionAPIError("HTTP 429: full", True, retry_after=20)

    monkeypatch.setattr(bot, "VISION_MODELS", [("groq", "q"), ("gemini", "bad"), ("gemini", "g")])
    monkeypatch.setattr(bot, "request_chart_analysis", groq_full)
    tried = []

    def gemini(http, model, *a):
        tried.append(model)
        if model == "bad":
            raise bot.VisionAPIError("HTTP 404: no such model", False)
        return answer

    monkeypatch.setattr(bot, "request_gemini_analysis", gemini)
    assert bot.extract_chart_analysis(b"img", "image/png") is answer
    assert tried == ["bad", "g"]


def test_all_bad_keys_fail_fast(monkeypatch):
    def bad(*a):
        raise bot.VisionAPIError("HTTP 401: bad key", False)

    monkeypatch.setattr(bot, "VISION_MODELS", [("groq", "q"), ("gemini", "g")])
    monkeypatch.setattr(bot, "request_chart_analysis", bad)
    monkeypatch.setattr(bot, "request_gemini_analysis", bad)
    with pytest.raises(bot.VisionAPIError):
        bot.extract_chart_analysis(b"img", "image/png")


def test_unusable_model_reply_is_retryable():
    with pytest.raises(bot.VisionAPIError) as caught:
        bot.parse_analysis('{"asset": 5}')
    assert caught.value.retryable


def test_admin_alerts_are_batched_and_deduplicated(monkeypatch):
    monkeypatch.setattr(bot, "ADMIN_CHAT_ID", "42")
    monkeypatch.setattr(bot, "_alerted_at", {})
    monkeypatch.setattr(bot, "_suppressed", {})
    log = logging.getLogger("bot-test")
    log.addHandler(bot.admin_alerts)
    try:
        for _ in range(3):
            log.error("Feed down")
        log.warning("only a warning")
        ctx = FakeContext()
        asyncio.run(bot.flush_admin_alerts(ctx))
        assert len(ctx.bot.sent) == 1 and ctx.bot.sent[0][1].count("Feed down") == 1
        log.error("Feed down")
        asyncio.run(bot.flush_admin_alerts(ctx))
        assert len(ctx.bot.sent) == 1  # repeat within the quiet period
    finally:
        log.removeHandler(bot.admin_alerts)


def test_tests_never_see_the_real_upstash():
    """conftest must blank the Upstash keys even when .env has the real ones."""
    assert bot.UPSTASH_URL == "" and bot.UPSTASH_TOKEN == ""
    assert not bot.remote_state.enabled
