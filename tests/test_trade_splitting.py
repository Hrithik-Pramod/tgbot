"""
Once a trade has been instructed, it is closed to new deposits.

THE INCIDENT (live, 11 September 2026)

  16:34:30  1,859 USDT lands            SUPB1 opens, 1,859 x 106 = INR 197,054
  16:37:39  instruction issued          client told to pay INR 197,054
  16:47:29  3,000 USDT lands            merged into SUPB1 -> INR 515,054
  17:14:42  supplier /send              re-nominates the account on SUPB1

The client is holding a message that says 197,054. The ledger says 515,054 is
expected. The trade can never close, because the client will never pay a figure
nobody asked them for; and re-issuing for the new total invites them to pay the
first 197,054 twice. The Bridge, seeing the notification:

    "as its wrong, too much"
    "Should be for only Previously: 1,859.00"

THE RULE (client, 11 September 2026)

    once the trade is issued, any new deposit is a new trade

WHAT THAT FORCES

  - trades.instructed_at, set the first time slots are issued and never moved
  - the deposit handler looks only for an UNINSTRUCTED open trade
  - one open trade per wallet becomes one UNINSTRUCTED open trade per wallet,
    because a wallet legitimately has two open at once now
  - a bank account no longer identifies a trade on its own, so payments are
    attributed to the oldest instructed unpaid trade for that account
  - /send must not re-nominate a trade that has already been instructed
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.notifier import Notifier  # noqa: E402
from db.repo import Repo  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "deploy" / "migrate-003-instructed.sql").read_text(encoding="utf-8")


def _sql(fn) -> str:
    """The function's source with whitespace flattened, for matching SQL."""
    return re.sub(r"\s+", " ", inspect.getsource(fn))


class TestADepositCannotJoinAnInstructedTrade:
    def test_the_deposit_handler_requires_an_uninstructed_trade(self):
        sql = _sql(Notifier.on_supplier_deposit)
        assert "instructed_at IS NULL" in sql, (
            "the deposit handler still picks up any open trade, so a deposit "
            "arriving after the instruction grows it instead of starting a new one"
        )

    def test_it_still_looks_up_by_wallet(self):
        """The pairing is the wallet. Narrowing must not lose that."""
        sql = _sql(Notifier.on_supplier_deposit)
        assert "WHERE wallet_id = $1" in sql

    def test_a_missing_trade_opens_a_new_one(self):
        """
        The existing branch is what now runs for the second deposit, so it has
        to still allocate a fresh reference rather than failing.
        """
        src = inspect.getsource(Notifier.on_supplier_deposit)
        assert "if trade is None:" in src
        assert "next_reference" in src


class TestInstructedAtIsSetAndNeverMoved:
    def test_issuing_slots_stamps_it(self):
        sql = _sql(Repo.issue_slots)
        assert "instructed_at" in sql

    def test_reissuing_does_not_reopen_the_trade_to_deposits(self):
        """
        /issue exists precisely so an instruction can be re-sent. If that reset
        the stamp, the trade would start swallowing deposits again and the bug
        would come back through the door we just built.
        """
        sql = _sql(Repo.issue_slots)
        assert "COALESCE(instructed_at, now())" in sql


class TestTheDatabaseEnforcesIt:
    def test_the_old_index_is_gone_from_the_schema(self):
        assert "trades_one_open_per_wallet" not in SCHEMA, (
            "the old unique index would reject the second open trade outright"
        )

    def test_the_new_index_allows_one_uninstructed_trade_per_wallet(self):
        assert "trades_one_uninstructed_per_wallet" in SCHEMA
        flat = re.sub(r"\s+", " ", SCHEMA)
        assert "ON trades (wallet_id) WHERE status IN ('open', 'awaiting_payment') AND instructed_at IS NULL" in flat

    def test_the_column_exists(self):
        assert "instructed_at" in SCHEMA


class TestTheMigration:
    def test_it_drops_the_old_index(self):
        assert "DROP INDEX IF EXISTS trades_one_open_per_wallet" in MIGRATION

    def test_it_creates_the_new_one(self):
        assert "trades_one_uninstructed_per_wallet" in MIGRATION

    def test_it_backfills_from_the_slots_already_issued(self):
        """
        SUPB1 is instructed and open RIGHT NOW. If the backfill misses it, the
        very next deposit merges into it and we have shipped the same bug again
        with a migration on top.
        """
        flat = re.sub(r"\s+", " ", MIGRATION)
        assert "UPDATE trades" in flat
        assert "MIN(issued_at)" in flat
        assert "FROM payment_slots" in flat

    def test_it_is_rerunnable(self):
        assert "ADD COLUMN IF NOT EXISTS" in MIGRATION
        assert "CREATE UNIQUE INDEX IF NOT EXISTS" in MIGRATION

    def test_it_is_one_transaction(self):
        assert MIGRATION.strip().count("BEGIN;") == 1
        assert "COMMIT;" in MIGRATION


class TestPaymentsStillLandOnTheRightTrade:
    """
    The account was the thing that identified a trade. Two open trades for one
    supplier breaks that, and getting it wrong misattributes real money — the
    exact failure this morning, in a new place.
    """

    def test_one_row_per_account(self):
        sql = _sql(Repo.open_trade_accounts_for_client)
        assert "DISTINCT ON (b.id)" in sql, (
            "without this the same account appears twice with two trade ids "
            "and the dict comprehension in client_bot keeps whichever came last"
        )

    def test_unpaid_trades_are_preferred(self):
        sql = _sql(Repo.open_trade_accounts_for_client)
        assert "sum(p.amount_inr)" in sql and ">= t.inr_expected" in sql

    def test_oldest_instruction_wins(self):
        sql = _sql(Repo.open_trade_accounts_for_client)
        assert "t.instructed_at, t.opened_at" in sql

    def test_the_display_order_is_still_by_name(self):
        """The client sees this list; it should not be ordered by row id."""
        sql = _sql(Repo.open_trade_accounts_for_client)
        assert "ORDER BY account_name" in sql


class TestProgressFollowsTheTradeBeingCollected:
    def test_it_prefers_an_instructed_trade(self):
        sql = _sql(Repo.open_trade_for_supplier)
        assert "(t.instructed_at IS NULL), t.instructed_at" in sql, (
            "newest-first would answer with the untouched new trade and tell "
            "the supplier nothing has been collected"
        )


class TestSendCannotRewriteAnIssuedInstruction:
    def test_nomination_skips_instructed_trades(self):
        sql = _sql(Repo.nominate_account)
        assert "instructed_at IS NULL" in sql, (
            "a /send meant for the next deposit overwrote the account on a "
            "trade whose instruction had already gone to the client"
        )

    def test_the_nomination_is_not_lost_when_nothing_matches(self):
        """
        Returning None is correct, not a failure: the claim stays unmatched
        in pending_sends and latest_nomination hands it to the next trade
        that opens. The audit entry must still be written either way, because
        it is the record of who chose what and when.
        """
        src = inspect.getsource(Repo.nominate_account)
        assert "trade.account_nominated" in src
        assert "return row[\"reference\"] if row else None" in src
