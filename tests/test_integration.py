"""
Integration tests against a real PostgreSQL database.

Everything else in the suite tests pure logic. This file is the only place the
schema and db/repo.py actually execute, which makes it the only place that can
catch a broken query, a wrong column name, or a constraint that does not do what
the comment above it claims.

It walks a complete trade using representative figures — the six tranches
totalling ₹1,499,297 — through the real code path: deposit, rate lookup, trade
creation, slot issuance, payments, duplicate rejection, and completion.

Skipped automatically if no database is available, so the suite still runs
anywhere. To run these:

    pip install pgserver          # bundles its own PostgreSQL, no root needed
    pytest tests/test_integration.py -v

or point DATABASE_URL at any PostgreSQL instance.
"""

from __future__ import annotations

import os
import pathlib
import sys
from decimal import Decimal as D

import pytest
import pytest_asyncio

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.money import inr_to_usdt, margin_usdt, usdt_to_inr  # noqa: E402
from core.summary import Payment, render_trade_summary  # noqa: E402
from db.repo import Repo  # noqa: E402

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "db" / "schema.sql"


def _database_url() -> str | None:
    """A live database from the environment, or a bundled one, or nothing."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    try:
        import pgserver
    except ImportError:
        return None
    data = pathlib.Path("/tmp/pgdata_test")
    data.mkdir(exist_ok=True)
    srv = pgserver.get_server(data)
    return srv.get_uri()


DB_URL = _database_url()
pytestmark = pytest.mark.skipif(
    DB_URL is None,
    reason="no PostgreSQL available (pip install pgserver, or set DATABASE_URL)",
)


@pytest_asyncio.fixture
async def repo():
    """A fresh schema per test, so no test can depend on another's leftovers."""
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
async def world(repo):
    """The client's real configuration: two suppliers, one client, real rates."""
    bridge = await repo.add_party(
        role="bridge", label="Bridge", display_name="Bridge Control",
        telegram_chat_id=-1001, telegram_user_id=None, actor_party_id=None)
    supplier = await repo.add_party(
        role="supplier", label="Supplier A", display_name="Northgate",
        telegram_chat_id=-1002, telegram_user_id=None, actor_party_id=bridge)
    client = await repo.add_party(
        role="client", label="Client A", display_name="Client A Exchange",
        telegram_chat_id=-1003, telegram_user_id=None, actor_party_id=bridge)

    async with repo.pool.acquire() as conn:
        wallet = await conn.fetchval(
            """
            INSERT INTO wallets (address, is_internal, supplier_id, client_id, label)
            VALUES ('TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb', TRUE, $1, $2, 'A/A')
            RETURNING id
            """, supplier, client)

    rate = await repo.set_rate(
        supplier_id=supplier, client_id=client,
        supply_rate=D("105.50"), sell_rate=D("106.50"), set_by=bridge)

    return dict(bridge=bridge, supplier=supplier, client=client,
                wallet=wallet, rate=rate)


# ---------------------------------------------------------------- basics

class TestSchemaAndParties:
    @pytest.mark.asyncio
    async def test_schema_applies_cleanly(self, repo):
        async with repo.pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        names = {t["tablename"] for t in tables}
        assert {"parties", "wallets", "rates", "trades", "deposits",
                "payments", "payment_slots", "audit_log",
                "monitor_state", "supplier_counters"} <= names

    @pytest.mark.asyncio
    async def test_party_lookup_is_by_chat(self, world, repo):
        found = await repo.party_by_chat_id(-1002)
        assert found["label"] == "Supplier A"
        assert found["role"] == "supplier"
        assert await repo.party_by_chat_id(-9999) is None

    @pytest.mark.asyncio
    async def test_one_chat_cannot_map_to_two_parties(self, world, repo):
        import asyncpg
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.add_party(
                role="client", label="Client Z", display_name="x",
                telegram_chat_id=-1002, telegram_user_id=None,
                actor_party_id=world["bridge"])


class TestRates:
    @pytest.mark.asyncio
    async def test_current_rate_returns_the_newest(self, world, repo):
        await repo.set_rate(
            supplier_id=world["supplier"], client_id=world["client"],
            supply_rate=D("108"), sell_rate=D("109"), set_by=world["bridge"])
        current = await repo.current_rate(world["supplier"], world["client"])
        assert current["supply_rate"] == D("108")

    @pytest.mark.asyncio
    async def test_rate_history_is_kept(self, world, repo):
        await repo.set_rate(
            supplier_id=world["supplier"], client_id=world["client"],
            supply_rate=D("108"), sell_rate=D("109"), set_by=world["bridge"])
        async with repo.pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM rates")
        assert n == 2, "rates must be append-only so old trades stay reproducible"


class TestDeposits:
    @pytest.mark.asyncio
    async def test_deposit_recorded_once(self, world, repo):
        first = await repo.record_deposit(
            tx_hash="abc", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address="TFrom", block_number=1)
        second = await repo.record_deposit(
            tx_hash="abc", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address="TFrom", block_number=1)
        assert first is not None
        assert second is None, "the unique index must absorb a redelivered tx"

    @pytest.mark.asyncio
    async def test_decimal_survives_the_round_trip(self, world, repo):
        """A float column would corrupt this. NUMERIC must not."""
        await repo.record_deposit(
            tx_hash="precise", wallet_id=world["wallet"],
            amount_usdt=D("14211.353614"), from_address=None, block_number=None)
        async with repo.pool.acquire() as conn:
            got = await conn.fetchval(
                "SELECT amount_usdt FROM deposits WHERE tx_hash = 'precise'")
        assert got == D("14211.353614")
        assert isinstance(got, D)

    @pytest.mark.asyncio
    async def test_monitor_cursor_only_moves_forward(self, world, repo):
        await repo.adopt_wallet(world["wallet"], cursor_ms=1000, adopted_at_ms=1000)
        await repo.set_monitor_cursor(world["wallet"], 5000)
        await repo.set_monitor_cursor(world["wallet"], 3000)
        async with repo.pool.acquire() as conn:
            got = await conn.fetchval(
                "SELECT last_timestamp_ms FROM monitor_state WHERE wallet_id = $1",
                world["wallet"])
        assert got == 5000, "GREATEST() must stop the cursor rewinding"

    @pytest.mark.asyncio
    async def test_a_cursor_never_creates_the_row(self, world, repo):
        """
        Only adoption may create monitor_state. An in-flight poll cycle
        holding a stale cursor used to recreate the row after /walletchange
        deleted it — leaving a cursor with no adoption baseline, which is
        indistinguishable from an established wallet and disables the history
        guard. Supplier D's wallet spent four hours like that on 15 September.
        """
        await repo.set_monitor_cursor(world["wallet"], 5000)
        async with repo.pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM monitor_state WHERE wallet_id = $1",
                world["wallet"]) == 0


