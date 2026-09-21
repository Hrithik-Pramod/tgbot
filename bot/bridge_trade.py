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

from core.money import (
    MoneyError, fmt_inr, fmt_rate, fmt_usdt_plain, round_inr, to_decimal,
)
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
    why = State()
    reason = State()


class Reprice(StatesGroup):
    pick = State()
    confirm = State()


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


@router.message(Command("issue"))
async def cmd_issue(message: Message, repo) -> None:
    """
    Issue — or re-issue — the payment instruction for an open trade.

    Until now the only route to the confirmation flow was the button attached
    to the deposit notification. That works right up until the message is
    scrolled past, or the trade changes after it was posted, and then there is
    no way to reach it at all.

    On 11 September 2026 the Bridge had a deposit he needed to send an
    instruction for, could not find the button, tried /send instead — which
    only writes a note to his own channel — and was stuck mid-trade with the
    client waiting. The flow existed; the door to it did not.
    """
    trades = await repo.list_open_trades()
    if not trades:
        await message.answer("There are no open trades.")
        return

    rows = []
    for t in trades:
        paid = t["paid_inr"] or Decimal(0)
        outstanding = round_inr(t["inr_expected"] or Decimal(0)) - round_inr(paid)
        rows.append([InlineKeyboardButton(
            text=f"{t['reference']} — {t['supplier_label']} — ₹{fmt_inr(outstanding)}",
            callback_data=f"cf:{t['id']}",
        )])

    await message.answer(
        "Which trade do you want to issue an instruction for?\n\n"
        "The amount shown is what is still outstanding.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


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

    # What the client was told last time, read before it is replaced.
    #
    # Client request, 18 September 2026:
    #
    #     Vendor payment account change: We need a way to handle situations
    #     where the vendor changes the payment account after the payment
    #     details have already been sent.
    #
    # Re-issuing already worked — the slots are replaced, only the unpaid
    # balance is reallocated, and instructed_at holds. What was missing was
    # anyone telling the client. The superseded instruction simply stayed in
    # their chat, indistinguishable from the live one, and their side is
    # automated: it reads payment instructions and acts on them. Sending a
    # replacement without retracting the original is how a vendor's old
    # account gets paid after they have moved off it.
    superseded = []
    if trade["instructed_at"] is not None:
        superseded = await repo.trade_slots(data["trade_id"])

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
    # The retraction goes FIRST, so the client reads "stop" before they read
    # the replacement. The other order leaves a window where the newest thing
    # on their screen is an instruction they are being told to ignore.
    #
    # It names the accounts rather than just saying "the previous ones",
    # because the client may be holding several messages and only the account
    # number tells them which. Those numbers were already sent to them, so
    # this discloses nothing new — and it says nothing about the vendor.
    if superseded:
        dropped = [
            s for s in superseded
            if s["account_number"] not in {o.account_number for o in slot_objs}
        ]
        lines = [
            f"CANCELLED — the payment details for {trade['reference']} have "
            "changed.",
            "",
            "Do NOT pay against the previous instruction. New details follow "
            "in the next message.",
        ]
        if dropped:
            lines += ["", "No longer to be paid:"]
            lines += [
                f"  {s['account_name']}  {s['account_number']}"
                for s in dropped
            ]
        lines += [
            "",
            "Anything you have already sent is still counted — the new "
            "amounts are what remains.",
        ]
        await notifier.to_party(trade["client_id"], "\n".join(lines))
        log.info(
            "trade %s re-issued; %d superseded slot(s) retracted to the client",
            trade["reference"], len(superseded),
        )

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
    #
    # Unless it looks like it has already been sent. On 18 September 2026 two
    # trades were reopened for deposits the Bridge had already paid the client
    # for, and this line would have handed him 2,321.22 and 6,922.01 to send a
    # second time. The last thing on screen, one tap from the clipboard, with
    # nothing between it and 9,243 USDT but a WhatsApp message he would have
    # had to remember at three in the morning.
    #
    # So when there is a matching payout already out the door, the bare number
    # is withheld and he has to ask for it. Withheld rather than merely warned
    # above, because the number is what gets acted on: a caption over a
    # copy-ready figure is read after the copy, if at all.
    prior = await repo.prior_payouts_for_trade(trade["id"])
    if prior:
        already = "\n".join(
            f"  {fmt_usdt_plain(p['amount_usdt'])} USDT   "
            f"{p['detected_at']:%d %b %H:%M}   {p['tx_hash'][:12]}…"
            for p in prior
        )
        await call.message.answer(
            "HOLD — this may already be paid.\n\n"
            f"{already}\n\n"
            "left for this client after the supplier's deposit landed and "
            "before you issued, which is not the usual order.\n\n"
            f"{trade['reference']} says "
            f"{fmt_usdt_plain(trade['usdt_owed_client'])} USDT is owed out. "
            "If that is the same money, send nothing.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Show the amount anyway",
                                     callback_data=f"pay:{trade['id']}")
            ]]),
        )
        log.warning(
            "trade %s issued with %d prior payout(s) matching the amount owed",
            trade["reference"], len(prior),
        )
    else:
        await call.message.answer(fmt_usdt_plain(trade["usdt_owed_client"]))

    await call.answer()
    log.info("trade %s slots issued and sent to client", trade["reference"])


