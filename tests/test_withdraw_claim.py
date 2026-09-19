"""
A claim that will never be matched can be withdrawn.

WHAT HAPPENED (live, 19 September 2026)

The Bridge ran a test /send from Malegao - Sam, nominating "Afrin fathima"
with the hash "1234test". The monitor did its job:

    Malegao - Sam said they had sent 30 minutes ago, but no deposit has
    arrived. They nominated: Afrin fathima. Hash they gave: 1234test.
    Nothing has been opened. Worth checking with them.

Then:

    i did a test, but i am unable to cancel it myself?

He was right. /cancel listed trades, and a claim is not a trade. There was no
way to clear one from anywhere in the product.

WHY IT IS NOT JUST TIDINESS

An unmatched claim is exactly what latest_nomination reads — that is the
whole point of pending_sends, and it is what replaced the audit-log lookup
after 11 September. So the test nomination would have been pre-selected on
Malegao's NEXT REAL deposit: the Bridge shown "Supplier nominated: Afrin
fathima" for a trade nobody had said that about.

That is the 11 September fault reappearing through a test rather than through
the audit log. The Bridge then: "money sent, but no one chose supplier... why
did it default? its should not do that." A default that reads as a decision
is one tap from sending a client to pay the wrong account.

WHERE IT LIVES

In /cancel, because that is what he typed. A second command he would have to
be told about is a worse answer than the one he already reached for.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_trade  # noqa: E402
from db.repo import Repo  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", _code(fn))


class TestCancelCoversBoth:
    def test_it_lists_claims_as_well_as_trades(self):
        src = _code(bridge_trade.cmd_cancel)
        assert "await repo.list_open_trades()" in src
        assert "await repo.outstanding_claims()" in src

    def test_a_claim_gets_its_own_button(self):
        src = _code(bridge_trade.cmd_cancel)
        assert 'callback_data=f"cs:{c[\'id\']}"' in src

    def test_the_button_says_which_supplier_and_which_hash(self):
        """"1234test" is how he will recognise his own test."""
        src = _code(bridge_trade.cmd_cancel)
        assert "c['supplier_label']" in src
        assert "c['hash_url']" in src

    def test_it_explains_what_a_claim_is(self):
        """Otherwise the extra buttons are clutter he scrolls past."""
        src = _code(bridge_trade.cmd_cancel)
        assert "nomination on their next deposit" in src.replace("\"\n            \"", "")

    def test_nothing_at_all_is_still_said_plainly(self):
        src = _code(bridge_trade.cmd_cancel)
        assert "if not trades and not claims:" in src

    def test_cancelling_a_trade_is_untouched(self):
        src = _code(bridge_trade.cmd_cancel)
        assert 'callback_data=f"cx:{t[\'id\']}"' in src


class TestWithdrawingIsSafe:
    def test_a_matched_claim_is_refused(self):
        """
        It has a deposit behind it. Withdrawing it would erase the record of
        which account the supplier actually chose for a real trade.
        """
        sql = _sql(Repo.drop_pending_send)
        assert 'if row["matched_trade_id"] is not None:' in sql

    def test_the_row_is_locked_first(self):
        assert "FOR UPDATE OF ps" in _sql(Repo.drop_pending_send)

    def test_it_is_written_to_the_audit_log_before_it_goes(self):
        sql = _sql(Repo.drop_pending_send)
        assert 'action="send.withdrawn"' in sql
        for field in ("supplier", "account", "hash", "claimed_at"):
            assert f'"{field}"' in sql

    def test_the_row_is_deleted_rather_than_flagged(self):
        """
        latest_nomination and stale_pending_sends both key on
        matched_trade_id IS NULL. A withdrawn claim has to vanish from both,
        and a flag neither of them reads would leave it live in both.
        """
        sql = _sql(Repo.drop_pending_send)
        assert "DELETE FROM pending_sends WHERE id = $1" in sql

    def test_the_bridge_is_told_what_it_stops(self):
        sql = _sql(Repo.drop_pending_send)
        assert "no longer be offered as a nomination" in sql

    def test_no_reason_is_demanded(self):
        """
        A claim carries no money and no instruction. Making him justify
        clearing his own test is how a flow gets abandoned halfway.
        """
        src = _code(bridge_trade.cancel_claim)
        assert "Cancel.reason" not in src
        assert "set_state" not in src


class TestTheQueryAsksTheRightQuestion:
    def test_it_ignores_age_and_whether_it_was_reported(self):
        """
        stale_pending_sends is the monitor deciding what to raise.
        outstanding_claims is the Bridge asking what is outstanding — his
        test had already been alerted on, so an alerted_at filter would have
        hidden the very row he was trying to clear.
        """
        sql = _sql(Repo.outstanding_claims)
        assert "alerted_at" not in sql
        assert "make_interval" not in sql

    def test_it_returns_only_unmatched_claims(self):
        assert "ps.matched_trade_id IS NULL" in _sql(Repo.outstanding_claims)

    def test_it_carries_the_account_that_would_be_nominated(self):
        sql = _sql(Repo.outstanding_claims)
        assert "b.account_name" in sql
        assert "LEFT JOIN bank_accounts" in sql, (
            "a claim with no account must still be listed, or it cannot be "
            "withdrawn either"
        )


class TestItIsTheBridgesAlone:
    def test_no_supplier_or_client_can_withdraw_a_claim(self):
        from bot import client_bot, supplier_bot

        for module in (client_bot, supplier_bot):
            src = Path(module.__file__).read_text()
            assert "drop_pending_send" not in src
