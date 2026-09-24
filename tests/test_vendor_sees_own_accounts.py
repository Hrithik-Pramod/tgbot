"""
A vendor can see the accounts they have live.

THE REQUEST (Bridge, relaying a vendor, 24 September 2026)

    Vendor is looking for option to display accounts and details they have
    live, bank accounts
    Just an /accounts
    And it shows their accounts and details

Until now a vendor could ADD an account (/account) and REMOVE one
(/account_remove, which lists them behind a button) but could not simply
read back what was registered. So "which account are they paying into" was
a question for the Bridge, which is the same shape as the 10 September
complaint that produced the supplier's /progress:

    collection progress, can i add this to the suppliers as an option? just
    another thing they always ask

WHY THIS IS NOT A DISCLOSURE

Every account shown belongs to the party asking. The handler passes
party["id"] — their own — and list_bank_accounts filters to is_active, so
"live" is answered by the query rather than by a promise in the text.

/accounts also exists on the CLIENT bot and answers a different question:
which accounts that client may pay into. That one was narrowed on
11 September after it listed the Bridge's whole book into a counterparty's
group. Two commands, one word, two handlers — asserted in
test_command_menu.py, because one function serving both is how the narrowed
version gets undone by accident.

THE TWO SWITCHES

main.py sets DefaultBotProperties(parse_mode=None), so a <code> span only
renders if the send passes parse_mode explicitly. Half-setting that pair is
precisely the 23 September fault — "the copy paste is not working again for
settlement values" — so both halves are asserted here from the start rather
than after somebody complains.
"""

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import supplier_bot  # noqa: E402
from bot.menu import MENUS  # noqa: E402
from core.summary import render_own_accounts  # noqa: E402

ACCOUNTS = [
    {"account_name": "NASEEM FASHION", "account_number": "0372073000001202",
     "ifsc": "SIBL0000372"},
    {"account_name": "royal trading company", "account_number": "50200122887492",
     "ifsc": "HDFC0002843"},
]


class TestEveryAccountIsShownWithItsDetails:
    def test_all_three_fields_for_each(self):
        out = render_own_accounts(ACCOUNTS)
        for a in ACCOUNTS:
            assert a["account_name"] in out
            assert a["account_number"] in out
            assert a["ifsc"] in out

    def test_the_layout_matches_a_payment_slot(self):
        """
        Same shape the client's instruction uses, so a vendor checking their
        details against what a client was sent compares like with like.
        """
        out = render_own_accounts(ACCOUNTS)
        assert "Acc num - " in out
        assert "Ifsc - " in out
        assert "Acc name - " in out

    def test_the_count_is_stated_when_there_is_more_than_one(self):
        assert "(2)" in render_own_accounts(ACCOUNTS)

    def test_a_single_account_is_not_counted_at_it(self):
        out = render_own_accounts(ACCOUNTS[:1])
        assert "(1)" not in out
        assert "Your registered accounts" in out

    def test_none_registered_says_how_to_add_one(self):
        """
        An empty list under a heading reads as a fault. A new vendor has no
        accounts yet and this is the first thing they will run.
        """
        out = render_own_accounts([])
        assert "no accounts registered" in out
        assert "/account" in out

    def test_it_says_how_to_change_the_list(self):
        out = render_own_accounts(ACCOUNTS)
        assert "/account" in out and "/account_remove" in out


class TestTheNumberIsCopyable:
    def test_it_is_a_code_span_in_html(self):
        out = render_own_accounts(ACCOUNTS, html=True)
        assert "<code>0372073000001202</code>" in out
        assert "<code>50200122887492</code>" in out

    def test_it_is_bare_in_plain_text(self):
        out = render_own_accounts(ACCOUNTS, html=False)
        assert "<code>" not in out
        assert "0372073000001202" in out

    def test_names_from_user_input_are_escaped(self):
        """
        Account names are typed by the vendor. An unescaped & makes Telegram
        reject the whole message.
        """
        out = render_own_accounts(
            [{"account_name": "Smith & Sons <Pvt>", "account_number": "123456789",
              "ifsc": "HDFC0000001"}],
            html=True,
        )
        assert "Smith &amp; Sons &lt;Pvt&gt;" in out
        # The only literal < in the message are the tags we put there; the
        # vendor's own angle brackets have become &lt; and &gt;.
        assert out.count("<") == out.count("<code>") + out.count("</code>")


class TestTheHandlerIsWiredUp:
    def test_it_exists(self):
        assert hasattr(supplier_bot, "cmd_accounts")

    def test_both_html_switches_are_set(self):
        """
        The 23 September lesson. html=True without parse_mode prints the
        tags; parse_mode without html=True sends markup-free HTML that looks
        fine and simply cannot be copied. Neither raises.
        """
        src = inspect.getsource(supplier_bot.cmd_accounts)
        assert "html=True" in src
        assert 'parse_mode="HTML"' in src

    def test_it_reads_only_the_callers_own_accounts(self):
        """
        The whole safety argument, and it should be visible in the source.
        list_bank_accounts takes a party id; passing anything but the
        caller's own would hand one vendor another's banking details.
        """
        src = inspect.getsource(supplier_bot.cmd_accounts)
        assert "list_bank_accounts(party['id'])" in src.replace('"', "'")

    def test_it_does_not_reach_for_another_partys_list(self):
        src = inspect.getsource(supplier_bot.cmd_accounts)
        for leak in ("suppliers_for_client", "matchable_accounts",
                     "open_trade_accounts", "book_progress"):
            assert leak not in src

    def test_the_empty_case_is_left_to_the_renderer(self):
        """
        One place decides what "no accounts" reads like. A separate early
        return in the handler is a second copy to keep in step.
        """
        src = inspect.getsource(supplier_bot.cmd_accounts)
        assert src.count("message.answer") == 1


class TestItIsDiscoverable:
    def test_it_is_in_the_supplier_menu(self):
        assert "accounts" in {c for c, _ in MENUS["supplier"]}

    def test_it_is_not_added_to_the_bridge_menu(self):
        """
        The Bridge has /wallet and /summary for this. An extra entry on his
        list is noise on the one menu that is already fifteen long.
        """
        assert "accounts" not in {c for c, _ in MENUS["bridge"]}

    def test_the_description_distinguishes_it_from_slash_account(self):
        """
        /account and /accounts differ by one character and sit next to each
        other in the dropdown. The descriptions have to do the work.
        """
        supplier = dict(MENUS["supplier"])
        assert "accounts" in supplier, "/accounts is not in the supplier menu"
        assert "account" in supplier, "/account is not in the supplier menu"
        assert supplier["accounts"] != supplier["account"]
        assert "show" in supplier["accounts"].lower()
        assert "register" in supplier["account"].lower()
