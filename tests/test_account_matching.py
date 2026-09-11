"""
Matching a pasted account name to a registered account.

Client request, 11 September 2026: "we need word match ... does not need to be
full word match, only first of the word Super etc". A payment came through as
"to ?  (account not recognised)" and had to be resolved by tapping a button.

The tension here is the whole point. Matching too loosely puts money against
the wrong account — and since 11 September, against the wrong TRADE as well.
Matching too tightly makes the client answer a question for every payment,
which is the friction they asked to be rid of in the first place.

The rule that resolves it: be generous about how a name is written, strict
about ambiguity. A candidate only wins if it is the only one that fits.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parse import classify, match_account, parse_payments  # noqa: E402

# The live accounts, as registered on 11 September 2026.
ACCOUNTS = [
    {"id": 1, "account_name": "Ekta traders"},
    {"id": 2, "account_name": "SUPER TRADING COMPANY (STC)"},
    {"id": 3, "account_name": "Bhagavati  trading co"},
    {"id": 4, "account_name": "Wasim Salim Shaikh"},
]


class TestHowPeopleActuallyWriteNames:
    @pytest.mark.parametrize("written", [
        "SUPER TRADING COMPANY (STC)",     # exactly
        "super trading company stc",       # case and punctuation
        "SUPER TRADING COMPANY",           # trailing part dropped
        "Super Trading",                   # first two words
        "Super",                           # first word only
        "SUPER TRAD",                      # truncated mid-word
        "STC",                             # the bracketed short form
        "Super Trading Company Pvt",       # extra words neither side contains
    ])
    def test_they_all_find_the_same_account(self, written):
        assert match_account(written, ACCOUNTS) == 2, f"{written!r} did not match"

    @pytest.mark.parametrize("written,expected", [
        ("Ekta traders", 1),
        ("ekta", 1),
        ("Ekta Traders Pvt Ltd", 1),
        ("Bhagavati", 3),
        ("bhagavati trading", 3),
        ("Wasim", 4),
        ("Wasim Salim Shaikh", 4),
    ])
    def test_the_other_accounts_match_too(self, written, expected):
        assert match_account(written, ACCOUNTS) == expected


class TestItStillRefusesToGuess:
    """
    Every case here must return None so the client is asked. A wrong match is
    money on the wrong account and the wrong trade.
    """

    def test_an_unknown_name(self):
        assert match_account("Northgate Exports", ACCOUNTS) is None

    def test_nothing_at_all(self):
        assert match_account(None, ACCOUNTS) is None
        assert match_account("", ACCOUNTS) is None

    def test_two_accounts_sharing_an_opening_word(self):
        """
        The case the leniency could break. "trading" appears in two names, so
        it must not resolve to either.
        """
        assert match_account("trading", ACCOUNTS) is None

    def test_two_accounts_with_the_same_first_word(self):
        ambiguous = [
            {"id": 10, "account_name": "Super Trading Company"},
            {"id": 11, "account_name": "Super Exports Ltd"},
        ]
        assert match_account("Super", ambiguous) is None

    def test_a_single_letter_is_not_a_match(self):
        assert match_account("S", ACCOUNTS) is None

    def test_a_two_letter_fragment_is_not_a_match(self):
        """Too short to be meant as a name. Ask rather than pick."""
        assert match_account("Su", ACCOUNTS) is None


class TestAPasteWithoutTheWordTo:
    """
    The reported case: "because it didn't start with 'to'".

    The account line is written bare, with no "to" in front of it. It must
    still be read as the beneficiary — people do not type a keyword before a
    name when they are pasting from a banking app.
    """

    def test_a_bare_name_is_classified_as_a_beneficiary(self):
        kind, _ = classify("SUPER TRADING COMPANY (STC)")
        assert kind == "beneficiary"

    def test_a_bare_first_word_is_classified_as_a_beneficiary(self):
        kind, _ = classify("Super")
        assert kind == "beneficiary"

    def test_the_whole_paste_reads_without_to(self):
        r = parse_payments(
            "BKIDR12026091100005720\n261000\nSUPER TRADING COMPANY (STC)"
        )
        assert r.payments, r.problems
        p = r.payments[0]
        assert p.utr == "BKIDR12026091100005720"
        assert str(p.amount_inr) == "261000"
        assert match_account(p.beneficiary, ACCOUNTS) == 2

    def test_the_whole_paste_reads_with_to(self):
        """The form that already worked must keep working."""
        r = parse_payments(
            "BKIDR12026091100005720\n261000\nto SUPER TRADING COMPANY (STC)"
        )
        assert r.payments, r.problems
        assert match_account(r.payments[0].beneficiary, ACCOUNTS) == 2

    def test_a_short_bare_name_reads_too(self):
        r = parse_payments("BKIDR12026091100005720\n261000\nSuper")
        assert r.payments, r.problems
        assert match_account(r.payments[0].beneficiary, ACCOUNTS) == 2