@router.callback_query(F.data.startswith("pay:"))
async def show_payout_amount(call: CallbackQuery, repo) -> None:
    """
    The figure, once he has said he wants it anyway.

    No second confirmation and no lecture. He has been told what the bot can
    see; deciding against it is his call to make, and making him fight for it
    is how a warning turns into something people route around.
    """
    trade_id = int(call.data.split(":", 1)[1])
    trade = await repo.trade_detail(trade_id)
    if trade is None:
        await call.answer("That trade no longer exists.", show_alert=True)
        return

    await call.message.answer(fmt_usdt_plain(trade["usdt_owed_client"]))
    await call.answer()
    log.info("trade %s payout amount revealed after a prior-payout warning",
             trade["reference"])


@router.callback_query(Confirm.final, F.data == "cfno")
async def confirm_restart(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "Discarded. Nothing was sent. Tap Confirm on the deposit notice to start again."
    )
    await call.answer()


# ======================================================================
# /reprice
# ======================================================================
#
# The rate moves between the deposit landing and the instruction going out.
# Until this existed the Bridge had exactly one lever for that — /cancel —
# and on 16 September he used it:
#
#     Peter bot is not reading the slips
#     Just fyi, lots of slips keep missing Pter
#
# Cancelling the trades took their suppliers' accounts out of the client's
# matcher, and the client went on paying them into a bot that could no longer
# recognise the names. The slips were a symptom. This is the cause.
#
# The guards live in repo.reprice_trade and are re-checked there under a row
# lock. What happens here is only the asking.

def _reprice_blocker(t) -> str | None:
    """
    Why this trade may not be moved, in the Bridge's words rather than a
    status code. None means it can be.
    """
    if t["instructed_at"] is not None:
        return "already issued to the client"
    if t["paid_inr"]:
        return f"₹{fmt_inr(t['paid_inr'])} already paid at the old rate"
    if t["current_rate_id"] is None:
        return "no rate set for this pairing"
    if t["current_rate_id"] == t["rate_id"]:
        return "already on the rate in force"
    return None


