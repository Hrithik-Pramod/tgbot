"""
Supplier bot: /account, /account_remove, /send, /progress.

Telegram command names cannot contain a space, so the brief's "/account remove"
becomes /account_remove.
"""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.summary import render_collection_progress

log = logging.getLogger(__name__)
router = Router()

# Indian IFSC: 4 letters, then 0, then 6 alphanumerics.
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
# Indian account numbers run roughly 9-18 digits across banks.
ACCOUNT_RE = re.compile(r"^\d{9,18}$")


class AddAccount(StatesGroup):
    name = State()
    number = State()
    ifsc = State()


class SendFlow(StatesGroup):
    hash_url = State()
    account = State()


# ----------------------------------------------------------------- /account

@router.message(Command("account"))
async def cmd_account(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccount.name)
    await message.answer("Account Name -")


@router.message(AddAccount.name)
async def account_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Account name cannot be empty.")
        return
    await state.update_data(account_name=name)
    await state.set_state(AddAccount.number)
    await message.answer("Account Number -")


@router.message(AddAccount.number)
async def account_number(message: Message, state: FSMContext) -> None:
    number = re.sub(r"[\s-]+", "", message.text or "")
    if not ACCOUNT_RE.match(number):
        await message.answer(
            "That does not look like an account number. "
            "Indian account numbers are 9 to 18 digits."
        )
        return
    await state.update_data(account_number=number)
    await state.set_state(AddAccount.ifsc)
    await message.answer("IFSC Code -")


@router.message(AddAccount.ifsc)
async def account_ifsc(message: Message, state: FSMContext, party, repo, notifier) -> None:
    ifsc = re.sub(r"\s+", "", (message.text or "")).upper()
    if not IFSC_RE.match(ifsc):
        await message.answer(
            "That is not a valid IFSC. The format is four letters, a zero, "
            "then six characters — for example EXBK0001234."
        )
        return

    data = await state.get_data()
    await repo.add_bank_account(
        party_id=party["id"],
        account_name=data["account_name"],
        account_number=data["account_number"],
        ifsc=ifsc,
    )
    await state.clear()

    detail = (
        f"Account Name - {data['account_name']}\n"
        f"Account Number - {data['account_number']}\n"
        f"IFSC Code - {ifsc}"
    )
    await message.answer(f"Stored.\n\n{detail}")

    # The brief requires the Bridge to be notified of every account change.
    await notifier.to_bridge(f"New account registered by {party['label']}\n\n{detail}")


# ---------------------------------------------------------------- /progress

@router.message(Command("progress"))
async def cmd_progress(message: Message, party, repo) -> None:
    """
    How much of the current trade's INR has landed.

    Client request, 10 September 2026: "collection progress, can i add this to
    the suppliers as an option? just another thing they always ask." Asking the
    bot costs the Bridge nothing; being asked the same question by every
    supplier costs them their day.

    Deliberately says nothing about rates, margin or the deal reference — the
    supplier sees the collection of their own INR and no more (decision D4).
    """
    trade = await repo.open_trade_for_supplier(party["id"])
    if trade is None:
        await message.answer("You have no trade in progress at the moment.")
        return

    if not trade["inr_expected"]:
        # The deposit is in but the Bridge has not issued instructions yet, so
        # there is no total to measure against.
        await message.answer(
            "Your deposit has been received. Payment instructions have not "
            "been issued yet."
        )
        return

    await message.answer(render_collection_progress(
        expected_inr=trade["inr_expected"],
        paid_inr=trade["paid_inr"],
    ))


# ---------------------------------------------------------- /account_remove

@router.message(Command("account_remove"))
async def cmd_account_remove(message: Message, party, repo) -> None:
    accounts = await repo.list_bank_accounts(party["id"])
    if not accounts:
        await message.answer("You have no registered accounts.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{a['account_name']} — {a['account_number'][-4:]}",
            callback_data=f"rm:{a['id']}",
        )]
        for a in accounts
    ])
    await message.answer("Which account do you want to remove?", reply_markup=kb)


