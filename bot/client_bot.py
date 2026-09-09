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
from core.summary import Payment, render_completion_notice, render_trade_summary

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

@router.message(Command("accounts"))
async def cmd_accounts(message: Message, party, repo) -> None:
    """Show the supplier accounts this client can pay into."""
    trade = await repo.open_trade_for_client(party["id"])
    if trade is None:
        await message.answer("You have no open trade at the moment.")
        return

    accounts = await repo.list_bank_accounts(trade["supplier_id"])
    if not accounts:
        await message.answer(
            "No accounts are registered for this supplier yet. "
            "The supplier needs to add one with /account."
        )
        return

    # D4: the supplier is shown by label only, never by real identity.
    lines = [f"Accounts under {trade['supplier_label']}:", ""]
    for a in accounts:
        lines.append(a["account_name"])
        lines.append(f"Acc num - {a['account_number']}")
        lines.append(f"Ifsc - {a['ifsc']}")
        lines.append("")
    await message.answer("\n".join(lines).rstrip())


# -------------------------------------------------------------------- /add

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext, party, repo) -> None:
    trade = await repo.open_trade_for_client(party["id"])
    if trade is None:
        await message.answer("You have no open trade to add payments to.")
        return

    await state.update_data(trade_id=trade["id"], supplier_id=trade["supplier_id"])
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

    data = await state.get_data()
    accounts = await repo.list_bank_accounts(data["supplier_id"])
    if not accounts:
        await message.answer("The supplier has no registered accounts. Cannot continue.")
        await state.clear()
        return

    await state.update_data(utr=utr)
    await state.set_state(AddPayment.account)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a["account_name"], callback_data=f"acct:{a['id']}")]
        for a in accounts
    ])
    await message.answer("Which account did you send it to?", reply_markup=kb)


@router.callback_query(AddPayment.account, F.data.startswith("acct:"))
async def add_account(call: CallbackQuery, state: FSMContext, party, repo,
                      notifier) -> None:
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()

    ok, msg = await repo.add_payment(
        trade_id=data["trade_id"],
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

    await notifier.check_near_completion(data["trade_id"])

    total = await repo.trade_paid_total(data["trade_id"])
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

    trade = await repo.open_trade_for_client(party["id"])
    if trade is None:
        return  # nothing open — stay quiet rather than nagging on small talk

    result = parse_payments(body)

    if not result.payments:
        # Almost always ordinary conversation, not a failed paste. Only speak
        # up if it looked like an attempt.
        if result.problems and any(c.isdigit() for c in (message.text or "")):
            await message.answer(
                "\n".join(["I could not read that as a payment:", *result.problems])
                + "\n\nSend it as UTR, amount, and the account — or use /add."
            )
        return

    accounts = await repo.list_bank_accounts(trade["supplier_id"])

    staged, lines = [], ["Read this as:", ""]
    for p in result.payments:
        account_id = match_account(p.beneficiary, accounts)
        staged.append({
            "utr": p.utr, "amount": str(p.amount_inr), "account_id": account_id,
        })
        name = next((a["account_name"] for a in accounts if a["id"] == account_id), None)
        lines.append(p.utr)
        lines.append(fmt_inr_plain(p.amount_inr))
        lines.append(f"to {name}" if name else "to ?  (account not recognised)")
        lines.append("")

    if len(staged) > 1:
        total = sum(to_decimal(s["amount"]) for s in staged)
        lines.append(f"{len(staged)} payments, total ₹{fmt_inr(total)}")
        lines.append("")

    for problem in result.problems:
        lines.append(f"Note: {problem}")

    await state.update_data(staged=staged, supplier_id=trade["supplier_id"],
                            trade_id=trade["id"])

    unmatched = [s for s in staged if s["account_id"] is None]
    if unmatched:
        # Never guess an account. A wrong one attributes money to the wrong
        # place, and the client is right here to be asked. This is the one case
        # that still stops and waits, whatever the confirmation setting.
        await state.set_state(PastedPayment.account)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=a["account_name"], callback_data=f"pacct:{a['id']}")]
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
    await _record(message, trade["id"], staged, party, repo, acknowledge=True,
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


async def _record(message, trade_id, staged, party, repo, *, acknowledge: bool,
                  notifier=None) -> None:
    """Write the staged payments and report anything that was refused."""
    added, rejected = [], []
    for s in staged:
        ok, msg = await repo.add_payment(
            trade_id=trade_id, utr=s["utr"],
            amount_inr=to_decimal(s["amount"]),
            beneficiary_account_id=s["account_id"], added_by=party["id"],
        )
        (added if ok else rejected).append(msg)

    if rejected:
        # A duplicate UTR is never acknowledged silently — the client must know
        # that one did not go in (E3).
        total = await repo.trade_paid_total(trade_id)
        await message.reply(
            "\n".join([*rejected, f"Recorded {len(added)} of {len(staged)}.",
                       f"Running total: ₹{fmt_inr(total)}"])
        )
        return

    if notifier is not None:
        await notifier.check_near_completion(trade_id)

    if acknowledge:
        await _acknowledge(message)


@router.callback_query(PastedPayment.account, F.data.startswith("pacct:"))
async def pasted_pick_account(call: CallbackQuery, state: FSMContext, party, repo,
                              notifier) -> None:
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    staged = [
        {**s, "account_id": s["account_id"] or account_id} for s in data["staged"]
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
    total_before = await repo.trade_paid_total(data["trade_id"])
    await _record(call.message, data["trade_id"], staged, party, repo,
                  acknowledge=False, notifier=notifier)
    total = await repo.trade_paid_total(data["trade_id"])
    await call.message.edit_text(
        call.message.text.split("Which account")[0].rstrip()
        + f"\n\nRecorded. Running total: ₹{fmt_inr(total)}"
    )
    await call.answer()


@router.callback_query(PastedPayment.confirm, F.data == "pyes")
async def pasted_confirm(call: CallbackQuery, state: FSMContext, party, repo,
                         notifier) -> None:
    data = await state.get_data()
    await state.clear()

    added, rejected = [], []
    for s in data["staged"]:
        ok, msg = await repo.add_payment(
            trade_id=data["trade_id"], utr=s["utr"],
            amount_inr=to_decimal(s["amount"]),
            beneficiary_account_id=s["account_id"], added_by=party["id"],
        )
        (added if ok else rejected).append(msg)

    total = await repo.trade_paid_total(data["trade_id"])
    out = [f"Recorded {len(added)} payment(s)."]
    out += [f"  {m}" for m in rejected]          # E3: duplicates named, not hidden
    out.append(f"Running total: ₹{fmt_inr(total)}")

    await notifier.check_near_completion(data["trade_id"])
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
    Close the trade and distribute the summary.

    Goes to the client, the Bridge, and the supplier (brief, Step 6).
    """
    trade = await repo.open_trade_for_client(party["id"])
    if trade is None:
        await message.answer("You have no open trade to close.")
        return

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

    # The supplier's copy is headed TRADE COMPLETED and carries no deal
    # reference (client request, 10 September 2026: "notification to supplier
    # needs header TRADE COMPLETED" and "remove the SUPA1 as dont want them to
    # see that"). The tranches and the arithmetic are identical, so all three
    # parties still reconcile against the same figures — only the heading
    # differs, and the reference is internal to the Bridge.
    await notifier.to_party(
        trade["supplier_id"],
        "TRADE COMPLETED\n\n" + render_trade_summary(
            payments,
            expected_inr=trade["inr_expected"] or None,
            include_header=False,
        ),
    )

    log.info(
        "trade %s completed by client %s, total %s",
        trade["reference"], party["label"], total,
    )
