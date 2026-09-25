"""
A wallet can be retired without losing what went through it.

THE REQUEST (Bridge, 25 September 2026)

    if i needed to remove an account wallet for a provider, and not have an
    associated wallet in place, can you add this

There was no way to. /walletadd created one and /walletchange moved its
address; a vendor who left stayed on the books for ever. The only lever was
is_monitored, which stops the polling and keeps the row occupying both
unique constraints — so the address could not be reused and the vendor could
not be given a new wallet.

DELETING WAS NEVER AVAILABLE

trades.wallet_id and deposits.wallet_id are NOT NULL references. Wallet 8
alone carried ALPH1 to ALPH5. A delete either fails on the foreign key or,
with a cascade, takes five settled trades with it.

WHAT RETIREMENT CHANGES

retired_at, and both unique indexes made partial on it, exactly as
bank_accounts has done with removed_at since the first build. The row keeps
its address and its history and stops reserving the address and the pairing.

THE LINE THAT MATTERS MOST IN THIS FILE

Not every query should forget a retired wallet, and getting that backwards
costs money in one direction and hides it in the other:

  FORGET it    anything forward-looking — the monitor, the wallet pickers,
               what a client may pay, where a new trade can be opened.
               Offering a retired pairing is offering a dead address.

  REMEMBER it  anything historical — the payment matcher, the book, and
               "where did this deposit land". A slip for a retired vendor's
               old trade must still read (16 September: 'unmatched
               beneficiary Barkaati Textile', a real account dropped from
               the candidates), and stranded USDT on a retired pairing must
               still appear (₹995,495, missed twice).
"""

import inspect
import pathlib
import re
import sys
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_bot  # noqa: E402
from bot.menu import MENUS  # noqa: E402
from db.repo import Repo  # noqa: E402

SCHEMA = (ROOT / "db" / "schema.sql").read_text()
MIGRATION = (ROOT / "deploy" / "migrate-010-wallet-retire.sql").read_text()
REPO_SRC = (ROOT / "db" / "repo.py").read_text()


def _sql_literals(src: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r'"""(.*?)"""', src, re.S)]


# --------------------------------------------------------------- the schema

class TestTheColumnAndTheConstraints:
    def test_the_column_exists_and_is_nullable(self):
        assert "retired_at      TIMESTAMPTZ" in SCHEMA
        line = next(l for l in SCHEMA.splitlines()
                    if "retired_at" in l and "TIMESTAMPTZ" in l)
        assert "NOT NULL" not in line

    def test_the_address_constraint_is_partial(self):
        """
        The whole point. A retired wallet's address must be registrable
        again, or retirement is no better than the is_monitored flag.
        """
        assert "CONSTRAINT wallets_address_unique" not in SCHEMA
        assert "ON wallets (address) WHERE retired_at IS NULL" in SCHEMA

    def test_the_pairing_constraint_is_partial(self):
        """
        And a vendor whose wallet is retired must be able to get a new one.
        """
        idx = SCHEMA.split("CREATE UNIQUE INDEX wallets_pairing_unique")[1]
        head = idx.split(";")[0]
        assert "is_internal" in head and "retired_at IS NULL" in head

    def test_the_monitor_index_skips_retired(self):
        idx = SCHEMA.split("CREATE INDEX wallets_monitored_idx")[1].split(";")[0]
        assert "retired_at IS NULL" in idx

    def test_the_migration_is_idempotent(self):
        assert "ADD COLUMN IF NOT EXISTS retired_at" in MIGRATION
        assert "DROP CONSTRAINT IF EXISTS wallets_address_unique" in MIGRATION
        assert "CREATE UNIQUE INDEX IF NOT EXISTS wallets_address_live_unique" in MIGRATION

    def test_the_migration_replaces_the_pairing_index_rather_than_adding_one(self):
        """
        A second index over the same columns without the retired clause
        would silently keep enforcing the old rule.
        """
        assert "DROP INDEX IF EXISTS wallets_pairing_unique" in MIGRATION

    def test_the_migration_retires_nothing(self):
        assert "SET retired_at" not in MIGRATION


# ------------------------------------------------------- which queries care

