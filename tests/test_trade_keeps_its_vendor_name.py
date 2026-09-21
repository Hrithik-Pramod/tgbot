"""
A trade keeps the vendor name it was done under.

THE REQUEST (Bridge, 19 September 2026)

A vendor was renamed mid-book and every trade they had ever done was renamed
with them. I said: "I'll change it so a trade keeps the name it was done
under." Three days later it was still not built.

WHY IT HAPPENS

bot/labels.py rewrites every active party's label from its Telegram group
title every half hour — SYNC_INTERVAL_SECONDS = 1800. Labels therefore drift
by design, and every screen naming a vendor read it live:

    JOIN parties s ON s.id = t.supplier_id   ->   s.label

So the name was never a property of the trade. It was a property of whatever
the group happened to be called at the moment somebody looked.

A settled deal then reads under a name it was never struck under, and a
reconciliation against a bank statement or an invoice issued at the time
stops lining up. That is the same class of problem as a rate changing
underneath a trade, which this system has snapshotted since the first build
for exactly that reason:

    -- Snapshotted at trade creation. Never read the rate through the join
    -- for calculation - a later rate change must not silently rewrite
    -- history.

The name is no different. Both are terms of a deal that already happened.

WHERE THE LINE IS

Pinned:     anything read through a trade. The deal is history.
Live:       rates, wallets, accounts, pending claims. These are about the
            vendor as they are NOW, and showing a stale name there would be
            the opposite mistake.
"""

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO_SQL = (ROOT / "db" / "repo.py").read_text()
NOTIFIER_SQL = (ROOT / "bot" / "notifier.py").read_text()
SCHEMA = (ROOT / "db" / "schema.sql").read_text()
MIGRATION = (ROOT / "deploy" / "migrate-009-supplier-label.sql").read_text()

PIN = "supplier_label_at_trade"


def _sql_literals(src: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r'"""(.*?)"""', src, re.S)]


class TestTheColumnExists:
    def test_the_schema_carries_it(self):
        assert f"{PIN} TEXT" in SCHEMA

    def test_it_is_nullable(self):
        """
        Trades opened before the column existed have no honest value to put
        in it. NOT NULL would have forced one to be invented.
        """
        line = next(l for l in SCHEMA.splitlines() if PIN in l and "TEXT" in l)
        assert "NOT NULL" not in line

    def test_the_migration_is_idempotent(self):
        assert "ADD COLUMN IF NOT EXISTS" in MIGRATION

    def test_the_migration_backfills_existing_trades(self):
        assert "UPDATE trades" in MIGRATION
        assert f"{PIN} = s.label" in MIGRATION
        assert f"{PIN} IS NULL" in MIGRATION, (
            "the backfill must not overwrite a name already pinned"
        )


class TestEveryNewTradePinsIt:
    def test_both_insert_sites_set_it(self):
        """
        There are exactly two places a trade is created: the monitor, when a
        deposit lands, and the reopen. A third added later without this is
        the whole bug back again, so both are asserted by name.
        """
        for src, where in [(NOTIFIER_SQL, "bot/notifier.py"),
                           (REPO_SQL, "db/repo.py")]:
            inserts = [q for q in _sql_literals(src) if "INSERT INTO trades" in q]
            assert inserts, f"no trade INSERT found in {where}"
            for q in inserts:
                assert PIN in q, f"a trade INSERT in {where} does not pin the name"

    def test_it_is_filled_from_the_row_being_inserted(self):
        """
        Taken as a subselect on the supplier_id the INSERT is already using,
        rather than passed in as a parameter — so a caller cannot open a
        trade and forget it, and it cannot disagree with supplier_id.
        """
        for src in (NOTIFIER_SQL, REPO_SQL):
            for q in _sql_literals(src):
                if "INSERT INTO trades" in q:
                    assert "SELECT label FROM parties WHERE id = $2" in q

    def test_the_insert_pins_the_supplier_being_billed(self):
        """
        On a re-attribution the trade's supplier is NOT the one whose
        address the USDT arrived on. $2 is the trade's supplier_id in both
        statements, so the name follows the billing, which is correct.
        """
        for src in (NOTIFIER_SQL, REPO_SQL):
            for q in _sql_literals(src):
                if "INSERT INTO trades" in q:
                    cols = q.split("VALUES")[0]
                    ordered = [c.strip() for c in
                               re.sub(r".*\(", "", cols, count=1).split(",")]
                    assert ordered[1] == "supplier_id", ordered[:3]


class TestEveryTradeScopedReadUsesIt:
    def test_no_trade_query_still_reads_the_live_label(self):
        """
        The regression guard, and the reason this is a source test rather
        than a unit test: the bug was not one bad query, it was every query.
        """
        offenders = []
        for src, name in [(REPO_SQL, "db/repo.py"),
                          (NOTIFIER_SQL, "bot/notifier.py")]:
            for q in _sql_literals(src):
                if "s.id = t.supplier_id" not in q:
                    continue
                if "s.label AS supplier_label" in q and PIN not in q:
                    offenders.append(f"{name}: {q.strip().splitlines()[0][:60]}")
        assert not offenders, "live label read through a trade:\n" + "\n".join(offenders)

    def test_the_fallback_is_the_live_label(self):
        """
        Trades from before the column have NULL in it, and a screen showing
        a blank vendor would be worse than one showing today's name.
        """
        for src in (REPO_SQL, NOTIFIER_SQL):
            for q in _sql_literals(src):
                if PIN in q and "INSERT" not in q:
                    assert f"COALESCE(t.{PIN}, s.label)" in q

    def test_the_deposit_announcement_reads_through_the_trade(self):
        """
        The first message about a deal must not drift from every later one.
        It used to select straight from parties by supplier_id.
        """
        q = next(q for q in _sql_literals(NOTIFIER_SQL)
                 if "supplier_label" in q and "client_label" in q
                 and "INSERT" not in q)
        assert "FROM trades t" in q
        assert "FROM parties s, parties c" not in q


class TestWhatDeliberatelyStaysLive:
    def test_rates_show_todays_name(self):
        """
        A rate is what is on offer NOW. Pinning a name there would show a
        vendor under a name they no longer use while quoting them a live
        price — and the query has no trade to take a name from anyway, so it
        would not even run.
        """
        q = next(q for q in _sql_literals(REPO_SQL)
                 if "FROM rates r" in q and "supplier_label" in q)
        assert PIN not in q
        assert "s.label AS supplier_label" in q

    def test_every_query_naming_the_pin_actually_has_a_trade_in_it(self):
        """
        Catches the copy-paste that would send `t.supplier_label_at_trade`
        into a query with no `trades t` — a runtime SQL error, on a path
        that may only be reached once a week.

        This is not hypothetical: the first pass at this change did exactly
        that to the rates query.
        """
        for src, name in [(REPO_SQL, "db/repo.py"),
                          (NOTIFIER_SQL, "bot/notifier.py")]:
            for q in _sql_literals(src):
                if PIN in q and "INSERT INTO trades" not in q:
                    assert re.search(r"\btrades\s+t\b", q), (
                        f"{name}: {PIN} used with no trades table in scope"
                    )


class TestTheNameIsNotTakenFromTelegramAgain:
    def test_label_sync_does_not_touch_trades(self):
        """
        sync_labels updates parties and must stop there. If it ever reached
        into trades, the pin would be undone every thirty minutes and this
        whole change would be invisible.
        """
        src = (ROOT / "bot" / "labels.py").read_text()
        assert "trades" not in src.lower().replace("trade a", "")
