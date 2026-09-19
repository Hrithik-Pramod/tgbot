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
    def test_no_bridge_command_leaks_to_a_counterparty(self):
        """
        Telegram scopes the menu to the bot, which is the same boundary as
        bot/auth.py. Knowing /reprice and /walletlink exist tells a
        counterparty how the book is run.
        """
        bridge = {c for c, _ in MENUS["bridge"]}
        for role in ("supplier", "client"):
            theirs = {c for c, _ in MENUS[role]}
            # /send is genuinely both parties' word for their own action.
            assert (bridge & theirs) <= {"send"}, (
                f"{role} advertises Bridge commands: {bridge & theirs}"
            )

    def test_the_client_is_not_shown_supplier_commands(self):
        client = {c for c, _ in MENUS["client"]}
        supplier = {c for c, _ in MENUS["supplier"]}
        assert not client & supplier

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
