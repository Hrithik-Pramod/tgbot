"""
Client bot: /accounts, /add, /done.

This is the surface that produces the summary the client showed us, so the
output here is held to their exact format.

/add captures three things, not two. The original brief asked for amount and
UTR; the real summary the client supplied shows each tranche carrying its
destination account ("to Alpha Traders", "to Alpha Traders Pvt Ltd"), with one
trade's INR split across several of the supplier's accounts. So the beneficiary
is part of every payment.
"""

from __future__ import annotations

import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReactionTypeEmoji,
)

from core.money import (
    MoneyError, fmt_inr, fmt_inr_plain, normalise_utr, round_inr, to_decimal,
)
from core.parse import match_account, parse_payments
from core.summary import (
    Payment, render_completion_notice, render_supplier_summary,
    render_trade_summary,
)

log = logging.getLogger(__name__)
router = Router()

# The client asked for the confirmation tap to be removed once the agreed
# labelled format was in place (8 Sep 2026). Kept as a switch rather than
# deleted: turning it back on is an env change, not a rebuild, and this is the
# kind of decision that gets revisited after the first surprise.
REQUIRE_PASTE_CONFIRMATION = (
    os.environ.get("REQUIRE_PASTE_CONFIRMATION", "false").strip().lower()
    in ("1", "true", "yes")
)


class AddPayment(StatesGroup):
    amount = State()
    utr = State()
    account = State()


class PastedPayment(StatesGroup):
    account = State()
    confirm = State()


# --------------------------------------------------------------- /accounts

def _render_accounts(supplier_label: str, accounts) -> list[str]:
    """D4: the supplier is shown by label only, never by real identity."""
    lines = [f"Accounts under {supplier_label}:", ""]
    for a in accounts:
        lines.append(a["account_name"])
        lines.append(f"Acc num - {a['account_number']}")
        lines.append(f"Ifsc - {a['ifsc']}")
        lines.append("")
    return lines


@router.message(Command("accounts"))
async def cmd_accounts(message: Message, party, repo) -> None:
    """
    Show the supplier accounts this client can pay into.

    With a trade open this is the one supplier that trade belongs to. Without
    one it lists every supplier the client is paired with, rather than refusing.
    Answering "you have no open trade" to someone asking where they pay reads
    as a broken bot, and it is not what they asked (reported 10 Sep 2026).
    """
    # Grouped by supplier, because a client can be running a trade with more
    # than one at the same time and needs to see whose account is whose.
    open_accounts = await repo.open_trade_accounts_for_client(party["id"])
    if open_accounts:
        by_supplier: dict[str, list] = {}
        for a in open_accounts:
            by_supplier.setdefault(a["supplier_label"], []).append(a)
        await message.answer("\n\n".join(
            "\n".join(_render_accounts(label, accs)).rstrip()
            for label, accs in by_supplier.items()
        ))
        return

    open_trades = await repo.open_trades_for_client(party["id"])
    if open_trades:
        await message.answer(
            "No accounts are registered for this supplier yet. "
            "The supplier needs to add one with /account."
        )
        return

    suppliers = await repo.suppliers_for_client(party["id"])
    blocks: list[str] = []
    for s in suppliers:
        accounts = await repo.list_bank_accounts(s["id"])
        if accounts:
            blocks.append("\n".join(_render_accounts(s["label"], accounts)).rstrip())

    if not blocks:
        await message.answer(
            "No accounts have been registered yet. Each supplier adds their "
            "own with /account in their group."
        )
        return

    await message.answer(
        "No trade is open at the moment. Registered accounts:\n\n"
        + "\n\n".join(blocks)
    )


# -------------------------------------------------------------------- /add

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext, party, repo) -> None:
    # No trade is chosen here. The account picked at the end decides it, the
    # same way a pasted payment is resolved — one rule, not two.
    if not await repo.open_trades_for_client(party["id"]):
        await message.answer("You have no open trade to add payments to.")
        return

    await state.set_state(AddPayment.amount)
    await message.answer("Amount?")


