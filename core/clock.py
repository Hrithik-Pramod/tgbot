"""
Every time anybody reads is in the desk's own timezone.

WHAT WENT WRONG — 25 November 2026

The introducer's group showed this:

    25 Nov 17:36   35951.00 USDT   0% collected

for a deal that opened at 23:06. Five and a half hours out, with nothing on
screen to say so. Earlier the same day /earned reported "05:26" for a deal
instructed at 10:56.

    because the same amount was sent twice on seperate trades, the bot is
    not picking up one of them
    — Bridge

    @… please check this, it is missing a trade here
    — the introducer, in his own group

Nothing was missing. Two deposits of 35,951 arrived that day, both were
recorded, both earned 169.26, and the first had completed so /progress —
which lists live deals only — showed one of them. But the timestamps did
not line up with anybody's own records, and that is what turned "I can only
see one" into "the bot has lost one".

A figure nobody can reconcile against their own notes is not trusted, and a
report that is not trusted gets checked by hand, which is the thing this
whole system exists to stop.

WHY IT HAPPENED

asyncpg returns timestamptz as an aware datetime in UTC. Calling strftime
on it prints UTC. Ten call sites did, and each one looked right in isolation
— the bug is only visible to somebody holding a clock.

The audit queries all said `AT TIME ZONE 'Asia/Kolkata'` explicitly, which
is how this went unnoticed for so long: every time anyone investigated, they
converted, and every time the bot printed, it did not.

THE DAY BOUNDARY IS A SEPARATE QUESTION, AND STAYS GMT

Commission counts to midnight GMT, which he asked for deliberately:

    Yes for this one, as I will not be trading I will be sleeping, so end
    of day 100%

So a deal at 02:00 IST falls in the PREVIOUS GMT day. Displaying IST while
counting to a GMT boundary is a real inconsistency, and the answer is to say
so on the screen where it matters rather than to quietly pick one. See the
/earned header.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

# The desk's timezone. Everything the Bridge, a vendor or an introducer
# reads is rendered here. Configurable because the next counterparty may
# not be in India, and hard-coding it would mean a code change to find out.
DISPLAY_TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Asia/Kolkata"))


def local(when: datetime | None) -> datetime | None:
    """A database timestamp, moved into the timezone people read in."""
    if when is None:
        return None
    if when.tzinfo is None:
        # A naive datetime out of the database is UTC by construction —
        # every timestamp column is timestamptz. Saying so is better than
        # letting astimezone assume the server's local zone, which inside
        # the container is whatever TZ happens to be set to.
        when = when.replace(tzinfo=ZoneInfo("UTC"))
    return when.astimezone(DISPLAY_TZ)


def fmt_when(when: datetime | None, fmt: str = "%d %b %H:%M") -> str:
    """
    Format a database timestamp for a human, in their own time.

    Empty string for None, because every caller was already writing
    `x.strftime(...) if x else ""` and the ones that forgot would raise on
    a trade that has not completed yet.
    """
    moved = local(when)
    return moved.strftime(fmt) if moved else ""
