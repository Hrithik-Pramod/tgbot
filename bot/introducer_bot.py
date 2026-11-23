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

log = logging.getLogger(__name__)
router = Router()


def _vendor_line(t) -> str:
    """
    One deal, as an introducer may read it.

    Numbered rather than named: the deal reference belongs to the desk and
    the vendor, and nothing here needs it.
    """
    when = t["opened_at"].strftime("%d %b %H:%M") if t["opened_at"] else ""
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
    What is live right now on the vendor they introduced.
    """
    trades = await repo.introducer_live_trades(party["id"])
    if not trades:
        await message.answer(
            "Nothing is collecting at the moment.\n\n"
            "Use /earned for what you have been credited so far."
        )
        return

    lines = ["Live deals on your vendor.", ""]
    lines += [_vendor_line(t) for t in trades]
    total = sum(t["introducer_usdt"] for t in trades)
    lines += ["", f"Commission on these: {fmt_usdt_plain(total)} USDT"]
    await message.answer("\n".join(lines))


@router.message(Command("earned"))
async def cmd_earned(message: Message, party, repo) -> None:
    """
    The running total he asked for, and what is behind it.

        a running report of his commission (nice to have)
    """
    rows = await repo.introducer_commission(party["id"], days=30)
    if not rows:
        await message.answer("No commission in the last 30 days.")
        return

    lines = ["Your commission, last 30 days.", ""]
    for n, r in enumerate(rows, 1):
        when = r["opened_at"].strftime("%d %b")
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
        when = r["created_at"].strftime("%d %b %H:%M")
        lines.append(f"{when}   {r['utr']}   ₹{fmt_inr(r['amount_inr'])}")
    await message.answer("\n".join(lines))
