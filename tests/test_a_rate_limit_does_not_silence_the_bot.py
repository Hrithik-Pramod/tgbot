"""
Telegram's rate limit must slow the bot down, never shut it up.

WHAT HAPPENED — 3 November 2026

    Hi Peter / Bot has stopped / Please can you check
    — Bridge, 21:57

    When you are out need back up asap please bro — 22:08
    Please let me know asap please bro — 22:49

It had not stopped. The container had been up since 26 October with zero
restarts, it wrote 133 audit entries that day, and it recorded a payment at
23:15 — while he was asking why it was dead.

    TelegramRetryAfter: Flood control exceeded on method 'SendMessage'
    in chat -100…  Retry in 8 seconds.
    ERROR aiogram.event: Cause exception while process update
    ERROR bot.notifier: failed to notify bridge channel

Telegram accepts a burst and then refuses, naming the number of seconds to
wait. Nothing in the bot waited. The refusal surfaced as an exception, was
never caught, and killed the handler where it stood — after the money was
written.

THE RULE WE GAVE THEM IS WHAT MADE IT LOOK FATAL

    no acknowledgement means it was not picked up

A bot that banks every rupee and cannot send a thumbs up is, from the desk,
a bot that has stopped — and a worse failure than stopping, because it is
silently succeeding. Nobody re-sends, and nobody trusts the ledger.

_acknowledge made it certain: react, and if that fails, reply. Both are
sends. Under flood control both were refused and the fallback raised.

WHAT IS TESTED HERE

That a refusal is waited out and the call retried, that the wait applies to
everything else queued for that chat rather than just the one call, that one
chat's limit cannot stall another's, and that a send which fails anyway
never again takes the handler down with it.
"""

import asyncio
import inspect
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiogram.exceptions import TelegramRetryAfter  # noqa: E402

from bot import client_bot, sending  # noqa: E402
from bot.sending import PacedSender  # noqa: E402


class _Method:
    """Stands in for SendMessage and friends."""

    def __init__(self, chat_id=None):
        self.chat_id = chat_id


class _NoChat:
    """getUpdates has no chat_id."""


def _refuses(times, retry_after=2, record=None):
    """A make_request that is refused `times` times, then succeeds."""
    state = {"calls": 0}

    async def make_request(bot, method):
        state["calls"] += 1
        if record is not None:
            record.append(time.monotonic())
        if state["calls"] <= times:
            raise TelegramRetryAfter(
                method=method, message="Flood control exceeded",
                retry_after=retry_after,
            )
        return "sent"

    make_request.state = state
    return make_request


class TestItWaitsAndSendsAnyway:
    @pytest.mark.asyncio
    async def test_a_refused_message_is_retried_and_delivered(self):
        """
        The whole fault in one assertion: this used to raise.
        """
        p = PacedSender(interval=0)
        send = _refuses(1, retry_after=0)
        assert await p(send, bot=None, method=_Method(-100)) == "sent"
        assert send.state["calls"] == 2

    @pytest.mark.asyncio
    async def test_it_waits_at_least_as_long_as_telegram_asked(self):
        p = PacedSender(interval=0)
        send = _refuses(1, retry_after=0.2)
        started = time.monotonic()
        await p(send, bot=None, method=_Method(-100))
        assert time.monotonic() - started >= 0.2

    @pytest.mark.asyncio
    async def test_it_gives_up_rather_than_looping_forever(self):
        p = PacedSender(interval=0, max_attempts=3)
        send = _refuses(99, retry_after=0)
        with pytest.raises(TelegramRetryAfter):
            await p(send, bot=None, method=_Method(-100))
        assert send.state["calls"] == 3

    @pytest.mark.asyncio
    async def test_an_absurd_wait_is_refused_immediately(self):
        """
        Holding a chat's queue for minutes stalls everything behind it, and
        a wait that long means something waiting will not fix.
        """
        p = PacedSender(interval=0, max_wait=10)
        send = _refuses(1, retry_after=600)
        with pytest.raises(TelegramRetryAfter):
            await p(send, bot=None, method=_Method(-100))
        assert send.state["calls"] == 1, "it slept on a 10 minute retry_after"


