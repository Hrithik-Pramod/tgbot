"""
Two vendors, one account holder name.

WHAT HAPPENED (13 September 2026)

A fourth supplier was onboarded and registered a bank account in the name
"Girish Kumar Ahirwar" — a name an existing supplier had already been trading
under for days, at a different bank and a different account number.

The Bridge, the same evening:

    please make sure there is no bleed between the vendors, as these are now
    different ones.

WHY THE MATCHING IS ALREADY SAFE

match_account returns None whenever two registered accounts fit the name a
client typed, so nothing is attributed on a coin toss. That part was right
before this file existed and must stay right.

WHY THAT WAS NOT ENOUGH

Refusing to guess hands the question to the client — as a list of buttons
labelled by account name. With two accounts called "Girish Kumar Ahirwar",
the client is asked to choose between two identical buttons. They pick one,
and half the time the money lands against the wrong vendor's trade: exactly
the misattribution the matching refused to commit, arriving through the
question instead.

RESOLVED SEPARATELY, AND WHY THIS STAYS

The Bridge deactivated the older of the two the same evening, so the live
collision is gone. This stays because the collision was not a mistake: real
account holders do appear behind more than one vendor, and the next one will
arrive without anybody noticing in advance. Removing an account is a decision
about who is trading, not a fix for the picker.

THE RULE

A button must identify the account it selects. A name unique among the
candidates stands alone; one that is not carries the last four digits of the
account number — which the client already holds, having been instructed to
pay it, and which says nothing about which supplier is behind it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.client_bot import _account_button  # noqa: E402
from core.parse import match_account  # noqa: E402

GIRISH_B = {"id": 5, "account_name": "Girish Kumar Ahirwar",
            "account_number": "920020006837535"}
GIRISH_D = {"id": 9, "account_name": "Girish Kumar Ahirwar",
            "account_number": "50100898878700"}
WASIM = {"id": 6, "account_name": "Wasim Salim Shaikh",
         "account_number": "500100234567"}
EKTA = {"id": 1, "account_name": "Ekta traders",
        "account_number": "1234567890"}


class TestMatchingStillRefusesToGuess:
    """The first line of defence, unchanged. If this breaks, money moves."""

    def test_a_name_shared_by_two_accounts_matches_neither(self):
        assert match_account("Girish Kumar Ahirwar", [GIRISH_B, GIRISH_D, EKTA]) is None

    def test_a_partial_shared_name_matches_neither(self):
        assert match_account("Girish", [GIRISH_B, GIRISH_D, EKTA]) is None

    def test_an_unambiguous_name_still_matches(self):
        assert match_account("Ekta traders", [GIRISH_B, GIRISH_D, EKTA]) == 1

    def test_an_unambiguous_partial_still_matches(self):
        assert match_account("Wasim", [GIRISH_B, GIRISH_D, WASIM]) == 6


class TestTheButtonsCanBeToldApart:
    def test_a_clashing_name_carries_the_last_four_digits(self):
        among = [GIRISH_B, GIRISH_D, EKTA]
        assert _account_button(GIRISH_B, among) == "Girish Kumar Ahirwar ••7535"
        assert _account_button(GIRISH_D, among) == "Girish Kumar Ahirwar ••8700"

    def test_the_two_buttons_are_actually_different(self):
        """The whole point, stated as the property rather than the format."""
        among = [GIRISH_B, GIRISH_D]
        assert _account_button(GIRISH_B, among) != _account_button(GIRISH_D, among)

    def test_a_unique_name_is_left_alone(self):
        among = [GIRISH_B, GIRISH_D, EKTA]
        assert _account_button(EKTA, among) == "Ekta traders"

    def test_the_comparison_ignores_case_and_padding(self):
        a = {"id": 1, "account_name": "  girish kumar ahirwar ",
             "account_number": "111122223333"}
        b = {"id": 2, "account_name": "Girish Kumar Ahirwar",
             "account_number": "444455556666"}
        assert _account_button(a, [a, b]).endswith("••3333")
        assert _account_button(b, [a, b]).endswith("••6666")

    def test_one_account_on_its_own_is_never_decorated(self):
        assert _account_button(EKTA, [EKTA]) == "Ekta traders"


class TestItRevealsNothingAboutTheSupplier:
    """
    The narrowing that cost us on 11 September: a client must not learn who
    else the Bridge deals with. Disambiguating must not smuggle it back in.
    """

    def test_no_supplier_name_appears(self):
        among = [GIRISH_B, GIRISH_D]
        for account in among:
            label = _account_button(account, among)
            for leaked in ("Supplier", "SUPA", "SUPB", "SUPC", "SUPD",
                           "Feb David", "Girish - Sam"):
                assert leaked not in label

    def test_only_the_last_four_digits_are_shown(self):
        """
        Not the whole number. The client has it already, but a button is a
        different audience from a payment instruction — it sits in the group
        for everyone to scroll past.
        """
        label = _account_button(GIRISH_D, [GIRISH_B, GIRISH_D])
        assert GIRISH_D["account_number"] not in label
        assert label.endswith("8700")
