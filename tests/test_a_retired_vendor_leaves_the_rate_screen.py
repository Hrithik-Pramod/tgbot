"""
A vendor that cannot trade does not appear on the rate screen.

WHAT IT COST — 26 NOVEMBER 2026, 23:20

Eight vendors were retired that evening at the Bridge's request. /viewrate
went on listing all of them, and because their rates were weeks or months
old, every line carried the stale-rate flag:

    IndoLondon - November → Haze & ProperPay: Exchange   ← CHECK THIS
    Buy  106.2
    Sell 107.2
    Set 15 days ago

    BIG BOSS → Haze & ProperPay: Exchange   ← CHECK THIS
    ...

    and please remove all of the following ... only leave Uncle and Raj 2.0
    — Bridge

Eight "CHECK THIS" warnings about pairings that cannot trade, with the two
that can buried among them. The flag exists so a rate left behind gets
noticed; pointed at eight dead vendors it trains the reader to ignore it,
which costs the one time it is real.

WHY THE RATES ARE NOT DELETED

rates is append-only, and every completed trade was priced from a row in it.
Deleting the rows would quietly change what a finished deal was worth and
break the audit trail behind /export and /vendorstatement. Retiring a vendor
already stops a rate being USED — the party is inactive, so no deposit opens
a trade. This stops it being SHOWN.

That also means reactivating a vendor brings its rate back, which is right:
the rate was never withdrawn, only the vendor.

BOTH ENDS OF THE PAIRING

A rate to an inactive CLIENT cannot be traded either, and showing it would
be the identical fault from the other side. Checked here so it is not
rediscovered the first time a client is switched off.
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
    def test_it_filters_on_both_ends(self):
        """
        Read as SQL, not as the paragraph above it — the docstring names
        is_active while explaining the rule, and a scan of the whole source
        would pass on the prose alone. See tests/sqltext.py; this has caught
        five tests in this project already.
        """
        sql = query_of(Repo.current_rates)
        assert "s.is_active" in sql
        assert "c.is_active" in sql

    def test_it_does_not_delete_anything(self):
        src = __import__("inspect").getsource(Repo.current_rates)
        assert "DELETE" not in src.upper().replace("DELETED", "")


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
    """Two vendors on one client, each with a rate — Uncle and one retiree."""
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
        role="supplier", label="BIG BOSS", display_name=None,
        telegram_chat_id=-1005, telegram_user_id=None, actor_party_id=bridge)

    for s, supply, sell in ((keep, "106.20", "107.20"),
                            (drop, "106.40", "107.80")):
        await repo.set_rate(supplier_id=s, client_id=client,
                            supply_rate=D(supply), sell_rate=D(sell),
                            set_by=bridge)

    return dict(bridge=bridge, client=client, keep=keep, drop=drop)


async def _labels(repo):
    return [r["supplier_label"] for r in await repo.current_rates()]


@pytest.mark.asyncio
class TestWhatTheScreenShows:
    async def test_before_and_after(self, repo, desk):
        """
        The first assertion guards the premise: if current_rates stopped
        returning active vendors at all, every test below would pass for
        the wrong reason.
        """
        assert sorted(await _labels(repo)) == ["BIG BOSS", "Uncle"]

        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["drop"])

        assert await _labels(repo) == ["Uncle"]

    async def test_the_rate_row_survives(self, repo, desk):
        """
        Hidden, not deleted. The completed deals priced off it still need
        it, and /export reads the trade's own stored rate — which came from
        here.
        """
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["drop"])
            still_there = await conn.fetchval(
                "SELECT count(*) FROM rates WHERE supplier_id = $1",
                desk["drop"])
        assert still_there == 1

    async def test_bringing_the_vendor_back_brings_the_rate_back(self, repo, desk):
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["drop"])
            await conn.execute(
                "UPDATE parties SET is_active = TRUE WHERE id = $1",
                desk["drop"])
        assert sorted(await _labels(repo)) == ["BIG BOSS", "Uncle"]

    async def test_an_inactive_client_hides_it_too(self, repo, desk):
        """
        The other end. Nothing can be traded to a switched-off client, so
        nothing about it belongs on a screen of live rates.
        """
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE parties SET is_active = FALSE WHERE id = $1",
                desk["client"])
        assert await _labels(repo) == []

    async def test_the_newest_rate_is_still_the_one_shown(self, repo, desk):
        """
        The filter must not disturb DISTINCT ON. A WHERE clause added in
        the wrong place relative to the ordering would show an older rate,
        which is worse than showing a retired vendor — it is a wrong
        number that looks right.
        """
        await repo.set_rate(
            supplier_id=desk["keep"], client_id=desk["client"],
            supply_rate=D("106.50"), sell_rate=D("107.50"),
            set_by=desk["bridge"])

        rows = {r["supplier_label"]: r for r in await repo.current_rates()}
        assert rows["Uncle"]["supply_rate"] == D("106.50")
        assert rows["Uncle"]["sell_rate"] == D("107.50")
