"""
Group membership alerts.

Access is granted by group membership (see bot/auth.py), so a change in
membership is a change in who can operate the system. That makes it worth
knowing about immediately rather than discovering later.

Every join and departure in a registered group is reported to the Bridge
channel. This is the compensating control for chat-based authorisation and it
costs the Bridge nothing — no approval step, no extra command, just a message
saying what happened.

Registered as a router on both the supplier and client bots.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

log = logging.getLogger(__name__)
router = Router()


def _describe(user) -> str:
    """A readable identifier for a Telegram user, without assuming they have a username."""
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    handle = f"@{user.username}" if user.username else "no username"
    return f"{name or 'Unknown'} ({handle}, id {user.id})"


@router.message(F.new_chat_members)
async def on_join(message: Message, party, notifier) -> None:
    """
    Someone was added to a registered group.

    They can now use this bot as this party, so the Bridge is told who, and by
    whom. The bot being added to the group itself is filtered out - that is a
    setup step, not a membership change worth alarming about.
    """
    joined = [u for u in (message.new_chat_members or []) if not u.is_bot]
    if not joined:
        return

    added_by = _describe(message.from_user) if message.from_user else "unknown"
    lines = [
        f"⚠ New member in {party['label']}'s group",
        "",
        "They can now use the bot as this party:",
    ]
    lines += [f"  • {_describe(u)}" for u in joined]
    lines += ["", f"Added by: {added_by}"]

    if party["role"] == "supplier":
        # The consequential case: a supplier group member can register a bank
        # account that ends up on a payment instruction.
        lines += [
            "",
            "Note: members of a supplier group can add and remove bank "
            "accounts. Check this is expected.",
        ]

    await notifier.to_bridge("\n".join(lines))
    log.warning(
        "membership: %s user(s) joined %s (chat %s)",
        len(joined), party["label"], message.chat.id,
    )


@router.message(F.left_chat_member)
async def on_leave(message: Message, party, notifier) -> None:
    """Someone left or was removed. Recorded so the picture stays complete."""
    left = message.left_chat_member
    if left is None or left.is_bot:
        return

    await notifier.to_bridge(
        f"Member left {party['label']}'s group\n\n"
        f"  • {_describe(left)}\n\n"
        "They can no longer use the bot as this party."
    )
    log.info("membership: user left %s (chat %s)", party["label"], message.chat.id)
