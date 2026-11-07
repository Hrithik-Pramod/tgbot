"""
A deal is a supplier AND an account. Money into any other account is not it.

THE RULE (Bridge, 9 October 2026)

    i think we need a rule to identify the combination, different supplier +
    same account = different debt.

    if a trade is happening, from supplier A to account A, regardless of
    anything that happens Sup A and Acc A completes, Acc B which was never
    allocated to this deal, should never break the expected outcome, which is
    all funds outstanding in the combo SUPA+ACCA is completed.

WHAT IT REPLACES

Two assumptions, each reasonable alone and wrong together.

  an account resolved to ANY open order of its vendor
        So ₹288,000 paid into `royal trading company` landed on SUPA50 — an
        order issued entirely against KISAN TRADERS — because both accounts
        belong to IndoLondon. The order's collected figure and the figure
        the client was working to stopped being the same number.

  the order already collecting won, even across vendors
        The established path, his own rule of 1 October, written when
        sharing an account was rare. By 9 October four accounts were shared
        across vendors, and "already collecting" had become the main way
        money reached the wrong debt. Royal Trading is registered to both
        Uncle and IndoLondon; SUPA50 was mid-collection; Uncle's BRAV20 had
        collected nothing. The payment was Uncle's.

            how has that done that?? it should be going to another vendor
            — Bridge, 9 October 2026

WHAT IS KEPT

The established path still decides between two orders of the SAME vendor —
one vendor splitting a collection is what it was written for, and there the
two orders are one debt in two parts. Across vendors they are two debts that
happen to share a bank account, and only the Bridge can say which is which.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


SHARED = [
    {"id": 9, "account_name": "royal trading company",
     "account_number": "50200000000000", "ifsc": "XXXX0000000",
     "trade_id": 88},
    {"id": 28, "account_name": "ROYAL TRADING COMPANY",
     "account_number": "50200000000000", "ifsc": "XXXX0000000",
     "trade_id": 91},
]


class _Msg:
    class _Chat:
        id = 1
    chat = _Chat()
    message_id = 2

    def __init__(self):
        self.sent = []

    async def answer(self, t, **kw): self.sent.append(t)
    async def reply(self, t, **kw): self.sent.append(t)
    async def react(self, r): pass


class _P:
    utr = "PUNBR10000000000000"
    amount_inr = D("288000")
    beneficiary = "royal trading company"


class _Notifier:
    def __init__(self):
        self.asked = []
    async def ask_which_order(self, **kw): self.asked.append(kw)
    async def check_completion(self, tid): return False
    async def check_near_completion(self, tid): return None


class _Repo:
    def __init__(self, trades):
        self.trades = trades
        self.added = []
        self.held = []
        self.audits = []

    async def live_trades_on_account(self, **kw): return list(self.trades)

    async def collection_path(self, **kw):
        # No path set: the Bridge has not been asked yet.
        return None

    async def add_payment(self, **kw):
        self.added.append(kw)
        return True, "Recorded."

    async def release_held_payments(self, utrs):
        # Added 22 October 2026: the shared-account branch now lets go of the
        # hold when it records, so every stub standing in for it needs this.
        return 0

    async def hold_payment(self, **kw):
        self.held.append(kw)
        return True, 1

    async def audit_standalone(self, **kw): self.audits.append(kw)


def _order(tid, supplier_id, collected, expected=D("5198490")):
    return {"id": tid, "account_id": 9, "supplier_id": supplier_id,
            "collected": collected, "inr_expected": expected,
            "reference": f"REF{tid}", "opened_at": None,
            "outstanding": expected - collected, "vendor": "A VENDOR"}


async def _place(repo, notifier=None):
    return await client_bot._place_on_shared_account(
        _Msg(), _P(), SHARED, {"id": 9}, repo, notifier or _Notifier())


# ----------------------------------------------------------------------

class TestAcrossVendorsTheBridgeAlwaysDecides:
    @pytest.mark.asyncio
    async def test_an_order_in_flow_now_wins_even_across_vendors(self):
        """
        REVERSED 7 November 2026, by the Bridge, knowing this history.

        This asserted the opposite until today — that an order
        mid-collection must NOT claim a payment when the other order on the
        account belongs to a different vendor. That was his rule of
        9 October, made after ₹288,000 for Uncle landed on IndoLondon's
        SUPA50 while SUPA50 was mid-collection.

        What it cost him:

            mid trade, another trade on the same account comes in, and
            causes a question to be triggered ... on ALL occassions, it has
            been a trade is midway, another comes in, and the bot asks where
            the funds should go, so it breaks the cycle. i dont want that to
            happen. midflow trades complete, unless i trigger a switch on my
            bot
            — Bridge, 7 November 2026

        Eleven payments held in four hours on 6 November, all waiting for
        him when he woke up.

        WHY THIS IS NOT SIMPLY REOPENING THE 9 OCTOBER HOLE

        SUPA50 was instructed against KISAN TRADERS. Royal Trading was never
        part of that deal — it was reachable only because an account
        resolved to ANY open order of its vendor. Every query here now
        requires a payment_slot on THIS account, so an order never
        instructed against it is not a candidate at all. The 9 October
        failure is closed upstream, by a different mechanism, and this gate
        was guarding a door that is already locked.

        WHAT IS STILL TRUE

        Only an order actually in flow claims anything, only when it is the
        only one in flow, and /collect still outranks it. The residual risk
        — in-flow order is vendor A's, money was vendor B's — he has
        accepted in writing, and the figures on screen plus /correct are the
        way back.
        """
        repo = _Repo([_order(88, supplier_id=30, collected=D("3649400")),
                      _order(91, supplier_id=31, collected=D(0))])
        notifier = _Notifier()
        out = await _place(repo, notifier)
        assert out is not None and out[0] == "recorded", (
            "an order in flow was interrupted by a second order opening on "
            "the same account — the 7 November complaint"
        )
        assert [a["trade_id"] for a in repo.added] == [88]
        assert not notifier.asked, "he was asked anyway"

    @pytest.mark.asyncio
    async def test_it_asks_even_when_both_are_collecting(self):
        repo = _Repo([_order(88, supplier_id=30, collected=D("3000000")),
                      _order(91, supplier_id=31, collected=D("1000000"))])
        out = await _place(repo)
        assert out[0] == "held"

    @pytest.mark.asyncio
    async def test_it_asks_even_when_neither_has_started(self):
        repo = _Repo([_order(88, supplier_id=30, collected=D(0)),
                      _order(91, supplier_id=31, collected=D(0))])
        out = await _place(repo)
        assert out[0] == "held"


class TestWithinOneVendorTheShortcutSurvives:
    """
    One vendor splitting a collection across two orders is one debt in two
    parts, and that is what the established path was written for.
    """

    @pytest.mark.asyncio
    async def test_the_collecting_order_still_wins(self):
        repo = _Repo([_order(88, supplier_id=30, collected=D("3649400")),
                      _order(91, supplier_id=30, collected=D(0))])
        out = await _place(repo)
        assert out[0] == "recorded"
        assert [a["trade_id"] for a in repo.added] == [88]

    @pytest.mark.asyncio
    async def test_two_of_one_vendors_orders_both_collecting_still_asks(self):
        """choose_established_path's own rule: it needs exactly one started."""
        repo = _Repo([_order(88, supplier_id=30, collected=D("1000000")),
                      _order(91, supplier_id=30, collected=D("2000000"))])
        out = await _place(repo)
        assert out[0] == "held"


