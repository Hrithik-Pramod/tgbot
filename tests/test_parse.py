"""
Paste parser tests.

Every format here is one the client actually sends, or a near neighbour of one.
The point of this file is that the client changes nothing about how they work.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parse import classify, match_account, parse_payments  # noqa: E402

UTR = "BKIDR12026090800000380"


class TestTheClientsActualFormats:
    """The three variants quoted in the client's message, verbatim."""

    def test_plain(self):
        r = parse_payments(f"{UTR}\n249000\nto Ekta traders")
        assert r.ok
        p = r.payments[0]
        assert p.utr == UTR
        assert p.amount_inr == D("249000")
        assert p.beneficiary == "Ekta traders"

    def test_amount_with_a_space(self):
        # "some of the team use spaces"
        r = parse_payments(f"{UTR}\n249 000\nto Ekta traders")
        assert r.ok
        assert r.payments[0].amount_inr == D("249000")

    def test_with_labels(self):
        r = parse_payments(
            f"UTR {UTR}\nAmount 249000\nto Ekta traders")
        assert r.ok
        assert r.payments[0].utr == UTR
        assert r.payments[0].amount_inr == D("249000")
        assert r.payments[0].beneficiary == "Ekta traders"


class TestOrderDoesNotMatter:
    """"sometimes the order is different" — all six permutations must work."""

    @pytest.mark.parametrize("order", [
        ("utr", "amt", "ben"), ("utr", "ben", "amt"),
        ("amt", "utr", "ben"), ("amt", "ben", "utr"),
        ("ben", "utr", "amt"), ("ben", "amt", "utr"),
    ])
    def test_every_permutation(self, order):
        parts = {"utr": UTR, "amt": "249000", "ben": "to Ekta traders"}
        r = parse_payments("\n".join(parts[k] for k in order))
        assert r.ok, r.problems
        p = r.payments[0]
        assert p.utr == UTR
        assert p.amount_inr == D("249000")
        assert p.beneficiary == "Ekta traders"


class TestAmountFormatting:
    @pytest.mark.parametrize("raw,expected", [
        ("249000", D("249000")),
        ("249 000", D("249000")),
        ("249,000", D("249000")),
        ("2,49,000", D("249000")),        # Indian grouping
        ("₹249000", D("249000")),
        ("Rs 249000", D("249000")),
        ("Rs. 249 000", D("249000")),
        ("INR 249000", D("249000")),
        ("Amount 249000", D("249000")),
        ("249000.00", D("249000")),
        ("  249000  ", D("249000")),
    ])
    def test_variants(self, raw, expected):
        r = parse_payments(f"{UTR}\n{raw}\nto Ekta traders")
        assert r.ok, r.problems
        assert r.payments[0].amount_inr == expected


class TestUtrFormatting:
    @pytest.mark.parametrize("raw", [
        UTR,
        f"UTR {UTR}",
        f"utr: {UTR}",
        "BKIDR1 2026 0908 00000380",       # spaced, as pasted from some apps
        f"Ref {UTR}",
        f"RRN {UTR}",
    ])
    def test_variants(self, raw):
        r = parse_payments(f"{raw}\n249000\nto Ekta traders")
        assert r.ok, r.problems
        assert r.payments[0].utr == UTR


class TestTheAmbiguousCase:
    """
    A bank whose UTRs are all digits. Length separates them: an Indian UTR is
    12+ characters, and a tranche is not 10^12 rupees.
    """

    def test_long_digit_string_is_a_utr(self):
        assert classify("123456789012345")[0] == "utr"

    def test_short_digit_string_is_an_amount(self):
        assert classify("249000")[0] == "amount"

    def test_numeric_utr_parses_when_long(self):
        r = parse_payments("123456789012345678\n249000\nto Ekta traders")
        assert r.ok, r.problems
        assert r.payments[0].utr == "123456789012345678"
        assert r.payments[0].amount_inr == D("249000")

    def test_a_label_overrides_the_length_rule(self):
        """
        The escape hatch. If a bank ever issues short numeric UTRs, asking the
        client to prefix "UTR" resolves it with no code change.
        """
        r = parse_payments("UTR 12345678\nAmount 249000\nto Ekta traders")
        assert r.ok, r.problems
        assert r.payments[0].utr == "12345678"
        assert r.payments[0].amount_inr == D("249000")