@router.message(AddPayment.amount)
async def add_amount(message: Message, state: FSMContext) -> None:
    """
    Clients paste straight out of banking apps, so the input arrives with
    commas, spaces, or a rupee sign attached. to_decimal strips all of it.
    """
    try:
        amount = round_inr(to_decimal(message.text or ""))
    except MoneyError:
        await message.answer("I could not read that as an amount. Send just the number.")
        return

    if amount <= 0:
        await message.answer("Amount must be greater than zero.")
        return

    await state.update_data(amount=str(amount))
    await state.set_state(AddPayment.utr)
    await message.answer("UTR?")


@router.message(AddPayment.utr)
async def add_utr(message: Message, state: FSMContext, repo) -> None:
    try:
        utr = normalise_utr(message.text or "")
    except MoneyError as exc:
        await message.answer(f"That UTR does not look right: {exc}")
        return

    accounts = await repo.open_trade_accounts_for_client(party["id"])
    if not accounts:
        await message.answer("The supplier has no registered accounts. Cannot continue.")
        await state.clear()
        return

    await state.update_data(utr=utr)
    await state.set_state(AddPayment.account)

    multi = len({a["trade_id"] for a in accounts}) > 1
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=(f"{a['supplier_label']} — {a['account_name']}"
                  if multi else a["account_name"]),
            callback_data=f"acct:{a['id']}")]
        for a in accounts
    ])
    await message.answer("Which account did you send it to?", reply_markup=kb)


@router.callback_query(AddPayment.account, F.data.startswith("acct:"))
async def add_account(call: CallbackQuery, state: FSMContext, party, repo,
                      notifier) -> None:
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()

    # The account decides the trade. Resolved here rather than carried in the
    # state, because the client had not yet chosen when the state was written.
    trade_id = None
    for a in await repo.open_trade_accounts_for_client(party["id"]):
        if a["id"] == account_id:
            trade_id = a["trade_id"]
            break

    if trade_id is None:
        await state.clear()
        await call.message.edit_text(
            "That account no longer belongs to an open trade. Nothing recorded."
        )
        await call.answer()
        return

    ok, msg = await repo.add_payment(
        trade_id=trade_id,
        utr=data["utr"],
        amount_inr=to_decimal(data["amount"]),
        beneficiary_account_id=account_id,
        added_by=party["id"],
    )
    await state.clear()

    if not ok:
        # E3: duplicate UTR rejected and the client told why.
        await call.message.edit_text(msg)
        await call.answer()
        return

    # Completion first. A payment that finishes a trade also drags it under the
    # near-completion threshold, so asking in the other order tells the supplier
    # to prepare the next batch and then, immediately, that this one is done.
    if not await notifier.check_completion(trade_id):
        await notifier.check_near_completion(trade_id)

    total = await repo.trade_paid_total(trade_id)
    await call.message.edit_text(
        f"{msg}\nRunning total: ₹{fmt_inr(total)}"
    )
    await call.answer()


# ------------------------------------------------------- pasted payments
#
# Client A's team pastes payments rather than answering prompts, in whatever
# order and formatting they happen to use (client note, 8 Sep 2026). Rather
# than asking them to change how they work, the bot reads what they send.
#
# Registered last so it only sees messages no FSM state claimed.
#
# The `~F.text.startswith("/")` is not cosmetic. aiogram tries handlers in
# registration order, so a bare F.text catch-all swallows every command
# registered below it — /done was defined after this handler and never ran, and
# because "/done" contains no digit the catch-all returned in silence. Filtering
# commands out here makes the order irrelevant, so the next command added to
# this file cannot be broken the same way. See tests/test_handler_order.py.


