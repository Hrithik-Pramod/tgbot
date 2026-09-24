"""
Every command is discoverable from inside Telegram.

THE COMPLAINT (Bridge, 19 September 2026)

    dont see - /issue?

/issue had been live since 11 September. Nothing had ever called
set_my_commands, so Telegram had no list for any of the three bots: no menu
button, no autocomplete. Every command in this product was discoverable only
by being told it existed — which is how /issue came to be built in the first
place, after he could not find the confirm button and got stuck mid-trade
with the client waiting.

main.py has claimed since the first commit that three tokens exist "so
Telegram shows each party only its own command menu". The token separation
was real. The menu was never published.

THE TEST THAT MATTERS MOST HERE

Not that the call exists — that the lists stay honest. A menu that has
drifted from the code is worse than none, because it advertises commands that
do nothing and hides the ones that work.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_bot, bridge_trade, client_bot, supplier_bot  # noqa: E402
from bot.menu import MENUS, publish_menus  # noqa: E402

MODULES = {
    "bridge": (bridge_bot, bridge_trade),
    "supplier": (supplier_bot,),
    "client": (client_bot,),
}


def _registered(role: str) -> set:
    found = set()
    for module in MODULES[role]:
        src = Path(module.__file__).read_text()
        found |= set(re.findall(r'Command\("([a-z_]+)"\)', src))
    return found


class TestTheMenuMatchesTheCode:
    def test_every_advertised_command_exists(self):
        """
        A menu entry for a command nobody wrote is a promise the bot breaks
        silently — the user types it and gets nothing at all, because an
        unregistered command falls through to the catch-all or to no handler.
        """
        for role, commands in MENUS.items():
            advertised = {c for c, _ in commands}
            missing = advertised - _registered(role)
            assert not missing, f"{role} menu advertises {missing}, which do not exist"

    def test_every_command_is_advertised(self):
        """
        The /issue failure, stated directly. A command that works and cannot
        be found may as well not be there.
        """
        for role, commands in MENUS.items():
            advertised = {c for c, _ in commands}
            hidden = _registered(role) - advertised
            assert not hidden, (
                f"{role} has {hidden} working but absent from the menu — "
                "this is exactly how /issue stayed invisible for eight days"
            )

    def test_issue_is_there(self):
        """The one he could not find, named so this cannot regress quietly."""
        assert "issue" in {c for c, _ in MENUS["bridge"]}


class TestTheMenusStayApart:
    # A name both sides genuinely own, each doing their own version of it.
    # Not a list to grow casually: every entry is a command word a
    # counterparty can see the Bridge also uses.
    #
    #   send      the supplier says they have sent; the Bridge notes a
    #             transfer he has made
    #   progress  the supplier sees their own collection and nothing else
    #             (decision D4); the Bridge sees every group's position
    #             (his request, 19 September 2026)
    SHARED = {"send", "progress"}

    def test_no_bridge_command_leaks_to_a_counterparty(self):
        """
        Telegram scopes the menu to the bot, which is the same boundary as
        bot/auth.py. Knowing /reprice and /walletlink exist tells a
        counterparty how the book is run.
        """
        bridge = {c for c, _ in MENUS["bridge"]}
        for role in ("supplier", "client"):
            theirs = {c for c, _ in MENUS[role]}
            assert (bridge & theirs) <= self.SHARED, (
                f"{role} advertises Bridge commands: "
                f"{(bridge & theirs) - self.SHARED}"
            )

    def test_a_shared_name_is_a_different_handler_on_each_bot(self):
        """
        Sharing a word is fine. Sharing an implementation is not — the
        supplier's /progress must not be able to return the Bridge's view of
        everyone's book.
        """
        from bot import bridge_bot, supplier_bot

        for name in self.SHARED:
            handlers = {
                role: getattr(module, f"cmd_{name}", None)
                for role, module in (("bridge", bridge_bot),
                                     ("supplier", supplier_bot))
            }
            present = [h for h in handlers.values() if h is not None]
            if len(present) == 2:
                assert present[0] is not present[1], (
                    f"/{name} is the same function on both bots"
                )

    def test_the_suppliers_progress_stays_theirs_alone(self):
        """
        D4, pinned. The Bridge's /progress walks every pairing; the
        supplier's must only ever reach their own trade.
        """
        import inspect

        from bot import supplier_bot

        src = inspect.getsource(supplier_bot.cmd_progress)
        assert "open_trade_for_supplier" in src
        assert "book_progress" not in src

    # Names the client and the supplier both use, for different things on
    # their own bots.
    #
    #   accounts  the supplier sees the accounts they have registered (their
    #             own, client request 24 September 2026); the client sees the
    #             accounts they may pay into for this trade, narrowed on
    #             11 September to stop it listing the Bridge's whole book
    CLIENT_SUPPLIER_SHARED = {"accounts"}

    def test_the_client_is_not_shown_supplier_commands(self):
        """
        Relaxed from "no overlap at all" on 24 September, when /accounts was
        added for vendors.

        The boundary that matters is not the word — each bot publishes only
        its own menu, so a client never sees the supplier's list. It is that
        a shared word must not become a shared implementation, which the
        next test holds.
        """
        client = {c for c, _ in MENUS["client"]}
        supplier = {c for c, _ in MENUS["supplier"]}
        assert (client & supplier) <= self.CLIENT_SUPPLIER_SHARED, (
            "client and supplier share commands that were not thought about: "
            f"{(client & supplier) - self.CLIENT_SUPPLIER_SHARED}"
        )

    def test_a_name_shared_with_the_client_is_a_different_handler(self):
        """
        The real guard. /accounts on the supplier bot answers "which
        accounts are mine"; on the client bot it answers "where may I pay".
        One function serving both would hand a client the vendor's list or
        a vendor the client's — and the second of those is the 11 September
        disclosure.
        """
        from bot import client_bot, supplier_bot

        for name in self.CLIENT_SUPPLIER_SHARED:
            s = getattr(supplier_bot, f"cmd_{name}", None)
            c = getattr(client_bot, f"cmd_{name}", None)
            if s is not None and c is not None:
                assert s is not c, f"/{name} is the same function on both bots"

    def test_all_three_roles_have_a_menu(self):
        assert set(MENUS) == {"bridge", "supplier", "client"}


class TestTheEntriesAreUsable:
    def test_descriptions_fit_and_say_something(self):
        for role, commands in MENUS.items():
            for command, description in commands:
                assert description, f"{role}/{command} has no description"
                assert len(description) <= 256, f"{role}/{command} too long"
                assert description.lower() != command, (
                    f"{role}/{command} describes itself with its own name"
                )

    def test_no_command_is_listed_twice(self):
        for role, commands in MENUS.items():
            names = [c for c, _ in commands]
            assert len(names) == len(set(names)), f"{role} repeats a command"

    def test_the_command_names_are_valid(self):
        """Telegram accepts lowercase letters, digits and underscores only."""
        for role, commands in MENUS.items():
            for command, _ in commands:
                assert re.fullmatch(r"[a-z0-9_]{1,32}", command), (
                    f"{role}/{command} is not a valid command name"
                )


class TestPublishingCannotStopTheBot:
    def test_a_failure_is_swallowed_per_bot(self):
        """
        A dropdown is a convenience. A process that refuses to start because
        Telegram would not accept one is a far worse trade.
        """
        src = inspect.getsource(publish_menus)
        assert "except Exception:" in src
        assert "log.exception" in src

    def test_it_runs_before_polling_starts(self):
        """Or the menu is wrong for however long the first poll takes."""
        main_src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
        published_at = main_src.index("await publish_menus")
        polling_at = main_src.index("start_polling")
        assert published_at < polling_at

    def test_it_runs_on_every_startup(self):
        """
        Published from code rather than set once by hand, so a command added
        later cannot be invisible the way /issue was.
        """
        main_src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
        assert "await publish_menus(bots_by_role)" in main_src
