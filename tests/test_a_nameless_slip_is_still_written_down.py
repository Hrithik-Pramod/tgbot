"""
A slip that names nobody is still held, and still reported.

WHAT HAPPENED — 22 October 2026, 18:28:09

    PUNBR50000000000000000   ₹290,000   typed: null   (UTR masked)

A slip with a UTR and an amount and no beneficiary line at all. The account
matched nothing, so the hold was attempted with account_number NULL and
typed_beneficiary NULL, and the database refused it:

    CheckViolationError: new row for relation "held_payments" violates
    check constraint "held_payments_knows_something"

THE CRASH WAS NOT THE DAMAGE

By the time it raised, the client had already been sent "Which account did
these go to?" — so from their side it looked like an ordinary question. The
exception then unwound the rest of the handler, and the rest of the handler
is where the Bridge is told that money is in limbo.

So: nothing held, nothing recorded, nobody told. The ₹290,000 reached the
ledger at 18:28:13 only because the client pasted it again four seconds
later and the second attempt took a different path.

    It has done it again and missed picking up trading, which has caused a
    huge problem this morning
    — Bridge, 22 October 2026

held_payments exists (5 October) precisely so that a payment nobody has
answered for survives a restart and gets chased. A payment that cannot be
written to it is back to where that table started.

TWO FIXES, BECAUSE THEY FAIL DIFFERENTLY

  1. The caller now says what it knows. The slip named no beneficiary — that
     is a fact about the slip, and recording it reads better to the Bridge
     than a null ever could. The constraint is left alone: it is right.

  2. A hold that fails anyway no longer takes the batch and the notification
     with it. One payment's fault stays one payment's fault, and the Bridge
     is told in the loudest terms available.

THE CONSTRAINT IS CHECKED HERE FOR REAL

A stub repo would accept None without complaint and this test would pass on
a codebase that still crashes in production. So the test applies the
constraint itself.
"""

import inspect
import re
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402
from bot.client_bot import (  # noqa: E402
    NO_BENEFICIARY_ON_SLIP, _hold_or_shout, _typed_or_marker,
)


class _RefusingRepo:
    """
    held_payments, with its CHECK constraint, and nothing else.
    """

    def __init__(self):
        self.rows = []

    async def hold_payment(self, *, account_number=None, typed_beneficiary=None,
                           **kw):
        if account_number is None and typed_beneficiary is None:
            raise RuntimeError(
                'new row for relation "held_payments" violates check '
                'constraint "held_payments_knows_something"'
            )
        self.rows.append({"account_number": account_number,
                          "typed_beneficiary": typed_beneficiary, **kw})
        return True, len(self.rows)


class _Notifier:
    def __init__(self):
        self.bridge = []

    async def to_bridge(self, text, **kw):
        self.bridge.append(text)


class TestWhatTheCallerPasses:
    def test_a_nameless_slip_gets_an_explicit_marker(self):
        assert _typed_or_marker({"utr": "X", "amount": "1"}) == \
            NO_BENEFICIARY_ON_SLIP

    def test_so_does_an_empty_one(self):
        """
        The parser can hand back "" as readily as None, and "" fails the
        constraint just the same.
        """
        for empty in (None, "", "   ", "\n"):
            assert _typed_or_marker({"beneficiary": empty}) == \
                NO_BENEFICIARY_ON_SLIP, repr(empty)

    def test_a_real_name_is_passed_through_untouched(self):
        """
        The point of the column is to show the Bridge what the client wrote.
        Tidying it would defeat that — "SUPER TRAD" must stay "SUPER TRAD".
        """
        assert _typed_or_marker({"beneficiary": "SUPER TRAD"}) == "SUPER TRAD"
        assert _typed_or_marker({"beneficiary": "  royal trading  "}) == \
            "royal trading"

    def test_the_marker_could_never_be_a_real_beneficiary(self):
        """
        It is displayed to the Bridge beside real typed names, so it has to
        be unmistakably not one.
        """
        assert NO_BENEFICIARY_ON_SLIP.startswith("(")
        assert NO_BENEFICIARY_ON_SLIP.endswith(")")


