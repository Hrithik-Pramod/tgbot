"""
Outbound pacing and retry, so Telegram's rate limit cannot silence the bot.

WHAT HAPPENED — 3 November 2026

    Hi Peter / Bot has stopped / Please can you check
    — Bridge, 21:57

It had not stopped. It recorded a payment at 23:15, and 133 audit entries
that day. What it could not do was say so:

    TelegramRetryAfter: Flood control exceeded on method 'SendMessage'
    in chat -100…  Retry in 8 seconds.
    ERROR aiogram.event: Cause exception while process update
    ERROR bot.notifier: failed to notify bridge channel

Telegram allows a burst and then starts refusing, asking you to come back in
N seconds. Nothing in the bot did. The refusal came back as an exception, it
was never caught, and it killed the handler on the spot.

WHY IT LOOKS EXACTLY LIKE BEING DEAD

The client's team runs on one rule, which we gave them:

    no acknowledgement means it was not picked up

So a bot that records every rupee correctly and cannot send a thumbs up is,
from the desk, a bot that has stopped. Worse than stopped: it is silently
succeeding, so nobody re-sends anything and everyone distrusts the ledger.

_acknowledge even had a fallback — react, and on failure reply instead — and
both calls are sends, so under flood control both failed and the fallback
raised too.

WHAT THIS DOES

Two things, in the one place every outbound call must pass through.

  PACE.  At most one request per chat per interval. Telegram's published
         guidance is about 20 messages a minute to a group; the default here
         is deliberately gentler than the limit, because being slightly slow
         is invisible and being refused is not.

  RETRY. When Telegram says "retry after N", wait N and send it again. That
         is not a workaround, it is the documented protocol — the message
         was never delivered, so re-sending is the correct response and not
         a duplicate.

It is a session middleware rather than a helper function on purpose: a
helper only protects the call sites that remember to use it, and the call
that failed on 3 November was a reaction inside an exception handler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Optional

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter

log = logging.getLogger(__name__)


# Gentler than Telegram's limit, on purpose. A payment acknowledged a second
# late is unnoticeable; one refused is an outage.
DEFAULT_INTERVAL = 1.2

# Telegram's own wait, honoured as given, is the authority. This only stops
# an unbounded loop if something is badly wrong.
MAX_ATTEMPTS = 6

# A retry_after longer than this means something is wrong that waiting will
# not fix, and holding the chat's queue for minutes would stall everything
# behind it.
MAX_WAIT = 90.0


def _chat_of(method: Any) -> Optional[Any]:
    """
    Which queue this call belongs in.

    Telegram's per-chat limit is per chat, so the pacing must be too — one
    global queue would make a quiet vendor group wait behind a busy client
    group for no reason.
    """
    return getattr(method, "chat_id", None)


class PacedSender(BaseRequestMiddleware):
    """
    Serialise and space out calls per chat, and honour Telegram's retry_after.
    """

    def __init__(self, *, interval: float = DEFAULT_INTERVAL,
                 max_attempts: int = MAX_ATTEMPTS,
                 max_wait: float = MAX_WAIT) -> None:
        self.interval = interval
        self.max_attempts = max_attempts
        self.max_wait = max_wait
        self._locks: dict[Any, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_allowed: dict[Any, float] = {}

    async def __call__(self, make_request, bot: Bot, method):
        chat = _chat_of(method)
        if chat is None:
            # getUpdates, getMe and friends. Not per-chat limited, and
            # pacing them would throttle the polling loop itself.
            return await self._with_retry(make_request, bot, method, chat)

        # One sender at a time per chat, so two concurrent handlers cannot
        # both decide the coast is clear and fire together.
        async with self._locks[chat]:
            await self._wait_turn(chat)
            try:
                return await self._with_retry(make_request, bot, method, chat)
            finally:
                self._next_allowed[chat] = time.monotonic() + self.interval

    async def _wait_turn(self, chat) -> None:
        due = self._next_allowed.get(chat)
        if due is None:
            return
        delay = due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _with_retry(self, make_request, bot: Bot, method, chat):
        last: Optional[TelegramRetryAfter] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                last = exc
                wait = float(exc.retry_after)
                if wait > self.max_wait:
                    log.error(
                        "%s refused for chat %s with retry_after=%ss, which "
                        "is beyond the cap — giving up rather than stalling "
                        "the queue",
                        type(method).__name__, chat, wait,
                    )
                    raise
                # Nothing is written to _next_allowed here, deliberately.
                #
                # The first version did, to hold the rest of the chat's
                # queue back during the wait — but the per-chat lock
                # already does that, and the `finally` in __call__ rewrites
                # the slot on every path out of here, so the assignment was
                # overwritten in all cases. A mutation test removed it and
                # not one assertion changed, which is the definition of a
                # line that only looks like a safeguard.
                log.warning(
                    "%s rate-limited on chat %s, waiting %ss (attempt %d/%d)",
                    type(method).__name__, chat, wait, attempt,
                    self.max_attempts,
                )
                # A little past what it asked for. Coming back at the exact
                # second it named is how you get refused twice.
                await asyncio.sleep(wait + 0.25)
        log.error(
            "%s for chat %s still rate-limited after %d attempts; the "
            "message was NOT delivered",
            type(method).__name__, chat, self.max_attempts,
        )
        raise last  # type: ignore[misc]


def install(*bots: Bot, interval: float = DEFAULT_INTERVAL) -> None:
    """
    Put the pacer in front of every outbound call on each bot.

    Called once at startup for all three bots. Each gets its own instance:
    the three have separate tokens and therefore separate rate budgets, and
    sharing the state would make one bot's busy group slow another's.
    """
    for bot in bots:
        bot.session.middleware(PacedSender(interval=interval))
