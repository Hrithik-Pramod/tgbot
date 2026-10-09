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

    async def add_payment(self, **kw):
        self.added.append(kw)
        return True, "Recorded."

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
    async def test_the_collecting_order_no_longer_wins(self):
        """
        The 9 October case exactly: IndoLondon mid-collection, Uncle at
        zero, and the payment was Uncle's.
        """
        repo = _Repo([_order(88, supplier_id=30, collected=D("3649400")),
                      _order(91, supplier_id=31, collected=D(0))])
        notifier = _Notifier()
        out = await _place(repo, notifier)
        assert out is not None and out[0] == "held", (
            "a payment was attributed across vendors on the strength of one "
            "order being mid-collection"
        )
        assert repo.added == [], "it was recorded without anyone being asked"
        assert notifier.asked, "the Bridge was not asked"

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
    async def test_rows_without_a_supplier_are_treated_as_unknown(self):
        """
        Asking costs a tap. Assuming costs a misattribution, so an
        incomplete row must not fall through to the shortcut.
        """
        repo = _Repo([{"id": 88, "account_id": 9, "collected": D("3649400"),
                       "inr_expected": D("5198490"), "reference": "REF88",
                       "opened_at": None, "outstanding": D(0),
                       "vendor": "A VENDOR"}])
        out = await _place(repo)
        assert out[0] == "held"


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