@router.message(Command("reprice"))
async def cmd_reprice(message: Message, state: FSMContext, repo) -> None:
    trades = await repo.repriceable_trades()
    if not trades:
        await message.answer("There are no open trades.")
        return

    movable = [t for t in trades if _reprice_blocker(t) is None]

    if not movable:
        # Never a bare "no". Every one of these has a different answer, and
        # not knowing which is what sent him to /cancel.
        lines = ["No open trade can be repriced right now.", ""]
        for t in trades:
            lines.append(f"{t['reference']} — {_reprice_blocker(t)}")
        lines += [
            "",
            "If the rate has moved, set it with /setrate first — a trade can "
            "only be moved onto a rate that exists.",
            "",
            "A trade the client is already holding cannot be repriced at all. "
            "Cancel and re-issue that one.",
        ]
        await message.answer("\n".join(lines))
        return

    await state.set_state(Reprice.pick)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{t['reference']} — {t['supplier_label']} → {t['client_label']}",
            callback_data=f"rp:{t['id']}",
        )]
        for t in movable
    ])

    note = ""
    held = [t for t in trades if _reprice_blocker(t) is not None]
    if held:
        note = "\n\nNot offered:\n" + "\n".join(
            f"{t['reference']} — {_reprice_blocker(t)}" for t in held
        )
    await message.answer(
        f"Which trade do you want to move onto the current rate?{note}",
        reply_markup=kb,
    )


