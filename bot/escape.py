"""
A command always means what it says, even mid-conversation.

THE TRAP

Every multi-step flow — /setrate, /account, /send, /correct, /cancel — parks
the user in an FSM state, and the handler for that step matches any message
at all. So a command typed while a flow is open is read as an answer to the
question being asked:

    /setrate
    Which supplier?           [taps one]
    Which client?             [taps one]
    Enter the supply rate:
    /viewrate
    I could not read that as a rate. Send just the number.

There is no way out. Every command is eaten by the step, answered with the
same complaint, and the flow stays open. The Bridge, 15 September 2026:

    Command override, if I say /setrate, and then decide to use another
    command, the command will not let me get out of the /setrate path

WHY IT IS FIXED HERE AND NOT IN THE HANDLERS

The same fix could be written as ~F.text.startswith("/") on every FSM step,
and it would work — until somebody adds a flow and forgets one. That is
precisely how /done was dead for a day: a single handler missing a single
filter, failing silently.

An outer middleware runs before filters are evaluated, so clearing the state
there means the step handler no longer matches and the command reaches its
own handler normally. One rule, applied to every flow that exists and every
flow anyone writes later.

WHY CLEARING IS THE RIGHT ANSWER

No step in this bot ever expects a value beginning with "/" — they take
amounts, rates, account names, account numbers, IFSC codes, hashes and
reasons. A leading slash is unambiguous: the user has changed their mind.
Abandoning the flow is what they meant.

Nothing is written on the way out. A flow only touches the database at its
final confirmation, so walking away mid-way leaves no half-finished trade,
rate or account behind.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

log = logging.getLogger(__name__)


class CommandEscapeMiddleware(BaseMiddleware):
    """Abandon any open conversation when a command arrives."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")

        if state is not None and isinstance(event, Message):
            # caption too: a command can arrive on a photo
            text = (event.text or event.caption or "").lstrip()
            if text.startswith("/"):
                current = await state.get_state()
                if current is not None:
                    await state.clear()

                    # And the copy the router is about to read.
                    #
                    # aiogram's FSMContextMiddleware runs on the UPDATE
                    # observer, before this one, and puts the state into the
                    # data dict twice: `state` (the live context) and
                    # `raw_state` (a snapshot of the string). StateFilter
                    # matches on the SNAPSHOT. So clearing the context alone
                    # empties the storage and changes nothing about routing —
                    # the message still reaches the step handler, which reads
                    # it as an answer to whatever it asked.
                    #
                    # Live, 19 September 2026. Mid-way through /addvendor the
                    # Bridge typed /issue, and the bot replied "Deal numbers
                    # for /issue@pt_bridge_ctrl_bot will read ISSU1, ISSU2" —
                    # it had taken the command as the vendor's name. Exactly
                    # the complaint this middleware was written for on
                    # 15 September, four days after it shipped.
                    #
                    # The tests passed throughout, because they asserted that
                    # clear() was called rather than that the command ran.
                    data["raw_state"] = None
                    log.info(
                        "command %r abandoned open state %s in chat %s",
                        text.split()[0], current, event.chat.id,
                    )

        return await handler(event, data)