FORWARD_LOOKING = {
    "list_wallets":                 "the wallet pickers",
    "wallets_owned_by":             "payout addresses offered for linking",
    "monitored_wallets":            "the monitor",
    "suppliers_for_client":         "what a client may pay",
}

DELIBERATELY_HISTORICAL = {
    "matchable_accounts_for_client": "a late slip must still match",
    "book_progress":                 "stranded USDT must stay visible",
    "pairing_for_deposit":           "where it landed does not change",
}


class TestForwardLookingQueriesForgetRetiredWallets:
    @pytest.mark.parametrize("name", sorted(FORWARD_LOOKING))
    def test_it_filters(self, name):
        src = inspect.getsource(getattr(Repo, name))
        assert "retired_at IS NULL" in src, (
            f"{name} ({FORWARD_LOOKING[name]}) would offer a retired wallet"
        )

    def test_a_new_trade_cannot_open_on_a_retired_pairing(self):
        src = inspect.getsource(Repo.reopen_deposit_as_trade)
        lookup = src.split("WHERE is_internal")[1][:120]
        assert "retired_at IS NULL" in lookup

    def test_adding_a_wallet_ignores_the_retired_one(self):
        """
        The pairing-exists check inside add_wallet. Without the filter a
        vendor whose wallet is retired could never be given another — which
        is the entire request.
        """
        src = inspect.getsource(Repo.add_wallet)
        assert "w.retired_at IS NULL" in src


class TestHistoricalQueriesRememberThem:
    @pytest.mark.parametrize("name", sorted(DELIBERATELY_HISTORICAL))
    def test_it_does_not_filter(self, name):
        src = inspect.getsource(getattr(Repo, name))
        assert "retired_at IS NULL" not in src, (
            f"{name} must NOT exclude retired wallets — {DELIBERATELY_HISTORICAL[name]}"
        )

    @pytest.mark.parametrize("name", sorted(DELIBERATELY_HISTORICAL))
    def test_the_exemption_is_explained_in_place(self, name):
        """
        Three queries that look like an oversight and are not. Each carries
        its reason, or the next person tidies it into a bug.
        """
        src = inspect.getsource(getattr(Repo, name))
        assert "Retired" in src or "retired" in src, (
            f"{name} omits the retired filter with nothing saying why"
        )


# -------------------------------------------------------------- the method

class _Conn:
    def __init__(self, *, retired=False, live=0, dependants=0, exists=True):
        self.retired, self.live, self.dependants, self.exists = (
            retired, live, dependants, exists)
        self.updates: list[str] = []
        self.audits: list[dict] = []

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return _T()

    async def fetchrow(self, sql, *a):
        if not self.exists:
            return None
        return {"id": 8, "address": "TLETtEy", "is_internal": True,
                "retired_at": "2026-09-25" if self.retired else None,
                "supplier_label": "Tata Mahalaxmi", "client_label": "Haze"}

    async def fetchval(self, sql, *a):
        s = " ".join(sql.split())
        if "status IN ('open', 'awaiting_payment')" in s:
            return self.live
        if "payout_wallet_id = $1" in s:
            return self.dependants
        if "FROM trades WHERE wallet_id" in s:
            return 5
        if "FROM deposits WHERE wallet_id" in s:
            return 4
        return 0

    async def execute(self, sql, *a):
        self.updates.append(" ".join(sql.split()))


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
        conn.audits.append({"action": action, "detail": detail})
    r.audit = _audit
    return r