@router.callback_query(Reprice.pick, F.data.startswith("rp:"))
async def reprice_pick(call: CallbackQuery, state: FSMContext, repo) -> None:
    """
    Both figures, before and after, spelled out. He is about to change what a
    client will be asked to pay, and it should not take arithmetic to see by
    how much.
    """
    trade_id = int(call.data.split(":", 1)[1])
    trades = {t["id"]: t for t in await repo.repriceable_trades()}
    t = trades.get(trade_id)

    if t is None or _reprice_blocker(t) is not None:
        await state.clear()
        await call.message.edit_text(
            "That trade moved while you were choosing — "
            f"{_reprice_blocker(t) if t else 'it is no longer open'}. "
            "Nothing was changed. Run /reprice again."
        )
        await call.answer()
        return

    new_inr = round_inr(Decimal(t["usdt_received"]) * Decimal(t["current_supply_rate"]))
    difference = new_inr - Decimal(t["inr_expected"])
    direction = "more" if difference > 0 else "less"

    await state.update_data(trade_id=trade_id)
    await state.set_state(Reprice.confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Reprice", callback_data="rp_yes"),
        InlineKeyboardButton(text="Cancel", callback_data="rp_no"),
    ]])
    await call.message.edit_text(
        f"{t['reference']} — {fmt_usdt_plain(t['usdt_received'])} USDT\n\n"
        f"Now:  buy {fmt_rate(t['supply_rate'])} / sell {fmt_rate(t['sell_rate'])}\n"
        f"      client pays ₹{fmt_inr(t['inr_expected'])}\n\n"
        f"New:  buy {fmt_rate(t['current_supply_rate'])} / "
        f"sell {fmt_rate(t['current_sell_rate'])}\n"
        f"      client pays ₹{fmt_inr(new_inr)}\n\n"
        f"That is ₹{fmt_inr(abs(difference))} {direction}.\n\n"
        "Nothing has gone to the client for this trade yet, so this changes "
        "only a figure you are holding.",
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(Reprice.confirm, F.data == "rp_yes")
async def reprice_confirm(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    data = await state.get_data()
    await state.clear()

    ok, msg, detail = await repo.reprice_trade(
        trade_id=data["trade_id"], actor_party_id=party["id"]
    )
    if not ok:
        await call.message.edit_text(msg)
        await call.answer()
        return

    await call.message.edit_text(
        f"{detail['reference']} repriced.\n\n"
        f"Buy  {fmt_rate(detail['old_supply'])} → "
        f"{fmt_rate(detail['new_supply'])}\n"
        f"Sell {fmt_rate(detail['old_sell'])} → "
        f"{fmt_rate(detail['new_sell'])}\n\n"
        f"Client pays ₹{fmt_inr(detail['old_inr'])} → "
        f"₹{fmt_inr(detail['new_inr'])}\n"
        f"Owed out {fmt_usdt_plain(detail['old_owed'])} → "
        f"{fmt_usdt_plain(detail['new_owed'])} USDT\n\n"
        "The client has not been told anything yet. Send it with /issue."
    )
    await call.answer()


@router.callback_query(Reprice.confirm, F.data == "rp_no")
async def reprice_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Cancelled. The trade is unchanged.")
    await call.answer()


# ======================================================================
# /cancel  (answer E4)
# ======================================================================

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, repo) -> None:
    """
    Cancel an open trade, or withdraw a supplier's unmatched claim.

    Both live here because this is where the Bridge looked. 19 September
    2026, after a test /send from Malegao - Sam with the hash "1234test":

        i did a test, but i am unable to cancel it myself?

    He was right — /cancel listed trades, and a claim is not a trade, so
    there was no way to clear one. Rather than a second command he would have
    to know about, the thing he reached for now covers both.

    It is not cosmetic. An unmatched claim is what latest_nomination reads,
    so that test would have pre-selected "Afrin fathima" on Malegao's next
    real deposit.
    """
    trades = await repo.list_open_trades()
    claims = await repo.outstanding_claims()

    if not trades and not claims:
        await message.answer("There are no open trades and no claims waiting.")
        return

    rows = [
        [InlineKeyboardButton(
            text=f"{t['reference']} — {t['supplier_label']} → {t['client_label']}",
            callback_data=f"cx:{t['id']}",
        )]
        for t in trades
    ]
    rows += [
        [InlineKeyboardButton(
            text=f"Claim: {c['supplier_label']} — "
                 f"{c['hash_url'] or 'no hash'}",
            callback_data=f"cs:{c['id']}",
        )]
        for c in claims
    ]

    note = ""
    if claims:
        # Say what a claim is and why leaving it costs something, or the
        # extra buttons are just clutter he scrolls past.
        note = (
            "\n\nA claim is a supplier saying they have sent, with nothing "
            "arrived yet. Left in place, the account they named will be "
            "offered as the nomination on their next deposit."
        )
    # cancel_pick is gated on Cancel.pick. Without this the cx: buttons match
    # no handler at all, aiogram logs "Update is not handled", nothing calls
    # answer(), and the button spins for ever with no error anywhere.
    #
    # Bridge, 22 September 2026: "why was it not letting me cancelling last
    # time? Its doing it again, says loading does not let me."
    #
    # The claim buttons carry no state filter, so clearing a claim always
    # worked — which is what made it look intermittent rather than broken.
    await state.set_state(Cancel.pick)
    await message.answer(
        f"What do you want to cancel?{note}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("cs:"))
async def cancel_claim(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    """
    No reason is asked for. A claim carries no money and no instruction — the
    audit entry records what it was, which is proportionate. Making him
    justify clearing his own test is how a flow gets abandoned halfway.
    """
    await state.clear()
    ok, msg = await repo.drop_pending_send(
        pending_id=int(call.data.split(":", 1)[1]), actor_party_id=party["id"]
    )
    await call.message.edit_text(msg)
    await call.answer()


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

    # The costly case, and the one that keeps happening.
    #
    # 19 September 2026: SUPB5 and SUPD2 were cancelled with the reasons
    # "cleaing" and "clearing" — housekeeping. Neither had been issued and
    # neither had a rupee against it, so on screen they looked like empty
    # rows worth tidying away. They were not. The USDT for both had already
    # gone to the client on the 16th, so cancelling left ₹995,495 of
    # delivered settlement billed to nobody, for the second time in three
    # days.
    #
    # Nothing about the screen said so. The only thing standing between him
    # and that was me putting it in a WhatsApp message, twice, and a message
    # is not a mechanism. The bot can see the payout — it is the same query
    # the double-send guard uses — so it says it here, at the moment it
    # matters, before the reason box.
    paid_out = await repo.prior_payouts_for_trade(trade_id)
    if paid_out and trade["paid_inr"] == 0:
        already = "\n".join(
            f"  {fmt_usdt_plain(p['amount_usdt'])} USDT   "
            f"{p['detected_at']:%d %b %H:%M}"
            for p in paid_out
        )
        note += (
            "\n\nCAREFUL — the client may already have been paid for this:\n\n"
            f"{already}\n\n"
            f"left for them after the deposit landed. Nothing has been "
            f"invoiced, so cancelling writes off ₹{fmt_inr(trade['inr_expected'])} "
            "you are owed. If you have settled it outside the bot, carry on."
        )
        log.warning(
            "cancelling %s, which has %d prior payout(s) and nothing invoiced",
            trade["reference"], len(paid_out),
        )

    # Ask WHICH of the two reasons, because he told us there are only two.
    #
    # Bridge, 19 September 2026:
    #
    #     I'm cancelling what I want to happen is The Usdt goes out to the
    #     client which is fine, but under these circumstances there are only
    #     two reasons to cancel a trade one is that they are sending more
    #     Usdt or they have picked the incorrect account to use
    #
    # That reframes the command. He has never meant "write this trade off" —
    # he means "start this part again", and the USDT going out is expected
    # rather than a problem. Treating every cancel as a write-off is why the
    # money kept ending up unbilled: the tool was doing something other than
    # what he was asking it for.
    #
    # And one of his two reasons does not need a cancel at all. Re-issuing
    # already moves a trade to a different account and now tells the client
    # to stop paying the old one, keeping the deposit, the reference and
    # every payment already logged. Cancelling to change an account throws
    # all of that away to achieve something /issue does in place — and it is
    # what stranded 9,354 USDT twice this week.
    await state.set_state(Cancel.why)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="They are sending more USDT",
                              callback_data="cwmore")],
        [InlineKeyboardButton(text="Wrong account was picked",
                              callback_data="cwacct")],
        [InlineKeyboardButton(text="Something else", callback_data="cwother")],
    ])
    await call.message.edit_text(
        f"Cancelling {trade['reference']}.{note}\n\nWhy?", reply_markup=kb
    )
    await call.answer()