# ------------------------------------------------- the full trade cycle

class TestFullTradeLifecycle:
    """A representative trade, start to finish, through the real code."""

    @pytest.mark.asyncio
    async def test_complete_trade(self, world, repo):
        supply, sell = D("105.50"), D("106.50")
        inr_target = D("1499297")
        usdt_in = D("14211.35")

        # --- supplier registers two accounts
        acct1 = await repo.add_bank_account(
            party_id=world["supplier"], account_name="Alpha Traders",
            account_number="123456789012345", ifsc="EXBK0001234")
        acct2 = await repo.add_bank_account(
            party_id=world["supplier"], account_name="Alpha Traders Pvt Ltd",
            account_number="123456789012346", ifsc="EXBK0001234")
        assert len(await repo.list_bank_accounts(world["supplier"])) == 2

        # --- deposit lands
        deposit = await repo.record_deposit(
            tx_hash="tx_real", wallet_id=world["wallet"],
            amount_usdt=usdt_in, from_address="TSupplier", block_number=100)
        assert deposit is not None

        # --- trade opens with a reference and snapshotted rates
        async with repo.pool.acquire() as conn:
            async with conn.transaction():
                ref = await repo.next_reference(conn, world["supplier"])
                inr_expected = usdt_to_inr(usdt_in, supply)
                owed = inr_to_usdt(inr_expected, sell)
                trade_id = await conn.fetchval(
                    """
                    INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                        rate_id, supply_rate, sell_rate, usdt_received, inr_expected,
                        usdt_owed_client, margin_usdt, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'awaiting_payment')
                    RETURNING id
                    """,
                    ref, world["supplier"], world["client"], world["wallet"],
                    world["rate"], supply, sell, usdt_in, inr_expected, owed,
                    margin_usdt(usdt_in, supply, sell))
        assert ref == "SUPA1"

        # --- supplier nominates an account
        attached = await repo.nominate_account(
            supplier_id=world["supplier"], account_id=acct2,
            actor_party_id=world["supplier"])
        assert attached == "SUPA1"
        detail = await repo.trade_detail(trade_id)
        assert detail["nominated_account_id"] == acct2

        # --- bridge issues the payment slot
        await repo.issue_slots(
            trade_id=trade_id, slots=[(acct2, inr_expected)],
            actor_party_id=world["bridge"])
        slots = await repo.trade_slots(trade_id)
        assert len(slots) == 1
        assert slots[0]["account_name"] == "Alpha Traders Pvt Ltd"

        # --- client logs the six real tranches
        tranches = [
            ("EXBKR12026090700002248", D("229000"), acct1),
            ("EXBKR12026090700002326", D("230000"), acct1),
            ("EXBKR12026090700000845", D("246000"), acct2),
            ("EXBKR12026090700002570", D("263000"), acct2),
            ("EXBKR12026090700002625", D("291000"), acct2),
            ("EXBKR12026090700001317", D("240297"), acct2),
        ]
        for utr, amount, acct in tranches:
            ok, msg = await repo.add_payment(
                trade_id=trade_id, utr=utr, amount_inr=amount,
                beneficiary_account_id=acct, added_by=world["client"])
            assert ok, msg

        # --- the total must match the client's own figure exactly
        assert await repo.trade_paid_total(trade_id) == inr_target

        # --- a duplicate UTR is refused (E3)
        ok, msg = await repo.add_payment(
            trade_id=trade_id, utr="EXBKR12026090700002248", amount_inr=D("1"),
            beneficiary_account_id=acct1, added_by=world["client"])
        assert not ok
        assert "already been recorded" in msg
        assert await repo.trade_paid_total(trade_id) == inr_target, \
            "a rejected duplicate must not change the total"

        # --- the summary renders in the client's exact format
        rows = await repo.trade_payments(trade_id)
        summary = render_trade_summary(
            [Payment(r["utr"], r["amount_inr"], r["account_name"]) for r in rows],
            reference="SUPA1")
        assert "229000+230000+246000+263000+291000+240297 = 1,499,297" in summary
        assert "to Alpha Traders Pvt Ltd" in summary

        # --- close it
        await repo.complete_trade(trade_id, world["client"])
        assert (await repo.trade_detail(trade_id))["status"] == "completed"

        # --- references continue rather than resetting (E2)
        async with repo.pool.acquire() as conn:
            assert await repo.next_reference(conn, world["supplier"]) == "SUPA2"


