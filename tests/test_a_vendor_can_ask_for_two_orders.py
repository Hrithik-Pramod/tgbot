"""
A vendor asks at /send, and their deposit arrives as two equal orders.

THE REQUEST (Bridge, 27 November 2026)

    the option i have to split a trade, can i add that to vendor side
    so Hash, the choose account, then split?
    but not amount, we just offer split 50/50

The Bridge has had this since 1 October — his version lets him put the line
anywhere. The vendor's does not: 50/50, no figure to type, no screen to get
wrong.

WHY THE REQUEST IS REMEMBERED RATHER THAN ACTED ON

    they cannot send without entering hash, so funds are always in transit
    — Bridge

/send is a claim typed while the USDT is still moving, so there is usually
no order yet to divide. The request therefore lands on pending_sends, which
already holds everything else the vendor said when they sent — account,
hash — and waits for the money to make it real.

FRESH ORDERS ONLY, AND THIS IS THE PART THAT MATTERS

    fresh order, so one already there is on its own, this is new
    — Bridge

A deposit arriving while that vendor already has a live order is MERGED into
it. Splitting the merged total would divide figures the Bridge has already
been issuing against: 20,000 open, 10,000 arrives, and a split of the
combined 30,000 silently turns his 20,000 order into two of 15,000.

So "fresh" is asked of the data — a trade carrying exactly one deposit —
rather than remembered from a code path that has an early return in the
middle of it.

NOBODY IS TOLD

    no, as for me it will make no difference, i use the instruction of the
    order — Bridge

So the deposit announcement still names the whole amount that arrived, which
is what the chain says and is true either way, and the split runs after it.
A refusal is logged rather than announced, because a thing nobody is told
about must at least be findable.

THE REQUEST IS SPENT, NOT RETRIED

If the split refuses — instructed or paid in the meantime — the request is
already marked applied. Carrying it forward would divide the NEXT deposit,
which the vendor never asked about, days later, with no prompt in sight.
"""

import pathlib
import sys
from decimal import Decimal as D

import pytest
import pytest_asyncio

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.repo import Repo  # noqa: E402
from tests.sqltext import query_of, table_of  # noqa: E402
from tests.test_integration import DB_URL, SCHEMA  # noqa: E402

pytestmark = pytest.mark.skipif(
    DB_URL is None,
    reason="no PostgreSQL available (pip install pgserver, or set DATABASE_URL)",
)