class TestMultiplePayments:
    def test_six_pasted_at_once(self):
        block = "\n".join(
            f"BKIDR1202609080000{i:04d}\n{amt}\nto Ekta traders"
            for i, amt in enumerate(
                ["229000", "230000", "246000", "263000", "291000", "240297"], 1)
        )
        r = parse_payments(block)
        assert r.ok, r.problems
        assert len(r.payments) == 6
        assert sum(p.amount_inr for p in r.payments) == D("1499297")

    def test_blank_lines_between_blocks(self):
        r = parse_payments(
            f"{UTR}\n249000\nto Ekta traders\n\n"
            "BKIDR12026090800000381\n251000\nto Ekta traders")
        assert r.ok, r.problems
        assert len(r.payments) == 2

    def test_different_beneficiaries(self):
        r = parse_payments(
            f"{UTR}\n249000\nto Ekta traders\n"
            "BKIDR12026090800000381\n251000\nto Alpha Traders")
        assert len(r.payments) == 2
        assert r.payments[0].beneficiary == "Ekta traders"
        assert r.payments[1].beneficiary == "Alpha Traders"


class TestScreenshotNoise:
    """Pasting alongside a screenshot brings surrounding text with it."""

    def test_status_words_ignored(self):
        r = parse_payments(
            f"Payment Successful\n{UTR}\n249000\nto Ekta traders\n11:04 am")
        assert r.ok, r.problems
        assert len(r.payments) == 1

    def test_rail_names_ignored(self):
        r = parse_payments(f"IMPS\n{UTR}\n249000\nto Ekta traders")
        assert r.ok, r.problems
        assert r.payments[0].amount_inr == D("249000")

    def test_dates_ignored(self):
        r = parse_payments(f"08/09/2026\n{UTR}\n249000\nto Ekta traders")
        assert r.ok, r.problems
        assert len(r.payments) == 1


class TestBeneficiaryOptional:
    def test_missing_beneficiary_still_parses(self):
        """The bot asks for it rather than refusing the whole paste."""
        r = parse_payments(f"{UTR}\n249000")
        assert r.ok, r.problems
        assert r.payments[0].beneficiary is None

    def test_bare_name_without_to(self):
        r = parse_payments(f"{UTR}\n249000\nEkta traders")
        assert r.ok, r.problems
        assert r.payments[0].beneficiary == "Ekta traders"


class TestRefusals:
    """Where it cannot be sure, it must say so rather than guess."""

    def test_empty(self):
        assert not parse_payments("").ok

    def test_no_amount(self):
        r = parse_payments(UTR)
        assert not r.ok
        assert "no amount" in r.problems[0].lower()

    def test_no_utr(self):
        r = parse_payments("249000\nto Ekta traders")
        assert not r.ok
        assert "no utr" in r.problems[0].lower()

    def test_prose_is_not_a_payment(self):
        assert not parse_payments("sent the money mate").ok

    def test_zero_amount_refused(self):
        r = parse_payments(f"{UTR}\n0\nto Ekta traders")
        assert not r.ok

    def test_one_bad_entry_does_not_lose_the_good_ones(self):
        r = parse_payments(
            f"{UTR}\n249000\nto Ekta traders\n"
            "BKIDR12026090800000381")          # missing its amount
        assert len(r.payments) == 1
        assert r.problems, "the incomplete entry must be reported"


class TestAccountMatching:
    ACCOUNTS = [
        {"id": 1, "account_name": "Ekta Traders"},
        {"id": 2, "account_name": "Alpha Traders Pvt Ltd"},
    ]

    @pytest.mark.parametrize("name,expected", [
        ("Ekta Traders", 1),
        ("ekta traders", 1),
        ("EKTA TRADERS", 1),
        ("Ekta  Traders", 1),
        ("Ekta", 1),                       # partial, unambiguous
        ("Alpha Traders Pvt Ltd", 2),
    ])
    def test_matches(self, name, expected):
        assert match_account(name, self.ACCOUNTS) == expected

    def test_unknown_name_returns_none(self):
        assert match_account("Someone Else", self.ACCOUNTS) is None

    def test_missing_name_returns_none(self):
        assert match_account(None, self.ACCOUNTS) is None

    def test_ambiguous_partial_returns_none(self):
        """
        "Traders" matches both. Guessing would attribute money to the wrong
        account, so the caller is made to ask.
        """
        assert match_account("Traders", self.ACCOUNTS) is None