class TestLifecycleGuards:
    @pytest.mark.asyncio
    async def test_two_uninstructed_trades_may_share_a_wallet(self, world, repo):
        """
        This asserted the opposite until 15 September, when the unique index
        was deliberately dropped.

        11 September: 1,859 USDT opened SUPB1, the client was instructed to
        pay ₹197,054, and a 3,000 USDT deposit ten minutes later grew the SAME
        trade to ₹515,054. The client's rule is that once a trade is issued,
        any new deposit is a new trade — so a stale trade and a fresh one have
        to be able to coexist on one wallet. The guarantee moved into the
        merge-window query; the index that remains is for lookup, not to
        constrain.
        """
        async def open_trade(ref):
            async with repo.pool.acquire() as conn:
                return await conn.fetchval(
                    """
                    INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                        rate_id, supply_rate, sell_rate, status)
                    VALUES ($1,$2,$3,$4,$5,105.50,105.00,'open') RETURNING id
                    """, ref, world["supplier"], world["client"],
                    world["wallet"], world["rate"])

        first = await open_trade("SUPB1")
        second = await open_trade("SUPB2")
        assert first != second

    @pytest.mark.asyncio
    async def test_cancel_then_reopen_guard(self, world, repo):
        async with repo.pool.acquire() as conn:
            tid = await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,105.00,'open') RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])

        ok, msg = await repo.cancel_trade(
            trade_id=tid, actor_party_id=world["bridge"], reason="test")
        assert ok and "cancelled" in msg

        # Cancelling twice is refused rather than silently repeated.
        ok, msg = await repo.cancel_trade(
            trade_id=tid, actor_party_id=world["bridge"], reason="again")
        assert not ok

        # A cancelled trade is not "completed", so it cannot be reopened.
        ok, msg = await repo.reopen_trade(
            trade_id=tid, actor_party_id=world["bridge"], reason="x")
        assert not ok and "not completed" in msg

    @pytest.mark.asyncio
    async def test_reopen_blocked_when_wallet_already_busy(self, world, repo):
        """The guard that stops a reopen violating the one-open-trade index."""
        async with repo.pool.acquire() as conn:
            done = await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status, completed_at)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,105.00,'completed',now())
                RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
            await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status)
                VALUES ('SUPA2',$1,$2,$3,$4,105.50,105.00,'open') RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])

        ok, msg = await repo.reopen_trade(
            trade_id=done, actor_party_id=world["bridge"], reason="fix")
        assert not ok
        assert "SUPA2" in msg, "the message should name the trade that is in the way"

    @pytest.mark.asyncio
    async def test_void_payment_audits_before_deleting(self, world, repo):
        async with repo.pool.acquire() as conn:
            tid = await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,105.00,'open') RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
        acct = await repo.add_bank_account(
            party_id=world["supplier"], account_name="Alpha Traders",
            account_number="123456789012345", ifsc="EXBK0001234")
        await repo.add_payment(
            trade_id=tid, utr="WRONGUTR123", amount_inr=D("229000"),
            beneficiary_account_id=acct, added_by=world["client"])

        rows = await repo.trade_payment_rows(tid)
        ok, msg = await repo.void_payment(
            payment_id=rows[0]["id"], actor_party_id=world["bridge"],
            reason="client typo")
        assert ok

        assert await repo.trade_paid_total(tid) == 0

        # The row is gone but the record of it must survive.
        async with repo.pool.acquire() as conn:
            entry = await conn.fetchrow(
                "SELECT detail FROM audit_log WHERE action = 'payment.void'")
        assert entry is not None
        import json
        detail = json.loads(entry["detail"])
        assert detail["utr"] == "WRONGUTR123"
        assert detail["reason"] == "client typo"

    @pytest.mark.asyncio
    async def test_voided_utr_can_be_reentered(self, world, repo):
        """The point of deleting rather than flagging: the UTR is free again."""
        async with repo.pool.acquire() as conn:
            tid = await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,105.00,'open') RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
        acct = await repo.add_bank_account(
            party_id=world["supplier"], account_name="Alpha Traders",
            account_number="123456789012345", ifsc="EXBK0001234")

        await repo.add_payment(trade_id=tid, utr="UTR123456", amount_inr=D("100"),
                               beneficiary_account_id=acct, added_by=world["client"])
        rows = await repo.trade_payment_rows(tid)
        await repo.void_payment(payment_id=rows[0]["id"],
                                actor_party_id=world["bridge"], reason="wrong amount")

        ok, _ = await repo.add_payment(
            trade_id=tid, utr="UTR123456", amount_inr=D("229000"),
            beneficiary_account_id=acct, added_by=world["client"])
        assert ok
        assert await repo.trade_paid_total(tid) == D("229000")


