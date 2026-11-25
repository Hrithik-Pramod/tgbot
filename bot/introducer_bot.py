"""
The introducer's view: their vendor's deposits, and what they have earned.

    the Introducer needs visability, so a separate group they can use a bot
    to see trade deposits, and check progress, and a running report of his
    commission

    There they can run as the usual supplier bot, but also are told on each
    trade that is sent, the commission they will receive on the value the
    supplier sent
    — Bridge, 23 November 2026

WHAT THEY MAY SEE

Their vendor's deposits, the collection progress against each, the UTRs, and
their own commission. He was asked directly whether they should see the
supply rate and answered:

    They can see utr progress as well. But the rest no

So: no sell rate, no margin, no client name, no other vendor. The commission
figure is theirs and the deposits are their vendor's.

ONE THING HE SHOULD KNOW, AND DOES

Showing the USDT deposit AND the INR collection lets anyone divide one by
the other and recover the supply rate. He was told that before this was
built and chose to show both — the introducer brought the vendor in and
almost certainly knows the rate already. It is a decision, not an oversight.

WHAT THEY MAY NOT DO

Anything. There is no command here that changes a figure. An introducer is
a reader of one vendor's book and nothing else, so the whole module is
queries.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from core.money import fmt_inr, fmt_usdt_plain, pct_collected
from core.clock import fmt_when

log = logging.getLogger(__name__)
router = Router()


def _vendor_line(t) -> str:
    """
    One deal, as an introducer may read it.

    Numbered rather than named: the deal reference belongs to the desk and
    the vendor, and nothing here needs it.
    """
    when = fmt_when(t["opened_at"])
    usdt = fmt_usdt_plain(t["usdt_received"])
    if not t["inr_expected"]:
        return f"{when}   {usdt} USDT   awaiting payment instructions"
    return (
        f"{when}   {usdt} USDT   "
        f"{pct_collected(t['paid_inr'], t['inr_expected'])} collected   "
        f"commission {fmt_usdt_plain(t['introducer_usdt'])} USDT"
    )


@router.message(Command("progress"))
async def cmd_progress(message: Message, party, repo) -> None:
    """
    What is live right now, and what has already finished today.

    IT USED TO SHOW ONLY THE LIVE ONES, AND THAT READ AS A MISSING TRADE

    Twice inside 24 hours, on 25 and 26 November 2026:

        please check this, it is missing a trade here — the introducer
        It's now only showing 1 not 3 — Bridge

    Both times nothing was missing. A deal that finishes collecting leaves
    the live list, which is correct and is also indistinguishable, on a
    phone, from a deal the bot never recorded. The first time it cost an
    evening of checking; the fix for that one was the timezone, and this
    screen then did the same thing again the next day.

    So the finished ones stay on the screen until the commission day rolls
    over, and the screen carries the day's total — the same figure /earned
    reports, from the same query, so the two can never disagree.
    """
    live = await repo.introducer_live_trades(party["id"])
    today = await repo.introducer_commission_today(party["id"])

    # Finished = today's commission rows that are not still collecting.
    # Taken from the /earned query rather than a third one of its own: a
    # second definition of "today" is a second number to reconcile, which
    # is the whole problem this screen is fixing.
    live_ids = {t["id"] for t in live}
    done = [r for r in today if r["id"] not in live_ids]

    if not live and not done:
        await message.answer(
            "Nothing is collecting at the moment, and nothing has "
            "completed today.\n\n"
            "Use /earned for today's commission."
        )
        return

    lines = []
    if live:
        lines += ["Live deals on your vendor.", ""]
        lines += [_vendor_line(t) for t in live]
    else:
        lines += ["Nothing is collecting at the moment.", ""]

    if done:
        lines += ["", "Completed today.", ""]
        for r in done:
            lines.append(
                f"{fmt_when(r['opened_at'])}   "
                f"{fmt_usdt_plain(r['usdt_received'])} USDT   collected   "
                f"commission {fmt_usdt_plain(r['introducer_usdt'])} USDT"
            )

    total = sum(r["introducer_usdt"] for r in today)
    lines += [
        "",
        f"Commission today: {fmt_usdt_plain(total)} USDT "
        f"(counted to midnight GMT)",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("earned"))
async def cmd_earned(message: Message, party, repo) -> None:
    """
    Today's commission, and what is behind it.

        a running report of his commission (nice to have)

        ANd - No commission in the last 30 days. - this should be today not
        30 days
        - Bridge, 23 November 2026

    Today runs from midnight GMT, the same boundary the settlement uses. A
    rolling window would show the introducer a figure that never matches
    the one being settled against.
    """
    rows = await repo.introducer_commission_today(party["id"])
    if not rows:
        await message.answer("No commission today.")
        return

    # The window is stated because the two do not line up: times are shown
    # in local time, the day is counted to midnight GMT (his choice — "I
    # will be sleeping, so end of day 100%"). A deal just after 05:30 local
    # therefore starts a new commission day. Saying so costs one line and
    # saves the argument about whose figure is right.
    lines = ["Your commission today (counted to midnight GMT).", ""]
    for n, r in enumerate(rows, 1):
        when = fmt_when(r["instructed_at"], "%H:%M")
        lines.append(
            f"{n}.  {when}   {fmt_usdt_plain(r['usdt_received'])} USDT "
            f"→ {fmt_usdt_plain(r['introducer_usdt'])} USDT"
        )
    total = sum(r["introducer_usdt"] for r in rows)
    lines += ["", f"Total   {fmt_usdt_plain(total)} USDT"]
    await message.answer("\n".join(lines))


@router.message(Command("utrs"))
async def cmd_utrs(message: Message, party, repo) -> None:
    """
    The payments against the deal currently collecting.

        They can see utr progress as well
    """
    rows = await repo.introducer_recent_payments(party["id"], limit=30)
    if not rows:
        await message.answer("No payments recorded yet.")
        return

    lines = ["Payments received on your vendor's deals.", ""]
    for r in rows:
        when = fmt_when(r["created_at"])
        lines.append(f"{when}   {r['utr']}   ₹{fmt_inr(r['amount_inr'])}")
    await message.answer("\n".join(lines))
