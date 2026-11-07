"""
A trade in motion finishes. A second order on the account does not stop it.

    so this is the issue, mid trade, another trade on the same account comes
    in, and causes a question to be triggered

    if neither has started, i would always wait to initiate the start of one

    but on ALL occassions, it has been a trade is midway, another comes in,
    and the bot asks where the funds should go, so it breaks the cycle. i
    dont want that to happen.

    midflow trades complete, unless i trigger a switch on my bot
    — Bridge, 7 November 2026

Eleven payments held between 05:00 and 09:15 on 6 November, one account, all
sitting there when he woke up.

THE DIAGNOSTIC LIED, AND IT NEARLY BOUGHT THE WRONG FIX

Every one of those eleven rows read:

    "two vendors have a live order on this account and neither has started
     collecting; asking the Bridge"

which said the cause was that nothing had started. He said, flatly, "on ALL
occassions it has been a trade is midway". He was right. That sentence was a
DEFAULT in hold_payment, used whenever the caller passed no `why` — written
on 1 October when it was the only case, never updated when 9 October added
another. It described an assumption and was read as a finding.

So: no default any more, and this branch states what it actually saw.

WHAT CHANGED IN THE RULE

The established path — the order already collecting into this account wins —
was limited to one vendor's orders on 9 October, after ₹288,000 for Uncle
landed on IndoLondon's SUPA50 while SUPA50 was mid-collection.

That gate is now guarding a door that is already locked. SUPA50 was
instructed against KISAN TRADERS; Royal Trading was never part of that deal
and was only reachable because an account resolved to ANY open order of its
vendor. Since 9 October every query here requires a payment_slot on THIS
account, so an order never instructed against it cannot be a candidate. The
failure that justified the gate is closed upstream, by a different mechanism
— and the gate's remaining effect was to stop a live collection dead every
time a second order opened.

WHAT DID NOT CHANGE

Only an order genuinely in flow claims anything. Two in flow, or none, and
he is still asked. /collect still outranks all of it: "unless i trigger a
switch on my bot".
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402

ACCOUNT, IFSC = "50200000000000", "HDFC0000001"

MATCHABLE = [
    {"id": 9, "account_name": "SUPER TRADING COMPANY",
     "account_number": ACCOUNT, "ifsc": IFSC, "trade_id": None,
     "supplier_label": "ONE"},
    {"id": 28, "account_name": "super trading company",
     "account_number": ACCOUNT, "ifsc": IFSC, "trade_id": None,
     "supplier_label": "TWO"},
]


class _P:
    utr = "BKIDR10000000000001"
    amount_inr = D("334000")
    beneficiary = "SUPER TRAD"


def _order(tid, *, supplier_id, collected, expected="5000000"):
    return {"id": tid, "account_id": 9, "supplier_id": supplier_id,
            "collected": D(collected), "inr_expected": D(expected),
            "reference": f"REF{tid}", "opened_at": None,
            "outstanding": D(expected) - D(collected), "vendor": "A VENDOR"}


class _Msg:
    chat = type("c", (), {"id": 1})()
    message_id = 2

    async def answer(self, *a, **kw): return None
    async def reply(self, *a, **kw): return None


class _Notifier:
    def __init__(self):
        self.asked = []

    async def ask_which_order(self, **kw): self.asked.append(kw)
    async def check_completion(self, tid): return False
    async def check_near_completion(self, tid): return None


class _Repo:
    def __init__(self, trades, *, path=None):
        self.trades = trades
        self.path = path
        self.added = []
        self.held = []

    async def live_trades_on_account(self, **kw): return list(self.trades)
    async def collection_path(self, **kw): return self.path

    async def add_payment(self, *, trade_id, **kw):
        self.added.append(trade_id)
        return True, "recorded"

    async def hold_payment(self, **kw):
        self.held.append(kw)
        return True, len(self.held)

    async def release_held_payments(self, utrs): return 0
    async def audit_standalone(self, **kw): pass


async def _place(repo, notifier=None):
    return await client_bot._place_on_shared_account(
        _Msg(), _P(), MATCHABLE, {"id": 3}, repo, notifier or _Notifier()
    )


class TestTheOrderInFlowKeepsCollecting:
    @pytest.mark.asyncio
    async def test_a_second_order_does_not_break_the_cycle(self):
        """
        The complaint itself. One order mid-collection, a second opens on
        the same account, different vendor — the first keeps filling.
        """
        repo = _Repo([_order(88, supplier_id=30, collected="3649400"),
                      _order(91, supplier_id=31, collected=0)])
        notifier = _Notifier()
        out = await _place(repo, notifier)
        assert out[0] == "recorded"
        assert repo.added == [88]
        assert notifier.asked == []

    @pytest.mark.asyncio
    async def test_it_works_within_one_vendor_too(self):
        """Unchanged — this always worked, and must keep working."""
        repo = _Repo([_order(88, supplier_id=30, collected="100000"),
                      _order(91, supplier_id=30, collected=0)])
        out = await _place(repo)
        assert out[0] == "recorded" and repo.added == [88]

    @pytest.mark.asyncio
    async def test_a_filled_order_does_not_keep_claiming(self):
        """
        "In flow" means started AND still short. A fully collected order is
        finished, and the next payment belongs to the one behind it — which
        is the whole sequence he described.
        """
        repo = _Repo([_order(88, supplier_id=30, collected="5000000",
                             expected="5000000"),
                      _order(91, supplier_id=31, collected=0)])
        out = await _place(repo)
        assert out[0] == "held", "a completed order went on collecting"


class TestWhenItStillAsks:
    @pytest.mark.asyncio
    async def test_two_orders_both_in_flow(self):
        """
        Nothing to follow: both are mid-collection and only he knows whose
        the next payment is.
        """
        repo = _Repo([_order(88, supplier_id=30, collected="3000000"),
                      _order(91, supplier_id=31, collected="1000000")])
        out = await _place(repo)
        assert out[0] == "held"

    @pytest.mark.asyncio
    async def test_neither_has_started(self):
        """
            if neither has started, i would always wait to initiate the
            start of one — Bridge
        """
        repo = _Repo([_order(88, supplier_id=30, collected=0),
                      _order(91, supplier_id=31, collected=0)])
        out = await _place(repo)
        assert out[0] == "held"

    @pytest.mark.asyncio
    async def test_the_figures_being_unreadable_asks_rather_than_assumes(self):
        repo = _Repo([{"id": 88, "account_id": 9, "supplier_id": 30,
                       "reference": "R", "opened_at": None,
                       "outstanding": D(0), "vendor": "V"}])
        out = await _place(repo)
        assert out[0] == "held"


class TestHisSwitchStillOutranksIt:
    @pytest.mark.asyncio
    async def test_collect_beats_the_order_in_flow(self):
        """
            midflow trades complete, UNLESS i trigger a switch on my bot

        /collect is that switch, and it has to win or it is not a switch.
        """
        repo = _Repo([_order(88, supplier_id=30, collected="3649400"),
                      _order(91, supplier_id=31, collected=0)],
                     path={"trade_id": 91})
        out = await _place(repo)
        assert out[0] == "recorded"
        assert repo.added == [91], "his own choice was overruled"


class TestTheHoldSaysWhatItSaw:
    @pytest.mark.asyncio
    async def test_it_records_the_real_state_not_a_default(self):
        """
        The sentence that misled the 7 November diagnosis claimed nothing
        had started collecting, on every hold of this kind.
        """
        repo = _Repo([_order(88, supplier_id=30, collected="3000000"),
                      _order(91, supplier_id=31, collected="1000000")])
        await _place(repo)
        why = repo.held[0]["why"]
        assert "2 live orders" in why
        assert "2 part-collected" in why
        assert "neither has started" not in why

    @pytest.mark.asyncio
    async def test_it_counts_in_flight_correctly_when_none_are(self):
        repo = _Repo([_order(88, supplier_id=30, collected=0),
                      _order(91, supplier_id=31, collected=0)])
        await _place(repo)
        assert "0 part-collected" in repo.held[0]["why"]

    @pytest.mark.asyncio
    async def test_a_fully_collected_order_is_not_counted_as_in_flight(self):
        """
        "In flow" is started AND still short, in the count as well as in
        the decision. A mutation that dropped the "still short" half left
        every other case here unchanged — collected=0 fails the first half
        anyway, and two part-collected orders are in flight either way.
        Two FILLED orders is the case that can tell: nothing is in flow, and
        a reason claiming two would be the 1 October default all over again,
        wrong in a new way.
        """
        repo = _Repo([_order(88, supplier_id=30, collected="5000000",
                             expected="5000000"),
                      _order(91, supplier_id=31, collected="5000000",
                             expected="5000000")])
        out = await _place(repo)
        assert out[0] == "held"
        assert "0 part-collected" in repo.held[0]["why"]

    def test_there_is_no_default_reason_left_in_the_repo(self):
        """
        A default that states an assumption is believed exactly when
        somebody is trying to work out what happened.
        """
        import inspect
        import re
        src = inspect.getsource(Repo.hold_payment)
        # Comments stripped: the comment recording WHY that sentence was
        # removed naturally quotes it, and the first version of this test
        # flagged its own explanation. Same trap as the ORDER BY scan in
        # test_collect_actually_moves_the_money.
        code = "\n".join(re.sub(r"#.*$", "", l) for l in src.splitlines())
        assert "neither has started" not in code
        assert 'why or "not recorded by the caller"' in code
