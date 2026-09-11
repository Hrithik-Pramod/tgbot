"""
One client, two suppliers, two trades open at once.

Found live on 11 September 2026. Client A had SUPA1 and SUPB1 running
together, and every client command resolved "the" open trade as:

    WHERE client_id = $1 AND status IN ('open','awaiting_payment')
    ORDER BY opened_at DESC LIMIT 1

so the most recent trade won and payments made against one supplier's
instruction were charged to the other's trade. The client reported it as the
bot "picking up wrong supplier". Nothing was lost, but only because of the
order things happened in that morning.

The fix rests on one fact: a bank account belongs to exactly one supplier, and
a supplier has at most one open trade. So the account a payment went to
identifies its trade, with nothing to guess and nothing to ask.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402
from core.parse import match_account  # noqa: E402
from db.repo import Repo  # noqa: E402


def _rows():
    """Two suppliers' accounts, each tagged with its own trade."""
    return [
        {"id": 1, "account_name": "SUPER TRADING COMPANY (STC)",
         "account_number": "1", "ifsc": "X", "trade_id": 10,
         "reference": "SUPA1", "supplier_label": "Supplier A"},
        {"id": 2, "account_name": "Wasim Salim Shaikh",
         "account_number": "2", "ifsc": "X", "trade_id": 20,
         "reference": "SUPB1", "supplier_label": "Supplier B"},
    ]


class TestTheAccountPicksTheTrade:
    def test_supplier_a_account_resolves_to_supplier_a_trade(self):
        rows = _rows()
        account_id = match_account("SUPER TRADING COMPANY (STC)", rows)
        trade = next(r["trade_id"] for r in rows if r["id"] == account_id)
        assert trade == 10

    def test_supplier_b_account_resolves_to_supplier_b_trade(self):
        rows = _rows()
        account_id = match_account("Wasim Salim Shaikh", rows)
        trade = next(r["trade_id"] for r in rows if r["id"] == account_id)
        assert trade == 20

    def test_the_newest_trade_does_not_win(self):
        """
        The actual regression. SUPB1 opened later; a payment to Supplier A's
        account must still go to SUPA1.
        """
        rows = _rows()
        newest = max(r["trade_id"] for r in rows)
        account_id = match_account("SUPER TRADING COMPANY (STC)", rows)
        chosen = next(r["trade_id"] for r in rows if r["id"] == account_id)
        assert chosen != newest

    def test_an_unrecognised_account_resolves_to_nothing(self):
        """It must ask rather than guess — a wrong guess is now two mistakes."""
        assert match_account("Some Other Firm Ltd", _rows()) is None


class TestNoHandlerStillPicksByRecency:
    """
    The property, stated against the source.

    open_trade_for_client returns one row by recency. It is fine for a supplier
    -side view, but no client-side handler may use it — that is precisely how
    payments were misattributed.
    """

    CLIENT_HANDLERS = [
        "cmd_accounts", "cmd_add", "add_utr", "add_account",
        "on_pasted_payment", "pasted_pick_account", "cmd_done",
    ]

    def test_no_client_handler_resolves_a_single_trade_by_recency(self):
        offenders = []
        for name in self.CLIENT_HANDLERS:
            fn = getattr(client_bot, name)
            if "open_trade_for_client" in inspect.getsource(fn):
                offenders.append(name)
        assert not offenders, (
            f"{offenders} still resolve the client's trade by recency — with "
            "two trades open that charges payments to the wrong supplier"
        )

    def test_the_accounts_query_carries_the_trade(self):
        src = inspect.getsource(Repo.open_trade_accounts_for_client)
        assert "trade_id" in src and "b.party_id = t.supplier_id" in src

    def test_done_refuses_to_guess_between_trades(self):
        src = inspect.getsource(client_bot.cmd_done)
        assert "len(open_trades) > 1" in src, \
            "/done must not pick a trade when several are open"


class TestOverpaymentIsNotCalledSentInFull:
    """
    Live, 11 September 2026. SUPB1 expected ₹212,000 and received ₹414,400.
    The summary correctly said "Over by ₹202,400" and the very next message
    said "₹414,400 sent in full" — two contradictory statements one after the
    other, which is how a correct arithmetic result got reported as a fault.
    """

    def test_an_overpayment_says_so(self):
        from decimal import Decimal as D
        from core.summary import render_completion_notice

        out = render_completion_notice(D("414400"), D("212000"))
        assert "OVERPAID" in out
        assert "202,400" in out
        assert "sent in full" not in out

    def test_a_shortfall_says_so(self):
        from decimal import Decimal as D
        from core.summary import render_completion_notice

        out = render_completion_notice(D("200000"), D("212000"))
        assert "Short by" in out and "sent in full" not in out

    def test_an_exact_payment_keeps_the_clients_wording(self):
        """Their own phrasing, unchanged, for the case it was written for."""
        from decimal import Decimal as D
        from core.summary import render_completion_notice

        out = render_completion_notice(D("212000"), D("212000"))
        assert out == "This order is completed — ₹212,000 sent in full."

    def test_it_still_works_without_an_expected_figure(self):
        from decimal import Decimal as D
        from core.summary import render_completion_notice

        assert "sent in full" in render_completion_notice(D("212000"))


class TestRecordingSpansTrades:
    """
    One pasted message can legitimately pay both suppliers. Each payment is
    written against its own trade, and each affected trade is checked for
    completion separately.
    """

    def test_record_uses_the_per_payment_trade(self):
        src = inspect.getsource(client_bot._record)
        assert 'trade_id=s["trade_id"]' in src
        assert "touched" in src, "completion must be checked per trade touched"

    def test_record_no_longer_takes_a_single_trade(self):
        params = list(inspect.signature(client_bot._record).parameters)
        assert "trade_id" not in params