class TestReprice:
    """
    The guards run against a real row under a real lock. Everything in
    tests/test_reprice.py proves the rules are written down; this proves they
    execute.

    16 September 2026: the rate moved, nothing could apply it, and /cancel was
    the only lever. The cancelled trades took their suppliers' accounts out of
    the client's matcher and the slips stopped landing.
    """

    @pytest_asyncio.fixture
    async def trade(self, world, repo):
        """37,736 USDT opened at 105.50 / 106.50 — ₹3,981,148 expected."""
        usdt = D("37736")
        async with repo.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, usdt_received, inr_expected,
                    usdt_owed_client, margin_usdt, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,106.50,$5,$6,$7,$8,'awaiting_payment')
                RETURNING id
                """,
                world["supplier"], world["client"], world["wallet"], world["rate"],
                usdt, usdt_to_inr(usdt, D("105.50")),
                inr_to_usdt(usdt_to_inr(usdt, D("105.50")), D("106.50")),
                margin_usdt(usdt, D("105.50"), D("106.50")))

    async def _new_rate(self, world, repo):
        return await repo.set_rate(
            supplier_id=world["supplier"], client_id=world["client"],
            supply_rate=D("106"), sell_rate=D("107.50"), set_by=world["bridge"])

    @pytest.mark.asyncio
    async def test_it_applies_the_rate_now_in_force(self, world, repo, trade):
        await self._new_rate(world, repo)

        ok, msg, detail = await repo.reprice_trade(
            trade_id=trade, actor_party_id=world["bridge"])
        assert ok, msg

        # 37,736 x 106 = 4,000,016 — the figure paid out by hand that day.
        assert detail["new_inr"] == D("4000016")
        assert detail["new_owed"] == D("37209.45")

        row = await repo.trade_detail(trade)
        assert row["inr_expected"] == D("4000016")
        assert row["supply_rate"] == D("106")
        assert row["sell_rate"] == D("107.50")
        assert row["margin_usdt"] == D("526.55")

    @pytest.mark.asyncio
    async def test_the_trade_now_points_at_the_new_rate_row(self, world, repo, trade):
        """
        Not just the snapshotted numbers — rate_id too, or the trade claims to
        be priced on a rate whose figures it no longer carries.
        """
        new_rate = await self._new_rate(world, repo)
        await repo.reprice_trade(trade_id=trade, actor_party_id=world["bridge"])
        row = await repo.trade_detail(trade)
        assert row["rate_id"] == new_rate

    @pytest.mark.asyncio
    async def test_the_old_rate_row_survives(self, world, repo, trade):
        """Rates are append-only. A reprice must not rewrite history either."""
        await self._new_rate(world, repo)
        await repo.reprice_trade(trade_id=trade, actor_party_id=world["bridge"])
        async with repo.pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM rates") == 2

    @pytest.mark.asyncio
    async def test_an_issued_trade_is_refused(self, world, repo, trade):
        """The 11 September fault. The client is holding ₹3,981,148."""
        await self._new_rate(world, repo)
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE trades SET instructed_at = now() WHERE id = $1", trade)

        ok, msg, detail = await repo.reprice_trade(
            trade_id=trade, actor_party_id=world["bridge"])
        assert not ok
        assert detail is None
        assert "issued" in msg

        row = await repo.trade_detail(trade)
        assert row["inr_expected"] == D("3981148"), "the figure must not move"

    @pytest.mark.asyncio
    async def test_a_part_paid_trade_is_refused(self, world, repo, trade):
        await self._new_rate(world, repo)
        acct = await repo.add_bank_account(
            party_id=world["supplier"], account_name="Alpha Traders",
            account_number="123456789012345", ifsc="EXBK0001234")
        await repo.add_payment(
            trade_id=trade, utr="BKIDR12026091500003075",
            amount_inr=D("218016"), beneficiary_account_id=acct,
            added_by=world["client"])

        ok, msg, _ = await repo.reprice_trade(
            trade_id=trade, actor_party_id=world["bridge"])
        assert not ok
        assert "218,016" in msg

        row = await repo.trade_detail(trade)
        assert row["inr_expected"] == D("3981148")

    @pytest.mark.asyncio
    async def test_a_cancelled_trade_is_refused(self, world, repo, trade):
        await self._new_rate(world, repo)
        await repo.cancel_trade(
            trade_id=trade, actor_party_id=world["bridge"], reason="test")
        ok, msg, _ = await repo.reprice_trade(
            trade_id=trade, actor_party_id=world["bridge"])
        assert not ok
        assert "cancelled" in msg

    @pytest.mark.asyncio
    async def test_repricing_onto_the_same_rate_is_refused(self, world, repo, trade):
        """
        Nothing has changed, so there is nothing to apply. Saying so points him
        at /setrate rather than leaving him to conclude the command is broken.
        """
        ok, msg, _ = await repo.reprice_trade(
            trade_id=trade, actor_party_id=world["bridge"])
        assert not ok
        assert "/setrate" in msg

    @pytest.mark.asyncio
    async def test_it_is_written_to_the_audit_log(self, world, repo, trade):
        await self._new_rate(world, repo)
        await repo.reprice_trade(trade_id=trade, actor_party_id=world["bridge"])
        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT actor_party_id, detail FROM audit_log "
                "WHERE action = 'trade.reprice' AND entity_id = $1", trade)
        assert row is not None
        assert row["actor_party_id"] == world["bridge"]
        import json
        detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"]
        assert detail["from_inr"] == "3981148.00" or D(str(detail["from_inr"])) == D("3981148")
        assert D(str(detail["to_inr"])) == D("4000016")

    @pytest.mark.asyncio
    async def test_the_picker_offers_it_and_then_does_not(self, world, repo, trade):
        """repriceable_trades before and after, through the real query."""
        await self._new_rate(world, repo)

        rows = {t["reference"]: t for t in await repo.repriceable_trades()}
        assert rows["SUPA1"]["current_supply_rate"] == D("106")
        assert rows["SUPA1"]["current_rate_id"] != rows["SUPA1"]["rate_id"]
        assert rows["SUPA1"]["paid_inr"] == 0

        await repo.reprice_trade(trade_id=trade, actor_party_id=world["bridge"])

        rows = {t["reference"]: t for t in await repo.repriceable_trades()}
        assert rows["SUPA1"]["current_rate_id"] == rows["SUPA1"]["rate_id"], (
            "a trade already on the newest rate must stop being offered")

    @pytest.mark.asyncio
    async def test_a_pairing_with_no_rate_is_still_listed(self, world, repo, trade):
        """
        The LEFT JOIN LATERAL has to survive a pairing with no rate of its own.
        An inner join would drop that trade from the list silently — and being
        dropped from a list without explanation is the failure mode this whole
        command exists to end.

        Reachable: a trade opened by repair SQL against a pairing that was
        never given its own rate. SUPB1 was split by hand on 11 September.
        """
        other = await repo.add_party(
            role="supplier", label="Supplier B", display_name="Southgate",
            telegram_chat_id=-1099, telegram_user_id=None,
            actor_party_id=world["bridge"])
        async with repo.pool.acquire() as conn:
            wallet = await conn.fetchval(
                """
                INSERT INTO wallets (address, is_internal, supplier_id, client_id, label)
                VALUES ('TB1oAaVN9LX3mEJmNTRxKbHSFhPRvFuqmM', TRUE, $1, $2, 'B/A')
                RETURNING id
                """, other, world["client"])
            await conn.execute(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status)
                VALUES ('SUPB1',$1,$2,$3,$4,105.50,106.50,'open')
                """, other, world["client"], wallet, world["rate"])

        rows = {t["reference"]: t for t in await repo.repriceable_trades()}
        assert "SUPB1" in rows, "an inner join would have hidden it"
        assert rows["SUPB1"]["current_rate_id"] is None


