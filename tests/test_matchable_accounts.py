"""
Recognising a name the client typed is not the same as showing them a list.

WHAT HAPPENED (live, 16 September 2026)

The Bridge changed a rate mid-trade. Nothing could apply it, so he cancelled
the trades and started again — the only lever he had. Cancelling those trades
took their suppliers' accounts out of the client's candidate list, and the
client kept paying them:

    15:44  unmatched beneficiary 'Barkaati Textile'
    17:37  unmatched beneficiary 'PRIME PATH ENTERPRISES GGN'
           candidates: Bhagavati trading co, Ekta traders,
                       SUPER TRADING COMPANY (STC), royal trading company

Every candidate belongs to one supplier — IndoLondon. Both rejected accounts
are real, active and registered, to Malegao - Sam and GS Group. Their trades
had been cancelled, so nothing could read the payments at all.

    Peter bot is not reading the slips
    Just fyi, lots of slips keep missing Pter

THE MISTAKE UNDERNEATH IT

open_trade_accounts_for_client narrows to suppliers the client has been
INSTRUCTED to pay. That narrowing came out of the 11 September disclosure
incident and is right for everything the client SEES — /accounts, and the
buttons offered when an account cannot be matched.

It was being used for a second, different job: deciding what the bot is
able to RECOGNISE. The client already knows who they paid; reading that name
back to attribute a payment tells them nothing they did not type. Using one
list for both jobs meant a disclosure rule silently decided which payments
could be recorded.

THE SEPARATION

  matchable   every active account of every supplier paired with this
              client. Matching only.
  accounts    instructed suppliers only. Anything the client is shown.

And a matched account whose supplier has nothing open resolves to no trade —
reported to the Bridge by supplier name, never recorded against a guess and
never silent.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", _code(fn))


class TestMatchingSeesEverySupplier:
    def test_the_query_is_not_limited_to_instructed_trades(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "JOIN payment_slots" not in sql, (
            "requiring an issued instruction is what hid Barkaati Textile "
            "and PRIME PATH from the matcher"
        )

    def test_it_walks_the_pairings_rather_than_the_trades(self):
        """
        Suppliers are paired with a client by internal wallet. That pairing
        exists whether or not a trade is open, which is the point.
        """
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "FROM wallets w" in sql
        assert "WHERE w.is_internal AND w.client_id = $1" in sql

    def test_only_active_accounts_are_offered(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "b.is_active" in sql

    def test_one_row_per_account(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "DISTINCT ON (b.id)" in sql

    def test_it_carries_the_supplier_so_a_gap_can_be_named(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "s.label AS supplier_label" in sql


class TestTheTradeIsStillChosenCarefully:
    def test_an_open_trade_is_preferred_over_none(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "(t.id IS NULL)" in sql

    def test_instructed_before_uninstructed(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "(t.instructed_at IS NULL)" in sql

    def test_unpaid_before_paid_then_oldest(self):
        """FIFO, as the client pays the instruction they were given."""
        sql = _sql(Repo.matchable_accounts_for_client)
        assert ">= t.inr_expected" in sql
        assert "t.opened_at" in sql

    def test_the_join_allows_no_open_trade(self):
        sql = _sql(Repo.matchable_accounts_for_client)
        assert "LEFT JOIN trades t" in sql


class TestTheClientStillSeesOnlyWhatTheyShould:
    """
    The 11 September disclosure rule is untouched. Widening what the bot can
    recognise must not widen what it displays.
    """

    def test_the_handler_keeps_both_lists(self):
        src = _code(client_bot.on_pasted_payment)
        assert "matchable = await repo.matchable_accounts_for_client" in src
        assert "accounts = await repo.open_trade_accounts_for_client" in src

    def test_matching_uses_the_wide_list(self):
        """
        match_beneficiary since 16 September — the same matcher with a
        tiebreak for one account registered under two vendors. The list it
        is handed is what this test is about, and that is still the wide one.
        """
        src = _code(client_bot.on_pasted_payment)
        assert "match_beneficiary(p.beneficiary, matchable)" in src

    def test_the_buttons_use_the_narrow_list(self):
        """
        The keyboard specifically — matching happens earlier in the same
        function and legitimately reads the wide list, so this looks at the
        InlineKeyboardMarkup that is actually sent.
        """
        src = _code(client_bot.on_pasted_payment)
        kb = src.split("InlineKeyboardMarkup(inline_keyboard=[")[1] \
                .split("])")[0]
        assert "for a in accounts" in kb
        assert "matchable" not in kb, (
            "the client would be shown every vendor's accounts — the "
            "11 September disclosure, exactly"
        )

    def test_accounts_command_is_unchanged(self):
        src = _code(client_bot.cmd_accounts)
        assert "open_trade_accounts_for_client" in src
        assert "matchable" not in src


class TestAPaymentWithNowhereToGoIsReported:
    def test_a_matched_account_with_no_open_trade_is_caught(self):
        src = _code(client_bot.on_pasted_payment)
        assert 'homeless = [s for s in staged if s["account_id"] and not s["trade_id"]]' in src

    def test_the_client_is_told_it_was_not_recorded(self):
        src = _code(client_bot.on_pasted_payment)
        branch = src.split("if homeless:")[1].split("return")[0]
        assert "NOT been recorded" in branch

    def test_the_bridge_alert_names_the_supplier(self):
        """
        "no open trade" is not actionable. "no open trade for Malegao - Sam"
        tells him exactly which one to open.

        The name is read in the notifier, not here: a supplier label must not
        appear in a client-facing handler at all, however it is being used.
        That rule is blunt on purpose — it is what caught the 11 September
        leak — so the handler hands over the rows instead.
        """
        from bot.notifier import Notifier

        handler = _code(client_bot.on_pasted_payment)
        assert "alert_no_open_trade" in handler
        assert "supplier_label" not in handler

        alert = _code(Notifier.alert_no_open_trade)
        assert "supplier_label" in alert
        assert "to_bridge" in alert

    def test_an_edit_is_held_to_the_same_rule(self):
        src = _code(client_bot.on_edited_payment)
        assert 'if row["trade_id"] is None:' in src
        assert "alert_no_open_trade" in src
        assert "supplier_label" not in src

    def test_an_unmatchable_name_with_nothing_instructed_is_not_a_dead_end(self):
        """
        The buttons come from instructed trades. With none, asking "which
        account?" offers an empty keyboard — which is where the client was
        left on 16 September.
        """
        src = _code(client_bot.on_pasted_payment)
        assert "if unmatched and not accounts:" in src
