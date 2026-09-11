"""
Bridge trade lifecycle: confirming a deposit into payment instructions,
cancelling, correcting, and exporting.

The confirmation flow is the missing half of the brief's Step 4. A deposit is
detected, the Bridge is told, and then the Bridge has to decide which of the
supplier's accounts the client should pay into and how much goes to each. That
decision cannot be inferred: a representative trade shows ₹1,499,297 split
across six transfers into two different accounts.

The flow supports one slot or several, which covers every option put to the
client without waiting for their answer:
  - one account, full amount           -> a single slot
  - several accounts, split by you      -> several slots
  - one total, client splits it freely  -> a single slot; /add still lets the
                                           client choose any of the supplier's
                                           accounts per tranche
"""

from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from core.money import MoneyError, fmt_inr, fmt_usdt_plain, round_inr, to_decimal
from core.slots import check_new_slot
from core.summary import (
    PaymentSlot, render_payment_slot, render_send_instruction,
)

log = logging.getLogger(__name__)
router = Router()


class Confirm(StatesGroup):
    account = State()
    amount = State()
    more = State()
    final = State()


class Cancel(StatesGroup):
    pick = State()
    reason = State()


class Correct(StatesGroup):
    pick_trade = State()
    action = State()
    pick_payment = State()
    reason = State()


# ======================================================================
# Confirming a deposit into payment instructions
# ======================================================================

