"""
The Bridge is asked once per account, not once per payment.

WHAT HAPPENED — 12 October 2026

Nineteen payments landed on ROYAL TRADING COMPANY in three hours. Two vendors
had live orders issued against it, so no slip could say which order each
payment belonged to, and the bot asked. Nineteen times.

    the bot is asking me everytime to choose
    so each send, the bot asks, which one?? but its done it 5 times.
    we cant keep having this happen mate ... i need this completely fixing
    final, this is happening the same with second account all the time
    — Bridge

Two of the nineteen were tapped onto the wrong order and BRAV23 closed
₹44,090 over. The audit log records both as:

    "placed_by": "Bridge chose the order; the account is shared and the slip
                  could not say"

Which is true, and is the whole point. He did not misread his book. He was
asked nineteen near-identical questions and mis-tapped twice. Nineteen
identical prompts is not a question, it is a reflex test, and the overpayment
was its output.

SO THE ASKING WAS THE FAULT, NOT THE ANSWERING

His answer is now remembered against the account and followed until that
order closes. One question per order instead of one per payment — which is
his own description of it from the day the behaviour was first specified:

    if a trade is in flow, and a different vendor chooses same account for
    deposits, the flow of the original is not interupted as the path is
    already set
    — Bridge, 1 October 2026

The difference from the rule that was removed on 9 October is who decides.
That one inferred the path from whichever order happened to be part-paid, and
put ₹288,000 of Uncle's money on IndoLondon's order. This one is his choice,
recorded, and it ends with the order it was about.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade, client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


SHARED = [
    {"id": 9, "account_name": "royal trading company",
     "account_number": "50200000000000", "ifsc": "XXXX0000000",
     "trade_id": 88},
    {"id": 28, "account_name": "ROYAL TRADING COMPANY",
     "account_number": "50200000000000", "ifsc": "XXXX0000000",
     "trade_id": 95},
]


def _order(tid, supplier_id, collected=D(0)):
    return {"id": tid, "account_id": 28, "supplier_id": supplier_id,
            "collected": collected, "inr_expected": D("4361634"),
            "reference": f"REF{tid}", "opened_at": None,
            "outstanding": D("4361634") - collected, "vendor": "A VENDOR"}


class _Msg:
    class _Chat:
        id = 1
    chat = _Chat()
    message_id = 2

    async def answer(self, t, **kw): pass
    async def reply(self, t, **kw): pass
    async def react(self, r): pass


class _P:
    utr = "BKIDR10000000000001"
    amount_inr = D("239000")
    beneficiary = "ROYAL TRADING COMPANY"


class _Notifier:
    def __init__(self):
        self.asked = []
    async def ask_which_order(self, **kw): self.asked.append(kw)
    async def check_completion(self, tid): return False
    async def check_near_completion(self, tid): return None


class _Repo:
    def __init__(self, trades, *, path_trade_id=None):
        self.trades = trades
        self.path_trade_id = path_trade_id
        self.added = []
        self.held = []
        self.audits = []
        self.paths_set = []

    async def live_trades_on_account(self, **kw): return list(self.trades)

    async def collection_path(self, **kw):
        if self.path_trade_id is None:
            return None
        return {"id": 1, "trade_id": self.path_trade_id, "set_at": None,
                "reference": "REF", "supplier_id": 31,
                "inr_expected": D("4361634"), "collected": D(0)}

    async def set_collection_path(self, **kw): self.paths_set.append(kw)

    async def add_payment(self, **kw):
        self.added.append(kw)
        return True, "Recorded."

    async def hold_payment(self, **kw):
        self.held.append(kw)
        return True, len(self.held)

    async def audit_standalone(self, **kw): self.audits.append(kw)


async def _place(repo, notifier=None):
    return await client_bot._place_on_shared_account(
        _Msg(), _P(), SHARED, {"id": 9}, repo, notifier or _Notifier())


# ----------------------------------------------------------------------

class TestOnceHeHasSaidItIsNotAskedAgain:
    @pytest.mark.asyncio
    async def test_the_payment_follows_his_answer(self):
        """The whole fix. Two vendors live, but he has already decided."""
        repo = _Repo([_order(88, supplier_id=30), _order(95, supplier_id=31)],
                     path_trade_id=95)
        notifier = _Notifier()
        out = await _place(repo, notifier)
        assert out is not None and out[0] == "recorded"
        assert [a["trade_id"] for a in repo.added] == [95]
        assert notifier.asked == [], "he was asked again after answering"

    @pytest.mark.asyncio
    async def test_it_does_not_hold_the_money(self):
        repo = _Repo([_order(88, supplier_id=30), _order(95, supplier_id=31)],
                     path_trade_id=95)
        await _place(repo)
        assert repo.held == []

    @pytest.mark.asyncio
    async def test_a_path_to_an_order_not_on_this_account_is_ignored(self):
        """
        The order he chose has moved on — reissued elsewhere, or closed and
        replaced. A path must never route money to an order that is not
        collecting here, so it falls back to asking.
        """
        repo = _Repo([_order(88, supplier_id=30), _order(95, supplier_id=31)],
                     path_trade_id=999)
        notifier = _Notifier()
        out = await _place(repo, notifier)
        assert out[0] == "held"
        assert notifier.asked

    @pytest.mark.asyncio
    async def test_with_no_path_it_still_asks(self):
        repo = _Repo([_order(88, supplier_id=30), _order(95, supplier_id=31)])
        notifier = _Notifier()
        out = await _place(repo, notifier)
        assert out[0] == "held"
        assert notifier.asked


class TestHisAnswerIsRemembered:
    def test_placing_a_held_payment_records_the_path(self):
        src = inspect.getsource(bridge_trade.place_held_payment)
        assert "set_collection_path" in src, (
            "his answer is not remembered, so the next payment asks again"
        )

    def test_only_for_the_shared_account_kind(self):
        """
        A payment held because the name matched nothing has no account to
        attach a path to, and the next unreadable name is a fresh question.
        """
        src = inspect.getsource(bridge_trade.place_held_payment)
        assert 'if ok and held["account_number"]:' in src

    def test_it_is_not_recorded_when_the_placement_failed(self):
        src = inspect.getsource(bridge_trade.place_held_payment)
        branch = src[src.index("set_collection_path") - 400:]
        assert "if ok and" in branch


class TestAPathDiesWithItsOrder:
    def test_completion_clears_it(self):
        """
        A finished order collects nothing. A path outliving its order would
        route the next vendor's money onto a closed trade.
        """
        src = inspect.getsource(Repo.claim_completion)
        assert "collection_paths" in src
        assert "cleared_at = now()" in src

    def test_cancelling_clears_it(self):
        src = inspect.getsource(Repo.cancel_trade)
        assert "collection_paths" in src
        assert "cleared_at = now()" in src

    def test_the_lookup_refuses_a_dead_order_anyway(self):
        """
        Belt as well as braces: even if a clear is missed, the path is only
        honoured while its order is live.
        """
        sql = " ".join(inspect.getsource(Repo.collection_path).split())
        assert "t.status IN ('open', 'awaiting_payment')" in sql
        assert "cp.cleared_at IS NULL" in sql


class TestOneAccountHasOneAnswer:
    def test_a_new_choice_replaces_the_old_one(self):
        """
        Two live paths for one account would be two answers to the same
        question, and the lookup would have to guess between them.
        """
        src = inspect.getsource(Repo.set_collection_path)
        assert "replaced by a new choice" in src

    def test_the_database_enforces_it_too(self):
        for path in ("db/schema.sql", "deploy/migrate-015-collection-paths.sql"):
            text = (ROOT / path).read_text(encoding="utf-8")
            assert "collection_paths_live_unique" in text, path
            assert "WHERE cleared_at IS NULL" in text, path

    def test_the_migration_can_be_run_twice(self):
        mig = (ROOT / "deploy" / "migrate-015-collection-paths.sql").read_text(
            encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS" in mig
        assert "CREATE UNIQUE INDEX IF NOT EXISTS" in mig

    def test_nothing_is_assumed_by_the_migration(self):
        """
        No path is seeded. The first payment on each shared account still
        asks, once, and only his answer sets it.
        """
        mig = (ROOT / "deploy" / "migrate-015-collection-paths.sql").read_text(
            encoding="utf-8")
        assert "INSERT INTO collection_paths" not in mig
