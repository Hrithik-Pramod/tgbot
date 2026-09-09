"""
Handler ordering.

aiogram tries a router's handlers in registration order and stops at the first
match. A handler filtered on nothing but `F.text` therefore matches every text
message including commands, and silently shadows every command registered
below it.

That is not hypothetical. /done was defined at the bottom of bot/client_bot.py,
below the pasted-payment catch-all, so it never ran: the catch-all claimed
"/done@pt_client_desk_bot", found no payments in it, and — because the text
contains no digit — returned without a word. In the group it looked exactly
like a dead bot. /accounts and /add worked only because they happened to sit
above the catch-all.

The fix was to filter commands out of the catch-all itself, so ordering stops
mattering. These tests pin that, and they pin it for every bot rather than just
the one that broke, because the next catch-all will be written somewhere else.
"""

import sys
from pathlib import Path

import pytest
from aiogram.filters import Command
from aiogram.utils.magic_filter import MagicFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_bot, bridge_trade, client_bot, supplier_bot  # noqa: E402

ROUTERS = {
    "bridge_bot": bridge_bot.router,
    "bridge_trade": bridge_trade.router,
    "client_bot": client_bot.router,
    "supplier_bot": supplier_bot.router,
}


class _StubMessage:
    """Enough of a Message for a magic filter to resolve against."""

    def __init__(self, text: str):
        self.text = text
        self.caption = None

    def __getattr__(self, name):        # any attribute a filter reaches for
        return None


def _catch_all_handlers(router):
    """
    Handlers filtered only by magic filters — no Command, no FSM state.

    These are the ones that can shadow a command. A state-filtered handler
    cannot: it only runs mid-conversation, when shadowing is what you want.
    """
    found = []
    for handler in router.observers["message"].handlers:
        callbacks = [f.callback for f in (handler.filters or [])]
        if not callbacks or any(isinstance(c, Command) for c in callbacks):
            continue
        magics = [
            c for c in callbacks
            if getattr(c, "__self__", None).__class__ is MagicFilter
        ]
        if len(magics) != len(callbacks):
            continue                     # state-filtered, not a catch-all
        found.append((handler.callback.__name__, magics))
    return found


def _matches(magics, text: str) -> bool:
    return all(bool(f(_StubMessage(text))) for f in magics)


COMMANDS = [
    "/done", "/done@pt_client_desk_bot", "/accounts", "/add",
    "/account", "/account_remove", "/send", "/setrate", "/wallet",
    "/walletchange", "/summary", "/viewrate", "/progress",
    "/cancel", "/correct", "/export", "/start", "/help",
]


@pytest.mark.parametrize("router_name", sorted(ROUTERS))
@pytest.mark.parametrize("command", COMMANDS)
def test_no_catch_all_swallows_a_command(router_name, command):
    """The regression guard. A catch-all must never claim a command."""
    for handler_name, magics in _catch_all_handlers(ROUTERS[router_name]):
        assert not _matches(magics, command), (
            f"{router_name}.{handler_name} matches {command!r}. Any command "
            f"registered after it will never run, and will fail silently. "
            f"Add ~F.text.startswith('/') to its filter."
        )


def test_the_paste_handler_still_reads_pastes():
    """The exclusion must not cost the catch-all its actual job."""
    handlers = dict(
        (name, magics)
        for name, magics in _catch_all_handlers(client_bot.router)
    )
    assert "on_pasted_payment" in handlers, "the paste handler vanished"

    magics = handlers["on_pasted_payment"]
    for paste in [
        "BKIDR12026090800000380\n249000\nto Ekta Traders",
        "249000 BKIDR12026090800000380 Ekta Traders",
        "Rs. 249 000 to Ekta Traders Pvt Ltd",
    ]:
        assert _matches(magics, paste), f"stopped matching a real paste: {paste!r}"


def test_client_bot_has_exactly_one_catch_all():
    """
    Two catch-alls on one router means the second is unreachable.

    Stated as a test because it is invisible in the source — both look fine on
    their own.
    """
    names = [n for n, _ in _catch_all_handlers(client_bot.router)]
    assert names == ["on_pasted_payment"], names