class TestTheWaitAppliesToTheWholeChat:
    @pytest.mark.asyncio
    async def test_a_second_send_cannot_overtake_one_that_is_waiting(self):
        """
        What actually holds the chat back during a retry is the per-chat
        lock, not a timestamp.

        The first version of this test asserted `elapsed > 0`, which is
        true of any code at all — and a mutation that deleted the line it
        was meant to protect passed cleanly. The line turned out to be
        redundant and was removed; this is the guarantee that is real.
        """
        order = []

        async def slow(bot, method):
            order.append("first-start")
            raise TelegramRetryAfter(method=method, message="flood",
                                     retry_after=0.3)

        async def quick(bot, method):
            order.append("second")
            return "sent"

        p = PacedSender(interval=0, max_attempts=1)

        async def first():
            try:
                await p(slow, bot=None, method=_Method(-100))
            except TelegramRetryAfter:
                order.append("first-done")

        async def second():
            await asyncio.sleep(0.05)
            await p(quick, bot=None, method=_Method(-100))

        await asyncio.gather(first(), second())
        assert order == ["first-start", "first-done", "second"], (
            "a queued send overtook one that was waiting out a rate limit"
        )

    @pytest.mark.asyncio
    async def test_one_chat_being_limited_does_not_stall_another(self):
        """
        A busy client group must not hold up a vendor group. The limit is
        per chat, so the queue is too.
        """
        p = PacedSender(interval=0)

        async def busy():
            await p(_refuses(1, retry_after=0.4), bot=None,
                    method=_Method(-111))

        async def other():
            started = time.monotonic()
            await p(_refuses(0), bot=None, method=_Method(-222))
            return time.monotonic() - started

        _, took = await asyncio.gather(busy(), other())
        assert took < 0.3, "the quiet chat waited behind the busy one"

    @pytest.mark.asyncio
    async def test_two_sends_to_one_chat_are_spaced_out(self):
        p = PacedSender(interval=0.3)
        await p(_refuses(0), bot=None, method=_Method(-100))
        started = time.monotonic()
        await p(_refuses(0), bot=None, method=_Method(-100))
        assert time.monotonic() - started >= 0.3

    @pytest.mark.asyncio
    async def test_polling_is_never_paced(self):
        """
        getUpdates has no chat_id and is not per-chat limited. Pacing it
        would throttle the bot's own ability to receive anything.
        """
        p = PacedSender(interval=5)
        started = time.monotonic()
        for _ in range(3):
            await p(_refuses(0), bot=None, method=_NoChat())
        assert time.monotonic() - started < 1


class TestNothingCanBypassIt:
    def test_it_is_a_session_middleware_not_a_helper(self):
        """
        A helper only protects call sites that remember it. The call that
        failed on 3 November was a reaction inside an exception handler.
        """
        from aiogram.client.session.middlewares.base import (
            BaseRequestMiddleware)
        assert issubclass(PacedSender, BaseRequestMiddleware)

    def test_all_three_bots_get_one(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        assert "sending.install(bridge, supplier, client)" in src

    def test_it_is_installed_before_anything_can_send(self):
        """
        The Notifier is handed the bots and can send immediately.
        """
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        assert src.index("sending.install") < src.index("notifier = Notifier")

    def test_each_bot_gets_its_own_pacer(self):
        """
        Three tokens, three separate rate budgets. Shared state would make
        one bot's busy group slow another bot down for no reason.
        """
        src = inspect.getsource(sending.install)
        assert "for bot in bots" in src
        assert "PacedSender(" in src[src.index("for bot in bots"):]


class TestTheAcknowledgementNeverKillsTheHandler:
    @pytest.mark.asyncio
    async def test_a_failing_reaction_falls_back_to_a_reply(self):
        sent = []

        class _M:
            chat = type("c", (), {"id": 1})()

            async def react(self, r): raise RuntimeError("no reactions here")
            async def reply(self, t, **kw): sent.append(t)

        await client_bot._acknowledge(_M())
        assert sent == ["Noted."]

    @pytest.mark.asyncio
    async def test_both_failing_does_not_raise(self):
        """
        The 3 November case exactly. The money is already recorded by the
        time this runs; losing the thumbs up must not unwind the handler.
        """
        class _M:
            chat = type("c", (), {"id": 1})()

            async def react(self, r):
                raise TelegramRetryAfter(method=None, message="flood",
                                         retry_after=8)

            async def reply(self, t, **kw):
                raise TelegramRetryAfter(method=None, message="flood",
                                         retry_after=8)

        await client_bot._acknowledge(_M())  # must not raise

    @pytest.mark.asyncio
    async def test_a_working_reaction_does_not_also_reply(self):
        """
        Two acknowledgements per payment is twice the sending, on the one
        path that runs for every single payment.
        """
        sent, reacted = [], []

        class _M:
            chat = type("c", (), {"id": 1})()

            async def react(self, r): reacted.append(r)
            async def reply(self, t, **kw): sent.append(t)

        await client_bot._acknowledge(_M())
        assert reacted and sent == []

    def test_the_failure_is_logged_with_the_reassurance(self):
        src = inspect.getsource(client_bot._acknowledge)
        assert "log.exception" in src
        assert "IS recorded" in src