@router.message(
    (F.text & ~F.text.startswith("/")) | (F.caption & ~F.caption.startswith("/"))
)
async def on_pasted_payment(message: Message, state: FSMContext, party, repo,
                            notifier) -> None:
    if await state.get_state() is not None:
        return  # mid-conversation; the FSM handlers own this message

    # Clients send a screenshot of the bank confirmation with the details typed
    # underneath (client question, 10 September 2026). On a photo the details
    # are in `caption` and `text` is None, so filtering on text alone made the
    # bot ignore the message entirely — no reaction, no record, and no way for
    # the sender to tell. The image itself is not needed and is not stored.
    body = message.text or message.caption or ""

    # Every account this client could be paying, each carrying the trade it
    # belongs to. A client can have one trade open per supplier at the same
    # time, so "the" open trade is not a thing that exists — resolving it by
    # recency charged payments to whichever trade happened to start last
    # (live, 11 September 2026). The account identifies the trade.
    accounts = await repo.open_trade_accounts_for_client(party["id"])
    if not accounts:
        return  # nothing open — stay quiet rather than nagging on small talk

    result = parse_payments(body)

    if not result.payments:
        # Speak only if a UTR was actually present.
        #
        # This used to fire on any message containing a digit, so the bot
        # answered ordinary conversation — "send 5 lakh by 4" and the like. In
        # the client's live group on 10 September 2026 it replied to nearly
        # everything and their team found it unusable, which is fair: a bot
        # that interrupts a conversation it does not understand is worse than
        # one that says nothing at all.
        #
        # A bank reference is long and distinctive, so its presence is a
        # reliable sign someone was logging a payment rather than talking.
        # Without one, stay quiet. The standing rule still covers it: no
        # acknowledgement means it was not picked up.
        if result.saw_utr and result.problems:
            await message.answer(
                "\n".join(["I could not read that as a payment:", *result.problems])
                + "\n\nSend it as UTR, amount, and the account — or use /add."
            )
        return

    # account id -> (name, trade id). The account carries its trade, so
    # matching the name resolves both at once.
    by_id = {a["id"]: a for a in accounts}
    multi = len({a["trade_id"] for a in accounts}) > 1

    staged, lines = [], ["Read this as:", ""]
    for p in result.payments:
        account_id = match_account(p.beneficiary, accounts)
        row = by_id.get(account_id)
        staged.append({
            "utr": p.utr, "amount": str(p.amount_inr), "account_id": account_id,
            "trade_id": row["trade_id"] if row else None,
        })
        lines.append(p.utr)
        lines.append(fmt_inr_plain(p.amount_inr))
        if row:
            # With more than one trade running, name the supplier too — the
            # client should be able to see which trade their money landed on
            # rather than trusting it silently.
            lines.append(f"to {row['account_name']}"
                         + (f"  ({row['supplier_label']})" if multi else ""))
        else:
            lines.append("to ?  (account not recognised)")
        lines.append("")

    if len(staged) > 1:
        total = sum(to_decimal(s["amount"]) for s in staged)
        lines.append(f"{len(staged)} payments, total ₹{fmt_inr(total)}")
        lines.append("")

    for problem in result.problems:
        lines.append(f"Note: {problem}")

    await state.update_data(staged=staged)

    unmatched = [s for s in staged if s["account_id"] is None]
    if unmatched:
        # Never guess an account. A wrong one attributes money to the wrong
        # place — and now to the wrong trade as well — and the client is right
        # here to be asked. This is the one case that still stops and waits,
        # whatever the confirmation setting.
        await state.set_state(PastedPayment.account)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=(f"{a['supplier_label']} — {a['account_name']}"
                      if multi else a["account_name"]),
                callback_data=f"pacct:{a['id']}")]
            for a in accounts
        ])
        lines.append("Which account did these go to?")
        await message.answer("\n".join(lines), reply_markup=kb)
        return

    if result.problems:
        # Something was only partly readable. Never silently drop it.
        await state.set_state(PastedPayment.confirm)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Confirm", callback_data="pyes"),
            InlineKeyboardButton(text="Cancel", callback_data="pno"),
        ]])
        lines.append("Correct?")
        await message.answer("\n".join(lines), reply_markup=kb)
        return

    if REQUIRE_PASTE_CONFIRMATION:
        await state.set_state(PastedPayment.confirm)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Confirm", callback_data="pyes"),
            InlineKeyboardButton(text="Cancel", callback_data="pno"),
        ]])
        lines.append("Correct?")
        await message.answer("\n".join(lines), reply_markup=kb)
        return

    # Everything read cleanly and every account matched: record it and
    # acknowledge, no tap required (client decision, 8 Sep 2026).
    await state.clear()
    await _record(message, staged, party, repo, acknowledge=True,
                  notifier=notifier)


