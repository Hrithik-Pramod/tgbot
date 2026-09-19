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
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)


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

    async def _report_new_chat(self, event, data, chat) -> None:
        """
        Tell the Bridge that this bot was just added somewhere unregistered.

        Only on the event that says so — the bot appearing in new_chat_members.
        Any other traffic from an unregistered chat stays silent, or an
        unknown group could make the bot chatter at the Bridge by messaging it.

        Wrapped whole: this is a convenience hanging off the security path, and
        it must never be able to change what that path decides. If the
        notification fails, the chat is still refused.
        """
        try:
            members = getattr(event, "new_chat_members", None)
            if not members:
                return
            bot = data.get("bot")
            if bot is None or not any(u.id == bot.id for u in members):
                return

            notifier = data.get("notifier")
            if notifier is None:
                return

            title = getattr(chat, "title", None) or "no title"
            await notifier.to_bridge(
                f"This bot was added to an unregistered group.\n\n"
                f"  {title}\n"
                f"  chat id  {chat.id}\n\n"
                "It will ignore everything sent there until the group is "
                "registered. Run /addvendor and give it that chat id."
            )
            log.info("announced unregistered chat %s (%r)", chat.id, title)
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
