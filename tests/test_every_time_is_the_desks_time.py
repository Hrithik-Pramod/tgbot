"""
Every timestamp anybody reads is in the desk's own timezone.

WHAT IT COST — 25 November 2026

The introducer's group showed:

    25 Nov 17:36   35951.00 USDT   0% collected

for a deal opened at 23:06 IST. Earlier that day /earned reported "05:26"
for one instructed at 10:56. Five and a half hours out, unlabelled.

    because the same amount was sent twice on seperate trades, the bot is
    not picking up one of them — Bridge

    please check this, it is missing a trade here — the introducer

Nothing was missing. Two 35,951 USDT deposits arrived, both were recorded
(RAJO4 and RAJO6), both earned 169.26, and the first had completed so
/progress — live deals only — listed one of them. But none of the times
matched anybody's own records, and that is what turned "I can only see one
of these" into "the bot has lost one".

A figure nobody can reconcile against their own notes gets checked by hand,
which is the thing this system exists to stop.

WHY IT SURVIVED SO LONG

asyncpg returns timestamptz as an aware datetime in UTC, and strftime on it
prints UTC. Ten call sites did exactly that and each looked right on its
own. Every diagnostic query written during an investigation said
`AT TIME ZONE 'Asia/Kolkata'` explicitly — so every time a person looked,
they converted, and every time the bot printed, it did not.

THE DAY BOUNDARY IS A DIFFERENT QUESTION AND STAYS GMT

Commission counts to midnight GMT because he asked for it:

    Yes for this one, as I will not be trading I will be sleeping, so end
    of day 100%

Local times against a GMT day boundary is a genuine inconsistency, so
/earned says which boundary it used rather than leaving it to be discovered.
"""

import inspect
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.clock import DISPLAY_TZ, fmt_when, local  # noqa: E402

# 25 November 2026, 17:36 UTC — the exact timestamp from the report. In the
# desk's timezone it is 23:06, which is when the deal actually opened.
THE_DEAL = datetime(2026, 11, 25, 17, 36, tzinfo=timezone.utc)


class TestTheConversion:
    def test_the_reported_timestamp_reads_correctly_now(self):
        assert fmt_when(THE_DEAL) == "25 Nov 23:06"

    def test_it_moves_into_the_display_timezone(self):
        assert local(THE_DEAL).tzinfo is DISPLAY_TZ

    def test_a_naive_timestamp_is_treated_as_utc(self):
        """
        Not as the container's local time. Inside the container TZ is
        whatever the compose file sets, so astimezone on a naive value
        would convert from a timezone that has nothing to do with the data.
        """
        naive = datetime(2026, 11, 25, 17, 36)
        assert fmt_when(naive) == fmt_when(THE_DEAL)

    def test_a_missing_timestamp_is_blank_not_an_error(self):
        """
        A trade that has not completed has no completed_at, and every
        caller used to write `x.strftime(...) if x else ""`.
        """
        assert fmt_when(None) == ""
        assert local(None) is None

    def test_the_format_is_the_caller_s_business(self):
        assert fmt_when(THE_DEAL, "%H:%M") == "23:06"
        assert fmt_when(THE_DEAL, "%d %b") == "25 Nov"
        assert fmt_when(THE_DEAL, "%Y-%m-%d %H:%M") == "2026-11-25 23:06"


class TestNobodyFormatsATimestampThemselves:
    """
    The fix is only a fix if every call site uses it. Ten did not.
    """

    FILES = ["bot/introducer_bot.py", "bot/supplier_bot.py",
             "bot/bridge_bot.py", "bot/bridge_trade.py", "bot/notifier.py"]

    @pytest.mark.parametrize("path", FILES)
    def test_no_strftime_on_a_database_value(self, path):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert ".strftime(" not in src, (
            f"{path} formats a timestamp directly; it will print UTC"
        )

    @pytest.mark.parametrize("path", FILES)
    def test_no_datetime_interpolated_into_an_f_string(self, path):
        """
        The other way it happened: f"{row['asked_at']:%d %b %H:%M}". It
        does not contain the word strftime and does exactly the same thing.
        """
        src = (ROOT / path).read_text(encoding="utf-8")
        assert ":%d " not in src and ":%H" not in src and ":%Y" not in src, (
            f"{path} interpolates a timestamp with a format spec"
        )

    def test_the_chase_message_was_one_of_them(self):
        """
        "Asked 26 Oct 12:50" for a payment held at 18:20 IST — flagged
        weeks before the introducer saw the same fault and not fixed then.
        """
        from bot import notifier
        src = inspect.getsource(notifier.Notifier.chase_held_payments)
        assert "fmt_when(h['asked_at'])" in src


class TestTheDayBoundaryIsStated:
    @pytest.mark.asyncio
    async def test_earned_says_which_midnight(self):
        """
        Times are local, the day is GMT. Both are deliberate and together
        they are confusing, so the screen says so.

        Asserted on what is SENT, not on the source. The first version
        scanned cmd_earned for "midnight GMT" — which its own docstring
        contains, so deleting the phrase from the header still passed. A
        mutation proved it. Fourth time prose has satisfied a source scan
        in this project; see tests/sqltext.py.
        """
        from bot import introducer_bot

        sent = []

        class _Msg:
            async def answer(self, t, **kw): sent.append(t)

        class _Repo:
            async def introducer_commission_today(self, pid):
                return [{"id": 1, "opened_at": THE_DEAL,
                         "instructed_at": THE_DEAL,
                         "usdt_received": D("35951"),
                         "introducer_usdt": D("169.26")}]

        await introducer_bot.cmd_earned(_Msg(), {"id": 13}, _Repo())
        assert "midnight GMT" in sent[0], (
            "the header does not say which day boundary it counted to"
        )
        # And the time in it is the desk's, not UTC.
        assert "23:06" in sent[0]

    def test_the_query_still_counts_to_gmt(self):
        from db.repo import Repo
        sql = inspect.getsource(Repo.introducer_commission_today)
        assert "AT TIME ZONE 'UTC'" in sql


class TestItIsConfigurable:
    def test_the_timezone_comes_from_the_environment(self):
        """
        The next counterparty may not be in India, and finding that out
        should not need a code change.
        """
        src = inspect.getsource(sys.modules["core.clock"])
        assert 'os.environ.get("DISPLAY_TZ"' in src
        assert "Asia/Kolkata" in src