class TestPriorPayoutGuard:
    """
    The two-hop join from a trade to its pairing's payout wallet, run for
    real. 18 September 2026: 9,243 USDT had already gone out on two reopened
    trades, and the issue flow was about to hand over the figures again.
    """

    @pytest_asyncio.fixture
    async def paired(self, world, repo):
        """A client payout wallet, linked to the internal one."""
        async with repo.pool.acquire() as conn:
            payout = await conn.fetchval(
                """
                INSERT INTO wallets (address, is_internal, owner_party_id, label)
                VALUES ('THazeProperPay11111111111111111111', FALSE, $1, 'client')
                RETURNING id
                """, world["client"])
            await conn.execute(
                "UPDATE wallets SET payout_wallet_id = $2 WHERE id = $1",
                world["wallet"], payout)
            trade = await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, usdt_received, inr_expected,
                    usdt_owed_client, margin_usdt, status)
                VALUES ('SUPB5',$1,$2,$3,$4,106.20,107.70,2354,249995,2321.22,32.78,
                        'awaiting_payment')
                RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
        await repo.record_deposit(
            tx_hash="supplier_in", wallet_id=world["wallet"],
            amount_usdt=D("2354"), from_address="TMalegao", block_number=1)
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE deposits SET trade_id = $1 WHERE tx_hash = 'supplier_in'",
                trade)
        return {"trade": trade, "payout": payout}

    async def _payout(self, repo, paired, amount, tx="paid_out"):
        await repo.record_deposit(
            tx_hash=tx, wallet_id=paired["payout"],
            amount_usdt=amount, from_address=None, block_number=2)

    @pytest.mark.asyncio
    async def test_nothing_out_yet_means_nothing_to_say(self, repo, paired):
        assert await repo.prior_payouts_for_trade(paired["trade"]) == []

    @pytest.mark.asyncio
    async def test_the_payment_already_made_is_found(self, repo, paired):
        """2,321.21 paid by hand against 2,321.22 owed — one paisa apart."""
        await self._payout(repo, paired, D("2321.21"))
        found = await repo.prior_payouts_for_trade(paired["trade"])
        assert len(found) == 1
        assert found[0]["amount_usdt"] == D("2321.21")
        assert found[0]["tx_hash"] == "paid_out"

    @pytest.mark.asyncio
    async def test_another_vendors_payout_is_not_swept_in(self, repo, paired):
        """
        One client, one payout wallet, every supplier. Five unrelated
        transfers sat in that window on the day; none may be reported.
        """
        for i, amt in enumerate(["18569.71", "6922.00", "32497",
                                 "32497.99", "46425.27"]):
            await self._payout(repo, paired, D(amt), tx=f"other{i}")
        assert await repo.prior_payouts_for_trade(paired["trade"]) == []

    @pytest.mark.asyncio
    async def test_a_payout_before_the_deposit_is_not_this_trades(
        self, repo, paired
    ):
        await self._payout(repo, paired, D("2321.21"), tx="earlier")
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE deposits SET detected_at = now() - interval '2 days' "
                "WHERE tx_hash = 'earlier'")
        assert await repo.prior_payouts_for_trade(paired["trade"]) == []

    @pytest.mark.asyncio
    async def test_a_transfer_that_has_a_trade_is_not_a_payout(
        self, repo, paired
    ):
        await self._payout(repo, paired, D("2321.21"), tx="claimed")
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE deposits SET trade_id = $1 WHERE tx_hash = 'claimed'",
                paired["trade"])
        assert await repo.prior_payouts_for_trade(paired["trade"]) == []

    async def _settled_trade(self, world, repo, ref):
        """Another trade on this pairing, instructed and paid."""
        async with repo.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, usdt_received, inr_expected,
                    usdt_owed_client, margin_usdt, status, instructed_at,
                    completed_at)
                VALUES ($5,$1,$2,$3,$4,106.20,107.70,2354,249995,2321.22,
                        32.78,'completed', now(), now())
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"], ref)

    @pytest.mark.asyncio
    async def test_a_surplus_payout_is_reported_with_its_neighbours(
        self, world, repo, paired
    ):
        """
        The live shape on 18 September. Two matching transfers, one other
        instructed trade to account for them — so one is unaccounted and
        both are shown, because nothing in the data says which is which.
        """
        await self._settled_trade(world, repo, "SUPB4")
        await self._payout(repo, paired, D("2321.21"), tx="unbilled")
        await self._payout(repo, paired, D("2321.22"), tx="supb4s")

        found = await repo.prior_payouts_for_trade(paired["trade"])
        assert {f["tx_hash"] for f in found} == {"unbilled", "supb4s"}

    @pytest.mark.asyncio
    async def test_a_payout_every_other_trade_accounts_for_is_silent(
        self, world, repo, paired
    ):
        """
        Two settled trades, two transfers, nothing owing an explanation. A
        vendor settling the same figure repeatedly must not be warned about
        every payout they have ever had.
        """
        await self._settled_trade(world, repo, "SUPB4")
        await self._settled_trade(world, repo, "SUPB6")
        await self._payout(repo, paired, D("2321.22"), tx="first")
        await self._payout(repo, paired, D("2321.21"), tx="second")

        assert await repo.prior_payouts_for_trade(paired["trade"]) == []

    @pytest.mark.asyncio
    async def test_one_claimant_cannot_silence_two_unpaid_transfers(
        self, world, repo, paired
    ):
        """
        The reason this counts instead of excluding. Dropping anything an
        instructed trade could explain would let a single trade hide any
        number of genuinely unbilled payouts — and missing a real one is the
        failure this exists to prevent.
        """
        await self._settled_trade(world, repo, "SUPB4")
        for i in range(3):
            await self._payout(repo, paired, D("2321.21"), tx=f"unbilled{i}")

        found = await repo.prior_payouts_for_trade(paired["trade"])
        assert len(found) == 3

    @pytest.mark.asyncio
    async def test_an_uninstructed_trade_accounts_for_nothing(
        self, world, repo, paired
    ):
        """
        SUPB3 was never instructed — that is precisely why its money went
        unbilled — so it cannot be the explanation for a transfer.
        """
        async with repo.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, usdt_received, inr_expected,
                    usdt_owed_client, margin_usdt, status)
                VALUES ('SUPB3',$1,$2,$3,$4,106.00,107.50,2354,249524,2321.15,
                        32.85,'cancelled')
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
        await self._payout(repo, paired, D("2321.21"), tx="unbilled")

        found = await repo.prior_payouts_for_trade(paired["trade"])
        assert [f["tx_hash"] for f in found] == ["unbilled"]

    @pytest.mark.asyncio
    async def test_a_pairing_with_no_payout_wallet_says_nothing(
        self, world, repo, paired
    ):
        """An inner join on a null link must not error — it must be silent."""
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE wallets SET payout_wallet_id = NULL WHERE id = $1",
                world["wallet"])
        await self._payout(repo, paired, D("2321.21"))
        assert await repo.prior_payouts_for_trade(paired["trade"]) == []


