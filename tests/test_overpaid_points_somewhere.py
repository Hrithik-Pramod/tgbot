"""
An overpaid trade says where the surplus probably belongs.

WHAT HAPPENED (Bridge audit, 1 October 2026)

Two trades closed over on 30 September:

    BRAV9    expected ₹1,440,945   received ₹1,770,506   over ₹329,561
    SUPA38   expected ₹6,390,000   received ₹6,570,439   over ₹180,439

Both produced the notice:

    The trade is closed. The difference needs resolving with the client.

Neither needed resolving with anyone. Every rupee belonged to an order that
was already open, instructed, and collecting into the same bank account. The
Bridge found it two days later by reconciling by hand, and sent back an
allocation in which all six completed trades reconcile to zero variance —
which the bot's did not.

    The allocation logic must prevent a payment for a newly opened trade
    being consumed by the previous open trade merely because the beneficiary
    account is the same.

WHY THE ALLOCATION ITSELF IS NOT CHANGED HERE

Tempting, and wrong. A trade still short SHOULD take the next payment: the
ordinary case is a client working through one instruction before starting
the next, and sending those payments to the newer order would leave the
older one permanently unfinished.

Reconstructed properly, the rule behaved. SUPA38 reached its figure exactly
on the ₹220,082 at 05:54, and the next payment would have gone to SUPA39 on
its own. It only looked wrong because three earlier payments were sitting on
BRAV9 — put there by hand, during the hours when two vendors shared the
royal collection account and every slip was a manual choice.

So the defect was never the ordering. It was that the overpaid notice ended
the conversation. It named a problem and pointed at the wrong person,
and two days passed before anyone looked one trade to the left.

WHAT CHANGED

The notice now lists the vendor's other open trades and what each still
owes. Nothing is moved automatically — the Bridge decides — but the question
"did this belong to the next order?" is now asked at the moment it is
answerable, instead of being reconstructed from a spreadsheet later.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import notifier as notifier_mod  # noqa: E402
from db.repo import Repo  # noqa: E402


class TestTheQueryFindsTheNeighbours:
    def test_it_is_scoped_to_the_same_pairing(self):
        """
        Another vendor's open trade is not where this surplus went — the
        client pays each vendor against their own instruction.
        """
        sql = inspect.getsource(Repo.other_open_trades_for_pairing)
        assert "t.supplier_id = $1" in sql
        assert "t.client_id = $2" in sql

    def test_it_excludes_the_overpaid_trade_itself(self):
        sql = inspect.getsource(Repo.other_open_trades_for_pairing)
        assert "t.id <> $3" in sql

    def test_only_live_trades_count(self):
        """A completed or cancelled trade cannot be where the money belongs."""
        sql = inspect.getsource(Repo.other_open_trades_for_pairing)
        assert "status IN ('open', 'awaiting_payment')" in sql

    def test_it_reports_what_each_still_owes(self):
        """
        The outstanding figure is the whole point — it is what lets the
        Bridge see at a glance whether the surplus fits.
        """
        sql = inspect.getsource(Repo.other_open_trades_for_pairing)
        assert "AS outstanding" in sql
        assert "sum(p.amount_inr)" in sql

    def test_oldest_first(self):
        sql = inspect.getsource(Repo.other_open_trades_for_pairing)
        assert "ORDER BY t.opened_at" in sql


class TestTheNoticeChangesWithTheSituation:
    def _src(self) -> str:
        src = inspect.getsource(notifier_mod)
        start = src.index("if expected is not None and total > expected:")
        return src[start:start + 2600]

    def test_it_asks_for_the_other_open_trades(self):
        assert "other_open_trades_for_pairing" in self._src()

    def test_it_names_them_and_their_outstanding(self):
        block = self._src()
        assert "o['reference']" in block
        assert "o['outstanding']" in block

    def test_it_still_says_chase_the_client_when_nothing_else_is_open(self):
        """
        The original message was right in that case and must survive. A
        genuine overpayment with no other order open IS money owed back.
        """
        block = self._src()
        assert "nothing else is open" in block
        assert "resolving with the client" in block

    def test_it_does_not_move_anything_by_itself(self):
        """
        The notice informs. Reallocating money on a guess is how the two
        days of confusion started — the Bridge decides, with the figures in
        front of him.
        """
        block = self._src()
        for mutating in ("UPDATE", "add_payment", "reallocate", "trade_id ="):
            assert mutating not in block

    def test_the_figures_are_still_stated_plainly(self):
        """
        11 September: a trade expecting ₹212,000 took ₹414,400 and closed
        quietly. The three lines that fixed that stay.
        """
        block = self._src()
        assert "Expected" in block and "Received" in block and "Over by" in block


class TestTheSurplusFitsTheNeighbour:
    """
    The arithmetic the Bridge had to do by hand, as a worked example — so the
    shape of the thing this notice is meant to catch is written down.
    """

    def test_the_two_real_cases(self):
        for expected, received, surplus in (
            (D("1440945"), D("1770506"), D("329561")),   # BRAV9
            (D("6390000"), D("6570439"), D("180439")),   # SUPA38
        ):
            assert received - expected == surplus

    def test_both_surpluses_were_smaller_than_the_next_order(self):
        """
        Which is what made them plausible as misallocation rather than
        overpayment: SUPA39 was open with ₹4.4m still to collect, so a
        surplus of either size fits inside it comfortably.
        """
        next_order_outstanding = D("4402997")
        for surplus in (D("329561"), D("180439")):
            assert surplus < next_order_outstanding