class TestTheHoldNowSucceeds:
    @pytest.mark.asyncio
    async def test_the_row_goes_in(self):
        repo, notifier = _RefusingRepo(), _Notifier()
        await _hold_or_shout(
            repo, notifier, client_id=3, utr="PUNBR50000000000000000",
            amount_inr=D("290000"),
            typed_beneficiary=_typed_or_marker({"beneficiary": None}),
            why="x", chat_id=1, message_id=2,
        )
        assert len(repo.rows) == 1
        assert repo.rows[0]["typed_beneficiary"] == NO_BENEFICIARY_ON_SLIP
        assert notifier.bridge == [], "a successful hold is not an alarm"

    @pytest.mark.asyncio
    async def test_without_the_marker_the_database_would_still_refuse(self):
        """
        Guards the premise. If this ever stops raising, the constraint has
        been changed and the fix above needs rethinking, not just re-running.
        """
        repo, notifier = _RefusingRepo(), _Notifier()
        await _hold_or_shout(
            repo, notifier, client_id=3, utr="X", amount_inr=D("1"),
            typed_beneficiary=None, why="x", chat_id=1, message_id=2,
        )
        assert repo.rows == []
        assert notifier.bridge, "it failed silently"


class TestAFailedHoldIsLoudAndLocal:
    @pytest.mark.asyncio
    async def test_the_bridge_is_told_which_payment(self):
        repo, notifier = _RefusingRepo(), _Notifier()
        await _hold_or_shout(
            repo, notifier, client_id=3, utr="PUNBR50000000000000000",
            amount_inr=D("290000"), typed_beneficiary=None,
            why="x", chat_id=1, message_id=2,
        )
        assert len(notifier.bridge) == 1
        said = notifier.bridge[0]
        assert "PUNBR50000000000000000" in said
        assert "290,000" in said
        assert "NOT recorded" in said

    @pytest.mark.asyncio
    async def test_it_does_not_raise_and_take_the_batch_with_it(self):
        """
        The whole point. Three slips, the middle one unholdable: the other
        two must still be held and the handler must still reach the Bridge
        notification that comes after the loop.
        """
        repo, notifier = _RefusingRepo(), _Notifier()
        slips = [
            {"utr": "A", "beneficiary": "EKTA TRADERS"},
            {"utr": "B", "beneficiary": None},   # passed through raw, refused
            {"utr": "C", "beneficiary": "ROYAL TRADING"},
        ]
        for s in slips:
            await _hold_or_shout(
                repo, notifier, client_id=3, utr=s["utr"], amount_inr=D("1"),
                typed_beneficiary=s["beneficiary"], why="x",
                chat_id=1, message_id=2,
            )
        assert [r["utr"] for r in repo.rows] == ["A", "C"]
        assert len(notifier.bridge) == 1

    @pytest.mark.asyncio
    async def test_it_survives_having_no_notifier(self):
        """
        Several callers pass notifier=None. Raising here would restore the
        exact fault being fixed.
        """
        repo = _RefusingRepo()
        await _hold_or_shout(
            repo, None, client_id=3, utr="A", amount_inr=D("1"),
            typed_beneficiary=None, why="x", chat_id=1, message_id=2,
        )
        assert repo.rows == []


class TestEveryHoldInTheHandlerGoesThroughIt:
    def test_no_call_site_passes_a_raw_beneficiary(self):
        """
        Two call sites had the same bug. A third would too.
        """
        src = (ROOT / "bot" / "client_bot.py").read_text(encoding="utf-8")
        assert 'typed_beneficiary=s.get("beneficiary")' not in src, (
            "a hold is passing the parsed beneficiary straight through; it "
            "is None on exactly the path where account_number is None too"
        )

    def test_the_unmatched_paths_hold_through_the_guard(self):
        src = (ROOT / "bot" / "client_bot.py").read_text(encoding="utf-8")
        # Every hold that can carry a NULL account — the ones keyed on a
        # typed name rather than an account number.
        for call in re.findall(r"(await [_a-z]*hold[_a-z]*\([^)]*typed_beneficiary[^)]*\))",
                               src, re.S):
            assert "_hold_or_shout" in call, call[:120]
            assert "_typed_or_marker" in call, call[:120]

    def test_the_guard_catches_broadly(self):
        """
        asyncpg raises CheckViolationError, the pool can raise its own
        errors, and the next fault here will be a different class again.
        Catching one exception type is how this bug reaches production twice.
        """
        src = inspect.getsource(_hold_or_shout)
        assert "except Exception" in src

    def test_it_logs_the_traceback(self):
        """
        The alert names the payment; the log has to say why it failed, or
        the next diagnosis starts from nothing again.
        """
        assert "log.exception" in inspect.getsource(_hold_or_shout)