async def _acknowledge(message: Message) -> None:
    """
    Tell the client their message landed.

    A thumbs up on their own message is what the client asked for, and it is
    better than a reply: it sits against the payment it refers to, so in a busy
    group there is never any doubt which message was picked up.

    Reactions need Bot API 7.0 and can be disabled by group settings, so a
    plain "Noted." is the fallback. The rule the client's team is given is
    "no acknowledgement means it was not picked up", and that rule has to hold
    even when reactions are unavailable.
    """
    try:
        await message.react([ReactionTypeEmoji(emoji="👍")])
    except Exception:
        log.info("reaction unavailable in chat %s, replying instead", message.chat.id)
        await message.reply("Noted.")


async def _record(message, staged, party, repo, *, acknowledge: bool,
                  notifier=None) -> None:
    """
    Write the staged payments and report anything that was refused.

    Each entry carries its OWN trade, taken from the account it was paid to.
    One pasted message can legitimately span two trades — a client running a
    trade with each of two suppliers may pay both in one go — and before this
    every payment in a message went to whichever trade started most recently.
    """
    added, rejected = [], []
    touched: list[int] = []

    for s in staged:
        ok, msg = await repo.add_payment(
            trade_id=s["trade_id"], utr=s["utr"],
            amount_inr=to_decimal(s["amount"]),
            beneficiary_account_id=s["account_id"], added_by=party["id"],
        )
        (added if ok else rejected).append(msg)
        if ok and s["trade_id"] not in touched:
            touched.append(s["trade_id"])

    if rejected:
        # A duplicate UTR is never acknowledged silently — the client must know
        # that one did not go in (E3).
        totals = []
        for tid in touched or [s["trade_id"] for s in staged]:
            totals.append(f"Running total: ₹{fmt_inr(await repo.trade_paid_total(tid))}")
        await message.reply(
            "\n".join([*rejected, f"Recorded {len(added)} of {len(staged)}.", *totals])
        )
    elif acknowledge:
        # Acknowledge first, so the client sees the thumbs up on their own
        # message before any summary arrives underneath it.
        await _acknowledge(message)

    # Checked even when something was rejected: a paste can carry one duplicate
    # and one good payment, and that good payment can be the one that finishes
    # the trade. Returning early here would leave a fully paid trade open.
    if notifier is not None:
        for tid in touched:
            # Completion first — see the note in add_account.
            if not await notifier.check_completion(tid):
                await notifier.check_near_completion(tid)


@router.callback_query(PastedPayment.account, F.data.startswith("pacct:"))
async def pasted_pick_account(call: CallbackQuery, state: FSMContext, party, repo,
                              notifier) -> None:
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()

    # The chosen account decides the trade as well as the beneficiary — they
    # are the same fact. Look it up rather than carrying a trade in the state,
    # because the state was written before the client answered.
    chosen_trade = None
    for a in await repo.open_trade_accounts_for_client(party["id"]):
        if a["id"] == account_id:
            chosen_trade = a["trade_id"]
            break

    staged = [
        {**s,
         "account_id": s["account_id"] or account_id,
         "trade_id": s["trade_id"] or chosen_trade}
        for s in data["staged"]
    ]
    if REQUIRE_PASTE_CONFIRMATION:
        await state.update_data(staged=staged)
        await state.set_state(PastedPayment.confirm)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Confirm", callback_data="pyes"),
            InlineKeyboardButton(text="Cancel", callback_data="pno"),
        ]])
        await call.message.edit_text(
            call.message.text + "\n\nAccount set. Confirm?", reply_markup=kb
        )
        await call.answer()
        return

    # The account was the only open question — answering it is the
    # confirmation. Do not ask twice.
    await state.clear()
    await _record(call.message, staged, party, repo,
                  acknowledge=False, notifier=notifier)

    totals = []
    for tid in {s["trade_id"] for s in staged if s["trade_id"]}:
        totals.append(f"Running total: ₹{fmt_inr(await repo.trade_paid_total(tid))}")
    await call.message.edit_text(
        call.message.text.split("Which account")[0].rstrip()
        + "\n\nRecorded. " + "  ".join(totals)
    )
    await call.answer()


