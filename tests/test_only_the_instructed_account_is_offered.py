"""
The client is offered the accounts this trade was instructed against.

    when i was correcting this morning, adding utrs that bot did not pick
    up, it showed this list of accounts to allocate to, its mirroring the
    accounts on the vendors login, regardless if it was used for this trade
    or not
    — Bridge, 6 November 2026

Eight buttons came back: KISAN TRADERS, MAHALAXMI ENTERPRISES, ORB INDIA
TECHSOLENCY PVT LTD, PRIME PATH ENTERPRISES GGN, ROYAL TRADING COMPANY,
SUPER TRADING COMPANY twice, royal trading company again. The trade had been
instructed against one.

THE BUG WAS ONE JOIN

    JOIN payment_slots ps ON ps.trade_id = t.id
    JOIN bank_accounts b ON b.party_id = t.supplier_id AND b.is_active

The payment_slots join proved only that the trade had been instructed at
all. The bank_accounts join then took every active account belonging to that
vendor. So "instructed" was a yes/no filter on the TRADE and never reached
the ACCOUNT.

It reads as though the work had been done, which is presumably why it
survived 9 October — the day the identical fault was fixed in
matchable_accounts_for_client and live_trades_on_account, after ₹288,000 for
Uncle landed on IndoLondon's SUPA50 because both vendors hold Royal Trading:

    i think we need a rule to identify the combination, different supplier +
    same account = different debt

    if a trade is happening, from supplier A to account A ... Acc B which
    was never allocated to this deal, should never break the expected
    outcome
    — Bridge, 9 October 2026

Every extra button is that same misattribution, offered as a menu, to the
one person who cannot know which is right.
"""

import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.repo import Repo  # noqa: E402


def _sql(method) -> str:
    src = inspect.getsource(method)
    return "\n".join(re.sub(r"--.*$", "", l) for l in src.splitlines())


OFFERED = _sql(Repo.open_trade_accounts_for_client)
INSTRUCTED = OFFERED[:OFFERED.index("if instructed:")]


class TestTheAccountComesFromTheInstruction:
    def test_it_joins_the_slot_to_the_account(self):
        """
        The whole fix. Without this the slot names the trade and the
        account comes from the vendor's list.
        """
        assert "b.id = ps.bank_account_id" in INSTRUCTED, (
            "the picker is offering every account the vendor holds"
        )

    def test_it_no_longer_joins_on_the_vendor(self):
        assert "b.party_id = t.supplier_id" not in INSTRUCTED, (
            "still pulling the vendor's whole account list"
        )

    def test_retired_accounts_are_still_excluded(self):
        assert "b.is_active" in INSTRUCTED

    def test_only_live_trades(self):
        assert "t.status IN ('open', 'awaiting_payment')" in INSTRUCTED

    def test_it_is_scoped_to_this_client(self):
        assert "t.client_id = $1" in INSTRUCTED

    def test_the_query_parses(self):
        import pglast
        # The SQL, not the docstring above it: both are triple-quoted, and
        # slicing from the first """ lands in the prose.
        i = INSTRUCTED.index('"""', INSTRUCTED.index("conn.fetch"))
        j = INSTRUCTED.index('"""', i + 3)
        pglast.parse_sql(INSTRUCTED[i + 3:j].replace("$1", "1"))


class TestTheSameFixEverywhereElse:
    """
    Three queries answer "which account, and whose order" and all three
    must agree. Two were corrected on 9 October; this one was missed, so
    the agreement is now asserted rather than assumed.
    """

    def test_the_matcher_requires_a_slot_on_the_account(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "ps.bank_account_id = b.id" in sql

    def test_the_live_orders_lookup_requires_one(self):
        sql = _sql(Repo.live_trades_on_account)
        assert "ps.bank_account_id = b.id" in sql or \
               "b.id = ps.bank_account_id" in sql

    def test_live_trades_on_account_is_narrowed_differently_and_that_is_fine(self):
        """
        It DOES join b.party_id = t.supplier_id, and that is correct here:
        it is looking the account up by number and IFSC, which it also
        matches on, and it carries the EXISTS(payment_slots) guard as well.

        The first version of this test asserted that no query may join on
        the vendor at all, and flagged this one. The rule that actually
        matters is not "never join on the vendor" — it is "never let the
        vendor alone decide which account". Asserting the crude version
        would have pushed a correct query into a worse shape.
        """
        sql = _sql(Repo.live_trades_on_account)
        assert "b.account_number = $2" in sql and "b.ifsc = $3" in sql
        assert "ps.bank_account_id = b.id" in sql

    def test_the_picker_reaches_the_account_only_through_the_slot(self):
        assert "b.party_id = t.supplier_id" not in INSTRUCTED


class TestWhatTheClientStillSees:
    def test_the_outstanding_figure_survives(self):
        """
        With one account genuinely shared by two vendors there are still
        two buttons, and the amount left is the only thing that tells them
        apart. It is the client's own figure from their own instructions.
        """
        assert "AS outstanding" in INSTRUCTED

    def test_no_vendor_name_is_selected(self):
        select = INSTRUCTED[INSTRUCTED.index("SELECT DISTINCT ON"):
                            INSTRUCTED.index("FROM trades")]
        for leak in ("supplier_label", "s.label", "supplier_name"):
            assert leak not in select, leak

    def test_one_row_per_account_still(self):
        """
        DISTINCT ON (b.id) is what stops one account appearing once per
        instruction against it.
        """
        assert "DISTINCT ON (b.id)" in INSTRUCTED

    def test_the_oldest_unpaid_instruction_still_wins(self):
        assert "t.instructed_at" in INSTRUCTED
        assert ">= t.inr_expected" in INSTRUCTED


class TestTheUninstructedFallbackIsUnchanged:
    """
    When nothing has been instructed there is no slot to join to, and the
    docstring's argument holds: the client can only be paying one of the
    open trades, and there is nothing narrower to offer. Left alone
    deliberately — but asserted, so removing it is a decision.
    """

    def test_it_still_exists(self):
        assert "if instructed:" in OFFERED
        assert OFFERED.count("return await conn.fetch") >= 1

    def test_it_is_only_reached_when_nothing_was_instructed(self):
        assert OFFERED.index("if instructed:") < OFFERED.rindex("FROM trades t")