async def _finish_cancel(target, state: FSMContext, party, repo, reason: str) -> None:
    """
    Cancel, then deal with what the cancel leaves behind.

    Shared by every route in so the cleanup cannot be reached by one path and
    missed by another — which is how the 16 September deposits were stranded
    in the first place.
    """
    data = await state.get_data()
    await state.clear()

    ok, msg = await repo.cancel_trade(
        trade_id=data["trade_id"], actor_party_id=party["id"], reason=reason
    )
    if not ok:
        await target.answer(msg)
        return

    stranded = await repo.stranded_deposits(data["trade_id"])
    if not stranded:
        await target.answer(msg)
        return

    lines = [
        msg,
        "",
        "That leaves the supplier's USDT with no trade to pay it:",
        "",
    ]
    lines += [
        f"  {fmt_usdt_plain(d['amount_usdt'])} USDT   "
        f"{d['detected_at']:%d %b %H:%M}"
        for d in stranded
    ]
    lines += [
        "",
        "They have sent it either way. Unless you are settling this by hand, "
        "open a fresh trade for it at the current rate so the client is "
        "invoiced.",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for d in stranded:
        rows.append([InlineKeyboardButton(
            text=f"Reopen {fmt_usdt_plain(d['amount_usdt'])} USDT as a new trade",
            callback_data=f"rd:{d['id']}",
        )])
        # Suppliers who share a sending wallet cannot be told apart by the
        # address the USDT arrives at, so the one the bot picked may be the
        # wrong one (22 September 2026: 4,708 USDT for Tata Mahalaxmi landed
        # on Malegao's address and opened SUPB7 against Malegao).
        rows.append([InlineKeyboardButton(
            text="…under a different vendor",
            callback_data=f"rdv:{d['id']}",
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(
        text="Leave it — settling by hand",
        # Carries the deposit, so the choice can be recorded against the
        # trade rather than merely acknowledged on screen.
        callback_data=f"rd_no:{stranded[0]['id']}",
    )]])
    await target.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(Cancel.why, F.data == "cwacct")
async def cancel_wrong_account(call: CallbackQuery, state: FSMContext, repo) -> None:
    """
    The one that should not be a cancel.

    /issue re-issues the same trade to different accounts, allocates only
    what is still outstanding, keeps every payment already logged, and since
    18 September tells the client in as many words to stop paying the old
    account. Cancelling throws the reference and the deposit away to reach
    the same place by a worse road.
    """
    data = await state.get_data()
    trade = await repo.trade_detail(data["trade_id"])
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Re-issue {trade['reference']} now",
                              callback_data=f"cf:{trade['id']}")],
        [InlineKeyboardButton(text="No, cancel it anyway",
                              callback_data=f"cwforce:{trade['id']}")],
    ])
    await call.message.edit_text(
        f"You do not need to cancel {trade['reference']} for that.\n\n"
        "Re-issuing it sends the client new details for the right account "
        "and tells them to stop paying the old one. The deposit, the "
        "reference and anything already paid all stay where they are — only "
        "the unpaid balance is re-allocated.",
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(Cancel.why, F.data == "cwmore")
async def cancel_more_usdt(
    call: CallbackQuery, state: FSMContext, party, repo
) -> None:
    """
    A second deposit does not always need a cancel either.

    An UNINSTRUCTED trade absorbs a further deposit on its own within the
    merge window and the total goes up. Only once the client is holding a
    figure does a new deposit have to become its own trade — the client's own
    rule from 11 September, after 1,859 USDT quietly became 4,859.
    """
    data = await state.get_data()
    trade = await repo.trade_detail(data["trade_id"])

    if trade["instructed_at"] is None:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="No, cancel it anyway",
                                  callback_data=f"cwforce:{trade['id']}")],
        ])
        await call.message.edit_text(
            f"You do not need to cancel {trade['reference']} for that — it "
            "has not gone to the client yet.\n\n"
            "A further deposit from this supplier joins this same trade and "
            "the total goes up by itself. Issue it once everything has "
            "landed.",
            reply_markup=kb,
        )
        await call.answer()
        return

    # Instructed. The client is holding a figure, so the extra genuinely has
    # to be its own trade and cancelling is the right call.
    await _finish_cancel(
        call.message, state, party, repo, "supplier is sending more USDT",
    )
    await call.answer()


