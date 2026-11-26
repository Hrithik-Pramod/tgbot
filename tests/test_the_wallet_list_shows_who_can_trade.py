"""
/wallet lists the addresses that can be used, and only those.

WHAT IT COST — 27 NOVEMBER 2026, 00:34

    also remove the ones i mention, leave skates, uncle, raj 2.0 and cloud
    cloud is not showing of list of wallets
    please check
    — Bridge

Two complaints in the same minute, one cause. list_wallets filtered retired
WALLETS and not retired VENDORS. Eight vendors had been switched off the
evening before; their addresses were not retired, so five of them stayed on
the screen. Cloud had just been switched back on, and its only address HAD
been retired hours earlier, so it showed nothing at all.

The screen was therefore wrong in both directions at once: it listed
vendors that cannot trade and omitted the one that can. At 00:34, with a
trade in flight, that is the screen he is using to work out where money
should go.

WHY IT MATTERS BEYOND THE READING

/walletlink and /walletchange read this same list. A retired vendor's
address appearing in those pickers means it can be linked as a settlement
destination or have its address changed — from a list it had no business
being in. The filter closes the screen and both commands together.

WHY THE ROWS SURVIVE

A wallet carries the attribution of every deposit made through it. An
address that received real money is not deleted because its vendor stopped
trading, and reactivating a vendor brings its addresses back with it. Same
rule as the rate screen; see
tests/test_a_retired_vendor_leaves_the_rate_screen.py.

THE NULL JOINS ARE THE TRAP

An internal wallet has a supplier and a client and no owner. An external
one has an owner and neither of the others. A filter written as
`s.is_active AND c.is_active AND o.is_active` is NULL for every row and
returns nothing — the screen goes blank instead of going wrong, which is
at least loud. Each clause has to pass when its party is absent.
"""

import pathlib
import sys
from decimal import Decimal as D

import pytest
import pytest_asyncio

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sqltext import query_of  # noqa: E402
from db.repo import Repo  # noqa: E402
from tests.test_integration import DB_URL, SCHEMA  # noqa: E402

pytestmark = pytest.mark.skipif(
    DB_URL is None,
    reason="no PostgreSQL available (pip install pgserver, or set DATABASE_URL)",
)


class TestTheQueryItself:
    def test_it_checks_every_party_on_the_row(self):
        """
        Read as SQL, not as the paragraph above it — this file's own prose
        names is_active repeatedly. See tests/sqltext.py.
        """
        sql = query_of(Repo.list_wallets)
        assert "s.is_active" in sql
        assert "c.is_active" in sql
        assert "o.is_active" in sql

    def test_an_absent_party_does_not_hide_the_row(self):
        """
        The NULL trap. Each clause must be guarded by its own IS NULL.
        """
        sql = query_of(Repo.list_wallets)
        for col in ("w.supplier_id IS NULL", "w.client_id IS NULL",
                    "w.owner_party_id IS NULL"):
            assert col in sql, col

    def test_retired_wallets_are_still_filtered(self):
        """
        The original rule, which this must not replace.
        """
        assert "w.retired_at IS NULL" in query_of(Repo.list_wallets)


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
    """One client, two vendors with addresses, plus a client payout address."""
    bridge = await repo.add_party(
        role="bridge", label="Bridge", display_name=None,
        telegram_chat_id=-1001, telegram_user_id=None, actor_party_id=None)
    client = await repo.add_party(
        role="client", label="Client A", display_name=None,
        telegram_chat_id=-1003, telegram_user_id=None, actor_party_id=bridge)
    keep = await repo.add_party(
        role="supplier", label="Uncle", display_name=None,
        telegram_chat_id=-1004, telegram_user_id=None, actor_party_id=bridge)
    drop = await repo.add_party(
        role="supplier", label="PCompany", display_name=None,
        telegram_chat_id=-1005, telegram_user_id=None, actor_party_id=bridge)

    async with repo.pool.acquire() as conn:
        for s, addr in ((keep, "TExampleUncleAddress00000000000000"),
                        (drop, "TExamplePCompanyAddress0000000000")):
            await conn.execute(
                """
                INSERT INTO wallets (address, is_internal, supplier_id,
                                     client_id, label, is_monitored)
                VALUES ($1, TRUE, $2, $3, 'pairing', TRUE)
                """, addr, s, client)
        await conn.execute(
            """
            INSERT INTO wallets (address, is_internal, owner_party_id,
                                 label, is_monitored)
            VALUES ('TExampleClientPayout000000000000', FALSE, $1,
                    'payout', TRUE)
            """, client)

    return dict(bridge=bridge, client=client, keep=keep, drop=drop)


async def _addresses(repo):
    return {w["address"] for w in await repo.list_wallets()}


@pytest.mark.asyncio
class TestWhatTheScreenShows:
    async def test_everything_shows_while_everyone_trades(self, repo, desk):
        """
        Guards the premise. If list_wallets returned nothing at all, every
        assertion below would pass for the wrong reason — which is exactly
        what the NULL-join mistake would cause.
        """
        assert len(await _addresses(repo)) == 3

    async def test_a_retired_vendor_leaves(self, repo, desk):
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["drop"])

        addrs = await _addresses(repo)
        assert "TExamplePCompanyAddress0000000000" not in addrs
        assert "TExampleUncleAddress00000000000000" in addrs

    async def test_the_client_payout_address_is_unaffected(self, repo, desk):
        """
        It has no supplier. The NULL-join trap would have removed it along
        with the vendor, taking the settlement destination off the screen.
        """
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["drop"])
        assert "TExampleClientPayout000000000000" in await _addresses(repo)

    async def test_a_retired_client_takes_its_pairings_with_it(self, repo, desk):
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["client"])
        assert await _addresses(repo) == set()

    async def test_the_wallet_row_survives(self, repo, desk):
        """
        Hidden, not deleted. Every deposit ever attributed through that
        address points at this row.
        """
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["drop"])
            still_there = await conn.fetchval(
                "SELECT count(*) FROM wallets WHERE supplier_id = $1",
                desk["drop"])
        assert still_there == 1

    async def test_bringing_the_vendor_back_brings_its_address_back(
        self, repo, desk
    ):
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["drop"])
            await conn.execute(
                "UPDATE parties SET is_active = TRUE WHERE id = $1",
                desk["drop"])
        assert "TExamplePCompanyAddress0000000000" in await _addresses(repo)

    async def test_a_retired_address_still_does_not_show(self, repo, desk):
        """
        The other half of Cloud's situation: the vendor is live and the
        address is finished with. That one stays hidden, and the vendor
        needs /walletadd.
        """
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE wallets SET retired_at = now() WHERE supplier_id = $1",
                desk["keep"])
        assert "TExampleUncleAddress00000000000000" not in await _addresses(repo)
