"""
Telling the Bridge when funds land, not when a supplier says they have sent.

Client request, 11 September 2026: "can we tie up, only when validation of
funds landing does the bot notify me".

A supplier running /send is making a claim. Funds arriving on chain is a fact.
The Bridge acts on facts, so the claim is recorded quietly and surfaces
attached to the deposit.

The cost of that is the opposite case: a claim that never materialises used to
be visible the instant it was made and would now vanish. So it is reported
after thirty minutes — the client chose the number. These tests cover both
halves, because the second one only matters when something has gone wrong,
which is exactly when nobody is watching for it.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.summary import render_deposit_notification  # noqa: E402
from monitor.tron import UNMATCHED_SEND_MINUTES, DepositMonitor  # noqa: E402


class TestTheDepositNotificationCarriesTheNomination:
    """
    One notification, at the point the Bridge can act, with everything needed
    on it — including the account the supplier chose.
    """

    def _render(self, nominated=None):
        return render_deposit_notification(
            reference="SUPA1", supplier_label="Supplier A", client_label="Client A",
            usdt_in=D("19.74"), inr_out=D("2092"), tx_hash="abc123",
            supply_rate=D("106"), sell_rate=D("107"), usdt_out=D("19.55"),
            nominated_account=nominated,
        )

    def test_the_nominated_account_is_named(self):
        assert "Supplier nominated: Ekta Traders" in self._render("Ekta Traders")

    def test_it_is_omitted_when_the_supplier_did_not_nominate(self):
        assert "nominated" not in self._render()

    def test_the_agreed_header_is_untouched(self):
        lines = self._render("Ekta Traders").splitlines()
        assert lines[0] == "SUPPLIER A  ·  Transaction SUPA1"

    def test_an_account_name_is_escaped_in_html(self):
        out = render_deposit_notification(
            reference="SUPA1", supplier_label="Supplier A", client_label="Client A",
            usdt_in=D("1"), inr_out=D("1"), tx_hash="abc",
            nominated_account="Smith & Co", html=True,
        )
        assert "Smith &amp; Co" in out


class TestUnmatchedSendsAreReported:
    """
    The half that only matters when something is wrong.
    """

    class _Repo:
        def __init__(self, stale):
            self._stale = list(stale)
            self.alerted = []

        async def stale_pending_sends(self, minutes):
            self.minutes_asked = minutes
            return [r for r in self._stale if r["id"] not in self.alerted]

        async def mark_pending_alerted(self, pending_id):
            # Mirrors the real single-statement claim: first caller wins.
            if pending_id in self.alerted:
                return False
            self.alerted.append(pending_id)
            return True

    class _Notifier:
        def __init__(self):
            self.messages = []

        async def to_bridge(self, text, reply_markup=None, html=False):
            self.messages.append(text)

    class _Config:
        min_deposit_amount = D("1")
        poll_interval_seconds = 5

    def _monitor(self, stale):
        repo = self._Repo(stale)
        notifier = self._Notifier()
        m = DepositMonitor(repo=repo, client=None, notifier=notifier,
                           config=self._Config())
        return m, repo, notifier

    ROW = {"id": 1, "hash_url": "https://tronscan.org/#/transaction/abc",
           "created_at": None, "supplier_label": "Supplier A",
           "account_name": "Ekta Traders"}

    @pytest.mark.asyncio
    async def test_the_bridge_is_told(self):
        m, _, n = self._monitor([self.ROW])
        await m._report_unmatched_sends()
        assert len(n.messages) == 1
        assert "Supplier A" in n.messages[0]
        assert "Ekta Traders" in n.messages[0]

    @pytest.mark.asyncio
    async def test_it_uses_the_agreed_window(self):
        m, repo, _ = self._monitor([self.ROW])
        await m._report_unmatched_sends()
        assert repo.minutes_asked == UNMATCHED_SEND_MINUTES == 30

    @pytest.mark.asyncio
    async def test_it_reports_once_not_on_every_poll(self):
        """
        This runs every few seconds. Without the claim it would repeat the
        same alert until someone acted on it.
        """
        m, _, n = self._monitor([self.ROW])
        for _ in range(5):
            await m._report_unmatched_sends()
        assert len(n.messages) == 1

    @pytest.mark.asyncio
    async def test_a_claim_lost_to_a_race_is_not_reported_twice(self):
        m, repo, n = self._monitor([self.ROW])
        repo.alerted.append(1)          # another poll won the claim first
        await m._report_unmatched_sends()
        assert n.messages == []

    @pytest.mark.asyncio
    async def test_it_survives_a_missing_hash(self):
        row = dict(self.ROW, hash_url=None, account_name=None)
        m, _, n = self._monitor([row])
        await m._report_unmatched_sends()
        assert len(n.messages) == 1
        assert "unnamed account" in n.messages[0]

    @pytest.mark.asyncio
    async def test_a_database_error_does_not_stop_the_poll(self):
        """
        This is called from the polling loop. An exception here would take
        down deposit detection, which is far worse than a missed alert.
        """
        class Broken:
            async def stale_pending_sends(self, minutes):
                raise RuntimeError("database gone")

        m = DepositMonitor(repo=Broken(), client=None,
                           notifier=self._Notifier(), config=self._Config())
        await m._report_unmatched_sends()   # must not raise