class TestRetiringAWallet:
    @pytest.mark.asyncio
    async def test_it_sets_retired_and_stops_monitoring(self):
        conn = _Conn()
        ok, msg, detail = await _repo(conn).retire_wallet(
            wallet_id=8, actor_party_id=1)
        assert ok, msg
        upd = next(u for u in conn.updates if "UPDATE wallets" in u)
        assert "retired_at = now()" in upd
        assert "is_monitored = FALSE" in upd

    @pytest.mark.asyncio
    async def test_nothing_is_deleted_but_the_cursor(self):
        """
        The row, the trades and the deposits all stay. Only monitor_state —
        a polling offset into an address nobody is watching — goes.
        """
        conn = _Conn()
        await _repo(conn).retire_wallet(wallet_id=8, actor_party_id=1)
        deletes = [u for u in conn.updates if u.startswith("DELETE")]
        assert deletes == ["DELETE FROM monitor_state WHERE wallet_id = $1"]

    @pytest.mark.asyncio
    async def test_it_reports_what_was_kept(self):
        conn = _Conn()
        _, _, detail = await _repo(conn).retire_wallet(wallet_id=8, actor_party_id=1)
        assert detail["trades_kept"] == 5
        assert detail["deposits_kept"] == 4
        assert detail["address"] == "TLETtEy"

    @pytest.mark.asyncio
    async def test_it_is_audited_with_the_name_at_the_time(self):
        """
        Vendor labels follow Telegram group titles and four changed in one
        week. The log has to say who it was when it happened.
        """
        conn = _Conn()
        await _repo(conn).retire_wallet(wallet_id=8, actor_party_id=1)
        entry = next(a for a in conn.audits if a["action"] == "wallet.retired")
        assert entry["detail"]["supplier"] == "Tata Mahalaxmi"
        assert entry["detail"]["address"] == "TLETtEy"


class TestWhatItRefuses:
    @pytest.mark.asyncio
    async def test_a_live_trade(self):
        """
        The one that would cost money. A counterparty holding an
        instruction sends to an address nothing is watching — the USDT
        arrives and is never seen.
        """
        conn = _Conn(live=1)
        ok, msg, _ = await _repo(conn).retire_wallet(wallet_id=8, actor_party_id=1)
        assert not ok
        assert "live trade" in msg
        assert not conn.updates, "it wrote something before refusing"

    @pytest.mark.asyncio
    async def test_a_wallet_other_pairings_pay_through(self):
        """
        payout_wallet_id would be left pointing at a retired row, and the
        Bridge would be told to send a client's USDT to a dead address.
        """
        conn = _Conn(dependants=2)
        ok, msg, _ = await _repo(conn).retire_wallet(wallet_id=8, actor_party_id=1)
        assert not ok
        assert "/walletlink" in msg

    @pytest.mark.asyncio
    async def test_one_already_retired(self):
        conn = _Conn(retired=True)
        ok, msg, _ = await _repo(conn).retire_wallet(wallet_id=8, actor_party_id=1)
        assert not ok
        assert "already retired" in msg

    @pytest.mark.asyncio
    async def test_one_that_is_gone(self):
        conn = _Conn(exists=False)
        ok, msg, _ = await _repo(conn).retire_wallet(wallet_id=8, actor_party_id=1)
        assert not ok
        assert "no longer exists" in msg


# ------------------------------------------------------------- the command

class TestTheCommand:
    def test_it_exists_and_is_in_the_menu(self):
        assert hasattr(bridge_bot, "cmd_walletremove")
        assert "walletremove" in {c for c, _ in MENUS["bridge"]}

    def test_it_is_not_offered_to_a_counterparty(self):
        for role in ("supplier", "client"):
            assert "walletremove" not in {c for c, _ in MENUS[role]}

    def test_the_buttons_are_not_state_gated(self):
        """
        The 22 September lesson. A picker gated on a state nothing sets is a
        button that dies silently.
        """
        src = inspect.getsource(bridge_bot)
        for prefix in ('"wr:"', '"wr_yes:"'):
            line = next(l for l in src.splitlines()
                        if f"startswith({prefix})" in l)
            assert line.strip().startswith("@router.callback_query(F.data")

    def test_the_confirmation_states_what_is_at_stake(self):
        src = inspect.getsource(bridge_bot.walletremove_which)
        assert "wallet_detail" in src
        assert "trades" in src and "deposits" in src

    def test_the_counts_are_read_live_not_carried_in_the_callback(self):
        """
        A button pressed on yesterday's message must not understate how much
        history is about to be taken out of service.
        """
        src = inspect.getsource(bridge_bot.walletremove_which)
        assert "repo.wallet_detail(wallet_id)" in src

    def test_a_live_trade_is_flagged_before_the_button(self):
        src = inspect.getsource(bridge_bot.walletremove_which)
        assert "live_trades" in src

    def test_the_dead_address_warning_is_shown(self):
        """
        The address does not stop existing on TRON when we stop watching it.
        """
        src = inspect.getsource(bridge_bot.walletremove_which)
        assert "will not see it" in src
