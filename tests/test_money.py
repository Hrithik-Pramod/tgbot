"""
Tests for the calculation engine.

The important tests here are the ones that reproduce the client's own numbers.
If these pass, the arithmetic matches trades they have already settled.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.money import (  # noqa: E402
    MoneyError,
    build_sum_line,
    check_total,
    fmt_inr,
    fmt_usdt,
    inr_to_usdt,
    margin_usdt,
    normalise_utr,
    round_inr,
    round_usdt,
    to_decimal,
    usdt_to_inr,
)
from core.summary import (  # noqa: E402
    Payment,
    PaymentSlot,
    render_completion_notice,
    render_payment_slot,
    render_trade_summary,
)

D = Decimal


# ---------------------------------------------------------------- client data

# The six tranches from the real trade the client supplied.
REAL_TRADE = [
    ("EXBKR12026090700002248", D("229000"), "Alpha Traders"),
    ("EXBKR12026090700002326", D("230000"), "Alpha Traders"),
    ("EXBKR12026090700000845", D("246000"), "Alpha Traders Pvt Ltd"),
    ("EXBKR12026090700002570", D("263000"), "Alpha Traders Pvt Ltd"),
    ("EXBKR12026090700002625", D("291000"), "Alpha Traders Pvt Ltd"),
    ("EXBKR12026090700001317", D("240297"), "Alpha Traders Pvt Ltd"),
]
REAL_TOTAL = D("1499297")


class TestClientRealTrade:
    """These must match the client's figures exactly."""

    def test_tranches_sum_to_the_clients_total(self):
        assert sum(a for _, a, _ in REAL_TRADE) == REAL_TOTAL

    def test_sum_line_matches_client_format_exactly(self):
        line = build_sum_line(a for _, a, _ in REAL_TRADE)
        assert line == (
            "229000+230000+246000+263000+291000+240297 = 1,499,297"
        )

    def test_full_summary_renders_in_the_clients_layout(self):
        payments = [Payment(u, a, b) for u, a, b in REAL_TRADE]
        out = render_trade_summary(payments, include_header=False)
        expected = (
            "EXBKR12026090700002248\n229000\nto Alpha Traders\n"
            "EXBKR12026090700002326\n230000\nto Alpha Traders\n"
            "EXBKR12026090700000845\n246000\nto Alpha Traders Pvt Ltd\n"
            "EXBKR12026090700002570\n263000\nto Alpha Traders Pvt Ltd\n"
            "EXBKR12026090700002625\n291000\nto Alpha Traders Pvt Ltd\n"
            "EXBKR12026090700001317\n240297\nto Alpha Traders Pvt Ltd\n"
            "\n"
            "229000+230000+246000+263000+291000+240297 = 1,499,297"
        )
        assert out == expected

    def test_completion_notice(self):
        assert render_completion_notice(REAL_TOTAL) == (
            "This order is completed — ₹1,499,297 sent in full."
        )

    def test_payment_slot_layout(self):
        slot = PaymentSlot(
            account_name="Alpha Traders Pvt Ltd",
            account_number="123456789012345",
            ifsc="EXBK0001234",
            amount_inr=REAL_TOTAL,
        )
        assert render_payment_slot(slot) == (
            "New slot\n"
            "Acc num - 123456789012345\n"
            "Ifsc - EXBK0001234\n"
            "Acc name - Alpha Traders Pvt Ltd\n"
            "1499297"
        )


class TestWorkedExample:
    """The 1,000 USDT example, with the sell rate the client's answer implies."""

    def test_supply_side(self):
        # 1000 USDT at rate 100 -> 100,000 INR
        assert usdt_to_inr("1000", "100") == D("100000")

    def test_sell_side_gives_the_clients_figure(self):
        # The client wrote 900.9009009 -> 900.90, which is 100000 / 111.
        assert inr_to_usdt("100000", "111") == D("900.90")

    def test_sell_rate_111_11_would_give_a_different_answer(self):
        # Guards the correction: 111.11 is NOT the rate in the example.
        assert inr_to_usdt("100000", "111.11") == D("900.01")

    def test_margin_sits_in_usdt(self):
        # A3: margin stays as USDT in the internal wallet.
        assert margin_usdt("1000", "100", "111") == D("99.10")


