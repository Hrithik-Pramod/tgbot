"""
Authorisation.

MODEL (client decision, 7 September 2026)
-----------------------------------------
A party is identified by the CHAT a command arrives in, not by which individual
sent it. If the Bridge adds the supplier bot to a group and registers that group
as Supplier A, then everyone in that group can act as Supplier A.

Access is therefore controlled by controlling group membership, which the Bridge
does manually as the group admin. This was an explicit instruction and it is the
model implemented here.

What that means in practice, so it is written down somewhere:

  * Anyone in a client group can log payments and close trades as that client.
  * Anyone in a supplier group can register and remove bank accounts, and
    nominate which account a trade's INR is paid into.

The second one is the one that matters, because a bank account registered by
someone in that group can end up on a payment instruction sent to a client. Two
controls compensate for it, and neither costs the Bridge any convenience:

  * Every account added or removed is reported to the Bridge channel.
  * Every join and departure from a registered group is reported to the Bridge
    channel (see bot/membership.py).

STRICT_BRIDGE_USER=true in the environment restores the previous behaviour for
the Bridge bot only - commands then have to come from BRIDGE_USER_ID as well as
from the Bridge chat. Off by default, as instructed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)

# How often an unregistered chat that is being USED may be re-reported to the
# Bridge. Long enough not to nag, short enough that a group cannot sit dead
# for a working day the way Alpha did on 19 September.
NUDGE_INTERVAL_SECONDS = 3600


class ChatRoleMiddleware(BaseMiddleware):
    """
    Resolves the chat a command arrived in to a party, and injects it as `party`.

    Handlers never resolve the sender themselves. If this middleware did not put
    a party in the data dict, the handler does not run at all.
    """

    def __init__(
        self,
        repo,
        required_role: str,
        *,
        bridge_user_id: int | None = None,
        strict_bridge_user: bool = False,
    ):
        self.repo = repo
        self.required_role = required_role
        self.bridge_user_id = bridge_user_id
        self.strict_bridge_user = strict_bridge_user
        # chat id -> when it was last reported. In memory on purpose: a
        # restart re-reports once, which is the right way round for a group
        # still waiting to be registered.
        self._nudged: dict[int, float] = {}

    async def _report_new_chat(self, event, data, chat) -> None:
        """
        Tell the Bridge about an unregistered chat — on the way in, and again
        if somebody is plainly trying to use it.

        TWO TRIGGERS, AND THE SECOND ONE COST A DAY

        The first is the bot being added: `new_chat_members` contains it. That
        posts the chat id so the group can be registered.

        Once was not enough. 19 September, 14:39: the id for V5 went to UI
        Control. Nobody ran /addvendor. The group was renamed Alpha, and from
        22:53 that night the Bridge typed commands at it — six of them over
        twelve hours, every one logged "ignored" and answered with nothing.

            Can you please fix Alpha and bot access as need to clear this asap
            bot ... not listening

        The bot was behaving exactly as designed and the design was wrong. A
        single notification that scrolls away is not a mechanism; it is a
        message, and the group was silently dead the moment it was missed.

        So a COMMAND in an unregistered chat re-reports it, at most hourly per
        chat. Commands only, because someone typing a slash is trying to use
        the bot, whereas ordinary conversation in a group we do not know is
        none of our business — and answering all of it would let any unknown
        group make the bot chatter at the Bridge.

        Still nothing is ever said IN the unregistered chat. A reply there
        would confirm to a stranger that this bot is live and hint at what it
        does. Everything goes to the Bridge privately.

        Wrapped whole: this is a convenience hanging off the security path,
        and it must never change what that path decides. If the notification
        fails, the chat is still refused.
        """
        try:
            bot = data.get("bot")
            members = getattr(event, "new_chat_members", None)
            joined = bool(
                members and bot is not None
                and any(u.id == bot.id for u in members)
            )

            if not joined:
                text = (getattr(event, "text", None)
                        or getattr(event, "caption", None) or "").lstrip()
                if not text.startswith("/"):
                    return
                # None, not 0.0. monotonic() counts from an arbitrary point —
                # on a fresh container it starts near zero — so a default of
                # 0.0 reads as "reported a moment ago" and would silence
                # every unregistered chat for the first hour after each
                # restart. Which is exactly when one is most likely to be
                # waiting to be registered.
                last = self._nudged.get(chat.id)
                now = time.monotonic()
                if last is not None and now - last < NUDGE_INTERVAL_SECONDS:
                    return
                self._nudged[chat.id] = now

            notifier = data.get("notifier")
            if notifier is None:
                return

            title = getattr(chat, "title", None) or "no title"
            opening = (
                "This bot was added to an unregistered group."
                if joined else
                "Somebody is trying to use the bot in a group that is NOT "
                "registered, so it is ignoring them."
            )
            await notifier.to_bridge(
                f"{opening}\n\n"
                f"  {title}\n"
                f"  chat id  {chat.id}\n\n"
                "Nothing sent there is read until the group is registered. "
                "Run /addvendor and give it that chat id."
            )
            log.info(
                "announced unregistered chat %s (%r), %s",
                chat.id, title, "joined" if joined else "in use",
            )
        except Exception:
            log.exception("could not announce unregistered chat %s", chat.id)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = data.get("event_chat")
        if chat is None:
            return None

        party = await self.repo.party_by_chat_id(chat.id)

        if party is None:
            # An unregistered chat. Silence rather than an error message: a
            # reply would confirm to a stranger that this bot is live and hint
            # at what it does.
            #
            # One exception, and it is not a reply to the chat: if this bot has
            # just been ADDED to the chat, the Bridge is told privately, with
            # the chat id. He needs that id to register the group and there was
            # no way to get it from inside the product — every vendor so far
            # was onboarded by me running SQL with an id he found elsewhere.
            #
            # Client request, 18 September 2026: "I'm going to create some more
            # FX groups now so they are ready for later use, add bots".
            #
            # Nothing about who may do what changes here. The chat is still
            # rejected on the next line.
            await self._report_new_chat(event, data, chat)
            log.warning("command in unregistered chat %s - ignored", chat.id)
            return None

        if party["role"] != self.required_role:
            # The wrong bot for this chat. Usually a setup mistake: the supplier
            # bot added to a client group, or similar.
            log.warning(
                "chat %s is registered as %s but the command reached the %s bot",
                chat.id, party["role"], self.required_role,
            )
            return None

        if (
            self.required_role == "bridge"
            and self.strict_bridge_user
            and self.bridge_user_id is not None
        ):
            user = data.get("event_from_user")
            if user is None or user.id != self.bridge_user_id:
                log.warning(
                    "strict mode: refusing bridge command from user %s",
                    getattr(user, "id", None),
                )
                return None

        data["party"] = party
        return await handler(event, data)
