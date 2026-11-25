"""
A deal that finishes collecting stays on the introducer's screen.

WHAT IT COST — 25 AND 26 NOVEMBER 2026

Twice inside 24 hours, the same screen produced the same false alarm.

    please check this, it is missing a trade here
    — the introducer, 25 Nov 23:12

    Check out / Time setting / It's now only showing 1 not 3 / Trades
    — Bridge, 26 Nov 00:27

Nothing was missing either time. The ledger for the GMT day held three
deals on that vendor — 169.26, 155.90 and 169.26, totalling 494.42 USDT.
Two had finished collecting and /progress listed live deals only, so the
screen showed one.

The first report was answered by fixing the timezone, which was a real
fault and the wrong diagnosis of THIS one. The screen then did it again
the next day. A deal completing and a deal never being recorded looked
identical on a phone, and the reader cannot tell them apart from the
outside — which is exactly the position the system exists to keep people
out of.

WHY THE TOTAL COMES FROM THE /earned QUERY

The finished section could have had a query of its own. It does not,
because a second definition of "today" is a second number to reconcile,
and two screens disagreeing about the day's commission would be a worse
version of the bug being fixed. /progress and /earned read the same rows.

THE DAY BOUNDARY IS STILL GMT

    Yes for this one, as I will not be trading I will be sleeping, so end
    of day 100% — Bridge

So a deal sent before midnight GMT and still collecting after it appears
as live, with its commission counted to the day it was sent. The screen
says which boundary it used rather than leaving that to be discovered;
see tests/test_every_time_is_the_desks_time.py.
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import introducer_bot  # noqa: E402


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# The three real deals, as the database held them. Times are UTC, as
# asyncpg returns them; 17:36 UTC is 23:06 at the desk.
LIVE = [{"id": 252, "opened_at": _utc(2026, 11, 25, 17, 36),
         "usdt_received": D("35951.00"), "inr_expected": D("3800021"),
         "paid_inr": D("251391"), "introducer_usdt": D("169.26")}]

TODAY = [
    {"id": 252, "opened_at": _utc(2026, 11, 25, 17, 36),
     "instructed_at": _utc(2026, 11, 25, 17, 40),
     "usdt_received": D("35951.00"), "introducer_usdt": D("169.26")},
    {"id": 250, "opened_at": _utc(2026, 11, 25, 10, 26),
     "instructed_at": _utc(2026, 11, 25, 10, 30),
     "usdt_received": D("33112.50"), "introducer_usdt": D("155.90")},
    {"id": 247, "opened_at": _utc(2026, 11, 25, 5, 8),
     "instructed_at": _utc(2026, 11, 25, 5, 10),
     "usdt_received": D("35951.00"), "introducer_usdt": D("169.26")},
]


class _Msg:
    def __init__(self): self.sent = []
    async def answer(self, text, **kw): self.sent.append(text)


class _Repo:
    def __init__(self, live=LIVE, today=TODAY):
        self._live, self._today = live, today
    async def introducer_live_trades(self, pid): return self._live
    async def introducer_commission_today(self, pid): return self._today


async def _progress(repo) -> str:
    msg = _Msg()
    await introducer_bot.cmd_progress(msg, {"id": 13}, repo)
    return msg.sent[0]


@pytest.mark.asyncio
class TestAllThreeAreOnTheScreen:
    async def test_the_two_finished_ones_are_shown(self):
        """
        "It's now only showing 1 not 3". It now shows 3.
        """
        out = await _progress(_Repo())
        assert "33112.50" in out
        assert out.count("35951.00") == 2

    async def test_each_carries_its_commission(self):
        out = await _progress(_Repo())
        assert "155.90" in out
        assert out.count("169.26") == 2

    async def test_the_finished_ones_are_marked_finished(self):
        """
        Shown, but not shown as still collecting — that would be a
        different lie in the other direction.
        """
        out = await _progress(_Repo())
        assert "Completed today" in out

    async def test_the_live_one_still_reads_as_live(self):
        out = await _progress(_Repo())
        assert "Live deals" in out
        assert "6% collected" in out

    async def test_the_time_is_the_desks(self):
        """
        On the finished rows too, not only the live one. A mutation
        reverting just the completed line to strftime was caught by the
        source scan in test_every_time_is_the_desks_time.py and by
        nothing that read the output.

        10:26 UTC is 15:56 at the desk, which is what the ledger says
        that deal opened at.
        """
        out = await _progress(_Repo())
        assert "25 Nov 23:06" in out          # the live one
        assert "25 Nov 15:56" in out          # a completed one
        assert "25 Nov 10:26" not in out      # the UTC reading of it


@pytest.mark.asyncio
class TestTheTotalIsTheSameFigureAsEarned:
    async def test_it_is_the_days_commission(self):
        out = await _progress(_Repo())
        assert "494.42" in out

    async def test_it_says_which_midnight(self):
        out = await _progress(_Repo())
        assert "midnight GMT" in out

    async def test_a_finished_deal_is_not_counted_twice(self):
        """
        The live row and the commission row are the same deal (252). If
        the finished list were "everything today" rather than "everything
        today that is not still live", it would appear in both sections
        and the day would read 663.68.
        """
        out = await _progress(_Repo())
        assert "663.68" not in out
        assert out.count("169.26") == 2

    async def test_the_total_is_not_summed_from_the_live_list(self):
        """
        What it used to do: "Commission on these" over the live deals
        only. That is the figure that read as 169.26 when the day was
        494.42.
        """
        out = await _progress(_Repo())
        assert "Commission on these" not in out


@pytest.mark.asyncio
class TestTheQuietCases:
    async def test_nothing_live_but_something_finished(self):
        """
        The case that caused the report: the vendor has traded today and
        every deal has completed. The old screen said "Nothing is
        collecting" and stopped, which is true and useless.
        """
        out = await _progress(_Repo(live=[], today=TODAY))
        assert "Completed today" in out
        assert "494.42" in out
        assert "33112.50" in out

    async def test_a_genuinely_empty_day_says_so(self):
        out = await _progress(_Repo(live=[], today=[]))
        assert "Nothing is collecting" in out
        assert "nothing has completed today" in out

    async def test_live_only_still_works(self):
        """
        Backward compatibility: a vendor whose only deal of the day is
        still collecting reads as it always did, plus the day's total.
        """
        out = await _progress(_Repo(live=LIVE, today=[TODAY[0]]))
        assert "Live deals" in out
        assert "Completed today" not in out
        assert "169.26" in out