class TestRounding:
    def test_usdt_two_places_half_up(self):
        assert round_usdt("900.9009009") == D("900.90")
        assert round_usdt("900.905") == D("900.91")
        assert round_usdt("900.904") == D("900.90")

    def test_inr_whole_rupees_half_up(self):
        assert round_inr("240296.5") == D("240297")
        assert round_inr("240296.4") == D("240296")

    def test_floats_are_rejected(self):
        # Accepting a float would import binary rounding error into the ledger.
        with pytest.raises(MoneyError):
            to_decimal(0.1)

    def test_repeated_division_does_not_drift(self):
        total = sum(inr_to_usdt("100000", "111") for _ in range(100))
        assert total == D("90090.00")


class TestInputCleaning:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1,499,297", D("1499297")),
            ("1 499 297", D("1499297")),
            ("₹229000", D("229000")),
            ("  240297  ", D("240297")),
            ("Rs 230000", D("230000")),
            ("246000 INR", D("246000")),
        ],
    )
    def test_pasted_amounts_are_cleaned(self, raw, expected):
        # Clients paste straight out of banking apps; formatting is arbitrary.
        assert to_decimal(raw) == expected

    def test_empty_amount_rejected(self):
        with pytest.raises(MoneyError):
            to_decimal("")


class TestUTR:
    def test_spaces_stripped_as_the_brief_requires(self):
        assert normalise_utr("EXBKR1 2026 0907 0000 2248") == (
            "EXBKR12026090700002248"
        )

    def test_case_and_separators_normalised(self):
        assert normalise_utr("exbkr1-2026_0907 00002248") == (
            "EXBKR12026090700002248"
        )

    def test_same_utr_pasted_two_ways_collides(self):
        # This is what makes duplicate detection (E3) actually work.
        a = normalise_utr("EXBKR1 2026090700002248")
        b = normalise_utr("exbkr12026090700002248")
        assert a == b

    def test_client_utrs_all_valid(self):
        for utr, _, _ in REAL_TRADE:
            assert normalise_utr(utr) == utr

    @pytest.mark.parametrize("bad", ["", "   ", "ABC", "UTR#12345678!", "-" * 10])
    def test_rejects_junk(self, bad):
        with pytest.raises(MoneyError):
            normalise_utr(bad)

    def test_accepts_other_bank_formats(self):
        # Deliberately loose: hard-coding Bank of India's pattern would reject
        # valid UTRs from every other bank.
        assert normalise_utr("SBIN0000123456789") == "SBIN0000123456789"
        assert normalise_utr("123456789012") == "123456789012"


class TestTotalCheck:
    """
    No tolerance band (client decision, 8 Sep 2026). Exact figures only: any
    difference at all is reported and the bot never decides a shortfall is
    acceptable.
    """

    def test_exact_payment(self):
        r = check_total(REAL_TOTAL, REAL_TOTAL)
        assert r.exact and r.difference == 0

    def test_one_rupee_short_is_still_reported(self):
        # Under the old tolerance rule this was absorbed silently. It is not now.
        r = check_total(REAL_TOTAL - D("1"), REAL_TOTAL)
        assert not r.exact
        assert r.difference == D("-1")

    def test_shortfall(self):
        r = check_total(D("1499200"), REAL_TOTAL)
        assert r.difference == D("-97")

    def test_overpayment(self):
        r = check_total(D("1499397"), REAL_TOTAL)
        assert r.difference == D("100")