@router.callback_query(F.data.startswith("rm:"))
async def confirm_remove(call: CallbackQuery) -> None:
    account_id = int(call.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes, delete", callback_data=f"rmyes:{account_id}"),
        InlineKeyboardButton(text="Cancel", callback_data="rmno"),
    ]])
    await call.message.edit_text("Do you want to delete this account?", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("rmyes:"))
async def do_remove(call: CallbackQuery, party, repo, notifier) -> None:
    account_id = int(call.data.split(":", 1)[1])
    ok = await repo.remove_bank_account(account_id=account_id, party_id=party["id"])
    if not ok:
        await call.message.edit_text("That account could not be removed.")
        await call.answer()
        return

    await call.message.edit_text("Account removed.")
    await notifier.to_bridge(f"{party['label']} removed a registered account.")
    await call.answer()


@router.callback_query(F.data == "rmno")
async def cancel_remove(call: CallbackQuery) -> None:
    await call.message.edit_text("Cancelled.")
    await call.answer()


# -------------------------------------------------------------------- /send

@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext, party, repo) -> None:
    """
    B8: manual hash entry is the fallback for when on-chain monitoring fails,
    not the primary path. The monitor normally detects the deposit first.
    """
    accounts = await repo.list_bank_accounts(party["id"])
    if not accounts:
        await message.answer("Register an account with /account first.")
        return
    await state.set_state(SendFlow.hash_url)
    await message.answer("Hash URL?")


@router.message(SendFlow.hash_url)
async def send_hash(message: Message, state: FSMContext, party, repo) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Send the transaction hash or its explorer URL.")
        return

    await state.update_data(hash_url=text)
    accounts = await repo.list_bank_accounts(party["id"])
    await state.set_state(SendFlow.account)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a["account_name"], callback_data=f"sacct:{a['id']}")]
        for a in accounts
    ])
    await message.answer("Which account should the funds be sent to?", reply_markup=kb)


@router.callback_query(SendFlow.account, F.data.startswith("sacct:"))
async def send_account(call: CallbackQuery, state: FSMContext, party, repo, notifier) -> None:
    """
    Record the supplier's nominated account.

    Client decision, 7 September 2026: one account per trade, chosen by the
    supplier at the point of sending. If a trade is already open for this
    supplier, the nomination is attached to it so the Bridge sees it prefilled
    when they confirm. If the deposit has not been detected yet, the nomination
    is still reported and will be picked up when the trade opens.
    """
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    await state.clear()

    accounts = {a["id"]: a for a in await repo.list_bank_accounts(party["id"])}
    account = accounts.get(account_id)
    if account is None:
        await call.message.edit_text("That account is no longer available.")
        await call.answer()
        return

    attached = await repo.nominate_account(
        supplier_id=party["id"], account_id=account_id, actor_party_id=party["id"]
    )

    # Recorded, not announced.
    #
    # This used to notify the Bridge the moment a supplier said they had sent.
    # That is a claim; funds arriving on chain is a fact, and the Bridge should
    # be acting on facts (client request, 11 September 2026: "only when
    # validation of funds landing does the bot notify me").
    #
    # The claim is stored so it can surface attached to the deposit when it
    # lands, and so a claim that never materialises can be reported instead of
    # vanishing.
    await repo.record_pending_send(
        supplier_id=party["id"],
        account_id=account_id,
        hash_url=data.get("hash_url"),
    )

    if attached:
        # Their own account, not the deal reference. "SUPA1" is the Bridge's
        # internal numbering and the client asked on 10 September 2026 that
        # suppliers not see it; this message was still printing it.
        await call.message.edit_text(
            f"Noted. INR for your current trade will go to "
            f"{account['account_name']}."
        )
    else:
        await call.message.edit_text(
            "Noted. This will be applied once your deposit is detected."
        )
    await call.answer()
