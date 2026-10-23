"""
A payment nobody has answered for is chased until somebody decides.

WHAT WAS FOUND — 23 October 2026

    id | asked               | amount_inr | typed_beneficiary
    38 | 2026-10-15 17:22:11 |  243000.00 | INDURKAR TEXTILES PRIVATE LIMITED

Eight days. The chaser had written:

    chased_at keeps the reminder to once a cycle. A notice repeated every
    sweep is one nobody reads, and this one has to be read.

and then implemented:

    AND chased_at IS NULL

which is not once a cycle. It is once, permanently. The first sweep after
the payment was held sent a notice, set chased_at, and removed the row from
every sweep that followed for the rest of its life.

WHY THE AMOUNT DID NOT MATTER

That ₹243,000 turned out not to be ours — every order around it was square
to the rupee and the beneficiary had never been registered. That was luck,
not design. A held payment that IS ours disappears from view on exactly the
same schedule, and that is the ₹902,460 of 11 September: not money nobody
was told about, money everybody forgot.

WHY REPEATING IS SAFE HERE

The usual objection to a repeating alert is that it trains people to ignore
it. That holds when there is nothing to be done. Here there are three things,
and all of them stop it: place the payment, answer which account it was, or
tap "Not ours — ignore it". Every one sets resolved_at. A reminder with an
off switch that is also the fix is not noise.

Six hours, not six minutes: long enough to outlast a busy afternoon without
nagging, short enough that nothing survives a day unseen.
"""

import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.notifier import Notifier  # noqa: E402
from db.repo import Repo  # noqa: E402


def _sql(method) -> str:
    """The query with its -- commentary removed; see the collect test."""
    src = inspect.getsource(method)
    i = src.index('"""', src.index("conn.fetch"))
    j = src.index('"""', i + 3)
    return "\n".join(
        re.sub(r"--.*$", "", line) for line in src[i + 3:j].splitlines()
    )


WAITING = _sql(Repo.waiting_held_payments)


class TestItDoesNotGiveUpAfterOne:
    def test_a_chased_payment_can_come_round_again(self):
        """
        The bug, in one assertion. `AND chased_at IS NULL` alone means a
        payment is chased once and then never again.
        """
        assert "chased_at IS NULL" in WAITING, (
            "something must still pick up a payment that has never been "
            "chased at all"
        )
        assert "OR chased_at <" in WAITING, (
            "a payment chased once is dropped from every later sweep — this "
            "is the eight days row 38 spent invisible"
        )

    def test_the_repeat_is_a_parameter_not_a_number_in_the_sql(self):
        """
        So the interval can be argued about without editing a query, and so
        a test can set it to something it can actually wait for.
        """
        assert "$2" in WAITING
        assert "repeat_after_minutes" in inspect.signature(
            Repo.waiting_held_payments).parameters

    def test_it_is_hours_not_minutes(self):
        """
        A held payment that reappears every few minutes is the notice
        nobody reads, which is the fault the original comment was guarding
        against. Both failures are real; the interval is what separates
        them.
        """
        default = inspect.signature(
            Repo.waiting_held_payments).parameters["repeat_after_minutes"].default
        assert default >= 60, "this will become noise"
        assert default <= 24 * 60, "slower than a day is the old bug again"

    def test_a_resolved_payment_is_never_chased(self):
        """
        The off switch. Placing it, answering it and dismissing it all set
        resolved_at, and all three must end the reminder.
        """
        assert "resolved_at IS NULL" in WAITING

    def test_it_still_waits_before_the_first_chase(self):
        """
        The client is usually answering. Chasing instantly would make the
        Bridge the first responder to every slip.
        """
        assert "asked_at <" in WAITING and "$1" in WAITING

    def test_the_oldest_is_chased_first(self):
        assert "ORDER BY asked_at" in WAITING


class TestTheThreeWaysToStopIt:
    """
    Each of these must set resolved_at, or the reminder becomes unstoppable
    and the objection to repeating it becomes correct.
    """

    def test_placing_it_resolves_it(self):
        assert "resolved_at" in inspect.getsource(Repo.resolve_held_payment)

    def test_dismissing_it_resolves_it(self):
        src = inspect.getsource(Repo.dismiss_held_payment)
        assert "resolved_at" in src
        assert "resolved_trade_id = NULL" in src or "NULL" in src

    def test_recording_it_by_any_route_resolves_it(self):
        assert "resolved_at" in inspect.getsource(Repo.release_held_payments)


class TestTheChaserSaysWhatItWants:
    def test_the_notice_names_the_money(self):
        src = inspect.getsource(Notifier.chase_held_payments)
        assert "STILL WAITING" in src
        assert "h['amount_inr']" in src or 'h["amount_inr"]' in src
        assert "h['utr']" in src or 'h["utr"]' in src

    def test_it_offers_a_way_out(self):
        """
        A repeating reminder with no button is a repeating reminder.
        """
        src = inspect.getsource(Notifier.chase_held_payments)
        assert "InlineKeyboardButton" in src

    def test_the_docstring_no_longer_promises_once_each(self):
        """
        The comment said one thing and the code did another, and the comment
        is why nobody checked. It is part of the fix.
        """
        doc = Notifier.chase_held_payments.__doc__ or ""
        assert "Once each" not in doc
        assert "not once ever" in doc


class TestTheQueryIsValid:
    def test_it_parses(self):
        import pglast
        pglast.parse_sql(WAITING.replace("$1", "'15'").replace("$2", "'360'"))

    @pytest.mark.parametrize("param", ["$1", "$2"])
    def test_both_intervals_are_cast_the_same_safe_way(self, param):
        """
        ($n::text || ' minutes')::interval — the form already used here.
        Interpolating a bare number would be a different kind of bug.
        """
        assert f"({param}::text || ' minutes')::interval" in WAITING
