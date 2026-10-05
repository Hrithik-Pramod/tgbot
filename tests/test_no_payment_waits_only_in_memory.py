"""
A payment the bot cannot place is written down before anyone is asked.

THE PATTERN THE BRIDGE NAMED

    it keeps doing this peter, the bot has issue, then misses
    can we fix this so never happens again?
    — Bridge, 4 October 2026, 11:55pm

He is describing one mechanism, three times:

  11 September   ₹902,460, four payments, sat in an unanswered prompt until a
                 restart discarded them. The first anyone knew was the client
                 asking why their completed trade had not closed.
  28-30 Sept     six slips, ₹1,439,018, all written "SUPER TRADING COMPANY"
                 or "SUPER TRAD". Read by the bot, recorded nowhere.
  4 October      ₹250,000, two hours unrecorded, found only because he went
                 looking at midnight.

Every one went through the same door: the beneficiary matched no registered
account, the bot asked the client which one, and until somebody tapped a
button the payment existed only in a Python dictionary. Nothing survived a
restart. Nothing chased.

WHAT WAS ALREADY TRUE, AND IS NOW TRUE OF BOTH

held_payments was built on 1 October for the other case — two vendors sharing
an account, where the Bridge is asked. That case has not lost a rupee, for one
reason: the money is on the ledger from the moment it is read, and he is
chased until he answers.

So the question of WHO is asked stays as it was — the client can genuinely
answer "which of your accounts", and only they know. What changes is that the
waiting is written down either way, because that was never the part the two
cases disagreed about.

AND THE CHASE CARRIES THE ORDERS

Chasing the Bridge to chase the client is a message that moves nothing. He can
place it himself, and usually knows better than they do: they are choosing
from a list of names, he knows which order is live.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot, notifier as notifier_mod  # noqa: E402
from db.repo import Repo  # noqa: E402


class TestTheWaitIsWrittenDown:
    def test_the_unmatched_branch_holds_the_payment(self):
        """
        The branch that asks the client which account must write the money
        down first. Until 5 October it asked and wrote nothing.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        assert "hold_payment" in src, (
            "an unmatched payment is still waiting only in memory — this is "
            "the ₹902,460 mechanism"
        )

    def test_it_holds_before_it_returns(self):
        """
        Ordering matters: the branch ends in a `return`, so a hold written
        after it would never run.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        marker = 'lines.append("Which account did these go to?")'
        assert marker in src
        after = src[src.index(marker):]
        hold = after.index("hold_payment")
        ret = after.index("\n        return")
        assert hold < ret, "the hold is written after the branch returns"

    def test_it_keeps_what_the_client_actually_typed(self):
        """
        The bot's reading of the name is the thing that failed, so the useful
        record is the raw text. On 11 September the only record was "account
        not recognised" and diagnosing it meant asking someone to find the
        message.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        assert "typed_beneficiary" in src

    def test_the_staged_payment_carries_the_name(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        assert '"beneficiary": p.beneficiary' in src

    def test_the_hold_no_longer_demands_an_account(self):
        """
        The account is what failed. Inventing one would be the guess the
        table exists to avoid.
        """
        sig = inspect.signature(Repo.hold_payment)
        assert sig.parameters["account_number"].default is None
        assert sig.parameters["ifsc"].default is None

    def test_the_schema_and_migration_agree(self):
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        mig = (ROOT / "deploy" / "migrate-013-hold-unmatched.sql").read_text(
            encoding="utf-8")
        assert "typed_beneficiary" in schema
        assert "typed_beneficiary" in mig
        assert "DROP NOT NULL" in mig
        assert "ADD COLUMN IF NOT EXISTS" in mig

    def test_a_row_must_still_say_something_about_where_the_money_went(self):
        """
        Both nullable would allow a row naming neither an account nor a name,
        which is a payment nobody can be asked about.
        """
        for path in ("db/schema.sql", "deploy/migrate-013-hold-unmatched.sql"):
            text = (ROOT / path).read_text(encoding="utf-8")
            assert "held_payments_knows_something" in text, path


class TestTheHoldIsReleasedWhenTheMoneyLands:
    def test_recording_releases_the_hold(self):
        """
        Otherwise the Bridge is chased about money already on the books, and
        a chaser that cries wolf is one he stops reading.
        """
        src = inspect.getsource(client_bot._record)
        assert "release_held_payments" in src

    def test_it_releases_by_utr(self):
        sql = inspect.getsource(Repo.release_held_payments)
        assert "p.utr = h.utr" in sql
        assert "resolved_at IS NULL" in sql

    def test_it_records_where_the_money_ended_up(self):
        sql = inspect.getsource(Repo.release_held_payments)
        assert "resolved_trade_id = p.trade_id" in sql

    @pytest.mark.asyncio
    async def test_releasing_nothing_touches_the_database(self):
        r = Repo.__new__(Repo)
        r.pool = None          # any access would raise
        assert await r.release_held_payments([]) == 0


class TestTheChaseCanActuallyPlaceIt:
    def test_the_chaser_handles_a_hold_with_no_account(self):
        """
        It read account_number[-4:] unconditionally. On the new kind of hold
        that is None, and the chaser — the one thing standing between a
        forgotten payment and a lost one — would have thrown every sweep.
        """
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        assert 'h["account_number"] is not None' in src

    def test_the_chaser_offers_the_orders(self):
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        assert "placeable_trades_for_client" in src
        assert "hp:" in src

    def test_the_chaser_shows_him_what_was_typed(self):
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        assert "typed_beneficiary" in src

    def test_placing_it_works_for_both_kinds_of_hold(self):
        src = inspect.getsource(bridge_place := _place())
        assert "placeable_trades_for_client" in src
        assert "live_trades_on_account" in src

    def test_every_offered_order_has_an_account_to_book_against(self):
        """
        payments.beneficiary_account_id is NOT NULL, so an order with no
        usable account must not become a button that cannot work.
        """
        sql = inspect.getsource(Repo.placeable_trades_for_client)
        assert "COALESCE(" in sql
        assert "nominated_account_id" in sql
        assert "FROM payment_slots" in sql
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        assert 't["account_id"] is not None' in src

    def test_it_prefers_the_account_the_client_was_told_to_pay(self):
        sql = inspect.getsource(Repo.placeable_trades_for_client)
        slot = sql.index("FROM payment_slots")
        nominated = sql.index("nominated_account_id")
        assert slot < nominated, (
            "the instruction the client was actually given should win over "
            "the vendor's later preference"
        )


def _place():
    from bot import bridge_trade
    return bridge_trade.place_held_payment


class TestTheOtherKindIsUntouched:
    """
    The shared-account hold has not lost a rupee since 1 October. Nothing
    here is worth regressing it for.
    """

    def test_the_shared_account_wording_still_stands(self):
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        assert "Two vendors share this account" in src

    def test_it_still_refuses_to_hold_a_utr_already_recorded(self):
        src = inspect.getsource(Repo.hold_payment)
        assert "SELECT 1 FROM payments WHERE utr = $1" in src

    def test_the_unique_utr_still_stops_a_double_queue(self):
        src = inspect.getsource(Repo.hold_payment)
        assert "UniqueViolationError" in src
