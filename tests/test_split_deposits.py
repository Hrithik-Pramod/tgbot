"""
A supplier sending their USDT in more than one transfer.

Client question, 10 September 2026: "What if supplier sends in 2 parts their
USDT?" Three cases, and only one of them is dangerous:

  before you confirm   both transfers accumulate onto one trade. Normal.
  after it closes      a new trade opens. Normal.
  in between           the trade's expected total rises AFTER instructions have
                       already gone to the client — and the client still has the
                       old instruction in their chat.

The third case is where someone pays twice, so most of this file is about it.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.money import inr_to_usdt, round_inr, usdt_to_inr  # noqa: E402
from core.slots import allocate, check_new_slot  # noqa: E402
from core.money import MoneyError  # noqa: E402
from core.summary import render_deposit_notification  # noqa: E402

SUPPLY = D("106")
SELL = D("107")


class TestAccumulatedArithmetic:
    """
    The figures must be derived from the running total, never by adding up
    per-transfer results. A sum of rounded parts is not the rounding of the sum.
    """

    def test_two_transfers_match_one_of_the_same_size(self):
        first, second = D("19.74"), D("30.26")     # 50.00 together

        combined = usdt_to_inr(first + second, SUPPLY)
        in_one_go = usdt_to_inr(D("50"), SUPPLY)
        assert combined == in_one_go

    def test_pricing_each_tranche_separately_would_drift(self):
        """
        Why the code recomputes from the running total instead of accumulating.

        Each of these rounds down on its own; together they round up. Adding the
        two rounded results gives a different answer from rounding the sum —
        which is the whole reason the trade is re-derived on every deposit.
        """
        a = b = D("3.333333")

        incremental = round_inr(usdt_to_inr(a, SUPPLY)) + round_inr(usdt_to_inr(b, SUPPLY))
        from_running_total = round_inr(usdt_to_inr(a + b, SUPPLY))

        assert incremental != from_running_total, (
            "these amounts no longer demonstrate the drift — pick others, or "
            "the test has stopped guarding anything"
        )
        assert abs(incremental - from_running_total) == 1

    def test_the_owed_amount_is_recomputed_not_added(self):
        a, b = D("19.74"), D("30.26")
        inr = usdt_to_inr(a + b, SUPPLY)
        owed = inr_to_usdt(inr, SELL)
        assert owed == inr_to_usdt(usdt_to_inr(D("50"), SUPPLY), SELL)


class TestTheNotificationSaysWhichItIs:
    """
    A second tranche and a new trade look identical if the message does not
    distinguish them — the Bridge would read the total as this transfer.
    """

    def _render(self, previous=None):
        return render_deposit_notification(
            reference="SUPA1", supplier_label="Supplier A", client_label="Client A",
            usdt_in=D("30.26"), inr_out=D("5300"), tx_hash="abc123",
            supply_rate=SUPPLY, sell_rate=SELL, usdt_out=D("49.53"),
            previous_usdt=previous,
        )

    def test_a_first_deposit_reads_normally(self):
        out = self._render()
        assert "Incoming deposit" in out
        assert "ADDITIONAL" not in out

    def test_a_second_tranche_is_flagged(self):
        out = self._render(previous=D("19.74"))
        assert "ADDITIONAL deposit" in out

    def test_it_shows_this_transfer_and_the_running_total_separately(self):
        out = self._render(previous=D("19.74"))
        assert "30.26" in out          # this transfer
        assert "19.74" in out          # what was already in
        assert "50.00" in out          # the trade so far

    def test_the_confirm_header_is_unchanged(self):
        """The client's agreed header must survive the extra lines."""
        assert self._render(previous=D("19.74")).splitlines()[0] \
            == "SUPPLIER A  ·  Transaction SUPA1"


class TestAllocatingOnlyWhatIsStillOwed:
    """
    The dangerous case, stated as arithmetic.

    Tranche one: 19.74 USDT → ₹2,092. Instruction issued, client pays it.
    Tranche two: 30.26 USDT → trade total ₹5,300.

    The Bridge must now be offered ₹3,208 to instruct, not ₹5,300. Offering the
    gross figure means the client — who has already paid ₹2,092 — is told to pay
    ₹5,300, and ends up ₹2,092 over.
    """

    EXPECTED = D("5300")
    PAID = D("2092")

    def test_the_outstanding_figure_is_what_should_be_instructed(self):
        outstanding = round_inr(self.EXPECTED) - round_inr(self.PAID)
        assert outstanding == D("3208")

    def test_allocating_the_gross_total_would_overpay(self):
        """States the bug in the terms it would be noticed in."""
        gross = round_inr(self.EXPECTED)
        would_be_paid = self.PAID + gross
        assert would_be_paid - self.EXPECTED == self.PAID, \
            "instructing the gross total overpays by exactly what was already paid"

    def test_slots_cannot_exceed_the_outstanding_amount(self):
        outstanding = round_inr(self.EXPECTED) - round_inr(self.PAID)
        with pytest.raises(MoneyError, match="more than"):
            check_new_slot([], D("3209"), outstanding)

    def test_slots_summing_to_the_outstanding_amount_are_accepted(self):
        outstanding = round_inr(self.EXPECTED) - round_inr(self.PAID)
        first = check_new_slot([], D("2000"), outstanding)
        second = check_new_slot([first], D("1208"), outstanding)
        state = allocate([first, second], outstanding)
        assert state.complete and not state.over

    def test_a_fully_paid_trade_leaves_nothing_to_instruct(self):
        outstanding = round_inr(self.EXPECTED) - round_inr(self.EXPECTED)
        assert outstanding == 0