class TestOnboardingAVendor:
    """
    seed-supplier.sql, as a command, against a real schema. Every guard here
    exists because the failure it prevents only shows up later — at the first
    deposit, or in a reference nobody can attribute.
    """

    async def _onboard(self, world, repo, **over):
        args = dict(
            label="Supplier C", telegram_chat_id=-1009, prefix="SUPC",
            client_id=world["client"], wallet_address="TC1jkLmNoPqRsTuVwXyZaBcDeFgHiJkLmN",
            actor_party_id=world["bridge"],
        )
        args.update(over)
        return await repo.onboard_supplier(**args)

    @pytest.mark.asyncio
    async def test_it_creates_all_three_records(self, world, repo):
        ok, msg, detail = await self._onboard(world, repo)
        assert ok, msg

        async with repo.pool.acquire() as conn:
            party = await conn.fetchrow(
                "SELECT role, telegram_chat_id FROM parties WHERE id = $1",
                detail["party_id"])
            prefix = await conn.fetchval(
                "SELECT prefix FROM supplier_counters WHERE supplier_id = $1",
                detail["party_id"])
            wallet = await conn.fetchrow(
                "SELECT is_internal, supplier_id, client_id, is_monitored "
                "FROM wallets WHERE id = $1", detail["wallet_id"])

        assert party["role"] == "supplier"
        assert party["telegram_chat_id"] == -1009
        assert prefix == "SUPC"
        assert wallet["is_internal"] and wallet["is_monitored"]
        assert wallet["supplier_id"] == detail["party_id"]
        assert wallet["client_id"] == world["client"]

    @pytest.mark.asyncio
    async def test_the_first_deposit_can_get_a_reference(self, world, repo):
        """
        The failure a missing counter causes, reproduced. Without the counter
        row next_reference raises and the vendor's first deposit fails.
        """
        ok, _, detail = await self._onboard(world, repo)
        assert ok
        async with repo.pool.acquire() as conn:
            async with conn.transaction():
                ref = await repo.next_reference(conn, detail["party_id"])
        assert ref == "SUPC1"

    @pytest.mark.asyncio
    async def test_no_rate_and_no_adoption(self, world, repo):
        ok, _, detail = await self._onboard(world, repo)
        assert ok
        async with repo.pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM rates WHERE supplier_id = $1",
                detail["party_id"]) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM monitor_state WHERE wallet_id = $1",
                detail["wallet_id"]) == 0

    @pytest.mark.asyncio
    async def test_a_duplicate_prefix_is_refused(self, world, repo):
        """Nothing in the schema stops this, and SUPA1 twice is unresolvable."""
        ok, msg, _ = await self._onboard(world, repo, prefix="supa")
        assert not ok
        assert "prefix" in msg

    @pytest.mark.asyncio
    async def test_a_taken_chat_is_refused(self, world, repo):
        ok, msg, _ = await self._onboard(world, repo, telegram_chat_id=-1002)
        assert not ok
        assert "already registered" in msg

    @pytest.mark.asyncio
    async def test_a_taken_label_is_refused(self, world, repo):
        ok, msg, _ = await self._onboard(world, repo, label="supplier a")
        assert not ok

    @pytest.mark.asyncio
    async def test_a_taken_address_is_refused(self, world, repo):
        ok, msg, _ = await self._onboard(
            world, repo, wallet_address="TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb")
        assert not ok
        assert "already registered" in msg

    @pytest.mark.asyncio
    async def test_a_refusal_leaves_nothing_behind(self, world, repo):
        """Half a vendor is worse than none — it looks finished."""
        async with repo.pool.acquire() as conn:
            before = await conn.fetchval("SELECT count(*) FROM parties")
        await self._onboard(world, repo, telegram_chat_id=-1002)
        async with repo.pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM parties") == before

    @pytest.mark.asyncio
    async def test_a_supplier_id_is_not_accepted_as_the_client(self, world, repo):
        ok, msg, _ = await self._onboard(world, repo, client_id=world["supplier"])
        assert not ok


