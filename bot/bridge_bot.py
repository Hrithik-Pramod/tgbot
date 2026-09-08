"""
Bridge bot: /setrate, /wallet, /walletchange, /send, /summary.

These handlers only run for commands arriving in the registered Bridge chat
(see bot/auth.py). Set STRICT_BRIDGE_USER=true to additionally require that the
sender is BRIDGE_USER_ID.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.money import MoneyError, fmt_inr, fmt_usdt, to_decimal

log = logging.getLogger(__name__)
router = Router()


class SetRate(StatesGroup):
    supplier = State()
    client = State()
    supply_rate = State()
    sell_rate = State()
    confirm = State()


class WalletChange(StatesGroup):
    which = State()
    address = State()
    confirm = State()


class BridgeSend(StatesGroup):
    supplier = State()
    client = State()
    hash_url = State()
    account = State()


def _party_keyboard(parties, prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p["label"], callback_data=f"{prefix}:{p['id']}")]
        for p in parties
    ])


# ----------------------------------------------------------------- /setrate

@router.message(Command("setrate"))
async def cmd_setrate(message: Message, state: FSMContext, repo) -> None:
    suppliers = await repo.list_parties("supplier")
    if not suppliers:
        await message.answer("No suppliers are registered yet.")
        return
    await state.set_state(SetRate.supplier)
    await message.answer("Which supplier?", reply_markup=_party_keyboard(suppliers, "sr_sup"))


@router.callback_query(SetRate.supplier, F.data.startswith("sr_sup:"))
async def setrate_supplier(call: CallbackQuery, state: FSMContext, repo) -> None:
    await state.update_data(supplier_id=int(call.data.split(":", 1)[1]))
    clients = await repo.list_parties("client")
    if not clients:
        await call.message.edit_text("No clients are registered yet.")
        await state.clear()
        await call.answer()
        return
    await state.set_state(SetRate.client)
    await call.message.edit_text("Which client?", reply_markup=_party_keyboard(clients, "sr_cli"))
    await call.answer()


@router.callback_query(SetRate.client, F.data.startswith("sr_cli:"))
async def setrate_client(call: CallbackQuery, state: FSMContext, repo) -> None:
    data = await state.get_data()
    client_id = int(call.data.split(":", 1)[1])
    await state.update_data(client_id=client_id)

    # Show the rate currently in force so the Bridge can confirm rather than
    # retype it, which is what the brief asks for.
    current = await repo.current_rate(data["supplier_id"], client_id)
    if current:
        age = float(current["age_hours"])
        stale = "  (STALE)" if age > 24 else ""
        note = (
            f"Current supply rate: {current['supply_rate']}\n"
            f"Current sell rate:   {current['sell_rate']}\n"
            f"Set {age:.1f} hours ago{stale}\n\n"
        )
    else:
        note = "No rate is set for this pairing yet.\n\n"

    await state.set_state(SetRate.supply_rate)
    await call.message.edit_text(f"{note}Enter the supply rate (INR per 1 USDT):")
    await call.answer()


@router.message(SetRate.supply_rate)
async def setrate_supply(message: Message, state: FSMContext) -> None:
    try:
        rate = to_decimal(message.text or "")
    except MoneyError:
        await message.answer("I could not read that as a rate. Send just the number.")
        return
    if rate <= 0:
        await message.answer("Rate must be greater than zero.")
        return
    await state.update_data(supply_rate=str(rate))
    await state.set_state(SetRate.sell_rate)
    await message.answer("Enter the sell rate (INR per 1 USDT):")


@router.message(SetRate.sell_rate)
async def setrate_sell(message: Message, state: FSMContext) -> None:
    try:
        rate = to_decimal(message.text or "")
    except MoneyError:
        await message.answer("I could not read that as a rate. Send just the number.")
        return
    if rate <= 0:
        await message.answer("Rate must be greater than zero.")
        return

    data = await state.get_data()
    supply = to_decimal(data["supply_rate"])

    await state.update_data(sell_rate=str(rate))
    await state.set_state(SetRate.confirm)

    # A sell rate at or below the supply rate means the trade loses money.
    # Worth stopping on, since it is almost always a typo.
    warning = ""
    if rate <= supply:
        warning = (
            "\n\nWARNING: the sell rate is not above the supply rate. "
            "This trade would run at a loss. Check before confirming."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="sr_yes"),
        InlineKeyboardButton(text="Cancel", callback_data="sr_no"),
    ]])
    await message.answer(
        f"Supply rate: {supply}\nSell rate: {rate}\n\nIs this correct?{warning}",
        reply_markup=kb,
    )


@router.callback_query(SetRate.confirm, F.data == "sr_yes")
async def setrate_confirm(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    data = await state.get_data()
    await repo.set_rate(
        supplier_id=data["supplier_id"],
        client_id=data["client_id"],
        supply_rate=to_decimal(data["supply_rate"]),
        sell_rate=to_decimal(data["sell_rate"]),
        set_by=party["id"],
    )
    await state.clear()
    await call.message.edit_text("Rate is set in the system.")
    await call.answer()


@router.callback_query(F.data == "sr_no")
async def setrate_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Cancelled. No rate was changed.")
    await call.answer()


# ------------------------------------------------------------------ /wallet

@router.message(Command("wallet"))
async def cmd_wallet(message: Message, repo) -> None:
    """
    Shows the internal-to-client linkage. The brief calls this "very important
    for routing and calculations" - the internal wallet IS the routing key
    (answer C3), so this view is how the Bridge verifies the wiring.
    """
    wallets = await repo.list_wallets()
    if not wallets:
        await message.answer("No wallets are configured.")
        return

    internal = [w for w in wallets if w["is_internal"]]
    external = [w for w in wallets if not w["is_internal"]]

    lines: list[str] = []
    if internal:
        lines.append("Internal wallets (supplier → client pairings)")
        lines.append("")
        for w in internal:
            flag = "" if w["is_monitored"] else "   [not monitored]"
            lines.append(f"{w['supplier_label']} → {w['client_label']}{flag}")
            lines.append(f"Internal wallet {w['address']}")
            lines.append("")

    if external:
        lines.append("Counterparty wallets")
        lines.append("")
        for w in external:
            flag = "" if w["is_monitored"] else "   [not monitored]"
            lines.append(f"{w['owner_label']} = {w['address']}{flag}")

    await message.answer("\n".join(lines).rstrip())


# ------------------------------------------------------------ /walletchange

@router.message(Command("walletchange"))
async def cmd_walletchange(message: Message, state: FSMContext, repo) -> None:
    wallets = await repo.list_wallets()
    if not wallets:
        await message.answer("No wallets are configured.")
        return

    rows = []
    for w in wallets:
        if w["is_internal"]:
            text = f"Internal: {w['supplier_label']} → {w['client_label']}"
        else:
            text = f"{w['owner_label']}"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"wc:{w['id']}")])

    await state.set_state(WalletChange.which)
    await message.answer("Which wallet?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(WalletChange.which, F.data.startswith("wc:"))
async def walletchange_which(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(wallet_id=int(call.data.split(":", 1)[1]))
    await state.set_state(WalletChange.address)
    await call.message.edit_text("Enter the new address:")
    await call.answer()


@router.message(WalletChange.address)
async def walletchange_address(message: Message, state: FSMContext) -> None:
    address = (message.text or "").strip()

    # TRON base58 addresses start with T and are 34 characters (B1: TRC20 only).
    if not (address.startswith("T") and len(address) == 34):
        await message.answer(
            "That does not look like a TRON address. "
            "TRC20 addresses start with T and are 34 characters long."
        )
        return

    await state.update_data(address=address)
    await state.set_state(WalletChange.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="wc_yes"),
        InlineKeyboardButton(text="Cancel", callback_data="wc_no"),
    ]])
    await message.answer(f"Set the address to:\n{address}\n\nConfirm?", reply_markup=kb)


@router.callback_query(WalletChange.confirm, F.data == "wc_yes")
async def walletchange_confirm(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    data = await state.get_data()
    await repo.update_wallet_address(
        wallet_id=data["wallet_id"],
        new_address=data["address"],
        actor_party_id=party["id"],
    )
    await state.clear()
    await call.message.edit_text(
        "Updated. Monitoring has moved to the new address."
    )
    await call.answer()


@router.callback_query(F.data == "wc_no")
async def walletchange_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Cancelled. No wallet was changed.")
    await call.answer()


# -------------------------------------------------------------------- /send

@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext, repo) -> None:
    """
    Forward a send instruction, per the brief: supplier, client, hash, account.

    This is the Bridge's own manual path. It exists alongside the automatic
    deposit detection so a trade can still be pushed through when the monitor
    has not seen the transaction — B8's fallback, from your side rather than
    the supplier's.
    """
    suppliers = await repo.list_parties("supplier")
    if not suppliers:
        await message.answer("No suppliers are registered.")
        return
    await state.set_state(BridgeSend.supplier)
    await message.answer("Which supplier?", reply_markup=_party_keyboard(suppliers, "bs_sup"))


@router.callback_query(BridgeSend.supplier, F.data.startswith("bs_sup:"))
async def send_supplier(call: CallbackQuery, state: FSMContext, repo) -> None:
    await state.update_data(supplier_id=int(call.data.split(":", 1)[1]))
    clients = await repo.list_parties("client")
    if not clients:
        await call.message.edit_text("No clients are registered.")
        await state.clear()
        await call.answer()
        return
    await state.set_state(BridgeSend.client)
    await call.message.edit_text("Which client?", reply_markup=_party_keyboard(clients, "bs_cli"))
    await call.answer()


@router.callback_query(BridgeSend.client, F.data.startswith("bs_cli:"))
async def send_client(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(client_id=int(call.data.split(":", 1)[1]))
    await state.set_state(BridgeSend.hash_url)
    await call.message.edit_text("Hash URL?")
    await call.answer()


@router.message(BridgeSend.hash_url)
async def send_hash(message: Message, state: FSMContext, repo) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Send the transaction hash or its explorer URL.")
        return

    data = await state.get_data()
    accounts = await repo.list_bank_accounts(data["supplier_id"])
    if not accounts:
        await message.answer("That supplier has no registered accounts.")
        await state.clear()
        return

    await state.update_data(hash_url=text)
    await state.set_state(BridgeSend.account)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a["account_name"], callback_data=f"bs_acct:{a['id']}")]
        for a in accounts
    ])
    await message.answer("Which account should the funds go to?", reply_markup=kb)


@router.callback_query(BridgeSend.account, F.data.startswith("bs_acct:"))
async def send_account(call: CallbackQuery, state: FSMContext, repo, notifier) -> None:
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    await state.clear()

    accounts = {a["id"]: a for a in await repo.list_bank_accounts(data["supplier_id"])}
    account = accounts.get(account_id)
    if account is None:
        await call.message.edit_text("That account is no longer available.")
        await call.answer()
        return

    async with repo.pool.acquire() as conn:
        labels = await conn.fetchrow(
            "SELECT s.label AS s, c.label AS c FROM parties s, parties c "
            "WHERE s.id = $1 AND c.id = $2",
            data["supplier_id"], data["client_id"],
        )

    summary = (
        f"Send instruction\n"
        f"{labels['s']} → {labels['c']}\n"
        f"Hash {data['hash_url']}\n"
        f"Send to Account name {account['account_name']} "
        f"Account Number {account['account_number']} "
        f"IFSC {account['ifsc']}"
    )
    await call.message.edit_text(summary)
    await notifier.to_bridge(summary)
    await call.answer()


# ----------------------------------------------------------------- /summary

@router.message(Command("summary"))
async def cmd_summary(message: Message, repo) -> None:
    """F2: the current open trade only."""
    suppliers = await repo.list_parties("supplier")
    if not suppliers:
        await message.answer("No suppliers are registered.")
        return
    await message.answer(
        "Which supplier?", reply_markup=_party_keyboard(suppliers, "sum")
    )


@router.callback_query(F.data.startswith("sum:"))
async def summary_supplier(call: CallbackQuery, repo) -> None:
    supplier_id = int(call.data.split(":", 1)[1])

    async with repo.pool.acquire() as conn:
        trades = await conn.fetch(
            """
            SELECT t.reference, t.usdt_received, t.inr_expected, t.status,
                   c.label AS client_label,
                   COALESCE(SUM(p.amount_inr), 0) AS paid
            FROM trades t
            JOIN parties c ON c.id = t.client_id
            LEFT JOIN payments p ON p.trade_id = t.id
            WHERE t.supplier_id = $1 AND t.status IN ('open', 'awaiting_payment')
            GROUP BY t.id, c.label
            ORDER BY t.opened_at
            """,
            supplier_id,
        )

    if not trades:
        await call.message.edit_text("No open trades for this supplier.")
        await call.answer()
        return

    lines = []
    for t in trades:
        outstanding = t["inr_expected"] - t["paid"]
        lines.append(f"Transaction {t['reference']} → {t['client_label']}")
        lines.append(f"USDT received: {fmt_usdt(t['usdt_received'])}")
        lines.append(f"INR expected:  ₹{fmt_inr(t['inr_expected'])}")
        lines.append(f"Collected:     ₹{fmt_inr(t['paid'])}")
        lines.append(f"Outstanding:   ₹{fmt_inr(outstanding)}")
        lines.append("")

    await call.message.edit_text("\n".join(lines).rstrip())
    await call.answer()