class TestSummaryReconciliation:
    def test_shortfall_is_stated_plainly(self):
        payments = [Payment(u, a, b) for u, a, b in REAL_TRADE[:5]]
        out = render_trade_summary(
            payments, expected_inr=REAL_TOTAL, include_header=False)
        assert "Short by ₹240,297 against expected ₹1,499,297" in out
        assert "tolerance" not in out.lower()

    def test_overpayment_is_stated_plainly(self):
        payments = [Payment("UTR12345678", D("1499397"), "Alpha Traders")]
        out = render_trade_summary(
            payments, expected_inr=REAL_TOTAL, include_header=False)
        assert "Over by ₹100" in out

    def test_exact_total_adds_no_note(self):
        payments = [Payment(u, a, b) for u, a, b in REAL_TRADE]
        out = render_trade_summary(
            payments, expected_inr=REAL_TOTAL, include_header=False)
        assert "Short by" not in out and "Over by" not in out

    def test_a_single_rupee_short_is_still_shown(self):
        payments = [Payment("UTR12345678", REAL_TOTAL - D("1"), "Alpha Traders")]
        out = render_trade_summary(
            payments, expected_inr=REAL_TOTAL, include_header=False)
        assert "Short by ₹1" in out

    def test_reference_header(self):
        payments = [Payment(u, a, b) for u, a, b in REAL_TRADE]
        out = render_trade_summary(payments, reference="SUPA1")
        assert out.startswith("Transaction SUPA1\n")

    def test_empty_summary_rejected(self):
        with pytest.raises(ValueError):
            render_trade_summary([])


class TestFormatting:
    def test_western_grouping_per_answer_f1(self):
        assert fmt_inr("1499297") == "1,499,297"
        assert fmt_inr("100000") == "100,000"

    def test_usdt_two_places(self):
        assert fmt_usdt("900.9") == "900.90"
        assert fmt_usdt("1000") == "1,000.00"


class TestGuards:
    @pytest.mark.parametrize("bad_rate", ["0", "-5"])
    def test_non_positive_rates_rejected(self, bad_rate):
        with pytest.raises(MoneyError):
            usdt_to_inr("1000", bad_rate)
        with pytest.raises(MoneyError):
            inr_to_usdt("100000", bad_rate)

    def test_non_positive_amounts_rejected(self):
        with pytest.raises(MoneyError):
            usdt_to_inr("0", "100")
        with pytest.raises(MoneyError):
            inr_to_usdt("-1", "111")


class TestDepositNotificationHeader:
    """
    Client request, 8 September 2026: the header must name the supplier and the
    internal wallet address, "to ensure no manual errors my side".
    """

    def _render(self, **kw):
        from core.summary import render_deposit_notification
        base = dict(
            reference="SUPA1", supplier_label="Supplier A",
            client_label="Client A", usdt_in=D("1000"),
            inr_out=D("105500"), tx_hash="3f2a91c4e8b7d05a",
            wallet_address="TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb",
        )
        base.update(kw)
        return render_deposit_notification(**base)

    def test_supplier_is_named_on_the_first_line(self):
        first = self._render().splitlines()[0]
        assert "SUPPLIER A" in first
        assert "SUPA1" in first

    def test_wallet_address_appears_in_the_header(self):
        lines = self._render().splitlines()
        assert lines[1] == "Internal wallet TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb"

    def test_address_is_never_truncated(self):
        """
        Abbreviating defeats the purpose: two different addresses can share
        their first and last characters.
        """
        out = self._render()
        assert "TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb" in out
        assert "…" not in out and "..." not in out

    def test_supplier_b_header_is_distinct(self):
        a = self._render(supplier_label="Supplier A")
        b = self._render(supplier_label="Supplier B",
                         wallet_address="TJJb8jUTcrdtq57YrAcyEWhTkECd6dbRbo")
        assert a.splitlines()[0] != b.splitlines()[0]
        assert a.splitlines()[1] != b.splitlines()[1]

    def test_amounts_and_hash_still_present(self):
        out = self._render()
        assert "USDT = 1,000.00 to Send INR 105,500" in out
        assert "Hash 3f2a91c4e8b7d05a" in out

    def test_works_without_an_address(self):
        out = self._render(wallet_address=None)
        assert "Internal wallet" not in out
        assert "SUPPLIER A" in out
