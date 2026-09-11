"""
The deposit notification waits until there is something to act on.

THE REQUEST (Bridge, 11 September 2026, 23:06)

    at the moment, the usdt getting sent first by supplier
    can we delay notification until the supplier enters the details of the
    trade?
    we considered enter first the send usdt
    but they are always sending usdt first then entering the details.
    and i think the system is not acting right because of it.

He is right about the cause. The notification fires when the chain shows the
transfer, which is before anyone has said where the INR goes. So he gets a
trade with no account on it, and the information he actually needs arrives
separately afterwards. Until earlier the same day it was worse: the gap was
filled with the supplier's previous choice, so a trade nobody had spoken for
arrived pre-filled with the wrong account.

WHAT WAS BUILT, AND THE ONE LINE THAT MATTERS

A delay, not a condition:

    supplier runs /send      announce immediately, account included
    N minutes pass           announce anyway, marked as not yet entered

Suppressing the message outright was the obvious reading of the request and
it is the one thing that must not happen. A supplier who sends USDT and then
goes quiet would leave money recorded in the ledger with nobody told — which
is precisely the failure that cost ₹902,460 earlier that day. The deposit is
still written the instant it lands; only the message waits.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import supplier_bot  # noqa: E402
from bot.notifier import Notifier  # noqa: E402
from db.repo import Repo  # noqa: E402
from monitor import tron  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "deploy" / "migrate-004-announced.sql").read_text(encoding="utf-8")


def _code(fn) -> str:
    """Source with docstring and comments removed — these tests assert on
    behaviour, and the comments quote the wording they replaced."""
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )


class TestTheDepositIsStillRecordedImmediately:
    """
    The money must land in the ledger whether or not anyone is told. This is
    the property the whole change hangs on.
    """

    def test_the_hold_happens_after_the_write(self):
        src = _code(Notifier.on_supplier_deposit)
        wrote = src.index("deposit.allocated")
        held = src.index("already_announced")
        assert wrote < held, (
            "the notification is being decided before the deposit is "
            "committed — a hold would then lose the deposit, not just the message"
        )

    def test_the_hold_is_only_a_return(self):
        """It must not roll anything back or mark the deposit differently."""
        src = _code(Notifier.on_supplier_deposit)
        branch = src.split("if not already_announced and nominated_name is None:")[1]
        head = branch.split("return")[0]
        assert "UPDATE" not in head and "DELETE" not in head


class TestWhenItHolds:
    def test_it_holds_when_nobody_has_nominated(self):
        src = _code(Notifier.on_supplier_deposit)
        assert "if not already_announced and nominated_name is None:" in src

    def test_it_sends_straight_away_when_the_account_is_known(self):
        """
        /send before the deposit is the order the flow was designed for and
        still works — there is nothing to wait for, so nothing waits.
        """
        src = _code(Notifier.on_supplier_deposit)
        assert "mark_trade_announced" in src, (
            "announcing on the ordinary path must stamp the trade, or the "
            "timeout sweep will announce it a second time"
        )

    def test_a_further_deposit_on_an_announced_trade_still_notifies(self):
        """
        The Bridge already knows that trade exists. A second tranche changes
        the total he is about to instruct, which is exactly what he needs
        telling about.
        """
        src = _code(Notifier.on_supplier_deposit)
        assert 'trade["announced_at"] is not None' in src


class TestTheSupplierReleasesIt:
    def test_send_announces_the_trade(self):
        src = _code(supplier_bot.send_account)
        assert "announce_trade" in src

    def test_it_only_does_so_once_a_trade_is_attached(self):
        src = _code(supplier_bot.send_account)
        assert "if attached:" in src
        released = src.split("if attached:")[1]
        assert "announce_trade" in released, \
            "releasing with no trade attached would announce nothing and hide the error"

    def test_it_guards_against_a_missing_trade(self):
        src = _code(supplier_bot.send_account)
        assert "if trade_id is not None:" in src


class TestTheWaitHasAFloor:
    def test_the_monitor_sweeps_for_held_trades(self):
        sweep = inspect.getsource(tron.DepositMonitor._release_held_announcements)
        assert "trades_awaiting_announcement" in sweep
        assert "waited=True" in sweep

    def test_the_sweep_actually_runs_each_cycle(self):
        """
        A sweep nobody calls is the monitor-alert bug from 10 September all
        over again: correct code, never reached, and the failure it guards
        against happens in silence.
        """
        cycle = inspect.getsource(tron.DepositMonitor)
        assert "await self._release_held_announcements()" in cycle

    def test_the_delay_is_short_enough_to_act_on(self):
        assert tron.ANNOUNCE_AFTER_MINUTES <= 15, (
            "a deposit sitting unreported for longer than this stops being a "
            "delay and starts being a hiding place"
        )

    def test_the_sweep_cannot_kill_the_poll_loop(self):
        """
        The poll loop is what notices money arriving. Nothing bolted onto it
        may be able to stop it.
        """
        src = inspect.getsource(tron.DepositMonitor._release_held_announcements)
        assert src.count("except Exception") >= 2

    def test_a_failed_announcement_is_retried(self):
        """
        The stamp is only set by a successful claim, so a trade that failed
        to send comes back round next cycle rather than being lost.
        """
        src = inspect.getsource(Repo.claim_trade_announcement)
        assert "announced_at = now()" in src
        assert "announced_at IS NULL" in src


class TestItIsAnnouncedExactlyOnce:
    def test_the_claim_reads_and_stamps_in_one_statement(self):
        """
        /send and the timeout sweep race. A supplier running /send at nine
        minutes fifty-nine is normal, not exotic.
        """
        sql = re.sub(r"\s+", " ", _code(Repo.claim_trade_announcement))
        assert "WITH claimed AS ( UPDATE trades SET announced_at = now()" in sql
        assert "WHERE id = $1 AND announced_at IS NULL" in sql

    def test_a_lost_race_returns_nothing(self):
        src = _code(Notifier.announce_trade)
        assert "if trade is None:" in src
        assert "return False" in src

    def test_the_claim_returns_everything_the_message_needs(self):
        """
        Going back for labels after claiming would mean a second round trip
        that can fail after the stamp is already set — announced, but never
        actually sent.
        """
        sql = re.sub(r"\s+", " ", _code(Repo.claim_trade_announcement))
        for field in ("supplier_label", "client_label", "nominated_name",
                      "wallet_address", "tx_hash"):
            assert field in sql


class TestTheBridgeCanTellTheTwoApart:
    def test_a_timed_out_announcement_says_the_details_are_missing(self):
        src = _code(Notifier.announce_trade)
        assert "waited" in src
        assert "has not entered the trade details yet" in src

    def test_a_released_announcement_carries_no_such_note(self):
        src = _code(Notifier.announce_trade)
        assert 'note = ""' in src

    def test_both_carry_the_confirm_button(self):
        src = _code(Notifier.announce_trade)
        assert "confirm_keyboard(trade_id)" in src

    def test_it_reports_the_trade_total_not_one_transfer(self):
        """
        Two transfers before the supplier speaks are one trade with one
        total. Two messages describing halves of it would be worse than the
        problem being fixed.
        """
        src = _code(Notifier.announce_trade)
        assert 'usdt_in=trade["usdt_received"]' in src
        assert 'inr_out=trade["inr_expected"]' in src


class TestTheMigration:
    def test_it_adds_the_column(self):
        assert "ADD COLUMN IF NOT EXISTS announced_at" in MIGRATION

    def test_it_marks_every_existing_trade_as_announced(self):
        """
        Without this the sweep sees every trade ever opened as unannounced
        and re-posts the lot on the first cycle — including the live one.
        """
        assert "UPDATE trades SET announced_at = COALESCE(announced_at, opened_at)" \
            in MIGRATION

    def test_it_is_rerunnable(self):
        assert MIGRATION.count("IF NOT EXISTS") >= 2

    def test_the_schema_matches(self):
        assert "announced_at    TIMESTAMPTZ" in SCHEMA
        assert "trades_unannounced_idx" in SCHEMA
