"""
The 18 September list, built.

    • Additional FX groups: To save time, I'm going to create some more FX
      groups now so they are ready for later use add bots
    • Vendor payment account change: We need a way to handle situations where
      the vendor changes the payment account after the payment details have
      already been sent.
    • Cancellation control: There needs to be a way to cancel or correct a
      transaction cleanly if something changes mid-process, while keeping the
      related records linked correctly.
    • Rate changes mid-journey: We also need the ability to amend the rate
      during an active transaction/journey, with the change properly
      reflected and tracked.

The fourth was /reprice, already live and covered by tests/test_reprice.py.
The other three are here, and all three are the same shape: the happy path
worked and the correction was a developer with a terminal.

  FX groups       deploy/seed-supplier.sql, run by me, every time
  account change  re-issue worked; nobody told the client
  cancellation    the cancel was clean; the cleanup was two SQL scripts I
                  wrote two days after 9,354 USDT went missing from the
                  ledger
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import auth, bridge_bot, bridge_trade  # noqa: E402
from db.repo import Repo  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", _code(fn))


# ======================================================================
# Additional FX groups
# ======================================================================

class TestAVendorCanBeRegisteredFromTheBot:
    def test_the_command_exists_on_the_bridge(self):
        src = Path(bridge_bot.__file__).read_text()
        assert 'Command("addvendor")' in src

    def test_no_other_bot_has_it(self):
        from bot import client_bot, supplier_bot

        for module in (client_bot, supplier_bot):
            assert 'Command("addvendor")' not in Path(module.__file__).read_text()

    def test_party_counter_and_wallet_are_one_transaction(self):
        """
        A party with no counter passes every check until its first deposit,
        when next_reference raises and the deposit fails. Half an onboarding
        is not a vendor.
        """
        sql = _sql(Repo.onboard_supplier)
        assert "async with conn.transaction():" in sql
        assert "INSERT INTO parties" in sql
        assert "INSERT INTO supplier_counters" in sql
        assert "INSERT INTO wallets" in sql

    def test_it_does_not_adopt_the_wallet(self):
        """
        Seeding an adoption baseline by hand is how 38 old transfers were
        once ingested as live deposits. The monitor adopts on first poll.
        """
        sql = _sql(Repo.onboard_supplier)
        assert "adopted_at_ms" not in sql
        assert "monitor_state" not in sql

    def test_it_does_not_set_a_rate(self):
        """
        /setrate shows the rate in force, warns on a loss-making pair, and
        records who set it. A rate seeded here bypasses all three.
        """
        assert "INSERT INTO rates" not in _sql(Repo.onboard_supplier)

    def test_the_bridge_is_told_what_is_still_missing(self):
        src = _code(bridge_bot.addvendor_confirm)
        assert "/setrate" in src
        assert "/walletlink" in src

    def test_a_personal_chat_is_refused(self):
        """
        Access is by group membership. Registering a one-to-one chat hands
        the whole supplier role to one person instead of a group the Bridge
        administers.
        """
        src = _code(bridge_bot.addvendor_chat)
        assert "if chat_id >= 0:" in src


class TestOnboardingRefusesWhatWouldBreakLater:
    def test_an_unknown_client_is_refused(self):
        assert "role = 'client'" in _sql(Repo.onboard_supplier)

    def test_a_chat_already_registered_is_refused(self):
        assert "WHERE telegram_chat_id = $1" in _sql(Repo.onboard_supplier)

    def test_a_duplicate_label_is_refused(self):
        assert "lower(label) = lower($1)" in _sql(Repo.onboard_supplier)

    def test_a_duplicate_prefix_is_refused(self):
        """
        Nothing in the schema stops two vendors sharing a prefix, and SUPB1
        meaning two different trades cannot be untangled afterwards.
        """
        assert "upper(sc.prefix) = $1" in _sql(Repo.onboard_supplier)

    def test_a_registered_address_is_refused(self):
        """
        A deposit is attributed by the wallet it lands in. Two vendors on one
        address are indistinguishable.
        """
        assert "FROM wallets WHERE address = $1" in _sql(Repo.onboard_supplier)


class TestANewGroupAnnouncesItsChatId:
    """
    The chat id was the one part of onboarding with no route through the
    product at all. Now adding the bot produces it.
    """

    def test_the_announcement_sits_on_the_rejected_path(self):
        src = _code(auth.ChatRoleMiddleware.__call__)
        assert "_report_new_chat" in src
        after = src.split("_report_new_chat")[1]
        assert "return None" in after, "the chat must still be refused"

    def test_it_only_fires_when_this_bot_was_added(self):
        """
        Otherwise any unregistered group could make the bot chatter at the
        Bridge simply by sending it messages.
        """
        src = _code(auth.ChatRoleMiddleware._report_new_chat)
        assert "new_chat_members" in src
        assert "u.id == bot.id" in src

    def test_it_gives_the_id_and_points_at_the_command(self):
        src = _code(auth.ChatRoleMiddleware._report_new_chat)
        assert "chat.id" in src
        assert "/addvendor" in src

    def test_a_failure_cannot_affect_the_decision(self):
        """
        A convenience hanging off the security path must never be able to
        change what that path decides.
        """
        src = _code(auth.ChatRoleMiddleware._report_new_chat)
        assert "except Exception:" in src

    def test_nothing_is_said_to_the_unregistered_chat_itself(self):
        src = _code(auth.ChatRoleMiddleware._report_new_chat)
        assert "to_bridge" in src
        assert "message.answer" not in src
        assert "message.reply" not in src


# ======================================================================
# Vendor payment account change
# ======================================================================

class TestASupersededInstructionIsRetracted:
    def test_the_old_slots_are_read_before_they_are_replaced(self):
        src = _code(bridge_trade.confirm_final)
        read_at = src.index("superseded = await repo.trade_slots")
        wrote_at = src.index("await repo.issue_slots")
        assert read_at < wrote_at, "issue_slots deletes them"

    def test_only_a_trade_already_issued_retracts_anything(self):
        """A first issue has nothing to take back."""
        src = _code(bridge_trade.confirm_final)
        assert 'if trade["instructed_at"] is not None:' in src

    def test_the_retraction_reaches_the_client_first(self):
        """
        The other order leaves the newest thing on their screen being an
        instruction they are being told to ignore — and their side is
        automated, so it may act on it.
        """
        src = _code(bridge_trade.confirm_final)
        retraction_at = src.index("if superseded:")
        new_slots_at = src.index("for slot in slot_objs:")
        assert retraction_at < new_slots_at

    def test_it_names_the_accounts_that_are_no_longer_to_be_paid(self):
        src = _code(bridge_trade.confirm_final)
        branch = src.split("if superseded:")[1].split("for slot in slot_objs:")[0]
        assert "account_name" in branch
        assert "account_number" in branch

    def test_an_account_still_in_use_is_not_retracted(self):
        """
        A split re-issued across the same accounts must not tell the client
        to stop paying one it is about to ask them to pay again.
        """
        src = _code(bridge_trade.confirm_final)
        assert "if s[\"account_number\"] not in {o.account_number for o in slot_objs}" in src

    def test_it_says_earlier_payments_still_count(self):
        src = _code(bridge_trade.confirm_final)
        branch = src.split("if superseded:")[1].split("for slot in slot_objs:")[0]
        assert "already sent is still counted" in branch

    def test_the_vendor_is_not_named_to_the_client(self):
        """The 11 September disclosure rule holds here too."""
        src = _code(bridge_trade.confirm_final)
        branch = src.split("if superseded:")[1].split("for slot in slot_objs:")[0]
        assert "supplier_label" not in branch


# ======================================================================
# Cancellation control
# ======================================================================

class TestCancellingOffersToKeepTheRecordsLinked:
    def test_a_cancel_looks_for_stranded_money(self):
        src = _code(bridge_trade._finish_cancel)
        assert "await repo.stranded_deposits" in src

    def test_a_cancel_with_nothing_stranded_is_unchanged(self):
        src = _code(bridge_trade._finish_cancel)
        assert "if not stranded:" in src

    def test_the_offer_names_the_amount(self):
        src = _code(bridge_trade._finish_cancel)
        assert "fmt_usdt_plain(d['amount_usdt'])" in src

    def test_settling_by_hand_is_an_option(self):
        """
        He often does. A flow with no way out gets abandoned halfway, which
        is its own mess.
        """
        src = _code(bridge_trade._finish_cancel)
        assert "rd_no" in src
        assert "by hand" in src

    def test_every_route_into_a_cancel_gets_the_cleanup(self):
        """
        The invariant, now that there is more than one way in. When this
        lived inside the typed-reason handler, a second route added later
        would have gone without it silently — which is exactly how the
        16 September deposits were stranded.
        """
        import inspect

        callers = [
            name for name, fn in vars(bridge_trade).items()
            if callable(fn) and getattr(fn, "__module__", None) == bridge_trade.__name__
            and "cancel_trade" in inspect.getsource(fn)
        ]
        assert callers == ["_finish_cancel"], (
            f"{callers} call cancel_trade directly and skip the cleanup"
        )

    def test_declining_says_what_that_means(self):
        src = _code(bridge_trade.leave_stranded_deposit)
        assert "not invoiced" in src

    def test_the_new_trade_is_not_sent_automatically(self):
        """
        Opening a trade and instructing a client are separate decisions, and
        have been since 11 September.
        """
        # The rendering moved into _reopen on 22 September, when reopening
        # under a named vendor gained its own entry point. Both routes share
        # it, so this is still the one place the decision is made.
        src = _code(bridge_trade._reopen)
        assert "/issue" in src
        assert "issue_slots" not in src


class TestCancellingSaysWhatItWritesOff:
    """
    THE SECOND TIME (live, 19 September 2026)

    SUPB5 and SUPD2 were cancelled with the reasons "cleaing" and
    "clearing". Neither had been issued and neither had a rupee logged, so on
    screen they were empty rows worth tidying. The USDT for both had already
    reached the client on the 16th, so tidying them wrote off ₹995,495 of
    delivered settlement — the same money, lost the same way, three days
    apart.

    The only thing that had ever stood in the way was me saying so in
    WhatsApp. Twice. A message is not a mechanism.
    """

    def test_the_cancel_screen_checks_for_a_payout(self):
        src = _code(bridge_trade.cancel_pick)
        assert "await repo.prior_payouts_for_trade" in src

    def test_it_only_warns_when_nothing_has_been_invoiced(self):
        """
        A trade the client has already started paying is a normal cancel and
        does not need this. The dangerous one looks empty.
        """
        src = _code(bridge_trade.cancel_pick)
        assert 'if paid_out and trade["paid_inr"] == 0:' in src

    def test_it_names_the_sum_being_written_off(self):
        """
        "Money may have gone out" is not actionable. "You are writing off
        ₹249,995" is.
        """
        src = _code(bridge_trade.cancel_pick)
        assert 'fmt_inr(trade[\'inr_expected\'])' in src
        assert "writes off" in src

    def test_it_shows_when_the_usdt_left(self):
        src = _code(bridge_trade.cancel_pick)
        assert "detected_at" in src
        assert "amount_usdt" in src

    def test_it_does_not_block_the_cancel(self):
        """
        He settles by hand often, and that is a legitimate reason to cancel.
        A guard that refuses gets worked around; one that informs gets read.
        """
        src = _code(bridge_trade.cancel_pick)
        assert "settled it outside the bot, carry on" in src
        assert "return" not in src.split("if paid_out")[1]

    def test_it_arrives_before_he_is_asked_why(self):
        """After the fact is a receipt, not a warning."""
        src = _code(bridge_trade.cancel_pick)
        assert src.index("prior_payouts_for_trade") < src.index("Why?")


class TestTheSourceTradeIsCorrectedToo:
    def test_moving_a_deposit_restates_where_it_came_from(self):
        """
        The half that was missed. SUPA5 read 66,038 for days after its 37,736
        went to SUPA6, so ₹4,000,016 was counted on both trades and every
        export overstated the period by that much.
        """
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert "remaining" in sql
        assert "SET usdt_received = $2" in sql

    def test_both_halves_are_one_transaction(self):
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert "async with conn.transaction():" in sql
        assert sql.count("async with conn.transaction():") == 1

    def test_the_source_keeps_its_own_rates(self):
        """
        A restatement removes a phantom; it does not reprice. Using today's
        rate would quietly change what a cancelled trade claims to have been.
        """
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert 'usdt_to_inr(remaining, src["supply_rate"])' in sql

    def test_the_new_trade_uses_the_rate_in_force(self):
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert "FROM rates" in sql
        assert 'usdt_to_inr(usdt, r["supply_rate"])' in sql

    def test_the_reference_continues_the_sequence(self):
        """Reusing the cancelled trade's number makes two things one name."""
        assert "next_reference" in _sql(Repo.reopen_deposit_as_trade)

    def test_a_live_trades_deposit_is_never_moved(self):
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert 'if status != "cancelled":' in sql

    def test_a_counterparty_deposit_is_refused(self):
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert 'not w["is_internal"]' in sql

    def test_no_rate_means_no_trade(self):
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert "There is no rate for that pairing" in sql

    def test_both_changes_are_audited(self):
        sql = _sql(Repo.reopen_deposit_as_trade)
        assert 'action="trade.restated"' in sql
        assert 'action="trade.reopened_from_deposit"' in sql

    def test_the_new_trade_is_marked_announced(self):
        """
        The Bridge is doing this himself. Left null, the monitor posts a
        notice saying the supplier never entered details.
        """
        assert "announced_at" in _sql(Repo.reopen_deposit_as_trade)