class TestReopeningAStrandedDeposit:
    """
    The 16 September repair, as a button. SUPB3 and SUPD1 were cancelled and
    their 2,354 and 7,000 USDT sat on dead trades for two days — delivered to
    the client, billed to nobody.
    """

    @pytest_asyncio.fixture
    async def cancelled(self, world, repo):
        """A cancelled trade holding two deposits, 2,354 and 3,000."""
        async with repo.pool.acquire() as conn:
            trade = await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, usdt_received, inr_expected,
                    usdt_owed_client, margin_usdt, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,106.50,5354,564847,5303.73,
                        50.27,'cancelled')
                RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
            # SUPA1 is taken by the trade above, so the counter must agree —
            # a reopened deposit continues the sequence rather than reusing a
            # number that already means something.
            await conn.execute(
                "UPDATE supplier_counters SET last_number = 1 "
                "WHERE supplier_id = $1", world["supplier"])
        ids = []
        for tx, amt in (("first", D("2354")), ("second", D("3000"))):
            await repo.record_deposit(
                tx_hash=tx, wallet_id=world["wallet"], amount_usdt=amt,
                from_address="TSup", block_number=1)
            async with repo.pool.acquire() as conn:
                ids.append(await conn.fetchval(
                    "UPDATE deposits SET trade_id = $1 WHERE tx_hash = $2 "
                    "RETURNING id", trade, tx))
        return {"trade": trade, "deposits": ids}

    @pytest.mark.asyncio
    async def test_it_finds_what_is_stranded(self, repo, cancelled):
        found = await repo.stranded_deposits(cancelled["trade"])
        assert [f["amount_usdt"] for f in found] == [D("2354"), D("3000")]

    @pytest.mark.asyncio
    async def test_the_deposit_gets_its_own_trade_at_todays_rate(
        self, world, repo, cancelled
    ):
        await repo.set_rate(
            supplier_id=world["supplier"], client_id=world["client"],
            supply_rate=D("106.20"), sell_rate=D("107.70"), set_by=world["bridge"])

        ok, msg, detail = await repo.reopen_deposit_as_trade(
            deposit_id=cancelled["deposits"][0], actor_party_id=world["bridge"])
        assert ok, msg

        assert detail["reference"] == "SUPA2"
        assert detail["inr_expected"] == D("249995")   # 2,354 x 106.20
        assert detail["usdt_owed"] == D("2321.22")

        row = await repo.trade_detail(detail["trade_id"])
        assert row["status"] == "awaiting_payment"
        assert row["announced_at"] is not None
        assert row["instructed_at"] is None, "opening is not instructing"

    @pytest.mark.asyncio
    async def test_the_source_trade_is_restated(self, world, repo, cancelled):
        """
        The half that was missed for three days. 5,354 minus the 2,354 that
        moved leaves 3,000, at the cancelled trade's OWN rates.
        """
        ok, _, detail = await repo.reopen_deposit_as_trade(
            deposit_id=cancelled["deposits"][0], actor_party_id=world["bridge"])
        assert ok

        src = await repo.trade_detail(cancelled["trade"])
        assert src["usdt_received"] == D("3000")
        assert src["inr_expected"] == D("316500")       # 3,000 x 105.50
        assert src["supply_rate"] == D("105.50"), "a restatement is not a reprice"
        assert detail["restated"]["from_usdt"] == D("5354")

    @pytest.mark.asyncio
    async def test_moving_the_last_deposit_empties_the_source(
        self, world, repo, cancelled
    ):
        for deposit in cancelled["deposits"]:
            ok, msg, _ = await repo.reopen_deposit_as_trade(
                deposit_id=deposit, actor_party_id=world["bridge"])
            assert ok, msg

        src = await repo.trade_detail(cancelled["trade"])
        assert src["usdt_received"] == 0
        assert src["inr_expected"] == 0
        assert src["usdt_owed_client"] == 0

    @pytest.mark.asyncio
    async def test_nothing_is_left_disagreeing_with_its_deposits(
        self, world, repo, cancelled
    ):
        """The condition the healthcheck warns on must be clear afterwards."""
        await repo.reopen_deposit_as_trade(
            deposit_id=cancelled["deposits"][0], actor_party_id=world["bridge"])
        async with repo.pool.acquire() as conn:
            drift = await conn.fetchval(
                """
                SELECT count(*) FROM (
                    SELECT t.id FROM trades t
                    LEFT JOIN deposits d ON d.trade_id = t.id
                    GROUP BY t.id, t.usdt_received
                    HAVING COALESCE(sum(d.amount_usdt), 0) <> t.usdt_received
                       AND t.usdt_received > 0
                ) x
                """)
        assert drift == 0

    @pytest.mark.asyncio
    async def test_a_live_trades_deposit_is_refused(self, world, repo, cancelled):
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE trades SET status = 'awaiting_payment' WHERE id = $1",
                cancelled["trade"])
        ok, msg, _ = await repo.reopen_deposit_as_trade(
            deposit_id=cancelled["deposits"][0], actor_party_id=world["bridge"])
        assert not ok
        assert "already has one" in msg

    @pytest.mark.asyncio
    async def test_no_rate_means_no_trade_and_no_change(
        self, world, repo, cancelled
    ):
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE trades SET rate_id = NULL WHERE FALSE")  # keep the FK happy
            await conn.execute(
                "DELETE FROM rates WHERE id <> $1", world["rate"])
            await conn.execute(
                "UPDATE rates SET supplier_id = $1 WHERE id = $2",
                world["bridge"], world["rate"])

        ok, msg, _ = await repo.reopen_deposit_as_trade(
            deposit_id=cancelled["deposits"][0], actor_party_id=world["bridge"])
        assert not ok
        assert "no rate" in msg

        src = await repo.trade_detail(cancelled["trade"])
        assert src["usdt_received"] == D("5354"), "a refusal changes nothing"


class TestAccountsAndExport:
    @pytest.mark.asyncio
    async def test_removed_account_is_soft_deleted(self, world, repo):
        acct = await repo.add_bank_account(
            party_id=world["supplier"], account_name="Alpha Traders",
            account_number="123456789012345", ifsc="EXBK0001234")
        assert await repo.remove_bank_account(
            account_id=acct, party_id=world["supplier"])
        assert await repo.list_bank_accounts(world["supplier"]) == []

        # The row must survive so historic summaries stay readable.
        async with repo.pool.acquire() as conn:
            still = await conn.fetchval(
                "SELECT count(*) FROM bank_accounts WHERE id = $1", acct)
        assert still == 1

    @pytest.mark.asyncio
    async def test_removing_another_partys_account_is_refused(self, world, repo):
        acct = await repo.add_bank_account(
            party_id=world["supplier"], account_name="Alpha Traders",
            account_number="123456789012345", ifsc="EXBK0001234")
        assert not await repo.remove_bank_account(
            account_id=acct, party_id=world["client"])

    @pytest.mark.asyncio
    async def test_export_includes_trades_without_payments(self, world, repo):
        async with repo.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,105.00,'open')
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
        rows = await repo.export_rows(days=90)
        assert len(rows) == 1
        assert rows[0]["reference"] == "SUPA1"
        assert rows[0]["utr"] is None