@router.callback_query(Cancel.why, F.data == "cwother")
async def cancel_other_reason(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Cancel.reason)
    await call.message.edit_text(
        "Why? (this goes in the audit log)"
    )
    await call.answer()


@router.callback_query(F.data.startswith("cwforce:"))
async def cancel_anyway(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    """He has been told there is a better route and wants this one regardless."""
    await state.update_data(trade_id=int(call.data.split(":", 1)[1]))
    await state.set_state(Cancel.reason)
    await call.message.edit_text("Why? (this goes in the audit log)")
    await call.answer()


@router.message(Cancel.reason)
async def cancel_reason(message: Message, state: FSMContext, party, repo) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Give a short reason so the record makes sense later.")
        return

    # The cleanup lives in _finish_cancel so every route into a cancel gets
    # it. When it lived here, only the typed-reason path had it — and a
    # second route added later would silently have gone without, which is
    # precisely how the 16 September deposits were stranded.
    await _finish_cancel(message, state, party, repo, reason)


@router.callback_query(F.data.startswith("rdv:"))
async def reopen_under_different_vendor(
    call: CallbackQuery, party, repo
) -> None:
    """
    Ask who actually sent it, when the address cannot say.

    The client is not in question — the money arrived at an address that
    belongs to one client and no other. Only the supplier is, so only the
    supplier is offered.
    """
    deposit_id = int(call.data.split(":", 1)[1])
    pairing = await repo.pairing_for_deposit(deposit_id)
    if pairing is None:
        await call.message.answer("That deposit is no longer on an internal wallet.")
        await call.answer()
        return

    suppliers = await repo.suppliers_for_client(pairing["client_id"])
    others = [s for s in suppliers if s["id"] != pairing["supplier_id"]]
    if not others:
        await call.message.answer(
            "This client is only paired with "
            f"{pairing['supplier_label']}, so there is no one else it could "
            "have come from. Add the vendor with /addvendor first."
        )
        await call.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=s["label"], callback_data=f"rds:{deposit_id}:{s['id']}"
        )]
        for s in others
    ])
    await call.message.answer(
        f"It arrived on {pairing['supplier_label']}'s address. Who actually "
        "sent it?\n\nThe trade opens under whoever you pick — their rate, "
        "their accounts, their deal numbers. The deposit still records where "
        "it landed.",
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("rds:"))
async def reopen_stranded_deposit_as(
    call: CallbackQuery, party, repo
) -> None:
    _, deposit_id, supplier_id = call.data.split(":", 2)
    await _reopen(call, party, repo, int(deposit_id), int(supplier_id))


