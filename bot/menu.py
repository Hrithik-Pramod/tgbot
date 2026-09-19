"""
The command menu each bot shows in Telegram.

THE COMPLAINT (Bridge, 19 September 2026)

    dont see - /issue?

/issue had been live since 11 September. He could not see it because nothing
had ever called set_my_commands, so Telegram had no list to show — no menu
button, no autocomplete, nothing. Every command in this product has only ever
been discoverable by someone telling him it exists.

main.py has said since the first commit that three tokens exist "so Telegram
shows each party only its own command menu". The separation was real; the
menu was never published.

WHY THE LISTS DIFFER

Telegram scopes the menu to the bot, so each party sees only their own
commands. That is the same boundary as bot/auth.py and it should not be
possible to read the Bridge's vocabulary off the supplier bot: knowing that
/reprice and /walletlink exist tells a counterparty how the book is run.

WHY THE DESCRIPTIONS ARE WRITTEN THE WAY THEY ARE

They appear in a dropdown a few words wide, next to the command, while
somebody is mid-task. So each says what the command DOES, in the shortest
form that distinguishes it from its neighbour — "change a rate on an open
trade" rather than "reprice". Telegram caps them at 256 characters; these are
nowhere near it, on purpose.

Published on every startup rather than once by hand, so a command added later
cannot be invisible the way /issue was.
"""

from __future__ import annotations

import logging

from aiogram.types import BotCommand, BotCommandScopeDefault

log = logging.getLogger(__name__)


MENUS: dict[str, list[tuple[str, str]]] = {
    "bridge": [
        ("issue",        "send the payment instruction to the client"),
        ("reprice",      "move an open trade onto the current rate"),
        ("setrate",      "set the buy and sell rate for a pairing"),
        ("viewrate",     "show every rate in force"),
        ("progress",     "every group's trading position on one screen"),
        ("summary",      "open trades for one supplier"),
        ("cancel",       "cancel a trade, or withdraw a supplier's claim"),
        ("correct",      "reopen a completed trade or remove a payment"),
        ("addvendor",    "register a new vendor group"),
        ("wallet",       "show wallets and which pairing they serve"),
        ("walletadd",    "register a new wallet address"),
        ("walletlink",   "set where a pairing pays the client"),
        ("walletchange", "change an existing wallet's address"),
        ("send",         "note a transfer you have made"),
        ("export",       "download the ledger as a CSV"),
    ],
    "supplier": [
        ("send",           "tell us you have sent, and to which account"),
        ("account",        "register a bank account"),
        ("account_remove", "remove a bank account"),
        ("progress",       "how much of your INR has arrived"),
    ],
    "client": [
        ("accounts", "the accounts you can pay for this trade"),
        ("add",      "log a payment with its UTR"),
        ("done",     "close the trade when you have paid in full"),
    ],
}


async def publish_menus(bots_by_role: dict) -> None:
    """
    Push each bot's command list to Telegram.

    Failures are logged and swallowed per bot. A menu is a convenience; a bot
    that refuses to start because Telegram would not accept a dropdown is a
    far worse outcome than one nobody can autocomplete.
    """
    for role, bot in bots_by_role.items():
        commands = MENUS.get(role)
        if not commands:
            continue
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in commands],
                scope=BotCommandScopeDefault(),
            )
            log.info("published %d commands to the %s bot", len(commands), role)
        except Exception:
            log.exception("could not publish the %s command menu", role)
