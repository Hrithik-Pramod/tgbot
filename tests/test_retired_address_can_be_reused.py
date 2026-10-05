"""
An address freed by retirement can be registered again.

WHAT HAPPENED (live, 28 September 2026)

    i retired it
    tried add
    it says alredy regeistered
    ...
    i removed wallet on Uncle and now its not showing at all in association
    of wallet
    Try to add back, says already registered
    ...
    We are mid trading, but the other accounts, big boss and Uncle are not,
    please fix asap

Two vendors with no working wallet while trades were arriving for both,
because retirement freed the pairing slot and not the address.

THE BUG

/walletremove was shipped on 25 September with retired_at and both unique
INDEXES made partial. The database would happily have taken the row back.
What refused it was an application check that predated the feature:

    clash = await conn.fetchval(
        "SELECT id FROM wallets WHERE address = $1", address)
    if clash is not None:
        return False, "That address is already registered."

and the same check in onboard_supplier. Retiring released the constraint and
left the guard in front of it.

WHY THE TEST THAT WAS MEANT TO CATCH THIS DID NOT

tests/test_wallet_retire.py asserted:

    src = inspect.getsource(Repo.add_wallet)
    assert "w.retired_at IS NULL" in src

which is TRUE — the pairing check has it. The function contains the string
while the address check two lines above does not. It was a test of the
mechanism, passing for the wrong half, in a file whose own docstring says
the 22 September lesson was that a test which cannot fail is not a test.

So these call add_wallet and read the answer.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.repo import Repo  # noqa: E402

ADDRESS = "TExampleRetiredWalletAddress00000"


class _Conn:
    """
    Enough of a connection to answer the two questions add_wallet asks, and
    to record whether the INSERT was reached.

    `live_rows` is what the address lookup finds AFTER its own WHERE clause
    is applied — so a query that forgets `retired_at IS NULL` sees the
    retired row and a query that includes it does not. That is the whole
    behaviour under test.
    """

    def __init__(self, *, retired_row=True, live_row=False, pairing_taken=False):
        self.retired_row = retired_row
        self.live_row = live_row
        self.pairing_taken = pairing_taken
        self.inserted = False
        self.audits: list[str] = []

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return _T()

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM wallets WHERE address" in s:
            filtered = "retired_at IS NULL" in s
            if self.live_row:
                return 1
            if self.retired_row and not filtered:
                return 1          # the bug: the retired row still blocks
            return None
        if "w.is_internal" in s and "w.supplier_id" in s:
            return 1 if self.pairing_taken else None
        if "INSERT INTO wallets" in s:
            self.inserted = True
            return 99
        return None

    async def execute(self, *a):
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

    async def _audit(c, *, actor_party_id, action, entity_type, entity_id, detail):
        conn.audits.append(action)
    r.audit = _audit
    return r


class TestTheAddressComesBack:
    @pytest.mark.asyncio
    async def test_a_retired_address_can_be_registered_again(self):
        """
        The live failure, end to end. Before the fix this returned
        "That address is already registered." and the vendor stayed dark.
        """
        conn = _Conn(retired_row=True)
        ok, msg = await _repo(conn).add_wallet(
            address=ADDRESS, is_internal=True, actor_party_id=1,
            supplier_id=3, client_id=9,
        )
        assert ok, msg
        assert conn.inserted

    @pytest.mark.asyncio
    async def test_it_is_watched_from_the_moment_it_is_added(self):
        """
        The vendor was dark because nothing was polling the address. A
        re-registered wallet has to come back monitored or the fix is only
        half of one.
        """
        import inspect
        src = inspect.getsource(Repo.add_wallet)
        insert = src.split("INSERT INTO wallets")[1].split("RETURNING")[0]
        assert "is_monitored" in insert
        assert "TRUE" in insert

    @pytest.mark.asyncio
    async def test_the_pairing_slot_is_free_too(self):
        """
        Both halves. Retiring released the pairing on 25 September; this
        holds it released.
        """
        conn = _Conn(retired_row=True, pairing_taken=False)
        ok, _ = await _repo(conn).add_wallet(
            address=ADDRESS, is_internal=True, actor_party_id=1,
            supplier_id=3, client_id=9,
        )
        assert ok


class TestALiveAddressIsStillRefused:
    @pytest.mark.asyncio
    async def test_an_address_in_use_is_rejected(self):
        """
        The other side, and the one that must not be lost: two live wallets
        on one address makes every deposit to it ambiguous, which is the
        whole reason a receiving address identifies a vendor.
        """
        conn = _Conn(retired_row=False, live_row=True)
        ok, msg = await _repo(conn).add_wallet(
            address=ADDRESS, is_internal=True, actor_party_id=1,
            supplier_id=3, client_id=9,
        )
        assert not ok
        assert "already registered" in msg
        assert not conn.inserted

    @pytest.mark.asyncio
    async def test_a_live_pairing_is_still_refused(self):
        conn = _Conn(retired_row=False, pairing_taken=True)
        ok, msg = await _repo(conn).add_wallet(
            address=ADDRESS, is_internal=True, actor_party_id=1,
            supplier_id=3, client_id=9,
        )
        assert not ok
        assert "/walletchange" in msg
        assert not conn.inserted


def _flat(src: str) -> str:
    """
    Join adjacent string literals and collapse whitespace.

    Both fixes split their SQL across two literals to fit the line:

        "SELECT id FROM wallets "
        "WHERE address = $1 AND retired_at IS NULL"

    so a naive substring search finds neither the clause nor the filter —
    and a guard written that way reports no offenders because it can no
    longer see any queries at all. This test was doing exactly that before
    the flattening was added.
    """
    import re
    joined = re.sub(r'"\s*\n\s*"', "", src)
    return " ".join(joined.split())


class TestOnboardingAVendorUsesTheSameRule:
    def test_its_address_check_skips_retired(self):
        """
        /addvendor carried a copy of the same guard. A vendor onboarded from
        scratch onto a freed address hit the identical wall.
        """
        import inspect
        src = _flat(inspect.getsource(Repo.onboard_supplier))
        check = src.split("FROM wallets WHERE address")[1][:120]
        assert "retired_at IS NULL" in check


# Lookups that ask a DIFFERENT question, with the reason each one is exempt.
#
# The rule being guarded is about availability: "is this address already in
# use, so that adding it again would clash?" Retirement is the whole point
# there — a retired address is free.
#
# A lookup asking whether an address was EVER the desk's is not that question,
# and filtering retirement would make it answer wrongly: a wallet taken out of
# service is still an address this desk has settled from, and its past
# settlements do not stop being ours because it was retired afterwards.
NOT_AVAILABILITY_CHECKS = {
    "is_known_payout_source":
        "asks whether an address is the desk's at all, not whether it is "
        "free. A retired wallet that once sent a settlement is still ours, "
        "and filtering it would tell a counterparty nothing arrived.",
}


class TestNoWalletCheckIsBlindToRetirementAgain:
    def test_every_address_lookup_filters(self):
        """
        The regression guard. The fault was not one query — it was every
        place that asked "is this address taken?" without saying what taken
        means.

        Scanned per method rather than over the whole file, so an offender
        can be named and a genuine exception can be recorded with its reason
        instead of the guard being loosened for everyone.
        """
        import inspect
        import re

        scanned = 0
        offenders = []
        for name, fn in inspect.getmembers(Repo, predicate=inspect.isfunction):
            if name in NOT_AVAILABILITY_CHECKS:
                continue
            src = _flat(inspect.getsource(fn))
            for q in re.findall(
                r"SELECT [^\"]{0,80}?FROM wallets WHERE address[^\"]{0,80}", src
            ):
                scanned += 1
                if "retired_at IS NULL" not in q:
                    offenders.append(f"{name}: {q}")

        assert scanned, "the scan matched no address lookups at all — it is not looking"
        assert not offenders, (
            "address lookups blind to retirement:\n" + "\n".join(offenders)
            + "\n\nIf this one genuinely asks a different question, add it to "
              "NOT_AVAILABILITY_CHECKS with the reason."
        )

    def test_every_exemption_still_exists(self):
        """
        An allowlist that outlives what it allows is how a guard quietly
        stops guarding.
        """
        for name in NOT_AVAILABILITY_CHECKS:
            assert hasattr(Repo, name), (
                f"{name} is exempted but no longer exists — drop the entry"
            )
