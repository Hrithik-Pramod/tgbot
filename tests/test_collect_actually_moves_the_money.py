"""
When the Bridge says which order an account is filling, the matcher obeys.

    Hello Peter, the Collect button does not work, set it and its still
    collecting on the other open trade
    — Bridge, 23 October 2026, 08:23

He was right, and the button was not the broken part. /collect wrote the row
and the matcher never read it.

WHY IT ONLY FAILED FOR HIM THIS WEEK

collection_paths was read in exactly one place: _place_on_shared_account.
That branch runs when a beneficiary resolves to SEVERAL candidate rows, which
happens when several VENDORS have registered the same physical account. That
was the shape of the problem it was built for on 16 October.

One vendor with two orders on one account is a different shape.
matchable_accounts_for_client returns DISTINCT ON (b.id) — one row per
account — so that case comes back as a single candidate, the shared-account
branch never runs, and the trade is picked by the ORDER BY instead. The
ordering's own rule is "unfilled before filled, then oldest first", so every
payment kept landing on the older order no matter what he had chosen.

Meanwhile /collect told him:

    Payments into it go there without asking, until it is full.

/collect was offering that account correctly all along —
shared_accounts_with_live_orders groups on (account_number, ifsc) and keeps
any with HAVING count(DISTINCT t.id) > 1, which counts ORDERS, not vendors.
So he was offered the choice, he made it, it was stored, and nothing read it.

WHAT THIS TEST CAN AND CANNOT DO

There is no database here, so this cannot prove rows come back in the right
order. It pins the things that actually broke: that the clause exists, that
it is scoped tightly enough not to move another account's money, that it sits
ABOVE the rules it has to beat, and that the key it matches on is the same
key /collect writes. The last one is the quiet killer — account_number and
ifsc written in one format and read in another would leave this failing
exactly as it did before, with every test green.
"""

import inspect
import re
import sys
from pathlib import Path

import pglast
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.repo import Repo  # noqa: E402


def _sql(method) -> str:
    """
    The SQL out of a repo method, with the -- comments stripped.

    Stripping is not tidiness. These queries carry more explanation than
    code, and the first version of this file searched the raw text: the
    comment explaining the bug contains the words "ORDER BY", so rindex
    landed in the prose and the positional tests read nonsense.

    The same trap has caught this project before — a mutation survived
    because the column it deleted was still named in a comment three lines
    up. A test that can be satisfied by a sentence is not testing the query.
    """
    src = inspect.getsource(method)
    i = src.index('"""', src.index("conn.fetch"))
    j = src.index('"""', i + 3)
    body = src[i + 3:j]
    return "\n".join(re.sub(r"--.*$", "", line) for line in body.splitlines())


MATCHER = _sql(Repo.matchable_accounts_for_client)


class TestTheClauseIsThere:
    def test_the_matcher_reads_collection_paths_at_all(self):
        """
        The whole bug in one assertion: it did not.
        """
        assert "collection_paths" in MATCHER, (
            "the matcher still ignores what the Bridge chose with /collect"
        )

    def test_it_only_counts_a_path_that_is_still_live(self):
        """
        cleared_at is what stops a path outliving its order. Without it a
        completed order would go on claiming its account's payments.
        """
        clause = MATCHER[MATCHER.index("collection_paths"):]
        assert "cp.cleared_at IS NULL" in clause[:400]

    def test_it_is_tied_to_this_account_and_this_trade(self):
        """
        Scope. A path set on one account must not reorder another's, and it
        must match the trade in the row being ordered — not merely exist.
        """
        clause = MATCHER[MATCHER.index("collection_paths"):][:500]
        for needed in ("cp.account_number = b.account_number",
                       "cp.ifsc = b.ifsc",
                       "cp.trade_id = t.id",
                       "cp.client_id = $1"):
            assert needed in clause, needed

    def test_the_query_still_parses(self):
        pglast.parse_sql(MATCHER.replace("$1", "1"))


class TestItOutranksTheRulesItHasTo:
    """
    Ordering is the fix. A correct clause in the wrong position is the bug.
    """

    def _pos(self, needle: str) -> int:
        order_by = MATCHER[MATCHER.rindex("ORDER BY"):]
        return order_by.index(needle)

    def test_it_beats_oldest_first(self):
        """
        "First in, first filled" is what sent his payments to the wrong
        order. His answer has to come first.
        """
        assert self._pos("collection_paths") < self._pos("t.opened_at")

    def test_it_beats_unfilled_before_filled(self):
        assert self._pos("collection_paths") < self._pos(">= t.inr_expected")

    def test_it_beats_instructed_before_uninstructed(self):
        assert self._pos("collection_paths") < self._pos("t.instructed_at IS NULL")

    def test_an_account_with_no_live_order_still_sorts_last(self):
        """
        The one rule it must NOT beat. A path cannot resurrect a row whose
        trade is NULL, and NOT EXISTS is true for those — so (t.id IS NULL)
        has to be tested first or they would climb above real orders.
        """
        assert self._pos("t.id IS NULL") < self._pos("collection_paths")

    def test_it_sorts_the_chosen_order_to_the_top_not_the_bottom(self):
        """
        NOT EXISTS is deliberate: false sorts before true in ascending
        order, so the row the Bridge named comes first. Dropping the NOT
        would silently do the exact opposite of what he asked for, which is
        indistinguishable from the bug.
        """
        clause = MATCHER[MATCHER.index("(NOT EXISTS"):][:60]
        assert clause.startswith("(NOT EXISTS")
        order_by = MATCHER[MATCHER.rindex("ORDER BY"):]
        assert "DESC" not in order_by[:order_by.index("collection_paths")], (
            "a DESC on the way in would flip the chosen order to last"
        )


class TestTheKeyMatchesWhatCollectWrites:
    """
    The silent-failure case. Both sides must name the same columns.
    """

    def test_collect_offers_accounts_keyed_the_same_way(self):
        sql = _sql(Repo.shared_accounts_with_live_orders)
        assert "SELECT b.account_number, b.ifsc" in sql

    def test_collect_offers_an_account_with_two_orders_from_one_vendor(self):
        """
        The case he hit. If this ever counted vendors instead of orders,
        /collect would stop offering the account and the fix above would be
        unreachable.
        """
        sql = _sql(Repo.shared_accounts_with_live_orders)
        assert "HAVING count(DISTINCT t.id) > 1" in sql
        assert "count(DISTINCT t.supplier_id)" not in sql

    def test_the_path_is_written_with_the_account_not_an_account_id(self):
        src = inspect.getsource(Repo.set_collection_path)
        assert "account_number" in src and "ifsc" in src

    def test_the_lookup_reads_it_back_with_the_same_three_keys(self):
        src = inspect.getsource(Repo.collection_path)
        for needed in ("cp.client_id = $1", "cp.account_number = $2",
                       "cp.ifsc = $3"):
            assert needed in src, needed


class TestThePromiseMatchesTheBehaviour:
    def test_collect_does_not_claim_it_will_ask_again(self):
        """
        A message that contradicts what the bot does is how an evening gets
        lost. Once the chosen order fills, its path is cleared and the
        matcher moves to the next order by itself — on a single-vendor
        account nothing asks, because the branch that asks never runs.
        """
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        said = src[src.index("is now first in line on that account"):][:400]
        assert "the next one asks again" not in said
        assert "/collect again" in said

    def test_a_closing_order_releases_its_account(self):
        """
        What makes the sentence above true.
        """
        assert "cleared_at = now()" in inspect.getsource(
            Repo.clear_collection_paths_for_trade)
