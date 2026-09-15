"""
A deposit joins the trade in progress, not one that has been sitting.

THE INCIDENT (live, 15 September 2026)

SUPA5 opened on the 12th with 28,302 USDT and was never issued. The Bridge
settled it by hand — paid the client 27,777.88 on the 14th — but the trade
stayed open in the ledger because nothing had closed it.

On the 15th at 14:42 a fresh 37,736 USDT arrived on that wallet and merged
into it. The notification read:

    ADDITIONAL deposit from IndoLondon group NEW to Client A
    This transfer: 37,736.00   Previously: 28,302.00
    Trade total now 66,038.00 to Send INR 7,000,028

He was mid-trade with a client waiting:

    ITS STILL STORED PREVIOUS TRADE
    SITTING WITH STOCK
    this is causing more problems that solving

He cancelled SUPA5 and settled the second half by hand too.

WHY MERGING EXISTS AT ALL, AND WHY IT STILL SHOULD

A supplier splitting one send into two transfers minutes apart is one trade,
and pricing it as two would round each half separately and drift (client
question, 10 September 2026). That case is real and common. The rule was
never wrong — it just had no end.

THE WINDOW

A deposit joins the open trade only if that trade took a deposit within
MERGE_WINDOW_MINUTES. Measured from the LAST deposit, not from opening, so
three transfers across an afternoon still make one trade. Past it, the
deposit starts its own.

WHAT HAD TO MOVE WITH IT

The unique index from migration 003 allowed exactly one uninstructed open
trade per wallet. Splitting requires a stale trade and a new one to coexist,
so the index is now a plain one and the guarantee lives in the query.

Losing a database guarantee for a query condition is a real trade, so the
stale trade is no longer allowed to be invisible: the health check reports
any uninstructed trade open more than six hours. SUPA5 sat for three days
and nothing ever mentioned it, which is what let it become an incident.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.notifier import Notifier  # noqa: E402
from config import Config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "deploy" / "migrate-006-merge-window.sql").read_text(encoding="utf-8")
HEALTH = (ROOT / "deploy" / "healthcheck.sh").read_text(encoding="utf-8")


def _sql(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    src = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    return re.sub(r"\s+", " ", src)


class TestTheWindowIsApplied:
    def test_the_lookup_is_time_bounded(self):
        sql = _sql(Notifier.on_supplier_deposit)
        assert "minutes')::interval" in sql, (
            "the open trade is still selected with no age limit, so a trade "
            "from days ago will absorb today's deposit"
        )

    def test_it_measures_from_the_last_deposit_not_from_opening(self):
        """
        A supplier sending in three parts across an afternoon is one trade.
        Measuring from opened_at would split the third one off.
        """
        sql = _sql(Notifier.on_supplier_deposit)
        assert "max(d.detected_at)" in sql
        assert "FROM deposits d" in sql

    def test_a_trade_with_no_deposits_falls_back_to_its_opening_time(self):
        sql = _sql(Notifier.on_supplier_deposit)
        assert "COALESCE(" in sql and "t.opened_at" in sql

    def test_the_other_conditions_are_intact(self):
        """The window narrows; it must not replace what was there."""
        sql = _sql(Notifier.on_supplier_deposit)
        assert "t.wallet_id = $1" in sql
        assert "t.instructed_at IS NULL" in sql
        assert "t.status IN ('open', 'awaiting_payment')" in sql

    def test_no_match_opens_a_new_trade(self):
        src = inspect.getsource(Notifier.on_supplier_deposit)
        assert "if trade is None:" in src
        assert "next_reference" in src


class TestTheWindowIsConfigurable:
    def test_it_comes_from_config(self):
        sql = _sql(Notifier.on_supplier_deposit)
        assert "self.config.merge_window_minutes" in sql

    def test_there_is_a_sensible_default(self):
        src = inspect.getsource(Config)
        assert 'MERGE_WINDOW_MINUTES", "120"' in src, (
            "two hours: long enough for a split send, far short of the three "
            "days that caused the incident"
        )

    def test_the_default_is_hours_not_days(self):
        m = re.search(r'MERGE_WINDOW_MINUTES",\s*"(\d+)"', inspect.getsource(Config))
        assert m and int(m.group(1)) <= 24 * 60, \
            "a window measured in days is the bug, not the fix"


class TestTheIndexNoLongerBlocksTheSplit:
    def test_the_unique_index_is_gone_from_the_schema(self):
        assert "trades_one_uninstructed_per_wallet" not in SCHEMA, (
            "the unique index would reject the new trade and the deposit "
            "would fail instead of splitting"
        )

    def test_a_plain_index_replaces_it(self):
        assert "trades_uninstructed_idx" in SCHEMA

    def test_the_migration_does_both(self):
        assert "DROP INDEX IF EXISTS trades_one_uninstructed_per_wallet" in MIGRATION
        assert "CREATE INDEX IF NOT EXISTS trades_uninstructed_idx" in MIGRATION

    def test_the_migration_is_rerunnable(self):
        assert MIGRATION.count("IF EXISTS") >= 1
        assert MIGRATION.count("IF NOT EXISTS") >= 1


class TestAStaleTradeCannotBeSilent:
    """
    A database guarantee was traded for a query condition. The replacement
    is that the situation is visible instead of prevented — so it has to
    actually be visible.
    """

    def test_the_health_check_reports_trades_left_open(self):
        assert "open and never issued" in HEALTH

    def test_it_names_them(self):
        """A count alone does not tell the Bridge which one to act on."""
        assert "t.reference" in HEALTH and "usdt_received" in HEALTH

    def test_it_is_a_warning_not_a_failure(self):
        """
        A trade waiting a few hours for an instruction is ordinary. It
        should be surfaced, not treated as the system being broken.
        """
        branch = HEALTH.split("STALE_T:-0")[1].split("\nfi")[0]
        assert "warn " in branch
        assert "bad " not in branch

    def test_the_old_guarantee_is_no_longer_claimed(self):
        assert "at most one uninstructed open trade per wallet" not in HEALTH, \
            "the health check would be asserting a rule that no longer holds"