class TestNearCompletion:
    """
    Client request, 8 September 2026: tell the supplier to prepare the next
    batch once outstanding INR falls to the threshold. Once per trade, not once
    per payment.
    """

    async def _trade(self, world, repo, expected=D("1000000")):
        async with repo.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, inr_expected, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,106.50,$5,'awaiting_payment')
                RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"], expected)

    async def _pay(self, repo, world, trade_id, acct, utr, amount):
        ok, msg = await repo.add_payment(
            trade_id=trade_id, utr=utr, amount_inr=amount,
            beneficiary_account_id=acct, added_by=world["client"])
        assert ok, msg

    @pytest_asyncio.fixture
    async def acct(self, world, repo):
        return await repo.add_bank_account(
            party_id=world["supplier"], account_name="Ekta Traders",
            account_number="123456789012345", ifsc="EXBK0001234")

    @pytest.mark.asyncio
    async def test_silent_while_well_short(self, world, repo, acct):
        tid = await self._trade(world, repo)
        await self._pay(repo, world, tid, acct, "UTR00000001", D("500000"))
        # 500,000 outstanding — above the 300,000 threshold.
        assert await repo.claim_near_completion(tid, D("300000")) is None

    @pytest.mark.asyncio
    async def test_fires_at_the_threshold(self, world, repo, acct):
        tid = await self._trade(world, repo)
        await self._pay(repo, world, tid, acct, "UTR00000001", D("700000"))
        claimed = await repo.claim_near_completion(tid, D("300000"))
        assert claimed is not None, "exactly 300,000 outstanding must trigger it"
        assert claimed["reference"] == "SUPA1"

    @pytest.mark.asyncio
    async def test_fires_below_the_threshold(self, world, repo, acct):
        tid = await self._trade(world, repo)
        await self._pay(repo, world, tid, acct, "UTR00000001", D("800000"))
        assert await repo.claim_near_completion(tid, D("300000")) is not None

    @pytest.mark.asyncio
    async def test_only_once_however_many_payments_follow(self, world, repo, acct):
        """
        Without the flag the supplier is told on every payment after the
        threshold — every one of them is also below it.
        """
        tid = await self._trade(world, repo)
        await self._pay(repo, world, tid, acct, "UTR00000001", D("800000"))
        assert await repo.claim_near_completion(tid, D("300000")) is not None

        for i in range(2, 6):
            await self._pay(repo, world, tid, acct, f"UTR0000000{i}", D("10000"))
            assert await repo.claim_near_completion(tid, D("300000")) is None

    @pytest.mark.asyncio
    async def test_concurrent_claims_yield_exactly_one_winner(self, world, repo, acct):
        """
        Two payments landing together would both see an unnotified trade if the
        check and the flag were separate statements.
        """
        import asyncio
        tid = await self._trade(world, repo)
        await self._pay(repo, world, tid, acct, "UTR00000001", D("800000"))

        results = await asyncio.gather(
            *[repo.claim_near_completion(tid, D("300000")) for _ in range(8)]
        )
        assert sum(r is not None for r in results) == 1

    @pytest.mark.asyncio
    async def test_completed_trade_never_triggers(self, world, repo, acct):
        tid = await self._trade(world, repo)
        await self._pay(repo, world, tid, acct, "UTR00000001", D("800000"))
        await repo.complete_trade(tid, world["client"])
        assert await repo.claim_near_completion(tid, D("300000")) is None

    @pytest.mark.asyncio
    async def test_a_trade_with_no_expected_total_is_ignored(self, world, repo):
        """inr_expected is 0 until a deposit lands — 0 - 0 <= threshold is true."""
        async with repo.pool.acquire() as conn:
            tid = await conn.fetchval(
                """
                INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                    rate_id, supply_rate, sell_rate, status)
                VALUES ('SUPA1',$1,$2,$3,$4,105.50,106.50,'open') RETURNING id
                """, world["supplier"], world["client"], world["wallet"],
                world["rate"])
        assert await repo.claim_near_completion(tid, D("300000")) is None

    @pytest.mark.asyncio
    async def test_it_is_written_to_the_audit_log(self, world, repo, acct):
        tid = await self._trade(world, repo)
        await self._pay(repo, world, tid, acct, "UTR00000001", D("800000"))
        await repo.claim_near_completion(tid, D("300000"))
        async with repo.pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE action = 'trade.near_completion'")
        assert n == 1


class TestDepositConfirmation:
    """
    B4: "notify immediately on detection, then confirm separately." The second
    half — without it a deposit sits at 'detected' forever and the Bridge has
    no way to tell a settled transaction from one that never landed.
    """

    @pytest.mark.asyncio
    async def test_detected_then_confirmed(self, world, repo):
        await repo.record_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address=None, block_number=None)

        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, confirmed_at FROM deposits WHERE tx_hash='tx1'")
        assert row["status"] == "detected"
        assert row["confirmed_at"] is None

        assert await repo.confirm_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], block_number=99)

        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, confirmed_at, block_number FROM deposits "
                "WHERE tx_hash='tx1'")
        assert row["status"] == "confirmed"
        assert row["confirmed_at"] is not None
        assert row["block_number"] == 99

    @pytest.mark.asyncio
    async def test_confirming_twice_transitions_once(self, world, repo):
        """The caller acts on the transition, so it must happen exactly once."""
        await repo.record_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address=None, block_number=None)
        assert await repo.confirm_deposit(tx_hash="tx1", wallet_id=world["wallet"])
        assert not await repo.confirm_deposit(tx_hash="tx1", wallet_id=world["wallet"])

    @pytest.mark.asyncio
    async def test_already_confirmed_on_arrival(self, world, repo):
        """TronScan often reports a transaction already confirmed."""
        await repo.record_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address=None, block_number=None, confirmed=True)
        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, confirmed_at FROM deposits WHERE tx_hash='tx1'")
        assert row["status"] == "confirmed"
        assert row["confirmed_at"] is not None

    @pytest.mark.asyncio
    async def test_fresh_deposits_are_not_reported_as_stale(self, world, repo):
        await repo.record_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address=None, block_number=None)
        assert await repo.unconfirmed_deposits(15) == []

    @pytest.mark.asyncio
    async def test_old_unconfirmed_deposit_is_reported(self, world, repo):
        await repo.record_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address=None, block_number=None)
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE deposits SET detected_at = now() - interval '30 minutes'")

        stale = await repo.unconfirmed_deposits(15)
        assert len(stale) == 1
        assert stale[0]["tx_hash"] == "tx1"
        assert stale[0]["address"] == "TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb"

    @pytest.mark.asyncio
    async def test_confirmed_deposits_are_never_reported(self, world, repo):
        await repo.record_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address=None, block_number=None, confirmed=True)
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE deposits SET detected_at = now() - interval '30 minutes'")
        assert await repo.unconfirmed_deposits(15) == []

    @pytest.mark.asyncio
    async def test_the_bridge_is_warned_only_once(self, world, repo):
        """Flagging moves it off 'detected' so the next cycle stays quiet."""
        await repo.record_deposit(
            tx_hash="tx1", wallet_id=world["wallet"], amount_usdt=D("1000"),
            from_address=None, block_number=None)
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE deposits SET detected_at = now() - interval '30 minutes'")

        stale = await repo.unconfirmed_deposits(15)
        assert await repo.flag_unconfirmed(stale[0]["id"])
        assert not await repo.flag_unconfirmed(stale[0]["id"])
        assert await repo.unconfirmed_deposits(15) == []
