"""
Slot allocation and the client-facing payment instruction.

A representative trade split ₹1,499,297 across two accounts, so these tests
work in those figures.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.money import MoneyError  # noqa: E402
from core.slots import allocate, check_new_slot  # noqa: E402
from core.summary import PaymentSlot, render_client_confirmation, render_payment_slot  # noqa: E402

D = Decimal
TOTAL = D("1499297")


class TestAllocation:
    def test_nothing_allocated_yet(self):
        a = allocate([], TOTAL)
        assert a.allocated == 0
        assert a.remaining == TOTAL
        assert not a.complete and not a.over

    def test_single_full_slot_completes(self):
        a = allocate([TOTAL], TOTAL)
        assert a.complete and not a.over
        assert a.remaining == 0

    def test_two_accounts_split_exactly(self):
        # 459,000 to Alpha Traders; the rest to Alpha Traders Pvt Ltd
        a = allocate([D("459000"), D("1040297")], TOTAL)
        assert a.allocated == TOTAL
        assert a.complete

    def test_partial_allocation_reports_remainder(self):
        a = allocate([D("459000")], TOTAL)
        assert a.remaining == D("1040297")
        assert not a.complete

    def test_over_allocation_detected(self):
        a = allocate([TOTAL, D("1")], TOTAL)
        assert a.over
        assert a.remaining == D("-1")

    def test_six_tranche_split_sums_exactly(self):
        parts = [D("229000"), D("230000"), D("246000"),
                 D("263000"), D("291000"), D("240297")]
        a = allocate(parts, TOTAL)
        assert a.complete, "the six amounts must allocate exactly"


class TestNewSlotValidation:
    def test_accepts_amount_within_remaining(self):
        assert check_new_slot([], D("459000"), TOTAL) == D("459000")

    def test_accepts_exact_remainder(self):
        assert check_new_slot([D("459000")], D("1040297"), TOTAL) == D("1040297")

    def test_rejects_over_allocation(self):
        with pytest.raises(MoneyError, match="more than"):
            check_new_slot([D("459000")], D("1040298"), TOTAL)

    def test_rejects_zero_and_negative(self):
        for bad in (D("0"), D("-1")):
            with pytest.raises(MoneyError):
                check_new_slot([], bad, TOTAL)

    def test_rejects_when_already_fully_allocated(self):
        with pytest.raises(MoneyError):
            check_new_slot([TOTAL], D("1"), TOTAL)

    def test_accepts_pasted_formatting(self):
        # The Bridge pastes from the same places the clients do.
        assert check_new_slot([], "1,499,297", TOTAL) == TOTAL
        assert check_new_slot([], "₹459 000", TOTAL) == D("459000")


class TestPaymentInstruction:
    def test_single_slot_layout(self):
        slot = PaymentSlot("Alpha Traders Pvt Ltd", "123456789012345",
                           "EXBK0001234", TOTAL)
        assert render_payment_slot(slot) == (
            "New slot\n"
            "Acc num - 123456789012345\n"
            "Ifsc - EXBK0001234\n"
            "Acc name - Alpha Traders Pvt Ltd\n"
            "1499297"
        )

    def test_multi_slot_confirmation(self):
        slots = [
            PaymentSlot("Alpha Traders", "123456789012345", "EXBK0001234", D("459000")),
            PaymentSlot("Alpha Traders Pvt Ltd", "123456789012346", "EXBK0001234", D("1040297")),
        ]
        out = render_client_confirmation(
            client_label="Client A",
            usdt_out=D("13507.18"),
            inr_amount=TOTAL,
            slots=slots,
        )
        assert out.startswith("Confirm amount and details to send to Client A")
        assert "USDT = 13507.18 to Send INR 1,499,297" in out
        assert out.count("New slot") == 2
        assert "459000" in out and "1040297" in out
        # Labels only between counterparties (answer D4) - no real identity leaks.
        assert "Supplier" not in out

    def test_confirmation_has_no_trailing_blank(self):
        slots = [PaymentSlot("Alpha Traders", "1", "EXBK0001234", TOTAL)]
        out = render_client_confirmation(
            client_label="Client A", usdt_out=D("1"), inr_amount=TOTAL, slots=slots
        )
        assert out == out.rstrip()


class TestDealPrefix:
    """
    Regression guard for a bug the pure-logic tests could not see: the prefix
    was derived as label.replace(" ","").upper(), turning "Supplier A" into
    "SUPPLIERA" and every reference into SUPPLIERA1 instead of SUPA1.
    """

    @pytest.mark.parametrize("label,expected", [
        ("Supplier A", "SUPA"),
        ("Supplier B", "SUPB"),
        ("supplier a", "SUPA"),
        ("  Supplier  C  ", "SUPC"),
        ("Supplier 1", "SUP1"),
    ])
    def test_supplier_labels(self, label, expected):
        from db.repo import derive_prefix
        assert derive_prefix(label) == expected

    def test_falls_back_for_other_labels(self):
        from db.repo import derive_prefix
        assert derive_prefix("Northgate") == "NORT"
        assert derive_prefix("AB group") == "ABGR"

    def test_rejects_a_label_with_nothing_usable(self):
        from db.repo import derive_prefix
        with pytest.raises(ValueError):
            derive_prefix("---")


class TestNoCommasInUsdt:
    """
    Client request, 8 September 2026: "on amount to send to client in USDT, i
    dont want any commas". That figure is pasted into a wallet, where a comma
    is at best rejected and at worst silently truncated. INR keeps its
    separators — it is read, not pasted.
    """

    def _out(self, usdt, **kw):
        return render_client_confirmation(
            client_label="Client A", usdt_out=usdt, inr_amount=TOTAL,
            slots=[PaymentSlot("Ekta Traders", "123456789012345",
                               "EXBK0001234", TOTAL)],
            **kw)

    def test_thousands_have_no_comma(self):
        assert "USDT = 13507.18" in self._out(D("13507.18"))

    def test_millions_have_no_comma(self):
        out = self._out(D("1234567.89"))
        assert "1234567.89" in out
        assert "1,234,567" not in out

    def test_always_two_decimals(self):
        assert "USDT = 990.60" in self._out(D("990.6"))
        assert "USDT = 1000.00" in self._out(D("1000"))

    def test_inr_keeps_its_separators(self):
        assert "INR 1,499,297" in self._out(D("13507.18"))

    def test_html_wraps_the_usdt_for_tap_to_copy(self):
        out = self._out(D("13507.18"), html=True)
        assert "<code>13507.18</code>" in out

    def test_html_wraps_the_account_number_too(self):
        # Transcribed into a banking app — the costliest place for a typo.
        assert "<code>123456789012345</code>" in self._out(D("1"), html=True)

    def test_plain_mode_has_no_markup(self):
        out = self._out(D("13507.18"))
        assert "<code>" not in out

    def test_user_content_is_escaped_in_html_mode(self):
        """An unescaped & in an account name would break the whole message."""
        out = render_client_confirmation(
            client_label="A & B", usdt_out=D("1"), inr_amount=TOTAL,
            slots=[PaymentSlot("R&D <Traders>", "1", "EXBK0001234", TOTAL)],
            html=True)
        assert "A &amp; B" in out
        assert "R&amp;D &lt;Traders&gt;" in out