@router.callback_query(PastedPayment.confirm, F.data == "pyes")
async def pasted_confirm(call: CallbackQuery, state: FSMContext, party, repo,
                         notifier) -> None:
    data = await state.get_data()
    await state.clear()

    added, rejected, touched = [], [], []
    for s in data["staged"]:
        ok, msg = await repo.add_payment(
            trade_id=s["trade_id"], utr=s["utr"],
            amount_inr=to_decimal(s["amount"]),
            beneficiary_account_id=s["account_id"], added_by=party["id"],
        )
        (added if ok else rejected).append(msg)
        if ok and s["trade_id"] not in touched:
            touched.append(s["trade_id"])

    out = [f"Recorded {len(added)} payment(s)."]
    out += [f"  {m}" for m in rejected]          # E3: duplicates named, not hidden
    for tid in touched:
        out.append(f"Running total: ₹{fmt_inr(await repo.trade_paid_total(tid))}")

    # Completion first. A payment that finishes a trade also drags it under the
    # near-completion threshold, so asking in the other order tells the supplier
    # to prepare the next batch and then, immediately, that this one is done.
    for tid in touched:
        if not await notifier.check_completion(tid):
            await notifier.check_near_completion(tid)
    await call.message.edit_text("\n".join(out))
    await call.answer()


@router.callback_query(PastedPayment.confirm, F.data == "pno")
async def pasted_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Discarded. Nothing was recorded.")
    await call.answer()


# ------------------------------------------------------------------- /done

@router.message(Command("done"))
async def cmd_done(message: Message, party, repo, notifier) -> None:
    """
    Close a trade early, before its payments cover the expected total.

    A trade that IS fully paid now closes itself the moment the last payment
    lands (client request, 10 September 2026) — nobody has to remember a
    command, and the supplier is not left waiting on a trade that is finished.

    This remains for the case that automation cannot decide: a trade that will
    never be paid in full, where the Bridge has accepted the shortfall. The
    summary states the difference plainly, as it always has.
    """
    open_trades = await repo.open_trades_for_client(party["id"])
    if not open_trades:
        await message.answer("You have no open trade to close.")
        return

    if len(open_trades) > 1:
        # Closing a trade short is a decision about a specific one. With
        # several running there is no "the" trade to close, and guessing would
        # close the wrong supplier's — which is the same class of mistake that
        # misattributed payments (11 September 2026).
        await message.answer(
            "You have more than one trade open, so I cannot tell which to "
            "close:\n\n"
            + "\n".join(
                f"  {t['reference']} — {t['supplier_label']}" for t in open_trades
            )
            + "\n\nA trade closes itself as soon as its payments cover the "
              "expected total. Use this only for one that will never be paid "
              "in full, and tell the Bridge which."
        )
        return

    trade = open_trades[0]
    rows = await repo.trade_payments(trade["id"])
    if not rows:
        await message.answer("No payments have been added yet. Nothing to close.")
        return

    payments = [
        Payment(
            utr=r["utr"],
            amount_inr=r["amount_inr"],
            beneficiary_name=r["account_name"],
        )
        for r in rows
    ]

    summary = render_trade_summary(
        payments,
        reference=trade["reference"],
        expected_inr=trade["inr_expected"] or None,
    )
    total = sum(p.amount_inr for p in payments)

    await repo.complete_trade(trade["id"], party["id"])

    # The client sees the summary plus their own confirmation wording.
    await message.answer(summary)
    await message.answer(render_completion_notice(total))

    await notifier.to_bridge(summary)
    await notifier.to_party(
        trade["supplier_id"],
        render_supplier_summary(payments, expected_inr=trade["inr_expected"] or None),
    )

    log.info(
        "trade %s completed by client %s, total %s",
        trade["reference"], party["label"], total,
    )
