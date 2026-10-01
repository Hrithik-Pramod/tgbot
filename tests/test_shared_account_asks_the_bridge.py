"""
One account, two vendors: the Bridge decides, not the client.

THE REQUEST (Bridge, 1 October 2026)

    think Bot cannot distinguish two seperate orders to 1 account ... if 2
    vendors using 1 account, before allocation, forget first in and serve ...
    think the bot asks something, on my side ... it asks, which trade ... but
    if the bot asks me, it needs to tell me the time or the age of each trade
    ... i choose, it sets the bot off on the right path

    yes, do it, but only when 2 vendors are using 1 account, thats the
    trigger, not for all trading, and also ensure, if a trade is in flow, and
    a different vendor chooses same account for deposiits, the flow of the
    original is not interupted as the path is already set

WHAT WENT WRONG WITHOUT IT

30 September: royal trading company was registered under two vendors, both
with an order open. Every slip naming it was ambiguous, so the bot asked the
CLIENT, who has no idea the orders exist. Three payments — ₹295,000,
₹298,000 and ₹390,000 — went onto BRAV9 when they belonged to SUPA38. It
took the Bridge two days and a hand-written reconciliation to find them,
and the figures only came right because all six trades balanced to zero
under his allocation and not under the bot's.

The client could never have answered that question. From their side there is
one account and one payment; nothing on the slip distinguishes the orders.
Asking the person who cannot know is how the money moved.

THE THREE RULES, IN ORDER

  1. TRIGGER ONLY ON A SHARED ACCOUNT.
     Two vendors at two different banks under one holder's name is a
     different problem — the client CAN answer that, and the last four
     digits tell them apart (13 September). Untouched.

  2. AN ESTABLISHED PATH IS NEVER INTERRUPTED.
     An order already taking money into that account is the one the client
     is working through. A second vendor opening on the same account does
     not change it and raises no question.

  3. OTHERWISE ASK THE BRIDGE, AND HOLD THE MONEY SAFELY MEANWHILE.
     Written to the database, not memory. The gap between question and
     answer is precisely where ₹902,460 vanished on 11 September.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade, client_bot, notifier as notifier_mod  # noqa: E402
from core.parse import (choose_established_path,  # noqa: E402
                        match_beneficiary, shared_account_for)
from db.repo import Repo  # noqa: E402

SCHEMA = (ROOT / "db" / "schema.sql").read_text()
MIGRATION = (ROOT / "deploy" / "migrate-011-held-payments.sql").read_text()


def _acct(name, number, ifsc="HDFC0005183", _id=1, trade_id=None):
    return {"id": _id, "account_name": name, "account_number": number,
            "ifsc": ifsc, "trade_id": trade_id}


# ---------------------------------------------------------------- rule one

class TestItTriggersOnlyOnAGenuinelySharedAccount:
    def test_one_account_under_two_vendors_is_detected(self):
        among = [_acct("royal trading company", "500000000000002", _id=9),
                 _acct("ROYAL TRADING COMPANY", "500000000000002", _id=28)]
        assert shared_account_for("royal trading company", among) == \
            ("500000000000002", "HDFC0005183")

    def test_case_and_spacing_do_not_hide_it(self):
        """
        The two registrations differed only in case, which is exactly why
        match_account treated them as one name and refused.
        """
        among = [_acct("Royal Trading Company", "500000000000002", _id=9),
                 _acct("ROYAL  TRADING  COMPANY", "500000000000002", _id=28)]
        assert shared_account_for("royal trading company", among) is not None

    def test_same_name_at_two_different_banks_is_NOT_this_case(self):
        """
        13 September: "Girish Kumar Ahirwar" at two banks. The client knows
        which they paid and the button shows the last four digits. Asking
        them stays right, and this must not take it over.
        """
        among = [_acct("Girish Kumar Ahirwar", "111111111122", "HDFC0000003"),
                 _acct("Girish Kumar Ahirwar", "222222222233", "UTIB0001240")]
        assert shared_account_for("Girish Kumar Ahirwar", among) is None

    def test_a_name_matching_one_account_is_not_this_case(self):
        among = [_acct("Ekta Traders", "111111111122"),
                 _acct("Barkaati Textile", "222222222233")]
        assert shared_account_for("Ekta Traders", among) is None

    def test_a_partial_match_is_never_treated_as_shared(self):
        """
        A guess about which account, plus a guess about which order, is two
        guesses deep — and the second one moves money.
        """
        among = [_acct("SUPER TRADING COMPANY (STC)", "500000000000003", _id=2),
                 _acct("SUPER TRADING COMPANY (STC)", "500000000000003", _id=26)]
        assert shared_account_for("SUPER TRAD", among) is None

    def test_nothing_typed_is_not_this_case(self):
        assert shared_account_for(None, []) is None
        assert shared_account_for("", []) is None


# ---------------------------------------------------------------- rule two

def _trade(tid, collected, expected=D("1000000")):
    return {"id": tid, "collected": D(collected), "inr_expected": expected}


class TestAnEstablishedPathIsNeverInterrupted:
    def test_the_order_already_collecting_wins(self):
        """
        The Bridge's own words: "if a trade is in flow, and a different
        vendor chooses same account for deposiits, the flow of the original
        is not interupted as the path is already set".
        """
        trades = [_trade(38, "1243000", D("6390000")), _trade(9, 0)]
        assert choose_established_path(trades) == 38

    def test_a_brand_new_order_does_not_steal_the_path(self):
        trades = [_trade(38, "5000000", D("6390000")), _trade(40, 0)]
        assert choose_established_path(trades) == 38

    def test_a_finished_order_is_not_the_path(self):
        """
        Collected to its figure means the client has moved on. Keeping it as
        the path would send the next order's money to a closed one — the
        30 September failure in reverse.
        """
        trades = [_trade(38, "6390000", D("6390000")), _trade(39, 0)]
        assert choose_established_path(trades) is None

    def test_nothing_started_means_no_path(self):
        assert choose_established_path([_trade(1, 0), _trade(2, 0)]) is None

    def test_two_orders_both_collecting_means_no_path(self):
        """
        The genuinely undecidable case, and the one that actually happened:
        SUPA38 and BRAV9 were both part-paid at once. Guessing between them
        is what put three payments on the wrong order.
        """
        trades = [_trade(38, "1000000", D("6390000")),
                  _trade(9, "500000", D("1440945"))]
        assert choose_established_path(trades) is None


# -------------------------------------------------------------- rule three

class TestTheHoldSurvivesARestart:
    def test_the_table_exists_in_the_schema(self):
        assert "CREATE TABLE held_payments" in SCHEMA

    def test_the_migration_is_idempotent(self):
        assert "CREATE TABLE IF NOT EXISTS held_payments" in MIGRATION

    def test_a_utr_cannot_be_held_twice(self):
        """
        A client re-pasting an unanswered slip must not queue it again, or
        one answer would record it twice.
        """
        assert "held_payments_utr_unique UNIQUE (utr)" in SCHEMA

    def test_it_records_the_physical_account_not_a_row(self):
        """
        Pointing at one of the clashing account rows would be the guess the
        whole hold exists to avoid.
        """
        table = SCHEMA.split("CREATE TABLE held_payments")[1].split(");")[0]
        assert "account_number" in table and "ifsc" in table

    def test_holding_refuses_a_utr_already_recorded(self):
        src = inspect.getsource(Repo.hold_payment)
        assert "SELECT 1 FROM payments WHERE utr = $1" in src

    def test_resolving_writes_the_payment_and_closes_the_hold_together(self):
        """
        One transaction. A payment recorded without releasing the hold would
        be recorded twice on the next tap; a hold released without the
        payment is the money gone.
        """
        src = inspect.getsource(Repo.resolve_held_payment)
        assert "async with conn.transaction():" in src
        assert "INSERT INTO payments" in src
        assert "SET resolved_at = now()" in src

    def test_resolving_twice_is_refused(self):
        src = inspect.getsource(Repo.resolve_held_payment)
        assert "FOR UPDATE" in src
        assert "already been placed" in src

    def test_a_duplicate_utr_at_resolve_time_clears_the_hold(self):
        """
        If it was recorded elsewhere while waiting, the hold must not be left
        open for ever — it would be chased nightly about money already in.
        """
        src = inspect.getsource(Repo.resolve_held_payment)
        assert "UniqueViolationError" in src
        assert "already recorded elsewhere" in src


class TestTheBridgeIsChasedUntilHeAnswers:
    def test_the_sweep_calls_it(self):
        from monitor import tron
        assert "_chase_held_payments" in inspect.getsource(tron.DepositMonitor)

    def test_a_failure_cannot_stop_the_deposit_polling(self):
        from monitor import tron
        src = inspect.getsource(tron.DepositMonitor._chase_held_payments)
        assert "except Exception" in src

    def test_each_one_is_chased_once(self):
        """
        A notice that repeats every five seconds is a notice nobody reads,
        and this one is money off the books.
        """
        src = inspect.getsource(Repo.waiting_held_payments)
        assert "chased_at IS NULL" in src
        assert inspect.getsource(notifier_mod.Notifier.chase_held_payments) \
            .count("mark_held_chased") == 1

    def test_only_unresolved_ones_are_chased(self):
        assert "resolved_at IS NULL" in inspect.getsource(Repo.waiting_held_payments)


# ------------------------------------------------------------ the question

class TestTheQuestionCarriesWhatHeAskedFor:
    def _src(self):
        return inspect.getsource(notifier_mod.Notifier.ask_which_order)

    def test_each_button_shows_the_reference(self):
        assert "t['reference']" in self._src()

    def test_each_button_shows_when_the_order_opened(self):
        """His words: "it needs to tell me the time or the age of each trade"."""
        assert "t['opened_at']" in self._src()

    def test_each_button_shows_what_is_still_outstanding(self):
        """
        Added beyond what he asked for, because on 30 September it was the
        decisive fact: a ₹390,000 slip went onto an order with ₹60,439 left.
        The balance makes that obvious; the opening time does not.
        """
        assert "t['outstanding']" in self._src()

    def test_it_says_nothing_is_recorded_until_he_picks(self):
        assert "Nothing is recorded until you pick" in self._src()

    def test_it_goes_to_the_bridge_and_not_the_client(self):
        src = self._src()
        assert "to_bridge" in src
        assert "to_party" not in src


class TestTheAnswerIsSafeToTapTwice:
    def test_the_handler_is_not_state_gated(self):
        """
        This question can sit for an hour and survive a restart. A gated
        button would die in the meantime and leave the money unanswerable —
        22 September, and 11 September before it.
        """
        src = inspect.getsource(bridge_trade)
        line = next(l for l in src.splitlines() if 'startswith("hp:")' in l)
        assert line.strip() == '@router.callback_query(F.data.startswith("hp:"))'

    def test_it_rechecks_the_order_is_still_open(self):
        src = inspect.getsource(bridge_trade.place_held_payment)
        assert "live_trades_on_account" in src
        assert "no longer open" in src

    def test_it_completes_the_trade_if_that_payment_finished_it(self):
        src = inspect.getsource(bridge_trade.place_held_payment)
        assert "check_completion" in src


# ---------------------------------------------- nothing else changed at all

class TestEverythingElseIsUntouched:
    def test_an_ordinary_match_never_reaches_the_new_path(self):
        """
        One account, one name, one vendor. The common case must not acquire
        a database round trip, let alone a question.
        """
        among = [_acct("Ekta Traders", "111111111122", _id=1, trade_id=5)]
        assert match_beneficiary("Ekta Traders", among) == 1
        assert shared_account_for("Ekta Traders", among) is None

    def test_the_tiebreak_on_a_live_trade_still_works(self):
        """
        16 September's rule: two identically-named registrations, only one
        with an order open, resolves without asking anyone.
        """
        among = [_acct("SUPER TRADING COMPANY (STC)", "500000000000003",
                       _id=2, trade_id=38),
                 _acct("SUPER TRADING COMPANY (STC)", "500000000000003",
                       _id=26, trade_id=None)]
        assert match_beneficiary("SUPER TRADING COMPANY (STC)", among) == 2

    def test_the_new_branch_only_runs_when_nothing_matched(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        assert "if account_id is None:" in src
        assert "_place_on_shared_account" in src

    def test_it_falls_through_when_fewer_than_two_orders_are_live(self):
        """
        Names clashed but only one order is open — match_beneficiary's own
        tiebreak covers that, and two ways of doing one thing is how they
        drift apart.
        """
        src = inspect.getsource(client_bot._place_on_shared_account)
        assert "len(trades) < 2" in src
        assert "return None" in src

    def test_the_client_is_never_told_there_are_two_orders(self):
        """
        11 September: this list went out reading "Supplier A — …" and told a
        counterparty the shape of the book. The client's line says the
        account and nothing else.
        """
        src = inspect.getsource(client_bot._place_on_shared_account)
        body = src.split('"""')[2]
        for leak in ("reference", "vendor", "supplier", "opened_at",
                     "outstanding"):
            assert leak not in body, f"{leak!r} could reach the client"