@router.callback_query(F.data.startswith("rd:"))
async def reopen_stranded_deposit(
    call: CallbackQuery, party, repo
) -> None:
    await _reopen(call, party, repo, int(call.data.split(":", 1)[1]), None)


async def _reopen(call, party, repo, deposit_id: int, supplier_id) -> None:
    ok, msg, detail = await repo.reopen_deposit_as_trade(
        deposit_id=deposit_id, actor_party_id=party["id"],
        supplier_id=supplier_id,
    )
    if not ok:
        await call.message.answer(msg)
        await call.answer()
        return

    lines = [
        f"{detail['reference']} opened for "
        f"{fmt_usdt_plain(detail['usdt'])} USDT.",
        "",
        f"Rate        {fmt_rate(detail['supply_rate'])} / "
        f"{fmt_rate(detail['sell_rate'])}",
        f"Client pays ₹{fmt_inr(detail['inr_expected'])}",
        f"Owed out    {fmt_usdt_plain(detail['usdt_owed'])} USDT",
    ]
    # Saying so out loud. The figure moving on a cancelled trade looks like a
    # second problem if it arrives unexplained, and it is the fix for the one
    # that made every export since 15 September overstate the period.
    if detail["restated"]:
        r = detail["restated"]
        lines += [
            "",
            f"{r['reference']} restated to the deposits it still holds: "
            f"{fmt_usdt_plain(r['from_usdt'])} → {fmt_usdt_plain(r['to_usdt'])} "
            f"USDT, ₹{fmt_inr(r['from_inr'])} → ₹{fmt_inr(r['to_inr'])}.",
        ]
    # A trade whose deal number belongs to a vendor the USDT did not arrive
    # from will look like a mistake to anyone reading it later. Say it was
    # deliberate, on the message he keeps.
    if detail.get("reattributed"):
        lines += [
            "",
            "Opened against the vendor you named, not the address it landed "
            "on. The deposit still records where it arrived.",
        ]
    lines += ["", "Send it to the client with /issue."]

    await call.message.answer("\n".join(lines))
    await call.answer()


@router.callback_query(F.data.startswith("rd_no"))
async def leave_stranded_deposit(call: CallbackQuery, party, repo) -> None:
    """
    Settling by hand is a decision, so it gets recorded like one.

    This used to say "left as it is" and write nothing, which meant the bot
    could not tell a trade he had settled himself from one that had been
    forgotten. /progress then reported 84,799 USDT against IndoLondon as
    never invoiced, most of it his own hand-settled business — a warning
    that fires on normal work, which is a warning nobody reads twice.
    """
    deposit_id = call.data.split(":", 1)[1] if ":" in call.data else None
    await call.message.edit_reply_markup(reply_markup=None)

    if deposit_id is None:
        await call.message.answer(
            "Left as it is. The deposit stays on the cancelled trade and the "
            "client is not invoiced for it."
        )
        await call.answer()
        return

    ok, msg = await repo.mark_settled_by_hand(
        deposit_id=int(deposit_id), actor_party_id=party["id"]
    )
    await call.message.answer(
        msg if ok else
        f"{msg}\n\nLeft as it is — the client is not invoiced for it."
    )
    await call.answer()


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