@pytest_asyncio.fixture
async def repo():
    import asyncpg

    conn = await asyncpg.connect(DB_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.execute(SCHEMA.read_text())
    await conn.close()

    r = await Repo.connect(DB_URL)
    try:
        yield r
    finally:
        await r.close()


@pytest_asyncio.fixture
async def desk(repo):
    bridge = await repo.add_party(
        role="bridge", label="Bridge", display_name=None,
        telegram_chat_id=-1001, telegram_user_id=None, actor_party_id=None)
    client = await repo.add_party(
        role="client", label="Client A", display_name=None,
        telegram_chat_id=-1003, telegram_user_id=None, actor_party_id=bridge)
    supplier = await repo.add_party(
        role="supplier", label="Uncle", display_name=None,
        telegram_chat_id=-1004, telegram_user_id=None, actor_party_id=bridge)

    async with repo.pool.acquire() as conn:
        wallet = await conn.fetchval(
            """
            INSERT INTO wallets (address, is_internal, supplier_id, client_id,
                                 label)
            VALUES ('TExampleUncleAddress00000000000000', TRUE, $1, $2, 'p')
            RETURNING id
            """, supplier, client)

    rate = await repo.set_rate(
        supplier_id=supplier, client_id=client,
        supply_rate=D("106.20"), sell_rate=D("107.20"), set_by=bridge)

    return dict(bridge=bridge, client=client, supplier=supplier,
                wallet=wallet, rate=rate)


async def _open_trade(repo, desk, usdt, *, reference="BRAV1"):
    """
    A trade with one deposit on it, as a fresh deposit produces.

    Priced through split_three_ways rather than with zeros. The first
    version left inr_expected at 0 and every split refused with "that
    leaves nothing for the second part" — correctly, because a parent
    worth nothing cannot be divided. A fixture that is not a real row
    tests the error path and calls it a pass.
    """
    from core.money import split_three_ways

    division = split_three_ways(usdt, D("106.20"), D("106.20"), D("107.20"))

    async with repo.pool.acquire() as conn:
        trade_id = await conn.fetchval(
            """
            INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                                rate_id, supply_rate, buy_rate, sell_rate,
                                status, usdt_received, inr_expected,
                                usdt_owed_client, margin_usdt,
                                supplier_label_at_trade)
            VALUES ($1, $2, $3, $4, $5, 106.20, 106.20, 107.20,
                    'awaiting_payment', $6, $7, $8, $9, 'Uncle')
            RETURNING id
            """, reference, desk["supplier"], desk["client"], desk["wallet"],
            desk["rate"], usdt, division.inr_expected, division.client_usdt,
            division.bridge_usdt)
        deposit_id = await conn.fetchval(
            """
            INSERT INTO deposits (tx_hash, wallet_id, trade_id, amount_usdt,
                                  status)
            VALUES ($1, $2, $3, $4, 'confirmed') RETURNING id
            """, f"hash-{reference}", desk["wallet"], trade_id, usdt)
    return trade_id, deposit_id


@pytest.mark.asyncio
class TestTheRequestIsRemembered:
    async def test_it_attaches_to_the_latest_send(self, repo, desk):
        await repo.record_pending_send(
            supplier_id=desk["supplier"], account_id=None, hash_url="h1")
        assert await repo.request_split_on_latest_send(desk["supplier"])

        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT split_requested, split_applied_at FROM pending_sends "
                "WHERE supplier_id = $1", desk["supplier"])
        assert row["split_requested"] is True
        assert row["split_applied_at"] is None

    async def test_it_says_no_when_there_is_no_send_to_attach_to(
        self, repo, desk
    ):
        """
        The vendor is told. A request silently not stored would be found out
        when the money arrived whole and the order was already live.
        """
        assert await repo.request_split_on_latest_send(desk["supplier"]) is False

    async def test_taking_it_consumes_it(self, repo, desk):
        await repo.record_pending_send(
            supplier_id=desk["supplier"], account_id=None, hash_url="h1")
        await repo.request_split_on_latest_send(desk["supplier"])

        assert await repo.take_split_request(desk["supplier"]) is True
        assert await repo.take_split_request(desk["supplier"]) is False

    async def test_the_record_of_the_request_survives_being_used(
        self, repo, desk
    ):
        """
        Two columns, not one flag cleared on use. "Never asked" and "asked,
        and done" must not become the same row.
        """
        await repo.record_pending_send(
            supplier_id=desk["supplier"], account_id=None, hash_url="h1")
        await repo.request_split_on_latest_send(desk["supplier"])
        await repo.take_split_request(desk["supplier"])

        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT split_requested, split_applied_at FROM pending_sends "
                "WHERE supplier_id = $1", desk["supplier"])
        assert row["split_requested"] is True
        assert row["split_applied_at"] is not None

    async def test_one_vendors_request_is_not_another_vendors(self, repo, desk):
        other = await repo.add_party(
            role="supplier", label="Raj", display_name=None,
            telegram_chat_id=-1009, telegram_user_id=None,
            actor_party_id=desk["bridge"])
        await repo.record_pending_send(
            supplier_id=desk["supplier"], account_id=None, hash_url="h1")
        await repo.request_split_on_latest_send(desk["supplier"])

        assert await repo.take_split_request(other) is False
        assert await repo.take_split_request(desk["supplier"]) is True

    async def test_applied_without_asked_is_refused_by_the_database(
        self, repo, desk
    ):
        import asyncpg
        await repo.record_pending_send(
            supplier_id=desk["supplier"], account_id=None, hash_url="h1")
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            async with repo.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE pending_sends SET split_applied_at = now() "
                    "WHERE supplier_id = $1", desk["supplier"])


@pytest.mark.asyncio
class TestFreshOrdersOnly:
    async def test_a_single_deposit_order_is_fresh(self, repo, desk):
        trade_id, deposit_id = await _open_trade(repo, desk, D("10000"))
        fresh = await repo.deposit_opened_a_fresh_trade(deposit_id)
        assert fresh is not None
        assert fresh["trade_id"] == trade_id

    async def test_a_merged_deposit_is_not(self, repo, desk):
        """
        The case that would have moved figures under the Bridge: 20,000
        already open, 10,000 joins it, and the order is now 30,000. Neither
        deposit may trigger a split.
        """
        trade_id, first = await _open_trade(repo, desk, D("20000"))
        async with repo.pool.acquire() as conn:
            second = await conn.fetchval(
                """
                INSERT INTO deposits (tx_hash, wallet_id, trade_id,
                                      amount_usdt, status)
                VALUES ('hash-merge', $1, $2, 10000, 'confirmed') RETURNING id
                """, desk["wallet"], trade_id)
            await conn.execute(
                "UPDATE trades SET usdt_received = 30000 WHERE id = $1",
                trade_id)

        assert await repo.deposit_opened_a_fresh_trade(second) is None
        assert await repo.deposit_opened_a_fresh_trade(first) is None

    async def test_a_deposit_on_no_trade_is_not_fresh(self, repo, desk):
        async with repo.pool.acquire() as conn:
            orphan = await conn.fetchval(
                """
                INSERT INTO deposits (tx_hash, wallet_id, amount_usdt, status)
                VALUES ('hash-orphan', $1, 500, 'confirmed') RETURNING id
                """, desk["wallet"])
        assert await repo.deposit_opened_a_fresh_trade(orphan) is None