def confirm_keyboard(trade_id: int) -> InlineKeyboardMarkup:
    """Attached to every deposit notification so confirmation is one tap away."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm and issue to client",
                             callback_data=f"cf:{trade_id}")
    ]])


@router.callback_query(F.data.startswith("cf:"))
async def confirm_start(call: CallbackQuery, state: FSMContext, repo) -> None:
    trade_id = int(call.data.split(":", 1)[1])
    trade = await repo.trade_detail(trade_id)

    if trade is None:
        await call.answer("That trade no longer exists.", show_alert=True)
        return
    if trade["status"] == "cancelled":
        await call.answer("That trade was cancelled.", show_alert=True)
        return

    accounts = await repo.list_bank_accounts(trade["supplier_id"])
    if not accounts:
        await call.answer(
            "That supplier has no registered accounts yet.", show_alert=True
        )
        return

    # Allocate what is still OWED, not the gross total.
    #
    # A supplier can send in two goes. The second deposit lands on the same open
    # trade and raises inr_expected — but by then the client may already have
    # paid against the first instruction. Offering the gross figure here would
    # have you issue an instruction for the whole new total, and a client who
    # pays it after already paying the first one has overpaid.
    paid = trade["paid_inr"] or Decimal(0)
    outstanding = round_inr(trade["inr_expected"]) - round_inr(paid)

    if outstanding <= 0:
        await call.answer(
            "This trade is already covered by payments received.", show_alert=True
        )
        return

    await state.set_state(Confirm.account)
    await state.update_data(
        trade_id=trade_id,
        supplier_id=trade["supplier_id"],
        remaining=str(outstanding),
        slots=[],
    )

    # The supplier nominates the account when they send (client decision,
    # 7 Sep 2026). Show their choice first and marked, so the normal path is one
    # tap — but leave the others selectable so the Bridge can override.
    nominated = trade["nominated_account_id"]
    ordered = sorted(accounts, key=lambda a: a["id"] != nominated)

    rows = []
    for a in ordered:
        mark = "✓ " if a["id"] == nominated else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{a['account_name']}", callback_data=f"cfa:{a['id']}"
        )])

    note = ""
    if nominated:
        chosen = next((a["account_name"] for a in accounts if a["id"] == nominated), None)
        if chosen:
            note = f"\nSupplier nominated: {chosen}"
    else:
        note = "\nThe supplier has not nominated an account yet."

    # If instructions have already gone out on this trade, say so before any
    # more are sent. The client keeps the old message in their chat, and two
    # live instructions for the same trade is how someone pays twice.
    already = await repo.trade_slots(trade_id)
    if already:
        note += (
            f"\n\n⚠ {len(already)} instruction(s) have already been sent to the "
            "client for this trade. Sending new ones replaces them here, but the "
            "old message stays in their chat — tell them to ignore it."
        )

    lines = [f"Transaction {trade['reference']}"]
    if paid > 0:
        lines.append(f"Expected ₹{fmt_inr(trade['inr_expected'])}, "
                     f"already paid ₹{fmt_inr(paid)}.")
        lines.append(f"Still to instruct: ₹{fmt_inr(outstanding)}.{note}")
    else:
        lines.append(f"Client pays ₹{fmt_inr(outstanding)} in total.{note}")
    lines.append("")
    lines.append("Which account should they pay into?")

    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(Confirm.account, F.data.startswith("cfa:"))
async def confirm_account(call: CallbackQuery, state: FSMContext, repo) -> None:
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    remaining = to_decimal(data["remaining"])

    await state.update_data(pending_account=account_id)
    await state.set_state(Confirm.amount)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Full remaining — ₹{fmt_inr(remaining)}",
                             callback_data="cffull")
    ]])
    await call.message.edit_text(
        f"How much should go to this account?\n"
        f"Remaining to allocate: ₹{fmt_inr(remaining)}",
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(Confirm.amount, F.data == "cffull")
async def confirm_amount_full(call: CallbackQuery, state: FSMContext, repo) -> None:
    data = await state.get_data()
    await _add_slot(call.message, state, repo, to_decimal(data["remaining"]), edit=True)
    await call.answer()


@router.message(Confirm.amount)
async def confirm_amount_typed(message: Message, state: FSMContext, repo) -> None:
    try:
        amount = round_inr(to_decimal(message.text or ""))
    except MoneyError:
        await message.answer("I could not read that as an amount. Send just the number.")
        return

    data = await state.get_data()
    already = [to_decimal(m) for _, m in data["slots"]]
    expected = to_decimal(data["remaining"]) + sum(already, Decimal(0))

    try:
        # Over-allocating would instruct the client to pay more than the deposit
        # justifies, so it is blocked rather than warned about.
        amount = check_new_slot(already, amount, expected)
    except MoneyError as exc:
        await message.answer(f"{exc} Enter a smaller amount.")
        return

    await _add_slot(message, state, repo, amount, edit=False)


async def _add_slot(message, state: FSMContext, repo, amount: Decimal, *, edit: bool) -> None:
    data = await state.get_data()
    slots = list(data["slots"])
    slots.append([data["pending_account"], str(amount)])
    remaining = to_decimal(data["remaining"]) - amount

    await state.update_data(slots=slots, remaining=str(remaining), pending_account=None)

    if remaining <= 0:
        await _show_final(message, state, repo, edit=edit)
        return

    await state.set_state(Confirm.more)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Add another account", callback_data="cfmore")],
        [InlineKeyboardButton(text="Send as it is", callback_data="cfsend")],
    ])
    text = (
        f"Allocated so far: {len(slots)} account(s).\n"
        f"Still unallocated: ₹{fmt_inr(remaining)}"
    )
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(Confirm.more, F.data == "cfmore")
async def confirm_more(call: CallbackQuery, state: FSMContext, repo) -> None:
    data = await state.get_data()
    accounts = await repo.list_bank_accounts(data["supplier_id"])
    await state.set_state(Confirm.account)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a["account_name"], callback_data=f"cfa:{a['id']}")]
        for a in accounts
    ])
    await call.message.edit_text("Which account next?", reply_markup=kb)
    await call.answer()


@router.callback_query(Confirm.more, F.data == "cfsend")
async def confirm_send_partial(call: CallbackQuery, state: FSMContext, repo) -> None:
    await _show_final(call.message, state, repo, edit=True)
    await call.answer()


async def _show_final(message, state: FSMContext, repo, *, edit: bool) -> None:
    """
    Render the confirmation exactly as the brief specifies, for the Bridge to
    approve before anything reaches the client.
    """
    data = await state.get_data()
    trade = await repo.trade_detail(data["trade_id"])
    accounts = {a["id"]: a for a in await repo.list_bank_accounts(data["supplier_id"])}

    slot_objs = []
    for account_id, amount in data["slots"]:
        a = accounts[account_id]
        slot_objs.append(PaymentSlot(
            account_name=a["account_name"],
            account_number=a["account_number"],
            ifsc=a["ifsc"],
            amount_inr=to_decimal(amount),
        ))

    # Built from the same two renderers the real messages use, so the preview
    # cannot drift away from what actually gets sent. The separator marks where
    # one client message ends and the next begins — they go out individually.
    allocated = sum(to_decimal(m) for _, m in data["slots"])
    preview = "\n\n".join([
        render_send_instruction(
            client_label=trade["client_label"],
            usdt_out=trade["usdt_owed_client"],
            inr_amount=allocated,
            sell_rate=trade["sell_rate"],
        ),
        f"To {trade['client_label']}, as {len(slot_objs)} separate message(s):",
        *[render_payment_slot(s) for s in slot_objs],
    ])

    await state.set_state(Confirm.final)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Send to client", callback_data="cfyes"),
        InlineKeyboardButton(text="Start over", callback_data="cfno"),
    ]])

    if edit:
        await message.edit_text(preview, reply_markup=kb)
    else:
        await message.answer(preview, reply_markup=kb)


@router.callback_query(Confirm.final, F.data == "cfyes")
async def confirm_final(call: CallbackQuery, state: FSMContext, party, repo, notifier) -> None:
    data = await state.get_data()
    trade = await repo.trade_detail(data["trade_id"])

    slots = [(account_id, to_decimal(amount)) for account_id, amount in data["slots"]]
    await repo.issue_slots(
        trade_id=data["trade_id"], slots=slots, actor_party_id=party["id"]
    )

    accounts = {a["id"]: a for a in await repo.list_bank_accounts(data["supplier_id"])}
    slot_objs = [
        PaymentSlot(
            account_name=accounts[a]["account_name"],
            account_number=accounts[a]["account_number"],
            ifsc=accounts[a]["ifsc"],
            amount_inr=m,
        )
        for a, m in slots
    ]

    # One message per slot, nothing else (client request, 10 Sep 2026: "need to
    # be separate messages to client, or their bot wont pick it up"). The
    # client's side is automated, so each message it receives should be one
    # complete instruction it can parse on its own.
    #
    # html=True so the account number is tap-to-copy — it gets typed into a
    # banking app, which is where a wrong digit costs most (client request,
    # 8 Sep 2026).
    for slot in slot_objs:
        await notifier.to_party(
            trade["client_id"], render_payment_slot(slot, html=True), html=True
        )

    await state.clear()

    # The onward obligation goes to the Bridge, who is the one who has to act
    # on it, rather than to the client.
    # html=True and parse_mode together, or the USDT figure is not tap-to-copy.
    # Missing the parse_mode was the whole bug: the <code> markup was rendered
    # as literal text, so the client could not copy the amount they had to send
    # (reported 10 September 2026, "I cannot copy just the Usdt value").
    await call.message.edit_text(
        f"Sent to {trade['client_label']}. Waiting for payment.\n\n"
        + render_send_instruction(
            client_label=trade["client_label"],
            usdt_out=trade["usdt_owed_client"],
            inr_amount=sum(m for _, m in slots),
            sell_rate=trade["sell_rate"],
            html=True,
        ),
        parse_mode="HTML",
    )

    # The amount on its own, as its own message.
    #
    # Tap-to-copy works on desktop but not reliably on iPhone, where copying
    # tends to take the whole message (client, 11 September 2026: "i am unable
    # to easily copy this, as iPhone has restrictions, it always picks up the
    # whole message"). A message containing nothing but the number sidesteps
    # every client's quirks: copy the message and you have the figure, with no
    # label, no currency, and nothing to trim off.
    #
    # Sent after the instruction so it is the last thing on screen, which is
    # also where a thumb lands.
    await call.message.answer(fmt_usdt_plain(trade["usdt_owed_client"]))

    await call.answer()
    log.info("trade %s slots issued and sent to client", trade["reference"])


@router.callback_query(Confirm.final, F.data == "cfno")
async def confirm_restart(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "Discarded. Nothing was sent. Tap Confirm on the deposit notice to start again."
    )
    await call.answer()


# ======================================================================
# /cancel  (answer E4)
# ======================================================================

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, repo) -> None:
    trades = await repo.list_open_trades()
    if not trades:
        await message.answer("There are no open trades to cancel.")
        return

    await state.set_state(Cancel.pick)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{t['reference']} — {t['supplier_label']} → {t['client_label']}",
            callback_data=f"cx:{t['id']}",
        )]
        for t in trades
    ])
    await message.answer("Which trade do you want to cancel?", reply_markup=kb)


@router.callback_query(Cancel.pick, F.data.startswith("cx:"))
async def cancel_pick(call: CallbackQuery, state: FSMContext, repo) -> None:
    trade_id = int(call.data.split(":", 1)[1])
    trade = await repo.trade_detail(trade_id)

    await state.update_data(trade_id=trade_id)
    await state.set_state(Cancel.reason)

    note = ""
    if trade["paid_inr"] > 0:
        # Worth surfacing loudly - money has already moved on this trade.
        note = (
            f"\n\nNOTE: ₹{fmt_inr(trade['paid_inr'])} has already been logged "
            "against this trade. Those payment records are kept."
        )
    await call.message.edit_text(
        f"Cancelling {trade['reference']}.{note}\n\nWhy? (this goes in the audit log)"
    )
    await call.answer()


@router.message(Cancel.reason)
async def cancel_reason(message: Message, state: FSMContext, party, repo) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Give a short reason so the record makes sense later.")
        return

    data = await state.get_data()
    ok, msg = await repo.cancel_trade(
        trade_id=data["trade_id"], actor_party_id=party["id"], reason=reason
    )
    await state.clear()
    await message.answer(msg)


# ======================================================================
# /correct  (answer E5)
# ======================================================================

@router.message(Command("correct"))
async def cmd_correct(message: Message, state: FSMContext, repo) -> None:
    trades = await repo.recent_completed_trades(10)
    if not trades:
        await message.answer("There are no completed trades to correct.")
        return

    await state.set_state(Correct.pick_trade)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{t['reference']} — {t['client_label']}",
            callback_data=f"co:{t['id']}",
        )]
        for t in trades
    ])
    await message.answer("Which completed trade?", reply_markup=kb)


@router.callback_query(Correct.pick_trade, F.data.startswith("co:"))
async def correct_pick(call: CallbackQuery, state: FSMContext, repo) -> None:
    trade_id = int(call.data.split(":", 1)[1])
    await state.update_data(trade_id=trade_id)
    await state.set_state(Correct.action)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Reopen the trade", callback_data="cor")],
        [InlineKeyboardButton(text="Remove a wrong payment", callback_data="cop")],
    ])
    await call.message.edit_text("What needs correcting?", reply_markup=kb)
    await call.answer()


@router.callback_query(Correct.action, F.data == "cor")
async def correct_reopen(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="reopen")
    await state.set_state(Correct.reason)
    await call.message.edit_text("Why is it being reopened? (this goes in the audit log)")
    await call.answer()


@router.callback_query(Correct.action, F.data == "cop")
async def correct_payment_list(call: CallbackQuery, state: FSMContext, repo) -> None:
    data = await state.get_data()
    rows = await repo.trade_payment_rows(data["trade_id"])
    if not rows:
        await call.message.edit_text("That trade has no payments recorded.")
        await state.clear()
        await call.answer()
        return

    await state.set_state(Correct.pick_payment)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{r['utr'][-8:]} — ₹{fmt_inr(r['amount_inr'])}",
            callback_data=f"cop:{r['id']}",
        )]
        for r in rows
    ])
    await call.message.edit_text("Which payment is wrong?", reply_markup=kb)
    await call.answer()


@router.callback_query(Correct.pick_payment, F.data.startswith("cop:"))
async def correct_payment_pick(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="void", payment_id=int(call.data.split(":", 1)[1]))
    await state.set_state(Correct.reason)
    await call.message.edit_text("Why is it being removed? (this goes in the audit log)")
    await call.answer()


@router.message(Correct.reason)
async def correct_reason(message: Message, state: FSMContext, party, repo) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Give a short reason so the record makes sense later.")
        return

    data = await state.get_data()
    if data["mode"] == "reopen":
        ok, msg = await repo.reopen_trade(
            trade_id=data["trade_id"], actor_party_id=party["id"], reason=reason
        )
    else:
        ok, msg = await repo.void_payment(
            payment_id=data["payment_id"], actor_party_id=party["id"], reason=reason
        )

    await state.clear()
    await message.answer(msg)


# ======================================================================
# /export  (answer G3)
# ======================================================================

@router.message(Command("export"))
async def cmd_export(message: Message, repo) -> None:
    rows = await repo.export_rows(days=90)
    if not rows:
        await message.answer("Nothing to export in the last 90 days.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Reference", "Status", "Opened", "Completed", "Supplier", "Client",
        "Supply rate", "Sell rate", "USDT received", "INR expected",
        "USDT owed client", "Margin USDT", "UTR", "Payment INR", "Beneficiary",
    ])
    for r in rows:
        writer.writerow([
            r["reference"], r["status"],
            r["opened_at"].strftime("%Y-%m-%d %H:%M") if r["opened_at"] else "",
            r["completed_at"].strftime("%Y-%m-%d %H:%M") if r["completed_at"] else "",
            r["supplier"], r["client"],
            r["supply_rate"], r["sell_rate"], r["usdt_received"], r["inr_expected"],
            r["usdt_owed_client"], r["margin_usdt"],
            r["utr"] or "", r["amount_inr"] or "", r["beneficiary"] or "",
        ])

    # utf-8-sig so Excel opens the rupee sign and any non-ASCII names correctly.
    data = buf.getvalue().encode("utf-8-sig")
    await message.answer_document(
        BufferedInputFile(data, filename="settlement_export.csv"),
        caption=f"{len(rows)} rows, last 90 days.",
    )
