"""Saving to Upstash, the backup chart reader, and error alerts."""

import asyncio
import json
import logging
import threading

import httpx
import pytest

import bot
from conftest import FakeContext, make_trade


def test_durable_snapshot_leaves_out_per_poll_bookkeeping():
    data = {"chats": [1], "trades": [make_trade(candle_mark=123.0)], "history": []}
    snap = json.loads(bot.durable_snapshot(data))
    assert "candle_mark" not in snap["trades"][0]
    assert "candle_mark" in data["trades"][0]  # the live state is untouched


@pytest.fixture
def remote(monkeypatch):
    """A RemoteState wired to an in-memory fake Upstash."""
    store, writes, fail = {}, [], {"read": False, "write": None}
    done = threading.Event()

    def fake_upstash(command):
        if command[0] == "GET":
            if fail["read"]:
                raise httpx.ConnectError("down")
            return store.get(command[1])
        if fail["write"]:
            raise httpx.HTTPStatusError(
                "refused", request=httpx.Request("POST", "https://fake"),
                response=httpx.Response(fail["write"]))
        if command[1].endswith(":write-check"):
            return "OK"  # the start-up permission probe, not a state save
        store[command[1]] = command[2]
        writes.append(command[2])
        done.set()
        return "OK"

    monkeypatch.setattr(bot, "upstash", fake_upstash)
    monkeypatch.setattr(bot, "UPSTASH_URL", "https://fake")
    monkeypatch.setattr(bot, "UPSTASH_TOKEN", "t")
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    return store, writes, fail, done


def test_remote_state_round_trip(remote):
    store, writes, fail, done = remote
    store[bot.STATE_KEY] = json.dumps({"trades": [make_trade()]})
    rs = bot.RemoteState()
    assert rs.load()["trades"][0]["asset"] == "XAUUSD" and rs.ready
    rs.save('{"trades": []}')
    assert done.wait(2) and writes == ['{"trades": []}']


def test_remote_state_never_writes_after_a_failed_read(remote):
    store, writes, fail, done = remote
    fail["read"] = True
    rs = bot.RemoteState()
    assert rs.load() is None and not rs.ready
    rs.save('{"trades": []}')
    assert not done.wait(0.3) and writes == []


def test_read_only_token_is_caught_at_startup(remote):
    """Regression: Upstash's read-only token reads fine, so the bot reported
    "loaded from Upstash" while every save was refused and trades were lost
    on the next restart."""
    store, writes, fail, done = remote
    fail["write"] = 403
    rs = bot.RemoteState()
    rs.load()
    assert not rs.ready and "READ-ONLY" in rs.problem
    rs.save('{"trades": []}')
    assert not done.wait(0.3) and writes == []


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
