"""
Startup wiring tests.

These build the real dispatchers the way main.py does. Importing a module is
not the same as wiring it: the first deployment crash-looped on
"Router is already attached", because bot/membership exposed one module-level
Router that was included in both the supplier and the client dispatcher, and
aiogram allows a router exactly one parent.

An import check cannot catch that. Constructing the dispatchers can.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_bot, bridge_trade, client_bot, membership, supplier_bot  # noqa: E402
from main import build_dispatcher  # noqa: E402


class _Config:
    bridge_user_id = 0
    strict_bridge_user = False


def _build_all():
    """Exactly the wiring main.py performs, minus the bot tokens."""
    cfg = _Config()
    return [
        build_dispatcher([bridge_bot.router, bridge_trade.router],
                         repo=None, notifier=None, role="bridge", config=cfg),
        build_dispatcher([membership.build_router(), supplier_bot.router],
                         repo=None, notifier=None, role="supplier", config=cfg),
        build_dispatcher([membership.build_router(), client_bot.router],
                         repo=None, notifier=None, role="client", config=cfg),
    ]


class TestDispatchersBuild:
    def test_all_three_build(self):
        """The regression guard for the first deployment failure."""
        assert len(_build_all()) == 3

    def test_membership_factory_returns_a_new_router_each_time(self):
        a, b = membership.build_router(), membership.build_router()
        assert a is not b, (
            "a shared router can only ever attach to one dispatcher — "
            "this is what broke the first deploy"
        )

    def test_a_shared_router_really_would_fail(self):
        """
        Pins the aiogram behaviour the factory exists to work around, so if it
        ever changes we find out here rather than in production.
        """
        shared = membership.build_router()
        cfg = _Config()
        build_dispatcher([shared], repo=None, notifier=None,
                         role="supplier", config=cfg)
        with pytest.raises(RuntimeError, match="already attached"):
            build_dispatcher([shared], repo=None, notifier=None,
                             role="client", config=cfg)

    def test_no_router_instance_is_included_twice(self):
        """
        The invariant that actually matters, stated directly.

        The per-bot routers are module-level singletons, which is fine because
        each attaches to exactly one dispatcher per process. Membership is the
        one router used by two bots, so it must come from the factory. This
        test fails the moment any single-use router is wired into two
        dispatchers — the same mistake, wherever it next appears.

        (Building the whole set twice in one process is NOT expected to work,
        and is not something main() does: a restart is a new process.)
        """
        configs = [
            [bridge_bot.router, bridge_trade.router],
            [membership.build_router(), supplier_bot.router],
            [membership.build_router(), client_bot.router],
        ]
        seen = [id(r) for group in configs for r in group]
        assert len(seen) == len(set(seen)), "a router is wired into two dispatchers"


class TestCommandCoverage:
    """Every command in the brief is registered on the right bot."""

    @staticmethod
    def _commands(*modules):
        import re
        found = set()
        for m in modules:
            src = Path(m.__file__).read_text()
            found |= set(re.findall(r'Command\("([a-z_]+)"\)', src))
        return found

    def test_bridge(self):
        got = self._commands(bridge_bot, bridge_trade)
        assert {"setrate", "wallet", "walletchange", "send", "summary"} <= got

    def test_supplier(self):
        got = self._commands(supplier_bot)
        assert {"account", "account_remove", "send"} <= got

    def test_client(self):
        got = self._commands(client_bot)
        assert {"accounts", "add", "done"} <= got
