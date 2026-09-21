"""
A deposit can be billed to the vendor who sent it, not the address it hit.

WHAT HAPPENED (live, 21-22 September 2026)

4,708 USDT arrived for Tata Mahalaxmi on Malegao - Sam's address. The two
send from a SHARED wallet, so the arriving transaction carried a destination
and nothing else — no sender identity the bot could trust. It opened SUPB7
against Malegao: wrong supplier, wrong bank accounts, wrong deal prefix.

    Bridge: cancel it and reopen under Tata.

reopen_deposit_as_trade could not. It read the supplier straight off the
deposit's wallet:

    w = SELECT * FROM wallets WHERE id = d.wallet_id
    ref = next_reference(w["supplier_id"])

so reopening produced the same trade under a new number. There was no way to
say who actually sent it.

THE LINE THIS DRAWS

Two facts were being conflated. WHERE the USDT arrived is on-chain and not
negotiable. WHO sent it is a judgement, and with a shared sending wallet it
is the Bridge's judgement, not the bot's.

So the override moves the trade and leaves the deposit alone:

    deposit.wallet_id   stays Malegao's       where it physically landed
    trade.wallet_id     becomes Tata's        who is being billed

They differ, deliberately. Rewriting the deposit would make the ledger
disagree with the chain for ever, and this system has already paid once for
figures that looked tidy and were false (SUPA5's phantom 66,038, carried for
days across every export).
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.repo import Repo  # noqa: E402


# --------------------------------------------------------------------------
# A fake connection, because the point of these is the BRANCHING — which
# wallet is chosen, what is refused, what is written to the audit log. The
# arithmetic is covered by the integration tests against a real database.
# --------------------------------------------------------------------------

MALEGAO_WALLET = {
    "id": 3, "is_internal": True, "supplier_id": 30, "client_id": 9,
    "address": "TMalegaoAddress",
}
TATA_WALLET = {
    "id": 8, "is_internal": True, "supplier_id": 80, "client_id": 9,
    "address": "TTataAddress",
}
DEPOSIT = {
    "id": 51, "wallet_id": 3, "trade_id": 7, "amount_usdt": D("4708"),
    "tx_hash": "0xabc", "detected_at": "2026-09-21T17:00:00+05:30",
}


class _Conn:
    def __init__(self, *, pairing_exists=True, rate_exists=True,
                 source_status="cancelled"):
        self.pairing_exists = pairing_exists
        self.rate_exists = rate_exists
        self.source_status = source_status
        self.inserted: dict = {}
        self.audits: list[dict] = []
        self.updated_deposit_wallet = False

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _T()

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM deposits WHERE id" in s:
            return dict(DEPOSIT)
        if "FROM wallets WHERE id" in s:
            return dict(MALEGAO_WALLET)
        if "WHERE is_internal AND supplier_id" in s:
            # The override's lookup: the named supplier's pairing.
            return dict(TATA_WALLET) if self.pairing_exists else None
        if "FROM rates" in s:
            return ({"id": 4, "supply_rate": D("106.2"), "sell_rate": D("107.7")}
                    if self.rate_exists else None)
        if "FROM trades WHERE id" in s:
            return {"id": 7, "reference": "SUPB7", "usdt_received": D("4708"),
                    "inr_expected": D("499990"), "supply_rate": D("106.2"),
                    "sell_rate": D("107.7")}
        return None

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "SELECT status FROM trades" in s:
            return self.source_status
        if "sum(amount_usdt)" in s:
            return D(0)
        if "INSERT INTO trades" in s:
            self.inserted = {"sql": s, "args": args}
            return 99
        return None

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if "UPDATE deposits SET wallet_id" in s:
            self.updated_deposit_wallet = True
        return None

    async def fetch(self, sql, *args):
        return []


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _A()


def _repo(conn):
    r = Repo.__new__(Repo)
    r.pool = _Pool(conn)

    async def _next_reference(c, supplier_id):
        return {30: "SUPB8", 80: "SUPD1"}[supplier_id]

    async def _audit(c, *, actor_party_id, action, entity_type, entity_id, detail):
        conn.audits.append({"action": action, "entity_type": entity_type,
                            "entity_id": entity_id, "detail": detail})

    r.next_reference = _next_reference
    r.audit = _audit
    return r


class TestWithoutAnOverrideNothingChanges:
    @pytest.mark.asyncio
    async def test_it_still_opens_under_the_wallets_own_supplier(self):
        """The ordinary path must be untouched."""
        conn = _Conn()
        ok, msg, detail = await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1,
        )
        assert ok, msg
        assert detail["reference"] == "SUPB8"
        assert detail["reattributed"] is False

    @pytest.mark.asyncio
    async def test_naming_the_same_supplier_is_not_a_reattribution(self):
        """Picking the vendor it already was is a no-op, not a correction."""
        conn = _Conn()
        ok, _, detail = await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=30,
        )
        assert ok
        assert detail["reattributed"] is False
        assert not [a for a in conn.audits
                    if a["action"] == "deposit.reattributed"]


class TestTheOverrideMovesTheTrade:
    @pytest.mark.asyncio
    async def test_the_trade_opens_under_the_named_vendor(self):
        """The live case. SUPD1 is Tata's prefix; SUPB8 would be Malegao's."""
        conn = _Conn()
        ok, msg, detail = await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=80,
        )
        assert ok, msg
        assert detail["reference"] == "SUPD1"
        assert detail["reattributed"] is True

    @pytest.mark.asyncio
    async def test_the_trade_is_written_against_tatas_wallet_and_supplier(self):
        conn = _Conn()
        await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=80,
        )
        args = conn.inserted["args"]
        # reference, supplier_id, client_id, wallet_id, rate_id, ...
        assert args[1] == 80, "supplier must be the one named"
        assert args[3] == 8, "trade must sit on the named vendor's wallet"

    @pytest.mark.asyncio
    async def test_the_deposit_keeps_the_address_it_landed_on(self):
        """
        The whole point. The chain says Malegao's address received it and
        the chain is not editable.
        """
        conn = _Conn()
        await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=80,
        )
        assert not conn.updated_deposit_wallet


class TestTheRecordSaysWhatWasDone:
    @pytest.mark.asyncio
    async def test_a_reattribution_is_audited_in_its_own_right(self):
        conn = _Conn()
        await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=80,
        )
        entry = next(a for a in conn.audits
                     if a["action"] == "deposit.reattributed")
        assert entry["entity_type"] == "deposit"
        assert entry["entity_id"] == 51
        assert entry["detail"]["from_supplier_id"] == 30
        assert entry["detail"]["to_supplier_id"] == 80
        assert entry["detail"]["billed_as"] == "SUPD1"
        assert entry["detail"]["landed_on_address"] == "TMalegaoAddress"

    @pytest.mark.asyncio
    async def test_the_landing_address_is_recorded_even_without_an_override(self):
        """
        So the log can answer "where did this arrive?" on its own, without a
        join to a wallets table that may have been edited since.
        """
        conn = _Conn()
        await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1,
        )
        entry = next(a for a in conn.audits
                     if a["action"] == "trade.reopened_from_deposit")
        assert entry["detail"]["landed_on_address"] == "TMalegaoAddress"
        assert entry["detail"]["reattributed"] is False


class TestWhatItRefuses:
    @pytest.mark.asyncio
    async def test_a_vendor_not_paired_with_this_client(self):
        """
        No pairing means no wallet and no rate, so there is nothing to open
        the trade on. Refusing is the only honest answer.
        """
        conn = _Conn(pairing_exists=False)
        ok, msg, detail = await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=80,
        )
        assert not ok
        assert "no wallet with this client" in msg
        assert detail is None

    @pytest.mark.asyncio
    async def test_the_named_vendor_must_have_a_rate(self):
        """
        The rate is looked up for the pairing the trade will OPEN on, not
        the one it landed on — otherwise it would be priced at the wrong
        vendor's rate, which is the error this is meant to correct.
        """
        conn = _Conn(rate_exists=False)
        ok, msg, _ = await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=80,
        )
        assert not ok
        assert "no rate for that pairing" in msg

    @pytest.mark.asyncio
    async def test_a_live_source_trade_is_still_refused(self):
        """
        The existing guard must not be weakened by the new path. A trade the
        client is holding an instruction for keeps its deposit.
        """
        conn = _Conn(source_status="awaiting_payment")
        ok, msg, _ = await _repo(conn).reopen_deposit_as_trade(
            deposit_id=51, actor_party_id=1, supplier_id=80,
        )
        assert not ok
        assert "already has one" in msg
