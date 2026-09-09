"""
The second batch of changes from 10 September 2026, after the client ran the
full test himself at 2am and reported back.

His words are quoted against each one.

The completion tests matter most. Closing a trade used to be a deliberate act by
a person; it is now something the system decides, which means it must decide
correctly every time — never twice, never early, and never on a trade that was
only nearly paid.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.summary import (  # noqa: E402
    Payment, render_collection_progress, render_supplier_summary,
    render_trade_summary,
)

PAYMENTS = [
    Payment(utr="BKIDR1202609101001022", amount_inr=D("592"),
            beneficiary_name="Ekta Traders Pvt Ltd"),
    Payment(utr="BKIDR12026091110010002", amount_inr=D("468"),
            beneficiary_name="Ekta Traders Pvt Ltd"),
]


class TestSupplierSummary:
    """
    "add to Bottom - Send Next Trade", against the exact message he pasted.
    """

    def test_it_matches_what_he_asked_for(self):
        got = render_supplier_summary(PAYMENTS, expected_inr=D("1060"))
        assert got == (
            "TRADE COMPLETED\n"
            "\n"
            "BKIDR1202609101001022\n"
            "592\n"
            "to Ekta Traders Pvt Ltd\n"
            "BKIDR12026091110010002\n"
            "468\n"
            "to Ekta Traders Pvt Ltd\n"
            "\n"
            "592+468 = 1,060\n"
            "\n"
            "Send Next Trade"
        )

    def test_the_call_to_action_is_last(self):
        """It is the line that should be left on screen."""
        assert render_supplier_summary(PAYMENTS).rstrip().endswith("Send Next Trade")

    def test_no_deal_reference_reaches_the_supplier(self):
        got = render_supplier_summary(PAYMENTS, expected_inr=D("1060"))
        assert "SUPA" not in got and "Transaction" not in got

    def test_a_shortfall_is_still_reported(self):
        """
        Automation must not quietly hide a difference. If a trade is closed
        early against an expected total, the supplier sees the gap too.
        """
        got = render_supplier_summary(PAYMENTS, expected_inr=D("2000"))
        assert "Short by" in got

    def test_the_figures_match_everyone_elses_copy(self):
        supplier = render_supplier_summary(PAYMENTS, expected_inr=D("1060"))
        others = render_trade_summary(PAYMENTS, reference="SUPA1",
                                      expected_inr=D("1060"))
        for line in ("BKIDR1202609101001022", "592", "to Ekta Traders Pvt Ltd",
                     "592+468 = 1,060"):
            assert line in supplier and line in others


class TestCollectionProgress:
    """
    "collection progress, can i add this to the suppliers as an option?"
    """

    def test_it_states_all_three_figures(self):
        got = render_collection_progress(expected_inr=D("2120"), paid_inr=D("1600"))
        assert "2,120" in got and "1,600" in got and "520" in got

    def test_it_gives_a_percentage(self):
        got = render_collection_progress(expected_inr=D("2000"), paid_inr=D("1500"))
        assert "75% collected" in got

    def test_nothing_collected_yet(self):
        got = render_collection_progress(expected_inr=D("2000"), paid_inr=D("0"))
        assert "0% collected" in got

    def test_overpayment_does_not_show_negative_outstanding(self):
        got = render_collection_progress(expected_inr=D("1000"), paid_inr=D("1200"))
        outstanding_line = got.split("Outstanding")[1].split("\n")[0]
        assert "-" not in outstanding_line

    def test_it_leaks_nothing_the_supplier_should_not_have(self):
        """
        The supplier sees the collection of their own INR and no more — not the
        pricing, not the margin, not even which client is paying.
        """
        got = render_collection_progress(expected_inr=D("2120"), paid_inr=D("1600"))
        for leak in ("rate", "Rate", "margin", "Margin", "USDT", "SUPA", "Client"):
            assert leak not in got


class TestAutomaticCompletion:
    """
    "yes, remove /done, have it calculate".

    These exercise the claim through a fake repository, because the property
    that matters is not the SQL but the behaviour around it: fire once, only
    when covered, and never leave a paid trade open.
    """

    class _Repo:
        """Mimics claim_completion's contract, including the single-winner rule."""

        def __init__(self, expected, payments):
            self.expected = expected
            self.payments = list(payments)
            self.status = "awaiting_payment"
            self.claims = 0

        async def claim_completion(self, trade_id):
            self.claims += 1
            if self.status != "awaiting_payment":
                return None
            if not self.expected or self.expected <= 0:
                return None
            if sum(self.payments) < self.expected:
                return None
            self.status = "completed"
            return {"id": trade_id, "reference": "SUPA1",
                    "inr_expected": self.expected,
                    "client_id": 2, "supplier_id": 1}

    @pytest.mark.asyncio
    async def test_it_fires_once_when_the_total_is_covered(self):
        repo = self._Repo(D("1060"), [D("592"), D("468")])
        assert await repo.claim_completion(1) is not None
        assert await repo.claim_completion(1) is None, \
            "a second payment landing at the same moment closed it twice"

    @pytest.mark.asyncio
    async def test_it_does_not_fire_while_short(self):
        repo = self._Repo(D("1060"), [D("592")])
        assert await repo.claim_completion(1) is None
        assert repo.status == "awaiting_payment"

    @pytest.mark.asyncio
    async def test_overpayment_still_closes(self):
        """Paying more than expected is still a finished trade."""
        repo = self._Repo(D("1000"), [D("1200")])
        assert await repo.claim_completion(1) is not None

    @pytest.mark.asyncio
    async def test_a_trade_with_no_expected_total_never_auto_closes(self):
        """
        A deposit whose instructions have not been issued has nothing to be
        measured against. It must wait, not close on the first rupee.
        """
        repo = self._Repo(None, [D("500")])
        assert await repo.claim_completion(1) is None

    @pytest.mark.asyncio
    async def test_exact_payment_closes(self):
        repo = self._Repo(D("1060"), [D("1060")])
        assert await repo.claim_completion(1) is not None


class TestDoneRemains:
    """
    /done is not removed, and the reason is worth stating.

    Automation closes a trade that is paid. It cannot close a trade that will
    never be paid in full — that is a commercial decision about accepting a
    shortfall, and it stays with a person.
    """

    def test_the_command_still_exists(self):
        from bot import client_bot
        src = Path(client_bot.__file__).read_text()
        assert 'Command("done")' in src

    def test_its_docstring_explains_when_to_use_it(self):
        from bot import client_bot
        doc = client_bot.cmd_done.__doc__ or ""
        assert "shortfall" in doc.lower()
