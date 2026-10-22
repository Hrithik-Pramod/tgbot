"""
Every path that records a payment lets go of the hold.

WHAT WAS FOUND — 22 October 2026, 23:00

    id | asked               | amount_inr | no_account
    46 | 2026-10-22 01:10:40 |  390300.00 | f

Still waiting. But that UTR went on the ledger at 09:59:44 the same morning.

It was asked about at 01:10 — two vendors had a live order on that account,
so it was held and the Bridge was asked which one. At 04:30 he answered, and
the answer was remembered. When the slip came round again at 09:59 the
remembered path placed it without asking, which is what that feature is for.

But only _record releases holds. The shared-account branch records straight
through repo.add_payment and returned without touching held_payments, so the
row sat there, unresolved, and the chase would have gone on asking him about
money he had already banked.

That is the 8 October fault one level down:

    Again peter / From nowhere — Bridge, 8 October 2026

    PUNBR…8869 was banked on BRAV19 at 14:55 and asked about at 19:25 and
    again at 19:32. Nothing was wrong with the money. The bot was
    interrogating the client about its own bookkeeping.

A chaser that cries wolf is one he stops reading — and the chase is the only
thing standing between a held payment and being forgotten entirely.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402


class _P:
    utr = "BKIDR10000000000000001"
    amount_inr = D("390300")
    beneficiary = "ROYAL TRADING COMPANY"


class _Repo:
    """Enough of the repo for the recorded branch, and a held_payments list."""

    def __init__(self, *, add_ok=True):
        self.add_ok = add_ok
        self.released: list[str] = []
        self.held = {"BKIDR10000000000000001"}
        self.logged: list[dict] = []

    async def add_payment(self, **kw):
        return (True, "recorded") if self.add_ok else (False, "duplicate")

    async def release_held_payments(self, utrs):
        for u in utrs:
            self.released.append(u)
            self.held.discard(u)
        return len(utrs)

    async def live_trades_on_account(self, **kw):
        # Two live orders from two vendors on one account — the state that
        # makes this branch run at all, and the state held row 46 was in.
        return [{"id": 7, "supplier_id": 2, "account_id": 1},
                {"id": 8, "supplier_id": 3, "account_id": 1}]

    async def collection_path(self, **kw):
        return {"trade_id": 7}

    async def audit_standalone(self, **kw):
        self.logged.append(kw)


class _Notifier:
    def __init__(self):
        self.completed = []

    async def check_completion(self, tid):
        self.completed.append(tid)
        return True

    async def check_near_completion(self, tid):
        return None


class _Msg:
    chat = type("c", (), {"id": 1})()
    message_id = 2

    async def answer(self, *a, **kw): return None
    async def reply(self, *a, **kw): return None


MATCHABLE = [
    {"id": 1, "account_number": "50200000000000", "ifsc": "HDFC0000001",
     "account_name": "ROYAL TRADING COMPANY", "trade_id": 7, "supplier_id": 2},
    {"id": 1, "account_number": "50200000000000", "ifsc": "HDFC0000001",
     "account_name": "ROYAL TRADING COMPANY", "trade_id": 8, "supplier_id": 3},
]


class TestTheRecordedBranchReleases:
    @pytest.mark.asyncio
    async def test_a_payment_it_records_stops_being_held(self):
        repo, notifier = _Repo(), _Notifier()
        outcome, _ = await client_bot._place_on_shared_account(
            _Msg(), _P(), MATCHABLE, {"id": 3}, repo, notifier
        )
        assert outcome == "recorded"
        assert repo.released == [_P.utr], "the hold was left open"
        assert repo.held == set()

    @pytest.mark.asyncio
    async def test_it_still_closes_the_trade(self):
        """
        The release sits next to check_completion. Neither may displace the
        other — SUPA43 is what a missing check_completion costs.
        """
        repo, notifier = _Repo(), _Notifier()
        await client_bot._place_on_shared_account(
            _Msg(), _P(), MATCHABLE, {"id": 3}, repo, notifier
        )
        assert notifier.completed == [7]

    @pytest.mark.asyncio
    async def test_a_duplicate_does_not_release_anything(self):
        """
        A duplicate is not this message recording the payment, so this
        message has no business resolving the hold. Whichever path really
        recorded it already did.
        """
        repo, notifier = _Repo(add_ok=False), _Notifier()
        outcome, _ = await client_bot._place_on_shared_account(
            _Msg(), _P(), MATCHABLE, {"id": 3}, repo, notifier
        )
        assert outcome == "duplicate"
        assert repo.released == []

    @pytest.mark.asyncio
    async def test_it_releases_even_with_no_notifier(self):
        """
        The release must not be hidden inside the `if notifier is not None`
        block — notifier is None in several callers and the money is on the
        ledger either way.
        """
        repo = _Repo()
        await client_bot._place_on_shared_account(
            _Msg(), _P(), MATCHABLE, {"id": 3}, repo, None
        )
        assert repo.released == [_P.utr]


class TestEveryWritingPathReleases:
    def test_record_releases(self):
        src = inspect.getsource(client_bot._record)
        assert "release_held_payments" in src

    def test_the_shared_account_path_releases(self):
        src = inspect.getsource(client_bot._place_on_shared_account)
        assert "release_held_payments" in src

    def test_the_release_comes_after_the_write_succeeded(self):
        """
        Releasing before add_payment returns would drop the hold for a
        payment that was then refused — the worst of both.
        """
        src = inspect.getsource(client_bot._place_on_shared_account)
        add = src.index("await repo.add_payment")
        rel = src.index("release_held_payments")
        dup = src.index('return "duplicate"')
        assert add < rel, "released before it was written"
        assert dup < rel, "the duplicate branch falls through to the release"