@pytest.mark.asyncio
class TestTheSplitItself:
    async def test_an_even_amount_halves(self, repo, desk):
        trade_id, _ = await _open_trade(repo, desk, D("10000"))
        ok, msg, detail = await repo.split_trade(
            trade_id=trade_id, actor_party_id=desk["supplier"],
            first_usdt=D("5000.00"))
        assert ok, msg
        assert detail["first"]["usdt"] == D("5000.00")
        assert detail["second"]["usdt"] == D("5000.00")

    async def test_an_odd_amount_still_accounts_for_every_cent(
        self, repo, desk
    ):
        """
        9,650.508 is a real deposit from 27 November. Half of it does not
        land on a cent.

        THE PARTS SUM TO THE ROUNDED TOTAL, NOT THE DEPOSIT

        4,825.25 + 4,825.26 = 9,650.51, against a deposit of 9,650.508. Two
        tenths of a cent sits in the parent that neither child holds.

        That is split_trade's documented behaviour, not a defect found
        here: USDT is carried to six decimals on a deposit and to two on an
        order, so a deposit whose third decimal is non-zero cannot be
        divided into two 2dp amounts that add back to it. The alternative —
        carrying the remainder on one child — would make that child's own
        figure unroundable and push the same problem onto the client's
        payout.

        Asserted so the size of the gap is on record. 0.002 USDT is about a
        fifth of a cent; if it ever grows past rounding it is a bug.
        """
        from core.money import round_usdt

        total = D("9650.508")
        trade_id, _ = await _open_trade(repo, desk, total)
        first = round_usdt(total / 2)

        ok, msg, detail = await repo.split_trade(
            trade_id=trade_id, actor_party_id=desk["supplier"],
            first_usdt=first)
        assert ok, msg

        parts = detail["first"]["usdt"] + detail["second"]["usdt"]
        assert parts == round_usdt(total)
        assert abs(parts - total) <= D("0.005")

    async def test_it_refuses_once_instructed(self, repo, desk):
        """
        The same guard the Bridge's button has. A vendor must not reshape an
        order the client has already been told to pay.
        """
        trade_id, _ = await _open_trade(repo, desk, D("10000"))
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE trades SET instructed_at = now() WHERE id = $1",
                trade_id)

        ok, msg, _ = await repo.split_trade(
            trade_id=trade_id, actor_party_id=desk["supplier"],
            first_usdt=D("5000.00"))
        assert not ok
        assert "client" in msg.lower()


class _StubRepo:
    """
    Enough repo for _apply_vendor_split, recording what it was asked to do.

    These exist because a mutation proved the integration tests above could
    not see the guard that matters: deleting the merged-deposit check from
    monitor/tron.py left all eighteen of them passing. They test that
    deposit_opened_a_fresh_trade answers correctly, not that the monitor
    acts on the answer.
    """

    def __init__(self, fresh, has_request=True):
        self._fresh = fresh
        self._has_request = has_request
        self.taken = False
        self.split_calls = []

    async def deposit_opened_a_fresh_trade(self, deposit_id):
        return self._fresh

    async def take_split_request(self, supplier_id):
        if not self._has_request:
            return False
        self.taken = True
        return True

    async def split_trade(self, *, trade_id, actor_party_id, first_usdt):
        self.split_calls.append(
            dict(trade_id=trade_id, actor=actor_party_id, first=first_usdt))
        return True, "ok", {
            "first": {"reference": "BRAV1", "usdt": first_usdt},
            "second": {"reference": "BRAV2", "usdt": first_usdt},
        }


def _monitor(repo):
    from monitor.tron import DepositMonitor

    m = DepositMonitor.__new__(DepositMonitor)
    m.repo = repo
    return m


FRESH = {"trade_id": 7, "supplier_id": 3, "reference": "BRAV1",
         "usdt_received": D("10000")}


