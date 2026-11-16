"""
A rupee on the wrong side of a split is one tap, not a phone call.

    if you can think of something to add for me to do instances like this
    — Bridge, 16 November 2026

He had just spent an evening on this:

    BRAV81   expected 3,549,394   paid 3,549,393   -1
    BRAV82   expected 3,549,393   paid 3,549,394   +1

The pair was exactly right — 7,098,787 either way — and neither order was.
One sat open a rupee short while its twin sat a rupee over. Fixing it took a
hand-written transaction with the figures typed in by a person, which is the
thing this project keeps trying to stop needing.

WHAT MAKES IT SAFE TO HAND HIM

The pair total never moves. Each half is set to what it actually holds, so
the sum is unchanged — the rupee is not written off, it is recorded on the
side it landed on. Three guards, all re-checked inside the transaction
rather than trusted from the screen, which may have been sitting open for an
hour:

  the pair must already sum correctly   otherwise money is genuinely
                                        missing or extra, and this is not a
                                        crossover

  the gap must be small                 a large one is a misallocated
                                        payment, and that is /correct, where
                                        the UTR is named

  there must be exactly two halves      an ordinary trade has nothing to
                                        rebalance against

WHY NO CONFIRMATION STEP

The move cannot change the total, both halves are re-read under FOR UPDATE,
and each gets its own audit entry. A confirmation would add ceremony to
something already impossible to get wrong — and ceremony is what makes him
stop using a tool and message me instead.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.repo import Repo  # noqa: E402


class _Conn:
    """Two halves of a split, and what happened to them."""

    def __init__(self, parts, *, split=True):
        self.parts = parts
        self.split = split
        self.updates = []
        self.audits = []

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return _T()

    async def fetch(self, sql, *args):
        if not self.split:
            return [self.parts[0]]
        return list(self.parts)

    async def execute(self, sql, *args):
        if "UPDATE trades SET inr_expected" in sql:
            self.updates.append((args[0], args[1]))

    async def fetchrow(self, sql, *args):
        return None


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return _A()


def _repo(conn):
    r = Repo.__new__(Repo)
    r.pool = _Pool(conn)

    async def _audit(c, **kw): conn.audits.append(kw)
    r.audit = _audit
    return r


def _part(tid, ref, expected, paid):
    return {"id": tid, "reference": ref,
            "inr_expected": D(expected), "paid": D(paid)}


# The real 16 November figures, masked only in reference.
CROSSED = [_part(213, "REFA", "3549394", "3549393"),
           _part(214, "REFB", "3549393", "3549394")]


class TestTheCrossoverIsFixed:
    @pytest.mark.asyncio
    async def test_each_half_moves_to_what_it_holds(self):
        conn = _Conn(CROSSED)
        ok, msg = await _repo(conn).rebalance_split_pair(213, actor_party_id=1)
        assert ok, msg
        assert conn.updates == [(213, D("3549393")), (214, D("3549394"))]

    @pytest.mark.asyncio
    async def test_the_pair_total_is_untouched(self):
        conn = _Conn(CROSSED)
        await _repo(conn).rebalance_split_pair(213, actor_party_id=1)
        before = sum(p["inr_expected"] for p in CROSSED)
        after = sum(v for _, v in conn.updates)
        assert before == after == D("7098787")

    @pytest.mark.asyncio
    async def test_both_orders_are_written_down(self):
        conn = _Conn(CROSSED)
        await _repo(conn).rebalance_split_pair(213, actor_party_id=1)
        assert len(conn.audits) == 2
        for a in conn.audits:
            assert a["action"] == "trade.split_pair_rebalanced"
            assert a["detail"]["pair_total"] == D("7098787")
            assert "was" in a["detail"] and "now" in a["detail"]

    @pytest.mark.asyncio
    async def test_it_says_the_total_is_unchanged(self):
        """
        The first thing he will want to know is that nothing moved in or
        out, so the reply says it rather than making him check.
        """
        conn = _Conn(CROSSED)
        _, msg = await _repo(conn).rebalance_split_pair(213, actor_party_id=1)
        assert "7,098,787" in msg and "same as before" in msg

    @pytest.mark.asyncio
    async def test_either_half_can_start_it(self):
        conn = _Conn(CROSSED)
        ok, _ = await _repo(conn).rebalance_split_pair(214, actor_party_id=1)
        assert ok


class TestWhatItRefuses:
    @pytest.mark.asyncio
    async def test_a_pair_that_is_genuinely_short(self):
        """
        The guard that matters. Money missing is not a rupee on the wrong
        side, and moving the goalposts would hide it.
        """
        conn = _Conn([_part(1, "A", "500000", "499000"),
                      _part(2, "B", "500000", "500000")])
        ok, msg = await _repo(conn).rebalance_split_pair(1, actor_party_id=1)
        assert not ok
        assert "genuinely short or over" in msg
        assert conn.updates == []

    @pytest.mark.asyncio
    async def test_a_gap_too_large_to_be_a_crossover(self):
        conn = _Conn([_part(1, "A", "500000", "400000"),
                      _part(2, "B", "500000", "600000")])
        ok, msg = await _repo(conn).rebalance_split_pair(1, actor_party_id=1)
        assert not ok
        assert "/correct" in msg
        assert conn.updates == []

    @pytest.mark.asyncio
    async def test_a_trade_that_is_not_half_of_a_split(self):
        conn = _Conn([_part(1, "A", "500000", "499999")], split=False)
        ok, msg = await _repo(conn).rebalance_split_pair(1, actor_party_id=1)
        assert not ok and "not one half of a split" in msg

    @pytest.mark.asyncio
    async def test_a_pair_that_already_matches(self):
        conn = _Conn([_part(1, "A", "500000", "500000"),
                      _part(2, "B", "500000", "500000")])
        ok, msg = await _repo(conn).rebalance_split_pair(1, actor_party_id=1)
        assert not ok and "already match" in msg
        assert conn.updates == []


class TestItIsCheckedWhenItRuns:
    def test_the_rows_are_locked(self):
        """
        The button may have been on screen for an hour. A payment arriving
        in between changes every figure it was drawn from.
        """
        assert "FOR UPDATE" in inspect.getsource(Repo.rebalance_split_pair)

    def test_the_guards_are_inside_the_transaction(self):
        src = inspect.getsource(Repo.rebalance_split_pair)
        begin = src.index("conn.transaction()")
        for guard in ("expected_total != paid_total", "REBALANCE_LIMIT_INR",
                      "len(rows) != 2"):
            assert src.index(guard) > begin, guard

    def test_it_does_not_trust_the_screen(self):
        """
        Nothing from the caller but the trade id and who tapped it.
        """
        params = inspect.signature(Repo.rebalance_split_pair).parameters
        assert set(params) == {"self", "trade_id", "actor_party_id"}


class TestTheButtonOnlyAppearsWhenItApplies:
    def test_the_offer_is_built_from_a_check(self):
        from bot import bridge_trade
        src = inspect.getsource(bridge_trade.correct_pick)
        assert "split_pair_imbalance" in src
        assert "if imbalance is not None:" in src

    def test_the_button_names_the_amount(self):
        from bot import bridge_trade
        src = inspect.getsource(bridge_trade.correct_pick)
        assert "on the wrong side" in src

    def test_the_handler_clears_the_flow(self):
        from bot import bridge_trade
        src = inspect.getsource(bridge_trade.correct_rebalance)
        assert "state.clear()" in src

    def test_the_limit_is_small(self):
        """
        Large enough for a rounding crossover on any split, small enough
        that a real misallocation cannot slip through it.
        """
        assert D("10") <= Repo.REBALANCE_LIMIT_INR <= D("5000")
