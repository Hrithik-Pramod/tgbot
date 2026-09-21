"""
Progress as a percentage, which never flatters itself.

THE REQUEST (Bridge, 21 September 2026)

    on MYFX, the progress can you add % to my look up as well

WHY THIS IS NOT A ONE-LINER

A trade collected to ₹3,999,999 of ₹4,000,016 is 99.9996% done, and every
natural way of writing that prints "100%". A hundred per cent means
finished, and ₹17 is still outstanding.

This system has spent a fortnight learning what a figure that looks
finished and is not costs:

    11 September   ₹902,460 in payments the bot declined to record, silently
    16 September   ₹995,495 delivered and never invoiced
    19 September   the same ₹995,495 again, cancelled as housekeeping

Every one of them was money that looked settled from the screen. A rounded
percentage is the same mistake in miniature, so it rounds DOWN, and 100%
appears only when nothing at all is outstanding.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_bot  # noqa: E402
from core.money import pct_collected  # noqa: E402


class TestOneHundredMeansFinished:
    def test_a_whisker_short_is_never_a_hundred(self):
        """₹17 outstanding on a ₹4m trade. Not finished."""
        assert pct_collected(D("3999999"), D("4000016")) == "99%"

    def test_one_rupee_short_is_never_a_hundred(self):
        assert pct_collected(D("4000015"), D("4000016")) == "99%"

    def test_exactly_paid_is_a_hundred(self):
        assert pct_collected(D("4000016"), D("4000016")) == "100%"

    def test_an_overpayment_shows_above_a_hundred(self):
        """
        Overpaid is its own problem and must not be disguised as complete.
        """
        assert pct_collected(D("4100000"), D("4000016")) == "102%"


class TestItRoundsDown:
    def test_a_part_collection_is_not_flattered(self):
        # 3,120,000 / 4,000,016 = 77.9997%
        assert pct_collected(D("3120000"), D("4000016")) == "77%"

    def test_nothing_collected_is_zero(self):
        assert pct_collected(D("0"), D("4000016")) == "0%"

    def test_a_real_part_payment(self):
        # 2,472,010 / 4,000,016 = 61.8%
        assert pct_collected(D("2472010"), D("4000016")) == "61%"


class TestNothingToMeasureAgainst:
    def test_a_trade_with_no_total_yet_shows_a_dash(self):
        """
        A deposit is in but the instruction has not gone out, so there is no
        figure to be a percentage of. "0%" would read as nobody paying.
        """
        assert pct_collected(D("0"), D("0")) == "—"

    def test_a_negative_expectation_is_also_a_dash(self):
        assert pct_collected(D("100"), D("-1")) == "—"


class TestItAppearsOnProgress:
    def test_each_pairing_carries_its_own_percentage(self):
        src = inspect.getsource(bridge_bot.cmd_progress)
        assert "pct_collected(r['collected_inr'], r['expected_inr'])" in src

    def test_the_book_carries_one_too(self):
        """
        He asked for it on the look-up, and the look-up ends with a total.
        """
        src = inspect.getsource(bridge_bot.cmd_progress)
        assert "pct_collected(collected, expected)" in src

    def test_the_book_total_only_counts_live_trades(self):
        """
        A cancelled trade's expectation is not something anyone is still
        collecting, and counting it would drag the figure down for ever.
        """
        src = inspect.getsource(bridge_bot.cmd_progress)
        assert 'for r in rows if r["open_trades"]' in src

    def test_no_percentage_when_nothing_is_open(self):
        """Dividing by an empty book would read 0% collected on a clear one."""
        src = inspect.getsource(bridge_bot.cmd_progress)
        assert "if expected > 0 else" in src

    def test_the_money_lines_are_unchanged(self):
        """The percentage is added beside the figures, not instead of them."""
        src = inspect.getsource(bridge_bot.cmd_progress)
        assert "fmt_inr(r['collected_inr'])" in src
        assert "fmt_inr(r['expected_inr'])" in src
        assert "Outstanding" in src
