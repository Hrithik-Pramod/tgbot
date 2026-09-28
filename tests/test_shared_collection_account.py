"""
A shared collection account is asked about, not guessed at.

THE SITUATION (Bridge, 28 September 2026)

    we have two accounts for different businesses ... Two different vendors
    are using the same account

    The account is not owned by any one, its a multi collection account, and
    its very well trusted.

One account, registered under three vendors. A payment slip names the
account, so it cannot say which vendor the money is for — the same shape as
a shared receiving wallet, and for the same reason: the information is not
on the payment.

THE FIX THAT WAS WRITTEN, TESTED AND NOT SHIPPED

He proposed, reasonably:

    the easiest would be that the bot will fill one order at a time, so if
    its a bank account used by different provider, it wont matter, as the
    client is sending until full, then looking at next order

That is one line: group the matcher by (account_number, ifsc) instead of by
account row, and the existing ORDER BY — instructed-first,
not-yet-full-first, oldest-first — already means "fill one order at a time".
It was built and the tests passed.

Checking the live book before deploying showed two orders collecting into
that one account SIMULTANEOUSLY: ₹1,734,800 outstanding on one, ₹19,400 on
the other. Grouping them would have put that second balance onto the first
trade. Asked directly — parallel, confirmed.

So the premise was wrong, and the fix was reverted before it shipped.

WHAT THE "ERRORS" ACTUALLY WERE

The bot asking which account a slip belonged to. That is not a fault. Two
live orders on one account is genuinely ambiguous, and asking is the only
honest answer. The complaint was that the question was hard to answer,
because both buttons looked the same.

SO THE FIX IS THE QUESTION, NOT THE GUESS

_account_button escalates: a unique name is shown alone; a clashing name
gains the last four digits of its number; and — added here — when the
number clashes too, because the account is shared, the button carries how
much is still outstanding on that instruction. That is the only thing left
that differs, the client already holds both figures, and it still says
nothing about which supplier is behind which.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.client_bot import _account_button  # noqa: E402
from db.repo import Repo  # noqa: E402


def _acct(name, number, outstanding=None, _id=1):
    row = {"id": _id, "account_name": name, "account_number": number}
    if outstanding is not None:
        row["outstanding"] = outstanding
    return row


class TestTheMatcherStillOffersEveryCandidate:
    def test_it_is_grouped_per_account_row(self):
        """
        Reverted deliberately on 28 September. Grouping by account number
        would have merged two live orders on one shared account and sent the
        smaller one's balance to the larger.
        """
        sql = inspect.getsource(Repo.matchable_accounts_for_client)
        assert "DISTINCT ON (b.id)" in sql
        assert "DISTINCT ON (b.account_number" not in sql

    def test_the_reason_is_recorded_where_it_will_be_read(self):
        """
        The next person to look at this will have the same good idea. The
        comment has to reach them before they ship it.
        """
        sql = inspect.getsource(Repo.matchable_accounts_for_client)
        assert "parallel" in sql.lower()


class TestTheButtonEscalates:
    def test_a_unique_name_stands_alone(self):
        among = [_acct("Ekta Traders", "111111111111"),
                 _acct("Barkaati Textile", "222222222222")]
        assert _account_button(among[0], among) == "Ekta Traders"

    def test_a_clashing_name_gains_the_last_four_digits(self):
        """
        13 September: two vendors registered the same holder's name at
        different banks, and two identical buttons is not a question.
        """
        among = [_acct("Girish Kumar Ahirwar", "111111111122"),
                 _acct("Girish Kumar Ahirwar", "222222222233")]
        assert _account_button(among[0], among) == "Girish Kumar Ahirwar ••1122"
        assert _account_button(among[1], among) == "Girish Kumar Ahirwar ••2233"

    def test_a_shared_account_falls_through_to_the_amount(self):
        """
        The 28 September case. Same name, same number — the digits cannot
        separate them and only the outstanding balance can.
        """
        among = [_acct("EXAMPLE TRADING CO", "500000000000", D("1734800"), 1),
                 _acct("EXAMPLE TRADING CO", "500000000000", D("19400"), 2)]
        assert _account_button(among[0], among) == \
            "EXAMPLE TRADING CO ••0000 — ₹1,734,800 left"
        assert _account_button(among[1], among) == \
            "EXAMPLE TRADING CO ••0000 — ₹19,400 left"

    def test_the_two_buttons_are_actually_different(self):
        """
        The whole point, asserted directly rather than implied.
        """
        among = [_acct("EXAMPLE TRADING CO", "500000000000", D("1734800"), 1),
                 _acct("EXAMPLE TRADING CO", "500000000000", D("19400"), 2)]
        assert _account_button(among[0], among) != _account_button(among[1], among)


class TestItDegradesHonestly:
    def test_no_outstanding_column_still_renders(self):
        """
        Called from two paths with different queries. A missing column must
        not take the keyboard down — the client would be left holding a
        payment nobody can record, which is the 11 September ₹902,460.
        """
        among = [_acct("EXAMPLE TRADING CO", "500000000000"),
                 _acct("EXAMPLE TRADING CO", "500000000000", _id=2)]
        assert _account_button(among[0], among) == "EXAMPLE TRADING CO ••0000"

    def test_a_single_candidate_never_needs_disambiguating(self):
        among = [_acct("EXAMPLE TRADING CO", "500000000000", D("19400"))]
        assert _account_button(among[0], among) == "EXAMPLE TRADING CO"


class TestTheDisclosureBoundaryHolds:
    def test_no_supplier_name_can_reach_the_button(self):
        """
        11 September: this list went to the client reading "Supplier A — …"
        and told a counterparty the shape of the Bridge's book. The button
        may carry a name, digits and an amount, and nothing else.
        """
        src = inspect.getsource(_account_button)
        # The docstring explains the boundary at length and naturally says
        # "supplier". Only the executable body is under test.
        body = src.split('"""')[2]
        # "label" is not in this list: the body's own comment uses the
        # word, and a check that trips on its own explanation is noise.
        for leak in ("supplier_label", "supplier", "reference", "vendor"):
            assert leak not in body, f"{leak!r} reachable in the button text"

    def test_the_query_exposes_the_amount_not_the_owner(self):
        sql = inspect.getsource(Repo.open_trade_accounts_for_client)
        assert "AS outstanding" in sql
        assert "supplier_label" not in sql
