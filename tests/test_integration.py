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
        await repo.set_monitor_cursor(world["wallet"], 5000)
        await repo.set_monitor_cursor(world["wallet"], 3000)
        async with repo.pool.acquire() as conn:
            got = await conn.fetchval(
                "SELECT last_timestamp_ms FROM monitor_state WHERE wallet_id = $1",
                world["wallet"])
        assert got == 5000, "GREATEST() must stop the cursor rewinding"


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
    async def test_only_one_open_trade_per_wallet(self, world, repo):
        import asyncpg

        async def open_trade(ref):
            async with repo.pool.acquire() as conn:
                return await conn.fetchval(
                    """
                    INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                        rate_id, supply_rate, sell_rate, status)
                    VALUES ($1,$2,$3,$4,$5,105.50,105.00,'open') RETURNING id
                    """, ref, world["supplier"], world["client"],
                    world["wallet"], world["rate"])

        await open_trade("SUPA1")
        with pytest.raises(asyncpg.UniqueViolationError):
            await open_trade("SUPA2")

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