class TestAMissingVendorIsNeverAssumedAway:
    @pytest.mark.asyncio
    async def test_rows_without_the_collection_figures_are_treated_as_unknown(self):
        """
        Asking costs a tap. Assuming costs a misattribution, so an
        incomplete row must not fall through to the shortcut.

        7 November 2026: this used to withhold a row missing supplier_id,
        because the shortcut was gated on the vendors being the same. The
        shortcut now reads the collection FIGURES instead, so supplier_id
        is no longer consulted and a row missing it is no longer dangerous
        — but a row missing `collected` or `inr_expected` is, and raises
        where the old guard used to catch.

        The rule did not change. The field that can be missing did.
        """
        repo = _Repo([{"id": 88, "account_id": 9, "supplier_id": 30,
                       "reference": "REF88", "opened_at": None,
                       "outstanding": D(0), "vendor": "A VENDOR"}])
        out = await _place(repo)
        assert out[0] == "held"

    @pytest.mark.asyncio
    async def test_a_row_without_a_vendor_is_now_harmless(self):
        """
        The other side of the same move: one order in flow on this account
        is claimed on the strength of its figures, and whose it is does not
        come into it.
        """
        repo = _Repo([{"id": 88, "account_id": 9, "collected": D("3649400"),
                       "inr_expected": D("5198490"), "reference": "REF88",
                       "opened_at": None, "outstanding": D(0),
                       "vendor": "A VENDOR"}])
        out = await _place(repo)
        assert out[0] == "recorded"


# ----------------------------------------------------------------------
# The other half: an account only resolves to an order it was issued against
# ----------------------------------------------------------------------

class TestAnAccountOnlyResolvesToAnOrderItWasIssuedAgainst:
    def test_the_matcher_requires_an_instruction_for_that_account(self):
        """
        Without this, `royal trading company` resolved to SUPA50 — an order
        issued entirely against KISAN TRADERS — because both belong to
        IndoLondon.
        """
        sql = inspect.getsource(Repo.matchable_accounts_for_client)
        joined = " ".join(sql.split())
        assert "FROM payment_slots ps" in joined
        assert "ps.bank_account_id = b.id" in joined
        assert "ps.trade_id = t.id" in joined

    def test_the_shared_account_lookup_requires_it_too(self):
        """
        Both routes resolve an account to an order. One of them honouring
        the rule is how the two drift apart.
        """
        sql = inspect.getsource(Repo.live_trades_on_account)
        joined = " ".join(sql.split())
        assert "FROM payment_slots ps" in joined
        assert "ps.bank_account_id = b.id" in joined

    def test_the_shared_lookup_reports_whose_order_it_is(self):
        """
        The across-vendors rule cannot be applied without knowing the
        vendor, and a missing field there silently restores the old
        behaviour.
        """
        sql = " ".join(inspect.getsource(Repo.live_trades_on_account).split())
        # The SELECT list specifically. `t.supplier_id` also appears in the
        # JOIN, so searching the whole statement passed with the column
        # removed — the mutation that proved it was E5, 9 October.
        select_list = sql[sql.index("SELECT DISTINCT ON"):sql.index("FROM trades")]
        assert "t.supplier_id" in select_list, (
            "the shared lookup no longer returns whose order each one is, so "
            "the across-vendors rule cannot be applied"
        )

    def test_the_bridges_own_list_is_not_restricted(self):
        """
        placeable_trades_for_client backs the buttons he is offered when he
        places a payment by hand. He is allowed to put money anywhere he
        judges right — the restriction is on what the bot decides alone.
        """
        sql = inspect.getsource(Repo.placeable_trades_for_client)
        assert "payment_slots" in sql, (
            "it still needs the slot to pick a sensible account"
        )
        joined = " ".join(sql.split())
        assert "AND EXISTS (SELECT 1 FROM payment_slots" not in joined