@pytest.mark.asyncio
class TestTheMonitorActsOnTheAnswer:
    async def test_a_fresh_order_is_split_in_half(self):
        repo = _StubRepo(FRESH)
        await _monitor(repo)._apply_vendor_split(deposit_id=1)

        assert repo.split_calls == [
            dict(trade_id=7, actor=3, first=D("5000.00"))
        ]

    async def test_a_merged_deposit_is_left_alone(self, caplog):
        """
        The guard two mutations walked straight through.

            fresh order, so one already there is on its own, this is new
            — Bridge

        WHY THIS ASSERTS ON THE LOG

        _apply_vendor_split is wrapped in `except Exception`, so the monitor
        survives a failure — which also means a broken guard looks exactly
        like a working one from outside. Removing `if fresh is None: return`
        makes the next line raise on a None row; the except catches it, no
        split happens, and `split_calls == []` still holds. The mutation
        passed twice before this line was added.

        A correct implementation returns quietly. A broken one logs an
        exception. That is the only visible difference, so that is the
        assertion.
        """
        import logging

        repo = _StubRepo(None)
        with caplog.at_level(logging.ERROR, logger="monitor.tron"):
            await _monitor(repo)._apply_vendor_split(deposit_id=1)

        assert repo.split_calls == []
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
            "a merged deposit should return quietly, not raise and be caught"
        )

    async def test_a_merged_deposit_does_not_spend_the_request(self):
        """
        It must still be waiting for the deposit that DOES open an order.
        Consuming it here would lose the vendor's request entirely.
        """
        repo = _StubRepo(None)
        await _monitor(repo)._apply_vendor_split(deposit_id=1)

        assert repo.taken is False

    async def test_nothing_happens_without_a_request(self):
        repo = _StubRepo(FRESH, has_request=False)
        await _monitor(repo)._apply_vendor_split(deposit_id=1)

        assert repo.split_calls == []

    async def test_the_request_is_checked_only_after_freshness(self):
        """
        Order matters: asking for the request first would consume it on a
        merged deposit and then decline to use it.
        """
        repo = _StubRepo(None)
        await _monitor(repo)._apply_vendor_split(deposit_id=1)
        assert repo.taken is False

    async def test_an_exploding_repo_does_not_stop_the_monitor(self):
        class _Boom(_StubRepo):
            async def deposit_opened_a_fresh_trade(self, deposit_id):
                raise RuntimeError("database gone")

        await _monitor(_Boom(FRESH))._apply_vendor_split(deposit_id=1)

    async def test_an_odd_total_halves_to_two_decimals(self):
        repo = _StubRepo({**FRESH, "usdt_received": D("9650.508")})
        await _monitor(repo)._apply_vendor_split(deposit_id=1)

        assert repo.split_calls[0]["first"] == D("4825.25")


class TestTheShapeOfIt:
    def test_the_fresh_check_counts_deposits(self):
        """
        Read as SQL, not as the prose around it — this file says
        "exactly one deposit" in English several times over.
        """
        sql = query_of(Repo.deposit_opened_a_fresh_trade)
        assert "count(*)" in sql
        assert "= 1" in sql

    def test_taking_the_request_is_one_statement(self):
        """
        Read-then-write would let two polls both decide to split the same
        order. The UPDATE ... RETURNING is what makes exactly one caller win.
        """
        sql = query_of(Repo.take_split_request)
        assert "UPDATE pending_sends" in sql
        assert "RETURNING" in sql
        assert "FOR UPDATE SKIP LOCKED" in sql

    def test_the_schema_carries_both_columns_and_the_check(self):
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        body = table_of(schema, "pending_sends")
        assert "split_requested" in body
        assert "split_applied_at" in body
        assert "pending_sends_split_shape" in body

    def test_the_vendor_is_offered_it_only_with_nothing_open(self):
        """
        The button is absent exactly where the answer would have to be no.
        It sits in the `else` of `if attached:` — attached meaning a live
        order the deposit will merge into.
        """
        import inspect

        from bot import supplier_bot
        src = inspect.getsource(supplier_bot.send_account)
        assert "ssplit:yes" in src
        head, _, tail = src.partition("if attached:")
        body, _, otherwise = tail.partition("else:")
        assert "ssplit:yes" not in body
        assert "ssplit:yes" in otherwise

    def test_the_monitor_splits_after_announcing(self):
        """
        The announcement states what arrived on chain. Splitting first would
        have it quote half of what was sent.
        """
        import inspect

        from monitor.tron import DepositMonitor
        src = inspect.getsource(DepositMonitor._handle_deposit)
        assert src.index("on_supplier_deposit") < src.index("_apply_vendor_split")

    def test_a_failing_split_cannot_stop_the_monitor(self):
        import inspect

        from monitor.tron import DepositMonitor
        src = inspect.getsource(DepositMonitor._apply_vendor_split)
        assert "except Exception" in src
        assert "log.exception" in src
